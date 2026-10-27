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

## `tests/unit/chainstate/_data/mainnet_headers_1_32300.xz`

Not vendored -- derived. The mainnet headers of heights 1 to 32,300, each
as `getblockheader <hash> false` of a Bitcoin Core v31.1.0 node printed it,
over its RPC. Height 32,256 is the first retarget that changes the target.
The expected hashes, bits and chain work in
`tests/unit/chainstate/real_header_chains_test.py` are that node's
`getblockheader` answers at heights 32,255, 32,256 and 32,300; a second
Core v31.1.0 with no peer, given the same headers by `submitheader`,
answered the same tip and chain work. `chainparams.cpp` holds no
checkpoint to compare a hash with.

The file keeps, for each header, the 48 bytes its predecessor does not
give (version, merkle root, time, bits, nonce: the 80 bytes less the
previous hash, bytes 4 to 36), concatenated and compressed. From the
80-byte headers, in order, as `headers`:

```python
import lzma

records = b"".join(header[:4] + header[36:] for header in headers)
data = lzma.compress(
    records,
    format=lzma.FORMAT_XZ,
    check=lzma.CHECK_CRC64,
    preset=9 | lzma.PRESET_EXTREME,
)
```

A regenerated file need not match byte for byte, only
`lzma.decompress` of it. The test pins the file's sha256,
`ce13c5e5b6be2a2022542151b4039dd8b4d8492a793caa876e1960e9d8d42274`,
and is the check:

```shell
shasum -a 256 tests/unit/chainstate/_data/mainnet_headers_1_32300.xz
uv run pytest tests/unit/chainstate/real_header_chains_test.py
```

The three nonces in that test's `CORRUPTIONS` were found offline, one
search each over the 32-bit nonce, and cannot be regenerated from this
file. `assert_valid_pow` in the test checks that each meets its own
target, which is what makes the context the only reason to refuse it.

## `tests/unit/chainstate/_data/testnet4_headers_1_8100.xz`

Not vendored -- derived. The testnet4 headers of heights 1 to 8,100, the
answer of a testnet4 peer (`seed.testnet4.wiz.biz`) to `getheaders` from
the genesis. The heights 1 to 8,100 of
`https://mempool.space/testnet4/api/v1/blocks/` are the same bytes.
Height 7,465 is a minimum difficulty block and height 8,064 the retarget
after it. The expected hashes, bits and chain work are the
`getblockheader` answers of a Bitcoin Core v31.1.0 on testnet4 with no
peer, given the headers by `submitheader`, at heights 7,464, 7,465,
8,063, 8,064 and 8,100.

The 48-byte records and the compression are the mainnet entry's. The
test pins the sha256
`860b6d82c242de36076b1229c2444c09e1a39419718f3dbfeaa7c6829be2a591` and is
the check, as above. The nonces of the corruption tests are those the
mainnet entry names.

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

## `tests/unit/_data/test_framework/v2_p2p.py`

```text
repo    bitcoin/bitcoin
path    test/functional/test_framework/v2_p2p.py
commit  6a129983c9bf8efa1081f9a8b462c3635d1cfb39  2026-06-04
blob    4057fd9a882322f8a8149bb318d383e0c97d05bc
pulled  2026-10-27
behind  0 revisions; that commit is the tip of the path
```

Bitcoin Core's functional-test v2 peer, `EncryptedP2PState`, vendored
byte for byte with the crypto files below. Each was taken at
bitcoin/bitcoin@de96f3ae6d16, and its `commit` is the last to touch it
there. `tests/unit/p2p/v2transport_core_sender_test.py` loads them by
path and drives `V2Transport` with decoys from a sender that shares no
code with `btclib.p2p.bip324`.

`v2_p2p.py`, `ellswift.py` and `secp256k1.py` also import from Core's
`key.py`, `messages.py` and `util.py`, which are not vendored: the test
module's docstring says why, and what stands in for them.

## `tests/unit/_data/test_framework/crypto/bip324_cipher.py`

```text
repo    bitcoin/bitcoin
path    test/functional/test_framework/crypto/bip324_cipher.py
commit  fa5f29774872d18febc0df38831a6e45f3de69cc  2025-12-16
blob    d0e94ac588ca99d3dce92adba343cccf698fff50
pulled  2026-10-27
behind  0 revisions; that commit is the tip of the path
```

Core's ChaCha20-Poly1305 AEAD and `FSChaCha20Poly1305`, which encrypts
each packet.

## `tests/unit/_data/test_framework/crypto/chacha20.py`

```text
repo    bitcoin/bitcoin
path    test/functional/test_framework/crypto/chacha20.py
commit  fa5f29774872d18febc0df38831a6e45f3de69cc  2025-12-16
blob    00197b99db21e23df634a3ef9ad1129450c2217b
pulled  2026-10-27
behind  0 revisions; that commit is the tip of the path
```

Core's ChaCha20 and `FSChaCha20`, which encrypts each packet's length.

## `tests/unit/_data/test_framework/crypto/poly1305.py`

```text
repo    bitcoin/bitcoin
path    test/functional/test_framework/crypto/poly1305.py
commit  fa5f29774872d18febc0df38831a6e45f3de69cc  2025-12-16
blob    866a55b0c3b7ef5a08cf9b05fc67641f5423820f
pulled  2026-10-27
behind  0 revisions; that commit is the tip of the path
```

Core's Poly1305, which `bip324_cipher.py` imports.

## `tests/unit/_data/test_framework/crypto/hkdf.py`

```text
repo    bitcoin/bitcoin
path    test/functional/test_framework/crypto/hkdf.py
commit  fa5f29774872d18febc0df38831a6e45f3de69cc  2025-12-16
blob    86c551efecdd997534d7cee3313e12eb9916d3a6
pulled  2026-10-27
behind  0 revisions; that commit is the tip of the path
```

Core's HKDF-SHA256, which derives the session's keys.

## `tests/unit/_data/test_framework/crypto/ellswift.py`

```text
repo    bitcoin/bitcoin
path    test/functional/test_framework/crypto/ellswift.py
commit  3fd68a95e68b4c6f3bb6c59d41dd196001110f3a  2026-04-07
blob    e73da525e72fcf6642b8634cb0c1ff0aab59f12c
pulled  2026-10-27
behind  0 revisions; that commit is the tip of the path
```

Core's ElligatorSwift encoding and the x-only ECDH over it.

## `tests/unit/_data/test_framework/crypto/secp256k1.py`

```text
repo    bitcoin/bitcoin
path    test/functional/test_framework/crypto/secp256k1.py
commit  b6fc3cf0046d3106e0dfb30f320d48514eff6fb2  2026-09-28
blob    7978acee7d2204fcb96ab51365233bf1a6cbbc96
pulled  2026-10-27
behind  0 revisions; that commit is the tip of the path
```

Core's secp256k1 field and group arithmetic, which `ellswift.py`
imports.

## `tests/unit/_data/BITCOIN_CORE_COPYING`

```text
repo    bitcoin/bitcoin
path    COPYING
commit  b23b901363c56043c536f32261ac8cb540624a84  2025-12-29
blob    89960cbf2f221a29852ed162b25bda2afc0b2dd6
pulled  2026-10-08
behind  0 revisions; that commit is the tip of the path
```

Bitcoin Core's licence, MIT, which `cluster_linearize_tests.cpp` and
the files under `test_framework/` are distributed under, travelling with
them as `tests/unit/chainstate/_data/BITCOIN_CORE_COPYING` travels with
`blockfilters.json`.

## `tests/unit/chainstate/_data/btcd/blk_0_to_4.dat.bz2`

```text
repo    btcsuite/btcd
path    blockchain/testdata/blk_0_to_4.dat.bz2
commit  b69a849114ea871b49a27bda2ebad36d460f8137  2015-01-30
blob    274c710d275f032791bcbd4b463be9c1901b85ed
sha256  e5844b96629a675c169d013c1eb86ce542a1936fcf199f6f60de4ffc2a5b1a64
pulled  2026-10-14
behind  0 revisions; that commit is the tip of the path
```

The genesis block and four blocks mined on top of it at mainnet's easiest
target, as btcd's `TestHaveBlock` and `TestCheckConnectBlockTemplate` load
them: records of `<magic><length><block>`, bzip2-compressed, vendored
byte for byte. The blocks after the genesis are not mainnet's: block 1's
hash is `00000000ebe5ec3e...`, not `00000000839a8e68...`. Blocks 2 and 4
spend the coinbases of blocks 1 and 2, which btcd's tests allow by
lowering coinbase maturity to 1.

## `tests/unit/chainstate/_data/btcd/blk_3A.dat.bz2`

```text
repo    btcsuite/btcd
path    blockchain/testdata/blk_3A.dat.bz2
commit  b69a849114ea871b49a27bda2ebad36d460f8137  2015-01-30
blob    01266565d573ca0f7e7601d011fb4f98e243445a
sha256  4447be6e520ff9818a68a1c1c74ff99ca1c8085cc8b0ceaf57f4664f5fc356f7
pulled  2026-10-14
behind  0 revisions; that commit is the tip of the path
```

Block 3a, a second child of block 2, in the same format.

## `tests/unit/chainstate/_data/btcd/blk_4A.dat.bz2`

```text
repo    btcsuite/btcd
path    blockchain/testdata/blk_4A.dat.bz2
commit  b69a849114ea871b49a27bda2ebad36d460f8137  2015-01-30
blob    19b409e752f37840b271e4e9f0b90dcd06e1cad3
sha256  c6023b85c07e8fbcbf704ea896879c3a6a0f5b88edd520197f820fd76436032c
pulled  2026-10-14
behind  0 revisions; that commit is the tip of the path
```

Block 4a, the child of 3a, in the same format. No test of
btcsuite/btcd@c615bd709 loads it.

## `tests/unit/chainstate/_data/btcd/blk_5A.dat.bz2`

```text
repo    btcsuite/btcd
path    blockchain/testdata/blk_5A.dat.bz2
commit  b69a849114ea871b49a27bda2ebad36d460f8137  2015-01-30
blob    47bff9038bdd596708d38440ed3a748e42c79988
sha256  f4159c24c58f86edb93482dcc7c6c972a7bc683ab264bbc4887e4f557267a325
pulled  2026-10-14
behind  0 revisions; that commit is the tip of the path
```

Block 5a, the child of 4a, in the same format. No test of
btcsuite/btcd@c615bd709 loads it. It spends the coinbase of block 2.

## `tests/unit/chainstate/_data/btcd/277647.dat.bz2`

```text
repo    btcsuite/btcd
path    blockchain/testdata/277647.dat.bz2
commit  b69a849114ea871b49a27bda2ebad36d460f8137  2015-01-30
blob    598420a65ab8b2801d2d917147fb58e53ee6014a
sha256  0e9236eea4eb0e02649598169ff710ec695be59f6296799a6f04ba5364760496
pulled  2026-10-14
behind  0 revisions; that commit is the tip of the path
```

Mainnet block 277647, in the same format, which btcd's `TestCheckBlockScripts`
verifies.

## `tests/unit/chainstate/_data/btcd/277647.utxostore.bz2`

```text
repo    btcsuite/btcd
path    blockchain/testdata/277647.utxostore.bz2
commit  a59ac5b18f028fabbd0f537094c995702a942dd3  2018-05-27
blob    c12b65e2a712c5a712ad4aef2ebdc9538a09e606
sha256  0b107034d984f1d6251ba5a32dc4617207bf502c6def49deb9ace2bbd93a91f0
pulled  2026-10-14
behind  0 revisions; that commit is the tip of the path
```

The outputs block 277647 spends, bzip2-compressed: records of
`<txid><index><length><entry>`, the entry in btcd's compressed UTXO
serialization. `tests/unit/chainstate/mainnet_blocks_test.py` is its only
reader.

## `tests/unit/chainstate/_data/btcd/LICENSE`

```text
repo    btcsuite/btcd
path    LICENSE
commit  75a1d4134a02208b627930ce83d2db0aaa1319ab  2025-01-15
blob    5eed08580dbd2d31013813aefe936db3a3994997
sha256  10db068dbe11e2b3d652a3d4ae3cdd7998b84b12cdefe3d8083a2b63c295c13f
pulled  2026-10-14
behind  0 revisions; that commit is the tip of the path
```

btcd's licence, ISC, which the btcd files above are distributed under and
which travels with them.

## `tests/unit/chainstate/_data/floresta/raw.zst`

```text
repo    getfloresta/Floresta
path    crates/floresta-chain/testdata/invalid_block_783426/raw.zst
commit  d3166e832b3b92e71c80137df3f47d9383b31df4  2026-09-09
blob    27d080b7bb699a608655413f326b1de2f67724cd
sha256  5763d43678698fbebaa723e96dd7c7dd56118dc90a2e2d00656177c66dffbde9
pulled  2026-10-14
behind  0 revisions; that commit is the tip of the path
```

Mainnet block candidate 783426, serialized and zstd-compressed, vendored
byte for byte. F2Pool mined it with valid proof of work and Bitcoin Core
refused it as `bad-blk-sigops`: its sigop cost is 80,003 against a limit of
80,000 (<https://b10c.me/observations/11-invalid-blocks-783426-and-784121/>).
Floresta added it in getfloresta/Floresta#1300.

## `tests/unit/chainstate/_data/floresta/spent_utxos.zst`

```text
repo    getfloresta/Floresta
path    crates/floresta-chain/testdata/invalid_block_783426/spent_utxos.zst
commit  d3166e832b3b92e71c80137df3f47d9383b31df4  2026-09-09
blob    305c936792e15ddfe7427fd8e6fc54a57c9763a9
sha256  2985a5d414a669e6828432e0df3ca89a6bd0beb65aa889f96e49d12d78dddc2b
pulled  2026-10-14
behind  0 revisions; that commit is the tip of the path
```

The outputs that candidate spends, as a zstd-compressed JSON list in the
order the candidate's inputs spend them. It also lists the outputs earlier
transactions of the candidate create and later ones spend.

## `tests/unit/chainstate/_data/floresta/LICENSE-MIT`

```text
repo    getfloresta/Floresta
path    LICENSE-MIT
commit  1e1f22177d0d57e863b922167f76fe2316001a9c  2026-03-16
blob    e7b4e70c90113c6273528025b5b21c58e593e311
sha256  f0ba1af4e8236d88591450d05c54ac4c39436075f0bf8b8967cba53e5de727a2
pulled  2026-10-14
behind  0 revisions; that commit is the tip of the path
```

Floresta's MIT licence, one of the two its `LICENSE.md` offers, which the two
files above are distributed under and which travels with them.
