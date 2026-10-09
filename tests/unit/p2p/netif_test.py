# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""This machine's interface addresses, Core's `GetLocalAddresses` (ISS 1238).

Each `getifaddrs` entry and each `GetAdaptersAddresses` one is built here
in `ctypes`, so every branch of the walks is reached whatever interfaces
the machine running them has. Only the call into `iphlpapi` is left to
the `windows-latest` cell, where the last test here reads the real one.
"""

import ctypes
import errno
import os
import socket
import sys
from ipaddress import IPv4Address, IPv6Address
from itertools import pairwise
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast, override

import pytest

from btclib_node.p2p.netif import (
    _ERROR_BUFFER_OVERFLOW,
    _IFF_LOOPBACK,
    _IFF_UP,
    _MAX_ADAPTER_ADDRESSES_SIZE,
    _Adapter,
    _adapter_addresses,
    _from_sockaddr,
    _IfAddrs,
    _interface_addresses,
    _UnicastAddress,
    _windows_addresses,
    local_addresses,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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


@pytest.mark.parametrize(
    ("make", "size"), [(an_inet, 16), (an_inet6, 28)], ids=["IPv4", "IPv6"]
)
def test_a_given_length_must_be_the_size_of_the_family_s_sockaddr(
    make: Callable[[str], ctypes.Array[ctypes.c_char]], size: int
) -> None:
    """Core's `SetSockAddr` refuses any other length."""
    held = make("::1" if size == 28 else "127.0.0.1")
    sockaddr = ctypes.addressof(held)
    assert _from_sockaddr(sockaddr, size) is not None
    assert _from_sockaddr(sockaddr, size - 1) is None
    assert _from_sockaddr(sockaddr, size + 1) is None


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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows asks GetAdaptersAddresses")
def test_a_failing_getifaddrs_answers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core returns its empty vector where `getifaddrs` fails."""
    libc = a_libc(lambda _: -1)
    monkeypatch.setattr(ctypes, "CDLL", lambda _: libc)
    assert local_addresses() == []
    assert libc.freed == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows asks GetAdaptersAddresses")
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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows asks GetAdaptersAddresses")
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


def _the_kernel_s_source() -> IPv4Address | None:
    """Ask the kernel for its source when called, at the test, not before."""
    return the_source_the_kernel_routes_from()


@pytest.mark.parametrize(
    ("source_of", "held"),
    [
        pytest.param(_the_kernel_s_source, True, id="the kernel's source"),
        pytest.param(lambda: _TEST_NET_1, False, id="TEST-NET-1"),
    ],
)
def test_this_machine_s_addresses_hold_the_one_the_kernel_routes_from(
    source_of: Callable[[], IPv4Address | None], *, held: bool
) -> None:
    """The real `getifaddrs` or `GetAdaptersAddresses`: IP addresses only.

    The source and the addresses are read together, in the test, so that a
    network change between collection and run cannot split them.

    TEST-NET-1 is reserved for documentation, so no interface is meant to
    hold it: it runs the same lines where there is no route, and shows
    that the membership asked of the source can answer no.
    """
    source = source_of()
    addresses = local_addresses()
    if source is None:
        pytest.skip("no route off this machine")
    assert all(isinstance(ip, (IPv4Address, IPv6Address)) for ip in addresses)
    assert not any(ip.is_loopback for ip in addresses)
    assert (source in addresses) is held


def test_the_kernel_s_source_is_read_when_the_test_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source read at collection, not at the run, would not be this one."""
    moved = IPv4Address("198.51.100.1")
    asked: list[None] = []

    def probe() -> IPv4Address:
        asked.append(None)
        return moved

    monkeypatch.setattr(
        sys.modules[__name__], "the_source_the_kernel_routes_from", probe
    )
    monkeypatch.setattr(sys.modules[__name__], "local_addresses", lambda: [moved])
    test_this_machine_s_addresses_hold_the_one_the_kernel_routes_from(
        _the_kernel_s_source, held=True
    )
    assert len(asked) == 1


def test_no_route_skips_the_kernel_s_source_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the machine has no route, the case skips at the run."""
    monkeypatch.setattr(
        sys.modules[__name__], "the_source_the_kernel_routes_from", lambda: None
    )
    with pytest.raises(pytest.skip.Exception):
        test_this_machine_s_addresses_hold_the_one_the_kernel_routes_from(
            _the_kernel_s_source, held=True
        )


def an_adapter(
    *,
    status: int = 1,
    kind: int = 6,
    unicast: Sequence[tuple[int, tuple[ctypes.Array[ctypes.c_char], int] | None]] = (),
) -> tuple[_Adapter, list[_UnicastAddress]]:
    """Return an `IP_ADAPTER_ADDRESSES` and its unicast list.

    Each of `unicast` is `(flags, where)`, `where` a `(sockaddr, length)`
    pair or `None` for no address.
    """
    entries = [_UnicastAddress() for _ in unicast]
    for entry, (flags, where) in zip(entries, unicast, strict=True):
        entry.Flags = flags
        if where is not None:
            sockaddr, length = where
            entry.Address.lpSockaddr = ctypes.addressof(sockaddr)
            entry.Address.iSockaddrLength = length
    for entry, following in pairwise(entries):
        entry.Next = ctypes.pointer(following)
    adapter = _Adapter(OperStatus=status, IfType=kind)
    if entries:
        adapter.FirstUnicastAddress = ctypes.pointer(entries[0])
    return adapter, entries


def test_the_adapter_walk_keeps_what_core_s_loop_keeps() -> None:
    """Not up, loopback, transient, no address, no IP, wrong length: skipped."""
    kept = an_inet("1.2.3.4")
    kept6 = an_inet6("2001:db8::1")
    other = a_sockaddr(socket.AF_UNSPEC, bytes(8))
    skipped = an_inet("5.6.7.8")
    adapters = [
        an_adapter(status=2, unicast=[(0, (skipped, 16))]),
        an_adapter(kind=24, unicast=[(0, (skipped, 16))]),
        an_adapter(),
        an_adapter(
            unicast=[
                (2, (skipped, 16)),
                (0, None),
                (0, (other, 16)),
                (0, (skipped, 15)),
                (1, (kept, 16)),
                (0, (kept6, 28)),
            ]
        ),
    ]
    structs = [adapter for adapter, _ in adapters]
    for adapter, following in zip(structs, [*structs[1:], None], strict=True):
        if following is not None:
            adapter.Next = ctypes.pointer(following)
    found = _adapter_addresses(ctypes.pointer(structs[0]))
    assert found == [IPv4Address("1.2.3.4"), IPv6Address("2001:db8::1")]


@pytest.mark.skipif(
    ctypes.sizeof(ctypes.c_void_p) != 8, reason="the offsets are the 64-bit ones"
)
def test_the_adapter_structs_lay_out_as_the_windows_headers_do() -> None:
    """`iptypes.h`'s `IP_ADAPTER_ADDRESSES_LH` and `..._UNICAST_ADDRESS_LH`."""
    assert _Adapter.FirstUnicastAddress.offset == 24
    assert _Adapter.IfType.offset == 100
    assert _Adapter.OperStatus.offset == 104
    assert _UnicastAddress.Address.offset == 16
    assert _UnicastAddress.Address.size == 16


def a_getter(*statuses: int, adapter: _Adapter | None = None) -> SimpleNamespace:
    """Return a `GetAdaptersAddresses` stand-in answering `statuses` in turn.

    Each call records the size it was given and sets it to 100000, as the
    real one sets what it needs. A `NO_ERROR` answer copies `adapter` in.
    """
    calls: list[int] = []
    answers = iter(statuses)

    def get(buffer: ctypes.Array[ctypes.c_char], size: object) -> int:
        count = size._obj  # type: ignore[attr-defined]
        calls.append(count.value)
        count.value = 100_000
        status = next(answers)
        if status == 0 and adapter is not None:
            ctypes.memmove(buffer, ctypes.byref(adapter), ctypes.sizeof(adapter))
        return status

    return SimpleNamespace(get=get, calls=calls)


def test_a_buffer_too_small_is_doubled_and_the_call_repeated() -> None:
    """15000 octets first, then twice what the call says it needs."""
    kept = an_inet("1.2.3.4")
    adapter, _entries = an_adapter(unicast=[(0, (kept, 16))])
    getter = a_getter(_ERROR_BUFFER_OVERFLOW, 0, adapter=adapter)
    assert _windows_addresses(getter.get) == [IPv4Address("1.2.3.4")]
    assert getter.calls == [15000, 200_000]


def test_the_buffer_stops_growing_at_core_s_bound() -> None:
    """Overflow is final once the buffer has reached the bound."""
    getter = a_getter(*[_ERROR_BUFFER_OVERFLOW] * 10)
    assert _windows_addresses(getter.get) == []
    assert getter.calls == [
        15000,
        200_000,
        400_000,
        800_000,
        1_600_000,
        3_200_000,
        4_000_000,
    ]
    assert getter.calls[-1] == _MAX_ADAPTER_ADDRESSES_SIZE


@pytest.mark.parametrize("status", [232, 87], ids=["ERROR_NO_DATA", "other"])
def test_a_failing_call_answers_nothing(status: int) -> None:
    """Core returns its empty vector where `GetAdaptersAddresses` fails."""
    assert _windows_addresses(a_getter(status).get) == []
