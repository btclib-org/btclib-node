# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`DownloadManager`, what decides what this node asks its peers for.

Which peer headers are synced from; which blocks each peer is asked
for, and which peer is dropped for stalling the download; transaction
announcement and request tracking, and the trickle timing behind both
-- `feefilter` resends, address relay, and the exponential delays that
keep two peers from being told the same thing in lockstep. Most of the
constants here are a named Bitcoin Core constant carried over with the
commit it was read at beside it, per this tree's own convention of
matching Core's behaviour, always.
"""

import heapq
import itertools
import math
import time
from bisect import bisect_left
from collections import deque
from random import SystemRandom
from typing import TYPE_CHECKING

from btclib.p2p.address import ServiceFlags
from btclib.p2p.addrv2 import BIP155Network
from btclib.p2p.inventory import GetData, Inv, Inventory, InventoryType
from btclib.p2p.negotiation import FeeFilter, SendHeaders

from btclib_node.chainstate.block_index import BlockStatus, block_time
from btclib_node.config import DEFAULT_MIN_RELAY_FEERATE
from btclib_node.constants import P2pConnStatus
from btclib_node.exceptions import MissingPrevoutError, TxRejectedError
from btclib_node.mempool import package_hash
from btclib_node.orphanage import TxOrphanage
from btclib_node.p2p.block_availability import (
    find_next_blocks_to_download,
    first_in_flight,
)
from btclib_node.p2p.callbacks import maybe_send_getheaders
from btclib_node.p2p.chain_sync import consider_eviction
from btclib_node.p2p.compact_block import MAX_EXTRA_TX_WEIGHT, MAX_EXTRA_TXNS
from btclib_node.p2p.eviction import get_network
from btclib_node.p2p.permissions import NetPermissionFlags
from btclib_node.p2p.protocol_version import (
    FEEFILTER_VERSION,
    SENDHEADERS_VERSION,
    common_version,
)
from btclib_node.txrequest import TxRequestTracker

if TYPE_CHECKING:
    from collections.abc import Sequence

    from btclib.tx.tx import Tx

    from btclib_node import Node
    from btclib_node.log import Logger
    from btclib_node.p2p.connection import Connection

__all__ = [
    "MAX_BLOCKS_IN_TRANSIT_PER_PEER",
    "DownloadManager",
    "block_inventory_type",
]

# net_processing.cpp's INBOUND_INVENTORY_BROADCAST_INTERVAL and
# OUTBOUND_INVENTORY_BROADCAST_INTERVAL, at bitcoin/bitcoin@58a7869f86: the
# mean of the exponential draw `_send_due_announcements` makes for the
# next trickle, an outbound peer's own and shorter than an inbound one's
# for the same reason Core's is -- an outbound peer is one this node
# chose to open, so there are fewer of them for a spy to multiply an
# inbound peer's sample count across. An inbound peer's draw is not its
# own: `_inbound_net_class` and `DownloadManager._next_inbound_inv_time`
# are why.
_INBOUND_TX_ANNOUNCE_INTERVAL = 5.0
_OUTBOUND_TX_ANNOUNCE_INTERVAL = 2.0

# net_processing.cpp's `INVENTORY_BROADCAST_TARGET` (14 per second over
# the 5 second inbound interval) and `INVENTORY_BROADCAST_MAX`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag: the number of transactions one
# trickle announces to a peer is the target plus 5 for each 1000 queued, at
# most the maximum. bitcoin/bitcoin#34628 replaces this with two global
# buckets after v31.1; relay follows the release.
_INVENTORY_BROADCAST_TARGET = 70
_INVENTORY_BROADCAST_MAX = 1000


# Sorts before every key `Mempool.mining_order_keys` returns, for an entry
# the mempool no longer holds: Core's `CompareMiningScoreWithTopology`
# (`txmempool.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) puts
# one first, so a trickle pops it before any held entry.
_GONE_KEY = (-(1 << 256), 0, b"")


def _trickle_cap(queued: int) -> int:
    """Return how many of `queued` announcements one trickle may send."""
    return min(
        _INVENTORY_BROADCAST_MAX,
        _INVENTORY_BROADCAST_TARGET + (queued // 1000) * 5,
    )


# Core's transaction download constants, `node/txdownloadman.h` and
# `net_processing.cpp` at bitcoin/bitcoin@9be056a8a7, the v31.1 tag:
# - `MAX_PEER_TX_ANNOUNCEMENTS`, the announcements tracked per peer;
# - `MAX_GETDATA_SZ`, the items in one `getdata`;
# - `GETDATA_TX_INTERVAL`, in seconds, how long a request holds before
#   the next announcer is asked;
# - `MAX_PEER_TX_REQUEST_IN_FLIGHT`, the requests to a peer past which
#   `OVERLOADED_PEER_TX_DELAY` applies to its announcements;
# - `NONPREF_PEER_TX_DELAY`, `TXID_RELAY_DELAY` and
#   `OVERLOADED_PEER_TX_DELAY`, in seconds, the delays before an
#   announcement may be asked for.
_MAX_PEER_TX_ANNOUNCEMENTS = 5000
_MAX_GETDATA_SZ = 1000
_GETDATA_TX_INTERVAL = 60.0
_MAX_PEER_TX_REQUEST_IN_FLIGHT = 100
_NONPREF_PEER_TX_DELAY = 2.0
_TXID_RELAY_DELAY = 2.0
_OVERLOADED_PEER_TX_DELAY = 2.0

# the inventory types a `notfound` names a transaction by
_TX_INVENTORY_TYPES = (
    InventoryType.MSG_TX,
    InventoryType.MSG_WTX,
    InventoryType.MSG_WITNESS_TX,
)

# Core's own `AVG_FEEFILTER_BROADCAST_INTERVAL` (10min) and
# `MAX_FEEFILTER_CHANGE_DELAY` (5min), `net_processing.cpp`, same commit:
# the mean of the exponential draw `_send_due_feefilters` makes for a
# connection's ordinary resend, and the bound a large-enough move pulls
# that draw forward to instead.
_AVG_FEEFILTER_BROADCAST_INTERVAL = 600.0
_MAX_FEEFILTER_CHANGE_DELAY = 300.0

# `FeeFilterRounder`'s own `FEE_FILTER_SPACING`/`MAX_FILTER_FEERATE`
# (`policy/fees/block_policy_estimator.h`, same commit): the geometric
# spacing `_fee_filter_buckets` grows its bucket set by, and the sat/kvB
# ceiling it stops at.
_FEE_FILTER_SPACING = 1.1
_MAX_FILTER_FEERATE = 1e7

# `rand_exp_duration`, the same file: a CSPRNG rather than a statistical
# one, for the same reason `secrets` is what the rest of this tree draws
# a peer-facing nonce or choice from -- this schedule is exactly what a
# peer is meant not to be able to predict.
_rng = SystemRandom()


# `block_download`'s own timing, in seconds: Core's
# `BLOCK_STALLING_TIMEOUT_DEFAULT` and `BLOCK_STALLING_TIMEOUT_MAX`, the
# bounds of how long a peer may hold up the download window, and
# `BLOCK_DOWNLOAD_TIMEOUT_BASE` and `BLOCK_DOWNLOAD_TIMEOUT_PER_PEER`,
# the multiples of `_POW_TARGET_SPACING` a block may stay in flight
# (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
_BLOCK_STALLING_TIMEOUT_DEFAULT = 2
_BLOCK_STALLING_TIMEOUT_MAX = 64
_BLOCK_DOWNLOAD_TIMEOUT_BASE = 1
_BLOCK_DOWNLOAD_TIMEOUT_PER_PEER = 0.5

# Core's `EXTRA_PEER_CHECK_INTERVAL` and `MINIMUM_CONNECT_TIME`
# (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag):
# how often `_check_for_stale_tip_and_evict_peers` runs, and how long
# the peer it picks must have been connected for it to be dropped.
_EXTRA_PEER_CHECK_INTERVAL = 45
_MINIMUM_CONNECT_TIME = 30

# Core's `ReattemptInitialBroadcast` runs 10 minutes after start-up and
# after each run, plus a random 0 to 5 minutes drawn afresh each time
# (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), in
# seconds.
_REATTEMPT_BROADCAST_INTERVAL = 600
_REATTEMPT_BROADCAST_JITTER = 300


def _reattempt_broadcast_delay() -> float:
    """Core's `10min + randrange(5min)`, in seconds."""
    return _REATTEMPT_BROADCAST_INTERVAL + _rng.uniform(0, _REATTEMPT_BROADCAST_JITTER)


# The number of block intervals `CanDirectFetch` (same sha) allows the
# active tip to lag the clock by, in `_POW_TARGET_SPACING` units.
_DIRECT_FETCH_SPACINGS = 20

# Core's `STALE_CHECK_INTERVAL` (same sha), in seconds, and the block
# intervals past which `TipMayBeStale` calls the tip stale.
_STALE_CHECK_INTERVAL = 10 * 60
_STALE_TIP_SPACINGS = 3

# `sync_headers`'s own timing, in seconds: `HEADERS_DOWNLOAD_TIMEOUT_BASE`
# and `HEADERS_DOWNLOAD_TIMEOUT_PER_HEADER` (`net_processing.cpp`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), the second scaled by the
# headers expected between the best header and now, one per
# `_POW_TARGET_SPACING` -- `nPowTargetSpacing`, `10 * 60` on every chain
# `kernel/chainparams.cpp` defines at the same sha. `_RECENT_BEST_HEADER`
# is the `24h` `SendMessages` compares the best header's own time
# against: a best header younger than that has every peer asked for
# headers, not one.
_HEADERS_DOWNLOAD_TIMEOUT_BASE = 15 * 60
_HEADERS_DOWNLOAD_TIMEOUT_PER_HEADER = 0.001
_POW_TARGET_SPACING = 10 * 60
_RECENT_BEST_HEADER = 24 * 60 * 60

# Core's `MAX_BLOCKS_IN_TRANSIT_PER_PEER` (`net_processing.cpp`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): how many blocks one peer is
# asked for and has not yet sent. Public because the largest answer a
# well-behaved peer owes this node is that many blocks, which the tests
# of the receive-side bounds size against.
MAX_BLOCKS_IN_TRANSIT_PER_PEER = 16


def _fee_filter_buckets(min_incremental_fee: int) -> list[float]:
    """Return the sat/kvB boundaries `_round_fee_filter` may round to.

    Core's own `MakeFeeSet` (`policy/fees/block_policy_estimator.cpp`,
    at bitcoin/bitcoin@58a7869f86): zero, then a geometric series from half
    `min_incremental_fee` (never under 1) up to `_MAX_FILTER_FEERATE`,
    spaced by `_FEE_FILTER_SPACING`. Kept as `float` and not rounded
    here: Core's own `std::set<double>` holds the raw boundary too, and
    only the value `_round_fee_filter` finally selects is ever
    truncated (`static_cast<CAmount>`) -- rounding a boundary to build
    this set would select a different sat/kvB than Core does for a
    boundary that was never an integer to begin with, 137.7 truncating
    to 137 there against rounding to 138 here. `Download` builds it from
    `DEFAULT_MIN_RELAY_FEERATE`, as Core builds its rounder from
    `DEFAULT_MIN_RELAY_TX_FEE` and not from `-minrelaytxfee`
    (`m_fee_filter_rounder{CFeeRate{DEFAULT_MIN_RELAY_TX_FEE}, ...}`,
    `net_processing.cpp`).
    """
    buckets = {0.0}
    boundary = float(max(1, min_incremental_fee // 2))
    while boundary <= _MAX_FILTER_FEERATE:
        buckets.add(boundary)
        boundary *= _FEE_FILTER_SPACING
    return sorted(buckets)


def _round_fee_filter(rate: int, buckets: list[float]) -> int:
    """Quantize a feerate to one of `buckets`, for privacy on broadcast.

    Core's own `FeeFilterRounder::round`
    (`policy/fees/block_policy_estimator.cpp`, same commit): the lowest
    bucket at or above `rate`, unless that is the top of the set or a
    2-in-3 draw says round down instead -- so this node's own rolling
    minimum is not readable exactly from what it tells a peer, and a
    peer that watches for the transition between two adjacent buckets
    still cannot tell it apart from the coin landing the other way.
    The selected boundary is truncated toward zero on the way out
    (`static_cast<CAmount>`; Python's `int()` does the same for a
    positive value), Core's own final step rather than a rounding this
    set already did while it was built.
    """
    index = bisect_left(buckets, rate)
    if index == len(buckets) or (index != 0 and _rng.randrange(3) != 0):
        index -= 1
    return int(buckets[index])


def _inbound_net_class(conn: Connection) -> BIP155Network | int:
    """Return the key an inbound peer's schedule is shared across.

    `CNode::m_network_key` (net.h:755) is what `NextInvToInbounds`
    (net_processing.cpp:6318-6319, calling `PeerManagerImpl::
    NextInvToInbounds` at :1273-1282) actually keys its per-peer timer
    on, at bitcoin/bitcoin@58a7869f86. For an inbound connection it is a
    hash (net.cpp:1853-1857) of the peer's coarse `GetNetClass()`
    (netaddress.cpp:674) together with *this node's own* listening bind
    address and port -- not anything of the peer's own beyond which
    class it falls into. `NetGroupManager::GetGroup` (netgroup.cpp),
    which does partition by the peer's /16 or /32, feeds
    `nKeyedNetGroup` instead: addrman bucketing and inbound-eviction
    diversity, not this timer.

    So every inbound peer of one address family shares this node's one
    schedule for that family, regardless of its own subnet. IPv4 and
    IPv6 are the only two `btclib_node.p2p.address.can_connect` ever
    hands a connection here, so returning the BIP155 network id itself
    is enough of a stand-in for Core's hash. Tor's id stands in for a
    connection on an `=onion` listener (`inbound_onion`). There is
    nothing here for Core's I2P, CJDNS or bind-address component to do.
    """
    return BIP155Network.TORV3 if conn.inbound_onion else conn.address.network_id


def _can_serve_blocks(conn: Connection) -> bool:
    """Whether `conn` can serve this node blocks at all.

    Core's own `CanServeBlocks` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `NODE_NETWORK` or
    `NODE_NETWORK_LIMITED` advertised, either being enough. `True` for
    a connection with no `version_message`, which `callbacks.verack`
    never promotes, so that this reads the absence in the direction
    `_is_limited_peer` below reads it. btclib-org/btclib-node#725
    """
    version_msg = conn.version_message
    if version_msg is None:
        return True
    services = version_msg.services
    return bool(
        services & (ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_NETWORK_LIMITED)
    )


def _is_limited_peer(conn: Connection) -> bool:
    """Whether `conn` can only serve blocks near its own tip.

    Core's own `IsLimitedPeer` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `NODE_NETWORK_LIMITED`
    advertised and `NODE_NETWORK` not. `False` for a connection with no
    `version_message`, which `callbacks.verack` never promotes.
    """
    version_msg = conn.version_message
    if version_msg is None:
        return False
    services = version_msg.services
    return bool(
        services & ServiceFlags.NODE_NETWORK_LIMITED
        and not services & ServiceFlags.NODE_NETWORK
    )


def _can_serve_witnesses(conn: Connection) -> bool:
    """Whether `conn` can serve this node witness data.

    Core's own `CanServeWitnesses` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `NODE_WITNESS`
    advertised. `False` for a connection with no `version_message`,
    which `callbacks.verack` never promotes, the same direction
    `_is_limited_peer` reads that absence in.
    """
    version_msg = conn.version_message
    return bool(version_msg and version_msg.services & ServiceFlags.NODE_WITNESS)


def block_inventory_type(conn: Connection) -> InventoryType:
    """Return the type a block is asked of `conn` by: Core's `GetFetchFlags`.

    `MSG_WITNESS_BLOCK` where `conn` can serve witnesses, `MSG_BLOCK`
    otherwise.
    """
    if _can_serve_witnesses(conn):
        return InventoryType.MSG_WITNESS_BLOCK
    return InventoryType.MSG_BLOCK


def _is_preferred_download(conn: Connection) -> bool:
    """Whether `conn` is a peer headers and blocks are preferably synced from.

    Core's own `fPreferredDownload` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): an outbound or `NO_BAN`
    peer that is not an addr-fetch one and can serve blocks.
    """
    return (
        (not conn.inbound or NetPermissionFlags.NO_BAN in conn.permissions)
        and not conn.addr_fetch
        and _can_serve_blocks(conn)
    )


def _is_sync_peer(conn: Connection, preferred: int, *, blocks_in_flight: bool) -> bool:
    """Whether headers and blocks are synced from `conn`.

    Core's `sync_blocks_and_headers_from_peer` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a preferred peer, or any
    other that is not an addr-fetch one while there is no preferred peer
    or no block in flight.
    """
    return _is_preferred_download(conn) or (
        not conn.addr_fetch and (not preferred or not blocks_in_flight)
    )


def _tx_fetch_type(conn: Connection, *, wtxid: bool) -> InventoryType:
    """Return the type a `getdata` asks `conn` for a transaction under.

    Core's `SendMessages` (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag): `MSG_WTX` for a wtxid, else `MSG_TX` with
    `GetFetchFlags`' witness flag where the peer offers `NODE_WITNESS`.
    """
    if wtxid:
        return InventoryType.MSG_WTX
    version_message = conn.version_message
    if version_message and version_message.services & ServiceFlags.NODE_WITNESS:
        return InventoryType.MSG_WITNESS_TX
    return InventoryType.MSG_TX


def _extend_tx_announce_queue(conn: Connection, new_for_conn: list[bytes]) -> None:
    """Append `new_for_conn`'s wtxids not already in `conn`'s queue.

    The queue keeps arrival order; `_send_due_announcements` sends from it
    best-paying first. btclib-org/btclib-node#444
    """
    for wtxid in new_for_conn:
        conn.tx_announce_queue.setdefault(wtxid)


class DownloadManager:
    """What decides what this node asks its peers for.

    Mostly one `step` at a time, and blocks also from the `headers`
    callback, through `headers_direct_fetch`.

    Which peer headers are synced from, which blocks each peer is asked
    for and who stalls them, transaction announcement and request
    tracking, and the `feefilter` trickle: the module docstring above is
    where the constants each of those follows are argued against Core's
    own.
    """

    def __init__(self, node: Node, logger: Logger) -> None:
        """Build the schedules and fee-filter buckets `step` reads from."""
        self.node = node
        self.logger = logger

        # Core's `m_block_stalling_timeout`: how long a peer may hold up
        # the download window before it is dropped, doubled at each drop
        # and decayed by `block_connected`.
        self.block_stalling_timeout: float = _BLOCK_STALLING_TIMEOUT_DEFAULT

        # conn_id is `None` for a transaction this node originated
        # (`P2pManager.broadcast_raw_transaction`) rather than received
        # from a peer -- the same list either way, so the peer an inv
        # goes out to cannot tell a relayed transaction from this node's
        # own by which path carried it. btclib-org/btclib-node#141
        self.received_txs: list[tuple[int | None, bytes]] = []
        # (conn_id, hash, txid): `txid` says `hash` was announced as a txid,
        # not a wtxid. A wtxid-relay peer may announce either, as Core's
        # `ToGenTxid` reads `MSG_WITNESS_TX` as a txid.
        self.inv_txs: list[tuple[int, bytes, bool]] = []
        # Core's `m_txrequest`: which of the peers that announced a
        # transaction is asked for it, and when. `inv_txs` feeds it in
        # `_request_wanted_txs`; the `tx` and `notfound` callbacks and
        # `_queue_announcements_for_received_txs` retire what it holds.
        self.tx_requests = TxRequestTracker()
        # Core's `m_orphanage`: what peers sent with parents not found yet,
        # kept to be taken up when a parent arrives and as the child a
        # parent that pays too little is accepted with. The `tx` callback
        # fills it through `mempool_rejected_tx`.
        self.orphanage = TxOrphanage()
        # Core's `vExtraTxnForCompact`: the transactions most recently
        # refused, which `callbacks.cmpctblock` rebuilds a block from
        # beside the mempool. `mempool_rejected_tx` fills it.
        self.extra_txns: deque[Tx] = deque(maxlen=MAX_EXTRA_TXNS)
        # Where `callbacks.cmpctblock` queues a block among the peers it is
        # asked of: `BlockAvailability.request_order`.
        self.request_orders = itertools.count(1)
        # Core's `lNodesAnnouncingHeaderAndIDs`, the ids of the peers asked
        # to announce new blocks as `cmpctblock`, the oldest first
        self.hb_peers: list[int] = []
        # Core's `mapBlockSource`: the peer each stored block not yet
        # connected came from, and whether it pays for a block that fails
        # to connect, which `compact_block.block_checked` reads
        self.block_source: dict[bytes, tuple[int, bool]] = {}

        # Core's `m_next_inv_to_inbounds_per_network_key`
        # (net_processing.cpp, the same commit): one schedule per
        # `_inbound_net_class`, shared by every inbound connection
        # currently in it, rather than one per connection -- an inbound
        # peer opening several connections to this node samples the same
        # draw from all of them instead of averaging several independent
        # ones down to a finer receipt time than one connection's jitter
        # allows. At most one live key per address family, matching how
        # coarse `m_network_key` actually is; never pruned, matching
        # Core, so a family's entry outlives the connections that drew
        # it.
        self._next_inv_to_inbounds: dict[BIP155Network | int, float] = {}

        # Built once, from this node's own configured floor, rather than
        # per call: Core's own `FeeFilterRounder` is a `PeerManagerImpl`
        # member constructed once too. `_max_feefilter` is what every
        # rate `_round_fee_filter` is given rounds to once it is at or
        # above the top bucket -- `_send_due_feefilters`'s own stand-in
        # for Core's `MAX_FILTER`, computed there by rounding `MAX_MONEY`
        # through the same set rather than read off it directly, which
        # is what forces every such value into the top bucket
        # deterministically instead of through `_round_fee_filter`'s own
        # coin flip: `bisect_left` finds the top bucket itself already
        # placed at `len(buckets) - 1`, only "at or past the end" -- not
        # "equal to the last element" -- always forces the round-down
        # branch. btclib-org/btclib-node#275
        self._fee_filter_buckets = _fee_filter_buckets(
            DEFAULT_MIN_RELAY_FEERATE.sats_per_kvbyte
        )
        # int(), not the top bucket's own float: BIP133's wire value is
        # an integer, and every other value this module ever sends is
        # one too, by way of _round_fee_filter's identical truncation.
        self._max_feefilter = int(self._fee_filter_buckets[-1])

        # Core's own `fSyncStarted` and `m_headers_sync_timeout`
        # (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        # tag), kept here rather than on `Connection` as Core keeps them
        # in `net_processing`'s own per-peer state rather than in
        # `CNode`: a connection id is in this dict once `sync_headers`
        # has sent that peer its initial `getheaders`, and the value is
        # when it gives up on the peer. `math.inf` is Core's
        # `microseconds::max()`, the timeout switched off once the best
        # header is recent.
        self.headers_sync_timeouts: dict[int, float] = {}
        # Core's `m_inv_triggered_getheaders_before_sync`, the peers
        # `callbacks.inv` has sent a `getheaders` before `sync_headers`
        # did, and `m_last_block_inv_triggering_headers_sync`, the block
        # announced that last did so.
        self.inv_triggered_getheaders: set[int] = set()
        self.last_block_inv_triggering_headers_sync: bytes | None = None
        # Core's `m_last_getheaders_timestamp`: when
        # `callbacks.maybe_send_getheaders` last sent each peer a
        # `getheaders` that no `headers` has answered since.
        self.last_getheaders_timestamps: dict[int, float] = {}
        # When `_check_for_stale_tip_and_evict_peers` next runs, which
        # Core schedules `EXTRA_PEER_CHECK_INTERVAL` from start-up, and
        # Core's `m_initial_sync_finished`.
        self._next_extra_peer_check = time.time() + _EXTRA_PEER_CHECK_INTERVAL
        self._initial_sync_finished = False
        # When `_reattempt_initial_broadcast` next runs
        self._next_reattempt_broadcast = time.time() + _reattempt_broadcast_delay()
        # Core's `m_last_tip_update`, which `main._finalize_fork` stamps
        # as each block connects, zero until then, and
        # `m_stale_tip_check_time`, when the stale-tip check next runs.
        self.last_tip_update = 0.0
        self._stale_tip_check_time = 0.0

    def step(self) -> None:
        """Run one pass.

        Headers, blocks and txs are asked for, sendheaders and feefilters
        sent, outbound peers behind this node's tip given a deadline, and
        Core's check for a stale tip and extra outbound peers run on its
        own interval.
        """
        self.sync_headers()
        self.block_download()
        # before `tx_download`, which announces and then empties `received_txs`
        self._reattempt_initial_broadcast()
        self.tx_download()
        self._send_due_sendheaders()
        self._send_due_feefilters()
        self._consider_evictions()
        self._check_for_stale_tip_and_evict_peers()

    def _reattempt_initial_broadcast(self) -> None:
        """Announce each unbroadcast transaction to every peer again.

        Core's `ReattemptInitialBroadcast` (`net_processing.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): it queues each held
        transaction of `Mempool.unbroadcast` the way
        `P2pManager.broadcast_raw_transaction` does, so `tx_download`
        skips a peer that already knows it, as `InitiateTxBroadcastToAll`
        does. A txid no longer held is dropped from the set. A peer's
        `getdata` for it is what ends the repeats (`callbacks.getdata`).

        It runs on `Node`'s thread, which owns the mempool and
        `received_txs`. Core, on its scheduler thread, takes `m_peer_mutex`
        instead. btclib-org/btclib-node#1816
        """
        now = time.time()
        if now < self._next_reattempt_broadcast:
            return
        self._next_reattempt_broadcast = now + _reattempt_broadcast_delay()
        mempool = self.node.mempool
        for txid in sorted(mempool.unbroadcast):
            wtxid = mempool.txid_index.get(txid)
            if wtxid is None:
                mempool.mark_broadcast(txid)
            else:
                self.received_txs.append((None, wtxid))

    def _check_for_stale_tip_and_evict_peers(self) -> None:
        """Drop an extra outbound peer, and let `P2pManager` dial one more.

        Core's `CheckForStaleTipAndEvictPeers` (`net_processing.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), every
        `_EXTRA_PEER_CHECK_INTERVAL`. Every `_STALE_CHECK_INTERVAL` it
        sets `try_new_outbound_peer` where the tip may be stale, under no
        `-connect`, and clears it where not. Once the active tip is
        within `CanDirectFetch`'s reach of the clock, it lets
        `P2pManager` dial an extra block-relay-only peer, once and for
        good, as `StartExtraBlockRelayPeers` does.
        """
        now = time.time()
        if now < self._next_extra_peer_check:
            return
        self._next_extra_peer_check = now + _EXTRA_PEER_CHECK_INTERVAL
        self._evict_extra_block_relay_peer(now)
        self._evict_extra_full_relay_peer(now)
        manager = self.node.p2p_manager
        if now > self._stale_tip_check_time:
            if manager.use_addrman_outgoing and self._tip_may_be_stale(now):
                self.logger.info(
                    "Potential stale tip detected, will try using extra outbound "
                    "peer (last tip update: %d seconds ago)",
                    now - self.last_tip_update,
                )
                manager.try_new_outbound_peer = True
            elif manager.try_new_outbound_peer:
                manager.try_new_outbound_peer = False
            self._stale_tip_check_time = now + _STALE_CHECK_INTERVAL
        if self._initial_sync_finished:
            return
        block_index = self.node.chainstate.block_index
        tip = block_index.header_dict[block_index.active_chain[-1]].header
        horizon = now - _POW_TARGET_SPACING * _DIRECT_FETCH_SPACINGS
        if tip.time.timestamp() > horizon:
            manager.start_extra_block_relay_peers = True
            self._initial_sync_finished = True

    def _tip_may_be_stale(self, now: float) -> bool:
        """Core's `TipMayBeStale`: no block for three intervals, none in flight.

        The first call stamps `last_tip_update` where no block has yet
        connected, as Core's does.
        """
        if not self.last_tip_update:
            self.last_tip_update = now
        connections = self.node.p2p_manager.connections.copy().values()
        return (
            self.last_tip_update < now - _POW_TARGET_SPACING * _STALE_TIP_SPACINGS
            and (not any(conn.download_queue for conn in connections))
        )

    def _evict_extra_full_relay_peer(self, now: float) -> None:
        """Drop one full-relay peer past the target, if one may go.

        The full-relay half of Core's `EvictExtraOutboundPeers`: of the
        automatic full-relay peers connected, those neither protected by
        `chain_sync.protect_if_caught_up` nor alone on their network among
        the manual and full-relay ones, the one that least recently
        announced a block, the youngest by connection id on a tie -- once
        connected longer than `_MINIMUM_CONNECT_TIME` and with no block in
        flight from it. Dropping it clears `try_new_outbound_peer` until
        the tip is next found stale.
        """
        manager = self.node.p2p_manager
        peers = [
            conn
            for conn in manager.connections.copy().values()
            if conn.status == P2pConnStatus.Connected
            and conn.automatic
            and not (conn.block_relay or conn.feeler)
        ]
        if len(peers) <= manager.max_outbound_full_relay:
            return
        counts = manager.network_conn_counts()
        worst = None
        for conn in peers:
            # Core's order: `m_protect` first, then the network
            if conn.chain_sync.protect or counts[get_network(conn.address)] <= 1:
                continue
            if worst is None or (conn.last_block_announcement, -conn.id) < (
                worst.last_block_announcement,
                -worst.id,
            ):
                worst = conn
        if worst is None:
            return
        if (
            now - worst.connected_time > _MINIMUM_CONNECT_TIME
            and not worst.download_queue
        ):
            self.logger.log_debug(
                "net",
                "disconnecting extra outbound peer=%s "
                "(last block announcement received at time %s)",
                worst.id,
                worst.last_block_announcement,
            )
            worst.stop()
            manager.try_new_outbound_peer = False

    def _evict_extra_block_relay_peer(self, now: float) -> None:
        """Drop one block-relay-only peer past the target, if one may go.

        The block-relay-only half of Core's `EvictExtraOutboundPeers`:
        of the youngest two such peers, by connection id, the youngest
        goes unless it delivered a novel block more recently than the
        other, which then goes instead -- once connected
        `_MINIMUM_CONNECT_TIME` and with no block in flight from it.
        Core's `ForEachNode` visits a peer done with its handshake and
        not disconnected, which is `Connected` here.
        """
        manager = self.node.p2p_manager
        peers = sorted(
            (
                conn
                for conn in manager.connections.copy().values()
                if conn.block_relay and conn.status == P2pConnStatus.Connected
            ),
            key=lambda conn: conn.id,
        )
        if len(peers) <= manager.max_outbound_block_relay:
            return
        # With no second peer Core's `next_youngest_peer` stays `{-1, 0}`,
        # an id `ForNode` finds no peer under.
        youngest = peers[-1]
        next_youngest = peers[-2] if len(peers) > 1 else None
        next_time = 0 if next_youngest is None else next_youngest.last_novel_block_time
        to_disconnect = youngest
        if youngest.last_novel_block_time > next_time:
            if next_youngest is None:
                return
            to_disconnect = next_youngest
        if (
            now - to_disconnect.connected_time >= _MINIMUM_CONNECT_TIME
            and not to_disconnect.download_queue
        ):
            self.logger.log_debug(
                "net",
                "disconnecting extra block-relay-only peer=%s "
                "(last block received at time %s)",
                to_disconnect.id,
                to_disconnect.last_novel_block_time,
            )
            to_disconnect.stop()

    def tx_download(self) -> None:
        """Announce what this node received, and request what it still wants.

        At any sync state: Core's `SendMessages` (`net_processing.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag) gates neither, and what
        `inv_txs` holds is already gated on IBD by `callbacks.inv`.
        """
        self._queue_announcements_for_received_txs()
        self._send_due_announcements()
        self._request_wanted_txs()

        self.inv_txs = []
        self.received_txs = []

    def _queue_announcements_for_received_txs(self) -> None:
        received = list(dict.fromkeys(wtxid for _, wtxid in self.received_txs))
        if not received:
            return
        # `received` itself stays a list, the arrival order a
        # connection's queue keeps; membership below is against the dict
        # `answers` instead, so a peer with many wtxids still outstanding
        # does not turn one `inv_txs` pass into a full scan of `received`
        # per entry. btclib-org/btclib-node#444
        #
        # A txid announcement (`callbacks.inv`) is asked by txid, so a
        # transaction received answers for its txid as well: `answers`
        # maps either hash to the wtxid. One
        # already evicted again has no txid to read back, and is left
        # to the ask's own timeout.
        answers = {wtxid: wtxid for wtxid in received}
        for wtxid in received:
            held = self.node.mempool.transactions.get(wtxid)
            if held is not None:
                answers.setdefault(held.id, wtxid)
        # a peer that announced a transaction we now hold, or sent it
        # to us, already has it: it is the others that are told. A
        # locally originated transaction's conn_id is `None`, which
        # matches no real connection, so nobody is excluded on its
        # account -- the same as Core's own `RelayTransaction` has
        # nobody to exclude for a transaction it did not receive from
        # a peer.
        has_it: dict[int | None, set[bytes]] = {}
        for conn_id, wtxid in self.received_txs:
            has_it.setdefault(conn_id, set()).add(wtxid)
        still_wanted: list[tuple[int, bytes, bool]] = []
        for conn_id, announced, txid in self.inv_txs:
            if announced in answers:
                has_it.setdefault(conn_id, set()).add(answers[announced])
            else:
                still_wanted.append((conn_id, announced, txid))
        self.inv_txs = still_wanted

        # the tx is in the mempool now: nobody is still to be asked for
        # it, by whichever hash it was announced under, and nobody is
        # owed an answer to a `getdata` already sent.
        for announced_hash in answers:
            self.tx_requests.forget_tx_hash(announced_hash)

        for conn in self.node.p2p_manager.connections.copy().values():
            # what the peer's version asked for. An answer nothing
            # consults is the same peer told the same thing whatever
            # it said, which is what #76 is about, so every send
            # that announces a transaction reads this --
            # `P2pManager.broadcast_raw_transaction` no longer reads
            # it itself, going through this same queue instead.
            # BIP37 is that a peer which sent fRelay false is sent
            # no transaction inventory at all, so it is skipped
            # whole rather than sent a shorter list.
            if not conn.relay_tx:
                continue
            known = has_it.get(conn.id, ())
            # BIP133: the peer's own floor (`conn.feefilter`) is applied
            # when the trickle is sent, as Core does
            # (`InitiateTxBroadcastToAll` queues whatever the peer does not
            # know), so a floor that changes while a transaction waits
            # applies to it. btclib-org/btclib-node#260
            #
            # Membership of the mempool is checked here: eviction
            # (`Mempool._evict_to_limit`) can remove a wtxid this
            # same batch already recorded in `received` before this
            # loop reaches it, another transaction in the same batch
            # having evicted it moments earlier; queuing an
            # announcement for it regardless would be exactly
            # #277/#293's own defect, reached through eviction rather
            # than a full mempool's outright refusal.
            # btclib-org/btclib-node#294
            new_for_conn = [
                wtxid
                for wtxid in received
                if wtxid not in known
                and wtxid in self.node.mempool.transactions
                and self._tx_inventory(conn, wtxid).hash not in conn.known_tx_inventory
            ]
            _extend_tx_announce_queue(conn, new_for_conn)

    def _request_wanted_txs(self) -> None:
        """Track what peers announced, and ask each peer for what is its turn.

        Core's `AddTxAnnouncement` for every announcement in `inv_txs`,
        then `GetRequestsToSend` and the `getdata` of `SendMessages` for
        every peer (`node/txdownloadman_impl.cpp` and
        `net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag). A transaction is asked for of one announcer at a time, and
        of the next only after the request expires or is answered with a
        `notfound`.

        The announcements and orphans of a connection no longer connected
        are forgotten here, where Core's `FinalizeNode` does it.
        """
        connections = self.node.p2p_manager.connections.copy()
        for peer in self.tx_requests.peers():
            if peer not in connections:
                self.tx_requests.disconnected_peer(peer)
        for peer in self.orphanage.peers():
            if peer not in connections:
                self.orphanage.erase_for_peer(peer)
        now = time.time()
        wtxid_peers = sum(conn.wtxidrelay_received for conn in connections.values())
        for conn_id, announced, txid in self.inv_txs:
            conn = connections.get(conn_id)
            if conn is not None:
                self._add_tx_announcement(conn, announced, now, wtxid_peers, txid=txid)
        for conn in connections.values():
            self._send_tx_requests(conn, now)

    def _add_tx_announcement(
        self,
        conn: Connection,
        announced: bytes,
        now: float,
        wtxid_peers: int,
        *,
        txid: bool,
    ) -> None:
        """Track `announced` by `conn`, to be asked for after Core's delays.

        Core's `AddTxAnnouncement` (`node/txdownloadman_impl.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the wtxid of an orphan
        makes `conn` a peer to resolve it with. Otherwise a transaction already
        had, a reconsiderable refusal included, is dropped, and so is an
        announcement of a peer with `MAX_PEER_TX_ANNOUNCEMENTS` tracked,
        unless it holds `RELAY`.
        Otherwise its `reqtime` is delayed by `NONPREF_PEER_TX_DELAY` where
        the peer is not preferred, by `TXID_RELAY_DELAY` where it
        announced a txid while a wtxid-relay peer is connected (a
        wtxid-relay peer's `MSG_WITNESS_TX` included), and by
        `OVERLOADED_PEER_TX_DELAY` where it already has
        `MAX_PEER_TX_REQUEST_IN_FLIGHT` requests outstanding and no
        `RELAY`.
        """
        by_wtxid = not txid
        orphan = self.orphanage.get_tx(announced) if by_wtxid else None
        if orphan is not None:
            self._resolve_orphan_with(conn, orphan, now, wtxid_peers)
            return
        if self.already_have_tx(announced, wtxid=by_wtxid, include_reconsiderable=True):
            return
        relay = NetPermissionFlags.RELAY in conn.permissions
        if not relay and self.tx_requests.count(conn.id) >= _MAX_PEER_TX_ANNOUNCEMENTS:
            return
        preferred = _is_preferred_download(conn)
        delay = 0.0
        if not preferred:
            delay += _NONPREF_PEER_TX_DELAY
        if not by_wtxid and wtxid_peers > 0:
            delay += _TXID_RELAY_DELAY
        if (
            not relay
            and self.tx_requests.count_in_flight(conn.id)
            >= _MAX_PEER_TX_REQUEST_IN_FLIGHT
        ):
            delay += _OVERLOADED_PEER_TX_DELAY
        self.tx_requests.received_inv(
            conn.id, announced, preferred=preferred, reqtime=now + delay, txid=txid
        )

    def _send_tx_requests(self, conn: Connection, now: float) -> None:
        """Send `conn` the `getdata`s for the transactions it is to be asked.

        Core's `GetRequestsToSend` (`node/txdownloadman_impl.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a transaction already
        had is forgotten instead; the others are
        requested until `GETDATA_TX_INTERVAL` has passed. They go in
        `getdata`s of at most `MAX_GETDATA_SZ` items. A parent of an orphan
        is asked for by txid, whatever `conn` relays.

        A transaction queued for a script check off `Node`'s thread is
        asked of no one meanwhile. It stays tracked, so another announcer
        is asked if the check ends with it neither kept nor refused. Core
        needs no such wait, as it checks the scripts while handling the
        `tx` message.
        """
        requestable, _expired = self.tx_requests.get_requestable(conn.id, now)
        tx_checks = self.node.tx_checks
        wanted: list[Inventory] = []
        for announced in requestable:
            wtxid = conn.wtxidrelay_received and not self.tx_requests.is_txid(
                conn.id, announced
            )
            if self.already_have_tx(
                announced, wtxid=wtxid, include_reconsiderable=False
            ):
                self.tx_requests.forget_tx_hash(announced)
                continue
            if tx_checks.pending(announced):
                continue
            wanted.append(Inventory(_tx_fetch_type(conn, wtxid=wtxid), announced))
            self.tx_requests.requested_tx(
                conn.id, announced, now + _GETDATA_TX_INTERVAL
            )
        for start in range(0, len(wanted), _MAX_GETDATA_SZ):
            conn.send(GetData(wanted[start : start + _MAX_GETDATA_SZ]))

    def already_have_tx(
        self, txhash: bytes, *, wtxid: bool, include_reconsiderable: bool
    ) -> bool:
        """Answer whether `txhash` is not to be asked for, as `AlreadyHaveTx`.

        `txhash` is a wtxid where `wtxid` holds and a txid otherwise
        (`src/node/txdownloadman_impl.cpp`, at bitcoin/bitcoin@9be056a8a7, the
        v31.1 tag). The orphanage is asked by wtxid whatever `txhash` is, as
        Core does: a txid is then a wtxid only for a transaction without a
        witness, and cannot be a false positive for one with. A refusal
        `mark_rejected_reconsiderable` holds counts only with
        `include_reconsiderable`.

        Core's filter of recently confirmed transactions is left out, which
        this tree has no counterpart of.
        """
        mempool = self.node.mempool
        if self.orphanage.have_tx(txhash):
            return True
        if include_reconsiderable and mempool.was_recently_rejected_reconsiderable(
            txhash
        ):
            return True
        if mempool.was_recently_rejected(txhash):
            return True
        return txhash in (mempool.transactions if wtxid else mempool.txid_index)

    @staticmethod
    def unique_parents(tx: Tx) -> list[bytes]:
        """Return the txids `tx` spends, each once."""
        # Core sorts a `Txid` by its internal bytes, the reverse of the display
        # bytes this tree keeps
        return sorted({tx_in.prev_out.tx_id for tx_in in tx.vin}, key=lambda t: t[::-1])

    def mempool_rejected_tx(
        self, tx: Tx, error: Exception, conn_id: int, *, first_time: bool
    ) -> tuple[Tx, Tx] | None:
        """Take in what refused `tx` means, Core's `MempoolRejectedTx`.

        `error` is `MissingPrevoutError`, Core's `TX_MISSING_INPUTS`, or
        what else refused it. `first_time` is whether this is the first
        refusal of a transaction a peer sent, and not a transaction taken
        from the orphanage or a package. Answers the parent and child to
        validate as a package, where `tx` was refused for a reason a
        package can undo and `conn_id` sent a child that spends it.
        (`src/node/txdownloadman_impl.cpp`, at bitcoin/bitcoin@9be056a8a7, the
        v31.1 tag.)

        A transaction missing inputs is kept as an orphan on its first
        refusal, unless a parent was refused already (`_keep_orphan`). It is
        recorded as refused nowhere else, as a missing input says nothing of
        the transaction: its parent may arrive. Any other refusal is
        recorded, in the filter a package can undo for `error.reconsiderable`
        and otherwise in `Mempool.mark_rejected`'s, and ends the orphan
        if it was one.

        A first refusal keeps `tx` in `extra_txns`, Core's
        `AddToCompactExtraTransactions`, unless `_keep_orphan` found it
        kept already or it weighs `MAX_EXTRA_TX_WEIGHT` or more. Core also
        keeps there what a replacement evicts, and this mempool replaces
        nothing (btclib-org/btclib-node#1334).

        Left out is what Core does for a witness-stripped refusal and for
        `TX_INPUTS_NOT_STANDARD`, which tell the txid apart from the wtxid
        in the filter: `Mempool.mark_rejected` has the reason this tree does
        not. So a witness-stripped transaction is kept in `extra_txns`,
        where Core keeps none.
        """
        mempool = self.node.mempool
        wtxid = tx.hash
        extra = first_time and tx.weight < MAX_EXTRA_TX_WEIGHT
        if isinstance(error, MissingPrevoutError):
            if first_time and not mempool.was_recently_rejected(wtxid):
                extra &= not self._keep_orphan(tx, conn_id)
            if extra:
                self.extra_txns.append(tx)
            return None
        package = None
        if isinstance(error, TxRejectedError) and error.reconsiderable:
            mempool.mark_rejected_reconsiderable(wtxid)
            if first_time:
                package = self.find_1p1c_package(tx, conn_id)
        else:
            mempool.mark_rejected(wtxid)
        self.tx_requests.forget_tx_hash(wtxid)
        self.orphanage.erase_tx(wtxid)
        if extra:
            self.extra_txns.append(tx)
        return package

    def _keep_orphan(self, tx: Tx, conn_id: int) -> bool:
        """Keep `tx` as an orphan of the peers that can resolve it.

        Core's first-refusal branch of `MempoolRejectedTx`. Not kept where
        a parent is in `Mempool.mark_rejected`'s cache, or where two parents
        are in the cache a package can undo: one parent and one child cannot
        undo two. Both hashes of `tx` are then recorded refused.
        Otherwise the parents not yet had are asked for from `conn_id` and
        from the other peers that announced `tx`
        (`_maybe_add_orphan_resolution_candidate`), and `tx` is kept for each
        that takes it. Answers whether it was kept already there, which
        keeps it out of `extra_txns`.
        """
        mempool = self.node.mempool
        txid, wtxid = tx.id, tx.hash
        unique_parents = self.unique_parents(tx)
        reconsiderable_parent: bytes | None = None
        rejected_parents = False
        for parent_txid in unique_parents:
            if mempool.was_recently_rejected(parent_txid):
                rejected_parents = True
                break
            if (
                mempool.was_recently_rejected_reconsiderable(parent_txid)
                and parent_txid not in mempool.txid_index
            ):
                if reconsiderable_parent is not None:
                    rejected_parents = True
                    break
                reconsiderable_parent = parent_txid
        if rejected_parents:
            # whatever the witness, it is refused: both hashes are recorded.
            # No parent is recorded as known: Core clears `unique_parents`
            # here (`MempoolRejectedTx`, `src/node/txdownloadman_impl.cpp`
            # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
            mempool.mark_rejected(txid)
            mempool.mark_rejected(wtxid)
            kept_already = False
        else:
            kept_already = self.orphanage.have_tx(wtxid)
            unique_parents = [
                parent_txid
                for parent_txid in unique_parents
                if not self.already_have_tx(
                    parent_txid, wtxid=False, include_reconsiderable=False
                )
            ]
            self._add_known_txs(conn_id, unique_parents)
            now = time.time()
            wtxid_peers = self._wtxid_peer_count()
            candidates = [conn_id]
            candidates += self.tx_requests.get_candidate_peers(txid)
            if tx.is_segwit:
                candidates += self.tx_requests.get_candidate_peers(wtxid)
            for peer in candidates:
                if self._maybe_add_orphan_resolution_candidate(
                    unique_parents, wtxid, peer, now, wtxid_peers
                ):
                    self.orphanage.add_tx(tx, peer)
        self.tx_requests.forget_tx_hash(txid)
        self.tx_requests.forget_tx_hash(wtxid)
        return kept_already

    def _add_known_txs(self, conn_id: int, txids: list[bytes]) -> None:
        """Record that `conn_id` has `txids`: it sent a child spending them.

        Core's `AddKnownTx`, which skips a peer no longer connected.
        """
        sender = self.node.p2p_manager.connections.get(conn_id)
        if sender is not None:
            for txid in txids:
                sender.known_tx_inventory.add(txid)

    def _wtxid_peer_count(self) -> int:
        """Return how many peers relay by wtxid, Core's `m_num_wtxid_peers`."""
        connections = self.node.p2p_manager.connections.copy()
        return sum(conn.wtxidrelay_received for conn in connections.values())

    def _resolve_orphan_with(
        self, conn: Connection, orphan: Tx, now: float, wtxid_peers: int
    ) -> None:
        """Make `conn`, which announced `orphan`, a peer to resolve it with.

        The first lines of Core's `AddTxAnnouncement`: the parents `orphan`
        still lacks are asked of `conn` as if it had announced them, and
        `conn` becomes an announcer of `orphan`. Nothing is left to ask
        where every parent is had.
        """
        parents = [
            parent_txid
            for parent_txid in self.unique_parents(orphan)
            if not self.already_have_tx(
                parent_txid, wtxid=False, include_reconsiderable=False
            )
        ]
        if parents and self._maybe_add_orphan_resolution_candidate(
            parents, orphan.hash, conn.id, now, wtxid_peers
        ):
            self.orphanage.add_announcer(orphan.hash, conn.id)

    def _maybe_add_orphan_resolution_candidate(
        self,
        parents: list[bytes],
        wtxid: bytes,
        peer: int,
        now: float,
        wtxid_peers: int,
    ) -> bool:
        """Ask `peer` for the `parents` of an orphan, and say whether it was.

        Core's `MaybeAddOrphanResolutionCandidate`
        (`node/txdownloadman_impl.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag): not a peer that is gone or announced `wtxid` already, nor one
        without `RELAY` that would then have more than
        `MAX_PEER_TX_ANNOUNCEMENTS` tracked. Each parent is announced by txid,
        with the delays `_add_tx_announcement` gives, and the one for
        `TXID_RELAY_DELAY` whenever a wtxid-relay peer is connected, as the
        parent may arrive from that peer sooner than asked.
        """
        conn = self.node.p2p_manager.connections.get(peer)
        if conn is None or self.orphanage.have_tx_from_peer(wtxid, peer):
            return False
        relay = NetPermissionFlags.RELAY in conn.permissions
        if (
            not relay
            and self.tx_requests.count(peer) + len(parents) > _MAX_PEER_TX_ANNOUNCEMENTS
        ):
            return False
        preferred = _is_preferred_download(conn)
        delay = 0.0
        if not preferred:
            delay += _NONPREF_PEER_TX_DELAY
        if wtxid_peers > 0:
            delay += _TXID_RELAY_DELAY
        if (
            not relay
            and self.tx_requests.count_in_flight(peer) >= _MAX_PEER_TX_REQUEST_IN_FLIGHT
        ):
            delay += _OVERLOADED_PEER_TX_DELAY
        for parent_txid in parents:
            self.tx_requests.received_inv(
                peer, parent_txid, preferred=preferred, reqtime=now + delay, txid=True
            )
        return True

    def find_1p1c_package(self, parent: Tx, conn_id: int) -> tuple[Tx, Tx] | None:
        """Find a child of `parent` from `conn_id` to try with it as a package.

        Core's `Find1P1CPackage` (`node/txdownloadman_impl.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), asked of a parent refused
        as `mark_rejected_reconsiderable` records. Only the children of
        `conn_id` count, newest first, so that a flood of fake children does
        not crowd out the real one a peer sent. A child is tried once with
        its parent, and not at all once it was refused itself.
        """
        mempool = self.node.mempool
        for child in self.orphanage.get_children_from_same_peer(parent, conn_id):
            pair = package_hash([parent.hash, child.hash])
            if not mempool.was_recently_rejected_reconsiderable(
                pair
            ) and not mempool.was_recently_rejected(child.id):
                return parent, child
        return None

    def mempool_accepted_tx(self, tx: Tx) -> None:
        """Take in that `tx` was accepted, Core's `MempoolAcceptedTx`.

        Nobody is asked for it any longer, the orphans that spend it are to be
        reconsidered, and it is no orphan itself.
        """
        self.tx_requests.forget_tx_hash(tx.id)
        self.tx_requests.forget_tx_hash(tx.hash)
        self.orphanage.add_children_to_work_set(tx)
        self.orphanage.erase_tx(tx.hash)

    def received_tx_response(self, conn_id: int, txid: bytes, wtxid: bytes) -> None:
        """Complete `conn_id`'s announcement of a transaction it sent.

        Core's `ReceivedTx` (`node/txdownloadman_impl.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), whether or not the
        transaction is then accepted: the peer answered, so it is not
        waited for, and another announcer may be asked.
        """
        self.tx_requests.received_response(conn_id, txid)
        self.tx_requests.received_response(conn_id, wtxid)

    def received_not_found(self, conn_id: int, items: Sequence[Inventory]) -> None:
        """Complete `conn_id`'s announcements of the transactions it lacks.

        Core's `ReceivedNotFound` (`node/txdownloadman_impl.cpp`), which
        `net_processing.cpp` calls with nothing where the message has more
        than `MAX_PEER_TX_ANNOUNCEMENTS` and `MAX_BLOCKS_IN_TRANSIT_PER_PEER`
        items together (both at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
        Block items are left alone, as Core's `IsGenTxMsg` leaves them.
        """
        if len(items) > _MAX_PEER_TX_ANNOUNCEMENTS + MAX_BLOCKS_IN_TRANSIT_PER_PEER:
            return
        for item in items:
            if item.type_code in _TX_INVENTORY_TYPES:
                self.tx_requests.received_response(conn_id, item.hash)

    def _send_due_announcements(self) -> None:
        # Core's `TxRelay::m_next_inv_send_time`/`m_tx_inventory_to_send`
        # (net_processing.cpp, at bitcoin/bitcoin@58a7869f86): each
        # connection is told what is waiting for it only once its own
        # timer comes due, rather than the instant something is queued,
        # so the gap between a `tx` this node receives and the `inv` it
        # sends on carries no information about when that arrival was.
        now = time.time()
        due_conns = []
        for conn in self.node.p2p_manager.connections.copy().values():
            if not conn.relay_tx:
                continue
            due = now >= conn.next_inv_send_time
            # `fSendTrickle` is always true for a `NO_BAN` peer
            if (
                conn.next_inv_send_time
                and now < conn.next_inv_send_time
                and NetPermissionFlags.NO_BAN not in conn.permissions
            ):
                continue
            # Core's trickle records the mempool's sequence whether or
            # not it announces anything
            conn.stats.last_inv_sequence = self.node.mempool.sequence
            due_conns.append((conn, due))
        caps = [_trickle_cap(len(conn.tx_announce_queue)) for conn, _ in due_conns]
        keys, best = self._rank_queued([conn for conn, _ in due_conns], caps)
        for (conn, due), cap in zip(due_conns, caps, strict=True):
            if conn.tx_announce_queue:
                # The cap is Core's, from the queue's size before anything
                # is popped (`m_tx_inventory_to_send.size()`).
                batch = self._pop_trickle(conn, cap, keys, best)
                if batch:
                    # `cap` is at most `_INVENTORY_BROADCAST_MAX`, below
                    # `MAX_INV_SZ`, so one `Inv` always holds a trickle.
                    self._send_trickle(conn, batch)
            # Core redraws the schedule when the timer is due, not for a
            # `NO_BAN` peer announced to ahead of it.
            if due:
                if conn.inbound:
                    conn.next_inv_send_time = self._next_inbound_inv_time(conn, now)
                else:
                    conn.next_inv_send_time = now + _rng.expovariate(
                        1 / _OUTBOUND_TX_ANNOUNCE_INTERVAL
                    )

    def _rank_queued(
        self, conns: list[Connection], caps: list[int]
    ) -> tuple[dict[bytes, tuple[int, int, bytes]], list[bytes]]:
        """Return each queued wtxid's key, and the best of them in order.

        The mempool cannot change within a call, so the queued transactions
        of every due connection are keyed once. An entry the mempool no
        longer holds is found at send time, not trusted from when it was
        queued: a wtxid can sit in a queue for its connection's whole
        schedule, easily longer than the time between two eviction rounds
        (`Mempool._evict_to_limit`). Core's own trickle send does the same
        (`net_processing.cpp`, `m_mempool.info(wtxid)`).
        btclib-org/btclib-node#294

        Peers mostly queue the same transactions, so the best of all of
        them is picked once, with room for the entries a connection drops,
        and each connection reads its own from that.
        """
        queued: dict[bytes, None] = {}
        for conn in conns:
            queued.update(conn.tx_announce_queue)
        keys = dict.fromkeys(queued, _GONE_KEY)
        keys.update(self.node.mempool.mining_order_keys(queued))
        best = heapq.nsmallest(2 * max(caps, default=0), queued, key=keys.__getitem__)
        return keys, best

    def _send_trickle(self, conn: Connection, batch: list[bytes]) -> None:
        """Send `batch` in one `Inv` and record it as known to the peer."""
        inventory = [self._tx_inventory(conn, w) for w in batch]
        conn.send(Inv(inventory))
        for item in inventory:
            conn.known_tx_inventory.add(item.hash)

    def _pop_trickle(
        self,
        conn: Connection,
        cap: int,
        keys: dict[bytes, tuple[int, int, bytes]],
        best: list[bytes],
    ) -> list[bytes]:
        """Pop what one trickle sends from `conn`'s queue, best-paying first.

        Core erases only what it pops, and pops best-paying first
        (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag). It drops an entry the mempool no longer holds, one the peer
        already has, and one below its feefilter, without counting it toward
        the cap. The last two are read when sending, so a change while the
        entry waited applies. What is not popped stays queued.

        `keys` has every wtxid of the queue. `best` is the head, in key
        order, of all the due connections' queued wtxids, so no entry of this
        queue that `best` lacks sorts before one it holds. The queue is read
        through `best`, then by `heapq.nsmallest` over what is left, taking
        twice as many each round, so a long run of dropped entries costs a
        few scans of the queue.
        """
        queue = conn.tx_announce_queue
        batch: list[bytes] = []
        for wtxid in best:
            if len(batch) == cap:
                return batch
            if wtxid in queue:
                del queue[wtxid]
                self._offer(conn, wtxid, batch)
        want = cap
        while queue and len(batch) < cap:
            for wtxid in heapq.nsmallest(want, queue, key=keys.__getitem__):
                if len(batch) == cap:
                    break
                del queue[wtxid]
                self._offer(conn, wtxid, batch)
            want *= 2
        return batch

    def _offer(self, conn: Connection, wtxid: bytes, batch: list[bytes]) -> None:
        """Add `wtxid`, popped from `conn`'s queue, to `batch` if it is sent."""
        mempool = self.node.mempool
        if wtxid not in mempool.transactions:
            return
        known = self._tx_inventory(conn, wtxid).hash in conn.known_tx_inventory
        if not known and mempool.meets_fee_rate(wtxid, conn.feefilter):
            batch.append(wtxid)

    def _tx_inventory(self, conn: Connection, wtxid: bytes) -> Inventory:
        """Name a held transaction the way `conn` relays: wtxid, else txid.

        Core's `SendMessages` announces `MSG_WTX` to a peer with
        `m_wtxid_relay` and `MSG_TX` by txid to any other.
        """
        if conn.wtxidrelay_received:
            return Inventory(InventoryType.MSG_WTX, wtxid)
        return Inventory(InventoryType.MSG_TX, self.node.mempool.txids[wtxid])

    def _consider_evictions(self) -> None:
        """Run Core's `ConsiderEviction` for every connected peer.

        `p2p/chain_sync.py` is where it is argued; Core runs it from
        `SendMessages`, once per peer past its handshake.
        """
        now = time.time()
        for conn in list(self.node.p2p_manager.connections.values()):
            if conn.status == P2pConnStatus.Connected:
                consider_eviction(self.node, conn, now, maybe_send_getheaders)

    def _send_due_sendheaders(self) -> None:
        """Ask a peer, once, for new blocks as headers (BIP130).

        Core's `MaybeSendSendHeaders` (`net_processing.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), reached from its
        per-peer message loop: not before the common version reaches
        `SENDHEADERS_VERSION`, and not before the best block the peer is
        known to have carries more than the minimum chain work, so that
        an initial header sync is not interleaved with announcements.
        """
        node = self.node
        minimum_chain_work = node.config.minimum_chain_work
        for conn in node.p2p_manager.connections.copy().values():
            if conn.status != P2pConnStatus.Connected or conn.sent_sendheaders:
                continue
            if common_version(conn) < SENDHEADERS_VERSION:
                continue
            best_known = conn.block_availability.best_known
            if best_known is None:
                continue
            chainwork = node.chainstate.block_index.chainwork[best_known]
            if chainwork <= minimum_chain_work:
                continue
            conn.send(SendHeaders())
            conn.sent_sendheaders = True

    def _send_due_feefilters(self) -> None:
        """Tell every connected peer this node's own current relay floor.

        Core's own `MaybeSendFeefilter` (`net_processing.cpp`,
        at bitcoin/bitcoin@58a7869f86), reached from its per-peer message
        loop for every peer regardless of what else that pass sent --
        `_send_due_announcements`'s own `conn.relay_tx` gate does not
        apply here, since BIP133's `feefilter` says what this node will
        not send *to* a peer, independent of whether that peer asked to
        be sent transactions at all. `Connection.status` stands in for
        Core's `fSuccessfullyConnected`: a connection still mid-handshake
        has no peer at the other end of `Connection.send` yet.

        A block-relay-only connection is sent none, as Core returns for
        `IsBlockOnlyConn()`: it never announces a transaction to this
        node. A peer holding `FORCE_RELAY` is sent none either, as Core
        returns for it. `-blocksonly` (`m_opts.ignore_incoming_txs`) has
        nothing to read here, being a run mode this tree does not have.

        Core's `GetCommonVersion() < FEEFILTER_VERSION` return is kept:
        a peer that old is sent none.
        """
        now = time.time()
        # Core's own `MaybeSendFeefilter` (`net_processing.cpp`,
        # at bitcoin/bitcoin@ca7162cde5) gates this same decision on
        # `m_chainman.IsInitialBlockDownload()`, which `main.
        # update_ibd_status`'s own `node.is_initial_block_download`
        # answers faithfully (btclib-org/btclib-node#575). An earlier
        # version of this branch read `NodeStatus.BlockSynced` instead,
        # a second, looser definition of the one Core concept living in
        # this tree -- caught unsafe against `is_initial_block_download`
        # only because the suite's own regtest fixtures dated every
        # block `GENESIS_TIME`-relative, far older than `MAX_TIP_AGE`
        # ever tolerates, which is a fixture defect and not a reason to
        # carry two answers to Core's one flag: closed by giving the one
        # caller that needs a recent tip a recent one
        # (`tests/__init__.py`'s own `generate_random_chain`, `tip_time`)
        # rather than reading `NodeStatus` here (btclib-org/btclib-node#661).
        ibd = self.node.is_initial_block_download
        current_filter = (
            self._max_feefilter
            if ibd
            else self.node.mempool.get_min_fee_rate().sats_per_kvbyte
        )
        min_relay_feerate = self.node.config.min_relay_feerate.sats_per_kvbyte

        for conn in self.node.p2p_manager.connections.copy().values():
            if conn.status != P2pConnStatus.Connected or conn.block_relay:
                continue
            if common_version(conn) < FEEFILTER_VERSION:
                continue
            # a peer holding `FORCE_RELAY` is not filtered
            if NetPermissionFlags.FORCE_RELAY in conn.permissions:
                continue
            # Once this node is done with IBD, a peer sitting on the
            # `_max_feefilter` this branch sent it during IBD is not
            # left there until its own ordinary schedule comes due --
            # Core's own `if (peer.m_fee_filter_sent == MAX_FILTER)
            # peer.m_next_send_feefilter = 0us`.
            if not ibd and conn.feefilter_sent == self._max_feefilter:
                conn.next_feefilter_send_time = 0.0

            if now > conn.next_feefilter_send_time:
                filter_to_send = (
                    self._max_feefilter
                    if ibd
                    else _round_fee_filter(current_filter, self._fee_filter_buckets)
                )
                # This node's own outgoing filter never asks a peer to
                # withhold what BIP133's own floor already relays.
                filter_to_send = max(filter_to_send, min_relay_feerate)
                if filter_to_send != conn.feefilter_sent:
                    conn.send(FeeFilter(filter_to_send))
                    conn.feefilter_sent = filter_to_send
                conn.next_feefilter_send_time = now + _rng.expovariate(
                    1 / _AVG_FEEFILTER_BROADCAST_INTERVAL
                )
            elif now + _MAX_FEEFILTER_CHANGE_DELAY < conn.next_feefilter_send_time and (
                current_filter < 3 * conn.feefilter_sent // 4
                or current_filter > 4 * conn.feefilter_sent // 3
            ):
                # The unrounded rate has moved far enough from what this
                # peer was last sent that waiting for the ordinary,
                # several-minutes-average schedule would leave it
                # filtering on a stale floor for too long -- pulled
                # forward to within `_MAX_FEEFILTER_CHANGE_DELAY` rather
                # than sent immediately, so the move itself is not
                # timestamped exactly either. `//` rather than `/`:
                # Core's own comparison (`net_processing.cpp:5859`) is
                # over `CAmount`, `int64_t`, so `3 * m_fee_filter_sent /
                # 4` is truncating integer division there too, not an
                # approximation this module tightens by keeping the
                # remainder.
                conn.next_feefilter_send_time = now + _rng.uniform(
                    0, _MAX_FEEFILTER_CHANGE_DELAY
                )

    def _next_inbound_inv_time(self, conn: Connection, now: float) -> float:
        """Return the schedule this connection's net class currently shares.

        `NextInvToInbounds` (net_processing.cpp, the same commit): redrawn
        only once the class's own timer has already passed, so every
        inbound connection consulting it before the next redraw is handed
        the same value -- an outbound connection never calls this, having
        its own independent draw instead, `_send_due_announcements`'s
        other branch.
        """
        net_class = _inbound_net_class(conn)
        due = self._next_inv_to_inbounds.get(net_class, 0.0)
        if due < now:
            due = now + _rng.expovariate(1 / _INBOUND_TX_ANNOUNCE_INTERVAL)
            self._next_inv_to_inbounds[net_class] = due
        return due

    def _forget_peers_gone(self) -> None:
        """Drop the header-sync state of every peer no longer connected.

        Core's `m_inv_triggered_getheaders_before_sync` and
        `m_last_getheaders_timestamp` live as long as the peer does.
        """
        connections = self.node.p2p_manager.connections
        self.inv_triggered_getheaders.intersection_update(list(connections))
        last_getheaders = self.last_getheaders_timestamps
        for conn_id in last_getheaders.keys() - connections.keys():
            del last_getheaders[conn_id]

    def sync_headers(self) -> None:
        """Ask one peer for headers, or every peer once the best one is recent.

        Core's own "Start block sync" and "Check for headers sync
        timeouts" in `SendMessages` (`net_processing.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag). While the best
        header is older than `_RECENT_BEST_HEADER`, one peer at a time
        is asked: the first one reached, though one not
        `_is_preferred_download` waits while a preferred peer exists and
        a block is in flight. That peer is dropped once its own
        `headers_sync_timeouts` entry passes, if it is still the only
        one asked and another preferred peer could take its place. Once
        the best header is recent every peer that can serve blocks is
        asked, and the timeout is switched off.

        Core's `nSyncStarted` is a counter `FinalizeNode` decrements;
        here it is the size of `headers_sync_timeouts` once every entry
        for a connection no longer connected is dropped, which is where
        a peer that disconnects hands its turn on. Core's
        `LoadingBlocks()` gate has nothing to read: this tree does not
        import blocks from disk.

        A peer with a `getheaders` already in flight is not asked and
        takes no turn, Core setting `fSyncStarted` only where
        `MaybeSendGetHeaders` sent; it is left for a later pass.

        The locator starts at the best header's parent, as Core's does,
        so that a peer already at this node's tip answers with that tip
        rather than with nothing.
        """
        node = self.node
        connections = [
            conn
            for conn in list(node.p2p_manager.connections.values())
            if conn.status == P2pConnStatus.Connected
        ]
        block_index = node.chainstate.block_index
        best_header = block_index.get_block_info(block_index.header_index[-1]).header
        # Core's `pindexStart->pprev`, where there is one: genesis has none
        header_index = block_index.header_index
        start = header_index[-2] if len(header_index) > 1 else header_index[-1]
        now = time.time()
        best_header_age = now - best_header.time.timestamp()
        recent = best_header_age < _RECENT_BEST_HEADER
        preferred = sum(_is_preferred_download(conn) for conn in connections)
        blocks_in_flight = any(conn.download_queue for conn in connections)
        timeouts = self.headers_sync_timeouts
        for conn_id in timeouts.keys() - {conn.id for conn in connections}:
            del timeouts[conn_id]
        self._forget_peers_gone()
        sync_started = len(timeouts)
        for conn in connections:
            if conn.id in timeouts or not _can_serve_blocks(conn):
                continue
            # Core's `sync_blocks_and_headers_from_peer`: a peer that is
            # not preferred is still one to sync from where there is no
            # preferred peer, or no block in flight from anybody.
            from_peer = _is_sync_peer(
                conn, preferred, blocks_in_flight=blocks_in_flight
            )
            if (sync_started == 0 and from_peer) or recent:
                locator = block_index.get_block_locator_hashes(start)
                if not maybe_send_getheaders(node, conn, locator):
                    continue
                timeouts[conn.id] = (
                    now
                    + _HEADERS_DOWNLOAD_TIMEOUT_BASE
                    + _HEADERS_DOWNLOAD_TIMEOUT_PER_HEADER
                    * best_header_age
                    / _POW_TARGET_SPACING
                )
                sync_started += 1
        for conn in connections:
            if timeouts.get(conn.id, math.inf) == math.inf:
                continue
            if recent:
                timeouts[conn.id] = math.inf
            elif (
                now > timeouts[conn.id]
                and sync_started == 1
                and preferred - _is_preferred_download(conn) >= 1
            ) and self._end_stalled_headers_sync(conn):
                sync_started -= 1

    def _end_stalled_headers_sync(self, conn: Connection) -> bool:
        """End `conn`'s stalled headers sync, answering whether it was reset.

        A peer holding `NO_BAN` is not disconnected: as in Core, its sync
        state is reset, so that another peer may be tried and this one
        asked again.
        """
        if NetPermissionFlags.NO_BAN in conn.permissions:
            self.logger.info(
                "Timeout downloading headers from noban connection %s,"
                " not disconnecting",
                conn.id,
            )
            del self.headers_sync_timeouts[conn.id]
            return True
        self.logger.info(
            "Timeout downloading headers, disconnecting connection %s", conn.id
        )
        conn.stop()
        return False

    def block_connected(self) -> None:
        """Bring the stalling timeout back towards its default, a block on.

        Core's `BlockConnected` (`net_processing.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): 85% of it, in whole
        seconds, and never under `_BLOCK_STALLING_TIMEOUT_DEFAULT`.
        """
        self.block_stalling_timeout = max(
            int(self.block_stalling_timeout * 0.85), _BLOCK_STALLING_TIMEOUT_DEFAULT
        )

    def block_download(self) -> None:
        """Drop the peers stalling the download, and ask each for blocks.

        Core's "Detect whether we're stalling", its block download
        timeout and its "Message: getdata (blocks)" in `SendMessages`
        (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag), peer by peer. A peer is dropped where it has held up the
        download window for longer than `block_stalling_timeout`, which
        then doubles up to `_BLOCK_STALLING_TIMEOUT_MAX`; or where the
        block at the front of its queue has been awaited for longer than
        `_POW_TARGET_SPACING` times `_BLOCK_DOWNLOAD_TIMEOUT_BASE`, plus
        `_BLOCK_DOWNLOAD_TIMEOUT_PER_PEER` for each other peer with a
        block in flight.

        A peer is asked for blocks where it can serve them, has fewer
        than `MAX_BLOCKS_IN_TRANSIT_PER_PEER` in flight, and either this
        node is out of initial block download or the peer is not limited
        and is one `sync_headers` would sync from. What it is asked for
        is `find_next_blocks_to_download`'s choice, on the peer's own
        best known chain; where that leaves the peer with nothing in
        flight, the peer it waits on is marked as stalling it.
        """
        node = self.node
        connections = [
            conn
            for conn in list(node.p2p_manager.connections.values())
            if conn.status == P2pConnStatus.Connected
        ]
        # Core's `mapBlocksInFlight`: the walk passes over what is in
        # flight, and waits on the peer a block was first asked of
        in_flight = first_in_flight(connections)
        downloading_from = sum(bool(conn.download_queue) for conn in connections)
        preferred = sum(_is_preferred_download(conn) for conn in connections)
        by_id = {conn.id: conn for conn in connections}
        now = time.time()
        for conn in connections:
            if self._stalling_or_timed_out(conn, now, downloading_from):
                conn.stop()
                continue
            from_peer = _is_sync_peer(conn, preferred, blocks_in_flight=bool(in_flight))
            if not (
                _can_serve_blocks(conn)
                and (
                    (from_peer and not _is_limited_peer(conn))
                    or not node.is_initial_block_download
                )
                and len(conn.download_queue) < MAX_BLOCKS_IN_TRANSIT_PER_PEER
            ):
                continue
            blocks, staller = find_next_blocks_to_download(
                node.chainstate.block_index,
                conn.block_availability,
                MAX_BLOCKS_IN_TRANSIT_PER_PEER - len(conn.download_queue),
                node.config.minimum_chain_work,
                node.chain.consensus.segwit_height,
                in_flight=in_flight,
                peer_id=conn.id,
                limited=_is_limited_peer(conn),
                can_serve_witnesses=_can_serve_witnesses(conn),
            )
            if blocks:
                downloading_from += not conn.download_queue
                self._request_blocks(conn, blocks, now, compact=False)
                in_flight.update(dict.fromkeys(blocks, conn.id))
            elif not conn.download_queue and staller in by_id:
                stalling = by_id[staller].block_availability
                stalling.stalling_since = stalling.stalling_since or now

    def headers_direct_fetch(self, conn: Connection, last_header: bytes) -> None:
        """Ask `conn` at once for the blocks up to a header it just sent.

        Core's `HeadersDirectFetchBlocks` (`net_processing.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which its `headers`
        handler calls for a batch that connected: only where the active
        tip is less than twenty block intervals old (`CanDirectFetch`),
        and `last_header` is not invalid and has at least the tip's work.
        The blocks from the active chain up to `last_header` that are
        neither held nor in flight from any peer are asked for, earliest
        first, up to `conn`'s room in flight. The walk back stops once
        more than `MAX_BLOCKS_IN_TRANSIT_PER_PEER` blocks are collected,
        as Core bounds `vToFetch`; where it has not reached the active
        chain by then, nothing is asked, and `block_download` is left
        to it.

        Core also leaves out a block at or past `segwit_height` where
        `conn` cannot serve witnesses -- `DeploymentActiveAt(*pindexWalk,
        ..., DEPLOYMENT_SEGWIT) || CanServeWitnesses(peer)`, the same
        test `find_next_blocks_to_download` makes.

        A single block is asked for as `MSG_CMPCT_BLOCK` where `conn` sent
        a `sendcmpct` of version 2, no other block is in flight, and the
        parent of `last_header` was validated.
        """
        node = self.node
        block_index = node.chainstate.block_index
        active_chain = block_index.active_chain
        header_dict = block_index.header_dict
        chainwork = block_index.chainwork
        tip = active_chain[-1]
        if (
            block_time(header_dict[tip].header)
            <= time.time() - _POW_TARGET_SPACING * _DIRECT_FETCH_SPACINGS
            or header_dict[last_header].status == BlockStatus.invalid
            or chainwork[tip] > chainwork[last_header]
        ):
            return

        def on_the_active_chain(block_hash: bytes) -> bool:
            height = header_dict[block_hash].index
            return height < len(active_chain) and active_chain[height] == block_hash

        segwit_height = node.chain.consensus.segwit_height
        can_serve_witnesses = _can_serve_witnesses(conn)
        in_flight = {
            block_hash
            for peer in list(node.p2p_manager.connections.values())
            for block_hash in peer.download_queue
        }
        to_fetch: list[bytes] = []
        walk = last_header
        while (
            not on_the_active_chain(walk)
            and len(to_fetch) <= MAX_BLOCKS_IN_TRANSIT_PER_PEER
        ):
            block_info = header_dict[walk]
            if (
                not block_info.downloaded
                and walk not in in_flight
                and (can_serve_witnesses or block_info.index < segwit_height)
            ):
                to_fetch.append(walk)
            walk = block_info.header.previous_block_hash
        if not on_the_active_chain(walk):
            self.logger.log_debug(
                "net",
                "Large reorg, won't direct fetch to %s (%d)",
                last_header.hex(),
                header_dict[last_header].index,
            )
            return
        room = MAX_BLOCKS_IN_TRANSIT_PER_PEER - len(conn.download_queue)
        blocks = to_fetch[::-1][: max(room, 0)]
        if not blocks:
            return
        # Core's `BLOCK_VALID_CHAIN`, which a block reaches once connected
        parent = header_dict[header_dict[last_header].header.previous_block_hash]
        compact = (
            conn.provides_cmpctblocks
            and len(blocks) == 1
            and not in_flight
            and parent.status in (BlockStatus.valid, BlockStatus.in_active_chain)
        )
        self._request_blocks(conn, blocks, time.time(), compact=compact)
        if len(blocks) > 1:
            self.logger.log_debug(
                "net",
                "Downloading blocks toward %s (%d) via headers direct fetch",
                last_header.hex(),
                header_dict[last_header].index,
            )

    def _stalling_or_timed_out(
        self, conn: Connection, now: float, downloading_from: int
    ) -> bool:
        """Whether `conn` is to be dropped for holding up block download."""
        state = conn.block_availability
        timeout = self.block_stalling_timeout
        if state.stalling_since and state.stalling_since < now - timeout:
            self.logger.info(
                "Peer is stalling block download, disconnecting connection %s",
                conn.id,
            )
            self.block_stalling_timeout = min(2 * timeout, _BLOCK_STALLING_TIMEOUT_MAX)
            return True
        if conn.download_queue and now > state.downloading_since + (
            _POW_TARGET_SPACING
            * (
                _BLOCK_DOWNLOAD_TIMEOUT_BASE
                + _BLOCK_DOWNLOAD_TIMEOUT_PER_PEER * (downloading_from - 1)
            )
        ):
            self.logger.info(
                "Timeout downloading block %s, disconnecting connection %s",
                conn.download_queue[0].hex(),
                conn.id,
            )
            return True
        return False

    def _request_blocks(
        self, conn: Connection, blocks: list[bytes], now: float, *, compact: bool
    ) -> None:
        """Ask `conn` for `blocks`, queued as Core's `BlockRequested` queues.

        The front of an empty queue is awaited from `now`. Asked as
        `block_inventory_type` says, or as `MSG_CMPCT_BLOCK` where
        `compact`.
        """
        if not conn.download_queue:
            conn.block_availability.downloading_since = now
        conn.download_queue.extend(blocks)
        # a block asked for is a block coming back for `update_chain` to
        # validate, the earliest point the worker pool is certain to be
        # wanted: btclib-org/btclib-node#262
        self.node.warm_worker_pool()
        fetch_type = (
            InventoryType.MSG_CMPCT_BLOCK if compact else block_inventory_type(conn)
        )
        conn.send(GetData([Inventory(fetch_type, block_hash) for block_hash in blocks]))
