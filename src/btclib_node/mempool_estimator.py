# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`MempoolEstimator`, Core's `MemPoolFeeRateEstimator`.

A port of `src/policy/fees/mempool_estimator.cpp`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag. The estimate is read from the
block the mempool would make now: the feerate of the chunk that fills
half of it is the conservative estimate, three quarters the economical
one. It is given only once six consecutive blocks are tracked and, where
they weigh a block or more, the mempool held three quarters of their
weight: each connected block records its weight and the weight the
mempool held of it. `write` and `read` are Core's
`mempool_policy_estimator.dat`, byte for byte.

Every caller runs on `Node`'s thread, so this carries no lock where Core
has `cs`.
"""

import itertools
import struct
from fractions import Fraction
from io import BytesIO
from typing import TYPE_CHECKING, NamedTuple

from btclib import var_int
from btclib.consensus import MAX_BLOCK_WEIGHT, WITNESS_SCALE_FACTOR

from btclib_node import clock
from btclib_node.cluster_linearize import FeeFrac
from btclib_node.constants import DIR_MODE
from btclib_node.mining import chunk_feerates

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from btclib.block import Block

    from btclib_node import Node
    from btclib_node.fee_estimator import RemovedTx
    from btclib_node.log import Logger

__all__ = [
    "BLOCK_POLICY",
    "MAX_TARGET",
    "MEMPOOL_POLICY",
    "FeeRateEstimate",
    "MempoolEstimator",
    "MinedBlockStats",
    "fee_per_k",
    "percentiles",
]

# Core's `FeeRateEstimatorTypeToString` of each estimator
BLOCK_POLICY = "block_policy"
MEMPOOL_POLICY = "mempool_policy"

# `MEMPOOL_FEE_ESTIMATOR_MAX_TARGET`: the target of every estimate, since
# the mempool is not expected to predict further
MAX_TARGET = 2

# `CACHE_LIFE`, in seconds: how long an estimate is served again on the
# same tip
_CACHE_LIFE = 7

# `MEMPOOL_HEALTH_WINDOW_BLOCKS` and `MEMPOOL_REPRESENTATION_THRESHOLD`:
# the blocks tracked, and the share of their weight the mempool must
# have held
_WINDOW_BLOCKS = 6
_REPRESENTATION_THRESHOLD = 0.75

# `MIN_REPRESENTATIVE_WINDOW_WEIGHT`, `DEFAULT_BLOCK_MAX_WEIGHT`: below
# this weight across the window the share is not checked
_MIN_WINDOW_WEIGHT = MAX_BLOCK_WEIGHT

# `CURRENT_MEMPOOL_ESTIMATOR_VERSION`
_FILE_VERSION = 1

_STATS = struct.Struct("<QQQ")


class FeeRateEstimate(NamedTuple):
    """Core's `FeeRateEstimation`, or its `FeeRateEstimationError`.

    `feerate` is a fee over a size in vbytes. Where `error` is set the
    estimate failed, and `feerate` is `FeeFrac(0, 0)`.
    """

    estimator: str
    feerate: FeeFrac
    returned_target: int
    error: str = ""


class MinedBlockStats(NamedTuple):
    """Core's `MinedBlockStats`: a block's weight, coinbase left out.

    `removed_weight` is the weight of what the block took from the
    mempool.
    """

    height: int
    removed_weight: int
    block_weight: int


def _ratio(feerate: FeeFrac) -> Fraction:
    return Fraction(feerate.fee, feerate.size)


def percentiles(feerates: Sequence[FeeFrac]) -> tuple[FeeFrac, FeeFrac]:
    """Return the feerates at half and three quarters of a block.

    Core's `CalculateMaxWeightPercentiles` over `feerates`, best first.
    A percentile the chunks do not reach is `FeeFrac(0, 0)`.
    """
    empty = FeeFrac(0, 0)
    p50 = p75 = empty
    accumulated = 0
    for feerate in feerates:
        accumulated += feerate.size * WITNESS_SCALE_FACTOR
        if accumulated >= MAX_BLOCK_WEIGHT // 2 and p50 == empty:
            p50 = feerate
        if accumulated >= MAX_BLOCK_WEIGHT * 3 // 4:
            p75 = feerate
            break
    return p50, p75


def fee_per_k(feerate: FeeFrac) -> int:
    """Return `CFeeRate::GetFeePerK`: sat/kvB, rounded down."""
    return feerate.fee * 1000 // feerate.size


def _read(stream: BytesIO, size: int) -> bytes:
    """Read `size` bytes, or raise `ValueError` at the end of `stream`."""
    data = stream.read(size)
    if len(data) != size:
        msg = "AutoFile::read: end of file"
        raise ValueError(msg)
    return data


def _error(reason: str) -> FeeRateEstimate:
    return FeeRateEstimate(
        MEMPOOL_POLICY, FeeFrac(0, 0), MAX_TARGET, f"{MEMPOOL_POLICY}: {reason}"
    )


class MempoolEstimator:
    """Core's `MemPoolFeeRateEstimator`, reading and writing `path`.

    `tip` is the active chain's height and hash when it starts: the file
    is read only where its last block is that tip.
    """

    def __init__(self, path: Path, logger: Logger, tip: tuple[int, bytes]) -> None:
        """Start with no block, then read `path` where it is there."""
        self.path = path
        self.logger = logger
        # `m_prev_mined_blocks` and `m_mined_blocks_tip_hash`
        self.blocks: list[MinedBlockStats] = []
        self.tip_hash = bytes(32)
        # `m_cache`: the tip, the time and the two estimates
        self._cache: tuple[bytes, float, FeeFrac, FeeFrac] | None = None
        try:
            data = path.read_bytes()
        except OSError:
            self.logger.log_debug(
                "estimatefee",
                "%s: %s does not exist. Continuing anyway",
                MEMPOOL_POLICY,
                path,
            )
            return
        if self.read(data, tip):
            self.logger.log_debug(
                "estimatefee",
                "%s: mined-block stats successfully read from %s.",
                MEMPOOL_POLICY,
                path,
            )

    def read(self, data: bytes, tip: tuple[int, bytes]) -> bool:
        """Take the blocks of `data`, a `write`, where they end at `tip`.

        Nothing changes where they do not, and a warning says why.
        """
        stream = BytesIO(data)
        try:
            (version,) = struct.unpack("<i", _read(stream, 4))
            if version != _FILE_VERSION:
                self.logger.warning(
                    "%s: file version not supported; continuing anyway", MEMPOOL_POLICY
                )
                return False
            blocks = [
                MinedBlockStats(*_STATS.unpack(_read(stream, _STATS.size)))
                for _ in range(var_int.parse(stream))
            ]
            tip_hash = _read(stream, 32)[::-1]
        except ValueError:
            self.logger.warning(
                "%s: Unable to read mined-block stats from stream (non-fatal)",
                MEMPOOL_POLICY,
            )
            return False
        if len(blocks) > _WINDOW_BLOCKS:
            self.logger.warning(
                "%s: Number of previously mined blocks read exceeds the maximum "
                "of %d; ignoring file",
                MEMPOOL_POLICY,
                _WINDOW_BLOCKS,
            )
            return False
        for previous, block in itertools.pairwise(blocks):
            # Core's `SaturatingAdd` on a `uint64_t`
            expected = min(previous.height + 1, 2**64 - 1)
            if block.height != expected:
                self.logger.warning(
                    "%s: Non-consecutive block heights read, expected height %d "
                    "but found %d; ignoring file",
                    MEMPOOL_POLICY,
                    expected,
                    block.height,
                )
                return False
        if blocks and (blocks[-1].height, tip_hash) != tip:
            self.logger.warning(
                "%s: Mined-block stats read end at height %d block %s, but the "
                "active chain tip is height %d block %s; ignoring file",
                MEMPOOL_POLICY,
                blocks[-1].height,
                tip_hash.hex(),
                tip[0],
                tip[1].hex(),
            )
            return False
        self.blocks = blocks
        self.tip_hash = tip_hash
        self._cache = None
        return True

    def write(self) -> bytes:
        """Serialize as Core's `MemPoolFeeRateEstimator::Write`."""
        return b"".join(
            [
                struct.pack("<i", _FILE_VERSION),
                var_int.serialize(len(self.blocks)),
                *(_STATS.pack(*block) for block in self.blocks),
                self.tip_hash[::-1],
            ]
        )

    def flush(self) -> None:
        """Write the blocks to `path`, Core's `FlushMinedBlockStats`."""
        try:
            self.path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        except OSError as error:
            self.logger.warning(
                "%s: failed to create mempool policy estimator directory %s: %s. "
                "Continuing anyway",
                MEMPOOL_POLICY,
                self.path.parent,
                error.strerror,
            )
            return
        try:
            self.path.write_bytes(self.write())
        except OSError:
            self.logger.warning(
                "%s: unable to open %s for writing. Continuing anyway",
                MEMPOOL_POLICY,
                self.path,
            )
            return
        self.logger.log_debug(
            "estimatefee",
            "%s: mined-block stats flushed to %s.",
            MEMPOOL_POLICY,
            self.path,
        )

    def mempool_load_failed(self) -> None:
        """Forget the blocks: the mempool that held them was not loaded."""
        if not self.blocks:
            return
        self.logger.log_debug(
            "estimatefee",
            "%s: mempool did not load; clearing mined-block stats",
            MEMPOOL_POLICY,
        )
        self.blocks.clear()

    def block_connected(
        self, block: Block, removed: Sequence[RemovedTx], height: int
    ) -> None:
        """Record `block`, which took `removed` from the mempool.

        Core's `MempoolTxsRemovedForBlock`: the weights leave out the
        coinbase, and the mempool's are of its own copies. A block at or
        below one tracked drops it and those above it, and a gap drops
        them all.
        """
        block_weight = sum(tx.weight for tx in block.transactions[1:])
        removed_weight = sum(tx.weight for tx in removed)
        if stale := [stats for stats in self.blocks if stats.height >= height]:
            self.logger.log_debug(
                "estimatefee",
                "%s: connected block height=%d discards tracked mined-block stats "
                "from height=%d to height=%d; stale_stats=%d",
                MEMPOOL_POLICY,
                height,
                stale[0].height,
                self.blocks[-1].height,
                len(stale),
            )
            del self.blocks[len(self.blocks) - len(stale) :]
        if self.blocks and self.blocks[-1].height + 1 != height:
            self.logger.log_debug(
                "estimatefee",
                "%s: clearing mined-block stats after height gap; tracked_stats=%d "
                "expected_height=%d received_height=%d",
                MEMPOOL_POLICY,
                len(self.blocks),
                self.blocks[-1].height + 1,
                height,
            )
            self.blocks.clear()
        if len(self.blocks) == _WINDOW_BLOCKS:
            del self.blocks[0]
        self.blocks.append(MinedBlockStats(height, removed_weight, block_weight))
        self.tip_hash = block.header.hash
        self._cache = None

    def health_error(self) -> str:
        """Return why the tracked blocks give no estimate, or "" if they do.

        Core's `GetMempoolHealth`: six blocks are needed, and where they
        weigh a block or more, the mempool must have held three quarters
        of their weight.
        """
        if len(self.blocks) < _WINDOW_BLOCKS:
            self.logger.log_debug(
                "estimatefee",
                "%s: mempool health check failed; tracked_blocks=%d required_blocks=%d",
                MEMPOOL_POLICY,
                len(self.blocks),
                _WINDOW_BLOCKS,
            )
            return "Not enough recent block data for fee rate estimation"
        total = sum(stats.block_weight for stats in self.blocks)
        removed = sum(stats.removed_weight for stats in self.blocks)
        if total < _MIN_WINDOW_WEIGHT:
            self.logger.log_debug(
                "estimatefee",
                "%s: mempool health check passed; low activity, "
                "total_block_weight=%d minimum=%d",
                MEMPOOL_POLICY,
                total,
                _MIN_WINDOW_WEIGHT,
            )
            return ""
        ratio = removed / total
        healthy = ratio >= _REPRESENTATION_THRESHOLD
        self.logger.log_debug(
            "estimatefee",
            "%s: mempool health check %s; removed_weight=%d total_block_weight=%d "
            "coverage=%.2f required_coverage=%.2f",
            MEMPOOL_POLICY,
            "passed" if healthy else "failed",
            removed,
            total,
            ratio,
            _REPRESENTATION_THRESHOLD,
        )
        return "" if healthy else "Mempool is unreliable for fee rate estimation"

    def estimate(self, node: Node, *, conservative: bool) -> FeeRateEstimate:
        """Return Core's `MemPoolFeeRateEstimator::EstimateFeeRate`.

        The block is built anew unless one built on the same tip less than
        `_CACHE_LIFE` seconds ago is cached. A percentile the mempool does
        not reach is the lowest feerate it relays.
        """
        if not node.mempool.load_tried:
            return _error("Mempool not loaded yet, no fee rate estimate available")
        if reason := self.health_error():
            return _error(reason)
        tip = node.chainstate.block_index.active_chain[-1]
        now = clock.now()
        cache = self._cache
        if cache is not None and cache[0] == tip and cache[1] + _CACHE_LIFE >= now:
            feerate = cache[2] if conservative else cache[3]
            return FeeRateEstimate(MEMPOOL_POLICY, feerate, MAX_TARGET)
        # sorted again, for the vsize each chunk's weight rounds up to
        feerates = sorted(chunk_feerates(node), key=_ratio, reverse=True)
        p50, p75 = percentiles(feerates)
        floor = FeeFrac(
            max(
                node.config.min_relay_feerate.sats_per_kvbyte,
                node.mempool.get_min_fee_rate().sats_per_kvbyte,
            ),
            1000,
        )
        p50 = p50 if p50.size else floor
        p75 = p75 if p75.size else floor
        self._cache = (tip, now, p50, p75)
        self.logger.log_debug(
            "estimatefee",
            "%s: conservative/economical fee rate: %d/%d sat/kvB",
            MEMPOOL_POLICY,
            fee_per_k(p50),
            fee_per_k(p75),
        )
        return FeeRateEstimate(MEMPOOL_POLICY, p50 if conservative else p75, MAX_TARGET)
