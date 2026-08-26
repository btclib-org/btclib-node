# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A broadcast transaction reaches a peer's mempool over `inv`/`getdata`."""

from typing import TYPE_CHECKING

import btclib_node.download as download_module
from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus, P2pConnStatus
from tests import (
    generate_random_chain,
    generate_random_transaction,
    get_random_port,
    local_addr,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_send_tx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`broadcast_raw_transaction` gets the tx into a real peer's mempool.

    Both nodes are brought to `BlockSynced` first, since `callbacks.tx`
    drops a transaction arriving before that regardless of what the
    sender's own version claimed. The trickle delay is pinned to zero
    so the announcement is due immediately rather than after a real,
    randomly drawn wait; without it this test would still pass, only
    slower and by however long that draw happened to be.
    """
    # `DownloadManager._send_due_announcements` draws a real, random
    # delay for both nodes' connections the moment each is created
    # (#141), so left undrawn this waits out a mean-2s outbound delay
    # rather than completing as soon as the tx is queued. Pinning the
    # draw to zero keeps every connection's own schedule always due,
    # the same way `tests/unit/download_test.py`'s
    # `test_an_outbound_peers_schedule_draws_from_the_shorter_mean` does.
    monkeypatch.setattr(download_module._rng, "expovariate", lambda lambd: 0.0)
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

    # Add one block
    block = generate_random_chain(1, RegTest().genesis.hash)[0]
    for node in (node1, node2):
        block_index = node.chainstate.block_index
        node.chainstate.block_index.add_headers([block.header])
        node.status = NodeStatus.HeaderSynced
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
        # both lambdas below are safe despite B023: wait_until resolves
        # each one before the loop rebinds block_index/node, see
        # wait_until's own comment
        wait_until(lambda: len(block_index.active_chain) == 2)  # noqa: B023
        # and not merely the chain being connected: callbacks.tx drops
        # a transaction that arrives before this node is block synced,
        # whatever its own version told the peer. btclib-org/btclib-node#129
        wait_until(lambda: node.status == NodeStatus.BlockSynced)  # noqa: B023

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

    tx = generate_random_transaction(block.transactions[0].id)

    assert node1.mempool.size == 0

    # `send_raw_transaction`'s own order (rpc/callbacks.py): the mempool
    # holds it before it is announced, since `broadcast_raw_transaction`
    # goes through the same `inv`/`getdata` round trip a relayed
    # transaction does (#141) and `getdata` serves a `tx` from the
    # mempool, not from what this call was handed.
    node2.mempool.add_tx(tx, 1000)
    node2.p2p_manager.broadcast_raw_transaction(tx, 1000)

    try:
        wait_until(lambda: node1.mempool.size)
    finally:
        node1.stop()
        node2.stop()
        node1.join()
        node2.join()
