# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What `scantxoutset` answers, called as `rpc.main` calls it.

The coins are coinbases the node mines to the scripts under test, on a
regtest node built and driven in this thread. The shapes, the refusals
and the descriptors are what `bitcoind` answers for the same call
(`tests/integration/scantxoutset_test.py` asks it).

`bitcoind` here is the release `integration-bitcoind.yml` pins.
"""

from collections.abc import Generator
from typing import TYPE_CHECKING, Any, cast

import pytest
from bip32_descriptors.descriptors import (
    Provider,
    add_checksum,
    multipath_descriptors,
    parse,
    strip_checksum,
)
from btclib.b58 import wif_from_prv_key
from btclib.exceptions import BTClibException, BTClibValueError
from btclib.hashes import hash160
from btclib.script.script_pub_key import ScriptPubKey
from ecc.exceptions import EccException

from btclib_node.rpc import utxo_set
from btclib_node.rpc.callbacks import arg_names, callbacks
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT
from btclib_node.rpc.jsonrpc import JsonRpcRequest
from btclib_node.rpc.main import _execute
from btclib_node.rpc.mining import generate_block
from btclib_node.rpc.utxo_set import scan_tx_out_set
from tests import finish
from tests.secret_free import assert_secret_free
from tests.unit.rpc import LEAK_HEX, LEAK_WIF

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

CONN = cast("RpcConnection", None)

KEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
KEY2 = "02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
X_ONLY = KEY[2:]
X_ONLY2 = KEY2[2:]
# the fingerprints `bitcoind` infers for these keys
FINGERPRINT = "751e76e8"
FINGERPRINT2 = "06afd46b"
# BIP32 test vector 1's master key on a test chain, and the xpub of its `/0h`
TPRV = (
    "tprv8ZgxMBicQKsPeDgjzdC36fs6bMjGApWDNLR9erAXMs5skhMv36j9MV5ecvfavji5kh"
    "qjWaWSFhN3YcCUUdiKH6isR4Pwy3U5y5egddBr16m"
)
TPUB = (
    "tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7we1vwNCpn8tGbBcgfVYjXyhWo4E1xkh56hjod1"
    "RhGjxbaTLV3X4FyWuejifB9j"
    "usQ46QzG87VKp"
)
XPUB = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoCu1R"
    "upje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"
)
SUBSIDY = "50.00000000"


@pytest.fixture
def node(regtest_node: Callable[[], Node]) -> Node:
    """Give one regtest node, built fresh for the test."""
    return regtest_node()


def mine_to(node: Node, descriptor: str, blocks: int = 1) -> None:
    """Mine `blocks` blocks, each paying the script of `descriptor`."""
    for _ in range(blocks):
        finish(generate_block(node, CONN, [descriptor, []]))


def scan(node: Node, *scan_objects: object) -> dict[str, Any]:
    """Run `scantxoutset start` to its answer."""
    result = scan_tx_out_set(node, CONN, ["start", list(scan_objects)])
    assert isinstance(result, Generator)
    return finish(result)


def refusal(node: Node, *params: object) -> tuple[int, str]:
    """Return the code and message `scantxoutset` is refused with."""

    def run() -> object:
        result = scan_tx_out_set(node, CONN, list(params))
        return finish(result) if isinstance(result, Generator) else result

    with pytest.raises(RpcError) as caught:
        run()
    return int(caught.value.code), caught.value.message


def test_it_is_served_as_core_names_it() -> None:
    """The method is dispatched, named, helped and listed under Blockchain."""
    assert callbacks["scantxoutset"] is scan_tx_out_set
    assert arg_names["scantxoutset"] == ("action", "scanobjects")
    assert HELP_TEXT["scantxoutset"].startswith('scantxoutset "action" (')
    assert CATEGORY["scantxoutset"] == "Blockchain"


def test_a_coin_is_found_with_core_s_answer(node: Node) -> None:
    """The answer has Core's keys in Core's order, and the coin's own fields."""
    mine_to(node, f"pkh({KEY})", 2)
    result = scan(node, f"pkh({KEY})")
    chain = node.chainstate.block_index.active_chain
    assert list(result) == [
        "success",
        "txouts",
        "height",
        "bestblock",
        "unspents",
        "total_amount",
    ]
    assert result["success"] is True
    assert result["txouts"] == 2
    assert result["height"] == 2
    assert result["bestblock"] == chain[2]
    assert result["total_amount"].text == "100.00000000"
    first, second = result["unspents"]
    assert list(first) == [
        "txid",
        "vout",
        "scriptPubKey",
        "desc",
        "amount",
        "coinbase",
        "height",
        "blockhash",
        "confirmations",
    ]
    assert {first["height"], second["height"]} == {1, 2}
    for unspent in result["unspents"]:
        assert unspent["vout"] == 0
        assert unspent["coinbase"] is True
        assert unspent["amount"].text == SUBSIDY
        assert unspent["blockhash"] == chain[unspent["height"]]
        assert unspent["confirmations"] == 3 - unspent["height"]
        assert unspent["desc"].startswith(f"pkh([{FINGERPRINT}]{KEY})#")
        assert unspent["scriptPubKey"].hex().startswith("76a914")


def test_a_scan_with_no_objects_counts_the_set(node: Node) -> None:
    """`txouts` is every coin, and nothing is listed."""
    mine_to(node, f"pkh({KEY})", 3)
    result = scan(node)
    assert result["txouts"] == 3
    assert result["unspents"] == []
    assert result["total_amount"].text == "0.00000000"


def test_combo_is_inferred_as_the_script_that_matched(node: Node) -> None:
    """Each of `combo`'s four scripts is its own descriptor, in txid order."""
    for fragment in ("pk", "pkh", "wpkh", "sh(wpkh"):
        close = ")" if fragment == "sh(wpkh" else ""
        mine_to(node, f"{fragment}({KEY}){close}")
    descriptors = [
        unspent["desc"].split("#")[0]
        for unspent in scan(node, f"combo({KEY})")["unspents"]
    ]
    assert sorted(descriptors) == sorted(
        [
            f"pk([{FINGERPRINT}]{KEY})",
            f"pkh([{FINGERPRINT}]{KEY})",
            f"wpkh([{FINGERPRINT}]{KEY})",
            f"sh(wpkh([{FINGERPRINT}]{KEY}))",
        ]
    )


@pytest.mark.parametrize(
    ("written", "inferred"),
    [
        (
            f"wsh(sortedmulti(1,{KEY2},{KEY}))",
            f"wsh(multi(1,[{FINGERPRINT}]{KEY},[{FINGERPRINT2}]{KEY2}))",
        ),
        (
            f"sh(multi(1,{KEY},{KEY2}))",
            f"sh(multi(1,[{FINGERPRINT}]{KEY},[{FINGERPRINT2}]{KEY2}))",
        ),
        (f"tr({X_ONLY})", f"tr([{FINGERPRINT}]{X_ONLY})"),
        (
            f"tr({X_ONLY},sortedmulti_a(1,{X_ONLY2},{X_ONLY}))",
            (
                f"tr([{FINGERPRINT}]{X_ONLY},multi_a(1,[{FINGERPRINT}]{X_ONLY},"
                f"[{FINGERPRINT2}]{X_ONLY2}))"
            ),
        ),
        (
            f"tr(musig({KEY},{KEY2}))",
            "tr([8307b7d6]3b46d262d2f610e9038b44beabdfe97ab5a0feb89870acc2264edfb7f63ec2ec)",
        ),
        (f"rawtr({X_ONLY})", f"rawtr([{FINGERPRINT}]{X_ONLY})"),
        (f"wsh(pk({KEY}))", f"wsh(pk([{FINGERPRINT}]{KEY}))"),
        ("raw(51)", "raw(51)"),
    ],
)
def test_the_descriptor_is_core_s_inference(
    node: Node, written: str, inferred: str
) -> None:
    """A key carries its origin, and a `sortedmulti` is a `multi`."""
    mine_to(node, written)
    [unspent] = scan(node, written)["unspents"]
    assert unspent["desc"].split("#")[0] == inferred


def test_an_address_descriptor_is_inferred_as_itself(node: Node) -> None:
    """With no key to name, the address is the descriptor."""
    mine_to(node, f"wpkh({KEY})")
    address = ScriptPubKey(
        bytes.fromhex("0014") + hash160(bytes.fromhex(KEY)), "regtest"
    ).address
    [unspent] = scan(node, f"addr({address})")["unspents"]
    assert unspent["desc"].split("#")[0] == f"addr({address})"


def test_a_ranged_descriptor_is_expanded_over_its_range(node: Node) -> None:
    """The origin names the index; `range` is inclusive at both ends."""
    child = f"pkh({TPRV}/0h/1)"
    mine_to(node, child)
    ranged = f"pkh({TPRV}/0h/*)"
    assert scan(node, {"desc": ranged, "range": 0})["unspents"] == []
    for scan_object in (
        {"desc": ranged, "range": 1},
        {"desc": ranged, "range": [1, 1]},
        ranged,
    ):
        [unspent] = scan(node, scan_object)["unspents"]
        assert unspent["desc"].split("#")[0].startswith("pkh([3442193e/0h/1]")


def mine_scripts(node: Node, descriptor: str, *indexes: int) -> None:
    """Mine a block to each path's script at each of `indexes`."""
    for one in multipath_descriptors(descriptor):
        for index in indexes:
            for script in parse(one, "regtest").script_pub_keys(index):
                mine_to(node, f"raw({script.script.hex()})")


def descs(result: dict[str, Any]) -> list[str]:
    """Return the `desc` of each coin found, sorted."""
    return sorted(unspent["desc"] for unspent in result["unspents"])


# the keys of `TPUB/0/0`, `TPUB/0/1` and `TPUB/1/0`
CHILD_00 = "02756de182c5dd4b717ea87e693006da62dbb3cddaa4a5cad2ed1f5bbab755f0f5"
CHILD_01 = "02e740d213a1aa5746c66bae1ecda3b95d7f64d4bf8aff9d93702fc302f28df0f1"
CHILD_10 = "029b393153a1ec68c7af3a98e88aecede3a409f27e698c090540098611c79e05b0"


def test_a_key_s_origin_is_the_first_index_s(node: Node) -> None:
    """The providers of a range merge in order, the first entry winning."""
    ranged = f"wsh(multi(1,[aaaaaaaa/5]{TPUB}/0/*,[bbbbbbbb/7]{TPUB}/0/1))"
    mine_scripts(node, ranged, 0, 1)
    # what bitcoind answers
    assert descs(scan(node, {"desc": ranged, "range": [0, 1]})) == [
        f"wsh(multi(1,[aaaaaaaa/5/0/0]{CHILD_00},[bbbbbbbb/7/0/1]{CHILD_01}))#ry2d83lg",
        f"wsh(multi(1,[bbbbbbbb/7/0/1]{CHILD_01},[bbbbbbbb/7/0/1]{CHILD_01}))#5xzh3ty0",
    ]
    # no origin is put in front of another (btclib-org/bip32-descriptors#20)
    assert descs(scan(node, {"desc": ranged, "range": [1, 1]})) == [
        f"wsh(multi(1,[bbbbbbbb/7/0/1]{CHILD_01},[bbbbbbbb/7/0/1]{CHILD_01}))#5xzh3ty0",
    ]


def test_a_key_s_origin_is_the_first_path_s(node: Node) -> None:
    """The providers of a multipath descriptor merge in its paths' order."""
    crossed = f"wsh(multi(1,[aaaaaaaa]{TPUB}/<0;1>/0,[bbbbbbbb]{TPUB}/<1;0>/0))"
    mine_scripts(node, crossed, 0)
    # what bitcoind answers
    assert descs(scan(node, crossed)) == [
        f"wsh(multi(1,[aaaaaaaa/0/0]{CHILD_00},[bbbbbbbb/1/0]{CHILD_10}))#pjvqr79u",
        f"wsh(multi(1,[bbbbbbbb/1/0]{CHILD_10},[aaaaaaaa/0/0]{CHILD_00}))#lenvaan3",
    ]


def test_a_script_is_inferred_by_the_first_scan_object(node: Node) -> None:
    """Each scan object has a provider of its own, and the first one wins."""
    with_origin = f"wpkh([abcdef01/1h/2]{TPUB}/0/5)"
    without = f"wpkh({TPUB}/0/5)"
    mine_to(node, without)
    key = "0364a609ea30f2f9e137c3069b387321e6949baa097168e6dbfea48f13fbbe9f79"
    # what bitcoind answers
    assert descs(scan(node, with_origin, without)) == [
        f"wpkh([abcdef01/1h/2/0/5]{key})#qd0f3dek"
    ]
    assert descs(scan(node, without, with_origin)) == [
        f"wpkh([3442193e/0/5]{key})#k84ulzp6"
    ]
    # the key's origin in another scan object is not this one's
    assert descs(scan(node, f"pkh([abcdef01/1h/2]{TPUB}/0/5)", without)) == [
        f"wpkh([3442193e/0/5]{key})#k84ulzp6"
    ]


@pytest.mark.parametrize("count", range(10))
@pytest.mark.parametrize("merge_step", [2, 2**14])
def test_merged_providers_are_a_fold_in_order(
    node: Node, monkeypatch: pytest.MonkeyPatch, count: int, merge_step: int
) -> None:
    """The binary counter merges as `Provider.merged` one at a time does."""
    monkeypatch.setattr(utxo_set, "_MERGE_STEP", merge_step)
    ranged = parse(f"wsh(multi(1,[aaaaaaaa/5]{TPUB}/0/*,{TPUB}/0/1))", "regtest")
    providers = [ranged.provider(index) for index in range(count)]
    merged = utxo_set._Merged(node)
    folded = Provider()
    for provider in providers:
        finish(merged.add(provider))
        folded = folded.merged(provider)
    assert finish(merged.provider()) == folded


def test_a_large_merge_yields_and_stops_with_the_node(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge of `_MERGE_STEP` providers or more is a step of its own."""
    monkeypatch.setattr(utxo_set, "_MERGE_STEP", 4)
    ranged = parse(f"wpkh({TPUB}/0/*)", "regtest")
    merged = utxo_set._Merged(node)
    yields = [len(list(merged.add(ranged.provider(index)))) for index in range(4)]
    # the fourth makes a merge of two, then one of four
    assert yields == [0, 0, 0, 1]
    finish(merged.add(ranged.provider(4)))
    job = merged.provider()
    next(job)
    node.terminate_flag.set()
    with pytest.raises(RpcError) as caught:
        finish(job)
    assert caught.value.message == "Shutting down"


def test_a_multipath_descriptor_is_expanded_by_each_path(node: Node) -> None:
    """Each of a multipath descriptor's paths is scanned."""
    mine_to(node, f"pkh({TPUB}/0/0)")
    mine_to(node, f"pkh({TPUB}/1/0)")
    assert len(scan(node, f"pkh({TPUB}/<0;1>/0)")["unspents"]) == 2


# a compressed testnet WIF of KEY's secret, 1
WIF = "cMahea7zqjxrtgAbB7LSGbcQUr1uX1ojuat9jZodMN87JcbXMTcA"


@pytest.mark.parametrize(
    "text",
    [
        f"wsh(multi(1,{WIF},{TPUB}/0h/*))",
        f"sh(multi(1,{WIF},{TPUB}/1h))",
        f"tr({WIF},pk({TPUB}/0h/*))",
        f"pkh({TPUB}/0h/*)",
    ],
    ids=["wsh", "sh", "tr", "no private key"],
)
def test_a_hardened_step_needs_its_private_key(node: Node, text: str) -> None:
    """An xpub cannot take it.

    Core's rpc/util.cpp:1376 quotes the descriptor, private keys included;
    this tree does not, as Core's rpc/mining.cpp:231 does not (both at
    bitcoin/bitcoin@f0de87f040, the v32.1 tag).
    """
    assert refusal(node, "start", [text]) == (
        -5,
        "Cannot derive script without private keys",
    )


def test_status_and_abort_with_no_scan(node: Node) -> None:
    """There is nothing to report on or to stop."""
    assert scan_tx_out_set(node, CONN, ["status"]) is None
    assert scan_tx_out_set(node, CONN, ["abort"]) is False
    assert scan_tx_out_set(node, CONN, ["abort", None]) is False


def test_status_reports_and_abort_stops_a_scan(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between two steps a scan is reported on, then stopped."""
    monkeypatch.setattr(utxo_set, "_STEP", 2)
    mine_to(node, f"pkh({KEY})", 8)
    job = scan_tx_out_set(node, CONN, ["start", [f"pkh({KEY})"]])
    assert isinstance(job, Generator)
    next(job)
    assert scan_tx_out_set(node, CONN, ["status"]) == {"progress": 0}
    assert refusal(node, "start", []) == (
        -8,
        'Scan already in progress, use action "abort" or "status"',
    )
    assert scan_tx_out_set(node, CONN, ["abort"]) is True
    result = finish(job)
    assert result["success"] is False
    assert result["txouts"] == 2
    assert scan_tx_out_set(node, CONN, ["status"]) is None


def test_abort_during_the_expansion_is_lost(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core clears the abort flag once the expansion is done."""
    monkeypatch.setattr(utxo_set, "_EXPAND_STEP", 2)
    # the walk looks at the flag after every coin
    monkeypatch.setattr(utxo_set, "_STEP", 1)
    mine_to(node, f"pkh({KEY})")
    job = scan_tx_out_set(
        node, CONN, ["start", [{"desc": f"pkh({TPUB}/0/*)", "range": 10}]]
    )
    assert isinstance(job, Generator)
    next(job)
    assert scan_tx_out_set(node, CONN, ["abort"]) is True
    result = finish(job)
    assert (result["success"], result["txouts"]) == (True, 1)


def test_a_stopping_node_ends_the_expansion(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expansion has Core's interruption point too."""
    monkeypatch.setattr(utxo_set, "_EXPAND_STEP", 2)
    job = scan_tx_out_set(
        node, CONN, ["start", [{"desc": f"pkh({TPUB}/0/*)", "range": 10}]]
    )
    assert isinstance(job, Generator)
    next(job)
    node.terminate_flag.set()
    with pytest.raises(RpcError) as caught:
        finish(job)
    assert caught.value.message == "Shutting down"


def test_an_expansion_step_is_bounded_by_keys(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descriptor of more keys yields more often."""
    monkeypatch.setattr(utxo_set, "_EXPAND_STEP", 4)

    def steps(descriptor: str) -> int:
        job = scan_tx_out_set(node, CONN, ["start", [{"desc": descriptor, "range": 7}]])
        assert isinstance(job, Generator)
        count = 0
        for _ in job:
            count += 1
        return count

    one = f"pkh({TPUB}/0/*)"
    four = f"multi(1,{TPUB}/0/*,{TPUB}/1/*,{TPUB}/2/*,{TPUB}/3/*)"
    assert steps(f"sh({four})") > steps(one)


def test_a_stopping_node_ends_the_scan(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's interruption point refuses once the node is stopping."""
    monkeypatch.setattr(utxo_set, "_STEP", 1)
    mine_to(node, f"pkh({KEY})", 2)
    job = scan_tx_out_set(node, CONN, ["start", []])
    assert isinstance(job, Generator)
    next(job)
    node.terminate_flag.set()
    with pytest.raises(RpcError) as caught:
        finish(job)
    assert caught.value.message == "Shutting down"
    assert scan_tx_out_set(node, CONN, ["status"]) is None


def test_a_record_that_does_not_parse_fails_the_scan(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's cursor fails to read it: not an error, `success` is false."""
    mine_to(node, f"pkh({KEY})")
    monkeypatch.setattr(
        type(node.chainstate.utxo_index),
        "cursor",
        lambda _: iter([(b"utxo-" + bytes(36), b"")]),
    )
    result = scan(node)
    assert (result["success"], result["txouts"]) == (False, 0)


@pytest.mark.parametrize(
    ("params", "code", "message"),
    [
        ([], -1, 'scantxoutset "action" ( [scanobjects,...] )'),
        ([1], -3, '"Position 1 (action)": "JSON value of type number'),
        ([None], -3, "JSON value of type null is not of expected type string"),
        (
            [1, "x"],
            -3,
            (
                '"Position 1 (action)": "JSON value of type number is not of '
                'expected type string",\n    "Position 2 (scanobjects)": '
                '"JSON value of type string is not of expected type array"'
            ),
        ),
        (["status", 5], -3, '"Position 2 (scanobjects)": "JSON value of type number'),
        (["bogus"], -8, "Invalid action 'bogus'"),
        (["start"], -1, "scanobjects argument is required for the start action"),
        (["start", None], -1, "scanobjects argument is required for the start action"),
        (["start", [1]], -8, "Scan object needs to be either a string or an object"),
        (["start", [None]], -8, "Scan object needs to be either a string"),
        (["start", [{}]], -8, "Descriptor needs to be provided in scan object"),
        (
            ["start", [{"desc": None}]],
            -8,
            "Descriptor needs to be provided in scan object",
        ),
        (
            ["start", [{"desc": 1}]],
            -3,
            "JSON value of type number is not of expected type string",
        ),
        (["start", ["bogus"]], -5, ""),
        (["start", [f"pkh({KEY})#00000000"]], -5, ""),
        (["start", [f"pkh({XPUB}/0/*)"]], -5, ""),
        (["start", [f"pkh({TPUB}/<0;1>/<2;3>)"]], -5, ""),
        (
            ["start", [{"desc": "raw(51)", "range": -1}]],
            -8,
            "End of range is too high",
        ),
        (
            ["start", [{"desc": "raw(51)", "range": [-1, 10]}]],
            -8,
            "Range should be greater or equal than 0",
        ),
        (
            ["start", [{"desc": "raw(51)", "range": [2, 1]}]],
            -8,
            "Range specified as [begin,end] must not have begin after end",
        ),
        (
            ["start", [{"desc": "raw(51)", "range": [0, 1000000]}]],
            -8,
            "Range is too large",
        ),
        (
            ["start", [{"desc": "raw(51)", "range": 2**31}]],
            -8,
            "End of range is too high",
        ),
        (
            ["start", [{"desc": "raw(51)", "range": "x"}]],
            -8,
            "Range must be specified as end or as [begin,end]",
        ),
        (
            ["start", [{"desc": "raw(51)", "range": [1]}]],
            -8,
            "Range must be specified as end or as [begin,end]",
        ),
        (
            ["start", [{"desc": "raw(51)", "range": True}]],
            -8,
            "Range must be specified as end or as [begin,end]",
        ),
        (
            ["start", [{"desc": "raw(51)", "range": 1.5}]],
            -1,
            "JSON integer out of range",
        ),
        (
            ["start", [{"desc": "raw(51)", "range": [1.5, 2]}]],
            -1,
            "JSON integer out of range",
        ),
        (
            ["start", [{"desc": "raw(51)", "range": 2**63}]],
            -1,
            "JSON integer out of range",
        ),
    ],
)
def test_the_refusals_are_core_s(
    node: Node, params: list[object], code: int, message: str
) -> None:
    """Each refusal has Core's code, and Core's message but a parser's."""
    got_code, got_message = refusal(node, *params)
    assert got_code == code
    assert message in got_message


def test_a_key_as_action_is_refused_without_quoting_it(node: Node) -> None:
    """ISS 2514: an action longer than a name is given by its length."""
    with pytest.raises(RpcError) as caught:
        scan_tx_out_set(node, CONN, [LEAK_WIF])
    assert (int(caught.value.code), caught.value.message) == (
        -8,
        f"Invalid action '<{len(LEAK_WIF)} characters>'",
    )
    assert_secret_free(caught.value, [LEAK_WIF])


H160 = "ab" * 20
H256 = "cd" * 32
NOT_A_FUNCTION = "'{}(...)' is not a valid descriptor function"
NOT_A_FUNCTION_AFTER = "'{}(...)/0' is not a valid descriptor function"

# What bip32_descriptors reads, after the draft BIP of bitcoin/bips#1721,
# and the message `bitcoind` refuses it with. Where `bitcoind` quotes the
# expression, the node names the function and writes `...` (SECURITY.md).
DRAFT_REFUSALS = [
    ("sh(raw(51))", "Can only have raw() at top level"),
    ("wsh(raw(51))", "Can only have raw() at top level"),
    ("sh(wsh(raw(AB)))", "Can only have raw() at top level"),
    (f"tr({X_ONLY},raw(51))", "Can only have raw() at top level"),
    (f"tr({X_ONLY},{{pk({X_ONLY2}),raw(AB)}})", "Can only have raw() at top level"),
    (f"tr(musig({KEY},{KEY2}),raw(51))", "Can only have raw() at top level"),
    (f"rawpkh({H160})", NOT_A_FUNCTION.format("rawpkh")),
    (f"sh(rawpkh({H160}))", "A function is needed within P2SH"),
    (f"wsh(rawpkh({H160}))", "A function is needed within P2WSH"),
    (
        f"sh(wsh(and_v(v:rawpkh({H160}),pk({KEY}))))",
        "A function is needed within P2WSH",
    ),
    (f"tr({X_ONLY},rawpkh({H160}))", NOT_A_FUNCTION.format("rawpkh")),
    # Core quotes the leaf, a miniscript holding the draft function
    (
        f"tr({X_ONLY},and_v(v:rawpkh({H160}),pk({X_ONLY2})))",
        NOT_A_FUNCTION.format("and_v"),
    ),
    (f"tr({X_ONLY},rawnode({H256}))", NOT_A_FUNCTION.format("rawnode")),
    (
        f"tr({X_ONLY},{{rawnode({H256.upper()}),pk({X_ONLY2})}})",
        NOT_A_FUNCTION.format("rawnode"),
    ),
    (f"tr({X_ONLY},rawleaf(51,c0))", NOT_A_FUNCTION.format("rawleaf")),
    (f"tr({X_ONLY},rawleaf(51))", NOT_A_FUNCTION.format("rawleaf")),
    (
        f"tr({X_ONLY},{{pk({X_ONLY2}),{{pk({X_ONLY}),rawleaf(AB,C0)}}}})",
        NOT_A_FUNCTION.format("rawleaf"),
    ),
    (
        f"tr({X_ONLY},{{pk({TPUB}/<0;1>/*),rawleaf(51,c0)}})",
        NOT_A_FUNCTION.format("rawleaf"),
    ),
    (add_checksum(f"tr({X_ONLY},raw(51))"), "Can only have raw() at top level"),
    # the first of two, as Core walks the tree
    (
        f"tr({X_ONLY},{{rawnode({H256}),raw(51)}})",
        NOT_A_FUNCTION.format("rawnode"),
    ),
    (
        f"tr({X_ONLY},{{raw(51),rawnode({H256})}})",
        "Can only have raw() at top level",
    ),
]

# What both read: raw() at top level, and trees and scripts of known leaves
DRAFT_CONTROLS = [
    "raw(51)",
    "raw(AB)",
    f"tr({X_ONLY},pk({X_ONLY2}))",
    f"tr({X_ONLY},{{pk({X_ONLY2}),{{pk({X_ONLY}),pk({X_ONLY2})}}}})",
    f"tr({X_ONLY},and_v(v:pk({X_ONLY2}),after(10)))",
    f"tr(musig({KEY},{KEY2}),pk({X_ONLY2}))",
    f"wsh(and_v(v:pk({KEY}),older(10)))",
    f"sh(wsh(pkh({KEY})))",
]


@pytest.mark.parametrize(("text", "message"), DRAFT_REFUSALS)
def test_what_core_does_not_read_is_refused_with_its_message(
    node: Node, text: str, message: str
) -> None:
    """bip32_descriptors reads the draft's forms, and Core's refusal stands."""
    for one in multipath_descriptors(strip_checksum(text)):
        parse(one, "regtest")
    assert refusal(node, "start", [text]) == (-5, message)


@pytest.mark.parametrize("text", DRAFT_CONTROLS)
def test_what_both_read_is_scanned(node: Node, text: str) -> None:
    """raw() at top level, and a tree of known leaves, are not refused."""
    assert scan(node, text)["success"] is True


# a fixed random value, where a key may be put by mistake
LEAK = "92e07cd20dd29f3b6e3e2db783825a195e913e105c098d44b4dd7c67e200ac1c"


# Each with the function `_core_refusal` names, or None where
# bip32_descriptors refuses it first, with a message of its own
@pytest.mark.parametrize(
    ("text", "name"),
    [
        (f"tr({X_ONLY},and_v(v:rawpkh({H160}),pk({TPRV})))", "and_v"),
        (f"tr({X_ONLY},rawnode({LEAK}))", "rawnode"),
        (f"tr({X_ONLY},rawleaf({LEAK},c0))", "rawleaf"),
        (f"rawpkh({LEAK[:40]})", "rawpkh"),
        (f"rawnode({LEAK})", None),
        (f"rawleaf({LEAK},c0)", None),
    ],
)
def test_a_refusal_quotes_no_key(node: Node, text: str, name: str | None) -> None:
    """What `bitcoind` quotes may hold a key, which the refusal leaves out."""
    if name is None:
        with pytest.raises(BTClibException):
            parse(text, "regtest", {})
    else:
        parse(text, "regtest", {})
    with pytest.raises(RpcError) as caught:
        scan(node, text)
    assert_secret_free(caught.value, [TPRV, LEAK])
    if name is not None:
        assert (int(caught.value.code), caught.value.message) == (
            -5,
            NOT_A_FUNCTION.format(name),
        )


# Each where Core quotes an expression whose name or tail may be a key:
# the name is kept where it is short or `musig` after a key origin Core
# reads, and the tail where it is a path Core reads
@pytest.mark.parametrize(
    ("text", "quoted"),
    [
        (f"{LEAK}(rawpkh({H160}))", "'...(...)'"),
        (f"tr({X_ONLY},{TPRV}(rawpkh({H160})))", "'...(...)'"),
        (f"{TPRV}:and_v(v:rawpkh({H160}),pk({KEY}))", "'...(...)'"),
        (f"rawpkh(pk({LEAK_WIF}/<0;1>", "'rawpkh(...)...'"),
        (f"{TPRV}]musig({KEY},{KEY2})", "'...(...)'"),
        (f"[{TPRV}]musig({KEY},{KEY2})", "'...(...)'"),
        (f"{LEAK_WIF}]musig({KEY},{KEY2})", "'...(...)'"),
        (f"tr({X_ONLY},{{{TPRV}]musig({KEY},{KEY2}),raw(51)}})", "'...(...)'"),
        (f"[deadbeef/2147483648]musig({KEY},{KEY2})", "'...(...)'"),
        (f"musig({KEY},{KEY2})/{TPRV}", "'musig(...)...'"),
        (f"musig({KEY},{KEY2})/0/{LEAK}", "'musig(...)...'"),
        (f"musig({KEY},{KEY2}){TPRV}", "'musig(...)...'"),
        (f"musig({KEY},{KEY2})/2147483648", "'musig(...)...'"),
        (f"musig(),/*{LEAK}", "'musig(...)...'"),
        (f"musig())pk_k({LEAK_WIF}", "'musig(...)...'"),
        (f"musig())/{TPRV}", "'musig(...)...'"),
        (
            f"[deadbeef/0h/1]musig({KEY},{KEY2})/<0;1>/*",
            "'[deadbeef/0h/1]musig(...)/<0;1>/*'",
        ),
    ],
)
def test_a_quoted_expression_holds_no_key(node: Node, text: str, quoted: str) -> None:
    """A name or a tail that may be a key is written `...`."""
    with pytest.raises(RpcError) as caught:
        scan(node, text)
    assert (int(caught.value.code), caught.value.message) == (
        -5,
        f"{quoted} is not a valid descriptor function",
    )
    assert_secret_free(caught.value, [TPRV, LEAK, LEAK_WIF])


# a step longer than `int` converts from a string, alone or after zeros
LONG_STEP = "1" * 4400
ZEROS = "0" * 4400


# Each with a step of more digits than `int` converts from a string
@pytest.mark.parametrize(
    ("text", "quoted"),
    [
        (f"musig({KEY},{KEY2})/{LONG_STEP}", "'musig(...)...'"),
        (f"[deadbeef/{LONG_STEP}]musig({KEY},{KEY2})", "'...(...)'"),
        (f"musig({KEY},{KEY2})/{ZEROS}2147483648", "'musig(...)...'"),
        (f"musig({KEY},{KEY2})/{ZEROS}1", f"'musig(...)/{ZEROS}1'"),
        (
            f"[deadbeef/{ZEROS}1]musig({KEY},{KEY2})",
            f"'[deadbeef/{ZEROS}1]musig(...)'",
        ),
    ],
)
def test_a_step_of_any_length_is_refused_through_the_rpc_caller(
    node: Node, text: str, quoted: str
) -> None:
    """The refusal is Core's -5, not the -32603 a raised `ValueError` gets."""
    request = JsonRpcRequest()
    request.method = "scantxoutset"
    request.params = ["start", [text]]
    with pytest.raises(RpcError) as caught:
        finish(_execute(node, CONN, request))
    assert (int(caught.value.code), caught.value.message) == (
        -5,
        f"{quoted} is not a valid descriptor function",
    )


@pytest.mark.parametrize(
    ("path", "fits"),
    [
        ("/2147483647", True),
        ("/2147483648", False),
        (f"/{ZEROS}2147483647", True),
        (f"/{ZEROS}2147483648", False),
        (f"/{LONG_STEP}", False),
        (f"/0/{ZEROS}", True),
    ],
)
def test_a_step_is_bounded_without_converting_all_its_digits(
    path: str, *, fits: bool
) -> None:
    """A step is below 2**31 or not, whatever its number of digits."""
    assert utxo_set._fits(path) is fits


@pytest.mark.parametrize(
    ("text", "argument"),
    [
        (f"{KEY},foo(1),{{a}})", f"{KEY},foo(1),{{a}}"),
        (f"{KEY},foo", f"{KEY},foo"),
    ],
)
def test_an_argument_runs_to_the_parenthesis_closing_its_call(
    text: str, argument: str
) -> None:
    """Commas and nested calls stay in it, and no `)` leaves all of `text`.

    `_parse` gives the key walk no text with an unclosed call: Core and
    bip32_descriptors refuse such a call as no function first.
    """
    assert utxo_set._argument(text) == argument


@pytest.mark.parametrize(
    "args",
    [f"{X_ONLY},)", f"{X_ONLY},pk({X_ONLY2}))", f"{X_ONLY},{{pk({X_ONLY2})}})"],
)
def test_a_tree_walk_stops_at_a_parenthesis_it_cannot_pass(args: str) -> None:
    """A `)` at level 0 ends the walk, which would otherwise not progress."""
    assert utxo_set._tree_refusal(args) is None


# an x no point of the curve has, as an x-only key and a compressed one
NOT_A_POINT_X = "4bf5122f344554c53bde2ebb8cd2b7e3d1600ad631c385a5d7cce23c7785459a"
NOT_A_POINT = f"02{NOT_A_POINT_X}"
INVALID = "Pubkey '...' is invalid"

# Each with the message of `ParseScript` in `src/script/descriptor.cpp`,
# at bitcoin/bitcoin@f0de87f040, the v32.1 tag, whose key is `...` here
NOT_A_POINT_REFUSALS = [
    (f"pk({NOT_A_POINT})", f"pk(): {INVALID}"),
    (add_checksum(f"pkh({NOT_A_POINT})"), f"pkh(): {INVALID}"),
    (f"sh(wpkh([deadbeef/0]{NOT_A_POINT}))", f"wpkh(): {INVALID}"),
    (f"combo({NOT_A_POINT})", f"combo(): {INVALID}"),
    (f"pk(04{'00' * 64})", f"pk(): {INVALID}"),
    (f"multi(1,{KEY},{NOT_A_POINT})", f"Multi: {INVALID}"),
    (f"wsh(multi(1,{TPUB}/<0;1>/0,{NOT_A_POINT}))", f"Multi: {INVALID}"),
    (f"tr({TPUB}/<0;1>/0,pk({NOT_A_POINT_X}))", f"pk(): {INVALID}"),
    (f"sh(wsh(pkh({NOT_A_POINT})))", f"pkh(): {INVALID}"),
    (f"wsh(and_v(v:pk({NOT_A_POINT}),older(10)))", INVALID),
    (f"tr({NOT_A_POINT_X})", f"tr(): {INVALID}"),
    (f"tr({NOT_A_POINT},pk({X_ONLY}))", f"tr(): {INVALID}"),
    (f"rawtr({NOT_A_POINT_X})", f"rawtr(): {INVALID}"),
    (f"tr({X_ONLY},{{pk({X_ONLY2}),pk({NOT_A_POINT_X})}})", f"pk(): {INVALID}"),
    (f"tr({X_ONLY},multi_a(1,{X_ONLY2},{NOT_A_POINT_X}))", f"Multi: {INVALID}"),
    (f"tr({X_ONLY},pkh({NOT_A_POINT_X}))", INVALID),
    (f"tr({X_ONLY},and_v(v:pk({NOT_A_POINT_X}),older(10)))", INVALID),
    (f"unused({NOT_A_POINT})", INVALID),
]


@pytest.mark.parametrize(("text", "message"), NOT_A_POINT_REFUSALS)
def test_a_key_that_is_no_point_is_refused_with_core_s_message(
    node: Node, text: str, message: str
) -> None:
    """bip32_descriptors refuses it with ecc's error, which is no BTClib one."""
    with pytest.raises(EccException):
        parse(multipath_descriptors(strip_checksum(text))[0], "regtest", {})
    with pytest.raises(RpcError) as caught:
        scan(node, text)
    assert (int(caught.value.code), caught.value.message) == (-5, message)
    assert_secret_free(caught.value, [NOT_A_POINT_X])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (f"tr(musig({NOT_A_POINT_X},{X_ONLY}))", f"tr(): musig(): {INVALID}"),
        (f"rawtr(musig({NOT_A_POINT_X},{X_ONLY}))", f"rawtr(): musig(): {INVALID}"),
        (f"tr(musig({X_ONLY},{X_ONLY2}))", f"tr(): musig(): {INVALID}"),
        (f"tr(musig({NOT_A_POINT},{KEY}))", f"tr(): musig(): {INVALID}"),
        (f"tr({X_ONLY},pk(musig({X_ONLY2},{KEY})))", f"pk(): musig(): {INVALID}"),
        (
            f"tr({X_ONLY},multi_a(1,musig({NOT_A_POINT},{KEY})))",
            f"Multi: musig(): {INVALID}",
        ),
        (f"pk({X_ONLY})", f"pk(): {INVALID}"),
        (f"wsh(pk({X_ONLY}))", f"pk(): {INVALID}"),
        (f"unused({X_ONLY})", INVALID),
    ],
)
def test_a_btclib_key_refusal_uses_core_s_secret_free_message(
    node: Node, text: str, message: str
) -> None:
    """These are rejected before ecc's exception reaches `_parse`."""
    with pytest.raises(BTClibValueError):
        parse(multipath_descriptors(strip_checksum(text))[0], "regtest", {})
    with pytest.raises(RpcError) as caught:
        scan(node, text)
    assert (int(caught.value.code), caught.value.message) == (-5, message)
    assert_secret_free(caught.value, [NOT_A_POINT_X, X_ONLY])


def test_an_earlier_musig_context_refusal_is_not_replaced(node: Node) -> None:
    """A key argument is irrelevant when its holder is refused first."""
    text = f"wpkh(musig({NOT_A_POINT_X},{X_ONLY}))"
    with pytest.raises(RpcError) as caught:
        scan(node, text)
    assert (int(caught.value.code), caught.value.message) == (
        -5,
        "wpkh(): musig() is only allowed in tr() and rawtr()",
    )


# the y of KEY's point, which is even
Y = "483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8"
UNCOMPRESSED = f"04{X_ONLY}{Y}"
UNCOMPRESSED_WIF = wif_from_prv_key(LEAK_HEX, "regtest", compressed=False)
# a WIF of another network than the node's, which Core reads as no key
MAINNET_WIF = wif_from_prv_key(LEAK_HEX, "mainnet", compressed=False)
# a prefix no key has
BAD_PREFIX = f"05{LEAK_HEX}"
MUSIG = f"musig({KEY},{KEY2})"
MUSIG_PLACE = "musig() is only allowed in tr() and rawtr()"
HYBRID = "Hybrid public keys are not allowed"
NOT_UNCOMPRESSED = "Uncompressed keys are not allowed"
NEEDED_P2SH = "A function is needed within P2SH"
NEEDED_P2WSH = "A function is needed within P2WSH"
NO_KEY = "key '...' is not valid"
MUSIG_PATH = "'musig(...)/0/*' is not a valid descriptor function"
SPACE = "Key '...' is invalid due to whitespace"
TOO_MANY = "Too many ')' in musig() expression"
UNEXTENDED = "musig(): derivation requires all participants to be xpubs or xprvs"
RAW_LEAF = "Can only have raw() at top level"

# Each with the message of `ParsePubkey`, `ParsePubkeyInner` and
# `ParseScript` in `src/script/descriptor.cpp`, at bitcoin/bitcoin@f0de87f040,
# the v32.1 tag, whose key is `...` here where Core quotes it
KEY_REFUSALS = [
    # a musig() that a function reads as a key outside tr() and rawtr()
    (f"wpkh({MUSIG})", f"wpkh(): {MUSIG_PLACE}"),
    (f"pk({MUSIG})", f"pk(): {MUSIG_PLACE}"),
    (f"pkh({MUSIG})", f"pkh(): {MUSIG_PLACE}"),
    (f"combo({MUSIG})", f"combo(): {MUSIG_PLACE}"),
    (f"sh(wpkh({MUSIG}))", f"wpkh(): {MUSIG_PLACE}"),
    (f"wsh(pkh({MUSIG}))", f"pkh(): {MUSIG_PLACE}"),
    (f"sortedmulti(1,{KEY},{MUSIG})", f"Multi: {MUSIG_PLACE}"),
    (f"sh(multi(1,{MUSIG}))", f"Multi: {MUSIG_PLACE}"),
    (f"wsh(and_v(v:pk({MUSIG}),older(10)))", MUSIG_PLACE),
    (f"wsh(or_d(pk({KEY}),pkh({MUSIG})))", MUSIG_PLACE),
    (f"unused({MUSIG})", MUSIG_PLACE),
    (f"wpkh(musig({MUSIG},{KEY}))", f"wpkh(): {MUSIG_PLACE}"),
    # a musig() where a script belongs, refused before its keys are read
    (MUSIG, NOT_A_FUNCTION.format("musig")),
    (f"sh({MUSIG})", NEEDED_P2SH),
    (f"wsh({MUSIG})", NEEDED_P2WSH),
    (f"sh(wsh({MUSIG}))", NEEDED_P2WSH),
    (f"tr({X_ONLY},{MUSIG})", NOT_A_FUNCTION.format("musig")),
    (f"tr({X_ONLY},{{pk({X_ONLY2}),{MUSIG}}})", NOT_A_FUNCTION.format("musig")),
    (f"tr({X_ONLY},musig({BAD_PREFIX},{KEY}))", NOT_A_FUNCTION.format("musig")),
    # a key with a prefix no key has
    (f"pk({BAD_PREFIX})", f"pk(): {INVALID}"),
    (f"multi(1,{KEY},{BAD_PREFIX})", f"Multi: {INVALID}"),
    (f"wsh(and_v(v:pk({BAD_PREFIX}),older(10)))", INVALID),
    (f"unused({BAD_PREFIX})", INVALID),
    (f"tr({BAD_PREFIX})", f"tr(): {INVALID}"),
    (f"tr(musig({BAD_PREFIX},{KEY}))", f"tr(): musig(): {INVALID}"),
    (f"tr({X_ONLY},pk(musig({KEY},{BAD_PREFIX})))", f"pk(): musig(): {INVALID}"),
    (f"pk(04{X_ONLY})", f"pk(): {INVALID}"),
    (f"pk(02{X_ONLY}{Y})", f"pk(): {INVALID}"),
    # a hybrid key, which Core tells by its prefix and size alone
    (f"pk(06{X_ONLY}{Y})", f"pk(): {HYBRID}"),
    (f"tr(07{X_ONLY}{Y})", f"tr(): {HYBRID}"),
    (f"pk(06{X_ONLY}{'00' * 32})", f"pk(): {HYBRID}"),
    (f"tr(musig(06{X_ONLY}{Y},{KEY2}))", f"tr(): musig(): {HYBRID}"),
    (f"wsh(and_v(v:pk(07{X_ONLY}{Y}),older(10)))", HYBRID),
    (f"unused(06{X_ONLY}{Y})", HYBRID),
    # an uncompressed key where Core reads compressed ones only
    (f"wpkh({UNCOMPRESSED})", f"wpkh(): {NOT_UNCOMPRESSED}"),
    (f"sh(wpkh({UNCOMPRESSED}))", f"wpkh(): {NOT_UNCOMPRESSED}"),
    (f"wsh(pk({UNCOMPRESSED}))", f"pk(): {NOT_UNCOMPRESSED}"),
    (f"wsh(multi(1,{TPUB}/<0;1>/*,{UNCOMPRESSED}))", f"Multi: {NOT_UNCOMPRESSED}"),
    (f"wsh(and_v(v:pk({UNCOMPRESSED}),older(10)))", NOT_UNCOMPRESSED),
    (f"tr({UNCOMPRESSED})", f"tr(): {NOT_UNCOMPRESSED}"),
    (f"rawtr({UNCOMPRESSED})", f"rawtr(): {NOT_UNCOMPRESSED}"),
    (f"tr({X_ONLY},pk({UNCOMPRESSED}))", f"pk(): {NOT_UNCOMPRESSED}"),
    (f"tr({X_ONLY},multi_a(1,{UNCOMPRESSED}))", f"Multi: {NOT_UNCOMPRESSED}"),
    (f"tr(musig({UNCOMPRESSED},{KEY}))", f"tr(): musig(): {NOT_UNCOMPRESSED}"),
    (f"wpkh([deadbeef/0]{UNCOMPRESSED_WIF})", f"wpkh(): {NOT_UNCOMPRESSED}"),
    (f"tr(musig({KEY},{UNCOMPRESSED_WIF}))", f"tr(): musig(): {NOT_UNCOMPRESSED}"),
    # an uncompressed WIF of another network, which Core reads as no key
    (f"wpkh({MAINNET_WIF})", f"wpkh(): {NO_KEY}"),
    (f"wsh(multi(1,{KEY},{MAINNET_WIF}))", f"Multi: {NO_KEY}"),
    (f"wsh(and_v(v:pk({MAINNET_WIF}),older(10)))", NO_KEY),
    (f"tr(musig({MAINNET_WIF},{KEY}))", f"tr(): musig(): {NO_KEY}"),
    # the first Core meets, a function it does not read or a key it refuses
    (f"wsh(or_d(rawpkh({H160}),pkh({MUSIG})))", NEEDED_P2WSH),
    (f"wsh(or_d(pkh({MUSIG}),rawpkh({H160})))", MUSIG_PLACE),
    (f"wsh(or_d(rawpkh({H160}),pk({NOT_A_POINT})))", NEEDED_P2WSH),
    (f"wsh(or_d(rawpkh({H160}),pk({X_ONLY})))", NEEDED_P2WSH),
    (f"wsh(or_d(pk({NOT_A_POINT}),rawpkh({H160})))", INVALID),
    (
        f"tr({X_ONLY},{{rawpkh({H160}),pk({UNCOMPRESSED})}})",
        NOT_A_FUNCTION.format("rawpkh"),
    ),
    (
        f"tr({X_ONLY},{{pk({UNCOMPRESSED}),rawpkh({H160})}})",
        f"pk(): {NOT_UNCOMPRESSED}",
    ),
    (
        f"tr({X_ONLY},{{raw(51),pk({UNCOMPRESSED})}})",
        "Can only have raw() at top level",
    ),
    (f"tr({X_ONLY},{{pk({NOT_A_POINT_X}),raw(51)}})", f"pk(): {INVALID}"),
    (f"tr({X_ONLY},{{rawpkh({H160}),{MUSIG}}})", NOT_A_FUNCTION.format("rawpkh")),
    (f"tr({X_ONLY},{{{MUSIG},rawpkh({H160})}})", NOT_A_FUNCTION.format("musig")),
    (
        f"tr({X_ONLY},and_v(v:rawpkh({H160}),pk({NOT_A_POINT_X})))",
        NOT_A_FUNCTION.format("and_v"),
    ),
    (f"tr({X_ONLY},and_v(v:pk({NOT_A_POINT_X}),rawpkh({H160})))", INVALID),
    # a musig() with a path where a script belongs, its path kept
    (f"tr({X_ONLY},{{raw(51),{MUSIG}/0/*}})", "Can only have raw() at top level"),
    (f"tr({X_ONLY},{{rawnode({H256}),{MUSIG}/0/*}})", NOT_A_FUNCTION.format("rawnode")),
    (f"tr({X_ONLY},{{{MUSIG}/0/*,raw(51)}})", MUSIG_PATH),
    (f"tr({X_ONLY},{MUSIG}/0/*)", MUSIG_PATH),
    (f"{MUSIG}/0/*", MUSIG_PATH),
    # a hex key of a length no key has, and hex of odd length, which is none
    (f"pk({H160})", f"pk(): {INVALID}"),
    (f"pk([deadbeef]{H160})", f"pk(): {INVALID}"),
    (f"wpkh([deadbeef/0h/1]{H160})", f"wpkh(): {INVALID}"),
    (f"pk({KEY}00)", f"pk(): {INVALID}"),
    (f"wsh(multi(1,{H160},{KEY}))", f"Multi: {INVALID}"),
    (f"sh(multi(1,{KEY},{H160}))", f"Multi: {INVALID}"),
    (f"tr({H160})", f"tr(): {INVALID}"),
    (f"rawtr({H160})", f"rawtr(): {INVALID}"),
    (f"tr({X_ONLY},pk({H160}))", f"pk(): {INVALID}"),
    (f"tr(musig({KEY},{H160}))", f"tr(): musig(): {INVALID}"),
    (f"and_v(v:pk({H160}),pk({KEY}))", INVALID),
    (f"sh(and_v(v:pk({H160}),pk({KEY})))", INVALID),
    (f"pk({KEY[:-1]})", f"pk(): {NO_KEY}"),
    # a musig() in a musig(), refused before its participants
    (f"tr(musig({MUSIG},{KEY}))", f"tr(): {TOO_MANY}"),
    (f"tr(musig({KEY},{MUSIG}))", f"tr(): {TOO_MANY}"),
    (f"tr({X_ONLY},pk(musig({MUSIG},{KEY})))", f"pk(): {TOO_MANY}"),
    # a musig() after a key origin, which Core reads as no key
    (f"wpkh([deadbeef]{MUSIG})", f"wpkh(): {NO_KEY}"),
    (f"tr([deadbeef]{MUSIG})", f"tr(): {NO_KEY}"),
    (f"tr({X_ONLY},pk([deadbeef]{MUSIG}))", f"pk(): {NO_KEY}"),
    # a musig() derivation where a participant is no extended key
    (f"tr({X_ONLY},pk({MUSIG}/0/*))", f"pk(): {UNEXTENDED}"),
    (f"tr(musig({TPUB},{KEY})/0/*)", f"tr(): {UNEXTENDED}"),
    (f"tr(musig({KEY},{TPUB})/<0;1>/*)", f"tr(): {UNEXTENDED}"),
    # a musig() where a script belongs, after a key origin or a multipath
    (f"{MUSIG}/<0;1>/*", "'musig(...)/<0;1>/*' is not a valid descriptor function"),
    (f"[deadbeef]{MUSIG}", "'[deadbeef]musig(...)' is not a valid descriptor function"),
    # whitespace at either end of a key, after its origin
    (f"wpkh( {KEY})", f"wpkh(): {SPACE}"),
    (f"wpkh({KEY} )", f"wpkh(): {SPACE}"),
    (f"wpkh([deadbeef] {KEY})", f"wpkh(): {SPACE}"),
    (f"wsh(multi(1,{KEY}, {KEY2}))", f"Multi: {SPACE}"),
    (f"tr({X_ONLY},pk( {X_ONLY}))", f"pk(): {SPACE}"),
    (f"wpkh( {MUSIG})", f"wpkh(): {SPACE}"),
    (f"tr(musig( {KEY},{KEY2}))", f"tr(): musig(): {SPACE}"),
    (f"wpkh( {LEAK_WIF})", f"wpkh(): {SPACE}"),
    # a compressed WIF of another network
    (f"pk({LEAK_WIF})", f"pk(): {NO_KEY}"),
    (f"wpkh({LEAK_WIF})", f"wpkh(): {NO_KEY}"),
    (f"tr({LEAK_WIF})", f"tr(): {NO_KEY}"),
    (f"sh(multi(1,{KEY},{LEAK_WIF}))", f"Multi: {NO_KEY}"),
    (f"wsh(multi(1,{LEAK_WIF},{H160}))", f"Multi: {NO_KEY}"),
    # text that is no key: no hex, WIF or extended key of the node's network
    ("wpkh(foo)", f"wpkh(): {NO_KEY}"),
    (f"wsh(multi(1,{KEY},foo))", f"Multi: {NO_KEY}"),
    ("wpkh(foo(1))", f"wpkh(): {NO_KEY}"),
    (f"wpkh([deadbeef]pk({KEY}))", f"wpkh(): {NO_KEY}"),
    # a key origin with no `]`, which Core reads as part of the key
    (f"wpkh([deadbeef{KEY})", f"wpkh(): {NO_KEY}"),
    (f"wpkh([{KEY})", f"wpkh(): {NO_KEY}"),
    ("pk([)", f"pk(): {NO_KEY}"),
    (f"tr([deadbeef{X_ONLY})", f"tr(): {NO_KEY}"),
    (f"tr({X_ONLY},pk([deadbeef{X_ONLY}))", f"pk(): {NO_KEY}"),
    (f"wsh(multi(1,{KEY},[deadbeef{KEY2}))", f"Multi: {NO_KEY}"),
    (f"tr(musig({KEY},[{KEY2}))", f"tr(): musig(): {NO_KEY}"),
    (f"wpkh([deadbeef/0{TPUB}/0)", f"wpkh(): {NO_KEY}"),
    (f"wpkh({KEY}/0)", f"wpkh(): {NO_KEY}"),
    (f"wpkh({UNCOMPRESSED_WIF}/0)", f"wpkh(): {NO_KEY}"),
    (f"wpkh({XPUB}/0/*)", f"wpkh(): {NO_KEY}"),
    (f"wpkh({TPUB}<0;1>/*)", f"wpkh(): {NO_KEY}"),
    (f"and_v(v:pk({LEAK_WIF}),pk({KEY}))", NO_KEY),
    ("wpkh()", "wpkh(): No key provided"),
    # the whole argument of a function of one key, commas included, read
    # as the key, whether the function is miniscript's or not
    (f"pk({H160},foo)", f"pk(): {NO_KEY}"),
    (f"pk({H160},foo )", f"pk(): {SPACE}"),
    (f"pk({KEY},foo)", f"pk(): {NO_KEY}"),
    (f"pk([deadbeef]{KEY},foo)", f"pk(): {NO_KEY}"),
    (f"pkh({KEY},foo)", f"pkh(): {NO_KEY}"),
    (f"combo({KEY},foo)", f"combo(): {NO_KEY}"),
    (f"wpkh({KEY},{KEY2})", f"wpkh(): {NO_KEY}"),
    (f"sh(wpkh({KEY},{KEY2}))", f"wpkh(): {NO_KEY}"),
    (f"wsh(pk({H160},foo))", f"pk(): {NO_KEY}"),
    (f"tr({X_ONLY},pk({H160},foo))", f"pk(): {NO_KEY}"),
    (f"tr({X_ONLY},{{pk({X_ONLY2}),pk({X_ONLY2},foo)}})", f"pk(): {NO_KEY}"),
    (f"pk({KEY},rawpkh({H160}))", f"pk(): {NO_KEY}"),
    (f"pk({KEY},rawpkh({H160}) )", f"pk(): {SPACE}"),
    (f"wpkh({KEY},rawpkh({H160}))", f"wpkh(): {NO_KEY}"),
    (f"wsh(pk({KEY},rawpkh({H160})))", f"pk(): {NO_KEY}"),
    (f"tr({X_ONLY},pk({X_ONLY2},rawpkh({H160})))", f"pk(): {NO_KEY}"),
    (f"wsh(and_v(v:pk({KEY},foo),pk({KEY2})))", NO_KEY),
    (f"wsh(and_v(v:pk_k({KEY},foo),pk({KEY2})))", NO_KEY),
    (f"wsh(and_v(v:pk({KEY},foo(1 )),pk({KEY2})))", NO_KEY),
    (f"wsh(pk_h({KEY},{KEY2}))", NO_KEY),
    (f"tr({X_ONLY},and_v(v:pk({X_ONLY2},foo),pk({X_ONLY2})))", NO_KEY),
    (f"tr({X_ONLY},pkh({X_ONLY2},foo))", NO_KEY),
    (f"pk({MUSIG},foo)", f"pk(): {MUSIG_PLACE}"),
    # outside wsh(), miniscript reads a key as tr() does
    (f"and_v(v:pk({UNCOMPRESSED}),pk({KEY}))", NOT_UNCOMPRESSED),
    # a key of another network before what bip32_descriptors refuses
    (f"tr({X_ONLY},{{pk({LEAK_WIF}),foo(1)}})", f"pk(): {NO_KEY}"),
    (f"tr({LEAK_WIF},foo(1))", f"tr(): {NO_KEY}"),
    (f"tr({LEAK_WIF},{{raw(51),foo(1)}})", f"tr(): {NO_KEY}"),
    # a leaf Core does not read, before a leaf no one reads
    (f"tr({X_ONLY},{{raw(51),foo(1)}})", RAW_LEAF),
    (f"tr({X_ONLY},{{pk({KEY}),{{raw(51),foo(1)}}}})", RAW_LEAF),
    (f"tr({X_ONLY},{{raw(51),{MUSIG}/<0;1>/*}})", RAW_LEAF),
    (f"tr({X_ONLY},{{raw(51),{MUSIG}/2147483647/*}})", RAW_LEAF),
    # text after the `)` of sh(), wsh(), tr() or raw(), which Core then
    # reads as no function
    (f"tr({X_ONLY},raw(51))/0", NOT_A_FUNCTION_AFTER.format("tr")),
    (f"tr({X_ONLY},{{raw(51),foo(1)}})/0", NOT_A_FUNCTION_AFTER.format("tr")),
    (
        f"sh(and_v(v:rawpkh({H160}),pk({KEY})))/0",
        NOT_A_FUNCTION_AFTER.format("sh"),
    ),
    (f"wsh(rawpkh({H160}))/0", NOT_A_FUNCTION_AFTER.format("wsh")),
    ("raw(51)/0", NOT_A_FUNCTION_AFTER.format("raw")),
    (f"tr({X_ONLY},raw(51)/0)", NOT_A_FUNCTION_AFTER.format("raw")),
    (f"tr({X_ONLY},{{raw(51)/0,foo(1)}})", NOT_A_FUNCTION_AFTER.format("raw")),
    (
        f"and_v(v:rawpkh({H160}),pk({KEY}))/0",
        NOT_A_FUNCTION_AFTER.format("and_v"),
    ),
    # a miniscript outside wsh() and tr() that holds a draft function
    (f"and_v(v:rawpkh({H160}),pk({KEY}))", NOT_A_FUNCTION.format("and_v")),
    (f"sh(and_v(v:rawpkh({H160}),pk({KEY})))", NEEDED_P2SH),
]


@pytest.mark.parametrize(("text", "message"), KEY_REFUSALS)
def test_a_key_or_musig_core_refuses_has_core_s_message(
    node: Node, text: str, message: str
) -> None:
    """bip32_descriptors and ecc refuse each in words of their own."""
    with pytest.raises((BTClibException, EccException)):
        parse(multipath_descriptors(strip_checksum(text))[0], "regtest", {})
    with pytest.raises(RpcError) as caught:
        scan(node, text)
    assert (int(caught.value.code), caught.value.message) == (-5, message)
    assert_secret_free(
        caught.value, [LEAK_HEX, UNCOMPRESSED_WIF, MAINNET_WIF, LEAK_WIF]
    )


# Each with a private key, or a fixed random value, where Core quotes the
# key or the text it reads as one
@pytest.mark.parametrize(
    ("text", "message"),
    [
        (f"pk({LEAK[:40]})", f"pk(): {INVALID}"),
        (f"pk({LEAK[:-1]})", f"pk(): {NO_KEY}"),
        (f"wpkh( {LEAK})", f"wpkh(): {SPACE}"),
        (f"wsh(multi(1,{KEY}, {TPRV}/0))", f"Multi: {SPACE}"),
        (f"wpkh({TPRV}<0;1>/*)", f"wpkh(): {NO_KEY}"),
        (f"tr([deadbeef]musig({TPRV},{KEY}))", f"tr(): {NO_KEY}"),
        (f"tr({TPRV},pk([deadbeef]musig({KEY},{TPRV})))", f"pk(): {NO_KEY}"),
        (f"wpkh([{TPRV})", f"wpkh(): {NO_KEY}"),
        (f"wpkh([deadbeef{TPRV}/0)", f"wpkh(): {NO_KEY}"),
        (f"pk({TPRV},foo)", f"pk(): {NO_KEY}"),
        (f"pk({KEY},{TPRV})", f"pk(): {NO_KEY}"),
    ],
)
def test_a_key_refusal_quotes_no_key(node: Node, text: str, message: str) -> None:
    """What `bitcoind` quotes may be a private key, which is left out."""
    with pytest.raises(RpcError) as caught:
        scan(node, text)
    assert (int(caught.value.code), caught.value.message) == (-5, message)
    assert_secret_free(caught.value, [TPRV, LEAK])


def test_the_key_walk_prints_no_key_in_its_repr_or_str() -> None:
    """`text` and `calls` may hold a private key, which neither shows."""
    walk = utxo_set._KeyWalk(f"wpkh({TPRV}(1))", "regtest")
    for at, part in ((0, "wpkh"), (len("wpkh(") + len(TPRV), TPRV)):
        assert walk.open(part, at) is None
    assert walk.calls == ["wpkh", TPRV]
    assert_secret_free(ValueError(repr(walk), str(walk)), [TPRV])


# Each with the threshold `bitcoind` refuses in it, which its message quotes
@pytest.mark.parametrize(
    ("text", "threshold"),
    [
        (f"wsh(multi( 1,{KEY}))", " 1"),
        (f"wsh(multi(a,{KEY}))", "a"),
        # `Expr` ends the threshold at a `)` or a `,` outside brackets
        ("wsh(multi(a))", "a"),
        ("multi(a)", "a"),
        (f"wsh(multi((1,2),{KEY}))", "(1,2)"),
        (f"wsh(sortedmulti( 1,{KEY}))", " 1"),
        (f"sh(sortedmulti(a,{KEY}))", "a"),
        (f"tr({X_ONLY},multi_a( 1,{X_ONLY}))", " 1"),
        (f"tr({X_ONLY},sortedmulti_a(a,{X_ONLY}))", "a"),
        # the threshold refused, not the first the text holds
        (f"tr({X_ONLY},{{multi_a(1,{X_ONLY}),multi_a(x,{X_ONLY})}})", "x"),
        (f"tr({X_ONLY},{{multi_a(x,{X_ONLY}),sortedmulti_a(1,{X_ONLY})}})", "x"),
        (f"tr({X_ONLY},{{sortedmulti_a(1,{X_ONLY}),multi_a(x,{X_ONLY})}})", "x"),
        (f"tr({X_ONLY},{{multi_a(001,{X_ONLY}),multi_a(x,{X_ONLY})}})", "x"),
        # numbers `ToIntegral<uint32_t>` does not read
        (f"wsh(multi(4294967296,{KEY}))", "4294967296"),
        (f"wsh(multi(11111111111,{KEY}))", "11111111111"),
        (f"wsh(multi(+1,{KEY}))", "+1"),
        (f"wsh(multi(-1,{KEY}))", "-1"),
    ],
)
def test_a_threshold_core_does_not_read_is_refused_with_its_message(
    node: Node, text: str, threshold: str
) -> None:
    """ISS 2553: Core's `ParseScript` quotes the threshold it refuses."""
    assert refusal(node, "start", [text]) == (
        -5,
        f"Multi threshold '{threshold}' is not valid",
    )


@pytest.mark.parametrize("key", [TPRV, LEAK_WIF, LEAK, str(int(LEAK, 16))])
def test_a_key_given_as_a_threshold_is_given_by_its_length(
    node: Node, key: str
) -> None:
    """Core quotes the threshold whole; this gives a long one by its length."""
    with pytest.raises(RpcError) as caught:
        scan(node, f"wsh(multi({key},{KEY}))")
    assert (int(caught.value.code), caught.value.message) == (
        -5,
        f"Multi threshold '<{len(key)} characters>' is not valid",
    )
    assert_secret_free(caught.value, [key])


def test_a_threshold_of_any_length_is_refused_with_its_length(node: Node) -> None:
    """A threshold of more digits than `int` converts is given by its length."""
    for threshold in (LONG_STEP, f"{ZEROS}4294967296"):
        assert refusal(node, "start", [f"wsh(multi({threshold},{KEY}))"]) == (
            -5,
            f"Multi threshold '<{len(threshold)} characters>' is not valid",
        )


@pytest.mark.parametrize(
    "text",
    [
        f"wpkh(\t{KEY})",
        f"wpkh(\n{KEY})",
        f"wpkh(\t{KEY})#qqqqqqqq",
        f"wpkh(\t{KEY})#" + "é" * 4,
    ],
    ids=["tab", "newline", "tab-and-checksum", "tab-and-checksum-of-8-bytes"],
)
def test_a_character_outside_the_descriptor_set_is_refused(
    node: Node, text: str
) -> None:
    """ISS 2551: Core's `DescriptorChecksum` refuses the payload.

    bitcoind v32.1.0 on regtest answered "Invalid characters in payload"
    for each.
    """
    assert refusal(node, "start", [text]) == (-5, "Invalid characters in payload")


@pytest.mark.parametrize("key", [TPRV, LEAK_WIF], ids=["tprv", "wif"])
def test_a_character_outside_the_descriptor_set_quotes_no_key(
    node: Node, key: str
) -> None:
    """The payload may hold a private key, which the refusal leaves out."""
    with pytest.raises(RpcError) as caught:
        scan(node, f"wpkh(\t{key})")
    assert (int(caught.value.code), caught.value.message) == (
        -5,
        "Invalid characters in payload",
    )
    assert_secret_free(caught.value, [key])


@pytest.mark.parametrize(
    "text, message",
    [
        (f"wpkh({KEY})#a#b", "Multiple '#' symbols"),
        (f"wpkh({KEY})#abc", "Expected 8 character checksum, not 3 characters"),
        (f"wpkh({KEY})#", "Expected 8 character checksum, not 0 characters"),
    ],
    ids=["two-hashes", "short-checksum", "empty-checksum"],
)
def test_a_second_hash_or_a_checksum_not_8_bytes_gets_core_s_message(
    node: Node, text: str, message: str
) -> None:
    """ISS 2649: Core's `CheckChecksum` refuses the `#` before the payload."""
    assert refusal(node, "start", [text]) == (-5, message)


@pytest.mark.parametrize(
    "text, message",
    [
        (f"wpkh(\t{KEY})#a#b", "Multiple '#' symbols"),
        (f"wpkh(\t{KEY})#abcd#efg", "Multiple '#' symbols"),
        (f"wpkh(\t{KEY})#abc", "Expected 8 character checksum, not 3 characters"),
        (
            f"wpkh(\t{KEY})#" + "é" * 8,
            "Expected 8 character checksum, not 16 characters",
        ),
    ],
    ids=[
        "two-hashes",
        "two-hashes-8-bytes-after-one",
        "short-checksum",
        "checksum-of-16-bytes",
    ],
)
def test_where_core_refuses_the_hash_before_the_payload_character(
    node: Node, text: str, message: str
) -> None:
    """ISS 2649: the `#` refusal comes before the character-set check."""
    assert refusal(node, "start", [text]) == (-5, message)


# The one case where Core's `CheckChecksum` does not refuse the payload's
# characters and does not refuse the `#` either: the checksum is 8 tabs,
# so 8 bytes, and the payload is in the set, so the message stays
# bip32_descriptors' via `strip_checksum`.
@pytest.mark.parametrize(
    "text",
    [
        pytest.param(f"wpkh({KEY})#" + "\t" * 8, id="tabs-as-checksum"),
    ],
)
def test_where_core_refuses_no_payload_character_the_message_is_the_parser_s(
    node: Node, text: str
) -> None:
    """ISS 2551: the message is bip32_descriptors', not the payload refusal."""
    with pytest.raises(BTClibValueError) as parsed:
        strip_checksum(text)
    assert refusal(node, "start", [text]) == (-5, str(parsed.value))


# Each where Core first refuses what `_parse` gives no message of its own
# for: an expression no function reads, before a key or not, a key origin
# or a key path, or a miniscript outside wsh() and tr(), which both refuse
# in the same words.
@pytest.mark.parametrize(
    "text",
    [
        f"tr({X_ONLY},{{foo(1),raw(51)}})",
        f"tr({X_ONLY},{{foo(1),pk({NOT_A_POINT_X})}})",
        f"tr({X_ONLY},{{foo(1),pk({LEAK_WIF})}})",
        f"foo(pk({NOT_A_POINT}))",
        f"tr({X_ONLY},{NOT_A_POINT})",
        f"tr({X_ONLY},foo)",
        f"wpkh( [deadbeef]{KEY})",
        f"wpkh({TPUB}/0/* )",
        f"wsh(multi(1,[deadbeef/2147483648]{KEY},{H160}))",
        f"wpkh([deadbeef]]{H160})",
        f"sh(and_v(v:pk({KEY}),pk({KEY2})))",
        f"and_v(v:pk({X_ONLY}),pk({KEY}))",
        f"and_v(v:pk({MUSIG}),pk({KEY2}))",
    ],
)
def test_a_refusal_core_meets_first_keeps_bip32_descriptors_message(
    node: Node, text: str
) -> None:
    """The message is bip32_descriptors'."""
    with pytest.raises(BTClibException) as parsed:
        parse(text, "regtest", {})
    assert refusal(node, "start", [text]) == (-5, str(parsed.value))


@pytest.mark.parametrize(
    "text",
    [
        f"pk({UNCOMPRESSED})",
        f"sh(pk({UNCOMPRESSED}))",
        f"sh(multi(1,{UNCOMPRESSED}))",
        f"combo({UNCOMPRESSED})",
        f"unused({UNCOMPRESSED})",
        f"pk({UNCOMPRESSED_WIF})",
        f"wpkh({wif_from_prv_key(LEAK_HEX, 'regtest')})",
        f"tr({MUSIG})",
        f"rawtr({MUSIG})",
        f"tr({X_ONLY},pk({MUSIG}))",
        f"tr({X_ONLY},multi_a(1,{MUSIG}))",
        f"tr(musig({TPUB},{TPUB}/1)/0/*)",
        f"tr(musig([deadbeef]{TPUB},{TPUB}/1)/0/*)",
        f"tr({X_ONLY},pk([deadbeef/0h/1]{X_ONLY2}))",
        f"and_v(v:pk({X_ONLY}),pk({KEY}))",
    ],
)
def test_a_key_or_musig_core_reads_has_no_refusal(text: str) -> None:
    """A key or a `musig()` that Core reads where it stands."""
    assert utxo_set._invalid_key_refusal(text, "regtest") is None


def test_an_unmatched_closing_parenthesis_keeps_the_parser_refusal(node: Node) -> None:
    """The key walker does not replace malformed-syntax diagnostics."""
    text = f"pk({KEY}))"
    assert utxo_set._invalid_key_refusal(text, "regtest") is None
    with pytest.raises(RpcError) as caught:
        scan(node, text)
    assert (int(caught.value.code), caught.value.message) == (-5, "unbalanced brackets")


@pytest.mark.parametrize(
    "text",
    [
        f"wsh(multi(1,[deadbeef/2147483648]{KEY},{H160}))",
        f"wsh(multi(1,[dead]{KEY},{H160}))",
        f"wsh(multi(1,[deadbeef]]{KEY},{H160}))",
    ],
)
def test_the_key_walk_stops_at_a_key_origin_core_refuses(text: str) -> None:
    """Core refuses the origin first, in words the walk does not give."""
    assert utxo_set._invalid_key_refusal(text, "regtest") is None


@pytest.mark.parametrize(
    "text",
    [NOT_A_POINT, f"{NOT_A_POINT},{KEY}", f"raw({NOT_A_POINT})", f"pk({KEY})"],
)
def test_a_text_with_no_key_that_is_no_point_has_no_such_refusal(text: str) -> None:
    """Keys outside a function, an argument that is no key, and a point."""
    assert utxo_set._invalid_key_refusal(text, "regtest") is None


def test_a_null_range_is_absent_and_scan_objects_are_read_in_order(node: Node) -> None:
    """A null `range` is the default, and an earlier refusal comes first."""
    assert scan(node, {"desc": "raw(51)", "range": None})["success"] is True
    assert refusal(node, "start", ["bogus", {"desc": 1}])[0] == -5


def test_a_scan_goes_on_between_steps(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expansion and the walk yield, and `status` reports the progress."""
    monkeypatch.setattr(utxo_set, "_STEP", 2)
    monkeypatch.setattr(utxo_set, "_EXPAND_STEP", 2)
    monkeypatch.setattr(utxo_set, "_PROGRESS_EVERY", 2)
    mine_to(node, f"pkh({KEY})", 6)
    job = scan_tx_out_set(
        node,
        CONN,
        ["start", [f"pkh({KEY})", {"desc": f"pkh({TPUB}/0/*)", "range": 5}]],
    )
    assert isinstance(job, Generator)
    seen = []
    result = None
    while result is None:
        try:
            next(job)
        except StopIteration as done:
            result = done.value
        else:
            seen.append(scan_tx_out_set(node, CONN, ["status"]))
    assert len(seen) > 3
    assert all(
        isinstance(status, dict) and 0 <= status["progress"] <= 100 for status in seen
    )
    assert result["success"] is True
    assert len(result["unspents"]) == 6
