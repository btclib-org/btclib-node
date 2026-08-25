# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A hand-edited template for repairing a mainnet chainstate in place.

Opens the chainstate and leaves two commented-out blocks below --
resetting a range of the active chain back to `valid_header` status, and
deleting every `utxo-` entry -- for whoever is repairing a corrupted
chainstate to uncomment and run, one block at a time, rather than a
program meant to run as it stands.
"""

from btclib_node.chainstate import Chainstate
from btclib_node.config import Config
from btclib_node.log import Logger

config = Config(
    chain="mainnet", data_dir=".btclib", p2p_port=30000, rpc_port=30001, debug=True
)
logger = Logger(config.data_dir / "history.log", debug=config.debug)
chainstate = Chainstate(config.data_dir, config.chain, logger)
blockindex = chainstate.block_index

# Nothing below runs on its own: uncomment whichever reset this run
# needs before invoking the script by hand. Kept current against the
# index's own API across refactors (most recently issue #117) rather
# than deleted, so per-file-ignores below turns ERA001 off for this
# file rather than a noqa per block.

# for block_hash in blockindex.active_chain[1:]:
#     blockindex.set_status(block_hash, BlockStatus.valid_header)

# with chainstate.db.write_batch():
#     for key, _ in chainstate.db:
#         if key.startswith(b"utxo-"):
#             chainstate.db.delete(key)
