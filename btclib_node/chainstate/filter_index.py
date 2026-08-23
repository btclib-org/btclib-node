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
block connects in the same write batch as the chainstate it advances,
and until that batch is written the parent of the block being indexed
is not in the database yet.

BIP157 asks for exactly this: "Nodes SHOULD NOT generate filters
dynamically on request, as malicious peers may be able to perform DoS
attacks by requesting small filters derived from large blocks."
"""

from btclib.block import Block
from btclib.block.block_filter import BasicBlockFilter, prevout_scripts_from_utxos

from btclib_node.block_db import BlockDB, RevBlock
from btclib_node.chains import Chain
from btclib_node.db import KeyValueStore
from btclib_node.log import Logger

_FILTER = b"cfilter-"
_HEADER = b"cfheader-"

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
    def __init__(self, parent_db: KeyValueStore, chain: Chain, logger: Logger) -> None:
        self.db = parent_db
        self.logger = logger

        self.pending: dict[bytes, tuple[bytes, bytes]] = {}

        # no peer serves the genesis block and no `getdata` asks for it,
        # so its filter is built from the chain's own copy, here, rather
        # than by the connect path every other block goes through
        self.genesis_hash = chain.genesis.hash
        if self.get_filter(self.genesis_hash) is None:
            self.add_block(chain.genesis_block, [])
            self.finalize()

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
                raise Exception(err_msg)
        block_filter = BasicBlockFilter.from_block(
            block, prevout_scripts, check_validity=False
        )
        self.pending[block_hash] = (
            block_filter.serialize(check_validity=False),
            block_filter.header(previous_header),
        )

    def add_connected_block(self, block: Block, rev_block: RevBlock) -> None:
        """Index a block from the reverse patch its connection produced.

        `RevBlock.to_add` is the output every input of the block spent,
        which is what a filter needs and a block does not carry.
        """
        self.add_block(block, prevout_scripts_from_utxos(block, dict(rev_block.to_add)))

    def catch_up(self, active_chain: list[bytes], block_db: BlockDB) -> int:
        """Index every block of the active chain that has no filter yet.

        A datadir synced before this index existed has the blocks and
        the reverse patches and none of the filters, and a node that
        answers `getcfilters` for some of its chain is worse than one
        that does not answer at all -- BIP157's service bit is a promise
        about the whole of it. Walked from the bottom so that each
        block's parent is indexed before it is.

        Returns how many it built, which is zero on every start but the
        first after the index appears.
        """
        built = 0
        for block_hash in active_chain:
            if self.get_filter(block_hash) is not None:
                continue
            block = block_db.get_block(block_hash)
            rev_block = block_db.get_rev_block(block_hash)
            if block is None or rev_block is None:
                # nothing else can be built either: the next block's
                # header chains onto this one's, which does not exist
                err_msg = "cannot build the block filter index: no block "
                err_msg += f"or reverse patch stored for {block_hash.hex()}"
                raise Exception(err_msg)
            self.add_connected_block(block, rev_block)
            built += 1
            if len(self.pending) >= _CATCH_UP_BATCH:
                # said as it goes, not at the end: this runs inside
                # Node.__init__, so on a long chain it is the only thing
                # between starting the node and the node appearing hung
                self.logger.info(f"Building block filters: {built} so far")
                self.finalize()
        if built:
            self.logger.info(f"Built {built} missing block filters")
        self.finalize()
        return built

    def finalize(self, wb: KeyValueStore | None = None) -> None:
        """Write what is held, into `wb` if there is one and atomically.

        The header before the filter, and the pair in one write. Both
        skip guards ask `get_filter`, so a filter written without its
        header is the state nothing repairs: the block is skipped for
        ever and its child cannot be indexed at all, which leaves the
        datadir unopenable. The other half of the pair is harmless --
        a header with no filter is simply rebuilt.
        """
        if wb is not None:
            self._write(wb)
            return
        with self.db.write_batch() as batch:
            self._write(batch)

    def _write(self, db: KeyValueStore) -> None:
        for block_hash, (filter_bytes, header) in self.pending.items():
            db.put(_HEADER + block_hash, header)
            db.put(_FILTER + block_hash, filter_bytes)
        self.pending = {}

    def rollback(self) -> None:
        self.pending = {}
