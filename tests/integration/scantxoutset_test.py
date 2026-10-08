# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`scantxoutset`, this node's answer against a real bitcoind's.

bitcoind mines a coinbase to each script under test and the node syncs
from it, so both hold one UTXO set. A scan of it is held to bitcoind's own
answer for the same call, key for key, `desc` included. A refusal is held
to its code, and to its message where it is not the descriptor parser's,
which words what it finds wrong in its own way.
"""

from typing import TYPE_CHECKING, Any, cast

import pytest
from bitcoin_core_rpc import RpcError
from btclib_wallet.descriptors import multipath_descriptors, parse

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
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

_K = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
_K2 = "02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
_X = _K[2:]
_X2 = _K2[2:]
_UNCOMPRESSED = (
    "0479be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    "483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8"
)
_TPRV = (
    "tprv8ZgxMBicQKsPeDgjzdC36fs6bMjGApWDNLR9erAXMs5skhMv36j9MV5ecvfavji5kh"
    "qjWaWSFhN3YcCUUdiKH6isR4Pwy3U5y5egddBr16m"
)
_TPUB = (
    "tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7we1vwNCpn8tGbBcgfVYjXyhWo4E1xkh56hjod1"
    "RhGjxbaTLV3X4FyWuejifB9j"
    "usQ46QzG87VKp"
)
# a mainnet key, which a regtest node refuses
_XPUB = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoCu1R"
    "upje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"
)
_ADDRESS = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"

# btclib-wallet's review cases: a multisig whose key at one index is the
# other's at the next, so its origin is the first index's, and a leaf at
# two depths of a taproot tree
_RANGED_MULTI = f"wsh(multi(1,[aaaaaaaa/5]{_TPUB}/0/*,[bbbbbbbb/7]{_TPUB}/0/1))"
_TWO_DEPTHS = f"tr({_TPUB}/9/1,{{pk({_TPUB}/0/1),{{pk({_TPUB}/0/1),pk({_TPUB}/1/1)}}}})"
# each path's key is the other's under another origin: the first path's wins
_CROSSED = f"wsh(multi(1,[aaaaaaaa]{_TPUB}/<0;1>/0,[bbbbbbbb]{_TPUB}/<1;0>/0))"
_RANGED_MULTIPATH = f"pkh([abcdef01/1h]{_TPUB}/<0;1>/*)"
_RANGED_TREE = f"tr([abcdef01]{_TPUB}/9/*,pk([abcdef02]{_TPUB}/0/*))"


def _scripts(descriptor: str, *indexes: int) -> list[str]:
    """Return each path's script at each of `indexes`, as `raw()`."""
    return [
        f"raw({script.script.hex()})"
        for one in multipath_descriptors(descriptor)
        for index in indexes
        for script in parse(one, "regtest").script_pub_keys(index)
    ]


# each is mined to, and scanned for
_MINED = [
    f"pkh({_K})",
    f"pk({_K})",
    f"wpkh({_K})",
    f"sh(wpkh({_K}))",
    f"pkh({_UNCOMPRESSED})",
    f"sh(multi(1,{_K},{_K2}))",
    f"wsh(sortedmulti(1,{_K2},{_K}))",
    f"wsh(multi(2,{_K2},{_K}))",
    f"tr({_X})",
    f"rawtr({_X})",
    f"tr({_X},pk({_X2}))",
    f"tr({_X},sortedmulti_a(1,{_X2},{_X}))",
    f"tr(musig({_K},{_K2}))",
    f"wsh(pk({_K}))",
    f"wsh(and_v(v:pk({_K}),older(10)))",
    f"pkh({_TPRV}/0h/1)",
    f"wpkh([abcdef01/1h/2]{_TPUB}/0/5)",
    f"addr({_ADDRESS})",
    "raw(51)",
    *_scripts(_RANGED_MULTI, 0, 1),
    _TWO_DEPTHS,
    *_scripts(_CROSSED, 0),
    *_scripts(_RANGED_MULTIPATH, 2),
    *_scripts(_RANGED_TREE, 1),
]

_SCANS: list[list[Any]] = [
    [],
    *([scan_object] for scan_object in _MINED),
    [f"combo({_K})"],
    [f"combo({_UNCOMPRESSED})"],
    [f"pkh({_K})", f"wpkh({_K})", "raw(51)", f"addr({_ADDRESS})"],
    [{"desc": f"pkh({_TPRV}/0h/*)", "range": 3}],
    [{"desc": f"pkh({_TPRV}/0h/*)", "range": [1, 1]}],
    [{"desc": f"pkh({_TPRV}/0h/*)", "range": 0}],
    [{"desc": f"wpkh([abcdef01/1h/2]{_TPUB}/0/*)", "range": [5, 5]}],
    [{"desc": f"wpkh({_TPUB}/0/*)", "range": 1500}],
    [f"pkh({_TPUB}/<0;1>/5)", f"wpkh([abcdef01/1h/2]{_TPUB}/<0;1>/5)"],
    [{"desc": "raw(51)", "range": None}],
    [{"desc": f"combo({_TPRV}/0h/*)", "range": [2**31 - 1, 2**31 - 1]}],
]

# each finds a coin, whose `desc` depends on the provider it is inferred with
_INFERRED: list[list[Any]] = [
    [{"desc": _RANGED_MULTI, "range": [0, 1]}],
    [{"desc": _RANGED_MULTI, "range": [1, 1]}],
    [_TWO_DEPTHS],
    [_CROSSED],
    [{"desc": _RANGED_MULTIPATH, "range": [1, 3]}],
    [{"desc": _RANGED_TREE, "range": [0, 2]}],
    [f"wpkh([abcdef01/1h/2]{_TPUB}/0/5)", f"wpkh({_TPUB}/0/5)"],
    [f"wpkh({_TPUB}/0/5)", f"wpkh([abcdef01/1h/2]{_TPUB}/0/5)"],
]

# a request each side refuses with the same code; the message too where the
# descriptor parser has no say
_REFUSED: list[tuple[list[Any], bool]] = [
    ([], True),
    ([None], True),
    ([1, "x"], True),
    (["bogus"], True),
    (["status", 5], True),
    (["start"], True),
    (["start", None], True),
    (["start", "x"], True),
    (["start", [1]], True),
    (["start", [None]], True),
    (["start", [{}]], True),
    (["start", [{"desc": 1}]], True),
    (["start", [{"desc": "raw(51)", "range": -1}]], True),
    (["start", [{"desc": "raw(51)", "range": [-1, 10]}]], True),
    (["start", [{"desc": "raw(51)", "range": [2, 1]}]], True),
    (["start", [{"desc": "raw(51)", "range": [0, 1000000]}]], True),
    (["start", [{"desc": "raw(51)", "range": 2**31}]], True),
    (["start", [{"desc": "raw(51)", "range": "x"}]], True),
    (["start", [{"desc": "raw(51)", "range": [1]}]], True),
    (["start", [{"desc": "raw(51)", "range": True}]], True),
    (["start", [{"desc": "raw(51)", "range": 1.5}]], True),
    (["start", [{"desc": "raw(51)", "range": 2**63}]], True),
    (["start", [{"desc": f"pkh({_TPUB}/0h/*)", "range": 3}]], True),
    (["start", [f"pkh({_TPUB}/0h/1)"]], True),
    (["start", ["raw(51)"], 7], True),
    (["start", ["bogus"]], False),
    (["start", [f"pkh({_K})#00000000"]], False),
    (["start", [f"pkh({_K[:-2]})"]], False),
    (["start", ["raw(zz)"]], False),
    (["start", [f"pkh({_TPUB}/<0;1>/<2;3>)"]], False),
    (["start", [f"pkh({_XPUB}/0/*)"]], False),
]


def _refusal(call: Any, params: list[object]) -> tuple[int, str] | Any:
    """Return the code and message `call` is refused with, else its answer."""
    try:
        return call("scantxoutset", params)
    except RpcError as err:
        return err.code, str(err.args[0]).split(": ", 1)[1]


@pytest.fixture
def both(bitcoind: Bitcoind, tmp_path: Path) -> Iterator[tuple[Bitcoind, Node]]:
    """Give bitcoind and a node that has synced its chain, `_MINED` paid."""
    for descriptor in _MINED:
        bitcoind.rpc("generatetodescriptor", [1, descriptor])
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


def test_a_scan_answers_as_bitcoind_does(both: tuple[Bitcoind, Node]) -> None:
    """Every scan holds bitcoind's own keys, in its order, and its values."""
    bitcoind, node = both
    client = rpc_client(node, 60)
    for scan_objects in _SCANS + _INFERRED:
        theirs = cast(
            "dict[str, Any]", bitcoind.rpc("scantxoutset", ["start", scan_objects])
        )
        ours = cast(
            "dict[str, Any]", client.call("scantxoutset", ["start", scan_objects])
        )
        assert ours == theirs, scan_objects
        assert theirs["unspents"] or scan_objects not in _INFERRED
        assert list(ours) == list(theirs)
        assert [list(u) for u in ours["unspents"]] == [
            list(u) for u in theirs["unspents"]
        ]
    assert any(
        client.call("scantxoutset", ["start", [scan_objects]])["unspents"]
        for scan_objects in _MINED
    )


def test_status_and_abort_answer_as_bitcoind_does(both: tuple[Bitcoind, Node]) -> None:
    """With no scan running, `status` is null and `abort` is false."""
    bitcoind, node = both
    client = rpc_client(node)
    calls: list[list[Any]] = [["status"], ["abort"], ["status", None], ["abort", None]]
    for params in calls:
        assert client.call("scantxoutset", params) == bitcoind.rpc(
            "scantxoutset", params
        )


def test_refusals_are_bitcoind_s(both: tuple[Bitcoind, Node]) -> None:
    """Each refusal has bitcoind's code, and its message but a parser's."""
    bitcoind, node = both
    client = rpc_client(node)
    for params, same_message in _REFUSED:
        theirs = _refusal(bitcoind.rpc, params)
        ours = _refusal(client.call, params)
        assert isinstance(theirs, tuple), params
        assert isinstance(ours, tuple), params
        assert ours[0] == theirs[0], params
        if same_message:
            assert ours[1] == theirs[1], params


def test_help_is_bitcoind_s(both: tuple[Bitcoind, Node]) -> None:
    """`help scantxoutset` is bitcoind's own text."""
    bitcoind, node = both
    client = rpc_client(node)
    ours = client.call("help", ["scantxoutset"])
    assert ours == cast("str", bitcoind.rpc("help", ["scantxoutset"])).rstrip("\n")
