# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`DownloadManager`, what decides what this node asks its peers for.

Block download candidates and stall detection, transaction
announcement and request tracking, and the trickle timing behind both
-- `feefilter` resends, address relay, and the exponential delays that
keep two peers from being told the same thing in lockstep. Most of the
constants here are a named Bitcoin Core constant carried over with the
commit it was read at beside it, per this tree's own convention of
following Core's behaviour where that is possible and reasonable.
"""

import time
from bisect import bisect_left
from collections import Counter
from random import SystemRandom
from typing import TYPE_CHECKING

from btclib.p2p.inventory import GetData, Inv, Inventory, InventoryType
from btclib.p2p.limits import MAX_INV_SZ
from btclib.p2p.negotiation import FeeFilter

from btclib_node.chainstate.block_index import MAX_DOWNLOAD_WINDOW
from btclib_node.constants import NodeStatus, P2pConnStatus

if TYPE_CHECKING:
    from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2

    from btclib_node import Node
    from btclib_node.log import Logger
    from btclib_node.p2p.connection import Connection

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
# wtxid, permanently, since `tx_download`'s own `wanted` filter reads the
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

# a block hash already queued to this many connections is left for one
# of them to answer before being handed to yet another -- redundant
# requests bound rather than eliminated, since a slow or lying peer is
# what the redundancy is for
_MAX_CONCURRENT_REQUESTS_PER_BLOCK = 3


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

    Block download candidates and stall detection, transaction
    announcement and request tracking, and the `feefilter` trickle: the
    module docstring above is where the constants each of those follows
    are argued against Core's own.
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

    def step(self) -> None:
        """Run one pass: block download, tx download, then feefilter resends."""
        self.block_download()
        self.tx_download()
        self._send_due_feefilters()

    def tx_download(self) -> None:
        """Announce what this node received, and request what it still wants.

        A no-op until the chain itself is synced: a peer's `inv` for a
        transaction is only worth requesting once this node has a
        mempool to check it against, and until then everything received
        here is a block's own, not a loose transaction.
        """
        if self.node.status < NodeStatus.BlockSynced:
            return

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
        # announcements` sends in; membership below is against this set
        # instead, so a peer with many wtxids still outstanding does not
        # turn one `inv_txs` pass into a full scan of `received` per
        # entry. btclib-org/btclib-node#444
        received_set = set(received)
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
        for conn_id, wtxid in self.inv_txs:
            if wtxid in received_set:
                has_it.setdefault(conn_id, set()).add(wtxid)
            else:
                still_wanted.append((conn_id, wtxid))
        self.inv_txs = still_wanted

        for conn in self.node.p2p_manager.connections.copy().values():
            # the tx is in the mempool now: nobody is still owed an
            # answer to a `getdata` this node already sent for it,
            # wtxid matching what the request loop below asks by.
            for wtxid in received:
                conn.tx_requested.pop(wtxid, None)

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
        for conn_id, wtxid in self.inv_txs:
            invs.setdefault(conn_id, []).append(wtxid)

        for conn_id, inv in invs.items():
            target = self.node.p2p_manager.connections.get(conn_id)
            if not target:
                continue
            now = time.time()
            # an ask outstanding longer than a peer could plausibly
            # still be about to answer is no longer treated as
            # outstanding: a peer that neither sends the transaction
            # nor answers `notfound` would otherwise block every
            # future request to it for this wtxid, permanently.
            # btclib-org/btclib-node#289
            for wtxid, asked_at in list(target.tx_requested.items()):
                if now - asked_at > _TX_REQUEST_TIMEOUT:
                    del target.tx_requested[wtxid]
            # a peer that announced the same transaction twice is
            # asked for it once, and a peer already asked for a
            # transaction is not asked again while that ask is still
            # outstanding: `not_found` is what clears it early, the
            # tx itself arriving is what clears it above.
            wanted = [
                wtxid
                for wtxid in dict.fromkeys(inv)
                if wtxid not in target.tx_requested
            ]
            if not wanted:
                continue
            for wtxid in wanted:
                target.tx_requested[wtxid] = now
            target.send(
                GetData([Inventory(InventoryType.MSG_WTX, wtxid) for wtxid in wanted])
            )

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
                for start in range(0, len(queue), MAX_INV_SZ):
                    chunk = queue[start : start + MAX_INV_SZ]
                    conn.send(
                        Inv(
                            [Inventory(InventoryType.MSG_WTX, wtxid) for wtxid in chunk]
                        )
                    )
                conn.tx_announce_queue = []
            if conn.inbound:
                conn.next_inv_send_time = self._next_inbound_inv_time(conn.address, now)
            else:
                conn.next_inv_send_time = now + _rng.expovariate(
                    1 / _OUTBOUND_TX_ANNOUNCE_INTERVAL
                )

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
        filter. `Connection.send_version`'s own `relay=True` argues the
        same absence already, for the identical set of Core concepts
        read against `RejectIncomingTxs`.

        Core's `GetCommonVersion() < FEEFILTER_VERSION` return is absent
        on different grounds, this tree having a peer version where it
        has none of the above: the `version` handshake callback
        discourages and stops a peer below `ProtocolVersion`, which
        sits above Core's `FEEFILTER_VERSION`, so a connection reached
        here has cleared that floor already.
        """
        now = time.time()
        # Core's own `IsInitialBlockDownload()`: while still syncing,
        # `handle_p2p_handshake`'s own `tx` handler already drops
        # anything received rather than queue it (btclib-org/btclib-node#129),
        # so this is telling a peer what this node already does rather
        # than computing a real minimum there is not yet a synced
        # mempool to have one.
        ibd = self.node.status < NodeStatus.BlockSynced
        current_filter = (
            self._max_feefilter
            if ibd
            else self.node.mempool.get_min_fee_rate().sats_per_kvbyte
        )
        min_relay_feerate = self.node.config.min_relay_feerate.sats_per_kvbyte

        for conn in self.node.p2p_manager.connections.copy().values():
            if conn.status != P2pConnStatus.Connected:
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
                if waiting:
                    new = waiting[:16]
                    waiting = waiting[16:]
                elif pending:
                    new = pending[:2]
                    pending = pending[2:]
                else:
                    return
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
