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

## v2026.10.30

Everything below is measured against `v2026.10.29`. `CHANGELOG.md`'s own
`v2026.10.30` section is the record of what went into it.

The floors are unchanged. The suite passes with btclib 2026.10.28 and ecc
2026.10.28, which `uv.lock` pins, and with btclib 2026.10.30, ecc 2026.10.30,
btclib-wallet 2026.10.29 and bitcoin-core-rpc 2026.10.23, which a fresh install
resolves.

### Breaking changes

- **`main.passes_check_block`, `main.is_block_failed` and
  `main.is_cached_invalid` take the chain as their argument after the block**
  (closes #2079). Pass the node's `Chain`, as `node.chain`.
- **On signet, a block failing its solution leaves no header** (closes #2079),
  from `submitblock` and from a peer, as Core's `ProcessNewBlock` does.
  2026.10.29 answered `submitblock` the same, `bad-signet-blksig`, and indexed
  the header as `valid_header`.
- **`submitblock` answers `high-hash` for a block whose header misses its proof
  of work and whose body fails `CheckBlock`**, such as a second coinbase (issue
  #2079). 2026.10.29 answered btclib's `invalid proof-of-work` text there.

### Worth knowing, though nothing raises

- **A `ping` longer than eight octets is answered** (closes #2086), with a
  `pong` carrying its first eight, as Core does. 2026.10.29 refused to parse it
  and sent nothing.
- **An address in 3fff::/20 is invalid** (closes #1756), as in Core v32.1: the
  address table drops it, and `setban` answers `Error: Invalid IP/Subnet`.
  2026.10.29 accepted it.
- **The log writes each line of a traceback or a stack after its record,
  starting with two spaces, its control characters escaped** (closes #2092). A
  reader that parses `history.log` takes such a line as part of the record
  above it. 2026.10.29 wrote a traceback as it was.

## v2026.10.29

Everything below is measured against `v2026.10.25`. `CHANGELOG.md`'s own
`v2026.10.29` section is the record of what went into it.

The suite passes with btclib 2026.10.28, ecc 2026.10.28 and btclib-wallet
2026.10.29, which a fresh install resolves.

### Breaking changes

- **The floors are `btclib>=2026.10.28` and `ecc>=2026.10.28`**, the
  maintainer's decision for this release. An earlier btclib refuses a block
  of version 0 or below under the BIP34, BIP66 and BIP65 heights, which Core
  accepts (issue btclib-org/btclib#2833), and ecc 2026.10.28 is the release
  btclib 2026.10.28 requires. Such a block, from `submitblock` or from a
  peer, is no longer refused for its version. 2026.10.25, with its locked
  btclib 2026.10.24, refused it once it was to join the active chain, and
  marked its header invalid. Extending the tip, `submitblock` answered
  `invalid version (outdated): 0x00000000 < 1` for version 0, and a peer's
  block was stored and then refused when connected. Off the tip, the block
  was stored and its header left `valid_header`, as it is in this release,
  until its branch became the best. On regtest, signet and testnet4 the
  three heights are 1, and the node takes no `-testactivationheight`, so
  there this concerns height 0 only; on mainnet and testnet3 it concerns the
  history below them.
- **`btclib_node.rpc.solver` is gone** (closes #1998, closes #2055). Import
  `solver` from `btclib.script.solver`, which the node calls itself.
- **`btclib_node.p2p.headers_sync` is gone, and `HeadersSyncParams` leaves
  `btclib_node.chains`' `__all__`** (issue btclib-org/btclib#2706). The node
  runs btclib's `btclib.p2p.headers_sync`, with the pairs of
  `btclib.consensus.HEADERS_SYNC_PARAMS`. Import `HeadersSyncParams` from
  `btclib.consensus`.
- **`Chain` takes no `rpc_port`, `genesis_block` or `headers_sync_params`**
  (closes #2074, issue btclib-org/btclib#2706, issue
  btclib-org/btclib#2707). Each is a property:
  `genesis_block` is btclib's `genesis_block` for the chain's name,
  `rpc_port` bitcoin-core-rpc's `rpc_port_from_chain`, and
  `headers_sync_params` the chain's row of btclib's table. `Main`,
  `TestNet`, `TestNet4`, `SigNet` and `RegTest` answer the same values as in
  2026.10.25. `addresses`, `fixed_seeds`, `prune_after_height` and
  `min_bip9_warning_height` move forward among `Chain`'s positional
  parameters. `create_genesis` is no longer importable from
  `btclib_node.chains`.
- **`mining.witness_reason` is gone** (issue btclib-org/btclib#2705). Call
  `mining.contextual_block_reason` instead: it answers why a block fails
  Core's `ContextualCheckBlock` at a height.
- **A block that fails bad-txns-nonfinal or bad-cb-height marks its header
  invalid and is not stored, from `submitblock` and from a peer**
  (closes #2075, closes #2053), as Core's `AcceptBlock` does. This holds
  where no earlier check refuses the block first, such as a header rule,
  time-too-new for a header more than two hours ahead of the clock among
  them, or, from a peer, the check for a mutated body. Off the active chain,
  2026.10.25's `submitblock` accepted such a block and answered nothing. A
  mutated body given to `submitblock` is answered with the rule, where
  2026.10.25 answered with the witness rule, such as
  `bad-witness-nonce-size`. A peer that sends such a block is judged
  misbehaving, unless it came as a compact block.
- **`submitblock` answers a body over the weight limit with `bad-blk-weight`
  and marks its header invalid**, where 2026.10.25 answered btclib's text,
  such as `invalid weight: 4001500 > 4000000`. This holds where no earlier
  check refuses the body first. Where bad-txns-nonfinal or bad-cb-height is
  the first refusal, it marks the header too.
- **A node whose store holds a header that misses its proof of work refuses
  to start** (closes #1997), with `Error loading block database`, and logs a
  line starting `CheckProofOfWork failed: block <hash> at height <height>`.
  A stored header is indexed under its own hash, not under the hash in its
  key.
- **A stored block is checked where Core's `ReadBlock` checks it**
  (closes #2002, closes #2038). A record that hashes to another block,
  misses its proof of work or, on signet, fails its solution is refused.
  `getrawtransaction` with that blockhash answers `No such transaction found
  in the provided block. Use gettransaction for wallet transactions.` A
  peer's `getblocktxn` for it, within `MAX_BLOCKTXN_DEPTH` of the tip, stops
  the node, and so do connecting the block and a reorg that removes it.
  `invalidateblock` and `dumptxoutset`'s rollback over it stop the node and
  leave the chain as it was, where Core's `InvalidateBlock` answers and runs
  on. The filter index's catch-up over it ends the node's start. `getblock`
  answers the record as stored, as Core's does. Over a record holding
  another block, 2026.10.25 answered `getrawtransaction`, connected,
  reorged, invalidated and caught up without a refusal.
- **`generatetoaddress` and `generateblock` refuse a signet block whose
  solution fails** (closes #2080), answering `ProcessNewBlock, block not
  accepted`; the block is neither indexed nor stored. 2026.10.25 stored it
  and made it the tip.

### Worth knowing, though nothing raises

- **The log escapes control characters in a message** (closes #1952).
  Every C0 control, DEL and C1 control in the message is written as `\x`
  and its hex code, so a newline becomes `\x0a` and ESC `\x1b`, after one
  trailing newline is stripped. A traceback is written as it is. 2026.10.25
  wrote both as they were. Escaping C1 departs from Core, and `SECURITY.md`
  says so.
- **A clock more than two hours behind the chain start's median time past
  starts no low-work headers sync** (issue btclib-org/btclib#2706), where
  2026.10.25 started one. The node keeps the peer.

## v2026.10.25

Everything below is measured against `v2026.10.18`. `CHANGELOG.md`'s own
`v2026.10.25` section is the record of what went into it.

**Upgrade from 2026.10.18 at once.** It declares `btclib>=2026.10.17` with
no ceiling, so a fresh install resolves btclib 2026.10.24, which renamed or
changed what 2026.10.18 calls. Measured on such an install, `BlockContext`
raises `BTClibTypeError: invalid consensus type: int`, which
`contextual_check_block` meets for every block that passes its finality
check, and `CoinStats.digest` is gone, which `gettxoutsetinfo` reads for
`muhash`.
This release is written against btclib 2026.10.24.

### Breaking changes

- **The floors are `btclib>=2026.10.24`, `ecc>=2026.10.23` and
  `btclib-wallet>=2026.10.25`**, and `ecc` replaces `btclib-ecc` as the
  runtime dependency (closes #2008, closes #2032). Import `ecc` where your
  own code imported `btclib_ecc`. `btclib_node.chainstate.muhash.CoinStats`
  inherits btclib's `digest`, now `digest_var`, so a caller of
  `coin_stats.digest` breaks.
- **The block filters live in `indexes/blockfilter`, a store of their own**
  (closes #1940). The first start copies an older data directory's filters
  there and removes them from the chainstate's store, one way.
  `-reindex-chainstate` keeps them. Deleting `indexes/blockfilter` rebuilds
  them from the stored blocks, which a pruned node cannot do, and it then
  refuses to start.
- **`getcfilters`, `getcfheaders` and `getcfcheckpt` send nothing and keep
  the peer for a range with a missing filter or header**, and no longer
  raise `ChainstateInconsistencyError` (closes #1940).
- **`FilterIndex` changes its API** (closes #1940). `FilterIndex.wipe` is
  gone. `FilterIndex.catch_up(active_chain, block_db)` is
  `catch_up(block_index, block_db)`. `FilterIndex.finalize(wb=None)` is
  `finalize(best_block=None)` and writes no caller's batch.
  `FilterIndex.__init__` takes the filter store as `db` where it took the
  chainstate's as `parent_db`, so `FilterIndex.db` is the filter store.
  `Chainstate.filter_index` is built on `Chainstate.filter_db`, and
  `Chainstate.db` no longer holds filters.
- **`BlockDB`'s `wipe_undo` argument is gone**, `BlockDB.wipe_undo()` taking
  its place (closes #1916).
- **A node whose block store records pruning refuses to start without
  `-prune`**, with Core's message (closes #1916). The refusal comes before
  `-reindex-chainstate` wipes anything.
- **`submitblock` answers Core's words for a mutated body** (#2041). A
  coinbase that commits to a witness and carries none gets its 32-byte nonce
  first, as Core's `UpdateUncommittedBlockStructures` does. A body that
  passes `CheckBlock` and is still mutated is answered with Core's word for
  its witness, is not stored, and leaves its header unmarked, so the same
  block, whole, is accepted afterwards. Any other refusal keeps btclib's
  text. `mining.witness_reason`, once private, is public.

### Worth knowing, though nothing raises

- **A `getdata` for `MSG_BLOCK` or `MSG_CMPCT_BLOCK` is answered only with
  the block of that hash** (closes #1990). A stored record that holds
  another block, misses its proof of work, or on signet fails its solution,
  is refused: the node logs "Cannot load block from disk" and drops the
  peer, as Core does.
- **`getblocktxn` keeps the blocks it reads in `Node.blocktxn_blocks`**
  (closes #1954), those at most `MAX_BLOCKTXN_DEPTH` below the tip, so a
  peer that repeats the request for one of them no longer makes the node
  parse it each time. The cache holds at most `MAX_BLOCKTXN_DEPTH + 1`
  blocks, about 280 MB at the largest parsed size measured on synthetic
  blocks. A block above the tip is still read on every request, and so is
  each block of a set larger than the cache that a peer asks for in turn.

## v2026.10.18

Everything below is measured against `v2026.10.8`. `CHANGELOG.md`'s own
`v2026.10.18` section is the record of everything that went into it.

### Breaking changes

- **A client from a source `-rpcallowip` refuses is closed as it connects,
  with no reply**, where it got `403 Forbidden` (closes #1963).
  `RpcManager.client_allowed` is gone.
- **The RPC server holds at most `-rpcmaxconnections` clients, 16 by
  default**, as Core master does (closes #1953). A client past the limit
  waits until one leaves. Raise the option if you hold more connections
  open.
- **`getrawmempool true` answers each entry as `getmempoolentry` does, and
  the `size` field is gone** (closes #1764). Read `vsize` or `weight`.
- **`getpeerinfo`'s `bytessent_per_msg` has no `""` entry**: the v2
  handshake's octets count in `bytessent` alone, as in Core (closes #1880).
- **The data, blocks, chainstate and index directories are created at most
  0700, and `.lock` at most 0600, whatever the caller's umask**
  (closes #1887). A directory that exists keeps its mode. On Windows the
  directories are created 0777.
- **A refused configuration line or argument, an unknown key's warning and
  the configuration log quote at most a 16-character name** (closes #1908).
  A longer name is given by its length, and an unknown key's warning starts
  with `file:line`. Match on the new text if you parse these messages.
- **`Node.stop` waits as long as the stores take to close**, and raises
  `NodeShutdownTimeoutError` only where what runs before the closes overruns
  `STOP_TIMEOUT` (closes #1870). A close that never returns holds `stop`.
- **`KNOWN_TX_INVENTORY_CAPACITY` is gone**, and `KnownTxInventory` is a
  rolling bloom filter, as Core's (closes #1743). Like Core's, it withholds
  a transaction from a peer about once in a million queries.
- **The reprs of `RequestHead`, `RpcAuthEntry` and `_Settings` leave out
  what may hold a secret** (closes #1957, closes #1965): the `Authorization`
  line, the salt and HMAC, and the sources read.
- **The tables of `rpc.callbacks`, `callbacks`, `arg_names` and
  `named_only`, and of `rpc.help`, `HELP_TEXT` and `CATEGORY`, hold the
  methods this release serves.**

  `Connection`'s `last_receive`, `last_send`, `connected_time` and
  `addr_token_timestamp` start from `clock.now()`, which `setmocktime`
  holds on regtest.

### New

- **`savemempool` and `importmempool` are served**, as in Core v31.1
  (closes #1746).
- **`signmessagewithprivkey`, `verifymessage`, `signrawtransactionwithkey`
  and `combinerawtransaction` are served** (closes #1538, closes #1400).
- **`setmocktime` holds the node's clock on regtest**, a hidden command as
  in Core (closes #1479).
- **`getnetworkinfo` answers `networkactive`, `connections`,
  `connections_in` and `connections_out`** (closes #1984, closes #1973).
- **`-reindex-chainstate` rebuilds the chainstate from the stored blocks**
  (issue #1415). It is refused beside `-prune`. While it runs, the node takes
  no headers or blocks from peers (closes #1915).
- **Held transactions expire after `-mempoolexpiry=<n>` hours, 336 by
  default**, as in Core (closes #1815). A load of `mempool.dat` expires
  them too.

### Worth knowing, though nothing raises

- **A transaction that conflicts with held ones replaces them where
  bitcoind v31.1 would**, whatever it signals (closes #1494). `submitpackage`
  replaces for a parent and its child, and lists `replaced-transactions`.
- **On signet, a block whose commitment holds an empty push is checked as
  Core checks it** (closes #1896). Such a block Core accepts was refused.
- **The node relays the addresses peers gossip**, as Core's `RelayAddress`
  does (closes #1867).
- **A `getdata` for a filtered block drops the peer**, as Core master does
  (closes #1949). An unasked `cmpctblock` is ignored, as in Core master
  (closes #1890, closes #1897).
- **A SIGTERM stops the node while its main thread waits on it**
  (closes #1274).
- **The dependency floors are `btclib>=2026.10.17` and
  `btclib-wallet>=2026.10.16`** (closes #2003). btclib-wallet 2026.10.9
  fails at import beside btclib 2026.10.17. `btclib-ecc>=2026.10.2` is a
  runtime dependency, which `signing.py` and `p2p/v2transport.py` import
  (#1742).

## v2026.10.8

**The fifth release.** Everything below is measured against `v2026.10.4`.
`CHANGELOG.md`'s own `v2026.10.8` section is the record of everything
that went into it.

### Security

- **A refusal quoted a configuration line or a command-line argument whole,
  and an RPC password in it reached stderr**
  ([GHSA-74pj-5cgg-3m66](https://github.com/btclib-org/btclib-node/security/advisories/GHSA-74pj-5cgg-3m66)).
  From 2026.9.4 to 2026.10.4, a configuration line that fails to parse, such
  as `rpcpassword <pw>` or `main.rpcpassword <pw>`, and a refused argument,
  such as `-main.rpcpassword=<pw>`, were quoted whole. A configuration line
  starting with `-`, such as `-rpcpassword=<pw>`, was quoted in 2026.10.4
  alone. Upgrade, and treat an RPC password that such a line or argument may
  have printed as exposed: a service manager can keep stderr in its journal.
- **Such a line or argument is quoted only up to the first `rpcauth`,
  `rpcpassword` or `rpcuser` in it**, which departs from Core, as
  `SECURITY.md` says. A misspelled name, as in `-rpcpasword=<pw>`, is still
  quoted whole. `scantxoutset`, not yet in a release, does not quote the
  descriptor in "Cannot derive script without private keys".

### Breaking changes

- **An RPC argument Core refuses is refused** (closes #1655, closes #1664,
  closes #1665). A hash with whitespace in it, an amount with a space, a
  `+`, an underscore, a leading zero, `1.` or `.5`, and a request body
  with a lone surrogate escape are errors, as they are in Core. Check the
  values you pass.
- **`mining.solve_block`, `rpc.mining.generate_to_address` and
  `generate_block` return generators, and `rpc.mining.get_block_template`
  does for a `longpollid`** (closes #1622) (closes #1606). A program that
  calls them directly drives the generator, as `rpc.main` does. A JSON-RPC
  client needs no change: `getblocktemplate` waits on a `longpollid` and
  `waitforblockheight` is served, both refused before.
- **A node given no `-bind` also listens on `127.0.0.1:<port + 1>`, a target
  for a Tor onion service, and stops where that port is taken** (closes
  #1666). Free the port or move `-port`. A `-bind` turns the default off, and
  the node then listens on the addresses it names alone.
- **A bare `gettxoutsetinfo` answers `hash_serialized_3`, not `muhash`**
  (closes #1598). Pass `"muhash"` to get the old answer. The call scans every
  stored coin, as Core's does, so it takes longer the larger the set.
- **A JSON number no double holds, and `NaN`, `Infinity` and `-Infinity`,
  are refused** (closes #1826, closes #1830). `1e400`, or a whole number as
  large, is answered "JSON double out of range" where a method reads a
  double, and the three names are a parse error, `-32700`, as in Core.
- **A full send buffer pauses a peer and never drops it** (closes #1775,
  closes #1789, closes #1805, closes #1812). `MAX_QUEUED_SEND_BYTES`,
  `MAX_GETDATA_INFLIGHT_BYTES`, `MAX_CFILTERS_INFLIGHT_BYTES`,
  `MAX_PENDING_GETDATA_ITEMS`, `MAX_PENDING_CFILTER_HASHES`,
  `Connection.queued_send_bytes` and the module
  `btclib_node.p2p.filter_size` are gone, and `MAX_QUEUED_RECV_BYTES` is
  `Connection.recv_flood_size`, from `-maxreceivebuffer`.
- **`config.listen_port` takes `whitebind` after `bind`,
  `Mempool.cluster` takes `max_weight` in place of `max_vsize`, and
  `Connection.tx_announce_queue` is a `dict` used as an ordered set, not a
  list** (closes #1625, closes #1499, closes #1810), for a program that
  uses them. The tables `callbacks` of `p2p` and of `rpc`, `arg_names`,
  `HELP_TEXT` and `CATEGORY` gained the messages and methods now served.

### Worth knowing, though nothing raises

- **At most 16 requests that wait or search run at once, and 64 more
  queue**, as in Core's defaults for
  `-rpcthreads` and `-rpcworkqueue`, which this node does not read. A
  request past that gets `503` "Work queue depth exceeded", so a client
  holding many `waitforblockheight` or long-poll calls open has to keep
  them under 80.
- **A request sent while the node starts is answered `-28`**, as in Core
  (closes #1317). A client that waits for the node by sending a request
  retries on `-28`, as it does for `bitcoind`.
- **`-assumevalid=<hash>` skips the script checks of the blocks under that
  block, as in Core** (issue #1576). Nothing is assumed valid by default
  yet: the node verifies every script unless you pass the option. Pass
  only a hash you have verified, since a block under it with an invalid
  script can be accepted.
- **A peer is pinged every two minutes, and dropped when its `pong` is
  twenty minutes overdue or when nothing is received from it, or sent whole
  to it, for twenty**, as in Core v31.1 (closes #1768).
- **`-maxsendbuffer` and `-maxreceivebuffer` are read**, in thousands of
  bytes, as in Core, with defaults of 1000 and 5000 (closes #1812).
- **`estimatesmartfee` and `estimaterawfee` are served from `fee_estimates.dat`
  in Core's format**, read at start, written every hour and at shutdown
  (closes #1543). A file over 60 hours old is not read unless
  `-acceptstalefeeestimates` is set, which is refused off regtest.
- **The mempool is kept across a restart**, in `mempool.dat` in Core's
  format: written at shutdown, loaded at start, and turned off by
  `-persistmempool=0` (issue #1746). `getmempoolinfo`'s `loaded` says
  whether the load has ended.
- **A reorg trims the mempool, and a full one evicts the worst chunk**, as
  bitcoind v31.1 does (closes #1823, closes #1740). A package admitted
  through its child is no longer the first evicted, and the members a trim
  leaves are kept.
- **A short-id collision in a `cmpctblock` is answered by asking for the
  block**, as in Core (closes #1859).
- **The dependency floors are `btclib>=2026.10.8` and
  `btclib-wallet>=2026.10.8`.** The first is the release with
  `btclib.compressor`, which `dumptxoutset` reads, `btclib.obfuscation`,
  which the mempool file reads, and the `ShortIdCollisionError` that a
  collision raises (btclib-org/btclib#2570). The second fixes
  [GHSA-62jf-v327-86h5](https://github.com/btclib-org/btclib-wallet/security/advisories/GHSA-62jf-v327-86h5),
  a private key in a refusal that `scantxoutset` answers with.

## v2026.10.4

**The fourth release.** Everything below is measured against `v2026.9.24`:
what changed since, and what it costs to move past it.
`CHANGELOG.md`'s own `v2026.10.4` section is the record of everything
that went into it.

The RPC surface and the options grew too: `getmempoolentry`, `getchaintips`,
`gettxout`, `help`, `invalidateblock`, `generatetoaddress` and
`setnetworkactive` are among the methods now served, and `-bind`,
`-externalip`, `-whitelist`, `-testnet4` and the `-*notify` options are read.
The data directory gains `banlist.json` and `anchors.dat`. This is additive
and costs nothing to move past.

### Breaking changes

- **Every JSON-RPC call has to carry a credential the node accepts**
  (closes #1055). A client sending none, or a placeholder the node used
  to ignore, is answered `401 Unauthorized`. Point it at the cookie the
  node writes at start, `.cookie` in the chain's own data directory
  (`<datadir>/regtest/.cookie` under `-regtest`), the way a client of
  `bitcoind` reads its cookie, or start the node with an `-rpcauth`
  line and give the client that user's password.
- **A node whose JSON-RPC listener cannot start stops, as `bitcoind`
  does** (closes #1076). With the RPC port taken or `.cookie` unwritable,
  `btclib-node` prints `Error: Unable to start HTTP server. See debug log
  for details.` and exits 1. A caller that starts a `Node` itself finds
  its thread ended and the message in `Node.init_errors`. Free the port
  or pick another with `-rpcport`, and make the chain's data directory writable
  where the cookie could not be written.
- **`rpcpassword=` in `bitcoin.conf` is read, and stops the cookie**
  (closes #1070). The node used to warn about the key and write
  `.cookie` all the same; a client reading `.cookie` beside a
  `rpcpassword=` line now has to send that user and password instead. A
  `#` anywhere on the line refuses to start, as in Core.
- **Any `rpcwhitelist=` refuses every method to a user it does not name**
  (closes #1070), as in Core: give that user a whitelist of its own, or
  set `rpcwhitelistdefault=0`.
- **A boolean in `bitcoin.conf`, or after `-listen=`, is read as Core
  reads it** (closes #1117): `true`, `yes` and `on` are false, as they are
  to `bitcoind`, so `testnet=true` selects mainnet and `listen=yes` does
  not listen. Write `1` for true and `0` for false. `norpccookiefile=true`
  and `-norpccookiefile=0` are double negatives, read as
  `rpccookiefile=1`: the cookie is written to a file named `1` in the
  chain's data directory, not to `.cookie`, as `bitcoind` writes it. Drop
  the line to keep `.cookie` where a client reads it, or write
  `norpccookiefile=1` for no cookie.
- **A command-line argument the node refuses exits 1, not 2**
  (closes #1116), after one `Error: ...` line on stderr, as `bitcoind`
  does: a script testing the exit status for 2 has to test for 1.
- **A node whose P2P listener cannot bind stops, as `bitcoind` does**
  (closes #1093). With the P2P port taken, `btclib-node` prints why the
  bind failed (closes #1135), then
  `Error: Failed to listen on any port. Use -listen=0 if you want this.`,
  and exits 1. A caller that starts a `Node` itself finds its thread
  ended and both messages in `Node.init_errors`. Free the port, pick
  another with `-port`, or start with `-listen=0`.
- **An option's value follows `=`, never the next argument**
  (closes #1136): `-datadir <dir>` is refused as `bitcoind` refuses it,
  with "Command line contains unexpected token"; write `-datadir=<dir>`.
- **`bitcoin.conf` is read as Core reads it** (closes #1137). Within one
  section the first value of an option taking one value wins, so keep one
  line per option or put the one you mean first. A `no`-prefixed key negates
  (`nolisten=1` does not listen), `prune=` is read, and `listen=` and
  `debug=` apply from a chain's own section too.
- **`-debug=<category>` takes Core's logging category names**
  (closes #1123): `debug=false`, or any name Core does not know, refuses
  to start with `Unsupported logging category`, and `debug=none` is off.
- **`-conf` naming another file while the data directory holds a
  `bitcoin.conf` refuses to start, as `bitcoind` does** (closes #1155).
  Move what that `bitcoin.conf` says into the file `-conf` names and
  delete or rename it, or start with `-allowignoredconf` to read the file
  `-conf` names with a warning in the log.
- **A `Node` opens its stores when it starts, not when it is built**
  (closes #1279). `Node(config)` no longer has `chainstate`, `block_db`,
  `p2p_manager` or `mempool`: they are there once `start()` returns,
  unless `init_errors` says start-up failed, or once `load()` has run for
  a node driven without its thread. A caller
  reading them off a node it never started calls `node.load()` first.
- **`-port`, `-rpcport`, `-rpcbind`, `-connect` or `-addnode` set only in
  `bitcoin.conf`'s default section, off `main`, refuses to start, as
  `bitcoind` refuses it** (closes #1327). The node used to start anyway
  with the option dropped; it now prints which option and which chain,
  `Error: Config setting for -<option> only applied on <chain> network
  when in [<chain>] section.`, and exits 1. Move the line into that
  chain's own section, e.g. `[regtest]`, or set it on the command line.
- **`version` signals `NODE_COMPACT_FILTERS`, and BIP157 requests are
  answered, only under `-peerblockfilters`** (closes #1395), off by
  default as in Core. A client relying on this node's own BIP157 filter
  service starts it with `-peerblockfilters=1`.
- **`btclib-node` now depends on `btclib-wallet` directly** (closes
  #1508): btclib 2026.9.29 moved `descriptors` out to it
  (btclib-org/btclib#2129), so an unlocked install resolving that
  release or later needs `btclib-wallet` alongside it, which
  `pip install btclib-node` now pulls in on its own.
- **Headers a peer serves on a chain with less work than Core's anti-DoS
  threshold are no longer stored** (closes #1246): they are counted,
  then downloaded a second time and stored only once the chain clears
  the threshold. Headers of such a chain that an existing data directory
  already holds are kept and read as before, as Core keeps what its block
  index holds, so nothing has to be done about them.
- **`interpreter.check_transactions` is `check_scripts(transaction_data,
  flags, node)`, and checks the scripts alone**: a block's amounts, fees
  and sigop cost are `main._validate_block`'s.
- **`history.log` marks each debug line with its category, `[net]` for
  one, where it had `[debug]`, and `-debug=<category>` writes only that
  category's lines** (closes #1322). A start with `-debug=rpc` no longer
  logs the peer lines; `-debug`, `-debug=1` or `-debug=all` still logs
  every one, and `-debugexclude=<category>` drops one (closes #1609).
- **Verifying a release's attestation names a new signer and the tag.**
  `gh attestation verify` takes
  `--signer-workflow btclib-org/.github/.github/workflows/reusable-build.yml@refs/heads/main`
  and `--source-ref refs/tags/v<version>`; SECURITY.md names the signer of
  an earlier release.
- **The attestation bundle is attached as `v2026.10.4.intoto.jsonl`**, where
  earlier releases attach `v<version>.attestation.jsonl`. A script that
  downloads it by name, or passes it to `gh attestation verify --bundle`, uses
  the new name.
- **`btclib-node` depends on `cryptography`**, through `btclib[bip324]` (issue
  #1190). PyPI serves no `cryptography` wheel for macOS on x86_64 or Windows
  on ARM, so there `pip install btclib-node` builds it from source, which
  needs a C compiler, Rust and OpenSSL: cryptography's installation guide has
  the steps.
- **Peers now speak BIP324 v2 with this node by default** (issue #1190).
  `-v2transport` is on, as in Core, so the node advertises `NODE_P2P_V2` and
  dials v2 where the address advertises it. `-nov2transport` turns it off. A
  peer that speaks v1 only is refused unless the node starts with
  `-v1transport=1`; the next entry has the rest.
- **The node speaks BIP324 v2 alone unless started with `-v1transport=1`**
  (closes #1190). A v1 peer connecting to it is dropped, an automatic
  outbound connection goes only to an address advertising `NODE_P2P_V2`
  or carrying exactly a seed's services, a v2 dial the peer drops is not
  retried with v1, and `addnode` or `addconnection` with `v2transport`
  false is refused.
  Bitcoin Core accepts v1; SECURITY.md's *Where this node departs from
  Bitcoin Core* says why this node does not. `-v2transport=0` turns v1 on
  by itself, and `-v2transport=0 -v1transport=0` is refused at start. To
  reach a v1-only peer or a light client, start with `-v1transport=1`, which
  also restores the v1 retry, or with `-nov2transport`.
- **The mempool refuses a transaction Core's `IsStandardTx`,
  `AreInputsStandard` or `IsWitnessStandard` refuses** (closes #1382). An
  output script of no standard type, more than one dust output, a bare
  multisig of more than 3 keys, a script or witness over Core's limits,
  or a transaction over 400000 weight is answered with Core's reject
  reason by `sendrawtransaction` and `testmempoolaccept`, and not
  relayed. `-acceptnonstdtxn` on a test chain accepts them.
  `-datacarrier`, `-datacarriersize`, `-permitbaremultisig` and
  `-dustrelayfee` set the limits.
- **The mempool refuses a `version=3` transaction BIP431 refuses, a
  transaction that would make a cluster of more than 64 transactions or
  101000 vbytes, and one under 65 non-witness bytes** (closes #1399,
  closes #1383, closes #1687). `sendrawtransaction` and
  `testmempoolaccept` answer `TRUC-violation`, `too-large-cluster` or
  `tx-size-small`, and the transaction is not relayed.
- **The JSON-RPC listener answers a source outside loopback only where
  `-rpcallowip` names it, and `-rpcbind` alone widens nothing** (closes
  #1211, closes #1268, closes #1269, closes #1281, closes #1291,
  closes #1288). It binds `::1` and `127.0.0.1`. A request from any
  other source gets a bare 403, as `bitcoind` answers it, and `-rpcbind` is
  ignored without `-rpcallowip`. A client on another host needs both, e.g.
  `-rpcallowip=<subnet>` and `-rpcbind=<address>`.
- **`btclib-node` sets the umask to 0077 on POSIX** (closes #1198): the
  chain's data directory is 0700 and `history.log` 0600, as `bitcoind`
  leaves them. A job of another account that read either, a backup or a
  monitor, runs as the node's user or is given access.
- **A `history.log` line is stamped and marked as `debug.log` stamps it**
  (closes #1297, closes #1280). The time is UTC ISO 8601 to the second, then
  one space, where it was local time with milliseconds and ` - `. A warning
  is marked `[warning]` and an error `[error]`. The file opens on five blank
  lines and a version line (closes #1309, closes #1305, closes #1306). A
  reader of the file has to parse the new shape.
- **An integer option is read as `bitcoind` reads it, and a port is ASCII
  digits alone** (closes #1313, closes #1324, closes #1285). A value that is
  not an integer was refused; its leading digits are read now, so
  `-maxconnections=12x` reads 12. `-rpcbind`, `-connect`, `-addnode` and
  `addnode` refuse a port with a sign, a space, a `_` or a non-ASCII digit.
  Check the values you pass.
- **A transaction's JSON is Core's `TxToUniv`** (closes #1440, closes #1448,
  closes #1446). A `vout` is `value`, `n` and a `scriptPubKey` that nests
  `asm`, `desc`, `hex`, `address` (where one exists) and `type`, where
  btclib's `to_dict` kept `type` and `addresses` beside it. A `vin` is
  `txid`, `vout`, `scriptSig` and `sequence`, or `coinbase` and `sequence`,
  with `txinwitness` only when the witness stack is not empty; `asm` stays
  btclib's. `getblock` answers `fee` and, at verbosity 3, `prevout`. A client
  written against the earlier shape has to read these.
- **`submitblock` answers a reason where it answered `null`** (closes #1335,
  closes #1390, closes #1339). A tip block failing `bad-cb-height` or `bad-txns-nonfinal`
  answers that reason, and failing scripts answer a reason beginning
  `block-script-verify-flag-failed`. A caller that took `null` for
  acceptance has to read the answer.
- **An RPC call is refused where Core refuses it** (closes #1424, closes
  #1293, closes #1294, closes #1168, closes #1457, closes #1372,
  closes #1405). More
  positional arguments than the method declares, or two or more of the wrong
  type, are refused with the method's full help. A `params` object is mapped
  onto positions, and a name repeated, unknown or given both ways is refused
  with `RPC_INVALID_PARAMETER`. A hex `txid` or `blockhash` of the wrong
  length is refused, and so is a `rawtx` with whitespace. A bare
  `gettxoutsetinfo` answers `muhash` (closes #1387).
- **`sendrawtransaction` and `testmempoolaccept` answer Core's reject reason
  and details, in the order Core checks them** (closes #1328, closes #1375,
  closes #1371, closes #1373, closes #1447). They apply `maxfeerate` and `maxburnamount`,
  and answer `-27` for a confirmed resubmission where they answered `-25`. A
  caller matching on a message has to read the reason.
- **The mempool refuses a spend of an outpoint a held transaction spends, a
  fee under the rolling minimum or `min_relay_feerate`, and a transaction of
  more than 16000 sigops** (closes #1244, closes #1245, closes #1357,
  closes #1332, closes #1252).
  Replacing is not ported (issue #1334), so a conflicting spend is refused
  whatever it pays. A reorg's re-added transactions are exempt from the fee
  floor. `-minrelaytxfee` sets the floor, in BTC/kvB.
- **`Connection.parse_messages` takes the octets read, and
  `Connection.buffer`, `p2p.connection.frame_message` and
  `p2p.connection.frame_message_bytes` are gone** (issue #1190):
  `Connection` frames through `p2p/transport.py`, which holds
  `frame_message_bytes`. A caller that drove a `Connection` by hand passes it
  the octets and imports `frame_message_bytes` from there.
- **The dependency floors rise** (closes #1684, issue #1685). `pip install
  btclib-node` takes `btclib>=2026.10.5`, `btclib-wallet>=2026.10.4` and
  `bitcoin-core-rpc>=2026.10.4`, and upgrading installs btclib-ecc 2026.10.2
  and btclib-secp256k1 0.8.0.10 with them. An install that pins any of these
  lower has to move. `generateblock` refuses a WIF of another chain, as Core
  does, which btclib-wallet 2026.10.4 makes possible.
- **`-datadir`, `-conf`, `-blocksdir` and `-rpccookiefile` are read lexically
  normal** (closes #1187), as Core reads them: a `..` takes the component
  before it off whether that component is missing or a symbolic link, so
  `link/..` names the directory holding the link, not the link target's
  parent. A path written that way lands elsewhere than before.
- **`testmempoolaccept` ends the call at the first bad `rawtx`** (closes
  #1329), as Core does: an array of fewer than 1 or more than 25 is refused
  with `-8`, and the first element of the wrong type (`-3`) or that does not
  decode (`-22`) ends the call, where it answered one entry per element.
- **The Python API under `btclib_node` changed in about sixty more places**
  (issue #1685), for a program that imports it. `release.yml`'s
  `public-api` job lists each against `v2026.9.24`. Among them:
  `rpc.connection.RequestHead` and `RpcConnection`'s send methods;
  `RpcManager.server`, which takes a list of sockets; `Chain.__init__`,
  which requires `fixed_seeds`, `headers_sync_params` and
  `min_bip9_warning_height`; `BlockIndex.get_download_candidates`,
  `BlockIndex.get_headers_from_locators` and `MAX_DOWNLOAD_WINDOW`, gone;
  `PeerDB`'s DNS and sampling helpers, gone; and `Config`'s `rpc_host`,
  whose default is now `None`.
- **An HTTP header section over 8192 bytes is refused**, as Core's
  libevent does; it was 65536. A JSON-RPC client sending larger headers
  has to trim them.

### Worth knowing, though nothing raises

- **A received message is logged under the `net` debug category, as Core logs
  it, and a payload that does not parse is one `net` line without a
  traceback.** `-logratelimit`, on by default, caps each source location's
  non-debug lines in `history.log` at 1 MiB an hour, as Core does; debug lines
  are never capped, and `-nologratelimit` removes the cap.
- **A relayed transaction's scripts are checked on `Node.worker_pool`, not on
  `Node`'s thread** (issue #1685), so peers keep being served while they run.
  One check is in flight at a time and a peer's next `tx` waits. A check with
  no verdict after 300 seconds is dropped and logged. `sendrawtransaction`,
  `testmempoolaccept` and a reorg's re-added transactions are still checked on
  `Node`'s thread.
- **The address table is Core's addrman** (closes #1308): new and tried
  buckets, test-before-evict, and failed attempts counted. The 10,000-address
  bound is replaced by Core's bucket capacity. A terrible answered address
  stays in the tried table until another takes its slot. On the first start
  over a store written by an earlier release, its rows are placed again, each
  as its own source, and a row with no free slot is deleted (about 1% of
  10,000 answered rows, by the PR's own measurement).
- **A block whose legacy, P2SH and witness sigops together cost more than
  `MAX_BLOCK_SIGOPS_COST` is refused `bad-blk-sigops`** (closes #1585), as
  Core's `ConnectBlock` refuses it.

## v2026.9.24

**The third release.** Everything below is measured against `v2026.9.4`:
what changed since, and what it costs to move past it.
`CHANGELOG.md`'s own `v2026.9.24` section is the record of everything
that went into it.

The rpc surface also grew — `getblock`, `submitblock`, `addnode` and
`getnetworkinfo` join the dispatch table (closes #1006) — which is
additive and costs nothing to move past.

### Breaking changes

- **`CoinStats.insert` and `.remove` no longer report whether the coin
  was unspendable, and `digest` is a property, not a method** (closes
  #869, btclib-org/btclib#1623).
  `btclib_node.chainstate.muhash.CoinStats` now inherits both from
  `btclib.coinstats.CoinStats` rather than defining them itself:
  `insert`/`.remove` return `None` where `v2026.9.4` returned `bool`
  (`True` unless the coin was unspendable), and `stats.digest()` raises
  `TypeError` where `v2026.9.4` had `digest` as a method -- `stats.digest`,
  with no parentheses, is the replacement. The constructor, `serialize`
  and `deserialize` keep `v2026.9.4`'s own shape. `tx_out_ser` and
  `is_unspendable` are re-exports of btclib's functions of the same
  name: `tx_out_ser` returns what it did for a well-formed coin and
  outpoint, and raises `BTClibValueError` or `BTClibTypeError` where
  `v2026.9.4` serialized a malformed one.

## v2026.9.4

**The second release, and the first that is an upgrade from
something.** Everything below is measured against `v2026.8.27`:
what changed since, and what it costs to move past it.
`CHANGELOG.md`'s own `v2026.9.4` section is the record of everything
that went into it.

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
  signatures or script"`; `testmempoolaccept` answers the same
  internal-error response for the whole batch, rather than the one
  entry's own reject-reason (issue #668); the peer-to-peer path no
  longer discourages a peer for exposing this node's own corrupted
  storage. Read the log for `StoreCorruptionError` rather than
  trusting any of them. A record whose bytes pass that checksum and
  still cannot be parsed is a different case, answered as absent (issue
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

- **`Config(pruned=True)` now builds a node and actually prunes,
  instead of raising `PruningNotImplementedError`** (issue #601). A
  caller catching that exception around the constructor no longer has
  one to catch; `pruned=False`, the default, is unaffected. A pruned
  node keeps only the last 288 blocks and their undo data on disk and
  deletes the rest as the chain advances -- there is no `-prune=<n>`
  MiB target and no `pruneblockchain` RPC, only whether pruning is on.
  Nothing to migrate for an existing, unpruned data directory: pruning
  only ever removes data going forward from when it is first turned on.

- **`-prune=<n>` now matches Core's own manual/automatic split, instead
  of collapsing every nonzero `<n>` to the fixed depth above** (issue
  #705). `-prune=1` is manual pruning: nothing is deleted on its own any
  more, only the new `pruneblockchain` RPC deletes, and only when asked
  -- a node started with `-prune=1` expecting the old automatic deletion
  now has to call that RPC itself, or use `-prune=550` or higher for
  automatic pruning to a MiB target instead. `-prune=<n>` from `2` to
  `549` now refuses to start rather than pruning to the fixed depth,
  Core's own wording: too small a target to actually run a node on.
  `-prune=<n>` at `550` or above is new: automatic pruning to roughly
  `<n>` MiB on disk, `getblockchaininfo`'s own `automatic_pruning` and
  `prune_target_size` answering for it.

- **A mempool candidate is now also checked against Core's own relay
  policy, not only consensus** (issue #810). `v2026.8.27` held a
  transaction to `getblockchaininfo`'s activation-gated consensus rules
  alone; a script only a standardness rule refuses -- `CLEANSTACK`
  among them -- now gets the transaction dropped rather than accepted,
  matching what a real Bitcoin Core peer would refuse to relay or mine
  in the first place. `sendrawtransaction` answers `VERIFY_REJECTED` /
  `"Invalid signatures or script"` for one, `testmempoolaccept` reports
  it not allowed, and the peer that relayed it over p2p is kept rather
  than discouraged -- Core's own policy is not to penalise a peer for
  forwarding a transaction only the non-mandatory rules refuse. Nothing
  to migrate: a transaction built to consensus rules alone and relying
  on this node's mempool to hold it despite failing standardness has to
  be built to a standard script instead, the same script a real network
  would have refused it over regardless.

### Windows

- **`pip install btclib-node` now claims Windows** (issue #430): its
  classifiers name it, and `test.yml`'s gate checks the claim on every
  pull request.

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
