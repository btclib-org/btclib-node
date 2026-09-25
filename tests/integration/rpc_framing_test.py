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
(btclib-org/btclib-node#1150), and an object naming a key twice
(btclib-org/btclib-node#1151). Each exchange ends in a request asking
for `Connection: close` or one libevent closes after, so each answer
ends at the close.
"""

import base64
import re
import socket
from typing import TYPE_CHECKING

import pytest

from btclib_node import Node
from btclib_node.config import Config
from tests import cookie_path, get_random_port, wait_until_listening

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

_GOOD = b'{"id":1,"method":"getblockcount"}'

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
