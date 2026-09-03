# Releasing btclib-node

Releases are published by GitHub Actions
([release.yml](./.github/workflows/release.yml)), not from a developer
machine. Pushing a `v<version>` tag runs the test workflow, the lint
workflow and the documentation build, builds and checks the distribution
files, publishes them to PyPI, and creates the GitHub release. The test
workflow it calls carries the Windows cell along with it (issue #430);
it does not call the platform sentinels beside that workflow --
`os-macos.yml` and `os-ubuntu.yml` gate no commit and no merge today,
and a release is not carved out as the one place that changes —
`release.yml`'s own header has why. There is no PyPI token anywhere:
both indices are configured to trust the workflow itself
([Trusted Publishing](https://docs.pypi.org/trusted-publishers/)).

The same workflow, started by hand instead of by a tag, is a full
rehearsal against TestPyPI. A rehearsal is never tagged.

**`v2026.8.27` was the first release, and it is the only tag this
repository has.** `project.version` is `2026.9`: the scheme below, on
the shape it takes between releases — a cycle open on a month, with no
day for a release to be confused with. It replaced `0.1.0`, the
placeholder from before this repository carried a release path at all
([ISS btclib-org/btclib-node#504][iss-504]). This file is what replaced
`CONTRIBUTING.md`'s former *A version, and no release* section,
[ISS btclib-org/btclib-node#286][iss-286] carrying that decision. A
release built with this file adds the day to the month already declared,
in the same pull request that retitles `CHANGELOG.md` and
`RELEASE_NOTES.md`.

This file no longer argues from a `v0.1.0` tag. **It was deleted, with
its release, on 2026-08-23**, on the maintainer's decision closing
[ISS btclib-org/.github#105][gh-105]: a lightweight tag is a ref to a
commit with no object of its own, so there is nothing on it to sign,
and a repository publishing nothing had no release the tag was the
record of. `portanode`'s `v2026.01.27` went the same way on the same
day, and both release bodies were captured into that issue before
anything was removed.

What was left behind is this tree's own paperwork, which went on
describing the tag for four days
([ISS btclib-org/btclib-node#553][iss-553]) — a deletion decided in one
repository's issue and never carried into another repository's files.
`git tag` still answers `v0.1.0` in any clone fetched before that day,
which is what kept it reading as true. Retiring `0.1.0` in #504 was
right for its other reason, that a placeholder is a poor thing for a
checkout of `main` to report itself as, and that reason never needed
the tag.

**A workflow GitHub has not registered cannot be dispatched, and it
registers one only once its file has reached the default branch.** That
makes `release.yml` — whose `push:` names tags and nothing else, so
nothing else ever triggers it — answer `gh: Not Found (HTTP 404)` to `gh
workflow run` until the pull request adding it is merged.

**It bit once, on `v2026.8.27`, and did exactly what this paragraph
predicted**: the order below inverted, and the TestPyPI rehearsal this
file asks for *before* the merge ran after it, still before the tag.
It cannot bite again while the file keeps its name; a rename or a
second publishing workflow is what would bring it back, which is why
the paragraph stays.

**A fence that fills in a placeholder holds nothing else.** A shell
reads an unfilled `<...>` as a redirection, and what that costs depends
on where it sits. A trailing `>` with nothing after it is a parse error;
a `<` naming a file the reader does not have is a redirection that
merely fails; and neither reaches past the command it is part of, so the
far side of a pipe is forked anyway. How much of a paste the parse error
takes is the reading shell's: `bash` and `sh` reading the block as a
script abort the whole feed on it, and so does `zsh` given it as a file,
where an interactive `bash` or `zsh`, and `zsh` reading the block on
standard input, discard that line alone and read the next as a fresh
command. What looks like a guard is therefore the reader's own directory
rather than the line — `git show v<version>:pyproject.toml | grep
'^version'` runs `grep`, and in a directory holding a file named
`version` it runs `git show v` and creates `./:pyproject.toml`
([ISS btclib-org/.github#751][gh-751]). A placeholder inside a quoted
string — a `python -c` program's — is not a redirection at all, so the
block holding it has no parse error in it and runs whole. Section 9 of
[btclib-org/.github's README][gh-readme] is the rule that a line which
writes goes in a fence of its own;
[ISS btclib-org/.github#745][gh-745] is the measurement that a trailing
`&&` does not stand in for it, the discarded line taking its own `&&`
with it and the line below starting a fresh chain. So a block needing a
value typed into it puts the placeholder in a fence of its own, and what
uses the value in the fence after it, which github.com gives a copy
button of its own. That second fence is live where the first is inert,
so each value it takes from the fence above is written `${name:?}` —
the shell's own must-be-set form — and its lines are chained with `&&`.
The two answer different failures and neither covers the other: a fence
taken alone names the variable and stops on the line that reads it, and
the chain is what keeps the lines below that one from running, a
run-time failure being what `&&` short-circuits where a discarded
placeholder line takes its own trailing `&&` away with it. A
`: "${version:?}"` at a fence's head asserts a value nothing below it
guards in time: one that a step the reader performs needs, there being
no command for the guard to sit on, and one first read below a line
that writes.

## Which version string is which

Telling these apart is most of what can go wrong when cutting a release.

- **`pyproject.toml`'s own `version`** takes three shapes over one
  cycle, never two at once: `YYYY.M`, month only, between releases — the
  placeholder "Open the next cycle" sets, so a checkout of `main` reports
  itself as work in progress rather than as a release it is not;
  `YYYY.M.D`, with the day added on release day — calendar versioning —
  which is what gets published; and `YYYY.M.D.N`, a fourth number added
  only if `YYYY.M.D` shipped broken and cannot be reuploaded (see "If
  something goes wrong"). All three are typed by hand. Three components
  is always the release day; four is always a patch on it. The day is
  never dropped in favor of a fourth digit standing in for it, which is
  what would make the two indistinguishable — and `version-check`
  refuses a tag on the placeholder shape for exactly that reason: two
  components reach the check and nothing past it, whichever one is
  declared. It does not tell three apart from four, both being a release
  it accepts.
- **`v<version>`**, the tag, carries no version of its own: it picks the
  index, PyPI rather than TestPyPI, and `version-check` exists to
  confirm it says what `pyproject.toml` says.
- **`<version>.dev<run*100+attempt>`** is a rehearsal, and nobody types
  it either half at a time: `.dev<run*100+attempt>` is the template the
  `dev-version` composite action patches into `pyproject.toml` when
  `workflow_dispatch` starts the workflow, `github.run_number` counted
  for `release.yml` alone and `github.run_attempt` counted for one
  dispatch of it, so the seventh such run's first attempt produces
  exactly that. The multiplier is what makes a re-run a version of its
  own rather than a collision: a re-run keeps the run's own number and
  only raises the attempt, so the run number alone was identical across
  every re-run of one dispatch and PEP 440 could not tell them apart.
  Placing the attempt below the run number's own place value keeps a
  run's later attempts sorting after its earlier ones and before the
  next run's, the attempt therefore capped at two digits and the
  workflow refusing a hundredth rather than silently wrapping into the
  next run's range. Nothing commits the result: `uv lock` runs straight
  after, so the lockfile the sdist ships agrees with the version it is
  named for.
- **`YYYY.M.Drc1`**, and a `vYYYY.M.Drc1` tag, have no place in this
  scheme: there is no pre-release here, only a version not yet tagged.
  `version-check` refuses anything that is not digits and dots, which is
  what stops it at `pyproject.toml` before a tag is even pushed.

PEP 440 sorts a `.dev` version before the release it rehearses, so a
rehearsal never shadows it. `git tag` on its own does not read the
numbers the same way: `v2026.10` lists before `v2026.7`, alphabetically
rather than chronologically. `git tag --sort=v:refname` reads them as
PEP 440 does.

## One-time setup

This is done, it has published, and what a `gh api` call can read back
of it — the two environments — is `REPOSITORY.md`'s *The two publishing
environments*. What follows is kept as the record of what was
configured and why, which is also what a second registration would
need: a rotated account, a fork that publishes, or a project renamed on
the index, a trusted publisher being attached to a project name.

Neither index held the project when these were added — an upload is
what creates it — so both entries went in as *pending* publishers, on
that same page: a publisher attached to a project can only be added to
a project that exists, and a first upload has nothing else to
authenticate with, there being no token anywhere. Both stopped being
pending on their first upload; a rename of the project on either index
would put that index's entry back into the pending state and is the
one change that makes this section live again.

1. On [PyPI](https://pypi.org/manage/account/publishing/), add a trusted
   publisher: PyPI project name `btclib-node`, owner `btclib-org`,
   repository `btclib-node`, workflow `release.yml`, environment `pypi`.

1. On [TestPyPI](https://test.pypi.org/), add the same trusted publisher,
   with environment `testpypi`.

1. In the GitHub repository settings, create the `pypi` and `testpypi`
   environments. Both require a review from `fametrano`, so neither
   index is uploaded to without a human approving that run; `publish-pypi`
   and `publish-testpypi` are the only holders of `id-token: write` that
   carry one of these two environments. `attest` holds `id-token: write`
   too, for its own Sigstore exchange, but no environment of its own —
   what gates it instead is `needs: [publish-pypi, publish-testpypi]`,
   so it never runs before one of the two reviewed jobs already has.
   `pypi` is additionally restricted to `v*` tags, which is the only ref
   its job runs on anyway.

   Self-review stays allowed on purpose: the maintainer who pushes the
   tag is the reviewer, and forbidding it would deadlock a
   one-maintainer release.

## Rehearse on TestPyPI

A rehearsal runs the identical pipeline — the test workflow (which now
builds and checks the distribution files in its own `dist` job), the
lint workflow, the documentation build, and the publish step — and
publishes the very files those checks passed to
[TestPyPI](https://test.pypi.org/project/btclib-node/) instead of PyPI.

1. On GitHub, Actions → release → Run workflow, and pick the branch to
   rehearse (usually `main`).

1. The workflow appends `.dev<run*100+attempt>` to whatever
   `pyproject.toml` declares on the branch dispatched. Every rehearsal is
   unique on TestPyPI this way, re-runs included.

1. Check the upload, and optionally install it:

   ```shell
   rehearsal=<version>.dev<run*100+attempt>
   ```

   ```shell
   uv run --isolated --no-project --python 3.14 \
     --index https://test.pypi.org/simple/ \
     --index-strategy unsafe-best-match \
     --with "btclib-node==${rehearsal:?}" \
     python -c "from btclib_node import Node; print(Node)"
   ```

1. Check that the `attest` job is green. It signs a rehearsal's files
   too, which is what it is here for: the release path attests after
   PyPI has the distribution files and the tag can no longer be moved,
   so a permission or an API that only works on release day is one this
   job would find there.

## What a red `public-api` means

`release.yml`'s `public-api` job walks the public API of the release
before this one against the one being cut, with griffe, and reports
what broke: a removed object, a changed parameter kind or default, a
narrowed type. **A red one is not a failure to stop on.** Before 1.0
the surface breaks deliberately and often, which is why that job is not
a merge gate and why its own comment says so: it runs here, on the
release, so the answer arrives while `RELEASE_NOTES.md` is already
being written.

Read it, do not obey it. Each finding is checked by hand against that
file's own *Breaking changes* list, and the question is whether the
break is announced, not whether it exists. A finding with an entry is
the system working; a finding with none is either a missing entry to
write or a break nobody meant, and only reading the finding tells you
which.

What the job does **not** do is stop the release: since #534 both
publish jobs keep it in `needs:` for ordering only, their own `if:`
opening with `always()` and reading the other dependencies' results
explicitly. Before that fix a red `public-api` silently kept them from
ever starting — no upload, no environment review, and nothing red
except the job that was designed to be.

On `v2026.8.27` it could not be red at all, and was not: with no
previous tag reachable it resolved none, skipped its own comparison and
reported success in eight steps. **So the release that has run is the
one that says least about this job.** The first one that exercises it is
the next, comparing against `v2026.8.27`, and the first finding it
reports will be the first one anybody here has read.

## Release to PyPI

**A release is a tag on `main`, and everything below that edits a file
does so on a branch of its own.** Nothing is pushed to `main` directly,
this release included.

1. Read what is open, and land first anything that fixes the release
   path itself:

   ```shell
   gh pr list --state open --search "release.yml OR test.yml OR lint.yml"
   ```

   A pull request touching `release.yml`, `test.yml`'s `dist` job,
   `lint.yml`, or anything under `.github/scripts/` or
   `.github/actions/` is one the tag is about to run, so leaving it in
   review means running the defect it fixes on the release.

1. Retitle the `## Unreleased` sections of
   [RELEASE_NOTES.md](./RELEASE_NOTES.md) and
   [CHANGELOG.md](./CHANGELOG.md) to `## v<version>` — the heading must
   be the version alone, and the section must not be empty.
   `release.yml` checks both before anything is built, because a version
   cannot be unpublished once an index has accepted it.

   In the same pull request, open the next cycle's `## Unreleased`
   section in both files, above the one just retitled, with no entry
   under it yet. That is what keeps the topmost heading of either file
   on `main` a work-in-progress heading at every commit. Opening the
   next cycle in a pull request of its own after this one, ahead of
   anything else landing, is the rejected alternative: until that pull
   request lands the topmost section of each file is the release's, so
   a branch landing in between files its entry under a release it is
   not in, and nothing reports it, the release commit having touched
   only the heading. `release.yml`'s check reads the `## v<version>` section
   alone, so an `## Unreleased` above it is nothing it sees, and it is
   not what the release publishes: the notes are lifted from the
   section whose heading is the tag's own.

1. Set the version in `pyproject.toml`, which is the one place it is
   declared, and re-lock so `uv.lock` agrees:

   ```shell
   uv lock
   ```

   **If `main` moves while the gates run, the default is to throw the
   branch away and redo these edits on top of it, and never to merge
   `main` into it.** `CHANGELOG.md` and `RELEASE_NOTES.md` are
   `merge=union`, so a change that opened a heading this release also
   opens is fused into one section carrying that heading twice, and the
   union driver reports no conflict for a reader to catch.

   The reset takes this release's edits away with it, so what follows is
   making them again on the new base: retitle, set the version, `uv lock`,
   and gate again.

   ```shell
   git fetch origin
   git reset --hard origin/main
   ```

   **A rebase is allowed where it is checked, and the check is the redo
   itself, done in a scratch file.** Rebuild what the redo would have
   produced, from `git show` rather than from the working tree, and
   compare it byte for byte against what the rebase left:

   ```shell
   version=<the version being released>
   ```

   ```shell
   : "${version:?}" &&
   git rebase origin/main &&
   scratch=$(mktemp -d) &&
   git show origin/main:CHANGELOG.md > "$scratch/expected.md"
   ```

   Apply this release's own edits to `$scratch/expected.md` by hand:
   retitle `## Unreleased` to `## v<version>`, open the next
   `## Unreleased` above it, and whatever the intro needs. That step is
   a person's, so it ends the chain rather than sitting inside it, and
   what follows is a fence of its own, whose `cmp` prints nothing where
   it passes:

   ```shell
   git show HEAD:CHANGELOG.md > "${scratch:?}/actual.md" &&
   cmp "${scratch:?}/expected.md" "${scratch:?}/actual.md"
   ```

   and the same pair, in a `scratch=$(mktemp -d)` of its own, for
   `RELEASE_NOTES.md`: a fixed name is a name a second run of this
   same step, or a second person on the same machine, can silently
   overwrite or read back stale. Identical bytes are a proof the
   rebase produced what the redo would have; a difference is both the
   defect and, in `$scratch/expected.md`, the file that should have
   been there. `v2026.8.27` was rebased when #551 landed in front of
   the tag, and this is what licensed it.

   **Two checks that look like this one and are not.** Both were
   measured against a fused rebase and against a *misordered* one — the
   same entry below the newly landed one instead of above it, with
   every line intact:

   - **The added and removed lines of the diff, compared before and
     after the rebase, are necessary and not sufficient.** They catch a
     fusion that ate a line and **pass** a pure misordering, the `+`
     lines being the same sequence wherever they land. This paragraph
     prescribed exactly that check until
     [ISS btclib-org/btclib-node#561][iss-561] measured it;
     btclib-org/.github#488 has the fuller scale it belongs to.
   - **`git merge-tree --write-tree` is not a check at all.**
     `merge-ort` reads `.gitattributes` from the trees it is merging,
     applies the `union` driver, and writes the fused blob — exit `0`,
     a tree id, and the defect inside it.

   **The reconstruction is owed on a hand redo too.** `git reset
   --hard` and retyping the edits is not immune: retitling a heading
   the landed change also opened fuses it the same way, and nothing
   about having typed it yourself says otherwise.

1. Give the release pull request its title and its body, before merging
   it and not after. The title is the version; the body says what the
   release is — what moved, what did not, and which of the two a user
   would notice. A squash leaves one commit whose message is that title,
   so the pull request is where the rest stays.

   The section of `RELEASE_NOTES.md` the retitle step renamed is what
   that body is written from. Check it against
   `git log v<previous version>..main --oneline` regardless of how
   current it looks, rather than trust that every line landed when it
   should have.

1. Run `uv run pre-commit run --all-files` and `uv run pytest` before
   pressing anything, then

   ```shell
   uv run --locked --no-default-groups --group docs \
       sphinx-build -W -n -b html docs/source docs/build/html
   ```

   the same build `docs.yml` runs on every pull request and `release.yml`
   runs again on the tag, `-W`, `-n` and all — checked here too, ahead of
   a tag, rather than trusted to a run this step already duplicates.

1. Merge it, with the button, the way every other pull request here
   lands: "Squash and merge", pressed by auto-merge once the review and
   the checks are in. `REPOSITORY.md`'s "Branch protection" is the pair
   that lets an admin bypass a review that will not arrive on a
   solo-maintainer repository — `gh pr merge <n> --squash --admin
   --subject "<title>" --body-file <path>` is the form that names the
   release commit explicitly rather than leaving it to
   `squash_merge_commit_message`'s repository default.

   Then read `lint` and `test` on the commit `main` ends up at before
   tagging, rather than trust the pull request's own green run:

   ```shell
   gh run list --commit "$(git rev-parse origin/main)"
   ```

   a squash creates a commit that is not the one the pull request
   tested, and the merge fires both workflows again from their own
   `push` trigger.

1. Rehearse on TestPyPI (see above) from `main`, if this cycle touched
   the publish path.

1. Tag the release commit on `main` and push the tag. **Name the
   commit**, and read the tag back before pushing it:

   ```shell
   version=<the version being released>
   sha=<sha of the release commit>
   ```

   ```shell
   git tag -s "v${version:?}" -m "release v${version:?}" "${sha:?}" &&
   git show "v${version:?}:pyproject.toml" | grep '^version' &&
   git push origin "v${version:?}"
   ```

   `git tag` with no commit tags whatever `HEAD` the shell is in, and
   every step above ran in a worktree while the primary checkout sits on
   another branch — so the argumentless form is one `cd` away from
   tagging the commit before the version bump. `version-check` would
   refuse it, comparing the declared version against the tag's; the
   `git show` above is the same check one step earlier, where it costs
   nothing, and the chain is what makes it binding — `grep` fails where
   the line is not there, and the push below it does not run.

1. Approve the `pypi` environment when the workflow asks. Up to here
   nothing is public and the tag can still be deleted; the upload that
   follows is the point of no return.

1. **Audit the run job by job, and not for red.** A failed job is loud;
   a skipped one is silent, and the signature is a conclusion of
   `skipped` with **zero steps**:

   ```shell
   run_id=<the release.yml run>
   ```

   ```shell
   gh api \
     "repos/btclib-org/btclib-node/actions/runs/${run_id:?}/jobs?per_page=100" \
     --jq '.jobs[] | [.conclusion, (.steps|length), .name] | @tsv'
   ```

   `btclib-org/btclib`'s own `v2026.8.27` published with its
   post-publish sentinel never having run and nothing saying so, the
   run reading as done (btclib-org/btclib#1470, btclib-org/.github#484).
   A `needs:` reads back through the listed job's own `needs:` chain, so
   a job two hops from something that failed by design is skipped
   without ever being mentioned.

   **Some skips are correct, and they are the ones a reader will cite
   as the defect**: `publish-testpypi` reports `skipped` on a tag push,
   its guard being `workflow_dispatch`. On a rehearsal the mirror image
   is correct for the same reason — `publish-pypi` skips, its guard
   being `push` — and `documented` skips on its own account, its guard
   being the push. Everything else that skipped wants explaining before
   the release is called done.

1. Read the `documented` job rather than the site: read the docs
   activates and builds a new release tag from the automation rule
   `REPOSITORY.md` records, and that job waits for
   `https://btclib-node.readthedocs.io/en/<tag>/` to be served and is
   red if it never is. Green means the release has a permanent URL of
   its own, which is the one to link when the version is named
   anywhere. Red means the build is missing and
   [the builds page](https://app.readthedocs.org/projects/btclib-node/builds/)
   says why: nothing about the publication depends on it, so the fix is
   a build on their side and never a moved tag.

1. `release.yml`'s own `pypi-install` job already installed what was
   just published and ran it past import, on every image this
   repository claims and on `windows-latest` besides — the audit above
   is where its own result is read. What is left to check by hand is
   the attestations, which that job carries no token for:

   ```shell
   file=<the distribution file to verify>
   ```

   ```shell
   pypi-attestations verify pypi "${file:?}" --repository \
     https://github.com/btclib-org/btclib-node
   ```

   **The
   [simple API](https://pypi.org/simple/btclib-node/) (`Accept:
   application/vnd.pypi.simple.v1+json`) is the index's own state; the
   JSON API is a cache of it** — `provenance` answers `null` there
   even where the link exists. The simple API carries the real
   provenance link, under
   `/integrity/<project>/<version>/<filename>/provenance`, and the
   command above checks the signature rather than merely its presence.
   The simple API is itself served through a CDN, so one request
   straight after a publish can still answer stale for a few minutes —
   `pypi-install.yml`'s own wait step is written against exactly that
   lag, a `curl` GET confirming a version and `uv` failing to resolve
   it one second later (#546, #548); a retry is what an absence needs
   before it is trusted, the way that step retries rather than asking
   once.

1. Read the bill of materials attached to the release,
   `btclib_node-<version>.cdx.json`: a CycloneDX 1.6 document naming the
   distribution, its licence, the two files with their SHA-256 under
   `metadata.component.externalReferences`, and **one component per
   `Requires-Dist` line the wheel declares** — which today is one,
   `btclib`, carrying `btclib[secp256k1]>=<floor>` as a property.

   ```shell
   version=<the released version>
   ```

   ```shell
   gh release download "v${version:?}" --repo btclib-org/btclib-node \
     --pattern '*.cdx.json' &&
   python3 -c "import json,sys; d=json.load(open(sys.argv[1])); \
     print(len(d['components']), 'components'); \
     print('git+https:// present:', 'git+https://' in json.dumps(d))" \
     "btclib_node-${version:?}.cdx.json"
   # 1 components
   # git+https:// present: False
   ```

   **A resolved version is not in it, deliberately**, and this is where
   a reader is most likely to think the document is broken.
   `.github/scripts/generate_sbom.py`'s own docstring is the argument:
   what a user's installer resolves is not a fact about these files, so
   a resolved version recorded here would be a claim the wheel does not
   make. A requirement pinned with `==` gets a `version`; anything else
   gets its specifier as a property and none. A vendored submodule
   would get a component of its own with the gitlink's sha — this tree
   vendors none.

   So the document stops at what this distribution declares, and a
   consumer matching vulnerabilities against it gets the direct
   dependency and nothing below it. That is a property of the document
   rather than a defect in it, and it is only a property if the step
   that tells you to read it says so
   ([ISS btclib-org/btclib-node#554][iss-554], which is this paragraph
   having said the opposite until then).

   What is worth reading rather than assuming is that a `git+https://`
   in it is a release that should not have got this far,
   `[tool.uv.sources]`'s pin to btclib's `main` branch being dropped
   from a built wheel's own metadata in favor of
   `project.dependencies`' floor — the comment beside that table says
   so, and the smoke test in `test.yml`'s `dist` job checks it on every
   pull request, not only at a release.

1. Check the GitHub release the `github-release` job created — **ask for
   the release itself, not for the run's conclusion**, a skipped job
   being what a green run looks like from the Actions page:

   ```shell
   version=<the released version>
   ```

   ```shell
   gh release view "v${version:?}" --json name,assets,author
   ```

   `author` is the cheap second question: `github-actions` is the
   workflow having cut it, any other login a release recreated by hand.
   Its notes are the tag's section of `RELEASE_NOTES.md`, and the
   distribution files are attached, `<tag>.attestation.jsonl` and the
   bill of materials beside them.

1. Verify the provenance of an asset:

   ```shell
   version=<the released version>
   ```

   ```shell
   gh release download "v${version:?}" --repo btclib-org/btclib-node &&
   wheel=btclib_node-${version:?}-py3-none-any.whl &&
   repo=btclib-org/btclib-node &&
   gh attestation verify "$wheel" --repo "$repo" \
     --signer-workflow "$repo/.github/workflows/release.yml" &&
   gh attestation verify "$wheel" --repo "$repo" \
     --bundle "v${version:?}.attestation.jsonl"
   ```

   the first asks the attestations API for the signed statement, the
   second reads it from the asset and asks nothing, and the chain runs
   the second only where the first passed. One attestation covers the
   wheel, the sdist and the bill of materials, so all three verify
   against the same bundle.

1. Open the next cycle: set a generic next version without the day
   (e.g. after `2026.8`, use `2026.9`) in `pyproject.toml`, through a
   pull request like any other, and re-lock so `uv.lock` agrees:

   ```shell
   uv lock
   ```

   The `## Unreleased` sections of `RELEASE_NOTES.md` and `CHANGELOG.md`
   are already there, the retitle step above having opened them in the
   release's own pull request. What stays here is the version, which
   cannot move earlier with them: `version-check` compares the tag
   against what `pyproject.toml` declares, so a tree already bumped
   would offer it the next cycle's month instead of the version being
   released.

## Rebuild a release from its tag

`test.yml`'s `dist` job exports `SOURCE_DATE_EPOCH` from the commit date
and normalizes the sdist, so a rebuild of a released tag is the same
bytes as what was published. Anyone can check that, and the check is one
command short of the provenance one above: verify the *rebuilt* file
rather than a downloaded one, and it can only pass if the digests agree.

A worktree and not `git checkout`, for the reason `CLAUDE.md` gives: the
primary checkout is the maintainer's, and a rebuild wants a tree of its
own regardless.

The version is a fence of its own, for the reason the introduction
gives. The `&&` below carries the `${version:?}` failure down the block,
and stops the rebuild where `git worktree add`, `uv build` or a
verification fails; what it is not is the guard on the placeholder line
above ([ISS btclib-org/.github#745][gh-745]). The
rejected alternative makes each step fatal on its own — `|| exit`, or a
`set -e` above the block — which refuses the same paste by ending the
shell the reader pasted into, where the chain leaves the session
standing.

The `mkdir` immediately above the checkout closes a second gap, on a
*correctly* filled paste: `git worktree add` checks out into a
pre-existing empty `/tmp/btclib-node-rebuild` exactly as into a fresh
one, saying nothing about which happened, so a directory left over
from an earlier rebuild is reused silently rather than refused.
`mkdir` without `-p` fails wherever the target already exists, empty
or not, and — placed first — takes the rest of the chain with it. The
`mkdir` is the fence's first line and it writes, so the
`: "${version:?}"` above it is what refuses an unfilled paste: a guard
below the first writing line arrives too late.

```shell
version=<the released version>
```

```shell
: "${version:?}" &&
mkdir /tmp/btclib-node-rebuild &&
git worktree add --detach /tmp/btclib-node-rebuild "v${version:?}" &&
cd /tmp/btclib-node-rebuild &&
export SOURCE_DATE_EPOCH=$(git log -1 --pretty=%ct) &&
uv build &&
uv run --no-project --python 3.14 \
  .github/scripts/normalize_sdist.py dist/ &&
uv run --no-project --python 3.14 \
  .github/scripts/generate_sbom.py dist/ sbom/ &&
repo=btclib-org/btclib-node &&
gh attestation verify "dist/btclib_node-${version:?}-py3-none-any.whl" \
  --repo "$repo" --signer-workflow "$repo/.github/workflows/release.yml" &&
gh attestation verify "dist/btclib_node-${version:?}.tar.gz" \
  --repo "$repo" --signer-workflow "$repo/.github/workflows/release.yml" &&
gh attestation verify "sbom/btclib_node-${version:?}.cdx.json" \
  --repo "$repo" --signer-workflow "$repo/.github/workflows/release.yml"
```

Two things bound that guarantee, and both are worth knowing before
reading a mismatch as tampering:

- **the build reads the working directory, not git.** `uv_build` walks
  the tree through the glob patterns of `[tool.uv.build-backend]`, so an
  *untracked* file matching one of them is packed like any other and
  changes the digest. Rebuild in a clean export, which takes the
  version from the fence above:

  ```shell
  : "${version:?}" &&
  d=$(mktemp -d) &&
  git archive "v${version:?}" | tar -x -C "$d" &&
  cd "$d"
  ```

- **the build backend is bounded, not pinned.** `[build-system] requires`
  asks for `uv_build>=0.12.5,<0.13`, and a build takes whichever version
  in that range the uv running it carries, so a rebuild months later
  runs a backend the release never saw. What the ceiling bounds is the
  *content* of the archive; its member metadata is `normalize_sdist.py`'s
  answer and not the backend's.
- **the rehearsal is a different version, by construction.** A TestPyPI
  dispatch appends `.dev<run*100+attempt>` to the version, so its files
  are not a second build of the release's — no digest is shared with the
  release.

## If something goes wrong

- The workflow failed before the `publish-pypi` job: nothing was
  uploaded — confirm that from the index's own state before deleting
  the tag, rather than from which job the run reports as having
  failed: a `needs:` chain several jobs deep can report a job
  `skipped` without saying why (*Audit the run job by job* above),
  and `publish-pypi` is not idempotent, so retagging over a version
  that in fact landed fails a second time on top of the first.

  ```shell
  version=<the released version>
  ```

  ```shell
  : "${version:?}" &&
  curl -s -H "Accept: application/vnd.pypi.simple.v1+json" \
    https://pypi.org/simple/btclib-node/ \
    | python3 -c "import json,sys; \
      print('${version:?}' in json.load(sys.stdin)['versions'])"
  ```

  `False`, and only `False`, is what licenses the delete — retry the
  request before trusting it, the simple API being cached the way the
  paragraph above describes. Delete the tag, fix, and tag again:

  ```shell
  git tag -d "v${version:?}" &&
  git push origin ":refs/tags/v${version:?}"
  ```

  Both lines, and the local one is the half that is easy to skip: a tag
  is per-repository where a branch is per-worktree, so deleting it in
  one worktree leaves it in every other.

- `publish-pypi` itself ran and failed at the token exchange
  (`invalid-publisher`), after the matrix had already built everything:
  nothing was uploaded, but retagging would rebuild what was never at
  fault. Fix the registration and re-run the publish job alone against
  what is already built:

  ```shell
  run_id=<the release.yml run>
  ```

  ```shell
  gh run rerun "${run_id:?}" --failed
  ```

  a fresh approval of the `pypi` environment is still required, the
  protection applying per deployment attempt rather than once per run.

- The upload succeeded but the release is broken: PyPI never accepts a
  file name twice, even after deletion. Yank the bad release on PyPI and
  publish a new patch version, a fourth number on the day
  (`2026.8.4` → `2026.8.4.1`).

- Only the `github-release` job failed, or shows **`skipped`** though
  both of its needs — `publish-pypi` and `attest` — report `success`:
  the run's own conclusion is `success` and no release exists, which is
  why the step above asks `gh release view` rather than reading the run.
  Recovery is by hand, from the run's own `dist`, `sbom` and
  `attestation` artifacts — three of them, not `dist` alone:

  ```shell
  run_id=<the release.yml run>
  version=<the released version, e.g. 2026.8.4>
  notes=<a file holding the release notes for this tag>
  ```

  ```shell
  tag="v${version:?}" &&
  : "${notes:?}" &&
  gh run download "${run_id:?}" -n dist -n sbom -n attestation &&
  for f in dist/*.whl dist/*.tar.gz; do
    sha=$(curl -s -H "Accept: application/vnd.pypi.simple.v1+json" \
      https://pypi.org/simple/btclib-node/ |
      jq -r --arg n "$(basename "$f")" \
        '.files[] | select(.filename==$n) | .hashes.sha256')
    echo "$sha  $f" | sha256sum -c -
  done &&
  mv attestation/attestation.jsonl "${tag:?}.attestation.jsonl" &&
  gh release create "${tag:?}" dist/*.whl dist/*.tar.gz \
    sbom/*.cdx.json "${tag:?}.attestation.jsonl" \
    --title "${tag:?}" --notes-file "${notes:?}"
  ```

  The digest comparison is not optional: it is what stands in for the
  provenance a second, unwanted publish attempt would otherwise have to
  establish.

[iss-286]: https://github.com/btclib-org/btclib-node/issues/286
[iss-504]: https://github.com/btclib-org/btclib-node/issues/504
[iss-554]: https://github.com/btclib-org/btclib-node/issues/554
[iss-561]: https://github.com/btclib-org/btclib-node/issues/561
[iss-553]: https://github.com/btclib-org/btclib-node/issues/553
[gh-105]: https://github.com/btclib-org/.github/issues/105
[gh-745]: https://github.com/btclib-org/.github/issues/745
[gh-751]: https://github.com/btclib-org/.github/issues/751
[gh-readme]: https://github.com/btclib-org/.github/blob/main/README.md
