# Contributing

What this repository holds in common with the others of the organization
— the toolchain, the lint gate, the tool tables behind it, the workflow
set and the branch rules — is stated once in the
[organization's standard](https://github.com/btclib-org/.github), each
rule with the alternative it was decided against. It binds this
repository, so a change departing from it is a divergence, and one filed
as an issue in btclib-org/.github rather than here: a difference between
two repositories belongs to neither of them.

**This file is the same in every repository of the organization up to
its last section.** What is true of one tree only — the commands that
build its environment, the gates it runs, which of its workflows decide
a merge — is under that heading, and the comparison stops there.

How the organization decides, and who holds which role, is
[`GOVERNANCE.md`][governance]; what it intends to do, and what it
deliberately does not, is [`ROADMAP.md`][roadmap]. Both are the
organization's, one copy each beside the standard.

## The issue tracker

Where an issue is filed, and what an alignment finding has to name, is
[the standard's *What this repository is*][s-what]: an issue spanning
repositories, or whose subject is the standard, goes to
[btclib-org/.github](https://github.com/btclib-org/.github/issues), and
one about this tree alone stays here.

A finding noticed while doing something else goes where `REVIEWING.md`'s
*What is filed, and what is not* says, for an author as much as for a
reviewer: a pull request answering two questions cannot be accepted for
either.

An issue labelled `good first issue` is one set aside for a first
contribution: small and self-contained.
[One search][good-first] lists the open ones across the organization, so
a newcomer need not know in advance which repository to look in.

## Documentation and comments

[Section 9 of the standard][s9] is the prose style beyond *Write
plainly* below, and it governs the prose this tree ships — comments,
docstrings and markdown. It is not restated here: a second wording is
the one that goes stale, which is that section's own *One fact in one
place*.

A commit message is prose this tree ships too, though section 9 does not
say so: [the only merge method the rule accepts][s11] puts it on `main`
as the landing commit's body, so what is written in one is read there
long after the branch is gone.

## Working with an LLM

**Development here is LLM-based.** Most issues, code and reviews are
written by agentic sessions that the maintainer directs and reads.
Humans are welcome, as long as they are comfortable working that way.

**A contribution is yours, whoever wrote it.** Read every line before
you open the pull request, understand it, and be ready to answer a
review without the model. Text or code you have not read, or cannot
explain, is closed without review.

**For a pull request the agent opened, whoever approves it reads every
line and answers for it as their own**, the agent being the pull-request
agent of [`GOVERNANCE.md`][governance]'s *Roles*. The maintainer,
landing it through the bypass without an approval, reads every line and
answers for it instead, the landing being their own act; requiring an
approval even where the bypass is kept is the alternative set aside. The
agent signs off nothing, since a bot cannot certify the [Developer
Certificate of Origin][dco], so the approval, or that landing, is the
certification.

**The `agent` label hands an issue to that agent**, in a repository
that calls its implement workflow. [Section 11][s11]'s *Labels and issue types*
says who applies it, [section 10][s10] what the agent does with an issue
it admits, and the workflow's header which issues it takes, what answers
a `decision`, and the switch that turns it on.

**Write plainly**, in the tree and on the forge: the point first, plain
words, one fact per sentence. On the forge, the title and the first
sentence carry the point; a body is at most two short paragraphs before
any code block or template section; a comment is about 250 words, and
never more than 300, with no list and no heading; a review's summary
takes the shape `REVIEWING.md`'s *The verdict* gives it.

## Pull requests

What `main` accepts, and what it refuses to everyone, is [section 11 of
the standard][s11]. Run the gates locally before opening anything —
the last section of this file says which they are — because CI runs
exactly them, so a red run there is a local run that was not done.

**Every commit of a pull request carries a `Signed-off-by:` trailer
naming its author**, which certifies the [Developer Certificate of
Origin][dco]. `git commit -s` adds it, and `git rebase --signoff <base>`
adds it to commits already made. The `Sign-off` check is required, so
only an administrator can merge a pull request with a commit the check
refuses: *Landing it* says why `--admin` waits for no required check.
The check's failure prints the command that adds the trailer. [The
standard's *Signatures*][s-sigs] says why a signature does not replace
it, and which commits the `Sign-off` job skips.

What a pull request's title and description have to say about the issues
it closes, and why a manual link in the Development panel is a trap
neither of them shows, is [the standard's *What a pull request says it
is*][s-title]. Read it before opening one; it is the rule most often
found broken after the fact.

**Before it is opened, the branch's own commit subjects and bodies are
read against that same rule.** The description does not exist yet to
disagree with them, and [the standard][s-title] has the command that
scans the branch's own commit text for a verb in front of a reference.

**The two spellings are named here as well as there, against [section 9's
*One fact in one place*][s9]**, the paragraph above naming the section
and not the forms, which are the half a citation is got wrong in:
`(closes #N)` cites an issue the change closes, wherever the citation
sits — the title, the commit subject where [*Merge method*][s11] makes
that the thing that lands — and `(issue #N)`
cites, in those same places, an issue the change advances and does *not*
close. One token holds one meaning whichever file it sits in, so the
pair is chosen by what is true of the change rather than by which file
is being written, and a tree's own landed subjects are not what to copy
it from: nothing already landed is rewritten, so what a repository wrote
before the rule stays where it is.

`REVIEWING.md` is the standard a review is written against, and is this
file's other half. Read before opening a pull request, it is what the
pull request will be answered against.

A pull request adds no entry to `CHANGELOG.md` or `RELEASE_NOTES.md`, a
release's own pull request excepted ([section 9][s9], [section 12][s12]).

### One subject, opened as soon as it is written

A pull request answers one question. Issues that share a subject are one
pull request, closing each of them; issues that do not are one pull
request each, however small either of them is.

It is opened the moment it is written and verified — not held for the
previous one to be reviewed or to land, and not batched with the next. A
batch arrives as one reviewing job with several subjects, which is the
shape that costs the most to read; a finished pull request held back is
review that could have started and did not. A security advisory's fix is
the exception, held back as [section 12][s12]'s *A security advisory is
worked in this order* says.

Working this way stacks branches, which is fine and costs one rule: a
child whose base was amended is moved with the old base named,

```shell
git rebase --onto <new-base> <old-base-sha> <child>
```

because a plain rebase replays the base's old commit inside the child,
and the forge then shows the base's old text as additions with nothing
red anywhere. Read the child's diff afterwards rather than trusting the
rebase, and retarget each child onto `main` as its parent lands.

### The review

A review is given promptly and on local evidence. It does not wait for
CI, does not report a check as a finding, and does not discuss a run at
all: whether CI is green is the author's business, once, at landing time.

The exchange is anchored to a sha rather than to a branch, a branch being
free to move under a review:

- the author hands off by naming the sha pushed and the evidence run
  against it, then leaves that head alone;
- the reviewer answers with findings — where, what is wrong, how they
  know it, and whether each is blocking;
- the author accepts what is reasonable, declines the rest with a reason
  in the thread, pushes the answer without waiting for CI, and resolves
  each thread once answered;
- the reviewer reopens a thread whose finding is still blocking, and
  re-reviews the delta rather than the branch.

**What ends the loop is the ack of record.** A reading that says what
it found and delivers no verdict is a review too and ends nothing; [the
standard's *Review*][s-rev] has which is which, and `REVIEWING.md` has
how each is written. A disagreement that survives a second exchange
goes to the maintainer instead of into a third round.

**The ack of record is not the approval *Landing it* asks for**: it is
a bot's review of type COMMENT ([the standard's *Review*][s-rev] says
whose).

### Landing it

CI is read once, and this is where: on the head that lands. `main` does
not ask that head to be up to date with it, and [section 11][s-branch]
says why. So a branch is rebased only where it conflicts with `main`. A
rebase that moved nothing but the base leaves the ack standing; one that
resolved a conflict does not, that resolution being a change no reviewer
has seen.

When `main`'s CI is red after a merge, its repair takes priority over
every other pull request, because until it lands `main`'s runs cannot
tell a new failure from the one already there. A pull request armed with
`--auto` (below) stays armed, and others keep landing meanwhile. Holding
every landing for a repair costs more than the break it guards against,
so the risk of landing on a red `main` is accepted
(btclib-org/.github#1777).

A pull request whose required check is red only because `main` was red
is closed and reopened once the repair lands (`gh pr close <n>`, then
`gh pr reopen <n>`). A re-run would test the old merge commit, and the
reopen runs the checks on the merge GitHub then holds. GitHub refreshes
that merge lazily, so the reopen's runs can still test the old one. A
run's log is read before the run is relied on: its line `HEAD is now at
<sha> Merge <head> into <base>` names the merge it tested, and where
`<base>` lacks the repair, the pull request is closed and reopened
again. No push is made, so its approvals and its ack of record stand.
Closing disarms `--auto`, so it is armed again once the checks are
green. The reopen also runs the review bot again, and changes that
review requests are answered like any other review's.

Then squash, [the only method the rule accepts][s11].

**It lands once somebody other than its author has approved that
head**, through `gh pr merge` pinned to it:

```shell
gh pr merge <n> --squash --match-head-commit <the head the checks ran on>
```

With `--auto` added, GitHub runs the same merge once the approval and
the checks are in.

**Where the repository has a bypass, the maintainer may land without
that approval, through it**: [`GOVERNANCE.md`][governance]'s *How a
decision is made* says which repositories.
`--admin` turns off `gh`'s own refusal, and GitHub applies
the bypass to the merge. `enforce_admins` is off, so it waits for no
required check either:

```shell
gh pr merge <n> --squash --admin \
  --match-head-commit <the head the checks ran on>
```

Without `--admin`, `gh pr merge` refuses a head with no approval, saying
it `is not mergeable: the base branch policy prohibits the merge`.

**The pin is not optional**, in either command. Reading the ack and
merging are two calls, and the head is free to move between them — the
push that would move it comes out of the same round the verdict does.
Unpinned, the command takes whatever sits at the head when it runs;
pinned, it refuses a head that has moved, and a round lost that way is
cheaper than a tree nobody has read reaching `main`.
*The review* above anchors the exchange to a sha and [section 11][s11]
has an ack name one: the pin is that rule reaching the call that
performs the landing.

**Verify what landed rather than trusting the answer**, the signature
[the standard asks for][s-sigs] being a valid one rather than a
particular signer's:

```shell
gh api repos/{owner}/{repo}/commits/main \
  --jq '.commit.verification | {verified, reason}'
```

**What it closed is read again here too, from the landed sha rather
than from the pull request**: [the standard's *What a pull request says
it is*][s-title] has the second read, and why the first alone does not
reach a squash subject composed after it runs.

The forge deletes the head branch itself, per the setting section 11
names. What is still yours is bringing every checkout sitting on `main`
up to date,
that being where the next session starts from and a stale one being where
a branch gets built on a base that has moved. `REPOSITORY.md` carries the
settings and why they are what they are.

[s-what]: https://github.com/btclib-org/.github#what-this-repository-is
[s11]: https://github.com/btclib-org/.github#11-github-settings
[s9]: https://github.com/btclib-org/.github#9-prose-comments-and-docstrings
[s10]: https://github.com/btclib-org/.github#10-workflows
[s12]: https://github.com/btclib-org/.github#12-releasing
[s-title]: https://github.com/btclib-org/.github#what-a-pull-request-says-it-is
[s-rev]: https://github.com/btclib-org/.github#review
[s-sigs]: https://github.com/btclib-org/.github#signatures
[s-branch]: https://github.com/btclib-org/.github#branch-protection-and-rulesets
[dco]: https://developercertificate.org/
[governance]: https://github.com/btclib-org/.github/blob/main/GOVERNANCE.md
[roadmap]: https://github.com/btclib-org/.github/blob/main/ROADMAP.md
[good-first]: https://github.com/search?q=org%3Abtclib-org+label%3A%22good+first+issue%22+is%3Aopen&type=issues

## This repository in particular

Everything above is the same file in every repository of the
organization; everything below is this one's, and the comparison stops at
this heading.

### The environment and the gates

uv is the only thing that has to be installed; it fetches the interpreter
`.python-version` pins and every dependency group itself. There is a
project here and it is installed, so the gates run through `uv run` and
not through the `uvx` a tree with no project needs:

```shell
uv sync
uv run pytest || exit $?
git add -A && uv run pre-commit run --all-files || exit $?
uv run pre-commit validate-config .pre-commit-config.yaml || exit $?
uv run --locked --no-default-groups --group docs \
    sphinx-build -W -n -b html docs/source docs/build/html || exit $?
if grep -rn 'href="#\.\.\?/' docs/build/html --include='*.html'; then
  echo "::error::the links above resolve to no page (unresolved relative path)"
  exit 1
fi
found=0
pin=$(sed -n 's/.*reusable-docs\.yml@\([0-9a-f]*\).*/\1/p' .github/workflows/docs.yml)
served=$(mktemp -d) &&
git clone --quiet --filter=blob:none --sparse \
    https://github.com/btclib-org/.github "$served" &&
git -C "$served" sparse-checkout set .github/scripts &&
git -C "$served" checkout --quiet "$pin" &&
uv run --no-project --python 3.15 \
    "$served"/.github/scripts/check_included_links.py docs/build/html || found=$?
rm -rf "$served"
test "$found" -eq 0 || exit "$found"
```

`--all-files` means every file git tracks, so a file that is new and not
yet staged is not one of them: run it unstaged and the hooks pass over
exactly the files most likely to fail them. Staging first is what makes
the local gate answer the same question pre-commit.ci does.

The documentation build is the one no hook reads reStructuredText for: a
docstring docutils cannot parse fails it with every hook green -- a name
ending in an underscore is a reference to a link target, which is what
the double backticks around a literal like ``NODE_`` are for.

**Not `--only-group docs` in place of the documentation build's own
`--no-default-groups --group docs`.** The two read like the same request
and are not: `--only-group` excludes the project along with every other
group, so autodoc's own import of `btclib_node` raises
`ModuleNotFoundError` under `-W` on a `.venv` that does not already have
it installed. Where the same `.venv` was synced earlier in the session
by `uv sync` or `uv run pytest`, the project is already there and the
build succeeds without `--only-group` ever having installed it, so the
command's outcome tracks the `.venv`'s own history rather than the tree
it is meant to check. `docs.yml` runs the form above for the same
reason, next to the same warning.

The `grep` after the build is that gate's second step. What myst renders
for a target it cannot resolve is an anchor on the page it is already on
-- an id nothing defines -- and the build fails on it only through the
`myst.xref_missing` warning `docs/source/conf.py` suppresses nowhere,
having resolved every link the included root files carry ahead of myst's
own resolver; the grep asks the same question of the HTML, where a
suppression cannot hide the answer. Two spellings answer for every local
destination, `.pre-commit-config.yaml`'s `local-link-prefix` hook holding
each of them to the `./` or `../` prefix.

`check_included_links.py` after the grep is that gate's third step. It is
btclib-org/.github's: the commands above check it out at the commit
`docs.yml` pins, as `docs.yml`'s job does. Its docstring says what it
refuses.

`validate-config` is worth running before pushing a change to the hook
config: it catches what a wrong `types_or` tag or a malformed entry would
otherwise turn into a red lint job.

**Check exit codes, not filtered output.** `pre-commit run ... | grep -v
Passed` hides a failure, and `grep` finding nothing exits 1, which is not
the gate's answer to anything.

**The gate is not installed as a git hook.** `pre-commit install` writes
into the common git directory, which every worktree of this repository
shares: `git -C <worktree> rev-parse --git-path hooks` answers with the
same directory in each. So one session installing it installs it for
every other. Run the gate by hand before committing.

Every statement and every branch is covered, and `uv run pytest` fails if
any stops being. A run narrowed by a path, `-k`, `-m`, `--deselect`,
`--ignore`, `--ignore-glob` or `--last-failed` is not the run a floor
over the whole suite is a claim about, so it is not held to one —
`tests/conftest.py` is where that is decided, and where anything else
narrowing a run is added. `--cov-fail-under` asked for explicitly still
applies.

The tests marked `slow` are skipped by a run without `--run-slow`, and
`tests/README.md`'s *Slow tests* says why. To run them, and only them:

```shell
uv run pytest -m slow --run-slow --no-cov
```

Every test is bounded, too. A node that stops answering fails the test
that built it, named, with a stack of every thread it left running,
instead of holding the run open until something outside it gives up. The
limit is `timeout` in `pyproject.toml`, measured against the slowest test
there is and reasoned about where it is set.

### What gates a merge, and what only reports

`lint.yml`, `test.yml`, `docs.yml`, `integration-bitcoind.yml` and
`sourcery.yml` produce the required checks, and `REPOSITORY.md` reads the
rule back from the endpoint rather than restating it. So a diff does not
reach a review without having passed them or passing them beside it on the
same sha, which is the reliance `REVIEWING.md` provides for.

| workflow | when | what it varies |
| --- | --- | --- |
| `test` | pull request, push to main | `windows-latest`, report-only `3.14t` |
| `lint`, `docs` | pull request, push to main | — |
| `integration-bitcoind` | pull request, push to main, weekly | — |
| `claude-review` | pull request, and `@claude` in a comment | — |
| `api-break` | pull request | — |
| `sourcery` | pull request | — |
| `verified-signatures` | push to main | — |
| `codeql` | pull request, push to main, and weekly | the languages |
| `os-ubuntu` | weekly | `ubuntu-latest`, `ubuntu-24.04-arm` |
| `os-macos` | weekly | `macos-latest` |
| `deps-latest` | weekly | dependencies upgraded |
| `deps-oldest` | weekly | the floor interpreter, dependencies at their floors |
| `slow` | weekly | — |
| `bootstrap-dns` | weekly | — |
| `scorecard` | weekly, push to main | — |
| `links` | weekly | — |
| `mutation` | weekly | — |
| `fuzz` | weekly | — |
| `vendored-vectors` | weekly | the vendored files' pins |
| `pypi-install` | weekly, a release | what PyPI serves |
| `sdist-rebuild` | weekly | the latest release's sdist, rebuilt |
| `release` | a tag, and a rehearsal | the workflows it calls |

Which workflows that last row covers is
`grep -n 'uses: \$/\.github/workflows/' .github/workflows/release.yml`,
not a list here. Which day each of the rest runs is section 10 of [the
organization's standard](https://github.com/btclib-org/.github), and
not this file's to restate. A defect only a sweep can see sits on `main`
until that sweep runs, at most a week.

A `when` cell leaves out a `pull_request` trigger that `paths:` filters.
Such a workflow also runs on a pull request that changes a file its
`paths:` names. The `test` row's push to main also leaves out what its
`paths-ignore:` names.

`lint.yml` runs the hooks `.pre-commit-config.yaml` declares, so there
is no second list of tools and versions to keep in step; its invocation
differs from the one above only in being `--locked` and in printing what
a fixing hook would have written. `docs.yml` runs the same build as the
environment section above. The coverage floor `pyproject.toml` declares
holds on the `ubuntu-latest` cells of `test`. A pull request that touches
only the root prose skips the suite and reports the skip as a pass, which
is what keeps an aggregate check from blocking on a run that never
happened.

`claude-review` gates nothing: a review that gates a merge would make a
model's judgement a branch rule. `api-break` gates nothing either: it adds
a label. So few checks gate a merge because the plan caps the jobs the
whole organization may run at once, and `REPOSITORY.md`'s *Plan-gated
settings* records that ceiling. `verified-signatures` reports after each
push to `main` whether every commit pushed is `verified`; [the standard's
*Signatures*][s-sigs] has the rule.

### Mutation testing

`mutation.yml` asks the question coverage cannot: a line the suite
executes is not a line the suite checks. It gates nothing and runs
weekly; the configuration is the single source of the scope and the
test command.

```shell
uv run --locked --no-default-groups --group test --group mutation \
    cosmic-ray baseline .github/mutation/interpreter.toml
uv run --locked --no-default-groups --group test --group mutation \
    cosmic-ray init .github/mutation/interpreter.toml interpreter.sqlite
uv run --locked --no-default-groups --group test --group mutation \
    cosmic-ray exec .github/mutation/interpreter.toml interpreter.sqlite
uv run --locked --no-default-groups --group test --group mutation \
    cr-report --surviving-only --show-diff interpreter.sqlite
```

The session writes each mutation into `src/btclib_node/interpreter.py`
and restores it afterwards, so nothing else may read the file while it
runs. `interpreter.py` is the consensus entry point ARCHITECTURE.md's
*Validation* names as what validates. Each scope is a `.toml` of its own
in `.github/mutation/`, the way `btclib-org/btclib`'s own holds one per
profile, and is run the same way with its own name in place of
`interpreter`.

`test-command` in `.github/mutation/interpreter.toml` runs with `-n0`,
overriding `addopts`' own `-n auto`.

**A mutation offered as proof that a test can fail must reach the process
that runs the test.** `addopts` carries `-n auto`, so the test runs in an
xdist worker. A mutation applied in the controlling process (a
monkeypatch before `pytest.main`) never reaches a worker, which imports
the module from disk, and the test passes. Edit the file and revert it,
proving the revert with `git diff --stat`. cosmic-ray edits the file, so
its mutants reach a worker either way.

### Building the distribution and checking it

`test.yml`'s `dist` job reads the sdist and the wheel with the `check`
dependency group. A pull request builds its own files there; on a
release the job reads the ones `reusable-build.yml` built and signed,
which `release.yml`'s publish jobs download. Running the same commands
here answers before a pull request does.

```shell
export SOURCE_DATE_EPOCH=$(git log -1 --pretty=%ct)
uv build
uv run --no-project --python 3.14 .github/scripts/normalize_sdist.py dist/
uv run --locked --only-group check twine check --strict dist/*
uv run --locked --only-group check check-wheel-contents dist/*.whl
uv run --locked --only-group check pyroma --min 10 dist/*.tar.gz
```

The epoch and the normalizer come first because they rewrite the sdist's
member metadata, so what the three checks read is the archive that job
would hand on. `--only-group` is right here where it is wrong for the
documentation build above: these three read `dist/` and none of them
needs `btclib_node` installed.

`check-sdist` is not among them: `.pre-commit-config.yaml` runs it as a
hook, so the lint gate already asks it, and it builds an archive of its
own rather than reading the one above. `dist/` is ignored, so building
here does not dirty the tree that gate reads.

### A release path, and what it has published

`.github/workflows/release.yml` exists and section 2 of
[the organization's
standard](https://github.com/btclib-org/.github/blob/main/README.md)
measures a repository's tier from that file and `pyproject.toml` alone,
so this tree is tier 1 — the decision
[ISS btclib-org/btclib-node#286](https://github.com/btclib-org/btclib-node/issues/286)
records. `RELEASING.md` and `RELEASE_NOTES.md` are the two files
section 2 gives a tier-1 repository, and this tree keeps both.

It publishes to PyPI. The `pypi` and `testpypi` pair of
`RELEASING.md`'s *One-time setup* is what uploads —
`REPOSITORY.md`'s *The two publishing environments* reads that pair
back — and between releases `project.version` is the shape
`RELEASING.md`'s calendar scheme takes then: the month a cycle is open
on, with no day, which is what `version-check` refuses on a tag. Which
versions are published, and which tags there are, is read from the
index and the repository rather than from this file:

```shell
curl -s https://pypi.org/pypi/btclib-node/json |
  python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)["releases"]))'
gh api repos/btclib-org/btclib-node/tags --jq '.[].name'
```

`CHANGELOG.md` starts where the record starts rather than where a tag
does, for the reason its own introduction gives.

A tag is cut with `git tag -s`. `tag-integrity` does not check the tag's
own signature: `REPOSITORY.md` has what it refuses and the call that
reads it back. Its one rule is `required_signatures`, with neither
`non_fast_forward` nor `deletion`, so nothing refuses a tag's deletion,
and what follows is kept by hand.
**The tag of a published version is never deleted or cut again**: the
version stays on PyPI whatever a tag does, an index refusing a version
that has been uploaded once, and deleting its tag takes away the only
thing that says which commit it was built from. A tag nothing has been
published from can be deleted and cut again.

Whether anything has been published is read from the simple API, the
index's own state. The JSON API of the `curl` call above is a cache of
it, and can still lack a version the index serves
(btclib-org/btclib-node#545):

```shell
curl -s -H "Accept: application/vnd.pypi.simple.v1+json" \
  https://pypi.org/simple/btclib-node/ |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["versions"])'
```

Run it again before an absence is trusted: the index can lack a version
just after its upload (btclib-org/btclib-node#546).

`RELEASING.md` is the whole of the procedure, from the one-time
publisher registration through the tag command itself — naming the
release commit explicitly, which the argumentless `git tag -s v<version>`
this paragraph could show instead does not — through what to do if a
release goes wrong; it is not repeated here.

### Following Bitcoin Core

Where this tree reimplements something Bitcoin Core also does — a
constant, an eviction order, the error an RPC answers a refusal with —
it **matches Core's behaviour**, and the comment beside it names the
commit Core was read at. It may differ in two ways. The first is the
language: Python-native, fluent or efficient in Python where Core's own
line is shaped by C++, never a design weighed against Core's on its own
merits. The second is a departure the maintainer decided, which
SECURITY.md's *Where this node departs from Bitcoin Core* lists and
argues, and which the code it changes cites. Refusing v1 by default is
one (btclib-org/btclib-node#1190). What differs from Core in consensus or
in relay is a difference the network sees, so the default is not a matter
of taste.

Write the citation as `at bitcoin/bitcoin@<sha>` with `at` on the same
physical line as the sha: ruff's `ERA001` reads a comment line at a
time, and `at` glued to the citation is never valid Python
(btclib-org/btclib-node#471). A citation in a docstring needs none of
this.

`src/btclib_node/db.py`'s docstring is the worked example of the
Python-native axis: taking LevelDB's own fork through `rocksdict`, a
typed wheel on this tree's interpreter, over Core's vendored LevelDB is
a packaging choice, and the docstring argues the store against Core's on
those terms.

Mimicry is of the observed behaviour end to end, not of a local
`catch`: where a layer below differs, the same behaviour can need
different code above it, and that is still matching. `db.py`'s store
verifies a per-block checksum on every read
(btclib-org/btclib-node#641), the guarantee LevelDB gives Core's
`CDBWrapper::Read` before its deserialize `try` runs. A checksum-clean
`utxo-` record that `UtxoIndex` reads by outpoint and cannot deserialize
stops the node, as Core's `CCoinsViewDB::GetCoin` does (`src/txdb.cpp`,
at bitcoin/bitcoin@f0de87f040, the v32.1 tag), and
`UtxoIndex._stored_prevout`'s docstring says how.

A capability Core has that this tree lacks is a gap to close, not a
constraint to design around, wherever the reason is a library fact
rather than a decision: the store's per-block checksum is RocksDB's own
(btclib-org/btclib-node#641), taken in place of a stdlib `sqlite3` that
ships no checksummed VFS (btclib-org/btclib-node#637).

**A convention of this tree is not on that axis.** Where Core defines
the surface — an RPC's field names and what they mean, a message's
semantics — consistency with the rest of this codebase is no reason to
answer differently from Core: the reader on the other side is a client
written against Core.

**Units are where that bites hardest.** A feerate here is satoshis per
kvB wherever one is emitted or read, and Core's `getmempoolinfo` answers
`mempoolminfee` in BTC per kvB. A field of Core's that this tree answers
takes Core's unit: the client reading it was written against Core, so
the internally consistent answer is wrong by eight orders of magnitude
with nothing to say so. The rule stops where the encoding is not this
tree's to pick: BIP133's `feefilter` carries satoshis per kvB on the
wire because BIP133 says so.

What is not legitimate is the silent kind. Where the behaviour departs
from a source the code cites, the code says so and says what forced it:
a reader who finds a citation and an unexplained difference cannot tell
a decision from an oversight. Reading Core's own source is part of
writing the divergence, because what gets reproduced is what Core does
rather than what a report said it does.

Core is read from a local checkout brought forward with a fetch and a
fast-forward on a clean `master`, never written to, rather than fetched
a file at a time: a raw fetch can come back truncated with nothing to
say so, missing the function a divergence question is about. The sha the
checkout lands on is what the comment cites.

The other sha a citation may carry is a released Core's, with the
release's tag name beside it: a claim about the bitcoind this tree is
tested against, rather than about Core's tip, is cited at that release's
tag commit. `.github/workflows/integration-bitcoind.yml` pins the
release, and a citation reads it there. The tag name is what tells such
a citation from a stale read, because ancestry does not: a sha read from
an out-of-date `master` is an ancestor of `master` all the same.

Where that release and Core master behave differently, consensus and
relay follow the release: the network runs released Cores, so a peer on
the wire is never speaking master's tip. A peer-refusal rule may go
stricter than the release ahead of that behaviour landing in one,
provided the code argues the divergence against the release and cites
master's sha beside it: refusing a misbehaving peer more readily changes
nothing an honest peer's messages achieve.

### A defect rooted in btclib

A defect rooted in btclib's behaviour is fixed in btclib, not worked
around here. The node branch that needs the fix does not land before the
btclib release that carries it, and the issue it closes carries the
`waits-release` label. Until that release, the code states the
divergence as *Following Bitcoin Core* asks, and cites the btclib issue.
`Tx.assert_valid`'s check order is the case in point
(btclib-org/btclib#2417).

### Ignored state that a second run or a second worktree shares

- **A second `pytest` in the same rootdir, even `--help`, erases a
  running suite's `.coverage*`** (btclib-org/btclib-node#191). Give each
  run its own data with `COVERAGE_FILE=$(mktemp -d)/.coverage`.
- **A `.venv` reused from another worktree imports that worktree's
  `src/`.** `btclib_node.pth` in `site-packages` is an absolute path
  written by `uv sync`. Read `btclib_node.__file__` with the interpreter,
  directory and environment of the test run, before trusting its result.
- **`.hypothesis/` is per worktree and replays recorded failures.**
  The `default` and `thorough` profiles in `tests/conftest.py` name no
  `database`, so every failing example is recorded under
  `.hypothesis/examples` and replayed on later runs in that worktree. A
  red property test that does not reproduce elsewhere is not a flake for
  that reason alone; use a fresh worktree
  (btclib-org/btclib-node#835).
