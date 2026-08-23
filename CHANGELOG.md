<!-- markdownlint-disable MD022 MD032 -->
<!-- This file is merge=union, so a rebase joins two sections and drops
     the blank line between them without a conflict: the rule is off
     here for the duration of btclib-org/.github#33, and goes back on
     when that queue is empty. btclib-org/.github#138 is the record. -->

# Changelog

What a reader of this repository would notice, in the group it belongs
to: what changed, why, and what it cost. There are no release notes for
this file to be the record behind: `CONTRIBUTING.md`'s *A version, and
no release* has why, and what the `## Unreleased` heading becomes.

The record starts here. `v0.1.0` was tagged before this file existed and
nothing is reconstructed for it: a changelog written backwards from a git
log is a guess at what somebody would have noticed, and there is no way
to check the guess.

## Unreleased

### `P2pManager.stop` waits on its own thread instead of spinning a core

- **`P2pManager.stop` blocks on `self.join()` rather than polling
  `self.loop.is_running()` in a tight loop** (#249). The calling thread
  no longer spins a full CPU core for the scheduling delay of the
  manager's own event-loop thread; the join is skipped where the
  manager was never started, which `Node.run` reaches unconditionally
  for a node built with `p2p_port` unset.

### `PeerDB` settles on one row per endpoint, on disk and in memory alike

- **`add_addresses` no longer keeps a second member of `self.addresses`
  for an endpoint already known, where a later gossip carries different
  `services`** (#247). Two records differing only in `services` used to
  become two entries: the durable `known-` row already settled on the
  endpoint (keyed on network id, address and port, not `services`), but
  the in-memory table did not, so the 10000-entry cap could be spent on
  several rows for the one endpoint, and `random_address`'s uniform draw
  favoured whichever endpoint had been gossiped with more than one
  `services` value. Updating an endpoint already held does not count
  against the cap; only a genuinely new one does.
- **`get_active_addresses` deletes an `answered-` row from the store
  once the entry it backs ages out of `active_addresses`** (#253).
  Nothing here called `KeyValueStore.delete` before, so a durable row
  outlived the endpoint it recorded for as long as the process ran,
  bounded only by the count of distinct endpoints ever dialled
  successfully rather than by what is still active.

### `REPOSITORY.md`'s required-checks section names what main now enforces

- **`REPOSITORY.md`'s *Required checks on main* section names `Lint and
  type-check` and `test: every job passed`** (#223), where it said none
  did. Classic branch protection's `required_status_checks.contexts`
  carries both; no ruleset on `main` carries a `required_status_checks`
  rule.

### `license-files` names `AUTHORS.md` beside `LICENSE`

- **The built wheel's `dist-info/licenses/` now carries `AUTHORS.md`
  next to `LICENSE`** (#235). The MIT notice names *The btclib
  developers*, a collective, and `AUTHORS.md` is where the archive says
  its members are listed. The sdist already carried the file through
  `[tool.uv.build-backend]`'s `*.md` pattern; only the wheel was short
  it.

### `pytest-remotedata` and `--remote-data=any` are gone

- **`pyproject.toml` drops the `pytest-remotedata` dependency and
  `addopts`'s `--remote-data=any`** (#227). #135 removed the only
  `@pytest.mark.remote_data` tests the tree had; `git grep -n
  remote_data -- tests/` finds none, so neither bought anything left to
  drop it for.

### The mypy configuration's prose states its decision once, and in the present

- **The move off the hosted `mirrors-mypy` hook is argued once, in
  `.pre-commit-config.yaml`, with `pyproject.toml`'s
  `[dependency-groups].lint` comment pointing there instead of arguing
  it again** (#210). Both comments also drop a paragraph on
  `[tool.mypy]` not being strict yet, which `strict = true` already
  states is no longer so.
- **`[tool.mypy]`'s comment on `show_error_codes` no longer cites
  btclib-org/.github#170 as an open divergence from the organization's
  sample** (#228). That issue is closed and the sample no longer sets
  the key; what stays is why the key is inert here regardless — `mypy
  --help` names it only as the inverse of `--hide-error-codes`, which
  reads `False` with or without the line.

### A peer's `sendheaders` now changes what a connected block is sent as

- **`callbacks.sendheaders` sets `Connection.prefers_headers`.** Neither
  dispatch table had a `sendheaders` entry, so the message was silently
  dropped and nothing recorded the preference Core's own handler sets
  as `peer.m_prefers_headers` (#202).
- **A block `update_chain` adds to the active chain is now sent to
  every connected peer** — as `Headers` where the peer's own
  `sendheaders` asked for that, as `Inv` otherwise — once the node is
  past its own initial sync, the gate the mempool bookkeeping beside it
  already uses. The only `Headers` this node sent before answered a
  peer's own `getheaders`, and the only `Inv` it built was for a
  transaction (`download.py`): a block this node accepted reached
  nobody, by either shape (#202).

### A short header batch that connects to nothing known gets a `getheaders`

- **`callbacks.headers` asks for what is missing whenever a batch's tip
  is `None`, whatever the batch's own length.** The
  `len(headers) == 2000` guard was the only place a follow-up
  `GetHeaders` was built, so a short, BIP130-style announcement whose
  first header's parent this node does not know was silently dropped
  rather than asked for (#233) — unlike Core's own
  `HandleUnconnectingHeaders`, which asks regardless of batch size.

### A header out of parent-before-child order no longer vanishes

- **`BlockIndex.add_headers` refuses the whole batch, rather than
  silently dropping one header, when a header's parent is itself later
  in the same batch.** A header whose parent had not yet arrived was
  left out of `pending` with a bare `continue`, and never retried once
  its parent was processed a few lines later in the same call: neither
  an exception nor the batch's own return value said anything had been
  lost (#214). A peer is not required to send a `headers` message in
  strict parent-before-child order, and Core's own per-message
  continuity check (`CheckHeadersAreContinuous`, `net_processing.cpp`)
  refuses such a message unconditionally rather than accepting part of
  it; this now raises `BTClibValueError` the same way a header failing
  its own proof-of-work or context check already does, so the caller
  can tell the batch was refused. A header whose parent this index has
  never heard of at all, from this batch or an earlier one, is
  unaffected: that batch still answers `None`, not a refusal (#75).

### `header_index` drops a chain once `invalidate` has proved it bad

- **`BlockIndex.header_index` no longer holds a chain `invalidate` has
  since marked bad.** `add_headers` weighed a candidate for
  `header_index` purely by chainwork, the same way it weighed
  `block_candidates` before #77/#120/#125, so a peer sending more
  headers onto an already-invalidated fork kept growing what this index
  reported as its best known header chain, and `invalidate` itself never
  touched `header_index` for a chain it already held (#218). Both are
  fixed the way Core's own `InvalidateBlock` (`src/validation.cpp`)
  recomputes `m_best_header`: `add_headers` skips a header computed
  invalid the same way it already skips it for `block_candidates`, and
  `invalidate` rescans the whole index to rebuild `header_index` when,
  and only when, the block it just invalidated was part of it.
  `generate_header_index`, which shares the rescan with `invalidate`,
  used to build `header_index` from a reloaded database without ever
  reading `BlockStatus` either, so a restart could bring an invalidated
  chain back as the best known one; it now reads it the same way
  `generate_block_candidates` already did.

### `PeerDB` keeps what it learns, and prefers what answered

- **`PeerDB` opens a `KeyValueStore` at `data_dir / "peers"` and reads it
  back in `init_from_db`.** Before this, `init_from_db` was `pass` and
  nothing under `btclib_node/` wrote an address anywhere, so every start
  was a cold one and the DNS seeds were the only way back in (#123).
  `add_addresses` and `get_addr_from_dns` write a `known-` row per
  address they add, keyed on its network id, address and port -- not on
  `timestamp` or `services`, so a row settles on the endpoint rather than
  growing one per gossip. `add_active_address` writes an `answered-` row
  the same way, carrying the moment this node itself last heard back
  from that endpoint. `data_dir` is `Path | None` now: `None` keeps a
  `PeerDB` in memory only, which is what every caller other than `Node`
  already did with it.
- **`random_address` prefers an address from `get_active_addresses` over
  the uniform draw across the whole table**, within a single run and not
  only across a restart: dialling used to draw uniformly from
  `self.addresses` regardless of whether any of it had ever answered.
- **`ask_dns_nodes` is decided by what has answered recently, not by
  whether the table is empty.** A table `add_addresses` filled with tor,
  i2p or an ipv6-only answer from a seed is not empty and was not one
  before either, and dialling never had anything in it to draw on --
  the AAAA-only table #89 describes. It is now `not any(can_connect(a)
  for a in get_active_addresses())`, which also covers the empty case:
  nothing has ever answered where nothing is known at all.
- **`Node.run` closes the peer store on shutdown**, alongside the
  chainstate and the block database.

### IPv6 is dialled, and a listener accepts on it too

- **`can_connect` admits `BIP155Network.IPV6`, and `dial` opens an
  `AF_INET6` socket for it.** Both used to answer only for `IPV4`, so a
  peer table that held nothing else -- a DNS seed answering with AAAA
  records alone, or a peer that only ever gossiped v6 addresses -- had
  nothing this node would draw and dial (#124). `PeerDB.random_address`
  already answers `None` for a table with nothing dialable (#89), so a
  peer whose family this node cannot reach is passed over the same way a
  peer that is merely slow or refusing is: `dial`'s existing
  `_DIAL_TIMEOUT` bounds the attempt, and a host with no IPv6 route fails
  it the same way. No separate reachability check was added for that
  case -- Bitcoin Core's own default is "everything is reachable"
  (`ReachableNets`, `src/netbase.h`), leaving an unreachable family to
  fail its own connection attempts rather than being detected ahead of
  one.
- **`P2pManager` binds an IPv6 listener beside the IPv4 one**, `::` with
  `IPV6_V6ONLY` set so that a v4 peer is never accepted on it wearing its
  address mapped into sixteen octets. The IPv6 bind is not required for
  `run` to succeed: a host with no IPv6 support fails only that one, and
  the IPv4 listener above is what a caller of `run` can still rely on --
  Core's own `InitBinds` treats its "::" the same way. An inbound
  peer's `sockaddr` is sliced to its host and port before becoming a
  `NetworkAddressV2`, an `AF_INET6` one carrying two fields BIP155 has
  nowhere to put.

### A BIP155 IPv6 record that embeds another network is not kept

- **`PeerDB.add_addresses` drops an `IPV6` record whose sixteen octets
  are `::ffff:0:0/96`, the IPv4 mapping, or OnionCat's
  `fd87:d87e:eb43::/48`, once how a TORv2 address was embedded in a fake
  IPv6 one.** BIP155 says a client should ignore both; before this the
  table kept whatever an `addrv2` carried, so a v4-mapped entry sat
  under network id 2 and `addr_entry` later wrote it into an `addr`
  version 1 message using the same sixteen octets an ordinary IPv4 peer
  uses -- a peer reading it back saw IPv4, and the same host was two
  entries in the table (#151). `btclib.p2p.addrv2`'s own docstring calls
  both rules receive policy left to the caller, which is why the check
  is here and not in the codec.

### The coverage floor knows what asked for it, and what erases it

- **`relax_coverage_floor`'s `asked_for` reads `config.option.cov_fail_under`
  instead of scanning `config.invocation_params.args` for a
  `--cov-fail-under` prefix** (#180). `invocation_params.args` is only
  what was handed to `pytest.main`; pytest splices `PYTEST_ADDOPTS` in
  afterwards, so a floor asked for that way never appeared in the scan
  and was silently stood down to 0 on any run `PYTEST_ADDOPTS` narrowed.
  `option.cov_fail_under` is argparse's own parsed result and carries the
  flag regardless of which of the two wrote it.
- **`CLAUDE.md` names the coverage data a second `pytest` invocation
  erases, and the environment variable that keeps it out of reach**
  (#191). `pytest-cov`'s own `tryfirst` hook on
  `pytest_load_initial_conftests` calls `cov.erase()` before this tree's
  `conftest.py` is even imported, so nothing at this repository's pytest
  configuration surface intercepts it; under `-n auto` that erase sweeps
  every parallel-suffixed file in the rootdir, including a running
  suite's own workers', and `--help` reaches it too. `COVERAGE_FILE` is
  read by `coverage.py` from the environment rather than through that
  hook chain, so pointing it outside the rootdir avoids the collision
  entirely; a same-prefix name still inside the rootdir does not, since
  a concurrent plain invocation's own `erase(parallel=True)` globs its
  base filename plus `.*` in that base's directory regardless of what
  name the protected run chose.

### `get_cfilters` stops once the connection it is answering has closed

- **`get_cfilters`'s loop over a `getcfilters` range now breaks once
  `conn.status == P2pConnStatus.Closed`.** `Connection.async_send`'s
  send-buffer bound (#101) can drop a connection partway through a
  `getcfilters` answer; before this, the loop kept serializing a
  `CFilter` and scheduling a send for every height still left in the
  range regardless, none of it ever reaching a peer whose socket is
  already closed (#239). `get_cfheaders` and `get_cfcheckpt` build their
  one answer across their own loop and call `conn.send` once at the end
  rather than per height, so neither has a doomed send mid-loop to skip.

### `BlockInfo.chainwork` moves off the serialized record

- **`BlockInfo.serialize` and `.deserialize` are inverses again.**
  `chainwork` used to be a field on `BlockInfo` that `serialize` never
  wrote and `deserialize` left at its default, so a round trip through
  the two was not the identity for a block whose chainwork was not
  zero (#201). It is no longer a field: chainwork lives in
  `BlockIndex.chainwork`, a `dict[bytes, int]` keyed by hash that
  `calculate_chainwork`'s start-up walk writes into directly, so a
  start does not rebuild one `BlockInfo` per header just to attach a
  derived value. `rpc/callbacks.py`'s `getblockheader` reads the same
  value from there now.

### A shutdown mid-reorg is noticed between blocks, not once per fork

- **`update_chain` reads `terminate_flag` before starting each block of
  `to_add`, not only at the top of `Node.run`'s own loop.** The wait
  `Node.stop` bounds used to scale with the whole fork: `check_transactions`
  is a blocking `worker_pool.starmap` over one block's inputs -- on
  mainnet, thousands of signature checks -- and nothing inside
  `update_chain` read the flag, so a shutdown requested during a deep
  reorg could outlast `STOP_TIMEOUT` and be reported as a node that
  would not stop, when it was validating correctly (#139). `STOP_TIMEOUT`
  itself is unchanged: it already had to cover only what one pass of the
  loop could take, and what one pass can now take is one block rather
  than a whole fork.
- **Stopping there rolls the trial back the way a failed block does,
  without marking any block invalid.** Nothing `update_chain` buffers
  along the way -- the utxo set, the reverse patches, the compact
  filters -- reaches disk before every block of the fork has validated,
  so there is no partial state for a stop to leave behind: the
  chainstate is exactly where it was before the call started. What
  distinguishes a stop from a block that failed its own check is that
  `update_header_index` is never called for it -- a shutdown is not the
  block's own defect, and the candidate is offered again on the next
  run.

### A reverse patch is filed with its own block, once its branch connects

- **`BlockDB` resolves the `.rev` file a patch goes in from the block it
  undoes, not from whichever `.blk` file happens to be open when it is
  written.** `add_rev_block`'s target used to be `self.file_index`, the
  block file currently being written, so a patch and the block it
  undoes could land in files with unrelated numbers. `BlockDB.finalize`
  now reads the block's own stored location instead, so the two always
  share a file number (#116). A patch already on disk keeps whatever
  file it was written to -- `get_rev_block` reads back through the
  location recorded for it rather than deriving one from a block's own
  file, so nothing already stored needs moving.
- **`add_rev_block` buffers what it is given, and `BlockDB.finalize` or
  `rollback` decides whether it reaches disk** -- the same pattern
  `UtxoIndex` and `FilterIndex` already hold their own writes under.
  `update_chain` calls `finalize` only for the branch that connects and
  `rollback` for the one it refuses, where before, a patch reached disk
  as soon as its own block validated: the earlier, validated blocks of
  a branch whose tip then failed kept their reverse patches on disk
  with nothing left pointing back to them (#200).

### Header sync tells a refused batch from an empty one, and moves on

- **`BlockIndex.add_headers` raises on a batch it refuses instead of
  answering `False`, the same answer it gave a batch that carried
  nothing new.** A header failing its own proof of work or a contextual
  check is a peer that sent something invalid, not a peer with nothing
  further to offer, and `callbacks.headers` no longer treats the two
  alike: the raise reaches `handle_p2p`, which drops the connection the
  way a bad block's raise already does (#75).
- **`add_headers` returns the hash of the highest header in the batch
  now indexed instead of a bool.** `header_index` only moves for a
  header that extends it or beats its chainwork, so a fork arriving
  below the active chain's tip left it where it was; the next
  `getheaders` locator asked for the same batch again, and the sync
  stopped short of the fork's own tip (#122).
- **`callbacks.headers` names that hash in a full batch's next
  `getheaders` locator only for a live fork below `header_index`'s own
  tip.** An ordinary batch extending `header_index` keeps its richer,
  multi-entry locator, which already reached that case; a batch built
  on a header this node has already proved invalid does too, rather
  than asking the same peer for more of a branch already proved bad,
  with no misbehaviour scoring anywhere in this tree to ever stop that
  otherwise (#75, #122).

### A connection is not reachable by any send until its handshake finishes

- **`P2pManager` keeps an accepted or dialled connection in a new
  `pending_connections` dict until `callbacks.verack` promotes it into
  `connections`** (#131), which is where every send that used to reach a
  handshaking peer read from: `broadcast_raw_transaction`, `ping_all`,
  the housekeeping sweep's own ping, and `DownloadManager`'s sends over
  the same dict. Before, a new connection went straight into
  `connections`, so an `Inv` or a raw `Tx` could reach a peer before its
  own `version`/`verack` exchange was done, which the protocol treats as
  a violation.
- **The housekeeping sweep still closes a connection stuck mid-handshake,
  without pinging it first**: `ping` is itself one of the messages a
  connection cannot be sent before `verack`, so a pending connection past
  the same idle bound `connections` are held to is dropped once quiet
  rather than pinged and given a second window to answer.
- **`stop_all` and the manager's own `stop` still close a pending
  connection**, and the housekeeping loop's dial count and
  already-connected check both read from `pending_connections` too, so a
  peer mid-handshake is neither left dangling on shutdown nor dialled a
  second time.
- **`getconnectioncount` still counts a peer mid-handshake**, matching
  Core's own `GetNodeCount`, which counts every entry of `m_nodes` and
  not only the ones that finished negotiating.

### `Connection` bounds what it queues to write, and drops a peer past it

- **A peer answered with more than `Connection` will queue is dropped
  rather than left to grow the queue further** (#101). `getcfilters`,
  bounded to 1,000 answers per request by `_filter_range`, is the
  message the issue names: nothing stopped a peer from pipelining a
  second request before the first's answers had gone out, so the
  per-request bound did not bound what a peer could have outstanding at
  once. `btclib_node/p2p/connection.py`'s `async_send` now tracks
  `queued_send_bytes`, every serialized octet handed toward the socket
  and not yet written, and refuses to queue a message that would push
  the total past `MAX_QUEUED_SEND_BYTES`, calling `stop()` instead.
- **The bound is BIP157's own traffic, not Core's `-maxsendbuffer`
  default.** Core's cap (1,000,000 bytes) is where Core starts pausing,
  not a size any one answer is held to -- its own send queue for an
  in-progress `getcfilters` answer routinely exceeds it while paused,
  because the per-request bound alone reaches tens of megabytes. This
  node drops instead of pausing (below), so its own number has to
  accommodate a whole legitimate answer rather than start throttling
  where Core's does. `MAX_QUEUED_SEND_BYTES` is derived, not copied:
  measuring `btclib`'s own Golomb-Rice filter encoder puts a filter
  element at about 2.632 bytes regardless of scale, a real mainnet block
  (height 481824, btclib's own test fixture) anchors what one busy block
  costs at around 24.5 KB of filter, and four times that block's element
  count stands in for a block nearer this node's own present -- about
  98 KB. `MAX_GETCFILTERS_SIZE` (1,000) of those is one legitimate
  answer at its largest, about 98 MB, the tens of megabytes the issue
  itself measured; `MAX_QUEUED_SEND_BYTES` is twice that, room for one
  answer to drain in full and a second one -- pipelined behind it, or
  simply the next request -- to be under way as well.
- **Dropping the connection instead of pausing it, unlike Core's own
  choice for a full send buffer.** Core's `ProcessMessages` and
  `ProcessGetData` (`net_processing.cpp`) each check `fPauseSend` before
  generating another message for a peer over budget, leaving what is
  already queued to drain and resuming the next call; this node has no
  message-processing stage separate from the handler that calls `send`
  once and is done, so there is no later call to resume at. Refusing to
  queue further and dropping the connection is what the same
  backpressure comes to here.
- **The writes themselves are now serialized through a lock**,
  `Connection.send_lock`: two `async_send` calls both past the point
  where `loop.sock_sendall`'s own first, synchronous `sock.send` could
  not take everything would otherwise register on the same file
  descriptor, and `BaseSelectorEventLoop._add_writer` cancels whichever
  of the two was already waiting rather than queuing behind it — a
  second message's bytes reaching the peer ahead of the first's
  remainder, on the same stream.
- **`Connection.stop` is idempotent.** Several messages queued at once
  can each independently discover the connection is over budget before
  any of them has changed anything a later one could check instead, so
  more than one can call `stop` for the same connection; a second call
  now does nothing rather than telling `peer_db` about the same address
  twice.

### `Connection.__repr__` spells a peer's endpoint through `ip_and_port` too

- **`btclib_node/p2p/connection.py` and `btclib_node/rpc/connection.py`
  both format their `__repr__` through `btclib_node/p2p/address.py`'s
  `ip_and_port` instead of `f"{peer[0]}:{peer[1]}"`** (#209). A peer at
  `2001:db8::1` on port 8333 now reads `[2001:db8::1]:8333` in a log
  record or a traceback carrying either connection's `repr`, where the
  hand-written spelling gave `2001:db8::1:8333`. Every p2p socket is
  `AF_INET` and the RPC listener binds `0.0.0.0`, so what either prints
  for a peer this node can have today does not move.

### A lint hook, not `.gitattributes`, catches what union merges in silence

- **`merge=union` stays** (#199): `btclib-org/.github#21` decided against
  dropping it — a guaranteed conflict on every pull request appending to
  a group, in a file where that conflict has nothing to decide, is a
  worse trade than the rare silent one, and `git merge-tree --write-tree
  --messages` on two commits that each add a `###` heading at the same
  point shows dropping the attribute does exactly that: the merge that
  used to exit 0 now exits 1 on every such pair, not only the ones that
  drop a blank line. Doubling the trailing blank line of every entry
  does not survive the same merge either — the boundary between two
  branches' own new headings still collapses to none — and a custom
  merge driver needs a section in the local, unversioned `.git/config`
  that no `.gitattributes` entry can supply, so neither is a
  repository-versioned alternative.

- **A local `pre-commit` hook now runs the check by hand instead of
  requiring somebody to run it by hand** (#199): `changelog-heading-
  blank-line` fails on any `###` line in `CHANGELOG.md` not preceded by
  a blank one, which `markdownlint-cli2`'s own MD022 does not catch here
  since it is disabled for this file. It runs as part of the same `uv
  run pre-commit run --all-files` a rebase already asks for, not as an
  installed git hook: `CONTRIBUTING.md`'s *The gate is not installed as
  a git hook* is why, `.git/hooks` being shared by every worktree of
  this repository.

- **The headings union had already collapsed are restored** (#194): the
  blank line is back before `links.yml`'s own heading and before
  `getrawmempool`'s.

- **The `REVIEWING.md` entries filed under `enable_error_code`'s own
  heading move to one of their own** (#194), ahead of
  `enable_error_code`'s remaining bullet.

### A branch this node has proved bad stops being offered

- **`BlockIndex.invalidate` is the one place a block's invalidity is
  recorded, and what it costs**: the block itself and every header this
  index has already indexed on top of it, candidate or not, are marked
  `BlockStatus.invalid` and dropped from `block_candidates` where
  present -- a new `children` map, the reverse of `previous_block_hash`,
  is what the walk costs the size of the bad lineage rather than the
  whole index. `add_headers` refuses to build a `valid_header` on a
  parent already carrying that status, so a header arriving afterwards
  inherits it without a walk of its own (#125).
- **A block whose `assert_valid` raises is invalidated before the peer
  that sent it is dropped**, so the next peer offering it is refused
  before being asked to send it again (#77).
- **`update_chain`'s failure path invalidates the block whose
  contextual check raised** rather than leaving every header built on
  it a candidate forever (#120).
- **`get_first_candidate` asks whether a candidate's whole branch has
  arrived, not just its tip.** A hole behind a downloaded tip used to
  pass that check, and `update_chain` then gave up its whole pass on
  the hole, leaving the same candidate at the front of the queue every
  time; a complete branch further back could not connect until it
  filled. It is stepped over now, the way a branch missing its tip
  already was (#121).

### `[tool.mypy]` sets nothing mypy already has on

- **`show_column_numbers` is set, so an error message names the column
  it starts at and not the line alone** (#173): the position reads
  `2:14` where it read `2`. Section 6 of the organization's standard
  samples the setting and mypy leaves it off.

- **`strict_bytes` is gone: mypy has it on, and the line bought no
  check** (#182). `show_error_codes`, which section 6 samples too, is of
  that same kind and is not added; btclib-org/.github#170 is the
  divergence from the sample, and the comment above the settings now
  carries the command that reads one back out of mypy's parsing of this
  file, so either claim is one line to check.

- **The comment above `enable_error_code` names no code as one mypy has
  on** (#190). The codes it named read as the whole of mypy's
  default-enabled set, where the command beside them is what decides
  any code.

- **The mypy hook's comment in `.pre-commit-config.yaml` states no count
  for the hosted hook it replaced** (#162). Nothing here configures that
  hook, so no command in this tree re-derives the number.

### A peer's endpoint is `ip_and_port`'s spelling, in the log too

- **The `Connected to` line the handshake writes goes through
  `btclib_node/p2p/address.py`'s `ip_and_port`, and asks the socket for
  the peer once** (#189). A peer at `2001:db8::1` on port 8333 reads
  `[2001:db8::1]:8333`, where the hand-written spelling gave
  `2001:db8::1:8333`, the host running into the port with nothing
  between them to say which is which. Every p2p socket is `AF_INET` and
  `dial` refuses every network but `BIP155Network.IPV4`, so what is
  written for a peer this node can have today does not move.

- **`P2pManager.server` calls `loop.sock_accept`'s answer `sockaddr`**
  (#185), which is what `PeerDB.get_addr_from_dns` calls the same pair.
  The name it had was the formatter's own, and the assignment made that
  name a local of `server` for the whole function: importing the
  formatter and calling it there would raise `UnboundLocalError` before
  the accept.

### A comment says why the code is as it is, in words a reader can look up

- **The comment on `get_peer_info`'s broad `except` gives the reason the
  exception is swallowed and stops there** (#193). What it deferred to
  was a bandit `# nosec B112` suppression that
  `grep -rn nosec btclib_node tests` does not find.

- **The comment on `callbacks.version`'s protocol-version check spells
  `simplicity`** (#203). Neither `codespell` nor `typos` reports the
  Italian spelling it replaces, so the lint gate catches no such word.

### The root prose and the editor configuration describe this repository

- **`README.md` opens with the name `pyproject.toml` declares** (#158).
  `btclib_node` is the import package and `btclib-node` is the project,
  and the heading, the opening sentence and the bold line say the second.
  The link on that opening name is dropped rather than repointed:
  `https://github.com/btclib-org/btclib_node` answers `301` to the
  address the *Source* line already carries, and a redirect resolves only
  while nobody else claims the name it redirects from.

- **`REPOSITORY.md` records what its calls answer and compares this
  repository to no sibling** (#157). Private vulnerability reporting
  answers `{"enabled":true}`, so the advisory route the organization's
  security policy sends a reporter to is open here, where the file said
  it was off and that siblings had it on. The wiki and the projects board
  are on and the standard states no rule about either, which makes them
  this repository's own answer rather than the divergence the file called
  them; `git ls-remote` against the wiki is what says it holds nothing.

- **`.vscode/extensions.json` justifies each entry by a hook this
  repository runs** (#161). `.pre-commit-config.yaml` carries `actionlint`
  and `zizmor`, which read `.github/workflows/`, where the comment on
  `github.vscode-github-actions` said no hook read a workflow at all and
  named a single file there. The header's account of what a sibling has
  and this tree does not is dropped: `.github/dependabot.yml` is in the
  tree with a `check-dependabot` hook over it, and the rule the list is
  kept to is the sentence above it.

- **`.vscode/settings.json` states no hook list and no path list**
  (#161, #163). The enumeration of what no extension wraps was wrong in
  both directions — `yamllint` was in it while `extensions.json`
  recommends `redhat.vscode-yaml` for that hook — and what stands in its
  place is that a hook with no entry in `extensions.json` is seen by the
  gate alone. The pyright survey drops `btclib_node tests`: pyright
  excludes every hidden directory by default, so with no paths it reads
  the directories `[tool.mypy]`'s `files` names, `scripts` among them.

### A header is checked for the target and the time its height requires

- **`BlockIndex.add_headers` runs `assert_valid_in_context` beside
  `assert_valid_pow`** (#118), in `btclib_node/chainstate/contextual.py`:
  the compact target the header's height requires,
  `next_bits_required` (Core's `GetNextWorkRequired`), and that its
  timestamp is later than the median of the eleven ancestors before it,
  `median_time_past` (Core's `GetMedianTimePast`). Before this,
  `assert_valid_pow` asked only whether a header's hash met the target
  the header itself claimed, so a header claiming any easier target
  within the network's limit was credited that chainwork regardless of
  what the chain at that height required.
- **`Chain` carries `pow_allow_min_difficulty_blocks` and
  `pow_no_retargeting`**, Core's `fPowAllowMinDifficultyBlocks` and
  `fPowNoRetargeting`, each set once per network in `Main`, `TestNet`,
  `SigNet` and `RegTest`'s own `__init__`.
- **A header's parent may be another header earlier in the same
  batch**, not only one already in the index: `add_headers` weighs each
  header against what came before it in its own `headers` message
  before any of the batch is indexed.
- Left unchecked: the BIP94 timewarp rule, which holds only on testnet4
  and on a regtest run started with `-test=bip94`, neither of which
  this node offers, and the version-floor checks
  `ContextualCheckBlockHeader` also makes.

### A `match` statement has to cover the type it matches on

- **`exhaustive-match` joins `enable_error_code`, so a `match` leaving a
  member of its subject's type unhandled is an error** (#188). mypy
  leaves that code off and no flag `strict = true` sets turns it on, so
  without the entry

  ```python
  def f(v: int | str) -> str:
      match v:
          case int():
              return "i"
      return "?"
  ```

  type-checks. `mypy` passes over `files` with the entry as it does
  without it, and what the entry answers is the first `match` written
  there.

  The comment above the list answers for `unused-ignore` too, which
  stays outside it.

### A read of the block index is not a write of it

- **`BlockInfo` is frozen, so what `BlockIndex.get_block_info` hands out
  is the index's own record and a caller cannot assign to its fields**
  (#117). Assigning to one is refused by the type check,
  `Property "status" defined in "BlockInfo" is read-only`, where it
  changed the index in place, reaching neither the write batch nor the
  database. `header` is btclib's own dataclass and is not frozen, so
  that one field is still a caller's to change.

- **`BlockIndex.set_status` and `BlockIndex.set_downloaded` are how the
  fields a caller changes are changed.** Each reads the record the index
  holds and writes its replacement to memory and to the database in one
  call, so a copy that has gone stale cannot be written back over one
  that has not. `insert_block_info` is private to the index and
  `main.update_block_status` is gone.

- **`update_chain` writes no status while a branch is being tried.** The
  status set on the trial path reached the database as the trial ran and
  refusing the branch did not take it back: a fork whose tip prints
  money left the blocks below that tip at `valid` in the database, where
  the utxo set the same pass wrote was rolled back. The database write
  moves into the batch `update_chain` commits on success, where the rest
  of the chainstate already wrote.

### The command-only messages are btclib's

- **`getaddr`, `mempool`, `sendheaders` and `wtxidrelay` are
  `btclib.p2p.negotiation`'s `GetAddr`, `Mempool`, `SendHeaders` and
  `WtxidRelay`, and `btclib_node/p2p/messages/empty.py` goes with the
  copies it held** (#196). btclib defines the wire format these commands
  travel under, so a codec for them kept here is one this tree has to
  hold in step with a format it does not own.

- **`btclib_node/p2p/messages/` defines BIP61's `reject` alone.**
  Bitcoin Core's `NetMsgType` has no entry for that command and
  `btclib.p2p` carries no codec for it. `btclib.p2p.negotiation`
  declares `FeeFilter` beside the four taken here. Neither `feefilter`
  nor `mempool` is a key in this node's dispatch tables, and #94 is
  where the first is wired up.

### `REVIEWING.md` is the organization's copy

- **`REVIEWING.md`'s *The gates are the evidence* excepts no gate from
  the run a reviewer may rely on, the test suite included.** The
  organization's copy, shared half byte for byte (section 14): a run is
  whole whoever makes it — never a module on its own, a `-k`, a `--lf`,
  a deselect or a marker in its place — and one that was narrowed or cut
  short is reported as no run (btclib-org/.github#168).

- **`REVIEWING.md` is the organization's copy.** A review reads the prose
  that stays in the tree, treats a commit message or a pull request's
  body as a finding only where it decides something, and asks a stated
  count, a measurement nothing re-derives, or the history of the code
  told in a comment to go — section 14 of the standard, the shared half
  byte for byte.

### `enable_error_code` holds only codes that need enabling

- **`comparison-overlap`, `import-not-found` and `import-untyped` are
  not added to `enable_error_code`, and `narrowed-type-not-subtype`
  leaves it: mypy has each of them on already, so an entry naming one
  buys no check** (#175). Each answers `True` to

  ```shell
  uv run --locked --no-default-groups --group lint --group test \
    python -c "import mypy.errorcodes as m; \
      print(m.error_codes['import-untyped'].default_enabled)"
  ```

  where every code the list still holds answers `False`. So under the
  mypy `uv.lock` pins, `files` is already checked under all of them, and
  the survey #175 asked for ends in there being nothing to enable rather
  than in a candidate enabled and measured at zero.

  Nothing changes about what `mypy` reports. What changes is that the
  list is shorter and its comment now states the condition an entry has
  to meet, which is what keeps these from being proposed again.

### `links.yml` asks lychee a question its flags let it answer

- **`--accept` is lychee's default range with `429` added, where it was
  `200,206,429`.** The flag replaces the default rather than adding to
  it, and `lychee --help` gives that default as `100..=103,200..=299`:
  the list passed here turned a `201` or a `204` into a dead link to add
  a `206` the default already covered, and a host that starts answering
  `204` to a HEAD would have turned a live link red without anybody
  touching the tree. The cell `tests/links_test.py` of btclib-org/.github
  reported on this repository against btclib-org/.github#110 goes from
  the backlog.

- **`links.yml` no longer passes `--cache`.** No step restored the cache
  file between runs, so the flag decided nothing across them, and it
  would decide nothing with the step added: the run is weekly and the
  cache age passed beside it was a day. Within one run lychee asks each
  URL once whatever the flag says — `lychee --offline` over this tree's
  globs reports the unique count beside the total, the same pair with
  the flag and without it. The cell `tests/links_test.py` reported
  on this repository against btclib-org/.github#111 goes from the
  backlog.

### The root files are the organization's, and the tree says which

- **`RELEASING.md` and `RELEASE_NOTES.md` are gone, and what the first
  said that is worth keeping is where a contributor reads.** Section 2
  of btclib-org/.github's README says a tier-2 repository carries neither
  (btclib-org/.github#150): the first opened *There is no release, and no
  machinery for one* in a tree with a release page on `v0.1.0`, and the
  second had nothing to be on top of, its one section reading *Nothing
  to act on*. What stays — that nothing is on the index and what anybody
  runs is a checkout of `main`, that a tag is signed and `tag-integrity`
  refuses one that is not, that `## Unreleased` becomes the version and
  a tag can be deleted and re-cut only while nothing is published — is
  `CONTRIBUTING.md`'s *A version, and no release*, under *This repository
  in particular*. The list of what a release waits on is not carried
  over: section 2 weighed a procedure kept ready for a release that may
  come and decided that it arrives with `release.yml` the day it does.
  `README.md`'s pointer to the release notes is a line saying there is
  no release, `REPOSITORY.md`'s *No publishing* bullet cites that section
  where it cited `RELEASING.md`, and this file's introduction stops
  naming a record it is behind. The entry below that lists the two files
  among those arriving describes the tree between that landing and this
  one. `.gitattributes` keeps `RELEASE_NOTES.md merge=union`: section 14
  of the standard owes the two `merge=union` entries to every copy, and
  an attribute on a path the tree does not hold is inert.

- **`CONTRIBUTING.md` and `REVIEWING.md` are the same file as every
  sibling's down to `## This repository in particular`.** Section 14 of
  btclib-org/.github is what says so and `tests/verbatim_test.py` there
  is what compares the bytes. What each said in this tree's own words
  about rules that are the organization's — the tracker, the prose
  style, the pull request, the review, the landing — is gone, a second
  wording being the copy that goes stale; what is under the marker is
  what only this tree can say.

- **`CODE_OF_CONDUCT.md`, `AUTHORS.md`, `SECURITY.md`, `REPOSITORY.md`,
  `RELEASING.md`, `CHANGELOG.md` and `RELEASE_NOTES.md` arrive**, which
  is most of issue #34. `AUTHORS.md` points at this repository's own
  contributor graph rather than a sibling's. `SECURITY.md` says what is
  this node's to answer for as against btclib's and Bitcoin Core's, and
  gives an address because private vulnerability reporting is off here
  (#136). `REPOSITORY.md` is every setting read back from the endpoint,
  including the one that says no status check is required (#88 of
  btclib-org/.github).

- **`.markdownlint.jsonc`, `.yamllint.yaml`, `.taplo.toml` and
  `LICENSE` are byte-for-byte the organization's copies**, this having
  been the one repository where the three configurations still differed.
  `.yamllint.yaml` is the one that changes behaviour: it extends the
  default set where this copy listed two rules and extended nothing, so
  indentation, trailing whitespace and duplicate keys were unchecked
  under a gate that passed for having nothing to check, and
  `document-start` moves from a warning the hook exits 0 on to an error.
  No tracked yaml file here trips the wider set. `LICENSE` gains its
  `MIT License` title and loses the year range that `COPYRIGHT` has
  never carried.

- **`CLAUDE.md` holds what no document written for a human can** — the
  architecture, the worktree rule, the model, the facts that otherwise
  cost a session. The environment and the gates moved to
  `CONTRIBUTING.md`'s last section, a human having no reason to open an
  agent's file to learn how to run one.

- **`.gitattributes` marks the two new record files `merge=union`.** It
  was empty; without those lines every branch appending a bullet would
  conflict on the insertion point, which is a conflict with nothing to
  decide.

- **`.gitattributes` is byte-for-byte the organization's copy.** The
  attributes it sets do not change: `git check-attr merge
  CHANGELOG.md RELEASE_NOTES.md` answers `union` for both before and
  after; what changed is the comment above them, which had been reworded
  here and did not point at the section of the standard that records
  the rule. Section 14 of btclib-org/.github is what makes the file one
  of those compared, and btclib-org/.github#102 is the sweep.

- **`README.md` points at those files rather than repeating them.** It
  carried the install, test and lint commands, which are now
  `CONTRIBUTING.md`'s last section: two copies of a command are two
  things to keep in step, and the one a contributor reads is the one CI
  runs.

### `.pydeps` and the dependency that read it are gone

- **Nothing ran `pydeps`.** The configuration file was in the tree and
  the package was in the `test` dependency group, and no hook, workflow,
  script or test invoked either — `grep -rn pydeps` named the two of
  them and nothing else. A tool that only a person can remember to run
  is not a gate, and its configuration is a file a reader has to account
  for.

### `macos.yml` is `os-macos.yml`

- **The platform sweeps carry a prefix that groups them.** The file is
  renamed, its `name:` key with it; the job name is untouched, a check
  being keyed by name alone and bound outside the tree. Nothing in the
  repository refers to the old path.

### `scripts/` was in the mypy gate's own configuration, and never in the gate

- **The pre-commit hook passed `btclib_node tests` on its command line,
  which overrides `[tool.mypy]`'s `files` rather than agreeing with
  it** — mypy reads `files` only when given no paths of its own. `files`
  named `scripts` and said so in a comment; the gate never checked it,
  the same shape of defect `.yamllint.yaml`'s entry above records for a
  config that extended nothing. The hook now passes no paths, so `files`
  is the one list both a bare `mypy` and the gate read, and cannot drift
  from each other again. The stale per-flag error counts the same
  `[tool.mypy]` block carried are replaced with the command that
  re-derives them: a count is a line every branch touching that block
  has to keep true, and this one had already gone false.

### The build backend is `uv_build`, and what the sdist carries is declared

- **`setuptools.build_meta` becomes `uv_build`**, pinned
  `>=0.12.5,<0.13`; `[tool.setuptools.packages.find]` goes with it, and
  `module-root = ""` is what now says the package sits at the root.
  btclib-org/.github#118 is where a pure-Python project was decided onto
  that backend, and btclib is the tree the pin comes from. The property
  the floor is about — the sdist's own `pyproject.toml` being a
  normalized copy with the verbatim one beside it as
  `pyproject.toml.orig` — arrives in `0.12.0`, so the pin sits above that
  boundary rather than on it: `0.12.5` is the uv this tree is gated
  against, being the rev `.pre-commit-config.yaml` pins for
  `uv-pre-commit`. `[build-system]` carries the command that locates the
  boundary, and why `uv build` is not it.

- **What the sdist holds is stated for the first time.** There was no
  `MANIFEST.in`, so the archive was whatever setuptools defaulted to and
  nothing had declared it — and what it left out was the suite, and every
  configuration file of the lint gate that is a file of its own.
  `pyproject.toml` went, so the tools reading their settings from it were
  served and the hooks keeping a file of their own were not.
  `[tool.uv.build-backend]` `source-include` now names the root prose,
  `scripts/`, `tests/`, `uv.lock`, `.python-version` and the
  configuration each hook keeps. `.github/` is left out as a decision and
  not because anything refuses it: adding `.github/**` ships the
  directory and `check-sdist` still reports the archive as matching git,
  that tool's default ignore list suppressing a report rather than
  governing what is packed.

- **The archive is not a tree the gate runs on**, which the include list
  raises and does not settle: `git init` in an unpacked sdist followed by
  `pre-commit run --all-files` exits 1 at `check-hooks-apply`, and that
  run names the hooks left with no file to read. Shipping `.github/` does
  not change the verdict — the same run still exits 1, on the hooks whose
  files are the workspaces `[tool.check-sdist]` keeps back.

### The review check is red on anything but an ack of its head

- **`claude-review.yml` reads the verdict the review posted, and fails
  on anything but an `ACK` naming the pull request's head.** The one
  guard it had tested whether the action had started, and that was the
  whole of the check's colour: on pull request 164 the run for
  `4da7fba`, a sha the review answered `CHANGES REQUESTED`, concluded
  `success`. The step is btclib-org/.github's, taken from its
  `claude-review.yml` at `18e6c64` with the comment that carries its
  measurement; btclib-org/.github#146 is the finding across the
  organization. Still not a required check, for the reason the file's
  header gives.

### Two root files are the organization's, and no longer this tree's

- **`CODE_OF_CONDUCT.md` is gone.** It pointed at the PSF code of
  conduct, as the one copy in btclib-org/.github does, and GitHub shows
  that copy for a public repository that has none of its own: a copy per
  repository is a copy of a pointer, which is why section 14 of the
  standard no longer lists the file (btclib-org/.github#123).

- **`SECURITY.md` is gone, and its *Limitations* are in `README.md`.**
  The policy a tree keeps is the one that travels in its sdist, and this
  repository publishes nothing for one to travel with, so what GitHub
  shows is the organization's (btclib-org/.github#116). What that policy
  cannot say for this node — the JSON-RPC listener on `0.0.0.0`
  authenticating nothing, #27, and what a peer may ask for not being
  bounded by what asking costs it, #101 — is under its own heading in
  `README.md`, which is where somebody about to run the node reads.
  `RELEASING.md`, `REPOSITORY.md` and the issue template's contact link
  pointed at the file, and now point at the policy shown here.

- **The wheel carries the same members**, less `top_level.txt`, which is
  setuptools' own legacy metadata rather than something the wheel
  specification asks for. The member *lists* are what is unchanged; the
  metadata files themselves are rewritten by the new generator, gaining
  a field as well as losing one — `METADATA` gains an `Author:` beside
  the `Author-email:` setuptools emitted alone, drops the `Dynamic:
  license-file` setuptools added, and reorders the rest, while `WHEEL`
  names `uv` where it named `setuptools` and `RECORD` is reordered. A
  `diff` over the two unpacked `dist-info` directories is what shows it,
  and no summary here stands in for that. What is unchanged is what the
  wheel installs: every `.py` is byte-identical, `RECORD` giving each the
  same hash and length as before. To re-derive either archive, build in a
  checkout of the commit before this one and in one of this commit:

  ```shell
  uv build -o dist                                   # in each checkout
  diff <(tar tzf <old>/dist/*.tar.gz | sed 's|^[^/]*/||;s|/$||' | sort -u) \
       <(tar tzf <new>/dist/*.tar.gz | sed 's|^[^/]*/||;s|/$||' | sort -u)
  diff <(unzip -Z1 <old>/dist/*.whl | grep -v '/$' | sort) \
       <(unzip -Z1 <new>/dist/*.whl | grep -v '/$' | sort)
  ```

  The directory members are dropped there because the two backends record
  them differently: setuptools ends an sdist's with a slash where the uv
  backend does not, and the uv backend writes them into the wheel where
  setuptools writes none. That is a difference in how the archive is
  recorded and not in what it carries.

- **`check-sdist` joins the lint gate**, which is the first thing here to
  ask what is tracked and not in the archive, and what is in the archive
  and not tracked. It reads `[tool.uv.build-backend]` for the uv backend,
  so an exclusion there is not reported again; `[tool.check-sdist]`
  `git-only` holds what no include pattern adds — the workspaces of an
  editor and of an agent, the files git reads, and `COPYRIGHT`, which
  btclib-org/.github#135 argues is a repository file rather than a
  distributed one.

### Every signature in `btclib_node`, `tests` and `scripts` is annotated

- **`disallow_untyped_defs`, `disallow_untyped_calls` and
  `disallow_incomplete_defs` join `[tool.mypy]`'s enabled bundle, and
  `explicit-override` joins `enable_error_code`** — the four #104 was
  the annotation pass for, each measured at zero over `files` before
  being turned on. Annotating a signature is what most of it took;
  where a test built a `SimpleNamespace` in place of a real class, the
  fix is one of three named patterns and not a fourth invented per
  call site — `cast` where a production constructor is handed the
  double, `Any` where the double is never handed to one, a local
  `Protocol` where several callers share a narrower shape than the
  real class has. Annotating `btclib_node/db.py`'s `_rows` surfaced
  that its return type was narrower than the `PRAGMA` queries already
  run through it; `Config.log_path` was declared `str` where its own
  `__init__` treats a falsy value as "no file logging" and every
  `scripts/chains/*.py` script relies on exactly that. Both are
  widened rather than worked around. `no_implicit_reexport` and
  `check_untyped_defs` are untouched by this pass and stay off for the
  reasons `[tool.mypy]`'s own comments give.

### An RPC connection is forgotten once its answer is sent

- **`RpcManager.connections` no longer grows for the life of the node**
  (#64). Every request opened a connection, `async_send` answered it and
  closed its socket, and the entry stayed in the dict anyway: measured
  on `origin/main` before this change, a run of eleven requests against
  a running node left `len(connections)` at 11 and climbing, never
  smaller. The removal was already written into `rpc/main.py`, commented
  out at the end of `handle_rpc`; restoring it is the whole of the fix,
  placed after the answer is sent on both paths -- after `send_and_wait`
  for `stop`, after `send` otherwise.
- **`RpcManager.remove_connection` is deleted rather than repaired.** It
  called `.stop()` on a `Connection`, which has no such method (`close()`
  is what it defines), and nothing had ever called it. Wiring it up in
  `handle_rpc`'s place would have called `Connection.close()` -- which
  closes the socket synchronously, from `Node`'s own thread -- while the
  still-scheduled `async_send` on `RpcManager`'s event loop thread can be
  mid-write on the fire-and-forget `send()` path that never waits for
  it: a cross-thread race that can cut an answer off before it reaches
  the client. The restored line only forgets the dict entry; the socket
  is closed by `async_send` itself, on the thread already writing to it.
  `btclib_node/p2p/manager.py` keeps its own `remove_connection`, which
  is safe for the same reason this one was not: it is only ever called
  from inside its own manager's event loop, not across threads.
- The RPC port binds every interface with no authentication (#27), so
  this was a leak any client could drive, not only an internal
  bookkeeping detail.

### `no_implicit_reexport` is on: every import names where a name is defined

- **`TxIn`, `TxOut` and `OutPoint` were imported from a module that only
  passes them through.** `TxIn` and `TxOut` came from `btclib.tx.tx`,
  which imports them for its own use and defines neither; `OutPoint`
  came from `btclib.tx.tx_in` the same way. `btclib` already answers
  this correctly at its package boundary -- `btclib.tx`'s own
  `__init__.py` re-exports all three explicitly, `__all__` and all --
  the gap was entirely on this side, every affected import redirected
  to where each name is actually defined: `OutPoint` in
  `btclib.tx.out_point`, `TxIn` in `btclib.tx.tx_in`, `TxOut` in
  `btclib.tx.tx_out`. `uv run --locked --no-default-groups --group lint
  --group test mypy --strict` is clean where it was not before.
- **`no_implicit_reexport` moves into the enabled bundle** -- the last
  of the flags `[tool.mypy]`'s comments had left off pending this fix,
  `check_untyped_defs` (#105) being the one still open.

### `[tool.mypy]` is `strict = true`

- **The itemized strict bundle is gone.** `[tool.mypy]` used to
  enumerate every flag `mypy --help`'s `--strict` entry bundles, one at
  a time, with the case against turning it on wholesale beside it: the
  organization's standard now requires `strict = true` outright, with
  no trajectory-toward-it exception (btclib-org/.github#112), which
  makes the itemized shape a non-conformance rather than a deferred
  step of #30. `warn_unused_configs`, `strict_bytes` and
  `warn_unreachable` are not in that bundle and stay their own lines.
- **`check_untyped_defs`, the one bundled flag the table still left
  off, measures at zero and closes #105**: `uv run --locked
  --no-default-groups --group lint --group test mypy
  --check-untyped-defs` reports no issues in 82 source files, #104's
  annotation pass having left nothing in `files` for it to check.
- `[project.classifiers]`'s reason for omitting `Typing :: Typed` no
  longer cites mypy not being strict, and the `lint` dependency group's
  comment above `[tool.mypy]` points at `strict = true` in place of the
  per-flag measurement it used to describe -- both assumed the itemized
  shape this removes.

### `getrawmempool`'s verbose output no longer misspells its weight key

- **The JSON-RPC key a client reads for a mempool transaction's weight
  is now `weight`** (#28), matching Bitcoin Core's own field name
  instead of the misspelling `get_raw_mempool` had carried in
  `btclib_node/rpc/callbacks.py`. A straight rename, with no one
  release answering to both keys: `btclib_node/rpc/callbacks.py`'s
  `callbacks` dict still has no `getrawtransaction` or `getblockcount`
  entry, which is what #21 is tracking, so btclib's `BitcoinCoreFetcher`
  cannot address this node's JSON-RPC surface yet, and there is no known
  client of it to carry across a two-key transition.
- **The spell checkers' suppressions for the misspelling are gone**,
  `[tool.codespell]`'s `ignore-words-list` entry and
  `[tool.typos.default.extend-words]`'s entry alike, each with the
  comment that justified it: a spell checker ignoring a word no longer
  in the tree has nothing left to ignore.

### `getblockheader` describes a block off the best chain, instead of failing

- **A block this node has indexed and did not follow is answered with
  its header** (#87). `get_block_header` read the height as
  `header_index.index(block_hash)`, and `header_index` holds the best
  chain alone: for a block on a losing fork -- indexed, and described by
  `block_index.header_dict` perfectly well -- that raised
  `ValueError: list.index(x): x not in list`, which `rpc/main.py`'s
  `handle_rpc` answers as `-32603 Internal Error`. Measured on a regtest
  index carrying three headers and a one-header fork off the genesis, on
  `origin/main` and here: the raise becomes `height: 1`,
  `confirmations: -1`, the fork's parent as `previousblockhash`, and no
  `nextblockhash`. A client can now tell a block the chain did not keep
  from a node that has broken, `-32603` being what this node owes a
  genuine fault.
- **The shape is Bitcoin Core's.** `blockheaderToJSON` and
  `ComputeNextBlockAndDepth` in its `src/rpc/blockchain.cpp` answer a
  header off the active chain with `confirmations: -1` in place of a
  depth and with no `nextblockhash`, and with the height the block has
  on its own fork. What Core counts that depth against is the chain of
  blocks it has validated, where this stays with the best header chain
  the function already used: #178 is that difference, which predates
  this entry and is not what #87 asked about.
- **The height is `BlockInfo.index`**, which every indexed block carries,
  rather than a position in a chain a fork is not on. Nothing changes for
  a block on the best chain: the two are the same number there, which is
  what `tests/functional/rpc/test_chain.py` asserts of a height read back
  over a real index built with `add_headers`.
  `previousblockhash` likewise comes from the header's own parent, which
  for a block on the best chain is the `header_index[height - 1]` it was
  read from before.
- **A hash this node has never seen still raises**, and so do a non-hex
  parameter and no parameter at all: #179 is those three, which want a
  way for a callback to name an error code and are the same mechanism
  #83 is waiting on.

### `pytest --help` prints the usage message instead of a traceback

- **`uv run pytest --help` exited 1 with a `TypeError` out of
  `tests/conftest.py` and printed nothing at all** (#154). The coverage
  floor's `asks_for_everything` read `config.option.file_or_dir` as a
  list, and on the `--help` path it is `None`, the parse having been
  abandoned rather than left unfinished: `--help` is bound to pytest's
  `HelpAction`, which raises `PrintHelp` to skip the rest of argument
  parsing, and `Config.parse` catches it and returns before the
  positional is consumed, so it still holds argparse's `None` default
  when `helpconfig` calls `_do_configure()` itself and the hook fires.
  Folding it to no paths is the fix, and is what the function already
  means by no path — the run is not selective, so the floor is left
  where it is, which is right for a run that collects nothing. It is
  `--help` alone and not the class of run-nothing options: on an
  `origin/main` snapshot `--markers` and `--fixtures` each exited 0 and
  `--co -q tests/helpers.py` exited 5, each reaching the hook with a
  list. No gate types `--help`, which is why nothing caught it.
- **The guard is a regression test's job, because the coverage floor
  cannot see it.** `tests/unit/coverage_floor.py` builds the option
  namespace by keyword, and now takes `file_or_dir=None` as well as a
  sequence, so `a_config(file_or_dir=None)` asks exactly what `--help`
  asks and asserts the floor is left alone rather than that nothing
  raised. With the `or []` taken back out that one test fails with the
  `TypeError` above, and nothing else does.
- **An `or` short-circuit is invisible to branch coverage, whatever the
  layout.** coverage.py records a branch as an arc between two line
  numbers, and both outcomes of an `or` leave the same line, so no arc
  distinguishes them. Spreading the expression over several lines does
  not help: `coverage.parser.PythonParser.arcs()` gives that form the
  same single exit, from the line the statement starts on. So
  `branch = true` and `fail_under = 100` would not have demanded this
  test: with it deleted, `uv run pytest` stays green and
  `tests/conftest.py` drops out of the report as fully covered. A fix of
  this shape has to be tested deliberately rather than left for the
  floor to ask.

### `getpeerinfo` brackets an IPv6 host the way Core does

- **A peer on IPv6 is reported as `[2001:db8::1]:8333`** (#147). Run
  against a snapshot of `55c5512`, `get_peer_info` answered
  `2001:db8::1:8333` for a peer at that address on that port, where the
  host and the port cannot be told apart: `2001:db8::1:8333` is itself
  an address, so a client splitting on the last colon reads one of the
  two wrong. `addr`, `addrbind` and `addrlocal` were each written that
  way, and each now carries the brackets.
- **The rule is `CService::ToStringAddrPort`'s**, in Core's
  `src/netaddress.cpp`, which writes
  `"[" + ToStringAddr() + "]:" + port_str` for every network its
  `IsIPv4() || IsTor() || IsI2P() || IsInternal()` does not name.
  Core's own `getpeerinfo` puts each of those fields through it:
  `src/rpc/net.cpp` writes `addrbind` from it, `addr` from
  `m_addr_name`, which `src/net.cpp` sets to it where no name was
  dialled, and `addrlocal` from the string `CopyStats` builds with it.
  This node has no onion or i2p socket to read a peer from, so IPv4 is
  the whole of what its own answer leaves unbracketed.
- **A v4-mapped host is unwrapped rather than bracketed**, so a v4 peer
  reads `1.2.3.4:8333`. That is Core's answer too, `SetLegacyIPv6`
  filing a mapped address under NET_IPV4, and it is what a
  `NetworkAddress` needs, holding every address in the sixteen octets
  of an IPv6 one. Whether a BIP155 record spelled that way should be
  kept at all, and what a peer reads it back as, is a different
  question, and #151 is it.
- **`btclib_node/p2p/address.py`'s `ip_and_port` takes a host's text
  and a port**, which is what lets the fields share it: `getpeername`
  and `getsockname` answer with a tuple and have no `NetworkAddress` to
  offer. A host that is not an IP address is refused rather than shown
  with brackets guessed at, `ipaddress.ip_address` being what refuses.

### `getblockheader`'s confirmations, and the code a bad request is owed

- **A header this node has accepted and not downloaded was reported
  confirmed** (#178). `get_block_header` measured `confirmations`
  against `block_index.header_index`, the best *header* chain, which
  holds a hash as soon as its header is accepted; `active_chain` holds
  only what the node has validated and connected. On a snapshot of
  `origin/main` at `f53d9cb`, three regtest headers added and nothing
  downloaded answer `height: 3 confirmations: 1 downloaded: False` for
  the last of them. `get_block_header` now counts against
  `active_chain`, and the same setup answers `confirmations: -1`.
- **The rule is `ComputeNextBlockAndDepth`'s**, in Core's
  `src/rpc/blockchain.cpp`, which counts a depth from
  `chainman.ActiveChain().Tip()` and answers `-1` for a block that chain
  does not hold at its own height. `height` stays `BlockInfo.index`,
  which every indexed header carries; only what it is compared against
  moved.
- **A hash nothing indexed, a hash that is not hex, and no hash at all
  each raised out of the callback and were answered `-32603 Internal
  Error`** (#179), the code this node owes its own fault and not a
  client's mistake. On the same snapshot: a hash nothing indexed raises
  `KeyError`, a non-hex hash raises `ValueError`, no parameter raises
  `IndexError`, and each reaches `rpc/main.py`'s catch-all.
- **`btclib_node/rpc/errors.py`'s `RpcError` is the mechanism #83
  asked for**: a callback names the code and the message its refusal is
  owed, and `handle_rpc` answers with them rather than logging the
  request as a fault of the node's. `get_block_header` raises it for the
  three cases above, with the codes Core gives the same three refusals:
  `RPC_INVALID_ADDRESS_OR_KEY` (`-5`) for a hash `LookupBlockIndex`
  does not have, `RPC_INVALID_PARAMETER` (`-8`) for one `ParseHashV`
  cannot read as hex, and `RPC_MISC_ERROR` (`-1`) for a call short of
  its required argument, which is what `RPCMethod::HandleRequest`
  throws and `ExecuteCommand`'s `catch (const std::exception&)` turns
  into. `#83`'s other half, `sendrawtransaction`, does not raise it yet.
- **`error_msg` takes the id of the request it answers.** A method not
  in `callbacks` used to answer `"id": null` regardless of what
  `is_valid_rpc` had already read from the request; JSON-RPC 2.0's
  section 5 reserves `null` for a request whose id could not be read at
  all, not for one `is_valid_rpc` already confirmed carried one.

### `getblockheader`'s two parameters, checked and read the way Core's are

- **A `blockhash` that is not a string used to reach `bytes.fromhex`
  unguarded** (#212). `bytes.fromhex(5)` raises `TypeError`, which the
  `except ValueError` around that call did not catch, so it fell through
  to `rpc/main.py`'s catch-all and answered `-32603 Internal Error` — the
  code this node owes its own fault, for a request that named the wrong
  JSON type. `RPCMethod::HandleRequest` checks a declared argument's
  type before the handler body runs at all
  (`src/rpc/util.cpp:653-661`), so Core never reaches `ParseHashV` for
  such a call either; it answers `RPC_TYPE_ERROR` (`-3`), which
  `btclib_node/rpc/errors.py`'s `RpcErrorCode` now names and
  `get_block_header` raises for a `blockhash` of any type but `str`.
- **`get_block_header` never read a second parameter** (#215). Core's
  `getblockheader` takes an optional `verbose`
  (`RPCArg::Type::BOOL, RPCArg::Default{true}`, `src/rpc/blockchain.cpp
  :617`): true answers the JSON object this node already built, false
  answers the header's own eighty bytes, hex-encoded
  (`src/rpc/blockchain.cpp:668-673`). `get_block_header` now reads
  `params[1]`, defaults it to `True` where omitted or `null`, and
  answers `header.serialize().hex()` for a false one — the same bytes
  `BlockHeader.serialize()` puts on the wire, hex-encoded rather than
  built into the object.
- **`verbose` is type-checked the same way `blockhash` is.** A `verbose`
  of any JSON type but `bool` (or `null`, which stands for the default)
  is the same `RPC_TYPE_ERROR` `blockhash` is refused with, against
  `RPCArg::Type::BOOL`.
- **`errors.py`'s `json_type_name` names the six JSON types the way
  Core's own `uvTypeName` does** (`src/univalue/lib/univalue.cpp`),
  which is the vocabulary `RPC_TYPE_ERROR`'s message speaks and what
  both checks above report the wrong type with.

### The issue template's security link is the advisory form, not the policy

- **`.github/ISSUE_TEMPLATE/config.yml`'s *Security vulnerability* entry
  links `/security/advisories/new`, where it linked `/security/policy`**
  (#136). Private vulnerability reporting is on for this repository, so
  the form the link now opens exists; `REPOSITORY.md` records the
  setting and points here rather than repeating it. This repository
  still keeps no `SECURITY.md` of its own: the route the setting opens
  reopens no question #167 already settled about which repositories
  carry one.

### The review workflow reads more, and reviews more pull requests

- **`claude-review.yml`'s `--allowedTools` now carries `Bash(git:*)`,
  `Bash(gh issue view:*)` and `Bash(gh api:*)`** (#153). `gh pr diff` was
  the job's only avenue before this: no base to check a "what this did
  before" claim against, and no way to read the issues a pull request
  says it closes.

- **The review step now names `allowed_bots: "dependabot[bot]"`**
  (#168). `.github/dependabot.yml` opens a pull request here every
  Thursday, and without this the action throws `Workflow initiated by
  non-human actor` for that actor rather than reviewing it.

- **The header no longer calls this the *only* workflow** (#140).
  `lint.yml` and `test.yml` already run on the same pull request and are
  required checks on `main`; this one deliberately is not.

### The merge gate no longer resolves live DNS to pass

- **The three `@pytest.mark.remote_data` tests in
  `tests/unit/p2p/address.py` that called `PeerDB.get_addr_from_dns`
  against `Main`, `TestNet` and `SigNet`'s real bootstrap seeds are gone**
  (#135). `pyproject.toml`'s `addopts` carries `--remote-data=any`, which
  forced them into every coverage run test.yml gates a pull request on,
  so a DNS hiccup or a seed going away turned that gate red on a change
  that touched neither. `get_addr_from_dns`'s own logic — the union over
  several seeds, the dedup, the `gaierror` skip, an IPv6 answer's host
  and port — is still exercised, deterministically, by the stubbed-loop
  tests already in the same file.
- **`.github/workflows/bootstrap-dns.yml` asks the three real chains'
  seeds the same question the removed tests did**, for the same reason
  `links.yml` is not a merge gate: a host having a bad afternoon is a
  thing to re-run, not a thing a pull request should have to fix. It
  carries no `schedule:` yet — the organization's calendar in section 10
  of `btclib-org/.github`'s README has no row for a workflow of this
  shape, and btclib-org/.github#201 is where one is asked for — so it
  runs by hand (`workflow_dispatch`) and on a change to itself until a
  row exists.

### A listener that cannot bind now says so, and a refused dial costs microseconds

- **A P2P or RPC listener whose bind fails now ends the manager's
  thread with the `OSError`, instead of leaving it `is_alive()` over a
  socket that never came up** (#88). Both managers scheduled `server`
  through `run_coroutine_threadsafe`, whose returned
  `concurrent.futures.Future` nobody read; a bind failure inside that
  coroutine sat in the unread future while the thread ran on. `_bind`
  now runs synchronously in `run`, before `run_forever`, so the same
  failure raises out of `run` itself and is logged. Also closes the
  socket `_bind` had already opened when the bind or the listen after it
  fails, which used to leak the file descriptor for the same reason the
  exception vanished.
- **A refused dial no longer costs the second a ten-pass, 0.1s poll
  always charged it, refused or merely slow alike** (#90).
  `btclib_node/p2p/address.py`'s `dial` reads the kernel's own answer
  through `loop.sock_connect`, which watches the socket become writable
  and checks `SO_ERROR` the moment it does, wrapped in
  `asyncio.wait_for` for the one real timeout the poll's two magic
  numbers stood in for.
- **A dial that connects without ever raising `BlockingIOError` — a
  local peer, most often — no longer leaks its socket** (#148): the
  hand-rolled `except BlockingIOError:` arm that was the only place a
  successful dial got returned or a failed one got closed is gone
  along with the poll it guarded, so there is no longer an arm the
  immediate-success case can fail to reach.

### `getrawmempool`'s two parameters, checked and read the way Core's are

- **`verbose` was read with `params[0] if params else False` and never
  type-checked** (#219), the same shape #212 named on `getblockheader`'s
  `blockhash`. `RPCMethod::HandleRequest` (`src/rpc/util.cpp:653-661`)
  checks a declared argument's type before the handler body runs;
  `verbose` and `mempool_sequence` are both declared
  `RPCArg::Type::BOOL` (`src/rpc/mempool.cpp:694-695`).
  `btclib_node/rpc/errors.py`'s `bool_param` reads a declared bool
  argument the same way `get_block_header`'s own `verbose` check does,
  raising `RPC_TYPE_ERROR` for anything else; `get_block_header` now
  calls it too, in place of the check it carried on its own.
- **The default answer, `verbose` and `mempool_sequence` both false, was
  `{"txids": [...]}`, not the plain array `MempoolToJSON` answers with
  in that case** (`src/rpc/mempool.cpp:624-634`). `get_raw_mempool` now
  answers a bare list of hex txids there, and reserves the object shape
  for where `mempool_sequence` is true.
- **`mempool_sequence` was never read at all** (#219). It now attaches
  `Mempool`'s own running count of the transactions it has added or
  removed, under the key `mempool_sequence`, next to `txids`
  (`src/rpc/mempool.cpp:635-639`). `Mempool` gained a `sequence` field,
  starting at `1` and bumped once in `add_tx` and once in `remove_tx`, on
  the same branch that already guards each against a no-op — Core's own
  `m_sequence_number` starts at `1`, not `0` (`src/txmempool.h:202`), and
  is "incremented once every time a transaction is added or removed from
  the mempool for any reason" (`:200-202`), so a fresh mempool with no
  events answers `mempool_sequence: 1`, not `0`, and a duplicate add or
  an absent remove is neither an addition nor a removal.
- **`verbose` and `mempool_sequence` both true is refused**, matching
  `MempoolToJSON`'s own `RPC_INVALID_PARAMETER` for the combination
  (`src/rpc/mempool.cpp:608-611`), rather than silently answering one
  and dropping the other.

### `getblockhash`'s height, checked and bounded the way Core's is

- **A negative height no longer reads the active chain from its own
  end** (#234). `active_chain[int(params[0])]` handed Python's own list
  indexing a negative number, which counts from the end rather than
  raising, so `getblockhash` with `-1` silently answered the tip's hash
  — a wrong answer, not an error. Core refuses `nHeight < 0` outright
  (`src/rpc/blockchain.cpp:599-601`), the same `RPC_INVALID_PARAMETER`,
  `"Block height out of range"`, a height past the tip already got.
- **A height of the wrong JSON type, or none at all, used to reach
  `int()` unguarded** (#234), the same shape #212 and #219 named on
  `getblockheader`'s `blockhash` and `getrawmempool`'s `verbose`:
  `int(None)` raises `TypeError`, `int("x")` raises `ValueError`, and an
  empty `params` raises `IndexError`, none of them caught, all three
  reaching `-32603 Internal Error`. `height` is declared
  `RPCArg::Type::NUM` (`src/rpc/blockchain.cpp:585`); a JSON value of
  any other type is now `RPC_TYPE_ERROR`, the same check
  `RPCMethod::HandleRequest` makes before its own handler runs
  (`src/rpc/util.cpp:653-661`), and an omitted height is
  `RPC_MISC_ERROR` with the method's own usage — `getblockhash height`,
  unquoted, unlike `getblockheader`'s own quoted `"blockhash"`:
  `RPCArg::ToString(oneline=true)` quotes an argument's name only for
  `Type::STR`/`STR_HEX`, and `height` is `Type::NUM`
  (`src/rpc/util.cpp:1265-1286`).
- **A height written as a JSON number with a decimal point is
  `RPC_MISC_ERROR`, not silently truncated.** `int(1.5)` truncates
  toward zero without complaint; `UniValue::getInt<int>()` fails on any
  such literal regardless of its value, and the `std::runtime_error` it
  throws is `ExecuteCommand`'s generic `catch (const std::exception&)`
  case, answered `RPC_MISC_ERROR` and not `RPC_TYPE_ERROR`
  (`src/rpc/server.cpp:884-886`).

### The review prompt is told the checkout it runs against is shallow

- **`claude-review.yml`'s prompt now names the checkout's `fetch-depth:
  1` and says what to do about it** (#222). `--allowedTools` has carried
  `Bash(git:*)` since #153, for checking a claim about what the tree
  looked like before a diff, but a depth-1 checkout of a pull request's
  merge commit carries no parent history for `git log` or `git diff` to
  walk, and nothing told the model so. The prompt now points a
  single-file check at `gh api repos/<repo>/contents/<path>?ref=<sha>`,
  which answers regardless of the checkout's depth, and a real range at
  `git fetch origin <base ref>` first — the base ref now passed in the
  prompt header alongside `REPO` and `PR NUMBER` — rather than raising
  `fetch-depth` for every run whether a review needs the history or not.
