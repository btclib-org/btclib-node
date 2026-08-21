# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from pathlib import Path

from btclib_node.chains import TestNet
from btclib_node.chainstate import Chainstate
from btclib_node.log import Logger

data_dir = Path(".btclib/testnet")
logger = Logger(data_dir / "log")
# BlockIndex takes the chainstate's open database, not a directory, so it
# is reached through Chainstate rather than built beside it
chainstate = Chainstate(data_dir, TestNet(), logger)
index = chainstate.block_index
