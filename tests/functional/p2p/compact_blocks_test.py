# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A block passes between two of these nodes as a `cmpctblock`.

The receiver holds one of the block's transactions in its mempool and
lacks the other, so the block goes as a `cmpctblock` and a `blocktxn`.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from btclib.tx.limits import COINBASE_MATURITY

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus, P2pConnStatus
from tests import (
    build_block,
    generate_coinbase,
    generate_random_chain,
    generate_random_transaction,
    get_random_port,
    local_addr,
    rpc_client,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path


def a_node(data_dir: Path) -> Node:
    """Return a started regtest node answering RPC."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=data_dir,
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
        )
    )
    node.start()
    return node


def test_a_block_is_received_as_a_cmpctblock_and_a_blocktxn(tmp_path: Path) -> None:
    """The receiver asks `MSG_CMPCT_BLOCK`, then the transaction it lacks."""
    receiver = a_node(tmp_path / "receiver")
    sender = a_node(tmp_path / "sender")
    try:
        chain = generate_random_chain(
            COINBASE_MATURITY, RegTest().genesis.hash, tip_time=datetime.now(UTC)
        )
        for node in (receiver, sender):
            wait_until_listening(node.p2p_manager)
            wait_until_listening(node.rpc_manager)
            block_index = node.chainstate.block_index
            block_index.add_headers([block.header for block in chain])
            node.status = NodeStatus.HeaderSynced
            for block in chain:
                node.block_db.add_block(block)
                block_index.set_downloaded(block.header.hash)
            wait_until(lambda: len(block_index.active_chain) == len(chain) + 1)  # noqa: B023
            wait_until(lambda: node.is_initial_block_download is False)  # noqa: B023

        receiver.p2p_manager.connect(local_addr(sender.p2p_port))
        wait_until(lambda: len(receiver.p2p_manager.connections))
        (conn,) = receiver.p2p_manager.connections.values()
        wait_until(lambda: conn.status == P2pConnStatus.Connected)
        wait_until(lambda: conn.provides_cmpctblocks)

        funding = chain[0].transactions[0]
        held = generate_random_transaction(funding.id, funding.vout[0].value - 1_000)
        missing = generate_random_transaction(held.id, held.vout[0].value - 1_000)
        receiver.mempool.add_tx(held, 1_000)
        height = len(chain) + 1
        block = build_block(
            chain[-1].header.hash,
            [generate_coinbase(height=height), held, missing],
            height,
            datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1),
        )
        submitted = rpc_client(sender).call(
            "submitblock", [block.serialize(check_validity=False).hex()]
        )
        assert submitted is None

        receiver_chain = receiver.chainstate.block_index.active_chain
        wait_until(lambda: receiver_chain[-1] == block.header.hash)
        received = conn.stats.bytes_recv_per_msg
        sent = conn.stats.bytes_sent_per_msg
        assert received["cmpctblock"]
        assert received["blocktxn"]
        assert sent["getblocktxn"]
        assert not received["block"]
    finally:
        for node in (receiver, sender):
            node.stop()
            node.join()
