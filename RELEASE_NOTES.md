# Release notes

<!-- markdownlint-configure-file
  {
    // MD024/no-duplicate-heading - "Breaking changes" is the heading of a
    // subsection under every release that has one, which is what keeps
    // the page readable scrolling down it; only a duplicate under the
    // same release heading would be the accident this rule looks for.
    // btclib's own RELEASE_NOTES.md carries this comment verbatim
    "MD024": { "siblings_only": true }
  }
-->

Notable changes are documented here.
[CHANGELOG.md](./CHANGELOG.md) is the record behind them: this file says
what a user has to act on, that one says what changed and why.

Versions are *[calendar versions](https://calver.org/)*, `YYYY.M.D`;
between releases `pyproject.toml` declares the month alone, which is the
shape `RELEASING.md` gives a cycle in progress. The number says when a
release was cut, and it promises nothing about compatibility, so a
breaking change is announced in this file — read it before upgrading,
rather than a digit.

## Unreleased

The `2026.9` cycle is open and nothing has been cut from it. This
section fills in one landed change at a time — what a user of
`v2026.8.27` would have to act on to move to it — and `RELEASING.md`'s
*Release to PyPI* is what retitles it to the version on release day.

### Breaking changes

- **A data directory written by `v2026.8.27` is refused** (issue #569).
  A node upgrading past this change will not start against its
  existing one, and says so rather than reading it wrongly -- one
  line, wrapped here to fit the page:

  ```text
  btclib_node.exceptions.IncompatibleStoreError: <data directory>
  holds a version 0 store, which this version (1) cannot read:
  delete the directory and sync again
  ```

  Delete the data directory and sync again; nothing outside it has to
  be touched, the blocks, the undo data, the chainstate and the log
  all living under it. There is no migration to run instead, and the
  reason is the change itself: `COINBASE_MATURITY` was enforced
  nowhere because the record could not express it, a stored output
  carrying no height for anything to ask how deep the coinbase that
  created it was. The record and the undo data now carry each coin's
  height and its coinbase bit, and both are on-disk formats a
  directory written by `v2026.8.27` does not hold. Recovering the
  missing height means reading every block again in order, which is
  the sync under another name.

  The store now carries a schema version so that an old directory
  fails on the first read rather than misparsing a record deep into
  one. The released version wrote no such stamp, which is exactly what
  the refusal recognises.

  This is the first change to break an installation of `v2026.8.27`,
  that being the first release there was. `CHANGELOG.md`'s own entry
  has what changed and why; this one is only what a user has to do.

- **`Config(pruned=True)` used to construct and do nothing; it now
  raises `PruningNotImplementedError`** (issue #574). A caller relying
  on the old, silently-ignored value was getting a full node that
  wrote every block to disk regardless of what it asked for --
  `pruned=False`, the default, is unaffected. There is nothing to
  migrate: pass `pruned=False`, or drop the argument, until pruning
  itself is built (issue #601).

- **The rpc listener's default port is now Core's own, not the p2p
  port plus one** (issue #605). `v2026.8.27` listened on 8334, 18334,
  38334 and 18445 for mainnet, testnet, signet and regtest; it now
  listens on Core's 8332, 18332, 38332 and 18443, which is where
  `bitcoin-cli` and anything else written against Core looks. A client
  or a firewall rule pointed at an old default has to move; a caller
  passing `rpc_port=` explicitly is unaffected.

- **A storage fault while connecting a block used to be recorded as
  that block's own rejection, silently, and the node kept running; it
  now stops the node** (issue #620). Where a node exits this way, do
  not restart it before reading the log for the exception and clearing
  whatever the store or disk reported -- restarting against the same
  fault reaches the same exit again.

- **A kill, a crash, or anything else that stops the node without
  going through a clean shutdown can now cost revalidating up to a few
  dozen of the most recently connected blocks the next time it starts**
  (issue #586). A datadir this happens to is not corrupted and needs no
  repair: the node simply offers those blocks to itself again, the same
  way it would a block arriving for the first time, and the store never
  ends up holding a UTXO set, a block status or a filter more advanced
  than the other two. A clean stop -- `SIGINT`, `SIGTERM`, or the `stop`
  RPC -- is unaffected and loses nothing: the store is flushed before it
  closes either way, and this cost is only ever paid by the shutdown
  that skips that step.

- **A corrupted, node-owned `utxo-` record read back while accepting a
  transaction into the mempool used to be answered as that transaction's
  own refusal, and could get the peer that sent it discouraged; it now
  answers as this node's own fault instead** (issue #631). Corrupted
  here means what the store's own checksum catches, raised as
  `StoreCorruptionError` (issue #641): `sendrawtransaction` answers an
  internal-error response rather than `VERIFY_REJECTED`/`"Invalid
  signatures or script"`; `testmempoolaccept` reports the entry
  `"Unknown error"` rather than the same reason; the peer-to-peer path
  no longer discourages a peer for exposing this node's own corrupted
  storage. Read the log for `StoreCorruptionError` rather than trusting
  either answer. A record whose bytes pass that checksum and still
  cannot be parsed is a different case, answered as absent (issue
  #650): the spend is refused as `"Missing prevouts"`, exactly as a
  genuinely missing prevout is, and nothing is logged -- which is what
  Core's own `CCoinsViewDB::GetCoin` answers for the same record.

- **The store is now RocksDB, not `sqlite3`; a data directory written
  by an older release cannot be read** (issues #637, #641). A node
  upgrading past this change refuses its existing directory rather than
  reading it wrongly:

  ```text
  btclib_node.exceptions.IncompatibleStoreError: <data directory>
  holds a sqlite3 database, which this version cannot read: delete
  the directory and sync again
  ```

  Delete the data directory and sync again; there is no migration from
  the old store's own `.sqlite` file into RocksDB's own format. This is
  the same shape of refusal, and the same remedy, as the schema-version
  one above -- it is a second, independent reason the same directory
  can now be refused, not a repeat of it. What is gained for the cost:
  a bit flipped on disk is now caught at the read that meets it, the
  same per-block checksum Bitcoin Core's own LevelDB store already
  carries, where the old store answered a corrupted value silently and
  could lose a corrupted key without ever reading it at all.

- **`scripts/chains/mainnet.py`, `testnet.py` and `signet.py` are gone**
  (issues #583, #581, #573). `pip install btclib-node` now puts
  `btclib-node` on `PATH`; run `btclib-node` in place of
  `python scripts/chains/mainnet.py`, `btclib-node -testnet`/`-signet`/
  `-regtest` in place of the other two, and `btclib-node -h` for every
  flag. `btclib-node -conf=<file>` reads an existing `bitcoin.conf` the
  way Core reads one.

## v2026.8.27

**The first release of btclib-node.** Nothing here is an upgrade: no
version of this package has ever been on an index, so there is no
installation for this one to change the behaviour of and nothing to
read these notes against. `pip install btclib-node` reaches a released
btclib-node for the first time with this version.

What it is, and what it is not, is `README.md`'s: a bitcoin node whose
consensus and network code is python, over
[btclib](https://github.com/btclib-org/btclib). It has downloaded and
validated the whole chain. Its `Development Status` classifier says
`3 - Alpha` and means it — the interfaces are not promised stable, and
this file is where a break in them is announced from the next release
on.

`CHANGELOG.md`'s own `v2026.8.27` section is the record of everything
that went into it, which for a first release is the whole history
rather than a cycle's worth.

### Two things to know before installing

- **The JSON-RPC listener binds every interface and authenticates
  nothing.** [SECURITY.md](./SECURITY.md) carries that and the rest of
  what is known. Do not expose it.
- **`Node.__init__` does not install signal handlers** (#436). If a
  `Node` you build is meant to stop on an operator's `SIGINT`,
  `SIGTERM` or `SIGTSTP`, call `install_signal_handlers(node)` for it,
  the way `scripts/chains/` does right after building the node each of
  them starts. This is listed here rather than under a *Breaking
  changes* heading on purpose: nothing published can have broken,
  there having been nothing published, and the change is a break only
  against the unreleased tree — anyone who was running this from git
  before #467 landed is the only reader it can surprise.
