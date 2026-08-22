# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import secrets
from datetime import UTC, datetime

import pytest
from btclib.block import BlockHeader
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS

from btclib_node.chains import Main, RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate.block_index import BlockInfo, BlockStatus, calculate_work
from btclib_node.log import Logger
from tests.helpers import brute_force_nonce, generate_random_header_chain


def unmined_header(previous_block_hash, bits):
    # Deliberately not brute_force_nonce'd: the point of each caller is a
    # header carrying a claim its hash does not back.
    return BlockHeader(
        version=70015,
        previous_block_hash=previous_block_hash,
        merkle_root=secrets.token_bytes(32),
        time=datetime.fromtimestamp(1231006506, UTC),
        bits=bits,
        nonce=1,
        check_validity=False,
    )


def test_calculate_work():
    header = BlockHeader(
        1,
        "00" * 32,
        "00" * 32,
        datetime.fromtimestamp(1231006506, UTC),
        REGTEST_POW_LIMIT_BITS,
        1,
    )
    brute_force_nonce(header)
    # Bitcoin Core's chainwork for the regtest genesis block, whose
    # target this is: 2^256 / (target + 1), rounded down.
    assert calculate_work(header) == 2


def test_reject_header_claiming_work_it_did_not_do(tmp_path):
    # bits 0x03000001 is a target of 1: nearly the whole hash space is
    # above it, so block_work credits ~2^255 -- more than the real chain
    # has ever accumulated. Nothing mined it, and the hash does not meet
    # it, which is the only thing standing between a peer and the best
    # chain.
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    header = unmined_header(RegTest().genesis.hash, b"\x03\x00\x00\x01")
    assert calculate_work(header) > 2**254

    assert not block_index.add_headers([header])
    assert header.hash not in block_index.header_dict
    assert not block_index.block_candidates
    # genesis alone, and its chainwork untouched
    assert len(block_index.header_dict) == 1


def test_reject_header_above_the_pow_limit(tmp_path):
    # Mainnet's target on a regtest index: easier than the network's
    # easiest, so no regtest peer would ever offer it.
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    header = unmined_header(RegTest().genesis.hash, b"\x1d\x00\xff\xff")

    assert not block_index.add_headers([header])
    assert len(block_index.header_dict) == 1


def test_reject_header_with_zero_target(tmp_path):
    # A zero target is unsatisfiable, and block_work raises on it rather
    # than reporting the block as free -- so an unchecked one takes the
    # node down from the wire instead of merely being wrong.
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    header = unmined_header(RegTest().genesis.hash, b"\x01\x00\xff\xff")

    assert not block_index.add_headers([header])
    assert len(block_index.header_dict) == 1


def test_one_bad_header_refuses_the_whole_batch(tmp_path):
    # Core takes a headers message as a unit, and so does this: the
    # valid prefix is not indexed either.
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(5, RegTest().genesis.hash)
    bad = unmined_header(chain[-1].hash, b"\x03\x00\x00\x01")

    assert not block_index.add_headers([*chain, bad])
    assert len(block_index.header_dict) == 1
    # and the same batch without it is taken
    assert block_index.add_headers(chain)
    assert len(block_index.header_dict) == 5 + 1


def test_simple_init(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    block_index.add_headers(generate_random_header_chain(2000, RegTest().genesis.hash))
    chainstate.db.close()
    new_chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    new_block_index = new_chainstate.block_index
    assert block_index.header_dict == new_block_index.header_dict
    assert block_index.header_index == new_block_index.header_index
    assert block_index.active_chain == new_block_index.active_chain
    assert block_index.block_candidates == new_block_index.block_candidates


def test_init_with_fork(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(5, chain[-10].hash)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    chainstate.db.close()
    new_chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    new_block_index = new_chainstate.block_index
    assert block_index.header_dict == new_block_index.header_dict
    assert block_index.header_index == new_block_index.header_index
    assert block_index.active_chain == new_block_index.active_chain
    assert sorted(block_index.block_candidates) == sorted(
        new_block_index.block_candidates
    )


def test_add_headers_fork(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(200, chain[-10 - 1].hash)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    assert len(block_index.header_index) == 2190 + 1


def test_generate_block_candidates(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(200, chain[-10 - 1].hash)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    for x in chain:
        block_info = block_index.get_block_info(x.hash)
        block_info.status = BlockStatus.in_active_chain
        block_index.insert_block_info(block_info)
    chainstate.db.close()
    new_chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    new_block_index = new_chainstate.block_index
    assert len(new_block_index.block_candidates) == 190


def test_generate_block_candidates_2(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(200, chain[-10 - 1].hash)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    for x in fork:
        block_info = block_index.get_block_info(x.hash)
        block_info.status = BlockStatus.invalid
        block_index.insert_block_info(block_info)
    chainstate.db.close()
    new_chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    new_block_index = new_chainstate.block_index
    assert len(new_block_index.block_candidates) == 2000


def test_block_info_serialization():
    header = BlockHeader(
        1,
        "00" * 32,
        "00" * 32,
        datetime.fromtimestamp(1231006506, UTC),
        REGTEST_POW_LIMIT_BITS,
        1,
        check_validity=False,
    )
    brute_force_nonce(header)
    for status in BlockStatus:
        for downloaded in (True, False):
            for x in range(1, 64):
                block_info = BlockInfo(
                    header=header,
                    index=x**2 - 1,
                    status=status,
                    downloaded=downloaded,
                )
                assert block_info == BlockInfo.deserialize(block_info.serialize())


def test_add_old_header(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert not block_index.add_headers([chain[10]])
    assert len(block_index.header_dict) == 2000 + 1
    assert len(block_index.header_index) == 2000 + 1
    assert len(block_index.block_candidates) == 2000


def test_add_invalid_header(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    invalid_chain = generate_random_header_chain(2000, Main().genesis.hash)
    assert not block_index.add_headers(invalid_chain)
    assert len(block_index.header_dict) == 2000 + 1
    assert len(block_index.header_index) == 2000 + 1
    assert len(block_index.block_candidates) == 2000


def test_add_headers_short(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    length = 10
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    assert len(block_index.header_dict) == 2000 * length + 1
    assert len(block_index.header_index) == 2000 * length + 1
    assert len(block_index.block_candidates) == 2000 * length


def test_add_headers_medium(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    length = 40  # 400
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    assert len(block_index.header_dict) == 2000 * length + 1
    assert len(block_index.header_index) == 2000 * length + 1
    assert len(block_index.block_candidates) == 2000 * length


def test_add_headers_long(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    length = 50  # 2000
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    assert len(block_index.header_dict) == 2000 * length + 1
    assert len(block_index.header_index) == 2000 * length + 1
    assert len(block_index.block_candidates) == 2000 * length


def test_long_init(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    length = 50  # 2000
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    chainstate.db.close()
    new_chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    new_block_index = new_chainstate.block_index
    assert block_index.header_dict == new_block_index.header_dict
    assert block_index.header_index == new_block_index.header_index
    assert block_index.active_chain == new_block_index.active_chain
    assert block_index.block_candidates == new_block_index.block_candidates


def test_block_candidates(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(512, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.get_download_candidates() == [x.hash for x in chain]


def test_block_candidates_2(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(1024, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.get_download_candidates() == [x.hash for x in chain]


def test_block_candidates_3(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(200, chain[-10 - 1].hash)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    for x in chain:
        block_info = block_index.get_block_info(x.hash)
        block_info.status = BlockStatus.in_active_chain
        block_index.insert_block_info(block_info)
    chainstate.db.close()
    new_chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    new_block_index = new_chainstate.block_index
    assert new_block_index.get_download_candidates() == [x.hash for x in fork]


def test_block_locators(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(24, RegTest().genesis.hash)
    block_index.add_headers(chain)
    locators = block_index.get_block_locator_hashes()
    assert len(locators) == 14


def test_block_locators_2(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    headers = block_index.get_headers_from_locators([RegTest().genesis.hash], "00" * 32)
    assert chain == headers


def test_block_locators_3(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    headers = block_index.get_headers_from_locators(
        [RegTest().genesis.hash], chain[1000].hash
    )
    assert headers[-1] == chain[1000]
    assert headers == chain[: 1000 + 1]


def test_block_locators_4(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain[:1000])
    headers = block_index.get_headers_from_locators(
        [chain[-1].hash, RegTest().genesis.hash], "00" * 32
    )
    assert headers == chain[:1000]


def test_only_the_tip_can_leave_the_active_chain(tmp_path):
    # a reorg unwinds from the tip: removing anything else would leave
    # the chain with a hole nothing else here checks for
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    for header in chain:
        block_index.add_to_active_chain(header.hash)

    with pytest.raises(Exception):  # noqa: B017, PT011
        block_index.remove_from_active_chain(chain[0].hash)
    block_index.remove_from_active_chain(chain[-1].hash)
    assert chain[-1].hash not in block_index.active_chain
    chainstate.close()


def test_nothing_is_offered_when_there_are_no_candidates(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    assert block_index.get_first_candidate() is None
    chainstate.close()


def test_no_candidate_is_offered_when_none_outweighs_the_chain(tmp_path):
    # a header is a candidate when it arrives and stops being one once
    # the active chain has caught up to it: equal work is not more work,
    # or the node would keep offering the block it is already on
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.get_first_candidate() is not None

    for header in chain:
        block_index.add_to_active_chain(header.hash)
    assert block_index.get_first_candidate() is None
    chainstate.close()


def test_headers_from_a_locator_stop_where_asked(tmp_path):
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(10, RegTest().genesis.hash)
    block_index.add_headers(chain)

    # from the genesis, stopping at the fifth
    got = block_index.get_headers_from_locators([RegTest().genesis.hash], chain[4].hash)
    assert [h.hash for h in got] == [h.hash for h in chain[:5]]

    # a locator nothing knows is skipped, and the next one answers
    got = block_index.get_headers_from_locators(
        [b"\x11" * 32, RegTest().genesis.hash], b"\x00" * 32
    )
    assert [h.hash for h in got] == [h.hash for h in chain]

    # no locator at all is no answer
    assert block_index.get_headers_from_locators([b"\x11" * 32], b"\x00" * 32) == []
    chainstate.close()


def test_a_header_that_does_not_outweigh_the_chain_is_not_a_candidate(tmp_path):
    # a candidate is a chain worth switching to. A fork branching low
    # carries less accumulated work than the tip, so it is indexed --
    # it may yet be extended -- without being offered for connection.
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(10, RegTest().genesis.hash)
    block_index.add_headers(chain)
    for header in chain:
        block_index.add_to_active_chain(header.hash)
    block_index.block_candidates.clear()

    short_fork = generate_random_header_chain(1, RegTest().genesis.hash)
    assert block_index.add_headers(short_fork)
    assert short_fork[0].hash in block_index.header_dict
    assert not block_index.block_candidates
    chainstate.close()
