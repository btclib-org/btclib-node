# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What rpc.connection.RpcConnection does with the octets off a socket.

Driven over a socketpair rather than through a running node: the
functional tests already put a real HTTP client in front of a real
server, and what they cannot reach from there is the half of this file
that only a malformed request provokes -- a header section that never
ends, a Content-Length no client would send, a body that is not JSON.
"""

import asyncio
import base64
import contextlib
import json
import os
import re
import socket
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

import btclib_node.rpc.connection as connection_module
from btclib_node.constants import RPC_WORK_QUEUE
from btclib_node.exceptions import (
    IncompleteRequestHeadError,
    MalformedRequestHeadError,
    OversizedRequestBodyError,
)
from btclib_node.log import Logger
from btclib_node.rpc.auth import (
    FAILED_ATTEMPT_DELAY,
    RpcAuth,
    RpcAuthEntry,
    parse_whitelist,
)
from btclib_node.rpc.connection import (
    MAX_BODY_BYTES,
    MAX_HEADER_BYTES,
    REQUEST_TIMEOUT,
    JSONEncoder,
    RawJSON,
    RequestHead,
    RpcConnection,
    parse_request_head,
)
from btclib_node.rpc.jsonrpc import NO_CONTENT, OK, HttpReply, decode
from btclib_node.rpc.manager import RpcManager
from tests import RPCAUTH, RPCAUTH_LINE
from tests.secret_free import assert_secret_free

if TYPE_CHECKING:
    from collections.abc import Callable


BODY = b'{"jsonrpc":"2.0","id":"x","method":"getbestblockhash"}'


def fake_manager(connections: dict[int, Any]) -> SimpleNamespace:
    """Stand in for the `RpcManager` a connection reads, accepting `RPCAUTH`."""
    # what `RpcManager.track_reply` is handed, `(coroutine, due)` pairs
    # in call order, and what `extend_reply_deadline` is
    tracked: list[tuple[Any, float]] = []
    deadlines: list[float] = []
    return SimpleNamespace(
        auth=RpcAuth((RpcAuthEntry.parse(RPCAUTH),)),
        logger=Logger(debug=True),
        messages=[],
        connections=connections,
        forget_connection=lambda connection_id: connections.pop(connection_id, None),
        # a node not shutting down; `refused`'s `prepare` is what sets
        # the flag (btclib-org/btclib-node#1542)
        node=SimpleNamespace(terminate_flag=threading.Event()),
        tracked=tracked,
        track_reply=lambda reply, due: tracked.append((reply, due)),
        reply_ended=lambda reply: None,
        deadlines=deadlines,
        extend_reply_deadline=deadlines.append,
        # unset, as a manager that never called `interrupt` -- every
        # test built on this fixture reads a connection that is not
        # being shut down; `refused`'s own `interrupted` argument is
        # what sets it (btclib-org/btclib-node#1515)
        interrupted=threading.Event(),
        # what `RpcConnection.run` holds from reading `interrupted` to
        # queuing onto `messages`, as `RpcManager` builds it
        queue_lock=threading.Lock(),
        # not in warmup, as a node that has finished loading
        in_warmup=False,
    )


def request(
    headers: bytes = b"",
    body: bytes = BODY,
    *,
    version: bytes = b"HTTP/1.1",
    auth: bytes = RPCAUTH_LINE,
    method: bytes = b"POST",
    target: bytes = b"/",
) -> bytes:
    """Build a raw HTTP request line and headers, followed by `body`.

    `version` names the request line's own trailing token -- `HTTP/1.1`
    by default, which is every existing caller's own request; a caller
    asking for another, `b"HTTP/1.0"` say, passes it instead.
    `auth` is the `Authorization` line, `RPCAUTH`'s user's unless a
    caller passes another, or `b""` for none. `method` and `target` are
    the request line's first two tokens.
    """
    head = method + b" " + target + b" " + version + b"\r\nHost: x\r\n" + auth + headers
    return head + b"\r\n" + body


def with_length(body: bytes = BODY) -> bytes:
    """Build `request` with a correct Content-Length header for `body`."""
    return request(b"Content-Length: %d\r\n" % len(body), body)


def answering(conn: RpcConnection, data: bytes = b"") -> RpcConnection:
    r"""Set `conn` to answer `data`'s head, as `run` sets it for a request.

    `request(b"Connection: close\r\n")` unless `data` is given.
    """
    conn.head = parse_request_head(data or request(b"Connection: close\r\n"))
    return conn


def drive(
    chunks: list[bytes],
    *,
    timeout: float = 1.0,
    hang_up: bool = False,
    request_timeout: float = REQUEST_TIMEOUT,
) -> tuple[str, list[Any], bool]:
    """Feed `chunks` to a RpcConnection.run and report what it did.

    Returns (outcome, dispatched messages, whether the socket was
    closed). The sender is async because a socketpair holds only a few
    kilobytes: a blocking send of a large chunk would deadlock before
    the loop starts. `request_timeout` is `REQUEST_TIMEOUT` unless a
    caller lowers it, which is what a test of the deadline itself does
    rather than waiting out the real, Core-matching default.

    `manager.connections` is seeded with the id `run` is given below,
    the way `RpcManager.create_connection` seeds it before scheduling
    `run` for real -- every path `run` fails through pops this id back
    out of it, and a manager missing the entry the pop expects would
    have that failure silently swallowed by `run`'s own catch-all
    instead of surfaced to whichever test misses it.
    """

    async def main() -> tuple[str, list[Any], bool]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={0: None})
        conn = RpcConnection(
            loop, ours, cast("RpcManager", manager), 0, request_timeout=request_timeout
        )

        async def send() -> None:
            for chunk in chunks:
                await loop.sock_sendall(theirs, chunk)
                # A bare yield, not a wall-clock delay: it only has to
                # give `conn.run()`'s pending `sock_recv` a loop turn
                # before the next chunk lands, so a many-chunk sender
                # (the chunked-body cases, up to 57 of them) is not
                # racing `timeout` below on a busy machine. Measured at
                # issue #1278: `sleep(0.01)` here cost 0.55-0.57s of
                # real sleep against `timeout`'s 1.0s default, most of
                # a loaded run's budget before any scheduling delay,
                # where `sleep(0)` still forces multiple separate reads
                # (37 of 55 chunks measured at idle, never one) and
                # completes in under a millisecond.
                await asyncio.sleep(0)
            if hang_up:
                theirs.close()

        sender = asyncio.ensure_future(send())
        task = asyncio.ensure_future(conn.run())
        try:
            await asyncio.wait_for(task, timeout)
            outcome = "returned"
        except TimeoutError:
            task.cancel()
            # awaited, so `closed` below reads a settled state rather
            # than racing the cancellation
            with contextlib.suppress(asyncio.CancelledError):
                await task
            outcome = "waiting"
        sender.cancel()
        # A parse error's own reply is scheduled as a task of its own
        # now (`RpcConnection.run`'s own comment on its `ValueError`
        # branch), so it may not have run yet the instant `task` above
        # resolves -- everything else `run` refuses through closes
        # `ours` synchronously, in its own frame, and `asyncio.sleep(0)`
        # costs nothing but a loop turn, so this never waits any real
        # time for those, only enough turns for a scheduled task to run.
        for _ in range(50):
            if ours.fileno() == -1:
                break
            await asyncio.sleep(0)
        closed = ours.fileno() == -1
        if theirs.fileno() != -1:
            theirs.close()
        # `conn.run()` closes `ours` on a refusal, through `async_send`
        # or its own `except`, and leaves it open on a dispatch --
        # `handle_rpc`'s `send` is what closes it there, and nothing
        # here ever calls it. Closed regardless so a dispatched
        # connection does not outlive this function.
        if ours.fileno() != -1:
            ours.close()
        return outcome, manager.messages, closed

    return asyncio.run(main())


def test_a_well_formed_request_is_dispatched() -> None:
    """A well-formed request is queued whole onto `manager.messages`."""
    outcome, messages, _ = drive([with_length()])
    assert outcome == "returned"
    assert messages == [(json.loads(BODY), 0)]


def test_a_batch_is_dispatched_as_it_arrived() -> None:
    """A JSON-RPC batch is dispatched as the array it arrived as.

    Not wrapped in a second list on top of it.
    """
    batch = json.dumps([json.loads(BODY), json.loads(BODY)]).encode()
    _, messages, _ = drive([with_length(batch)])
    # already a list: not wrapped in a second one
    assert messages[0][0] == json.loads(batch)


def test_a_body_split_across_reads_is_reassembled() -> None:
    """A body arriving split across two reads is reassembled before parsing."""
    whole = with_length()
    _, messages, _ = drive([whole[:-10], whole[-10:]])
    assert messages == [(json.loads(BODY), 0)]


def test_a_request_with_no_body_is_refused() -> None:
    """A request with no Content-Length and no body is refused, not dispatched.

    No Content-Length is a length of zero, and `b""` is not JSON. Sends
    its own `Connection: close`, so the refusal's own reply closing is
    what this asserts rather than an idle wait for a next request this
    test never sends -- the parse-error branch honours `Connection` the
    same as any other reply now (issue #640, `test_several_malformed_
    bodies_over_one_kept_alive_connection_are_each_answered` below is
    the keep-alive half of this).
    """
    _, messages, closed = drive([request(b"Connection: close\r\n")])
    assert not messages
    assert closed


def test_a_body_that_is_not_json_is_refused() -> None:
    """A body that fails to parse as JSON is refused, and the socket closed.

    `Connection: close` of its own, for the same reason
    `test_a_request_with_no_body_is_refused` above sends one.
    """
    body = b"not json"
    headers = b"Connection: close\r\nContent-Length: %d\r\n" % len(body)
    _, messages, closed = drive([request(headers, body)])
    assert not messages
    assert closed


def test_a_zero_header_request_leaves_the_next_ones_bytes_intact() -> None:
    r"""A request with no header fields trims `self.buffer` by what it consumed.

    Review round 1: `RequestHead.serialize()` used to insert a `\r\n`
    between `request_line` and `fields` unconditionally, fabricating
    two octets nothing in the wire carried whenever a request had no
    header fields at all -- `head.partition(b"\r\n")` (inside
    `parse_request_head`) then returns an empty separator, `fields`
    empty. `run` trimmed `self.buffer` by that fabricated length,
    eating the first two bytes of whatever followed: a second,
    pipelined request on a kept-alive connection. `request()`'s own
    helper always injects `Host: x`, which is why nothing else in this
    file exercises the zero-field case.
    """

    async def main() -> tuple[bytes, bytes]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={0: None})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)
        second = with_length()
        await loop.sock_sendall(theirs, b"POST / HTTP/1.1\r\n\r\n" + second)
        await conn.run()
        buffer = bytes(conn.buffer)
        theirs.close()
        ours.close()
        return buffer, second

    buffer, second = asyncio.run(main())
    assert buffer == second


def test_an_unterminated_header_section_is_refused() -> None:
    """A header section past MAX_HEADER_BYTES that never ends is refused."""
    flood = [b"POST / HTTP/1.1\r\n"] + [b"X: y\r\n" * 2000] * 12
    _, messages, closed = drive(flood, timeout=3.0)
    assert not messages
    assert closed
    assert len(b"X: y\r\n" * 2000) * 12 > MAX_HEADER_BYTES


def test_a_client_that_goes_away_mid_request_is_refused() -> None:
    """A peer that closes mid-request is refused rather than waited on forever.

    The header section never terminates and the peer closes: the read
    returns nothing, which is the other way out of `_recv`.
    """
    _, messages, closed = drive([b"POST / HTTP/1.1\r\nHost: x\r\n"], hang_up=True)
    assert not messages
    assert closed


def test_a_body_shorter_than_its_length_is_waited_for() -> None:
    """A body shorter than its own declared Content-Length is waited for."""
    whole = with_length()
    outcome, messages, _ = drive([whole[:-5]], timeout=0.4)
    assert outcome == "waiting"
    assert not messages


def test_a_stalled_read_is_refused_once_request_timeout_elapses() -> None:
    """A client that never completes a request is refused at request_timeout.

    ISS 437: with no deadline at all, this coroutine hung on `sock_recv`
    for as long as the node ran instead of ever reaching either outcome
    above. `request_timeout` is lowered here, well below the external
    `timeout` `drive` itself waits on, so the assertion is that `run`
    gives up **on its own** -- `outcome == "returned"`, not `"waiting"` --
    rather than that `drive`'s own `asyncio.wait_for` gave up on it.
    """
    outcome, messages, closed = drive(
        [b"POST / HTTP/1.1\r\nHost: x\r\n"], timeout=1.0, request_timeout=0.05
    )
    assert outcome == "returned"
    assert not messages
    assert closed


def test_the_response_is_crlf_framed_and_the_socket_closed() -> None:
    """async_send frames the reply behind an HTTP header and closes the socket.

    `bytes` are hex-encoded.
    """

    async def main() -> bytes:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        conn = answering(
            RpcConnection(
                loop, ours, cast("RpcManager", fake_manager(connections={})), 0
            )
        )
        await conn.async_send(HttpReply(OK, {"result": b"\xff", "id": "x"}))
        data = await loop.sock_recv(theirs, 4096)
        theirs.close()
        return data

    data = asyncio.run(main())
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"\r\nContent-Type: application/json\r\n" in b"\r\n" + head
    assert json.loads(body) == {"result": "ff", "id": "x"}
    assert int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0]) == len(body)


def sent(reply: HttpReply, *, keep_alive: bool = True) -> bytes:
    """Return what `async_send` writes for `reply`."""

    async def main() -> bytes:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        conn = RpcConnection(
            loop, ours, cast("RpcManager", fake_manager(connections={})), 0
        )
        answering(conn, request() if keep_alive else b"")
        # a kept-alive reply goes on to read the next request, which
        # never comes: the reply is on the wire before that read
        task = asyncio.ensure_future(conn.async_send(reply))
        data = await asyncio.wait_for(loop.sock_recv(theirs, 4096), timeout=2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        theirs.close()
        if ours.fileno() != -1:
            ours.close()
        return data

    return asyncio.run(main())


def test_a_batch_reply_is_written_as_the_array_it_is() -> None:
    """A batch's reply stays an array, whatever its length (issue #653)."""
    for body in ([{"id": "a"}], [{"id": "a"}, {"id": "b"}], []):
        data = sent(HttpReply(OK, body))
        assert json.loads(data.partition(b"\r\n\r\n")[2]) == body


def test_the_status_line_is_the_reply_s_own() -> None:
    """A legacy error goes out under the HTTP status `handle_rpc` chose."""
    body = {"result": None, "error": {"code": -32601, "message": "x"}, "id": 1}
    data = sent(HttpReply("404 Not Found", body))
    assert data.startswith(b"HTTP/1.1 404 Not Found\r\n")
    assert json.loads(data.partition(b"\r\n\r\n")[2]) == body


def test_no_content_is_the_status_line_alone() -> None:
    """A 204 has no body and no `Content-Length`, as `bitcoind` writes it."""
    assert sent(HttpReply(NO_CONTENT, None)) == b"HTTP/1.1 204 No Content\r\n\r\n"
    assert sent(HttpReply(NO_CONTENT, None), keep_alive=False) == (
        b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n"
    )


def test_a_kept_alive_connection_reads_a_second_request_off_the_same_socket() -> None:
    """A reply that keeps the connection open lets a second request through.

    Neither request here carries a `Connection` header, so both default
    to HTTP/1.1's own keep-alive (issue #640): `async_send` calls `run`
    again rather than closing, and the second request queued this way is
    `manager.messages`'s second entry, off the very socket the first
    arrived on. The reply itself carries no `Connection` header of its
    own -- HTTP/1.1's default needs none.
    """

    async def main() -> tuple[list[Any], bool, bytes]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)

        await loop.sock_sendall(theirs, with_length())
        await conn.run()

        await loop.sock_sendall(theirs, with_length())
        await conn.async_send(HttpReply(OK, {"id": "x", "result": None}))
        head = (await loop.sock_recv(theirs, 4096)).partition(b"\r\n\r\n")[0]

        # kept alive by construction -- both requests default to it, and
        # the assertions below are what actually pin that -- so unlike
        # `drive`'s own shared helper this has nothing conditional left
        # to check before closing it itself
        closed = ours.fileno() == -1
        theirs.close()
        ours.close()
        return manager.messages, closed, head

    messages, closed, head = asyncio.run(main())
    assert len(messages) == 2
    assert not closed
    assert b"Connection:" not in head


def test_a_connection_asking_for_close_is_closed_after_its_reply() -> None:
    """`Connection: close` is honoured: one reply, then the socket closes.

    The reply says so too, matching Core's own explicit `Connection:
    close` header (`HTTPRequest::WriteReply`, at
    bitcoin/bitcoin@ca7162cde5) rather than a close `http.client` -- and
    so `bitcoin_core_rpc.SessionTransport` -- has no way to tell from a
    still-open connection otherwise (issue #640).
    """

    async def main() -> tuple[bool, bytes]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={0: None})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)

        headers = b"Connection: close\r\nContent-Length: %d\r\n" % len(BODY)
        await loop.sock_sendall(theirs, request(headers))
        await conn.run()

        await conn.async_send(HttpReply(OK, {"id": "x", "result": None}))
        head = (await loop.sock_recv(theirs, 4096)).partition(b"\r\n\r\n")[0]

        # async_send closes `ours` itself, off the `Connection: close`
        # just read, so there is nothing of this side left to close
        closed = ours.fileno() == -1
        theirs.close()
        return closed, head

    closed, head = asyncio.run(main())
    assert closed
    assert b"\r\nConnection: close\r\n" in head + b"\r\n"


def test_a_kept_alive_connection_idles_out_once_request_timeout_elapses() -> None:
    """A kept-alive connection with no next request is dropped once idle.

    Matches Core's own idle-connection disconnect
    (`HTTPServer::DisconnectClients`, `REQUEST_TIMEOUT`'s own docstring
    above has the citation and the reasoning for reusing that same
    constant here). `request_timeout` is lowered, well below
    `REQUEST_TIMEOUT`'s own real, Core-matching default, so this test
    does not itself wait thirty seconds for it.
    """

    async def main() -> tuple[bool, bool]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={})
        conn = RpcConnection(
            loop, ours, cast("RpcManager", manager), 0, request_timeout=0.2
        )

        await loop.sock_sendall(theirs, with_length())
        await conn.run()

        await asyncio.wait_for(
            conn.async_send(HttpReply(OK, {"id": "x", "result": None})), timeout=2
        )

        # the idle wait inside that async_send is what closes `ours`,
        # once it times out -- nothing of this side left to close either
        closed = ours.fileno() == -1
        theirs.close()
        return closed, 0 in manager.connections

    closed, still_registered = asyncio.run(main())
    assert closed
    assert not still_registered


@pytest.mark.parametrize(
    ("version", "connection_header", "expect_keep_alive"),
    [
        (b"HTTP/1.0", b"", False),
        (b"HTTP/1.0", b"Connection: keep-alive\r\n", True),
        (b"HTTP/1.0", b"Connection: Keep-Alive\r\n", True),
        (b"HTTP/1.0", b"Connection: keep-alive, x\r\n", False),
        (b"HTTP/1.1", b"", True),
        (b"HTTP/1.9", b"", True),
        (b"HTTP/1.1", b"Connection: close\r\n", False),
        (b"HTTP/1.1", b"Connection: CLOSE \t\r\n", False),
        (b"HTTP/1.1", b"Connection: close, x\r\n", True),
    ],
    ids=[
        "1.0-bare-closes",
        "1.0-keep-alive-stays",
        "1.0-keep-alive-any-case-stays",
        "1.0-keep-alive-with-more-closes",
        "1.1-bare-stays",
        "1.9-bare-stays",
        "1.1-close-closes",
        "1.1-close-any-case-trailing-space-closes",
        "1.1-close-with-more-stays",
    ],
)
def test_keep_alive_follows_core_s_own_default_per_version(
    version: bytes, connection_header: bytes, *, expect_keep_alive: bool
) -> None:
    """A version before 1.1 defaults to closing, and 1.1 on to keep-alive.

    Core's `HTTPRequest::WriteReply`, as `RpcConnection._frame` cites it,
    comparing the whole value. A second request, sent here
    regardless of what the first asked for, is answered only where
    `expect_keep_alive` says this connection is still being read from.
    """

    async def main() -> tuple[bool, bool]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)

        headers = connection_header + b"Content-Length: %d\r\n" % len(BODY)
        one_request = request(headers, BODY, version=version)

        await loop.sock_sendall(theirs, one_request)
        await conn.run()

        await loop.sock_sendall(theirs, one_request)
        await conn.async_send(HttpReply(OK, {"id": "x", "result": None}))
        keep_alive = conn.keep_alive
        second_arrived = False
        for _ in range(50):
            if len(manager.messages) >= 2:
                second_arrived = True
                break
            await asyncio.sleep(0)

        theirs.close()
        if ours.fileno() != -1:
            ours.close()
        return keep_alive, second_arrived

    keep_alive, second_arrived = asyncio.run(main())
    assert keep_alive == expect_keep_alive
    assert second_arrived == expect_keep_alive


def test_several_malformed_bodies_over_one_kept_alive_connection_are_each_answered() -> (
    None
):
    """A kept-alive connection answers a run of malformed bodies, staying open.

    `run`'s own `ValueError` branch used to force-close regardless of
    `Connection`: scheduling that reply the way `send` already
    schedules a dispatched one, instead of awaiting it inline, is what
    keeps a run of malformed bodies from growing this coroutine's own
    call stack by one frame each, and what lets each answer honour
    keep-alive instead of always closing (issue #640), matching Core's
    own `HTTPReq_JSONRPC` (`src/httprpc.cpp:224-234`, at
    bitcoin/bitcoin@ca7162cde5).
    """

    async def main() -> tuple[int, bool]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)

        bad = request(b"Content-Length: 3\r\n", b"bad")
        await loop.sock_sendall(theirs, bad)
        await conn.run()

        replies = 0
        for _ in range(20):
            data = await asyncio.wait_for(loop.sock_recv(theirs, 4096), timeout=2)
            assert data
            replies += 1
            await loop.sock_sendall(theirs, bad)

        # kept alive by construction -- every reply here is a valid
        # HTTP/1.1 request with no `Connection: close`, and the
        # assertions below are what actually pin that -- so nothing
        # conditional is left to check before closing this side too
        closed = ours.fileno() == -1
        theirs.close()
        ours.close()
        return replies, closed

    replies, closed = asyncio.run(main())
    assert replies == 20
    assert not closed


def test_the_encoder_defers_to_json_for_what_is_not_bytes() -> None:
    """JSONEncoder encodes bytes as hex and refuses anything else json does.

    `bytes` become hex; anything else is `json`'s own default to
    refuse.
    """
    assert json.dumps(b"\x01\x02", cls=JSONEncoder) == '"0102"'
    with pytest.raises(TypeError):
        json.dumps(object(), cls=JSONEncoder)


def test_a_raw_json_value_with_no_mark_supplied_is_refused_like_any_other_object() -> (
    None
):
    """A RawJSON reaching a JSONEncoder built with no mark= is refused.

    Only `RpcConnection.async_send` is meant to construct `JSONEncoder`
    with a `mark=`; a `RawJSON` reaching one built without it
    (`json.dumps`'s own default `cls=` use) has no placeholder to
    become, so it is refused the same as any other object `json` does
    not know how to encode.
    """
    with pytest.raises(TypeError):
        json.dumps(RawJSON("1.00000000"), cls=JSONEncoder)


def test_a_raw_json_value_is_written_unquoted_and_verbatim() -> None:
    """async_send writes a RawJSON value as an unquoted, verbatim JSON number.

    The whole point: an exact decimal string reaches the wire as a JSON
    number, not a quoted string and not round-tripped through a Python
    float -- 1e-08 is what `float("0.00000001")` would repr as.
    """

    async def main() -> bytes:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        conn = answering(
            RpcConnection(
                loop, ours, cast("RpcManager", fake_manager(connections={})), 0
            )
        )
        await conn.async_send(
            HttpReply(OK, {"result": RawJSON("0.00000001"), "id": "x"})
        )
        data = await loop.sock_recv(theirs, 4096)
        theirs.close()
        return data

    data = asyncio.run(main())
    head, _, body = data.partition(b"\r\n\r\n")
    assert body == b'{"result":0.00000001,"id":"x"}\n'
    assert int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0]) == len(body)


def test_a_raw_json_value_does_not_swallow_a_field_containing_its_own_mark() -> None:
    """async_send's mark substitution ignores a field carrying the mark's text.

    A string field that happens to contain the marker's own text is not
    mistaken for a `RawJSON` placeholder -- the substitution only fires
    where the mark appears twice inside its own pair of quotes.
    """

    async def main() -> bytes:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        conn = answering(
            RpcConnection(
                loop, ours, cast("RpcManager", fake_manager(connections={})), 0
            )
        )
        await conn.async_send(
            HttpReply(
                OK, {"result": "RawJSONx", "extra": RawJSON("1.00000000"), "id": "x"}
            )
        )
        data = await loop.sock_recv(theirs, 4096)
        theirs.close()
        return data

    body = asyncio.run(main()).partition(b"\r\n\r\n")[2]
    assert json.loads(body) == {"result": "RawJSONx", "extra": 1.0, "id": "x"}


def test_a_connection_carries_no_task_handle_for_close_to_cancel() -> None:
    """A connection holds no `task` attribute of its own, past construction.

    An earlier version did (`self.task`), set once at accept and never
    again, so `close` cancelling it did nothing for any request after a
    kept-alive connection's first (issue #714) -- `RpcManager.stop`'s
    own `asyncio.all_tasks(self.loop)` sweep is what actually cancels
    whatever is live, run before `close` is ever called, which is what
    `close`'s own docstring argues rather than a handle this class
    would have to keep current across every request to make good on.
    """
    ours, theirs = socket.socketpair()
    try:
        conn = RpcConnection(
            cast("asyncio.AbstractEventLoop", None),
            ours,
            cast("RpcManager", fake_manager(connections={})),
            0,
        )
        assert not hasattr(conn, "task")
    finally:
        ours.close()
        theirs.close()


def test_repr_names_the_peer_and_says_so_when_there_is_none() -> None:
    """__repr__ names the connected peer, or 'Broken connection' once closed.

    A real TCP pair, not a socketpair: `__repr__` reads `peer[0]` and
    `peer[1]`, which is an AF_INET peer name -- the family the RPC
    server listens on.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname())
    served, _ = listener.accept()

    conn = RpcConnection(
        cast("asyncio.AbstractEventLoop", None),
        client,
        cast("RpcManager", fake_manager(connections={})),
        0,
    )
    host, port = listener.getsockname()
    assert repr(conn) == f"Connection to {host}:{port}"

    client.close()
    assert repr(conn) == "Broken connection"
    served.close()
    listener.close()


@pytest.mark.parametrize(
    ("host", "endpoint"),
    [
        ("::ffff:1.2.3.4", "1.2.3.4:8332"),
        ("2001:db8::1", "[2001:db8::1]:8332"),
    ],
    ids=["v4-mapped", "ipv6"],
)
def test_repr_brackets_an_ipv6_peer(host: str, endpoint: str) -> None:
    """__repr__ brackets an IPv6 peer address, and unwraps a v4-mapped one.

    The RPC listener's own socket is AF_INET
    (`rpc.manager.RpcManager.server`), so no live peer reaches this
    today -- exercised through a mocked `getpeername` the way
    `ip_and_port` itself is (issue #209).
    """
    client = cast("socket.socket", SimpleNamespace(getpeername=lambda: (host, 8332)))
    conn = RpcConnection(
        cast("asyncio.AbstractEventLoop", None),
        client,
        cast("RpcManager", fake_manager(connections={})),
        0,
    )
    assert repr(conn) == f"Connection to {endpoint}"


def test_close_closes_the_socket() -> None:
    """`close` closes `client`, unconditionally."""
    ours, theirs = socket.socketpair()
    conn = RpcConnection(
        cast("asyncio.AbstractEventLoop", None),
        ours,
        cast("RpcManager", fake_manager(connections={})),
        0,
    )
    conn.close()
    assert ours.fileno() == -1
    theirs.close()


# A Windows kernel wait is satisfied against the interrupt-time tick
# clock, not the QPC-backed clock `time.monotonic()` reads (see the
# docstring below), and can be resolved up to one tick before its
# requested duration -- 15.625ms is the documented default tick
# ("The system clock 'ticks' at a constant rate", Microsoft's own
# Remarks for `Sleep`, which times against the same tick-based wait
# timer `WaitForMultipleObjects` itself documents no accuracy for:
# https://learn.microsoft.com/en-us/windows/win32/api/synchapi/
# nf-synchapi-sleep).
_WINDOWS_TIMER_TICK = 0.015625


@pytest.mark.filterwarnings(
    "ignore:coroutine 'RpcConnection.async_send' was never awaited:RuntimeWarning"
)
def test_send_and_wait_gives_up_rather_than_blocking_forever() -> None:
    """send_and_wait gives up after its own timeout, not blocking forever.

    It is what the `stop` RPC uses: the answer has to reach the client
    before the node goes down, but a client that never reads it must not
    keep the node up. The loop here is never run, so the coroutine never
    completes and the wait is the whole of what is exercised -- which
    costs the two seconds the timeout is set to.

    `send_and_wait` hands that coroutine to the loop through
    `run_coroutine_threadsafe`, which only turns it into a `Task` once
    the loop's own thread runs the callback that does so -- never, since
    this loop's `run_forever` is never called. The coroutine object then
    sits unreferenced except by that queued callback, and Python warns
    when it is collected unawaited: a real defect ordinarily, and
    exactly the state this test asks for on purpose.

    `future.result(timeout=2)` waits on a `threading.Condition`, whose
    `wait` reaches `_thread.lock.acquire(True, 2)` -- CPython's
    `lock_PyThread_acquire_lock` (`Modules/_threadmodule.c:814-833`, at
    python/cpython@v3.14.0) calls `_PyMutex_LockTimed`
    (`Python/lock.c:53`), whose own deadline reads `PyTime_MonotonicRaw`
    (`lock.c:67,148`) -- on Windows `QueryPerformanceCounter`
    (`Python/pytime.c:1065-1090`), the same clock `time.monotonic()`
    itself reads (`PyTime_Monotonic`, `pytime.c:1223-1225`, through the
    same `py_get_monotonic_clock`), so the lock's deadline and this
    test's own `waited` measurement are not two different clocks. The
    wait itself parks through `_PyParkingLot_Park` into
    `_PySemaphore_PlatformWait`, which on Windows calls
    `WaitForMultipleObjects` with a millisecond count taken from
    `_PyTime_AsMilliseconds(timeout, _PyTime_ROUND_TIMEOUT)`
    (`Python/parking_lot.c:95-133`, the call at `:130`, the conversion
    at `:105`) -- not `WaitForSingleObject`, and not the legacy
    `Python/thread_nt.h` path `_thread.Lock` no longer uses in 3.14.
    What remains is the gap between that millisecond count and the
    clock the OS actually satisfies the wait against: the kernel's own
    interrupt-time tick, which `_WINDOWS_TIMER_TICK`'s own comment
    above cites Microsoft's documentation for. A 2000ms wait is
    satisfied once that tick clock reaches "start tick + 2000ms", up to
    one tick before 2000ms have elapsed on the QPC-backed clock
    `time.monotonic()` reads, so `_WINDOWS_TIMER_TICK` is the slack
    this bound needs -- small next to the two-second wait it guards, so
    a regression that actually shortens the wait still fails loudly.
    """
    ours, theirs = socket.socketpair()
    loop = asyncio.new_event_loop()
    conn = RpcConnection(
        loop, ours, cast("RpcManager", fake_manager(connections={})), 0
    )
    started = time.monotonic()
    conn.send_and_wait(HttpReply(OK, {"id": "x"}))  # returns, does not raise
    waited = time.monotonic() - started
    assert waited >= 2 - _WINDOWS_TIMER_TICK
    loop.close()
    ours.close()
    theirs.close()


def test_send_and_close_after_returns_at_once_and_delays_the_write() -> None:
    """`send_and_close_after` returns before `delay`, and writes after it.

    `handle_rpc` calls it on `Node`'s thread, played here by this test's
    own, and stops the node right after (ISS 1467), so the wait runs on
    `loop`, a thread of its own here as `RpcManager`'s is. What it hands
    `RpcManager.track_reply` is the coroutine `loop` then runs, due
    when `delay` ends, which is the deadline it records too.
    """
    delay = 1.0
    ours, theirs = socket.socketpair()
    ours.setblocking(False)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    manager = fake_manager(connections={})
    try:
        conn = answering(
            RpcConnection(loop, ours, cast("RpcManager", manager), 0), request()
        )
        started = time.monotonic()
        conn.send_and_close_after(HttpReply(OK, ANSWER), delay)
        returned = time.monotonic()
        # nothing written yet: the call did not wait `delay` out itself
        theirs.setblocking(False)
        with pytest.raises(BlockingIOError):
            theirs.recv(4096)
        theirs.settimeout(5)
        reply = b""
        while chunk := theirs.recv(4096):
            reply += chunk
        received = time.monotonic()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()
        theirs.close()
    [(delayed, due)] = manager.tracked
    assert started + delay <= due <= returned + delay
    assert manager.deadlines == [due]
    # run to completion by `loop`, which is what clears a coroutine's frame
    assert delayed.cr_frame is None
    assert received - started >= delay - _WINDOWS_TIMER_TICK
    assert reply == framed(b"HTTP/1.1 200 OK", b"Content-Length: {length}")
    assert ours.fileno() == -1


def refused(
    data: bytes,
    *,
    interrupted: bool = False,
    prepare: Callable[[SimpleNamespace], None] | None = None,
) -> tuple[bytes, float, bool, list[Any], list[tuple[Any, ...]]]:
    """Send `data` to a `RpcConnection.run` expecting a refusal, and read it.

    Returns the reply, up to the end of the body its own
    `Content-Length` counts, the seconds from `run` starting to the
    reply arriving, whether the connection closed after it, what was
    queued for `handle_rpc`, and every warning logged. `interrupted`
    sets `manager.interrupted` before `run` reads it
    (btclib-org/btclib-node#1515), and `prepare` is handed the manager
    last, before `run` starts.
    """
    warnings: list[tuple[Any, ...]] = []

    def whole(reply: bytes) -> bool:
        """Return whether `reply` holds its header section and its body."""
        head, _, body = reply.partition(b"\r\n\r\n")
        return b"\r\n\r\n" in reply and len(body) >= int(
            re.findall(rb"Content-Length: (\d+)", head)[0]
        )

    async def main() -> tuple[bytes, float, bool, list[Any]]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={0: None})
        manager.logger = SimpleNamespace(
            warning=lambda *args: warnings.append(args),
            log_debug=lambda *_args: None,
        )
        if interrupted:
            manager.interrupted.set()
        if prepare is not None:
            prepare(manager)
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)
        await loop.sock_sendall(theirs, data)
        # before `run`, so the delay it schedules is inside what is measured
        start = time.monotonic()
        await conn.run()
        reply = b""
        async with asyncio.timeout(5):
            while not whole(reply):
                reply += await loop.sock_recv(theirs, 4096)
        elapsed = time.monotonic() - start
        # the reply is on the wire before `_write` decides what comes
        # next, so give it the turns it takes to get there
        for _ in range(50):
            if ours.fileno() == -1:
                break
            await asyncio.sleep(0)
        closed = ours.fileno() == -1
        theirs.close()
        if not closed:
            ours.close()
        return reply, elapsed, closed, manager.messages

    reply, elapsed, closed, messages = asyncio.run(main())
    return reply, elapsed, closed, messages, warnings


def shutting_down(manager: SimpleNamespace) -> None:
    """Set `terminate_flag` on `manager`'s node, as `Node.stop` sets it."""
    manager.node.terminate_flag.set()


def refusing_every_method(manager: SimpleNamespace) -> None:
    """Set `terminate_flag`, and whitelist nothing for `RPCAUTH`'s user."""
    shutting_down(manager)
    manager.auth = RpcAuth(
        (RpcAuthEntry.parse(RPCAUTH),),
        whitelist=parse_whitelist(()),
        whitelist_default=True,
    )


_WRONG_CREDENTIAL = (
    b"Authorization: Basic " + base64.b64encode(b"pytest:wrong") + b"\r\n"
)


@pytest.mark.parametrize(
    ("data", "status", "prepare"),
    [
        (request(b"Content-Length: 0\r\n", b"", auth=b""), b"401", None),
        (
            request(b"Content-Length: 0\r\n", b"", auth=_WRONG_CREDENTIAL),
            b"401",
            None,
        ),
        (request(b"Content-Length: 0\r\n", b"", target=b"/rest/"), b"404", None),
        (request(b"Content-Length: 0\r\n", b"", method=b"GET"), b"405", None),
        (request(b"Content-Length: 3\r\n", b"bad"), b"500", None),
        (with_length(), b"403", refusing_every_method),
    ],
    ids=[
        "401-no-credential",
        "401-wrong-credential",
        "404-target",
        "405-method",
        "500-parse-error",
        "403-whitelist",
    ],
)
def test_a_refusal_written_once_shutdown_began_closes_its_connection(
    data: bytes,
    status: bytes,
    prepare: Callable[[SimpleNamespace], None] | None,
) -> None:
    """Once shutdown has begun, a refusal closes its connection.

    It writes no `Connection: close` for it, as `RpcConnection.
    _shutting_down` says; a 401 for a wrong credential reads the flag
    after its `FAILED_ATTEMPT_DELAY`. Every request here asks for
    keep-alive (btclib-org/btclib-node#1542).
    """
    reply, _, closed, messages, _ = refused(data, prepare=prepare or shutting_down)
    head = reply.partition(b"\r\n\r\n")[0].split(b"\r\n")
    assert head[0].startswith(b"HTTP/1.1 " + status)
    assert b"Connection: close" not in head
    assert closed
    assert messages == []


def test_a_401_reads_the_shutdown_flag_after_its_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong credential's 401 reads `terminate_flag` once its delay ends.

    `HTTPReq_JSONRPC` sleeps `FAILED_ATTEMPT_DELAY` before it writes the
    401. The flag is set as that delay begins, so a 401 that read it any
    earlier would keep the connection open (btclib-org/btclib-node#1542).
    """
    sleep = asyncio.sleep
    delays: list[float] = []

    def prepare(manager: SimpleNamespace) -> None:
        async def flag_then_sleep(delay: float) -> None:
            delays.append(delay)
            manager.node.terminate_flag.set()
            await sleep(delay)

        monkeypatch.setattr(asyncio, "sleep", flag_then_sleep)

    data = request(b"Content-Length: 0\r\n", b"", auth=_WRONG_CREDENTIAL)
    reply, _, closed, _, _ = refused(data, prepare=prepare)
    # the first sleep of the exchange is the 401's own delay
    assert delays[0] == FAILED_ATTEMPT_DELAY
    assert reply.startswith(b"HTTP/1.1 401 Unauthorized\r\n")
    assert closed


UNAUTHORIZED = (
    b"HTTP/1.1 401 Unauthorized\r\n"
    b'WWW-Authenticate: Basic realm="jsonrpc"\r\n'
    b"Content-Length: 0\r\n\r\n"
)


def test_a_request_with_no_credential_is_refused_401_at_once() -> None:
    """No `Authorization`: a 401 naming the scheme, and nothing queued.

    Core answers this one without its brute-force delay, and keeps an
    HTTP/1.1 connection open for the client to retry on.
    """
    reply, elapsed, closed, messages, warnings = refused(
        request(b"Content-Length: %d\r\n" % len(BODY), auth=b"")
    )
    assert reply == UNAUTHORIZED
    assert elapsed < FAILED_ATTEMPT_DELAY
    assert not closed
    assert not messages
    assert not warnings


def test_a_wrong_password_is_refused_401_after_the_delay() -> None:
    """A credential not accepted: logged, delayed, a 401, and nothing queued."""
    wrong = b"Authorization: Basic " + base64.b64encode(b"pytest:wrong") + b"\r\n"
    reply, elapsed, closed, messages, warnings = refused(
        request(b"Content-Length: %d\r\n" % len(BODY), auth=wrong)
    )
    assert reply == UNAUTHORIZED
    assert elapsed >= FAILED_ATTEMPT_DELAY
    assert not closed
    assert not messages
    ((message, peer),) = warnings
    assert message == "ThreadRPCServer incorrect password attempt from %s"
    # a POSIX socketpair is AF_UNIX, whose peer has no `ip:port` to name;
    # Windows emulates `socket.socketpair` over loopback TCP
    expected = r"127\.0\.0\.1:\d+" if os.name == "nt" else "an unknown address"
    assert re.fullmatch(expected, peer)


def test_a_refusal_closes_where_the_request_asked() -> None:
    """`Connection: close` on the request is honoured on a 401 too."""
    reply, _, closed, _, _ = refused(
        request(b"Connection: close\r\nContent-Length: %d\r\n" % len(BODY), auth=b"")
    )
    assert reply == UNAUTHORIZED.replace(b"\r\n\r\n", b"\r\nConnection: close\r\n\r\n")
    assert closed


def test_a_body_that_is_not_json_is_not_parsed_without_a_credential() -> None:
    """A 401 and not `PARSE_ERROR`: the body is never decoded."""
    body = b"not json"
    reply, _, _, _, _ = refused(
        request(b"Content-Length: %d\r\n" % len(body), body, auth=b"")
    )
    assert reply == UNAUTHORIZED


def no_store(status: bytes, *fields: bytes, version: bytes = b"HTTP/1.1") -> bytes:
    """Return `status` with no body, as `WriteNoStoreErrorReply` writes it.

    `fields` follow `Content-Length`, which an HTTP/1.0 answer that is
    not kept open does not carry.
    """
    lines = [version + b" " + status, b"Cache-Control: no-store"]
    if version != b"HTTP/1.0":
        lines.append(b"Content-Length: 0")
    return b"".join(line + b"\r\n" for line in [*lines, *fields]) + b"\r\n"


@pytest.mark.parametrize("method", [b"FOO", b"POST", b"DELETE"])
def test_manager_interrupted_is_503_whatever_the_method(method: bytes) -> None:
    """ISS 1515: `interrupted` is answered 503, with no body.

    `InterruptHTTPServer` swaps `MaybeDispatchRequestToWorker` for
    `RejectRequest`, which answers every request that reaches it so,
    whatever its method (`src/httpserver.cpp`, at
    bitcoin/bitcoin@f0de87f040, the v32.1 tag).
    """
    reply, _, closed, messages, _ = refused(
        request(b"Content-Length: %d\r\n" % len(BODY), auth=b"", method=method),
        interrupted=True,
    )
    assert reply == no_store(b"503 Service Unavailable")
    assert not closed
    assert not messages


def interrupt(manager: SimpleNamespace) -> None:
    """Run `RpcManager.interrupt` on `manager`, a `fake_manager`."""
    RpcManager.interrupt(cast("RpcManager", manager))


def test_an_interrupt_past_the_early_checks_is_refused_as_submit_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1515: interrupted after `_refused_early` let a request through.

    `_dispatch_refusal`, the last check `_refused_early` makes of a `POST`,
    interrupts the manager itself, so the request is past the 503
    `RejectRequest` stands for and not yet queued. Core's
    `ThreadPool::Submit` refuses it there, reading `m_interrupt` under
    the `m_mutex` it queues under, and `MaybeDispatchRequestToWorker`
    answers the refusal (`src/util/threadpool.h`, `src/httpserver.cpp`,
    at bitcoin/bitcoin@f0de87f040, the v32.1 tag).
    """

    def interrupting(manager: SimpleNamespace) -> None:
        refusal = connection_module._dispatch_refusal

        def interrupt_then_refuse(method: bytes, target: bytes) -> str | None:
            interrupt(manager)
            return refusal(method, target)

        monkeypatch.setattr(
            connection_module, "_dispatch_refusal", interrupt_then_refuse
        )

    reply, _, closed, messages, warnings = refused(with_length(), prepare=interrupting)
    body = b"Request rejected during server shutdown"
    assert reply == (
        b"HTTP/1.1 503 Service Unavailable\r\nCache-Control: no-store\r\n"
        b"Content-Length: %d\r\n\r\n" % len(body) + body
    )
    assert closed
    assert not messages
    assert warnings == [
        ("HTTP request rejected during server shutdown: '%s'", "Interrupted")
    ]


def test_a_request_past_the_work_queue_is_refused_as_core_refuses_it() -> None:
    """With `RPC_WORK_QUEUE` requests waiting, the next is a 503.

    It is refused before any credential is read.

    Core's `MaybeDispatchRequestToWorker` answers "Work queue depth
    exceeded" when `g_threadpool_http.WorkQueueSize()` has reached
    `-rpcworkqueue` (`src/httpserver.cpp`, at bitcoin/bitcoin@f0de87f040,
    the v32.1 tag).
    """

    def full(manager: SimpleNamespace) -> None:
        manager.messages.extend([(None, 0)] * RPC_WORK_QUEUE)

    reply, _, _, messages, warnings = refused(with_length(), prepare=full)
    body = b"Work queue depth exceeded"
    assert reply == (
        b"HTTP/1.1 503 Service Unavailable\r\nCache-Control: no-store\r\n"
        b"Content-Length: %d\r\n\r\n" % len(body) + body
    )
    assert len(messages) == RPC_WORK_QUEUE
    assert warnings == [("Request rejected because http work queue depth exceeded",)]


def test_an_interrupt_waits_for_a_request_being_queued() -> None:
    """ISS 1515: `interrupt` cannot land between the check and the queue.

    `authenticated_user`, which `run` calls between reading
    `interrupted` and queuing the request, starts `RpcManager.interrupt`
    on a thread of its own, as `Node._drain_rpc_queue` calls it from
    `Node`'s, and gives it half a second to return before `run` goes on.
    What that thread finds on `messages` once `interrupt` returns is
    what the drain after it would answer: the request, rather than an
    empty queue the request lands on afterwards with nothing left to
    answer it.
    """

    async def main() -> tuple[list[list[Any]], list[Any]]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={0: None})
        seen: list[list[Any]] = []
        returned = threading.Event()

        def interrupting() -> None:
            interrupt(manager)
            seen.append(list(manager.messages))
            returned.set()

        interrupter = threading.Thread(target=interrupting)
        auth: RpcAuth = manager.auth

        def authenticating(authorization: str) -> bytes | None:
            interrupter.start()
            returned.wait(0.5)
            return auth.authenticated_user(authorization)

        manager.auth = SimpleNamespace(
            authenticated_user=authenticating, refusal=auth.refusal
        )
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)
        await loop.sock_sendall(theirs, with_length())
        await conn.run()
        interrupter.join(10)
        theirs.close()
        ours.close()
        return seen, manager.messages

    seen, messages = asyncio.run(main())
    assert messages == [(json.loads(BODY), 0)]
    assert seen == [messages]


ONLY_POST = b"JSONRPC server handles only POST requests"


@pytest.mark.parametrize(
    ("method", "target", "status", "body"),
    [
        (b"GET", b"/", b"405 Method Not Allowed", ONLY_POST),
        (b"HEAD", b"/wallet/w", b"405 Method Not Allowed", ONLY_POST),
        (b"PUT", b"/wallet/", b"405 Method Not Allowed", ONLY_POST),
        (b"DELETE", b"/", b"405 Method Not Allowed", None),
        (b"DELETE", b"/x", b"405 Method Not Allowed", None),
        (b"OPTIONS", b"/", b"405 Method Not Allowed", None),
        (b"PATCH", b"/x", b"405 Method Not Allowed", None),
        (b"TRACE", b"/", b"405 Method Not Allowed", None),
        (b"FOO", b"/x", b"405 Method Not Allowed", None),
        (b"post", b"/", b"405 Method Not Allowed", None),
        (b"CONNECT", b"h:1", b"405 Method Not Allowed", None),
        (b"POST", b"/x", b"404 Not Found", None),
        (b"GET", b"/x", b"404 Not Found", None),
        (b"POST", b"/wallet", b"404 Not Found", None),
        (b"POST", b"/?a=1", b"404 Not Found", None),
        (b"POST", b"/rest/chaininfo.json", b"404 Not Found", None),
    ],
)
def test_a_method_or_a_path_core_refuses_is_refused_before_the_credential(
    method: bytes, target: bytes, status: bytes, body: bytes | None
) -> None:
    """A method or a path `bitcoind` refuses gets its reply, credential or not.

    Each row is what a real `bitcoind` v32.1.0 answers, with no
    `Authorization` as with a good one, and it keeps an HTTP/1.1
    connection open afterwards; a method it does not name is refused on
    any path, the order `_dispatch_refusal`'s own docstring cites. `body` is
    `None` for a refusal `WriteNoStoreErrorReply` writes.
    """
    for auth in (b"", RPCAUTH_LINE):
        data = request(
            b"Content-Length: %d\r\n" % len(BODY),
            auth=auth,
            method=method,
            target=target,
        )
        reply, _, closed, messages, warnings = refused(data)
        assert reply == (
            no_store(status)
            if body is None
            else b"HTTP/1.1 "
            + status
            + b"\r\nContent-Length: %d\r\n\r\n" % len(body)
            + body
        )
        assert not closed
        assert not messages
        assert not warnings


def test_a_refusal_closes_where_the_request_asked_as_a_401_does() -> None:
    """`Connection: close` on the request is honoured on a 404 too."""
    headers = b"Connection: close\r\nContent-Length: %d\r\n" % len(BODY)
    reply, _, closed, _, _ = refused(request(headers, target=b"/x"))
    assert reply == no_store(b"404 Not Found", b"Connection: close")
    assert closed


@pytest.mark.parametrize(
    "line",
    [
        b"POST /",
        b"POST / HTTP/2.0",
        b"POST / HTTP/0.9",
        b"POST / HTTP/1.10",
        b"POST / HTTP/01.1",
        b"POST / http/1.1",
        b"POST / FOO",
        b"POST / HTTP/1.1x",
        b"POST / HTTP/1",
        b"POST /  HTTP/1.1",
        b"POST / HTTP/1.1 ",
        b"POST\t/ HTTP/1.1",
        b"POST /\0 HTTP/1.1",
        b"A / HTTP/1.1",
        b"GET / HTTP/1.",
    ],
)
def test_a_request_line_core_refuses_is_400_and_closes(line: bytes) -> None:
    """A request line `bitcoind` v32.1.0 answers 400 is answered 400 here too.

    Before the credential and before any body is read, and the
    connection closed: the first two rows are ISS 1086's.
    """
    for auth in (b"", RPCAUTH_LINE):
        data = line + b"\r\nHost: x\r\n" + auth
        data += b"Content-Length: %d\r\n\r\n" % len(BODY) + BODY
        reply, _, closed, messages, warnings = refused(data)
        assert reply == no_store(b"400 Bad Request")
        assert closed
        assert not messages
        assert not warnings


def test_an_empty_target_is_404() -> None:
    """Two spaces in a line of three words leave the target empty, not refused.

    `bitcoind` v32.1.0 answers `POST  HTTP/1.1` 404, and keeps the
    connection.
    """
    reply, closed = conversation(b"POST  HTTP/1.1\r\n\r\n" + THEN_CLOSE)
    assert reply == no_store(b"404 Not Found") + THEN_CLOSE_ANSWER
    assert closed


def test_a_malformed_request_on_a_kept_alive_connection_closes_it() -> None:
    """A 400 closes the connection even where the request before it kept it."""

    async def main() -> tuple[bytes, bool]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={0: None})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)
        await loop.sock_sendall(theirs, with_length())
        await conn.run()
        await loop.sock_sendall(theirs, request(b"Content-Length: -1\r\n"))
        await conn.async_send(HttpReply(OK, {"id": "x", "result": None}))
        reply = b""
        async with asyncio.timeout(5):
            while chunk := await loop.sock_recv(theirs, 4096):
                reply += chunk
        closed = ours.fileno() == -1
        theirs.close()
        return reply, closed

    reply, closed = asyncio.run(main())
    assert reply.endswith(no_store(b"400 Bad Request"))
    assert closed


@pytest.mark.parametrize(
    "value",
    [
        b"-1",
        b"-0",
        b"+33",
        b"abc",
        b"",
        b"1 2",
        b"0x10",
        b"1.0",
        b"18446744073709551616",
        b"1" * 5000,
    ],
)
def test_a_content_length_core_refuses_is_400_and_closes(value: bytes) -> None:
    """A `Content-Length` `ToIntegral<uint64_t>` cannot read is answered 400.

    No sign, digits only, and no more than a `uint64_t` holds. `-1` and
    `abc` are ISS 1086's rows, and `bitcoind` v32.1.0 answers `-0`,
    `+33`, an empty value and `18446744073709551616` 400 too.
    """
    for auth in (b"", RPCAUTH_LINE):
        data = request(b"Content-Length: " + value + b"\r\n", BODY, auth=auth)
        reply, _, closed, messages, _ = refused(data)
        assert reply == no_store(b"400 Bad Request")
        assert closed
        assert not messages


@pytest.mark.parametrize(
    "value", [b"%d" % (MAX_BODY_BYTES + 1), b"18446744073709551615"]
)
def test_a_content_length_past_the_cap_is_413_and_closes(value: bytes) -> None:
    """Past `MAX_BODY_BYTES`, Core's `MAX_BODY_SIZE`, is a 413, unread."""
    over = b"Content-Length: " + value + b"\r\n"
    reply, _, closed, messages, _ = refused(request(over, b"", auth=b""))
    assert reply == no_store(b"413 Content too large")
    assert closed
    assert not messages


@pytest.mark.parametrize(
    "fields",
    [
        b"Content-Length: \t%d \t\r\n" % len(BODY),
        b"Content-Length: %s\r\n" % (b"0" * 5000 + b"%d" % len(BODY)),
        b"Content-Length: %d\r\nContent-Length: %d\r\n" % (len(BODY), len(BODY)),
    ],
    ids=["blanks", "leading-zeros", "twice"],
)
def test_a_content_length_core_reads_is_read(fields: bytes) -> None:
    """Blanks, leading zeros and a field named twice are read, as in Core.

    White space is trimmed off the value, and a field named again must
    carry the same value.
    """
    _, messages, _ = drive([request(fields)])
    assert messages == [(json.loads(BODY), 0)]


def test_content_length_values_that_differ_are_400() -> None:
    """A second `Content-Length` must be the first's text, not its number.

    `bitcoind` v32.1.0 answers `33` then `033` 400.
    """
    fields = b"Content-Length: %d\r\nContent-Length: 0%d\r\n" % (len(BODY), len(BODY))
    with pytest.raises(MalformedRequestHeadError, match="differ"):
        parse_request_head(request(fields))


@pytest.mark.parametrize("method", [b"HEAD", b"GET", b"FOO"])
def test_a_content_length_is_read_whatever_the_method(method: bytes) -> None:
    """`LoadBody` reads a body for every method, as `bitcoind` v32.1.0 does.

    A malformed `Content-Length` on a `HEAD` is 400, and a body sent with
    a method Core does not name is read before its 405.
    """
    with pytest.raises(MalformedRequestHeadError, match="Content-Length"):
        parse_request_head(method + b" / HTTP/1.1\r\nContent-Length: abc\r\n\r\n")
    data = method + b" /x HTTP/1.1\r\nContent-Length: 5\r\n\r\nabcde" + THEN_CLOSE
    reply, closed = conversation(data)
    assert reply.endswith(THEN_CLOSE_ANSWER)
    assert reply.count(b"HTTP/1.1 ") == 2
    assert closed


def test_a_body_that_is_not_json_is_a_500_parse_error() -> None:
    """`HTTPReq_JSONRPC`'s parse error, in the legacy envelope, kept alive."""
    body = b"not json"
    reply, _, closed, messages, _ = refused(
        request(b"Content-Length: %d\r\n" % len(body), body)
    )
    head, _, content = reply.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 500 Internal Server Error\r\n")
    assert json.loads(content) == {
        "result": None,
        "error": {"code": -32700, "message": "Parse error"},
        "id": None,
    }
    assert not closed
    assert not messages


@pytest.mark.parametrize(
    "body",
    [
        b'["\\ud800"]',
        b'["\\udc00"]',
        b'["\\ud800x"]',
        b'["\\ud83d\\ud83d\\ude00"]',
        b'{"\\ud800":1}',
        b'{"a":[{"b":"\\ud800"}]}',
        b'{"a":1,"a":"\\ud800"}',
        b'["\xed\xa0\x80"]',
    ],
)
def test_a_lone_surrogate_anywhere_in_a_body_is_a_value_error(body: bytes) -> None:
    """`decode` refuses a lone surrogate, as Core's UTF-8 filter does.

    The escape, its UTF-8 bytes, a key, a nested value and a duplicate
    key's lost value all count. btclib-org/btclib-node#1665
    """
    with pytest.raises(ValueError, match="surrogates not allowed"):
        decode(body)


def test_a_surrogate_pair_is_one_character() -> None:
    """The control: an escaped pair is U+1F600, not a lone surrogate."""
    assert decode(b'["\\ud83d\\ude00"]') == ["\U0001f600"]


def test_a_body_holding_a_lone_surrogate_is_a_500_parse_error() -> None:
    """`bitcoind` answers it `-32700` with a null `id`, before any method."""
    body = b'{"jsonrpc":"1.0","id":7,"method":"help","params":["\\ud800"]}'
    reply, _, _, messages, _ = refused(
        request(b"Content-Length: %d\r\n" % len(body), body)
    )
    head, _, content = reply.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 500 Internal Server Error\r\n")
    assert json.loads(content)["error"] == {"code": -32700, "message": "Parse error"}
    assert json.loads(content)["id"] is None
    assert not messages


def test_a_key_named_twice_is_read_as_its_first_value() -> None:
    """`method` named twice runs the first, as `bitcoind` does (issue #1151)."""
    body = b'{"id":1,"method":"getblockcount","method":"nosuch"}'
    _, messages, _ = drive([with_length(body)])
    assert messages == [({"id": 1, "method": "getblockcount"}, 0)]


def test_an_id_naming_a_key_twice_is_written_back_whole() -> None:
    """An object is written back with every pair it was read with."""
    request_id = decode(b'{"a":1,"a":2}')
    data = sent(HttpReply(OK, {"result": 0, "error": None, "id": request_id}))
    assert data.endswith(b'"id":{"a":1,"a":2}}\n')


def test_a_request_under_wallet_is_dispatched() -> None:
    """A `POST` under `/wallet/` is dispatched like one to `/`, as in Core."""
    data = request(b"Content-Length: %d\r\n" % len(BODY), target=b"/wallet/w")
    _, messages, _ = drive([data])
    assert messages == [(json.loads(BODY), 0)]


@pytest.mark.parametrize("connection", [b"", b"Connection: close\r\n"])
def test_a_reply_to_a_socket_closed_under_it_is_dropped_not_raised(
    connection: bytes,
) -> None:
    """A reply whose socket closed while it was queued leaves no exception.

    Issue #1079: `run` schedules a parse error's reply as a task of its
    own, and closing `ours` before that task runs used to fail its
    `sock_sendall` with `EBADF`, left on a task nothing awaits. The
    connection is let go of either way, whether or not it was to be
    kept alive.
    """

    async def main() -> bool:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={0: None})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)
        headers = connection + b"Content-Length: 3\r\n"
        await loop.sock_sendall(theirs, request(headers, b"bad"))
        await conn.run()
        reply = conn._parse_error_reply
        assert reply is not None
        assert not reply.done()
        ours.close()
        await reply
        theirs.close()
        return 0 in manager.connections

    assert not asyncio.run(main())


ANSWER = {"result": 0, "error": None, "id": 1}
THEN_CLOSE = b"POST /x HTTP/1.1\r\nConnection: close\r\n\r\n"
THEN_CLOSE_ANSWER = (
    b"HTTP/1.1 404 Not Found\r\nCache-Control: no-store\r\n"
    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
)


def conversation(
    data: bytes,
    prepare: Callable[[SimpleNamespace], None] | None = None,
    later: bytes = b"",
) -> tuple[bytes, bool]:
    """Write `data` to a connection and return what it answers, to the close.

    A request `run` queues is answered `ANSWER`, as `handle_rpc` would
    answer it, closing where `send` would; `prepare` is handed the
    manager before that answer is written, and `later` is written to
    the socket after `run` returns, before that answer. Returns
    everything written back, and whether the connection closed. Where
    a request is to be kept open, `data` ends in `THEN_CLOSE`, whose
    answer is what says it was kept: the connection is left nothing
    unread where it closes, so a TCP `socketpair`, which Windows
    emulates one with, closes it cleanly.
    `data` is written as `run` reads it, so that more of it than a
    socket buffer holds is not waited on before `run` starts.
    """

    async def main() -> tuple[bytes, bool]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={0: None})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)
        sender = asyncio.ensure_future(loop.sock_sendall(theirs, data))
        await conn.run()
        await sender
        if later:
            await loop.sock_sendall(theirs, later)
        if manager.messages:
            if prepare is not None:
                prepare(manager)
            await conn.async_send(
                HttpReply(OK, ANSWER), close=manager.node.terminate_flag.is_set()
            )
        reply = b""
        async with asyncio.timeout(5):
            while chunk := await loop.sock_recv(theirs, 4096):
                reply += chunk
        closed = ours.fileno() == -1
        theirs.close()
        ours.close()
        return reply, closed

    return asyncio.run(main())


def good(version: bytes, fields: bytes = b"") -> bytes:
    """Return a request of `version` that is dispatched, with `fields`."""
    return request(fields + b"Content-Length: %d\r\n" % len(BODY), version=version)


def framed(status_line: bytes, *fields: bytes) -> bytes:
    """Return `ANSWER` as `async_send` frames it under `status_line`."""
    body = json.dumps(ANSWER, separators=(",", ":")).encode() + b"\n"
    lines = [status_line, b"Content-Type: application/json", *fields]
    head = b"".join(line + b"\r\n" for line in lines) + b"\r\n"
    return head.replace(b"{length}", b"%d" % len(body)) + body


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (good(b"HTTP/1.0"), framed(b"HTTP/1.0 200 OK")),
        (
            good(b"HTTP/1.0", b"Connection: close\r\n"),
            framed(b"HTTP/1.0 200 OK", b"Connection: close"),
        ),
        (
            good(b"HTTP/1.5", b"Connection: close\r\n"),
            framed(
                b"HTTP/1.5 200 OK", b"Content-Length: {length}", b"Connection: close"
            ),
        ),
        (
            good(b"HTTP/1.0", b"Connection: keep-alive\r\n") + THEN_CLOSE,
            framed(
                b"HTTP/1.0 200 OK",
                b"Connection: keep-alive",
                b"Content-Length: {length}",
            )
            + THEN_CLOSE_ANSWER,
        ),
        (
            good(b"HTTP/1.0", b"Connection:\tkeep-alive\r\n") + THEN_CLOSE,
            framed(
                b"HTTP/1.0 200 OK",
                b"Connection: keep-alive",
                b"Content-Length: {length}",
            )
            + THEN_CLOSE_ANSWER,
        ),
        (
            good(b"HTTP/1.5") + THEN_CLOSE,
            framed(b"HTTP/1.5 200 OK", b"Content-Length: {length}") + THEN_CLOSE_ANSWER,
        ),
        (
            good(b"HTTP/1.1", b"Connection:\tclose\r\n"),
            framed(
                b"HTTP/1.1 200 OK", b"Content-Length: {length}", b"Connection: close"
            ),
        ),
    ],
    ids=[
        "1.0",
        "1.0-close",
        "1.5-close",
        "1.0-keep-alive",
        "1.0-tab-keep-alive",
        "1.5-kept",
        "1.1-tab-close",
    ],
)
def test_an_answer_is_written_in_its_request_s_version(
    data: bytes, expected: bytes
) -> None:
    """The status line echoes the request's version, as `bitcoind` does.

    Issue #1127: before HTTP/1.1, no `Content-Length` unless the request
    asked `keep-alive`, and the connection closed unless it did. A
    `Connection` value is read with its white space trimmed. Each row is
    what `bitcoind` v32.1.0 answers.
    """
    reply, closed = conversation(data)
    assert reply == expected
    assert closed


CLOSING = good(b"HTTP/1.1", b"Connection: close\r\n")
CLOSING_ANSWER = framed(
    b"HTTP/1.1 200 OK", b"Content-Length: {length}", b"Connection: close"
)
TO_X = b"POST /x HTTP/1.1\r\n\r\n"
X_ANSWER = no_store(b"404 Not Found")


def queue_full(manager: SimpleNamespace) -> None:
    """Fill `manager.messages` up to `RPC_WORK_QUEUE`."""
    manager.messages.extend([(None, 0)] * RPC_WORK_QUEUE)


@pytest.mark.parametrize(
    ("data", "expected", "prepare"),
    [
        (CLOSING + TO_X, CLOSING_ANSWER + X_ANSWER, None),
        (CLOSING + TO_X + TO_X, CLOSING_ANSWER + X_ANSWER, None),
        (
            CLOSING
            + b"POST /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
            + b"2\r\nab\r\n0\r\n\r\n",
            CLOSING_ANSWER + X_ANSWER,
            None,
        ),
        (
            CLOSING + b"POST /x HTTP/1.1\r\nConnection: close\r\n\r\n",
            CLOSING_ANSWER + no_store(b"404 Not Found", b"Connection: close"),
            None,
        ),
        (
            CLOSING + b"POST /x HTTP/1.0\r\n\r\n",
            CLOSING_ANSWER + no_store(b"404 Not Found", version=b"HTTP/1.0"),
            None,
        ),
        (
            CLOSING + b"FOO / HTTP/1.1\r\n\r\n",
            CLOSING_ANSWER + no_store(b"405 Method Not Allowed"),
            None,
        ),
        (
            CLOSING + b"garbage\r\n\r\n",
            CLOSING_ANSWER + no_store(b"400 Bad Request"),
            None,
        ),
        (
            CLOSING + b"POST / HTTP/1.1\r\nContent-Length: 99999999999\r\n\r\n",
            CLOSING_ANSWER + no_store(b"413 Content too large"),
            None,
        ),
        (
            CLOSING + b"POST /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
            CLOSING_ANSWER + no_store(b"400 Bad Request"),
            None,
        ),
        (CLOSING + with_length(), CLOSING_ANSWER, None),
        (CLOSING + b"GET / HTTP/1.1\r\n\r\n", CLOSING_ANSWER, None),
        (CLOSING + b"POST /x HTTP/1.1\r\n", CLOSING_ANSWER, None),
        (
            CLOSING + b"POST /x HTTP/1.1\r\nContent-Length: 5\r\n\r\nab",
            CLOSING_ANSWER,
            None,
        ),
        (
            CLOSING + b"POST /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab",
            CLOSING_ANSWER,
            None,
        ),
        (
            CLOSING + TO_X,
            CLOSING_ANSWER + no_store(b"503 Service Unavailable"),
            interrupt,
        ),
        (
            CLOSING + with_length(),
            CLOSING_ANSWER
            + b"HTTP/1.1 503 Service Unavailable\r\nCache-Control: no-store\r\n"
            b"Content-Length: 25\r\n\r\nWork queue depth exceeded",
            queue_full,
        ),
        (
            good(b"HTTP/1.0") + TO_X,
            framed(b"HTTP/1.0 200 OK") + X_ANSWER,
            None,
        ),
        (
            good(b"HTTP/1.1") + TO_X,
            framed(b"HTTP/1.1 200 OK", b"Content-Length: {length}"),
            shutting_down,
        ),
    ],
    ids=[
        "404",
        "only-one",
        "404-chunked",
        "404-close",
        "404-1.0",
        "405-unknown",
        "400",
        "413",
        "400-chunk-size",
        "worker",
        "worker-get",
        "head-not-whole",
        "body-not-whole",
        "chunks-not-whole",
        "interrupted-503",
        "queue-full-503",
        "1.0-closes",
        "shutdown-close-reads-none",
    ],
)
def test_one_request_past_a_worker_s_close_is_answered_as_core_does(
    data: bytes,
    expected: bytes,
    prepare: Callable[[SimpleNamespace], None] | None,
) -> None:
    """Past a worker's reply that closes, Core reads one buffered request.

    It answers it where its socket thread does, and not where a worker
    would: each row is what `bitcoind` v32.1.0 answered
    (btclib-org/btclib-node#2101). For `worker-get` the worker races
    the disconnect and sometimes answers; the row is the more common
    outcome. A close forced by shutdown reads none: Core does not close
    there.
    """
    reply, closed = conversation(data, prepare)
    assert reply == expected
    assert closed


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            request(b"Connection: close\r\n", b"", auth=b"") + TO_X,
            UNAUTHORIZED.removesuffix(b"\r\n")
            + b"Connection: close\r\n\r\n"
            + X_ANSWER,
        ),
        (
            request(b"", b"", method=b"GET", version=b"HTTP/1.0") + TO_X,
            b"HTTP/1.0 405 Method Not Allowed\r\n\r\n" + ONLY_POST + X_ANSWER,
        ),
        (
            request(b"Connection: close\r\n", b"", target=b"/x") + TO_X,
            no_store(b"404 Not Found", b"Connection: close"),
        ),
    ],
    ids=["401", "jsonrpc-405", "dispatcher-404-reads-none"],
)
def test_a_refusal_a_worker_writes_is_read_past_as_an_answer_is(
    data: bytes, expected: bytes
) -> None:
    """A 401, or `HTTPReq_JSONRPC`'s 405, is a worker's reply, read past.

    The dispatcher's own 404 is written on Core's socket thread, which
    reads nothing more once it closes. Each row is what `bitcoind`
    v32.1.0 answered (btclib-org/btclib-node#2101).
    """
    reply, closed = conversation(data)
    assert reply == expected
    assert closed


@pytest.mark.parametrize(
    ("later", "status"),
    [(TO_X, b"404 Not Found"), (b"garbage\r\n\r\n", b"400 Bad Request")],
    ids=["404", "400"],
)
def test_a_request_sent_before_a_worker_s_close_is_read_past(
    later: bytes, status: bytes
) -> None:
    """A request sent apart, after the closing one, is answered too.

    Core's socket thread receives while the worker runs, its buffer
    empty, so the request is there when the reply closes;
    `bitcoind` v32.1.0 answers it (btclib-org/btclib-node#2101).
    """
    reply, closed = conversation(CLOSING, later=later)
    assert reply == CLOSING_ANSWER + no_store(status)
    assert closed


def test_a_get_with_the_work_queue_full_is_503() -> None:
    """Core checks the queue before its worker answers a `GET` 405.

    `bitcoind` v32.1.0 run with `-rpcthreads=1 -rpcworkqueue=1`, its
    queue full, answers a `GET /` 503 "Work queue depth exceeded"
    (`MaybeDispatchRequestToWorker`, `src/httpserver.cpp`, at
    bitcoin/bitcoin@f0de87f040, the v32.1 tag).
    """
    data = request(method=b"GET", body=b"", auth=b"")
    reply, _, _, _, warnings = refused(data, prepare=queue_full)
    body = b"Work queue depth exceeded"
    assert reply == (
        b"HTTP/1.1 503 Service Unavailable\r\nCache-Control: no-store\r\n"
        b"Content-Length: %d\r\n\r\n" % len(body) + body
    )
    assert warnings == [("Request rejected because http work queue depth exceeded",)]


def test_a_refusal_is_written_in_its_request_s_version() -> None:
    """A 401 and a 404 to HTTP/1.0 are framed as the answer to a call is."""
    reply, closed = conversation(
        request(b"Content-Length: 0\r\n", b"", version=b"HTTP/1.0", auth=b"")
    )
    assert reply == (
        b'HTTP/1.0 401 Unauthorized\r\nWWW-Authenticate: Basic realm="jsonrpc"\r\n\r\n'
    )
    assert closed
    reply, closed = conversation(b"POST /x HTTP/1.0\r\n\r\n")
    assert reply == no_store(b"404 Not Found", version=b"HTTP/1.0")
    assert closed


@pytest.mark.parametrize(
    ("data", "version"),
    [
        (b"POST / HTTP/1.0\r\nContent-Length: abc\r\n\r\n", b"HTTP/1.0"),
        (b"POST / HTTP/1.5\r\nContent-Length: abc\r\n\r\n", b"HTTP/1.5"),
        (b"POST / HTTP/0.9\r\nContent-Length: abc\r\n\r\n", b"HTTP/1.1"),
        (b"POST / HTTP/2.0\r\n\r\n", b"HTTP/1.1"),
    ],
    ids=["1.0", "1.5", "0.9", "2.0"],
)
def test_a_request_core_cannot_read_is_answered_in_the_version_it_read(
    data: bytes, version: bytes
) -> None:
    """A 400 is in the request's version, HTTP/1.1 where Core did not read one.

    `HTTPVersion`'s default, as `bitcoind` v32.1.0 answers
    `http-1.0-length-abc`, `http-1.5-length-abc` and `http-0.9`; it
    closes after each.
    """
    reply, closed = conversation(data)
    assert reply == no_store(b"400 Bad Request", version=version)
    assert closed


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (b"1" * 5000, MalformedRequestHeadError),
        (b"18446744073709551616", MalformedRequestHeadError),
        (b"18446744073709551615", OversizedRequestBodyError),
        (b"0" * 5000 + b"1", None),
    ],
    ids=["past-4300-digits", "past-uint64", "uint64-max", "zeros"],
)
def test_a_long_content_length_is_read_as_uint64(
    value: bytes, error: type[Exception] | None
) -> None:
    """400 past a `uint64_t`, 413 below it, and not `int`'s own `ValueError`."""
    data = b"POST / HTTP/1.1\r\nContent-Length: " + value + b"\r\n\r\n"
    if error is None:
        assert parse_request_head(data).length == 1
    else:
        with pytest.raises(error):
            parse_request_head(data)


CREDENTIAL = b"dXNlcjpjb3JyZWN0LWhvcnNl"


@pytest.mark.parametrize(
    "line",
    [
        b"Authorization Basic " + CREDENTIAL,
        b"Authorization: Basic " + CREDENTIAL + b"\r" + CREDENTIAL,
        b"Authorization\r: Basic " + CREDENTIAL,
    ],
    ids=["no colon", "bare CR in the value", "CR in the name"],
)
def test_a_malformed_header_is_named_by_index_not_by_its_text(line: bytes) -> None:
    """ISS 1908: a malformed `Authorization` line is not in the refusal."""
    head = b"POST / HTTP/1.1\r\nHost: h\r\n" + line + b"\r\n\r\n"
    with pytest.raises(MalformedRequestHeadError) as raised:
        parse_request_head(head)
    assert "header field 1" in str(raised.value)
    assert_secret_free(raised.value, [CREDENTIAL.decode(), "Authorization"])


# A run of zeros two digit quantifiers could split between them, which
# backtracks in quadratic time where what follows it fails: 60000 of them
# took seconds that way, and take about a millisecond read by one. Past
# what a line holds (`MAX_HEADER_BYTES`), so `_uint64` is driven directly.
ZEROS = 60000


@pytest.mark.parametrize(
    ("text", "base", "expected"),
    [
        (b"x", 10, None),
        (b"", 10, 0),
        (b"3x", 10, None),
        (b"x", 16, None),
        (b"21", 16, 0x21),
    ],
    ids=["then-x", "0", "3-then-x", "hex-then-x", "hex-21"],
)
def test_a_long_run_of_zeros_is_read_in_linear_time(
    text: bytes, base: int, expected: int | None
) -> None:
    """A length or a chunk size of leading zeros is read at once.

    The head is read before any credential, on the listener's one loop,
    so a parse that takes seconds is a stall any client can cause. The
    bound is a hundred Python-level passes over the run, timed beside
    it: a parse that reads each octet once takes a few passes, a
    quadratic one tens of thousands. It is there to fail the second.
    """
    zeros = b"0" * ZEROS

    def one_pass() -> None:
        for _ in zeros:
            pass

    def read() -> None:
        assert connection_module._uint64(zeros + text, base) == expected

    assert _fastest(read) < 100 * _fastest(one_pass)


def _fastest(run: Callable[[], object], runs: int = 3) -> float:
    """Return the shortest of `runs` timings of `run`, in seconds."""
    fastest = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        run()
        fastest = min(fastest, time.perf_counter() - start)
    return fastest


def test_an_answer_with_no_request_read_is_refused() -> None:
    """`_frame` has no version to write before `run` has read a request."""
    conn = RpcConnection(
        cast("asyncio.AbstractEventLoop", None),
        cast("socket.socket", None),
        cast("RpcManager", fake_manager(connections={})),
        0,
    )
    with pytest.raises(RuntimeError, match="no request to answer"):
        conn._frame(OK, b"")


@pytest.mark.parametrize(
    "target",
    [
        b"/a:b",
        b"a:b",
        b"1:x",
        b"/%zz",
        b"*",
        b"http://h:99999/",
        b"http://[zz]/",
        b"http://u^@h/",
        b"//h:x/",
    ],
)
def test_a_request_target_is_read_as_given(target: bytes) -> None:
    """`LoadControlData` reads any word as the target, and parses none.

    So each of these is a 404, the connection kept, as `bitcoind`
    v32.1.0 answers `target-colon-first` and `target-bad-ipv6`.
    """
    assert parse_request_head(b"POST " + target + b" HTTP/1.1\r\n\r\n").target == target
    reply, closed = conversation(b"POST " + target + b" HTTP/1.1\r\n\r\n" + THEN_CLOSE)
    assert reply == no_store(b"404 Not Found") + THEN_CLOSE_ANSWER
    assert closed


@pytest.mark.parametrize(
    "fields", [b"", b"Proxy-Connection: keep-alive\r\n", b"Connection: keep-alive\r\n"]
)
def test_an_absolute_form_target_is_a_404_kept_open(fields: bytes) -> None:
    """An `http` target with an authority is no proxy request to Core.

    `bitcoind` v32.1.0 answers it 404 and keeps the connection, a
    `Proxy-Connection` read as any other field.
    """
    data = b"POST http://127.0.0.1:1/ HTTP/1.1\r\n" + fields + b"\r\n" + THEN_CLOSE
    reply, closed = conversation(data)
    assert reply == no_store(b"404 Not Found") + THEN_CLOSE_ANSWER
    assert closed


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            b"CONNECT / HTTP/1.1\r\n\r\n" + THEN_CLOSE,
            no_store(b"405 Method Not Allowed") + THEN_CLOSE_ANSWER,
        ),
        (
            b"CONNECT / HTTP/1.1\r\nContent-Length: 2\r\n\r\nxy" + THEN_CLOSE,
            no_store(b"405 Method Not Allowed") + THEN_CLOSE_ANSWER,
        ),
        (
            b"CONNECT h:1 HTTP/1.0\r\n\r\n",
            no_store(b"405 Method Not Allowed", version=b"HTTP/1.0"),
        ),
        (
            b"CONNECT / HTTP/1.1\r\nContent-Length: abc\r\n\r\n",
            no_store(b"400 Bad Request"),
        ),
    ],
    ids=["1.1", "body", "1.0", "length-abc"],
)
def test_a_connect_is_framed_as_any_other_method(data: bytes, expected: bytes) -> None:
    """A `CONNECT` is a method Core does not name: a 405, its body read first.

    What `bitcoind` v32.1.0 answers `connect`, `connect-1.0` and
    `connect-length-abc`.
    """
    reply, closed = conversation(data)
    assert reply == expected
    assert closed


@pytest.mark.parametrize("terminator", [b"\r\n\r\n", b"\n\n", b"\r\n\n", b"\n\r\n"])
def test_a_refused_request_line_is_taken_to_its_own_line_end(
    terminator: bytes,
) -> None:
    """What a refused head holds is the line and its own ending."""
    with pytest.raises(MalformedRequestHeadError):
        parse_request_head(b"POST / HTTP/2.0" + terminator)
    head = connection_module._HeadReader(
        bytearray(b"POST / HTTP/2.0" + terminator)
    ).read()
    assert head is not None
    assert (
        head.serialize() == b"POST / HTTP/2.0" + terminator.partition(b"\n")[0] + b"\n"
    )
    assert head.consumed == len(head.serialize())


def lf(data: bytes) -> bytes:
    """Return `data` with every CRLF a bare LF."""
    return data.replace(b"\r\n", b"\n")


@pytest.mark.parametrize(
    "data",
    [
        lf(with_length()),
        with_length().replace(b"\r\n", b"\n", 1),
        with_length().replace(b"\r\n\r\n", b"\r\n\n"),
        with_length().replace(b"\r\n\r\n", b"\n\r\n"),
    ],
    ids=["every-line", "request-line", "empty-line", "last-field"],
)
def test_a_line_ended_by_a_bare_line_feed_is_read(data: bytes) -> None:
    """A bare LF ends a line, as `util::LineReader` ends one (issue #1150)."""
    _, messages, _ = drive([data])
    assert messages == [(json.loads(BODY), 0)]
    head = parse_request_head(data)
    assert head.serialize() == data[: -len(BODY)]
    assert head.consumed == len(data) - len(BODY)


def test_only_one_carriage_return_is_dropped_before_a_line_feed() -> None:
    r"""`POST / HTTP/1.1\r\r\n` keeps a CR in its version, and is 400.

    What `bitcoind` v32.1.0 answers.
    """
    with pytest.raises(MalformedRequestHeadError, match="version"):
        parse_request_head(b"POST / HTTP/1.1\r\r\n\r\n")


def test_the_empty_line_ends_the_section_whatever_follows() -> None:
    """The first empty line ends the section, with a second request after it."""
    second = lf(with_length())
    data = b"POST / HTTP/1.1\nHost: x\n\n" + second
    head = parse_request_head(data)
    assert data[head.consumed :] == second


def test_the_answer_to_stop_closes_a_kept_alive_connection() -> None:
    """`send_and_wait` closes once its answer is written, saying nothing of it.

    What `bitcoind` v32.1.0 does with its answer to `stop` on a
    connection it keeps otherwise: no `Connection: close`, and the
    connection closed.
    """
    ours, theirs = socket.socketpair()
    ours.setblocking(False)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    try:
        conn = answering(
            RpcConnection(
                loop, ours, cast("RpcManager", fake_manager(connections={})), 0
            ),
            request(),
        )
        conn.send_and_wait(HttpReply(OK, ANSWER))
        theirs.settimeout(5)
        reply = b""
        while chunk := theirs.recv(4096):
            reply += chunk
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()
        theirs.close()
    assert reply == framed(b"HTTP/1.1 200 OK", b"Content-Length: {length}")
    assert ours.fileno() == -1


# issue #1126: the header section as `HTTPHeaders::Read` reads it, and a
# chunked body as `HTTPRequest::LoadBody` does


def head_with(fields: bytes) -> RequestHead:
    """Parse a `POST /` head of `fields`, which end at the section's end."""
    return parse_request_head(b"POST / HTTP/1.1\r\n" + fields + b"\r\n")


@pytest.mark.parametrize(
    ("fields", "name", "value"),
    [
        (b"Connection:\tclose\r\n", b"Connection", "close"),
        (b"Connection:  \tclose\r\n", b"Connection", "close"),
        (b"Connection: close \t\r\n", b"Connection", "close"),
        (b"Connection: \x0bclose\x0c\r\n", b"Connection", "close"),
        (b"connection: close\r\nConnection: keep-alive\r\n", b"Connection", "close"),
        (b"X: a:b\r\n", b"x", "a:b"),
        (b"X:\r\n", b"X", ""),
    ],
    ids=[
        "leading-tab",
        "leading-blanks",
        "trailing-blanks",
        "vertical-tab-and-form-feed",
        "first-of-two",
        "colon-in-value",
        "empty-value",
    ],
)
def test_a_field_is_read_as_core_reads_it(
    fields: bytes, name: bytes, value: str
) -> None:
    """A field's name runs to its first colon, its value trimmed of white space.

    `HTTPHeaders::Read` and `FindFirst`: a name is looked up by its first
    field, ASCII case-insensitively.
    """
    assert head_with(fields).field(name) == value


@pytest.mark.parametrize(
    "fields",
    [
        b"garbage\r\n",
        b": x\r\n",
        b"X Y: z\r\n",
        b"Connection\t: close\r\n",
        b"X\x0b: y\r\n",
        b"X\r: y\r\n",
        b"X: a\rb\r\n",
        b"X: a\r b\r\n",
        b"X: a\r\r\n",
        b" x\r\n",
        b"X: y\r\n x: z\r\n",
        b"Connection\0: keep-alive\r\n",
        b"Connection: close\0junk\r\n",
        b"\0junk\r\n",
    ],
    ids=[
        "no-colon",
        "empty-name",
        "space-in-name",
        "tab-before-colon",
        "vertical-tab-in-name",
        "cr-in-name",
        "bare-cr-in-value",
        "cr-then-space",
        "cr-ending-value",
        "continuation-first",
        "continuation",
        "nul-in-name",
        "nul-in-value",
        "nul-led",
    ],
)
def test_a_field_core_refuses_is_400_and_closes(fields: bytes) -> None:
    """A field `HTTPHeaders::Read` refuses is 400, as in `bitcoind` v32.1.0."""
    with pytest.raises(MalformedRequestHeadError):
        head_with(fields)
    # refused on the last octet sent, so that nothing is left unread
    reply, closed = conversation(b"POST / HTTP/1.1\r\n" + fields)
    assert reply == no_store(b"400 Bad Request")
    assert closed


def test_more_fields_than_http_client_reads_are_read() -> None:
    """A section is bounded by its size, as Core bounds it, not by count."""
    fields = b"X: y\r\n" * 101 + b"Content-Length: %d\r\n" % len(BODY)
    _, messages, _ = drive([request(fields)])
    assert messages == [(json.loads(BODY), 0)]


def sized(total: int, eol: bytes = b"\r\n") -> bytes:
    """Return a head whose section `HTTPHeaders::Read` counts as `total` octets.

    A request line and one field, each ended by `eol`, then the
    section's own end: the field and the end are what is counted.
    """
    field = b"X: " + b"a" * (total - 3 - 2 * len(eol))
    return b"POST / HTTP/1.1" + eol + field + eol + eol


def test_a_section_s_size_counts_its_lines_with_their_endings() -> None:
    """`MAX_HEADER_BYTES` of field lines, their endings and the section's end.

    The request line is not counted. `bitcoind` v32.1.0 answers 400 past
    8192, `size-8193-many-lines` among them (issue #1972).
    """
    assert parse_request_head(sized(MAX_HEADER_BYTES)).error is None
    assert parse_request_head(sized(MAX_HEADER_BYTES, b"\n")).error is None
    long_line = b"POST /" + b"a" * 8000 + b" HTTP/1.1\r\n"
    rest = sized(MAX_HEADER_BYTES).partition(b"\r\n")[2]
    assert parse_request_head(long_line + rest).error is None
    many = b"POST / HTTP/1.1\r\n" + b"X:\r\n" * ((MAX_HEADER_BYTES - 2) // 4)
    assert parse_request_head(many + b"\r\n").error is None
    with pytest.raises(MalformedRequestHeadError, match="MAX_HEADER_BYTES"):
        parse_request_head(sized(MAX_HEADER_BYTES + 1))
    with pytest.raises(MalformedRequestHeadError, match="MAX_HEADER_BYTES"):
        parse_request_head(many + b"X:\r\n\r\n")


def test_a_line_past_its_limit_is_refused_before_it_ends() -> None:
    """A line of `MAX_HEADER_BYTES` octets is read, and one more is refused.

    `util::LineReader` counts every octet before the line feed, a
    carriage return included, and refuses once that runs past the limit,
    the line feed sent or not; a line under it is waited on.
    """
    field = b"POST / HTTP/1.1\r\nX: " + b"a" * (MAX_HEADER_BYTES - 3)
    with pytest.raises(IncompleteRequestHeadError):
        parse_request_head(field)
    with pytest.raises(MalformedRequestHeadError, match="line past"):
        parse_request_head(field + b"a")
    line = b"POST /" + b"a" * (MAX_HEADER_BYTES - 15) + b" HTTP/1.0"
    assert parse_request_head(line + b"\n\n").target.endswith(b"a")
    with pytest.raises(MalformedRequestHeadError, match="line past"):
        parse_request_head(line + b"\r\n\r\n")
    with pytest.raises(IncompleteRequestHeadError):
        parse_request_head(line)
    reply, closed = conversation(field + b"a")
    assert reply == no_store(b"400 Bad Request")
    assert closed


def test_a_refused_section_is_answered_by_the_fields_read_before_it() -> None:
    """A `Connection: close` read before the refused line frames the 400.

    As `bitcoind` v32.1.0 answers `continuation-length`.
    """
    fields = b"Connection: close\r\nbad\r\n"
    head = connection_module._HeadReader(
        bytearray(b"POST / HTTP/1.1\r\n" + fields + b"\r\n")
    ).read()
    assert head is not None
    assert head.error is not None
    assert head.connection == "close"
    # refused on the last octet sent, so that nothing is left unread
    reply, closed = conversation(
        b"POST / HTTP/1.1\r\nHost: x\r\n" + RPCAUTH_LINE + fields
    )
    assert reply == no_store(b"400 Bad Request", b"Connection: close")
    assert closed


CHUNKED_FIELD = b"Transfer-Encoding: chunked\r\n"


def chunked(*chunks: bytes, trailer: bytes = b"") -> bytes:
    """Return `chunks` as a chunked body, then `trailer` and its end."""
    body = b"".join(b"%x\r\n%s\r\n" % (len(chunk), chunk) for chunk in chunks)
    return body + b"0\r\n" + trailer + b"\r\n"


@pytest.mark.parametrize(
    ("fields", "method", "is_chunked", "length"),
    [
        (CHUNKED_FIELD + b"Content-Length: 5\r\n", b"POST", True, 0),
        (CHUNKED_FIELD + b"Content-Length: abc\r\n", b"POST", True, 0),
        (b"Transfer-Encoding: ChUnKeD\r\n", b"POST", True, 0),
        (b"Transfer-Encoding:\tchunked\r\nContent-Length: 5\r\n", b"POST", True, 0),
        (b"Transfer-Encoding: gzip, chunked\r\n", b"POST", False, 0),
        (CHUNKED_FIELD, b"HEAD", True, 0),
    ],
    ids=["over-length", "over-length-abc", "any-case", "tab-led", "a-list", "head"],
)
def test_a_body_is_chunked_where_load_body_says(
    fields: bytes, method: bytes, *, is_chunked: bool, length: int
) -> None:
    """A first `Transfer-Encoding` of `chunked`, any case, on any method."""
    head = parse_request_head(method + b" / HTTP/1.1\r\n" + fields + b"\r\n")
    assert head.chunked == is_chunked
    assert head.length == length


def chunk_size(line: bytes) -> int:
    """Return the size `_ChunkedReader` reads off `line`."""
    head = parse_request_head(b"POST / HTTP/1.1\r\n" + CHUNKED_FIELD + b"\r\n")
    return connection_module._ChunkedReader(bytearray(), head)._chunk_size(line)


@pytest.mark.parametrize(
    ("line", "size"),
    [
        (b"21", 0x21),
        (b"21;x", 0x21),
        (b"21 ;x", 0x21),
        (b"21;", 0x21),
        (b"  21\t", 0x21),
        (b"\x0b21\x0c", 0x21),
        (b"0021", 0x21),
        (b"aB", 0xAB),
        (b"0", 0),
        (b"0x21", None),
        (b"+21", None),
        (b"-1", None),
        (b"-0", None),
        (b"zz", None),
        (b"2 1", None),
        (b"", None),
        (b";x", None),
        (b"\x0021", None),
        (b"f" * 17, None),
    ],
)
def test_a_chunk_size_is_read_as_to_integral_reads_it(
    line: bytes, size: int | None
) -> None:
    """Base 16, up to the first `;`, white space trimmed, and nothing else.

    No sign and no `0x`, and no more than a `uint64_t` holds.
    """
    if size is None:
        with pytest.raises(MalformedRequestHeadError, match="chunk size"):
            chunk_size(line)
    else:
        assert chunk_size(line) == size


@pytest.mark.parametrize(
    "body",
    [
        chunked(BODY),
        chunked(BODY[:10], BODY[10:]),
        b"%x;a=b\r\n%s\r\n0\r\n\r\n" % (len(BODY), BODY),
        b"%X\r\n%s\r\n0;x\r\n\r\n" % (len(BODY), BODY),
        chunked(BODY, trailer=b"X: y\r\n"),
    ],
    ids=["one", "two", "extension", "upper-case", "trailer"],
)
def test_a_chunked_body_is_decoded_and_dispatched(body: bytes) -> None:
    """The chunks' data, joined, is the body, as `bitcoind` v32.1.0 reads it.

    Sent three octets at a time, so each line is read across reads.
    `outcome` is asserted on too (issue #1278): `drive`'s own
    `wait_for` can in principle still time out on a starved machine,
    and discarding it would then surface as this test's `messages`
    assertion with nothing dispatched -- the same shape a real
    decoding failure produces -- rather than naming the timeout.
    """
    data = request(CHUNKED_FIELD, body)
    outcome, messages, _ = drive([data[i : i + 3] for i in range(0, len(data), 3)])
    assert outcome == "returned"
    assert messages == [(json.loads(BODY), 0)]


def test_a_trailer_s_fields_are_read_and_dropped() -> None:
    """A trailer carries neither the credential nor a `Connection: close`.

    `LoadBody` reads the trailer with `write=false`, so what Core looks
    up is the request's own fields: `bitcoind` v32.1.0 answers
    `trailer-credential` 401 and keeps a connection whose trailer asks
    to close.
    """
    trailer = RPCAUTH_LINE + b"Connection: close\r\n"
    data = request(CHUNKED_FIELD, chunked(BODY, trailer=trailer), auth=b"")
    reply, closed = conversation(data + THEN_CLOSE)
    assert reply == UNAUTHORIZED + THEN_CLOSE_ANSWER
    assert closed


def test_a_chunked_request_on_a_kept_alive_connection_leaves_the_next() -> None:
    """What follows the trailer is the next request."""
    reply, closed = conversation(request(CHUNKED_FIELD, chunked(BODY)) + THEN_CLOSE)
    assert reply == (
        framed(b"HTTP/1.1 200 OK", b"Content-Length: {length}") + THEN_CLOSE_ANSWER
    )
    assert closed


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b"zz\r\n", b"400 Bad Request"),
        (b"-1\r\n", b"400 Bad Request"),
        (b"0x21\r\n", b"400 Bad Request"),
        (b"\r\n", b"400 Bad Request"),
        (b"\x0021\r\n", b"400 Bad Request"),
        (b"10\r\n" + BODY[:16] + b"11\r\n", b"400 Bad Request"),
        (chunked(BODY, trailer=b"bad\r\n")[:-2], b"400 Bad Request"),
        (chunked(BODY, trailer=b" x\r\n")[:-2], b"400 Bad Request"),
        (chunked(BODY, trailer=b"\0junk\r\n")[:-2], b"400 Bad Request"),
        (b"41\r\n", b"413 Content too large"),
        (b"40\r\n" + b"a" * 0x40 + b"\r\n1\r\n", b"413 Content too large"),
    ],
    ids=[
        "not-hex",
        "negative",
        "0x",
        "empty-line",
        "nul-led",
        "no-line-end",
        "trailer-no-colon",
        "trailer-continuation",
        "trailer-nul",
        "past-cap",
        "past-cap-summed",
    ],
)
def test_a_chunked_body_core_cannot_read_is_refused_and_closes(
    body: bytes, status: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bitcoind` v32.1.0 answers each so, as soon as it is read.

    400 for what it cannot read, and 413 past the cap, lowered to 0x40
    here so that a body at it is small to send.
    """
    monkeypatch.setattr(connection_module, "MAX_BODY_BYTES", 0x40)
    reply, closed = conversation(request(CHUNKED_FIELD, body))
    assert reply == no_store(status)
    assert closed


def test_a_size_line_past_its_limit_is_400() -> None:
    """A size line is a line, held to `MAX_HEADER_BYTES` as any other is.

    `bitcoind` v32.1.0 answers one of 8193 octets before its line feed
    400; one of 8192 is waited on.
    """
    outcome, messages, closed = drive(
        [request(CHUNKED_FIELD, b"0" * MAX_HEADER_BYTES)], timeout=0.5
    )
    assert outcome == "waiting"
    assert not messages
    assert not closed
    reply, closed = conversation(request(CHUNKED_FIELD, b"0" * (MAX_HEADER_BYTES + 1)))
    assert reply == no_store(b"400 Bad Request")
    assert closed


def test_an_empty_line_first_in_the_buffer_is_empty() -> None:
    r"""A line feed at the buffer's start ends a line of nothing.

    Whatever the buffer ends with: here the body, a lone `\r`.
    """
    head = parse_request_head(b"POST / HTTP/1.1\nContent-Length: 1\n\n\r")
    assert head.length == 1
    assert head.consumed == len(b"POST / HTTP/1.1\nContent-Length: 1\n\n")


def test_a_chunked_body_at_the_cap_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """`MAX_BODY_BYTES` of data is read whole, as Core reads it."""
    monkeypatch.setattr(connection_module, "MAX_BODY_BYTES", 0x40)
    body = BODY + b" " * (0x40 - len(BODY))
    _, messages, _ = drive([request(CHUNKED_FIELD, chunked(body))])
    assert messages == [(json.loads(BODY), 0)]


def test_a_trailer_is_counted_on_from_the_header_section() -> None:
    """`m_consumed` is the request's, so the trailer adds to it.

    What `bitcoind` v32.1.0 answers `trailer-8192` and `trailer-8193`
    (issue #1972), the second 400. It is refused on the line that passes
    the count, the last sent, so that nothing is left unread.
    """
    fields = CHUNKED_FIELD + b"Connection: close\r\n"
    counted = len(b"Host: x\r\n" + RPCAUTH_LINE + fields + b"\r\n")
    field = b"X: " + b"a" * (MAX_HEADER_BYTES - counted - 3 - 4)
    reply, closed = conversation(
        request(fields, chunked(BODY, trailer=field + b"\r\n"))
    )
    assert reply.startswith(b"HTTP/1.1 200 OK")
    assert closed
    reply, closed = conversation(
        request(fields, chunked(BODY, trailer=field + b"a\r\n"))
    )
    assert reply == no_store(b"400 Bad Request", b"Connection: close")
    assert closed


def test_a_line_arriving_in_pieces_is_searched_from_where_the_last_read_ended() -> None:
    """`scan` is how much of the buffer is known to hold no line feed."""
    buffer = bytearray(b"POST / HT")
    lines = connection_module._LineReader(buffer)
    assert lines.readln() is None
    assert lines.scan == len(buffer)
    buffer += b"TP/1.1\r\nX"
    assert lines.readln() == b"POST / HTTP/1.1"
    assert lines.scan == 0
    assert lines.consumed == len(b"POST / HTTP/1.1\r\n")


@pytest.mark.parametrize(
    "fields",
    [b"Proxy-Connection: keep-alive\r\n", b"Connection: close\r\n"],
    ids=["proxy-keep-alive", "close"],
)
def test_a_refused_trailer_is_answered_by_the_request_s_own_fields(
    fields: bytes,
) -> None:
    """Trailer fields read before the refusal are not kept, so frame nothing.

    What `bitcoind` v32.1.0 answers `trailer-proxy-keep-alive-refused`
    and `trailer-close-refused`: a 400 with no `Connection` field.
    """
    data = request(CHUNKED_FIELD, chunked(BODY)[:-2] + fields + b"bad\r\n")
    reply, closed = conversation(data)
    assert reply == no_store(b"400 Bad Request")
    assert closed


@pytest.mark.parametrize("value", [b"100-continue", b"foo"])
def test_an_expectation_is_not_read(value: bytes) -> None:
    """`Expect` is a field like any other: no `100 Continue`, and no 417.

    What `bitcoind` v32.1.0 answers either value with: the request's
    answer, with nothing before it.
    """
    fields = b"Expect: " + value + b"\r\nConnection: close\r\n"
    reply, closed = conversation(good(b"HTTP/1.1", fields))
    assert reply == framed(
        b"HTTP/1.1 200 OK", b"Content-Length: {length}", b"Connection: close"
    )
    assert closed


def test_a_content_length_at_the_cap_is_read() -> None:
    """`MAX_BODY_BYTES` itself is no 413."""
    at_cap = b"Content-Length: %d\r\n" % MAX_BODY_BYTES
    assert parse_request_head(request(at_cap, b"")).length == MAX_BODY_BYTES


# issue #1957: section 7 of the btclib-org standard, *A refusal carries no
# secret*, for the Authorization header a head holds

_CREDENTIAL = "Basic YWxpY2U6WnQ5cUsybVdwN1J0"


def assert_free_of_credential(*texts: str) -> None:
    """Assert that no 12-character piece of `_CREDENTIAL` is in `texts`."""
    size = 12
    pieces = [
        _CREDENTIAL[i : i + size].lower() for i in range(len(_CREDENTIAL) - size + 1)
    ]
    for text in texts:
        for piece in pieces:
            assert piece not in text.lower()


def test_a_head_prints_no_authorization_in_its_repr_or_str() -> None:
    """`raw` and `fields` hold the header, so neither shows in a repr."""
    head = head_with(b"Authorization: " + _CREDENTIAL.encode() + b"\r\n")
    assert head.authorization == _CREDENTIAL
    assert_free_of_credential(repr(head), str(head))


def test_a_refused_header_field_is_named_by_index_and_length() -> None:
    """A field with no colon is refused by its index, not echoed."""
    line = b"Authorization " + _CREDENTIAL.encode()
    with pytest.raises(MalformedRequestHeadError) as refused:
        head_with(b"Host: x\r\n" + line + b"\r\n")
    assert_free_of_credential(str(refused.value), repr(refused.value))
    assert str(refused.value).endswith(f"header field 1 of {len(line)} octets")


def test_a_refused_chunk_size_line_is_named_by_its_length() -> None:
    """A size line may be a body's, so it is refused by its length alone."""
    line = b"Authorization: " + _CREDENTIAL.encode()
    with pytest.raises(MalformedRequestHeadError) as refused:
        chunk_size(line)
    assert_free_of_credential(str(refused.value), repr(refused.value))
    assert str(refused.value).endswith(f"chunk size line of {len(line)} octets")
