# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from btclib import var_int
from btclib.block import Block
from btclib.tx.tx_in import OutPoint
from btclib.tx.tx_out import TxOut
from btclib.utils import bytesio_from_binarydata

from btclib_node.db import KeyValueStore
from btclib_node.log import Logger


@dataclass
class RevBlock:
    hash: bytes
    to_add: list[tuple[OutPoint, TxOut]]
    to_remove: list[OutPoint]

    @classmethod
    def deserialize(cls, data: bytes, check_validity: bool = False) -> RevBlock:
        stream = bytesio_from_binarydata(data)
        hash = stream.read(32)
        to_add: list[tuple[OutPoint, TxOut]] = []
        for x in range(var_int.parse(stream)):
            out_point = OutPoint.parse(stream, check_validity=check_validity)
            tx_out = TxOut.parse(stream, check_validity=check_validity)
            to_add.append((out_point, tx_out))
        to_remove: list[OutPoint] = []
        for x in range(var_int.parse(stream)):
            out_point = OutPoint.parse(stream, check_validity=check_validity)
            to_remove.append(out_point)
        return cls(hash, to_add, to_remove)

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
        filename = stream.read(10).decode()
        index = var_int.parse(stream)
        size = var_int.parse(stream)
        return cls(filename, index, size)

    def serialize(self) -> bytes:
        out = self.filename.encode()
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
        filename = stream.read(10).decode()
        size = var_int.parse(stream)
        return cls(filename, size)

    def serialize(self) -> bytes:
        out = self.filename.encode()
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
                self.file_index = int.from_bytes(value, "big")
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
            self.db.put(b"i", (self.file_index).to_bytes(2, "big"))
        return self.__get_block_file(filename)

    def __get_block_file(self, filename: str) -> BinaryIO:
        if not self.open_block_file:
            self.open_block_file = (self.data_dir / filename).open("a+b")
        if self.open_block_file.name[-len(filename) :] != filename:
            self.open_block_file.close()
            self.open_block_file = (self.data_dir / filename).open("a+b")
        return self.open_block_file

    # A patch goes in the .rev file indexed by the block file currently
    # being written, which need not be the one indexed by the block the
    # patch undoes: btclib-org/btclib-node#116
    def __find_rev_file(self) -> BinaryIO:
        filename = f"{self.file_index:06d}.rev"
        if filename not in self.files:
            file_metadata = FileMetadata(filename, 0)
            self.files[filename] = file_metadata
            self.db.put(b"f" + filename.encode(), file_metadata.serialize())
        return self.__get_rev_file(filename=f"{self.file_index:06d}.rev")

    def __get_rev_file(self, filename: str) -> BinaryIO:
        if not self.open_rev_file:
            self.open_rev_file = (self.data_dir / filename).open("a+b")
        if self.open_rev_file.name[-len(filename) :] != filename:
            self.open_rev_file.close()
            self.open_rev_file = (self.data_dir / filename).open("a+b")
        return self.open_rev_file

    def __add_data_to_file(self, file: BinaryIO, data: bytes) -> tuple[int, int]:
        file.write(data)
        file.flush()
        file_metadata = self.files[file.name[-10:]]
        data_index = file_metadata.size
        data_size = len(data)
        file_metadata.size += data_size
        self.db.put(b"f" + file_metadata.filename.encode(), file_metadata.serialize())
        return data_index, data_size

    def __get_data_from_file(self, file: BinaryIO, index: int, size: int) -> bytes:
        file.seek(index)
        data = file.read(size)
        return data

    def add_block(self, block: Block) -> None:
        block_hash = block.header.hash
        if block_hash in self.blocks:
            return
        data = block.serialize(check_validity=False)
        file = self.__find_block_file()
        index, block_size = self.__add_data_to_file(file, data)
        block_location = BlockLocation(file.name[-10:], index, block_size)
        self.blocks[block_hash] = block_location
        self.db.put(b"b" + block_hash, block_location.serialize())

    def add_rev_block(self, rev_block: RevBlock) -> None:
        rev_block_hash = rev_block.hash
        if rev_block_hash in self.rev_patches:
            return
        data = rev_block.serialize()
        file = self.__find_rev_file()
        index, block_size = self.__add_data_to_file(file, data)
        block_location = BlockLocation(file.name[-10:], index, block_size)
        self.rev_patches[rev_block_hash] = block_location
        self.db.put(b"r" + rev_block_hash, block_location.serialize())

    def get_block(self, hash: bytes) -> Block | None:
        if hash not in self.blocks:
            return None
        block_location = self.blocks[hash]
        file = self.__get_block_file(block_location.filename)
        block_data = self.__get_data_from_file(
            file, block_location.index, block_location.size
        )
        return Block.parse(block_data, check_validity=False)

    def get_rev_block(self, hash: bytes) -> RevBlock | None:
        if hash not in self.rev_patches:
            return None
        rev_patch_location = self.rev_patches[hash]
        file = self.__get_rev_file(rev_patch_location.filename)
        rev_patch_data = self.__get_data_from_file(
            file, rev_patch_location.index, rev_patch_location.size
        )
        return RevBlock.deserialize(rev_patch_data)
