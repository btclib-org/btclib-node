# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Mempool`'s bookkeeping, eviction and its rolling minimum feerate."""

import secrets
import time
from fractions import Fraction

from btclib.fee import FeeRate, fee_from_vsize
from btclib.script import script
from btclib.script.witness import Witness
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

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
    child_b = generate_random_transaction(parent.id)
    grandchild = a_transaction_spending(child_a.id, child_b.id)
    keeper = generate_random_transaction()
    mempool.add_tx(parent, 0)
    mempool.add_tx(child_a, 0)
    mempool.add_tx(child_b, 0)
    mempool.add_tx(grandchild, 0)
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
