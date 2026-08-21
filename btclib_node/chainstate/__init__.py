# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import plyvel

from .block_index import BlockIndex
from .utxo_index import UtxoIndex


class Chainstate:
    def __init__(self, data_dir, chain, logger):
        data_dir = data_dir / "chainstate"
        data_dir.mkdir(exist_ok=True, parents=True)
        self.db = plyvel.DB(str(data_dir), create_if_missing=True)

        self.block_index = BlockIndex(self.db, chain, logger)
        self.utxo_index = UtxoIndex(self.db, logger)

        self.logger = logger

    def close(self):
        self.logger.info("Closing Chainstate db")
        self.db.close()
