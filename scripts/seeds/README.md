# Bitcoin Core's fixed seeds

`generate_seeds.py` writes `src/btclib_node/_chainparamsseeds.py` from the
lists below, which are Bitcoin Core's, and `BITCOIN_CORE_COPYING` is the
licence they are distributed under, which the generator writes into the
module's opening comment in full. Each is pinned in the shape
`tests/_data/README.md` gives, and each is identical to its blob.

The pins are to a release tag and not to Core's tip: each carries the
tag as `ref`, so `.github/workflows/vendored-vectors.yml`'s
`check_vendored_pin.py` compares it against that tag's own tree rather
than upstream's default branch, where these lists move ahead of a
release. Refreshing them is copying Core's lists at a newer release
here, running the command in `generate_seeds.py`'s docstring, and
updating the pins -- `ref` among them.

```shell
git hash-object scripts/seeds/nodes_main.txt
git -C <bitcoin checkout> rev-parse <ref>:contrib/seeds/nodes_main.txt
```

## `scripts/seeds/nodes_main.txt`

```text
repo    bitcoin/bitcoin
path    contrib/seeds/nodes_main.txt
ref     v32.1
commit  fcfc1df041b8cecbc2bb4bf6836f5ba643f298a5  2026-09-08
blob    eefc9bf7d37287a39d57e153398881274c0adae0
pulled  2026-11-07
behind  0 revisions; that commit is the tip of the path
```

## `scripts/seeds/nodes_signet.txt`

```text
repo    bitcoin/bitcoin
path    contrib/seeds/nodes_signet.txt
ref     v32.1
commit  fcfc1df041b8cecbc2bb4bf6836f5ba643f298a5  2026-09-08
blob    3506ee691d3186e1a1376e63f1919433b0d4f485
pulled  2026-11-07
behind  0 revisions; that commit is the tip of the path
```

## `scripts/seeds/nodes_test.txt`

```text
repo    bitcoin/bitcoin
path    contrib/seeds/nodes_test.txt
ref     v32.1
commit  fcfc1df041b8cecbc2bb4bf6836f5ba643f298a5  2026-09-08
blob    421f11a4655ebea15d553d4e30f21b9dc0c0716c
pulled  2026-11-07
behind  0 revisions; that commit is the tip of the path
```

## `scripts/seeds/nodes_testnet4.txt`

```text
repo    bitcoin/bitcoin
path    contrib/seeds/nodes_testnet4.txt
ref     v32.1
commit  fcfc1df041b8cecbc2bb4bf6836f5ba643f298a5  2026-09-08
blob    069255f13e8ec8002bb6769351b52b67cf60f803
pulled  2026-11-07
behind  0 revisions; that commit is the tip of the path
```

## `scripts/seeds/BITCOIN_CORE_COPYING`

```text
repo    bitcoin/bitcoin
path    COPYING
ref     v32.1
commit  b23b901363c56043c536f32261ac8cb540624a84  2025-12-29
blob    89960cbf2f221a29852ed162b25bda2afc0b2dd6
pulled  2026-11-07
behind  0 revisions; that commit is the tip of the path
```
