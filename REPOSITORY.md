# Repository configuration

What is set on this repository, as the `gh api` call that reads it back
and the answer that call gives today. A setting recorded as prose alone
is one nobody can check; recorded this way, a drift is one command away
from being seen.

The rules and the settings live *outside* the tree, so this file is the
whole of them: nothing below is recoverable by reading the repository.

## Required checks on main

Set through classic branch protection; no ruleset on `main` carries a
`required_status_checks` rule.

```shell
gh api repos/btclib-org/btclib-node/branches/main/protection \
  --jq '.required_status_checks.contexts[]'
# Lint and type-check
# test: every job passed
# Build the documentation
# Regtest against Bitcoin Core
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

The context is the job's `name:`, not the workflow's, and the array
above holds it as a literal string that nothing in the tree can keep in
step: **renaming the `regtest` job would leave a required check nothing
produces, and a merge would wait on it forever.** That is a change to
make here first, in the same order this section's own opening argues —
the setting, then the record of it.

`docs.yml`'s "Build the documentation" runs on every pull request
already, the way `lint.yml` and `test.yml` do (*What gates a merge, and
what only reports* in `CONTRIBUTING.md`), and `release.yml` calls it too
(btclib-org/btclib-node#264); its presence in the `contexts` array above
is what makes a red run on it block a merge into `main`, a repository
setting no pull request carries (this section's own opening sentence).
Adding a context to that array is a `gh api` PATCH:

```shell
gh api -X PATCH \
  repos/btclib-org/btclib-node/branches/main/protection/required_status_checks \
  -F strict=true \
  -f 'contexts[]=Lint and type-check' \
  -f 'contexts[]=test: every job passed' \
  -f 'contexts[]=Build the documentation' \
  -f 'contexts[]=Regtest against Bitcoin Core'
```

The array is rewritten whole rather than added to: a context left out of
that PATCH stops being required, silently. `-F` is what `strict` needs,
`-f` sending a string even for a boolean — btclib-org/btclib-node#453 is
where this command was first run rather than only documented.

Re-run the first command above to confirm the contexts still hold
— its answer, not this paragraph, is what is true today.

**`links.yml`, `os-macos.yml` and `bootstrap-dns.yml` must not become
required checks**, and neither must `claude-review.yml`. `links.yml` and
`bootstrap-dns.yml` ask whether somebody else's server answered,
`os-macos.yml` runs on a schedule and reports what a sweep sees rather
than what a pull request introduced, and `claude-review.yml` writes an
opinion for an author to weigh. Each says so in its own header.

`codeql.yml` is not among them. It runs on `pull_request` and carries an
aggregate job, `codeql: every job passed`, so its result is one context
a rule can name however many languages the matrix grows to
(btclib-org/.github#459). Whether the rule asks for it is the `contexts`
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
         message: .squash_merge_commit_message}'
# {"auto":true,"delete_on_merge":true,"merge":false,
#  "message":"COMMIT_MESSAGES","rebase":false,"squash":true,
#  "title":"COMMIT_OR_PR_TITLE"}
```

Squash is the only method, and `main-self-merge` names it too, so the
constraint holds even if this repository setting is flipped back.

`COMMIT_OR_PR_TITLE` is the subject — the pull request title with its
number, or the subject of the single commit where a branch has one, which
the convention of writing the two alike keeps the same text.
`COMMIT_MESSAGES` is the body — which is why a commit message here is
prose this tree ships.

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
# bitcoin, bitcoin-node, blockchain, consensus, full-node, p2p,
# script-interpreter, sqlite
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
Nothing here publishes, attests, or writes to the repository's contents.

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
```

**Private vulnerability reporting is on**, so *Report a vulnerability* on
this repository's Security tab opens an advisory only the maintainers
see. `SECURITY.md` states the call above rather than the answer, the
route being a setting and not a file — true of every repository whether
or not it keeps a policy of its own, this one now doing so as a tier-1
repository. `.github/ISSUE_TEMPLATE/config.yml`'s Security vulnerability
entry links straight to that advisory form.

The second command is what the open alerts are. Every one of them today
answers `poetry.lock` for its `dependency.manifest_path`, which is a file
this tree no longer has — issue #37 is where that is worked off, and its
remaining step is to read that list back now that `uv.lock` is what the
tree carries.

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
-- the platform axis `os-macos.yml` covers, among them.

`test.yml`'s own interpreter axis is the row this repository gates
instead of sweeping, and that file's own header carries the argument
rather than this section repeating it: the two cells run as parallel
jobs, so the second costs one more slot at the ceiling and no additional
wait, against a review that costs more than the wait regardless.

## What is not configured, and why

- **A release workflow, and no publishing yet.** `.github/workflows/release.yml`
  exists and `CONTRIBUTING.md`'s *A release path, and nothing published
  on it yet* is the whole of that answer and carries the commands
  behind it. There is still no `pypi` environment, `RELEASING.md`'s
  *One-time setup* not having been done:
  `gh api repos/btclib-org/btclib-node/environments --jq .total_count`
  answers `0`. There is a `v0.1.0` tag and a GitHub release with no
  artifact attached to it, and `tag-integrity` already holds the
  signature a tag would need.
- **No Pages, and no Read the Docs project connected.**
  `gh api repos/btclib-org/btclib-node/pages` answers `404`. `docs/source/`
  and `.readthedocs.yaml` exist (issue #264) and `docs.yml` builds them
  on every pull request, but connecting a Read the Docs project to this
  repository is a setting on that service's own side, outside what a
  `gh api` call here can read back or a pull request can carry, and this
  tree does not publish -- the day it does, `btclib`'s own "Read the
  Docs, which is btclib.readthedocs.io" section of its `REPOSITORY.md`
  is the shape this file's own section would take.
- **No `homepage`**, the answer to `.homepage` being empty. There is no
  published site for it to point at.
