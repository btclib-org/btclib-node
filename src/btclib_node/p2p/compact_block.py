# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""BIP152 compact blocks, as this node sends and receives them.

`compact_block` is a block as the `cmpctblock` this node sends of it.
`callbacks` serves one asked for, and `main`'s block announcement sends
one to a high-bandwidth peer. `callbacks` imports `main`, so what both
need cannot live there.

`block_checked` is what `main.update_chain` calls for every block it
connects, and it is where a peer that gave this node a new block is
chosen as one of its high-bandwidth peers
(`maybe_set_peer_as_announcing_header_and_ids`).
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from btclib.p2p.compact_blocks import (
    CMPCTBLOCKS_VERSION,
    CmpctBlock,
    PrefilledTransaction,
    SendCmpct,
)

from btclib_node.constants import P2pConnStatus

if TYPE_CHECKING:
    from btclib.block import Block
    from btclib.tx import Tx

    from btclib_node import Node

__all__ = [
    "MAX_CMPCTBLOCKS_INFLIGHT_PER_BLOCK",
    "MAX_EXTRA_TXNS",
    "MAX_EXTRA_TX_WEIGHT",
    "MostRecentBlock",
    "block_checked",
    "compact_block",
    "maybe_set_peer_as_announcing_header_and_ids",
]

# Core's `MAX_CMPCTBLOCKS_INFLIGHT_PER_BLOCK` and
# `DEFAULT_BLOCK_RECONSTRUCTION_EXTRA_TXN` (`src/net_processing.h`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag): how many peers one block is
# asked of through `cmpctblock`, and how many refused transactions are kept
# to rebuild one with. Core's `-blockreconstructionextratxn` sets the
# second, and this tree has no such option.
MAX_CMPCTBLOCKS_INFLIGHT_PER_BLOCK = 3
MAX_EXTRA_TXNS = 100
# Core keeps a refused transaction for that only below 100000 bytes of
# `RecursiveDynamicUsage`. Its orphanage measures a transaction's memory by
# its weight, which "is often higher than the actual memory usage"
# (`src/node/txorphanage.cpp`, same tag), and so does this bound.
MAX_EXTRA_TX_WEIGHT = 100_000
# BIP152: "we only get 3 of our peers to announce blocks using compact
# encodings", in Core's words (`MaybeSetPeerAsAnnouncingHeaderAndIDs`)
_HB_PEERS = 3


@dataclass(frozen=True, slots=True)
class MostRecentBlock:
    """The block `new_pow_valid_block` last kept, and its `cmpctblock`.

    Core's `m_most_recent_block` and `m_most_recent_compact_block`
    (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), which `m_most_recent_block_hash` names. `txs` is Core's
    `m_most_recent_block_txs`, built with them: every transaction of the
    block under its txid and under its wtxid, the two kept apart as
    Core's `GenTxid` does.
    """

    block: Block
    compact: CmpctBlock
    txs: dict[tuple[bool, bytes], Tx] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Index the block's transactions by txid and by wtxid."""
        txs: dict[tuple[bool, bytes], Tx] = {}
        for tx in self.block.transactions:
            txs.setdefault((False, tx.id), tx)
            txs.setdefault((True, tx.hash), tx)
        object.__setattr__(self, "txs", txs)

    @property
    def hash(self) -> bytes:
        """Return the block's hash, Core's `m_most_recent_block_hash`."""
        return self.block.header.hash


def compact_block(block: Block, nonce: int) -> CmpctBlock:
    """Return `block` as a `cmpctblock`, the coinbase alone sent whole.

    Core's `CBlockHeaderAndShortTxIDs` constructor: the coinbase
    prefilled at index 0, and every other transaction by the short id of
    its wtxid under the key `nonce` and the header give.
    """
    coinbase = PrefilledTransaction(0, block.transactions[0])
    keyed = CmpctBlock(block.header, nonce, (), (coinbase,), check_validity=False)
    short_ids = [keyed.short_id(tx.hash) for tx in block.transactions[1:]]
    return CmpctBlock(block.header, nonce, short_ids, (coinbase,))


def maybe_set_peer_as_announcing_header_and_ids(node: Node, conn_id: int) -> None:
    """Ask a peer to announce new blocks as `cmpctblock`, dropping the oldest.

    Core's `MaybeSetPeerAsAnnouncingHeaderAndIDs` (`src/net_processing.cpp`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), over
    `DownloadManager.hb_peers`, Core's `lNodesAnnouncingHeaderAndIDs`.
    Nothing for a peer gone or one that sent no `sendcmpct` of version 2.
    A peer already chosen moves to the end. Otherwise, where `_HB_PEERS`
    are chosen, the first is sent `sendcmpct(0, 2)` and dropped, an
    outbound one being kept where it is the only outbound left and an
    inbound peer is the one added. The peer is sent `sendcmpct(1, 2)`
    and added at the end.

    A peer id stays in the list after its peer is gone, as in Core.
    """
    connections = node.p2p_manager.connections
    conn = connections.get(conn_id)
    if conn is None or not conn.provides_cmpctblocks:
        return
    hb_peers = node.download_manager.hb_peers
    if conn_id in hb_peers:
        hb_peers.remove(conn_id)
        hb_peers.append(conn_id)
        return
    if conn.inbound and len(hb_peers) >= _HB_PEERS:
        outbound = [
            peer_id
            for peer_id in hb_peers
            if (peer := connections.get(peer_id)) is not None and not peer.inbound
        ]
        if outbound == hb_peers[:1]:
            hb_peers[0], hb_peers[1] = hb_peers[1], hb_peers[0]
    # Core's `ForNode`, which reaches a peer only once its handshake is done
    if conn.status != P2pConnStatus.Connected:
        return
    if len(hb_peers) >= _HB_PEERS:
        dropped = connections.get(hb_peers.pop(0))
        if dropped is not None and dropped.status == P2pConnStatus.Connected:
            dropped.send(SendCmpct(announce=False, version=CMPCTBLOCKS_VERSION))
            dropped.bip152_highbandwidth_to = False
    conn.send(SendCmpct(announce=True, version=CMPCTBLOCKS_VERSION))
    conn.bip152_highbandwidth_to = True
    hb_peers.append(conn_id)


def block_checked(node: Node, block_hash: bytes, *, valid: bool) -> None:
    """Take in that a block was connected, or refused, by `update_chain`.

    Core's `BlockChecked` (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which `ConnectTip` calls
    for each block it connects or refuses. A valid block that came from
    a peer (`DownloadManager.block_source`) makes it a high-bandwidth
    peer, out of initial block download and where no other block is in
    flight, as Core's "this is currently the best block we're aware of".
    A refused one costs the peer that sent it, unless it came through
    BIP152: Core's `MaybePunishNodeForBlock` for `BLOCK_CONSENSUS`. The
    block's source is then forgotten.
    """
    source = node.download_manager.block_source.pop(block_hash, None)
    if source is None:
        return
    conn_id, may_punish = source
    if not valid:
        conn = node.p2p_manager.connections.get(conn_id)
        if conn is not None and may_punish:
            node.logger.log_debug("net", "Misbehaving: peer=%d", conn_id)
            node.p2p_manager.maybe_discourage_and_disconnect(conn)
        return
    if node.is_initial_block_download:
        return
    in_flight = {
        requested
        for conn in list(node.p2p_manager.connections.values())
        for requested in conn.download_queue
    }
    if in_flight <= {block_hash}:
        maybe_set_peer_as_announcing_header_and_ids(node, conn_id)
