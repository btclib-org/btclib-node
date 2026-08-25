# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Run a testnet node against `.btclib`, logging to the console.

Meant to be run directly (`python scripts/chains/testnet.py`) rather
than imported: `log_path=None` makes `Config` route the log to a stream
instead of a file, and `debug=True` keeps it live for this chain, the
same as `signet.py`'s own.
"""

from btclib_node import Node
from btclib_node.config import Config

node = Node(
    config=Config(
        chain="testnet",
        data_dir=".btclib",
        p2p_port=30000,
        rpc_port=30001,
        debug=True,
        log_path=None,
    )
)
node.start()
