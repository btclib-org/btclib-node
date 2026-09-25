# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Which inbound peer the selection evicts, and which netgroup a peer is in.

The protection and eviction tables are Core's own
`src/test/net_peer_eviction_tests.cpp`, case for case, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag: random candidates with the
fields each case is about overwritten, shuffled, then selected. Core
runs each case under one fixed seed; these run under several.
"""

import dataclasses
import random
from ipaddress import IPv6Address
from typing import TYPE_CHECKING, Any

import pytest

from btclib_node.p2p import eviction
from btclib_node.p2p.address import peer_address
from btclib_node.p2p.eviction import (
    EvictionCandidate,
    Network,
    is_local,
    is_valid,
    keyed_net_group,
    net_class,
    net_group,
    protect_eviction_candidates_by_ratio,
    select_node_to_evict,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# Core's `ALL_NETWORKS` (`src/test/util/net.h`)
_ALL_NETWORKS = [n for n in Network if n != Network.MAX]
_SEEDS = range(4)


def random_candidates(n: int, rng: random.Random) -> list[EvictionCandidate]:
    """Core's `GetRandomNodeEvictionCandidates`."""
    return [
        EvictionCandidate(
            id=i,
            connected=rng.randrange(100),
            min_ping_time=rng.randrange(100),
            last_block_time=rng.randrange(100),
            last_tx_time=rng.randrange(100),
            relevant_services=rng.random() < 0.5,
            relay_txs=rng.random() < 0.5,
            bloom_filter=rng.random() < 0.5,
            keyed_net_group=rng.randrange(100),
            prefer_evict=rng.random() < 0.5,
            is_local=rng.random() < 0.5,
            network=rng.choice(_ALL_NETWORKS),
            noban=False,
            inbound=True,
        )
        for i in range(n)
    ]


type Setup = Callable[[EvictionCandidate, int], EvictionCandidate]


def _uptime(c: EvictionCandidate, _: int) -> EvictionCandidate:
    return dataclasses.replace(c, connected=c.id)


def _peers(
    local: set[int],
    networks: dict[Network, set[int]],
    default: Network,
    *,
    by_uptime: bool = True,
) -> Setup:
    """Core's own setup lambda: uptime by id, locality and network by id."""

    def setup(c: EvictionCandidate, n: int) -> EvictionCandidate:
        network = next((net for net, ids in networks.items() if c.id in ids), default)
        c = dataclasses.replace(c, is_local=c.id in local, network=network)
        return _uptime(c, n) if by_uptime else c

    return setup


def _reverse_uptime(c: EvictionCandidate, n: int) -> EvictionCandidate:
    return dataclasses.replace(
        c, connected=n - c.id, is_local=False, network=Network.IPV6
    )


R = range
ONION, I2P, CJDNS = Network.ONION, Network.I2P, Network.CJDNS
IPV4, IPV6 = Network.IPV4, Network.IPV6

# (peers, setup, protected ids, ids left unprotected), `peer_protection_test`
PROTECTION_CASES: list[tuple[int, Setup, set[int], set[int]]] = [
    (12, _peers(set(), {}, IPV4), set(R(6)), set(R(6, 12))),
    (12, _reverse_uptime, set(R(6, 12)), set(R(6))),
    (12, _peers(set(), {ONION: {3, 8, 9}}, IPV4, by_uptime=False), {3, 8, 9}, set()),
    (
        12,
        _peers(set(), {ONION: {3, 8, 9, 10, 11}}, IPV6),
        {0, 1, 2, 3, 8, 9},
        {4, 5, 6, 7, 10, 11},
    ),
    (12, _peers({1, 9, 11}, {}, IPV4, by_uptime=False), {1, 9, 11}, set()),
    (12, _peers(set(R(7, 12)), {}, IPV6), {0, 1, 2, 7, 8, 9}, {3, 4, 5, 6, 10, 11}),
    (12, _peers(set(), {I2P: {2, 7, 10}}, IPV4, by_uptime=False), {2, 7, 10}, set()),
    (
        12,
        _peers(set(), {I2P: {4, 9, 10, 11}}, IPV6),
        {0, 1, 2, 4, 9, 10},
        {3, 5, 6, 7, 8, 11},
    ),
    (12, _peers(set(), {CJDNS: {2, 7, 10}}, IPV4, by_uptime=False), {2, 7, 10}, set()),
    (
        12,
        _peers(set(), {CJDNS: {4, 9, 10, 11}}, IPV6),
        {0, 1, 2, 4, 9, 10},
        {3, 5, 6, 7, 8, 11},
    ),
    # two networks
    (4, _peers({4}, {ONION: {3}}, IPV4), {0, 4}, {1, 2}),
    (7, _peers({6}, {ONION: {5}}, IPV4), {0, 1, 6}, {2, 3, 4, 5}),
    (8, _peers({6}, {ONION: {5}}, IPV4), {0, 1, 5, 6}, {2, 3, 4, 7}),
    (
        12,
        _peers({6, 9, 11}, {ONION: {7, 8, 10}}, IPV6),
        {0, 1, 2, 6, 7, 9},
        {3, 4, 5, 8, 10, 11},
    ),
    (
        12,
        _peers({5, 6, 7, 8}, {ONION: {10}}, IPV4),
        {0, 1, 2, 5, 6, 10},
        {3, 4, 7, 8, 9, 11},
    ),
    (
        16,
        _peers({6, 9, 11, 12}, {ONION: {8, 10}}, IPV6),
        {0, 1, 2, 3, 6, 8, 9, 10},
        {4, 5, 7, 11, 12, 13, 14, 15},
    ),
    (
        16,
        _peers(set(R(11, 16)), {ONION: {10}}, IPV4),
        {0, 1, 2, 3, 10, 11, 12, 13},
        {4, 5, 6, 7, 8, 9, 14, 15},
    ),
    (
        16,
        _peers({15}, {ONION: {7, 8, 9, 10}}, IPV6),
        {0, 1, 2, 3, 7, 8, 9, 15},
        {5, 6, 10, 11, 12, 13, 14},
    ),
    (
        12,
        _peers(set(), {ONION: {8, 10}, I2P: {6, 9, 11, 12}}, IPV4),
        {0, 1, 2, 6, 8, 10},
        {3, 4, 5, 7, 9, 11},
    ),
    # three networks
    (4, _peers({2}, {I2P: {3}, ONION: {1}}, IPV6), {0, 3}, {1, 2}),
    (7, _peers({4}, {I2P: {6}, ONION: {5}}, IPV6), {0, 1, 6}, {2, 3, 4, 5}),
    (8, _peers({6}, {I2P: {5}, ONION: {4}}, IPV6), {0, 1, 5, 6}, {2, 3, 4, 7}),
    (
        16,
        _peers({6, 12, 13, 14, 15}, {I2P: {7, 11}, ONION: {9, 10}}, IPV4),
        {0, 1, 2, 3, 6, 7, 9, 11},
        {4, 5, 8, 10, 12, 13, 14, 15},
    ),
    (
        24,
        _peers({12}, {I2P: set(R(15, 23)), ONION: {23}}, IPV6),
        {0, 1, 2, 3, 4, 5, 12, 15, 16, 17, 18, 23},
        {6, 7, 8, 9, 10, 11, 13, 14, 19, 20, 21, 22},
    ),
    (
        24,
        _peers({15}, {I2P: {12, 14, 17}, ONION: set(R(18, 24))}, IPV4),
        {0, 1, 2, 3, 4, 5, 12, 14, 15, 17, 18, 19},
        {6, 7, 8, 9, 10, 11, 13, 16, 20, 21, 22, 23},
    ),
    (
        24,
        _peers({13}, {I2P: set(R(17, 24)), ONION: {12, 14, 15, 16}}, IPV6),
        {0, 1, 2, 3, 4, 5, 12, 13, 14, 15, 17, 18},
        {6, 7, 8, 9, 10, 11, 16, 19, 20, 21, 22, 23},
    ),
    (
        24,
        _peers(set(R(16, 24)), {CJDNS: set(R(11, 15)), ONION: {7, 8, 9}}, IPV4),
        {0, 1, 2, 3, 4, 5, 7, 8, 11, 12, 16, 17},
        {6, 9, 10, 13, 14, 15, 18, 19, 20, 21, 22, 23},
    ),
    # four networks
    (5, _peers({3}, {CJDNS: {4}, I2P: {1}, ONION: {2}}, IPV6), {0, 4}, {1, 2, 3}),
    (
        7,
        _peers({4}, {CJDNS: {6}, I2P: {5}, ONION: {3}}, IPV4),
        {0, 1, 6},
        {2, 3, 4, 5},
    ),
    (
        8,
        _peers({3}, {CJDNS: {5}, I2P: {6}, ONION: {3}}, IPV6),
        {0, 1, 5, 6},
        {2, 3, 4, 7},
    ),
    (
        16,
        _peers(set(R(6, 16)), {CJDNS: {11, 15}, I2P: {10, 14}, ONION: {8, 9}}, IPV4),
        {0, 1, 2, 3, 6, 8, 10, 11},
        {4, 5, 7, 9, 12, 13, 14, 15},
    ),
    (
        24,
        _peers({13}, {CJDNS: set(R(18, 24)), I2P: {17}, ONION: {12, 14, 15, 16}}, IPV6),
        {0, 1, 2, 3, 4, 5, 12, 13, 14, 17, 18, 19},
        {6, 7, 8, 9, 10, 11, 15, 16, 20, 21, 22, 23},
    ),
]


@pytest.mark.parametrize("seed", _SEEDS)
@pytest.mark.parametrize(
    ("peers", "setup", "protected", "unprotected"), PROTECTION_CASES
)
def test_protection_by_ratio(
    seed: int, peers: int, setup: Setup, protected: set[int], unprotected: set[int]
) -> None:
    """Core's `IsProtected`: half kept, the named ids among them."""
    rng = random.Random(seed)
    candidates = [setup(c, peers) for c in random_candidates(peers, rng)]
    rng.shuffle(candidates)
    protect_eviction_candidates_by_ratio(candidates)
    assert len(candidates) == peers - peers // 2
    left = {c.id for c in candidates}
    assert not protected & left
    assert unprotected <= left


def test_the_ratio_stops_when_no_disadvantaged_peer_is_left_to_protect() -> None:
    """A peer both local and onion counts for both, and is protected once.

    Its first protection leaves the onion count stale; the next pass
    finds no onion peer left, protects nothing, and stops, handing
    the unused slot to uptime.
    """
    candidates = [
        dataclasses.replace(
            _unprotected(i, group=i, connected=i),
            is_local=i == 7,
            network=ONION if i == 7 else IPV4,
        )
        for i in range(8)
    ]
    protect_eviction_candidates_by_ratio(candidates)
    assert {c.id for c in candidates} == {3, 4, 5, 6}


def _keyed(c: EvictionCandidate, n: int) -> EvictionCandidate:
    return dataclasses.replace(c, keyed_net_group=n - c.id)


def _ping(c: EvictionCandidate, _: int) -> EvictionCandidate:
    return dataclasses.replace(c, min_ping_time=c.id)


def _tx(c: EvictionCandidate, n: int) -> EvictionCandidate:
    return dataclasses.replace(c, last_tx_time=n - c.id)


def _block(c: EvictionCandidate, n: int) -> EvictionCandidate:
    return dataclasses.replace(c, last_block_time=n - c.id)


def _block_relay_only(c: EvictionCandidate, n: int) -> EvictionCandidate:
    c = _block(c, n)
    if c.id <= 7:
        c = dataclasses.replace(c, relay_txs=False, relevant_services=True)
    return c


def _all_four(c: EvictionCandidate, n: int) -> EvictionCandidate:
    return _block(_tx(_ping(_keyed(c, n), n), n), n)


# (setup, ids none of which is evicted), `peer_eviction_test`
EVICTION_CASES: list[tuple[Setup, set[int]]] = [
    (_keyed, set(R(4))),
    (_ping, set(R(8))),
    (_tx, set(R(4))),
    (_block_relay_only, set(R(8))),
    (_block, set(R(4))),
    (_block_relay_only, set(R(12))),
    (_all_four, set(R(20))),
]


@pytest.mark.parametrize("seed", _SEEDS)
@pytest.mark.parametrize(("setup", "kept"), EVICTION_CASES)
def test_the_protected_are_never_evicted(
    seed: int, setup: Setup, kept: set[int]
) -> None:
    """Core's `IsEvicted` over every candidate count from 0 to 199."""
    rng = random.Random(seed)
    for n in range(200):
        candidates = [setup(c, n) for c in random_candidates(n, rng)]
        rng.shuffle(candidates)
        assert select_node_to_evict(candidates) not in kept


@pytest.mark.parametrize("seed", _SEEDS)
def test_enough_candidates_always_evict_and_few_never_do(seed: int) -> None:
    """At least 29 random candidates leave one to evict, at most 20 none."""
    rng = random.Random(seed)
    for n in range(200):
        evicted = select_node_to_evict(random_candidates(n, rng))
        if n >= 29:
            assert evicted is not None
        if n <= 20:
            assert evicted is None


def _unprotected(
    i: int, group: int, connected: int, **fields: Any
) -> EvictionCandidate:
    """Build a candidate every `_protected` one outranks, `fields` aside."""
    candidate = EvictionCandidate(
        id=i,
        connected=connected,
        min_ping_time=1000.0,
        last_block_time=0,
        last_tx_time=0,
        relevant_services=False,
        relay_txs=True,
        bloom_filter=False,
        keyed_net_group=group,
        prefer_evict=False,
        is_local=False,
        network=Network.IPV4,
        noban=False,
        inbound=True,
    )
    return dataclasses.replace(candidate, **fields)


@pytest.mark.parametrize(("block_relay_only", "protected"), [(False, 20), (True, 28)])
def test_the_fixed_protections_keep_exactly_their_counts(
    block_relay_only: bool,  # noqa: FBT001
    protected: int,
) -> None:
    """Alike candidates: the fixed counts protect so many, one more is evicted.

    Four by netgroup, eight by ping, four by transaction and four by
    block, and eight more by block where no candidate relays
    transactions and every one offers the wanted services. One
    candidate past those is one the ratio, keeping half of one, leaves
    to evict.
    """
    fields = {"relay_txs": False, "relevant_services": True}
    peers = [
        _unprotected(i, group=7, connected=0, **(fields if block_relay_only else {}))
        for i in range(protected + 1)
    ]
    assert select_node_to_evict(peers[:protected]) is None
    assert select_node_to_evict(peers) is not None


def _protected(i: int) -> EvictionCandidate:
    """Build one of the candidates every fixed-count protection takes first.

    The oldest, the fastest, the latest to relay a block and a
    transaction, and in the highest netgroups: `_SHIELD` holds enough of
    them to fill every fixed-count protection, and the ratio's uptime
    half takes the rest before any `_unprotected` candidate, so what a
    test adds is what is left to choose from.
    """
    return dataclasses.replace(
        _unprotected(i, group=1000 + i, connected=-1000 - i),
        min_ping_time=0.0,
        last_block_time=1000 + i,
        last_tx_time=1000 + i,
        relevant_services=True,
    )


_SHIELD = [_protected(100 + i) for i in range(24)]


def test_the_largest_netgroup_loses_its_youngest() -> None:
    """Of what is left, the largest netgroup gives up its youngest peer."""
    left = [
        _unprotected(1, group=7, connected=10),
        _unprotected(2, group=7, connected=30),
        _unprotected(3, group=9, connected=50),
    ]
    # the ratio keeps half the unprotected by uptime, so twice as many
    # old ones again, in a netgroup of their own
    old = [_unprotected(10 + i, group=5, connected=i - 100) for i in range(3)]
    assert select_node_to_evict([*_SHIELD, *old, *left]) == 2


def test_a_tie_between_netgroups_goes_to_the_one_with_the_youngest_member() -> None:
    """Netgroups of one each: the peer that connected last is evicted."""
    left = [
        _unprotected(1, group=7, connected=30),
        _unprotected(2, group=9, connected=50),
    ]
    old = [_unprotected(10 + i, group=5 + i, connected=i - 100) for i in range(2)]
    assert select_node_to_evict([*_SHIELD, *old, *left]) == 2


def test_a_peer_preferred_for_eviction_goes_first() -> None:
    """Among what is left, only a `prefer_evict` peer is considered."""
    left = [
        _unprotected(1, group=7, connected=10, prefer_evict=True),
        _unprotected(2, group=7, connected=30),
        _unprotected(3, group=7, connected=50),
    ]
    old = [_unprotected(10 + i, group=5, connected=i - 100) for i in range(3)]
    assert select_node_to_evict([*_SHIELD, *old, *left]) == 1


@pytest.mark.parametrize("field", ["noban", "outbound"])
def test_a_noban_or_outbound_peer_is_never_a_candidate(field: str) -> None:
    """`ProtectNoBanConnections` and `ProtectOutboundConnections`."""
    fields = {"noban": True} if field == "noban" else {"inbound": False}
    peers = [_unprotected(i, group=7, connected=i, **fields) for i in range(40)]
    assert select_node_to_evict(peers) is None


def _tie(**fields: Any) -> EvictionCandidate:
    """Build a candidate equal to every other `_tie` one but for `fields`."""
    return dataclasses.replace(_unprotected(0, group=0, connected=0), **fields)


# (step's comparator, the one it evicts, the one it protects), each pair
# equal in the step's own time, so Core's fall-through decides
TIE_CASES = [
    # `CompareNodeTXTime`: relaying, then no bloom filter, then uptime
    ("_tx_time", _tie(id=1, relay_txs=False), _tie(id=2, relay_txs=True)),
    ("_tx_time", _tie(id=1, bloom_filter=True), _tie(id=2, bloom_filter=False)),
    ("_tx_time", _tie(id=1, connected=5), _tie(id=2, connected=4)),
    # `CompareNodeBlockTime`: the wanted services, then uptime
    (
        "_block_time",
        _tie(id=1, relevant_services=False),
        _tie(id=2, relevant_services=True),
    ),
    ("_block_time", _tie(id=1, connected=5), _tie(id=2, connected=4)),
    # `CompareNodeBlockRelayOnlyTime`: not relaying, then the wanted
    # services, then uptime
    (
        "_block_relay_only_time",
        _tie(id=1, relay_txs=True),
        _tie(id=2, relay_txs=False),
    ),
    (
        "_block_relay_only_time",
        _tie(id=1, relevant_services=False),
        _tie(id=2, relevant_services=True),
    ),
    ("_block_relay_only_time", _tie(id=1, connected=5), _tie(id=2, connected=4)),
]


@pytest.mark.parametrize(("key", "evicted", "kept"), TIE_CASES)
def test_a_tie_in_a_step_s_own_time_falls_through_as_core_s(
    key: str, evicted: EvictionCandidate, kept: EvictionCandidate
) -> None:
    """One slot, two candidates alike in time: the step protects the better."""
    for order in ([evicted, kept], [kept, evicted]):
        remaining = list(order)
        eviction._erase_last_k(remaining, getattr(eviction, key), 1)
        assert remaining == [evicted]


# (address, net class, netgroup, local), `NetGroupManager::GetGroup` with
# no asmap and the `CNetAddr` predicates it reads
NET_GROUP_CASES = [
    ("1.2.3.4", Network.IPV4, "010102", False),
    ("127.0.0.1", Network.UNROUTABLE, "00", True),
    ("0.1.2.3", Network.UNROUTABLE, "00", True),
    ("::1", Network.UNROUTABLE, "00", True),
    ("10.1.2.3", Network.UNROUTABLE, "00", False),
    ("192.168.1.1", Network.UNROUTABLE, "00", False),
    ("100.64.0.1", Network.UNROUTABLE, "00", False),
    ("255.255.255.255", Network.UNROUTABLE, "00", False),
    ("fe80::1", Network.UNROUTABLE, "00", False),
    ("fc00::1", Network.UNROUTABLE, "00", False),
    ("2001:db8::1", Network.UNROUTABLE, "00", False),
    ("2001:10::1", Network.UNROUTABLE, "00", False),
    ("::", Network.UNROUTABLE, "00", False),
    # an IPv4 address mapped into IPv6 is that IPv4 address
    ("::ffff:5.6.7.8", Network.IPV4, "010506", False),
    # IPv6 carrying an IPv4 address: SIIT, NAT64, 6to4, Teredo
    ("::ffff:0:1.2.3.4", Network.IPV4, "010102", False),
    ("64:ff9b::102:304", Network.IPV4, "010102", False),
    ("2002:102:304::1", Network.IPV4, "010102", False),
    ("2001:0:1:2:3:4:fefd:fcfb", Network.IPV4, "010102", False),
    # he.net's `/36`: the fifth octet's low nibble set, the high one kept
    ("2001:470:a0cd::1", Network.IPV6, "0220010470af", False),
    # Core's internal prefix: `NET_INTERNAL`, one group per address
    (
        "fd6b:88c0:8724:1:2:3:4:5",
        Network.INTERNAL,
        "0600010002000300040005",
        False,
    ),
    ("fd6b:88c0:8725::1", Network.UNROUTABLE, "00", False),
    ("2a01:4f8::1", Network.IPV6, "022a0104f8", False),
]


@pytest.mark.parametrize(("ip", "cls", "group", "local"), NET_GROUP_CASES)
def test_net_group(ip: str, cls: Network, group: str, local: bool) -> None:  # noqa: FBT001
    """IPv4 by `/16`, he.net by `/36`, other IPv6 by `/32`, the rest as one."""
    address = peer_address(ip, 8333)
    assert net_class(address) == cls
    assert net_group(address).hex() == group
    assert is_local(address) is local


# (the sixteen octets of an `addr` v1 field, `CNetAddr::IsValid` of them)
VALID_CASES = [
    ("::ffff:1.2.3.4", True),
    # local and private addresses are valid, only not routable
    ("::ffff:127.0.0.1", True),
    ("::ffff:10.0.0.1", True),
    ("::ffff:0.1.2.3", True),
    ("::1", True),
    ("2a01:4f8::1", True),
    ("::ffff:0.0.0.0", False),
    ("::ffff:255.255.255.255", False),
    ("::", False),
    ("2001:db8::1", False),
    ("fd6b:88c0:8724::1", False),
    ("fd87:d87e:eb43::1", False),
    # beside both prefixes, and valid
    ("fd6b:88c0:8725::1", True),
    ("fd87:d87e:eb44::1", True),
]


@pytest.mark.parametrize(("ip", "valid"), VALID_CASES)
def test_is_valid(ip: str, valid: bool) -> None:  # noqa: FBT001
    """Neither unspecified, broadcast, documentation nor internal is valid.

    What `SetLegacyIPv6` reads under the Tor v2 prefix is the
    unspecified address, and so not valid either.
    """
    assert is_valid(IPv6Address(ip)) is valid


def test_the_keyed_net_group_depends_on_the_key_alone() -> None:
    """One netgroup, one value under one key, another under another."""
    a, b = peer_address("1.2.3.4", 1), peer_address("1.2.99.99", 2)
    key = bytes(16)
    assert keyed_net_group(key, a) == keyed_net_group(key, b)
    assert keyed_net_group(key, a) != keyed_net_group(b"\x01" * 16, a)
    assert keyed_net_group(key, a) != keyed_net_group(key, peer_address("1.3.0.0", 1))
