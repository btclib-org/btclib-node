# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The BIP158 filter of every connected block, and its filter header.

What a node holds that `btclib.block.block_filter` does not: the
arithmetic over one block is btclib's, and this is the index over the
chain -- one filter per block, and the header chaining it onto its
parent's.

A filter is keyed by block hash and so is its header, both being
functions of the block and of its ancestry: a header is
`filter_header(this filter's hash, the parent's header)` and the parent
is fixed by `previous_block_hash`, not by which chain is active. So a
block stepped over in a reorg keeps the filter and the header it had,
and coming back costs nothing -- which is why there is no counterpart
here to `UtxoIndex.apply_rev_block`.

Writes are held until `finalize`, the way `UtxoIndex` holds them: a
block connects across more than one trial before `Chainstate.flush`
writes anything, and until then the parent of the block being indexed
is not in the database yet.

The filters are kept in a `KeyValueStore` of their own, `indexes/blockfilter`
under the chain's directory, as Core keeps its block filter index in
`indexes/blockfilter/` (`src/index/blockfilterindex.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag). The store holds a best-block
locator, Core's `DB_BEST_BLOCK` (`BaseIndex::DB::ReadBestBlock` and
`WriteBestBlock`, `src/index/base.cpp`, same commit): the index's own
progress, a block whose ancestors all have their filters stored. It is
not the chainstate's tip. It moves in the batch of the filters that make
it true: in `catch_up`, after each batch it writes, and in
`Chainstate.flush` only once `catch_up` has finished at start. So a flush
on the way out of a failed start leaves it where the index had got to.

`Chainstate.flush` writes the filters after the chainstate, so a crash
between the two leaves the locator behind the active tip and never ahead
of a filter. `catch_up` closes the gap at start, from the fork point of
the locator and the active chain, as `BaseIndex::Sync` does in Core. A
reorg needs no rewind, the filters being keyed by block hash.

`catch_up` builds the active chain alone, as `BaseIndex::Sync` does. So
a block off it has no filter where a crash between the two writes lost
it, or where the store was deleted, and a peer asking for it is
answered with nothing, as in Core (`p2p.callbacks._log_missing_filters`).

`-reindex-chainstate` wipes the chainstate and keeps this store, as Core
does (`src/init.cpp`, same commit). Core wipes it on `-reindex`, which
this node lacks (btclib-org/btclib-node#1415). A corrupt filter is
rebuilt by deleting the directory. A datadir from before the store
existed keeps its filters in the chainstate's store: `adopt_legacy`
moves them.

BIP157 asks for exactly this: "Nodes SHOULD NOT generate filters
dynamically on request, as malicious peers may be able to perform DoS
attacks by requesting small filters derived from large blocks."
"""

from typing import TYPE_CHECKING

from btclib.block.block_filter import BasicBlockFilter, prevout_scripts_from_utxos

from btclib_node.exceptions import ChainstateInconsistencyError
from btclib_node.stored_block import read_held_block

if TYPE_CHECKING:
    from btclib.block import Block

    from btclib_node.block_db import BlockDB, RevBlock
    from btclib_node.chains import Chain
    from btclib_node.chainstate.block_index import BlockIndex
    from btclib_node.db import KeyValueStore
    from btclib_node.log import Logger

__all__ = ["NO_PREVIOUS_FILTER_HEADER", "FilterIndex"]

_FILTER = b"cfilter-"
_HEADER = b"cfheader-"
# in the store's meta column family, away from the filters' key order
_BEST_BLOCK = b"best-block"

# BIP157: "The previous filter header used to calculate that of the
# genesis block is defined to be the 32-byte array of 0's."
NO_PREVIOUS_FILTER_HEADER = b"\x00" * 32

# how many blocks a catch-up builds before writing them. Without it the
# whole index is held in memory before a byte reaches the disk, and a
# basic filter costs about two and a half octets per element -- a chain
# is gigabytes. Each flush is one atomic write, so a catch-up
# interrupted resumes from the last of them rather than from nothing.
_CATCH_UP_BATCH = 500


class FilterIndex:
    """The BIP157/BIP158 filter and header for every connected block.

    The module docstring above is where the keying, the store, the
    locator, the write-batch discipline and the lack of a reorg-time undo
    are all argued. `best_block` is the locator as last written.

    `pending` now survives more than one trial of `main.update_chain`,
    the same way `UtxoIndex`'s own staging does, so `rollback` cannot
    stay a blanket wipe either: `add_block` only ever adds a hash that
    was not already a key (its own guard, checking `get_filter` first),
    so `_trial_log` needs to remember only which hashes a trial added,
    not what they replace -- `UtxoIndex`'s own `_undo_log` carries a
    prior value for exactly the mutations here that never happen.
    """

    def __init__(self, db: KeyValueStore, chain: Chain, logger: Logger) -> None:
        """Read the locator, and build the genesis filter if absent."""
        self.db = db
        self.chain = chain
        self.logger = logger
        self.best_block: bytes | None = db.get_meta(_BEST_BLOCK)

        self.pending: dict[bytes, tuple[bytes, bytes]] = {}
        self._trial_log: list[bytes] = []
        # set by `catch_up`; until then `advance` leaves the locator alone
        self.synced = False

        # genesis never goes through the connect path every other block
        # does, so its filter is built from the chain's own copy, here
        self.genesis_hash = chain.genesis.hash
        if self.get_filter(self.genesis_hash) is None:
            self.add_block(chain.genesis_block, [])
            self.finalize(best_block=self.best_block or self.genesis_hash)

    @staticmethod
    def adopt_legacy(legacy: KeyValueStore, db: KeyValueStore, logger: Logger) -> int:
        """Move the filters a chainstate store holds into `db`, if it holds any.

        A datadir from before the filters had a store of their own keeps
        them in the chainstate's, under the same keys. They are copied,
        then removed there, so a stop in between copies them again on the
        next start, and one after leaves nothing to do. A header with no
        filter is dropped, as is a filter with no header, which
        `finalize` never wrote. The locator is left to `catch_up`, which
        starts from genesis and finds every filter in place.

        Returns how many filters it moved.
        """
        if not FilterIndex._has_legacy_filters(legacy):
            return 0
        logger.info("Moving the block filters to their own store in %s", db.path)
        moved = 0
        batch: list[tuple[bytes, bytes, bytes]] = []
        for key, filter_bytes in legacy.scan_prefix(_FILTER):
            block_hash = key[len(_FILTER) :]
            header = legacy.get(_HEADER + block_hash)
            if header is None:
                continue
            batch.append((block_hash, filter_bytes, header))
            moved += 1
            if len(batch) >= _CATCH_UP_BATCH:
                FilterIndex._copy(db, batch)
                batch = []
        FilterIndex._copy(db, batch)
        with legacy.write_batch() as wb:
            wb.delete_prefix(_FILTER)
            wb.delete_prefix(_HEADER)
        logger.info("Moved %s block filters", moved)
        return moved

    @staticmethod
    def _has_legacy_filters(legacy: KeyValueStore) -> bool:
        # the iterator holds the store's directory lock until dropped, as
        # it is, with the call
        return any(
            next(legacy.scan_prefix(prefix), None) is not None
            for prefix in (_FILTER, _HEADER)
        )

    @staticmethod
    def _copy(db: KeyValueStore, batch: list[tuple[bytes, bytes, bytes]]) -> None:
        with db.write_batch() as wb:
            for block_hash, filter_bytes, header in batch:
                wb.put(_HEADER + block_hash, header)
                wb.put(_FILTER + block_hash, filter_bytes)

    def get_filter(self, block_hash: bytes) -> bytes | None:
        """Return the serialized filter of a block, or None."""
        if block_hash in self.pending:
            return self.pending[block_hash][0]
        return self.db.get(_FILTER + block_hash)

    def get_header(self, block_hash: bytes) -> bytes | None:
        """Return the filter header of a block, or None."""
        if block_hash in self.pending:
            return self.pending[block_hash][1]
        return self.db.get(_HEADER + block_hash)

    def get_filter_hash(self, block_hash: bytes) -> bytes | None:
        """Return the filter hash of a block, in display order, or None."""
        filter_bytes = self.get_filter(block_hash)
        if filter_bytes is None:
            return None
        # round-tripped through the filter rather than hashed here: the
        # hash is btclib's definition of what a `cfheaders` carries, and
        # a second spelling of it would be a second thing to keep right
        return BasicBlockFilter.parse(
            filter_bytes, block_hash, check_validity=False
        ).hash

    def add_block(self, block: Block, prevout_scripts: list[bytes]) -> None:
        """Index the filter of a block whose parent is already indexed."""
        block_hash = block.header.hash
        if self.get_filter(block_hash) is not None:
            return
        previous_header: bytes | None
        if block_hash == self.genesis_hash:
            previous_header = NO_PREVIOUS_FILTER_HEADER
        else:
            previous_header = self.get_header(block.header.previous_block_hash)
            if previous_header is None:
                err_msg = "no filter header for the parent of "
                err_msg += block_hash.hex()
                raise ChainstateInconsistencyError(err_msg)
        block_filter = BasicBlockFilter.from_block(
            block, prevout_scripts, check_validity=False
        )
        self.pending[block_hash] = (
            block_filter.serialize(check_validity=False),
            block_filter.header(previous_header),
        )
        self._trial_log.append(block_hash)

    def add_connected_block(self, block: Block, rev_block: RevBlock) -> None:
        """Index a block from the reverse patch its connection produced.

        `RevBlock.to_add` is the output every input of the block spent,
        which is what a filter needs and a block does not carry --
        `prevout_scripts_from_utxos` wants a bare `TxOut` per outpoint,
        one step short of what `RevBlock.to_add` now carries alongside
        it (`Coin`'s own docstring, `block_db/__init__.py`), so it is
        unwrapped here rather than asking that function to know about a
        type outside its own package.
        """
        utxos = {out_point: coin.tx_out for out_point, coin in rev_block.to_add}
        self.add_block(block, prevout_scripts_from_utxos(block, utxos))

    def catch_up(self, block_index: BlockIndex, block_db: BlockDB) -> int:
        """Index every block of the active chain the locator does not cover.

        Core's `BaseIndex::Sync`: from the block after the fork point of
        the locator and the active tip, up to the tip. The locator is
        behind after a stop between `Chainstate.flush`'s two writes, and
        on its fork's far side after a reorg that stop interrupted. A
        block whose filter is stored is skipped, so a datadir that has
        none, the filters having just been moved into this store or
        deleted, is built from genesis. A node that answers `getcfilters`
        for some of its chain is worse than one that does not answer at
        all: BIP157's service bit is a promise about the whole of it.
        Walked from the bottom so that each block's parent is indexed
        before it is.

        Returns how many it built, which is zero on every start but the
        first after the index appears, or after a stop it had to repair.
        """
        active_chain = block_index.active_chain
        built = 0
        for height in range(self._first_uncovered(block_index), len(active_chain)):
            block_hash = active_chain[height]
            if self.get_filter(block_hash) is not None:
                continue
            # Core's `BaseIndex::ProcessBlock` reads it (`read_held_block`)
            block = read_held_block(block_db, self.chain, block_hash)
            rev_block = block_db.get_rev_block(block_hash)
            if block is None or rev_block is None:
                # nothing else can be built either: the next block's
                # header chains onto this one's, which does not exist
                err_msg = "cannot build the block filter index: no block "
                err_msg += f"or reverse patch stored for {block_hash.hex()}"
                raise ChainstateInconsistencyError(err_msg)
            self.add_connected_block(block, rev_block)
            built += 1
            if len(self.pending) >= _CATCH_UP_BATCH:
                # said as it goes, not at the end: this runs inside
                # Node.load, so on a long chain it is the only thing
                # between starting the node and the node appearing hung
                self.logger.info("Building block filters: %s so far", built)
                self.finalize(best_block=block_hash)
        if built:
            self.logger.info("Built %s missing block filters", built)
        self.finalize(best_block=active_chain[-1])
        self.synced = True
        return built

    def advance(self, tip: bytes) -> None:
        """Write what is held, and move the locator to `tip` once synced."""
        self.finalize(best_block=tip if self.synced else None)

    def _first_uncovered(self, block_index: BlockIndex) -> int:
        """Return the height after the fork point of the locator and the tip."""
        if self.best_block is None:
            return 0
        if self.best_block not in block_index.header_dict:
            # Core's `BaseIndex::Init` refuses the same way
            err_msg = "the best block of the block filter index is not in the "
            err_msg += "block index; delete indexes/blockfilter to rebuild it"
            raise ChainstateInconsistencyError(err_msg)
        fork = block_index.last_common_ancestor(
            self.best_block, block_index.active_chain[-1]
        )
        return block_index.header_dict[fork].index + 1

    def finalize(self, best_block: bytes | None = None) -> None:
        """Write what is held, and the locator if there is one, atomically.

        The header before the filter, and the pair in one write. Both
        skip guards ask `get_filter`, so a filter written without its
        header is the state nothing repairs: the block is skipped for
        ever and its child cannot be indexed at all, which leaves the
        datadir unopenable. The other half of the pair is harmless --
        a header with no filter is simply rebuilt.

        `best_block` moves the locator in the same batch, which is only
        right where every ancestor of it has its filter stored or held.
        """
        with self.db.write_batch() as batch:
            for block_hash, (filter_bytes, header) in self.pending.items():
                batch.put(_HEADER + block_hash, header)
                batch.put(_FILTER + block_hash, filter_bytes)
            if best_block is not None:
                batch.put_meta(_BEST_BLOCK, best_block)
        if best_block is not None:
            self.best_block = best_block
        self.pending = {}
        # everything just written is durable, so nothing recorded before
        # now can ever be rolled back to -- UtxoIndex.finalize's own
        # docstring argues the same clearing
        self._trial_log = []

    def trial_mark(self) -> int:
        """Return a point in the trial log a failed trial can be rolled back to.

        `UtxoIndex.trial_mark`'s own docstring is where the reason this
        exists at all -- `pending` surviving more than one trial -- is
        argued; `main.update_chain` is the one caller.
        """
        return len(self._trial_log)

    def rollback(self, mark: int = 0) -> None:
        """Discard whatever `pending` gained since `mark`.

        `mark` defaults to the very start, which is every direct test
        of this method: a fresh index, nothing pending before its own
        trial. `UtxoIndex.rollback`'s own docstring is where a caller
        with something to protect gets a real one from.
        """
        while len(self._trial_log) > mark:
            del self.pending[self._trial_log.pop()]
