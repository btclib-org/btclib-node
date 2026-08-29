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

## `tests/unit/chainstate/_data/chacha20_vectors.json` and `muhash_vectors.json`

Not vendored — derived, both from one file: `src/test/crypto_tests.cpp`
(bitcoin/bitcoin@ca7162cde5, blob
`b348793bfb6397ebde806961b6783b1540a33804`), a Boost.Test `.cpp` file
rather than a `src/test/data/*.json` Core publishes on its own, so there
is no upstream blob shaped like either JSON file here to pin against
directly.

`chacha20_vectors.json` is every `TestChaCha20(...)` call inside
`BOOST_AUTO_TEST_CASE(chacha20_testvector)` (21, RFC 7539/8439's own
Appendix A.1/A.2/A.4 vectors among them, cited in that test case's own
comments) parsed out of the call's five arguments -- `message`, `key`,
`nonce_first`/`nonce_second` (`ChaCha20::Nonce96`), `seek` (the block
counter `Seek` starts from) and `keystream_or_ciphertext`, the last
being ciphertext when `message` is non-empty and raw keystream when it
is empty, matching `TestChaCha20`'s own two modes. Every top-level comma
in each call was split outside string literals and parentheses, and
every adjacent C++ string literal concatenated, by a small script run
once against the pinned commit rather than committed here -- reproduced
by parsing the same five-argument calls out of that test case again at
the same sha and diffing the result against this file, byte for byte.

`muhash_vectors.json` is `muhash_tests`' own three numeric checks: the
`FromInt(0)*FromInt(1)/FromInt(2)` cancellation (`insert`/`remove`,
`digest_uint256_hex`, a `uint256{"..."}` literal -- reversed relative to
the raw digest, `chainstate/muhash.py`'s own docstring is where that
convention is read off `uint256.h`'s own comment), the serialization
vector (`ser_exp`) and the overflow vector (`ss_max`'s `DataStream`
input, and `out4`'s digest read through `HexStr` directly rather than
`GetHex()` -- **not** reversed, the one place in this file the two
conventions differ, confirmed against `crypto_tests.cpp`'s own two
different assertion macros rather than assumed uniform). `FromInt(i)` is
expanded here to the full 32-byte element (`i` then 31 zero bytes) each
vector inserts or removes, rather than left as the bare integer
`crypto_tests.cpp` passes to its own local helper, since this file has
no such helper to call.

Both are read by `tests/unit/chainstate/muhash_test.py`.

Re-checked by re-deriving both files against the pinned commit and
diffing byte for byte -- there is no single upstream blob either
matches to compare a hash against instead:

```shell
git -C <bitcoin checkout> show ca7162cde5:src/test/crypto_tests.cpp \
  | git hash-object --stdin
```

answers `b348793bfb6397ebde806961b6783b1540a33804` if the source file
this derivation reads has not moved since.
