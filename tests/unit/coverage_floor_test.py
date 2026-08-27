# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""When the coverage floor applies, and when it stands aside.

The run that measures the suite is the whole suite, which is the one
run the floor is never lowered on -- so the decision is a function, and
this is what asks it the questions the command line otherwise would.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from tests.conftest import asks_for_everything, pytest_configure, relax_coverage_floor

if TYPE_CHECKING:
    from collections.abc import Sequence

FLOOR = 100

TESTPATHS = ["tests/unit", "tests/functional"]

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
    plugin: bool = True,
    testpaths: list[str] | None = None,
    rootpath: Path | None = None,
) -> tuple[Any, SimpleNamespace]:
    """Build a `pytest.Config` stand-in and the `pytest-cov` options it exposes.

    Only the attributes `asks_for_everything` and `relax_coverage_floor`
    actually read are here, named after the command-line options they
    come from; the second element returned is the `SimpleNamespace` the
    fake `_cov` plugin holds, so a test can read `cov_fail_under` back
    off it after calling one of the two functions on the first element.
    """
    options = SimpleNamespace(cov_fail_under=FLOOR)
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
        ),
        # what testpaths is relative to; the working directory is the
        # same thing only when pytest is run from the rootdir
        rootpath=Path.cwd() if rootpath is None else rootpath,
        getini=lambda name: {
            "testpaths": TESTPATHS if testpaths is None else testpaths
        }[name],
        # answers to the name pytest-cov registers under, and to no
        # other: the string is what couples this to the plugin
        pluginmanager=SimpleNamespace(
            getplugin=lambda name: (
                SimpleNamespace(options=options) if plugin and name == "_cov" else None
            )
        ),
    ), options


@pytest.mark.parametrize("paths", WHOLE_SUITE, ids=lambda p: " ".join(p) or "(bare)")
def test_naming_the_whole_suite_holds_the_floor(paths: list[str]) -> None:
    """Every spelling in `WHOLE_SUITE` leaves the floor at 100."""
    # a directory above testpaths collects it too, so what decides this
    # is containment: read as strings, the everyday `pytest tests/`
    # would count as a subset and lose the gate
    config, options = a_config(file_or_dir=paths)
    assert asks_for_everything(config) is True
    assert relax_coverage_floor(config) is False
    assert options.cov_fail_under == FLOOR


def test_the_help_path_names_no_paths_at_all() -> None:
    """`file_or_dir=None`, `--help`'s case, still reads as the whole suite."""
    # Why --help reaches the hook with file_or_dir None is
    # asks_for_everything's docstring. What is here is the cover: the
    # guard it describes adds no branch for the floor to miss, so
    # deleting this test leaves the fix untested.
    config, options = a_config(file_or_dir=None)
    assert asks_for_everything(config) is True
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
    config, _ = a_config(file_or_dir=[str(elsewhere)], rootpath=elsewhere)
    assert asks_for_everything(config) is True
    config, _ = a_config(
        file_or_dir=[str(elsewhere / "tests/unit")], rootpath=elsewhere
    )
    assert asks_for_everything(config) is False


def test_a_symlinked_rootdir_still_reads_as_the_whole_suite(tmp_path: Path) -> None:
    """A `rootpath` reached through a symlink still contains the resolved suite.

    `rootpath` is built with `os.path.abspath`, which leaves a symlink in
    the path alone; the paths given on the command line are resolved, which
    follows one. Before `wanted` was resolved on the same terms, a rootdir
    reached through a symlink made the two sides incomparable, and a run
    naming the whole suite read as a subset of itself.
    """
    real = tmp_path / "real"
    (real / "tests").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    config, options = a_config(
        file_or_dir=[str(link / "tests")], rootpath=link, testpaths=["tests"]
    )
    assert asks_for_everything(config) is True
    assert relax_coverage_floor(config) is False
    assert options.cov_fail_under == FLOOR


def test_a_suite_that_names_no_paths_of_its_own() -> None:
    """With `testpaths` unset, one named file is narrower; the floor drops."""
    # with testpaths unset a bare run collects the rootdir, so anything
    # named is less than the suite. `all` over an empty sequence is
    # true, which would answer the opposite of that.
    config, options = a_config(file_or_dir=["tests/unit/mempool_test.py"], testpaths=[])
    assert asks_for_everything(config) is False
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


def test_there_is_nothing_to_lower_when_coverage_is_not_running() -> None:
    """With no `_cov` plugin, a narrowed run leaves the floor untouched."""
    config, options = a_config(file_or_dir=["tests/unit/mempool_test.py"], plugin=False)
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
