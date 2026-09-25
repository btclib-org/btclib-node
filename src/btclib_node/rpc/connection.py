# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`RpcConnection`, one accepted HTTP socket carrying one or more requests.

Parses the header section off the wire, bounded by `MAX_HEADER_BYTES`
and `MAX_BODY_BYTES` since both are read before any credential is
checked, answers a request line, a request-target or a `Content-Length`
Core's listener cannot frame with 400 or 413 (`parse_request_head`),
refuses a method
or a path Core's listener refuses before Core checks a credential
(`_refusal`), answers a request whose `Authorization` header
`rpc.auth.RpcAuth` does not accept with 401, and decodes the body of
one it does accept, which `rpc.manager.RpcManager.messages` queues for
`rpc.main.handle_rpc`. `RawJSON` is a JSON number written
back out exactly as given, the way Core's own `UniValue` writes one
built from a string rather than from a `float`.

Each answer is framed as libevent frames it (`_frame`), in the version
of the request it answers, and the socket is kept open across answers
where libevent keeps it, the next request read off the same connection
rather than requiring a fresh accept per call (issue #640).

The framing is the libevent `bitcoind` v31.1.0 links, not Core's own
source: bitcoin/bitcoin@9be056a8a7, the v31.1 tag, hands the socket to
`evhttp`, and `depends/packages/libevent.mk` there pins
2.1.12-stable, read at libevent/libevent@5df3037 (`http.c`).
"""

import asyncio
import contextlib
import ipaddress
import json
import re
import secrets
from dataclasses import dataclass
from http.client import HTTPException, HTTPMessage, parse_headers
from io import BytesIO
from typing import TYPE_CHECKING, Any, override

from bitcoin_core_rpc import RPCErrorCode

from btclib_node.exceptions import (
    IncompleteRequestHeadError,
    MalformedRequestHeadError,
    OversizedRequestBodyError,
)
from btclib_node.p2p.address import ip_and_port
from btclib_node.rpc.auth import FAILED_ATTEMPT_DELAY, WWW_AUTHENTICATE, Refusal
from btclib_node.rpc.jsonrpc import NO_CONTENT, HttpReply, decode, error_reply

if TYPE_CHECKING:
    import socket
    from collections.abc import Callable

    from btclib_node.rpc.manager import RpcManager

__all__ = [
    "MAX_BODY_BYTES",
    "MAX_HEADER_BYTES",
    "REQUEST_TIMEOUT",
    "JSONEncoder",
    "RawJSON",
    "RequestHead",
    "RpcConnection",
    "parse_request_head",
]

# `evbuffer_readln`'s `EVBUFFER_EOL_CRLF`, which libevent reads every line
# of a header section with: a line ends at a line feed, with or without
# a carriage return before it, and the section ends at an empty line
_EOL = re.compile(rb"\r?\n")
_HEAD_END = re.compile(rb"\r?\n\r?\n")
# Bounds on the read below, which is fed by whoever connects: the header
# section and the body are read whole before `RpcConnection.run` checks
# a credential, as Core's HTTP server reads a request before
# `HTTPReq_JSONRPC` sees it, so an unterminated header section or an
# overstated Content-Length must not grow the buffer without limit.
# Both are generous next to a real JSON-RPC request -- headers run to a
# few hundred bytes, and the largest body this node is sent is a raw
# transaction.
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


def _error_page(status: str) -> str:
    """Return the page libevent's `evhttp_send_error` writes for `status`."""
    reason = status.partition(" ")[2]
    return (
        f"<HTML><HEAD>\n<TITLE>{status}</TITLE>\n"
        f"</HEAD><BODY>\n<H1>{reason}</H1>\n</BODY></HTML>\n"
    )


_BAD_REQUEST = "400 Bad Request"
_ENTITY_TOO_LARGE = "413 Request Entity Too Large"
_NOT_IMPLEMENTED = "501 Not Implemented"
# The methods Core's listener lets through to `http_request_cb`: every
# other one is answered 501 before that callback runs. Core's source has
# no 501 and never sets evhttp's allowed methods, so the refusal is the
# libevent Core links, measured against a real `bitcoind` v31.1.0.
_LIBEVENT_METHODS = frozenset((b"GET", b"POST", b"HEAD", b"PUT", b"DELETE"))
# `evhttp_method_may_have_body`: libevent reads no `Content-Length` for
# any other method, so no body is read for it and none refused
_BODY_METHODS = frozenset(
    (b"GET", b"POST", b"PUT", b"DELETE", b"PATCH", b"OPTIONS", b"CONNECT")
)
# `evhttp_parse_request_line`'s shortest line, trailing spaces removed
_MIN_REQUEST_LINE = len(b"GET / HTTP/1.0")
# `evhttp_parse_http_version`'s `sscanf(version, "HTTP/%d.%d%c", ...)`
# with exactly two conversions: `%d` skips leading C white space and
# takes a sign, and nothing may follow the minor number
_C_SPACE = rb"[ \t\n\v\f\r]*"
_HTTP_VERSION = re.compile(
    rb"HTTP/%b([+-]?[0-9]+)\.%b([+-]?[0-9]+)" % (_C_SPACE, _C_SPACE)
)
# where `%d` stops being defined: C leaves a number past `int` undefined,
# and `bitcoind` v31.1.0 on macOS answers `HTTP/1.99999999999` as
# `HTTP/1.-1` and `HTTP/1.2147483648` as `HTTP/1.0`, which no rule here
# models, so both are refused
_C_INT = range(-(2**31), 2**31)
# `evhttp_get_body_length`'s `evutil_strtoll(value, &endp, 10)`, which
# must consume the whole value: leading C white space and a sign, then
# decimal digits. One quantifier over the digits: a `0*` ahead of
# `[0-9]+` matches the same zeros, and a failing tail after a long run
# of them backtracks in quadratic time, before any credential is read
_CONTENT_LENGTH = re.compile(_C_SPACE.decode() + "([+-]?)([0-9]+)")
# The URI grammar of libevent's `evhttp_uri_parse_with_flags`:
# `scheme_ok`, then `CHAR_IS_UNRESERVED`, `SUBDELIMS` and a `%` escape
# of two hex digits, which `userinfo_ok` and `regname_ok` build on;
# `end_of_authority`; and `bracket_addr_ok`'s `IPvFuture`
_SCHEME = re.compile(rb"[A-Za-z][A-Za-z0-9+.-]*")
_REG_CHAR = rb"[A-Za-z0-9._~!$&'()*+,;=-]"
_REG_NAME = re.compile(rb"(?:%b|%%[0-9A-Fa-f]{2})*" % _REG_CHAR)
_USERINFO = re.compile(rb"(?:%b|:|%%[0-9A-Fa-f]{2})*" % _REG_CHAR)
# one quantifier over the digits, as `_CONTENT_LENGTH` has
_PORT = re.compile(rb":([0-9]*)\Z")
_MAX_PORT = 65535
_AUTHORITY_END = re.compile(rb"[/?#]")
_IP_FUTURE = re.compile(rb"\[v[0-9A-Fa-f]+\.(?:%b|:)*\]" % _REG_CHAR)
_PATH_END = re.compile(rb"[?#]")


def _split_request_line(line: bytes) -> tuple[bytes, bytes, bytes]:
    """Split `line` into its method, target and version as libevent does.

    `evhttp_parse_request_line`: trailing spaces are dropped, the method
    ends at the first space and the version starts after the last one,
    so the target is everything between and may hold spaces of its own.
    Raises `MalformedRequestHeadError` where libevent refuses the line
    before it knows the method. A NUL in it is an ordinary byte here,
    where libevent's C string ends at it.
    """
    line = line.rstrip(b" ")
    if len(line) < _MIN_REQUEST_LINE:
        detail = "request line too short"
        raise MalformedRequestHeadError(detail)
    method, space, rest = line.partition(b" ")
    target, space, version = rest.rpartition(b" ")
    if not space or not target:
        detail = "request line not method, target and version"
        raise MalformedRequestHeadError(detail)
    return method, target, version


def _http_version(token: bytes) -> tuple[int, int]:
    """Return the version `evhttp_parse_http_version` reads off `token`.

    Raises `MalformedRequestHeadError` where it refuses the token, and
    for a number past a C `int`, whose conversion C leaves undefined.
    """
    match = _HTTP_VERSION.fullmatch(token)
    # measured as text first: `int` refuses a string past 4300 digits
    digits = len(str(_C_INT.stop))
    if match is None or any(len(n.lstrip(b"+-0")) > digits for n in match.groups()):
        detail = "unsupported HTTP version"
        raise MalformedRequestHeadError(detail)
    major, minor = int(match[1]), int(match[2])
    if major > 1 or major not in _C_INT or minor not in _C_INT:
        detail = "unsupported HTTP version"
        raise MalformedRequestHeadError(detail)
    return major, minor


def _authority_ok(authority: bytes) -> bool:
    """Return whether libevent's `parse_authority` accepts `authority`.

    An optional `userinfo@`, then an optional `:port` of digits up to
    65535, and a host that is a bracketed IPv6 or IPvFuture address or
    a registered name, an IPv4 address among them.
    """
    userinfo, at, host = authority.partition(b"@")
    if not at:
        host = authority
    elif _USERINFO.fullmatch(userinfo) is None:
        return False
    port = _PORT.search(host)
    if port is not None:
        # leading zeros stripped, so a port is refused by its length
        # before `int` reads it
        digits = port[1].lstrip(b"0")
        if len(digits) > len(str(_MAX_PORT)) or int(digits or b"0") > _MAX_PORT:
            return False
        host = host[: port.start()]
    if host.startswith(b"[") and host.endswith(b"]"):
        return _bracketed_ok(host)
    return _REG_NAME.fullmatch(host) is not None


def _bracketed_ok(host: bytes) -> bool:
    """Return whether `bracket_addr_ok` accepts `host`, brackets included.

    An IPvFuture address, or an IPv6 address as the platform's
    `inet_pton` reads one, where libevent hands it. This reads one as
    `ipaddress` does, with a `%` scope refused as `bitcoind` v31.1.0
    refuses it; `bitcoind` on macOS also takes `::ffff:1.2.3.04`, which
    `ipaddress` refuses.
    """
    if host[1:2] == b"v":
        return _IP_FUTURE.fullmatch(host) is not None
    address = host[1:-1]
    try:
        ipaddress.IPv6Address(address.decode("ascii"))
    except ValueError:
        return False
    return b"%" not in address


def _is_proxy_request(method: bytes, target: bytes) -> bool:
    """Parse `target` as libevent does, and say whether it is proxied.

    `evhttp_parse_request_line` parses a `CONNECT` target with
    `evhttp_uri_parse_authority`, which reads the authority up to the
    first `/`, `?` or `#` and nothing after it, and any other with
    `evhttp_uri_parse_with_flags(uri, EVHTTP_URI_NONCONFORMANT)`: an
    optional scheme, an optional `//` and authority, then a path up to
    the first `?` or `#`, whose first segment may hold no `:` where
    there is no scheme; what follows the path is never refused. The two
    checks libevent's own comment calls "maybe-unreachable" are
    unreachable here too. Raises `MalformedRequestHeadError` where the
    parse fails.

    A proxy request is an `http` or `https` target with an authority
    naming no host libevent serves, and Core names none to it
    (`evhttp_add_server_alias` and `evhttp_add_virtual_host` are called
    nowhere in `src/`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), so
    every such target is one.
    """
    detail = f"request-target {target!r}"
    if method == b"CONNECT":
        end = _AUTHORITY_END.search(target)
        if not _authority_ok(target[: end.start() if end else len(target)]):
            raise MalformedRequestHeadError(detail)
        return False
    scheme = None
    rest = target
    colon = target.find(b":")
    if colon != -1 and _SCHEME.fullmatch(target, 0, colon):
        scheme, rest = target[:colon], target[colon + 1 :]
    has_authority = rest.startswith(b"//")
    if has_authority:
        end = _AUTHORITY_END.search(rest, 2)
        stop = end.start() if end else len(rest)
        if not _authority_ok(rest[2:stop]):
            raise MalformedRequestHeadError(detail)
        rest = rest[stop:]
    # `path_matches_noscheme`
    path = _PATH_END.split(rest, maxsplit=1)[0]
    if scheme is None and b":" in path.partition(b"/")[0]:
        raise MalformedRequestHeadError(detail)
    return (
        has_authority and scheme is not None and scheme.lower() in {b"http", b"https"}
    )


def _content_length(value: str | None) -> int:
    """Return the body length `value` declares, as libevent reads it.

    Raises `MalformedRequestHeadError` where `evhttp_get_body_length`
    refuses it and `OversizedRequestBodyError` past `MAX_BODY_BYTES`,
    the `MAX_SIZE` Core passes to `evhttp_set_max_body_size`; a value
    past `strtoll`'s range, which clamps it, is refused the same way.
    """
    if value is None:
        return 0
    # the spaces and tabs `evutil_rtrim_lws_` trims off a header value
    value = value.rstrip(" \t")
    match = _CONTENT_LENGTH.fullmatch(value)
    if match is None:
        detail = f"Content-Length {value!r}"
        raise MalformedRequestHeadError(detail)
    sign, digits = match.groups()
    digits = digits.lstrip("0") or "0"
    # `int` refuses a string past 4300 digits, and a length that long is
    # past the cap anyway
    too_long = len(digits) > len(str(MAX_BODY_BYTES))
    if sign == "-" and (too_long or int(digits) > 0):
        detail = f"Content-Length {value!r}"
        raise MalformedRequestHeadError(detail)
    if too_long or int(digits) > MAX_BODY_BYTES:
        raise OversizedRequestBodyError(value)
    return int(digits)


def _refusal(method: bytes, target: bytes) -> tuple[str, str] | None:
    """Return the status and body Core refuses `method` at `target` with.

    Core answers these before `HTTPReq_JSONRPC` reads `Authorization`,
    so a caller without a credential gets them too (`http_request_cb`,
    `src/httpserver.cpp`, and `HTTPReq_JSONRPC` and `StartHTTPRPC`,
    `src/httprpc.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag):

    - a method outside `_LIBEVENT_METHODS` is libevent's 501;
    - `DELETE`, which `HTTPRequest::GetRequestMethod` maps to `UNKNOWN`,
      is 405 with no body, on any path;
    - a target other than exactly `/` or one starting `/wallet/` matches
      no handler `StartHTTPRPC` registers, and is 404 with no body;
    - `GET`, `HEAD` and `PUT` there are 405, "JSONRPC server handles only
      POST requests".

    `/wallet/` is registered where `HasWalletSupport()` holds, as it does
    in the `bitcoind` v31.1.0 release binary, which answers there. The
    only other handlers are `StartREST`'s, registered only under `-rest`,
    off by default (`DEFAULT_REST_ENABLE`, `src/init.cpp`); this node has
    no REST interface, so `/rest/` is 404 here as in Core at its default.
    The target is compared as the raw request-target, the way Core
    compares `GetURI()`: a query string or an absolute URI matches
    neither handler.

    A `HEAD` reply here is framed like a `GET` one, by `Content-Length`;
    `bitcoind` drops that header from a `HEAD` reply and still writes
    the body, on a connection it keeps open, so nothing framed would
    tell a client where that body ends.
    """
    if method not in _LIBEVENT_METHODS:
        return _NOT_IMPLEMENTED, _error_page(_NOT_IMPLEMENTED)
    if method == b"DELETE":
        return "405 Method Not Allowed", ""
    if target != b"/" and not target.startswith(b"/wallet/"):
        return "404 Not Found", ""
    if method != b"POST":
        return "405 Method Not Allowed", "JSONRPC server handles only POST requests"
    return None


@dataclass(frozen=True)
class RequestHead:
    r"""One request's own header section, and what its answer is framed by.

    `request_line`, `separator`, `fields` and `terminator` are the raw
    `bytes` `parse_request_head` split, not reduced to `HTTPMessage`'s
    own parsed object, so `serialize` reproduces the exact octets
    consumed -- the same round-trip `tests/fuzz_corpus_test.py` already
    holds `p2p.connection.frame_message_bytes` to. `separator` is the
    line ending after `request_line` where a field follows it, and
    `terminator` the line ending and empty line that close the section.
    `consumed` is `len(serialize())`, computed once, which is what `run`
    trims its buffer by.

    `method` and `target` are the request line's first part and its
    middle, `b""` where the line did not split, and `version` is `None`
    where libevent had not read it. `proxy` is libevent's
    `EVHTTP_PROXY_REQUEST` (`_is_proxy_request`). `connection`,
    `proxy_connection` and `authorization` are the first such field's
    value, `None` where there is none, and `length` the body's.
    `error` is what libevent refuses the section for, answered with its
    error page; `method` is then what it knows of the request, which
    decides whether a `CONNECT` keeps its connection. Where libevent
    refuses a `CONNECT`'s request line the section is that line and its
    own ending alone, in `separator`, libevent reading the next request
    from the line after it.
    """

    request_line: bytes
    separator: bytes
    fields: bytes
    terminator: bytes
    method: bytes
    target: bytes
    version: tuple[int, int] | None
    proxy: bool
    connection: str | None
    proxy_connection: str | None
    authorization: str | None
    length: int
    consumed: int
    error: MalformedRequestHeadError | OversizedRequestBodyError | None

    def serialize(self) -> bytes:
        """Reproduce the exact octets `parse_request_head` consumed."""
        return self.request_line + self.separator + self.fields + self.terminator


def _read_head(data: bytes) -> RequestHead:
    """Read one request's header section off the front of `data`.

    What `parse_request_head` parses, with a section libevent refuses
    returned rather than raised, `error` saying why: `RpcConnection.run`
    answers it from what was read of it. Raises
    `IncompleteRequestHeadError` where `data` holds no empty line yet.
    """
    end = _HEAD_END.search(data)
    if end is None:
        raise IncompleteRequestHeadError
    head, terminator = data[: end.start()], end.group()
    eol = _EOL.search(head)
    request_line = head[: eol.start()] if eol else head
    separator = eol.group() if eol else b""
    fields = head[eol.end() :] if eol else b""
    method = target = b""
    version = None
    try:
        method, target, token = _split_request_line(request_line)
        version = _http_version(token)
        proxy = _is_proxy_request(method, target)
    except MalformedRequestHeadError as e:
        if method == b"CONNECT":
            # kept open, so the next request starts on the next line
            separator = separator or terminator[: terminator.index(b"\n") + 1]
            fields = terminator = b""
        return RequestHead(
            request_line=request_line,
            separator=separator,
            fields=fields,
            terminator=terminator,
            method=method,
            target=target,
            version=version,
            proxy=False,
            connection=None,
            proxy_connection=None,
            authorization=None,
            length=0,
            consumed=len(request_line + separator + fields + terminator),
            error=e,
        )
    error: MalformedRequestHeadError | OversizedRequestBodyError | None = None
    length = 0
    try:
        headers = parse_headers(BytesIO(fields + b"\r\n\r\n"))
    except HTTPException as e:
        headers = HTTPMessage()
        error = MalformedRequestHeadError(str(e))
    else:
        if method in _BODY_METHODS:
            try:
                length = _content_length(headers.get("Content-Length"))
            except (MalformedRequestHeadError, OversizedRequestBodyError) as e:
                error = e
    return RequestHead(
        request_line=request_line,
        separator=separator,
        fields=fields,
        terminator=terminator,
        method=method,
        target=target,
        version=version,
        proxy=proxy,
        connection=_field(headers, "Connection"),
        proxy_connection=_field(headers, "Proxy-Connection"),
        authorization=headers.get("Authorization"),
        length=length,
        consumed=len(head) + len(terminator),
        error=error,
    )


def _field(headers: HTTPMessage, name: str) -> str | None:
    """Return the first `name` field's value, trailing blanks trimmed.

    The spaces and tabs `evutil_rtrim_lws_` trims. `http.client` has
    already dropped leading tabs libevent keeps, so `Connection:<TAB>close`
    closes here and not in `bitcoind` (issue #1126).
    """
    value = headers.get(name)
    return None if value is None else value.rstrip(" \t")


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

    Every line ends at a line feed, a carriage return before it dropped,
    and the section at the first empty line, as libevent reads them.
    Raises `IncompleteRequestHeadError` where `data` holds no empty line
    yet -- `run`'s own call site never hits this, since it only reads a
    section once `_recv_until` has seen one, but a fuzzed byte string
    has no such guarantee. Raises `MalformedRequestHeadError`, answered
    400, for a request line or a request-target libevent refuses, a
    `Content-Length` `_content_length` refuses, and a header section
    `http.client` itself refuses, whose limits are not libevent's
    (issue #1126). Raises `OversizedRequestBodyError` for a
    `Content-Length` past `MAX_BODY_BYTES`, which libevent answers 413.
    """
    head = _read_head(data)
    if head.error is not None:
        raise head.error
    return head


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
        self.request_timeout = request_timeout
        # The request being answered, set by `run` as each one is read
        # and framing its answer (`_frame`)
        self.head: RequestHead | None = None
        # Set by `_frame` as each answer is framed, `False` until then:
        # `_write` reads it once the answer is on the wire, to decide
        # whether to close the socket or read another request off it
        # (issue #640).
        self.keep_alive = False
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
        # A `_refusal`'s own reply, kept for the same reason
        self._refusal_reply: asyncio.Task[None] | None = None

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

        Reads the header section up to its empty line, then the
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

        In the order Core answers them, and none of them queued: a
        header section `parse_request_head` refuses is answered 400 or
        413 by `_send_framing_error`; a
        method or a target `_refusal` refuses is answered by
        `_send_refusal`, whatever the credential; a request whose
        `Authorization` header `manager.auth` does not accept is
        answered 401 by `_send_unauthorized`, its body read off the
        socket and never decoded; and one whose decoded body
        `manager.auth.refusal` refuses is answered by
        `_send_whitelist_refusal`.

        Called again, by `async_send` below, for every request after the
        first one a kept-alive connection carries -- `self.buffer` is
        trimmed to what is left after this request's own body before
        that request is queued, so a second call starts clean rather
        than re-reading bytes this one already consumed.
        """
        try:
            async with asyncio.timeout(self.request_timeout):
                await self._recv_until(
                    lambda: _HEAD_END.search(self.buffer) is not None,
                    MAX_HEADER_BYTES,
                )
                # `_read_head` is what `fuzz/fuzz_rpc_head.py` also
                # drives, through `parse_request_head`, directly over
                # octets -- the latter's own docstring is where the
                # framing/JSON-body scoping boundary is argued.
                # `bytes(self.buffer)` rather than the `bytearray`
                # itself: the function's own contract is over immutable
                # octets, matching `p2p.connection.frame_message`'s.
                head = self.head = _read_head(bytes(self.buffer))
                self.buffer = self.buffer[head.consumed :]
                if head.error is not None:
                    self._send_framing_error(head.error)
                    return
                length = head.length
                await self._recv_until(lambda: len(self.buffer) >= length)

            body_bytes = self.buffer[:length]
            # Whatever is left belongs to a request after this one --
            # pipelined ahead of its own reply, or simply not sent yet --
            # and must not be replayed as part of this request's own
            # body on a second call to this method.
            self.buffer = self.buffer[length:]
            # Ahead of the credential check below, as in Core, and once
            # the body is read, so that a connection `_frame` keeps goes
            # on to its next request after the refusal.
            refusal = _refusal(head.method, head.target)
            if refusal is not None:
                status, refusal_body = refusal
                self._refusal_reply = self.loop.create_task(
                    self._send_refusal(
                        status, refusal_body, page=status == _NOT_IMPLEMENTED
                    )
                )
                return
            # Core's `HTTPReq_JSONRPC`: no `Authorization` at all is a
            # 401 at once, one it does not accept a 401 after
            # `FAILED_ATTEMPT_DELAY`. Scheduled as a task of its own, as
            # the parse-error reply below is, for the reasons given there.
            if head.authorization is None:
                self._unauthorized_reply = self.loop.create_task(
                    self._send_unauthorized(0)
                )
                return
            user = self.manager.auth.authenticated_user(head.authorization)
            if user is None:
                self.manager.logger.warning(
                    "ThreadRPCServer incorrect password attempt from %s",
                    self._peer_address(),
                )
                self._unauthorized_reply = self.loop.create_task(
                    self._send_unauthorized(FAILED_ATTEMPT_DELAY)
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
                # awaited in this very frame: this request's own
                # keep-alive is
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
                # statement: unlike `run_coroutine_threadsafe` elsewhere
                # in this class, `create_task` returns an `Awaitable`,
                # which mypy's own `unused-awaitable` flags as a
                # likely-missing `await` when discarded outright.
                self._parse_error_reply = self.loop.create_task(
                    self.async_send(
                        error_reply(RPCErrorCode.PARSE_ERROR, "Parse error")
                    )
                )
                return

            # Core's per-method check, which needs the decoded body and
            # so comes after the credential's
            whitelist_refusal = self.manager.auth.refusal(user, body)
            if whitelist_refusal is not None:
                if whitelist_refusal.warning:
                    self.manager.logger.warning(*whitelist_refusal.warning)
                self._whitelist_reply = self.loop.create_task(
                    self._send_whitelist_refusal(whitelist_refusal)
                )
                return
            self.manager.messages.append((body, self.id))
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
        # `self.manager.connections.pop` below covers every other way
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
            self.manager.connections.pop(self.id, None)

    def _frame(
        self,
        status: str,
        body: bytes,
        fields: tuple[str, ...] = (),
        *,
        page: bool = False,
    ) -> bytes:
        """Frame `body` under `status` and `fields`, answering `self.head`.

        libevent's `evhttp_make_header_response` and `evhttp_send_done`:
        the status line carries the request's own version. HTTP/1.1 on,
        and HTTP/1.0 asking `keep-alive`, get `Content-Length`, the
        latter with `Connection: keep-alive`; any other version gets
        neither. A request asking to close gets `Connection: close`, and
        a proxy request (`RequestHead.proxy`) no `Connection` at all.
        `page` is `evhttp_send_error`'s page, which replaces `fields`
        with `Connection: close` and answers a version with a zero in it
        as HTTP/1.1.

        Sets `self.keep_alive`: a connection closes after a request
        before HTTP/1.1 that did not ask `keep-alive`, and after a
        proxy request or an answer carrying `Connection: close`, unless
        the request is a `CONNECT`. A `CONNECT`'s answer and a 204 carry
        no `Content-Length`, as `evhttp_response_needs_body`; a
        `HEAD`'s does, `_refusal` saying why. The `Date` header libevent
        adds, and the `Content-Type` it adds where `fields` has none,
        are not written.
        """
        head = self.head
        if head is None:
            detail = "no request to answer"
            raise RuntimeError(detail)
        major, minor = head.version or (0, 0)
        if page:
            fields = ("Connection: close",)
            if not major or not minor:
                major, minor = 1, 1
        # `evhttp_is_connection_keepalive` compares a prefix, and
        # `evhttp_is_connection_close` the whole value, both ASCII
        # case-insensitively; `http.client` decodes a field as Latin-1,
        # where `lower` changes no character into an ASCII one
        connection = (head.connection or "").lower()
        asks_keep_alive = connection.startswith("keep-alive")
        asks_close = connection == "close"
        if head.proxy:
            asks_close = (head.proxy_connection or "").lower() != "keep-alive"
        lines = list(fields)
        if major == 1:
            if minor == 0 and asks_keep_alive:
                lines.append("Connection: keep-alive")
            if (
                (minor >= 1 or asks_keep_alive)
                and status != NO_CONTENT
                and head.method != b"CONNECT"
            ):
                lines.append(f"Content-Length: {len(body)}")
        if asks_close:
            lines = [line for line in lines if not line.startswith("Connection:")]
            if not head.proxy:
                lines.append("Connection: close")
        old = (major, minor) < (1, 1) and not asks_keep_alive
        closing = head.proxy or "Connection: close" in lines
        self.keep_alive = not old and (head.method == b"CONNECT" or not closing)
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
        which `bitcoind` writes with no `Content-Type`. `close` is the
        `Connection: close` Core's `HTTPRequest::WriteReply` adds once
        shutdown has begun.
        """
        fields = ("Connection: close",) if close else ()
        if reply.body is None:
            await self._write(self._frame(reply.status, b"", fields))
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
        fields = ("Content-Type: application/json", *fields)
        await self._write(self._frame(reply.status, body, fields))

    def _send_framing_error(
        self, error: MalformedRequestHeadError | OversizedRequestBodyError
    ) -> None:
        """Schedule libevent's answer to a header section it cannot frame.

        `evhttp_connection_incoming_fail`: 413 for a body past the limit
        and 400 for the rest, as `evhttp_send_error`'s own page, which
        `_frame` closes after unless the request is a `CONNECT`.
        libevent answers a request line as soon as it is read, where
        this waits for the whole header section.
        """
        status = _BAD_REQUEST
        if isinstance(error, OversizedRequestBodyError):
            status = _ENTITY_TOO_LARGE
        self._refusal_reply = self.loop.create_task(
            self._send_refusal(status, _error_page(status), page=True)
        )

    async def _send_whitelist_refusal(self, refusal: Refusal) -> None:
        """Answer `refusal`, a request `-rpcwhitelist` refuses.

        Measured against a real `bitcoind` v31.1.0: a 403 has no body,
        and a request Core refuses before its whitelist check is its
        JSON-RPC error object on one line, as `application/json`, framed
        as `_frame` frames it. The `Content-Type` libevent adds to a 403
        is not written, as `_send_unauthorized` does not write it.
        """
        fields: tuple[str, ...] = ()
        body = b""
        if refusal.body is not None:
            fields = ("Content-Type: application/json",)
            text = json.dumps(refusal.body, separators=(",", ":"), ensure_ascii=False)
            body = (text + "\n").encode()
        await self._write(self._frame(refusal.status, body, fields))

    async def _send_unauthorized(self, delay: float) -> None:
        """Answer 401 with Core's `WWW-Authenticate`, `delay` seconds from now.

        Measured against a real `bitcoind` v31.1.0: `401 Unauthorized`,
        `WWW-Authenticate: Basic realm="jsonrpc"` and no body, after 0.25
        seconds for a wrong credential and at once for none, framed as
        `_frame` frames it. Core's reply also carries the `Content-Type`
        header libevent adds, which this does not write.
        """
        await asyncio.sleep(delay)
        fields = (f"WWW-Authenticate: {WWW_AUTHENTICATE}",)
        await self._write(self._frame("401 Unauthorized", b"", fields))

    async def _send_refusal(
        self, status: str, body: str, *, page: bool = False
    ) -> None:
        """Answer `status` with `body`, one of `_refusal`'s own replies.

        The status line and the body are `bitcoind` v31.1.0's, framed as
        `_frame` frames them, `page` saying the body is libevent's own
        error page; the `Content-Type` header libevent adds is not
        written, as `_send_unauthorized` does not write it.
        """
        await self._write(self._frame(status, body.encode(), page=page))

    async def _write(self, http_response: bytes) -> None:
        """Write one reply, then read the next request or close.

        `self.keep_alive` decides which, as `_frame` set it.
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
        else:
            self.client.close()
            # The only place besides `run`'s own `except Exception` above
            # and `RpcManager.stop`'s shutdown sweep that removes this id
            # from `manager.connections`: the parse-error branch of `run`
            # above pops nothing itself, deliberately, so this is what
            # removes its entry once this connection is actually done
            # rather than only kept reachable for `RpcManager.stop`'s own
            # socket-closing sweep in the meantime (issue #640 review
            # round 2).
            self.manager.connections.pop(self.id, None)

    def send(self, reply: HttpReply) -> None:
        """Schedule `async_send` on `loop`, from `handle_rpc`'s own thread."""
        asyncio.run_coroutine_threadsafe(self.async_send(reply), self.loop)

    # Use with care
    def send_and_wait(self, reply: HttpReply) -> None:
        """Like `send`, but block up to 2 seconds for the write to finish.

        `handle_rpc`'s own `stop` request is the only caller: the client
        has to see its own reply before `node.stop()` starts tearing
        `loop` down under it. Writes `Connection: close` and closes,
        whatever the request asked for -- `RpcManager.stop`, called right
        after this returns, tears the whole loop down, so there is no
        next request this connection could still answer.
        """
        future = asyncio.run_coroutine_threadsafe(
            self.async_send(reply, close=True), self.loop
        )
        with contextlib.suppress(TimeoutError):
            future.result(timeout=2)

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
