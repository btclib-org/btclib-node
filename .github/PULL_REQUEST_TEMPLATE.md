<!-- markdownlint-disable-next-line first-line-heading -->
## What this changes

<!-- What the node does now that it did not do before, and why.
     CONTRIBUTING.md asks every change to start from an open issue, so
     there is normally one to link: "Closes #123". -->

## How it was verified

<!-- The test that covers it, the chain it was run against, the command
     you ran. New behaviour without a test is the usual reason a pull
     request waits: the suite is held to covering every statement and
     every branch, so a line nothing reaches fails the run rather than
     lowering a number. -->

## Checks

<!-- The gate runs the first two of these and reports; whether a red one
     blocks the merge is a repository setting rather than a file, and
     nothing names a status check here today, so reading them is the
     reviewer's job until something does. Running them locally is how
     you find out before either of you. -->

- [ ] `git add -A && uv run pre-commit run --all-files` is clean
- [ ] `uv run pytest` passes, coverage floor included
- [ ] What a reader would have to be told is written down: a comment
      saying why the code is as it is, or a line in the README if it
      changes how the node is run

## Anything the reviewer should know

<!-- A decision you are unsure of, an alternative you rejected, a
     specification that is ambiguous, a follow-up you left out on
     purpose. Consensus and storage changes especially: say what you
     believe cannot break, so somebody can try to. Delete the section if
     there is none. -->
