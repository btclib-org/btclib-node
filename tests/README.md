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

## Order

Section 7 of the [organization standard][std] installs `pytest-randomly`
and needs no flag for it, and asks a suite that declines the shuffle to
say so here with the reason. This suite takes it, and carries
`pytest-order` beside it; what each of the two holds is said here
because their names read as opposites.

`pytest.mark.order` sits on one test, `functional/p2p/download_test.py`'s
`test_download`, and what it holds is a place in the queue rather than a
sequence: that test is among the slowest in the suite, and under
`-n auto` a slow test drawn last is what the whole run then waits on.
No test here needs another to have run before it -- `conftest.py`'s
fixtures give each node they build its own ports and its own
`tmp_path` -- so the shuffle has nothing to hold still in `unit/` or in
`functional/`, and every run is the check on that: a test that comes to
lean on what ran before it fails under some seed rather than passing
until a reader notices.

The two plugins compose rather than compete. `pytest-randomly` reorders
the collection ahead of every other plugin and `pytest-order` sorts
after them, by the hook order each declares, so `test_download` still
runs first and everything behind it is shuffled; `--indulgent-ordering`
would give the shuffle the last word, and is not passed. `-p no:randomly`
puts the collection order back to reproduce a failure against it, and
the seed a run prints reproduces the shuffle. The reseeding of `random`
the plugin does before every test reaches nothing of this tree's:
the suite's generators draw from `secrets`, and `download.py` draws from
`SystemRandom`, which `random.seed` does not touch.

## Property tests

Section 7 keys a property layer on the property section 10 keys the
fuzzer on -- nobody standing between a parser and an adversary who
chooses the bytes -- and this tree has it: `fuzz.yml`'s own header says
so, and each harness under `fuzz/` names what it owns of that surface.
The layer the fuzzer presupposes is not here. `hypothesis` is in no
dependency group and nothing under `tests/` imports it;
`fuzz_corpus_test.py` holds every seed under `fuzz/corpus/` to parsing
and reserializing to itself, which is a check on the seeds and not a
property searched over a domain; and a test here that states a property
-- `unit/chainstate/muhash_test.py`'s commutativity, say -- states it
over the values it names, which is a vector test.
btclib-org/btclib-node#742 is that gap.

[std]: https://github.com/btclib-org/.github/blob/main/README.md
