# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.cluster_linearize`.

The vectors are Bitcoin Core's own, read from the vendored
`_data/cluster_linearize_tests.cpp` (`tests/_data/README.md` pins it):
each serialized cluster with its optimal linearization, and each
cluster with its serialization. The serialization is Core's test-only
`DepGraphFormatter` (`src/test/util/cluster_linearize.h`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag), so it is ported here and not
in the module.

The properties are Core's fuzz targets in `src/test/fuzz/` at the same
tag, against an exhaustive search on clusters small enough for one.
"""

from __future__ import annotations

import ast
import itertools
import random
import re
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from btclib_node.cluster_linearize import (
    DepGraph,
    FeeFrac,
    InsecureRandomContext,
    SetInfo,
    SpanningForestState,
    chunk_linearization,
    chunk_linearization_info,
    compare_chunks,
    feerate_compare,
    linearize,
    post_linearize,
)

# a cluster as Core's test writes it: ((fee, size), parents), or a hole
type _Cluster = list[tuple[tuple[int, int], list[int]] | None]

_DATA = Path(__file__).parent / "_data"
_CORE_TESTS = _DATA / "cluster_linearize_tests.cpp"

# Core's `MaxOptimalLinearizationCost` (`src/test/util/cluster_linearize.h`,
# at bitcoin/bitcoin@9be056a8a7): the highest cost seen for an optimal
# result per cluster size, which Core's tests allow twice of
_COSTS = (
    0,
    *(0, 545, 928, 1633, 2647, 4065, 5598, 8258),
    *(9505, 11471, 14137, 19553, 20460, 26191, 28397, 32599),
    *(41631, 47419, 56329, 57767, 72196, 63652, 95366, 96537),
    *(115653, 125407, 131734, 145090, 156349, 164665, 194224, 203953),
    *(207710, 225878, 239971, 252284, 256534, 222142, 251332, 357098),
    *(325788, 295867, 410053, 497483, 533892, 576572, 577845, 572400),
    *(592536, 455082, 609249, 659130, 714091, 544507, 718788, 562378),
    *(601926, 1025081, 732725, 708896, 738224, 900445, 1092519, 1139946),
)


def _max_optimal_cost(count: int) -> int:
    return _COSTS[count] * 2


# Core's `DepGraphFormatter` reads positions into a `BitSet` of this size
_SET_SIZE = 64


class _Reader:
    """Read Core's `VARINT`s from bytes, failing past the end."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def varint(self) -> int:
        n = 0
        while True:
            if self.pos == len(self.data):
                raise EOFError
            ch = self.data[self.pos]
            self.pos += 1
            n = (n << 7) | (ch & 0x7F)
            if not ch & 0x80:
                return n
            n += 1


def _unsigned_to_signed(x: int) -> int:
    return -(x // 2) - 1 if x & 1 else x // 2


def deserialize(data: bytes) -> DepGraph:
    """Return the cluster `data` encodes, Core's `DepGraphFormatter`."""
    reader = _Reader(data)
    topo = DepGraph()
    reordering: list[int] = []
    total_size = 0
    while True:
        fee = size = diff = 0
        new_ancestors = 0
        read_error = False
        try:
            size = reader.varint() & 0x3FFFFF
            if size == 0 or topo.tx_count == _SET_SIZE:
                break
            fee = _unsigned_to_signed(reader.varint() & 0xFFFFFFFFFFFFF)
            topo_idx = len(reordering)
            diff = reader.varint()
            for dep_dist in range(topo_idx):
                dep_topo_idx = topo_idx - 1 - dep_dist
                if new_ancestors >> dep_topo_idx & 1:
                    continue
                if diff == 0:
                    new_ancestors |= topo.ancestors[dep_topo_idx]
                    diff = reader.varint()
                else:
                    diff -= 1
        except EOFError:
            read_error = True
        if size == 0:
            break
        topo_idx = topo.add_transaction(FeeFrac(fee, size))
        topo.add_dependencies(new_ancestors, topo_idx)
        diff %= _SET_SIZE
        if diff <= total_size:
            reordering = [pos + (pos >= total_size - diff) for pos in reordering]
            reordering.append(total_size - diff)
            total_size += 1
        else:
            total_size = diff
            reordering.append(total_size)
            total_size += 1
        if read_error:
            break
    return DepGraph.remapped(topo, reordering, total_size)


def _optimal_vectors() -> list[tuple[str, list[int]]]:
    text = _CORE_TESTS.read_text(encoding="utf-8")
    found = re.findall(
        r'TestOptimalLinearization\("([0-9a-f]*)"_hex_u8, \{([0-9, ]*)\}\);', text
    )
    return [(enc, [int(i) for i in lin.split(",")]) for enc, lin in found]


def _serialization_vectors() -> list[tuple[_Cluster, str]]:
    text = _CORE_TESTS.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    found = re.findall(
        r"TestDepGraphSerialization<TestBitSet>\(\s*(.*?),\s*((?:\"[0-9a-f]*\"\s*)+)\);",
        text,
        flags=re.DOTALL,
    )
    vectors = []
    for cluster, strings in found:
        literal = cluster.replace("{", "[").replace("}", "]").replace("HOLE", "None")
        hexenc = "".join(re.findall(r'"([0-9a-f]*)"', strings))
        vectors.append((ast.literal_eval(literal), hexenc))
    return vectors


_OPTIMAL = _optimal_vectors()
_SERIALIZED = _serialization_vectors()


def test_every_vector_is_read() -> None:
    """Each call in Core's file is a vector here: the regexes miss none."""
    text = _CORE_TESTS.read_text(encoding="utf-8")
    # the definition of each helper is one more match than its calls
    assert len(_OPTIMAL) == text.count("TestOptimalLinearization(") - 1
    assert len(_SERIALIZED) == text.count("TestDepGraphSerialization<TestBitSet>(")
    assert _OPTIMAL
    assert _SERIALIZED


def _graph_from(cluster: _Cluster) -> DepGraph:
    """Build Core's `TestDepGraphSerialization` cluster, holes removed."""
    depgraph = DepGraph()
    holes = 0
    for i, entry in enumerate(cluster):
        if entry is None:
            depgraph.add_transaction(FeeFrac(0, 0x3FFFFF))
            holes |= 1 << i
        else:
            depgraph.add_transaction(FeeFrac(*entry[0]))
    for i, entry in enumerate(cluster):
        if entry is not None:
            depgraph.add_dependencies(sum(1 << p for p in entry[1]), i)
    depgraph.remove_transactions(holes)
    return depgraph


def _same(a: DepGraph, b: DepGraph) -> bool:
    """Core's `DepGraph` equality: positions, then each used entry."""
    return a.positions == b.positions and all(
        (a.fees[i], a.sizes[i], a.ancestors[i], a.descendants[i])
        == (b.fees[i], b.sizes[i], b.ancestors[i], b.descendants[i])
        for i in range(a.position_range)
        if a.positions >> i & 1
    )


@pytest.mark.parametrize(
    ("cluster", "hexenc"), _SERIALIZED, ids=range(len(_SERIALIZED))
)
def test_serialization_vectors(cluster: _Cluster, hexenc: str) -> None:
    """Core's `depgraph_ser_tests`, read back: the encoding is the cluster."""
    depgraph = _graph_from(cluster)
    assert depgraph.is_acyclic()
    assert _same(deserialize(bytes.fromhex(hexenc)), depgraph)


def test_a_truncated_encoding_reads_what_it_holds() -> None:
    """Core's deserializer keeps what it read before its input ends."""
    for _, hexenc in _SERIALIZED:
        data = bytes.fromhex(hexenc)
        counts = [deserialize(data[:end]).tx_count for end in range(len(data) + 1)]
        assert counts == sorted(counts)
        assert counts[-1] == deserialize(data).tx_count


def test_a_new_transaction_takes_the_lowest_hole() -> None:
    """Core's `AddTransaction`: a removed position is used again."""
    depgraph = DepGraph()
    for fee in range(3):
        depgraph.add_transaction(FeeFrac(fee, 1))
    depgraph.add_dependencies(0b001, 1)
    depgraph.remove_transactions(0b010)
    assert depgraph.add_transaction(FeeFrac(7, 2)) == 1
    assert depgraph.feerate(1) == FeeFrac(7, 2)
    assert (depgraph.ancestors[1], depgraph.descendants[1]) == (0b010, 0b010)
    assert depgraph.add_transaction(FeeFrac(8, 2)) == 3


def test_an_empty_cluster_linearizes_to_nothing() -> None:
    """No chunk to optimize: `PickChunkToOptimize` finds none."""
    lin, optimal, _ = linearize(DepGraph(), 100, 0)
    assert (lin, optimal) == ([], True)


def test_equal_transactions_in_a_chunk_go_by_position() -> None:
    """Two equal children of one parent: the lower position first."""
    depgraph = DepGraph()
    for feerate in (FeeFrac(0, 1), FeeFrac(10, 1), FeeFrac(10, 1)):
        depgraph.add_transaction(feerate)
    depgraph.add_dependencies(0b001, 1)
    depgraph.add_dependencies(0b001, 2)
    lin, _, _ = linearize(depgraph, 10**6, 0)
    assert lin == [0, 1, 2]
    _check_within_chunks(depgraph, lin, chunk_linearization_info(depgraph, lin))


def _check_topological(depgraph: DepGraph, linearization: list[int]) -> None:
    """Each transaction once, after all its ancestors."""
    assert sorted(linearization) == [
        i for i in range(depgraph.position_range) if depgraph.positions >> i & 1
    ]
    done = 0
    for i in linearization:
        done |= 1 << i
        assert depgraph.ancestors[i] & ~done == 0


@pytest.mark.parametrize(("hexenc", "expected"), _OPTIMAL, ids=range(len(_OPTIMAL)))
def test_optimal_vectors(hexenc: str, expected: list[int]) -> None:
    """Core's `depgraph_optimal_tests`: each input gives the one optimum.

    Core's four kinds of input linearization, from a random seed each:
    none, the last result, a random topological one, and a random one.
    """
    depgraph = deserialize(bytes.fromhex(hexenc))
    rng = random.Random(hexenc)
    lin: list[int] = []
    for kind in range(4):
        is_topological = True
        if kind == 0:
            lin = []
        elif kind == 2:
            rng.shuffle(lin)
            lin.sort(key=lambda i: depgraph.ancestors[i].bit_count())
        elif kind == 3:
            rng.shuffle(lin)
            is_topological = False
        lin, optimal, cost = linearize(
            depgraph,
            10**12,
            rng.getrandbits(64),
            old_linearization=lin,
            is_topological=is_topological,
        )
        assert optimal
        assert cost <= _max_optimal_cost(depgraph.tx_count)
        _check_topological(depgraph, lin)
        assert lin == expected


# --- properties, against an exhaustive search ---


@st.composite
def depgraphs(draw: st.DrawFn, max_count: int = 7) -> DepGraph:
    """Draw an acyclic cluster: each transaction may spend any earlier one."""
    count = draw(st.integers(1, max_count))
    depgraph = DepGraph()
    for i in range(count):
        fee = draw(st.integers(-20, 60))
        size = draw(st.integers(1, 12))
        depgraph.add_transaction(FeeFrac(fee, size))
        parents = draw(st.integers(0, (1 << i) - 1)) if i else 0
        depgraph.add_dependencies(parents, i)
    return depgraph


def _exhaustive(depgraph: DepGraph) -> list[FeeFrac]:
    """Return the best diagram over every topological order.

    Core's `ExhaustiveLinearize`: more chunks win a tie, the minimal chunks.
    """
    best: list[FeeFrac] | None = None
    positions = list(range(depgraph.position_range))
    for perm in itertools.permutations(positions):
        done, valid = 0, True
        for i in perm:
            done |= 1 << i
            if depgraph.ancestors[i] & ~done:
                valid = False
                break
        if not valid:
            continue
        chunks = chunk_linearization(depgraph, perm)
        if best is None:
            best = chunks
            continue
        cmp = compare_chunks(chunks, best)
        if cmp == 1 or (cmp == 0 and len(chunks) > len(best)):
            best = chunks
    assert best is not None
    return best


@given(depgraphs(), st.integers(0, 2**64 - 1))
def test_linearize_is_optimal_and_minimal(depgraph: DepGraph, seed: int) -> None:
    """With work enough, the diagram is the best, in its smallest chunks."""
    lin, optimal, _ = linearize(depgraph, 10**12, seed)
    assert optimal
    _check_topological(depgraph, lin)
    chunks = chunk_linearization(depgraph, lin)
    best = _exhaustive(depgraph)
    assert compare_chunks(chunks, best) == 0
    assert len(chunks) == len(best)


@given(depgraphs(max_count=10), st.integers(0, 2**64 - 1), st.integers(0, 2**64 - 1))
def test_an_optimal_result_does_not_depend_on_the_seed(
    depgraph: DepGraph, seed1: int, seed2: int
) -> None:
    """Core's `clusterlin_linearize`: two seeds, one optimal linearization.

    And its order rules: within a chunk, no later transaction that could
    come first beats an earlier one by feerate, then size, then
    position; nor does a later chunk that could come first.
    """
    lin, _, _ = linearize(depgraph, 10**12, seed1)
    assert linearize(depgraph, 10**12, seed2)[0] == lin
    info = chunk_linearization_info(depgraph, lin)
    _check_within_chunks(depgraph, lin, info)
    _check_across_chunks(depgraph, info)


def _check_within_chunks(
    depgraph: DepGraph, lin: list[int], info: list[SetInfo]
) -> None:
    done, pos = 0, 0
    for chunk in info:
        count = chunk.transactions.bit_count()
        for pos1 in range(pos, pos + count):
            tx1 = lin[pos1]
            for tx2 in lin[pos1 + 1 : pos + count]:
                if (depgraph.ancestors[tx2] & ~done).bit_count() == 1:
                    f1, f2 = depgraph.feerate(tx1), depgraph.feerate(tx2)
                    cmp = feerate_compare(f1, f2)
                    assert cmp > 0 or (cmp == 0 and f1.size <= f2.size)
                    if f1 == f2:
                        assert tx1 < tx2
            done |= 1 << tx1
        pos += count


def _check_across_chunks(depgraph: DepGraph, info: list[SetInfo]) -> None:
    done = 0
    for n1, chunk1 in enumerate(info):
        for chunk2 in info[n1 + 1 :]:
            ancestors = 0
            for tx in range(depgraph.position_range):
                if chunk2.transactions >> tx & 1:
                    ancestors |= depgraph.ancestors[tx]
            if ancestors & ~done & ~chunk2.transactions == 0:
                cmp = feerate_compare(chunk1.feerate, chunk2.feerate)
                assert cmp > 0 or (
                    cmp == 0 and chunk1.feerate.size <= chunk2.feerate.size
                )
                if chunk1.feerate == chunk2.feerate:
                    assert (
                        chunk1.transactions.bit_length()
                        < chunk2.transactions.bit_length()
                    )
        done |= chunk1.transactions


def _a_pair() -> DepGraph:
    """Return a parent and the child paying for it."""
    depgraph = DepGraph()
    depgraph.add_transaction(FeeFrac(1, 2))
    depgraph.add_transaction(FeeFrac(5, 1))
    depgraph.add_dependencies(0b01, 1)
    return depgraph


@given(depgraphs(max_count=10), st.integers(0, 2**64 - 1), st.integers(0, 2000))
@example(_a_pair(), 0, 2000)
def test_a_bounded_run_is_topological_and_no_worse(
    depgraph: DepGraph, seed: int, max_cost: int
) -> None:
    """Out of work, the result is topological and no worse than it was.

    It is held to the topological linearization it was given.
    """
    old: list[int] = []
    depgraph.append_topo(old, depgraph.positions)
    lin, optimal, cost = linearize(depgraph, max_cost, seed, old_linearization=old)
    _check_topological(depgraph, lin)
    old_chunks = chunk_linearization(depgraph, old)
    assert compare_chunks(chunk_linearization(depgraph, lin), old_chunks) in (0, 1)
    # Core's `clusterlin_linearize` sets no bound for a single transaction
    if depgraph.tx_count > 1 and max_cost > _max_optimal_cost(depgraph.tx_count):
        assert optimal
    if not optimal:
        assert cost >= max_cost


def _random_topological(depgraph: DepGraph, rng: random.Random) -> list[int]:
    lin: list[int] = []
    todo = depgraph.positions
    while todo:
        ready = [
            i
            for i in range(depgraph.position_range)
            if todo >> i & 1 and depgraph.ancestors[i] & todo == 1 << i
        ]
        i = rng.choice(ready)
        lin.append(i)
        todo &= ~(1 << i)
    return lin


@given(depgraphs(max_count=10), st.randoms(use_true_random=False))
def test_post_linearize_is_no_worse_and_connects_chunks(
    depgraph: DepGraph, rng: random.Random
) -> None:
    """Core's `clusterlin_postlinearize`: never worse, chunks connected.

    A second pass can improve it again, and never worsens it.
    """
    lin = _random_topological(depgraph, rng)
    before = chunk_linearization(depgraph, lin)
    post = list(lin)
    post_linearize(depgraph, post)
    _check_topological(depgraph, post)
    after = chunk_linearization(depgraph, post)
    assert compare_chunks(after, before) in (0, 1)
    for chunk in chunk_linearization_info(depgraph, post):
        assert depgraph.is_connected(chunk.transactions)
    again = list(post)
    post_linearize(depgraph, again)
    assert compare_chunks(chunk_linearization(depgraph, again), after) in (0, 1)


@given(
    depgraphs(max_count=10),
    st.randoms(use_true_random=False),
    st.integers(0, 0x3FFFF),
)
def test_post_linearize_keeps_a_leaf_moved_to_the_back_no_worse(
    depgraph: DepGraph, rng: random.Random, fee_inc: int
) -> None:
    """Core's `clusterlin_postlinearize_moved_leaf`.

    A leaf moved to the back, its fee raised, and then post-linearized
    is no worse than where it was: what an RBF of a same-size leaf needs.
    """
    lin = _random_topological(depgraph, rng)
    leaf = _random_topological(depgraph, rng)[-1]
    moved = [i for i in lin if i != leaf] + [leaf]
    post_linearize(depgraph, moved)
    _check_topological(depgraph, moved)
    old = chunk_linearization(depgraph, lin)
    depgraph.set_fee(leaf, depgraph.fees[leaf] + fee_inc)
    assert compare_chunks(chunk_linearization(depgraph, moved), old) in (0, 1)


@st.composite
def trees(draw: st.DrawFn) -> DepGraph:
    """Draw a cluster where each transaction has at most one parent.

    Or, the edges reversed, at most one child: Core's `BuildTreeGraph`.
    """
    count = draw(st.integers(1, 7))
    upright = draw(st.booleans())
    depgraph = DepGraph()
    for _ in range(count):
        depgraph.add_transaction(
            FeeFrac(draw(st.integers(-20, 60)), draw(st.integers(1, 12)))
        )
    for i in range(1, count):
        if (other := draw(st.integers(-1, i - 1))) >= 0:
            parent, child = (other, i) if upright else (i, other)
            depgraph.add_dependencies(1 << parent, child)
    return depgraph


@given(trees(), st.randoms(use_true_random=False))
def test_post_linearize_makes_a_tree_optimal(
    depgraph: DepGraph, rng: random.Random
) -> None:
    """Core's `clusterlin_postlinearize_tree`: its two passes are optimal."""
    lin = _random_topological(depgraph, rng)
    post_linearize(depgraph, lin)
    _check_topological(depgraph, lin)
    optimum = _exhaustive(depgraph)
    assert compare_chunks(chunk_linearization(depgraph, lin), optimum) == 0


@given(depgraphs(max_count=10), st.integers(0, 2**64 - 1))
def test_post_linearize_keeps_an_optimal_diagram(depgraph: DepGraph, seed: int) -> None:
    """What Core's `Relinearize` does after an optimal `Linearize`."""
    lin, _, _ = linearize(depgraph, 10**12, seed)
    chunks = chunk_linearization(depgraph, lin)
    post_linearize(depgraph, lin)
    _check_topological(depgraph, lin)
    assert chunk_linearization(depgraph, lin) == chunks


@given(depgraphs(max_count=10), st.integers(0, 2**64 - 1))
def test_the_diagram_of_an_optimal_state(depgraph: DepGraph, seed: int) -> None:
    """Core's `clusterlin_sfl`: a minimal state's diagram is its order's.

    The two are compared chunk for chunk.
    """
    forest = SpanningForestState(depgraph, seed)
    forest.make_topological()
    forest.start_optimizing()
    while forest.optimize_step():
        pass
    forest.start_minimizing()
    while forest.minimize_step():
        pass
    lin = forest.get_linearization(lambda i: i)
    assert sorted(chunk_linearization(depgraph, lin), key=_by_feefrac) == sorted(
        forest.get_diagram(), key=_by_feefrac
    )


def _by_feefrac(f: FeeFrac) -> tuple[Fraction, int]:
    return Fraction(f.fee, f.size), -f.size


# --- the diagram comparison, against an evaluation at every point ---


def _evaluate(size: int, diagram: list[tuple[int, int]]) -> Fraction:
    """Return the fee of `diagram` at `size`, flat past its ends."""
    if size <= diagram[0][1]:
        return Fraction(diagram[0][0])
    for (fee_a, size_a), (fee_b, size_b) in itertools.pairwise(diagram):
        if size_a <= size <= size_b:
            return fee_a + Fraction((fee_b - fee_a) * (size - size_a), size_b - size_a)
    return Fraction(diagram[-1][0])


def _points(chunks: list[FeeFrac]) -> list[tuple[int, int]]:
    points = [(0, 0)]
    for fee, size in chunks:
        points.append((points[-1][0] + fee, points[-1][1] + size))
    return points


feefracs = st.builds(FeeFrac, st.integers(-(2**30), 2**30), st.integers(1, 10**6))


@given(st.lists(feefracs, max_size=8), st.lists(feefracs, max_size=8))
def test_compare_chunks(chunks0: list[FeeFrac], chunks1: list[FeeFrac]) -> None:
    """Core's `build_and_compare_feerate_diagram`.

    Its `CompareDiagrams` evaluates each diagram at the other's points.
    """
    dia0, dia1 = _points(chunks0), _points(chunks1)
    ge = le = True
    for diagram, other, sign in ((dia0, dia1, 1), (dia1, dia0, -1)):
        for fee, size in diagram:
            value = _evaluate(size, other)
            if fee < value:
                ge, le = (False, le) if sign == 1 else (ge, False)
            if fee > value:
                ge, le = (ge, False) if sign == 1 else (False, le)
    expected = {(True, True): 0, (True, False): 1, (False, True): -1}.get((ge, le))
    assert compare_chunks(chunks0, chunks1) == expected


def _core_runs() -> list[tuple[str, str]]:
    lines = (_DATA / "core_linearize_runs.txt").read_text().splitlines()
    return list(zip(lines[::2], lines[1::2], strict=True))


_CORE_RUNS = _core_runs()


@pytest.mark.parametrize(("case", "answer"), _CORE_RUNS, ids=range(len(_CORE_RUNS)))
def test_core_s_own_runs(case: str, answer: str) -> None:
    """Core v31.1's `Linearize` and `PostLinearize`, run on the same input.

    The order, `optimal` and cost of a run, then the order `PostLinearize`
    makes of a topological one. `tests/_data/README.md` says how they were
    made, and the one path of `linearize` they do not reach.
    """
    words = [int(w) for w in case.split()]
    count, rest = words[0], words[1:]
    depgraph = DepGraph()
    for fee, size, parents in zip(*[iter(rest[: 3 * count])] * 3, strict=True):
        depgraph.add_dependencies(parents, depgraph.add_transaction(FeeFrac(fee, size)))
    max_cost, seed, old, _, *post = rest[3 * count :]
    lin, optimal, cost = linearize(
        depgraph, max_cost, seed, old_linearization=post if old else ()
    )
    post_linearize(depgraph, post)
    lin_text, run, post_text = answer.split("|")
    assert lin == [int(w) for w in lin_text.split()]
    assert [int(optimal), cost] == [int(w) for w in run.split()]
    assert post == [int(w) for w in post_text.split()]


def test_insecure_random_context() -> None:
    """Core's `xoroshiro128plusplus_reference_values`: two seeds' outputs.

    `src/test/random_tests.cpp`, at bitcoin/bitcoin@9be056a8a7.
    """
    rng = InsecureRandomContext(0)
    assert [rng.rand64() for _ in range(4)] == [
        0x6F68E1E7E2646EE1,
        0xBF971B7F454094AD,
        0x48F2DE556F30DE38,
        0x6EA7C59F89BBFC75,
    ]
    rng = InsecureRandomContext(0x1A26F3FA8546B47A)
    assert [rng.rand64() for _ in range(4)] == [
        0xC8DC5E08D844AC7D,
        0x5B5F1F6D499DAD1B,
        0xBEB0031F93313D6F,
        0xBFBCF4F43A264497,
    ]


def test_randbits_draws_from_a_buffer() -> None:
    """Core's `RandomMixin::randbits`: small draws share one output."""
    rng = InsecureRandomContext(0)
    first = 0x6F68E1E7E2646EE1
    assert rng.randbits(1) == first & 1
    assert rng.randbits(3) == (first >> 1) & 7
    assert rng.randbits(64) == 0xBF971B7F454094AD
    # 60 bits remain buffered, so 62 bits draw a new output beneath them
    third = 0x48F2DE556F30DE38
    assert rng.randbits(62) == ((third << 60) | (first >> 4)) & ((1 << 62) - 1)
    assert rng.randrange(1) == 0
