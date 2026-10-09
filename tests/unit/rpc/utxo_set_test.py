# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What `scantxoutset` answers, called as `rpc.main` calls it.

The coins are coinbases the node mines to the scripts under test, on a
regtest node built and driven in this thread. The shapes, the refusals
and the descriptors are what `bitcoind` v31.1.0 answers for the same
call (`tests/integration/scantxoutset_test.py` asks it).
"""

from collections.abc import Generator
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.hashes import hash160
from btclib.script.script_pub_key import ScriptPubKey
from btclib_wallet.descriptors import Provider, multipath_descriptors, parse

from btclib_node.rpc import utxo_set
from btclib_node.rpc.callbacks import arg_names, callbacks
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT
from btclib_node.rpc.mining import generate_block
from btclib_node.rpc.utxo_set import scan_tx_out_set
from tests import finish

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

CONN = cast("RpcConnection", None)

KEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
KEY2 = "02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
X_ONLY = KEY[2:]
X_ONLY2 = KEY2[2:]
# the fingerprints `bitcoind` v31.1.0 infers for these keys
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
    # what bitcoind v31.1.0 answers
    assert descs(scan(node, {"desc": ranged, "range": [0, 1]})) == [
        f"wsh(multi(1,[aaaaaaaa/5/0/0]{CHILD_00},[bbbbbbbb/7/0/1]{CHILD_01}))#ry2d83lg",
        f"wsh(multi(1,[bbbbbbbb/7/0/1]{CHILD_01},[bbbbbbbb/7/0/1]{CHILD_01}))#5xzh3ty0",
    ]
    assert descs(scan(node, {"desc": ranged, "range": [1, 1]})) == [
        f"wsh(multi(1,[bbbbbbbb/7/5/0/1]{CHILD_01},[bbbbbbbb/7/5/0/1]{CHILD_01}))#q0ht8plu",
    ]


def test_a_key_s_origin_is_the_first_path_s(node: Node) -> None:
    """The providers of a multipath descriptor merge in its paths' order."""
    crossed = f"wsh(multi(1,[aaaaaaaa]{TPUB}/<0;1>/0,[bbbbbbbb]{TPUB}/<1;0>/0))"
    mine_scripts(node, crossed, 0)
    # what bitcoind v31.1.0 answers
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
    # what bitcoind v31.1.0 answers
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

    Core's rpc/util.cpp:1370 quotes the descriptor, private keys included;
    this tree does not, as Core's rpc/mining.cpp:229 does not.
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
        (
            ["start", None],
            -3,
            "JSON value of type null is not of expected type array",
        ),
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
