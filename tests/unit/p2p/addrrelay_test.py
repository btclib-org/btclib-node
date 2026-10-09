# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.p2p.addrrelay`, Core's `RelayAddress` choice."""

from itertools import pairwise

import pytest
from btclib.hashes import siphash
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2

from btclib_node.p2p.address import peer_address
from btclib_node.p2p.addrrelay import is_relayable, relay_destinations

KEY = (1, 2)
PEERS = range(1, 21)
DAY = 24 * 3600


def test_a_reachable_address_goes_to_two_distinct_peers() -> None:
    """ISS 1867: `nRelayNodes` is 2 for a reachable address."""
    for last in range(30):
        address = peer_address(f"5.6.7.{last}", 8333)
        chosen = relay_destinations(KEY, address, PEERS, reachable=True, now=1e6)
        assert len(chosen) == len(set(chosen)) == 2
        assert set(chosen) <= set(PEERS)


def test_an_unreachable_address_goes_to_one_or_two_peers() -> None:
    """ISS 1867: `nRelayNodes` is 1 or 2, by a bit of the hash."""
    counts = {
        len(
            relay_destinations(
                KEY,
                peer_address(f"5.6.7.{last}", 8333),
                PEERS,
                reachable=False,
                now=1e6,
            )
        )
        for last in range(60)
    }
    assert counts == {1, 2}


def test_the_peers_hold_for_a_day_and_then_rotate() -> None:
    """ISS 1867: `time_addr` changes once in 24 hours."""
    address = peer_address("5.6.7.8", 8333)
    rotations = {
        tuple(
            relay_destinations(KEY, address, PEERS, reachable=True, now=1e6 + day * DAY)
        )
        for day in range(10)
    }
    assert len(rotations) > 1
    hourly = [
        relay_destinations(KEY, address, PEERS, reachable=True, now=1e6 + hour * 3600)
        for hour in range(25)
    ]
    # 25 hours cross at most two boundaries
    assert sum(a != b for a, b in pairwise(hourly)) <= 2


def test_each_address_rotates_at_a_time_of_its_own() -> None:
    """ISS 1867: the address hash offsets `time_addr`, so rotations spread."""

    def rotation_hour(last: int) -> int | None:
        address = peer_address(f"5.6.7.{last}", 8333)
        hourly = [
            relay_destinations(
                KEY, address, PEERS, reachable=True, now=1e6 + hour * 3600
            )
            for hour in range(25)
        ]
        return next(
            (hour for hour, (a, b) in enumerate(pairwise(hourly)) if a != b), None
        )

    assert len({rotation_hour(last) for last in range(40)} - {None}) > 1


def test_the_key_decides_which_peers() -> None:
    """ISS 1867: a peer that does not know the key cannot predict the peers."""
    address = peer_address("5.6.7.8", 8333)
    chosen = {
        tuple(relay_destinations((key, key), address, PEERS, reachable=True, now=1e6))
        for key in range(10)
    }
    assert len(chosen) > 1


def test_the_ranking_is_core_s_siphash_of_the_words_it_writes() -> None:
    """ISS 1867: `CSipHasher` writes the id, the hashes and the peer in turn."""
    address = peer_address("5.6.7.8", 8333)
    now = 1_700_000_000
    # `CServiceHash(0, 0)`: `m_net` (1 for IPv4), the port, `m_addr`
    address_hash = siphash(
        0,
        0,
        (1).to_bytes(8, "little") + (8333).to_bytes(8, "little") + bytes([5, 6, 7, 8]),
    )
    window = (now + address_hash) % 2**64 // DAY
    seed = (
        (0x3CAC0035B5866B90).to_bytes(8, "little")
        + address_hash.to_bytes(8, "little")
        + window.to_bytes(8, "little")
    )
    expected = sorted(
        PEERS,
        key=lambda peer: -siphash(*KEY, seed + peer.to_bytes(8, "little")),
    )[:2]
    assert relay_destinations(KEY, address, PEERS, reachable=True, now=now) == expected


def test_no_peers_no_destinations() -> None:
    """ISS 1867: nothing is chosen where no peer takes part."""
    address = peer_address("5.6.7.8", 8333)
    assert relay_destinations(KEY, address, [], reachable=True, now=1e6) == []


@pytest.mark.parametrize(
    ("network_id", "relayable"),
    [
        (BIP155Network.IPV4, True),
        (BIP155Network.IPV6, True),
        (BIP155Network.TORV3, True),
        (BIP155Network.I2P, True),
        (BIP155Network.CJDNS, True),
        (BIP155Network.YGGDRASIL, False),
    ],
)
def test_is_relayable_is_core_s(network_id: BIP155Network, *, relayable: bool) -> None:
    """ISS 1867: `IsRelayable` is IPv4, IPv6, Tor, I2P or CJDNS."""
    size = {BIP155Network.IPV4: 4, BIP155Network.TORV3: 32, BIP155Network.I2P: 32}
    address = NetworkAddressV2(0, 0, network_id, b"\x01" * size.get(network_id, 16), 1)
    assert is_relayable(address) is relayable
