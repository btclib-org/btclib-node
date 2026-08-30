# Repository configuration

What is set on this repository, as the `gh api` call that reads it back
and the answer that call gives today. A setting recorded as prose alone
is one nobody can check; recorded this way, a drift is one command away
from being seen.

The rules and the settings live *outside* the tree: nothing below is
recoverable by reading the repository. What is recorded is the settings
[the standard](https://github.com/btclib-org/.github) asks about — the
ones [section 16's
checklist](https://github.com/btclib-org/.github#16-checklists) sets on a
new repository, and the ones a section of the standard states a rule
for — together with whatever a call quoted for one of those answers
alongside it. Where that scope ends is *What this file passes over*, at
the foot.

## Required checks on main

Set through classic branch protection; no ruleset on `main` carries a
`required_status_checks` rule.

```shell
gh api repos/btclib-org/btclib-node/branches/main/protection \
  --jq '.required_status_checks
        | {strict, checks: [.checks[] | {app_id, context}]}'
# {"checks":[{"app_id":15368,"context":"Lint and type-check"},
#   {"app_id":15368,"context":"test: every job passed"},
#   {"app_id":15368,"context":"Build the documentation"},
#   {"app_id":15368,"context":"Regtest against Bitcoin Core"}],
#   "strict":true}
gh api repos/btclib-org/btclib-node/rulesets --jq '.[].id' \
  | xargs -I{} gh api repos/btclib-org/btclib-node/rulesets/{} \
    --jq '.rules[] | select(.type=="required_status_checks")'
# (nothing)
```

A red run on any of them blocks the merge. Each is produced by the
workflow that answers for it:

| Check | Produced by |
| --- | --- |
| `Lint and type-check` | `lint.yml`'s only job |
| `test: every job passed` | `test.yml`'s aggregate job |
| `Build the documentation` | `docs.yml`'s only job |
| `Regtest against Bitcoin Core` | `integration-bitcoind.yml`'s `regtest` job |

`lint.yml`, `docs.yml` and `integration-bitcoind.yml` each have one job,
so that job is the context; an aggregate over a single cell would be a
job whose whole purpose is to repeat another's answer. `test.yml` has
more than one and is therefore named through its aggregate, so that a
job added to that workflow is gated on by being added rather than by
somebody editing a rule stored outside the tree. Every one of these jobs
carries the reasoning in its own header.

The context is the job's `name:`, not the workflow's, and the `checks`
array above holds it as a literal string that nothing in the tree can
keep in step, bound to the Actions app (`15368`) that produces it:
**renaming the `regtest` job would leave a required check nothing
produces, and a merge would wait on it forever.** That is a change to
make here first, in the same order this section's own opening argues —
the setting, then the record of it.

`docs.yml`'s "Build the documentation" runs on every pull request
already, the way `lint.yml` and `test.yml` do (*What gates a merge, and
what only reports* in `CONTRIBUTING.md`), and `release.yml` calls it too
(btclib-org/btclib-node#264); its presence in the `checks` array above
is what makes a red run on it block a merge into `main`, a repository
setting no pull request carries (this section's own opening sentence).
Adding a check to that array is a `gh api` PATCH of the `checks` array,
as a JSON body on stdin:

```shell
gh api -X PATCH \
  repos/btclib-org/btclib-node/branches/main/protection/required_status_checks \
  --input - <<'JSON'
{"strict": true,
 "checks": [{"context": "Lint and type-check", "app_id": 15368},
            {"context": "test: every job passed", "app_id": 15368},
            {"context": "Build the documentation", "app_id": 15368},
            {"context": "Regtest against Bitcoin Core", "app_id": 15368}]}
JSON
```

The array is rewritten whole rather than added to: a check left out of
that PATCH stops being required, silently. `checks` and not `contexts`,
and a JSON body and not `-f`: `contexts` has no field for an app, so a
PATCH sending it replaces a list bound to the Actions app with the same
names bound to nothing, and `-f` sends `app_id` as a string, which the
endpoint refuses (section 11 of the organization standard). This section
carried the `contexts` form, with `-F strict=true` and one `-f` per
name, from btclib-org/btclib-node#264 until btclib-org/btclib-node#657;
btclib-org/btclib-node#453 is where a PATCH was first run here rather
than only documented.

Re-run the first command above to confirm the checks still hold, each
with its `app_id` — its answer, not this paragraph, is what is true
today. A read of `.contexts[]` cannot tell: it answers the same four
names whether or not each is bound to an app, which is why the record
above reads `.checks[]`.

**`links.yml`, `os-macos.yml` and `bootstrap-dns.yml` must not become
required checks**, and neither must `claude-review.yml` nor
`scorecard.yml`. `links.yml` and `bootstrap-dns.yml` ask whether
somebody else's server answered, `os-macos.yml` runs on a schedule and
reports what a sweep sees rather than what a pull request introduced,
and `claude-review.yml` writes an opinion for an author to weigh.
`scorecard.yml` carries neither a `pull_request` trigger nor
`workflow_dispatch` (`CONTRIBUTING.md`'s *What gates a merge, and what
only reports*), so a required check on it could never be satisfied by
any pull request at all. Each says so in its own header.

`codeql.yml` is not among them. It runs on `pull_request` and carries an
aggregate job, `codeql: every job passed`, so its result is one context
a rule can name however many languages the matrix grows to
(btclib-org/.github#459). Whether the rule asks for it is the `checks`
array above, which does not name it.

## Branch protection and the rulesets

`main` is the default branch and everything reaches it through a pull
request. Rules aggregate across the classic protection and the rulesets
rather than replacing each other, and where two overlap the stricter
answer is the one that holds:

```shell
gh api repos/btclib-org/btclib-node/branches/main/protection \
  --jq '{
    linear: .required_linear_history.enabled,
    force: .allow_force_pushes.enabled,
    deletions: .allow_deletions.enabled,
    conversations: .required_conversation_resolution.enabled,
    reviews: .required_pull_request_reviews.required_approving_review_count,
    dismiss: .required_pull_request_reviews.dismiss_stale_reviews,
    admins: .enforce_admins.enabled}'
# {"admins":false,"conversations":true,"deletions":false,"dismiss":true,
#  "force":false,"linear":true,"reviews":1}
```

`enforce_admins: false` is not a relaxation but what makes a solo merge
possible at all: the ruleset bypass below reaches the ruleset's own rule
and nothing else.

```shell
gh api repos/btclib-org/btclib-node/rulesets --jq '.[].id' \
  | xargs -I{} gh api repos/btclib-org/btclib-node/rulesets/{} \
    --jq '{name, target, rules: [.rules[].type],
           bypass: [.bypass_actors[]?.bypass_mode]}'
# {"bypass":[],"name":"main-integrity","target":"branch",
#  "rules":["required_signatures","required_linear_history",
#           "non_fast_forward","deletion"]}
# {"bypass":["pull_request"],"name":"main-self-merge","target":"branch",
#  "rules":["pull_request"]}
# {"bypass":[],"name":"tag-integrity","target":"tag",
#  "rules":["required_signatures"]}
```

- `main-integrity` — required signatures, required linear history, no
  force pushes, no deletions — with **no bypass actor at all**, which is
  what makes every one of those true of an administrator too.
- `main-self-merge` — a pull request, an approving review, stale reviews
  dismissed on push, conversations resolved, and `squash` as the only
  merge method it accepts — bypassed by the maintainer in **`pull_request`
  mode**.
- `tag-integrity` — required signatures on `refs/tags/v*`, no bypass.

**The bypass mode is the whole of the design.** `pull_request` excuses
its holder from the rule while merging a pull request and at no other
time, so it answers the one thing a one-maintainer repository cannot —
an approving review from somebody else — and answers nothing else.
`always` in that field would mean a direct push to `main` had become
possible for its holder, which is the drift the command above exists to
catch.

```shell
gh api repos/btclib-org/btclib-node/rulesets --jq '.[].id' \
  | xargs -I{} gh api repos/btclib-org/btclib-node/rulesets/{} \
    --jq '.rules[] | select(.type=="pull_request") | .parameters'
# {"allowed_merge_methods":["squash"],
#  "dismiss_stale_reviews_on_push":true,"require_code_owner_review":false,
#  "require_extra_approval_for_unattributed_changes":true,
#  "require_last_push_approval":false,"required_approving_review_count":1,
#  "required_review_thread_resolution":true}
```

## Signed commits

```shell
gh api repos/btclib-org/btclib-node/commits/main \
  --jq '.commit.verification | {verified, reason}'
# {"reason":"valid","verified":true}
```

`required_signatures` refuses an unsigned commit at the push rather than
noticing it afterwards, and with an empty bypass list it refuses one from
everybody. What that call answers for is the squash GitHub composed at
the merge, signed with its own web-flow key rather than the maintainer's,
which satisfies the rule: it asks for a valid signature and not for a
particular signer.

What no rule covers is a commit before it is pushed:
`git log -1 --format='%G? %GS'`, an `N` being a defect to fix rather than
to explain.

## Merge methods

```shell
gh api repos/btclib-org/btclib-node \
  --jq '{squash: .allow_squash_merge, merge: .allow_merge_commit,
         rebase: .allow_rebase_merge, auto: .allow_auto_merge,
         delete_on_merge: .delete_branch_on_merge,
         title: .squash_merge_commit_title,
         message: .squash_merge_commit_message,
         old_title: .use_squash_pr_title_as_default}'
# {"auto":true,"delete_on_merge":true,"merge":false,
#  "message":"COMMIT_MESSAGES","old_title":false,"rebase":false,
#  "squash":true,"title":"COMMIT_OR_PR_TITLE"}
```

Squash is the only method, and `main-self-merge` names it too, so the
constraint holds even if this repository setting is flipped back.

`COMMIT_OR_PR_TITLE` is the subject — the pull request title with its
number, or the subject of the single commit where a branch has one, which
the convention of writing the two alike keeps the same text.
`COMMIT_MESSAGES` is the body — which is why a commit message here is
prose this tree ships.

`use_squash_pr_title_as_default` is the older spelling of the title
setting, which GitHub's own API description marks as closing down in
favour of `squash_merge_commit_title`. Section 11 states a rule about the
title, so both spellings are read back here.

`delete_branch_on_merge` fires on its own, every landing being a merged
pull request, so a branch still standing is one that was closed rather
than merged.

## Features, and the topics

```shell
gh api repos/btclib-org/btclib-node \
  --jq '{wiki: .has_wiki, projects: .has_projects, issues: .has_issues,
         visibility: .visibility, default_branch: .default_branch}'
# {"default_branch":"main","issues":true,"projects":true,
#  "visibility":"public","wiki":true}
```

The wiki and the projects board are on, and the standard states no rule
about either, so each is this repository's own answer rather than a
divergence from one. The wiki holds nothing, this tree's documentation
sitting beside what it describes:

```shell
git ls-remote https://github.com/btclib-org/btclib-node.wiki.git
# remote: Repository not found.
git ls-remote https://github.com/btclib-org/btclib-node.git
# (the refs of this repository)
```

The second call is what says the first reports the wiki rather than the
network or the credentials.

```shell
gh api repos/btclib-org/btclib-node --jq '.topics | join(", ")'
# bitcoin, bitcoin-node, blockchain, consensus, full-node, p2p, rocksdb,
# script-interpreter
```

The topics are `pyproject.toml`'s `keywords`, in the same lowercase
spelling; the API answers them alphabetically and the metadata carries an
order, which decides only which one is dropped once the ceiling on topics
is reached.

## Token permissions

```shell
gh api repos/btclib-org/btclib-node/actions/permissions/workflow
# {"default_workflow_permissions":"read",
#  "can_approve_pull_request_reviews":false}
gh api repos/btclib-org/btclib-node/actions/permissions
# {"enabled":true,"allowed_actions":"all","sha_pinning_required":false}
```

`read` is what every workflow here starts from, and a job elevates
itself where it needs more: `test.yml`'s `changes` job adds
`pull-requests: read` to ask which files a pull request touches,
`codeql.yml`'s analysis adds `security-events: write` to upload its
SARIF, and `claude-review.yml` adds `pull-requests: write` to post a
comment and `id-token: write` for the OIDC token its action mints.
`release.yml` is where that stops being a list of reads: `publish-pypi`
and `publish-testpypi` add `id-token: write` for the OIDC token each
index trusts, `attest` adds `attestations: write` beside an
`id-token: write` of its own, and `github-release` adds
`contents: write`, which is the one token in this repository that
writes to it. Each is a job-level block under a workflow whose own
top-level `permissions:` is `contents: read` like every other.

`can_approve_pull_request_reviews` is false, which matters as much as the
token: a workflow that could approve would satisfy `main-self-merge`
without a person.

`sha_pinning_required` is false at the repository, and every action in
these workflows is pinned to a commit SHA anyway — the rule is in the
files rather than in the setting, and turning the setting on would make
it enforced rather than conventional.

**What this call cannot say is whether a value is this repository's own
or the organization's**, there being no endpoint that answers. Whoever
moves an organization default reads this repository back afterwards
rather than assuming it moved.

## Security and analysis

```shell
gh api repos/btclib-org/btclib-node --jq '.security_and_analysis'
# {"dependabot_security_updates":{"status":"enabled"},
#  "secret_scanning":{"status":"enabled"},
#  "secret_scanning_non_provider_patterns":{"status":"disabled"},
#  "secret_scanning_push_protection":{"status":"enabled"},
#  "secret_scanning_validity_checks":{"status":"disabled"}}
```

Secret scanning, its push protection and Dependabot security updates are
what the standard asks for, and each answers `enabled`. What answers
`disabled` is plan-gated rather than declined, so the call reports the
setting and not the request. What runs before any of them is the
`detect-private-key` hook and the `detect-secrets` baseline, on the
author's own machine.

`.github/dependabot.yml` is in the tree and carries the `github-actions`
and `uv` ecosystems; the day it runs is the organization's calendar
rather than this file's.

```shell
gh api repos/btclib-org/btclib-node/private-vulnerability-reporting
# {"enabled":true}
gh api repos/btclib-org/btclib-node/dependabot/alerts --jq 'length'
# 27
gh api repos/btclib-org/btclib-node/dependabot/alerts \
  --jq '[.[].dependency.manifest_path] | unique'
# ["poetry.lock","uv.lock"]
gh api repos/btclib-org/btclib-node/dependabot/alerts \
  --jq 'group_by(.dependency.manifest_path)
        | map({(.[0].dependency.manifest_path): length}) | add'
# {"poetry.lock":26,"uv.lock":1}
gh api repos/btclib-org/btclib-node/dependabot/alerts \
  --jq 'group_by(.state) | map({(.[0].state): length}) | add'
# {"dismissed":21,"fixed":6}
gh api repos/btclib-org/btclib-node/dependabot/alerts \
  --jq '.[] | select(.dependency.manifest_path=="uv.lock")
        | [.security_advisory.severity, .dependency.package.name,
           .security_vulnerability.first_patched_version.identifier]
        | @tsv'
# medium pytest 9.0.3
```

**Private vulnerability reporting is on**, so *Report a vulnerability* on
this repository's Security tab opens an advisory only the maintainers
see. `SECURITY.md` states the call above rather than the answer, the
route being a setting and not a file — true of every repository whether
or not it keeps a policy of its own, this one now doing so as a tier-1
repository. `.github/ISSUE_TEMPLATE/config.yml`'s Security vulnerability
entry links straight to that advisory form.

The alert list is not only the open alerts: Dependabot's own default
listing carries every state. By manifest it is 26 `poetry.lock` and 1
`uv.lock`; by state it is 21 `dismissed` and 6 `fixed`, none `open`.
The one `uv.lock` alert is already `fixed`: `pytest`, medium severity,
patched at `9.0.3`, and `uv.lock` on `main` already pins pytest above
that. A future `uv.lock` alert answering `open` is this tree's own to
act on, as a finding of its own — none is recorded here to defer to.

## Code scanning, and which setup performs it

```shell
gh api repos/btclib-org/btclib-node/code-scanning/default-setup \
  --jq '{state, languages, query_suite}'
# {"languages":["actions","python"],"query_suite":"default",
#  "state":"not-configured"}
gh api repos/btclib-org/btclib-node/code-scanning/alerts --jq 'length'
```

`state: not-configured` is what has to stay true, and it is the setting
`codeql.yml` depends on: the default setup and an advanced workflow are
exclusive, and the collision is at the upload rather than at the start —
a run would build its database, be refused the SARIF, and report a
failure it did not have. The `languages` and `query_suite` fields are
what the setting *would* analyse, which is why `codeql.yml` matches them.

## The concurrent-job ceiling

```shell
gh api orgs/btclib-org --jq '{plan: .plan.name}'
# {"plan":"free"}
```

The concurrent-job limit GitHub documents for that plan belongs to the
organization and not to this repository: every repository in it draws on
the same allowance. So a matrix on every commit here is a slot a reviewer
in a sibling repository waits behind, which is the argument for keeping
most of a matrix out of the merge gate and into a weekly sweep instead
([btclib-org/.github#85](https://github.com/btclib-org/.github/issues/85))
-- the platform axis the `os-*` sentinels cover, `os-macos.yml` and
`os-ubuntu.yml` among them.

`test.yml`'s own interpreter axis and its `windows-latest` cell are the
rows this repository gates instead of sweeping, and that file's own
header carries the argument for each rather than this section repeating
it: every cell runs as a parallel job, so each one past the first costs
one more slot at the ceiling and no additional wait, against a review
that costs more than the wait regardless. Windows had a weekly sentinel
of its own, `os-windows.yml`, whose runs priced this trade before the
cell existed; the cell now runs what the sentinel ran, so the sentinel
is gone rather than kept beside a gate cell duplicating it
([issue #430](https://github.com/btclib-org/btclib-node/issues/430)).

Section 10's *Which trees carry which sentinel* names this repository
under `os-windows`, so the organization's record and this tree disagree
over whether a gate cell may stand in for a platform sentinel.
[btclib-org/.github#618](https://github.com/btclib-org/.github/issues/618)
is where that is settled, and
[issue #735](https://github.com/btclib-org/btclib-node/issues/735) is
this tree's half of it.

`3.14t`, the interpreter axis's own second cell, is the row that trade
stopped covering: it still runs, in `test.yml`'s `free-threaded` job,
at the same cost as any other cell, but no longer gates, because
`rocksdict` -- this tree's own store -- has never published a wheel or
an sdist for it on any release
([issue #723](https://github.com/btclib-org/btclib-node/issues/723)),
so there is no wait left to weigh against a review, only a job that
cannot install.
[Issue #387](https://github.com/btclib-org/btclib-node/issues/387) is
where that row entered the gate on this section's own rule; #723 is
where it left again on the same rule, unmet rather than repealed, and
where it goes back in once a wheel exists.

Gating a cell inside `test.yml` cost no change to the ruleset: *Required
checks on main* above names `test.yml`'s aggregate job, not any cell by
name, so the `windows` job reached that aggregate's own `needs:` by
being added to the workflow, the same way `3.14t` did for issue #387 --
and the same way taking `3.14t` back out needed no ruleset change
either, for issue #723. Re-run that section's own first command to
confirm the required checks still hold; its answer, not this paragraph,
is what is true today.

## The two publishing environments

```shell
env=repos/btclib-org/btclib-node/environments
gh api "$env" --jq .total_count
# 2
for e in pypi testpypi; do
  gh api "$env/$e" --jq '.name, (.protection_rules[]
    | select(.type=="required_reviewers")
    | "\(.reviewers[].reviewer.login) self_review=\(.prevent_self_review)")'
done
# pypi
# fametrano self_review=false
# testpypi
# fametrano self_review=false
gh api "$env/pypi/deployment-branch-policies" \
  --jq '.branch_policies[] | "\(.name) (\(.type))"'
# v* (tag)
```

`publish-pypi` and `publish-testpypi` are the only jobs that carry one
of these, and carrying one is what makes an upload wait for a person:
the run stops before the job and does not start it until the review
lands. `pypi` additionally takes only `v*` tags, which is the only ref
its own `if:` lets it run on; `testpypi` takes any, a rehearsal being
dispatched from a branch on purpose. `RELEASING.md`'s *One-time setup*
is where the rest of the argument is, self-review included -- allowed
on both, the maintainer who pushes the tag being the reviewer.

**What the pair has published**, read back rather than recalled — which
is this file's own contract, and is what the bullet removed from *What
is not configured, and why* stopped doing: it recorded a `v0.1.0` tag
and a release page that were deleted on 2026-08-23, four days before
this line was written, on the decision closing btclib-org/.github#105
(btclib-org/btclib-node#553):

```shell
gh api repos/btclib-org/btclib-node/tags --jq '.[].name'
# v2026.8.27
gh api repos/btclib-org/btclib-node/releases \
  --jq '.[] | "\(.tag_name) by \(.author.login), \(.assets|length) assets"'
# v2026.8.27 by github-actions[bot], 4 assets
curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/btclib-node/json
# 200
```

The four assets are the wheel, the sdist, the CycloneDX bill of
materials and the attestation bundle; `RELEASING.md` has what each is
checked with. `author` being `github-actions[bot]` is the cheap second
question that separates a release the workflow cut from one recreated
by hand.

**This pair is not the kind of setting whose absence a workflow would
have reported.** An environment a workflow names and the settings do
not carry is created by GitHub at the first deployment that references
it, [with no protection rules on
it](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments),
so the run would not have failed for want of one: it would have
published without asking anybody. That is why the count above is read
back here rather than inferred from `release.yml` naming the two
(btclib-org/btclib-node#509).

The trusted publishers these are named by live on the two indices'
own side, where no `gh api` call here reads them back -- the same
boundary the Read the Docs section below sits on.

## Read the Docs, which is btclib-node.readthedocs.io

**The documentation is published by a project on Read the Docs**,
`btclib-node` on <https://app.readthedocs.org/projects/btclib-node/>.
`.readthedocs.yaml` says how a build runs; *which* versions run it is
settings there, and nothing in the tree records them:

```shell
curl -s https://app.readthedocs.org/api/v3/projects/btclib-node/ \
  | python3 -c 'import json, sys; p = json.load(sys.stdin); \
      print(p["default_branch"], p["default_version"])'
# main latest
curl -s https://app.readthedocs.org/api/v3/projects/btclib-node/versions/ \
  | python3 -c 'import json, sys
for v in json.load(sys.stdin)["results"]:
    if v["type"] == "tag":
        print(v["slug"], v["active"], v["built"])'
# v2026.8.27 False False
# stable True True
for v in latest stable v2026.8.27; do
  printf '%s ' "$v"
  curl -s -o /dev/null -w '%{http_code}\n' \
    "https://btclib-node.readthedocs.io/en/$v/"
done
# latest 200
# stable 200
# v2026.8.27 404
```

- **`latest` follows the default branch, which is `main`**, and
  `default_version` is `latest`, so the root of the site serves the
  development tip. Both are Read the Docs' settings rather than the
  forge's, so a branch renamed here leaves `latest` following one that
  does not exist.
- **`stable` is the highest semantic-version tag**, chosen and moved by
  Read the Docs rather than by a setting of its own, and it is what
  `/en/stable/` serves.
- **What connects the project is the `read-the-docs-community` GitHub
  App**, installed on the organization for every repository, so this
  repository carries no webhook of its own and none to give a secret
  to:

  ```shell
  gh api orgs/btclib-org/installations \
    --jq '.installations[] | select(.app_slug == "read-the-docs-community")
          | [.app_slug, .repository_selection]'
  # ["read-the-docs-community","all"]
  gh api repos/btclib-org/btclib-node/hooks --jq 'length'
  # 0
  ```

- **A release tag has no URL of its own.** The version created for
  `v2026.8.27` is inactive and unbuilt, which is the 404 above. What
  activates each new tag is an automation rule the sibling projects
  carry and this one does not -- issue #596 has its form and the
  measurement -- and adding it is an action on Read the Docs' own side
  that no `gh api` call in this file takes or reads back, the same
  boundary the trusted publishers above sit on.
- **The repository's `.homepage` names this same site**, read back from
  the endpoint rather than from `pyproject.toml`'s own copy of it
  (issue btclib-org/.github#533):

  ```shell
  gh api repos/btclib-org/btclib-node --jq '.homepage'
  # https://btclib-node.readthedocs.io
  ```

  `[project.urls] homepage` carries the identical string: a releasing
  tree's home is its own documentation, not `btclib.org`, the sibling's
  project page the field named before.

## What is not configured, and why

- **No Pages**, `gh api repos/btclib-org/btclib-node/pages` answering
  `404`. What it would serve is `docs/source/` (issue #264), which the
  Read the Docs project above already publishes; `btclib` runs Pages
  over its own repository root instead, which is a website rather than
  a second copy of its documentation.

## What this file passes over

*What is not configured, and why* above records what this repository
decided against. This section is the other edge of the scope at the top:
what the API answers for and no section here reads back. What another
service decides and no call here reaches — the trusted publishers on the
two indices, the Read the Docs automation rule that would build a release
tag — is named where each of those arises above instead.

**What no call sets.** `gh api repos/btclib-org/btclib-node` answers the
whole repository document, and most of it is URLs, counts and state
GitHub derives from the tree rather than anything anybody sets. Nothing
here reads those back.

**A field the standard states no rule about, and no call above answers
alongside one it does.** `allow_forking`, `allow_update_branch`,
`archived`, `description`, `has_discussions`, `has_downloads`,
`has_pull_requests`, `is_template`, `pull_request_creation_policy` and
`web_commit_signoff_required` are in the repository document and in none
of the `--jq` objects here:

```shell
std=$(gh api repos/btclib-org/.github/contents/README.md --jq .content \
  | base64 -d)
for f in allow_forking allow_update_branch archived has_discussions \
         has_downloads has_pull_requests is_template \
         pull_request_creation_policy web_commit_signoff_required; do
  printf '%s %s\n' "$f" "$(printf '%s' "$std" | grep -c -- "$f")"
done
# each name, then 0
printf '%s' "$std" | grep -c '\.description'   # 0
printf '%s' "$std" | grep -c 'default branch'  # not 0
printf '%s' "$std" | grep -c '\.homepage'      # not 0
```

`description` is asked with its field spelling, the bare word being
ordinary prose in that file, and `\.homepage` is the control for that
same shape. The two controls are what make the zeros absences rather than
a pattern that cannot match, and feeding the loop `topics` answers
non-zero, which is what says it can still fail. Recording a field on no
rule grows this file with GitHub's API rather than with the standard.

`merge_commit_title` and `merge_commit_message` are the same case reached
from the other end: they compose a merge commit *Merge methods* above
reads back as a button this repository does not offer.

**A facility nobody reached for.** Actions variables, Dependabot secrets,
self-hosted runners, deploy keys, autolinks and custom property values
each answer empty, and an empty answer records no decision:

```shell
for e in actions/variables dependabot/secrets actions/runners \
         keys autolinks properties/values; do
  gh api "repos/btclib-org/btclib-node/$e" \
    --jq 'if type=="array" then length else .total_count end'
done
# 0, once per endpoint
gh api repos/btclib-org/btclib-node/environments --jq .total_count
# 2
```

The last call is the control: an endpoint of this repository's that
answers non-empty, so the zeros above it are absences rather than a
call that reports nothing. Webhooks are not among them: *Read
the Docs* above reads that endpoint back, an empty answer there being
what says the integration is the GitHub App rather than a hook of this
repository's.

**A credential the organization holds.** `claude-review.yml` runs on
`CLAUDE_CODE_OAUTH_TOKEN`, an organization secret visible to every
repository, so this repository sets nothing for it and has nothing of its
own to read back:

```shell
gh api orgs/btclib-org/actions/secrets \
  --jq '.secrets[] | "\(.name) \(.visibility)"'
# CLAUDE_CODE_OAUTH_TOKEN all
gh api repos/btclib-org/btclib-node/actions/secrets --jq '.total_count'
# 0
```

The pair is what says the repository's own zero is inheritance rather
than a credential it lacks.

The price of the scope is a silent flip. A change to any of the above
shows up in nothing here, and what would find it is somebody reading the
repository document against this file rather than a command.
