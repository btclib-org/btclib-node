# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node import Node
from btclib_node.config import Config

node = Node(
    config=Config(
        chain="mainnet",
        data_dir=".btclib",
        p2p_port=30000,
        rpc_port=30001,
        # a hand toggle, unlike signet.py/testnet.py's live debug=True:
        # mainnet's own log is the one this script leaves quiet by
        # default, meant to be uncommented by whoever is chasing a
        # mainnet-specific problem
        # debug=True,  # noqa: ERA001
        log_path=None,
    )
)
node.start()
