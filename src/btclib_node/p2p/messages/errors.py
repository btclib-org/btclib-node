# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Reject`, BIP61's message, and the codes it carries.

`p2p.callbacks.reject` is the only handler that reads one, logging what
a peer sent; this node never constructs or sends one of its own.

Every way `parse` below refuses a payload is a `BTClibException`, which
is what `handle_p2p` (`p2p/main.py`) sorts a peer's fault from this
node's by: `btclib.var_int.parse`'s own refusal of a length prefix, and
`InvalidRejectPayloadError` for everything this module decides itself.
"""

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from btclib import var_int
from btclib.p2p.payload import Payload
from btclib.utils import bytesio_from_binarydata

from btclib_node.exceptions import InvalidRejectPayloadError

if TYPE_CHECKING:
    from io import BytesIO

    from btclib.alias import BinaryData

__all__ = ["Reject", "RejectCode"]

# BIP61's tx and block rejects each append the hash of what was
# rejected to the common payload, and its version reject appends
# nothing, so what follows `reason` is either absent or exactly this
# many octets
_HASH_SIZE = 32


def _read(stream: BytesIO, size: int, field: str) -> bytes:
    """Return `field`'s own `size` octets, refusing a shorter stream.

    `BytesIO.read` answers a stream that has run out with whatever is
    left rather than with an error, so a payload cut short mid-field
    would otherwise parse as a shorter one a peer never sent.
    """
    octets = stream.read(size)
    if len(octets) != size:
        err_msg = f"{field} wants {size} octets, {len(octets)} left"
        raise InvalidRejectPayloadError(err_msg)
    return octets


def _parse_str(stream: BytesIO, field: str) -> str:
    """Return the var_str `field`, refusing octets no utf-8 decodes.

    The length prefix is `btclib.var_int.parse`'s to refuse, its
    `BTClibValueError` already being the family `handle_p2p` reads as
    the peer's fault.
    """
    octets = _read(stream, var_int.parse(stream), field)
    try:
        return octets.decode()
    except UnicodeDecodeError as e:
        err_msg = f"{field} is not utf-8"
        raise InvalidRejectPayloadError(err_msg) from e


class RejectCode(enum.IntEnum):
    """BIP61's own reject codes, as `Reject.code` carries them on the wire."""

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
    """BIP61's `reject` message: what a peer refused, and why.

    The module docstring above is where its one reader is named.
    """

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
        """Parse a `Reject` from the bytes a peer sent.

        Accepts exactly what `serialize` below writes, so a payload
        holding anything else -- a field cut short, a trailing octet
        past the hash -- is refused rather than parsed into an object
        that reserializes to something the peer did not send.
        """
        # every btclib Payload's own parse/serialize pair takes
        # check_validity, called polymorphically (Connection.py's own
        # payload.serialize(check_validity=False) among them) without a
        # caller knowing which subclass is on the other end -- kept
        # here for that shared shape even though Reject's own wire
        # format has nothing check_validity would gate. serialize
        # below carries the same parameter, unread the same way, but
        # is @override and so is not itself one of ARG's own findings.
        stream = bytesio_from_binarydata(data)
        message = _parse_str(stream, "message")
        octet = _read(stream, 1, "code")[0]
        try:
            code = RejectCode(octet)
        except ValueError as e:
            err_msg = f"not a BIP61 reject code: {octet:#04x}"
            raise InvalidRejectPayloadError(err_msg) from e
        reason = _parse_str(stream, "reason")
        # the wire carries a hash in internal order, and everything
        # here holds one the way it is displayed, as an inventory does
        rejected = stream.read()
        if rejected and len(rejected) != _HASH_SIZE:
            err_msg = f"a hash is {_HASH_SIZE} octets, not {len(rejected)}"
            raise InvalidRejectPayloadError(err_msg)
        return cls(message, code, reason, rejected[::-1])

    @override
    def serialize(self, *, check_validity: bool = True) -> bytes:
        payload = var_int.serialize(len(self.message))
        payload += self.message.encode()
        payload += self.code.to_bytes(1, "little")
        payload += var_int.serialize(len(self.reason))
        payload += self.reason.encode()
        payload += self.data[::-1]
        return payload
