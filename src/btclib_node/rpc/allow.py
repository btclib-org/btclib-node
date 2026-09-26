# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`-rpcallowip`: which sources the JSON-RPC listener answers.

Core's `InitHTTPAllowList` and `ClientAllowed` (`src/httpserver.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag), over `LookupSubNet`
(`src/netbase.cpp`) and `CSubNet` (`src/netaddress.cpp`), both at the
same sha. A value is an IP, a network and its netmask, or a network and
its prefix length; loopback is always allowed.
"""

import re
import socket
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)

from btclib_node.p2p.eviction import is_valid

__all__ = ["allowed_subnets", "client_allowed"]

type _IP = IPv4Address | IPv6Address
type _Subnet = IPv4Network | IPv6Network

# `InitHTTPAllowList`'s refusal, word for word
_INVALID = (
    "Invalid -rpcallowip subnet specification: {}. Valid values are a single "
    "IP (e.g. 1.2.3.4), a network/netmask (e.g. 1.2.3.4/255.255.255.0), a "
    "network/CIDR (e.g. 1.2.3.4/24), all ipv4 (0.0.0.0/0), or all ipv6 "
    "(::/0). RFC4193 is allowed only if -cjdnsreachable=0."
)
# what `InitHTTPAllowList` allows ahead of every `-rpcallowip` value
_ALWAYS = (ip_network("127.0.0.0/8"), ip_network("::1/128"))
# `INTERNAL_IN_IPV6_PREFIX`, which `SetLegacyIPv6` reads as `NET_INTERNAL`
# and `LookupIntern` drops from what a lookup answers
_INTERNAL = IPv6Network("fd6b:88c0:8724::/48")
# `TORV2_IN_IPV6_PREFIX`, which `SetLegacyIPv6` reads as the unspecified
# address
_TORV2 = IPv6Network("fd87:d87e:eb43::/48")
# `ToIntegral<uint8_t>`: digits alone, no sign and no space
_PREFIX_LENGTH = re.compile(r"[0-9]+")
_MAX_OCTET = 0xFF
# Core's `NetmaskBits`: an octet of a netmask, and its leading ones
_NETMASK_BITS = {
    0x00: 0,
    0x80: 1,
    0xC0: 2,
    0xE0: 3,
    0xF0: 4,
    0xF8: 5,
    0xFC: 6,
    0xFE: 7,
    0xFF: 8,
}


def _legacy(ip: _IP) -> _IP:
    """`CNetAddr::SetLegacyIPv6`'s reading: a mapped IPv4 address is IPv4."""
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _lookup_host(name: str) -> _IP | None:
    """Core's `LookupHost(name, fAllowLookup=false)`, for an IP address.

    `getaddrinfo` with `AI_NUMERICHOST`, as `WrappedGetAddrInfo` asks it,
    so the forms the platform's resolver reads as an address are read so
    here too, `127.1` among them, and a name is not looked up. The
    first answer is taken. `SetSpecial`'s Tor and I2P names are not
    read (btclib-org/btclib-node#1288).
    """
    if not name:
        return None
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    try:
        answers = socket.getaddrinfo(
            name,
            None,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
            flags=socket.AI_NUMERICHOST,
        )
    except OSError, UnicodeError:
        return None
    # a scope id is kept by Core and read by nothing `Match` compares
    ip = ip_address(str(answers[0][4][0]).partition("%")[0])
    if ip in _INTERNAL:
        return None
    if ip in _TORV2:
        return IPv6Address(0)
    return _legacy(ip)


def _masked(network: _IP, mask: _IP) -> _Subnet | None:
    """`CSubNet(addr, mask)`: one network, its mask contiguous ones."""
    if network.version != mask.version:
        return None
    ones = 0
    zeros_found = False
    for octet in mask.packed:
        bits = _NETMASK_BITS.get(octet, -1)
        if bits == -1 or (zeros_found and bits != 0):
            return None
        zeros_found = bits < 8  # noqa: PLR2004
        ones += bits
    return ip_network((network, ones), strict=False)


def _lookup_subnet(value: str) -> _Subnet | None:
    """Core's `LookupSubNet`: the subnet `value` names, `None` if none.

    The last `/` splits the address from its mask. A mask that is a
    number is a prefix length, host bits cleared; any other is read as
    an address and must be a netmask of the address's own network.
    """
    if "\0" in value:
        return None
    address, slash, mask = value.rpartition("/")
    if not slash:
        address = value
    network = _lookup_host(address)
    if network is None:
        return None
    if not slash:
        return ip_network(network)
    if _PREFIX_LENGTH.fullmatch(mask) and int(mask) <= _MAX_OCTET:
        if int(mask) > network.max_prefixlen:
            return None
        return ip_network((network, int(mask)), strict=False)
    netmask = _lookup_host(mask)
    return None if netmask is None else _masked(network, netmask)


def allowed_subnets(
    values: tuple[str, ...],
) -> tuple[IPv4Network | IPv6Network, ...]:
    """Core's `InitHTTPAllowList`: loopback, then every `-rpcallowip` value.

    Raises `ValueError` with Core's message for the first value that
    names no subnet.
    """
    subnets: list[_Subnet] = list(_ALWAYS)
    for value in values:
        subnet = _lookup_subnet(value)
        if subnet is None:
            raise ValueError(_INVALID.format(value))
        subnets.append(subnet)
    return tuple(subnets)


def client_allowed(host: str, subnets: tuple[IPv4Network | IPv6Network, ...]) -> bool:
    """Core's `ClientAllowed` of the peer at `host`, as `getpeername` names it.

    A mapped IPv4 peer is IPv4, as `CNetAddr` reads one, and a peer
    `CNetAddr::IsValid` refuses is refused.
    """
    try:
        peer = ip_address(host.partition("%")[0])
    except ValueError:
        return False
    legacy = (
        peer
        if isinstance(peer, IPv6Address)
        else IPv6Address(b"\0" * 10 + b"\xff\xff" + peer.packed)
    )
    if not is_valid(legacy):
        return False
    peer = _legacy(peer)
    return any(peer in subnet for subnet in subnets)
