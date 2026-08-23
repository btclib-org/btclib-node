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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from typing import cast

from btclib.p2p.address import NetworkAddress, ServiceFlags, TimestampedNetworkAddress
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2

from btclib_node.chains import Chain
from btclib_node.db import KeyValueStore

# the two ids whose address field is an IP address, which is the whole
# of what an addr version 1 entry can carry, and the whole of what
# `dial` below opens a socket for
_IP_NETWORKS = (BIP155Network.IPV4, BIP155Network.IPV6)

# BIP155: a client SHOULD ignore an IPV6 entry whose sixteen octets fall
# in a range reserved for embedding another network's address into an
# IPv6 one -- the IPv4 mapping, `::ffff:0:0/96`, and OnionCat's
# `fd87:d87e:eb43::/48`, once used to carry a TORv2 address the same way.
# Core's `netaddress.h` (58a7869f86) is `IPV4_IN_IPV6_PREFIX` and
# `TORV2_IN_IPV6_PREFIX`; `IPv6Address.ipv4_mapped` is `ipaddress`'s own
# name for the first, and the second has no name of its own to borrow.
_ONIONCAT_PREFIX = b"\xfd\x87\xd8\x7e\xeb\x43"


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
    return address.network_id in _IP_NETWORKS


def _is_embedded_ipv6(address: NetworkAddressV2) -> bool:
    """Answer whether an `IPV6` record's octets are really another network's.

    The two BIP155 ignore rules `_ONIONCAT_PREFIX` documents, applied
    together: `PeerDB.add_addresses` is where this is asked, that being
    the point a record kept becomes an entry gossiped back to the next
    peer -- `btclib.p2p.addrv2`'s own docstring calls both rules receive
    policy and leaves them to the caller rather than the parser.
    """
    return address.network_id == BIP155Network.IPV6 and (
        IPv6Address(address.address).ipv4_mapped is not None
        or address.address.startswith(_ONIONCAT_PREFIX)
    )


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


# The old poll loop's budget, ten passes at 0.1s each -- what a dial
# gave up after regardless of whether the peer was refusing or merely
# slow. `loop.sock_connect` below does not need the two magic numbers a
# poll needs, only this one: a real timeout wrapped around a wait that
# is otherwise event-driven.
_DIAL_TIMEOUT = 1.0


async def dial(address: NetworkAddressV2) -> socket.socket | None:
    """Return a socket connected to the peer, or nothing if it never came up.

    `dial` and not `connect`, which is what `P2pManager` calls the whole
    of making a connection out of one: this is the socket alone.

    `loop.sock_connect` is the kernel's own answer rather than a guess at
    it: a refusal is `SO_ERROR` on the socket, read the moment the OS
    notifies the loop's writer callback, not inferred after a fixed
    number of `getpeername` polls that cannot tell a refusal from a peer
    that is merely slow. And where `connect` completes without ever
    raising `BlockingIOError` -- a local peer most often -- `sock_connect`
    returns at once instead of an `except` arm that never runs.

    No separate check for a host with no route to the family being
    dialled: `_DIAL_TIMEOUT` already bounds every attempt, and an
    unreachable family fails the same `sock_connect` a slow or refusing
    peer does, landing on the same `None` `P2pManager` already treats as
    "try someone else". Bitcoin Core's own default (`ReachableNets`,
    src/netbase.h at 58a7869f86: "Everything is reachable by default")
    is the same bet -- reachability is what a dial's outcome says it is,
    not a property guessed at beforehand -- so there is nothing here for
    a heavier check to buy.
    """
    if address.network_id not in _IP_NETWORKS:
        raise ValueError("Address type not yet supported")
    if address.network_id == BIP155Network.IPV4:
        family = socket.AF_INET
        host = str(IPv4Address(address.address))
    else:
        family = socket.AF_INET6
        host = str(IPv6Address(address.address))
    client = socket.socket(family, socket.SOCK_STREAM)
    client.settimeout(0)
    loop = asyncio.get_running_loop()
    peer = (host, address.port)
    try:
        await asyncio.wait_for(loop.sock_connect(client, peer), _DIAL_TIMEOUT)
    except OSError, TimeoutError:
        client.close()
        return None
    return client


# Two record kinds share the one store `PeerDB` opens, so `init_from_db`
# below walks it whole and dispatches on the prefix rather than stopping
# at the first key without one -- `btclib_node/db.py`'s own docstring
# names that shape as `BlockDB`'s, next to the other one, `BlockIndex`'s,
# that a store of one record kind can use instead.
_KNOWN = b"known-"
_ANSWERED = b"answered-"


def _endpoint(address: NetworkAddressV2) -> bytes:
    """Return the octets a persisted address is keyed on.

    The network id, the address and the port -- what names an endpoint
    on the wire -- and not `timestamp` or `services`: those are this
    node's own opinion of the endpoint, not part of what it is, so two
    records differing only in them settle on the one row written last
    rather than growing the table an entry per gossip or per reconnect.
    """
    endpoint = replace(address, timestamp=0, services=ServiceFlags.NODE_NONE)
    return endpoint.serialize(check_validity=False)


class PeerDB:
    def __init__(self, chain: Chain, data_dir: Path | None) -> None:
        self.chain = chain
        self.data_dir = data_dir
        self.addresses: set[NetworkAddressV2] = set()
        self.active_addresses: list[NetworkAddressV2] = []

        # `None` is a table kept in memory only, which is what every
        # test here wants and what `data_dir` was before this: assigned
        # and never opened. `Node` always passes an actual directory
        # (`Config.data_dir` has no `None` of its own), so this is the
        # one place that distinction is made.
        self.db = KeyValueStore(data_dir / "peers") if data_dir is not None else None

        self.init_from_db()
        # DNS is asked only where the durable table came back with
        # nothing this node has itself confirmed working recently:
        # `get_active_addresses` is what "recently" already means, and
        # `can_connect` is what catches a table `add_addresses` filled
        # with tor, i2p or an ipv6-only answer from a seed -- #89, where
        # a nonempty table was exactly the case DNS was skipped for and
        # none of it was dialable.
        self.ask_dns_nodes = not any(
            can_connect(address) for address in self.get_active_addresses()
        )

    def init_from_db(self) -> None:
        if self.db is None:
            return
        for key, value in self.db:
            if key.startswith(_KNOWN):
                self.addresses.add(NetworkAddressV2.parse(value, check_validity=False))
            elif key.startswith(_ANSWERED):
                self.active_addresses.append(
                    NetworkAddressV2.parse(value, check_validity=False)
                )

    @contextmanager
    def _write_batch(self) -> Iterator[KeyValueStore | None]:
        """Yield a batch to write into, or `None` where there is nothing to.

        One shape either way, so a caller writes `if wb is not None`
        around the puts it makes and nothing around the batch itself --
        the alternative, a call site branching on `self.db` before ever
        reaching a loop, is what this exists to not be.
        """
        if self.db is None:
            yield None
            return
        with self.db.write_batch() as wb:
            yield wb

    def close(self) -> None:
        if self.db is not None:
            self.db.close()

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
        # through add_addresses, and not a bare add to the set: a seed
        # is gossip like a peer's is, and belongs in the durable table
        # the same way, so a later restart has it without asking again
        self.add_addresses(peer_address(ip, port) for ip, port in endpoints)

    @property
    def is_empty(self) -> bool:
        return not len(self.addresses)

    def random_address(self) -> NetworkAddressV2 | None:
        # Preferred: an address this node has itself dialled and heard
        # back from recently, over one merely gossiped -- #123, so that
        # a run draws on what it already knows works rather than on the
        # whole table uniformly, even before a restart ever reads any
        # of it back.
        preferred = [addr for addr in self.get_active_addresses() if can_connect(addr)]
        if preferred:
            return secrets.choice(preferred)
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
        with self._write_batch() as wb:
            for address in addresses:
                if len(self.addresses) >= 10000:
                    break
                # BIP155's ignore rule: an IPV6 record that is really an
                # IPv4 or a (long-retired) TORv2 address wearing another
                # network's sixteen octets is not a second peer, and
                # keeping it under network id 2 is what used to gossip
                # it back as IPv4 -- the same host, twice in the table
                # (#151). Checked before the durable write too, so a
                # dropped record is dropped everywhere, not merely kept
                # out of the in-memory set.
                if _is_embedded_ipv6(address):
                    continue
                known = replace(address, timestamp=0)
                self.addresses.add(known)
                if wb is not None:
                    value = known.serialize(check_validity=False)
                    wb.put(_KNOWN + _endpoint(known), value)

    def get_active_addresses(self) -> list[NetworkAddressV2]:
        now = time.time()
        # active if seen within the last three hours
        self.active_addresses = [
            addr for addr in self.active_addresses if now - addr.timestamp < 3600 * 3
        ]
        return self.active_addresses

    def add_active_address(self, addr: NetworkAddressV2) -> None:
        # a whole second: the field is four octets on the wire
        answered = replace(addr, timestamp=int(time.time()))
        self.active_addresses.append(answered)
        if self.db is not None:
            self.db.put(
                _ANSWERED + _endpoint(answered),
                answered.serialize(check_validity=False),
            )
