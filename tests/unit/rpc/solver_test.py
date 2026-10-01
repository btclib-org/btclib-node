# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.rpc.solver`: Core's `Solver` and its type names.

Each expected type is the one a bitcoind v31.1 answers for the script:
`tests/integration/rawtx_test.py` holds the same classification to a real
one, over these scripts and random ones near them.
"""

import pytest

from btclib_node.rpc.solver import solver

_KEY = bytes.fromhex(
    "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
)
_UNCOMPRESSED = b"\x04" + bytes(64)
_HASH = bytes(range(20))


def _push(data: bytes) -> bytes:
    return bytes([len(data)]) + data


def _script_id(value: object) -> str | None:
    # The default id is the script's repr, 280 kB for the largest, and
    # Windows refuses an environment variable over 32767 characters, which
    # is where pytest puts the id (PYTEST_CURRENT_TEST).
    return f"{value[:8].hex()}-{len(value)}" if isinstance(value, bytes) else None


def _multisig(m: bytes, keys: list[bytes], n: bytes) -> bytes:
    return m + b"".join(_push(key) for key in keys) + n + b"\xae"


@pytest.mark.parametrize(
    ("script", "script_type"),
    [
        (b"\xa9\x14" + _HASH + b"\x87", "scripthash"),
        (b"\x00\x14" + _HASH, "witness_v0_keyhash"),
        (b"\x00\x20" + bytes(32), "witness_v0_scripthash"),
        (b"\x51\x20" + bytes(32), "witness_v1_taproot"),
        (b"\x51\x02\x4e\x73", "anchor"),
        (b"\x52\x02\xab\xcd", "witness_unknown"),
        (b"\x51\x02\xab\xcd", "witness_unknown"),
        (b"\x51\x21" + bytes(33), "witness_unknown"),
        (b"\x00\x15" + bytes(21), "nonstandard"),
        (b"\x00\x00", "nonstandard"),
        (b"\x6a", "nulldata"),
        (b"\x6a\x02hi", "nulldata"),
        (b"\x6a\x51\x60", "nulldata"),
        (b"\x6a\x4d\x01\x01" + bytes(257), "nulldata"),
        (b"\x6a\xac", "nonstandard"),
        (b"\x6a\x05ab", "nonstandard"),
        (_push(_KEY) + b"\xac", "pubkey"),
        (_push(_UNCOMPRESSED) + b"\xac", "pubkey"),
        (_push(b"\x06" + bytes(64)) + b"\xac", "pubkey"),
        (_push(b"\x07" + bytes(64)) + b"\xac", "pubkey"),
        (_push(b"\x05" + bytes(32)) + b"\xac", "nonstandard"),
        (_push(b"") + b"\xac", "nonstandard"),
        (b"", "nonstandard"),
        (b"\x76\xa9\x14" + _HASH + b"\x88\xac", "pubkeyhash"),
        (b"\x76\xa9\x14" + _HASH + b"\x88\xad", "nonstandard"),
        (_multisig(b"\x51", [_KEY], b"\x51"), "multisig"),
        (_multisig(b"\x52", [_KEY, _UNCOMPRESSED], b"\x52"), "multisig"),
        (_multisig(b"\x51", [_KEY], b"\x52"), "nonstandard"),
        (_multisig(b"\x52", [_KEY], b"\x51"), "nonstandard"),
        (_multisig(b"\x00", [_KEY], b"\x51"), "nonstandard"),
        (_multisig(b"\x51", [_KEY, b"\x05" + bytes(32)], b"\x52"), "nonstandard"),
        (_multisig(b"\x51", [], b"\x50"), "nonstandard"),
        (_multisig(b"\x51", [_KEY], b"\x51")[:-1] + b"\xad", "nonstandard"),
        (_multisig(b"\x51", [_KEY], b"\x51") + b"\xae", "nonstandard"),
        (b"\xae", "nonstandard"),
        (b"\x4c\xae", "nonstandard"),
        (b"\x51" + _push(_KEY) + b"\x05ab\xae", "nonstandard"),
        # m and n as pushes: the minimal push of a number above 16 counts,
        # a push of one that is below it, or padded, does not
        (
            _multisig(b"\x01\x11", [_KEY] * 17, b"\x01\x11"),
            "multisig",
        ),
        (_multisig(b"\x01\x01", [_KEY], b"\x51"), "nonstandard"),
        (_multisig(b"\x01\x81", [_KEY], b"\x51"), "nonstandard"),
        (_multisig(b"\x01\x80", [_KEY], b"\x51"), "nonstandard"),
        (_multisig(b"\x01\x00", [_KEY], b"\x51"), "nonstandard"),
        (_multisig(b"\x02\xff\x00", [_KEY], b"\x51"), "nonstandard"),
        (_multisig(b"\x4c\x00", [_KEY], b"\x51"), "nonstandard"),
        (_multisig(b"\x4c\x02\x11\x00", [_KEY], b"\x51"), "nonstandard"),
        (_multisig(b"\x4c\x50" + bytes(80), [_KEY], b"\x51"), "nonstandard"),
        (_multisig(b"\x4d\x2c\x01" + bytes(300), [_KEY], b"\x51"), "nonstandard"),
        (
            _multisig(b"\x4e\x70\x11\x01\x00" + bytes(70000), [_KEY], b"\x51"),
            "nonstandard",
        ),
        (_multisig(b"\xac", [_KEY], b"\x51"), "nonstandard"),
    ],
    ids=_script_id,
)
def test_solver_names_the_type_core_does(script: bytes, script_type: str) -> None:
    """`solver` answers `GetTxnOutputType`'s name for `script`."""
    assert solver(script)[0] == script_type


def test_solver_answers_the_solutions_core_fills() -> None:
    """The solutions are the hash, the key or the program `Solver` fills."""
    assert solver(b"\xa9\x14" + _HASH + b"\x87")[1] == [_HASH]
    assert solver(b"\x76\xa9\x14" + _HASH + b"\x88\xac")[1] == [_HASH]
    assert solver(b"\x00\x14" + _HASH)[1] == [_HASH]
    assert solver(_push(_KEY) + b"\xac")[1] == [_KEY]
    assert solver(b"\x52\x02\xab\xcd")[1] == [b"\x02", b"\xab\xcd"]
    assert solver(_multisig(b"\x52", [_KEY, _UNCOMPRESSED], b"\x52"))[1] == [
        b"\x02",
        _KEY,
        _UNCOMPRESSED,
        b"\x02",
    ]
    assert solver(b"\x6a")[1] == []
