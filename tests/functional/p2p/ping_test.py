# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A `pong` matching its `ping` is recorded, one that does not drops the peer.
"""

import time
from typing import TYPE_CHECKING

from btclib.p2p.keepalive import Ping

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import P2pConnStatus
from tests.helpers import get_random_port, local_addr, wait_until, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path


def test_correct_ping(tmp_path: Path) -> None:
    """A `ping` sent to a real peer comes back as a `pong` that sets latency.

    `ping_nonce` and `ping_sent` are set by hand rather than through
    `send_ping`, so the nonce matched against the peer's `pong` is
    known in advance; `conn.latency` going from unset to a value is
    what `p2p.callbacks.pong` does once it matches that nonce.
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

    node1.stop()
    node2.stop()


def test_wrong_ping(tmp_path: Path) -> None:
    """A `pong` whose nonce does not match the pending `ping` drops the peer.

    `node1` is made to expect nonce `1` (`ping_nonce` set by hand) and
    then sends a `Ping(2)`; node2's `ping` handler echoes `2` back in
    its `pong`, which cannot match what `node1` is holding, so
    `p2p.callbacks.pong` discourages and drops the connection on both
    ends. The check is by connection id and not by an empty
    `connections`, because a peer whose address is already known to the
    other side is redialled the moment the drop lowers the live count,
    which this test is not about.
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

    node1_conn_id = node1.p2p_manager.connections[0].id
    node2_conn_id = node2.p2p_manager.connections[0].id

    node1.p2p_manager.connections[0].ping_sent = time.time()
    node1.p2p_manager.connections[0].ping_nonce = 1
    node1.p2p_manager.send(Ping(2), 0)

    # by id, and not "the manager holds none": with #70 and #71 both
    # working, each side now knows the other's own gossiped address, so
    # manage_connections redials it the moment this drop takes the live
    # count under connection_num -- a peer this connection has nothing to
    # say about (issue #283) and this test is not either, which is only
    # that the connection the wrong nonce was sent on is gone.
    wait_until(lambda: node1_conn_id not in node1.p2p_manager.connections)
    wait_until(lambda: node2_conn_id not in node2.p2p_manager.connections)

    node1.stop()
    node2.stop()
