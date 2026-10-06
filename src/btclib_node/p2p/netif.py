# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""This machine's own interface addresses, Core's `GetLocalAddresses`.

`local_addresses` is `GetLocalAddresses` (`src/common/netif.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag): every address of an
interface that is up and not a loopback one, IPv4 and IPv6 alone, as its
`FromSockAddr` reads them. Where Core asks `getifaddrs` this does, and
on Windows, where Core asks `GetAdaptersAddresses`, so does this. Both
are reached through `ctypes`, the standard library having no binding of
either.
"""

import ctypes
import socket
import sys
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

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


def _from_sockaddr(
    sockaddr: int, length: int | None = None
) -> IPv4Address | IPv6Address | None:
    """Return the IP of the `sockaddr` at `sockaddr`, Core's `FromSockAddr`.

    `None` for any family but `AF_INET` and `AF_INET6`, and for a `length`
    other than the size of the family's `sockaddr_in` or `sockaddr_in6`.
    `length` is `None` where the system gives none, and is then that size.
    """
    family = _family(sockaddr)
    if family == socket.AF_INET:
        size, offset, width = 16, 4, 4
    elif family == socket.AF_INET6:
        size, offset, width = 28, 8, 16
    else:
        return None
    if length is not None and length != size:
        return None
    return ip_address(ctypes.string_at(sockaddr + offset, width))


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


# `<iphlpapi.h>` and `<iptypes.h>`
_GAA_FLAGS = 0x2 | 0x4 | 0x8 | 0x20  # skip anycast, multicast, DNS, name
_NO_ERROR = 0
_ERROR_BUFFER_OVERFLOW = 111
_IF_OPER_STATUS_UP = 1
_IF_TYPE_SOFTWARE_LOOPBACK = 24
# 0x1 is `IP_ADAPTER_ADDRESS_DNS_ELIGIBLE`, set on an ordinary address
_IP_ADAPTER_ADDRESS_TRANSIENT = 0x2
# Core's bound on what it will grow the buffer to
_MAX_ADAPTER_ADDRESSES_SIZE = 4_000_000


class _SocketAddress(ctypes.Structure):
    """`SOCKET_ADDRESS`."""


_SocketAddress._fields_ = [
    ("lpSockaddr", ctypes.c_void_p),
    ("iSockaddrLength", ctypes.c_int32),
]


class _UnicastAddress(ctypes.Structure):
    """`IP_ADAPTER_UNICAST_ADDRESS_LH` up to `Address`, the last field read."""


_UnicastAddress._fields_ = [
    ("Length", ctypes.c_uint32),
    ("Flags", ctypes.c_uint32),
    ("Next", ctypes.POINTER(_UnicastAddress)),
    ("Address", _SocketAddress),
]


class _Adapter(ctypes.Structure):
    """`IP_ADAPTER_ADDRESSES_LH` up to `OperStatus`, the last field read."""


_Adapter._fields_ = [
    ("Length", ctypes.c_uint32),
    ("IfIndex", ctypes.c_uint32),
    ("Next", ctypes.POINTER(_Adapter)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_UnicastAddress)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_void_p),
    ("Description", ctypes.c_void_p),
    ("FriendlyName", ctypes.c_void_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", ctypes.c_uint32),
    ("Flags", ctypes.c_uint32),
    ("Mtu", ctypes.c_uint32),
    ("IfType", ctypes.c_uint32),
    ("OperStatus", ctypes.c_int32),
]


def _adapter_addresses(
    first: ctypes._Pointer[_Adapter],
) -> list[IPv4Address | IPv6Address]:
    """Walk a `GetAdaptersAddresses` list, keeping what Core's loop keeps.

    An adapter not up or a software loopback one is skipped, and of the
    others' unicast addresses a transient one and one `_from_sockaddr`
    answers `None` for.
    """
    addresses: list[IPv4Address | IPv6Address] = []
    adapter = first
    while ctypes.cast(adapter, ctypes.c_void_p).value:
        found = adapter.contents
        if (
            found.OperStatus == _IF_OPER_STATUS_UP
            and found.IfType != _IF_TYPE_SOFTWARE_LOOPBACK
        ):
            unicast = found.FirstUnicastAddress
            while ctypes.cast(unicast, ctypes.c_void_p).value:
                entry = unicast.contents
                where = entry.Address
                if not entry.Flags & _IP_ADAPTER_ADDRESS_TRANSIENT and where.lpSockaddr:
                    address = _from_sockaddr(where.lpSockaddr, where.iSockaddrLength)
                    if address is not None:
                        addresses.append(address)
                unicast = entry.Next
        adapter = found.Next
    return addresses


def _windows_addresses(
    get: Callable[[ctypes.Array[ctypes.c_char], ctypes._CArgObject], int],
) -> list[IPv4Address | IPv6Address]:
    """Return what `get`, `GetAdaptersAddresses`, lists, as Core's call does.

    The buffer starts at 15000 octets and grows, at least doubling, while
    `get` reports it too small, up to Core's bound. Nothing where `get`
    then fails, as Core returns its empty vector.
    """
    buffer = ctypes.create_string_buffer(15000)
    while True:
        size = ctypes.c_uint32(len(buffer))
        status = get(buffer, ctypes.byref(size))
        if (
            status == _ERROR_BUFFER_OVERFLOW
            and len(buffer) < _MAX_ADAPTER_ADDRESSES_SIZE
        ):
            grown = min(max(size.value, len(buffer)) * 2, _MAX_ADAPTER_ADDRESSES_SIZE)
            buffer = ctypes.create_string_buffer(grown)
        else:
            break
    if status != _NO_ERROR:
        return []
    return _adapter_addresses(ctypes.cast(buffer, ctypes.POINTER(_Adapter)))


def local_addresses() -> list[IPv4Address | IPv6Address]:
    """Return this machine's interface addresses, as Core's `GetLocalAddresses`.

    Nothing where `getifaddrs` or `GetAdaptersAddresses` fails, as Core
    returns its empty vector, and nothing where the C library or `iphlpapi`
    cannot be loaded or has no such function: Core's own call cannot fail
    that way, and discovery never stops its start-up.
    """
    if sys.platform == "win32":  # pragma: no cover -- the windows-latest cell
        try:
            iphlpapi = ctypes.WinDLL("iphlpapi")
            iphlpapi.GetAdaptersAddresses.restype = ctypes.c_uint32
            get_adapters = iphlpapi.GetAdaptersAddresses
        except OSError, AttributeError:
            return []
        return _windows_addresses(
            lambda buffer, size: get_adapters(0, _GAA_FLAGS, None, buffer, size)
        )
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
