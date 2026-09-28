# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`-rpcallowip`: which sources the JSON-RPC listener answers.

Core's `InitHTTPAllowList` and `ClientAllowed` (`src/httpserver.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag). A value is read by
`lookup_subnet`, the ban list's `LookupSubNet`, as `setban` reads one in
Core; loopback is always allowed.
"""

from ipaddress import IPv4Address, IPv6Address

from btclib_node.p2p.banman import Subnet, lookup_host, lookup_subnet

__all__ = ["allowed_subnets", "client_allowed"]

# `InitHTTPAllowList`'s refusal, word for word
_INVALID = (
    "Invalid -rpcallowip subnet specification: {}. Valid values are a single "
    "IP (e.g. 1.2.3.4), a network/netmask (e.g. 1.2.3.4/255.255.255.0), a "
    "network/CIDR (e.g. 1.2.3.4/24), all ipv4 (0.0.0.0/0), or all ipv6 "
    "(::/0). RFC4193 is allowed only if -cjdnsreachable=0."
)
# what `InitHTTPAllowList` allows ahead of every `-rpcallowip` value
_ALWAYS = (Subnet.of(IPv4Address("127.0.0.1"), 8), Subnet.of(IPv6Address("::1")))


def allowed_subnets(values: tuple[str, ...]) -> tuple[Subnet, ...]:
    """Core's `InitHTTPAllowList`: loopback, then every `-rpcallowip` value.

    Raises `ValueError` with Core's message for the first value that
    names no subnet.
    """
    subnets = list(_ALWAYS)
    for value in values:
        subnet = lookup_subnet(value)
        if subnet is None:
            raise ValueError(_INVALID.format(value))
        subnets.append(subnet)
    return tuple(subnets)


def client_allowed(host: str, subnets: tuple[Subnet, ...]) -> bool:
    """Core's `ClientAllowed` of the peer at `host`, as `getpeername` names it.

    `HTTPRequest::GetPeer` reads the peer's numeric address through
    `LookupNumeric`, which `lookup_host` is, and `Subnet.matches` refuses
    one `CNetAddr::IsValid` refuses.
    """
    peer = lookup_host(host)
    return peer is not None and any(subnet.matches(peer) for subnet in subnets)
