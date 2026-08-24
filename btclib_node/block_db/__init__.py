# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from btclib import var_int
from btclib.block import Block
from btclib.tx.out_point import OutPoint
from btclib.tx.tx_out import TxOut
from btclib.utils import bytesio_from_binarydata

from btclib_node.db import KeyValueStore
from btclib_node.log import Logger

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
    hash: bytes
    to_add: list[tuple[OutPoint, TxOut]]
    to_remove: list[OutPoint]

    @classmethod
    def deserialize(cls, data: bytes, check_validity: bool = False) -> RevBlock:
        stream = bytesio_from_binarydata(data)
        block_hash = stream.read(32)
        to_add: list[tuple[OutPoint, TxOut]] = []
        for _ in range(var_int.parse(stream)):
            out_point = OutPoint.parse(stream, check_validity=check_validity)
            tx_out = TxOut.parse(stream, check_validity=check_validity)
            to_add.append((out_point, tx_out))
        to_remove: list[OutPoint] = []
        for _ in range(var_int.parse(stream)):
            out_point = OutPoint.parse(stream, check_validity=check_validity)
            to_remove.append(out_point)
        return cls(block_hash, to_add, to_remove)

    def serialize(self, check_validity: bool = False) -> bytes:
        out = self.hash
        out += var_int.serialize(len(self.to_add))
        for out_point, tx_out in self.to_add:
            out += out_point.serialize(check_validity=check_validity)
            out += tx_out.serialize(check_validity=check_validity)
        out += var_int.serialize(len(self.to_remove))
        for out_point in self.to_remove:
            out += out_point.serialize(check_validity=check_validity)
        return out


@dataclass
class BlockLocation:
    filename: str
    index: int
    size: int

    @classmethod
    def deserialize(cls, data: bytes) -> BlockLocation:
        stream = bytesio_from_binarydata(data)
        filename_length = var_int.parse(stream)
        filename = stream.read(filename_length).decode()
        index = var_int.parse(stream, max_size=_LOCAL_BOOKKEEPING_MAX)
        size = var_int.parse(stream, max_size=_LOCAL_BOOKKEEPING_MAX)
        return cls(filename, index, size)

    def serialize(self) -> bytes:
        filename_bytes = self.filename.encode()
        out = var_int.serialize(len(filename_bytes))
        out += filename_bytes
        out += var_int.serialize(self.index)
        out += var_int.serialize(self.size)
        return out


@dataclass
class FileMetadata:
    filename: str
    size: int

    @classmethod
    def deserialize(cls, data: bytes) -> FileMetadata:
        stream = bytesio_from_binarydata(data)
        filename_length = var_int.parse(stream)
        filename = stream.read(filename_length).decode()
        size = var_int.parse(stream, max_size=_LOCAL_BOOKKEEPING_MAX)
        return cls(filename, size)

    def serialize(self) -> bytes:
        filename_bytes = self.filename.encode()
        out = var_int.serialize(len(filename_bytes))
        out += filename_bytes
        out += var_int.serialize(self.size)
        return out


class BlockDB:
    def __init__(self, data_dir: Path, logger: Logger) -> None:
        self.logger = logger

        self.data_dir = data_dir / "blocks"
        self.data_dir.mkdir(exist_ok=True, parents=True)
        self.db = KeyValueStore(self.data_dir)
        self.files: dict[str, FileMetadata] = {}
        self.blocks: dict[bytes, BlockLocation] = {}
        self.rev_patches: dict[bytes, BlockLocation] = {}
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
        self.logger.info("Finished Block database initialization")

    def close(self) -> None:
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
        block_hash = block.header.hash
        if block_hash in self.blocks:
            return
        data = block.serialize(check_validity=False)
        file = self.__find_block_file()
        index, block_size = self.__add_data_to_file(file, data)
        block_location = BlockLocation(Path(file.name).name, index, block_size)
        self.blocks[block_hash] = block_location
        self.db.put(b"b" + block_hash, block_location.serialize())

    def add_rev_block(self, rev_block: RevBlock) -> None:
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
        for rev_block_hash, rev_block in self.pending_rev_blocks.items():
            if rev_block_hash not in self.blocks:
                err_msg = "reverse patch for a block not stored: "
                err_msg += rev_block_hash.hex()
                raise Exception(err_msg)
            file_index = int(Path(self.blocks[rev_block_hash].filename).stem)
            data = rev_block.serialize()
            file = self.__find_rev_file(file_index)
            index, block_size = self.__add_data_to_file(file, data)
            block_location = BlockLocation(Path(file.name).name, index, block_size)
            self.rev_patches[rev_block_hash] = block_location
            self.db.put(b"r" + rev_block_hash, block_location.serialize())
        self.pending_rev_blocks = {}

    def rollback(self) -> None:
        self.pending_rev_blocks = {}

    def get_block(self, block_hash: bytes) -> Block | None:
        if block_hash not in self.blocks:
            return None
        block_location = self.blocks[block_hash]
        file = self.__get_block_file(block_location.filename)
        block_data = self.__get_data_from_file(
            file, block_location.index, block_location.size
        )
        return Block.parse(block_data, check_validity=False)

    def get_rev_block(self, block_hash: bytes) -> RevBlock | None:
        if block_hash not in self.rev_patches:
            return None
        rev_patch_location = self.rev_patches[block_hash]
        file = self.__get_rev_file(rev_patch_location.filename)
        rev_patch_data = self.__get_data_from_file(
            file, rev_patch_location.index, rev_patch_location.size
        )
        return RevBlock.deserialize(rev_patch_data)
