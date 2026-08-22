# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The helpers the rest of the suite is built on.

A helper that is wrong makes the tests using it agree with each other
and with nothing else, and a helper whose failure path is untested
fails as a bare traceback in the middle of somebody else's test.
"""

import socket
import time

import pytest
from btclib.block import BlockHeader
from btclib.exceptions import BTClibValueError
from btclib.hashes import hash256, merkle_root

from btclib_node.chains import RegTest
from tests.helpers import (
    brute_force_nonce,
    build_block,
    generate_coinbase,
    generate_random_chain,
    generate_random_header_chain,
    generate_random_transaction,
    get_random_port,
    local_addr,
    wait_until,
)


def test_the_port_offered_is_one_that_can_be_bound():
    port = get_random_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", port))
    assert 1024 <= port <= 65535
    # and a second caller is not handed the first one: two nodes in one
    # test would otherwise fight over it
    assert get_random_port() != port


def test_a_condition_that_holds_is_not_waited_for():
    timeout = 10
    start = time.time()
    wait_until(lambda: True, timeout=timeout)
    assert time.time() - start < timeout


def test_a_condition_that_never_holds_is_given_up_on():
    calls = []
    timeout = 0.2
    start = time.time()
    with pytest.raises(Exception, match=f"helpers.py:.* within {timeout} seconds"):
        wait_until(lambda: calls.append(True), timeout=timeout)
    # asked repeatedly, and for as long as it said it would
    assert len(calls) > 1
    assert time.time() - start >= timeout


def test_a_header_that_is_not_well_formed_is_refused():
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


def test_a_header_that_cannot_be_solved_is_not_passed_off_as_valid():
    header = BlockHeader(
        version=70015,
        previous_block_hash=RegTest().genesis.hash,
        merkle_root=b"\x11" * 32,
        time=RegTest().genesis.time,
        bits=RegTest().genesis.bits,
        nonce=1,
        check_validity=False,
    )
    with pytest.raises(BTClibValueError):
        brute_force_nonce(header, attempts=0)


def test_a_generated_header_chain_links_and_holds_up():
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    assert chain[0].previous_block_hash == RegTest().genesis.hash
    for parent, child in zip(chain, chain[1:]):
        assert child.previous_block_hash == parent.hash
    for header in chain:
        header.assert_valid_pow(RegTest().pow_limit_bits)


def test_a_generated_block_chain_spends_what_the_block_before_it_made():
    chain = generate_random_chain(3, RegTest().genesis.hash)
    for block in chain:
        assert (
            block.header.merkle_root
            == merkle_root(
                [tx.serialize(True, check_validity=False) for tx in block.transactions],
                hash256,
            )[::-1]
        )
    assert chain[0].header.previous_block_hash == RegTest().genesis.hash
    for previous, block in zip(chain, chain[1:]):
        assert block.header.previous_block_hash == previous.header.hash
        spend = block.transactions[1]
        assert spend.vin[0].prev_out.tx_id == previous.transactions[0].id


def test_a_coinbase_spends_nothing():
    coinbase = generate_coinbase()
    assert coinbase.vin[0].prev_out.tx_id == b"\x00" * 32
    assert coinbase.vout[0].value == 50 * 10**8
    assert generate_coinbase(value=1).vout[0].value == 1


def test_a_transaction_spends_what_it_is_told_to():
    funding = generate_coinbase()
    spend = generate_random_transaction(funding.id)
    assert spend.vin[0].prev_out.tx_id == funding.id
    assert generate_random_transaction().vin[0].prev_out.tx_id != funding.id


def test_a_built_block_carries_the_transactions_it_was_given():
    transactions = [generate_coinbase()]
    block = build_block(RegTest().genesis.hash, transactions, 0)
    assert list(block.transactions) == transactions
    block.header.assert_valid_pow(RegTest().pow_limit_bits)


def test_a_placeholder_address_is_unroutable():
    address = local_addr(18444)
    assert repr(address) == "0.0.0.0:18444"
    assert local_addr(18444, time=7, services=9).time == 7
