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
import contextlib
import json
import socket
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from btclib_node.rpc.connection import (
    MAX_BODY_BYTES,
    MAX_HEADER_BYTES,
    REQUEST_TIMEOUT,
    JSONEncoder,
    RawJSON,
    RpcConnection,
)

if TYPE_CHECKING:
    from btclib_node.rpc.manager import RpcManager

BODY = b'{"jsonrpc":"2.0","id":"x","method":"getbestblockhash"}'


def request(
    headers: bytes = b"", body: bytes = BODY, *, version: bytes = b"HTTP/1.1"
) -> bytes:
    """Build a raw HTTP request line and headers, followed by `body`.

    `version` names the request line's own trailing token -- `HTTP/1.1`
    by default, which is every existing caller's own request; a caller
    of `_wants_keep_alive`'s HTTP/1.0 half passes `b"HTTP/1.0"` instead.
    """
    return b"POST / " + version + b"\r\nHost: x\r\n" + headers + b"\r\n" + body


def with_length(body: bytes = BODY) -> bytes:
    """Build `request` with a correct Content-Length header for `body`."""
    return request(b"Content-Length: %d\r\n" % len(body), body)


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
        manager = SimpleNamespace(messages=[], connections={0: None})
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
    assert messages == [([json.loads(BODY)], 0)]


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
    assert messages == [([json.loads(BODY)], 0)]


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


def test_a_negative_content_length_is_refused() -> None:
    """A negative Content-Length is refused, not read as a body length."""
    _, messages, closed = drive([request(b"Content-Length: -1\r\n", BODY)])
    assert not messages
    assert closed


def test_a_content_length_past_the_cap_is_refused() -> None:
    """A Content-Length past MAX_BODY_BYTES is refused before the read."""
    over = b"Content-Length: %d\r\n" % (MAX_BODY_BYTES + 1)
    _, messages, closed = drive([request(over, b"a")])
    assert not messages
    assert closed


def test_a_content_length_that_is_not_an_integer_is_refused() -> None:
    """A `Content-Length` `int()` cannot parse is refused, not read as one.

    `parse_request_head`'s own `int(headers.get("Content-Length", 0))`
    raises `ValueError` here, caught and reraised as
    `MalformedRequestHeadError` -- `run`'s own bare `except Exception`
    still catches it exactly as it caught the bare `ValueError` before
    that wrapping existed.
    """
    _, messages, closed = drive([request(b"Content-Length: abc\r\n", BODY)])
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
        manager = SimpleNamespace(messages=[], connections={0: None})
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
    returns nothing, which is the other way out of `_recv_until`.
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

    A single-element response is unwrapped and `bytes` are hex-encoded.
    """

    async def main() -> bytes:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        conn = RpcConnection(
            loop,
            ours,
            cast("RpcManager", SimpleNamespace(messages=[], connections={})),
            0,
        )
        await conn.async_send([{"result": b"\xff", "id": "x"}])
        data = await loop.sock_recv(theirs, 4096)
        theirs.close()
        return data

    data = asyncio.run(main())
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"\r\nContent-Type: application/json\r\n" in b"\r\n" + head
    # a single-element response is unwrapped, and bytes become hex
    assert json.loads(body) == {"result": "ff", "id": "x"}
    assert int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0]) == len(body)


def test_a_response_of_several_stays_a_list() -> None:
    """async_send does not unwrap a batch's own response of several entries."""

    async def main() -> bytes:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        conn = RpcConnection(
            loop,
            ours,
            cast("RpcManager", SimpleNamespace(messages=[], connections={})),
            0,
        )
        # What `run` would have set off a real two-member batch: this
        # calls `async_send` directly, bypassing `run` and the parse it
        # would otherwise read this off (issue #653).
        conn.is_batch = True
        await conn.async_send([{"id": "a"}, {"id": "b"}])
        data = await loop.sock_recv(theirs, 4096)
        theirs.close()
        return data

    body = asyncio.run(main()).partition(b"\r\n\r\n")[2]
    assert json.loads(body) == [{"id": "a"}, {"id": "b"}]


def test_a_response_of_one_stays_a_list_where_the_request_was_a_batch() -> None:
    """A one-member batch's own reply stays an array, not a bare object.

    `async_send` used to unwrap purely from `len(response) == 1`, which
    cannot tell a lone request from a one-member batch apart -- both
    reached it as a response list of the same one-element shape. `run`
    reads that off the request instead, before it is lost, matching
    Core's own `ExecuteHTTPRPC`: an array of any size, `valRequest.
    isArray()`, is always answered as an array, `UniValue::VARR`
    (`HTTPReq_JSONRPC`, `src/httprpc.cpp:135-169`, at
    bitcoin/bitcoin@ca7162cde5) -- never unwrapped for having only one
    member (issue #653).
    """

    async def main() -> bytes:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        conn = RpcConnection(
            loop,
            ours,
            cast("RpcManager", SimpleNamespace(messages=[], connections={})),
            0,
        )
        conn.is_batch = True
        await conn.async_send([{"id": "a"}])
        data = await loop.sock_recv(theirs, 4096)
        theirs.close()
        return data

    body = asyncio.run(main()).partition(b"\r\n\r\n")[2]
    assert json.loads(body) == [{"id": "a"}]


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
        manager = SimpleNamespace(messages=[], connections={})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)

        await loop.sock_sendall(theirs, with_length())
        await conn.run()
        assert conn.keep_alive

        await loop.sock_sendall(theirs, with_length())
        await conn.async_send([{"id": "x", "result": None}])
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
        manager = SimpleNamespace(messages=[], connections={0: None})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)

        headers = b"Connection: close\r\nContent-Length: %d\r\n" % len(BODY)
        await loop.sock_sendall(theirs, request(headers))
        await conn.run()
        assert not conn.keep_alive

        await conn.async_send([{"id": "x", "result": None}])
        head = (await loop.sock_recv(theirs, 4096)).partition(b"\r\n\r\n")[0]

        # async_send closes `ours` itself, off the `Connection: close`
        # just read, so there is nothing of this side left to close
        closed = ours.fileno() == -1
        theirs.close()
        return closed, head

    closed, head = asyncio.run(main())
    assert closed
    assert b"\r\nConnection: close\r\n" in b"\r\n" + head


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
        manager = SimpleNamespace(messages=[], connections={})
        conn = RpcConnection(
            loop, ours, cast("RpcManager", manager), 0, request_timeout=0.2
        )

        await loop.sock_sendall(theirs, with_length())
        await conn.run()
        assert conn.keep_alive

        await asyncio.wait_for(
            conn.async_send([{"id": "x", "result": None}]), timeout=2
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
        (b"HTTP/1.1", b"", True),
        (b"HTTP/1.1", b"Connection: close\r\n", False),
    ],
    ids=[
        "1.0-bare-closes",
        "1.0-keep-alive-stays",
        "1.1-bare-stays",
        "1.1-close-closes",
    ],
)
def test_keep_alive_follows_core_s_own_default_per_version(
    version: bytes, connection_header: bytes, *, expect_keep_alive: bool
) -> None:
    """HTTP/1.0 defaults to closing, HTTP/1.1 to keep-alive, matching Core.

    `HTTPRequest::WriteReply` (`httpserver.cpp:557-575`, at
    bitcoin/bitcoin@ca7162cde5): HTTP/1.0 stays open only for an
    explicit `Connection: keep-alive`; HTTP/1.1 stays open unless told
    `Connection: close`. `run` used to read every request the same way
    regardless of its own request line's version (issue #640) -- a
    second request, sent here regardless of what the first asked for,
    is answered only where `expect_keep_alive` says this connection is
    still being read from.
    """

    async def main() -> tuple[bool, bool]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = SimpleNamespace(messages=[], connections={})
        conn = RpcConnection(loop, ours, cast("RpcManager", manager), 0)

        headers = connection_header + b"Content-Length: %d\r\n" % len(BODY)
        one_request = request(headers, BODY, version=version)

        await loop.sock_sendall(theirs, one_request)
        await conn.run()
        keep_alive = conn.keep_alive

        await loop.sock_sendall(theirs, one_request)
        await conn.async_send([{"id": "x", "result": None}])
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
        manager = SimpleNamespace(messages=[], connections={})
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
        conn = RpcConnection(
            loop,
            ours,
            cast("RpcManager", SimpleNamespace(messages=[], connections={})),
            0,
        )
        await conn.async_send([{"result": RawJSON("0.00000001"), "id": "x"}])
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
        conn = RpcConnection(
            loop,
            ours,
            cast("RpcManager", SimpleNamespace(messages=[], connections={})),
            0,
        )
        await conn.async_send(
            [{"result": "RawJSONx", "extra": RawJSON("1.00000000"), "id": "x"}]
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
            cast("RpcManager", SimpleNamespace(messages=[], connections={})),
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
        cast("RpcManager", SimpleNamespace(messages=[], connections={})),
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
        cast("RpcManager", SimpleNamespace(messages=[], connections={})),
        0,
    )
    assert repr(conn) == f"Connection to {endpoint}"


def test_close_closes_the_socket() -> None:
    """`close` closes `client`, unconditionally."""
    ours, theirs = socket.socketpair()
    conn = RpcConnection(
        cast("asyncio.AbstractEventLoop", None),
        ours,
        cast("RpcManager", SimpleNamespace(messages=[], connections={})),
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
        loop, ours, cast("RpcManager", SimpleNamespace(messages=[], connections={})), 0
    )
    started = time.monotonic()
    conn.send_and_wait([{"id": "x"}])  # returns, does not raise
    waited = time.monotonic() - started
    assert waited >= 2 - _WINDOWS_TIMER_TICK
    loop.close()
    ours.close()
    theirs.close()
