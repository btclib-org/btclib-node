# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The listening half of the RPC manager, without a node behind it.

What answers a request is `handle_rpc`, which the node's loop calls and
tests/unit/rpc/main.py covers. What is left is everything between the
port and that queue -- binding it, accepting a client, and letting go
of both -- and until now only a functional test reached any of it.
"""

import json
import socket
from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol, cast

import pytest

from btclib_node.chains import RegTest
from btclib_node.log import Logger
from btclib_node.rpc.manager import RpcManager
from tests.helpers import get_random_port, wait_until, wait_until_listening

if TYPE_CHECKING:
    from btclib_node import Node

REQUEST = {"jsonrpc": "2.0", "id": "a", "method": "getbestblockhash"}


class AManagerFactory(Protocol):
    def __call__(self, port: int | None) -> RpcManager: ...


@pytest.fixture
def a_manager() -> Iterator[AManagerFactory]:
    """Build managers, and close their event loops however the test ends."""
    made: list[RpcManager] = []

    def make(port: int | None) -> RpcManager:
        manager = RpcManager(
            cast("Node", SimpleNamespace(logger=Logger(debug=True), chain=RegTest())),
            port,
        )
        made.append(manager)
        return manager

    yield make
    for manager in made:
        # a no-op on the loop a stopped manager has already closed
        manager.loop.close()


def as_http(payload: Mapping[str, object]) -> bytes:
    body = json.dumps(payload).encode()
    head = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body)
    return head + body


def test_a_manager_says_when_it_is_listening_and_queues_what_arrives(
    a_manager: AManagerFactory,
) -> None:
    # #46: `is_alive()` holds before `run` has bound anything, so a
    # client that posts on the strength of it is refused. The event is
    # what a caller can wait on instead.
    port = get_random_port()
    manager = a_manager(port)
    # the bind and not the thread: see tests/unit/p2p/manager.py
    assert not manager.listening.is_set()
    manager.start()
    try:
        wait_until_listening(manager)
        with socket.create_connection(("127.0.0.1", port), timeout=20) as client:
            client.sendall(as_http(REQUEST))
            wait_until(lambda: manager.messages)
            data, conn_id = manager.messages.popleft()
        # handed on as it arrived, and addressed to the connection it
        # arrived on, which is how the answer gets back to this client
        assert data == [REQUEST]
        assert conn_id in manager.connections
    finally:
        manager.stop()
        manager.join(timeout=10)
    assert not manager.is_alive()
    assert manager.loop.is_closed()
    # and it stops saying so once the socket is gone
    assert not manager.listening.is_set()


def test_an_answer_is_written_back_to_the_client_that_asked(
    a_manager: AManagerFactory,
) -> None:
    # `Connection.send` is what the node's loop calls once it has an
    # answer, from its own thread: the write itself belongs to the
    # manager's loop, and this is the line that crosses over
    port = get_random_port()
    manager = a_manager(port)
    manager.start()
    try:
        wait_until_listening(manager)
        with socket.create_connection(("127.0.0.1", port), timeout=20) as client:
            client.sendall(as_http(REQUEST))
            wait_until(lambda: manager.messages)
            _, conn_id = manager.messages.popleft()
            answer = {"jsonrpc": "2.0", "result": "0" * 64, "id": "a"}
            manager.connections[conn_id].send([answer])
            client.settimeout(20)
            head, _, body = client.recv(4096).partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200 OK\r\n")
        assert json.loads(body) == answer
    finally:
        manager.stop()
        manager.join(timeout=10)
