# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""When the coverage floor applies, and when it stands aside.

The run that measures the suite is the whole suite, which is the one
run the floor is never lowered on -- so the decision is a function, and
this is what asks it the questions the command line otherwise would.
"""

from types import SimpleNamespace

from tests.conftest import relax_coverage_floor

FLOOR = 100


def a_config(*, file_or_dir=(), keyword="", args=(), plugin=True):
    options = SimpleNamespace(cov_fail_under=FLOOR)
    return SimpleNamespace(
        option=SimpleNamespace(file_or_dir=list(file_or_dir), keyword=keyword),
        invocation_params=SimpleNamespace(args=tuple(args)),
        pluginmanager=SimpleNamespace(
            getplugin=lambda name: SimpleNamespace(options=options) if plugin else None
        ),
    ), options


def test_the_whole_suite_is_held_to_the_floor():
    config, options = a_config()
    assert relax_coverage_floor(config) is False
    assert options.cov_fail_under == FLOOR


def test_one_file_is_not_the_suite():
    config, options = a_config(file_or_dir=["tests/unit/mempool.py"])
    assert relax_coverage_floor(config) is True
    assert options.cov_fail_under == 0


def test_neither_is_one_expression():
    config, options = a_config(keyword="mempool")
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
