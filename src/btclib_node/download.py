# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`DownloadManager`, what decides what this node asks its peers for.

Which peer headers are synced from; block download candidates and stall
detection, transaction announcement and request tracking, and the
trickle timing behind both -- `feefilter` resends, address relay, and
the exponential delays that keep two peers from being told the same
thing in lockstep. Most of the constants here are a named Bitcoin Core
constant carried over with the commit it was read at beside it, per
this tree's own convention of matching Core's behaviour, always.
"""

import math
import time
from bisect import bisect_left
from collections import Counter
from random import SystemRandom
from typing import TYPE_CHECKING

from btclib.p2p.address import ServiceFlags
from btclib.p2p.inventory import GetData, Inv, Inventory, InventoryType
from btclib.p2p.limits import MAX_INV_SZ
from btclib.p2p.negotiation import FeeFilter, SendHeaders

from btclib_node.chainstate.block_index import MAX_DOWNLOAD_WINDOW
from btclib_node.constants import MIN_BLOCKS_TO_KEEP, NodeStatus, P2pConnStatus
from btclib_node.p2p.block_availability import update_last_common_block
from btclib_node.p2p.callbacks import (
    MAX_GETDATA_INFLIGHT_BYTES,
    maybe_send_getheaders,
)
from btclib_node.p2p.protocol_version import (
    FEEFILTER_VERSION,
    SENDHEADERS_VERSION,
    common_version,
)

if TYPE_CHECKING:
    from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2

    from btclib_node import Node
    from btclib_node.log import Logger
    from btclib_node.p2p.connection import Connection

__all__ = ["DownloadManager"]

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

# Core's own `GETDATA_TX_INTERVAL` (`node/txdownloadman.h`, 60s): the
# `TxRequestTracker` bound on how long a `getdata` a peer has not
# answered still holds that peer's slot before another candidate is
# tried. `Connection.tx_requested` here is a simpler, per-connection-only
# table with no second candidate to fall back to, but the same problem
# applies to it: with no expiry at all, a peer that neither answers nor
# sends `notfound` blocks this node from ever asking it again for that
# hash, permanently, since `tx_download`'s own `wanted` filter reads the
# entry as still outstanding. Reusing Core's own bound rather than a
# fresh one is a lower-risk choice, not evidence the two trackers behave
# alike beyond this one number. btclib-org/btclib-node#289
_TX_REQUEST_TIMEOUT = 60.0

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

# `block_download`'s own two marks on a peer that has gone quiet
# mid-sync, not Core's `BLOCK_STALLING_TIMEOUT_DEFAULT` (2s, adaptive up
# to `BLOCK_STALLING_TIMEOUT_MAX`'s 64s, `net_processing.cpp`,
# aed80c7395) -- this tree's own coarser pair instead, checked against
# `last_block_timestamp` rather than a single in-flight request: no
# block in `_BLOCK_STALL_EVICTION_TIMEOUT` empties this peer's queue and
# excludes it from new work, and no block in
# `_BLOCK_STALL_DISCONNECT_TIMEOUT` drops the connection outright.
_BLOCK_STALL_EVICTION_TIMEOUT = 120
_BLOCK_STALL_DISCONNECT_TIMEOUT = 300

# `sync_headers`'s own timing, in seconds: `HEADERS_DOWNLOAD_TIMEOUT_BASE`
# and `HEADERS_DOWNLOAD_TIMEOUT_PER_HEADER` (`net_processing.cpp`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag), the second scaled by the
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

# a block hash already queued to this many connections is left for one
# of them to answer before being handed to yet another -- redundant
# requests bound rather than eliminated, since a slow or lying peer is
# what the redundancy is for
_MAX_CONCURRENT_REQUESTS_PER_BLOCK = 3

# How many blocks `_request_new_block_work` below batches into one
# outgoing `GetData` at a time, matching Core's own
# `MAX_BLOCKS_IN_TRANSIT_PER_PEER` (`net_processing.cpp:133`,
# at bitcoin/bitcoin@b91d983f66). Public rather than this module's usual
# underscore-prefixed constants: `p2p/connection.py`'s own
# `MAX_QUEUED_SEND_BYTES` used to be sized from this fact and no longer
# is, but the fact itself -- the size of the answer a well-behaved peer
# sends back to one such request -- outlives whichever bound was last
# sized from it, and is the natural thing a receive-side bound would
# want if this tree grows one; nothing in this tree does yet.
MAX_BLOCKS_PER_GETDATA_BURST = 16


def _fee_filter_buckets(min_relay_feerate: int) -> list[float]:
    """Return the sat/kvB boundaries `_round_fee_filter` may round to.

    Core's own `MakeFeeSet` (`policy/fees/block_policy_estimator.cpp`,
    at bitcoin/bitcoin@58a7869f86): zero, then a geometric series from half
    `min_relay_feerate` (never under 1) up to `_MAX_FILTER_FEERATE`,
    spaced by `_FEE_FILTER_SPACING`. Kept as `float` and not rounded
    here: Core's own `std::set<double>` holds the raw boundary too, and
    only the value `_round_fee_filter` finally selects is ever
    truncated (`static_cast<CAmount>`) -- rounding a boundary to build
    this set would select a different sat/kvB than Core does for a
    boundary that was never an integer to begin with, 137.7 truncating
    to 137 there against rounding to 138 here. Built once, from
    `Config.min_relay_feerate`, rather than a module constant, since
    that field is configurable and Core's own equivalent --
    `-minrelaytxfee`, not the incremental fee `mempool.py`'s own
    constant of the same default value is -- is what this set is keyed
    to (`m_fee_filter_rounder{CFeeRate{DEFAULT_MIN_RELAY_TX_FEE}, ...}`,
    `net_processing.cpp`).
    """
    buckets = {0.0}
    boundary = float(max(1, min_relay_feerate // 2))
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


def _inbound_net_class(address: NetworkAddressV2) -> BIP155Network | int:
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
    is enough of a stand-in for Core's hash: there is nothing here for
    Core's Tor, I2P, CJDNS or bind-address component to do.
    """
    return address.network_id


def _can_serve_blocks(conn: Connection) -> bool:
    """Whether `conn` can serve this node blocks at all.

    Core's own `CanServeBlocks` (net_processing.cpp:1254, at
    bitcoin/bitcoin@ca7162cde5): `NODE_NETWORK` or `NODE_NETWORK_LIMITED`
    advertised, either being enough. Core applies it at the call site of
    `FindNextBlocksToDownload` (net_processing.cpp:6495) -- the same
    point `_request_new_block_work` below applies it, gating a
    connection out of block work entirely rather than folding it into
    `_reachable_blocks`' own per-candidate filter, which is Core's own
    separation too: `CanServeBlocks` and `IsLimitedPeer` are two
    different questions there, "can this peer serve blocks at all" and
    "which ones", and `FindNextBlocks`
    (net_processing.cpp:1575-1645) only asks the second once the first
    has already passed. `True` for a connection with no
    `version_message` rather than a raised `AttributeError` or a `False`
    that would newly restrict it: `_is_limited_peer` below reads the
    same absence permissively, `False` there meaning "not limited" and
    so unrestricted, and the two gates read the same missing state the
    same direction for the same reason -- every connection
    `_request_new_block_work` below ever sees has a `version_message` by
    the time it is promoted, `callbacks.verack` guaranteeing it, so this
    branch is only ever exercised defensively and never in place of the
    real check. btclib-org/btclib-node#725
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

    Core's own `IsLimitedPeer` (net_processing.cpp:1261, at
    bitcoin/bitcoin@ca7162cde5): `NODE_NETWORK_LIMITED` advertised and
    `NODE_NETWORK` not -- a full archival peer sets both, so this misses
    it, and so does a peer with neither, which is `_can_serve_blocks`
    above's own question rather than this one: this tree used to have
    no counterpart to Core's separate `CanServeBlocks` gate, offering
    such a peer the whole download window as if it were archival
    (issue #725, fixed in `_request_new_block_work` below). `False` for
    a connection with no `version_message` rather than a raised
    `AttributeError`: every connection `_request_new_block_work` below
    ever sees has one, `callbacks.verack` refusing to promote one that
    does not, but that invariant lives in `p2p/callbacks.py` and not
    here, so this reads defensively rather than asserting it.
    """
    version_msg = conn.version_message
    if version_msg is None:
        return False
    services = version_msg.services
    return bool(
        services & ServiceFlags.NODE_NETWORK_LIMITED
        and not services & ServiceFlags.NODE_NETWORK
    )


def _is_preferred_download(conn: Connection) -> bool:
    """Whether `conn` is a peer headers and blocks are preferably synced from.

    Core's own `fPreferredDownload` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): an outbound peer that
    can serve blocks and is no `-seednode` one, Core's `ADDR_FETCH`.
    Core's other term has nothing to read here: a `NoBan` inbound peer
    counts as preferred, and this tree grants no permission.
    """
    return not conn.inbound and not conn.addr_fetch and _can_serve_blocks(conn)


def _syncs_from(conn: Connection, *, preferred: int, blocks_in_flight: bool) -> bool:
    """Core's `sync_blocks_and_headers_from_peer` (`SendMessages`, same sha).

    A preferred peer, or any but a `-seednode` one where there is no
    preferred peer, or no block in flight from anybody. Core's own
    `CanServeBlocks` term is asked by each caller before this.
    """
    if _is_preferred_download(conn):
        return True
    return not conn.addr_fetch and (not preferred or not blocks_in_flight)


def _tx_fetch_type(conn: Connection) -> InventoryType:
    """Return the type a `getdata` asks `conn` for a transaction under.

    Core's `SendMessages` (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag): `MSG_WTX` of a peer with `m_wtxid_relay`, else `MSG_TX`
    with `GetFetchFlags`' witness flag where the peer offers `NODE_WITNESS`.
    """
    if conn.wtxidrelay_received:
        return InventoryType.MSG_WTX
    version_message = conn.version_message
    if version_message and version_message.services & ServiceFlags.NODE_WITNESS:
        return InventoryType.MSG_WITNESS_TX
    return InventoryType.MSG_TX


def _extend_tx_announce_queue(conn: Connection, new_for_conn: list[bytes]) -> None:
    """Append `new_for_conn`'s own wtxids not already in `conn`'s queue.

    `tx_announce_queue` stays the `list[bytes]` `connection.py` declares
    it and `_send_due_announcements` drains in the order it is appended
    in; `queued` is local and rebuilt on every call, only so that
    membership below is not a scan of the whole queue for every wtxid a
    connection is newly offered. btclib-org/btclib-node#444

    `queued.add(wtxid)` keeps `queued` correct for the rest of this call
    even though `new_for_conn` cannot itself repeat a wtxid today -- its
    caller builds it from `received`, deduplicated further up -- so this
    loop stays right if that upstream guarantee ever stops holding,
    rather than depending on it silently.
    """
    if not new_for_conn:
        return
    queued = set(conn.tx_announce_queue)
    for wtxid in new_for_conn:
        if wtxid not in queued:
            conn.tx_announce_queue.append(wtxid)
            queued.add(wtxid)


class DownloadManager:
    """What decides what this node asks its peers for, one `step` at a time.

    Which peer headers are synced from, block download candidates and
    stall detection, transaction announcement and request tracking, and
    the `feefilter` trickle: the module docstring above is where the
    constants each of those follows are argued against Core's own.
    """

    def __init__(self, node: Node, logger: Logger) -> None:
        """Build the schedules and fee-filter buckets `step` reads from."""
        self.node = node
        self.logger = logger

        self.block_window: list[bytes] = []

        # conn_id is `None` for a transaction this node originated
        # (`P2pManager.broadcast_raw_transaction`) rather than received
        # from a peer -- the same list either way, so the peer an inv
        # goes out to cannot tell a relayed transaction from this node's
        # own by which path carried it. btclib-org/btclib-node#141
        self.received_txs: list[tuple[int | None, bytes]] = []
        self.inv_txs: list[tuple[int, bytes]] = []

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
            node.config.min_relay_feerate.sats_per_kvbyte
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

    def step(self) -> None:
        """Run one pass.

        Headers, blocks and txs are asked for, and sendheaders and
        feefilters sent.
        """
        self.sync_headers()
        self.update_last_common_blocks()
        self.block_download()
        self.tx_download()
        self._send_due_sendheaders()
        self._send_due_feefilters()

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
        # `received` itself stays a list, for the order `_send_due_
        # announcements` sends in; membership below is against the dict
        # `answers` instead, so a peer with many wtxids still outstanding
        # does not turn one `inv_txs` pass into a full scan of `received`
        # per entry. btclib-org/btclib-node#444
        #
        # A peer without wtxid relay announces and is asked by txid
        # (`callbacks.inv`), so a transaction received answers for its
        # txid as well: `answers` maps either hash to the wtxid. One
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
        still_wanted: list[tuple[int, bytes]] = []
        for conn_id, announced in self.inv_txs:
            if announced in answers:
                has_it.setdefault(conn_id, set()).add(answers[announced])
            else:
                still_wanted.append((conn_id, announced))
        self.inv_txs = still_wanted

        for conn in self.node.p2p_manager.connections.copy().values():
            # the tx is in the mempool now: nobody is still owed an
            # answer to a `getdata` this node already sent for it,
            # by whichever hash the request loop below asked it by.
            for asked in answers:
                conn.tx_requested.pop(asked, None)

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
            # BIP133: a peer told this node its own floor
            # (callbacks.feefilter, `conn.feefilter`) is not queued
            # a transaction below it either -- checked once here,
            # against the mempool's own record of what the
            # transaction paid, rather than re-checked on every
            # `_send_due_announcements` drain of an unchanging queue.
            # btclib-org/btclib-node#260
            #
            # `wtxid in self.node.mempool.transactions` is checked
            # here too, and not left to `meets_fee_rate` alone: that
            # method reads a wtxid it holds no fee for as clearing
            # every rate, which is right for its own purpose -- a
            # wtxid already relayed out of `Mempool.add_tx`'s own
            # default -- and wrong for this one. Eviction
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
                and self.node.mempool.meets_fee_rate(wtxid, conn.feefilter)
            ]
            _extend_tx_announce_queue(conn, new_for_conn)

    def _request_wanted_txs(self) -> None:
        if not self.inv_txs:
            return
        invs: dict[int, list[bytes]] = {}
        for conn_id, announced in self.inv_txs:
            invs.setdefault(conn_id, []).append(announced)

        for conn_id, inv in invs.items():
            target = self.node.p2p_manager.connections.get(conn_id)
            if not target:
                continue
            now = time.time()
            # an ask outstanding longer than a peer could plausibly
            # still be about to answer is no longer treated as
            # outstanding: a peer that neither sends the transaction
            # nor answers `notfound` would otherwise block every
            # future request to it for this hash, permanently.
            # btclib-org/btclib-node#289
            for announced, asked_at in list(target.tx_requested.items()):
                if now - asked_at > _TX_REQUEST_TIMEOUT:
                    del target.tx_requested[announced]
            # a peer that announced the same transaction twice is
            # asked for it once, and a peer already asked for a
            # transaction is not asked again while that ask is still
            # outstanding: `not_found` is what clears it early, the
            # tx itself arriving is what clears it above.
            wanted = [
                announced
                for announced in dict.fromkeys(inv)
                if announced not in target.tx_requested
            ]
            if not wanted:
                continue
            for announced in wanted:
                target.tx_requested[announced] = now
            # Core's `TXID_RELAY_DELAY` for a txid announcement is one of
            # `TxRequestTracker`'s delays, none of which this node applies
            # (btclib-org/btclib-node#1196).
            fetch_type = _tx_fetch_type(target)
            target.send(GetData([Inventory(fetch_type, h) for h in wanted]))

    def _send_due_announcements(self) -> None:
        # Core's `TxRelay::m_next_inv_send_time`/`m_tx_inventory_to_send`
        # (net_processing.cpp, at bitcoin/bitcoin@58a7869f86): each
        # connection is told what is waiting for it only once its own
        # timer comes due, rather than the instant something is queued,
        # so the gap between a `tx` this node receives and the `inv` it
        # sends on carries no information about when that arrival was.
        now = time.time()
        for conn in self.node.p2p_manager.connections.copy().values():
            if not conn.relay_tx:
                continue
            if conn.next_inv_send_time and now < conn.next_inv_send_time:
                continue
            # Core's trickle records the mempool's sequence whether or
            # not it announces anything
            conn.stats.last_inv_sequence = self.node.mempool.sequence
            if conn.tx_announce_queue:
                # `Inv.assert_valid` (btclib.p2p.inventory) refuses more
                # than `MAX_INV_SZ` entries, and this queue has had this
                # connection's whole schedule -- a mean of several
                # seconds, an exponential draw's own tail longer still --
                # to grow past that bound. Core's own `SendMessages`
                # (net_processing.cpp) answers the same way: several
                # `MakeAndPushMessage` calls of at most `MAX_INV_SZ` each
                # rather than one built whole. btclib-org/btclib-node#282
                #
                # Filtered against current mempool membership here, at
                # send time, rather than trusted from when it was queued:
                # a wtxid can sit in this queue for this connection's
                # whole schedule, easily longer than the time between two
                # eviction rounds (`Mempool._evict_to_limit`), so an entry
                # that was held when queued can be gone by the time this
                # runs. Core's own trickle send re-derives its inv from
                # the live mempool at this same point
                # (`CTxMemPool::ExtractBestByMiningScoreWithTopology`,
                # net_processing.cpp) rather than trusting a queue of
                # hashes either, for the same reason.
                # btclib-org/btclib-node#294
                queue = [
                    wtxid
                    for wtxid in conn.tx_announce_queue
                    if wtxid in self.node.mempool.transactions
                ]
                # Paced the way `advance_getdata` (`p2p/callbacks.py`)
                # paces a `getdata` answer's own blocks: checked before
                # every chunk rather than after, against the same field
                # and the same bound, so a peer this node is already
                # answering a `getdata` on is not additionally charged
                # for its own announcements -- whichever of the two ran
                # first this turn has already pushed `queued_send_bytes`
                # toward `MAX_GETDATA_INFLIGHT_BYTES`, and the second
                # sees that and backs off before committing anything, the
                # same displacement `p2p/connection_test.py` already
                # measures between a `getdata` answer and `get_cfilters`.
                # Nothing bounds how large `queue` itself can grow between
                # two trickles -- every transaction accepted while this
                # connection lives is appended to it, and this node
                # answers no BIP35 `mempool` request that would dump the
                # whole mempool in at once, but nothing refuses one that
                # grows this queue by hand across many turns either --
                # and before this check, nothing paced sending it in one
                # piece regardless of size. Core's own tx-inventory loop
                # in `SendMessages` (`src/net_processing.cpp`,
                # at bitcoin/bitcoin@05e49b342f) has no such check --
                # it pushes an `INV` every time `vInv` reaches
                # `MAX_INV_SZ`, as many chunks as one call needs. The
                # divergence is this tree's to own rather than Core's to
                # answer for, and `_notfound_pace`
                # (`p2p/callbacks.py`) is where it is argued: Core's full
                # send buffer only stops it reading from that peer, where
                # `MAX_QUEUED_SEND_BYTES` here drops the connection.
                # btclib-org/btclib-node#529
                sent_through = 0
                for start in range(0, len(queue), MAX_INV_SZ):
                    if conn.queued_send_bytes >= MAX_GETDATA_INFLIGHT_BYTES:
                        break
                    chunk = queue[start : start + MAX_INV_SZ]
                    conn.send(Inv([self._tx_inventory(conn, w) for w in chunk]))
                    sent_through = start + len(chunk)
                # Only the entries this call actually served leave the
                # queue: what a chunk past the bound above left behind is
                # still owed, and stays for this same function's next
                # call -- `DownloadManager.step` runs every turn of
                # `Node`'s own loop, the resume cadence `resume_getdata`
                # and `resume_cfilters` (`p2p/main.py`) already have.
                conn.tx_announce_queue = queue[sent_through:]
            # A schedule is only redrawn once this connection's queue is
            # actually empty: redrawing it while a chunk is still owed
            # would push the next attempt out to this trickle's own mean
            # delay instead of the very next turn, which is the resume
            # cadence the comment above relies on.
            if not conn.tx_announce_queue:
                if conn.inbound:
                    conn.next_inv_send_time = self._next_inbound_inv_time(
                        conn.address, now
                    )
                else:
                    conn.next_inv_send_time = now + _rng.expovariate(
                        1 / _OUTBOUND_TX_ANNOUNCE_INTERVAL
                    )

    def _tx_inventory(self, conn: Connection, wtxid: bytes) -> Inventory:
        """Name a held transaction the way `conn` relays: wtxid, else txid.

        Core's `SendMessages` announces `MSG_WTX` to a peer with
        `m_wtxid_relay` and `MSG_TX` by txid to any other.
        """
        if conn.wtxidrelay_received:
            return Inventory(InventoryType.MSG_WTX, wtxid)
        txid = self.node.mempool.transactions[wtxid].id
        return Inventory(InventoryType.MSG_TX, txid)

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
        minimum_chain_work = node.chain.consensus.minimum_chain_work
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

        `NetPermissionFlags::ForceRelay`, block-relay-only outbound
        connections and `-blocksonly` (`m_opts.ignore_incoming_txs`) all
        have nothing to read here and are not reproduced: every one is
        a permission, connection-kind or run mode this tree does not
        have -- `P2pManager` dials and accepts one connection kind, and
        nothing here grants a peer immunity from this node's own
        filter. `Connection.own_version`'s own `relay=True` argues the
        same absence already, for the identical set of Core concepts
        read against `RejectIncomingTxs`.

        Core's `GetCommonVersion() < FEEFILTER_VERSION` return is kept:
        a peer that old is sent none.
        """
        now = time.time()
        # Core's own `MaybeSendFeefilter` (`net_processing.cpp`, at
        # bitcoin/bitcoin@ca7162cde5) gates this same decision on
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
            if conn.status != P2pConnStatus.Connected:
                continue
            if common_version(conn) < FEEFILTER_VERSION:
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

    def _next_inbound_inv_time(self, address: NetworkAddressV2, now: float) -> float:
        """Return the schedule this address's net class currently shares.

        `NextInvToInbounds` (net_processing.cpp, the same commit): redrawn
        only once the class's own timer has already passed, so every
        inbound connection consulting it before the next redraw is handed
        the same value -- an outbound connection never calls this, having
        its own independent draw instead, `_send_due_announcements`'s
        other branch.
        """
        net_class = _inbound_net_class(address)
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
            from_peer = _syncs_from(
                conn, preferred=preferred, blocks_in_flight=blocks_in_flight
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
            ):
                self.logger.info(
                    "Timeout downloading headers, disconnecting connection %s",
                    conn.id,
                )
                conn.stop()

    def update_last_common_blocks(self) -> None:
        """Move each peer's `last_common` block, as Core's `SendMessages` does.

        Core runs `FindNextBlocksToDownload` for a peer that can serve
        blocks and has fewer than `MAX_BLOCKS_IN_TRANSIT_PER_PEER` in
        flight, where this node is out of initial block download or the
        peer is one `sync_headers` would sync from and not limited
        (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag). This runs the part of it that moves `last_common`
        (`block_availability.update_last_common_block`) under the same
        gate, every pass, `block_download` being what chooses the
        blocks to request. Every connection serves witnesses, which
        `callbacks.version` requires, so Core's stop at a block a peer
        without witnesses cannot serve has nothing to stop at.
        """
        node = self.node
        connections = [
            conn
            for conn in list(node.p2p_manager.connections.values())
            if conn.status == P2pConnStatus.Connected
        ]
        preferred = sum(_is_preferred_download(conn) for conn in connections)
        blocks_in_flight = any(conn.download_queue for conn in connections)
        block_index = node.chainstate.block_index
        minimum_chain_work = node.chain.consensus.minimum_chain_work
        for conn in connections:
            from_peer = _syncs_from(
                conn, preferred=preferred, blocks_in_flight=blocks_in_flight
            )
            if (
                _can_serve_blocks(conn)
                and (
                    (from_peer and not _is_limited_peer(conn))
                    or not node.is_initial_block_download
                )
                and len(conn.download_queue) < MAX_BLOCKS_PER_GETDATA_BURST
            ):
                update_last_common_block(
                    block_index, conn.block_availability, minimum_chain_work
                )

    def block_download(self) -> None:
        """Refresh the block window, evict stalled peers, and request new work.

        A no-op before headers are synced -- there is nothing to
        request candidates against yet -- and stall eviction only runs
        during IBD, once the chain is synced a slow peer costing this
        node latency rather than a stalled sync.
        """
        node = self.node
        if node.status < NodeStatus.HeaderSynced:
            return
        if not self._refresh_block_window():
            return

        connections = list(node.p2p_manager.connections.values())
        if node.status < NodeStatus.BlockSynced:
            self._evict_stalled_connections(connections)

        pending_and_waiting = self._pending_and_waiting_blocks(connections)
        if pending_and_waiting is None:
            return
        waiting, pending = pending_and_waiting

        self._request_new_block_work(connections, waiting, pending)

    def _refresh_block_window(self) -> bool:
        """Answer whether `block_window` still has work due this pass."""
        block_index = self.node.chainstate.block_index
        if not self.block_window:
            self.block_window = block_index.get_download_candidates()
        self.block_window = [
            x for x in self.block_window if not block_index.get_block_info(x).downloaded
        ]
        if not self.block_window:
            return False
        current_index = len(block_index.active_chain) - 1
        download_index = block_index.get_block_info(self.block_window[0]).index
        # too much ahead with the download
        return download_index - current_index <= MAX_DOWNLOAD_WINDOW

    def _evict_stalled_connections(self, connections: list[Connection]) -> None:
        for conn in connections:
            if (
                time.time() - conn.last_block_timestamp > _BLOCK_STALL_EVICTION_TIMEOUT
                and not conn.pending_eviction
            ):
                conn.download_queue = []
                conn.pending_eviction = True
            if (
                time.time() - conn.last_block_timestamp
                > _BLOCK_STALL_DISCONNECT_TIMEOUT
            ):
                conn.stop()

    def _pending_and_waiting_blocks(
        self, connections: list[Connection]
    ) -> tuple[list[bytes], list[bytes]] | None:
        """Answer what is still due, or `None` if every queue is full."""
        block_index = self.node.chainstate.block_index
        pending: list[bytes] = []
        skip = True
        for conn in connections:
            conn_queue = conn.download_queue
            new_queue: list[bytes] = [
                header
                for header in conn_queue
                if not block_index.get_block_info(header).downloaded
            ]
            conn.download_queue = new_queue
            pending.extend(new_queue)
            if not new_queue:
                skip = False
        if skip:
            return None

        waiting = [header for header in self.block_window if header not in pending]
        pending = [
            x[0]
            for x in Counter(pending).most_common()[::-1]
            if x[1] < _MAX_CONCURRENT_REQUESTS_PER_BLOCK
        ]
        return waiting, pending

    def _reachable_blocks(
        self, conn: Connection, candidates: list[bytes]
    ) -> list[bytes]:
        """Filter `candidates` to what `conn` can actually be asked for.

        Unfiltered for a peer that is not `_is_limited_peer` -- Core's own
        check runs only `if (is_limited_peer)` (`FindNextBlocks`,
        net_processing.cpp:1635, at bitcoin/bitcoin@ca7162cde5), any other
        peer's own candidates there being bounded only by the download
        window, matching this module's own `block_window`. For a limited
        peer, a candidate more than `MIN_BLOCKS_TO_KEEP - 2` behind
        `conn.best_known_height` is dropped -- `MIN_BLOCKS_TO_KEEP` stands
        in for Core's own `NODE_NETWORK_LIMITED_MIN_BLOCKS`, both 288 at
        that sha (`constants.py`), and `- 2` is Core's own "two blocks
        buffer for possible races", the same sign as its `>=` skip and
        the opposite of `_below_prune_threshold`'s `+ 2` on the serving
        side (`p2p/callbacks.py`) -- the request side wants to stop
        asking a little before the serving side would actually refuse,
        not a little after.
        """
        if not _is_limited_peer(conn):
            return candidates
        block_index = self.node.chainstate.block_index
        threshold = conn.best_known_height - (MIN_BLOCKS_TO_KEEP - 2)
        return [
            h for h in candidates if block_index.get_block_info(h).index > threshold
        ]

    def _request_new_block_work(
        self, connections: list[Connection], waiting: list[bytes], pending: list[bytes]
    ) -> None:
        node = self.node
        for conn in connections:
            # `pending_eviction` is this peer's queue having just been
            # emptied for stalling past `_BLOCK_STALL_EVICTION_TIMEOUT`
            # above: an empty queue is what this loop otherwise reads as
            # "ready for more work", so a peer marked here is excluded
            # rather than being handed back the very blocks it was just
            # failing to deliver. It clears on the peer's own next block
            # (callbacks.block), or the peer is gone by
            # `_BLOCK_STALL_DISCONNECT_TIMEOUT` instead.
            if conn.download_queue == [] and not conn.pending_eviction:
                if not waiting and not pending:
                    return
                # `_can_serve_blocks`'s own docstring is where gating a
                # connection out of block work entirely, here rather
                # than inside `_reachable_blocks`, is argued against
                # Core's own two-gate structure. A peer that fails it is
                # `continue`, not `return`, for the same reason
                # `_reachable_blocks` finding nothing reachable is:
                # `waiting` and `pending` still hold work for whichever
                # other connection this same pass reaches next.
                # btclib-org/btclib-node#725
                if not _can_serve_blocks(conn):
                    continue
                reachable_waiting = self._reachable_blocks(conn, waiting)
                if reachable_waiting:
                    new = reachable_waiting[:MAX_BLOCKS_PER_GETDATA_BURST]
                    waiting = [h for h in waiting if h not in new]
                else:
                    reachable_pending = self._reachable_blocks(conn, pending)[:2]
                    if not reachable_pending:
                        # Nothing left that this connection can be asked
                        # for right now: a NODE_NETWORK_LIMITED peer
                        # without NODE_NETWORK, sitting behind every
                        # candidate `_reachable_blocks` above passed it.
                        # `waiting` and `pending` still hold work for
                        # whichever other connection this same pass
                        # reaches next, so this is `continue`, not the
                        # `return` below -- that one fires only once
                        # neither list holds anything for anybody.
                        # btclib-org/btclib-node#706
                        continue
                    new = reachable_pending
                    pending = [h for h in pending if h not in new]
                conn.download_queue = new
                getdata = GetData(
                    [
                        Inventory(InventoryType.MSG_WITNESS_BLOCK, block_hash)
                        for block_hash in new
                    ]
                )
                # a block asked for here is a block coming back for
                # `update_chain` to validate, so this is the earliest
                # point that is actually true, rather than merely
                # reaching HeaderSynced -- a node whose headers are
                # synced but which never has a block to ask for (a
                # header-only peer under test, a peer whose counterpart
                # stopped serving blocks) never reaches this line and
                # never builds the pool: btclib-org/btclib-node#262
                node.warm_worker_pool()
                conn.send(getdata)
