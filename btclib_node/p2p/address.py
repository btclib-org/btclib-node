# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio
import enum
import secrets
import socket
import time
from dataclasses import dataclass

from btclib import var_bytes, var_int
from btclib.utils import bytesio_from_binarydata


class NetworkID(enum.IntEnum):
    ipv4 = 1
    ipv6 = 2
    torv2 = 3
    torv3 = 4
    i2p = 5
    cjdns = 6

    @property
    def addr_bytesize(self):
        return _ADDR_BYTESIZE[self]

    @property
    def can_addrv1(self):
        if self in (NetworkID.ipv4, NetworkID.ipv6):
            return True
        return False


# BIP155 fixes the length an address of each network has on the wire. A
# member added without an entry raises here, where a chain of ifs ending
# in a bare raise said the same thing in more lines and one of them dead.
_ADDR_BYTESIZE = {
    NetworkID.ipv4: 4,
    NetworkID.ipv6: 16,
    NetworkID.torv2: 10,
    NetworkID.torv3: 32,
    NetworkID.i2p: 32,
    NetworkID.cjdns: 16,
}


@dataclass(frozen=True)
class NetworkAddress:
    time: int = 0
    services: int = 0
    netid: NetworkID = NetworkID.ipv4
    addr: bytes = b"\x00\x00\x00\x00"
    port: int = 0

    @classmethod
    def from_ip_and_port(cls, ip: str, port: int, time: int = 0, services: int = 0):
        try:
            addr = socket.inet_pton(socket.AF_INET, ip)
            netid = NetworkID.ipv4
        except OSError:
            addr = socket.inet_pton(socket.AF_INET6, ip)
            netid = NetworkID.ipv6
        return cls(time=time, services=services, netid=netid, addr=addr, port=port)

    @classmethod
    def deserialize(cls, data, version_msg=False, addrv2=False):
        stream = bytesio_from_binarydata(data)
        time = 0 if version_msg else int.from_bytes(stream.read(4), "little")
        if addrv2:
            services = var_int.parse(stream)
            netid = NetworkID.from_bytes(stream.read(1), "little")
            addr = var_bytes.parse(stream)
            if len(addr) != netid.addr_bytesize:
                raise ValueError("Invalid address byte length")
        else:
            services = int.from_bytes(stream.read(8), "little")
            addr = stream.read(16)
            if addr.startswith(b"\x00" * 10 + b"\xff" * 2):
                netid = NetworkID.ipv4
                addr = addr[12:]
            else:
                netid = NetworkID.ipv6
        port = int.from_bytes(stream.read(2), "big")
        return cls(time=time, services=services, netid=netid, addr=addr, port=port)

    def serialize(self, version_msg=False, addrv2=False):
        if len(self.addr) != self.netid.addr_bytesize:
            raise ValueError
        payload = b""
        if not version_msg:
            payload += self.time.to_bytes(4, "little")
        if addrv2:
            payload += var_int.serialize(self.services)
            payload += self.netid.to_bytes(1, "big")
            payload += var_bytes.serialize(self.addr)
        else:
            if not self.netid.can_addrv1:
                err_msg = (
                    "This type of address cannot be serialized for "
                    "addr version 1 message"
                )
                raise ValueError(err_msg)
            payload += self.services.to_bytes(8, "little")
            payload += (
                b"" if self.netid == NetworkID.ipv6 else b"\x00" * 10 + b"\xff" * 2
            )
            payload += self.addr
        payload += self.port.to_bytes(2, "big")
        return payload

    @property
    def can_connect(self):
        if self.netid == NetworkID.ipv4:
            return True
        return False

    async def connect(self):
        if self.netid == NetworkID.ipv4:
            family = socket.AF_INET
            client = socket.socket(family, socket.SOCK_STREAM)
            client.settimeout(0)
            try:
                client.connect((socket.inet_ntop(family, self.addr), self.port))
            except BlockingIOError:
                for _ in range(10):
                    await asyncio.sleep(0.1)
                    try:
                        client.getpeername()
                        return client
                    except OSError:
                        pass
                client.close()
        else:
            raise ValueError("Address type not yet supported")

    def __repr__(self):
        if self.netid == NetworkID.ipv4:
            return f"{socket.inet_ntop(socket.AF_INET, self.addr)}:{self.port}"
        if self.netid == NetworkID.ipv6:
            return f"{socket.inet_ntop(socket.AF_INET6, self.addr)}:{self.port}"
        return f"{self.addr.hex()}:{self.port}"


class PeerDB:
    def __init__(self, chain, data_dir):
        self.chain = chain
        self.data_dir = data_dir
        self.addresses = set()
        self.active_addresses = []

        self.init_from_db()
        self.ask_dns_nodes = self.is_empty

    def init_from_db(self):
        pass

    async def get_addr_from_dns(self):
        if not self.ask_dns_nodes:
            return
        chain = self.chain
        loop = asyncio.get_running_loop()
        addresses = []
        for dns_server in chain.addresses:
            try:
                ips = await loop.getaddrinfo(dns_server, chain.port)
            except socket.gaierror:
                continue
            for ip in ips:
                addresses.append(ip[4])
        for ip, port, *_ in set(addresses):
            address = NetworkAddress.from_ip_and_port(ip, port)
            self.addresses.add(address)

    @property
    def is_empty(self):
        return not len(self.addresses)

    def random_address(self):
        while True:
            address = secrets.choice(list(self.addresses))
            if address.can_connect:
                return address

    def add_addresses(self, addresses):
        for address in addresses:
            if len(self.addresses) >= 10000:
                break
            new_addr = NetworkAddress(
                time=0,
                services=address.services,
                netid=address.netid,
                addr=address.addr,
                port=address.port,
            )
            if new_addr not in self.addresses:
                self.addresses.add(new_addr)

    def get_active_addresses(self):
        now = time.time()
        # active if seen within the last three hours
        self.active_addresses = [
            addr for addr in self.active_addresses if now - addr.time < 3600 * 3
        ]
        return self.active_addresses

    def add_active_address(self, addr):
        active_addr = NetworkAddress(
            # a whole second: the field is four octets on the wire
            time=int(time.time()),
            services=addr.services,
            netid=addr.netid,
            addr=addr.addr,
            port=addr.port,
        )
        self.active_addresses.append(active_addr)
