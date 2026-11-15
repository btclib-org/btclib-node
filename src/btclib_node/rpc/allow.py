# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`-rpcallowip`: which sources the JSON-RPC listener answers.

Core's `InitHTTPAllowList` and `ClientAllowed` (`src/httpserver.cpp`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag). A value is read by
`lookup_subnet`, the ban list's `LookupSubNet`, as `setban` reads one in
Core; loopback is always allowed.
"""

from ipaddress import IPv4Address, IPv6Address

from btclib_node.p2p.banman import Subnet, lookup_host, lookup_subnet

__all__ = ["allowed_subnets", "client_allowed"]

# `InitHTTPAllowList`'s refusal (`src/httpserver.cpp:103`, same tag), with
# `'...'` where Core writes the value bare, and the value named by its
# position in `allowed_subnets`' `values`, counted from 1: a value that names
# no subnet may be a key. Section 7 of the organization's standard, *A refusal
# carries no secret*, wins over Core here (SECURITY.md, "Where this node
# departs from Bitcoin Core")
_INVALID = (
    "Invalid -rpcallowip subnet specification: '...' (value {} of -rpcallowip). "
    "Valid values are a single IP (e.g. 1.2.3.4), a network/netmask (e.g. "
    "1.2.3.4/255.255.255.0), a network/CIDR (e.g. 1.2.3.4/24), all ipv4 "
    "(0.0.0.0/0), or all ipv6 (::/0). RFC4193 is allowed only if "
    "-cjdnsreachable=0."
)
# what `InitHTTPAllowList` allows ahead of every `-rpcallowip` value
_ALWAYS = (Subnet.of(IPv4Address("127.0.0.1"), 8), Subnet.of(IPv6Address("::1")))


def allowed_subnets(values: tuple[str, ...]) -> tuple[Subnet, ...]:
    """Core's `InitHTTPAllowList`: loopback, then every `-rpcallowip` value.

    Raises `ValueError` with `_INVALID` at the first value that names no
    subnet.
    """
    subnets = list(_ALWAYS)
    for position, value in enumerate(values, 1):
        subnet = lookup_subnet(value)
        if subnet is None:
            raise ValueError(_INVALID.format(position))
        subnets.append(subnet)
    return tuple(subnets)


def client_allowed(host: str, subnets: tuple[Subnet, ...]) -> bool:
    """Core's `ClientAllowed` of the peer at `host`, as `accept` names it.

    `lookup_host` reads `host` as an address, and `Subnet.matches`
    refuses one `CNetAddr::IsValid` refuses.
    """
    peer = lookup_host(host)
    return peer is not None and any(subnet.matches(peer) for subnet in subnets)
