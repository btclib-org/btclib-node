# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Where a peer is, and the table of the ones this node knows of.

The address itself is btclib's, and `btclib.p2p.addrv2.NetworkAddressV2`
is the one this node holds a peer in: BIP155's record is the only
encoding that carries every network a peer can be on, so the narrower
`addr` entry would lose an onion peer the moment one is gossiped. What
goes on the wire is that entry all the same wherever the peer has not
asked for BIP155, and `addr_entry` and `peer_from_addr_entry` are the
translation.

What is left here is what btclib has no business holding: dialling a
socket, and the table of addresses to dial. btclib is a codec -- it
speaks to nobody -- so the question "can this be connected to" and the
answer to it are this node's.
"""

import asyncio
import secrets
import socket
import time
from collections.abc import Iterable
from dataclasses import replace
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from typing import cast

from btclib.p2p.address import NetworkAddress, TimestampedNetworkAddress
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2

from btclib_node.chains import Chain

# the two ids whose address field is an IP address, which is the whole
# of what an addr version 1 entry can carry -- and, of those two, the
# one this node has a dial for
_IP_NETWORKS = (BIP155Network.IPV4, BIP155Network.IPV6)
_DIALABLE = BIP155Network.IPV4


def peer_address(
    ip: str, port: int, timestamp: int = 0, services: int = 0
) -> NetworkAddressV2:
    """Return the BIP155 record of a peer named by the text of its IP.

    `ipaddress.ip_address` is what tells the two IP networks apart, and
    the octets it packs are what BIP155 asks for: four for a v4 peer and
    sixteen for a v6 one, where an `addr` entry would carry the v4 one
    mapped into sixteen.
    """
    parsed = ip_address(ip)
    network_id = BIP155Network.IPV4 if parsed.version == 4 else BIP155Network.IPV6
    return NetworkAddressV2(timestamp, services, network_id, parsed.packed, port)


def can_addrv1(address: NetworkAddressV2) -> bool:
    """Answer whether an addr version 1 message has room for this peer."""
    return address.network_id in _IP_NETWORKS


def can_connect(address: NetworkAddressV2) -> bool:
    """Answer whether this node has a dial for the peer's network."""
    return address.network_id == _DIALABLE


def network_address(address: NetworkAddressV2) -> NetworkAddress:
    """Return the untimestamped form of a BIP155 record, where it has one.

    What a `version` message's two addresses are, and what an `addr`
    entry is built on. `can_addrv1` is the question a caller asks first;
    the refusal below is what makes the answer binding rather than
    advisory, because the length would not catch it: BIP155 gives cjdns
    and yggdrasil the sixteen octets an IPv6 address has, so `IPv6Address`
    would take either for an IP address and hand back a peer that is not
    the one that was gossiped.
    """
    if not can_addrv1(address):
        err_msg = f"not an ip address: network id {int(address.network_id)}"
        raise ValueError(err_msg)
    ip = (
        IPv4Address(address.address)
        if address.network_id == BIP155Network.IPV4
        else IPv6Address(address.address)
    )
    return NetworkAddress(address.services, ip, address.port)


def addr_entry(address: NetworkAddressV2) -> TimestampedNetworkAddress:
    """Return the addr version 1 entry a BIP155 record is, where it is one."""
    return TimestampedNetworkAddress(address.timestamp, network_address(address))


def peer_from_addr_entry(entry: TimestampedNetworkAddress) -> NetworkAddressV2:
    """Return the BIP155 record an addr version 1 entry describes.

    An `addr` entry holds every address in sixteen octets, a v4 one
    mapped into them, where BIP155 gives the two networks different ids
    and different lengths: `ipv4_mapped` is what tells them apart, and it
    is the reason this is not a field rename.
    """
    ip = entry.address.ip
    mapped = ip.ipv4_mapped
    network_id = BIP155Network.IPV4 if mapped else BIP155Network.IPV6
    return NetworkAddressV2(
        entry.timestamp,
        entry.address.services,
        network_id,
        mapped.packed if mapped else ip.packed,
        entry.address.port,
    )


def ip_and_port(ip: str, port: int) -> str:
    """Return the endpoint the way Core's `CService::ToStringAddrPort` does.

    `"[" + ToStringAddr() + "]:" + port_str` for every network that
    function's `IsIPv4() || IsTor() || IsI2P() || IsInternal()` does not
    name. The brackets are what tells a v6 host from its port:
    `2001:db8::1` on port 8333 and `2001:db8::1:8333` on some other port
    are both addresses, and without brackets both render as the second.

    The host's text rather than the `NetworkAddress` a peer is held in,
    because a socket's `getpeername` has no such object to offer and
    answers with this.

    A v4-mapped host is unwrapped rather than bracketed, which is Core's
    answer too: `CNetAddr::SetLegacyIPv6` files a mapped address under
    NET_IPV4, which that predicate names. Without the unwrapping a v4
    peer would read `[::ffff:1.2.3.4]:8333`, a `NetworkAddress` holding
    every address in the sixteen octets of an IPv6 one.

    Raises `ValueError` where the host is not an IP address, which is
    what `ipaddress.ip_address` answers with: a hostname is refused
    rather than shown with brackets guessed at.
    """
    parsed = ip_address(ip)
    if not isinstance(parsed, IPv6Address):
        return f"{parsed}:{port}"
    mapped = parsed.ipv4_mapped
    if mapped:
        return f"{mapped}:{port}"
    return f"[{parsed}]:{port}"


async def dial(address: NetworkAddressV2) -> socket.socket | None:
    """Return a socket connected to the peer, or nothing if it never came up.

    `dial` and not `connect`, which is what `P2pManager` calls the whole
    of making a connection out of one: this is the socket alone.
    """
    if address.network_id != _DIALABLE:
        raise ValueError("Address type not yet supported")
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(0)
    try:
        client.connect((str(IPv4Address(address.address)), address.port))
    except BlockingIOError:
        for _ in range(10):
            await asyncio.sleep(0.1)
            try:
                client.getpeername()
                return client
            except OSError:
                pass
        client.close()
    return None


class PeerDB:
    def __init__(self, chain: Chain, data_dir: Path) -> None:
        self.chain = chain
        self.data_dir = data_dir
        self.addresses: set[NetworkAddressV2] = set()
        self.active_addresses: list[NetworkAddressV2] = []

        self.init_from_db()
        self.ask_dns_nodes = self.is_empty

    def init_from_db(self) -> None:
        pass

    async def get_addr_from_dns(self) -> None:
        if not self.ask_dns_nodes:
            return
        chain = self.chain
        loop = asyncio.get_running_loop()
        # what a seed answers with, deduplicated: seeds overlap, and one
        # of them answers with the same host over several records.
        endpoints: set[tuple[str, int]] = set()
        for dns_server in chain.addresses:
            try:
                answers = await loop.getaddrinfo(dns_server, chain.port)
            except socket.gaierror:
                continue
            # (family, type, proto, canonname, sockaddr), and the
            # sockaddr is the only part a peer table wants. It opens
            # with the host and the port -- two fields for AF_INET,
            # four for AF_INET6, whose flow info and scope id say
            # nothing a BIP155 record holds. The stub also admits
            # AF_PACKET's (protocol, address) pair, which resolving an
            # internet host and a port cannot answer with, so the cast
            # is what that fact is written as rather than a check no
            # test could reach.
            for *_, sockaddr in answers:
                endpoints.add(cast("tuple[str, int]", sockaddr[:2]))
        for ip, port in endpoints:
            self.addresses.add(peer_address(ip, port))

    @property
    def is_empty(self) -> bool:
        return not len(self.addresses)

    def random_address(self) -> NetworkAddressV2 | None:
        # Drawn from the addresses that can be dialled, rather than from
        # the whole table with a retry on the ones that cannot: a table
        # holding none of them -- a seed answering with AAAA records
        # alone is enough, and `add_addresses` takes whatever tor, i2p
        # and cjdns a peer sends -- made that retry a loop with no exit,
        # in the caller's event loop. Nothing to dial is an answer, and
        # `None` is it.
        dialable = [address for address in self.addresses if can_connect(address)]
        if not dialable:
            return None
        return secrets.choice(dialable)

    def add_addresses(self, addresses: Iterable[NetworkAddressV2]) -> None:
        # a peer's word for when it last saw an address is not evidence,
        # and keeping it would make the one address several entries
        for address in addresses:
            if len(self.addresses) >= 10000:
                break
            self.addresses.add(replace(address, timestamp=0))

    def get_active_addresses(self) -> list[NetworkAddressV2]:
        now = time.time()
        # active if seen within the last three hours
        self.active_addresses = [
            addr for addr in self.active_addresses if now - addr.timestamp < 3600 * 3
        ]
        return self.active_addresses

    def add_active_address(self, addr: NetworkAddressV2) -> None:
        # a whole second: the field is four octets on the wire
        self.active_addresses.append(replace(addr, timestamp=int(time.time())))
