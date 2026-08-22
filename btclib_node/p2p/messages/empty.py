# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The messages that are a command and nothing else.

btclib defines the empty payloads it has a use for -- `verack`,
`sendaddrv2` -- and here are the ones this node speaks that it does not.
What they have in common is the only thing they have, so they share a
base rather than repeating an empty `parse` and an empty `serialize`
apiece.

`filterclear` is of the same shape and stays in `filters.py` with the
rest of that module: those stubs are dead and wrong on the wire
(btclib-org/btclib_node#50), and giving them a base built for payloads
that are *correctly* empty would only make them look finished.
"""

from dataclasses import dataclass

from btclib.p2p.payload import Payload


@dataclass
class _EmptyPayload(Payload):
    """A payload of no octets: the command carries the whole message.

    Private, as btclib's own payload bases are: it declares no
    `command`, so it is a shape to inherit rather than a message to
    send.
    """

    @classmethod
    def parse(cls, data, *, check_validity: bool = True):
        """Return the payload these no octets carry, whatever they are.

        `data` and `check_validity` are btclib's signature for a
        `parse`, kept so that a caller cannot be surprised by which of
        these classes accepts it; there is nothing here to read or to
        check.
        """
        return cls()

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
