# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""This machine's own interface addresses, Core's `GetLocalAddresses`.

`local_addresses` is `GetLocalAddresses` (`src/common/netif.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag) on the `getifaddrs` side:
every address of an interface that is up and not a loopback one, IPv4
and IPv6 alone, as its `FromSockAddr` reads them. `getifaddrs` is
reached through `ctypes`, the standard library having no binding of it.

On Windows Core asks `GetAdaptersAddresses` instead, which this does not
port: there it answers nothing (btclib-org/btclib-node#1310).
"""

import ctypes
import socket
import sys
from ipaddress import IPv4Address, IPv6Address

__all__ = ["local_addresses"]

# `<net/if.h>`'s flags, the same on Linux and on the BSDs
_IFF_UP = 0x1
_IFF_LOOPBACK = 0x8


class _IfAddrs(ctypes.Structure):
    """`struct ifaddrs` up to `ifa_addr`, the last field this reads."""


_IfAddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_IfAddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.c_void_p),
]

# A BSD `struct sockaddr` opens with `sa_len` and a one-octet
# `sa_family`, Linux's with a native `unsigned short` `sa_family`. Both
# lay a `sockaddr_in`'s address at offset 4 and a `sockaddr_in6`'s at 8.
# iOS and DragonFly are BSD-derived too, and no "bsd" in their
# `sys.platform` names them.
_BSD_SOCKADDR = sys.platform.startswith(
    ("darwin", "ios", "freebsd", "openbsd", "netbsd", "dragonfly")
)


def _family(sockaddr: int) -> int:
    """Return the `sa_family` of the `struct sockaddr` at `sockaddr`."""
    return (
        ctypes.c_uint8.from_address(sockaddr + 1).value
        if _BSD_SOCKADDR
        else ctypes.c_ushort.from_address(sockaddr).value
    )


def _from_sockaddr(sockaddr: int) -> IPv4Address | IPv6Address | None:
    """Return the IP of the `sockaddr` at `sockaddr`, Core's `FromSockAddr`.

    `None` for any family but `AF_INET` and `AF_INET6`.
    """
    family = _family(sockaddr)
    if family == socket.AF_INET:
        return IPv4Address(ctypes.string_at(sockaddr + 4, 4))
    if family == socket.AF_INET6:
        return IPv6Address(ctypes.string_at(sockaddr + 8, 16))
    return None


def _interface_addresses(
    first: ctypes._Pointer[_IfAddrs],
) -> list[IPv4Address | IPv6Address]:
    """Walk a `getifaddrs` list, keeping what Core's loop keeps.

    An entry with no address, of an interface down or of a loopback one
    is skipped, and so is one `_from_sockaddr` answers `None` for.
    """
    addresses: list[IPv4Address | IPv6Address] = []
    entry = first
    # a NULL `ifa_next` ends the list
    while ctypes.cast(entry, ctypes.c_void_p).value:
        ifa = entry.contents
        flags = ifa.ifa_flags
        if ifa.ifa_addr and flags & _IFF_UP and not flags & _IFF_LOOPBACK:
            address = _from_sockaddr(ifa.ifa_addr)
            if address is not None:
                addresses.append(address)
        entry = ifa.ifa_next
    return addresses


def local_addresses() -> list[IPv4Address | IPv6Address]:
    """Return this machine's interface addresses, as Core's `GetLocalAddresses`.

    Nothing where `getifaddrs` fails, as Core returns its empty vector,
    and nothing where the C library cannot be loaded or has no
    `getifaddrs`: Core's own call cannot fail that way, and discovery
    never stops its start-up.
    """
    if sys.platform == "win32":  # pragma: no cover -- no getifaddrs (#1310)
        return []
    try:
        libc = ctypes.CDLL(None)
        getifaddrs = libc.getifaddrs
        freeifaddrs = libc.freeifaddrs
    except OSError, AttributeError:
        return []
    first = ctypes.POINTER(_IfAddrs)()
    if getifaddrs(ctypes.byref(first)) != 0:
        return []
    try:
        return _interface_addresses(first)
    finally:
        freeifaddrs(first)
