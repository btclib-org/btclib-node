# Contributing

Every change starts with an open issue. A pull request needs an approving
review from somebody other than its author before it can merge — GitHub
does not allow a self-approval. `Closes #N` in the pull request's
description is what closes the issue once a reviewed pull request merges.

## What `main` enforces

`main` enforces four things on every commit that reaches it, not only on
review: a verified signature, linear history, no force push, no branch
deletion. These are a GitHub ruleset with no bypass actor, not a rule
trusted to hold on its own — a commit that is unsigned or that rewrites
history is rejected before it is something to review.

Commits need a verified signature (GPG, SSH or S/MIME — see
[About commit signature
verification](https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification)).

## What runs, and when

Two things run on a pull request that touches anything the suite reads:
the lint gate, which is `.pre-commit-config.yaml` run as you would run it
yourself, and the suite on one image and one interpreter, held to the
coverage floor `pyproject.toml` declares. A pull request that edits only
the root prose -- this file among it -- skips the suite and reports the
skip as a pass, which is what keeps a required check from blocking on a
run that never happened. That is the whole of the merge gate, and it is
deliberately the cheapest answer that can still refuse a broken change.
Whether a red one blocks the merge is a repository setting rather than a
file, and nothing names a status check here today -- neither `main`'s
ruleset nor the classic protection where the aligned siblings keep theirs
(btclib-org/.github#88). Read them yourself until one does.

Everything else answers on a schedule instead of holding a merge: the
links in the prose, CodeQL over the Python and the workflows, and the
same suite on macOS, the one platform pyproject.toml claims that no merge
gate runs. Which day each of them runs is one calendar for the whole
organization, in [section 10 of `btclib-org/.github`'s
README](https://github.com/btclib-org/.github/blob/main/README.md), not
repeated here.

The trade that calendar makes is worth knowing before you rely on it: a
defect only a sweep can see sits on `main` until that sweep runs, at
most a week.
