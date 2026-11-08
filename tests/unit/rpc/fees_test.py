# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`btclib_node.rpc.fees`: `estimatesmartfee` and `estimaterawfee`.

Every refusal and every empty answer here without `options` is the text
a regtest bitcoind answered for the same parameters. Those of `options`
are read from `src/rpc/fees.cpp`, at bitcoin/bitcoin@f0de87f040, the
v32.1 tag, and `tests/integration/fee_estimation_test.py` compares them
with bitcoind's.

`bitcoind` here is the release `integration-bitcoind.yml` pins.
"""

import json
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from btclib.fee import FeeRate

from btclib_node.cluster_linearize import FeeFrac
from btclib_node.config import DEFAULT_MIN_RELAY_FEERATE
from btclib_node.fee_estimator import (
    EstimationResult,
    EstimatorBucket,
    FeeEstimateHorizon,
    FeeEstimator,
    llround,
)
from btclib_node.log import Logger
from btclib_node.mempool_estimator import (
    BLOCK_POLICY,
    MEMPOOL_POLICY,
    FeeRateEstimate,
    MinedBlockStats,
)
from btclib_node.rpc.connection import JSONEncoder
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.fees import _bucket, estimate_raw_fee, estimate_smart_fee
from btclib_node.rpc.help import HELP_TEXT
from tests import LogLines
from tests.unit.fee_estimator_test import _busy

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# the connection a call came on, which neither callback reads
NO_CONN: Any = None


class _Mempool:
    """A mempool estimator answering `estimate`, and the blocks it holds."""

    def __init__(self, estimate: FeeRateEstimate) -> None:
        self._estimate = estimate
        self.blocks = [MinedBlockStats(7, 30, 40), MinedBlockStats(8, 10, 20)]

    def estimate(self, node: object, *, conservative: bool) -> FeeRateEstimate:
        return self._estimate


_NO_MEMPOOL_ESTIMATE = FeeRateEstimate(
    MEMPOOL_POLICY,
    FeeFrac(0, 0),
    2,
    "mempool_policy: Not enough recent block data for fee rate estimation",
)


def a_node(
    estimator: FeeEstimator,
    *,
    mempool_min: int = 0,
    min_relay: FeeRate = DEFAULT_MIN_RELAY_FEERATE,
    mempool_estimate: FeeRateEstimate = _NO_MEMPOOL_ESTIMATE,
) -> Any:
    """Return what the two callbacks read: the estimators and the floors."""
    return SimpleNamespace(
        fee_estimator=estimator,
        logger=estimator.logger,
        mempool_estimator=_Mempool(mempool_estimate),
        mempool=SimpleNamespace(
            get_min_fee_rate=lambda: FeeRate(sats_per_kvbyte=mempool_min)
        ),
        config=SimpleNamespace(min_relay_feerate=min_relay),
    )


def an_empty_node(tmp_path: Path) -> Any:
    """Return a node whose estimators have no history."""
    return a_node(FeeEstimator(tmp_path / "fee_estimates.dat", Logger(debug=True)))


def wire(result: object) -> str:
    """Return `result` as the RPC server writes it."""
    mark = "MARK"
    text = json.dumps(result, separators=(",", ":"), cls=JSONEncoder, mark=mark)
    return re.sub(f'"{mark}(.*?){mark}"', r"\1", text)


def refusal(call: Callable[[], object]) -> tuple[int, str]:
    """Return the code and message `call` is refused with."""
    with pytest.raises(RpcError) as caught:
        call()
    return int(caught.value.code), caught.value.message


_TOO_FAR = (-8, "Invalid conf_target, must be between 1 and 1008")
_NOT_AN_INT = (-1, "JSON integer out of range")
_BAD_MODE = (
    -8,
    (
        'Invalid estimate_mode parameter, must be one of: "unset", "economical", '
        '"conservative"'
    ),
)


def _wrong_types(*entries: str) -> tuple[int, str]:
    return -3, "Wrong type passed:\n{\n" + ",\n".join(entries) + "\n}"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ([0], _TOO_FAR),
        ([-1], _TOO_FAR),
        ([1009], _TOO_FAR),
        ([1.5], _NOT_AN_INT),
        ([1.0], _NOT_AN_INT),
        ([2**31], _NOT_AN_INT),
        ([-(2**31) - 1], _NOT_AN_INT),
        ([1, "bogus"], _BAD_MODE),
        # Core's `ToUpper` leaves a dotless i as it is
        ([1, "econom\u0131cal"], _BAD_MODE),
        (
            ["1"],
            _wrong_types(
                '    "Position 1 (conf_target)": "JSON value of type string is '
                'not of expected type number"'
            ),
        ),
        (
            ["x", 3],
            _wrong_types(
                '    "Position 1 (conf_target)": "JSON value of type string is '
                'not of expected type number"',
                '    "Position 2 (estimate_mode)": "JSON value of type number is '
                'not of expected type string"',
            ),
        ),
    ],
)
def test_estimatesmartfee_refuses_as_core(
    tmp_path: Path, params: list[Any], expected: tuple[int, str]
) -> None:
    """Each refusal is Core's code and message."""
    node = an_empty_node(tmp_path)
    assert refusal(lambda: estimate_smart_fee(node, NO_CONN, params)) == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ([1009], _TOO_FAR),
        ([1, 1.5], (-8, "Invalid threshold")),
        ([1, -0.1], (-8, "Invalid threshold")),
        ([1, float("inf")], (-1, "JSON double out of range")),
        ([1, 10**400], (-1, "JSON double out of range")),
        (
            [1, True],
            _wrong_types(
                '    "Position 2 (threshold)": "JSON value of type bool is '
                'not of expected type number"'
            ),
        ),
        (
            ["a", "b"],
            _wrong_types(
                '    "Position 1 (conf_target)": "JSON value of type string is '
                'not of expected type number"',
                '    "Position 2 (threshold)": "JSON value of type string is '
                'not of expected type number"',
            ),
        ),
    ],
)
def test_estimaterawfee_refuses_as_core(
    tmp_path: Path, params: list[Any], expected: tuple[int, str]
) -> None:
    """Each refusal is Core's code and message."""
    node = an_empty_node(tmp_path)
    assert refusal(lambda: estimate_raw_fee(node, NO_CONN, params)) == expected


@pytest.mark.parametrize(
    ("method", "call"),
    [("estimatesmartfee", estimate_smart_fee), ("estimaterawfee", estimate_raw_fee)],
)
def test_no_conf_target_is_the_help(
    tmp_path: Path, method: str, call: Callable[..., object]
) -> None:
    """A call with no argument answers the method's help, as Core's."""
    node = an_empty_node(tmp_path)
    assert refusal(lambda: call(node, NO_CONN, [])) == (-1, HELP_TEXT[method])


@pytest.mark.parametrize(
    "mode", [None, "unset", "ECONOMICAL", "conservative", "ConServative"]
)
def test_no_estimate_is_core_s_answer(tmp_path: Path, mode: str | None) -> None:
    """No history is Core's error list and 0 blocks, in every mode."""
    node = an_empty_node(tmp_path)
    assert wire(estimate_smart_fee(node, NO_CONN, [1, mode])) == (
        '{"errors":["Insufficient data or no feerate found"],"blocks":0}'
    )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (
            [],
            _wrong_types(
                '    "Position 3 (options)": "JSON value of type array is '
                'not of expected type object"'
            ),
        ),
        (
            {"fee_rate_estimator": 1},
            (
                -3,
                (
                    "JSON value of type number for field fee_rate_estimator is not of "
                    "expected type string"
                ),
            ),
        ),
        (
            {"verbosity": "2", "fee_rate_estimator": 1},
            (
                -3,
                (
                    "JSON value of type number for field fee_rate_estimator is not of "
                    "expected type string"
                ),
            ),
        ),
        (
            {"verbosity": True},
            (
                -3,
                (
                    "JSON value of type bool for field verbosity is not of expected "
                    "type number"
                ),
            ),
        ),
        ({"blocks": 2}, (-3, "Unexpected key blocks")),
        (
            {"fee_rate_estimator": "mempool"},
            (
                -8,
                (
                    'Invalid fee_rate_estimator parameter, must be one of: "auto", '
                    '"block_policy", "mempool_policy"'
                ),
            ),
        ),
        # Core's `ToLower` leaves a dotted capital I as it is
        (
            {"fee_rate_estimator": "\u0130"},
            (
                -8,
                (
                    'Invalid fee_rate_estimator parameter, must be one of: "auto", '
                    '"block_policy", "mempool_policy"'
                ),
            ),
        ),
        ({"verbosity": 1.5}, _NOT_AN_INT),
        ({"verbosity": 2**31}, _NOT_AN_INT),
    ],
)
def test_options_are_refused_as_core_s(
    tmp_path: Path, options: object, expected: tuple[int, str]
) -> None:
    """Each refusal of `options` is Core's code and message."""
    node = an_empty_node(tmp_path)
    call = lambda: estimate_smart_fee(node, NO_CONN, [1, None, options])  # noqa: E731
    assert refusal(call) == expected


def test_conf_target_is_checked_before_options(tmp_path: Path) -> None:
    """Core parses `conf_target`, then `estimate_mode`, then `options`."""
    node = an_empty_node(tmp_path)
    bad = {"blocks": 2}
    assert refusal(lambda: estimate_smart_fee(node, NO_CONN, [0, None, bad])) == (
        _TOO_FAR
    )
    assert refusal(lambda: estimate_smart_fee(node, NO_CONN, [1, "x", bad])) == (
        _BAD_MODE
    )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (None, '{"errors":["Insufficient data or no feerate found"],"blocks":0}'),
        (
            {"fee_rate_estimator": "Block_Policy"},
            '{"errors":["Insufficient data or no feerate found"],"blocks":0}',
        ),
        (
            {"fee_rate_estimator": "MEMPOOL_POLICY"},
            (
                '{"errors":["mempool_policy: Not enough recent block data for fee rate '
                'estimation"],"blocks":2}'
            ),
        ),
        (
            {"verbosity": 2},
            (
                '{"errors":["Insufficient data or no feerate found"],"blocks":0,'
                '"mempool_health_statistics":[{"block_height":8,"block_weight":20,'
                '"mempool_txs_weight":10},{"block_height":7,"block_weight":40,'
                '"mempool_txs_weight":30}]}'
            ),
        ),
    ],
)
def test_no_estimate_names_its_estimator_s_error(
    tmp_path: Path, options: object, expected: str
) -> None:
    """The error and target are the estimator's; blocks come newest first."""
    node = an_empty_node(tmp_path)
    assert wire(estimate_smart_fee(node, NO_CONN, [1, None, options])) == expected


def _mempool_estimate(fee_per_k: int) -> FeeRateEstimate:
    return FeeRateEstimate(MEMPOOL_POLICY, FeeFrac(fee_per_k * 3, 3000), 2)


def test_auto_is_the_lower_estimate(tmp_path: Path) -> None:
    """The mempool's estimate where it is lower, the block policy's on a tie."""
    estimator = _busy(tmp_path)
    block = estimator.estimate_smart_fee(6, conservative=False)
    lower = a_node(estimator, mempool_estimate=_mempool_estimate(block.fee_per_k - 1))
    assert wire(estimate_smart_fee(lower, NO_CONN, [6])) == (
        f'{{"feerate":0.{block.fee_per_k - 1:08d},"estimator":"mempool_policy",'
        '"blocks":2}'
    )
    tie = a_node(estimator, mempool_estimate=_mempool_estimate(block.fee_per_k))
    assert wire(estimate_smart_fee(tie, NO_CONN, [6])) == (
        f'{{"feerate":0.{block.fee_per_k:08d},"estimator":"block_policy",'
        f'"blocks":{block.returned_target}}}'
    )


def test_auto_logs_the_estimate_it_chose_and_why_one_failed(tmp_path: Path) -> None:
    """The estimator manager's debug lines, in Core's words."""
    estimator = _busy(tmp_path)
    lines = LogLines()
    estimator.logger.addHandler(lines)
    block = estimator.estimate_smart_fee(6, conservative=False)
    estimate_smart_fee(a_node(estimator), NO_CONN, [6])
    assert lines.messages[-2:] == [
        _NO_MEMPOOL_ESTIMATE.error,
        (
            f"Fee rate estimated using block_policy: target={block.returned_target} "
            f"feerate={block.fee_per_k} sat/kvB."
        ),
    ]
    lower = a_node(estimator, mempool_estimate=_mempool_estimate(block.fee_per_k - 1))
    estimate_smart_fee(lower, NO_CONN, [6])
    assert lines.messages[-1] == (
        f"Fee rate estimated using mempool_policy: target=2 "
        f"feerate={block.fee_per_k - 1} sat/kvB."
    )
    empty = an_empty_node(tmp_path)
    empty.logger.addHandler(lines)
    estimate_smart_fee(empty, NO_CONN, [6])
    assert lines.messages[-1] == "Insufficient data or no feerate found"


@pytest.mark.parametrize(
    ("estimator_name", "expected_blocks"), [(BLOCK_POLICY, None), (MEMPOOL_POLICY, 2)]
)
def test_one_estimator_named_answers_no_estimator_field(
    tmp_path: Path, estimator_name: str, expected_blocks: int | None
) -> None:
    """Only `auto` names the estimator that answered."""
    estimator = _busy(tmp_path)
    block = estimator.estimate_smart_fee(6, conservative=False)
    node = a_node(estimator, mempool_estimate=_mempool_estimate(block.fee_per_k + 7))
    fee = block.fee_per_k if expected_blocks is None else block.fee_per_k + 7
    blocks = block.returned_target if expected_blocks is None else expected_blocks
    options = {"fee_rate_estimator": estimator_name}
    assert wire(estimate_smart_fee(node, NO_CONN, [6, None, options])) == (
        f'{{"feerate":0.{fee:08d},"blocks":{blocks}}}'
    )


def test_a_mempool_estimate_is_rounded_down_then_raised_to_the_floors(
    tmp_path: Path,
) -> None:
    """`CFeeRate::GetFeePerK` of a fee over vbytes, then the higher floor."""
    estimate = FeeRateEstimate(MEMPOOL_POLICY, FeeFrac(1000, 3), 2)
    options = {"fee_rate_estimator": "mempool_policy"}
    node = a_node(_busy(tmp_path), mempool_estimate=estimate)
    assert wire(estimate_smart_fee(node, NO_CONN, [6, None, options])) == (
        '{"feerate":0.00333333,"blocks":2}'
    )
    node = a_node(_busy(tmp_path), mempool_estimate=estimate, mempool_min=333_334)
    assert wire(estimate_smart_fee(node, NO_CONN, [6, None, options])) == (
        '{"feerate":0.00333334,"blocks":2}'
    )


def _no_raw(horizon: str, decay: str, scale: int) -> str:
    return (
        f'"{horizon}":{{"decay":{decay},"scale":{scale},"fail":{{"startrange":0,'
        '"endrange":1e+99,"withintarget":0,"totalconfirmed":0,"inmempool":0,'
        '"leftmempool":0},"errors":["Insufficient data or no feerate found '
        'which meets threshold"]}'
    )


_SHORT = _no_raw("short", "0.962", 1)
_MEDIUM = _no_raw("medium", "0.9952", 2)
_LONG = _no_raw("long", "0.99931", 24)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ([1], f"{{{_SHORT},{_MEDIUM},{_LONG}}}"),
        ([1, None], f"{{{_SHORT},{_MEDIUM},{_LONG}}}"),
        ([1, 0], f"{{{_SHORT},{_MEDIUM},{_LONG}}}"),
        ([1, 1], f"{{{_SHORT},{_MEDIUM},{_LONG}}}"),
        ([13], f"{{{_MEDIUM},{_LONG}}}"),
        ([49], f"{{{_LONG}}}"),
    ],
)
def test_no_raw_estimate_is_core_s_answer(
    tmp_path: Path, params: list[Any], expected: str
) -> None:
    """No history is Core's answer for each horizon tracking the target."""
    node = an_empty_node(tmp_path)
    assert wire(estimate_raw_fee(node, NO_CONN, params)) == expected


def test_a_smart_estimate_is_the_estimator_s_in_btc(tmp_path: Path) -> None:
    """The mode reaches the estimator; the answer is in BTC per kvB."""
    estimator = _busy(tmp_path)
    node = a_node(estimator)
    economical = estimator.estimate_smart_fee(2, conservative=False)
    conservative = estimator.estimate_smart_fee(2, conservative=True)
    assert economical.fee_per_k != conservative.fee_per_k
    for mode, fee in [("unset", economical), ("conservative", conservative)]:
        expected = (
            f'{{"feerate":0.{fee.fee_per_k:08d},"estimator":"block_policy","blocks":2}}'
        )
        assert wire(estimate_smart_fee(node, NO_CONN, [2, mode])) == expected


@pytest.mark.parametrize("floor", ["mempool", "relay"])
def test_a_smart_estimate_is_raised_to_the_floors(tmp_path: Path, floor: str) -> None:
    """An estimate below the mempool or relay minimum is raised to it."""
    estimator = _busy(tmp_path)
    high = estimator.estimate_smart_fee(2, conservative=True).fee_per_k + 1
    if floor == "mempool":
        node = a_node(estimator, mempool_min=high)
    else:
        node = a_node(estimator, min_relay=FeeRate(sats_per_kvbyte=high))
    assert wire(estimate_smart_fee(node, NO_CONN, [2, "conservative"])) == (
        f'{{"feerate":0.{high:08d},"estimator":"block_policy","blocks":2}}'
    )


def test_a_raw_estimate_names_its_passing_and_failing_ranges(tmp_path: Path) -> None:
    """Each horizon's answer is its estimate and ranges, rounded as Core's."""
    estimator = _busy(tmp_path)
    node = a_node(estimator)
    answer = json.loads(wire(estimate_raw_fee(node, NO_CONN, [2, 0.5])))
    assert list(answer) == ["short", "medium", "long"]
    for horizon in FeeEstimateHorizon:
        result = EstimationResult()
        fee = estimator.estimate_raw_fee(2, 0.5, horizon, result)
        entry = answer[horizon.value]
        assert list(entry) == ["feerate", "decay", "scale", "pass", "fail"]
        assert entry["feerate"] == fee / 1e8
        assert entry["decay"] == result.decay
        assert entry["scale"] == result.scale
        for key, bucket in [("pass", result.pass_bucket), ("fail", result.fail_bucket)]:
            assert entry[key] == {
                "startrange": llround(bucket.start),
                "endrange": llround(bucket.end),
                "withintarget": llround(bucket.within_target * 100) / 100,
                "totalconfirmed": llround(bucket.total_confirmed * 100) / 100,
                "inmempool": llround(bucket.in_mempool * 100) / 100,
                "leftmempool": llround(bucket.left_mempool * 100) / 100,
            }


def test_a_raw_estimate_with_no_failing_range_names_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing range starting at -1 is one no bucket fell in: not shown."""
    estimator = _busy(tmp_path)

    def every_bucket_passes(
        conf_target: int,
        threshold: float,
        horizon: FeeEstimateHorizon,
        result: EstimationResult,
    ) -> int:
        result.decay, result.scale = 0.5, 1
        return 1000

    monkeypatch.setattr(estimator, "estimate_raw_fee", every_bucket_passes)
    answer = json.loads(wire(estimate_raw_fee(a_node(estimator), NO_CONN, [1])))
    assert list(answer["short"]) == ["feerate", "decay", "scale", "pass"]


def test_a_range_is_rounded_halves_away_from_zero_as_c_s_round() -> None:
    """Core's `round`, where Python's would round a half to even."""
    bucket = EstimatorBucket(2.5, 4.5, 0.125, 0.375, 0.0, 0.0)
    assert wire(_bucket(bucket)) == (
        '{"startrange":3,"endrange":5,"withintarget":0.13,'
        '"totalconfirmed":0.38,"inmempool":0,"leftmempool":0}'
    )
