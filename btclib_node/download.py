# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import time
from collections import Counter
from typing import TYPE_CHECKING

from btclib.p2p.inventory import GetData, Inv, Inventory, InventoryType

from btclib_node.constants import NodeStatus
from btclib_node.log import Logger

if TYPE_CHECKING:
    from btclib_node import Node


class DownloadManager:
    def __init__(self, node: Node, logger: Logger) -> None:
        self.node = node
        self.logger = logger

        self.block_window: list[bytes] = []

        self.received_txs: list[tuple[int, bytes]] = []
        self.inv_txs: list[tuple[int, bytes]] = []

    def step(self) -> None:
        self.block_download()
        self.tx_download()

    def tx_download(self) -> None:
        if self.node.status < NodeStatus.BlockSynced:
            return

        received = list(dict.fromkeys(wtxid for _, wtxid in self.received_txs))
        if received:
            # a peer that announced a transaction we now hold, or sent it
            # to us, already has it: it is the others that are told
            has_it: dict[int, set[bytes]] = {}
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
                # what the peer's version asked for. An answer nothing
                # consults is the same peer told the same thing whatever
                # it said, which is what #76 is about, so every send
                # that announces a transaction reads this --
                # `P2pManager.broadcast_raw_transaction` is the other.
                # BIP37 is that a peer which sent fRelay false is sent
                # no transaction inventory at all, so it is skipped
                # whole rather than sent a shorter list.
                if not conn.relay_tx:
                    continue
                known = has_it.get(conn.id, ())
                # BIP133: a peer told this node its own floor
                # (callbacks.feefilter, `conn.feefilter`) is not
                # announced a transaction below it either.
                # btclib-org/btclib-node#260
                inv = [
                    wtxid
                    for wtxid in received
                    if wtxid not in known
                    and self.node.mempool.meets_fee_rate(wtxid, conn.feefilter)
                ]
                # every accepted transaction, and not only those that
                # arrived in a batch of more than five: a batch size is
                # not a throttle, it is a filter on whether a transaction
                # is announced at all, and on a quiet network no batch
                # ever clears it. What Core has here instead is a
                # per-peer Poisson timer, which announces everything too
                # and buys the timing privacy an immediate announcement
                # does not; that privacy is what this does not have, and
                # it is a change of its own rather than the defect.
                if inv:
                    conn.send(
                        Inv([Inventory(InventoryType.MSG_WTX, wtxid) for wtxid in inv])
                    )

        if self.inv_txs:
            invs: dict[int, list[bytes]] = {}
            for conn_id, wtxid in self.inv_txs:
                invs.setdefault(conn_id, []).append(wtxid)

            for conn_id, inv in invs.items():
                target = self.node.p2p_manager.connections.get(conn_id)
                if target:
                    # a peer that announced the same transaction twice is
                    # asked for it once
                    target.send(
                        GetData(
                            [
                                Inventory(InventoryType.MSG_WTX, wtxid)
                                for wtxid in dict.fromkeys(inv)
                            ]
                        )
                    )

        self.inv_txs = []
        self.received_txs = []

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
            if conn.download_queue == []:
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
                conn.send(getdata)
