# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Mempool`'s bookkeeping, eviction and its rolling minimum feerate."""

import secrets
import time
from fractions import Fraction

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
    relay = fee_from_vsize(candidate.vsize, mempool_module._INCREMENTAL_RELAY_FEE_RATE)
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
    rate = mempool_module._INCREMENTAL_RELAY_FEE_RATE
    enough = 12_000 + fee_from_vsize(candidate.vsize, rate)
    with pytest.raises(TxRejectedError, match="not enough additional fees"):
        mempool.check_replacement(candidate, enough, 10 * candidate.vsize)


def test_a_conflict_paying_for_what_it_replaces_is_still_refused() -> None:
    """This mempool replaces nothing: Core's reason where it allows none.

    `bitcoind` v31.1 accepts this replacement; the divergence is argued
    at `Mempool.check_replacement`.
    """
    mempool, coin, held, child = a_mempool_with_a_conflict()
    candidate = a_spend_of([coin])
    relay = fee_from_vsize(candidate.vsize, mempool_module._INCREMENTAL_RELAY_FEE_RATE)
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
