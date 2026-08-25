# Contributing

What this repository holds in common with the others of the organization
— the toolchain, the lint gate, the tool tables behind it, the workflow
set and the branch rules — is stated once in the
[btclib-org repository standard](https://github.com/btclib-org/.github),
each rule with the alternative it was decided against. It binds this
repository, so a change departing from it is a divergence, and one filed
as an issue in that repository rather than here: a difference between two
repositories belongs to neither of them.

**This file is the same in every repository of the organization up to
its last section.** What is true of one tree only — the commands that
build its environment, the gates it runs, which of its workflows decide
a merge — is under that heading, and the comparison stops there.

## The issue tracker

Where an issue is filed, and what an alignment finding has to name, is
[the standard's *What this repository is*][s-what]: an issue spanning
repositories, or whose subject is the standard, goes to
[btclib-org/.github](https://github.com/btclib-org/.github/issues), and
one about this tree alone stays here.

A finding noticed while doing something else is filed, not carried.
`REVIEWING.md`'s *Every collateral finding becomes an issue* is the whole
of what to do with one, and it applies to an author as much as to a
reviewer: a pull request answering two questions cannot be accepted for
either.

## Documentation and comments

[Section 9 of the standard][s9] is the prose style, and it governs the
prose this tree ships — comments, docstrings and markdown. It is not
restated here: a second wording is the one that goes stale, which is
that section's own *One fact in one place*.

A commit message is prose this tree ships too, though section 9 does not
say so: [the only merge method the rule accepts][s11] puts it on `main`
as the landing commit's body, so what is written in one is read there
long after the branch is gone.

## Pull requests

What `main` accepts, and what it refuses to everyone, is [section 11 of
the standard][s11]. Run the gates locally before opening anything —
the last section of this file says which they are — because CI runs
exactly them, so a red run there is a local run that was not done.

What a pull request's title and description have to say about the issues
it closes, and why a manual link in the Development panel is a trap
neither of them shows, is [the standard's *What a pull request says it
is*][s-title]. Read it before opening one; it is the rule most often
found broken after the fact.

`REVIEWING.md` is the standard a review is written against, and is this
file's other half. Read before opening a pull request, it is what the
pull request will be answered against.

`CHANGELOG.md` gets an entry for anything a reader would notice, and the
release notes move only for something a user has to *act* on, in the
repositories that publish.

### One subject, opened as soon as it is written

A pull request answers one question. Issues that share a subject are one
pull request, closing each of them; issues that do not are one pull
request each, however small either of them is.

It is opened the moment it is written and verified — not held for the
previous one to be reviewed or to land, and not batched with the next. A
batch arrives as one reviewing job with several subjects, which is the
shape that costs the most to read; a finished pull request held back is
review that could have started and did not.

Working this way stacks branches, which is fine and costs one rule: a
child whose base was amended is moved with the old base named,

```shell
git rebase --onto <new-base> <old-base-sha> <child>
```

because a plain rebase replays the base's old commit inside the child,
and the forge then shows the base's old text as additions with nothing
red anywhere. Read the child's diff afterwards rather than trusting the
rebase, and retarget each child onto `main` as its parent lands.

### The landing queue

Where more than one pull request is open against this repository, only
one is carried to `main` at a time: rebased onto the tip, reviewed on
that head, and landed, while every other one waits, untouched, for its
turn. This governs which of several *already open* pull requests reaches
`main` next; *One subject, opened as soon as it is written* above governs
the moment before that, when a finished one is opened — the two do not
conflict, since a pull request is still opened without delay and still
waits its turn once several are open.

The reason is CI throughput, not the ack a waiting pull request keeps —
`REVIEWING.md`'s *The verdict* states what an ack belongs to, and
*Landing it* below states which rebase voids one. Every rebase queues
this repository's whole check matrix against the organization's ceiling
on concurrent jobs, so rebasing every waiting pull request after each
landing spends that capacity on runs the next landing invalidates
anyway, and delays the one pull request that is actually next: work
spent on a pull request that is not next is work that delays the one
that is.

Order is cheapest and least contended first, most invasive last, so that
a large change does not sit at the head blocking everything behind it.

The maintainer may declare a bounded exception — several pull requests in
flight against one repository, for a named piece of work — trading the
cost above for throughput; it is recorded as a comment in
[btclib-org/.github](https://github.com/btclib-org/.github/issues), by
*The issue tracker* above, and holds only for the work it names.

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
  in the thread, and pushes the answer without waiting for CI;
- the reviewer resolves the threads they opened, that being what says a
  finding is closed, and re-reviews the delta rather than the branch.

**What ends the loop is the ack of record**, and the author does not
supply their own. A reading that says what it found and delivers no
verdict is a review too and ends nothing; [the standard's *Review*][s-rev]
has which is which, and `REVIEWING.md` has how each is written. A
disagreement that survives a second exchange goes to the maintainer
instead of into a third round.

### Landing it

CI is read once, and this is where. Rebase onto `main`'s tip, push that
head so the checks run on the tree that will land, and only then wait for
them: checks read before a rebase describe a tree nobody is landing. A
rebase that moved nothing but the base leaves the ack standing; one that
resolved a conflict does not, that resolution being a change no reviewer
has seen.

Then squash, [the only method the rule accepts][s11].

**The maintainer's bypass is not automatic — it has to be invoked, and
`gh pr merge` cannot invoke it**, refusing client-side before it asks
GitHub anything:

```text
Pull request is not mergeable: the base branch policy prohibits the merge
```

The merge endpoint applies it server-side, and it is the same endpoint
the merge button asks:

```shell
gh api -X PUT repos/{owner}/{repo}/pulls/<n>/merge \
  -f merge_method=squash
```

**Verify what landed rather than trusting the answer**, the signature
[the standard asks for][s-sigs] being a valid one rather than a
particular signer's:

```shell
gh api repos/{owner}/{repo}/commits/main \
  --jq '.commit.verification | {verified, reason}'
```

The forge deletes the head branch itself, per the setting section 11
names. What is still yours is bringing every checkout sitting on `main`
up to date,
that being where the next session starts from and a stale one being where
a branch gets built on a base that has moved. `REPOSITORY.md` carries the
settings and why they are what they are.

[s-what]: https://github.com/btclib-org/.github#what-this-repository-is
[s11]: https://github.com/btclib-org/.github#11-github-settings
[s9]: https://github.com/btclib-org/.github#9-prose-comments-and-docstrings
[s-title]: https://github.com/btclib-org/.github#what-a-pull-request-says-it-is
[s-rev]: https://github.com/btclib-org/.github#review
[s-sigs]: https://github.com/btclib-org/.github#signatures

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
uv sync                                          # the environment
uv run pytest                                    # the suite, coverage included
git add -A && uv run pre-commit run --all-files  # the lint gate
uv run pre-commit validate-config .pre-commit-config.yaml
uv run --locked --no-default-groups --group docs \
    sphinx-build -W --keep-going -b html docs/source docs/build/html
```

`--all-files` means every file git tracks, so a file that is new and not
yet staged is not one of them: run it unstaged and the hooks pass over
exactly the files most likely to fail them. Staging first is what makes
the local gate answer the same question pre-commit.ci does.

The documentation build is the one no hook reads reStructuredText for: a
docstring docutils cannot parse fails it with every hook green -- a name
ending in an underscore is a reference to a link target, which is what
the double backticks around a literal like ``NODE_`` are for.

The last command is worth running before pushing a change to the hook
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

Every test is bounded, too. A node that stops answering fails the test
that built it, named, with a stack of every thread it left running,
instead of holding the run open until something outside it gives up. The
limit is `timeout` in `pyproject.toml`, measured against the slowest test
there is and reasoned about where it is set.

### What gates a merge, and what only reports

`lint.yml` runs the hooks `.pre-commit-config.yaml` declares, so there
is no second list of tools and versions to keep in step; its invocation
differs from the one above only in being `--locked` and in printing what
a fixing hook would have written. `test.yml` runs the suite on one image
and one interpreter, held to the coverage floor `pyproject.toml`
declares. A pull request that touches
only the root prose skips the suite and reports the skip as a pass, which
is what keeps an aggregate check from blocking on a run that never
happened. `docs.yml` runs the same build the environment section above
does, on every pull request the way `lint.yml` and `test.yml` do rather
than on a schedule -- but it is reporting-only for now: `REPOSITORY.md`'s
required-checks table names only the two above, `docs.yml` having landed
too recently to have reported the green run branch protection would
need before naming it a third.

**Whether any of these can refuse a merge is a repository setting and
not a file**, and `REPOSITORY.md` reads it back from the endpoint rather
than restating it here. Read that file before assuming a red run stops
anything.

Everything else reports. `codeql.yml` follows a value from a peer's
message or an RPC request body to where it is used, which no hook here
does. `os-macos.yml` runs the suite on the one platform
`pyproject.toml` classifies that no other workflow runs, and its header
says what differs beneath it. `links.yml` asks whether somebody else's
server answered, and `bootstrap-dns.yml` asks the same question of the
DNS seeds `src/btclib_node/chains.py` names. `claude-review.yml` writes the
review and its own header says it must not become a required check.
`vendored-vectors.yml` re-checks `tests/_data/README.md`'s pin against
upstream, `deps-latest.yml` upgrades every dependency and runs the suite
and the lint gate against the result, and `mutation.yml` is its own
section below.

Which day each of the periodic ones runs is one calendar for the whole
organization, in [section 10 of `btclib-org/.github`'s
README](https://github.com/btclib-org/.github/blob/main/README.md), not
repeated here. The trade it makes is worth knowing before relying on it:
a defect only a sweep can see sits on `main` until that sweep runs, at
most a week.

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
runs. `interpreter.py` is the one scope so far — the consensus entry
point CLAUDE.md's architecture section names as what validates — and a
second scope is a second `.toml` beside it, the way
`btclib-org/btclib`'s own `.github/mutation/` holds one per profile.

### A version, and no release

There is no release, and no machinery for one: nothing is on an index,
`.github/workflows/` holds no `release.yml`, and `REPOSITORY.md`'s *What
is not configured, and why* has the call that answers `0` environments.
So this tree carries no `RELEASING.md` and no `RELEASE_NOTES.md` —
section 2 of [btclib-org/.github's
README](https://github.com/btclib-org/.github/blob/main/README.md) has
why a tier-2 repository carries neither — and a file whose content is its
own absence is this section instead. What anybody runs is a checkout of
`main`, and a fix reaches them when they pull it.

```shell
curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/btclib-node/json
# 404
```

`project.version` is `0.1.0` and static, and `v0.1.0` is the one tag: a
lightweight one from 2023, with a release page and no artifact on it,
which btclib-org/.github#105 measures against the rule below — a ref with
no object of its own has nothing on it to sign. `CHANGELOG.md` opens
under `## Unreleased` and starts after that tag, for the reason its own
introduction gives.

Cutting a tag, the day there is something to tag, is signed and not by
convention: the `tag-integrity` ruleset requires a signature on
`refs/tags/v*` and has no bypass actor, so a tag made without `-s` is
refused at the push rather than noticed afterwards. `REPOSITORY.md`
carries the call that reads that rule back.

```shell
git tag -s v<version> -m "v<version>"
git push origin v<version>
```

`CHANGELOG.md`'s `## Unreleased` heading becomes the version. That rule
is the whole of what `tag-integrity` holds — `required_signatures`, and
neither `non_fast_forward` nor `deletion` — so a tag here can still be
deleted and cut again, which is a property of having published nothing:
an index refuses a version that has been uploaded once, whatever a tag
does. The day a distribution is published is the day a bad release stops
being recoverable and becomes a new version, and it is also the day
`release.yml` arrives and with it the two files above, which is what
section 2 calls tier 1.
