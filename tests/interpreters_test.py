# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The interpreters this package claims are the ones it runs on.

One fact, and each of these declares it: `requires-python` is the floor,
`Programming Language :: Python :: X.Y` is what PyPI shows whoever is
choosing the package, `.python-version` is what a bare `uv run` picks,
and what CI names is what actually runs. Nothing compared them, and
they drift in the direction hardest to notice -- a classifier left
behind when a floor moves is a package advertising an interpreter its
suite never touches, and the person it misleads is not reading this
repository.

Section 1 of the organization standard is what they have to say between
them, and the window it asks for is python.org's release cycle rather
than this tree's choice. This module does not know that calendar and
does not try to: python.org keeps it, and a test hard-coding a date
would be one more thing to move. What it holds is the weaker and
checkable claim that whatever these say, they say the same thing.

The window here is one version wide, so every site naming an
interpreter names that one, which is why the comparison runs over every
workflow and composite action rather than over the platform sweeps
alone the way `btclib`'s copy of this module does. `test.yml`'s matrix
carries a second value, the free-threaded build of the same version,
which a classifier does not distinguish.

`pyproject.toml` is parsed, `tomllib` being in the standard library at
this tree's floor. The workflow files are yaml and no dependency group
here carries a parser for that, so they are read with the pattern
below -- which reads a comment naming an interpreter as readily as a
step that runs one, the safe direction to be wrong in: a comment
arguing for a version this tree no longer classifies has gone stale
too.
"""

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).parents[1]
_PYPROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
_PROJECT = _PYPROJECT["project"]

# every yaml a job or a step reads an interpreter out of: the workflows
# and the composite actions they call. The issue templates and
# dependabot.yml are not CI and name no interpreter
_CI = sorted((_ROOT / ".github/workflows").glob("*.yml")) + sorted(
    (_ROOT / ".github/actions").glob("*/action.yml")
)

# a classifier naming one interpreter version: `:: 3` and `:: 3 :: Only`
# say something about the major version rather than about an interpreter
_CLASSIFIER = re.compile(r"^Programming Language :: Python :: (3\.\d+)$")
_PYPY_CLASSIFIER = "Programming Language :: Python :: Implementation :: PyPy"
# PyPI's free-threading classifiers, the bare one and its maturity
# levels alike: each is a claim about the code under a free-threaded
# build, and which level is claimed is not this module's question
_FREE_THREADING_CLASSIFIER = "Programming Language :: Python :: Free Threading"

# `python-version: "3.14"` and `python-version: ["3.14", "3.14t"]`, the
# two shapes the setup steps here are given, and the `--python 3.14` a
# `run:` step hands uv directly. `python-version: ${{ matrix.* }}` is
# neither: an expression names no version, and the matrix it reads is
# already matched where that matrix is written.
#
# `_VERSION` is what says so, and both arms are held to it rather than
# one: the closure below asks whether the gate names an interpreter at
# all, so a token that is not a version but arrives as one leaves that
# question answered by nothing. A run of non-space hands it `${{` out
# of `--python ${{ matrix.python }}`, and whatever sits between the
# quotes hands it `${{ matrix.python }}` out of a quoted expression --
# two characters apart, and the second is the ordinary thing to write.
# No expression reaches either arm now; what the closure still cannot
# see is named where it is read.
_VERSION = r"[\w.+-]+"
_NAMED = re.compile(rf'python-version: ("[^"\n]*"|\[[^]\n]*\])|--python ({_VERSION})')
_QUOTED = re.compile(rf'"({_VERSION})"')

# the merge gate, and inside it the jobs a landing waits on. Section 3
# of the organization standard declares a free-threading classifier
# where the gate exercises that build, a gate being what refuses the
# landing that breaks it, so what answers here is the aggregate's
# `needs:` closure rather than the file: a job of this workflow that no
# required check waits on reports what a sweep reports, which is the
# ground that section declines. The aggregate is found by the name
# `main`'s required contexts hold, which is a job's `name:` and not its
# key
_GATE = _ROOT / ".github/workflows/test.yml"
_AGGREGATE = "test: every job passed"
# `jobs:` and everything under it: the trigger keys of `on:` sit at the
# same indent as a job key, so a pattern that did not cut here would
# offer `pull_request` to the closure below as though it were a job
_JOBS = re.compile(r"^jobs:\n(?P<block>.*)\Z", re.MULTILINE | re.DOTALL)
# a job key at the one indent `jobs:` gives them, and the block that
# follows it
_JOB = re.compile(
    r"^  (?P<key>[a-z0-9_-]+):\n(?P<block>(?:^ {3,}.*\n|^\n)*)", re.MULTILINE
)
_NEEDS = re.compile(r"^    needs: (?P<needs>\[[^]\n]*\]|\S+)", re.MULTILINE)
_NAME = re.compile(r'^    name: "?(?P<name>[^"\n]*)"?', re.MULTILINE)
# comments are dropped before the read above runs, where `_named` above
# keeps them on purpose. The two want opposite things: a stale comment
# naming a version this tree no longer classifies is worth catching,
# where this file's own header argues at length about `3.14t` and a
# comment is not a job that runs it.
#
# What this read does not answer shows in `dist`. A setup step naming
# no interpreter takes the one `.python-version` pins, so that job's
# own interpreter is nowhere in this workflow -- `_PIN` above is where
# the pin is read, and a free-threaded pin reaches it as `3.14t`. And a
# version a job names counts whatever that job does with it: `dist`'s
# `--python` runs the sdist normalizer and the bill-of-materials writer
# under `--no-project`, which import nothing of this package. A step
# skipped on its own condition counts the same way -- `free-threaded`'s
# suite runs only where its sync succeeded -- and issue #750 is the
# gap. Nor does this read leave the workflow: an interpreter a gating
# job takes from a composite action it calls is outside it, `dist`
# calling `./.github/actions/dev-version`, which hands uv a version of
# its own.
_UNCOMMENTED = re.compile(r"(?:^|\s)#.*$", re.MULTILINE)


def _jobs() -> dict[str, str]:
    """Return each job of the merge gate, its comments dropped."""
    text = _JOBS.search(_UNCOMMENTED.sub("", _GATE.read_text(encoding="utf-8")))
    assert text, f"{_GATE.name} declares no jobs"
    return {match["key"]: match["block"] for match in _JOB.finditer(text["block"])}


def _needed(jobs: dict[str, str], key: str) -> set[str]:
    """Return `key` and every job it waits on, however deep."""
    found = {key}
    pending = [key]
    while pending:
        block = jobs.get(pending.pop(), "")
        for match in _NEEDS.finditer(block):
            listed = match["needs"].strip("[]").replace(",", " ").split()
            for name in listed:
                if name not in found:
                    found.add(name)
                    pending.append(name)
    return found


def _gating() -> set[str]:
    """Return every interpreter a job the merge gate waits on names."""
    jobs = _jobs()
    named = {key: _NAME.search(block) for key, block in jobs.items()}
    aggregate = next(
        (key for key, name in named.items() if name and name["name"] == _AGGREGATE), ""
    )
    assert aggregate, f"{_GATE.name} carries no job named {_AGGREGATE!r}"
    found: set[str] = set()
    for key in _needed(jobs, aggregate):
        found.update(_named(jobs[key]))
    return found


# the files naming an interpreter, named rather than counted: a pattern
# that stopped matching one of them would leave the rest agreeing with
# each other and this module green
_NAMES_ONE = (
    ".github/actions/dev-version/action.yml",
    ".github/workflows/bootstrap-dns.yml",
    ".github/workflows/deps-latest.yml",
    ".github/workflows/deps-oldest.yml",
    ".github/workflows/fuzz.yml",
    ".github/workflows/mutation.yml",
    ".github/workflows/os-macos.yml",
    ".github/workflows/os-ubuntu.yml",
    # pypi-install.yml installs the published package from the index and
    # imports it, the runner's own default interpreter being below this
    # project's `requires-python`, so it names the one uv resolves
    # (btclib-org/btclib-node#502)
    ".github/workflows/pypi-install.yml",
    ".github/workflows/test.yml",
)


def _named(text: str) -> set[str]:
    """Return every interpreter version one CI file names literally."""
    found: set[str] = set()
    for match in _NAMED.finditer(text):
        value, bare = match.groups()
        found.update(_QUOTED.findall(value) if value else [bare])
    return found


def _ordered(version: str) -> tuple[int, ...]:
    """Return a version as the numbers that sort it, `3.9` below `3.10`."""
    return tuple(int(part) for part in version.split("."))


_FLOOR = str(_PROJECT.get("requires-python", "")).removeprefix(">=")
_CLASSIFIED = tuple(
    match[1] for match in map(_CLASSIFIER.match, _PROJECT["classifiers"]) if match
)
# a whole-line comment is what .python-version takes -- a trailing one
# on the version line makes uv ignore the file -- so what is left once
# they are dropped is the pin. The `t` of a free-threaded build goes
# with them: it asks for that version without the GIL, which is the
# same version as far as a classifier is concerned
_LINES = (_ROOT / ".python-version").read_text(encoding="utf-8").splitlines()
_PIN = next(
    (line.strip() for line in _LINES if line.strip() and line.lstrip()[0] != "#"), ""
).removesuffix("t")

# `as_posix()`, not `str()`: a `PurePath`'s own `__str__` renders with
# `os.sep`, backslashes on a `WindowsPath` (`pathlib`'s own
# documentation for `PurePath.__str__`), while `_NAMES_ONE` above is
# written with forward slashes -- so `str()` here compared a Windows
# path against a POSIX literal on `windows-latest` and nowhere else,
# `_NAMING == _NAMES_ONE` below false for every entry despite naming
# the same file. `as_posix()` renders with `/` on every platform
# `pathlib` runs on, POSIX included, so this is nothing more than what
# `str()` already did there (btclib-org/btclib-node#663).
_RUN = {
    path.relative_to(_ROOT).as_posix(): _named(path.read_text("utf-8")) for path in _CI
}
_NAMING = tuple(sorted(path for path, versions in _RUN.items() if versions))
_CPYTHON = tuple(
    sorted({version.removesuffix("t") for name in _NAMING for version in _RUN[name]})
)


def test_every_declaration_was_read() -> None:
    """Each of them was found, so the checks below quantify over it.

    A key renamed, a classifier reindented, a step rewritten: each would
    leave one of these empty and every comparison below trivially true.
    """
    assert _FLOOR, "pyproject.toml declares no requires-python"
    assert _CLASSIFIED, "pyproject.toml declares no per-version Python classifier"
    assert _PIN, ".python-version names no interpreter"
    assert _NAMING == _NAMES_ONE, (
        f"an interpreter is named in {', '.join(_NAMING) or 'no CI file'},"
        f" and the files that carry one are {', '.join(_NAMES_ONE)}"
    )


def test_the_floor_is_the_lowest_classifier() -> None:
    """`requires-python` and the classifiers name the same oldest Python."""
    lowest = min(_CLASSIFIED, key=_ordered)
    assert lowest == _FLOOR, (
        f"requires-python is >={_FLOOR} and the lowest classifier is"
        f" {lowest}: one of the two was moved and the other was not"
    )


def test_the_pin_is_the_newest_classifier() -> None:
    """`.python-version` and the classifiers name the same newest Python."""
    newest = max(_CLASSIFIED, key=_ordered)
    assert newest == _PIN, (
        f".python-version pins {_PIN} and the newest classifier is"
        f" {newest}: a bare `uv run` is the version this package claims"
    )


def test_every_classified_interpreter_is_run() -> None:
    """A version PyPI advertises is a version CI runs."""
    unrun = [version for version in _CLASSIFIED if version not in _CPYTHON]
    assert not unrun, (
        f"classified and no CI file runs it: {', '.join(unrun)}."
        " PyPI shows a classifier to whoever is choosing this package"
    )


def test_every_interpreter_ci_names_is_classified() -> None:
    """A version CI runs is a version PyPI advertises."""
    unclassified = [version for version in _CPYTHON if version not in _CLASSIFIED]
    assert not unclassified, (
        f"run by a CI file and not classified: {', '.join(unclassified)}"
    )


def test_pypy_is_classified_exactly_when_it_is_run() -> None:
    """The PyPy classifier is a claim about what runs, not a decoration."""
    classified = _PYPY_CLASSIFIER in _PROJECT["classifiers"]
    run = any(version.startswith("pypy") for name in _NAMING for version in _RUN[name])
    assert classified == run, (
        f"the PyPy classifier is {'present' if classified else 'absent'} and"
        f" CI {'runs' if run else 'does not run'} a PyPy interpreter"
    )


def test_free_threading_is_classified_exactly_when_the_gate_runs_it() -> None:
    """The free-threading classifier is a claim about the merge gate.

    Section 3 of the organization standard declares one where the gate
    exercises the free-threaded build, a gate being what refuses the
    landing that breaks it. So a report-only cell does not answer for
    the classifier however loudly it runs: what it says is that the
    build passed somewhere, which is the claim that section rejects.
    """
    gating = _gating()
    assert gating, f"no job {_AGGREGATE!r} waits on names an interpreter"
    classified = any(
        classifier.startswith(_FREE_THREADING_CLASSIFIER)
        for classifier in _PROJECT["classifiers"]
    )
    run = sorted(version for version in gating if version.endswith("t"))
    assert classified == bool(run), (
        f"the free-threading classifier is {'present' if classified else 'absent'}"
        f" and the jobs {_AGGREGATE!r} waits on run"
        f" {', '.join(run) or 'no free-threaded interpreter'}"
    )
