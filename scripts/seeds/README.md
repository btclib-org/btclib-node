# Bitcoin Core's fixed seeds

`generate_seeds.py` writes `src/btclib_node/_chainparamsseeds.py` from the
lists below, which are Bitcoin Core's, and `BITCOIN_CORE_COPYING` is the
licence they are distributed under, which the generator writes into the
module's opening comment in full. Each is pinned in the shape
`tests/_data/README.md` gives, and each is identical to its blob.

The pins are to the v31.1 tag, `9be056a8a7`, and not to Core's tip:
`.github/workflows/vendored-vectors.yml` compares a pin against upstream's
default branch, where these lists already move ahead of a release, so it
reads `tests/_data/README.md` alone (btclib-org/btclib-node#1227).
Refreshing them is copying Core's lists at a newer release here, running the
command in `generate_seeds.py`'s docstring, and updating the pins.

```shell
git hash-object scripts/seeds/nodes_main.txt
git -C <bitcoin checkout> rev-parse 9be056a8a7:contrib/seeds/nodes_main.txt
```

## `scripts/seeds/nodes_main.txt`

```text
repo    bitcoin/bitcoin
path    contrib/seeds/nodes_main.txt
commit  fec58229fa671cb870ebf795b54b73b7e22a1eb6  2026-02-25
blob    a63b00683b922b2315aff191497fb7b4cfbd8e0c
pulled  2026-09-25
behind  1 revision on master; the newest at the v31.1 tag
```

## `scripts/seeds/nodes_signet.txt`

```text
repo    bitcoin/bitcoin
path    contrib/seeds/nodes_signet.txt
commit  fec58229fa671cb870ebf795b54b73b7e22a1eb6  2026-02-25
blob    399e5942f017050c177c206217b8e43b4fc0ed2e
pulled  2026-09-25
behind  1 revision on master; the newest at the v31.1 tag
```

## `scripts/seeds/nodes_test.txt`

```text
repo    bitcoin/bitcoin
path    contrib/seeds/nodes_test.txt
commit  fec58229fa671cb870ebf795b54b73b7e22a1eb6  2026-02-25
blob    26fd9c6b74bcbabf0283a43b2528e815da27330b
pulled  2026-09-25
behind  1 revision on master; the newest at the v31.1 tag
```

## `scripts/seeds/BITCOIN_CORE_COPYING`

```text
repo    bitcoin/bitcoin
path    COPYING
commit  b23b901363c56043c536f32261ac8cb540624a84  2025-12-29
blob    89960cbf2f221a29852ed162b25bda2afc0b2dd6
pulled  2026-09-26
behind  0 revisions; that commit is the tip of the path
```
