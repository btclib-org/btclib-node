# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node import Node
from btclib_node.config import Config

node = Node(
    config=Config(
        chain="signet",
        data_dir=".btclib",
        p2p_port=30000,
        rpc_port=30001,
        debug=True,
        log_path=None,
    )
)
node.start()
