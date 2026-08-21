# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from dataclasses import dataclass

from btclib import var_int
from btclib.block import BlockHeader
from btclib.tx.tx import Tx
from btclib.utils import bytesio_from_binarydata

from btclib_node.p2p.messages import add_headers


@dataclass
class Sendcmpct:
    flag: bool
    version: int

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        flag = bool(stream.read(1)[0])
        version = int.from_bytes(stream.read(8), "little")
        return cls(flag, version)

    def serialize(self):
        payload = int(self.flag).to_bytes(1, "little")
        payload += self.version.to_bytes(8, "little")
        return add_headers("sendcmpt", payload)


@dataclass
class Cmpctblock:
    header: BlockHeader
    nonce: int
    short_ids: list[bytes]
    prefilled_tx_list: list[tuple[int, Tx]]
    include_witness: bool = True

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        header = BlockHeader.parse(stream)
        nonce = int.from_bytes(stream.read(8), "little")
        short_ids = []
        short_ids_length = var_int.parse(stream)
        for x in range(short_ids_length):
            short_ids.append(stream.read(6)[::-1])
        prefilled_tx_list = []
        prefilled_tx_num = var_int.parse(stream)
        for x in range(prefilled_tx_num):
            tx_index = var_int.parse(stream)
            tx = Tx.parse(stream)
            prefilled_tx_list.append((tx_index, tx))
        return cls(
            header=header,
            nonce=nonce,
            short_ids=short_ids,
            prefilled_tx_list=prefilled_tx_list,
        )

    def serialize(self):
        payload = self.header.serialize()
        payload += self.nonce.to_bytes(8, "little")
        payload += var_int.serialize(len(self.short_ids))
        for short_id in self.short_ids:
            payload += short_id[::-1]
        payload += var_int.serialize(len(self.prefilled_tx_list))
        for tx_index, tx in self.prefilled_tx_list:
            payload += var_int.serialize(tx_index)
            payload += tx.serialize(self.include_witness)
        return add_headers("cmptblock", payload)
