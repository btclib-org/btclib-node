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

### `select` gains `SIM`, `RET` and `FURB`

- **`select` gains `SIM` (flake8-simplify), `RET` (flake8-return) and
  `FURB` (refurb)** (issue #284).
- **`Node.run`'s `math.log(x, 2)` calls, over how many queued RPC or
  peer-to-peer messages one pass of the main loop takes, become
  `math.log2(x)`**, the redundant-base form `FURB163` names — a rate
  limit, not anything consensus- or network-facing. Checked the two
  forms are numerically interchangeable for every non-negative integer
  input these call sites can reach, exact powers of two included,
  before taking the rewrite rather than trusting it.
- **`update_chain` gains an explicit `return None` at the fall-through
  end of the function**, where every other exit is already explicit
  (`RET503`). Nothing before this point in the function's own
  `try`/`except`/`finally` is touched — the fall-through in question is
  the last statement in the function, well after that block ends.
- **A trivial `assign, then return` pair collapses to `return
  <expression>`** wherever `RET504` finds one, an `if`/`else` block
  that only chooses which value to assign becomes a ternary wherever
  `SIM108` finds one, a `try`/`except ...: pass` becomes
  `contextlib.suppress(...)` wherever `SIM105` finds one, and
  `is_valid_rpc`'s own last two lines become `return "id" in request`
  (`SIM103`). `rpc/main.py`'s own `SIM108` ternary rewrite of a
  dict-key-with-default `if`/`else` block turns out to still read as an
  `if` block to `SIM401` once it is a ternary, so that one goes one
  step further, to `request.get("params", [])`, rather than stopping
  at the ternary this round's own `select` addition first suggested.
- **`init_test.py`'s two nested `with` statements over the same test
  become one `with (...)`** (`SIM117`).
- **`init_test.py`'s `STOP_TIMEOUT < timeout` keeps the constant on the
  left, against `SIM300`, with a `noqa` and a reason**: `SIM300`'s own
  rationale — preventing an accidental `=` for `==` — does not apply to
  Python, and the constant's own comment states the claim this way
  round, the constant being the sentence's subject.

### `select` gains `PT` and `RUF043`, and bare raises carry a reason

- **`select` gains `PT` (flake8-pytest-style), and `extend-select` gains
  the single rule `RUF043`** (issue #284). `RUF043` travels with `PT`
  rather than waiting for the rest of `RUF`, because the fix for
  `PT011` is to give `pytest.raises` a `match=`, and a `match=` carrying
  an unescaped regex metacharacter is exactly what `RUF043` reports; the
  comment above `select` says the rest of `RUF` is its own round.
- **`Config.__init__`'s `raise ValueError` for an unrecognised `chain`,
  `check_transactions`'s `raise ValueError` for a prevout list that
  does not match its transaction's inputs, and the `raise Exception`
  in `UtxoIndex.add_block`, `UtxoIndex.apply_rev_block` and
  `BlockIndex.remove_from_active_chain` now carry a message.** Each
  raised bare before, so nothing told the failures within one function
  apart; `PT011` asked the tests here for a narrower catch, and a
  message the tests can `match=` is the honest answer where the
  exception itself stays what it already was. The `raise Exception`
  sites already carried a bare `noqa: B017, PT011` with no reason
  recorded, from before `PT` was selected; a message and a `match=`
  answer both rules at once — `B017` (still unselected) checks the
  same way `PT011` does — so the whole `noqa` comes off rather than
  narrowing to the half that is not yet enforced.
- **The `pytest.raises(RuntimeError | KeyboardInterrupt)` blocks in
  `tests/unit/db_test.py` that keep the write before the raise inside
  the block, against `PT012`, each carry a `noqa` explaining why:** the
  write has to land on the open batch before the exception that
  unwinds it, which is what the test is about, so it cannot move out
  of the block.
- **A composite `assert a and b` splits into two `assert`s wherever
  `PT018` finds one**, so a failure names which half failed.
- The long comment above `select` re-points its two citations of #25,
  closed in favour of #284 and #285: the general reference-`select`
  measurement and its suggested shape are #284's, and the `D`
  (pydocstyle) paragraph is #285's.

### `RpcManager.stop()` closes what `P2pManager.stop()` was already fixed to close

- **The listening socket is now closed explicitly in `stop()` rather
  than only through `server`'s own `with server_socket:`, and
  `server`'s own accept is wrapped in `asyncio.shield`** (closes #323).
  Mirrors `P2pManager.stop()`'s own fix for the same two races (#312):
  a coroutine `Task.cancel()` reaches before its first step raises
  `CancelledError` at its own definition point, so the `with` is never
  entered and the listening socket is never closed; and an accept
  already resolved at the instant of cancellation is lost past an
  unshielded `await`. `stop()` also requests every task's cancellation
  in its own pass before awaiting any one of them, as `P2pManager.stop()`
  does — here this closes log noise rather than a leak, since
  `RpcManager`'s own connections sweep already runs after that same
  loop and reaches a connection landed during it, where `P2pManager`'s
  sweep runs before. `RpcManager` has no `dial` or
  `manage_connections`, so #312's own third mechanism — an outbound
  connect losing its socket to the same unguarded `except` — has no
  counterpart here.

### `Connection.run` no longer takes a parameter it never reads

- **`Connection.run`'s own `connect` parameter is removed** (closes
  #318). Its body never read it, and no caller —
  `P2pManager.create_connection` or any test — ever passed it a
  non-default value.

### `P2pManager.stop()` leaves no socket behind

- **Every task is now cancelled before the loop is allowed to run again,
  and the sweep of the connection dicts and that cancellation repeat
  until `asyncio.all_tasks` answers with nothing** (closes #312).
  `run_until_complete` runs the loop, so a single pass of
  each left `server()`'s accept loop working through the drain: it took
  what the kernel had left in the listen backlog, and `create_connection`
  registered that connection after the sweep had passed and gave it a
  task the snapshot taken before the drain could not hold. Nothing closed
  that socket and nothing ended that task, so what a run saw was
  `Connection.run` pending at its own `sock_recv` beside an unclosed
  socket, reported against whichever test the collector reached them in.
- **`dial` closes the socket it opened when it is cancelled mid-connect**
  (issue #312). `CancelledError` is neither an `OSError` nor a
  `TimeoutError`, so the arm that answers a peer which never came up did
  not see it: `stop()` cancelling `manage_connections` while it was
  inside a dial left that socket open with a `laddr` and no `raddr`,
  which is the shape the issue was filed on.
- **`server()` accepts through a shielded task of its own**, so a
  connection the kernel handed over in the same instant the accept loop
  was cancelled is closed rather than dropped (issue #312).
  `Task.cancel` cannot cancel a future that is already done, so it throws
  `CancelledError` in on the next step instead of resuming with that
  result, and the accepted socket was held by nothing but the frame that
  unwound.
- **The explicit close of the listening sockets is no longer described as
  a backstop for a mechanism nobody could name**: `server`'s own `with
  server_socket:` is skipped where `stop` arrives before that task has
  taken a first step, which is the same fact the connections sweep turns
  on (issue #312).
- **`test_stop_closes_a_connection_accepted_in_its_own_race_window` hangs
  its hook on `is_alive` rather than on `join`** (issue #312). `stop()`
  reaches `join` only while the thread is still running, so a manager
  whose loop had already stopped by then skipped the hook and the test
  asserted on a socket it had never handed over — passing for the wrong
  reason on an idle machine and failing on a loaded one.

### `block_download`'s out-of-work branch is now covered on purpose

- **`tests/unit/download_test.py` now covers the `else: return` a
  connection's own turn in `block_download` falls into once neither
  `waiting` nor `pending` has anything left to hand it** (issue #319).
  Whether that branch ran at all used to depend on how the suite's other
  tests happened to divide a window's blocks across connections at that
  moment; the new test builds three connections already holding the
  window's one block between them and a fourth, idle one, so the branch
  is reached by construction rather than by luck.

### `P2pManager.stop()` sweeps connections after `join`, not before

- **The sweep that closes every known connection moved to after
  `join()` returns, and the listening sockets are now closed
  explicitly rather than only through `server`'s own `with
  server_socket:`** (issue #312). A connection `server()`'s own accept
  loop created in the window between `stop()` scheduling `loop.stop`
  and that being delivered used to be missed by a sweep taken before
  `join()`, reaching only the generic task-cancellation loop
  afterward — which cannot close `Connection.client` for a task
  cancelled before it ever ran. The sweep now runs once nothing but
  this thread can still be adding to `self.connections`/
  `self.pending_connections`. `Connection.run` also closes its own
  socket in a `finally`, the same guarantee `server`'s own `with`
  already gave its listener.

### `REPOSITORY.md` points at the release fact instead of restating it

- **`REPOSITORY.md`'s *What is not configured, and why* restated that
  nothing here is released instead of pointing at `CONTRIBUTING.md`'s *A
  version, and no release*** (btclib-org/.github#291). The bullet now
  carries the pointer, in the shape `bbt`'s `REPOSITORY.md` already uses.

### A convention of this tree is not a reason to diverge from Core

- **Where Core defines the surface — an RPC's field names and what they
  mean, a message's semantics — being consistent with the rest of this
  codebase is not a reason to answer differently from Core**, the reader
  on the other side being a client written against Core. `CLAUDE.md`'s
  *Following Bitcoin Core* names only a constraint of this tree as
  grounds for a divergence, which leaves a convention of this tree
  readable as such a constraint. Units are where that bites hardest: a
  feerate here is satoshis per kvB wherever one is emitted or read, and
  Core's `getmempoolinfo` answers `mempoolminfee` in BTC per kvB, so the
  internally consistent answer is the one wrong by eight orders of
  magnitude. The rule stops where the encoding is not this tree's to
  pick: BIP133's `feefilter` carries satoshis per kvB because BIP133
  says so.

### `REPOSITORY.md`'s Merge methods section named the wrong landing subject

- **`COMMIT_OR_PR_TITLE` is the pull request title only where a branch
  has more than one commit; a single-commit branch lands under its own
  subject** (closes #302). `REPOSITORY.md`'s *Merge methods* section
  named the pull request's title unconditionally, in wording the
  organization standard's own *Merge method* section does not carry;
  `bitcoin-core-rpc` and `portanode` already state the conditional, and
  this tree's `REPOSITORY.md` now matches them.

### This node's own `feefilter` is resent as its mempool's own minimum moves

- **`DownloadManager._send_due_feefilters`, called from `step()` like
  every other per-connection schedule this file keeps, tells each
  connected peer this node's own current relay floor and resends it as
  that floor changes** (closes #275). Sent once and never again before
  this, out of `callbacks.verack`; Core's own `MaybeSendFeefilter`
  (`net_processing.cpp:5822`, bitcoin/bitcoin@58a7869f86) is not a
  handshake action either, reached instead from the ordinary per-peer
  message loop, so the static send is removed from `verack` rather than
  kept alongside the new schedule. The floor itself is
  `Mempool.get_min_fee_rate()` (#294), floored at `Config.
  min_relay_feerate`, rounded through a geometric bucket set
  (`_fee_filter_buckets`, Core's own `FeeFilterRounder`) that a 2-in-3
  draw rounds down from even at an exact boundary, so this node's own
  rolling minimum is not readable exactly from what it tells a peer.
  Resent on an exponential schedule averaging ten minutes, pulled
  forward to within five minutes of one already due where the floor
  moves by more than a third; during initial block download every peer
  is sent the top of the bucket set instead, telling it not to bother,
  the same as Core.

### `getmempoolinfo` answers `maxmempool` and `mempoolminfee`

- **Both read `Mempool.bytesize_limit` and `Mempool.get_min_fee_rate()`
  (#294), the source neither field had before it** (closes #305).
  `mempoolminfee` is BTC/kvB, matching Core's own `MempoolInfoToJSON`
  (`src/rpc/mempool.cpp:1075-1086`, bitcoin/bitcoin@58a7869f86) rather
  than this tree's own sat/kvB used everywhere else a feerate is
  emitted or read: Core defines this surface, so the unit follows Core
  here even though it does not match the rest of this tree. The exact
  eight-decimal string Core's own `ValueFromAmount` produces is written
  to the wire directly, through a new `RawJSON` (`rpc/connection.py`),
  since a Python `float` cannot always carry it without exponent
  notation. `maxmempool` needs no such divergence, Core's own field
  being plain bytes already. `minrelaytxfee` and `incrementalrelayfee`
  are left out despite being real and cheap to answer, because #305
  named only these two fields; every other field RPC answers is left
  out because, unlike those two, it has no concept behind it in this
  tree to read a real number from: none of `usage`, `total_fee`,
  `unbroadcastcount`, `permitbaremultisig`, `maxdatacarriersize`,
  `limitclustercount`, `limitclustersize` or `optimal`.

### `CLAUDE.md` says this tree follows Bitcoin Core, and what a divergence owes

- **Where this tree reimplements something Bitcoin Core also does, it
  matches Core's behaviour wherever that is possible and reasonable, and
  the comment names the commit Core was read at.** What differs from
  Core in consensus or in relay is a difference the network sees. A
  divergence a constraint of this tree forces is legitimate —
  `btclib_node/db.py`'s docstring is the worked example — but it is
  argued where it is made: a citation with an unexplained difference
  beside it does not tell a reader a decision from an oversight.

### `CLAUDE.md`'s Architecture names which state crosses threads

- **The paragraph #304 added to *Architecture* has no entry of its own
  above, and this is it.** It says a coroutine enters either manager's
  asyncio loop only through `run_coroutine_threadsafe` while their plain
  methods do not, so what decides whether state needs a lock is which
  thread reaches it and never which callback names it — which is why
  `Mempool` needs none and `PeerDB` carries two.

### `CLAUDE.md` names the worktree `wt-<tracker>-<issue>-<repo>-<role>`

- **The recipe named the worktree after the issue alone, `wt<issue>`**
  (btclib-org/.github#292). A worktree's administrative directory lives
  in the `.git` of the repository `git worktree add` was run from, one
  per repository, so two repositories cannot collide there; what the
  recipe left uncovered was a same-repository collision, between two
  worktrees of different work sharing a generic basename, and a *path*
  collision across repositories, since the workers of one session share
  one scratchpad directory and a session carrying one issue into several
  repositories computed the same target path for each. The recipe now
  names the worktree `wt-<tracker>-<issue>-<repo>-<role>`, most general
  part first: `tracker` because an issue number is unique only within
  one tracker, `issue` against the same-repository collision, `repo`
  against the cross-repository path collision, and `role` against a
  coder and its reviewer holding a worktree at once.

### `CLAUDE.md`'s primary-checkout paragraph names the read that cannot go stale

- **The paragraph said reading the checkout was fine and so was `git
  fetch`, without saying `git fetch` moves `refs/remotes/origin/main`
  and leaves the work tree where it was** (btclib-org/.github#255), so a
  `grep` or a `Read` against the checkout answered for whenever it was
  last brought forward. It now names `git show origin/main:<path>` as
  the read that does not go stale, and gives the fast-forward that
  brings a clean checkout forward without working in it.

### A test that builds a `Node`, a `Chainstate` or a `BlockDB` closes it

- **A fixture builds a regtest `Node` that is never `start()`ed and
  closes it: `Chainstate`, `BlockDB`, both managers' event loops and the
  worker pool** (closes #111). `run`'s own teardown closes all four, and
  never runs for a node driven directly on the thread that built it,
  which is the shape `tests/unit/main_test.py`'s tests and
  `tests/unit/chainstate/filter_index_test.py`'s use throughout.
  `tests/unit/chainstate/block_index_test.py` and
  `tests/unit/block_db_test.py`, which open a `Chainstate` or a
  `BlockDB` directly rather than through a `Node`, get the same
  factory-fixture shape.
- **`tests/unit/init_test.py`'s `a_networked_node` closes the real
  `PeerDB` its own real `P2pManager` opened**, before replacing that
  manager with a stand-in that carries none of it: nothing else ever
  reached the original to close it.
- **`filterwarnings` is `["error"]`**, in place of the blanket
  `['ignore:cannot collect test class']` (closes #31). What stays
  named is `TestNet`'s own collection warning -- pytest collects any
  class whose name matches `Test*`, and `btclib_node.chains.TestNet`
  is one -- and, on the handful of tests whose own subject is a
  warning raised on purpose (an unhandled exception on a manager's
  thread that cannot bind, a coroutine a deliberately unrun loop never
  awaits), a `pytest.mark.filterwarnings` naming that test alone.

### `Mempool` evicts to its limit instead of refusing everything past it

- **`Mempool.add_tx` evicts the worst individual feerate, and its
  in-mempool descendants, to make room past `bytesize_limit` rather
  than refusing outright once it is reached** (closes #294). A full
  mempool used to be a wall: nothing already held was ever removed, so
  a transaction paying whatever fee still could not get in, and
  `get_missing` answered every request with nothing at all rather than
  let `add_tx` decide per transaction. Bitcoin Core's own
  `CTxMemPool::TrimToSize` (`src/txmempool.cpp`) evicts the worst
  *chunk*, a package score over its whole cluster graph; this mempool
  keeps no dependency graph to score packages by, so evicting the
  worst individual feerate together with everything depending on it is
  the substitute that stays consistent without one --
  `main.verify_mempool_acceptance` admits a child whose parent is only
  in the mempool, and evicting a parent alone would leave that child's
  own prevout resolving nowhere.
- **`bytesize_limit` moves from 500 vMB to 300, matching Core's own
  `DEFAULT_MAX_MEMPOOL_SIZE_MB`.** The old value carried no argument on
  record for the difference, and was inert while nothing ever evicted;
  eviction is what first makes it an economic threshold rather than a
  fixed ceiling, which is the reasoning this number needed and did not
  have. It is not exposed on `Config`: eviction does not need a
  configurable limit to exist, only a real one.
- **A rolling minimum feerate, Core's own `GetMinFee`, tracks what an
  eviction round just raised and decays it once a block confirms.**
  `Mempool.note_block_connected`, called once per block
  `main.update_chain` connects, restarts the decay clock
  `Mempool.get_min_fee_rate` reads.
- **`DownloadManager` checks current mempool membership at send time**,
  both for a transaction newly queued in `tx_download` and for one
  already sitting in a connection's `tx_announce_queue`: eviction can
  take a transaction back out between the moment it is queued for
  announcement and the moment that announcement is sent, which a queue
  of hashes alone cannot tell apart from one still held.

### `manage_connections` stops redialling an endpoint it just dropped for cause

- **`P2pManager` keeps an in-memory, process-lifetime set of endpoints
  `manage_connections` will not dial** (closes #283), added to by every
  `conn.stop()` site that drops a connection for incompatibility or for
  a protocol violation -- a self-connection, a `version` below
  `ProtocolVersion`, no `NODE_WITNESS`, no `NODE_NETWORK` once
  `BlockSynced`, a `verack` out of order, a `pong` whose nonce does not
  match, a message ahead of the handshake, and a `BTClibException`
  raised while handling one. Not a `PeerDB` table and not persisted:
  Core's own discouragement is the same shape, a `CRollingBloomFilter`
  held only for the process's life (`banman.h`,
  bitcoin/bitcoin@58a7869f86), and a wrongly discouraged endpoint is
  recovered by a restart rather than by touching the datadir. An
  endpoint this node dialled and closed on its own account -- an idle
  timeout, a send-buffer bound -- is not marked.
- **An exception `p2p/main.py`'s bare `except` turns into `conn.stop()`
  marks the endpoint only where it is a `BTClibException`.** That
  `except` also catches this node's own bugs on content a peer sent
  that was otherwise fine -- `get_cfilters`'s "no filter for a block on
  the active chain" among them -- and those are not cause to discourage
  the peer that merely triggered them.
- **`Connection.run`'s own envelope-parsing failure marks the endpoint
  the same way, and on the same `BTClibException` guard.** A bad
  checksum, an oversized length, or a message for a network this node is
  not on never reaches `p2p/main.py` at all -- `Message.parse` and the
  network-magic check right after it raise straight out of `Connection`
  itself, which is where `manage_connections` would otherwise have
  redialled the address back the next tick.

### `PeerDB.addresses` gets a lock of its own, separate from the active table's

- **`add_addresses` and `random_address` serialize every touch of
  `PeerDB.addresses` through a lock of their own, distinct from
  `active_addresses`'s** (closes #298). `add_addresses` reaches the set
  from both `Node`'s thread, off gossip through `callbacks.addr` and
  `addrv2`, and `P2pManager`'s, off `get_addr_from_dns` resolving a
  seed; `random_address`'s own dialable-address comprehension walks the
  same set from `P2pManager`'s thread while gossip can be mutating it on
  `Node`'s. Unprotected, that pairing is `RuntimeError: Set changed size
  during iteration` in CPython, not only a lost update.
- **A lock shared with `active_addresses` was measured and declined.**
  Nothing here ever needs the two tables updated as one atomic step, and
  `add_addresses`'s own durable write batch runs measurably longer than
  `add_active_address`'s single row; sharing one lock would let that
  batch hold up every completed handshake for no invariant a lock of its
  own does not already give.

### `Node`'s worker pool is sized to the machine, and always closed

- **`_WORKER_PROCESSES` is 8 outside of a test run and the machine's core
  count divided across `pytest-xdist`'s own workers under one** (closes
  #46). `-n auto` runs one worker per core, and each independently built
  every `Node` under test a pool of 8 processes on top of that; on a
  ten-core machine under ten workers that is up to 80 processes
  contending with the ten the cores can actually run, which is what a
  `wait_until` timeout in the functional suite was measuring. Reading
  `PYTEST_XDIST_WORKER_COUNT`, which `pytest-xdist` sets in a worker's
  environment before any test module is imported, keeps the total near
  the core count instead.
- **`run`'s teardown joins the worker pool after `terminate()` and drops
  the reference, and `Node.__del__` closes it too, for a `Node` that
  never reaches that teardown at all** (closes #195). `terminate()`
  alone left the pool's workers unreaped, but the reference it left
  behind was not on its own the source of the `Exception ignored ...
  OSError: [Errno 9] Bad file descriptor` reported against #195:
  `tests/unit/main_test.py` builds several `Node`s and calls
  `update_chain` against them directly, never starting the thread
  `run`'s own teardown lives on, so their pools were never terminated at
  all rather than merely unjoined -- and `__del__` is what a `Node`
  built and used that way still has.

### `add_active_address` settles a redialled endpoint onto its one row

- **A handshake with an endpoint already active settles onto that
  endpoint's one row in `PeerDB.active_addresses` rather than adding
  another** (closes #270), the way `add_addresses`'s own `by_endpoint`
  already settles `self.addresses`. `add_active_address` runs once per
  handshake rather than once per batch, so the lookup is an endpoint-
  keyed index kept alongside the list rather than a per-call scan of
  it, which would turn many handshakes against the one peer quadratic
  overall.

### `getaddr` answers from a cached sample, and the active table prunes on its own

- **The sample `getaddr` answers with is drawn once and served to every
  connection until it expires, rather than redrawn per connection**
  (#71). A fresh random sample per connection let two peers connecting
  close together compare answers and infer what changed between them,
  which answering a `getaddr` only once per connection does not stop by
  itself, a new connection still drawing its own fresh sample.
- **`P2pManager.manage_connections` prunes `PeerDB`'s active-address
  table on its own timer** (#71), rather than only as a side effect of
  `random_address` or `getaddr`. Both stop reaching for the table once
  this node already has enough connections or has already answered a
  connection's one `getaddr`, so a well-connected node nobody ever asks
  never pruned a stale row.
- **`PeerDB` serializes every write to `active_addresses` and its
  endpoint index through one lock** (#71). `add_active_address` reaches
  them from `Node`'s own thread, off `callbacks.verack`, and the
  periodic prune above reaches them from `P2pManager`'s, off
  `manage_connections`; unlocked, a position read against one thread's
  view of the list could be written into the other's already-reshaped
  one, corrupting an unrelated row rather than merely losing an update.
- **The periodic prune runs inside the same `try`/`except`
  `manage_connections`'s dial already does** (#71). `get_active_addresses`
  does real I/O against the store, and that coroutine's own future is
  never awaited, so whatever it ever raised unguarded would end the
  whole loop's pinging, eviction and dialling for the rest of this
  node's life rather than only that one prune.

### A relay octet that is neither `0x00` nor `0x01` keeps costing the peer

- **`Version.parse` raising on a relay flag outside `0x00`/`0x01` is this
  node's policy, not a defect left open** (closes #149). Core's own
  `Unserialize<bool>` (`src/serialize.h`) reads any nonzero octet as
  true, so a `0x02` there reads as a peer asking for relay; btclib's own
  docstring calls that reading the malleability its refusal is for --
  one payload, two possible readings serializing back to only one of
  them. Reaching Core's leniency in `btclib_node` would need either
  replaying `Version.parse`'s whole field walk (both `NetworkAddress`
  entries, the `var_bytes` user agent) just to find where the octet
  sits in the payload, or matching the wording of the
  `BTClibValueError` it raises -- unlike the stream-based leniency
  `addr`/`addrv2` use below, both bind this node to btclib's private
  shape rather than its public contract, for a byte Core's own encoder
  (`Serialize<bool>`, same file) can only ever write as `0x00` or
  `0x01`. Pinned by
  `test_a_relay_octet_that_is_neither_0_nor_1_still_costs_the_peer`.

### `sendrawtransaction` refuses a transaction it cannot decode

- **A `rawtx` `Tx.parse` cannot decode raises `RpcError` with the new
  `DESERIALIZATION_ERROR` (`-22`) and Core's own message, "TX decode
  failed. Make sure the tx has at least one input."** (#274), rather
  than being caught by a bare `except Exception` and answered `{"result":
  null}` -- a shape a JSON-RPC client reads as success. A `rawtx` of a
  JSON type other than a string is now named with `TYPE_ERROR`, the same
  way `getrawtransaction`'s `txid` already is.

### `AManager`, the `Node.run` test stand-in, carries a `peer_db`

- **`AManager` gains a `peer_db` attribute whose `close` is a no-op**
  (#263). `Node.run`'s shutdown path calls
  `self.p2p_manager.peer_db.close()` unconditionally, which `AManager`
  did not carry: a test built on the `a_networked_node` fixture that
  reached that path raised `AttributeError` in the node's own
  background thread, surfacing as an unhandled-thread-exception warning
  rather than a test failure.

### `bootstrap-dns.yml` runs on the calendar's own schedule

- **The workflow carries a `schedule:` naming `cron: "20 5 * * 4"`**
  (#272), Thursday at that hour and this repository's own minute being
  the row btclib-org/.github#201 gave it in section 10 of that
  repository's README. It previously ran on `workflow_dispatch` alone,
  waiting on that row to exist.
- **The header's paragraph on the absent schedule is rewritten to say
  where the one it now carries comes from**, instead of continuing to
  describe a state this same change ends.

### A peer's `feefilter` is honoured, and `Mempool` keeps a fee per transaction

- **`Mempool` now keeps the fee each transaction paid alongside it
  (`Mempool.fees`, `Mempool.add_tx`'s new `fee` argument), and
  `DownloadManager.tx_download` and `P2pManager.broadcast_raw_transaction`
  withhold a transaction from a connection whose own `feefilter` (#94)
  it does not clear** (#260), through the new `Mempool.meets_fee_rate`.
  The fee itself is `main.verify_mempool_acceptance`'s own
  sum-of-inputs-less-sum-of-outputs, computed there already and
  returned rather than discarded. `broadcast_raw_transaction` grew a
  required `fee` argument to carry it in from
  `rpc.callbacks.send_raw_transaction`, the one caller outside the
  mempool's own bookkeeping.

### The worker pool's own cold start moves off the thread that promotes a connection

- **`Node.warm_worker_pool` builds `worker_pool` and dispatches
  warm-up calls across its processes, on a thread of its own, right
  before `download_manager.block_download` sends the first `GetData`
  for a block this node does not have** (#262). `check_transactions`'
  own first call used to be what built and warmed the pool, on
  `Node.run`'s own thread -- the same one that drains
  `p2p_manager.handshake_messages` and promotes a connection once its
  `verack` arrives. Each of the pool's processes pays its own import of
  `btclib_node.interpreter`, and through it `btclib.script.engine`, on
  its own first dispatch; while that import ran on `Node.run`'s thread,
  a peer whose `verack` the kernel had already delivered sat unpromoted
  in `pending_connections` until the call returned. The import still
  happens on the same schedule relative to block download, now on a
  thread a peer's own promotion does not wait on. A node whose headers
  are synced but which never has a block to ask for -- a header-only
  peer under test, a peer whose counterpart stops serving blocks --
  never reaches that line and never pays for a pool it never validates
  a script with, matching `Node.__init__`'s own comment beside
  `_worker_pool`.

### `sendrawtransaction` answers a refusal, not a rejected transaction's txid

- **A transaction `verify_mempool_acceptance` finds invalid is answered
  `-26` (Core's `RPC_VERIFY_REJECTED`) with the same reject reason
  `testmempoolaccept` already gives it, and one whose prevout is
  nowhere is answered `-25` (`RPC_VERIFY_ERROR`, Core's own code for
  missing inputs) rather than `-32603 Internal error`** (#83). The
  first case used to be swallowed and answered with the txid, as if the
  network had taken a transaction this node itself refused; the second
  fell through to the callback dispatcher's generic handler, which
  answers as though this node were the one at fault. `send_raw_transaction`
  and `testmempoolaccept` now share the same two reject-reason strings,
  so the two RPCs agree about the same transaction.

### `RpcManager.stop` waits on its own thread instead of spinning a core

- **`RpcManager.stop` blocks on `self.join()` rather than polling
  `self.loop.is_running()` in a tight loop** (#257), the same fix #249
  gave `P2pManager.stop`. The calling thread no longer spins a full CPU
  core for the scheduling delay of the manager's own event-loop thread;
  the join is skipped where the manager was never started, which
  `Node.run` reaches unconditionally for a node built with `rpc_port`
  unset.

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

### `check-sdist` builds with the backend `[build-system]` declares

- **The `check-sdist` hook now carries `args: [--inject-junk,
  --installer=pip]` and `additional_dependencies:
  ["uv_build>=0.12.5,<0.13"]`, naming `[build-system]`'s own `requires`
  range** (btclib-org/.github#197). Without `--installer=pip` the hook
  packs the archive with the outer `uv`'s own bundled backend regardless
  of what `additional_dependencies` names, `check_sdist/sdist.py`'s
  `get_uv()` finding a `uv` on `PATH` before ever consulting the
  dependency it was given: with the two specifiers set to disagreeing
  ranges the hook stayed green. `--installer=pip` runs `build
  --no-isolation` instead, which reads `[build-system]` from the
  environment and refuses one that does not satisfy it; measured against
  a deliberately mismatched range, the hook then fails with `ERROR
  Missing dependencies`.

### `.gitattributes` carries the organization's own paragraph on both lines

- **`.gitattributes` is the standard's file, byte for byte**
  (btclib-org/.github#192). This tree holds no `RELEASE_NOTES.md` and no
  attribute of its own, so the shared half is the whole file; its hash
  matches the raw file `btclib-org/.github` serves at its own `main`,
  and `git check-attr merge CHANGELOG.md RELEASE_NOTES.md` still answers
  `union` for both.

### The installed package ships `py.typed` and declares its surface

- **`btclib_node/py.typed` is in the tree, and `uv build` puts it in
  both the wheel and the sdist** (btclib-org/.github#239), needing no
  change to `[tool.uv.build-backend]`'s patterns. `classifiers` gains
  `Typing :: Typed` beside it, the comment that used to argue the
  classifier's absence now arguing why it is there instead.
  `btclib_node/__init__.py` declares `__all__ = ["Node"]`: no consumer
  of the package root, in this repository or out, has ever imported
  anything else from it — `from btclib_node import Node, main` in
  `tests/unit/main.py` reaches `main` as the submodule Python already
  registers on import, not as a name `__all__` would need to carry.

### `BlockDB`'s file counter and filenames no longer have a fixed width

- **The block-file rotation counter is a `var_int`, not a fixed-width
  field** (#78). `(self.file_index).to_bytes(2, "big")` raised
  `OverflowError` once the counter grew past what two octets encode,
  with nothing checking for it beforehand.
- **`BlockLocation` and `FileMetadata` read and write their filename
  length-prefixed, not as a fixed-width slice** (#78). `file.name[-10:]`
  and `stream.read(10)` assumed the name was always exactly ten
  characters, keying or truncating it wrongly once it grew past that.
- **`BlockLocation.index` and `FileMetadata.size` parse past
  `var_int.parse`'s default cap** (collateral of the same fix): that cap
  bounds an item count a peer could inflate, and a byte offset inside a
  still-open 128MB block file already exceeds it before the file
  rotates. Bitcoin Core's own on-disk position, `FlatFilePos::nPos`,
  skips the same guard for the same reason.
- **A block or reverse-patch file is matched by its resolved path, not
  by a basename or a string suffix** (#79). Comparing suffixes accepts
  another directory's file of the same name; nothing exercises that today
  since one `BlockDB` owns one `data_dir`, but the comparison now says
  what is meant instead of what happens to be true.
- **A block store's `blocks/` directory written before this change
  cannot be reopened**: its records were the fixed ten-octet filename
  this change removes, and `BlockLocation.deserialize` now reads the
  first of those octets as a length prefix instead, raising rather than
  returning a wrong answer. A node upgrading past this change starts
  from an empty `blocks/` directory.

### A reorg checks the transactions it hands back to the mempool

- **`update_chain` puts a transaction of an abandoned block back into
  the mempool only once `verify_mempool_acceptance` has passed it**
  (#85), the same check every other entrant into the mempool already
  went through. A transaction that spent an output only the abandoned
  branch ever had is now dropped rather than sitting in the mempool
  answering `getrawmempool` and `getdata` for something the node's own
  acceptance check would refuse. The abandoned blocks are walked oldest
  first for this, the opposite of the order the utxo undo above it
  uses, so that a transaction depending on an earlier abandoned block's
  own transaction finds it already back in the mempool: Core's
  `MaybeUpdateMempoolForReorg` re-adds the same way
  (`src/validation.cpp`).
- The loop re-adding those transactions to the mempool and the one
  removing the newly connected chain's own transactions from it now
  read the same way, `for tx in block.transactions[1:]`: coinbase
  transactions were excluded from the first and not the second, an
  asymmetry with no consequence — a coinbase is never a mempool
  entrant — and no explanation either.

### `finish_sync` no longer drops every peer to revise a relay flag

- **A connection's own `Version` always asks for transaction relay**
  (#129): Core's `fRelay` is about the connection itself — a
  block-relay-only peer, a feeler, `-blocksonly`
  (`RejectIncomingTxs`, `src/net_processing.cpp`) — and never about
  `IsInitialBlockDownload()`, so it never has to be revised once a node
  catches up. `finish_sync` no longer calls `p2p_manager.stop_all()` to
  get there: every connection already asked for relay from its first
  handshake, inbound or outbound. A transaction a peer sends before
  this node has enough of the chain to check it is dropped where it
  arrives, `p2p.callbacks.tx`, matching Core's own early return for the
  same reason during initial block download.

### Every test module under `tests/` is named `*_test.py`

- **Every test module under `tests/` is renamed to end in `_test.py`,
  `tests/unit/`'s losing the `test_` prefix and `tests/functional/`'s
  losing it for the suffix, and the test modules that had been living
  inside a package's `__init__.py` move out into a sibling of their
  own** (#26). A package's own `__init__.py` cannot itself be named
  `*_test.py`, so `tests/unit/init_test.py`,
  `tests/unit/chainstate/init_test.py`,
  `tests/unit/p2p/messages/init_test.py`,
  `tests/functional/p2p/init_test.py` and
  `tests/functional/rpc/init_test.py` carry what used to live in the
  package's `__init__.py`, and `tests/unit/rpc/connection_test.py`
  carries what `tests/unit/rpc/__init__.py` tested despite
  `btclib_node/rpc/__init__.py` itself being empty.
  `tests/unit/helpers.py` renames to `tests/unit/helpers_test.py` for
  the same reason: nothing imports it, it tests `tests/helpers.py`'s
  functions, and only its name said otherwise.
- **`.pre-commit-config.yaml` gains `name-tests-test` at its default
  args**, enforcing the `*_test.py` pattern the rename above puts in
  place.
- **`[tool.pytest.ini_options]` drops `python_files` and
  `python_functions`**: pytest's own defaults already collect
  `*_test.py` and any function named `test*`, so restating either was a
  second place for a fact pytest already states, and the one that had
  drifted — `python_files = "*.py"` was collecting every module under
  `tests/`, which is what let the test modules living inside a
  package's `__init__.py` go unnoticed as anything other than a package
  marker.

### `pytest --strict-config --strict-markers` and `xfail_strict = true`

- **`addopts` gains `--strict-config --strict-markers`, and
  `xfail_strict = true` joins `[tool.pytest.ini_options]`** (#31). Every
  marker this suite uses (`pytest.mark.parametrize`, `pytest.mark.order`)
  is already registered by pytest or by pytest-order, and the suite has
  no `xfail` today, so both are ratchets at zero cost.
- **`filterwarnings` keeps its single, named `ignore` and does not
  become `["error"]`**: measured, that setting fails tests scattered
  across the suite, nearly all on a `ResourceWarning` raised at
  garbage-collection time against the sqlite connections, sockets,
  event loops and multiprocessing pools issue #111 and issue #195
  already track, plus a hazard neither issue is about — a
  `ResourceWarning` raised at GC time fails whatever test the collector
  happens to run during, not the test that leaked, so two runs of the
  same tree fail a different set of tests. Turning this on is
  contingent on #111 and #195, not a `pyproject.toml` line.

### `feefilter` is answered and stored, not thrown away

- **This node sends its own `feefilter` (`Config.min_relay_feerate`,
  defaulting to Core's own `DEFAULT_MIN_RELAY_TX_FEE` of 100 sat/kvB)
  once the handshake completes, and a peer's own `feefilter` is parsed
  and kept on `Connection.feefilter`** (#94), where it used to be an
  unknown command `handle_p2p` silently dropped.

### An octet past an `addr` or `addrv2` no longer costs the peer

- **A trailing octet past the last address of an `addr` or `addrv2`
  payload is now silently left unread rather than disconnecting the
  peer that sent it** (#149). btclib's `Addr.parse`/`AddrV2.parse` raise
  on it (`assert_no_trailing`, a malleability guard), and
  `main.handle_p2p` turned that raise into `conn.stop()`; Bitcoin Core
  does not disconnect over it. Both accept a `BinaryData` stream, and
  `assert_no_trailing` skips its own check for one by design (its own
  docstring: a stream is "the caller's"), so wrapping the payload in one
  reaches Core's leniency without a second copy of either codec.
  `Version.parse` takes narrower `Octets` and cannot be handed the same
  way without risking its own optional relay-flag byte being misread, so
  a version carrying a trailing octet still disconnects the peer -- the
  same policy question, answered differently because the two codecs
  offer different means to answer it. Issue #149's other half -- a relay
  octet that is neither `0x00` nor `0x01` -- is above.

### `getblockcount`, `getrawtransaction` and `getblockchaininfo` answer

- **`getblockcount` answers the active chain's own height** (#21), the
  active chain's last index rather than its length.
- **`getrawtransaction` answers a mempool transaction by default, and a
  block named explicitly alongside it** (#21) -- `block_index` and
  `block_db`, read and not indexed again, so no store is added. A
  transaction confirmed and not named a block is answered
  `RPC_INVALID_ADDRESS_OR_KEY`, the same code Core's own no-`-txindex`
  fallback answers (`src/rpc/rawtransaction.cpp:313-314`), and verbosity
  is a bool and not Core's `NUM` `0`/`1`/`2`: `2`'s fee and prevout
  fields need undo data this node keeps nowhere.
- **`getblockchaininfo` answers `chain` alone** (#21). btclib's
  `BitcoinCoreFetcher.assert_network` and `bitcoin_core_rpc`'s
  `BitcoinCoreRpcClient.assert_chain` both call it before their first
  fetch by default, `verify_network` defaulting to `True` -- measured
  against a real `BitcoinCoreFetcher` here, `get_best_block_id` failed
  `-32601 Method not found` on this method, not on the one asked for,
  before this. `SigNet` here has no configurable challenge to report
  the `signet_challenge` member `assert_chain` also reads on that chain.

### `RpcManager` binds `Config.rpc_host`, not every interface, by default

- **`Config` gained `rpc_host`, `"127.0.0.1"` unless a caller asks
  otherwise, and `RpcManager._bind` reads it in place of a hardcoded
  `"0.0.0.0"`** (#27). The RPC port is this node's control plane, and
  `rpc/callbacks.py` and `rpc/connection.py` check no credential of any
  kind, so its own default should not be a peer-to-peer listener's:
  Bitcoin Core's own `rpcbind`/`rpcallowip` default to localhost for the
  same reason. `P2pManager.server`'s own `0.0.0.0` is unchanged and
  right to be: a peer listener is supposed to accept a stranger.

### A `method` that is not a string, and a body that is not JSON, both answer

- **`is_valid_rpc` now checks that `method` is a JSON string** (#63).
  `handle_rpc`'s `request["method"] not in callbacks` uses it as a dict
  key past that check, `TypeError: unhashable type` for a JSON array or
  object there -- unanswered since #62's guard on `Node.run` caught the
  exception, and the whole node's crash before #62.
- **A body `json.loads` cannot parse now answers `-32700 Parse error`,
  id `null`**, JSON-RPC 2.0 section 5.1's own code for it, in place of
  `Connection.run`'s `except Exception: self.client.close()`, which
  closed the socket with no answer at all.

### `active_addresses` records a completed handshake, not a closed connection

- **`PeerDB.add_active_address` is now called from `callbacks.verack`,
  once the handshake completes, and no longer from `Connection.stop`.**
  `stop` recorded `self.address` on every close, including a connection
  `callbacks.version` had itself just refused for calling itself, for
  running an old protocol version, or for missing a required service
  (#70): a peer this node turned away was gossiped to the next one as
  good. The address recorded is now the evidence the completed
  handshake is: the address this node dialled, for an outbound
  connection, since a socket connecting there already answered; the
  connection's own accepted address with the port the peer's own
  `version` names as `addr_from`, for an inbound one, since
  `P2pManager.server`'s own address is `sock_accept`'s source port, the
  peer's ephemeral one and never one this node could dial back on — and
  nothing at all where `addr_from` names no port (#70).
- **`getaddr` answers a connection once.** `Connection.answered_getaddr`
  is set the first time it does; a peer asking again on the same
  connection gets nothing, where before every ask walked the table
  again (#71).
- **`getaddr` answers with a random sample of `active_addresses`, capped
  the way Core caps its own answer, rather than the whole table sent as
  however many messages it takes.** Serving everything this node knows
  of to whoever asks is what an observer mapping the network wants; the
  sample is drawn without replacement from the active table, sized like
  Core's own `MAX_PCT_ADDR_TO_SEND` (23 percent, rounded up here rather
  than down so a handful of addresses is still answered with something)
  and capped at `MAX_ADDR_TO_SEND`, so one message is always enough
  (#71).
- **`PeerDB.active_addresses` is bounded the same way `PeerDB.addresses`
  already is, at ten thousand entries.** `get_active_addresses` prunes
  what has gone stale, but only where something still calls it, and a
  node with enough peers that nobody ever asks a `getaddr` had nothing
  left to call it: `add_active_address` had no bound of its own, so such
  a node grew the table for as long as it ran (#71).
- **A reconnect to the same endpoint still adds a second row to
  `active_addresses` rather than replacing the first.**
  `add_active_address` does not settle on one row per endpoint the way
  `add_addresses` already does for `self.addresses` (#247): doing so
  once per handshake rather than once per batch needs an index kept in
  sync across every mutation site, not a per-call scan, so it stays open
  as its own issue (#270).
- **`Connection.address` itself moves to the resolved endpoint `verack`
  computes, and `P2pManager.manage_connections`'s own already-connected
  check now compares by `endpoint_key` (`address.py`'s private
  `_endpoint`, renamed and exported) rather than by the
  `NetworkAddressV2` dataclasses themselves.** A raw-equality check never
  matched a live connection against a draw of that same peer's own
  gossiped address, `timestamp` and `services` differing between the two
  by construction, so once #70 and #71 made a two-node `getaddr` round
  trip actually work, `manage_connections` redialled a peer this node
  was already connected to the moment any connection to it ended --
  including one `pong` had just dropped for answering the wrong ping
  nonce, which is what `tests/functional/p2p/ping_test.py::test_wrong_ping`
  starts asserting by connection id rather than by the manager holding
  none, since a redial is exactly what now happens next. That a node
  redials a peer at all right after dropping it for cause, with nothing
  recording the cause, is left open as its own issue, #283.

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
  what `tests/functional/rpc/chain_test.py` asserts of a height read back
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
  cannot see it.** `tests/unit/coverage_floor_test.py` builds the option
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
  `tests/unit/p2p/address_test.py` that called `PeerDB.get_addr_from_dns`
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
  runs weekly, on the row btclib-org/.github#201 gave it in section 10
  of that repository's README, and on demand.

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

### The ack-of-record comment cites the organization's standard, not a local section

- **`claude-review.yml`'s ack-of-this-head comment now cites "section
  11 of the organization's standard" instead of "README.md's section
  11"** (btclib-org/.github#243). This repository's own `README.md`
  carries no numbered sections, so the citation pointed a reader at a
  document this tree does not have; the wording now matches the
  workflow's other copies, which name the standard rather than a file
  local to the reading repository.

### `DownloadManager`'s per-peer bookkeeping stops working against itself

- **`block_download`'s assignment loop now excludes a connection marked
  `pending_eviction`** (#68). The 120-second stall mark emptied
  `conn.download_queue` to free those blocks for another peer, but
  nothing past it in the loop read the mark, so the loop's own
  `download_queue == []` test read the just-emptied queue as "ready for
  more work" and handed the peer back the blocks it was already failing
  to deliver. The mark now means what it says until the peer's next
  block clears it (`callbacks.block`) or the 300-second bound
  disconnects it.
- **A transaction `DownloadManager.tx_download` accepts is queued on the
  connection it will be announced to, `Connection.tx_announce_queue`,
  and sent as an `Inv` only once that connection's own
  `Connection.next_inv_send_time` comes due** (#141), an exponential
  draw around a mean of 5 seconds for an inbound connection and 2 for an
  outbound one — `INBOUND_INVENTORY_BROADCAST_INTERVAL`,
  `OUTBOUND_INVENTORY_BROADCAST_INTERVAL` and `rand_exp_duration`,
  net_processing.cpp on bitcoin/bitcoin@58a7869f86 — rather than the
  single per-step `Inv` the previous batch-of-five removal (#114) turned
  into an immediate announcement. An outbound connection's draw is its
  own; an inbound one's is shared with every other inbound connection of
  the same address family (`DownloadManager._next_inbound_inv_time`,
  `_inbound_net_class`, mirroring `NextInvToInbounds` and the
  `CNode::m_network_key` it is keyed on, net.h and net.cpp of the same
  commit — not `NetGroupManager::GetGroup`, which feeds `nKeyedNetGroup`
  for addrman and eviction instead), so a peer opening several inbound
  connections to this node, from one address or from several, cannot
  average several independent draws down to a receipt time finer than
  one connection's own jitter allows. `P2pManager.broadcast_raw_transaction`
  no longer pushes a `Tx` of its own the instant it is called: it appends to the
  same list a relayed transaction's own arrival does, `conn_id` `None`
  in place of a peer to exclude, so a transaction of this node's own
  goes through the same queue and the same delay — the gap between a
  `tx` a peer sends this node and the `inv` this node sends on no
  longer says whether this node originated it or relayed it.
- **A `notfound` this node receives for a transaction now clears that
  peer's own outstanding ask, `Connection.tx_requested`** (#144), the
  per-peer table `tx_download`'s own request loop reads to avoid asking
  a peer twice for a wtxid it has not yet answered — mirroring
  `TxDownloadManagerImpl::ReceivedNotFound` (net_processing.cpp, the
  same commit), which reads only the transaction items of a `notfound`
  for the same reason. Before this, a `notfound` was logged and nothing
  else, since there was no per-peer record of an outstanding ask for it
  to clear.

### Transaction relay is bounded, expires and only announces what it kept

- **`_send_due_announcements` now splits `Connection.tx_announce_queue`
  into `Inv` messages of at most `MAX_INV_SZ` entries each** (closes
  #282), rather than building one `Inv` from the whole queue.
  `btclib.p2p.inventory.Inv` raises past that bound on construction, and
  a slow-scheduled connection's own timer — a mean of several seconds,
  an exponential draw's own tail longer still — was enough wall-clock
  time for a busy node's mempool churn to grow the queue past it, taking
  down `Node.run`'s own loop from inside `tx_download`. Core's own
  `SendMessages` (net_processing.cpp) answers the same way: several
  `MakeAndPushMessage` calls of at most `MAX_INV_SZ` each.
- **`Connection.tx_requested`'s entries now expire after 60 seconds**
  (closes #289), Core's own `GETDATA_TX_INTERVAL`
  (`node/txdownloadman.h`). A peer that neither sent the transaction nor
  answered `notfound` left its own entry in place forever, which made
  `tx_download`'s `wanted` filter read a `getdata` as still outstanding
  for the rest of that connection's life — this node would never ask
  that one peer for the wtxid again, even past a later re-announcement.
- **`Mempool.add_tx` now reports whether it added the transaction, and
  `p2p/callbacks.py`'s `tx` handler queues an announcement only when it
  did** (closes #277). The handler used to gate both `add_tx` and the
  announcement queue, `DownloadManager.received_txs`, on one
  `contains_tx` check taken before either ran; `add_tx` silently declines
  past `Mempool.is_full()`, so a transaction dropped for a full mempool
  was still announced to every other peer, which then asked for it and
  got `notfound`.

### `sendrawtransaction` refuses a transaction a full mempool could not keep

- **`send_raw_transaction` now raises `RpcErrorCode.VERIFY_REJECTED`
  ("Mempool is full") rather than answering `tx.id.hex()` for a
  transaction `Mempool.add_tx` silently declined past
  `Mempool.is_full()`** (closes #293), the same defect #277 fixed on the
  peer-to-peer path. `-26` is Core's own code for this refusal too:
  `TxValidationResult::TX_MEMPOOL_POLICY` invalidated "mempool full"
  (`validation.cpp`) becomes `TransactionError::MEMPOOL_REJECTED`
  (`node/transaction.cpp`), which `RPCErrorFromTransactionError`
  (`rpc/util.cpp`) answers with `RPC_TRANSACTION_REJECTED` — a bare
  alias of `RPC_VERIFY_REJECTED` (`rpc/protocol.h`,
  bitcoin/bitcoin@58a7869f86). The exemption for a resubmission is keyed
  on `tx.id in node.mempool.txid_index`, not `Mempool.contains_tx`,
  which is keyed by wtxid: a resubmission under a different witness is
  still tolerated and reannounced, mirroring `BroadcastTransaction`'s
  own early return for a txid already in the mempool -- itself
  txid-keyed, and explicit that the held transaction "may have the same
  or different witness" -- which does not reach Core's own capacity
  check either. What is reannounced on a resubmission is the mempool's
  own copy of the transaction and not the resubmitted object: the two
  can carry different witnesses and therefore different wtxids, and
  `P2pManager.broadcast_raw_transaction` queues whichever one it is
  handed for announcement by that object's own `.hash` -- the same
  substitution `BroadcastTransaction` itself makes ("Use the mempool's
  wtxid for reannouncement"), needed here for the same reason: announcing
  a wtxid `add_tx` never stored answers a peer's `getdata` with
  `notfound`. This substitution is not gated on `Mempool.is_full()`:
  `add_tx` returns `False` for an already-held txid whether or not the
  mempool is full, so a resubmission under a different witness into a
  mempool with room to spare reached the same mismatched-wtxid
  announcement before this change, unrelated to the refusal above and
  present before this branch touched the file.

### Two testnet blocks exercise the filter index at a scale Core's vectors miss

- **`tests/unit/chainstate/_data/testnet_bip158_vectors.json` adds
  heights 54499 and 54503, derived rather than vendored, beside
  `blockfilters.json`'s ten** (closes #181). Neither is in Bitcoin
  Core's own `src/test/data/blockfilters.json`, whose largest block is a
  few kilobytes with no transaction spending another in the same block;
  54499 is forty-odd kilobytes and twenty-four transactions, most of
  them resolving a previous output from elsewhere in the same block,
  which is the scenario none of Core's rows reaches. The two blocks were
  pulled from testnet by the hash a survey of Libbitcoin's test suite
  named, and their filters built with this tree's own
  `BasicBlockFilter.from_block` — nothing of Libbitcoin, AGPL-3.0-or-later,
  is in the tree; only the two block hashes and, as a positive control,
  the filter an independent SipHash-2-4 and Golomb-Rice implementation
  in that suite computed for height 54503. The filter header column has
  no external source either and is computed locally, chained only
  within the new file. `tests/_data/README.md` has the full derivation.
