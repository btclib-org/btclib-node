# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Run a mainnet node against `.btclib`, logging to the console.

Meant to be run directly (`python scripts/chains/mainnet.py`) rather
than imported: `log_path=None` makes `Config` route the log to a stream
instead of a file, and mainnet's own `debug` line stays commented out
by default, left there for whoever is chasing a mainnet-specific
problem to uncomment.

The body sits under `if __name__ == "__main__":` because
`Node.worker_pool` is a `multiprocessing.Pool`, and every start method
other than `fork` re-imports `__main__` in each worker
(`multiprocessing/spawn.py`'s own `import_main_path`) -- unguarded, each
worker would build and start a second `Node` on this same data
directory (issue #579).
"""

from btclib_node import Node, install_signal_handlers
from btclib_node.config import Config

if __name__ == "__main__":
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
    # this script is the process: an operator's Ctrl-C or a kill is meant
    # to reach this node, which is what a library object never assumes for
    # itself (issue #436)
    install_signal_handlers(node)
    node.start()
