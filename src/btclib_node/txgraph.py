# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The mempool's clusters and their linearizations: Core's `TxGraph`.

A port of the main graph of `src/txgraph.{h,cpp}`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag. A cluster is a connected
component of the spend graph, kept with a linearization and a quality,
and `do_work` improves the linearizations within a budget of work, as
`DoWork` does.

Not ported:

- the staging graph: the mempool stages a package's parent outside the
  graph (`Mempool.staged`);
- `Trim`: `Mempool.check_cluster` refuses a transaction whose cluster
  would pass the limits, but a reorg can still pass them, since a
  re-added parent joins the children it already has
  (btclib-org/btclib-node#1823);
- `GetWorstMainChunk`, the chunk Core's eviction takes: eviction here
  scores each transaction alone (btclib-org/btclib-node#1740);
- queued changes: removals and dependencies are applied as they arrive,
  where Core queues them until a read. That changes the order a merged
  cluster's linearization starts from, so it changes the work spent
  and not the result once a cluster is optimal.

Core draws its seeds from `FastRandomContext`, so that a peer cannot
predict which clusters are hard for this node to linearize, and the
default here is the operating system's generator for the same reason.
"""

from __future__ import annotations

import fractions
import random
from enum import IntEnum
from typing import TYPE_CHECKING, NamedTuple

from btclib_node.cluster_linearize import (
    DepGraph,
    FeeFrac,
    OrderKey,
    chunk_linearization_info,
    feerate_compare,
    linearize,
    post_linearize,
)

if TYPE_CHECKING:
    from collections.abc import Hashable, Iterable, Iterator

__all__ = [
    "ACCEPTABLE_COST",
    "POST_CHANGE_COST",
    "BlockBuilder",
    "Chunk",
    "MiningKey",
    "Quality",
    "TxGraph",
]

# Core's `ACCEPTABLE_COST` and `POST_CHANGE_COST` (`src/txmempool.h`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the work a cluster gets
# before its linearization is acceptable, and the work done after a
# change to the mempool. The units are Core's, so a cluster ends as
# often optimal here as there; a unit takes longer in Python.
ACCEPTABLE_COST = 75_000
POST_CHANGE_COST = 5 * ACCEPTABLE_COST


class Quality(IntEnum):
    """How good a cluster's linearization is known to be, Core's order."""

    NEEDS_FIX = 0
    NEEDS_RELINEARIZE = 1
    ACCEPTABLE = 2
    OPTIMAL = 3


class MiningKey(NamedTuple):
    """A transaction's place in the order a block takes them; lower first.

    `TxGraph._order_keys` says what each field is.
    """

    rate: fractions.Fraction
    prefix: int
    highest: OrderKey
    position: int


class Chunk[R](NamedTuple):
    """A chunk of a cluster: its transactions in order, and their feerate."""

    refs: list[R]
    feerate: FeeFrac


class _Cluster[R]:
    """One connected component, with its linearization."""

    def __init__(self) -> None:
        self.depgraph = DepGraph()
        self.mapping: list[R] = []
        self.linearization: list[int] = []
        self.quality = Quality.OPTIMAL
        self.setindex = -1
        # the mining keys of an acceptable cluster, until anything they
        # read changes: its membership, dependencies, fees or linearization
        self.keys: dict[R, MiningKey] | None = None

    @property
    def is_topological(self) -> bool:
        return self.quality != Quality.NEEDS_FIX

    @property
    def is_acceptable(self) -> bool:
        return self.quality >= Quality.ACCEPTABLE


class TxGraph[R: Hashable]:
    """Transactions as fees, sizes and dependencies, grouped in clusters.

    A transaction is named by a `ref`, the mempool's wtxid, and
    `order_key` is its place in Core's `fallback_order`, which breaks
    ties between equal-feerate transactions and chunks.
    """

    def __init__(
        self,
        acceptable_cost: int = ACCEPTABLE_COST,
        rng: random.Random | None = None,
    ) -> None:
        """Start empty; `rng` is for tests, the default being the OS's."""
        self.acceptable_cost = acceptable_cost
        self._rng = random.SystemRandom() if rng is None else rng
        self._locator: dict[R, tuple[_Cluster[R], int]] = {}
        self._keys: dict[R, OrderKey] = {}
        self._queues: list[list[_Cluster[R]]] = [[] for _ in Quality]

    def __contains__(self, ref: object) -> bool:
        """Whether `ref` is in the graph."""
        return ref in self._locator

    def __len__(self) -> int:
        """Return the number of transactions."""
        return len(self._locator)

    def _set_quality(self, cluster: _Cluster[R], quality: Quality) -> None:
        """Move `cluster` to the queue of `quality`, as Core's vectors do."""
        if cluster.setindex >= 0:
            if cluster.quality == quality:
                return
            queue = self._queues[cluster.quality]
            last = queue.pop()
            if last is not cluster:
                queue[cluster.setindex] = last
                last.setindex = cluster.setindex
        queue = self._queues[quality]
        cluster.quality = quality
        cluster.setindex = len(queue)
        queue.append(cluster)

    def _delete(self, cluster: _Cluster[R]) -> None:
        queue = self._queues[cluster.quality]
        last = queue.pop()
        if last is not cluster:
            queue[cluster.setindex] = last
            last.setindex = cluster.setindex
        cluster.setindex = -1

    def _append(self, cluster: _Cluster[R], ref: R, feerate: FeeFrac) -> int:
        # a cluster's positions have no holes, so `pos` is the next one
        pos = cluster.depgraph.add_transaction(feerate)
        cluster.mapping.append(ref)
        cluster.linearization.append(pos)
        self._locator[ref] = cluster, pos
        return pos

    def add_transaction(self, ref: R, feerate: FeeFrac, order_key: OrderKey) -> None:
        """Add `ref` as a cluster of its own, which is optimal."""
        cluster: _Cluster[R] = _Cluster()
        self._append(cluster, ref, feerate)
        self._keys[ref] = order_key
        self._set_quality(cluster, Quality.OPTIMAL)

    def add_dependency(self, parent: R, child: R) -> None:
        """Make `child` spend `parent`, merging their clusters.

        The smaller cluster's linearization is appended to the larger's,
        as Core's `Merge` does, and the result needs fixing.
        """
        if parent == child:
            return
        parent_cluster, _ = self._locator[parent]
        child_cluster, _ = self._locator[child]
        if parent_cluster is not child_cluster:
            into, other = parent_cluster, child_cluster
            if other.depgraph.tx_count > into.depgraph.tx_count:
                into, other = other, into
            self._merge(into, other)
        cluster, parent_pos = self._locator[parent]
        _, child_pos = self._locator[child]
        cluster.depgraph.add_dependencies(1 << parent_pos, child_pos)
        cluster.keys = None
        self._set_quality(cluster, Quality.NEEDS_FIX)

    def _merge(self, into: _Cluster[R], other: _Cluster[R]) -> None:
        remap: dict[int, int] = {}
        for pos in other.linearization:
            ref = other.mapping[pos]
            remap[pos] = self._append(into, ref, other.depgraph.feerate(pos))
        for pos in other.linearization:
            parents = 0
            for parent in _positions(other.depgraph.reduced_parents(pos)):
                parents |= 1 << remap[parent]
            into.depgraph.add_dependencies(parents, remap[pos])
        self._delete(other)

    def remove_transaction(self, ref: R) -> None:
        """Remove `ref`, splitting what is left of its cluster.

        Each part keeps its order and needs relinearizing, unless it is a
        single transaction, as in Core's `Split`. The dependencies of a
        transaction on what `ref` spent stay, as in Core's `DepGraph`.
        """
        cluster, pos = self._locator.pop(ref)
        del self._keys[ref]
        depgraph = cluster.depgraph
        depgraph.remove_transactions(1 << pos)
        lin = [p for p in cluster.linearization if p != pos]
        self._delete(cluster)
        quality = (
            Quality.NEEDS_RELINEARIZE if cluster.is_topological else Quality.NEEDS_FIX
        )
        todo = depgraph.positions
        while todo:
            component = depgraph.find_connected_component(todo)
            todo &= ~component
            part: _Cluster[R] = _Cluster()
            for p in lin:
                if component >> p & 1:
                    self._append(part, cluster.mapping[p], depgraph.feerate(p))
            remap = {
                p: self._locator[cluster.mapping[p]][1] for p in _positions(component)
            }
            for p in _positions(component):
                parents = 0
                for parent in _positions(depgraph.reduced_parents(p)):
                    parents |= 1 << remap[parent]
                part.depgraph.add_dependencies(parents, remap[p])
            single = component & (component - 1) == 0
            self._set_quality(part, Quality.OPTIMAL if single else quality)

    def set_fee(self, ref: R, fee: int) -> None:
        """Change the fee of `ref`, Core's `SetTransactionFee`."""
        cluster, pos = self._locator[ref]
        if cluster.depgraph.fees[pos] == fee:
            return
        cluster.depgraph.set_fee(pos, fee)
        cluster.keys = None
        if cluster.depgraph.tx_count > 1 and cluster.is_acceptable:
            self._set_quality(cluster, Quality.NEEDS_RELINEARIZE)

    def _relinearize(self, cluster: _Cluster[R], max_cost: int) -> tuple[int, bool]:
        """Improve `cluster`'s linearization; return the cost and progress.

        Core's `Relinearize`: from the linearization it has, then
        `PostLinearize`.
        """
        if cluster.quality == Quality.OPTIMAL:
            return 0, False
        keys = [self._keys[ref] for ref in cluster.mapping]
        lin, optimal, cost = linearize(
            cluster.depgraph,
            max_cost,
            self._rng.getrandbits(64),
            keys.__getitem__,
            cluster.linearization,
            is_topological=cluster.is_topological,
        )
        post_linearize(cluster.depgraph, lin)
        cluster.linearization = lin
        cluster.keys = None
        improved = True
        if optimal:
            self._set_quality(cluster, Quality.OPTIMAL)
        elif max_cost >= self.acceptable_cost and not cluster.is_acceptable:
            self._set_quality(cluster, Quality.ACCEPTABLE)
        elif not cluster.is_topological:
            self._set_quality(cluster, Quality.NEEDS_RELINEARIZE)
        else:
            improved = False
        return cost, improved

    def _make_acceptable(self, cluster: _Cluster[R]) -> None:
        if not cluster.is_acceptable:
            self._relinearize(cluster, self.acceptable_cost)

    def do_work(self, max_cost: int) -> bool:
        """Improve linearizations within `max_cost`, Core's `DoWork`.

        Every cluster is made acceptable first, then optimal, each picked
        at random. Return whether nothing is left to do: with `max_cost`
        zero, whether every cluster is optimal.
        """
        cost_done = 0
        for quality in (
            Quality.NEEDS_FIX,
            Quality.NEEDS_RELINEARIZE,
            Quality.ACCEPTABLE,
        ):
            queue = self._queues[quality]
            while queue:
                if cost_done >= max_cost:
                    return False
                pos = self._rng.randrange(len(queue))
                cost_now = max_cost - cost_done
                if quality != Quality.ACCEPTABLE:
                    cost_now = min(cost_now, self.acceptable_cost)
                cost, improved = self._relinearize(queue[pos], cost_now)
                cost_done += cost
                if not improved:
                    return False
        return True

    def cluster(self, ref: R) -> list[R]:
        """Return the cluster of `ref`, in its linearization's order."""
        cluster, _ = self._locator[ref]
        self._make_acceptable(cluster)
        return [cluster.mapping[pos] for pos in cluster.linearization]

    def chunks(self, ref: R) -> list[Chunk[R]]:
        """Return the chunks of the cluster of `ref`, best first."""
        cluster, _ = self._locator[ref]
        self._make_acceptable(cluster)
        return self._chunks(cluster)

    @staticmethod
    def _chunks(cluster: _Cluster[R]) -> list[Chunk[R]]:
        chunks: list[Chunk[R]] = []
        lin = iter(cluster.linearization)
        for info in chunk_linearization_info(cluster.depgraph, cluster.linearization):
            count = info.transactions.bit_count()
            refs = [cluster.mapping[next(lin)] for _ in range(count)]
            chunks.append(Chunk(refs, info.feerate))
        return chunks

    def chunk_feerate(self, ref: R) -> FeeFrac:
        """Return the feerate of the chunk `ref` is in."""
        for chunk in self.chunks(ref):
            if ref in chunk.refs:
                return chunk.feerate
        raise AssertionError  # pragma: no cover -- `ref` is in one of them

    def _order_keys(self, cluster: _Cluster[R]) -> dict[R, MiningKey]:
        """Return each transaction's place in Core's mining order.

        Core's `CompareMainTransactions`: by chunk feerate, highest first,
        then by the size of the equal-feerate chunks up to its own in its
        cluster, then by the greatest `order_key` in its chunk, the lowest
        first, then by its place in the linearization.
        """
        keys: dict[R, MiningKey] = {}
        prefix = FeeFrac(0, 0)
        index = 0
        for chunk in self._chunks(cluster):
            if feerate_compare(chunk.feerate, prefix) < 0:
                prefix = chunk.feerate
            else:
                prefix = FeeFrac(
                    prefix.fee + chunk.feerate.fee, prefix.size + chunk.feerate.size
                )
            rate = fractions.Fraction(-chunk.feerate.fee, chunk.feerate.size)
            highest = max(self._keys[ref] for ref in chunk.refs)
            for ref in chunk.refs:
                keys[ref] = MiningKey(rate, prefix.size, highest, index)
                index += 1
        return keys

    def order_keys(self, refs: Iterable[R]) -> dict[R, MiningKey]:
        """Return a sort key for each of `refs`: the order a block takes them.

        Core's `CompareMainOrder`, each cluster being made acceptable
        first. A cluster keeps its keys until its membership,
        dependencies, fees or linearization change, as Core keeps them
        in its entries.
        """
        keys: dict[R, MiningKey] = {}
        for ref in refs:
            cluster, _ = self._locator[ref]
            if cluster.keys is None:
                self._make_acceptable(cluster)
                cluster.keys = self._order_keys(cluster)
            keys[ref] = cluster.keys[ref]
        return keys

    def _main_chunks(self) -> list[tuple[_Cluster[R], Chunk[R]]]:
        """Return every chunk with its cluster, in the order a block takes them.

        Every cluster is made acceptable first, as `MakeAllAcceptable` does.
        """
        for quality in (Quality.NEEDS_FIX, Quality.NEEDS_RELINEARIZE):
            while self._queues[quality]:
                self._make_acceptable(self._queues[quality][-1])
        keyed: list[tuple[MiningKey, _Cluster[R], Chunk[R]]] = []
        for quality in (Quality.ACCEPTABLE, Quality.OPTIMAL):
            for cluster in self._queues[quality]:
                keys = self._order_keys(cluster)
                keyed.extend(
                    (keys[chunk.refs[0]], cluster, chunk)
                    for chunk in self._chunks(cluster)
                )
        keyed.sort(key=lambda item: item[0])
        return [(cluster, chunk) for _, cluster, chunk in keyed]

    def mining_order(self) -> list[Chunk[R]]:
        """Return every chunk, in the order a block takes them.

        What Core's `BlockBuilder` returns when every chunk is included.
        """
        return [chunk for _, chunk in self._main_chunks()]

    def block_builder(self) -> BlockBuilder[R]:
        """Return the chunks in mining order, as Core's `GetBlockBuilder`.

        The graph must not change while the builder is in use.
        """
        return BlockBuilder(self._main_chunks())


class BlockBuilder[R]:
    """The chunks a block takes, best first: Core's `TxGraph::BlockBuilder`.

    Iterating yields each chunk. Moving on to the next is Core's
    `Include`; `skip` is its `Skip`, and leaves out the later chunks of
    the cluster of the chunk last yielded, since they may spend it.
    """

    def __init__(self, chunks: Iterable[tuple[object, Chunk[R]]]) -> None:
        """Hold `chunks` in mining order, each with the cluster it is of."""
        self._chunks = list(chunks)
        self._skipped: set[object] = set()
        self._current: object = None

    def __iter__(self) -> Iterator[Chunk[R]]:
        """Yield each chunk whose cluster has no skipped chunk."""
        for cluster, chunk in self._chunks:
            if cluster not in self._skipped:
                self._current = cluster
                yield chunk

    def skip(self) -> None:
        """Leave out the rest of the cluster of the chunk last yielded."""
        self._skipped.add(self._current)


def _positions(mask: int) -> Iterable[int]:
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low
