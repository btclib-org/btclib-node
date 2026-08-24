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

`Node` in `btclib_node/__init__.py` is a thread running one loop: it
drains the handshake queue, then a share of the RPC queue and a share of
the peer-to-peer queue, then steps the download manager and extends the
chain. A message that raises is logged and the loop continues; a failure
under `update_chain` leaves it, because the databases below have to be
closed on the way out.

- `btclib_node/p2p/` is the protocol — the connections, the peer
  manager, the address book and the message handlers the loop calls.
- `btclib_node/rpc/` is the JSON-RPC surface, on the same shape of
  manager and handler.
- `btclib_node/chainstate/` is the block index, the UTXO set and the
  compact filter index; `btclib_node/block_db/` is the blocks and their
  undo data.
- `btclib_node/db.py` is the ordered key-value store all of those are
  kept in. Its docstring is where the implementation is argued, and
  **key order is load-bearing**: a reader that stops at the first key
  without its prefix is truncated by a prefix that sorts before it.
- `btclib_node/interpreter.py` validates, and `Node.worker_pool` is what
  it validates a fork across.

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

Not always, though. This tree has constraints Core does not share, and a
divergence one of those forces is legitimate: `btclib_node/db.py`'s
docstring is the worked example, arguing its store against Core's.

What is not legitimate is the silent kind. Where the behaviour departs
from a source the code cites, the code says so and says what forced it.
A reader who finds a citation and an unexplained difference cannot tell
a decision from an oversight, and will reproduce whichever one they
guess. Reading Core's own source is part of writing the divergence and
not only part of matching it, because what gets reproduced is what Core
does rather than what a report said it does.

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
alone: a worktree's administrative directory lives in the one shared
`.git`, keyed on its path's basename, and one issue is routinely owed by
several repositories of the organization, so a name that dropped the
repository collided silently between them. `issue` is what prevents a
collision between two different issues sharing a generic name; `role`
covers a coder and its reviewer holding a worktree at once, which the
ordinary sequence avoids by each removing its own.

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
- **The store is `sqlite3` from the standard library**, since
  btclib-org/btclib-node#107. A datadir written by the LevelDB this
  replaced cannot be read; `btclib_node/db.py` is where that is handled
  and where the choice is argued against Bitcoin Core's.

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
