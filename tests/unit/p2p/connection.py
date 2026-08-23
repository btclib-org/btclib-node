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
from contextlib import suppress
from types import SimpleNamespace
from typing import cast

import pytest
from btclib.hashes import hash256
from btclib.p2p.addrv2 import NetworkAddressV2
from btclib.p2p.payload import Payload

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p.address import peer_address
from btclib_node.p2p.connection import Connection
from btclib_node.p2p.manager import P2pManager


class Unserializable:
    command = "unserializable"

    def serialize(self, *, check_validity: bool = True) -> bytes:
        raise ValueError("no")


def a_connection(
    client: socket.socket | None = None,
) -> tuple[Connection, list[str]]:
    logged: list[str] = []
    node = SimpleNamespace(
        chain=RegTest(),
        logger=SimpleNamespace(
            warning=logged.append, info=lambda *a: None, debug=lambda *a: None
        ),
    )
    manager = SimpleNamespace(node=node, loop=None, peer_db=None)
    connection = Connection(
        cast(P2pManager, manager),
        client if client is not None else socket.socket(),
        peer_address("1.2.3.4", 18444),
        0,
        False,
    )
    return connection, logged


def test_a_message_that_will_not_serialize_is_logged_and_dropped() -> None:
    # the connection stays up: one message this node cannot build is not
    # a reason to drop a peer that has done nothing wrong
    connection, logged = a_connection()
    sent: list[bytes] = []

    async def fake_send(data: bytes) -> None:
        sent.append(data)

    connection._send = fake_send  # type: ignore[method-assign]
    with connection.client:
        asyncio.run(connection.async_send(cast(Payload, Unserializable())))
    assert not sent
    (line,) = logged
    assert "error in serializing message" in line


def test_a_connection_names_the_peer_it_is_to() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        with socket.create_connection(("127.0.0.1", port)) as client:
            connection, _ = a_connection(client)
            assert repr(connection) == f"Connection to 127.0.0.1:{port}"
    finally:
        listener.close()


def test_a_connection_whose_socket_is_gone_says_so() -> None:
    client = socket.socket()
    client.close()
    connection, _ = a_connection(client)
    assert repr(connection) == "Broken connection"


def a_running_connection(
    loop: asyncio.AbstractEventLoop, client: socket.socket
) -> tuple[Connection, list[NetworkAddressV2]]:
    stopped: list[NetworkAddressV2] = []
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
        cast(P2pManager, manager), client, peer_address("127.0.0.1", 18444), 0, False
    )
    return connection, stopped


def a_message_for_another_network() -> bytes:
    # well formed in every other way: the checksum is right, so what is
    # left to refuse it on is the network it announces
    payload = b""
    return (
        b"\x11\x22\x33\x44"
        + b"verack".ljust(12, b"\x00")
        + len(payload).to_bytes(4, "little")
        + hash256(payload)[:4]
        + payload
    )


@pytest.mark.parametrize(
    "octets",
    [
        # a checksum that belongs to no payload: refused by the envelope
        b"\x11\x22\x33\x44" + b"\x00" * 20,
        a_message_for_another_network(),
    ],
    ids=["not a message", "a message for another network"],
)
def test_a_peer_sending_something_this_node_cannot_read_is_dropped(
    octets: bytes,
) -> None:
    async def drive() -> tuple[Connection, list[NetworkAddressV2]]:
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection, stopped = a_running_connection(loop, ours)
        try:
            theirs.sendall(octets)
            await connection.run()
        finally:
            theirs.close()
        return connection, stopped

    connection, stopped = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Closed
    assert stopped == [connection.address]


def test_a_connection_closed_before_it_reads_anything_reads_nothing() -> None:
    async def drive() -> bytes:
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection, _ = a_running_connection(loop, ours)
        connection.status = P2pConnStatus.Closed
        await connection.run()
        try:
            # bounded: a version that never arrives is a failure, not a
            # run that never ends
            theirs.settimeout(1)
            # what the peer got is the version, and nothing was read back
            return theirs.recv(4096)
        finally:
            theirs.close()
            ours.close()

    assert asyncio.run(drive())


def test_a_peer_that_hangs_up_is_dropped() -> None:
    async def drive() -> tuple[Connection, list[NetworkAddressV2], bool]:
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection, stopped = a_running_connection(loop, ours)
        # the task the read loop is running in: cancelling it from
        # inside would cancel the coroutine doing the cancelling, which
        # is why stop() is asked not to
        task = asyncio.ensure_future(asyncio.sleep(60))
        connection.task = task  # type: ignore[assignment]
        theirs.close()
        await connection.run()
        # asked inside the loop: shutting the loop down cancels whatever
        # is still pending, which would answer this on its own
        left_alone = not task.cancelling()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return connection, stopped, left_alone

    connection, stopped, left_alone = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Closed
    assert stopped == [connection.address]
    assert left_alone
