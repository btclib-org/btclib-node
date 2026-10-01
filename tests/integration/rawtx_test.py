# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A transaction's JSON, this node's answer against a real bitcoind's.

`TxToUniv` (`src/core_io.cpp`, at bitcoin/bitcoin@9be056a8a7) is what
`decoderawtransaction`, `getrawtransaction` and `getblock` render a
transaction with, so each of them is held to bitcoind's own answer for
the same bytes, key for key.

`asm` is left out of every comparison. This node renders it with
btclib's `script_to_dict`, which is not `ScriptToAsmStr`
(btclib-org/btclib#2461).
"""

import random
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.hashes import hash160, sha256
from btclib.script import script
from btclib.script.witness import Witness
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.p2p.address import peer_address
from tests import get_random_port, rpc_client, wait_until, wait_until_listening

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


def _descriptor(bitcoind: Bitcoind, script_pub_key: bytes) -> str:
    """Return bitcoind's own checksummed `raw(...)` descriptor for a script."""
    info = bitcoind.rpc("getdescriptorinfo", [f"raw({script_pub_key.hex()})"])
    return cast("dict[str, str]", info)["descriptor"]


def _spend(inputs: list[tuple[bytes, TxIn]], outputs: list[TxOut]) -> Tx:
    """Return a transaction of `inputs`, each a `(txid, TxIn)` built already."""
    return Tx(2, 0, [tx_in for _, tx_in in inputs], outputs, check_validity=False)


def _assert_getblock_agrees(client: Any, bitcoind: Bitcoind, tip: int) -> None:
    """Assert both nodes answer `getblock` 2 and 3 alike for every block."""
    for height in range(tip + 1):
        block_hash = bitcoind.rpc("getblockhash", [height])
        for verbosity in (2, 3):
            ours = client.call("getblock", [block_hash, verbosity])
            theirs = bitcoind.rpc("getblock", [block_hash, verbosity])
            ours, theirs = _without_asm(ours), _without_asm(theirs)
            # one double, spelled with 16 digits by one and 17 by the other
            assert ours.pop("difficulty") == pytest.approx(theirs.pop("difficulty"))
            assert _key_order(ours) == _key_order(theirs)
            assert ours == theirs, (height, verbosity)


def _assert_getrawtransaction_agrees(
    client: Any, bitcoind: Bitcoind, txs: list[Tx], block_hash: str
) -> None:
    """Assert both nodes answer `getrawtransaction` alike at each verbosity."""
    for tx in txs:
        for verbosity in (True, 1, 2, 3):
            args: list[object] = [tx.id.hex(), verbosity, block_hash]
            ours = _without_asm(client.call("getrawtransaction", args))
            theirs = _without_asm(bitcoind.rpc("getrawtransaction", args))
            assert _key_order(ours) == _key_order(theirs), verbosity
            assert ours == theirs, verbosity


def test_getblock_and_getrawtransaction_answer_as_bitcoind_does(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """A block with fees, witnesses and an in-block spend, at every verbosity.

    The first three blocks pay anyone-can-spend outputs of three kinds
    (P2WSH, P2SH and bare `OP_TRUE`), and a fourth spends them once
    they mature, with one of its own outputs spent again in the same
    block. `getblock` at verbosity 2 and 3 is then asked of both, for
    every block, and `getrawtransaction` at verbosity 1 to 3 with its
    block named, `asm` aside. Last the spending block is invalidated and
    replaced, and the same transactions are asked for in the block that
    is off the active chain.
    """
    op_true = b"\x51"
    p2wsh = bytes.fromhex("0020") + sha256(op_true)
    p2sh = bytes.fromhex("a914") + hash160(op_true) + bytes.fromhex("87")
    funding = [p2wsh, p2sh, op_true]
    burial = _descriptor(bitcoind, op_true)
    for script_pub_key in funding:
        bitcoind.rpc("generatetodescriptor", [1, _descriptor(bitcoind, script_pub_key)])
    bitcoind.rpc("generatetodescriptor", [100, burial])

    coinbases: list[tuple[bytes, int]] = []
    for height in (1, 2, 3):
        block = cast(
            "dict[str, Any]",
            bitcoind.rpc("getblock", [bitcoind.rpc("getblockhash", [height]), 2]),
        )
        coinbase = block["tx"][0]
        coinbases.append(
            (bytes.fromhex(coinbase["txid"]), int(coinbase["vout"][0]["value"] * 10**8))
        )
    witness = Witness([op_true])
    first = _spend(
        [
            (
                coinbases[0][0],
                TxIn(OutPoint(coinbases[0][0], 0), b"", 0xFFFFFFFD, witness),
            ),
            (
                coinbases[1][0],
                TxIn(OutPoint(coinbases[1][0], 0), script.serialize([op_true]), 0),
            ),
            (coinbases[2][0], TxIn(OutPoint(coinbases[2][0], 0), b"", 0xFFFFFFFF)),
        ],
        [TxOut(1000, p2wsh), *(TxOut(1000 + n, s) for n, s in enumerate(_SCRIPTS))],
    )
    total = sum(value for _, value in coinbases)
    first.vout[0] = TxOut(total - 12345 - sum(o.value for o in first.vout[1:]), p2wsh)
    second = _spend(
        [(first.id, TxIn(OutPoint(first.id, 0), b"", 0xFFFFFFFF, witness))],
        [TxOut(first.vout[0].value - 777, bytes.fromhex("0014") + hash160(_G))],
    )
    mined = bitcoind.rpc(
        "generateblock",
        [
            burial,
            [
                tx.serialize(include_witness=True, check_validity=False).hex()
                for tx in (first, second)
            ],
        ],
    )
    spending_block = cast("dict[str, str]", mined)["hash"]
    bitcoind.rpc("generatetodescriptor", [2, burial])
    tip = cast("int", bitcoind.rpc("getblockcount"))

    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
        )
    )
    node.start()
    try:
        wait_until_listening(node.p2p_manager)
        wait_until_listening(node.rpc_manager)
        node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))
        wait_until(lambda: len(node.chainstate.block_index.active_chain) == tip + 1)
        wait_until(lambda: node.status == NodeStatus.BlockSynced)
        client = rpc_client(node)

        _assert_getblock_agrees(client, bitcoind, tip)
        spending = cast("dict[str, Any]", bitcoind.rpc("getblock", [spending_block, 3]))
        assert all("fee" in tx for tx in spending["tx"][1:])
        assert all("prevout" in vin for tx in spending["tx"][1:] for vin in tx["vin"])

        txs = [first, second]
        _assert_getrawtransaction_agrees(client, bitcoind, txs, spending_block)
        on_chain = client.call("getrawtransaction", [first.id.hex(), 2, spending_block])
        assert on_chain["confirmations"] == 3
        assert {"time", "blocktime"} <= set(on_chain)

        bitcoind.rpc("invalidateblock", [spending_block])
        bitcoind.rpc("generatetodescriptor", [4, burial])
        wait_until(lambda: len(node.chainstate.block_index.active_chain) == tip + 2)
        off_chain = client.call(
            "getrawtransaction", [first.id.hex(), 2, spending_block]
        )
        assert off_chain["in_active_chain"] is False
        assert off_chain["confirmations"] == 0
        _assert_getrawtransaction_agrees(client, bitcoind, txs, spending_block)
    finally:
        node.stop()
        node.join()
