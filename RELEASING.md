# Releasing

There is no release, and no machinery for one.

```shell
curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/btclib-node/json
# 404
gh api repos/btclib-org/btclib-node/environments --jq .total_count
# 0
```

`.github/workflows/` holds no release workflow, `project.version` is
`0.1.0` and static, and the one tag this repository carries has a
release page with no artifact attached to it. What follows is therefore
what a release will be and what it waits on — a statement about today,
which a command re-derives, rather than a promise.

## What a release waits on

- **The package-content floor.** The backend is setuptools, so the
  sdist's inclusion list is a `MANIFEST.in`, and there is none; nor is
  there a job that inspects what would be published. Section 12 of
  btclib-org/.github is the floor and issue #34 is where it is owed.
- **A version scheme.** `0.1.0` was tagged in 2023 and says nothing
  about what the next number means. The siblings that publish use
  calendar versions, and picking that or another one is a decision to
  make once rather than at each release.
- **A publishing environment and a trusted publisher**, so that the
  distributions are built by a workflow and signed by the run that built
  them rather than uploaded from somebody's machine with a token.

Until those exist, what anybody runs is a checkout of `main`, and
`SECURITY.md` says what that means for a fix reaching them.

## Cutting one, when there is one to cut

```shell
uv sync
uv run pytest
git add -A && uv run pre-commit run --all-files
git tag -s v<version> -m "v<version>"
git push origin v<version>
```

Signed, and not by convention: the `tag-integrity` ruleset requires a
signature on `refs/tags/v*` and has no bypass actor, so a tag made
without `-s` is refused at the push rather than noticed afterwards.
`REPOSITORY.md` carries the call that reads that rule back.

`CHANGELOG.md`'s `## Unreleased` heading becomes the version, and
`RELEASE_NOTES.md`'s with it; a release with nothing for a user to act
on says so there in a sentence rather than leaving the section out.

## Recovering one

That rule is the whole of what `tag-integrity` holds: it carries
`required_signatures` and neither `non_fast_forward` nor `deletion`, so
a tag here can still be deleted and cut again. That is a property of
having published nothing — an index refuses a version that has been
uploaded once, whatever a tag does — so the day a distribution is
published is the day a bad release stops being recoverable and becomes
a new version instead. The publishing step and the version scheme are
therefore one decision.
