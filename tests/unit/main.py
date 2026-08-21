# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import pytest

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.main import update_chain, verify_mempool_acceptance
from tests.helpers import generate_random_chain, generate_random_transaction


def test_chain(tmp_path):
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            debug=True,
        )
    )
    node.status = NodeStatus.HeaderSynced
    length = 2000 * 1  # 2000
    chain = generate_random_chain(length, RegTest().genesis.hash)
    headers = [block.header for block in chain]
    block_index = node.chainstate.block_index
    for x in range(0, length, 2000):
        block_index.add_headers(headers[x : x + 2000])
    for x in block_index.header_dict:
        block_info = block_index.get_block_info(x)
        block_info.downloaded = True
        block_index.insert_block_info(block_info)
    for block in chain:
        node.block_db.add_block(block)
    for x in range(len(chain)):
        update_chain(node)
    assert len(block_index.active_chain) == length + 1


def test_add_tx(tmp_path):
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            allow_rpc=False,
            debug=True,
        )
    )
    node.status = NodeStatus.HeaderSynced
    chain = generate_random_chain(10, RegTest().genesis.hash)
    headers = [block.header for block in chain]
    block_index = node.chainstate.block_index
    block_index.add_headers(headers)
    for x in block_index.header_dict:
        block_info = block_index.get_block_info(x)
        block_info.downloaded = True
        block_index.insert_block_info(block_info)
    for block in chain:
        node.block_db.add_block(block)
    for x in range(len(chain)):
        update_chain(node)

    with pytest.raises(MissingPrevoutError):
        invalid_tx = generate_random_transaction()
        verify_mempool_acceptance(node, invalid_tx)

    tx1 = generate_random_transaction(chain[-1].transactions[0].id)
    tx2 = generate_random_transaction(tx1.id)

    verify_mempool_acceptance(node, tx1)

    # We can't find the prevouts
    with pytest.raises(MissingPrevoutError):
        verify_mempool_acceptance(node, tx2)

    # tx1 needs to be added to the mempool
    node.mempool.add_tx(tx1)
    verify_mempool_acceptance(node, tx2)
