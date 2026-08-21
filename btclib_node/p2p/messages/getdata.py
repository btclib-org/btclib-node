# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import enum
from dataclasses import dataclass

from btclib import var_int
from btclib.utils import bytesio_from_binarydata

from btclib_node.p2p.messages import add_headers


@dataclass
class Getdata:
    inventory: list[tuple[int, bytes]]

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        inventory_length = var_int.parse(stream)
        inventory = []
        for x in range(inventory_length):
            item_type = int.from_bytes(stream.read(4), "little")
            item_hash = stream.read(32)[::-1]
            inventory.append((item_type, item_hash))
        return cls(inventory)

    def serialize(self):
        payload = var_int.serialize(len(self.inventory))
        for item in self.inventory:
            payload += item[0].to_bytes(4, "little")
            payload += item[1][::-1]
        return add_headers("getdata", payload)


@dataclass
class Getblocks:
    version: int
    block_hashes: list[bytes]
    hash_stop: bytes

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        version = int.from_bytes(stream.read(4), "little")
        block_hashes = []
        for x in range(var_int.parse(stream)):
            block_hash = stream.read(32)[::-1]
            block_hashes.append(block_hash)
        hash_stop = stream.read(32)[::-1]
        return cls(version=version, block_hashes=block_hashes, hash_stop=hash_stop)

    def serialize(self):
        payload = self.version.to_bytes(4, "little")
        payload += var_int.serialize(len(self.block_hashes))
        for hash in self.block_hashes:
            payload += hash[::-1]
        payload += self.hash_stop[::-1]
        return add_headers("getblocks", payload)


@dataclass
class Getheaders:
    version: int
    block_hashes: list[bytes]
    hash_stop: bytes

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        version = int.from_bytes(stream.read(4), "little")
        block_hashes = []
        for x in range(var_int.parse(stream)):
            block_hash = stream.read(32)[::-1]
            block_hashes.append(block_hash)
        hash_stop = stream.read(32)[::-1]
        return cls(version=version, block_hashes=block_hashes, hash_stop=hash_stop)

    def serialize(self):
        payload = self.version.to_bytes(4, "little")
        payload += var_int.serialize(len(self.block_hashes))
        for hash in self.block_hashes:
            payload += hash[::-1]
        payload += self.hash_stop[::-1]
        return add_headers("getheaders", payload)


@dataclass
class Getblocktxn:
    blockhash: bytes
    indexes: list[int]

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        blockhash = stream.read(32)[::-1]
        num_indexes = var_int.parse(stream)
        indexes = []
        for x in range(num_indexes):
            indexes.append(var_int.parse(stream))
        return cls(blockhash=blockhash, indexes=indexes)

    def serialize(self):
        payload = self.blockhash[::-1]
        payload += var_int.serialize(len(self.indexes))
        for id in self.indexes:
            payload += var_int.serialize(id)
        return add_headers("getblocktxn", payload)


@dataclass
class Mempool:
    @classmethod
    def deserialize(cls, data):
        return cls()

    def serialize(self):
        return add_headers("mempool", b"")


@dataclass
class Sendheaders:
    @classmethod
    def deserialize(cls, data):
        return cls()

    def serialize(self):
        return add_headers("sendheaders", b"")


class InventoryType(enum.IntEnum):
    tx = 1
    block = 2
    filtered_block = 3
    cmpct_block = 4
    wtx = 5
    witness_tx = 0x40000001
    witness_block = 0x40000002
    filtered_witness_block = 0x40000003
