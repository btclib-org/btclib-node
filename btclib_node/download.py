# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import time
from collections import Counter
from random import SystemRandom
from typing import TYPE_CHECKING

from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2
from btclib.p2p.inventory import GetData, Inv, Inventory, InventoryType
from btclib.p2p.limits import MAX_INV_SZ

from btclib_node.constants import NodeStatus
from btclib_node.log import Logger

if TYPE_CHECKING:
    from btclib_node import Node

# net_processing.cpp's INBOUND_INVENTORY_BROADCAST_INTERVAL and
# OUTBOUND_INVENTORY_BROADCAST_INTERVAL, bitcoin/bitcoin@58a7869f86: the
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

# `rand_exp_duration`, the same file: a CSPRNG rather than a statistical
# one, for the same reason `secrets` is what the rest of this tree draws
# a peer-facing nonce or choice from -- this schedule is exactly what a
# peer is meant not to be able to predict.
_rng = SystemRandom()


def _inbound_net_class(address: NetworkAddressV2) -> BIP155Network | int:
    """Return the key an inbound peer's schedule is shared across.

    `CNode::m_network_key` (net.h:755) is what `NextInvToInbounds`
    (net_processing.cpp:6318-6319, calling `PeerManagerImpl::
    NextInvToInbounds` at :1273-1282) actually keys its per-peer timer
    on, bitcoin/bitcoin@58a7869f86. For an inbound connection it is a
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


class DownloadManager:
    def __init__(self, node: Node, logger: Logger) -> None:
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

    def step(self) -> None:
        self.block_download()
        self.tx_download()

    def tx_download(self) -> None:
        if self.node.status < NodeStatus.BlockSynced:
            return

        received = list(dict.fromkeys(wtxid for _, wtxid in self.received_txs))
        if received:
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
                if wtxid in received:
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
                for wtxid in new_for_conn:
                    if wtxid not in conn.tx_announce_queue:
                        conn.tx_announce_queue.append(wtxid)

        self._send_due_announcements()

        if self.inv_txs:
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
                    GetData(
                        [Inventory(InventoryType.MSG_WTX, wtxid) for wtxid in wanted]
                    )
                )

        self.inv_txs = []
        self.received_txs = []

    def _send_due_announcements(self) -> None:
        # Core's `TxRelay::m_next_inv_send_time`/`m_tx_inventory_to_send`
        # (net_processing.cpp, bitcoin/bitcoin@58a7869f86): each
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
        node = self.node
        if node.status < NodeStatus.HeaderSynced:
            return

        block_index = node.chainstate.block_index

        if not self.block_window:
            self.block_window = block_index.get_download_candidates()
        self.block_window = [
            x for x in self.block_window if not block_index.get_block_info(x).downloaded
        ]
        if not self.block_window:
            return
        current_index = len(block_index.active_chain) - 1
        download_index = block_index.get_block_info(self.block_window[0]).index
        # too much ahead with the download
        if download_index - current_index > 1024:
            return

        connections = list(node.p2p_manager.connections.values())
        if node.status < NodeStatus.BlockSynced:
            for conn in connections:
                if (
                    time.time() - conn.last_block_timestamp > 120
                    and not conn.pending_eviction
                ):
                    conn.download_queue = []
                    conn.pending_eviction = True
                if time.time() - conn.last_block_timestamp > 300:
                    conn.stop()

        pending: list[bytes] = []
        skip = True
        for conn in connections:
            conn_queue = conn.download_queue
            new_queue: list[bytes] = []
            for header in conn_queue:
                if not block_index.get_block_info(header).downloaded:
                    new_queue.append(header)
            conn.download_queue = new_queue
            pending.extend(new_queue)
            if not new_queue:
                skip = False
        if skip:
            return

        waiting = [header for header in self.block_window if header not in pending]
        pending = [x[0] for x in Counter(pending).most_common()[::-1] if x[1] < 3]

        for conn in connections:
            # `pending_eviction` is this peer's queue having just been
            # emptied for stalling past the 120s mark above: an empty
            # queue is what this loop otherwise reads as "ready for more
            # work", so a peer marked here is excluded rather than being
            # handed back the very blocks it was just failing to deliver.
            # It clears on the peer's own next block (callbacks.block),
            # or the peer is gone by the 300s mark instead.
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
                    [Inventory(InventoryType.MSG_WITNESS_BLOCK, hash) for hash in new]
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
