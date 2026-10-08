# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for the mining keys `btclib_node.txgraph.TxGraph` keeps."""

from __future__ import annotations

import random

import pytest

from btclib_node.cluster_linearize import FeeFrac
from btclib_node.txgraph import MiningKey, TxGraph


def fresh_keys(graph: TxGraph[int]) -> dict[int, MiningKey]:
    """Return every key computed from scratch, bypassing the kept ones."""
    fresh: dict[int, MiningKey] = {}
    for ref in graph._locator:
        cluster = graph._locator[ref][0]
        graph._make_acceptable(cluster)
        fresh.update(graph._order_keys(cluster))
    return fresh


def check(graph: TxGraph[int]) -> None:
    """Assert that the keys the graph answers are the fresh ones."""
    kept = graph.order_keys(list(graph._locator))
    assert kept == fresh_keys(graph)


@pytest.mark.parametrize("seed", range(8))
def test_kept_keys_equal_fresh_ones_after_every_change(seed: int) -> None:
    """Random changes of every kind, each followed by the comparison.

    A cluster is acceptable without work, so it is not optimal until
    `do_work` is given enough, and that changes a linearization the
    keys were kept for.
    """
    rng = random.Random(seed)
    graph: TxGraph[int] = TxGraph(acceptable_cost=0, rng=random.Random(seed))
    refs: list[int] = []
    created = 0
    for _ in range(300):
        op = rng.choice("aaadddddfwwwwr")
        if op == "a" or len(refs) < 2:
            graph.add_transaction(
                created, FeeFrac(rng.randint(1, 50), rng.randint(1, 10)), created
            )
            refs.append(created)
            created += 1
        elif op == "d":
            # a parent is older than its child, so there is no cycle
            parent, child = sorted(rng.sample(refs, 2))
            graph.add_dependency(parent, child)
        elif op == "r":
            ref = rng.choice(refs)
            graph.remove_transaction(ref)
            refs.remove(ref)
        elif op == "f":
            graph.set_fee(rng.choice(refs), rng.randint(1, 50))
        else:
            graph.do_work(rng.choice((10, 1_000, 100_000)))
        check(graph)
