# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""This machine's interface addresses, Core's `GetLocalAddresses` (ISS 1238).

Each `getifaddrs` entry is built here in `ctypes`, so every branch of the
walk is reached whatever interfaces the machine running it has.
"""

import ctypes
import socket
import sys
from ipaddress import IPv4Address, IPv6Address
from types import SimpleNamespace
from typing import TYPE_CHECKING

from btclib_node.p2p import netif
from btclib_node.p2p.netif import (
    _IFF_LOOPBACK,
    _IFF_UP,
    _IfAddrs,
    from_sockaddr,
    interface_addresses,
    local_addresses,
)

if TYPE_CHECKING:
    import pytest


def a_sockaddr(family: int, body: bytes) -> ctypes.Array[ctypes.c_char]:
    """Return a `struct sockaddr` of `family`, as this platform lays it out.

    `body` starts at offset 2, the port, as in `sockaddr_in` and
    `sockaddr_in6`.
    """
    head = (
        bytes([16, family])
        if netif._BSD_SOCKADDR
        else family.to_bytes(2, sys.byteorder)
    )
    return ctypes.create_string_buffer(head + body + bytes(32))


def an_inet(ip: str) -> ctypes.Array[ctypes.c_char]:
    """Return a `sockaddr_in` for `ip`, port 8333."""
    return a_sockaddr(
        socket.AF_INET, (8333).to_bytes(2, "big") + IPv4Address(ip).packed
    )


def an_inet6(ip: str) -> ctypes.Array[ctypes.c_char]:
    """Return a `sockaddr_in6` for `ip`, port 8333, no flow label."""
    return a_sockaddr(
        socket.AF_INET6,
        (8333).to_bytes(2, "big") + bytes(4) + IPv6Address(ip).packed,
    )


def test_from_sockaddr_reads_an_ipv4_and_an_ipv6_address_and_nothing_else() -> None:
    """Core's `FromSockAddr`: `AF_INET` and `AF_INET6` alone."""
    ipv4 = an_inet("1.2.3.4")
    ipv6 = an_inet6("2001:db8::1")
    other = a_sockaddr(socket.AF_UNIX, bytes(8))
    assert from_sockaddr(ctypes.addressof(ipv4)) == IPv4Address("1.2.3.4")
    assert from_sockaddr(ctypes.addressof(ipv6)) == IPv6Address("2001:db8::1")
    assert from_sockaddr(ctypes.addressof(other)) is None


def test_the_walk_keeps_what_core_s_loop_keeps() -> None:
    """No address, an interface down or a loopback one, or no IP: skipped."""
    kept = an_inet("1.2.3.4")
    down = an_inet("5.6.7.8")
    loopback = an_inet("127.0.0.1")
    other = a_sockaddr(socket.AF_UNIX, bytes(8))
    entries = [
        (_IFF_UP, None),
        (0, down),
        (_IFF_UP | _IFF_LOOPBACK, loopback),
        (_IFF_UP, other),
        (_IFF_UP, kept),
    ]
    structs = [_IfAddrs() for _ in entries]
    for struct, (flags, sockaddr), following in zip(
        structs, entries, [*structs[1:], None], strict=True
    ):
        struct.ifa_flags = flags
        struct.ifa_addr = None if sockaddr is None else ctypes.addressof(sockaddr)
        if following is not None:
            struct.ifa_next = ctypes.pointer(following)
    assert interface_addresses(ctypes.pointer(structs[0])) == [IPv4Address("1.2.3.4")]


def test_a_failing_getifaddrs_answers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core returns its empty vector where `getifaddrs` fails."""
    no_interfaces = SimpleNamespace(getifaddrs=lambda _: -1)
    monkeypatch.setattr(ctypes, "CDLL", lambda _: no_interfaces)
    assert local_addresses() == []


def test_this_machine_s_addresses_are_ip_addresses() -> None:
    """The real `getifaddrs`, freed after the walk; no loopback address."""
    addresses = local_addresses()
    assert all(isinstance(ip, (IPv4Address, IPv6Address)) for ip in addresses)
    assert not any(ip.is_loopback for ip in addresses)
