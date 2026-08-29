# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What the test suite shares, and how a vendored vector is read.

Vectors live in `tests/**/_data/` and are vendored rather than fetched:
a test that downloads its own input is a test whose verdict depends on
somebody else's uptime, and one that regenerates its input is a test
that agrees with whatever this tree already does. `tests/_data/README.md`
says where each file came from, pinned to a commit and to a blob.

The chain, transaction and wait builders below this file's own two
readers are shared the same way, and none of them is a fixture: a
`conftest.py` fixture is process-wide and built once per test, where a
header chain or a transaction has to be built fresh, sized and shaped
differently, by whichever test needs one -- so these are plain
functions the tests import directly, in `tests/unit/`,
`tests/functional/` and `tests/integration/` alike.
"""

import json
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from bitcoin_core_rpc import BitcoinCoreRpcClient, http_request
from btclib.block import (
    Block,
    BlockHeader,
    bip34_commitment,
    merkle_root_and_mutated_from_transactions,
)
from btclib.block.mining import mine
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS
from btclib.exceptions import BTClibValueError
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node.chains import RegTest
from btclib_node.constants import COINBASE_MATURITY
from btclib_node.p2p.address import peer_address

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib.p2p.addrv2 import NetworkAddressV2

    from btclib_node import Node


_TESTS_DIR = Path(__file__).parent


def load(*relative_path: str, encoding: str = "ascii") -> Any:
    """Read a vendored JSON vector file, named relative to `tests/`.

    Naming a vector file by its path from the test suite root, rather
    than from the test module that reads it, is what lets two modules
    share one file without the `parent.parent` walk that breaks the
    moment a test module moves.
    """
    with _TESTS_DIR.joinpath(*relative_path).open(encoding=encoding) as file_:
        return json.load(file_)


# what makes an id unreadable in a report and unusable in a -k
# expression: anything that is not a letter or a digit. Bitcoin Core's
# own notes hold spaces, commas and parentheses
_NOT_IN_AN_ID = re.compile(r"[^0-9A-Za-z]+")


def vector_id(index: int, *description: object) -> str:
    """Name the vector at `index`: where it is, then what it is about.

    The position alone is what parametrize generates on its own, and it
    says where in the file to look but not what the case was testing;
    the description alone -- a height, one of Core's notes -- reads well
    but is neither unique nor always there. Both, so that the red line
    of a report identifies the vector and says what it is.
    """
    text = "-".join(str(d) for d in description if d)
    text = _NOT_IN_AN_ID.sub("-", text).strip("-")
    return f"{index}-{text[:60]}" if text else str(index)


class WaitTimeoutError(TimeoutError):
    """A poll below gave up: whatever it waited for never became true.

    `TimeoutError`, and not a bare `Exception` (`TRY002`): the failure
    genuinely is a timeout, so the builtin already named for it is more
    honest than a bespoke class would be, and it still needs its own
    `__init__` -- `TRY003` flags a message built at the raise site
    whichever class carries it, builtin or not.
    """

    def __init__(self, message: str) -> None:
        """Build the error around the caller's own account of the deadline."""
        super().__init__(message)


def log_recorder() -> tuple[list[str], Callable[..., None]]:
    """Return a list and a stand-in for a `Logger` method that fills it.

    `G004` moved every production `logger.*` call this tree makes off
    an f-string and onto the `%`-style form `logging` itself defers
    formatting for, so a call like `logger.warning("...: %s", value)`
    reaches whatever `warning` is bound to as two arguments, not one
    already-formatted string. A stand-in of `list.append` alone -- one
    argument only -- broke the moment the call it captured carried a
    second one; this applies the same `%` formatting the real
    `logging.Logger` would, so a test asserting on the finished message
    reads the same string either way.
    """
    entries: list[str] = []

    def record(msg: str, *args: object) -> None:
        entries.append(msg % args if args else msg)

    return entries, record


class _ListensOnAPort(Protocol):
    # what wait_until_listening needs: a manager, or a stand-in for one
    listening: threading.Event
    port: int | None


# every chain built here is regtest's, from its target down to the block
# these hang off: a header has to be dated after the median of the
# eleven before it, so a chain whose first block predates the genesis it
# extends is one the index refuses
GENESIS_TIME = RegTest().genesis.time


def generate_random_header_chain(
    length: int, start: bytes, previous_time: datetime = GENESIS_TIME
) -> list[BlockHeader]:
    """Return a chain of solved headers, a second apart, off `start`.

    `previous_time` is the timestamp of the block `start` names, the
    genesis' by default: a chain forking off a block further up has to
    be given that block's, or its first header is older than the median
    it has to beat.
    """
    chain: list[BlockHeader] = []
    for x in range(length):
        previous_block_hash = chain[-1].hash if chain else start
        header = BlockHeader(
            version=70015,
            previous_block_hash=previous_block_hash,
            merkle_root=secrets.token_bytes(32),
            time=previous_time + timedelta(seconds=x + 1),
            bits=REGTEST_POW_LIMIT_BITS,
            nonce=1,
            check_validity=False,
        )
        brute_force_nonce(header)
        chain.append(header)
    return chain


def generate_random_transaction(
    prevouthash: bytes | None = None, value: int = 50 * 10**8
) -> Tx:
    """Return a one-input, one-output transaction spending `prevouthash`.

    `prevouthash` names the outpoint being spent, defaulting to a random
    one where the caller only needs a transaction that is structurally
    valid rather than one that actually spends something built earlier
    in the same test. `value` is what the new output pays, the subsidy
    by default -- what `prevouthash` is worth where it names a coinbase
    still paying it in full, which stops holding past a halving
    (`generate_random_chain` is what has to pass its own).
    """
    prevouthash = prevouthash or secrets.token_bytes(32)
    tx_in = TxIn(
        prev_out=OutPoint(prevouthash, 0),
        script_sig=script.serialize([secrets.token_bytes(32)]),
        sequence=0xFFFFFFFF,
    )
    tx_out = TxOut(
        value=value,
        script_pub_key=script.serialize([secrets.token_bytes(32)]),
    )
    return Tx(
        version=1,
        lock_time=0,
        vin=[tx_in],
        vout=[tx_out],
    )


def generate_coinbase(value: int = 50 * 10**8, height: int | None = None) -> Tx:
    """Return a coinbase transaction paying `value`, the subsidy by default.

    A null-outpoint input marks it as a coinbase; `value` lets a test
    fund an output with an exact, known amount rather than the subsidy,
    where what it is checking is the amount rather than the block being
    otherwise ordinary. `height`, where given, is prefixed onto the
    script_sig as BIP34's own commitment (`bip34_commitment`) -- what a
    chain enforcing BIP34 needs to connect the block this funds, which
    regtest does from height 1 on. Left out, the script_sig commits to
    no height at all, which is what a test of that rule itself builds.
    """
    script_sig = script.serialize([secrets.token_bytes(32)])
    if height is not None:
        script_sig = bip34_commitment(height) + script_sig
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(),
                script_sig=script_sig,
                sequence=0xFFFFFFFF,
            )
        ],
        vout=[
            TxOut(
                value=value,
                script_pub_key=script.serialize([secrets.token_bytes(32)]),
            )
        ],
    )


def build_block(
    previous_block_hash: bytes,
    transactions: list[Tx],
    height: int,
    time: datetime | None = None,
) -> Block:
    """Return a solved regtest block extending `previous_block_hash`.

    `height` only dates the header -- one second past genesis per
    height, matching `generate_random_header_chain`'s own spacing -- so
    two blocks built at the same height carry the same timestamp, which
    is fine for a caller building disjoint forks but not for one
    building a single chain out of order. `time` overrides that dating
    outright, for a caller that needs a block recent against the real
    clock rather than dated relative to `GENESIS_TIME` --
    `Node.is_initial_block_download`'s own tip-age half is never
    satisfied by a block dated the ordinary way. `generate_random_chain`'s
    own `tip_time` reaches this same parameter, for the one caller that
    needs its own chain's tip to qualify.
    """
    header = BlockHeader(
        version=70015,
        previous_block_hash=previous_block_hash,
        merkle_root=merkle_root_and_mutated_from_transactions(transactions)[0],
        time=time if time is not None else GENESIS_TIME + timedelta(seconds=height + 1),
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)
    # Block.__init__ validates against mainnet's pow limit, which no
    # regtest block meets; brute_force_nonce has already checked this
    # header against the limit that does apply to it.
    return Block(header, transactions, check_validity=False)


def generate_random_chain(
    length: int, start: bytes, *, tip_time: datetime | None = None
) -> list[Block]:
    """Return `length` solved blocks extending `start`.

    `start` is assumed to be its own chain's genesis (height 0), so the
    block built at loop position `x` sits at real height `x + 1` --
    true of every caller in this tree but one, an orphan fork never
    offered to `update_chain` (`filter_index_test.py`'s own
    `orphan`) -- which is the height each coinbase commits to (BIP34):
    regtest enforces it from height 1, and a chain meant to connect has
    to carry one that does.

    `tip_time` overrides only the last block's own timestamp, `build_block`'s
    own `time` reaching that one call site and no other: every earlier
    block keeps its ordinary `GENESIS_TIME`-relative dating, spaced the
    way BIP34 height and maturity above already need it to be, and the
    chain's own tip is what `Node.is_initial_block_download`
    (`main.update_ibd_status`) reads for its tip-age half -- a caller
    whose test needs this node to read as caught up names a recent one
    here rather than the whole chain being dated against the real clock,
    which two-hour drift and this function's own second-per-height
    spacing would answer for a chain of any real length.

    Every block up to `COINBASE_MATURITY` carries its own coinbase and
    nothing else: nothing this chain has made is old enough yet for a
    second transaction to spend, `COINBASE_MATURITY` a bare constant
    Bitcoin Core does not relax for regtest either
    (`constants.COINBASE_MATURITY`'s own docstring), so there is no
    shorter, honestly-spendable chain to build instead. From
    `COINBASE_MATURITY + 1` on, every block carries a second transaction
    spending the oldest output this chain has made spendable -- `chain[0]`'s
    own coinbase, the first time, and that spend's own output every block
    after, which is never a coinbase's and so carries no maturity of its
    own left to wait out. Each value paid is exactly what its own source
    was worth: past regtest's own hundred-and-fiftieth-block halving, a
    flat amount on either side of a spend would be a coinbase printing
    money or a transaction printing money instead, one rule swapped for
    the other.
    """
    chain: list[Block] = []
    spendable: Tx | None = None
    for x in range(length):
        previous_block_hash = chain[-1].header.hash if chain else start
        height = x + 1
        transactions = [
            generate_coinbase(value=RegTest().subsidy(height), height=height)
        ]
        if spendable is None and height > COINBASE_MATURITY:
            spendable = chain[0].transactions[0]
        if spendable is not None:
            tx = generate_random_transaction(
                spendable.id, value=spendable.vout[0].value
            )
            transactions.append(tx)
            spendable = tx
        time = tip_time if tip_time is not None and x == length - 1 else None
        chain.append(build_block(previous_block_hash, transactions, x, time=time))
    return chain


def get_random_port() -> int:
    """Return a TCP port the operating system currently reports as free.

    Binding to port 0 and reading back the port the kernel picked is
    the same trick `conftest.py`'s node fixtures rely on to give every
    node its own p2p and RPC port, so parallel tests never contend for
    one -- the port is free at the moment this returns, not reserved,
    so a caller has to bind it before another process can.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        port = sock.getsockname()[1]
        assert isinstance(port, int)
        return port


def wait_until(func: Callable[[], object], timeout: float = 60) -> None:
    """Poll `func` until truthy, or raise `WaitTimeoutError` after `timeout`."""
    # The timeout bounds a failure, not a success: the loop returns as
    # soon as func() holds, so a generous limit costs a passing run
    # nothing and only delays one that was going to fail. Almost every
    # wait returns immediately; what moves under load is the tail, and
    # the longest single wait is always `test_download`'s wait for the
    # active chain to reach the downloaded length -- 7.64s on an idle
    # ten-core machine and 12.60s at twenty times its core count. The
    # wait above it in that test is `wait_until_listening`, which is a
    # loop of its own with a deadline of its own and is not bounded by
    # anything here.
    #
    # This has to be a timeout on the node rather than on the scheduler,
    # and under `-n auto` a node's background thread competes for the
    # CPU with every other worker's proof-of-work. Issue #46 measures
    # that starvation and names its cause: the node still makes
    # progress, just slowly, so what a short deadline reports is the
    # test's patience. Sixty is headroom over the worst tail seen here,
    # not a measured bound on ubuntu-latest, where the suite has failed
    # at twenty and nobody has recorded by how much it missed. What
    # bounds a genuine hang is `timeout` in pyproject.toml, which still
    # names the test it killed.
    #
    # A lambda closing over a `for` loop's own variable is safe to pass
    # here despite B023's late-binding warning: this function returns
    # (or raises) before the loop that built the lambda ever reaches its
    # next iteration and rebinds the name, so the closure is read and
    # discarded within the same iteration it was built in -- there is no
    # later call for the rebinding to have changed anything under.
    start = time.time()
    while time.time() - start < timeout:
        if func():
            return
        time.sleep(0.025)
    # where the condition is written, because a caller passes a lambda
    # and a test often has several
    code = func.__code__
    err_msg = (
        f"{code.co_filename}:{code.co_firstlineno} "
        f"did not hold within {timeout} seconds"
    )
    raise WaitTimeoutError(err_msg)


def wait_until_listening(manager: _ListensOnAPort, timeout: float = 20) -> None:
    """Wait for a manager's socket to be bound, not for its thread.

    `wait_until(manager.is_alive)` is `threading.Thread.is_alive`, which
    holds from `start()` -- before `run` has scheduled the coroutine
    that binds the port. A peer dialled in that window is refused, and
    a refusal is silent: `dial` polls for a second and returns None,
    `async_connect` drops it, and nothing dials again. The
    test then spends its whole timeout waiting for a connection that was
    lost at the start, which is what #46 sees.
    """
    start = time.time()
    while time.time() - start < timeout:
        if manager.listening.is_set():
            return
        time.sleep(0.025)
    # named here rather than left to `wait_until`, whose message is the
    # line its lambda was written on -- a lambda written in this helper
    # is the same line for every caller, and a test that waits on
    # several managers would be told nothing about which. The manager
    # and its port are what tell them apart.
    err_msg = f"{type(manager).__name__} on port {manager.port} was not "
    err_msg += f"listening within {timeout} seconds"
    raise WaitTimeoutError(err_msg)


def rpc_client(node: Node, timeout: float = 5) -> BitcoinCoreRpcClient:
    """Return a client pointed at `node`'s own RPC port.

    This node checks no credential of its own (issue #27), so `user`
    and `password` are placeholders the constructor requires one of,
    not anything the node reads.
    """
    return BitcoinCoreRpcClient(
        f"http://127.0.0.1:{node.rpc_port}",
        user="pytest",
        password="pytest",  # noqa: S106
        timeout=timeout,
    )


def post(node: Node, payload: Any, timeout: float = 5) -> str:
    """POST `payload` as JSON to `node`'s RPC port; return the raw body.

    The body comes back as text, not parsed: every caller runs
    `json.loads` on it itself, most often to reach into the JSON-RPC
    envelope's own `"error"` field rather than to get an object back
    directly.

    Built on `bitcoin_core_rpc.http_request` and not on
    `BitcoinCoreRpcClient.call_raw`: `call_raw` always sets its own
    `id` and always carries a `method` it has checked is a string, so a
    caller building a request missing either key, or naming a `method`
    that is not one, has nothing to build it with -- `call_raw`'s own
    docstring names both as deliberately out of scope. Every caller
    left on this helper is exactly one of those two shapes, or the bare
    `[]` neither `call_raw` nor `call_batch` can send at all, `call_raw`
    posting one object and `call_batch` refusing an empty `calls`.
    """
    _, body = http_request(
        f"http://127.0.0.1:{node.rpc_port}",
        data=json.dumps(payload).encode(),
        timeout=timeout,
    )
    return body.decode()


def call_within[T](func: Callable[[], T], timeout: float = 5) -> T:
    """Call `func` on a daemon thread; return its result, or its exception."""
    # For a call whose way of being wrong is never coming back. A test
    # that asserts on the answer hangs the whole suite when there is no
    # answer (btclib-org/btclib-node#98); one that calls through here
    # fails, and names where the call was written. As in wait_until
    # above, the timeout bounds the failure and not the success: the
    # join returns as soon as the call does.
    returned: list[T] = []
    raised: list[Exception] = []

    def call() -> None:
        try:
            returned.append(func())
        # deliberately blind (BLE001): func is a caller-supplied call of
        # any nature, and this thread's whole purpose is to hand its
        # exception back unchanged to the thread that called call_within
        # (below) -- narrowing this would mean some of func's own
        # failures are silently never re-raised
        except Exception as exception:  # noqa: BLE001
            raised.append(exception)

    # daemon, because a call that never returns must not keep the
    # interpreter alive on the way out
    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        code = func.__code__
        err_msg = (
            f"{code.co_filename}:{code.co_firstlineno} "
            f"did not return within {timeout} seconds"
        )
        raise WaitTimeoutError(err_msg)
    # re-raised here rather than left to threading's own hook, which
    # would print a traceback and hand the caller a KeyError
    if raised:
        raise raised[0]
    return returned[0]


def brute_force_nonce(header: BlockHeader) -> None:
    """Solve the header in place, or refuse to hand it back unsolved.

    btclib's `mine` does the searching and answers with a copy, so the
    nonce is carried back into the caller's header: every caller here
    goes on to use the object it passed. `None` is that search's honest
    answer to a bounded one, and a test that took it for a solved header
    would fail somewhere else entirely.
    """
    # a regtest header meets its own target about half the time, so the
    # bound below is on the failure and not on the search
    solved = mine(header, 100)
    if solved is None:
        # BTClibValueError and not a class of this tree's own: this is
        # the same failure header.assert_valid_pow below would itself
        # raise as a BTClibValueError, had mine found a nonce that
        # somehow did not pass it -- one class for the two, since a
        # caller catching one has to catch the other.
        err_msg = f"no nonce solves the header: {header.hash.hex()}"
        raise BTClibValueError(err_msg)
    header.nonce = solved.nonce
    header.assert_valid_pow(REGTEST_POW_LIMIT_BITS)


def local_addr(
    port: int | None, timestamp: int = 0, services: int = 0
) -> NetworkAddressV2:
    """Return a `NetworkAddressV2` naming `0.0.0.0` and `port`, to dial locally.

    A test helper building an address to hand `P2pManager.connect`, not
    a socket bind: a client socket connecting to `0.0.0.0` reaches
    whatever is listening on that port on the same host, which is what
    lets a test dial a node it started locally without hardcoding
    `127.0.0.1`.
    """
    assert port is not None
    addr = "0.0.0.0"  # noqa: S104
    return peer_address(addr, port, timestamp, services)
