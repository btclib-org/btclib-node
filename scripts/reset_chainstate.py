# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.chainstate import Chainstate
from btclib_node.config import Config
from btclib_node.log import Logger

config = Config(
    chain="mainnet", data_dir=".btclib", p2p_port=30000, rpc_port=30001, debug=True
)
logger = Logger(config.data_dir / "history.log", config.debug)
chainstate = Chainstate(config.data_dir, config.chain, logger)
blockindex = chainstate.block_index

# for block_hash in blockindex.active_chain[1:]:
#     block_info = blockindex.get_block_info(block_hash)
#     block_info.status = BlockStatus.valid_header
#     blockindex.insert_block_info(block_info)

# with chainstate.db.write_batch():
#     for key, _ in chainstate.db:
#         if key.startswith(b"utxo-"):
#             chainstate.db.delete(key)
