# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from dataclasses import dataclass

from btclib import var_int
from btclib.p2p.payload import Payload
from btclib.utils import bytesio_from_binarydata, read_exactly

from btclib_node.p2p.address import NetworkAddress


@dataclass
class Version(Payload):
    command = "version"

    version: int
    services: int
    timestamp: int
    addr_recv: NetworkAddress
    addr_from: NetworkAddress
    nonce: int
    user_agent: str
    start_height: int
    # None and not False: the octets ran out before BIP37's flag, which
    # is a different thing from a peer that sent it as zero, and only one
    # of the two is asking not to be relayed to. `is_relay_requested`
    # below is the reading; this field is what was on the wire, so that
    # a payload without the flag serializes back without it.
    relay: bool | None = None

    @property
    def is_relay_requested(self) -> bool:
        """Answer whether this peer wants transactions announced to it.

        BIP37 added the flag at protocol version 70001 and made its
        absence mean true. Bitcoin Core reads it so: `bool fRelay = true`
        is declared before the message is read and assigned only inside
        `if (!vRecv.empty())`, so a `version` that stops before the flag
        is asking for relay rather than refusing it.

        Named as `btclib.p2p.handshake.Version` names it. This class is
        the duplicate btclib-org/btclib-node#43 deletes, and a caller
        written against this property outlives that deletion.
        """
        return self.relay is not False

    @classmethod
    def parse(cls, data, *, check_validity: bool = True):
        stream = bytesio_from_binarydata(data)
        # read_exactly and not stream.read: a short read answers with
        # whatever is left rather than raising, and int.from_bytes takes
        # the short answer without a word, so a truncated payload used to
        # parse into a Version whose every field past the cut was zero.
        # The two addresses and the user agent below read through
        # btclib_node.p2p.address and btclib.var_int, which is where the
        # rest of that hole is; #43 closes it by deleting both copies in
        # favour of btclib's, which read exactly throughout.
        version = int.from_bytes(read_exactly(stream, 4, "version"), "little")
        services = int.from_bytes(read_exactly(stream, 8, "services"), "little")
        timestamp = int.from_bytes(read_exactly(stream, 8, "timestamp"), "little")
        addr_recv = NetworkAddress.deserialize(stream, version_msg=True, addrv2=False)
        addr_from = NetworkAddress.deserialize(stream, version_msg=True, addrv2=False)
        nonce = int.from_bytes(read_exactly(stream, 8, "nonce"), "little")
        user_agent_len = var_int.parse(stream)
        user_agent = stream.read(user_agent_len).decode()
        start_height = int.from_bytes(read_exactly(stream, 4, "start height"), "little")
        octet = stream.read(1)
        relay = bool(octet[0]) if octet else None
        return cls(
            version=version,
            services=services,
            timestamp=timestamp,
            addr_recv=addr_recv,
            addr_from=addr_from,
            nonce=nonce,
            user_agent=user_agent,
            start_height=start_height,
            relay=relay,
        )

    def serialize(self, *, check_validity: bool = True) -> bytes:
        payload = self.version.to_bytes(4, "little")
        payload += self.services.to_bytes(8, "little")
        payload += self.timestamp.to_bytes(8, "little")
        payload += self.addr_recv.serialize(version_msg=True)
        payload += self.addr_from.serialize(version_msg=True)
        payload += self.nonce.to_bytes(8, "little")
        if self.user_agent:
            payload += var_int.serialize(len(self.user_agent))
            payload += self.user_agent.encode()
        else:
            payload += var_int.serialize(0)
        payload += self.start_height.to_bytes(4, "little")
        # written only if there is one, so that the payload a peer sent
        # without the flag is the payload this writes back
        if self.relay is not None:
            payload += self.relay.to_bytes(1, "little")
        return payload
