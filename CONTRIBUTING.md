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
