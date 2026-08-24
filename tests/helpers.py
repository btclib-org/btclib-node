# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import json
import secrets
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol

import requests
from btclib.block import Block, BlockHeader, merkle_root_and_mutated_from_transactions
from btclib.block.mining import mine
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS
from btclib.exceptions import BTClibValueError
from btclib.p2p.addrv2 import NetworkAddressV2
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.p2p.address import peer_address


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


def generate_random_transaction(prevouthash: bytes | None = None) -> Tx:
    prevouthash = prevouthash or secrets.token_bytes(32)
    tx_in = TxIn(
        prev_out=OutPoint(prevouthash, 0),
        script_sig=script.serialize([secrets.token_bytes(32)]),
        sequence=0xFFFFFFFF,
    )
    tx_out = TxOut(
        value=50 * 10**8,
        script_pub_key=script.serialize([secrets.token_bytes(32)]),
    )
    return Tx(
        version=1,
        lock_time=0,
        vin=[tx_in],
        vout=[tx_out],
    )


def generate_coinbase(value: int = 50 * 10**8) -> Tx:
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(),
                script_sig=script.serialize([secrets.token_bytes(32)]),
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
    previous_block_hash: bytes, transactions: list[Tx], height: int
) -> Block:
    header = BlockHeader(
        version=70015,
        previous_block_hash=previous_block_hash,
        merkle_root=merkle_root_and_mutated_from_transactions(transactions)[0],
        time=GENESIS_TIME + timedelta(seconds=height + 1),
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)
    # Block.__init__ validates against mainnet's pow limit, which no
    # regtest block meets; brute_force_nonce has already checked this
    # header against the limit that does apply to it.
    return Block(header, transactions, check_validity=False)


def generate_random_chain(length: int, start: bytes) -> list[Block]:
    chain: list[Block] = []
    for x in range(length):
        previous_block_hash = chain[-1].header.hash if chain else start
        transactions = [generate_coinbase()]
        if chain:
            tx = generate_random_transaction(chain[x - 1].transactions[0].id)
            transactions.append(tx)
        chain.append(build_block(previous_block_hash, transactions, x))
    return chain


def get_random_port() -> int:
    # port 0 is the operating system being asked for one that is free,
    # which is what a caller about to bind it wants to know
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        port = sock.getsockname()[1]
        assert isinstance(port, int)
        return port


def wait_until(func: Callable[[], object], timeout: float = 60) -> None:
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
    raise Exception(err_msg)


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
    raise Exception(err_msg)


def post(node: Node, payload: Any, timeout: float = 5) -> str:
    return requests.post(
        url=f"http://127.0.0.1:{node.rpc_port}",
        data=json.dumps(payload).encode(),
        timeout=timeout,
    ).text


def call_within[T](func: Callable[[], T], timeout: float = 5) -> T:
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
        except Exception as exception:
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
        raise Exception(err_msg)
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
        raise BTClibValueError(f"no nonce solves the header: {header.hash.hex()}")
    header.nonce = solved.nonce
    header.assert_valid_pow(REGTEST_POW_LIMIT_BITS)


def local_addr(
    port: int | None, timestamp: int = 0, services: int = 0
) -> NetworkAddressV2:
    # A test helper building an unroutable placeholder address, not a
    # socket bind.
    assert port is not None
    addr = "0.0.0.0"  # noqa: S104
    return peer_address(addr, port, timestamp, services)
