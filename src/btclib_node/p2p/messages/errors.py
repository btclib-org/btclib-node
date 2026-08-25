# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from btclib import var_int
from btclib.p2p.payload import Payload
from btclib.utils import bytesio_from_binarydata

if TYPE_CHECKING:
    from btclib.alias import BinaryData


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
class Reject(Payload):
    command = "reject"

    message: str
    code: RejectCode
    reason: str
    data: bytes

    @classmethod
    def parse(
        cls,
        data: BinaryData,
        *,
        check_validity: bool = True,  # noqa: ARG003
    ) -> Reject:
        # every btclib Payload's own parse/serialize pair takes
        # check_validity, called polymorphically (Connection.py's own
        # payload.serialize(check_validity=False) among them) without a
        # caller knowing which subclass is on the other end -- kept
        # here for that shared shape even though Reject's own wire
        # format has nothing check_validity would gate. serialize
        # below carries the same parameter, unread the same way, but
        # is @override and so is not itself one of ARG's own findings.
        stream = bytesio_from_binarydata(data)
        message = stream.read(var_int.parse(stream)).decode()
        code = RejectCode.from_bytes(stream.read(1), "little")
        reason = stream.read(var_int.parse(stream)).decode()
        # the wire carries a hash in internal order, and everything
        # here holds one the way it is displayed, as an inventory does
        data = stream.read(32)[::-1]
        return cls(message, code, reason, data)

    @override
    def serialize(self, *, check_validity: bool = True) -> bytes:
        payload = var_int.serialize(len(self.message))
        payload += self.message.encode()
        payload += self.code.to_bytes(1, "little")
        payload += var_int.serialize(len(self.reason))
        payload += self.reason.encode()
        payload += self.data[::-1]
        return payload
