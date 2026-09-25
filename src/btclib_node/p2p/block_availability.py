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

A block is a hash here where Core holds a `CBlockIndex*`. `BlockIndex`
keeps no ancestor pointer but the parent's hash, where Core's
`GetAncestor` has a skip list. `_Ancestry` walks a block back once, to
where it joins `header_index` or `active_chain` -- both lists indexed
by height -- and answers every height from there in constant time, so
a walk over a peer's chain costs its length plus the distance of the
peer's best block from both of this node's chains, once per call.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from btclib_node.chainstate.block_index import BlockStatus
from btclib_node.constants import MIN_BLOCKS_TO_KEEP

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

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


class _Ancestry:
    """One block's ancestors, read by height: Core's `GetAncestor`.

    Built by walking the block back to the first ancestor `header_index`
    or `active_chain` holds; every height at or below that one is read
    off that list, every height above it off the walk.
    """

    def __init__(self, block_index: BlockIndex, block_hash: bytes) -> None:
        """Walk `block_hash` back to where it joins one of the two lists."""
        header_dict = block_index.header_dict
        active_chain = block_index.active_chain
        side: list[bytes] = []
        current = block_hash
        height = header_dict[current].index
        while True:
            if current in block_index.header_index_pos:
                self._base = block_index.header_index
                break
            if height < len(active_chain) and active_chain[height] == current:
                self._base = active_chain
                break
            side.append(current)
            current = header_dict[current].header.previous_block_hash
            height -= 1
        self._join = height
        self._side = side
        self.height = height + len(side)

    def at(self, height: int) -> bytes | None:
        """Return the ancestor at `height`, `None` above the block itself."""
        if height > self.height:
            return None
        if height <= self._join:
            return self._base[height]
        return self._side[self.height - height]


def get_ancestor(
    block_index: BlockIndex, block_hash: bytes, height: int
) -> bytes | None:
    """Return the ancestor of `block_hash` at `height`: Core's `GetAncestor`.

    `None` for a height above the block's own, as Core answers `nullptr`.
    """
    return _Ancestry(block_index, block_hash).at(height)


def _fork_point(block_index: BlockIndex, ancestry: _Ancestry) -> bytes:
    """Return the highest block of `ancestry` on the active chain.

    Core's `LastCommonAncestor` against the active tip. The heights at
    which the two agree are a prefix, genesis always among them, so the
    last of them is found by bisection.
    """
    active_chain = block_index.active_chain
    agree, disagree = 0, min(ancestry.height, len(active_chain) - 1) + 1
    while disagree - agree > 1:
        middle = (agree + disagree) // 2
        if ancestry.at(middle) == active_chain[middle]:
            agree = middle
        else:
            disagree = middle
    return active_chain[agree]


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
) -> _Ancestry | None:
    """Reset `last_common` and return `best_known`'s ancestry, if any is due.

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
    ancestry = _Ancestry(block_index, best_known)
    fork_point = _fork_point(block_index, ancestry)
    last_common = state.last_common
    if (
        last_common is None
        or chainwork[fork_point] > chainwork[last_common]
        or ancestry.at(block_index.header_dict[last_common].index) != last_common
    ):
        state.last_common = fork_point
    return None if state.last_common == best_known else ancestry


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
    ancestry = (
        None if count == 0 else _walk_start(block_index, state, minimum_chain_work)
    )
    if ancestry is None:
        return [], None
    header_dict = block_index.header_dict
    assert state.last_common is not None  # noqa: S101 -- set by _walk_start
    window_end = header_dict[state.last_common].index + BLOCK_DOWNLOAD_WINDOW
    blocks: list[bytes] = []
    waiting_for: int | None = None
    all_held = True
    for height in range(
        header_dict[state.last_common].index + 1,
        min(ancestry.height, window_end + 1) + 1,
    ):
        block_hash = ancestry.at(height)
        assert block_hash is not None  # noqa: S101 -- at or below its own height
        block_info = header_dict[block_hash]
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
        if limited and ancestry.height - height >= MIN_BLOCKS_TO_KEEP - 2:
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
