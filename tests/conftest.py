# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from tests.helpers import get_random_port

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def asks_for_everything(config: pytest.Config) -> bool:
    """Whether the paths named on the command line take the suite in.

    No path at all is `testpaths`, which is the suite. A path above one
    of them -- `pytest tests/` -- collects it too, so what matters is
    containment and not equality: comparing the strings would call the
    whole suite a subset and quietly drop the floor from it.

    On the `--help` path `config.option.file_or_dir` is `None` and not
    `[]`, the parse having been abandoned rather than left unfinished:
    `--help` is bound to pytest's `HelpAction`, which raises
    `PrintHelp` to skip the rest of argument parsing, and
    `Config.parse` catches it and returns before the positional is
    consumed, so it still holds argparse's `None` default when
    `helpconfig` calls `_do_configure()` and `pytest_configure` fires.
    That is no path either, and folding it is what keeps `--help` from
    ending in a traceback whose last frame is this file.
    """
    given = [Path(path).resolve() for path in config.option.file_or_dir or []]
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


def relax_coverage_floor(config: pytest.Config) -> bool:
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
    # `invocation_params.args` is only what was handed to `pytest.main`;
    # pytest splices `PYTEST_ADDOPTS` in afterwards, so a floor asked for
    # that way is invisible there. `option.cov_fail_under` is the parsed
    # result and carries the flag regardless of which of the two wrote
    # it -- argparse does not remember where an argument came from.
    asked_for = option.cov_fail_under is not None
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


def pytest_configure(config: pytest.Config) -> None:
    relax_coverage_floor(config)


@contextmanager
def node_context(
    tmp_path: Path, *, allow_p2p: bool = True, allow_rpc: bool = True
) -> Iterator[Node]:
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
def rpc_node(tmp_path: Path) -> Iterator[Node]:
    with node_context(tmp_path, allow_p2p=False) as node:
        yield node


@contextmanager
def unstarted_node_context(tmp_path: Path) -> Iterator[Node]:
    """A node built and driven directly, never `start()`ed, closed on exit.

    `run`'s own teardown -- `peer_db.close()`, `chainstate.close()`,
    `block_db.close()`, both managers' event loops and `logger.close()`
    -- only runs once a node's thread has reached the end of its loop,
    which a node built here and driven on the thread that built it
    never does. Each is closed explicitly rather than dropped: a
    dropped database or file is a `ResourceWarning` raised against
    whichever test the collector is running when it reaches it, not
    against this one, and `tests/unit/init_test.py`'s `a_networked_node`
    is the precedent for the two loops. The managers' own `stop()` is
    not called -- it waits on a thread that `start()` never began.

    The worker pool is taken down here too, rather than left to
    `Node.__del__`'s own backstop: that backstop only runs once the
    collector reaches this node, and where the pool it built is also
    unreachable by then, `gc.collect()` does not promise which of the
    two finalizers -- the node's or the pool's own -- runs first, so
    relying on it is a `ResourceWarning` that fires on some collection
    passes and not others.
    """
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            debug=True,
        )
    )
    try:
        yield node
    finally:
        node._close_worker_pool()
        node.p2p_manager.peer_db.close()
        node.chainstate.close()
        node.block_db.close()
        node.p2p_manager.loop.close()
        node.rpc_manager.loop.close()
        node.logger.close()


@pytest.fixture
def regtest_node(tmp_path: Path) -> Iterator[Callable[[], Node]]:
    """A factory for header-synced regtest nodes, closed at teardown.

    Every node it hands out shares `tmp_path`: a test that checks a
    chainstate or a header chain survives being closed and reopened
    calls it twice, closing the first itself in between. Each is closed
    once more here regardless of what the test already did to it --
    `unstarted_node_context`'s own closes are all safe to repeat -- since
    most callers build exactly one node and never close it themselves.
    """
    with ExitStack() as stack:

        def make() -> Node:
            node = stack.enter_context(unstarted_node_context(tmp_path))
            node.status = NodeStatus.HeaderSynced
            return node

        yield make
