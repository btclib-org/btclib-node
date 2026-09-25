# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What this node records a peer has, against a real regtest `BlockIndex`.

Each case is one of Core's own rules for the four `CNodeState` fields
(`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
"""

from typing import TYPE_CHECKING, Any, override

import pytest

from btclib_node.chains import RegTest
from btclib_node.p2p.block_availability import (
    BlockAvailability,
    get_ancestor,
    peer_has_header,
    process_block_availability,
    update_block_availability,
    update_last_common_block,
)
from tests import generate_random_header_chain

if TYPE_CHECKING:
    from collections.abc import Callable

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
    state = BlockAvailability(**{field: chain[1]})

    assert peer_has_header(index, state, chain[1])
    assert peer_has_header(index, state, chain[0])
    assert peer_has_header(index, state, GENESIS)
    assert not peer_has_header(index, state, chain[2])
    assert not peer_has_header(index, state, side[0])
    assert not peer_has_header(index, BlockAvailability(), GENESIS)


def test_nothing_moves_for_a_peer_with_no_better_chain(index: BlockIndex) -> None:
    """No best known block, or one with less work than the tip: no move."""
    active = extend(index, 2)
    activate(index, active)
    state = BlockAvailability()
    update_last_common_block(index, state, 0)
    assert state == BlockAvailability()

    behind = extend(index, 1)
    state = BlockAvailability(best_known=behind[0])
    update_last_common_block(index, state, 0)
    assert state.last_common is None


def test_nothing_moves_below_the_minimum_chain_work(index: BlockIndex) -> None:
    """A best known block short of the minimum chain work moves nothing."""
    chain = extend(index, 2)
    state = BlockAvailability(best_known=chain[1])
    update_last_common_block(index, state, index.chainwork[chain[1]] + 1)
    assert state.last_common is None
    update_last_common_block(index, state, index.chainwork[chain[1]])
    assert state.last_common == GENESIS


def test_the_last_common_block_moves_over_what_this_node_holds(
    index: BlockIndex,
) -> None:
    """From the fork point, over held blocks, to the first one missing.

    The active chain is a branch the peer's is not, so the walk starts
    at genesis, where the two part.
    """
    activate(index, extend(index, 1))
    chain = extend(index, 4)
    state = BlockAvailability(best_known=chain[3])

    update_last_common_block(index, state, 0)
    assert state.last_common == GENESIS

    index.set_downloaded(chain[0])
    index.set_downloaded(chain[1])
    index.set_downloaded(chain[3])
    update_last_common_block(index, state, 0)
    assert state.last_common == chain[1]


def test_the_last_common_block_stops_short_of_an_invalid_one(
    index: BlockIndex,
) -> None:
    """An invalid block ends the walk, held or not."""
    chain = extend(index, 3)
    for block_hash in chain:
        index.set_downloaded(block_hash)
    index.invalidate(chain[1])
    state = BlockAvailability(best_known=chain[2])
    update_last_common_block(index, state, 0)
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
    update_last_common_block(index, state, 0)
    assert state.last_common == chain[0]
    index.set_downloaded(chain[0], downloaded=False)
    update_last_common_block(index, state, 0)
    assert state.last_common == chain[0]

    other = extend(index, 3)
    update_block_availability(index, state, other[2])
    update_last_common_block(index, state, 0)
    assert state.last_common == GENESIS


def test_the_last_common_block_moves_one_window_at_a_time(index: BlockIndex) -> None:
    """Core's walk reaches `BLOCK_DOWNLOAD_WINDOW` + 1 blocks past its start."""
    chain = extend(index, 1030)
    for block_hash in chain:
        index.set_downloaded(block_hash)
    state = BlockAvailability(best_known=chain[-1])

    update_last_common_block(index, state, 0)
    assert state.last_common == chain[1024]
    update_last_common_block(index, state, 0)
    assert state.last_common == chain[-1]


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

    update_last_common_block(index, state, 0)
    assert state.last_common == side[-1]
    assert index.header_dict.reads <= 5 * length
