# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Mempool`'s replacement rules, each refusal and what each lets through.

`tests/integration/replacement_test.py` holds the answers equal to
bitcoind v31.1's.
"""

import secrets
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.fee import fee_from_vsize
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node import mempool as mempool_module
from btclib_node.exceptions import TxRejectedError
from btclib_node.log import Logger
from btclib_node.main import verify_mempool_acceptance
from btclib_node.mempool import Mempool
from btclib_node.p2p.callbacks import _replaced_txs
from btclib_node.rpc.callbacks import send_raw_transaction
from btclib_node.rpc.callbacks import test_mempool_accept as mempool_accept
from tests import anyone_can_spend, anyone_can_spend_script_sig
from tests.unit.main_test import (
    FEE,
    child_of,
    funded_spends,
    hold,
    with_two_outputs,
)
from tests.unit.rpc.package_test import hexes, paying

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from btclib_node import Node

_CONN = cast("Any", None)

Outpoint = tuple[bytes, int]


def a_coin() -> Outpoint:
    """Return an outpoint nothing in the mempool spends."""
    return secrets.token_bytes(32), 0


def a_spend(outpoints: list[Outpoint], *, outputs: int = 1, version: int = 1) -> Tx:
    """Return a transaction spending exactly `outpoints`."""
    return Tx(
        version=version,
        lock_time=0,
        vin=[
            TxIn(OutPoint(txid, vout), script.serialize([secrets.token_bytes(32)]))
            for txid, vout in outpoints
        ],
        vout=[
            TxOut(1, script.serialize([secrets.token_bytes(32)]))
            for _ in range(outputs)
        ],
    )


def held(mempool: Mempool, outpoints: list[Outpoint], fee: int, **kwargs: int) -> Tx:
    """Hold a spend of `outpoints` paying `fee`, and return it."""
    tx = a_spend(outpoints, **kwargs)
    assert mempool.add_tx(tx, fee)
    return tx


def coins(tx: Tx) -> list[Outpoint]:
    """Return the outpoints `tx` spends."""
    return [(vin.prev_out.tx_id, vin.prev_out.vout) for vin in tx.vin]


def relay(mempool: Mempool, tx: Tx) -> int:
    """Return the incremental relay fee for `tx`'s own vsize."""
    return fee_from_vsize(tx.vsize, mempool.incremental_relay_feerate)


@pytest.fixture
def mempool() -> Mempool:
    """Give an empty mempool."""
    return Mempool(Logger(debug=True))


def singletons(mempool: Mempool, count: int) -> list[Tx]:
    """Hold `count` spends of a coin each, each its own cluster, at 1000."""
    return [held(mempool, [a_coin()], 1_000) for _ in range(count)]


def test_conflicts_in_100_clusters_are_replaced(mempool: Mempool) -> None:
    """Core's rule 5 bounds the clusters, `MAX_REPLACEMENT_CANDIDATES`."""
    spends = singletons(mempool, 100)
    candidate = a_spend([coins(spend)[0] for spend in spends])
    replaced = mempool.check_replacement(candidate, 10**6, candidate.vsize)
    assert replaced == {spend.hash for spend in spends}


def test_conflicts_in_101_clusters_are_too_many(mempool: Mempool) -> None:
    """Core's words, with the count, whatever the fee."""
    spends = singletons(mempool, 101)
    candidate = a_spend([coins(spend)[0] for spend in spends])
    with pytest.raises(TxRejectedError) as refused:
        mempool.check_replacement(candidate, 10**9, candidate.vsize)
    assert refused.value.reason == "too many potential replacements"
    assert refused.value.details == (
        f"rejecting replacement {candidate.id.hex()}; too many conflicting "
        "clusters (101 > 100)"
    )


def test_two_conflicts_of_one_cluster_count_once(mempool: Mempool) -> None:
    """101 conflicts in 100 clusters are not too many."""
    spends = singletons(mempool, 99)
    parent = held(mempool, [a_coin()], 1_000)
    coin = a_coin()
    child = held(mempool, [(parent.id, 0), coin], 1_000)
    spent = [coins(spend)[0] for spend in [*spends, parent]]
    candidate = a_spend([*spent, coin])
    assert len(mempool.direct_conflicts(candidate)) == 101
    replaced = mempool.check_replacement(candidate, 10**6, candidate.vsize)
    assert child.hash in replaced


def test_a_sibling_counts_as_a_conflict(mempool: Mempool) -> None:
    """With the sibling, rule 5 and rules 3 and 4 say so."""
    spends = singletons(mempool, 100)
    sibling = held(mempool, [a_coin()], 1_000)
    candidate = a_spend([coins(spend)[0] for spend in spends])
    with pytest.raises(TxRejectedError) as refused:
        mempool.check_replacement(
            candidate, 10**9, candidate.vsize, sibling=sibling.hash
        )
    assert refused.value.reason == (
        "too many potential replacements (including sibling eviction)"
    )
    alone = a_spend([a_coin()])
    with pytest.raises(TxRejectedError) as refused:
        mempool.check_replacement(alone, 1_000, alone.vsize, sibling=sibling.hash)
    assert refused.value.reason == "insufficient fee (including sibling eviction)"
    assert refused.value.reconsiderable
    fee = 1_000 + relay(mempool, alone)
    assert mempool.check_replacement(alone, fee, alone.vsize, sibling=sibling.hash) == {
        sibling.hash
    }


def test_a_replacement_past_the_cluster_limit_is_refused(
    mempool: Mempool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's limits with the change staged: what goes is not counted.

    A candidate replacing one transaction and spending a held parent joins
    the parent's cluster, which is at the limit, and is refused. One that
    replaces the parent's child takes its place, and is not.
    """
    monkeypatch.setattr(mempool_module, "_CLUSTER_LIMIT", 2)
    parent = held(mempool, [a_coin()], 1_000, outputs=2)
    coin = a_coin()
    child = held(mempool, [(parent.id, 0), coin], 1_000)
    other = held(mempool, [a_coin()], 1_000)
    joining = a_spend([coins(other)[0], (parent.id, 1)])
    with pytest.raises(TxRejectedError) as refused:
        mempool.check_replacement(joining, 10**6, joining.vsize)
    assert str(refused.value) == "too-large-cluster"
    taking_its_place = a_spend([coin, (parent.id, 1)])
    replaced = mempool.check_replacement(
        taking_its_place, 10**6, taking_its_place.vsize
    )
    assert replaced == {child.hash}


def test_a_replacement_that_does_not_improve_the_diagram_is_refused(
    mempool: Mempool,
) -> None:
    """Core's `ImprovesFeerateDiagram`, after rules 3 and 4 pass.

    A larger candidate paying just past rule 4 has the lower feerate, so
    the diagram is worse at the size of what it replaces.
    """
    conflict = held(mempool, [a_coin()], 10_000)
    candidate = a_spend(coins(conflict))
    vsize = 10 * candidate.vsize
    fee = 10_000 + fee_from_vsize(vsize, mempool.incremental_relay_feerate)
    with pytest.raises(TxRejectedError) as refused:
        mempool.check_replacement(candidate, fee, vsize, 4 * vsize)
    assert refused.value.reason == "replacement-failed"
    assert refused.value.details == (
        "insufficient feerate: does not improve feerate diagram"
    )
    assert refused.value.reconsiderable
    assert mempool.check_replacement(candidate, fee, candidate.vsize)


def test_the_diagram_counts_the_chunk_of_a_held_parent(mempool: Mempool) -> None:
    """A fee that replaces alone does not where it pays for a parent too.

    The candidate's own feerate is above the conflict's, so only its chunk
    with the parent refuses it.
    """
    conflict = held(mempool, [a_coin()], 10_000)
    parent = held(mempool, [a_coin()], 0)
    alone = a_spend(coins(conflict))
    fee = 10_000 + relay(mempool, alone) + 10
    assert mempool.check_replacement(alone, fee, alone.vsize) == {conflict.hash}
    with_parent = a_spend([*coins(conflict), (parent.id, 0)])
    fee = 10_000 * with_parent.vsize // conflict.vsize + 100
    with pytest.raises(TxRejectedError, match="replacement-failed"):
        mempool.check_replacement(with_parent, fee, with_parent.vsize)


def test_a_spend_of_what_it_replaces_is_refused(mempool: Mempool) -> None:
    """Core's `EntriesAndTxidsDisjoint`, over every held ancestor.

    It names the first ancestor in internal txid order that is a conflict.
    """
    conflict = held(mempool, [a_coin()], 1_000, outputs=2)
    child = held(mempool, [(conflict.id, 0)], 1_000)
    for spent in [(conflict.id, 1), (child.id, 0)]:
        candidate = a_spend([*coins(conflict), spent])
        with pytest.raises(TxRejectedError) as refused:
            mempool.check_spends_conflicts(candidate)
        assert refused.value.reason == "bad-txns-spends-conflicting-tx"
        assert refused.value.details == (
            f"{candidate.id.hex()} spends conflicting transaction {conflict.id.hex()}"
        )
    unrelated = held(mempool, [a_coin()], 1_000)
    candidate = a_spend([*coins(conflict), (unrelated.id, 0)])
    mempool.check_spends_conflicts(candidate)
    with pytest.raises(TxRejectedError, match="spends conflicting"):
        mempool.check_spends_conflicts(a_spend([(unrelated.id, 0)]), unrelated.hash)
    mempool.check_spends_conflicts(a_spend([(unrelated.id, 0)]))


def test_a_replacement_the_trim_takes_has_still_replaced(mempool: Mempool) -> None:
    """Core removes the conflicts before `LimitMempoolSize` trims."""
    conflict = held(mempool, [a_coin()], 1_000)
    candidate = a_spend(coins(conflict))
    mempool.bytesize_limit = 0
    assert not mempool.add_tx(candidate, 10**6, None, None, {conflict.hash})
    assert mempool.size == 0
    assert mempool.outpoint_spender == {}


def test_what_a_relayed_replacement_replaced_is_kept_by_internal_txid(
    mempool: Mempool,
) -> None:
    """Core's `setEntries` order, whatever order the wtxids come in."""
    first = held(mempool, [a_coin()], 1_000)
    # a second txid whose internal order is not its displayed one
    second = next(
        tx
        for tx in (a_spend([a_coin()]) for _ in range(64))
        if (tx.id < first.id) != (tx.id[::-1] < first.id[::-1])
    )
    assert mempool.add_tx(second, 1_000)
    expected = sorted([first, second], key=lambda tx: tx.id[::-1])
    node = cast("Node", SimpleNamespace(mempool=mempool))
    wtxids = [tx.hash for tx in reversed(expected)]
    assert _replaced_txs(node, wtxids) == expected


def test_a_package_replaces_first(mempool: Mempool) -> None:
    """`add_package` takes out what the package replaces before it adds."""
    conflict = held(mempool, [a_coin()], 1_000)
    parent = a_spend(coins(conflict))
    child = a_spend([(parent.id, 0)])
    members = [(parent, 0, parent.vsize, None), (child, 10**6, child.vsize, None)]
    assert mempool.add_package(members, height=0, replaced={conflict.hash}) == [
        True,
        True,
    ]
    assert not mempool.contains_tx(conflict)


def package(
    mempool: Mempool, conflict: Tx, parent_fee: int, child_fee: int
) -> tuple[Tx, Tx, list[tuple[Tx, int, int, int | None]]]:
    """Return a parent conflicting with `conflict`, its child, as members."""
    parent = a_spend(coins(conflict))
    child = a_spend([(parent.id, 0)])
    members: list[tuple[Tx, int, int, int | None]] = [
        (parent, parent_fee, parent.vsize, None),
        (child, child_fee, child.vsize, None),
    ]
    return parent, child, members


def refusal(
    mempool: Mempool, members: Sequence[tuple[Tx, int, int, int | None]]
) -> str:
    """Return how `check_package_replacement` refuses `members`."""
    with pytest.raises(TxRejectedError) as refused:
        mempool.check_package_replacement(members)
    return str(refused.value)


def test_a_package_that_pays_replaces(mempool: Mempool) -> None:
    """Core's `PackageRBFChecks`: the conflict and its descendants go."""
    conflict = held(mempool, [a_coin()], 1_000)
    descendant = held(mempool, [(conflict.id, 0)], 1_000)
    _, _, members = package(mempool, conflict, 0, 10_000)
    replaced = mempool.check_package_replacement(members)
    assert replaced == {conflict.hash, descendant.hash}
    assert mempool.check_package_replacement(members[1:]) == frozenset()


def test_a_package_replacement_is_one_parent_and_its_child(
    mempool: Mempool,
) -> None:
    """Core refuses any other shape, and a member with a held parent."""
    conflict = held(mempool, [a_coin()], 1_000)
    parent, child, members = package(mempool, conflict, 0, 10_000)
    third = (a_spend([(child.id, 0)]), 0, 100, None)
    failed = "package RBF failed: "
    assert refusal(mempool, [*members, third]) == (
        failed + "package must be 1-parent-1-child"
    )
    holder = held(mempool, [a_coin()], 1_000)
    child = a_spend([(parent.id, 0), (holder.id, 0)])
    assert refusal(mempool, [members[0], (child, 10_000, child.vsize, None)]) == (
        failed + "new transaction cannot have mempool ancestors"
    )


def test_a_package_replacing_too_many_clusters_is_refused(mempool: Mempool) -> None:
    """Rule 5 over both members' conflicts, the child's txid named."""
    spends = singletons(mempool, 101)
    parent = a_spend([coins(spend)[0] for spend in spends[:50]])
    child = a_spend([(parent.id, 0), *(coins(spend)[0] for spend in spends[50:])])
    members = [(parent, 0, parent.vsize, None), (child, 10**9, child.vsize, None)]
    assert refusal(mempool, members) == (
        "package RBF failed: too many potential replacements, rejecting "
        f"replacement {child.id.hex()}; too many conflicting clusters (101 > 100)"
    )


def test_a_package_short_of_the_fees_it_replaces_is_refused(
    mempool: Mempool,
) -> None:
    """Rules 3 and 4 over the package's modified fees and its vsize."""
    conflict = held(mempool, [a_coin()], 10_000)
    _, child, members = package(mempool, conflict, 0, 9_999)
    assert refusal(mempool, members) == (
        "package RBF failed: insufficient anti-DoS fees, rejecting replacement "
        f"{child.id.hex()}, less fees than conflicting txs; 0.00009999 < 0.0001"
    )
    vsize = sum(vsize for _, _, vsize, _ in members)
    mempool.prioritise(
        child.id, 1 + fee_from_vsize(vsize, mempool.incremental_relay_feerate)
    )
    # the delta counts: past rules 3 and 4, the diagram is what refuses it
    assert "insufficient feerate" in refusal(mempool, members)


def test_a_package_no_better_than_its_parent_is_refused(mempool: Mempool) -> None:
    """The child has to raise the parent's feerate, in Core's words."""
    conflict = held(mempool, [a_coin()], 1_000)
    parent, child, members = package(mempool, conflict, 10_000, 0)
    vsize = parent.vsize + child.vsize
    rate = 10_000 * 1000 // vsize
    parent_rate = 10_000 * 1000 // parent.vsize
    assert refusal(mempool, members) == (
        "package RBF failed: package feerate is less than or equal to parent "
        f"feerate, package feerate 0.{rate:08d} BTC/kvB <= parent feerate is "
        f"0.{parent_rate:08d} BTC/kvB"
    )


def test_a_package_as_good_as_its_parent_is_refused(mempool: Mempool) -> None:
    """Equal feerates are refused too, Core's `<=`."""
    conflict = held(mempool, [a_coin()], 1_000)
    parent, child, _ = package(mempool, conflict, 0, 0)
    members: list[tuple[Tx, int, int, int | None]] = [
        (parent, parent.vsize * 1_000, parent.vsize, None),
        (child, child.vsize * 1_000, child.vsize, None),
    ]
    assert refusal(mempool, members) == (
        "package RBF failed: package feerate is less than or equal to parent "
        "feerate, package feerate 0.01000000 BTC/kvB <= parent feerate is "
        "0.01000000 BTC/kvB"
    )


def test_a_package_replacement_past_the_cluster_limit_is_refused(
    mempool: Mempool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent and its child are two, over a limit of one."""
    conflict = held(mempool, [a_coin()], 1_000)
    _, _, members = package(mempool, conflict, 0, 10_000)
    monkeypatch.setattr(mempool_module, "_CLUSTER_LIMIT", 1)
    assert refusal(mempool, members) == "too-large-cluster"


def test_a_package_that_does_not_improve_the_diagram_is_refused(
    mempool: Mempool,
) -> None:
    """Core's reason names the diagram, with no details."""
    conflict = held(mempool, [a_coin()], 10_000)
    _, _, members = package(mempool, conflict, 4_000, 7_000)
    assert refusal(mempool, members) == (
        "package RBF failed: insufficient feerate: does not improve feerate diagram"
    )


@pytest.mark.parametrize(
    ("fee", "vsize", "text"),
    [
        (1_000, 100, "0.00010000 BTC/kvB"),
        (2, 3, "0.00000666 BTC/kvB"),
        (10**8, 1, "1000.00000000 BTC/kvB"),
        (-5, 1_000, "0.-0000005 BTC/kvB"),
        (-(10**8 + 5), 1_000, "-1.-0000005 BTC/kvB"),
        (-1, 3, "0.-0000334 BTC/kvB"),
    ],
)
def test_a_fee_rate_is_written_as_core_s_cfeerate(
    fee: int, vsize: int, text: str
) -> None:
    """`GetFeePerK` rounds down, then C++ splits toward zero."""
    assert mempool_module._fee_rate_text(fee, vsize) == text


def a_version_3_family(mempool: Mempool) -> tuple[Tx, Tx]:
    """Hold a version 3 parent with one child, and return both."""
    parent = held(mempool, [a_coin()], 1_000, outputs=2, version=3)
    child = held(mempool, [(parent.id, 0)], 1_000, version=3)
    return parent, child


def test_a_second_child_is_offered_its_sibling_to_evict(mempool: Mempool) -> None:
    """Core's `SingleTRUCChecks`, with sibling eviction allowed."""
    parent, child = a_version_3_family(mempool)
    second = a_spend([(parent.id, 1)], version=3)
    assert mempool.check_truc(second, second.vsize, sibling_eviction=True) == (
        child.hash
    )
    with pytest.raises(TxRejectedError, match="would exceed descendant count"):
        mempool.check_truc(second, second.vsize)


def test_no_sibling_is_offered_of_a_parent_with_two_children(
    mempool: Mempool,
) -> None:
    """Which to evict would be a choice, so Core offers none."""
    parent = held(mempool, [a_coin()], 1_000, outputs=3, version=3)
    held(mempool, [(parent.id, 0)], 1_000, version=3)
    held(mempool, [(parent.id, 1)], 1_000, version=3)
    third = a_spend([(parent.id, 2)], version=3)
    with pytest.raises(TxRejectedError, match="would exceed descendant count"):
        mempool.check_truc(third, third.vsize, sibling_eviction=True)


def test_no_sibling_is_offered_that_has_another_parent(mempool: Mempool) -> None:
    """A sibling with a second held parent has three ancestors, not two."""
    parent = held(mempool, [a_coin()], 1_000, outputs=2, version=3)
    other = held(mempool, [a_coin()], 1_000, version=3)
    held(mempool, [(parent.id, 0), (other.id, 0)], 1_000, version=3)
    second = a_spend([(parent.id, 1)], version=3)
    with pytest.raises(TxRejectedError, match="would exceed descendant count"):
        mempool.check_truc(second, second.vsize, sibling_eviction=True)


@pytest.fixture
def node(regtest_node: Callable[[], Node]) -> Node:
    """Give one header-synced regtest node, built fresh for the test."""
    return regtest_node()


def test_sendrawtransaction_replaces_what_it_pays_for(node: Node) -> None:
    """The conflict goes, and the replacement is held and announced."""
    held = hold(node, funded_spends(node, 1)[0])
    rival = paying(replace(held, lock_time=1), 10 * FEE)
    answer = send_raw_transaction(node, _CONN, hexes([rival]))
    assert answer == rival.id.hex()
    assert node.mempool.contains_tx(rival)
    assert not node.mempool.contains_tx(held)


def test_testmempoolaccept_allows_a_lone_replacement_and_keeps_both(
    node: Node,
) -> None:
    """Core's `SingleAccept` with `test_accept`: allowed, nothing changed."""
    held = hold(node, funded_spends(node, 1)[0])
    rival = paying(replace(held, lock_time=1), 10 * FEE)
    before = dict(node.mempool.transactions), node.mempool.sequence
    (verdict,) = mempool_accept(node, _CONN, [hexes([rival])])
    assert verdict["allowed"] is True
    assert (dict(node.mempool.transactions), node.mempool.sequence) == before


def test_a_spend_of_its_own_conflict_is_refused_after_the_replacement_checks(
    node: Node,
) -> None:
    """Core's `bad-txns-spends-conflicting-tx`, once the fees pass."""
    held = hold(node, with_two_outputs(funded_spends(node, 1)[0]))
    rival = paying(
        replace(
            held,
            vin=[*held.vin, TxIn(OutPoint(held.id, 1), anyone_can_spend_script_sig())],
            vout=[TxOut(held.vout[0].value + held.vout[1].value, anyone_can_spend())],
        ),
        10 * FEE,
    )
    with pytest.raises(TxRejectedError) as refused:
        verify_mempool_acceptance(node, rival)
    assert str(refused.value) == (
        f"bad-txns-spends-conflicting-tx, {rival.id.hex()} spends conflicting "
        f"transaction {held.id.hex()}"
    )


def test_a_replacement_is_held_to_the_cluster_limit_without_what_it_replaces(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second child of a parent at the limit takes the first one's place."""
    parent = hold(node, with_two_outputs(funded_spends(node, 1)[0]))
    first = hold(node, child_of(parent, 0))
    monkeypatch.setattr(mempool_module, "_CLUSTER_LIMIT", 2)
    second = child_of(parent, 1)
    with pytest.raises(TxRejectedError, match="too-large-cluster"):
        verify_mempool_acceptance(node, second)
    rival = paying(replace(first, lock_time=1), 10 * FEE)
    assert verify_mempool_acceptance(node, rival).replaced == {first.hash}
