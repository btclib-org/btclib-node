# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.


from collections.abc import Iterable

from btclib.fee import FeeRate, fee_from_vsize
from btclib.tx.tx import Tx

from btclib_node.log import Logger


class Mempool:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger

        self.transactions: dict[bytes, Tx] = {}
        self.txid_index: dict[bytes, bytes] = {}
        # wtxid -> fee in satoshi, the sum-of-inputs-less-sum-of-outputs
        # main.verify_mempool_acceptance already computes and would
        # otherwise discard. btclib-org/btclib-node#260
        self.fees: dict[bytes, int] = {}
        self.size: int = 0
        self.bytesize: int = 0
        self.bytesize_limit: int = 500 * 1000**2  # 500vMB
        # Core's own external-tracking counter, `CTxMemPool::m_sequence_number`
        # (`src/txmempool.h:200-202`): "incremented once every time a
        # transaction is added or removed from the mempool for any reason".
        # `getrawmempool`'s `mempool_sequence` is a read of this value
        # (`GetSequence`, `src/txmempool.h:598-600`), not itself a bump.
        # Core initializes the field to 1, not 0 (`src/txmempool.h:202`),
        # and bumps it through `GetAndIncrementSequence`'s C++
        # post-increment (`return m_sequence_number++;`, `:594-596`) --
        # the value handed to a signal recipient is the one *before* the
        # bump, but the field itself, which is what `GetSequence` later
        # reads, is already past it. Starting at 1 here is what makes a
        # fresh mempool answer `mempool_sequence: 1`, matching Core's own
        # answer for zero events, and what keeps every later answer at
        # Core's own N+1 after N add/remove events rather than N.
        self.sequence: int = 1

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
    def add_tx(self, tx: Tx, fee: int = 0) -> bool:
        # `fee` defaults to 0 rather than being required, for the
        # callers -- mostly in tests -- that add a transaction without
        # ever asking what it pays; every production caller has just
        # computed the real one out of main.verify_mempool_acceptance
        # and passes it explicitly.
        #
        # The return value is what a caller that also queues the
        # transaction for announcement -- p2p/callbacks.py's `tx` --
        # gates that on: a full mempool below is a silent no-op, and
        # queuing an announcement for what this call did not actually
        # add would tell every other peer about a transaction only to
        # answer their own `getdata` with `notfound`.
        # btclib-org/btclib-node#277
        if self.is_full():
            return False
        wtxid, txid = tx.hash, tx.id
        if txid not in self.txid_index:
            self.transactions[wtxid] = tx
            self.txid_index[tx.id] = wtxid
            self.fees[wtxid] = fee
            self.size += 1
            self.bytesize += tx.vsize
            self.sequence += 1
            return True
        return False

    def remove_tx(self, tx: Tx) -> None:
        txid = tx.id
        if txid in self.txid_index:
            wtxid = self.txid_index.pop(tx.id)
            tx = self.transactions.pop(wtxid)
            self.fees.pop(wtxid, None)
            self.size -= 1
            self.bytesize -= tx.vsize
            self.sequence += 1

    def contains_tx(self, tx: Tx) -> bool:
        return tx.hash in self.transactions

    def meets_fee_rate(self, wtxid: bytes, min_fee_rate: int) -> bool:
        """Whether the entry's own fee clears a rate quoted in sat/kvB.

        BIP133's own comparison -- Core's `txiter->GetFee() <
        filterrate.GetFee(txiter->GetTxSize())`, net_processing.cpp --
        against this mempool's own record of what the transaction paid,
        rather than recomputing it at relay time. `min_fee_rate` of
        zero, BIP133's and `Connection.feefilter`'s own "no filter"
        value, always clears; so does a wtxid this mempool holds no fee
        for -- already relayed out of `Mempool.add_tx`'s own default, or
        gone from the mempool by the time this is asked -- since there
        is nothing here to withhold it for.
        """
        if not min_fee_rate:
            return True
        tx = self.transactions.get(wtxid)
        fee = self.fees.get(wtxid)
        if tx is None or fee is None:
            return True
        return fee >= fee_from_vsize(tx.vsize, FeeRate(sats_per_kvbyte=min_fee_rate))
