# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Which inbound peer a full `P2pManager` disconnects to make room.

`select_node_to_evict` is Core's `SelectNodeToEvict` and
`protect_eviction_candidates_by_ratio` its
`ProtectEvictionCandidatesByRatio` (`src/node/eviction.cpp`), and
`net_group` is `NetGroupManager::GetGroup` with no asmap loaded
(`src/netgroup.cpp`), all read at bitcoin/bitcoin@9be056a8a7, the v31.1
tag. The selection is pure: it reads `EvictionCandidate` records and
returns an id, and `P2pManager` is what builds the records from its
connections and disconnects the peer chosen.

Core sorts each protection step with `std::sort`, which leaves the order
of two equal candidates unspecified; `list.sort` is stable, so a tie
here keeps the order the candidates arrived in. Where Core's own tests
depend on a tie, they depend on `std::stable_sort` over the disadvantaged
networks, which `list.sort` reproduces.
"""

import hashlib
from dataclasses import dataclass
from enum import IntEnum
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import TYPE_CHECKING, Any

from btclib.p2p.addrv2 import BIP155Network, can_addrv1, network_address

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from btclib.p2p.addrv2 import NetworkAddressV2

__all__ = [
    "EvictionCandidate",
    "Network",
    "get_network",
    "is_local",
    "is_routable",
    "is_valid",
    "keyed_net_group",
    "net_class",
    "net_group",
    "protect_eviction_candidates_by_ratio",
    "select_node_to_evict",
]


class Network(IntEnum):
    """Core's `enum Network` (`src/netaddress.h`), in its own order."""

    UNROUTABLE = 0
    IPV4 = 1
    IPV6 = 2
    ONION = 3
    I2P = 4
    CJDNS = 5
    INTERNAL = 6
    MAX = 7


@dataclass(frozen=True, slots=True)
class EvictionCandidate:
    """Core's `NodeEvictionCandidate` (`src/node/eviction.h`), field for field.

    Times are whole seconds since the epoch, as Core's are, and 0 where
    nothing has happened yet; `min_ping_time` is seconds and infinite
    until a ping is answered, Core's `microseconds::max()`. `inbound`
    stands for `m_conn_type == ConnectionType::INBOUND`, this node having
    no other connection type an eviction would treat differently.
    """

    id: int
    connected: int
    min_ping_time: float
    last_block_time: int
    last_tx_time: int
    relevant_services: bool
    relay_txs: bool
    bloom_filter: bool
    keyed_net_group: int
    prefer_evict: bool
    is_local: bool
    network: Network
    noban: bool
    inbound: bool


def _erase_last_k(
    candidates: list[EvictionCandidate],
    key: Callable[[EvictionCandidate], Any],
    k: int,
    predicate: Callable[[EvictionCandidate], bool] = lambda _: True,
) -> None:
    """Core's `EraseLastKElements`: sort, then drop the last `k` matching.

    `key` sorts ascending the way Core's comparator does, so the last
    elements are the ones the step protects.
    """
    candidates.sort(key=key)
    start = len(candidates) - min(k, len(candidates))
    candidates[start:] = [c for c in candidates[start:] if not predicate(c)]


def _reverse_connected(c: EvictionCandidate) -> int:
    # `ReverseCompareNodeTimeConnected`: the longest connected last
    return -c.connected


def _net_group_keyed(c: EvictionCandidate) -> int:
    # `CompareNetGroupKeyed`
    return c.keyed_net_group


def _reverse_min_ping(c: EvictionCandidate) -> float:
    # `ReverseCompareNodeMinPingTime`: the lowest ping last
    return -c.min_ping_time


def _block_time(c: EvictionCandidate) -> tuple[int, bool, int]:
    # `CompareNodeBlockTime`
    return (c.last_block_time, c.relevant_services, -c.connected)


def _tx_time(c: EvictionCandidate) -> tuple[int, bool, bool, int]:
    # `CompareNodeTXTime`: a bloom filter sorts ahead of none
    return (c.last_tx_time, c.relay_txs, not c.bloom_filter, -c.connected)


def _block_relay_only_time(c: EvictionCandidate) -> tuple[bool, int, bool, int]:
    # `CompareNodeBlockRelayOnlyTime`: a tx-relaying peer sorts first
    return (not c.relay_txs, c.last_block_time, c.relevant_services, -c.connected)


@dataclass(slots=True)
class _Net:
    """One entry of `ProtectEvictionCandidatesByRatio`'s `networks` array."""

    is_local: bool
    id: Network
    count: int = 0

    def holds(self, c: EvictionCandidate) -> bool:
        """Whether `c` belongs to this disadvantaged network."""
        return c.is_local if self.is_local else c.network == self.id

    def network_time(self, c: EvictionCandidate) -> tuple[bool, bool, int]:
        """`CompareNodeNetworkTime`: this network's peers last, oldest last."""
        return (self.is_local and c.is_local, c.network == self.id, -c.connected)


def protect_eviction_candidates_by_ratio(candidates: list[EvictionCandidate]) -> None:
    """Protect half of `candidates` by uptime, a quarter by network first.

    Core's `ProtectEvictionCandidatesByRatio`, removing in place what it
    protects. Onion, I2P and CJDNS peers never reach it from this node,
    which dials and listens on IPv4 and IPv6 alone, so of the four
    disadvantaged networks only localhost ever holds one of its peers.
    """
    initial_size = len(candidates)
    total_protect_size = initial_size // 2
    networks = [
        _Net(is_local=False, id=Network.CJDNS),
        _Net(is_local=False, id=Network.I2P),
        _Net(is_local=True, id=Network.MAX),
        _Net(is_local=False, id=Network.ONION),
    ]
    for n in networks:
        n.count = sum(n.holds(c) for c in candidates)
    networks.sort(key=lambda n: n.count)

    max_protect_by_network = total_protect_size // 2
    num_protected = 0
    while num_protected < max_protect_by_network:
        num_networks = sum(1 for n in networks if n.count)
        if num_networks == 0:
            break
        disadvantaged_to_protect = max_protect_by_network - num_protected
        protect_per_network = max(disadvantaged_to_protect // num_networks, 1)
        protected_at_least_one = False
        for n in networks:
            if n.count == 0:
                continue
            before = len(candidates)
            _erase_last_k(candidates, n.network_time, protect_per_network, n.holds)
            delta = before - len(candidates)
            if delta:
                protected_at_least_one = True
                num_protected += delta
                if num_protected >= max_protect_by_network:
                    break
                n.count -= delta
        if not protected_at_least_one:
            break

    _erase_last_k(candidates, _reverse_connected, total_protect_size - num_protected)


def select_node_to_evict(candidates: Iterable[EvictionCandidate]) -> int | None:
    """Return the id of the peer to evict, `None` where every one is protected.

    Core's `SelectNodeToEvict`, step for step and with Core's own counts.
    """
    remaining = [c for c in candidates if not c.noban and c.inbound]
    _erase_last_k(remaining, _net_group_keyed, 4)
    _erase_last_k(remaining, _reverse_min_ping, 8)
    _erase_last_k(remaining, _tx_time, 4)
    _erase_last_k(
        remaining,
        _block_relay_only_time,
        8,
        lambda c: not c.relay_txs and c.relevant_services,
    )
    _erase_last_k(remaining, _block_time, 4)
    protect_eviction_candidates_by_ratio(remaining)
    if not remaining:
        return None
    if any(c.prefer_evict for c in remaining):
        remaining = [c for c in remaining if c.prefer_evict]

    # The netgroup holding the most candidates, a tie going to the one
    # whose youngest member connected last; `remaining` is sorted
    # youngest first, so each group's own first entry is its youngest.
    groups: dict[int, list[EvictionCandidate]] = {}
    most_connections = 0
    most_connections_time = 0
    most_connections_group = remaining[0].keyed_net_group
    for c in remaining:
        group = groups.setdefault(c.keyed_net_group, [])
        group.append(c)
        group_time = group[0].connected
        if len(group) > most_connections or (
            len(group) == most_connections and group_time > most_connections_time
        ):
            most_connections = len(group)
            most_connections_time = group_time
            most_connections_group = c.keyed_net_group
    return groups[most_connections_group][0].id


type _IP = IPv4Address | IPv6Address

# `CNetAddr::IsLocal`
_LOCAL = (IPv4Network("127.0.0.0/8"), IPv4Network("0.0.0.0/8"), IPv6Network("::1/128"))
# What `CNetAddr::IsValid` refuses of an IP address, `NET_INTERNAL` aside
_INVALID = (
    IPv4Network("0.0.0.0/32"),  # INADDR_ANY
    IPv4Network("255.255.255.255/32"),  # INADDR_NONE
    IPv6Network("::/128"),  # unspecified
    IPv6Network("2001:db8::/32"),  # RFC3849
)
# What `CNetAddr::IsRoutable` refuses, beside the local and the invalid
# addresses above
_UNROUTABLE = (
    IPv4Network("10.0.0.0/8"),  # RFC1918
    IPv4Network("172.16.0.0/12"),  # RFC1918
    IPv4Network("192.168.0.0/16"),  # RFC1918
    IPv4Network("198.18.0.0/15"),  # RFC2544
    IPv4Network("169.254.0.0/16"),  # RFC3927
    IPv4Network("100.64.0.0/10"),  # RFC6598
    IPv4Network("192.0.2.0/24"),  # RFC5737
    IPv4Network("198.51.100.0/24"),  # RFC5737
    IPv4Network("203.0.113.0/24"),  # RFC5737
    IPv6Network("fe80::/64"),  # RFC4862
    IPv6Network("fc00::/7"),  # RFC4193
    IPv6Network("2001:10::/28"),  # RFC4843
    IPv6Network("2001:20::/28"),  # RFC7343
)
# The IPv6 ranges `CNetAddr::HasLinkedIPv4` reads an IPv4 address out of
_SIIT = IPv6Network("::ffff:0:0:0/96")  # RFC6145
_NAT64 = IPv6Network("64:ff9b::/96")  # RFC6052
_6TO4 = IPv6Network("2002::/16")  # RFC3964
_TEREDO = IPv6Network("2001::/32")  # RFC4380
_HE_NET = IPv6Network("2001:470::/32")
# `INTERNAL_IN_IPV6_PREFIX`: `SetLegacyIPv6` parses an IPv6 address under
# it as `NET_INTERNAL`, whatever the peer meant by it
_INTERNAL = IPv6Network("fd6b:88c0:8724::/48")
# `TORV2_IN_IPV6_PREFIX`: `SetLegacyIPv6` reads an IPv6 address under it
# as the unspecified address
_TORV2 = IPv6Network("fd87:d87e:eb43::/48")
# `CJDNS_PREFIX`, the first octet `CNetAddr::IsValid` asks of a CJDNS address
_CJDNS_PREFIX = 0xFC


def _ip(address: NetworkAddressV2) -> _IP:
    """Return the peer's IP, an IPv4 one mapped into IPv6 taken as IPv4.

    `network_address` gives every IP address as IPv6, an IPv4 one
    mapped; Core parses a mapped address as IPv4
    (`CNetAddr::SetLegacyIPv6`), and so does this.
    """
    ip = network_address(address).ip
    return ip.ipv4_mapped or ip


def _is_valid(ip: _IP) -> bool:
    """`CNetAddr::IsValid` for IPv4 and IPv6, `NET_INTERNAL` aside."""
    return not any(ip in net for net in _INVALID)


def _is_routable(ip: _IP) -> bool:
    """`CNetAddr::IsRoutable` of an IP: `_is_valid`, in none of its ranges."""
    return _is_valid(ip) and not any(ip in net for net in (*_LOCAL, *_UNROUTABLE))


def _linked_ipv4(ip: _IP) -> IPv4Address | None:
    """`CNetAddr::GetLinkedIPv4`, or `None` where `HasLinkedIPv4` is false.

    For a routable `ip` alone: `HasLinkedIPv4` is `IsRoutable()` and the
    ranges below, and both callers ask `_is_routable` first.
    """
    if isinstance(ip, IPv4Address):
        return ip
    packed = ip.packed
    if ip in _SIIT or ip in _NAT64:
        return IPv4Address(packed[12:])
    if ip in _6TO4:
        return IPv4Address(packed[2:6])
    if ip in _TEREDO:
        return IPv4Address(bytes(b ^ 0xFF for b in packed[12:]))
    return None


def is_valid(ip: IPv6Address) -> bool:
    """Core's `CNetAddr::IsValid` of the sixteen octets of an `addr` v1 field.

    What `CNetAddr::V1` deserializes, through `SetLegacyIPv6`: a mapped
    IPv4 address read as IPv4, one under the internal prefix as
    `NET_INTERNAL`, which is invalid, and one under the Tor v2 prefix as
    the unspecified address, which is invalid too.
    """
    if ip in _INTERNAL or ip in _TORV2:
        return False
    return _is_valid(ip.ipv4_mapped or ip)


def is_local(address: NetworkAddressV2) -> bool:
    """Core's `CNetAddr::IsLocal`, what `m_is_local` is read from."""
    ip = _ip(address)
    return any(ip in net for net in _LOCAL)


def is_routable(address: NetworkAddressV2) -> bool:
    """Core's `CNetAddr::IsRoutable`, what `AddrManImpl::AddSingle` asks first.

    An IP address is `_is_routable`'s to answer rather than `is_valid`'s,
    which reads the octets of an `addr` v1 field: `_ip` reads a mapped
    IPv4 address as `is_valid` does, and the prefixes `is_valid` refuses
    ahead of `_is_valid` lie inside RFC4193's range, which `_is_routable`
    refuses too. A TORv3 or an I2P address is routable, and a CJDNS one
    under Core's `CJDNS_PREFIX` alone: that prefix is the clause of
    `IsValid` about a CJDNS address, and no range of `IsRoutable` reaches
    one. Core decodes any other network id as an address `IsValid`
    refuses (`CNetAddr::UnserializeV2Stream`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), BIP155's TORv2 id and
    Yggdrasil's included. An IPv6 record embedding IPv4 or Tor v2, which
    Core decodes as invalid too, is left to `is_embedded_ipv6`, run ahead
    of this.
    """
    if can_addrv1(address):
        return _is_routable(_ip(address))
    if address.network_id in (BIP155Network.TORV3, BIP155Network.I2P):
        return True
    return (
        address.network_id == BIP155Network.CJDNS
        and address.address[0] == _CJDNS_PREFIX
    )


def net_class(address: NetworkAddressV2) -> Network:
    """Core's `CNetAddr::GetNetClass`, what `m_network` is read from.

    `NET_INTERNAL` answers for an IPv6 address under Core's internal
    prefix, as it does in Core, where accepting a peer parses its
    address through `SetLegacyIPv6`.
    """
    ip = _ip(address)
    if ip in _INTERNAL:
        return Network.INTERNAL
    if not _is_routable(ip):
        return Network.UNROUTABLE
    if _linked_ipv4(ip) is not None:
        return Network.IPV4
    return Network.IPV6


# `CNetAddr::m_net` of each BIP155 id `is_routable` can answer for
_NETWORK_OF_ID: dict[int, Network] = {
    BIP155Network.IPV4: Network.IPV4,
    BIP155Network.IPV6: Network.IPV6,
    BIP155Network.TORV3: Network.ONION,
    BIP155Network.I2P: Network.I2P,
    BIP155Network.CJDNS: Network.CJDNS,
}


def get_network(address: NetworkAddressV2) -> Network:
    """Core's `CNetAddr::GetNetwork`, what `m_network_conn_counts` is keyed on.

    Unlike `net_class`, an IPv6 address carrying an IPv4 one is IPv6.
    """
    if can_addrv1(address) and _ip(address) in _INTERNAL:
        return Network.INTERNAL
    if not is_routable(address):
        return Network.UNROUTABLE
    return _NETWORK_OF_ID[address.network_id]


def net_group(address: NetworkAddressV2) -> bytes:
    """Core's `NetGroupManager::GetGroup` with no asmap: the peer's netgroup.

    The net class, then IPv4's `/16` (an IPv6 address carrying an IPv4
    one counting as that IPv4 address), he.net's `/36` or the rest of
    IPv6's `/32`. Every local and every other unroutable address shares
    the one group `00`, `NET_UNROUTABLE`'s class alone, while an internal
    address is a group of its own, the ten octets after its prefix. Core
    buckets by autonomous system instead where `-asmap` is given, which
    this node has no counterpart to.
    """
    ip = _ip(address)
    group = bytes([net_class(address)])
    if ip in _INTERNAL:
        return group + ip.packed[6:]
    if not _is_routable(ip):
        return group
    ipv4 = _linked_ipv4(ip)
    if ipv4 is not None:
        return group + ipv4.packed[:2]
    packed = ip.packed
    if ip in _HE_NET:
        # `/36`: the fifth octet's low four bits set, as Core sets them
        return group + packed[:4] + bytes([packed[4] | 0x0F])
    return group + packed[:4]


def keyed_net_group(key: bytes, address: NetworkAddressV2) -> int:
    """Core's `CConnman::CalculateKeyedNetGroup`: `net_group`, keyed-hashed.

    Core keys a SipHash with this node's own random seeds, so a peer
    cannot predict which netgroups sort last and are protected; a keyed
    BLAKE2b under `key`, `P2pManager`'s own random draw, is the same
    property from what `hashlib` offers.
    """
    digest = hashlib.blake2b(
        net_group(address), digest_size=8, key=key, person=b"netgroup"
    ).digest()
    return int.from_bytes(digest, "little")
