# Vendored test vectors

Where every file under a `tests/**/_data/` directory came from, and
whether our copy still matches it.

The test modules already cite their sources, but a citation names a
path, and a path changes under us: it says what a vector *is* and
nothing about which revision of it we hold. That is what this file is
for, and it is not a mirror of those citations — a citation naming the
wrong upstream is corrected in the module, not here.

Vectors are vendored, never fetched at test time. A test that downloads
its own input has a verdict that depends on somebody else's uptime, and
a suite that cannot run offline is a suite that cannot run in a
sandbox. A vector we fail is vendored anyway and marked `xfail`, never
left out: an absent vector hides the defect it would have shown.

## Re-checking a pin

```shell
git hash-object tests/unit/chainstate/_data/blockfilters.json
gh api repos/bitcoin/bitcoin/git/trees/<commit>:src/test/data \
    --jq '.tree[] | select(.path == "blockfilters.json") | .sha'
```

The comparison is on the git blob SHA-1, not on a sha256 of the
contents: it is what a tree entry already carries, so nothing has to be
downloaded to compare against, and `git hash-object` reproduces it
locally. Whether the pinned commit is still the newest to touch that
path:

```shell
path=src/test/data/blockfilters.json
gh api "repos/bitcoin/bitcoin/commits?path=$path&per_page=1" --jq '.[0].sha'
```

`.github/workflows/vendored-vectors.yml` runs both commands weekly, over
every heading below carrying a full `repo`/`path`/`commit`/`blob`
quadruple, and fails where either has moved. `pulled` and `behind` stay
manual: refreshing a drifted pin is a decision the workflow does not get
to make, so a red run is answered by hand, and `pulled` is updated to
the date that answer was reached.

## `tests/unit/chainstate/_data/blockfilters.json`

```text
repo    bitcoin/bitcoin
path    src/test/data/blockfilters.json
commit  c7efb652f3543b001b4dd22186a354605b14f47e  2019-04-06
blob    8945296a079b984d65b0aeb4a3e9b0798df075e0
pulled  2026-08-25
behind  0 revisions; that commit is the tip of the path
```

Bitcoin Core's BIP158 vector file: ten testnet blocks, each row a
height, a block hash, the whole serialized block, the previous output
scripts the block does not carry, the previous basic filter header, and
the two answers — the serialized basic filter and the basic header
chained onto it.

`btclib` vendors the same file, byte for byte, and tests
`btclib.block.block_filter` with it. The copy here is not that test
again: what it holds this tree to is the *index* built on top —
`FilterIndex` storing a filter, chaining its header onto the one before
it, and answering the two back — and the genesis block `chains.py`
builds, whose filter is row 0.

## `tests/unit/chainstate/_data/BITCOIN_CORE_COPYING`

```text
repo    bitcoin/bitcoin
path    COPYING
commit  b23b901363c56043c536f32261ac8cb540624a84  2025-12-29
blob    89960cbf2f221a29852ed162b25bda2afc0b2dd6
pulled  2026-09-26
behind  0 revisions; that commit is the tip of the path
```

Bitcoin Core's licence, MIT, which `blockfilters.json` is distributed
under and which travels with it, in the sdist too. The name keeps it
from reading as the licence of the directory: `testnet_bip158_vectors.json`
beside it is this tree's own.

## `tests/unit/chainstate/_data/testnet_bip158_vectors.json`

Not vendored — derived. Two testnet blocks Core's own file above does
not carry (#181): height 54499 is forty-odd kilobytes and twenty-four
transactions, most of them resolving a previous output from elsewhere in
the same block, and height 54503 is a positive control. Both were pulled
from testnet by the block hash #181 names, parsed with
`btclib.block.Block`, and their basic filters built with
`btclib.block.block_filter.BasicBlockFilter.from_block` — this tree's
own dependency, the same one `FilterIndex` calls. Height 54503's filter,
`06294070f18c8b0ff84b92738259ca89b4`, matches what an independent
SipHash-2-4 and Golomb-Rice implementation in Libbitcoin's test suite
computed for the same block; no file of Libbitcoin's, AGPL-3.0-or-later,
is copied here — only the block hash and that one filter, checked
against, ever came from that survey.

The row shape matches `blockfilters.json`'s, with one column meaning
something different: neither Core nor Libbitcoin publishes a filter
*header* for these two blocks, so "Previous Basic Header" and "Basic
Header" are computed here rather than taken from a source, and they
chain only within this file — height 54499's previous header is
BIP157's all-zero genesis value, and height 54503's is 54499's own
header from the row above it, the two not being adjacent blocks on
testnet. `tests/unit/chainstate/filter_index_test.py` is the only
reader of either column.

Re-checked by re-running the derivation, not by a blob pin — there is
no upstream copy to fall out of step with:

```shell
uv run pytest tests/unit/chainstate/filter_index_test.py -k scale
```

## `tests/unit/chainstate/_data/regtest_hash_serialized_3.json`

Not vendored -- derived. A regtest chain of 103 blocks mined by Bitcoin
Core v31.1.0 (`bitcoind -regtest`, `setmocktime 1700000000`): 101 blocks
to a wallet address, a block holding one transaction with 301 outputs,
and a block spending two of them (outputs 3 and 270). `blocks` is each
block as `getblock <hash> 0` printed it, heights 1 to 103, and
`gettxoutsetinfo` is that node's own answer to `gettxoutsetinfo` and
`gettxoutsetinfo muhash` at height 103, the fields the tests compare.

The hashes are Core's, not this tree's. The wide transaction is what
makes the vector check the order the coins are folded in: the store
sorts output 256 before output 1, and Core's `ComputeUTXOStats` sorts
them numerically. There is no upstream copy to pin; the tests that read
the file are the check:

```shell
uv run pytest tests/unit/chainstate/utxo_index_test.py -k serialized_hash
```

## `tests/unit/_data/cluster_linearize_tests.cpp`

```text
repo    bitcoin/bitcoin
path    src/test/cluster_linearize_tests.cpp
commit  ecc9a84f854e5b77dfc8876cf7c9b8d0f3de89d0  2026-02-24
blob    4f851c1d5ff7b59b8d51bdb4fbb059729ce72199
pulled  2026-10-08
behind  0 revisions; that commit is the tip of the path
```

Bitcoin Core's unit tests of cluster linearization, vendored whole
rather than as an extract: the vectors are calls in C++, and
`tests/unit/cluster_linearize_test.py` reads them out of the file with a
regex and counts them against the calls. Each `TestOptimalLinearization`
is a serialized cluster with its one optimal linearization, and each
`TestDepGraphSerialization` a cluster with its serialization.
The blob is also the file at the v31.1 tag, bitcoin/bitcoin@9be056a8a7.

## `tests/unit/_data/core_linearize_runs.txt`

Not vendored -- derived. Clusters run through Bitcoin Core v31.1's own
`Linearize` and `PostLinearize`, two lines a case: the input, and what
Core printed. The odd lines are what this script prints, under
`python3 -I`:

```python
import random

r = random.Random(1499)


def case(n, fee_size, density, budget):
    parents = [sum(1 << j for j in range(i) if r.random() < density) for i in range(n)]
    txs = [(*fee_size(), parents[i]) for i in range(n)]
    order, done = [], 0
    while len(order) < n:
        ready = [i for i in range(n) if not done >> i & 1 and not parents[i] & ~done]
        order.append(r.choice(ready))
        done |= 1 << order[-1]
    old = r.randint(0, 1)
    print(n, *(w for t in txs for w in t), budget, r.getrandbits(64), old, n, *order)


# any feerates, any budget
for _ in range(60):
    case(
        r.randint(1, r.choice([5, 12, 30, 64])),
        lambda: (r.randint(-50, 500), r.randint(1, 300)),
        r.choice([0.05, 0.2, 0.5]),
        r.choice([0, 1, 50, 300, 2000, 10**9]),
    )
# equal feerates, which reach the minimizing splits
for _ in range(60):
    rate = r.choice([1, 2, 2, 3])
    case(
        r.randint(2, 20),
        lambda: (lambda size: (rate * size, size))(r.randint(1, 5)),
        0.25,
        r.choice([50, 2000, 10**9]),
    )
# large clusters worked to the end, which leave stale queue entries
for _ in range(30):
    case(
        r.randint(20, 64),
        lambda: (r.randint(-50, 500), r.randint(1, 300)),
        r.choice([0.03, 0.1, 0.3]),
        10**9,
    )
# no chunk to pick at all
case(0, None, 0, 10**9)
```

`core_linearize.cpp`, beside the file, is the program that read the odd
lines and printed the even ones, compiled with Apple clang 21 against
`src/` at bitcoin/bitcoin@9be056a8a7, the v31.1 tag:

```shell
git -C <bitcoin> archive 9be056a8a7 src | tar -x -C <dir>
clang++ -std=c++20 -O1 -I<dir>/src \
    -o core_linearize tests/unit/_data/core_linearize.cpp
awk 'NR % 2' tests/unit/_data/core_linearize_runs.txt | ./core_linearize
```

The last command's output is the file's even lines. The runs pin a
run's order, `optimal` and cost, the random draws behind them, and the
order of `PostLinearize`'s two passes. They reach every line of
`SpanningForestState`, and every line of `linearize` but its call to
`make_topological` after an old linearization that is not topological:
that path is not pinned. There is no upstream copy to pin; the test
that reads the file is the check:

```shell
uv run pytest tests/unit/cluster_linearize_test.py -k core_s_own_runs
```

## `tests/unit/_data/core_rolling_bloom_runs.txt`

Not vendored -- derived. Keys run through Bitcoin Core v31.1's own
`CRollingBloomFilter`, two lines a case: the input, and what Core
printed. The odd lines are what this script prints, under `python3 -I`:

```python
import random

r = random.Random(1743)
keys = [r.randbytes(n).hex() for n in range(41)]


def case(n, fp, tweak, *ops):
    print(n, fp, tweak, *(f"+{k}" for k in keys), *ops, *(f"?{k}" for k in keys))


# every MurmurHash3 tail, three generations wiped, both ends of the tweak
for tweak in (0, 0xFFFF_FFFF, r.getrandbits(32)):
    case(100, 0.01, tweak, "+n", 0, 300, "?n", 0, 400)
case(1000, 0.001, r.getrandbits(32), "+n", 0, 2600, "?n", 0, 3000)
# Core's own parameters for the transactions a peer knows
ask = ("?n", 0, 1000, "?n", 24990, 20, "?n", 79000, 1000, "?n", 100000, 1000)
case(50000, 0.000001, r.getrandbits(32), "+n", 0, 80000, *ask)
# one hash function, one key a generation
case(1, 0.5, r.getrandbits(32))
# fifty hash functions, the most Core takes
case(10, 1e-30, r.getrandbits(32))
# a ratio below a half, rounded to none and raised to one
case(10, 0.99, r.getrandbits(32))
# a ratio of exactly two and a half, rounded away from zero
case(20, 0.1767766952966369, r.getrandbits(32), "?n", 0, 100)
# Core's parameters for its other filters, each reset to a new tweak
for n, fp in ((120000, 0.000001), (48000, 0.000001), (5000, 0.001)):
    reset = f"!{r.getrandbits(32)}"
    ops = ("+n", 0, 3000, reset, "+n", 3000, 1000, "?n", 0, 4000)
    case(n, fp, r.getrandbits(32), *ops)
```

`core_rolling_bloom.cpp`, beside the file, is the program that read the
odd lines and printed the even ones, compiled with Apple clang 21. Its
filter is Core's own code, cut out of `src/hash.cpp` and
`src/common/bloom.cpp` at bitcoin/bitcoin@9be056a8a7, the v31.1 tag, but
for `reset`, which takes the tweak from the input:

```shell
git -C <bitcoin> archive 9be056a8a7 src | tar -x -C <dir>
git -C <bitcoin> show 9be056a8a7:src/hash.cpp |
  sed -n '/^unsigned int MurmurHash3/,/^}/p' > <dir>/core_rolling_bloom.inc
git -C <bitcoin> show 9be056a8a7:src/common/bloom.cpp |
  sed -n '/^CRollingBloomFilter::/,/::reset()$/p' |
  sed '$d' >> <dir>/core_rolling_bloom.inc
clang++ -std=c++20 -O1 -I<dir>/src -I<dir> \
    -o core_rolling_bloom tests/unit/_data/core_rolling_bloom.cpp
awk 'NR % 2' tests/unit/_data/core_rolling_bloom_runs.txt | ./core_rolling_bloom
```

The last command's output is the file's even lines. There is no upstream
copy to pin; the test that reads the file is the check:

```shell
uv run pytest tests/unit/rolling_bloom_test.py -k core_s_own_runs
```

## `tests/unit/_data/BITCOIN_CORE_COPYING`

```text
repo    bitcoin/bitcoin
path    COPYING
commit  b23b901363c56043c536f32261ac8cb540624a84  2025-12-29
blob    89960cbf2f221a29852ed162b25bda2afc0b2dd6
pulled  2026-10-08
behind  0 revisions; that commit is the tip of the path
```

Bitcoin Core's licence, MIT, which `cluster_linearize_tests.cpp` is
distributed under, travelling with it as
`tests/unit/chainstate/_data/BITCOIN_CORE_COPYING` travels with
`blockfilters.json`.
