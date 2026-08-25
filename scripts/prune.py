# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Build mainnet's block store and index, for pruning by hand.

Opens `BlockDB` and `Chainstate` (and, through it, `BlockIndex`) against
a mainnet data directory and stops: neither class has a `prune` method
of its own, so this is meant to be run under `python -i` and whatever
deletion a session needs is typed at the resulting prompt against
`blockdb` and `blockindex`.
"""

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
