# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What a connection does with what it cannot do.

The functional tests drive two connections that stay up and speak the
protocol. What is left is a message that will not serialize, how a
connection describes itself once the socket underneath it is gone, and
a peer answered past what this connection will queue for it.
"""

import asyncio
import socket
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast

import pytest
from btclib.hashes import hash256
from btclib.p2p.block_filters import BlockFilterType, CFilter
from btclib.p2p.keepalive import Ping
from btclib.p2p.limits import MAX_GETCFILTERS_SIZE
from btclib.p2p.message import Message
from btclib.p2p.payload import Payload

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p import connection as connection_module
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

    async def _send(data: bytes) -> None:
        # never reached: the message fails to serialize before _send
        # is called, which is the whole point of this test
        sent.append(data)  # pragma: no cover

    connection._send = _send  # type: ignore[method-assign]
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


@pytest.mark.parametrize(
    ("host", "endpoint"),
    [
        ("::ffff:1.2.3.4", "1.2.3.4:18444"),
        ("2001:db8::1", "[2001:db8::1]:18444"),
    ],
    ids=["v4-mapped", "ipv6"],
)
def test_a_connection_brackets_an_ipv6_peer(host: str, endpoint: str) -> None:
    client = cast(socket.socket, SimpleNamespace(getpeername=lambda: (host, 18444)))
    connection, _ = a_connection(client)
    assert repr(connection) == f"Connection to {endpoint}"


def test_a_connection_whose_socket_is_gone_says_so() -> None:
    client = socket.socket()
    client.close()
    connection, _ = a_connection(client)
    assert repr(connection) == "Broken connection"


def a_running_connection(
    loop: asyncio.AbstractEventLoop, client: socket.socket
) -> Connection:
    node = SimpleNamespace(
        chain=RegTest(),
        status=NodeStatus.Starting,
        logger=SimpleNamespace(
            warning=lambda *a: None, info=lambda *a: None, debug=lambda *a: None
        ),
    )
    discouraged: list[object] = []
    manager = SimpleNamespace(
        node=node,
        loop=loop,
        nonces=[],
        port=18444,
        peer_db=None,
        discourage=discouraged.append,
        discouraged=discouraged,
    )
    connection = Connection(
        cast(P2pManager, manager), client, peer_address("127.0.0.1", 18444), 0, False
    )
    return connection


def discouraged_of(connection: Connection) -> list[Any]:
    """The stub `discourage` list `a_running_connection` built.

    `connection.manager` is typed `P2pManager`, whose own `discouraged`
    is a `set[bytes]` -- the stub underneath is a `SimpleNamespace`
    carrying a `list` instead, so a caller comparing it against what was
    passed to `discourage` needs its own, unstatic view of the attribute.
    """
    return cast("list[Any]", cast(Any, connection.manager).discouraged)


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
    async def drive() -> Connection:
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection = a_running_connection(loop, ours)
        try:
            theirs.sendall(octets)
            await connection.run()
        finally:
            theirs.close()
        return connection

    connection = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Closed
    # #283: `Message.parse`, or the network-magic check right after it,
    # refusing this peer's own envelope is cause to discourage it
    assert discouraged_of(connection) == [connection.address]


def test_a_bug_of_this_node_s_own_in_parsing_drops_the_peer_but_not_discouraged() -> (
    None
):
    # #283: not every exception out of parse_messages is the peer's
    # fault -- a RuntimeError is not one btclib raised over the octets
    # it sent, the same distinction p2p/main.py's own except draws
    async def drive() -> Connection:
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection = a_running_connection(loop, ours)

        def boom() -> None:
            raise RuntimeError("no")

        connection.parse_messages = boom  # type: ignore[method-assign]
        try:
            theirs.sendall(b"x")
            await connection.run()
        finally:
            theirs.close()
        return connection

    connection = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Closed
    assert not discouraged_of(connection)


def test_a_connection_closed_before_it_reads_anything_reads_nothing() -> None:
    async def drive() -> bytes:
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection = a_running_connection(loop, ours)
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
    async def drive() -> tuple[Connection, bool]:
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection = a_running_connection(loop, ours)
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
        return connection, left_alone

    connection, left_alone = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Closed
    assert left_alone
    # a peer that hung up is not one that broke the protocol: #283
    assert not discouraged_of(connection)


def test_a_connections_own_task_cancelled_directly_still_closes_its_socket() -> None:
    # #312: P2pManager.stop() closes every connection it already knows
    # about through Connection.stop() -- but its own final sweep, over
    # asyncio.all_tasks(self.loop), cancels whatever task is still
    # pending there directly, the only reach it has left for a
    # connection accepted or dialled after its dict-based sweep already
    # ran. That direct task.cancel(), with nothing standing between it
    # and this coroutine's own suspension in sock_recv, must not be able
    # to leave self.client open the way going through stop() never does.
    async def drive() -> socket.socket:
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection = a_running_connection(loop, ours)
        task = asyncio.ensure_future(connection.run())
        connection.task = task  # type: ignore[assignment]
        # past send_version's own await and parked in sock_recv: nothing
        # here ever completes that read, so a task still running after
        # this is one still suspended there and not one already done
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        theirs.close()
        return ours

    ours = asyncio.run(drive())
    # a closed socket's own fileno is -1; still >= 0 is still open
    assert ours.fileno() == -1


def test_a_peer_already_at_the_send_bound_is_dropped_not_queued_further() -> None:
    async def drive() -> tuple[Connection, list[bytes]]:
        loop = asyncio.get_running_loop()
        connection = a_running_connection(loop, socket.socket())
        # already owed this much, whatever it is: one more octet queued
        # is refused regardless of what filled the budget
        connection.queued_send_bytes = connection_module.MAX_QUEUED_SEND_BYTES
        sent: list[bytes] = []

        async def _send(data: bytes) -> None:
            sent.append(data)  # pragma: no cover -- never reached

        connection._send = _send  # type: ignore[method-assign]
        await connection.async_send(Ping(1))
        return connection, sent

    connection, sent = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Closed
    # the reservation is untouched: the refused message never joined it
    assert connection.queued_send_bytes == connection_module.MAX_QUEUED_SEND_BYTES
    assert not sent
    # this node's own choice under load, not the peer's doing: #283
    assert not discouraged_of(connection)


def _message_overhead() -> int:
    """The wire octets `async_send` adds on top of one `cfilter`'s
    `filter_bytes`: `Message`'s envelope, plus `CFilter`'s own filter
    type, block hash, and `filter_bytes`'s own `var_int` length prefix.

    Measured, at a size in the range the two bursts below build,
    rather than assumed: `var_int` is five octets only above 65,536,
    which is a fact about the encoding and not about this module, so it
    is read off a real `Message` built the way `async_send` builds one
    instead of counted on to stay what it was last measured as.
    """
    size = 98_000
    payload = CFilter(
        BlockFilterType.BASIC, b"\x00" * 32, b"\x00" * size, check_validity=False
    ).serialize(check_validity=False)
    envelope = Message(RegTest().magic, "cfilter", payload).serialize()
    return len(envelope) - size


def _burst_summing_to(total_wire_bytes: int, count: int) -> list[CFilter]:
    """`count` `cfilter`-shaped answers whose wire bytes add to
    `total_wire_bytes` -- what `async_send` actually counts toward
    `queued_send_bytes`, not merely the `filter_bytes` argument each is
    built from.

    One `send` per block and nothing between it and the event loop is
    exactly `get_cfilters`'s own loop over a `getcfilters` request's
    heights, so a burst is what a single request answers with, not one
    message of the whole answer's size -- which no message here could
    be anyway, `Message.serialize` refusing a payload over
    `MAX_PROTOCOL_MESSAGE_LENGTH` regardless of `check_validity`.
    `filter_bytes` is not a real Golomb-Rice set: `check_validity=False`
    on both the object and `async_send`'s own `serialize` call is what
    lets zeroed octets stand in for one, the way a wrong-shaped block
    already does elsewhere in this test tree.
    """
    total = total_wire_bytes - count * _message_overhead()
    base = total // count
    sizes = [base] * count
    sizes[-1] += total - base * count  # the remainder, on the last one
    return [
        CFilter(
            BlockFilterType.BASIC, b"\x00" * 32, b"\x00" * size, check_validity=False
        )
        for size in sizes
    ]


def _two_bursts_in_flight(
    first_burst: list[CFilter], second_burst: list[CFilter]
) -> tuple[Connection, list[int]]:
    """Put two whole `getcfilters` answers in flight together.

    Both bursts are scheduled the way `get_cfilters`'s synchronous loop
    schedules them -- one `Connection.send` per block, all of one
    request before the next -- and the first is held open on a socket
    write that never finishes, the way a real one would be by a peer
    reading slower than this node can serialize. Returns the
    connection and the sizes `_send` actually saw.
    """

    async def drive() -> tuple[Connection, list[int]]:
        loop = asyncio.get_running_loop()
        connection = a_running_connection(loop, socket.socket())
        release = asyncio.Event()
        delivered: list[int] = []

        async def _send(data: bytes) -> None:
            delivered.append(len(data))
            await release.wait()

        connection._send = _send  # type: ignore[method-assign]

        first_tasks = [
            asyncio.ensure_future(connection.async_send(f)) for f in first_burst
        ]
        # one turn of the loop: every task in the burst runs its
        # synchronous prefix -- the bound check and the reservation --
        # before any of them reaches the (contended, after the first)
        # send lock
        await asyncio.sleep(0)
        assert connection.queued_send_bytes  # the first answer is on the books

        second_tasks = [
            asyncio.ensure_future(connection.async_send(f)) for f in second_burst
        ]
        await asyncio.sleep(0)

        release.set()
        await asyncio.gather(*first_tasks, *second_tasks)
        # closed here rather than left to the drop path: that path
        # closes it only where the third burst tips the connection into
        # P2pConnStatus.Closed, and the socket built above is real
        # (`socket.socket()`, not a pair `_send` stands in for) either
        # way
        connection.client.close()
        return connection, delivered

    return asyncio.run(drive())


def test_two_maximal_getcfilters_answers_pipelined_are_not_dropped() -> None:
    """`MAX_QUEUED_SEND_BYTES` is sized for BIP157 traffic, not against it.

    One maximal, realistically-estimated `getcfilters` answer -- 1,000
    blocks, `MAX_GETCFILTERS_SIZE`, at this module's own busy-block
    estimate -- is about half the bound, so a second one, pipelined
    behind the first per the issue's other complaint, still fits while
    the first has not finished draining.
    """
    one_response = connection_module.MAX_QUEUED_SEND_BYTES // 2
    connection, delivered = _two_bursts_in_flight(
        _burst_summing_to(one_response, MAX_GETCFILTERS_SIZE),
        _burst_summing_to(
            connection_module.MAX_QUEUED_SEND_BYTES - one_response - 4096,
            MAX_GETCFILTERS_SIZE,
        ),
    )
    assert connection.status == P2pConnStatus.Open
    assert len(delivered) == 2 * MAX_GETCFILTERS_SIZE
    assert connection.queued_send_bytes == 0


def test_a_third_maximal_answer_s_worth_in_flight_drops_the_peer() -> None:
    """Past twice a maximal legitimate answer, the peer is dropped.

    The same two answers as above, past the bound instead of short of
    it: not a single request out of spec, but more outstanding at once
    than the protocol's own per-request bound and this node's own
    pipelining allowance together account for.
    """
    one_response = connection_module.MAX_QUEUED_SEND_BYTES // 2
    connection, delivered = _two_bursts_in_flight(
        _burst_summing_to(one_response, MAX_GETCFILTERS_SIZE),
        _burst_summing_to(
            connection_module.MAX_QUEUED_SEND_BYTES - one_response + 4096,
            MAX_GETCFILTERS_SIZE,
        ),
    )
    assert connection.status == P2pConnStatus.Closed
    # the first answer reached the socket in full; at least one message
    # of the second, the one that tipped the bound, never did
    assert len(delivered) < 2 * MAX_GETCFILTERS_SIZE
    assert len(delivered) >= MAX_GETCFILTERS_SIZE
    # released and accounted for, not left on the books by the drop
    assert connection.queued_send_bytes == 0
