# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`estimatesmartfee` and `estimaterawfee`, Core's `src/rpc/fees.cpp`.

Read at bitcoin/bitcoin@9be056a8a7, the v31.1 tag: the same arguments,
refusals, answers and units, from `Node.fee_estimator`.
"""

import string
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import RPCErrorCode

from btclib_node.fee_estimator import (
    EstimationResult,
    EstimatorBucket,
    FeeEstimateHorizon,
    llround,
)
from btclib_node.rpc.connection import RawJSON, btc_amount
from btclib_node.rpc.errors import RpcError, type_errors
from btclib_node.rpc.help import HELP_TEXT
from btclib_node.rpc.jsonrpc import get_real

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

__all__ = ["estimate_raw_fee", "estimate_smart_fee"]

# Core's `FeeModeMap`, in its order (`src/common/messages.cpp`)
_FEE_MODES = ("unset", "economical", "conservative")

_INT_MIN, _INT_MAX = -(2**31), 2**31 - 1

_UPPER = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_types(method: str, params: list[Any], second: tuple[str, str, bool]) -> None:
    """Refuse a call as `RPCHelpMan::HandleRequest` does, before its body.

    A missing `conf_target` is the method's help; a value of the wrong
    JSON type, `second` naming the optional second argument and its
    type, is every such argument in one `RPC_TYPE_ERROR`.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT[method])
    mismatches = []
    if not _is_number(params[0]):
        mismatches.append((1, "conf_target", params[0], "number"))
    name, expected, is_number = second
    if len(params) > 1 and params[1] is not None:
        value = params[1]
        matches = _is_number(value) if is_number else isinstance(value, str)
        if not matches:
            mismatches.append((2, name, value, expected))
    if mismatches:
        raise type_errors(*mismatches)


# `float | int`: a float is refused whatever its value
def _conf_target(node: Node, value: float | int) -> int:  # noqa: PYI041
    """Return Core's `ParseConfirmTarget` of `value`, a JSON number."""
    if isinstance(value, float) or not _INT_MIN <= value <= _INT_MAX:
        # `getInt<int>`, which reads no fraction or exponent
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    highest = node.fee_estimator.highest_target_tracked(FeeEstimateHorizon.LONG)
    if not 1 <= value <= highest:
        msg = f"Invalid conf_target, must be between 1 and {highest}"
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, msg)
    return value


def estimate_smart_fee(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `estimatesmartfee`.

    The estimate is raised to the mempool's minimum feerate and to
    `-minrelaytxfee`, as Core raises it. `unset` is `economical`.
    """
    _check_types("estimatesmartfee", params, ("estimate_mode", "string", False))
    conf_target = _conf_target(node, params[0])
    mode = params[1] if len(params) > 1 and params[1] is not None else "economical"
    # Core's `ToUpper`, which changes ASCII letters alone
    mode = mode.translate(_UPPER)
    if mode not in (name.upper() for name in _FEE_MODES):
        modes = '", "'.join(_FEE_MODES)
        msg = f'Invalid estimate_mode parameter, must be one of: "{modes}"'
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, msg)
    fee = node.fee_estimator.estimate_smart_fee(
        conf_target, conservative=mode == "CONSERVATIVE"
    )
    result: dict[str, Any] = {}
    if fee.fee_per_k:
        rate = max(
            fee.fee_per_k,
            node.mempool.get_min_fee_rate().sats_per_kvbyte,
            node.config.min_relay_feerate.sats_per_kvbyte,
        )
        result["feerate"] = btc_amount(rate)
    else:
        result["errors"] = ["Insufficient data or no feerate found"]
    result["blocks"] = fee.returned_target
    return result


def _number(value: float) -> RawJSON:
    """Write `value` as `UniValue::setFloat` does: 16 significant digits."""
    return RawJSON(format(value, ".16g"))


def _round(value: float) -> float:
    """Return C's `round`: the nearest integer, halves away from zero."""
    return float(llround(value))


def _bucket(bucket: EstimatorBucket) -> dict[str, RawJSON]:
    return {
        "startrange": _number(_round(bucket.start)),
        "endrange": _number(_round(bucket.end)),
        "withintarget": _number(_round(bucket.within_target * 100.0) / 100.0),
        "totalconfirmed": _number(_round(bucket.total_confirmed * 100.0) / 100.0),
        "inmempool": _number(_round(bucket.in_mempool * 100.0) / 100.0),
        "leftmempool": _number(_round(bucket.left_mempool * 100.0) / 100.0),
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
            answer["decay"] = _number(buckets.decay)
            answer["scale"] = buckets.scale
            answer["pass"] = _bucket(buckets.pass_bucket)
            # a start of -1 says every bucket passed
            if buckets.fail_bucket.start != -1:
                answer["fail"] = _bucket(buckets.fail_bucket)
        else:
            answer["decay"] = _number(buckets.decay)
            answer["scale"] = buckets.scale
            answer["fail"] = _bucket(buckets.fail_bucket)
            answer["errors"] = [
                "Insufficient data or no feerate found which meets threshold"
            ]
        result[horizon.value] = answer
    return result
