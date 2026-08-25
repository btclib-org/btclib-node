# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.


from typing import TYPE_CHECKING

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import P2pConnStatus
from tests.helpers import get_random_port, local_addr, wait_until, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path


def test_simple_connection(tmp_path: Path) -> None:
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

    wait_until(lambda: len(node.p2p_manager.nonces) == 2)
    # a connection to itself is stopped inside `version`, before its own
    # `verack` could ever promote it: it never reaches `connections`, so
    # `pending_connections` emptying out is what proves it was let go of
    wait_until(lambda: not len(node.p2p_manager.pending_connections))
    assert not node.p2p_manager.connections

    node.stop()
