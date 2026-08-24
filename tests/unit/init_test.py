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

import re
import signal
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import btclib_node
from btclib_node import Node
from btclib_node.config import Config
from tests.helpers import wait_until


def a_node(tmp_path: Path) -> Node:
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
        # a stand-in for both P2pManager and RpcManager, whose queues
        # carry different shapes -- (command, payload, id) for one,
        # (batch, id) for the other -- so this is Any rather than either
        self.messages: deque[Any] = deque()
        self.handshake_messages: deque[Any] = deque()
        self.connections: dict[int, Any] = {}
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def a_networked_node(tmp_path: Path) -> Iterator[Node]:
    """A node whose ports are set and whose managers are stand-ins.

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
    # AManager stands in for both real managers, over the shape of
    # theirs it actually reads: not their own types, deliberately
    node.p2p_manager = AManager()  # type: ignore[assignment]
    node.rpc_manager = AManager()  # type: ignore[assignment]
    yield node
    node.stop()


def test_init(tmp_path: Path) -> None:
    node = a_node(tmp_path)
    node.start()
    node.stop()


def test_stop_does_not_return_until_the_node_has_stopped(tmp_path: Path) -> None:
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
    # __init__ registers the signal handlers, so a node can be asked to
    # stop before it is running and there is nothing to wait for
    node = a_node(tmp_path)
    node.stop()
    assert not node.is_alive()


def test_the_node_asking_itself_to_stop_does_not_wait_for_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the `stop` RPC is handled inside the loop it stops, so the caller
    # there is the node's own thread, and a thread that joins itself
    # raises instead of waiting
    import btclib_node

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
    # the loop cannot recover from a chainstate it could not advance, so
    # it stops -- and stopping means closing the databases, which is
    # what makes this different from an exception escaping run()
    import btclib_node

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
    """A started node whose loop will not come back, and lets go anyway."""
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
    # the claim the constant's comment makes, asserted rather than
    # written: a node that will not stop has to be reported by name,
    # and it is only reported at all if this wait ends before the
    # harness gives up on the test around it
    assert btclib_node.STOP_TIMEOUT < int(pytestconfig.getini("timeout"))


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
        with pytest.raises(Exception, match="did not stop"):
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
    # several nodes are running in any functional test, and a message
    # that does not say which one leaves the reader to guess
    with a_wedged_node(tmp_path, monkeypatch) as node:
        with pytest.raises(Exception, match=re.escape(str(tmp_path))):
            node.stop()


def test_a_port_configured_is_a_manager_started_and_stopped(
    tmp_path: Path, a_networked_node: Node
) -> None:
    # the ports decide it: a node given neither starts neither, which is
    # every other node in this file
    node = a_networked_node
    p2p_manager = cast(AManager, node.p2p_manager)
    rpc_manager = cast(AManager, node.rpc_manager)
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
    # all three queues, drained in one pass: what each handler does with
    # a message it can deliver is tests/unit/p2p/main.py's and
    # tests/unit/rpc/main.py's. These are addressed to connections that
    # are not there, so the answer is to drop them -- and dropping them
    # is what the loop has to do rather than sleep on them.
    node = a_networked_node
    p2p_manager = cast(AManager, node.p2p_manager)
    rpc_manager = cast(AManager, node.rpc_manager)
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
    rpc_manager = cast(AManager, node.rpc_manager)
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
        self.built = built
        built.append(processes)

    def terminate(self) -> None:
        self.built.append("terminated")


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import btclib_node

    built: list[Any] = []
    monkeypatch.setattr(btclib_node, "Pool", lambda processes: APool(built, processes))
    return built


def test_the_worker_pool_is_built_on_first_use_and_only_once(
    tmp_path: Path, pools: list[Any]
) -> None:
    # a pool is interpreters, spawned rather than forked wherever that
    # is the default, and most of the nodes this suite builds never
    # validate a script
    node = a_node(tmp_path)
    assert not pools
    pool = node.worker_pool
    assert node.worker_pool is pool
    assert pools == [8]


def test_a_node_that_used_the_pool_takes_it_down_with_it(
    tmp_path: Path, pools: list[Any]
) -> None:
    node = a_node(tmp_path)
    assert node.worker_pool is not None
    node.start()
    node.stop()
    assert pools == [8, "terminated"]


def test_a_node_that_never_used_the_pool_does_not_build_one_to_stop_it(
    tmp_path: Path, pools: list[Any]
) -> None:
    node = a_node(tmp_path)
    node.start()
    node.stop()
    assert not pools
