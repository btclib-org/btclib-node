# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""When the coverage floor applies, and when it stands aside.

The run that measures the suite is the whole suite, which is the one
run the floor is never lowered on -- so the decision is a function, and
this is what asks it the questions the command line otherwise would.
"""

from types import SimpleNamespace

import pytest

from tests.conftest import pytest_configure, relax_coverage_floor

FLOOR = 100

TESTPATHS = ["tests/unit", "tests/functional"]

# every way of asking for less than the suite that the function reads
NARROWINGS = [
    {"file_or_dir": ["tests/unit/mempool.py"]},
    {"keyword": "mempool"},
    {"markexpr": "order"},
    {"deselect": ["tests/unit/mempool.py::test_add_tx"]},
    {"ignore": ["tests/functional"]},
    {"lf": True},
]


def a_config(
    *,
    file_or_dir=(),
    keyword="",
    markexpr="",
    deselect=None,
    ignore=None,
    lf=False,
    args=(),
    plugin=True,
):
    options = SimpleNamespace(cov_fail_under=FLOOR)
    return SimpleNamespace(
        option=SimpleNamespace(
            file_or_dir=list(file_or_dir),
            keyword=keyword,
            markexpr=markexpr,
            deselect=deselect,
            ignore=ignore,
            lf=lf,
        ),
        getini=lambda name: {"testpaths": TESTPATHS}[name],
        invocation_params=SimpleNamespace(args=tuple(args)),
        # answers to the name pytest-cov registers under, and to no
        # other: the string is what couples this to the plugin
        pluginmanager=SimpleNamespace(
            getplugin=lambda name: (
                SimpleNamespace(options=options) if plugin and name == "_cov" else None
            )
        ),
    ), options


def test_the_whole_suite_is_held_to_the_floor():
    config, options = a_config()
    assert relax_coverage_floor(config) is False
    assert options.cov_fail_under == FLOOR


@pytest.mark.parametrize("paths", [TESTPATHS, ["tests"], ["."]])
def test_a_path_the_suite_lives_under_is_still_the_suite(paths):
    # `pytest tests/` collects everything testpaths names, so the floor
    # holds: reading the paths as strings would call it a subset and let
    # the everyday way of running everything past the gate
    config, options = a_config(file_or_dir=paths)
    assert relax_coverage_floor(config) is False
    assert options.cov_fail_under == FLOOR


@pytest.mark.parametrize("narrowing", NARROWINGS, ids=lambda n: next(iter(n)))
def test_asking_for_less_than_the_suite_stands_the_floor_down(narrowing):
    config, options = a_config(**narrowing)
    assert relax_coverage_floor(config) is True
    assert options.cov_fail_under == 0


def test_a_floor_asked_for_on_the_command_line_is_left_alone():
    config, options = a_config(
        file_or_dir=["tests/unit/mempool.py"], args=("--cov-fail-under=100",)
    )
    assert relax_coverage_floor(config) is False
    assert options.cov_fail_under == FLOOR


def test_there_is_nothing_to_lower_when_coverage_is_not_running():
    config, options = a_config(file_or_dir=["tests/unit/mempool.py"], plugin=False)
    assert relax_coverage_floor(config) is False
    assert options.cov_fail_under == FLOOR


def test_a_run_without_the_cache_plugin_has_no_last_failed_to_read():
    # -p no:cacheprovider leaves `lf` off the namespace altogether, and
    # reading it as an attribute would raise out of pytest_configure
    config, options = a_config(file_or_dir=["tests/unit/mempool.py"])
    del config.option.lf
    assert relax_coverage_floor(config) is True
    assert options.cov_fail_under == 0


def test_the_hook_is_wired_to_the_decision():
    # the function is what is tested above; this is that pytest calls it
    config, options = a_config(keyword="mempool")
    pytest_configure(config)
    assert options.cov_fail_under == 0
