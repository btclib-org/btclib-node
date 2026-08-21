# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from pathlib import Path

from btclib_node import BlockIndex, Logger
from btclib_node.chains import TestNet

data_dir = Path(".btclib/testnet")
logger = Logger(data_dir / "log")
index = BlockIndex(data_dir, TestNet(), logger)
