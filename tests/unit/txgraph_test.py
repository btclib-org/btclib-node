# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.txgraph`.

`test_a_chain_chunks_as_core_s_does` is Core's `txgraph_chunk_chain`
(`src/test/txgraph_tests.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
tag). The property holds the graph to `linearize` and `post_linearize`
run from scratch on each cluster, which is what an optimal cluster's
order is whatever path led to it.
"""

from __future__ import annotations

import random
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from btclib_node.cluster_linearize import (
    DepGraph,
    FeeFrac,
    linearize,
    post_linearize,
)
from btclib_node.txgraph import Quality, TxGraph


def a_graph() -> TxGraph[Any]:
    """Return a graph that works each cluster to the end, seeded."""
    return TxGraph(acceptable_cost=100_000_000, rng=random.Random(0))


def test_a_chain_chunks_as_core_s_does() -> None:
    """Core's `txgraph_chunk_chain`, read through `mining_order`."""
    graph = a_graph()

    def chunks() -> list[list[str]]:
        return [chunk.refs for chunk in graph.mining_order()]

    graph.add_transaction("A", FeeFrac(2, 10), "A")
    assert chunks() == [["A"]]
    graph.add_transaction("B", FeeFrac(1, 10), "B")
    graph.add_dependency("A", "B")
    assert chunks() == [["A"], ["B"]]
    graph.add_transaction("C", FeeFrac(2, 10), "C")
    graph.add_dependency("B", "C")
    assert chunks() == [["A"], ["B", "C"]]
    graph.add_transaction("D", FeeFrac(4, 10), "D")
    graph.add_dependency("C", "D")
    assert chunks() == [["A", "B", "C", "D"]]
    # D still depends on A through C, the parent B taken out between
    graph.remove_transaction("B")
    assert len(graph) == 3
    assert graph.cluster("A") == ["A", "C", "D"]
    graph.remove_transaction("C")
    graph.remove_transaction("D")
    assert chunks() == [["A"]]


def test_equal_chunks_go_by_their_greatest_key_then_their_own_prefix() -> None:
    """Core's chunk index: the equal-feerate prefix, then the fallback."""
    graph = a_graph()
    # one cluster of two equal-feerate chunks, and a single of that feerate
    graph.add_transaction("p", FeeFrac(10, 10), "p")
    graph.add_transaction("c", FeeFrac(10, 10), "c")
    graph.add_dependency("p", "c")
    graph.add_transaction("s", FeeFrac(20, 20), "a")
    # `c` and `s` each close 20 units of that feerate, and `s`'s key is lower
    assert [chunk.refs for chunk in graph.mining_order()] == [["p"], ["s"], ["c"]]
    keys = graph.order_keys(["s", "c", "p"])
    assert sorted(keys, key=keys.__getitem__) == ["p", "s", "c"]
    # a smaller prefix goes first whatever its key
    graph.add_transaction("t", FeeFrac(5, 5), "z")
    assert [chunk.refs[0] for chunk in graph.mining_order()] == ["t", "p", "s", "c"]


def test_a_chunk_of_two_goes_by_its_greatest_key() -> None:
    """Core's `m_main_max_chunk_fallback`: the greatest key, not the least."""
    graph = a_graph()
    # a child paying for its parent, and a single of the pair's feerate
    graph.add_transaction("p", FeeFrac(5, 10), "a")
    graph.add_transaction("c", FeeFrac(15, 10), "z")
    graph.add_dependency("p", "c")
    graph.add_transaction("s", FeeFrac(20, 20), "m")
    assert [chunk.refs for chunk in graph.mining_order()] == [["s"], ["p", "c"]]


def test_a_skipped_chunk_leaves_out_the_rest_of_its_cluster() -> None:
    """Core's `BlockBuilder::Skip`: the later chunks of that cluster only."""
    graph = a_graph()
    # a chain of three chunks, and a single between its first two
    graph.add_transaction("A", FeeFrac(30, 10), "A")
    graph.add_transaction("B", FeeFrac(10, 10), "B")
    graph.add_transaction("C", FeeFrac(5, 10), "C")
    graph.add_dependency("A", "B")
    graph.add_dependency("B", "C")
    graph.add_transaction("S", FeeFrac(20, 10), "S")
    graph.add_transaction("T", FeeFrac(1, 10), "T")
    builder = graph.block_builder()
    taken = []
    for chunk in builder:
        taken.append(chunk.refs)
        if chunk.refs == ["B"]:
            builder.skip()
    assert taken == [["A"], ["S"], ["B"], ["T"]]
    assert [chunk.refs for chunk in graph.mining_order()] == [
        ["A"],
        ["S"],
        ["B"],
        ["C"],
        ["T"],
    ]


def test_a_dependency_on_itself_is_ignored() -> None:
    """Core's `AddDependency` does nothing for a dependency on self."""
    graph = a_graph()
    graph.add_transaction("A", FeeFrac(1, 1), "A")
    graph.add_dependency("A", "A")
    cluster, _ = graph._locator["A"]
    assert cluster.quality == Quality.OPTIMAL
    assert graph._relinearize(cluster, 10**9) == (0, False)


def test_a_fee_change_relinearizes_its_cluster() -> None:
    """Core's `SetTransactionFee`: no change, a single, and a cluster."""
    graph = a_graph()
    graph.add_transaction("p", FeeFrac(0, 10), "p")
    graph.add_transaction("c", FeeFrac(30, 10), "c")
    graph.add_dependency("p", "c")
    graph.add_transaction("s", FeeFrac(1, 10), "s")
    assert graph.do_work(10**9)
    graph.set_fee("p", 0)
    graph.set_fee("s", 5)
    assert graph.do_work(0)
    assert [chunk.feerate for chunk in graph.chunks("p")] == [FeeFrac(30, 20)]
    graph.set_fee("p", 100)
    assert not graph.do_work(0)
    assert [chunk.feerate for chunk in graph.chunks("p")] == [
        FeeFrac(100, 10),
        FeeFrac(30, 10),
    ]
    assert graph.chunk_feerate("c") == FeeFrac(30, 10)


def test_work_stops_at_the_budget() -> None:
    """`do_work` stops where its budget runs out, and resumes."""
    graph = a_graph()
    for i in range(8):
        graph.add_transaction(i, FeeFrac(i, 1), i)
        if i:
            graph.add_dependency(i - 1, i)
    assert not graph.do_work(0)
    assert not graph.do_work(1)
    assert graph.do_work(10**9)
    assert graph.do_work(0)


def test_a_cluster_short_of_work_is_still_acceptable() -> None:
    """Below the acceptable cost a cluster is relinearized no further."""
    graph: TxGraph[int] = TxGraph(acceptable_cost=1, rng=random.Random(1))
    for i in range(8):
        graph.add_transaction(i, FeeFrac(i % 3, 1), i)
        if i:
            graph.add_dependency(i // 2, i)
    assert graph.cluster(0)[0] == 0
    cluster, _ = graph._locator[0]
    assert cluster.quality == Quality.ACCEPTABLE
    # the acceptable cluster is not improved by a budget below its cost
    assert not graph.do_work(1)


def test_a_relinearization_that_gets_nowhere_ends_the_work() -> None:
    """Core's `DoWork` stops at a cluster that was not improved."""
    graph: TxGraph[int] = TxGraph(acceptable_cost=10**9, rng=random.Random(2))
    for i in range(8):
        graph.add_transaction(i, FeeFrac(i % 3, 1), i)
        if i:
            graph.add_dependency(i // 2, i)
    graph.cluster(0)
    graph.remove_transaction(7)
    assert not graph.do_work(1)


def test_the_cost_default_is_core_s() -> None:
    """The default graph draws its seeds from the operating system."""
    graph: TxGraph[int] = TxGraph()
    assert graph.acceptable_cost == 75_000
    assert isinstance(graph._rng, random.SystemRandom)


# --- Core's `Trim` ---


def _chain(graph: TxGraph[str], refs: str, fee: int) -> None:
    for i, ref in enumerate(refs):
        graph.add_transaction(ref, FeeFrac(fee, 10), ref)
        if i:
            graph.add_dependency(refs[i - 1], ref)


def test_a_spend_is_linked_once_its_dependency_is_added() -> None:
    """`linked` answers the graph's clusters, and yes for what it lacks."""
    graph = a_graph()
    graph.add_transaction("p", FeeFrac(1, 10), "p")
    graph.add_transaction("c", FeeFrac(1, 10), "c")
    assert not graph.linked("p", "c")
    assert graph.linked("p", "staged")
    assert graph.linked("staged", "c")
    graph.trim([("p", "c")], 64, 10**6)
    assert graph.linked("p", "c")


def test_a_trim_within_the_limits_only_adds_the_dependencies() -> None:
    """Nothing goes, and the clusters merge."""
    graph = a_graph()
    _chain(graph, "ab", 1)
    graph.add_transaction("p", FeeFrac(1, 10), "p")
    assert graph.trim([("p", "a")], 3, 30) == []
    assert graph.cluster("b") == ["p", "a", "b"]


def test_a_trim_keeps_the_higher_chunk_feerate() -> None:
    """Of two chains under one parent, the cheaper one goes, child too.

    Which dependency comes first does not matter.
    """
    for dependencies in ([("p", "a"), ("p", "x")], [("p", "x"), ("p", "a")]):
        graph = a_graph()
        _chain(graph, "ab", 1)
        _chain(graph, "xy", 5)
        graph.add_transaction("p", FeeFrac(1, 10), "p")
        assert graph.trim(dependencies, 3, 10**6) == ["a", "b"]
        assert graph.cluster("p") == ["p", "x", "y"]
        assert "a" not in graph
        assert "b" not in graph


def test_a_trim_goes_by_size_too() -> None:
    """Core's `m_max_cluster_size`: a part past it is not taken."""
    graph = a_graph()
    _chain(graph, "ab", 1)
    graph.add_transaction("p", FeeFrac(1, 10), "p")
    assert graph.trim([("p", "a")], 64, 29) == ["b"]


def test_equal_feerates_go_by_the_smaller_chunk_then_the_lower_key() -> None:
    """FeeFrac's order puts the smaller chunk first; equal chunks, the key."""
    graph = a_graph()
    graph.add_transaction("p", FeeFrac(1, 10), "p")
    graph.add_transaction("big", FeeFrac(2, 20), "a")
    graph.add_transaction("small", FeeFrac(1, 10), "z")
    assert graph.trim([("p", "big"), ("p", "small")], 2, 10**6) == ["big"]
    graph = a_graph()
    graph.add_transaction("p", FeeFrac(1, 10), "p")
    graph.add_transaction("x", FeeFrac(1, 10), "b")
    graph.add_transaction("y", FeeFrac(1, 10), "a")
    assert graph.trim([("p", "x"), ("p", "y")], 2, 10**6) == ["x"]


def test_a_transaction_past_the_size_limit_goes_with_what_follows_it() -> None:
    """An oversized transaction is never taken, nor what comes after it."""
    graph = a_graph()
    graph.add_transaction("p", FeeFrac(1, 10), "p")
    _chain(graph, "ab", 1)
    graph.add_transaction("huge", FeeFrac(1_000, 100), "h")
    graph.add_dependency("huge", "a")
    assert graph.trim([("p", "a")], 64, 50) == ["huge", "a", "b"]
    assert len(graph) == 1


# --- against `linearize` from scratch ---


def _reference(
    feerates: dict[int, FeeFrac], parents: dict[int, set[int]], refs: list[int]
) -> list[int]:
    """Return the optimal order of `refs`, one cluster."""
    depgraph = DepGraph()
    position = {ref: depgraph.add_transaction(feerates[ref]) for ref in refs}
    for ref in refs:
        mask = sum(1 << position[p] for p in parents[ref] if p in position)
        depgraph.add_dependencies(mask, position[ref])
    lin, optimal, _ = linearize(depgraph, 10**12, 0, refs.__getitem__)
    assert optimal
    post_linearize(depgraph, lin)
    return [refs[i] for i in lin]


@st.composite
def histories(draw: st.DrawFn) -> list[tuple[str, int, int, set[int]]]:
    """Draw additions, each with parents among those held, and removals."""
    steps: list[tuple[str, int, int, set[int]]] = []
    held: dict[int, set[int]] = {}
    for ref in range(draw(st.integers(1, 14))):
        if bool(held) and draw(st.booleans()) and draw(st.booleans()):
            gone = draw(st.sampled_from(sorted(held)))
            steps.append(("remove", gone, 0, set()))
            for r in _with_descendants(held, gone):
                del held[r]
        fee = draw(st.integers(-5, 40))
        choices = sorted(held)
        parents = (
            set(draw(st.lists(st.sampled_from(choices), max_size=3))) if held else set()
        )
        steps.append(("add", ref, fee, parents))
        held[ref] = parents
    return steps


def _with_descendants(parents: dict[int, set[int]], ref: int) -> set[int]:
    gone = {ref}
    while more := {r for r, ps in parents.items() if ps & gone} - gone:
        gone |= more
    return gone


@given(histories(), st.integers(0, 2**32))
def test_every_cluster_ends_in_the_order_linearize_gives(
    steps: list[tuple[str, int, int, set[int]]], seed: int
) -> None:
    """Whatever the history, each optimal cluster is ordered as from scratch.

    A removal takes the descendants too, as the mempool's do.
    """
    graph: TxGraph[int] = TxGraph(rng=random.Random(seed))
    feerates: dict[int, FeeFrac] = {}
    parents: dict[int, set[int]] = {}
    for kind, ref, fee, deps in steps:
        if kind == "add":
            feerates[ref] = FeeFrac(fee, 1 + ref % 4)
            parents[ref] = deps
            graph.add_transaction(ref, feerates[ref], ref)
            for parent in deps:
                graph.add_dependency(parent, ref)
            graph.do_work(1_000)
        else:
            for r in sorted(_with_descendants(parents, ref), reverse=True):
                graph.remove_transaction(r)
                del parents[r], feerates[r]
    assert graph.do_work(10**12)
    seen: set[int] = set()
    for ref in feerates:
        if ref in seen:
            continue
        members = graph.cluster(ref)
        seen.update(members)
        assert members == _reference(feerates, parents, sorted(members))
    order = [r for chunk in graph.mining_order() for r in chunk.refs]
    keys = graph.order_keys(feerates)
    assert order == sorted(feerates, key=keys.__getitem__)
