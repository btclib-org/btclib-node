# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""This machine's interface addresses, Core's `GetLocalAddresses` (ISS 1238).

Each `getifaddrs` entry is built here in `ctypes`, so every branch of the
walk is reached whatever interfaces the machine running it has.
"""

import ctypes
import errno
import os
import socket
import sys
from ipaddress import IPv4Address, IPv6Address
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast, override

import pytest

from btclib_node.p2p.netif import (
    _IFF_LOOPBACK,
    _IFF_UP,
    _from_sockaddr,
    _IfAddrs,
    _interface_addresses,
    local_addresses,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# the platforms whose `struct sockaddr` has no `sa_len`, named apart from
# the flag `netif` reads so that a wrong flag reads these wrong
_NATIVE_FAMILY = sys.platform.startswith(
    ("linux", "android", "win32", "cygwin", "sunos")
)


def a_sockaddr(family: int, body: bytes) -> ctypes.Array[ctypes.c_char]:
    """Return a `struct sockaddr` of `family`, as this platform lays it out.

    `body` starts at offset 2, the port, as in `sockaddr_in` and
    `sockaddr_in6`.
    """
    head = family.to_bytes(2, sys.byteorder) if _NATIVE_FAMILY else bytes([16, family])
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
    other = a_sockaddr(socket.AF_UNSPEC, bytes(8))
    assert _from_sockaddr(ctypes.addressof(ipv4)) == IPv4Address("1.2.3.4")
    assert _from_sockaddr(ctypes.addressof(ipv6)) == IPv6Address("2001:db8::1")
    assert _from_sockaddr(ctypes.addressof(other)) is None


def test_the_walk_keeps_what_core_s_loop_keeps() -> None:
    """No address, an interface down or a loopback one, or no IP: skipped."""
    kept = an_inet("1.2.3.4")
    down = an_inet("5.6.7.8")
    loopback = an_inet("127.0.0.1")
    other = a_sockaddr(socket.AF_UNSPEC, bytes(8))
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
    assert _interface_addresses(ctypes.pointer(structs[0])) == [IPv4Address("1.2.3.4")]


def a_libc(getifaddrs: Callable[[object], int]) -> SimpleNamespace:
    """Return a C library stand-in, recording each list it is asked to free."""
    freed: list[object] = []
    return SimpleNamespace(getifaddrs=getifaddrs, freeifaddrs=freed.append, freed=freed)


@pytest.mark.skipif(sys.platform == "win32", reason="no getifaddrs (#1310)")
def test_a_failing_getifaddrs_answers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core returns its empty vector where `getifaddrs` fails."""
    libc = a_libc(lambda _: -1)
    monkeypatch.setattr(ctypes, "CDLL", lambda _: libc)
    assert local_addresses() == []
    assert libc.freed == []


@pytest.mark.skipif(sys.platform == "win32", reason="no getifaddrs (#1310)")
def test_the_list_getifaddrs_gives_is_freed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`freeifaddrs` of the very list the walk read."""
    kept = an_inet("1.2.3.4")
    entry = _IfAddrs(ifa_flags=_IFF_UP, ifa_addr=ctypes.addressof(kept))
    listed: list[int | None] = []

    def getifaddrs(first: object) -> int:
        pointer = cast("ctypes._Pointer[_IfAddrs]", first._obj)  # type: ignore[attr-defined]
        pointer.contents = entry
        listed.append(ctypes.addressof(entry))
        return 0

    libc = a_libc(getifaddrs)
    monkeypatch.setattr(ctypes, "CDLL", lambda _: libc)
    assert local_addresses() == [IPv4Address("1.2.3.4")]
    freed = [ctypes.cast(first, ctypes.c_void_p).value for first in libc.freed]
    assert freed == listed


def no_c_library(_: object) -> object:
    """Fail to load the library, as `ctypes.CDLL` can."""
    raise OSError


@pytest.mark.skipif(sys.platform == "win32", reason="no getifaddrs (#1310)")
@pytest.mark.parametrize(
    "cdll",
    [no_c_library, lambda _: SimpleNamespace(freeifaddrs=None)],
    ids=["no library", "no getifaddrs"],
)
def test_a_c_library_without_getifaddrs_answers_nothing(
    monkeypatch: pytest.MonkeyPatch, cdll: Callable[[object], object]
) -> None:
    """Discovery does not stop the node, as it cannot stop Core's."""
    monkeypatch.setattr(ctypes, "CDLL", cdll)
    assert local_addresses() == []


# TEST-NET-1, reserved by RFC 5737 for documentation
_TEST_NET_1 = IPv4Address("192.0.2.1")


def the_source_the_kernel_routes_from() -> IPv4Address | None:
    """Return the source the kernel picks to reach TEST-NET-1, or `None`.

    A UDP `connect` sends nothing, and chooses a route and a source;
    `None` is a machine with no route off it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect((str(_TEST_NET_1), 9))
        except OSError:
            return None
        return IPv4Address(probe.getsockname()[0])


# asked at import, so that the skip below is decided before the test runs
_SOURCE = the_source_the_kernel_routes_from()


class _Unrouted(socket.socket):
    """A socket whose `connect` finds no route, as a machine offline does."""

    @override
    def connect(self, address: object) -> None:
        raise OSError(errno.ENETUNREACH, os.strerror(errno.ENETUNREACH))


class _Routed(socket.socket):
    """A socket whose `connect` routes from TEST-NET-2's first address."""

    @override
    def connect(self, address: object) -> None:
        """Choose the route, and send nothing, as a UDP `connect` does."""

    @override
    def getsockname(self) -> tuple[str, int]:
        return ("198.51.100.1", 49152)


@pytest.mark.parametrize(
    ("kind", "source"),
    [(_Unrouted, None), (_Routed, IPv4Address("198.51.100.1"))],
    ids=["no route", "a route"],
)
def test_the_probe_answers_the_source_or_none_without_a_route(
    monkeypatch: pytest.MonkeyPatch, kind: type[socket.socket], source: object
) -> None:
    """Either answer, whatever route the machine running it has."""
    monkeypatch.setattr(socket, "socket", kind)
    assert the_source_the_kernel_routes_from() == source


@pytest.mark.skipif(sys.platform == "win32", reason="no getifaddrs (#1310)")
@pytest.mark.parametrize(
    ("source", "held"),
    [
        pytest.param(
            _SOURCE,
            True,
            marks=pytest.mark.skipif(
                _SOURCE is None, reason="no route off this machine"
            ),
            id="the kernel's source",
        ),
        pytest.param(_TEST_NET_1, False, id="TEST-NET-1"),
    ],
)
def test_this_machine_s_addresses_hold_the_one_the_kernel_routes_from(
    source: IPv4Address, *, held: bool
) -> None:
    """The real `getifaddrs`: IP addresses, no loopback, the source kept.

    TEST-NET-1 is reserved for documentation, so no interface is meant to
    hold it: it runs the same lines where there is no route, and shows
    that the membership asked of the source can answer no.
    """
    addresses = local_addresses()
    assert all(isinstance(ip, (IPv4Address, IPv6Address)) for ip in addresses)
    assert not any(ip.is_loopback for ip in addresses)
    assert (source in addresses) is held
