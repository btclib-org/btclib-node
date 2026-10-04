# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Where a peer is, and the table of the ones this node knows of.

The address itself is btclib's, and `btclib.p2p.addrv2.NetworkAddressV2`
is the one this node holds a peer in: BIP155's record is the only
encoding that carries every network a peer can be on, so the narrower
`addr` entry would lose an onion peer the moment one is gossiped. The
translation between the two is btclib's as well --
`btclib.p2p.addrv2.addr_entry` and `peer_from_addr_entry` -- for what
goes on the wire wherever the peer has not asked for BIP155.

What is left here is what btclib has no business holding: dialling a
socket, and the table of addresses to dial. btclib is a codec -- it
speaks to nobody -- so the question "can this be connected to" and the
answer to it are this node's.
"""

import asyncio
import hashlib
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from io import BytesIO
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import TYPE_CHECKING, Any, cast

from btclib import var_int
from btclib.p2p.address import ServiceFlags
from btclib.p2p.addrv2 import (
    BIP155Network,
    NetworkAddressV2,
    can_addrv1,
    is_embedded_ipv6,
    network_address,
)

from btclib_node.db import KeyValueStore
from btclib_node.exceptions import UnsupportedAddressTypeError
from btclib_node.p2p.eviction import get_network, is_routable, net_class, net_group

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from pathlib import Path

    from btclib_node.chains import Chain
    from btclib_node.p2p.eviction import Network

__all__ = [
    "BAD_PORTS",
    "RECENT_TRY_SECONDS",
    "SEEDS_SERVICE_FLAGS",
    "AddrResponseCache",
    "PeerDB",
    "can_connect",
    "dial",
    "endpoint_key",
    "fixed_seed_addresses",
    "host_key",
    "internal_source",
    "ip_and_port",
    "network_class",
    "peer_address",
]

# Core's `IsBadPort` (`src/netbase.cpp`, at bitcoin/bitcoin@9be056a8a7,
# the v31.1 tag): ports other services listen on, which an automatic
# dial passes over while `_BAD_PORT_DRAWS` has not been reached, and
# which `-bind` and `-port` are warned of.
BAD_PORTS = frozenset(
    {
        1,
        7,
        9,
        11,
        13,
        15,
        17,
        19,
        20,
        21,
        22,
        23,
        25,
        37,
        42,
        43,
        53,
        69,
        77,
        79,
        87,
        95,
        101,
        102,
        103,
        104,
        109,
        110,
        111,
        113,
        115,
        117,
        119,
        123,
        135,
        137,
        139,
        143,
        161,
        179,
        389,
        427,
        465,
        512,
        513,
        514,
        515,
        526,
        530,
        531,
        532,
        540,
        548,
        554,
        556,
        563,
        587,
        601,
        636,
        989,
        990,
        993,
        995,
        1719,
        1720,
        1723,
        2049,
        3306,
        3389,
        3659,
        4045,
        5060,
        5061,
        5432,
        5900,
        6000,
        6566,
        6665,
        6666,
        6667,
        6668,
        6669,
        6697,
        10080,
        27017,
    }
)

# Core's `INTERNAL_IN_IPV6_PREFIX` (`src/netaddress.h`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
_INTERNAL_PREFIX = bytes.fromhex("fd6b88c08724")

# the two networks this node has a dial for, and the whole of what
# `dial` below opens a socket for. `can_connect`'s own docstring is
# where this is told apart from `btclib.p2p.addrv2.can_addrv1`
_IP_NETWORKS = (BIP155Network.IPV4, BIP155Network.IPV6)


def peer_address(
    ip: str, port: int, timestamp: int = 0, services: int = 0
) -> NetworkAddressV2:
    """Return the BIP155 record of a peer named by the text of its IP.

    `ipaddress.ip_address` is what tells the two IP networks apart, and
    the octets it packs are what BIP155 asks for: four for a v4 peer and
    sixteen for a v6 one, where an `addr` entry would carry the v4 one
    mapped into sixteen.
    """
    parsed = ip_address(ip)
    # `ip_address(...).version` is the stdlib's own name for this, not
    # this tree's: it is only ever 4 or 6, and naming the 4 here would
    # give a second name to something `ipaddress` already names by being
    # IPv4 itself
    network_id = (
        BIP155Network.IPV4 if parsed.version == 4 else BIP155Network.IPV6  # noqa: PLR2004
    )
    return NetworkAddressV2(timestamp, services, network_id, parsed.packed, port)


# Core's `SeedsServiceFlags` (`src/protocol.h`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the services a fixed seed
# and a DNS seed's answer are recorded with, which the dial loop requires.
SEEDS_SERVICE_FLAGS = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS
# `ThreadDNSAddressSeed`'s `nMaxIPs`: how many answers one seed's `x9.`
# subdomain may add (`src/net.cpp`, same sha).
_MAX_SEED_ANSWERS = 32

# Core's own flat discount for a whole gossiped `ADDR`/`ADDRV2` batch
# (`net_processing.cpp`'s `m_addrman.Add(vAddrOk, pfrom.addr,
# /*time_penalty=*/2h)`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag):
# `add_addresses`' own default `time_penalty`.
_GOSSIP_TIME_PENALTY = 2 * 3600
# `ThreadDNSAddressSeed`'s own backdating of a DNS seed's answer,
# `rng.rand_uniform_delay(Now<NodeSeconds>() - 3 * 24h, -4 * 24h)` --
# uniform between three and seven days old (`src/net.cpp`, same sha).
_DNS_SEED_MIN_AGE = 3 * 24 * 3600
_DNS_SEED_MAX_AGE = 7 * 24 * 3600
# `ConvertSeeds`'s own backdating of a fixed seed,
# `rng.rand_uniform_delay(Now<NodeSeconds>() - one_week, -one_week)` with
# `one_week = 7 * 24h` -- uniform between one and two weeks old
# (`src/net.cpp`, same sha).
_FIXED_SEED_MIN_AGE = 7 * 24 * 3600
_FIXED_SEED_MAX_AGE = 14 * 24 * 3600


def fixed_seed_addresses(seeds: bytes) -> list[NetworkAddressV2]:
    """Decode a chain's `fixed_seeds`, as Core's `ConvertSeeds` does.

    Each endpoint is a BIP155 network id, a compact-size length, the
    address and a big-endian port (`src/net.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), and is given Core's
    `SeedsServiceFlags`, `NODE_NETWORK | NODE_WITNESS`, and
    `_fixed_seed_timestamp`'s own draw for Core's random time one to two
    weeks past (`ConvertSeeds`, same sha) -- btclib-org/btclib-node#1571.
    """
    services = SEEDS_SERVICE_FLAGS
    stream = BytesIO(seeds)
    addresses: list[NetworkAddressV2] = []
    while stream.tell() < len(seeds):
        network_id = stream.read(1)[0]
        address = stream.read(var_int.parse(stream))
        port = int.from_bytes(stream.read(2), "big")
        addresses.append(
            NetworkAddressV2(
                _fixed_seed_timestamp(), services, network_id, address, port
            )
        )
    return addresses


def internal_source(name: str) -> NetworkAddressV2:
    """Return the source Core gives what it learns from `name`: `SetInternal`.

    `CNetAddr::SetInternal` (`src/netaddress.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) is the first ten octets of
    the SHA-256 of `name`, held in IPv6 under `INTERNAL_IN_IPV6_PREFIX`.
    `ThreadDNSAddressSeed` passes one per DNS seed, and
    `ThreadOpenConnections` one named `fixedseeds`, to `addrman.Add`, so
    what a seed gives is placed as one source group's.
    """
    octets = _INTERNAL_PREFIX + hashlib.sha256(name.encode()).digest()[:10]
    return NetworkAddressV2(0, 0, BIP155Network.IPV6, octets, 0)


def can_connect(address: NetworkAddressV2) -> bool:
    """Answer whether this node has a dial for the peer's network.

    A different question from `btclib.p2p.addrv2.can_addrv1`, which asks
    whether the address fits an `addr` version 1 entry at all. The two
    agree on every network this node knows of today and are not the same
    rule: a dial through a SOCKS proxy would reach a network the version
    1 wire format still has no room for.
    """
    return address.network_id in _IP_NETWORKS


def ip_and_port(ip: str, port: int) -> str:
    """Return the endpoint the way Core's `CService::ToStringAddrPort` does.

    `"[" + ToStringAddr() + "]:" + port_str` for every network that
    function's `IsIPv4() || IsTor() || IsI2P() || IsInternal()` does not
    name. The brackets are what tells a v6 host from its port:
    `2001:db8::1` on port 8333 and `2001:db8::1:8333` on some other port
    are both addresses, and without brackets both render as the second.

    The host's text rather than the `NetworkAddress` a peer is held in,
    because a socket's `getpeername` has no such object to offer and
    answers with this.

    A v4-mapped host is unwrapped rather than bracketed, which is Core's
    answer too: `CNetAddr::SetLegacyIPv6` files a mapped address under
    NET_IPV4, which that predicate names. Without the unwrapping a v4
    peer would read `[::ffff:1.2.3.4]:8333`, a `NetworkAddress` holding
    every address in the sixteen octets of an IPv6 one.

    Raises `ValueError` where the host is not an IP address, which is
    what `ipaddress.ip_address` answers with: a hostname is refused
    rather than shown with brackets guessed at.
    """
    parsed = ip_address(ip)
    if not isinstance(parsed, IPv6Address):
        return f"{parsed}:{port}"
    mapped = parsed.ipv4_mapped
    if mapped:
        return f"{mapped}:{port}"
    return f"[{parsed}]:{port}"


# Core's own default (`DEFAULT_CONNECT_TIMEOUT`, src/netbase.h
# at bitcoin/bitcoin@ca7162cde5), not the old poll loop's ten-passes-at-0.1s
# budget this constant carried until ISS 681: that budget was never
# itself checked against Core, and it was too tight for what
# `loop.sock_connect` needs on Windows' Proactor loop to notice a
# refused loopback connect -- measured on this tree's own
# instrumentation, in btclib-org/btclib-node run 33271519023, at
# elapsed=2.017810 for a `ConnectionRefusedError` the run's own kernel
# raised. `loop.sock_connect` does not need the two magic numbers a poll
# needs, only this one: a real timeout wrapped around a wait that is
# otherwise event-driven.
_DIAL_TIMEOUT = 5.0


async def dial(address: NetworkAddressV2) -> socket.socket | None:
    """Return a socket connected to the peer, or nothing if it never came up.

    `dial` and not `connect`, which is what `P2pManager` calls the whole
    of making a connection out of one: this is the socket alone.

    `loop.sock_connect` is the kernel's own answer rather than a guess at
    it: a refusal is `SO_ERROR` on the socket, read the moment the OS
    notifies the loop's writer callback, not inferred after a fixed
    number of `getpeername` polls that cannot tell a refusal from a peer
    that is merely slow. And where `connect` completes without ever
    raising `BlockingIOError` -- a local peer most often -- `sock_connect`
    returns at once instead of an `except` arm that never runs.

    No separate check for a host with no route to the family being
    dialled: `_DIAL_TIMEOUT` already bounds every attempt, and an
    unreachable family fails either the socket creation itself -- a host
    with no IPv6 support at all, `socket.socket(AF_INET6, ...)` raising
    `OSError` before there is anything to connect -- or the same
    `sock_connect` a slow or refusing peer does, landing on the same
    `None` `P2pManager` already treats as "try someone else" either way.
    Core's `ConnectDirectly` (`src/netbase.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) answers the same way: a
    `CreateSock` that comes back null is logged and returned as the same
    `{}` a failed `Connect` answers with, one `nullptr` for both. Core's
    own default reachability check (`ReachableNets`, src/netbase.h at
    58a7869f86: "Everything is reachable by default") is the same bet --
    reachability is what a dial's outcome says it is, not a property
    guessed at beforehand -- so there is nothing here for a heavier check
    to buy.
    """
    if address.network_id not in _IP_NETWORKS:
        raise UnsupportedAddressTypeError
    if address.network_id == BIP155Network.IPV4:
        family = socket.AF_INET
        host = str(IPv4Address(address.address))
        peer: tuple[str, int] | tuple[str, int, int, int] = (host, address.port)
    else:
        family = socket.AF_INET6
        host = str(IPv6Address(address.address))
        # ISS 682: a bare 2-tuple is what a POSIX `socket.connect()`
        # accepts for an IPv6 peer, defaulting flowinfo and scope id to
        # 0. Windows' Proactor loop hands this straight to `ConnectEx`
        # instead, and CPython's `Modules/overlapped.c`
        # (`parse_address`, read at python/cpython@v3.14.0) dispatches
        # on the tuple's length alone rather than on the socket's own
        # family: a 2-tuple is always parsed as `AF_INET`, so
        # `WSAStringToAddressW` is asked to read "::1" as an IPv4
        # dotted quad and answers `WSAEINVAL` synchronously, before
        # `ConnectEx` is ever reached -- confirmed from
        # btclib-org/btclib-node run 33270966438's own instrumentation:
        # `family=23 ... elapsed=0.000099 exc=OSError(22, 'An invalid
        # argument was supplied', None, 10022, None)`. The four-tuple
        # form names the family explicitly and is accepted on every
        # platform this node runs on, POSIX included.
        peer = (host, address.port, 0, 0)
    try:
        client = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        # ISS 1249: a host with no support at all for `family` -- no
        # IPv6 stack -- fails here rather than at `sock_connect`, and
        # gets the same "try someone else" `None` Core's own
        # `ConnectDirectly` answers with when its `CreateSock` comes
        # back null.
        return None
    client.settimeout(0)
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.sock_connect(client, peer), _DIAL_TIMEOUT)
    except OSError, TimeoutError:
        client.close()
        return None
    except asyncio.CancelledError:
        # `manage_connections` cancelled with a dial in flight, which is
        # what `P2pManager.stop`'s own drain does to it. This socket is
        # this call's own until it is handed back, and the caller that
        # never receives it has nothing to close: without this it goes
        # out with the frame the cancellation unwinds, and is reported
        # against whichever test the collector reaches it in
        # (btclib-org/btclib-node#312).
        client.close()
        raise
    return client


# Several record kinds share the one store `PeerDB` opens, so
# `init_from_db` below walks it whole and dispatches on the prefix rather
# than stopping at the first key without one -- `src/btclib_node/db.py`'s
# own docstring names that shape as `BlockDB`'s, next to the other one,
# `BlockIndex`'s, that a store of one record kind can use instead.
# `_SOURCE` holds the net groups of the sources a known row was placed
# from, and `_STATS` its `m_last_success` and `nAttempts`; with
# `_BUCKET_KEY`, in the store's meta column family, they are what places
# the row again. A store without them, as an earlier release wrote it, is
# placed as `init_from_db` says.
_KNOWN = b"known-"
_ANSWERED = b"answered-"
_SOURCE = b"source-"
_STATS = b"stats-"
_BUCKET_KEY = b"bucket-key"

# Core's addrman geometry (`src/addrman_impl.h` and `src/addrman.cpp`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a bucket holds
# `ADDRMAN_BUCKET_SIZE` positions, the new table has
# `ADDRMAN_NEW_BUCKET_COUNT` buckets and the tried table
# `ADDRMAN_TRIED_BUCKET_COUNT`. What one source group gives spreads over
# `ADDRMAN_NEW_BUCKETS_PER_SOURCE_GROUP` new buckets, what one group has
# tried over `ADDRMAN_TRIED_BUCKETS_PER_GROUP` tried ones, and an address
# is in up to `ADDRMAN_NEW_BUCKETS_PER_ADDRESS` new buckets.
_NEW_BUCKET_COUNT = 1024
_TRIED_BUCKET_COUNT = 256
_BUCKET_SIZE = 64
_NEW_BUCKETS_PER_SOURCE_GROUP = 64
_TRIED_BUCKETS_PER_GROUP = 8
_NEW_BUCKETS_PER_ADDRESS = 8
# the same file and sha: how many tried-table collisions are kept to
# test, how long an address tried this recently keeps its tried slot, and
# how long a collision may wait before the old address is evicted anyway
_SET_TRIED_COLLISION_SIZE = 10
_ADDRMAN_REPLACEMENT = 4 * 3600
_ADDRMAN_TEST_WINDOW = 40 * 60

# `ThreadOpenConnections`' own window (`src/net.cpp`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a draw tried less than
# this long ago is passed over, and `GetChance` weighs it down.
RECENT_TRY_SECONDS = 10 * 60

# Core's `ADDRMAN_HORIZON` (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7,
# the v31.1 tag): an address not seen for this long is terrible
_ADDRMAN_HORIZON = 30 * 24 * 3600
# how far ahead of the clock a timestamp may be before it is terrible,
# `IsTerrible`'s "flying DeLorean" (same file and sha)
_ADDRMAN_FUTURE_SLACK = 10 * 60
# `IsTerrible`'s own grace, ahead of the tests below (same file and
# sha): "never remove things tried in the last minute". `PeerDB._last_try`
# is `AddrInfo::m_last_try`; `RECENT_TRY_SECONDS` above is a different
# Core window, `ThreadOpenConnections`'s, not this one.
_ADDRMAN_RECENT_TRY_GRACE = 60
# `IsTerrible`'s tests of failures (same file and sha): never answered and
# tried this many times, or this many failures since an answer longer ago
# than the minimum
_ADDRMAN_RETRIES = 3
_ADDRMAN_MAX_FAILURES = 10
_ADDRMAN_MIN_FAIL = 7 * 24 * 3600
# `GetChance`'s own weights (same file and sha)
_RECENT_TRY_CHANCE = 0.01
_ATTEMPT_CHANCE = 0.66
_MAX_CHANCE_ATTEMPTS = 8
# `Select_`'s growth of the factor on a rejected draw (same file and sha)
_CHANCE_FACTOR_GROWTH = 1.2
# `Connected_`'s own granularity (same file and sha): a time is moved
# forward only where it is older than this
_CONNECTED_UPDATE_INTERVAL = 20 * 60
# `AddSingle`'s `nTime` update interval for an entry held (same file and
# sha): an hour where the gossip is under a day old, a day otherwise
_ONLINE_WINDOW = 24 * 3600
_ONLINE_UPDATE_INTERVAL = 3600
_OFFLINE_UPDATE_INTERVAL = 24 * 3600

_RNG = secrets.SystemRandom()


@dataclass(slots=True)
class _Stats:
    """The members of Core's `AddrInfo` that an address has no field for.

    `m_last_success`, `nAttempts` and `m_last_count_attempt`, which
    `Good_` and `Attempt_` keep (`src/addrman_impl.h`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). `m_last_count_attempt` is
    not in `peers.dat`, nor is it here.
    """

    last_success: float = 0.0
    attempts: int = 0
    last_count_attempt: float = 0.0


def _roll(factor: int) -> bool:
    """Whether a one-in-`factor` draw comes up: `randrange(factor) == 0`."""
    return _RNG.randrange(factor) == 0


def _aged_out(
    address: NetworkAddressV2,
    now: float,
    last_try: float,
    stats: _Stats | None = None,
) -> bool:
    """Whether `address` is terrible, `AddrInfo::IsTerrible`.

    `AddrInfo::IsTerrible` (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag) checks `last_try` first: tried within
    `_ADDRMAN_RECENT_TRY_GRACE`, an address is never terrible, whatever
    its timestamp says. Past that grace, its two time tests: stamped
    more than ten minutes ahead of `now`, or older than
    `_ADDRMAN_HORIZON`. Then its two tests of `stats`, where it is given:
    tried `_ADDRMAN_RETRIES` times and never answered, or
    `_ADDRMAN_MAX_FAILURES` failures and no answer within
    `_ADDRMAN_MIN_FAIL`. `get_addr` tests a known row's timestamp, Core's
    `nTime`, which gossip and `PeerDB.connected` move and a handshake
    does not.
    """
    if now - last_try <= _ADDRMAN_RECENT_TRY_GRACE:
        return False
    age = now - address.timestamp
    if age < -_ADDRMAN_FUTURE_SLACK or age > _ADDRMAN_HORIZON:
        return True
    if stats is None:
        return False
    if not stats.last_success and stats.attempts >= _ADDRMAN_RETRIES:
        return True
    return (
        now - stats.last_success > _ADDRMAN_MIN_FAIL
        and stats.attempts >= _ADDRMAN_MAX_FAILURES
    )


def _chance(now: float, last_try: float, attempts: int) -> float:
    """Return `AddrInfo::GetChance`: how likely a draw of the address is kept.

    `src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag: a
    hundredth for an address tried in the last ten minutes, and
    two thirds of that for each failed attempt, the eighth the last.
    """
    chance = 1.0
    if now - last_try < RECENT_TRY_SECONDS:
        chance *= _RECENT_TRY_CHANCE
    return chance * _ATTEMPT_CHANCE ** min(attempts, _MAX_CHANCE_ATTEMPTS)


def network_class(address: NetworkAddressV2) -> Network:
    """Return Core's `CNetAddr::GetNetClass`, what addrman and the RPCs name.

    An IPv6 address linked to an IPv4 one (6to4, Teredo) is IPv4, as in
    `net_class`; any other network is `get_network`'s.
    """
    return net_class(address) if can_addrv1(address) else get_network(address)


def _held_time(
    existing: NetworkAddressV2 | None,
    address: NetworkAddressV2,
    penalty: float,
    now: float,
) -> int:
    """Return the `nTime` `AddSingle` leaves on an entry gossiped again.

    A new entry has the gossiped time less `penalty`, floored at zero. An
    entry held keeps its own unless the gossip is newer by more than an
    update interval: an hour where the gossip is under a day old, a day
    otherwise (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag).
    """
    gossiped = max(0, int(address.timestamp - penalty))
    if existing is None:
        return gossiped
    interval = (
        _ONLINE_UPDATE_INTERVAL
        if now - address.timestamp < _ONLINE_WINDOW
        else _OFFLINE_UPDATE_INTERVAL
    )
    if existing.timestamp < address.timestamp - interval - penalty:
        return gossiped
    return existing.timestamp


def _storable(address: NetworkAddressV2) -> bool:
    """Whether Core's addrman would hold `address` at all.

    `AddrManImpl::AddSingle` returns early for an address `IsRoutable`
    refuses (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), and BIP155's embedded-IPv6 records are ignored before they
    reach it. `AddrManImpl::Good_` only updates an entry already held,
    so an address this refuses is never recorded as answered either.
    """
    return not is_embedded_ipv6(address) and is_routable(address)


def _dns_seed_timestamp() -> int:
    """Return a whole second, uniform between three and seven days ago.

    Core's `ThreadDNSAddressSeed` (`src/net.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `addr.nTime =
    rng.rand_uniform_delay(Now<NodeSeconds>() - 3 * 24h, -4 * 24h)`, a
    draw uniform over `[now - 7d, now - 3d]`.
    `secrets.SystemRandom().uniform`, the same draw
    `callbacks.getaddr`'s own sample-cache jitter already uses for the
    identical reason: the spread is a privacy property, not a
    formality, an attacker scraping this node's answers over time being
    what a predictable draw would let single out which entry came from
    a seed rather than gossip.
    """
    now = time.time()
    age = secrets.SystemRandom().uniform(_DNS_SEED_MIN_AGE, _DNS_SEED_MAX_AGE)
    return int(now - age)


def _fixed_seed_timestamp() -> int:
    """Return a whole second, uniform between one and two weeks ago.

    Core's `ConvertSeeds` (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag): `addr.nTime = rng.rand_uniform_delay(Now<NodeSeconds>()
    - one_week, -one_week)` with `one_week = 7 * 24h`, a draw uniform
    over `[now - 14d, now - 7d]` -- "It'll only connect to one or two
    seed nodes because once it connects, it'll get a pile of addresses
    with newer timestamps", that function's own comment for why a fixed
    seed is backdated at all. `secrets.SystemRandom().uniform`, the same
    draw `_dns_seed_timestamp` above already uses for the identical
    reason.
    """
    now = time.time()
    age = secrets.SystemRandom().uniform(_FIXED_SEED_MIN_AGE, _FIXED_SEED_MAX_AGE)
    return int(now - age)


@dataclass(slots=True)
class _Side:
    """One table as `_select` draws from it: Core's `vvNew` or `vvTried`.

    `occupant` is what a slot holds, `buckets` those with something in
    them, `row_of` the address of what a slot holds, `fits` whether this
    draw can use it, and `chance` its `GetChance`.
    """

    occupant: dict[tuple[int, int], Any]
    buckets: list[int]
    row_of: Callable[[Any], NetworkAddressV2]
    fits: Callable[[NetworkAddressV2], bool]
    chance: Callable[[NetworkAddressV2], float]
    usable: bool | None = None

    def is_usable(self) -> bool:
        """Whether some address of the table fits, asked once."""
        if self.usable is None:
            self.usable = any(
                self.fits(self.row_of(item)) for item in self.occupant.values()
            )
        return self.usable

    def scan(self, bucket: int) -> NetworkAddressV2 | None:
        """Return the first fit from a random place in `bucket`."""
        start = secrets.choice(range(_BUCKET_SIZE))
        for step in range(_BUCKET_SIZE):
            item = self.occupant.get((bucket, (start + step) % _BUCKET_SIZE))
            if item is not None:
                row = self.row_of(item)
                if self.fits(row):
                    return row
        return None


def _select(tried: _Side | None, new: _Side | None) -> NetworkAddressV2 | None:
    """Draw from one table, a fair coin deciding where both hold something.

    Core's `AddrManImpl::Select_` (`src/addrman.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) searches the tried table
    or the new table with `randbool()` when both hold an address that
    fits, and whichever one does otherwise. In the table it draws a
    bucket, then the first address that fits from a random position in it,
    looping around, and keeps it with the probability `GetChance` gives,
    times a factor that grows by a fifth with every address it does not
    keep. Core draws among all buckets and starts over on an empty one,
    which is a draw among the non-empty ones.
    """
    sides = [side for side in (new, tried) if side is not None and side.is_usable()]
    if not sides:
        return None
    side = sides[secrets.randbelow(2)] if len(sides) == 2 else sides[0]  # noqa: PLR2004
    factor = 1.0
    while True:
        row = side.scan(secrets.choice(side.buckets))
        if row is not None and _RNG.random() < factor * side.chance(row):
            return row
        if row is not None:
            factor *= _CHANCE_FACTOR_GROWTH


def endpoint_key(address: NetworkAddressV2) -> bytes:
    """Return the octets a persisted address is keyed on.

    The network id, the address and the port -- what names an endpoint
    on the wire -- and not `timestamp` or `services`: those are this
    node's own opinion of the endpoint, not part of what it is, so two
    records differing only in them settle on the one row written last
    rather than growing the table an entry per gossip or per reconnect.
    """
    endpoint = replace(address, timestamp=0, services=ServiceFlags.NODE_NONE)
    return endpoint.serialize(check_validity=False)


def _endpoint(address: NetworkAddressV2) -> tuple[int, bytes, int]:
    """Return the fields `endpoint_key` serializes, unserialized.

    Two addresses have one `endpoint_key` exactly where they have one of
    these, and this costs no `replace` and no `serialize`: what a walk
    over a whole table compares by (btclib-org/btclib-node#1283).
    """
    return address.network_id, address.address, address.port


def _host(address: NetworkAddressV2) -> tuple[int, bytes]:
    """Return the fields Core's `CNetAddr::operator==` compares: no port.

    `bool operator==(const CNetAddr& a, const CNetAddr& b)` (`src/
    netaddress.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) is
    `a.m_net == b.m_net && a.m_addr == b.m_addr` -- `host_key` below is
    `GetAddrBytes()`'s own octets alone, `m_addr`, and is paired with
    `network_id` here for `m_net`: `AddrManImpl::AddSingle`'s `addr ==
    source` (`src/addrman.cpp`, same sha) slices a `CAddress` down to
    its `CNetAddr` base before this operator ever runs, which is what
    drops the port from the comparison in Core too, not an omission of
    this function's own.
    """
    return address.network_id, host_key(address)


def host_key(address: NetworkAddressV2) -> bytes:
    """Return the octets Core's `CNetAddr::GetAddrBytes` gives: no port.

    What `P2pManager.discourage` keys on, as Core's `BanMan::Discourage`
    does (`src/banman.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), so every peer on one host shares one entry whatever port it
    connects from. An address an `addr` entry can carry is that entry's
    sixteen octets, an IPv4 one mapped, which makes an IPv4 peer and the
    same peer mapped into IPv6 one host; any other is its own octets,
    with no network id, as in Core.
    """
    if can_addrv1(address):
        return network_address(address).ip.packed
    return address.address


def _new_bucket_key() -> bytes:
    """Return a new secret for placing addresses, Core's `rand256()`.

    Core's `AddrMan` takes `uint256{1}` instead where it is built
    `deterministic`, which its own tests do; `tests/conftest.py` does as
    much here, by replacing this function.
    """
    return secrets.token_bytes(32)


def _cheap_hash(*fields: bytes) -> int:
    """Return `(HashWriter{} << fields...).GetCheapHash()`.

    `src/hash.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag: the first
    eight octets, little-endian, of the double SHA-256 of what was
    written. `fields` are already serialized.
    """
    digest = hashlib.sha256(hashlib.sha256(b"".join(fields)).digest()).digest()
    return int.from_bytes(digest[:8], "little")


def _vector(octets: bytes) -> bytes:
    """Return `octets` as a `std::vector<unsigned char>` serializes."""
    return var_int.serialize(len(octets)) + octets


def _address_key(address: NetworkAddressV2) -> bytes:
    """Return `CService::GetKey`: the address octets, then the port."""
    return host_key(address) + address.port.to_bytes(2, "big")


def _position(key: bytes, tag: bytes, bucket: int, address_key: bytes) -> int:
    """Return `AddrInfo::GetBucketPosition`: `N` is new, `K` is tried.

    `address_key` is the address's `_address_key`.
    """
    return (
        _cheap_hash(key, tag, bucket.to_bytes(4, "little"), _vector(address_key))
        % _BUCKET_SIZE
    )


def _new_slot(
    key: bytes,
    address: NetworkAddressV2,
    source_group: bytes,
    *,
    own: tuple[bytes, bytes] | None = None,
) -> tuple[int, int]:
    """Return the bucket and position of the new table that `address` maps to.

    Core's `AddrInfo::GetNewBucket` and `GetBucketPosition`
    (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag):
    the address's group and its source's group pick one of
    `_NEW_BUCKETS_PER_SOURCE_GROUP` buckets out of the source group's own,
    and the endpoint picks the position in it. `key` is Core's `nKey`, this
    table's own. `own` is the address's `net_group` and `_address_key`,
    for a caller that places one address many times.
    """
    group, address_key = own or (net_group(address), _address_key(address))
    source = _vector(source_group)
    spread = _cheap_hash(key, _vector(group), source) % _NEW_BUCKETS_PER_SOURCE_GROUP
    bucket = _cheap_hash(key, source, spread.to_bytes(8, "little")) % _NEW_BUCKET_COUNT
    return bucket, _position(key, b"N", bucket, address_key)


def _tried_slot(key: bytes, address: NetworkAddressV2) -> tuple[int, int]:
    """Return the bucket and position of the tried table that `address` maps to.

    Core's `AddrInfo::GetTriedBucket` and `GetBucketPosition`
    (`src/addrman.cpp`, same sha): the endpoint picks one of
    `_TRIED_BUCKETS_PER_GROUP` buckets out of its group's own.
    """
    address_key = _address_key(address)
    spread = _cheap_hash(key, _vector(address_key)) % _TRIED_BUCKETS_PER_GROUP
    bucket = (
        _cheap_hash(key, _vector(net_group(address)), spread.to_bytes(8, "little"))
        % _TRIED_BUCKET_COUNT
    )
    return bucket, _position(key, b"K", bucket, address_key)


class _Pending:
    """The writes `init_from_db` stages, in order, to put in one batch."""

    def __init__(self) -> None:
        self.operations: dict[bytes, bytes | None] = {}

    def put(self, key: bytes, value: bytes) -> None:
        """Stage a write."""
        self.operations[key] = value

    def delete(self, key: bytes) -> None:
        """Stage a deletion."""
        self.operations[key] = None

    def apply(self, wb: KeyValueStore) -> None:
        """Write what was staged."""
        for key, value in self.operations.items():
            if value is None:
                wb.delete(key)
            else:
                wb.put(key, value)


# Where a change to the table is written: `None` for a table kept in memory.
_Sink = KeyValueStore | _Pending | None


def _pack_groups(groups: list[bytes]) -> bytes:
    """Return net groups as stored: each behind its length."""
    return b"".join(bytes([len(group)]) + group for group in groups)


def _unpack_groups(raw: bytes) -> list[bytes]:
    """Return the net groups `_pack_groups` stored."""
    groups: list[bytes] = []
    at = 0
    while at < len(raw):
        groups.append(raw[at + 1 : at + 1 + raw[at]])
        at += 1 + raw[at]
    return groups


@dataclass
class AddrResponseCache:
    """One `getaddr` cache entry: a sample, and until when it is good for.

    Core's `CConnman::CachedAddrResponse` (`src/net.h`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `m_addrs_response_cache`
    and `m_cache_entry_expiration`. `PeerDB.addr_response_caches` holds
    one of these per key, in place of the one shared sample this table
    used to hold -- `callbacks.getaddr`'s own docstring is where the key
    itself, and the reason for more than one, is argued.
    """

    sample: list[NetworkAddressV2] = field(default_factory=list)
    # `0.0` starts already expired, so the first `getaddr` on a key
    # computes a sample rather than serving an empty one.
    expiration: float = 0.0


class PeerDB:
    """The table of addresses this node knows of, gossiped and self-confirmed.

    Core's addrman (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag). `addresses` is its `mapInfo`, every address heard about,
    and `active_addresses` is its tried table, the subset this node has
    itself dialled and heard back from. Each is behind its own lock,
    taken separately and never nested -- the comment beside each lock's
    own field says which thread reaches it and why sharing the other lock
    was declined -- and a move between the two holds a third around the
    other two.

    An address is in the new table, in up to `_NEW_BUCKETS_PER_ADDRESS`
    buckets (`_insert_new`), or in the tried table (`_make_tried`), as in
    Core.
    """

    def __init__(self, chain: Chain, data_dir: Path | None) -> None:
        """Load the durable tables, then decide whether DNS is still needed."""
        self.chain = chain
        self.data_dir = data_dir
        self.addresses: set[NetworkAddressV2] = set()
        # The `endpoint_key` of every member of `addresses`, kept in step
        # by `add_addresses` under `_addresses_lock` and by `init_from_db`
        # before this object is shared, so `_held` answers an endpoint
        # the table does not hold without walking the set.
        self._known_keys: set[bytes] = set()
        # Core's `mapInfo` and `vvNew`, under `_addresses_lock` and loaded
        # by `init_from_db`: the known row of each endpoint, the secret
        # that places an address, the new-table slots an endpoint holds
        # with the group of the source each was given by, the endpoint
        # each slot holds, the group of the first source (Core's
        # `AddrInfo::source`), `m_last_success` and `nAttempts`, the
        # endpoints in the tried table, Core's `m_tried_collisions`, and
        # `m_last_good`. An endpoint held in the tried table holds no slot
        # of the new one.
        self._rows: dict[tuple[int, bytes, int], NetworkAddressV2] = {}
        self._bucket_key = _new_bucket_key()
        self._slots: dict[tuple[int, bytes, int], dict[tuple[int, int], bytes]] = {}
        self._occupant: dict[tuple[int, int], tuple[int, bytes, int]] = {}
        self._source: dict[tuple[int, bytes, int], bytes] = {}
        self._stats: dict[tuple[int, bytes, int], _Stats] = {}
        self._tried_endpoints: set[tuple[int, bytes, int]] = set()
        self._collisions: set[tuple[int, bytes, int]] = set()
        # Core's `m_last_good{1}`, so that a first failure is counted
        self._last_good = 1.0
        # A lock of its own, not `_active_lock` below: `add_addresses`
        # reaches this set from both threads too (#298) -- gossip
        # through `callbacks.addr`/`addrv2` on `Node`'s, DNS seed
        # answers through `query_dns_seed` on `P2pManager`'s, and
        # `address_sampler`'s own dialable-address comprehension on
        # `P2pManager`'s as well, racing against gossip on `Node`'s, and
        # `add_active_address` asks `_known_keys` under it on `Node`'s.
        # Unprotected, that last pairing is not only the lost-update or
        # wrong-row risk `_active_lock` guards against: iterating a
        # `set` while another thread mutates it is `RuntimeError: Set
        # changed size during iteration` in CPython, a crash rather than
        # a silent corruption. Sharing `_active_lock` instead was
        # measured and declined: nothing here ever needs the two tables
        # updated as one atomic step, and `add_addresses`'s own durable
        # write batch is measurably slower than `add_active_address`'s
        # single row -- sharing would let it hold up a handshake for no
        # invariant this table's own lock does not already give it.
        self._addresses_lock = threading.Lock()
        self.active_addresses: list[NetworkAddressV2] = []
        # endpoint bytes -> its position in `active_addresses`, so
        # `add_active_address` can find a repeat endpoint's row in O(1)
        # rather than by scanning the list it is called once per
        # handshake against (#270).
        self._active_index: dict[bytes, int] = {}
        # `add_addresses` and `set_services` read this index and then write
        # into `active_addresses` at the position found -- two statements,
        # not one -- while a move between the tables, on either thread,
        # reshapes the list. Interleaved without a lock, a
        # position read before a move can be written after it: one
        # endpoint's row silently holding another endpoint's data, or an
        # `IndexError`. `KeyValueStore` has its own lock for the store;
        # this one is for these in-memory structures alone.
        self._active_lock = threading.Lock()
        # Core's `vvTried`, under `_active_lock`: the slot of each answered
        # endpoint, by `endpoint_key`, and the endpoint each slot holds.
        self._tried_slot_of: dict[bytes, tuple[int, int]] = {}
        self._tried_occupant: dict[tuple[int, int], bytes] = {}
        # A third lock, taken first and held while a move between the two
        # tables takes the other two in turn: `Good_` and
        # `ResolveCollisions_` each read one table and write the other,
        # from `Node`'s thread and `P2pManager`'s. Of the three, it is the
        # only one held across a take of another.
        self._move_lock = threading.Lock()
        # What `callbacks.getaddr` last answered with, keyed the way
        # Core's own `m_addr_response_caches` is: one cache per
        # `Connection.addr_cache_key`, network and local socket, rather
        # than one shared sample, so that two inbound connections
        # cannot compare answers to link this node across networks or
        # listeners (`AddrResponseCache`'s own docstring quotes Core's
        # reasoning). A fresh `secrets.SystemRandom().sample` on every
        # `getaddr` would let two peers connecting close together
        # compare answers and infer what changed between them within
        # one key too, which serving one cache per connection alone
        # does not stop -- a new connection on the same key still draws
        # fresh. btclib-org/btclib-node#71, btclib-org/btclib-node#1478
        self.addr_response_caches: dict[tuple[int, str, int], AddrResponseCache] = {}
        # Core's `AddrInfo::m_last_try`, by `endpoint_key`: when this
        # node last tried to connect to an endpoint either table holds.
        # In memory only, as `AddrInfo`'s serialization leaves
        # `m_last_try` out of `peers.dat` (`src/addrman_impl.h`,
        # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag). `attempt` writes it
        # from `P2pManager`'s thread alone. `last_try` reads it from
        # there too, and from `get_addr`'s call into
        # `_aged_out`'s grace check, reachable from
        # `Node`'s thread through `callbacks.getaddr`
        # (btclib-org/btclib-node#1435).
        # Written under `_addresses_lock`, by `attempt` and `_good`, and
        # read without it on purpose: a write is one step, an item set
        # or a reassignment of the whole dict, so a concurrent `last_try`
        # sees a whole dict -- at worst one write stale, the same
        # imprecision `is_empty` already accepts.
        self._last_try: dict[bytes, float] = {}

        # `None` is a table kept in memory only, which is what every
        # test here wants and what `data_dir` was before this: assigned
        # and never opened. `Node` always passes an actual directory
        # (`Config.data_dir` has no `None` of its own), so this is the
        # one place that distinction is made.
        self.db = KeyValueStore(data_dir / "peers") if data_dir is not None else None

        self.init_from_db()

    def init_from_db(self) -> None:
        """Load every stored address into `addresses` or `active_addresses`.

        One store keyed by several prefixes (the comment on `_KNOWN` and
        its neighbours above argues why), so this walks it whole and
        dispatches on the prefix rather than stopping at the first key
        without one. A row `_storable` refuses is deleted rather than
        loaded, as Core's addrman holds no such address, and so is an
        answered row whose endpoint no known row holds, as Core's tried
        table holds nothing addrman does not.

        Rows are placed again as Core places what it reads from
        `peers.dat` (`_place_stored`). A row with no stored source, as an
        earlier release wrote them, is its own source: the rows of one
        group share its one bucket, and those over its room are dropped.
        """
        if self.db is None:
            return
        self._load_bucket_key(self.db)
        pending = _Pending()
        known, answered, sources, stats = self._read_store(self.db, pending)
        self._place_stored(known, sources, stats, answered, pending)
        for suffix in answered.keys() - self._known_keys:
            pending.delete(_ANSWERED + suffix)
        for orphans, prefix in ((sources, _SOURCE), (stats, _STATS)):
            for suffix in orphans:
                pending.delete(prefix + suffix)
        with self.db.write_batch() as wb:
            pending.apply(wb)

    def _read_store(
        self, db: KeyValueStore, pending: _Pending
    ) -> tuple[
        list[tuple[bytes, NetworkAddressV2]],
        dict[bytes, NetworkAddressV2],
        dict[bytes, list[bytes]],
        dict[bytes, bytes],
    ]:
        """Read the store whole: known rows, answered rows, sources, stats.

        Each is by the `endpoint_key` its key ends in. A row `_storable`
        refuses is staged for deletion in `pending`.
        """
        known: list[tuple[bytes, NetworkAddressV2]] = []
        answered: dict[bytes, NetworkAddressV2] = {}
        sources: dict[bytes, list[bytes]] = {}
        stats: dict[bytes, bytes] = {}
        for key, value in db:
            if key.startswith(_SOURCE):
                sources[key[len(_SOURCE) :]] = _unpack_groups(value)
            elif key.startswith(_STATS):
                stats[key[len(_STATS) :]] = value
            elif key.startswith((_KNOWN, _ANSWERED)):
                address = NetworkAddressV2.parse(value, check_validity=False)
                if not _storable(address):
                    pending.delete(key)
                elif key.startswith(_KNOWN):
                    known.append((key[len(_KNOWN) :], address))
                else:
                    answered[key[len(_ANSWERED) :]] = address
        return known, answered, sources, stats

    def _load_bucket_key(self, db: KeyValueStore) -> None:
        """Take the key the store holds, or store the one this table has."""
        stored = db.get_meta(_BUCKET_KEY)
        if stored is None:
            db.put_meta(_BUCKET_KEY, self._bucket_key)
        else:
            self._bucket_key = stored

    def _place_stored(
        self,
        known: list[tuple[bytes, NetworkAddressV2]],
        sources: dict[bytes, list[bytes]],
        stats: dict[bytes, bytes],
        answered: dict[bytes, NetworkAddressV2],
        pending: _Pending,
    ) -> None:
        """Place the stored known rows as `AddrManImpl::Unserialize` does.

        An answered row takes its tried slot. Where another holds it, the
        row goes to the new table at its first source's slot, as `MakeTried`
        sends the entry it displaces, and is lost only if that is held too.
        Core's own file never meets this: one `peers.dat` holds no two tried
        entries for a slot, so it is no divergence on a path Core reaches.
        Here it is the first start over an earlier release's store, whose
        answered rows were never placed by slot.

        A row of the new table takes the slot of each group it was
        given a slot by, up to `_NEW_BUCKETS_PER_ADDRESS`; where another
        holds that slot, it is tried once more at the slot of its first
        source, and is not placed where that is held too. A row left with
        no slot is lost, and so are its other rows, which are staged in
        `pending` for deletion. What is read out of `sources` and `stats`
        is removed from them, the rest being left for the caller to delete.
        """
        for suffix, address in known:
            endpoint = _endpoint(address)
            own = (net_group(address), _address_key(address))
            groups = sources.pop(suffix, None) or [own[0]]
            primary = groups[0]
            entry = _Stats()
            raw = stats.pop(suffix, None)
            if raw is not None:
                entry.last_success = int.from_bytes(raw[:8], "little")
                entry.attempts = int.from_bytes(raw[8:12], "little")
            tried = answered.get(suffix)
            if tried is not None:
                entry.last_success = tried.timestamp
                slot = _tried_slot(self._bucket_key, address)
                if slot not in self._tried_occupant:
                    self._stats[endpoint] = entry
                    self._adopt(address, primary)
                    self._tried_endpoints.add(endpoint)
                    self._tried_insert(suffix, tried, slot)
                    continue
                self._restore_slot(address, endpoint, primary, primary, own)
                pending.delete(_ANSWERED + suffix)
            else:
                for group in groups[1:] or [primary]:
                    self._restore_slot(address, endpoint, group, primary, own)
            if endpoint in self._slots:
                self._stats[endpoint] = entry
                self._adopt(address, primary)
                if groups != [primary, *dict.fromkeys(self._slots[endpoint].values())]:
                    self._put_sources(endpoint, pending)
                if tried is not None:
                    self._put_stats(endpoint, pending)
            else:
                for prefix in (_KNOWN, _SOURCE, _STATS, _ANSWERED):
                    pending.delete(prefix + suffix)

    def _restore_slot(
        self,
        address: NetworkAddressV2,
        endpoint: tuple[int, bytes, int],
        group: bytes,
        primary: bytes,
        own: tuple[bytes, bytes],
    ) -> None:
        """Give a stored row the slot of `group`, or of `primary` if held.

        `Unserialize`'s own rule for the new table: a free slot is taken,
        and a held one sends the row once to its first source's slot. The
        table is not yet shared.
        """
        if len(self._slots.get(endpoint, ())) >= _NEW_BUCKETS_PER_ADDRESS:
            return
        slot = _new_slot(self._bucket_key, address, group, own=own)
        if slot in self._occupant:
            group = primary
            slot = _new_slot(self._bucket_key, address, primary, own=own)
            if slot in self._occupant:
                return
        self._occupant[slot] = endpoint
        self._slots.setdefault(endpoint, {})[slot] = group

    def _adopt(self, address: NetworkAddressV2, group: bytes) -> None:
        """Hold `address` as a known row from the source of `group`.

        The caller holds `_addresses_lock`, or the table is not yet shared.
        """
        endpoint = _endpoint(address)
        self._rows[endpoint] = address
        self.addresses.add(address)
        self._known_keys.add(endpoint_key(address))
        self._source[endpoint] = group

    def _tried_insert(
        self, key: bytes, row: NetworkAddressV2, slot: tuple[int, int]
    ) -> None:
        """Put `row` in the tried slot, in memory.

        The caller holds `_active_lock`, or the table is not yet shared.
        """
        self._active_index[key] = len(self.active_addresses)
        self.active_addresses.append(row)
        self._tried_slot_of[key] = slot
        self._tried_occupant[slot] = key

    def _tried_remove(self, key: bytes) -> NetworkAddressV2:
        """Take the row of `key` out of the tried table, in memory.

        The last row takes its place in `active_addresses`. The caller
        holds `_active_lock`.
        """
        position = self._active_index.pop(key)
        last = self.active_addresses.pop()
        removed = last
        if position < len(self.active_addresses):
            removed = self.active_addresses[position]
            self.active_addresses[position] = last
            self._active_index[endpoint_key(last)] = position
        del self._tried_occupant[self._tried_slot_of.pop(key)]
        return removed

    def _insert_new(
        self,
        address: NetworkAddressV2,
        group: bytes,
        now: float,
        wb: _Sink,
    ) -> bool:
        """Give an endpoint a slot of the new table from `group`, if it can.

        Core's slot insertion in `AddrManImpl::AddSingle` (`src/addrman.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), answering whether it
        did. A slot the endpoint holds already is not a new insertion. A
        slot another endpoint holds goes to this one if its holder is
        terrible, or is in several buckets and this endpoint in none;
        otherwise it stays with its holder. The caller holds
        `_addresses_lock`, and puts a new endpoint in `addresses`,
        `_known_keys` and `_rows` (`_adopt`) when this answers `True`.
        """
        endpoint = _endpoint(address)
        slot = _new_slot(self._bucket_key, address, group)
        holder = self._occupant.get(slot)
        if holder == endpoint:
            return False
        if holder is not None:
            old = self._rows[holder]
            held_elsewhere = len(self._slots[holder]) > 1
            if not (
                _aged_out(old, now, self.last_try(old), self._stats.get(holder))
                or (held_elsewhere and endpoint not in self._slots)
            ):
                return False
            self._clear_slot(slot, wb)
        self._occupant[slot] = endpoint
        self._slots.setdefault(endpoint, {})[slot] = group
        return True

    def _clear_slot(self, slot: tuple[int, int], wb: _Sink) -> None:
        """Empty a slot of the new table: Core's `ClearNew`.

        An endpoint left with no slot is forgotten. The caller holds
        `_addresses_lock`.
        """
        holder = self._occupant.pop(slot)
        slots = self._slots[holder]
        del slots[slot]
        if slots:
            self._put_sources(holder, wb)
        else:
            del self._slots[holder]
            self._forget(holder, wb)

    def _forget(self, endpoint: tuple[int, bytes, int], wb: _Sink) -> None:
        """Drop an endpoint from the table: Core's `Delete`.

        The caller holds `_addresses_lock`.
        """
        row = self._rows.pop(endpoint)
        key = endpoint_key(row)
        self.addresses.discard(row)
        self._known_keys.discard(key)
        del self._source[endpoint]
        self._stats.pop(endpoint, None)
        self._collisions.discard(endpoint)
        if wb is not None:
            for prefix in (_KNOWN, _SOURCE, _STATS):
                wb.delete(prefix + key)

    def _put_sources(self, endpoint: tuple[int, bytes, int], wb: _Sink) -> None:
        """Write the groups of an endpoint's first source and of its slots.

        The first source is Core's `AddrInfo::source`, which never changes
        and is where an entry goes when it is returned to the new table.
        The caller holds `_addresses_lock`.
        """
        if wb is not None:
            groups = [
                self._source[endpoint],
                *dict.fromkeys(self._slots.get(endpoint, {}).values()),
            ]
            wb.put(_SOURCE + endpoint_key(self._rows[endpoint]), _pack_groups(groups))

    def _put_stats(self, endpoint: tuple[int, bytes, int], wb: _Sink) -> None:
        """Write `m_last_success` and `nAttempts`, or delete them if none.

        The answered row's time is `m_last_success` of an endpoint in the
        tried table, so only the attempts of that one are kept. The caller
        holds `_addresses_lock`.
        """
        if wb is None:
            return
        entry = self._stats[endpoint]
        key = _STATS + endpoint_key(self._rows[endpoint])
        tried = endpoint in self._tried_endpoints
        if entry.attempts or (entry.last_success and not tried):
            wb.put(
                key,
                int(entry.last_success).to_bytes(8, "little")
                + entry.attempts.to_bytes(4, "little"),
            )
        else:
            wb.delete(key)

    @contextmanager
    def _write_batch(self) -> Iterator[KeyValueStore | None]:
        """Yield a batch to write into, or `None` where there is nothing to.

        One shape either way, so a caller writes `if wb is not None`
        around the puts it makes and nothing around the batch itself --
        the alternative, a call site branching on `self.db` before ever
        reaching a loop, is what this exists to not be.
        """
        if self.db is None:
            yield None
            return
        with self.db.write_batch() as wb:
            yield wb

    def close(self) -> None:
        """Close the durable store, if this table has one."""
        if self.db is not None:
            self.db.close()

    async def query_dns_seed(self, seed: str) -> str | None:
        """Ask one chain DNS seed's `x9.` subdomain; return it if unanswered.

        Core's `ThreadDNSAddressSeed` asks `x%x.<seed>` of
        `requiredServiceBits`, `SeedsServiceFlags()` (`x9.` for
        `NODE_NETWORK | NODE_WITNESS`), on which a seed answers only with
        peers it believes offer those services -- `src/net.cpp` and
        `src/protocol.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag --
        and keeps at most `nMaxIPs`, 32, of the answers. One seed at a
        time, rather than every one of them in a single call, is what
        lets `P2pManager._dns_address_seed` wait between batches of
        `_DNS_SEEDS_TO_QUERY_AT_ONCE` (btclib-org/btclib-node#1265): that
        coroutine is what shuffles the chain's seeds, decides how many
        to ask before the next wait, and queues `seed` as its own
        addr-fetch where this returns it rather than `None` -- Core
        makes an `ADDR_FETCH` connection to the bare name instead
        (`AddAddrFetch(seed)`).
        """
        chain = self.chain
        loop = asyncio.get_running_loop()
        host = f"x{int(SEEDS_SERVICE_FLAGS):x}.{seed}"
        # what the subdomain answers with, deduplicated: a name
        # resolves once per socket type absent a `type` hint, and
        # `SOCK_STREAM` is what a peer table wants of it.
        endpoints: set[tuple[str, int]] = set()
        try:
            answers = await loop.getaddrinfo(host, chain.port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            answers = []
        # (family, type, proto, canonname, sockaddr), and the
        # sockaddr is the only part a peer table wants. It opens
        # with the host and the port -- two fields for AF_INET,
        # four for AF_INET6, whose flow info and scope id say
        # nothing a BIP155 record holds. The stub also admits
        # AF_PACKET's (protocol, address) pair, which resolving an
        # internet host and a port cannot answer with, so the cast
        # is what that fact is written as rather than a check no
        # test could reach.
        for *_, sockaddr in answers:
            endpoints.add(cast("tuple[str, int]", sockaddr[:2]))
            if len(endpoints) >= _MAX_SEED_ANSWERS:
                break
        if not endpoints:
            return seed
        # through add_addresses, and not a bare add to the set: a
        # seed is gossip like a peer's is, and belongs in the
        # durable table the same way, so a later restart has it
        # without asking again. `time_penalty=0`: `AddrMan::Add`'s own
        # default, since `_dns_seed_timestamp` below already backdates
        # the entry the way Core's own caller does before it ever
        # reaches `Add` (`ThreadDNSAddressSeed`, `src/net.cpp`, at
        # bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
        self.add_addresses(
            (
                peer_address(
                    ip,
                    port,
                    timestamp=_dns_seed_timestamp(),
                    services=SEEDS_SERVICE_FLAGS,
                )
                for ip, port in endpoints
            ),
            source=internal_source(host),
            time_penalty=0,
        )
        return None

    @property
    def size(self) -> int:
        """Return Core's `addrman.Size()`: every distinct endpoint known.

        Core's `vRandom.size()` (`AddrManImpl::Size_`, `src/addrman.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) counts one entry
        per address, new or tried, because `Good_` *moves* an entry from
        the new table to the tried one rather than copying it -- the two
        never hold the one address at once there. This table's own
        `active_addresses` is not `addresses` with one row moved out: an
        answered endpoint is left in `addresses` too (`_good` leaves the
        known row in place). A bare
        `len(self.addresses) + len(self.active_addresses)` therefore
        counted every answered endpoint twice.
        The honest count is `addresses`' own size plus whatever
        `active_addresses` holds that `addresses` does not -- read as
        `_known_keys`, kept in step with `addresses` by `add_addresses`
        (a slot taken over by a newcomer drops its holder's key), and
        `_active_index`, kept in step with `active_addresses` the same
        way, so counting keys rather than walking either table answers
        without a stale-row's own fields mattering. Locked, unlike
        `is_empty` below: `_known_keys` is snapshotted under
        `_addresses_lock` first, into a local `set` this thread alone
        holds, so the walk of `_active_index` under `_active_lock` next
        reads no collection a third thread could still be mutating --
        `add_addresses`'s own two-lock methods take the two the same
        way, never nested.
        """
        with self._addresses_lock:
            known_count = len(self.addresses)
            known_keys = set(self._known_keys)
        with self._active_lock:
            unmatched = sum(1 for key in self._active_index if key not in known_keys)
        return known_count + unmatched

    @property
    def is_empty(self) -> bool:
        """Whether `addresses` holds nothing at all, read without a lock."""
        # Unlocked on purpose: `len` on a set is one step, not a walk of
        # it, so there is nothing here for another thread's `add`/
        # `discard` to catch mid-stride -- the answer is at worst one
        # mutation stale, the same imprecision `manage_connections`
        # already reads this property through (a table this answers
        # empty for can gain an entry the instant after, dialable or
        # not, and nothing here promised otherwise).
        return not len(self.addresses)

    def holds_network(self, network_id: int) -> bool:
        """Whether either table holds an address on `network_id`.

        Core's `addrman.Size(net) != 0`, which `GetReachableEmptyNetworks`
        asks of each reachable network (`src/net.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Each table is read
        under its own lock, one after the other.
        """
        with self._addresses_lock:
            known = any(a.network_id == network_id for a in self.addresses)
        with self._active_lock:
            answered = any(a.network_id == network_id for a in self.active_addresses)
        return known or answered

    def random_address(self) -> NetworkAddressV2 | None:
        """Return a random dialable address, or `None` if there is none.

        One draw of `address_sampler`.
        """
        return self.address_sampler()()

    def address_sampler(
        self, *, new_only: bool = False, network: Network | None = None
    ) -> Callable[[], NetworkAddressV2 | None]:
        """Return a draw over the dialable addresses of both tables, as of now.

        Each call of what this returns is one `_select`, between the
        answered table and the gossiped one, and in either a bucket
        before an address in it, kept as `GetChance` weighs it, so
        `P2pManager` can draw many times a pass for the price of a copy of
        each table's slots. An answered endpoint holds no slot of the
        gossiped table, as Core's `Good_` moves an entry from the new table
        to the tried one and `Select_` flips between two tables that never
        hold one endpoint twice. With `new_only` the draw is from the
        gossiped side alone, Core's `Select(true, ...)` that a feeler makes,
        and with `network` from both sides kept to the one network, as
        `CNetAddr::GetNetwork` names it: Core's `Select(false, {network})`,
        which an extra network peer makes.

        A terrible entry is not left out of a draw: Core's `Select_`
        (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
        never calls `IsTerrible`, only `GetAddr_`'s serving and
        `AddSingle`'s overwrite of a slot do (btclib-org/btclib-node#1434).
        A terrible tried entry stays in the tried table until another
        address takes its slot, as Core's does.
        """

        def fits(row: NetworkAddressV2) -> bool:
            return can_connect(row) and (network is None or get_network(row) == network)

        def chance(row: NetworkAddressV2) -> float:
            entry = self._stats.get(_endpoint(row))
            return _chance(
                time.time(), self.last_try(row), entry.attempts if entry else 0
            )

        # Locked, unlike `is_empty` above: a copy of a dict another thread
        # is writing can fail with `RuntimeError: dictionary changed size
        # during iteration`, and add_addresses (#298) reaches it from Node's
        # own thread while this runs on P2pManager's.
        with self._addresses_lock:
            rows = dict(self._rows)
            occupant = dict(self._occupant)
        new = _Side(
            occupant,
            list({slot[0] for slot in occupant}),
            rows.__getitem__,
            fits,
            chance,
        )
        if new_only:
            return partial(_select, None, new)
        with self._active_lock:
            tried_occupant = dict(self._tried_occupant)
            tried_rows = {
                key: self.active_addresses[position]
                for key, position in self._active_index.items()
            }
        tried = _Side(
            tried_occupant,
            list({slot[0] for slot in tried_occupant}),
            tried_rows.__getitem__,
            fits,
            chance,
        )
        return partial(_select, tried, new)

    def add_addresses(
        self,
        addresses: Iterable[NetworkAddressV2],
        *,
        source: NetworkAddressV2 | None = None,
        time_penalty: float = _GOSSIP_TIME_PENALTY,
    ) -> int:
        """Merge `addresses` into the new table, checked and deduplicated.

        Returns how many of them the new table took a slot for, new
        endpoints and further buckets of held ones alike: Core's `Add`
        answers whether there were any.

        An address `_storable` refuses is dropped. Every other address
        settles onto its own `endpoint_key` row, its services ORed into
        those the row held. The services are ORed into the endpoint's
        answered row as well, where it has one. Takes `_addresses_lock`,
        then `_active_lock`, the two never nested.

        Placement is Core's `AddSingle` (`_insert_new`): a new endpoint
        takes the slot its group and the group of `source` map it to, and an
        endpoint held and not tried may take a further one, up to
        `_NEW_BUCKETS_PER_ADDRESS`, if the gossip is newer than its time and
        a draw of one in two to the power of the slots it holds comes up.
        What one source group gives spreads over
        `_NEW_BUCKETS_PER_SOURCE_GROUP` buckets at most. With no `source` an
        address is its own source.

        A new row's timestamp is the gossiped one, `time_penalty` seconds
        less, floored at zero; a row held keeps its own unless the gossip
        is newer by more than an hour, a day where it is a day old
        itself (btclib-org/btclib-node#1603) -- Core's
        `AddrManImpl::AddSingle` (`src/addrman.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `pinfo->nTime =
        max(0, addr.nTime - time_penalty)`. `time_penalty` defaults to
        `_GOSSIP_TIME_PENALTY`, the flat two hours Core's own `ADDR`/
        `ADDRV2` handler passes `AddrMan::Add` for a whole gossiped batch
        (`net_processing.cpp`, same sha); `query_dns_seed` below and
        `P2pManager._maybe_add_fixed_seeds` both pass `0`,
        `AddrMan::Add`'s own default (`ThreadOpenConnections`'s
        `addrman.get().Add(seed_addrs, local)` and `ThreadDNSAddressSeed`'s
        `addrman.get().Add(vAdd, resolveSource)` alike pass no third
        argument), each answer already carrying a timestamp Core backdates
        itself before it ever reaches here. An
        address equal to `source` -- host only, `_host` rather than
        `==`, since two records differing in `timestamp`, `services` or
        even port are still one self-announcement -- is exempted from
        the penalty, as `AddSingle`'s own `if (addr == source) { time_penalty
        = 0s; }` is: a peer's word for its own address costs it nothing,
        where the same word for somebody else's does. The port is
        dropped from the comparison because Core's is: `source` there is
        a `CNetAddr`, not a `CAddress`, and `addr == source` slices
        `addr` down to its own `CNetAddr` base first
        (`AddrManImpl::AddSingle`, `src/addrman.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag) -- `_host`'s own
        docstring is where `CNetAddr::operator==` itself is read. An
        inbound `source` carries the peer's ephemeral source port from
        `sock.accept()`'s own peername (`callbacks.py`'s own `version`
        handler rewrites `conn.address` for an outbound connection
        alone), which an endpoint-including comparison would have
        compared against the peer's own announced listening port and
        almost never matched (btclib-org/btclib-node#1380, review round
        2) -- host-only is not merely Core's own comparison, it is what
        makes the exemption reachable for an inbound peer at all.
        """
        # what each endpoint kept was gossiped with, for its answered row
        gossiped: dict[bytes, ServiceFlags] = {}
        added = 0
        now = time.time()
        source_host = _host(source) if source is not None else None
        source_group = net_group(source) if source is not None else None
        with self._addresses_lock, self._write_batch() as wb:
            for address in addresses:
                # BIP155's ignore rule: an IPV6 record that is really an
                # IPv4 or a (long-retired) TORv2 address wearing another
                # network's sixteen octets is not a second peer, and
                # keeping it under network id 2 is what used to gossip
                # it back as IPv4 -- the same host, twice in the table
                # (#151). Checked before the durable write too, so a
                # dropped record is dropped everywhere, not merely kept
                # out of the in-memory set.
                if not _storable(address):
                    continue
                key = endpoint_key(address)
                endpoint = _endpoint(address)
                # one row per endpoint, whatever `services` it was gossiped
                # with (#247)
                existing = self._rows.get(endpoint)
                # a gossip adds services to an endpoint already held and
                # never takes one away, as Core's `AddSingle` ORs them in
                # (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the
                # v31.1 tag)
                services = address.services
                if existing is not None:
                    services |= existing.services
                penalty = 0.0 if _host(address) == source_host else time_penalty
                timestamp = _held_time(existing, address, penalty, now)
                known = replace(address, timestamp=timestamp, services=services)
                group = source_group or net_group(address)
                if existing is None:
                    if not self._insert_new(known, group, now, wb):
                        continue
                    self._adopt(known, group)
                    self._put_sources(endpoint, wb)
                    added += 1
                else:
                    self._replace_row(endpoint, existing, known)
                    added += self._insert_further(address, known, group, now, wb)
                if wb is not None and known != existing:
                    wb.put(_KNOWN + key, known.serialize(check_validity=False))
                gossiped[key] = (
                    gossiped.get(key, ServiceFlags.NODE_NONE) | address.services
                )
        # Core keeps one entry per endpoint, and `AddSingle` ORs gossip
        # into a tried one as into a new one
        with self._active_lock:
            for key, services in gossiped.items():
                position = self._active_index.get(key)
                if position is not None:
                    row = self.active_addresses[position]
                    self._set_answered(
                        position, replace(row, services=row.services | services)
                    )
        return added

    def _insert_further(
        self,
        gossip: NetworkAddressV2,
        known: NetworkAddressV2,
        group: bytes,
        now: float,
        wb: _Sink,
    ) -> bool:
        """Give a held endpoint a further slot from `group`, if Core would.

        `AddSingle` (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the
        v31.1 tag) does so for an endpoint whose gossip is newer than the
        time it now holds, that is not in the tried table and holds fewer
        than `_NEW_BUCKETS_PER_ADDRESS` slots, and then at odds of one to
        two to the power of the slots held. The caller holds
        `_addresses_lock`.
        """
        endpoint = _endpoint(known)
        if gossip.timestamp <= known.timestamp or endpoint in self._tried_endpoints:
            return False
        held = len(self._slots.get(endpoint, ()))
        if held >= _NEW_BUCKETS_PER_ADDRESS or (held and not _roll(1 << held)):
            return False
        if not self._insert_new(known, group, now, wb):
            return False
        self._put_sources(endpoint, wb)
        return True

    def _replace_row(
        self,
        endpoint: tuple[int, bytes, int],
        old: NetworkAddressV2,
        new: NetworkAddressV2,
    ) -> None:
        """Hold `new` for an endpoint in place of `old`.

        The caller holds `_addresses_lock`.
        """
        self.addresses.discard(old)
        self.addresses.add(new)
        self._rows[endpoint] = new

    def set_services(self, address: NetworkAddressV2, services: ServiceFlags) -> None:
        """Overwrite the services held for `address`'s endpoint.

        Core's `AddrMan::SetServices`, which `ProcessMessage` calls with
        an outbound peer's own `version` (`src/net_processing.cpp`,
        `src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag): what a gossip only adds to, the peer's own word replaces.
        The known row and the answered row are both written, standing for
        Core's one entry, and an endpoint neither holds is not recorded.
        Takes `_addresses_lock`, then `_active_lock`, the two never
        nested.
        """
        key = endpoint_key(address)
        with self._addresses_lock, self._write_batch() as wb:
            existing = self._held(address)
            if existing is None:
                return
            known = replace(existing, services=services)
            self._replace_row(_endpoint(address), existing, known)
            if wb is not None:
                wb.put(_KNOWN + key, known.serialize(check_validity=False))
        with self._active_lock:
            position = self._active_index.get(key)
            if position is not None:
                row = self.active_addresses[position]
                self._set_answered(position, replace(row, services=services))

    def _held(self, address: NetworkAddressV2) -> NetworkAddressV2 | None:
        """Return the known row of `address`'s endpoint, or `None`.

        The caller holds `_addresses_lock`.
        """
        return self._rows.get(_endpoint(address))

    def _set_answered(self, position: int, row: NetworkAddressV2) -> None:
        """Write `row` at `position` of `active_addresses`, and to the store.

        The caller holds `_active_lock`.
        """
        self.active_addresses[position] = row
        if self.db is not None:
            self.db.put(
                _ANSWERED + endpoint_key(row), row.serialize(check_validity=False)
            )

    def attempt(
        self, address: NetworkAddressV2, *, count_failure: bool = False
    ) -> None:
        """Record a try to connect to `address`, as Core's `Attempt_` does.

        `AddrManImpl::Attempt_` (`src/addrman.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag) sets `m_last_try` on
        the entry addrman holds for the address, and does nothing where
        it holds none; so is an endpoint `_known_keys` does not hold left
        out here, an answered one being known too. With `count_failure`
        it also counts an attempt, `nAttempts`, once for the tries since
        the last `Good_`: `ThreadOpenConnections` passes it once the node
        holds outbound peers in enough net groups to be online. A try is
        kept as long as `Good_` and `ResolveCollisions_` read it,
        `_ADDRMAN_REPLACEMENT`.
        """
        key = endpoint_key(address)
        endpoint = _endpoint(address)
        now = time.time()
        with self._addresses_lock, self._write_batch() as wb:
            if endpoint not in self._rows:
                return
            self._last_try = {
                tried: when
                for tried, when in self._last_try.items()
                if now - when < _ADDRMAN_REPLACEMENT
            }
            self._last_try[key] = now
            entry = self._stats.setdefault(endpoint, _Stats())
            if count_failure and entry.last_count_attempt < self._last_good:
                entry.last_count_attempt = now
                entry.attempts += 1
                self._put_stats(endpoint, wb)

    def last_try(self, address: NetworkAddressV2) -> float:
        """Return when `address` was last tried, `0.0` for never or long ago."""
        return self._last_try.get(endpoint_key(address), 0.0)

    def get_addr(
        self, max_addresses: int, max_pct: int, network: Network | None = None
    ) -> list[NetworkAddressV2]:
        """Return a random sample of the known addresses, the terrible left out.

        Core's `AddrManImpl::GetAddr_` (`src/addrman.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the answer is sized
        from every entry, new and tried -- here `addresses`, which holds
        every answered endpoint too -- as `max_pct` percent of them, `0`
        for all, then at most `max_addresses`, `0` for no limit. Entries
        are then drawn at random, skipping one `_aged_out` calls terrible
        and, with `network`, one on another network, until the size is
        reached or the table is spent, so a table of terrible entries
        answers less than its size. The percentage rounds up where Core's
        rounds down: a table of a handful of addresses, every functional
        test's own two-node regtest, would otherwise answer none
        (btclib-org/btclib-node#71).
        """
        now = time.time()
        with self._addresses_lock:
            rows = list(self.addresses)
        size = len(rows)
        if max_pct:
            size = -(-size * min(max_pct, 100) // 100)
        if max_addresses:
            size = min(size, max_addresses)
        secrets.SystemRandom().shuffle(rows)
        sample: list[NetworkAddressV2] = []
        for row in rows:
            if len(sample) >= size:
                break
            if network is not None and network_class(row) != network:
                continue
            if not _aged_out(
                row, now, self.last_try(row), self._stats.get(_endpoint(row))
            ):
                sample.append(row)
        return sample

    def add_active_address(
        self, addr: NetworkAddressV2, *, test_before_evict: bool = True
    ) -> bool:
        """Record `addr` as answered, and say whether it moved to the tried one.

        Core's `AddrManImpl::Good_` (`src/addrman.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the entry is marked
        as connected to now, its attempts are reset, and `nTime` is left
        alone, "to avoid leaking information about currently-connected
        peers" -- the known row is not written, and the answered row is
        stamped now, `m_last_success`, which nothing serves. `connected`
        is what moves `nTime`. An endpoint `addresses` does not hold is
        not recorded, as `Good_` updates only an entry addrman already has,
        and neither is an address `_storable` refuses, which `addresses`
        never holds.

        The entry leaves every bucket of the new table for its slot in the
        tried table, and the entry that held that slot goes back to the
        new table. With `test_before_evict`, `Good`'s own default, an
        entry that would take a held slot waits instead in the collisions
        `resolve_collisions` settles, and this answers `False`, as it does
        for an entry already tried, whose row a repeat handshake settles
        onto (#270). Takes `_move_lock`, then `_addresses_lock` and
        `_active_lock` in turn, never nested.
        """
        with self._move_lock:
            return self._good(addr, time.time(), test_before_evict=test_before_evict)

    def _good(
        self, addr: NetworkAddressV2, now: float, *, test_before_evict: bool
    ) -> bool:
        """Do `Good_`; the caller holds `_move_lock`."""
        endpoint = _endpoint(addr)
        key = endpoint_key(addr)
        with self._addresses_lock, self._write_batch() as wb:
            # before the lookup, as `Good_` sets `m_last_good` ahead of `Find`
            self._last_good = now
            known = self._rows.get(endpoint)
            if known is None:
                return False
            entry = self._stats.setdefault(endpoint, _Stats())
            entry.last_success = now
            entry.attempts = 0
            self._last_try[key] = now
            tried = endpoint in self._tried_endpoints
            slot = _tried_slot(self._bucket_key, known)
            self._put_stats(endpoint, wb)
        answered = replace(addr, timestamp=int(now))
        with self._active_lock:
            if tried:
                # a repeat handshake with an endpoint already held settles
                # onto its one row rather than growing the table an entry
                # per reconnect (#270)
                self._set_answered(self._active_index[key], answered)
                return False
            holder = self._tried_occupant.get(slot)
        if holder is not None and test_before_evict:
            with self._addresses_lock:
                if len(self._collisions) < _SET_TRIED_COLLISION_SIZE:
                    self._collisions.add(endpoint)
            return False
        self._make_tried(endpoint, answered, slot)
        return True

    def _make_tried(
        self,
        endpoint: tuple[int, bytes, int],
        answered: NetworkAddressV2,
        slot: tuple[int, int],
    ) -> None:
        """Move an endpoint to its slot of the tried table: `MakeTried`.

        The caller holds `_move_lock`. Whatever held the slot goes back to
        the new table, to the slot its own group and first source map it
        to, over whatever holds that.
        """
        key = endpoint_key(answered)
        with self._addresses_lock, self._write_batch() as wb:
            for held in self._slots.pop(endpoint, {}):
                del self._occupant[held]
            self._tried_endpoints.add(endpoint)
            self._collisions.discard(endpoint)
            self._put_sources(endpoint, wb)
            self._put_stats(endpoint, wb)
        with self._active_lock:
            evicted_key = self._tried_occupant.get(slot)
            evicted = None if evicted_key is None else self._tried_remove(evicted_key)
            self._tried_insert(key, answered, slot)
            if self.db is not None:
                if evicted_key is not None:
                    self.db.delete(_ANSWERED + evicted_key)
                self.db.put(_ANSWERED + key, answered.serialize(check_validity=False))
        if evicted is not None:
            self._return_to_new(evicted)

    def _return_to_new(self, row: NetworkAddressV2) -> None:
        """Give an entry evicted from the tried table its new-table slot again.

        It takes the slot its group and first source map it to from
        whatever holds it, as `MakeTried` does for the entry it evicts. The
        caller holds `_move_lock`.
        """
        endpoint = _endpoint(row)
        with self._addresses_lock, self._write_batch() as wb:
            self._tried_endpoints.discard(endpoint)
            group = self._source[endpoint]
            slot = _new_slot(self._bucket_key, self._rows[endpoint], group)
            if slot in self._occupant:
                self._clear_slot(slot, wb)
            self._occupant[slot] = endpoint
            self._slots[endpoint] = {slot: group}
            self._put_sources(endpoint, wb)
            self._put_stats(endpoint, wb)

    def resolve_collisions(self) -> None:
        """Settle the tried-table collisions that can be, `ResolveCollisions_`.

        An entry waiting for the slot of one tried within
        `_ADDRMAN_REPLACEMENT` stops waiting and the old one stays. An old
        entry that was tried and failed in that time, for over a minute,
        or that has had neither a success nor a try in it and whose waiting
        entry answered over `_ADDRMAN_TEST_WINDOW` ago, is replaced.
        `ThreadOpenConnections` calls it before each draw, as
        `P2pManager` does; the feeler's connection to the old entry that
        `select_tried_collision` names is what gives it a success to read.
        Takes `_move_lock`.
        """
        now = time.time()
        with self._move_lock:
            with self._addresses_lock:
                waiting = list(self._collisions)
            for endpoint in waiting:
                if self._resolved(endpoint, now):
                    with self._addresses_lock:
                        self._collisions.discard(endpoint)

    def _resolved(self, endpoint: tuple[int, bytes, int], now: float) -> bool:
        """Whether a waiting entry's collision is settled, doing so if it can.

        The caller holds `_move_lock`.
        """
        with self._addresses_lock:
            known = self._rows.get(endpoint)
            if known is None:
                return True
            waiting = self._stats[endpoint].last_success
            slot = _tried_slot(self._bucket_key, known)
        with self._active_lock:
            old_key = self._tried_occupant.get(slot)
            old = (
                None
                if old_key is None
                else self.active_addresses[self._active_index[old_key]]
            )
        if old is not None and old_key is not None:
            old_success = self._stats[_endpoint(old)].last_success
            old_try = self._last_try.get(old_key, 0.0)
            if now - old_success < _ADDRMAN_REPLACEMENT:
                return True
            if now - old_try < _ADDRMAN_REPLACEMENT:
                if now - old_try <= _ADDRMAN_RECENT_TRY_GRACE:
                    return False
            elif now - waiting <= _ADDRMAN_TEST_WINDOW:
                return False
        self._good(known, now, test_before_evict=False)
        return True

    def select_tried_collision(self) -> NetworkAddressV2 | None:
        """Return a tried address another is waiting to replace, or `None`.

        Core's `SelectTriedCollision`, whose caller makes a feeler
        connection to the address it names: that connection's handshake is
        what `Good_` reads as the old entry answering.
        """
        with self._addresses_lock:
            if not self._collisions:
                return None
            endpoint = secrets.choice(list(self._collisions))
            known = self._rows.get(endpoint)
            if known is None:
                self._collisions.discard(endpoint)
                return None
            slot = _tried_slot(self._bucket_key, known)
        with self._active_lock:
            key = self._tried_occupant.get(slot)
            return (
                None if key is None else self.active_addresses[self._active_index[key]]
            )

    def connected(self, address: NetworkAddressV2) -> None:
        """Move the time of `address`'s endpoint forward to now, if it is stale.

        Core's `AddrManImpl::Connected_` (`src/addrman.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which
        `FinalizeNode` calls for a full outbound peer alone: `nTime` moves
        only where it is older than `_CONNECTED_UPDATE_INTERVAL`, and an
        endpoint the table does not hold is left out. Only the known row
        is written: the answered row's timestamp is `m_last_success`.
        Takes `_addresses_lock`.
        """
        now = int(time.time())
        key = endpoint_key(address)
        with self._addresses_lock, self._write_batch() as wb:
            existing = self._held(address)
            if existing is None or now - existing.timestamp <= (
                _CONNECTED_UPDATE_INTERVAL
            ):
                return
            known = replace(existing, timestamp=now)
            self._replace_row(_endpoint(address), existing, known)
            if wb is not None:
                wb.put(_KNOWN + key, known.serialize(check_validity=False))
