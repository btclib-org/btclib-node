# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`btclib_node.mempool_estimator`: Core's `MemPoolFeeRateEstimator`.

The cases follow `src/policy/fees/mempool_estimator.cpp` at
bitcoin/bitcoin@f0de87f040, the v32.1 tag;
`tests/integration/fee_estimation_test.py` compares the estimates and
the file with `bitcoind`'s.

`bitcoind` here is the release `integration-bitcoind.yml` pins.
"""

import struct
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from btclib import var_int
from btclib.consensus import MAX_BLOCK_WEIGHT
from btclib.fee import FeeRate
from btclib.tx import OutPoint

from btclib_node import clock, main, mempool_estimator
from btclib_node.cluster_linearize import FeeFrac
from btclib_node.config import DEFAULT_MIN_RELAY_FEERATE
from btclib_node.fee_estimator import RemovedTx
from btclib_node.log import Logger
from btclib_node.mempool_estimator import (
    MAX_TARGET,
    MEMPOOL_POLICY,
    FeeRateEstimate,
    MempoolEstimator,
    MinedBlockStats,
    percentiles,
)
from btclib_node.mining import create_new_block
from tests import LogLines
from tests.unit.mining_test import (
    FEE,
    SCRIPT,
    SUBSIDY,
    coinbases,
    mine,
    mine_template,
    pool,
    spend,
)

if TYPE_CHECKING:
    from pathlib import Path

_TIP = (5, bytes(range(32)))

# a block's weight in vbytes, so that a chunk of this size fills it
_BLOCK_VBYTES = MAX_BLOCK_WEIGHT // 4


def an_estimator(
    tmp_path: Path, tip: tuple[int, bytes] = _TIP
) -> tuple[MempoolEstimator, LogLines]:
    """Return an estimator with no file to read, and what it logs."""
    logger = Logger(debug=True)
    lines = LogLines()
    logger.addHandler(lines)
    path = tmp_path / "fees" / "mempool_policy_estimator.dat"
    return MempoolEstimator(path, logger, tip), lines


def _stats(*heights: int, removed: int = 3, weight: int = 4) -> list[MinedBlockStats]:
    return [MinedBlockStats(height, removed, weight) for height in heights]


def _file(
    blocks: list[MinedBlockStats], tip_hash: bytes = _TIP[1], version: int = 1
) -> bytes:
    return b"".join(
        [
            struct.pack("<i", version),
            var_int.serialize(len(blocks)),
            *(struct.pack("<QQQ", *block) for block in blocks),
            tip_hash[::-1],
        ]
    )


def test_percentiles_are_read_at_half_and_three_quarters_of_a_block() -> None:
    """Core's `CalculateMaxWeightPercentiles`, by the weight of each chunk."""
    quarter = _BLOCK_VBYTES // 4
    rates = [FeeFrac(4 * quarter, quarter), FeeFrac(3 * quarter, quarter)]
    rates += [FeeFrac(2 * quarter, quarter), FeeFrac(quarter, quarter)]
    assert percentiles(rates) == (rates[1], rates[2])
    assert percentiles(rates[:2]) == (rates[1], FeeFrac(0, 0))
    assert percentiles(rates[:1]) == (FeeFrac(0, 0), FeeFrac(0, 0))
    # one chunk past both is both
    assert percentiles([FeeFrac(5, _BLOCK_VBYTES)]) == (
        FeeFrac(5, _BLOCK_VBYTES),
        FeeFrac(5, _BLOCK_VBYTES),
    )


def test_a_missing_file_starts_with_no_block(tmp_path: Path) -> None:
    """Core's debug line, and nothing tracked."""
    estimator, lines = an_estimator(tmp_path)
    assert estimator.blocks == []
    assert estimator.tip_hash == bytes(32)
    assert any("does not exist. Continuing anyway" in line for line in lines.messages)


def test_a_file_read_back_is_written_alike(tmp_path: Path) -> None:
    """`write` is what `read` takes, byte for byte, at its own tip."""
    estimator, _ = an_estimator(tmp_path)
    data = _file(_stats(3, 4, 5))
    assert estimator.read(data, _TIP)
    assert estimator.blocks == _stats(3, 4, 5)
    assert estimator.write() == data
    estimator.flush()
    reader, lines = an_estimator(tmp_path)
    assert reader.blocks == _stats(3, 4, 5)
    assert any("successfully read" in line for line in lines.messages)


def test_a_file_left_at_another_tip_is_not_taken_at_start(tmp_path: Path) -> None:
    """The node starts with no block, and says why."""
    path = tmp_path / "fees" / "mempool_policy_estimator.dat"
    path.parent.mkdir()
    path.write_bytes(_file(_stats(4)))
    estimator, lines = an_estimator(tmp_path)
    assert estimator.blocks == []
    assert all("successfully read" not in line for line in lines.messages)


def test_an_empty_list_is_read_at_any_tip(tmp_path: Path) -> None:
    """No block to check against the tip: the file's hash is taken."""
    estimator, _ = an_estimator(tmp_path)
    assert estimator.read(_file([], bytes(32)), _TIP)
    assert estimator.tip_hash == bytes(32)


@pytest.mark.parametrize(
    ("data", "warning"),
    [
        (_file(_stats(5), version=2), "file version not supported"),
        (_file(_stats(5))[:-1], "Unable to read mined-block stats"),
        (b"\x01\x00\x00\x00\xfd\x01\x00", "Unable to read mined-block stats"),
        (_file(_stats(*range(7))), "exceeds the maximum of 6"),
        (_file(_stats(3, 5)), "expected height 4 but found 5"),
        (_file(_stats(4)), "the active chain tip is height 5"),
        (_file(_stats(5), bytes(32)), "the active chain tip is height 5"),
    ],
)
def test_a_file_that_does_not_end_at_the_tip_is_ignored(
    tmp_path: Path, data: bytes, warning: str
) -> None:
    """Each refusal is Core's warning, and nothing is taken."""
    estimator, lines = an_estimator(tmp_path)
    estimator.blocks = _stats(1)
    assert not estimator.read(data, _TIP)
    assert estimator.blocks == _stats(1)
    assert any(warning in line for line in lines.messages)


def test_a_flush_that_fails_is_warned_about(tmp_path: Path) -> None:
    """A directory or a file that cannot be written is Core's warning."""
    estimator, lines = an_estimator(tmp_path)
    (tmp_path / "fees").write_bytes(b"")
    estimator.flush()
    assert any("failed to create mempool policy" in line for line in lines.messages)
    estimator.path = tmp_path / "taken"
    estimator.path.mkdir()
    estimator.flush()
    assert any("unable to open" in line for line in lines.messages)


def test_a_mempool_that_did_not_load_clears_the_blocks(tmp_path: Path) -> None:
    """Core's `MempoolLoadFailed`: the blocks went with the mempool."""
    estimator, lines = an_estimator(tmp_path)
    estimator.mempool_load_failed()
    assert all("did not load" not in line for line in lines.messages)
    estimator.blocks = _stats(4, 5)
    estimator.mempool_load_failed()
    assert estimator.blocks == []
    assert any("did not load" in line for line in lines.messages)


def _block(*weights: int) -> Any:
    """Return a block whose transactions after the coinbase weigh `weights`."""
    txs = [SimpleNamespace(id=bytes([i]), weight=w) for i, w in enumerate(weights)]
    coinbase = SimpleNamespace(id=b"cb", weight=1000)
    header = SimpleNamespace(hash=bytes([len(weights)]) * 32)
    return SimpleNamespace(transactions=[coinbase, *txs], header=header)


def test_a_block_records_its_weight_and_the_mempool_s(tmp_path: Path) -> None:
    """The coinbase is left out; the removed weight is the mempool's copies'."""
    estimator, _ = an_estimator(tmp_path)
    block = _block(10, 20, 30)
    # a copy with another witness weighs what the mempool's weighs
    removed = [RemovedTx(b"\x00", 1, 1, 1, 11), RemovedTx(b"\x02", 1, 1, 1, 30)]
    estimator.block_connected(block, removed, 6)
    assert estimator.blocks == [MinedBlockStats(6, 41, 60)]
    assert estimator.tip_hash == block.header.hash


def test_the_window_holds_six_consecutive_blocks(tmp_path: Path) -> None:
    """A seventh drops the first, a reorg drops what it replaces, a gap all."""
    estimator, lines = an_estimator(tmp_path)
    for height in range(1, 8):
        estimator.block_connected(_block(), [], height)
    assert [stats.height for stats in estimator.blocks] == [2, 3, 4, 5, 6, 7]
    estimator.block_connected(_block(), [], 5)
    assert [stats.height for stats in estimator.blocks] == [2, 3, 4, 5]
    assert any("stale_stats=3" in line for line in lines.messages)
    estimator.block_connected(_block(), [], 9)
    assert [stats.height for stats in estimator.blocks] == [9]
    assert any("after height gap" in line for line in lines.messages)


@pytest.mark.parametrize(
    ("removed", "weight", "error", "logged"),
    [
        # under a block's weight in all, the share is not asked
        (0, MAX_BLOCK_WEIGHT // 6 - 1, "", "passed; low activity, total_block_weight"),
        (3, MAX_BLOCK_WEIGHT, "", "passed; removed_weight=18000000"),
        (
            2,
            MAX_BLOCK_WEIGHT,
            "Mempool is unreliable for fee rate estimation",
            (
                "failed; removed_weight=12000000 total_block_weight=24000000 "
                "coverage=0.50 required_coverage=0.75"
            ),
        ),
    ],
)
def test_the_health_of_six_blocks_is_their_share_held(
    tmp_path: Path, removed: int, weight: int, error: str, logged: str
) -> None:
    """Core's `GetMempoolHealth`: three quarters held, or little weight."""
    estimator, lines = an_estimator(tmp_path)
    assert estimator.health_error() == (
        "Not enough recent block data for fee rate estimation"
    )
    removed_weight = removed * weight // 4
    estimator.blocks = _stats(*range(6), removed=removed_weight, weight=weight)
    assert estimator.health_error() == error
    assert f"mempool_policy: mempool health check {logged}" in lines.messages[-1]


def a_node(*, loaded: bool = True, mempool_min: int = 0) -> Any:
    """Return what `estimate` reads of a node."""
    return SimpleNamespace(
        mempool=SimpleNamespace(
            load_tried=loaded,
            get_min_fee_rate=lambda: FeeRate(sats_per_kvbyte=mempool_min),
        ),
        chainstate=SimpleNamespace(
            block_index=SimpleNamespace(active_chain=[b"genesis", b"tip"])
        ),
        config=SimpleNamespace(min_relay_feerate=DEFAULT_MIN_RELAY_FEERATE),
    )


def _healthy(tmp_path: Path) -> MempoolEstimator:
    estimator, _ = an_estimator(tmp_path)
    estimator.blocks = _stats(*range(6))
    return estimator


def test_no_estimate_before_the_mempool_loads_or_while_unhealthy(
    tmp_path: Path,
) -> None:
    """Each reason is Core's, prefixed with the estimator's name."""
    estimator = _healthy(tmp_path)
    assert estimator.estimate(a_node(loaded=False), conservative=False) == (
        FeeRateEstimate(
            MEMPOOL_POLICY,
            FeeFrac(0, 0),
            MAX_TARGET,
            "mempool_policy: Mempool not loaded yet, no fee rate estimate available",
        )
    )
    estimator.blocks.pop()
    assert estimator.estimate(a_node(), conservative=False).error == (
        "mempool_policy: Not enough recent block data for fee rate estimation"
    )


def test_an_estimate_is_the_block_s_percentile_or_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conservative is half a block, economical three quarters; sorted first."""
    half = _BLOCK_VBYTES // 2
    rates = [FeeFrac(3 * half, half), FeeFrac(5 * half, half)]
    monkeypatch.setattr(mempool_estimator, "chunk_feerates", lambda _: rates)
    estimator = _healthy(tmp_path)
    node = a_node(mempool_min=150)
    assert estimator.estimate(node, conservative=True) == FeeRateEstimate(
        MEMPOOL_POLICY, rates[1], MAX_TARGET
    )
    estimator._cache = None
    assert estimator.estimate(node, conservative=False) == FeeRateEstimate(
        MEMPOOL_POLICY, rates[0], MAX_TARGET
    )
    # a mempool too sparse for either is the higher of the two minimums
    monkeypatch.setattr(mempool_estimator, "chunk_feerates", lambda _: [])
    estimator._cache = None
    assert estimator.estimate(node, conservative=True).feerate == FeeFrac(150, 1000)


def test_an_estimate_is_cached_on_its_tip_for_seven_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's estimate cache, which a new tip or a new block clears."""
    built: list[int] = []

    def feerates(_: object) -> list[FeeFrac]:
        built.append(1)
        return [FeeFrac(len(built) * _BLOCK_VBYTES, _BLOCK_VBYTES)]

    monkeypatch.setattr(mempool_estimator, "chunk_feerates", feerates)
    estimator = _healthy(tmp_path)
    node = a_node()
    clock.set_mock_time(1_000_000)
    try:
        first = estimator.estimate(node, conservative=False)
        clock.set_mock_time(1_000_007)
        assert estimator.estimate(node, conservative=True) == first
        assert len(built) == 1
        clock.set_mock_time(1_000_008)
        assert estimator.estimate(node, conservative=False) != first
        assert len(built) == 2
        node.chainstate.block_index.active_chain.append(b"next")
        estimator.estimate(node, conservative=False)
        assert len(built) == 3
        estimator.block_connected(_block(), [], 6)
        estimator.estimate(node, conservative=False)
        assert len(built) == 4
    finally:
        clock.set_mock_time(0)


def test_a_node_records_the_blocks_it_connects_out_of_ibd(
    regtest_node: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each block after initial block download, with what its mempool held."""
    node = regtest_node()
    mine(node, 101 + 6)
    blocks = node.mempool_estimator.blocks
    assert [stats.height for stats in blocks] == list(range(102, 108))
    (coinbase,) = coinbases(node, 1)
    tx = pool(node, spend([OutPoint(coinbase.id, 0)], [SUBSIDY - FEE]), FEE)
    mined = mine_template(node, create_new_block(node, SCRIPT).block)
    assert node.mempool_estimator.blocks[-1] == MinedBlockStats(
        108, tx.weight, tx.weight
    )
    assert node.mempool_estimator.tip_hash == mined.header.hash
    # a block connected during initial block download is not recorded
    node.is_initial_block_download = True
    monkeypatch.setattr(main, "update_ibd_status", lambda *_: None)
    mine(node, 1)
    assert node.mempool_estimator.blocks[-1].height == 108


def test_a_node_s_sparse_mempool_is_estimated_at_the_relay_floor(
    regtest_node: Any,
) -> None:
    """One transaction fills no half block: the estimate is the relay floor."""
    node = regtest_node()
    mine(node, 101 + 6)
    (coinbase,) = coinbases(node, 1)
    pool(node, spend([OutPoint(coinbase.id, 0)], [SUBSIDY - FEE]), FEE)
    node.mempool.load_tried = True
    floor = node.config.min_relay_feerate.sats_per_kvbyte
    assert node.mempool_estimator.estimate(node, conservative=False) == (
        FeeRateEstimate(MEMPOOL_POLICY, FeeFrac(floor, 1000), MAX_TARGET)
    )
