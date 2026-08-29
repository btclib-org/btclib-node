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

# `python-version: "3.14"` and `python-version: ["3.14", "3.14t"]`, the
# two shapes the setup steps here are given, and the `--python 3.14` a
# `run:` step hands uv directly. `python-version: ${{ matrix.* }}` is
# neither: an expression names no version, and the matrix it reads is
# already matched where that matrix is written
_NAMED = re.compile(r'python-version: ("[^"\n]*"|\[[^]\n]*\])|--python (\S+)')
_QUOTED = re.compile(r'"([^"]*)"')

# the files naming an interpreter, named rather than counted: a pattern
# that stopped matching one of them would leave the rest agreeing with
# each other and this module green
_NAMES_ONE = (
    ".github/actions/dev-version/action.yml",
    ".github/workflows/bootstrap-dns.yml",
    ".github/workflows/deps-latest.yml",
    ".github/workflows/fuzz.yml",
    ".github/workflows/mutation.yml",
    ".github/workflows/os-macos.yml",
    ".github/workflows/os-ubuntu.yml",
    ".github/workflows/os-windows.yml",
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
