# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import enum
from dataclasses import dataclass

from btclib import var_int
from btclib.utils import bytesio_from_binarydata

from btclib_node.p2p.messages import add_headers


@dataclass
class Notfound:
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
        return add_headers("notfound", payload)


class RejectCode(enum.IntEnum):
    malformed = 0x01
    invalid = 0x10
    obsolete = 0x11
    duplicate = 0x12
    nonstandard = 0x40
    dust = 0x41
    insufficientfee = 0x42
    checkpoint = 0x43


@dataclass
class Reject:
    message: str
    code: RejectCode
    reason: str
    data: bytes

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        message = stream.read(var_int.parse(stream)).decode()
        code = RejectCode.from_bytes(stream.read(1), "little")
        reason = stream.read(var_int.parse(stream)).decode()
        data = stream.read(32)
        return cls(message, code, reason, data)

    def serialize(self):
        payload = var_int.serialize(len(self.message))
        payload += self.message.encode()
        payload += self.code.to_bytes(1, "little")
        payload += var_int.serialize(len(self.reason))
        payload += self.reason.encode()
        payload += self.data[::-1]
        return add_headers("reject", payload)
