# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working
with code in this repository.

How to work here — what the issue tracker takes, the prose style, how a
pull request is opened and landed, and the commands and gates of this
tree — is `CONTRIBUTING.md`, which is the same file in every repository
of the organization up to its last section, that last section being this
tree's. Repository configuration is `REPOSITORY.md`: read it before
changing a workflow, a branch rule or a setting. Reviewing is
`REVIEWING.md`, and `/review` is that file as a command; read it before
reviewing a pull request and before opening one, since it is what the
pull request will be answered against.

## Architecture

A bitcoin full node whose consensus and network code is Python, over
[btclib](https://github.com/btclib-org/btclib), which is where the
objects on the wire and their serialization come from. What is here is
the loop that drives them, and its state.

`Node` in `src/btclib_node/__init__.py` is a thread running one loop: it
drains the handshake queue, then a share of the RPC queue and a share of
the peer-to-peer queue, then steps the download manager and extends the
chain. A message that raises is logged and the loop continues; a failure
under `update_chain` leaves it, because the databases below have to be
closed on the way out.

- `src/btclib_node/p2p/` is the protocol — the connections, the peer
  manager, the address book and the message handlers the loop calls.
- `src/btclib_node/rpc/` is the JSON-RPC surface, on the same shape of
  manager and handler.
- `src/btclib_node/chainstate/` is the block index, the UTXO set and the
  compact filter index; `src/btclib_node/block_db/` is the blocks and
  their undo data. **Genesis sits at index 0 of the active chain**, so
  `active_chain[i]` has height `i` and `len(active_chain)` is the
  height a block extending the chain would connect at -- which is what
  a mempool check wants, a transaction there being judged as if it were
  in the next block, and is Core's own `GetSpendHeight`
  (`m_chain.Height() + 1`). `verify_mempool_acceptance` carried
  `len(active_chain) + 1` until btclib-org/btclib-node#569, harmless
  only because every regtest activation height is 0 and the number had
  never had to be right.
- `src/btclib_node/db.py` is the ordered key-value store all of those
  are kept in. Its docstring is where the implementation is argued, and
  **key order is load-bearing**: a reader that stops at the first key
  without its prefix is truncated by a prefix that sorts before it.
- `src/btclib_node/interpreter.py` validates, and `Node.worker_pool` is
  what it validates a fork across.

`P2pManager` and `RpcManager` are each a thread of their own, running an
asyncio loop, and a coroutine enters that loop only through
`run_coroutine_threadsafe`. Their plain methods are another matter:
`verack` calls `promote_connection` directly, from `Node`'s thread. So
what decides whether a piece of state needs a lock is which thread
reaches it, never which callback names it — `handle_p2p`,
`handle_p2p_handshake` and `handle_rpc` above run on `Node`'s own loop,
and so does `update_chain` beside them. `Mempool` is reached from that
one thread and no other, its `add_tx` and `remove_tx` being called from
the p2p callbacks, the rpc callbacks and `update_chain`: its own
"handled in same thread" comment needs no lock to back it, and a `cast`
standing on the same invariant needs no runtime check either. `PeerDB`,
the address book above, is not so lucky: `add_active_address` arrives
from the `verack` callback on `Node`'s thread, `get_active_addresses`
from `manage_connections` on `P2pManager`'s, and `add_addresses` from
both — gossip on one thread, a DNS answer on the other. It carries two
locks for that reason, one per table, taken separately and never nested.

`tests/unit/` mirrors that layout and `tests/functional/` builds a node
and speaks to it over a socket.

## Following Bitcoin Core

Where this tree reimplements something Bitcoin Core also does — a
constant, an eviction order, the error an RPC answers a refusal with —
it **matches Core's behaviour, always**, and the comment beside it names
the commit Core was read at. The only licence to differ is the
language: Python-native, fluent or efficient in Python where Core's own
line is shaped by C++, never a design weighed against Core's on its own
merits. What differs from Core in consensus or in relay is a difference
the network sees, so the default is not a matter of taste.

The citation itself reads `at bitcoin/bitcoin@<sha>`, with `at`
immediately before the sha **on the same physical line as it**: an
identifier directly followed by a parenthesised list ending in the bare
citation parses as a Python call, which is what ruff's `ERA001` reads as
commented-out code, and `ERA001` walks one physical comment line at a
time, so an `at` left stranded on the line above by a wrap does nothing
for the line that actually carries the sha. `at` glued to the citation
defeats `ERA001` for a structural reason rather than an accidental one,
whatever else is on that line or encloses it: `at` and the citation's
own leading word are two consecutive names with no operator between
them, which is never valid Python (btclib-org/btclib-node#471). A
citation inside a docstring rather than a comment needs none of this:
`ERA001` only ever walks comment ranges.

`src/btclib_node/db.py`'s docstring is the worked example of that same
axis, not an exception to it: taking LevelDB's own fork through
`rocksdict`, a typed wheel on this tree's interpreter, over Core's
vendored LevelDB is a packaging choice, the packaging half of
Python-native rather than a design taken against Core's, and the
docstring argues the store against Core's on exactly those terms.

Mimicry is of the observed behaviour end to end, not of a local
`catch`: where a layer below differs, the same behaviour can need
different code above it, and that is still matching rather than
diverging. `db.py`'s own store verifies a per-block checksum on every
read (btclib-org/btclib-node#641), the same guarantee LevelDB's own
checksum gives Core's `CDBWrapper::Read` before its own deserialize
`try` ever runs. `UtxoIndex`'s two `Coin.parse` fallbacks answer
`None` for a checksum-clean record that still does not deserialize,
matching `CDBWrapper::Read`/`CCoinsViewDB::GetCoin`'s own "absent"
rather than raising (btclib-org/btclib-node#650) —
`ChainstateInconsistencyError`'s own docstring is where that
decision is argued end to end, uncomfortable half included.

A capability Core has that this tree lacks is a gap to close, not a
constraint to design around, wherever the reason is a library fact
rather than a decision: the store's per-block checksum is RocksDB's own
(btclib-org/btclib-node#641), taken in place of a stdlib `sqlite3` that
ships no checksummed VFS (btclib-org/btclib-node#637) rather than
designed around, that absence never having been this tree's own
design.

**A convention of this tree is not on that axis.** Where Core defines
the surface — an RPC's field names and what they mean, a message's
semantics — being consistent with the rest of this codebase is not a
reason to answer differently from Core, because the reader on the
other side is a client written against Core rather than against this
tree.

**Units are where that bites hardest.** A feerate here is satoshis per
kvB wherever one is emitted or read, and Core's `getmempoolinfo` answers
`mempoolminfee` in BTC per kvB. A field of Core's that this tree answers
takes Core's unit and not this one's: the client reading it was written
against Core, so the internally consistent answer is the one that is
wrong by eight orders of magnitude with nothing to say so.

The rule stops where the encoding is not this tree's to pick. BIP133's
`feefilter` carries satoshis per kvB on the wire because BIP133 says so,
not because either project chose it, and matching Core there is matching
the protocol.

What is not legitimate is the silent kind. Where the behaviour departs
from a source the code cites, the code says so and says what forced it.
A reader who finds a citation and an unexplained difference cannot tell
a decision from an oversight, and will reproduce whichever one they
guess. Reading Core's own source is part of writing the divergence and
not only part of matching it, because what gets reproduced is what Core
does rather than what a report said it does.

A checkout of Core is kept beside the primary checkout of this
repository, and is where Core is read rather than fetched a file at a
time over the network: a raw fetch can come back truncated with nothing
to say so, missing the very function a divergence question is about and
answering confidently from what is left, where the local checkout
answers the same question with one `grep`. It is brought forward with a
fetch and a fast-forward on a clean `master`, never written to, the same
way the primary checkout below is kept current; the sha it lands on
afterward is what the comment beside a divergence cites, in place of
"master, some time today".

The other sha a citation may carry is a released Core's, and where it
does, the release's own tag name sits beside it: a claim about the
behaviour of the bitcoind this tree is tested against, rather than
about Core's tip, is cited at that release's tag commit.
`.github/workflows/integration-bitcoind.yml` pins which release that
is, and is where a citation reads it rather than copying the version
here. The tag name is what tells such a citation from a stale read,
because ancestry does not: a sha read from an out-of-date `master` is
an ancestor of `master` all the same, so
`git merge-base --is-ancestor <sha> master` answering false finds a
release commit and still cannot say whether its author meant one.

That checkout sits at `../bitcoin` from there, not from whichever
worktree a session is working in — every session works in one, by the
rule below — so a plain `../bitcoin` typed from a worktree resolves to
nothing. The primary checkout is always `git worktree list`'s own first
entry, from any worktree of this repository, so its sibling is reached
from anywhere with

```shell
git worktree list --porcelain | awk '/^worktree /{print $2; exit}'
```

## The primary checkout is the maintainer's

**Never work in it.** No edit, no `git add`, no commit, no branch
switch, no rebase, no `git stash` — the hooks fix files in place. It is a
local reference only, and it stays on `main`.

Reading it is fine, but `git fetch` moves `refs/remotes/origin/main` and
leaves the work tree where it was, so a `grep` or a `Read` against the
checkout answers for whenever it was last brought forward, not for now.
The read that cannot go stale is `git show origin/main:<path>`: it
answers from the ref `git fetch` just moved, never from the tree.

Where the checkout has to be current rather than merely readable, a
fast-forward of a clean `main` brings it up:

```shell
git fetch origin && git merge --ff-only origin/main
```

That writes no commit, switches no branch and runs no hook, so it is on
the permitted side of *never work in it*, not an exception to it. Stop
if the checkout is not on `main` or is not clean: that is no longer
bringing it forward.

**Every session works in a worktree**, its own, from the first edit,
named `wt-<tracker>-<issue>-<repo>-<role>` rather than after the issue
alone. `tracker` is the repository whose issue tracker holds the issue:
an issue number is unique only within one tracker, so
`btclib-org/.github#45` and `btclib-org/btclib#45` are different issues
that would otherwise name the same worktree. `issue` is what prevents
the collision that has actually happened — two worktrees of different
work sharing a generic basename in one repository's own `.git`, keyed on
its path's basename. `repo` prevents a different collision, a *path*
one rather than a `.git` one: two repositories each keep their own
`.git/worktrees/<basename>` and cannot collide there, but the workers of
one session share one scratchpad directory, so a session carrying one
issue into several repositories computes the same target path for each
of them, and `git worktree add` refuses a directory that already
exists — or worse, a second worker reads the first one's tree; naming it
this way also sorts every worktree of one issue together. `role` covers
the narrower case of a coder and its reviewer holding a worktree at
once, which the ordinary sequence avoids by each removing its own.

An issue in `btclib-org/.github`'s tracker, worked in `btclib` by a
coder, names its worktree `wt-github-255-btclib-coder`. The `uv sync`
gives the worktree a `.venv` of its own rather than a shared one, and
the editing, the gates and the commits all happen there before the push.

```shell
WT=<scratchpad>/wt-<tracker>-<issue>-<repo>-<role>
git worktree add "$WT" origin/main -b <branch>
cd "$WT" && uv sync
git push origin HEAD:refs/heads/<branch>
```

`-b <branch>` sits after the path and the commit-ish so that the
placeholder ends the command, which is the rule in section 9 of
[btclib-org/.github's
README](https://github.com/btclib-org/.github/blob/main/README.md). With
the placeholder ahead of `"$WT"` the `>` closing it takes that path as
its target, and a path with no directory at it is a file the paste
creates.

Removing the worktree is part of finishing, and it stands in a block of
its own: the block above ends in a placeholder, and a shell that
discards that line as a parse error reads the next as a fresh command —
which, in one block, is this line against whatever `$WT` already held.
Standing alone it is a second fence, so `${WT:?}` is what it writes:
with no `$WT` set the expansion fails and the removal does not run.
That is the only case it catches: a `$WT` left over from an earlier
session or command is set, so it expands, and the removal runs against
whatever worktree that value names.

```shell
git worktree remove --force "${WT:?}"
```

**Never `git stash` in a worktree either: `refs/stash` is shared.** A
worktree isolates files, not refs, so `git stash push` pushes onto the
same stack every other session pops from. Commit to your own branch
instead.

**It does not isolate objects either, which is how an unpushed branch
reads as pushed.** A commit written in a worktree is in that one shared
object store the moment it exists, so `git cat-file -t`, `git show
<sha>:<path>`, a diff and `git log --format='%h %G? %GS'` all answer for
it exactly as they would after a push -- the right content, a good
signature, nothing stale and nothing erroring. Hand that sha to a
reviewer and it confirms every one of those, having read something the
forge does not have. The ref is the only read that answers, and what it
answers with is compared against the sha you sent:

```shell
git -C "$WT" fetch origin
git -C "$WT" rev-parse "origin/<branch>"
```

Twice in one campaign a branch was reported pushed while `origin/` still
held the pre-rebase commit, the second time to a reviewer that caught it
only by resolving the ref rather than the bare sha
(btclib-org/btclib-node#783, btclib-org/btclib-node#806). Push with
`--force-with-lease=<ref>:<expected old sha>` so a concurrent writer is
refused rather than clobbered, and read the ref back before saying the
word.

**Do not rewrite `refs/heads/main`, or advance it with work that is not
yours.** Your own branch is what you push, and the pull request is what
moves `main`.

## Model

The default model for this repository is Sonnet. Switch to Opus only for
architectural decisions with conflicting constraints — a trade-off with
no obvious side, a refactor crossing modules whose dependencies are
unclear, a diagnosis whose symptom does not point at its cause. Use
`/model opus` for the session, then switch back.

Do not use Fable unless explicitly instructed.

## Non-obvious facts that will otherwise waste a session

- **A trailing comment on the version line of `.python-version` makes uv
  ignore the whole file.** The reasoning for the pin is therefore in the
  lines above it, which is not where a reader looks first.
- **The suite binds ports.** `addopts` carries `-n auto`, so a run is
  parallel, and a functional test starts a node on a port it picked. A
  machine loaded past its core count is where the per-test `timeout`
  starts deciding runs; measuring anything on one is measuring the
  machine.

  **What that decides presents as a timeout and never as an assertion**,
  which is the discriminator to reach for while holding a red run, and
  the three coverage-floor bullets below do not supply it: each of those
  is a run with **every test green**, says so as its own discriminator,
  and therefore rules itself out for a reader looking at actual `F`s --
  who is then left concluding the branch broke something. Measured at
  one-minute load 107 on this ten-core machine, another session's suite
  live, on a diff touching no `.py` file at all: `test_download` raised
  `WaitTimeoutError` and `test_rev_patch` hit its own per-test bound,
  and the same sha at load 7.96 was `1441 passed, 3 skipped` with the
  floor met (btclib-org/btclib-node#807).

  **The two are not the same kind of test, and that is the half worth
  keeping.** `test_download` is `tests/functional/`'s, starts nodes and
  waits on them, and is what the sentence above already predicts.
  `test_rev_patch` is a unit test -- `tests/unit/chainstate/`'s -- that
  builds a twenty-thousand-block chain in process and unwinds it,
  touching no socket and no node at all: it is merely slow, and a
  machine at ten times its core count is enough to push it past its
  bound. So what discriminates is the timeout itself and never the
  directory the test sits in -- which is `pyproject.toml`'s own design
  rather than an induction from these two: the comment beside its
  `timeout = 300` argues one measured global bound against per-test
  markers, and is where the tests that bound was measured against are
  named and re-measured. An assertion failure is not this shape, and
  load does not explain one.
- **Collection under `tests/unit` and `tests/functional` follows
  pytest's own default `python_files`** (`test_*.py`, `*_test.py`).
  `pyproject.toml` carries no override, and its own comment beside
  `testpaths` says why.
- **A second `pytest` anywhere in this tree, even `--help`, erases a
  running suite's coverage data, unless `COVERAGE_FILE` points outside
  the rootdir.** `addopts` names no `data_file`, so every invocation from
  this rootdir shares `.coverage*`; `pytest-cov` erases that set at
  configure time unless `--cov-append` is given, and under `-n auto` the
  erase sweeps every parallel-suffixed file, which is what a running
  suite's own workers are writing. `--help` reaches it because `pytest`'s
  own `helpconfig` still calls `_do_configure()` before printing
  anything. The mechanism runs inside `pytest-cov`'s own `tryfirst` hook
  on `pytest_load_initial_conftests`, before this tree's `conftest.py` is
  even imported, so nothing in this repository's pytest configuration
  intercepts it. `COVERAGE_FILE` does, because `coverage.py` reads it
  from the environment rather than through that hook chain:
  `COVERAGE_FILE=$(mktemp -d)/.coverage uv run pytest` keeps a run's data
  out of reach of a second invocation in the same rootdir. A same-prefix
  name still inside the rootdir, such as `.coverage.protected`, does not
  help: a concurrent plain invocation's own erase still reaches it,
  because `erase(parallel=True)` globs its own base filename plus `.*`
  in that base's directory, and `.coverage.protected...` starts with the
  plain default's `.coverage` followed by a literal dot. Only leaving
  the directory removes the file from that glob's reach. Left unset, the
  run reports a coverage number with every test passing, which reads as
  a real regression and is not one: btclib-org/btclib-node#191.
- **The coverage floor can fail under a loaded machine with every test
  passing.** This is a different way the number lies from the one above:
  the miss is a branch inside `P2pManager`'s own background thread, on a
  line no test asserts anything about directly, and the run reports
  under the 100% floor — `99.98%` in the run
  [ISS 372](https://github.com/btclib-org/btclib-node/issues/372)
  measured — with every test green and no `F` among the progress marks.
  That is the discriminator: a run failing only the coverage line, with
  the rest of it clean, is not evidence the branch under test caused it.
  The load average `uptime` gives at the run, not read back afterward, is
  the number ISS 372 records beside each of its runs, and the one worth
  recording here too.
- **The coverage floor can also fail by losing a whole worker's share of
  one file, at ordinary load, and not reproduce.**
  [ISS 617](https://github.com/btclib-org/btclib-node/issues/617) is
  where that happened once: dozens of pre-existing lines across many
  unrelated functions of `main.py` reported missing, every test green,
  `uptime` ordinary (2.20) rather than the load ISS 372's own run
  carried (~19) — the shape neither ISS 372 nor
  [ISS 319](https://github.com/btclib-org/btclib-node/issues/319)
  is, both of those being one line or branch tied to the timing of one
  function. The candidate mechanism is `xdist`/`pytest-cov`'s own
  parallel-data combine silently losing one worker's `.coverage.*` file
  — and that silence is real: `pytest-cov`'s `DistMaster` never passes
  `messages=True` to the `coverage.Coverage()` it drives its `combine()`
  through, so a dropped or duplicate-skipped file changes nothing a
  stock run prints, in either direction. Seven whole-suite runs on this
  repository's ten-core machine, five at the default `-n auto` and two
  at an oversubscribed `-n 20`, each with `COVERAGE_DEBUG=dataio,combine`
  and `COVERAGE_DEBUG_FILE` pointed outside the rootdir, every one
  combining exactly one file per worker plus the master's own and
  losing none of them, did not reproduce it — on the same
  coverage 7.15.4 / pytest-cov 7.1.0 / pytest-xdist 3.8.0 this tree
  pinned at ISS 617's own sha, so this is not something a version bump
  already fixed underneath the report. Nothing here is changed for it:
  ISS 617 itself carries the argument for why a config change with no
  demonstrated mechanism behind it is worse than the flake. Where this
  shape recurs — many lines across several functions of one file
  missing, at ordinary load, every test passing, every other file whole
  — rerun once before trusting it, and rerun with that same
  `COVERAGE_DEBUG` pair set if it persists, since that is the only way
  a dropped file leaves a mark a plain `uv run pytest` does not.
- **The coverage floor can also fail on one statement of a test's own
  helper, at ordinary load, and not reproduce.**
  [ISS 762](https://github.com/btclib-org/btclib-node/issues/762) is
  where that happened: one statement of a coroutine defined inside a
  test in `tests/unit/p2p/manager_test.py` reported missing, every test
  green, `uptime` ordinary, and the next run of the same suite in the
  same worktree met the floor. Neither of the two above is that shape —
  ISS 372's miss is a branch of `src/` under heavy load, ISS 617's is a
  whole file's worth of lines. The coroutine stands in for
  `P2pManager.server` for the length of one test, and its body runs
  only where the loop gives that task a first step before `stop`
  cancels it; the test asserts on `manager._server_sockets` and never
  on the coroutine having run, so the scheduler decides whether
  coverage sees the line while the test passes either way. The
  discriminator is the missing line itself: a statement in a test
  helper whose execution nothing asserts on is not a report about the
  branch under review, and one rerun settles it.
- **A mutation applied outside the runner's own process never reaches an
  `-n auto` worker, and the guarded test passes.** A wrapper that
  monkeypatches an attribute and then calls `pytest.main` mutates the
  controlling process; each worker `-n auto` spawns imports the module
  from disk in its own subprocess, unmutated, and runs the test against
  the original code. Verifying that a test can fail means editing the
  file the mutation targets and reverting it afterward, since a worker
  reads the file rather than the controlling process's state — proving
  the revert with `git diff --stat` or a grep for a marker the mutation
  left, rather than trusting that it ran.
- **`.hypothesis/` is per-worktree and outlives the diff that provoked
  it.** `.gitignore:50` covers it, `tests/conftest.py`'s `default` and
  `thorough` profiles name no `database`, and hypothesis's own
  `DirectoryBasedExampleDatabase` therefore records every failing
  example a property test finds under `.hypothesis/examples` and
  replays it on every later run in that same worktree. Measured by
  forcing `tests/property_test.py` to fail on a value random search
  rarely reaches on its own and reverting the test: the worktree that
  had recorded the value fails on it again, deterministically, while
  a worktree with no such record passes, on the same code and the same
  default example budget. A red property test that will not reproduce
  outside the worktree that raised it is not evidence of a flake for
  that reason alone — removing the worktree rather than reusing it is
  what a fresh reading needs (btclib-org/btclib-node#835).
- **A `.venv` reused from another worktree imports that worktree's
  `src/`, not the caller's.** `site-packages/btclib_node.pth` is a plain
  absolute path written at `uv sync` time and does not follow `cwd`.
  Confirm which `src/` an interpreter actually loaded before trusting a
  test result against it —
  `python -c "import btclib_node; print(btclib_node.__file__)"`, run
  with the same interpreter, `cwd` and environment as the test
  invocation, and read before the result rather than after.
- **The store is RocksDB through `rocksdict`**, since
  btclib-org/btclib-node#641, which reversed btclib-org/btclib-node#107's
  stdlib `sqlite3`. A datadir written by the sqlite3 store, marked by
  its `index.sqlite`, cannot be read and is refused by name;
  `src/btclib_node/db.py` is where that is handled and where the choice
  is argued against Bitcoin Core's.
- **`gh api`'s `-f` always sends a string, even for a boolean field.**
  `gh api -X PATCH .../required_status_checks -f strict=true` fails
  with `"true" is not a boolean`, because `-f`/`--raw-field` encodes
  every value as JSON text. `-F`/`--field` is the typed form and is what
  a boolean, a number, or a `contexts[]=` array element needs.
  `REPOSITORY.md`'s own documented branch-protection command carried
  this mistake, unexecuted, since btclib-org/btclib-node#264 landed it;
  btclib-org/btclib-node#453 is where the follow-up PATCH was actually
  run and the command corrected.
- **`autodoc_typehints_format` and `autodoc_type_aliases` cannot resolve
  a `TYPE_CHECKING`-only annotation, whatever they are set to.** Sphinx
  falls back to the bare source string for an annotation it can never
  import, and both settings only reformat a type hint autodoc already
  resolved. `autodoc_type_aliases` needs PEP 563's `from __future__
  import annotations` to engage at all, which only one module in this
  tree still carries — the rest reach PEP 649's native lazy evaluation
  on this tree's `>=3.14` target instead, so the setting doesn't rescue
  most of what it's tried against — confirmed by mapping every
  remaining name and rebuilding, with no change in the warnings.
  btclib-org/btclib-node#417 is the cross-reference ambiguity this
  forced a rename to fix rather than a `conf.py` setting;
  btclib-org/btclib-node#264's own `nitpick_ignore` list is the same
  wall met a second time.
- **A `merge=union` file makes a local rebase's silence no evidence at all,
  whichever of the forge's own signals is read next.**
  GitHub's server-side merge check does not apply git's own `union` merge
  driver, so `git rebase origin/main` in a worktree, with the same
  `.gitattributes` as a pull request touching only `CHANGELOG.md`, can find
  nothing to resolve by hand where a real three-way merge under the same
  anchor fails. That silence is the driver picking both sides, in an order
  nobody chose, which is what `union` is for — a driver built never to
  conflict cannot ever report one, clean or not. `git merge-tree --write-tree`
  answers the same way and is not a dry run of this question either: it too
  reads `.gitattributes` from the trees it merges and writes the fused blob at
  exit `0`.

  **That is about conflict detection, and it is not licence to skip the
  command — read the blob it writes, not its exit code.** merge-tree
  writes the tree the rebase will produce, so the damage is there with a
  line number, before a branch is touched, and three real rebases
  elsewhere produced trees byte-identical to what it had predicted. The
  sharpest case is a merge that *does* conflict:
  `origin/iss-507-381-interpreter-window-and-btclib-floor` exits `1` with
  stage-1/2/3 entries for seven source paths, `CHANGELOG.md` appears in
  that same output only as `Auto-merging CHANGELOG.md`, and the tree is
  written anyway — `eaa8f182`, flush heading at line 2853. One run
  showing both halves: merge-tree is not blind to conflicts, it finds
  seven; the union file passes silently through it; the blob is what
  answers. Of the forge's own two signals, `gh pr view --json mergeable` is
  not the one to trust: it is an asynchronous, cached read that can still
  answer `UNKNOWN` on a pull request already `MERGED`, so a `CONFLICTING` seen
  there is not yet confirmed real. The merge the endpoint actually attempts
  (`gh api -X PUT .../merge`) is a genuine three-way merge computed at that
  moment, and its refusal is the true report. `RELEASING.md`'s step 3 has the
  check that tells a safe rebase from a fused one, by reconstructing the file
  rather than reading the rebase's silence, and it is what a rebase across a
  union file is checked against.

  **The blank line it eats is what later inverts the order**, so the two
  are one defect and not two. The driver drops the blank line before the
  arriving entry's own heading -- damage on its own, and what
  `markdownlint-cli2 --fix` writes back on the next hook run, leaving a
  worktree dirty after a gate already reported clean. A branch carrying
  that loss into a *second* rebase across the same boundary then has its
  entry placed **above** the one already there rather than below,
  because the missing line is the context the driver orders on. Measured
  seven times in one day: six carried their blank line and landed below,
  the seventh had already lost it and landed above. Isolated on the real
  case -- restoring that one line in the branch's own commit, changing
  nothing else, and rebasing onto the same base puts the entry back
  below (btclib-org/btclib-node#610).

  That the order is *mechanical* rather than arbitrary is what makes it
  worth stating: an entry that arrives above one already there is not
  bad luck, it is a report that this branch was rebased twice and lost
  its blank line the first time.

  **The eaten line is not silent: the lint gate is a witness.** Fed a
  damaged `CHANGELOG.md`, `markdownlint-cli2` exits 1 naming
  `MD022/blanks-around-headings` and `MD032/blanks-around-lists` at the
  line, against the repaired file at exit 0 as the control, and the
  hook's own `--fix` restores it byte for byte. The rule and the autofix
  are section 9 and section 4 of btclib-org/.github's README,
  respectively; what is worth adding is that a
  detector the tree already runs beats building one, and that this one
  says nothing about the misplacement -- a copy with two entries
  swapped lints clean.

  **The unit is the *seam*, not the rebase and not the branch.** One
  blank line eaten per seam, a seam being where the two sides' added
  blocks abut in the fused output. A blank line internal to one side's
  own addition is not at a seam and survives:
  `origin/workflows-say-what-they-hold` arrives with three entries,
  makes one seam against the base's last bullet, and loses one line of
  the three. `origin/iss-547-changelog-boundary` makes two insertions
  in one merge -- prose into the preamble, a `###` lower down -- and
  loses the line at the second and not the first, the same branch and
  the same merge answering both ways, which is the case that isolates
  the seam from everything else. Eighteen seams measured across two
  sessions in one day, eighteen eaten, and no case yet of two blocks
  abutting with the line surviving.

  **What does not predict a seam is the hunk header**, recorded so that
  it is not re-invented: anchor equality is necessary and not
  sufficient, and `grep -m1` reads the wrong hunk on a branch making two
  insertions -- `iss-547` seams at its second while its first differs
  from `main`'s by two lines. There is no `grep` shortcut. merge-tree is
  the answer.

  **The repair is a rebuild, and the check is a full comparison.**
  Reconstruct the file as `origin/main` plus exactly the lines the
  branch adds over it, appended at the end of the open section, and
  compare the whole thing byte for byte -- **not** merely that nothing
  was *removed* from the base, which a misordering passes, having
  repositioned rather than deleted; `RELEASING.md`'s step 3 is where
  that same insufficiency is already argued. Run a control that mutates
  the reconstruction too, since a comparison that cannot see a
  difference reports a match for the wrong reason: an expected file
  built by copying the arriving block rather than by normalizing it
  inherits the missing blank line and matches the damage it was meant to
  catch, which is how two branches reached review with the markdown gate
  red.

  **A no-seam case has to be shown to be a case at all**, or the whole
  comparison reports a match for the emptiest reason there is.
  `origin/iss-586-finish` and `origin/iss-586-utxo-cache-across-blocks`
  look like perfect negatives -- anchors coinciding, nothing eaten --
  and their fused blobs are byte-identical to `main`'s file, because the
  entry they add is already on `main` (`1ce7b20`, #643). Nothing
  arrives, so there is no second side and no seam, and *every* check
  passes, the rebuild among them, the reconstruction matching too. The
  measurement is correct and answers a different question than the one
  asked: *did the line survive* is not *does a seam eat one*. The guard
  is one `cmp` of the fused blob against the base, with a real arrival
  as the positive control.

  **That generalizes, and is the thing to carry out of this bullet: a
  check that can only report the absence of damage passes on an input
  that cannot carry the damage -- silently, and correctly.** This tree
  met the shape three times on 2026-08-30 and no instance was found by
  anything going red.

  **What says whether a given check needs a guard is whether its input
  can go empty without anyone choosing it.** A walk can -- a
  `parametrize` over a walk that finds nothing collects nothing and
  reports no failure, and a refactor nobody thinks of as touching the
  test is enough to empty it, which is why `tests/property_test.py`
  asserts its own walk is non-empty. A module literal cannot: a count
  set to zero is a deliberate act, and demanding a guard there is
  ceremony. So a sibling tree whose equivalent layer is driven by a
  literal rather than a walk needs no such guard, and that is the
  condition holding rather than an exception to it.

  The null case above is one instance. The badge render is another:
  `curl -s '<workflow>/badge.svg?branch=main' | grep -oE
  '<title>[^<]*</title>'` answered `test - failing` nine minutes after a
  green run and `test - passing` at the same moment with
  `-H 'Cache-Control: no-cache'` and a cache-busting parameter, so a
  `failing` from the bare form is indistinguishable from a stale copy
  and only a `passing` is evidence -- and a control on an already-green
  workflow cannot catch it, cached and fresh agreeing there. The third
  is the `-n auto` mutation that never reaches a worker, already in the
  list above.

  **A structural diff against the base is the cheaper check, and
  "nothing removed" is not enough there either.** Run
  `difflib.SequenceMatcher` over the base and the rebased file: a clean
  rebase is exactly *one* `insert` opcode and no `delete` and no
  `replace`, which catches a line lost from the base and a reordering
  of what the base already held. It does **not** catch the eaten line
  -- measured on this file's own rebase at btclib-org/btclib-node#753,
  a copy with the blank line eaten again reports the identical single
  `insert`. Drop a base line and watch a `delete` appear, or the opcode
  shape is measuring nothing.

  **Where the inserted block starts is not a fact about the file.** An
  entry is bounded by a blank line at each end, so at a clean append
  two alignments reproduce the rebased blob byte for byte -- one whose
  block begins with the blank line, one whose block ends with it -- and
  `difflib` reports whichever its own tie-break reaches, under
  `autojunk` either way, with nothing in its output saying the other
  exists. Enumerating every `j` for which `A[:j] + B[j:j+n] + A[j:] ==
  B` at btclib-org/btclib-node@4cf1370 finds both; the same enumeration
  over the damaged copy finds one, whose block begins with the heading.
  So `inserted[0] == ""` reports which alignment was reached and not
  whether the file is whole (btclib-org/btclib-node#771).

  **What discriminates is a statement about the text.** With `A` the
  new base's blob, `B` the rebased blob and `X` exactly the block the
  branch adds over its own old base: `X` occurs in `B` once, and
  `B.replace(X, "", 1) == A`. It needs no splice point, and it is
  indifferent to which alignment `X` was taken at -- both satisfy it on
  the whole file and both break it on the damaged one. The equality is
  the half an eaten blank breaks; the occurrence count stays at one
  there, the block's own leading newline being indistinguishable from
  the line terminator before it, and answers the stacked duplicate
  instead. Run the controls every time: `B.count(X + "zz")` and a
  perturbed `X` at zero, `B != A` against the vacuous pass above, and
  the blank re-eaten breaking the equality.

  **Neither of those says where the block sits.** `X` spliced above the
  entry that landed while the branch waited satisfies the identity and
  reports the same single `insert`, both asking what `B` holds rather
  than where. The misplacement is the byte-for-byte rebuild's to
  answer, which is why that one is the check and these are the cheap
  ones beside it.

  **The cheapest question is whether the base side moved at all.** A
  seam needs two sides, and `git diff --stat <old base> <new base> --
  CHANGELOG.md` answers for one of them: empty means the commits being
  rebased over never touched the file, so nothing can abut and nothing
  can be eaten, before any blob is read. It answered empty three times
  in one session -- `btclib-org/.github@b1aeb3a` and `6279da8` each
  touching only `.pre-commit-config.yaml`.

  **It is emphatically not the *no-seam case has to be shown to be a
  case* guard above, and reading it as one is worse than not running
  it.** That bullet is about the *arriving* side going empty, which
  this command cannot see: `origin/iss-586-finish` against the `1ce7b20`
  that already carried its entry answers **301 insertions**, and its
  fused blob is nevertheless byte-identical to the base, so nothing
  arrives and every check is vacuous exactly as that bullet describes.
  A non-empty answer here licenses nothing. The arriving side still
  wants its own `cmp` of the fused blob against the base, and the
  rebuild still runs after both -- it is what says the block landed
  where it belongs, which no amount of *nothing was eaten* answers.

  **A reconstruction is a control only once the block's own boundaries
  have been read.** Print the block's first and last lines and the
  anchor's neighbours before trusting any `cmp`: get the boundaries
  wrong and the splice duplicates a blank on one side and drops one on
  the other, which reads exactly like a two-line seam and sends a reader
  hunting a second defect that does not exist. It earns a line because
  it was the first-attempt outcome in both of two trees -- the default
  result of the obvious implementation rather than an edge case.

  **`RELEASE_NOTES.md` is not rebuilt the same way.** `CHANGELOG.md`'s
  open section is flat, so "the base plus exactly the branch's lines,
  appended at the end of the open section" is the right reconstruction
  there and only there. `RELEASE_NOTES.md` carries subsections under
  `## Unreleased`, and the driver's damage there is not confined to a
  blank line: at btclib-org/btclib-node#728's rebase it placed the
  branch's `### Breaking changes` bullet *inside* the `### Windows`
  subsection btclib-org/btclib-node#724 had landed meanwhile, nothing
  missing and nothing removed, so a "nothing lost from the base" check
  and an end-of-section rebuild both pass it -- the rebuild reproduced
  the same misplacement, and only reading the resulting diff caught it.
  Reconstruct `RELEASE_NOTES.md` against the entry's own anchor, the
  subsection heading it belongs under, and compare byte for byte with
  a control, as for the other file.

- **`CHANGELOG.md`'s own open section carries its citations on the
  headings, and a new entry does not follow that.** A coder deriving the
  convention from the file alone reads every `### heading` there closing
  the issue beside it and would write the next one the same way; section
  9 of btclib-org/.github's README, on `CHANGELOG.md` and
  `RELEASE_NOTES.md`, now says the citation sits in the bullet making
  the claim, not on the heading above it, precisely
  because a heading answering for the entry as a whole leaves the
  bullets under it free to name issues of their own with nothing saying
  how the two sets relate — which is what this tree's own
  `### btclib-node reads bitcoin.conf (closes #583, closes #581, closes
  #573)` heading and its bullets citing `#577` and `#589` separately
  already show. Nothing already landed is rewritten for it (*Nothing
  already written is rewritten*, same subsection): the next entry's
  heading takes no citation, and each of its bullets cites the issue it
  answers for (issue btclib-org/.github#586).

- **`sysctl -n vm.loadavg` prints comma decimals under this machine's
  locale** (`{ 3,57 4,10 4,22 }`), so a shell wait-for-load loop that
  strips the fraction with `${l%.*}` never parses a number and waits
  its whole timeout on every run, reading as a machine that is always
  loaded. `${l%%[.,]*}` strips either separator. Measured on
  2026-08-29, after every suite of one session had waited ten minutes
  for a load that was under the bound the whole time.

- **The docs build is a third gate, and it does not refuse every closing
  backtick directly followed by a bare letter.** `CONTRIBUTING.md`
  names three gates; a report naming two reads as complete, and the
  reader supplies the third from memory. `` `Coin`s `` only fails the
  build where its paragraph carries no *later* single backtick:
  docutils, having rejected the one right after `Coin` as an
  end-string, keeps scanning the same block rather than stopping
  there, and a backtick further down closes a title-reference that
  swallows everything in between -- legal, silent, and wrong on the
  rendered page. `src/btclib_node/db.py`'s own module docstring
  carries the construct this way, autodoc renders it, and the docs
  gate is green over it: the built page's `<cite>` runs from `Coin`
  through two more sentences and closes on `` `Chainstate.close` ``,
  and `src/btclib_node/p2p/connection.py`'s module docstring does the
  same, rescued by a later `` `P2pManager` ``. Only a paragraph with no
  rescuing backtick at all draws "Inline interpreted text or phrase
  reference start-string without end-string", which `sphinx-build -W`
  turns into exit 1 -- reproduced on a scratch page carrying the
  identical sentence with nothing after it to rescue it, one warning
  at the line the unrescued backtick opens on
  (btclib-org/btclib-node#784). `pytest` and the lint gate stay green
  in both cases, catching neither. Autodoc's own exemption still
  holds beside this: a leading-underscore function or method and a
  dunder method such as `__del__` are never rendered without
  `:private-members:` or `:special-members:`, so `_default_worker_count`
  and `_tasks` carry the identical construct and neither docstring
  ever reaches the build.
- **`caplog` cannot see anything this tree's logger emits, and fails
  silently when asked to.** `Node.logger` is a `Logger(logging.Logger)`
  instantiated directly rather than through `logging.getLogger()`, so
  `logger.parent` is `None` and no record ever propagates to the root
  logger pytest's capture handler sits on. `caplog.records` stays empty,
  which means a test asserting against it passes by asserting nothing.
  This is not a matter of the message needing to be parsed out of a
  traceback: it is no visibility at all, in any test in this tree. A
  test that has to observe this logger attaches a handler to it, or
  reads back the file `log_path` names, and proves it can fail before
  it is believed (btclib-org/btclib-node#587).
- **Another session may be working this same tree at the same time, and
  the tracker is not enough to tell.** Two sessions produced two
  branches for one issue within two minutes, with near-identical diffs
  and neither having skipped a check: the first ran `gh pr list` and
  `git worktree list` before starting and found nothing, the other
  branch not existing yet. `ListAgents` lists the peer sessions on the
  machine and `SendMessage` reaches them, which is how that collision
  was settled and how the two then divided the tracker between them.
  This repository's own `git worktree list` is the cheapest live signal:
  another session's worktrees are in it, under that session's own
  scratchpad path.

## Conventions to match

Section 9 of [btclib-org/.github's
README](https://github.com/btclib-org/.github/blob/main/README.md) is
the prose style, and it governs this file and the code alike. It is not
re-listed here, that section's own *One fact in one place* being the
reason. `CONTRIBUTING.md`'s *Pull requests* has what a title does with
the issue it closes, and its *The issue tracker* has what an issue filed
here may be about.

What is left to this file is what those cannot say, because it is about a
session rather than about the tree: the worktree rule, the model, the
failure modes in the section that names them, and what this tree is.

## Verifying

Run the command as documented before claiming it works, and read its exit
code rather than its filtered output, for the reason `CONTRIBUTING.md`'s
*This repository in particular* gives. Every claim in this file was
checked against the tree, and the tree changes.
