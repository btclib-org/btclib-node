# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What this node knows one peer has of the block chain.

`BlockAvailability` is the part of Core's `CNodeState` that is about
blocks: `pindexBestKnownBlock`, `hashLastUnknownBlock`,
`pindexLastCommonBlock` and `pindexBestHeaderSent`
(`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
tag). The functions below are the ones of Core's that read and write
those fields, under the same names: `callbacks.headers` and
`callbacks.inv` call `update_block_availability`, `callbacks.getheaders`
and `main`'s block announcement set `best_header_sent`, the announcement
asks `peer_has_header`, and `DownloadManager` runs
`update_last_common_block`. `getpeerinfo`'s `synced_headers` and
`synced_blocks` are the heights of `best_known` and `last_common`.

A block is a hash here where Core holds a `CBlockIndex*`. `BlockIndex`
keeps no ancestor pointer but the parent's hash, so `get_ancestor`
walks back until it reaches `header_index` or `active_chain`, both of
which are lists indexed by height.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from btclib_node.chainstate.block_index import BlockStatus

if TYPE_CHECKING:
    from btclib_node.chainstate.block_index import BlockIndex

__all__ = [
    "BlockAvailability",
    "get_ancestor",
    "peer_has_header",
    "process_block_availability",
    "update_block_availability",
    "update_last_common_block",
]

# Core's `BLOCK_DOWNLOAD_WINDOW` (`src/net_processing.cpp`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag): how far past
# `last_common` its walk reaches.
_BLOCK_DOWNLOAD_WINDOW = 1024


@dataclass
class BlockAvailability:
    """The blocks one peer is known to have, as block hashes.

    `best_known` is the most-work block the peer announced, and
    `last_unknown` the last one it announced that was not yet indexed.
    `last_common` is the last block both this node and the peer hold in
    full. `best_header_sent` is the best header this node sent the peer.
    Each is `None` until first set, as Core's pointers start null.
    """

    best_known: bytes | None = None
    last_unknown: bytes | None = None
    last_common: bytes | None = None
    best_header_sent: bytes | None = None


def _ancestor_at(block_index: BlockIndex, block_hash: bytes, height: int) -> bytes:
    """Return the ancestor of `block_hash` at `height`, not above its own."""
    header_dict = block_index.header_dict
    header_index = block_index.header_index
    active_chain = block_index.active_chain
    current = block_hash
    current_height = header_dict[current].index
    while current_height > height:
        if current in block_index.header_index_pos:
            return header_index[height]
        if (
            current_height < len(active_chain)
            and active_chain[current_height] == current
        ):
            return active_chain[height]
        current = header_dict[current].header.previous_block_hash
        current_height -= 1
    return current


def get_ancestor(
    block_index: BlockIndex, block_hash: bytes, height: int
) -> bytes | None:
    """Return the ancestor of `block_hash` at `height`: Core's `GetAncestor`.

    `None` for a height above the block's own, as Core answers `nullptr`.
    """
    if height > block_index.header_dict[block_hash].index:
        return None
    return _ancestor_at(block_index, block_hash, height)


def _last_common_ancestor(
    block_index: BlockIndex, first: bytes, second: bytes
) -> bytes:
    """Return the highest block both descend from: `LastCommonAncestor`."""
    header_dict = block_index.header_dict
    height = min(header_dict[first].index, header_dict[second].index)
    first = _ancestor_at(block_index, first, height)
    second = _ancestor_at(block_index, second, height)
    while first != second:
        first = header_dict[first].header.previous_block_hash
        second = header_dict[second].header.previous_block_hash
    return first


def _path_down(
    block_index: BlockIndex, block_hash: bytes, top: int, bottom: int
) -> list[bytes]:
    """Return the ancestors of `block_hash` from `top` down to above `bottom`.

    One `_ancestor_at` and then one parent per height, where a call of
    `_ancestor_at` per height would walk back from `block_hash` each time:
    a peer announcing a side chain far above both of this node's chains
    would make every pass of `DownloadManager` cost that distance times
    the window.
    """
    if top <= bottom:
        return []
    header_dict = block_index.header_dict
    path = [_ancestor_at(block_index, block_hash, top)]
    for _ in range(top - bottom - 1):
        path.append(header_dict[path[-1]].header.previous_block_hash)
    return path


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


def update_last_common_block(
    block_index: BlockIndex, state: BlockAvailability, minimum_chain_work: int
) -> None:
    """Move `last_common` as Core's `FindNextBlocksToDownload` moves it.

    Nothing moves while `best_known` is unset or has less work than the
    active tip or than `minimum_chain_work`. `last_common` is then reset
    to the fork point between `best_known` and the active tip, where it
    is unset, has less work than that fork point, or is no longer an
    ancestor of `best_known`. From there it moves forward along
    `best_known`'s chain over every block this node holds, stopping at
    the first it does not, at an invalid block, and at Core's
    `BLOCK_DOWNLOAD_WINDOW` + 1 blocks past where it started, which is
    how far Core's walk reaches.

    Core moves it over a block that has its data and all its
    ancestors', or that is on the active chain. No block past
    `last_common` along `best_known`'s chain is on the active chain,
    `last_common` never having less work than the fork point, and a
    block held after an unbroken run of held blocks has all its
    ancestors. Core's walk also collects the blocks to request from the
    peer, which is `DownloadManager`'s own job here, so it goes on past
    the first block it cannot move over where this one stops.
    """
    process_block_availability(block_index, state)
    best_known = state.best_known
    if best_known is None:
        return
    chainwork = block_index.chainwork
    tip = block_index.active_chain[-1]
    if (
        chainwork[best_known] < chainwork[tip]
        or chainwork[best_known] < minimum_chain_work
    ):
        return
    header_dict = block_index.header_dict
    fork_point = _last_common_ancestor(block_index, best_known, tip)
    last_common = state.last_common
    if (
        last_common is None
        or chainwork[fork_point] > chainwork[last_common]
        or get_ancestor(block_index, best_known, header_dict[last_common].index)
        != last_common
    ):
        last_common = fork_point
    height = header_dict[last_common].index
    max_height = min(header_dict[best_known].index, height + _BLOCK_DOWNLOAD_WINDOW + 1)
    for block_hash in reversed(_path_down(block_index, best_known, max_height, height)):
        block_info = header_dict[block_hash]
        if block_info.status == BlockStatus.invalid:
            break
        if not block_info.downloaded:
            break
        last_common = block_hash
    state.last_common = last_common
