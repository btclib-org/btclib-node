# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`pruneblockchain`, over a real socket, against a real `-prune=1` node.

`main_test.py` and `block_db_test.py` cover the mechanism this RPC calls
into (`main.prune_up_to_height`, `BlockDB.prune_up_to`), and
`rpc/callbacks_test.py` covers the RPC's own argument checks and Core
citations against a node built directly. What this checks is that a
node actually started with manual pruning (`pruned=True`,
`prune_target_mib=None`, matching `-prune=1`) deletes nothing on its
own and answers this RPC over a real connection, the same shape
`chain_test.py`'s own RPC tests use.
"""

from typing import TYPE_CHECKING

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from tests import (
    generate_random_chain,
    get_random_port,
    rpc_client,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_pruneblockchain_deletes_up_to_the_given_height(tmp_path: Path) -> None:
    """A `-prune=1` node deletes nothing on its own; this RPC does, asked to."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
            pruned=True,
            prune_target_mib=None,
        )
    )
    node.start()
    wait_until_listening(node.rpc_manager)

    regtest = RegTest()
    chain = generate_random_chain(regtest.prune_after_height + 5, regtest.genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    node.status = NodeStatus.HeaderSynced
    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    wait_until(lambda: len(block_index.active_chain) == len(chain) + 1)
    assert node.block_db.pruned_up_to == -1

    _, body = rpc_client(node).call_raw(
        "pruneblockchain", [3], jsonrpc="1.0", request_timeout=2
    )

    assert body["result"] == 3
    assert node.block_db.pruned_up_to == 3
    assert node.block_db.get_block(block_index.active_chain[3]) is None
    assert node.block_db.get_block(block_index.active_chain[4]) is not None

    node.stop()


def test_pruneblockchain_refuses_a_node_not_in_prune_mode(tmp_path: Path) -> None:
    """Core's own refusal message, over the wire."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
        )
    )
    node.start()
    wait_until_listening(node.rpc_manager)

    _, body = rpc_client(node).call_raw(
        "pruneblockchain", [3], jsonrpc="1.0", request_timeout=2
    )

    assert "result" not in body
    assert body["error"]["code"] == -1
    assert body["error"]["message"] == (
        "Cannot prune blocks because node is not in prune mode."
    )

    node.stop()
