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
import threading
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.hashes import hash256
from btclib.p2p.block_filters import BlockFilterType, CFilter
from btclib.p2p.keepalive import Ping, Pong
from btclib.p2p.limits import MAX_PROTOCOL_MESSAGE_LENGTH
from btclib.p2p.message import Message

from btclib_node.chains import RegTest
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.p2p import connection as connection_module
from btclib_node.p2p.address import peer_address
from btclib_node.p2p.callbacks import pong
from btclib_node.p2p.connection import Connection
from btclib_node.p2p.filter_size import ONE_BUSY_MODERN_BLOCK_FILTER_BYTES
from tests.helpers import log_recorder

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
        logger=SimpleNamespace(
            warning=warning, info=lambda *a: None, debug=lambda *a: None
        ),
    )
    manager = SimpleNamespace(node=node, loop=None, peer_db=None)
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


def test_send_version_keeps_the_most_recently_sent_nonces() -> None:
    """`manager.nonces` is a ring of the ten most recent, not the ten oldest.

    #433: appending to the list and then slicing `[:10]` kept the first
    ten nonces this process ever drew, so past the tenth `send_version`
    every new nonce was appended and immediately discarded by that same
    slice -- the ring never moved again, and `callbacks.version`'s
    self-connection check compared against connections long gone. Past
    eleven calls, the ring must be the last ten appends in order and
    must no longer hold the first nonce. `sent` records the ring's own
    last entry after each call rather than the nonce `send_version`
    drew, which there is no other way to observe -- under the defect
    that makes `sent` repeat its tenth entry, and the equality below
    fails on that repetition as much as on the eviction that never
    happened.
    """
    connection, _ = a_connection()
    manager = cast("Any", connection.manager)
    manager.nonces = []
    manager.port = 18444

    async def _send(data: bytes) -> None:
        return

    connection._send = _send  # type: ignore[method-assign]

    async def drive() -> list[int]:
        sent = []
        for _ in range(11):
            await connection.send_version()
            sent.append(manager.nonces[-1])
        return sent

    with connection.client:
        sent = asyncio.run(drive())
    assert manager.nonces == sent[-10:]
    assert sent[0] not in manager.nonces


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
    `nonces` list and a stub `discourage`, which is what the tests
    below need to drive `Connection.run` end to end rather than only
    a synchronous method on an idle connection.
    """
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
    """Measure the wire octets `async_send` adds on top of one `filter_bytes`.

    `Message`'s envelope, plus `CFilter`'s own filter type, block hash,
    and `filter_bytes`'s own `var_int` length prefix.

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
    """Build `count` `CFilter`-shaped messages summing to `total_wire_bytes`.

    What `async_send` actually counts toward `queued_send_bytes` is the whole
    wire message, not merely the `filter_bytes` argument each is built from.
    `CFilter` is a convenient, arbitrarily-sized payload to stand in for
    whatever a connection has queued -- a `getdata` answer's own blocks
    among them, since `count` here models how many separate messages one
    handler schedules back to back, not that they are filters: no message
    here could be one whole answer's size anyway, `Message.serialize`
    refusing a payload over `MAX_PROTOCOL_MESSAGE_LENGTH` regardless of
    `check_validity`. `filter_bytes` is not a real Golomb-Rice set:
    `check_validity=False` on both the object and `async_send`'s own
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

    Each burst is scheduled the way a handler that calls `Connection.send`
    several times in a row without anything else running in between
    schedules its own messages -- `callbacks.getdata`'s own loop over a
    `GetData`'s items, unpaced (btclib-org/btclib-node#470), rather than
    `get_cfilters`'s, which paces itself since #442 and so no longer
    produces a burst this large in one call. The first burst is held open
    on a socket write that never finishes, the way a real one would be by
    a peer reading slower than this node can serialize. Returns the
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


def _getdata_burst_and_cfilters_headroom(
    slack: int,
) -> tuple[list[CFilter], list[CFilter]]:
    """Build a legitimate `getdata` burst and `get_cfilters`'s own headroom.

    `MAX_QUEUED_SEND_BYTES` (`connection.py`) is derived from exactly these
    two terms -- `_MAX_BLOCKS_PER_GETDATA_BURST` messages at
    `MAX_PROTOCOL_MESSAGE_LENGTH` each, the largest `getdata` still
    unpaced (btclib-org/btclib-node#470), plus twice
    `ONE_BUSY_MODERN_BLOCK_FILTER_BYTES` -- so the two together, short of
    the bound by `slack`, are what the bound is actually sized to hold.
    """
    burst = connection_module._MAX_BLOCKS_PER_GETDATA_BURST
    getdata_total = burst * MAX_PROTOCOL_MESSAGE_LENGTH
    headroom = int(2 * ONE_BUSY_MODERN_BLOCK_FILTER_BYTES) - slack
    return (
        _burst_summing_to(getdata_total, burst),
        _burst_summing_to(headroom, 2),
    )


def test_a_legitimate_getdata_burst_and_cfilters_headroom_are_not_dropped() -> None:
    """`MAX_QUEUED_SEND_BYTES` is sized for BIP157 traffic, not against it.

    A `getdata` answering `_MAX_BLOCKS_PER_GETDATA_BURST` maximal blocks in
    one unpaced call is what `getdata` may still legitimately schedule
    (btclib-org/btclib-node#470); `get_cfilters`'s own paced headroom,
    pipelined behind it the way a `getcfilters` request arriving while
    that answer still drains would be, still fits alongside it.
    """
    first, second = _getdata_burst_and_cfilters_headroom(slack=4096)
    connection, delivered = _two_bursts_in_flight(first, second)
    assert connection.status == P2pConnStatus.Open
    assert len(delivered) == len(first) + len(second)
    assert connection.queued_send_bytes == 0


def test_past_that_headroom_the_peer_is_dropped() -> None:
    """Past the bound above, the peer is dropped.

    The same two bursts, tipping past `MAX_QUEUED_SEND_BYTES` instead of
    stopping short of it.
    """
    first, second = _getdata_burst_and_cfilters_headroom(slack=-4096)
    connection, delivered = _two_bursts_in_flight(first, second)
    assert connection.status == P2pConnStatus.Closed
    # the first burst reached the socket in full; at least one message of
    # the second, the one that tipped the bound, never did
    assert len(delivered) < len(first) + len(second)
    assert len(delivered) >= len(first)
    # released and accounted for, not left on the books by the drop
    assert connection.queued_send_bytes == 0


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
