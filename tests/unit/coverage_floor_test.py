# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""When the coverage floor applies, and when it stands aside.

The run that measures the suite is the whole suite, which is the one
run the floor is never lowered on -- so the decision is a function, and
this is what asks it the questions the command line otherwise would.

The guards beside the floor are driven the same way. The configuration
guard has one exception: the run it refuses cannot be the run reporting
on it either, so the case it exists for is taken in a subprocess started
from `tests/`.
"""

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

import tests.conftest
from tests.conftest import (
    CoverageConfiguration,
    asks_for_everything,
    configuration_went_unread,
    coverage_configuration,
    pytest_configure,
    relax_coverage_floor,
    sources_out_of_reach,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

FLOOR = 100

TESTPATHS = ["tests/unit", "tests/functional"]

ROOT = Path(__file__).parents[2]

# what pytest reads its own configuration from here, which the guard
# compares against what coverage read and the message names
INIPATH = ROOT / "pyproject.toml"

# what `[tool.coverage.run]` source names, read rather than restated
SOURCE: list[str] = tomllib.loads(INIPATH.read_text(encoding="utf-8"))["tool"][
    "coverage"
]["run"]["source"]

# the entries of that list which are paths and no module name, so that
# coverage can only read them as directories: `.github/scripts`
DIRECTORIES = [entry for entry in SOURCE if "/" in entry]

# every way of asking for less than the suite that the function reads
NARROWINGS = [
    {"file_or_dir": ["tests/unit/mempool_test.py"]},
    # some of what testpaths names and not all of it, which is the
    # commonest partial run after a single file
    {"file_or_dir": ["tests/unit"]},
    {"keyword": "mempool"},
    {"markexpr": "order"},
    {"deselect": ["tests/unit/mempool_test.py::test_add_tx"]},
    {"ignore": ["tests/functional"]},
    {"ignore_glob": ["*functional*"]},
    {"lf": True},
]

# and the ways of naming the whole of it: no path at all, the paths
# themselves, a directory they live under, and the last two spelled the
# way a shell hands them over rather than the way testpaths holds them
WHOLE_SUITE = [[], TESTPATHS, ["tests"], ["."], ["./tests"], ["tests/"]]


def a_cov_config(
    config_file: str | None, source: list[str] | None = None
) -> CoverageConfiguration:
    """Build what the guards read of coverage's own configuration.

    What it holds is the file coverage took its settings from, `None`
    where it took them from none, and the `source` list it read there.
    """
    return cast(
        "CoverageConfiguration",
        SimpleNamespace(config_file=config_file, source=source),
    )


def a_controller(config_file: str | None, source: list[str] | None = None) -> object:
    """Build the controller pytest-cov leaves on its plugin.

    A stand-in reachable by the attribute path `coverage_configuration`
    walks, and nothing else of one.
    """
    return SimpleNamespace(
        cov=SimpleNamespace(config=a_cov_config(config_file, source))
    )


def a_config(
    *,
    file_or_dir: Sequence[str] | None = (),
    keyword: str = "",
    markexpr: str = "",
    deselect: list[str] | None = None,
    ignore: list[str] | None = None,
    ignore_glob: list[str] | None = None,
    lf: bool = False,
    cov_fail_under: float | None = None,
    testpaths: list[str] | None = None,
    asked_for_help: bool = False,
    collect_only: bool = False,
    measuring: bool = True,
    cov_config_file: str | None = str(INIPATH),
    source: list[str] | None = None,
    inipath: Path | None = INIPATH,
    rootpath: Path | None = None,
) -> tuple[Any, SimpleNamespace]:
    """Build a `pytest.Config` stand-in and the `known_args_namespace` on it.

    Only what the hook reads is here: the `option` attributes, named
    after the command-line options they come from; `rootpath` and
    `getini`, which it hands on to `asks_for_everything`; and the
    `inipath` and the plugin the guard reads, `measuring` being
    `--no-cov`'s own case, where pytest-cov leaves the controller
    unbuilt. The second element returned is the `SimpleNamespace`
    standing in for `known_args_namespace`, so a test can read
    `cov_fail_under` back off it after calling `relax_coverage_floor`
    on the first element.
    """
    known_args_namespace = SimpleNamespace(cov_fail_under=FLOOR)
    plugin = SimpleNamespace(
        cov_controller=a_controller(cov_config_file, source) if measuring else None
    )
    return SimpleNamespace(
        option=SimpleNamespace(
            # None is what the --help path leaves it, and is not the
            # same absence as the empty list a bare run gets
            file_or_dir=None if file_or_dir is None else list(file_or_dir),
            keyword=keyword,
            markexpr=markexpr,
            deselect=deselect,
            ignore=ignore,
            ignore_glob=ignore_glob,
            lf=lf,
            # argparse's own default, whether nothing asked for a floor
            # or the ask arrived on the command line or through
            # PYTEST_ADDOPTS -- the two are indistinguishable here
            cov_fail_under=cov_fail_under,
            help=asked_for_help,
            collectonly=collect_only,
        ),
        # what testpaths is relative to; the working directory is the
        # same thing only when pytest is run from the rootdir
        rootpath=Path.cwd() if rootpath is None else rootpath,
        getini=lambda name: {
            "testpaths": TESTPATHS if testpaths is None else testpaths
        }[name],
        # what pytest read its own configuration from, which the guard
        # holds against what coverage read
        inipath=inipath,
        pluginmanager=SimpleNamespace(getplugin=lambda _name: plugin),
        # pytest-cov reads this copy, which pytest builds by parsing
        # the known arguments into a copy of config.option, and never
        # config.option itself
        known_args_namespace=known_args_namespace,
    ), known_args_namespace


@pytest.mark.parametrize("paths", WHOLE_SUITE, ids=lambda p: " ".join(p) or "(bare)")
def test_naming_the_whole_suite_holds_the_floor(paths: list[str]) -> None:
    """Every spelling in `WHOLE_SUITE` leaves the floor at 100."""
    # a directory above testpaths collects it too, so what decides this
    # is containment: read as strings, the everyday `pytest tests/`
    # would count as a subset and lose the gate
    assert asks_for_everything(paths, TESTPATHS, Path.cwd()) is True
    config, options = a_config(file_or_dir=paths)
    assert relax_coverage_floor(config) is False
    assert options.cov_fail_under == FLOOR


def test_the_help_path_names_no_paths_at_all() -> None:
    """`file_or_dir=None`, `--help`'s case, still reads as the whole suite."""
    # Why --help reaches the hook with file_or_dir None is
    # asks_for_everything's docstring. What is here is the cover: the
    # guard it describes adds no branch for the floor to miss, so
    # deleting this test leaves the fix untested.
    assert asks_for_everything(None, TESTPATHS, Path.cwd()) is True
    config, options = a_config(file_or_dir=None)
    assert relax_coverage_floor(config) is False
    assert options.cov_fail_under == FLOOR


@pytest.mark.parametrize("narrowing", NARROWINGS, ids=lambda n: next(iter(n)))
def test_asking_for_less_than_the_suite_stands_the_floor_down(
    narrowing: dict[str, Any],
) -> None:
    """Every narrowing in `NARROWINGS` drops `cov_fail_under` to 0."""
    config, options = a_config(**narrowing)
    assert relax_coverage_floor(config) is True
    assert options.cov_fail_under == 0


def test_testpaths_are_read_against_the_rootdir_and_not_the_working_directory() -> None:
    """`testpaths` is joined onto a fictitious `rootpath`, not the real cwd.

    Naming that `rootpath` itself still reads as the whole suite, and
    naming one of its subdirectories does not -- proof that the
    containment check above is computed off `rootpath` and never off
    wherever this process actually runs.
    """
    # the paths a run names are the shell's and testpaths is the
    # configuration file's; reading the second against the working
    # directory answers about a tree that is not the one being tested
    elsewhere = Path("/a/rootdir/that/is/not/here").resolve()
    assert asks_for_everything([str(elsewhere)], TESTPATHS, elsewhere) is True
    subdirectory = [str(elsewhere / "tests/unit")]
    assert asks_for_everything(subdirectory, TESTPATHS, elsewhere) is False


def test_a_parent_directory_segment_names_the_whole_suite_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`tests/../tests`, and `../tests` from inside `tests/`, are the suite.

    `pathlib` keeps a parent-directory segment where it collapses `.`
    and a trailing separator, so each spelling here is an object that
    compares unequal to the directory it names until `given`'s call
    resolves it. `WHOLE_SUITE`'s relative spellings are collapsed as
    they are built and so ask nothing of that half: they hold the call
    against being dropped, not against being rewritten to make a path
    absolute without normalizing it.

    The two commands differ in where the shell stood. A positional is
    read against the working directory, which is what `Path.resolve()`
    joins a relative one onto, while `rootpath` is the rootdir pytest
    computes from the configuration file above the paths it was given --
    so a run started inside `tests/` moves the first and leaves the
    second where it was.
    """
    base = tmp_path.resolve()
    (base / "tests").mkdir()
    monkeypatch.chdir(base)
    assert asks_for_everything(["tests/../tests"], TESTPATHS, base) is True
    monkeypatch.chdir(base / "tests")
    assert asks_for_everything(["../tests"], TESTPATHS, base) is True


# the pragma sits on the `def` because an exclusion on a line that
# introduces a block takes the whole block: this case's body is reachable
# only where the platform makes a symbolic link, so a floor over a
# `source` naming `tests` asks about the runner rather than about the
# suite. An exclusion on the `except` reaches only the two lines that do
# not run wherever the link is made, and the platform the guard is for
# then meets a skip and a floor it cannot reach in the same run. What it
# costs is that dead code inside the case stops being flagged; the case's
# one assertion is its whole subject, so the trade is cheap and is still
# a trade.
def test_a_symlinked_rootdir_still_reads_as_the_whole_suite(  # pragma: no cover -- the body needs a symlink
    tmp_path: Path,
) -> None:
    """A `rootpath` reached through a symlink still contains the resolved suite.

    `rootpath` is built with `os.path.abspath`, which leaves a symlink in
    the path alone; the paths given on the command line are resolved, which
    follows one. `wanted` is resolved on the same terms, which is what holds
    the two sides comparable: without it a run naming the whole suite reads
    as a subset of itself.

    A machine that will not create a symlink skips the case rather than
    failing it: on Windows an account can lack the privilege it takes.
    What holds the two calls there is
    `test_a_testpaths_entry_is_the_directory_its_parent_segment_reaches`
    for the one on `wanted`, and
    `test_a_parent_directory_segment_names_the_whole_suite_too` for the
    one on `given`, which `WHOLE_SUITE`'s relative spellings hold only
    against being dropped.
    """
    real = tmp_path / "real"
    (real / "tests").mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as refused:
        pytest.skip(f"this platform will not create a symlink: {refused}")
    assert asks_for_everything([str(link / "tests")], ["tests"], link) is True


def test_a_testpaths_entry_is_the_directory_its_parent_segment_reaches(
    tmp_path: Path,
) -> None:
    """`tests/../src` is `src`, which a command line naming `tests` misses.

    `pathlib` keeps a parent-directory segment where it collapses `.`
    and a trailing separator, so an unresolved join carries `..` into a
    path whose parents include the directory that segment left: `tests`
    then reads as above `tests/../src`, and a run collecting nothing of
    `src` is handed the whole suite's ratchet. Resolving the join makes
    the entry the directory it reaches, which `tests` is not above.

    That is the `testpaths` side's second reason to resolve, and it asks
    for no symlink and no privilege, so it holds where
    `test_a_symlinked_rootdir_still_reads_as_the_whole_suite` can only
    skip. A `..` that re-enters the directory it left --
    `tests/../tests` -- cannot see it: the unresolved target then has
    more parents and the command line's path is one of them, so
    containment answers the same with the call and without it.
    """
    # both sides start from the same spelling, or the `..` is not the
    # only difference between them; `tmp_path` already arrives resolved,
    # `TempPathFactory.getbasetemp` resolving the basetemp on both its
    # branches, so this states that invariant rather than establishing it
    base = tmp_path.resolve()
    assert asks_for_everything([str(base / "tests")], ["tests/../src"], base) is False


def test_a_suite_that_names_no_paths_of_its_own() -> None:
    """With `testpaths` unset, one named file is narrower; the floor drops."""
    # with testpaths unset a bare run collects the rootdir, so anything
    # named is less than the suite. `all` over an empty sequence is
    # true, which would answer the opposite of that.
    assert asks_for_everything(["tests/unit/mempool_test.py"], [], Path.cwd()) is False
    config, options = a_config(file_or_dir=["tests/unit/mempool_test.py"], testpaths=[])
    assert relax_coverage_floor(config) is True
    assert options.cov_fail_under == 0


def test_a_floor_asked_for_explicitly_is_left_alone() -> None:
    """A narrowed run leaves an explicit `cov_fail_under` alone."""
    # `option.cov_fail_under` is argparse's parsed result, which reads
    # the same whether the flag arrived on the command line or through
    # PYTEST_ADDOPTS -- the two are not distinguishable past this point,
    # which is what fixes #180: a scan of `invocation_params.args` would
    # only see the first.
    config, options = a_config(
        file_or_dir=["tests/unit/mempool_test.py"], cov_fail_under=FLOOR
    )
    assert relax_coverage_floor(config) is False
    assert options.cov_fail_under == FLOOR


def test_a_run_without_the_cache_plugin_has_no_last_failed_to_read() -> None:
    """A run narrowed by nothing else still reads a missing `lf` safely."""
    # -p no:cacheprovider leaves `lf` off the namespace altogether, and
    # reading it as an attribute raises out of pytest_configure. Nothing
    # else about this run narrows it, which is what makes the chain of
    # `or`s reach the read: with a path named here the first operand is
    # already true and the attribute is never touched.
    config, options = a_config()
    del config.option.lf
    assert relax_coverage_floor(config) is False
    assert options.cov_fail_under == FLOOR


def test_the_hook_is_wired_to_the_decision() -> None:
    """`pytest_configure` calls `relax_coverage_floor` on the real config."""
    # the function is what is tested above; this is that pytest calls it
    config, options = a_config(keyword="mempool")
    pytest_configure(config)
    assert options.cov_fail_under == 0


def test_a_run_coverage_read_a_configuration_for_is_not_refused() -> None:
    """The guard is silent where the configuration reached the run."""
    # the gate itself is this case -- `uv run pytest` from the rootdir,
    # where coverage reads pyproject.toml -- so a guard firing here
    # would refuse the run it exists to protect
    assert not configuration_went_unread(
        a_cov_config(str(INIPATH)),
        INIPATH,
        None,
        asked_for_help=False,
        collect_only=False,
    )


def test_a_run_coverage_read_no_configuration_for_is_refused() -> None:
    """A run held to a floor it cannot see is refused.

    This is the defect the guard is for: coverage looks for its
    configuration in the directory the process started in, so from
    `tests/` it finds no `fail_under`, no `source` and no
    `branch = true`, which leaves the run measuring a different set of
    files against nothing (btclib-org/.github#443). pytest reads its own
    configuration all the same, and that asymmetry is what the guard
    keys on.
    """
    assert configuration_went_unread(
        a_cov_config(None), INIPATH, None, asked_for_help=False, collect_only=False
    )


def test_nothing_measuring_is_not_an_ungated_run() -> None:
    """`--no-cov` is left alone."""
    # section 10 of the organization's standard has a platform sentinel
    # pass it, and a run measuring no coverage has no configuration to
    # be missing -- os-macos.yml and os-ubuntu.yml are this tree's own
    assert not configuration_went_unread(
        None, INIPATH, None, asked_for_help=False, collect_only=False
    )


def test_an_explicit_threshold_is_not_overruled_by_the_guard() -> None:
    """`--cov-fail-under` outranks the guard as it does the floor.

    The standard has the hook never overruling a caller who named the
    threshold, and the guard is that same hook: what it exists to catch
    is a floor going off with nobody having asked, which a named one is
    not. Zero is a threshold somebody asked for, so it has to survive
    the `is not None` test rather than be read as falsy.
    """
    assert not configuration_went_unread(
        a_cov_config(None), INIPATH, 0, asked_for_help=False, collect_only=False
    )


@pytest.mark.parametrize(
    ("asked_for_help", "collect_only"),
    [(True, False), (False, True)],
    ids=["--help", "--collect-only"],
)
def test_a_run_no_floor_applies_to_is_not_refused(
    asked_for_help: bool,  # noqa: FBT001
    collect_only: bool,  # noqa: FBT001
) -> None:
    """The two runs pytest-cov never gates are left alone.

    `--help` exits before a session, and pytest-cov never fails a
    `--collect-only` run on the floor whatever its report prints, which
    is what `pyproject.toml`'s own `--strict-markers` comment sends a
    reader to run. Refusing either would answer a question about a
    floor neither is held to.
    """
    assert not configuration_went_unread(
        a_cov_config(None),
        INIPATH,
        None,
        asked_for_help=asked_for_help,
        collect_only=collect_only,
    )


def test_without_a_configuration_pytest_read_there_is_nothing_to_name() -> None:
    """The guard needs pytest's own answer, not only coverage's.

    What the message tells a reader is where the configuration pytest
    found is, so a run that found none leaves it with nothing to say;
    and the two tools finding none alike is no asymmetry to report.
    """
    assert not configuration_went_unread(
        a_cov_config(None), None, None, asked_for_help=False, collect_only=False
    )


def test_no_pytest_cov_plugin_is_nothing_measuring() -> None:
    """A run without the plugin registered reads as unmeasured."""
    # pytest-cov registers its plugin only where a `--cov` reached the
    # parser, from addopts here rather than from a command line, and
    # `getplugin` hands back `None` where none did
    config = SimpleNamespace(
        pluginmanager=SimpleNamespace(getplugin=lambda _name: None)
    )
    assert coverage_configuration(cast("pytest.Config", config)) is None


def test_no_cov_leaves_the_controller_unbuilt() -> None:
    """The plugin without a controller reads as unmeasured too."""
    # `--no-cov` returns from `CovPlugin.__init__` before `start()`, so
    # the plugin is registered and its `cov_controller` is still None:
    # the same `getattr` default answers for that and for no plugin
    config, _ = a_config(measuring=False)
    assert coverage_configuration(config) is None


def test_the_configuration_is_the_controllers_own() -> None:
    """The attribute path to coverage's configuration is pinned.

    The hook is keyed on a path through pytest-cov it does not own: the
    plugin under `_cov`, its `cov_controller`, that controller's `cov`
    and the `config` on it. Renaming either of the first two reads as
    nothing measuring and leaves the guard silent, which is the
    direction that fails without saying so; renaming what is below them
    raises instead.
    """
    config, _ = a_config(cov_config_file="/somewhere/setup.cfg")
    measuring = coverage_configuration(config)

    assert measuring is not None
    assert measuring.config_file == "/somewhere/setup.cfg"


def test_the_guards_own_names_are_ones_pytest_fills_in(
    pytestconfig: pytest.Config,
) -> None:
    """`help` and `collectonly` are still pytest's own spellings.

    The hook reads them as attributes rather than with a default, both
    being pytest's own rather than a plugin's, so a rename is an
    `AttributeError` in `pytest_configure` and not a silent refusal.
    This run's own configuration is what says they are still there.
    """
    absent = [
        name
        for name in ("help", "collectonly")
        if not hasattr(pytestconfig.option, name)
    ]
    assert not absent, f"pytest no longer fills in {absent}"


def test_the_hook_refuses_a_run_that_cannot_see_its_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`pytest_configure` raises, and each path is in its own clause.

    The function above decides; this is what wires it to a run.
    `pytest.UsageError` is what pytest prints without a traceback and
    exits `4` for, so the exit code says the run measured nothing rather
    than that something in the tree failed. The three paths are asserted
    inside the clause each belongs to, and are three different
    directories here so that no two of them can answer for one another:
    a message naming the directory the run started in where it means
    the root sends a reader back to the run that is refused.
    """
    monkeypatch.chdir(tmp_path)
    inipath = Path("/a/configuration/that/is/not/here/pyproject.toml")
    config, _ = a_config(cov_config_file=None, inipath=inipath)

    with pytest.raises(pytest.UsageError) as raised:
        pytest_configure(config)

    message = str(raised.value)
    assert f"only in the directory the run started in, {tmp_path}, and" in message
    assert f"pytest read {inipath}." in message
    # no --cov-config is offered: in a checkout, from anywhere but the
    # tree, it leaves `.github/scripts` unmeasured, which
    # `sources_out_of_reach` refuses
    assert message.endswith(f"Run from {ROOT}.")


def test_the_refusal_names_the_tree_whatever_the_rootdir(tmp_path: Path) -> None:
    """`-c` moves the rootdir, and the message still names this tree.

    `-c other.ini` makes the rootdir the directory holding `other.ini`,
    which is no place to run this suite from. The tree holding
    `tests/conftest.py` is, and no flag moves it.
    """
    config, _ = a_config(cov_config_file=None, rootpath=tmp_path)

    with pytest.raises(pytest.UsageError) as raised:
        pytest_configure(config)

    assert str(raised.value).endswith(f"Run from {ROOT}.")


def test_a_selection_does_not_excuse_the_configuration_missing() -> None:
    """Asking for less is refused the same way.

    A selective run is gated at zero by `relax_coverage_floor`, so
    nothing was taken from it -- but `source` and `branch = true` went
    unread as well, and its report is a measurement of a different set
    of files. Iterating on one module from inside `tests/` reads a
    percentage that is not about this tree, which is what the guard says
    instead. The decision above cannot see a selection at all; the hook
    is where one arrives, so this is where that is asserted.
    """
    config, options = a_config(
        file_or_dir=["tests/unit/mempool_test.py"],
        keyword="mempool",
        cov_config_file=None,
    )

    with pytest.raises(pytest.UsageError, match="coverage read no configuration"):
        pytest_configure(config)

    # the raise is ahead of the write, so the copy pytest-cov reads is
    # left holding the floor this selective run would have been let off
    assert options.cov_fail_under == FLOOR


def test_a_run_started_from_tests_says_it_is_ungated(tmp_path: Path) -> None:
    """The guard stops a real run started from `tests/`.

    Everything above is the decision driven as a function; this is the
    invocation the issue is about, and the only case that says the two
    are wired together -- that `tests/conftest.py` is loaded at all on
    such a run, and that what it raises reaches whoever typed it. The
    run costs no collection: `pytest_configure` is ahead of it, so the
    subprocess is refused before it imports a test module.

    `COVERAGE_FILE` is redirected because pytest-cov erases the data
    file it is pointed at as it starts, absent `--cov-append`, which
    would otherwise destroy the data file of the run reading this;
    `HYPOTHESIS_STORAGE_DIRECTORY` because hypothesis takes `Path.cwd()`
    for its example database, so a child started in `tests/` is what
    `[tool.uv.build-backend]`'s `source-exclude` already names.
    """
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    environment["COVERAGE_FILE"] = str(tmp_path / "coverage-data")
    environment["HYPOTHESIS_STORAGE_DIRECTORY"] = str(tmp_path / "hypothesis")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"],
        cwd=ROOT / "tests",
        env=environment,
        capture_output=True,
        encoding="utf-8",
        check=False,
        # a child that hangs fails as this test rather than holding an
        # xdist worker until the job's timeout-minutes, which the report
        # would not name
        timeout=120,
    )

    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, completed.stderr
    # pytest writes a usage error to stderr, where nothing of the run's
    # own output is, so the assertion is on the stream that carries it
    assert "coverage read no configuration" in completed.stderr
    assert str(ROOT) in completed.stderr


def _laid_out(root: Path) -> Path:
    """Lay out `.github/scripts` and `tests/` under `root`, and return it.

    A tree of its own rather than this one: the sdist runs this suite and
    ships no `.github`.
    """
    for directory in DIRECTORIES:
        (root / directory).mkdir(parents=True)
    (root / "tests").mkdir()
    return root


def test_every_source_directory_is_in_reach_from_the_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """From the tree, where the gate runs, the guard is silent."""
    root = _laid_out(tmp_path)
    monkeypatch.chdir(root)
    measuring = a_cov_config(str(root / "pyproject.toml"), SOURCE)
    assert (
        sources_out_of_reach(
            measuring, root, None, asked_for_help=False, collect_only=False
        )
        == []
    )


@pytest.mark.parametrize(
    ("config_dir", "rootdir"),
    [
        ("tree", "tree"),
        ("tree", "tree/tests"),
        ("elsewhere", "tree"),
        ("elsewhere", "tree/tests"),
        ("elsewhere", "elsewhere"),
    ],
    ids=[
        "--cov-config=../pyproject.toml",
        "--rootdir=.",
        "a copy outside the tree",
        "a copy outside the tree, --rootdir=.",
        "a copy outside the tree, -c other.ini beside it",
    ],
)
def test_a_run_from_tests_leaves_the_scripts_out_of_reach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_dir: str, rootdir: str
) -> None:
    """From `tests/`, `.github/scripts` is neither a directory nor a module.

    `tests` and the package are not named: from `tests/` coverage reads
    them as module names, and measures them as such.

    Each case moves the file coverage reads, the rootdir, or both, and
    none of them moves the tree holding `tests/conftest.py`, which is
    where the entry is looked for and where the run is sent. The tree is
    stood in for by one laid out here, as `TREE` is what the hook reads.
    """
    root = _laid_out(tmp_path / "tree")
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setattr(tests.conftest, "TREE", root)
    monkeypatch.chdir(root / "tests")
    config, known = a_config(
        cov_config_file=str(tmp_path / config_dir / "pyproject.toml"),
        source=SOURCE,
        rootpath=tmp_path / rootdir,
    )

    with pytest.raises(pytest.UsageError) as raised:
        pytest_configure(config)

    assert DIRECTORIES, "source names no directory for this case to drop"
    unmeasured = ", ".join(DIRECTORIES)
    assert str(raised.value).startswith(f"coverage would not measure {unmeasured}:")
    assert f"started in, {root / 'tests'}," in str(raised.value)
    assert str(raised.value).endswith(f"Run from {root}.")
    assert known.cov_fail_under == FLOOR


def test_a_tree_without_the_directory_is_not_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An sdist ships no `.github`, and its run from `tests/` stands.

    There the entry is no directory under the tree either, so it has
    nothing to measure and nothing is lost.
    """
    (tmp_path / "tests").mkdir()
    monkeypatch.chdir(tmp_path / "tests")
    measuring = a_cov_config(str(tmp_path / "pyproject.toml"), SOURCE)
    assert (
        sources_out_of_reach(
            measuring, tmp_path, None, asked_for_help=False, collect_only=False
        )
        == []
    )


@pytest.mark.parametrize("with_tests", [False, True], ids=["scripts", "both"])
def test_another_checkout_s_directories_are_not_the_tree_s(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    with_tests: bool,  # noqa: FBT001
) -> None:
    """From another checkout, coverage would measure that checkout's.

    There each entry is a directory, so coverage measures it, and the
    floor passes without the tree's own. Each entry the other checkout
    holds is refused.
    """
    root = _laid_out(tmp_path / "tree")
    elsewhere = tmp_path / "elsewhere"
    for directory in DIRECTORIES:
        (elsewhere / directory).mkdir(parents=True)
    if with_tests:
        (elsewhere / "tests").mkdir()
    monkeypatch.chdir(elsewhere)
    measuring = a_cov_config(str(root / "pyproject.toml"), SOURCE)
    expected = ["tests", *DIRECTORIES] if with_tests else DIRECTORIES
    unmeasured = sources_out_of_reach(
        measuring, root, None, asked_for_help=False, collect_only=False
    )
    assert sorted(unmeasured) == sorted(expected)


def test_the_tree_spelled_another_way_is_still_the_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The directories are compared, not the paths that name them."""
    root = _laid_out(tmp_path)
    monkeypatch.chdir(root)
    measuring = a_cov_config(str(root / "pyproject.toml"), SOURCE)
    spelled = root / "tests" / ".."
    assert (
        sources_out_of_reach(
            measuring, spelled, None, asked_for_help=False, collect_only=False
        )
        == []
    )


@pytest.mark.parametrize(
    ("asked", "asked_for_help", "collect_only"),
    [(0, False, False), (None, True, False), (None, False, True)],
    ids=["--cov-fail-under", "--help", "--collect-only"],
)
def test_the_source_guard_leaves_alone_what_the_other_one_does(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    asked: float | None,
    asked_for_help: bool,  # noqa: FBT001
    collect_only: bool,  # noqa: FBT001
) -> None:
    """The runs `held_to_a_floor` excuses are excused here too."""
    root = _laid_out(tmp_path)
    monkeypatch.chdir(root / "tests")
    measuring = a_cov_config(str(root / "pyproject.toml"), SOURCE)
    excused = sources_out_of_reach(
        measuring,
        root,
        asked,
        asked_for_help=asked_for_help,
        collect_only=collect_only,
    )
    assert excused == []
    assert (
        sources_out_of_reach(None, root, None, asked_for_help=False, collect_only=False)
        == []
    )


def test_the_tree_is_the_one_holding_the_conftest() -> None:
    """`TREE` is this tree, the one the cases above stand in for."""
    assert tests.conftest.TREE == ROOT


def test_the_source_guard_reads_coverage_s_own_list(
    pytestconfig: pytest.Config,
) -> None:
    """`source` is where coverage keeps what `pyproject.toml` names.

    The cases of `sources_out_of_reach` above hand it a stand-in; this
    one reads the attribute off coverage's own configuration for this
    run. A run measuring nothing, `--no-cov`, has no configuration to
    read.
    """
    measuring = coverage_configuration(pytestconfig)
    assert measuring is None or measuring.source == SOURCE
