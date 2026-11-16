# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`estimatesmartfee` and `estimaterawfee`, Core's `src/rpc/fees.cpp`.

Read at bitcoin/bitcoin@f0de87f040, the v32.1 tag: the same arguments,
refusals, answers and units, from `Node.fee_estimator` and
`Node.mempool_estimator`, combined as Core's `FeeRateEstimatorManager`
combines its two (`src/policy/fees/estimator_man.cpp`, same tag).
"""

import string
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import RPCErrorCode

from btclib_node.cluster_linearize import FeeFrac
from btclib_node.fee_estimator import (
    EstimationResult,
    EstimatorBucket,
    FeeEstimateHorizon,
    llround,
)
from btclib_node.mempool_estimator import (
    BLOCK_POLICY,
    MAX_TARGET,
    MEMPOOL_POLICY,
    FeeRateEstimate,
    fee_per_k,
)
from btclib_node.rpc.connection import btc_amount, univalue_float
from btclib_node.rpc.errors import RpcError, check_object, type_errors
from btclib_node.rpc.help import HELP_TEXT
from btclib_node.rpc.jsonrpc import RawJSON, get_real

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

__all__ = ["estimate_raw_fee", "estimate_smart_fee"]

# Core's `FeeModeMap`, in its order (`src/common/messages.cpp`)
_FEE_MODES = ("unset", "economical", "conservative")

# Core's `FeeRateEstimatorTypeFromString` (`src/util/fees.cpp`)
_AUTO = "auto"
_ESTIMATORS = (_AUTO, BLOCK_POLICY, MEMPOOL_POLICY)

_INT_MIN, _INT_MAX = -(2**31), 2**31 - 1

_UPPER = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)
_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_types(
    method: str, params: list[Any], *optional: tuple[str, str, bool]
) -> None:
    """Refuse a call as `RPCMethod::HandleRequest` does, before its body.

    A missing `conf_target` is the method's help; a value of the wrong
    JSON type is every such argument in one `RPC_TYPE_ERROR`. `optional`
    names each argument after `conf_target`, its type, and whether that
    type is a number.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT[method])
    mismatches = []
    if not _is_number(params[0]):
        mismatches.append((1, "conf_target", params[0], "number"))
    for position, (name, expected, is_number) in enumerate(optional, start=2):
        if len(params) < position or params[position - 1] is None:
            continue
        value = params[position - 1]
        if is_number:
            matches = _is_number(value)
        elif expected == "object":
            matches = isinstance(value, dict)
        else:
            matches = isinstance(value, str)
        if not matches:
            mismatches.append((position, name, value, expected))
    if mismatches:
        raise type_errors(*mismatches)


# `float | int`: a float is refused whatever its value
def _int(value: float | int) -> int:  # noqa: PYI041
    """Return `UniValue::getInt<int>` of `value`, a JSON number."""
    if isinstance(value, float) or not _INT_MIN <= value <= _INT_MAX:
        # `getInt<int>`, which reads no fraction or exponent
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    return value


def _conf_target(node: Node, value: float | int) -> int:  # noqa: PYI041
    """Return Core's `ParseConfirmTarget` of `value`, a JSON number.

    The highest target is `FeeRateEstimatorManager::MaximumTarget`, the
    higher of the two estimators'.
    """
    target = _int(value)
    highest = max(
        node.fee_estimator.highest_target_tracked(FeeEstimateHorizon.LONG), MAX_TARGET
    )
    if not 1 <= target <= highest:
        msg = f"Invalid conf_target, must be between 1 and {highest}"
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, msg)
    return target


def _block_policy(
    node: Node, conf_target: int, *, conservative: bool
) -> FeeRateEstimate:
    """Return `CBlockPolicyEstimator::EstimateFeeRate`."""
    fee = node.fee_estimator.estimate_smart_fee(conf_target, conservative=conservative)
    if not fee.fee_per_k:
        return FeeRateEstimate(
            BLOCK_POLICY,
            FeeFrac(0, 0),
            fee.returned_target,
            "Insufficient data or no feerate found",
        )
    return FeeRateEstimate(
        BLOCK_POLICY, FeeFrac(fee.fee_per_k, 1000), fee.returned_target
    )


def _ratio(feerate: FeeFrac) -> Fraction:
    return Fraction(feerate.fee, feerate.size)


def _estimate(
    node: Node, estimator: str, conf_target: int, *, conservative: bool
) -> FeeRateEstimate:
    """Return `FeeRateEstimatorManager::GetFeeRateEstimate`.

    `auto` is the block policy estimate, or the mempool's where that
    one is there and lower; a tie is the block policy's.
    """
    if estimator == MEMPOOL_POLICY:
        return node.mempool_estimator.estimate(node, conservative=conservative)
    block = _block_policy(node, conf_target, conservative=conservative)
    if estimator == BLOCK_POLICY:
        return block
    logger = node.logger
    if block.error:
        logger.log_debug("estimatefee", "%s", block.error)
        return block
    selected = block
    mempool = node.mempool_estimator.estimate(node, conservative=conservative)
    if mempool.error:
        logger.log_debug("estimatefee", "%s", mempool.error)
    elif _ratio(mempool.feerate) < _ratio(block.feerate):
        selected = mempool
    logger.log_debug(
        "estimatefee",
        "Fee rate estimated using %s: target=%s feerate=%s sat/kvB.",
        selected.estimator,
        selected.returned_target,
        fee_per_k(selected.feerate),
    )
    return selected


def _health_statistics(node: Node) -> list[dict[str, int]]:
    """Return the tracked blocks, newest first, as Core answers them."""
    return [
        {
            "block_height": stats.height,
            "block_weight": stats.block_weight,
            "mempool_txs_weight": stats.removed_weight,
        }
        for stats in reversed(node.mempool_estimator.blocks)
    ]


def estimate_smart_fee(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `estimatesmartfee`.

    The estimate is raised to the mempool's minimum feerate and to
    `-minrelaytxfee`, as Core raises it. `unset` is `economical`.
    `options` names the estimator, `auto` by default, and a `verbosity`
    of 2 or more adds the mempool estimator's blocks.
    """
    _check_types(
        "estimatesmartfee",
        params,
        ("estimate_mode", "string", False),
        ("options", "object", False),
    )
    conf_target = _conf_target(node, params[0])
    mode = params[1] if len(params) > 1 and params[1] is not None else "economical"
    # Core's `ToUpper`, which changes ASCII letters alone
    mode = mode.translate(_UPPER)
    if mode not in (name.upper() for name in _FEE_MODES):
        modes = '", "'.join(_FEE_MODES)
        msg = f'Invalid estimate_mode parameter, must be one of: "{modes}"'
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, msg)
    options = params[2] if len(params) > 2 and params[2] is not None else {}  # noqa: PLR2004
    # `RPCTypeCheckObj` reads its fields in the order of a `std::map`
    check_object(
        options,
        {"fee_rate_estimator": "string", "verbosity": "number"},
        allow_null=True,
    )
    estimator = options.get("fee_rate_estimator")
    # Core's `ToLower`, which changes ASCII letters alone
    estimator = _AUTO if estimator is None else estimator.translate(_LOWER)
    if estimator not in _ESTIMATORS:
        estimators = '", "'.join(_ESTIMATORS)
        msg = f'Invalid fee_rate_estimator parameter, must be one of: "{estimators}"'
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, msg)
    verbosity = options.get("verbosity")
    verbosity = 1 if verbosity is None else _int(verbosity)
    estimate = _estimate(
        node, estimator, conf_target, conservative=mode == "CONSERVATIVE"
    )
    result: dict[str, Any] = {}
    if estimate.error:
        result["errors"] = [estimate.error]
    else:
        floor = max(
            node.mempool.get_min_fee_rate().sats_per_kvbyte,
            node.config.min_relay_feerate.sats_per_kvbyte,
        )
        # the higher feerate's sat/kvB
        result["feerate"] = btc_amount(max(fee_per_k(estimate.feerate), floor))
        if estimator == _AUTO:
            result["estimator"] = estimate.estimator
    result["blocks"] = estimate.returned_target
    if verbosity >= 2:  # noqa: PLR2004
        result["mempool_health_statistics"] = _health_statistics(node)
    return result


def _round(value: float) -> float:
    """Return C's `round`: the nearest integer, halves away from zero."""
    return float(llround(value))


def _bucket(bucket: EstimatorBucket) -> dict[str, RawJSON]:
    return {
        "startrange": univalue_float(_round(bucket.start)),
        "endrange": univalue_float(_round(bucket.end)),
        "withintarget": univalue_float(_round(bucket.within_target * 100.0) / 100.0),
        "totalconfirmed": univalue_float(
            _round(bucket.total_confirmed * 100.0) / 100.0
        ),
        "inmempool": univalue_float(_round(bucket.in_mempool * 100.0) / 100.0),
        "leftmempool": univalue_float(_round(bucket.left_mempool * 100.0) / 100.0),
    }


def estimate_raw_fee(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `estimaterawfee`: each horizon tracking `conf_target`."""
    _check_types("estimaterawfee", params, ("threshold", "number", True))
    conf_target = _conf_target(node, params[0])
    threshold = 0.95
    if len(params) > 1 and params[1] is not None:
        threshold = get_real(params[1])
    if not 0 <= threshold <= 1:
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, "Invalid threshold")
    estimator = node.fee_estimator
    result: dict[str, Any] = {}
    for horizon in FeeEstimateHorizon:
        if conf_target > estimator.highest_target_tracked(horizon):
            continue
        buckets = EstimationResult()
        fee_per_k = estimator.estimate_raw_fee(conf_target, threshold, horizon, buckets)
        answer: dict[str, Any] = {}
        if fee_per_k:
            answer["feerate"] = btc_amount(fee_per_k)
            answer["decay"] = univalue_float(buckets.decay)
            answer["scale"] = buckets.scale
            answer["pass"] = _bucket(buckets.pass_bucket)
            # a start of -1 says every bucket passed
            if buckets.fail_bucket.start != -1:
                answer["fail"] = _bucket(buckets.fail_bucket)
        else:
            answer["decay"] = univalue_float(buckets.decay)
            answer["scale"] = buckets.scale
            answer["fail"] = _bucket(buckets.fail_bucket)
            answer["errors"] = [
                "Insufficient data or no feerate found which meets threshold"
            ]
        result[horizon.value] = answer
    return result
