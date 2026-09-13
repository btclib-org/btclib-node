# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Suite-wide pytest hooks and node fixtures used across the tests.

The hooks keep the coverage floor from firing on a run that could not
have crossed it; the fixtures start and stop real `Node` instances,
on their own ports, for the functional and unit tests that need one.

Beside the floor is a guard on its reaching the run at all. coverage
looks for its configuration in the directory the process started in, so
a run started from `tests/` finds no `fail_under`, no `source` and no
`branch = true`. Section 8 of the organization standard leaves a tree to
point such a run at its configuration or to make it say it is ungated,
and this file is the second of the two: such a run is refused
(btclib-org/.github#443).
"""

import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pytest
from hypothesis import settings

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from tests import get_random_port

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


# The property layer's profiles, registered once here rather than
# repeated on every `@given`, which is the shape section 7 of
# btclib-org/.github's README names (btclib-org/btclib-node#742).
#
# `deadline=None` because a per-example time limit is a timing flake on
# whichever cell of the matrix is slowest -- and this suite runs one on
# `windows-latest`, where the same work is measurably slower than on the
# image the floor is set against (btclib-org/btclib-node#737's own
# durations). A deadline here would be pyproject.toml's `timeout`
# problem a second time, at a hundredth of the scale.
#
# 500 is section 7's own figure and it is affordable here, measured
# rather than assumed: `tests/property_test.py` draws 500 examples for
# each entry point the walk finds and the whole file runs in under two
# seconds, against a suite of about ninety. The deep profile is opt-in
# because the search that finds a latent defect is not one to run at
# every commit, and what it finds graduates into a vector test rather
# than staying in a search that may not repeat it.
settings.register_profile("default", deadline=None, max_examples=500)
settings.register_profile("thorough", deadline=None, max_examples=2_000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


def asks_for_everything(
    file_or_dir: list[str] | None, testpaths: list[str], rootpath: Path
) -> bool:
    """Whether the paths named on the command line take the suite in.

    The arguments are read off a `pytest.Config` by the caller rather
    than taken as one here: a predicate taking the config is reachable
    only through a stand-in for one, and a case built on a stand-in
    measures the stand-in as much as the predicate.

    No path at all is `testpaths`, which is the suite. A path above one
    of them -- `pytest tests/` -- collects it too, so what matters is
    containment and not equality: comparing the strings would call the
    whole suite a subset and quietly drop the floor from it.

    On the `--help` path `file_or_dir` is `None` and not `[]`, the
    parse having been abandoned rather than left unfinished: `--help`
    is bound to pytest's `HelpAction`, which raises `PrintHelp` to skip
    the rest of argument parsing, and `Config.parse` catches it and
    returns before the positional is consumed, so it still holds
    argparse's `None` default when `helpconfig` calls `_do_configure()`
    and `pytest_configure` fires. That is no path either, and folding
    it is what keeps `--help` from ending in a traceback whose last
    frame is this file.
    """
    given = [Path(path).resolve() for path in file_or_dir or []]
    if not given:
        return True
    # against the rootdir and not against where pytest was run from,
    # which is what `testpaths` means. `rootpath` is built with
    # `os.path.abspath`, which leaves a symlink in the path alone, while
    # `Path.resolve` above follows one -- so a rootdir reached through a
    # symlink needs resolving here too, or a tree under `/tmp` on macOS
    # would compare `/tmp/...` against `/private/tmp/...` and find no
    # containment anywhere.
    wanted = [(rootpath / p).resolve() for p in testpaths]
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

    A subset is what pytest was *asked* for, and section 8 of the
    organization standard is what names the set `selective` reads below.

    Answers whether it wrote the floor down, which is how it is tested:
    the run that measures the suite is the one run this never fires on.
    """
    option = config.option
    selective = bool(
        not asks_for_everything(
            option.file_or_dir, config.getini("testpaths"), config.rootpath
        )
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
    # pytest builds `known_args_namespace` by parsing the known
    # arguments into a copy of `config.option`, and pytest-cov holds on
    # to that copy: `config.option` is a different object, so setting
    # the floor there runs without error and changes nothing
    config.known_args_namespace.cov_fail_under = 0
    return True


class CoverageConfiguration(Protocol):
    """What this file reads of coverage's own configuration object.

    `config_file` is the file coverage took its settings from, and
    `None` where it took them from none: coverage sets it as it reads
    one, so the attribute is the run's own answer to whether the
    configuration reached it, rather than an inference from a value
    that reached it.
    """

    config_file: str | None


def coverage_configuration(config: pytest.Config) -> CoverageConfiguration | None:
    """Return the configuration coverage is measuring with, or `None`.

    `None` is the two ways there is nothing to ask about: `--no-cov`,
    where pytest-cov registers its plugin and returns from `__init__`
    with the controller left unbuilt, and a run whose plugin was never
    registered, where `getplugin` hands back `None` -- the same
    `getattr` default answers for both.
    """
    plugin = config.pluginmanager.getplugin("_cov")
    controller = getattr(plugin, "cov_controller", None)
    if controller is None:
        return None
    # annotated because the plugin manager hands back `Any`, and a
    # return of that is what mypy's strict mode refuses here
    measuring: CoverageConfiguration = controller.cov.config
    return measuring


def configuration_went_unread(
    cov_config: CoverageConfiguration | None,
    inipath: Path | None,
    asked: float | None,
    *,
    asked_for_help: bool,
    collect_only: bool,
) -> bool:
    """Whether a run held to the floor cannot see one.

    A guard and not a sentence in `CONTRIBUTING.md`. What it catches is
    a plausible spelling switching the floor off, and a reader told to
    start from the root is not the run that does not: the sentence
    leaves the same failure, with somebody having been told about it.

    What it compares is not the threshold. `pyproject.toml` is the one
    place the number lives, and a `== 100` here would be the second, so
    what decides is whether coverage read a file at all against whether
    pytest read one -- the asymmetry the defect leaves behind, pytest
    walking up from where it was invoked to find its configuration and
    coverage looking only where the process started.

    The arguments are read off a `pytest.Config` by the caller rather
    than taken as one, for the reason `asks_for_everything` above gives.
    """
    if cov_config is None:
        return False
    if asked is not None:
        # section 8 of the organization standard has the hook never
        # overruling an explicit `--cov-fail-under`, and a caller who
        # named the floor has not had one taken away in silence
        return False
    if asked_for_help or collect_only:
        # neither run is held to a floor to begin with: `--help`
        # exits before a session, and `collectonly` is the one
        # invocation shape pytest-cov itself exempts, its
        # `pytest_runtestloop` returning on `cov_fail_under is None or
        # self.options.collectonly` whatever its report prints. The
        # pair is an enumeration rather than every run pytest-cov
        # leaves ungated -- `--markers` and `--fixtures` exit before
        # that loop as well, and are refused knowingly. Widening it is
        # the rejected alternative, and `--setup-plan` is what says so:
        # pytest-cov gates one, so a run of it started from `tests/` is
        # held to a floor it cannot see and is refused rather than
        # exempted
        return False
    # `inipath` is what the message has to name, so a run pytest read no
    # configuration for is one this cannot tell anybody anything about
    return cov_config.config_file is None and inipath is not None


def pytest_configure(config: pytest.Config) -> None:
    """Refuse a run that cannot see its floor; relax one that cannot clear it.

    A run coverage's configuration never reached is refused rather than
    gated, `pytest.UsageError` being what pytest prints without a
    traceback and exits `4` for -- an exit of its own, so the code says
    the run measured nothing rather than that something in the tree
    failed.
    """
    if configuration_went_unread(
        coverage_configuration(config),
        config.inipath,
        config.option.cov_fail_under,
        asked_for_help=config.option.help,
        collect_only=config.option.collectonly,
    ):
        refusal = (
            "coverage read no configuration, so this run is held to no floor"
            " and measures a different set of files: coverage looks only in"
            f" the directory the run started in, {Path.cwd()}, and pytest"
            f" read {config.inipath}. Run from {config.rootpath};"
            " --cov-config restores the floor and not the file set, a"
            " relative omit pattern being resolved against the directory"
            " the run started in."
        )
        raise pytest.UsageError(refusal)
    relax_coverage_floor(config)


@contextmanager
def node_context(
    tmp_path: Path, *, allow_p2p: bool = True, allow_rpc: bool = True
) -> Iterator[Node]:
    """Start a regtest node with each enabled server on a random port.

    `allow_p2p` and `allow_rpc` toggle which of the two servers actually
    binds one; `rpc_node` below is this with `allow_p2p=False`, for a
    test that only ever talks to the node over RPC. `node.stop()` runs
    once the caller's `with` block exits, whichever way it exits.
    """
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
    """Give an RPC-only, started regtest node for the functional RPC tests."""
    with node_context(tmp_path, allow_p2p=False) as node:
        yield node


@contextmanager
def unstarted_node_context(
    tmp_path: Path, *, pruned: bool = False, prune_target_mib: int | None = None
) -> Iterator[Node]:
    """Build and drive a node directly, never `start()`ed; close it on exit.

    `run`'s own teardown -- `peer_db.close()`, `chainstate.close()`,
    `block_db.close()`, both managers' event loops and `logger.close()`
    -- only runs once a node's thread has reached the end of its loop,
    which a node built here and driven on the thread that built it
    never does, so each is closed explicitly here instead. The two
    loops and `logger.close()`'s own file close for the reason
    `tests/unit/init_test.py`'s `a_networked_node` is already the
    precedent for the loops: a dropped event loop or open file is a
    `ResourceWarning` raised against whichever test the collector is
    running when it reaches it, not against this one. The managers'
    own `stop()` is not called -- it waits on a thread that `start()`
    never began.

    The three stores close for a different reason. The store is
    RocksDB through `rocksdict` (btclib-org/btclib-node#641), and a
    dropped `Rdict` raises no `ResourceWarning` at all -- measured
    directly, where dropping this function's own event loop or open
    file does. What a live handle holds instead is `db.py`'s own
    directory `LOCK` ("The lock stays" section): a second `Rdict`
    opened on the same path while the first is still referenced fails
    outright with an IO error naming the lock, measured directly,
    never merely a warning. `regtest_node` hands out several nodes
    sharing one `tmp_path`, and the node holding a store's handle
    stays referenced by the test for as long as the test holds it --
    nothing here drops that reference on its own -- so a test that
    reopens the same store needs this `close()` to have actually run,
    not a collector that may never reach the handle in time.

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
            pruned=pruned,
            prune_target_mib=prune_target_mib,
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
def regtest_node(tmp_path: Path) -> Iterator[Callable[..., Node]]:
    """Give out header-synced regtest nodes, each closed at teardown.

    Every node it hands out shares `tmp_path`: a test that checks a
    chainstate or a header chain survives being closed and reopened
    calls it twice, closing the first itself in between. Each is closed
    once more here regardless of what the test already did to it --
    `unstarted_node_context`'s own closes are all safe to repeat -- since
    most callers build exactly one node and never close it themselves.
    `pruned` and `prune_target_mib` reach `Config` unchanged, `False`/
    `None` by default, matching every caller here before
    btclib-org/btclib-node#601 and btclib-org/btclib-node#705
    respectively.
    """
    with ExitStack() as stack:

        def make(*, pruned: bool = False, prune_target_mib: int | None = None) -> Node:
            node = stack.enter_context(
                unstarted_node_context(
                    tmp_path, pruned=pruned, prune_target_mib=prune_target_mib
                )
            )
            node.status = NodeStatus.HeaderSynced
            return node

        yield make
