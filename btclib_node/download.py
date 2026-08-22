# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import time
from collections import Counter

from btclib.p2p.inventory import GetData, Inv, Inventory, InventoryType

from btclib_node.constants import NodeStatus


class DownloadManager:
    def __init__(self, node, logger):
        self.node = node
        self.logger = logger

        self.block_window = []

        self.received_txs = []
        self.inv_txs = []

    def step(self):
        self.block_download()
        self.tx_download()

    def tx_download(self):
        if self.node.status < NodeStatus.BlockSynced:
            return

        received = list(dict.fromkeys(wtxid for _, wtxid in self.received_txs))
        if received:
            # a peer that announced a transaction we now hold, or sent it
            # to us, already has it: it is the others that are told
            has_it = {}
            for conn_id, wtxid in self.received_txs:
                has_it.setdefault(conn_id, set()).add(wtxid)
            still_wanted = []
            for conn_id, wtxid in self.inv_txs:
                if wtxid in received:
                    has_it.setdefault(conn_id, set()).add(wtxid)
                else:
                    still_wanted.append((conn_id, wtxid))
            self.inv_txs = still_wanted

            for conn in self.node.p2p_manager.connections.copy().values():
                known = has_it.get(conn.id, ())
                inv = [wtxid for wtxid in received if wtxid not in known]
                if len(inv) > 5:
                    conn.send(
                        Inv([Inventory(InventoryType.MSG_WTX, wtxid) for wtxid in inv])
                    )

        if self.inv_txs:
            invs = {}
            for conn_id, wtxid in self.inv_txs:
                invs.setdefault(conn_id, []).append(wtxid)

            for conn_id, inv in invs.items():
                conn = self.node.p2p_manager.connections.get(conn_id)
                if conn:
                    # a peer that announced the same transaction twice is
                    # asked for it once
                    conn.send(
                        GetData(
                            [
                                Inventory(InventoryType.MSG_WTX, wtxid)
                                for wtxid in dict.fromkeys(inv)
                            ]
                        )
                    )

        self.inv_txs = []
        self.received_txs = []

    def block_download(self):
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

        pending = []
        skip = True
        for conn in connections:
            conn_queue = conn.download_queue
            new_queue = []
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
