# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import re
import signal
import threading
import time
from contextlib import contextmanager

import pytest

import btclib_node
from btclib_node import Node
from btclib_node.config import Config


def a_node(tmp_path):
    return Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            debug=True,
        )
    )


def test_init(tmp_path):
    node = a_node(tmp_path)
    node.start()
    node.stop()


def test_stop_does_not_return_until_the_node_has_stopped(tmp_path):
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


def test_stopping_a_node_that_never_started_is_not_an_error(tmp_path):
    # __init__ registers the signal handlers, so a node can be asked to
    # stop before it is running and there is nothing to wait for
    node = a_node(tmp_path)
    node.stop()
    assert not node.is_alive()


def test_the_node_asking_itself_to_stop_does_not_wait_for_itself(tmp_path, monkeypatch):
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
def test_a_signal_asks_the_node_to_stop(tmp_path, signal_number):
    # both are registered on the process, and stopping is what they are
    # for: a node killed without it leaves its databases open
    node = a_node(tmp_path)
    node.start()
    handler = signal.getsignal(signal_number)
    handler(signal_number, None)
    node.join(timeout=10)
    assert not node.is_alive()


def test_a_step_that_raises_brings_the_node_down_rather_than_spinning(
    tmp_path, monkeypatch
):
    # the loop cannot recover from a chainstate it could not advance, so
    # it stops -- and stopping means closing the databases, which is
    # what makes this different from an exception escaping run()
    import btclib_node

    def boom(node):
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
def a_wedged_node(tmp_path, monkeypatch):
    """A started node whose loop will not come back, and lets go anyway."""
    wedged = threading.Event()
    released = threading.Event()

    def never_returns(node):
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
    pytestconfig,
):
    # the claim the constant's comment makes, asserted rather than
    # written: a node that will not stop has to be reported by name,
    # and it is only reported at all if this wait ends before the
    # harness gives up on the test around it
    assert btclib_node.STOP_TIMEOUT < int(pytestconfig.getini("timeout"))


def test_a_node_that_will_not_stop_is_reported_rather_than_waited_for(
    tmp_path, monkeypatch
):
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


def test_the_node_that_will_not_stop_is_named(tmp_path, monkeypatch):
    # several nodes are running in any functional test, and a message
    # that does not say which one leaves the reader to guess
    with a_wedged_node(tmp_path, monkeypatch) as node:
        with pytest.raises(Exception, match=re.escape(str(tmp_path))):
            node.stop()
