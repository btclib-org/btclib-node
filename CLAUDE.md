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
  their undo data.
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
it **matches Core's behaviour wherever that is possible and
reasonable**, and the comment beside it names the commit Core was read
at. What differs from Core in consensus or in relay is a difference the
network sees, so the default is not a matter of taste.

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

Not always, though. This tree has constraints Core does not share, and a
divergence one of those forces is legitimate: `src/btclib_node/db.py`'s
docstring is the worked example, arguing its store against Core's.

**A convention of this tree is not one of those constraints.** Where
Core defines the surface — an RPC's field names and what they mean, a
message's semantics — being consistent with the rest of this codebase is
not a reason to answer differently from Core, because the reader on the
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

It sits at `../bitcoin` from there, not from whichever worktree a
session is working in — every session works in one, by the rule below —
so a plain `../bitcoin` typed from a worktree resolves to nothing. The
primary checkout is always `git worktree list`'s own first entry, from
any worktree of this repository, so its sibling is reached from
anywhere with

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
git fetch origin && git merge --ff-only origin/main   # clean main only
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

```shell
WT=<scratchpad>/wt-<tracker>-<issue>-<repo>-<role>  # wt-github-255-btclib-coder
git worktree add -b <branch> "$WT" origin/main
cd "$WT" && uv sync                   # its own .venv, not a shared one
# edit, gate and commit here, then
git push origin HEAD:refs/heads/<branch>
git worktree remove --force "$WT"     # removing it is part of finishing
```

**Never `git stash` in a worktree either: `refs/stash` is shared.** A
worktree isolates files, not refs, so `git stash push` pushes onto the
same stack every other session pops from. Commit to your own branch
instead.

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

- **`btclib` is resolved from its `main` branch**, not from a release:
  `[tool.uv.sources]` says so and says why, and `uv.lock` pins the
  commit. So `uv sync` needs `git` and builds a source distribution, and
  moving onto a newer btclib is a re-lock and a decision rather than a
  refresh.
- **A trailing comment on the version line of `.python-version` makes uv
  ignore the whole file.** The reasoning for the pin is therefore in the
  lines above it, which is not where a reader looks first.
- **The suite binds ports.** `addopts` carries `-n auto`, so a run is
  parallel, and a functional test starts a node on a port it picked. A
  machine loaded past its core count is where the per-test `timeout`
  starts deciding runs; measuring anything on one is measuring the
  machine.
- **`python_files = "*.py"`**, so every module under `tests/unit` and
  `tests/functional` is collected, suffix or no suffix, and a helper put
  there is collected too.
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
- **The store is `sqlite3` from the standard library**, since
  btclib-org/btclib-node#107. A datadir written by the LevelDB this
  replaced cannot be read; `src/btclib_node/db.py` is where that is
  handled and where the choice is argued against Bitcoin Core's.
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
- **A `merge=union` file can make GitHub report a conflict a local
  rebase resolves cleanly.** GitHub's server-side merge check does not
  apply git's own `union` merge driver, so a pull request touching
  only `CHANGELOG.md` can show `mergeable: CONFLICTING` while
  `git rebase origin/main` in a worktree, with the same
  `.gitattributes`, finds nothing to resolve by hand. Rebase and look,
  rather than searching the file for a conflict the banner did not
  actually find.

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
