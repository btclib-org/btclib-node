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
import logging
import socket
import threading
import time
from collections import deque
from contextlib import suppress
from importlib.metadata import version
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, cast

import pytest
from btclib.hashes import hash256
from btclib.p2p.address import ServiceFlags
from btclib.p2p.block_filters import BlockFilterType, CFilter
from btclib.p2p.handshake import Verack, Version
from btclib.p2p.inventory import GetData, Inventory, InventoryType
from btclib.p2p.keepalive import Ping, Pong
from btclib.p2p.limits import MAX_INV_SZ, MAX_PROTOCOL_MESSAGE_LENGTH
from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.download import MAX_BLOCKS_PER_GETDATA_BURST
from btclib_node.p2p import connection as connection_module
from btclib_node.p2p.address import peer_address
from btclib_node.p2p.callbacks import (
    MAX_CFILTERS_INFLIGHT_BYTES,
    MAX_GETDATA_INFLIGHT_BYTES,
    getdata,
    pong,
)
from btclib_node.p2p.connection import Connection
from btclib_node.p2p.filter_size import ONE_BUSY_MODERN_BLOCK_FILTER_BYTES
from tests import log_recorder

if TYPE_CHECKING:
    from btclib.p2p.payload import Payload

    from btclib_node.p2p.manager import P2pManager


class Unserializable:
    """A payload standing in for one this node has built but cannot send."""

    command = "unserializable"

    def serialize(self, *, check_validity: bool = True) -> bytes:
        """Raise, the way a payload that cannot serialize itself would."""
        raise ValueError("no")


def a_connection(
    client: socket.socket | None = None,
) -> tuple[Connection, list[str]]:
    """Build a `Connection` over `client`, or a fresh unconnected socket.

    Returns the connection alongside the list its own logger's
    `warning` calls are recorded into, since several tests here check
    what got logged rather than only what the connection did.
    """
    logged, warning = log_recorder()
    node = SimpleNamespace(
        chain=RegTest(),
        config=SimpleNamespace(pruned=False),
        logger=SimpleNamespace(
            warning=warning, info=lambda *a: None, debug=lambda *a: None
        ),
    )
    manager = SimpleNamespace(
        node=node,
        loop=None,
        peer_db=None,
        messages=deque(),
        handshake_messages=deque(),
    )
    connection = Connection(
        cast("P2pManager", manager),
        client if client is not None else socket.socket(),
        peer_address("1.2.3.4", 18444),
        0,
        inbound=False,
    )
    return connection, logged


def test_a_message_that_will_not_serialize_is_logged_and_dropped() -> None:
    """A message that fails to serialize is logged, and the connection stays up.

    The connection stays up: one message this node cannot build is not
    a reason to drop a peer that has done nothing wrong.
    """
    connection, logged = a_connection()
    sent: list[bytes] = []

    async def _send(data: bytes) -> None:
        # never reached: the message fails to serialize before _send
        # is called, which is the whole point of this test
        sent.append(data)  # pragma: no cover

    connection._send = _send  # type: ignore[method-assign]
    with connection.client:
        asyncio.run(connection.async_send(cast("Payload", Unserializable())))
    assert not sent
    (line,) = logged
    assert "error in serializing message" in line


def test_send_version_records_this_connections_own_nonce() -> None:
    """`send_version` sets `self.nonce` to what it drew, on either side.

    #448: `callbacks.version` reads this connection's own nonce back off
    it rather than off a manager-wide ring, so this is the whole of what
    `send_version` owes it, whether the connection is outbound or in.
    """
    connection, _ = a_connection()
    manager = cast("Any", connection.manager)
    manager.pending_outbound_nonces = set()
    manager.add_pending_outbound_nonce = manager.pending_outbound_nonces.add
    manager.port = 18444

    async def _send(data: bytes) -> None:
        return

    connection._send = _send  # type: ignore[method-assign]

    with connection.client:
        asyncio.run(connection.send_version())
    drawn = connection.nonce
    assert drawn is not None
    assert manager.pending_outbound_nonces == {drawn}


def test_send_version_only_adds_an_outbound_nonce_to_the_manager() -> None:
    """An inbound connection's own nonce never enters `pending_outbound_nonces`.

    `P2pManager.is_self_connect_nonce`'s own docstring is where that set
    is argued against Core's search -- an inbound connection's own draw
    has to stay out of it for the same reason.
    """
    connection, _ = a_connection()
    connection.inbound = True
    manager = cast("Any", connection.manager)
    manager.pending_outbound_nonces = set()
    manager.add_pending_outbound_nonce = manager.pending_outbound_nonces.add
    manager.port = 18444

    async def _send(data: bytes) -> None:
        return

    connection._send = _send  # type: ignore[method-assign]

    with connection.client:
        asyncio.run(connection.send_version())
    assert connection.nonce is not None
    assert not manager.pending_outbound_nonces


def test_send_version_announces_the_name_and_the_installed_version() -> None:
    """The `version` on the wire carries `/btclib:<installed version>/`.

    Read back off the framed octets `_send` is handed, not off
    `_USER_AGENT`: what #580 reported is what a peer received, and a
    constant asserted against itself answers for nothing between the
    two.
    """
    connection, _ = a_connection()
    manager = cast("Any", connection.manager)
    manager.pending_outbound_nonces = set()
    manager.add_pending_outbound_nonce = manager.pending_outbound_nonces.add
    manager.port = 18444
    sent: list[bytes] = []

    async def _send(data: bytes) -> None:
        sent.append(data)

    connection._send = _send  # type: ignore[method-assign]

    with connection.client:
        asyncio.run(connection.send_version())

    (framed,) = sent
    user_agent = Version.parse(Message.parse(framed).payload).user_agent
    assert user_agent == f"/btclib:{version('btclib-node')}/".encode()


def test_send_version_advertises_node_network_when_not_pruned() -> None:
    """An unpruned node's `version` carries `NODE_NETWORK`, among the rest."""
    connection, _ = a_connection()
    manager = cast("Any", connection.manager)
    manager.pending_outbound_nonces = set()
    manager.add_pending_outbound_nonce = manager.pending_outbound_nonces.add
    manager.port = 18444
    sent: list[bytes] = []

    async def _send(data: bytes) -> None:
        sent.append(data)

    connection._send = _send  # type: ignore[method-assign]

    with connection.client:
        asyncio.run(connection.send_version())

    (framed,) = sent
    services = Version.parse(Message.parse(framed).payload).services
    assert services & ServiceFlags.NODE_NETWORK
    assert services & ServiceFlags.NODE_NETWORK_LIMITED
    assert services & ServiceFlags.NODE_WITNESS


def test_send_version_drops_node_network_when_pruned() -> None:
    """A pruned node's `version` keeps `NODE_NETWORK_LIMITED`, drops `NODE_NETWORK`.

    Core's own `g_local_services` (`src/init.cpp`, at
    bitcoin/bitcoin@ca7162cde5): `NODE_NETWORK_LIMITED | NODE_WITNESS` from
    the start, `NODE_NETWORK` added only where `!fPruneMode`.
    """
    connection, _ = a_connection()
    connection.manager.node.config.pruned = True
    manager = cast("Any", connection.manager)
    manager.pending_outbound_nonces = set()
    manager.add_pending_outbound_nonce = manager.pending_outbound_nonces.add
    manager.port = 18444
    sent: list[bytes] = []

    async def _send(data: bytes) -> None:
        sent.append(data)

    connection._send = _send  # type: ignore[method-assign]

    with connection.client:
        asyncio.run(connection.send_version())

    (framed,) = sent
    services = Version.parse(Message.parse(framed).payload).services
    assert not services & ServiceFlags.NODE_NETWORK
    assert services & ServiceFlags.NODE_NETWORK_LIMITED
    assert services & ServiceFlags.NODE_WITNESS


def test_a_connection_names_the_peer_it_is_to() -> None:
    """`repr(connection)` names the real peer address a live socket has."""
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
    """`repr(connection)` brackets an IPv6 host, mapped or not, like Core does.

    `getpeername` is faked rather than a real socket bound to either
    address, since what is under test is `ip_and_port`'s own
    formatting and not the ability to actually reach these hosts.
    """
    client = cast("socket.socket", SimpleNamespace(getpeername=lambda: (host, 18444)))
    connection, _ = a_connection(client)
    assert repr(connection) == f"Connection to {endpoint}"


def test_a_connection_whose_socket_is_gone_says_so() -> None:
    """`repr` on a connection whose socket already closed does not raise.

    `getpeername` on a closed socket raises `OSError`; `__repr__`
    catches it and answers "Broken connection" instead.
    """
    client = socket.socket()
    client.close()
    connection, _ = a_connection(client)
    assert repr(connection) == "Broken connection"


def a_running_connection(
    loop: asyncio.AbstractEventLoop, client: socket.socket
) -> Connection:
    """Build a `Connection` with enough manager state for `run` to actually run.

    Unlike `a_connection` above, this one carries a real loop, a
    `pending_outbound_nonces` set, a stub `discourage`, and the two
    queues `parse_messages` routes a completed message onto, which is
    what the tests below need to drive `Connection.run` end to end
    rather than only a synchronous method on an idle connection.
    """
    node = SimpleNamespace(
        chain=RegTest(),
        status=NodeStatus.Starting,
        config=SimpleNamespace(pruned=False),
        logger=SimpleNamespace(
            warning=lambda *a: None, info=lambda *a: None, debug=lambda *a: None
        ),
    )
    discouraged: list[object] = []
    pending_outbound_nonces: set[int] = set()
    manager = SimpleNamespace(
        node=node,
        loop=loop,
        pending_outbound_nonces=pending_outbound_nonces,
        add_pending_outbound_nonce=pending_outbound_nonces.add,
        port=18444,
        peer_db=None,
        discourage=discouraged.append,
        discouraged=discouraged,
        messages=deque(),
        handshake_messages=deque(),
    )
    return Connection(
        cast("P2pManager", manager),
        client,
        peer_address("127.0.0.1", 18444),
        0,
        inbound=False,
    )


def discouraged_of(connection: Connection) -> list[Any]:
    """Read back the stub `discourage` list `a_running_connection` built.

    `connection.manager` is typed `P2pManager`, whose own `discouraged`
    is a `set[bytes]` -- the stub underneath is a `SimpleNamespace`
    carrying a `list` instead, so a caller comparing it against what was
    passed to `discourage` needs its own, unstatic view of the attribute.
    """
    return cast("list[Any]", cast("Any", connection.manager).discouraged)


def a_message_for_another_network() -> bytes:
    """Build a `verack` whose magic names a network that is not regtest.

    Well formed in every other way: the checksum is right, so what is left to
    refuse it on is the network it announces.
    """
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
    """A peer whose own envelope this node refuses to parse is discouraged.

    Two ways an envelope can be refused before any payload is looked
    at -- a checksum matching no payload, and a magic naming another
    network -- and both are #283's own case for discouraging: the
    refusal is `Message.parse`'s own reading of what the peer sent,
    not a bug of this node's.
    """

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
    """A bug in this node's own parsing drops the peer, but is not its fault.

    #283: not every exception out of `parse_messages` is the peer's fault -- a
    `RuntimeError` is not one btclib raised over the octets it sent, the same
    distinction `p2p/main.py`'s own `except` draws.
    """

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
    """A connection already `Closed` before `run` still sends its version.

    Status set to `Closed` ahead of `run` stands in for a connection
    dropped between being accepted and being started; the peer still
    gets a `version` message -- `run` sends it before checking status
    -- and nothing is read back on this side.
    """

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
    """A closed socket read as empty drops the connection, not discouraged.

    `theirs.close()` before `run` reads is what makes `sock_recv`
    answer empty rather than blocking; the read loop's own task is left
    alone by `stop`, since cancelling it from inside would cancel the
    coroutine doing the cancelling. A peer that merely hung up has not
    broken the protocol, so #283 says this is not a discourage either.
    """

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


def test_a_reset_connection_is_dropped_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sock_recv` raising is the same hangup an empty read is, not a crash.

    `test_a_peer_that_hangs_up_is_dropped` above is the graceful half --
    `theirs.close()` on a POSIX `socket.socketpair()`, a kernel-backed
    pipe that always answers a local close with an empty read. What it
    cannot reach on this platform is the other half `run`'s own `except
    OSError` answers: a peer whose reset reaches `sock_recv` as a raised
    `ConnectionResetError`/`ConnectionAbortedError` rather than an empty
    `b""` -- `socket.socketpair()`'s own Windows fallback, a real TCP
    loopback pair rather than a kernel pipe, answering exactly that way
    to an abrupt `close()` (btclib-org/btclib-node#430) -- so `sock_recv`
    is monkeypatched to raise it directly instead.
    """

    async def drive() -> tuple[Connection, bool]:
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        connection = a_running_connection(loop, ours)

        async def reset(sock: socket.socket, nbytes: int) -> bytes:
            raise ConnectionResetError

        monkeypatch.setattr(loop, "sock_recv", reset)
        task = asyncio.ensure_future(asyncio.sleep(60))
        connection.task = task  # type: ignore[assignment]
        await connection.run()
        left_alone = not task.cancelling()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        theirs.close()
        return connection, left_alone

    connection, left_alone = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Closed
    assert left_alone
    # a reset connection is not one that broke the protocol either: #283
    assert not discouraged_of(connection)


def test_a_connections_own_task_cancelled_directly_still_closes_its_socket() -> None:
    """A cancelled `run` task still closes `self.client` on the way out.

    #312: `P2pManager.stop()` closes every connection it already knows about
    through `Connection.stop()` -- but its own final sweep, over
    `asyncio.all_tasks(self.loop)`, cancels whatever task is still pending there
    directly, the only reach it has left for a connection accepted or dialled
    after its dict-based sweep already ran. That direct `task.cancel()`, with
    nothing standing between it and this coroutine's own suspension in
    `sock_recv`, must not be able to leave `self.client` open the way going
    through `stop()` never does.
    """

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


def test_stop_from_another_thread_does_not_raise_past_a_registered_writer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`stop`, called off the loop's thread, does not crash a live reader.

    btclib-org/btclib-node#518: `self.client.close()` used to run
    directly on whichever thread called `stop`, and `stop`'s own comment
    names callers on both -- `handle_p2p`, `handle_p2p_handshake` and
    every callback that drops a peer for cause, on `Node`'s thread;
    `_prune_stale_connections` through `remove_connection`, on
    `P2pManager`'s, which is the loop's own. The race is therefore the
    ordering rather than the thread, and both halves of it are covered:
    this test calls `stop` from off the loop, the one after it from
    the loop's own thread. Closing the fd, while `run`'s own `sock_recv` still
    had a reader registered for it, raced `BaseSelectorEventLoop`'s
    bookkeeping: `_sock_read_done`'s later `remove_reader` found a writer
    also registered -- `Connection._send`'s own `sock_sendall`, for a
    peer not draining its send queue -- and took `_selector.modify`
    rather than `unregister`, and `modify` re-registers a fd that
    `KqueueSelector.unregister` alone would have tolerated closed.
    Reproduced here with a real second thread running the loop, the
    shape `P2pManager` itself runs in, and `stop` called from the thread
    running the test -- the caller not the loop's own. No custom exception
    handler is installed on the loop: the raise this guards against is
    from inside an asyncio callback, which the loop's own default
    handler logs rather than propagates, so `caplog` on that logger's
    own `ERROR` is what would have caught it, not an assertion on a
    value this test would otherwise have to fabricate a branch to reach.
    """
    caplog.set_level(logging.ERROR, logger="asyncio")
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    ours, theirs = socket.socketpair()
    ours.setblocking(False)
    try:
        connection = a_running_connection(loop, ours)
        connection.task = asyncio.run_coroutine_threadsafe(connection.run(), loop)
        # theirs never reads, so this fills ours's own send buffer and
        # leaves a writer registered on the same fd sock_recv reads from
        send = asyncio.run_coroutine_threadsafe(
            connection._send(b"x" * (16 * 1024 * 1024)), loop
        )
        time.sleep(0.15)
        connection.stop()
        time.sleep(0.15)
    finally:
        # cancels _send's own sock_sendall, still pending because theirs
        # never drained it; not awaited synchronously back on this
        # thread, so the loop stopping just after can still log its own
        # harmless "Task was destroyed but it is pending!" for it
        send.cancel()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
        theirs.close()

    assert connection.status == P2pConnStatus.Closed
    # a closed socket's own fileno is -1; still >= 0 is still open
    assert ours.fileno() == -1
    assert "Bad file descriptor" not in caplog.text


def test_stop_on_the_loop_s_own_thread_does_not_raise_past_a_registered_writer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same race, reached from the loop's own thread rather than off it.

    `_prune_stale_connections` drops an idle peer through
    `remove_connection`, which calls `conn.stop()`; `manage_connections`
    is a coroutine `P2pManager.run` schedules onto its own loop, so that
    whole path runs on the loop's thread. Nothing about the ordering
    changes there -- closing the fd before the reader is removed is what
    raced, not which thread did it -- so the fix is scheduling `_close`
    rather than which thread scheduled it, and this is the half the test
    above cannot reach: `call_soon_threadsafe` from off the loop is
    already how a call arrives from another thread, where here `stop`
    itself runs inside the loop and schedules onto the loop it is on.

    What this catches is the synchronous close, which is what `stop`
    did before the fix. Measured: removing `_close`'s own
    `remove_reader`/`remove_writer` while leaving the scheduling in
    place leaves this test green -- `_sock_read_done` gets its
    `remove_reader` in before the scheduled `_close` runs, so there is
    no closed fd for `modify` to re-register. The test above fails on
    that narrower mutation and this one does not, which is the whole
    reason both are here rather than either alone.
    """
    caplog.set_level(logging.ERROR, logger="asyncio")
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    ours, theirs = socket.socketpair()
    ours.setblocking(False)
    try:
        connection = a_running_connection(loop, ours)
        connection.task = asyncio.run_coroutine_threadsafe(connection.run(), loop)
        send = asyncio.run_coroutine_threadsafe(
            connection._send(b"x" * (16 * 1024 * 1024)), loop
        )
        time.sleep(0.15)
        # not connection.stop(): this runs stop itself on the loop's
        # thread, which is where _prune_stale_connections reaches it
        loop.call_soon_threadsafe(connection.stop)
        time.sleep(0.15)
    finally:
        send.cancel()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
        theirs.close()

    assert connection.status == P2pConnStatus.Closed
    assert ours.fileno() == -1
    assert "Bad file descriptor" not in caplog.text


def test_close_on_an_already_closed_socket_touches_neither_reader_nor_writer() -> None:
    """`_close`'s `fd != -1` guard: nothing is registered to remove twice.

    `stop`'s own idempotency (#360) can reach `_close` a second time for
    one connection -- through two racing callers, or through `run`'s own
    `finally` after `stop` already ran. `self.client.fileno()` on the
    second call is already -1, and `remove_reader`/`remove_writer` for
    that fd would be meaningless; the stub loop below raises if either
    is called, so this fails were the guard removed rather than merely
    left untested.
    """
    client = socket.socket()
    client.close()
    connection, _ = a_connection(client)

    def boom(_fd: int) -> bool:
        # never reached: the guard in _close is the point of this test
        raise AssertionError("no")  # pragma: no cover

    connection.loop = cast(
        "asyncio.AbstractEventLoop",
        SimpleNamespace(remove_reader=boom, remove_writer=boom),
    )
    connection._close()
    assert client.fileno() == -1


def test_close_still_closes_where_the_loop_has_no_reader_or_writer_to_remove() -> None:
    """`_close` reaches `self.client.close()` where `remove_reader` raises.

    Windows' own Proactor loop raises `NotImplementedError` from both
    `remove_reader` and `remove_writer` unconditionally, registration or
    none: `sock_recv`/`sock_sendall` never register either there, unlike
    on a selector loop, so this is not a registration `_close` has to
    reach the selector to undo, only a call this loop family answers by
    raising rather than by no-op (btclib-org/btclib-node#430). The stub
    loop below reproduces that shape on this platform, which does not
    carry it.
    """
    client = socket.socket()
    connection, _ = a_connection(client)

    def not_implemented(_fd: int) -> NoReturn:
        raise NotImplementedError

    connection.loop = cast(
        "asyncio.AbstractEventLoop",
        SimpleNamespace(remove_reader=not_implemented, remove_writer=not_implemented),
    )
    connection._close()
    assert client.fileno() == -1


def _wire_ping(nonce: int = 1) -> bytes:
    """One whole `ping` message's own wire octets, magic through checksum."""
    return Message(RegTest().magic, "ping", Ping(nonce).serialize()).serialize()


def _wire_verack() -> bytes:
    """One whole `verack` message's own wire octets."""
    return Message(RegTest().magic, "verack", Verack().serialize()).serialize()


def test_parse_messages_weighs_a_queued_message_against_the_recv_bound() -> None:
    """A `messages`-bound item adds its own wire size to `queued_recv_bytes`.

    Far under `MAX_QUEUED_RECV_BYTES`, so `_recv_resume` stays set: what
    this checks is the size accounting itself, the boundary tests below
    are what check the pause it feeds.
    """
    connection, _ = a_connection()
    with connection.client:
        wire = _wire_ping()
        connection.buffer += wire
        connection.parse_messages()
    (item,) = connection.manager.messages
    assert item == ("ping", Ping(1).serialize(), 0, len(wire))
    assert connection.queued_recv_bytes == len(wire)
    assert connection._recv_resume.is_set()


def test_parse_messages_weighs_a_handshake_message_too() -> None:
    """A `handshake_messages`-bound item adds its own wire size too.

    `handshake_messages` is still drained whole every pass of `Node`'s
    own loop (`_drain_message_queues`) rather than sharing `messages`'s
    own log2-scaled share, but shares this connection's own recv-bound
    pacing with it since btclib-org/btclib-node#482.
    """
    connection, _ = a_connection()
    with connection.client:
        wire = _wire_verack()
        connection.buffer += wire
        connection.parse_messages()
    (item,) = connection.manager.handshake_messages
    assert item == ("verack", Verack().serialize(), 0, len(wire))
    assert connection.queued_recv_bytes == len(wire)
    assert connection._recv_resume.is_set()


def test_parse_messages_clears_recv_resume_once_over_the_bound() -> None:
    """Crossing `MAX_QUEUED_RECV_BYTES` clears `_recv_resume`.

    Seeded one octet under the bound, so the incoming message's own
    size is what tips it over -- landing exactly on the bound, the
    complementary case below, is deliberately not this.
    """
    connection, _ = a_connection()
    with connection.client:
        wire = _wire_ping()
        connection.queued_recv_bytes = (
            connection_module.MAX_QUEUED_RECV_BYTES - len(wire) + 1
        )
        connection.buffer += wire
        connection.parse_messages()
    assert connection.queued_recv_bytes == connection_module.MAX_QUEUED_RECV_BYTES + 1
    assert not connection._recv_resume.is_set()


def test_parse_messages_leaves_recv_resume_set_landing_exactly_on_the_bound() -> None:
    """Landing exactly on `MAX_QUEUED_RECV_BYTES`, not over it, does not pause.

    `handle_p2p`'s own resume check (`p2p/main.py`) uses the same `<=`,
    so the two share this one boundary rather than each picking it
    independently.
    """
    connection, _ = a_connection()
    with connection.client:
        wire = _wire_ping()
        connection.queued_recv_bytes = connection_module.MAX_QUEUED_RECV_BYTES - len(
            wire
        )
        connection.buffer += wire
        connection.parse_messages()
    assert connection.queued_recv_bytes == connection_module.MAX_QUEUED_RECV_BYTES
    assert connection._recv_resume.is_set()


def test_run_does_not_read_again_while_recv_resume_is_cleared() -> None:
    """`run`'s own read loop honours `_recv_resume`, not only who clears it.

    `sock_recv` is patched rather than driven over a real socket, so
    this is a check on `run`'s own loop structure -- the `await` gating
    its next read -- and not on how quickly a real read becomes ready;
    the `parse_messages` tests above are what cover `_recv_resume`
    actually being cleared and set.
    """

    async def drive() -> tuple[list[int], list[int]]:
        loop = asyncio.get_running_loop()
        connection = a_running_connection(loop, socket.socket())
        connection._recv_resume.clear()
        calls: list[int] = []
        read = asyncio.Event()

        async def fake_sock_recv(_client: socket.socket, size: int) -> bytes:
            calls.append(size)
            read.set()
            return b""  # an empty read: run's own hang-up path, ending the loop

        connection.loop.sock_recv = fake_sock_recv  # type: ignore[method-assign,assignment]
        task = asyncio.ensure_future(connection.run())
        # two turns: past send_version's own await and parked on
        # `_recv_resume.wait()`, the same shape the cancellation test
        # above uses to park a connection in `sock_recv` instead
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        before = list(calls)
        connection._recv_resume.set()
        await asyncio.wait_for(read.wait(), timeout=5)
        await task
        return before, calls

    before, after = asyncio.run(drive())
    assert not before
    assert after == [65536]


def test_a_peer_already_at_the_send_bound_is_dropped_not_queued_further() -> None:
    """A send that would exceed `MAX_QUEUED_SEND_BYTES` is refused, not queued.

    `queued_send_bytes` starts already at the bound, whatever filled it,
    so one more message is refused regardless of its own size; this
    node's own choice under load and not the peer's doing, so #283 says
    it is not a discourage.
    """

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
    """Measure the wire octets a send adds on top of one `filter_bytes`.

    `Message`'s envelope, plus `CFilter`'s own filter type, block hash,
    and `filter_bytes`'s own `var_int` length prefix.

    Measured, at a size in the range the two bursts below build,
    rather than assumed: `var_int` is five octets only above 65,536,
    which is a fact about the encoding and not about this module, so it
    is read off a real `Message` built the way `Connection._queue`
    builds one instead of counted on to stay what it was last measured
    as.
    """
    size = 98_000
    payload = CFilter(
        BlockFilterType.BASIC, b"\x00" * 32, b"\x00" * size, check_validity=False
    ).serialize(check_validity=False)
    envelope = Message(RegTest().magic, "cfilter", payload).serialize()
    return len(envelope) - size


def _burst_summing_to(total_wire_bytes: int, count: int) -> list[CFilter]:
    """Build `count` `CFilter`-shaped messages summing to `total_wire_bytes`.

    What `Connection._queue` actually counts toward `queued_send_bytes`
    is the whole wire message, not merely the `filter_bytes` argument
    each is built from.
    `CFilter` is a convenient, arbitrarily-sized payload to stand in for
    whatever a connection has queued -- a `getdata` answer's own blocks
    among them, since `count` here models how many separate messages one
    handler schedules back to back, not that they are filters: no message
    here could be one whole answer's size anyway, `Message.serialize`
    refusing a payload over `MAX_PROTOCOL_MESSAGE_LENGTH` regardless of
    `check_validity`. `filter_bytes` is not a real Golomb-Rice set:
    `check_validity=False` on both the object and `_queue`'s own
    `serialize` call is what lets zeroed octets stand in for one, the way
    a wrong-shaped block already does elsewhere in this test tree.
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
    """Put two bursts of messages in flight on the same connection together.

    Between them the two bursts stand for whatever one connection has
    been committed to sending, in two handlers' worth rather than one:
    the sizes are the caller's to pick, and what is measured is
    `Connection._queue`'s own comparison against `MAX_QUEUED_SEND_BYTES`
    rather than any dispatch's own schedule. The first burst is held
    open on a socket write that never finishes, the way a real one would
    be by a peer reading slower than this node can serialize. Returns
    the connection and the sizes `_send` actually saw.
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
        # synchronous prefix -- the bound check and the reservation,
        # under `_send_lock` -- before any of them reaches the
        # (contended, after the first) `_write_lock`
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


def _bursts_summing_to_the_bound(slack: int) -> tuple[list[CFilter], list[CFilter]]:
    """Build two bursts summing to `MAX_QUEUED_SEND_BYTES` less `slack`.

    A total, not a schedule: what these two drive is
    `Connection._queue`'s own comparison at its own boundary, and
    nothing here claims either pacing mechanism reaches this much. What
    they do reach is
    `test_filters_in_flight_come_out_of_a_getdata_answers_own_room` and
    `test_a_realistic_getdata_burst_and_cfilters_headroom_are_not_dropped`
    below, each driving the real dispatch instead of assuming a total.
    """
    first_share = MAX_GETDATA_INFLIGHT_BYTES + MAX_PROTOCOL_MESSAGE_LENGTH
    total = connection_module.MAX_QUEUED_SEND_BYTES - slack
    # slack always comes out of the second share, never the first, so a
    # caller's own slack has to stay well under what the bound leaves
    # above `first_share` or that share goes negative
    return (
        _burst_summing_to(first_share, 3),
        _burst_summing_to(total - first_share, 3),
    )


def test_a_total_at_the_send_bound_is_not_dropped() -> None:
    """`MAX_QUEUED_SEND_BYTES` holds a total landing just short of it.

    A boundary check on `Connection._queue`'s own comparison, and only
    that: what either pacing mechanism actually schedules is what the
    tests below drive.
    """
    first, second = _bursts_summing_to_the_bound(slack=4096)
    connection, delivered = _two_bursts_in_flight(first, second)
    assert connection.status == P2pConnStatus.Open
    assert len(delivered) == len(first) + len(second)
    assert connection.queued_send_bytes == 0


def test_past_the_send_bound_the_peer_is_dropped() -> None:
    """Past the bound above, the peer is dropped.

    The same two bursts, tipping past `MAX_QUEUED_SEND_BYTES` instead of
    stopping short of it.
    """
    first, second = _bursts_summing_to_the_bound(slack=-4096)
    connection, delivered = _two_bursts_in_flight(first, second)
    assert connection.status == P2pConnStatus.Closed
    # the first burst reached the socket in full; at least one message of
    # the second, the one that tipped the bound, never did
    assert len(delivered) < len(first) + len(second)
    assert len(delivered) >= len(first)
    # released and accounted for, not left on the books by the drop
    assert connection.queued_send_bytes == 0


class _FakeBigBlock:
    """A `Block`-shaped stand-in whose serialized size is exactly `size`.

    `advance_getdata` (`callbacks.py`) only ever calls `.serialize` on
    what `node.block_db` hands it, through `BlockPayload`'s own
    `check_validity=False` path all the way to `Connection._queue`'s
    own `payload.serialize` call -- so a synthetic size stands in for a
    real block's the same way `_burst_summing_to` above already stands a
    `CFilter` in for whatever a connection has queued.
    """

    def __init__(self, size: int) -> None:
        self.size = size

    def serialize(self, *_args: object, check_validity: bool = True) -> bytes:
        """Ignore whatever `BlockPayload.serialize` passes, padded to `size`."""
        return b"\x00" * self.size


def test_a_realistic_getdata_burst_and_cfilters_headroom_are_not_dropped() -> None:
    """`getdata`'s own real schedule and `get_cfilters`'s own headroom survive.

    Six 1.5 MB blocks in one `getdata` -- comfortably inside
    `MAX_BLOCKS_IN_TRANSIT_PER_PEER` (sixteen) and each well under
    `MAX_PROTOCOL_MESSAGE_LENGTH` -- driven through the real
    `getdata`/`advance_getdata` dispatch rather than hand-summed to an
    assumed total: that assumption is exactly what let
    `MAX_QUEUED_SEND_BYTES` under-size itself once already
    (btclib-org/btclib-node#470), `advance_getdata`'s check-before-send
    shape scheduling whatever fits *before* the item that tips its own
    pacing bound, not a total capped at that bound. The filters behind
    it are handed to `Connection.async_send` rather than to
    `advance_cfilters`, which at this much already queued pauses on its
    own first check instead: what is measured here is the total this
    connection carries, where the displacement that pause produces is
    what `test_filters_in_flight_come_out_of_a_getdata_answers_own_room`
    below drives.
    """
    size = 1_500_000
    blocks = {bytes([i]) + b"\x00" * 31: _FakeBigBlock(size) for i in range(6)}
    items = [Inventory(InventoryType.MSG_BLOCK, h) for h in blocks]
    cfilters_headroom = _burst_summing_to(
        int(2 * ONE_BUSY_MODERN_BLOCK_FILTER_BYTES), 2
    )

    async def drive() -> tuple[Connection, list[int]]:
        loop = asyncio.get_running_loop()
        connection = a_running_connection(loop, socket.socket())
        connection.node.block_db = SimpleNamespace(get_block=blocks.get)  # type: ignore[assignment]
        connection.node.mempool = SimpleNamespace(get_tx=lambda *a, **k: None)  # type: ignore[assignment]
        connection.node.pending_getdata = {}
        release = asyncio.Event()
        delivered: list[int] = []

        async def _send(data: bytes) -> None:
            delivered.append(len(data))
            await release.wait()

        connection._send = _send  # type: ignore[method-assign]

        getdata(connection.node, GetData(items).serialize(), connection)
        # every block reserved its own share on this thread, before any
        # turn of the loop and so before any of them drained
        assert connection.queued_send_bytes == pytest.approx(
            len(blocks) * size, rel=0.01
        )
        # Connection.send schedules the write through
        # run_coroutine_threadsafe rather than starting its task's own
        # first step immediately the way a direct ensure_future in this
        # coroutine's own frame would, so it takes several turns for
        # every block to reach `release`
        for _ in range(50):
            await asyncio.sleep(0)

        second_tasks = [
            asyncio.ensure_future(connection.async_send(f)) for f in cfilters_headroom
        ]
        await asyncio.sleep(0)

        release.set()
        await asyncio.gather(*second_tasks)
        connection.client.close()
        return connection, delivered

    connection, delivered = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Open
    assert len(delivered) == len(items) + len(cfilters_headroom)
    assert connection.queued_send_bytes == 0


def _blocks_served_behind(in_flight_bytes: int) -> tuple[int, int]:
    """Answer a `getdata` for small blocks with `in_flight_bytes` already owed.

    Returns the peak `queued_send_bytes` the answer reached and how many
    of the blocks it served before `MAX_GETDATA_INFLIGHT_BYTES` paused
    it. The blocks are sized so that a filter answer's own peak is the
    same order as one of them, which is what makes the displacement
    below visible at all.
    """
    size = 300_000
    blocks = {bytes([i]) + b"\x00" * 31: _FakeBigBlock(size) for i in range(40)}
    items = [Inventory(InventoryType.MSG_BLOCK, h) for h in blocks]

    async def drive() -> tuple[int, int]:
        loop = asyncio.get_running_loop()
        connection = a_running_connection(loop, socket.socket())
        connection.node.block_db = SimpleNamespace(get_block=blocks.get)  # type: ignore[assignment]
        connection.node.mempool = SimpleNamespace(get_tx=lambda *a, **k: None)  # type: ignore[assignment]
        connection.node.pending_getdata = {}
        release = asyncio.Event()

        async def _send(data: bytes) -> None:
            await release.wait()

        connection._send = _send  # type: ignore[method-assign]

        tasks = []
        if in_flight_bytes:
            tasks = [
                asyncio.ensure_future(connection.async_send(f))
                for f in _burst_summing_to(in_flight_bytes, 2)
            ]
            await asyncio.sleep(0)

        getdata(connection.node, GetData(items).serialize(), connection)
        peak = connection.queued_send_bytes
        assert connection.status == P2pConnStatus.Open
        _conn, remaining = connection.node.pending_getdata[connection.id]

        release.set()
        await asyncio.gather(*tasks)
        connection.client.close()
        return peak, len(items) - len(remaining)

    return asyncio.run(drive())


def test_filters_in_flight_come_out_of_a_getdata_answers_own_room() -> None:
    """A filter answer already owed displaces blocks rather than adding to them.

    `advance_getdata` and `advance_cfilters` (`callbacks.py`) pace on the
    one `queued_send_bytes` field, so a `getcfilters` pipelined behind a
    `getdata` the peer has not drained is counted inside that answer's
    own peak: the same request is served fewer blocks, and the total the
    connection carries does not grow. That is what
    `MAX_QUEUED_SEND_BYTES` is sized against, rather than the two
    overshoots added together (btclib-org/btclib-node#521).
    """
    alone_peak, alone_served = _blocks_served_behind(0)
    behind_peak, behind_served = _blocks_served_behind(
        MAX_CFILTERS_INFLIGHT_BYTES + int(ONE_BUSY_MODERN_BLOCK_FILTER_BYTES)
    )
    assert behind_served < alone_served
    assert behind_peak <= alone_peak
    assert behind_peak < connection_module.MAX_QUEUED_SEND_BYTES


def test_the_send_bound_holds_the_peak_a_getdata_answer_can_pace_to() -> None:
    """The bound stays above `MAX_GETDATA_INFLIGHT_BYTES` and one whole block.

    The relation the bound is derived from: `advance_getdata`'s last
    check passes just under its own bound, and what it then commits is a
    whole wire message rather than the payload
    `MAX_PROTOCOL_MESSAGE_LENGTH` bounds. A change to either constant
    that left this false would have this bound drop the peer the pacing
    bound had already paused. The envelope is measured off a
    `Message` built the way `Connection._queue` builds one rather than
    counted on to stay what it was.
    """
    envelope = len(Message(RegTest().magic, "block", b"").serialize())
    peak = MAX_GETDATA_INFLIGHT_BYTES + MAX_PROTOCOL_MESSAGE_LENGTH + envelope
    assert peak < connection_module.MAX_QUEUED_SEND_BYTES


def test_send_counts_a_message_before_the_loop_has_written_it() -> None:
    """`Connection.send` has counted the message by the time it returns.

    The whole of what a caller pacing on `queued_send_bytes` between two
    sends depends on: the count is taken on the calling thread, so it is
    true of the connection before any turn of the loop the write is
    scheduled on. btclib-org/btclib-node#512
    """

    async def drive() -> tuple[int, int, list[int]]:
        loop = asyncio.get_running_loop()
        connection = a_running_connection(loop, socket.socket())
        delivered: list[int] = []

        async def _send(data: bytes) -> None:
            delivered.append(len(data))

        connection._send = _send  # type: ignore[method-assign]

        connection.send(Ping(1))
        reserved = connection.queued_send_bytes
        # the write itself is the loop's, and takes turns of it to
        # happen: run_coroutine_threadsafe schedules rather than starts
        for _ in range(10):
            await asyncio.sleep(0)
        connection.client.close()
        return reserved, connection.queued_send_bytes, delivered

    reserved, after, delivered = asyncio.run(drive())
    (written,) = delivered
    assert reserved == written
    assert after == 0


def test_a_getdata_answer_paces_on_what_it_has_already_handed_over() -> None:
    """A `getdata` for more blocks than fit is paced, not dropped.

    A whole `MAX_BLOCKS_PER_GETDATA_BURST` (`btclib_node/download.py`)
    of megabyte blocks, which is the request this node makes of its own
    peers and so an ordinary one to answer. The peer drains nothing, so
    `advance_getdata` stops within one block of
    `MAX_GETDATA_INFLIGHT_BYTES` -- the overshoot
    `MAX_QUEUED_SEND_BYTES`'s own room above that bound is sized for --
    and leaves the rest on `node.pending_getdata` for
    `p2p.main.resume_getdata`, which is a peer paced rather than a peer
    dropped. btclib-org/btclib-node#512
    """
    size = 1_000_000
    blocks = {
        bytes([i]) + b"\x00" * 31: _FakeBigBlock(size)
        for i in range(MAX_BLOCKS_PER_GETDATA_BURST)
    }
    items = [Inventory(InventoryType.MSG_BLOCK, h) for h in blocks]

    async def drive() -> tuple[Connection, int, int]:
        loop = asyncio.get_running_loop()
        connection = a_running_connection(loop, socket.socket())
        connection.node.block_db = SimpleNamespace(get_block=blocks.get)  # type: ignore[assignment]
        connection.node.mempool = SimpleNamespace(get_tx=lambda *a, **k: None)  # type: ignore[assignment]
        connection.node.pending_getdata = {}
        release = asyncio.Event()
        delivered: list[int] = []

        async def _send(data: bytes) -> None:
            delivered.append(len(data))
            await release.wait()

        connection._send = _send  # type: ignore[method-assign]

        getdata(connection.node, GetData(items).serialize(), connection)
        # the whole answer this call committed to, before any turn of
        # the loop has written a byte of it
        peak = connection.queued_send_bytes
        release.set()
        for _ in range(50):
            await asyncio.sleep(0)
        connection.client.close()
        return connection, peak, delivered[0]

    connection, peak, one_message = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Open
    # one message past the bound and no further, measured off a message
    # this answer actually queued rather than assumed from `size`
    assert peak < MAX_GETDATA_INFLIGHT_BYTES + one_message
    # every block here serializes to the same length, so what the peak
    # holds says how many of them were served
    _conn, pending = connection.node.pending_getdata[connection.id]
    assert list(pending) == items[peak // one_message :]
    assert connection.queued_send_bytes == 0


def test_a_getdata_of_mostly_misses_does_not_drop_the_connection() -> None:
    """A `getdata` answer's own trailing `notfound` is paced too.

    One request, `MAX_INV_SZ` items, all but three a transaction this
    node no longer has -- a peer whose asks raced an eviction round, not
    a protocol violation -- and the last three blocks just under
    `MAX_PROTOCOL_MESSAGE_LENGTH`. Before btclib-org/btclib-node#529 a
    miss cost nothing against `MAX_GETDATA_INFLIGHT_BYTES`, so this
    request's whole `notfound` landed on top of the three blocks and
    past `MAX_QUEUED_SEND_BYTES`, dropping a connection the pacing bound
    above it had already handled for the blocks alone.
    """
    block_size = MAX_PROTOCOL_MESSAGE_LENGTH - 100
    blocks = {bytes([i]) + b"\x00" * 31: _FakeBigBlock(block_size) for i in range(3)}
    misses = [
        Inventory(InventoryType.MSG_WTX, i.to_bytes(4, "big") + b"\x01" * 28)
        for i in range(MAX_INV_SZ - len(blocks))
    ]
    items = misses + [Inventory(InventoryType.MSG_BLOCK, h) for h in blocks]

    async def drive() -> tuple[Connection, int]:
        loop = asyncio.get_running_loop()
        connection = a_running_connection(loop, socket.socket())
        connection.node.block_db = SimpleNamespace(get_block=blocks.get)  # type: ignore[assignment]
        connection.node.mempool = SimpleNamespace(get_tx=lambda *a, **k: None)  # type: ignore[assignment]
        connection.node.pending_getdata = {}
        release = asyncio.Event()

        async def _send(data: bytes) -> None:
            await release.wait()

        connection._send = _send  # type: ignore[method-assign]

        getdata(connection.node, GetData(items).serialize(), connection)
        peak = connection.queued_send_bytes
        release.set()
        for _ in range(50):
            await asyncio.sleep(0)
        connection.client.close()
        return connection, peak

    connection, peak = asyncio.run(drive())
    assert connection.status == P2pConnStatus.Open
    assert peak < connection_module.MAX_QUEUED_SEND_BYTES
    # the request was not served in full in this one call: at least the
    # last block, popped only after the accumulated misses were flushed
    # and the bound tripped, is left for `p2p.main.resume_getdata`
    assert connection.id in connection.node.pending_getdata


def test_send_ping_racing_pong_does_not_tear_the_ping_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#357's second interleaving: a `send_ping` racing `pong`'s own clear.

    `callbacks.pong` clears `ping_sent` then `ping_nonce` as two
    statements, and `Connection.send_ping` writes both as two
    statements too. Unlocked, a `send_ping` slipped in between `pong`'s
    two clears would have its own fresh nonce overwritten by `pong`'s
    second write, leaving a real outstanding ping under
    `ping_nonce == 0` -- the sentinel `send_ping`'s own comment is
    careful never to send -- and the peer discouraged and dropped for a
    protocol violation this node caused.

    Driven deterministically by hooking the exact write `pong` makes
    first, `ping_sent = 0`: a real second thread runs `send_ping` from
    inside that write, and is given only until it is blocked on
    `_ping_lock` -- the same lock `send_ping` takes -- before `pong`'s
    own second write runs. Unlocked, that second thread would already
    have finished writing by the time this test could look.
    """
    connection, _ = a_connection()
    connection.send = lambda msg: None  # type: ignore[method-assign]
    original_nonce = 111
    connection.ping_nonce = original_nonce

    other_thread_about_to_block = threading.Event()
    hook_armed = [False]

    def get_ping_sent(self: Connection) -> float:
        return cast("float", self.__dict__.get("_ping_sent_value", 0))

    def set_ping_sent(self: Connection, value: float) -> None:
        self.__dict__["_ping_sent_value"] = value
        if hook_armed[0] and value == 0:
            hook_armed[0] = False
            send_ping_thread.start()
            # A bound only against a hang: unlocked, `send_ping` runs to
            # completion at once and this event is never set.
            other_thread_about_to_block.wait(timeout=5)

    monkeypatch.setattr(
        Connection,
        "ping_sent",
        property(get_ping_sent, set_ping_sent),
        raising=False,
    )
    connection.ping_sent = time.time() - 1  # a ping already outstanding

    real_lock = connection._ping_lock

    class SignallingLock:
        def __enter__(self) -> None:
            if threading.current_thread() is not threading.main_thread():
                other_thread_about_to_block.set()
            real_lock.acquire()

        def __exit__(self, *exc_info: object) -> None:
            real_lock.release()

    connection._ping_lock = cast("Any", SignallingLock())
    send_ping_thread = threading.Thread(target=connection.send_ping)
    hook_armed[0] = True

    discouraged: list[Any] = []
    node = SimpleNamespace(p2p_manager=SimpleNamespace(discourage=discouraged.append))
    pong(cast("Any", node), Pong(original_nonce).serialize(), connection)

    send_ping_thread.join(timeout=5)
    connection.client.close()
    assert not send_ping_thread.is_alive()
    assert not discouraged
    # send_ping's own fresh pair survives, rather than carrying the
    # sentinel pong's own second write would otherwise have left behind
    assert connection.ping_sent != 0
    assert connection.ping_nonce not in (0, original_nonce)
