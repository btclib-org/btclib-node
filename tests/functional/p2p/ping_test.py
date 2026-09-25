# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A `pong` matching its `ping` is recorded, one that does not is ignored."""

import time
from typing import TYPE_CHECKING

from btclib.p2p.keepalive import Ping, Pong

from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.callbacks import callbacks
from tests import local_addr, wait_until, wait_until_listening
from tests.conftest import node_context

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from btclib_node import Node
    from btclib_node.p2p.connection import Connection


def test_correct_ping(tmp_path: Path) -> None:
    """A `ping` sent to a real peer comes back as a `pong` that sets latency.

    `ping_nonce` and `ping_sent` are set by hand rather than through
    `send_ping`, so the nonce matched against the peer's `pong` is
    known in advance; `conn.latency` going from unset to a value is
    what `p2p.callbacks.pong` does once it matches that nonce.
    """
    with (
        node_context(tmp_path / "node1", allow_rpc=False) as node1,
        node_context(tmp_path / "node2", allow_rpc=False) as node2,
    ):
        wait_until_listening(node1.p2p_manager)
        wait_until_listening(node2.p2p_manager)

        node2.p2p_manager.connect(local_addr(node1.p2p_port))
        # each side's own `connections` only holds a peer past its own
        # `verack`, and the two handshakes complete independently, so each
        # is waited for on its own rather than assuming one implies the other
        wait_until(lambda: len(node1.p2p_manager.connections))
        conn = node1.p2p_manager.connections[0]
        wait_until(lambda: conn.status == P2pConnStatus.Connected)
        wait_until(lambda: len(node2.p2p_manager.connections))
        conn = node2.p2p_manager.connections[0]
        wait_until(lambda: conn.status == P2pConnStatus.Connected)

        conn = node1.p2p_manager.connections[0]
        # wait until the previous ping is cleared
        wait_until(lambda: conn.ping_nonce == 0)

        conn.ping_sent = time.time()
        conn.ping_nonce = 1
        conn.send(Ping(1))
        wait_until(lambda: conn.latency)


def test_wrong_ping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ISS 1133: a `pong` whose nonce matches no pending `ping` is ignored.

    `node1` is made to expect nonce `1` (`ping_nonce` set by hand) and
    then sends a `Ping(2)`; node2's `ping` handler echoes `2` back in
    its `pong`. Core's `PONG` logs "Nonce mismatch" and punishes nobody,
    so once `node1` has handled that `pong` the connection is still
    held, its host is not discouraged, and the `ping` is still pending.
    """
    handled: list[int] = []
    original = callbacks["pong"]

    def counting(node: Node, msg: bytes, conn: Connection) -> None:
        original(node, msg, conn)
        handled.append(Pong.parse(msg).nonce)

    monkeypatch.setitem(callbacks, "pong", counting)
    with (
        node_context(tmp_path / "node1", allow_rpc=False) as node1,
        node_context(tmp_path / "node2", allow_rpc=False) as node2,
    ):
        wait_until_listening(node1.p2p_manager)
        wait_until_listening(node2.p2p_manager)

        node2.p2p_manager.connect(local_addr(node1.p2p_port))
        # each side's own `connections` only holds a peer past its own
        # `verack`, and the two handshakes complete independently, so each
        # is waited for on its own rather than assuming one implies the other
        wait_until(lambda: len(node1.p2p_manager.connections))
        connection = node1.p2p_manager.connections[0]
        wait_until(lambda: connection.status == P2pConnStatus.Connected)
        wait_until(lambda: len(node2.p2p_manager.connections))
        connection = node2.p2p_manager.connections[0]
        wait_until(lambda: connection.status == P2pConnStatus.Connected)

        # the ping `verack` sends answered first, as `test_correct_ping` above
        # waits for too: `connections` holds a peer before `verack` has sent
        # it, so that ping can go out after the one below and overwrite the
        # nonce set by hand (btclib-org/btclib-node#1037)
        node1_conn = node1.p2p_manager.connections[0]
        wait_until(lambda: node1_conn.ping_nonce == 0)
        node1_conn.ping_sent = time.time()
        node1_conn.ping_nonce = 1
        node1.p2p_manager.send(Ping(2), 0)

        wait_until(lambda: 2 in handled)
        assert node1.p2p_manager.connections[0] is node1_conn
        assert node1_conn.status == P2pConnStatus.Connected
        assert node1_conn.ping_nonce == 1
        assert not node1.p2p_manager.is_discouraged(node1_conn.address)
