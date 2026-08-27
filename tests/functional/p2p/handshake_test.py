# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Two real nodes complete a handshake, and a node refuses one with itself."""

from __future__ import annotations

from typing import TYPE_CHECKING

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import P2pConnStatus
from tests import get_random_port, local_addr, wait_until, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path


def test_simple_connection(tmp_path: Path) -> None:
    """Two real nodes, connected over a socket, each reach `Connected`.

    Each side's own handshake completes independently -- `connections`
    on either node only holds the peer once its own `verack` has been
    processed -- so both are waited for on their own rather than one
    being assumed once the other is seen.
    """
    node1 = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node1",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    node2 = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node2",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    node1.start()
    node2.start()

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

    node1.stop()
    node2.stop()


def test_connection_to_ourselves(tmp_path: Path) -> None:
    """A node that dials its own address drops the connection, never adds it.

    `p2p.callbacks.version` recognises its own nonce and stops the
    connection there, before `verack` could ever promote it into
    `connections` -- so `pending_connections` emptying out, not
    `connections` staying at zero, is what proves the drop actually
    happened rather than the connection never having been attempted.
    Two pending connections are waited for first because the loopback
    dial reaches this same node's listener too, and each side of that
    pair has to exist before the drain that follows means anything.
    """
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    node.start()

    wait_until_listening(node.p2p_manager)

    node.p2p_manager.connect(local_addr(node.p2p_port))

    wait_until(lambda: len(node.p2p_manager.pending_connections) == 2)
    # a connection to itself is stopped inside `version`, before its own
    # `verack` could ever promote it: it never reaches `connections`, so
    # `pending_connections` emptying out is what proves it was let go of
    wait_until(lambda: not len(node.p2p_manager.pending_connections))
    assert not node.p2p_manager.connections

    node.stop()
