# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What this node knows one peer has of the block chain, and asks it for.

`BlockAvailability` is the part of Core's `CNodeState` that is about
blocks: `pindexBestKnownBlock`, `hashLastUnknownBlock`,
`pindexLastCommonBlock`, `pindexBestHeaderSent`, `m_stalling_since` and
`m_downloading_since` (`src/net_processing.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag); `Connection.download_queue`
is its `vBlocksInFlight`. The functions below are the ones of Core's
that read and write those fields, under the same names:
`callbacks.headers` and `callbacks.inv` call `update_block_availability`,
`callbacks.getheaders` and `main`'s block announcement set
`best_header_sent`, the announcement asks `peer_has_header`,
`DownloadManager.block_download` runs `find_next_blocks_to_download`,
and `callbacks.block` runs `remove_block_request`. `getpeerinfo`'s
`synced_headers` and `synced_blocks` are the heights of `best_known`
and `last_common`.

A block is a hash here where Core holds a `CBlockIndex*`, and an
ancestor is read through `BlockIndex.get_ancestor` and
`BlockIndex.last_common_ancestor`, Core's `GetAncestor` and
`LastCommonAncestor` over the same skip pointers.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from btclib_node.chainstate.block_index import BlockStatus
from btclib_node.constants import MIN_BLOCKS_TO_KEEP

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from btclib_node.chainstate.block_index import BlockIndex
    from btclib_node.p2p.connection import Connection

__all__ = [
    "BLOCK_DOWNLOAD_WINDOW",
    "BlockAvailability",
    "find_next_blocks_to_download",
    "get_ancestor",
    "peer_has_header",
    "process_block_availability",
    "remove_block_request",
    "update_block_availability",
]

# Core's `BLOCK_DOWNLOAD_WINDOW` (`src/net_processing.cpp`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag): how far past a peer's
# `last_common` block anything is asked of it.
BLOCK_DOWNLOAD_WINDOW = 1024

# How many successors Core's `FindNextBlocks` (`src/net_processing.cpp`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) reads at a time at
# least, "because CBlockIndex::GetAncestor may be as expensive as
# iterating over ~100 CBlockIndex* entries anyway"
_FETCH_CHUNK = 128


@dataclass
class BlockAvailability:
    """The blocks one peer is known to have, and how its downloads stand.

    `best_known` is the most-work block the peer announced, and
    `last_unknown` the last one it announced that was not yet indexed.
    `last_common` is the last block both this node and the peer hold in
    full. `best_header_sent` is the best header this node sent the peer.
    Each is `None` until first set, as Core's pointers start null.
    `stalling_since` is when the peer started holding up the download
    window, and `downloading_since` when the block at the front of its
    queue became the one awaited: wall-clock times, each 0 where Core's
    is zero.
    """

    best_known: bytes | None = None
    last_unknown: bytes | None = None
    last_common: bytes | None = None
    best_header_sent: bytes | None = None
    stalling_since: float = 0.0
    downloading_since: float = 0.0


def get_ancestor(
    block_index: BlockIndex, block_hash: bytes, height: int
) -> bytes | None:
    """Return the ancestor of `block_hash` at `height`: Core's `GetAncestor`.

    `None` for a height above the block's own, as Core answers `nullptr`.
    """
    return block_index.get_ancestor(block_hash, height)


def process_block_availability(
    block_index: BlockIndex, state: BlockAvailability
) -> None:
    """Take up `last_unknown` once indexed: Core's `ProcessBlockAvailability`.

    It becomes `best_known` unless that has more work. Core also asks
    that the block's chain work be positive, which every header indexed
    here has, genesis included.
    """
    last_unknown = state.last_unknown
    if last_unknown is None or last_unknown not in block_index.header_dict:
        return
    chainwork = block_index.chainwork
    best_known = state.best_known
    if best_known is None or chainwork[last_unknown] >= chainwork[best_known]:
        state.best_known = last_unknown
    state.last_unknown = None


def update_block_availability(
    block_index: BlockIndex, state: BlockAvailability, block_hash: bytes
) -> None:
    """Record that the peer has `block_hash`: Core's `UpdateBlockAvailability`.

    An indexed block becomes `best_known` unless that has more work; one
    not indexed yet is kept as `last_unknown` until it is.
    """
    process_block_availability(block_index, state)
    if block_hash not in block_index.header_dict:
        state.last_unknown = block_hash
        return
    chainwork = block_index.chainwork
    best_known = state.best_known
    if best_known is None or chainwork[block_hash] >= chainwork[best_known]:
        state.best_known = block_hash


def peer_has_header(
    block_index: BlockIndex, state: BlockAvailability, block_hash: bytes
) -> bool:
    """Whether the peer has `block_hash`'s header: Core's `PeerHasHeader`.

    It does where the header is an ancestor of, or is, the best block it
    announced or the best header it was sent.
    """
    height = block_index.header_dict[block_hash].index
    return any(
        known is not None and get_ancestor(block_index, known, height) == block_hash
        for known in (state.best_known, state.best_header_sent)
    )


def _walk_start(
    block_index: BlockIndex, state: BlockAvailability, minimum_chain_work: int
) -> bytes | None:
    """Reset `last_common` and return `best_known`, if a walk is due.

    The half of Core's `FindNextBlocksToDownload` before `FindNextBlocks`.
    """
    process_block_availability(block_index, state)
    best_known = state.best_known
    if best_known is None:
        return None
    chainwork = block_index.chainwork
    if (
        chainwork[best_known] < chainwork[block_index.active_chain[-1]]
        or chainwork[best_known] < minimum_chain_work
    ):
        return None
    fork_point = block_index.last_common_ancestor(
        best_known, block_index.active_chain[-1]
    )
    last_common = state.last_common
    if (
        last_common is None
        or chainwork[fork_point] > chainwork[last_common]
        or block_index.get_ancestor(
            best_known, block_index.header_dict[last_common].index
        )
        != last_common
    ):
        state.last_common = fork_point
    return None if state.last_common == best_known else best_known


def _successors(
    block_index: BlockIndex,
    best_known: bytes,
    start: int,
    end: int,
    wanted: Callable[[], int],
) -> Iterator[bytes]:
    """Yield `best_known`'s ancestors above height `start` up to `end`.

    In chunks, as Core's `FindNextBlocks` reads them: the ancestor at the
    top of each chunk through `get_ancestor`, the rest by parent hash,
    each chunk as long as `wanted()` or `_FETCH_CHUNK`, whichever is
    larger, `wanted` being asked again before each chunk. Where
    `best_known` is on `header_index`, its ancestors are that list's
    entries, read off it without a walk.
    """
    if best_known in block_index.header_index_pos:
        yield from block_index.header_index[start + 1 : end + 1]
        return
    header_dict = block_index.header_dict
    height = start
    while height < end:
        size = min(end - height, max(wanted(), _FETCH_CHUNK))
        top = block_index.get_ancestor(best_known, height + size)
        assert top is not None  # noqa: S101 -- at or below its own height
        chunk = [top]
        for _ in range(size - 1):
            chunk.append(header_dict[chunk[-1]].header.previous_block_hash)
        height += size
        yield from reversed(chunk)


def find_next_blocks_to_download(  # noqa: PLR0913
    block_index: BlockIndex,
    state: BlockAvailability,
    count: int,
    minimum_chain_work: int,
    *,
    in_flight: Mapping[bytes, int],
    peer_id: int,
    limited: bool,
) -> tuple[list[bytes], int | None]:
    """Choose up to `count` blocks to ask this peer for: Core's own walk.

    `FindNextBlocksToDownload` and `FindNextBlocks`. Nothing is chosen,
    and `last_common` stays, where `count` is 0, where `best_known` is
    unset or has less work than the active tip or than
    `minimum_chain_work`.
    `last_common` is then reset to the fork point between `best_known`
    and the active tip where it is unset, has less work than that fork
    point, or is no longer an ancestor of `best_known`; nothing is chosen
    where it is `best_known` itself.

    The walk goes up `best_known`'s chain from `last_common`,
    `BLOCK_DOWNLOAD_WINDOW` + 1 blocks at most, and ends at an invalid
    block. A block this node holds is passed over, and becomes
    `last_common` while every block before it is held too. A block in
    `in_flight`, which maps each block asked for to a peer it is asked
    of, is passed over, and the first one's peer is who this peer waits
    on. A block past the window's end ends the walk, and where nothing
    was chosen the peer waited on is returned as the staller, unless it
    is this one. A `limited` peer is not asked for a block
    `MIN_BLOCKS_TO_KEEP` - 2 or more below its `best_known`: Core's
    `NODE_NETWORK_LIMITED_MIN_BLOCKS`, with "two blocks buffer for
    possible races".

    Core also passes over a block of the active chain, which a walk
    starting at or above the fork point never meets, and stops at a
    block a peer without witnesses could not serve, where
    `callbacks.version` refuses such a peer.
    """
    best_known = (
        None if count == 0 else _walk_start(block_index, state, minimum_chain_work)
    )
    if best_known is None:
        return [], None
    header_dict = block_index.header_dict
    assert state.last_common is not None  # noqa: S101 -- set by _walk_start
    last_common_height = header_dict[state.last_common].index
    best_height = header_dict[best_known].index
    window_end = last_common_height + BLOCK_DOWNLOAD_WINDOW
    blocks: list[bytes] = []
    waiting_for: int | None = None
    all_held = True
    for block_hash in _successors(
        block_index,
        best_known,
        last_common_height,
        min(best_height, window_end + 1),
        lambda: count - len(blocks),
    ):
        block_info = header_dict[block_hash]
        height = block_info.index
        if block_info.status == BlockStatus.invalid:
            break
        if block_info.downloaded:
            if all_held:
                state.last_common = block_hash
            continue
        all_held = False
        if block_hash in in_flight:
            # the first block in flight is the one this peer waits for
            waiting_for = in_flight[block_hash] if waiting_for is None else waiting_for
            continue
        if height > window_end:
            stalled = not blocks and waiting_for != peer_id
            return blocks, waiting_for if stalled else None
        if limited and best_height - height >= MIN_BLOCKS_TO_KEEP - 2:
            continue
        blocks.append(block_hash)
        if len(blocks) == count:
            break
    return blocks, None


def remove_block_request(
    connections: Iterable[Connection],
    block_hash: bytes,
    now: float,
    from_peer: int | None = None,
) -> None:
    """Drop `block_hash` from the queues it is asked in: `RemoveBlockRequest`.

    Every connection's, or `from_peer`'s alone. A peer the block was
    asked of stops stalling, and where it was the block awaited at the
    front of the queue, the one after it is awaited from `now`.
    """
    for conn in connections:
        if from_peer is not None and conn.id != from_peer:
            continue
        queue = conn.download_queue
        if block_hash not in queue:
            continue
        state = conn.block_availability
        if queue[0] == block_hash:
            state.downloading_since = max(state.downloading_since, now)
        queue.remove(block_hash)
        state.stalling_since = 0.0
