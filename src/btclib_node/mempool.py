# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Mempool`, this node's set of transactions not yet in a block.

Reached from `Node`'s own thread alone -- `add_tx` and `remove_tx` are
called from the p2p callbacks, the rpc callbacks and `main.update_chain`,
never from `P2pManager`'s or `RpcManager`'s own asyncio loop -- so it
carries no lock of its own. The rolling minimum feerate an eviction
round leaves behind decays the way Core's own does, `_ROLLING_FEE_HALFLIFE`
below being `ROLLING_FEE_HALFLIFE` (`src/txmempool.h`).
"""

import heapq
import time
from collections import deque
from fractions import Fraction
from typing import TYPE_CHECKING, NamedTuple

from btclib.fee import FeeRate, fee_from_vsize

from btclib_node.exceptions import TxRejectedError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from btclib.tx.tx import Tx

    from btclib_node.log import Logger

__all__ = ["Mempool", "MempoolEntry", "format_money"]

# Core's own `MAX_BIP125_RBF_SEQUENCE` (`src/policy/rbf.h`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `SignalsOptInRBF`'s own
# bound, an input below it opting a transaction into BIP125 replacement.
_MAX_BIP125_RBF_SEQUENCE = 0xFFFFFFFE


class MempoolEntry(NamedTuple):
    """One held transaction's own `getmempoolentry` accounting.

    `Mempool.entry` below is the one place this is built; its own
    docstring is where the shape -- Core's own `entryToJSON`, less what
    a cluster mempool alone backs -- is argued.
    """

    vsize: int
    weight: int
    time: int
    height: int
    wtxid: bytes
    fee: int
    modified_fee: int
    ancestor_count: int
    ancestor_size: int
    ancestor_fees: int
    descendant_count: int
    descendant_size: int
    descendant_fees: int
    depends: list[bytes]
    spent_by: list[bytes]
    bip125_replaceable: bool
    unbroadcast: bool


# Core's own `CRollingBloomFilter(120'000, 0.000'001)`
# (`src/node/txdownloadman_impl.h`, at bitcoin/bitcoin@4519933391): "a
# flooding attacker attempting to roll-over the filter using
# minimum-sized, 60byte, transactions might manage to send 1000/sec if
# we have fast peers, so we pick 120,000 to give our peers a two minute
# window" -- reasoning about the attacker's own bandwidth, not about
# btclib-node's implementation, so the element count carries over even
# though the structure holding them does not: a plain `set` of 32-byte
# wtxids has no false-positive rate to pick a filter size against, so
# there is no analogue of Core's other parameter (one in a million)
# here at all. `Mempool.mark_rejected` below is where the set this
# bounds is kept.
_RECENT_REJECTS_CAPACITY = 120_000

# Core's own `DEFAULT_INCREMENTAL_RELAY_FEE` (`src/policy/policy.h`,
# at bitcoin/bitcoin@58a7869f86): what an eviction round bumps the rolling
# minimum to, above the feerate of whatever it just evicted, so a
# transaction does not requalify at the exact rate something was just
# evicted for. A constant of this module and not `Config.min_relay_feerate`:
# Core keeps the two as separate knobs, `-minrelaytxfee` and
# `-incrementalrelayfee`, that merely share a default.
_INCREMENTAL_RELAY_FEE_RATE = FeeRate(sats_per_kvbyte=100)

# Core's own `ROLLING_FEE_HALFLIFE` (`src/txmempool.h:212`, same commit):
# seconds for the rolling minimum to decay by half once it is decaying at
# all, shortened as this mempool empties -- `get_min_fee_rate` below.
_ROLLING_FEE_HALFLIFE = 60 * 60 * 12

_COIN = 100_000_000


def format_money(amount: int) -> str:
    """Core's own `FormatMoney` for an amount never negative here.

    Eight decimals, right-trimmed of zeros down to two
    (`src/util/moneystr.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag): 5000 satoshi is "0.00005", one bitcoin "1.00".
    """
    whole, fraction = divmod(amount, _COIN)
    decimals = f"{fraction:08d}".rstrip("0").ljust(2, "0")
    return f"{whole}.{decimals}"


class Mempool:
    """The node's set of transactions not yet in a block, keyed both ways.

    `transactions` is by wtxid, `txid_index` maps a txid to the wtxid
    that holds it, `fees` carries what each entry paid and `vsizes` the
    size it is priced and counted by -- the module
    docstring above is where the single-thread invariant that lets this
    class carry no lock of its own is argued. `spent_by` is the fourth
    index, `_descendants` below is where it is read; `_feerate_heap` is
    the fifth, `_pop_worst_wtxid` below being where it is read and
    `_rebuild_feerate_heap` where it is kept from growing without bound.
    `_heap_current_seq` is the sixth, and is what tells a heap entry for
    a wtxid still held apart from one superseded by a later re-add of
    the same wtxid -- `_pop_worst_wtxid` again being where that is read.
    `_recent_rejects` is the seventh, a bounded record of refused
    candidates rather than held ones, `_recent_rejects_order` alongside
    it tracking insertion order for eviction -- `mark_rejected` below is
    where both are written and `was_recently_rejected` where the first
    is read.
    """

    def __init__(self, logger: Logger) -> None:
        """Start empty, with the rolling minimum feerate at zero, undecayed."""
        self.logger = logger

        self.transactions: dict[bytes, Tx] = {}
        self.txid_index: dict[bytes, bytes] = {}
        # wtxid -> fee in satoshi, the sum-of-inputs-less-sum-of-outputs
        # main.verify_mempool_acceptance already computes and would
        # otherwise discard. btclib-org/btclib-node#260
        self.fees: dict[bytes, int] = {}
        # wtxid -> Core's `CTxMemPoolEntry::GetTxSize`, the sigop-adjusted
        # vsize `main.verify_mempool_acceptance` computes: what every
        # feerate and the size limit here read. btclib-org/btclib-node#1357
        self.vsizes: dict[bytes, int] = {}
        # wtxid -> Core's own `CTxMemPoolEntry::GetTime`
        # (`src/kernel/mempool_entry.h`, at bitcoin/bitcoin@9be056a8a7, the
        # v31.1 tag): the wall-clock second this entry was accepted,
        # `add_tx` below's own `time.time()`. `getmempoolentry`'s own
        # `time` field. btclib-org/btclib-node#1397
        self.entry_times: dict[bytes, float] = {}
        # wtxid -> Core's own `CTxMemPoolEntry::GetHeight`, the active
        # chain's own tip height -- not `verify_mempool_acceptance`'s own
        # `spend_height`, one past it -- at the moment this entry was
        # accepted (`m_active_chainstate.m_chain.Height()`,
        # `MemPoolAccept::Finalize`, `src/validation.cpp`, same tag).
        # `getmempoolentry`'s own `height` field. btclib-org/btclib-node#1397
        self.heights: dict[bytes, int] = {}
        # txid -> nothing, this mempool's own copy of Core's own
        # `m_unbroadcast_txids` (`src/txmempool.h`, same tag): a
        # transaction `rpc.callbacks.send_raw_transaction` submitted and
        # kept, until some peer's own `getdata` is served for it
        # (`mark_broadcast` below) or it leaves this mempool for any
        # reason (`_pop`) -- never one a peer handed this node over the
        # wire, `p2p.callbacks.tx` calling neither. `get_mempool_info`'s
        # own `unbroadcastcount` is this set's size.
        # btclib-org/btclib-node#1421
        self.unbroadcast: set[bytes] = set()
        # txid -> the wtxids, held in this mempool, of whatever spends an
        # output of that txid -- kept up to date in `add_tx` and `_pop`
        # rather than rebuilt at eviction time, `_descendants` below being
        # the reader btclib-org/btclib-node#441 added it for. A txid this
        # mempool never holds a spender of is simply absent, not mapped to
        # an empty set: `_pop` deletes the entry once its last spender
        # leaves rather than leaving an empty set behind for every
        # confirmed parent a mempool transaction ever spent.
        self.spent_by: dict[bytes, set[bytes]] = {}
        # (txid, vout) -> the wtxid, held in this mempool, spending that
        # outpoint: Core's own `mapNextTx` (`src/txmempool.h`,
        # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), what
        # `check_replacement` and `remove_conflicts` below read. `add_tx`
        # refusing a second spender is what keeps one wtxid per outpoint.
        # btclib-org/btclib-node#1244
        self.outpoint_spender: dict[tuple[bytes, int], bytes] = {}
        # (individual feerate, insertion order, wtxid), a min-heap
        # `add_tx` pushes one entry onto and `_pop_worst_wtxid` below
        # reads from instead of `_evict_to_limit` scanning `transactions`
        # whole -- the other O(n) factor btclib-org/btclib-node#441
        # measured and deliberately left alone, #457 being where this
        # heap is argued and measured both ways. A wtxid's feerate is
        # fixed at push time and never mutated in place -- there is no
        # fee-bump or replace-by-fee path in this mempool to change what
        # an entry already held pays -- but a wtxid can still leave and
        # come back (a reorg's own `_reconcile_mempool_for_reorg`, in
        # `main.py`, is one path that does this), and a heap entry from
        # its first spell is not deleted when it leaves, only ignored
        # once found stale. `_heap_current_seq` below is what tells that
        # entry apart from the fresh one its second spell pushes;
        # `_pop_worst_wtxid` is where a mismatch, not mere membership in
        # `transactions`, is what discards it, and `_rebuild_feerate_heap`
        # is what keeps every kind of leftover from growing without
        # bound across a mempool's whole lifetime, most of which is
        # spent under its limit rather than evicting anything -- see
        # both docstrings.
        self._feerate_heap: list[tuple[Fraction, int, bytes]] = []
        # wtxid -> the second element of whichever `_feerate_heap` entry
        # is this wtxid's own current one -- the value `add_tx` just
        # pushed, or the value `_rebuild_feerate_heap` just re-assigned,
        # never one a since-superseded push or rebuild left behind. A
        # popped heap entry answers this call correctly (not merely "is
        # this wtxid still held") only once its own second element is
        # checked against this: a re-add's fresh entry and its own
        # first spell's leftover entry both name a wtxid `transactions`
        # currently holds, and only one of the two is the current entry.
        self._heap_current_seq: dict[bytes, int] = {}
        self.size: int = 0
        self.bytesize: int = 0
        # Core's own `DEFAULT_MAX_MEMPOOL_SIZE_MB`
        # (`src/kernel/mempool_options.h`, at bitcoin/bitcoin@58a7869f86) is
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

        # Core's own `m_recent_rejects` (`src/node/txdownloadman_impl.h`,
        # at bitcoin/bitcoin@4519933391): every wtxid `mark_rejected`
        # below has recorded, oldest first, so a resubmission is dropped
        # before it reaches `interpreter.check_transaction`'s own
        # two-flag-set verification again -- `p2p.callbacks.tx` is the
        # only caller. `_recent_rejects_order` is what makes the set
        # bounded: a plain `set` has no eviction of its own, and
        # `deque(maxlen=...)` would silently drop the wtxid that falls
        # off the far end without telling this class to drop it from the
        # set as well, which is why the two are kept apart and
        # `mark_rejected` retires an entry from both together rather
        # than trusting the deque to do it alone. btclib-org/btclib-node#845
        self._recent_rejects: set[bytes] = set()
        self._recent_rejects_order: deque[bytes] = deque()

    def is_full(self) -> bool:
        """Whether `bytesize` has already reached `bytesize_limit`."""
        return self.bytesize >= self.bytesize_limit

    def get_missing(
        self, transactions: Iterable[bytes], *, wtxid: bool = False
    ) -> list[bytes]:
        """Return every id in `transactions` this mempool does not hold."""
        # No `is_full` guard: that used to answer every request with
        # nothing at all once past the limit, which was the wall
        # `_evict_to_limit` now removes -- Core's own request tracking
        # never consults mempool occupancy before asking either, letting
        # `add_tx` decide per transaction instead. btclib-org/btclib-node#294
        index = self.transactions if wtxid else self.txid_index
        return [tx_id for tx_id in transactions if tx_id not in index]

    def get_tx(self, txid: bytes, *, wtxid: bool = False) -> Tx | None:
        """Return the transaction stored under `txid` (or wtxid), or `None`."""
        key = txid if wtxid else self.txid_index.get(txid)
        if key is None:
            return None
        return self.transactions.get(key)

    def was_recently_rejected(self, wtxid: bytes) -> bool:
        """Whether `mark_rejected` has recorded `wtxid` since the last block.

        `p2p.callbacks.tx`'s own guard against paying
        `interpreter.check_transaction`'s two-flag-set verification a
        second time for a resubmission of a candidate this mempool has
        already refused once.
        """
        return wtxid in self._recent_rejects

    def mark_rejected(self, wtxid: bytes) -> None:
        """Record a mempool candidate's own refusal, oldest evicted first.

        `p2p.callbacks.tx` calls this from its own `except
        BTClibValueError`, for every refusal `verify_mempool_acceptance`
        can make except `MissingPrevoutError` -- a relay-policy-only one
        exactly as much as a genuine consensus one, since Core bounds a
        resubmission of either the same way. `note_block_connected`
        below clears the whole cache, the same trigger Core's own
        `ActiveTipChange` (`src/node/txdownloadman_impl.cpp`, at
        bitcoin/bitcoin@4519933391) resets `m_recent_rejects` on: a
        refusal recorded here can turn on the chain tip -- finality, a
        sequence lock, coinbase maturity -- and stop holding once that
        tip moves, so nothing here can outlive the block that might
        invalidate it.

        Keyed on wtxid alone, matching Core's own general case
        (`RecentRejectsFilter().insert(ptx->GetWitnessHash())`,
        `src/node/txdownloadman_impl.cpp`, same commit), not Core's
        narrower `TX_INPUTS_NOT_STANDARD` special case, which also
        records the txid because that one failure is provably
        independent of the witness -- nothing here tells a
        witness-dependent refusal from a witness-independent one, every
        refusal reaching this call through the same `except` clause. So
        a sender that mutates the witness of an already-refused
        candidate (a different trailing push, say) draws a different
        wtxid and is verified again in full: the same gap Core's own
        filter leaves open for the identical reason, not one specific
        to this mempool. btclib-org/btclib-node#845
        """
        if wtxid in self._recent_rejects:
            return
        if len(self._recent_rejects_order) >= _RECENT_REJECTS_CAPACITY:
            oldest = self._recent_rejects_order.popleft()
            self._recent_rejects.discard(oldest)
        self._recent_rejects_order.append(wtxid)
        self._recent_rejects.add(wtxid)

    # Don't need lock because handled in same thread
    def add_tx(
        self, tx: Tx, fee: int = 0, vsize: int | None = None, *, height: int = 0
    ) -> bool:
        """Add `tx`, evict past the limit, and say whether it stuck.

        A no-op, returning `False`, for a txid already held or a
        transaction spending an outpoint one held already spends. Otherwise
        added provisionally and run through `_evict_to_limit`, which
        takes it right back out if it is itself the worst entry left
        once trimming is done -- so the return value is `False` there
        too, exactly as it would be for an outright refusal.

        `height` defaults to 0 for the same callers `fee` and `vsize`
        default for: every production caller passes the active chain's
        own tip height, `self.heights`' own docstring above is where
        Core's own convention for it -- and its own name,
        `main.verify_mempool_acceptance`'s `spend_height` being one past
        it -- is argued.
        """
        # `fee` defaults to 0 rather than being required, for the
        # callers -- mostly in tests -- that add a transaction without
        # ever asking what it pays; every production caller has just
        # computed the real one out of main.verify_mempool_acceptance
        # and passes it explicitly. `vsize` defaults to `tx.vsize` for
        # the same callers: Core's size with no sigops counted, which
        # needs the prevouts only verification reads.
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
        # at bitcoin/bitcoin@58a7869f86): if this transaction is itself the
        # worst one held once trimming is done, eviction takes it right
        # back out and the return value is `False` here exactly as it
        # was for the old outright refusal -- `rpc/callbacks.py`'s own
        # "mempool full", Core's, answers that case whether it is reached this
        # way or the old way. A transaction already held under this
        # txid, same witness or not, is still a no-op that never touches
        # bytesize at all, for a caller that skipped
        # `main.verify_mempool_acceptance`, which refuses it first.
        # btclib-org/btclib-node#294
        wtxid, txid = tx.hash, tx.id
        if txid in self.txid_index:
            return False
        outpoints = [(vin.prev_out.tx_id, vin.prev_out.vout) for vin in tx.vin]
        if any(outpoint in self.outpoint_spender for outpoint in outpoints):
            # a caller that skipped `main.verify_mempool_acceptance`, whose
            # `check_replacement` call refuses this first
            return False
        for outpoint in outpoints:
            self.outpoint_spender[outpoint] = wtxid
        self.transactions[wtxid] = tx
        self.txid_index[txid] = wtxid
        self.fees[wtxid] = fee
        self.vsizes[wtxid] = tx.vsize if vsize is None else vsize
        self.entry_times[wtxid] = time.time()
        self.heights[wtxid] = height
        for vin in tx.vin:
            self.spent_by.setdefault(vin.prev_out.tx_id, set()).add(wtxid)
        self.size += 1
        self.bytesize += self.vsizes[wtxid]
        self.sequence += 1
        # `self.sequence`, already bumped once above and unique to this
        # call -- it never repeats and only ever grows -- is this heap's
        # own tie-breaker too, so a second counter kept only for this is
        # not needed: two equal-feerate entries pop in the order they
        # were pushed, `min`'s own stability over `self.transactions`'
        # insertion order before this heap existed. It also doubles as
        # this wtxid's current heap entry's own identifier: a wtxid
        # re-added after leaving overwrites `_heap_current_seq[wtxid]`
        # with this call's own value, so the entry a leftover, unpopped
        # heap tuple from its first spell still carries stops matching.
        self._heap_current_seq[wtxid] = self.sequence
        heapq.heappush(
            self._feerate_heap,
            (Fraction(fee, self.vsizes[wtxid]), self.sequence, wtxid),
        )
        self._evict_to_limit()
        return wtxid in self.transactions

    def remove_tx(self, tx: Tx) -> None:
        """Remove `tx` by txid, a no-op if this mempool does not hold it."""
        txid = tx.id
        if txid in self.txid_index:
            self._pop(self.txid_index[txid])

    def contains_tx(self, tx: Tx) -> bool:
        """Whether `tx`'s own wtxid is currently held."""
        return tx.hash in self.transactions

    def _replaced(self, tx: Tx) -> set[bytes]:
        """Return what spends an outpoint `tx` spends, with its descendants.

        Core's own `all_conflicts`, `GetEntriesForConflicts`' union of
        `CalculateDescendants` over the direct conflicts
        (`src/policy/rbf.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag).
        """
        outpoints = ((vin.prev_out.tx_id, vin.prev_out.vout) for vin in tx.vin)
        conflicts = {
            self.outpoint_spender[outpoint]
            for outpoint in outpoints
            if outpoint in self.outpoint_spender
        }
        return set().union(*(self._descendants(wtxid) for wtxid in conflicts))

    def check_replacement(self, tx: Tx, fee: int, vsize: int) -> None:
        """Refuse `tx` if it spends an outpoint a held transaction spends.

        Core replaces the held transactions where the candidate pays for
        them (`ReplacementChecks`, `src/validation.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag); this mempool has
        no replacement, so every conflicting candidate is refused. Where
        Core's `PaysForRBF` (`src/policy/rbf.cpp`, same commit) would
        refuse it too, it is refused in the same words: "insufficient
        fee", with a fee under the conflicts' and their descendants', or
        an increase under the incremental relay fee for `vsize`, the
        candidate's sigop-adjusted size (btclib-org/btclib-node#1357). A
        candidate that pays for them, which Core may accept, is refused
        "bip125-replacement-disallowed", Core's reason where it allows no
        replacement; replacing is btclib-org/btclib-node#1334.
        btclib-org/btclib-node#1244
        """
        replaced = self._replaced(tx)
        if not replaced:
            return
        original = sum(self.fees[wtxid] for wtxid in replaced)
        txid = tx.id.hex()
        if fee < original:
            details = (
                f"rejecting replacement {txid}, less fees than conflicting txs; "
                f"{format_money(fee)} < {format_money(original)}"
            )
            reason = "insufficient fee"
            raise TxRejectedError(reason, details)
        relay_fee = fee_from_vsize(vsize, _INCREMENTAL_RELAY_FEE_RATE)
        if fee - original < relay_fee:
            details = (
                f"rejecting replacement {txid}, not enough additional fees to "
                f"relay; {format_money(fee - original)} < {format_money(relay_fee)}"
            )
            reason = "insufficient fee"
            raise TxRejectedError(reason, details)
        reason = "bip125-replacement-disallowed"
        raise TxRejectedError(reason)

    def remove_conflicts(self, tx: Tx) -> None:
        """Remove what spends an outpoint `tx` spends, with its descendants.

        Core's own `removeConflicts`, which `removeForBlock` calls for
        every transaction of a connected block (`src/txmempool.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a held spend of a
        coin the block spent can never confirm. Called after `remove_tx`
        has taken `tx` itself out, so every spender left is a conflict.
        btclib-org/btclib-node#1244
        """
        for victim in self._replaced(tx):
            self._pop(victim)

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
        fee = self.fees.get(wtxid)
        if fee is None:
            return True
        vsize = self.vsizes[wtxid]
        return fee >= fee_from_vsize(vsize, FeeRate(sats_per_kvbyte=min_fee_rate))

    def _pop(self, wtxid: bytes) -> Tx:
        """Remove one entry by wtxid and return the transaction removed.

        The one place every removal, `remove_tx` and eviction alike,
        updates the bookkeeping the indices and counters above carry --
        so they never drift the way two independent copies of the same
        accounting would. `_feerate_heap` is the exception: a removal
        through `remove_tx` leaves that wtxid's own entry there, stale,
        because finding and dropping one buried entry out of a heap is
        itself an O(n) scan -- exactly the cost this index exists to
        avoid paying on every acceptance. `_heap_current_seq` is what
        marks it stale despite still being physically present --
        dropping this wtxid's own entry here is what a re-add's later
        push has to overwrite instead of finding absent -- and
        `_rebuild_feerate_heap` is what the check below hands the
        physical leftover to, rather than letting it accumulate for as
        long as this mempool runs.
        """
        tx = self.transactions.pop(wtxid)
        self.txid_index.pop(tx.id, None)
        self.fees.pop(wtxid, None)
        vsize = self.vsizes.pop(wtxid)
        self.entry_times.pop(wtxid, None)
        self.heights.pop(wtxid, None)
        # Core's own `removeUnchecked` discards it unconditionally on
        # every way a transaction leaves the mempool -- eviction,
        # confirmation, a conflict -- not only through `remove_tx`, the
        # same way this call is not only reached from `remove_tx` here.
        # btclib-org/btclib-node#1421
        self.unbroadcast.discard(tx.id)
        self._heap_current_seq.pop(wtxid, None)
        # A set of the spent txids first, not a loop over `tx.vin` itself:
        # two inputs of one transaction spending two outputs of the same
        # earlier one are not unusual, and `add_tx` below records that
        # txid once regardless (`set.add` is idempotent), so popping it
        # twice here would `del` an already-deleted `spent_by` entry on
        # the second `vin` and raise `KeyError` on a transaction that
        # never did anything wrong.
        for vin in tx.vin:
            del self.outpoint_spender[vin.prev_out.tx_id, vin.prev_out.vout]
        for spent_txid in {vin.prev_out.tx_id for vin in tx.vin}:
            spenders = self.spent_by[spent_txid]
            spenders.discard(wtxid)
            if not spenders:
                del self.spent_by[spent_txid]
        self.size -= 1
        self.bytesize -= vsize
        self.sequence += 1
        # Bounds the heap at twice the size it would be with no stale
        # entries in it at all: a wtxid removed here without its own
        # heap entry ever being popped (every removal but the one
        # `_pop_worst_wtxid` itself just consumed) is one more entry
        # `len(self._feerate_heap)` counts and `self.size` no longer
        # does, and this is the one place, alongside `add_tx`'s own
        # push, that both numbers are already in hand to compare.
        if len(self._feerate_heap) > 2 * self.size:
            self._rebuild_feerate_heap()
        return tx

    def _descendants(self, wtxid: bytes) -> set[bytes]:
        """Return wtxid and every mempool transaction depending on it.

        `verify_mempool_acceptance` (`main.py`) admits a child whose
        parent is only in this mempool, not yet confirmed -- so evicting
        a parent without what spends it would leave a transaction here
        whose own prevout resolves nowhere. Answered from `spent_by`,
        kept up to date in `add_tx` and `_pop` rather than rebuilt here:
        this walks the package on the frontier, once per element of it,
        instead of the whole mempool once per element -- the O(n * k)
        cost btclib-org/btclib-node#441 measured, `n` being every entry
        this mempool holds and `k` the size of the package being walked.
        A second index does mean a second place the accounting above
        could drift, the risk the comment this replaces weighed against
        the scan -- `_pop` being the single place every removal updates
        it is what keeps that from happening, the same invariant that
        already holds `txid_index` and `fees` to the dict they index.
        `prev_out.tx_id` is a txid, matching what `txid_index` keys
        transactions by and what `verify_mempool_acceptance`'s own
        mempool lookup reads a prevout by. btclib-org/btclib-node#294
        """
        root_txid = self.transactions[wtxid].id
        descendants = {wtxid}
        frontier = [root_txid]
        while frontier:
            txid = frontier.pop()
            for candidate_wtxid in self.spent_by.get(txid, ()):
                if candidate_wtxid in descendants:
                    continue
                descendants.add(candidate_wtxid)
                frontier.append(self.transactions[candidate_wtxid].id)
        return descendants

    def _ancestors(self, wtxid: bytes) -> set[bytes]:
        """Return wtxid and every mempool transaction it spends, transitively.

        `_descendants`'s own mirror: walked from `tx.vin` rather than
        from `spent_by`, through `txid_index` to find each parent still
        held rather than confirmed. Terminates the same way
        `_descendants` does -- a transaction's own inputs are always an
        earlier transaction's own outputs, never its own, so the walk
        never revisits a wtxid it has not already added to the set that
        guards it. `getmempoolentry`'s own `ancestorcount`,
        `ancestorsize` and `fees.ancestor`, and `is_bip125_replaceable`
        below, are what read it. btclib-org/btclib-node#1397
        """
        ancestors = {wtxid}
        frontier = list(self.transactions[wtxid].vin)
        while frontier:
            vin = frontier.pop()
            parent_wtxid = self.txid_index.get(vin.prev_out.tx_id)
            if parent_wtxid is None or parent_wtxid in ancestors:
                continue
            ancestors.add(parent_wtxid)
            frontier.extend(self.transactions[parent_wtxid].vin)
        return ancestors

    def is_bip125_replaceable(self, wtxid: bytes) -> bool:
        """Whether `wtxid`, or an unconfirmed ancestor, signals BIP125 opt-in.

        Core's own `IsRBFOptIn`/`SignalsOptInRBF` (`src/policy/rbf.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): any input sequence
        number under `MAX_BIP125_RBF_SEQUENCE`, on this transaction or on
        an ancestor this mempool still holds unconfirmed -- a child of a
        replaceable parent is itself replaceable, its own parent being
        free to leave and be replaced by something that double-spends it
        too. This mempool has no replacement of its own yet
        (`check_replacement`'s own docstring, btclib-org/btclib-node#1334),
        so this only ever answers the signal, the way Core's own
        `getmempoolentry` does whether or not `-mempoolreplacement` is
        set to allow acting on it.
        """
        return any(
            vin.sequence < _MAX_BIP125_RBF_SEQUENCE
            for ancestor_wtxid in self._ancestors(wtxid)
            for vin in self.transactions[ancestor_wtxid].vin
        )

    def entry(self, wtxid: bytes) -> MempoolEntry:
        """Return `wtxid`'s own `getmempoolentry` accounting.

        Core's own `entryToJSON` (`src/rpc/mempool.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), less `chunkweight`
        and the `fees` object's own `chunk`: both `GetMainChunkFeerate`'s,
        a cluster mempool's own linearization this mempool does not
        carry, the same reason `rpc.callbacks.get_mempool_info` leaves
        out every field a cluster graph would back. `modified` is `base`
        unchanged: Core's own difference between the two is
        `prioritisetransaction`'s fee delta, which this tree does not
        serve. `depends` and `spent_by` are each this transaction's own
        direct mempool parents and children -- `tx.vin` filtered to
        `txid_index`, and `self.spent_by` itself -- not the transitive
        closure `_ancestors`/`_descendants` walk for the counts and
        sizes beside them, matching Core's own `setDepends`/`GetChildren`.
        """
        tx = self.transactions[wtxid]
        ancestors = self._ancestors(wtxid)
        descendants = self._descendants(wtxid)
        depends = sorted(
            {vin.prev_out.tx_id for vin in tx.vin} & self.txid_index.keys()
        )
        spent_by = sorted(
            self.transactions[child].id for child in self.spent_by.get(tx.id, ())
        )
        fee = self.fees[wtxid]
        return MempoolEntry(
            vsize=self.vsizes[wtxid],
            weight=tx.weight,
            time=int(self.entry_times[wtxid]),
            height=self.heights[wtxid],
            wtxid=wtxid,
            fee=fee,
            modified_fee=fee,
            ancestor_count=len(ancestors),
            ancestor_size=sum(self.vsizes[w] for w in ancestors),
            ancestor_fees=sum(self.fees[w] for w in ancestors),
            descendant_count=len(descendants),
            descendant_size=sum(self.vsizes[w] for w in descendants),
            descendant_fees=sum(self.fees[w] for w in descendants),
            depends=depends,
            spent_by=spent_by,
            bip125_replaceable=self.is_bip125_replaceable(wtxid),
            unbroadcast=tx.id in self.unbroadcast,
        )

    def mark_broadcast_locally(self, txid: bytes) -> None:
        """Add `txid` to the unbroadcast set, Core's own `AddUnbroadcastTx`.

        Called only where Core calls it: once a transaction submitted
        through `sendrawtransaction` is kept, never for one a peer handed
        this node over the wire -- `BroadcastTransaction`'s own
        `MEMPOOL_AND_BROADCAST_TO_ALL` branch is the one call site Core
        has for it (`src/node/transaction.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), and `net_processing.cpp`'s
        own p2p acceptance path has none. A txid this mempool does not
        hold is left alone, the way Core's own sanity check leaves it
        out of the set rather than inserting it. btclib-org/btclib-node#1421
        """
        if txid in self.txid_index:
            self.unbroadcast.add(txid)

    def mark_broadcast(self, txid: bytes) -> None:
        """Discard `txid` from `unbroadcast`, Core's own `RemoveUnbroadcastTx`.

        Called once some peer's own `getdata` is served for it
        (`net_processing.cpp`'s `m_mempool.RemoveUnbroadcastTx`, same
        commit) -- `_pop` above is the other call site, Core's own
        `removeUnchecked` discarding it unconditionally on every way a
        transaction leaves this mempool. btclib-org/btclib-node#1421
        """
        self.unbroadcast.discard(txid)

    def _pop_worst_wtxid(self) -> bytes:
        """Pop and return the currently held wtxid of the lowest feerate.

        `_feerate_heap` holds one entry per wtxid this mempool has ever
        held a push for, and a push happens once per `add_tx` call and
        once per `_rebuild_feerate_heap` sweep -- never updated in
        place, since a wtxid's feerate cannot change while it is held,
        there being no fee-bump or replace-by-fee path into this
        mempool. What can change is whether a given physical entry is
        still the one this wtxid is currently held under: `add_tx` on a
        wtxid that left and came back pushes a fresh entry with a fresh
        second element and overwrites `_heap_current_seq[wtxid]` to
        match it, so an older entry for the same wtxid, still physically
        in the heap because `_pop` never goes looking for it, now names
        a second element `_heap_current_seq` no longer agrees with. This
        popped a wtxid still in `self.transactions` and got the eviction
        order wrong on exactly that path -- checking membership alone
        cannot tell the two entries apart, only checking the entry
        `_heap_current_seq` currently names for that wtxid can -- and
        that check is the fix (btclib-org/btclib-node#457, second
        round).

        This discards every entry that fails the check, in ascending
        feerate order, until one that passes surfaces -- which always
        happens before the heap runs out. For every wtxid still held,
        exactly one entry in the heap has the second element
        `_heap_current_seq` currently names for it, pushed either by the
        `add_tx` call that last (re-)added it or by the most recent
        `_rebuild_feerate_heap` sweep since; that entry cannot have been
        popped already, since popping the entry matching a wtxid's
        current mapping only ever happens here, at the moment this
        method returns that wtxid to be evicted, and eviction is what
        removes the wtxid (and its mapping) from `self.transactions` --
        so the invariant is "at least one matching entry per currently
        held wtxid", not "exactly one": a re-add can leave a second,
        now-permanently-stale entry for the same wtxid behind, and nothing
        needs it gone before this can terminate correctly.
        """
        while True:
            _, seq, wtxid = heapq.heappop(self._feerate_heap)
            if self._heap_current_seq.get(wtxid) == seq:
                return wtxid

    def _rebuild_feerate_heap(self) -> None:
        """Re-derive `_feerate_heap` from scratch, dropping every stale entry.

        `self.transactions` and `self.fees` are inserted into and
        deleted from together, in `add_tx` and `_pop`, so their key
        order agrees at every point this can run -- `enumerate` over
        one, read against the other, reproduces each currently held
        wtxid's own original relative insertion order without this
        mempool having kept a separate record of it. Every entry built
        here is the new current one: `_heap_current_seq[wtxid]` is
        overwritten to the same index the rebuilt entry carries, so a
        heap entry from before this rebuild -- superseded here whether
        or not it had already gone stale on its own -- stops matching
        exactly the way an ordinary re-add's leftover does. The indices
        handed out this way, `0` upward, are smaller than `self.sequence`
        can ever be read as here: `self.sequence` only ever grows, by at
        least one per add and one per removal, so it already exceeds
        `self.size` -- and therefore every index below it -- at any
        point `_pop`'s own check calls this. A push after this rebuild
        still carries the current, larger `self.sequence`, so it still
        breaks a tie against a rebuilt entry the same way it would have
        against the entry the rebuild replaced.
        """
        self._feerate_heap = []
        for index, (wtxid, fee) in enumerate(self.fees.items()):
            self._heap_current_seq[wtxid] = index
            self._feerate_heap.append((Fraction(fee, self.vsizes[wtxid]), index, wtxid))
        heapq.heapify(self._feerate_heap)

    def _evict_to_limit(self) -> None:
        """Evict by worst individual feerate until back at the limit.

        Core's own `TrimToSize` (`src/txmempool.cpp:909`,
        at bitcoin/bitcoin@58a7869f86) evicts the worst *chunk*, a package
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
        longest-*currently*-held entry: `_heap_current_seq` (`add_tx`,
        `_rebuild_feerate_heap`) names each wtxid's own tiebreak value
        by when it was last (re-)added, not by when it was first ever
        seen, reproducing what `self.transactions`' own insertion order
        and `min`'s own stability gave before this heap replaced that
        scan -- a wtxid that left and came back sorts as newly held,
        the same way a dict does on a delete followed by a fresh insert
        (btclib-org/btclib-node#457). This is a deliberate, argued
        departure from Core, unlike `removed_rate` below, which matches
        it.
        """
        while self.bytesize > self.bytesize_limit and self.transactions:
            worst = self._pop_worst_wtxid()
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
            package_vsize = sum(self.vsizes[w] for w in package)
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

        Also clears `mark_rejected`'s own cache, whichever peer's
        transaction the connected block held or did not: a reorg's own
        multi-block connect loop calls this once per block added, so the
        cache is emptied at least once for any active tip change, the
        same event Core's `ActiveTipChange` resets `m_recent_rejects` on.
        """
        self._last_rolling_fee_update = time.time()
        self._block_since_last_rolling_fee_bump = True
        self._recent_rejects.clear()
        self._recent_rejects_order.clear()

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
