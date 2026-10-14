# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Chainstate`: the block index, the UTXO set and the compact filter index.

All three share the one `db.KeyValueStore` `Chainstate` opens, told apart
by each's own key prefix -- `db.py`'s own docstring is where that shared
store's key order is argued. `block_index.BlockIndex` tracks headers and
which chain is active, checking a header's own height- and
time-dependent rules over btclib's `block.next_bits_required` and
`median_time_past` before it is indexed; `utxo_index.UtxoIndex` is the
spendable outputs on it, and `filter_index.FilterIndex` the BIP157/BIP158
filters served over p2p.

`flush` is what writes all three indexes' own staged changes in one
batch, and `close` calls it before closing the store -- `db.py`'s
docstring is where the crash this is the other half of is argued.

`wipe` is `-reindex-chainstate`, Core's `InitCoinsDB` with
`should_wipe` (`src/validation.cpp`, `src/node/chainstate.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag). It removes the coins, their
running commitment and the filters, and takes every block of the active
chain but genesis off it, all in one batch. The headers stay, and so do
the blocks `block_db` holds: `main.update_chain` then connects them
again from genesis, as Core's `ImportBlocks` does through
`ActivateBestChains`. A stop part-way leaves what the last flush wrote,
a shorter active chain and its coins, so the next start carries on from
there with no marker, as Core's own coins database does.

Wiping the filters departs from Core, which keeps its block filter
index, a database of its own, and wipes it only on `-reindex`:
SECURITY.md's *Where this node departs from Bitcoin Core* records it.
Here the filters share the store the rebuild is recovering, and the
reconnect builds each one again from the block and the undo data it
computes anyway. `FilterIndex.add_block` and `catch_up` skip a block
that already has a filter, so without the wipe a corrupt filter in the
shared store would survive the rebuild, and there is no way here to
rebuild the filters alone. The departure lasts until
btclib-org/btclib-node#1940 gives the filters a store of their own,
after which `-reindex-chainstate` keeps them as Core does.
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

    The module docstring above is where the three, and the one
    `KeyValueStore` they share, are argued.
    """

    def __init__(
        self, data_dir: Path, chain: Chain, logger: Logger, *, wipe: bool = False
    ) -> None:
        """Open the store under `data_dir` and build all three indexes on it.

        With `wipe`, the store is wiped first, as the module docstring says.
        """
        data_dir = data_dir / "chainstate"
        data_dir.mkdir(mode=DIR_MODE, exist_ok=True, parents=True)
        self.db = KeyValueStore(data_dir)
        if wipe:
            # where Core's `CDBWrapper` logs "Wiping LevelDB in <path>"
            # (`src/dbwrapper.cpp`, at bitcoin/bitcoin@9be056a8a7)
            logger.info("Wiping the chainstate in %s", data_dir)
            with self.db.write_batch() as wb:
                UtxoIndex.wipe(wb)
                FilterIndex.wipe(wb)
                BlockIndex.leave_active_chain(wb, chain.genesis.hash)

        self.block_index = BlockIndex(self.db, chain, logger)
        self.utxo_index = UtxoIndex(self.db, logger)
        # `BlockIndex.init_from_db` walks the database until the first
        # key that is not a `blkinfo-`, and `cfilter-`/`cfheader-` sort
        # after those -- which is what makes the order these three are
        # built in not matter, rather than what makes this one right
        self.filter_index = FilterIndex(self.db, chain, logger)

        self.logger = logger

    def flush(self) -> None:
        """Write every index's own staged changes, in one atomic batch.

        `main._finalize_fork` stages a connected or disconnected block's
        own status (`BlockIndex.stage_status`) and its filter
        (`FilterIndex.add_connected_block`) the same way `UtxoIndex`
        already staged its spends and creations, across more than one
        block; writing the three together here -- one `write_batch`, one
        commit -- is what keeps a status or a filter from ever landing
        on disk ahead of the UTXO set it was validated against. `db.py`'s
        own docstring argues why that has to hold.
        """
        with self.db.write_batch() as wb:
            self.block_index.finalize(wb)
            self.utxo_index.finalize(wb)
            self.filter_index.finalize(wb)

    def close(self) -> None:
        """Flush every staged index, then close the shared store.

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
