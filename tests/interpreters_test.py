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
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

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
# own `--python` runs the sdist normalizer and the bill-of-materials
# writer under `--no-project`, which import nothing of this package.
#
# A step carrying an `if:` of its own is a narrower thing than either,
# because the job can conclude without it having run. Two shapes reach
# that same end and this read separates neither: a step keyed on a
# prior step's outcome is skipped when `continue-on-error: true`
# upstream lets that step fail without reddening the job, which is
# `free-threaded`'s own suite step, its sync failing on every run today
# (issue #723); and a step keyed on the run's own context is skipped
# whenever the context does not hold, which is `dist`'s call to
# `./.github/actions/dev-version`, gated `if: inputs.version-suffix
# != ''` and so run on a release rehearsal and on nothing else --
# neither a pull request nor a push to `main`, as `test.yml`'s own
# comment beside it says.
#
# So the rule is one rule: a conditioned step is not shown to have run,
# and what it names or reaches is not this read's business. `3.14t`
# named in `free-threaded`'s unconditioned setup step is refused for
# that reason (issue #750), and an interpreter reached only through
# `dist`'s rehearsal-only step is refused for the same one. Erring this
# way costs nothing that matters: an interpreter dropped is a claim of
# support this tree does not make, where an interpreter counted in is a
# claim that the gate exercises what it does not -- the "it passed
# somewhere" that section 3 of the organization standard refuses, and
# the whole reason issue #750 exists.
#
# Nor does this read stop at the workflow file: a `uses: ./...` an
# unconditioned step of a gating job calls is read too, the
# repository's own file and no network fetch, the same way `_CI` above
# reads every composite action. Issue #757 is what closes that gap.
# **No job the gate waits on is shaped that way today** -- the gate's
# only local-action call is the conditioned step above, and the tree's
# other one is `integration-bitcoind.yml`'s, which no gate job reaches
# -- so the follow is held by a unit test on job text of its own rather
# than by the real workflow, which is what a walk that currently finds
# nothing has to be. A `uses:` pinned to a third party's own commit is
# not read this way at all: fetching it would be the network call this
# test does not make, so an interpreter such an action hard-codes
# outside the inputs its caller passes stays outside this read's reach.
_UNCOMMENTED = re.compile(r"(?:^|\s)#.*$", re.MULTILINE)
# one step of a job: the list marker's own line, then whatever it
# indents under, a blank line included since a multi-line `run: |`
# block can carry one. A job's own attributes -- `if:`, `needs:`,
# `runs-on:` -- sit at the shallower indent `_JOB` gives the block as a
# whole and are never mistaken for a step by this.
#
# That blank alternative is `^ *\n` and not `^\n` because a step's own
# text reaches this already stripped, and `_UNCOMMENTED` takes one
# whitespace character with the comment it removes: a comment on its
# own line at a step's indent leaves eight spaces less one, which is
# one short of the threshold beside it and matches neither branch. The
# step would end there, hiding every line below it in the same step --
# an `if:` this read is about among them -- and no test would go red,
# the shape being one this tree's own workflows already write
# (`bootstrap-dns.yml`, `claude-review.yml`). `_JOB` above needs
# nothing of the sort: its `^ {3,}` runs a space below the indent it
# reads, and that slack is what absorbs the same residue.
_STEP = re.compile(r"^      - .*\n(?:^ {8,}.*\n|^ *\n)*", re.MULTILINE)
# any `if:` a step carries, at a step's own indent -- a job's `if:` sits
# at the shallower indent `_JOB` gives it and is not matched here. Not
# narrowed to one shape of condition: the header above has the two this
# workflow actually carries, and both end with the job concluding on a
# step that did not run. A condition that always holds -- `always()`,
# say -- is caught by this too and its interpreter dropped, which is
# the direction to be wrong in.
#
# `      - if:` is the same key on the step's own dash line rather than
# under it, which YAML allows and this tree writes for other keys
# (`lint.yml` has three `- uses:` that way). An anchor on eight spaces
# alone misses it, and missing it is the unsafe direction here: a step
# conditioned that way would read as one that always runs, which is the
# over-count issue #750 exists to refuse. Both patterns take the dash
# form for that reason, `_LOCAL_ACTION` for symmetry rather than for
# safety, its own miss erring the other way
_CONDITIONED = re.compile(r"^(?:        |      - )if:", re.MULTILINE)
# `uses: ./...`: the repository's own composite action, read by path
# rather than fetched. A third-party `uses:` names an owner and a commit
# instead and is not matched here
_LOCAL_ACTION = re.compile(r"^(?:        |      - )uses: (\./\S+)", re.MULTILINE)


def _runs_the_suite(block: str) -> bool:
    """Whether some step of this job invokes pytest under no `if:` of its own.

    A job carrying no pytest step at all -- `dist`, say -- is not what
    this asks about and answers `True` by default, `_gating` below
    reading such a job's named interpreters regardless. Only a job
    every one of whose pytest steps is conditioned answers `False`.
    """
    pytest_steps = [step for step in _STEP.findall(block) if "pytest" in step]
    return not pytest_steps or any(
        not _CONDITIONED.search(step) for step in pytest_steps
    )


def _unconditioned(block: str) -> str:
    """Return this job's text with every conditioned step removed.

    What a step carrying an `if:` names, and what it reaches, is read
    out of the job before anything else looks at it -- one excision
    rather than a rule repeated at each reader. What a job declares
    outside any step, a `strategy:` matrix among the shapes, survives
    it: no gate job here writes one today, and dropping it would be a
    gap this excision has no reason to open.
    """
    for step in _STEP.findall(block):
        if _CONDITIONED.search(step):
            block = block.replace(step, "", 1)
    return block


def _reached(block: str) -> Iterator[Path]:
    """Yield each local action a step of this job calls.

    Called on `_unconditioned` text, so a conditioned step's own
    `uses:` never reaches this.
    """
    for path in _LOCAL_ACTION.findall(block):
        yield _ROOT / path / "action.yml"


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


def _found(jobs: dict[str, str], closure: set[str]) -> set[str]:
    """Return every interpreter the jobs of `closure` name.

    A job's own text is read only where `_runs_the_suite` finds nothing
    to doubt in it (issue #750), and a local composite action only
    where the step calling it is itself unconditioned (issue #757).
    The two are one rule read at two scales: what a step that may not
    run names, or reaches, is not evidence the gate runs it.

    That the scales differ is not an oversight. A `uses:` belongs to
    one step, so the step's own `if:` answers for it exactly. A version
    a job merely *names* does not: `_named` cannot tell a
    `python-version:` that installs an interpreter from a `--python`
    that runs one, so there is no step to attribute the name to and the
    job as a whole is the only unit left. `_runs_the_suite` is that
    coarser instrument, and it is coarse in the safe direction -- it
    withholds every name of a doubtful job rather than admitting one,
    so where it is wrong `_gating` is short and never long. Kept apart
    from `_gating` below so that a test can call this on job text of
    its own, the real gate taking neither branch on any job it waits on
    today.
    """
    found: set[str] = set()
    for key in closure:
        block = jobs[key]
        # `_runs_the_suite` reads the job whole, the conditioned pytest
        # step included: it asks whether that step may be skipped, which
        # an excision would hide from it by leaving no pytest step at all
        runs = _runs_the_suite(block)
        block = _unconditioned(block)
        if runs:
            found.update(_named(block))
        for action in _reached(block):
            text = _UNCOMMENTED.sub("", action.read_text(encoding="utf-8"))
            found.update(_named(text))
    return found


def _gating() -> set[str]:
    """Return every interpreter the merge gate is shown to run.

    Not every interpreter such a job *names*: `_found` above drops the
    name a job carries when every pytest step of it is conditioned, and
    adds the one a local action pins where the step calling it is not
    (issues #750 and #757).
    """
    jobs = _jobs()
    named = {key: _NAME.search(block) for key, block in jobs.items()}
    aggregate = next(
        (key for key, name in named.items() if name and name["name"] == _AGGREGATE), ""
    )
    assert aggregate, f"{_GATE.name} carries no job named {_AGGREGATE!r}"
    return _found(jobs, _needed(jobs, aggregate))


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


def test_found_discounts_a_job_whose_only_pytest_step_may_not_run() -> None:
    """A job shaped like `free-threaded` names nothing `_found` trusts.

    None of the jobs `test-passed` waits on today are shaped this way --
    `free-threaded` is, and issue #723 is why it is not among them -- so
    this exercises `_found` on text of its own rather than waiting for
    the gate to grow a job like it.
    """
    maybe_skipped = (
        "      - name: Setup uv\n"
        "        uses: astral-sh/setup-uv@x\n"
        "        with:\n"
        '          python-version: "3.14t"\n'
        "      - name: Run the suite\n"
        "        if: steps.sync.outcome == 'success'\n"
        "        run: >\n"
        "          uv run --locked --no-default-groups --group test pytest\n"
    )
    ordinary = (
        "      - name: Setup uv\n"
        "        uses: astral-sh/setup-uv@x\n"
        "        with:\n"
        '          python-version: "3.14"\n'
        "      - name: Run the suite\n"
        "        run: >\n"
        "          uv run --locked --no-default-groups --group test pytest\n"
    )
    jobs = {"maybe-skipped": maybe_skipped, "ordinary": ordinary}
    assert _found(jobs, {"maybe-skipped"}) == set()
    assert _found(jobs, {"ordinary"}) == {"3.14"}
    assert _found(jobs, {"maybe-skipped", "ordinary"}) == {"3.14"}


def test_found_follows_a_local_action_regardless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `uses: ./...` a job calls names an interpreter of its own.

    `dist` is the one job shaped this way in the real gate, calling
    `./.github/actions/dev-version` -- built here on a directory of its
    own instead, so the assertion is about `_found` rather than about
    that action's own file staying at `--python 3.14`.

    *Regardless* is what the caller here is shaped to exercise: its own
    pytest step is gated, so `_runs_the_suite` refuses the `3.14` the
    job names, and the `3.14t` the action pins arrives anyway. Asserting
    that the answer is exactly `{"3.14t"}` is therefore one assertion
    about both halves -- a follow made conditional on `_runs_the_suite`
    answers the empty set here, where a caller with no pytest step at
    all would pass either way.
    """
    (tmp_path / "action").mkdir()
    (tmp_path / "action" / "action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n"
        "    - shell: bash\n      run: uv run --no-project --python 3.14t true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "_ROOT", tmp_path)
    caller = (
        "      - name: Setup uv\n"
        "        uses: astral-sh/setup-uv@x\n"
        "        with:\n"
        '          python-version: "3.14"\n'
        "      - name: Run the suite\n"
        "        if: steps.sync.outcome == 'success'\n"
        "        run: >\n"
        "          uv run --locked --no-default-groups --group test pytest\n"
        "      - name: A step\n"
        "        uses: ./action\n"
    )
    assert _found({"caller": caller}, {"caller"}) == {"3.14t"}


def test_found_refuses_a_step_conditioned_on_its_own_dash_line() -> None:
    """`- if:` conditions the step as surely as an `if:` below it.

    YAML puts a mapping's first key on the sequence's own dash line or
    on the line under it indifferently, and this tree writes both forms
    -- `lint.yml` carries three `- uses:`. A pattern anchored on the
    deeper indent alone sees only the second, and the miss is the unsafe
    one: the step reads as unconditioned, so the job counts as running
    an interpreter that may have been skipped, which is the over-count
    issue #750 exists to refuse. The shape is absent from `.github/`
    today, so this is the walk finding nothing again, held by job text
    of its own.
    """
    dash = (
        "      - if: inputs.version-suffix != ''\n"
        "        name: A step\n"
        "        run: uv run --no-project --python 3.14t true\n"
    )
    assert _found({"caller": dash}, {"caller"}) == set()
    always = dash.replace("      - if: inputs.version-suffix != ''\n", "      - ")
    assert _found({"caller": always}, {"caller"}) == {"3.14t"}


def test_found_reads_a_local_action_with_its_comments_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version an action only mentions in a comment is not one it pins.

    `_jobs` strips a workflow's comments before any of this reads them,
    so a version named in prose there has never counted. An action's
    own file is read separately and had escaped that, which would have
    let a sentence about an interpreter make the gate look as though it
    ran one -- the same false positive at a different scale from the one
    issue #750 is about. The comment below is a commented-out step, so
    it carries `--python 3.14t` in exactly the form `_NAMED` matches --
    which is what makes this a test rather than a decoration: prose
    mentioning a bare `3.14t` is invisible to `_NAMED` either way, and
    a read left unstripped would have passed against it.
    """
    (tmp_path / "action").mkdir()
    (tmp_path / "action" / "action.yml").write_text(
        "# the free-threaded rehearsal this replaced:\n"
        "#      run: uv run --no-project --python 3.14t true\n"
        "runs:\n  using: composite\n  steps:\n"
        "    - shell: bash\n      run: uv run --no-project --python 3.14 true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "_ROOT", tmp_path)
    caller = "      - name: A step\n        uses: ./action\n"
    assert _found({"caller": caller}, {"caller"}) == {"3.14"}


def test_found_refuses_an_interpreter_a_conditioned_step_names() -> None:
    """A name inside a conditioned step counts no more than a `uses:` does.

    The excision is one rule at one place, so the two arms of `_named`
    -- a `python-version:` and a `--python` -- are refused by it exactly
    as `_LOCAL_ACTION` is. Without that, a job whose pytest step runs
    unconditionally could still contribute an interpreter written into
    a step beside it that the gate never reaches.
    """
    job = (
        "      - name: Run the suite\n"
        "        run: >\n"
        "          uv run --locked --no-default-groups --group test pytest\n"
        "      - name: Rehearsal only\n"
        "        if: inputs.version-suffix != ''\n"
        "        run: uv run --no-project --python 3.14t true\n"
    )
    assert _found({"caller": job}, {"caller"}) == set()
    # the control: the same line with nothing gating it is read
    always = job.replace("        if: inputs.version-suffix != ''\n", "")
    assert _found({"caller": always}, {"caller"}) == {"3.14t"}


def test_found_refuses_a_local_action_a_conditioned_step_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `uses:` on a step carrying an `if:` is not evidence of anything.

    This is the real gate's own shape and the reason issue #757 closes
    on the test above rather than on the workflow: `dist` calls
    `./.github/actions/dev-version` from a step gated
    `if: inputs.version-suffix != ''`, which holds on a release
    rehearsal and on neither a pull request nor a push to `main`. Read
    unconditionally, the interpreter that action pins would count as
    one the merge gate runs, which is the claim issue #750 refuses one
    scope up.
    """
    (tmp_path / "action").mkdir()
    (tmp_path / "action" / "action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n"
        "    - shell: bash\n      run: uv run --no-project --python 3.14t true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "_ROOT", tmp_path)
    rehearsal = (
        "      - name: A step\n"
        "        if: inputs.version-suffix != ''\n"
        "        uses: ./action\n"
    )
    assert _found({"caller": rehearsal}, {"caller"}) == set()
    # the control: the same call with the gate off is read
    always = rehearsal.replace("        if: inputs", "        env: inputs")
    assert _found({"caller": always}, {"caller"}) == {"3.14t"}


def test_runs_the_suite_survives_a_comment_written_inside_the_step() -> None:
    """A comment at a step's own indent does not truncate the step.

    `_UNCOMMENTED` removes one whitespace character along with the
    comment it strips, so a comment on its own line at a step's indent
    leaves a line one space short of it. `_STEP`'s blank alternative is
    what absorbs that, and without it the step would end at the comment
    and the `if:` below would be invisible -- the gap issue #750 closes,
    reopened by a comment, with nothing red to say so. The shape is one
    this tree's own workflows write, so this is not hypothetical.
    """
    gated = (
        "      - name: Run the suite\n"
        "        # what this comment says is not the point\n"
        "        if: steps.sync.outcome == 'success'\n"
        "        run: >\n"
        "          uv run --locked --no-default-groups --group test pytest\n"
    )
    assert not _runs_the_suite(_UNCOMMENTED.sub("", gated))
    # the control: the same block with nothing to skip on answers the
    # other way, so the assertion above turns on the `if:` being seen.
    # It says nothing about the comment, both variants truncating alike
    # under a `_STEP` that cannot absorb the residue -- the assertion
    # above is what rules that out, by failing there
    ungated = gated.replace("        if:", "        env:")
    assert _runs_the_suite(_UNCOMMENTED.sub("", ungated))
