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

[ARCHITECTURE.md](./ARCHITECTURE.md) is the design: the loop `Node`
runs, the protocol and the RPC surface around it, the store, and which
thread each piece of state belongs to. Read it before touching
`src/btclib_node/p2p/` or `src/btclib_node/rpc/`, where what decides
whether a piece of state needs a lock is which thread reaches it, never
which callback names it.

## Following Bitcoin Core

`CONTRIBUTING.md`'s *Following Bitcoin Core* is the rule for matching Core.

## The primary checkout is the maintainer's

Never work in it: no edit, no `git add`, no commit, no branch switch, no
rebase, no `git stash` — the hooks fix files in place. The one write
allowed there brings it forward, and only while it is on `main` and
`git status --porcelain` prints nothing; where it is not, stop:

```shell
checkout=<checkout>
```

```shell
git -C "${checkout:?}" pull --ff-only
```

Read it only after that, once this prints one sha twice:

```shell
git -C "${checkout:?}" rev-parse HEAD origin/main
```

A measurement that has to hold at a named revision reads
`git -C "${checkout:?}" show <sha>:<path>` instead.

Every session works in a worktree of its own, from its first edit, named
`wt-<tracker>-<issue>-<repo>-<role>` — `wt-github-255-btclib-writer` for
issue 255 of `btclib-org/.github`'s tracker, worked in `btclib` by a
writer. The environment is created there, with the command `CONTRIBUTING.md`
names under *The environment and the gates*. Every path is written out in
full, `<scratchpad>` being the session's scratch directory:

```shell
git worktree add \
  <scratchpad>/wt-<tracker>-<issue>-<repo>-<role> origin/main -b <branch>
```

Removing it is part of finishing:

```shell
git worktree remove --force <scratchpad>/wt-<tracker>-<issue>-<repo>-<role>
```

`refs/stash` and the local `main` are shared by every worktree: never
`git stash`, and move `main` only by the `git pull --ff-only` above.

## Model

Default model: Sonnet; Opus for design decisions with conflicting
constraints. Do not use Fable unless instructed.

## Non-obvious facts that will otherwise waste a session

- **A trailing comment on the version line of `.python-version` makes uv
  ignore the whole file.** The reasoning for the pin is therefore in the
  lines above it, which is not where a reader looks first.
- **A red suite with no assertion failure is usually the machine.** The
  suite binds ports and runs `-n auto`; under load a test fails as a
  timeout (`WaitTimeoutError`, or pytest-timeout's own bound), never as an
  assertion (btclib-org/btclib-node#807). The coverage floor can also fail
  with every test green: one `P2pManager` thread branch under load
  (btclib-org/btclib-node#372), a whole worker's share of `main.py` at
  ordinary load, not reproduced (btclib-org/btclib-node#617), one
  statement of a test helper (btclib-org/btclib-node#762). Rerun once; if
  it persists, rerun with `COVERAGE_DEBUG=dataio,combine` and
  `COVERAGE_DEBUG_FILE` outside the rootdir.
- **Collection under `tests/unit` and `tests/functional` follows
  pytest's own default `python_files`** (`test_*.py`, `*_test.py`).
  `pyproject.toml` carries no override, and its own comment beside
  `testpaths` says why.
- **A second `pytest` in this rootdir, even `--help`, erases a running
  suite's `.coverage*`** (btclib-org/btclib-node#191).
  `COVERAGE_FILE=$(mktemp -d)/.coverage` keeps a run's data out of reach.
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
  import annotations` to engage at all, which most modules here do not
  carry, reaching PEP 649's native lazy evaluation on this tree's
  `>=3.14` target instead — confirmed by mapping every
  remaining name and rebuilding, with no change in the warnings.
  btclib-org/btclib-node#417 is the cross-reference ambiguity this
  forced a rename to fix rather than a `conf.py` setting;
  btclib-org/btclib-node#264's own `nitpick_ignore` list is the same
  wall met a second time.
- **Union files after a rebase**: `CONTRIBUTING.md`'s `awk` prints the
  open section's headings with the branch's own last, and step 3 of
  `RELEASING.md`'s *Release to PyPI* has this tree's byte-for-byte
  reconstruction. `RELEASE_NOTES.md` has subsections under
  `## Unreleased`, so its entry is reconstructed under the subsection
  heading it belongs to, not at the end of the section: the driver once
  placed a bullet inside another entry's subsection with nothing lost
  (btclib-org/btclib-node#728).
- **The open section's headings carrying `(closes …)` are landed text; a
  new entry follows section 9 of the standard**
  (btclib-org/.github#586).
- **The docs build does not refuse every closing backtick followed by a
  bare letter.** `` `Coin`s `` fails only where its
  paragraph has no later single backtick; otherwise docutils keeps
  scanning and a later one closes a title-reference swallowing the text
  between, silent and wrong on the rendered page. `db.py`'s and
  `p2p/connection.py`'s module docstrings carry it, with the docs gate
  green (btclib-org/btclib-node#784).
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
