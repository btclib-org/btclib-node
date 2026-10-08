# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What `decode` refuses as JSON, and what `get_real` refuses as a double.

Overflow and the constants are held to bitcoind v31.1.0 by
`tests/integration/json_double_test.py`. Underflow follows Core's Linux
release, as `get_real`'s docstring says why.
"""

import pytest
from bitcoin_core_rpc import RPCErrorCode

from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.jsonrpc import decode, get_real


@pytest.mark.parametrize(
    "text",
    [
        "1e400",
        "-1e400",
        "1.7976931348623159e308",
        "1e99999999999",
        "1" + "0" * 309,
        "-1" + "0" * 400,
    ],
)
def test_a_number_no_double_holds_is_refused(text: str) -> None:
    """Overflow is out of range, a whole number too."""
    (value,) = decode(f"[{text}]".encode())
    with pytest.raises(RpcError) as raised:
        get_real(value)
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "JSON double out of range"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.0", 0.0),
        ("-0.0", 0.0),
        ("0e999999", 0.0),
        ("1e-400", 0.0),
        ("-1e-400", 0.0),
        ("4.9e-324", 5e-324),
        ("1e-99999999999", 0.0),
        ("2.2250738585072014e-308", 2.2250738585072014e-308),
        ("1.7976931348623157e308", 1.7976931348623157e308),
        ("1.7976931348623158e308", 1.7976931348623157e308),
        ("1" + "0" * 308, 1e308),
        ("0", 0.0),
        ("5", 5.0),
        ("1e-5", 1e-5),
        ("1.5", 1.5),
    ],
)
def test_a_number_a_double_holds_is_read_as_is(text: str, expected: float) -> None:
    """Underflow reads as zero or a subnormal, as Core on Linux reads it."""
    (value,) = decode(f"[{text}]".encode())
    assert get_real(value) == expected


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("shape", ["{}", '[{{"a":{}}}]', '{{"params":[{}]}}'])
def test_a_constant_json_has_not_is_a_parse_error(text: str, shape: str) -> None:
    """Core's `UniValue::read` refuses them wherever they stand."""
    with pytest.raises(ValueError, match=text):
        decode(shape.format(text).encode())
