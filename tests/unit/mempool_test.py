# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Mempool`'s bookkeeping, eviction and its rolling minimum feerate."""

import secrets
import time
from fractions import Fraction
from typing import TYPE_CHECKING

from btclib.fee import FeeRate, fee_from_vsize
from btclib.script.witness import Witness

from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from tests.helpers import generate_random_transaction

if TYPE_CHECKING:
    from btclib.tx.tx import Tx


def a_witness_transaction() -> Tx:
    """Return a random transaction whose txid and wtxid actually differ."""
    # a txid and a wtxid are the same bytes until there is a witness, and
    # an assertion about one would then pass by naming the other
    tx = generate_random_transaction()
    tx.vin[0].script_witness = Witness([secrets.token_bytes(32)])
    return tx


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
    # bitcoin/bitcoin@58a7869f86) bumps the rolling minimum from the
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
