# Tests

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
