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

`tests/unit/` mirrors that layout and `tests/functional/` builds a node
and speaks to it over a socket.

## The primary checkout is the maintainer's

**Never work in it.** No edit, no `git add`, no commit, no branch
switch, no rebase, no `git stash` — the hooks fix files in place. Reading
it is fine, and so is `git fetch`, which writes refs and leaves the work
tree alone.

**Every session works in a worktree**, its own, from the first edit:

```shell
WT=<scratchpad>/wt<issue>
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
- **The suite reaches the network and binds ports.** `addopts` carries
  `-n auto --remote-data=any`, so a run is parallel, and a functional
  test starts a node on a port it picked. A machine loaded past its core
  count is where the per-test `timeout` starts deciding runs; measuring
  anything on one is measuring the machine.
- **`python_files = "*.py"`**, so every module under `tests/unit` and
  `tests/functional` is collected, suffix or no suffix, and a helper put
  there is collected too.
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
