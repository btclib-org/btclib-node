# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import time

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.messages.ping import Ping
from tests.helpers import get_random_port, local_addr, wait_until


def test_correct_ping(tmp_path):
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

    wait_until(lambda: node1.p2p_manager.is_alive())
    wait_until(lambda: node2.p2p_manager.is_alive())

    node2.p2p_manager.connect(local_addr(node1.p2p_port))
    wait_until(lambda: len(node1.p2p_manager.connections))
    conn = node1.p2p_manager.connections[0]
    wait_until(lambda: conn.status == P2pConnStatus.Connected)
    conn = node2.p2p_manager.connections[0]
    wait_until(lambda: conn.status == P2pConnStatus.Connected)

    conn = node1.p2p_manager.connections[0]
    # wait until the previous ping is cleared
    wait_until(lambda: conn.ping_nonce == 0)

    conn.ping_sent = time.time()
    conn.ping_nonce = 1
    conn.send(Ping(1))
    wait_until(lambda: conn.latency)

    node1.stop()
    node2.stop()


def test_wrong_ping(tmp_path):
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

    wait_until(lambda: node1.p2p_manager.is_alive())
    wait_until(lambda: node2.p2p_manager.is_alive())

    node2.p2p_manager.connect(local_addr(node1.p2p_port))
    wait_until(lambda: len(node1.p2p_manager.connections))
    connection = node1.p2p_manager.connections[0]
    wait_until(lambda: connection.status == P2pConnStatus.Connected)
    connection = node2.p2p_manager.connections[0]
    wait_until(lambda: connection.status == P2pConnStatus.Connected)

    node1.p2p_manager.connections[0].ping_sent = time.time()
    node1.p2p_manager.connections[0].ping_nonce = 1
    node1.p2p_manager.send(Ping(2), 0)

    wait_until(lambda: not len(node1.p2p_manager.connections))
    wait_until(lambda: not len(node2.p2p_manager.connections))

    node1.stop()
    node2.stop()
