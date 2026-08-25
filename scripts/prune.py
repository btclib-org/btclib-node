# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.block_db import BlockDB
from btclib_node.chainstate import Chainstate
from btclib_node.config import Config
from btclib_node.log import Logger

config = Config(
    chain="mainnet", data_dir=".btclib", p2p_port=30000, rpc_port=30001, debug=True
)
logger = Logger(config.data_dir / "history.log", debug=config.debug)
blockdb = BlockDB(config.data_dir, logger)
# BlockIndex takes the chainstate's open database, not a directory, so it
# is reached through Chainstate rather than built beside it
chainstate = Chainstate(config.data_dir, config.chain, logger)
blockindex = chainstate.block_index
