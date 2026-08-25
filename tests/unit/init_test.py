# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What the node's own loop does with what it is handed.

Nearly all of `Node.run` was reached only by the functional tests: two
real nodes, or an HTTP client, and a race to win before the loop is
asked anything at all. That is #97 -- a run where no test failed and
the coverage floor went red all the same, on `run`'s own `except
Exception`. The managers below are stand-ins, so the loop is handed its
messages directly and what it does with them does not depend on
scheduling.
"""

import os
import re
import signal
import threading
import time
from collections import deque
from contextlib import contextmanager
from multiprocessing.pool import Pool, ThreadPool
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast, override

import pytest

import btclib_node
from btclib_node import Node
from btclib_node.config import Config
from btclib_node.exceptions import NodeShutdownTimeoutError
from btclib_node.interpreter import warm
from tests.conftest import unstarted_node_context
from tests.helpers import wait_until

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator


def a_node(tmp_path: Path) -> Node:
    """Return a regtest `Node`, neither p2p nor RPC enabled, never started."""
    return Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            debug=True,
        )
    )


class AManager:
    """What `Node.run` asks of a manager, and nothing else."""

    def __init__(self) -> None:
        """Build empty queues and connections, started/stopped both false."""
        # a stand-in for both P2pManager and RpcManager, whose queues
        # carry different shapes -- (command, payload, id) for one,
        # (batch, id) for the other -- so this is Any rather than either
        self.messages: deque[Any] = deque()
        self.handshake_messages: deque[Any] = deque()
        self.connections: dict[int, Any] = {}
        self.started = False
        self.stopped = False
        # only P2pManager's own peer_db attribute has one; run()'s
        # shutdown path reads it off whichever manager it holds without
        # checking which, so the stand-in carries it too (#263)
        self.peer_db = SimpleNamespace(close=lambda: None)

    def start(self) -> None:
        """Record that `run`'s own start branch reached this stand-in."""
        self.started = True

    def stop(self) -> None:
        """Record that `run`'s own teardown reached this stand-in."""
        self.stopped = True


@pytest.fixture
def a_networked_node(tmp_path: Path) -> Iterator[Node]:
    """Give a node whose ports are set and whose managers are stand-ins.

    The ports are what `run` reads to decide whether to start a manager;
    nothing binds them, because the managers built from them are thrown
    away here. Their event loops are closed rather than dropped, a
    dropped one being a ResourceWarning somewhere else's test.

    Stopped however the test ends, and a fixture rather than a call for
    that reason: the tests below wait on the loop, and a wait that gives
    up leaves a non-daemon thread holding the interpreter open after the
    last test has passed, which is the half of #98 no per-test limit
    reaches.
    """
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            p2p_port=18444,
            rpc_port=18445,
            debug=True,
        )
    )
    node.p2p_manager.loop.close()
    node.rpc_manager.loop.close()
    # the real P2pManager built above opened a real PeerDB, a database
    # of its own -- closed here, before the stand-in below drops the
    # only reference to it, or nothing ever closes it
    node.p2p_manager.peer_db.close()
    # AManager stands in for both real managers, over the shape of
    # theirs it actually reads: not their own types, deliberately
    node.p2p_manager = AManager()  # type: ignore[assignment]
    node.rpc_manager = AManager()  # type: ignore[assignment]
    yield node
    node.stop()


def test_init(tmp_path: Path) -> None:
    """A node built, started and stopped raises nothing."""
    node = a_node(tmp_path)
    node.start()
    node.stop()


def test_a_config_omitted_is_constructed_rather_than_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Node()` with no config gets a fresh `Config()`, field for field."""
    # `Node`'s own `config` used to default to `Config()`, built once at
    # `def`-time and handed to every caller that left it out (B008) --
    # nothing here relied on that sharing, so `None` plus a construction
    # in the body is the fix. `Path.home` is patched rather than left
    # alone: `Config()`'s own default `data_dir` is under it, and this
    # node is never started, so nothing else here would stop it from
    # writing under this session's real home directory.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    node = Node()
    try:
        assert node.config == Config()
    finally:
        # closed by hand, the same way `tests.conftest.unstarted_node_context`
        # closes a node it never starts: a node built and dropped here
        # leaves its databases, sockets and event loops for the garbage
        # collector, which raises against whichever test it is running
        # when it gets to them rather than against this one.
        node._close_worker_pool()
        node.p2p_manager.peer_db.close()
        node.chainstate.close()
        node.block_db.close()
        node.p2p_manager.loop.close()
        node.rpc_manager.loop.close()
        node.logger.close()


def test_stop_does_not_return_until_the_node_has_stopped(tmp_path: Path) -> None:
    """`stop` returns only once the loop exits and every database closes."""
    # what this pins is not a failure but a hang: a caller that goes on
    # while the node is still running leaves a thread logging into a
    # harness that has moved on, and, when the loop cannot come back at
    # all, a non-daemon thread holding the interpreter open after the
    # last test
    node = a_node(tmp_path)
    node.start()
    node.stop()
    assert not node.is_alive()
    assert node.chainstate.db.closed
    assert node.block_db.db.closed
    # closed by the end of run(), which is what stop() now waits for:
    # there is no handler left for a late record to be written to
    assert not node.logger.handlers


def test_stopping_a_node_that_never_started_is_not_an_error(tmp_path: Path) -> None:
    """`stop` on a node that was never `start()`ed raises nothing."""
    # __init__ registers the signal handlers, so a node can be asked to
    # stop before it is running and there is nothing to wait for. `stop`
    # itself has nothing to close: `run`'s teardown is what closes the
    # databases and the loops, and a node that never started never
    # reaches it -- unstarted_node_context is what closes them here.
    with unstarted_node_context(tmp_path) as node:
        node.stop()
        assert not node.is_alive()


def test_the_node_asking_itself_to_stop_does_not_wait_for_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stop` on the node's own thread signals and returns without joining."""
    # the `stop` RPC is handled inside the loop it stops, so the caller
    # there is the node's own thread, and a thread that joins itself
    # raises instead of waiting
    node = a_node(tmp_path)
    exceptions = []
    monkeypatch.setattr(node.logger, "exception", lambda *args: exceptions.append(args))
    monkeypatch.setattr(btclib_node, "update_chain", lambda node: node.stop())
    node.start()
    node.join(timeout=10)
    assert not node.is_alive()
    # run() logs and swallows what a step raises, so the exception a
    # self-join raises would leave the node stopping all the same
    assert not exceptions


@pytest.mark.parametrize("signal_number", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_asks_the_node_to_stop(
    tmp_path: Path, signal_number: signal.Signals
) -> None:
    """`SIGTERM`/`SIGINT` both reach a handler that stops the node."""
    # both are registered on the process, and stopping is what they are
    # for: a node killed without it leaves its databases open
    node = a_node(tmp_path)
    node.start()
    handler = signal.getsignal(signal_number)
    # getsignal also answers SIG_DFL, SIG_IGN or None -- a disposition
    # the process never installed a function for -- and calling one of
    # those is a TypeError rather than the failure this test is about.
    # That the node installed a handler at all is half of what it says.
    assert callable(handler)
    handler(signal_number, None)
    node.join(timeout=10)
    assert not node.is_alive()


def test_a_step_that_raises_brings_the_node_down_rather_than_spinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `update_chain` that raises stops the loop and closes both databases."""

    # the loop cannot recover from a chainstate it could not advance, so
    # it stops -- and stopping means closing the databases, which is
    # what makes this different from an exception escaping run()
    def boom(node: Node) -> None:
        raise RuntimeError("no")

    monkeypatch.setattr(btclib_node, "update_chain", boom)
    node = a_node(tmp_path)
    node.start()
    node.join(timeout=10)
    assert not node.is_alive()
    assert node.chainstate.db.closed
    assert node.block_db.db.closed


# how long the wedge holds if nothing lets it go. It has to outlast the
# bound under test by enough that a `stop` respecting that bound is
# never mistaken for one that does not, and it has to expire by itself:
# a test waiting inside `stop` is not in a position to release it, so a
# wedge with no expiry of its own turns a broken bound into a run that
# hangs -- which is the thing being tested for.
WEDGE_LIMIT = 30


@contextmanager
def a_wedged_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Node]:
    """Start a node whose loop will not come back, and let go anyway."""
    wedged = threading.Event()
    released = threading.Event()

    def never_returns(node: Node) -> None:
        wedged.set()
        released.wait(timeout=WEDGE_LIMIT)

    monkeypatch.setattr(btclib_node, "update_chain", never_returns)
    monkeypatch.setattr(btclib_node, "STOP_TIMEOUT", 0.5)
    node = a_node(tmp_path)
    # from the moment the thread exists, and not from the moment it is
    # known to be wedged: a node that never reaches the loop is still a
    # node holding the interpreter open, and the wait below is a thing
    # that can fail
    try:
        node.start()
        assert wedged.wait(timeout=10)
        yield node
    finally:
        released.set()
        node.join(timeout=10)
        assert not node.is_alive()


def test_the_bound_is_under_the_limit_that_would_otherwise_expire_first(
    pytestconfig: pytest.Config,
) -> None:
    """`STOP_TIMEOUT` is well under `pytest-timeout`'s own per-test limit."""
    # the claim the constant's comment makes, asserted rather than
    # written: a node that will not stop has to be reported by name,
    # and it is only reported at all if this wait ends before the
    # harness gives up on the test around it. STOP_TIMEOUT stays the
    # subject on the left -- SIM300 has no accidental-assignment risk
    # to guard against in Python, and STOP_TIMEOUT's own comment states
    # the claim this way round: "well under the per-test limit".
    assert btclib_node.STOP_TIMEOUT < int(pytestconfig.getini("timeout"))  # noqa: SIM300


def test_a_node_that_will_not_stop_is_reported_rather_than_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound on the wait, and why it has to be one.

    `pytest-timeout` arms one timer per test and `setitimer` is
    one-shot, so a limit already spent in the call phase is not there
    for the teardown -- and `tests/conftest.py`'s `node_context` calls
    `stop` from a `finally`. An unbounded wait there is a worker that
    never reports and a controller that waits for it: the run stops
    rather than failing, which is the shape
    btclib-org/btclib-node#98 exists to remove.
    """
    with a_wedged_node(tmp_path, monkeypatch) as node:
        start = time.perf_counter()
        with pytest.raises(NodeShutdownTimeoutError, match="did not stop"):
            node.stop()
        elapsed = time.perf_counter() - start
        # bracketed by what it was told, not merely finite: a wait
        # bounded by some other literal would pass an upper bound alone
        # half the bound and not the bound itself: a timed acquire does
        # not return early, so the exact figure holds by a margin too
        # thin to be an assertion about anything
        assert 0.5 * btclib_node.STOP_TIMEOUT <= elapsed < 3 * btclib_node.STOP_TIMEOUT
        assert node.is_alive()


def test_the_node_that_will_not_stop_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NodeShutdownTimeoutError` names the wedged node's own data dir."""
    # several nodes are running in any functional test, and a message
    # that does not say which one leaves the reader to guess
    with (
        a_wedged_node(tmp_path, monkeypatch) as node,
        pytest.raises(NodeShutdownTimeoutError, match=re.escape(str(tmp_path))),
    ):
        node.stop()


def test_a_port_configured_is_a_manager_started_and_stopped(
    tmp_path: Path, a_networked_node: Node
) -> None:
    """`run` starts and stops only the manager whose port was set."""
    # the ports decide it: a node given neither starts neither, which is
    # every other node in this file
    node = a_networked_node
    p2p_manager = cast("AManager", node.p2p_manager)
    rpc_manager = cast("AManager", node.rpc_manager)
    assert (node.p2p_port, node.rpc_port) == (18444, 18445)
    node.start()
    wait_until(lambda: p2p_manager.started and rpc_manager.started)
    node.stop()
    assert p2p_manager.stopped
    assert rpc_manager.stopped

    quiet = a_node(tmp_path / "quiet")
    quiet.start()
    quiet.stop()
    assert (quiet.p2p_port, quiet.rpc_port) == (None, None)
    # and neither manager thread was ever started: asserting the ports
    # alone is a fact about Config, true whether or not `run` started
    # anything
    assert not quiet.p2p_manager.is_alive()
    assert not quiet.rpc_manager.is_alive()


def test_every_message_waiting_is_taken_before_the_loop_waits(
    a_networked_node: Node,
) -> None:
    """`run` drains all three queues in one pass, not a sleep between them."""
    # all three queues, drained in one pass: what each handler does with
    # a message it can deliver is tests/unit/p2p/main_test.py's and
    # tests/unit/rpc/main_test.py's. These are addressed to connections that
    # are not there, so the answer is to drop them -- and dropping them
    # is what the loop has to do rather than sleep on them.
    node = a_networked_node
    p2p_manager = cast("AManager", node.p2p_manager)
    rpc_manager = cast("AManager", node.rpc_manager)
    p2p_manager.handshake_messages.append(("version", None, 99))
    p2p_manager.messages.append(("ping", None, 99))
    rpc_manager.messages.append(([], 99))

    def every_queue_is_empty() -> bool:
        return not (
            p2p_manager.handshake_messages
            or p2p_manager.messages
            or rpc_manager.messages
        )

    node.start()
    wait_until(every_queue_is_empty)


def test_a_message_the_handlers_did_not_expect_does_not_end_the_loop(
    a_networked_node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued entry of the wrong shape is logged; the loop keeps answering."""
    # #97's line. This test's own trigger used to be a method that is
    # not a string: `request["method"] not in callbacks` raised
    # TypeError: unhashable, outside handle_rpc's own try. #63 makes
    # is_valid_rpc refuse that shape before the lookup runs, answered
    # rather than raised -- tests/unit/rpc/main_test.py covers that
    # shape now. What is left to exercise here is `run`'s own guard,
    # not that one particular cause of it firing: a queued entry of the
    # wrong shape unpacks nowhere handle_rpc's own try reaches,
    # `data, conn_id = ...popleft()` being the function's first line.
    node = a_networked_node
    rpc_manager = cast("AManager", node.rpc_manager)
    answered: list[Any] = []
    rpc_manager.connections[0] = SimpleNamespace(
        send=answered.append, send_and_wait=answered.append
    )
    logged: list[Any] = []
    monkeypatch.setattr(node.logger, "exception", logged.append)
    rpc_manager.messages.append("not a (batch, id) pair")
    node.start()
    wait_until(lambda: logged)

    rpc_manager.messages.append(
        ([{"jsonrpc": "2.0", "id": "b", "method": "getbestblockhash"}], 0)
    )
    wait_until(lambda: answered)
    node.stop()
    (answer,) = answered
    assert answer[0]["id"] == "b"


class APool:
    """A worker pool that costs nothing to build, and says it was."""

    def __init__(self, built: list[Any], processes: int) -> None:
        """Record `processes` into `built`, this stand-in's own report list."""
        self.built = built
        built.append(processes)

    def terminate(self) -> None:
        """Record that `_close_worker_pool` called `terminate` before `join`."""
        self.built.append("terminated")

    def join(self) -> None:
        """Record that `_close_worker_pool` called `join` after `terminate`."""
        self.built.append("joined")


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Patch both `btclib_node.Pool` and `.ThreadPool` to `APool`.

    `_pool_factory` picks one of the two by `sys._is_gil_enabled()`, and
    that answer is the interpreter running this suite's, not this
    fixture's to choose (issue #388) -- patching only `Pool` would make
    every test below pass under a GIL build and fail under a
    free-threaded one, on an assertion about which pool was built rather
    than about `worker_pool`'s own behaviour. Both names patched to the
    same stand-in makes the test interpreter-agnostic instead.
    """
    built: list[Any] = []
    monkeypatch.setattr(btclib_node, "Pool", lambda processes: APool(built, processes))
    monkeypatch.setattr(
        btclib_node, "ThreadPool", lambda processes: APool(built, processes)
    )
    return built


@pytest.mark.parametrize(
    ("gil_enabled", "expected"),
    [(True, Pool), (False, ThreadPool)],
    ids=["gil-enabled", "gil-disabled"],
)
def test_pool_factory_picks_by_gil_enabled(
    *, gil_enabled: bool, expected: type[Pool]
) -> None:
    """`_pool_factory` returns `Pool` if the GIL is on, `ThreadPool` if not.

    Both arms constructible and asserted on whichever interpreter runs
    this: a `ThreadPool` builds as readily as a `Pool` under a GIL
    build, so this does not need a second interpreter to exercise the
    branch the free-threaded one would take (issue #388).
    """
    assert btclib_node._pool_factory(gil_enabled=gil_enabled) is expected


def test_the_worker_pool_is_built_on_first_use_and_only_once(
    tmp_path: Path, pools: list[Any]
) -> None:
    """`node.worker_pool` builds nothing until read, then the same pool."""
    # a pool is interpreters or threads, and most of the nodes this
    # suite builds never validate a script
    with unstarted_node_context(tmp_path) as node:
        assert not pools
        pool = node.worker_pool
        assert node.worker_pool is pool
        assert pools == [btclib_node._WORKER_COUNT]


def test_a_node_that_used_the_pool_takes_it_down_with_it(
    tmp_path: Path, pools: list[Any]
) -> None:
    """`run`'s teardown terminates and joins a built pool, then drops it."""
    # terminate() alone leaves the pool referenced by a Node that a
    # test can go on holding well past its own worker processes exiting
    # (btclib-org/btclib-node#195): join() has to follow it, and the
    # reference has to go, so nothing later is left to collect it.
    node = a_node(tmp_path)
    assert node.worker_pool is not None
    node.start()
    node.stop()
    assert pools == [btclib_node._WORKER_COUNT, "terminated", "joined"]
    assert node._worker_pool is None


def test_a_node_that_never_used_the_pool_does_not_build_one_to_stop_it(
    tmp_path: Path, pools: list[Any]
) -> None:
    """A node that never reads `worker_pool` builds no pool over its life."""
    node = a_node(tmp_path)
    node.start()
    node.stop()
    assert not pools


def test_del_closes_a_worker_pool_on_a_node_that_was_never_started(
    tmp_path: Path, pools: list[Any]
) -> None:
    """`__del__` terminates and joins a pool `run`'s teardown never reached."""
    # tests/unit/main_test.py calls update_chain directly against a
    # Node built and never start()ed, which is the shape this guards:
    # run()'s own teardown never runs for one, so nothing but __del__
    # ever takes the pool it built back down (btclib-org/btclib-node#195).
    # Called directly rather than through `del` and a collection: the
    # node's own signal handler closes over `self`, so it is still
    # referenced from `signal`'s table and a real collection would not
    # reach it inside this test, only whenever the next test's own node
    # replaces that handler.
    with unstarted_node_context(tmp_path) as node:
        assert node.worker_pool is not None
        assert pools == [btclib_node._WORKER_COUNT]

        node.__del__()

        assert pools == [btclib_node._WORKER_COUNT, "terminated", "joined"]
        assert node._worker_pool is None


def test_del_on_a_node_that_never_built_a_pool_does_nothing(
    tmp_path: Path, pools: list[Any]
) -> None:
    """`__del__` on a node that never read `worker_pool` closes nothing."""
    with unstarted_node_context(tmp_path) as node:
        node.__del__()

        assert not pools


class ARecordingPool(APool):
    """A worker pool that also remembers what it was asked to run."""

    def __init__(self, built: list[Any], processes: int) -> None:
        """Build like `APool`, and start `calls` empty."""
        super().__init__(built, processes)
        self.calls: list[tuple[Any, list[Any]]] = []

    def starmap(
        self, fn: Callable[..., object], args: Iterable[Iterable[object]]
    ) -> None:
        """Record `fn` and the whole of `args`, without calling `fn` at all."""
        self.calls.append((fn, list(args)))


def test_warm_worker_pool_builds_it_and_warms_it_off_the_calling_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`warm_worker_pool` builds the pool and dispatches `warm` many times."""
    built: list[Any] = []
    instances: list[ARecordingPool] = []

    def make_pool(processes: int) -> ARecordingPool:
        pool = ARecordingPool(built, processes)
        instances.append(pool)
        return pool

    # both names, for the same reason the `pools` fixture patches both:
    # which one `_pool_factory` reaches for is the running interpreter's
    # to decide, not this test's (issue #388)
    monkeypatch.setattr(btclib_node, "Pool", make_pool)
    monkeypatch.setattr(btclib_node, "ThreadPool", make_pool)
    with unstarted_node_context(tmp_path) as node:
        node.warm_worker_pool()

        assert node._worker_pool_warmup is not None
        node._worker_pool_warmup.join(timeout=5)
        assert built == [btclib_node._WORKER_COUNT]
        (pool,) = instances
        (call,) = pool.calls
        fn, args = call
        assert fn is warm
        assert len(args) == btclib_node._WORKER_COUNT * 4
        assert all(a == () for a in args)


def test_a_second_call_to_warm_worker_pool_does_not_start_a_second_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second `warm_worker_pool` call is a no-op once the pool is built."""
    # download_manager.block_download calls this once per dispatched
    # batch of blocks, not once per node, so the guard the call site
    # used to get for free from headers()'s own status transition now
    # lives here instead: btclib-org/btclib-node#262
    built: list[Any] = []

    def make_pool(processes: int) -> ARecordingPool:
        return ARecordingPool(built, processes)

    monkeypatch.setattr(btclib_node, "Pool", make_pool)
    monkeypatch.setattr(btclib_node, "ThreadPool", make_pool)
    with unstarted_node_context(tmp_path) as node:
        node.warm_worker_pool()
        first = node._worker_pool_warmup
        assert first is not None
        first.join(timeout=5)

        node.warm_worker_pool()

        assert node._worker_pool_warmup is first
        assert built == [btclib_node._WORKER_COUNT]


def test_stopping_the_node_waits_for_an_in_flight_warmup_before_the_pool_comes_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run`'s teardown joins the warm-up before the pool terminates."""
    # a warm-up still building or warming the pool when the loop stops
    # races the `is not None` check `run`'s teardown does before
    # `terminate` -- the pool it eventually builds would never be
    # handed to it. Held here with a pool whose own starmap blocks
    # until released, so the race is forced rather than hoped for.
    built: list[Any] = []
    entered = threading.Event()
    release = threading.Event()

    class SlowPool(ARecordingPool):
        @override
        def starmap(self, fn: Any, args: Any) -> None:
            entered.set()
            release.wait(timeout=5)
            super().starmap(fn, args)

    def make_slow_pool(processes: int) -> SlowPool:
        return SlowPool(built, processes)

    monkeypatch.setattr(btclib_node, "Pool", make_slow_pool)
    monkeypatch.setattr(btclib_node, "ThreadPool", make_slow_pool)
    node = a_node(tmp_path)
    node.start()

    node.warm_worker_pool()
    assert entered.wait(timeout=5)

    node.terminate_flag.set()
    # run's own loop has nothing else to drain (p2p and rpc are both
    # off) and notices the flag within one spin; well short of this is
    # enough margin for it to reach the join this test is about without
    # making a passing run wait on it
    time.sleep(0.2)
    assert "terminated" not in built

    release.set()
    node.join(timeout=5)
    assert built == [btclib_node._WORKER_COUNT, "terminated", "joined"]


def test_worker_count_defaults_to_eight_outside_of_xdist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no `PYTEST_XDIST_WORKER_COUNT`, the worker count defaults to 8."""
    monkeypatch.delenv("PYTEST_XDIST_WORKER_COUNT", raising=False)
    assert btclib_node._default_worker_count() == 8


def test_worker_count_is_the_machine_split_across_xdist_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under xdist, the worker count divides the core count across workers."""
    # ten cores split six ways is one worker short of two each: what
    # matters is that the total the run spawns tracks the core count
    # rather than staying flat at eight regardless of it
    # (btclib-org/btclib-node#46)
    monkeypatch.setattr(os, "cpu_count", lambda: 10)
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "6")
    assert btclib_node._default_worker_count() == 1


def test_worker_count_under_xdist_never_goes_below_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More xdist workers than cores still floors at one worker."""
    # more xdist workers than cores still has to build a pool at all:
    # `Pool(processes=0)` raises, and so does `ThreadPool(processes=0)`
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "20")
    assert btclib_node._default_worker_count() == 1


def test_worker_count_falls_back_to_eight_split_if_the_core_count_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `None` `os.cpu_count()` falls back to eight, split across xdist."""
    # `os.cpu_count()` is documented to return `None` where it cannot
    # tell
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "4")
    assert btclib_node._default_worker_count() == 2
