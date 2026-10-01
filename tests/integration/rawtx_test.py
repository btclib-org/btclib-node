# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A transaction's JSON, this node's answer against a real bitcoind's.

`TxToUniv` (`src/core_io.cpp`, at bitcoin/bitcoin@9be056a8a7) is what
`decoderawtransaction`, `getrawtransaction` and `getblock` render a
transaction with, so each of them is held to bitcoind's own answer for
the same bytes, key for key.

`asm` is left out of every comparison. This node renders it with
btclib's `script_to_dict`, which is not `ScriptToAsmStr` and says so in its
own docstring.
"""

import random
from typing import TYPE_CHECKING, Any

from btclib.hashes import hash160, sha256
from btclib.script import script
from btclib.script.witness import Witness
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.config import Config
from tests import get_random_port, rpc_client, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

_G = bytes.fromhex("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
_G2 = bytes.fromhex(
    "02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
)
# the x coordinate of `_G` is on the curve, `_X_OFF_CURVE` is not
_X = _G[1:]
_X_OFF_CURVE = bytes.fromhex("00" * 31 + "05")
# secp256k1's field prime: `_P + 1` is no coordinate, though it reads as 1
_P = 2**256 - 2**32 - 977

_SCRIPTS = [
    # the standard types
    bytes.fromhex("76a914") + hash160(_G) + bytes.fromhex("88ac"),
    bytes.fromhex("a914") + hash160(_G) + bytes.fromhex("87"),
    bytes.fromhex("0014") + hash160(_G),
    bytes.fromhex("0020") + sha256(b"\x51"),
    bytes.fromhex("5120") + _X,
    bytes.fromhex("21") + _G + bytes.fromhex("ac"),
    bytes.fromhex("5121") + _G + bytes.fromhex("51ae"),
    bytes.fromhex("5221") + _G + bytes.fromhex("21") + _G2 + bytes.fromhex("52ae"),
    bytes.fromhex("6a026869"),
    bytes.fromhex("51024e73"),
    bytes.fromhex("5202abcd"),
    # what only `Solver` tells from btclib's own types
    bytes.fromhex("5120") + _X_OFF_CURVE,
    bytes.fromhex("5120") + (_P + 1).to_bytes(32, "big"),
    bytes.fromhex("6a4d0101") + bytes(257),
    bytes.fromhex("6a") + b"\x00" * 3,
    bytes.fromhex("0015") + bytes(21),
    bytes.fromhex("0000"),
    bytes.fromhex("21") + b"\x05" + _X + bytes.fromhex("ac"),
    bytes.fromhex("21") + b"\x06" + _X + bytes.fromhex("ac"),
    bytes.fromhex("41") + b"\x07" + _X + _X + bytes.fromhex("ac"),
    bytes.fromhex("5121") + b"\x07" + _X + bytes.fromhex("51ae"),
    bytes.fromhex("5121") + _G + bytes.fromhex("52ae"),
    bytes.fromhex("5221") + _G + bytes.fromhex("51ae"),
    bytes.fromhex("5121") + _G + bytes.fromhex("0101ae"),
    bytes.fromhex("51"),
    bytes.fromhex("ff"),
    b"",
]


def _random_scripts(count: int) -> list[bytes]:
    """Return `count` scripts near the templates, from a fixed seed."""
    rng = random.Random(1440)
    out: list[bytes] = []
    for _ in range(count):
        kind = rng.randrange(4)
        if kind == 0:
            out.append(rng.randbytes(rng.randrange(60)))
        elif kind == 1:
            data = bytearray(rng.choice(_SCRIPTS))
            if data:
                data[rng.randrange(len(data))] = rng.randrange(256)
            out.append(bytes(data[: rng.randrange(len(data) + 1)]))
        elif kind == 2:
            keys = [
                bytes([rng.choice([2, 3, 4, 5, 6, 7])])
                + rng.randbytes(rng.choice([32, 64, 31]))
                for _ in range(rng.randrange(1, 4))
            ]
            body = b"".join(bytes([len(key)]) + key for key in keys)
            out.append(
                bytes([0x50 + rng.randrange(0, 5)])
                + body
                + bytes([0x50 + rng.randrange(0, 5), 0xAE])
            )
        else:
            out.append(bytes([0x6A]) + rng.randbytes(rng.randrange(0, 100)))
    return out


def _without_asm(value: Any) -> Any:
    """Return `value` with every `asm` key dropped, at any depth."""
    if isinstance(value, dict):
        return {k: _without_asm(v) for k, v in value.items() if k != "asm"}
    if isinstance(value, list):
        return [_without_asm(item) for item in value]
    return value


def _key_order(value: Any) -> Any:
    """Return `value` with each object as its keys in order, at any depth.

    A `dict` compares equal whatever its key order, and Core's is what a
    client that reads the JSON as text sees.
    """
    if isinstance(value, dict):
        return [(key, _key_order(item)) for key, item in value.items()]
    if isinstance(value, list):
        return [_key_order(item) for item in value]
    return None


def _a_node(tmp_path: Path) -> Node:
    """Start a regtest node that listens for RPC only."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
        )
    )
    node.start()
    wait_until_listening(node.rpc_manager)
    return node


def _transactions() -> list[Tx]:
    """Return transactions whose inputs and outputs cover every shape."""
    outs = [TxOut(1000 + n, script_pub_key=s) for n, s in enumerate(_SCRIPTS)]
    outs += [TxOut(n, script_pub_key=s) for n, s in enumerate(_random_scripts(400))]
    ordinary = TxIn(
        prev_out=OutPoint(b"\x11" * 32, 3),
        script_sig=script.serialize([b"\x30" * 71, _G]),
        sequence=0xFFFFFFFD,
        script_witness=Witness([b"\x01", b"", b"\x02" * 33]),
    )
    plain = TxIn(OutPoint(b"\x22" * 32, 0), b"", 0)
    coinbase = TxIn(OutPoint(), b"\x01\x65\x04abcd", 0xFFFFFFFF, Witness([bytes(32)]))
    return [
        Tx(2, 500_000_000, [ordinary, plain], outs, check_validity=False),
        Tx(1, 0, [plain], outs[:3], check_validity=False),
        Tx(1, 0, [coinbase], outs[:2], check_validity=False),
        Tx(1, 0, [coinbase, plain], outs[:2], check_validity=False),
        Tx(
            1,
            0,
            [plain],
            [TxOut(-1, _SCRIPTS[0], check_validity=False)],
            check_validity=False,
        ),
    ]


def test_decoderawtransaction_answers_as_bitcoind_does(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Every `vin` and `vout` shape decodes to bitcoind's own JSON, minus `asm`.

    The output scripts are the standard types, the cases btclib's own
    classification and `Solver` part on, and random ones near them.
    """
    node = _a_node(tmp_path)
    try:
        client = rpc_client(node)
        for tx in _transactions():
            raw = tx.serialize(include_witness=True, check_validity=False).hex()
            ours = client.call("decoderawtransaction", [raw])
            theirs = bitcoind.rpc("decoderawtransaction", [raw])
            ours, theirs = _without_asm(ours), _without_asm(theirs)
            assert _key_order(ours) == _key_order(theirs)
            assert ours["vin"] == theirs["vin"]
            assert len(ours["vout"]) == len(theirs["vout"])
            for ours_out, theirs_out in zip(ours["vout"], theirs["vout"], strict=True):
                assert ours_out == theirs_out, tx.vout[
                    ours_out["n"]
                ].script_pub_key.script.hex()
            assert ours == theirs
    finally:
        node.stop()
        node.join()
