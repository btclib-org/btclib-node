# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`RpcConnection`, one accepted HTTP socket carrying one or more requests.

Parses the header section off the wire, bounded by `MAX_HEADER_BYTES`
and `MAX_BODY_BYTES` since both are read before any credential is
checked, answers a request line, a header field, a `Content-Length` or
a chunked body Core's HTTP server cannot read with 400 or 413
(`_HeadReader`, `_ChunkedReader`), answers `manager.interrupted` with
503 once `RpcManager.interrupt` has set it (issue #1515), refuses a
method or a path Core refuses before it checks a credential
(`_dispatch_refusal`), answers a request whose `Authorization` header
`rpc.auth.RpcAuth` does not accept with 401, and decodes the body of one
it does accept, which `rpc.manager.RpcManager.messages` queues for
`rpc.main.handle_rpc`. `RawJSON` is a JSON number written back out
exactly as given, the way Core's own `UniValue` writes one built from a
string rather than from a `float`.

Each answer is framed as Core's `HTTPRequest::WriteReply` frames it
(`_frame`), in the version of the request it answers, and the socket is
kept open across answers where Core keeps it, the next request read off
the same connection rather than requiring a fresh accept per call
(issue #640).

The HTTP server read is Core's own, `src/httpserver.cpp` at
bitcoin/bitcoin@f0de87f040 -- v32.1, the release
`integration-bitcoind.yml` pins.
"""

import asyncio
import contextlib
import dataclasses
import json
import re
import secrets
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, override

from bitcoin_core_rpc import RPCErrorCode

from btclib_node.constants import RPC_WORK_QUEUE
from btclib_node.exceptions import (
    IncompleteRequestHeadError,
    MalformedRequestHeadError,
    OversizedRequestBodyError,
)
from btclib_node.p2p.address import ip_and_port
from btclib_node.rpc.auth import (
    FAILED_ATTEMPT_DELAY,
    WWW_AUTHENTICATE,
    Refusal,
)
from btclib_node.rpc.jsonrpc import NO_CONTENT, HttpReply, decode, error_reply

if TYPE_CHECKING:
    import socket
    from collections.abc import Callable, Coroutine
    from concurrent.futures import Future

    from btclib_node.rpc.manager import RpcManager

__all__ = [
    "COIN",
    "MAX_BODY_BYTES",
    "MAX_HEADER_BYTES",
    "REQUEST_TIMEOUT",
    "JSONEncoder",
    "RawJSON",
    "RequestHead",
    "RpcConnection",
    "btc_amount",
    "parse_request_head",
]

# Bounds on the read below, which is fed by whoever connects: the header
# section and the body are read whole before `RpcConnection.run` checks
# a credential, as Core's HTTP server reads a request before
# `HTTPReq_JSONRPC` sees it. Both are Core's own (`src/httpserver.h`,
# at bitcoin/bitcoin@f0de87f040, the v32.1 tag): `MAX_HEADERS_SIZE` caps
# each line `HTTPRemoteClient::ReadRequest` reads and the header section
# `HTTPHeaders::Read` counts (`_LineReader`, `_FieldReader`), and
# `MAX_BODY_SIZE` caps the body.
MAX_HEADER_BYTES = 8192
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
# that would otherwise disagree with an int-inferred attribute. This is
# `cli.py`'s own default for `-rpcservertimeout`, which is what
# `RpcManager.request_timeout` actually carries once a `Node` is built
# from a `Config` -- `rpc.manager._request_timeout`'s own docstring has
# Core's `0` and `-1`, both of which turn this into `None` instead.
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


# Core's own `COIN` (`src/consensus/amount.h`, at bitcoin/bitcoin@9be056a8a7)
COIN = 100_000_000


def btc_amount(sats: int) -> RawJSON:
    """Format a satoshi amount as Core's own exact BTC string.

    Core's own `ValueFromAmount` (`src/core_io.cpp:285-296`,
    at bitcoin/bitcoin@9be056a8a7): integer `amount / COIN` and
    `amount % COIN`, formatted `%d.%08d` -- exact at every magnitude,
    where a Python float division (`sats / 1e8`) serializes through
    `repr`, which fixes no decimal places and emits exponent notation
    (`1e-06`) at a magnitude ordinary for a feerate. A negative amount,
    which a decoded output can carry (`CAmount` is signed), keeps its
    sign in front, as Core's own version places it.
    """
    quotient, remainder = divmod(abs(sats), COIN)
    sign = "-" if sats < 0 else ""
    return RawJSON(f"{sign}{quotient}.{remainder:08d}")


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


# `HTTPStatusReasonString` (`src/rpc/protocol.h`, at bitcoin/bitcoin@f0de87f040,
# the v32.1 tag) gives each status its reason phrase
_BAD_REQUEST = "400 Bad Request"
_NOT_FOUND = "404 Not Found"
_METHOD_NOT_ALLOWED = "405 Method Not Allowed"
_CONTENT_TOO_LARGE = "413 Content too large"
_SERVICE_UNAVAILABLE = "503 Service Unavailable"
# What `WriteNoStoreErrorReply` adds to each answer Core's HTTP server
# writes itself, before a request reaches `HTTPReq_JSONRPC`
_NO_STORE = "Cache-Control: no-store"
# The methods `HTTPRequest::LoadControlData` names, compared
# case-sensitively: any other method is `HTTPRequestMethod::UNKNOWN`
_CORE_METHODS = frozenset((b"GET", b"POST", b"HEAD", b"PUT"))
# `MIN_REQUEST_LINE_LENGTH`, and the words a request line is
_MIN_REQUEST_LINE = len(b"GET / HTTP/1.0")
_REQUEST_LINE_WORDS = len(("method", "target", "version"))
# `LoadControlData`'s version: one digit, a point and one digit, the
# first of them a 1
_HTTP_VERSION = re.compile(rb"HTTP/1\.([0-9])")
# what `util::TrimString` trims, and what a field name may not hold
_WHITESPACE = b" \f\n\r\t\v"
# what a field line may not hold, its ending apart
_INVALID_IN_FIELD = re.compile(rb"[\r\n\0]")
# `ToIntegral<uint64_t>`: `std::from_chars` over the whole value, which
# takes no sign and no white space, in base 10 or 16, and the most
# digits a `uint64_t` has in each
_UINT64_MAX = 2**64 - 1
_DIGITS = {10: re.compile(rb"[0-9]+"), 16: re.compile(rb"[0-9A-Fa-f]+")}
_UINT64_WIDTH = {10: len(str(_UINT64_MAX)), 16: len(f"{_UINT64_MAX:x}")}


def _uint64(text: bytes, base: int) -> int | None:
    """Return `ToIntegral<uint64_t>(text, base)`, `None` where it fails.

    A number past `uint64_t` fails, as `std::from_chars` answers
    `result_out_of_range` for it.
    """
    if _DIGITS[base].fullmatch(text) is None:
        return None
    digits = text.lstrip(b"0") or b"0"
    # measured as text first: `int` refuses a string past 4300 digits
    if len(digits) > _UINT64_WIDTH[base]:
        return None
    number = int(digits, base)
    return number if number <= _UINT64_MAX else None


def _request_line(line: bytes) -> tuple[bytes, bytes, tuple[int, int]]:
    """Return the method, target and version `LoadControlData` reads.

    The line is at least `MIN_REQUEST_LINE_LENGTH` octets, holds no NUL,
    and is three words, one space apart. Raises
    `MalformedRequestHeadError` where Core refuses it.
    """
    parts = line.split(b" ")
    if (
        len(line) < _MIN_REQUEST_LINE
        or b"\0" in line
        or len(parts) != _REQUEST_LINE_WORDS
    ):
        detail = "request line not method, target and version"
        raise MalformedRequestHeadError(detail)
    method, target, version = parts
    match = _HTTP_VERSION.fullmatch(version)
    if match is None:
        detail = "unsupported HTTP version"
        raise MalformedRequestHeadError(detail)
    return method, target, (1, int(match[1]))


def _dispatch_refusal(method: bytes, target: bytes) -> str | None:
    """Return the status Core's dispatcher refuses `method` at `target` with.

    `MaybeDispatchRequestToWorker` (`src/httpserver.cpp`, with
    `StartHTTPRPC` in `src/httprpc.cpp`, at bitcoin/bitcoin@f0de87f040,
    the v32.1 tag) answers these on its socket thread, with no body and
    `_NO_STORE`, before any worker reads `Authorization`:

    - a method outside `_CORE_METHODS` is 405, on any path;
    - a target other than exactly `/` or one starting `/wallet/` matches
      no handler `StartHTTPRPC` registers, and is 404.

    `/wallet/` is registered where `HasWalletSupport()` holds, as it
    does in the `bitcoind` release binary, which answers there. The only
    other handlers are `StartREST`'s, registered only under `-rest`, off
    by default (`DEFAULT_REST_ENABLE`, `src/init.cpp`); this node has no
    REST interface, so `/rest/` is 404 here as in Core at its default.
    The target is compared as the raw request-target, the way Core
    compares `GetURI()`: a query string or an absolute URI matches
    neither handler.
    """
    if method not in _CORE_METHODS:
        return _METHOD_NOT_ALLOWED
    if target != b"/" and not target.startswith(b"/wallet/"):
        return _NOT_FOUND
    return None


def _framing_status(
    error: MalformedRequestHeadError | OversizedRequestBodyError,
) -> str:
    """Return the status Core answers a request it cannot read with.

    `HTTPRemoteClient::TryReadRequest` (`src/httpserver.cpp`, at
    bitcoin/bitcoin@f0de87f040, the v32.1 tag): 413 for a body past
    `MAX_BODY_BYTES`, 400 for the rest.
    """
    if isinstance(error, OversizedRequestBodyError):
        return _CONTENT_TOO_LARGE
    return _BAD_REQUEST


_CR = ord("\r")


class _LineReader:
    r"""`util::LineReader` over a buffer that grows.

    A line ends at a line feed, a carriage return before it dropped, and
    is taken off the front of `buffer` as it is read. A line of more
    than `MAX_HEADER_BYTES` octets before its line feed is refused,
    whether or not the line feed has arrived: `HTTPRemoteClient::
    ReadRequest` builds its reader with `MAX_HEADERS_SIZE` as the line
    limit. `consumed` counts the octets taken, line endings included,
    and `record`, where given, collects them. Each octet is searched for
    a line feed once: `scan` is how much of `buffer` is known to hold
    none, so a line arriving a byte at a time is not searched again from
    its start.
    """

    def __init__(self, buffer: bytearray, record: bytearray | None = None) -> None:
        """Read lines off the front of `buffer`, into `record` where given."""
        self.buffer = buffer
        self.record = record
        self.consumed = 0
        self.scan = 0

    def readln(self) -> bytes | None:
        """Take the next whole line, its ending dropped; `None` if none yet.

        Raises `MalformedRequestHeadError` for a line past the limit.
        """
        eol = self.buffer.find(b"\n", self.scan)
        if (len(self.buffer) if eol == -1 else eol) > MAX_HEADER_BYTES:
            detail = "a line past MAX_HEADER_BYTES"
            raise MalformedRequestHeadError(detail)
        if eol == -1:
            self.scan = len(self.buffer)
            return None
        end = eol - 1 if eol and self.buffer[eol - 1] == _CR else eol
        line = bytes(self.buffer[:end])
        self.take(eol + 1)
        return line

    def take(self, size: int) -> bytes:
        """Take `size` octets off the front of `buffer`."""
        taken = bytes(self.buffer[:size])
        del self.buffer[:size]
        self.scan = 0
        self.consumed += size
        if self.record is not None:
            self.record += taken
        return taken


class _FieldReader:
    """`HTTPHeaders::Read`: a header section's field lines off `lines`.

    `size` is Core's `m_consumed`: the octets of each field line read,
    its ending included, and of the empty line that ends the section,
    the request line left out. Past `MAX_HEADER_BYTES` the section is
    refused, and a chunked body's trailer is counted on from the
    request's own section. A line holding a carriage return, a line feed
    or a NUL is refused, and so is one with no colon or whose name is
    empty or holds white space, a line starting with a space or a tab
    among them. A value is what follows the first colon, white space
    trimmed at both ends. `fields` keeps every field in order, a name
    named twice included; a trailer's are read and not kept (`keep`).
    """

    def __init__(self, lines: _LineReader, size: int = 0, *, keep: bool = True) -> None:
        """Read field lines off `lines`, `size` octets already counted."""
        self.lines = lines
        self.size = size
        self.keep = keep
        self.fields: list[tuple[bytes, bytes]] = []

    def read(self) -> bool:
        """Read what `lines` holds; `True` once the section's end is read.

        Raises `MalformedRequestHeadError` where Core refuses it.
        """
        while True:
            start = self.lines.consumed
            if (line := self.lines.readln()) is None:
                return False
            self.size += self.lines.consumed - start
            if self.size > MAX_HEADER_BYTES:
                detail = "header section past MAX_HEADER_BYTES"
                raise MalformedRequestHeadError(detail)
            if not line:
                return True
            key, colon, value = line.partition(b":")
            if (
                _INVALID_IN_FIELD.search(line)
                or not colon
                or not key
                or any(c in _WHITESPACE for c in key)
            ):
                detail = f"header field {len(self.fields)} of {len(line)} octets"
                raise MalformedRequestHeadError(detail)
            if self.keep:
                self.fields.append((key, value.strip(_WHITESPACE)))


@dataclass(frozen=True)
class RequestHead:
    r"""One request's own header section, and what its answer is framed by.

    `raw` is the octets read for it, which `serialize` gives back -- the
    same round-trip `tests/fuzz_corpus_test.py` already holds
    `p2p.transport.frame_message_bytes` to. `consumed` is its length.

    `method`, `target` and `version` are the request line's three parts,
    `b""` and `None` where Core had not read them. `fields` is every
    field `_FieldReader` read, in order, and `size` the octets it
    counted. `chunked` is `LoadBody`'s own test, a first
    `Transfer-Encoding` of `chunked`, and `length` the body's
    `Content-Length` where it is not.

    `error` is what Core refuses the request for, answered with no body
    and the connection closed, `fields` then holding what was read
    before it.

    `raw` and `fields` hold the `Authorization` field, so `repr` leaves
    them out (section 7 of the btclib-org standard, *A refusal carries
    no secret*).
    """

    raw: bytes = dataclasses.field(repr=False)
    method: bytes
    target: bytes
    version: tuple[int, int] | None
    fields: tuple[tuple[bytes, bytes], ...] = dataclasses.field(repr=False)
    size: int
    chunked: bool
    length: int
    error: MalformedRequestHeadError | OversizedRequestBodyError | None

    @property
    def consumed(self) -> int:
        """How many octets the section took off the front of its buffer."""
        return len(self.raw)

    def serialize(self) -> bytes:
        """Reproduce the exact octets the section was read from."""
        return self.raw

    def field(self, name: bytes) -> str | None:
        """Return the first `name` field's value, `None` where there is none.

        `HTTPHeaders::FindFirst`, comparing names ASCII
        case-insensitively, as `bytes.lower` does. The value is decoded
        as Latin-1, where `str.lower` changes no character into an ASCII
        one.
        """
        name = name.lower()
        for key, value in self.fields:
            if key.lower() == name:
                return value.decode("latin-1")
        return None

    @property
    def connection(self) -> str | None:
        """The first `Connection` field's value."""
        return self.field(b"Connection")

    @property
    def authorization(self) -> str | None:
        """The first `Authorization` field's value."""
        return self.field(b"Authorization")


class _HeadReader:
    """`HTTPRemoteClient::ReadRequest` over a growing buffer, up to the body.

    `LoadControlData`, then `LoadHeaders`, then what `LoadBody` reads off
    the fields before the body: a chunked body, or a `Content-Length`.
    `read` is called again as more of `buffer` arrives, carrying on from
    the line it stopped at, and takes each line off the front of
    `buffer` as it reads it.
    """

    def __init__(self, buffer: bytearray) -> None:
        """Read one request's header section off the front of `buffer`."""
        self.raw = bytearray()
        self.lines = _LineReader(buffer, self.raw)
        self.method = self.target = b""
        self.version: tuple[int, int] | None = None
        self.fields: _FieldReader | None = None

    def read(self) -> RequestHead | None:
        """Read what `buffer` holds, `None` until the section is whole.

        A section Core refuses is returned with its `error` set.
        """
        try:
            if not self._read_section():
                return None
        except MalformedRequestHeadError as e:
            return self._head(e)
        head = self._head(None)
        encoding = head.field(b"Transfer-Encoding")
        if encoding is not None and encoding.lower() == "chunked":
            return replace(head, chunked=True)
        try:
            return replace(head, length=_content_length(head))
        except (MalformedRequestHeadError, OversizedRequestBodyError) as e:
            return replace(head, error=e)

    def _read_section(self) -> bool:
        """Read the request line, then fields; `True` once the section ends."""
        if self.fields is None:
            if (line := self.lines.readln()) is None:
                return False
            self.method, self.target, self.version = _request_line(line)
            self.fields = _FieldReader(self.lines)
        return self.fields.read()

    def _head(self, error: MalformedRequestHeadError | None) -> RequestHead:
        fields = self.fields
        return RequestHead(
            raw=bytes(self.raw),
            method=self.method,
            target=self.target,
            version=self.version,
            fields=() if fields is None else tuple(fields.fields),
            size=0 if fields is None else fields.size,
            chunked=False,
            length=0,
            error=error,
        )


def _content_length(head: RequestHead) -> int:
    """Return the body length `head` declares, as `LoadBody` reads it.

    No `Content-Length` is no body. Every `Content-Length` must carry
    the first one's value, which `ToIntegral<uint64_t>` must read.
    Raises `MalformedRequestHeadError` where Core answers 400, and
    `OversizedRequestBodyError` for a length past `MAX_BODY_BYTES`, the
    `MAX_BODY_SIZE` it answers 413.
    """
    values = [value for key, value in head.fields if key.lower() == b"content-length"]
    if not values:
        return 0
    if any(value != values[0] for value in values):
        detail = "Content-Length values that differ"
        raise MalformedRequestHeadError(detail)
    length = _uint64(values[0], 10)
    if length is None:
        detail = f"Content-Length {values[0]!r}"
        raise MalformedRequestHeadError(detail)
    if length > MAX_BODY_BYTES:
        detail = f"Content-Length past {MAX_BODY_BYTES}"
        raise OversizedRequestBodyError(detail)
    return length


class _ChunkedReader:
    """`HTTPRequest::LoadBody` over a chunked body, then its trailer.

    A size line is read up to its first `;`, white space trimmed, as a
    hexadecimal `ToIntegral<uint64_t>`. A size's data is taken whole,
    and the line after it must be empty. A size of 0 ends the body, and
    the trailer after it is a header section read as `_FieldReader`
    reads one, counted on from `head`'s and not kept. A size line Core
    cannot read, data not ended by an empty line and a trailer it
    refuses raise `MalformedRequestHeadError`, answered 400; a body
    past `MAX_BODY_BYTES` raises `OversizedRequestBodyError`, answered
    413.
    """

    def __init__(self, buffer: bytearray, head: RequestHead) -> None:
        """Read `head`'s chunked body off the front of `buffer`."""
        self.lines = _LineReader(buffer)
        self.head = head
        self.body = bytearray()
        # Core's `m_chunk_size`, `None` where a size line is next
        self.size: int | None = None
        # whether the data of a chunk of `size` octets is taken
        self.taken = False
        self.trailer: _FieldReader | None = None

    def read(self) -> bool:
        """Read what `buffer` holds; `True` once the trailer is whole."""
        while self.trailer is None:
            if self.size is None:
                if (line := self.lines.readln()) is None:
                    return False
                self.size = self._chunk_size(line)
                if not self.size:
                    self.trailer = _FieldReader(self.lines, self.head.size, keep=False)
                    break
            if not self.taken:
                if len(self.lines.buffer) < self.size:
                    return False
                self.body += self.lines.take(self.size)
                self.taken = True
            if (line := self.lines.readln()) is None:
                return False
            if line:
                detail = "a chunk not ended by an empty line"
                raise MalformedRequestHeadError(detail)
            self.size = None
            self.taken = False
        return self.trailer.read()

    def _chunk_size(self, line: bytes) -> int:
        """Return the size `line` declares, refusing what Core refuses."""
        size = _uint64(line.partition(b";")[0].strip(_WHITESPACE), 16)
        if size is None:
            detail = f"chunk size line of {len(line)} octets"
            raise MalformedRequestHeadError(detail)
        if size > MAX_BODY_BYTES - len(self.body):
            detail = "chunked body past MAX_BODY_BYTES"
            raise OversizedRequestBodyError(detail)
        return size


def parse_request_head(data: bytes) -> RequestHead:
    """Parse one request's header section off the front of `data`.

    The framing half of what `RpcConnection.run` does, pulled out so
    `fuzz/fuzz_rpc_head.py` can drive it over raw octets the way Core's
    own `http_request.cpp` fuzz target drives
    `HTTPRequest::LoadControlData`/`LoadHeaders` over a raw
    `http_buffer` (at bitcoin/bitcoin@ca7162cde5) -- scoped the same
    way that target is: request-line and header-field framing and the
    `Content-Length` drawn from them, never the JSON-RPC body those
    bytes go on to carry, which is stdlib `json`'s own business (`run`
    below) and not this node's.

    Read as `_HeadReader` reads a section. Raises
    `IncompleteRequestHeadError` where `data` holds no whole section and
    Core would wait for more -- `run`'s own call site never hits this,
    since it reads more instead, but a fuzzed byte string has no such
    guarantee. Raises `MalformedRequestHeadError`, answered 400, for a
    section Core refuses, and `OversizedRequestBodyError` for a
    `Content-Length` past `MAX_BODY_BYTES`, which Core answers 413.
    """
    head = _HeadReader(bytearray(data)).read()
    if head is None:
        raise IncompleteRequestHeadError
    if head.error is not None:
        raise head.error
    return head


class RpcConnection:
    """One accepted RPC socket, from the header read through the reply.

    `RpcManager.server` builds one per client `_accept_loop` queues, on this
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
        request_timeout: float | None = REQUEST_TIMEOUT,
    ) -> None:
        """Set up empty buffers for `client`, tracked under `connection_id`.

        `request_timeout` is `REQUEST_TIMEOUT` unless `manager` -- in
        practice `RpcManager.create_connection`, handing over
        `manager.request_timeout` -- is built or told to hand over
        something else, which is the seam a test uses to keep
        `REQUEST_TIMEOUT`'s own real, Core-matching value off its own
        critical path. `None` is `-rpcservertimeout=0` or `=-1`, Core's
        own two ways of arming no timeout at all
        (`rpc.manager._request_timeout`'s own docstring).
        """
        super().__init__()
        self.loop = loop
        self.client = client
        self.manager = manager
        self.id = connection_id
        self.rpc_id = ""
        self.messages: list[Any] = []
        # A `bytearray`, not `bytes`: `_recv`'s own `+=` below is
        # an in-place, amortised extend on this type and a full copy of
        # everything held so far on the other -- btclib-org/btclib-node#466,
        # the same shape btclib-org/btclib-node#438 fixed on the p2p side.
        self.buffer = bytearray()
        self.request_timeout = request_timeout
        # The request being answered, set by `run` as each one is read
        # and framing its answer (`_frame`)
        self.head: RequestHead | None = None
        # Set by `_frame` as each answer is framed, `False` until then:
        # `_write` reads it once the answer is on the wire, to decide
        # whether to close the socket or read another request off it
        # (issue #640).
        self.keep_alive = False
        # Set by `_frame` with `keep_alive`: whether the request answered
        # closes the connection itself, by its version or its
        # `Connection` field, rather than by `_frame`'s `close`. `_write`
        # reads it to decide whether Core would read one more request
        # off the buffer (btclib-org/btclib-node#2101).
        self._request_closes = False
        # A `-rpcwhitelist` refusal's own reply, kept for the reason
        # `_parse_error_reply` below is
        self._whitelist_reply: asyncio.Task[None] | None = None
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
        # A 401's own reply, kept for the reason `_parse_error_reply`
        # above is
        self._unauthorized_reply: asyncio.Task[None] | None = None
        # A `_send_refusal`'s own reply, kept for the same reason
        self._refusal_reply: asyncio.Task[None] | None = None
        # A warmup answer, kept for the same reason
        self._warmup_reply: asyncio.Task[None] | None = None

    def close(self) -> None:
        """Close `client`.

        Cancelling whatever this connection is doing -- reading a
        request, writing a reply -- is `RpcManager.stop`'s own job:
        its `asyncio.all_tasks(self.loop)` sweep already reaches
        whichever task is actually live for this connection, cancelled
        and driven to completion before this is ever called, so this
        method does not need a handle of its own to end one -- an
        earlier version kept one anyway (`self.task`), set once at
        accept and never again, so past a connection's first request
        it named a long-finished `Future` and cancelled nothing
        (issue #714). Core's own per-connection object,
        `HTTPRemoteClient`, carries no such handle either:
        `HTTPServer::ClearConnectedClients` (`src/httpserver.cpp:1160-1167`,
        at bitcoin/bitcoin@ca7162cde5), its own shutdown-time sweep,
        drops whatever is left in `m_connected` the same unconditional
        way, once its own socket-handling thread has already been
        joined, rather than reaching into a live worker to end it.
        """
        self.client.close()

    async def _recv_until(self, predicate: Callable[[], bool]) -> None:
        while not predicate():
            await self._recv()

    async def _recv(self) -> None:
        """Add what `client` sends to `self.buffer`; at its end, raise."""
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

    async def _read_body(self, head: RequestHead) -> tuple[RequestHead, bytes] | None:
        """Read `head`'s body off `client`, `None` where it is refused.

        A refusal, `head`'s own or `_ChunkedReader`'s, is answered by
        `_send_framing_error`. What is left after the body belongs to a
        request after this one -- pipelined ahead of its own reply, or
        simply not sent yet -- and is left in `self.buffer` for it.
        """
        if head.error is not None:
            self._send_framing_error(head.error)
            return None
        if not head.chunked:
            length = head.length
            await self._recv_until(lambda: len(self.buffer) >= length)
            body = bytes(self.buffer[:length])
            del self.buffer[:length]
            return head, body
        chunked = _ChunkedReader(self.buffer, head)
        try:
            while not chunked.read():
                await self._recv()
        except (MalformedRequestHeadError, OversizedRequestBodyError) as e:
            self._send_framing_error(e)
            return None
        return head, bytes(chunked.body)

    async def run(self) -> None:
        """Read one request off `client` and queue it for `handle_rpc`.

        Reads the header section a line at a time, as `_HeadReader` does,
        then the body, up to its own `Content-Length` or as
        `_ChunkedReader` reads a chunked one, both bounded against an
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

        In the order Core answers them, and none of them queued: a
        header section `_HeadReader` refuses, or a chunked body
        `_ChunkedReader` refuses, is answered 400 or 413 by
        `_send_framing_error`, as soon as it is refused;
        `manager.interrupted` is answered 503, by
        `_refused_early` ahead of everything below it, or by
        `_send_shutdown_refusal` where it is set only once
        `_refused_early` has let the request through (issue #1515); a
        method or a target `_dispatch_refusal` refuses is answered by
        `_send_refusal`, whatever the credential; past the queue checks,
        a method other than `POST` is answered 405 by `_queue`; a
        request whose `Authorization` header `manager.auth` does not
        accept is answered 401 by `_send_unauthorized`, its body read off the
        socket and never decoded; and one whose decoded body
        `manager.auth.refusal` refuses is answered by
        `_send_whitelist_refusal`.

        Called again, by `async_send` below, for every request after the
        first one a kept-alive connection carries -- what this request
        takes is taken off the front of `self.buffer` as it is read, so a
        second call starts on what is left rather than re-reading bytes
        this one already consumed.
        """
        try:
            async with asyncio.timeout(self.request_timeout):
                # `_HeadReader` is what `fuzz/fuzz_rpc_head.py` also
                # drives, through `parse_request_head`, directly over
                # octets -- the latter's own docstring is where the
                # framing/JSON-body scoping boundary is argued.
                reader = _HeadReader(self.buffer)
                while (head := reader.read()) is None:
                    await self._recv()
                self.head = head
                read = await self._read_body(head)
            if read is None:
                return
            head, body_bytes = read
            # Ahead of the credential check below, as in Core, and once
            # the body is read, so that a connection `_frame` keeps goes
            # on to its next request after the refusal.
            if self._refused_early(head):
                return
            # Core's `ThreadPool::Submit` reads `m_interrupt` and queues
            # under the one `m_mutex` `Interrupt` sets it under
            # (`src/util/threadpool.h`, at bitcoin/bitcoin@9be056a8a7, the
            # v31.1 tag), so a request is either queued before
            # `RpcManager.interrupt` returns, and drained, or refused
            # here. Held through the credential check below as well,
            # which Core makes on a worker, after `Submit`.
            with self.manager.queue_lock:
                if self.manager.interrupted.is_set():
                    self._refusal_reply = self._start_reply(
                        self._send_shutdown_refusal()
                    )
                    return
                if len(self.manager.messages) >= RPC_WORK_QUEUE:
                    self._refusal_reply = self._start_reply(self._send_queue_refusal())
                    return
                self._queue(head, body_bytes)
        # deliberately blind (BLE001), not for the event loop's own
        # sake: `run` is scheduled through `run_coroutine_threadsafe`,
        # whose own Future nothing here ever reads, so an unhandled
        # exception neither crashes `RpcManager`'s loop nor any other
        # connection on it -- asyncio isolates that much on its own.
        # What this catch buys instead is the only place `self.client`
        # gets closed for a failure in this method: there is no outer
        # `finally` here, so narrowing this would leak a socket anybody
        # reaching the port can open, credential or not, on top of
        # losing the exception itself to that same unread Future.
        # `self.manager.forget_connection` below covers every other way
        # this method fails: `ConnectionError` (an unterminated header, a
        # peer that goes away mid-request) and `TimeoutError`
        # (`REQUEST_TIMEOUT` elapsing) never reach `send()` or
        # `async_send`'s own close branch either,
        # and one of those two is otherwise the only place this id leaves
        # `manager.connections` (issue #437). Popped in the same
        # statement group as `self.client.close()` above rather than
        # scheduled apart from it the way the parse-error branch's own
        # reply is: there is no gap here for `RpcManager.stop`'s sweep to
        # land in the middle of, since both run synchronously, in this
        # thread, before this method's own frame returns.
        except Exception:  # noqa: BLE001
            self.client.close()
            self.manager.forget_connection(self.id)

    def _queue(self, head: RequestHead, body_bytes: bytes) -> None:
        """Answer what `HTTPReq_JSONRPC` refuses on a worker; queue the rest.

        `run` calls this holding `manager.queue_lock`, `manager.interrupted`
        unset. While `manager.in_warmup` holds, a request that passes every
        check is answered `RPC_IN_WARMUP` here and not queued: `Node`'s
        thread is opening the stores.
        """
        # Core's `HTTPReq_JSONRPC` answers a `GET`, `HEAD` or `PUT` 405
        # "JSONRPC server handles only POST requests", with that body, on
        # a worker: after the queue checks `run` makes, so a full queue
        # is a 503 for these methods too.
        if head.method != b"POST":
            self._refusal_reply = self._start_reply(
                self._send_refusal(
                    _METHOD_NOT_ALLOWED,
                    "JSONRPC server handles only POST requests",
                    (),
                    on_worker=True,
                )
            )
            return
        # Then no `Authorization` at all is a
        # 401 at once, one it does not accept a 401 after
        # `FAILED_ATTEMPT_DELAY`. Scheduled as a task of its own, as
        # the parse-error reply below is, for the reasons given there.
        if head.authorization is None:
            self._unauthorized_reply = self._start_reply(self._send_unauthorized(0))
            return
        user = self.manager.auth.authenticated_user(head.authorization)
        if user is None:
            self.manager.logger.warning(
                "ThreadRPCServer incorrect password attempt from %s",
                self._peer_address(),
            )
            self._unauthorized_reply = self._start_reply(
                self._send_unauthorized(FAILED_ATTEMPT_DELAY), FAILED_ATTEMPT_DELAY
            )
            return
        try:
            body = decode(body_bytes)
        except ValueError:
            # `HTTPReq_JSONRPC`'s `RPC_PARSE_ERROR`, thrown before
            # any request is read, so `JSONErrorReply` answers it
            # 500 in the legacy envelope with `"id":null`
            # (`src/httprpc.cpp`, at bitcoin/bitcoin@9be056a8a7, the
            # v31.1 tag). `ValueError` and
            # not `json.JSONDecodeError` alone, since malformed
            # bytes `json.loads` cannot even decode as text raise
            # the stdlib's own `UnicodeDecodeError`, a `ValueError`
            # too and the same "invalid JSON" from the client's side.
            # Scheduled as a task of its own, the same seam `send`
            # below reaches this same coroutine through, rather than
            # awaited in `run`'s own frame: this request's own
            # keep-alive is
            # honoured here exactly as it is for a dispatched reply,
            # matching Core's own `HTTPReq_JSONRPC`
            # (`src/httprpc.cpp:232-244`, at bitcoin/bitcoin@ca7162cde5),
            # which negotiates keep-alive on a parse error the same
            # way it does on any other reply -- so where it says to,
            # `async_send` calls back into `run` for a further
            # request, and doing that by awaiting it from `run` would
            # grow `run`'s own stack by one frame per malformed
            # body a kept-alive connection sent in a row, rather than
            # by a scheduled task's own fresh one each time, the way
            # `send` below already keeps a dispatched reply's own
            # recursion flat.
            #
            # `_start_reply`, not `send`'s own `_hand_reply`: this
            # method already runs on `self.loop`'s own thread, so
            # `run_coroutine_threadsafe` here would only requeue itself
            # onto the very loop it is already running on, through
            # `call_soon_threadsafe` -- a Task made real one further
            # turn later rather than this one, where `create_task`
            # makes it exist synchronously.
            #
            # Left in `manager.connections` rather than popped here,
            # so that `stop`'s own socket-closing sweep of that dict
            # can still reach this connection even on a turn where
            # the task above never gets to run at all; `async_send`
            # below pops it once it does, on the branch that closes
            # rather than keeps this connection. `rpc.main.handle_rpc`
            # carried a pop of its own once, on `Node`'s thread, that
            # this comment used to be contrasted against as the
            # thread-safe side of the same dict -- issue #688 is where
            # that pop turned out not to be safe after all, racing not
            # `stop` but `async_send`'s own re-entry into `run` for
            # this same connection's next request, and removed it.
            # Assigned to `self._parse_error_reply` (its own
            # docstring above has why) rather than left a bare
            # statement: unlike `_hand_reply` elsewhere in this
            # class, `_start_reply` returns an `Awaitable`, which
            # mypy's own `unused-awaitable` flags as a likely-missing
            # `await` when discarded outright.
            self._parse_error_reply = self._start_reply(
                self.async_send(
                    error_reply(RPCErrorCode.PARSE_ERROR, "Parse error"),
                    close=self._shutting_down(),
                )
            )
            return

        # Core's per-method check, which needs the decoded body and
        # so comes after the credential's
        whitelist_refusal = self.manager.auth.refusal(user, body)
        if whitelist_refusal is not None:
            if whitelist_refusal.warning:
                self.manager.logger.warning(*whitelist_refusal.warning)
            self._whitelist_reply = self._start_reply(
                self._send_whitelist_refusal(whitelist_refusal)
            )
            return
        if self.manager.in_warmup:
            self._warmup_reply = self._start_reply(
                self.async_send(
                    self.manager.answer_warmup(self, body),
                    close=self._shutting_down(),
                )
            )
            return
        self.manager.messages.append((body, self.id))

    def _refused_early(self, head: RequestHead) -> bool:
        """Schedule the reply to what Core refuses ahead of the credential.

        `manager.interrupted` is a 503: `InterruptHTTPServer`
        (`src/httpserver.cpp`, at bitcoin/bitcoin@f0de87f040, the v32.1
        tag) swaps `MaybeDispatchRequestToWorker` -- the dispatcher the
        checks below model, `_dispatch_refusal`'s own docstring naming
        which -- out for `RejectRequest`, which answers every request
        that reaches it `503` with no body, whatever its method. Then a
        method or a target `_dispatch_refusal` refuses is answered as it
        says. A source `-rpcallowip` does not name never reaches this:
        `RpcManager._accept_loop` closes it. Answers whether a reply was
        scheduled.
        """
        if self.manager.interrupted.is_set():
            self.manager.logger.log_debug(
                "http", "Rejecting request while shutting down"
            )
            self._refusal_reply = self._start_reply(
                self._send_refusal(_SERVICE_UNAVAILABLE)
            )
            return True
        status = _dispatch_refusal(head.method, head.target)
        if status is None:
            return False
        self._refusal_reply = self._start_reply(self._send_refusal(status))
        return True

    def _frame(
        self,
        status: str,
        body: bytes,
        fields: tuple[str, ...] = (),
        *,
        close: bool = False,
    ) -> bytes:
        """Frame `body` under `status` and `fields`, answering `self.head`.

        Core's `HTTPRequest::WriteReply`: the status line carries the
        request's own version, HTTP/1.1 where Core had not read it. A
        request of HTTP/1.1 on is kept open; one of HTTP/1.0 is kept
        open where its first `Connection` field is `keep-alive`, and the
        answer then says `Connection: keep-alive`. An answer on a
        connection kept open carries `Content-Length`, unless it is a
        204. A request whose first `Connection` field is `close` gets
        `Connection: close`, and is not kept open. Both are compared
        whole and ASCII case-insensitively. `RequestHead.field` decodes
        a value as Latin-1, where `lower` changes no character into an
        ASCII one.

        Sets `self.keep_alive`, `False` where `close` asks for it with no
        field written, and `self._request_closes` where the request
        itself is not kept open. The `Date` field Core adds from
        HTTP/1.1 on, and the `Content-Type` it adds where `fields` has
        none, are not written.
        """
        head = self.head
        if head is None:
            detail = "no request to answer"
            raise RuntimeError(detail)
        major, minor = head.version or (1, 1)
        connection = (head.connection or "").lower()
        keep_alive = minor >= 1 or connection == "keep-alive"
        lines = list(fields)
        if minor == 0 and keep_alive:
            lines.append("Connection: keep-alive")
        if keep_alive and status != NO_CONTENT:
            lines.append(f"Content-Length: {len(body)}")
        if connection == "close":
            lines.append("Connection: close")
            keep_alive = False
        self._request_closes = not keep_alive
        self.keep_alive = keep_alive and not close
        text = f"HTTP/{major}.{minor} {status}\r\n"
        text += "".join(f"{line}\r\n" for line in lines)
        return (text + "\r\n").encode() + body

    async def async_send(self, reply: HttpReply, *, close: bool = False) -> None:
        """Write `reply` back as one JSON-RPC HTTP reply.

        Wraps any `RawJSON` value in a fresh per-call mark before
        encoding, substitutes it back out unquoted once encoding is
        done, and frames the result as `_frame` does, which also
        decides what happens once the reply is on the wire: `client`
        closes, or this reads another request off the same socket
        (issue #640). A reply with no body is `rpc.jsonrpc.NO_CONTENT`,
        which `bitcoind` writes with no `Content-Type`. `close` closes
        `client` once the reply is written, whatever the request asked.
        """
        if reply.body is None:
            await self._write(
                self._frame(reply.status, b"", close=close), on_worker=True
            )
            return
        # A fresh token per call, not a fixed word: RawJSON's own
        # docstring has why -- a legitimate string value containing a
        # guessable mark once, unpaired, corrupts a fixed-word
        # substitution the way it cannot corrupt one this unlikely to
        # collide with.
        mark = secrets.token_hex(16)
        output_str = json.dumps(
            reply.body, separators=(",", ":"), cls=JSONEncoder, mark=mark
        )
        # RawJSON's own placeholder, quotes and all, unquoted to the
        # exact text it carries -- before Content-Length, which has to
        # count what is actually sent rather than what encoding
        # produced before this ran.
        output_str = re.sub(f'"{mark}(.*?){mark}"', r"\1", output_str)
        body = (output_str + "\n").encode()
        fields = ("Content-Type: application/json",)
        await self._write(
            self._frame(reply.status, body, fields, close=close), on_worker=True
        )

    def _send_framing_error(
        self, error: MalformedRequestHeadError | OversizedRequestBodyError
    ) -> None:
        """Schedule Core's answer to a request it cannot read.

        `HTTPRemoteClient::TryReadRequest`: 413 for a body past
        `MAX_BODY_BYTES` and 400 for the rest, with no body, and the
        connection closed after it.
        """
        self._refusal_reply = self._start_reply(
            self._send_refusal(_framing_status(error), close=True)
        )

    def _shutting_down(self) -> bool:
        """Whether `Node.terminate_flag` is set, after which a reply closes.

        Core writes no field for it: `StopHTTPServer` disconnects every
        client once its replies are sent (`DisconnectAllClients`,
        `src/httpserver.cpp`, at bitcoin/bitcoin@f0de87f040, the v32.1
        tag). Core keeps a connection open until then, and answers a
        request on it 503 (`_refused_early`); this closes it as each
        reply is written. Read as each reply is produced: after a 401's
        `FAILED_ATTEMPT_DELAY`, not before (btclib-org/btclib-node#1542).
        """
        return self.manager.node.terminate_flag.is_set()

    async def _send_whitelist_refusal(self, refusal: Refusal) -> None:
        """Answer `refusal`, a request `-rpcwhitelist` refuses.

        Measured against a real `bitcoind` v32.1.0: a 403 has no body,
        and a request Core refuses before its whitelist check is its
        JSON-RPC error object on one line, as `application/json`, framed
        as `_frame` frames it. The `Content-Type` Core adds to a 403 is
        not written, as `_send_unauthorized` does not write it.
        """
        fields: tuple[str, ...] = ()
        body = b""
        if refusal.body is not None:
            fields = ("Content-Type: application/json",)
            text = json.dumps(refusal.body, separators=(",", ":"), ensure_ascii=False)
            body = (text + "\n").encode()
        await self._write(
            self._frame(refusal.status, body, fields, close=self._shutting_down()),
            on_worker=True,
        )

    async def _send_unauthorized(self, delay: float) -> None:
        """Answer 401 with Core's `WWW-Authenticate`, `delay` seconds from now.

        Measured against a real `bitcoind` v32.1.0: `401 Unauthorized`,
        `WWW-Authenticate: Basic realm="jsonrpc"` and no body, after 0.25
        seconds for a wrong credential and at once for none, framed as
        `_frame` frames it. Core's reply also carries a `Content-Type`,
        which this does not write.
        """
        await asyncio.sleep(delay)
        fields = (f"WWW-Authenticate: {WWW_AUTHENTICATE}",)
        await self._write(
            self._frame("401 Unauthorized", b"", fields, close=self._shutting_down()),
            on_worker=True,
        )

    async def _send_refusal(
        self,
        status: str,
        body: str = "",
        fields: tuple[str, ...] = (_NO_STORE,),
        *,
        close: bool = False,
        on_worker: bool = False,
    ) -> None:
        """Answer `status` with `body` and `fields`, as `_frame` frames them.

        `fields` is `WriteNoStoreErrorReply`'s `_NO_STORE` unless given.
        `close` closes `client` after it, as it does once
        `_shutting_down`. `on_worker` is for an answer Core writes on a
        worker rather than on its socket thread, as `_write` says.
        """
        await self._write(
            self._frame(
                status,
                body.encode(),
                fields,
                close=close or self._shutting_down(),
            ),
            on_worker=on_worker,
        )

    async def _send_queue_refusal(self) -> None:
        """Answer a request that arrives with `RPC_WORK_QUEUE` already waiting.

        `MaybeDispatchRequestToWorker`'s own answer (`src/httpserver.cpp`,
        at bitcoin/bitcoin@f0de87f040, the v32.1 tag): a warning, then
        503 "Work queue depth exceeded", before the credential is read.
        `constants.RPC_THREADS` has what plays the queue here. The warning
        leaves out Core's pointer to `-rpcworkqueue`, which is not read.
        """
        self.manager.logger.warning(
            "Request rejected because http work queue depth exceeded"
        )
        await self._send_refusal(_SERVICE_UNAVAILABLE, "Work queue depth exceeded")

    async def _send_shutdown_refusal(self) -> None:
        """Answer a request `manager.interrupted` stopped from being queued.

        `MaybeDispatchRequestToWorker`'s own answer where `Submit`
        refuses a request once the pool is interrupted: a warning naming
        `SubmitError::Interrupted`, then 503 "Request rejected during
        server shutdown" (`src/httpserver.cpp`, at
        bitcoin/bitcoin@f0de87f040, the v32.1 tag). The connection
        closes after it, as `_shutting_down` says.
        """
        self.manager.logger.warning(
            "HTTP request rejected during server shutdown: '%s'", "Interrupted"
        )
        await self._send_refusal(
            _SERVICE_UNAVAILABLE, "Request rejected during server shutdown", close=True
        )

    async def _write(self, http_response: bytes, *, on_worker: bool = False) -> None:
        """Write one reply, then read the next request or close.

        `self.keep_alive` decides which, as `_frame` set it. Where an
        answer `on_worker` closes because its own request asks for it,
        `_answer_past_a_close` answers first what Core reads past it.
        """
        # A reply can run after its socket is gone: the client hung up,
        # or the socket was closed while this task was still queued.
        # Suppressed as `p2p.connection.Connection._send` suppresses it,
        # rather than left on a task nothing awaits, where asyncio
        # prints it as "Task exception was never retrieved". What
        # follows cleans up either way: the close branch below, or a
        # read in `run` that fails and closes (issue #1079).
        with contextlib.suppress(OSError):
            await self.loop.sock_sendall(self.client, http_response)
        # The reply is written, or its client gone: `RpcManager.stop`
        # waits for no more of this task, which on a kept-alive
        # connection goes on below to read the next request, idle until
        # one comes.
        task = asyncio.current_task()
        self.manager.reply_ended(None if task is None else task.get_coro())
        if self.keep_alive:
            # No re-insertion into `manager.connections` here: unlike an
            # earlier version of this method, nothing removed this id on
            # the way to this point. `create_connection` puts it in once,
            # at accept, and the only things that ever take it back out
            # are this method's own close branch below, `run`'s own
            # `except Exception` and `RpcManager.stop`'s shutdown sweep --
            # `rpc.main.handle_rpc`'s own docstring is where the earlier,
            # racy alternative (an eager pop there, compensated by a
            # re-insertion here) is argued against (issue #688).
            await self.run()
            return
        try:
            if on_worker and self._request_closes:
                await self._answer_past_a_close()
        finally:
            self.client.close()
            # The only place besides `run`'s own `except Exception` above
            # and `RpcManager.stop`'s shutdown sweep that removes this id
            # from `manager.connections`: the parse-error branch of `run`
            # above pops nothing itself, deliberately, so this is what
            # removes its entry once this connection is actually done
            # rather than only kept reachable for `RpcManager.stop`'s own
            # socket-closing sweep in the meantime (issue #640 review
            # round 2).
            self.manager.forget_connection(self.id)

    async def _answer_past_a_close(self) -> None:
        """Answer one buffered request as Core's socket thread answers it.

        A worker's reply that closes the connection sets
        `m_disconnect` once it is sent, but Core's socket thread still
        runs `HTTPRemoteClient::TryReadRequest` once on what it holds
        before it disconnects (`HTTPServer::SocketHandlerConnected` and
        `HTTPServer::DisconnectClients`, `src/httpserver.cpp`, at
        bitcoin/bitcoin@f0de87f040, the v32.1 tag). While the worker
        runs, that thread receives whenever its buffer is empty
        (`HTTPServer::GenerateWaitSockets`), so it holds what the client
        sent after the closing request too, and what arrives within its
        next wait, up to `SELECT_TIMEOUT` (50 ms). This reads `client`
        once, without waiting, where `self.buffer` is empty, which takes
        in what the client sent before the reply, and not what arrives
        after it (btclib-org/btclib-node#2153). A request that thread
        answers itself is then answered, as `_socket_thread_answer`
        says. One it hands to a worker is not: that worker races the
        disconnect and sometimes answers, and this takes the more common
        outcome, no answer (btclib-org/btclib-node#2101).
        """
        if not self.buffer:
            # Non-blocking, as every socket `loop` reads: nothing waiting
            # is `BlockingIOError`, a peer gone some other `OSError`
            with contextlib.suppress(OSError):
                self.buffer += self.client.recv(65536)
        answer = self._socket_thread_answer()
        if answer is not None:
            with contextlib.suppress(OSError):
                await self.loop.sock_sendall(self.client, answer)

    def _socket_thread_answer(self) -> bytes | None:
        """Frame Core's socket-thread answer to the request `self.buffer` holds.

        `None` where it holds no whole request, or one Core's dispatcher
        hands to a worker. The rest, in Core's order: a request Core
        cannot read is 400 or 413; `manager.interrupted` is 503
        (`RejectRequest`); a method or a target `_dispatch_refusal`
        refuses is answered as it says; a full queue is 503 "Work queue
        depth exceeded". Each carries `_NO_STORE`.
        """
        head = _HeadReader(self.buffer).read()
        if head is None:
            return None
        self.head = head
        error = head.error
        if error is None and head.chunked:
            try:
                if not _ChunkedReader(self.buffer, head).read():
                    return None
            except (MalformedRequestHeadError, OversizedRequestBodyError) as e:
                error = e
        elif error is None and len(self.buffer) < head.length:
            return None
        body = b""
        if error is not None:
            status = _framing_status(error)
        elif self.manager.interrupted.is_set():
            self.manager.logger.log_debug(
                "http", "Rejecting request while shutting down"
            )
            status = _SERVICE_UNAVAILABLE
        elif (refusal := _dispatch_refusal(head.method, head.target)) is not None:
            status = refusal
        else:
            with self.manager.queue_lock:
                if len(self.manager.messages) < RPC_WORK_QUEUE:
                    return None
            self.manager.logger.warning(
                "Request rejected because http work queue depth exceeded"
            )
            status = _SERVICE_UNAVAILABLE
            body = b"Work queue depth exceeded"
        return self._frame(status, body, (_NO_STORE,))

    def _start_reply(
        self, reply: Coroutine[Any, Any, None], delay: float = 0.0
    ) -> asyncio.Task[None]:
        """Schedule `reply` on `loop`, from `loop`'s own thread.

        Recorded through `RpcManager.track_reply`, its write due `delay`
        seconds from now, so that `RpcManager.stop` finishes it rather
        than cancels it (btclib-org/btclib-node#1539), and forgotten once
        its task ends, however it ends.
        """
        self.manager.track_reply(reply, time.monotonic() + delay)
        task = self.loop.create_task(reply)
        task.add_done_callback(lambda _: self.manager.reply_ended(reply))
        return task

    def _hand_reply(
        self, reply: Coroutine[Any, Any, None], due: float | None = None
    ) -> Future[None]:
        """Hand `reply` to `loop`, from `handle_rpc`'s own thread.

        `_start_reply`, across threads: recorded before
        `run_coroutine_threadsafe` hands it over, its write due by the
        `time.monotonic()` value `due`, now where it is not given.
        """
        self.manager.track_reply(reply, time.monotonic() if due is None else due)
        future = asyncio.run_coroutine_threadsafe(reply, self.loop)
        future.add_done_callback(lambda _: self.manager.reply_ended(reply))
        return future

    def send(self, reply: HttpReply) -> None:
        """Schedule `async_send` on `loop`, from `handle_rpc`'s own thread.

        Once `Node.terminate_flag` is set, the connection closes after
        the reply, as `_shutting_down` says.
        """
        self._hand_reply(self.async_send(reply, close=self._shutting_down()))

    # Use with care
    def send_and_wait(self, reply: HttpReply) -> None:
        """Like `send`, but block up to 2 seconds for the write to finish.

        `rpc.main._step` is the only caller, for a `stop` request: the client
        has to see its own reply before `node.stop()` starts tearing
        `loop` down under it. Closes once the reply is written, whatever
        the request asked for -- `RpcManager.stop`, called right
        after this returns, tears the whole loop down, so there is no
        next request this connection could still answer.
        """
        future = self._hand_reply(self.async_send(reply, close=True))
        with contextlib.suppress(TimeoutError):
            future.result(timeout=2)

    def send_and_close_after(self, reply: HttpReply, delay: float) -> None:
        """Write `reply` `delay` seconds from now, on this connection's loop.

        `rpc.main._step` is the only caller, for a `stop` carrying
        a positive `wait` (btclib-org/btclib-node#1467), and returns at
        once to set `Node.terminate_flag`, as Core's own `stop` requests
        shutdown before it sleeps. The wait runs on `loop`, and
        `RpcManager.stop` finishes it rather than cancelling it, so the
        reply still reaches the client once that shutdown is under way;
        `close=True` for the reason `send_and_wait` above gives. The
        moment the wait ends is recorded through
        `RpcManager.extend_reply_deadline`, for `Node.stop` to wait for.
        """
        due = time.monotonic() + delay
        self.manager.extend_reply_deadline(due)
        self._hand_reply(self._delayed_send(reply, delay), due)

    async def _delayed_send(self, reply: HttpReply, delay: float) -> None:
        """Sleep `delay` seconds, then write `reply` and close."""
        await asyncio.sleep(delay)
        await self.async_send(reply, close=True)

    def _peer_address(self) -> str:
        """Return the client's `ip:port`, for a log line naming who asked."""
        try:
            host, port = self.client.getpeername()[:2]
        # `OSError` for a socket no longer connected, `ValueError` for a
        # peer name that is not a `(host, port)` pair
        except OSError, ValueError:
            return "an unknown address"
        return ip_and_port(host, port)

    @override
    def __repr__(self) -> str:
        try:
            peer = self.client.getpeername()
            out = f"Connection to {ip_and_port(peer[0], peer[1])}"
        except OSError:
            out = "Broken connection"
        return out
