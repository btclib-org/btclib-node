# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`btclib_node.rpc.fees`: `estimatesmartfee` and `estimaterawfee`.

Every refusal and every empty answer here is the text a regtest
bitcoind v31.1.0 answered for the same parameters.
"""

import json
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from btclib.fee import FeeRate

from btclib_node.config import DEFAULT_MIN_RELAY_FEERATE
from btclib_node.fee_estimator import (
    EstimationResult,
    EstimatorBucket,
    FeeEstimateHorizon,
    FeeEstimator,
    llround,
)
from btclib_node.log import Logger
from btclib_node.rpc.connection import JSONEncoder
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.fees import _bucket, estimate_raw_fee, estimate_smart_fee
from btclib_node.rpc.help import HELP_TEXT
from tests.unit.fee_estimator_test import _busy

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# the connection a call came on, which neither callback reads
NO_CONN: Any = None


def a_node(
    estimator: FeeEstimator,
    *,
    mempool_min: int = 0,
    min_relay: FeeRate = DEFAULT_MIN_RELAY_FEERATE,
) -> Any:
    """Return what the two callbacks read: the estimator and the floors."""
    return SimpleNamespace(
        fee_estimator=estimator,
        mempool=SimpleNamespace(
            get_min_fee_rate=lambda: FeeRate(sats_per_kvbyte=mempool_min)
        ),
        config=SimpleNamespace(min_relay_feerate=min_relay),
    )


def an_empty_node(tmp_path: Path) -> Any:
    """Return a node whose estimator has no history."""
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
        expected = f'{{"feerate":0.{fee.fee_per_k:08d},"blocks":2}}'
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
        f'{{"feerate":0.{high:08d},"blocks":2}}'
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
