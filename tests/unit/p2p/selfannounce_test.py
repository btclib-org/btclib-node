# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.p2p.selfannounce`, Core's `GetLocalAddrForPeer`."""

import secrets
from ipaddress import IPv4Address, IPv6Address, ip_address

import pytest

from btclib_node.p2p.selfannounce import (
    LOCAL_BIND,
    LOCAL_MANUAL,
    LocalService,
    address_for_peer,
    reachability,
)

V4 = IPv4Address("8.8.8.8")
V6 = IPv6Address("2606:4700::1")
TEREDO = IPv6Address("2001:0:1::1")
TUNNEL = IPv6Address("2002:808:808::1")


@pytest.mark.parametrize(
    ("ours", "theirs", "routable", "expected"),
    [
        (V4, V4, True, 4),
        (V6, V4, True, 1),
        (TEREDO, V4, True, 1),
        (V4, V6, True, 4),
        (V6, V6, True, 5),
        (TUNNEL, V6, True, 3),
        (TEREDO, V6, True, 2),
        (V4, TEREDO, True, 4),
        (V6, TEREDO, True, 3),
        (TEREDO, TEREDO, True, 2),
        (V4, IPv4Address("127.0.0.1"), False, 4),
        (V6, IPv4Address("127.0.0.1"), False, 3),
        (TEREDO, IPv6Address("::1"), False, 2),
    ],
)
def test_reachability_ranks_as_getreachabilityfrom(
    ours: IPv4Address | IPv6Address,
    theirs: IPv4Address | IPv6Address,
    expected: int,
    *,
    routable: bool,
) -> None:
    """ISS 1641: `GetReachabilityFrom` ranks IPv4, IPv6 and Teredo."""
    assert reachability(ours, theirs, routable=routable) == expected


def _held(*services: LocalService) -> dict[bytes, LocalService]:
    return {bytes(service.address.packed): service for service in services}


def _tell(
    held: dict[bytes, LocalService],
    peer: str = "9.9.9.9",
    *,
    seen_as: tuple[IPv4Address | IPv6Address, int | None] | None = None,
    inbound_onion: bool = False,
) -> tuple[IPv4Address | IPv6Address, int] | None:
    return address_for_peer(
        held,
        ip_address(peer),
        routable=True,
        listen_port=8333,
        seen_as=seen_as,
        inbound_onion=inbound_onion,
    )


def test_nothing_is_told_where_nothing_is_held() -> None:
    """ISS 1641: no local address and no view of the peer, no announcement."""
    assert _tell({}) is None


def test_a_peer_on_an_onion_listener_is_told_none_of_the_held_addresses() -> None:
    """ISS 1644: `GetLocal` skips a local address not on the peer's network."""
    held = _held(LocalService(V4, 2, LOCAL_BIND), LocalService(V6, 1, LOCAL_MANUAL))
    assert _tell(held) == (V4, 2)
    assert _tell(held, inbound_onion=True) is None


def test_the_best_reachable_address_then_the_best_score_is_told() -> None:
    """ISS 1641: `GetLocal` ranks by reachability first and score second."""
    assert _tell(
        _held(LocalService(V6, 1, LOCAL_MANUAL), LocalService(V4, 2, LOCAL_BIND))
    ) == (V4, 2)
    assert _tell(
        _held(LocalService(V4, 2, LOCAL_BIND), LocalService(V6, 1, LOCAL_MANUAL))
    ) == (V4, 2)
    other = IPv4Address("8.8.4.4")
    assert _tell(
        _held(LocalService(V4, 1, LOCAL_BIND), LocalService(other, 3, LOCAL_MANUAL))
    ) == (other, 3)


def test_the_address_a_peer_sees_stands_in_where_nothing_is_held() -> None:
    """ISS 1641: with nothing held, an inbound peer's view is told, port too."""
    seen = (IPv4Address("5.6.7.8"), 1234)
    assert _tell({}, seen_as=seen) == seen
    # an outbound peer cannot see the listening port
    assert _tell({}, seen_as=(seen[0], None)) == (seen[0], 8333)


@pytest.mark.parametrize(
    ("score", "bits"), [(LOCAL_BIND, 1), (LOCAL_MANUAL, 1), (LOCAL_MANUAL + 1, 3)]
)
def test_the_address_a_peer_sees_replaces_one_held_by_chance(
    monkeypatch: pytest.MonkeyPatch, score: int, bits: int
) -> None:
    """ISS 1641: one time in two, or eight where the score beats the manual."""
    drawn = []

    def randbits(count: int) -> int:
        drawn.append(count)
        return 0

    monkeypatch.setattr(secrets, "randbits", randbits)
    seen = (IPv4Address("5.6.7.8"), 1234)
    held = _held(LocalService(V4, 2, score))
    assert _tell(held, seen_as=seen) == seen
    assert drawn == [bits]
    monkeypatch.setattr(secrets, "randbits", lambda count: 1)
    assert _tell(held, seen_as=seen) == (V4, 2)
