# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`BlockDB`: blocks and their undo data on disk.

Blocks and reverse patches (`RevBlock`) are appended to flat, rotating
files under `data_dir`; `BlockLocation` and `FileMetadata`, kept in the
key-value store, are what let a later read seek straight to a block
instead of scanning a file for it. A reverse patch is filed against its
own block only once that block's branch connects, `pending_rev_blocks`
holding one generated for a branch `update_chain` may still refuse.

`prune_up_to` is this store's own half of pruning
(btclib-org/btclib-node#601): it deletes a block and its reverse patch,
by height rather than by file, because this store's own rotation
(`__find_block_file`) tracks append order and not a file's own height
range the way Core's `FlatFilePos`/`nHeightFirst`/`nHeightLast`
(`node/blockstorage.h`, at bitcoin/bitcoin@ca7162cde5) does. `live` is
what lets a `.blk`/`.rev` file still be reclaimed once every block it
ever held has been pruned this way -- `_release`'s own docstring is
where that is argued end to end, the uncomfortable half (a syncing
node's own append order tracking height only approximately) included.
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from btclib import var_int
from btclib.block import Block
from btclib.tx.coin import Coin as _BtclibCoin
from btclib.tx.out_point import OutPoint
from btclib.tx.tx_out import TxOut
from btclib.utils import bytesio_from_binarydata

from btclib_node.db import KeyValueStore
from btclib_node.exceptions import ChainstateInconsistencyError

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib.alias import BinaryData

    from btclib_node.log import Logger

__all__ = ["BlockDB", "BlockLocation", "Coin", "FileMetadata", "RevBlock"]


class Coin(_BtclibCoin):
    """One UTXO row: `btclib.tx.coin.Coin`, plus this store's own wire format.

    `tx_out`, `height` and `is_coinbase` -- what a coinbase-maturity or a
    sequence-lock rule needs of a prevout -- are `btclib.tx.coin.Coin`'s;
    that class carries no `parse` and no `serialize` on purpose (its own
    docstring, and btclib-org/btclib#1123), on-disk shape being a node's
    own decision. `parse` and `serialize` below are that decision: a
    varint packing `(height << 1) | coinbase`, ahead of the output
    itself, matching Core's own `Coin` (`src/coins.h`, at
    bitcoin/bitcoin@204256c73f) except for one thing -- Core additionally
    runs the output through `TxOutCompression`, a space optimisation not
    reproduced here, so the two do not agree byte for byte and are not
    meant to. `KeyValueStore`'s own store is measured write-dominated
    rather than read-dominated -- a modern block's own reads costing on
    the order of 3us each against 17us for a delete or a put at eight
    million rows (btclib-org/btclib-node#586) -- which argues for a
    varint kept as tight as `var_int` already makes it, not for folding
    in a second space optimisation on top of it.

    `UtxoIndex.add_block` sets `height` and `is_coinbase` when an output
    is first created; `UtxoIndex.apply_rev_block` restores a `Coin`
    exactly as `add_block` staged it for removal, so a coin a reorg
    brings back carries the height and the coinbase bit it was created
    with, never the height of the block being disconnected or of the one
    reconnecting it.
    """

    @classmethod
    def parse(cls, data: BinaryData, *, check_validity: bool = True) -> Coin:
        """Build a `Coin` by parsing the bytes `serialize` produced."""
        stream = bytesio_from_binarydata(data)
        packed = var_int.parse(stream)
        tx_out = TxOut.parse(stream, check_validity=check_validity)
        return cls(
            tx_out,
            packed >> 1,
            is_coinbase=bool(packed & 1),
            check_validity=check_validity,
        )

    def serialize(self, *, check_validity: bool = True) -> bytes:
        """Serialize this `Coin` to the bytes kept under a `utxo-` key."""
        packed = (self.height << 1) | int(self.is_coinbase)
        out = var_int.serialize(packed)
        out += self.tx_out.serialize(check_validity=check_validity)
        return out


# A file's byte offset or length, and the store's own file-rotation
# counter, are this store's bookkeeping about itself, not a count of
# items an untrusted peer handed it -- so none of the three needs
# var_int.parse's default cap, which exists to bound an attacker-
# inflated item count (btclib's var_int module docstring) and is what
# a fixed two-octet counter and a fixed-width filename slice were
# standing in for (btclib-org/btclib-node#78, #79). Bitcoin Core's own
# on-disk position and file index, FlatFilePos::nFile and ::nPos, skip
# that guard the same way: SERIALIZE_METHODS reads them through
# VARINT_MODE, not ReadCompactSize (src/flatfile.h). var_int's own
# encoding ceiling, 8 bytes, is the only bound left here.
_LOCAL_BOOKKEEPING_MAX = 0xFFFF_FFFF_FFFF_FFFF


@dataclass
class RevBlock:
    """Undo data for one block, filed once its own branch connects.

    `to_add` is the prevout each spent input consumed, restored on
    reversal; `to_remove` is every outpoint the block itself created,
    dropped on reversal. `UtxoIndex.add_block` builds one alongside the
    block it applies, and `UtxoIndex.apply_rev_block` is what walks it
    back. `to_add` carries a `Coin`, not a bare `TxOut`, for the same
    reason `Coin` exists at all: a coin a reorg restores is a coin the
    maturity rule has to be able to judge again, and only a `Coin`
    still carries what that needs. Core does the same for the same
    reason -- its own `CTxUndo` holds a `Coin` rather than a `CTxOut`.
    """

    hash: bytes
    to_add: list[tuple[OutPoint, Coin]]
    to_remove: list[OutPoint]

    @classmethod
    def deserialize(cls, data: bytes, *, check_validity: bool = False) -> RevBlock:
        """Parse a `RevBlock` from the bytes `serialize` produced."""
        stream = bytesio_from_binarydata(data)
        block_hash = stream.read(32)
        to_add: list[tuple[OutPoint, Coin]] = []
        for _ in range(var_int.parse(stream)):
            out_point = OutPoint.parse(stream, check_validity=check_validity)
            coin = Coin.parse(stream, check_validity=check_validity)
            to_add.append((out_point, coin))
        to_remove: list[OutPoint] = []
        for _ in range(var_int.parse(stream)):
            out_point = OutPoint.parse(stream, check_validity=check_validity)
            to_remove.append(out_point)
        return cls(block_hash, to_add, to_remove)

    def serialize(self, *, check_validity: bool = False) -> bytes:
        """Serialize this reverse patch to the bytes stored in a `.rev` file."""
        out = self.hash
        out += var_int.serialize(len(self.to_add))
        for out_point, coin in self.to_add:
            out += out_point.serialize(check_validity=check_validity)
            out += coin.serialize(check_validity=check_validity)
        out += var_int.serialize(len(self.to_remove))
        for out_point in self.to_remove:
            out += out_point.serialize(check_validity=check_validity)
        return out


@dataclass
class BlockLocation:
    """Where one block or reverse patch sits inside its own flat file.

    `filename` names the `.blk` or `.rev` file it was appended to,
    `index` is the byte offset `__add_data_to_file` returned for it, and
    `size` is its length -- together enough for `__get_data_from_file`
    to seek straight to it rather than scan the file.
    """

    filename: str
    index: int
    size: int

    @classmethod
    def deserialize(cls, data: bytes) -> BlockLocation:
        """Parse a `BlockLocation` from the bytes `serialize` produced."""
        stream = bytesio_from_binarydata(data)
        filename_length = var_int.parse(stream)
        filename = stream.read(filename_length).decode()
        index = var_int.parse(stream, max_size=_LOCAL_BOOKKEEPING_MAX)
        size = var_int.parse(stream, max_size=_LOCAL_BOOKKEEPING_MAX)
        return cls(filename, index, size)

    def serialize(self) -> bytes:
        """Serialize this location to the bytes kept in the key-value store."""
        filename_bytes = self.filename.encode()
        out = var_int.serialize(len(filename_bytes))
        out += filename_bytes
        out += var_int.serialize(self.index)
        out += var_int.serialize(self.size)
        return out


@dataclass
class FileMetadata:
    """Bookkeeping for one flat file this store has written to.

    `filename` is the `.blk` or `.rev` file, and `size` is how many
    bytes have been appended to it so far -- both the next write's own
    offset and, for a `.blk` file, what `__find_block_file` checks
    against the rotation threshold.
    """

    filename: str
    size: int

    @classmethod
    def deserialize(cls, data: bytes) -> FileMetadata:
        """Parse a `FileMetadata` from the bytes `serialize` produced."""
        stream = bytesio_from_binarydata(data)
        filename_length = var_int.parse(stream)
        filename = stream.read(filename_length).decode()
        size = var_int.parse(stream, max_size=_LOCAL_BOOKKEEPING_MAX)
        return cls(filename, size)

    def serialize(self) -> bytes:
        """Serialize this metadata to the bytes kept in the key-value store."""
        filename_bytes = self.filename.encode()
        out = var_int.serialize(len(filename_bytes))
        out += filename_bytes
        out += var_int.serialize(self.size)
        return out


class BlockDB:
    """Blocks and their undo data, appended to rotating flat files on disk.

    `blocks` and `rev_patches` map a block hash to the `BlockLocation`
    that finds it inside a `.blk` or `.rev` file; `files` tracks every
    such file's own size for `__find_block_file`'s rotation check.
    `_LOCAL_BOOKKEEPING_MAX` above is where that layout's own bookkeeping
    fields are argued against Bitcoin Core's.

    Unlike `Mempool` (`mempool.py:7-10`), this is reached from more than
    one thread already: `update_chain`, on `Node`'s own thread, is every
    production caller of every method here, but the suite calls
    `add_block` directly from a test's own thread while `Node`'s thread
    is concurrently inside `update_chain` reading earlier blocks back
    through `get_block` -- and `get_raw_transaction`
    (`rpc/callbacks.py:535`) is one call away from doing the same for
    real, the moment a handler is added that is not routed through
    `Node`'s own loop. `open_block_file` and `open_rev_file` are each one
    `BinaryIO` handle with one file position shared by every `seek`,
    `read` and `write` that reaches it, so a write landing between a
    reader's `seek` and its `read` moves the position out from under the
    read and hands it back whatever is there instead
    (btclib-org/btclib-node#432). `_lock` -- one `RLock` for the whole
    instance, matching `KeyValueStore`'s own "one connection, and a lock
    around every use of it" (`db.py:58-73`) -- is held for the whole of
    every public method below, the write path included: the race is
    symmetric, and a write left unlocked could still move a reader's
    position it does not itself take. One lock rather than one per
    handle, because `__add_data_to_file` updates `files` -- shared by
    both the `.blk` and the `.rev` sides -- regardless of which handle it
    is writing through, and a second lock would only ever be taken
    together with the first, never instead of it.
    """

    def __init__(
        self, data_dir: Path, logger: Logger, blocks_dir: Path | None = None
    ) -> None:
        """Open the store under `blocks_dir` or `data_dir`, and load its index.

        `blocks_dir` is `Config`'s own field of the same name -- already
        absolute and chain-suffixed there, `None` unless a caller named
        one -- so this is the one place Core's own "default: <datadir>"
        (`-blocksdir=<dir>`'s own help text, `src/init.cpp:514`, at
        bitcoin/bitcoin@ca7162cde5) is actually applied: a `BlockDB`
        built directly, the way every test here does, still gets it
        without going through `Config` at all.
        """
        self.logger = logger
        self._lock = threading.RLock()

        self.data_dir = (blocks_dir if blocks_dir is not None else data_dir) / "blocks"
        self.data_dir.mkdir(exist_ok=True, parents=True)
        self.db = KeyValueStore(self.data_dir)
        self.files: dict[str, FileMetadata] = {}
        self.blocks: dict[bytes, BlockLocation] = {}
        self.rev_patches: dict[bytes, BlockLocation] = {}
        # how many still-held blocks/rev patches point into each flat
        # file, kept alongside `files`: `_release` unlinks a file once
        # its own count reaches zero. Built by init_from_db below and
        # maintained at every site that adds to or removes from `blocks`
        # and `rev_patches` -- add_block, finalize's own rev-writing
        # loop, and prune_up_to's own two deletes.
        self.live: dict[str, int] = {}
        # the last height prune_up_to has deleted through, -1 meaning
        # nothing has been pruned yet -- persisted as its own value plus
        # one, since var_int carries no sign, and read back the same way
        # below.
        self.pruned_up_to = -1
        # held here between add_rev_block and finalize/rollback, the way
        # UtxoIndex.updated_utxo_set and FilterIndex.pending hold theirs:
        # a branch update_chain refuses never reaches disk, where writing
        # each patch as it was generated left it there regardless of
        # whether the branch it belonged to connected:
        # btclib-org/btclib-node#200
        self.pending_rev_blocks: dict[bytes, RevBlock] = {}

        self.open_block_file: BinaryIO | None = None
        self.open_rev_file: BinaryIO | None = None
        self.file_index = 0

        self.init_from_db()

    def init_from_db(self) -> None:
        """Rebuild the in-memory index from what the store already holds."""
        self.logger.info("Start Block database initialization")
        for key, value in self.db:
            if key[:1] == b"f":
                self.files[key[1:].decode()] = FileMetadata.deserialize(value)
            elif key[:1] == b"b":
                self.blocks[key[1:]] = BlockLocation.deserialize(value)
            elif key[:1] == b"r":
                self.rev_patches[key[1:]] = BlockLocation.deserialize(value)
            elif key == b"i":
                self.file_index = var_int.parse(value, max_size=_LOCAL_BOOKKEEPING_MAX)
            elif key == b"p":
                pruned = var_int.parse(value, max_size=_LOCAL_BOOKKEEPING_MAX)
                self.pruned_up_to = pruned - 1
        for location in self.blocks.values():
            self.live[location.filename] = self.live.get(location.filename, 0) + 1
        for location in self.rev_patches.values():
            self.live[location.filename] = self.live.get(location.filename, 0) + 1
        self.logger.info("Finished Block database initialization")

    def close(self) -> None:
        """Close the key-value store and any file still open for writing."""
        with self._lock:
            self.db.close()
            if self.open_block_file:
                self.open_block_file.close()
            if self.open_rev_file:
                self.open_rev_file.close()
            self.logger.info("Closing Block Database")

    def __find_block_file(self) -> BinaryIO:
        # the name is bound before the test rather than inside one
        # branch of it: written as a flag set in one `if` and read in
        # the next, it was bound on some paths only and nothing but the
        # flag said which. The `or` short-circuits, so index zero --
        # nothing written yet -- never looks up metadata it has none of.
        filename = f"{self.file_index:06d}.blk"
        if self.file_index == 0 or self.files[filename].size > 128 * 1000**2:  # 128MB
            self.file_index += 1
            filename = f"{self.file_index:06d}.blk"
            file_metadata = FileMetadata(filename, 0)
            self.files[filename] = file_metadata
            self.db.put(b"f" + filename.encode(), file_metadata.serialize())
            self.db.put(b"i", var_int.serialize(self.file_index))
        return self.__get_block_file(filename)

    def __get_block_file(self, filename: str) -> BinaryIO:
        # matched by the resolved path, not by a basename or a suffix
        # comparison: two data_dirs can share a filename, and neither is
        # unique past that (btclib-org/btclib-node#79).
        target = (self.data_dir / filename).resolve()
        if self.open_block_file is None or (
            Path(self.open_block_file.name).resolve() != target
        ):
            if self.open_block_file is not None:
                self.open_block_file.close()
            self.open_block_file = target.open("a+b")
        return self.open_block_file

    # A patch goes in the .rev file named for its own block's .blk file
    # (file_index below, resolved by finalize from self.blocks), not
    # whichever block file happens to be open when it is written:
    # btclib-org/btclib-node#116
    def __find_rev_file(self, file_index: int) -> BinaryIO:
        filename = f"{file_index:06d}.rev"
        if filename not in self.files:
            file_metadata = FileMetadata(filename, 0)
            self.files[filename] = file_metadata
            self.db.put(b"f" + filename.encode(), file_metadata.serialize())
        return self.__get_rev_file(filename=filename)

    def __get_rev_file(self, filename: str) -> BinaryIO:
        target = (self.data_dir / filename).resolve()
        if self.open_rev_file is None or (
            Path(self.open_rev_file.name).resolve() != target
        ):
            if self.open_rev_file is not None:
                self.open_rev_file.close()
            self.open_rev_file = target.open("a+b")
        return self.open_rev_file

    def __add_data_to_file(self, file: BinaryIO, data: bytes) -> tuple[int, int]:
        file.write(data)
        file.flush()
        file_metadata = self.files[Path(file.name).name]
        data_index = file_metadata.size
        data_size = len(data)
        file_metadata.size += data_size
        self.db.put(b"f" + file_metadata.filename.encode(), file_metadata.serialize())
        return data_index, data_size

    def __get_data_from_file(self, file: BinaryIO, index: int, size: int) -> bytes:
        file.seek(index)
        return file.read(size)

    def add_block(self, block: Block) -> None:
        """Append `block` to the current `.blk` file; a no-op if held."""
        with self._lock:
            block_hash = block.header.hash
            if block_hash in self.blocks:
                return
            data = block.serialize(check_validity=False)
            file = self.__find_block_file()
            index, block_size = self.__add_data_to_file(file, data)
            block_location = BlockLocation(Path(file.name).name, index, block_size)
            self.blocks[block_hash] = block_location
            self.db.put(b"b" + block_hash, block_location.serialize())
            self.live[block_location.filename] = (
                self.live.get(block_location.filename, 0) + 1
            )

    def add_rev_block(self, rev_block: RevBlock) -> None:
        """Buffer `rev_block` for `finalize` to write out.

        A no-op if `rev_block`'s own hash is already held, on disk or
        still pending -- `update_chain` can generate the same patch more
        than once for a branch it has not yet committed to.
        """
        with self._lock:
            rev_block_hash = rev_block.hash
            already_held = (
                rev_block_hash in self.rev_patches
                or rev_block_hash in self.pending_rev_blocks
            )
            if already_held:
                return
            self.pending_rev_blocks[rev_block_hash] = rev_block

    def finalize(self) -> None:
        """Write every reverse patch buffered since the last finalize.

        Each goes in the .rev file named for its own block's .blk file
        (btclib-org/btclib-node#116), looked up now rather than at
        `add_rev_block` time since that is a pure buffer and this is
        where the write happens.
        """
        with self._lock:
            for rev_block_hash, rev_block in self.pending_rev_blocks.items():
                if rev_block_hash not in self.blocks:
                    err_msg = "reverse patch for a block not stored: "
                    err_msg += rev_block_hash.hex()
                    raise ChainstateInconsistencyError(err_msg)
                file_index = int(Path(self.blocks[rev_block_hash].filename).stem)
                data = rev_block.serialize()
                file = self.__find_rev_file(file_index)
                index, block_size = self.__add_data_to_file(file, data)
                block_location = BlockLocation(Path(file.name).name, index, block_size)
                self.rev_patches[rev_block_hash] = block_location
                self.db.put(b"r" + rev_block_hash, block_location.serialize())
                self.live[block_location.filename] = (
                    self.live.get(block_location.filename, 0) + 1
                )
            self.pending_rev_blocks = {}

    def rollback(self) -> None:
        """Discard every reverse patch buffered since the last finalize."""
        with self._lock:
            self.pending_rev_blocks = {}

    def get_block(self, block_hash: bytes) -> Block | None:
        """Return the block stored under `block_hash`, or `None` if not held."""
        with self._lock:
            if block_hash not in self.blocks:
                return None
            block_location = self.blocks[block_hash]
            file = self.__get_block_file(block_location.filename)
            block_data = self.__get_data_from_file(
                file, block_location.index, block_location.size
            )
        return Block.parse(block_data, check_validity=False)

    def get_rev_block(self, block_hash: bytes) -> RevBlock | None:
        """Return the reverse patch for `block_hash`, or `None` if not held."""
        with self._lock:
            if block_hash not in self.rev_patches:
                return None
            rev_patch_location = self.rev_patches[block_hash]
            file = self.__get_rev_file(rev_patch_location.filename)
            rev_patch_data = self.__get_data_from_file(
                file, rev_patch_location.index, rev_patch_location.size
            )
        return RevBlock.deserialize(rev_patch_data)

    def prune_up_to(
        self, target_height: int, hash_at_height: Callable[[int], bytes]
    ) -> None:
        """Delete every block and reverse patch from the last pruned height on.

        `hash_at_height` is `main.prune_up_to_height`'s own
        `active_chain.__getitem__` -- the one caller this method has,
        shared by `main._prune_chain`'s own automatic-target walk and
        `rpc.callbacks.prune_blockchain`'s manual call: this store tracks
        locations by hash, never by height, so the height -> hash step
        lives with the caller that already holds `BlockIndex.active_chain`
        rather than being threaded through here. A no-op if `target_height`
        is at or behind what an earlier call already reached, the same
        idempotence `add_block` and `add_rev_block` already give the rest
        of this store -- a retry after a crash, or a second automatic-
        target step that lands on a height an earlier one already passed,
        costs nothing extra.
        """
        with self._lock:
            if target_height <= self.pruned_up_to:
                return
            for height in range(self.pruned_up_to + 1, target_height + 1):
                block_hash = hash_at_height(height)
                self._delete_block(block_hash)
                self._delete_rev_block(block_hash)
            self.pruned_up_to = target_height
            self.db.put(b"p", var_int.serialize(target_height + 1))

    def current_usage(self) -> int:
        """Bytes this store still accounts for, across every `.blk`/`.rev` file.

        Core's own `BlockManager::CalculateCurrentUsage`
        (`node/blockstorage.cpp:811-818`, at bitcoin/bitcoin@ca7162cde5)
        sums `nSize + nUndoSize` over every block file its own
        `m_blockfile_info` still tracks; `self.files` is this store's own
        counterpart, one `FileMetadata` per `.blk` or `.rev` file not yet
        unlinked by `_release`, so the same sum over its `size` fields
        answers the same question. `main._prune_chain`'s own automatic-
        target walk is the one caller.
        """
        with self._lock:
            return sum(file.size for file in self.files.values())

    def _delete_block(self, block_hash: bytes) -> None:
        location = self.blocks.pop(block_hash, None)
        if location is None:
            return
        self.db.delete(b"b" + block_hash)
        self._release(location.filename, is_block=True)

    def _delete_rev_block(self, block_hash: bytes) -> None:
        location = self.rev_patches.pop(block_hash, None)
        if location is None:
            return
        self.db.delete(b"r" + block_hash)
        self._release(location.filename, is_block=False)

    def _release(self, filename: str, *, is_block: bool) -> None:
        """Drop one live reference to `filename`, unlinking it once none remain.

        Core's own `UnlinkPrunedFiles` (`node/blockstorage.cpp`, at
        bitcoin/bitcoin@ca7162cde5) reaches the same file once every block
        `FindFilesToPrune` sees inside it is behind the retained depth,
        found by the file's own stored height range
        (`nHeightFirst`/`nHeightLast`). This store's own rotation
        (`__find_block_file`) tracks append order rather than height, so
        `prune_up_to` above reaches the same file from the other
        direction -- one block at a time, oldest height first -- and this
        is where the count converges on Core's own "every block in this
        file is gone" the same file-granularity deletion answers to. A
        syncing node writes in close to height order already, so the two
        line up in the ordinary case; a node whose forks left blocks
        scattered across files out of height order reclaims those files
        later than Core would, once the last of their still-live blocks
        is finally pruned too, rather than not at all.

        Never unlinks the file `file_index` currently names: that is
        still open for new blocks or patches to land in, whatever this
        call's own count says, and reclaiming it here would have the very
        next `add_block` or `finalize` reopen a file this store had just
        deleted out from under itself.

        `filename` is trusted to already be a key of `self.files`, the
        same way `__find_block_file`'s own `self.files[filename].size`
        is above -- every `BlockLocation`/`RevBlock` location this class
        ever hands `_delete_block`/`_delete_rev_block` came from a
        `self.blocks`/`self.rev_patches` entry that was itself only ever
        set alongside the matching `self.files` entry, in `add_block` and
        `finalize`, so there is no path that reaches here with one and
        not the other.

        Only ever closes `open_rev_file`, never `open_block_file`, and
        that asymmetry is real rather than a gap: `__get_block_file`
        keeps `open_block_file` in lockstep with `self.file_index` on
        every `add_block`, the very call that would have to move
        `self.file_index` past a `.blk` file for that file to reach this
        method at all, so a `.blk` file past the guard above never has
        `open_block_file` still pointing at it -- there is no `filename`
        for which that check could ever be true. `__find_rev_file` opens
        a `.rev` file named for its own block's own file index
        (`btclib-org/btclib-node#116`), not for whatever is current, so
        `open_rev_file` alone can lag `self.file_index` this way.
        """
        remaining = self.live.get(filename, 0) - 1
        if remaining > 0:
            self.live[filename] = remaining
            return
        self.live.pop(filename, None)
        if Path(filename).stem == f"{self.file_index:06d}":
            return
        if (
            not is_block
            and self.open_rev_file is not None
            and Path(self.open_rev_file.name).name == filename
        ):
            self.open_rev_file.close()
            self.open_rev_file = None
        (self.data_dir / filename).unlink(missing_ok=True)
        del self.files[filename]
        self.db.delete(b"f" + filename.encode())
