# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from contextlib import contextmanager

import pytest

from btclib_node import Node
from btclib_node.config import Config
from tests.helpers import get_random_port


def relax_coverage_floor(config):
    """Hold the coverage floor to runs that could clear it.

    `fail_under` is a statement about the whole suite. A run of one file
    or one `-k` expression is not that run, and failing it there teaches
    people to reach for `--no-cov`, which is how a floor stops being
    read at all. An explicit `--cov-fail-under` still means what it
    says.

    Answers whether it lowered the floor, which is how it is tested: the
    run that measures the suite is the one run this never fires on.
    """
    selective = bool(config.option.file_or_dir or config.option.keyword)
    asked_for = any(
        arg.startswith("--cov-fail-under") for arg in config.invocation_params.args
    )
    if not selective or asked_for:
        return False
    # pytest-cov keeps its own namespace, built from the arguments before
    # the configuration file is read; config.option is a different object
    # and setting the floor there changes nothing
    plugin = config.pluginmanager.getplugin("_cov")
    if plugin is None:
        return False
    plugin.options.cov_fail_under = 0
    return True


def pytest_configure(config):
    relax_coverage_floor(config)


@contextmanager
def node_context(tmp_path, allow_p2p: bool = True, allow_rpc: bool = True):
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=allow_p2p,
            p2p_port=get_random_port() if allow_p2p else None,
            allow_rpc=allow_rpc,
            rpc_port=get_random_port() if allow_rpc else None,
            debug=True,
        )
    )
    node.start()
    try:
        yield node
    finally:
        node.stop()


@pytest.fixture
def rpc_node(tmp_path):
    with node_context(tmp_path, allow_p2p=False) as node:
        yield node
