# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The address this node tells a peer it is reachable at, as Core's.

`reachability` is `CNetAddr::GetReachabilityFrom` (`src/netaddress.cpp`)
and `address_for_peer` is `GetLocal` and `GetLocalAddrForPeer`
(`src/net.cpp`), both read at bitcoin/bitcoin@f0de87f040, the v32.1 tag.
An onion or I2P address is held only as an `-externalip`, and `GetLocal`
tells one only to a peer of its own network: here, an onion one to a peer
that came in through an `=onion` listener, and an I2P one to none.
"""

import secrets
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, IPv6Network

from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2

from btclib_node.p2p.address import peer_address
from btclib_node.p2p.banman import Host, SpecialAddress

__all__ = [
    "LOCAL_BIND",
    "LOCAL_IF",
    "LOCAL_MANUAL",
    "LocalService",
    "address_for_peer",
    "host_record",
    "reachability",
]

# Core's `LOCAL_IF`, `LOCAL_BIND` and `LOCAL_MANUAL` (`src/net.h`, same
# sha): the score of an address `AddLocal` was told came from an
# interface, a bind or the operator. Only `LOCAL_MANUAL` is kept when
# `-discover` is off.
LOCAL_IF = 1
LOCAL_BIND = 2
LOCAL_MANUAL = 4

# `GetReachabilityFrom`'s own enum, in its order
_REACH_DEFAULT = 1
_REACH_TEREDO = 2
_REACH_IPV6_WEAK = 3
_REACH_IPV4 = 4
_REACH_IPV6_STRONG = 5
_REACH_PRIVATE = 6

_TEREDO = IPv6Network("2001::/32")
# RFC3964, RFC6052 and RFC6145: the tunnels `GetReachabilityFrom` calls weak
_TUNNELS = (
    IPv6Network("2002::/16"),
    IPv6Network("64:ff9b::/96"),
    IPv6Network("::ffff:0:0:0/96"),
)


@dataclass(slots=True)
class LocalService:
    """Core's `LocalServiceInfo`: the port and score of one local address.

    The score moves only as `AddLocal` moves it; Core's `SeenLocal` also
    raises it for an address a peer reports: btclib-org/btclib-node#1646.
    """

    address: Host
    port: int
    score: int


def host_record(
    host: Host, port: int, timestamp: int = 0, services: int = 0
) -> NetworkAddressV2:
    """Return the BIP155 record of `host` at `port`, as `peer_address` does."""
    if isinstance(host, SpecialAddress):
        return NetworkAddressV2(timestamp, services, host.network, host.packed, port)
    return peer_address(str(host), port, timestamp, services)


def _onion(address: Host) -> bool:
    """Return whether `address` is an onion one."""
    return (
        isinstance(address, SpecialAddress) and address.network == BIP155Network.TORV3
    )


def _ours(ours: IPv4Address | IPv6Address) -> int:
    """Return the rank of `ours` for a peer that has no network of its own."""
    if isinstance(ours, IPv4Address):
        return _REACH_IPV4
    return _REACH_TEREDO if ours in _TEREDO else _REACH_IPV6_WEAK


def reachability(
    ours: Host,
    theirs: IPv4Address | IPv6Address,
    *,
    routable: bool,
) -> int:
    """Return how well `theirs` reaches `ours`, a routable address.

    `routable` is whether `theirs` is: an address that is not, a loopback
    peer for one, is ranked as no network.
    """
    if isinstance(ours, SpecialAddress):
        return _REACH_PRIVATE if _onion(ours) and not routable else _REACH_DEFAULT
    if not routable:
        return _ours(ours)
    if isinstance(theirs, IPv4Address):
        return _REACH_IPV4 if isinstance(ours, IPv4Address) else _REACH_DEFAULT
    if theirs in _TEREDO:
        return _ours(ours)
    if isinstance(ours, IPv6Address) and ours not in _TEREDO:
        tunnel = any(ours in net for net in _TUNNELS)
        return _REACH_IPV6_WEAK if tunnel else _REACH_IPV6_STRONG
    return _ours(ours)


def address_for_peer(  # noqa: PLR0913
    local: dict[bytes, LocalService],
    peer: IPv4Address | IPv6Address,
    *,
    routable: bool,
    listen_port: int,
    seen_as: tuple[IPv4Address | IPv6Address, int | None] | None,
    inbound_onion: bool = False,
) -> tuple[Host, int] | None:
    """Return the address and port to tell `peer`, or `None`.

    The best of `local` by reachability from `peer`, then by score, among
    the addresses of `peer`'s network. That network is Tor where
    `inbound_onion` says `peer` reached an `=onion` listener, and IP
    otherwise: `GetLocal` keeps an onion or I2P address from a peer of
    another network, and every other address from a Tor peer. Where
    `seen_as` is given -- the address `peer` says it reaches this node at,
    the caller having found it routable and `-discover` on -- it replaces
    that, always where nothing is held and otherwise at random, one time
    in two, or one in eight where the score is above `LOCAL_MANUAL`. Its
    port is that of `seen_as` where it has one, an outbound peer being
    unable to see the listening port, and otherwise the held address's,
    or `listen_port` where none is held.
    """
    best = None
    best_rank = (-1, -1)
    for service in local.values():
        if (
            not _onion(service.address)
            if inbound_onion
            else isinstance(service.address, SpecialAddress)
        ):
            continue
        rank = (reachability(service.address, peer, routable=routable), service.score)
        if rank > best_rank:
            best, best_rank = service, rank
    address, port = (None, listen_port) if best is None else (best.address, best.port)
    if seen_as is not None:
        draw = 3 if best is not None and best.score > LOCAL_MANUAL else 1
        if best is None or secrets.randbits(draw) == 0:
            address = seen_as[0]
            port = port if seen_as[1] is None else seen_as[1]
    return None if address is None else (address, port)
