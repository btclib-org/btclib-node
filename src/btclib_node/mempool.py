# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.


import time
from fractions import Fraction
from typing import TYPE_CHECKING

from btclib.fee import FeeRate, fee_from_vsize

if TYPE_CHECKING:
    from collections.abc import Iterable

    from btclib.tx.tx import Tx

    from btclib_node.log import Logger

# Core's own `DEFAULT_INCREMENTAL_RELAY_FEE` (`src/policy/policy.h`,
# bitcoin/bitcoin@58a7869f86): what an eviction round bumps the rolling
# minimum to, above the feerate of whatever it just evicted, so a
# transaction does not requalify at the exact rate something was just
# evicted for. A constant of this module and not `Config.min_relay_feerate`
# -- that field is BIP133's own floor, documented in `config.py` as "not
# enforced anywhere else" than the `feefilter` this node sends, and
# reading it here for a second purpose would make that comment false for
# a reason nobody asked for. Core keeps the two as separate knobs,
# `-minrelaytxfee` and `-incrementalrelayfee`, that merely share a
# default; this module does the same rather than coupling to a field
# whose own documentation disclaims the coupling.
_INCREMENTAL_RELAY_FEE_RATE = FeeRate(sats_per_kvbyte=100)

# Core's own `ROLLING_FEE_HALFLIFE` (`src/txmempool.h:212`, same commit):
# seconds for the rolling minimum to decay by half once it is decaying at
# all, shortened as this mempool empties -- `get_min_fee_rate` below.
_ROLLING_FEE_HALFLIFE = 60 * 60 * 12


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
        # Core's own `DEFAULT_MAX_MEMPOOL_SIZE_MB`
        # (`src/kernel/mempool_options.h`, bitcoin/bitcoin@58a7869f86) is
        # 300, and this field carried 500 with no argument on record for
        # the difference -- inert while nothing evicted, since the exact
        # number only ever decided whether `add_tx` refused outright.
        # `_evict_to_limit` below is what first makes it a real economic
        # threshold rather than a wall, which is the reasoning this value
        # needed and did not have: matching Core's own default is that
        # reasoning, absent a measurement that says this tree's own
        # traffic wants a different one. Not exposed on `Config`: making
        # it configurable is a widening (a new CLI/RPC-facing knob, its
        # own validation, its own interaction with a live-lowered limit)
        # that eviction does not need to exist -- the wall this issue
        # closes is the missing eviction, not the fixed ceiling.
        # btclib-org/btclib-node#294
        self.bytesize_limit: int = 300 * 1000**2  # 300vMB
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

        # Core's own `rollingMinimumFeeRate`/`lastRollingFeeUpdate`/
        # `blockSinceLastRollingFeeBump` (`src/txmempool.h`, same commit):
        # the state `get_min_fee_rate` reads and updates, and
        # `_track_package_removed`/`note_block_connected` write. A float,
        # matching Core's own `double`: this is advisory relay policy
        # decayed by a continuous exponential, not a satoshi amount any
        # consensus or acceptance rule reads exactly.
        self._rolling_min_fee_rate: float = 0.0
        self._last_rolling_fee_update: float = 0.0
        self._block_since_last_rolling_fee_bump: bool = False

    def is_full(self) -> bool:
        return self.bytesize >= self.bytesize_limit

    def get_missing(
        self, transactions: Iterable[bytes], *, wtxid: bool = False
    ) -> list[bytes]:
        # No `is_full` guard: that used to answer every request with
        # nothing at all once past the limit, which was the wall
        # `_evict_to_limit` now removes -- Core's own request tracking
        # never consults mempool occupancy before asking either, letting
        # `add_tx` decide per transaction instead. btclib-org/btclib-node#294
        index = self.transactions if wtxid else self.txid_index
        return [tx_id for tx_id in transactions if tx_id not in index]

    def get_tx(self, txid: bytes, *, wtxid: bool = False) -> Tx | None:
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
        # transaction for announcement -- p2p/callbacks.py's `tx`,
        # rpc/callbacks.py's `send_raw_transaction` -- gates that on: a
        # transaction this call did not keep is not one to tell every
        # other peer about, or that call answers its own `getdata` with
        # `notfound`. btclib-org/btclib-node#277
        #
        # Past `is_full()`, this used to refuse outright, whatever the
        # transaction paid. It now adds the transaction provisionally and
        # runs `_evict_to_limit`, Core's own `LimitMempoolSize` shape
        # (`validation.cpp`, called right after a provisional add,
        # bitcoin/bitcoin@58a7869f86): if this transaction is itself the
        # worst one held once trimming is done, eviction takes it right
        # back out and the return value is `False` here exactly as it
        # was for the old outright refusal -- `rpc/callbacks.py`'s own
        # "Mempool is full" answers that case whether it is reached this
        # way or the old way. A transaction already held under this
        # txid, same witness or not, is still a no-op that never touches
        # bytesize at all: btclib-org/btclib-node#293's own resubmission
        # handling reads unchanged. btclib-org/btclib-node#294
        wtxid, txid = tx.hash, tx.id
        if txid in self.txid_index:
            return False
        self.transactions[wtxid] = tx
        self.txid_index[txid] = wtxid
        self.fees[wtxid] = fee
        self.size += 1
        self.bytesize += tx.vsize
        self.sequence += 1
        self._evict_to_limit()
        return wtxid in self.transactions

    def remove_tx(self, tx: Tx) -> None:
        txid = tx.id
        if txid in self.txid_index:
            self._pop(self.txid_index[txid])

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
        for -- already relayed out of `Mempool.add_tx`'s own default,
        evicted, or gone from the mempool for any other reason by the
        time this is asked -- since there is nothing here to withhold it
        for. A caller relaying only what this mempool still holds is
        `download.py`'s own responsibility, checked there rather than
        assumed here: btclib-org/btclib-node#294.
        """
        if not min_fee_rate:
            return True
        tx = self.transactions.get(wtxid)
        fee = self.fees.get(wtxid)
        if tx is None or fee is None:
            return True
        return fee >= fee_from_vsize(tx.vsize, FeeRate(sats_per_kvbyte=min_fee_rate))

    def _pop(self, wtxid: bytes) -> Tx:
        """Remove one entry by wtxid and return the transaction removed.

        The one place every removal, `remove_tx` and eviction alike,
        updates the bookkeeping the four dicts and three counters above
        carry -- so the two never drift the way two independent copies
        of the same accounting would.
        """
        tx = self.transactions.pop(wtxid)
        self.txid_index.pop(tx.id, None)
        self.fees.pop(wtxid, None)
        self.size -= 1
        self.bytesize -= tx.vsize
        self.sequence += 1
        return tx

    def _descendants(self, wtxid: bytes) -> set[bytes]:
        """Return wtxid and every mempool transaction depending on it.

        `verify_mempool_acceptance` (`main.py`) admits a child whose
        parent is only in this mempool, not yet confirmed -- so evicting
        a parent without what spends it would leave a transaction here
        whose own prevout resolves nowhere. This mempool holds no
        dependency graph to answer that from directly, only two dicts
        and a fee map, so the answer is a scan run at eviction time
        rather than an index kept up to date on every `add_tx` and
        `_pop`: eviction is the rarer path, and a second index is a
        second place the accounting above could drift. `prev_out.tx_id`
        is a txid, matching what `txid_index` keys transactions by and
        what `verify_mempool_acceptance`'s own mempool lookup reads a
        prevout by. btclib-org/btclib-node#294
        """
        root_txid = self.transactions[wtxid].id
        descendants = {wtxid}
        frontier = [root_txid]
        while frontier:
            txid = frontier.pop()
            for candidate_wtxid, candidate_tx in self.transactions.items():
                if candidate_wtxid in descendants:
                    continue
                if any(vin.prev_out.tx_id == txid for vin in candidate_tx.vin):
                    descendants.add(candidate_wtxid)
                    frontier.append(candidate_tx.id)
        return descendants

    def _evict_to_limit(self) -> None:
        """Evict by worst individual feerate until back at the limit.

        Core's own `TrimToSize` (`src/txmempool.cpp:909`,
        bitcoin/bitcoin@58a7869f86) evicts the worst *chunk*, a package
        score `m_txgraph` computes over the whole cluster graph -- so a
        low-feerate parent paid for by a high-feerate child is not taken
        out from under it. This mempool holds no dependency graph to
        score packages by, only enough to answer "what depends on this"
        once a root is already chosen (`_descendants`), so the
        substitute that stays consistent picks the worst *individual*
        feerate instead and evicts it together with whatever depends on
        it -- not because that is cheapest to evict, but because it is
        the only choice that never leaves a remaining transaction whose
        own prevout no longer resolves. Ties break toward the
        longest-held entry, `self.transactions`' own insertion order and
        `min`'s own stability, there being no ordering Core's package
        score would give here to break them by instead. This is a
        deliberate, argued departure from Core, unlike `removed_rate`
        below, which matches it.
        """
        while self.bytesize > self.bytesize_limit and self.transactions:
            worst = min(
                self.transactions,
                key=lambda w: Fraction(self.fees[w], self.transactions[w].vsize),
            )
            package = self._descendants(worst)
            # Core's own `removed` (`:917-925`): the feerate of the whole
            # evicted chunk -- `GetWorstMainChunk`'s own aggregate fee
            # over its own aggregate size, not the worst entry's rate
            # alone -- with `m_opts.incremental_relay_feerate` added to
            # it by `CFeeRate::operator+=` (`policy/feerate.h:80-82`),
            # which sums the two rates' own sat/kvB values rather than
            # combining them by size. A low-fee parent evicted together
            # with a child overpaying for it (CPFP) bumps the rolling
            # minimum by their combined rate, not by the parent's own
            # rate alone, which the aggregate here reproduces even though
            # this mempool's own selection above does not chase CPFP the
            # way `m_txgraph`'s package score does.
            #
            # sat/kvB, exact until the float `_track_package_removed`
            # stores it as -- Core's own `CFeeRate` arithmetic in
            # `TrimToSize` is int64 rather than float, a difference this
            # module's own advisory, non-consensus use of the number
            # does not need to close.
            package_fee = sum(self.fees[w] for w in package)
            package_vsize = sum(self.transactions[w].vsize for w in package)
            removed_rate = Fraction(package_fee, package_vsize) * 1000
            removed_rate += _INCREMENTAL_RELAY_FEE_RATE.sats_per_kvbyte
            for victim in package:
                self._pop(victim)
            self._track_package_removed(float(removed_rate))

    def _track_package_removed(self, rate_per_kvbyte: float) -> None:
        """Core's own `trackPackageRemoved` (`txmempool.cpp`, same commit).

        The rolling minimum only ever rises here, and every rise restarts
        `get_min_fee_rate`'s own decay: an eviction round always answers
        "no decay yet" until a block has passed since the last one of
        these, `note_block_connected` being what sets that back to true.
        """
        if rate_per_kvbyte > self._rolling_min_fee_rate:
            self._rolling_min_fee_rate = rate_per_kvbyte
            self._block_since_last_rolling_fee_bump = False

    def note_block_connected(self) -> None:
        """Restart the rolling minimum's decay clock for one connected block.

        Core's own `removeForBlock` (`src/txmempool.cpp:405-427`, same
        commit) sets `lastRollingFeeUpdate`/`blockSinceLastRollingFeeBump`
        this way for every block, whether or not that block held any
        transaction this mempool was also holding -- called once per
        block from `main.update_chain`'s own connect loop, and not folded
        into `remove_tx`, which already runs once per transaction inside
        that same loop rather than once per block.
        """
        self._last_rolling_fee_update = time.time()
        self._block_since_last_rolling_fee_bump = True

    def get_min_fee_rate(self) -> FeeRate:
        """Return the rolling minimum feerate, decayed since it last moved.

        Core's own `GetMinFee` (`src/txmempool.cpp:877`, same commit),
        with `sizelimit` read from `self.bytesize_limit` rather than
        threaded through as an argument, since this mempool already owns
        that number instead of a caller supplying it each call.

        No decay at all until a block has passed since the value last
        rose (`_block_since_last_rolling_fee_bump`) -- an eviction round
        with no block in between only ever raises it,
        `_track_package_removed`'s own guard. Past that, every 12-hour
        half-life (`_ROLLING_FEE_HALFLIFE`) erodes it toward zero, the
        half-life itself shortened while this mempool is well under its
        limit -- `self.bytesize` standing in for Core's own
        `DynamicMemoryUsage()`, both being how full the mempool actually
        is rather than how many transactions it holds -- and the value
        floored at `_INCREMENTAL_RELAY_FEE_RATE` once it decays, or
        zeroed once it decays under half of that: below that floor it is
        not a small minimum, it is none.

        `round` rather than Core's own `llround` (ties-to-even against
        ties-away-from-zero) is the one place this departs from
        `GetMinFee`'s own arithmetic -- a tie only a decayed float lands
        on exactly, and advisory relay policy this module does not
        thread through consensus does not need closed to the bit.
        """
        if (
            not self._block_since_last_rolling_fee_bump
            or not self._rolling_min_fee_rate
        ):
            return FeeRate(sats_per_kvbyte=round(self._rolling_min_fee_rate))

        now = time.time()
        if now > self._last_rolling_fee_update + 10:
            halflife: float = _ROLLING_FEE_HALFLIFE
            if self.bytesize < self.bytesize_limit / 4:
                halflife /= 4
            elif self.bytesize < self.bytesize_limit / 2:
                halflife /= 2
            elapsed = now - self._last_rolling_fee_update
            self._rolling_min_fee_rate /= 2 ** (elapsed / halflife)
            self._last_rolling_fee_update = now

            if (
                self._rolling_min_fee_rate
                < _INCREMENTAL_RELAY_FEE_RATE.sats_per_kvbyte / 2
            ):
                self._rolling_min_fee_rate = 0.0
                return FeeRate(sats_per_kvbyte=0)

        return FeeRate(
            sats_per_kvbyte=max(
                round(self._rolling_min_fee_rate),
                _INCREMENTAL_RELAY_FEE_RATE.sats_per_kvbyte,
            )
        )
