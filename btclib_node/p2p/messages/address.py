# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from dataclasses import dataclass

from btclib import var_int
from btclib.utils import bytesio_from_binarydata

from btclib_node.p2p.address import NetworkAddress
from btclib_node.p2p.messages import add_headers


@dataclass
class Addr:
    addresses: list[NetworkAddress]

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        len_addresses = var_int.parse(stream)
        addresses = []
        for x in range(len_addresses):
            addresses.append(NetworkAddress.deserialize(stream, addrv2=False))
        return cls(addresses=addresses)

    def serialize(self):
        payload = var_int.serialize(len(self.addresses))
        for address in self.addresses:
            payload += address.serialize(addrv2=False)
        return add_headers("addr", payload)


@dataclass
class AddrV2:
    addresses: list[NetworkAddress]

    @classmethod
    def deserialize(cls, data):
        stream = bytesio_from_binarydata(data)
        len_addresses = var_int.parse(stream)
        addresses = []
        for x in range(len_addresses):
            addresses.append(NetworkAddress.deserialize(stream, addrv2=True))
        return cls(addresses=addresses)

    def serialize(self):
        payload = var_int.serialize(len(self.addresses))
        for address in self.addresses:
            payload += address.serialize(addrv2=True)
        return add_headers("addrv2", payload)


@dataclass
class Getaddr:
    @classmethod
    def deserialize(cls, data):
        return cls()

    def serialize(self):
        return add_headers("getaddr", b"")
