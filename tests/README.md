# Tests

## Suite layout

Section 7 of the [organization standard][std] admits a suite split by
kind, rather than by module, where a test's subject is a running process
instead of something `tests/unit/` can mirror — and asks that the split
be declared here, with its reason.

`tests/unit/` mirrors `src/btclib_node/`, module for module, and is free
to reach into the object under test directly. `tests/functional/` and
`tests/integration/` hold what has no module to mirror, told apart by
what each needs: `functional/` starts a `Node` itself and speaks to it
over its p2p or RPC socket, needing nothing the repository does not
ship; `integration/` speaks to a real `bitcoind` instead, which the
repository does not ship, and every test in it skips itself without one.
All three directories are in `testpaths`, so a bare run is still the
whole suite.

## Convention tests

Section 7 of the [organization standard][std] lists convention-test
bullets a suite can turn into a red test, and says a repository needs
the ones its own conventions state in prose, plus the public surface,
which a repository publishing an importable package has whether its
prose states it or not.

So which of them this repository tests is **declared here**, and
`conventions_test.py` asserts the declaration is true: every convention
named below is one of section 7's, every module named exists and holds
at least one test, and the two halves together account for every
convention section 7 lists.

| convention | tested in |
| --- | --- |
| the documentation | `unit/docs_test.py` |
| the public surface | `unit/all_test.py` |

Not tested here: the copyright header; the import graph;
the changelog; the build system; the calling convention;
input validation; the suite opens no socket.

[std]: https://github.com/btclib-org/.github/blob/main/README.md
