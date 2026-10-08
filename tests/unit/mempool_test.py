# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Mempool`'s bookkeeping, eviction and its rolling minimum feerate."""

import hashlib
import secrets
import time
from fractions import Fraction
from typing import Any, override

import pytest
from btclib.fee import FeeRate, fee_from_vsize
from btclib.script import script
from btclib.script.witness import Witness
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import mempool as mempool_module
from btclib_node.exceptions import TxRejectedError
from btclib_node.fee_estimator import RemovedTx
from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from tests import generate_random_transaction


def a_witness_transaction() -> Tx:
    """Return a random transaction whose txid and wtxid actually differ."""
    # a txid and a wtxid are the same bytes until there is a witness, and
    # an assertion about one would then pass by naming the other
    tx = generate_random_transaction()
    tx.vin[0].script_witness = Witness([secrets.token_bytes(32)])
    return tx


def a_transaction_spending(*prevout_txids: bytes) -> Tx:
    """Return a transaction with one input per txid in `prevout_txids`."""
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(txid, 0),
                script_sig=script.serialize([secrets.token_bytes(32)]),
                sequence=0xFFFFFFFF,
            )
            for txid in prevout_txids
        ],
        vout=[
            TxOut(
                value=50 * 10**8,
                script_pub_key=script.serialize([secrets.token_bytes(32)]),
            )
        ],
    )


def test_init() -> None:
    """`Mempool()` constructs without raising."""
    Mempool(Logger(debug=True))


def test_workflow() -> None:
    """Add, remove, size/bytesize accounting and eviction together."""
    mempool = Mempool(Logger(debug=True))

    tx = generate_random_transaction()
    mempool.add_tx(tx)

    assert mempool.size == 1
    assert mempool.bytesize == tx.vsize
    assert mempool.get_tx(tx.id) == tx
    assert mempool.get_tx(tx.hash, wtxid=True) == tx

    mempool.remove_tx(tx)
    assert mempool.size == 0
    assert mempool.bytesize == 0

    txs = []
    for _ in range(100):
        tx = generate_random_transaction()
        mempool.add_tx(tx)
        txs.append(tx)

    prev_size = mempool.size
    prev_bytesize = mempool.bytesize
    # Every entry so far pays no fee, so eviction (`Mempool._evict_to_limit`)
    # breaks the tie toward insertion order -- `dict.items()`'s own order and
    # `min`'s own stability -- and takes out the oldest of the 100, `txs[0]`,
    # to make room for the one just added: size and bytesize both come back
    # to what they were, not because the add refused (the old `is_full()`
    # wall this replaces) but because eviction undid exactly what the add
    # did. btclib-org/btclib-node#294
    mempool.bytesize_limit = mempool.bytesize
    new_tx = generate_random_transaction()
    mempool.add_tx(new_tx)
    assert prev_size == mempool.size
    assert prev_bytesize == mempool.bytesize
    assert not mempool.contains_tx(txs[0])
    assert mempool.contains_tx(new_tx)

    missing_tx = generate_random_transaction()
    mempool.bytesize_limit = 1000**2
    held = [t.id for t in txs[1:]] + [new_tx.id]
    assert mempool.get_missing([*held, missing_tx.id]) == [missing_tx.id]

    assert mempool.get_tx(b"\x00" * 32) is None


def test_a_bytesize_limit_of_zero_evicts_every_add_right_back_out() -> None:
    """At `bytesize_limit=0`, `add_tx` evicts its own add; returns `False`."""
    # bytesize_limit at zero means every add is immediately the only, and so
    # the worst, entry held: `_evict_to_limit` takes it right back out,
    # giving the same outcome the old `is_full()` outright refusal gave,
    # reached now through eviction rather than a pre-check. get_missing no
    # longer short-circuits on `is_full()` either -- download.tx_download's
    # own eviction-aware membership check (`Mempool.transactions`) is what
    # keeps a request for something this mempool cannot hold from being
    # announced now, not a blanket "nothing is missing" answer that used to
    # stop every request even for something worth holding.
    # btclib-org/btclib-node#294
    mempool = Mempool(Logger(debug=True))
    mempool.bytesize_limit = 0
    assert mempool.is_full()

    tx = generate_random_transaction()
    assert mempool.get_missing([tx.id]) == [tx.id]
    assert mempool.add_tx(tx) is False
    assert mempool.size == 0
    assert not mempool.contains_tx(tx)


def test_add_tx_reports_a_full_mempool_added_nothing() -> None:
    """`add_tx` on a mempool with no room left returns `False`."""
    # what p2p/callbacks.py's `tx` handler gates queuing an announcement
    # on: a full mempool's silent no-op has to be visible to the caller,
    # or a transaction this node declined to keep is still announced to
    # every other peer. btclib-org/btclib-node#277
    mempool = Mempool(Logger(debug=True))
    mempool.bytesize_limit = 0
    assert mempool.add_tx(generate_random_transaction()) is False


def test_add_tx_reports_what_it_added_and_declined() -> None:
    """`add_tx` returns `True` for a new tx, `False` for the same twice."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    assert mempool.add_tx(tx) is True
    # the same transaction a second time is the other no-op add_tx makes
    assert mempool.add_tx(tx) is False


def test_the_same_transaction_twice_is_counted_once() -> None:
    """Adding the same transaction twice leaves size/bytesize unchanged."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx)
    mempool.add_tx(tx)
    assert mempool.size == 1
    assert mempool.bytesize == tx.vsize


def test_removing_what_was_never_there_changes_nothing() -> None:
    """`remove_tx` on a txid this mempool never held is a no-op."""
    mempool = Mempool(Logger(debug=True))
    mempool.remove_tx(generate_random_transaction())
    assert mempool.size == 0
    assert mempool.bytesize == 0


def test_the_sequence_starts_at_one_and_counts_every_real_add_and_remove_once() -> None:
    """`sequence` starts at 1, bumping once per real add/remove, not a no-op."""
    # Core's own semantics, `CTxMemPool::m_sequence_number`
    # (src/txmempool.h:200-202): "incremented once every time a
    # transaction is added or removed from the mempool for any
    # reason" -- but a no-op is neither an addition nor a removal, the
    # same guard size and bytesize already have above. The field starts
    # at 1, not 0 (`:202`), and `GetSequence` (`:598-600`) -- what
    # `getrawmempool`'s `mempool_sequence` reads -- answers the current,
    # already-bumped value, so a fresh mempool with zero events answers
    # 1 and every later answer is Core's own N+1 after N events, not N
    mempool = Mempool(Logger(debug=True))
    assert mempool.sequence == 1
    tx = generate_random_transaction()

    mempool.add_tx(tx)
    assert mempool.sequence == 2
    mempool.add_tx(tx)
    assert mempool.sequence == 2

    mempool.remove_tx(tx)
    assert mempool.sequence == 3
    mempool.remove_tx(tx)
    assert mempool.sequence == 3

    mempool.remove_tx(generate_random_transaction())
    assert mempool.sequence == 3


def test_nothing_is_missing_when_everything_is_held() -> None:
    """`get_missing` answers empty by txid and wtxid, not by the other's id."""
    mempool = Mempool(Logger(debug=True))
    txs = [a_witness_transaction() for _ in range(3)]
    for tx in txs:
        mempool.add_tx(tx)
    assert mempool.get_missing([tx.id for tx in txs]) == []
    assert mempool.get_missing([tx.hash for tx in txs], wtxid=True) == []
    # the other identifier of the same transaction is not held under this
    # one, so asking by txid for a wtxid reports every one of them missing
    assert mempool.get_missing([tx.hash for tx in txs]) == [tx.hash for tx in txs]


def test_a_transaction_is_found_by_either_of_its_identifiers() -> None:
    """`get_tx` finds a stored tx by txid or wtxid, never by the other index."""
    mempool = Mempool(Logger(debug=True))
    tx = a_witness_transaction()
    assert tx.id != tx.hash  # what the two lookups below are about
    mempool.add_tx(tx)
    assert mempool.get_tx(tx.id) == tx
    assert mempool.get_tx(tx.hash, wtxid=True) == tx
    # and neither identifier answers under the other's index
    assert mempool.get_tx(tx.hash) is None
    assert mempool.get_tx(tx.id, wtxid=True) is None
    assert mempool.get_tx(b"\x11" * 32) is None


def test_a_fee_is_kept_and_dropped_with_its_transaction() -> None:
    """`fees` records what `add_tx` was told a tx paid, dropped on removal."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, 1000)
    assert mempool.fees[tx.hash] == 1000
    mempool.remove_tx(tx)
    assert tx.hash not in mempool.fees


def test_a_transaction_added_without_a_fee_is_recorded_at_zero() -> None:
    """`add_tx`'s `fee` argument defaults to 0, not to a missing entry."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx)
    assert mempool.fees[tx.hash] == 0


def test_a_zero_min_fee_rate_is_no_filter_and_clears_everything() -> None:
    """`meets_fee_rate` with `min_fee_rate=0` always answers `True`."""
    # BIP133's and Connection.feefilter's own "no filter" value
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, 0)
    assert mempool.meets_fee_rate(tx.hash, 0)


def test_a_wtxid_the_mempool_holds_no_fee_for_clears_every_rate() -> None:
    """`meets_fee_rate` on a wtxid this mempool does not hold answers `True`."""
    # gone already, or never held: there is nothing here to withhold it
    # for, so the filter does not withhold it
    mempool = Mempool(Logger(debug=True))
    assert mempool.meets_fee_rate(b"\x00" * 32, 1000)


def test_a_fee_below_the_rate_is_withheld_and_at_or_above_it_clears() -> None:
    """`meets_fee_rate` compares the stored fee against BIP133's boundary."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    required = fee_from_vsize(tx.vsize, FeeRate(sats_per_kvbyte=1000))
    mempool.add_tx(tx, required - 1)
    assert not mempool.meets_fee_rate(tx.hash, 1000)

    mempool.remove_tx(tx)
    mempool.add_tx(tx, required)
    assert mempool.meets_fee_rate(tx.hash, 1000)


def test_eviction_takes_the_worst_feerate_and_keeps_the_rest() -> None:
    """`_evict_to_limit` removes the lowest-feerate entry, not just any."""
    mempool = Mempool(Logger(debug=True))
    cheap = generate_random_transaction()
    rich = generate_random_transaction()
    mempool.add_tx(cheap, 0)
    # room for exactly one more: adding rich puts this one vsize over
    mempool.bytesize_limit = mempool.bytesize + rich.vsize - 1
    assert mempool.add_tx(rich, 10_000) is True
    assert not mempool.contains_tx(cheap)
    assert mempool.contains_tx(rich)


def test_eviction_of_the_worst_parent_takes_its_descendant_with_it() -> None:
    """Evicting the worst-feerate parent evicts the child that spends it too."""
    # verify_mempool_acceptance (main.py) admits a child whose parent is
    # only in the mempool, so evicting the parent alone would leave the
    # child's own prevout resolving nowhere -- _descendants is what keeps
    # this from happening. btclib-org/btclib-node#294
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    other = generate_random_transaction()
    mempool.add_tx(parent, 0)
    mempool.add_tx(child, 0)
    mempool.bytesize_limit = mempool.bytesize + other.vsize - 1
    assert mempool.add_tx(other, 10_000) is True
    assert not mempool.contains_tx(parent)
    assert not mempool.contains_tx(child)
    assert mempool.contains_tx(other)


def test_eviction_of_a_diamond_shaped_package_removes_every_descendant_once() -> None:
    """Evicting a parent takes a grandchild reachable through two children too.

    A parent with two children and a grandchild spending both is one
    package, and eviction of the parent takes all four out, `grandchild`
    included -- reached from `parent` through either child, never twice.
    """
    # btclib-org/btclib-node#441: the spend index `_descendants` now
    # walks, `spent_by`, has one entry per (parent txid, spending wtxid)
    # pair, so a transaction reachable through two parents at once --
    # this is what a diamond exercises -- has to land in the walk's own
    # `descendants` set once, not be visited or queued twice.
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child_a = generate_random_transaction(parent.id)
    # parent:1, not parent:0 again: a second spend of one outpoint is a
    # conflict `add_tx` refuses (btclib-org/btclib-node#1244)
    child_b = a_spend_of([(parent.id, 1)])
    grandchild = a_transaction_spending(child_a.id, child_b.id)
    keeper = generate_random_transaction()
    for tx in (parent, child_a, child_b, grandchild):
        assert mempool.add_tx(tx, 0)
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    assert mempool.add_tx(keeper, 10_000) is True
    assert not mempool.contains_tx(parent)
    assert not mempool.contains_tx(child_a)
    assert not mempool.contains_tx(child_b)
    assert not mempool.contains_tx(grandchild)
    assert mempool.contains_tx(keeper)
    assert mempool.size == 1


def test_two_inputs_into_one_parent_do_not_crash_removal() -> None:
    """A child spending two outputs of one parent is removed without error.

    A child with two inputs into one parent is removed without a
    `KeyError`, and its parent still evicts cleanly afterwards.
    """
    # `spent_by[parent.id]` gets `child`'s own wtxid once (`add_tx`'s own
    # `set.add` is idempotent over the child's two vins), but a `_pop`
    # that walked `tx.vin` itself rather than the set of distinct spent
    # txids would `discard` it, delete the now-empty entry on the first
    # vin, and then `KeyError` on `spent_by[parent.id]` for the second.
    # btclib-org/btclib-node#441
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(parent.id, 0),
                script_sig=script.serialize([secrets.token_bytes(32)]),
                sequence=0xFFFFFFFF,
            ),
            TxIn(
                prev_out=OutPoint(parent.id, 1),
                script_sig=script.serialize([secrets.token_bytes(32)]),
                sequence=0xFFFFFFFF,
            ),
        ],
        vout=[
            TxOut(
                value=50 * 10**8,
                script_pub_key=script.serialize([secrets.token_bytes(32)]),
            )
        ],
    )
    mempool.add_tx(parent, 0)
    mempool.add_tx(child, 0)
    mempool.remove_tx(child)
    assert mempool.size == 1
    mempool.remove_tx(parent)  # would KeyError on a stale spent_by entry
    assert mempool.size == 0


def test_a_removed_child_does_not_reappear_in_a_later_eviction_of_its_parent() -> None:
    """A child removed by `remove_tx` is not evicted again with its parent.

    `remove_tx` drops a transaction out of the descendant walk too, not
    only out of `transactions` -- a later eviction of the same parent
    must not try to evict it a second time.
    """
    # A `spent_by` entry `_pop` failed to clear behind `remove_tx` would
    # leave `stale_child`'s own wtxid in `spent_by[parent.id]` after it
    # left `transactions`; `_descendants` would then index `transactions`
    # by that wtxid and raise `KeyError` the next time `parent` is
    # evicted, rather than silently evicting the wrong set.
    # btclib-org/btclib-node#441
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    stale_child = generate_random_transaction(parent.id)
    mempool.add_tx(parent, 0)
    mempool.add_tx(stale_child, 0)
    mempool.remove_tx(stale_child)  # e.g. already mined, unrelated to eviction

    fresh_child = generate_random_transaction(parent.id)
    mempool.add_tx(fresh_child, 0)
    keeper = generate_random_transaction()
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    assert mempool.add_tx(keeper, 10_000) is True
    assert not mempool.contains_tx(parent)
    assert not mempool.contains_tx(fresh_child)
    assert mempool.contains_tx(keeper)
    assert mempool.size == 1


def test_a_stale_heap_entry_left_by_an_evicted_descendant_is_skipped() -> None:
    """`_pop_worst_wtxid` discards a descendant's own leftover heap entry.

    Evicting a parent's package leaves the descendant's own
    `_feerate_heap` entry unconsumed -- `_pop_worst_wtxid` only pops the
    package root off the heap itself, `_evict_to_limit`'s own loop
    removing every other package member through `_pop` alone. A later
    eviction round has to reach past that stale entry, not raise on it
    or evict the same wtxid a second time: without the current-entry
    check this test guards, `_descendants` would be asked for the
    descendants of a wtxid `self.transactions` no longer holds and raise
    `KeyError`.
    btclib-org/btclib-node#457
    """
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    mempool.add_tx(parent, 0)
    mempool.add_tx(child, 0)
    other = generate_random_transaction()
    mempool.bytesize_limit = mempool.bytesize + other.vsize - 1
    assert mempool.add_tx(other, 10_000) is True
    assert not mempool.contains_tx(parent)
    assert not mempool.contains_tx(child)
    # `child`'s own heap entry is still in `_feerate_heap`, unconsumed and
    # now stale -- feerate 0, the same as `cheap` below, but pushed
    # earlier and so ordered first by the heap's own insertion-order
    # tiebreak, which is exactly what makes the next eviction round
    # discard it before finding `cheap` as the genuine worst entry.
    cheap = generate_random_transaction()
    mempool.add_tx(cheap, 0)
    rich = generate_random_transaction()
    mempool.bytesize_limit = mempool.bytesize + rich.vsize - 1  # room for one more
    assert mempool.add_tx(rich, 10_000) is True
    assert not mempool.contains_tx(cheap)
    assert mempool.contains_tx(other)
    assert mempool.contains_tx(rich)


def test_a_wtxid_that_left_and_came_back_ties_as_the_newest_entry() -> None:
    """A re-added wtxid's leftover heap entry does not sort as its old self.

    `b` (fee 50), `a` (fee 100), remove `a`, `c` (fee 100, tying `a`'s
    own feerate), re-add `a` (fee 100): `a`'s first-spell heap entry is
    still physically in `_feerate_heap`, unconsumed by the `remove_tx`
    that dropped it, and carries `a`'s *original* insertion-order
    tiebreak -- lower than `c`'s, since `a` was first added before `c`
    ever was. Evicting worst-first twice has to remove `b`, then `c`,
    the same as a plain dict tied on `min`'s own stability would (a
    delete followed by a fresh insert moves a key to the end, past
    every key already there when it was reinserted) -- not `b` then
    `a`, which is what accepting that first-spell entry on membership in
    `transactions` alone gives, `a` still being held under its second
    spell. This is what a review of the first round of #457 caught by
    running this exact sequence against the pre-heap `Mempool`.
    """
    mempool = Mempool(Logger(debug=True))
    b = generate_random_transaction()
    a = generate_random_transaction()
    c = generate_random_transaction()
    mempool.add_tx(b, 50)
    mempool.add_tx(a, 100)
    mempool.remove_tx(a)
    mempool.add_tx(c, 100)
    mempool.add_tx(a, 100)  # a's second spell

    mempool.bytesize_limit = mempool.bytesize - 1
    mempool._evict_to_limit()
    assert not mempool.contains_tx(b)
    assert mempool.contains_tx(a)
    assert mempool.contains_tx(c)

    mempool.bytesize_limit = mempool.bytesize - 1
    mempool._evict_to_limit()
    assert not mempool.contains_tx(c)
    assert mempool.contains_tx(a)


def test_the_feerate_heap_is_rebuilt_once_its_garbage_outgrows_its_entries() -> None:
    """`_rebuild_feerate_heap` fires once stale entries exceed live ones.

    Three transactions, none ever evicted: two plain `remove_tx` calls
    each leave that wtxid's own heap entry behind, stale, since neither
    goes through `_pop_worst_wtxid`. `_pop`'s own check
    (`len(self._feerate_heap) > 2 * self.size`) fires on the second
    removal, once garbage outnumbers what is still held two to one, and
    `_feerate_heap` comes back holding exactly one entry per surviving
    transaction rather than the three pushed since the mempool started.
    btclib-org/btclib-node#457
    """
    mempool = Mempool(Logger(debug=True))
    first = generate_random_transaction()
    second = generate_random_transaction()
    third = generate_random_transaction()
    mempool.add_tx(first, 0)
    mempool.add_tx(second, 0)
    mempool.add_tx(third, 0)
    assert len(mempool._feerate_heap) == 3

    mempool.remove_tx(first)
    assert len(mempool._feerate_heap) == 3  # 3 > 2*2 is false: no rebuild yet

    mempool.remove_tx(second)
    assert len(mempool._feerate_heap) == 1  # 3 > 2*1 was true: rebuilt
    assert mempool.size == 1
    assert mempool.contains_tx(third)


def test_eviction_runs_multiple_rounds_when_one_is_not_enough() -> None:
    """`_evict_to_limit` loops, evicting more than one entry to reach limit."""
    mempool = Mempool(Logger(debug=True))
    worst = generate_random_transaction()
    middle = generate_random_transaction()
    best = generate_random_transaction()
    mempool.add_tx(worst, 0)
    mempool.add_tx(middle, 100)
    mempool.bytesize_limit = worst.vsize  # room for only one of the three
    assert mempool.add_tx(best, 10_000) is True
    assert not mempool.contains_tx(worst)
    assert not mempool.contains_tx(middle)
    assert mempool.contains_tx(best)
    assert mempool.size == 1


def test_eviction_raises_the_rolling_minimum_above_what_it_evicted() -> None:
    """Evicting a free tx sets the rolling minimum to the incremental fee."""
    mempool = Mempool(Logger(debug=True))
    victim = generate_random_transaction()
    keeper = generate_random_transaction()
    mempool.add_tx(victim, 0)
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    mempool.add_tx(keeper, 10_000)
    # victim paid nothing, so the rolling minimum lands on the incremental
    # relay fee rate itself -- Core's own DEFAULT_INCREMENTAL_RELAY_FEE
    assert mempool._rolling_min_fee_rate == 100
    assert mempool._block_since_last_rolling_fee_bump is False


def test_eviction_bumps_the_rolling_minimum_by_the_whole_package_it_evicts() -> None:
    """A CPFP-evicted package bumps the rolling minimum by its combined rate."""
    # Core's own TrimToSize (src/txmempool.cpp:917-925,
    # at bitcoin/bitcoin@58a7869f86) bumps the rolling minimum from the
    # removed chunk's own aggregate feerate, not from the worst entry's
    # own rate alone: a low-fee parent evicted together with a child
    # overpaying for it (CPFP) bumps the rolling minimum by their
    # combined rate, higher than the parent's own individual rate --
    # which is what the parent alone paid nothing would otherwise give,
    # `test_eviction_raises_the_rolling_minimum_above_what_it_evicted`'s
    # own 100.
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    keeper = generate_random_transaction()
    mempool.add_tx(parent, 0)
    mempool.add_tx(child, 100_000)
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    mempool.add_tx(keeper, 1)
    assert not mempool.contains_tx(parent)
    assert not mempool.contains_tx(child)

    package_rate = Fraction(100_000, parent.vsize + child.vsize) * 1000
    expected = float(package_rate + 100)
    assert mempool._rolling_min_fee_rate == expected
    assert mempool._rolling_min_fee_rate != 100  # the parent's own rate alone


def test_a_lower_rate_eviction_does_not_lower_the_rolling_minimum() -> None:
    """`_track_package_removed` only ever raises the rolling minimum."""
    mempool = Mempool(Logger(debug=True))
    mempool._rolling_min_fee_rate = 5000.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._track_package_removed(1000.0)
    assert mempool._rolling_min_fee_rate == 5000.0
    assert mempool._block_since_last_rolling_fee_bump is True


def test_a_higher_rate_eviction_raises_the_rolling_minimum_and_restarts_decay() -> None:
    """A higher eviction rate raises the minimum and clears the decay flag."""
    mempool = Mempool(Logger(debug=True))
    mempool._rolling_min_fee_rate = 1000.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._track_package_removed(5000.0)
    assert mempool._rolling_min_fee_rate == 5000.0
    assert mempool._block_since_last_rolling_fee_bump is False


def test_note_block_connected_restarts_the_decay_clock() -> None:
    """`note_block_connected` sets the decay flag and the last-update time."""
    mempool = Mempool(Logger(debug=True))
    mempool._block_since_last_rolling_fee_bump = False
    before = time.time()
    mempool.note_block_connected()
    assert mempool._block_since_last_rolling_fee_bump is True
    assert mempool._last_rolling_fee_update >= before


def test_a_fresh_mempool_has_rejected_nothing() -> None:
    """`was_recently_rejected` answers False for a wtxid never marked."""
    mempool = Mempool(Logger(debug=True))
    assert not mempool.was_recently_rejected(secrets.token_bytes(32))


def test_mark_rejected_is_read_back_by_was_recently_rejected() -> None:
    """A marked wtxid reads back as recently rejected."""
    mempool = Mempool(Logger(debug=True))
    wtxid = secrets.token_bytes(32)
    mempool.mark_rejected(wtxid)
    assert mempool.was_recently_rejected(wtxid)


def test_note_block_connected_clears_the_reject_cache() -> None:
    """A connected block forgets every refusal recorded before it.

    Mirrors Core's own `ActiveTipChange`, which resets `m_recent_rejects`
    for the same reason: a refusal that turned on the chain tip --
    finality, a sequence lock, coinbase maturity -- can stop holding
    once the tip moves.
    """
    mempool = Mempool(Logger(debug=True))
    wtxid = secrets.token_bytes(32)
    mempool.mark_rejected(wtxid)
    mempool.note_block_connected()
    assert not mempool.was_recently_rejected(wtxid)


def test_marking_an_already_rejected_wtxid_is_a_no_op() -> None:
    """Re-marking a wtxid already held does not double its own eviction entry.

    Otherwise a peer resubmitting the identical refused candidate over
    and over would push a fresh entry onto `_recent_rejects_order` per
    resubmission while `_recent_rejects` itself still counts the wtxid
    once, and the capacity bound below would evict long before its own
    count says it should.
    """
    mempool = Mempool(Logger(debug=True))
    wtxid = secrets.token_bytes(32)
    mempool.mark_rejected(wtxid)
    mempool.mark_rejected(wtxid)
    assert len(mempool._recent_rejects_order) == 1


def test_mark_rejected_evicts_the_oldest_past_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past capacity, the earliest-marked wtxid is forgotten first."""
    monkeypatch.setattr(mempool_module, "_RECENT_REJECTS_CAPACITY", 2)
    mempool = Mempool(Logger(debug=True))
    first, second, third = (secrets.token_bytes(32) for _ in range(3))
    mempool.mark_rejected(first)
    mempool.mark_rejected(second)
    assert mempool.was_recently_rejected(first)
    mempool.mark_rejected(third)
    assert not mempool.was_recently_rejected(first)
    assert mempool.was_recently_rejected(second)
    assert mempool.was_recently_rejected(third)


def test_get_min_fee_rate_is_zero_before_anything_is_ever_evicted() -> None:
    """A fresh mempool's rolling minimum feerate is zero."""
    mempool = Mempool(Logger(debug=True))
    assert mempool.get_min_fee_rate() == FeeRate(sats_per_kvbyte=0)


def test_get_min_fee_rate_is_zero_after_a_block_but_no_bump_yet() -> None:
    """A connected block alone, with no eviction ever, still answers zero."""
    mempool = Mempool(Logger(debug=True))
    mempool.note_block_connected()
    assert mempool.get_min_fee_rate() == FeeRate(sats_per_kvbyte=0)


def test_get_min_fee_rate_does_not_decay_before_a_block_has_connected() -> None:
    """With no block connected since the last rise, the minimum stays put."""
    # _track_package_removed's own guard: a run of evictions with no block
    # in between only ever raises the rolling minimum, never decays it
    mempool = Mempool(Logger(debug=True))
    mempool._rolling_min_fee_rate = 5000.0
    mempool._block_since_last_rolling_fee_bump = False
    mempool._last_rolling_fee_update = time.time() - 60 * 60 * 24
    assert mempool.get_min_fee_rate() == FeeRate(sats_per_kvbyte=5000)


def test_get_min_fee_rate_does_not_decay_within_ten_seconds_of_its_last_move() -> None:
    """Inside the ten-second guard, the rolling minimum reads back unchanged."""
    mempool = Mempool(Logger(debug=True))
    mempool._rolling_min_fee_rate = 5000.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._last_rolling_fee_update = time.time()
    assert mempool.get_min_fee_rate() == FeeRate(sats_per_kvbyte=5000)


def test_get_min_fee_rate_decays_by_half_after_one_full_halflife() -> None:
    """A full 12-hour halflife, over half full, halves the rolling minimum."""
    # bytesize at least half of bytesize_limit: no halflife shortening
    mempool = Mempool(Logger(debug=True))
    mempool.bytesize_limit = 1000
    mempool.bytesize = 999
    mempool._rolling_min_fee_rate = 4000.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._last_rolling_fee_update = time.time() - 60 * 60 * 12
    assert mempool.get_min_fee_rate() == FeeRate(sats_per_kvbyte=2000)


def test_get_min_fee_rate_decays_twice_as_fast_under_half_full() -> None:
    """Under half full, the halflife is shortened to six hours."""
    mempool = Mempool(Logger(debug=True))
    mempool.bytesize_limit = 1000
    mempool.bytesize = 400  # limit/4 <= bytesize < limit/2
    mempool._rolling_min_fee_rate = 4000.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._last_rolling_fee_update = time.time() - 60 * 60 * 6  # halflife/2
    assert mempool.get_min_fee_rate() == FeeRate(sats_per_kvbyte=2000)


def test_get_min_fee_rate_decays_four_times_as_fast_near_empty() -> None:
    """Under a quarter full, the halflife is shortened to three hours."""
    mempool = Mempool(Logger(debug=True))
    mempool.bytesize_limit = 1000
    mempool.bytesize = 100  # < limit/4
    mempool._rolling_min_fee_rate = 4000.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._last_rolling_fee_update = time.time() - 60 * 60 * 3  # halflife/4
    assert mempool.get_min_fee_rate() == FeeRate(sats_per_kvbyte=2000)


def test_get_min_fee_rate_floors_at_the_incremental_fee_once_decayed() -> None:
    """A decay under the incremental relay fee floors there instead."""
    mempool = Mempool(Logger(debug=True))
    mempool.bytesize_limit = 1000
    mempool.bytesize = 999
    mempool._rolling_min_fee_rate = 150.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._last_rolling_fee_update = time.time() - 60 * 60 * 12  # 150 -> 75
    assert mempool.get_min_fee_rate() == FeeRate(sats_per_kvbyte=100)


def test_get_min_fee_rate_zeroes_out_below_half_the_incremental_fee() -> None:
    """Enough halvings under half the incremental fee zero it out instead."""
    mempool = Mempool(Logger(debug=True))
    mempool.bytesize_limit = 1000
    mempool.bytesize = 999
    mempool._rolling_min_fee_rate = 100.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._last_rolling_fee_update = time.time() - 60 * 60 * 12 * 20  # 20 halvings
    assert mempool.get_min_fee_rate() == FeeRate(sats_per_kvbyte=0)
    assert mempool._rolling_min_fee_rate == 0.0


def test_the_incremental_relay_fee_is_the_one_given_at_construction() -> None:
    """`-incrementalrelayfee` is what the eviction bump and the decay read.

    Neither is a fixed 100 sat/kvB (btclib-org/btclib-node#1596).
    """
    incremental = FeeRate(sats_per_kvbyte=1000)
    mempool = Mempool(Logger(debug=True), incremental)
    assert mempool.incremental_relay_feerate == incremental

    victim = generate_random_transaction()
    keeper = generate_random_transaction()
    mempool.add_tx(victim, 0)
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    mempool.add_tx(keeper, 10_000)
    assert mempool._rolling_min_fee_rate == 1000

    mempool.bytesize_limit = 1000
    mempool.bytesize = 999
    mempool._rolling_min_fee_rate = 1500.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._last_rolling_fee_update = time.time() - 60 * 60 * 12  # 1500 -> 750
    assert mempool.get_min_fee_rate() == incremental
    mempool._rolling_min_fee_rate = 1000.0
    mempool._last_rolling_fee_update = time.time() - 60 * 60 * 12 * 2  # 1000 -> 250
    assert mempool.get_min_fee_rate() == FeeRate(sats_per_kvbyte=0)


def a_spend_of(outpoints: list[tuple[bytes, int]], value: int = 1) -> Tx:
    """Return a transaction spending exactly `outpoints`."""
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(txid, vout),
                script_sig=script.serialize([secrets.token_bytes(32)]),
                sequence=0xFFFFFFFF,
            )
            for txid, vout in outpoints
        ],
        vout=[
            TxOut(
                value=value, script_pub_key=script.serialize([secrets.token_bytes(32)])
            )
        ],
    )


def test_a_second_spend_of_one_outpoint_is_not_added() -> None:
    """`add_tx` keeps one spender per outpoint, and forgets it once gone.

    btclib-org/btclib-node#1244: two spends of one outpoint were both kept.
    """
    mempool = Mempool(Logger(debug=True))
    coin = (secrets.token_bytes(32), 3)
    first = a_spend_of([coin, (secrets.token_bytes(32), 0)])
    second = a_spend_of([coin])
    assert mempool.add_tx(first, 1000)
    assert not mempool.add_tx(second, 5000)
    assert mempool.outpoint_spender[coin] == first.hash
    assert not mempool.contains_tx(second)
    assert mempool.size == 1

    mempool.remove_tx(first)
    assert mempool.outpoint_spender == {}
    assert mempool.add_tx(second, 5000)


def test_format_money_is_core_s_own() -> None:
    """Eight decimals, right-trimmed to no fewer than two."""
    fmt = mempool_module.format_money
    assert fmt(0) == "0.00"
    assert fmt(5000) == "0.00005"
    assert fmt(10000) == "0.0001"
    assert fmt(10**8) == "1.00"
    assert fmt(123_456_789) == "1.23456789"
    assert fmt(2_150_000_000) == "21.50"


def a_mempool_with_a_conflict() -> tuple[Mempool, tuple[bytes, int], Tx, Tx]:
    """Hold a spend of one coin paying 10000, and its child paying 2000."""
    mempool = Mempool(Logger(debug=True))
    coin = (secrets.token_bytes(32), 0)
    held = a_spend_of([coin])
    child = a_spend_of([(held.id, 0)])
    assert mempool.add_tx(held, 10_000)
    assert mempool.add_tx(child, 2_000)
    return mempool, coin, held, child


def test_a_conflict_paying_less_than_what_it_replaces_is_insufficient() -> None:
    """Core's rule 3, over the conflict and its descendants, in its words.

    `bitcoind` v31.1 on regtest answers a 5000-sat conflict with a
    10000-sat spend "insufficient fee, rejecting replacement <txid>, less
    fees than conflicting txs; 0.00005 < 0.0001". Here the held spend's
    child counts too, so 11999 is still short of 12000.
    """
    mempool, coin, _, _ = a_mempool_with_a_conflict()
    candidate = a_spend_of([coin])
    with pytest.raises(TxRejectedError) as refused:
        mempool.check_replacement(candidate, 11_999, candidate.vsize)
    assert refused.value.reason == "insufficient fee"
    assert str(refused.value) == (
        f"insufficient fee, rejecting replacement {candidate.id.hex()}, less fees "
        "than conflicting txs; 0.00011999 < 0.00012"
    )


def test_a_conflict_not_paying_its_own_relay_is_insufficient() -> None:
    """Core's rule 4: the increase has to cover the incremental relay fee."""
    mempool, coin, _, _ = a_mempool_with_a_conflict()
    candidate = a_spend_of([coin])
    relay = fee_from_vsize(candidate.vsize, mempool.incremental_relay_feerate)
    assert relay > 0
    for fee in (12_000, 12_000 + relay - 1):
        with pytest.raises(TxRejectedError) as refused:
            mempool.check_replacement(candidate, fee, candidate.vsize)
        increase = mempool_module.format_money(fee - 12_000)
        assert str(refused.value) == (
            f"insufficient fee, rejecting replacement {candidate.id.hex()}, not "
            f"enough additional fees to relay; {increase} < "
            f"{mempool_module.format_money(relay)}"
        )


def test_the_relay_increase_is_priced_by_the_vsize_given() -> None:
    """Rule 4 prices the candidate's sigop-adjusted size, as Core's does.

    An increase covering the relay fee at the weight's size falls short at
    ten times it (btclib-org/btclib-node#1357).
    """
    mempool, coin, _, _ = a_mempool_with_a_conflict()
    candidate = a_spend_of([coin])
    rate = mempool.incremental_relay_feerate
    enough = 12_000 + fee_from_vsize(candidate.vsize, rate)
    with pytest.raises(TxRejectedError, match="not enough additional fees"):
        mempool.check_replacement(candidate, enough, 10 * candidate.vsize)


def test_a_replacement_pays_the_incremental_relay_fee_it_was_given() -> None:
    """Rule 4 prices the increase at `-incrementalrelayfee`.

    An increase that covers the default rate falls short of a higher one
    (btclib-org/btclib-node#1596).
    """
    mempool, coin, _, _ = a_mempool_with_a_conflict()
    candidate = a_spend_of([coin])
    fee = 12_000 + fee_from_vsize(candidate.vsize, mempool.incremental_relay_feerate)
    with pytest.raises(TxRejectedError, match="bip125-replacement-disallowed"):
        mempool.check_replacement(candidate, fee, candidate.vsize)
    mempool.incremental_relay_feerate = FeeRate(sats_per_kvbyte=1_000_000)
    with pytest.raises(TxRejectedError, match="not enough additional fees"):
        mempool.check_replacement(candidate, fee, candidate.vsize)


def test_a_conflict_paying_for_what_it_replaces_is_still_refused() -> None:
    """This mempool replaces nothing: Core's reason where it allows none.

    `bitcoind` v31.1 accepts this replacement; the divergence is argued
    at `Mempool.check_replacement`.
    """
    mempool, coin, held, child = a_mempool_with_a_conflict()
    candidate = a_spend_of([coin])
    relay = fee_from_vsize(candidate.vsize, mempool.incremental_relay_feerate)
    with pytest.raises(TxRejectedError) as refused:
        mempool.check_replacement(candidate, 12_000 + relay, candidate.vsize)
    assert refused.value.reason == "bip125-replacement-disallowed"
    assert str(refused.value) == "bip125-replacement-disallowed"
    assert mempool.contains_tx(held)
    assert mempool.contains_tx(child)


def test_a_candidate_with_no_conflict_passes_the_replacement_check() -> None:
    """Spending another output of a held transaction is no conflict."""
    mempool, _, held, _ = a_mempool_with_a_conflict()
    candidate = a_spend_of([(held.id, 1)])
    mempool.check_replacement(candidate, 0, candidate.vsize)


def test_a_confirmed_spend_evicts_its_conflicts_and_their_descendants() -> None:
    """Core's `removeConflicts`: a spend of a spent coin goes, with children."""
    mempool, coin, held, child = a_mempool_with_a_conflict()
    unrelated = a_spend_of([(secrets.token_bytes(32), 0)])
    assert mempool.add_tx(unrelated, 1000)
    confirmed = a_spend_of([coin])
    mempool.remove_tx(confirmed)
    mempool.remove_conflicts(confirmed)
    assert not mempool.contains_tx(held)
    assert not mempool.contains_tx(child)
    assert mempool.contains_tx(unrelated)
    assert set(mempool.outpoint_spender) == {(unrelated.vin[0].prev_out.tx_id, 0)}


def test_remove_dependents_walks_a_chain_of_held_spenders() -> None:
    """`remove_dependents` walks a chain, not only a spender directly held.

    `tx` itself is never a member here -- the case
    `main._reconcile_mempool_for_reorg` calls it for, a disconnected
    transaction past the 10-block cap and never re-added -- so `child`
    and `grandchild`, both already held, are what it has to find through
    `spent_by` alone. btclib-org/btclib-node#1570
    """
    mempool = Mempool(Logger(debug=True))
    dropped = a_spend_of([(secrets.token_bytes(32), 0)])
    child = a_spend_of([(dropped.id, 0)])
    grandchild = a_spend_of([(child.id, 0)])
    assert mempool.add_tx(child, 1000)
    assert mempool.add_tx(grandchild, 1000)

    mempool.remove_dependents(dropped)

    assert not mempool.contains_tx(child)
    assert not mempool.contains_tx(grandchild)
    assert mempool.size == 0


def test_remove_dependents_does_not_revisit_a_shared_descendant() -> None:
    """A descendant reached through two parents is walked once, not twice.

    `dropped` has two outputs; `child_a` and `child_b` each spend one,
    and `grandchild` spends both of theirs in turn -- a diamond, not a
    chain, so the walk reaches `grandchild`'s own wtxid a second time
    once both parents are processed. That second arrival is the
    `if candidate_wtxid in dependents: continue` branch
    `test_remove_dependents_walks_a_chain_of_held_spenders` above, a
    single-parent chain, never reaches. The guard is a shortcut: the
    removed set is the same without it, since `dependents` is a set. What
    it saves is a second `spent_by` lookup for `grandchild`, so that is
    what is counted: `dropped`, `child_a`, `child_b` and `grandchild`
    once each.
    """
    mempool = Mempool(Logger(debug=True))
    dropped = Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(secrets.token_bytes(32), 0),
                script_sig=script.serialize([secrets.token_bytes(32)]),
                sequence=0xFFFFFFFF,
            )
        ],
        vout=[
            TxOut(value=1, script_pub_key=script.serialize([secrets.token_bytes(32)])),
            TxOut(value=1, script_pub_key=script.serialize([secrets.token_bytes(32)])),
        ],
    )
    child_a = a_spend_of([(dropped.id, 0)])
    child_b = a_spend_of([(dropped.id, 1)])
    grandchild = a_spend_of([(child_a.id, 0), (child_b.id, 0)])
    assert mempool.add_tx(child_a, 1000)
    assert mempool.add_tx(child_b, 1000)
    assert mempool.add_tx(grandchild, 1000)
    lookups: list[bytes] = []

    class CountingSpentBy(dict[bytes, set[bytes]]):
        @override
        def get(self, key: bytes, default: object = None) -> Any:
            lookups.append(key)
            return super().get(key, default)

    mempool.spent_by = CountingSpentBy(mempool.spent_by)

    mempool.remove_dependents(dropped)

    assert len(lookups) == 4
    assert not mempool.contains_tx(child_a)
    assert not mempool.contains_tx(child_b)
    assert not mempool.contains_tx(grandchild)
    assert mempool.size == 0


def test_remove_with_descendants_on_an_absent_wtxid_is_a_no_op() -> None:
    """`remove_with_descendants` of a wtxid never held changes nothing.

    `main._evict_immature_or_nonfinal`'s own snapshot-then-skip guard
    covers the case a descendant's own removal already popped a later
    wtxid in the same pass; this is the same absence, reached directly.
    """
    mempool = Mempool(Logger(debug=True))
    held = a_spend_of([(secrets.token_bytes(32), 0)])
    assert mempool.add_tx(held, 1000)

    mempool.remove_with_descendants(secrets.token_bytes(32))

    assert mempool.contains_tx(held)
    assert mempool.size == 1


def test_an_entry_is_counted_and_priced_by_the_vsize_it_came_with() -> None:
    """The sigop-adjusted vsize, not the weight's, is the entry's size.

    Core's `CTxMemPoolEntry::GetTxSize` is what its mempool sums against
    its limit and prices every feerate by (btclib-org/btclib-node#1357).
    """
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    vsize = 10 * tx.vsize
    rate = FeeRate(sats_per_kvbyte=1000)
    fee = fee_from_vsize(tx.vsize, rate)
    mempool.add_tx(tx, fee, vsize)
    assert mempool.bytesize == vsize
    assert mempool.vsizes == {tx.hash: vsize}
    # clears the rate at the weight's size, not at the one it came with
    assert not mempool.meets_fee_rate(tx.hash, 1000)
    mempool.remove_tx(tx)
    assert mempool.bytesize == 0
    assert mempool.vsizes == {}


@pytest.mark.parametrize("heap", ["pushed", "rebuilt"])
def test_eviction_ranks_by_the_vsize_an_entry_came_with(heap: str) -> None:
    """Two entries paying alike: the one priced larger is the worse rate.

    Whether the heap is the one `add_tx` pushed to or the one
    `_rebuild_feerate_heap` made. The rolling minimum it leaves is the
    evicted fee over that size (btclib-org/btclib-node#1357).
    """
    mempool = Mempool(Logger(debug=True))
    dense, plain, rich = (generate_random_transaction() for _ in range(3))
    # the older of two equal rates goes first, so a size misread would
    # evict `plain`
    mempool.add_tx(plain, 1_000)
    mempool.add_tx(dense, 1_000, 10 * dense.vsize)
    if heap == "rebuilt":
        mempool._rebuild_feerate_heap()
    mempool.bytesize_limit = mempool.bytesize + rich.vsize - 1
    mempool.add_tx(rich, 100_000)
    assert not mempool.contains_tx(dense)
    assert mempool.contains_tx(plain)
    evicted_rate = Fraction(1_000, 10 * dense.vsize) * 1000
    assert mempool._rolling_min_fee_rate == float(evicted_rate + 100)


def test_add_tx_records_entry_time_and_height() -> None:
    """`add_tx` stamps `entry_times` and `heights`, `_pop` discards both."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    before = time.time()
    mempool.add_tx(tx, 0, height=712_345)
    after = time.time()
    assert before <= mempool.entry_times[tx.hash] <= after
    assert mempool.heights[tx.hash] == 712_345
    mempool.remove_tx(tx)
    assert tx.hash not in mempool.entry_times
    assert tx.hash not in mempool.heights


def test_add_tx_height_defaults_to_zero() -> None:
    """A caller that never names `height` gets 0, not a `KeyError`."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, 0)
    assert mempool.heights[tx.hash] == 0


def test_mark_broadcast_locally_only_a_held_txid() -> None:
    """`mark_broadcast_locally` is a no-op for a txid this mempool lacks."""
    mempool = Mempool(Logger(debug=True))
    held = generate_random_transaction()
    mempool.add_tx(held, 0)
    absent_txid = secrets.token_bytes(32)
    mempool.mark_broadcast_locally(absent_txid)
    assert mempool.unbroadcast == set()
    mempool.mark_broadcast_locally(held.id)
    assert mempool.unbroadcast == {held.id}


def test_mark_broadcast_discards_from_the_unbroadcast_set() -> None:
    """`mark_broadcast` is `mark_broadcast_locally`'s own reverse."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, 0)
    mempool.mark_broadcast_locally(tx.id)
    assert mempool.unbroadcast == {tx.id}
    mempool.mark_broadcast(tx.id)
    assert mempool.unbroadcast == set()
    # a second call, nothing left to discard, is not an error
    mempool.mark_broadcast(tx.id)
    assert mempool.unbroadcast == set()


def test_pop_discards_a_locally_broadcast_txid_from_unbroadcast() -> None:
    """Leaving the mempool for any reason clears the unbroadcast mark too.

    Core's own `removeUnchecked` calls `RemoveUnbroadcastTx`
    unconditionally on every removal, not only through an explicit
    `RemoveUnbroadcastTx` call. btclib-org/btclib-node#1421
    """
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, 0)
    mempool.mark_broadcast_locally(tx.id)
    assert mempool.unbroadcast == {tx.id}
    mempool.remove_tx(tx)
    assert mempool.unbroadcast == set()


def test_entry_counts_ancestors_and_descendants_including_itself() -> None:
    """`entry`'s own ancestor/descendant counts, sizes and fees, one chain.

    `parent` <- `child` <- `grandchild`: `child`'s own ancestors are
    itself and `parent`, its own descendants itself and `grandchild`
    -- Core's own "including this one" convention for both counts.
    """
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    grandchild = generate_random_transaction(child.id)
    mempool.add_tx(parent, 1_000)
    mempool.add_tx(child, 2_000)
    mempool.add_tx(grandchild, 3_000)
    entry = mempool.entry(child.hash)
    assert entry.ancestor_count == 2
    assert (
        entry.ancestor_size == mempool.vsizes[parent.hash] + mempool.vsizes[child.hash]
    )
    assert entry.ancestor_fees == 1_000 + 2_000
    assert entry.descendant_count == 2
    assert (
        entry.descendant_size
        == mempool.vsizes[child.hash] + mempool.vsizes[grandchild.hash]
    )
    assert entry.descendant_fees == 2_000 + 3_000
    assert entry.depends == [parent.id]
    assert entry.spent_by == [grandchild.id]
    assert entry.fee == 2_000
    assert entry.modified_fee == 2_000
    assert entry.wtxid == child.hash
    assert entry.vsize == mempool.vsizes[child.hash]
    assert entry.weight == child.weight
    assert entry.unbroadcast is False


def test_entry_of_a_root_and_leaf_has_no_depends_or_spentby() -> None:
    """A transaction with no mempool parent or child answers both empty."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, 0)
    entry = mempool.entry(tx.hash)
    assert entry.ancestor_count == 1
    assert entry.descendant_count == 1
    assert entry.depends == []
    assert entry.spent_by == []


def _a_signaling_transaction(prevouthash: bytes | None = None) -> Tx:
    """Build a transaction whose own input signals BIP125 opt-in replacement.

    `mempool_module._MAX_BIP125_RBF_SEQUENCE` (Core's own
    `MAX_BIP125_RBF_SEQUENCE`) is the bound `SignalsOptInRBF` reads; any
    sequence under it opts in, `0` here.
    """
    prevouthash = prevouthash or secrets.token_bytes(32)
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                prev_out=OutPoint(prevouthash, 0),
                script_sig=script.serialize([secrets.token_bytes(32)]),
                sequence=0,
            )
        ],
        vout=[
            TxOut(
                value=50 * 10**8,
                script_pub_key=script.serialize([secrets.token_bytes(32)]),
            )
        ],
    )


def test_entry_is_bip125_replaceable_when_its_own_sequence_signals() -> None:
    """A transaction signaling in its own right is replaceable."""
    mempool = Mempool(Logger(debug=True))
    tx = _a_signaling_transaction()
    mempool.add_tx(tx, 0)
    assert mempool.entry(tx.hash).bip125_replaceable is True


def test_entry_is_bip125_replaceable_when_an_ancestor_signals() -> None:
    """A final child of a signaling, unconfirmed parent is replaceable too.

    Core's own `IsRBFOptIn`: the parent is free to be replaced, and a
    replacement conflicts with everything that spends it.
    """
    mempool = Mempool(Logger(debug=True))
    parent = _a_signaling_transaction()
    child = generate_random_transaction(parent.id)
    assert child.vin[0].sequence == 0xFFFFFFFF
    mempool.add_tx(parent, 0)
    mempool.add_tx(child, 0)
    assert mempool.entry(child.hash).bip125_replaceable is True


def test_entry_is_not_bip125_replaceable_when_nothing_signals() -> None:
    """`bip125_replaceable` is `False` where nothing in the chain signals."""
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    mempool.add_tx(parent, 0)
    mempool.add_tx(child, 0)
    assert mempool.entry(child.hash).bip125_replaceable is False


def test_a_package_hash_does_not_depend_on_the_order() -> None:
    """Core's `GetPackageHash` sorts the wtxids and hashes them held."""
    first, second = secrets.token_bytes(32), secrets.token_bytes(32)
    assert mempool_module.package_hash([first, second]) == mempool_module.package_hash(
        [second, first]
    )
    low, high = sorted([first, second])
    assert (
        mempool_module.package_hash([first, second])
        == hashlib.sha256(low[::-1] + high[::-1]).digest()
    )
    assert mempool_module.package_hash([first]) != mempool_module.package_hash([second])


def test_a_package_hash_sorts_by_the_display_bytes() -> None:
    """Core compares the wtxids from their last held byte, so by display bytes.

    `src/policy/packages.cpp` and `src/test/txpackage_tests.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag. Here the display bytes and
    the held bytes order the pair differently: the hash is of the held
    bytes of `low` then `high`, written out.
    """
    low = b"\x00" * 31 + b"\xff"
    high = b"\x01" + b"\x00" * 31
    expected = "7811638deb8c6514e9189e82d2b182306872bf316ee7cf32e65051779d0ea9c8"
    assert mempool_module.package_hash([high, low]).hex() == expected
    assert mempool_module.package_hash([low, high]).hex() == expected


def test_the_reconsiderable_cache_is_kept_apart_and_cleared_by_a_block() -> None:
    """A key recorded for a package to undo is not a recent reject."""
    mempool = Mempool(Logger(debug=True))
    key = secrets.token_bytes(32)
    assert not mempool.was_recently_rejected_reconsiderable(key)
    mempool.mark_rejected_reconsiderable(key)
    mempool.mark_rejected_reconsiderable(key)
    assert mempool.was_recently_rejected_reconsiderable(key)
    assert not mempool.was_recently_rejected(key)
    assert len(mempool._recent_rejects_reconsiderable_order) == 1
    mempool.note_block_connected()
    assert not mempool.was_recently_rejected_reconsiderable(key)


def test_the_reconsiderable_cache_forgets_the_oldest_past_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same bound as the other cache."""
    monkeypatch.setattr(mempool_module, "_RECENT_REJECTS_CAPACITY", 1)
    mempool = Mempool(Logger(debug=True))
    first, second = secrets.token_bytes(32), secrets.token_bytes(32)
    mempool.mark_rejected_reconsiderable(first)
    mempool.mark_rejected_reconsiderable(second)
    assert not mempool.was_recently_rejected_reconsiderable(first)
    assert mempool.was_recently_rejected_reconsiderable(second)


def a_package(parent_fee: int, child_fee: int) -> list[tuple[Tx, int, int, int | None]]:
    """Return a parent and its child as `add_package` takes them."""
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    return [
        (parent, parent_fee, parent.vsize, None),
        (child, child_fee, child.vsize, None),
    ]


def test_a_package_is_added_whole_with_the_fee_given_to_each() -> None:
    """Both are held, parent first, with their own fee and vsize."""
    mempool = Mempool(Logger(debug=True))
    members = a_package(0, 1000)
    assert mempool.add_package(members, height=7)
    for tx, fee, vsize, _ in members:
        assert mempool.contains_tx(tx)
        assert mempool.fees[tx.hash] == fee
        assert mempool.vsizes[tx.hash] == vsize
    assert mempool.size == 2


def test_a_package_makes_room_by_evicting_what_pays_less() -> None:
    """The worst are evicted, and the rolling minimum rises."""
    mempool = Mempool(Logger(debug=True))
    members = a_package(0, 10_000)
    cheap, dear = generate_random_transaction(), generate_random_transaction()
    mempool.add_tx(cheap, 0)
    mempool.add_tx(dear, 10**7)
    mempool.bytesize_limit = mempool.bytesize + members[0][2] + members[1][2] - 1
    assert mempool.add_package(members, height=0)
    assert not mempool.contains_tx(cheap)
    assert mempool.contains_tx(dear)
    assert all(mempool.contains_tx(tx) for tx, *_ in members)
    assert mempool.get_min_fee_rate().sats_per_kvbyte > 0


def test_a_package_paying_less_than_the_worst_is_refused_and_raises_the_floor() -> None:
    """Nothing is evicted, nothing added, and the entry stays in the heap."""
    mempool = Mempool(Logger(debug=True))
    members = a_package(0, 100)
    incumbent = generate_random_transaction()
    mempool.add_tx(incumbent, 10**7)
    mempool.bytesize_limit = mempool.bytesize + members[0][2] + members[1][2] - 1
    assert not mempool.add_package(members, height=0)
    assert mempool.contains_tx(incumbent)
    assert mempool.size == 1
    assert mempool.get_min_fee_rate().sats_per_kvbyte > 0
    # the incumbent is still the worst, so a package paying more evicts it
    better = a_package(0, 10**8)
    assert mempool.add_package(better, height=0)
    assert not mempool.contains_tx(incumbent)


def test_a_package_whose_mempool_parent_is_evicted_for_room_is_not_added() -> None:
    """A parent the room costs leaves the package nothing to spend."""
    mempool = Mempool(Logger(debug=True))
    held = generate_random_transaction()
    mempool.add_tx(held, 0)
    parent = generate_random_transaction(held.id)
    child = generate_random_transaction(parent.id)
    members = [(parent, 0, parent.vsize, None), (child, 10**6, child.vsize, None)]
    # the package fits once `held` is gone, and not before
    mempool.bytesize_limit = mempool.bytesize + parent.vsize + child.vsize - 1
    assert not mempool.add_package(members, height=0)
    assert mempool.size == 0


def test_a_package_over_the_limit_is_evicted_whole() -> None:
    """Nothing of it stays where the limit leaves it no room."""
    mempool = Mempool(Logger(debug=True))
    members = a_package(0, 10_000)
    mempool.bytesize_limit = 0
    assert not mempool.add_package(members, height=0)
    assert mempool.size == 0


def test_a_package_left_only_its_parent_by_the_limit_is_removed_with_it() -> None:
    """If only the child is evicted after the add, the parent goes too."""
    mempool = Mempool(Logger(debug=True))
    members = a_package(10**6, 10)
    mempool.bytesize_limit = members[0][2]
    assert not mempool.add_package(members, height=0)
    assert mempool.size == 0


def test_a_staged_transaction_is_there_only_while_staged() -> None:
    """Its outpoints and indexes are held, then every counter is as before."""
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    mempool.add_tx(generate_random_transaction())
    sequence, updated = mempool.sequence, mempool.transactions_updated
    with mempool.staged(parent, 0, parent.vsize):
        assert mempool.contains_tx(parent)
        assert mempool._parents(child) == [parent.hash]
    assert not mempool.contains_tx(parent)
    assert (mempool.sequence, mempool.transactions_updated) == (sequence, updated)
    assert mempool.size == 1
    assert (parent.vin[0].prev_out.tx_id, 0) not in mempool.outpoint_spender


def test_a_staged_transaction_is_taken_out_when_the_block_raises() -> None:
    """The mempool is left as it was by a check that fails."""
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()

    def fails() -> None:
        with mempool.staged(parent, 0, parent.vsize):
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        fails()
    assert mempool.size == 0


def test_a_delta_stacks_and_is_kept_for_a_transaction_not_held() -> None:
    """Deltas add up whether or not the txid is in the mempool.

    A delta of zero is not kept, and one that comes back to zero is
    dropped, as `bitcoind` v31.1's `getprioritisedtransactions` answers.
    """
    mempool = Mempool(Logger(debug=True))
    txid = secrets.token_bytes(32)
    mempool.prioritise(txid, 0)
    assert mempool.deltas == {}
    mempool.prioritise(txid, 100)
    mempool.prioritise(txid, -30)
    assert mempool.delta(txid) == 70
    assert mempool.prioritised() == [(txid, 70, None)]
    mempool.prioritise(txid, -70)
    assert mempool.deltas == {}
    assert mempool.delta(txid) == 0


@pytest.mark.parametrize(
    ("first", "second", "kept"),
    [
        (2**63 - 1, 5, 2**63 - 1),
        (-(2**63), -5, -(2**63)),
        (2**63 - 1, -(2**63), -1),
    ],
)
def test_a_delta_saturates_at_the_int64_range(
    first: int, second: int, kept: int
) -> None:
    """`SaturatingAdd` clamps the sum, as `bitcoind` v31.1 does."""
    mempool = Mempool(Logger(debug=True))
    txid = secrets.token_bytes(32)
    mempool.prioritise(txid, first)
    mempool.prioritise(txid, second)
    assert mempool.delta(txid) == kept


def test_a_delta_is_applied_once_the_transaction_enters() -> None:
    """The fee paid is kept, the modified fee carries the delta."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.prioritise(tx.id, 500)
    assert mempool.add_tx(tx, 1_000)
    assert mempool.fees[tx.hash] == 1_000
    assert mempool.modified_fee(tx.hash) == 1_500
    assert mempool.prioritised() == [(tx.id, 500, 1_500)]
    mempool.prioritise(tx.id, -2_000)
    assert mempool.modified_fee(tx.hash) == -500
    assert mempool.fees[tx.hash] == 1_000


def test_prioritised_lists_by_internal_txid_bytes() -> None:
    """Core's `std::map<Txid, CAmount>` orders by the bytes it holds."""
    mempool = Mempool(Logger(debug=True))
    txids = [secrets.token_bytes(32) for _ in range(8)]
    for txid in txids:
        mempool.prioritise(txid, 1)
    listed = [txid for txid, _, _ in mempool.prioritised()]
    assert listed == sorted(txids, key=lambda txid: txid[::-1])
    assert listed != sorted(txids)


def a_diamond_with_deltas() -> tuple[Mempool, list[Tx]]:
    """Hold `root`, `left`, `right` and `tip` paying 1000 to 4000, with deltas.

    `left` has 10, `right` -3 and `tip` 100.
    """
    mempool = Mempool(Logger(debug=True))
    root = generate_random_transaction()
    left = generate_random_transaction(root.id)
    right = a_spend_of([(root.id, 1)])
    tip = a_transaction_spending(left.id, right.id)
    txs = [root, left, right, tip]
    for number, tx in enumerate(txs, start=1):
        assert mempool.add_tx(tx, 1_000 * number)
    for tx, delta in zip(txs[1:], (10, -3, 100), strict=True):
        mempool.prioritise(tx.id, delta)
    return mempool, txs


def test_an_entry_has_its_own_modified_fee() -> None:
    """`modified` is `base` and the delta; `base` is what was paid."""
    mempool, (root, left, right, tip) = a_diamond_with_deltas()
    assert [mempool.entry(tx.hash).fee for tx in (root, left, right, tip)] == [
        1_000,
        2_000,
        3_000,
        4_000,
    ]
    assert [mempool.entry(tx.hash).modified_fee for tx in (root, left, right, tip)] == [
        1_000,
        2_010,
        2_997,
        4_100,
    ]


def test_an_entry_sums_the_modified_fees_of_its_ancestors() -> None:
    """Core's `CalculateAncestorData`: the transaction and its ancestors."""
    mempool, (root, left, right, tip) = a_diamond_with_deltas()
    assert mempool.entry(root.hash).ancestor_fees == 1_000
    assert mempool.entry(left.hash).ancestor_fees == 1_000 + 2_010
    assert mempool.entry(right.hash).ancestor_fees == 1_000 + 2_997
    assert mempool.entry(tip.hash).ancestor_fees == 1_000 + 2_010 + 2_997 + 4_100


def test_an_entry_sums_the_modified_fees_of_its_descendants() -> None:
    """Core's `CalculateDescendantData`: the transaction and what spends it."""
    mempool, (root, left, right, tip) = a_diamond_with_deltas()
    assert mempool.entry(root.hash).descendant_fees == 1_000 + 2_010 + 2_997 + 4_100
    assert mempool.entry(left.hash).descendant_fees == 2_010 + 4_100
    assert mempool.entry(right.hash).descendant_fees == 2_997 + 4_100
    assert mempool.entry(tip.hash).descendant_fees == 4_100


def test_prioritising_a_held_transaction_is_an_update_and_no_sequence_event() -> None:
    """`nTransactionsUpdated` counts it, `GetSequence` does not.

    A transaction not held is neither.
    """
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, 1_000)
    sequence, updated = mempool.sequence, mempool.transactions_updated
    mempool.prioritise(secrets.token_bytes(32), 5)
    assert (mempool.sequence, mempool.transactions_updated) == (sequence, updated)
    mempool.prioritise(tx.id, 5)
    assert mempool.sequence == sequence
    assert mempool.transactions_updated == updated + 1


def test_the_feerate_it_evicts_by_includes_the_delta_of_a_new_entry() -> None:
    """A free transaction with a delta outranks one paying a little."""
    mempool = Mempool(Logger(debug=True))
    free, paying, keeper = (generate_random_transaction() for _ in range(3))
    mempool.prioritise(free.id, 10_000)
    mempool.add_tx(free, 0)
    mempool.add_tx(paying, 100)
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    assert mempool.add_tx(keeper, 5_000)
    assert mempool.contains_tx(free)
    assert not mempool.contains_tx(paying)


def test_a_delta_set_on_a_held_transaction_changes_who_is_evicted() -> None:
    """The heap holds a fresh entry at the new rate, not the one it pushed."""
    mempool = Mempool(Logger(debug=True))
    worse, better, keeper = (generate_random_transaction() for _ in range(3))
    mempool.add_tx(worse, 100)
    mempool.add_tx(better, 5_000)
    mempool.prioritise(worse.id, 10_000)
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    assert mempool.add_tx(keeper, 20_000)
    assert mempool.contains_tx(worse)
    assert not mempool.contains_tx(better)


def test_only_the_last_delta_of_a_held_transaction_ranks_it() -> None:
    """An entry pushed for an earlier delta no longer stands for the wtxid.

    No sequence event lies between the two calls, so they need pushes
    of their own to be told apart.
    """
    mempool = Mempool(Logger(debug=True))
    raised, other, keeper = (generate_random_transaction() for _ in range(3))
    mempool.add_tx(raised, 5_000)
    mempool.add_tx(other, 3_000)
    mempool.prioritise(raised.id, -4_999)
    mempool.prioritise(raised.id, 10_000)
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    assert mempool.add_tx(keeper, 20_000)
    assert mempool.contains_tx(raised)
    assert not mempool.contains_tx(other)


def test_a_negative_delta_makes_a_well_paying_transaction_the_one_evicted() -> None:
    """The delta is read both ways: against the fee as well as for it."""
    mempool = Mempool(Logger(debug=True))
    rich, plain, keeper = (generate_random_transaction() for _ in range(3))
    mempool.add_tx(rich, 100_000)
    mempool.add_tx(plain, 5_000)
    mempool.prioritise(rich.id, -99_000)
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    assert mempool.add_tx(keeper, 20_000)
    assert not mempool.contains_tx(rich)
    assert mempool.contains_tx(plain)


def test_a_rebuilt_heap_ranks_by_the_modified_feerate() -> None:
    """`_rebuild_feerate_heap` is what bounds the heap, and keeps the delta."""
    mempool = Mempool(Logger(debug=True))
    worse, better, keeper = (generate_random_transaction() for _ in range(3))
    mempool.add_tx(worse, 100)
    mempool.add_tx(better, 5_000)
    mempool.prioritise(worse.id, 10_000)
    mempool._rebuild_feerate_heap()
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    assert mempool.add_tx(keeper, 20_000)
    assert mempool.contains_tx(worse)
    assert not mempool.contains_tx(better)


def test_prioritising_again_and_again_does_not_grow_the_heap_without_bound() -> None:
    """Each call leaves a stale entry; the heap is rebuilt past twice `size`."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, 1_000)
    for _ in range(50):
        mempool.prioritise(tx.id, 1)
    assert len(mempool._feerate_heap) <= 2 * mempool.size
    assert mempool.delta(tx.id) == 50


def test_eviction_bumps_the_rolling_minimum_by_the_modified_rate_it_evicts() -> None:
    """Core's `removed` is the chunk's modified feerate."""
    mempool = Mempool(Logger(debug=True))
    victim, keeper = generate_random_transaction(), generate_random_transaction()
    mempool.add_tx(victim, 0)
    mempool.prioritise(victim.id, 7_000)
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    mempool.add_tx(keeper, 10**6)
    assert not mempool.contains_tx(victim)
    expected = Fraction(7_000, victim.vsize) * 1000 + 100
    assert mempool._rolling_min_fee_rate == float(expected)


def test_a_package_is_judged_by_its_modified_feerate() -> None:
    """Deltas on its members lift it over an incumbent that pays more."""
    mempool = Mempool(Logger(debug=True))
    members = a_package(0, 0)
    incumbent = generate_random_transaction()
    mempool.add_tx(incumbent, 10**5)
    mempool.bytesize_limit = mempool.bytesize + members[0][2] + members[1][2] - 1
    assert not mempool.add_package(members, height=0)
    mempool.prioritise(members[0][0].id, 10**6)
    mempool.prioritise(members[1][0].id, 10**6)
    assert mempool.add_package(members, height=0)
    assert not mempool.contains_tx(incumbent)


def test_a_package_is_ranked_against_the_modified_feerate_of_what_it_evicts() -> None:
    """An incumbent with a negative delta is worth less than it paid."""
    mempool = Mempool(Logger(debug=True))
    members = a_package(0, 10_000)
    incumbent = generate_random_transaction()
    mempool.add_tx(incumbent, 10**6)
    mempool.prioritise(incumbent.id, -(10**6))
    mempool.bytesize_limit = mempool.bytesize + members[0][2] + members[1][2] - 1
    assert mempool.add_package(members, height=0)
    assert not mempool.contains_tx(incumbent)


def a_package_at(rate: int) -> list[tuple[Tx, int, int, int | None]]:
    """Return a package whose child pays for both at `rate` sat/vB."""
    parent, child = a_package(0, 0)
    size = parent[2] + child[2]
    return [parent, (child[0], rate * size, child[2], None)]


def test_a_refused_package_leaves_the_worst_entry_at_its_modified_rate() -> None:
    """The entry put back after a refusal is not the base-rate one.

    `worst` paid 200 sat/vB and is worth 50 with its delta, `other` pays
    100: a package at 10 is refused, and one at 60 still evicts `worst`
    and not `other`.
    """
    mempool = Mempool(Logger(debug=True))
    worst, other = generate_random_transaction(), generate_random_transaction()
    mempool.add_tx(worst, 200 * worst.vsize)
    mempool.prioritise(worst.id, -150 * worst.vsize)
    mempool.add_tx(other, 100 * other.vsize)
    poor = a_package_at(10)
    mempool.bytesize_limit = mempool.bytesize
    assert not mempool.add_package(poor, height=0)
    assert mempool.contains_tx(worst)
    middling = a_package_at(60)
    mempool.bytesize_limit = mempool.bytesize + middling[0][2] + middling[1][2] - 1
    assert mempool.add_package(middling, height=0)
    assert not mempool.contains_tx(worst)
    assert mempool.contains_tx(other)


def test_a_replacement_counts_the_delta_of_what_it_replaces() -> None:
    """Core's `PaysForRBF` reads the conflicts' modified fees."""
    mempool, coin, held, child = a_mempool_with_a_conflict()
    mempool.prioritise(held.id, 1_000)
    mempool.prioritise(child.id, 500)
    candidate = a_spend_of([coin])
    with pytest.raises(TxRejectedError) as refused:
        mempool.check_replacement(candidate, 13_499, candidate.vsize)
    assert refused.value.reason == "insufficient fee"
    shortfall = f"{mempool_module.format_money(13_499)} < "
    assert str(refused.value).endswith(shortfall + mempool_module.format_money(13_500))


def test_a_replacement_counts_its_own_delta() -> None:
    """The candidate's fee is its modified one, in the words of the refusal."""
    mempool, coin, _, _ = a_mempool_with_a_conflict()
    candidate = a_spend_of([coin])
    mempool.prioritise(candidate.id, 100)
    with pytest.raises(TxRejectedError) as refused:
        mempool.check_replacement(candidate, 11_899, candidate.vsize)
    assert str(refused.value).endswith(
        f"{mempool_module.format_money(11_999)} < {mempool_module.format_money(12_000)}"
    )


def test_a_block_takes_the_delta_of_what_it_holds_and_of_what_it_conflicts_with() -> (
    None
):
    """Core's `removeForBlock` and `removeConflicts` clear it, a child's stays.

    The child of a conflict is evicted with it but is not the conflict, and
    its delta is kept as it is for any transaction evicted.
    """
    mempool, coin, held, child = a_mempool_with_a_conflict()
    mined = generate_random_transaction()
    never_held = generate_random_transaction()
    mempool.add_tx(mined, 1_000)
    for tx in (held, child, mined, never_held):
        mempool.prioritise(tx.id, 5)
    block = [
        a_spend_of([coin]),
        mined,
        never_held,
    ]
    mempool.remove_for_block(block)
    assert mempool.deltas == {child.id: 5}
    assert mempool.size == 0


def test_a_block_clears_the_delta_of_a_coinbase_it_holds() -> None:
    """Core iterates the whole of `vtx`, the coinbase included."""
    mempool = Mempool(Logger(debug=True))
    coinbase = generate_random_transaction()
    mempool.prioritise(coinbase.id, 5)
    mempool.remove_for_block([coinbase])
    assert mempool.deltas == {}


def test_a_delta_survives_an_eviction() -> None:
    """Only a block clears it: eviction and removal keep it.

    Core's `mining_prioritisetransaction.py` at v31.1 says so, and keeps
    it through a replacement.
    """
    mempool = Mempool(Logger(debug=True))
    victim, keeper = generate_random_transaction(), generate_random_transaction()
    mempool.add_tx(victim, 0)
    mempool.prioritise(victim.id, -5)
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    mempool.add_tx(keeper, 10**6)
    assert not mempool.contains_tx(victim)
    assert mempool.delta(victim.id) == -5
    mempool.remove_tx(victim)
    mempool.remove_with_descendants(keeper.hash)
    assert mempool.delta(victim.id) == -5


def test_a_feefilter_is_held_against_the_fee_paid() -> None:
    """BIP133 asks what the transaction pays, as Core's `GetFee` does."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, 0)
    mempool.prioritise(tx.id, 10**6)
    assert not mempool.meets_fee_rate(tx.hash, 1_000)


def test_a_held_modified_fee_saturates_at_each_step_as_core_keeps_it() -> None:
    """The entry's modified fee is not its fee plus the delta at the bound.

    `bitcoind` v31.1, a held transaction paying 1410: a delta of
    9223372036854775797 gives a modified fee of 9223372036854775807, and
    the opposite delta then gives an empty list and a modified fee of 10,
    not 1410. btclib-org/btclib-node#1502
    """
    mempool = Mempool(Logger(debug=True))
    tx = a_transaction_spending(secrets.token_bytes(32))
    mempool.add_tx(tx, 1410, tx.vsize, height=0)
    wtxid = tx.hash
    mempool.prioritise(tx.id, 2**63 - 11)
    assert mempool.modified_fee(wtxid) == 2**63 - 1
    assert mempool.prioritised() == [(tx.id, 2**63 - 11, 2**63 - 1)]
    mempool.prioritise(tx.id, -(2**63 - 11))
    assert mempool.prioritised() == []
    assert mempool.modified_fee(wtxid) == 10
    assert mempool.fees[wtxid] == 1410


def test_the_trickle_order_is_the_order_a_block_takes() -> None:
    """A parent its child pays for goes first, as one chunk, then a single.

    Core's `CompareMiningScoreWithTopology` reads the clusters' chunks:
    the parent pays nothing, its child pays for both above the single's
    rate, and the single pays above the parent's own rate.
    """
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    single = generate_random_transaction()
    mempool.add_tx(parent, 0)
    mempool.add_tx(child, 100 * (parent.vsize + child.vsize))
    mempool.add_tx(single, 10 * single.vsize)
    wtxids = [single.hash, child.hash, parent.hash]
    keys = mempool.mining_order_keys(wtxids)
    assert sorted(wtxids, key=keys.__getitem__) == [
        parent.hash,
        child.hash,
        single.hash,
    ]


def test_equal_chunks_go_by_txid_in_its_internal_byte_order() -> None:
    """Core's `fallback_order`, the txid as stored, breaks a tie."""
    mempool = Mempool(Logger(debug=True))
    txs = [generate_random_transaction() for _ in range(6)]
    for tx in txs:
        mempool.add_tx(tx, 1_000, 100, 400)
    keys = mempool.mining_order_keys([tx.hash for tx in txs])
    by_order = sorted(txs, key=lambda tx: keys[tx.hash])
    assert by_order == sorted(txs, key=lambda tx: tx.id[::-1])


def test_the_graph_follows_what_the_mempool_holds() -> None:
    """Added, linked to its parent, prioritised, and removed with it."""
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    mempool.add_tx(parent, 0, 100, 400)
    mempool.add_tx(child, 1_000, 100, 400)
    graph = mempool.graph
    assert graph.cluster(parent.hash) == [parent.hash, child.hash]
    assert graph.chunk_feerate(parent.hash) == (1_000, 800)
    mempool.prioritise(parent.id, 3_000)
    assert graph.do_work(10**9)
    chunks = graph.chunks(child.hash)
    assert [chunk.refs for chunk in chunks] == [[parent.hash], [child.hash]]
    mempool.remove_with_descendants(parent.hash)
    assert len(graph) == 0


def test_a_parent_held_after_its_child_is_linked_to_it() -> None:
    """A reorg puts a parent back under the child the mempool kept."""
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    mempool.add_tx(child, 1_000, 100, 400)
    mempool.add_tx(parent, 0, 100, 400)
    assert mempool.graph.cluster(child.hash) == [child.hash]
    mempool.update_transactions_from_block([parent.hash])
    assert mempool.graph.cluster(child.hash) == [parent.hash, child.hash]


def test_a_reorg_trims_a_cluster_past_64_transactions() -> None:
    """Core's `Trim` takes the last of a chain of 64 under a re-added parent.

    A chain spends an output of `p`, which is not held, as after `p`
    confirmed; a reorg brings `p` back. The chain's last transaction
    would be the cluster's 65th, and goes.
    """
    mempool = Mempool(Logger(debug=True))
    p = generate_random_transaction()
    chain = [generate_random_transaction(p.id)]
    while len(chain) < 64:
        chain.append(generate_random_transaction(chain[-1].id))
    for tx in chain:
        assert mempool.add_tx(tx, 1_000, 100, 400)
    mempool.check_cluster(p, p.vsize)
    assert mempool.add_tx(p, 1_000, 100, 400)
    gone = generate_random_transaction()
    mempool.update_transactions_from_block([gone.hash, p.hash])
    assert mempool.graph.cluster(p.hash) == [p.hash] + [tx.hash for tx in chain[:63]]
    assert set(mempool.transactions) == {p.hash} | {tx.hash for tx in chain[:63]}


def test_a_re_added_child_counts_no_held_child_of_its_parent() -> None:
    """Core's graph links a re-added parent to its held children last.

    `r` spends a re-added `p` under which a chain of 64 is held: its
    cluster is `p` and itself until `update_transactions_from_block`,
    which keeps `r`, paying more than the chain, and trims two of it.
    """
    mempool = Mempool(Logger(debug=True))
    p = generate_random_transaction()
    chain = [generate_random_transaction(p.id)]
    while len(chain) < 64:
        chain.append(generate_random_transaction(chain[-1].id))
    for tx in chain:
        assert mempool.add_tx(tx, 1_000, 100, 400)
    assert mempool.add_tx(p, 1_000, 100, 400)
    # another output of `p`'s, which the mempool does not check
    r = Tx(1, 0, [TxIn(OutPoint(p.id, 1), b"", 0xFFFFFFFF)], chain[0].vout)
    mempool.check_cluster(r, 100, 400)
    assert mempool.add_tx(r, 5_000, 100, 400)
    mempool.update_transactions_from_block([p.hash, r.hash])
    kept = {p.hash, r.hash} | {tx.hash for tx in chain[:62]}
    assert set(mempool.transactions) == kept


def test_a_staged_transaction_never_enters_the_graph() -> None:
    """`staged` leaves the clusters as they were, optimal included."""
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    mempool.add_tx(parent, 0)
    child = generate_random_transaction(parent.id)
    with mempool.staged(child, 1_000, child.vsize):
        assert child.hash not in mempool.graph
    assert mempool.graph.cluster(parent.hash) == [parent.hash]
    assert mempool.graph.do_work(0)


def test_a_block_leaves_the_clusters_optimal() -> None:
    """Core's `removeForBlock` relinearizes what the block leaves."""
    mempool = Mempool(Logger(debug=True))
    grand = generate_random_transaction()
    parent = generate_random_transaction(grand.id)
    child = generate_random_transaction(parent.id)
    for tx, fee in ((grand, 0), (parent, 5_000), (child, 0)):
        mempool.add_tx(tx, fee)
    mempool.remove_for_block([grand])
    assert mempool.graph.do_work(0)
    assert mempool.graph.cluster(child.hash) == [parent.hash, child.hash]


def test_a_weight_stands_in_where_the_caller_has_none() -> None:
    """`tx.weight`, or four times a `vsize` above the one it rounds up to."""
    tx = generate_random_transaction()
    own = -(-tx.weight // 4)
    assert mempool_module._weight(tx, None) == tx.weight
    assert mempool_module._weight(tx, own) == tx.weight
    assert mempool_module._weight(tx, own + 5) == 4 * (own + 5)


def test_a_cluster_is_sized_by_weight_as_core_s_graph_is() -> None:
    """404,000 weight units fit, though the two vsizes add to 101,001."""
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    assert mempool.add_tx(parent, 100, 50_501, 202_001)
    mempool.check_cluster(child, 50_500, 201_999)
    with pytest.raises(TxRejectedError, match="too-large-cluster"):
        mempool.check_cluster(child, 50_500, 202_000)


def test_a_staged_parent_counts_by_the_weight_it_is_given() -> None:
    """A child of a staged parent is held to 404,000 weight units by weight."""
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    with mempool.staged(parent, 100, 50_501, 202_001):
        mempool.check_cluster(child, 50_500, 201_999)
        with pytest.raises(TxRejectedError, match="too-large-cluster"):
            mempool.check_cluster(child, 50_500, 202_000)


def test_a_cluster_follows_no_unlinked_spend_either_way() -> None:
    """From the held child of a re-added parent, as from the parent."""
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    child = generate_random_transaction(parent.id)
    assert mempool.add_tx(child, 1_000, 100, 400)
    assert mempool.add_tx(parent, 1_000, 100, 400)
    for seed in (parent, child):
        members, _ = mempool.cluster([seed.hash], max_count=64, max_weight=10**6)
        assert members == {seed.hash}
    mempool.update_transactions_from_block([parent.hash])
    members, _ = mempool.cluster([child.hash], max_count=64, max_weight=10**6)
    assert members == {parent.hash, child.hash}


def test_a_staged_parent_s_staged_child_counts_toward_a_sibling() -> None:
    """A spend of a staged parent, outside the graph, is followed."""
    mempool = Mempool(Logger(debug=True))
    parent = generate_random_transaction()
    sibling = generate_random_transaction(parent.id)
    child = a_transaction_spending(parent.id)
    with (
        mempool.staged(parent, 0, 100, 400),
        mempool.staged(sibling, 0, 50_000, 200_000),
    ):
        mempool.check_cluster(child, 50_900, 203_600)
        with pytest.raises(TxRejectedError, match="too-large-cluster"):
            mempool.check_cluster(child, 50_901, 203_601)


def test_the_stored_txid_follows_the_entry_in_and_out() -> None:
    """`txids` holds each held wtxid's txid and drops it with the entry."""
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    assert mempool.add_tx(tx, fee=100)
    assert mempool.txids == {tx.hash: tx.id}
    mempool.remove_tx(tx)
    assert mempool.txids == {}


def test_a_block_returns_what_it_held_and_reports_only_its_conflicts() -> None:
    """The fee estimator learns of a block's own transactions from the block.

    Core's `removeForBlock` signals `TransactionRemovedFromMempool` for
    the conflicts and their descendants, never for what the block holds,
    which `MempoolTransactionsRemovedForBlock` lists in block order.
    """
    mempool, coin, held, child = a_mempool_with_a_conflict()
    mined = generate_random_transaction()
    mempool.add_tx(mined, 1_000, height=7)
    told: list[bytes] = []
    mempool.removal_listener = told.append
    removed = mempool.remove_for_block([mined, a_spend_of([coin])])
    assert removed == [RemovedTx(mined.id, 1_000, mined.vsize, 7)]
    assert sorted(told) == sorted([held.id, child.id])


def test_every_removal_but_a_block_s_and_a_staged_one_is_reported() -> None:
    """A removal and an eviction are told; a staged entry is not."""
    mempool = Mempool(Logger(debug=True))
    told: list[bytes] = []
    mempool.removal_listener = told.append
    gone, victim, keeper = (generate_random_transaction() for _ in range(3))
    mempool.add_tx(gone, 0)
    mempool.remove_tx(gone)
    with mempool.staged(keeper, 0, keeper.vsize):
        pass
    mempool.add_tx(victim, 0)
    mempool.bytesize_limit = mempool.bytesize + keeper.vsize - 1
    mempool.add_tx(keeper, 10**6)
    assert told == [gone.id, victim.id]


def test_a_block_on_an_empty_mempool_returns_nothing() -> None:
    """No transaction is looked up: there is nothing to remove."""
    mempool = Mempool(Logger(debug=True))
    assert mempool.remove_for_block([generate_random_transaction()]) == []
