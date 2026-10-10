# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`FeeEstimator`, Core's `CBlockPolicyEstimator`.

A port of `src/policy/fees/block_policy_estimator.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag. A transaction entering the
mempool is put in a feerate bucket; each connected block records how
many blocks the transactions it confirms took, in three moving averages
of different half-lives; `estimate_smart_fee` and `estimate_raw_fee`
read them back. `write` and `read` are Core's `fee_estimates.dat`, byte
for byte.

Every caller runs on `Node`'s thread, so this carries no lock where Core
has `m_cs_fee_estimator`. Core's estimator also learns of each event
through the validation interface's queue, which `estimatesmartfee`
drains first; here every event is a direct call, already done.

The arithmetic is Core's: the same IEEE doubles, operations in the same
order, and the `unsigned int` wrap where Core subtracts heights.
"""

import math
import struct
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from enum import Enum
from io import BytesIO
from typing import TYPE_CHECKING, NamedTuple

from btclib import var_int

from btclib_node.chainstate.block_index import block_time

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from btclib.tx import Tx

    from btclib_node import Node
    from btclib_node.log import Logger

__all__ = [
    "FEE_FLUSH_INTERVAL",
    "MAX_FILE_AGE_HOURS",
    "EstimationResult",
    "EstimatorBucket",
    "FeeEstimateHorizon",
    "FeeEstimator",
    "RemovedTx",
    "SmartFee",
    "is_current_for_fee_estimation",
    "llround",
    "track_accepted",
]

# Seconds between two writes of the file, and the hours past which it is
# not read: Core's `FEE_FLUSH_INTERVAL` and `MAX_FILE_AGE`.
FEE_FLUSH_INTERVAL = 60 * 60
MAX_FILE_AGE_HOURS = 60

# The version written, and the highest read: `CURRENT_FEES_FILE_VERSION`.
_FILE_VERSION = 309900

_INF_FEERATE = 1e99

# Track confirm delays up to 12, 48 and 1008 blocks.
_SHORT_BLOCK_PERIODS, _SHORT_SCALE, _SHORT_DECAY = 12, 1, 0.962
_MED_BLOCK_PERIODS, _MED_SCALE, _MED_DECAY = 24, 2, 0.9952
_LONG_BLOCK_PERIODS, _LONG_SCALE, _LONG_DECAY = 42, 24, 0.99931

# Historical estimates older than this many blocks are not valid.
_OLDEST_ESTIMATE_HISTORY = 6 * 1008

# More than 60% confirmed within half the target, 85% within it, 95%
# within twice it.
_HALF_SUCCESS_PCT = 0.6
_SUCCESS_PCT = 0.85
_DOUBLE_SUCCESS_PCT = 0.95

# Average transactions per block a bucket range needs to be tested.
_SUFFICIENT_FEETXS = 0.1
_SUFFICIENT_TXS_SHORT = 0.5

# Bucket bounds in sat/kvB, spaced exponentially.
_MIN_BUCKET_FEERATE = 100.0
_MAX_BUCKET_FEERATE = 1e7
_FEE_SPACING = 1.05

_U32 = 0xFFFFFFFF

# a double's exponent and mantissa bits, and Core's one NaN
_EXPONENT, _MANTISSA, _NAN = 0x7FF0000000000000, 0xFFFFFFFFFFFFF, 0x7FF8000000000000

# A tip older than this many seconds is not current: Core's
# `MAX_FEE_ESTIMATION_TIP_AGE` (`src/validation.cpp`).
_MAX_FEE_ESTIMATION_TIP_AGE = 3 * 60 * 60


class FeeEstimateHorizon(Enum):
    """The three time horizons, named as `estimaterawfee` names them."""

    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class RemovedTx(NamedTuple):
    """A transaction a block took out of the mempool.

    Core's `RemovedMempoolTransactionInfo`: the fee without its delta,
    the sigop-adjusted vsize and the tip height when it was accepted.
    """

    txid: bytes
    fee: int
    vsize: int
    height: int


class SmartFee(NamedTuple):
    """`estimate_smart_fee`'s answer: sat/kvB, 0 for none, and the target."""

    fee_per_k: int
    returned_target: int


@dataclass
class EstimatorBucket:
    """Core's `EstimatorBucket`: a range of buckets and its counts."""

    start: float = -1.0
    end: float = -1.0
    within_target: float = 0.0
    total_confirmed: float = 0.0
    in_mempool: float = 0.0
    left_mempool: float = 0.0


@dataclass
class EstimationResult:
    """Core's `EstimationResult`: the passing and failing ranges."""

    pass_bucket: EstimatorBucket = field(default_factory=EstimatorBucket)
    fail_bucket: EstimatorBucket = field(default_factory=EstimatorBucket)
    decay: float = 0.0
    scale: int = 0


def _fee_per_k(fee: int, vsize: int) -> int:
    """Return `CFeeRate(fee, vsize).GetFeePerK()`, rounded down."""
    return fee * 1000 // vsize if vsize > 0 else 0


def llround(value: float) -> int:
    """Return C's `llround`: the nearest integer, halves away from zero."""
    magnitude = abs(value)
    whole = math.floor(magnitude)
    # exact for a double: what is left of it below its own floor
    if magnitude - whole >= 0.5:  # noqa: PLR2004
        whole += 1
    return int(math.copysign(whole, value))


def _divide(numerator: float, denominator: float) -> float:
    """Divide as IEEE doubles do, a zero denominator included."""
    if denominator:
        return numerator / denominator
    if numerator == 0 or math.isnan(numerator):
        return math.nan
    return math.copysign(math.inf, numerator) * math.copysign(1.0, denominator)


class _Buckets:
    """The bucket bounds, and Core's `bucketMap` over them."""

    def __init__(self, bounds: list[float]) -> None:
        self.bounds = bounds
        # a later bound equal to an earlier one wins, as in a `std::map`
        index = {bound: i for i, bound in enumerate(bounds)}
        self._keys = sorted(index)
        self._index = [index[key] for key in self._keys]

    def __len__(self) -> int:
        return len(self.bounds)

    def find(self, feerate: float) -> int:
        """Return the bucket of `feerate`: `bucketMap.lower_bound`."""
        # Core dereferences `end()` past the last bound, which the
        # infinite bucket keeps any feerate from reaching
        position = min(bisect_left(self._keys, feerate), len(self._keys) - 1)
        return self._index[position]


def _default_buckets() -> _Buckets:
    bounds = []
    bound = _MIN_BUCKET_FEERATE
    while bound <= _MAX_BUCKET_FEERATE:
        bounds.append(bound)
        bound *= _FEE_SPACING
    bounds.append(_INF_FEERATE)
    return _Buckets(bounds)


class _Reader:
    """Core's `AutoFile` deserialization, over the bytes of the file."""

    def __init__(self, data: bytes) -> None:
        self._stream = BytesIO(data)

    def read(self, size: int) -> bytes:
        data = self._stream.read(size)
        if len(data) != size:
            msg = "AutoFile::read: end of file"
            raise ValueError(msg)
        return data

    def u32(self) -> int:
        return int.from_bytes(self.read(4), "little")

    def i32(self) -> int:
        return int.from_bytes(self.read(4), "little", signed=True)

    def double(self) -> float:
        bits = int.from_bytes(self.read(8), "little")
        if bits & _EXPONENT == _EXPONENT and bits & _MANTISSA:
            return math.nan
        (value,) = struct.unpack("<d", struct.pack("<Q", bits))
        return float(value)

    def doubles(self) -> list[float]:
        return [self.double() for _ in range(self.size())]

    def size(self) -> int:
        try:
            return var_int.parse(self._stream)
        except Exception as error:
            raise ValueError(str(error)) from error


def _double(value: float) -> bytes:
    """Return Core's `EncodeDouble`: IEEE 754, one NaN for every NaN."""
    if math.isnan(value):
        return _NAN.to_bytes(8, "little")
    return struct.pack("<d", value)


def _doubles(values: list[float]) -> bytes:
    return var_int.serialize(len(values)) + b"".join(_double(v) for v in values)


class _TxConfirmStats:
    """Core's `TxConfirmStats`: one horizon's moving averages.

    `conf_avg[Y][X]` counts the transactions of bucket X confirmed within
    Y + 1 periods, `fail_avg[Y][X]` those that left the mempool after,
    unconfirmed. `unconf_txs` is a circular buffer of the transactions
    still in the mempool, by the height they entered it.
    """

    def __init__(
        self, buckets: _Buckets, max_periods: int, decay: float, scale: int
    ) -> None:
        count = len(buckets)
        self.buckets = buckets
        self.decay = decay
        self.scale = scale
        self.conf_avg = [[0.0] * count for _ in range(max_periods)]
        self.fail_avg = [[0.0] * count for _ in range(max_periods)]
        self.tx_ct_avg = [0.0] * count
        self.feerate_avg = [0.0] * count
        self._resize_in_memory_counters(count)

    def _resize_in_memory_counters(self, count: int) -> None:
        self.unconf_txs = [[0] * count for _ in range(self.max_confirms)]
        self.old_unconf_txs = [0] * count

    @property
    def max_confirms(self) -> int:
        """Return `GetMaxConfirms`: the most blocks this horizon tracks."""
        return self.scale * len(self.conf_avg)

    def clear_current(self, height: int) -> None:
        """Roll the circular buffer of unconfirmed transactions."""
        current = self.unconf_txs[height % len(self.unconf_txs)]
        for j in range(len(self.buckets)):
            self.old_unconf_txs[j] += current[j]
            current[j] = 0

    def record(self, blocks_to_confirm: int, feerate: float) -> None:
        """Record a transaction confirmed after `blocks_to_confirm` blocks."""
        if blocks_to_confirm < 1:
            return
        periods = (blocks_to_confirm + self.scale - 1) // self.scale
        bucket = self.buckets.find(feerate)
        for i in range(periods, len(self.conf_avg) + 1):
            self.conf_avg[i - 1][bucket] += 1
        self.tx_ct_avg[bucket] += 1
        self.feerate_avg[bucket] += feerate

    def update_moving_averages(self) -> None:
        """Decay every moving average by one block."""
        decay = self.decay
        for row in self.conf_avg:
            row[:] = [value * decay for value in row]
        for row in self.fail_avg:
            row[:] = [value * decay for value in row]
        self.feerate_avg = [value * decay for value in self.feerate_avg]
        self.tx_ct_avg = [value * decay for value in self.tx_ct_avg]

    def new_tx(self, height: int, feerate: float) -> int:
        """Count a transaction entering the mempool, and return its bucket."""
        bucket = self.buckets.find(feerate)
        self.unconf_txs[height % len(self.unconf_txs)][bucket] += 1
        return bucket

    def remove_tx(
        self, entry_height: int, best_seen_height: int, bucket: int, *, in_block: bool
    ) -> None:
        """Stop counting a transaction as unconfirmed, failed if not mined."""
        blocks_ago = best_seen_height - entry_height
        if best_seen_height == 0:
            blocks_ago = 0
        if blocks_ago < 0:
            return
        if blocks_ago >= len(self.unconf_txs):
            if self.old_unconf_txs[bucket] > 0:
                self.old_unconf_txs[bucket] -= 1
        else:
            counts = self.unconf_txs[entry_height % len(self.unconf_txs)]
            if counts[bucket] > 0:
                counts[bucket] -= 1
        if not in_block and blocks_ago >= self.scale:
            periods_ago = blocks_ago // self.scale
            for i in range(min(periods_ago, len(self.fail_avg))):
                self.fail_avg[i][bucket] += 1

    def estimate_median_val(  # noqa: C901, PLR0915
        self,
        conf_target: int,
        sufficient_tx_val: float,
        success_break_point: float,
        height: int,
        result: EstimationResult | None = None,
    ) -> float:
        """Return Core's `EstimateMedianVal`, -1 where no range passes.

        From the highest bucket down, buckets are combined until a range
        holds enough transactions, and the range is tested against the
        success threshold. The answer is the average feerate of the
        bucket holding the median transaction of the lowest passing range.
        """
        n_conf = 0.0
        total_num = 0.0
        extra_num = 0
        fail_num = 0.0
        period_target = (conf_target + self.scale - 1) // self.scale
        max_bucket = len(self.buckets) - 1

        cur_near = best_near = cur_far = best_far = max_bucket
        partial_num = 0.0
        found_answer = False
        bins = len(self.unconf_txs)
        new_bucket_range = True
        passing = True
        pass_bucket = EstimatorBucket()
        fail_bucket = EstimatorBucket()
        bounds = self.buckets.bounds

        def range_bucket(near: int, far: int) -> EstimatorBucket:
            low, high = min(near, far), max(near, far)
            return EstimatorBucket(
                start=bounds[low - 1] if low else 0.0,
                end=bounds[high],
                within_target=n_conf,
                total_confirmed=total_num,
                in_mempool=extra_num,
                left_mempool=fail_num,
            )

        for bucket in range(max_bucket, -1, -1):
            if new_bucket_range:
                cur_near = bucket
                new_bucket_range = False
            cur_far = bucket
            n_conf += self.conf_avg[period_target - 1][bucket]
            partial_num += self.tx_ct_avg[bucket]
            total_num += self.tx_ct_avg[bucket]
            fail_num += self.fail_avg[period_target - 1][bucket]
            for confirms in range(conf_target, self.max_confirms):
                # `unsigned int` arithmetic: a height below `confirms` wraps
                extra_num += self.unconf_txs[((height - confirms) & _U32) % bins][
                    bucket
                ]
            extra_num += self.old_unconf_txs[bucket]
            if partial_num < sufficient_tx_val / (1 - self.decay):
                continue
            partial_num = 0.0
            cur_pct = _divide(n_conf, total_num + fail_num + extra_num)
            if cur_pct < success_break_point:
                if passing:
                    fail_bucket = range_bucket(cur_near, cur_far)
                    passing = False
                continue
            fail_bucket = EstimatorBucket()
            found_answer = True
            passing = True
            pass_bucket.within_target = n_conf
            n_conf = 0.0
            pass_bucket.total_confirmed = total_num
            total_num = 0.0
            pass_bucket.in_mempool = extra_num
            pass_bucket.left_mempool = fail_num
            fail_num = 0.0
            extra_num = 0
            best_near, best_far = cur_near, cur_far
            new_bucket_range = True

        median = -1.0
        tx_sum = 0.0
        low, high = min(best_near, best_far), max(best_near, best_far)
        for j in range(low, high + 1):
            tx_sum += self.tx_ct_avg[j]
        if found_answer and tx_sum != 0:
            tx_sum /= 2
            # every part of the range was tested with a positive count, so
            # the median's bucket is reached by `high` at the latest
            j = low
            while self.tx_ct_avg[j] < tx_sum:
                tx_sum -= self.tx_ct_avg[j]
                j += 1
            median = _divide(self.feerate_avg[j], self.tx_ct_avg[j])
            pass_bucket.start = bounds[low - 1] if low else 0.0
            pass_bucket.end = bounds[high]

        if passing and not new_bucket_range:
            fail_bucket = range_bucket(cur_near, cur_far)

        if result is not None:
            result.pass_bucket = pass_bucket
            result.fail_bucket = fail_bucket
            result.decay = self.decay
            result.scale = self.scale
        return median

    def write(self) -> bytes:
        """Serialize as Core's `TxConfirmStats::Write`."""
        return b"".join(
            [
                _double(self.decay),
                struct.pack("<I", self.scale),
                _doubles(self.feerate_avg),
                _doubles(self.tx_ct_avg),
                var_int.serialize(len(self.conf_avg)),
                *(_doubles(row) for row in self.conf_avg),
                var_int.serialize(len(self.fail_avg)),
                *(_doubles(row) for row in self.fail_avg),
            ]
        )

    @classmethod
    def read(cls, reader: _Reader, buckets: _Buckets) -> _TxConfirmStats:
        """Deserialize as Core's `TxConfirmStats::Read`, with its checks."""
        count = len(buckets)
        decay = reader.double()
        if not 0 < decay < 1:
            msg = (
                "Corrupt estimates file. Decay must be between 0 and 1 (non-inclusive)"
            )
            raise ValueError(msg)
        scale = reader.u32()
        if scale == 0:
            msg = "Corrupt estimates file. Scale must be non-zero"
            raise ValueError(msg)
        stats = cls(buckets, 0, decay, scale)
        stats.feerate_avg = reader.doubles()
        if len(stats.feerate_avg) != count:
            msg = "Corrupt estimates file. Mismatch in feerate average bucket count"
            raise ValueError(msg)
        stats.tx_ct_avg = reader.doubles()
        if len(stats.tx_ct_avg) != count:
            msg = "Corrupt estimates file. Mismatch in tx count bucket count"
            raise ValueError(msg)
        stats.conf_avg = [reader.doubles() for _ in range(reader.size())]
        max_periods = len(stats.conf_avg)
        if not 0 < scale * max_periods <= 6 * 24 * 7:
            msg = (
                "Corrupt estimates file.  Must maintain estimates for between 1 "
                "and 1008 (one week) confirms"
            )
            raise ValueError(msg)
        if any(len(row) != count for row in stats.conf_avg):
            msg = (
                "Corrupt estimates file. Mismatch in feerate conf average bucket count"
            )
            raise ValueError(msg)
        stats.fail_avg = [reader.doubles() for _ in range(reader.size())]
        if len(stats.fail_avg) != max_periods:
            msg = "Corrupt estimates file. Mismatch in confirms tracked for failures"
            raise ValueError(msg)
        if any(len(row) != count for row in stats.fail_avg):
            msg = "Corrupt estimates file. Mismatch in one of failure average bucket counts"
            raise ValueError(msg)
        stats._resize_in_memory_counters(count)
        return stats


class FeeEstimator:
    """Core's `CBlockPolicyEstimator`, reading and writing `path`.

    `read_stale` is `-acceptstalefeeestimates`: read `path` however long
    ago it was last written.
    """

    def __init__(self, path: Path, logger: Logger, *, read_stale: bool = False) -> None:
        """Start empty, then read `path` where it is there and fresh enough."""
        self.path = path
        self.logger = logger
        self.best_seen_height = 0
        self.first_recorded_height = 0
        self.historical_first = 0
        self.historical_best = 0
        # txid -> (entry height, bucket): Core's `mapMemPoolTxs`
        self.mempool_txs: dict[bytes, tuple[int, int]] = {}
        self.tracked_txs = 0
        self.untracked_txs = 0
        self._new_stats(_default_buckets())
        self._next_flush = time.monotonic() + FEE_FLUSH_INTERVAL
        try:
            data = path.read_bytes()
            # in whole hours, truncated, as Core's `duration_cast` does
            age = int((time.time() - path.stat().st_mtime) / 3600)
        except OSError:
            logger.info("%s is not found. Continue anyway.", path)
            return
        if age > MAX_FILE_AGE_HOURS and not read_stale:
            logger.warning(
                "Fee estimation file %s too old (age=%d > %d hours) and will not "
                "be used to avoid serving stale estimates.",
                path,
                age,
                MAX_FILE_AGE_HOURS,
            )
            return
        if not self.read(data):
            logger.warning(
                "Failed to read fee estimates from %s. Continue anyway.", path
            )

    def _new_stats(self, buckets: _Buckets) -> None:
        self.buckets = buckets
        self.fee_stats = _TxConfirmStats(
            buckets, _MED_BLOCK_PERIODS, _MED_DECAY, _MED_SCALE
        )
        self.short_stats = _TxConfirmStats(
            buckets, _SHORT_BLOCK_PERIODS, _SHORT_DECAY, _SHORT_SCALE
        )
        self.long_stats = _TxConfirmStats(
            buckets, _LONG_BLOCK_PERIODS, _LONG_DECAY, _LONG_SCALE
        )

    def _all_stats(self) -> tuple[_TxConfirmStats, ...]:
        return self.fee_stats, self.short_stats, self.long_stats

    def _stats(self, horizon: FeeEstimateHorizon) -> _TxConfirmStats:
        return {
            FeeEstimateHorizon.SHORT: self.short_stats,
            FeeEstimateHorizon.MEDIUM: self.fee_stats,
            FeeEstimateHorizon.LONG: self.long_stats,
        }[horizon]

    def process_transaction(  # noqa: PLR0913
        self,
        txid: bytes,
        fee: int,
        vsize: int,
        height: int,
        *,
        limit_bypassed: bool,
        in_package: bool,
        chain_current: bool,
        has_no_mempool_parents: bool,
    ) -> None:
        """Track a transaction the mempool just accepted, Core's way.

        Only one accepted at the height of the last block seen, not
        re-added by a reorg, not in a package, while the chain is
        current, and spending nothing the mempool holds.
        """
        if txid in self.mempool_txs:
            self.logger.log_debug(
                "estimatefee",
                "Blockpolicy error mempool tx %s already being tracked",
                txid.hex(),
            )
            return
        if height != self.best_seen_height:
            return
        if (
            limit_bypassed
            or in_package
            or not chain_current
            or not has_no_mempool_parents
        ):
            self.untracked_txs += 1
            return
        self.tracked_txs += 1
        feerate = float(_fee_per_k(fee, vsize))
        bucket = 0
        for stats in self._all_stats():
            bucket = stats.new_tx(height, feerate)
        self.mempool_txs[txid] = (height, bucket)

    def remove_tx(self, txid: bytes, *, in_block: bool = False) -> bool:
        """Stop tracking `txid`; outside a block it may count as a failure."""
        entry = self.mempool_txs.pop(txid, None)
        if entry is None:
            return False
        height, bucket = entry
        for stats in self._all_stats():
            stats.remove_tx(height, self.best_seen_height, bucket, in_block=in_block)
        return True

    def _process_block_tx(self, height: int, tx: RemovedTx) -> bool:
        if not self.remove_tx(tx.txid, in_block=True):
            return False
        blocks_to_confirm = height - tx.height
        if blocks_to_confirm <= 0:
            return False
        feerate = float(_fee_per_k(tx.fee, tx.vsize))
        for stats in self._all_stats():
            stats.record(blocks_to_confirm, feerate)
        return True

    def process_block(self, removed: Iterable[RemovedTx], height: int) -> None:
        """Record the block at `height`, which took `removed` from the mempool.

        A block at or below the highest seen is ignored, as Core ignores
        side chains and reorgs.
        """
        if height <= self.best_seen_height:
            return
        self.best_seen_height = height
        for stats in self._all_stats():
            stats.clear_current(height)
        for stats in self._all_stats():
            stats.update_moving_averages()
        counted = sum(self._process_block_tx(height, tx) for tx in removed)
        if self.first_recorded_height == 0 and counted > 0:
            self.first_recorded_height = height
        self.logger.log_debug(
            "estimatefee",
            "Blockpolicy estimates updated by %d block txs, since last block %d of "
            "%d tracked, mempool map size %d, max target %d",
            counted,
            self.tracked_txs,
            self.tracked_txs + self.untracked_txs,
            len(self.mempool_txs),
            self._max_usable_estimate(),
        )
        self.tracked_txs = 0
        self.untracked_txs = 0

    def estimate_fee(self, conf_target: int) -> int:
        """Return Core's deprecated `estimateFee`, in sat/kvB, 0 for none."""
        if conf_target <= 1:
            return 0
        return self.estimate_raw_fee(
            conf_target, _DOUBLE_SUCCESS_PCT, FeeEstimateHorizon.MEDIUM
        )

    def estimate_raw_fee(
        self,
        conf_target: int,
        success_threshold: float,
        horizon: FeeEstimateHorizon,
        result: EstimationResult | None = None,
    ) -> int:
        """Return one horizon's estimate at `success_threshold`, 0 for none."""
        stats = self._stats(horizon)
        sufficient = (
            _SUFFICIENT_TXS_SHORT
            if horizon == FeeEstimateHorizon.SHORT
            else _SUFFICIENT_FEETXS
        )
        if conf_target <= 0 or conf_target > stats.max_confirms:
            return 0
        if success_threshold > 1:
            return 0
        median = stats.estimate_median_val(
            conf_target, sufficient, success_threshold, self.best_seen_height, result
        )
        return 0 if median < 0 else llround(median)

    def highest_target_tracked(self, horizon: FeeEstimateHorizon) -> int:
        """Return the highest target `horizon` tracks."""
        return self._stats(horizon).max_confirms

    def _block_span(self) -> int:
        if self.first_recorded_height == 0:
            return 0
        return self.best_seen_height - self.first_recorded_height

    def _historical_block_span(self) -> int:
        if self.historical_first == 0:
            return 0
        if self.best_seen_height - self.historical_best > _OLDEST_ESTIMATE_HISTORY:
            return 0
        return self.historical_best - self.historical_first

    def _max_usable_estimate(self) -> int:
        span = max(self._block_span(), self._historical_block_span())
        return min(self.long_stats.max_confirms, span // 2)

    def _estimate_combined_fee(
        self, conf_target: int, success_threshold: float, *, check_shorter: bool
    ) -> float:
        """Estimate at the shortest horizon tracking `conf_target`.

        With `check_shorter`, a lower estimate at the highest target a
        shorter horizon tracks is taken instead.
        """
        short, medium, long = self.short_stats, self.fee_stats, self.long_stats
        estimate = -1.0
        if not 1 <= conf_target <= long.max_confirms:
            return estimate
        if conf_target <= short.max_confirms:
            stats, sufficient = short, _SUFFICIENT_TXS_SHORT
        elif conf_target <= medium.max_confirms:
            stats, sufficient = medium, _SUFFICIENT_FEETXS
        else:
            stats, sufficient = long, _SUFFICIENT_FEETXS
        height = self.best_seen_height
        estimate = stats.estimate_median_val(
            conf_target, sufficient, success_threshold, height
        )
        if not check_shorter:
            return estimate
        for stats, sufficient in (
            (medium, _SUFFICIENT_FEETXS),
            (short, _SUFFICIENT_TXS_SHORT),
        ):
            if conf_target <= stats.max_confirms:
                continue
            shorter = stats.estimate_median_val(
                stats.max_confirms, sufficient, success_threshold, height
            )
            if shorter > 0 and (estimate == -1 or shorter < estimate):
                estimate = shorter
        return estimate

    def _estimate_conservative_fee(self, double_target: int) -> float:
        """Hold `double_target` to 95% at the longer horizons too."""
        height = self.best_seen_height
        estimate = -1.0
        if double_target <= self.short_stats.max_confirms:
            estimate = self.fee_stats.estimate_median_val(
                double_target, _SUFFICIENT_FEETXS, _DOUBLE_SUCCESS_PCT, height
            )
        if double_target <= self.fee_stats.max_confirms:
            estimate = max(
                estimate,
                self.long_stats.estimate_median_val(
                    double_target, _SUFFICIENT_FEETXS, _DOUBLE_SUCCESS_PCT, height
                ),
            )
        return estimate

    def estimate_smart_fee(self, conf_target: int, *, conservative: bool) -> SmartFee:
        """Return Core's `estimateSmartFee`, in sat/kvB, 0 for none.

        The highest of three estimates: 60% within half the target, 85%
        within it, and 95% within twice it, each at the shortest horizon
        tracking it. A conservative estimate, or one where those three
        found nothing, also holds twice the target to 95% at the longer
        horizons. The target is raised to 2 and lowered to what the data
        seen so far can answer, and the target used is returned.

        Core also returns which estimate decided, for its wallet; no
        caller here reads it.
        """
        if conf_target <= 0 or conf_target > self.long_stats.max_confirms:
            return SmartFee(0, conf_target)
        conf_target = max(conf_target, 2)
        conf_target = min(conf_target, self._max_usable_estimate())
        if conf_target <= 1:
            return SmartFee(0, conf_target)
        median = max(
            self._estimate_combined_fee(
                conf_target // 2, _HALF_SUCCESS_PCT, check_shorter=True
            ),
            self._estimate_combined_fee(conf_target, _SUCCESS_PCT, check_shorter=True),
            self._estimate_combined_fee(
                2 * conf_target, _DOUBLE_SUCCESS_PCT, check_shorter=not conservative
            ),
        )
        if conservative or median == -1:
            median = max(median, self._estimate_conservative_fee(2 * conf_target))
        if median < 0:
            return SmartFee(0, conf_target)
        return SmartFee(llround(median), conf_target)

    def flush_unconfirmed(self) -> None:
        """Count every transaction still tracked as left unconfirmed."""
        for txid in list(self.mempool_txs):
            self.remove_tx(txid)

    def write(self) -> bytes:
        """Serialize as Core's `CBlockPolicyEstimator::Write`."""
        if self._block_span() > self._historical_block_span() // 2:
            first, best = self.first_recorded_height, self.best_seen_height
        else:
            first, best = self.historical_first, self.historical_best
        return b"".join(
            [
                struct.pack("<iIII", _FILE_VERSION, self.best_seen_height, first, best),
                _doubles(self.buckets.bounds),
                *(stats.write() for stats in self._all_stats()),
            ]
        )

    def read(self, data: bytes) -> bool:
        """Replace the estimates with those of `data`, a `write`.

        Nothing changes where `data` does not parse, and a warning says why.
        """
        try:
            self._read(data)
        except ValueError as error:
            self.logger.warning(
                "Unable to read policy estimator data (non-fatal): %s", error
            )
            return False
        return True

    def _read(self, data: bytes) -> None:
        reader = _Reader(data)
        version = reader.i32()
        if version > _FILE_VERSION:
            msg = f"File version ({version}) too high to be read."
            raise ValueError(msg)
        best_seen = reader.u32()
        if version < _FILE_VERSION:
            self.logger.warning(
                "Incompatible old fee estimation data (non-fatal). Version: %d", version
            )
            return
        first, best = reader.u32(), reader.u32()
        if first > best or best > best_seen:
            msg = "Corrupt estimates file. Historical block range for estimates is invalid"
            raise ValueError(msg)
        bounds = reader.doubles()
        if not 1 < len(bounds) <= 1000:  # noqa: PLR2004
            msg = "Corrupt estimates file. Must have between 2 and 1000 feerate buckets"
            raise ValueError(msg)
        buckets = _Buckets(bounds)
        fee_stats = _TxConfirmStats.read(reader, buckets)
        short_stats = _TxConfirmStats.read(reader, buckets)
        long_stats = _TxConfirmStats.read(reader, buckets)
        self.buckets = buckets
        self.fee_stats, self.short_stats, self.long_stats = (
            fee_stats,
            short_stats,
            long_stats,
        )
        self.best_seen_height = best_seen
        self.historical_first = first
        self.historical_best = best

    def flush_estimates(self) -> None:
        """Write the estimates to `path`, Core's `FlushFeeEstimates`."""
        try:
            self.path.write_bytes(self.write())
        except OSError:
            self.logger.warning(
                "Failed to write fee estimates to %s. Continue anyway.", self.path
            )
            return
        self.logger.log_debug("estimatefee", "Flushed fee estimates to %s.", self.path)

    def flush(self) -> None:
        """Drop what is still tracked, then write: Core's `Flush`."""
        self.flush_unconfirmed()
        self.flush_estimates()

    def flush_if_due(self) -> None:
        """Write the estimates if `FEE_FLUSH_INTERVAL` has passed since last."""
        now = time.monotonic()
        if now < self._next_flush:
            return
        self._next_flush = now + FEE_FLUSH_INTERVAL
        self.flush_estimates()


def is_current_for_fee_estimation(node: Node) -> bool:
    """Whether a transaction accepted now may be tracked.

    Core's `IsCurrentForFeeEstimation` (`src/validation.cpp`): out of
    initial block download, with a tip less than three hours old, and at
    most one block behind the best header.
    """
    if node.is_initial_block_download:
        return False
    block_index = node.chainstate.block_index
    tip = block_index.header_dict[block_index.active_chain[-1]].header
    if block_time(tip) < int(time.time()) - _MAX_FEE_ESTIMATION_TIP_AGE:
        return False
    return len(block_index.active_chain) >= len(block_index.header_index) - 1


def track_accepted(
    node: Node, tx: Tx, *, limit_bypassed: bool = False, in_package: bool = False
) -> None:
    """Hand `tx`, just kept by the mempool, to `node.fee_estimator`.

    Core's `TransactionAddedToMempool`, with the flags of its
    `NewMempoolTransactionInfo`: `limit_bypassed` for a transaction a
    reorg put back, `in_package` for one submitted in a package.
    """
    mempool = node.mempool
    wtxid = tx.hash
    if wtxid not in mempool.transactions:
        return
    node.fee_estimator.process_transaction(
        mempool.txids[wtxid],
        mempool.fees[wtxid],
        mempool.vsizes[wtxid],
        mempool.heights[wtxid],
        limit_bypassed=limit_bypassed,
        in_package=in_package,
        chain_current=is_current_for_fee_estimation(node),
        has_no_mempool_parents=not any(
            vin.prev_out.tx_id in mempool.txid_index for vin in tx.vin
        ),
    )
