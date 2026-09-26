# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The JSON-RPC listener's answers, this node's against a real bitcoind's.

Each request is written over a raw socket to both, with each side's own
cookie, and the two answers are held to the same status line, the same
body and the same close: the request lines and `Content-Length` values
libevent refuses (btclib-org/btclib-node#1086), the legacy and 2.0
envelopes `HTTPReq_JSONRPC` answers with (btclib-org/btclib-node#1109),
the request-targets libevent refuses or proxies and a `CONNECT`
(btclib-org/btclib-node#1125), the version an answer is written in
(btclib-org/btclib-node#1127), lines ended by a bare line feed
(btclib-org/btclib-node#1150), an object naming a key twice
(btclib-org/btclib-node#1151), the header fields, the header section's
size and the chunked bodies libevent reads or refuses
(btclib-org/btclib-node#1126), and named parameters
(btclib-org/btclib-node#1168). Each exchange ends in a request asking
for `Connection: close` or one libevent closes after, so each answer
ends at the close.
"""

import base64
import re
import socket
from typing import TYPE_CHECKING, Any, cast

import pytest

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.rpc.callbacks import arg_names
from tests import cookie_path, get_random_port, wait_until_listening

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

_GOOD = b'{"id":1,"method":"getblockcount"}'

# regtest's genesis block, which both nodes hold from the start
_GENESIS = b"0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"

# a request line, the header fields after the credential, and the body
_CASES = {
    "no-version": (b"POST /", b"Content-Length: %d\r\n" % len(_GOOD), _GOOD),
    "http-2.0": (b"POST / HTTP/2.0", b"Content-Length: %d\r\n" % len(_GOOD), _GOOD),
    "version-foo": (b"POST / FOO", b"Content-Length: %d\r\n" % len(_GOOD), _GOOD),
    "length-negative": (b"POST / HTTP/1.1", b"Content-Length: -1\r\n", b""),
    "length-abc": (b"POST / HTTP/1.1", b"Content-Length: abc\r\n", b""),
    "length-past-max": (b"POST / HTTP/1.1", b"Content-Length: 33554433\r\n", b""),
    "legacy-params-5": (
        b"POST / HTTP/1.1",
        b"",
        b'{"id":1,"method":"getblockcount","params":5}',
    ),
    "legacy-parse-error": (b"POST / HTTP/1.1", b"", b"{"),
    "legacy-not-found": (
        b"POST / HTTP/1.1",
        b"",
        b'{"jsonrpc":"1.0","id":1,"method":"x"}',
    ),
    "legacy-top-level": (b"POST / HTTP/1.1", b"", b"5"),
    "legacy-no-id": (b"POST / HTTP/1.1", b"", b'{"method":"getblockcount"}'),
    "2.0-not-found": (
        b"POST / HTTP/1.1",
        b"",
        b'{"jsonrpc":"2.0","id":1,"method":"x"}',
    ),
    "2.0-params-5": (
        b"POST / HTTP/1.1",
        b"",
        b'{"jsonrpc":"2.0","id":1,"method":"getblockcount","params":5}',
    ),
    "2.0-notification": (
        b"POST / HTTP/1.1",
        b"",
        b'{"jsonrpc":"2.0","method":"getblockcount"}',
    ),
    "batch": (
        b"POST / HTTP/1.1",
        b"",
        b'[{"id":7,"method":"getblockcount"},5,{"jsonrpc":"2.0","method":"x"}]',
    ),
    "method-twice": (
        b"POST / HTTP/1.1",
        b"",
        b'{"id":1,"method":"getblockcount","method":"nosuch"}',
    ),
    "id-key-twice": (
        b"POST / HTTP/1.1",
        b"",
        b'{"id":{"a":1,"a":2},"method":"getblockcount"}',
    ),
}

# named parameters, each a `params` object sent as a legacy request, as
# a 2.0 one, and as a batch member
_NAMED = {
    "named": b'"getblockhash","params":{"height":0}',
    "named-none": b'"getblockcount","params":{}',
    "named-twice": b'"getblockhash","params":{"height":0,"height":5}',
    "named-unknown": b'"getblockcount","params":{"b":1,"a":2}',
    "named-unknown-after-a-known": b'"getblockhash","params":{"x":1,"height":0}',
    "named-not-found": b'"nosuch","params":{"a":1,"a":2}',
    "named-hole": (
        b'"getrawtransaction","params":{"txid":"%s","blockhash":"%s"}'
        % (b"00" * 32, b"00" * 32)
    ),
    "named-alias": b'"getblockheader","params":{"blockhash":"%s","verbose":false}'
    % _GENESIS,
    "named-both-aliases": (
        b'"getblock","params":{"blockhash":"%s","verbose":0,"verbosity":1}' % _GENESIS
    ),
    "named-args": b'"getblockhash","params":{"args":[0]}',
    "named-args-then-named": (
        b'"getblock","params":{"args":["%s"],"verbose":0}' % _GENESIS
    ),
    "named-args-and-named": b'"getblockhash","params":{"args":[0],"height":0}',
    "named-args-and-alias": (
        b'"getblock","params":{"args":["%s",1],"verbose":0}' % _GENESIS
    ),
    "named-args-twice": b'"getblockhash","params":{"args":[0],"args":[1]}',
    "named-args-not-an-array": b'"getblockhash","params":{"args":5,"height":0}',
    "named-args-unknown": b'"getblockhash","params":{"args":"a","height":0,"q":1}',
}
for _name, _call in _NAMED.items():
    for _prefix, _request_head, _suffix in (
        ("", b'{"id":1,"method":', b"}"),
        ("2.0-", b'{"jsonrpc":"2.0","id":1,"method":', b"}"),
        ("batch-", b'[{"id":1,"method":', b"}]"),
    ):
        _CASES[_prefix + _name] = (
            b"POST / HTTP/1.1",
            b"",
            _request_head + _call + _suffix,
        )


# written whole, `{AUTH}` standing for the `Authorization` field, and
# ending in a request that closes, where the one before it is kept open
_THEN_CLOSE = b"POST /x HTTP/1.1\r\nConnection: close\r\n\r\n"
_RAW = {
    "target-colon-first": b"POST 1:x HTTP/1.1\r\n{AUTH}\r\n\r\n",
    "target-port-past-65535": b"POST http://h:99999/ HTTP/1.1\r\n\r\n",
    "target-bad-userinfo": b"POST http://a b@h/ HTTP/1.1\r\n\r\n",
    "target-bad-ipv6": b"POST http://[zz]/ HTTP/1.1\r\n\r\n",
    "target-ipv6-five-digit-group": b"POST http://[00000::1]/ HTTP/1.1\r\n\r\n",
    "target-ipv6-scope": b"POST http://[::1%25lo0]/ HTTP/1.1\r\n\r\n",
    "target-ipv6": b"POST http://[::1]:1/ HTTP/1.1\r\n\r\n",
    "absolute-form": b"POST http://127.0.0.1:1/ HTTP/1.1\r\n{AUTH}\r\n\r\n",
    "absolute-form-proxy-keep-alive": (
        b"POST HTTP://h/ HTTP/1.1\r\nProxy-Connection: keep-alive\r\n\r\n"
    ),
    "absolute-form-length-abc": b"POST https://h/ HTTP/1.1\r\nContent-Length: abc\r\n\r\n",
    "absolute-form-length-abc-proxy-keep-alive": (
        b"POST http://h/ HTTP/1.0\r\nContent-Length: abc\r\n"
        b"Proxy-Connection: keep-alive\r\n\r\n"
    ),
    "other-scheme-kept": b"POST ftp://h/ HTTP/1.1\r\n\r\n" + _THEN_CLOSE,
    "no-authority-kept": b"POST http:/ HTTP/1.1\r\n\r\n" + _THEN_CLOSE,
    "connect": b"CONNECT / HTTP/1.1\r\n\r\n" + _THEN_CLOSE,
    "connect-1.0": b"CONNECT h:1 HTTP/1.0\r\n\r\n" + _THEN_CLOSE,
    "connect-close": b"CONNECT / HTTP/1.1\r\nConnection: close\r\n\r\n" + _THEN_CLOSE,
    "connect-length-abc": (
        b"CONNECT / HTTP/1.1\r\nContent-Length: abc\r\n\r\n" + _THEN_CLOSE
    ),
    "connect-port-past-65535": b"CONNECT h:99999 HTTP/1.1\r\n\r\n" + _THEN_CLOSE,
    "http-1.0": b"POST / HTTP/1.0\r\n{AUTH}\r\nContent-Length: 33\r\n\r\n" + _GOOD,
    "http-1.0-keep-alive": (
        b"POST / HTTP/1.0\r\n{AUTH}\r\nConnection: keep-alive\r\n"
        b"Content-Length: 33\r\n\r\n" + _GOOD + _THEN_CLOSE
    ),
    "http-1.0-close": (
        b"POST / HTTP/1.0\r\n{AUTH}\r\nConnection: close\r\n"
        b"Content-Length: 33\r\n\r\n" + _GOOD
    ),
    "http-0.9": b"POST / HTTP/0.9\r\n{AUTH}\r\nContent-Length: 33\r\n\r\n" + _GOOD,
    "http-1.5": (
        b"POST / HTTP/1.5\r\n{AUTH}\r\nConnection: close\r\n"
        b"Content-Length: 33\r\n\r\n" + _GOOD
    ),
    "http-1.-1": b"POST / HTTP/1.-1\r\n{AUTH}\r\nContent-Length: 33\r\n\r\n" + _GOOD,
    "http-1.0-401": b"POST / HTTP/1.0\r\nContent-Length: 33\r\n\r\n" + _GOOD,
    "http-1.0-404": b"POST /x HTTP/1.0\r\n\r\n",
    "http-1.0-length-abc": b"POST / HTTP/1.0\r\nContent-Length: abc\r\n\r\n",
    "http-1.5-length-abc": b"POST / HTTP/1.5\r\nContent-Length: abc\r\n\r\n",
    "http-1.0-501": b"FOO / HTTP/1.0\r\n\r\n",
    "lf-every-line": (
        b"POST / HTTP/1.1\n{AUTH}\nConnection: close\nContent-Length: 33\n\n" + _GOOD
    ),
    "lf-request-line": (
        b"POST / HTTP/1.1\nHost: x\r\n{AUTH}\r\nConnection: close\r\n"
        b"Content-Length: 33\r\n\r\n" + _GOOD
    ),
    "lf-empty-line": (
        b"POST / HTTP/1.1\r\n{AUTH}\r\nConnection: close\r\n"
        b"Content-Length: 33\r\n\n" + _GOOD
    ),
    "lf-last-field": (
        b"POST / HTTP/1.1\r\n{AUTH}\r\nConnection: close\r\n"
        b"Content-Length: 33\n\r\n" + _GOOD
    ),
}


# issue #1126: header fields as `evhttp_parse_headers_` reads them, the
# section's size as it counts it, and a chunked body
_POST = b"POST / HTTP/1.1\r\n{AUTH}\r\n"
_CLOSE = b"Connection: close\r\n"
_LENGTH = b"Content-Length: 33\r\n"
_TE = b"Transfer-Encoding: chunked\r\n"
_CHUNKED = b"21\r\n" + _GOOD + b"\r\n0\r\n\r\n"


def _pad(size: int, eol: bytes = b"\r\n") -> bytes:
    """Return a field line of `size` octets, `eol` apart."""
    return b"X: " + b"a" * (size - 3) + eol


def _fields(fields: bytes) -> bytes:
    """Return a closing request carrying `fields` and `_GOOD`."""
    return _POST + fields + _CLOSE + _LENGTH + b"\r\n" + _GOOD


def _chunked(fields: bytes, body: bytes) -> bytes:
    """Return a closing chunked request carrying `fields` and `body`."""
    return _POST + fields + _CLOSE + _TE + b"\r\n" + body


# the octets of `_POST` and `_CLOSE` and `_LENGTH`, their endings apart,
# the credential's line as long for either side's cookie
_HEAD = len(b"POST / HTTP/1.1") + 121 + len(_CLOSE + _LENGTH) - 4
_RAW |= {
    "101-fields": _fields(b"X: y\r\n" * 101),
    "cr-in-value": _fields(b"X: close\rY: y\r\n"),
    "cr-space-in-value": _fields(b"X: a\r b\r\n"),
    "cr-ending-value": _fields(b"X: a\r\r\n"),
    "cr-in-key": _fields(b"X\r: y\r\n"),
    "no-colon": _fields(b"garbage\r\n"),
    "empty-key": _fields(b": x\r\n"),
    "space-in-key": _fields(b"X Y: z\r\n"),
    "tab-before-colon": _fields(b"Connection\t: keep-alive\r\n"),
    "continuation-first": b"POST / HTTP/1.1\r\n x\r\n{AUTH}\r\n" + _CLOSE,
    "continuation-length": _POST + _CLOSE + b"Content-Length:\r\n\t33\r\n\r\n" + _GOOD,
    "continuation-length-twice": (
        _POST + _CLOSE + b"Content-Length: 3\r\n 3\r\n\r\n" + _GOOD
    ),
    "length-twice": _POST + _CLOSE + _LENGTH + b"Content-Length: 5\r\n\r\n" + _GOOD,
    "nul-line-ends-head": _POST + _CLOSE + _LENGTH + b"\0junk\r\n" + _GOOD,
    "nul-in-value": _POST + b"Connection: close\0junk\r\n" + _LENGTH + b"\r\n" + _GOOD,
    "nul-in-key": _fields(b"Connection\0: keep-alive\r\n"),
    "tab-close-kept": (
        _POST + b"Connection:\tclose\r\n" + _LENGTH + b"\r\n" + _GOOD + _THEN_CLOSE
    ),
    "spaces-then-tab-close-kept": (
        _POST + b"Connection:  \tclose\r\n" + _LENGTH + b"\r\n" + _GOOD + _THEN_CLOSE
    ),
    "continuation-close-kept": (
        _POST + b"Connection:\r\n close\r\n" + _LENGTH + b"\r\n" + _GOOD + _THEN_CLOSE
    ),
    "http-1.0-tab-keep-alive": (
        b"POST / HTTP/1.0\r\n{AUTH}\r\nConnection:\tkeep-alive\r\n"
        + _LENGTH
        + b"\r\n"
        + _GOOD
        + _THEN_CLOSE
    ),
    "size-8192": _fields(_pad(8192 - _HEAD)),
    "size-8193": _fields(_pad(8193 - _HEAD)),
    "size-8192-lf": _fields(_pad(8192 - _HEAD, b"\n")),
    "size-8192-many-lines": _fields(b"X:\r\n" * ((8192 - _HEAD) // 2) + b":\r\n"),
    "size-8193-many-lines": _fields(b"X:\r\n" * ((8193 - _HEAD) // 2)),
    "unended-8193": b"POST / HTTP/1.1\r\n" + _pad(8193 - 15, b""),
    "request-line-8192": b"POST /" + b"a" * (8192 - 15) + b" HTTP/1.0\r\n\r\n",
    "request-line-8193": b"POST /" + b"a" * (8193 - 15) + b" HTTP/1.0\r\n\r\n",
    "connect-bad-field": b"CONNECT h:1 HTTP/1.1\r\nbad\r\n" + _THEN_CLOSE,
    "connect-bad-field-then-more": (
        b"CONNECT h:1 HTTP/1.1\r\nX: y\r\nbad\r\nZ: w\r\n\r\n" + _THEN_CLOSE
    ),
    "connect-cr-in-value": b"CONNECT h:1 HTTP/1.1\r\nX: a\rb\r\n" + _THEN_CLOSE,
    "chunked": _chunked(b"", _CHUNKED),
    "chunked-two": _chunked(
        b"", b"10\r\n" + _GOOD[:16] + b"\r\n11\r\n" + _GOOD[16:] + b"\r\n0\r\n\r\n"
    ),
    "chunked-no-line-end": _chunked(
        b"", b"10\r\n" + _GOOD[:16] + b"11\r\n" + _GOOD[16:] + b"0\r\n\r\n"
    ),
    "chunked-empty-lines": _chunked(b"", b"\r\n\r\n" + _CHUNKED),
    "chunked-extension": _chunked(b"", b"21 ;a=b\r\n" + _GOOD + b"\r\n0\r\n\r\n"),
    "chunked-0x": _chunked(b"", b"0x21\r\n" + _GOOD + b"\r\n0\r\n\r\n"),
    "chunked-tab-led": _chunked(b"", b"\t21\r\n" + _GOOD + b"\r\n0\r\n\r\n"),
    "chunked-space-ends": _chunked(b"", b"21\r\n" + _GOOD + b"\r\n \r\n\r\n"),
    "chunked-nul-after-size": _chunked(b"", b"21\0x\r\n" + _GOOD + b"\r\n0\r\n\r\n"),
    "chunked-over-length": _chunked(b"Content-Length: 5\r\n", _CHUNKED),
    "chunked-any-case": (
        _POST + _CLOSE + b"Transfer-Encoding: ChUnKeD\r\n\r\n" + _CHUNKED
    ),
    "chunked-tab-led-encoding": _fields(b"Transfer-Encoding:\tchunked\r\n"),
    "chunked-http-1.0": b"POST / HTTP/1.0\r\n{AUTH}\r\n" + _TE + b"\r\n" + _CHUNKED,
    "chunked-kept": _POST + _TE + b"\r\n" + _CHUNKED + _THEN_CLOSE,
    "chunked-semicolon": _chunked(b"", b"21;a=b\r\n"),
    "chunked-not-hex": _chunked(b"", b"zz\r\n"),
    "chunked-negative": _chunked(b"", b"-1\r\n"),
    "chunked-0x-alone": _chunked(b"", b"0x\r\n"),
    "chunked-nul-led-size": _chunked(b"", b"\x0021\r\n" + _GOOD + b"\r\n"),
    "chunked-past-max": _chunked(b"", b"2000001\r\n"),
    "trailer": _chunked(b"", _CHUNKED[:-2] + b"X: y\r\n\r\n"),
    "trailer-credential": (
        b"POST / HTTP/1.1\r\n"
        + _CLOSE
        + _TE
        + b"\r\n"
        + _CHUNKED[:-2]
        + b"{AUTH}\r\n\r\n"
    ),
    "trailer-close": (
        _POST + _TE + b"\r\n" + _CHUNKED[:-2] + _CLOSE + b"\r\n" + _THEN_CLOSE
    ),
    "trailer-continuation": _chunked(b"", _CHUNKED[:-2] + b" x\r\n\r\n"),
    "trailer-nul-ends": _chunked(b"", _CHUNKED[:-2] + b"\0junk\r\n"),
    "trailer-no-colon": _chunked(b"", _CHUNKED[:-2] + b"bad\r\n"),
    "trailer-8192": _chunked(
        _pad(8180 - _HEAD + len(_LENGTH) - len(_TE)), _CHUNKED[:-2] + _pad(12) + b"\r\n"
    ),
    "trailer-8193": _chunked(
        _pad(8180 - _HEAD + len(_LENGTH) - len(_TE)), _CHUNKED[:-2] + _pad(13) + b"\r\n"
    ),
    "trailer-proxy-keep-alive-refused": (
        b"POST http://x/ HTTP/1.1\r\n"
        + _TE
        + b"\r\n"
        + _CHUNKED[:-2]
        + b"Proxy-Connection: keep-alive\r\nbad\r\n"
    ),
    "trailer-close-refused": (
        _POST + _TE + b"\r\n" + _CHUNKED[:-2] + _CLOSE + b"bad\r\n"
    ),
    "connect-chunked-not-hex": (
        b"CONNECT h:1 HTTP/1.1\r\n" + _TE + b"\r\nzz\r\n" + _THEN_CLOSE
    ),
}


def _request(case: str, credential: bytes) -> bytes:
    """Build `case`'s request, closing, with `credential` as its cookie."""
    auth = b"Authorization: Basic " + base64.b64encode(credential)
    if case in _RAW:
        return _RAW[case].replace(b"{AUTH}", auth)
    line, fields, body = _CASES[case]
    if not fields:
        fields = b"Connection: close\r\nContent-Length: %d\r\n" % len(body)
    return line + b"\r\nHost: x\r\n" + auth + b"\r\n" + fields + b"\r\n" + body


def _answer(port: int, data: bytes) -> list[tuple[bytes, list[bytes], bytes]]:
    """Return each answer's status line, framing fields and body, to the close.

    `Date` and `Content-Type` are left out: libevent writes them and this
    node does not, as `RpcConnection._send_refusal` says. An answer ends
    where the next status line starts, since an answer libevent writes
    with no `Content-Length` says nothing else about where it ends.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        client.sendall(data)
        reply = b""
        while chunk := client.recv(65536):
            reply += chunk
    answers = []
    for answer in re.split(rb"(?=HTTP/-?[0-9]+\.-?[0-9]+ [0-9]{3} )", reply)[1:]:
        head, _, body = answer.partition(b"\r\n\r\n")
        status, *fields = head.split(b"\r\n")
        kept = [
            field
            for field in fields
            if not field.lower().startswith((b"date:", b"content-type:"))
        ]
        answers.append((status, sorted(kept), body))
    return answers


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    """Start a regtest node listening for RPC, and stop it afterwards."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
        )
    )
    node.start()
    try:
        wait_until_listening(node.rpc_manager)
        yield node
    finally:
        node.stop()
        node.join()


@pytest.mark.parametrize("case", [*_CASES, *_RAW])
def test_the_answer_is_bitcoind_s(case: str, bitcoind: Bitcoind, node: Node) -> None:
    """This node answers `case` with `bitcoind`'s status, fields and body."""
    theirs = _answer(
        bitcoind.rpc_port, _request(case, bitcoind.cookie_path.read_bytes())
    )
    # set by the fixture's own `Config`
    assert node.rpc_port is not None
    ours = _answer(
        node.rpc_port, _request(case, cookie_path(node.config.data_dir).read_bytes())
    )
    assert theirs
    assert ours == theirs


def test_each_method_names_its_positions_as_bitcoind_does(bitcoind: Bitcoind) -> None:
    """`arg_names` is `bitcoind`'s own table, for every method this node has.

    `help dump_all_command_conversions` answers one row per name, each
    naming its method and its position; a method taking nothing has no
    row.
    """
    rows = bitcoind.rpc("help", ["dump_all_command_conversions"])
    names: dict[str, dict[int, list[str]]] = {}
    for method, position, name, _ in cast("list[list[Any]]", rows):
        names.setdefault(method, {}).setdefault(position, []).append(name)
    theirs = {}
    for method in arg_names:
        positions = names.get(method, {})
        theirs[method] = tuple("|".join(positions[i]) for i in range(len(positions)))
    assert theirs == arg_names
