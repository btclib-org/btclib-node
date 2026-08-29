# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`RpcConnection`, one accepted HTTP socket carrying one or more requests.

Parses the header section off the wire, bounded by `MAX_HEADER_BYTES`
and `MAX_BODY_BYTES` since the listener this serves is bound to every
interface, and decodes the JSON-RPC batch `rpc.manager.RpcManager.messages`
queues for `rpc.main.handle_rpc`. `RawJSON` is a JSON number written
back out exactly as given, the way Core's own `UniValue` writes one
built from a string rather than from a `float`.

Matching Core's own per-version keep-alive default (`_wants_keep_alive`,
`src/httpserver.cpp:557-575`, at bitcoin/bitcoin@ca7162cde5), `async_send`
keeps the socket open across replies where the request it is answering
asked to, reading the next request off the same connection rather than
requiring a fresh accept per call (issue #640).
"""

import asyncio
import contextlib
import json
import re
import secrets
from http.client import HTTPMessage, parse_headers
from io import BytesIO
from typing import TYPE_CHECKING, Any, override

from btclib_node.p2p.address import ip_and_port
from btclib_node.rpc.errors import RpcErrorCode, error_msg

if TYPE_CHECKING:
    import socket
    from collections.abc import Callable
    from concurrent.futures import Future

    from btclib_node.rpc.manager import RpcManager

__all__ = [
    "MAX_BODY_BYTES",
    "MAX_HEADER_BYTES",
    "REQUEST_TIMEOUT",
    "JSONEncoder",
    "RawJSON",
    "RpcConnection",
]

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
# at bitcoin/bitcoin@ca7162cde5), is 30 seconds, and is what this is matched
# to -- for both of the bounds Core spends it on: one request in flight,
# and the idle gap a kept-alive connection may sit in between two of them
# before `HTTPServer::DisconnectClients` (`httpserver.cpp:1097`) drops it,
# its own `m_idle_since` reset on every receive (`:947`) and every send
# (`:1292`), not on each byte. `async_send` below re-enters this same
# `asyncio.timeout(self.request_timeout)` scope for a connection's next
# request rather than closing after its first one (issue #640), so
# REQUEST_TIMEOUT now serves both of Core's own two roles the way Core's
# own single constant does, rather than a second constant duplicating the
# same citation -- not reset per byte or per request either way, which is
# also what bounds a client dribbling one byte of a single request at a
# time forever.
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


def _wants_keep_alive(request_line: bytes | bytearray, headers: HTTPMessage) -> bool:
    """Return whether `request_line` and `headers` ask to be kept alive.

    Matches Core's own `HTTPRequest::WriteReply` (`httpserver.cpp:557-575`,
    at bitcoin/bitcoin@ca7162cde5): HTTP/1.0 defaults to closing and
    stays open only for an explicit `Connection: keep-alive`; HTTP/1.1
    defaults to keep-alive and closes only for an explicit `Connection:
    close` -- which wins where a request carries both, the way Core's
    own `close` check runs unconditionally after its two version
    branches and overrides whichever of them ran. `request_line`'s own
    trailing token is where the version lives; anything other than
    exactly `HTTP/1.0` reads as 1.1 or later, matching what this
    listener's own reply already always claims (`async_send` below)
    regardless of what the request itself asked for.
    """
    connection_header = headers.get("Connection", "").strip().casefold()
    keep_alive = not request_line.rstrip().endswith(b"HTTP/1.0")
    if connection_header == "keep-alive":
        keep_alive = True
    if connection_header == "close":
        keep_alive = False
    return keep_alive


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
        # Recomputed by `run` from each request's own `Connection`
        # header, `False` until the first one is read: `async_send`
        # reads this once a reply is ready, to decide whether to close
        # the socket or read another request off it (issue #640).
        self.keep_alive = False
        # Recomputed by `run` alongside `keep_alive`, off the same
        # request: whether the JSON `run` just parsed was a non-empty
        # array, which is what `async_send` below reads instead of its
        # own former `len(response) == 1` to decide whether to answer
        # as a bare object or keep the array shape the client sent
        # (issue #653). `len(body) > 0` rather than `isinstance(body,
        # list)` alone -- unlike Core's own `isArray()`
        # (`HTTPReq_JSONRPC`, `src/httprpc.cpp:114`, at
        # bitcoin/bitcoin@ca7162cde5), which draws no such line -- is
        # this tree's own, separate, already-tested choice to answer an
        # empty `[]` batch as a single `Invalid request` object
        # (`rpc.main.handle_rpc`'s own docstring has why); ISS 653 is
        # about a batch of one, not that one, so this keeps it as it
        # was rather than deciding it here.
        self.is_batch = False
        # A parse error's own reply, set by `run` below and never read
        # back: `asyncio.Task` only holds a *weak* reference to itself
        # in the loop's own bookkeeping, so a `Task` nothing else
        # references can be garbage-collected before it ever runs --
        # `asyncio.create_task`'s own documentation warns of exactly
        # this. Kept here purely so one survives to run, one request at
        # a time: `run` calls into this branch again only once the
        # previous one has already sent its own reply and, if kept
        # alive, read the next request in turn, so this is never
        # overwritten while still in flight.
        self._parse_error_reply: asyncio.Task[None] | None = None

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

        Called again, by `async_send` below, for every request after the
        first one a kept-alive connection carries -- `self.buffer` is
        trimmed to what is left after this request's own body before
        that request is queued, so a second call starts clean rather
        than re-reading bytes this one already consumed.
        """
        try:
            async with asyncio.timeout(self.request_timeout):
                await self._recv_until(
                    lambda: HEADER_TERMINATOR in self.buffer, MAX_HEADER_BYTES
                )
                head, _, self.buffer = self.buffer.partition(HEADER_TERMINATOR)
                # parse_headers wants the field lines alone, so the
                # request line is split off on its own rather than
                # simply dropped -- its own trailing token is what
                # `keep_alive` below reads.
                request_line, _, fields = head.partition(b"\r\n")
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
                self.keep_alive = _wants_keep_alive(request_line, headers)
                await self._recv_until(lambda: len(self.buffer) >= length)

            body_bytes = self.buffer[:length]
            # Whatever is left belongs to a request after this one --
            # pipelined ahead of its own reply, or simply not sent yet --
            # and must not be replayed as part of this request's own
            # body on a second call to this method.
            self.buffer = self.buffer[length:]
            try:
                body = json.loads(body_bytes)
            except ValueError:
                # JSON-RPC 2.0 section 5.1's own `PARSE_ERROR`, id
                # `null`: a body that is not JSON is not a request this
                # node can read enough of to disagree with, where the
                # header section above already parsed. `ValueError` and
                # not `json.JSONDecodeError` alone, since malformed
                # bytes `json.loads` cannot even decode as text raise
                # the stdlib's own `UnicodeDecodeError`, a `ValueError`
                # too and the same "invalid JSON" from the client's side.
                # Scheduled as a task of its own, the same seam `send`
                # below reaches this same coroutine through, rather than
                # awaited in this very frame: `self.keep_alive` --
                # already read off this request's own headers above -- is
                # honoured here exactly as it is for a dispatched reply,
                # matching Core's own `HTTPReq_JSONRPC`
                # (`src/httprpc.cpp:232-244`, at bitcoin/bitcoin@ca7162cde5),
                # which negotiates keep-alive on a parse error the same
                # way it does on any other reply -- so where it says to,
                # `async_send` calls back into `run` for a further
                # request, and doing that by awaiting it here would grow
                # this coroutine's own stack by one frame per malformed
                # body a kept-alive connection sent in a row, rather than
                # by a scheduled task's own fresh one each time, the way
                # `send` below already keeps a dispatched reply's own
                # recursion flat.
                #
                # `self.loop.create_task`, not `send`'s own
                # `run_coroutine_threadsafe`: this method already runs on
                # `self.loop`'s own thread, so `run_coroutine_threadsafe`
                # here would only requeue itself onto the very loop it is
                # already running on, through `call_soon_threadsafe` -- a
                # Task made real one further turn later rather than this
                # one, invisible to `asyncio.all_tasks()` for that whole
                # turn. `RpcManager.stop` reads `all_tasks()` exactly
                # once, to build the set it cancels; a task not yet in it
                # that turn is not in the set it cancels either, and never
                # gets to run at all once `stop` has since closed the loop
                # under it (measured against the unmodified `stop()`,
                # issue #640 review round 2). `self.loop.create_task` --
                # unlike `send`'s own cross-thread call, which does need
                # the thread-safe seam -- makes the `Task` object exist
                # synchronously, in time for that one read to find it.
                #
                # Left in `manager.connections` rather than popped here,
                # unlike a dispatched reply's own pop in
                # `rpc.main.handle_rpc`: that pop is safe precisely
                # because it runs on `Node`'s own thread, the same one
                # `stop` is called from, so the two cannot interleave
                # (issue #640 review round 2) -- this method runs on
                # `self.loop`'s own thread instead, where a pop here could
                # still race `stop`'s own socket-closing sweep of
                # `manager.connections`, the second collection that sweep
                # reads, the same way popping raced its first. Leaving the
                # entry in place keeps that sweep able to close this
                # connection's own socket even on a turn where the task
                # above never gets to run at all; `async_send` below pops
                # it once it does, on the branch that closes rather than
                # keeps this connection.
                # Assigned to `self._parse_error_reply` (its own
                # docstring above has why) rather than left a bare
                # statement: unlike `run_coroutine_threadsafe` elsewhere
                # in this class, `create_task` returns an `Awaitable`,
                # which mypy's own `unused-awaitable` flags as a
                # likely-missing `await` when discarded outright.
                # A body `json.loads` could not even parse is never a
                # batch, whatever it superficially looked like -- and
                # this is set unconditionally rather than left at
                # whatever a previous request on a kept-alive connection
                # last set it to, which this reply would otherwise
                # inherit.
                self.is_batch = False
                self._parse_error_reply = self.loop.create_task(
                    self.async_send(
                        [error_msg(RpcErrorCode.PARSE_ERROR, "Parse error")]
                    )
                )
                return

            self.is_batch = isinstance(body, list) and len(body) > 0
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
        # `self.manager.connections.pop` below covers every other way
        # this method fails: `ConnectionError` (an unterminated header,
        # an overstated or negative Content-Length, a peer that goes away
        # mid-request) and `TimeoutError` (`REQUEST_TIMEOUT` elapsing)
        # never reach `send()` or `async_send`'s own close branch either,
        # and one of those two is otherwise the only place this id leaves
        # `manager.connections` (issue #437). Popped in the same
        # statement group as `self.client.close()` above rather than
        # scheduled apart from it the way the parse-error branch's own
        # reply is: there is no gap here for `RpcManager.stop`'s sweep to
        # land in the middle of, since both run synchronously, in this
        # thread, before this method's own frame returns.
        except Exception:  # noqa: BLE001
            self.client.close()
            self.manager.connections.pop(self.id, None)

    async def async_send(self, response: list[dict[str, Any]]) -> None:
        """Write `response` back as one JSON-RPC HTTP reply.

        Wraps any `RawJSON` value in a fresh per-call mark before
        encoding, substitutes it back out unquoted once encoding is
        done, and frames the result behind a `Content-Length` header.
        `self.keep_alive` -- `_wants_keep_alive`'s own answer, set by
        `run` above off the request this is answering -- decides what
        happens once the reply is on the wire: `client` closes, or this
        reads another request off the same socket, matching Core's own
        per-version keep-alive default either way (issue #640).
        `self.is_batch`, set by `run` off the same request, decides
        whether `response` stays an array here: unwrapping it purely
        from `len(response) == 1` used to answer a one-member batch
        with the same bare object a lone request gets, with nothing
        left in `response` by then to tell the two apart (issue #653).
        """
        body: list[dict[str, Any]] | dict[str, Any] = response
        if not self.is_batch:
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
        if not self.keep_alive:
            # Only written where this is closing: HTTP/1.1 defaults to
            # keep-alive with no header at all (Core's own
            # `HTTPRequest::WriteReply`, as above), and a close this
            # reply does not announce is one `http.client`'s own
            # `HTTPResponse.will_close` -- which reads this header and
            # not the socket's own fate -- would still read as open,
            # which is exactly the race issue #640 is about: a pooling
            # client kept believing a connection reusable past this
            # node's own close of it.
            http_response += "Connection: close\r\n"
        http_response += f"Content-Length: {len(output_str) + 1}\r\n"
        http_response += "\r\n"  # Important!
        http_response += output_str
        http_response += "\n"
        await self.loop.sock_sendall(self.client, http_response.encode())
        if self.keep_alive:
            # Back in `manager.connections` before the read below can
            # possibly complete: `handle_rpc`'s own call into `send`,
            # which scheduled this coroutine, already popped this id out
            # unconditionally once it did, on the assumption every reply
            # closes -- true of every reply but this one now.
            self.manager.connections[self.id] = self
            await self.run()
        else:
            self.client.close()
            # A no-op, `pop`'s own default absorbing it, for a dispatched
            # reply: `handle_rpc` already popped this id out once it
            # scheduled this coroutine. The parse-error branch of `run`
            # above pops nothing itself, deliberately, so this is what
            # removes its entry once this connection is actually done
            # rather than only kept reachable for `RpcManager.stop`'s own
            # socket-closing sweep in the meantime (issue #640 review
            # round 2).
            self.manager.connections.pop(self.id, None)

    def send(self, response: list[dict[str, Any]]) -> None:
        """Schedule `async_send` on `loop`, from `handle_rpc`'s own thread."""
        asyncio.run_coroutine_threadsafe(self.async_send(response), self.loop)

    # Use with care
    def send_and_wait(self, response: list[dict[str, Any]]) -> None:
        """Like `send`, but block up to 2 seconds for the write to finish.

        `handle_rpc`'s own `stop` request is the only caller: the client
        has to see its own reply before `node.stop()` starts tearing
        `loop` down under it. Forces a close of its own regardless of
        what the request asked for, whatever `run` last set
        `self.keep_alive` to -- `RpcManager.stop`, called right after
        this returns, tears the whole loop down, so there is no next
        request this connection could still answer.
        """
        self.keep_alive = False
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
