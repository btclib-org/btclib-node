# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The helpers the rest of the suite is built on.

A helper that is wrong makes the tests using it agree with each other
and with nothing else, and a helper whose failure path is untested
fails as a bare traceback in the middle of somebody else's test.
"""

import socket
import threading
import time
from itertools import pairwise
from types import SimpleNamespace

import pytest
from btclib.block import (
    BlockHeader,
    bip34_commitment,
    merkle_root_and_mutated_from_transactions,
)
from btclib.exceptions import BTClibValueError
from btclib.p2p.addrv2 import BIP155Network
from btclib.tx.limits import COINBASE_MATURITY

from btclib_node.chains import RegTest
from tests import (
    WaitTimeoutError,
    brute_force_nonce,
    build_block,
    call_within,
    generate_coinbase,
    generate_random_chain,
    generate_random_header_chain,
    generate_random_transaction,
    get_random_port,
    local_addr,
    wait_until,
    wait_until_listening,
)


def test_the_port_offered_is_one_that_can_be_bound() -> None:
    """`get_random_port` returns a bindable port, different each call."""
    port = get_random_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", port))
    assert 1024 <= port <= 65535
    # and a second caller is not handed the first one: two nodes in one
    # test would otherwise fight over it
    assert get_random_port() != port


def test_a_condition_that_holds_is_not_waited_for() -> None:
    """`wait_until` returns immediately once `func` is already truthy."""
    timeout = 10
    start = time.time()
    wait_until(lambda: True, timeout=timeout)
    assert time.time() - start < timeout


def test_a_condition_that_never_holds_is_given_up_on() -> None:
    """`wait_until` polls repeatedly and raises, naming its own caller."""
    calls: list[bool] = []
    timeout = 0.2
    start = time.time()
    with pytest.raises(
        WaitTimeoutError, match=f"helpers_test.py:.* within {timeout} seconds"
    ):
        wait_until(lambda: calls.append(True), timeout=timeout)
    # asked repeatedly, and for as long as it said it would
    assert len(calls) > 1
    assert time.time() - start >= timeout


def test_a_manager_that_is_listening_is_not_waited_for() -> None:
    """`wait_until_listening` returns immediately once `listening` is set."""
    listening = SimpleNamespace(listening=threading.Event(), port=18444)
    listening.listening.set()
    start = time.time()
    wait_until_listening(listening, timeout=10)
    assert time.time() - start < 10


def test_a_manager_that_never_binds_is_given_up_on() -> None:
    """`wait_until_listening` raises, naming the manager and its port."""
    # the whole point of the helper: a listener that never comes up is a
    # bounded failure and not a test that waits on it forever -- and the
    # failure says which manager, because a test that waits on several
    # is told nothing by a line number inside the helper
    never = SimpleNamespace(listening=threading.Event(), port=18444)
    with pytest.raises(WaitTimeoutError, match=r"18444.* within 0\.2 seconds"):
        wait_until_listening(never, timeout=0.2)


def test_a_bounded_call_hands_back_what_it_returned() -> None:
    """`call_within` returns the wrapped call's own result."""
    assert call_within(lambda: "answer") == "answer"


def test_a_bounded_call_that_raises_raises_where_it_was_called() -> None:
    """`call_within` re-raises the call's exception, on the caller's thread."""

    # and not on the thread it ran on, where the caller would see a
    # printed traceback and an error about the answer being missing
    def refuses() -> None:
        err_msg = "no"
        raise ValueError(err_msg)

    with pytest.raises(ValueError, match="no"):
        call_within(refuses)


def test_a_bounded_call_that_does_not_return_is_given_up_on() -> None:
    """`call_within` gives up naming its caller when a call never returns."""
    release = threading.Event()
    timeout = 0.2

    def never_returns() -> None:
        release.wait()

    start = time.time()
    try:
        with pytest.raises(
            WaitTimeoutError, match=f"helpers_test.py:.* within {timeout} seconds"
        ):
            call_within(never_returns, timeout=timeout)
        assert time.time() - start >= timeout
    finally:
        release.set()


def test_a_header_that_is_not_well_formed_is_refused() -> None:
    """`brute_force_nonce` propagates btclib's own validation error."""
    header = BlockHeader(
        version=0,
        previous_block_hash=RegTest().genesis.hash,
        merkle_root=b"\x11" * 32,
        time=RegTest().genesis.time,
        bits=RegTest().genesis.bits,
        nonce=1,
        check_validity=False,
    )
    with pytest.raises(BTClibValueError, match="invalid version"):
        brute_force_nonce(header)


def test_a_header_that_cannot_be_solved_is_not_passed_off_as_valid() -> None:
    """`brute_force_nonce` raises rather than returning an unsolved header."""
    # a target of one: nearly the whole hash space is above it, so the
    # bounded search comes back with nothing rather than with a nonce,
    # and what must not happen is the header being handed back unsolved
    header = BlockHeader(
        version=70015,
        previous_block_hash=RegTest().genesis.hash,
        merkle_root=b"\x11" * 32,
        time=RegTest().genesis.time,
        bits=(0x03000001).to_bytes(4, "big"),
        nonce=1,
        check_validity=False,
    )
    with pytest.raises(BTClibValueError, match="no nonce"):
        brute_force_nonce(header)


def test_a_generated_header_chain_links_and_holds_up() -> None:
    """`generate_random_header_chain` links and solves each header in turn."""
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    assert chain[0].previous_block_hash == RegTest().genesis.hash
    for parent, child in pairwise(chain):
        assert child.previous_block_hash == parent.hash
    for header in chain:
        header.assert_valid_pow(RegTest().pow_limit_bits)


def test_a_generated_block_chain_links_and_its_roots_match() -> None:
    """`generate_random_chain` links its blocks and each root matches."""
    chain = generate_random_chain(3, RegTest().genesis.hash)
    for block in chain:
        # btclib's, not a second implementation of it written here: the
        # one this used to carry hashed the transactions *with* their
        # witnesses, which is not what a merkle root is over and passed
        # only because nothing built here has a witness
        root, _ = merkle_root_and_mutated_from_transactions(block.transactions)
        assert block.header.merkle_root == root
    assert chain[0].header.previous_block_hash == RegTest().genesis.hash
    for previous, block in pairwise(chain):
        assert block.header.previous_block_hash == previous.header.hash


def test_a_generated_chain_carries_no_coinbase_spend_before_maturity() -> None:
    """Every block up to `COINBASE_MATURITY` is its own coinbase alone.

    Nothing a chain this short has made is old enough yet to spend --
    `COINBASE_MATURITY` is not relaxed for regtest either, so there is
    no shorter chain that could honestly carry one.
    """
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    for block in chain:
        assert len(block.transactions) == 1


def test_a_generated_chain_spends_what_it_made_once_that_is_mature() -> None:
    """Past `COINBASE_MATURITY`, each block spends the chain's own history.

    `chain[0]`'s own coinbase is spent the first time it is old enough
    to be, and every block after that spends the one before it's own
    spend -- never a coinbase again, so nothing past that first spend
    has any maturity left to wait out.
    """
    chain = generate_random_chain(COINBASE_MATURITY + 2, RegTest().genesis.hash)
    first_spend = chain[COINBASE_MATURITY].transactions[1]
    assert first_spend.vin[0].prev_out.tx_id == chain[0].transactions[0].id
    for previous, block in pairwise(chain[COINBASE_MATURITY:]):
        spend = block.transactions[1]
        assert spend.vin[0].prev_out.tx_id == previous.transactions[1].id


def test_a_coinbase_spends_nothing() -> None:
    """`generate_coinbase` builds a null-outpoint input, paying the subsidy."""
    coinbase = generate_coinbase()
    assert coinbase.vin[0].prev_out.tx_id == b"\x00" * 32
    assert coinbase.vout[0].value == 50 * 10**8
    assert generate_coinbase(value=1).vout[0].value == 1


def test_a_coinbase_commits_to_its_height_only_where_asked() -> None:
    """`generate_coinbase`'s `height` prefixes the BIP34 commitment, or not."""
    uncommitted = generate_coinbase().vin[0].script_sig
    assert not uncommitted.startswith(bip34_commitment(52))
    committed = generate_coinbase(height=52).vin[0].script_sig
    assert committed.startswith(bip34_commitment(52))


def test_a_transaction_spends_what_it_is_told_to() -> None:
    """`generate_random_transaction` spends the given txid, or a random one."""
    funding = generate_coinbase()
    spend = generate_random_transaction(funding.id)
    assert spend.vin[0].prev_out.tx_id == funding.id
    assert generate_random_transaction().vin[0].prev_out.tx_id != funding.id
    assert generate_random_transaction(value=1).vout[0].value == 1


def test_a_built_block_carries_the_transactions_it_was_given() -> None:
    """`build_block` returns a solved block holding the given transactions."""
    transactions = [generate_coinbase()]
    block = build_block(RegTest().genesis.hash, transactions, 0)
    assert list(block.transactions) == transactions
    block.header.assert_valid_pow(RegTest().pow_limit_bits)


def test_a_local_address_names_loopback() -> None:
    """`local_addr` builds a `127.0.0.1` `NetworkAddressV2` for a port."""
    address = local_addr(18444)
    assert address.address == b"\x7f\x00\x00\x01"
    assert address.network_id == BIP155Network.IPV4
    assert address.port == 18444
    assert local_addr(18444, timestamp=7, services=9).timestamp == 7
