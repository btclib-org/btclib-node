# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The messages that are a command and nothing else.

btclib defines the empty payloads it has a use for -- `verack`,
`sendaddrv2` -- and here are the ones this node speaks that it does not.
What they have in common is the only thing they have, so they share a
base rather than repeating an empty `parse` and an empty `serialize`
apiece.

Every payload here is one the node sends or dispatches on. A command it
speaks to nobody is not an empty payload for want of a body: the base is
for messages that carry nothing, not a place to park a stub for a
message that carries something nobody has written yet.
"""

from dataclasses import dataclass
from typing import TypeVar, override

from btclib.alias import BinaryData
from btclib.p2p.payload import Payload

_Empty = TypeVar("_Empty", bound="_EmptyPayload")


@dataclass
class _EmptyPayload(Payload):
    """A payload of no octets: the command carries the whole message.

    Private, as btclib's own payload bases are: it declares no
    `command`, so it is a shape to inherit rather than a message to
    send.
    """

    @classmethod
    def parse(
        cls: type[_Empty], data: BinaryData, *, check_validity: bool = True
    ) -> _Empty:
        """Return the payload these no octets carry, whatever they are.

        `data` and `check_validity` are btclib's signature for a
        `parse`, kept so that a caller cannot be surprised by which of
        these classes accepts it; there is nothing here to read or to
        check.
        """
        return cls()

    @override
    def serialize(self, *, check_validity: bool = True) -> bytes:
        return b""


@dataclass
class Getaddr(_EmptyPayload):
    command = "getaddr"


@dataclass
class Mempool(_EmptyPayload):
    command = "mempool"


@dataclass
class Sendheaders(_EmptyPayload):
    command = "sendheaders"


@dataclass
class Wtxidrelay(_EmptyPayload):
    command = "wtxidrelay"
