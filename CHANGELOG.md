# Changelog

Every change of a release, in full: what changed, why, and what it cost.
[RELEASE_NOTES.md](./RELEASE_NOTES.md) has the release notes, which say
what a user has to act on; this file is the record behind them, and is
where a claim in those notes can be checked.

The record starts here, and nothing is reconstructed for the years of
work before it: a changelog written backwards from a git log is a guess
at what somebody would have noticed, and there is no way to check the
guess. This paragraph used to date that boundary to a `v0.1.0` tag,
deleted on 2026-08-23 (#553, btclib-org/.github#105); the boundary is
where this file was written rather than where anything was tagged.

This record carries two shapes of citation. The bullet, beside the claim
it answers, is where section 9 of the organization standard puts the
citation (btclib-org/.github#547); a heading citation is not a claim
that its entry predates that rule. A heading without a number is not an
entry that left its issue out.

The number reached a heading rather than having been chosen for it:
section 11 of that standard requires the landing subject to cite the
issue it answers -- `(closes #N)` where it closes it, `(issue #N)` where
it only advances it -- so a heading written from that subject arrives
with the citation already in it, the pull request number a squash
appends being the part taken off. Section 9's *One fact in one place* is
why it does not stay there once the bullet has it, and that section's
*Nothing already written is rewritten* is why an entry already landed
keeps whichever shape it was written in.

## Unreleased

### `reorg_test.py`'s confirmed spend is standard, and re-enters the mempool

- **`_FUNDED` gains `OP_2DROP OP_1`, the same suffix `tests.anyone_can_spend`
  carries** (closes #865). `Chainstate::MaybeUpdateMempoolForReorg`
  (`src/validation.cpp:303-340`, at bitcoin/bitcoin@4519933391) runs a
  resurrected transaction through `AcceptToMemoryPool` in full --
  `bypass_limits=true` skips only the fee-rate and mempool-size limits,
  not standardness -- and drops it silently where that refuses it, so a
  node that now runs `interpreter.STANDARD_FLAGS` on one, as #847 added,
  and drops one that fails it, matches Core rather than diverging from
  it. The bug this closes was in the test's own fixture: `_FUNDED`'s bare
  data push left two elements on the stack once `_SPENDS_IT` spent it,
  which CLEANSTACK refuses, so `confirmed` could not re-enter the mempool
  after the reorg and `test_a_node_follows_a_reorg_a_real_bitcoind_announces`
  timed out waiting for it on every `integration-bitcoind` run since #847
  landed. `tests/unit/main_test.py`'s own `spend` keeps the old, bare-push
  shape: its transactions are only ever connected in a block, where
  consensus and not `STANDARD_FLAGS` applies.

### `claude-review.yml`'s fork example is a fork (issue btclib-org/.github#456)

- **The header comment's example was `btclib-org/bbt, itself a fork`**, and
  `bbt` is not a fork, nor is any repository the organization owns: the
  sentence illustrated `.fork`'s divergence from `full_name` with a
  repository that no longer instances the case. The replacement is `bbt`'s
  own wording, landed there first as btclib-org/bbt#8
  (btclib-org/bbt@6ccfd3a, closing btclib-org/bbt#3): it argues the same
  point from a repository that is itself a fork rather than from this
  tree's own, and reads back
  `gh api repos/btclib-org/btclib-node --jq .fork`, which answers `false`
  here as it does in every repository of the organization.

### `[tool.uv] required-version` moves with the ceiling `dependabot-core` bundles

- **The floor rises from `>=0.12.1` to `>=0.12.7`**, the uv
  `dependabot/dependabot-core`'s `uv/Dockerfile` currently ships in its
  `ghcr.io/astral-sh/uv` image tag. Section 1 of the organization
  standard sets the floor at that ceiling: left below it, a uv older
  than the one that updater runs `uv lock` with is still free to rewrite
  this tree's lock, which is the failure the key guards against; raised
  above it, that same updater would refuse to re-lock rather than
  upgrading itself. Matching the ceiling is what avoids both
  (issue btclib-org/.github#448).

### CHANGELOG.md's preamble says what is true of its own citations

- **The preamble no longer dates the heading citation or calls every
  instance of it deliberate**: `CHANGELOG.md`'s own `claude-review.yml`
  fork entry carries its citation on the heading after the bullet
  convention already applied, which the old wording could not account
  for. It now says a heading citation is not a claim about when its
  entry was written, and that the bullet is where a citation belongs
  (closes #871).
- **It no longer reads section 11 as requiring `(closes #N)` of every
  landing subject**: that section asks the subject to cite the issue it
  answers, `(issue #N)` where the change advances one without closing
  it, and headings here already carry that token. The mechanism the
  paragraph describes is untouched -- a heading written from the landing
  subject arrives with whichever citation the subject carried
  (closes #874).

## v2026.9.4

### btclib resolves from the released package, not from git `main`

- **`[tool.uv.sources]`'s pin of btclib to its `main` branch is gone**:
  `btclib` 2026.9.3, on PyPI, carries every module and name `src/`
  imports, so the floor in `[project.dependencies]` is what a clean
  install resolves and it is now also what this checkout resolves
  (closes #848).
- **Dropping the table removes the class of defect it created and not
  only this instance of it**: nothing here can diverge from what a
  clean `pip install` resolves through a `[tool.uv.sources]` override
  any longer, because there is no longer one (closes #848).
- **The re-lock is `uv lock --upgrade`**, so every dependency was
  offered its newest resolvable release rather than btclib alone being
  moved (closes #848).
- **The resolution policy
  [ISS 145](https://github.com/btclib-org/btclib-node/issues/145) argued
  for ends here**, and what it bought is what is given up: resolving
  btclib from its `main` made a defect found in this tree fixable there
  the same week, where a release pin waits for the next release. What
  guards against an unannounced break is `test.yml`'s own
  `pull_request` run, where that issue recorded no test job running on
  a pull request at all (issue #145).

### `test.yml` says which checks `main` actually requires (closes #749)

- **The comment above `test-passed` said nothing required a status
  check, and four do.** It named `main-integrity` for signatures and
  linear history and called the classic protection "empty", citing
  btclib-org/.github#88. Read back today, classic protection carries
  `Lint and type-check`, `test: every job passed`, `Build the
  documentation` and `Regtest against Bitcoin Core`, and none of the
  three rulesets holds a `required_status_checks` rule. #88 is the
  issue that records that state *moving*, so the comment cited its own
  evidence for the thing that evidence had stopped saying.
- **It is the comment a reader consults to learn whether anything in
  this file refuses a landing**, so anything keyed on it -- what a job's
  place in `test-passed`'s `needs:` buys, how a report-only cell differs
  from a gating one -- started from a false premise.
- **`REPOSITORY.md`'s *Required checks on main* stays the one place the
  four are listed**, and the comment points there instead of repeating
  them, which is what kept the two statements able to disagree.
- **Four other places named the wrong mechanism-holder.** The push
  trigger's "the name main's ruleset can require", this job's own "what
  a ruleset matches", and "which a ruleset counts as satisfied" are
  classic branch protection in this tree, not a ruleset. Each now names
  the mechanism, or is written to hold of either.

### CLAUDE.md's union bullet gains the seam model (closes #753)

- **The unit of the damage is the seam, not the rebase.** One blank line
  eaten per seam, a seam being where the two sides' added blocks abut in
  the fused output; a blank line internal to one side's own addition
  survives. `origin/iss-547-changelog-boundary` is the case that
  isolates it -- two insertions in one merge, the line lost at the
  second and not the first. Eighteen seams across two sessions in one
  day, eighteen eaten.
- **The `merge-tree` sentence was read as licence to skip the command,
  which it is not.** It was, and remains, true about *conflict
  detection*: exit code and silence are not evidence of a safe rebase.
  What it left a reader to infer is that merge-tree is no use here. It
  writes the tree the rebase will produce, and the damage is in that
  blob with a line number. The case that shows both halves at once is a
  merge that conflicts: exit `1`, stage-1/2/3 entries for seven source
  paths, `CHANGELOG.md` present only as `Auto-merging`, tree written
  anyway, flush heading readable at line 2853.
- **The hunk header does not predict a seam**, recorded so it is not
  re-invented. Anchor equality is necessary and not sufficient, and a
  first-hunk `grep` reads the wrong hunk on a branch making two
  insertions.
- **A no-seam case has to be shown to be a case at all.** Two branches
  whose entry already landed produce a fused blob byte-identical to the
  base: nothing arrives, no seam is possible, and every check passes
  including the rebuild. Correct measurement, different question. The
  guard is one `cmp` against the base with a real arrival as control.
- **The general form is what outranks the rest**: a check that can only
  report the absence of damage passes on an input that cannot carry the
  damage, silently and correctly. Three instances in one day -- the null
  case, the cached badge render whose `failing` is worthless and whose
  `passing` is sound, and the `-n auto` mutation already listed.
- **And what says whether a check needs a guard is whether its input can
  go empty without anyone choosing it.** A walk can, through a refactor
  nobody thinks of as touching the test; a module literal cannot, a
  count set to zero being deliberate. So the rule carries a condition
  rather than demanding ceremony everywhere -- a sibling tree whose
  equivalent layer is driven by a literal needs no such guard.
- **A reconstruction is a control only once the block's own boundaries
  have been read**, the splice otherwise duplicating a blank on one side
  and dropping one on the other, which reads as a two-line seam that is
  not there.

### The suite gains the property layer the fuzzer presupposes (closes #742)

- **`tests/property_test.py` states, over generated octets, the property
  every harness in `fuzz/` already encodes as a `contextlib.suppress`**:
  a declared entry point either returns or raises `BTClibException`, and
  nothing else. A property test answers *does this hold over the domain
  I described*; a fuzzer answers *what is in the domain I did not
  describe*, and the second presupposes the first -- so what the weekly
  `fuzz` run reported was bounded by a description the suite did not
  carry.
- **hypothesis, section 7's named shape, and not hand-rolled
  properties.** The domain is unconstrained octets, which is `binary()`
  and nothing more, so hand-rolling buys no fidelity and loses the
  shrink; this tree also had no hand-rolled property machinery to build
  on. Executed rather than reasoned about: with a `RuntimeError` planted
  in `frame_message_bytes`, the layer went red on that entry point alone
  and reported the minimal `data=b'\x00\x00\x00'`, where a seeded
  generator reports whichever blob it drew. The mutation was reverted
  and the revert proved by the marker's absence.
- **The specs come from the harnesses' own `ENTRY_POINTS`**, through
  `fuzz_corpus_test.py`'s existing walk rather than a second one: two
  walks over one fact are two things that can disagree, and a harness
  added for a new parser now gets a property test by being added.
- **What the walk skips is structural, not a name list.** A
  `fuzz.`-prefixed spec is one `_resolve` loads out of `fuzz/` by path,
  and today's names a dispatch through a whole `Node` rather than a
  parse -- section 7's own carve-out being that a subject which is not a
  parser does not owe the layer.
- **An empty walk is guarded.** `parametrize` over an empty sequence
  collects nothing and reports no failure, so the layer would be green
  having stated nothing; a test asserts the walk finds something.
- **Profiles are registered once in `tests/conftest.py`**: `default` at
  500 examples, `thorough` at 2000, selected by `HYPOTHESIS_PROFILE`,
  `deadline=None` because a per-example limit is a timing flake on the
  slowest cell of the matrix and this suite runs one on
  `windows-latest`. 500 is affordable measured rather than assumed --
  the whole file runs in 1.31s against a suite of about ninety.
- `tests/README.md` records the choice where section 7 says a reader
  meets it.

### `links.yml` asks lychee for the fragment too (issue btclib-org/.github#583)

- **`.github/workflows/links.yml` passes `--include-fragments`.** A link
  into a heading is then checked as an anchor and not only as a page,
  where the forge serves the page and drops a fragment it cannot resolve,
  so a heading renamed in the tree a link here points into is red in this
  run rather than nowhere. The check reads the page already fetched for
  the link and adds no request, and no fragment this tree links to fails
  it today.
- **What the flag reaches here is bounded by the token the step already
  holds.** Every anchor this tree carries into another repository is the
  bare `github.com/btclib-org/.github#<heading>` shape, and on a failed
  link of that shape lychee falls back to the repositories API and takes
  the repository's existence as the answer, so those anchors are not what
  this buys; btclib-org/.github#630 is where that is weighed.

### The free-threaded cell stops reddening the run (closes #747)

- **`test.yml`'s `free-threaded` job branches on its own environment
  sync instead of failing at it.** A workflow run's conclusion
  aggregates every job in it, `needs:` or not, so a report-only cell
  that cannot install concluded every push to `main` `failure` while
  `test: every job passed` was green. #723 closed that as a *gate*, by
  taking the cell out of `test-passed`'s `needs:` (#727), and left the
  run conclusion exactly where it was.
- **The saturation had already cost something.** #737's red
  `windows-latest` went unreported through eight consecutive pushes:
  with the run red for a known and accepted reason, a second red reason
  changed nothing a reader could see.
- **Not `continue-on-error` on the job**, which would buy the green by
  hiding every outcome including the two worth hearing. No `cp314t`
  wheel is now a `::notice::` and a green job; an installed wheel is
  interrogated before the suite runs. #746 measured why that second
  step is needed: rocksdict builds for `3.14t` unmodified but declares
  a bare `#[pymodule]`, which PyO3 documents as `gil_used = true` for
  0.23 through 0.27, so importing it hands the GIL back -- and
  `Node.worker_pool` reads `sys._is_gil_enabled()` at first use, after
  that import. A cell that simply ran the suite would report a
  free-threaded pass measured entirely on the process-pool arm.
- **The two states that are news fail the job and colour the run**: a
  wheel that installs but gives the GIL back, and a suite that
  genuinely fails free-threaded.
- **A false sentence in the file's header is corrected.** It tied
  `free-threaded` rejoining `needs:` to #723 closing; #723 closed by
  doing the opposite, and the row still does not install.

### The per-test timeout is remeasured (closes #737)

- **`pyproject.toml`'s `timeout` is 300, from 120.** The old number was
  the same ratio the comment still argues -- nearly three times the
  worst test -- against a suite whose worst was
  `tests/functional/p2p/download_test.py::test_download` at 42.09s.
  That suite has since grown
  `tests/unit/chainstate/utxo_index_test.py::test_rev_patch` and its
  `::test_long_init` beside it, 75.30s and 63.78s on `ubuntu-latest`
  under coverage and 86.20s for the second of them on
  `windows-latest` without it, where the first exceeded 120 outright.
  What moved is the suite and not the runner: `::test_rev_patch`
  stages a 20000-block chain and unwinds all 20000 of its `RevBlock`s
  in 34s serial on a ten-core machine, at a 143 MiB peak.
- **The `windows-latest` cell had been red on every push to `main`
  since #728 for exactly that**, reporting it as
  `worker 'gw3' crashed while running ...::test_rev_patch` rather than
  as a timeout. pytest-timeout picks `signal` where `SIGALRM` exists
  and `thread` where it does not, and the `thread` handler ends the
  process with `os._exit`; under `-n auto` that is a worker that
  stopped answering, which xdist reports as a crash with no traceback
  and nothing naming the bound. Reproduced away from Windows by giving
  the `thread` method a test that sleeps past it, which produces the
  same two lines. The comment beside `timeout` says so, so that a
  crash on that image is read as a suspected timeout first.
- **One bound rather than a marker on the slow tests**: three tests now
  sit within a factor of two of it, so marking two of them would leave
  the third to be the next to fail.

### The three chain scripts guard their module body (closes #579)

- **`scripts/chains/mainnet.py`, `testnet.py` and `signet.py` build and
  start their `Node` under `if __name__ == "__main__":`.**
  `Node.worker_pool` is a `multiprocessing.Pool`, and every start method
  other than `fork` re-imports `__main__` in each worker
  (`multiprocessing/spawn.py`'s own `import_main_path`), so an unguarded
  script built a second `Node` on the same data directory in every pool
  worker once block download warmed the pool.

### A rejection test asserts which rule refused a block (closes #587)

- **`Node.last_rejected_block` pairs the hash `update_chain`'s trial
  loop was on with the exception it raised**, once its `except`
  catches one -- alongside `failed_hash` (`src/btclib_node/main.py`),
  which it already tracked and only logged. `update_chain` catches
  `Exception` generically, so a rejection test asserting only
  `hash not in active_chain` was satisfied by any raise anywhere in
  the per-block gate: a new rule ranked ahead of an older one there
  could retire the older one's own end-to-end test silently, with the
  suite green at its 100% coverage floor. Measured on #568/#571's own
  branch, where two tests built their bad block with a coinbase that,
  before that branch, committed to no height, and after it were
  refused for BIP34 before either ever reached the rule it was written
  for.
- **The exception is captured rather than matched from a log line**:
  `update_chain` already logs it, but only behind a fixed message and
  the logging module's own formatting, neither of which a test should
  have to parse to ask what actually raised.
- **`tests/unit/main_test.py`'s `rejected_because(node, block, phrase)`
  asserts both halves**: that `block` is the node's own last
  rejection, and that `phrase` -- a substring of the exception's own
  message, not an exact match, since the wording is btclib's or this
  tree's own to change rather than an interface either promises to
  keep -- is why. `tests/unit/chainstate/filter_index_test.py` imports
  it the way it already imports `connect` and `spend`. Applied to
  every rejection test in both files that used to assert only the
  outcome.

### A block is checked against subsidy and BIP34 (closes #568, closes #571)

- **`Chain` carries `subsidy_halving_interval` and `bip34_height`, one
  per network** -- read from Core's `src/kernel/chainparams.cpp` at
  bitcoin/bitcoin@204256c73f: mainnet, testnet3 and signet halve every
  210000 blocks and regtest halves every 150; mainnet's coinbase must
  commit to its height from block 227931, testnet3's from 21111, and
  regtest's and signet's both from block 1.
- **A block whose coinbase pays more than subsidy plus fees does not
  connect** (closes #568). Core's `bad-cb-amount`
  (`ConnectBlock`, `src/validation.cpp:2619-2621`,
  at bitcoin/bitcoin@204256c73f): `nFees + GetBlockSubsidy(...)` is the
  ceiling, and `Chain.subsidy` is this tree's own `GetBlockSubsidy`.
- **`Block.assert_valid_contextual` is called where a block connects,
  with the height it connects at** (closes #571), so a coinbase that
  does not commit to it (BIP34's `bad-cb-height`) does not connect
  either. Core's `ContextualCheckBlock`
  (`src/validation.cpp:4170-4176`, at bitcoin/bitcoin@204256c73f).

### The user agent names the project, and its version (closes #580)

- **`/Btclib/` becomes `/btclib:<version>/`** (closes #580), which is
  BIP14's `/Name:Version/` -- the shape Core's own `FormatSubVersion`
  builds (`src/clientversion.cpp:65-70`) and the one a crawler reporting
  what the network is made of parses. The name is the project's own
  spelling, lowercase, and not the distribution's `btclib-node`: btclib
  is what this is a node over.
- **The version is read from the installed distribution**, through
  `importlib.metadata`, rather than written here a second time. No gate
  in this tree reads the wire, so a literal is the one spelling of the
  version that nothing would catch drifting, and
  `RELEASING.md`'s *Which version string is which* already tracks four
  of them. A checkout of `main` therefore announces the cycle it is
  open on and what pip installs announces its release day.
- **A tree that was never installed raises rather than announcing a
  placeholder**: a user agent is a claim, and one saying `unknown` where
  the version belongs is worse than a node that says why it will not
  start.
- **The test reads the framed octets, not the constant**: what #580
  reported is what a real peer received -- `getpeerinfo` on the
  `bitcoind` a node of this tree was connected to answered `/Btclib/` --
  and a constant asserted against itself answers for nothing in
  between. Verified by mutation: putting `b"/Btclib/"` back fails the
  new test and nothing else.

### CLAUDE.md's union bullet trusts the endpoint, not the cache (closes #565)

- **CLAUDE.md's `merge=union` bullet told a session to "rebase and
  look" past GitHub's `mergeable: CONFLICTING`, reading a local
  rebase's silence as proof the merge was clean** (closes #565). A
  driver built never to conflict cannot report one whether the merge it
  produced is right or wrong, so the silence was never evidence of
  anything.
- **The rewrite does not swap one unmeasured trust for another**
  (closes #565): `gh pr view --json mergeable` is an asynchronous,
  cached read that can still answer `UNKNOWN` on a pull request already
  `MERGED`, so a `CONFLICTING` seen there is not itself confirmed real.
  The merge the endpoint actually attempts
  (`gh api -X PUT .../merge`) is the one genuine three-way merge in
  the pair, and its refusal is the true report. The bullet now points
  at `RELEASING.md`'s step 3 reconstruction as the check that tells a
  safe rebase from a fused one, and names `git merge-tree --write-tree`
  as applying the same driver rather than being a dry run of the
  question.

### RELEASING.md treats the simple API as the index's state (closes #545)

- **`RELEASING.md` named the JSON API's staleness as a quirk of the
  `provenance` field alone** (closes #545). It now states the general
  rule: the simple API is the index's own state and the JSON API is a
  cache of it, disagreeing with the simple API on more than which
  fields it fills in.
- **The *If something goes wrong* section's first branch, on whether
  anything was uploaded, now answers that from the simple API** rather
  than from which job a run reports as having failed — `publish-pypi`
  is not idempotent, so retagging over a version that in fact landed
  fails a second time on top of the first.
- The `github-release` recovery script's own digest check read the
  JSON API for a version the workflow had just published, the exact
  situation the staleness reaches; it now reads the digest off the
  simple API instead.

### `tests/README.md` declares the suite's split (closes #530, #531)

- **`tests/README.md` now carries the split's reason, and the package
  `__init__.py` docstrings point at it instead of restating it**
  (closes #531): section 7 of the organization standard asks for the
  split between `tests/unit/`, `tests/functional/` and
  `tests/integration/` to be declared in `tests/README.md`, and it
  lived instead in the package docstrings, each naming its counterpart
  rather than pointing at one place.
- **`tests/integration/conftest.py`'s docstrings no longer name a
  single consumer** (closes #530): they named `bitcoind_test.py` as the
  module the fixtures below are for, where every module under
  `tests/integration/` asks for them; both now say "this package's
  tests" instead of one module that stopped being the only one.

### dependabot.yml argues from what deps-latest.yml does (closes #558)

- **The uv ecosystem's comment stops arguing from a missing `latest`
  sentinel** (closes #558): `deps-latest.yml` is in the tree and
  resolves the same floor `pyproject.toml` declares a day before
  Dependabot's own Thursday run, so a Thursday pull request bumping a
  dependency is not the first run that tries the bump -- it is only the
  first whose own required checks a merge waits on, the earlier report
  not being one of them.
- **The github-actions ecosystem keeps the old argument, now in its own
  sentence** (closes #558): nothing resolves an action version the way
  `deps-latest.yml` resolves a dependency, so that pull request is still
  the first run that tries one, and weekly is still the trade against a
  month of unread drift.

### The Read the Docs bullet stops waiting on a met condition (closes #559)

- **`REPOSITORY.md`'s Read the Docs bullet gave "this tree does not
  publish -- the day it does" as the reason nothing is connected**
  (closes #559), and `v2026.8.27` is that day. It now says what the
  connection is actually waiting on: an action on Read the Docs' own
  side, importing the project under the organization's account, which
  no `gh api` call in that file can take or read back. Issue #563
  tracks it.
- **A reason naming a condition already met is worse than a stale
  record** (closes #559). A stale record is wrong about the past; this
  was wrong about what happens next, and the next reader takes the
  section as saying the work is due and goes looking for the blocker.
  It is also the same defect #556 fixed in the sibling bullet of that
  same section two hours earlier and did not carry across -- #553's
  own finding, arriving from a decision I made myself rather than from
  somebody else's.
- **The `homepage` bullet beside it loses a reason the release
  falsified too** (closes #559): "there is no published site for it to
  point at", where <https://pypi.org/project/btclib-node/> has been one
  since the release. Whether to set it stays a decision of its own --
  `btclib`'s own `homepage` is `https://btclib.org` rather than its
  documentation site, so it is not something the Read the Docs
  connection settles on its way past.
- **The bullet carries the badge command and its control** (closes
  #559): `btclib-node` answers `unknown` where `btclib` answers
  `passing`, which is the difference between a project that does not
  exist and one that does. Both were run against the live service.

### RELEASING.md's two steps that a release disproved (closes #554, closes #561)

- **The bill-of-materials step describes the document the script
  writes** (closes #554). It said the document names "one component per
  dependency the wheel's metadata declares — `btclib[secp256k1]`, and
  whatever it in turn resolves to on the interpreter that built the
  release". `btclib_node-2026.8.27.cdx.json` names **one** component,
  `pkg:pypi/btclib`, with no version and nothing transitive. The script
  is right and the prose was wrong:
  `.github/scripts/generate_sbom.py`'s own docstring says a resolved
  version "would be a claim the wheel does not make".
- **It also says what the document therefore does not cover** (closes
  #554): a consumer matching vulnerabilities against it gets the direct
  dependency and nothing below. That is a property rather than a
  defect, and it is only a property if the step saying to read it says
  so. The `git+https://` check, which is why the step exists, holds and
  stays the point of the paragraph — verified by running the step's own
  command against the published release rather than by reading it.
- **A verification step whose expected output does not match a correct
  run is the failure mode here** (closes #554), and it is worse than
  having no step: a reader who follows it finds one component where the
  text led them to expect a tree, and has to decide whether the release
  is broken, the script is, or the sentence is. The one that leaves no
  trace on the artifact is the right answer and the hardest to reach
  just after publishing something.
- **Step 3 gives the check that tells a safe rebase from a fused one**
  (closes #561), keeping the prohibition as the default rather than
  replacing it. The check is the redo itself, done in a scratch file:
  rebuild the file from `git show origin/main:<file>` plus this
  release's own edits, and `cmp` it against `git show HEAD:<file>`.
  Identical bytes prove the rebase produced what the redo would have,
  and a difference hands back the file that should have been there.
  `v2026.8.27` was rebased when #551 landed in front of the tag, and
  this is what licensed it.
- **Two checks that look like that one are named as not being it**
  (closes #561), both measured rather than reasoned about. Comparing
  the added and removed lines of the diff before and after the rebase
  is **necessary and not sufficient**: it catches a fusion that ate a
  line and passes a pure misordering — the same entry below the newly
  landed one instead of above it, every line intact — because the `+`
  lines are the same sequence wherever they land. This entry's first
  draft prescribed exactly that check, and the counterexample was
  built to test it rather than to illustrate it. And `git merge-tree
  --write-tree` is not a check at all: `merge-ort` reads
  `.gitattributes` from the trees it merges, applies the `union`
  driver, and writes the fused blob with exit `0`.
- **The reconstruction is owed on the hand redo the rule prescribes**
  (closes #561), which the rule did not say. Retitling a heading the
  landed change also opened fuses it the same way, and having typed it
  yourself says nothing about that — a prohibition standing in for a
  check it could have specified does not cover the path it sends the
  reader down.

### The crons and the badges follow the reordered calendar (closes #520)

- **Every `cron:` here names the instant section 10's calendar gives its
  workflow at this repository's own minute** (issue
  btclib-org/.github#480): that calendar orders its rows by what they
  ask about, and the day and the hour are its to state, so no comment
  here restates them. `deps-latest` is the one schedule this leaves
  where it is, and `.github/dependabot.yml` is not touched: section 10
  ties Dependabot's own run to it.
- **A schedule comment names the cadence rather than a weekday** (issue
  btclib-org/.github#480): a comment naming the day is one the next move
  through the calendar falsifies while it stays grammatical, and the day
  is not this tree's to state in the first place. `mutation.yml` loses
  its reasoning about the emptiest day with it: the calendar groups by
  family instead.
- **`README.md`'s badge block is section 2's three groups** (closes
  #520): what the software is, whether it works, and what the OpenSSF
  makes of it -- the gates inside the second in the order a commit meets
  them, then the sentinels in the calendar's order. The Scorecard badge
  opens the third group rather than sitting among the sentinels, which
  is what `scorecard` being the calendar's last row buys.
- **The block's own comment points at section 2 rather than restating
  it** (closes #520): the rule the block is kept to lives in the
  organization standard, so a copy of it here is a second thing to keep
  true, and this one had stopped being true.
- **`github/v/release`, the licence and `wheel` are in the block**
  (closes btclib-org/.github#496): `v2026.8.27` is on the index and on
  the forge, so each of the three renders a measurement. The licence
  badge is `LICENSE` read through `img.shields.io/github/license`, which
  is the spelling that section asks for.

### The tree's prose says what the forge holds (closes #553, closes #555)

- **Four files no longer say nothing has been published** (closes
  #555): `v2026.8.27` is on PyPI and on the forge, and `README.md`,
  `CONTRIBUTING.md`, `RELEASING.md` and `REPOSITORY.md` each said it was
  not. Two of them printed the pre-release answer beside a live command
  — `README.md`'s badge comment, and `CONTRIBUTING.md`'s
  `curl … # 404`, which answers `200` — and those are the two that cost
  a reader something, since running the command is what the file asked
  for and disagreeing with it is what the reader gets.
- **`README.md` says how to install the package** (closes #555), which
  it could not truthfully do until today and which a published
  project's README not saying is a gap rather than a matter of taste.
- **Eleven sites stop arguing from a `v0.1.0` tag that was deleted on
  2026-08-23** (closes #553). The tag and its release were removed on
  the maintainer's decision closing btclib-org/.github#105 — a
  lightweight tag is a ref with no object of its own, so there is
  nothing on it to sign, and a repository publishing nothing had no
  release the tag was the record of; `portanode`'s `v2026.01.27` went
  the same way the same day, both release bodies captured into that
  issue first. This tree's files were never carried along, and went on
  describing the tag for four days.
- **What made it hard to see is that `git tag` answers locally**
  (closes #553): the tag is still in every clone fetched before that
  day, pointing at the real 2023 commit `d0ac7646`, so six files
  agreeing with each other and with a local `git tag` is what a reader
  checking them found. `gh api …/git/ref/tags/v0.1.0` answers `404`;
  btclib-org/.github#105's own measurement four days earlier recorded
  it answering `commit`, which is the pair that dates the deletion.
- **The seven historical `CHANGELOG.md` entries are left as they
  stand** (closes #553). #504's entry gives "a version this repository
  had already tagged" as a reason for retiring `0.1.0`, and that was
  true when it landed — the tag was deleted after it. A changelog
  edited to agree with what happened afterwards stops being a record;
  the correction is here, in the entry for the fix.
- **`release.yml`'s `--exclude 'v0.1.0'` is kept and its comment made
  checkable** (closes #553): the flag has been inert since the
  deletion, and removing it would change the behaviour of a path that
  runs twice a year and cannot be rehearsed against a second tag, in
  exchange for nothing — from `v2026.8.27` on that tag is the nearest
  one reachable from any later release, so a `v0.1.0` re-cut tomorrow
  could not be resolved as "previous" either way.
- **`RELEASING.md`'s `gh workflow run` 404 paragraph is a record rather
  than a prediction** (closes #555). It said the 404 "bites once, on the
  first release after it is written"; it bit once, on `v2026.8.27`,
  and inverted the order exactly as written — the TestPyPI rehearsal
  asked for before the merge ran after it, still before the tag.
- **`RELEASING.md` says what `v2026.8.27` did and did not exercise**
  (closes #555): `public-api` resolved no previous tag, skipped its own
  comparison and reported success in eight steps, so the release that
  has run is the one that says least about that job. The first one to
  exercise it is the next.

### The downloads badge links to the plural pepy path (closes #578)

- **`README.md`'s downloads badge now links to
  `https://pepy.tech/projects/btclib-node`**, the plural path section 2
  of the organization standard fixes; the singular
  `https://pepy.tech/project/btclib-node` it linked to before answers a
  redirect rather than the page itself. The badge image URL,
  `https://static.pepy.tech/badge/btclib-node`, is unchanged: section 2
  already fixes that spelling.

### A coin knows its own height, coinbase bit and maturity (closes #569)

- **The UTXO record is a `Coin`, not a bare `TxOut`**
  (`src/btclib_node/block_db/__init__.py`): the height of the block
  whose transaction created the output, and whether that transaction
  was the block's own coinbase, alongside the output itself. Matches
  Core's own `Coin` (`src/coins.h`, at bitcoin/bitcoin@204256c73f) --
  a varint packing `(height << 1) | coinbase` ahead of the output --
  except for Core's own `TxOutCompression`, a space optimisation this
  class does not reproduce: `KeyValueStore` is measured
  write-dominated, not read-dominated (btclib-org/btclib-node#586),
  which argues for the varint staying tight and not for a second
  optimisation on top of it. `RevBlock.to_add` (same file) carries
  `Coin`s rather than `TxOut`s for the same reason Core's own
  `CTxUndo` does: a reorg that restores a spent output has to bring
  back the height and coinbase bit it was created with, not the
  height of whichever block the restore runs at.
- **A spend of a coinbase output not yet `COINBASE_MATURITY` blocks
  deep does not connect, and does not enter the mempool either**
  (`interpreter.check_coinbase_maturity`, called from both
  `main._validate_block` and `main.verify_mempool_acceptance`). Core's
  `bad-txns-premature-spend-of-coinbase`
  (`Consensus::CheckTxInputs`, `src/consensus/tx_verify.cpp:185-186`,
  same commit), checked at both of Core's own call sites
  (`ConnectBlock` and `AcceptToMemoryPoolWorker`) because both reach
  the same rule against a different spend height. `COINBASE_MATURITY`
  (`src/btclib_node/constants.py`) is Core's own bare `100`, not part
  of `Chain` and not relaxed for regtest, matching Core's own
  `consensus.h`.
- **`KeyValueStore` refuses a store from before this schema version
  existed, rather than misreading it** (`src/btclib_node/db.py`):
  `PRAGMA user_version` is stamped on a fresh store and checked on
  every open, kept out of the `kv` table itself so a version marker
  never sits inside the key order `BlockIndex.init_from_db` depends
  on. A version-0 store already holding a row is what a datadir from
  before this existed looks like, told apart from a genuinely fresh
  one, which starts at the same `0`.
- **`tests/__init__.py`'s `generate_random_chain` no longer embeds a
  spend younger than `COINBASE_MATURITY`**: every block up to that
  depth carries its own coinbase alone, and every block past it spends
  the oldest output the chain has made spendable -- `chain[0]`'s own
  coinbase the first time, and that spend's own output after, since
  neither is ever a coinbase again. Every test built on a chain shorter
  than that, or spending its own tip rather than its root, is adjusted
  to a shape this rule actually accepts.

### `Node.__init__` refuses inside a re-imported `__main__` (closes #589)

- **`Node()` raises `ReimportedMainProcessError` where `multiprocessing`
  re-imported `__main__` to build it** -- `current_process().name` is
  not `"MainProcess"` and the active start method is not `fork` --
  rather than leaving that solely to the module-body
  `if __name__ == "__main__":` guard `scripts/chains/*.py` now carries
  (#579): a caller that forgets the guard used to build a second `Node`
  on the same data directory in every worker its own `worker_pool`
  spawned, silently, with no exception from either `multiprocessing` or
  this tree.
- **`allow_reimported_main=True` opts a caller out of that refusal**:
  the two calls it reads cannot tell an unguarded module top level from
  a supervisor deliberately building a `Node` inside its own pool
  worker, so the distinction is the caller's to state rather than
  `__init__`'s to guess.
- **`tests/unit/scripts_test.py` parses `scripts/chains/*.py` with
  `ast`** and asserts every `Node(...)` call sits lexically inside the
  `__main__` guard's own lines, resolved through whatever name `Node`
  is actually bound to rather than the literal string `"Node"` -- an
  aliased import still reads as compliant, and a wrapped call is still
  found. Closes the gap `scripts/` sitting outside `testpaths` would
  otherwise leave: nothing else in the suite would catch a future
  flattening of one of the three shipped scripts.
- **Lexically, and nothing beyond it**, which refuses `def main():
  Node(...)` called from the guard -- correct code, refused. A version
  of this test did follow a bare `name()` call into the function it
  named, and four review rounds each found a different way to make that
  answer *guarded* for a `Node(...)` that really does run unguarded on
  the re-import: two `def`s sharing a name, then a `def` nested in the
  guard shadowing a module-level one, then a `lambda`, a `class` and an
  `import ... as` doing the same without being a `def` at all. Deciding
  which definition a name reaches is Python's own binding semantics and
  `ast` does not model them; `symtable` does not answer it either,
  reporting a plain `def` as assigned exactly as it reports one
  shadowed by a `lambda`. The five shapes are kept as tests of the
  limit, since a resolver reintroduced without them would pass its own
  tests.

### Two platform sentinels, and neither gates a merge (closes #528, issue #430)

- **`.github/workflows/os-ubuntu.yml` runs the suite on `ubuntu-latest`
  and `ubuntu-24.04-arm`, weekly** (closes #528), which is where aarch64
  Linux is exercised at all. It is not a sweep over pure Python:
  `pyproject.toml` depends on `btclib[secp256k1]`, so a compiled
  libsecp256k1 is resolved and imported at run time, and an aarch64
  runner selects a different published wheel of it from the one the
  gate's cell installs.
- **Its matrix is the image, and the interpreter axis is absent by
  decision** -- which the header states, so that a single column does
  not read as a matrix somebody left half written. `requires-python` is
  `>=3.14` because an application takes the newest interpreter its
  dependencies allow, which is what issue #507 settled, so that axis has
  one value here and that value is the merge gate's own cell. The
  sibling repositories that carry this workflow sweep interpreter
  against image, and copying that shape would mean giving the axis
  versions this package does not claim.
- **`.github/workflows/os-windows.yml` runs the same suite on
  `windows-latest`, weekly, and is expected to fail** (issue #430).
  `loop.add_reader`, which `src/btclib_node/p2p/manager.py` and
  `src/btclib_node/rpc/manager.py` both call, belongs to the selector
  event loop rather than to the Proactor loop Windows takes by default;
  `signal.SIGTSTP`, which `install_signal_handlers` passes to
  `signal.signal`, does not exist there. Issue #429 records that pair and
  calls it a lower bound. A sentinel reports where a gate refuses,
  so it may land red, and its runs produce the list that prices the gate
  cell issue #430 stays open for.
- **No Windows classifier lands with it** (issue #430): a classifier is
  a claim made to an index, and the claim would be that a pull request
  is checked there, which is the gate cell rather than the sentinel.
  `pyproject.toml`'s comment beside `Operating System :: POSIX` says
  that, and names the two calls the suite reaches.
- **Both cells pass `--no-cov`**, `test.yml`'s coverage job being where
  the floor is measured and gated: the floor is a claim about one
  interpreter on one image, and a cell held to it would report a
  platform's own finding under the name of the coverage number.
- **`tests/interpreters_test.py` gains both files**, its list of the CI
  files that name an interpreter being written out rather than derived.
- **The two badges sit after `os-macos`'s in `README.md`**, where
  section 2's fixed order puts them, that order over the sentinels being
  section 10's calendar.
- **`test.yml`'s header names the three platform sentinels beside it**,
  rewritten rather than extended: the paragraph's own subject was that
  a Windows sentinel would be the support claim rather than its check.
  `REPOSITORY.md`'s concurrent-job ceiling names the platform axis by
  the sentinels that carry it rather than by `os-macos.yml` alone.
  `CONTRIBUTING.md`'s walk of the workflows that only report names
  both beside `os-macos.yml`. `release.yml`'s header said `os-macos.yml`
  was this tree's only platform sentinel and that neither of these two
  was here, and `RELEASING.md` named the one where there are now three:
  both say the set rather than the member, and `release.yml`'s pointer
  at `test.yml` resolves again, that header having said what each of the
  two *would* ask and now saying what each of the three does.

### BIP30, finality and BIP68 gate a connecting block (closes #570, closes #572)

- **A block that creates an output already unspent elsewhere on the
  chain does not connect** (`UtxoIndex.add_block`'s own BIP30 check,
  closes #570) -- Core's `bad-txns-BIP30` (`ConnectBlock`,
  `src/validation.cpp:2401-2431`, at bitcoin/bitcoin@204256c73f),
  CVE-2012-1909's shape: without it, a second block sharing an
  already-mined, still-unspent txid silently overwrote the first
  block's own coin, and a reorg away from the second deleted an output
  the first block's own branch still carried. Checked over every
  transaction the block carries, coinbase included, before either of
  `add_block`'s own two loops stages a write -- against the state
  exactly as it stood before this block, matching Core's own separate
  pre-pass.
- **`Chain.bip30_exceptions` carries the two 2010 mainnet blocks Core's
  own `IsBIP30Repeat` exempts** (`src/validation.cpp:6218-6222`, same
  commit), empty on every other network. Once BIP34 binds (#571), a
  block's own coinbase commits to its own real height, which two
  different heights can never share -- the property that makes a new
  violation of this kind unreachable, and the reason the two 2010
  blocks are the only ones this check will ever have to carve out.
- **A transaction whose `lock_time` has not been reached does not
  connect, and does not enter the mempool either**
  (`interpreter.check_final_transactions`/`is_final_tx`, closes #572)
  -- Core's `bad-txns-nonfinal` (`ContextualCheckBlock`,
  `src/validation.cpp:4158-4166`, at bitcoin/bitcoin@204256c73f).
  Checked against the previous block's median time past once BIP113
  binds -- the same height `Chain.flags` already turns
  `CHECKSEQUENCEVERIFY` on at, Core deploying both as one soft fork --
  and against the block's own timestamp before that; the mempool path
  checks unconditionally against the active chain tip's own median
  time past, matching Core's own `CheckFinalTxAtTip`.
- **A non-coinbase transaction whose BIP68 relative lock is not yet
  satisfied does not connect, and does not enter the mempool either**
  (`interpreter.check_sequence_locks`) -- Core's
  `SequenceLocks`/`CalculateSequenceLocks`/`EvaluateSequenceLocks`
  (`src/consensus/tx_verify.cpp:45-115`, same commit), over each
  input's own `Coin.height` (#569). A relative lock binds a transaction
  of version 2 or above only, and an input carrying the disable bit is
  skipped, both matching BIP68.

### Read the Docs is recorded, and README.md carries its badge (closes #563)

- **`README.md` carries the Read the Docs badge** (closes #563), after
  the `docs` workflow badge and before the sentinels, which is the
  position section 2 of `btclib-org/.github`'s `README.md` fixes for
  it. It answers `passing` where a fabricated project name answers
  `unknown`, so it reports a state rather than an absence, which is
  what the badge block's own rule asks of a badge.
- **`REPOSITORY.md` records the project in a section of its own**
  (closes #563), "Read the Docs, which is btclib-node.readthedocs.io",
  which is the shape the bullet it replaces said this file would take:
  what `latest` and `stable` follow, read back from the project API,
  the `read-the-docs-community` GitHub App the connection runs through
  rather than a webhook of this repository's own, and the 404 on
  `/en/v2026.8.27/` beside the 200s on `/en/latest/` and `/en/stable/`.
  Activating a release tag needs an automation rule on Read the Docs'
  own side, which #596 tracks, so the section records the tag's absence
  rather than a pull request fixing it.
- **"What is not configured, and why" keeps the Pages half** (closes
  #563) of the bullet that described the two together, `gh api
  repos/btclib-org/btclib-node/pages` still answering `404`. The
  `homepage` bullet beside it is untouched: it is a decision of its
  own, and btclib-org/.github#533 is where the rule for it is being
  settled.
- **The comments that pointed at that bullet point at the section**
  (closes #563): `.readthedocs.yaml` on which versions run the build it
  configures, `pyproject.toml` beside `[project.urls] documentation`,
  whose link it said would 404 until a project was connected,
  `release.yml` on the job it does not have --
  bitcoin-core-rpc's `documented`, which polls `/en/<tag>/` until that
  tag is served and here would wait on a version no rule activates --
  and `docs.yml`, which said read the docs builds this tree once a
  project is connected there.

### CLAUDE.md records what this batch cost a round each to learn (closes #600)

- **The docs build is named as the third gate, with what it alone
  catches**: a closing backtick followed by a bare letter is not a
  valid RST end-string, `sphinx-build -W` fails on it, and `pytest`
  and the lint gate both stay green. Latent until it lands on a
  rendered docstring, autodoc never reading an underscore-prefixed
  function's own.
- **`caplog` sees nothing this tree's logger emits**, `Node.logger`
  being built without `logging.getLogger()` and so having no parent to
  propagate to. A test asserting against `caplog.records` passes by
  asserting nothing, which is why the entry says how to observe that
  logger instead.
- **A peer session may hold the same tree**, which the tracker does not
  say and `git worktree list` does: two sessions produced two branches
  for one issue within two minutes, each having checked first.
- **The `union` driver's silence hides two defects that do not travel
  together** -- the entry placed below the one already there, which
  the organization's bottom-append rule wants, and the eaten blank
  line, which is damage under either convention. Measured three times;
  the reconstruction is owed because the silence does not say which
  happened.
- **`len(active_chain)` is the height a block extending the chain
  would connect at**, genesis sitting at index 0, which is what a
  mempool check wants and what an off-by-one in
  `verify_mempool_acceptance` had wrong until #569.

### `Config(pruned=True)` raises rather than being silently ignored (closes #574)

- **`Config.__init__` refuses `pruned=True` with the new
  `PruningNotImplementedError`** (`src/btclib_node/exceptions.py`)
  instead of storing it and doing nothing with it: `BlockDB` never
  deletes a file, so the previous behaviour wrote every block of
  mainnet to `data_dir` for a caller who had asked it not to.
  `pruned=False`, the default, still constructs.
- **The parameter and the field stay** -- `pruned` is public API, and
  nothing here is removed for it -- with a field comment on
  `Config.pruned` explaining the refusal, in place of the two comments
  that used to cite `pruned` as an ordinary, freely settable example
  alongside `debug` and the other booleans.
- **btclib-org/btclib-node#601 is where pruning itself gets built.**
  The exception's own docstring cites what Bitcoin Core does instead --
  drops `NODE_NETWORK` from its advertised services, keeps only
  `NODE_NETWORK_LIMITED`, and deletes a block file once it is more than
  `MIN_BLOCKS_TO_KEEP` (288 blocks) behind the tip.

### The declared authors are the collective the tree names (closes #598)

- **`pyproject.toml`'s `[project].authors` is `The btclib developers
  <devs@btclib.org>`**, which is what `btclib`, `btclib-secp256k1`,
  `btclib-benchmarks` and `bitcoin-core-rpc` declare in the same field.
- **The archive around it names that collective.** `license-files`
  ships `LICENSE`, whose notice reads `Copyright (c) The btclib
  developers`, and `AUTHORS.md`, which points at the contributor graph
  rather than listing anybody; `docs/source/conf.py` gives `author` and
  `project_copyright` the same name.
- **The notice every source file opens with is a gate rather than a
  convention**: ruff's `CPY001` is selected here, and what it checks the
  head of each file against is `notice-rgx` in this file, which spells
  the holder out -- a header naming anyone else is reported
  `missing-copyright-notice`.
- **`authors` is the field an index displays**, and an index shows what
  the version it holds was uploaded with:

  ```shell
  curl -s https://pypi.org/pypi/btclib-node/json \
    | jq -r '.info | .author, .author_email'
  ```

  answers for `v2026.8.27`, so the page moves with the release cut from
  this cycle rather than with this change. Both fields, because newer
  core metadata puts a name-and-address entry in `Author-email` and
  leaves `Author` unset -- `btclib-secp256k1` and `bitcoin-core-rpc`
  answer `null` to the first under this same value, so reading `author`
  alone would report a change that landed as one that did not.
- **The field records who publishes, not who wrote the node.** Giacomo
  Caironi wrote it and the revision history holds that permanently;
  `AUTHORS.md` is where the archive says the members of the collective
  are listed, and section 3 of the organization standard is what puts it
  in `license-files` for that purpose.
- **Nothing measures this field.** The standard says nothing about
  `[project].authors` and no test in this tree reads it, which is
  btclib-org/.github#534.

### `Connection.stop` closes the socket on the loop's own thread (closes #518)

- **`stop`, called from a thread that is not `self.loop`'s, no longer
  closes `self.client` itself.** It schedules a new method, `_close`,
  onto the loop through `call_soon_threadsafe`, and `_close` removes any
  reader and any writer registered for the socket's fd before closing
  it. `BaseSelectorEventLoop._sock_read_done` (`asyncio/selector_events.py`)
  calls `remove_reader` once `run`'s own `sock_recv` future completes or
  is cancelled, and `_remove_reader` takes `_selector.modify` rather than
  `unregister` where a writer is still registered on the same fd --
  `Connection._send`'s own `sock_sendall`, which `async_send` reaches
  through `_deliver`, for a peer not draining its send queue. `modify`
  re-registers, and registering an fd already closed raises
  `OSError: Bad file descriptor` from `KqueueSelector`'s own
  `control()` call; `unregister` alone swallows exactly that error,
  which is why the traceback only ever surfaced with a writer sharing
  the descriptor. Removing both before closing means neither callback
  finds anything left to remove by the time it runs.
- **Reproduced without the fix at 30 of 30 rounds**, on both the GIL
  build and `3.14t`, with a writer registered on the same descriptor as
  a pending `sock_recv`; 0 of 30 with no writer registered. `stop` is
  called from `handle_p2p`, `handle_p2p_handshake`, `callbacks.pong` and
  every other caller that drops a peer for cause, all on `Node`'s own
  thread rather than `P2pManager`'s loop, which is what put every one of
  those calls on the losing side of the race.
- **`tests/unit/p2p/connection_test.py` gains a test driving this from a
  real second thread running the loop** -- `P2pManager`'s own shape --
  with `stop` called from the thread running the test, neither one the
  loop's.

### The peer-versus-node verdict is in the line it logs (closes #526)

- **`handle_p2p_handshake`, `handle_p2p`, `resume_cfilters` and
  `resume_getdata` (`src/btclib_node/p2p/main.py`) each log which of the
  two an exception was decided to be**, rather than the same
  `"Exception occurred"` regardless: the command and the connection
  that raised, and whether that connection was discouraged for it. The
  traceback under the old line answered what raised; it never answered
  whose fault the node decided that was, and that verdict -- the
  `isinstance(e, BTClibException)` check the line sits beside -- existed
  only in memory. `conn_id`, not the peer's own address, is what each
  line carries: Core keys the same judgment the same way, its
  `PeerManagerImpl::Misbehaving` (`src/net_processing.cpp`,
  at bitcoin/bitcoin@05e49b342f) logging `peer=%d` and nothing else,
  and `CNode::LogPeer` appending the address only under `fLogIPs`,
  which is off by default. An address on a line every exception writes,
  whether or not the peer is at fault, is a privacy cost for nothing.
- **The handshake's own `Connected to` line now carries that id beside
  the address** (`p2p/callbacks.py`), which is what makes an id-keyed
  line resolvable back to a peer at all. Core pairs them the other way
  round and only on request; this tree logged the address here
  unconditionally already, so withholding the id bought no privacy and
  only cost the correlation -- an operator could read that connection
  42 was discouraged and never learn which peer 42 was.
- **The other six sites this issue named but did not file are
  unchanged**: none of them weighs a peer's own fault against this
  node's, so none has a verdict to lose. Named by file rather than by
  line, the issue's own numbers having been read at its creation-time
  commit and moved twice since -- `grep -rn "Exception occurred"
  src/btclib_node/` re-derives them, where a number here would go
  stale again.
- **The paragraph above each of the four sites, arguing the
  `BTClibException` split (#283, #515), is left as it was**: it argues
  *why* the split is drawn, which the log line does not restate -- the
  line reports which way the split fell on one run, not why the code
  draws it there at all.
- **Read back from a real `Logger` writing to a file, not from
  `caplog`**: `Node.logger` is built directly from `logging.Logger`
  rather than through `logging.getLogger`, so nothing it emits reaches
  the root logger pytest's capture handler sits on, and an assertion
  against `caplog.records` would pass on every one of the four having
  logged nothing at all.

### The union driver's two defects are one (closes #610)

- **`CLAUDE.md`'s `merge=union` bullet said the driver "places the
  arriving entry below the one already there" and called that the
  *wanted* result under the organization's bottom-append rule.** Hours
  after that landed, on this same tree, an arriving entry went
  **above** the one already there.
- **The two are not independent, and the blank line is the cause.** A
  branch that lost its blank line to an earlier rebase carries that
  loss as context into the next one, and the driver orders on it.
  Isolated on the real case: restoring that one line in the branch's
  own commit, changing nothing else, and rebasing onto the same base
  moves the entry back below. Seven rebases in one day fit it -- six
  carried the blank line and landed below, the seventh had lost it and
  landed above.
- **So an inverted order is a report rather than bad luck**: it says
  this branch was rebased twice and lost its blank line the first
  time. That is worth more than "arbitrary", which was the first
  correction and was wrong.
- **The check has to be a full comparison, not a nothing-was-removed
  assertion.** A misordering repositions rather than deletes, so it
  passes that weaker test -- `RELEASING.md`'s step 3 already argues the
  same insufficiency. And the reconstruction must *normalize* the
  arriving block rather than copy it: an expected file built by copying
  inherits the missing blank line and matches the damage it exists to
  catch, which is how two branches in this batch reached review with
  the markdown gate red.

### A `getdata` answer's `notfound`, and its `inv`, are paced (closes #529)

- **`advance_getdata` (`p2p/callbacks.py`) paces a miss the same way it
  already paces a block or a transaction it does hold**, checked before
  every item rather than once the whole request is served. A miss used
  to cost nothing against `MAX_GETDATA_INFLIGHT_BYTES`, so a `getdata`
  naming mostly transactions this node no longer held committed its
  whole `notfound` on top of whatever blocks in the same request had
  already queued, past `MAX_QUEUED_SEND_BYTES` and into the drop -- for
  a peer whose request had done nothing wrong.
- **`_send_due_announcements` (`download.py`) paces a transaction
  announcement's `inv` against the same bound**, checked before every
  `MAX_INV_SZ` chunk, leaving what a pass could not commit queued for
  its own next call rather than sending as many chunks as the mempool
  had entries to announce, unpaced, in one pass.
- Neither change alters what a well-behaved peer receives, only when a
  large answer's tail arrives; a node that used to drop a connection in
  either shape now paces it instead, which is not a compatibility break.

### `dial` refuses every non-IP network, not every non-IPv4 one (closes #616)

- **`get_peer_info`'s comment (`rpc/callbacks.py`) said `dial` refuses
  "everything but IPv4"**, arguing from that why a BIP155 id no member
  names can reach `PeerDB` but never a `Connection`. `dial`
  (`p2p/address.py`) refuses every id outside
  `_IP_NETWORKS = (IPV4, IPV6)` and then picks `AF_INET6` for the
  second, opening a real socket on it -- an IPv6 peer is dialled, not
  refused. Both halves are already exercised: `manager_test.py`'s IPv6
  accept inbound, and `address_test.py`'s own
  `test_a_v6_peer_that_is_listening_is_connected_to` outbound, which
  binds a real `AF_INET6` listener and asserts the family `dial` opened.
- **The conclusion the comment draws was never wrong, only its
  reason**: the guard is the two IP networks rather than IPv4 alone, so
  the cast below it still stands. What is corrected is a claim about
  the code beside it, which is what a reader checks the cast
  against.

### A connection's id is logged beside its address at creation (closes #611)

- **`P2pManager.create_connection` now logs the id it mints beside the
  address the connection was accepted from or dialled to**, before any
  wire message is parsed. `callbacks.verack`'s own pairing (#526) is the
  last statement `verack` executes, so a handshake exception raised
  before it -- a malformed `version`, the most common one, among them --
  left the id `handle_p2p_handshake`'s own except block logs unpaired
  with any address anywhere in the tree. `create_connection` is the one
  point every path into a connection shares, dialled or accepted, ahead
  of that exception.
- **Unconditional on the address, like `verack`'s own line and for the
  same reason, argued there rather than twice here**: Core's analogous
  site, `CNode`'s own constructor (`src/net.cpp`, at
  bitcoin/bitcoin@05e49b342f), gates the address on `fLogIPs`, off by
  default.
- **`verack`'s own line is kept, not removed**: it marks the handshake
  completing, this one marks the connection existing, and the two moments
  are what an operator needs told apart to place where a connection that
  disappeared between them actually got to.
- **`info`, matching `verack`'s own line**: `async_connect` and
  `_maybe_dial_more_peers` only reach `create_connection` once `dial`
  has already returned a live socket, so this runs once per connection
  actually made, never once per dial attempt.

### The 100% floor's `main.py` loss did not reproduce (closes #617)

- **Seven whole-suite runs on this repository's ten-core machine — five
  at the default `-n auto`, two at an oversubscribed `-n 20` — with
  `COVERAGE_DEBUG=dataio,combine` and `COVERAGE_DEBUG_FILE` pointed
  outside the rootdir, each combined exactly one data file per worker
  plus the master's own, losing none of them.** The candidate mechanism
  named in #617 — `xdist`/`pytest-cov`'s own parallel-data combine
  dropping a worker's `.coverage.*` file — was not caught in the act,
  on the same coverage 7.15.4 / pytest-cov 7.1.0 / pytest-xdist 3.8.0
  pins the tree already carried at #617's own sha.
- **The silence such a drop would leave is real, whether or not it
  happened here**: `pytest-cov`'s `DistMaster` never passes
  `messages=True` to the `coverage.Coverage()` it drives its
  `combine()` through, so a dropped or duplicate-skipped file changes
  nothing a stock run prints. `CLAUDE.md`'s *Non-obvious facts* carries
  the discriminator against ISS 372 and ISS 319, and what to do if the
  shape recurs. Nothing in the coverage configuration is changed by
  this: a change made without a demonstrated mechanism behind it would
  be believed and could hide the next occurrence.

### `bitcoin-core-rpc` is declared where it is used (closes #606)

- **`tests/functional/rpc/chain_test.py` and `tx_test.py` import
  `bitcoin_core_rpc` at module top, and `pyproject.toml` said nothing
  about it**: the package arrived transitively, as one of btclib's own
  dependencies. The reasoning for those top-level imports lived only in
  this file, in the entry that made them -- so if btclib ever dropped or
  gated that dependency, the suite would break with nothing in this
  repository's own metadata to say why, and no lockfile diff here to
  point at.
- **`>=2026.8.20` in the `test` group, which is the version `uv.lock`
  already pinned.** The floor is deliberately the resolved version and
  not a newer one: this declares what the tree relies on rather than
  deciding to move onto something. The re-lock adds only the dependency
  edges, no package entry having had to move.
- `tests/integration/conftest.py` still hand-rolls that client instead
  of importing it, which is a separate change (issue #607) and is not
  closed here.

### `tests/integration/conftest.py` imports `bitcoin_core_rpc` (closes #607)

- **`Bitcoind.rpc` was `requests.post` with `auth=(user, password)`, the
  cookie parsed by hand with `.partition(":")`, its path assembled by
  hand as `datadir / "regtest" / ".cookie"`, and its errors raised as
  `RuntimeError`** -- `bitcoin_core_rpc`'s own `BitcoinCoreRpcClient`,
  `cookie_auth` and `cookie_path_from_chain` rewritten against a real
  bitcoind, that library's home ground. `Bitcoind.rpc` is now a thin
  wrapper over `BitcoinCoreRpcClient.call`, built with the
  explicit-url constructor rather than `from_chain`: the fixture starts
  bitcoind on a port `get_random_port` drew, not Core's regtest
  default.
- **The comment defending the copy -- "the cookie is read again on
  every call rather than cached at construction" -- described the
  library's own behaviour and not something the copy did differently**:
  `cookie_auth` re-reads the file at every call already, so importing
  the client keeps that property rather than losing it.
- **`_wait_for_rpc` waited on a bare `except Exception`; it now waits on
  `FetchError`**, `bitcoin_core_rpc`'s own base for a cookie not yet
  written (`CookieNotFoundError`) or an rpc socket not yet listening.
- Exercised against a real `bitcoind` v31.1.0 with
  `BTCLIB_NODE_INTEGRATION=1` (`tests/integration/bitcoind_test.py`,
  `backpressure_test.py` and `reorg_test.py`, 3 passed), since the
  ordinary gate only collects this package and skips its tests without
  that switch and a daemon.

### `[project.urls] homepage` names this tree's own documentation (closes btclib-org/.github#533)

- **`[project.urls] homepage` reads `https://btclib-node.readthedocs.io`,
  matching `documentation`.** A releasing tree provides documentation,
  and its home is that documentation rather than `btclib.org`, a
  sibling's project page. `documentation` stays: an index showing the
  two fields as one link is cheaper than the field tools read for that
  purpose specifically.
- **`REPOSITORY.md`'s Read the Docs section records `.homepage`, read
  back from the endpoint rather than from `pyproject.toml`'s own copy of
  it.** The bullet in "What is not configured, and why" that called the
  field unset is gone with it: the reason it gave for leaving the field
  unset -- no published site to point at -- was already answered by
  <https://pypi.org/project/btclib-node/>, and the field it argued was a
  decision of its own now agrees with `documentation`.

### CONTRIBUTING.md's reporting walk names `scorecard.yml` too (closes #595)

- **`.github/workflows/scorecard.yml` is now among the workflows *What
  gates a merge, and what only reports* walks.** It carries neither a
  `pull_request` trigger nor `workflow_dispatch`, unlike every other
  workflow that section names, so it is also the one reporting workflow
  with no way to ask about a branch at all.

### `MAX_PENDING_CFILTERS_HEIGHTS` no longer quotes a stale sentence (closes #537)

- **The comment beside `MAX_PENDING_CFILTERS_HEIGHTS` quoted a sentence
  `connection.py`'s `MAX_QUEUED_SEND_BYTES` no longer carries** --
  rewritten away when `get_cfilters` gained its own pacing bound
  (#442), the same commit that added the quotation here. The paragraph
  now argues the two-full-requests room on its own terms instead of
  citing a sibling constant's reasoning; the bound's own value is
  unchanged.

### REPOSITORY.md's must-not-require list names `scorecard.yml` (closes #626)

- **`scorecard.yml` joins `links.yml`, `os-macos.yml`, `bootstrap-dns.yml`
  and `claude-review.yml` in the list of workflows that must not become
  required checks.** It carries neither a `pull_request` trigger nor
  `workflow_dispatch`, so unlike the other four a required check on it
  could never be satisfied by any pull request at all.

### The rpc listener's default port is Core's own, not `p2p_port + 1` (closes #605)

- **`Chain` carries its own `rpc_port` now, one below `port` on every
  leaf** — 8332/18332/38332/18443 for mainnet/testnet/signet/regtest,
  Core's own `CreateBaseChainParams` (`src/chainparamsbase.cpp`, at
  bitcoin/bitcoin@05e49b342f). `Config.__init__` derived it from
  `chain.port + 1` instead, which happens to be Core's own Tor
  incoming-connection port for each of these four chains rather than
  its rpc one, so a client left on Core's own rpc default never found
  this node.
- **`tests/unit/config_test.py::test_default_rpc_port_is_cores_own`
  checks the default for all four chains against
  `bitcoin_core_rpc.rpc_port_from_chain`, read independently of this
  node's own `Config`** — every functional rpc test passes an explicit
  `rpc_port` of its own, so none of them exercised the default this
  issue was about.

### An I/O fault trying a block is not the block's own fault (closes #620)

- **`update_chain`'s trial loop tells a content failure from a storage one
  by exception type, not by which call raised it.** `_CONTENT_FAILURE`
  (`main.py`) is `BTClibValueError`, `InvalidBlockInputError` and
  `PrevoutCountMismatchError` -- what `_validate_block` and
  `utxo_index.add_block`'s own BIP30/double-spend checks raise to refuse
  a candidate's own content. Everything else the trial's `to_add` loop
  raises -- a `KeyValueStore` read or write failing inside
  `utxo_index.add_block` or `filter_index.add_connected_block`, or
  `ChainstateInconsistencyError` -- rolls the trial back and then
  propagates out of `update_chain`, rather than invalidating the block it
  happened to land on.
- **Core keeps the same split inside `ConnectBlock`** (`src/validation.cpp`,
  at bitcoin/bitcoin@b91d983f66): an ordinary `CheckBlock` failure is
  rejected, a `BLOCK_MUTATED` one is `FatalError`.
- A propagated exception reaches `Node._step_chain`, whose own existing
  catch stops the main loop and closes every database, the same path a
  failure out of `_blocks_to_add`, `_rev_blocks_to_remove` or
  `_finalize_fork` already took.

### The UTXO cache survives across connected blocks (closes #586)

- **`UtxoIndex.updated_utxo_set` and `removed_utxos` now stage several
  connected blocks' own changes rather than one, up to
  `UtxoIndex._FLUSH_BOUND` (500,000 entries), and `main._finalize_fork`
  writes them only once `UtxoIndex.should_flush` says that bound is
  reached.** Before, `_finalize_fork` flushed on every connected block
  -- one sqlite read and one write per input for the length of the
  chain, the larger half of a sync and the only one of the two this
  node cannot make faster by adding cores, blocks connecting one at a
  time on this store's single writer.
- `BlockIndex.stage_status` and `FilterIndex`'s own `pending` are held
  back the same way, and `Chainstate.flush` writes all three -- the
  block a status names, the UTXO set it was validated against, and the
  filter built from it -- into the one SQLite transaction this store
  already gives a caller, so it never advances one of the three past
  another. `Chainstate.close` flushes before closing, so a clean stop
  loses nothing staged.
- **What an unclean stop costs is decided rather than left implicit.**
  The store reopens holding exactly the state of its last flush, and a
  block validated since is simply offered to `update_chain` again --
  `check_transactions` included -- the same way a block arriving for
  the first time is, rather than through a replay path of its own.
  `db.py`'s docstring argues this against Bitcoin Core's own
  `FlushStateToDisk`/`ReplayBlocks`, which writes a separate block-tree
  LevelDB and a separate coins LevelDB in sequence and reconciles a
  crash landing between the two; this store's one shared, one-batch
  write has no such gap to reconcile.
- `UtxoIndex.rollback` and `FilterIndex.rollback` undo only the
  mutations a failed trial itself made, through a small per-trial undo
  log, rather than wiping every staged change: a trial rolled back
  against an earlier, already-succeeded trial's own still-unflushed
  state would otherwise discard that state too.
- `UtxoIndex.get_coin` reads a coin through the staged dicts before the
  store, and `main.verify_mempool_acceptance` calls it rather than
  reading `UtxoIndex.db` directly, for the same reason the staging
  exists at all: a coin several blocks' own worth of staging created is
  real before `finalize` ever writes it out.
- **`BlockIndex.set_status` -- and so `invalidate`, its own caller --
  routes through `pending` rather than writing straight through where
  `pending` already holds the hash being set.** `update_chain` sets
  `failed_hash` to a block across `utxo_index.add_block`,
  `_validate_block`, `block_db.add_rev_block` and
  `filter_index.add_connected_block` alike, so a fault in either of the
  last two -- an I/O failure, nothing to do with the block's own
  content -- invalidates a block exactly as a real validation failure
  would, and can reach a hash a chain-tip flip-flop already staged in
  `pending` (disconnected, then offered again). A write-through there
  used to be undone by the very next `finalize`, which still held the
  stale pending entry and wrote it back over the invalidation, with no
  crash needed. `tests/unit/chainstate/block_index_test.py`'s
  `test_invalidate_after_stage_status_is_not_undone_by_a_later_finalize`
  reproduces it against the write-through and passes against the fix.
- **`utxo_index.py`'s own entries bound is now argued with a measured
  figure**: 500,000 `(serialized OutPoint, Coin)` pairs held in a plain
  dict, measured with `tracemalloc`, come to about 229 MB -- the same
  order as Core's own 450 MiB `-dbcache` default the comment already
  cites for contrast.
- **`UtxoIndex.apply_rev_block` unmarks a restored prevout from
  `removed_utxos`, not only puts it back into `updated_utxo_set`.** A
  reorg restoring a prevout that was durable when the block being
  undone spent it left the outpoint in both: `add_block` had staged
  that spend with `_mark_removed`, and `apply_rev_block`'s own restore
  never cleared it. A later, legitimate spend of the same output then
  hit `add_block`'s own double-spend guard and the block carrying it
  was invalidated for good, stranding the node on a lighter fork. This
  was unreachable before staging crossed trial boundaries -- the old,
  per-trial `finalize` wiped both dicts, and the stale flag along with
  them, before a second trial could ever see it.
- **The same stale flag also hid a genuine BIP30 duplicate of the
  restored output.** `_bip30_violation` reads `removed_utxos` before
  `updated_utxo_set` or the store, so a block recreating an outpoint
  that a reorg had just made unspent again connected instead of being
  refused `bad-txns-BIP30` -- CVE-2012-1909's own shape, independently
  of the double-spend-guard consequence above; `_unmark_removed`
  running unconditionally fixes both at once.
- **`UtxoIndex.add_block`'s own two creation loops now unmark a
  recreated outpoint from `removed_utxos` before staging it, the same
  order `apply_rev_block`'s own restore already used.** Spending a
  durable output stages it into `removed_utxos` alone; a later block,
  still uncommitted, recreating that exact outpoint's own txid then
  staged the recreation into `updated_utxo_set` while `removed_utxos`
  still carried it, leaving the outpoint in both at once. A subsequent
  `apply_rev_block` undoing that recreation raised
  `ChainstateInconsistencyError("output already removed")` on a coin
  that was legitimately staged and unspent. Unreachable through the
  node's own production caller, which validates a candidate block's
  BIP34 commitment before this staging could ever be reached a second
  time for the same coinbase, and a non-coinbase duplicate txid is
  already a plain double spend caught elsewhere -- so this is a
  private-method invariant made to hold by construction rather than a
  reachable fault.

### `CLAUDE.md`'s Following Bitcoin Core says always, not reasonable (closes #638)

- **The section now reads "matches Core's behaviour, always", replacing
  "wherever that is possible and reasonable"**, and names the permitted
  axis of variation as the language -- Python-native, fluent or
  efficient in Python where Core's own line is shaped by C++ -- rather
  than a design weighed against Core's. #605's rpc-port-from-p2p-port
  argument and #631/#636's framing of a corrupt-store abort as a
  justified divergence are what "reasonable" had licensed.
- **`src/btclib_node/db.py`'s example is relabelled rather than
  removed.** Stdlib `sqlite3` over Core's vendored LevelDB is a
  stdlib-versus-compiled-dependency choice, on the permitted axis
  rather than an exception the rule carves out for this tree's own
  constraints.
- **A clarification says mimicry is of Core's behaviour end to end, not
  of a local `catch`**: Core's `CDBWrapper::Read` answers "absent" on a
  deserialize failure only because LevelDB's own checksum has already
  made real corruption fatal before that line runs, so a tree without
  that checksum which raises instead is reproducing Core's behaviour
  rather than departing from it.
- **A clarification says a capability Core has that this tree lacks,
  for a library reason rather than a decision, is a gap to close and
  not a constraint to design around** -- btclib-org/btclib-node#637 and
  btclib-org/btclib-node#641's missing checksummed VFS is the live
  case.
- `src/btclib_node/download.py`'s own docstring carried the same
  "possible and reasonable" wording and is corrected to match.

### The suites drive the seams bitcoin-core-rpc ships (closes #632, closes #633)

- **The test-group floor moves to `bitcoin-core-rpc>=2026.8.29`**, for
  `call_raw`, `call_batch` and `SessionTransport`.
- **`tests/functional/rpc/*.py`'s hand-rolled `requests.post` envelopes
  are built through `BitcoinCoreRpcClient.call_raw` wherever it can
  express the case** -- the protocol marker as an argument, the reply
  read exactly as far as `call_raw` reads before interpreting it. What
  stays on `tests/__init__.py`'s own `post` helper: a request missing
  `method` or `id`, since `call_raw` always carries both; a `method`
  that is not a string, since `call_raw` refuses to build one; and the
  bare `[]` empty batch, which neither `call_raw` (one object per post)
  nor `call_batch` (refuses an empty `calls`) can send. No well-formed,
  multi-member batch is posted anywhere in the suite, so `call_batch`
  itself has no existing case to convert.
- **`requests` is gone from the `test` dependency group.** `post` is
  rebuilt on `bitcoin_core_rpc.http_request`, the same seam `call_raw`
  itself posts through, so a raw envelope no longer needs a second HTTP
  client to stay raw. `types-requests` goes with it from `lint`.
- **`tests/integration/conftest.py`'s `Bitcoind` wrapper drives its
  client through a `SessionTransport`.** A real bitcoind keeps an rpc
  connection alive across calls, unlike this node's own listener, and
  `reorg_test.py` and `backpressure_test.py` each submit their chain
  one `bitcoind.rpc` call per block. Measured directly against a live
  regtest bitcoind, one connection kept open across many sequential
  calls answers each measurably faster than a fresh connection would;
  neither integration module's own wall time moves, both being
  dominated by bitcoind's own block validation rather than by the rpc
  round trip.
- **A functional test drives several calls through one `SessionTransport`
  against this node's own listener and pins what it does with the
  connection**: `RpcConnection.async_send` closes its socket
  unconditionally after every reply, with no idling and no keep-alive
  for `SessionTransport`'s own probe to find, so the transport
  reconnects on every call rather than reusing one -- the harness stays
  on the default transport here, the measurement above being what
  argues for `SessionTransport` at the integration client and not for
  the functional one.

### `apply_rev_block` undoes an in-block chained transaction (closes #634)

- **`UtxoIndex.apply_rev_block` applies `to_add` before `to_remove`**
  (`src/btclib_node/chainstate/utxo_index.py`), the reverse of the
  order `add_block` builds them in. An ordinary chained transaction --
  one spending an output another transaction earlier in the *same*
  block created -- puts that output's outpoint in both lists:
  `to_remove` from being created, `to_add` from being spent before the
  block ever finalized it to disk. Its net effect on the persisted set
  is nothing, both before the block and after it, and removing first
  looked it up while it was in neither `updated_utxo_set` nor the
  database, raising `ChainstateInconsistencyError("output not found")`
  on a block that did nothing wrong.
- **The consequence was a stuck node, not a corrupted one.**
  `update_chain`'s `to_remove` loop (`src/btclib_node/main.py`) never
  sets `failed_hash`, so the raise rolled the trial back without
  writing anything and without invalidating the block, and the next
  pass of `update_chain` picked the same fork again -- any reorg
  disconnecting a block with a chained transaction, ordinary on
  mainnet, retried forever rather than completing.
- **Core's `DisconnectBlock` (`src/validation.cpp`, at
  bitcoin/bitcoin@05e49b342f) reaches the same result walking one
  transaction at a time in reverse block order** -- spend its own
  outputs, then restore its own inputs -- rather than in the two flat
  passes here; the two agree because a valid block spends a given
  outpoint at most once, so a chained output's create and spend never
  collide with anything but each other.

### A corrupted stored coin is this node's fault (closes #631, closes #636)

- **`Coin.parse` reading back a `utxo-` record inside
  `UtxoIndex.get_coin`'s own store fallback is now wrapped the way #620
  wrapped `UtxoIndex.add_block`'s own read of the same kind of record: a
  `BTClibValueError` it raises re-raises `ChainstateInconsistencyError`,
  the key it reads being one only this node's own `UtxoIndex.finalize`
  ever writes, so a submitted transaction's own bytes never reach that
  call.** `send_raw_transaction` and `test_mempool_accept`
  (`rpc/callbacks.py`) used to answer a corrupted own-stored record the
  same way they answer a genuinely bad script --
  `VERIFY_REJECTED`/`"Invalid signatures or script"` -- misattributing
  this node's own storage fault to the transaction it happened to be
  checking.
- **Neither RPC handler needed a new catch of its own.**
  `send_raw_transaction` has no clause for `ChainstateInconsistencyError`,
  so it propagates into `handle_rpc`'s own generic catch and is answered
  as an internal error instead of a refusal; `test_mempool_accept`'s own
  per-entry loop already carries a catch-all beneath its two specific
  ones, and reports the one entry `"Unknown error"` instead.
- **The peer-to-peer `tx` handler (`p2p/callbacks.py`) needed no change
  either.** It already lets anything but `MissingPrevoutError` propagate
  into `handle_p2p`'s own generic catch, which stops that one connection
  and discourages the peer only for a `BTClibException`.
  `ChainstateInconsistencyError` is a plain `RuntimeError`, so a
  corrupted own-stored record no longer discourages the peer that sent
  a transaction this node itself could not read back -- a
  `BTClibValueError` reaching the same path used to.
- **Core detects this at the storage layer, where this tree has
  nothing, so the two differ in where the fault is caught rather than
  in what either chose.** `CDBWrapper::Read`
  (`src/dbwrapper.h:220-237`, at bitcoin/bitcoin@05e49b342f) does catch
  a deserialize failure and treat the record as absent -- but it never
  sees storage-layer corruption to begin with: `CDBWrapper` reads with
  `verify_checksums = true` and a mismatch reaches `HandleError`, which
  throws before `Read`'s own `try` runs at all, so what that `try`
  catches is a format mismatch on an intact record. `db.py`'s own store
  carries no equivalent guard, which makes `Coin.parse` the only point
  this node ever learns a `utxo-` record it wrote is unreadable back --
  at the read above and at `UtxoIndex.add_block`'s own read of the same
  kind of record (#620). Answering Core's literal way there, silently
  absent, would fold a node-owned fault into an ordinary "prevout
  missing" refusal. `ChainstateInconsistencyError`'s own docstring
  carries the argument, including what about Core's behaviour is *not*
  settled: whether its RPC and net-processing paths, which each catch
  `std::exception`, absorb `dbwrapper_error` where its own `AbortNode`
  machinery says the intent is to stop. Neither call site here rests on
  that. btclib-org/btclib-node#637 is the missing guard and
  btclib-org/btclib-node#641 is whether the store lacking it is the
  right one (#636).

### The btclib floor is the release cut today

- **`pyproject.toml` floors `btclib[secp256k1]` at the sibling's
  v2026.8.29**, the release this tree is developed against: the one
  that puts btclib itself on the post-vendorability
  `bitcoin-core-rpc` and publishes `SessionTransport` in its fetch
  seam. `uv.lock`'s git pin moves to btclib's `main` past that
  release, which is what the suite exercises.

### `dev` reaches every group this tree declares (issue btclib-org/.github#498)

- **`pyproject.toml`'s `dev` group now includes `fuzz` alongside `test`,
  `lint`, `mutation`, `docs` and `check`.** Section 1 of the organization
  standard's dependency-group table gives `dev` as every group above, and
  `fuzz` carried no exception to it: its `sys_platform == 'linux'` marker
  is what makes `uv sync` on another platform resolve the group rather
  than refuse it, so a developer off Linux gets a `dev` sync that installs
  nothing from `fuzz` instead of one that fails. `uv.lock` moves with it.

### A failed rollback stops the node instead of continuing on it (closes #623)

- **`update_chain`'s `to_remove` loop no longer swallows a
  `ChainstateInconsistencyError` its own `apply_rev_block` raises**
  (`src/btclib_node/main.py`): the exception now propagates out of
  `update_chain`, the same way a non-content failure already does for
  the `to_add` loop, `_blocks_to_add`, `_rev_blocks_to_remove` and
  `_finalize_fork`, and `Node`'s own loop leaves and closes the
  databases on it. Undoing an already-connected block is this node's
  own bookkeeping rather than a verdict on a new block, so the trial
  still rolls back first and still blames no block for it -- only
  whether the failure is fatal to the node has changed, matching
  `DisconnectTip`'s own failure being fatal one level up, in
  `ActivateBestChainStep` (`src/validation.cpp`, at
  bitcoin/bitcoin@b91d983f66).

### The suite passing free-threaded is now a claim the metadata makes (closes #593)

- **`pyproject.toml` declares `Programming Language :: Python :: Free
  Threading :: 3 - Stable`.** `test.yml`'s `coverage` job runs the whole
  suite on `3.14t`, at the 100% coverage floor, on every merge, and the
  classifier is that claim made to an index. The comment beside it says
  the claim is about the free-threaded build working and not about
  throughput: the UTXO store is the larger half in wall clock and is
  serial regardless of the interpreter build (issue #576).

### pypi-install installs from the index (closes #502, closes #287)

- **`.github/workflows/pypi-install.yml` installs `btclib-node` from PyPI
  weekly, at this repository's own slot in section 10's calendar, and
  checks it past a bare import.** A bare import cannot see this
  package's own runtime path: `btclib[secp256k1]` is an unconditional
  dependency, and a broken pair on the index degrades silently to the
  Python arithmetic rather than failing, so one step asserts the
  bindings serve and a second builds and starts a `Node` on regtest, the
  same shape `tests/conftest.py`'s own `node_context` already exercises
  against a source tree. The image matrix is every platform this
  repository's own `os-*` sentinels already sample, `windows-latest`
  included: a stranger can install this from the index regardless of
  what `pyproject.toml`'s classifiers claim, and `os-windows.yml`'s own
  accepted-red convention is what that cell inherits rather than
  re-decides.
- **`release.yml`'s `publish-pypi` job calls the new workflow instead of
  carrying its own inline install-and-import check**, `needs:
  publish-pypi` paired with its own `always()` and an explicit
  `needs.publish-pypi.result == 'success'` -- a bare `needs:` here reads
  back through `publish-pypi`'s own `needs:` to `public-api` two hops
  away, which a real breaking-change failure there would then skip
  silently despite `publish-pypi` itself having succeeded
  (btclib-org/btclib#1470 is where that shape was measured on a tag).
  `publish-testpypi` uploads and stops, with no install-verify step of
  its own: `pypi-install.yml` reads `pypi.org` and has nothing to say
  about `test.pypi.org`, matching every other repository that publishes.

### The store moves to RocksDB, through `rocksdict` (closes #637, closes #641)

- **`src/btclib_node/db.py`'s `KeyValueStore` moves from the standard
  library's `sqlite3` to RocksDB, through `rocksdict`, behind its
  unchanged interface.** #641 is where sqlite3's own lack of a checksum
  layer was measured against `Coin.parse`'s own detection rate over a
  corrupted `utxo-` record (4% of single-bit flips caught, 96% parsed
  cleanly into a wrong `Coin`, 23% of those a silently wrong amount) and
  against the fault an in-record checksum alone (#637) cannot reach at
  all -- a corrupted key, which makes the record it names cease to
  exist rather than read back wrong. RocksDB is LevelDB's own fork and
  carries the per-block CRC32C checksum Core's own
  `verify_checksums = true` relies on; `rocksdict` is what ships it as
  a wheel, on nine platforms, against `plyvel`'s own dead `cp314`
  support.
- **The configuration matches Core's `GetOptions`/`DBParams`
  (`src/dbwrapper.cpp`, `src/dbwrapper.h`) line by line**, cited at
  bitcoin/bitcoin@ca7162cde5 in `db.py`'s own module docstring: no
  compression, paranoid checks, checksums verified on every read and
  scan, the storage engine's own bloom filter (unrelated to BIP37's
  deprecated, peer-relay one), and a block cache and write buffer sized
  from what Core's own default `-dbcache` gives the coins database
  specifically, computed rather than guessed.
- **The schema version moves out of `PRAGMA user_version` and into its
  own RocksDB column family**, kept out of the default column family's
  key order for the same reason the old version lived outside `kv`'s
  own rows. **The datadir marker inverts**: RocksDB writes the
  `CURRENT` file LevelDB used to, so the refusal this store raises is
  now for a directory holding the old store's own `index.sqlite`, with
  the same message.
- **A corruption `rocksdict` itself detects is now `StoreCorruptionError`**
  (`exceptions.py`), raised at every read this store makes -- `get`,
  `__iter__`, and the open itself -- classified from `rocksdict`'s own
  untyped `Exception`, whose message begins `Corruption:`, since only
  its `DbClosedError` carries a type of its own to catch instead.
  `ChainstateInconsistencyError`'s own docstring is corrected where it
  used to say this tree carried no such layer; what a `Coin.parse`
  failure now means on a record this new guard already passed as
  intact is btclib-org/btclib-node#650's own question.
- **`__iter__` and the internal read `_check_schema_version` uses to
  tell an empty store from one this class predates both walk the store
  through `rocksdict`'s lower-level `Rdict.iter`, not the higher-level
  `Rdict.items`/`Rdict.keys`.** Measured directly: the higher-level
  wrappers answer a corrupted block the way they answer the genuine end
  of the store -- nothing raised, fewer pairs than were written, in one
  case none at all -- because neither ever calls the separate `status()`
  the underlying iterator reports its own fault through. `db.py`'s own
  module docstring has the two measurements.
- **`pyproject.toml` floors `rocksdict` at `0.3.29`**, the release
  measured against; the `sqlite` keyword becomes `rocksdb`, and
  `.python-version`'s own comment is corrected -- `rocksdict` ships
  `cp314` wheels and no `cp314t` wheel at all, where the comment used
  to claim every compiled dependency in the lock had one.
- **`RELEASE_NOTES.md` carries the action this forces**: a datadir
  written by the sqlite3 store cannot be read by this version, and is
  refused by name rather than silently started over as an empty chain.

### `getblockchaininfo` answers a sync in progress (closes #575)

- **`blocks`, `headers`, `bestblockhash`, `bits`, `target`, `difficulty`,
  `time`, `mediantime`, `chainwork`, `initialblockdownload` and `pruned`
  join `chain`**, each with Core's own meaning
  (`src/rpc/blockchain.cpp`, at bitcoin/bitcoin@ca7162cde5). `headers`
  is `BlockIndex.header_index`'s own height, tracked separately from
  the validated `active_chain` `blocks` answers -- the gap between the
  two is a header sync in progress, invisible until now to anything
  that could only read `getblockcount`.
- **`initialblockdownload` is `Node.is_initial_block_download`, a new
  one-way latch on `Node` that reproduces Core's own
  `IsInitialBlockDownload`** (`src/validation.cpp`, at
  bitcoin/bitcoin@ca7162cde5): the active chain's work must reach a new
  per-`Chain` `minimum_chain_work` (`chains.py`, cited to
  `chainparams.cpp`) *and* its tip must be no older than a new
  `MAX_TIP_AGE` of 24 hours (`constants.py`, cited to
  `DEFAULT_MAX_TIP_AGE`), checked in `main.update_ibd_status`. This
  replaced a first pass that read `NodeStatus`'s own latch to
  `BlockSynced` instead, a second, looser definition of the same Core
  concept the review of this closing round caught.
  `download.py`'s `_send_due_feefilters` keeps reading
  `NodeStatus.BlockSynced` rather than moving onto the new latch: tried
  the other way once, and reverted, because a regtest chain built with
  this tree's own default, `GENESIS_TIME`-relative timestamps never
  satisfies `MAX_TIP_AGE`, so a node that had validated its entire known
  chain would advertise the top `feefilter` bucket to every peer
  forever, and one measured functional test regression back that up
  (`tests/functional/p2p/tx_test.py::test_send_tx`, timing out against a
  peer refusing to announce a transaction below that bucket).
  `verificationprogress` stays absent: Core's own
  `GuessVerificationProgress` extrapolates from `ChainTxData`, an
  assumed per-chain transaction rate this tree does not carry, and
  answering a different number under Core's own name would be
  answering the wrong thing under it. `get_blockchain_info`'s own
  docstring (`src/btclib_node/rpc/callbacks.py`) has the citation for
  every member answered and every member still left out.

### `btclib-node` reads `bitcoin.conf` (closes #583, closes #581, closes #573)

- **`pip install btclib-node` puts `btclib-node` on `PATH`**
  (`[project.scripts]`, `pyproject.toml`), and `python -m btclib_node`
  reaches the same entry point (`src/btclib_node/__main__.py`) for a
  checkout nobody has installed yet. `src/btclib_node/cli.py` is the
  module behind both, built on `argparse` rather than click: this
  wheel's SBOM (`.github/scripts/generate_sbom.py`) still names one
  runtime component, `btclib`.
- **Every flag is spelled the way Core spells its own** --
  `-datadir=<dir>`, `-conf=<file>`, `-chain=`/`-testnet`/`-signet`/
  `-regtest`, `-port=`, `-rpcport=`, `-rpcbind=`, `-prune=`, `-debug`,
  `-connect=`, `-addnode=`, `-listen=`/`-nolisten`, each also accepted
  with a double dash -- and `-h`/`--help` lists every one. A page
  walking an installed user through what the command actually enforces
  is still owed (issue #577), filed rather than written here.
- **`-conf`'s file is read the way Core reads `bitcoin.conf`**: a
  `key=value` line per option named without its leading `-`, a
  `[section]` per chain (`main` included) plus one default section,
  `includeconf=` resolved against the data directory, and the command
  line always taking precedence over the file. `cli.py`'s own module
  docstring is where the precedence, and every simplification against
  Core's own reader, is argued.
- **`Config` takes `connect` and `addnode`, sequences of `host:port`
  pairs `Node.run` dials at startup** through
  `p2p_manager.connect(peer_address(...))`, the same route the
  integration suite already dials a real `bitcoind` through.
  `-connect` dials those alone and turns off DNS seeding, every
  automatically-drawn outbound connection
  (`P2pManager.use_addrman_outgoing`) and, by default, this node's own
  P2P listener (`P2pManager.listen`) -- Core's own
  `InitParameterInteraction` turns off the same three, and an explicit
  `-listen`/`-listen=1` still wins over that default the way Core's own
  `SoftSetBoolArg` does. `-connect=0` dials nobody but still counts as
  `-connect` for that interaction, matching
  `connect.size() != 1 || connect[0] != "0"`. `-addnode` dials alongside
  the ordinary draw. Reading Core's own `blk*.dat` files is not
  implemented: the p2p route above already delivers the same blocks
  with no new parser.
- **`scripts/chains/mainnet.py`, `testnet.py` and `signet.py` are
  gone**, replaced by the command above; `tests/unit/scripts_test.py`,
  which existed only to check their own module-body guard, goes with
  them -- `Node.__init__`'s own `ReimportedMainProcessError` (issue
  #589) is what a re-imported `__main__` meets now, whichever entry
  point built the `Node`.
- **`-blocksdir` is not implemented**: `BlockDB` takes one directory
  and giving it a second is wider than a flag, filed as
  btclib-org/btclib-node#652 rather than carried here. A standing
  reconnect for a dropped `-connect`/`-addnode` peer is filed as
  btclib-org/btclib-node#651 for the same reason: each dials once, at
  startup, and Core keeps redialling either forever.

### An RPC connection stays open across replies (closes #640, closes #642)

- **`RpcConnection.async_send` keeps its socket open across replies
  now, following Core's own per-version keep-alive default**
  (`HTTPRequest::WriteReply`, `src/httpserver.cpp:557-575`, at
  bitcoin/bitcoin@ca7162cde5), rather than closing unconditionally
  after every one: HTTP/1.1 stays open unless the request carried
  `Connection: close`, HTTP/1.0 closes unless it carried an explicit
  `Connection: keep-alive` -- `run`'s own `_wants_keep_alive` is where
  the two are told apart, off the request line's own trailing token,
  which used to be discarded entirely. `bitcoin_core_rpc.
  SessionTransport` pools a connection across calls and probes it
  before reuse, which the unconditional close made race: the probe
  could find the socket not yet readable and reuse it into the narrow
  window between this node's own close and the client's next request,
  surfacing a bare `ConnectionResetError` `SessionTransport`'s own
  docstring already argues it does not retry.
- **A malformed body's own `PARSE_ERROR` reply honours the same
  keep-alive rule as any other reply, and no longer answers it from
  inside `run`'s own call frame.** It used to force a close regardless
  of what the request asked, because the reply was awaited inline
  rather than scheduled the way a dispatched reply's `async_send`
  already is -- honouring keep-alive there would have `run` call back
  into itself, unbounded, for a run of malformed bodies sent one after
  another over one kept-alive connection, eventually hitting Python's
  own recursion limit, caught by `run`'s own blind exception handler
  and closed with nothing logged. Scheduling the reply as its own task
  instead keeps every one of those frames independent, and is what
  makes honouring keep-alive here safe, matching Core's own
  `HTTPReq_JSONRPC` (`src/httprpc.cpp:232-244`, at
  bitcoin/bitcoin@ca7162cde5), which negotiates keep-alive on a parse
  error exactly like any other reply.
- **That same reply now schedules through `self.loop.create_task`
  rather than `run_coroutine_threadsafe`, and its connection is no
  longer popped out of `manager.connections` until it is actually
  done.** `run_coroutine_threadsafe`, called from `run`'s own thread --
  which is already `self.loop`'s own thread, unlike `send`'s cross-thread
  call to the same seam -- only queues the Task's creation for a later
  turn of that loop, leaving it briefly absent from
  `asyncio.all_tasks()`; `RpcManager.stop` reads that set exactly once
  to build what it cancels, and a connection already popped out of
  `manager.connections` besides was unreachable by either of the two
  collections `stop` sweeps, for a turn where nothing had scheduled it
  yet. `create_task` makes the `Task` exist immediately, and leaving the
  connection in `manager.connections` until `async_send` itself pops it
  keeps `stop`'s own socket-closing sweep able to reach it regardless.
- **`REQUEST_TIMEOUT` -- Core's own `DEFAULT_HTTP_SERVER_TIMEOUT`,
  unchanged -- now also bounds the idle wait for a kept-alive
  connection's next request**, matching Core spending that one setting
  on both bounds rather than adding a second constant for the same
  fact.
- **`tests/functional/rpc/connections_test.py`'s own
  `test_one_session_transport_still_opens_a_socket_per_call`, which
  paced its calls to dodge the race above, is rewritten to assert the
  opposite: one `SessionTransport` instance keeps one connection across
  calls, and hundreds of unpaced ones do not reset it.**
- **`tests/functional/rpc/batch_test.py` drives a well-formed,
  multi-member JSON-RPC 2.0 batch through a real socket, through
  `BitcoinCoreRpcClient.call_batch`** -- answer ordering, a mixed
  valid/invalid batch, `handle_rpc`'s own stop-anywhere-in-the-batch
  rule, and a batch sharing its connection with the call after it --
  none of which any test below `tests/unit/rpc/` reached before this.

### A Coin.parse failure on an intact record now answers absent (closes #650)

- **`UtxoIndex.add_block` and `UtxoIndex.get_coin` no longer raise
  `ChainstateInconsistencyError` when a stored `utxo-` record's own
  bytes pass RocksDB's own checksum (btclib-org/btclib-node#641) but
  `Coin.parse` still cannot read them.** `add_block` folds it into the
  same `InvalidBlockInputError` ("prevout not found") a genuinely
  missing prevout already raises; `get_coin` returns `None`. Both
  match `CDBWrapper::Read`/`CCoinsViewDB::GetCoin`'s own "absent" for
  a checksum-clean record that does not deserialize, read at
  `bitcoin/bitcoin@ca7162cde5` -- the case #641 made reachable by
  moving genuine corruption to a separate, earlier guard
  (`StoreCorruptionError`), which is still raised and still not
  mapped onto this class.
- **The cost is accepted, not hidden**: a coin this node wrote itself,
  that a bug in this node's own serializer alone can make unparsable
  on a checksum-clean read, is answered as absent, so a later,
  genuinely valid block spending it is rejected
  (`bad-txns-inputs-missingorspent`, `BLOCK_CONSENSUS`) -- Core pays
  the identical price for the identical reason, argued in
  `ChainstateInconsistencyError`'s own docstring.
- **`ChainstateInconsistencyError`'s and `StoreCorruptionError`'s own
  docstrings drop the two now-false raise sites and the "unsettled"
  framing**, and `CLAUDE.md`'s *Following Bitcoin Core* paragraph is
  rewritten to describe this tree's checksum as landed rather than as
  a gap still open; `RELEASE_NOTES.md`'s own #631 entry now scopes
  its "this node's own fault" answer to what the checksum catches, and
  says beside it that a checksum-clean record `Coin.parse` cannot read
  is answered as absent, with nothing logged.
- **The two tests from #620 and #631 assert the decided behaviour**:
  a real, on-disk truncated `utxo-` record (not a monkeypatch) now
  rejects the candidate block as `InvalidBlockInputError` and answers
  a mempool spend of it `MissingPrevoutError`, in place of the
  `ChainstateInconsistencyError` both used to assert.

### `getblockheader` answers Core's own field names and types (closes #658)

- **`chainwork` is hex, zero-padded to 64 digits**, matching Core's own
  `nChainWork.GetHex()` (`src/rpc/blockchain.cpp:184`, at
  bitcoin/bitcoin@ca7162cde5) rather than the plain JSON number
  `block_index.chainwork[block_hash]` used to answer un-encoded: a
  client written against Core parsing this the way it parses
  `getblockchaininfo`'s own `chainwork` (#575) got a JSON number where
  it expected a string.
- **`versionHex`, `mediantime` join `version`, `nonce`, `bits`,
  `target`, `difficulty`**, each matching Core's own name and type
  (`src/rpc/blockchain.cpp:170-184`, same commit); `merkleroot` and
  `previousblockhash` replace btclib's own `to_dict`'s `merkle_root` and
  `previous_block_hash`, which this callback used to answer verbatim
  under btclib's own snake_case names rather than Core's -- for
  `previous_block_hash` specifically also for a genesis-height block,
  where `to_dict` answered an all-zero hash under that name and Core
  answers no `previousblockhash` member at all. `time` is now the raw
  timestamp `to_dict`'s own ISO 8601 string was standing in for.
  `nTx` stays absent: Core reads it off `CBlockIndex::nTx`, a count
  kept beside the header once the block is received, and `BlockInfo`
  (`chainstate/block_index.py`) carries no such count -- answering it
  here would mean parsing the whole block body off `block_db` for
  every call, a header lookup paying a block's own cost.

### The feefilter gate reads the same IBD flag Core does (closes #661)

- **`DownloadManager._send_due_feefilters` reads
  `node.is_initial_block_download`**, matching Core's own
  `MaybeSendFeefilter` (`net_processing.cpp`, at
  bitcoin/bitcoin@ca7162cde5), in place of `NodeStatus.BlockSynced`: a
  second, looser definition of the one Core concept #575 already built
  a faithful `Node.is_initial_block_download` for. The swap had been
  tried and reverted once before, in #575's own round 2, because the
  suite's regtest fixtures dated every block `GENESIS_TIME`-relative
  (2011), far older than `MAX_TIP_AGE` ever tolerates, so a node that
  had validated its whole known chain still never left IBD and kept
  every peer on the top feefilter bucket forever --
  `tests/functional/p2p/tx_test.py::test_send_tx` was the one test that
  measurably broke under it.
- **`generate_random_chain` (`tests/__init__.py`) takes an optional
  `tip_time`**, reaching `build_block`'s own `time` override for only
  the chain's last block, so a caller whose test needs a recent tip
  gets one without every earlier block losing the `GENESIS_TIME`-relative
  dating BIP34 height and coinbase-maturity arithmetic elsewhere in the
  same chain still depend on. `test_send_tx` is that caller now, both
  of its nodes' chains built with a tip timestamped against the real
  clock, closing the gap #575's round 2 left open.

### A one-member JSON-RPC batch is answered as an array (closes #653)

- **`RpcConnection.async_send` now unwraps a reply to a bare object off
  `self.is_batch`, set by `run` from whether the request it just parsed
  was a JSON array, rather than off `len(response) == 1`.** A lone
  request and a one-member batch both reached `async_send` as a
  `response` list of the same one-element shape, with nothing left at
  that point to tell the two apart, so a batch of exactly one member
  used to be unwrapped the same way a lone request is. Matches Core's
  own `ExecuteHTTPRPC`, which decides singleton against array at parse
  time (`valRequest.isObject()` against `valRequest.isArray()`) and
  answers an array of any size as an array, `UniValue::VARR`, never
  unwrapped for holding only one member (`HTTPReq_JSONRPC`,
  `src/httprpc.cpp:114` and `:135-169`, at bitcoin/bitcoin@ca7162cde5).
  An empty `[]` batch is unchanged, still answered as the single
  `Invalid request` object `tests/functional/rpc/errors_test.py::
  test_an_empty_batch_does_not_end_the_node` already pins -- a separate
  question this tree already answered before this issue and does not
  revisit here.

### A load-contention discriminator for the unpaced-calls test (closes #664)

- **`tests/functional/rpc/connections_test.py::
  test_many_unpaced_calls_over_one_session_transport_do_not_reset`'s own
  docstring now names the shape of a load-caused failure**, matching
  `CLAUDE.md`'s own coverage-floor bullets (issue #372, issue #617):
  measured `uptime` values from two real occurrences, what confirmed
  each as external contention rather than a regression, and what to
  check before trusting a future failure of this one test. The call
  count is unchanged -- shrinking it would trade away the statistical
  confidence issue #640's own report chose 300 for, without removing
  the underlying exposure a documented discriminator already answers.

### testmempoolaccept no longer swallows a store error (closes #668)

- **`test_mempool_accept`'s own blind `except Exception` no longer
  catches a `StoreCorruptionError` raised while reading a prevout.**
  It now catches only `BTClibValueError` and `MissingPrevoutError` --
  `verify_mempool_acceptance`'s own two verdicts on the candidate
  transaction -- and lets anything else propagate, ending the whole
  batch rather than reporting it as that one entry's own
  `"reject-reason"`. This matches Core's own `testmempoolaccept`
  (`src/rpc/mempool.cpp:379-430`, at bitcoin/bitcoin@ca7162cde5),
  whose per-tx loop never catches anything either -- it only ever
  branches on the `TxValidationResult` `ProcessTransaction` returns
  rather than raises -- so a genuine fault reaches `ExecuteCommand`'s
  own uniform catch (`src/rpc/server.cpp:874-887`, same commit)
  instead, this tree's own equivalent being `handle_rpc`
  (`rpc/main.py`), which already logs and answers `INTERNAL_ERROR`
  for exactly this, the same catch `sendrawtransaction` already
  relies on for a store error past its own two excepts.
- **`RELEASE_NOTES.md`'s #631 entry is corrected to say so**: it used
  to tell an operator to read the log for `StoreCorruptionError`
  where `testmempoolaccept` wrote nothing there to find, the gap this
  closes.
- **A rewritten `send_raw_transaction` test in the same file swaps
  its own monkeypatched stand-in from `ChainstateInconsistencyError`
  to `StoreCorruptionError`**, matching what `verify_mempool_acceptance`
  can still actually raise for this node's own storage now that
  #650 stopped `UtxoIndex.get_coin` raising the former.

### `-blocksdir` puts `BlockDB`'s own files on a disk of their own (closes #652)

- **`Config` gains `blocks_dir`, `None` unless a caller names one**,
  chain-suffixed and made absolute the same way `data_dir` already is;
  `BlockDB.__init__` takes it as an optional third argument and falls
  back to `data_dir` when it is not given, Core's own `"default:
  <datadir>"` (`-blocksdir=<dir>`'s own help text). `-blocksdir` naming
  a directory that does not already exist is fatal, `Config`'s own
  refusal, matching Core's "Specified blocks directory ... does not
  exist" rather than creating one silently.
- **`-blocksdir=<dir>` reaches the command line and `bitcoin.conf`**
  (`_RECOGNIZED_KEYS`, not network-only, matching Core's own
  registration): unlike `-datadir`, it never decides which file is
  read, so nothing stops it being set inside that same file.

### A dropped `-connect`/`-addnode` peer is redialled (closes #651)

- **`P2pManager.manage_connections`'s own 0.1s loop gains
  `_maybe_redial_specified`**, one shared, capped, doubling backoff
  (1s floor, 60s cap) covering both `Config.connect` and
  `Config.addnode`, reset to the floor the moment a peer is seen
  connected. Core keeps a whole thread apiece for this --
  `CConnman::ThreadOpenConnections`'s own `-connect` arm and
  `ThreadOpenAddedConnections` -- each with its own cadence; this node
  reuses the one loop it already has for both rather than replicating
  either cadence exactly, argued in `p2p/manager.py`'s own comment
  beside the two new constants.
- **The very first pass never races `Node.run`'s own one-shot dial**
  (issue #573): each peer's own next-eligible time is seeded a full
  backoff floor into the future in `P2pManager.run`, immediately
  before `manage_connections` is scheduled, rather than at
  construction time, when the gap to `start()` is unknown.

### `P2pManager`/`RpcManager` accept off a task, not a reader (issue #430)

- **`server`'s own accept loop no longer calls `loop.add_reader`**,
  which Windows' default Proactor loop does not implement at all,
  raising `NotImplementedError` on every bind. A task awaiting
  `loop.sock_accept` in a retry loop replaces it, on both managers,
  reachable by `stop`'s own `asyncio.all_tasks` sweep the same way the
  reader callback's own shielded task was.
- **That task's own failure is logged rather than left for asyncio's
  "Task exception was never retrieved" warning**, timed to whenever the
  garbage collector reaches it rather than to the failure itself:
  `run` now attaches `_report_server_failure` to `server`'s own
  scheduled task.
- **`Connection._close` no longer raises where the loop it is closing
  has no reader or writer to remove** -- `remove_reader`/`remove_writer`
  share `add_reader`'s own absence on Proactor, so a connection's socket
  went uncleaned on every close attempted there.
- **`install_signal_handlers` no longer registers `SIGTSTP`
  unconditionally** -- the signal does not exist on Windows, and the
  bare `signal.signal` call raised `AttributeError` looking it up before
  ever reaching `signal.signal`.
- **`Connection.run`'s read loop treats a `sock_recv` that raises
  `OSError` -- `ConnectionResetError`, `ConnectionAbortedError` -- the
  same hangup an empty read already was**, rather than letting it
  propagate: `socket.socketpair()`'s own Windows fallback, a real TCP
  loopback pair rather than a kernel-backed one, answers an abrupt local
  close with a hard reset a POSIX pair never sends, matching Core's own
  `SocketHandler` treating every `Recv` failure that is not
  `WOULDBLOCK`/`MSGSIZE`/`EINTR`/`EINPROGRESS` as a disconnect rather
  than a crash.
- **The gate cell and classifier `os-windows.yml`'s own header points
  at stay this issue's rather than landing here**: dispatched runs
  against this branch still fail on real-node functional tests unrelated
  to this listener defect, filed as their own issues rather than guessed
  at here, so this closes none of what #430 asks for closed.

### REPOSITORY.md's required-check PATCH sends the checks array (closes #657)

- **The documented `PATCH` of `main`'s required checks sends the
  `checks` array as a JSON body on stdin, each entry bound to the
  Actions app, and the read-back beside it quotes `.checks[]` with its
  `app_id`** rather than `.contexts[]`. The `contexts` form the section
  carried since btclib-org/btclib-node#264 would have replaced the
  four names bound to the app with the same four bound to nothing, and
  the `.contexts[]` read-back answers identically either way, so the
  record was a command that creates the drift and a check that cannot
  see it (section 11 of the organization standard). The live setting is
  bound today and was not changed: the new read-back's example output
  is its answer at 991b385.
- **The topics example under *Features, and the topics* reads
  `rocksdb` where it read `sqlite`**, the setting having been aligned
  to `pyproject.toml`'s `keywords` when btclib-org/btclib-node#641
  landed (issue #660).

### A MuHash commitment to the UTXO set, and `gettxoutsetinfo` (closes #639)

- **`chainstate/muhash.py` is a `MuHash3072` matching Core's own
  construction bit for bit**: the same 3072-bit safe-prime modulus, the
  same ChaCha20-keyed-by-SHA256 per-element hash, and the same
  numerator/denominator fraction that lets an element be inserted or
  removed in either order without recomputing the rest of the set --
  checked directly against `crypto_tests.cpp`'s own `muhash_tests` and
  RFC 7539/8439's own ChaCha20 vectors
  (`tests/unit/chainstate/muhash_test.py`,
  `tests/_data/README.md`'s own new entry). The arithmetic is native
  Python `int`, in place of Core's own fixed-width limb representation
  -- `CLAUDE.md`'s own Python-native licence for this axis.
- **`CoinStats` bundles that accumulator with `CoinStatsIndex`'s own
  three running counters** -- the output count, the total amount, the
  bogo size -- and `UtxoIndex.add_block`/`apply_rev_block` move all
  four together with every coin they stage, undone by `rollback` the
  same way a staged coin already was: an insert's own undo is a remove
  of the same element, needing no prior accumulator state recorded.
  `KeyValueStore.get_meta`/`put_meta` (`db.py`) are what let it persist
  in the store's own meta column family, inside the same `write_batch`
  as the coins it commits to, so a crash cannot leave the two out of
  step.
- **A provably unspendable output -- a leading `OP_RETURN`, or a script
  over `MAX_SCRIPT_SIZE` -- is never hashed into the commitment**,
  matching `CCoinsViewCache::AddCoin`'s own refusal to add one to
  Core's UTXO set at all. `UtxoIndex`'s own `utxo-` store still keeps
  every output regardless of spendability, a pre-existing divergence
  this branch does not close (issue #667).
- **`gettxoutsetinfo` answers `hash_type: "muhash"` and `"none"`**, with
  Core's own field names and units -- `height`, `bestblock`, `txouts`,
  `bogosize`, `muhash`, `total_amount`. `hash_type: "hash_serialized_3"`
  is refused with Core's own "not a valid hash_type" error, this tree
  answering only the indexed shape and not the from-scratch double-SHA256
  scan Core computes it with; `hash_or_height` is refused the way a
  `bitcoind` running without `-coinstatsindex` already refuses it, since
  this tree has no historical snapshot to answer a past height from
  either. `transactions` and `disk_size` are left out the same way
  Core's own indexed answer already leaves them out, for the identical
  reason: both are an `O(n)` count over the whole set, which an
  incremental accumulator exists specifically to avoid paying on every
  call.

### A unit test no longer binds regtest's own well-known port (closes #678)

- **The `a_manager` fixture's default `port` is `get_random_port()`,
  drawn fresh on every call rather than the fixed `18444`**: a test
  that starts a manager without naming its own port used to bind
  regtest's own well-known port for real, colliding with a second
  suite of this same tree on one machine -- two sessions, or one
  session's coder and reviewer -- and, within one `-n auto` run,
  possibly with a second worker's own manager at the same default.
  `test_connect_and_explicit_listen_binds_and_dials` is the one this
  was measured landing on. `wait_until_listening`'s own error already
  named the port a wait gave up on, so a future collision stays
  legible without a change there.

### A page for running a node, and what a sync costs (closes #577, closes #576)

- **`docs/source/running_a_node.md`** covers the command against each of
  the four chains, `-connect`/`-addnode` for pointing at a peer of one's
  own, reading a sync's progress off `getblockchaininfo`, the fourteen
  RPC methods, and what this node validates against what it does not,
  linking the issues rather than restating them; `README.md` points at
  it, and `index.rst`'s toctree carries it into the docs build.
- **The page also carries this tree's first measurement of an initial
  block download** (#576): a mainnet header sync, block validation over
  two ranges of different density, and the store's own cost curve, each
  dated to the commit it was measured against. The store figure is the
  sqlite3 store this tree used at the time of the measurement, not the
  RocksDB store it ships now (#641); no committed script exists here to
  re-run it, so the page says the RocksDB figure is unmeasured rather
  than guessing at one.

### `tests/interpreters_test.py` compares POSIX paths everywhere (closes #663)

- **`_RUN`'s own keys are `path.relative_to(_ROOT).as_posix()` rather
  than `str(path.relative_to(_ROOT))`.** A `PurePath`'s own `__str__`
  renders with `os.sep` -- backslashes on a `WindowsPath`, `pathlib`'s
  own documentation for `PurePath.__str__` -- while `_NAMES_ONE`
  beside it is written with forward-slash literals, so `str()` there
  compared a Windows path against a POSIX one on `windows-latest` and
  nowhere else, `_NAMING == _NAMES_ONE` false for every entry despite
  naming the same file. `as_posix()` renders with `/` on every
  platform `pathlib` runs on, POSIX included, so this changes nothing
  observable there.
- **`tests/fuzz_corpus_test.py`'s own `_SEED_IDS` carries the same
  `str(path.relative_to(...))` shape but not the same defect**: it
  only ever feeds `pytest.mark.parametrize`'s own `ids=`, a display
  string nothing compares against a literal, so it is left as `str()`.

### `REPOSITORY.md` says which settings it covers (issue btclib-org/.github#551)

- **Its opening claims the settings the standard asks about, in place of
  the whole of what is set outside the tree**: the ones [section 16's
  checklist](https://github.com/btclib-org/.github#16-checklists) sets on
  a new repository, and the ones a section of the standard states a rule
  for, together with whatever a call quoted for one of those answers
  alongside. That is the scope [section 11 of the organization
  standard](https://github.com/btclib-org/.github#11-github-settings)
  fixes, and the claim it rejects is the one no command checks: the
  repository document answers with fields the standard states no rule
  about, and telling those from the settings a repository decides is a
  reading rather than a call.
- **A *What this file passes over* section at the foot is the other edge
  of that scope**, each part of it with the call that reads it back: the
  document fields no section quotes and the standard asks nothing of,
  the facilities that answer empty, and `CLAUDE_CODE_OAUTH_TOKEN`, which
  the organization holds and this repository sets nothing for. Every
  block carries a control that answers non-zero, so a zero beside it is
  an absence rather than a pattern that cannot match.
- **`use_squash_pr_title_as_default` is read back in *Merge methods*,
  beside `squash_merge_commit_title`**: GitHub's own API description
  marks the first as closing down in favour of the second, and section
  11 of the standard states a rule about the squash title, so the older
  spelling of it is not a field the standard is silent about.

### An empty JSON-RPC batch is answered `[]`, matching Core (closes #669)

- **`RpcConnection.is_batch` is now plain `isinstance(body, list)`, with
  no length condition, and `handle_rpc` no longer injects an `Invalid
  request` object for an empty batch.** An empty `[]` used to be
  excepted from `is_batch` on purpose, so #653's own array-shape fix
  left it answered as a single error object -- a literal reading of
  JSON-RPC 2.0 section 6's own wording for a batch that "fails to be
  recognized... as an Array with at least one value." Core's own
  `ExecuteHTTPRPC` does not read it that way: the array branch loops
  zero times over an empty `valRequest`, and its own `HTTP_NO_CONTENT`
  exception is gated on `valRequest.size() > 0`, so an empty input falls
  through unchanged and is answered with the empty array it started as
  (`src/httprpc.cpp:135-185`, at bitcoin/bitcoin@ca7162cde5). This tree
  now matches that.
- **`tests/functional/rpc/errors_test.py::
  test_an_empty_batch_answers_an_empty_array`** (renamed from
  `test_an_empty_batch_does_not_end_the_node`) **and
  `tests/unit/rpc/main_test.py::test_an_empty_batch_answers_an_empty_response`**
  (renamed from `test_an_empty_batch_is_an_invalid_request`) now assert
  Core's own answer.

### `add_block` no longer stores a provably unspendable output (closes #667)

- **A leading `OP_RETURN` or an over-`MAX_SCRIPT_SIZE` script is never
  written under a `utxo-` key at all**, matching `CCoinsViewCache
  ::AddCoin` (`src/coins.cpp:82`, at bitcoin/bitcoin@ca7162cde5), which
  returns without adding such an output to Core's own UTXO set in the
  first place. `_stage_creation` (`chainstate/utxo_index.py`) is the
  one gate both of `add_block`'s own creation loops share, and its
  return value is what keeps `RevBlock.to_remove` from ever naming an
  outpoint this call never wrote: an output never stored is never
  restored, `apply_rev_block` needing no gate of its own for either
  side of the undo.
- **`gettxoutsetinfo`'s `txouts` and `bogosize` are unchanged**: both
  were always `CoinStats`'s own running count (issue #639), gated on
  the identical `is_unspendable` independently of what the `utxo-`
  namespace held, never a scan of that namespace -- this closes the
  gap between what the store persists and what the RPC answers, not
  a gap in the RPC itself.
- **An existing data directory needs nothing.** The entries a
  pre-#667 node already wrote for a provably unspendable output stay
  exactly where they are -- harmless, since nothing ever looks one up
  as a prevout, and never revisited by this change, which only ever
  gates a new write. Core's own answer to the same question is a full
  `-reindex-chainstate` rebuild, not an incremental scrub of the coins
  database in place; this tree implements no such rebuild yet, so
  there is nothing to run and nothing to invent one for.

### A directory as a conf file is refused the same way everywhere (closes #684)

- **`_read_conf_file` (`cli.py`) checks `path.is_dir()` before opening
  the file**, rather than catching the `OSError` subclass an open
  attempt raises. Opening a directory raises `IsADirectoryError`
  (`errno.EISDIR`) on POSIX and `PermissionError` (`errno.EACCES`) on
  Windows, so a handler written for one platform's exception class
  never catches the other's, and `test_read_conf_file_a_directory_raises`
  failed on `windows-latest` with the wrong exception propagating
  uncaught. The `is_dir()` check matches `ReadConfigFiles`'s own
  `fs::is_directory` guard, which likewise runs before its stream is
  ever opened.

### `-datadir` naming a non-directory is refused, not a crash (closes #693)

- **`build_config` (`cli.py`) checks an explicit `-datadir` with a new
  `_check_datadir` before it is ever joined into `conf_path`**, refusing
  it with a `ValueError` where the path does not already exist as a
  directory -- missing, or blocked by a file at the path itself or at
  any parent component of it -- the same `fs::is_directory` test and
  the same "does not exist" wording Core's own `CheckDataDirOption`
  (`src/common/args.cpp:891`, at bitcoin/bitcoin@ca7162cde5) uses,
  fatal there too. Before this, a datadir a file blocks reached
  `_read_conf_file`'s `path.read_text()` unchecked and raised
  `NotADirectoryError`, uncaught, rather than the `ValueError` every
  other configuration problem in this module raises. The default
  (unset `-datadir`) path is not checked at all and keeps the lazy
  creation `Node.__init__`'s own `mkdir(exist_ok=True, parents=True)`
  already gives it, matching Core's own default path, which is never
  checked either and is created the same way, lazily, by
  `GetBlocksDirPath`'s `fs::create_directories`.

### Windows: dial reaches IPv6, notices a refusal (closes #681, closes #682)

- **`dial`'s own peer tuple is four elements for an `AF_INET6` target,
  not two.** Windows' Proactor loop hands a bare `(host, port)` straight
  to `ConnectEx`, and CPython's `Modules/overlapped.c` (`parse_address`,
  read at python/cpython@v3.14.0) dispatches on the tuple's length
  rather than on the socket's own family: a two-tuple is always parsed
  as `AF_INET`, so `WSAStringToAddressW` reads `"::1"` as an IPv4
  dotted quad and answers `WSAEINVAL` before `ConnectEx` is ever
  reached. The four-tuple form, `(host, port, 0, 0)`, names the family
  explicitly and is accepted on every platform this node runs on.
- **`_DIAL_TIMEOUT` is 5.0 seconds, Core's own `DEFAULT_CONNECT_TIMEOUT`
  (`src/netbase.h` at bitcoin/bitcoin@ca7162cde5), not the 1.0 the old
  poll loop's own ten-passes-at-0.1s budget left behind.** That figure
  was never checked against Core's own default: it was too tight for
  what Windows' Proactor loop needs to notice a refused loopback
  connect, measured at a little over two seconds on a Windows run and
  well inside Core's own five-second budget.
- **`tests.local_addr` names `127.0.0.1`, not `0.0.0.0`.** A POSIX
  kernel remaps a client connect to `0.0.0.0` onto loopback; Windows'
  `ConnectEx` refuses it outright, synchronously, which is what left
  every two-node functional test dialling through this helper waiting
  out its own 60-second timeout for a connection Windows never made.

### A parametrize id fits in an environment variable (closes #701)

- **`test_is_unspendable_matches_core`'s two largest vectors get an
  explicit, short `ids=` rather than pytest's own default.** A `bytes`
  parameter with no explicit id is escaped byte for byte, so the
  10,000/10,001-byte all-zero vectors turned into a node id of roughly
  40,000 characters; `PYTEST_CURRENT_TEST`, set to that id plus
  `" (setup)"`/`" (call)"`/`" (teardown)"` through `os.environ[...] =`,
  goes through `_wputenv_s`/`SetEnvironmentVariableW` on Windows, which
  refuses a value past 32767 characters -- `ValueError: the environment
  variable is longer than 32767 characters`, at both setup and teardown,
  on every Windows run of btclib-org/btclib-node#681/#682's own branch.
  POSIX has no such limit, which is why the suite was green everywhere
  else. The six new ids name the vector by what it is rather than by its
  bytes, matching the inline comments already beside each one.
- **No other `parametrize` in the tree carries this risk.** Every one
  without an explicit `ids=` parametrizes on a short, bounded string or
  a small int -- the longest id the tree collects without one is
  `test_is_bip30_unspendable`'s, 141 characters, its `"00" * 32` being a
  64-character string rather than bytes escaped four to one -- and every
  one whose values are `bytes` or read from a corpus already carries its
  own `ids=`.

### REPOSITORY.md's alert paragraph answers what is true today (closes #680)

- **The *Security and analysis* section's alert calls now print the
  answer they read back, and the paragraph under them says what is
  true of it.** The second call's default listing carries every alert
  state, not only `open`, and every alert on this tree today answers
  `dismissed` or `fixed`, none `open`. By manifest it is 26
  `poetry.lock` (a file this tree no longer has) and 1 `uv.lock`, this
  tree's own; the `uv.lock` alert is `pytest`, medium severity,
  already `fixed` at `9.0.3`. The paragraph used to point its
  remaining step at the closed issue #37; it now defers to no issue,
  stating the split outright and naming a future open `uv.lock` alert
  as its own finding rather than a step recorded here.

### Two more of the parsers a peer reaches unauthenticated are fuzzed (issue #516)

- **`p2p.connection.frame_message`/`frame_message_bytes`** pull
  `Connection.parse_messages`'s own per-message framing -- the header
  peek, `Message.parse`, and the network-magic check right after it --
  into a function of a stream (or, for the second, bare octets) rather
  than an inline loop body, the same shape Core's own
  `V1Transport::ReceivedBytes` takes in its own
  `p2p_transport_serialization.cpp` fuzz target
  (at bitcoin/bitcoin@ca7162cde5): a transport built with no socket,
  fed raw bytes directly. `parse_messages` itself now calls
  `frame_message` rather than repeating its body; nothing about what it
  parses, queues or rewinds on a partial message changed.
- **`rpc.connection.parse_request_head`** pulls `RpcConnection.run`'s
  own header-section framing -- the request line, the header fields,
  the `Content-Length` bound and the keep-alive decision -- into the
  same shape, scoped the way Core's own `http_request.cpp` fuzz target
  is scoped: control-line and header framing only, never the JSON-RPC
  body decoding stdlib's own `json.loads` already does. Two new
  exceptions, `IncompleteRequestHeadError` and
  `MalformedRequestHeadError` (`btclib_node.exceptions`), are what it
  raises for octets a live connection's own `_recv_until` would never
  hand it but a fuzzer can -- both `BTClibException` subclasses, so
  `run`'s own bare `except Exception` catches them exactly as it caught
  the `ConnectionError` this replaces.
- **`fuzz/fuzz_framing.py` and `fuzz/fuzz_rpc_head.py`** are the two new
  atheris harnesses over those entry points, each with a seed corpus
  under `fuzz/corpus/`; `fuzz.yml` now runs all three harnesses in one
  job, `-max_total_time` split three ways rather than tripled so the
  job's own 30-minute ceiling does not grow with the harness count.
  `.pre-commit-config.yaml` excludes `fuzz/corpus/` from
  `trailing-whitespace`, `end-of-file-fixer` and `mixed-line-ending`:
  measured against `fuzz_rpc_head`'s own seeds, whose valid HTTP
  request heads are printable ASCII ending in `\r\n\r\n`,
  `end-of-file-fixer` dropped one of the required pair and
  `mixed-line-ending`'s own `--fix=lf` rewrote every `\r\n` to `\n`,
  each silently turning a valid seed into one `parse_request_head`
  itself would never produce or accept.
- **The p2p callbacks (`p2p/callbacks.py`) are not fuzzed.** Core's own
  matching target for that layer, `process_message.cpp`, builds a
  whole `TestingSetup` rather than feeding a bare object octets, and
  this tree's own handlers likewise need a `Node`, a `P2pManager` and a
  `Connection` rather than a buffer -- a materially different harness
  shape, not a small refactor away from the other two. Left open,
  tracked forward as issue #698.

### `test_download` no longer copies a running node's `LOCK` files (closes #683)

- **`tests/functional/p2p/download_test.py::test_download` copies its
  bootstrap node's directory with `shutil.ignore_patterns("LOCK")`**,
  seeding the other nine download peers from everything but the three
  `LOCK` files RocksDB keeps open for as long as a store is
  (`src/btclib_node/db.py`) -- the bootstrap node stays running
  throughout the copy, as a peer the rest of the test connects to.
  Windows refuses to copy a file another handle still holds where
  POSIX does not, so `shutil.copytree` failed on `windows-latest` with
  `WinError 32` on those three files. Excluding them costs nothing a
  fresh peer needs: RocksDB re-creates a store's `LOCK` file on every
  `Rdict` open regardless of what, if anything, was already at that
  path, measured directly by opening a `Node` against a directory
  copied this way.

### db.py records that a closed store's LOCK releases synchronously (issue #703)

- **`src/btclib_node/db.py`'s lock section says what `close()` does to
  the directory `LOCK`, source to source, and what one Windows run
  measured.** [ISS 703](https://github.com/btclib-org/btclib-node/issues/703)
  was filed on one `windows-latest` run of a branch of #683 in which
  the three `LOCK` files were still held immediately after every
  store's own `close()` had returned. Read at the versions `uv.lock`
  pins -- `rocksdict` 0.3.29, its `rust-rocksdb` fork at
  Congyuwang/rust-rocksdb@4cf3c68a993b807bc54ff1c5293cdf49e62aaf72,
  RocksDB at facebook/rocksdb@44e95d8af5d7ec503b3f1d5754c3379ab6c29a9d
  -- every link from `Rdict.close()` to `::CloseHandle` on the lock is a
  synchronous call on the calling thread: `DbReferenceHolder::close`
  drops the last `Arc` clone, `DBCommon::drop` reaches
  `ffi::rocksdb_close`, `DBImpl::CloseHelper` waits out background
  compaction and flush before `env_->UnlockFile`, and
  `WinFileSystem::UnlockFile` is a bare `delete` into
  `WinFileLock::~WinFileLock`. Measured once on `windows-latest` with a
  bounded, timed retry around a reopen straight after `close()` (run
  33274969391): reopened at the first attempt, in 0.091 s. Nothing here
  is deferred to a background thread or to the garbage collector, so
  the one held lock reads as contention on a loaded runner rather than
  a defect this tree or `rocksdict` owes a fix for; the issue is closed
  on that measurement and the section records it, so the next reader
  of a `WinError 32` on a `LOCK` file starts from the trace rather than
  from the guess.

### `test_download`'s copies now hold the whole chain (closes #710)

- **`test_download` flushes `bootstrap_node`'s chainstate before
  copying its directory.** `_finalize_fork` only calls
  `Chainstate.flush` once `UtxoIndex.should_flush` says the staged
  UTXO cache has grown past its own bound (`src/btclib_node/main.py`),
  a bound this test's own chain of coinbase-only blocks never
  reaches: `BlockIndex.stage_status` and `FilterIndex`'s own `pending`
  held everything in Python state, never written to the store, so the
  nine copies taken from `bootstrap_node`'s directory opened on
  essentially empty stores -- genesis alone -- while the test still
  passed, `bootstrap_node` itself answering every request as the same
  live Python object regardless of what had reached disk. Each copy
  now asserts its own chain length right after it opens, before it
  ever talks to a peer, rather than relying on the final assertion
  against `main_node`, which `bootstrap_node` alone already satisfies.

### `handle_rpc` no longer drops a kept-alive reply (closes #688, closes #685)

- **`rpc.main.handle_rpc` no longer pops `RpcManager.connections`'s own
  entry for the connection it just answered.** It used to, right after
  scheduling the reply through `RpcConnection.send`, unconditionally,
  on `Node`'s own thread -- racing `RpcConnection.async_send`, on
  `RpcManager`'s own thread, writing that same reply, re-arming the
  connection for its next kept-alive request and reading that next
  request whole, all without ever suspending in between. Where
  `async_send` won that race, `handle_rpc`'s own pop removed the entry
  `async_send` had just put back for the request already queued behind
  it, so `rpc.main.get_connection` found nothing for it and the request
  was silently dropped, answering nobody: the client blocked for its
  own full per-call timeout with no reply, no close and no reset ever
  arriving, and nothing on the server's own side to say why.
  `RpcConnection.async_send` is now the sole owner of this dict's
  membership, on the branch that already ran there for a reply that
  closes.
- **This is what `tests/functional/rpc/connections_test.py::
  test_many_unpaced_calls_over_one_session_transport_do_not_reset` was
  failing standalone on, at a load issue #664 had read as ordinary
  contention** -- that docstring is corrected to say so.

### A 2 s wait's own bound survives a kernel tick (closes #704)

- **`test_send_and_wait_gives_up_rather_than_blocking_forever` asserts
  `waited >= 2 - _WINDOWS_TIMER_TICK`, not `waited >= 2`**, after two
  Windows runs failed at `1.9998... >= 2`, a few hundred microseconds
  short. `send_and_wait`'s `future.result(timeout=2)` reaches
  `_thread.lock.acquire(True, 2)`, which CPython 3.14 implements as
  `lock_PyThread_acquire_lock` (`Modules/_threadmodule.c:814-833`)
  calling `_PyMutex_LockTimed` (`Python/lock.c:53`, read at
  python/cpython@v3.14.0) -- not the legacy `PyThread_acquire_lock_timed`
  in `Python/thread_nt.h` that an earlier version of this fix cited,
  which `_thread.Lock` no longer reaches in 3.14. `_PyMutex_LockTimed`'s
  own deadline reads `PyTime_MonotonicRaw`
  (`lock.c:67,148`; on Windows `QueryPerformanceCounter`,
  `Python/pytime.c:1065-1090`), the same clock `time.monotonic()`
  itself reads (`PyTime_Monotonic`, `pytime.c:1223-1225`) -- so the
  lock's own deadline and the test's own measurement are not two
  different clocks, contrary to that earlier fix's own claim. On
  Windows the wait parks through `_PyParkingLot_Park` into
  `_PySemaphore_PlatformWait`, which calls `WaitForMultipleObjects`
  (`Python/parking_lot.c:95-133`, the call at `:130`) with a
  millisecond count from `_PyTime_AsMilliseconds(timeout,
  _PyTime_ROUND_TIMEOUT)` (`parking_lot.c:105`) -- not
  `WaitForSingleObject`. The real gap is one level deeper: between that
  millisecond count and the interrupt-time tick clock the OS kernel
  actually satisfies the wait against, which Microsoft's own Remarks
  for `Sleep` describe for the same tick-based wait timer
  (`WaitForMultipleObjects` documents no accuracy of its own): "The
  system clock 'ticks' at a constant rate... If dwMilliseconds is
  greater than one tick but less than two, the wait can be anywhere
  between one and two ticks, and so on."
  (<https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-sleep>).
  A 2000ms wait can therefore be satisfied up to one tick early on the
  QPC-backed clock `time.monotonic()` reads. `_WINDOWS_TIMER_TICK` is
  set to 15.625ms, the documented default tick -- generous next to the
  two measured shortfalls and small next to the two-second bound
  itself. A review round caught the first version of this fix citing a
  Windows call path (`Python/thread_nt.h`'s `WaitForSingleObject`) that
  is real code at that sha but not the one `_thread.Lock` reaches in
  3.14, and a "two clocks" story the source does not support -- fixed
  here to the call path and deadline arithmetic actually traced.

### `RpcConnection.close` no longer cancels a stale handle (closes #714)

- **`RpcConnection` no longer carries a `task` attribute, and `close`
  no longer tries to cancel one.** It was set once, at accept, and
  never again -- every request on a kept-alive connection after the
  first is read by a fresh call to `run` from inside `async_send`,
  which never touched it -- so past a connection's first request
  `close`'s own `self.task.cancel()` named a long-finished `Future`
  and cancelled nothing. `RpcManager.stop`'s own
  `asyncio.all_tasks(self.loop)` sweep is what actually cancels
  whatever is live for a connection, run before `close` is ever
  called; `close` now only closes the socket, matching Core's own
  `HTTPRemoteClient`, which carries no handle onto whatever worker
  thread might be answering a request for it either --
  `HTTPServer::ClearConnectedClients` (`src/httpserver.cpp:1160-1167`, at
  bitcoin/bitcoin@ca7162cde5) drops what is left the same
  unconditional way, once its own socket-handling thread has already
  been joined.

### The p2p callbacks are fuzzed through a node (closes #698, closes #516)

- **`fuzz/fuzz_process_message.py`** is the third parser #516 left open:
  one atheris harness driving `p2p.callbacks`' every handler --
  `handshake_callbacks` through `p2p.main.handle_p2p_handshake`,
  `callbacks` through `handle_p2p` -- the way `Node`'s own loop does,
  over one regtest `Node` built once and reused across every call,
  cheaper by orders of magnitude than rebuilding one per call. Core's
  own `process_message.cpp` differs from this rather than being the
  same shape at less cost: it discards and rebuilds its own `connman`,
  `banman`, `addrman` and `peerman` fresh on every single pass, for a
  sanitizer reason -- catching a dangling pointer -- this harness's own
  Python objects have no counterpart to, and resets only `chainman` and
  `mempool` conditionally, on a pass that leaves either dirty. This
  harness's own `Node` is reused whole, with nothing inside it rebuilt
  or reset either way. `dispatch`'s own docstring is where that
  comparison, and what it costs to leave `peer_db` unreset across
  calls, measured the same way, are argued.
- **The command a message dispatches under is picked by index from the
  fixed set `p2p.callbacks` actually dispatches, not read from whatever
  twelve octets the fuzz input itself spells** -- Core's own
  `LIMIT_TO_MESSAGE_TYPE` exists for the same reason, an environment
  variable this tree's own script-per-harness `fuzz.yml` has no
  per-target corpus to key one on, so the harness takes the same
  outcome structurally instead. A header is built from the command and
  payload through `btclib.p2p.message.Message`, so only the command and
  the payload are ever fuzzed and the header is always well-formed --
  `fuzz_framing.py` already fuzzes the header on its own.
- **`p2p.main.handle_p2p`/`handle_p2p_handshake` themselves catch
  anything a callback raises**, discouraging the peer only for a
  `BTClibException` and otherwise only logging, so a harness that
  merely called them and trusted what escaped would never see a
  callback's own bug -- nor tell a callback's own refusal of the input
  apart from genuine acceptance, both landing on `dispatch`'s own
  `None` return otherwise. `_CrashCapture`, a `logging.Handler` attached
  directly to `node.logger` -- the one way this tree's own logger can be
  observed, not being reached through `logging.getLogger()` -- reads
  `record.exc_info` off every such log call and sorts it the way
  `handle_p2p` itself already does, into `escaped` for a bug and
  `refused` for a `BTClibException`; `dispatch` re-raises `escaped`
  first and `refused` after, so a crash, a refusal and an acceptance are
  three outcomes a seed test can tell apart rather than two.
- **`_built_node`'s `tempfile.mkdtemp()` directory is removed with
  `atexit.register(shutil.rmtree, ...)`**, registered the moment it is
  built: nothing else ever closed or removed it, and
  `tests/fuzz_corpus_test.py` collects into the ordinary suite, so every
  `pytest` worker process that ever imported this module was leaving one
  behind under the real temp directory, forever, on every run.
- **`fuzz/corpus/fuzz_process_message/`** holds one seed per command
  `p2p.callbacks` dispatches, each built from the same fixtures
  `tests/unit/p2p/callbacks_test.py` uses. `tests/fuzz_corpus_test.py`
  is extended to exercise them: `_resolve` loads this one harness by
  path rather than through the installed package, since it needs no
  atheris to run and is not reachable through it either, and `_REFUSED`
  replaces `None` as what a refused seed is marked with, since
  `dispatch`'s own successful return is `None` and reusing it for
  "refused" would count every accepted seed of this harness as refused
  instead.
- **`fuzz.yml`'s own `-max_total_time` is now split four ways** rather
  than three, unchanged in total.

### An unsolicited block with an unknown header is not a KeyError (closes #711)

- **`p2p/callbacks.py`'s `block` indexes an unrecognised header through
  `BlockIndex.add_headers` before looking up the block's info**, rather
  than reading `block_index.header_dict` on a hash nothing had entered
  there yet. Matching Core's own `NetMsgType::BLOCK` path
  (`net_processing.cpp`, at bitcoin/bitcoin@ca7162cde5): every block
  runs through `AcceptBlockHeader` (`validation.cpp`, same sha) first,
  which accepts a header whose parent is already known and refuses one
  whose parent is not with `BLOCK_MISSING_PREV`; `MaybePunishNodeForBlock`
  then calls `Misbehaving` for that refusal, unlike an unconnecting
  `headers` batch, which this file already answers by asking for more
  rather than by discouraging the peer (#233). A block extending a
  known parent but never separately indexed by a prior `headers` round
  is now accepted and stored instead of raising `KeyError`; a block
  naming a parent this node has never heard of now raises
  `BTClibValueError`, which `p2p/main.py`'s `handle_p2p` already
  discourages and drops the peer for -- where the pre-fix `KeyError`
  was neither.

### `Config(pruned=True)` builds a node that actually prunes (closes #601)

- **`BlockDB.prune_up_to` deletes a block and its reverse patch once
  more than `MIN_BLOCKS_TO_KEEP` (`constants.py`, Core's own 288, two
  days) blocks behind the tip**, called from `main._prune_chain` after
  every fork `update_chain` connects, which also clears
  `BlockInfo.downloaded` for the same range, matching Core's own
  `BlockManager::PruneOneBlockFile` clearing `BLOCK_HAVE_DATA`/
  `BLOCK_HAVE_UNDO` on the index entry it prunes -- genesis stays
  `downloaded=True`, since it is never written to `block_db` in the
  first place. `BlockIndex.set_downloaded` now checks `pending` before
  writing through, the same shape `set_status` already used
  (`btclib-org/btclib-node#586`): a fork longer than the retained depth
  connects in one `update_chain` trial, staging every hash it adds into
  `pending` before that trial's own flush, which a write-through clear
  reaching one of them would have lost to the next `finalize`. Deletion
  is by height rather than by file, this store's
  own rotation tracking append order rather than a height range the way
  Core's `FlatFilePos` does; a `.blk`/`.rev` file is unlinked once every
  block it ever held has been pruned this way, `BlockDB.live` counting
  down to that point.
- **`send_version` drops `NODE_NETWORK` and keeps `NODE_NETWORK_LIMITED`
  once `Config.pruned` is true**, matching Core's own `g_local_services`
  gate on `!fPruneMode`.
- **A `getdata` for a block below a pruned node's own retained depth
  disconnects the peer** rather than answering silently, matching
  Core's own `ProcessGetBlockData` -- fired on height alone, not on
  whether the block happens to still be on disk. `getrawtransaction`'s
  block-hash lookup answers `"Block not available (pruned data)"` or
  `"Block not available (not fully downloaded)"`, Core's own
  `CheckBlockDataAvailability` distinction, in place of the one
  undifferentiated message both cases used to share.
  `getblockchaininfo` answers `pruneheight` once `pruned` is true.
- **`Config.__init__` no longer refuses `pruned=True`**, and
  `PruningNotImplementedError` is gone along with the refusal. `-prune`
  on the command line keeps Core's own spelling but not its `<n>` MiB
  target: any nonzero value turns pruning on at the fixed retained
  depth above, the size-target axis being this issue's own deferred
  remainder. A negative `-prune` refuses to start, Core's own wording
  from `node::ApplyArgsManOptions`, rather than silently pruning.

### `windows-latest` gates the suite, and the package claims it (closes #430)

- **`test.yml` carries a `windows` job, `windows-latest` at `3.14`
  alone** -- not crossed with `3.14t`, the same axis the interpreter
  cells argue -- gated on the suite passing rather than on the
  coverage floor the `ubuntu-latest` cells still carry: the floor is a
  claim about one interpreter on one image, and this cell answers a
  different question, whether the suite runs at all on a platform
  nothing in the gate ran before.
- **`pyproject.toml` now carries `Operating System :: Microsoft ::
  Windows`** beside `POSIX` and `MacOS`, checked by the job above.
- **`os-windows.yml` is gone.** Its runs priced the job above before
  it existed; the job now runs what the sentinel ran, so the sentinel
  is removed rather than kept beside a gate cell duplicating it.
- **`REPOSITORY.md`'s concurrent-job-ceiling section names the new
  job** and records that `test.yml`'s required check, being its own
  aggregate job rather than any cell by name, needs no ruleset change
  to cover it.

### A `NODE_NETWORK_LIMITED` peer is skipped for an old block (closes #706)

- **`_request_new_block_work` skips a candidate for a connection whose
  `version_message.services` carry `NODE_NETWORK_LIMITED` without
  `NODE_NETWORK`, once it falls more than `MIN_BLOCKS_TO_KEEP - 2` behind
  that connection's own `best_known_height`**, matching Core's own
  `FindNextBlocks` (`net_processing.cpp:1635`, at
  bitcoin/bitcoin@ca7162cde5); a candidate still within reach is offered
  to such a peer exactly as any other connection's is.
- **`Connection.best_known_height` is this tree's own stand-in for Core's
  chainwork-ranked `pindexBestKnownBlock`**: `callbacks.version` sets it
  from the peer's own `version.start_height`, and `callbacks.headers`
  raises it off every batch that peer itself sends, this tree keeping no
  per-peer chainwork-ranked index of its own for `download.py` to read
  instead.

### The free-threaded cell reports instead of gating (closes #723)

- **`test.yml`'s `free-threaded` job runs `3.14t` beside `coverage`'s
  `3.14` but is out of `test-passed`'s own `needs:`** -- `rocksdict`
  has never published a `cp314t` wheel or an sdist on any release, so
  the job cannot install this tree's own store and would otherwise
  block every merge on a wheel no branch here can ship. Building
  `rocksdict` from its own repository in CI was considered and
  rejected: it is a Rust crate compiling RocksDB's own C++ source, and
  that cost is not owed to a job whose entire purpose is a report
  nobody is blocked on.
- **`REPOSITORY.md`'s concurrent-job-ceiling section names the row's
  new place**, still costing the same slot at the ceiling it always
  did, no longer gated.

### `-prune=<n>` has a MiB target and a `pruneblockchain` RPC (closes #705)

- **`-prune=<n>` for `n >= MIN_PRUNE_TARGET_MIB` (550) prunes
  automatically to roughly `<n>` MiB on disk**, `Config.prune_target_mib`
  carrying the target and `BlockDB.current_usage` summing every
  still-tracked `.blk`/`.rev` file's own size, this store's counterpart
  of Core's `BlockManager::CalculateCurrentUsage`. `main._prune_to_target`
  walks height by height rather than Core's own file-by-file
  `FindFilesToPrune`, this store's own rotation tracking append order
  rather than a file's height range, and stops as soon as usage is back
  under target, `MIN_BLOCKS_TO_KEEP` behind the tip never crossed either
  way. `getblockchaininfo` answers `size_on_disk`, `automatic_pruning`
  and `prune_target_size` accordingly.
- **`-prune=1` is manual pruning: nothing is deleted on its own**,
  matching Core's own `PRUNE_TARGET_MANUAL`. The new `pruneblockchain`
  RPC deletes block and undo data up to a height or a timestamp, refuses
  a chain shorter than `Chain.prune_after_height` -- a new field on
  `chains.py`'s four leaves, matching Core's own per-chain
  `nPruneAfterHeight` (100000 mainnet, 1000 elsewhere) rather than this
  tree's uniform `MIN_BLOCKS_TO_KEEP` -- clamps a height closer than
  `MIN_BLOCKS_TO_KEEP` to the tip rather than refusing it, and answers
  the height of the last block actually pruned.
- **`-prune=<n>` for `2 <= n < MIN_PRUNE_TARGET_MIB` now refuses to
  start**, Core's own wording: too small a target to actually run a
  node on, rather than the fixed 288-block depth every such value used
  to collapse to.

### `send_version` carries this node's own tip height (closes #722)

- **`send_version`'s own `version` message carries `Node.best_height` as
  `start_height`, not the literal `0` every connection sent regardless
  of how synced this node actually was**, matching Core's own
  `PushNodeVersion` (`net_processing.cpp:1673`, at
  bitcoin/bitcoin@ca7162cde5), which fills the same field from
  `m_best_height`.
- **`Node.best_height` is a plain `int`, not a lock-guarded field.**
  `main._finalize_fork` writes it on `Node`'s own thread, once per tip
  change, and `Connection.send_version` reads it cross-thread, on
  `P2pManager`'s own asyncio loop -- the same freedom from a torn read
  Core's own `std::atomic<int> m_best_height` (`net_processing.cpp:873`)
  gives `PushNodeVersion` without `cs_main`, CPython's GIL making a
  single attribute's read and write each one uninterruptible step.

### A peer that cannot serve blocks gets no block work (closes #725)

- **`_request_new_block_work` now skips a connection outright when
  neither `NODE_NETWORK` nor `NODE_NETWORK_LIMITED` is in its
  `version_message.services`**, matching Core's own `CanServeBlocks`
  gate (`net_processing.cpp:1254`, at bitcoin/bitcoin@ca7162cde5),
  applied at the same point Core applies it, the call site of
  `FindNextBlocksToDownload` (`net_processing.cpp:6495`). Such a peer
  used to be offered the whole download window, as though it were
  archival, because `_is_limited_peer` alone answers only "which
  blocks", not "any at all" -- the two questions Core keeps as two
  separate gates.
- **`callbacks.version`'s own disconnect for a peer missing
  `NODE_NETWORK` now runs only for an outbound connection, and now
  accepts `NODE_NETWORK_LIMITED` alone once this node is close to the
  tip**, matching Core's own `ExpectServicesFromConn`
  (`net.h:847-856`, false for `INBOUND`, `MANUAL` and `FEELER`
  connections) and `GetDesirableServiceFlags`
  (`net_processing.cpp:1861-1869`, `NODE_NETWORK_LIMITED | NODE_WITNESS`
  desirable once this node's own tip is within 144 blocks of its best
  header). This used to test `NODE_NETWORK` alone on every connection,
  inbound included, and discouraged and dropped a
  `NODE_NETWORK_LIMITED`-only peer this tree's own new
  `_can_serve_blocks` gate above (and Core's) both consider
  block-capable -- exactly once this node was closest to the tip,
  the condition under which Core is most willing to keep such a peer.
  `node.status >= NodeStatus.BlockSynced` stands in for "close to the
  tip", this tree computing no counterpart to Core's own
  `ApproximateBestBlockDepth()`.

### The release pull request opens the next cycle (issue btclib-org/.github#528)

- **`RELEASING.md`'s release pull request retitles the `## Unreleased`
  sections of `CHANGELOG.md` and `RELEASE_NOTES.md` and opens the next
  cycle's above them, in the one pull request.** Opened in a pull
  request of their own after the release's, the sections were missing
  from `main` for as long as that pull request took to land, and a
  branch landing in between filed its entry under a release it is not
  in, with nothing to say so. The version bump stays in the pull request
  that opens the cycle, `version-check` comparing the tag against what
  `pyproject.toml` declares. Section 12 of btclib-org/.github's
  `README.md` is the decision; this is its port.

### The verbatim files are the standard's copies, byte for byte

- **`.gitattributes` states the union price as section 9 of the
  standard does** (issue btclib-org/.github#423): the driver is a
  checkout's and the forge does not apply it, so a pull request whose
  `CHANGELOG.md` or `RELEASE_NOTES.md` overlaps its base is reported
  `CONFLICTING` however cleanly the pair merges locally, and a rebase on
  a checkout is what clears it.
- **`.markdownlint.jsonc` points at section 14 of the standard for who
  carries it** (issue btclib-org/.github#316), in place of an
  enumeration of trees.
- **`CONTRIBUTING.md`'s shared half is btclib-org/.github's** (issue
  btclib-org/.github#281): the half is replaced whole rather than each
  change applied by hand, a hand-written list of them being what comes
  up short. Among them, *The landing queue* points at `REPOSITORY.md`'s
  *Plan-gated settings* for the ceiling's figure (issue
  btclib-org/.github#412).

### The suite shuffles after one marker (closes btclib-org/.github#428) (issue btclib-org/.github#426)

- **`pytest-randomly` is in the `harness` group**, section 7's default.
  `pytest.mark.order` sits on one test and holds a place at the head of
  the queue rather than a sequence, and the two plugins' own hook order
  keeps that place under the shuffle; `tests/README.md` says so.
  `test_download`'s docstring called it the slowest test in the suite,
  which `pyproject.toml`'s own `timeout` measurement refutes, and now
  says among the slowest.
- **`tests/README.md` declares the property layer absent.** Section 7
  keys it on the property `fuzz.yml` claims for this tree, and #742 is
  the gap.

### `REPOSITORY.md`'s perimeter is section 11's (issue btclib-org/.github#550)

- **The scope statement carries section 11's three limbs in the
  standard's own words** (issue btclib-org/.github#582). The third --
  the settings a behaviour the standard describes rests on -- is what
  keeps `.visibility` under *Features, and the topics* now that the wiki
  and the projects board have left it.
- **The opening no longer claims that nothing below is recoverable by
  reading the repository** (issue btclib-org/.github#571). The topics
  and `.homepage` have a copy in `pyproject.toml`, which *Features, and
  the topics* and *Read the Docs* already said, so the clause names
  those two and holds of the rest; this tree is where that contradiction
  was found.
- **`has_wiki` and `has_projects` leave the file** (issue
  btclib-org/.github#550). Section 11 puts both outside the perimeter,
  so the sentence calling each this repository's own answer goes, and
  the measurement that the wiki is empty goes with it; the foot names
  the pair as outside.
- **The `scorecard` sentinel's bar is why the visibility is read back**
  (issue btclib-org/.github#584), section 10 asking a repository that is
  public and nothing else. `.fork` is read back nowhere, nothing setting
  it.
- **The ceiling's section takes the heading section 10 names as its one
  home, *Plan-gated settings*, with GitHub's table beside the plan
  command** (issue btclib-org/.github#569, issue btclib-org/.github#412).
  `CONTRIBUTING.md`'s shared half already pointed there;
  `claude-review.yml`'s and `codeql.yml`'s headers pointed at the old
  name and point at the new one now. Secret scanning's two plan-gated
  settings stay where the call that answers them is.
- **The Dependabot secret store leaves the facilities nobody reached
  for** (issue btclib-org/.github#572). It is empty because the standard
  holds that token at the organization, in both stores, which is a
  decision rather than an absence, and the bullet that already said so
  for the Actions store reads both back now.

### The `Free Threading` classifier goes (closes #745) (issue btclib-org/.github#577)

- **`pyproject.toml` declares no `Free Threading` classifier.** Section
  3 of the organization standard declares one where the merge gate
  exercises the free-threaded build, a gate being what refuses the
  landing that breaks it, and `test.yml`'s `free-threaded` job sits
  outside `test-passed`'s `needs:`. It runs no suite either:
  `rocksdict` publishes no `cp314t` wheel and no sdist, so that job's
  sync step fails, a `::notice::` reports the miss and the suite step
  behind it is skipped (#746). The classifier would promise a consumer
  of this package a build nothing here runs. Where that job gates is
  not reopened by this -- #723 and #747 are where it was decided -- and
  the declaration is the half that moves. The classifier returns when a
  job the gate waits on runs the suite free-threaded; a place in those
  `needs:` is not that condition, a job green with its suite step
  skipped refusing no landing.
- **`tests/interpreters_test.py` gates that classifier on the
  interpreters the merge gate waits on** (issue btclib-org/.github#577),
  the biconditional the PyPy classifier already carries over a narrower
  second side: the PyPy one reads every CI file, where section 3 keys
  free threading on the gate. That side is the `needs:` closure of
  `test: every job passed` and not this workflow's own text: a job
  nothing waits on says the build passed somewhere, which is the ground
  section 3 declines. Comments are dropped before that read, where
  `_named` above it keeps them on purpose, and what the read leaves is
  what `dist` shows: a setup step naming no interpreter takes the
  `.python-version` pin, and a version a job names counts whatever that
  job does with it, a step skipped on its own condition included (#750).
  Both arms of that read take the characters a version is made of and
  nothing else: an expression otherwise reaches the closure as `${{`
  from `--python`, or whole from between a pair of quotes, and answers
  its emptiness check with a name that is not one.

### `local-link-prefix`'s comment matches what `README.md` holds

- **The comment said this tree carried no badge link, and `README.md`
  carries a row of them.** It now names that row, and says what the row
  does not settle: every destination on it is absolute, so the pattern's
  scheme lookahead decides each of those lines before its badge handling
  is reached, and the case is unmeasured here for that reason rather
  than for the absent row the comment claimed (closes #743).

### The topics and the keywords are compared by a command

- **`REPOSITORY.md`'s *Features, and the topics* carries the `diff` that
  reads the topics and `pyproject.toml`'s `keywords` together** (issue
  btclib-org/.github#636). It stated the pair and gave no command for
  it, half being a repository setting and half a tracked file, so the
  comparison was left to whoever thought to make it by eye. The command
  is the one sibling copies of this file carry, running here unchanged
  but for the repository name, and it exits nonzero on a drift. The
  sentences beside it are theirs too: what an empty right-hand side
  means, and why both sides are sorted.

### A convention name wrapped in its middle is read as it is written (closes btclib-org/.github#651)

- **`tests/unit/conventions_test.py` collapses the whitespace of the
  *Not tested here* list and then of each name in it.** `strip()` takes
  whitespace off a name's ends and leaves what an eighty-column wrap puts
  in its middle, so a name the break splits matched none of section 7's
  and was reported as a convention the declaration invented -- a red test
  on a declaration that reads correctly in the file. `tests/README.md`'s
  line here breaks at a semicolon, so nothing was red; what goes is the
  dependence on where the column falls, which nothing enforces.
  `btclib-benchmarks` reads its own line this way, its wrap falling
  inside a name.

### The `.gitattributes` comment names the driver's sides and one anchor

- **The comment above the two `merge=union` lines is `btclib-org/.github`'s
  wording** (issue btclib-org/.github#646): `union` keeps `ours` first
  and then `theirs`, each of merging and rebasing is named for which
  side it calls `ours`, and the comment reads the driver as resolving
  two branches writing an entry at one anchor rather than a bullet
  appended to one of a few changelog groups.

### The tree gains the `deps-oldest` sentinel, and the badge row two badges

- **`README.md`'s row carried no badge for `pypi-install.yml`**
  (closes #740), though the workflow was already here: section 2 of
  the organization standard ties a sentinel's badge and its workflow
  together as one membership, and the row was short of its half. The
  new line sits after `deps-latest` and before `deps-oldest`, section
  10's calendar order over that stretch, and carries `?branch=main`,
  which section 2 has asked of every workflow-status badge since
  btclib-org/.github#579.
- **`deps-oldest` was nowhere in this tree** (closes #739): section 10's
  *Which trees carry which sentinel* names `btclib-node` for it, and
  nothing here ran the workflow or showed its badge.
  `.github/workflows/deps-oldest.yml` runs
  `uv lock --resolution lowest-direct` on `requires-python`'s own floor
  and then the suite, the mirror of `deps-latest.yml`'s `--upgrade`;
  its badge sits after `pypi-install` and before `os-macos`, the
  calendar order again, with the same `?branch=main`. Unlike
  `deps-latest.yml`, this workflow carries no lint job: that job exists
  there because a new mypy release can add a check the codebase fails
  before the next re-lock catches it, and a floor resolution moves mypy
  to a release already behind rather than one ahead, which is not a
  claim `pyproject.toml`'s dependencies make to an installer. `btclib`
  is the one direct dependency the new job cannot take to a floor:
  `[tool.uv.sources]` points it at the git branch `main` rather than at
  a registry range, so the specifier beside it is a claim to an
  installer and not to this checkout, and the resolution moves it to
  that branch's tip as `--upgrade` does rather than to the floor. What
  it verifies at a floor is every specifier uv resolves from a
  registry: `rocksdict` among the runtime dependencies, and the
  `[dependency-groups]` entries the suite step installs.
- **No repository of the organization carried
  `.github/workflows/deps-oldest.yml` before this branch**, measured
  across the organization by HTTP status line with `deps-latest.yml` as
  the positive control. The debt is carried until the first tree
  schedules the workflow, and this is that tree
  (issue btclib-org/.github#323).

### `os-macos.yml` and its neighbours name the store this tree runs

- **`os-macos.yml`'s header called the store no argument for the file:
  `sqlite3` from the standard library, no wheel to be missing and no
  compiler to be present** (closes #765). It is RocksDB through
  `rocksdict`, so the paragraph says instead what the store adds to
  this cell -- a compiled extension whose published wheel this image
  resolves and the gate's own cell does not, read back by the suite.
  `uv.lock` names wheels for `rocksdict` and no sdist, which is what
  "no compiler runs here" now rests on.
- **The paragraph above it no longer calls the classifier the whole of
  the argument**, and the comment above `runs-on:` no longer argues for
  one macOS image on the start method alone: the store's wheel is the
  architecture's, where the start method is the platform's, and
  `os-macos.yml` names issue #772 rather than settling it.
- **`pypi-install.yml` carried the same clause in its own words**,
  contrasting a standard-library store against `btclib[secp256k1]`'s
  compiled extension. `import btclib_node` pulls `rocksdict` in, so
  what a bare import leaves open there is the bindings that degrade
  silently and not the store; the quotation of `os-macos.yml` in its
  matrix comment tracks the sentence as that file now reads.
- **`.github/ISSUE_TEMPLATE/bug_report.yml` asked a reporter for
  `sqlite3.sqlite_version`**, explaining that the store bound whatever
  SQLite the interpreter was built against. It asks for the version of
  `rocksdict`, the wheel platform and interpreter pick.

### `_gating()` reads a conditioned step as one that may not have run

- **A job whose pytest steps all carry an `if:` no longer counts as the
  merge gate running the interpreter it names.** Its setup step still
  names that interpreter unconditionally, but the job's own conclusion
  does not require the gated step to have run -- `free-threaded`'s own
  sync fails on every run today (issue #723), so the job is green with
  its suite never exercised. `_runs_the_suite` tells the two apart, and
  no job the gate waits on takes that branch today (closes #750).
- **A `uses: ./...` an unconditioned step of a gating job calls is read
  too, the same way `test_every_declaration_was_read` already reads
  every workflow and composite action** (closes #757): what such an
  action pins is invisible to a read that stops at the workflow file,
  the call site naming no interpreter of its own.
- **The two are one rule, applied once.** A step that may not run is
  not evidence the gate runs what it names, and it is no more evidence
  of what it reaches, so every conditioned step is excised from a job
  before either reader looks at it -- a `python-version:`, a `--python`
  and a `uses: ./...` all refused by the same excision rather than by a
  rule restated per reader. That matters here rather than in the
  abstract: the gate's only local-action call is `dist`'s, gated
  `if: inputs.version-suffix != ''`, which holds on a release rehearsal
  and on neither a pull request nor a push to `main`. Followed
  unconditionally it would have made an interpreter the gate never runs
  count as one it does, which is the "it passed somewhere" section 3 of
  the organization standard refuses and the whole ground of issue #750.
  No job the gate waits on calls a local action from an unconditioned
  step today, so the follow is held by a unit test on job text of its
  own rather than by the real workflow -- a walk that finds nothing,
  named as one rather than left to read as coverage.
- **An action's own comments are dropped before it is read**, as a
  workflow's already were (issue #757): a commented-out step carries
  `--python` in exactly the form this read matches, so an unstripped
  read would have reported an interpreter the gate does not run --
  the same false positive as the two above, one scale further down.
- **A step conditioned on its own dash line counts as conditioned**
  (issue #750): YAML puts a mapping's first key on the sequence's dash
  or on the line below it indifferently, and this tree writes both
  forms, so a read anchored on the deeper indent alone saw only the
  second. For a `uses:` that miss withheld; for an `if:` it admitted,
  reading a conditioned step as one that always runs -- which is the
  over-count the entry above exists to refuse.

### What `rocksdict` publishes, and what a dropped resource warns

- **`pyproject.toml`'s argument against an `OS Independent` classifier
  said `rocksdict` carries an sdist, and `src/btclib_node/db.py`'s
  argument for choosing it over `plyvel` said `win_arm64` builds from
  source** (closes #777). It publishes wheels and no sdist, on every
  one of its releases, and no `win_arm64` wheel on any of them, so
  that platform fails at resolution rather than compiling. The
  paragraph's own opening went with it: it had said the dependencies
  are not what argues against the classifier, which held only while
  they were believed to install everywhere. One of them now makes the
  install fail outright on one architecture, so the paragraph names two
  arguments rather than dismissing one.
- **The comment above `filterwarnings` named a sqlite connection**
  (closes #774) among what `tests/conftest.py`'s fixtures close, and
  the store is RocksDB through `rocksdict` (#641). A dropped `Rdict`
  raises no `ResourceWarning`, where a socket, an event loop, a worker
  pool and an open file each raise one, so the comment names those.

### The platform sentinels answer for the store's own wheel

- **`os-macos.yml` runs `macos-26-intel` beside `macos-latest`**
  (closes #772). The start method a macOS cell answers is the
  platform's and either image answers it; what the second image adds is
  the store, `rocksdict` being a compiled extension whose
  `macosx_10_14_x86_64` wheel `uv.lock` names beside the
  `macosx_11_0_arm64` one, with no sdist behind either, so a cell
  resolves the wheel its own architecture selects or fails at
  environment sync. Two images is the shape btclib-org/.github#85 gives
  a platform sentinel.
- **`os-ubuntu.yml`'s header named the compiled `libsecp256k1` as what
  the second image buys, and the store is the other** (closes #773).
  What a missing wheel costs differs between the two: the bindings
  publish an sdist and build on the runner, `rocksdict` publishes none
  and fails the cell at environment sync.
- **`pypi-install.yml`'s matrix is every image a sentinel or a gate
  cell of this repository runs**, its own rule, so the Intel macOS cell
  is there too; the comment says what a Darwin cell resolves rather
  than quoting a sentence about one macOS image.
- **`pyproject.toml`'s operating-system classifiers are a claim about
  where the suite is run**, and the comment listing those images names
  the new one.

- **The `pyproject.toml` paragraph this branch edits gained two prose
  fixes review left to it** (issue #777): a trailing relative clause
  that attached to the wrong noun and duplicated a sentence four lines
  below, and a missing connective that made the dependency inventory
  read as the first of the two arguments when it is the evidence for
  it. They belong here rather than in a round of their own, being the
  same comment block.

### `CLAUDE.md` checks the rebased file, not `difflib`'s tie-break

- **`inserted[0] == ""` is named as a tie-break rather than a fact**
  (closes #771). An entry is bounded by a blank line at each end, so at
  a clean append two alignments reproduce the rebased blob byte for
  byte -- one whose block begins with the blank line, one whose block
  ends with it -- and `difflib` reports whichever its own tie-break
  reaches, with nothing in its output saying the other exists. The
  damaged file admits one. So the assertion reads which alignment was
  reached, not whether the file is whole.
- **What the file now says is the check is the byte-for-byte rebuild**
  (issue #771), with the cheap ones named as cheap ones beside it: the
  opcode shape, which catches a line lost from the base and a
  reordering of what the base held; the reconstruction identity
  `B.replace(X, "", 1) == A`,
  whose occurrence count does not move under an eaten blank; and
  `markdownlint`'s `MD022/blanks-around-headings`, a detector of exactly
  this damage that the tree already runs and that the hook's own
  `--fix` repairs. Neither the opcode shape nor the identity says where
  the block sits, and the bullet says so.
- **Whether the base side moved at all is one `git diff` away, and is
  asked first** (issue #771): a seam needs two sides, so where the
  commits being rebased over never touched the file, nothing abuts and
  nothing can be eaten. `git diff --stat <old base> <new base> --
  CHANGELOG.md` answering empty settles that half before a blob is
  read. The bullet is explicit that this is **not** the *no-seam case
  has to be shown to be a case* guard, which is about the arriving side
  going empty and which this command cannot see: a first draft said it
  was, and review produced the branch on which the command answers 301
  insertions while nothing arrives at all.
- **A third shape of the coverage-floor flake sits beside the two
  already recorded** (closes #762): one statement of a coroutine defined
  inside a test in `tests/unit/p2p/manager_test.py`, at ordinary load,
  every test green, and the next run meeting the floor. The scheduler
  decides whether that coroutine's body runs, and nothing in the test
  asserts on it.

### `release.yml` says what `public-api`'s `needs:` entry orders

- **The comment above `public-api` and the one on `publish-testpypi`
  name the upload as what that entry orders** (closes #733).
  `public-api` carries no `needs:` of its own, so it starts when the
  run starts, beside the lint and docs calls; the test call waits on
  `version-check`, and the graph orders the two against each other
  neither way. The jobs that name it are `publish-testpypi` and
  `publish-pypi`, so what the entry holds is an upload until the
  surface has been read. The job-to-`needs:` map the wording is
  checked against is
  `grep -nE '^  [a-z0-9-]+:$|^    needs:' .github/workflows/release.yml`.
  btclib-org/btclib#1536 is the same sentence in that tree's own
  `release.yml`.

### Every workflow-status badge reads `main`

- **Every one of `README.md`'s fifteen workflow-status badges carries
  `?branch=main`** (closes #767), which section 2 of the organization
  standard requires of the gates' and the sentinels' alike (issue
  btclib-org/.github#579): unqualified, such a badge renders another
  branch's run where the workflow has none on `main`, and nothing in
  the render says so.
- **The badge that was visibly wrong is `vendored-vectors`** (closes
  #736), whose runs were all a feature branch's, where every other
  badge in the row was reading `main` only because `main` happened to
  have a run. #767 is the row, #736 the one member of it whose render
  had already gone wrong, and a row qualified in part is one a reader
  cannot tell the audited half of.
- **`vendored-vectors.yml` is dispatched from `main`** (closes #736)
  rather than left at `no status` until its calendar day, which is what
  section 2 asks of a tree landing the badge and what its own
  `workflow_dispatch` trigger is there for.

### Every dependency-group entry declares a floor, and `check` has a local run

- **`mutation`, `docs` and `check` declared no lower bound, and
  `uv lock --resolution lowest-direct` takes an entry that declares
  none as far down as the rest of the resolution allows** (closes
  #761): a `furo` from 2021, a `cosmic-ray` at `0.0.0`, a `twine` at
  `1.0.1`. The command warns on an entry that declares no bound, and now
  has none to warn about. The bound each gains names the release its own
  consumer is run against, and the head of `[dependency-groups]` is
  where that is argued: `deps-oldest.yml` installs `--group test` alone,
  so for a group it does not install, the run behind the number is that
  group's own workflow rather than the sentinel's, and that workflow's
  own header names the argument in place of a list of what declares
  none. The re-lock this takes changes `uv.lock` only inside
  `[package.metadata.requires-dev]`, where the specifiers are recorded:
  no package version and no pinned commit moves with them.
- **`CONTRIBUTING.md` gives the commands that build the distribution
  and read it with the `check` group** (closes #778): `uv build`, the
  sdist normalizer, and `twine check --strict`, `check-wheel-contents`
  and `pyroma --min 10` under `--only-group check`, which is the form
  `test.yml`'s `dist` job runs them in. It goes under *This repository
  in particular*, the half of that file this tree writes for itself.
  The group's `check-sdist` is left out: `.pre-commit-config.yaml` runs
  it as a hook the lint gate already asks.

### deps-latest builds the distribution and inspects it

- **`deps-latest.yml` gains a `dist-latest` job** (closes #763): it runs
  `uv build` after `uv lock --upgrade`, then `twine check --strict`,
  `check-wheel-contents` and `pyroma --min 10` over what that wrote.
  Section 10 of `btclib-org/.github`'s `README.md` names the packaging
  checks as one of the three things this sentinel is, and `test.yml`'s
  `dist` job -- the other consumer of the `check` group -- runs them
  against the committed lock, where these run against the one
  `uv lock --upgrade` has just rewritten: a pyroma that scores this
  metadata below the `--min 10` ratchet, or a twine that refuses metadata
  this project publishes, reports on the schedule rather than on the tag
  that consumes the version. The job is narrower than `test.yml`'s
  `dist`, which also produces the files `release.yml`'s publish jobs
  download and smoke-tests the wheel with no lock in play.
- **`.github/dependabot.yml` names what each uv sentinel resolves**
  (closes #775): `uv lock --upgrade` takes every floor `pyproject.toml`
  declares to the newest release that floor admits, and the comment above
  the `uv` ecosystem says that and the three things the run puts it
  through. It says too that `deps-oldest.yml`, which runs the Thursday
  Dependabot does, takes those specifiers to the other end and so reports
  nothing about the release being offered.

### The issue templates ask what a reporter actually has

- **The *Commit* field said this package is not on an index**, which
  `v2026.8.27` on PyPI (`CONTRIBUTING.md`'s *A release path, and what
  it has published*) falsifies: it now asks for `git rev-parse HEAD`
  from a checkout or the installed version from a PyPI install, the
  latter naming an exact commit through its own `v<version>` tag
  (closes #789).
- **The *Platform* field asked every reporter for a `rocksdict`
  version**, which the published `2026.8.27` release has none of --
  its `requires_dist` is `['btclib[secp256k1]>=2026.8.27']` and its
  `db.py` imports the stdlib `sqlite3` -- so the field now asks a
  reporter on that release to say so instead of running a command that
  raises against it (closes #789).
- **`question.yml`'s own *Commit* field carried the same sentence**
  (closes #792), a `git rev-parse HEAD` and the claim that a checkout
  is all anybody runs. It takes the same repair, shorter for being the
  field that says *if it matters*: the installed version answers for a
  PyPI install, and names a commit through the release tag. The two
  templates are one sentence in two files, so they are one change.

### `test.yml`'s header cites the design, not an issue tracking its absence

- **The paragraph on the organization's one-image, one-interpreter gate
  states section 10's own interpreter carve-out** (closes #764). It named
  btclib-org/.github#390 as tracking this file's divergence, in the
  present tense, where that issue is closed as completed and the pull
  request closing it is what wrote the section 10 paragraph carving an
  interpreter axis out of the rule.
- **Section 10 now carves out the platform axis too, and the paragraph
  states that rather than pointing at the issue that settled it.** A
  platform row leaves a tree's entry where a gate cell asks the whole of
  what that tree's sentinel asked, and section 10 names this tree on that
  ground -- the `windows` job gating a `windows-latest` cell at the
  interpreter `.python-version` pins, on the suite passing and not on the
  coverage floor. So the Windows row here is sanctioned rather than
  merely unreviewed, and no `os-windows.yml` is owed back.
- **That second half was written twice, and the first version was the
  same defect this entry is about.** It named the question as open and
  cited btclib-org/.github#618 for it, which was true when written and
  false once that issue landed -- a citation in the present tense
  outliving what it cites, which is exactly what the `#390` reference
  above had done. A paragraph that names an open
  question cannot be written to survive the answer; only one that states
  what the standard says can.

### `unstarted_node_context`'s docstring says why the stores close

- **The docstring justified all six of its closes as avoiding a
  `ResourceWarning`, and a dropped `rocksdict` `Rdict` raises none**
  (closes #781). Measured directly: `del`-ing an `Rdict` and
  collecting warns nothing, where dropping this same function's event
  loop or open file each raise one.
- **The three stores close for a different, measured reason: a live
  handle holds `db.py`'s own directory `LOCK`.** A second `Rdict`
  opened on the same path while the first is still referenced fails
  outright with an IO error naming the lock, also measured directly,
  never a warning. `regtest_node` hands out several nodes sharing one
  `tmp_path`, and the node holding a store's handle stays referenced
  by the test until this teardown runs, so a test that reopens the
  same store needs the `close()` to have actually run.

### The docs-gate bullet states the condition it actually catches

- **`CLAUDE.md` said the docs build refuses a closing backtick right
  before a bare letter, and `src/btclib_node/db.py`'s own docstring
  carries `` `Coin`s `` while the gate stays green over it** (closes
  #784). Docutils only reports that shape where its paragraph has no
  *later* single backtick to close a title-reference with instead;
  where one exists, as it does in `db.py` and in
  `src/btclib_node/p2p/connection.py`, the reference is rendered --
  swallowing everything between the two backticks -- and nothing warns.
- **The two-sided reproduction is a scratch page, not a hand trace**
  (closes #784): the identical sentence with a later backtick to
  rescue it builds silently, and the same sentence with nothing after
  it draws "Inline interpreted text or phrase reference start-string
  without end-string" at the line the backtick opens, which
  `sphinx-build -W` turns into exit 1.
- **The citation the old bullet carried for its underscore-prefixed
  claim named the wrong issue** (closes #784): #569 is the
  `verify_mempool_acceptance` height off-by-one `CLAUDE.md` argues two
  paragraphs above it, reused here by mistake rather than naming
  anything about a docs build. The exemption itself still holds --
  `_default_worker_count`, `_tasks` and `Node.__del__` all carry the
  construct, and autodoc renders none of their docstrings without
  `:private-members:` or `:special-members:`.

### `pypi-install.yml` says which store it installs

- **The header said the store was what `import btclib_node` pulls in
  of its own accord -- true of this tree, not of the one release the
  workflow installs.** `btclib-node` 2026.8.27's `requires_dist` is
  `['btclib[secp256k1]>=2026.8.27']` and its own `db.py` imports the
  stdlib `sqlite3`, read from the index on 2026-09-02; no cell resolves
  a `rocksdict` wheel (closes #783).
- **`pyproject.toml` already depends on `rocksdict>=0.3.29` and no
  published release carries it**, which is what makes the gap between
  the tree and the index one release wide rather than something here
  wanting a further change (closes #783). The header says so in those
  terms rather than predicting the next release, a sentence true only
  until it lands being the defect issue #764 is about.
- **The matrix comment beside the two macOS images carried the same
  claim** -- that the wheels a Darwin cell resolves included the store
  -- and is corrected alongside the header (closes #783).

### The `lint` group's floors name the runs behind them

- **`mypy`'s floor moves from `1.18.2` to `2.3.1`** (closes #785).
  Nothing had ever installed the group at `1.18.2`: `deps-oldest.yml`
  installs `--group test` alone, and its own header says lowest-direct
  "moves mypy to its own floor instead, which is not a release this
  tree has yet to see." The `mypy` hook is `language: system` over
  `uv run --locked --no-default-groups --group lint --group test mypy`,
  so this group's own resolution is what it type-checks under, and the
  bound now names a release that run has exercised.
- **`ruff`'s floor moves from `0.16.4` to `0.16.5`, the `rev:` that
  actually runs it** (closes #800). Nothing runs the group's own
  `ruff`: `ruff-check` and `ruff-format` come from
  `astral-sh/ruff-pre-commit`, which pre-commit builds as an
  environment of its own, and the two numbers had already drifted
  apart. That entry's consumer is a `rev:` rather than a workflow step,
  as `check-sdist`'s is, and `[dependency-groups]`'s header now says so
  of both instead of of one.
- **The header says what a floor falling below the lock does and does
  not mean** (closes #785, closes #800). Nothing re-widens a bound --
  `dependabot.yml`'s own comment says the uv-lock hook does not
  upgrade, and `deps-latest.yml` moves `uv.lock` and never this file --
  so drifting under the resolution is what every entry here does by
  design. What neither issue tolerates is a bound no run has ever
  exercised, which is why the check the header asks a reader for is
  that the consumer named beside an entry is still that entry's
  consumer, not that the digits still match.

### The record says where its own entries cite their issues

- **`CHANGELOG.md`'s introduction says which of its entries carry the
  issue number on the `###` heading and which carry it in the bullet
  alone** (closes btclib-org/.github#547): section 9 of the organization
  standard puts the citation in the bullet, beside the claim it answers,
  and a reader of this record does not reach that section --
  `CONTRIBUTING.md` is what points a contributor at it. What that reader
  met was a heading with a number, then a heading without one, and
  nothing saying whether the second was a convention or an omission.
- **The reason is written beside the shapes rather than left to be
  worked out**: section 11 of the standard requires the landing subject
  to carry `(closes #N)`, so a heading written from that subject arrives
  with the citation in it, and *One fact in one place* is what decides
  it does not stay there once the bullet has it.
- **The entries had already converged before the introduction said so**,
  which is the gap this closes: a reader cannot read a convention off
  practice, and the two shapes are indistinguishable from a lapse until
  something states which is which. Nothing already written is rewritten,
  that being the standard's own answer to the retrofit.

### `git worktree remove --force "$WT"` stands in a block of its own

- **The removal no longer sits under a line that ends in a
  placeholder** (closes btclib-org/.github#676): `<` and `>` are the
  shell's redirection operators, so the `WT=` line fails on its own
  redirection without clearing whatever `WT` already held, and a shell
  that discards that failure as a parse error reads the next line as a
  fresh command against that value. The removal stands in a fence of
  its own, with prose between the two fences saying why the first one
  closes early, so the line that deletes a directory is reached by
  pasting it on purpose.

### `REPOSITORY.md` reads both variable stores back for the review switch

- **The file records `vars.CLAUDE_REVIEW_ENABLED`, the switch
  `claude-review.yml` guards both its jobs with** (closes
  btclib-org/.github#682). Both stores are read: the repository's
  because a variable set here would take precedence over one of the
  same name set on the organization, so the organization's answer alone
  would not show the switch off for this tree, and the organization's
  for the empty name list section 11 reads as the off state -- with a
  `total_count` beside it, a store that prints nothing at all when it
  answers needing one to show the call reached it. The paragraph sits
  beside *A credential the organization holds*, which is where this tree
  already records what the organization sets on its behalf, rather than
  under a heading of its own.
- **The repository's Actions variables leave the list of facilities
  whose empty answer records no decision** (closes
  btclib-org/.github#682): that zero is half of what records this one,
  so the sentence excluding webhooks from that list gains its
  counterpart for the variable store.

### `REPOSITORY.md` records the Read the Docs automation rule

- **The Read the Docs section's expected outputs are current**:
  `v2026.8.27` now reads `active: true, built: true` and
  `/en/v2026.8.27/` answers `200`, where the rule's absence had left
  both false (closes #596).
- **Its automation-rule bullet records what Read the Docs' dashboard now
  carries** -- Match: SemVer versions, Version type: Tag, Action:
  Activate version, matching the three sibling projects' -- and why
  `v2026.8.27` needed a second, manual activation: a rule fires only on
  a version Read the Docs creates or updates from a webhook, which a tag
  already on the dashboard when the rule is added never triggers
  (closes #596).
- **The rule itself stays outside what any call here reads back**: the
  automation-rules endpoint answers `404` unauthenticated for
  `btclib-secp256k1`'s own rule too, so that `404` is an absent token
  rather than an absent rule, and "What this file passes over" is
  reworded from "would build" to name the rule rather than a still-future
  effect (closes #596).

### `CLAUDE.md` says what a worktree does not isolate, and what load does to a run

- **The worktree rules gain the object store beside `refs/stash`**
  (closes #806): a commit written in a worktree is in the one shared
  store the moment it exists, so every local read -- content, diff,
  signature -- answers for it exactly as it would after a push, and a
  reviewer handed that sha confirms all of it while reading something
  the forge does not have. The ref is the read that answers, and it is
  given with the `--force-with-lease` that makes a concurrent writer a
  refusal rather than a clobber.
- **The ports bullet says what a load-decided failure presents as**
  (closes #807): a timeout and never an assertion. The three
  coverage-floor bullets below it are each a run with every test green
  and say so as their own discriminator, so a reader holding actual `F`s
  matches three bullets that all rule themselves out; the measurement
  beside it is two tests timing out at load 107 -- one functional, one a
  unit test that is merely slow, which is why the discriminator is the
  timeout and not the directory -- against the same sha green at 7.96,
  on a diff touching no Python at all.

### `chains.py` reads consensus off `btclib.consensus` instead of holding it

- **`Chain.consensus` is `btclib.consensus.CONSENSUS_PARAMS[chain.name]`,
  and `flags`, `bip34_height`, `subsidy_halving_interval`,
  `pow_allow_min_difficulty_blocks`, `pow_no_retargeting`,
  `bip30_exceptions` and `minimum_chain_work` are gone from `Chain`
  itself.** `Chain` keeps what identifies this node's own copy of a
  network rather than the network itself -- its ports, its seed
  addresses, `prune_after_height` and its genesis block -- and every
  caller that used to read one of the seven off `Chain` now reads it off
  `chain.consensus` instead (issue #801).
- **Testnet3's `TAPROOT` row, which carried `1628640000` -- Core's own
  deployment timeout, a unix timestamp, marked "wrong, this is the
  date" in this file -- disappears with the rest of the height table
  rather than being replaced by a number.**
  `btclib.consensus.ConsensusParams` carries no BIP9 deployment data --
  Core's own `vDeployments`, of which a taproot activation height is one
  field -- by its own design: a table of heights does not model
  signalling, and no caller of it asks for one (issue #801).
- **The removed `(170061, "P2SH")` matched no Bitcoin Core parameter at
  any ref.** `BIP16Height` was `173805` on mainnet and `514` on
  testnet3, and `bitcoin/bitcoin@ce650182f4` removed it outright, with
  no successor field of any spelling since. The removed
  `(709632, "TAPROOT")` is a different kind of number: it **was**
  Core's own `DEPLOYMENT_TAPROOT.min_activation_height` at
  `bitcoin/bitcoin@9be056a8a7` (v31.1, the tag this table transcribes
  against), and is gone from Core's own `master` since
  `bitcoin/bitcoin@74f71c5054` dropped taproot's BIP9 tracking
  entirely. `GetBlockScriptFlags` never read that height even while
  Core carried one -- only DERSIG, CLTV, CSV and NULLDUMMY consult a
  per-height activation there -- so this row's own defect was always
  the height gate itself, and after this change the gate exists in
  neither tree (issue #801).

### `interpreter.get_flags` stops gating P2SH, segwit v0 and taproot on height

- **`get_flags` is `chain.consensus.script_flags_at(height, block_hash)`,
  Bitcoin Core's own `GetBlockScriptFlags`, in place of a table of
  `(height, name)` pairs.** P2SH, segwit v0 and taproot are on for every
  block of every chain rather than gated on an activation height, so a
  mainnet block below height 170061 is now checked with P2SH on where
  it used to be checked with P2SH off -- the permissive direction, this
  node having accepted a spend Core rejects on a chain whose history it
  is replaying (closes #809).
- **`block_hash` is what answers for the handful of historical blocks
  Core exempts by hash rather than by height**, `check_transactions`
  threading the connecting block's own hash through and mempool
  acceptance threading the active chain's own tip -- there being no
  candidate block a mempool transaction is checked against, which is
  also why Core's own consensus re-check of one reads the tip's flags
  rather than the next block's (issue #801).

### `git worktree add` ends in the placeholder it names

- **`-b <branch>` sits after the path and the commit-ish, and a
  paragraph under the fence says why** (issue btclib-org/.github#687):
  `<` and `>` are the shell's redirection operators, so a placeholder
  with a word after it leaves `>` that word as its target, and ahead of
  `"$WT"` the target is the worktree path itself. In a directory holding
  a file named `branch` the `<` succeeds and the line runs; the removal
  fence below leaves no directory at `$WT`, which is the state a second
  paste starts from, so the `>` creates a regular file where the next
  `git worktree add` needs a free path. At the end of the command there
  is nothing for `>` to open, which is what section 9 of the
  organization standard asks of a bare placeholder.

### `Chain.subsidy` and this tree's own coinbase and block builders are btclib's

- **`Chain.subsidy` is gone; `interpreter.check_coinbase_value` reads
  `btclib.consensus.subsidy(height, chain.consensus.subsidy_halving_interval)`
  directly** (issue #801). `chains.create_genesis` stays: btclib left a
  network's genesis block out of `btclib.block.build`, filed as
  [btclib#1602](https://github.com/btclib-org/btclib/issues/1602),
  `Network.genesis_block` being 32 bytes rather than a `Block` and a
  `Network` field holding one closing an import cycle.
- **`tests/__init__.py`'s `generate_coinbase` and `build_block` build
  through btclib's own `block.build.build_coinbase` and
  `block.mining.candidate_block_header`.** A coinbase committing to no
  height at all is still built by hand, `build_coinbase`'s own
  docstring naming that as the escape for a deliberately invalid
  candidate (issue #801).

### `chainstate/contextual.py` is gone, and the bug found in it closes with it

- **The module is deleted: `chainstate/block_index.py` checks a
  header's required target and its timestamp over btclib's own
  `block.next_bits_required` and `median_time_past`, `main.parent_lookup`
  the one callable of it that stays** (issue #801).
- **`next_bits_required` used to return the parent's own target
  whenever `chain.consensus.pow_no_retargeting` was set, before ever
  reading `pow_allow_min_difficulty_blocks`** (closes #812). Regtest
  sets both flags at once: Bitcoin Core's `GetNextWorkRequired`
  (`bitcoin/bitcoin@9be056a8a7`, v31.1) reads
  `fPowAllowMinDifficultyBlocks` first, `fPowNoRetargeting` gating only
  `CalculateNextWorkRequired`, reached solely at a difficulty period
  boundary -- 144 blocks on regtest, not 2016, `pow_target_timespan`
  being one day rather than two weeks there. btclib's own
  `next_bits_required` keeps Core's order.

### The documentation build is `sphinx-build -W -n` at every site

- **`--keep-going` is passed nowhere.** `.github/workflows/docs.yml`,
  `.readthedocs.yaml`, `CONTRIBUTING.md`'s gate block and
  `RELEASING.md`'s pre-tag build each run `sphinx-build -W -n`, which is
  what section 2 of the organization standard's *The documentation* asks
  for, with the reason written there (issue btclib-org/.github#347).
- **`-W` reports every warning a build raises and fails at the end of
  it**, which is what the comment beside the command in `docs.yml` and
  `.readthedocs.yaml` says, so one broken page does not hide the next.

### `RELEASING.md`'s rebuild block chains from its placeholder line

- **Pasted before `<version>` was filled in, the block reached `uv
  build` in the reader's own working directory rather than in the
  rebuild worktree** (issue btclib-org/.github#657). Its first line ends
  in `v<version>`, which an interactive shell answers with a parse error
  and discards, reading `cd /tmp/btclib-node-rebuild` as a fresh
  command; that `cd` then fails, the worktree never having been created,
  and `uv build`, `normalize_sdist.py` and `generate_sbom.py` ran where
  the reader stood.
- **The `cd` sits inside the chain, so no line below it runs unless the
  `cd` succeeded.** Whatever the reader's directory holds, the build
  runs in `/tmp/btclib-node-rebuild` or it does not run.
- **The rejected alternative makes the `cd` fatal on its own** -- `||
  exit`, or a `set -e` above the block. Either closes this block too,
  and closes it by ending the shell: a command appended under the block
  runs after the chained paste and does not run after either of those,
  so an interactive reader loses the terminal they pasted into. The
  chain refuses the same paste with the session still standing.
- **A shell reading the block from a file aborts at the parse error and
  invokes nothing** -- `zsh` rc 1, `bash` and `sh` rc 2. `zsh` reading
  the same bytes from stdin does not: `zsh < block` and `cat block |
  zsh` discard the line and carry on as an interactive shell does, which
  is the split section 9 of the organization standard states. So what
  reports this block harmless is a harness handing the block over as a
  file, not a non-interactive harness in general.

### The release asks whether read the docs served the tag

- **`release.yml` gains a `documented` job, and it is not the `docs`
  job** (closes btclib-org/.github#644): `docs` runs the same build
  `docs.yml` gates a pull request with, on the runner; `documented`
  asks whether `https://btclib-node.readthedocs.io/en/<tag>/` answers
  200. A build green on the runner and a read the docs project that
  never triggered, or triggered and failed, are indistinguishable from
  inside a release that only asks the first.
- **The wait is a script with a test rather than a loop in the
  workflow.** `.github/scripts/wait_for_readthedocs_build.py` polls on a
  deadline, so the job's `timeout-minutes` is compared against one
  number rather than a product, and carries the last answer into the
  `::error::` that names the builds page. A tag push is the only trigger
  that reaches it, so `tests/wait_for_readthedocs_build_test.py` --
  which substitutes the transport and the clock -- is where the retry,
  the deadline and the annotation are exercised at all.
- **`needs: version-check`, and nothing needs it.** A tag that check
  refuses produces one verdict and not two, and a late documentation
  build is not a reason to withhold a wheel.
- **The header comment saying this file calls no such job is gone.** It
  gave as its reason that no automation rule activates a tag on this
  project; #596 added the rule, and REPOSITORY.md's *Read the Docs*
  section reads its effect back.
- **`RELEASING.md`'s audit step names the skip the job adds.** The guard
  is the push, so a rehearsal finds `documented` `skipped` with no steps
  -- the signature that step teaches a releaser to read as the defect.
- **`RELEASING.md` gains a step that reads the job's verdict.** Green is
  the permanent URL to link wherever the released version is named; red
  is a build on read the docs' side rather than a moved tag, nothing
  about the publication depending on it.
- **`testpaths` names the new test module.** `[tool.coverage.run]`'s
  `source` takes all of `tests/`, so a module no run collects is a file
  the floor reports at zero. The comment above that list now names each
  module sitting beside the directories, `tests/property_test.py`
  included.
- **`tests/interpreters_test.py` names `release.yml` among the CI files
  that name an interpreter**, the wait running through `uv run
  --no-project --python 3.14`. That list is a named one rather than a
  count, so a file that starts naming an interpreter is added to it or
  the module goes red.

### `CLAUDE.md`'s section citations name the document they belong to

- **A bare `section 9` and a bare `section 4` named no document.** Both
  now read "of btclib-org/.github's README", the qualifier *Conventions
  to match* already gives that document elsewhere in the file (closes
  #813).

### `interpreter.py`'s five contextual transaction rules are `btclib.tx.tx_context`'s

- **`is_final_tx`, `check_final_transactions`, `check_sequence_locks`,
  `check_coinbase_maturity` and `check_coinbase_value` are gone, and
  `constants.COINBASE_MATURITY` with them.** `main._validate_block` and
  `main.verify_mempool_acceptance` now call
  `btclib.tx.tx_context.is_final`, `assert_sequence_locks`,
  `assert_coinbase_maturity` and `assert_coinbase_value` directly, and
  `COINBASE_MATURITY` is `btclib.tx.limits`'s (issue #801,
  [btclib#1580](https://github.com/btclib-org/btclib/issues/1580)).
  `assert_sequence_locks` takes no `enforce_bip68` flag the way
  `check_sequence_locks` did -- `btclib.tx.tx_context`'s own module
  docstring is why -- so `_validate_block` calls it at all only where
  `bip113_active` already holds, once per transaction, rather than
  passing the flag through unconditionally.
- **A coinbase paying more than subsidy plus fees is now refused as
  `bad-cb-amount: X instead of Y`, Core's own phrase, in place of
  `coinbase pays too much: X instead of Y`.** `assert_coinbase_value`
  is btclib's to word, not this tree's.
- **`block_db.Coin` is a subclass of `btclib.tx.coin.Coin`, carrying
  only `parse` and `serialize`.** btclib's own `Coin` declares no
  on-disk shape on purpose -- its docstring and
  [btclib#1123](https://github.com/btclib-org/btclib/issues/1123) --
  so the varint-packed wire format this store already used is what the
  subclass adds, unchanged byte for byte; `BlockLocation` and
  `FileMetadata`, the two other value types `block_db/__init__.py`
  already carries a `parse`/`serialize` pair for, are the same shape.
  A module of free functions over the plain `btclib.tx.coin.Coin` was
  the alternative, and would have reached every call site the three
  chainstate indexes already make against `Coin.parse`/`coin.serialize`
  -- files this row leaves alone.

### RELEASING.md's scratch names and prose match what the tree does

- **The rebase-reconstruction check wrote its two comparison files to
  the fixed names `/tmp/expected.md` and `/tmp/actual.md`**, so a
  second run of the same step, or a second person on the same machine,
  silently overwrote or read back the other's file. Each comparison
  now runs inside its own `mktemp -d` (closes #821).
- **The *Rehearse on TestPyPI* paragraph said a rehearsal skips the
  documentation build**, and `release.yml`'s `docs` job carries no
  `if:` and no `needs:`, so a `workflow_dispatch` run enters it exactly
  as a tag push does. The paragraph now says a rehearsal runs the
  identical pipeline, the documentation build included (closes #819).
- **The rebuild-block paragraph named only an interactive shell as the
  one that reads the unfilled placeholder line's parse error and then
  the `cd` below it as a fresh command.** `zsh` reading the same block
  from standard input does the same, where `bash`, `sh` and `zsh`
  itself given the block as a file all abort the whole feed on that
  same error instead (closes #818).

### `chainstate/muhash.py`'s MuHash arithmetic is `btclib.muhash`'s

- **`MuHash3072`, `_chacha20_block`, `chacha20_keystream` and `_num3072`
  are gone.** `CoinStats.muhash` is now `btclib.muhash.MuHash3072`
  (issue #801,
  [btclib#1122](https://github.com/btclib-org/btclib/issues/1122)),
  which carries the same construction under a different shape:
  `digest` and `serialize` are properties there, not methods, so
  `CoinStats.digest`/`CoinStats.serialize` -- which stay methods,
  this tree's own on-disk shape being unchanged -- read
  `self.muhash.digest`/`self.muhash.serialize` rather than calling
  them. `CoinStats`, `tx_out_ser`, `_bogo_size` and
  `is_bip30_unspendable` stay: they are this tree's own index and its
  on-disk shapes. Where each belongs is decided in btclib and not here:
  [btclib#1623](https://github.com/btclib-org/btclib/issues/1623)
  carries btclib#1122's own deferred second step for `CoinStats`,
  `tx_out_ser` and `_bogo_size`, and `is_bip30_unspendable`, which 1122
  never named, has no issue of its own yet.
- **The two vendored vector files move with the code they tested.**
  `tests/unit/chainstate/_data/chacha20_vectors.json` and
  `muhash_vectors.json`, and `tests/_data/README.md`'s entry for both,
  are deleted; btclib carries its own copy under its own `tests/_data/`
  now that the arithmetic they check is btclib's.

### `p2p/address.py`'s BIP155 translation and `constants.ProtocolVersion` are btclib's

- **`network_address`, `addr_entry`, `peer_from_addr_entry`, `can_addrv1`
  and the embedded-IPv6 check are gone from `p2p/address.py`; `PeerDB`,
  `can_connect`, `ip_and_port` and `peer_address` stay.** The five now
  live in `btclib.p2p.addrv2`, the last public there as
  `is_embedded_ipv6` where this tree's own copy was `_is_embedded_ipv6`
  (issue #801,
  [btclib#1581](https://github.com/btclib-org/btclib/issues/1581)).
- **`constants.ProtocolVersion` is gone; every call site reads
  `btclib.p2p.limits.PROTOCOL_VERSION` instead, the same value.**
- **`btclib.p2p.handshake.Version`'s `version` field defaults to
  `PROTOCOL_VERSION` now rather than `0`, a breaking change on btclib's
  side** ([btclib#1582](https://github.com/btclib-org/btclib/issues/1582)).
  Every `Version(...)` this tree builds already passes `version=`
  explicitly, so the new default changes nothing here.

### `p2p/messages/errors.py` is `btclib.p2p.reject`'s (issue #801)

- **`Reject` and `RejectCode` are `btclib.p2p.reject`'s, and
  `p2p/messages/errors.py` is gone with the copies it held**
  ([btclib#1583](https://github.com/btclib-org/btclib/issues/1583)).
  BIP61's `reject` was the last message `btclib_node.p2p.messages`
  defined on its own; the package's docstring said btclib carried no
  codec for it, a sentence the btclib landing made false, and it is
  corrected rather than left standing.
- **`callbacks.reject` no longer assumes every code is named.** btclib's
  `Reject.code` is a `RejectCode` where a member names the value and a
  plain `int` where none does, BIP61 reserving whole ranges without
  naming every member of them; this tree's own `Reject` used to refuse
  such a code outright, and the handler read `.code.name`
  unconditionally, which raises `AttributeError` on the bare `int` half
  of that range. The handler now logs the member's name where there is
  one and the bare number otherwise.
- **`exceptions.InvalidRejectPayloadError` is gone.** It was raised only
  by the deleted module's own `Reject.parse`; btclib's `Reject.parse`
  raises `BTClibValueError`/`BTClibTypeError` directly, the family
  `p2p.main.handle_p2p` already sorts a peer's fault by, so nothing in
  this tree raises the class any more.

### A `shell` fence explains itself above the fence

- **`git reset --hard origin/main` reaches git as a hard reset with
  paths under an interactive `zsh`** (issue #828). The comment after it
  becomes arguments, git answers `fatal: Cannot do hard reset with
  paths.` with status 128, and the `git fetch origin` above it
  succeeded -- so the next instruction, which that same comment
  carried, is followed against a tree that was never reset.
  `RELEASING.md` carries it as prose above the fence instead. Section
  9's *A trailing `#` comment ends a chained line* is where the
  mechanism is written down: `zsh` leaves `INTERACTIVE_COMMENTS` unset,
  so an interactive one takes `#` as an ordinary word and what follows
  it as arguments.
- **`CLAUDE.md`'s worktree block sets `WT` in the shell.** With the
  example name after it the assignment is a prefix to that comment's
  first word rather than an assignment, so `git worktree add` on the
  next line is given an empty path; the example sits in the prose above
  the fence.
- **`CLAUDE.md`'s fast-forward and its `rev-parse` of the pushed ref,
  `CONTRIBUTING.md`'s gate block and the chain flags in
  `docs/source/running_a_node.md` lose their trailing comments too.**
  Each handed its command the comment's words, and what each said is in
  the prose around its own fence.
- **`REPOSITORY.md`'s controls state their expected answer on a line of
  their own**, the form the rest of that file already uses, rather than
  after a `grep` that reads `#` and the answer itself as filenames and
  exits 2.
- **The `cmp` in `RELEASING.md`'s rebase check keeps its comment**, the
  block around it being rewritten under btclib-org/.github#745; the
  landed entry carrying one stays as section 9's *Nothing already
  written is rewritten* leaves it.

### The documentation build reads the inventories as published

- **`docs/source/conf.py` sets `intersphinx_cache_limit = 0`, so every
  build fetches `python`'s and `btclib`'s inventories rather than reading
  back the copy sphinx keeps beside the doctrees** (closes #826). The
  copy is under `docs/build`, which `.gitignore` covers, so neither a
  diff nor `git status` says which of the two a build resolved against:
  the documented command answered exit 0 at `c6674ee` in a worktree
  building for the first time, and exit 1 at the same commit in that
  worktree with a copy from an earlier session in place and a source
  file's timestamp moved, on `btclib.tx.coin.Coin` and
  `btclib.consensus.ConsensusParams`, names btclib has published since
  that copy was taken. The timestamp is what makes sphinx re-read the
  page carrying the reference: a build that re-reads nothing resolves
  nothing and answers green whatever its copy holds, which is why the red
  comes and goes.
- **`-E` does not reach it**: it discards the environment and reads the
  copy back, so the build fails there too -- on a third name,
  `btclib.muhash.MuHash3072`, that re-reading every page reaches.
- **A copy resolving a name btclib no longer publishes is the worse
  direction**, and the reason the setting is in `conf.py` rather than in
  the command `CONTRIBUTING.md` documents: `docs.yml` and
  `.readthedocs.yaml` run their own builds, and a reference that passes
  off a copy locally fails on their fresh checkouts. A scratch project
  referencing a name only a doctored copy carried built green against
  that copy and red against the published inventory.
- **What it costs is a fetch of each inventory on every build**, which is
  what those two workflows already do, and a build with no route to the
  two hosts now fails under `-W` where one reading the copy passed --
  measured both ways, with the proxy pointed at a closed port.
- **Not `-d`, putting the doctrees outside the tree**: that relocates the
  copy rather than expiring it, and reaches only the documented command,
  leaving the two workflows resolving against whatever their own doctree
  directory holds. **Not a vendored inventory**, which fixes the
  reference set at the commit that added it, so the build stops asking
  whether btclib still publishes the name. **Not a rewrite of the
  docstring reference `block_db` makes**, which answers for one name and
  leaves every other btclib name a signature carries reading the same
  copy.

### The rebuild block refuses a leftover checkout target

- **`git worktree add --detach <path> <ref>` checks out into a
  pre-existing *empty* `<path>` exactly as into a fresh one**, refusing
  only where the directory is non-empty, so a `/tmp/btclib-node-rebuild`
  left over from an earlier rebuild was reused with nothing in the
  transcript saying so. A `mkdir` of that same literal path, ahead of
  the checkout, now fails there instead: `mkdir` without `-p` rejects an
  existing target whether or not it is empty, and that failure carries
  the same short-circuit the `cd` below it already relies on for the
  placeholder-unfilled paste (closes #823).
- **The guard is silent on that same unfilled paste**, since a parse
  error consumes it exactly as it consumes the `git worktree add` line
  it precedes; only a correctly filled paste is protected against a
  leftover target.

### The worktree fence and the property database say what they guard

- **The worktree-removal fence wrote a bare `"$WT"`; it now writes
  `"${WT:?}"`, catching an unset `$WT` and not a stale one, matching
  `btclib-org/.github`'s own copy of the same block** (closes #834).
- **A property test's failing example is recorded per worktree, under
  `.hypothesis/examples`, and replayed on every later run there but on
  no other: a red property test that will not reproduce outside the
  worktree that raised it is not evidence of a flake for that reason
  alone** (closes #835).

### BIP61's `reject` is fuzzed where its codec lives

- **`fuzz/fuzz_reject.py` and its seed corpus are gone** (closes #827).
  `Reject.parse` is `btclib`'s codec, and `btclib`'s own
  `fuzz/fuzz_reject.py` declares the same entry point under the same
  suppressed family; `.clusterfuzzlite/build.sh` there discovers it with
  its own `find "$SRC/btclib/fuzz" -maxdepth 1 -name 'fuzz_*.py'` loop
  and zips the seed corpus beside it, so the parser is fuzzed in the
  package that implements it, with a corpus that outlives a run --
  which `fuzz.yml`'s own header says this job gives up. Keeping the
  harness buys a second run of one entry point under one family, and
  seeds this suite maintains, for a parser this tree does not implement.
- **The reach that argued for keeping it is `fuzz_process_message.py`'s.**
  `p2p.callbacks.reject` is in `p2p.callbacks.callbacks`, so a peer's
  own octets select it through that harness's selector byte and reach
  `Reject.parse` behind it, seeded by that harness's own `reject.bin`.
  `tests/fuzz_corpus_test.py` holds that seed to selecting the command
  it is named for and to being refused by the callback where it is cut
  inside its reason string: the surface the retired harness's docstring
  argued for -- what a stranger reaches with no verification in front
  of it -- is still driven with a stranger's own octets.
- **Retargeting the harness at `p2p.callbacks.reject` is the rejected
  alternative.** That callback takes a `Node`, a `P2pManager` and a
  `Connection` rather than a bare buffer, which is issue #698's own
  shape and `fuzz_process_message.py`'s already, so the retarget trades
  a duplication against `btclib` for one inside `fuzz/`; and what the
  callback does past the parse is a `RejectCode` test, a format and a
  log, so a fuzzer aimed at it spends its run inside `btclib`'s parser
  either way.
- **`fuzz.yml`'s fuzzing budget is the job's and not each harness's.**
  `-max_total_time` divides the same total across the harnesses the loop
  names, so retiring one lengthens the runs of the rest rather than
  shortening the step.
- **`tests/property_test.py` draws for the entry points the harnesses
  declare**, so `Reject.parse` leaves that layer with the harness.
  `btclib`'s own hypothesis layer, `tests/fuzz_test.py`'s
  `BINARY_PARSERS`, does not name it: btclib-org/btclib#1629 is where
  the per-commit half of fuzzing that codec is tracked.

### `RELEASING.md` gives a placeholder a fence of its own

- **A placeholder line and a line that runs no longer share a fence.**
  A shell reads an unfilled `<...>` as a redirection, and the failure
  stops at the command it is part of, so what stood below a placeholder
  ran where the reader stood: `git rebase origin/main`, a `git show`
  redirected over `/tmp/expected.md`, `gh release download`,
  `gh run download` and `mv attestation/attestation.jsonl`
  (issue btclib-org/.github#745).
- **A placeholder is not always a parse error, and what it costs is
  decided by the reader's directory.** `gh release download v<version>
  --repo ...` reads as an input redirection then an output one, fails on
  the input half, and carries on to the lines below;
  `git show v<version>:pyproject.toml | grep '^version'` forks `grep`
  regardless (btclib-org/.github#751). Where the directory holds a file
  the placeholder names, the input half succeeds instead: that same
  tagging line runs `git show v` and creates `./:pyproject.toml`, and
  `gh run rerun <run id> --failed` re-runs a workflow and leaves a file
  called `--failed`. So every block splits, a one-line block included.
- **A placeholder inside a quoted string is not a redirection at all,
  so the block holding it has no parse error in it.** The PyPI-state
  check carries `<version>` inside a `python -c` program and the job
  audit carries `<id>` inside a `gh api` URL, and an unfilled paste of
  either runs whole: the first makes the request and prints `False`,
  which this file says is what licenses deleting the tag.
- **A second fence is live where the first is inert, and takes two
  guards.** Each value it reads from the fence above is written
  `${name:?}`, the shell's own must-be-set form, and its lines are
  chained with `&&`. Neither closes it alone: an interactive shell
  answers a parameter-expansion failure by ending one command rather
  than the input, and a chain below a discarded placeholder line never
  forms, the parse error taking that line's trailing `&&` with it.
- **The rebuild block keeps its chain and stops resting on it.** `&&`
  closed that block only because the `cd` under the discarded line
  failed and took the rest with it. What the chain carries now is the
  `${version:?}` failure, on top of stopping the rebuild where a build
  or a verification fails. The sentence naming which shells discard that
  line and which abort the whole feed moves to the introduction, the
  mechanism belonging with the rule rather than with one block, and it
  names an interactive `bash` among those that read on rather than among
  those that abort: fed the block on standard input, it invokes what
  stands under the placeholder line.
- **The rebase reconstruction's manual step moves into the prose between
  two fences.** A chain cannot express a step a person performs, so the
  block ends where the hand edit begins and the comparison after it is a
  fence of its own. The first asserts `: "${version:?}"` at its head, no
  command in it reading the value the hand edit needs; the second writes
  the scratch directory it takes from above as `${scratch:?}`, in the
  redirection that is its own first write. The comparison's trailing `#`
  comment moves to the prose above the fence, an interactive `zsh`
  leaving `INTERACTIVE_COMMENTS` unset and passing it to `cmp` as
  arguments.
- **The guard pair is section 9's.** Its *The fence a split leaves
  below is live* names both — each value written `${name:?}`, the lines
  chained with `&&` — and puts each read at or above the fence's first
  line that writes.
- **Three more fences held a placeholder beside commands.** The
  clean-export rebuild and the tag deletion were inert only by the parse
  error their own placeholder line raises; the recovery's `gh release
  create` was already behind the `${version:?}` at its chain's head.
  Each takes its value from a fence above now, and the recovery's notes
  placeholder names a file, which is what `--notes-file` takes.
- **The `mkdir` guard keeps the half of its paragraph the split leaves
  true.** `git worktree add` enters a pre-existing empty
  `/tmp/btclib-node-rebuild` exactly as a fresh one, so the guard is
  what refuses a leftover on a correctly filled paste. The `mkdir` is
  the fence's first line and it writes, so a `: "${version:?}"` heads
  the fence: a guard below the first writing line arrives too late.

### Every dependency is at the newest version this tree resolves to

`uv.lock` re-resolved with `uv lock --upgrade`, and
`.pre-commit-config.yaml` walked by `pre-commit autoupdate`. Only
strangers moved and nothing here had to catch up.

`pytest-randomly` is the one major, 4.1.0 to 5.0.0, and it is the entry
worth reading twice: it is what shuffles every run, so a break in it
does not look like a break in it. `pytest-order` runs under it as
`tests/README.md` says it does, and the suite is green -- but one green
run is one seed, which is weaker evidence for this package than for any
other, since it is the one that decided the order every other result
was measured in.

`btclib` moves with the branch `[tool.uv.sources]` names, and that is
the re-lock this repository absorbs deliberately rather than a stranger
moving. Two of the changes it carries could have reached this tree and
neither does. `Fetcher` gained a fourth abstract method,
`get_block_header`, which is a break for anyone subclassing that ABC --
nothing here does, the four files naming a fetcher all instantiating
`BitcoinCoreFetcher`, which btclib implemented on both its own backends.
And `psbt_signer_contract.optional_protocols` widened from a 2-tuple to
a 3-tuple, with the new element in the middle, so a caller indexing it
changed meaning silently -- `psbt_signer` is named nowhere in this
tree.

`bitcoin-core-rpc` moves in the lock, 2026.8.29 to 2026.9.3, and its
floor in the `test` group does not. That release carries `RPCErrorCode`,
which is what issue #801's last row needs and what this tree does not
call yet; the floor names the oldest release the code here works
against, and the commit that first reads that enum is the one that
raises it.

`zizmor` is the one rev held back, at v1.29.0, and `pre-commit
autoupdate` offered v1.30.0 again: it walks every `repo:` entry and has
no notion of a pin held on purpose, the reason for one living in a
comment it does not read. That hold arrives as a red run rather than as
a rejected line -- 1.30.0's new `self-repository` audit flags every
`uses: ./...` reference here and its auto-fix rewrites them to
`uses: $/...`, which actionlint 1.7.12 does not parse. The comment above
the rev says the bump waits on an actionlint release that understands
that syntax, and actionlint-py is still at v1.7.12.24.

`uv-pre-commit` reaches 0.12.9 and `[build-system]`'s `uv_build` range
stays as it is. That range is what the build has to satisfy rather than
a pin at the hook's rev, and the lock format the `uv-lock` hook writes
is unchanged.

### `anthropics/claude-code-action` is pinned at what `v1` names now

`claude-review.yml` pinned a sha from before `v1`'s own, in both of its
jobs. A pin behind a moving tag is a difference nothing chose: the sha
moves when somebody edits the workflow, so it drifts by however long
nobody has.

The tag is annotated, which is the part worth writing down. `gh api
.../git/ref/tags/v1` answers the **tag object's** sha, not the commit's,
and pinning that is a reference no runner can check out. `git/tags/<sha>`
dereferences it, and `refs/tags/v1^{}` does the same from a checkout.

### Mempool acceptance reads a candidate against relay policy

- **`interpreter.STANDARD_FLAGS` is Core's own
  `STANDARD_SCRIPT_VERIFY_FLAGS`** (`src/policy/policy.h:118-131`, at
  bitcoin/bitcoin@9be056a8a7), and `check_transaction` reads a mempool
  candidate against it in place of the consensus set `get_flags`
  answers (closes #810). Core's `PolicyScriptChecks`
  (`src/validation.cpp:1139-1160`, same commit) is that call; a spend
  breaking a standardness rule alone -- a non-minimal push, a high-s
  signature, a leftover stack element -- was one this node held in its
  mempool and Core refuses before its own second check is reached.
- **The set reads neither a height nor a block hash**, as Core's
  `constexpr` is, and `check_transaction` takes neither a `Node` nor an
  index: relay policy here is not gated on an activation height, and a
  tip that is one of the blocks
  `btclib.consensus.ConsensusParams.script_flags_at` exempts by hash
  cannot relax what this node will hold.
- **Core's `ConsensusScriptChecks` (`src/validation.cpp:1162-1193`,
  same commit) is not reproduced, and `check_transaction`'s docstring
  says what is left of it.** That call fills the script execution cache
  `ConnectBlock` reads later, where `check_transactions` here verifies
  every input of every block transaction whether the mempool held it or
  not; the rest of it is an assertion, which is why Core answers a
  failure there with `LogError("BUG! PLEASE REPORT THIS!")` under an
  `Assume(false)` rather than with a verdict on the transaction.
- **A candidate only a standardness rule refuses raises
  `NonStandardTxError`, and `p2p.callbacks.tx` drops the transaction
  rather than the peer that relayed it.** Core puts that rule above the
  set this one copies -- "we do not ban/disconnect nodes that forward
  txs violating the additional (non-mandatory) rules here, to improve
  forwards and backwards compatibility" (`src/policy/policy.h:112-117`,
  at bitcoin/bitcoin@9be056a8a7) -- where `p2p.main.handle_p2p`
  discourages a peer on `isinstance(e, BTClibException)`, which every
  refusal btclib's engine raises answers yes to. The class is a
  `BTClibValueError`, so both RPC paths answer a candidate refused this
  way through the clause they already have.
- **`interpreter._consensus_accepts` is what tells the two apart**, a
  second run of the engine under `ALL_FLAGS` and only where the
  candidate is already refused: btclib raises one class for a
  standardness rule and for a consensus one, so the flag set a refusal
  was produced under is all there is to read it by. A candidate no
  chain would carry reaches `handle_p2p` as the engine raised it, which
  discourages the peer for it.
- **`STANDARD_FLAGS` contains every set `script_flags_at` can answer**,
  over every network `btclib.consensus.CONSENSUS_PARAMS` names and over
  each of their by-hash exception rows. `ALL_FLAGS` contains every one
  of them too, which is what lets the classification above read a
  refusal without being told a height. Both are the claims the
  paragraphs above rest on, so they are tests rather than sentences.
- **One spend per rule the set adds, each accepted by the consensus
  flags and refused by these**, so a rule dropped from the set leaves
  exactly its own case red. `SIGPUSHONLY` is the `ScriptFlag` member
  the set leaves out, Core leaving it out of
  `STANDARD_SCRIPT_VERIFY_FLAGS` too, and a test names it so a member
  btclib adds later cannot be omitted here in silence.
- **`CONST_SCRIPTCODE` is in the set, and btclib reads it more widely
  than Core does.** `btclib.script.engine._check_script_sig_policy`
  refuses a signature check carried anywhere in the script_sig, where
  Core errors inside the executed branch its `FindAndDelete` reaches --
  that comment calling its own rule stricter in one direction and short
  in another.
- **`tests.anyone_can_spend` is the script_pub_key the suite's
  synthetic outputs carry.** A script that only pushes leaves the
  spender's own push standing beside its own, which CLEANSTACK refuses,
  so `generate_random_transaction` and `generate_coinbase` build an
  `OP_2DROP` and an `OP_1` over the random push that keeps two of them
  distinct.

### `rpc/errors.py` imports `bitcoin_core_rpc`'s `RPCErrorCode` (issue #801)

- **`rpc/errors.py` no longer defines its own error-code enum; every call
  site imports `RPCErrorCode` from `bitcoin_core_rpc` instead.** Checked
  member by member before the deletion: every name this tree used exists
  in the package's enum with the same integer value, so no code on the
  wire changes. The package transcribes the whole of Core's
  `RPCErrorCode` (`src/rpc/protocol.h`); this tree still raises only the
  subset it always has.
- **`RpcError`, `json_type_name`, `type_error`, `bool_param` and
  `error_msg` stay in `rpc/errors.py`.** `RpcError` is this node's own
  refusal exception, opposite in direction from `bitcoin_core_rpc`'s own
  `RpcError`, which reads an error a node's answer already carries.
- **`bitcoin-core-rpc` moves from the `test` group alone into
  `[project.dependencies]`, at `>=2026.9.3`, the release `RPCErrorCode`
  first ships in.** It was already importable at runtime through
  btclib's own dependency on it; the declaration is what makes that
  import provable rather than incidental now that `src/` reads it.

### `verify_mempool_acceptance` checks scripts after the cheap checks

- **`interpreter.check_transaction`, the script verification, now runs
  after `is_final` and `assert_sequence_locks` rather than before them**
  (closes #829), matching `MemPoolAccept::AcceptSingleTransactionInternal`,
  which runs `PreChecks` -- where Core's own finality and sequence-lock
  checks sit -- ahead of `PolicyScriptChecks`
  (`src/validation.cpp:1378`, at bitcoin/bitcoin@4519933391) for the
  same reason its own comment there gives: to spend no signature
  verification on a candidate a comparison of two integers already
  refuses. `assert_coinbase_maturity` already ran ahead of the script
  check and keeps its place; only the two lock checks moved.
- **A test asserts the order, not only the verdict**: a candidate both
  non-final and unverifiable is refused with `bad-txns-nonfinal` rather
  than with its script's own failure, which either order would raise as
  a `BTClibValueError`.

### A mempool tx failure costs the peer nothing, and a resend is not reverified

- **`p2p.callbacks.tx` catches every `BTClibValueError`
  `verify_mempool_acceptance` can raise, not only `NonStandardTxError`.**
  Core discourages no peer for a transaction failure of any kind --
  "Tx failures never trigger disconnections/bans ... either due to
  non-consensus relay policies ... or due to new consensus rules
  introduced in soft forks" (`src/validation.cpp:2112-2117`, at
  bitcoin/bitcoin@4519933391) -- and there is no `MaybePunishNodeForTx`,
  where `MaybePunishNodeForBlock` exists and is called. (closes #843)
- **`Mempool.mark_rejected` records a refused wtxid, cleared on the next
  connected block, so a resubmission is dropped before
  `interpreter.check_transaction` pays for its own two-flag-set
  verification a second time.** Core's own mitigation is
  `m_recent_rejects` plus `AlreadyHaveTx`, reset the same way on
  `ActiveTipChange`; `MissingPrevoutError` is exempt from this tree's
  cache, matching Core's identical `TX_MISSING_INPUTS` exemption, since a
  missing parent can arrive on its own with no block having to connect
  first. (closes #845)
- **Keyed on wtxid alone, the cache does not catch a resubmission whose
  witness was mutated**: a different trailing push draws a different
  wtxid and is verified again in full. Core's own filter carries the
  identical gap for the identical reason -- only its narrower
  `TX_INPUTS_NOT_STANDARD` case also records a txid, for a failure
  proven independent of the witness, and nothing in this tree's own
  single flag comparison tells that case apart from one that is not.

### `getblockchaininfo`'s `chain` member calls `bitcoin_core_rpc.chain_from_network`

- **`_CORE_CHAIN_NAMES` duplicated `chain_from_network`'s own table** --
  the BIP32/BIP173 network names `Chain.name` (`chains.py`) carries,
  against Core's own chain names -- and `chain_from_network` is exported
  at `bitcoin_core_rpc`'s own top level rather than being one of
  `chains.py`'s private tables. `getblockchaininfo`'s `chain` now calls
  it directly, and the local table is gone (closes #846).
- **The comment above the table named `chain_from_network` as btclib's
  own, and reasoned that `bitcoin-core-rpc` was not this node's own
  dependency.** Neither held: the function is `bitcoin_core_rpc.chains`'s,
  imported rather than defined by btclib's `fetch.bitcoin_core`, and
  `rpc/errors.py`'s own `RPCErrorCode` import (#801) had already made the
  package a direct dependency (closes #846).

### A Core citation names the tag where its sha is a release's

- **`CLAUDE.md`'s *Following Bitcoin Core* described one case, the sha a
  fast-forward of `master` lands on, and the tree cites shas that are
  not on `master`** (closes #850). It now states the second: where the
  claim is about the behaviour of the bitcoind
  `.github/workflows/integration-bitcoind.yml` pins rather than about
  Core's tip, the citation is at that release's tag commit and carries
  the tag's name beside the sha.
- **The tag's name carries the choice because ancestry cannot.** A sha
  read from an out-of-date `master` is an ancestor of `master` all the
  same, so `git merge-base --is-ancestor <sha> master` answering false
  establishes that a sha is a release's and never that its author meant
  one; `9be056a8a7`, cited here and in `src/`, answers false and is the
  commit `v31.1` tags.
- **`master` stays the default, and the subject is not what picks
  between the two.** The `master` shas this tree cites are for
  consensus and relay behaviour as much as the release ones are --
  `src/kernel/chainparams.cpp`, `validation.cpp` and
  `net_processing.cpp` are all cited at `master` -- so a rule reading
  "consensus and relay behaviour takes the release" would have
  contradicted the citations already landed rather than sorted them.
  What picks is whether the claim is about the Core this tree is run
  against.
- **The citations at `9be056a8a7` that do not name `v31.1` keep the
  form they landed in**, per section 9's *Nothing already written is
  rewritten*; the rule binds what is written next.

### Core's own source is cited at `master`, the pinned bitcoind at its tag

- **`exceptions.py`, `interpreter.py`, `interpreter_test.py` and
  `p2p/callbacks_test.py` read Core at `bitcoin/bitcoin@4519933391`,
  where they read it at `9be056a8a7`** (closes #853). `CLAUDE.md`'s
  *Following Bitcoin Core* reserves a release's tag commit for a claim
  about the bitcoind `.github/workflows/integration-bitcoind.yml` pins,
  and what these describe is Core's relay policy and its mempool
  acceptance path: production code and unit tests, which start no
  bitcoind at all.
- **`PolicyScriptChecks` is `src/validation.cpp:1129-1150` and
  `ConsensusScriptChecks` `:1152-1183` at that sha**, against
  `1139-1160` and `1162-1193` at `9be056a8a7`. Over those ranges the
  two shas differ by the `const ATMPArgs& args` parameter each function
  drops, which neither citation's prose reads.
- **The claims themselves hold at `9be056a8a7` as well.** Core's
  comment on not disconnecting a peer that forwards a
  non-mandatory-rule violation is `src/policy/policy.h:112-117` at both
  shas, byte for byte, and `STANDARD_SCRIPT_VERIFY_FLAGS` is `:118-131`
  at both, with the same members in the same order. Its declaration
  differs -- `static constexpr` at the release, `inline constexpr` at
  `master` -- which no citation of the set reads. So what the move
  corrects is which Core a claim is read against and not a falsehood.
- **`tests/integration/reorg_test.py` stays at `9be056a8a7`**: it hands
  blocks to the pinned bitcoind and asserts on what it answers, which
  is the case the tag commit is reserved for. `COINBASE_MATURITY`,
  `DEFAULT_MAX_TIP_AGE`, `UpdatedBlockTip`'s early return during
  initial block download, `bad-cb-length` and `submitblock`'s
  `"inconclusive"` all read the same at `master`;
  `include_dummy_extranonce` does not, `master` carrying no such option
  and appending the `OP_0` at `nHeight <= 16` outright, so the pin is
  what keeps that sentence true.
- **`9be056a8a7` is cited in this file and in
  `tests/integration/reorg_test.py`, the entry above having said this
  file and `src/`.** That entry keeps what it landed with, per section
  9's *Nothing already written is rewritten*, and this bullet is what
  serves the reader instead. The citations there that name no tag keep
  their form too: `reorg_test.py`'s `_COMMON` comment names `v31.1` and
  says the module runs against that release.

### The badge render greps a whole title element (closes btclib-org/.github#724)

- **`<title>` is the badge SVG's own element name and not a value a
  reader fills in**, so unquoting it -- what section 9 of the
  organization standard asks of a placeholder standing as a whole
  argument -- leaves the angle brackets as redirections and a line no
  shell will run. What the render takes instead is that bullet's other
  clause: a `<...>` inside a larger string is not the shape it refuses.
- **The pattern is the one section 15 of that standard already reads a
  badge's title with**, `grep -oE '<title>[^<]*</title>'`, so the line
  converges on the standard rather than on a form invented for it.
  Against this repository's `test.yml` badge both forms select the same
  `<title>` element, the one lacking the closing tag selecting it inside
  the whole matching line, so what the paragraph quotes the render for
  is unchanged.
- **The `unquoted-placeholder` hook cannot see the shape this entry
  corrects.** Its single-quote branch crosses a quoted run of the other
  kind only, and `'<workflow>/badge.svg?branch=main'` earlier on the
  line is of the same kind, which is the blind spot section 4 states at
  the hook. The port that would ordinarily surface such a line does not
  surface this one, which is why the fix is written here and the hook is
  not widened.
- **The paragraph is rewrapped around the longer pattern**, which also
  brings `-H 'Cache-Control: no-cache'` onto one line.

### A pre-commit check fails where the bitcoind pin and a Core citation disagree

- **Nothing tied `integration-bitcoind.yml`'s `version:` pin to
  `tests/integration/reorg_test.py`'s citations of that release, so a
  bump left them describing a bitcoind the suite no longer runs**
  (closes #856). A new local hook, `check_core_citation_pin.py`, now
  reads the pin and the module's own citations together and fails where
  either disagrees with the other, or where the module's citations
  disagree among themselves.
- **The same failure reaches the other sites that copy the release's
  full version number for a measurement claim rather than cite Core's
  source**: `integration-bitcoind.yml`'s own timing comment beside the
  pin, `tests/integration/conftest.py`'s startup measurement, and
  `src/btclib_node/rpc/errors.py`'s docstring -- a bump falsifies each
  of them exactly as it falsifies a citation.
- **The check reads this tree and nothing else**, so it cannot say a
  citation is still true of Core's source -- only a human re-reading
  Core at the new tag can say that. What it forces is that re-read
  wherever a bump goes on without it; it does not perform one.
- **A pygrep hook cannot ask whether one file's pin agrees with
  another's citation**, both being needed at once, so the hook is
  `language: system`, invoked with a bare `python3` the way
  `check_vendored_pin.py` already is rather than through `uv`, the
  script importing nothing beyond the standard library.

### CLAUDE.md's block-deletion check names where the block's size comes from

- **Deleting a computed-size block from the fused tip and asking
  whether the base returns is decisive only where the size is pinned
  to the branch's pre-rebase blob, not derived from `len(tip) -
  len(base)` at the two commits a session has once the rebase is
  done** -- the shortfall from an eaten seam blank does not fail the
  check, reproducing the base at several positions instead of none,
  and `git diff --numstat` against the pre-rebase base is what still
  discriminates once that blob is gone (closes #859).

### REVIEWING.md and RELEASING.md take section 9's placeholder rules literally

- **REVIEWING.md's two placeholders — `gh issue list --search` and `gh
  issue create --title`/`--body` — go bare** (issue
  btclib-org/.github#772): quoted, `gh issue create` takes both flags as
  literal text and files an issue titled with the placeholder itself,
  rather than failing at the shell before it reaches the tool.
- **RELEASING.md's `--subject <title>` loses its quoting and moves
  after `--body-file <path>`, which moves the position rule's guard onto
  the more expensive placeholder** (issue btclib-org/.github#775):
  `--body-file <path>` already ended the command bare, and `<title>` was
  the one left quoted and mid-command. A paste that filled in every
  other placeholder and left this one used to reach `gh` with the
  literal string `<title>` as the release commit's subject, where it now
  fails at the shell instead.
- **A trailing `#` comment inside a `shell` fence of a landed
  `CHANGELOG.md` entry is not swept** (issue btclib-org/.github#771):
  this file is append-only under `merge=union`, and a landed entry is
  the record of what was said when it landed, not a line this branch
  rewrites.

## v2026.8.27

### A functional test waits for the status it is about (closes #525)

- **`test_a_slow_manager_start_cannot_still_clobber_the_status_it_raced`
  waits on `node.status`, in place of waiting on the chain length and
  sampling the status after it** (closes #525): the two are separate
  writes on the node's own loop thread -- `update_chain` commits the
  fork, and `finish_sync` a few statements later moves the status --
  so the chain reaching its new length says nothing about whether the
  status has moved yet. The window is narrow and real on every
  interpreter; it was hit on
  [run 33092703719](https://github.com/btclib-org/btclib-node/actions/runs/33092703719),
  the free-threaded job of the release pull request, with the suite
  otherwise green and coverage at 100%.
- **The wait still fails where the status is genuinely clobbered**
  (closes #525), which is the regression #398 put the test there to
  catch: a clobber leaves the status below `HeaderSynced`, `_ready_fork`
  returns at its own first guard, `finish_sync` is never reached again
  and the wait runs out. What changes is the failure's shape, a
  `WaitTimeoutError` in place of an `AssertionError`, not what makes it
  fail.

### The retry wraps the resolution, not a probe of it (closes #548)

- **Both publish jobs retry the `uv run` itself, with `--refresh`, in
  place of #546's `curl` poll followed by a single attempt** (closes
  #548): the poll and the install were not served the same index. On
  [run 33091369402](https://github.com/btclib-org/btclib-node/actions/runs/33091369402)
  the wait reported `the index serves 2026.8.dev401` and uv failed on
  that version one second later. The file existed throughout -- its
  project page and JSON API both answered `200` -- and the simple API
  disagreed with itself by request: a plain GET still served a stale
  list minutes afterwards, while the same GET with `Cache-Control:
  no-cache` and a cachebust served the new version three times out of
  three.
- **What that cost was a check whose subject was not the operation it
  guarded** (closes #548): the step needs to know whether uv can
  resolve the version, and a `curl` GET answers a different question
  that can differ. Retrying the real command subsumes the probe, so the
  probe is gone rather than kept beside it -- a second check that can
  disagree with the first is a second thing to reason about.
  `--refresh` is the other half: the CDN is not the only cache in the
  path, uv keeping its own copy of an index response. Verified against
  the exact version the runner failed on, which resolves and imports
  with `--refresh`.

### The install check waits for the index (closes #546)

- **Both publish jobs poll the simple API for the version they just
  uploaded before installing it, twenty attempts fifteen seconds
  apart, erroring by name if it never arrives** (closes #546): the
  index does not serve a new file the instant the upload returns, and
  the step that installs from it ran 1.2 seconds later. On
  [run 33089825557](https://github.com/btclib-org/btclib-node/actions/runs/33089825557)
  TestPyPI answered `200 OK` for both files and printed the project
  URL; the resolver was told there was no such version; the index
  served it minutes afterwards. `publish-pypi` carried the identical
  shape, which on a tag push is the third distinct way these three
  lines produce a published-but-unattested release with no GitHub
  release page (#541, #543).
- **The wait asks the simple API, not the JSON one** (closes #546):
  the resolver reads the simple API, and #545 is the finding that JSON
  is a cache of the index where simple is its state -- a wait in front
  of a resolver asks what the resolver asks.
  `btclib-org/btclib`'s own `pypi-install.yml` carries the same wait
  against a failure this tree cannot have, its step installing whatever
  the resolver picks so that starting early tests the *previous*
  version and reports a pass for it. The `==$version` here makes that a
  loud failure rather than a quiet wrong answer, and the wait makes it
  neither. The match is exact on the quoted version, checked against
  the live index for a version present, one absent, and a prefix of a
  present one.

### The install check names its interpreter (closes #543)

- **Both publish jobs, and `RELEASING.md`'s two install checks, pass
  `--python 3.14` to the `uv run` that installs the published package
  and imports `Node`** (closes #543): `requires-python` here is
  `>=3.14` and `ubuntu-latest` defaults to 3.12, so uv resolved against
  an interpreter the published package excludes and reported the
  requirement unsatisfiable -- in a step that runs *after* the upload.
  On a tag push that is the shape #541 described: a version on PyPI, a
  filename that index never accepts twice, and `attest` and
  `github-release` skipped behind the failure.
- **The file was already internally inconsistent about it** (closes
  #543): *Rebuild a release from its tag* passes `--python 3.14` in both
  of its commands, and the two install checks did not -- which is to
  say the two commands that lacked it were exactly the two nobody had
  ever run. Measured on
  [run 33070323112](https://github.com/btclib-org/btclib-node/actions/runs/33070323112),
  the rehearsal #542's own fix first let reach this step, and the fix
  verified against TestPyPI's `2026.8.dev201` -- the artifact that run
  uploaded before dying -- by running the failing command locally at
  3.12, where it fails identically, and at 3.14, where it imports.

### The send bound is derived from the peak its pacing checks reach (closes #521)

- **The two pacing mechanisms' overshoots do not add, so
  `MAX_QUEUED_SEND_BYTES` (`p2p/connection.py`) is not their sum**:
  `advance_getdata` and `advance_cfilters` (`p2p/callbacks.py`) both
  pace on `Connection.queued_send_bytes` and neither reads anything
  else, so filters a connection already owes leave a `getdata` answer
  that much less room rather than adding to what that answer may commit;
  and `MAX_CFILTERS_INFLIGHT_BYTES` being the lower of the two bounds,
  `advance_cfilters` stops on its first check throughout a `getdata`
  overshoot. A `getcfilters` pipelined behind a `getdata` the peer has
  not drained is counted inside that answer's own peak rather than on
  top of it. The bound is written as `MAX_GETDATA_INFLIGHT_BYTES` and
  one block, and room above them; its value is unchanged, that bound
  being twice `MAX_PROTOCOL_MESSAGE_LENGTH`.
- **What the room above that peak is for is written down**: a sender
  that passes no pacing check commits its whole message on top of
  whatever the field already holds — the `notfound` closing a `getdata`
  answer, a transaction announcement's `inv`, a `headers`, an `addr` —
  and this room does not hold the largest of them, so a peer that has
  stopped draining can be dropped by a message no pacing check stands in
  front of, where the pacing bound would have paused an answer instead.
  Issue #529 is where that is measured and where pacing those senders is
  decided; raising this bound is not the answer to it, because
  `_send_due_announcements` (`download.py`) sends as many `MAX_INV_SZ`
  chunks in one pass as the mempool has entries to announce.
- **`tests/unit/p2p/connection_test.py` measures the displacement rather
  than assuming it**: a `getdata` answered on a connection that already
  owes a filter answer serves fewer blocks and carries no larger a
  total, driven through the real dispatch; and the bound is held above
  `MAX_GETDATA_INFLIGHT_BYTES` and one whole block message, the wire
  envelope measured off a `Message` built the way `Connection._queue`
  builds one. The boundary tests over that comparison say that is what
  they are, rather than that they are the maximum either mechanism
  reaches.

### Both publish jobs set up uv before running it (closes #541)

- **`publish-testpypi` and `publish-pypi` each gain a `Setup uv` step,
  ahead of the step that installs the published package from the index
  and imports `Node`** (closes #541): neither job had one, and the
  runner carries no uv. Measured rather than reasoned -- the first
  TestPyPI rehearsal
  ([run 33067874355](https://github.com/btclib-org/btclib-node/actions/runs/33067874355))
  uploaded successfully and then exited `127` on
  `uv: command not found`. On a tag push `publish-pypi` has the
  identical shape, so the release would have put a version on PyPI --
  a filename that index never accepts twice -- and then failed, taking
  `attest` and `github-release` down as skipped behind it: published,
  unattested, with no release page and no bill of materials, and
  recoverable only by yanking and cutting a patch version. These two
  jobs had never executed a step before today, `release.yml` having
  landed with #503 and nothing ever having been published here, so
  every belief about them was inference from reading the file.
- **The inline post-publish check says why it is inline** (closes
  #541): `btclib`, `btclib-secp256k1` and `bitcoin-core-rpc` each put
  theirs in a `pypi-install.yml` of its own, and each provisions a
  toolchain explicitly there. That file reads the index, so it has
  nothing to install until a release exists, which is the case this
  repository is in; btclib-org/.github#488 is where the standard's own
  silence on the question is being settled, and #502 is where moving
  it becomes possible. Without the comment the next reader moves this
  onto the three-tree shape and loses what the inline step was for.

### RELEASING.md names `public-api`, and audits the run (closes #538)

- **A *What a red `public-api` means* section, and a numbered post-tag
  step that audits the run job by job** (closes #538): the job has been
  in `release.yml` since #503 and the procedure never named it, though
  it is the one job in the pipeline designed to exit 1 on an ordinary
  cycle -- any public-API difference since the last release. What the
  section says is that a red one is read rather than obeyed, each
  finding checked by hand against `RELEASE_NOTES.md`'s own *Breaking
  changes* list, the question being whether a break is announced rather
  than whether it exists.
- **The audit step looks for `skipped` with zero steps, not for red**
  (closes #538): a failed job is loud and a skipped one is silent.
  `btclib-org/btclib`'s own `v2026.8.27` published with its
  post-publish sentinel never having run and the run reading as done
  (btclib-org/btclib#1470, btclib-org/.github#484). The step carries the
  `gh api` call that lists every job with its step count, and names the
  one skip that is correct and would otherwise be cited as the defect:
  `publish-testpypi` on a tag push, its guard being `workflow_dispatch`.
  Neither gap can bite this repository's first release -- `public-api`
  resolves no previous tag and cannot fail, and there is no
  post-publish job here yet (#502) -- which is the argument for writing
  both now rather than after: a post-tag audit that lands after the tag
  documents a check nobody ran on the release it was written for.

### A reorg reaches this node from a real bitcoind (closes #513)

- **`tests/integration/reorg_test.py` submits a competing branch to the
  regtest bitcoind the node is already synced against, and holds the
  node to the tip Core switches to** (closes #513): `update_chain` and
  `_reconcile_mempool_for_reorg` (`btclib_node/main.py`) are otherwise
  driven by tests that hand this node both branches directly, on the
  thread that built them, which says nothing about a reorg arriving
  over p2p from an implementation this tree did not write. A reorg is
  where the block index, the UTXO set, the filter index and the mempool
  have to move backwards together, and where a disagreement with Core
  is a chain split rather than a slow peer.
- **The reorg is asserted and not assumed**: the node is held to the
  abandoned branch's own tip before the competing branch is built at
  all, the abandoned block is looked up in the block index afterwards
  and found off the active chain at `BlockStatus.valid`, and the
  transaction that branch confirmed is looked for in the mempool.
  Stubbing `_reconcile_mempool_for_reorg` out leaves the first two
  passing and fails the third, which is what says the mempool half is
  carried by that function rather than by the sync.
- **The mempool is waited for where the block index is read straight
  off**: `update_chain` commits the new tip in `_finalize_fork` and
  reconciles the mempool only after it returns, so the wait that sees
  the tip move can still see the transaction outside the mempool.
  Delaying `_reconcile_mempool_for_reorg` fails a bare read there and
  leaves the wait passing, which is what says the order is the node's
  and not the test's timing.
- **The chain is dated backwards from the clock rather than from the
  regtest genesis** `backpressure_test.py` counts from: Core relays no
  inventory while it holds itself to be in initial block download
  (`PeerManagerImpl::UpdatedBlockTip`, `src/net_processing.cpp`) and
  leaves that state only once its own tip is within
  `DEFAULT_MAX_TIP_AGE` of the clock
  (`src/kernel/chainstatemanager_opts.h`), so a chain dated from the
  genesis is one Core accepts and never announces. The modules that
  sync rather than wait to be told do not notice.
- **The branch carries a transaction, which costs the chain Core's own
  coinbase maturity**: the only thing a chain built from nothing has to
  spend is a coinbase, and `COINBASE_MATURITY`
  (`src/consensus/consensus.h`) is a constant rather than a chain
  parameter, so regtest does not relax it. What the blocks cost at that
  height is nearly nothing, each being a coinbase and at most one other
  transaction: both branches together cost less than the megabyte chain
  `backpressure_test.py` hands over, which is why this module is in the
  gate's own workflow rather than reserved for a sentinel.

### The two publish jobs stop gating on `public-api`'s result (closes #534)

- **Both jobs' `if:` now opens with `always()` and reads
  `needs.test.result`, `needs.lint.result` and `needs.docs.result`
  explicitly, `public-api` staying in `needs:` for ordering only**
  (closes #534): `public-api`'s own comment calls it deliberately not a
  merge gate, exiting 1 on any public-API difference since the last
  release with no matching `RELEASE_NOTES.md` entry -- its documented
  behaviour on a real breaking-changes cycle, not a crash. A bare
  `needs:` gates on every listed job regardless of why it failed, a
  skipped job counting the same as a failed one, so that designed
  failure would have kept both publish jobs from ever starting.
  `btclib-org/btclib#1461` is where the identical shape did exactly
  that, on the first cycle to run the job against a real breaking
  change; this repository's own `release.yml` carried the same bug and
  had never run `public-api` against one, no release having been cut
  yet. `attest` and `github-release`, two jobs further down the same
  file, already carried this pattern correctly.

### The btclib floor carries `p2p.negotiation` (closes #381)

- **`btclib[secp256k1]>=2026.8.27` in place of `>=2026.8.21`, and
  `test.yml`'s `dist` job installs the built wheel with no override and
  imports `Node`, in place of `--no-deps` and a metadata-only read**
  (closes #381): `src/btclib_node/download.py` imports
  `btclib.p2p.negotiation.FeeFilter` unconditionally, and no PyPI
  release before `2026.8.27` carried that module, so an ordinary `pip
  install btclib-node` could not resolve regardless of the floor
  declared — the smoke test was narrowed to what it could honestly
  assert without a released btclib rather than block on a gap release
  machinery could not close. Verified against the index rather than
  assumed: installing `2026.8.27` in isolation and importing every
  btclib module `src/` uses found none missing, and installing the
  built wheel the same way and importing `Node` succeeded.

### A bare run collects the integration directory (closes #508)

- **`tests/integration` is a `testpaths` entry** (closes #508): section
  7 of the organization standard puts every suite directory there, so
  that a bare `uv run pytest` is still the whole suite, and attaches to
  an integration directory the conditions that make that affordable --
  each test skipping itself where the environment switch that asks for
  it is unset, the switch named in the skip message, and the directory
  kept out of the coverage ratchet. `tests/integration/conftest.py` and
  `[tool.coverage.run]`'s `omit` already held all of those, so the entry
  was what was left. What a contributor without a bitcoind sees is
  `set BTCLIB_NODE_INTEGRATION=1 to run the integration tests` against
  each test there, rather than a directory a bare run never mentions --
  and not the divergence from the standard the alternative would have
  owed an issue.
- **The skips do not cost the run its green**: the summary bar stays
  green with the skipped count sitting beside the passed one, and the
  run exits 0. That is the whole of what collecting the directory costs.
- **`integration-bitcoind.yml`'s own invocation is a selection, so the
  floor does not apply to it**: the workflow names `tests/integration`
  alone, which leaves the rest of `testpaths` out, and
  `tests/conftest.py`'s `relax_coverage_floor` drops the threshold for
  it. Asking for `--cov-fail-under=100` on that same command exits 1
  where the command the workflow runs exits 0, which is what says the
  drop is what carries it rather than an absent floor.

### A peer's malformed `reject` is refused as the peer's own fault (closes #515)

- **`Reject.parse` refuses a payload with `InvalidRejectPayloadError`, a
  `BTClibValueError`**: `handle_p2p` (`p2p/main.py`) discourages the
  peer where the exception is a `BTClibException` and reads anything
  else as this node's own code failing on content that was fine, so a
  `message` or a `reason` no utf-8 decodes and a code outside BIP61's
  own tables belong inside that family. `WrongNetworkMagicError` is the
  same decision one layer out, over the envelope rather than the
  payload, and its docstring already carries the reasoning.
- **`parse` accepts exactly what `serialize` writes**: every field is
  held to the length its own prefix declares, `BytesIO.read` answering
  a stream that has run out with what is left rather than with an
  error, and what follows `reason` is either the 32 octets of a hash or
  nothing at all — BIP61 ending a version reject after the common
  payload and having a tx or block reject append the hash of what was
  rejected. A payload cut short mid-field, a hash cut short, and octets
  past a whole one are each refused rather than parsed into an object
  the peer did not send.
- **`fuzz/fuzz_reject.py` suppresses `BTClibException` alone**: that
  being the whole of what the parser refuses an input with, an input it
  refuses any other way leaves the harness, which is what gives
  `fuzz.yml`'s scheduled row something to report.

### The fuzz sentinel, over the parser a peer reaches first (closes #402)

- **`fuzz/fuzz_reject.py` fuzzes BIP61's `reject` payload parser, and
  `fuzz.yml` runs it on the calendar row section 10 of
  btclib-org/.github's README gives `fuzz`**: the property that section
  keys the sentinel on is that nobody stands between a parser and an
  adversary choosing the octets, and `p2p.callbacks.reject` reads what a
  peer sent with no verification of any kind in front of it.
  `Reject.parse` is what this tree owns of that surface: the rest of
  what a peer's octets reach here is `btclib`'s codec, fuzzed by that
  repository's own harnesses, or a method over a connection that owns a
  socket, a manager and a node — which is issue #516 and not this
  change.
- **Atheris runs as an ordinary script rather than under
  ClusterFuzzLite**, which is what `btclib` runs and would otherwise be
  the port: that toolchain builds targets inside
  `gcr.io/oss-fuzz-base/base-builder-python`, whose interpreter is the
  `ENV PYTHON_VERSION 3.11.13` of google/oss-fuzz's own
  `infra/base-images/base-builder/Dockerfile`, where `requires-python`
  here is `>=3.14` — so the `pip3 install .` such a build begins with is
  refused before a target is compiled. Atheris itself admits this tree:
  the `fuzz` dependency group resolves its cp314 manylinux wheel, under
  the platform marker its own wheel list forces.
- **The seeds are a starting point and not a regression suite**:
  `tests/fuzz_corpus_test.py` holds every file under `fuzz/corpus/` to
  parsing and reserializing to itself, and says why an input a crash was
  found on belongs in the ordinary suite instead — a hardened parser
  refuses it, which is the opposite of what that module asserts. What
  the test also asks is that a harness's declared entry point still
  resolves, so a harness aimed at a name this tree has renamed fails on
  the pull request that renamed it rather than on the sentinel's day.
- **`Reject.parse` refuses a peer's own malformed payload with
  `ValueError` where `handle_p2p` sorts a peer's fault from this node's
  by `BTClibException`** (issue #515): the harness suppresses both
  families and cites that issue, which is what keeps the sentinel about
  the crashes nobody has described rather than about a classification
  already known and filed.

### A pacing check counts what it has handed over (closes #512)

- **`Connection.send` frames a message and counts it against
  `queued_send_bytes` on the calling thread, and schedules only the
  write** (closes #512): `advance_getdata` and `advance_cfilters`
  (`p2p/callbacks.py`) pace an answer by reading that field between two
  items, and both run on `Node`'s thread. Counted where the write
  happens instead, the field says nothing about the items the same loop
  has just handed over, so a `getdata` answer runs as far past
  `MAX_GETDATA_INFLIGHT_BYTES` as the loop is behind — far enough, for
  blocks of the size a peer in initial block download asks for, to
  spend the room `MAX_QUEUED_SEND_BYTES` leaves above that bound and
  reach the drop. A peer merely slow to drain is dropped that way for
  asking for the blocks this node asks its own peers for, which is the
  outcome `MAX_QUEUED_RECV_BYTES`'s own comment names as the wrong one
  for a flood-control case. Counted at the hand-off, the room
  `MAX_QUEUED_SEND_BYTES` leaves above each pacing bound holds what a
  check made before its own send can put past it, which is what that
  bound's own comment already says it is sized for.
- **Serializing a message is the caller's cost rather than the loop's**
  (closes #512): the thread that asks for a block has already parsed
  that block out of `block_db` to build the payload, and how much of
  that it does in one pass is what the pacing bound bounds. The
  asyncio loop, shared by every connection, no longer serializes a
  block between two socket reads.
- **`queued_send_bytes` is guarded by a lock, the way `queued_recv_bytes`
  already is** (closes #512): both `Node`'s thread and `P2pManager`'s
  reach `Connection.send`, and the write's own completion decrements
  from the loop. The two directions carry counters of one shape —
  each incremented by the thread that hands the work over, before it
  is offered anywhere else — so a reader who has met one has met the
  other. What differs is only the granularity each is checked at: a
  send is weighed one message at a time, where `parse_messages`
  accumulates a whole pass and weighs it once at the end.

### Every backpressure bound is watched doing its job (closes #490, #492)

- **`tests/functional/p2p/backpressure_test.py` drives the send-side
  bounds against a peer on a real socket that completes the handshake
  and then never reads again** (closes #492): a `getdata` larger than
  the send queue leaves the rest on `node.pending_getdata` with the
  connection still `Connected`, which is what says
  `MAX_GETDATA_INFLIGHT_BYTES` engaged — `advance_getdata` has no other
  way out with items still to serve; a `getcfilters` reaching a
  connection already that far behind is parked whole against
  `MAX_CFILTERS_INFLIGHT_BYTES`; and what reaches `Connection.send`
  with no pacing point in front of it is refused at
  `MAX_QUEUED_SEND_BYTES`, the connection stopped and
  `queued_send_bytes` never past the bound. A bitcoind cannot be the
  peer for any of the three, a well-behaved daemon always reading, so
  this half wants a synthetic peer and no daemon at all.
- **`tests/integration/backpressure_test.py` puts a real bitcoind
  behind `MAX_QUEUED_RECV_BYTES`, serving megabyte blocks faster than
  this node validates them** (closes #492): the pause is counted on the
  loop's own thread, by an event that records being cleared, rather
  than by a poll that would have to catch a pause lasting
  milliseconds — and the node still reaches bitcoind's own tip, which
  is what separates a pause from a stall. Its chain is built in the
  test and handed to bitcoind through `submitblock`: what a wallet can
  put in one block is bounded by mempool policy, so a megabyte of it
  costs hundreds of transactions and a coinbase maturity to fund them,
  where a coinbase paying one large unspendable output is a megabyte on
  its own and needs no policy relaxed on the command line.
  `integration-bitcoind.yml`'s header names the second question the
  directory now asks. The initial-block-download rehearsal the issue
  sketched around this one -- a reorg announced to this node by Core
  rather than handed to it in process -- is its own question and is
  filed as one (issue #513).
- **Both halves run in the gate rather than on a weekly sentinel**
  (closes #492): the issue reserved that question and argued the other
  way, on the ground that reaching bounds this size means mining
  regtest blocks and moving tens of megabytes. Built rather than mined,
  the fixtures cost seconds, so the cadence a sentinel buys is not
  worth the delay it also buys — a bound that stops engaging is found
  by the pull request that broke it rather than by the following
  Sunday. Nothing in the weekly calendar changes, and neither does
  which workflows this tree owes.
- **`Node._drain_message_queues`'s docstring says what one shared queue
  costs a connection paused on `MAX_QUEUED_RECV_BYTES`, and
  `tests/unit/init_test.py` measures it in passes of that loop**
  (closes #490): the items another peer has ahead of a paused
  connection grow with the number of peers currently busy, while the
  share popped per pass grows only with the log of the whole backlog,
  so the wait is a function of how many peers are busy rather than the
  constant Core's own per-peer round gives — but it grows more slowly
  than their number, and it is bounded, each connection's own
  contribution being capped by that same bound. That is the
  measurement `MAX_QUEUED_RECV_BYTES`'s own comment said it was
  deciding without, and it does not ask for a bound of the paused
  connection's own.

### The two publishing environments exist, and the files say so (closes #509)

- **`REPOSITORY.md` gains *The two publishing environments*, recording
  the `pypi` and `testpypi` pair by the calls that read it back, and
  `RELEASING.md`'s *One-time setup* and `CONTRIBUTING.md`'s *A release
  path, and nothing published on it yet* stop naming the pair as
  missing** (closes #509): both were created with `fametrano` as the
  required reviewer and self-review left on — the maintainer who pushes
  the tag is the reviewer — and `pypi` restricted to `v*` tags. What the
  new section argues rather than states is why the count is read back at
  all: an environment a workflow names and the settings do not carry is
  created by GitHub at the first deployment that references it, with no
  protection rules, so the pair being absent would not have failed a
  release for want of a gate — it would have published without asking
  anybody. `REPOSITORY.md`'s *What is not configured, and why* keeps
  only what is still not configured, which is the release itself, and
  `RELEASING.md`'s cross-reference to that bullet now uses the title the
  bullet carries.
- **`REPOSITORY.md`'s *Token permissions* names the four jobs of
  `release.yml` that elevate past `contents: read`** (closes #509): the
  section ended "Nothing here publishes, attests, or writes to the
  repository's contents", which that workflow has contradicted since it
  landed — `publish-pypi` and `publish-testpypi` hold `id-token: write`,
  `attest` holds `attestations: write` beside its own, and
  `github-release` holds `contents: write`.

### The declared version is a calendar version (closes #504)

- **`pyproject.toml` declares `2026.8`, the shape `RELEASING.md`'s
  calendar scheme takes between releases, in place of the `0.1.0` that
  predates this tree's release path** (closes #504): `0.1.0` is a
  version this repository has already tagged — a lightweight tag from
  2023 with a release page of its own and nothing published from it — so
  a checkout of `main` reported itself as the prototype that tag names,
  which `release.yml`'s own `public-api` job already reads as not a
  release, excluding `v0.1.0` there by name. The month alone cannot be
  mistaken for a release either: `version-check` refuses a
  two-component version on a tag. What the first release does to the
  version is now add the day, rather than adopt a scheme for the first
  time on the one day every step of `RELEASING.md` is also running for
  the first time, ending in an upload that cannot be undone.
  `RELEASING.md`, `CONTRIBUTING.md` and `RELEASE_NOTES.md` no longer
  say the declared version is `0.1.0`, and `uv.lock` carries the
  project version too, so it is re-locked. Nothing is published by any
  of this: both this file and `RELEASE_NOTES.md` still open under
  `## Unreleased`, and there is still no release.

### `codeql.yml` runs on a pull request and reports one context (issue #402)

- **`codeql.yml` gains the `pull_request` trigger and an aggregate job
  named `codeql: every job passed`** (issue #402, under
  btclib-org/.github#349 and btclib-org/.github#459): the analysis ran
  on `main` after a merge, on a commit that is already the default
  branch, and the `analyze` matrix produced a context per language, so
  no branch rule had a single stable name to hold. The two land
  together because either alone is inert — an aggregate with no
  pull-request trigger produces no context on the run a pull request
  has, which is what btclib-org/bitcoin-core-rpc#233 was about, and the
  shape here is ported from that tree's `dee71fe8`. The header paragraph arguing
  against the trigger goes with it, as does the concurrency comment
  reading `github.ref` as the whole of what groups a run; the group is
  now the pull request's own number where there is one. `REPOSITORY.md`
  drops `codeql.yml` from the workflows that must not become required
  checks, and the `contexts` array still does not name it: requiring the
  check is a repository setting and no part of this change.

### Shared test code moves into the package (issue btclib-org/.github#371)

- **`tests/helpers.py` is gone, and what it held is in
  `tests/__init__.py`** (issue btclib-org/.github#371): section 7 of the
  organization standard puts shared test code in a package
  `__init__.py`, never in a module whose name says "test" and holds
  none, and `tests/helpers.py` was named neither `*_test.py` nor either
  of the two names `name-tests-test` excepts. `.pre-commit-config.yaml`
  drops the `exclude` that held that hook off the one file it would have
  refused, so the hook now reads every file under `tests/`. Each
  importer moves from `from tests.helpers import` to `from tests
  import`; `tests/unit/helpers_test.py` keeps its name, testing the same
  functions from where they now live.

### The interpreter declarations are compared (issue btclib-org/.github#365)

- **`tests/interpreters_test.py` compares `requires-python`, the
  per-version classifiers, `.python-version` and every interpreter a
  workflow or composite action names** (issue btclib-org/.github#365):
  section 15 of the organization standard asks a library for the module
  that keeps its own declarations in step, and this is ported from
  `btclib`'s copy. It reads every CI file rather than the platform
  sweeps alone, this tree's window being one version wide and written
  out literally in each of them, and it parses `pyproject.toml` with
  `tomllib` where the sibling reads it with a regex — that sibling's
  floor being below the version `tomllib` arrives in and this tree's
  not. `[tool.pytest.ini_options] testpaths` names the module, which
  sits beside `tests/unit` and `tests/functional` rather than under
  either, and `[tool.coverage.run] source` covers all of `tests/`, so a
  module nothing collects is a file the floor reports at zero.
- **The window itself is unchanged**: the standard gives a tier-1
  repository the library window and this tree declares an application's,
  which is the first of that issue's boxes and is issue #507 here — the
  package does not import below 3.14, PEP 649's lazy annotations being
  what lets its modules annotate with `TYPE_CHECKING`-only names, so the
  floor moves with the work that makes the claim true rather than ahead
  of it.

### The lint gate selects every family ruff ships (issue #402)

- **`[tool.ruff.lint]` selects `ALL`, with every declined rule in
  `ignore` carrying its reason** (issue #402, under
  btclib-org/.github#334): a hand-picked list rots, where `ALL` takes a
  new family in on the pull request that bumps ruff's own pinned rev,
  which is section 5 of the organization standard. What the switch
  surfaced is answered where it sits: the formatter-conflict rules in
  `ignore` with the vendor's citation, whitebox `SLF001`/`EM101` under
  `tests/**` in `per-file-ignores`, `INP001` for the entry-point
  directories nothing imports, and a `# noqa` with its reason at each
  `src/btclib_node/` site that reaches another object's private state
  on purpose.
- **`[tool.ruff.lint.pydocstyle]` declares `convention = "pep257"`**,
  the btclib-node half of btclib-org/.github#177, bbt's half still
  owed there: the convention settles the two rule pairs ruff warns
  about as incompatible, and it turns `docstring-starts-with-this`
  off — a rule the select list had named on its own, and one the tree
  has no finding under either way.
- **`tests/functional/` and `tests/unit/p2p/` are packages**: each was
  the one directory on its path without an `__init__.py`, so the
  modules under them read as an implicit namespace package (`INP001`)
  while their siblings did not.
- **`rpc/errors.py`'s `type_error` docstring is raw**: it quotes Core's
  own `"Wrong type passed:\n%s"` format string, and a raw docstring
  carries that backslash without an escape sequence (`D301`).

### Badges, a scorecard sentinel, and a release-path API check (issue #402)

- **`README.md` opens with the badge row section 2 of the organization
  standard derives from what the tree is** (issue #402, under
  btclib-org/.github#338): the index and forge badges, the gate
  workflows and pre-commit.ci, then one badge per sentinel in section
  10's calendar order. The index badges answer "not found" until a
  first release reaches PyPI, which is datable rather than a defect;
  there is no Read the Docs badge, no project there being connected
  (`REPOSITORY.md`).
- **`.github/workflows/scorecard.yml` runs the OpenSSF Scorecard
  weekly** (issue #402, under btclib-org/.github#339): Saturday hour
  03 at this repository's minute, ported from btclib's copy, its
  triggers the action's own rather than section 10's general rule.
- **`release.yml` gains a `public-api` job: `griffe check` against the
  previous released `v*` tag** (issue #402, under
  btclib-org/.github#326): both publish jobs need it, so a break in the
  public surface is refused while `RELEASE_NOTES.md` is being written
  rather than found by a caller. `v0.1.0` is excluded as the prototype
  tag it is, so the first release resolves no previous tag and the
  check skips itself, the workflow's own comment carrying the reason.
  The header sentence claiming `integration-bitcoind.yml` is not in
  this tree goes with it, that workflow existing and gating every pull
  request as a required check.

### REVIEWING.md converges with the organization's copy (issue #402)

- **The shared half of `REVIEWING.md` — everything above `## This
  repository in particular` — is byte for byte btclib-org/.github's**
  (issue #402, under btclib-org/.github#353): the wording this copy
  held predates the ack of record becoming a forge review, and
  convergence rather than the one measured sentence is what also
  covers the drift nobody measured.

### `asks_for_everything` resolves `testpaths` like `file_or_dir` (closes #496)

- **`tests/conftest.py`'s `asks_for_everything` now resolves `wanted`,
  built from `testpaths`, the same way it already resolved `given`**
  (closes #496): `config.rootpath` is built with `os.path.abspath`,
  which leaves a symlink in the path alone, where `Path.resolve` on the
  command-line paths follows one, so a rootdir reached through a symlink
  made the two sides incomparable, read the whole suite as a subset of
  itself, and relaxed the coverage floor on the run it exists to gate.
  `tests/unit/coverage_floor_test.py` pins the case through a real
  symlinked directory built in `tmp_path`.

### `__all__` covers the package, and the public surface is tested (closes #497)

- **Every module and package under `src/btclib_node/` now declares
  `__all__`, and `tests/unit/all_test.py` asserts nothing public is
  missing from one** (closes #497): `py.typed` ships and this package is
  published, so section 7 of the organization standard does not let its
  escape clause reach the public-surface bullet the way it reaches the
  other seven, and nothing before this walked the tree to check that
  every module declares its own list. `tests/README.md`'s table and its
  "Not tested here" line move the public surface into what is declared
  tested, and `tests/unit/all_test.py` is ported from `btclib`'s own
  `tests/all_test.py`, named as the precedent it takes its shape from.

### `tests/README.md` declares section 7's convention-test bullets (closes #488)

- **`tests/README.md` names which of section 7's convention-test bullets
  this repository tests and the module that tests each, and
  `tests/unit/conventions_test.py` asserts the declaration is true**
  (closes #488): section 7 of the organization standard says which of
  these bullets a repository implements is declared, not inferred, and
  an absent declaration made an absent convention test indistinguishable
  from a convention this tree does not have.

### A mutation from outside the runner never reaches a worker (closes #477)

- **CLAUDE.md's *Non-obvious facts* names two more ways a green pytest
  run means something other than it appears to** (closes #477): a
  mutation applied by monkeypatching an attribute in the controlling
  process, before calling `pytest.main`, never reaches the worker
  subprocess `-n auto` runs the test in, and the guarded test passes
  against the original, unmutated code; editing the file and reverting
  it afterward is the form that reaches a worker, since a worker reads
  the file. A `.venv` reused from another worktree carries the same
  hazard from a different mechanism: its `btclib_node.pth` is a plain
  absolute path fixed at `uv sync` time, so an interpreter run from a
  different `cwd` still imports the other worktree's `src/`, unmutated.
  `REVIEWING.md`'s *This repository in particular* asks a reviewer to
  check which form a mutation took, and `CONTRIBUTING.md`'s *Mutation
  testing* notes that `cosmic-ray`'s own sweep already takes the form
  that works, writing each mutation into the file rather than into a
  running process.

### CLAUDE.md says what pytest collects (closes #483)

- **The `python_files` bullet named an override `pyproject.toml` does
  not carry** (closes #483): collection under `tests/unit` and
  `tests/functional` follows pytest's own default (`test_*.py`,
  `*_test.py`).

### A repeat `version` ahead of `verack` is ignored, not answered again

- **`callbacks.version` now ignores a second `version` from the same
  peer ahead of its own `verack`, and `Connection.parse_messages` now
  paces a handshake command's own wire size the same way it already
  paced an ordinary message's** (closes #482): `handle_p2p_handshake`
  never moved a connection's own `status` off `Open` on a repeat, so a
  peer withholding `verack` could resend `version` as fast as this
  node's read loop parsed it, each one answered again with
  `WtxidRelay`, `SendAddrV2` and `Verack` and queued onto
  `handshake_messages` with nothing pausing that connection's own
  reads. Core's own guard, `pfrom.nVersion != 0`
  (`net_processing.cpp`, at bitcoin/bitcoin@5f45583e43), ignores a
  repeat outright instead -- no reply, no discouragement -- which is
  what this callback now does too, and `handshake_messages` now shares
  `MAX_QUEUED_RECV_BYTES` with `messages`: the earlier fix (#462)
  scoped that pacing away from this queue on the reasoning that a full
  drain every pass bounds how long a backlog persists, which says
  nothing about how large one pass's own backlog can grow before it
  drains.

### `P2pManager.messages`/`handshake_messages` name why an unlocked deque is safe

- **The comment beside `P2pManager.messages`/`handshake_messages` now
  names the mechanism that makes appending from one thread and popping
  from another safe with neither locked** (closes #484): `deque.append`,
  `.appendleft` and `.popleft` are each wrapped in their own
  `Py_BEGIN_CRITICAL_SECTION`/`Py_END_CRITICAL_SECTION`
  (`Modules/_collectionsmodule.c` and its clinic-generated wrapper, at
  python/cpython@f54fd2ab6e) -- CPython's own per-object lock under a
  free-threaded build, and a no-op under the ordinary GIL one
  (`Include/critical_section.h`'s own "no-ops in non-free-threaded
  builds"). The only argument on record before this was an empirical
  stress test in a coder's own report to the human, never landed prose.

### The `typos` hook is `repo: local`, pinned through `additional_dependencies`

- **`.pre-commit-config.yaml`'s `typos` entry no longer mirrors
  `crate-ci/typos`** (closes btclib-org/.github#399): `local` and `meta`
  are the only two `repo:` values `pre-commit autoupdate` filters out
  before it walks a config's `repos:` list, so a mirrored `typos` entry
  is what a scheduled autoupdate can move onto `crate-ci/typos`'s own
  moving `v1` alias, past the `pinned-rev` guard that only reads
  `rev:`. The `repo: local` hook carries the pin itself, in
  `additional_dependencies: [typos==1.49.0]`, and `language`, `entry`,
  `args` and `types` are upstream's own typos hook definition, copied in
  rather than fetched — `args` was already named explicitly in the
  mirrored entry, for the reason its own comment gave, and the rest the
  mirror took from the manifest. The block carries
  `stages: [pre-commit, pre-merge-commit, pre-push, manual]` too:
  a `repo: local` hook inherits no stage restriction from a manifest, so
  leaving `stages:` out would run the hook at `commit-msg` too, where its
  own `--write-changes` rewrites the commit message being typed.

### `.gitignore`'s `docs/_build/` entry is gone

- **`build/` already covers `docs/build/html`, the directory
  `CONTRIBUTING.md`'s documented `sphinx-build` command writes**
  (closes btclib-org/.github#411): `docs/_build/` is the stock
  GitHub Python template's Sphinx section, and no command in this
  repository writes there — `docs/` carries no `Makefile` or `make.bat`
  of its own, sphinx's own default output directory being reachable only
  by a command `CONTRIBUTING.md` does not name.

### `Regtest against Bitcoin Core` is a required check on `main`

- **`integration-bitcoind.yml`'s own job now blocks a merge rather than
  only reporting**: the workflow landed with #374 gating every pull
  request, but whether a red run stops anything is a repository setting
  and not a file, so it needed the `gh api` PATCH `REPOSITORY.md`
  already carried as its own follow-up. The context is the job's
  `name:`, held outside the tree as a literal string nothing here can
  keep in step, so that file now says what renaming the job would cost
  and in which order the two changes go.
- **`CONTRIBUTING.md`'s *What gates a merge, and what only reports*
  named three required checks and the workflow's own header still said
  making it gate a merge was a change for later**: both are the same
  fact recorded in a second place, and both are corrected here.

### A Bitcoin Core citation reads `at bitcoin/bitcoin@<sha>` (closes #471)

- **An identifier directly followed by a parenthesised list ending in
  `bitcoin/bitcoin@<sha>` parses as a Python call, which is what ruff's
  `ERA001` reads as commented-out code** (closes #471): every citation
  in a `#` comment across `src/` and `tests/` now has `at` glued to the
  sha on the same physical line, which cannot parse for a structural
  reason rather than an accidental one -- `at` and the citation's own
  leading word are two consecutive names with no operator between them,
  whatever text surrounds them or however the comment wraps. A citation
  inside a docstring needs none of this, since `ERA001` only ever walks
  comment ranges. `CLAUDE.md`'s *Following Bitcoin Core* states the
  shape.

### The self-connect nonce moves onto the connection, off the ring (closes #448)

- **`callbacks.version` now asks `P2pManager.is_self_connect_nonce`, which
  walks `pending_outbound_nonces` -- a set that shrinks exactly as an
  outbound connection completes its handshake or closes -- in place of
  `manager.nonces`'s own fixed-size ring** (closes #448): the ring was a
  process-wide list of the ten most recently sent nonces, so a burst of
  outbound connects could evict a still-outstanding attempt's own nonce
  before its `version` came back, a false negative on a genuine
  self-connection. `Connection.send_version` now records the nonce it
  drew on the connection itself and, only for an outbound connection,
  in that set through `add_pending_outbound_nonce`;
  `P2pManager.promote_connection` and `remove_connection` each discard
  their own connection's entry once it leaves the handshake, both
  inside the same `_connections_lock` every other access to the set
  takes. This is closer to Core's own `CConnman::CheckIncomingNonce`
  (`net.cpp:360-376` at bitcoin/bitcoin@b91d983f66), which walks the
  live, not-yet-successful outbound connections rather than a ring at
  all.

### A live node syncs against a real bitcoind (closes #374)

- **`tests/integration/bitcoind_test.py` starts a disposable regtest
  `bitcoind`, connects a fresh `Node` to it over p2p, and asserts the
  node's own tip against bitcoind's `getbestblockhash` once it reaches
  `NodeStatus.BlockSynced`** (closes #374): every other p2p test in this
  suite connects one `Node` to another, which shows `btclib`'s p2p
  implementation working against itself and nothing about it meeting a
  Bitcoin Core it did not write. `tests/integration/` is new, following
  section 7 of the organization standard -- each test skips itself
  without `BTCLIB_NODE_INTEGRATION` set, excluded from both `testpaths`
  and the coverage ratchet, and covered instead by
  `integration-bitcoind.yml`, which fails if what it runs skips rather
  than reaches a node. `.github/actions/install-bitcoind` downloads the
  pinned release the workflow points the test at, its version and
  digest read off `btclib`'s and `bitcoin-core-rpc`'s own copies of the
  same action rather than either file copied whole.

### Two waits stop sitting tighter than `wait_until`'s default (closes #476)

- **`test_a_slow_manager_start_cannot_still_clobber_the_status_it_raced` and
  `test_download` each passed `wait_until` a `timeout` under its own
  default of 60, with nothing beside either saying why** (closes #476):
  `tests/helpers.py`'s own docstring argues the timeout bounds a failure
  and not a success, so a generous limit costs a passing run nothing and
  only delays one that was going to fail -- a bound worth keeping tight
  only where the test itself asserts that the timeout fires, which
  neither of these does. Both now let the default stand.

### A connection stops reading once its queue piles up unprocessed (closes #462)

- **`Connection.run` pauses `sock_recv` once `queued_recv_bytes` --
  every octet `parse_messages` has handed to `P2pManager.messages` and
  `handle_p2p` (`p2p/main.py`) has not yet popped -- crosses
  `MAX_QUEUED_RECV_BYTES`, resuming once enough of it drains** (closes
  #462): `P2pManager.messages` was a plain `deque`, pushed to
  unconditionally by every connection's own read loop and drained by
  `Node`'s own loop at a `log2`-scaled share of its length, with nothing
  stopping a peer sending valid messages faster than that share drains
  them from growing the queue without bound. `MAX_QUEUED_RECV_BYTES`
  matches Core's own `recv_flood_size`, 5,000,000 bytes from `net.h`'s
  `DEFAULT_MAXRECEIVEBUFFER * 1000`, exactly -- not the size of this
  node's own worst legitimate receive burst, `download.py`'s own
  16-block `_request_new_block_work` batch at
  `MAX_PROTOCOL_MESSAGE_LENGTH` each, 64,000,000 bytes, which was the
  first number tried and is the wrong shape: Core requests that
  identical 16-block burst per peer too and still caps at 5,000,000,
  read at bitcoin/bitcoin@b91d983f66, pausing reading mid-burst on its
  own ordinary IBD traffic by construction, because the bytes already
  sent sit in the kernel's own receive buffer rather than being
  dropped, and the read resumes once the queue drains. A bound sized to
  fit the whole legitimate case, the way `MAX_QUEUED_SEND_BYTES` was
  before #442, never distinguishes flooding from ordinary traffic.
- **Pausing, not dropping the message or the connection** -- Core's own
  answer, `fPauseRecv`, which stops selecting a connection's socket for
  a read event rather than discarding anything already parsed, all
  read at bitcoin/bitcoin@b91d983f66: a connection over this bound has
  sent nothing but valid protocol messages faster than this node
  currently drains them, not a protocol violation to punish the way
  `MAX_QUEUED_SEND_BYTES` punishes a connection already over its own
  send budget.
- **`P2pManager.messages`' own items now carry a fourth element, the
  message's own wire size**, what `handle_p2p` weighs back off
  `queued_recv_bytes`; `handshake_messages` carries no such element,
  that queue being drained whole every pass of `Node`'s own loop rather
  than sharing this pacing.

### `RpcConnection` reads 64 KB at a time, copying a body O(1) times (closes #466)

- **`_recv_until` reads into a 64 KB buffer, matching Core's own HTTP
  server -- `HTTPServer::SocketHandlerConnected`'s `char buf[0x10000]`
  (`src/httpserver.cpp`, bitcoin/bitcoin@b91d983f66), itself adapted
  from the p2p read loop this tree's own sibling already cites -- rather
  than 1024 bytes with no argument behind that number** (closes #466):
  fewer syscalls
  per request, and `self.buffer` is a `bytearray` whose `+=` extends in
  place instead of copying everything held so far the way
  `bytes += bytes` did -- the same shape #438 fixed on the p2p side.
  That fix's own third part, deferring a parse until the buffer holds a
  whole message, has no equivalent here: `_recv_until` already returns
  only once its own predicate -- the header terminator found, or the
  declared `Content-Length` reached -- holds, and `run` parses nothing
  before that, so there was no per-chunk parse attempt to defer in the
  first place.

### The Windows classifier comment now names its known blockers (closes #429)

- **`pyproject.toml`'s classifier comment said Windows was left out
  only because nothing here runs it, not because anything is known to
  stop it** (closes #429): that was false. The comment now names
  `loop.add_reader` and `signal.SIGTSTP` as already-known blockers,
  says what changed about `SIGTSTP`'s own reach when its handler moved
  into `install_signal_handlers`, and says the list is a lower bound
  rather than an inventory. `test.yml`'s own header restated the same
  false claim in its own words, to explain why it keeps no
  `windows.yml` sentinel; it now points at the classifier comment
  instead of repeating it. Whether to support Windows at all stays
  issue #430's to decide.

### `Mempool._evict_to_limit` reads its worst entry off a heap (closes #457)

- **`Mempool` keeps `_feerate_heap`, a min-heap of individual feerate
  pushed once per accepted transaction, and `_evict_to_limit` reads the
  current worst entry off it instead of scanning every held transaction
  once per eviction round** (closes #457): the `min` scan
  btclib-org/btclib-node#441 measured and deliberately left alone is
  gone. A wtxid's feerate cannot change while it is held -- this
  mempool has no fee-bump or replace-by-fee path -- but a wtxid can
  still leave and come back (a reorg's own reconciliation is one path
  that does this), so a heap entry is discarded once its own second
  element no longer matches `_heap_current_seq`'s current record for
  that wtxid, not merely once it names a wtxid the mempool no longer
  holds: membership alone cannot tell a re-added wtxid's fresh entry
  from its own leftover first-spell entry, and the two do not tie-break
  the same way. `_pop` rebuilds the heap from scratch once its stale
  entries outnumber what is actually held, so a mempool that runs for a
  long time under its limit, the ordinary case, does not carry one heap
  entry for every transaction it has ever accepted. Bitcoin Core avoids
  this cost differently, keeping `m_txgraph`'s own live package-score
  structure rather than a heap that tolerates stale entries at all
  (`TrimToSize`, `src/txmempool.cpp:909`, bitcoin/bitcoin@58a7869f86);
  the substitute here is narrower, matching btclib-org/btclib-node#441's
  own reasoning for the index it added instead of that structure.

### `Node.run`'s idle sleep is 5 ms, not a tenth of a millisecond (closes #440)

- **`Node.run`'s loop sleeps `IDLE_SLEEP_SECONDS`, 5 ms, once a pass
  finds nothing waiting in either queue** (closes #440): the figure it
  replaces sat below the platform timer's own granularity, so an idle
  node's actual pace was set by the OS rather than by the sleep, and
  every one of those passes still ran `download_manager.step()` and
  `update_chain()` in full. Raising it cuts an idle node's own CPU cost
  by most of what it was paying, at a latency added to work arriving
  while the loop sleeps that stays a small fraction of
  `tests/helpers.py`'s own 25 ms poll in `wait_until` and
  `wait_until_listening`. Core's own message loop takes the shape of a
  wait on a condition variable a producer signals rather than a plain
  sleep (`CConnman::ThreadMessageHandler`, `src/net.cpp`, up to 100 ms,
  bitcoin/bitcoin@b91d983f66); this loop still spins on a fixed
  interval instead of being woken, which the citation does not paper
  over.

### RPC error messages now match Core's own rendering, closing #450 and #451

- **`getblockheader`'s and `getrawtransaction`'s own `RPC_MISC_ERROR`
  usage strings now match `RPCArg::ToString(oneline=true)`'s own
  rendering of their declared arguments** (closes #450):
  `getblockheader`'s used to omit its trailing optional `verbose`
  entirely, and `getrawtransaction`'s wrapped its two optional trailing
  arguments in two separate `( ... )` groups where
  `RPCMethod::ToString` opens one only on the first required-to-optional
  transition and closes it once, after the loop -- both read at
  `bitcoin/bitcoin@b91d983f66`, `src/rpc/util.cpp:775-798`.
  `getrawtransaction`'s own argument keeps this tree's `verbose` rather
  than Core's own first name for it, `verbosity`: this node's own
  argument answers only the boolean shape Core's `allow_bool=true`
  degrades to, not the full `0`/`1`/`2` verbosity Core's name is for,
  and the usage string is where that choice is now argued rather than
  left silent.
- **Every `RPC_TYPE_ERROR` this node raises for a wrongly typed
  argument now builds Core's own `"Wrong type passed:\n{...}"` wrapper**
  (closes #451), keyed `Position N (name)` the way
  `RPCMethod::HandleRequest`'s own type check builds it
  (`src/rpc/util.cpp:652-661`), rather than answering the bare sentence
  that value would carry at that one key on its own. `rpc/errors.py`'s
  new `type_error` builds the wrapper from the argument's own one-based
  position and declared name, both of which a raise site already has to
  hand or reads off `bool_param`'s own `position`; `CLAUDE.md`'s
  *Following Bitcoin Core* is why this tree's own prior agreement on the
  bare shape, across every site including the two issue #443 added, was
  not by itself a reason to keep answering Core's own surface
  differently from Core.

### `update_chain`'s own block reads stay outside the trial's rollback (closes #452)

- **A raise from `_blocks_to_add`/`_rev_blocks_to_remove` stops the node
  rather than rolling the trial back** (closes #452): both read this
  node's own already-validated blocks and reverse patches back off
  `block_db`, so a raise there is this node's storage failing to give
  back what it wrote, not a fork turning out bad. Core's `ConnectTip`
  answers a failed read the same way, with `FatalError`; its
  `DisconnectTip` answers the same failure plainly and leaves
  `FatalError` to `ActivateBestChainStep`, one level up, which holds
  any failure to walk its chain backward fatal -- a bad read among
  them -- on the same chain-advance path `update_chain` mirrors
  (`src/validation.cpp`, bitcoin/bitcoin@b91d983f66). The two calls
  keep their place ahead of the trial's own `try`, now argued in a
  comment beside them rather than left to be read as an oversight, and
  `test_a_missing_reverse_patch_stops_the_node_rather_than_rolling_back`
  (`tests/unit/init_test.py`) drives a real `Node.run` through exactly
  this raise to pin it.

### `get_cfilters` paces itself against `Connection`'s own send queue (closes #442)

- **`get_cfilters` sends from a `getcfilters` range only while
  `conn.queued_send_bytes` stays under a new, much smaller
  `MAX_CFILTERS_INFLIGHT_BYTES`, handing what it could not schedule to
  `node.pending_cfilters` for `resume_cfilters` (`p2p/main.py`) to
  finish on a later pass of `Node`'s own loop** (closes #442): this
  node has no message-processing stage to pause and resume the way
  Core's `fPauseSend` does (`net.cpp`, bitcoin/bitcoin@b91d983f66) --
  `get_cfilters` runs once, on `Node`'s own thread, and cannot `await`
  the drain the way a coroutine could, so the range it has not yet sent
  is a plain `deque` this node's own loop keeps coming back to instead.
- **`MAX_QUEUED_SEND_BYTES` (`connection.py`) no longer has to hold one
  whole legitimate `getcfilters` answer**, now that one is paced: it is
  sized for a legitimate `getdata` burst instead -- still unpaced,
  filed as #470 -- plus the new pacing bound.
- **A second `getcfilters` arriving while the first is still paused
  extends the same connection's own pending range rather than replacing
  it**, up to `MAX_PENDING_CFILTERS_HEIGHTS` -- two full requests, the
  pipelining `MAX_QUEUED_SEND_BYTES` already tolerated before this
  change -- past which a third stacked request is silent, the same
  answer this node already gives a request `_filter_range` declines for
  other reasons.

### `claude-review.yml`'s comments name the right subcommands, job and ceiling

- **The `claude_args` comment names the `gh pr` subcommands the prompt
  uses** (issue btclib-org/.github#398): `diff`, `review` and `view`.
- **The `mention` job's credential step refuses in the words of the job
  it guards** (issue btclib-org/.github#402): that job answers an
  `@claude` mention and reviews nothing, so the step is named *Refuse to
  answer without a credential* and its annotation says the workflow
  answers nothing without the secret.
- **The comment above that step points at the review job's reason
  rather than restating it** (issue btclib-org/.github#410): the
  restatement narrated a measurement made on the review job -- a token
  found empty, a review reported successful -- inside the job that
  reviews nothing.
- **The header's argument for the job's slot carries no figure** (issue
  btclib-org/.github#405): the ceiling on concurrent jobs belongs to the
  organization, so the other repositories' matrices compete for the same
  slots, and that is the whole of the argument. `REPOSITORY.md`'s *The
  concurrent-job ceiling* has the command that reads the plan the limit
  is documented for, where a figure in a comment goes wrong in silence
  the day the plan moves.
- **Section 11 of the organization's standard is cited in a full form
  and a subsection form rather than the two concatenated** (issue
  btclib-org/.github#400): `section 11's *Review*` where *Review* holds
  the rule cited; `section 11 of the organization's standard` where no
  one subsection does, the two secret stores a Dependabot-initiated run
  reads being stated in the section's own prose and in *Dependabot and
  pre-commit.ci*; and the same full form again wherever the sentence is
  the one that names the standard, which is what the file's first
  citation of it is. What chooses the shape is what holds the rule and
  never where the sentence sits. A rule that lives elsewhere in the
  standard is still cited with no section number at all.

### `getdata` paces itself against `Connection`'s own send queue too (closes #470)

- **`getdata` serves an item at a time from a `GetData` only while
  `conn.queued_send_bytes` stays under a new `MAX_GETDATA_INFLIGHT_BYTES`,
  handing what it could not serve to `node.pending_getdata` for
  `resume_getdata` (`p2p/main.py`) to finish on a later pass of `Node`'s
  own loop** (closes #470): the same mechanism #442 gave `get_cfilters`,
  reused rather than Core's own "at most one BLOCK item per call"
  (`ProcessGetData`, `net_processing.cpp:2798`, bitcoin/bitcoin@b91d983f66)
  -- a byte bound produces the same shape without a second, per-item-type
  count to keep in step with `MAX_QUEUED_SEND_BYTES`, many small
  transaction items fitting under it in one pass the way Core's own
  "process as many TX items as possible" does, and a block item large
  enough on its own that one or two exhaust it.
- **`MAX_QUEUED_SEND_BYTES` (`connection.py`) no longer has to hold one
  whole legitimate `getdata` burst either**, now that this answer is
  paced too: it is sized for each pacing bound plus one item past it --
  `advance_getdata` and `advance_cfilters` both check their own bound
  *before* the next item is popped and sent, not after, so either can
  schedule one item beyond its own bound before the next check catches
  it -- for both mechanisms together, superseding the figure #442 left
  it at.
- **A second `getdata` arriving while the first is still paused extends
  the same connection's own pending items rather than replacing them**,
  up to a new `MAX_PENDING_GETDATA_ITEMS` -- two full requests,
  `MAX_INV_SZ` apiece. Core's own protection here is not a numeric cap:
  `ProcessMessages` (`net_processing.cpp:5429-5436`,
  bitcoin/bitcoin@b91d983f66) declines to read a connection's next
  message at all while its `Peer.m_getdata_requests` backlog is still
  non-empty, which this tree cannot reproduce without restructuring
  `P2pManager.messages` from the single queue shared by every
  connection it is today into one queue per connection.
- **`notfound` covers only what one call actually served**, not the whole
  original request: an item never reached because the pacing bound
  tripped first is reported once a later call gets to it, matching
  Core's own `vNotFound`, built fresh by every `ProcessGetData` call
  rather than carried across them.
- **The batch size `_request_new_block_work` (`download.py`) asks a peer
  for is now a named, public `MAX_BLOCKS_PER_GETDATA_BURST`**, where it
  used to be an untied literal cross-referenced only in a comment on
  `connection.py`'s own former `MAX_QUEUED_SEND_BYTES` derivation: that
  derivation no longer needs it, but the fact it named -- this node
  never asks a peer for more than sixteen blocks at once, matching
  Core's own `MAX_BLOCKS_IN_TRANSIT_PER_PEER` -- outlives the bound it
  used to be sized from, and is what a peer answering this node's own
  request sends back in one burst, still relevant to this connection's
  own buffering on the receiving end.
- **`MAX_QUEUED_SEND_BYTES` (~12.3 MB) sits above both of Core's own
  flat, content-blind per-connection figures**: its send buffer default
  (1,000,000 bytes, cited above) by more than an order of magnitude,
  and its receive buffer default (`recv_flood_size`, 5,000,000 bytes)
  by roughly two and a half times. Neither is the number this bound
  matches: both are sized without reference to any one message's
  content, where this bound is sized from two real message sizes, a
  block and a filter, for the reason the paragraph beside it argues --
  this node's own dispatch has no incremental pause-and-resume loop of
  Core's own shape to lean on for the rest of an answer.

### The signal handlers move out of `Node.__init__` (closes #436)

- **`Node.__init__` no longer calls `signal.signal`** (closes #436): a
  second `Node` built in one process used to replace every handler with
  one bound to itself, leaving the first running with its databases
  open once an operator's interrupt reached the newer node instead, and
  `signal.signal` raises outside the main thread of the main
  interpreter, so a `Node` could not be built there at all. The three
  handlers move to `install_signal_handlers(node)`, a new function next
  to `Node` in `__all__`, called explicitly by the one caller in a
  process that wants an operator's interrupt to reach a given node --
  `scripts/chains/` calls it right after building the node it starts.
  Matches Core's `AppInitBasicSetup`, which `AppInit` calls and `main`
  in turn calls, registering its signal handlers at process start-up
  rather than in a constructor (`src/init.cpp`, `src/bitcoind.cpp`,
  bitcoin/bitcoin@b91d983f66).

### `Connection` reads 64 KB at a time and copies a message O(1) times (closes #438)

- **`Connection.run` reads into a 64 KB buffer, matching Core's own
  `pchBuf` (`src/net.cpp`, bitcoin/bitcoin@b91d983f66), rather than
  1024 bytes with no argument behind that number** (closes #438):
  fewer syscalls per message, and `self.buffer` is a `bytearray` whose
  `+=` extends in place instead of copying everything held so far the
  way `bytes += bytes` did.
- **`parse_messages` peeks the 24-byte envelope's own `length` field in
  `buffer` before building a stream or calling `Message.parse` at
  all**, so a chunk that does not yet complete the first message in
  `buffer` returns without copying anything -- the common case on a
  connection carrying one large message, a block during initial block
  download chief among them, over many reads. A `length` already past
  `MAX_PROTOCOL_MESSAGE_LENGTH` falls through the gate instead of being
  waited on, so `Message.parse`'s own refusal of it still fires as soon
  as the header arrives rather than once (if ever) that many octets
  did.

### `BlockDB` serializes its own reads and writes (closes #432)

- **`BlockDB` now holds one `RLock` across every public method** (closes
  #432): `open_block_file` and `open_rev_file` are each one handle with
  one file position shared by every `seek`, `read` and `write` reaching
  it, and nothing serialized `add_block`/`finalize` against
  `get_block`/`get_rev_block` before this -- a write landing between a
  reader's own `seek` and its `read` moved the position out from under
  it, answering with whatever bytes were there instead of the block or
  patch asked for. The lock matches `KeyValueStore`'s own "one
  connection, and a lock around every use of it" (`db.py:58-73`), one
  lock for the whole instance rather than one per handle: `files`, the
  size bookkeeping `__add_data_to_file` updates, is shared by both the
  `.blk` and the `.rev` side regardless of which handle a call is
  writing through.

### `Mempool._descendants` walks a spend index, not the mempool (closes #441)

- **`Mempool` keeps `spent_by`, a `dict[bytes, set[bytes]]` from a spent
  txid to the wtxids, held in this mempool, that spend it** (closes
  #441), maintained in `add_tx` and `_pop` alongside the dicts those two
  already kept in step. `_descendants` walks it from the eviction root
  instead of scanning every held transaction once per element of the
  package it is discovering, so one eviction round's own cost no longer
  multiplies the package a peer chose by the size of the mempool it is
  evicted from: measured against a growing mempool at a fixed package
  size, `_descendants` now holds flat where it grew with the mempool
  before. `_evict_to_limit`'s own `min` scan, the other O(n) factor the
  issue named, is untouched and stays linear per round; #457 is where
  that is measured and argued on its own.

### The tx-relay queueing step checks two sets, not two lists (closes #444)

- **`_queue_announcements_for_received_txs` (`download.py`) tests
  membership against a `set` at both of its peer-controlled loops, not a
  `list`** (closes #444): whether a wtxid a peer announced is one this
  node already holds, and whether a wtxid about to be queued to a
  connection is already in that connection's own `tx_announce_queue`.
  `received` keeps the list order `_send_due_announcements` sends in, and
  `tx_announce_queue` stays the `list[bytes]` `connection.py` declares it
  as; only the membership test against each now reads a `set` built
  alongside it, in the shape `has_it` already used one function over.
  The second loop is the one whose cost a benchmark can show: `queue`
  persists across calls until a connection's own trickle schedule drains
  it, `received` does not, so a call with little to announce can still
  face a large accumulated queue -- measured directly against that loop
  as it stood before this change, its own cost was quadratic in the two
  peer-controlled sizes it multiplied and is linear in their sum after
  it. The first loop's own `received` and `inv_txs` are each reset every
  call, so its argument is the one the issue itself makes: a list scan
  repeated once per `inv_txs` entry costs more than a set built once,
  without a benchmark behind that half.

### `RpcConnection.run` bounds the whole request read (closes #437)

- **`RpcConnection.run` now reads a request under `REQUEST_TIMEOUT`**
  (closes #437): `_recv_until` used to await `sock_recv` with no
  deadline anywhere in it, so a client that connected, sent a byte and
  stopped kept its socket -- and its entry in `RpcManager.connections`,
  since only `send()` popped it -- open for the life of the node. The
  bound is spent once, on the whole read from accept to a complete
  request, rather than reset on each `sock_recv`: a per-read timeout
  alone would still leave a client trickling one byte at a time
  unbounded. It matches Core's own `-rpcservertimeout` default, 30
  seconds (`DEFAULT_HTTP_SERVER_TIMEOUT`, `src/httpserver.h`), though
  not Core's own mechanism -- Core resets that timer on every receive
  and every send (`httpserver.cpp:930,1275`) and its own
  `DisconnectClients` (`:1098-1100`) only disconnects a client idle
  *between* requests on a connection carrying more than one, and this
  tree's own connection is one request per socket, with no such
  "between" for a reset to find. `RpcManager`'s own periodic sweep the
  issue also named was not taken: the peer-to-peer side's
  `_prune_stale_connections` fits a peer connection that is legitimately
  idle between messages, and an RPC connection is one request with no
  such gap to distinguish from a stall.
- **`run`'s own catch-all now also pops `manager.connections`** on every
  failure, not only the JSON-parse branch, which already did: an
  unterminated header, an overstated or negative `Content-Length`, and a
  peer going away mid-request each raise `ConnectionError` there, and
  none of them reached `send()` -- the only other place popping that
  table -- so each left its id behind regardless of `REQUEST_TIMEOUT`.

### `claude-review.yml` converges to the organization's current mechanism

- **The `review` job now gates on the organization variable
  `CLAUDE_REVIEW_ENABLED`** (issue btclib-org/.github#364), on the job
  rather than a step: unset organization-wide, so a `pull_request` run
  skips cleanly instead of failing at "Review against REVIEWING.md" the
  way every run has since the action's own SDK call started erroring,
  a cause that issue leaves unestablished.
- **The guard step that reports a review which never ran now reads
  `api_error_status`, `stop_reason` and `.result` off the SDK's
  execution file** (issue btclib-org/.github#385) when the review step
  did not succeed, instead of reporting only that it failed.
- **The verdict now posts as a pull request review of type `COMMENT`**
  (`gh pr review --comment`, never `--approve` or `--request-changes`)
  **rather than an issue comment** (issue btclib-org/.github#340), and
  the verification step now reads `pulls/<n>/reviews` instead of issue
  comments; the verdict lines are `ACK`, `CHANGES REQUESTED` and now
  also `NACK`.
- **The `review` job's timeout is 20 minutes, with a 15-minute ceiling
  on the review step itself**: a review that exhausts its own budget
  now fails that step, with the runner's own line saying so, rather
  than the job being cancelled by the outer limit with nothing in the
  checks to show for it.

### `NodeStatus.Reindexing` goes (closes #445)

- **`NodeStatus` no longer declares a `Reindexing` member** (closes
  #445): nothing assigned it, and its position between `HeaderSynced`
  and `BlockSynced` meant every inequality comparison against either end
  would apply to a reindexing node the moment something did, without
  anyone having chosen which side of each it belonged on. The issue
  reserved a decision between naming that ordering explicitly and
  removing the member until there is a reindex to represent; this takes
  the removal, matching `get_mempool_info`'s own refusal to answer
  fields it has no source for (#305). A reindex is represented again by
  whatever change implements one, together with the code that sets it.

### `_prune_stale_connections` continues past a removed connection (closes #435)

- **The `Closed` branch of `_prune_stale_connections`'s first loop now
  `continue`s** (closes #435): without it, a connection removed there
  was still the loop variable for the idle check right below, and with
  `last_receive` frozen at whatever it stopped at and `ping_sent` still
  `0`, that check ran `send_ping` on a connection already out of both
  `connections` and `pending_connections` -- drawing a nonce and
  taking `_ping_lock` for a socket `remove_connection` had just handed
  to `conn.stop()`. `#357` is what made `_ping_lock` protect exactly
  that state.

### `send_version`'s nonce ring keeps the newest ten, not the oldest (closes #433)

- **`Connection.send_version` truncates `manager.nonces` with `[-10:]`
  instead of `[:10]`** (closes #433): the old slice kept the first ten
  nonces this process ever drew, so past the tenth connection every
  freshly appended nonce was discarded on the same line, and
  `callbacks.version`'s self-connection check compared an incoming
  `version`'s nonce against ten connections long gone rather than any
  connection still in flight. `[-10:]` keeps the ten most recently
  sent, the ring `manager.nonces` was written to be.

### `Node.run` sets `status` before starting either manager (closes #398)

- **`Node.run` now assigns `self.status = NodeStatus.SyncingHeaders`
  before `p2p_manager.start()` and `rpc_manager.start()` rather than
  after both** (closes #398): `listening` is set on a manager's own
  thread, so a caller whose `wait_until_listening` returns learns
  nothing about whether `Node`'s own thread has reached that assignment
  yet. A test writing `node.status = NodeStatus.HeaderSynced` right
  after `wait_until_listening` could race it, and a late write from
  `Node`'s own thread landing after the test's put `status` back below
  `HeaderSynced` for the life of the node -- `_ready_fork` never returns
  past that again, so the chain never extends and a functional test
  waiting on it times out at 60 seconds, on a machine loaded enough to
  deschedule `Node`'s thread in that window. Every reader outside
  `Node`'s own loop compares `status` against `HeaderSynced` or
  `BlockSynced`; the one that names `SyncingHeaders` itself, `headers`
  in `p2p/callbacks.py`, is reached only from inside that loop, which
  both statements precede on that same thread, so it cannot observe
  the order between them either way. Moving the write earlier
  therefore changes no behaviour other than closing the window.

### A missing argument to `testmempoolaccept`/`sendrawtransaction` (closes #443)

- **An empty `params` no longer reaches `params[0]` unguarded.** Both
  callbacks used to raise `IndexError` on it, which `handle_rpc` answers
  `INTERNAL_ERROR` / "Internal Error" -- the code this node owes its own
  fault, for a call that was merely short of a required argument. Each
  now raises `RpcError(MISC_ERROR, ...)` carrying the method's own
  oneline usage, the same shape `get_block_hash`, `get_block_header` and
  `get_raw_transaction` already answer this with, derived from
  `RPCArg::ToString(oneline=true)` over `sendrawtransaction`'s and
  `testmempoolaccept`'s own declared arguments
  (`src/rpc/mempool.cpp:72-77` and `:291-298`, read at
  `bitcoin/bitcoin@b91d983f66`).
- **`testmempoolaccept`'s `rawtxs` is now type-checked before the loop
  that reads it.** A JSON string is itself iterable in Python, so a
  non-list `rawtxs` used to be walked one character at a time rather
  than refused; Core declares this argument `RPCArg::Type::ARR`,
  type-checked before the handler body runs, the same mechanism
  `blockhash` and `txid` are already checked against elsewhere in this
  file.

### A `stop` at or below the locator no longer raises (closes #434)

- **A known `stop` hash at or below `getheaders`'s own resolved locator
  no longer raises `ValueError`** (closes #434): the previous check
  looked for `stop` in the whole of `header_index`, where the answer is
  built from the slice *after* the locator, and a `stop` below it is in
  the first without being in the second. `p2p.main.handle_p2p` read the
  exception as this node's own bug and dropped the peer for it, on a
  request Core answers without incident -- with nothing to send where
  the locator is already this node's own tip, and with the headers past
  the locator otherwise, `stop` being unreachable going forward from
  it.

### `header_index_pos` resolves a locator in O(1), not a list scan (closes #439)

- **`BlockIndex` now keeps `header_index_pos`, a `dict[bytes, int]` from
  hash to position, beside `header_index`** (closes #439), the same way
  `chainwork` sits beside `header_dict` (#201) and `children` beside
  `header_dict`'s own lineage (#125). `get_headers_from_locators` now
  resolves a locator by lookup rather than by scanning the whole known
  chain of headers, once per entry the peer's own locator carries -- so
  neither the size of the chain nor the length of a locator the peer
  chooses is anything a `getheaders` request can turn into more work for
  this node.
- **The answer is sliced to 2000 before `stop` is looked for, not
  after**: the tail past the resolved locator is no longer copied in
  full only to be capped once `stop` has already been searched for
  across the whole of it.

### The docs gate warns against `--only-group docs` (closes #425)

- **`CONTRIBUTING.md`'s *The environment and the gates* now names
  `--only-group docs` as the wrong substitute for the docs-gate
  command's own `--no-default-groups --group docs`** (closes #425):
  `--only-group` excludes the project along with every other group, so
  autodoc's own import of `btclib_node` raises `ModuleNotFoundError`
  under `-W` on a `.venv` that does not already have it installed, and
  says nothing on one that does from an earlier `uv sync` or
  `uv run pytest` in the same session -- an outcome that tracks the
  `.venv`'s history rather than the tree the command is meant to check.
  `docs.yml` already carries the same warning beside its own copy of
  the command; this is that reasoning reaching the file a contributor
  reads before running the gate by hand.

### `docs.yml`'s job is a required check on `main`

- **Branch protection's `required_status_checks` now names three
  contexts** instead of two: `Lint and type-check`, `test: every job
  passed`, and `Build the documentation` -- the follow-up
  `REPOSITORY.md`'s *Required checks on main* names beside the entry
  below, applied once that job had a green run on `main`.
  `REPOSITORY.md` is updated to match, and its documented `gh api`
  PATCH now reads `-F strict=true` rather than `-f strict=true`: the
  latter sends the JSON string `"true"`, which the API refuses for a
  boolean field.

### `docs.yml` earns its place in the release path (closes #264)

- **`release.yml` gains a `docs:` job**, calling `docs.yml` the way
  `lint:` already calls `lint.yml`, and named in `publish-testpypi`'s
  and `publish-pypi`'s own `needs:` (closes #264): a tag now publishes
  only once its own tree's documentation has built, not merely once
  whichever commit last ran the check on `main` has. `REPOSITORY.md`'s
  *Required checks on main* names the three contexts branch protection
  is to require; the `gh api` PATCH that adds the third to the live
  setting is that section's own follow-up, applied outside this pull
  request rather than carried by it.
- **`docs/source/conf.py` sets `html_theme = "furo"`**, replacing
  `sphinx_rtd_theme`, and `pyproject.toml`'s `docs` dependency group
  follows (issue #402): section 3 of the organization standard.
- **The docs build runs `-n` alongside `-W`**, in `docs.yml`,
  `.readthedocs.yaml`, `RELEASING.md` and `CONTRIBUTING.md` (issue
  #402): section 5 of the organization standard. `conf.py` gains
  `sphinx.ext.intersphinx`, mapped against python and btclib, and a
  `nitpick_ignore` list, each entry reasoned, for the references neither
  inventory answers -- ruff's own "TC" family moving a typing-only
  import under `TYPE_CHECKING` on this tree's `>=3.14` target is what
  blocks autodoc from resolving the annotation it renders in most of
  them, a python doc-versus-implementation-module mismatch accounts for
  `asyncio.AbstractEventLoop`, and two local type aliases this tree
  documents nowhere account for the rest.

### The py.typed entry's stale test path is corrected here (closes #421)

- **The `py.typed`/`__all__` entry (`btclib-org/.github#239`, further
  down this file) still cites `tests/unit/main.py` for `from btclib_node
  import Node, main`: that file is `tests/unit/main_test.py`** (closes
  #421). The citation was accurate when written and went stale under
  #26/#268's later rename; the entry it sits in already landed, so it
  stands uncorrected and this entry is the correction instead of a
  rewrite of it.

### `tests/unit/rpc/manager_test.py` cites the right test module (closes #419)

- **The module docstring now cites `tests/unit/rpc/main_test.py`**
  (closes #419): it named `tests/unit/rpc/main.py`, which does not
  exist -- every test module in this tree ends in `_test.py`, per
  #26/#268.

### `rpc.connection`'s `Connection` becomes `RpcConnection` (closes #417)

- **`src/btclib_node/rpc/connection.py`'s `Connection` is renamed
  `RpcConnection`**, along with every annotation, import and docstring
  mention across `rpc/callbacks.py`, `rpc/main.py`, `rpc/manager.py` and
  their tests (closes #417): it shared its bare name with
  `p2p/connection.py`'s own unrelated `Connection`, and the docs build's
  `-W` fails on Sphinx's "more than one target found for cross-reference
  'Connection'" wherever autodoc renders one as a type hint. Both
  classes reach every annotation that names them only through a
  `TYPE_CHECKING`-only import, so autodoc can never introspect the real
  class behind either annotation and falls back to the bare word
  written in the source -- `autodoc_typehints_format`, which only
  reformats a type hint autodoc *did* resolve, has nothing to qualify in
  that fallback and leaves the warning unchanged. Renaming one of the
  two removes the ambiguity from the word itself, with no
  `docs/source/conf.py` change and no quoted or dotted annotation
  needed at any call site.

### `tests/unit/init_test.py`'s comment names the right test modules (closes #415)

- **The comment above
  `test_every_message_waiting_is_taken_before_the_loop_waits` now cites
  `tests/unit/p2p/main_test.py` and `tests/unit/rpc/main_test.py`**
  (closes #415): it named `tests/unit/p2p/main.py` and
  `tests/unit/rpc/main.py`, which do not exist -- every test module in
  this tree ends in `_test.py`, per #26/#268.

### `test.yml`'s coverage job gates both `3.14` and `3.14t`

- **The coverage job is a two-cell matrix over the interpreter, `3.14`
  and `3.14t`, and `test: every job passed` requires both** (closes
  #387): the two cells run as parallel jobs, so the second one costs one
  more job at the organization's concurrency ceiling and no extra wait,
  which is what buys it a place in the gate rather than in a weekly
  sentinel beside it -- the trade `os-macos.yml`'s own header states for
  a platform row, read the other way. `3.14t` reaches the 100% floor
  `[tool.coverage.report]` already declares with no change to that
  configuration; `.python-version` and `requires-python` both stay
  `3.14`.

### `worker_pool` builds a `ThreadPool` under free threading, closing issue #388

- **`Node.worker_pool` builds a `ThreadPool` rather than a process
  `Pool` where `sys._is_gil_enabled()` answers `False`** (closes #388):
  a new `_pool_factory(gil_enabled=...)` -- a pure function with the
  predicate injected, so both arms construct and are asserted on either
  interpreter -- is what the property calls with that reading, rather
  than branching inline. Under a GIL build the choice still favours a
  process pool, `btclib-secp256k1`'s cffi call not releasing the GIL
  across it, so `Pool` stays the default there.
- **The `ThreadPool` arm answers the two questions issue #388 raised
  against it, by reading and by running rather than by trusting a
  process-era comment**: `btclib`'s script engine
  (2a93afb3cdfaad5df25d1ec2516f9899e28c5ce2) never writes to the `Tx`,
  `TxIn` or `PrecomputedTxData` a task is handed, and `btclib-secp256k1`
  verifies through a single libsecp256k1 context its own "Thread safety"
  section documents as safe for concurrent calls. Under this arm,
  `interpreter._tasks`' own `precomputed` is what becomes Core's raw
  `PrecomputedTransactionData*`, shared by reference across a
  transaction's own per-input tasks exactly as `CScriptCheck::txdata` is
  shared across Core's `CCheckQueue` threads (`validation.h`,
  bitcoin/bitcoin@794a753958), where the process arm still ships each
  task its own pickled copy.
- `_WORKER_PROCESSES`/`_default_worker_processes` are renamed
  `_WORKER_COUNT`/`_default_worker_count`: the size they compute names a
  thread count on the new arm as much as a process count on the
  existing one, and the reasoning against `pytest-xdist` (issue #46)
  that sizes it holds for both, an OS thread competing for a core
  exactly as an OS process does once the interpreter running it is
  free-threaded.
- `.python-version`'s own comment no longer points at issue #306, closed
  `NOT_PLANNED`, as the open question behind the `3.14` pin: it points at
  this issue and at issue #385 instead, and says why the pin itself does
  not move now that `worker_pool` reads `sys._is_gil_enabled()` at
  runtime -- that is not the same as switching the default interpreter
  this tree runs under.

### `scripts/` gets real docstrings, `D100`'s own deferral is gone (issue #373)

- **Every module under `scripts/` -- the three `chains/` launchers, the
  two hand-edited reset templates, `prune.py`, `test_errors.py` and
  `testnet_test.py` -- carries a module docstring grounded in what it
  does and how it is meant to be run** (issue #373): a one-off template
  edited by hand before each use, a manual diagnostic run directly
  against local fixtures, or a launcher invoked as a plain script, each
  said which. `pyproject.toml`'s own `"scripts/**" = ["D100"]`
  per-file-ignore, left in place by issue #264 and split out untouched
  by #373's own `tests/**` restructuring, is removed now that the
  directory it deferred is clean. `tests/**`'s own seven per-subdirectory
  keys for the same eight codes are unaffected and remain #373's
  outstanding scope.

### `tests/unit/rpc/` and `tests/functional/rpc/` get real docstrings (issue #373)

- **`D100`/`D104`/`D101`/`D102`/`D103`/`D107`/`D205`/`D401` are selected
  for `tests/unit/rpc/` and `tests/functional/rpc/`** (issue #373): every
  module, class, function and `__init__` across both directories now
  carries a docstring grounded in what it actually tests, read against
  the source and the test body rather than restated from the function
  name -- `RpcManager`'s own accept-queue mechanism among them. The two
  `pyproject.toml` per-file-ignore keys these codes occupied are removed
  outright now that both directories answer zero. This is one of #373's
  five parallel `tests/**` slices; the `p2p/` bucket under `tests/unit/`
  and `tests/functional/`, the `tests/unit/`-root enumeration, and
  `tests/{__init__,conftest,helpers}.py`, remain. `scripts/**`'s and
  `tests/unit/chainstate/**`'s own slices already landed.

### `docs/source/index.rst`'s toctree names `SECURITY.md` and `RELEASE_NOTES.md`

- **`docs/source/security_link.md` and `docs/source/release_notes_link.md`
  join the existing four `*_link.md` shims, and `index.rst`'s toctree
  names both, in the position `bitcoin-core-rpc`'s own index.rst uses**
  (closes #390): `SECURITY.md` and `RELEASE_NOTES.md` landed at the repo
  root with the tier-1 promotion and neither had a shim, so the
  documentation build resolved `README.md`'s own links to either file to
  a plain GitHub blob link rather than to an in-site page.

### `no-hyphen-at-end-of-line`, the organization's other pygrep hook, joins `local-link-prefix`

- **`.pre-commit-config.yaml` carries `no-hyphen-at-end-of-line` beside
  `local-link-prefix`, matching `bitcoin-core-rpc`'s own pattern and
  `types: [markdown]` scoping** (closes #392): section 4 of the
  organization standard lists both pygrep hooks as adopted
  organization-wide, and this tree carried only the first. The lines
  elsewhere in this file that the hook would have caught -- a word and
  an inline code span identifier each wrapped at their own hyphen -- are
  rewrapped so `--all-files` passes clean.

### `CLAUDE.md` names Core's own checkout and the coverage floor's load-based flake

- **`CLAUDE.md`'s *Following Bitcoin Core* now says a checkout of Core is
  kept beside the primary checkout of this repository, not beside
  whichever worktree a session is working in, and names the
  `git worktree list --porcelain` recipe that finds the primary
  checkout's sibling from any worktree** (closes #396): a plain
  `../bitcoin` resolves only from the primary checkout, and a raw
  network fetch of one file is no substitute for the local checkout
  either, since it can come back truncated with nothing to say so,
  missing the very function a divergence question is about.
- **`CLAUDE.md`'s *Non-obvious facts* now names the coverage floor's
  other flake, beside the `COVERAGE_FILE` one already there**:
  [ISS 372](https://github.com/btclib-org/btclib-node/issues/372)
  measured a run reporting `99.98%` and missing the 100% floor on a
  branch inside `P2pManager`'s own background thread while every test
  passed, distinguished from a real regression only by the load average
  `uptime` gave at that run.

### `src/btclib_node/`'s own root modules get real docstrings (issue #373)

- **`D101`/`D102`/`D103`/`D105`/`D107` are selected for `src/btclib_node/`'s
  own root-level modules** (issue #373): every class, method, function,
  `__init__` and magic method under `__init__.py`, `chains.py`,
  `config.py`, `constants.py`, `db.py`, `download.py`, `exceptions.py`,
  `interpreter.py`, `log.py`, `main.py` and `mempool.py` now carries a
  docstring grounded in what it does, or a reasoned per-file suppression
  where one would only repeat its class's own -- `exceptions.py`'s own
  trivial `__init__`s, deferred to `pyproject.toml`'s own per-file-ignore
  rather than a near-identical one-liner apiece. `block_db/`, `chainstate/`,
  `p2p/` and `rpc/` are the remaining four slices, each deferred the same
  way #264's own `D100`/`D104` split already deferred `tests/**` and
  `scripts/**`.
- **`D205`/`D401`/`D403`/`D404`/`D105`, the small style codes issue #373
  asked to fold into whichever slice landed first, are selected
  tree-wide for `src/btclib_node/`, `scripts/` and `.github/scripts/`**:
  stray findings outside this slice's own root modules -- `D205` and
  `D401` in `rpc/`, `D205` in `p2p/`, `D401` and `D403` in
  `.github/scripts/check_vendored_pin.py` -- are fixed directly, since
  none is tied to a directory's own D101-D107 sweep still being
  incomplete. `scripts/test_errors.py`'s own finding is a `D103`
  instead, picked up alongside them since it was one function;
  `D101`/`D102`/`D103`/`D107` stay unselected for `scripts/**`
  otherwise, deferred with `tests/**`, which carries findings of its own
  for `D205` and `D401` and is deferred the same way.

### `src/btclib_node/block_db/` gets real docstrings (issue #373)

- **`D101`/`D102`/`D103`/`D107` are selected for
  `src/btclib_node/block_db/`** (issue #373, slice 2): every class,
  method and `__init__` under `block_db/__init__.py` -- `RevBlock`,
  `BlockLocation`, `FileMetadata` and `BlockDB` themselves, and their
  public methods -- now carries a docstring grounded in what it does.
  `block_db/`'s own private helpers (the double-underscore file lookups
  `BlockDB` keeps for itself) stay undocumented, a leading underscore
  already keeping a method outside every one of those four codes.
  `chainstate/`, `p2p/` and `rpc/` are the remaining three slices, each
  still deferred the way #264's own `D100`/`D104` split deferred
  `tests/**` and `scripts/**`.

### `src/btclib_node/chainstate/` gets real docstrings (issue #373)

- **`D101`/`D102`/`D103`/`D107` are selected for
  `src/btclib_node/chainstate/`** (issue #373, slice 3): every class,
  method and `__init__` across `__init__.py`, `block_index.py`,
  `filter_index.py` and `utxo_index.py` now carries a docstring grounded
  in what it does -- `contextual.py` already had one for everything it
  defines. `chainstate/__init__.py`'s own module docstring is corrected
  alongside its new `Chainstate` docstring: it claimed each of the three
  indexes is kept in its own `KeyValueStore`, where `Chainstate.__init__`
  opens one store and hands the same object to all three, told apart by
  key prefix alone. `p2p/` and `rpc/` are the remaining two slices, each
  still deferred the way #264's own `D100`/`D104` split deferred
  `tests/**` and `scripts/**`.

### `src/btclib_node/p2p/` gets real docstrings (issue #373)

- **`D101`/`D102`/`D103`/`D107` are selected for `src/btclib_node/p2p/`**
  (issue #373, slice 4): every class, method, function and `__init__`
  across `__init__.py`, `address.py`, `callbacks.py`, `connection.py`,
  `main.py`, `manager.py` and `messages/errors.py` now carries a
  docstring grounded in what it does and, where the code's own
  cross-thread behavior is what a docstring would otherwise get wrong,
  checked against every real call site rather than assumed -- caught
  this way before landing: `Connection.send` and `Connection.send_ping`
  are each reachable from `Node`'s own thread and from `P2pManager`'s
  alike, not from one alone. `rpc/` is the remaining slice, still
  deferred the way #264's own `D100`/`D104` split deferred `tests/**`
  and `scripts/**`.

### `src/btclib_node/rpc/` gets real docstrings (issue #373)

- **`D101`/`D102`/`D103`/`D107` are selected for `src/btclib_node/rpc/`**
  (issue #373, slice 5): every class, method, function and `__init__`
  across `callbacks.py`, `connection.py`, `errors.py`, `main.py` and
  `manager.py` now carries a docstring grounded in what it does, several
  cited against Bitcoin Core's own source at the commit each was read
  at. This is the fifth and last of #373's own package-directory
  slices: no directory under `src/btclib_node/` defers any of the four
  any longer. Left open, unchanged from #264's own
  scope and not part of the five-slice split: `D100`/`D104` for
  `tests/**` and `scripts/**`, and `D205`/`D401` for `tests/**`, both
  still deferred by their own per-file-ignore rather than read
  individually. `D403`/`D404`/`D105` are clean tree-wide, `tests/**`
  and `scripts/**` included.

### `tests/**`'s own D-family per-file-ignore splits by subdirectory

- **The single `"tests/**"` per-file-ignore entry for those eight codes
  is now one key per `tests/unit/` subpackage** (issue #373): no
  docstring is written and no finding is fixed by this change --
  `uv run ruff check --select <those eight codes> --statistics tests
  scripts` reports the exact same total before and after. This is
  infrastructure ahead of the five parallel pull requests #373's own
  remaining scope is about to be split into, one per new key, so each
  can remove only its own line without conflicting with the other
  four's. Verified directly rather than assumed: `ruff`'s own glob
  matches across a `/` even for a bare `*`, so the bucket for
  `tests/unit/`'s own root-level files (no subpackage of their own) is
  an explicit enumeration by name, not a glob that would also silently
  catch the four subpackages split out beside it -- confirmed by
  removing each new key in turn and checking that only its own files'
  findings reappear, never a neighbor's.

### `tests/unit/chainstate/` gets real docstrings (issue #373)

- **`tests/unit/chainstate/`'s own per-file-ignore key is removed
  entirely** (issue #373): every module, class and function under
  `__init__.py`, `block_index_test.py`, `contextual_test.py`,
  `filter_index_test.py`, `init_test.py` and `utxo_index_test.py` now
  carries a docstring, each `test_*` function's own naming the scenario
  its assertions actually cover rather than restating the function
  name. One test's own name and comment, `block_index_test.py`'s
  `test_reject_header_above_the_pow_limit`, described a mechanism the
  code does not take: mainnet's own proof-of-work limit is harder than
  regtest's, so a header claiming it never reaches
  `assert_valid_pow`'s range check at all, and is refused instead
  because an unmined nonce does not satisfy so hard a target -- renamed
  to `test_a_header_claiming_a_target_it_was_never_mined_to_is_refused`
  and its comment corrected to match. This is one of #373's five
  parallel `tests/**` slices; the `p2p/` and `rpc/` buckets under
  `tests/unit/` and `tests/functional/`, the `tests/unit/`-root
  enumeration, and `tests/{__init__,conftest,helpers}.py`, remain.
  `scripts/**`'s own slice already landed.

### `tests/unit/`'s own root-level files get real docstrings (issue #373)

- **`D100`/`D104`/`D101`/`D102`/`D103`/`D107`/`D205`/`D401` are selected
  for every `.py` file directly in `tests/unit/` -- not its `p2p/`,
  `rpc/` or `chainstate/` subpackages, each its own slice -- plus
  `tests/__init__.py`, `tests/conftest.py` and `tests/helpers.py`**
  (issue #373): every module, class, fixture and test function across
  these files now carries a docstring, including every `test_*`
  function, none left to a per-file suppression. A test's own docstring
  says what scenario it verifies and why, read from what its body
  actually asserts rather than restated from its own name --
  `coverage_floor_test.py`'s test for a run with no `lf` cache plugin is
  the sharpest case, its own docstring turning on
  `relax_coverage_floor`'s short-circuiting `or` chain only reaching
  `option.lf` once every other narrowing option is already falsy.
  `block_db_test.py`'s docstrings are cross-checked against `BlockDB`
  itself, already documented from this issue's own second slice. This
  is the root-level bucket of the five parallel slices `tests/**`'s own
  per-file-ignore was split into; its own two keys are now removed
  outright rather than narrowed, the family having no finding left
  under either.

### `tests/unit/p2p/` and `tests/functional/p2p/` get real docstrings (issue #373)

- **Both per-file-ignore keys for the eight-code family are removed
  entirely** (issue #373): every module, class, function and `__init__`
  under `tests/unit/p2p/` and `tests/functional/p2p/` now carries a
  docstring, each `test_*` function's own naming the scenario its
  assertions actually cover rather than restating the function name,
  checked against `src/btclib_node/p2p/callbacks.py` and
  `src/btclib_node/p2p/connection.py` rather than against the test's own
  name. `callbacks_test.py`'s
  `test_a_pruned_peer_is_let_go_only_once_the_blocks_are_synced` names
  its own scenario with a variable called `pruned` carrying
  `NODE_WITNESS`, where the service `callbacks.version` actually gates
  the drop on is `NODE_NETWORK`; the docstring describes the real gate
  rather than the variable's own name. This is the last of #373's five
  parallel `tests/**` slices to close, past `scripts/**`,
  `tests/unit/chainstate/`, `tests/unit/rpc/`/`tests/functional/rpc/`
  and `tests/unit/`'s own root-level bucket, all already landed. The
  rpc slice's own landing renamed `tests/functional/rpc/**`'s
  per-file-ignore key to `tests/unit/chainstate/**` instead of deleting
  it outright, silently reintroducing a key the chainstate slice had
  already removed; that key is removed again here, alongside this
  slice's own two, since the family now has no finding left under any
  of the three.

### The docs gate's own remaining gaps close: `[project.urls]`, `local-link-prefix`

- **`conf.py`'s `BLOB` constant reads `pyproject.toml`'s own
  `[project.urls].repository`** (closes #378): that table landed with
  the tier-1 promotion (issue #286), after `docs/source/` (issue #264)
  had already worked around its absence with a local `REPO_URL`
  constant -- removed now that the table it stood in for exists.
- **`local-link-prefix` is a pre-commit hook here too** (closes #379):
  section 4 of the organization standard carries it in every
  repository, and it refuses a local markdown link destination that
  does not begin `./` -- the shape `docs.yml`'s own built-page grep
  cannot always catch, since an unresolvable link written without the
  prefix renders indistinguishably from a real autodoc anchor.
- **`check-readthedocs` validates `.readthedocs.yaml`** the way
  `check-dependabot` already validates `.github/dependabot.yml`: the
  hook existed and this repository's own `.pre-commit-config.yaml`
  said outright why it was left out, a sentence issue #264 made false
  the day `.readthedocs.yaml` landed.

### `stop()`'s own leftover `loop.stop` is cancelled, closing issues #377 and #380

- **`stop()`'s own first line, `self.loop.call_soon_threadsafe(self.loop.stop)`,
  now keeps the `Handle` it returns and cancels it once `join()` above
  has returned** (closes #377, closes #380): that call only *schedules*
  `loop.stop`, delivered only once something drives the loop's
  `run_forever` far enough to reach it, and nothing does where the
  manager's own thread was never started or where `run()` raised before
  ever reaching `run_forever` -- a bind failure being the ordinary way.
  Every `run_until_complete` `stop()` goes on to call, this method's own
  thread now driving the loop instead, used to be primed to collide with
  that leftover callback and raise `RuntimeError('Event loop stopped
  before Future completed.')`: the grace step guard issues #368 and #362
  each added answered "was `start()` called", true from the moment of
  that call and well before `run_forever`, not "did `run_forever` ever
  deliver this method's own scheduled stop" (issue #380); and the
  unconditional drain loop beneath the grace step carried no guard of
  its own at all, for a task whose cancellation-unwind needs a second
  real step to finish (issue #377). `Handle.cancel()` on a handle
  already delivered by the manager's own thread is a no-op, so
  cancelling it unconditionally here is correct for the ordinary case
  and removes the leftover callback outright for the other two, on both
  methods alike.
- **The grace step itself is now guarded on `self._server_sockets`
  (`P2pManager`) and `self._server_socket is not None` (`RpcManager`)**,
  set only once `run` has bound successfully and is about to schedule
  `server`'s own accept task: not for safety, which the cancelled handle
  above already covers unconditionally, but because a manager whose
  `server` was never scheduled has no accept task the step could be
  owed to.

### `P2pManager.server` stops discarding an accepted socket, closing issue #386

- **`server` stores what it accepts in a queue, from a plain reader
  callback registered with `loop.add_reader`, rather than awaiting
  `loop.sock_accept` inside a task of its own** (closes #386): that
  task's own future could already carry a connection when `stop`'s own
  blanket sweep over `asyncio.all_tasks` cancelled it directly, and
  `Task.cancel` on a task whose own awaited future is already done
  discards it -- forcing `CancelledError` in on the next step regardless
  of what the future already held, with nothing left holding the
  accepted socket. Measured against a live listener under load, not
  only the deterministic race the existing regression tests construct:
  an instrumented copy of `stop()` traced the exact cancel discarding an
  already-resolved `sock_accept` future on a manager fielding real
  connections. The accepted socket now sits in the queue's own deque
  the instant the callback runs, immune to that discard regardless of
  when a cancel reaches the task waiting on the queue, and `server`'s
  own `finally` closes whatever a cancellation leaves there.
- **`P2pManager.stop`'s own grace step is removed rather than given a new
  guard**:
  it existed only to let a task sitting on an already-resolved future --
  `server`'s own former `accept` task -- return normally into
  `create_connection` before a direct cancel discarded it, which
  `server`'s new queue makes unnecessary, and to avoid asking a loop
  for one more step past its own scheduled `loop.stop` before that
  stop was ever delivered, which `stop_handle.cancel()` (issue #377,
  issue #380) already answers unconditionally. Neither reason applies
  any longer.

### `RpcManager.server` stops discarding an accepted socket, closing issue #391

- **`RpcManager.server` now stores what it accepts in a queue, from a
  plain reader callback registered with `loop.add_reader`, rather than
  awaiting `loop.sock_accept` inside a task of its own** (closes #391):
  the identical race #386 fixed on `P2pManager.server`, in
  `RpcManager.server`'s own copy of the same construct -- `stop`'s own
  blanket sweep over `asyncio.all_tasks` could cancel that task directly
  while its awaited future already carried a connection, and
  `Task.cancel` on a task whose own awaited future is already done
  discards it regardless. The accepted socket now sits in the queue's
  own deque the instant the callback runs, and `server`'s own `finally`
  closes whatever a cancellation leaves there.
- **`RpcManager.stop`'s own grace step is removed rather than given a
  new guard**, for the same reason #386 removed `P2pManager.stop`'s:
  the task it protected no longer exists, and `stop_handle.cancel()`
  (issue #377, issue #380) already answers the other reason a step like
  it ever ran.

### `check_transactions`'s tasks share a `PrecomputedTxData`, closing issue #385

- **Every `f` task `check_transactions` dispatches now carries a
  `PrecomputedTxData` built once for its own transaction, where it
  carried none at all** (closes #385): handed `None`, `sig_hash.segwit_v0`
  re-serialized and re-hashed a transaction's prevouts, sequences and
  outputs for every one of its inputs, the quadratic `PrecomputedTxData`'s
  own docstring names (btclib-org/btclib#164). The unit of work stays
  the input and `Node.worker_pool` stays a process pool: Core keeps the
  same pair of properties -- per-input granularity and one
  precomputation per transaction -- by sharing one
  `PrecomputedTransactionData` through a raw pointer across the threads
  its `CCheckQueue` runs (`validation.cpp`'s `ConnectBlock`,
  `validation.h`'s `CScriptCheck::txdata`, read at
  bitcoin/bitcoin@794a753958). A process pool cannot share a pointer, so
  each task is pickled its own copy of the same object instead -- a
  handful of hashes, cheap next to the whole transaction every task
  already carries.

### `P2pManager.stop()`'s grace step is guarded on `self.ident`, closing issue #368

- **`stop()`'s grace step -- `run_until_complete(asyncio.sleep(0))`,
  giving `accept` a chance to land normally before the sweep cancels it
  (issue #353) -- runs only where `self.ident is not None`** (closes
  #368): guarding it on `pending` being non-empty, as before, read a
  caller's own tasks created directly on `manager.loop` the same as
  `server`'s own `accept`, and asked a loop that had never delivered its
  own scheduled `loop.stop` to run one more step, raising
  `RuntimeError('Event loop stopped before Future completed.')`
  precisely where `start()` was never called at all. `RpcManager.stop()`
  carries the identical guard, for the identical reason (issue #362).

### `deps-latest.yml` and `mutation.yml` join this repository's weekly sentinels

- **`deps-latest.yml` upgrades every dependency `uv lock --upgrade`
  resolves -- `btclib`'s pinned `main` included -- and runs the suite and
  the lint gate against the result** (issue #287): weekly, at this
  repository's minute, and gating nothing, the way every workflow this
  calendar adds does -- a release nobody has locked yet is not a
  regression a pull request introduced.
- **`mutation.yml` runs a `cosmic-ray` session against
  `src/btclib_node/interpreter.py`**, the consensus entry point
  `CLAUDE.md`'s architecture section names as what validates (issue
  #287): a new `mutation` dependency group carries `cosmic-ray`, and
  `.github/mutation/interpreter.toml` is the one scope so far. It gates
  nothing either: a surviving mutant is a missing test, not a regression
  a merge caused.
- Issue #287's own remaining rows are triaged rather than landed by this
  change: `vendored-vectors.yml` already existed, `os-windows`,
  `py-arm-authority`, `alignment` and `os-ubuntu` are declined with the
  measurement on the issue itself, `pypi-install` waits on issue #286's
  release workflow, and `integration-bitcoind`'s own design is issue
  #374.

### `docs/source/` exists, hand-written, with a gate that builds it

- **`docs/source/` documents this package** (issue #264): one Sphinx
  page per package directory, `tests/unit/docs_test.py` failing where a
  shipped module gains no automodule stanza or a stanza names a module
  the tree has lost. `.readthedocs.yaml` and `.github/workflows/docs.yml`
  build it with `-W --keep-going`, joining `test` and `lint` as a gate.
- **Every module and package under `src/btclib_node/` carries a
  docstring** (issue #264): `D100`/`D104` are selected in
  `pyproject.toml` for the shipped package, at zero findings.
  `D101`/`D102`/`D103`/`D107` -- the class, method, function and
  `__init__` narration this pass did not attempt -- are issue #373's.

### This repository is tier 1: a release path exists, published on nothing yet

- **`.github/workflows/release.yml`, tag-triggered, calls `test` and
  `lint` before publishing to PyPI** (closes #286): the maintainer's
  decision to promote this repository to tier 1 supersedes the tier-2
  state PR #171 landed one day before this issue was filed, and section
  2 of btclib-org/.github's README measures the tier from
  `pyproject.toml` and this file alone. It does not call `docs`:
  `.github/workflows/docs.yml` (issue #264) is reporting-only, not a
  required check, and this workflow calls only what already gates a
  merge — the same reason it does not call `os-macos.yml` either, both
  argued in `release.yml`'s own header comment, which names what would
  make `docs` earn a job here. `test.yml`'s new `dist` job builds
  the sdist and the wheel, normalizes the sdist's member metadata
  (`.github/scripts/normalize_sdist.py`), writes a CycloneDX bill of
  materials over the two files (`.github/scripts/generate_sbom.py`),
  and checks them with `twine check --strict`, `check-wheel-contents`
  (`[tool.check-wheel-contents]`'s own `package` diffing the wheel
  against `src/btclib_node` in both directions) and `pyroma --min 10` —
  on every pull request, not only at a release, so a defect a release
  would hit is one a review already has. `check-sdist` diffing the
  sdist against git is unchanged, already running as a pre-commit hook
  since PR #265.
- **The packaging smoke test installs the wheel with `--no-deps` and
  checks only its metadata** (issue #381): PyPI's own btclib has no
  release carrying `btclib.p2p.negotiation`, which
  `src/btclib_node/download.py` imports unconditionally, so an ordinary
  `pip install btclib-node` cannot resolve today whatever floor
  `pyproject.toml` declares. Filed rather than fixed here, no floor
  this bundle could declare making PyPI satisfy it.
- **`RELEASING.md` and `RELEASE_NOTES.md` return** (closes #286): the
  two files PR #171 removed under the tier-2 decision, back under the
  shape section 2 gives a tier-1 repository. `CONTRIBUTING.md`'s *A
  version, and no release* becomes *A release path, and nothing
  published on it yet* to match: `pyproject.toml` still declares
  `0.1.0`, and the `pypi`/`testpypi` environments `RELEASING.md`'s
  *One-time setup* describes do not exist yet, so nothing here changes
  what a checkout runs or how a caller reaches this code — no version is
  published, no tag is cut.
- **`SECURITY.md` carries this node's own policy, not the organization's
  shared one** (closes #286): the file section 2 of btclib-org/.github's
  README owes a tier-1 repository. `README.md`'s *Limitations, not
  vulnerabilities* moves there, its own reason for holding them —
  publishing nothing for a policy of its own to travel with — no longer
  holding; `README.md` ends with the "actively supported by" line
  section 2 gives every publishing repository's own instead.

### `Connection`'s ping state is one step against the two threads that touch it

- **`send_ping` and `callbacks.pong` share `Connection._ping_lock`**
  (closes #357): each writes or clears `ping_sent` and `ping_nonce` as a
  pair, and the lock makes each pair one step against the other's, so a
  `send_ping` landing between `pong`'s own two clears no longer has its
  fresh nonce overwritten by the sentinel `0` -- `send_ping`'s own
  comment is careful never to send it -- which used to discourage and
  drop a peer for a protocol violation this node caused.
- **`_prune_stale_connections` reads `ping_sent` once, not twice**
  (closes #357): its own `elif` re-read the attribute, so a `pong`
  clearing it to 0 between the two reads dropped a peer that had just
  answered its ping.
- **`Connection.stop`'s idempotence comment argues from both of this
  node's threads, not from one** (closes #360): `stop` is called from
  `Node`'s own thread as well as from `P2pManager`'s, and what makes two
  threads passing its guard together harmless is that a second
  `self.task.cancel()` and a second `socket.close()` each take an early
  return with no further effect, and `status` only ever moves toward
  `Closed` -- not, as the comment argued before, several calls queued
  within one turn of a single loop. No code changes.

### This node depends on btclib's libsecp256k1 bindings, closing issue #361

- **`btclib`'s dependency line carries the `secp256k1` extra** (closes
  #361): signature verification runs through btclib's libsecp256k1
  bindings instead of the pure-Python elliptic-curve path, a
  single-thread speedup. `CLAUDE.md`'s *Following Bitcoin Core* is why
  this is a dependency of this package rather than an extra of it: Core
  does not treat its own libsecp256k1 as optional, and this is
  consensus-critical arithmetic rather than a convenience. The bindings
  do not make threads run in parallel under the GIL, so nothing about
  how this node is concurrent changes. This package's dependency tree
  now carries compiled, per-platform extensions, where it carried none.

### `P2pManager`'s connection dicts stay consistent across threads

- **`promote_connection` and `remove_connection` share `_connections_lock`**
  (closes #358): each moves a connection between `connections` and
  `pending_connections` in two statements, and the lock makes each of
  those two statements one step against the other's, so a connection
  `remove_connection` decides to stop is never left in neither dict
  with nothing having stopped it.
- **`_maybe_dial_more_peers` reads `connections` and `pending_connections`
  under the same lock** (closes #355): a connection `promote_connection`
  is moving between the two dicts is read as being in one of them
  rather than, for the width of the move, in neither, which is what let
  this method draw and redial a peer it already holds.
- **`_maybe_dial_more_peers` reads its own `live` count under the lock
  too, separately from the snapshot above** (closes #367): two unlocked
  `len()` calls apart, a `connections` before the write and a
  `pending_connections` after the pop, could each miss the same moving
  connection and undercount a node that already has enough peers,
  which is what let this method dial past the target it was told to
  stop at.
- **`get_peer_info` reads `p2p_manager.connections.copy()`** (closes
  #356): the live dict is popped from on `P2pManager`'s own loop, every
  pass of `manage_connections`, and a pop landing mid-iteration on
  `Node`'s own loop raised `RuntimeError: dictionary changed size
  during iteration` out of a client's `getpeerinfo`.
- **`P2pManager.send` reads `connections.get(connection_id)`** (closes
  #359): an `in` check followed by a subscript is two dict operations,
  not one, and a connection popped between them raised `KeyError` out
  of the send.

### `RpcManager.stop()` gives `accept` a step first, closing issue #362

- **`stop()` runs the loop one step before gathering the tasks it is
  about to cancel** (closes #362): the same gap issue #353 closed in
  `P2pManager.stop()`, in `RpcManager.stop()` instead. `server()`'s own
  `accept` is a task of its own, reached by `stop()`'s sweep directly and
  not only through `server`'s task cascading a cancel onto it.
  `Task.cancel` on a task whose own awaited future is already resolved
  still forces `CancelledError` into it on the next step, discarding a
  socket the kernel had already handed over with nothing left to close
  it — `server`'s own except arm already guards this for a cancel
  arriving through its shield (#323), and could not guard a cancel that
  reaches `accept` directly, which is what `stop()`'s own sweep did on
  every call. `run_until_complete(asyncio.sleep(0))` before the sweep,
  repeated until a step changes nothing and run only where this
  manager's thread was ever started, lets `accept` return normally into
  `create_connection` instead. The step is repeated rather than run
  once: the future it waits on is resolved from another thread with no
  guarantee its own delivery lands within a single step, `asyncio.shield`
  adding a callback hop of its own between `accept` completing and
  `server` resuming with its result.

### The importable package sits under `src/`, closing issue #343

- **`btclib_node` moves under `src/btclib_node/`** (closes #343): a
  package at the repository root is on `sys.path` whenever anything runs
  from that root, so an import could resolve to the checkout instead of
  to the installed distribution, which is what section 2 of the
  organization's standard moves it to avoid. `[tool.uv.build-backend]
  module-root = ""` is deleted rather than changed: that key existed
  only to override the backend's own default, which is `src/`. Every
  path this repository's own configuration and prose named the package
  by moves with it: `[tool.ruff.lint.per-file-ignores]`'s two keys,
  `[tool.mypy] files`, and the citations in `CLAUDE.md`, `CONTRIBUTING.md`
  and `REVIEWING.md`. `[tool.coverage.run] source` keeps naming
  `btclib_node` unchanged -- an importable name rather than a path,
  which `coverage` resolves through the installed package wherever it
  sits.

### `stop()` gives `accept` a step before cancelling it, closing issue #353

- **`stop()` runs the loop one step before gathering the tasks it is
  about to cancel** (closes #353): `server()`'s own `accept` is a task
  of its own, reached by `stop()`'s blanket sweep directly and not only
  through `server`'s task cascading a cancel onto it. `Task.cancel` on a
  task whose own awaited future is already resolved still forces
  `CancelledError` into it on the next step, discarding a socket the
  kernel had already handed over with nothing left to close it —
  `server`'s own except arm already guards this for a cancel arriving
  through its shield (#312), and could not guard a cancel that reaches
  `accept` directly, which is what `stop()`'s own sweep did on every
  pass. One `run_until_complete(asyncio.sleep(0))` before the sweep, run
  only where there is a task to give it to, lets `accept` return
  normally into `create_connection` instead — a task the same loop
  already knows how to close, on its next pass.

### `CHANGELOG.md`'s lint derogation is gone, and `codespell` now fixes

- **The two-comment directive disabling MD022 and MD032 at this file's
  head is deleted** (closes #328): `markdownlint-cli2` already fixes in
  place, so a `merge=union` join dropping the blank line between two
  `###` sections is repaired on the hook's next run instead of failing
  a gate with nothing to fix it, and the two rules apply to this file
  again.
- **The local `changelog-heading-blank-line` check the derogation
  needed alongside it is gone too**, redundant now that MD022 and MD032
  cover the same defect and repair it rather than only reporting it.
- **`codespell` gains `--write-changes`**, joining `markdownlint-cli2`
  and `typos` as hooks that fix in place instead of only reporting;
  `yamllint` has no fix mode and stays check-only.

### The vendored blockfilters.json pin is re-checked weekly, closing issue #327

- **`vendored-vectors.yml` re-checks the vendored pin weekly** (closes
  #327): `tests/_data/README.md` pinned
  `tests/unit/chainstate/_data/blockfilters.json` to a Bitcoin Core
  commit and blob by hand, with a documented procedure and nothing
  running it without being asked. `.github/scripts/check_vendored_pin.py`
  runs that procedure -- `git hash-object` against the recorded `blob`,
  a `commits?path=` query against the recorded `commit` -- and the
  workflow fails where either has moved. It carries no issue-tracking
  machinery of its own and no `issues: write`: unlike btclib's
  `check_vendored_vectors.py`, which serves a README with dozens of
  vendored headings, this tree's one entry is answered by the same
  contents-read, fail-on-drift shape `links.yml` and `bootstrap-dns.yml`
  already use for a scheduled report on something outside this tree's
  own commits.

### `select` gains `FBT`, closing issue #341

- **`select` gains `FBT` (flake8-boolean-trap)** (closes #341): the
  third and last of issue #341's own real-judgment rounds, and the only
  one of the three that changes call sites outside the file each fix is
  declared in -- a bare `True`/`False` positional argument
  (`f(a, True)`) is unreadable without the signature open beside it, so
  the fix is a keyword-only parameter and every caller updated to name
  it, not a rename. 82 findings, 34 in `btclib_node/`, all fixed by
  making the parameter keyword-only (`*,`) after confirming, by
  grepping every call site in the tree first, that none of them was
  positional to begin with: `RevBlock`/`BlockInfo`'s own
  `check_validity`, `BlockIndex.set_downloaded`'s `downloaded`,
  `Config.__init__`'s four booleans (and, since every other parameter
  there was already keyword at each of its own call sites too, made
  keyword-only throughout rather than only the four this round is
  about -- which also drops the `PLR0913` neighbour noqa's own
  `PLR0917`, positional-count no longer being a thing to measure),
  `Logger.__init__`'s `debug`, `Mempool.get_missing`/`get_tx`'s
  `wtxid`, `Connection.__init__`'s `inbound` and `.stop`'s
  `cancel_task`. `P2pManager.create_connection`'s own `inbound`
  parameter needed the same fix one layer up, at its own three
  production call sites and two in `tests/`.
- **`BlockInfo(...)`'s own two dataclass-constructor call sites** (not
  a declaration `FBT001`/`FBT002` reach, since `ruff` does not flag an
  auto-generated dataclass `__init__`) **and two
  `btclib` calls this tree does not own the signature of**
  (`SendCmpct(False, 1)`, one site in `p2p/callbacks.py`, and
  `Tx.serialize(True)`, 23 sites across `rpc/callbacks.py` and the
  test suite): all `FBT003`, fixed by naming the argument at the call
  site since the callee's own signature is not this tree's to change.
- **A pytest test function's own parametrized boolean parameters**
  (`p2p/callbacks_test.py`'s
  `test_what_a_peer_said_about_relay_lands_on_the_connection`): made
  keyword-only like every other finding here, verified by running the
  test directly rather than assumed safe -- `pytest.mark.parametrize`
  calls a test function by keyword, matching fixture and parameter
  names, so this is not a special case needing its own reasoning.
- **`socket.socket.setblocking`, 16 sites across
  `tests/unit/p2p/connection_test.py` and
  `tests/unit/rpc/connection_test.py`**: declined, the one `FBT003`
  finding this round could not fix either way -- verified directly
  (`socket().setblocking(flag=False)`) that the standard library's own
  C-level method takes no keyword arguments at all, so there is no
  call-site rewrite available and no signature of this tree's own to
  change. A new `pyproject.toml` per-file-ignore for each of the two
  files, since every `FBT003` remaining in each is this one call.
- **Every `FBT` fix's call sites were found by grep across the whole
  tree first, not by an editor's local references**, the same
  discipline issue #284's own builtin-shadowing round used: `scripts/`
  turned up two more `Logger(...)` positional calls `mypy` caught
  (`Too many positional arguments for "Logger"`) that a `tests/`- and
  `btclib_node/`-only grep had missed, which is the reason `mypy`
  stays the round's own real check rather than the grep that started
  it.

### `select` gains `ANN`, the first of issue #341's own three real-judgment rounds

- **`select` gains `ANN` (flake8-annotations)** (issue #341): the
  first of a further three rounds past issue #340's own mechanical
  sweep, each needing a per-site read rather than a rule's own safe
  fix. `ANN001`/`ANN201` and the rest of the family that names a
  missing annotation report zero -- issue #166 already annotated every
  signature in `btclib_node`, `tests` and `scripts` -- so `ANN401`
  (`Any` disallowed as a parameter or return annotation) is the whole
  of what this round found and the whole of what it is about.
- **`ANN401`, `btclib_node/` (6 of 77)**: read individually rather than
  bulk-suppressed, since a placeholder `Any` and a genuine escape hatch
  look the same to the rule. Two were dead code, not merely
  under-annotated: `log.Logger.__init__`'s `**kwargs` forwarded to
  `logging.Logger.__init__`, whose own signature (`name`, `level`) both
  arrive already named, and no caller in this tree ever supplied a
  third; `rpc.connection.JSONEncoder.__init__`'s `*args` forwarded to
  `json.JSONEncoder.__init__`, which is keyword-only and never receives
  a positional argument from `json.dumps`'s own construction of it.
  Both parameters removed. Two narrowed to `object`, since nothing at
  either site does more than store or type-dispatch on the value:
  `rpc.errors.json_type_name`'s `value` (looked up in a `dict[type,
  str]` by `type(value)` alone) and `rpc.main.is_valid_rpc`'s `request`
  (narrowed by `isinstance` before anything else touches it) --
  `rpc.errors.error_msg`'s `request_id` the same way, echoed into the
  response unread. The one genuine escape hatch left, `JSONEncoder`'s
  own `**kwargs`, keeps `Any` with a `noqa` and a comment: it forwards
  to `json.JSONEncoder.__init__`'s own heterogeneous keyword arguments
  (`bool`, `int | None`, `tuple[str, str] | None`, a callable), not one
  type to narrow to.
- **`ANN401`, `tests/` (71 of 77)**: read individually rather than
  swallowed as a family. 67 of the 71 are one idiom, run throughout the
  whole suite -- a `SimpleNamespace` standing in for a production type
  across the fields one scenario needs and none it does not, or a spy
  forwarding blindly to the real callable it wraps (`socket.socket`,
  `asyncio.Task.cancel`, `Loop.run_until_complete`) -- and are declined
  together, in a new `pyproject.toml` `"tests/**"` per-file-ignore
  entry that names the idiom and the 4 exceptions rather than the rule
  wholesale, following the existing entries for `PLR0913`/`PLR2004`/
  `S101`/`S311`. The 4 exceptions are narrowed rather than declined:
  `filter_index_test.py`'s `CountingDb.write_batch` returns exactly
  what `KeyValueStore.write_batch` returns
  (`AbstractContextManager[KeyValueStore]`), not an unrelated `Any`;
  `init_test.py`'s `ARecordingPool.starmap` stores its arguments
  without calling them, so `Callable[..., object]` and
  `Iterable[Iterable[object]]` say what the real ones would without
  claiming more; `rpc/callbacks_test.py`'s `a_chain_index_node` already
  built its return value with `cast("Node", ...)` before returning it,
  so the function's own declared return type could simply be `Node`
  rather than repeating `Any` one line later.

### `select` gains `ARG`, issue #341's second real-judgment round

- **`select` gains `ARG` (flake8-unused-arguments)** (issue #341): 138
  findings, read individually rather than assumed to be the same shape
  as each other -- 33 in `btclib_node/`, 105 in `tests/`/`scripts/`.
- **`btclib_node/__init__.py`'s `stop_handler`**: `signal`/`frame`
  renamed `_signum`/`_frame`, `signal.signal`'s own calling convention
  being the reason both are unread, and the renamed pair no longer
  shadows the `signal` module this file also imports.
- **`p2p/callbacks.py` (13 findings) and `rpc/callbacks.py` (15
  findings)**: every one read against its own dispatch table
  (`callbacks`/`handshake_callbacks` in each file, called uniformly by
  `p2p.main`'s `handle_p2p`/`handle_p2p_handshake` and by `rpc.main`'s
  `handle_rpc`) -- an argument one handler does not read is required by
  the table's own shared signature, not a mistake, and renaming it to
  `_` would lose that signature's own documentation for a table that
  calls every handler the same way. Declined together, in a new
  `pyproject.toml` per-file-ignore for each of the two files, rather
  than 28 near-identical `noqa` comments.
- **`p2p/manager.py`'s `manage_connections`**: its own `loop` parameter
  was read nowhere in the body -- `asyncio.run_coroutine_threadsafe`
  takes the loop as its own second argument, not through the coroutine
  it schedules -- and was removed outright, at its one call site and in
  the one test that drove it directly.
- **`p2p/manager.py`'s `broadcast_raw_transaction`**: `fee` stays,
  declined with a `noqa` next to the comment already there explaining
  why -- the same caller already recorded it in `Mempool.add_tx`, which
  is where the BIP133 feefilter check reads it from.
- **`p2p/messages/errors.py`'s `Reject.parse`**: `check_validity` stays
  for the same reason `serialize` beside it does (exempt from this rule
  as an `@override`) -- every btclib `Payload`'s own parse/serialize
  pair takes it, called polymorphically without a caller knowing which
  subclass is on the other end, even though `Reject`'s own wire format
  has nothing it would gate.
- **`tests/`/`scripts/` (105 findings)**: read individually rather than
  swallowed as a family. 103 are monkeypatch replacements or fakes
  matching the real callable's own signature they stand in for -- a
  dispatch handler swapped into a `callbacks` dict the same way
  production's own are, `Connection.send`, `update_chain`,
  `Loop.getaddrinfo`/`sock_accept`, `random.expovariate`, and the like.
  The other 2, both in `tests/functional/p2p/block_filters_test.py`: a
  pytest fixture (`mark`) requested by two tests only for its own
  construction's side effect, not for a value either test reads. All
  105 declined together in the existing `"tests/**"` per-file-ignore
  (`ARG001`/`ARG002`/`ARG005` added alongside the entries already
  there, the comment naming both reasons rather than only the larger
  one).

### `select` gains `TC`, the first family beyond issue #284's own reference selection

- **`select` gains `TC` (flake8-type-checking)** (issue #340): the
  largest and most mechanical family of a further sweep past issue
  #284's own reference selection, every rule ruff ships rather than
  only the standard's own list. A typing-only import moved under `if
  TYPE_CHECKING:` costs nothing at runtime on this tree's `>=3.14`
  target -- PEP 649's lazy annotation evaluation is native there, and
  `FA` (already selected) confirms it: only one file in the whole tree
  still needs `from __future__ import annotations`, not every module a
  typing-only import would otherwise reach.
- **`TC006` (a `typing.cast()` call's own type argument, quoted)**: 46
  sites, `btclib_node/` and `tests/` alike, `ruff`'s own safe fix
  applied after reading every site for the one thing that fix is not
  safe for -- a type expression spanning more than one line, or
  carrying a comment on any line but its last. None did.
- **`TC001`/`TC002`/`TC003` (an application, third-party or standard
  library import moved under `TYPE_CHECKING`)**: 109 sites, 57 of them
  in `btclib_node/`. `ruff`'s own static check is what decides an
  import is typing-only, and the risk worth checking by hand rather
  than trusting it is a name that check missed at runtime -- an
  `isinstance`, a decorator, a default value. Read individually before
  the fix: `socket` (`p2p/connection.py`, `rpc/connection.py`),
  `pathlib.Path` (`chainstate/__init__.py`, `log.py`, `p2p/address.py`)
  and `concurrent.futures.Future`/`collections.abc.Callable`
  (`p2p/connection.py`, `rpc/connection.py`, `rpc/manager.py`) are each
  the kind of name that is often both a type and a runtime constructor
  or protocol elsewhere, and each is confirmed, by reading every one of
  its own uses in the file that flagged it, to appear only in an
  annotation there. `uv run ruff check --unsafe-fixes` applied to the
  rest without a per-site read of every one of the 109 -- the real
  check is not a grep's own guess at completeness but the tree actually
  running: `mypy` (which would refuse a name it cannot resolve inside
  an annotation) and the full suite (which would raise `NameError` the
  moment a runtime use reached a name that no longer exists outside
  `TYPE_CHECKING`) both ran clean afterward, unchanged in count from
  before this round.

### `select` gains `G`, `N` and `PERF`, and records what stays out

- **`select` gains `G` (flake8-logging-format), `N` (pep8-naming) and
  `PERF` (Perflint)** (closes #340): the second, smaller round of a
  further sweep past issue #284's own reference selection, alongside
  `TC` (issue #340's own first round).
- **`G004` (a `logger.*` call's own f-string argument)**: 15 sites, all
  in `btclib_node/`. 7 converted by `ruff`'s own safe fix; the other 8
  -- every one where the interpolated value was an attribute access, a
  method call, or an `!s`/`!r` conversion rather than a bare name --
  converted by hand, read individually so the `%s`/`%r` argument still
  says what the f-string said. `{e!s}` becomes a plain `%s` argument
  (str, exactly what `!s` already called); `{self!r}` becomes `%r`
  (repr, exactly what `!r` already called) -- checked directly, not
  assumed, since `%` and `!` conversions do not otherwise line up
  one-for-one. Five tests, across `tests/unit/p2p/connection_test.py`
  and `tests/unit/p2p/callbacks_test.py`, stood a bare `list.append` in
  for the `Logger` method they read a message back from -- one
  argument only, where the production call each of them exercises now
  passes two. `tests/helpers.py` gains `log_recorder`, applying the
  same `%` formatting `logging` itself would before recording the
  message, so each assertion reads the same finished string either
  way.
- **`N806` (a function-scoped name not lowercase), one renamed, one
  declined**: `interpreter.py`'s `check_transactions` had `FLAGS`,
  read as a possible function-scoped constant the way #284's own round
  3 declined `SIM300`'s constant-on-the-left -- but this one differs
  per call, keyed on the block `index` passed in, so it is an ordinary
  write-once local rather than a constant; renamed to `flags`.
  `tests/unit/rpc/manager_test.py`'s `AcceptResult` is a type alias,
  not a variable, and PEP 8's own naming convention for one is
  CapWords, the same as a class -- declined, `noqa: N806`.
- **`PERF401`/`PERF403` (a manual loop or dict-update where a
  comprehension already says the same thing)**: `download.py`'s
  `_pending_and_waiting_blocks` and
  `tests/unit/p2p/messages/init_test.py`'s `payload_classes`, both
  `ruff`'s own unsafe fix, reformatted to this tree's own 88-column
  wrap afterward.
- **`pyproject.toml`'s `select` comment gains a paragraph on what this
  sweep declined, and why, so it is not resurveyed**: `COM`
  (flake8-commas, 361 findings, all `COM812`) and `Q` (flake8-quotes,
  zero findings) both redundant against `ruff-format`, which already
  enforces trailing commas and quote style on everything it reflows --
  the same reasoning `line-too-long` is already ignored for. `EM`
  (flake8-errmsg, 13 findings) and `SLF` (flake8-self, 140 findings)
  are, measured, entirely inside `tests/` -- zero of either in
  `btclib_node/` -- a whitebox-test idiom rather than a defect. `INP`
  (flake8-no-pep420, 16 findings) is `scripts/**` and a handful of
  `tests/**/p2p` directories lacking their own `__init__.py`; whether
  `scripts/` is meant to be an importable package is a packaging
  decision this tree has not made, not a lint fix to make it for.

### `select` gains `PL` and `C90`, closing issue #284

- **`select` gains `PL` (pylint) and `C90` (mccabe)** (closes #284): the
  last two families of the standard reference selection this issue
  measured against this tree, `select` now carrying every one of the 13
  families that reference selection named and this tree did not
  already have (`A`, `B`, `BLE`, `C90`, `ERA`, `FURB`, `PL`, `PT`,
  `RET`, `RUF`, `SIM`, `T20`, `TRY`), plus `RUF043` and
  `unspecified-encoding`, both of which predate this sequence.
- **`PLR2004` (magic-value-comparison), a named constant where the
  number is a concept and a decline where it is not**: `block_index.py`
  and `download.py` share `MAX_DOWNLOAD_WINDOW`, one bound read from
  both ends of it -- how many candidates `get_download_candidates`
  hands back, and how far `block_download` lets the download frontier
  run ahead of the active chain before backing off. `download.py` gains
  three of its own, `_BLOCK_STALL_EVICTION_TIMEOUT` and
  `_BLOCK_STALL_DISCONNECT_TIMEOUT` (this tree's own coarser pair, not
  Core's adaptive `BLOCK_STALLING_TIMEOUT_DEFAULT`/`_MAX`, checked
  directly against `net_processing.cpp` rather than assumed to match)
  and `_MAX_CONCURRENT_REQUESTS_PER_BLOCK`; `p2p/manager.py` gains
  `_IDLE_TIMEOUT` (this tree's own bound too, shorter than Core's
  `TIMEOUT_INTERVAL`, same check). `p2p/callbacks.py`'s own `2000`
  becomes `MAX_HEADERS_RESULTS` -- not a new constant of this tree's
  own, but one `btclib.p2p.limits` already exports, matching Core's own
  name for it (`net_processing.h`). Three sites decline, each for a
  reason checked rather than assumed: `block_index.py`'s block-locator
  step doubling at `10` matches Core's own unnamed literal
  (`LocatorEntries`, `src/chain.cpp`) -- naming it here would claim a
  meaning Core's own algorithm never gave it; `p2p/address.py`'s `4` is
  `ipaddress`'s own IPv4 version number, already named by being IPv4;
  `rpc/callbacks.py`'s `2` is `getrawtransaction`'s own third
  positional, already named by the help string raised two lines above
  it. `tests/**` gains a `PLR2004` per-file-ignore for the rest: a
  test's own literal expected value is not a magic number needing a
  name.
- **`PLC0415` (import-outside-top-level), fixed at every site rather
  than declined anywhere**: most were a test needing the module object
  itself, not a name out of it, to `monkeypatch.setattr` against --
  `import btclib_node.p2p.callbacks as cb` and
  `import btclib_node.rpc.callbacks as cb`, repeated once per test that
  needed it where one shared, hoisted import serves every one of them
  just as well. A few were genuinely redundant: `tests/unit/init_test.py`
  re-imported `btclib_node` inside three functions when the module was
  already bound at the top of the file, and
  `tests/functional/rpc/chain_test.py`/`tx_test.py` each imported
  `bitcoin_core_rpc`/`btclib.fetch.bitcoin_core` inside their one user
  apiece for no reason a circular import or an optional dependency
  gives -- both packages are ordinary, always-installed dependencies,
  and every other import in both files already lives at the top.
- **`C901`/`PLR0912`/`PLR0915` (complex-structure, too-many-branches,
  too-many-statements), read function by function before deciding
  whether each was refactored or tangled by the problem it solves**.
  Every flagged production function turned out to be a sequence of
  distinct stages sharing little state across them, not a single
  decision tree that would fragment badly -- so each split cleanly into
  named helpers, the caller left reading as the stages it always was:
  `main.py`'s `update_chain` into `_ready_fork`, `_blocks_to_add`,
  `_rev_blocks_to_remove`, `_finalize_fork` and
  `_reconcile_mempool_for_reorg`; `download.py`'s `tx_download` into
  `_queue_announcements_for_received_txs` and `_request_wanted_txs`,
  and its `block_download` into `_refresh_block_window`,
  `_evict_stalled_connections`, `_pending_and_waiting_blocks` and
  `_request_new_block_work`; `__init__.py`'s `Node.run` into
  `_drain_message_queues` and `_step_chain`;
  `chainstate/block_index.py`'s `add_headers` into
  `_validate_header_batch` and `_insert_pending_headers`, matching the
  function's own existing comment about the two stages never
  interleaving; `config.py`'s `Config.__init__` loses its chain-string
  resolution to a module-level `_resolve_chain`; `p2p/manager.py`'s
  `manage_connections` into `_prune_stale_connections`,
  `_maybe_prune_active_addresses` and `_maybe_dial_more_peers`;
  `rpc/callbacks.py`'s `get_raw_transaction` into `_parse_txid`,
  `_parse_optional_block_hash` and `_find_transaction`. None of these
  changes the behaviour of the function it came out of; the tests that
  already covered each are what confirm that, unchanged.
- **`PLR0913`/`PLR0917` (too-many-arguments,
  too-many-positional-arguments), declined at both of its two
  production sites, each with a `noqa` and a reason rather than a
  reshaping that would only rename the same problem**: `Config.__init__`
  takes eleven parameters because `Config` is eleven independent
  settings and every call site already reads it by keyword (checked --
  no call site in this tree passes it positionally); nesting them into
  sub-objects moves the same count behind an extra name apiece for
  callers who do not have it today. `contextual.py`'s
  `assert_valid_in_context` takes six for the same reason, matching
  Core's own `ContextualCheckBlockHeader`'s parameter set for the same
  check. `tests/**` gains a `PLR0913` per-file-ignore of its own: every
  remaining site is a test double's own builder, one keyword-only
  argument per field of the object it stands in for.
- **`PLW2901` (redefined-loop-name), fixed**:
  `block_index.py`'s `init_from_db` reused its own loop variable `key`
  for the row's suffix after splitting off the row's prefix; the
  suffix is `block_hash` now, and the loop variable is never
  reassigned.

### `select` gains `TRY` and `BLE`, and this tree gets its own exceptions

- **`select` gains `TRY` (tryceratops) and `BLE` (flake8-blind-except)**
  (issue #284): weighed each finding as a possible latent bug rather
  than a style preference, per the round's own brief, since a narrowed
  `except` can turn "logged and the loop continues" into "propagates
  and kills the loop" for whichever exception nobody meant to let
  through.
- **`btclib_node/exceptions.py` gains ten classes**, grouped by what
  actually went wrong rather than by which module raises it —
  `ChainstateInconsistencyError` alone answers every site downstream
  of `update_chain` finding that its own index promised something the
  data underneath it does not have, across `btclib_node/main.py`,
  `block_db`, `block_index`, `utxo_index` and `filter_index` alike, and
  `StoreClosedError` answers both of `db.py`'s own "the store is
  closed" sites. `UtxoIndex.add_block`'s own two raises share the same
  two messages as two of `ChainstateInconsistencyError`'s sites
  ("prevout not found", "prevout already spent in this batch") but not
  the invariant: `add_block` runs against a freshly-downloaded
  candidate block, before `check_transactions` has validated anything
  about it, so a failure there is a peer's bad block, not this tree's
  own bug — `InvalidBlockInputError(ValueError)` is that one, matching
  `MissingPrevoutError`'s own shape (the same failure, checked at a
  different point in the pipeline: mempool reprocessing after a
  reorg) rather than `ChainstateInconsistencyError`'s. `TRY002` (`raise
  Exception(...)`) is what asked for a class; `TRY003` (a message
  built at the `raise` site rather than carried by the class) is why
  each one takes a `message` in its own `__init__` instead of a bare
  `pass` — checked directly against ruff itself rather than assumed:
  the message still has to be assigned to a variable before the
  `raise` and not written as a literal there, which is `TRY003`'s own
  actual test and not quite what its rule name or its docstring's own
  prose suggest.
- **`Config.__init__`'s `chain` validation gains two of those classes**:
  `UnknownChainError(ValueError)` for a `chain` string this tree does
  not recognise, and `InvalidChainTypeError(TypeError)` for a `chain`
  that is neither a `Chain` nor a `str` (`TRY004`, prefer `TypeError`
  for a wrong *type* over `ValueError`) — the existing
  `unknown chain` test needs no change, being a `ValueError` still, and
  the `Config(chain=None)` test is updated to expect `TypeError`.
- **Four `except Exception:` sites narrow to the specific exception the
  surrounding code actually depends on**: `rpc/main.py`'s
  `get_connection` to `KeyError` (a plain `dict` lookup, nothing else
  the miss could raise), two `tests/unit/db_test.py` race tests to the
  new `StoreClosedError`, checked against `KeyValueStore.close`'s own
  locking that no other exception can reach that `except` under the
  race either test drives, and `scripts/test_errors.py`'s own
  `TxOut.parse` loop to `BTClibException`, confirmed directly against
  the installed btclib rather than assumed.
- **Seven `except Exception:` sites stay exactly as broad as they were,
  each now with a `noqa: BLE001` and a comment saying why**:
  `p2p/connection.py`'s own two, `rpc/connection.py`'s, and
  `rpc/callbacks.py`'s own two — `get_peer_info`'s (alongside `S112`: a
  peer disconnecting mid-lookup can surface as more than one socket
  error depending on timing and platform, and every one of them means
  the same "skip this peer, ask the next") and `testmempoolaccept`'s
  (answering one entry per transaction, Core's own contract for that
  RPC, so an unexpected failure on one is that entry's own
  reject-reason rather than the whole batch's answer). The two
  connections' own read loops
  (`p2p/connection.py`, `rpc/connection.py`) are not guarding a shared
  event loop from a crash — both are scheduled through
  `run_coroutine_threadsafe`, and asyncio isolates one scheduled
  coroutine's own unhandled exception from the loop and from every
  other connection on it regardless of how broad this `except` is.
  What each catch buys instead, checked against its own connection's
  structure rather than assumed: `p2p/connection.py`'s
  gives every failure the same explicit `stop()` its own outer
  `finally` would eventually reach anyway, in the one place that also
  decides whether to discourage the peer; `rpc/connection.py`'s is the
  *only* place `self.client` gets closed for a failure in that method,
  there being no outer `finally` there to fall back on. Two more stay
  broad for an unrelated reason of their own: propagating a
  caller-supplied call's own exception back unchanged
  (`tests/helpers.py`'s `call_within`) or a whole directory of local,
  unpredictable fixtures' own failures back as one line each
  (`scripts/test_errors.py`'s driver loop).
- **Three `raise`s stay declined rather than rewritten, each with a
  `noqa` and a reason**: `block_index.py`'s header-batch validation
  raises `BTClibValueError` from inside the same `try` its own
  `except` logs and re-raises every refusal through — this one and
  `assert_valid_in_context`'s own — so abstracting it out against
  `TRY301` would split one log line into two shapes for no reader's
  benefit; `rpc/connection.py`'s own bounds check on `Content-Length`
  is the same shape, one `except Exception` downstream answering
  everything this method can raise. A third, in
  `tests/unit/p2p/manager_test.py`, keeps raising a bare `OSError`
  against `TRY003` rather than a class of this tree's own: `_bind`
  itself catches `OSError`, and a test double standing in for the real
  `socket` module has to raise what that module would.

### `select` gains `B` and the rest of `RUF`

- **`select` gains `B` (flake8-bugbear) and `RUF` (the rest of ruff's own
  rules)** (issue #284). `RUF043` moves out of its own `extend-select`
  entry now that the family it belongs to is selected outright.
- **`Node.__init__`'s `config: Config = Config()` default becomes
  `config: Config | None = None`, with the body constructing a `Config`
  when the caller omits one** (`B008`): a mutable default built once at
  `def`-time and shared across every call that does not override it is
  the shape `B008` warns about, and nothing here relies on that sharing.
- **`Config.__init__`'s `chain: Chain | str = Main()` default becomes
  `chain: Chain | str = DEFAULT_CHAIN`, a module-level singleton next to
  the existing `DEFAULT_MIN_RELAY_FEERATE`** (`B008`, the same rule
  `Node.__init__`'s fix above answers: `Config` defines its own
  `__init__` rather than one `@dataclass` generates, so its parameter
  default is ordinary `B008` and not `RUF009`). `Config(chain=None)`
  still raises `ValueError`, unchanged: the singleton replaces the call
  in the signature, not the type it stands for.
- **`BlockInfo`'s `status: BlockStatus = BlockStatus(1)` default becomes
  `status: BlockStatus = BlockStatus.valid_header`** (`RUF009`, the
  dataclass-field form of the same warning: `BlockInfo` takes the
  `__init__` `@dataclass` generates), `BlockStatus` being an `IntEnum`
  whose members are already singletons.
- **The two loop variables `RevBlock.deserialize` never reads become
  `_`** (`B007`), matching the `for _ in range(...)` shape already used
  elsewhere in this tree.
- **A `zip()` over two sequences a caller can prove are the same length
  now says so with `strict=True`**, in `update_chain` and in a
  functional compact-filters test, wherever `B905` finds one and the
  invariant that makes the lengths equal is read out of the code around
  it rather than assumed.
- **Two `zip(chain, chain[1:])` pairs in `tests/unit/helpers_test.py`
  become `itertools.pairwise(chain)`**, the idiom `RUF007` names and the
  same change `B905` would otherwise have asked a `strict=` of.
- **Three `wait_until(lambda: ...)` calls that close over a `for` loop's
  own variable keep the pattern, against `B023`, each with a `noqa` and
  a reason**: `wait_until` itself now carries a comment arguing why the
  closure is safe — it is read and discarded within the iteration that
  built it, since `wait_until` returns or raises before the loop can
  reach its next iteration and rebind the name — and the three call
  sites point back to it rather than repeating the argument.
- **Unpacked tuple elements a test never reads become `_`** (`RUF059`),
  across `tests/functional/p2p/block_filters_test.py` and
  `tests/unit/rpc/main_test.py`, checked per site that the name really
  is unread rather than reached some way ruff's own static view misses.
- **Stale `# noqa: BLE001` and `# noqa: SLF001` comments come off**,
  in `tests/unit/db_test.py`, `tests/unit/p2p/manager_test.py` and
  `tests/unit/rpc/manager_test.py`: neither `BLE` nor `SLF` is selected,
  so nothing was ever suppressing anything at those lines, and `RUF100`
  is what now says so.

### `select` gains `A`, flake8-builtins

- **`select` gains `A` (flake8-builtins)** (issue #284): every local
  variable, loop variable and function argument named `hash` or `id`
  that shadowed the Python builtin of the same name is renamed to say
  what the value actually is — `block_hash` wherever it is one, and
  `connection_id`, `request_id` or `txid` depending on which "id" a
  given site actually holds — rather than to a generic disambiguator
  like `hash_` or `id_`.
- **None of these are wire-facing renames.** `error_msg`'s `id`
  parameter feeds the JSON-RPC 2.0 response's own `"id"` key, and
  `get_peer_info`'s loop variable feeds `getpeerinfo`'s own `"id"` field
  — both dict keys stay the literal string `"id"`, untouched; only the
  Python identifier that holds the value on its way there changes.
  Checked for every renamed parameter that no caller in this tree passes
  it by keyword (`grep -rn` for `hash=` and `id=` across `btclib_node/`,
  `tests/` and `scripts/`), so no call site needed updating alongside a
  signature.
- **`RevBlock`'s own `hash` field, and `Connection.id` (both `p2p` and
  `rpc`), are untouched.** `A` selects `A003`
  (builtin-attribute-shadowing) too, and it reports nothing here,
  checked directly rather than assumed: it only fires where an
  attribute is referenced ambiguously with the builtin it shadows (a
  callable resolved as the attribute instead of the type, in the rule's
  own example), which a plain data attribute assigned once in
  `__init__` and only ever read is not.

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

### `select` gains `ERA` and `T20`, and a stale local drops out

- **`select` gains `ERA` (eradicate, commented-out code) and `T20`
  (flake8-print)** (issue #284). `scripts/reset_chainstate.py` and
  `scripts/reset_download.py` are each a template edited by hand before
  a run rather than a program run as-is — their reset is commented out
  on purpose, one block per operation, kept current against the block
  index's own API across refactors rather than left to rot — so
  `per-file-ignores` turns `ERA001` off for both files instead of a
  `noqa` per block. `scripts/test_errors.py` gets the same treatment
  for `T20`: its `print`s are a manual diagnostic run's progress and
  result, not debug residue.
- **`scripts/chains/mainnet.py`'s commented `debug=True,` carries a
  reason and a `noqa`**: unlike `signet.py`/`testnet.py`'s live
  `debug=True`, this is the one script that leaves its log quiet by
  default, meant to be uncommented by hand when chasing a
  mainnet-specific problem.
- **`btclib_node/rpc/callbacks.py`'s citation of Core's own
  `JSONRPCError` call carries a `noqa`**: it is C++ prose citing
  `src/rpc/server.cpp` for `get_block_hash`'s missing-argument error
  shape, and `ERA001` reads the parenthesised call as commented-out
  Python.
- **`BlockIndex.generate_block_candidates`'s unused `# header =
  block_info.header` is gone.** Nothing downstream of it ever read
  `header`; the assignment goes back to an optimisation that removed
  what used to need it and left the line commented rather than deleted.

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
  handshake rather than once per batch, so the lookup is an endpoint-keyed
  index kept alongside the list rather than a per-call scan of it, which
  would turn many handshakes against the one peer quadratic overall.

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
  requiring somebody to run it by hand** (#199):
  `changelog-heading-blank-line` fails on any `###` line in
  `CHANGELOG.md` not preceded by a blank one, which `markdownlint-cli2`'s
  own MD022 does not catch here since it is disabled for this file. It
  runs as part of the same `uv run pre-commit run --all-files` a rebase
  already asks for, not as an installed git hook: `CONTRIBUTING.md`'s
  *The gate is not installed as a git hook* is why, `.git/hooks` being
  shared by every worktree of this repository.

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
