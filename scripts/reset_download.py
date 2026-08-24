# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.block_db import BlockDB
from btclib_node.chainstate import Chainstate
from btclib_node.config import Config
from btclib_node.log import Logger

config = Config(
    chain="mainnet",
    data_dir=".btclib",
)
logger = Logger(debug=True)
blockdb = BlockDB(config.data_dir, logger)
chainstate = Chainstate(config.data_dir, config.chain, logger)
blockindex = chainstate.block_index

# first index to reset
fix_idx = 402822

# The check below runs first, by hand, before the reset that follows
# it: both stay commented until invoked, and both are kept current
# against the index's own API across refactors (most recently issue
# #117) rather than deleted -- see reset_chainstate.py and the
# per-file-ignores entry both share.

# for block_hash in blockindex.header_index[fix_idx:]:
#     block_info = blockindex.get_block_info(block_hash)
#     if block_info.status != BlockStatus.valid_header:
#         print("Error, invalid reset parameters")
#         exit()

# for block_hash in blockindex.header_index[fix_idx:]:
#     blockindex.set_downloaded(block_hash, downloaded=False)
#     blockdb.db.delete(b"b" + block_hash)
