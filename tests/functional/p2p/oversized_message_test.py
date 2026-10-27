# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A message past `MAX_PROTOCOL_MESSAGE_LENGTH` drops its peer, of any type.

Core's `V1Transport::readHeader` refuses a header announcing a payload
longer than `MAX_PROTOCOL_MESSAGE_LENGTH`, and `ReceiveMsgBytes` answers
by disconnecting the peer, which nothing discourages (`src/net.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag). The bound is the same for
every type: nothing judges the command before the length.

Each command the node dispatches is sent once, from a raw socket, at one
octet past the bound. `version` opens the connection, another command of
`handshake_callbacks` follows the peer's `version`, and a command of
`callbacks` follows a completed handshake. The commands are read from
the two dispatch tables, so a command added to either is sent here too.

Only a peer's own `version` tells the node it speaks v1: other opening
octets are read as a v2 handshake, which fails on its own account and
would drop the peer whatever the length.

Over v2 the length is decrypted and checked before the command is, so
one test holds that bound for every type:
`tests/unit/p2p/v2transport_test.py`.

The bounds a type holds on its own contents, counts such as
`MAX_INV_SZ`, are each checked where the callback reads them, in
`tests/unit/p2p/callbacks_test.py`.
"""

import functools
import secrets
import socket
import time
from collections import deque
from contextlib import suppress
from io import BytesIO
from typing import TYPE_CHECKING, Any

import pytest
from btclib.exceptions import IncompleteMessageError
from btclib.hashes import hash256
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.p2p.handshake import Verack, Version
from btclib.p2p.keepalive import Ping, Pong
from btclib.p2p.limits import MAX_PROTOCOL_MESSAGE_LENGTH, PROTOCOL_VERSION
from btclib.p2p.message import Message
from btclib.p2p.negotiation import WtxidRelay

from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.callbacks import callbacks, handshake_callbacks
from tests import discourage_recorder, wait_until, wait_until_listening
from tests.conftest import node_context

if TYPE_CHECKING:
    from collections.abc import Iterator

    from btclib.p2p.payload import Payload

    from btclib_node import Node
    from btclib_node.p2p.connection import Connection

_COMMANDS = [*handshake_callbacks, *callbacks]


def test_no_command_is_in_both_tables() -> None:
    """Each command is sent once, set up as the one table holding it says."""
    assert not set(handshake_callbacks) & set(callbacks)
    assert len(_COMMANDS) == len(set(_COMMANDS))


@pytest.fixture(scope="module")
def node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Node, list[Any]]]:
    """Give one node for the module, accepting v1, and its judgments.

    `maybe_discourage_and_disconnect` is where a misbehaving peer goes.
    The peers here are local, which it drops and never discourages, so
    the recorder in its place is what tells a drop from a judgment.
    """
    judged, record = discourage_recorder()
    with (
        node_context(
            tmp_path_factory.mktemp("node"), allow_rpc=False, v1transport=True
        ) as started,
        pytest.MonkeyPatch.context() as patch,
    ):
        wait_until_listening(started.p2p_manager)
        patch.setattr(started.p2p_manager, "maybe_discourage_and_disconnect", record)
        yield started, judged


def _octets(node: Node, command: str, payload: bytes) -> bytes:
    """Frame `payload` under `command`, its checksum right.

    By hand, not by `Message`, which may refuse the length under test.
    """
    return (
        node.chain.magic
        + command.encode("ascii").ljust(12, b"\x00")
        + len(payload).to_bytes(4, "little")
        + hash256(payload)[:4]
        + payload
    )


def _framed(node: Node, payload: Payload) -> bytes:
    return Message(node.chain.magic, payload.command, payload.serialize()).serialize()


def _version() -> Version:
    services = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS
    return Version(
        version=PROTOCOL_VERSION,
        services=services,
        timestamp=int(time.time()),
        addr_recv=NetworkAddress(services=services, port=0),
        addr_from=NetworkAddress(services=services, port=0),
        nonce=secrets.randbelow(2**64),
        user_agent=b"/oversized/",
        start_height=0,
        relay=True,
    )


def _conn_of(node: Node, port: int) -> Connection | None:
    """Return the node's connection from the peer on `port`, held or pending."""
    manager = node.p2p_manager
    held = [*manager.connections.values(), *manager.pending_connections.values()]
    return next((c for c in held if c.address.port == port), None)


def _set_up(node: Node, peer: socket.socket, command: str) -> Connection:
    """Bring `peer` to where `command` is read, and return its connection.

    Accepted for `version`, past the peer's `version` for another command
    of `handshake_callbacks`, and connected for a command of `callbacks`.
    """
    port = peer.getsockname()[1]
    setup: tuple[Payload, ...] = (_version(), WtxidRelay(), Verack())
    if command == "version":
        setup = ()
    elif command in handshake_callbacks:
        setup = (_version(),)
    for message in setup:
        peer.sendall(_framed(node, message))

    def ready() -> bool:
        conn = _conn_of(node, port)
        if conn is None:
            return False
        if command in callbacks:
            return conn.status == P2pConnStatus.Connected
        return not setup or conn.version_message is not None

    wait_until(ready)
    conn = _conn_of(node, port)
    assert conn is not None
    return conn


def _read_until_hung_up(peer: socket.socket) -> None:
    """Read what the node sends until it closes or resets the connection.

    A node that keeps the connection fails the test on the socket's
    timeout.
    """
    with suppress(ConnectionError):
        deque(iter(functools.partial(peer.recv, 1 << 16), b""), maxlen=0)


@pytest.mark.parametrize("command", _COMMANDS)
def test_a_message_past_the_bound_drops_the_peer_undiscouraged(
    node: tuple[Node, list[Any]], command: str
) -> None:
    """One octet past the bound, and the node hangs up, judging nobody."""
    started, judged = node
    payload = bytes(MAX_PROTOCOL_MESSAGE_LENGTH + 1)
    with socket.create_connection(("127.0.0.1", started.p2p_port), timeout=30) as peer:
        conn = _set_up(started, peer, command)
        # the node may hang up before the payload is all sent
        with suppress(OSError):
            peer.sendall(_octets(started, command, payload))
        _read_until_hung_up(peer)
        wait_until(lambda: conn.status == P2pConnStatus.Closed)
        wait_until(lambda: _conn_of(started, conn.address.port) is None)
    assert not judged


def _pong_nonces(peer: socket.socket) -> Iterator[int]:
    """Yield the nonce of each `pong` the node sends, as it arrives."""
    buffer = b""
    while True:
        stream = BytesIO(buffer)
        try:
            message = Message.parse(stream, check_validity=False)
        except IncompleteMessageError:
            data = peer.recv(1 << 16)
            assert data, "the node hung up"
            buffer += data
            continue
        buffer = buffer[stream.tell() :]
        if message.command == "pong":
            yield Pong.parse(message.payload).nonce


def test_a_message_at_the_bound_keeps_the_peer(node: tuple[Node, list[Any]]) -> None:
    """The control: at the bound the message is read whole, and the peer kept.

    A `ping` payload with octets past its nonce does not parse, which is
    logged and keeps the peer, so the hang-up above is the length's. The
    `ping` behind it is answered, which is the node reading on.
    """
    started, judged = node
    nonce = secrets.randbelow(2**64)
    with socket.create_connection(("127.0.0.1", started.p2p_port), timeout=30) as peer:
        conn = _set_up(started, peer, "ping")
        peer.sendall(_octets(started, "ping", bytes(MAX_PROTOCOL_MESSAGE_LENGTH)))
        peer.sendall(_framed(started, Ping(nonce)))
        assert nonce in _pong_nonces(peer)
        assert _conn_of(started, conn.address.port) is conn
    assert not judged
