# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import secrets
from dataclasses import dataclass

from btclib.p2p.payload import Payload
from btclib.utils import bytesio_from_binarydata


@dataclass
class Ping(Payload):
    command = "ping"

    nonce: int

    def __init__(self, nonce=None):
        if not nonce:
            self.nonce = secrets.randbelow(0xFFFFFFFFFFFF)
        else:
            self.nonce = nonce

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        nonce = int.from_bytes(stream.read(8), "little")
        return cls(nonce=nonce)

    def serialize(self, *, check_validity: bool = True) -> bytes:
        return self.nonce.to_bytes(8, "little")


@dataclass
class Pong(Payload):
    command = "pong"

    nonce: int

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        nonce = int.from_bytes(stream.read(8), "little")
        return cls(nonce=nonce)

    def serialize(self, *, check_validity: bool = True) -> bytes:
        return self.nonce.to_bytes(8, "little")
