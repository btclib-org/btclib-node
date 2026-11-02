# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The RPC idle timeout and shutdown, this node's against a real bitcoind's.

Both run with `-rpcservertimeout=1`, and each case is driven over raw
sockets against both, its outcome held to the same answer: an idle
connection closed after about a second, a request sent in pieces each
within that second still answered, and a `stop` whose reply waits while
a kept-alive connection idles (btclib-org/btclib-node#2152).
"""

import base64
import contextlib
import json
import socket
import time
from typing import TYPE_CHECKING

import pytest

from btclib_node import Node
from btclib_node.config import Config
from tests import cookie_path, get_random_port, wait_until_listening
from tests.integration.conftest import _started_bitcoind

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# how long a read waits before the test fails, well past every bound
# these cases wait for
_READ_TIMEOUT = 10.0


@pytest.fixture
def bitcoind_port(bitcoind_path: str, tmp_path: Path) -> Iterator[tuple[int, bytes]]:
    """Start bitcoind with `-rpcservertimeout=1`; give its port and cookie."""
    with _started_bitcoind(bitcoind_path, tmp_path, "-rpcservertimeout=1") as node:
        yield node.rpc_port, node.cookie_path.read_bytes()


@pytest.fixture
def node_port(tmp_path: Path) -> Iterator[tuple[int, bytes]]:
    """Start this node with `-rpcservertimeout=1`; give its port and cookie."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
            rpcservertimeout=1,
        )
    )
    node.start()
    try:
        wait_until_listening(node.rpc_manager)
        assert node.rpc_port is not None
        yield node.rpc_port, cookie_path(node.config.data_dir).read_bytes()
    finally:
        node.stop()
        node.join()


def _request(cookie: bytes, method: str, params: list[object]) -> bytes:
    """Frame a kept-alive JSON-RPC request carrying `cookie`."""
    body = json.dumps({"id": 1, "method": method, "params": params}).encode()
    credential = base64.b64encode(cookie.strip())
    return (
        b"POST / HTTP/1.1\r\nHost: x\r\nAuthorization: Basic "
        + credential
        + b"\r\nContent-Length: %d\r\n\r\n" % len(body)
        + body
    )


def _read_answer(sock: socket.socket) -> tuple[bytes, bytes]:
    """Read one answer off `sock`: its status line and its body."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(1 << 16)
        assert chunk, f"closed before an answer: {data!r}"
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    while len(rest) < length:
        chunk = sock.recv(1 << 16)
        assert chunk, f"closed inside an answer: {head!r}"
        rest += chunk
    return head.split(b"\r\n")[0], rest[:length]


def _read_to_close(sock: socket.socket) -> None:
    """Read `sock` until the other end closes it, or `_READ_TIMEOUT` fails."""
    # a reset is a close too
    with contextlib.suppress(ConnectionResetError):
        while sock.recv(1 << 16):
            pass


def _idle_close(port: int) -> float:
    """Return the seconds a connection that sends nothing stays open."""
    with socket.create_connection(("127.0.0.1", port), timeout=_READ_TIMEOUT) as sock:
        opened = time.monotonic()
        _read_to_close(sock)
        return time.monotonic() - opened


def _pieces(port: int, cookie: bytes) -> bytes:
    """Send a request in four pieces 0.6 seconds apart; give its status line."""
    data = _request(cookie, "getblockcount", [])
    size = len(data) // 4 + 1
    with socket.create_connection(("127.0.0.1", port), timeout=_READ_TIMEOUT) as sock:
        for start in range(0, len(data), size):
            time.sleep(0.6)
            sock.sendall(data[start : start + size])
        return _read_answer(sock)[0]


def _stop(port: int, cookie: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    """Stop with a 2-second `wait` beside a kept-alive connection.

    The kept-alive connection answers one request first, then sends
    another 0.5 seconds into the `stop`, and is read until it is closed.
    Gives both of its status lines, then `stop`'s own status line and
    result.
    """
    idle = socket.create_connection(("127.0.0.1", port), timeout=_READ_TIMEOUT)
    stopping = socket.create_connection(("127.0.0.1", port), timeout=_READ_TIMEOUT)
    try:
        idle.sendall(_request(cookie, "getblockcount", []))
        first, _ = _read_answer(idle)
        stopping.sendall(_request(cookie, "stop", [2000]))
        time.sleep(0.5)
        idle.sendall(_request(cookie, "getblockcount", []))
        during, _ = _read_answer(idle)
        _read_to_close(idle)
        status, body = _read_answer(stopping)
    finally:
        idle.close()
        stopping.close()
    return first, during, status, json.loads(body)["result"].encode()


def test_an_idle_connection_is_closed_as_bitcoind_closes_it(
    bitcoind_port: tuple[int, bytes], node_port: tuple[int, bytes]
) -> None:
    """At `-rpcservertimeout=1` both close a silent connection within 3 s."""
    theirs = _idle_close(bitcoind_port[0])
    ours = _idle_close(node_port[0])
    assert 0.9 < theirs < 3.0
    assert 0.9 < ours < 3.0


def test_a_request_in_pieces_restarts_the_timer_as_bitcoind_does(
    bitcoind_port: tuple[int, bytes], node_port: tuple[int, bytes]
) -> None:
    """A request taking 2.4 s, in pieces under a second apart, is answered."""
    theirs = _pieces(*bitcoind_port)
    ours = _pieces(*node_port)
    assert theirs == ours == b"HTTP/1.1 200 OK"


def test_stop_beside_an_idle_connection_is_bitcoind_s(
    bitcoind_port: tuple[int, bytes], node_port: tuple[int, bytes]
) -> None:
    """`stop` refuses the idle connection's request 503, then closes it."""
    theirs = _stop(*bitcoind_port)
    ours = _stop(*node_port)
    assert theirs[:3] == ours[:3]
    assert theirs[:3] == (
        b"HTTP/1.1 200 OK",
        b"HTTP/1.1 503 Service Unavailable",
        b"HTTP/1.1 200 OK",
    )
    assert theirs[3].endswith(b" stopping")
    assert ours[3].endswith(b" stopping")
