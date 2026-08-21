# Reviewing a btclib_node pull request

What a review here establishes before it gives an ack, what a finding
must contain, and what becomes of everything it notices that the diff is
not about.

This is the organization's standard, narrowed to what is true of this
repository today. Where it is thinner than btclib's own `REVIEWING.md`,
that is because this tree is mid-normalization and a rule with nothing
behind it is worse than no rule. The organization standard is
[btclib-org/.github](https://github.com/btclib-org/.github).

## The standard an ack is given against

**A diff is acked when it leaves the tree better than it found it**, not
when it is the diff the reviewer would have written. Perfection is not
the bar and is not reachable; the question is whether `main` with this
change is in better shape than `main` without it. The formulation is
Google's
[standard of code review](https://google.github.io/eng-practices/review/reviewer/standard.html).

Two things follow, and they are what reviews get wrong in opposite
directions:

- **A matter of taste is not a finding.** Where the author's choice and
  the reviewer's are both defensible, the author's stands. Say it once
  as a nit, if it is worth saying at all.
- **Work the diff never set out to do is not a finding either.** It is
  an issue; the section below is the whole of what to do with it.

## What is under review

1. **A sha, never a branch.** `gh pr view <N> --json
   url,headRefOid,baseRefName` — `headRefOid` is what is reviewed, and a
   branch name moves under a review that names it.
1. **The issues it closes**, all of them: answering one of two is a
   finding.
1. **The diff against the pull request's base**, not against `main`:
   `git diff <baseRefName>...<headRefOid>`, three dots. A finding that
   belongs to the parent goes on the parent's pull request.
1. **The tree at that sha**, checked out.

Read the whole diff before writing the first comment. A comment on line
5 that line 60 answers costs the author a reply and the reviewer their
credit for the rest of the review.

## What to look for

In priority order, stopping at what this diff can be wrong about:

- **Does it answer its issues?** The whole of them, and nothing beside.
  A diff carrying an unrelated fix cannot be acked for either question.
- **Is it correct?** Reason about the code as it will run, not as it
  reads. Where a claim can be checked, check it rather than believing
  it.
- **Does it break a rule this repository states?** Not a rule the
  reviewer would have written — one this repository's own documents
  state, cited by the line that states it. This is the class of finding
  a review exists for: the author has the diff in view and the document
  out of view.
- **Is what it adds tested the way this repository tests things?**
- **Is it simpler than it needs to be?** As a non-blocking finding, and
  never as a rewrite.

**Never review what a hook already gates.** Formatting, import order and
line length are decided by `.pre-commit-config.yaml`, and a comment
about one of them is either wrong or a bug in the hook.

## Every collateral finding becomes an issue

A review notices more than its subject: a defect the diff did not cause,
a document gone stale, a rule the tree quietly stopped following.
**None of it is a review comment, and every one of it is an issue.**
File it, and go back to the diff.

The reason is the author's round trip. A finding they cannot address
without leaving the subject is a round of review spent on something the
pull request was not for, and asking for it anyway is how a branch stops
converging. Filing costs one command and loses nothing.

What is *not* collateral, and stays in the review, is what this diff
introduces or breaks, and what was already wrong and this diff makes
materially worse. The test is not whether the code sits on a changed
line; it is whether this change put it there or made it worse.

Look for the issue already open before filing another:

```shell
gh issue list --state open --search "<the thing, in a word or two>"
```

Name the issues filed at the foot of the summary, under a line saying
they are **not** findings against this pull request. Without that line
the list reads as more things to fix before merging, which is the
opposite of what filing them was for.

## What a finding says

- **Where**: `file:line`, as an inline comment wherever a line is the
  subject.
- **What is wrong**, in a sentence.
- **How it is known**: the command and what it printed, or the concrete
  path through the code that produces the wrong result.
- **What kind it is**, said explicitly and never left to be inferred:

  - **blocking** — wrong, misleading or unmaintainable on `main`
  - **non-blocking** — worth doing, does not hold the ack
  - **nit** — taste; said once and never repeated
  - **question** — something not reproduced, asked as a question
      rather than asserted as a defect

Labelling every comment is
[conventional comments](https://conventionalcomments.org/)' idea and its
whole value: an unlabelled remark makes the author guess whether it
holds the merge, and they guess conservatively, which turns a nit into a
round of review.

No speculation dressed as a defect, no "consider maybe", no restating
what the diff plainly does. The subject is the code and never its
author: "this returns the wrong sign for a negative scalar", not "you
forgot the sign".

## The gates, and what this repository has instead

Run what exists on that sha, and read **exit codes, not filtered
output** — a pipe into `grep -v Passed` hides the failure it was meant
to find.

**This repository is mid-normalization, and the honest position is to
say so in the review rather than imply gates that are not there.** What
that means today:

- there are **no CI workflows** beyond this review — no test, lint or
  docs job runs on a pull request, so nothing green is standing behind
  the author's word;
- `pre-commit run --all-files` has **pre-existing failures on `main`**.
  A failure a review reports must be shown to be *this diff's*, by
  running the same hook against the base;
- the suite does not run out of the box on every machine. A collection
  error is not a finding until it is reproduced against the base.

A gate that fails locally, and demonstrably fails *because of this
diff*, is the strongest finding available. A gate that passes is not
evidence that the diff is right.

## What a review of this tree checks that a generic one would not

- **Is the interpreter floor still both a floor and a ceiling?** A
  dependency's C extension pins the upper end as well as the lower, so a
  change to `requires-python` or to `.python-version` is a claim about
  both.
- **Is the `btclib` pin still exact, and does the diff know why?** It is
  not the organization's usual unbounded floor: the newer releases break
  this tree. A relaxation is a decision, not a tidy-up.
- **Does the diff state a count** — of tests, of entries, of seconds?
  The organization standard says why it must not, and nothing here
  checks it yet.
- **A new workflow**: every action pinned to a commit SHA with the tag
  in a trailing comment, `permissions:` declared, `timeout-minutes:` on
  every job, and `persist-credentials: false` on every checkout.

## The verdict

Inline comments for the line-anchored findings, then exactly one summary
comment whose last line is one of two forms:

```text
CHANGES REQUESTED <sha>
```

```text
ACK <sha>
```

Nothing else is an ack — not "looks good", and not a forge approval,
which GitHub refuses to the author of the pull request. It names the sha
because an ack belongs to a tree and not to a branch.

The summary says, in a few lines, what was reviewed — the sha, what was
run and what was not —, lists the blocking findings, and names the
issues filed. No blocking findings and no ack is a contradiction: either
the finding is blocking or the ack is due.

## Re-review

The delta is `git diff <old-sha>..<new-sha>`, and there is one to read
because a correction is added as a commit rather than amended in.

- **Resolve every thread the author addressed, and only those.** A
  finding declined as out of scope and filed as an issue is addressed.
- Do not re-open settled ground, and do not introduce a preference late.
  A new blocking finding at round three is legitimate only if the new
  commits introduced it, or if leaving it would be wrong on `main`.
- Where the author declined something still considered blocking, say so
  once, with the argument. If they hold their position, do not spend a
  fourth round: withhold the ack and put both positions to a human.
  **Escalating is a result**; a stalemate repeated in silence is not.
- An acked pull request comes back for one more round when a rebase
  **resolved a conflict**. The delta is the resolution and nothing else:
  a conflict resolved by one hand is the change that passes every gate
  and is still wrong.

Ack when every blocking finding is closed, what could be run was run on
that sha, and the diff answers its issues. Non-blocking findings and
nits do not hold an ack — say that they are left to the author.
