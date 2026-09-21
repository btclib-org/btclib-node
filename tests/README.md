# Tests

## Suite layout

Section 7 of the [organization standard][std] admits a suite split by
kind, rather than by module, where a test's subject is a running process
instead of something `tests/unit/` can mirror — and asks that the split
be declared here, with its reason.

`tests/unit/` mirrors `src/btclib_node/`, module for module, and is free
to reach into the object under test directly. A module there with no
source counterpart still belongs where its subject is `src/btclib_node/`
taken whole rather than one file of it -- `unit/all_test.py`'s `__all__`
convention, `unit/docs_test.py`'s documentation convention -- or the
suite's own machinery: `tests/__init__.py`, `conftest.py`, or the pytest
configuration it runs under, which is what `unit/helpers_test.py`,
`unit/coverage_floor_test.py`, `unit/harness_test.py` and
`unit/thread_exception_origin_test.py` each read, and a declaration this
file keeps about the suite itself, which
`unit/conventions_test.py` reads (a module named without a leading
`tests/` anywhere in this file is resolved against `tests/`, `unit/`
marking one under `tests/unit/`). `tests/functional/` and
`tests/integration/` hold what has no module to mirror either, told
apart by what each needs:
`functional/` starts a `Node` itself and speaks to it over its p2p or RPC
socket, needing nothing the repository does not ship; `integration/`
speaks to a real `bitcoind` instead, which the repository does not ship,
and every test in it skips itself without one.

`tests/` root holds what neither of those is the subject of: a script
under `.github/scripts/` (`check_core_citation_pin_test.py` and its two
release-wait siblings), the corpus and property layer over `fuzz/`'s own
harnesses (`fuzz_corpus_test.py`, `property_test.py`), or a rule or fact
recorded across files no one directory owns, such as
`interpreters_test.py`'s reading of `pyproject.toml`, `.python-version`
and every workflow at once, and `pragma_test.py`'s reading of every
tracked `.py` file for `pyproject.toml`'s own pragma-reason rule.
`pyproject.toml`'s `testpaths` names each of those individually, beside
the three directories, so a bare run is the whole suite for that reason
as well as because the three directories are each in it.

## Convention tests

Section 7 of the [organization standard][std] lists convention-test
bullets a suite can turn into a red test, and says a repository needs
the ones its own conventions state in prose, plus the public surface,
which a repository publishing an importable package has whether its
prose states it or not.

So which of them this repository tests is **declared here**, in two
halves that together account for every convention section 7 lists: the
table below and the "Not tested here" line under it.
`unit/conventions_test.py` asserts the declaration is true, and what its
assertions catch is written in that module's docstring, a second list
here being the statement section 9 refuses.

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
`property_test.py` is that layer, with **hypothesis**, which is section
7's own named shape (btclib-org/btclib-node#742).

The property is the one every harness already states in prose and
encodes as a `contextlib.suppress`: over unconstrained octets a declared
entry point either returns or raises `BTClibException`, and nothing
else. Each `fuzz_target` suppresses that family and lets atheris report
anything else; each test there suppresses it and lets pytest report
anything else. The same sentence, over a domain the suite describes and
the weekly `fuzz` run then extends -- a fuzzer extending no described
domain having nothing to contradict.

The specs are not listed there. They come from the harnesses' own
module-level `ENTRY_POINTS`, through `fuzz_corpus_test.py`'s existing
walk, so a harness added for a new parser gets a property test by being
added. What the walk skips is structural rather than a name list: a
`fuzz.`-prefixed spec is one `_resolve` loads out of `fuzz/` by path,
and today's is a dispatch through a whole `Node` rather than a parse --
section 7's own carve-out being that a subject which is not a parser
does not owe the layer.

**hypothesis rather than hand-rolled properties**, which section 7
allows if declared here. The domain is unconstrained octets, which is
`binary()` and nothing more, so hand-rolling buys no fidelity and loses
the shrink: with a `RuntimeError` planted in `frame_message_bytes` and
executed rather than reasoned about, the search reported the minimal
`data=b'\x00\x00\x00'`, where a seeded generator reports whichever
blob it happened to draw. This tree also had no hand-rolled property
machinery to build on, so the alternative was writing a generator as
well as the properties.

`fuzz_corpus_test.py` remains a different thing: it holds every seed
under `fuzz/corpus/` to parsing and reserializing to itself, which is a
check on the seeds. A test stating a property over the values it names
-- `unit/chainstate/muhash_test.py`'s commutativity, say -- is a vector
test, and stays one.

The profiles are registered once in `conftest.py`, not repeated on every
`@given`: `default` at 500 examples, `thorough` at 2000, selected by
`HYPOTHESIS_PROFILE`. `deadline=None`, a per-example time limit being a
timing flake on whichever cell of the matrix is slowest, and this suite
runs one on `windows-latest`. The deep profile is opt-in because the
search that finds a latent defect is not one to run at every commit, and
what it finds graduates into a vector test rather than staying in a
search that may not repeat it.

[std]: https://github.com/btclib-org/.github/blob/main/README.md
