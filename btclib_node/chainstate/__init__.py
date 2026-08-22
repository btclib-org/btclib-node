# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import plyvel

from .block_index import BlockIndex
from .filter_index import FilterIndex
from .utxo_index import UtxoIndex


class Chainstate:
    def __init__(self, data_dir, chain, logger):
        data_dir = data_dir / "chainstate"
        data_dir.mkdir(exist_ok=True, parents=True)
        self.db = plyvel.DB(str(data_dir), create_if_missing=True)

        self.block_index = BlockIndex(self.db, chain, logger)
        self.utxo_index = UtxoIndex(self.db, logger)
        # after BlockIndex: its init_from_db walks the whole database
        # until the first key that is not a `blkinfo-`, and the keys
        # written here sort after those
        self.filter_index = FilterIndex(self.db, chain, logger)

        self.logger = logger

    def close(self):
        self.logger.info("Closing Chainstate db")
        self.db.close()
