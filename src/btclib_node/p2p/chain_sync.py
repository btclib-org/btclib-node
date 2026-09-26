# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Whether an outbound peer keeps up with this node's chain.

`ChainSyncTimeoutState` is Core's `CNodeState::m_chain_sync`
(`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
tag), and the two functions below are the two of Core's that write it:
`consider_eviction` is `ConsiderEviction`, which `DownloadManager.step`
runs for every connected peer as Core's `SendMessages` does, and
`protect_if_caught_up` is the protection at the end of
`UpdatePeerStateForReceivedHeaders`, which `callbacks.headers` runs on a
batch that connects.

Core applies the two to different sets of outbound connections, which
`_outbound_or_block_relay` and `_full_outbound` name after Core's
`IsOutboundOrBlockRelayConn` and `IsFullOutboundConn`. This node opens
one kind of automatic outbound connection, Core's `OUTBOUND_FULL_RELAY`,
so today both answer `Connection.automatic`. A block is a hash here
where Core holds a `CBlockIndex*`, as in `block_availability`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.block_availability import get_ancestor

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib_node import Node
    from btclib_node.chainstate.block_index import BlockIndex
    from btclib_node.p2p.connection import Connection

__all__ = [
    "CHAIN_SYNC_TIMEOUT",
    "HEADERS_RESPONSE_TIME",
    "MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT",
    "ChainSyncTimeoutState",
    "consider_eviction",
    "protect_if_caught_up",
]

# Core's constants of the same names, in seconds
CHAIN_SYNC_TIMEOUT = 20 * 60
HEADERS_RESPONSE_TIME = 2 * 60
MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT = 4


@dataclass
class ChainSyncTimeoutState:
    """Core's `ChainSyncTimeoutState`, one per peer.

    `timeout` is when the peer must have caught up, 0 where no timeout
    is set; `work_header` is the tip it must match the work of;
    `sent_getheaders` is whether it has been asked once; and `protect`
    exempts it from all of this for as long as it is connected.
    """

    timeout: float = 0
    work_header: bytes | None = None
    sent_getheaders: bool = False
    protect: bool = False


def _outbound_or_block_relay(conn: Connection) -> bool:
    """Core's `IsOutboundOrBlockRelayConn`, which `ConsiderEviction` reads."""
    return conn.automatic


def _full_outbound(conn: Connection) -> bool:
    """Core's `IsFullOutboundConn`, which the protection reads."""
    return conn.automatic


def _locator(block_index: BlockIndex, start: bytes) -> list[bytes]:
    """Return Core's `GetLocator(start)`: `start` and its ancestors.

    Core's `LocatorEntries` (`src/chain.cpp`, at bitcoin/bitcoin@9be056a8a7):
    `start` and each of the ten blocks below it, then exponentially
    sparser, always ending at genesis. The ancestors are `start`'s own,
    through `get_ancestor`, whether or not `start` is still on the active
    chain.
    """
    current = start
    height = block_index.header_dict[start].index
    locator: list[bytes] = []
    step = 1
    while True:
        locator.append(current)
        if height == 0:
            return locator
        height = max(height - step, 0)
        # always found: `height` is at most `current`'s own
        current = cast("bytes", get_ancestor(block_index, current, height))
        # Core's own unnamed 10, as in `get_block_locator_hashes`
        if len(locator) > 10:  # noqa: PLR2004
            step *= 2


def consider_eviction(
    node: Node,
    conn: Connection,
    now: float,
    send_getheaders: Callable[[Node, Connection, list[bytes]], bool],
) -> None:
    """Give an outbound peer behind this node's tip a deadline, then drop it.

    Core's `ConsiderEviction`: a peer whose best known block has less
    work than the tip gets `CHAIN_SYNC_TIMEOUT`, then one `getheaders`
    from the parent of the tip it was measured against and
    `HEADERS_RESPONSE_TIME` to answer, and is disconnected if it has
    still not caught up. A peer reaching the tip's work clears the
    timeout, and one reaching only the old tip's work gets a new one
    against the current tip. It applies to a peer this node has started
    syncing headers from and has not protected. `send_getheaders` is
    `callbacks.maybe_send_getheaders`, Core's `MaybeSendGetHeaders`.
    """
    state = conn.chain_sync
    manager = node.download_manager
    if state.protect or not _outbound_or_block_relay(conn):
        return
    if conn.id not in manager.headers_sync_timeouts:
        return
    block_index = node.chainstate.block_index
    chainwork = block_index.chainwork
    tip = block_index.active_chain[-1]
    best_known = conn.block_availability.best_known
    best_work = -1 if best_known is None else chainwork[best_known]
    if best_work >= chainwork[tip]:
        if state.timeout != 0:
            state.timeout = 0
            state.work_header = None
            state.sent_getheaders = False
    elif state.timeout == 0 or (
        state.work_header is not None
        and best_known is not None
        and best_work >= chainwork[state.work_header]
    ):
        state.timeout = now + CHAIN_SYNC_TIMEOUT
        state.work_header = tip
        state.sent_getheaders = False
    elif state.timeout > 0 and now > state.timeout:
        if state.sent_getheaders:
            node.logger.info(
                "Outbound peer has old chain, best known block = %s, "
                "disconnecting connection %s",
                "<none>" if best_known is None else best_known.hex(),
                conn.id,
            )
            conn.stop()
        else:
            # set with `timeout`, so never None here
            work_header = cast("bytes", state.work_header)
            work_info = block_index.header_dict[work_header]
            # Core's `GetLocator(m_work_header->pprev)`, which is empty
            # where the tip measured against is genesis
            locator = (
                _locator(block_index, work_info.header.previous_block_hash)
                if work_info.index > 0
                else []
            )
            send_getheaders(node, conn, locator)
            state.sent_getheaders = True
            state.timeout = now + HEADERS_RESPONSE_TIME


def protect_if_caught_up(node: Node, conn: Connection) -> None:
    """Exempt an outbound peer with the tip's work from `consider_eviction`.

    The end of Core's `UpdatePeerStateForReceivedHeaders`: a peer this
    node drew itself, still connected, whose best known block has at
    least the tip's work, is protected while fewer than
    `MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT` are. Core counts in
    `m_outbound_peers_with_protect_from_disconnect`, which drops as a
    protected peer is finalized; this counts the connections held.
    """
    state = conn.chain_sync
    best_known = conn.block_availability.best_known
    if state.protect or not _full_outbound(conn) or best_known is None:
        return
    if conn.status != P2pConnStatus.Connected:
        return
    # a copy, as `DownloadManager` takes one: `P2pManager`'s own thread
    # removes connections while this runs on `Node`'s
    connections = list(node.p2p_manager.connections.values())
    protected = sum(other.chain_sync.protect for other in connections)
    if protected >= MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT:
        return
    block_index = node.chainstate.block_index
    chainwork = block_index.chainwork
    if chainwork[best_known] >= chainwork[block_index.active_chain[-1]]:
        state.protect = True
