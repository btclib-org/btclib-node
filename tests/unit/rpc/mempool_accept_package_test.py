# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`testmempoolaccept` of several transactions, judged as one package.

`tests/integration/testmempoolaccept_test.py` holds the answers equal to
bitcoind v31.1's.
"""

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.exceptions import BTClibValueError
from btclib.script import script
from btclib.tx.tx import Tx

from btclib_node import Node, main
from btclib_node import mempool as mempool_module
from btclib_node.rpc import package as package_module
from btclib_node.rpc.callbacks import test_mempool_accept as mempool_accept
from tests.unit.main_test import (
    FEE,
    a_free_parent,
    child_of,
    funded_spends,
    hold,
    padded,
    with_script_sig,
)
from tests.unit.rpc.package_test import btc, fees_of, hexes, paying

if TYPE_CHECKING:
    from collections.abc import Callable

_CONN = cast("Any", None)


@pytest.fixture
def node(regtest_node: Callable[[], Node]) -> Node:
    """Give one header-synced regtest node, built fresh for the test."""
    return regtest_node()


def accept(node: Node, txs: list[Tx], *args: Any) -> list[dict[str, Any]]:
    """Return what `testmempoolaccept` answers for `txs`, and hold none."""
    before = dict(node.mempool.transactions), node.mempool.sequence
    answer = mempool_accept(node, _CONN, [hexes(txs), *args])
    assert (dict(node.mempool.transactions), node.mempool.sequence) == before
    return answer


def unfinished(tx: Tx, **extra: object) -> dict[str, Any]:
    """Return the entry of a transaction Core did not fully validate."""
    return {"txid": tx.id, "wtxid": tx.hash, **extra}


def allowed(entry: dict[str, Any], tx: Tx, fee: int = FEE) -> None:
    """Assert `entry` allows `tx`, at its own feerate over its own wtxid."""
    assert list(entry) == [
        "txid",
        "wtxid",
        "allowed",
        "vsize_adjusted",
        "vsize",
        "vsize_bip141",
        "fees",
    ]
    assert entry["allowed"] is True
    assert fees_of(entry) == {
        "base": btc(fee),
        "effective-feerate": btc(fee * 1000 // tx.vsize),
        "effective-includes": [tx.hash.hex()],
    }


def test_a_parent_and_its_child_are_allowed_each_at_its_own_feerate(
    node: Node,
) -> None:
    """Neither is added to the mempool, whose sequence does not move."""
    parent = funded_spends(node, 1)[0]
    child = child_of(parent)
    for entry, tx in zip(accept(node, [parent, child]), [parent, child], strict=True):
        allowed(entry, tx)


def test_transactions_that_do_not_spend_each_other_are_a_package(
    node: Node,
) -> None:
    """Core's `PackageTestAccept` asks no topology of a child with parents."""
    first, second = funded_spends(node, 2)
    for entry, tx in zip(accept(node, [first, second]), [first, second], strict=True):
        allowed(entry, tx)


def test_a_child_does_not_pay_for_its_parent(node: Node) -> None:
    """No package feerate: the parent is held to the floor alone."""
    parent = a_free_parent(node)
    child = child_of(parent)
    first, second = accept(node, [parent, child])
    assert first["allowed"] is False
    assert first["reject-reason"] == "min relay fee not met"
    assert first["reject-details"].startswith("min relay fee not met, 0 < ")
    assert second == unfinished(child)


def test_a_delta_is_in_the_floor_and_the_effective_feerate(node: Node) -> None:
    """`prioritisetransaction` lifts a free parent over the floor alone."""
    parent = a_free_parent(node)
    node.mempool.prioritise(parent.id, FEE)
    first, _ = accept(node, [parent, child_of(parent)])
    assert first["allowed"] is True
    assert fees_of(first)["base"] == btc(0)
    assert fees_of(first)["effective-feerate"] == btc(FEE * 1000 // parent.vsize)


def test_the_package_fee_floor_is_not_asked(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core skips it under `PackageTestAccept`: each pays its own floor.

    No answer tells the skip from the run, each floor being rounded up, so
    the sizes the floor is asked of are what is held: a submission asks it
    of the whole package too.
    """
    asked: list[int] = []
    check_fee_rate = main._check_fee_rate

    def spy(node: Node, vsize: int, fee: int) -> None:
        asked.append(vsize)
        check_fee_rate(node, vsize, fee)

    monkeypatch.setattr(main, "_check_fee_rate", spy)
    parent = funded_spends(node, 1)[0]
    child = child_of(parent)
    accept(node, [parent, child])
    assert asked == [parent.vsize, child.vsize]
    asked.clear()
    main.pre_verify_subpackage(node, [parent, child])
    assert asked == [parent.vsize + child.vsize]


def test_what_is_no_package_is_the_package_error_of_each(node: Node) -> None:
    """`IsWellFormedPackage`'s words, and nothing validated."""
    parent = funded_spends(node, 1)[0]
    child = child_of(parent)
    both = replace(child, vin=[*child.vin, *parent.vin])
    big_parent = padded(parent, 52_000)
    big_child = padded(child_of(big_parent), 52_000)
    cases = [
        ([child, parent], "package-not-sorted"),
        ([parent, parent, child], "package-contains-duplicates"),
        ([parent, both], "conflict-in-package"),
        ([big_parent, big_child], "package-too-large"),
    ]
    for txs, message in cases:
        answer = accept(node, txs)
        assert answer == [unfinished(tx, **{"package-error": message}) for tx in txs]


def test_a_truc_rule_of_the_package_is_the_package_error(node: Node) -> None:
    """Core's `PackageTRUCChecks`, with its details, and nothing validated."""
    parent = replace(funded_spends(node, 1)[0], version=3)
    child = child_of(parent)
    answer = accept(node, [parent, child])
    error = answer[0]["package-error"]
    assert error.startswith("TRUC-violation, non-version=3 tx ")
    assert answer == [
        unfinished(tx, **{"package-error": error}) for tx in [parent, child]
    ]


def test_a_cluster_over_the_limit_is_the_package_error(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `too-large-cluster`, with no details."""
    parent = funded_spends(node, 1)[0]
    child = child_of(parent)
    monkeypatch.setattr(mempool_module, "_CLUSTER_LIMIT", 1)
    answer = accept(node, [parent, child])
    error = {"package-error": "too-large-cluster"}
    assert answer == [unfinished(parent, **error), unfinished(child, **error)]


def test_a_conflict_with_a_held_transaction_is_no_replacement(node: Node) -> None:
    """`PackageTestAccept` allows none, whatever the fee."""
    held = hold(node, funded_spends(node, 1)[0])
    rival = paying(replace(held, lock_time=1), 50_000)
    child = child_of(rival)
    first, second = accept(node, [rival, child])
    assert first["reject-reason"] == "bip125-replacement-disallowed"
    assert first["reject-details"] == "bip125-replacement-disallowed"
    assert second == unfinished(child)


def test_a_held_transaction_is_refused_and_ends_the_package(node: Node) -> None:
    """Core's `PreChecks` refuses it, so the child is not validated."""
    parent = hold(node, funded_spends(node, 1)[0])
    child = child_of(parent)
    first, second = accept(node, [parent, child])
    assert first["reject-reason"] == "txn-already-in-mempool"
    assert second == unfinished(child)


def test_a_missing_input_ends_the_package_before_a_later_check(node: Node) -> None:
    """`PreChecks` asks `CheckTransaction` of each in its turn."""
    good, unsent = funded_spends(node, 2)
    orphan = child_of(unsent)
    empty = Tx(1, 0, unsent.vin, [], check_validity=False)
    first, second = accept(node, [orphan, empty])
    assert first == unfinished(
        orphan, allowed=False, **{"reject-reason": "missing-inputs"}
    )
    assert second == unfinished(empty)
    first, second = accept(node, [good, empty])
    assert first == unfinished(good)
    assert second["reject-reason"] == second["reject-details"] == "bad-txns-vout-empty"


def test_a_script_that_fails_keeps_the_verdicts_before_it(node: Node) -> None:
    """The scripts are checked in turn: those after the one failing are not."""
    failing = script.serialize([b"\x11" * 32, b"\x51"])
    parent = funded_spends(node, 1)[0]
    child = with_script_sig(child_of(parent), failing)
    first, second = accept(node, [parent, child])
    allowed(first, parent)
    assert second["allowed"] is False
    assert second["reject-reason"].startswith("mempool-script-verify-flag-failed (")
    parent = with_script_sig(parent, failing)
    child = child_of(parent)
    first, second = accept(node, [parent, child])
    assert first["reject-reason"].startswith("mempool-script-verify-flag-failed (")
    assert second == unfinished(child)


def test_a_script_fault_that_is_no_refusal_ends_the_call(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As it does for one transaction (btclib-org/btclib-node#668)."""
    parent = funded_spends(node, 1)[0]
    fault = BTClibValueError("not a refusal")
    monkeypatch.setattr(package_module, "check_package", lambda _: (1, fault))
    with pytest.raises(BTClibValueError, match="not a refusal"):
        accept(node, [parent, child_of(parent)])


def test_maxfeerate_leaves_the_entries_after_it_unanswered(node: Node) -> None:
    """Core's handler stops at the first transaction over the cap."""
    parent = paying(funded_spends(node, 1)[0], 50_000)
    child = child_of(parent)
    first, second = accept(node, [parent, child], "0.0001")
    assert first == unfinished(
        parent, allowed=False, **{"reject-reason": "max-fee-exceeded"}
    )
    assert second == unfinished(child)
    rich = paying(child, 500_000)
    first, second = accept(node, [parent, rich], "0.01")
    allowed(first, parent, FEE + 50_000)
    assert second["reject-reason"] == "max-fee-exceeded"
    for entry in accept(node, [parent, rich], 0):
        assert entry["allowed"] is True
