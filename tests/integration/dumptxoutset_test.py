# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`dumptxoutset`, this node's answer and file against a real bitcoind's.

bitcoind mines coinbases paying each kind of script the snapshot
compresses, and one transaction of several hundred outputs, and the node
syncs from it. Each dumps its UTXO set, at the tip and rolled back, and
the two files are the same bytes.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from bitcoin_core_rpc import RpcError
from btclib.hashes import hash160
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.p2p.address import peer_address
from tests import (
    get_random_port,
    rpc_client,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.integration.conftest import Bitcoind

_K = bytes.fromhex("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
_UNCOMPRESSED = bytes.fromhex(
    "0479be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    "483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8"
)
# `-G`, whose y is odd, and a point off the curve, which is not compressed
_FIELD = 2**256 - 2**32 - 977
_ODD = _UNCOMPRESSED[:33] + (
    _FIELD - int.from_bytes(_UNCOMPRESSED[33:], "big")
).to_bytes(32, "big")
_OFF_CURVE = b"\x04" + bytes(31) + b"\x05" + bytes(32)

_SCRIPTS = [
    b"\x76\xa9\x14" + hash160(_K) + b"\x88\xac",
    b"\xa9\x14" + hash160(_K) + b"\x87",
    b"\x21" + _K + b"\xac",
    b"\x41" + _UNCOMPRESSED + b"\xac",
    b"\x41" + _ODD + b"\xac",
    b"\x41" + _OFF_CURVE + b"\xac",
    b"\x00\x14" + hash160(_K),
    b"\x00\x20" + bytes(32),
    b"\x51\x20" + _K[1:],
    b"\x51\x21" + _K + b"\x51\xae",
    b"\x51",
    b"\x6a",
]
_AMOUNTS = [12_345_678, 1, 10_000_000, 5, 20_000, 1_000_000, 999_999, 1_100_000]


def _descriptor(bitcoind: Bitcoind, script_pub_key: bytes) -> str:
    """Return bitcoind's own checksummed `raw(...)` descriptor for a script."""
    info = bitcoind.rpc("getdescriptorinfo", [f"raw({script_pub_key.hex()})"])
    return cast("dict[str, str]", info)["descriptor"]


@pytest.fixture
def both(bitcoind: Bitcoind, tmp_path: Path) -> Iterator[tuple[Bitcoind, Node]]:
    """Give bitcoind and a node that has synced its chain.

    The chain: a coinbase paying each script, a hundred more paying
    `OP_TRUE`, and a block spending the first of those into many outputs.
    """
    op_true = _descriptor(bitcoind, b"\x51")
    for script in _SCRIPTS:
        bitcoind.rpc("generatetodescriptor", [1, _descriptor(bitcoind, script)])
    bitcoind.rpc("generatetodescriptor", [100, op_true])
    funding = cast(
        "dict[str, Any]",
        bitcoind.rpc(
            "getblock", [bitcoind.rpc("getblockhash", [len(_SCRIPTS) + 1]), 2]
        ),
    )
    coinbase = bytes.fromhex(funding["tx"][0]["txid"])
    outputs = [
        TxOut(
            _AMOUNTS[n % len(_AMOUNTS)],
            _SCRIPTS[n % len(_SCRIPTS)],
            check_validity=False,
        )
        for n in range(300)
    ]
    spend = Tx(
        2,
        0,
        [TxIn(OutPoint(coinbase, 0), b"", 0xFFFFFFFF)],
        outputs,
        check_validity=False,
    )
    bitcoind.rpc(
        "generateblock",
        [op_true, [spend.serialize(include_witness=True, check_validity=False).hex()]],
    )
    bitcoind.rpc("generatetodescriptor", [2, op_true])
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
        yield bitcoind, node
    finally:
        node.stop()
        node.join()


def _both(
    pair: tuple[Bitcoind, Node], params: list[Any]
) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    """Dump on each, and return both answers and both files."""
    bitcoind, node = pair
    client = rpc_client(node, 120)
    theirs = cast("dict[str, Any]", bitcoind.rpc("dumptxoutset", params))
    ours = cast("dict[str, Any]", client.call("dumptxoutset", params))
    return (
        ours,
        theirs,
        Path(ours["path"]).read_bytes(),
        Path(theirs["path"]).read_bytes(),
    )


def test_the_tip_is_dumped_as_bitcoind_dumps_it(both: tuple[Bitcoind, Node]) -> None:
    """The answer and the file are bitcoind's, byte for byte."""
    ours, theirs, our_file, their_file = _both(both, ["tip.dat", "latest"])
    assert list(ours) == list(theirs)
    assert {k: v for k, v in ours.items() if k != "path"} == {
        k: v for k, v in theirs.items() if k != "path"
    }
    assert our_file == their_file
    assert ours["coins_written"] > 300


@pytest.mark.parametrize("target", ["height", "hash", "type"])
def test_a_rollback_is_dumped_as_bitcoind_dumps_it(
    both: tuple[Bitcoind, Node], target: str
) -> None:
    """Rolled back to the many-output block, and to an earlier one."""
    bitcoind, node = both
    for number in (len(_SCRIPTS) + 1, 7):
        base = cast("str", bitcoind.rpc("getblockhash", [number]))
        rollback: object = number if target == "height" else base
        ours, theirs, our_file, their_file = _both(
            both,
            [
                f"back{number}.dat",
                "" if target != "type" else "rollback",
                {"rollback": rollback},
            ],
        )
        assert ours["base_height"] == number
        assert {k: v for k, v in ours.items() if k != "path"} == {
            k: v for k, v in theirs.items() if k != "path"
        }
        assert our_file == their_file
        wait_until(lambda: _at_tip(bitcoind, node), timeout=120)


def _at_tip(bitcoind: Bitcoind, node: Node) -> bool:
    """Say whether the node's tip is bitcoind's again."""
    return bool(
        rpc_client(node).call("getbestblockhash") == bitcoind.rpc("getbestblockhash")
    )


_REFUSED: list[list[Any]] = [
    [],
    [1],
    [None],
    ["a.dat", 1],
    ["a.dat"],
    ["a.dat", "bogus"],
    ["a.dat", "latest", 5],
    ["a.dat", "latest", {"rollback": 3}],
    ["a.dat", "", {"rollback": 10**6}],
    ["a.dat", "", {"rollback": -1}],
    ["a.dat", "", {"rollback": "zz"}],
    ["a.dat", "", {"rollback": "zz" * 32}],
    ["a.dat", "", {"rollback": "00" * 32}],
    ["a.dat", "", {"rollback": True}],
    ["a.dat", "", {"rollback": None}],
    ["a.dat", "", {"rollback": 1.5}],
    ["a.dat", "", {"rollback": [1]}],
    ["a.dat", "rollback"],
]


def test_refusals_are_bitcoind_s(both: tuple[Bitcoind, Node]) -> None:
    """Each has bitcoind's code and message."""
    bitcoind, node = both
    client = rpc_client(node)
    for params in _REFUSED:
        with pytest.raises(RpcError) as theirs:
            bitcoind.rpc("dumptxoutset", params)
        with pytest.raises(RpcError) as ours:
            client.call("dumptxoutset", params)
        assert ours.value.code == theirs.value.code, params
        assert (
            str(ours.value.args[0]).split(": ", 1)[1]
            == str(theirs.value.args[0]).split(": ", 1)[1]
        ), params


def test_an_existing_path_and_an_unopenable_one_are_refused(
    both: tuple[Bitcoind, Node], tmp_path: Path
) -> None:
    """The paths in the messages are each side's own."""
    bitcoind, node = both
    client = rpc_client(node)
    for call in (client.call, bitcoind.rpc):
        target = str(tmp_path / f"x-{id(call)}.dat")
        call("dumptxoutset", [target, "latest"])
        with pytest.raises(RpcError) as err:
            call("dumptxoutset", [target, "latest"])
        assert err.value.code == -8
        assert str(err.value.args[0]).endswith(
            f"{target} already exists. If you are sure this is what you want, "
            "move it out of the way first"
        )
        missing = str(tmp_path / "none" / "x.dat")
        with pytest.raises(RpcError) as err:
            call("dumptxoutset", [missing, "latest"])
        assert str(err.value.args[0]).endswith(
            f"Couldn't open file {missing}.incomplete for writing."
        )


_NAMED: list[dict[str, Any]] = [
    {"path": "n1.dat", "rollback": 5},
    {"path": "n2.dat", "type": "rollback", "rollback": 5},
    {"path": "n3.dat", "options": {"rollback": 5}},
    {"path": "n4.dat", "options": {}, "rollback": 5},
    {"path": "n5.dat", "type": "latest", "rollback": 5},
    {"rollback": 5},
    {"args": ["n6.dat"], "rollback": 5},
    {"args": ["n7.dat", "", {}], "rollback": 5},
    {"path": "n8.dat", "bogus": 5},
]


def test_named_arguments_are_bitcoind_s(both: tuple[Bitcoind, Node]) -> None:
    """The named-only `rollback` is read as bitcoind reads it."""
    bitcoind, node = both
    client = rpc_client(node, 120)
    their_client = bitcoind._client
    for params in _NAMED:
        _, theirs = their_client.call_raw("dumptxoutset", params, request_timeout=120)
        _, ours = client.call_raw("dumptxoutset", params, request_timeout=120)
        our_error, their_error = ours.get("error"), theirs.get("error")
        assert (our_error or {}).get("code") == (their_error or {}).get("code"), params
        if their_error:
            assert our_error["message"] == their_error["message"], params
        else:
            assert ours["result"]["base_height"] == theirs["result"]["base_height"]
