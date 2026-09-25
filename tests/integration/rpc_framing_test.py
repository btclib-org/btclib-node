# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The JSON-RPC listener's answers, this node's against a real bitcoind's.

Each request is written over a raw socket to both, with each side's own
cookie, and the two answers are held to the same status line, the same
body and the same close: the request lines and `Content-Length` values
libevent refuses (btclib-org/btclib-node#1086), the legacy and 2.0
envelopes `HTTPReq_JSONRPC` answers with (btclib-org/btclib-node#1109),
and an object naming a key twice (btclib-org/btclib-node#1151).
Each request asks for `Connection: close` or is one libevent closes
after, so each answer ends at the close.
"""

import base64
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


def _request(case: str, credential: bytes) -> bytes:
    """Build `case`'s request, closing, with `credential` as its cookie."""
    line, fields, body = _CASES[case]
    if not fields:
        fields = b"Connection: close\r\nContent-Length: %d\r\n" % len(body)
    auth = b"Authorization: Basic " + base64.b64encode(credential) + b"\r\n"
    return line + b"\r\nHost: x\r\n" + auth + fields + b"\r\n" + body


def _answer(port: int, data: bytes) -> tuple[bytes, list[bytes], bytes]:
    """Return the status line, the framing fields and the body until the close.

    `Date` and `Content-Type` are left out: libevent writes them and this
    node does not, as `RpcConnection._send_refusal` says.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        client.sendall(data)
        reply = b""
        while chunk := client.recv(65536):
            reply += chunk
    head, _, body = reply.partition(b"\r\n\r\n")
    status, *fields = head.split(b"\r\n")
    kept = [
        field
        for field in fields
        if not field.lower().startswith((b"date:", b"content-type:"))
    ]
    return status, sorted(kept), body


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


@pytest.mark.parametrize("case", _CASES)
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
    assert ours == theirs
