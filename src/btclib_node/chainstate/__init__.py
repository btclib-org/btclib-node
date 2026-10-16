# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Chainstate`: the block index, the UTXO set and the compact filter index.

The block index and the UTXO set share the one `db.KeyValueStore`
`Chainstate` opens, told apart by each's own key prefix -- `db.py`'s own
docstring is where that shared store's key order is argued. The filters
have a store of their own (`filter_index.py`'s docstring).
`block_index.BlockIndex` tracks headers and which chain is active,
checking a header's own height- and time-dependent rules over btclib's
`block.next_bits_required` and `median_time_past` before it is indexed;
`utxo_index.UtxoIndex` is the spendable outputs on it, and
`filter_index.FilterIndex` the BIP157/BIP158 filters served over p2p.

`flush` writes the staged changes and `close` calls it before closing
both stores -- `db.py`'s docstring is where the crash this is the other
half of is argued. `filter_index.py`'s docstring is where the filter
store, its locator and the catch-up are.

`wipe` is `-reindex-chainstate`, Core's `InitCoinsDB` with
`should_wipe` (`src/validation.cpp`, `src/node/chainstate.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag). It removes the coins and their
running commitment, and takes every block of the active chain but genesis
off it, all in one batch. The headers stay, and so do
the blocks `block_db` holds: `main.update_chain` then connects them
again from genesis, as Core's `ImportBlocks` does through
`ActivateBestChains`. A stop part-way leaves what the last flush wrote,
a shorter active chain and its coins, so the next start carries on from
there with no marker, as Core's own coins database does.

The wipe leaves the filters: `filter_index.py`'s docstring says why.
"""

from typing import TYPE_CHECKING

from btclib_node.constants import DIR_MODE
from btclib_node.db import KeyValueStore

from .block_index import BlockIndex
from .filter_index import FilterIndex
from .utxo_index import UtxoIndex

__all__ = ["Chainstate"]

if TYPE_CHECKING:
    from pathlib import Path

    from btclib_node.chains import Chain
    from btclib_node.log import Logger


class Chainstate:
    """The block index, the UTXO set and the compact filter index, together.

    The module docstring above is where the three, and the two stores
    they live in, are argued.
    """

    def __init__(
        self,
        data_dir: Path,
        chain: Chain,
        logger: Logger,
        *,
        wipe: bool = False,
    ) -> None:
        """Open the stores under `data_dir` and build all three indexes on them.

        With `wipe`, the chainstate is wiped first, as the module docstring
        says.
        """
        chain_dir = data_dir / "chainstate"
        chain_dir.mkdir(mode=DIR_MODE, exist_ok=True, parents=True)
        self.db = KeyValueStore(chain_dir)
        if wipe:
            # where Core's `CDBWrapper` logs "Wiping LevelDB in <path>"
            # (`src/dbwrapper.cpp`, at bitcoin/bitcoin@9be056a8a7)
            logger.info("Wiping the chainstate in %s", chain_dir)
            with self.db.write_batch() as wb:
                UtxoIndex.wipe(wb)
                BlockIndex.leave_active_chain(wb, chain.genesis.hash)

        indexes_dir = data_dir / "indexes"
        indexes_dir.mkdir(mode=DIR_MODE, exist_ok=True)
        filter_dir = indexes_dir / "blockfilter"
        self.filter_db = KeyValueStore(filter_dir)
        FilterIndex.adopt_legacy(self.db, self.filter_db, logger)

        self.block_index = BlockIndex(self.db, chain, logger)
        self.utxo_index = UtxoIndex(self.db, logger)
        self.filter_index = FilterIndex(self.filter_db, chain, logger)

        self.logger = logger

    def flush(self) -> None:
        """Write the staged changes of every index, the chainstate first.

        `main._finalize_fork` stages a connected or disconnected block's
        own status (`BlockIndex.stage_status`) and its filter
        (`FilterIndex.add_connected_block`) the same way `UtxoIndex`
        already staged its spends and creations, across more than one
        block. The block index and the UTXO set are written in one
        `write_batch`, one commit, which is what keeps a status from ever
        landing on disk ahead of the UTXO set it was validated against;
        `db.py`'s own docstring argues why that has to hold. The filters
        follow, in a batch of the filter store's own; `filter_index.py`'s
        docstring says why second, and `FilterIndex.advance` what happens
        to the locator.
        Pruning flushes first (`main.prune_up_to_height`), so the blocks
        `FilterIndex.catch_up` reads are still stored.
        """
        with self.db.write_batch() as wb:
            self.block_index.finalize(wb)
            self.utxo_index.finalize(wb)
        self.filter_index.advance(self.block_index.active_chain[-1])

    def close(self) -> None:
        """Flush every staged index, then close both stores.

        A clean close loses nothing staged -- the crash this store has
        to survive is one that never reaches this method at all, and
        `db.py`'s docstring is where what that crash costs is decided.
        Safe to call twice: `flush` needs the connection open, so a
        second call skips it and reaches only `KeyValueStore.close`'s
        own no-op on an already-closed store.
        """
        self.logger.info("Closing Chainstate db")
        if not self.db.closed:
            self.flush()
        self.db.close()
        self.filter_db.close()
