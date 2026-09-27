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
from btclib_node.exceptions import (
    IncompleteRequestHeadError,
    MalformedRequestHeadError,
    OversizedRequestBodyError,
    UnmetExpectationError,
)
from btclib_node.log import Logger
from btclib_node.rpc.auth import FAILED_ATTEMPT_DELAY, RpcAuth, RpcAuthEntry
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
from tests import RPCAUTH, RPCAUTH_LINE

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib_node.rpc.manager import RpcManager

BODY = b'{"jsonrpc":"2.0","id":"x","method":"getbestblockhash"}'


def fake_manager(connections: dict[int, Any]) -> SimpleNamespace:
    """Stand in for the `RpcManager` a connection reads, accepting `RPCAUTH`."""
    return SimpleNamespace(
        auth=RpcAuth((RpcAuthEntry.parse(RPCAUTH),)),
        logger=Logger(debug=True),
        messages=[],
        connections=connections,
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
                await asyncio.sleep(0.01)
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
        (b"HTTP/1.0", b"Connection: Keep-Alive, x\r\n", True),
        (b"HTTP/0.9", b"", False),
        (b"HTTP/1.1", b"", True),
        (b"HTTP/1.9", b"", True),
        (b"HTTP/1.1", b"Connection: close\r\n", False),
        (b"HTTP/1.1", b"Connection: CLOSE \t\r\n", False),
        (b"HTTP/1.1", b"Connection: close, x\r\n", True),
    ],
    ids=[
        "1.0-bare-closes",
        "1.0-keep-alive-stays",
        "1.0-keep-alive-prefix-stays",
        "0.9-bare-closes",
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

    libevent's `evhttp_send_done`, as `RpcConnection._frame` cites it; each
    row is what `bitcoind` v31.1.0 does. A second request, sent here
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


def refused(data: bytes) -> tuple[bytes, float, bool, list[Any], list[tuple[Any, ...]]]:
    """Send `data` to a `RpcConnection.run` expecting a refusal, and read it.

    Returns the reply, up to the end of the body its own
    `Content-Length` counts, the seconds from `run` starting to the
    reply arriving, whether the connection closed after it, what was
    queued for `handle_rpc`, and every warning logged.
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
        manager.logger = SimpleNamespace(warning=lambda *args: warnings.append(args))
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


ONLY_POST = b"JSONRPC server handles only POST requests"
NOT_IMPLEMENTED_PAGE = (
    b"<HTML><HEAD>\n<TITLE>501 Not Implemented</TITLE>\n"
    b"</HEAD><BODY>\n<H1>Not Implemented</H1>\n</BODY></HTML>\n"
)


@pytest.mark.parametrize(
    ("method", "target", "status", "body"),
    [
        (b"GET", b"/", b"405 Method Not Allowed", ONLY_POST),
        (b"HEAD", b"/wallet/w", b"405 Method Not Allowed", ONLY_POST),
        (b"PUT", b"/wallet/", b"405 Method Not Allowed", ONLY_POST),
        (b"DELETE", b"/", b"405 Method Not Allowed", b""),
        (b"DELETE", b"/x", b"405 Method Not Allowed", b""),
        (b"POST", b"/x", b"404 Not Found", b""),
        (b"GET", b"/x", b"404 Not Found", b""),
        (b"POST", b"/wallet", b"404 Not Found", b""),
        (b"POST", b"/?a=1", b"404 Not Found", b""),
        (b"POST", b"/rest/chaininfo.json", b"404 Not Found", b""),
    ],
)
def test_a_method_or_a_path_core_refuses_is_refused_before_the_credential(
    method: bytes, target: bytes, status: bytes, body: bytes
) -> None:
    """A method or a path `bitcoind` refuses gets its reply, credential or not.

    Each row is what a real `bitcoind` v31.1.0 answers, with no
    `Authorization` as with a good one, and it keeps an HTTP/1.1
    connection open afterwards; `DELETE` is refused on any path and `GET`
    off `/` is a 404, the order `_refusal`'s own docstring cites.
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
            b"HTTP/1.1 " + status + b"\r\nContent-Length: %d\r\n\r\n" % len(body) + body
        )
        assert not closed
        assert not messages
        assert not warnings


def test_a_refusal_closes_where_the_request_asked_as_a_401_does() -> None:
    """`Connection: close` on the request is honoured on a 404 too."""
    headers = b"Connection: close\r\nContent-Length: %d\r\n" % len(BODY)
    reply, _, closed, _, _ = refused(request(headers, target=b"/x"))
    assert reply == (
        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    )
    assert closed


@pytest.mark.parametrize("method", [b"OPTIONS", b"PATCH", b"TRACE", b"FOO", b"post"])
def test_a_method_libevent_does_not_know_is_501_and_closes(method: bytes) -> None:
    """A method outside libevent's own five is 501, and the connection closes.

    What a real `bitcoind` v31.1.0 answers, on a path it would answer as
    well as on one it would not, even where the request asked to be kept
    alive.
    """
    for target in (b"/", b"/x"):
        data = request(
            b"Content-Length: %d\r\n" % len(BODY),
            auth=b"",
            method=method,
            target=target,
        )
        reply, _, closed, messages, _ = refused(data)
        assert reply == (
            b"HTTP/1.1 501 Not Implemented\r\nConnection: close\r\n"
            b"Content-Length: %d\r\n\r\n"
            % len(NOT_IMPLEMENTED_PAGE)
            + NOT_IMPLEMENTED_PAGE
        )
        assert closed
        assert not messages


def error_page(status: bytes) -> bytes:
    """Return libevent's error page for `status`, as `bitcoind` writes it."""
    reason = status.partition(b" ")[2]
    return (
        b"<HTML><HEAD>\n<TITLE>" + status + b"</TITLE>\n"
        b"</HEAD><BODY>\n<H1>" + reason + b"</H1>\n</BODY></HTML>\n"
    )


def closing(status: bytes) -> bytes:
    """Return the reply libevent closes a connection it cannot frame with."""
    page = error_page(status)
    return (
        b"HTTP/1.1 " + status + b"\r\nConnection: close\r\n"
        b"Content-Length: %d\r\n\r\n" % len(page) + page
    )


def test_the_501_page_is_libevent_s() -> None:
    """`error_page` builds the 501 page pinned above."""
    assert error_page(b"501 Not Implemented") == NOT_IMPLEMENTED_PAGE


@pytest.mark.parametrize(
    "line",
    [
        b"POST /",
        b"POST / HTTP/2.0",
        b"POST / FOO",
        b"POST / HTTP/1.1x",
        b"POST / HTTP/1",
        b"POST  HTTP/1.1",
        b"A / HTTP/1.1",
        b"GET / HTTP/1.",
    ],
)
def test_a_request_line_libevent_refuses_is_400_and_closes(line: bytes) -> None:
    """A request line `bitcoind` v31.1.0 answers 400 is answered 400 here too.

    Before the credential and before any body is read, and the
    connection closed: the first three rows are ISS 1086's.
    """
    for auth in (b"", RPCAUTH_LINE):
        data = line + b"\r\nHost: x\r\n" + auth
        data += b"Content-Length: %d\r\n\r\n" % len(BODY) + BODY
        reply, _, closed, messages, warnings = refused(data)
        assert reply == closing(b"400 Bad Request")
        assert closed
        assert not messages
        assert not warnings


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
    assert reply.endswith(closing(b"400 Bad Request"))
    assert closed


@pytest.mark.parametrize("value", [b"-1", b"abc", b"", b"1 2", b"0x10", b"1.0"])
def test_a_content_length_libevent_refuses_is_400_and_closes(value: bytes) -> None:
    """A `Content-Length` `bitcoind` v31.1.0 answers 400 is answered 400 here.

    `-1` and `abc` are ISS 1086's rows.
    """
    for auth in (b"", RPCAUTH_LINE):
        data = request(b"Content-Length: " + value + b"\r\n", BODY, auth=auth)
        reply, _, closed, messages, _ = refused(data)
        assert reply == closing(b"400 Bad Request")
        assert closed
        assert not messages


def test_a_content_length_past_the_cap_is_413_and_closes() -> None:
    """Past `MAX_BODY_BYTES`, Core's `MAX_SIZE`, is libevent's 413, unread."""
    over = b"Content-Length: %d\r\n" % (MAX_BODY_BYTES + 1)
    reply, _, closed, messages, _ = refused(request(over, b"", auth=b""))
    assert reply == closing(b"413 Request Entity Too Large")
    assert closed
    assert not messages


@pytest.mark.parametrize("value", [b"+%d", b" \t%d \t", b"%d\r\nContent-Length: abc"])
def test_a_content_length_libevent_reads_is_read(value: bytes) -> None:
    """A sign, blanks and a duplicate field are read as `bitcoind` reads them.

    `strtoll` takes the sign and leading white space, the trailing blanks
    are trimmed off the value, and the first of two fields is the one
    read.
    """
    headers = b"Content-Length: " + value % len(BODY) + b"\r\n"
    _, messages, _ = drive([request(headers)])
    assert messages == [(json.loads(BODY), 0)]


@pytest.mark.parametrize("method", [b"HEAD", b"TRACE", b"FOO"])
def test_a_method_libevent_reads_no_body_for_has_no_content_length(
    method: bytes,
) -> None:
    """No `Content-Length` is read for a method libevent reads no body for.

    So a malformed one is not refused, and bytes after the header section
    are the next request's, as `bitcoind` v31.1.0 answers a `HEAD` whose
    "body" is a second request.
    """
    data = method + b" / HTTP/1.1\r\nContent-Length: abc\r\n\r\n"
    assert parse_request_head(data + b"next").length == 0


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
    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)


def conversation(data: bytes) -> tuple[bytes, bool]:
    """Write `data` to a connection and return what it answers, to the close.

    A request `run` queues is answered `ANSWER`, as `handle_rpc` would
    answer it. Returns everything written back, and whether the
    connection closed. Where a request is to be kept open, `data` ends
    in `THEN_CLOSE`, whose answer is what says it was kept: the
    connection is left nothing unread where it closes, so a TCP
    `socketpair`, which Windows emulates one with, closes it cleanly.
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
        if manager.messages:
            await conn.async_send(HttpReply(OK, ANSWER))
        await sender
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
        (good(b"HTTP/0.9"), framed(b"HTTP/0.9 200 OK")),
        (good(b"HTTP/1.-1"), framed(b"HTTP/1.-1 200 OK")),
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
            good(b"HTTP/+01.+01", b"Connection: close\r\n"),
            framed(
                b"HTTP/1.1 200 OK", b"Content-Length: {length}", b"Connection: close"
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
            good(b"HTTP/1.5") + THEN_CLOSE,
            framed(b"HTTP/1.5 200 OK", b"Content-Length: {length}") + THEN_CLOSE_ANSWER,
        ),
    ],
    ids=[
        "1.0",
        "0.9",
        "1.-1",
        "1.0-close",
        "1.5-close",
        "signed-and-zero-padded",
        "1.0-keep-alive",
        "1.5-kept",
    ],
)
def test_an_answer_is_written_in_its_request_s_version(
    data: bytes, expected: bytes
) -> None:
    """The status line echoes the request's version, as `bitcoind` does.

    Issue #1127: before HTTP/1.1, no `Content-Length` unless the request
    asked `keep-alive`, and the connection closed unless it did; each row
    is what `bitcoind` v31.1.0 answers.
    """
    reply, closed = conversation(data)
    assert reply == expected
    assert closed


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
    assert reply == b"HTTP/1.0 404 Not Found\r\n\r\n"
    assert closed


@pytest.mark.parametrize(
    ("data", "status_line"),
    [
        (b"POST / HTTP/1.0\r\nContent-Length: abc\r\n\r\n", b"HTTP/1.1"),
        (b"POST / HTTP/0.9\r\nContent-Length: abc\r\n\r\n", b"HTTP/1.1"),
        (b"POST / HTTP/1.5\r\nContent-Length: abc\r\n\r\n", b"HTTP/1.5"),
        (b"POST 1:x HTTP/1.5\r\n\r\n", b"HTTP/1.5"),
        (b"POST / HTTP/2.0\r\n\r\n", b"HTTP/1.1"),
        (b"FOO / HTTP/1.0\r\n\r\n", b"HTTP/1.1"),
    ],
    ids=["1.0", "0.9", "1.5", "1.5-target", "2.0", "1.0-501"],
)
def test_libevent_s_own_page_is_http_1_1_for_a_version_with_a_zero(
    data: bytes, status_line: bytes
) -> None:
    """`evhttp_send_error`'s page answers a zero in the version as HTTP/1.1.

    And a version it never read too; any other keeps its own. What
    `bitcoind` v31.1.0 answers, and it closes after each.
    """
    reply, closed = conversation(data)
    assert reply.startswith(status_line + b" ")
    assert (
        reply.partition(b" ")[2]
        == closing(
            b"501 Not Implemented" if data.startswith(b"FOO") else b"400 Bad Request"
        ).partition(b" ")[2]
    )
    assert closed


@pytest.mark.parametrize(
    "token",
    [b"HTTP/1." + b"1" * 5000, b"HTTP/1.2147483648", b"HTTP/-2147483649.1"],
    ids=["past-4300-digits", "past-int", "past-int-negative"],
)
def test_a_version_number_past_a_c_int_is_refused(token: bytes) -> None:
    """A number `sscanf`'s `%d` leaves undefined is a 400, not an exception."""
    with pytest.raises(MalformedRequestHeadError):
        parse_request_head(b"POST / " + token + b"\r\n\r\n")
    assert parse_request_head(b"POST / HTTP/1.2147483647\r\n\r\n").version == (
        1,
        2147483647,
    )


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (b"1" * 5000, OversizedRequestBodyError),
        (b"99999999999999999999", OversizedRequestBodyError),
        (b"-" + b"1" * 5000, MalformedRequestHeadError),
    ],
    ids=["past-4300-digits", "past-strtoll", "negative-past-4300-digits"],
)
def test_a_content_length_past_strtoll_is_refused_as_libevent_refuses_it(
    value: bytes, error: type[Exception]
) -> None:
    """`strtoll` clamps: 413 for a length past its range, 400 below it.

    What `bitcoind` v31.1.0 answers, and not the `ValueError` `int`
    raises past 4300 digits.
    """
    with pytest.raises(error):
        parse_request_head(b"POST / HTTP/1.1\r\nContent-Length: " + value + b"\r\n\r\n")


# A run of zeros two digit quantifiers could split between them, which
# backtracks in quadratic time where what follows it fails: 60000 of them
# took seconds that way, and take about a millisecond read by one. Past
# what `MAX_HEADER_BYTES` lets a header section hold, so each parser is
# driven directly; a chunk-size line is bounded by `MAX_BODY_BYTES` alone.
LONG_ZEROS = b"0" * 60000


@pytest.mark.parametrize(
    ("read", "expected"),
    [
        (
            lambda: connection_module._is_proxy_request(
                b"POST", b"http://h:" + LONG_ZEROS + b"x/"
            ),
            MalformedRequestHeadError,
        ),
        (
            lambda: connection_module._is_proxy_request(
                b"POST", b"http://h:" + LONG_ZEROS + b"80/"
            ),
            True,
        ),
        (
            lambda: connection_module._is_proxy_request(
                b"CONNECT", b"h:" + LONG_ZEROS + b"x"
            ),
            MalformedRequestHeadError,
        ),
        (
            lambda: connection_module._content_length(" " + "0" * 60000 + "x"),
            MalformedRequestHeadError,
        ),
        (lambda: connection_module._content_length("0" * 60000), 0),
        (
            lambda: connection_module._content_length("-" + "0" * 60000 + "3"),
            MalformedRequestHeadError,
        ),
        (
            lambda: connection_module._chunk_size(LONG_ZEROS + b"x"),
            OversizedRequestBodyError,
        ),
        (lambda: connection_module._chunk_size(b" " + LONG_ZEROS + b"21"), 0x21),
    ],
    ids=[
        "port-then-x",
        "port-80",
        "connect-port-then-x",
        "length-then-x",
        "length-0",
        "length-negative",
        "chunk-size-then-x",
        "chunk-size-21",
    ],
)
def test_a_long_run_of_zeros_is_read_in_linear_time(
    read: Callable[[], object], expected: object
) -> None:
    """A port, a length or a chunk size of leading zeros is read at once.

    The head is read before any credential, on the listener's one loop,
    so a parse that takes seconds is a stall any client can cause. The
    bound is generous: it is there to fail a quadratic parse, not to
    measure a linear one.
    """
    start = time.perf_counter()
    if isinstance(expected, type):
        with pytest.raises(expected):
            read()
    else:
        assert read() == expected
    assert time.perf_counter() - start < 2


def test_a_negative_zero_content_length_is_zero() -> None:
    """`strtoll` reads `-0` as 0, which libevent accepts."""
    data = b"POST / HTTP/1.1\r\nContent-Length: -00\r\n\r\n"
    assert parse_request_head(data).length == 0


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
        b"/",
        b"/a:b",
        b"a/b:c",
        b"a:b",
        b"/?a:b#c:d",
        b"/a b",
        b"/%zz",
        b"http://h",
        b"http://",
        b"http:/",
        b"http://u:p@h:8332/x?y#z",
        b"http://%41%3a@h%41:/",
        b"http://h:0000080/",
        b"http://h:65535/",
        b"http://[::1]:80/",
        b"http://[0000::ffff:1.2.3.4]/",
        b"http://[1:2:3:4:5:6:7::]/",
        b"http://[v1f.a:b]/",
        b"http://[v1.]/",
        b"http://1.2.3.4/",
        b"//h/",
        b"a+b.c-d://h/",
    ],
)
def test_a_request_target_libevent_accepts_is_read(target: bytes) -> None:
    """What `evhttp_uri_parse_with_flags` accepts, not answered 400 here."""
    head = parse_request_head(b"POST " + target + b" HTTP/1.1\r\n\r\n")
    assert head.target == target


@pytest.mark.parametrize(
    "target",
    [
        b"1:x",
        b"1:b:c/d:e",
        b"http://h:65536/",
        b"http://h:99999/",
        b"http://h:" + b"9" * 5000 + b"/",
        b"http://a b/",
        b"http://h%4/",
        b"http://h%/",
        b"http://u^@h/",
        b"http://u%4@h/",
        b"http://h@h@h/",
        b"http://[zz]/",
        b"http://[]/",
        b"http://[v]/",
        b"http://[v1]/",
        b"http://[vg.a]/",
        b"http://[v1.a/b]/",
        b"http://[00000::1]/",
        b"http://[::1:2:3:4:5:6:7:8]/",
        b"http://[::1%25lo0]/",
        b"http://[::1\0]/",
        b"http://[::\xe9]/",
        b"http://::1/",
        b"//h:x/",
    ],
)
def test_a_request_target_libevent_refuses_is_400(target: bytes) -> None:
    """What `evhttp_uri_parse_with_flags` refuses is 400, closing (issue #1125).

    `1:x` and a port past 65535 are what `bitcoind` v31.1.0 answers 400.
    """
    with pytest.raises(MalformedRequestHeadError, match="request-target"):
        parse_request_head(b"POST " + target + b" HTTP/1.1\r\n\r\n")
    reply, closed = conversation(b"POST " + target + b" HTTP/1.1\r\n\r\n")
    assert reply == closing(b"400 Bad Request")
    assert closed


@pytest.mark.parametrize(
    ("target", "error"),
    [
        (b"/", False),
        (b"h:1", False),
        (b"u@h:1/x?y", False),
        (b"h:1/x:y:z", False),
        (b"[::1]:8332", False),
        (b"h:99999", True),
        (b"h^", True),
        (b"[zz]", True),
    ],
)
def test_a_connect_target_is_an_authority(target: bytes, *, error: bool) -> None:
    """`CONNECT` reads its target up to the first `/`, `?` or `#`, as libevent.

    `evhttp_uri_parse_authority`, whatever follows it unread.
    """
    data = b"CONNECT " + target + b" HTTP/1.1\r\n\r\n"
    if error:
        with pytest.raises(MalformedRequestHeadError, match="request-target"):
            parse_request_head(data)
    else:
        assert not parse_request_head(data).proxy


@pytest.mark.parametrize(
    ("target", "proxy"),
    [
        (b"http://h/", True),
        (b"HTTPS://h/", True),
        (b"http://", True),
        (b"http:/", False),
        (b"http:x", False),
        (b"ftp://h/", False),
        (b"//h/", False),
        (b"/", False),
    ],
)
def test_an_http_target_with_an_authority_is_a_proxy_request(
    target: bytes, *, proxy: bool
) -> None:
    """Set as libevent sets `EVHTTP_PROXY_REQUEST`, Core naming no host."""
    assert parse_request_head(b"POST " + target + b" HTTP/1.1\r\n\r\n").proxy is proxy


@pytest.mark.parametrize(
    "fields",
    [b"", b"Connection: keep-alive\r\n", b"Proxy-Connection: keep-alive\r\n"],
    ids=["bare", "connection-keep-alive", "proxy-connection-keep-alive"],
)
def test_a_proxy_request_is_answered_without_connection_and_closed(
    fields: bytes,
) -> None:
    """An absolute-form `http` target: 404 with no `Connection`, then a close.

    What `bitcoind` v31.1.0 answers (issue #1125), whatever the request
    asks: libevent reads `Proxy-Connection` off the answer too, which
    Core never writes. A 400 to one is framed the same way, its page's
    own `Connection: close` kept only where `Proxy-Connection` asks
    `keep-alive`.
    """
    data = b"POST http://127.0.0.1:1/ HTTP/1.1\r\n" + fields + b"\r\n"
    reply, closed = conversation(data)
    assert reply == b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
    assert closed
    data = b"POST http://h/ HTTP/1.0\r\nContent-Length: abc\r\n" + fields + b"\r\n"
    reply, closed = conversation(data)
    page = closing(b"400 Bad Request")
    if not fields.startswith(b"Proxy-Connection"):
        page = page.replace(b"Connection: close\r\n", b"")
    assert reply == page
    assert closed


@pytest.mark.parametrize("target", [b"ftp://h/", b"http:/", b"/x"])
def test_a_404_that_is_not_a_proxy_request_keeps_the_connection(target: bytes) -> None:
    """A 404 keeps an HTTP/1.1 connection, the next request answered on it."""
    reply, closed = conversation(b"POST " + target + b" HTTP/1.1\r\n\r\n" + THEN_CLOSE)
    assert reply == b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n" + (
        THEN_CLOSE_ANSWER
    )
    assert closed


NOT_IMPLEMENTED_UNFRAMED = (
    b"HTTP/1.1 501 Not Implemented\r\nConnection: close\r\n\r\n" + NOT_IMPLEMENTED_PAGE
)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"CONNECT / HTTP/1.1\r\n\r\n", NOT_IMPLEMENTED_UNFRAMED),
        (b"CONNECT h:1 HTTP/1.0\r\n\r\n", NOT_IMPLEMENTED_UNFRAMED),
        (b"CONNECT / HTTP/0.9\r\n\r\n", NOT_IMPLEMENTED_UNFRAMED),
        (b"CONNECT / HTTP/1.1\r\nConnection: close\r\n\r\n", NOT_IMPLEMENTED_UNFRAMED),
        (
            b"CONNECT / HTTP/1.1\r\nContent-Length: 2\r\n\r\nxy",
            NOT_IMPLEMENTED_UNFRAMED,
        ),
        (
            b"CONNECT / HTTP/1.1\r\nContent-Length: abc\r\n\r\n",
            b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"
            + error_page(b"400 Bad Request"),
        ),
        (
            b"CONNECT / HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % (MAX_BODY_BYTES + 1),
            b"HTTP/1.1 413 Request Entity Too Large\r\nConnection: close\r\n\r\n"
            + error_page(b"413 Request Entity Too Large"),
        ),
    ],
    ids=["1.1", "1.0", "0.9", "close", "body", "length-abc", "length-past-cap"],
)
def test_a_connect_is_answered_unframed_and_kept(data: bytes, expected: bytes) -> None:
    """The page libevent writes, with no `Content-Length`, and a next request.

    What `bitcoind` v31.1.0 answers a `CONNECT` (issue #1125), whatever
    its version or `Connection`: `evhttp_is_request_connection_close`
    never closes one, and `evhttp_response_needs_body` frames no body
    for one.
    """
    reply, closed = conversation(data + THEN_CLOSE)
    assert reply == expected + THEN_CLOSE_ANSWER
    assert closed


def test_a_connect_libevent_cannot_read_the_line_of_reads_the_next_line() -> None:
    """A refused `CONNECT` request line is 400, and the next line a request.

    `bitcoind` v31.1.0 answers `CONNECT h:99999` 400 and reads on from
    the line after it: here the empty line, which is a request line too
    short, answered 400 and closing.
    """
    reply, closed = conversation(b"CONNECT h:99999 HTTP/1.1\r\n\r\n" + THEN_CLOSE)
    page = error_page(b"400 Bad Request")
    assert reply == (
        b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"
        + page
        + closing(b"400 Bad Request")
    )
    assert closed
    head = parse_request_head(b"CONNECT / HTTP/1.1\r\n\r\n")
    assert head.serialize() == b"CONNECT / HTTP/1.1\r\n\r\n"


@pytest.mark.parametrize("terminator", [b"\r\n\r\n", b"\n\n", b"\r\n\n", b"\n\r\n"])
def test_a_refused_connect_line_is_consumed_to_its_own_line_end(
    terminator: bytes,
) -> None:
    """What `run` trims is the line and its own ending, as libevent reads it."""
    with pytest.raises(MalformedRequestHeadError):
        parse_request_head(b"CONNECT h:99999 HTTP/1.1" + terminator)
    head = connection_module._HeadReader(
        bytearray(b"CONNECT h:99999 HTTP/1.1" + terminator)
    ).read()
    assert head is not None
    assert (
        head.serialize()
        == b"CONNECT h:99999 HTTP/1.1" + terminator.partition(b"\n")[0] + b"\n"
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
    """A bare LF ends a line, as `EVBUFFER_EOL_CRLF` ends one (issue #1150).

    Each is what `bitcoind` v31.1.0 answers, where CRLF alone was framed
    here before.
    """
    _, messages, _ = drive([data])
    assert messages == [(json.loads(BODY), 0)]
    head = parse_request_head(data)
    assert head.serialize() == data[: -len(BODY)]
    assert head.consumed == len(data) - len(BODY)


def test_only_one_carriage_return_is_dropped_before_a_line_feed() -> None:
    r"""`POST / HTTP/1.1\r\r\n` keeps a CR in its version, and is 400.

    What `bitcoind` v31.1.0 answers.
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
    """`send_and_wait` writes `Connection: close`, and closes.

    What Core's `HTTPRequest::WriteReply` writes once shutdown has
    begun, whatever the request asked for: here a request that is kept
    alive otherwise.
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
    assert reply == framed(
        b"HTTP/1.1 200 OK", b"Connection: close", b"Content-Length: {length}"
    )
    assert ours.fileno() == -1


# issue #1126: the header section as libevent's `evhttp_parse_headers_`
# reads it, and a chunked body as `evhttp_handle_chunked_read` does


def head_with(fields: bytes) -> RequestHead:
    """Parse a `POST /` head of `fields`, which end at the section's end."""
    return parse_request_head(b"POST / HTTP/1.1\r\n" + fields + b"\r\n")


@pytest.mark.parametrize(
    ("fields", "name", "value"),
    [
        (b"Connection:\tclose\r\n", b"Connection", "\tclose"),
        (b"Connection:  \tclose\r\n", b"Connection", "\tclose"),
        (b"Connection: close \t\r\n", b"Connection", "close"),
        (b"Connection:\r\n close\r\n", b"Connection", " close"),
        (b"Connection: keep\r\n\t-alive \r\n", b"Connection", "keep -alive"),
        (b"Connection: close\0junk\r\n", b"Connection", "close"),
        (b"Connection\t: close\r\n", b"Connection", None),
        (b"connection: close\r\nConnection: keep-alive\r\n", b"Connection", "close"),
        (b"X: a\r b\r\n", b"x", "a\r b"),
        (b"X: a\r\r b\r\n", b"x", "a\r\r b"),
        (b": x\r\n", b"", "x"),
        (b"X Y: z\r\n", b"X Y", "z"),
    ],
    ids=[
        "leading-tab-kept",
        "leading-spaces-dropped",
        "trailing-blanks-dropped",
        "continuation",
        "continuation-trimmed",
        "nul-ends-value",
        "tab-before-colon",
        "first-of-two",
        "cr-then-space",
        "crs-then-space",
        "empty-key",
        "space-in-key",
    ],
)
def test_a_field_is_read_as_libevent_reads_it(
    fields: bytes, name: bytes, value: str | None
) -> None:
    """A field's key runs to its first colon, and only spaces lead a value.

    What `bitcoind` v31.1.0 reads: a value keeps a leading tab, so
    `Connection:<TAB>close` is not `close`; a line starting with a blank
    continues the field before it; a field is a C string, ending at a
    NUL; and a name is looked up by its first field.
    """
    assert head_with(fields).field(name) == value


@pytest.mark.parametrize(
    "fields",
    [
        b"garbage\r\n",
        b"X\r: y\r\n",
        b"X: a\rb\r\n",
        b"X: a\r\r\n",
        b" x\r\n",
        b"Connection\0: keep-alive\r\n",
    ],
    ids=[
        "no-colon",
        "cr-in-key",
        "bare-cr-in-value",
        "cr-ending-value",
        "continuation-first",
        "nul-in-key",
    ],
)
def test_a_field_libevent_refuses_is_400_and_closes(fields: bytes) -> None:
    """What `evhttp_parse_headers_` refuses is refused, as `bitcoind` does."""
    with pytest.raises(MalformedRequestHeadError):
        head_with(fields)
    # refused on the last octet sent, so that nothing is left unread
    reply, closed = conversation(b"POST / HTTP/1.1\r\n" + fields)
    assert reply == closing(b"400 Bad Request")
    assert closed


def test_a_line_starting_with_a_nul_ends_the_section() -> None:
    """A NUL-led line is an empty C string to libevent: the section's end."""
    data = b"POST / HTTP/1.1\r\nContent-Length: 2\r\n\0junk\r\n{}"
    head = parse_request_head(data)
    assert head.length == 2
    assert head.consumed == len(data) - 2


def test_more_fields_than_http_client_reads_are_read() -> None:
    """A section is bounded by its size, as libevent bounds it, not by count."""
    fields = b"X: y\r\n" * 101 + b"Content-Length: %d\r\n" % len(BODY)
    _, messages, _ = drive([request(fields)])
    assert messages == [(json.loads(BODY), 0)]


def sized(total: int, eol: bytes = b"\r\n", *, ended: bool = True) -> bytes:
    """Return a head whose lines hold `total` octets, their endings apart.

    A request line and one field, ended by `eol`, and the section's own
    end after them where `ended`.
    """
    line = b"POST / HTTP/1.1"
    field = b"X: " + b"a" * (total - len(line) - 3)
    return line + eol + field + (eol + eol if ended else b"")


def test_a_section_s_size_counts_its_lines_not_their_endings() -> None:
    """`MAX_HEADER_BYTES` of lines are read, whatever ends them.

    `bitcoind` v31.1.0 answers 400 past 8192, the count its libevent
    keeps in `headers_size`.
    """
    assert parse_request_head(sized(MAX_HEADER_BYTES)).error is None
    assert parse_request_head(sized(MAX_HEADER_BYTES, b"\n")).error is None
    many = b"POST / HTTP/1.1\r\n" + b"X:\r\n" * ((MAX_HEADER_BYTES - 15) // 2)
    assert parse_request_head(many + b"\r\n").error is None
    with pytest.raises(MalformedRequestHeadError, match="MAX_HEADER_BYTES"):
        parse_request_head(sized(MAX_HEADER_BYTES + 1))
    with pytest.raises(MalformedRequestHeadError, match="MAX_HEADER_BYTES"):
        parse_request_head(many + b"X:\r\n\r\n")


def test_a_section_past_its_size_is_refused_before_it_ends() -> None:
    """The line read so far counts, so the 400 needs no end of section.

    `evhttp_parse_headers_` and `evhttp_parse_firstline_` both count
    what is buffered of a line not yet ended.
    """
    with pytest.raises(IncompleteRequestHeadError):
        parse_request_head(sized(MAX_HEADER_BYTES, ended=False))
    with pytest.raises(MalformedRequestHeadError, match="MAX_HEADER_BYTES"):
        parse_request_head(sized(MAX_HEADER_BYTES + 1, ended=False))
    line = b"POST /" + b"a" * (MAX_HEADER_BYTES - 6)
    with pytest.raises(IncompleteRequestHeadError):
        parse_request_head(line)
    with pytest.raises(MalformedRequestHeadError, match="request line"):
        parse_request_head(line + b"a")
    reply, closed = conversation(sized(MAX_HEADER_BYTES + 1, ended=False))
    assert reply == closing(b"400 Bad Request")
    assert closed


def test_a_request_line_past_the_size_is_refused() -> None:
    """`evhttp_parse_firstline_` counts the request line on its own too."""
    line = b"POST /" + b"a" * (MAX_HEADER_BYTES - 15) + b" HTTP/1.0\r\n"
    assert parse_request_head(line + b"\r\n").target.endswith(b"a")
    with pytest.raises(MalformedRequestHeadError, match="request line"):
        parse_request_head(line.replace(b" ", b"a ", 1) + b"\r\n")


def test_a_refused_section_is_answered_by_the_fields_read_before_it() -> None:
    """A `Connection: close` read before the refused line frames the 400.

    `evhttp_make_header_response` moves `Connection: close` after
    `Content-Length` where the request asked to close, as `bitcoind`
    v31.1.0 answers `size-8193`.
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
    page = error_page(b"400 Bad Request")
    assert reply == (
        b"HTTP/1.1 400 Bad Request\r\nContent-Length: %d\r\n" % len(page)
        + b"Connection: close\r\n\r\n"
        + page
    )
    assert closed


@pytest.mark.parametrize(
    ("data", "expected", "closes"),
    [
        (
            good(b"HTTP/1.1", b"Connection:\tclose\r\n") + THEN_CLOSE,
            framed(b"HTTP/1.1 200 OK", b"Content-Length: {length}") + THEN_CLOSE_ANSWER,
            False,
        ),
        (
            good(b"HTTP/1.0", b"Connection:\tkeep-alive\r\n"),
            framed(b"HTTP/1.0 200 OK"),
            True,
        ),
    ],
    ids=["1.1-tab-close-kept", "1.0-tab-keep-alive-closed"],
)
def test_a_connection_value_led_by_a_tab_asks_nothing(
    data: bytes, expected: bytes, *, closes: bool
) -> None:
    """`bitcoind` v31.1.0 reads a tab-led value as neither of the two."""
    reply, closed = conversation(data)
    assert reply == expected
    assert closed
    assert closes != reply.endswith(THEN_CLOSE_ANSWER)


def test_a_connect_libevent_cannot_read_a_field_of_reads_the_next_line() -> None:
    """A refused `CONNECT` field is 400, the line after it the next request.

    What `bitcoind` v31.1.0 answers `connect-bad-field`: the page with
    no `Content-Length`, then `THEN_CLOSE`'s answer.
    """
    reply, closed = conversation(b"CONNECT h:1 HTTP/1.1\r\nbad\r\n" + THEN_CLOSE)
    assert reply == (
        b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"
        + error_page(b"400 Bad Request")
        + THEN_CLOSE_ANSWER
    )
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
        (b"Transfer-Encoding: ChUnKeD\r\n", b"POST", True, 0),
        (b"Transfer-Encoding:\tchunked\r\nContent-Length: 5\r\n", b"POST", False, 5),
        (b"Transfer-Encoding: gzip, chunked\r\n", b"POST", False, 0),
        (CHUNKED_FIELD, b"HEAD", False, 0),
    ],
    ids=["over-length", "any-case", "tab-led", "a-list", "head"],
)
def test_a_body_is_chunked_where_evhttp_get_body_says(
    fields: bytes, method: bytes, *, is_chunked: bool, length: int
) -> None:
    """`Transfer-Encoding: chunked`, any case, on a method with a body."""
    head = parse_request_head(method + b" / HTTP/1.1\r\n" + fields + b"\r\n")
    assert head.chunked == is_chunked
    assert head.length == length


@pytest.mark.parametrize(
    ("line", "size"),
    [
        (b"21", 0x21),
        (b"21 ;x", 0x21),
        (b"0x21", 0x21),
        (b"0X21", 0x21),
        (b"+21", 0x21),
        (b"  21", 0x21),
        (b"\t21", 0x21),
        (b"-0", 0),
        (b" ", 0),
        (b" x", 0),
        (b"f" * 40, MAX_BODY_BYTES + 1),
        (b"21;x", None),
        (b"-1", None),
        (b"zz", None),
        (b"0x", None),
        (b"0xg", None),
        (b" 0x", None),
        (b"\t", None),
    ],
)
def test_a_chunk_size_is_read_as_strtoll_reads_it(
    line: bytes, size: int | None
) -> None:
    """Base 16, a sign and a `0x` taken, and nothing after it but a space.

    With no digit `strtoll` stops where it started, which a leading
    space passes and a leading tab does not.
    """
    if size is None:
        with pytest.raises(OversizedRequestBodyError, match="chunk size"):
            connection_module._chunk_size(line)
    else:
        assert connection_module._chunk_size(line) == size


@pytest.mark.parametrize(
    "body",
    [
        chunked(BODY),
        chunked(BODY[:10], BODY[10:]),
        b"%x\r\n%s%x\r\n%s0\r\n\r\n" % (10, BODY[:10], len(BODY) - 10, BODY[10:]),
        b"\r\n\r\n" + chunked(BODY),
        b"\0ff\r\n" + chunked(BODY),
        chunked(BODY, trailer=b"X: y\r\n"),
    ],
    ids=["one", "two", "no-line-end", "empty-lines", "nul-led-line", "trailer"],
)
def test_a_chunked_body_is_decoded_and_dispatched(body: bytes) -> None:
    """The chunks' data, joined, is the body, as `bitcoind` v31.1.0 reads it.

    Sent three octets at a time, so each line is read across reads.
    """
    data = request(CHUNKED_FIELD, body)
    _, messages, _ = drive([data[i : i + 3] for i in range(0, len(data), 3)])
    assert messages == [(json.loads(BODY), 0)]


def test_a_trailer_s_fields_are_the_request_s() -> None:
    """A trailer can carry the credential, and ask to close.

    `evhttp_read_trailer` adds its fields to the request's, where
    `HTTPReq_JSONRPC` and `evhttp_send_done` look them up.
    """
    trailer = RPCAUTH_LINE + b"Connection: close\r\n"
    reply, closed = conversation(
        request(CHUNKED_FIELD, chunked(BODY, trailer=trailer), auth=b"")
    )
    assert reply == framed(
        b"HTTP/1.1 200 OK", b"Content-Length: {length}", b"Connection: close"
    )
    assert closed


def test_a_trailer_continuation_line_continues_the_request_s_last_field() -> None:
    """`Connection: close` continued by the trailer is `close x`, and kept.

    What `bitcoind` v31.1.0 answers `trailer-continuation-first`: the
    line is appended to the last field libevent holds, the request's.
    """
    fields = CHUNKED_FIELD + b"Connection: close\r\n"
    reply, closed = conversation(
        request(fields, chunked(BODY, trailer=b" x\r\n")) + THEN_CLOSE
    )
    assert reply == (
        framed(b"HTTP/1.1 200 OK", b"Content-Length: {length}") + THEN_CLOSE_ANSWER
    )
    assert closed


def test_a_chunked_request_on_a_kept_alive_connection_leaves_the_next() -> None:
    """What follows the trailer is the next request."""
    reply, closed = conversation(request(CHUNKED_FIELD, chunked(BODY)) + THEN_CLOSE)
    assert reply == (
        framed(b"HTTP/1.1 200 OK", b"Content-Length: {length}") + THEN_CLOSE_ANSWER
    )
    assert closed


@pytest.mark.parametrize(
    "body",
    [
        b"21;x\r\n",
        b"zz\r\n",
        b"-1\r\n",
        b"41\r\n",
        b"40\r\n" + b"a" * 0x40 + b"1\r\n",
        chunked(BODY, trailer=b"bad\r\n")[:-2],
    ],
    ids=[
        "semicolon",
        "not-hex",
        "negative",
        "past-cap",
        "past-cap-summed",
        "trailer-no-colon",
    ],
)
def test_a_chunked_body_libevent_cannot_read_is_413_and_closes(
    body: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bitcoind` v31.1.0 answers each 413, as soon as it is read.

    The cap is lowered to 0x40, so that a body at it is small to send.
    """
    monkeypatch.setattr(connection_module, "MAX_BODY_BYTES", 0x40)
    reply, closed = conversation(request(CHUNKED_FIELD, body))
    assert reply == closing(b"413 Request Entity Too Large")
    assert closed


def test_a_size_line_past_the_body_cap_closes_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What bounds what is buffered, where libevent reads on without end."""
    monkeypatch.setattr(connection_module, "MAX_BODY_BYTES", 0x40)
    outcome, messages, closed = drive(
        [request(CHUNKED_FIELD, b"0" * 0x41)], timeout=0.5
    )
    assert outcome == "returned"
    assert not messages
    assert closed


def test_an_empty_line_first_in_the_buffer_is_empty() -> None:
    r"""A line feed at the buffer's start ends a line of nothing.

    Whatever the buffer ends with: here the body, a lone `\r`.
    """
    head = parse_request_head(b"POST / HTTP/1.1\nContent-Length: 1\n\n\r")
    assert head.length == 1
    assert head.consumed == len(b"POST / HTTP/1.1\nContent-Length: 1\n\n")


def test_a_chunked_body_at_the_cap_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """`MAX_BODY_BYTES` of data is read whole, as libevent reads it."""
    monkeypatch.setattr(connection_module, "MAX_BODY_BYTES", 0x40)
    body = BODY + b" " * (0x40 - len(BODY))
    _, messages, _ = drive([request(CHUNKED_FIELD, chunked(body))])
    assert messages == [(json.loads(BODY), 0)]


def test_a_size_line_at_the_body_cap_is_waited_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A size line of `MAX_BODY_BYTES` octets not yet ended is still read."""
    monkeypatch.setattr(connection_module, "MAX_BODY_BYTES", 0x40)
    outcome, messages, closed = drive(
        [request(CHUNKED_FIELD, b"0" * 0x40)], timeout=0.5
    )
    assert outcome == "waiting"
    assert not messages
    assert not closed


def test_a_trailer_is_counted_on_from_the_header_section() -> None:
    """`headers_size` is the request's, so the trailer adds to it.

    What `bitcoind` v31.1.0 answers `trailer-8192` and `trailer-8193`.
    The second is refused before its line ends, on the last octet sent,
    so that nothing is left unread.
    """
    fields = CHUNKED_FIELD + b"Connection: close\r\n"
    lines = b"POST / HTTP/1.1\r\nHost: x\r\n" + RPCAUTH_LINE + fields
    counted = len(lines) - 2 * lines.count(b"\r\n")
    field = b"X: " + b"a" * (MAX_HEADER_BYTES - counted - 3)
    reply, closed = conversation(
        request(fields, chunked(BODY, trailer=field + b"\r\n"))
    )
    assert reply.startswith(b"HTTP/1.1 200 OK")
    assert closed
    reply, closed = conversation(request(fields, chunked(BODY)[:-2] + field + b"a"))
    page = error_page(b"413 Request Entity Too Large")
    assert reply == (
        b"HTTP/1.1 413 Request Entity Too Large\r\n"
        + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(page)
        + page
    )
    assert closed


def test_a_size_line_arriving_in_pieces_is_searched_once() -> None:
    """A line feed is searched for in each octet once, however it arrives.

    A size line is bounded by `MAX_BODY_BYTES` alone: searched again from
    its start at every read, the one here would take tens of seconds. The
    bound is generous: it is there to fail that, not to measure this.
    """
    head = parse_request_head(b"POST / HTTP/1.1\r\n" + CHUNKED_FIELD + b"\r\n")
    buffer = bytearray()
    reader = connection_module._ChunkedReader(buffer, head)
    start = time.perf_counter()
    for _ in range(4 * 1024 * 1024 // 64):
        buffer += b"0" * 64
        assert not reader.read()
    assert time.perf_counter() - start < 2


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            b"POST http://x/ HTTP/1.1\r\n"
            + CHUNKED_FIELD
            + b"\r\n"
            + chunked(BODY)[:-2]
            + b"Proxy-Connection: keep-alive\r\nbad\r\n",
            closing(b"413 Request Entity Too Large"),
        ),
        (
            request(
                CHUNKED_FIELD, chunked(BODY)[:-2] + b"Connection: close\r\nbad\r\n"
            ),
            b"HTTP/1.1 413 Request Entity Too Large\r\n"
            + b"Content-Length: %d\r\nConnection: close\r\n\r\n"
            % len(error_page(b"413 Request Entity Too Large"))
            + error_page(b"413 Request Entity Too Large"),
        ),
    ],
    ids=["proxy-keep-alive", "close"],
)
def test_a_refused_trailer_is_answered_by_the_fields_read_before_it(
    data: bytes, expected: bytes
) -> None:
    """The trailer fields read before the refusal frame the 413, as libevent's.

    What `bitcoind` v31.1.0 answers `trailer-proxy-keep-alive-refused`
    and `trailer-close-refused`: the proxy request keeps the page's own
    `Connection: close`, and the request asking to close has it moved
    after `Content-Length`.
    """
    reply, closed = conversation(data)
    assert reply == expected
    assert closed


def expecting(value: bytes, fields: bytes = b"", **kwargs: Any) -> bytes:
    """Return a request head carrying `Expect: value`, and `fields`, no body."""
    return request(b"Expect: " + value + b"\r\n" + fields, b"", **kwargs)


LENGTH = b"Content-Length: %d\r\n" % len(BODY)


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (expecting(b"foo", LENGTH), UnmetExpectationError),
        (expecting(b"", LENGTH), UnmetExpectationError),
        (expecting(b"foo", CHUNKED_FIELD), UnmetExpectationError),
        (expecting(b"foo", LENGTH, version=b"HTTP/1.5"), UnmetExpectationError),
        (
            expecting(b"foo", LENGTH, method=b"GET", target=b"/nope"),
            UnmetExpectationError,
        ),
        (
            expecting(b"foo", b"Expect: 100-continue\r\n" + LENGTH),
            UnmetExpectationError,
        ),
        (
            expecting(b"foo", b"Content-Length: %d\r\n" % (MAX_BODY_BYTES + 1)),
            UnmetExpectationError,
        ),
        (
            expecting(
                b"100-continue", b"Content-Length: %d\r\n" % (MAX_BODY_BYTES + 1)
            ),
            OversizedRequestBodyError,
        ),
        (expecting(b"foo", b"Content-Length: x\r\n"), MalformedRequestHeadError),
    ],
    ids=[
        "other",
        "empty",
        "chunked",
        "http-1.5",
        "before-the-refusals",
        "first-field",
        "before-the-413",
        "continue-then-413",
        "after-the-400",
    ],
)
def test_an_expectation_libevent_does_not_meet_is_refused(
    data: bytes, error: type[Exception]
) -> None:
    """ISS 1194: `evhttp_have_expect` answers `OTHER`, and 417 follows.

    Any value but `100-continue`, the first `Expect` field's, on HTTP/1.1
    or later and a request with a body, ahead of the 413 of a length past
    the cap but after the 400 of one libevent cannot read.
    """
    with pytest.raises(error):
        parse_request_head(data)


@pytest.mark.parametrize(
    ("data", "expects_continue"),
    [
        (expecting(b"foo", LENGTH, version=b"HTTP/1.0"), False),
        (expecting(b"foo", b"Content-Length: 0\r\n"), False),
        (expecting(b"foo"), False),
        (expecting(b"foo", LENGTH, method=b"HEAD"), False),
        (request(LENGTH, b""), False),
        (expecting(b"100-continue", LENGTH), True),
        (expecting(b"100-CONTINUE", LENGTH), True),
        (expecting(b"  100-continue  ", LENGTH), True),
        (expecting(b"100-continue", CHUNKED_FIELD), True),
        (expecting(b"100-continue", b"Expect: foo\r\n" + LENGTH), True),
        (expecting(b"100-continue", LENGTH, version=b"HTTP/1.0"), False),
    ],
    ids=[
        "http-1.0",
        "no-body",
        "no-length",
        "head",
        "no-expect",
        "continue",
        "any-case",
        "trimmed",
        "chunked",
        "first-field",
        "continue-http-1.0",
    ],
)
def test_an_expectation_libevent_meets_or_never_reads_is_not_refused(
    data: bytes, *, expects_continue: bool
) -> None:
    """ISS 1194: `NO` below HTTP/1.1 and with no body, `CONTINUE` otherwise."""
    assert parse_request_head(data).expects_continue is expects_continue


@pytest.mark.parametrize(
    ("method", "target", "auth"),
    [
        (b"POST", b"/", b""),
        (b"POST", b"/nope", RPCAUTH_LINE),
        (b"GET", b"/", RPCAUTH_LINE),
        (b"PUT", b"/", RPCAUTH_LINE),
        (b"OPTIONS", b"/", RPCAUTH_LINE),
    ],
)
def test_an_expectation_not_met_is_417_before_any_other_refusal(
    method: bytes, target: bytes, auth: bytes
) -> None:
    """ISS 1194: 417 where `bitcoind` v31.1.0 would 401, 404, 405 or 501.

    `evhttp_get_body` runs before Core's request handler. The body is
    never sent: `bitcoind` answers without it.
    """
    data = expecting(
        b"foo",
        LENGTH + b"Connection: close\r\n",
        method=method,
        target=target,
        auth=auth,
    )
    reply, closed = conversation(data)
    page = error_page(b"417 Expectation Failed")
    assert reply == (
        b"HTTP/1.1 417 Expectation Failed\r\n"
        b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(page) + page
    )
    assert closed


def test_an_expectation_not_met_closes_a_kept_alive_connection() -> None:
    """`evhttp_send_error`'s page, `Connection: close` ahead of its length."""
    reply, closed = conversation(expecting(b"foo", LENGTH))
    assert reply == closing(b"417 Expectation Failed")
    assert closed


def test_an_expectation_not_met_on_connect_leaves_the_body_as_the_next_request() -> (
    None
):
    """A `CONNECT` keeps its connection, and its unread body is read next.

    As `bitcoind` v31.1.0 reads a body it did not take as the next
    request's head.
    """
    fields = b"Content-Length: %d\r\n" % len(THEN_CLOSE)
    data = expecting(b"foo", fields, method=b"CONNECT", target=b"x:1") + THEN_CLOSE
    reply, closed = conversation(data)
    page = error_page(b"417 Expectation Failed")
    assert reply == (
        b"HTTP/1.1 417 Expectation Failed\r\nConnection: close\r\n\r\n"
        + page
        + THEN_CLOSE_ANSWER
    )
    assert closed


def continued(
    head: bytes, body: bytes, *, interim: bytes = b"", buffered: bytes = b""
) -> tuple[bytes, bytes, bool]:
    """Send `head`, read `interim` off the reply, then send `body`.

    `buffered` is put where `run` reads it from before `run` starts, as
    though it came with the head. Returns what came before `body` was
    sent, what came after, to the close, and whether the connection
    closed. A request `run` queues is answered `ANSWER`.
    """

    async def main() -> tuple[bytes, bytes, bool]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={0: None})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)
        conn.buffer += buffered
        await loop.sock_sendall(theirs, head)
        task = asyncio.ensure_future(conn.run())
        before = b""
        async with asyncio.timeout(5):
            while len(before) < len(interim):
                before += await loop.sock_recv(theirs, len(interim) - len(before))
        await loop.sock_sendall(theirs, body)
        await task
        if manager.messages:
            await conn.async_send(HttpReply(OK, ANSWER))
        after = b""
        async with asyncio.timeout(5):
            while chunk := await loop.sock_recv(theirs, 4096):
                after += chunk
        closed = ours.fileno() == -1
        theirs.close()
        ours.close()
        return before, after, closed

    return asyncio.run(main())


CLOSE = b"Connection: close\r\n"


@pytest.mark.parametrize("version", [b"1.1", b"1.5"])
def test_100_continue_is_answered_before_the_body_in_the_request_s_version(
    version: bytes,
) -> None:
    """ISS 1194: `evhttp_send_continue`'s `HTTP/%d.%d 100 Continue`.

    Then the body is read and answered as any other.
    """
    interim = b"HTTP/" + version + b" 100 Continue\r\n\r\n"
    head = expecting(b"100-continue", LENGTH + CLOSE, version=b"HTTP/" + version)
    before, after, closed = continued(head, BODY, interim=interim)
    assert before == interim
    assert after == framed(
        b"HTTP/" + version + b" 200 OK",
        b"Content-Length: {length}",
        b"Connection: close",
    )
    assert closed


def test_100_continue_is_answered_before_a_chunked_body() -> None:
    """ISS 1194: `CONTINUE` for a chunked body too, its length unknown."""
    interim = b"HTTP/1.1 100 Continue\r\n\r\n"
    head = expecting(b"100-continue", CHUNKED_FIELD + CLOSE)
    before, after, _ = continued(head, chunked(BODY), interim=interim)
    assert before == interim
    assert after.startswith(b"HTTP/1.1 200 OK\r\n")


def test_100_continue_comes_before_the_credential_is_read() -> None:
    """ISS 1194: `bitcoind` v31.1.0 sends it, and then its 401."""
    interim = b"HTTP/1.1 100 Continue\r\n\r\n"
    head = expecting(b"100-continue", LENGTH + CLOSE, auth=b"")
    before, after, closed = continued(head, BODY, interim=interim)
    assert before == interim
    assert after == UNAUTHORIZED.replace(b"\r\n\r\n", b"\r\nConnection: close\r\n\r\n")
    assert closed


def test_no_100_continue_where_the_body_began_with_the_head() -> None:
    """ISS 1194: only where `evbuffer_get_length(input)` is still 0.

    What came with the head is already in `run`'s buffer when it starts.
    """
    head = expecting(b"100-continue", LENGTH + CLOSE)
    before, after, closed = continued(b"", BODY[5:], buffered=head + BODY[:5])
    assert not before
    assert after == framed(
        b"HTTP/1.1 200 OK", b"Content-Length: {length}", b"Connection: close"
    )
    assert closed


def test_100_continue_is_answered_for_each_request_kept_alive() -> None:
    """ISS 1194: the second request on a connection gets its own."""
    interim = b"HTTP/1.1 100 Continue\r\n\r\n"
    first = expecting(b"100-continue", LENGTH)
    second = expecting(b"100-continue", LENGTH + CLOSE)

    async def main() -> bytes:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = fake_manager(connections={0: None})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)
        reply = b""

        async def exchange(head: bytes, reading: Any) -> None:
            nonlocal reply
            await loop.sock_sendall(theirs, head)
            async with asyncio.timeout(5):
                while not reply.endswith(interim):
                    reply += await loop.sock_recv(theirs, 1)
            await loop.sock_sendall(theirs, BODY)
            # `run` for the first, and for the second the `async_send`
            # whose keep-alive reads it
            await reading
            (message,) = manager.messages
            manager.messages.clear()
            assert message == (json.loads(BODY), 0)

        # kept alive past the first answer, `async_send` goes on to read
        # the second request, and is done once it has
        await exchange(first, asyncio.ensure_future(conn.run()))
        answering_first = asyncio.ensure_future(conn.async_send(HttpReply(OK, ANSWER)))
        async with asyncio.timeout(5):
            while not reply.endswith(b"}\n"):
                reply += await loop.sock_recv(theirs, 4096)
        await exchange(second, answering_first)
        await conn.async_send(HttpReply(OK, ANSWER))
        async with asyncio.timeout(5):
            while chunk := await loop.sock_recv(theirs, 4096):
                reply += chunk
        theirs.close()
        ours.close()
        return reply

    reply = asyncio.run(main())
    assert reply == (
        interim
        + framed(b"HTTP/1.1 200 OK", b"Content-Length: {length}")
        + interim
        + framed(b"HTTP/1.1 200 OK", b"Content-Length: {length}", b"Connection: close")
    )


def test_a_content_length_at_the_cap_is_read_whatever_is_expected() -> None:
    """`MAX_BODY_BYTES` itself is no 413, `100-continue` asked or not."""
    at_cap = b"Content-Length: %d\r\n" % MAX_BODY_BYTES
    for data in (request(at_cap, b""), expecting(b"100-continue", at_cap)):
        assert parse_request_head(data).length == MAX_BODY_BYTES
