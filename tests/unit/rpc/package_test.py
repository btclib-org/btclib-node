# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`submitpackage`, and the package evaluation under it.

`tests/integration/submitpackage_test.py` holds the answers equal to
bitcoind v31.1's, in the shapes and the refusals each test here names. A
test says so where the answer is one that only bitcoind's is measured for.
"""

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from bitcoin_core_rpc import RPCErrorCode
from btclib.fee import FeeRate, fee_from_vsize
from btclib.script import script
from btclib.script.witness import Witness
from btclib.tx.limits import COINBASE_MATURITY
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import Node
from btclib_node import mempool as mempool_module
from btclib_node.chains import RegTest
from btclib_node.exceptions import (
    MissingPrevoutError,
    PackageRefusedError,
    TxRejectedError,
)
from btclib_node.main import (
    check_max_feerate,
    package_refusal,
    pre_verify_subpackage,
)
from btclib_node.rpc.callbacks import arg_names, callbacks
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT
from btclib_node.rpc.package import submit_package
from tests import (
    anyone_can_spend,
    anyone_can_spend_script_sig,
    generate_random_chain,
)
from tests.unit.main_test import (
    FEE,
    a_child_spending_the_dust,
    a_free_dust_parent,
    a_free_parent,
    child_of,
    connect,
    funded_spends,
    hold,
    padded,
    with_script_sig,
    with_two_outputs,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_CONN = cast("Any", None)


@pytest.fixture
def node(regtest_node: Callable[[], Node]) -> Node:
    """Give one header-synced regtest node, built fresh for the test."""
    return regtest_node()


def hexes(txs: list[Tx]) -> list[str]:
    """Return `txs` as `submitpackage` takes them."""
    return [
        tx.serialize(include_witness=True, check_validity=False).hex() for tx in txs
    ]


def submit(node: Node, txs: list[Tx], *args: Any) -> dict[str, Any]:
    """Return what `submitpackage` answers for `txs`."""
    return submit_package(node, _CONN, [hexes(txs), *args])


def refused(params: list[Any], node: Node | None = None) -> RpcError:
    """Return what `submitpackage` refuses `params` with."""
    with pytest.raises(RpcError) as raised:
        submit_package(cast("Any", node), _CONN, params)
    return raised.value


def btc(sats: int) -> str:
    """Return `sats` as the eight decimals Core's `ValueFromAmount` writes."""
    return f"{sats // 10**8}.{sats % 10**8:08d}"


def result(answer: dict[str, Any], tx: Tx) -> dict[str, Any]:
    """Return the entry of `tx` in `answer`'s `tx-results`."""
    entry: dict[str, Any] = answer["tx-results"][tx.hash.hex()]
    assert entry["txid"] == tx.id.hex()
    return entry


def fees_of(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the `fees` of `entry`, each amount as its text."""
    return {
        key: value.text if hasattr(value, "text") else value
        for key, value in entry["fees"].items()
    }


def free(spend: Tx) -> Tx:
    """Return `spend` paying no fee."""
    return replace(
        spend, vout=[replace(spend.vout[0], value=spend.vout[0].value + FEE)]
    )


def a_child_of_all(parents: list[Tx]) -> Tx:
    """Return a spend of the first output of each of `parents`, paying `FEE`."""
    vin = [
        TxIn(OutPoint(parent.id, 0), anyone_can_spend_script_sig(), 0xFFFFFFFF)
        for parent in parents
    ]
    value = sum(parent.vout[0].value for parent in parents) - FEE
    return Tx(1, 0, vin, [TxOut(value, anyone_can_spend())])


def announced(node: Node) -> list[bytes]:
    """Return the wtxids queued for announcement, in order."""
    return [wtxid for _, wtxid in node.download_manager.received_txs]


def test_it_is_served_under_the_name_and_arguments_core_gives() -> None:
    """Core's three arguments, in its category."""
    assert callbacks["submitpackage"] is submit_package
    assert arg_names["submitpackage"] == ("package", "maxfeerate", "maxburnamount")
    assert CATEGORY["submitpackage"] == "Rawtransactions"
    assert HELP_TEXT["submitpackage"].startswith(
        'submitpackage ["rawtx",...] ( maxfeerate maxburnamount )\n'
    )


def test_one_transaction_is_taken_and_announced(node: Node) -> None:
    """Its effective feerate is its own, rounded down, over its own wtxid."""
    tx = funded_spends(node, 1)[0]
    answer = submit(node, [tx])
    assert list(answer) == ["package_msg", "tx-results", "replaced-transactions"]
    assert answer["package_msg"] == "success"
    assert answer["replaced-transactions"] == []
    entry = result(answer, tx)
    assert list(entry) == ["txid", "vsize", "fees"]
    assert entry["vsize"] == tx.vsize
    assert fees_of(entry) == {
        "base": btc(FEE),
        "effective-feerate": btc(FEE * 1000 // tx.vsize),
        "effective-includes": [tx.hash.hex()],
    }
    assert list(entry["fees"]) == ["base", "effective-feerate", "effective-includes"]
    assert node.mempool.contains_tx(tx)
    assert announced(node) == [tx.hash]
    assert node.mempool.unbroadcast == set()


def test_a_held_transaction_answers_its_size_and_fee_only(node: Node) -> None:
    """It is not taken again, and is announced again."""
    tx = funded_spends(node, 1)[0]
    submit(node, [tx])
    answer = submit(node, [tx])
    assert answer["package_msg"] == "success"
    entry = result(answer, tx)
    assert list(entry) == ["txid", "vsize", "fees"]
    assert fees_of(entry) == {"base": btc(FEE)}
    assert announced(node) == [tx.hash, tx.hash]


def test_another_witness_of_a_held_transaction_is_answered_and_ignored(
    node: Node,
) -> None:
    """The mempool's wtxid is answered, and the mempool's copy is announced."""
    tx = funded_spends(node, 1)[0]
    submit(node, [tx])
    other = replace(tx, vin=[replace(tx.vin[0], script_witness=Witness([b"\x01"]))])
    assert other.id == tx.id
    assert other.hash != tx.hash
    answer = submit(node, [other])
    assert answer["package_msg"] == "success"
    entry = result(answer, other)
    assert entry == {"txid": tx.id.hex(), "other-wtxid": tx.hash.hex()}
    assert announced(node) == [tx.hash, tx.hash]


def test_a_child_pays_for_a_parent_the_floor_refuses(node: Node) -> None:
    """Both are taken, each with the package's feerate and both wtxids."""
    parent = a_free_parent(node)
    child = child_of(parent)
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "success"
    size = parent.vsize + child.vsize
    package = {
        "effective-feerate": btc(FEE * 1000 // size),
        "effective-includes": [parent.hash.hex(), child.hash.hex()],
    }
    assert fees_of(result(answer, parent)) == {"base": btc(0), **package}
    assert fees_of(result(answer, child)) == {"base": btc(FEE), **package}
    assert node.mempool.contains_tx(parent)
    assert node.mempool.contains_tx(child)
    assert announced(node) == [parent.hash, child.hash]


def test_two_parents_a_child_pays_for_are_taken_with_it(node: Node) -> None:
    """The package is the three, the child spending both."""
    first, second = (free(parent) for parent in funded_spends(node, 2))
    child = a_child_of_all([first, second])
    answer = submit(node, [first, second, child])
    assert answer["package_msg"] == "success"
    for tx in (first, second, child):
        assert result(answer, tx)["fees"]["effective-includes"] == [
            first.hash.hex(),
            second.hash.hex(),
            child.hash.hex(),
        ]
        assert node.mempool.contains_tx(tx)
    assert announced(node) == [first.hash, second.hash, child.hash]


def test_a_parent_that_passes_alone_is_taken_alone(node: Node) -> None:
    """Its feerate is its own, and the other parent's is the package's."""
    paying, spare = funded_spends(node, 2)
    nothing = free(spare)
    child = a_child_of_all([paying, nothing])
    answer = submit(node, [paying, nothing, child])
    assert answer["package_msg"] == "success"
    assert result(answer, paying)["fees"]["effective-includes"] == [paying.hash.hex()]
    package = [nothing.hash.hex(), child.hash.hex()]
    assert result(answer, nothing)["fees"]["effective-includes"] == package
    assert result(answer, child)["fees"]["effective-includes"] == package


def test_a_child_is_taken_alone_where_its_parent_is_held(node: Node) -> None:
    """The held parent answers its entry, with no effective feerate."""
    parent = hold(node, funded_spends(node, 1)[0])
    child = child_of(parent)
    answer = submit(node, [parent, child])
    assert fees_of(result(answer, parent)) == {"base": btc(FEE)}
    assert fees_of(result(answer, child))["effective-includes"] == [child.hash.hex()]


def test_a_child_that_cannot_pay_for_the_parent_that_is_not_held_is_refused(
    node: Node,
) -> None:
    """The package is held to the floor by its total fee."""
    parent = a_free_parent(node)
    free = child_of(parent)
    child = replace(free, vout=[replace(free.vout[0], value=free.vout[0].value + FEE)])
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "transaction failed"
    floor = fee_from_vsize(parent.vsize + child.vsize, FeeRate(sats_per_kvbyte=100))
    assert result(answer, parent)["error"] == f"min relay fee not met, 0 < {floor // 2}"
    assert result(answer, child)["error"] == f"min relay fee not met, 0 < {floor}"
    assert node.mempool.size == 0
    assert announced(node) == []


def test_a_refusal_that_a_child_cannot_undo_ends_the_package(node: Node) -> None:
    """The others are still tried alone: one that passes is taken."""
    bad, good = funded_spends(node, 2)
    bad = replace(bad, version=4)
    child = a_child_of_all([bad, good])
    answer = submit(node, [bad, good, child])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, bad)["error"] == "version"
    assert result(answer, good)["fees"]["effective-includes"] == [good.hash.hex()]
    assert result(answer, child)["error"] == "bad-txns-inputs-missingorspent"
    assert node.mempool.contains_tx(good)
    assert not node.mempool.contains_tx(bad)
    assert announced(node) == [good.hash]


def test_a_refusal_alone_that_is_the_package_s_too_is_not_the_package_s(
    node: Node,
) -> None:
    """The package is not tried, so TRUC's rule is not its message."""
    held_spend, spare = funded_spends(node, 2)
    held = hold(node, held_spend)
    breaking = child_of(held, version=3)
    nothing = free(spare)
    child = a_child_of_all([nothing, breaking])
    answer = submit(node, [nothing, breaking, child])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, breaking)["error"].startswith("TRUC-violation, ")
    assert result(answer, nothing)["error"].startswith("min relay fee not met")
    assert result(answer, child)["error"] == "bad-txns-inputs-missingorspent"


def test_a_lone_refusal_is_a_failed_package(node: Node) -> None:
    """A single transaction is not tried again as a package."""
    tx = replace(funded_spends(node, 1)[0], version=4)
    answer = submit(node, [tx])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, tx) == {"txid": tx.id.hex(), "error": "version"}


def test_a_lone_fee_floor_refusal_is_a_failed_package(node: Node) -> None:
    """The floor is one a child undoes, and a lone transaction has none."""
    tx = a_free_parent(node)
    answer = submit(node, [tx])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, tx)["error"].startswith("min relay fee not met, 0 < ")


def test_a_child_whose_parent_is_not_there_is_a_missing_input(node: Node) -> None:
    """Both answer Core's reason: neither input is there."""
    parent = child_of(a_free_parent(node))
    child = child_of(parent)
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "transaction failed"
    for tx in (parent, child):
        assert result(answer, tx)["error"] == "bad-txns-inputs-missingorspent"


def test_a_failure_of_the_checks_of_a_transaction_is_its_own(node: Node) -> None:
    """`CheckTransaction`'s reason, bare, as Core's `ToString` of it is."""
    tx = funded_spends(node, 1)[0]
    empty = Tx(tx.version, tx.lock_time, tx.vin, [], check_validity=False)
    answer = submit(node, [empty])
    assert result(answer, empty)["error"] == "bad-txns-vout-empty"


def test_a_package_that_is_not_one_has_no_answer_for_its_transactions(
    node: Node,
) -> None:
    """`IsWellFormedPackage`'s message, and `package-not-validated` for each."""
    parent = free(funded_spends(node, 1)[0])
    child = child_of(parent)
    both = replace(child, vin=[*child.vin, *parent.vin])
    cases = [
        ([parent, parent, child], "package-contains-duplicates"),
        ([parent, both], "conflict-in-package"),
    ]
    for txs, message in cases:
        answer = submit(node, txs)
        assert answer["package_msg"] == message
        for tx in txs:
            error = {"txid": tx.id.hex(), "error": "package-not-validated"}
            assert result(answer, tx) == error
    assert node.mempool.size == 0


def test_a_package_over_the_weight_has_no_answer_for_its_transactions(
    node: Node,
) -> None:
    """Over `MAX_PACKAGE_WEIGHT`, though each is within a transaction's."""
    parent = padded(funded_spends(node, 1)[0], 52_000)
    child = padded(child_of(parent), 52_000)
    assert parent.weight + child.weight > 404_000
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "package-too-large"
    for tx in (parent, child):
        error = {"txid": tx.id.hex(), "error": "package-not-validated"}
        assert result(answer, tx) == error
    assert node.mempool.size == 0


def test_a_package_that_is_not_a_child_with_its_parents_is_refused(node: Node) -> None:
    """The topology is `-25`, in Core's words, before anything is verified."""
    first, second = funded_spends(node, 2)
    child = child_of(first)
    for txs in ([first, second], [child, first], [first, first]):
        error = refused([hexes(txs)], node)
        assert error.code == RPCErrorCode.VERIFY_ERROR
        assert error.message == (
            "package topology disallowed. not child-with-parents or parents "
            "depend on each other."
        )
    chain = child_of(first)
    tip = a_child_of_all([chain, second])
    assert refused([hexes([first, chain, second, tip])], node).code == -25
    assert node.mempool.size == 0


def test_the_arguments_are_refused_in_core_s_order(node: Node) -> None:
    """Each with its code and its words, `bitcoind` v31.1's."""
    tx = funded_spends(node, 1)[0]
    raw = hexes([tx])[0]
    usage = refused([], node)
    assert usage.code == RPCErrorCode.MISC_ERROR
    assert usage.message == HELP_TEXT["submitpackage"]
    wrong_type = refused(["x"], node)
    assert wrong_type.code == RPCErrorCode.TYPE_ERROR
    assert "Position 1 (package)" in wrong_type.message
    assert (
        "JSON value of type string is not of expected type array" in wrong_type.message
    )
    for count in (0, 26):
        count_error = refused([[raw] * count, "x"], node)
        assert count_error.code == RPCErrorCode.INVALID_PARAMETER
        assert (
            count_error.message == "Array must contain between 1 and 25 transactions."
        )
    assert refused([[1], "x"], node).message == "Invalid amount"
    rate = refused([[raw], 1, "x"], node)
    assert rate.code == RPCErrorCode.INVALID_PARAMETER
    assert rate.message == "Fee rates larger than or equal to 1BTC/kvB are not accepted"
    assert refused([[1], 0.1, "x"], node).message == "Invalid amount"
    assert refused([[raw], 0.1, -1], node).message == "Amount out of range"
    for element in (1, None):
        not_string = refused([[raw, element]], node)
        assert not_string.code == RPCErrorCode.TYPE_ERROR
        assert (
            not_string.message
            == "JSON value of type "
            + ("number" if element == 1 else "null")
            + " is not of expected type string"
        )
    undecoded = refused([[raw, "zz"]], node)
    assert undecoded.code == RPCErrorCode.DESERIALIZATION_ERROR
    assert undecoded.message == (
        "TX decode failed: zz Make sure the tx has at least one input."
    )
    assert node.mempool.size == 0


def test_a_burn_over_the_limit_is_refused_before_anything_is_taken(node: Node) -> None:
    """Core's `MAX_BURN_EXCEEDED`, `-25`, whichever transaction burns."""
    paying, burning = funded_spends(node, 2)
    burning = replace(
        burning, vout=[*burning.vout, TxOut(100, bytes([0x6A, 0x01, 0x07]))]
    )
    burn_message = (
        "Unspendable output exceeds maximum configured by user (maxburnamount)"
    )
    for burn_limit in (None, 0, "0.00000099"):
        error = refused([hexes([paying, burning]), 0.1, burn_limit], node)
        assert error.code == RPCErrorCode.VERIFY_ERROR
        assert error.message == burn_message
    assert node.mempool.size == 0
    answer = submit(node, [burning], 0.1, "0.000001")
    assert answer["package_msg"] == "success"


def test_a_modified_feerate_over_maxfeerate_is_refused_alone(node: Node) -> None:
    """The limit is on each transaction, and zero is no limit."""
    tx = funded_spends(node, 1)[0]
    answer = submit(node, [tx], "0.00001")
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, tx)["error"] == "max feerate exceeded"
    assert submit(node, [tx], 0)["package_msg"] == "success"


def test_a_child_over_maxfeerate_is_refused_in_the_package(node: Node) -> None:
    """The parent that the floor refused keeps that answer."""
    parent = a_free_parent(node)
    child = child_of(parent)
    answer = submit(node, [parent, child], "0.00001")
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, parent)["error"].startswith("min relay fee not met")
    assert result(answer, child)["error"] == "max feerate exceeded"
    assert node.mempool.size == 0


@pytest.mark.parametrize(
    ("fee", "limit", "exceeded"),
    [(960, 10_000, False), (961, 10_000, True), (960, 9_999, True), (10**9, 0, False)],
)
def test_the_feerate_is_compared_as_a_fraction(
    node: Node, fee: int, limit: int, *, exceeded: bool
) -> None:
    """960 satoshi for 96 vbytes is 10,000 per kvB, which 9,999 is under."""
    tx = funded_spends(node, 1)[0]
    if exceeded:
        with pytest.raises(TxRejectedError, match="max feerate exceeded"):
            check_max_feerate(node, tx, fee, 96, limit)
    else:
        check_max_feerate(node, tx, fee, 96, limit)


def test_the_delta_of_a_transaction_is_in_its_feerate(node: Node) -> None:
    """The limit is on the modified fee, which `prioritisetransaction` moves."""
    tx = funded_spends(node, 1)[0]
    check_max_feerate(node, tx, 960, 96, 10_000)
    node.mempool.prioritise(tx.id, 1)
    with pytest.raises(TxRejectedError, match="max feerate exceeded"):
        check_max_feerate(node, tx, 960, 96, 10_000)


def test_the_floor_is_held_to_the_modified_fee_of_the_package(node: Node) -> None:
    """A delta on the child pays for the parent, and is in the feerate."""
    parent = a_free_parent(node)
    free = child_of(parent)
    child = replace(free, vout=[replace(free.vout[0], value=free.vout[0].value + FEE)])
    floor = fee_from_vsize(parent.vsize + child.vsize, node.config.min_relay_feerate)
    node.mempool.prioritise(child.id, floor)
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "success"
    size = parent.vsize + child.vsize
    assert fees_of(result(answer, parent))["effective-feerate"] == btc(
        floor * 1000 // size
    )
    assert fees_of(result(answer, child))["base"] == btc(0)


def test_the_rules_that_refuse_a_package_as_a_whole_keep_each_answer(
    node: Node,
) -> None:
    """TRUC's, in Core's words, and the cluster limit's."""
    parent = a_free_parent(node)
    child = child_of(parent, version=3)
    answer = submit(node, [parent, child])
    assert answer["package_msg"].startswith("TRUC-violation, ")
    assert "cannot spend from non-version=3" in answer["package_msg"]
    assert result(answer, parent)["error"].startswith("min relay fee not met")
    assert result(answer, child)["error"] == "bad-txns-inputs-missingorspent"


def test_a_cluster_over_the_limit_refuses_the_package(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`too-large-cluster` is the message, as it is Core's."""
    parent = a_free_parent(node)
    child = child_of(parent)
    monkeypatch.setattr(mempool_module, "_CLUSTER_LIMIT", 1)
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "too-large-cluster"
    assert result(answer, child)["error"] == "bad-txns-inputs-missingorspent"
    assert node.mempool.size == 0


def test_a_truc_rule_against_a_held_parent_is_the_transactions_own(
    node: Node,
) -> None:
    """Core's `PreChecks`: "transaction failed", and the child's error."""
    parent = replace(a_free_parent(node), version=3)
    child = padded(child_of(parent, version=3), 12_955)
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "transaction failed"
    error = result(answer, child)["error"]
    assert error.startswith("TRUC-violation, version=3 tx ")
    assert error.endswith("is too big: 12955 > 10000 virtual bytes")
    assert result(answer, parent)["error"].startswith("min relay fee not met")
    rich = paying(child, 500_000)
    limited = submit(node, [parent, rich], "0.0001")
    assert result(limited, rich)["error"].startswith("TRUC-violation, ")


def test_a_truc_rule_against_a_held_non_truc_parent_is_the_transactions_own(
    node: Node,
) -> None:
    """A version 3 child of a held parent that is not, and of a free one."""
    held_spend, free_spend = funded_spends(node, 2)
    held = hold(node, held_spend)
    other = replace(free(free_spend), version=3)
    child = replace(a_child_of_all([held, other]), version=3)
    answer = submit(node, [other, child])
    assert answer["package_msg"] == "transaction failed"
    assert "cannot spend from non-version=3" in result(answer, child)["error"]


def package_truc(
    node: Node, package: list[Tx], index: int, vsize: int = 100
) -> str | None:
    """Return the words `check_package_truc` refuses `package[index]` in."""
    try:
        node.mempool.check_package_truc(package, index, vsize)
    except TxRejectedError as refusal:
        return str(refusal)
    return None


def test_a_package_truc_refusal_follows_core_s_order(node: Node) -> None:
    """`PackageTRUCChecks`, rule by rule, in the order Core asks them."""
    first, plain = funded_spends(node, 2)
    v3 = replace(with_two_outputs(first), version=3)
    both = replace(a_child_of_all([v3, plain]), version=3)
    assert package_truc(node, [v3], 0) is None
    assert str(package_truc(node, [v3], 0, 10_001)).startswith("TRUC-violation, ")
    assert "is too big: 10001 > 10000" in str(package_truc(node, [v3], 0, 10_001))
    # the ancestors come before the version of the parent
    assert "would have too many ancestors" in str(
        package_truc(node, [v3, plain, both], 2)
    )
    # then the size of a child, before the version of its parent
    child = child_of(plain, version=3)
    assert "is too big: 1001 > 1000" in str(
        package_truc(node, [plain, child], 1, 1_001)
    )
    assert "cannot spend from non-version=3" in str(
        package_truc(node, [plain, child], 1)
    )
    # a sibling, and a child, in the package
    one, two = child_of(v3, version=3), child_of(v3, 1, version=3)
    assert "descendant count limit" in str(package_truc(node, [v3, one, two], 1))
    grandchild = child_of(one, version=3)
    assert "would have too many ancestors" in str(
        package_truc(node, [v3, one, grandchild], 1)
    )
    # a non-version-3 transaction cannot spend from a version 3 one
    assert "non-version=3 tx" in str(package_truc(node, [v3, child_of(v3)], 1))


def test_a_package_truc_refusal_reads_the_held_parents(node: Node) -> None:
    """The held parent's ancestors and children count, as in Core."""
    first, second = funded_spends(node, 2)
    held = hold(node, replace(with_two_outputs(first), version=3))
    held_child = hold(node, child_of(held, version=3))
    # a child of a held parent that has a child already
    other = child_of(held, 1, version=3)
    assert "descendant count limit" in str(package_truc(node, [other], 0))
    # a child of a held parent that has an ancestor of its own
    grandchild = child_of(held_child, version=3)
    assert "would have too many ancestors" in str(package_truc(node, [grandchild], 0))
    # a non-version-3 child of a held version 3 parent
    assert "non-version=3 tx" in str(package_truc(node, [child_of(held)], 0))
    # a held parent with no child takes one
    lone = hold(node, replace(second, version=3))
    assert package_truc(node, [child_of(lone, version=3)], 0) is None


def test_maxfeerate_is_asked_before_the_truc_rules(node: Node) -> None:
    """Core's `PreChecks` and `maxfeerate` of each come before the rules."""
    parent = a_free_parent(node)
    child = paying(child_of(parent, version=3), 5_000)
    answer = submit(node, [parent, child], "0.0001")
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, child)["error"] == "max feerate exceeded"


def test_the_truc_rules_are_asked_before_the_package_fee_floor(node: Node) -> None:
    """A package that breaks both is refused for the rules."""
    parent = a_free_parent(node)
    child = free(child_of(parent, version=3))
    answer = submit(node, [parent, child])
    assert answer["package_msg"].startswith("TRUC-violation, ")


def test_a_conflict_is_asked_before_the_cluster_limit(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflict and a large cluster are refused for the conflict."""
    held = hold(node, funded_spends(node, 1)[0])
    rival = replace(
        held,
        lock_time=1,
        vout=[replace(held.vout[0], value=held.vout[0].value + 1)],
    )
    monkeypatch.setattr(mempool_module, "_CLUSTER_LIMIT", 1)
    answer = submit(node, [rival, child_of(rival)])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, rival)["error"].startswith("insufficient fee, ")


def test_the_package_fee_floor_is_asked_before_the_cluster_limit(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A package that pays too little is refused for that, not its cluster."""
    parent = a_free_parent(node)
    child = free(child_of(parent))
    monkeypatch.setattr(mempool_module, "_CLUSTER_LIMIT", 1)
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, child)["error"].startswith("min relay fee not met")


def test_the_package_fee_floor_is_asked_before_the_dust_a_child_leaves(
    node: Node,
) -> None:
    """A package that pays too little is refused for that, not its dust."""
    parent = a_free_dust_parent(node)
    leaving = free(child_of(parent))
    answer = submit(node, [parent, leaving])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, leaving)["error"].startswith("min relay fee not met")


def test_dust_a_child_leaves_unspent_is_the_children_refusal(node: Node) -> None:
    """The message is Core's `unspent-dust`, and the answer the child's own."""
    parent = a_free_dust_parent(node)
    leaving = child_of(parent)
    answer = submit(node, [parent, leaving])
    assert answer["package_msg"] == "unspent-dust"
    assert result(answer, leaving)["error"].startswith("missing-ephemeral-spends, tx ")
    assert result(answer, parent)["error"].startswith("min relay fee not met")
    taken = submit(node, [parent, a_child_spending_the_dust(parent)])
    assert taken["package_msg"] == "success"


def test_a_script_that_fails_in_a_package_is_that_transaction_s_refusal(
    node: Node,
) -> None:
    """The reason is the one a lone transaction gets, and nothing is taken."""
    parent = a_free_parent(node)
    child = with_script_sig(child_of(parent), script.serialize([b"\x11" * 32, b"\x51"]))
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, child)["error"].startswith(
        "mempool-script-verify-flag-failed ("
    )
    assert result(answer, parent)["error"].startswith("min relay fee not met")
    assert node.mempool.size == 0


def test_a_conflict_is_refused_as_a_lone_transaction_is(node: Node) -> None:
    """Package replacement is btclib-org/btclib-node#1334.

    `bitcoind` v31.1 answers the pair `package RBF failed: insufficient
    anti-DoS fees`, and each transaction as here.
    """
    held = hold(node, funded_spends(node, 1)[0])
    rival = replace(
        held,
        lock_time=1,
        vout=[replace(held.vout[0], value=held.vout[0].value + 1)],
    )
    child = child_of(rival)
    answer = submit(node, [rival, child])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, rival)["error"].startswith("insufficient fee, ")
    assert answer["replaced-transactions"] == []
    assert node.mempool.contains_tx(held)
    paying = replace(held, lock_time=2, vout=[replace(held.vout[0], value=1_000)])
    alone = submit(node, [paying])
    assert result(alone, paying)["error"] == "bip125-replacement-disallowed"


def test_a_child_conflicting_with_a_held_transaction_is_missing_inputs(
    node: Node,
) -> None:
    """ISS 1792: Core's `PackageRBFChecks` has no result for either.

    Each keeps its answer from alone, the child's a missing input.
    `bitcoind` v31.1 answers the pair `package RBF failed: insufficient
    anti-DoS fees`; the message is btclib-org/btclib-node#1334.
    """
    first, second = funded_spends(node, 2)
    held = hold(node, first)
    value = second.vout[0].value + FEE
    parent = replace(second, vout=[replace(second.vout[0], value=value)])
    child = child_of(parent)
    child = replace(
        child,
        vin=[*child.vin, held.vin[0]],
        vout=[replace(child.vout[0], value=child.vout[0].value + held.vout[0].value)],
    )
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, parent)["error"].startswith("min relay fee not met")
    assert result(answer, child)["error"] == "bad-txns-inputs-missingorspent"
    assert node.mempool.size == 1
    assert node.mempool.contains_tx(held)


def test_a_transaction_the_trimmed_mempool_does_not_keep_is_refused_mempool_full(
    node: Node,
) -> None:
    """Core's last pass over the results."""
    tx = funded_spends(node, 1)[0]
    node.mempool.bytesize_limit = 1
    answer = submit(node, [tx])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, tx)["error"] == "mempool full"
    assert announced(node) == []
    assert node.mempool.size == 0


def test_a_package_the_trimmed_mempool_does_not_keep_is_refused_mempool_full(
    node: Node,
) -> None:
    """Each member of it, and nothing is held."""
    parent = a_free_parent(node)
    child = child_of(parent)
    node.mempool.bytesize_limit = 1
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "transaction failed"
    for member in (parent, child):
        assert result(answer, member)["error"] == "mempool full"
    assert node.mempool.size == 0


def test_the_checks_of_a_package_are_its_members_own(node: Node) -> None:
    """`pre_verify_subpackage` names the transaction it refuses, one entry."""
    first, second = funded_spends(node, 2)
    first = replace(
        first, vout=[replace(first.vout[0], value=first.vout[0].value + FEE)]
    )
    second = replace(second, version=4)
    child = a_child_of_all([first, second])
    with pytest.raises(PackageRefusedError) as refused_package:
        pre_verify_subpackage(node, [first, second, child])
    assert list(refused_package.value.errors) == [second.hash]
    assert isinstance(refused_package.value.errors[second.hash], TxRejectedError)
    with pytest.raises(PackageRefusedError) as missing:
        pre_verify_subpackage(node, [first, child_of(a_free_parent(node))])
    assert isinstance(next(iter(missing.value.errors.values())), MissingPrevoutError)


def test_package_refusal_is_core_s_is_well_formed_package() -> None:
    """Its words, in its order, for what is decided without the chain."""
    tx = Tx(
        1,
        0,
        [TxIn(OutPoint(b"\x01" * 32, 0), b"", 0xFFFFFFFF)],
        [TxOut(1, anyone_can_spend())],
    )
    spend = Tx(
        1,
        0,
        [TxIn(OutPoint(tx.id, 0), b"", 0xFFFFFFFF)],
        [TxOut(1, anyone_can_spend())],
    )
    assert package_refusal([tx]) is None
    assert package_refusal([tx, spend]) is None
    assert package_refusal([tx, tx]) == "package-contains-duplicates"
    assert package_refusal([spend, tx]) == "package-not-sorted"
    rival = replace(tx, lock_time=1)
    assert package_refusal([tx, rival]) == "conflict-in-package"
    big = SimpleNamespace(weight=404_001, id=b"a", vin=[])
    assert package_refusal([cast("Any", big)]) is None
    assert package_refusal([cast("Any", big), tx]) == "package-too-large"
    exact = SimpleNamespace(weight=404_000 - tx.weight, id=b"b", vin=[])
    assert package_refusal([cast("Any", exact), tx]) is None


def paying(spend: Tx, extra: int) -> Tx:
    """Return `spend` with `extra` satoshi more fee."""
    out = spend.vout[0]
    return replace(spend, vout=[replace(out, value=out.value - extra), *spend.vout[1:]])


def barely_paying(spend: Tx, vsize: int) -> Tx:
    """Return `spend` paying `vsize` satoshi, the relay floor for it."""
    return paying(free(spend), vsize)


@pytest.mark.xfail(
    strict=True,
    reason="eviction scores the parent alone, not its chunk: "
    "btclib-org/btclib-node#1740",
)
def test_a_child_pays_for_a_parent_a_full_mempool_would_have_refused(
    node: Node,
) -> None:
    """Core trims once at the end, so the parent taken alone is kept."""
    held_spend, parent_spend = funded_spends(node, 2)
    held = hold(node, padded(held_spend, 400))
    parent = barely_paying(parent_spend, 119)
    child = paying(child_of(parent), 100_000)
    node.mempool.bytesize_limit = 450
    answer = submit(node, [parent, child])
    kept = node.mempool.transactions.keys() >= {parent.hash, child.hash}
    assert (answer["package_msg"], held.hash in node.mempool.transactions, kept) == (
        "success",
        False,
        True,
    )


def test_a_free_parent_and_its_child_make_room_in_a_full_mempool(node: Node) -> None:
    """`add_package` scores the package whole, and evicts what pays less."""
    held_spend, parent_spend = funded_spends(node, 2)
    held = hold(node, padded(held_spend, 400))
    parent = free(parent_spend)
    child = paying(child_of(parent), 100_000)
    node.mempool.bytesize_limit = parent.vsize + child.vsize + 10
    answer = submit(node, [parent, child])
    assert answer["package_msg"] == "success"
    assert held.hash not in node.mempool.transactions
    assert node.mempool.transactions.keys() == {parent.hash, child.hash}


def test_a_parent_taken_alone_is_still_there_for_its_child(node: Node) -> None:
    """The parent taken alone is there when its child is tried."""
    held_spend, parent_spend = funded_spends(node, 2)
    hold(node, padded(held_spend, 400))
    parent = barely_paying(parent_spend, 119)
    child = paying(child_of(parent), 100_000)
    node.mempool.bytesize_limit = 450
    answer = submit(node, [parent, child])
    assert result(answer, child).get("error") != "bad-txns-inputs-missingorspent"


@pytest.mark.xfail(
    strict=True,
    reason="eviction scores the parent alone, not its chunk, and so does "
    "`add_package`'s room-making, which would have to score chunks too: "
    "btclib-org/btclib-node#1740",
)
def test_a_parent_taken_alone_is_not_evicted_for_its_package(node: Node) -> None:
    """The room a package makes is made once every transaction is in."""
    held_spend, alone_spend, free_spend = funded_spends(node, 3)
    held = hold(node, padded(held_spend, 400))
    alone = barely_paying(alone_spend, 119)
    other = free(free_spend)
    child = paying(a_child_of_all([alone, other]), 100_000)
    node.mempool.bytesize_limit = alone.vsize + other.vsize + child.vsize + 10
    answer = submit(node, [alone, other, child])
    kept = node.mempool.transactions.keys() >= {alone.hash, other.hash, child.hash}
    assert (answer["package_msg"], held.hash in node.mempool.transactions, kept) == (
        "success",
        False,
        True,
    )


def test_nothing_is_evicted_until_the_caller_trims(node: Node) -> None:
    """`add_tx` takes `trim`, and `trim` evicts."""
    mempool = node.mempool
    first, second = funded_spends(node, 2)
    mempool.bytesize_limit = 1
    assert mempool.add_tx(first, FEE, first.vsize, trim=False)
    assert mempool.add_tx(second, FEE, second.vsize, trim=False)
    assert mempool.size == 2
    mempool.trim()
    assert mempool.size == 0


def a_confirmed_transaction(node: Node) -> Tx:
    """Connect a chain and return a transaction in it with an unspent output."""
    chain = generate_random_chain(COINBASE_MATURITY + 1, RegTest().genesis.hash)
    connect(node, chain)
    return chain[-1].transactions[1]


def test_a_confirmed_transaction_is_already_known(node: Node) -> None:
    """Its inputs are spent, which no package undoes."""
    confirmed = a_confirmed_transaction(node)
    answer = submit(node, [confirmed])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, confirmed)["error"] == "txn-already-known"


def test_a_confirmed_parent_ends_the_package_but_for_its_child(node: Node) -> None:
    """The child is tried alone, as Core tries it."""
    confirmed = a_confirmed_transaction(node)
    child = child_of(confirmed)
    answer = submit(node, [confirmed, child])
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, confirmed)["error"] == "txn-already-known"
    assert "error" not in result(answer, child)
    assert child.hash in node.mempool.transactions


def test_maxfeerate_is_asked_before_the_dust_a_child_leaves(node: Node) -> None:
    """Core refuses the child's feerate where it would refuse its dust."""
    parent = a_free_dust_parent(node)
    leaving = paying(child_of(parent), 500_000)
    answer = submit(node, [parent, leaving], "0.0001")
    assert answer["package_msg"] == "transaction failed"
    assert result(answer, leaving)["error"] == "max feerate exceeded"
    assert node.mempool.size == 0
