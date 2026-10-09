# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Cluster linearization: Bitcoin Core's `cluster_linearize.h` in Python.

A port of `src/cluster_linearize.h`, of `CompareChunks` in
`src/util/feefrac.cpp` and of `InsecureRandomContext` in `src/random.h`,
at bitcoin/bitcoin@9be056a8a7, the v31.1 tag. `linearize` orders a
cluster's transactions for mining with the spanning-forest algorithm
(SFL), within a budget of work counted as Core counts it.

The order `linearize` returns depends on its random seed only where it
is not optimal. Where it is, Core's own fuzz target `clusterlin_linearize`
asserts that two seeds give the same order, so an optimal answer here is
Core's answer for the same graph and the same fallback order.

A set of transactions is an `int` used as a bitmask, bit `i` being the
transaction at position `i`, where Core uses a `BitSet`. Core's
comparator for the fallback order is a key function here.
"""

from __future__ import annotations

import heapq
from collections import deque
from fractions import Fraction
from typing import TYPE_CHECKING, NamedTuple, Protocol, Self

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

__all__ = [
    "DepGraph",
    "FeeFrac",
    "InsecureRandomContext",
    "OrderKey",
    "SetInfo",
    "SpanningForestState",
    "chunk_linearization",
    "chunk_linearization_info",
    "compare_chunks",
    "feerate_compare",
    "linearize",
    "post_linearize",
]

_MASK64 = (1 << 64) - 1


class OrderKey(Protocol):
    """A key of the fallback order: anything ordered by `<`."""

    def __lt__(self, other: Self, /) -> bool:
        """Whether `self` goes first."""
        ...


class FeeFrac(NamedTuple):
    """A fee and a size, Core's `FeeFrac`."""

    fee: int
    size: int


class SetInfo(NamedTuple):
    """A set of transactions and their combined fee and size."""

    transactions: int
    feerate: FeeFrac


def feerate_compare(a: FeeFrac, b: FeeFrac) -> int:
    """Return -1, 0 or 1 as `a`'s feerate is below, equal to or above `b`'s.

    Core's `FeeRateCompare`, by cross-multiplication, so it is exact.
    """
    cross_a, cross_b = a.fee * b.size, b.fee * a.size
    return (cross_a > cross_b) - (cross_a < cross_b)


def _bits(mask: int) -> Iterator[int]:
    """Yield the positions set in `mask`, lowest first."""
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low


def _first(mask: int) -> int:
    """Return the lowest position set in `mask`, which is not empty."""
    return (mask & -mask).bit_length() - 1


class InsecureRandomContext:
    """Core's `InsecureRandomContext`: xoroshiro128++ seeded by SplitMix64.

    With the bit buffer of Core's `RandomMixin`, so that a seed draws the
    same numbers as Core's.
    """

    def __init__(self, seed: int) -> None:
        """Seed the two words of state from `seed`, a 64-bit integer."""
        self._seed = seed & _MASK64
        self._s0 = self._split_mix64()
        self._s1 = self._split_mix64()
        self._bitbuf = 0
        self._bitbuf_size = 0

    def _split_mix64(self) -> int:
        self._seed = (self._seed + 0x9E3779B97F4A7C15) & _MASK64
        z = self._seed
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
        return z ^ (z >> 31)

    def rand64(self) -> int:
        """Return the next 64-bit output."""
        s0, s1 = self._s0, self._s1
        total = (s0 + s1) & _MASK64
        result = ((((total << 17) | (total >> 47)) & _MASK64) + s0) & _MASK64
        s1 ^= s0
        self._s0 = (((s0 << 49) | (s0 >> 15)) & _MASK64) ^ s1 ^ ((s1 << 21) & _MASK64)
        self._s1 = ((s1 << 28) | (s1 >> 36)) & _MASK64
        return result

    def randbits(self, bits: int) -> int:
        """Return a `bits`-bit integer, drawing from the bit buffer first."""
        if bits == 64:  # noqa: PLR2004
            return self.rand64()
        if bits <= self._bitbuf_size:
            ret = self._bitbuf
            self._bitbuf >>= bits
            self._bitbuf_size -= bits
        else:
            gen = self.rand64()
            ret = (gen << self._bitbuf_size) | self._bitbuf
            self._bitbuf = gen >> (bits - self._bitbuf_size)
            self._bitbuf_size += 64 - bits
        return ret & ((1 << bits) - 1)

    def randrange(self, upper: int) -> int:
        """Return an integer in `[0, upper)`, by rejection, as Core does."""
        maxval = upper - 1
        bits = maxval.bit_length()
        while True:
            ret = self.randbits(bits)
            if ret <= maxval:
                return ret

    def randbool(self) -> bool:
        """Return one random bit."""
        return bool(self.randbits(1))


class DepGraph:
    """A cluster's fees, sizes, ancestors and descendants: Core's `DepGraph`.

    Positions are reused: `add_transaction` takes the lowest free one, and
    `remove_transactions` leaves holes.
    """

    def __init__(self) -> None:
        """Start empty."""
        self.fees: list[int] = []
        self.sizes: list[int] = []
        self.ancestors: list[int] = []
        self.descendants: list[int] = []
        self.positions = 0

    @classmethod
    def remapped(
        cls, depgraph: DepGraph, mapping: Sequence[int], pos_range: int
    ) -> DepGraph:
        """Return `depgraph` with position `i` moved to `mapping[i]`.

        `pos_range` is the highest new position plus one.
        """
        new = cls()
        new.fees = [0] * pos_range
        new.sizes = [0] * pos_range
        new.ancestors = [0] * pos_range
        new.descendants = [0] * pos_range
        for i in _bits(depgraph.positions):
            j = mapping[i]
            new.ancestors[j] = new.descendants[j] = 1 << j
            new.positions |= 1 << j
            new.fees[j], new.sizes[j] = depgraph.fees[i], depgraph.sizes[i]
        for i in _bits(depgraph.positions):
            parents = 0
            for j in _bits(depgraph.reduced_parents(i)):
                parents |= 1 << mapping[j]
            new.add_dependencies(parents, mapping[i])
        return new

    @property
    def position_range(self) -> int:
        """Return one past the highest position in use, or 0."""
        return len(self.fees)

    @property
    def tx_count(self) -> int:
        """Return the number of transactions."""
        return self.positions.bit_count()

    def feerate(self, i: int) -> FeeFrac:
        """Return the fee and size of the transaction at `i`."""
        return FeeFrac(self.fees[i], self.sizes[i])

    def set_fee(self, i: int, fee: int) -> None:
        """Change the fee of the transaction at `i`."""
        self.fees[i] = fee

    def add_transaction(self, feerate: FeeFrac) -> int:
        """Add an unconnected transaction at the lowest free position."""
        i = (~self.positions & (self.positions + 1)).bit_length() - 1
        if i == len(self.fees):
            self.fees.append(feerate.fee)
            self.sizes.append(feerate.size)
            self.ancestors.append(1 << i)
            self.descendants.append(1 << i)
        else:
            self.fees[i], self.sizes[i] = feerate
            self.ancestors[i] = self.descendants[i] = 1 << i
        self.positions |= 1 << i
        return i

    def remove_transactions(self, delete: int) -> None:
        """Remove the set `delete`, and every dependency on it.

        A grandparent stays an ancestor where the parent between is
        removed, as in Core.
        """
        self.positions &= ~delete
        while self.fees and not self.positions >> (len(self.fees) - 1) & 1:
            self.fees.pop()
            self.sizes.pop()
            self.ancestors.pop()
            self.descendants.pop()
        for i in range(len(self.fees)):
            self.ancestors[i] &= self.positions
            self.descendants[i] &= self.positions

    def add_dependencies(self, parents: int, child: int) -> None:
        """Make each of the set `parents` a parent of `child`."""
        child_ancestors = self.ancestors[child]
        parent_ancestors = 0
        for parent in _bits(parents & ~child_ancestors):
            parent_ancestors |= self.ancestors[parent]
        parent_ancestors &= ~child_ancestors
        if not parent_ancestors:
            return
        child_des = self.descendants[child]
        for i in _bits(parent_ancestors):
            self.descendants[i] |= child_des
        for i in _bits(child_des):
            self.ancestors[i] |= parent_ancestors

    def reduced_parents(self, i: int) -> int:
        """Return the parents of `i` that are not ancestors of another."""
        parents = self.ancestors[i] & ~(1 << i)
        for parent in _bits(parents):
            if parents >> parent & 1:
                parents &= ~self.ancestors[parent]
                parents |= 1 << parent
        return parents

    def connected_component(self, todo: int, tx: int) -> int:
        """Return the transactions of `todo` connected to `tx` within it.

        Two are connected through an ancestry in the whole graph, as in
        Core's `GetConnectedComponent`.
        """
        to_add, ret = 1 << tx, 0
        while to_add:
            old = ret
            for i in _bits(to_add):
                ret |= self.descendants[i] | self.ancestors[i]
            ret &= todo
            to_add = ret & ~old
        return ret

    def find_connected_component(self, todo: int) -> int:
        """Return the component of `todo` holding its lowest position."""
        return self.connected_component(todo, _first(todo)) if todo else 0

    def is_connected(self, subset: int | None = None) -> bool:
        """Whether `subset`, the whole graph by default, is connected."""
        subset = self.positions if subset is None else subset
        return self.find_connected_component(subset) == subset

    def append_topo(self, linearization: list[int], select: int) -> None:
        """Append the set `select`, parents first, by ancestor count."""
        linearization.extend(
            sorted(_bits(select), key=lambda i: (self.ancestors[i].bit_count(), i))
        )

    def is_acyclic(self) -> bool:
        """Whether no transaction is its own strict ancestor."""
        return all(
            self.ancestors[i] & self.descendants[i] == 1 << i
            for i in _bits(self.positions)
        )


def chunk_linearization_info(
    depgraph: DepGraph, linearization: Sequence[int]
) -> list[SetInfo]:
    """Return the chunks of `linearization`, Core's `ChunkLinearizationInfo`.

    Each transaction starts a chunk, which absorbs the chunk before it
    while it has the strictly higher feerate.
    """
    chunks: list[tuple[int, int, int]] = []
    fees, sizes = depgraph.fees, depgraph.sizes
    for i in linearization:
        mask, fee, size = 1 << i, fees[i], sizes[i]
        while chunks and fee * chunks[-1][2] > chunks[-1][1] * size:
            prev_mask, prev_fee, prev_size = chunks.pop()
            mask, fee, size = mask | prev_mask, fee + prev_fee, size + prev_size
        chunks.append((mask, fee, size))
    return [SetInfo(mask, FeeFrac(fee, size)) for mask, fee, size in chunks]


def chunk_linearization(
    depgraph: DepGraph, linearization: Sequence[int]
) -> list[FeeFrac]:
    """Return the feerates of the chunks of `linearization`."""
    return [
        chunk.feerate for chunk in chunk_linearization_info(depgraph, linearization)
    ]


def compare_chunks(  # noqa: C901 -- Core's `CompareChunks`, kept in its shape
    chunks0: Sequence[FeeFrac], chunks1: Sequence[FeeFrac]
) -> int | None:
    """Compare two feerate diagrams, Core's `CompareChunks`.

    Each diagram starts at (0, 0), adds its chunks in order, and goes on
    flat. Return 1 where the first is better somewhere and worse nowhere,
    -1 for the reverse, 0 where they are equal, and `None` where each is
    better somewhere.
    """
    chunk = (chunks0, chunks1)
    next_index = [0, 0]
    accum = [[0, 0], [0, 0]]
    better = [False, False]

    def next_point(dia: int) -> tuple[int, int]:
        fee, size = chunk[dia][next_index[dia]]
        return accum[dia][0] + fee, accum[dia][1] + size

    def advance(dia: int) -> None:
        fee, size = chunk[dia][next_index[dia]]
        accum[dia][0] += fee
        accum[dia][1] += size
        next_index[dia] += 1

    while True:
        done0 = next_index[0] == len(chunks0)
        done1 = next_index[1] == len(chunks1)
        if done0 and done1:
            break
        if done0 or done1:
            side = int(done0)
        else:
            side = int(next_point(0)[1] > next_point(1)[1])
        p_fee, p_size = next_point(side)
        a_fee, a_size = accum[1 - side]
        slope_ap = FeeFrac(p_fee - a_fee, p_size - a_size)
        if done0 or done1:
            cmp = feerate_compare(slope_ap, FeeFrac(0, 1))
        else:
            b_fee, b_size = next_point(1 - side)
            cmp = feerate_compare(slope_ap, FeeFrac(b_fee - a_fee, b_size - a_size))
            if b_size == p_size:
                advance(1 - side)
        if cmp > 0:
            better[side] = True
        if cmp < 0:
            better[1 - side] = True
        advance(side)
        if better[0] and better[1]:
            return None
    return int(better[0]) - int(better[1])


def _identity(i: int) -> int:
    return i


class SpanningForestState:
    """The state of Core's spanning-forest linearization (SFL).

    Every dependency is active or inactive, and the active ones form a
    spanning forest whose trees are the chunks. Merging two chunks
    activates a dependency between them, splitting one deactivates one.
    Core's comment above `SpanningForestState` is the description of the
    algorithm, and the order of its random draws is Core's, so a seed
    takes the same steps here as there. `cost` counts work as Core's
    `SFLDefaultCostModel` does.
    """

    _INVALID = -1

    def __init__(self, depgraph: DepGraph, rng_seed: int) -> None:
        """Put every transaction in a chunk of its own."""
        self._rng = InsecureRandomContext(rng_seed)
        self._depgraph = depgraph
        self.cost = 0
        txs = depgraph.positions
        size = depgraph.position_range
        count = txs.bit_count()
        self._transactions = txs
        self._parents = [0] * size
        self._children = [0] * size
        self._active_children = [0] * size
        self._chunk_idx = [0] * size
        # the top set of each active dependency, by parent and then child
        self._dep_top: list[dict[int, int]] = [{} for _ in range(size)]
        self._set_tx = [0] * count
        self._set_fee = [0] * count
        self._set_size = [0] * count
        self._reach_up = [0] * count
        self._reach_down = [0] * count
        self._suboptimal: deque[int] = deque()
        self._suboptimal_idxs = 0
        self._nonminimal: deque[tuple[int, int, int]] = deque()
        num_chunks = num_deps = 0
        for tx in _bits(txs):
            parents = depgraph.reduced_parents(tx)
            self._parents[tx] = parents
            for parent in _bits(parents):
                self._children[parent] |= 1 << tx
            num_deps += parents.bit_count()
            self._chunk_idx[tx] = num_chunks
            self._set_tx[num_chunks] = 1 << tx
            self._set_fee[num_chunks] = depgraph.fees[tx]
            self._set_size[num_chunks] = depgraph.sizes[tx]
            num_chunks += 1
        for chunk in range(count):
            tx = _first(self._set_tx[chunk])
            self._reach_up[chunk] = self._parents[tx]
            self._reach_down[chunk] = self._children[tx]
        self._chunk_idxs = (1 << num_chunks) - 1
        self.cost += 39 * num_chunks + 48 * num_chunks + 4 * num_deps

    def _pick_random_tx(self, txs: int) -> int:
        pos = self._rng.randrange(txs.bit_count())
        for tx in _bits(txs):
            if pos == 0:
                return tx
            pos -= 1
        raise AssertionError  # pragma: no cover -- `pos` is below the count

    def _compare(self, a: int, b: int) -> int:
        """Compare the feerates of the sets `a` and `b`."""
        cross_a = self._set_fee[a] * self._set_size[b]
        cross_b = self._set_fee[b] * self._set_size[a]
        return (cross_a > cross_b) - (cross_a < cross_b)

    def _activate(self, parent: int, child: int) -> int:
        """Activate the dependency of `child` on `parent`; return the chunk."""
        set_tx, set_fee, set_size = self._set_tx, self._set_fee, self._set_size
        top = self._chunk_idx[parent]
        bottom = self._chunk_idx[child]
        top_tx, top_fee, top_size = set_tx[top], set_fee[top], set_size[top]
        bot_tx, bot_fee, bot_size = set_tx[bottom], set_fee[bottom], set_size[bottom]
        # every top set holding the parent gains the bottom, and every top
        # set holding the child gains the top
        for tx in _bits(top_tx):
            self._chunk_idx[tx] = bottom
            dep_top = self._dep_top[tx]
            for dep_child in _bits(self._active_children[tx]):
                d = dep_top[dep_child]
                if set_tx[d] >> parent & 1:
                    set_tx[d] |= bot_tx
                    set_fee[d] += bot_fee
                    set_size[d] += bot_size
        for tx in _bits(bot_tx):
            dep_top = self._dep_top[tx]
            for dep_child in _bits(self._active_children[tx]):
                d = dep_top[dep_child]
                if set_tx[d] >> child & 1:
                    set_tx[d] |= top_tx
                    set_fee[d] += top_fee
                    set_size[d] += top_size
        merged = bot_tx | top_tx
        set_tx[bottom] = merged
        set_fee[bottom] = bot_fee + top_fee
        set_size[bottom] = bot_size + top_size
        self._reach_up[bottom] = (
            self._reach_up[bottom] | self._reach_up[top]
        ) & ~merged
        self._reach_down[bottom] = (
            self._reach_down[bottom] | self._reach_down[top]
        ) & ~merged
        self._dep_top[parent][child] = top
        self._active_children[parent] |= 1 << child
        self._chunk_idxs &= ~(1 << top)
        self.cost += 10 * (merged.bit_count() - 1) + 1
        return bottom

    def _deactivate(self, parent: int, child: int) -> tuple[int, int]:
        """Deactivate an active dependency; return the top and bottom chunks."""
        set_tx, set_fee, set_size = self._set_tx, self._set_fee, self._set_size
        top = self._dep_top[parent].pop(child)
        bottom = self._chunk_idx[parent]
        self._active_children[parent] &= ~(1 << child)
        self._chunk_idxs |= 1 << top
        ntx = set_tx[bottom].bit_count()
        top_tx, top_fee, top_size = set_tx[top], set_fee[top], set_size[top]
        bot_tx = set_tx[bottom] & ~top_tx
        bot_fee = set_fee[bottom] - top_fee
        bot_size = set_size[bottom] - top_size
        set_tx[bottom], set_fee[bottom], set_size[bottom] = bot_tx, bot_fee, bot_size
        top_parents = top_children = 0
        for tx in _bits(top_tx):
            self._chunk_idx[tx] = top
            top_parents |= self._parents[tx]
            top_children |= self._children[tx]
            dep_top = self._dep_top[tx]
            for dep_child in _bits(self._active_children[tx]):
                d = dep_top[dep_child]
                if set_tx[d] >> parent & 1:
                    set_tx[d] &= ~bot_tx
                    set_fee[d] -= bot_fee
                    set_size[d] -= bot_size
        bot_parents = bot_children = 0
        for tx in _bits(bot_tx):
            bot_parents |= self._parents[tx]
            bot_children |= self._children[tx]
            dep_top = self._dep_top[tx]
            for dep_child in _bits(self._active_children[tx]):
                d = dep_top[dep_child]
                if set_tx[d] >> child & 1:
                    set_tx[d] &= ~top_tx
                    set_fee[d] -= top_fee
                    set_size[d] -= top_size
        self._reach_up[top] = top_parents & ~top_tx
        self._reach_down[top] = top_children & ~top_tx
        self._reach_up[bottom] = bot_parents & ~bot_tx
        self._reach_down[bottom] = bot_children & ~bot_tx
        self.cost += 11 * (ntx - 1) + 8
        return top, bottom

    def _merge_chunks(self, top: int, bottom: int) -> int:
        """Activate a random dependency of `bottom` on `top`."""
        top_tx, bot_tx = self._set_tx[top], self._set_tx[bottom]
        num_deps = sum(
            (self._children[tx] & bot_tx).bit_count() for tx in _bits(top_tx)
        )
        self.cost += 2 * top_tx.bit_count()
        pick = self._rng.randrange(num_deps)
        for steps, tx in enumerate(_bits(top_tx), 1):
            intersect = self._children[tx] & bot_tx
            count = intersect.bit_count()
            if pick < count:
                for child in _bits(intersect):  # pragma: no branch -- `pick` < `count`
                    if pick == 0:
                        self.cost += 3 * steps + 5
                        return self._activate(tx, child)
                    pick -= 1
            pick -= count
        raise AssertionError  # pragma: no cover -- `pick` is below `num_deps`

    def _pick_merge_candidate(self, chunk: int, *, downward: bool) -> int:
        """Return the chunk to merge `chunk` with, or `_INVALID`.

        Upward, the lowest-feerate chunk it depends on among those at or
        below its own feerate; downward, the highest-feerate one depending
        on it among those at or above. Ties go to a random one.
        """
        set_tx, set_fee, set_size = self._set_tx, self._set_fee, self._set_size
        best_fee, best_size = set_fee[chunk], set_size[chunk]
        best = self._INVALID
        best_tiebreak = 0
        todo = self._reach_down[chunk] if downward else self._reach_up[chunk]
        steps = 0
        while todo:
            steps += 1
            reached = self._chunk_idx[_first(todo)]
            todo &= ~set_tx[reached]
            fee, size = set_fee[reached], set_size[reached]
            if downward:
                cross_a, cross_b = best_fee * size, fee * best_size
            else:
                cross_a, cross_b = fee * best_size, best_fee * size
            if cross_a > cross_b:
                continue
            tiebreak = self._rng.rand64()
            if cross_a < cross_b or tiebreak >= best_tiebreak:
                best_fee, best_size, best = fee, size, reached
                best_tiebreak = tiebreak
        self.cost += 8 * steps
        return best

    def _merge_step(self, chunk: int, *, downward: bool) -> int:
        """Merge `chunk` once; return the result, or `_INVALID`."""
        other = self._pick_merge_candidate(chunk, downward=downward)
        if other == self._INVALID:
            return self._INVALID
        if downward:
            return self._merge_chunks(chunk, other)
        return self._merge_chunks(other, chunk)

    def _queue_suboptimal(self, chunk: int) -> None:
        if not self._suboptimal_idxs >> chunk & 1:
            self._suboptimal_idxs |= 1 << chunk
            self._suboptimal.append(chunk)

    def _merge_sequence(self, chunk: int, *, downward: bool) -> None:
        """Merge `chunk` while it can be, then queue it for improvement."""
        while True:
            merged = self._merge_step(chunk, downward=downward)
            if merged == self._INVALID:
                break
            chunk = merged
        self._queue_suboptimal(chunk)

    def _improve(self, parent: int, child: int) -> None:
        """Split at a dependency, then merge until topological again."""
        top, bottom = self._deactivate(parent, child)
        if self._reach_up[top] & self._set_tx[bottom]:
            # the top depends on the bottom: they merge back, the other way
            self._queue_suboptimal(self._merge_chunks(bottom, top))
        else:
            self._merge_sequence(top, downward=False)
            self._merge_sequence(bottom, downward=True)

    def _pick_chunk_to_optimize(self) -> int:
        steps = 0
        while self._suboptimal:
            steps += 1
            chunk = self._suboptimal.popleft()
            self._suboptimal_idxs &= ~(1 << chunk)
            if self._chunk_idxs >> chunk & 1:
                self.cost += steps + 4
                return chunk
        self.cost += steps + 4
        return self._INVALID

    def _pick_dependency_to_split(self, chunk: int) -> tuple[int, int] | None:
        """Return a random active dependency whose top beats `chunk`."""
        candidate = None
        candidate_tiebreak = 0
        chunk_tx = self._set_tx[chunk]
        for tx in _bits(chunk_tx):
            dep_top = self._dep_top[tx]
            for child in _bits(self._active_children[tx]):
                if self._compare(dep_top[child], chunk) <= 0:
                    continue
                tiebreak = self._rng.rand64()
                if tiebreak < candidate_tiebreak:
                    continue
                candidate = tx, child
                candidate_tiebreak = tiebreak
        self.cost += 8 * chunk_tx.bit_count() + 9
        return candidate

    def _shuffled_into[T](self, queue: deque[T], item: T) -> None:
        """Append `item` and swap it with a random entry, as Core does."""
        queue.append(item)
        j = self._rng.randrange(len(queue))
        if j != len(queue) - 1:
            queue[-1], queue[j] = queue[j], queue[-1]

    def load_linearization(self, old_linearization: Sequence[int]) -> None:
        """Merge upward along `old_linearization`, right after construction."""
        for tx in old_linearization:
            chunk = self._chunk_idx[tx]
            while chunk != self._INVALID:
                chunk = self._merge_step(chunk, downward=False)

    def make_topological(self) -> None:
        """Merge chunks until no chunk depends on one it beats or ties."""
        init_dir = int(self._rng.randbool())
        merged_chunks = 0
        self._suboptimal_idxs = self._chunk_idxs
        for chunk in _bits(self._chunk_idxs):
            self._shuffled_into(self._suboptimal, chunk)
        chunks = self._chunk_idxs.bit_count()
        steps = 0
        while self._suboptimal:
            steps += 1
            chunk = self._suboptimal.popleft()
            self._suboptimal_idxs &= ~(1 << chunk)
            if not self._chunk_idxs >> chunk & 1:
                continue
            direction = 3 if merged_chunks >> chunk & 1 else init_dir + 1
            flip = int(self._rng.randbool())
            for i in range(2):
                downward = not i ^ flip
                if not direction & (2 if downward else 1):
                    continue
                merged = self._merge_step(chunk, downward=downward)
                if merged != self._INVALID:
                    self._queue_suboptimal(merged)
                    merged_chunks |= 1 << merged
                    break
        self.cost += 20 * chunks + 28 * steps

    def start_optimizing(self) -> None:
        """Queue every chunk for improvement, in a random order."""
        self._suboptimal_idxs = self._chunk_idxs
        for chunk in _bits(self._chunk_idxs):
            self._shuffled_into(self._suboptimal, chunk)
        self.cost += 13 * len(self._suboptimal)

    def optimize_step(self) -> bool:
        """Improve one chunk; return whether more may be improved."""
        chunk = self._pick_chunk_to_optimize()
        if chunk == self._INVALID:
            return False
        dependency = self._pick_dependency_to_split(chunk)
        if dependency is None:
            return bool(self._suboptimal)
        self._improve(*dependency)
        return True

    def start_minimizing(self) -> None:
        """Queue every chunk, with a random pivot, to be split if it can."""
        self._nonminimal.clear()
        for chunk in _bits(self._chunk_idxs):
            pivot = self._pick_random_tx(self._set_tx[chunk])
            entry = (chunk, pivot, self._rng.randbits(1))
            self._shuffled_into(self._nonminimal, entry)
        self.cost += 18 * len(self._nonminimal)

    def minimize_step(self) -> bool:  # noqa: C901, PLR0912 -- Core's, in its shape
        """Try to split one chunk; return whether any chunk may still split.

        A chunk splits into equal-feerate parts where an active dependency
        has a top of the chunk's own feerate. The pivot is moved to the
        top first, or to the bottom; failing both, the chunk is minimal.
        """
        if not self._nonminimal:
            return False
        chunk, pivot, flags = self._nonminimal.popleft()
        chunk_tx = self._set_tx[chunk]
        move_down = bool(flags & 1)
        second_stage = bool(flags & 2)
        candidate = (0, 0)
        candidate_tiebreak = 0
        have_any = False
        for tx in _bits(chunk_tx):
            dep_top = self._dep_top[tx]
            for child in _bits(self._active_children[tx]):
                top = dep_top[child]
                if self._compare(top, chunk) < 0:
                    continue
                have_any = True
                if move_down == bool(self._set_tx[top] >> pivot & 1):
                    continue
                tiebreak = self._rng.rand64() | 1
                if tiebreak > candidate_tiebreak:
                    candidate_tiebreak = tiebreak
                    candidate = tx, child
        self.cost += 11 * chunk_tx.bit_count() + 11
        if not have_any:
            return True
        if candidate_tiebreak == 0:
            flags ^= 3
            if not second_stage:  # pragma: no branch -- a second stage finds them
                self._nonminimal.append((chunk, pivot, flags))
            return True
        top, bottom = self._deactivate(*candidate)
        if self._reach_up[top] & self._set_tx[bottom]:
            merged = self._merge_chunks(bottom, top)
            self._nonminimal.append((merged, pivot, flags))
            self.cost += 7
            return True
        if move_down:
            top_pivot = self._pick_random_tx(self._set_tx[top])
            self._nonminimal.append((top, top_pivot, self._rng.randbits(1)))
            self._nonminimal.append((bottom, pivot, flags))
        else:
            bottom_pivot = self._pick_random_tx(self._set_tx[bottom])
            self._nonminimal.append((top, pivot, flags))
            self._nonminimal.append((bottom, bottom_pivot, self._rng.randbits(1)))
        if self._rng.randbool():
            self._nonminimal[-1], self._nonminimal[-2] = (
                self._nonminimal[-2],
                self._nonminimal[-1],
            )
        self.cost += 24
        return True

    def get_linearization(self, fallback_key: Callable[[int], OrderKey]) -> list[int]:
        """Return the chunks in order, each in order, parents first.

        Chunks go by feerate, highest first, then by size, smallest first,
        then by the lowest greatest member under `fallback_key`. Within a
        chunk, transactions go by the same three keys, the last being the
        transaction's own. The state must be topological.
        """
        depgraph = self._depgraph
        keys = {tx: fallback_key(tx) for tx in _bits(self._transactions)}
        chunk_deps = [0] * len(self._set_tx)
        tx_deps = [0] * len(self._parents)
        for tx in _bits(self._transactions):
            tx_deps[tx] = self._parents[tx].bit_count()
            chunk = self._chunk_idx[tx]
            chunk_deps[chunk] += (self._parents[tx] & ~self._set_tx[chunk]).bit_count()

        def chunk_entry(chunk: int) -> tuple[Fraction, int, OrderKey, int]:
            highest = max(keys[tx] for tx in _bits(self._set_tx[chunk]))
            size = self._set_size[chunk]
            return Fraction(-self._set_fee[chunk], size), size, highest, chunk

        def tx_entry(tx: int) -> tuple[Fraction, int, OrderKey, int]:
            size = depgraph.sizes[tx]
            return Fraction(-depgraph.fees[tx], size), size, keys[tx], tx

        ready_chunks = [
            chunk_entry(chunk)
            for chunk in _bits(self._chunk_idxs)
            if chunk_deps[chunk] == 0
        ]
        heapq.heapify(ready_chunks)
        linearization: list[int] = []
        while ready_chunks:
            chunk = heapq.heappop(ready_chunks)[-1]
            chunk_tx = self._set_tx[chunk]
            ready_tx = [tx_entry(tx) for tx in _bits(chunk_tx) if tx_deps[tx] == 0]
            heapq.heapify(ready_tx)
            while ready_tx:
                tx = heapq.heappop(ready_tx)[-1]
                linearization.append(tx)
                for child in _bits(self._children[tx]):
                    tx_deps[child] -= 1
                    if tx_deps[child] == 0 and chunk_tx >> child & 1:
                        heapq.heappush(ready_tx, tx_entry(child))
                    child_chunk = self._chunk_idx[child]
                    if child_chunk != chunk:
                        chunk_deps[child_chunk] -= 1
                        if chunk_deps[child_chunk] == 0:
                            heapq.heappush(ready_chunks, chunk_entry(child_chunk))
        return linearization

    def get_diagram(self) -> list[FeeFrac]:
        """Return the chunk feerates, best first, by Core's `FeeFrac` order."""
        chunks = [
            FeeFrac(self._set_fee[chunk], self._set_size[chunk])
            for chunk in _bits(self._chunk_idxs)
        ]
        # by feerate, then the smaller size first, as `FeeFrac`'s `<=>`
        chunks.sort(key=lambda c: (Fraction(c.fee, c.size), -c.size), reverse=True)
        return chunks


def linearize(  # noqa: PLR0913
    depgraph: DepGraph,
    max_cost: int,
    rng_seed: int,
    fallback_key: Callable[[int], OrderKey] = _identity,
    old_linearization: Sequence[int] = (),
    *,
    is_topological: bool = True,
) -> tuple[list[int], bool, int]:
    """Find or improve a linearization of a cluster, Core's `Linearize`.

    `max_cost` bounds the work, counted in Core's units. `rng_seed`
    drives the random choices. `fallback_key` orders equal-feerate
    transactions and chunks, and position order is its default, Core's
    `IndexTxOrder`. `old_linearization` is an earlier linearization or
    empty, and `is_topological` says whether it keeps parents before
    children.

    Return the linearization, whether it is optimal with minimal chunks,
    and the work it took. It is never worse than `old_linearization`.
    """
    forest = SpanningForestState(depgraph, rng_seed)
    if old_linearization:
        forest.load_linearization(old_linearization)
        if not is_topological:
            forest.make_topological()
    else:
        forest.make_topological()
    if forest.cost < max_cost:
        forest.start_optimizing()
        while forest.optimize_step() and forest.cost < max_cost:
            pass
    optimal = False
    if forest.cost < max_cost:
        forest.start_minimizing()
        while True:
            if not forest.minimize_step():
                optimal = True
                break
            if forest.cost >= max_cost:
                break
    return forest.get_linearization(fallback_key), optimal, forest.cost


def post_linearize(  # noqa: PLR0915 -- Core's `PostLinearize`, in its shape
    depgraph: DepGraph, linearization: list[int]
) -> None:
    """Improve `linearization` in place, Core's `PostLinearize`.

    Two passes, back to front and then front to back. Each moves a group
    of transactions ahead of a lower-feerate group it does not depend on,
    and merges it with one it does. The chunks come out connected, and a
    tree-shaped cluster comes out optimal.
    """
    sentinel = 0
    no_prev_tx = 0
    size = depgraph.position_range + 1
    prev_tx = [0] * size
    first_tx = [0] * size
    prev_group = [0] * size
    group = [0] * size
    deps = [0] * size
    fees = [0] * size
    sizes = [0] * size
    count = len(linearization)
    for pass_number in range(2):
        rev = not pass_number & 1
        prev_group[sentinel] = sentinel
        for i in range(count):
            idx = linearization[count - 1 - i if rev else i]
            cur = idx + 1
            group[cur] = 1 << idx
            deps[cur] = depgraph.descendants[idx] if rev else depgraph.ancestors[idx]
            fees[cur] = -depgraph.fees[idx] if rev else depgraph.fees[idx]
            sizes[cur] = depgraph.sizes[idx]
            prev_tx[cur] = no_prev_tx
            first_tx[cur] = cur
            prev_group[cur] = prev_group[sentinel]
            prev_group[sentinel] = cur
            next_group = sentinel
            prev = prev_group[cur]
            # the sentinel's empty feerate is never lower than another
            while (
                prev != sentinel and fees[cur] * sizes[prev] > fees[prev] * sizes[cur]
            ):
                if deps[cur] & group[prev]:
                    group[cur] |= group[prev]
                    deps[cur] |= deps[prev]
                    fees[cur] += fees[prev]
                    sizes[cur] += sizes[prev]
                    prev_tx[first_tx[cur]] = prev
                    first_tx[cur] = first_tx[prev]
                    prev = prev_group[prev]
                    prev_group[cur] = prev
                else:
                    preprev = prev_group[prev]
                    prev_group[next_group] = prev
                    prev_group[prev] = cur
                    prev_group[cur] = preprev
                    next_group = prev
                    prev = preprev
        cur = prev_group[sentinel]
        done = 0
        while cur != sentinel:
            tx = cur
            while True:
                if rev:
                    linearization[done] = tx - 1
                else:
                    linearization[count - 1 - done] = tx - 1
                done += 1
                tx = prev_tx[tx]
                if tx == no_prev_tx:
                    break
            cur = prev_group[cur]
