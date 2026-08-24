# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import secrets
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from btclib.block import BlockHeader
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS
from btclib.exceptions import BTClibValueError

from btclib_node.chains import Main, RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate.block_index import BlockInfo, BlockStatus, calculate_work
from btclib_node.log import Logger
from tests.helpers import brute_force_nonce, generate_random_header_chain


@pytest.fixture
def a_chainstate(tmp_path: Path) -> Iterator[Callable[[Path | None], Chainstate]]:
    """A factory for `Chainstate`s at `tmp_path`, closed at teardown.

    A test that checks a chainstate survives being closed and reopened
    closes the first itself and builds a second, at the same path;
    each is closed once more here regardless of what the test already
    did to it, `Chainstate.close` being safe to call twice.
    """
    with ExitStack() as stack:

        def make(path: Path | None = None) -> Chainstate:
            chainstate = Chainstate(
                tmp_path if path is None else path, RegTest(), Logger(debug=True)
            )
            stack.callback(chainstate.close)
            return chainstate

        yield make


def unmined_header(previous_block_hash: bytes, bits: bytes) -> BlockHeader:
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


def test_calculate_work() -> None:
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


def test_reject_header_claiming_work_it_did_not_do(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # bits 0x03000001 is a target of 1: nearly the whole hash space is
    # above it, so block_work credits ~2^255 -- more than the real chain
    # has ever accumulated. Nothing mined it, and the hash does not meet
    # it, which is the only thing standing between a peer and the best
    # chain.
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    header = unmined_header(RegTest().genesis.hash, b"\x03\x00\x00\x01")
    assert calculate_work(header) > 2**254

    with pytest.raises(BTClibValueError):
        block_index.add_headers([header])
    assert header.hash not in block_index.header_dict
    assert not block_index.block_candidates
    # genesis alone, and its chainwork untouched
    assert len(block_index.header_dict) == 1


def test_reject_header_above_the_pow_limit(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # Mainnet's target on a regtest index: easier than the network's
    # easiest, so no regtest peer would ever offer it.
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    header = unmined_header(RegTest().genesis.hash, b"\x1d\x00\xff\xff")

    with pytest.raises(BTClibValueError):
        block_index.add_headers([header])
    assert len(block_index.header_dict) == 1


def test_reject_header_with_zero_target(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # A zero target is unsatisfiable, and block_work raises on it rather
    # than reporting the block as free -- so an unchecked one takes the
    # node down from the wire instead of merely being wrong.
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    header = unmined_header(RegTest().genesis.hash, b"\x01\x00\xff\xff")

    with pytest.raises(BTClibValueError):
        block_index.add_headers([header])
    assert len(block_index.header_dict) == 1


def test_one_bad_header_refuses_the_whole_batch(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # Core takes a headers message as a unit, and so does this: the
    # valid prefix is not indexed either.
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(5, RegTest().genesis.hash)
    bad = unmined_header(chain[-1].hash, b"\x03\x00\x00\x01")

    with pytest.raises(BTClibValueError):
        block_index.add_headers([*chain, bad])
    assert len(block_index.header_dict) == 1
    # and the same batch without it is taken
    assert block_index.add_headers(chain)
    assert len(block_index.header_dict) == 5 + 1


def test_a_header_with_valid_pow_but_the_wrong_required_target_is_refused(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # assert_valid_pow and assert_valid_in_context share one except
    # clause in add_headers; this header trips only the second, so a
    # test that only ever builds a header failing the first cannot
    # tell the two apart
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    genesis = RegTest().genesis
    # one target harder than regtest's own limit, so assert_valid_pow
    # (which only bounds the header's claimed target by the network's)
    # takes it, and mine still solves it in the same handful of tries a
    # regtest header always does
    header = BlockHeader(
        version=70015,
        previous_block_hash=genesis.hash,
        merkle_root=secrets.token_bytes(32),
        time=genesis.time + timedelta(seconds=1),
        bits=b"\x20\x7f\xff\xfe",
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)
    assert header.bits != REGTEST_POW_LIMIT_BITS

    with pytest.raises(BTClibValueError):
        block_index.add_headers([header])
    assert header.hash not in block_index.header_dict
    assert len(block_index.header_dict) == 1


def test_a_header_with_valid_pow_but_no_later_than_the_median_is_refused(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # same wiring question as above, tripped by the other branch of
    # assert_valid_in_context: this header carries the required target
    # and a solved nonce, and only its timestamp is wrong
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    genesis = RegTest().genesis
    header = BlockHeader(
        version=70015,
        previous_block_hash=genesis.hash,
        merkle_root=secrets.token_bytes(32),
        time=genesis.time,  # not later than the median of itself alone
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)

    with pytest.raises(BTClibValueError):
        block_index.add_headers([header])
    assert header.hash not in block_index.header_dict
    assert len(block_index.header_dict) == 1


def test_add_headers_returns_the_batch_s_own_tip(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(5, RegTest().genesis.hash)
    assert block_index.add_headers(chain) == chain[-1].hash
    # already known in full, and still answers with its own tip
    assert block_index.add_headers(chain) == chain[-1].hash


def test_add_headers_resumes_from_a_fork_s_own_tip_not_the_best_chain(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # header_index only moves for a header extending it or beating its
    # chainwork, so a fork arriving below the active chain's tip is
    # indexed without moving it: a caller resuming from header_index's
    # own locator would ask for this same batch again and never reach
    # further into the fork. btclib-org/btclib-node#122
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    active = generate_random_header_chain(5, RegTest().genesis.hash)
    block_index.add_headers(active)
    for header in active:
        block_index.add_to_active_chain(header.hash)

    fork = generate_random_header_chain(3, RegTest().genesis.hash)
    assert block_index.add_headers(fork) == fork[-1].hash
    assert fork[-1].hash not in block_index.header_index


def test_simple_init(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    block_index.add_headers(generate_random_header_chain(2000, RegTest().genesis.hash))
    chainstate.db.close()
    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert block_index.header_dict == new_block_index.header_dict
    assert block_index.header_index == new_block_index.header_index
    assert block_index.active_chain == new_block_index.active_chain
    assert block_index.block_candidates == new_block_index.block_candidates
    # not persisted, recomputed by calculate_chainwork on each start:
    # btclib-org/btclib-node#201
    assert block_index.chainwork == new_block_index.chainwork


def test_init_with_fork(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(5, chain[-10].hash, chain[-10].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    chainstate.db.close()
    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert block_index.header_dict == new_block_index.header_dict
    assert block_index.header_index == new_block_index.header_index
    assert block_index.active_chain == new_block_index.active_chain
    assert sorted(block_index.block_candidates) == sorted(
        new_block_index.block_candidates
    )


def test_add_headers_fork(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(200, chain[-10 - 1].hash, chain[-10 - 1].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    assert len(block_index.header_index) == 2190 + 1


def test_generate_block_candidates(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(200, chain[-10 - 1].hash, chain[-10 - 1].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    for x in chain:
        block_index.set_status(x.hash, BlockStatus.in_active_chain)
    chainstate.db.close()
    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert len(new_block_index.block_candidates) == 190


def test_generate_block_candidates_2(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(200, chain[-10 - 1].hash, chain[-10 - 1].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    for x in fork:
        block_index.set_status(x.hash, BlockStatus.invalid)
    chainstate.db.close()
    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert len(new_block_index.block_candidates) == 2000


def test_invalidate_marks_every_header_indexed_on_it_not_only_candidates(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # a header enters header_dict, at valid_header, whenever it merely
    # arrives -- block_candidates only holds the ones whose own
    # cumulative chainwork individually cleared the active chain's at
    # the moment they arrived, so a real descendant that never did is
    # only reached by walking `children`, not the deque:
    # btclib-org/btclib-node#125
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    active = generate_random_header_chain(5, RegTest().genesis.hash)
    block_index.add_headers(active)
    for header in active:
        block_index.add_to_active_chain(header.hash)

    # genesis-rooted, and never its own candidate: five headers' worth
    # of chainwork does not exceed what active's own five already hold
    victim = generate_random_header_chain(5, RegTest().genesis.hash)
    block_index.add_headers(victim)
    victim_hashes = {header.hash for header in victim}
    assert not victim_hashes & {h for h, _ in block_index.block_candidates}

    sibling = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers(sibling)

    block_index.invalidate(victim[0].hash)

    for header in victim:
        assert block_index.get_block_info(header.hash).status == BlockStatus.invalid
    assert (
        block_index.get_block_info(sibling[0].hash).status == BlockStatus.valid_header
    )
    assert not victim_hashes & {h for h, _ in block_index.block_candidates}
    chainstate.close()


def test_a_header_built_on_an_invalid_parent_is_invalid_and_not_a_candidate(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    block_index.add_headers(chain)
    block_index.invalidate(chain[0].hash)

    extension = generate_random_header_chain(1, chain[1].hash, chain[1].time)
    assert block_index.add_headers(extension)

    info = block_index.get_block_info(extension[0].hash)
    assert info.status == BlockStatus.invalid
    assert extension[0].hash not in [h for h, _ in block_index.block_candidates]
    chainstate.close()


def test_invalidate_moves_header_index_off_the_chain_it_was_pointing_at(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # header_index is the best known header chain for locator/announce
    # purposes, tracked independently of block_candidates and weighed
    # purely by chainwork -- so invalidating the chain it happened to
    # end on left it pointing at a chain this node has already refused.
    # btclib-org/btclib-node#218
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.header_index[-1] == chain[-1].hash

    block_index.invalidate(chain[0].hash)

    assert block_index.header_index[-1] != chain[-1].hash
    assert chain[-1].hash not in block_index.get_block_locator_hashes()
    chainstate.close()


def test_invalidate_recomputes_header_index_onto_the_next_best_surviving_chain(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # the fallback is not always the active chain: a rescan has to pick
    # whichever surviving chain now has the most work, the same way
    # Core's InvalidateBlock (src/validation.cpp) recomputes
    # m_best_header. btclib-org/btclib-node#218
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    active = generate_random_header_chain(5, RegTest().genesis.hash)
    block_index.add_headers(active)
    for header in active:
        block_index.add_to_active_chain(header.hash)

    fork = generate_random_header_chain(7, RegTest().genesis.hash)
    assert block_index.add_headers(fork) == fork[-1].hash
    assert block_index.header_index[-1] == fork[-1].hash

    block_index.invalidate(fork[0].hash)

    assert block_index.header_index == block_index.active_chain
    chainstate.close()


def test_a_batch_extending_an_invalidated_chain_does_not_move_header_index(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # add_headers's own header_index update has to read the same
    # invalid flag block_candidates already does, or a peer sending
    # more of a chain this node has already refused keeps growing what
    # this index reports as its best known header chain.
    # btclib-org/btclib-node#218
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    block_index.add_headers(chain)
    block_index.invalidate(chain[0].hash)
    header_index_before = list(block_index.header_index)

    extension = generate_random_header_chain(10, chain[1].hash, chain[1].time)
    assert block_index.add_headers(extension) == extension[-1].hash
    assert block_index.header_index == header_index_before
    chainstate.close()


def test_invalidated_headers_stay_out_of_header_index_after_a_restart(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # generate_header_index rebuilds from the persisted BlockStatus on
    # every start-up, and used to do so without reading it at all.
    # btclib-org/btclib-node#218
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    block_index.invalidate(chain[0].hash)
    header_index_before = list(block_index.header_index)
    chainstate.db.close()

    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert new_block_index.header_index == header_index_before
    assert chain[-1].hash not in new_block_index.header_index


def test_first_candidate_skips_a_hole_behind_a_downloaded_tip(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # get_first_candidate used to ask only whether a candidate's own tip
    # had arrived: a branch missing a block behind it passed that check
    # and update_chain then stalled on the hole every pass, leaving
    # nothing else able to connect: btclib-org/btclib-node#121
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    hole = generate_random_header_chain(2, RegTest().genesis.hash)
    block_index.add_headers(hole)
    block_index.set_downloaded(hole[-1].hash)  # the tip alone

    complete = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers(complete)
    block_index.set_downloaded(complete[0].hash)

    candidate = block_index.get_first_candidate()
    assert candidate is not None
    assert candidate.header.hash == complete[0].hash
    chainstate.close()


def test_block_info_serialization() -> None:
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


def test_add_old_header(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    # already known, and still answers with its own hash: nothing new,
    # but a real point a caller could resume from
    assert block_index.add_headers([chain[10]]) == chain[10].hash
    assert len(block_index.header_dict) == 2000 + 1
    assert len(block_index.header_index) == 2000 + 1
    assert len(block_index.block_candidates) == 2000


def test_add_headers_connecting_to_nothing_known_is_not_a_refusal(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # every header here is validly mined; none of them fails on its own
    # terms, so this is the "connects to nothing" branch and not the
    # "a header failed a check" one -- it does not raise
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    disconnected_chain = generate_random_header_chain(2000, Main().genesis.hash)
    assert block_index.add_headers(disconnected_chain) is None
    assert len(block_index.header_dict) == 2000 + 1
    assert len(block_index.header_index) == 2000 + 1
    assert len(block_index.block_candidates) == 2000


def test_a_header_before_its_own_new_parent_in_the_batch_refuses_the_batch(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # a peer is not required to send a headers message in strict
    # parent-before-child order, and a compliant one reordering
    # internally produces exactly this: btclib-org/btclib-node#214
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    parent, child = generate_random_header_chain(2, RegTest().genesis.hash)

    with pytest.raises(BTClibValueError):
        block_index.add_headers([child, parent])
    assert child.hash not in block_index.header_dict
    assert parent.hash not in block_index.header_dict
    assert len(block_index.header_dict) == 1


def test_add_headers_short(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    length = 10
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    assert len(block_index.header_dict) == 2000 * length + 1
    assert len(block_index.header_index) == 2000 * length + 1
    assert len(block_index.block_candidates) == 2000 * length


def test_add_headers_medium(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    length = 40  # 400
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    assert len(block_index.header_dict) == 2000 * length + 1
    assert len(block_index.header_index) == 2000 * length + 1
    assert len(block_index.block_candidates) == 2000 * length


def test_add_headers_long(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    length = 50  # 2000
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    assert len(block_index.header_dict) == 2000 * length + 1
    assert len(block_index.header_index) == 2000 * length + 1
    assert len(block_index.block_candidates) == 2000 * length


def test_long_init(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    length = 50  # 2000
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    chainstate.db.close()
    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert block_index.header_dict == new_block_index.header_dict
    assert block_index.header_index == new_block_index.header_index
    assert block_index.active_chain == new_block_index.active_chain
    assert block_index.block_candidates == new_block_index.block_candidates
    # not persisted, recomputed by calculate_chainwork on each start:
    # btclib-org/btclib-node#201
    assert block_index.chainwork == new_block_index.chainwork


def test_block_candidates(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(512, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.get_download_candidates() == [x.hash for x in chain]


def test_block_candidates_2(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(1024, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.get_download_candidates() == [x.hash for x in chain]


def test_block_candidates_3(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(200, chain[-10 - 1].hash, chain[-10 - 1].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    for x in chain:
        block_index.set_status(x.hash, BlockStatus.in_active_chain)
    chainstate.db.close()
    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert new_block_index.get_download_candidates() == [x.hash for x in fork]


def test_block_locators(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(24, RegTest().genesis.hash)
    block_index.add_headers(chain)
    locators = block_index.get_block_locator_hashes()
    assert len(locators) == 14


def test_block_locators_2(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    headers = block_index.get_headers_from_locators(
        [RegTest().genesis.hash], b"\x00" * 32
    )
    assert chain == headers


def test_block_locators_3(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    headers = block_index.get_headers_from_locators(
        [RegTest().genesis.hash], chain[1000].hash
    )
    assert headers[-1] == chain[1000]
    assert headers == chain[: 1000 + 1]


def test_block_locators_4(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain[:1000])
    headers = block_index.get_headers_from_locators(
        [chain[-1].hash, RegTest().genesis.hash], b"\x00" * 32
    )
    assert headers == chain[:1000]


def test_only_the_tip_can_leave_the_active_chain(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # a reorg unwinds from the tip: removing anything else would leave
    # the chain with a hole nothing else here checks for
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    for header in chain:
        block_index.add_to_active_chain(header.hash)

    with pytest.raises(Exception, match="not the active chain's tip"):
        block_index.remove_from_active_chain(chain[0].hash)
    block_index.remove_from_active_chain(chain[-1].hash)
    assert chain[-1].hash not in block_index.active_chain
    chainstate.close()


def test_nothing_is_offered_when_there_are_no_candidates(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    assert block_index.get_first_candidate() is None
    chainstate.close()


def test_no_candidate_is_offered_when_none_outweighs_the_chain(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # a header is a candidate when it arrives and stops being one once
    # the active chain has caught up to it: equal work is not more work,
    # or the node would keep offering the block it is already on
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.get_first_candidate() is not None

    for header in chain:
        block_index.add_to_active_chain(header.hash)
    assert block_index.get_first_candidate() is None
    chainstate.close()


def test_headers_from_a_locator_stop_where_asked(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    chainstate = a_chainstate(None)
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


def test_a_header_that_does_not_outweigh_the_chain_is_not_a_candidate(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # a candidate is a chain worth switching to. A fork branching low
    # carries less accumulated work than the tip, so it is indexed --
    # it may yet be extended -- without being offered for connection.
    chainstate = a_chainstate(None)
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


def test_a_candidate_the_chain_has_caught_up_with_is_not_downloaded_again(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # the deque is not emptied when a branch connects, so what keeps a
    # connected block from being fetched all over again is the work it
    # is weighed against
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.get_download_candidates() == [header.hash for header in chain]

    for header in chain:
        block_index.add_to_active_chain(header.hash)
    assert block_index.get_download_candidates() == []
    chainstate.close()


def test_a_block_already_held_is_left_out_of_what_is_asked_for(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # the walk back from a candidate goes through blocks this node may
    # already have: they are what it is walking towards, and asking a
    # peer for them again is the download running twice
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    block_index.set_downloaded(chain[1].hash)

    assert block_index.get_download_candidates() == [chain[0].hash, chain[2].hash]
    chainstate.close()


def test_the_locators_of_a_node_that_holds_only_the_genesis_block(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    # the list ends at the oldest header this node has, and here the
    # walk back has already named it, it being the newest as well. What
    # keeps it off the end a second time -- and the peer from being
    # asked the same question twice -- is the guard on that last append
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    assert block_index.get_block_locator_hashes() == [RegTest().genesis.hash]
    chainstate.close()
