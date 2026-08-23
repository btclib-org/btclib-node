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

### `enable_error_code` holds only codes that need enabling

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
