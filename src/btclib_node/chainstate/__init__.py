# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Chainstate`: the block index, the UTXO set and the compact filter index.

All three share the one `db.KeyValueStore` `Chainstate` opens, told apart
by each's own key prefix -- `db.py`'s own docstring is where that shared
store's key order is argued. `block_index.BlockIndex` tracks headers and
which chain is active, `utxo_index.UtxoIndex` the spendable outputs on
it, and `filter_index.FilterIndex` the BIP157/BIP158 filters served over
p2p; `contextual.py` is the height- and time-dependent validation the
first of those calls before extending the active chain.
"""

from typing import TYPE_CHECKING

from btclib_node.db import KeyValueStore

from .block_index import BlockIndex
from .filter_index import FilterIndex
from .utxo_index import UtxoIndex

if TYPE_CHECKING:
    from pathlib import Path

    from btclib_node.chains import Chain
    from btclib_node.log import Logger


class Chainstate:
    """The block index, the UTXO set and the compact filter index, together.

    The module docstring above is where the three, and the one
    `KeyValueStore` they share, are argued.
    """

    def __init__(self, data_dir: Path, chain: Chain, logger: Logger) -> None:
        """Open the store under `data_dir` and build all three indexes on it."""
        data_dir = data_dir / "chainstate"
        data_dir.mkdir(exist_ok=True, parents=True)
        self.db = KeyValueStore(data_dir)

        self.block_index = BlockIndex(self.db, chain, logger)
        self.utxo_index = UtxoIndex(self.db, logger)
        # `BlockIndex.init_from_db` walks the database until the first
        # key that is not a `blkinfo-`, and `cfilter-`/`cfheader-` sort
        # after those -- which is what makes the order these three are
        # built in not matter, rather than what makes this one right
        self.filter_index = FilterIndex(self.db, chain, logger)

        self.logger = logger

    def close(self) -> None:
        """Close the shared store, for all three indexes at once."""
        self.logger.info("Closing Chainstate db")
        self.db.close()
