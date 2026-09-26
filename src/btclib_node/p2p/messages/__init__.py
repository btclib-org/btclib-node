# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The p2p payloads this node carries itself: two, for old peers only.

Every other wire message this node speaks is `btclib.p2p`'s, imported
where it is used -- BIP61's `reject` included, `Reject` and `RejectCode`
being `btclib.p2p.reject`'s. The two here are what Core still sends a
peer too old for the rest, and they are sent and never parsed:
`p2p.callbacks` is where every command received is dispatched to a
handler.
"""

from dataclasses import dataclass
from typing import ClassVar, override

from btclib.p2p.payload import Payload

__all__ = ["FinalAlert", "NoncelessPing"]


@dataclass(frozen=True)
class NoncelessPing(Payload):
    """A `ping` with no payload, for a peer at `BIP0031_VERSION` or below.

    What Core's `MaybeSendPing` (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) sends such a peer, which
    answers it with no `pong`. btclib's own `Ping` always carries a
    nonce.
    """

    command: ClassVar[str] = "ping"

    @override
    def serialize(self, *, check_validity: bool = True) -> bytes:
        """Return no octets."""
        return b""


# Core's `finalAlert`, verbatim: the retired alert system's last message,
# "URGENT: Alert key compromised, upgrade required", signed with that key
_FINAL_ALERT = bytes.fromhex(
    "60010000000000000000000000ffffff7f00000000ffffff7ffeffff7f01ffffff"
    "7f00000000ffffff7f00ffffff7f002f555247454e543a20416c657274206b6579"
    "20636f6d70726f6d697365642c2075706772616465207265717569726564004630"
    "440220653febd6410f470f6bae11cad19c48413becb1ac2c17f908fd0fd53bdc3a"
    "bd5202206d0e9c96fe88d4a0f01ed9dedae2b6f9e00da94cad0fecaae66ecf689b"
    "f71b50"
)


@dataclass(frozen=True)
class FinalAlert(Payload):
    """The final `alert`, for a peer at `SENDHEADERS_VERSION` or below.

    What Core's `version` handler (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) sends a peer "old enough
    to have the old alert system", its common version 70012 or below.
    """

    command: ClassVar[str] = "alert"

    @override
    def serialize(self, *, check_validity: bool = True) -> bytes:
        """Return Core's `finalAlert` octets."""
        return _FINAL_ALERT
