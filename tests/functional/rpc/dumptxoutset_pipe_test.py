# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`dumptxoutset` to a named pipe, live: the node serves while it waits.

The dump runs on `Node`'s thread, so a real node answering another
request while the pipe has no reader is what shows the loop is not held.
"""

import os
from threading import Thread
from typing import TYPE_CHECKING, Any

import pytest
from bitcoin_core_rpc import RpcError

from tests import rpc_client, wait_until, wait_until_listening

if TYPE_CHECKING:
    from btclib_node import Node

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes")


def waiting_dump(node: Node, answers: list[Any]) -> Thread:
    """Start `dumptxoutset` to a new pipe, and return once it is under way."""
    os.mkfifo(node.data_dir / "pipe.dat")

    def call() -> None:
        try:
            answers.append(
                rpc_client(node, 30).call("dumptxoutset", ["pipe.dat", "latest"])
            )
        except RpcError as error:
            answers.append(error)

    thread = Thread(target=call)
    thread.start()
    wait_until(lambda: len(node.pending_rpc) == 1)
    return thread


def test_a_dump_awaiting_its_reader_leaves_the_node_answering(rpc_node: Node) -> None:
    """Another request is answered, and then the reader gets the snapshot."""
    wait_until_listening(rpc_node.rpc_manager)
    answers: list[Any] = []
    thread = waiting_dump(rpc_node, answers)
    assert rpc_client(rpc_node).call("getblockcount") == 0
    with (rpc_node.data_dir / "pipe.dat").open("rb") as reader:
        data = reader.read()
    thread.join(30)
    assert answers[0]["coins_written"] == 0
    assert data[:5] == b"utxo\xff"


def test_stop_ends_a_dump_awaiting_its_reader(rpc_node: Node) -> None:
    """`stop` is answered and the waiting call ends, with no reader ever."""
    wait_until_listening(rpc_node.rpc_manager)
    answers: list[Any] = []
    thread = waiting_dump(rpc_node, answers)
    rpc_client(rpc_node).call("stop")
    thread.join(30)
    assert "Shutting down" in str(answers[0])
