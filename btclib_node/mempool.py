# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.


from collections.abc import Iterable

from btclib.tx.tx import Tx

from btclib_node.log import Logger


class Mempool:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger

        self.transactions: dict[bytes, Tx] = {}
        self.txid_index: dict[bytes, bytes] = {}
        self.size: int = 0
        self.bytesize: int = 0
        self.bytesize_limit: int = 500 * 1000**2  # 500vMB

    def is_full(self) -> bool:
        return self.bytesize >= self.bytesize_limit

    def get_missing(
        self, transactions: Iterable[bytes], wtxid: bool = False
    ) -> list[bytes]:
        if self.is_full():
            return []
        missing: list[bytes] = []
        index = self.transactions if wtxid else self.txid_index
        for tx_id in transactions:
            if tx_id not in index:
                missing.append(tx_id)
        return missing

    def get_tx(self, txid: bytes, wtxid: bool = False) -> Tx | None:
        key = txid if wtxid else self.txid_index.get(txid)
        if key is None:
            return None
        return self.transactions.get(key)

    # Don't need lock because handled in same thread
    def add_tx(self, tx: Tx) -> None:
        if self.is_full():
            return
        wtxid, txid = tx.hash, tx.id
        if txid not in self.txid_index:
            self.transactions[wtxid] = tx
            self.txid_index[tx.id] = wtxid
            self.size += 1
            self.bytesize += tx.vsize

    def remove_tx(self, tx: Tx) -> None:
        txid = tx.id
        if txid in self.txid_index:
            wtxid = self.txid_index.pop(tx.id)
            tx = self.transactions.pop(wtxid)
            self.size -= 1
            self.bytesize -= tx.vsize

    def contains_tx(self, tx: Tx) -> bool:
        return tx.hash in self.transactions
