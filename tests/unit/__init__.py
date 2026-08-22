# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import signal

import pytest

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
