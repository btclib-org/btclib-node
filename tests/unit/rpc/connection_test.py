# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What rpc.connection.Connection does with the octets off a socket.

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
    Connection,
    JSONEncoder,
    RawJSON,
)

if TYPE_CHECKING:
    from btclib_node.rpc.manager import RpcManager

BODY = b'{"jsonrpc":"2.0","id":"x","method":"getbestblockhash"}'


def request(headers: bytes = b"", body: bytes = BODY) -> bytes:
    """Build a raw HTTP request line and headers, followed by `body`."""
    return b"POST / HTTP/1.1\r\nHost: x\r\n" + headers + b"\r\n" + body


def with_length(body: bytes = BODY) -> bytes:
    """Build `request` with a correct Content-Length header for `body`."""
    return request(b"Content-Length: %d\r\n" % len(body), body)


def drive(
    chunks: list[bytes], *, timeout: float = 1.0, hang_up: bool = False
) -> tuple[str, list[Any], bool]:
    """Feed `chunks` to a Connection.run and report what it did.

    Returns (outcome, dispatched messages, whether the socket was
    closed). The sender is async because a socketpair holds only a few
    kilobytes: a blocking send of a large chunk would deadlock before
    the loop starts.
    """

    async def main() -> tuple[str, list[Any], bool]:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        manager = SimpleNamespace(messages=[])
        conn = Connection(loop, ours, cast("RpcManager", manager), 0)

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

    No Content-Length is a length of zero, and `b""` is not JSON.
    """
    _, messages, closed = drive([request()])
    assert not messages
    assert closed


def test_a_body_that_is_not_json_is_refused() -> None:
    """A body that fails to parse as JSON is refused, and the socket closed."""
    body = b"not json"
    _, messages, closed = drive([with_length(body)])
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


def test_the_response_is_crlf_framed_and_the_socket_closed() -> None:
    """async_send frames the reply behind an HTTP header and closes the socket.

    A single-element response is unwrapped and `bytes` are hex-encoded.
    """

    async def main() -> bytes:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        conn = Connection(
            loop, ours, cast("RpcManager", SimpleNamespace(messages=[])), 0
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
    """async_send does not unwrap a response of more than one entry."""

    async def main() -> bytes:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        theirs.setblocking(False)
        loop = asyncio.get_running_loop()
        conn = Connection(
            loop, ours, cast("RpcManager", SimpleNamespace(messages=[])), 0
        )
        await conn.async_send([{"id": "a"}, {"id": "b"}])
        data = await loop.sock_recv(theirs, 4096)
        theirs.close()
        return data

    body = asyncio.run(main()).partition(b"\r\n\r\n")[2]
    assert json.loads(body) == [{"id": "a"}, {"id": "b"}]


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

    Only `Connection.async_send` is meant to construct `JSONEncoder`
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
        conn = Connection(
            loop, ours, cast("RpcManager", SimpleNamespace(messages=[])), 0
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
        conn = Connection(
            loop, ours, cast("RpcManager", SimpleNamespace(messages=[])), 0
        )
        await conn.async_send(
            [{"result": "RawJSONx", "extra": RawJSON("1.00000000"), "id": "x"}]
        )
        data = await loop.sock_recv(theirs, 4096)
        theirs.close()
        return data

    body = asyncio.run(main()).partition(b"\r\n\r\n")[2]
    assert json.loads(body) == {"result": "RawJSONx", "extra": 1.0, "id": "x"}


def test_close_cancels_the_task_it_was_given() -> None:
    """close cancels the running task it was handed."""

    async def main() -> bool:
        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        loop = asyncio.get_running_loop()
        conn = Connection(
            loop, ours, cast("RpcManager", SimpleNamespace(messages=[])), 0
        )

        async def forever() -> None:
            await asyncio.sleep(60)

        task = asyncio.ensure_future(forever())
        conn.task = task  # type: ignore[assignment]
        # let it start, so that what is cancelled is a running task
        await asyncio.sleep(0)
        conn.close()
        await asyncio.sleep(0)
        cancelled = bool(task.cancelled() or task.cancelling())
        theirs.close()
        return cancelled

    assert asyncio.run(main())


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

    conn = Connection(
        cast("asyncio.AbstractEventLoop", None),
        client,
        cast("RpcManager", SimpleNamespace(messages=[])),
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
    conn = Connection(
        cast("asyncio.AbstractEventLoop", None),
        client,
        cast("RpcManager", SimpleNamespace(messages=[])),
        0,
    )
    assert repr(conn) == f"Connection to {endpoint}"


def test_close_without_a_task_closes_the_socket_anyway() -> None:
    """close still closes the socket for a connection whose task is None."""
    ours, theirs = socket.socketpair()
    conn = Connection(
        cast("asyncio.AbstractEventLoop", None),
        ours,
        cast("RpcManager", SimpleNamespace(messages=[])),
        0,
    )
    assert conn.task is None
    conn.close()
    assert ours.fileno() == -1
    theirs.close()


@pytest.mark.filterwarnings(
    "ignore:coroutine 'Connection.async_send' was never awaited:RuntimeWarning"
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
    """
    ours, theirs = socket.socketpair()
    loop = asyncio.new_event_loop()
    conn = Connection(loop, ours, cast("RpcManager", SimpleNamespace(messages=[])), 0)
    started = time.monotonic()
    conn.send_and_wait([{"id": "x"}])  # returns, does not raise
    waited = time.monotonic() - started
    assert waited >= 2
    loop.close()
    ours.close()
    theirs.close()
