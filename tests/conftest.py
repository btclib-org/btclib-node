# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from contextlib import contextmanager
from pathlib import Path

import pytest

from btclib_node import Node
from btclib_node.config import Config
from tests.helpers import get_random_port


def asks_for_everything(config):
    """Whether the paths named on the command line take the suite in.

    No path at all is `testpaths`, which is the suite. A path above one
    of them -- `pytest tests/` -- collects it too, so what matters is
    containment and not equality: comparing the strings would call the
    whole suite a subset and quietly drop the floor from it.
    """
    given = [Path(path).resolve() for path in config.option.file_or_dir]
    if not given:
        return True
    # against the rootdir and not against where pytest was run from,
    # which is what `testpaths` means. `rootpath` is already absolute,
    # so joining is all it takes; the paths above are the ones that need
    # resolving, `./tests` and the absolute path being one directory.
    wanted = [config.rootpath / p for p in config.getini("testpaths")]
    if not wanted:
        # `all` over nothing is true, and would make every path named
        # here the whole suite. Nothing names the suite, so a bare run
        # collects the rootdir and anything asked for is less than it.
        return False
    return all(
        any(target == path or path in target.parents for path in given)
        for target in wanted
    )


def relax_coverage_floor(config):
    """Hold the coverage floor to runs that could clear it.

    `fail_under` is a statement about the whole suite. A run of one file,
    one `-k` expression or one `-m` marker is not that run, and failing
    it there teaches people to reach for `--no-cov`, which is how a floor
    stops being read at all. An explicit `--cov-fail-under` still means
    what it says.

    Answers whether it lowered the floor, which is how it is tested: the
    run that measures the suite is the one run this never fires on.
    """
    option = config.option
    selective = bool(
        not asks_for_everything(config)
        or option.keyword
        or option.markexpr
        or option.deselect
        or option.ignore
        or option.ignore_glob
        # absent under `-p no:cacheprovider`, which is why it is asked
        # for rather than read
        or getattr(option, "lf", None)
    )
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
