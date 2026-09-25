# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What this node records a peer has, against a real regtest `BlockIndex`.

Each case is one of Core's own rules for the `CNodeState` block fields
and the blocks a peer is asked for (`src/net_processing.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
"""

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, override

import pytest

from btclib_node.chains import RegTest
from btclib_node.constants import MIN_BLOCKS_TO_KEEP
from btclib_node.p2p.block_availability import (
    BLOCK_DOWNLOAD_WINDOW,
    BlockAvailability,
    find_next_blocks_to_download,
    get_ancestor,
    peer_has_header,
    process_block_availability,
    remove_block_request,
    update_block_availability,
)
from tests import generate_random_header_chain

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from btclib_node import Node
    from btclib_node.chainstate.block_index import BlockIndex

GENESIS = RegTest().genesis.hash


@pytest.fixture
def index(regtest_node: Callable[[], Node]) -> BlockIndex:
    """Return a fresh regtest node's block index, holding genesis alone."""
    return regtest_node().chainstate.block_index


def extend(block_index: BlockIndex, length: int, start: bytes = GENESIS) -> list[bytes]:
    """Index `length` new headers on top of `start`, and return their hashes."""
    previous_time = block_index.get_block_info(start).header.time
    chain = generate_random_header_chain(length, start, previous_time)
    block_index.add_headers(chain)
    return [header.hash for header in chain]


def activate(block_index: BlockIndex, chain: list[bytes]) -> None:
    """Put `chain`, which extends the active tip, on the active chain."""
    for block_hash in chain:
        block_index.add_to_active_chain(block_hash)
        block_index.set_downloaded(block_hash)


def test_an_ancestor_is_found_off_either_chain_and_up_a_side_branch(
    index: BlockIndex,
) -> None:
    """`get_ancestor` answers for any indexed block, `None` above it.

    The active chain here is a branch the best header chain left, and a
    third branch forks off the best header chain above genesis.
    """
    active = extend(index, 2)
    activate(index, active)
    best = extend(index, 3)
    assert index.header_index[1:] == best
    side = extend(index, 1, best[0])

    assert get_ancestor(index, best[2], 1) == best[0]
    assert get_ancestor(index, active[1], 1) == active[0]
    assert get_ancestor(index, side[0], 1) == best[0]
    assert get_ancestor(index, side[0], 0) == GENESIS
    assert get_ancestor(index, side[0], 2) == side[0]
    assert get_ancestor(index, side[0], 3) is None


def test_the_best_known_block_is_the_announced_one_with_the_most_work(
    index: BlockIndex,
) -> None:
    """A block with less work does not replace it, one with as much does."""
    chain = extend(index, 2)
    sibling = extend(index, 2, chain[0])
    state = BlockAvailability()

    update_block_availability(index, state, chain[1])
    assert state.best_known == chain[1]
    update_block_availability(index, state, chain[0])
    assert state.best_known == chain[1]
    update_block_availability(index, state, sibling[0])
    assert state.best_known == sibling[0]


def test_an_unknown_block_is_taken_up_once_it_is_indexed(index: BlockIndex) -> None:
    """An unindexed block waits in `last_unknown`, then competes on work."""
    chain = generate_random_header_chain(2, GENESIS)
    state = BlockAvailability()

    update_block_availability(index, state, chain[1].hash)
    assert state == BlockAvailability(last_unknown=chain[1].hash)
    process_block_availability(index, state)
    assert state.last_unknown == chain[1].hash

    index.add_headers(chain)
    process_block_availability(index, state)
    assert state == BlockAvailability(best_known=chain[1].hash)

    lower = extend(index, 1)
    state.last_unknown = lower[0]
    process_block_availability(index, state)
    assert state == BlockAvailability(best_known=chain[1].hash)


@pytest.mark.parametrize("field", ["best_known", "best_header_sent"])
def test_a_peer_has_every_header_up_to_what_it_announced_or_was_sent(
    index: BlockIndex, field: str
) -> None:
    """`peer_has_header` answers for the block and its ancestors, not above."""
    chain = extend(index, 3)
    side = extend(index, 1, chain[0])
    state = BlockAvailability()
    setattr(state, field, chain[1])

    assert peer_has_header(index, state, chain[1])
    assert peer_has_header(index, state, chain[0])
    assert peer_has_header(index, state, GENESIS)
    assert not peer_has_header(index, state, chain[2])
    assert not peer_has_header(index, state, side[0])
    assert not peer_has_header(index, BlockAvailability(), GENESIS)


def find(
    block_index: BlockIndex,
    state: BlockAvailability,
    count: int = 16,
    minimum_chain_work: int = 0,
    *,
    in_flight: Mapping[bytes, int] | None = None,
    peer_id: int = 1,
    limited: bool = False,
) -> tuple[list[bytes], int | None]:
    """Run `find_next_blocks_to_download` with nothing in flight by default."""
    return find_next_blocks_to_download(
        block_index,
        state,
        count,
        minimum_chain_work,
        in_flight={} if in_flight is None else in_flight,
        peer_id=peer_id,
        limited=limited,
    )


def test_nothing_is_chosen_for_a_peer_with_no_better_chain(index: BlockIndex) -> None:
    """No best known block, or one with less work than the tip: nothing."""
    active = extend(index, 2)
    activate(index, active)
    state = BlockAvailability()
    assert find(index, state) == ([], None)
    assert state == BlockAvailability()

    behind = extend(index, 1)
    state = BlockAvailability(best_known=behind[0])
    assert find(index, state) == ([], None)
    assert state.last_common is None


def test_nothing_is_chosen_for_a_count_of_zero(index: BlockIndex) -> None:
    """Asked for no block, the walk does not start."""
    chain = extend(index, 2)
    state = BlockAvailability(best_known=chain[1])
    assert find(index, state, 0) == ([], None)
    assert state.last_common is None


def test_nothing_is_chosen_below_the_minimum_chain_work(index: BlockIndex) -> None:
    """A best known block short of the minimum chain work: nothing."""
    chain = extend(index, 2)
    state = BlockAvailability(best_known=chain[1])
    assert find(index, state, 16, index.chainwork[chain[1]] + 1) == ([], None)
    assert state.last_common is None
    assert find(index, state, 16, index.chainwork[chain[1]]) == (chain, None)
    assert state.last_common == GENESIS


def test_the_blocks_missing_are_chosen_and_last_common_moves_over_the_held(
    index: BlockIndex,
) -> None:
    """From the fork point, over held blocks, to the first one missing.

    The active chain is a branch the peer's is not, so the walk starts
    at genesis, where the two part.
    """
    activate(index, extend(index, 1))
    chain = extend(index, 4)
    state = BlockAvailability(best_known=chain[3])

    assert find(index, state) == (chain, None)
    assert state.last_common == GENESIS

    index.set_downloaded(chain[0])
    index.set_downloaded(chain[1])
    index.set_downloaded(chain[3])
    assert find(index, state) == ([chain[2]], None)
    assert state.last_common == chain[1]


def test_no_more_than_count_blocks_are_chosen(index: BlockIndex) -> None:
    """The walk ends at the `count`-th block chosen."""
    chain = extend(index, 5)
    state = BlockAvailability(best_known=chain[4])
    assert find(index, state, 2) == (chain[:2], None)


def test_the_walk_ends_at_an_invalid_block(index: BlockIndex) -> None:
    """An invalid block ends the walk, held or not."""
    chain = extend(index, 3)
    index.set_downloaded(chain[0])
    index.set_downloaded(chain[1])
    index.invalidate(chain[1])
    state = BlockAvailability(best_known=chain[2])
    assert find(index, state) == ([], None)
    assert state.last_common == chain[0]


def test_the_last_common_block_is_reset_once_the_peer_leaves_its_chain(
    index: BlockIndex,
) -> None:
    """No longer an ancestor of the best known block, it is the fork point.

    Kept otherwise, even above the fork point with the active chain.
    """
    chain = extend(index, 2)
    index.set_downloaded(chain[0])
    state = BlockAvailability(best_known=chain[1])
    assert find(index, state) == ([chain[1]], None)
    assert state.last_common == chain[0]
    index.set_downloaded(chain[0], downloaded=False)
    assert find(index, state) == ([chain[1]], None)
    assert state.last_common == chain[0]

    other = extend(index, 3)
    update_block_availability(index, state, other[2])
    assert find(index, state) == (other, None)
    assert state.last_common == GENESIS


def test_the_last_common_block_is_the_fork_point_where_that_has_more_work(
    index: BlockIndex,
) -> None:
    """A `last_common` below the fork point with the active chain is raised.

    The active chain's blocks are pruned here, so it is the fork point
    and not the walk over held blocks that passes them over.
    """
    chain = extend(index, 3)
    activate(index, chain[:2])
    for block_hash in chain[:2]:
        index.set_downloaded(block_hash, downloaded=False)
    state = BlockAvailability(best_known=chain[2], last_common=chain[0])
    assert find(index, state) == ([chain[2]], None)
    assert state.last_common == chain[1]


def test_nothing_is_chosen_where_the_last_common_block_is_the_best_known(
    index: BlockIndex,
) -> None:
    """A peer whose best known block this node holds has nothing to give."""
    chain = extend(index, 2)
    activate(index, chain)
    state = BlockAvailability(best_known=chain[1])
    assert find(index, state) == ([], None)
    assert state.last_common == chain[1]


def test_the_last_common_block_moves_one_window_at_a_time(index: BlockIndex) -> None:
    """Core's walk reaches `BLOCK_DOWNLOAD_WINDOW` + 1 blocks past its start."""
    chain = extend(index, BLOCK_DOWNLOAD_WINDOW + 6)
    for block_hash in chain:
        index.set_downloaded(block_hash)
    state = BlockAvailability(best_known=chain[-1])

    assert find(index, state) == ([], None)
    assert state.last_common == chain[BLOCK_DOWNLOAD_WINDOW]
    assert find(index, state) == ([], None)
    assert state.last_common == chain[-1]


def test_a_block_in_flight_is_skipped_and_its_peer_is_waited_on(
    index: BlockIndex,
) -> None:
    """Blocks asked of anybody are passed over, the rest are chosen."""
    chain = extend(index, 4)
    state = BlockAvailability(best_known=chain[3])
    in_flight = {chain[0]: 2, chain[2]: 3}
    assert find(index, state, in_flight=in_flight) == ([chain[1], chain[3]], None)


def test_a_window_all_in_flight_names_the_first_peer_waited_on_as_staller(
    index: BlockIndex,
) -> None:
    """With nothing chosen, the next block past the window names a staller.

    Unless the peer waited on is this one, which does not stall itself;
    and not while the window holds a block still to choose.
    """
    chain = extend(index, BLOCK_DOWNLOAD_WINDOW + 2)
    state = BlockAvailability(best_known=chain[-1])
    in_flight = dict.fromkeys(chain[:BLOCK_DOWNLOAD_WINDOW], 3)
    in_flight[chain[0]] = 2
    assert find(index, state, in_flight=in_flight) == ([], 2)
    assert find(index, state, in_flight=in_flight, peer_id=2) == ([], None)

    del in_flight[chain[-3]]
    assert find(index, state, in_flight=in_flight) == ([chain[-3]], None)


def test_a_best_known_block_at_the_window_s_end_names_no_staller(
    index: BlockIndex,
) -> None:
    """A walk that runs out of the peer's chain first has no staller."""
    chain = extend(index, BLOCK_DOWNLOAD_WINDOW)
    state = BlockAvailability(best_known=chain[-1])
    in_flight = dict.fromkeys(chain, 2)
    assert find(index, state, in_flight=in_flight) == ([], None)


def test_a_limited_peer_is_not_asked_for_a_block_far_below_its_best(
    index: BlockIndex,
) -> None:
    """Blocks `MIN_BLOCKS_TO_KEEP` - 2 or more below `best_known` are left."""
    depth = MIN_BLOCKS_TO_KEEP - 2
    chain = extend(index, depth + 3)
    state = BlockAvailability(best_known=chain[-1])
    # `chain[i]` is at height i + 1, `best_known` at depth + 3: the first
    # three are depth or more below it, the fourth is depth - 1
    assert find(index, state, limited=True) == (chain[3:19], None)
    assert find(index, state) == (chain[:16], None)


class CountingDict(dict[bytes, Any]):
    """A `header_dict` that counts its own lookups by key."""

    reads = 0

    @override
    def __getitem__(self, key: bytes) -> Any:
        """Count the lookup, then answer it."""
        self.reads += 1
        return super().__getitem__(key)


def test_a_side_chain_s_walk_reads_each_header_a_bounded_number_of_times(
    index: BlockIndex,
) -> None:
    """The walk costs its length, however far `best_known` is from both chains.

    `best_known` here is on a branch that neither the active chain nor
    `header_index` holds, so every ancestor of it is read by walking
    parents: once per height, not once per height per height walked.
    """
    length = 60
    extend(index, length + 1)
    side = extend(index, length)
    assert side[-1] not in index.header_index_pos
    for block_hash in side:
        index.set_downloaded(block_hash)
    index.header_dict = CountingDict(index.header_dict)
    state = BlockAvailability(best_known=side[-1])

    assert find(index, state) == ([], None)
    assert state.last_common == side[-1]
    assert index.header_dict.reads <= 5 * length


def a_peer(
    conn_id: int,
    queue: list[bytes],
    stalling_since: float = 0.0,
    downloading_since: float = 0.0,
) -> Any:
    """Build a connection holding `queue` in flight, with timing fields."""
    return SimpleNamespace(
        id=conn_id,
        download_queue=list(queue),
        block_availability=BlockAvailability(
            stalling_since=stalling_since, downloading_since=downloading_since
        ),
    )


def test_a_block_received_leaves_every_queue_it_was_asked_in() -> None:
    """Each peer asked stops stalling; one awaiting it at the front moves on.

    The front of a queue is awaited from now, or from later where it
    already was.
    """
    block = b"\x01" * 32
    other = b"\x02" * 32
    front = a_peer(1, [block, other], stalling_since=5.0, downloading_since=3.0)
    later = a_peer(2, [other, block], stalling_since=5.0, downloading_since=3.0)
    ahead = a_peer(3, [block], downloading_since=20.0)
    unasked = a_peer(4, [other], stalling_since=5.0, downloading_since=3.0)

    remove_block_request([front, later, ahead, unasked], block, 10.0)

    assert front.download_queue == [other]
    assert front.block_availability.downloading_since == 10.0
    assert later.download_queue == [other]
    assert later.block_availability.downloading_since == 3.0
    assert ahead.block_availability.downloading_since == 20.0
    for peer in (front, later):
        assert peer.block_availability.stalling_since == 0.0
    assert unasked.download_queue == [other]
    assert unasked.block_availability == BlockAvailability(
        stalling_since=5.0, downloading_since=3.0
    )


def test_a_block_received_from_one_peer_leaves_that_peer_s_queue_alone() -> None:
    """Named, only that peer's request is dropped."""
    block = b"\x01" * 32
    first = a_peer(1, [block])
    second = a_peer(2, [block])
    remove_block_request([first, second], block, 10.0, 2)
    assert first.download_queue == [block]
    assert second.download_queue == []
