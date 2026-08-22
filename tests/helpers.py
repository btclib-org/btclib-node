# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import secrets
import socket
import threading
import time
from datetime import UTC, datetime

from btclib.block import Block, BlockHeader
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS
from btclib.exceptions import BTClibValueError
from btclib.hashes import hash256, merkle_root
from btclib.script import script
from btclib.tx.tx import Tx, TxIn, TxOut
from btclib.tx.tx_in import OutPoint

from btclib_node.p2p.address import NetworkAddress


def generate_random_header_chain(length, start):
    chain: list[BlockHeader] = []
    for x in range(length):
        if chain:
            previous_block_hash = chain[-1].hash
        else:
            previous_block_hash = start
        header = BlockHeader(
            version=70015,
            previous_block_hash=previous_block_hash,
            merkle_root=secrets.token_bytes(32),
            time=datetime.fromtimestamp(1231006505 + x + 1, UTC),
            bits=REGTEST_POW_LIMIT_BITS,
            nonce=1,
            check_validity=False,
        )
        brute_force_nonce(header)
        chain.append(header)
    return chain


def generate_random_transaction(prevouthash=None):
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
    tx = Tx(
        version=1,
        lock_time=0,
        vin=[tx_in],
        vout=[tx_out],
    )
    return tx


def generate_coinbase(value=50 * 10**8):
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


def build_block(previous_block_hash, transactions, height):
    header = BlockHeader(
        version=70015,
        previous_block_hash=previous_block_hash,
        merkle_root=merkle_root(
            [tx.serialize(True, check_validity=False) for tx in transactions],
            hash256,
        )[::-1],
        time=datetime.fromtimestamp(1231006505 + height + 1, UTC),
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)
    # Block.__init__ validates against mainnet's pow limit, which no
    # regtest block meets; brute_force_nonce has already checked this
    # header against the limit that does apply to it.
    return Block(header, transactions, check_validity=False)


def generate_random_chain(length, start):
    chain: list[Block] = []
    for x in range(length):
        previous_block_hash = chain[-1].header.hash if chain else start
        transactions = [generate_coinbase()]
        if chain:
            tx = generate_random_transaction(chain[x - 1].transactions[0].id)
            transactions.append(tx)
        chain.append(build_block(previous_block_hash, transactions, x))
    return chain


def get_random_port():
    # port 0 is the operating system being asked for one that is free,
    # which is what a caller about to bind it wants to know
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


def wait_until(func, timeout=20):
    # The timeout bounds a failure, not a success: the loop returns as
    # soon as func() holds. It has to be generous because the suite runs
    # under `-n auto`, where a node's background thread competes for the
    # CPU with every other worker's proof-of-work; a couple of seconds
    # is a timeout on the scheduler rather than on the node.
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


def wait_until_listening(manager, timeout=20):
    """Wait for a manager's socket to be bound, not for its thread.

    `wait_until(manager.is_alive)` is `threading.Thread.is_alive`, which
    holds from `start()` -- before `run` has scheduled the coroutine
    that binds the port. A peer dialled in that window is refused, and
    a refusal is silent: `NetworkAddress.connect` polls for a second and
    returns None, `async_connect` drops it, and nothing dials again. The
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


def call_within(func, timeout=5):
    # For a call whose way of being wrong is never coming back. A test
    # that asserts on the answer hangs the whole suite when there is no
    # answer (btclib-org/btclib_node#98); one that calls through here
    # fails, and names where the call was written. As in wait_until
    # above, the timeout bounds the failure and not the success: the
    # join returns as soon as the call does.
    outcome = {}

    def call():
        try:
            outcome["returned"] = func()
        except Exception as exception:
            outcome["raised"] = exception

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
    if "raised" in outcome:
        raise outcome["raised"]
    return outcome["returned"]


def brute_force_nonce(header, attempts=100):
    for _ in range(attempts):
        try:
            header.assert_valid_pow(REGTEST_POW_LIMIT_BITS)
            break
        except BTClibValueError:
            header.nonce += 1
    header.assert_valid()
    # assert_valid does not look at the target, so a header the loop
    # gave up on would otherwise be handed back unsolved and fail in
    # whatever test went on to use it
    header.assert_valid_pow(REGTEST_POW_LIMIT_BITS)


def local_addr(port: int, time: int = 0, services: int = 0):
    # A test helper building an unroutable placeholder address, not a
    # socket bind.
    addr = "0.0.0.0"  # noqa: S104
    return NetworkAddress.from_ip_and_port(addr, port, time, services)
