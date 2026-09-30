# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Build mainnet's `Node` against a real data directory, for pruning by hand.

Opens every database under the data directory and stops: run under
`python -i` and call `main.prune_up_to_height(node, height)` at the
resulting prompt with whatever height a session needs, repeating it as
often as it likes -- the RPC this backs, `pruneblockchain`
(`rpc/callbacks.py`), takes one call the same way.
"""

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.main import prune_up_to_height  # noqa: F401 -- for the prompt

config = Config(
    chain="mainnet",
    data_dir=".btclib",
    allow_p2p=False,
    allow_rpc=False,
    debug=True,
)
node = Node(config)
node.load()
