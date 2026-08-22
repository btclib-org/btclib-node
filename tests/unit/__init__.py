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
