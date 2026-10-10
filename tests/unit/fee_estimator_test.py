# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`btclib_node.fee_estimator`: Core's `CBlockPolicyEstimator`.

`test_block_policy_estimates` is Core's `policyestimator_tests.cpp`
(at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), ported check for check.
"""

import hashlib
import math
import os
import struct
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from btclib import var_int

from btclib_node.fee_estimator import (
    EstimationResult,
    FeeEstimateHorizon,
    FeeEstimator,
    RemovedTx,
    _divide,
    is_current_for_fee_estimation,
    llround,
    track_accepted,
)
from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from tests import LogLines
from tests.unit.mempool_test import a_transaction_spending

if TYPE_CHECKING:
    from pathlib import Path

# the `fee_estimates.dat` a fresh regtest bitcoind v31.1.0 writes at its
# first stop, 309269 bytes
_CORE_EMPTY_FILE_SHA256 = (
    "3adca9e6d7ee02bb3088474896f4eaa52b719c8976db0a7e49bcdc7366000f2d"
)

# Core's test transaction: one input with a 128-byte scriptSig, one
# empty output
_VSIZE = 188
_BASE_FEE = 2000
_DELTA_FEE = 100
_BASE_RATE = _BASE_FEE * 1000 // _VSIZE


def an_estimator(tmp_path: Path) -> tuple[FeeEstimator, LogLines]:
    """Return an estimator with no file to read, and what it logs."""
    logger = Logger(debug=True)
    lines = LogLines()
    logger.addHandler(lines)
    return FeeEstimator(tmp_path / "fee_estimates.dat", logger), lines


def _add(estimator: FeeEstimator, txid: bytes, fee: int, height: int) -> RemovedTx:
    estimator.process_transaction(
        txid,
        fee,
        _VSIZE,
        height,
        limit_bypassed=False,
        in_package=False,
        chain_current=True,
        has_no_mempool_parents=True,
    )
    return RemovedTx(txid, fee, _VSIZE, height)


def _txid(blocknum: int, j: int, k: int) -> bytes:
    return (10000 * blocknum + 100 * j + k).to_bytes(32, "little")


def _fill(estimator: FeeEstimator, blocknum: int) -> list[list[RemovedTx]]:
    """Add four transactions at each of the ten fees, as Core's test does."""
    return [
        [
            _add(estimator, _txid(blocknum, j, k), _BASE_FEE * (j + 1), blocknum)
            for k in range(4)
        ]
        for j in range(10)
    ]


def test_block_policy_estimates(tmp_path: Path) -> None:  # noqa: C901, PLR0912
    """Core's `BlockPolicyEstimates`, the same transactions and checks."""
    estimator, _ = an_estimator(tmp_path)
    held: list[list[RemovedTx]] = [[] for _ in range(10)]
    blocknum = 0
    # higher fees are included more often: the highest in every block,
    # the lowest in one in ten
    while blocknum < 200:
        for j, added in enumerate(_fill(estimator, blocknum)):
            held[j] += added
        block: list[RemovedTx] = []
        for h in range(blocknum % 10 + 1):
            while held[9 - h]:
                block.append(held[9 - h].pop())
        blocknum += 1
        estimator.process_block(block, blocknum)
        if blocknum == 3:
            # three buckets combined: 100%, 100% and 90%, 97% on average
            assert estimator.estimate_fee(1) == 0
            assert estimator.estimate_fee(2) < 9 * _BASE_RATE + _DELTA_FEE
            assert estimator.estimate_fee(2) > 9 * _BASE_RATE - _DELTA_FEE

    original = []
    for i in range(1, 10):
        original.append(estimator.estimate_fee(i))
        if i > 2:
            assert original[i - 1] <= original[i - 2]
        mult = 11 - i
        if i % 2 == 0:
            assert original[i - 1] < mult * _BASE_RATE + _DELTA_FEE
            assert original[i - 1] > mult * _BASE_RATE - _DELTA_FEE
    original += [estimator.estimate_fee(i) for i in range(10, 49)]

    # 50 blocks with no transaction leave the estimates where they were
    while blocknum < 250:
        blocknum += 1
        estimator.process_block([], blocknum)
    assert estimator.estimate_fee(1) == 0
    for i in range(2, 10):
        assert estimator.estimate_fee(i) < original[i - 1] + _DELTA_FEE
        assert estimator.estimate_fee(i) > original[i - 1] - _DELTA_FEE

    # 15 blocks of transactions none of which is mined raise them
    while blocknum < 265:
        for j, added in enumerate(_fill(estimator, blocknum)):
            held[j] += added
        blocknum += 1
        estimator.process_block([], blocknum)
    for i in range(1, 10):
        fee = estimator.estimate_fee(i)
        assert fee == 0 or fee > original[i - 1] - _DELTA_FEE

    # mining them all does not bring them below the original
    block = [tx for txs in held for tx in reversed(txs)]
    estimator.process_block(block, 266)
    assert estimator.estimate_fee(1) == 0
    for i in range(2, 10):
        fee = estimator.estimate_fee(i)
        assert fee == 0 or fee > original[i - 1] - _DELTA_FEE

    # 400 blocks in which everything is mined bring them below it
    while blocknum < 665:
        block = [tx for txs in _fill(estimator, blocknum) for tx in txs]
        blocknum += 1
        estimator.process_block(block, blocknum)
    assert estimator.estimate_fee(1) == 0
    for i in range(2, 9):
        assert estimator.estimate_fee(i) < original[i - 1] - _DELTA_FEE


def test_a_fresh_file_is_byte_for_byte_core_s(tmp_path: Path) -> None:
    """What a fresh estimator writes is what a fresh bitcoind writes."""
    estimator, _ = an_estimator(tmp_path)
    data = estimator.write()
    assert len(data) == 309269
    assert hashlib.sha256(data).hexdigest() == _CORE_EMPTY_FILE_SHA256


def _busy(tmp_path: Path) -> FeeEstimator:
    """Return an estimator that has recorded twenty blocks of transactions."""
    estimator, _ = an_estimator(tmp_path)
    for blocknum in range(20):
        block = [tx for txs in _fill(estimator, blocknum) for tx in txs[: j_cut(txs)]]
        estimator.process_block(block, blocknum + 1)
    return estimator


def j_cut(txs: list[RemovedTx]) -> int:
    """Mine the higher fees more often, so that some buckets fail."""
    return 4 if txs[0].fee > 5 * _BASE_FEE else 1


def test_a_file_read_back_is_written_alike(tmp_path: Path) -> None:
    """A file read and written again is the same bytes, the estimates alike.

    The file holds no count of what is still in the mempool, so the
    estimates agree once those are counted out, as at a shutdown.
    """
    estimator = _busy(tmp_path)
    estimator.flush_unconfirmed()
    data = estimator.write()
    reader, _ = an_estimator(tmp_path)
    assert reader.read(data)
    assert reader.write() == data
    for target in range(1, 20):
        for conservative in (False, True):
            assert reader.estimate_smart_fee(
                target, conservative=conservative
            ) == estimator.estimate_smart_fee(target, conservative=conservative)


def test_a_file_is_read_at_start(tmp_path: Path) -> None:
    """The estimates written at a flush are those read by the next estimator."""
    estimator = _busy(tmp_path)
    estimator.flush_estimates()
    written = estimator.path.read_bytes()
    restarted, _ = an_estimator(tmp_path)
    assert restarted.write() == written
    assert restarted.best_seen_height == 20


def test_a_stale_file_is_not_read_unless_asked(tmp_path: Path) -> None:
    """Past 60 hours old the file is ignored, as Core's, but for the option."""
    _busy(tmp_path).flush_estimates()
    path = tmp_path / "fee_estimates.dat"
    stale = time.time() - 61 * 3600
    os.utime(path, (stale, stale))
    ignored, lines = an_estimator(tmp_path)
    assert ignored.best_seen_height == 0
    assert any("too old (age=61 > 60 hours)" in line for line in lines.messages)
    logger = Logger(debug=True)
    read = FeeEstimator(path, logger, read_stale=True)
    assert read.best_seen_height == 20
    sixty = time.time() - 60.5 * 3600
    os.utime(path, (sixty, sixty))
    assert an_estimator(tmp_path)[0].best_seen_height == 20


def test_a_missing_file_is_said_so(tmp_path: Path) -> None:
    """No file leaves the estimates empty and says it was not found."""
    _, lines = an_estimator(tmp_path)
    assert any("is not found. Continue anyway." in line for line in lines.messages)


def test_an_unreadable_file_leaves_the_estimates_empty(tmp_path: Path) -> None:
    """A file that does not parse is warned about, and nothing is read."""
    (tmp_path / "fee_estimates.dat").write_bytes(b"\x01")
    estimator, lines = an_estimator(tmp_path)
    assert estimator.best_seen_height == 0
    assert any(
        "Unable to read policy estimator data (non-fatal): AutoFile::read: end of file"
        in line
        for line in lines.messages
    )
    assert any("Failed to read fee estimates from" in line for line in lines.messages)


def _header(version: int, best: int, first: int, last: int) -> bytes:
    return struct.pack("<iIII", version, best, first, last)


def _stats(
    decay: float = 0.5,
    scale: int = 1,
    buckets: int = 2,
    periods: int = 1,
    *,
    feerates: int | None = None,
    counts: int | None = None,
    fail_periods: int | None = None,
    fail_buckets: int | None = None,
    conf_buckets: int | None = None,
) -> bytes:
    def doubles(count: int) -> bytes:
        return var_int.serialize(count) + struct.pack("<d", 0.0) * count

    def rows(count: int, width: int) -> bytes:
        return var_int.serialize(count) + doubles(width) * count

    return b"".join(
        [
            struct.pack("<dI", decay, scale),
            doubles(buckets if feerates is None else feerates),
            doubles(buckets if counts is None else counts),
            rows(periods, buckets if conf_buckets is None else conf_buckets),
            rows(
                periods if fail_periods is None else fail_periods,
                buckets if fail_buckets is None else fail_buckets,
            ),
        ]
    )


def _file(stats: bytes, buckets: int = 2) -> bytes:
    bounds = var_int.serialize(buckets) + struct.pack("<d", 1.0) * buckets
    return _header(309900, 5, 1, 2) + bounds + stats * 3


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (_header(309901, 0, 0, 0), "File version (309901) too high to be read."),
        (
            _header(309900, 5, 3, 2),
            "Corrupt estimates file. Historical block range for estimates is invalid",
        ),
        (
            _header(309900, 5, 1, 6),
            "Corrupt estimates file. Historical block range for estimates is invalid",
        ),
        (
            _file(_stats(), buckets=1),
            "Corrupt estimates file. Must have between 2 and 1000 feerate buckets",
        ),
        (
            _header(309900, 5, 1, 2)
            + var_int.serialize(1001)
            + struct.pack("<d", 1.0) * 1001,
            "Corrupt estimates file. Must have between 2 and 1000 feerate buckets",
        ),
        (
            _file(_stats(decay=1.0)),
            "Corrupt estimates file. Decay must be between 0 and 1 (non-inclusive)",
        ),
        (
            _file(_stats(decay=0.0)),
            "Corrupt estimates file. Decay must be between 0 and 1 (non-inclusive)",
        ),
        (_file(_stats(scale=0)), "Corrupt estimates file. Scale must be non-zero"),
        (
            _file(_stats(feerates=3)),
            "Corrupt estimates file. Mismatch in feerate average bucket count",
        ),
        (
            _file(_stats(counts=3)),
            "Corrupt estimates file. Mismatch in tx count bucket count",
        ),
        (
            _file(_stats(periods=0)),
            (
                "Corrupt estimates file.  Must maintain estimates for between 1 "
                "and 1008 (one week) confirms"
            ),
        ),
        (
            _file(_stats(scale=1009)),
            (
                "Corrupt estimates file.  Must maintain estimates for between 1 "
                "and 1008 (one week) confirms"
            ),
        ),
        (
            _file(_stats(conf_buckets=3)),
            "Corrupt estimates file. Mismatch in feerate conf average bucket count",
        ),
        (
            _file(_stats(fail_periods=2)),
            "Corrupt estimates file. Mismatch in confirms tracked for failures",
        ),
        (
            _file(_stats(fail_buckets=3)),
            "Corrupt estimates file. Mismatch in one of failure average bucket counts",
        ),
        (
            _header(309900, 5, 1, 2) + b"\xfd\x01\x00",
            "non-canonical var_int: 1 encoded in 3 bytes",
        ),
    ],
)
def test_a_corrupt_file_is_refused_in_core_s_words(
    tmp_path: Path, data: bytes, reason: str
) -> None:
    """Each of Core's checks refuses, and the estimates stay as they were."""
    estimator, lines = an_estimator(tmp_path)
    before = estimator.write()
    assert not estimator.read(data)
    assert estimator.write() == before
    assert (
        f"Unable to read policy estimator data (non-fatal): {reason}" in lines.messages
    )


def test_a_well_formed_small_file_is_read(tmp_path: Path) -> None:
    """A file of other buckets and horizons replaces the default ones."""
    estimator, _ = an_estimator(tmp_path)
    assert estimator.read(_file(_stats()))
    assert estimator.best_seen_height == 5
    assert estimator.highest_target_tracked(FeeEstimateHorizon.LONG) == 1


def test_an_older_version_is_warned_about_and_ignored(tmp_path: Path) -> None:
    """Core's "Incompatible old fee estimation data": nothing is read."""
    estimator, lines = an_estimator(tmp_path)
    before = estimator.write()
    assert estimator.read(_header(309899, 7, 0, 0))
    assert estimator.write() == before
    assert (
        "Incompatible old fee estimation data (non-fatal). Version: 309899"
        in lines.messages
    )


def test_a_nan_is_written_and_read_as_core_s_one_nan(tmp_path: Path) -> None:
    """`EncodeDouble` writes one NaN, and `DecodeDouble` reads any as NaN."""
    estimator, _ = an_estimator(tmp_path)
    estimator.short_stats.tx_ct_avg[0] = math.nan
    data = estimator.write()
    reader, _ = an_estimator(tmp_path)
    assert reader.read(data)
    assert math.isnan(reader.short_stats.tx_ct_avg[0])
    assert reader.write() == data
    assert struct.pack("<Q", 0x7FF8000000000000) in data


def test_a_write_that_fails_is_warned_about(tmp_path: Path) -> None:
    """A path that cannot be written is Core's warning, and nothing else."""
    estimator, lines = an_estimator(tmp_path)
    estimator.path = tmp_path / "missing" / "fee_estimates.dat"
    estimator.flush_estimates()
    assert any("Failed to write fee estimates to" in line for line in lines.messages)


def test_the_estimates_are_written_every_hour(tmp_path: Path) -> None:
    """`flush_if_due` writes once an hour has passed, and not before."""
    estimator, _ = an_estimator(tmp_path)
    estimator.flush_if_due()
    assert not estimator.path.exists()
    estimator._next_flush = time.monotonic() - 1
    estimator.flush_if_due()
    assert estimator.path.exists()
    assert estimator._next_flush > time.monotonic() + 3500


def test_a_flush_counts_what_is_still_tracked_as_unconfirmed(tmp_path: Path) -> None:
    """Core's `Flush`: each tracked transaction counts as left unconfirmed."""
    estimator, _ = an_estimator(tmp_path)
    estimator.process_block([], 1)
    _add(estimator, b"\x01" * 32, 10_000, 1)
    for blocknum in range(2, 5):
        estimator.process_block([], blocknum)
    estimator.flush()
    assert not estimator.mempool_txs
    assert sum(estimator.short_stats.fail_avg[0]) == 1
    assert estimator.path.exists()


def test_only_what_core_tracks_is_tracked(tmp_path: Path) -> None:
    """A transaction at another height, or failing a flag, is not tracked."""
    estimator, lines = an_estimator(tmp_path)
    estimator.process_block([], 1)
    for flag in (
        "limit_bypassed",
        "in_package",
        "chain_current",
        "has_no_mempool_parents",
    ):
        flags = {
            "limit_bypassed": False,
            "in_package": False,
            "chain_current": True,
            "has_no_mempool_parents": True,
        }
        flags[flag] = not flags[flag]
        estimator.process_transaction(flag.encode(), 1000, 100, 1, **flags)
    assert estimator.untracked_txs == 4
    estimator.process_transaction(
        b"other height",
        1000,
        100,
        0,
        limit_bypassed=False,
        in_package=False,
        chain_current=True,
        has_no_mempool_parents=True,
    )
    assert estimator.untracked_txs == 4
    assert not estimator.mempool_txs
    _add(estimator, b"tracked", 1000, 1)
    _add(estimator, b"tracked", 1000, 1)
    assert estimator.tracked_txs == 1
    assert any("already being tracked" in line for line in lines.messages)


def test_a_block_at_or_below_the_best_seen_is_ignored(tmp_path: Path) -> None:
    """Core ignores a side chain, a reorg, a block confirming nothing new."""
    estimator, _ = an_estimator(tmp_path)
    estimator.process_block([], 2)
    tx = _add(estimator, b"\x02" * 32, 5000, 2)
    estimator.process_block([tx], 2)
    assert estimator.mempool_txs
    # an entry height past the block is a removal and no record
    late = RemovedTx(tx.txid, tx.fee, tx.vsize, 9)
    estimator.process_block([late], 3)
    assert not estimator.mempool_txs
    assert estimator.first_recorded_height == 0


def test_a_removal_counts_a_failure_past_the_scale(tmp_path: Path) -> None:
    """Left unconfirmed after a period, a transaction is a failure there."""
    estimator, _ = an_estimator(tmp_path)
    assert not estimator.remove_tx(b"never tracked")
    estimator.process_block([], 1)
    _add(estimator, b"a" * 32, 10_000, 1)
    estimator.process_block([], 2)
    estimator.process_block([], 3)
    assert estimator.remove_tx(b"a" * 32)
    # two blocks ago: a failure at the short horizon's first two periods,
    # at the medium's first, none at the long's
    assert sum(estimator.short_stats.fail_avg[1]) == 1
    assert sum(estimator.fee_stats.fail_avg[0]) == 1
    assert sum(estimator.long_stats.fail_avg[0]) == 0


def test_a_removal_long_after_entry_takes_from_the_old_counts(tmp_path: Path) -> None:
    """Past the circular buffer, the count kept for old entries goes down."""
    estimator, _ = an_estimator(tmp_path)
    estimator.process_block([], 1)
    _add(estimator, b"b" * 32, 10_000, 1)
    for blocknum in range(2, 20):
        estimator.process_block([], blocknum)
    short = estimator.short_stats
    bucket = estimator.mempool_txs[b"b" * 32][1]
    assert short.old_unconf_txs[bucket] == 1
    short.old_unconf_txs[bucket] = 0
    assert estimator.remove_tx(b"b" * 32)
    assert short.old_unconf_txs[bucket] == 0


def test_a_removal_already_counted_out_leaves_the_counts(tmp_path: Path) -> None:
    """Core logs a removal it finds no count for and changes nothing."""
    estimator, _ = an_estimator(tmp_path)
    estimator.process_block([], 1)
    _add(estimator, b"c" * 32, 10_000, 1)
    height, bucket = estimator.mempool_txs[b"c" * 32]
    estimator.fee_stats.unconf_txs[height % 48][bucket] = 0
    assert estimator.remove_tx(b"c" * 32)
    assert estimator.fee_stats.unconf_txs[height % 48][bucket] == 0


def test_a_removal_before_any_block_or_from_the_future(tmp_path: Path) -> None:
    """At height 0 nothing is ago; an entry above the best seen is skipped."""
    estimator, _ = an_estimator(tmp_path)
    _add(estimator, b"d" * 32, 10_000, 0)
    assert estimator.remove_tx(b"d" * 32)
    assert not any(any(row) for row in estimator.short_stats.fail_avg)
    estimator.process_block([], 1)
    _add(estimator, b"e" * 32, 10_000, 1)
    estimator.mempool_txs[b"e" * 32] = (5, 0)
    assert estimator.remove_tx(b"e" * 32)


def test_raw_estimates_refuse_what_core_refuses(tmp_path: Path) -> None:
    """A target outside the horizon, or a threshold above one, is 0."""
    estimator = _busy(tmp_path)
    short = FeeEstimateHorizon.SHORT
    assert estimator.estimate_raw_fee(0, 0.5, short) == 0
    assert estimator.estimate_raw_fee(13, 0.5, short) == 0
    assert estimator.estimate_raw_fee(2, 1.5, short) == 0
    result = EstimationResult()
    assert estimator.estimate_raw_fee(2, 0.5, short, result) > 0
    assert result.decay == 0.962
    assert result.scale == 1
    assert result.pass_bucket.end > result.pass_bucket.start


def test_smart_estimates_clamp_the_target(tmp_path: Path) -> None:
    """Out of range is 0 at the target asked; 1 is 2; past the data is less."""
    estimator = _busy(tmp_path)
    assert estimator.estimate_smart_fee(0, conservative=False) == (0, 0)
    assert estimator.estimate_smart_fee(1009, conservative=False) == (0, 1009)
    fee, target = estimator.estimate_smart_fee(1, conservative=False)
    assert target == 2
    assert fee > 0
    assert estimator.estimate_smart_fee(1008, conservative=True)[1] == 9
    empty, _ = an_estimator(tmp_path / "missing")
    assert empty.estimate_smart_fee(6, conservative=False) == (0, 0)


def test_the_longer_horizons_answer_a_far_target(tmp_path: Path) -> None:
    """A target past the short and medium horizons is read from the long one."""
    estimator, _ = an_estimator(tmp_path)
    for blocknum in range(400):
        block = [tx for txs in _fill(estimator, blocknum) for tx in txs]
        estimator.process_block(block, blocknum + 1)
    economical = estimator.estimate_smart_fee(100, conservative=False)
    conservative = estimator.estimate_smart_fee(100, conservative=True)
    assert economical.returned_target == conservative.returned_target == 100
    assert 0 < economical.fee_per_k <= conservative.fee_per_k
    assert estimator.estimate_smart_fee(20, conservative=True).fee_per_k > 0


def test_a_history_older_than_the_file_s_span_is_not_used(tmp_path: Path) -> None:
    """The historical span counts only where it is recent enough."""
    estimator, _ = an_estimator(tmp_path)
    estimator.historical_first, estimator.historical_best = 10, 30
    estimator.best_seen_height = 40
    assert estimator.estimate_smart_fee(6, conservative=False).returned_target == 6
    estimator.best_seen_height = 30 + 6 * 1008 + 1
    assert estimator.estimate_smart_fee(6, conservative=False).returned_target == 0


@pytest.mark.parametrize(
    ("value", "rounded"),
    [(0.5, 1), (1.5, 2), (2.5, 3), (-0.5, -1), (0.49999999999999994, 0), (7.0, 7)],
)
def test_llround_rounds_halves_away_from_zero(value: float, rounded: int) -> None:
    """C's `llround`, which Python's `round` is not."""
    assert llround(value) == rounded


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        (6.0, 3.0, 2.0),
        (1.0, 0.0, math.inf),
        (-1.0, 0.0, -math.inf),
        (1.0, -0.0, -math.inf),
    ],
)
def test_a_division_by_zero_is_ieee_s(
    numerator: float, denominator: float, expected: float
) -> None:
    """A zero denominator answers an infinity, as C++ doubles do."""
    assert _divide(numerator, denominator) == expected


@pytest.mark.parametrize("numerator", [0.0, math.nan])
def test_zero_or_nan_over_zero_is_nan(numerator: float) -> None:
    """0/0 and NaN/0 are NaN."""
    assert math.isnan(_divide(numerator, 0.0))


def test_a_confirmation_in_no_block_is_not_recorded(tmp_path: Path) -> None:
    """Core's `Record` returns for under one block, which no caller asks."""
    estimator, _ = an_estimator(tmp_path)
    before = estimator.write()
    estimator.short_stats.record(0, 1000.0)
    assert estimator.write() == before


def test_a_block_confirming_untracked_transactions_records_none(
    tmp_path: Path,
) -> None:
    """What the estimator never tracked is not counted when a block holds it."""
    estimator, _ = an_estimator(tmp_path)
    estimator.process_block([RemovedTx(b"untracked", 1000, 100, 0)], 1)
    assert estimator.first_recorded_height == 0


def test_twice_a_target_past_the_long_horizon_is_no_estimate(tmp_path: Path) -> None:
    """Above 504, the 95% estimate at twice the target has nothing to read."""
    estimator = _busy(tmp_path)
    estimator.historical_first, estimator.historical_best = 1, 2000
    estimator.best_seen_height = 2000
    assert estimator.estimate_smart_fee(600, conservative=False).returned_target == 600


def test_a_threshold_of_one_is_met_by_every_confirmation_in_time(
    tmp_path: Path,
) -> None:
    """A range passes at exactly the threshold: Core fails it only below."""
    estimator, _ = an_estimator(tmp_path)
    for blocknum in range(20):
        block = [tx for txs in _fill(estimator, blocknum) for tx in txs]
        estimator.process_block(block, blocknum + 1)
    assert estimator.estimate_raw_fee(1, 1.0, FeeEstimateHorizon.SHORT) > 0


def test_a_height_below_the_target_wraps_as_core_s_unsigned_int(
    tmp_path: Path,
) -> None:
    """`(nBlockHeight - confct) % bins` wraps at 2**32 before the modulo.

    At height 0, 2**32 - 256 is a multiple of the long horizon's 1008
    bins, so 256 confirmations back is the slot a transaction entered
    now, and it counts as still in the mempool.
    """
    estimator, _ = an_estimator(tmp_path)
    _add(estimator, b"t" * 32, _BASE_FEE, 0)
    result = EstimationResult()
    estimator.estimate_raw_fee(1, 0.95, FeeEstimateHorizon.LONG, result)
    assert result.fail_bucket.in_mempool == 1


def test_a_last_range_passing_at_the_lowest_bucket_leaves_no_failure(
    tmp_path: Path,
) -> None:
    """The fail range keeps Core's start of -1 when nothing below failed."""
    estimator, _ = an_estimator(tmp_path)
    for blocknum in range(5):
        block = [
            _add(estimator, (1000 * blocknum + k).to_bytes(32, "little"), 10, blocknum)
            for k in range(40)
        ]
        estimator.process_block(block, blocknum + 1)
    result = EstimationResult()
    assert estimator.estimate_raw_fee(1, 0.95, FeeEstimateHorizon.SHORT, result) > 0
    assert result.pass_bucket.start == 0.0
    assert result.fail_bucket.start == -1.0


def test_a_conservative_double_target_past_the_short_horizon_is_the_long_s(
    tmp_path: Path,
) -> None:
    """Core reads the medium horizon only up to the short one's 12 blocks."""
    estimator, _ = an_estimator(tmp_path)
    for blocknum in range(2):
        block = [tx for txs in _fill(estimator, blocknum) for tx in txs]
        estimator.process_block(block, blocknum + 1)
    medium = estimator.fee_stats.estimate_median_val(
        13, 0.1, 0.95, estimator.best_seen_height
    )
    assert medium > 0
    assert estimator._estimate_conservative_fee(13) == -1


_NOW = 1_800_000_000


def a_node(
    tmp_path: Path,
    *,
    tip_age: int = 0,
    headers_ahead: int = 0,
    ibd: bool = False,
) -> Any:
    """Return a node with a three-block chain and an empty mempool.

    The tip is `tip_age` seconds old at `_NOW`, and the best header is
    `headers_ahead` blocks past it.
    """
    chain = [bytes([i]) * 32 for i in range(3)]
    tip_time = datetime.fromtimestamp(_NOW - tip_age, UTC)
    return SimpleNamespace(
        is_initial_block_download=ibd,
        chainstate=SimpleNamespace(
            block_index=SimpleNamespace(
                active_chain=chain,
                header_dict={
                    chain[-1]: SimpleNamespace(header=SimpleNamespace(time=tip_time))
                },
                header_index=chain + [b"h" * 32] * headers_ahead,
            )
        ),
        mempool=Mempool(Logger(debug=True)),
        fee_estimator=an_estimator(tmp_path)[0],
    )


@pytest.mark.parametrize(
    ("node_args", "current"),
    [
        ({}, True),
        ({"ibd": True}, False),
        ({"tip_age": 3 * 60 * 60}, True),
        ({"tip_age": 3 * 60 * 60 + 1}, False),
        ({"headers_ahead": 1}, True),
        ({"headers_ahead": 2}, False),
    ],
)
def test_a_chain_is_current_as_core_s_is(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    node_args: dict[str, Any],
    current: bool,  # noqa: FBT001
) -> None:
    """Out of initial download, a tip at most 3 hours old, one header ahead."""
    monkeypatch.setattr(time, "time", lambda: _NOW + 0.9)
    node = a_node(tmp_path, **node_args)
    assert is_current_for_fee_estimation(node) is current


def test_an_accepted_transaction_is_tracked_with_its_mempool_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its fee, size and height are the mempool's; a child is not tracked."""
    monkeypatch.setattr(time, "time", lambda: _NOW)
    node = a_node(tmp_path)
    parent = a_transaction_spending(b"\x01" * 32)
    child = a_transaction_spending(parent.id)
    node.mempool.add_tx(parent, 2_000)
    node.mempool.add_tx(child, 4_000)
    track_accepted(node, parent)
    track_accepted(node, child)
    control, _ = an_estimator(tmp_path / "control")
    control.process_transaction(
        parent.id,
        2_000,
        parent.vsize,
        0,
        limit_bypassed=False,
        in_package=False,
        chain_current=True,
        has_no_mempool_parents=True,
    )
    assert node.fee_estimator.mempool_txs == control.mempool_txs
    for stats, expected in zip(
        node.fee_estimator._all_stats(), control._all_stats(), strict=True
    ):
        assert stats.unconf_txs == expected.unconf_txs
    assert (node.fee_estimator.tracked_txs, node.fee_estimator.untracked_txs) == (1, 1)


@pytest.mark.parametrize(
    ("flags", "node_args"),
    [
        ({"limit_bypassed": True}, {}),
        ({"in_package": True}, {}),
        ({}, {"ibd": True}),
    ],
)
def test_what_core_skips_is_counted_untracked(
    tmp_path: Path, flags: dict[str, bool], node_args: dict[str, Any]
) -> None:
    """A reorg's re-add, a package member, or a stale chain."""
    node = a_node(tmp_path, **node_args)
    tx = a_transaction_spending(b"\x01" * 32)
    node.mempool.add_tx(tx, 2_000)
    track_accepted(node, tx, **flags)
    assert node.fee_estimator.mempool_txs == {}
    assert node.fee_estimator.untracked_txs == 1


def test_a_transaction_the_mempool_did_not_keep_is_not_seen(tmp_path: Path) -> None:
    """Core signals only what entered: the estimator counts nothing."""
    node = a_node(tmp_path)
    track_accepted(node, a_transaction_spending(b"\x01" * 32))
    assert (node.fee_estimator.tracked_txs, node.fee_estimator.untracked_txs) == (0, 0)
