# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Unit tests for `btclib_node.chainstate.block_index`.

Covers `BlockInfo`'s own serialization, `calculate_work`, and
`BlockIndex`'s header validation and indexing, its active chain and
candidates, `invalidate`, persistence across a restart, and the block
locators it serves.
"""

import secrets
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from btclib.block import BlockHeader
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS
from btclib.exceptions import BTClibValueError

from btclib_node.chains import Main, RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate.block_index import BlockInfo, BlockStatus, calculate_work
from btclib_node.exceptions import ChainstateInconsistencyError
from btclib_node.log import Logger
from tests import brute_force_nonce, generate_random_header_chain

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


@pytest.fixture
def a_chainstate(tmp_path: Path) -> Iterator[Callable[[Path | None], Chainstate]]:
    """Build a factory for `Chainstate`s under `tmp_path`, closed at teardown.

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
    """Build a header claiming `bits`, without mining a nonce that meets it.

    Deliberately not brute_force_nonce'd: the point of every caller here
    is a header carrying a claim its own hash does not back.
    """
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
    """calculate_work on a mined regtest-genesis-target header returns 2.

    2 is Bitcoin Core's own chainwork for the regtest genesis block,
    whose target this header carries.
    """
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
    """A header claiming far more chainwork than it ever mined is refused.

    Bits 0x03000001 is a target of 1: nearly the whole hash space is
    above it, so block_work credits ~2^255 -- more than the real chain
    has ever accumulated. Nothing mined it, and the hash does not meet
    it, which is the only thing standing between a peer and the best
    chain.
    """
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


def test_a_header_claiming_a_target_it_was_never_mined_to_is_refused(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A header naming mainnet's own limit, unmined, is refused for its hash.

    Mainnet's easiest target is still far harder than regtest's own,
    so this is well inside the range `assert_valid_pow` allows a regtest
    header to claim -- it fails the other half of that same check
    instead, `hash > target`, because an unmined nonce practically never
    satisfies a target this hard.
    """
    # mainnet's easiest target is far harder than regtest's own limit,
    # so this trips the hash-versus-target half of assert_valid_pow
    # rather than the half bounding a claimed target by the chain's own
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    header = unmined_header(RegTest().genesis.hash, b"\x1d\x00\xff\xff")

    with pytest.raises(BTClibValueError):
        block_index.add_headers([header])
    assert len(block_index.header_dict) == 1


def test_reject_header_with_zero_target(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A header naming a zero target is refused rather than treated as free.

    A zero target is unsatisfiable, and block_work raises on it rather
    than reporting the block as free -- so an unchecked one takes the
    node down from the wire instead of merely being wrong.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    header = unmined_header(RegTest().genesis.hash, b"\x01\x00\xff\xff")

    with pytest.raises(BTClibValueError):
        block_index.add_headers([header])
    assert len(block_index.header_dict) == 1


def test_one_bad_header_refuses_the_whole_batch(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """One bad header keeps the whole batch, valid prefix included, out.

    Core takes a headers message as a unit, and so does this: the valid
    prefix ahead of the bad header is not indexed either, though the
    same headers sent again on their own are.
    """
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
    """A header solved at the wrong target is refused by the contextual check.

    assert_valid_pow and _assert_valid_in_context share one except clause
    in add_headers; this header trips only the second, mining a target
    harder than regtest's own limit but not the one the chain requires,
    so a test that only ever builds a header failing the first check
    could not tell the two apart.
    """
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
    """A header solved and correctly targeted, but too early, is refused.

    Same wiring question as above, tripped by the other branch of
    _assert_valid_in_context: this header carries the required target
    and a solved nonce, and only its timestamp -- the genesis' own, no
    later than the median of itself alone -- is wrong.
    """
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
    """add_headers answers with the batch's own tip, new or already known.

    Sending the same batch again, once every header in it is already
    indexed, answers with the same tip rather than `None`.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(5, RegTest().genesis.hash)
    assert block_index.add_headers(chain) == chain[-1].hash
    # already known in full, and still answers with its own tip
    assert block_index.add_headers(chain) == chain[-1].hash


def test_add_headers_resumes_from_a_fork_s_own_tip_not_the_best_chain(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A fork below the active chain's own tip does not move header_index.

    header_index only moves for a header extending it or beating its
    chainwork, so add_headers' own return value -- the fork's own tip,
    not header_index's -- is what a caller has to resume a locator from,
    or it would ask for this same batch again and never reach further
    into the fork. btclib-org/btclib-node#122
    """
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
    """A freshly built index matches one reloaded from the same store.

    2000 headers are indexed, the store is closed, and a second
    `BlockIndex` opened on the same path agrees on every field
    `init_from_db` rebuilds, chainwork included -- recomputed rather
    than persisted (btclib-org/btclib-node#201).
    """
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
    """A reloaded index agrees on a forked chain too, candidates included.

    A 2000-header active chain and a five-header fork off its tenth
    block from the tip both survive a close and reopen, `header_dict`,
    `header_index`, `active_chain` and `block_candidates` agreeing
    between the two.
    """
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
    """header_index grows by the whole batch, chain and fork alike.

    A 2000-header chain and a 200-header fork off its eleventh block
    from the tip are both indexed, and header_index ends up holding
    every one of the 2190 distinct headers plus the genesis.
    """
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
    """Marking a chain in_active_chain leaves only the fork as a candidate.

    Once every header of the 2000-header chain is set
    `in_active_chain`, a reload's `generate_block_candidates` rebuilds
    `block_candidates` from what is left: the 200-header fork, minus
    the ten blocks its own branch point sits before the tip.
    """
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
    """Marking the fork invalid leaves the whole active chain as candidates.

    With the 200-header fork marked `invalid` instead, a reload's
    `generate_block_candidates` counts every header of the 2000-header
    chain: `valid_header` status alone is what qualifies a header, and
    the active chain never had its own status set to anything else here.
    """
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
    """`invalidate` reaches a whole lineage, not only its block_candidates.

    A header enters header_dict, at valid_header, whenever it merely
    arrives -- block_candidates only holds the ones whose own
    cumulative chainwork individually cleared the active chain's at
    the moment they arrived, so a real descendant that never did is
    only reached by walking `children`, not the deque:
    btclib-org/btclib-node#125.
    """
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
    """A header extending an invalidated parent is indexed invalid on arrival.

    Invalidating a chain's first header, then sending a header that
    extends its second, still succeeds -- add_headers takes the batch --
    but the new header is filed `invalid` from the start and never
    enters `block_candidates`.
    """
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
    """Invalidating header_index's own tip moves header_index off that chain.

    header_index is the best known header chain for locator/announce
    purposes, tracked independently of block_candidates and weighed
    purely by chainwork -- so invalidating the chain it happened to
    end on leaves it no longer pointing at a chain this node has
    refused. btclib-org/btclib-node#218
    """
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
    """Invalidating header_index's chain moves it onto the best surviving one.

    The fallback is not always the active chain: a seven-header fork
    with more work than the five-header active chain is what
    header_index recomputes onto once the fork itself is invalidated --
    the same way Core's InvalidateBlock (src/validation.cpp) recomputes
    m_best_header. btclib-org/btclib-node#218
    """
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
    """More headers on an already-invalidated chain never move header_index.

    add_headers' own header_index update has to read the same invalid
    flag block_candidates already does, or a peer sending more of a
    chain this node has already refused keeps growing what this index
    reports as its best known header chain. btclib-org/btclib-node#218
    """
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
    """A restart's rebuilt header_index still excludes an invalidated chain.

    generate_header_index rebuilds from the persisted BlockStatus on
    every start-up, so a chain invalidated before the restart is
    excluded again rather than reappearing because nothing in the
    fresh index remembers the earlier invalidate call.
    btclib-org/btclib-node#218
    """
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
    """get_first_candidate skips a branch downloaded only at its own tip.

    get_first_candidate used to ask only whether a candidate's own tip
    had arrived: a branch with a downloaded tip but an undownloaded
    block behind it passed that check and update_chain then stalled on
    the hole every pass, leaving nothing else able to connect --
    btclib-org/btclib-node#121. The complete one-header branch here is
    what it returns instead.
    """
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
    """A `BlockInfo` round-trips through serialize/deserialize for every status.

    Every `BlockStatus`, both `downloaded` values, and 63 distinct
    `index` values are each built into a `BlockInfo` and checked against
    the record `deserialize` parses back from its own `serialize`.
    """
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
    """Re-sending an already-indexed header answers with its own hash.

    add_headers on a single header already inside a 2000-header chain
    returns that header's own hash -- a real point a caller could
    resume from -- and leaves `header_dict`, `header_index` and
    `block_candidates` at the sizes the original batch already set.
    """
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
    """A batch connecting to nothing known answers `None` rather than raising.

    Every header here is validly mined; none of them fails on its own
    terms, so this is the "connects to nothing" branch and not the "a
    header failed a check" one, and it leaves the existing index
    untouched.
    """
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
    """A batch carrying a child before its own new parent is refused whole.

    A peer is not required to send a headers message in strict
    parent-before-child order, and a compliant one reordering
    internally produces exactly this: btclib-org/btclib-node#214. Both
    headers stay out of `header_dict`.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    parent, child = generate_random_header_chain(2, RegTest().genesis.hash)

    with pytest.raises(BTClibValueError):
        block_index.add_headers([child, parent])
    assert child.hash not in block_index.header_dict
    assert parent.hash not in block_index.header_dict
    assert len(block_index.header_dict) == 1


def test_add_headers_short(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """Ten batches of 2000 headers each add up the same as one big one.

    Sent 2000 at a time, a 20000-header chain still leaves
    `header_dict`, `header_index` and `block_candidates` at the sizes
    a single batch of the same chain would.
    """
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
    """The same batching check as above, at 40 batches of 2000 headers."""
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
    """The same batching check as above, at 50 batches of 2000 headers."""
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
    """A 100000-header index reloads from disk agreeing on every field.

    The same close-and-reopen check as test_simple_init, at 50 batches
    of 2000 headers each, so a start-up rebuild is also exercised at a
    size closer to what a real sync produces.
    """
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
    """A freshly indexed 512-header chain is entirely its own candidates.

    Nothing on it is downloaded or on the active chain yet, so
    get_download_candidates returns every one of its headers, in order.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(512, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.get_download_candidates() == [x.hash for x in chain]


def test_block_candidates_2(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """The same check as above, at exactly MAX_DOWNLOAD_WINDOW headers."""
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(1024, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.get_download_candidates() == [x.hash for x in chain]


def test_block_candidates_3(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """Once the chain is active, only its fork's headers are candidates.

    With the 2000-header chain marked `in_active_chain` and reloaded,
    get_download_candidates on the reopened index answers with the
    200-header fork alone, in order.
    """
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
    """A 24-header chain's locator carries 14 entries.

    Ten dense entries near the tip, then a step that doubles each time,
    reaching back to the genesis in four more -- the shape
    get_block_locator_hashes' own docstring names.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(24, RegTest().genesis.hash)
    block_index.add_headers(chain)
    locators = block_index.get_block_locator_hashes()
    assert len(locators) == 14


def test_block_locators_2(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """A locator naming only the genesis returns the whole chain after it."""
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    headers = block_index.get_headers_from_locators(
        [RegTest().genesis.hash], b"\x00" * 32
    )
    assert chain == headers


def test_block_locators_3(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """get_headers_from_locators stops at the requested `stop` hash.

    Asked for what follows the genesis and to stop at the chain's own
    1000th header, the answer ends there rather than running to the tip.
    """
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
    """The first known locator resumes the answer, whatever order it is in.

    Of the two locators offered, the chain's own unindexed tip and the
    genesis, only the genesis is known, and the answer resumes from it
    regardless of its position in the list.
    """
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
    """remove_from_active_chain refuses anything but the chain's own tip.

    A reorg unwinds from the tip: removing anything else would leave
    the chain with a hole nothing else here checks for, so it raises
    `ChainstateInconsistencyError` instead.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    for header in chain:
        block_index.add_to_active_chain(header.hash)

    with pytest.raises(
        ChainstateInconsistencyError, match="not the active chain's tip"
    ):
        block_index.remove_from_active_chain(chain[0].hash)
    block_index.remove_from_active_chain(chain[-1].hash)
    assert chain[-1].hash not in block_index.active_chain
    chainstate.close()


def test_nothing_is_offered_when_there_are_no_candidates(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """get_first_candidate answers `None` on a genesis-only index."""
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    assert block_index.get_first_candidate() is None
    chainstate.close()


def test_no_candidate_is_offered_when_none_outweighs_the_chain(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """get_first_candidate stops offering a chain once caught up to.

    A header is a candidate when it arrives and stops being one once
    the active chain has caught up to it: equal work is not more work,
    or the node would keep offering the block it is already on.
    """
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
    """get_headers_from_locators resumes from the first known locator.

    Stopping at the fifth header of ten answers only those five; an
    unknown locator ahead of the genesis in the list is skipped rather
    than failing the call; and no known locator at all answers nothing.
    """
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
    """A fork with less work than the active chain is indexed but not offered.

    A candidate is a chain worth switching to. A fork branching off the
    genesis carries less accumulated work than a ten-header active
    chain, so it is indexed -- it may yet be extended -- without being
    added to `block_candidates`.
    """
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
    """A branch the active chain has connected stops appearing as a candidate.

    The deque is not emptied when a branch connects, so what keeps a
    connected block from being fetched all over again is the work it
    is weighed against, not its removal from `block_candidates`.
    """
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
    """get_download_candidates skips a block already marked downloaded.

    The walk back from a candidate goes through blocks this node may
    already have: they are what it is walking towards, and asking a
    peer for them again is the download running twice.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    block_index.set_downloaded(chain[1].hash)

    assert block_index.get_download_candidates() == [chain[0].hash, chain[2].hash]
    chainstate.close()


def test_a_stop_hash_at_or_below_the_locator_is_answered_not_raised(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A known `stop` at or below the resolved locator answers, not raises.

    `stop` sitting at or below the locator's own height is not in the
    slice `get_headers_from_locators` takes *after* it, and looking for
    `stop` in `header_index` as a whole -- rather than in that slice --
    used to raise `ValueError` here: btclib-org/btclib-node#434. A
    `stop` behind the locator can never be reached going forward, so it
    does not truncate the answer at all; where the locator is already
    the chain's own tip, that answer is empty -- Core's own "nothing to
    send" for the same request.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(5, RegTest().genesis.hash)
    block_index.add_headers(chain)

    # the genesis is the measured case in the issue: known, and below
    # every locator this chain can offer -- and the locator here is
    # already the tip, so there is nothing to send either way
    assert (
        block_index.get_headers_from_locators([chain[-1].hash], RegTest().genesis.hash)
        == []
    )
    # stop at the locator itself, not only strictly below it
    assert block_index.get_headers_from_locators([chain[-1].hash], chain[-1].hash) == []
    # stop below a locator that is not the tip: unreachable going
    # forward, so it does not raise and does not truncate what follows
    assert (
        block_index.get_headers_from_locators([chain[2].hash], chain[0].hash)
        == chain[3:]
    )
    chainstate.close()


def test_header_index_pos_agrees_with_header_index(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """header_index_pos always maps header_index's own hashes to their position.

    Checked after add_headers builds a fork that beats the active
    chain's own tip (moving header_index by more than one append) and
    again after invalidate rebuilds header_index from scratch.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index

    def assert_consistent() -> None:
        assert block_index.header_index_pos == {
            h: i for i, h in enumerate(block_index.header_index)
        }

    assert_consistent()
    active = generate_random_header_chain(5, RegTest().genesis.hash)
    block_index.add_headers(active)
    for header in active:
        block_index.add_to_active_chain(header.hash)
    assert_consistent()

    fork = generate_random_header_chain(7, RegTest().genesis.hash)
    block_index.add_headers(fork)
    assert block_index.header_index[-1] == fork[-1].hash  # the fork won
    assert_consistent()

    block_index.invalidate(fork[0].hash)
    assert_consistent()
    chainstate.close()


def test_the_locators_of_a_node_that_holds_only_the_genesis_block(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """get_block_locator_hashes on a genesis-only index names the genesis once.

    The list ends at the oldest header this node has, and here the walk
    back has already named it, it being the newest as well. What keeps
    it off the end a second time -- and the peer from being asked the
    same question twice -- is the guard on that last append.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    assert block_index.get_block_locator_hashes() == [RegTest().genesis.hash]
    chainstate.close()


def test_finalize_with_no_batch_opens_its_own_and_writes_pending(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """`finalize()`, called with no `wb`, still writes and clears `pending`.

    `main._finalize_fork` always hands `finalize` the batch
    `UtxoIndex`/`FilterIndex` share (`Chainstate.flush`), so this is the
    other path: a caller with no batch of its own, mirroring
    `FilterIndex.finalize`'s own bare form.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    (header,) = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers([header])
    block_index.stage_status(header.hash, BlockStatus.in_active_chain)
    assert header.hash in block_index.pending

    block_index.finalize()

    assert block_index.pending == {}
    data = block_index.db.get(b"blkinfo-" + header.hash)
    assert data is not None
    stored = BlockInfo.deserialize(data, check_validity=False)
    assert stored.status == BlockStatus.in_active_chain
    chainstate.close()


def test_invalidate_after_stage_status_is_not_undone_by_a_later_finalize(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A write-through invalidate on a staged hash must survive the next flush.

    `stage_status` stages a hash in `pending` without writing it. If
    `invalidate` (through `set_status`) then targeted that same hash --
    reachable in `main.update_chain` through a chain-tip flip-flop, or
    through an I/O fault in `block_db.add_rev_block` or
    `filter_index.add_connected_block` that has nothing to do with the
    block's own content -- writing straight through used to leave
    `pending` holding a stale entry that the next `finalize` wrote back
    over the invalidation, undoing it silently. btclib-org/btclib-node#586.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    (header,) = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers([header])
    block_index.stage_status(header.hash, BlockStatus.in_active_chain)
    assert header.hash in block_index.pending

    block_index.invalidate(header.hash)

    block_index.finalize()

    assert block_index.pending == {}
    data = block_index.db.get(b"blkinfo-" + header.hash)
    assert data is not None
    stored = BlockInfo.deserialize(data, check_validity=False)
    assert stored.status == BlockStatus.invalid
    chainstate.close()


def test_set_downloaded_after_stage_status_is_not_undone_by_a_later_finalize(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """`set_downloaded` on a staged hash survives the next flush.

    Reachable from `main.prune_up_to_height`: `_finalize_fork`'s own
    to_add loop stages every hash a fork connects through `stage_status`,
    before that fork's own `finalize` ever runs, and `to_add` is not
    bounded by `MIN_BLOCKS_TO_KEEP` anywhere -- a fork longer than the
    retained depth stages a hash `prune_up_to_height`, run once at the
    end of that same `update_chain` call, then clears the flag on.
    Writing straight through would leave `pending` holding a stale
    `downloaded=True` entry that the next `finalize` writes back over
    the clear, undoing it silently -- the same shape
    btclib-org/btclib-node#586 fixed for `set_status`.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    (header,) = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers([header])
    # downloaded=True before staging: _finalize_fork's own to_add loop
    # only ever stages a hash _ready_fork already required downloaded,
    # so the pending entry stage_status below captures carries that
    # True forward -- the stale value a write-through set_downloaded
    # would otherwise lose to the next finalize.
    block_index.set_downloaded(header.hash)
    block_index.stage_status(header.hash, BlockStatus.in_active_chain)
    assert header.hash in block_index.pending
    assert block_index.pending[header.hash].downloaded is True

    block_index.set_downloaded(header.hash, downloaded=False)

    block_index.finalize()

    assert block_index.pending == {}
    data = block_index.db.get(b"blkinfo-" + header.hash)
    assert data is not None
    stored = BlockInfo.deserialize(data, check_validity=False)
    assert stored.downloaded is False
    chainstate.close()
