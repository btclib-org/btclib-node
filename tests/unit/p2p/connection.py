# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What a connection does with what it cannot do.

The functional tests drive two connections that stay up and speak the
protocol. What is left is a message that will not serialize, and how a
connection describes itself once the socket underneath it is gone.
"""

import asyncio
import socket
from types import SimpleNamespace

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p.address import NetworkAddress
from btclib_node.p2p.connection import Connection


class Unserializable:
    command = "unserializable"

    def serialize(self, *, check_validity=True):
        raise ValueError("no")


def a_connection(client=None):
    logged = []
    node = SimpleNamespace(
        chain=RegTest(),
        logger=SimpleNamespace(
            warning=logged.append, info=lambda *a: None, debug=lambda *a: None
        ),
    )
    manager = SimpleNamespace(node=node, loop=None, peer_db=None)
    connection = Connection(
        manager,
        client if client is not None else socket.socket(),
        NetworkAddress.from_ip_and_port("1.2.3.4", 18444),
        0,
        False,
    )
    return connection, logged


def test_a_message_that_will_not_serialize_is_logged_and_dropped():
    # the connection stays up: one message this node cannot build is not
    # a reason to drop a peer that has done nothing wrong
    connection, logged = a_connection()
    sent = []
    connection._send = lambda data: sent.append(data)
    asyncio.run(connection.async_send(Unserializable()))
    assert not sent
    (line,) = logged
    assert "error in serializing message" in line
    connection.client.close()


def test_a_connection_names_the_peer_it_is_to():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    try:
        connection, _ = a_connection(client)
        assert repr(connection) == f"Connection to 127.0.0.1:{port}"
    finally:
        client.close()
        listener.close()


def test_a_connection_whose_socket_is_gone_says_so():
    client = socket.socket()
    client.close()
    connection, _ = a_connection(client)
    assert repr(connection) == "Broken connection"


def a_running_connection(loop, client):
    stopped = []
    node = SimpleNamespace(
        chain=RegTest(),
        status=NodeStatus.Starting,
        logger=SimpleNamespace(
            warning=lambda *a: None, info=lambda *a: None, debug=lambda *a: None
        ),
    )
    manager = SimpleNamespace(
        node=node,
        loop=loop,
        nonces=[],
        port=18444,
        peer_db=SimpleNamespace(add_active_address=stopped.append),
    )
    connection = Connection(
        manager, client, NetworkAddress.from_ip_and_port("127.0.0.1", 18444), 0, False
    )
    return connection, stopped


def test_a_peer_sending_something_that_is_not_a_message_is_dropped():
    async def drive():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection, stopped = a_running_connection(loop, ours)
        # a magic no chain uses: Message.parse refuses the envelope, and
        # a peer that cannot frame a message is not one to keep
        theirs.sendall(b"\x11\x22\x33\x44" + b"\x00" * 20)
        await connection.run()
        theirs.close()
        return connection, stopped

    connection, stopped = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Closed
    assert stopped == [connection.address]


def test_a_connection_closed_before_it_reads_anything_reads_nothing():
    async def drive():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection, _ = a_running_connection(loop, ours)
        connection.status = P2pConnStatus.Closed
        await connection.run()
        # what the peer got is the version, and nothing was read back
        answer = theirs.recv(4096)
        theirs.close()
        ours.close()
        return answer

    assert asyncio.run(drive())


def test_a_peer_that_hangs_up_is_dropped():
    async def drive():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection, stopped = a_running_connection(loop, ours)
        theirs.close()
        await connection.run()
        return connection, stopped

    connection, stopped = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Closed
    assert stopped == [connection.address]
