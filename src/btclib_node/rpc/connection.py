# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`RpcConnection`, one HTTP socket carrying a JSON-RPC request and reply.

Parses the header section off the wire, bounded by `MAX_HEADER_BYTES`
and `MAX_BODY_BYTES` since the listener this serves is bound to every
interface, and decodes the JSON-RPC batch `rpc.manager.RpcManager.messages`
queues for `rpc.main.handle_rpc`. `RawJSON` is a JSON number written
back out exactly as given, the way Core's own `UniValue` writes one
built from a string rather than from a `float`.
"""

import asyncio
import contextlib
import json
import re
import secrets
from http.client import parse_headers
from io import BytesIO
from typing import TYPE_CHECKING, Any, override

from btclib_node.p2p.address import ip_and_port
from btclib_node.rpc.errors import RpcErrorCode, error_msg

if TYPE_CHECKING:
    import socket
    from collections.abc import Callable
    from concurrent.futures import Future

    from btclib_node.rpc.manager import RpcManager

HEADER_TERMINATOR = b"\r\n\r\n"
# Bounds on the read below, which is fed by whoever connects: the RPC
# socket is bound to every interface (see rpc.manager.RpcManager.server),
# so an unterminated header section or an overstated Content-Length must
# not grow the buffer without limit. Both are generous next to a real
# JSON-RPC request -- headers run to a few hundred bytes, and the largest
# body this node is sent is a raw transaction.
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 32 * 1024 * 1024
# What bounds how *long* a read may take, where the two above only bound
# how much of it this node buffers: a client that sends a byte and then
# stops never crosses either cap, and used to leave `run` below suspended
# on `sock_recv` for the life of the node, its socket and its entry in
# `RpcManager.connections` both held the whole time (issue #437). Core's
# own `-rpcservertimeout`, `DEFAULT_HTTP_SERVER_TIMEOUT` (`src/httpserver.h:42`,
# at bitcoin/bitcoin@b91d983f66), is 30 seconds, and is what this is matched
# to -- but not to Core's own mechanism: Core resets that timer on every
# receive (`httpserver.cpp:930`) and every send (`:1275`), and its own
# `DisconnectClients` (`:1098-1100`) only disconnects a client genuinely
# idle *between* requests, one of its own HTTP connections carrying more
# than one. This tree's connection is one request per socket with no
# such "between" to distinguish, so
# REQUEST_TIMEOUT is spent once, on the whole read from accept to a
# complete request (`run` below), rather than reset on each byte -- which
# also bounds a client that dribbles one byte at a time forever, where a
# per-`sock_recv` reset would not.
# a float, not Core's own int seconds: asyncio.timeout below and
# RpcManager.request_timeout both carry this as a float throughout, a
# test lowering it to a fraction of a second being the only assignment
# that would otherwise disagree with an int-inferred attribute
REQUEST_TIMEOUT = 30.0


class RawJSON:
    """A JSON number written to the wire exactly as given, not from a `float`.

    Core's own `UniValue(UniValue::VNUM, "<string>")` does the same:
    the value is built from a string and written out verbatim, whatever
    that string was, rather than round-tripped through a floating-point
    type on the way out. `ValueFromAmount` (`src/core_io.cpp:283-293`,
    at bitcoin/bitcoin@58a7869f86) is the caller this exists for --
    `rpc.callbacks.get_mempool_info`'s own `mempoolminfee`, an exact
    eight-decimal BTC amount a Python `float` cannot always carry:
    `repr` fixes no decimal places and emits exponent notation
    (`1e-06`) at a magnitude ordinary for a feerate, which Core's own
    `%d.%08d` format never does.

    `json.JSONEncoder.default` cannot return this directly -- its
    return value is re-encoded through the same machinery rather than
    written as-is, and Python's `json` has no hook for a raw literal.
    `JSONEncoder.default` below returns a marked placeholder instead,
    and `RpcConnection.async_send` substitutes it, quotes and all, for
    `text` once encoding has already run.

    The mark is not a fixed word: a fixed one is not actually safe -- a
    plain string value that happens to contain it once, unpaired (an
    error message echoing back a client's own malformed method name,
    say), lets a regex substitution's own non-greedy match run past
    that string's closing quote and merge it with an unrelated
    placeholder later in the same response, corrupting both.
    `RpcConnection.async_send` passes a fresh random token instead, one
    per call, so a legitimate value colliding with it is not a
    realistic risk the way colliding with a guessable word is.
    """

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        """Wrap `text`, the exact number `JSONEncoder` writes back out."""
        self.text = text


class JSONEncoder(json.JSONEncoder):
    """Encode `bytes` as hex and unwrap a `RawJSON` under a caller's mark.

    `default` below is `json.dumps`'s own hook for a type it has no
    built-in encoding for; it is what `RpcConnection.async_send` supplies
    `cls=` and `mark=` to, so that a `RawJSON` value comes out marked
    rather than quoted, for `async_send` to unquote once encoding is
    done -- `json` itself has no hook for writing a literal unquoted.
    """

    def __init__(
        self,
        mark: str = "",
        # json.dumps(cls=JSONEncoder, **kw) is the only caller (see
        # below), and it always calls this keyword-only -- json's own
        # dumps builds every argument by name, skipkeys through
        # sort_keys, never positionally -- so there is no *args to
        # accept here. **kwargs is still Any and stays that way: it
        # forwards blindly to json.JSONEncoder.__init__, whose own
        # keyword arguments are a heterogeneous mix (bool, int | None,
        # tuple[str, str] | None, a callable), not one type to narrow
        # to.
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Set `mark`, the token `default` wraps a `RawJSON`'s text in."""
        super().__init__(**kwargs)
        self._mark = mark

    @override
    def default(self, obj: object) -> Any:
        if isinstance(obj, bytes):
            return obj.hex()
        if isinstance(obj, RawJSON):
            if not self._mark:
                # A RawJSON reached an encoder built with no mark to wrap
                # it in -- json.dumps(cls=JSONEncoder) with no mark=,
                # which only RpcConnection.async_send is meant to supply.
                # Refusing here is the same "not serializable" TypeError
                # super().default(obj) below raises for any other object
                # json does not know, rather than writing RawJSON's own
                # text out unmarked and unsubstituted.
                return super().default(obj)
            return f"{self._mark}{obj.text}{self._mark}"
        return super().default(obj)


class RpcConnection:
    """One accepted RPC socket, from the header read through the reply.

    `RpcManager.server` builds one per accepted client, on this
    manager's own thread; `run` below is scheduled on the same loop and
    reads the request off `client`, queuing it onto `manager.messages`
    for `rpc.main.handle_rpc` on `Node`'s own thread to answer, through
    `send` or `send_and_wait`.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client: socket.socket,
        manager: RpcManager,
        connection_id: int,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        """Set up empty buffers for `client`, tracked under `connection_id`.

        `request_timeout` is `REQUEST_TIMEOUT` unless `manager` -- in
        practice `RpcManager.create_connection` -- is built or told to
        hand over something else, which is the seam a test uses to keep
        `REQUEST_TIMEOUT`'s own real, Core-matching value off its own
        critical path.
        """
        super().__init__()
        self.loop = loop
        self.client = client
        self.manager = manager
        self.id = connection_id
        self.rpc_id = ""
        self.messages: list[Any] = []
        # A `bytearray`, not `bytes`: `_recv_until`'s own `+=` below is
        # an in-place, amortised extend on this type and a full copy of
        # everything held so far on the other -- btclib-org/btclib-node#466,
        # the same shape btclib-org/btclib-node#438 fixed on the p2p side.
        self.buffer = bytearray()
        self.task: Future[None] | None = None
        self.request_timeout = request_timeout

    def close(self) -> None:
        """Cancel the running `run` task, if any, and close `client`."""
        if self.task:
            self.task.cancel()
        self.client.close()

    async def _recv_until(
        self, predicate: Callable[[], bool], max_bytes: int | None = None
    ) -> None:
        while not predicate():
            if max_bytes is not None and len(self.buffer) > max_bytes:
                raise ConnectionError
            # 64 KB, matching Core's own HTTP server:
            # `HTTPServer::SocketHandlerConnected` (`src/httpserver.cpp:904`,
            # at bitcoin/bitcoin@b91d983f66) reads into `char buf[0x10000]`,
            # "typical socket buffer is 8K-64K" by its own comment there --
            # the hand-written raw-socket read loop that server uses in
            # place of libevent's `evhttp` (`doc/release-notes-35182.md`).
            # Not a second, independent reason to reuse this tree's own
            # p2p `Connection.run`'s read size (`pchBuf`, `src/net.cpp`,
            # same commit -- btclib-org/btclib-node#438): Core's own
            # `SocketHandlerConnected` is "adapted from CConnman"
            # (`net.cpp`'s own class, `pchBuf`'s home) by its own commit
            # message (at bitcoin/bitcoin@80e1cfe5a2), and the comment
            # above is copied verbatim between the two files -- one
            # Core design decision, applied to both of its own read
            # loops, cited here the same way it is on the p2p side.
            data = await self.loop.sock_recv(self.client, 65536)
            if not data:
                raise ConnectionError
            self.buffer += data

    async def run(self) -> None:
        """Read one request off `client` and queue it for `handle_rpc`.

        Reads the header section up to `HEADER_TERMINATOR`, then the
        body up to its own `Content-Length`, both bounded against an
        unterminated or overstated one and, together, against taking
        longer than `self.request_timeout` -- `REQUEST_TIMEOUT`'s own
        docstring is where that bound is argued against Core's. A body
        that is not valid JSON is answered `PARSE_ERROR` directly, on
        the spot, and a body that is gets appended to `manager.messages`
        for `rpc.main.handle_rpc` to answer instead, through `send`. Any
        failure -- `asyncio.timeout` raises the standard library's own
        `TimeoutError` once expired, caught below like any other -- closes
        `client` rather than raising, since nothing reads the `Future`
        this task runs under.
        """
        try:
            async with asyncio.timeout(self.request_timeout):
                await self._recv_until(
                    lambda: HEADER_TERMINATOR in self.buffer, MAX_HEADER_BYTES
                )
                head, _, self.buffer = self.buffer.partition(HEADER_TERMINATOR)
                # parse_headers wants the field lines alone, so drop the
                # request line, and the blank line partition() consumed.
                _, _, fields = head.partition(b"\r\n")
                headers = parse_headers(BytesIO(fields + HEADER_TERMINATOR))
                length = int(headers.get("Content-Length", 0))
                # int() admits a negative, which would make the
                # predicate below true before a single body byte arrived
                # and then slice the body from the wrong end.
                if not 0 <= length <= MAX_BODY_BYTES:
                    # kept inside the try, against TRY301: the outer
                    # `except Exception: self.client.close()` below is
                    # what every failure in this method already answers
                    # through, abstracting this one raise to a helper
                    # would not change what catches it, only add a call
                    # for no reader
                    raise ConnectionError  # noqa: TRY301
                await self._recv_until(lambda: len(self.buffer) >= length)

            try:
                body = json.loads(self.buffer[:length])
            except ValueError:
                # JSON-RPC 2.0 section 5.1's own `PARSE_ERROR`, id
                # `null`: a body that is not JSON is not a request this
                # node can read enough of to disagree with, where the
                # header section above already parsed. `ValueError` and
                # not `json.JSONDecodeError` alone, since malformed
                # bytes `json.loads` cannot even decode as text raise
                # the stdlib's own `UnicodeDecodeError`, a `ValueError`
                # too and the same "invalid JSON" from the client's side
                await self.async_send(
                    [error_msg(RpcErrorCode.PARSE_ERROR, "Parse error")]
                )
                # send() below is what a valid request answers through,
                # and it is what pops this id out of `manager.connections`
                # once `handle_rpc` is done with it; this reply never
                # reaches `handle_rpc`, so the entry is forgotten here
                # instead -- left in place otherwise, an unauthenticated,
                # all-interfaces port (#27) fed one malformed body per
                # connection it never forgets
                self.manager.connections.pop(self.id, None)
                return

            if not isinstance(body, list):
                body = [body]
            self.manager.messages.append((body, self.id))
        # deliberately blind (BLE001), not for the event loop's own
        # sake: `run` is scheduled through `run_coroutine_threadsafe`,
        # whose own Future nothing here ever reads, so an unhandled
        # exception neither crashes `RpcManager`'s loop nor any other
        # connection on it -- asyncio isolates that much on its own.
        # What this catch buys instead is the only place `self.client`
        # gets closed for a failure in this method: there is no outer
        # `finally` here, so narrowing this would leak the socket this
        # unauthenticated, all-interfaces port (#27) opened, on top of
        # losing the exception itself to that same unread Future.
        # `self.manager.connections.pop` below is the same reasoning
        # the parse-error branch's own pop above already argues, applied
        # to every other way this method fails rather than only that
        # one: `ConnectionError` (an unterminated header, an overstated
        # or negative Content-Length, a peer that goes away mid-request)
        # and `TimeoutError` (`REQUEST_TIMEOUT` elapsing) never reach
        # `send()` either, and `send()` is the only other place this id
        # leaves `manager.connections` (issue #437).
        except Exception:  # noqa: BLE001
            self.client.close()
            self.manager.connections.pop(self.id, None)

    async def async_send(self, response: list[dict[str, Any]]) -> None:
        """Write `response` back as one JSON-RPC HTTP reply, then close.

        Wraps any `RawJSON` value in a fresh per-call mark before
        encoding, substitutes it back out unquoted once encoding is
        done, and frames the result behind a `Content-Length` header --
        one reply per accepted connection, closing `client` once it is
        sent.
        """
        body: list[dict[str, Any]] | dict[str, Any] = response
        if len(response) == 1:
            body = response[0]
        # A fresh token per call, not a fixed word: RawJSON's own
        # docstring has why -- a legitimate string value containing a
        # guessable mark once, unpaired, corrupts a fixed-word
        # substitution the way it cannot corrupt one this unlikely to
        # collide with.
        mark = secrets.token_hex(16)
        output_str = json.dumps(body, separators=(",", ":"), cls=JSONEncoder, mark=mark)
        # RawJSON's own placeholder, quotes and all, unquoted to the
        # exact text it carries -- before Content-Length below, which
        # has to count what is actually sent rather than what encoding
        # produced before this ran.
        output_str = re.sub(f'"{mark}(.*?){mark}"', r"\1", output_str)
        # CRLF, which is what run() above requires of a request and what
        # HTTP/1.1 specifies: this server should not emit framing it
        # would itself refuse to read.
        http_response = "HTTP/1.1 200 OK\r\n"
        http_response += "Content-Type: application/json\r\n"
        http_response += f"Content-Length: {len(output_str) + 1}\r\n"
        http_response += "\r\n"  # Important!
        http_response += output_str
        http_response += "\n"
        await self.loop.sock_sendall(self.client, http_response.encode())
        self.client.close()

    def send(self, response: list[dict[str, Any]]) -> None:
        """Schedule `async_send` on `loop`, from `handle_rpc`'s own thread."""
        asyncio.run_coroutine_threadsafe(self.async_send(response), self.loop)

    # Use with care
    def send_and_wait(self, response: list[dict[str, Any]]) -> None:
        """Like `send`, but block up to 2 seconds for the write to finish.

        `handle_rpc`'s own `stop` request is the only caller: the client
        has to see its own reply before `node.stop()` starts tearing
        `loop` down under it.
        """
        future = asyncio.run_coroutine_threadsafe(self.async_send(response), self.loop)
        with contextlib.suppress(TimeoutError):
            future.result(timeout=2)

    @override
    def __repr__(self) -> str:
        try:
            peer = self.client.getpeername()
            out = f"Connection to {ip_and_port(peer[0], peer[1])}"
        except OSError:
            out = "Broken connection"
        return out
