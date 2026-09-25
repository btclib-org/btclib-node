# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Suite-wide pytest hooks and node fixtures used across the tests.

The hooks keep the coverage floor from firing on a run that could not
have crossed it, and name which test started a thread that later raises
off it; the fixtures start and stop real `Node` instances, on their own
ports, for the functional and unit tests that need one.

Beside the floor is a guard on its reaching the run at all. coverage
looks for its configuration in the directory the process started in, so
a run started from `tests/` finds no `fail_under`, no `source` and no
`branch = true`. Section 8 of the organization standard leaves a tree to
point such a run at its configuration or to make it say it is ungated,
and this file is the second of the two: such a run is refused
(btclib-org/.github#443).

A thread that outlives the test that started it is the other guard here.
`_pytest.threadexception` drains `threading.excepthook` at the boundary
of each test's own setup, call and teardown, so an exception raised on a
thread nobody joined lands on whichever test is at one of those
boundaries when the drain runs -- not on the test that started the
thread. `thread_exception_origin_note` and the hook installed below by
`pytest_configure` do not stop that from happening; they say, in the
report pytest already prints, which test actually started the thread,
so a reader is not sent to look for a defect in the one the exception
merely surfaced against (btclib-org/btclib-node#1002).

A task left pending on an event loop is the third: found as its loop
closes or as the collector frees it, it fails the test running then, at
that test's own teardown, and the whole run where no test is left to
fail (btclib-org/btclib-node#1107).
"""

import asyncio
import gc
import os
import threading
import weakref
from collections import deque

# not under TYPE_CHECKING (TC003's own suggestion): pluggy inspects a
# hookimpl's signature with annotations forced to evaluate, ahead of
# ever running it, and `pytest_runtest_protocol` below is one -- a name
# only `TYPE_CHECKING` had put in scope raised `NameError` there, at
# collection, before any test ran.
from collections.abc import Iterator  # noqa: TC003
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import pytest
from hypothesis import settings

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from tests import get_random_port

if TYPE_CHECKING:
    from collections.abc import Callable


# Which test's own execution a thread was started during. Keyed on the
# thread itself, weakly: a thread this suite joins and drops is not kept
# alive by this dict having once recorded it. Read by
# `thread_exception_origin_note` below, written by
# `_record_thread_origin`.
_THREAD_ORIGIN: weakref.WeakKeyDictionary[threading.Thread, str] = (
    weakref.WeakKeyDictionary()
)

# The nodeid of whichever test is inside its own setup, call or teardown
# right now -- a one-element list rather than a bare module global so
# `_record_thread_origin` closes over the box and sees every later
# write, not the value the box held when it was defined. The sentinel
# names a thread started before any test's own protocol began (an
# import, collection) as what it is, rather than as some particular
# test's.
_CURRENT_TEST_NODEID: list[str] = ["<no test running yet>"]

_real_thread_start = threading.Thread.start


def _record_thread_origin(
    self: threading.Thread, *args: object, **kwargs: object
) -> None:
    """Start `self` as `threading.Thread.start` always has, and note the caller.

    Patched onto the class itself rather than called at each of this
    suite's own thread-building call sites: `Node` and `P2pManager` are
    both `threading.Thread` subclasses, `warm_worker_pool` builds a
    `threading.Thread` of its own, and a handful of unit tests build a
    bare one directly (`tests/unit/db_test.py` among them) -- every one
    of those reaches `start` and none of them would otherwise reach a
    single recording point.
    """
    _THREAD_ORIGIN[self] = _CURRENT_TEST_NODEID[0]
    _real_thread_start(self, *args, **kwargs)


threading.Thread.start = _record_thread_origin  # type: ignore[method-assign]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Iterator[None]:
    """Point `_CURRENT_TEST_NODEID` at `item.nodeid` before anything else runs.

    Not an autouse fixture, which this carried until a review round of
    btclib-org/btclib-node#1002 found the gap: a fixture's own scope
    decides when pytest builds it relative to the other fixtures the
    same test needs, and a module-, class- or session-scoped fixture a
    test depends on is always built ahead of a function-scoped one,
    autouse or not -- so a thread a higher-scoped fixture starts, on the
    first test of its own scope, starts before that function-scoped
    fixture has run at all, and got recorded against whichever earlier,
    unrelated test's own copy of it had run last. Measured directly
    against a module fixture starting a thread on the first test of a
    second module: the origin came back naming the previous module's
    last test.

    A hookwrapper on `pytest_runtest_protocol` does not have this gap:
    its own code ahead of `yield` runs before every non-wrapper
    implementation of the same hook, which is where `_pytest.runner`
    does the actual fixture setup of every scope, not only the
    function-scoped one -- so this box is current before the first
    fixture this test needs, of any scope, is even built.
    """
    _CURRENT_TEST_NODEID[0] = item.nodeid
    yield


# Every task found left pending on an event loop since a test's teardown
# last drained this: its `repr`, after the nodeid `_CURRENT_TEST_NODEID`
# held when it was found. Written on whichever thread closes a loop or
# runs the collector -- a node's own, for both managers' loops -- and
# drained on the test's. A deque and no lock: a collection can run the
# recorder on the draining thread itself, in the middle of the drain,
# where a lock it already held would deadlock it, and `append` and
# `popleft` are atomic without one.
_PENDING_TASKS: deque[str] = deque()

# What `Task.__del__` hands the loop's exception handler, as the
# `message` of its context, for a task freed while still pending.
_DESTROYED_PENDING = "Task was destroyed but it is pending!"

# The key a worker's `pytest_sessionfinish` hands its remainder to the
# xdist controller under, through `config.workeroutput`.
_WORKEROUTPUT_KEY = "btclib_node_pending_tasks"

_real_loop_close = asyncio.BaseEventLoop.close
_real_call_exception_handler = asyncio.BaseEventLoop.call_exception_handler
# bound here rather than looked up at each close, so that a test
# replacing `asyncio.all_tasks` -- tests/unit/rpc/manager_test.py does,
# to hand `stop` tasks of its own -- does not replace what this reads
_real_all_tasks = asyncio.all_tasks


def _record_pending_at_close(self: asyncio.BaseEventLoop) -> None:
    """Close `self` as `BaseEventLoop.close` does, noting what it left pending.

    asyncio raises nothing when a loop closes with a task pending on it,
    and no warning either, so `filterwarnings`' `"error"` cannot see it.
    A task something still references is found here, at the close.

    Patched onto `BaseEventLoop` itself, which the selector and proactor
    loops' own `close` reach through `super().close()`, and only for a
    loop neither running nor already closed: each returns or raises
    ahead of that call otherwise. The tasks are read after the real
    close, `asyncio.all_tasks` still answering for a closed loop. Each
    one found is told not to report its own destruction, which
    `_record_destroyed_pending` would otherwise record a second time.
    """
    _real_loop_close(self)
    during = _CURRENT_TEST_NODEID[0]
    for task in _real_all_tasks(self):
        _PENDING_TASKS.append(f"{during}: {task!r}, pending when its loop closed")
        task._log_destroy_pending = False  # type: ignore[attr-defined]


def _record_destroyed_pending(
    self: asyncio.BaseEventLoop, context: dict[str, Any]
) -> None:
    """Hand `context` to the loop's handler, noting a task freed while pending.

    A task nothing references but a cycle of its own -- its coroutine's
    frame, the future it awaits, that future's wakeup callback back to
    the task -- is freed by the collector whenever it runs, before its
    loop closes as readily as after, and `asyncio.all_tasks` no longer
    answers for it once it is. What is left is `Task.__del__` handing
    `_DESTROYED_PENDING` to this method, which the default handler
    merely logs, and pytest's log capture shows only on a test that
    fails anyway. The handler still runs.
    """
    if context.get("message") == _DESTROYED_PENDING:
        _PENDING_TASKS.append(
            f"{_CURRENT_TEST_NODEID[0]}: {context.get('task')!r},"
            " freed by the collector while pending"
        )
    _real_call_exception_handler(self, context)


asyncio.BaseEventLoop.close = _record_pending_at_close  # type: ignore[method-assign]
asyncio.BaseEventLoop.call_exception_handler = _record_destroyed_pending  # type: ignore[method-assign]


def drain_pending_tasks() -> list[str]:
    """Empty `_PENDING_TASKS`, returning what it held."""
    drained = []
    while _PENDING_TASKS:
        drained.append(_PENDING_TASKS.popleft())
    return drained


def fail_on_pending_tasks() -> None:
    """Drain `_PENDING_TASKS`, failing the running test where it held any."""
    pending = drain_pending_tasks()
    if pending:
        pytest.fail(
            "tasks were left pending on an event loop"
            " (btclib-org/btclib-node#1107):\n" + "\n".join(pending),
            pytrace=False,
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(nextitem: pytest.Item | None) -> Iterator[None]:
    """Fail the test that left a task pending on a loop.

    After the fixtures' own finalizers, so a loop a fixture closes is
    measured against the test it was torn down for, and after a
    collection, so a task the test left reachable only through its own
    cycle is freed, and recorded, against this test rather than
    whichever one the collector next runs during.

    A teardown that raises skips the check, and so does a loop closed
    on a thread after its test has finished: what either leaves
    recorded fails the next test's teardown instead, each entry naming
    the test it was found during. After a worker's last test there is
    no next one, and `pytest_sessionfinish` reports it instead.
    """
    yield
    gc.collect()
    if nextitem is None:
        _CURRENT_TEST_NODEID[0] = "<after the last test>"
    fail_on_pending_tasks()


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object) -> None:
    """On the xdist controller, take in what a worker's session end found."""
    _PENDING_TASKS.extend(getattr(node, "workeroutput", {}).get(_WORKEROUTPUT_KEY, ()))


def pytest_sessionfinish(
    session: pytest.Session,
) -> None:  # pragma: no cover -- pytest-cov stops measuring when pytest_runtestloop ends, ahead of this
    """Report, and fail the run for, what is found pending after the last test.

    A worker hands it to the controller, whose own run of this reports
    it with what the controller found itself: xdist fails a run on a
    worker's exit status only where that was an interrupt.
    """
    gc.collect()
    pending = drain_pending_tasks()
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_WORKEROUTPUT_KEY] = pending
    elif pending:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        reporter.write_sep(
            "=",
            "tasks left pending on an event loop after the last test"
            " (btclib-org/btclib-node#1107)",
            red=True,
        )
        for entry in pending:
            reporter.write_line(entry)


def thread_exception_origin_note(args: threading.ExceptHookArgs) -> str | None:
    """Return the note owed on `args`, or `None` where none is.

    `None` covers three cases: `args.thread` is `None`, which
    `threading.excepthook` documents for a thread it could not
    determine; the thread is not in `_THREAD_ORIGIN` at all, having
    started before `_record_thread_origin` was patched in or through
    something other than `threading.Thread.start`; and the thread's
    recorded origin is the test running right now, in which case
    whatever pytest already attributes this to is correct and a note
    would say nothing new.
    """
    if args.thread is None:
        return None
    origin = _THREAD_ORIGIN.get(args.thread)
    if origin is None or origin == _CURRENT_TEST_NODEID[0]:
        return None
    return (
        f"this thread was started during {origin}, not during the test "
        "pytest reports this against -- that test is only the one in "
        "progress when the exception surfaced "
        "(btclib-org/btclib-node#1002)"
    )


def install_thread_exception_origin_hook() -> None:
    """Wrap `threading.excepthook` so a leaked thread's own note says who.

    Reads whatever `threading.excepthook` already is at the moment this
    runs and wraps it rather than replacing it: `pytest_configure`
    below installs this with `trylast=True`, so
    `_pytest.threadexception`'s own `pytest_configure` -- unmarked, and
    so run first -- has already replaced the default hook with the one
    that queues an exception for `PytestUnhandledThreadExceptionWarning`
    to be raised from later. `add_note` (PEP 678) lands the note in
    `args.exc_value` itself, which is what that later warning's own
    `traceback.format_exception` call formats -- so the note reaches the
    same report pytest already prints, rather than a second one nothing
    reads.
    """
    prev_hook = threading.excepthook

    def hook(args: threading.ExceptHookArgs) -> None:
        note = thread_exception_origin_note(args)
        if note is not None and args.exc_value is not None:
            args.exc_value.add_note(note)
        prev_hook(args)

    threading.excepthook = hook


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


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """Refuse a run that cannot see its floor; relax one that cannot clear it.

    A run coverage's configuration never reached is refused rather than
    gated, `pytest.UsageError` being what pytest prints without a
    traceback and exits `4` for -- an exit of its own, so the code says
    the run measured nothing rather than that something in the tree
    failed.

    `trylast=True` is for `install_thread_exception_origin_hook` below,
    not for the floor logic above: it needs `_pytest.threadexception`'s
    own `pytest_configure` -- unmarked, so ordinarily first -- to have
    already run and installed the hook this one wraps.
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
    install_thread_exception_origin_hook()


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
