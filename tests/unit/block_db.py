# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.block_db import BlockDB
from btclib_node.chains import RegTest
from btclib_node.log import Logger
from tests.helpers import generate_random_chain


def test_init(tmp_path):
    BlockDB(tmp_path, Logger(debug=True))


def test_blocks(tmp_path):
    chain = generate_random_chain(2000, RegTest().genesis.hash)
    for x in range(10):
        block_db = BlockDB(tmp_path / f"{x}", Logger(debug=True))
        for block in chain:
            block_db.add_block(block)
            stored_block = block_db.get_block(block.header.hash)
            assert stored_block == block
