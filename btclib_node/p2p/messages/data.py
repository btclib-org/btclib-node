# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from dataclasses import dataclass

from btclib import var_int
from btclib.block import Block as BlockData
from btclib.block import BlockHeader
from btclib.p2p.payload import Payload
from btclib.tx.tx import Tx as TxData
from btclib.utils import bytesio_from_binarydata


@dataclass
class Tx(Payload):
    command = "tx"

    tx: TxData
    include_witness: bool = True

    @classmethod
    def deserialize(cls, data):
        tx = TxData.parse(data)
        return cls(tx)

    def serialize(self, *, check_validity: bool = True) -> bytes:
        return self.tx.serialize(self.include_witness)


@dataclass
class Block(Payload):
    command = "block"

    block: BlockData
    include_witness: bool = True

    # Neither direction validates: btclib checks a block against
    # mainnet's pow limit unless told which one applies, and a message
    # codec has no chain to tell it. Whoever holds the chain does that,
    # on the way in -- p2p.callbacks.block, against
    # node.chain.pow_limit_bits.
    @classmethod
    def deserialize(cls, data):
        block = BlockData.parse(data, check_validity=False)
        return cls(block)

    def serialize(self, *, check_validity: bool = True) -> bytes:
        return self.block.serialize(self.include_witness, check_validity=False)


@dataclass
class Headers(Payload):
    command = "headers"

    headers: list[BlockHeader]

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        headers_num = var_int.parse(stream)
        headers = []
        for x in range(headers_num):
            header = BlockHeader.parse(stream)
            stream.read(1)
            headers.append(header)
        return cls(headers)

    def serialize(self, *, check_validity: bool = True) -> bytes:
        payload = var_int.serialize(len(self.headers))
        for header in self.headers:
            payload += header.serialize()
            payload += b"\x00"
        return payload


@dataclass
class Blocktxn(Payload):
    command = "blocktxn"

    blockhash: bytes
    transactions: list[TxData]
    include_witness: bool = True

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        blockhash = stream.read(32)[::-1]
        num_transactions = var_int.parse(stream)
        transactions = []
        for x in range(num_transactions):
            transactions.append(TxData.parse(stream))
        return cls(blockhash=blockhash, transactions=transactions)

    def serialize(self, *, check_validity: bool = True) -> bytes:
        payload = self.blockhash[::-1]
        payload += var_int.serialize(len(self.transactions))
        for tx in self.transactions:
            payload += tx.serialize(self.include_witness)
        return payload


@dataclass
class Inv(Payload):
    command = "inv"

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

    def serialize(self, *, check_validity: bool = True) -> bytes:
        payload = var_int.serialize(len(self.inventory))
        for item in self.inventory:
            payload += item[0].to_bytes(4, "little")
            payload += item[1][::-1]
        return payload
