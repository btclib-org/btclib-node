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

import hashlib
import heapq
import time
from collections import deque
from contextlib import contextmanager
from fractions import Fraction
from typing import TYPE_CHECKING, NamedTuple

from btclib.fee import FeeRate, fee_from_vsize

from btclib_node.config import DEFAULT_INCREMENTAL_RELAY_FEERATE
from btclib_node.exceptions import TxRejectedError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from btclib.tx.tx import Tx

    from btclib_node.log import Logger

__all__ = ["Mempool", "MempoolEntry", "format_money", "package_hash"]

# Core's own `MAX_BIP125_RBF_SEQUENCE` (`src/policy/rbf.h`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `SignalsOptInRBF`'s own
# bound, an input below it opting a transaction into BIP125 replacement.
_MAX_BIP125_RBF_SEQUENCE = 0xFFFFFFFE

# BIP431's TRUC policy (`src/policy/truc_policy.h`, same commit): the
# version it applies to and the sizes it bounds. Core's
# `SingleTRUCChecks` is specialized for an ancestor and a descendant limit
# of two, a parent and one child, and so is `Mempool.check_truc`.
_TRUC_VERSION = 3
_TRUC_MAX_VSIZE = 10_000
_TRUC_CHILD_MAX_VSIZE = 1_000
_TRUC_ANCESTORS = 2


def _named(tx: Tx) -> str:
    """Return `tx` as Core's TRUC messages name it."""
    return f"tx {tx.id.hex()} (wtxid={tx.hash.hex()})"


# `DEFAULT_CLUSTER_LIMIT` and `DEFAULT_CLUSTER_SIZE_LIMIT_KVB`
# (`src/policy/policy.h`, same commit): the most transactions, and
# thousands of vbytes, one cluster may hold. btclib-org/btclib-node#1383
_CLUSTER_LIMIT = 64
_CLUSTER_VSIZE_LIMIT = 101 * 1000


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

# Core's own `ROLLING_FEE_HALFLIFE` (`src/txmempool.h:212`, same commit):
# seconds for the rolling minimum to decay by half once it is decaying at
# all, shortened as this mempool empties -- `get_min_fee_rate` below.
_ROLLING_FEE_HALFLIFE = 60 * 60 * 12

_COIN = 100_000_000

# `CAmount` is an `int64_t`, and `SaturatingAdd` clamps a delta to it
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def _saturate(amount: int) -> int:
    """Clamp `amount` to an `int64`, Core's `SaturatingAdd` of two."""
    return min(max(amount, _INT64_MIN), _INT64_MAX)


def format_money(amount: int) -> str:
    """Core's own `FormatMoney` for an amount never negative here.

    Eight decimals, right-trimmed of zeros down to two
    (`src/util/moneystr.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag): 5000 satoshi is "0.00005", one bitcoin "1.00".
    """
    whole, fraction = divmod(amount, _COIN)
    decimals = f"{fraction:08d}".rstrip("0").ljust(2, "0")
    return f"{whole}.{decimals}"


def package_hash(wtxids: Iterable[bytes]) -> bytes:
    """Return Core's `GetPackageHash` of a package's wtxids.

    The SHA-256 of the wtxids sorted ascending by their display bytes
    (`lexicographical_compare` over reverse iterators), each hashed as the
    bytes Core holds, the reverse of the display bytes this tree keeps
    (`src/policy/packages.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag). It does not depend on the order the package is given in.
    """
    held = [wtxid[::-1] for wtxid in sorted(wtxids)]
    return hashlib.sha256(b"".join(held)).digest()


def _remember(members: set[bytes], order: deque[bytes], key: bytes) -> None:
    """Add `key` to a bounded cache, the oldest leaving at the capacity.

    `deque(maxlen=...)` would drop the key that falls off the far end
    without telling `members`, so the two are retired together.
    """
    if key in members:
        return
    if len(order) >= _RECENT_REJECTS_CAPACITY:
        members.discard(order.popleft())
    order.append(key)
    members.add(key)


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
    is read. `_recent_rejects_reconsiderable` and its order are the same
    for the refusals a package can undo.
    """

    def __init__(
        self,
        logger: Logger,
        incremental_relay_feerate: FeeRate = DEFAULT_INCREMENTAL_RELAY_FEERATE,
    ) -> None:
        """Start empty, with the rolling minimum feerate at zero, undecayed.

        `incremental_relay_feerate` is `-incrementalrelayfee`
        (`Config.incremental_relay_feerate`): what an eviction round bumps
        the rolling minimum by, above the feerate of whatever it just
        evicted, so a transaction does not requalify at the exact rate
        something was just evicted for, and the extra fee a replacement
        pays. Core keeps it apart from `-minrelaytxfee`, the two merely
        sharing a default.
        """
        self.logger = logger
        self.incremental_relay_feerate = incremental_relay_feerate

        self.transactions: dict[bytes, Tx] = {}
        self.txid_index: dict[bytes, bytes] = {}
        # wtxid -> txid, kept beside `txid_index` so that a read does not
        # serialize the transaction again: `Tx.id` is not cached.
        self.txids: dict[bytes, bytes] = {}
        # wtxid -> fee in satoshi, the sum-of-inputs-less-sum-of-outputs
        # main.verify_mempool_acceptance already computes and would
        # otherwise discard. btclib-org/btclib-node#260
        self.fees: dict[bytes, int] = {}
        # wtxid -> Core's `m_modified_fee`: `fees` and the delta, saturated
        # at the `int64` range at each step as `UpdateModifiedFee` does
        # (`src/kernel/mempool_entry.h`, at bitcoin/bitcoin@9be056a8a7, the
        # v31.1 tag), so it is not always `fees` plus `deltas`, which a
        # delta at the bound shows. btclib-org/btclib-node#1502
        self.modified_fees: dict[bytes, int] = {}
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
        # (modified feerate, push order, wtxid), a min-heap
        # `add_tx` pushes one entry onto and `_pop_worst_wtxid` below
        # reads from instead of `_evict_to_limit` scanning `transactions`
        # whole -- the other O(n) factor btclib-org/btclib-node#441
        # measured and deliberately left alone, #457 being where this
        # heap is argued and measured both ways. A wtxid's feerate is
        # fixed at push time and never mutated in place: `prioritise`,
        # which changes what a held entry is worth, pushes a fresh entry.
        # A wtxid can also leave and
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
        # The last second element `_push_heap` handed out, which `sequence`
        # cannot be: `prioritise` pushes a second entry for a held wtxid
        # without a sequence event, and Core's `GetSequence` does not
        # count it either.
        self._heap_pushes: int = 0
        # txid -> Core's `mapDeltas` (`src/txmempool.h`, at
        # bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the fee delta
        # `prioritisetransaction` added, kept whether or not the
        # transaction is held, never zero. `modified_fee` is its reader.
        # btclib-org/btclib-node#1502
        self.deltas: dict[bytes, int] = {}
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
        # Core's `nTransactionsUpdated` (`src/txmempool.h`, at
        # bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which a
        # `getblocktemplate` long poll waits on: one per transaction added
        # or removed, as `sequence`, and one per block connected or
        # disconnected, through `add_transactions_updated`. Core counts
        # genesis too where it connects it into a fresh data directory;
        # here genesis is seeded at every start, so this starts at 0, as
        # Core's does after a restart.
        self.transactions_updated: int = 0

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
        # two-flag-set verification again -- `p2p.callbacks` is the
        # only caller. `_recent_rejects_order` is what makes the set
        # bounded: a plain `set` has no eviction of its own, and
        # `deque(maxlen=...)` would silently drop the wtxid that falls
        # off the far end without telling this class to drop it from the
        # set as well, which is why the two are kept apart and
        # `mark_rejected` retires an entry from both together rather
        # than trusting the deque to do it alone. btclib-org/btclib-node#845
        self._recent_rejects: set[bytes] = set()
        self._recent_rejects_order: deque[bytes] = deque()
        # Core's `m_lazy_recent_rejects_reconsiderable`, the same size and
        # reset as the cache above: the wtxids refused for a reason a
        # package can undo, and the hashes of the packages refused.
        self._recent_rejects_reconsiderable: set[bytes] = set()
        self._recent_rejects_reconsiderable_order: deque[bytes] = deque()

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

        `p2p.callbacks` calls this for every refusal
        `verify_mempool_acceptance`'s two halves can make except
        `MissingPrevoutError` -- a relay-policy-only one
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
        _remember(self._recent_rejects, self._recent_rejects_order, wtxid)

    def was_recently_rejected_reconsiderable(self, key: bytes) -> bool:
        """Whether `key` was marked reconsiderable since the last block."""
        return key in self._recent_rejects_reconsiderable

    def mark_rejected_reconsiderable(self, key: bytes) -> None:
        """Record a wtxid refused for a reason a package can undo, or a package.

        Core's `RecentRejectsReconsiderableFilter().insert`
        (`src/node/txdownloadman_impl.cpp`, at bitcoin/bitcoin@9be056a8a7, the
        v31.1 tag): the wtxid of a transaction refused as `TX_RECONSIDERABLE`
        is not downloaded or submitted alone again, and the hash of a
        package refused for any reason (`package_hash`) is not tried again.
        Cleared and bounded as `mark_rejected`'s cache is.
        """
        _remember(
            self._recent_rejects_reconsiderable,
            self._recent_rejects_reconsiderable_order,
            key,
        )

    # Don't need lock because handled in same thread
    def add_tx(
        self,
        tx: Tx,
        fee: int = 0,
        vsize: int | None = None,
        *,
        height: int = 0,
        trim: bool = True,
    ) -> bool:
        """Add `tx`, evict past the limit, and say whether it stuck.

        With `trim` false nothing is evicted, and the caller calls `trim`
        itself once it has added what it will: Core's package submission,
        which trims once at the end.

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
        self._insert(tx, fee, vsize, height)
        self._push_heap(wtxid)
        if trim:
            self._evict_to_limit()
        return wtxid in self.transactions

    def trim(self) -> None:
        """Evict past the limit, Core's `LimitMempoolSize`, as `add_tx` does."""
        self._evict_to_limit()

    def add_package(
        self, members: Sequence[tuple[Tx, int, int]], *, height: int
    ) -> bool:
        """Add a package's transactions, parents first, or none, and say which.

        `members` are `(tx, fee, vsize)` of the parents and the child
        paying for them, each already refused by none of
        `main.pre_verify_subpackage`'s checks on this very state. They are
        judged by their aggregate modified feerate where the mempool is over
        its limit: the others are evicted for room, worst first, as long as
        they pay less than the package, and the package is itself the
        eviction once the worst left pays more. Core evicts the worst
        chunk after adding (`LimitMempoolSize` after `SubmitPackage`,
        `src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag),
        which is the same choice with chunks scored by the cluster graph
        this mempool does not hold. `_evict_to_limit`'s own docstring has
        the departure for the others.

        A parent of the package that an eviction takes leaves nothing to
        add, and so does a member left alone by the eviction after the
        add, where a package larger than the limit is evicted.
        """
        fee = sum(fee + self.delta(tx.id) for tx, fee, _ in members)
        size = sum(member[2] for member in members)
        rate = Fraction(fee, size)
        parents = {wtxid for tx, _, _ in members for wtxid in self._parents(tx)}
        while self.bytesize + size > self.bytesize_limit and self.transactions:
            worst = self._pop_worst_wtxid()
            worst_rate = Fraction(self.modified_fee(worst), self.vsizes[worst])
            if rate < worst_rate:
                self._unpop_worst_wtxid(worst)
                self._track_package_removed(
                    float(rate * 1000 + self.incremental_relay_feerate.sats_per_kvbyte)
                )
                return False
            self._evict_chunk(worst)
        if not parents <= self.transactions.keys():
            return False
        for tx, member_fee, vsize in members:
            self._insert(tx, member_fee, vsize, height)
            self._push_heap(tx.hash)
        self._evict_to_limit()
        wtxids = [tx.hash for tx, _, _ in members]
        if all(wtxid in self.transactions for wtxid in wtxids):
            return True
        for wtxid in wtxids:
            self.remove_with_descendants(wtxid)
        return False

    @contextmanager
    def staged(self, tx: Tx, fee: int, vsize: int) -> Iterator[None]:
        """Hold `tx` as if accepted for the block, then take it out again.

        What a package's child is checked against: the mempool its parent
        would leave. Nothing is evicted or announced, the heap is left
        alone, and `sequence` and `transactions_updated` are as they were
        after, so no reader of either sees the parent come and go.
        `tx` is one `main.pre_verify_mempool_acceptance` accepted, or one
        `main.pre_verify_subpackage` stages before asking about conflicts,
        which may conflict with a held transaction.
        """
        sequence, updated = self.sequence, self.transactions_updated
        # a held transaction `tx` conflicts with keeps its claim on the outpoint
        spenders = {
            outpoint: self.outpoint_spender[outpoint]
            for vin in tx.vin
            if (outpoint := (vin.prev_out.tx_id, vin.prev_out.vout))
            in self.outpoint_spender
        }
        self._insert(tx, fee, vsize, height=0)
        try:
            yield
        finally:
            self._pop(tx.hash)
            self.outpoint_spender.update(spenders)
            self.sequence, self.transactions_updated = sequence, updated

    def _insert(self, tx: Tx, fee: int, vsize: int | None, height: int) -> None:
        """Enter `tx` in every index, with no eviction and no heap entry."""
        wtxid, txid = tx.hash, tx.id
        for vin in tx.vin:
            self.outpoint_spender[vin.prev_out.tx_id, vin.prev_out.vout] = wtxid
        self.transactions[wtxid] = tx
        self.txid_index[txid] = wtxid
        self.txids[wtxid] = txid
        self.fees[wtxid] = fee
        self.modified_fees[wtxid] = _saturate(fee + self.delta(txid))
        self.vsizes[wtxid] = tx.vsize if vsize is None else vsize
        self.entry_times[wtxid] = time.time()
        self.heights[wtxid] = height
        for vin in tx.vin:
            self.spent_by.setdefault(vin.prev_out.tx_id, set()).add(wtxid)
        self.size += 1
        self.bytesize += self.vsizes[wtxid]
        self.sequence += 1
        self.transactions_updated += 1

    def _push_heap(self, wtxid: bytes) -> None:
        """Give `wtxid`, just inserted, its entry in the eviction heap."""
        # `_heap_pushes` is unique among the entries this heap holds and
        # is this heap's own tie-breaker too: two equal-feerate entries pop
        # in the order they were pushed, `min`'s own stability over
        # `self.transactions`' insertion order before this heap existed. It
        # also doubles as this wtxid's current heap entry's own identifier: a
        # wtxid re-added after leaving, or prioritised, overwrites
        # `_heap_current_seq[wtxid]` with this call's own value, so the entry
        # a leftover, unpopped heap tuple from before carries stops matching.
        self._heap_pushes += 1
        self._heap_current_seq[wtxid] = self._heap_pushes
        heapq.heappush(
            self._feerate_heap,
            (
                Fraction(self.modified_fee(wtxid), self.vsizes[wtxid]),
                self._heap_pushes,
                wtxid,
            ),
        )

    def remove_tx(self, tx: Tx) -> None:
        """Remove `tx` by txid, a no-op if this mempool does not hold it."""
        txid = tx.id
        if txid in self.txid_index:
            self._pop(self.txid_index[txid])

    def contains_tx(self, tx: Tx) -> bool:
        """Whether `tx`'s own wtxid is currently held."""
        return tx.hash in self.transactions

    def delta(self, txid: bytes) -> int:
        """Return the fee delta of `txid`, held or not, zero where it has none.

        Core's `ApplyDelta` (`src/txmempool.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which its mempool
        acceptance asks of a candidate before it is held.
        btclib-org/btclib-node#1502
        """
        return self.deltas.get(txid, 0)

    def modified_fee(self, wtxid: bytes) -> int:
        """Return the modified fee of the held `wtxid`: its fee and its delta.

        Core's `GetModifiedFee`: what the feerate this mempool evicts by
        reads, and the ancestor and descendant sums. The fee the
        transaction pays, `fees`, stays what a block template adds to the
        coinbase and what BIP133's `feefilter` is held against
        (`GetFee`, `src/node/miner.cpp` and `src/net_processing.cpp`, same
        tag): the delta is a ranking and is never paid.
        btclib-org/btclib-node#1502
        """
        return self.modified_fees[wtxid]

    def prioritise(self, txid: bytes, fee_delta: int) -> None:
        """Add `fee_delta` to the delta of `txid`, held or not.

        Core's `PrioritiseTransaction` (same tag): deltas stack, are
        clamped to an `int64`, and a delta that comes to zero is dropped.
        A held transaction is also counted in `transactions_updated`, as
        `nTransactionsUpdated` is, which a `getblocktemplate` long poll
        waits on, and is pushed on the eviction heap at its new rate.
        Nothing is evicted here: Core trims at the next addition only.
        A delta is not dropped when the transaction is evicted or
        replaced, only when a block holds it or conflicts with it
        (`remove_for_block`).

        Core writes the deltas to `mempool.dat`
        (`src/node/mempool_persist.cpp`). This node keeps no mempool on
        disk, so a restart drops them with it
        (btclib-org/btclib-node#1746). btclib-org/btclib-node#1502
        """
        delta = _saturate(self.delta(txid) + fee_delta)
        if delta:
            self.deltas[txid] = delta
        else:
            self.deltas.pop(txid, None)
        wtxid = self.txid_index.get(txid)
        if wtxid is not None:
            self.modified_fees[wtxid] = _saturate(self.modified_fees[wtxid] + fee_delta)
            self.transactions_updated += 1
            self._push_heap(wtxid)
            self._bound_heap()

    def prioritised(self) -> list[tuple[bytes, int, int | None]]:
        """Return `(txid, delta, modified fee)` for each delta.

        Core's `GetPrioritisedTransactions` (same tag), in the order of its
        `std::map<Txid, CAmount>`: by the txid's internal bytes. The
        modified fee is `None` for a transaction not held.
        btclib-org/btclib-node#1502
        """
        return [
            (
                txid,
                delta,
                self.modified_fee(self.txid_index[txid])
                if txid in self.txid_index
                else None,
            )
            for txid, delta in sorted(self.deltas.items(), key=lambda d: d[0][::-1])
        ]

    def _replaced(self, tx: Tx) -> set[bytes]:
        """Return what spends an outpoint `tx` spends, with its descendants.

        Core's own `all_conflicts`, `GetEntriesForConflicts`' union of
        `CalculateDescendants` over the direct conflicts
        (`src/policy/rbf.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag).
        """
        conflicts = self.direct_conflicts(tx)
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
        replacement; replacing is btclib-org/btclib-node#1334. Both sides of
        the comparison are modified fees, as `PaysForRBF`'s are: `fee` is
        what `tx` pays, and its own delta is added here.
        btclib-org/btclib-node#1244, btclib-org/btclib-node#1502
        """
        replaced = self._replaced(tx)
        if not replaced:
            return
        original = sum(self.modified_fee(wtxid) for wtxid in replaced)
        fee += self.delta(tx.id)
        txid = tx.id.hex()
        if fee < original:
            details = (
                f"rejecting replacement {txid}, less fees than conflicting txs; "
                f"{format_money(fee)} < {format_money(original)}"
            )
            reason = "insufficient fee"
            raise TxRejectedError(reason, details)
        relay_fee = fee_from_vsize(vsize, self.incremental_relay_feerate)
        if fee - original < relay_fee:
            details = (
                f"rejecting replacement {txid}, not enough additional fees to "
                f"relay; {format_money(fee - original)} < {format_money(relay_fee)}"
            )
            reason = "insufficient fee"
            raise TxRejectedError(reason, details)
        reason = "bip125-replacement-disallowed"
        raise TxRejectedError(reason)

    def _parents(self, tx: Tx) -> list[bytes]:
        """Return the wtxids of the held transactions `tx` spends, each once.

        In input order. Core's `CTxMemPool::GetParents` orders them by txid,
        as `_parents_by_txid` does.
        """
        return list(
            dict.fromkeys(
                parent
                for vin in tx.vin
                if (parent := self.txid_index.get(vin.prev_out.tx_id)) is not None
            )
        )

    def _parents_by_txid(self, tx: Tx) -> list[bytes]:
        """Return `_parents(tx)` as Core's `GetParents` does, by txid.

        Core walks a `std::set<Txid>`, so `mempool_parents[0]` is the parent
        with the smallest txid, and `SingleTRUCChecks` and
        `PackageTRUCChecks` refuse on the first that breaks a rule. The set
        compares the 32 bytes as stored, which is `memcmp` over the reverse
        of `Tx.id`, the displayed order.
        btclib-org/btclib-node#1783
        """
        return sorted(
            self._parents(tx), key=lambda wtxid: self.transactions[wtxid].id[::-1]
        )

    def check_truc(self, tx: Tx, vsize: int) -> None:
        """Refuse `tx` if BIP431 does, "TRUC-violation" in Core's words.

        Core's `SingleTRUCChecks` (`src/policy/truc_policy.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), called from
        `PreChecks` with the held parents and the held direct conflicts: a
        version-3 transaction spends only from version-3 ones and the
        reverse, is at most 10,000 vbytes, has at most one held parent
        that has no held parent of its own, is then at most 1,000 vbytes,
        and is the only child its parent has.

        A second child is refused even where it pays to replace the first,
        which Core's sibling eviction would try: this mempool replaces
        nothing, `check_replacement`. What needs the package is
        `check_package_truc`'s.
        btclib-org/btclib-node#1399
        """
        reason = "TRUC-violation"
        parents = self._parents_by_txid(tx)
        truc = tx.version == _TRUC_VERSION
        who = f"tx {tx.id.hex()} (wtxid={tx.hash.hex()})"
        for parent in parents:
            parent_tx = self.transactions[parent]
            held = f"tx {parent_tx.id.hex()} (wtxid={parent_tx.hash.hex()})"
            if truc and parent_tx.version != _TRUC_VERSION:
                details = f"version=3 {who} cannot spend from non-version=3 {held}"
                raise TxRejectedError(reason, details)
            if not truc and parent_tx.version == _TRUC_VERSION:
                details = f"non-version=3 {who} cannot spend from version=3 {held}"
                raise TxRejectedError(reason, details)
        if not truc:
            return
        if vsize > _TRUC_MAX_VSIZE:
            details = (
                f"version=3 {who} is too big: {vsize} > {_TRUC_MAX_VSIZE} virtual bytes"
            )
            raise TxRejectedError(reason, details)
        if not parents:
            return
        parent = parents[0]
        if len(parents) > 1 or len(self._ancestors(parent)) > 1:
            details = f"{who} would have too many ancestors"
            raise TxRejectedError(reason, details)
        if vsize > _TRUC_CHILD_MAX_VSIZE:
            details = (
                f"version=3 child {who} is too big: {vsize} > "
                f"{_TRUC_CHILD_MAX_VSIZE} virtual bytes"
            )
            raise TxRejectedError(reason, details)
        # a child this candidate conflicts with is not counted: whether it
        # pays to replace it is `check_replacement`'s to say
        siblings = self._descendants(parent) - {parent}
        if siblings and not siblings & self.direct_conflicts(tx):
            parent_tx = self.transactions[parent]
            details = (
                f"tx {parent_tx.id.hex()} (wtxid={parent_tx.hash.hex()}) "
                "would exceed descendant count limit"
            )
            raise TxRejectedError(reason, details)

    def check_package_truc(self, package: Sequence[Tx], index: int, vsize: int) -> None:
        """Refuse `package[index]` if BIP431 does of a package, as Core does.

        Core's `PackageTRUCChecks` (`src/policy/truc_policy.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), in its order, with
        the parents the mempool holds and those before `index` in
        `package`: too many ancestors, the ancestors of a held parent, the
        size of a version 3 child, the version of its parent, a sibling or
        a child in the package, and the other children of a held parent.
        A conflict with a held sibling does not excuse it, as it does not in
        Core.
        """
        tx = package[index]
        possible = {vin.prev_out.tx_id for vin in tx.vin}
        held = [self.transactions[wtxid] for wtxid in self._parents_by_txid(tx)]
        in_package = [other for other in package[:index] if other.id in possible]
        if tx.version == _TRUC_VERSION:
            self._check_truc_child(package, tx, vsize, held, in_package)
            return
        reason = "TRUC-violation"
        for parent in [*held, *in_package]:
            if parent.version == _TRUC_VERSION:
                details = (
                    f"non-version=3 {_named(tx)} cannot spend from version=3 "
                    f"{_named(parent)}"
                )
                raise TxRejectedError(reason, details)

    def _check_truc_child(
        self,
        package: Sequence[Tx],
        tx: Tx,
        vsize: int,
        held: list[Tx],
        in_package: list[Tx],
    ) -> None:
        """Refuse a version 3 `tx` of a package, `check_package_truc`'s rest."""
        reason = "TRUC-violation"
        who = _named(tx)
        if vsize > _TRUC_MAX_VSIZE:
            details = (
                f"version=3 {who} is too big: {vsize} > {_TRUC_MAX_VSIZE} virtual bytes"
            )
            raise TxRejectedError(reason, details)
        ancestors = f"{who} would have too many ancestors"
        count = len(in_package) + 1
        if len(held) + count > _TRUC_ANCESTORS:
            raise TxRejectedError(reason, ancestors)
        if held and len(self._ancestors(held[0].hash)) + count > _TRUC_ANCESTORS:
            raise TxRejectedError(reason, ancestors)
        if held or in_package:
            self._check_truc_parent(package, tx, vsize, held, in_package)

    def _check_truc_parent(
        self,
        package: Sequence[Tx],
        tx: Tx,
        vsize: int,
        held: list[Tx],
        in_package: list[Tx],
    ) -> None:
        """Refuse a version 3 `tx` with a parent, `check_package_truc`'s end."""
        reason = "TRUC-violation"
        who = _named(tx)
        if vsize > _TRUC_CHILD_MAX_VSIZE:
            details = (
                f"version=3 child {who} is too big: {vsize} > "
                f"{_TRUC_CHILD_MAX_VSIZE} virtual bytes"
            )
            raise TxRejectedError(reason, details)
        parent = held[0] if held else in_package[0]
        if parent.version != _TRUC_VERSION:
            details = (
                f"version=3 {who} cannot spend from non-version=3 {_named(parent)}"
            )
            raise TxRejectedError(reason, details)
        for other in package:
            if other is tx:
                continue
            for vin in other.vin:
                if vin.prev_out.tx_id == parent.id:
                    details = f"{_named(parent)} would exceed descendant count limit"
                    raise TxRejectedError(reason, details)
                if vin.prev_out.tx_id == tx.id:
                    details = f"{_named(other)} would have too many ancestors"
                    raise TxRejectedError(reason, details)
        if held and len(self._descendants(parent.hash)) > 1:
            details = f"{_named(parent)} would exceed descendant count limit"
            raise TxRejectedError(reason, details)

    def direct_conflicts(self, tx: Tx) -> set[bytes]:
        """Return the wtxids of the held spenders of what `tx` spends."""
        outpoints = ((vin.prev_out.tx_id, vin.prev_out.vout) for vin in tx.vin)
        return {
            self.outpoint_spender[outpoint]
            for outpoint in outpoints
            if outpoint in self.outpoint_spender
        }

    def cluster(
        self, seeds: Iterable[bytes], *, max_count: int, max_vsize: int
    ) -> tuple[set[bytes], int]:
        """Return the held transactions connected to `seeds`, with their vsize.

        A cluster is a connected component of the graph whose edges are
        the spends among held transactions, followed up through each
        input's parent and down through `spent_by`. The walk stops at the
        first transaction past `max_count` transactions or `max_vsize`
        vbytes, so a caller tests `len(members) > max_count or vsize >
        max_vsize` and a cluster past a limit costs no more than the
        limit. btclib-org/btclib-node#1383
        """
        members: set[bytes] = set()
        vsize = 0
        frontier = list(seeds)
        while frontier and len(members) <= max_count and vsize <= max_vsize:
            wtxid = frontier.pop()
            if wtxid in members:
                continue
            members.add(wtxid)
            vsize += self.vsizes[wtxid]
            tx = self.transactions[wtxid]
            frontier.extend(self.spent_by.get(tx.id, ()))
            frontier.extend(self._parents(tx))
        return members, vsize

    def check_cluster(self, tx: Tx, vsize: int) -> None:
        """Refuse `tx` if its cluster would pass Core's limits.

        Core's `CheckMemPoolPolicyLimits` (`src/validation.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), "too-large-cluster"
        for more than 64 transactions or 101,000 vbytes in the cluster
        `tx` would join. Core sizes a cluster by sigop-adjusted weight and
        this mempool keeps the vsize that weight rounds up to, so a
        cluster within less than four weight units per transaction of the
        limit is refused here and accepted there. btclib-org/btclib-node#1383
        """
        max_count = _CLUSTER_LIMIT - 1
        max_vsize = _CLUSTER_VSIZE_LIMIT - vsize
        members, size = self.cluster(
            self._parents(tx), max_count=max_count, max_vsize=max_vsize
        )
        if len(members) > max_count or size > max_vsize:
            reason = "too-large-cluster"
            raise TxRejectedError(reason)

    def remove_conflicts(self, tx: Tx) -> None:
        """Remove what spends an outpoint `tx` spends, with its descendants.

        Core's own `removeConflicts`, which `removeForBlock` calls for
        every transaction of a connected block (`src/txmempool.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a held spend of a
        coin the block spent can never confirm. Called after `remove_tx`
        has taken `tx` itself out, so every spender left is a conflict.
        The direct spenders lose their fee delta and their descendants keep
        theirs, as in Core's `removeConflicts`.
        btclib-org/btclib-node#1244, btclib-org/btclib-node#1502
        """
        for conflict in self.direct_conflicts(tx):
            self.clear_prioritisation(self.transactions[conflict].id)
        for victim in self._replaced(tx):
            self._pop(victim)

    def clear_prioritisation(self, txid: bytes) -> None:
        """Drop the fee delta of `txid`, Core's `ClearPrioritisation`."""
        self.deltas.pop(txid, None)

    def remove_for_block(self, transactions: Iterable[Tx]) -> None:
        """Remove what a connected block holds or conflicts with, deltas too.

        Core's `removeForBlock` (`src/txmempool.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag) takes each transaction
        out, then what conflicts with it, then clears its fee delta. The
        delta of a transaction a block holds is gone with it, so the
        transaction has none if a reorg returns it to the mempool; one set
        after the block connected is applied then. Nothing is done for an
        empty mempool with no delta, as in Core
        (`mapTx.size() || mapNextTx.size() || mapDeltas.size()`): a block
        connected during initial block download would otherwise hash every
        transaction. btclib-org/btclib-node#1502
        """
        if not (self.size or self.deltas):
            return
        for tx in transactions:
            self.remove_tx(tx)
            self.remove_conflicts(tx)
            self.clear_prioritisation(tx.id)

    def remove_dependents(self, tx: Tx) -> None:
        """Remove what spends any of `tx`'s own outputs, with its descendants.

        `tx` itself is not a member -- a disconnected block's
        transaction this mempool chose not to re-add, still confirmed
        elsewhere, or a coinbase, never a mempool entrant on any path --
        so the walk `_descendants` otherwise begins from an already-held
        wtxid starts at `tx.id` itself instead: every wtxid `spent_by`
        names for it, and everything depending on each of those in turn.
        Core's own `removeRecursive` reached the identical way, on a
        disconnected transaction that did not make it back into the
        mempool (`MaybeUpdateMempoolForReorg`, `src/validation.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): "If the
        transaction doesn't make it in to the mempool, remove any
        transactions that depend on it (which would now be orphans)."
        """
        dependents: set[bytes] = set()
        frontier = [tx.id]
        while frontier:
            txid = frontier.pop()
            for candidate_wtxid in self.spent_by.get(txid, ()):
                if candidate_wtxid in dependents:
                    continue
                dependents.add(candidate_wtxid)
                frontier.append(self.transactions[candidate_wtxid].id)
        for wtxid in dependents:
            self._pop(wtxid)

    def remove_with_descendants(self, wtxid: bytes) -> None:
        """Remove `wtxid` and everything depending on it, a no-op if absent.

        `wtxid` is itself a member here, unlike `remove_dependents`
        above -- `main._evict_immature_or_nonfinal`'s own case, a
        transaction a reorg left immature or non-final, where
        `remove_dependents` answers `main._reconcile_mempool_for_reorg`'s
        own case, a disconnected transaction dropped rather than
        re-added. The package this removes is the same one
        `_evict_to_limit` above already computes through `_descendants`,
        generalized into its own method rather than duplicated a second
        time: Core's own `CTxMemPool::RemoveStaged`, over
        `CalculateDescendants`, is what `removeForReorg` calls for an
        entry `filter_final_and_mature` flags
        (`src/txmempool.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag).
        """
        if wtxid not in self.transactions:
            return
        for victim in self._descendants(wtxid):
            self._pop(victim)

    def meets_fee_rate(self, wtxid: bytes, min_fee_rate: int) -> bool:
        """Whether the entry's own fee clears a rate quoted in sat/kvB.

        BIP133's own comparison -- Core's `txiter->GetFee() <
        filterrate.GetFee(txiter->GetTxSize())`, net_processing.cpp --
        against this mempool's own record of what the transaction paid,
        rather than recomputing it at relay time. That is the fee without
        its delta, as in Core: a peer's filter asks what the transaction
        pays, not what this node ranks it by. `min_fee_rate` of
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
        self.txid_index.pop(self.txids.pop(wtxid), None)
        self.fees.pop(wtxid, None)
        self.modified_fees.pop(wtxid, None)
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
        self.transactions_updated += 1
        self._bound_heap()
        return tx

    def _bound_heap(self) -> None:
        """Rebuild the heap once it holds over twice the entries `size` does.

        Bounds the heap at twice the size it would be with no stale
        entries in it at all: a wtxid removed without its own heap entry
        ever being popped (every removal but the one `_pop_worst_wtxid`
        itself just consumed) or prioritised again is one more entry
        `len(self._feerate_heap)` counts and `self.size` no longer does.
        `_pop` and `prioritise` are the places that leave one.
        """
        if len(self._feerate_heap) > 2 * self.size:
            self._rebuild_feerate_heap()

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
        root_txid = self.txids[wtxid]
        descendants = {wtxid}
        frontier = [root_txid]
        while frontier:
            txid = frontier.pop()
            for candidate_wtxid in self.spent_by.get(txid, ()):
                if candidate_wtxid in descendants:
                    continue
                descendants.add(candidate_wtxid)
                frontier.append(self.txids[candidate_wtxid])
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

    def related(self, wtxid: bytes, *, ancestors: bool) -> list[bytes]:
        """Return the wtxids `wtxid` descends from, or that descend from it.

        `_ancestors` or `_descendants` less `wtxid`, which both include
        and the two RPCs omit. Ordered by internal hash, the txid's bytes
        reversed, as Core's `setEntries` is: a `std::set` under
        `CompareIteratorByHash` (`src/kernel/mempool_entry.h`), listed in
        order by `src/rpc/mempool.cpp`, at bitcoin/bitcoin@9be056a8a7,
        the v31.1 tag. btclib-org/btclib-node#1501
        """
        found = self._ancestors(wtxid) if ancestors else self._descendants(wtxid)
        found.discard(wtxid)
        return sorted(found, key=lambda w: self.transactions[w].id[::-1])

    def mining_order_keys(
        self, wtxids: Iterable[bytes]
    ) -> dict[bytes, tuple[int, int, bytes]]:
        """Return a sort key for each of `wtxids`, best-paying first.

        Core at bitcoin/bitcoin@9be056a8a7, the v31.1 tag, orders
        announcements by `CompareMiningScoreWithTopology`, the cluster
        linearization, which this mempool does not have.

        Feerates use the modified fee, as Core's graph does
        (`txmempool.cpp:641`, same tag).

        The score is the highest feerate among the ancestor packages of
        `wtxid` and of each of its descendants. A parent scores at least
        what any child paying for it does, and a tie goes to fewer
        ancestors, so parents sort first. A tie after that goes to the
        lower txid, Core's mempool fallback order (`txmempool.cpp`,
        `fallback_order`, a comparison of the internal byte order). Each
        package is computed once per call, and a transaction with no
        relatives in this mempool is scored without the walks.

        A feerate is `fee * scale // vsize`, an integer. Two distinct
        feerates `a/b` and `c/d` differ by at least `1 / (b * d)`, and
        `scale` is the square of the total vsize held, which no package's
        vsize exceeds, so the floors of the scaled feerates differ in the order
        of the feerates, and equal feerates give equal floors.
        """
        scale = self.bytesize**2
        packages: dict[bytes, tuple[int, int]] = {}

        def package(wtxid: bytes) -> tuple[int, int]:
            if wtxid not in packages:
                ancestors = self._ancestors(wtxid)
                fee = sum(self.modified_fee(w) for w in ancestors)
                vsize = sum(self.vsizes[w] for w in ancestors)
                packages[wtxid] = (fee * scale // vsize, len(ancestors))
            return packages[wtxid]

        def key(wtxid: bytes) -> tuple[int, int, bytes]:
            tx = self.transactions[wtxid]
            txid = self.txids[wtxid]
            if txid not in self.spent_by and not any(
                vin.prev_out.tx_id in self.txid_index for vin in tx.vin
            ):
                # no relatives here: its own package, with no walk
                rate = self.modified_fee(wtxid) * scale // self.vsizes[wtxid]
                return -rate, 1, txid[::-1]
            return (
                -max(package(d)[0] for d in self._descendants(wtxid)),
                package(wtxid)[1],
                txid[::-1],
            )

        return {wtxid: key(wtxid) for wtxid in wtxids}

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
        plus the transaction's fee delta, and `ancestor` and `descendant`
        sum the modified fees, as `CalculateAncestorData` and
        `CalculateDescendantData` do (`src/txmempool.cpp`, same tag).
        `depends` and `spent_by` are each this transaction's own
        direct mempool parents and children -- `tx.vin` filtered to
        `txid_index`, and `self.spent_by` itself -- not the transitive
        closure `_ancestors`/`_descendants` walk for the counts and
        sizes beside them, matching Core's own `setDepends`/`GetChildren`.
        `depends` is in the order of the txids as displayed, Core's
        `std::set<std::string>`; `spent_by` is in the order of their
        internal bytes, which `GetChildren` sorts by
        (`src/txmempool.cpp`, same tag).
        """
        tx = self.transactions[wtxid]
        ancestors = self._ancestors(wtxid)
        descendants = self._descendants(wtxid)
        depends = sorted(
            {vin.prev_out.tx_id for vin in tx.vin} & self.txid_index.keys()
        )
        spent_by = sorted(
            (self.transactions[child].id for child in self.spent_by.get(tx.id, ())),
            key=lambda txid: txid[::-1],
        )
        return MempoolEntry(
            vsize=self.vsizes[wtxid],
            weight=tx.weight,
            time=int(self.entry_times[wtxid]),
            height=self.heights[wtxid],
            wtxid=wtxid,
            fee=self.fees[wtxid],
            modified_fee=self.modified_fee(wtxid),
            ancestor_count=len(ancestors),
            ancestor_size=sum(self.vsizes[w] for w in ancestors),
            ancestor_fees=sum(self.modified_fee(w) for w in ancestors),
            descendant_count=len(descendants),
            descendant_size=sum(self.vsizes[w] for w in descendants),
            descendant_fees=sum(self.modified_fee(w) for w in descendants),
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
        held a push for, and a push happens once per `add_tx` call, once
        per `prioritise` of a held wtxid and once per
        `_rebuild_feerate_heap` sweep -- never updated in place. What can
        change is whether a given physical entry is
        still the one this wtxid is currently held under: `add_tx` on a
        wtxid that left and came back, or `prioritise` of one held, pushes
        a fresh entry with a fresh
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
        `add_tx` or `prioritise` call that last pushed it or by the most
        recent `_rebuild_feerate_heap` sweep since; that entry cannot have been
        popped already, since popping the entry matching a wtxid's
        current mapping only ever happens here, at the moment this
        method returns that wtxid to be evicted, and eviction is what
        removes the wtxid (and its mapping) from `self.transactions` --
        so the invariant is "at least one matching entry per currently
        held wtxid", not "exactly one": a re-add or a `prioritise` can leave
        a second, now-permanently-stale entry for the same wtxid behind, and
        nothing needs it gone before this can terminate correctly.
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
        handed out this way, `0` upward, are smaller than
        `self._heap_pushes` can ever be read as here: each held wtxid has
        had a push of its own, so it already exceeds `self.size` -- and
        therefore every index below it -- at any point `_bound_heap`
        calls this. A push after this rebuild
        still carries a larger `self._heap_pushes`, so it still
        breaks a tie against a rebuilt entry the same way it would have
        against the entry the rebuild replaced.
        """
        self._feerate_heap = []
        for index, wtxid in enumerate(self.fees):
            self._heap_current_seq[wtxid] = index
            self._feerate_heap.append(
                (Fraction(self.modified_fee(wtxid), self.vsizes[wtxid]), index, wtxid)
            )
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
            self._evict_chunk(self._pop_worst_wtxid())

    def _unpop_worst_wtxid(self, wtxid: bytes) -> None:
        """Put back the heap entry `_pop_worst_wtxid` just took for `wtxid`."""
        heapq.heappush(
            self._feerate_heap,
            (
                Fraction(self.modified_fee(wtxid), self.vsizes[wtxid]),
                self._heap_current_seq[wtxid],
                wtxid,
            ),
        )

    def _evict_chunk(self, worst: bytes) -> None:
        """Evict `worst`, just popped from the heap, with what depends on it."""
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
        # this mempool's own selection (`_evict_to_limit`) does not chase
        # CPFP the way `m_txgraph`'s package score does.
        #
        # sat/kvB, exact until the float `_track_package_removed`
        # stores it as -- Core's own `CFeeRate` arithmetic in
        # `TrimToSize` is int64 rather than float, a difference this
        # module's own advisory, non-consensus use of the number
        # does not need to close.
        package_fee = sum(self.modified_fee(w) for w in package)
        package_vsize = sum(self.vsizes[w] for w in package)
        removed_rate = Fraction(package_fee, package_vsize) * 1000
        removed_rate += self.incremental_relay_feerate.sats_per_kvbyte
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

    def add_transactions_updated(self, count: int) -> None:
        """Count `count` tip changes in `transactions_updated`.

        Core's `AddTransactionsUpdated`, which `UpdateTip` calls once per
        block connected or disconnected (`src/validation.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
        """
        self.transactions_updated += count

    def note_block_connected(self) -> None:
        """Restart the rolling minimum's decay clock for one connected block.

        Core's own `removeForBlock` (`src/txmempool.cpp:405-427`, same
        commit) sets `lastRollingFeeUpdate`/`blockSinceLastRollingFeeBump`
        this way for every block, whether or not that block held any
        transaction this mempool was also holding -- called once per
        block from `main.update_chain`'s own connect loop, and not folded
        into `remove_tx`, which already runs once per transaction inside
        that same loop rather than once per block.

        Also clears `mark_rejected`'s own cache and
        `mark_rejected_reconsiderable`'s, whichever peer's
        transaction the connected block held or did not: a reorg's own
        multi-block connect loop calls this once per block added, so the
        caches are emptied at least once for any active tip change, the
        same event Core's `ActiveTipChange` resets `m_recent_rejects` and
        `m_lazy_recent_rejects_reconsiderable` on.
        """
        self._last_rolling_fee_update = time.time()
        self._block_since_last_rolling_fee_bump = True
        self._recent_rejects.clear()
        self._recent_rejects_order.clear()
        self._recent_rejects_reconsiderable.clear()
        self._recent_rejects_reconsiderable_order.clear()

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
        floored at `incremental_relay_feerate` once it decays, or
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
                < self.incremental_relay_feerate.sats_per_kvbyte / 2
            ):
                self._rolling_min_fee_rate = 0.0
                return FeeRate(sats_per_kvbyte=0)

        return FeeRate(
            sats_per_kvbyte=max(
                round(self._rolling_min_fee_rate),
                self.incremental_relay_feerate.sats_per_kvbyte,
            )
        )
