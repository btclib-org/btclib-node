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
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from io import BytesIO
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import TYPE_CHECKING, cast

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
from btclib_node.p2p.eviction import get_network, is_routable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from pathlib import Path

    from btclib_node.chains import Chain
    from btclib_node.p2p.eviction import Network

__all__ = [
    "RECENT_TRY_SECONDS",
    "SEEDS_SERVICE_FLAGS",
    "AddrResponseCache",
    "PeerDB",
    "can_connect",
    "dial",
    "endpoint_key",
    "fixed_seed_addresses",
    "host_key",
    "ip_and_port",
    "peer_address",
]

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


# Two record kinds share the one store `PeerDB` opens, so `init_from_db`
# below walks it whole and dispatches on the prefix rather than stopping
# at the first key without one -- `src/btclib_node/db.py`'s own docstring
# names that shape as `BlockDB`'s, next to the other one, `BlockIndex`'s,
# that a store of one record kind can use instead.
_KNOWN = b"known-"
_ANSWERED = b"answered-"

# The bound both tables are kept under: an address a peer gossiped, and
# an address this node has itself confirmed reachable. The cap is on
# distinct endpoints, not on handshakes: `add_active_address` settles a
# repeat handshake with the same endpoint onto the one row already held
# for it (#270), the way `add_addresses`'s own `by_endpoint` already
# does for `self.addresses`.
_MAX_ADDRESSES = 10000

# `ThreadOpenConnections`' own window (`src/net.cpp`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a draw tried less than
# this long ago is passed over. The longest any reader of
# `PeerDB.last_try` looks back, so it is also how long a try is kept.
RECENT_TRY_SECONDS = 10 * 60

# Core's `ADDRMAN_HORIZON` (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7,
# the v31.1 tag): an address not seen for this long is terrible
_ADDRMAN_HORIZON = 30 * 24 * 3600
# how far ahead of the clock a timestamp may be before it is terrible,
# `IsTerrible`'s "flying DeLorean" (same file and sha)
_ADDRMAN_FUTURE_SLACK = 10 * 60
# `IsTerrible`'s own grace, ahead of both tests above (same file and
# sha): "never remove things tried in the last minute". `PeerDB._last_try`
# is `AddrInfo::m_last_try`; `RECENT_TRY_SECONDS` above is a different
# Core window, `ThreadOpenConnections`'s, not this one.
_ADDRMAN_RECENT_TRY_GRACE = 60
# `Connected_`'s own granularity (same file and sha): a time is moved
# forward only where it is older than this
_CONNECTED_UPDATE_INTERVAL = 20 * 60


def _aged_out(address: NetworkAddressV2, now: float, last_try: float) -> bool:
    """Whether `address`'s timestamp fails `IsTerrible`'s time tests.

    `AddrInfo::IsTerrible` (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag) checks `last_try` first: tried within
    `_ADDRMAN_RECENT_TRY_GRACE`, an address is never terrible, whatever
    its timestamp says. Past that grace, its two time tests: stamped
    more than ten minutes ahead of `now`, or older than
    `_ADDRMAN_HORIZON`. The timestamp is Core's `nTime`, which gossip
    and `PeerDB.connected` move and a handshake does not.
    """
    if now - last_try <= _ADDRMAN_RECENT_TRY_GRACE:
        return False
    age = now - address.timestamp
    return age < -_ADDRMAN_FUTURE_SLACK or age > _ADDRMAN_HORIZON


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


def _select(
    answered: list[NetworkAddressV2], known: list[NetworkAddressV2]
) -> NetworkAddressV2 | None:
    """Draw from one table, a fair coin deciding where both hold something.

    Core's `AddrManImpl::Select_` (`src/addrman.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) searches the tried table
    or the new table with `randbool()` when both are non-empty, and
    whichever one is not empty otherwise. `answered` stands for tried,
    and `known` for new.
    """
    if not answered and not known:
        return None
    if not known:
        table = answered
    elif not answered:
        table = known
    else:
        table = answered if secrets.randbelow(2) else known
    return secrets.choice(table)


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

    `addresses` is every address heard about; `active_addresses` is the
    subset this node has itself dialled and heard back from recently.
    Each is behind its own lock, taken separately and never nested --
    the comment beside each lock's own field says which thread reaches
    it and why sharing the other lock was declined.
    """

    def __init__(self, chain: Chain, data_dir: Path | None) -> None:
        """Load the durable tables, then decide whether DNS is still needed."""
        self.chain = chain
        self.data_dir = data_dir
        self.addresses: set[NetworkAddressV2] = set()
        # The `endpoint_key` of every member of `addresses`, kept in step
        # by `add_addresses` under `_addresses_lock` and by `init_from_db`
        # before this object is shared, so `add_active_address` asks
        # whether an endpoint is known without walking the set.
        self._known_keys: set[bytes] = set()
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
        # handshake against (#270). Rebuilt rather than kept in step
        # wherever something else reshapes the list instead --
        # `init_from_db`'s bulk load, and `get_active_addresses` where
        # its prune removed a row, both already O(n) over it.
        self._active_index: dict[bytes, int] = {}
        # `add_active_address` reads this index and then writes into
        # `active_addresses` at the position it found -- two statements, not one
        # -- and `get_active_addresses`, where its prune removed a row,
        # reassigns the list and then rebuilds the index against it -- likewise
        # two. The first runs on `Node`'s own thread, off `callbacks.version`;
        # the second runs on `P2pManager`'s, off `manage_connections`, which
        # calls it every few minutes regardless of what else that loop is doing
        # (#71). Interleaved without a lock, a position read before a prune can
        # be written after it, into a list the prune already reshaped: one
        # endpoint's row silently holding another endpoint's data, or an
        # `IndexError`. `KeyValueStore` has its own lock for the store; this one
        # is for these two in-memory structures alone, and is not the same lock.
        self._active_lock = threading.Lock()
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
        # there too, and from `get_active_addresses`'s own call into
        # `_aged_out`'s grace check, and `get_addr`'s, reachable from
        # `Node`'s thread through `callbacks.getaddr`
        # (btclib-org/btclib-node#1435).
        # Unlocked on purpose: `attempt`'s reassignment of this dict is
        # one step, not a read-modify-write of the attribute itself, so
        # a concurrent `last_try` sees the whole old dict or the whole
        # new one, never a partial one -- at worst one `attempt` stale,
        # the same imprecision `is_empty` already accepts.
        self._last_try: dict[bytes, float] = {}

        # `None` is a table kept in memory only, which is what every
        # test here wants and what `data_dir` was before this: assigned
        # and never opened. `Node` always passes an actual directory
        # (`Config.data_dir` has no `None` of its own), so this is the
        # one place that distinction is made.
        self.db = KeyValueStore(data_dir / "peers") if data_dir is not None else None

        self.init_from_db()
        # `get_active_addresses` also deletes an `_aged_out` row from the
        # durable store (#253); called here so a restart's store is
        # already pruned by the time construction returns, rather than
        # only once whatever reaches it first runs.
        self.get_active_addresses()

    def init_from_db(self) -> None:
        """Load every stored address into `addresses` or `active_addresses`.

        One store keyed by two prefixes (the comment on `_KNOWN` and
        `_ANSWERED` above argues why), so this walks it whole and
        dispatches on the prefix rather than stopping at the first key
        without one. A row `_storable` refuses is deleted rather than
        loaded, as Core's addrman holds no such address, and so is an
        answered row whose endpoint no known row holds, as Core's tried
        table holds nothing addrman does not.
        """
        if self.db is None:
            return
        refused: list[bytes] = []
        answered: list[tuple[bytes, NetworkAddressV2]] = []
        for key, value in self.db:
            if key.startswith(_KNOWN):
                known = True
            elif key.startswith(_ANSWERED):
                known = False
            else:
                continue
            address = NetworkAddressV2.parse(value, check_validity=False)
            if not _storable(address):
                refused.append(key)
            elif known:
                self.addresses.add(address)
                self._known_keys.add(endpoint_key(address))
            else:
                answered.append((key, address))
        # after the walk: the store is sorted, and `answered-` rows come
        # ahead of the `known-` rows they are checked against
        for key, address in answered:
            if endpoint_key(address) in self._known_keys:
                self.active_addresses.append(address)
            else:
                refused.append(key)
        with self.db.write_batch() as wb:
            for key in refused:
                wb.delete(key)
        self._reindex_active()

    def _reindex_active(self) -> None:
        """Rebuild the endpoint index over the current `active_addresses`.

        O(n), the same order `get_active_addresses`'s own prune already
        walks the list at -- called from there and from `init_from_db`,
        the two places that reshape the list itself rather than through
        `add_active_address`.
        """
        self._active_index = {
            endpoint_key(address): position
            for position, address in enumerate(self.active_addresses)
        }

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
        answered endpoint is left in `addresses` too (`address_sampler`'s
        own comment above says so, and is what lets a repeat gossip for
        an already-answered endpoint still update its known row). A bare
        `len(self.addresses) + len(self.active_addresses)` therefore
        counted every answered endpoint twice.
        The honest count is `addresses`' own size plus whatever
        `active_addresses` holds that `addresses` does not -- read as
        `_known_keys`, kept in step with `addresses` by `add_addresses`
        (never shrunk: nothing in this class discards a known key), and
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
        answered table and the gossiped one, so `P2pManager` can draw
        many times a pass for the price of one walk of each table. An
        answered endpoint is left out of the gossiped side, where
        `addresses` holds it too, as Core's `Good_` moves an entry from
        the new table to the tried one and `Select_` flips between two
        tables that never hold one endpoint twice. With `new_only` the
        draw is from the gossiped side alone, Core's `Select(true, ...)`
        that a feeler makes, and with `network` from both sides kept to
        the one network, as `CNetAddr::GetNetwork` names it: Core's
        `Select(false, {network})`, which an extra network peer makes.

        The answered side reads `active_addresses` as it stands, not
        `get_active_addresses`'s pruned view: Core's `Select_`
        (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
        never calls `IsTerrible` -- only `GetAddr_`'s serving and
        `AddSingle`'s duplicate-overwrite check do -- so a terrible entry
        stays eligible for a draw here as it does there
        (btclib-org/btclib-node#1434). A row this draws can still be
        pruned from the table, and from the store, by
        `get_active_addresses`'s own periodic sweep before the next
        call; that remaining divergence from Core's tried table, which
        keeps the entry until another address takes its slot, is argued
        at `get_active_addresses`'s own docstring (#253).
        """
        with self._active_lock:
            raw_active = list(self.active_addresses)
        answered = [
            addr
            for addr in raw_active
            if can_connect(addr) and (network is None or get_network(addr) == network)
        ]
        tried = {_endpoint(addr) for addr in answered}
        # Drawn from the addresses that can be dialled, rather than from
        # the whole table with a retry on the ones that cannot: a table
        # holding none of them -- a seed answering with AAAA records
        # alone is enough, and `add_addresses` takes the tor, i2p and
        # routable cjdns a peer sends -- made that retry a loop with no exit,
        # in the caller's event loop. Nothing to dial is an answer, and
        # `None` is it.
        # Locked, unlike `is_empty` above: this walks the set rather
        # than asking its length, and add_addresses (#298) reaches it
        # from Node's own thread while this runs on P2pManager's --
        # unprotected, that is CPython's `RuntimeError: Set changed
        # size during iteration`, not merely a stale answer.
        with self._addresses_lock:
            known = [
                address
                for address in self.addresses
                if can_connect(address)
                and _endpoint(address) not in tried
                and (network is None or get_network(address) == network)
            ]
        return partial(_select, [] if new_only else answered, known)

    def add_addresses(
        self,
        addresses: Iterable[NetworkAddressV2],
        *,
        source: NetworkAddressV2 | None = None,
        time_penalty: float = _GOSSIP_TIME_PENALTY,
    ) -> None:
        """Merge `addresses` into `self.addresses`, checked and deduplicated.

        An address `_storable` refuses is dropped. Every other address
        settles onto its own `endpoint_key` row, its services ORed into
        those the row held, up to `_MAX_ADDRESSES` distinct endpoints,
        past which a genuinely new one is dropped too. The services and
        the timestamp go to the endpoint's answered row as well, where
        it has one.
        Takes `_addresses_lock`, then `_active_lock`, the two never
        nested.

        The row's own timestamp is kept, `time_penalty` seconds less,
        floored at zero -- Core's `AddrManImpl::AddSingle` (`src/addrman.cpp`,
        at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `pinfo->nTime =
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
        stamps: dict[bytes, int] = {}
        source_host = _host(source) if source is not None else None
        with self._addresses_lock, self._write_batch() as wb:
            # `endpoint_key` is what the durable row is already keyed on --
            # network id, address and port, not `services` -- so a
            # second gossip for the one endpoint overwrites the row on
            # disk. This index is what makes `self.addresses` settle on
            # the endpoint the same way instead of holding one member
            # per `services` value ever seen for it (#247).
            by_endpoint = {endpoint_key(known): known for known in self.addresses}
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
                existing = by_endpoint.get(key)
                # a gossip adds services to an endpoint already held and
                # never takes one away, as Core's `AddSingle` ORs them in
                # (`src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the
                # v31.1 tag)
                services = address.services
                if existing is not None:
                    services |= existing.services
                penalty = 0.0 if _host(address) == source_host else time_penalty
                timestamp = max(0, int(address.timestamp - penalty))
                known = replace(address, timestamp=timestamp, services=services)
                # the cap is on distinct endpoints, so updating one
                # already held does not spend it -- only a genuinely new
                # endpoint can run the table out of room
                if existing is None and len(self.addresses) >= _MAX_ADDRESSES:
                    break
                if existing is not None:
                    self.addresses.discard(existing)
                self.addresses.add(known)
                self._known_keys.add(key)
                by_endpoint[key] = known
                stamps[key] = timestamp
                gossiped[key] = (
                    gossiped.get(key, ServiceFlags.NODE_NONE) | address.services
                )
                if wb is not None:
                    value = known.serialize(check_validity=False)
                    wb.put(_KNOWN + key, value)
        # Core keeps one entry per endpoint, and `AddSingle` ORs gossip
        # into a tried one as into a new one, its time included
        with self._active_lock:
            for key, services in gossiped.items():
                position = self._active_index.get(key)
                if position is not None:
                    row = self.active_addresses[position]
                    self._set_answered(
                        position,
                        replace(
                            row, services=row.services | services, timestamp=stamps[key]
                        ),
                    )

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
            self.addresses.discard(existing)
            self.addresses.add(known)
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
        endpoint = _endpoint(address)
        return next((a for a in self.addresses if _endpoint(a) == endpoint), None)

    def _set_answered(self, position: int, row: NetworkAddressV2) -> None:
        """Write `row` at `position` of `active_addresses`, and to the store.

        The caller holds `_active_lock`.
        """
        self.active_addresses[position] = row
        if self.db is not None:
            self.db.put(
                _ANSWERED + endpoint_key(row), row.serialize(check_validity=False)
            )

    def attempt(self, address: NetworkAddressV2) -> None:
        """Record a try to connect to `address`, as Core's `Attempt_` does.

        `AddrManImpl::Attempt_` (`src/addrman.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag) sets `m_last_try` on
        the entry addrman holds for the address, and does nothing where
        it holds none; so is an endpoint `_known_keys` does not hold left
        out here, an answered one being known too. A try older than
        `RECENT_TRY_SECONDS` is dropped, since nothing reads one.
        """
        key = endpoint_key(address)
        with self._addresses_lock:
            if key not in self._known_keys:
                return
        now = time.time()
        self._last_try = {
            tried: when
            for tried, when in self._last_try.items()
            if now - when < RECENT_TRY_SECONDS
        }
        self._last_try[key] = now

    def last_try(self, address: NetworkAddressV2) -> float:
        """Return when `address` was last tried, `0.0` for never or long ago."""
        return self._last_try.get(endpoint_key(address), 0.0)

    def get_addr(self, max_addresses: int, max_pct: int) -> list[NetworkAddressV2]:
        """Return a random sample of the known addresses, the terrible left out.

        Core's `AddrManImpl::GetAddr_` (`src/addrman.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the answer is sized
        from every entry, new and tried -- here `addresses`, which holds
        every answered endpoint too -- as `max_pct` percent of them, at
        most `max_addresses`. Entries are then drawn at random, skipping
        one `_aged_out` calls terrible, until the size is reached or the
        table is spent, so a table of terrible entries answers less than
        its size. The percentage rounds up where Core's rounds down: a
        table of a handful of addresses, every functional test's own
        two-node regtest, would otherwise answer none
        (btclib-org/btclib-node#71).
        """
        now = time.time()
        with self._addresses_lock:
            rows = list(self.addresses)
        size = min(max_addresses, -(-len(rows) * max_pct // 100))
        secrets.SystemRandom().shuffle(rows)
        sample: list[NetworkAddressV2] = []
        for row in rows:
            if len(sample) >= size:
                break
            if not _aged_out(row, now, self.last_try(row)):
                sample.append(row)
        return sample

    def get_active_addresses(self) -> list[NetworkAddressV2]:
        """Return `active_addresses`, pruned of every entry `_aged_out` names.

        A pruned entry's durable `answered-` row is deleted too. Locked
        with `_active_lock`.
        """
        now = time.time()
        with self._active_lock:
            # A row's timestamp is its last handshake, and it is kept
            # until `IsTerrible`'s time tests call that stamp terrible
            # (`_aged_out`, which says why the stamp is not Core's
            # `nTime`). Core keeps even a terrible entry in its tried
            # table, leaving it out of a `getaddr` answer and moving it
            # back to the new table only when another entry needs its
            # slot. Here it leaves the table and its `answered-` row
            # with it, so that the durable store stays bounded by what
            # answered within the horizon (#253).
            active: list[NetworkAddressV2] = []
            for addr in self.active_addresses:
                if not _aged_out(addr, now, self.last_try(addr)):
                    active.append(addr)
                elif self.db is not None:
                    self.db.delete(_ANSWERED + endpoint_key(addr))
            # rebuilt only where the prune removed something: a row
            # kept keeps its position, and rebuilding costs an
            # `endpoint_key` per row every call (btclib-org/btclib-node#1217)
            if len(active) != len(self.active_addresses):
                self.active_addresses = active
                self._reindex_active()
            return self.active_addresses

    def add_active_address(self, addr: NetworkAddressV2) -> bool:
        """Record `addr` as answered, and say whether it was.

        Core's `AddrManImpl::Good_` (`src/addrman.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which moves an entry
        to the tried table and leaves `nTime` alone, "to avoid leaking
        information about currently-connected peers": the row keeps the
        timestamp the known row has, and `connected` is what moves it.
        A repeat handshake with an already-held endpoint settles onto its
        one row rather than growing the table. An endpoint `addresses`
        does not hold is not recorded, as `Good_` updates only an entry
        addrman already has, and neither is an address `_storable`
        refuses, which `addresses` never holds. Takes `_addresses_lock`
        to ask, then `_active_lock` to write, the two never nested.
        """
        key = endpoint_key(addr)
        with self._addresses_lock:
            existing = self._held(addr)
        if existing is None:
            return False
        answered = replace(addr, timestamp=existing.timestamp)
        with self._active_lock:
            position = self._active_index.get(key)
            if position is not None:
                # a repeat handshake with an endpoint already held:
                # settle onto its one row rather than growing the table
                # an entry per reconnect (#270), matching
                # `add_addresses`'s own `by_endpoint`.
                self.active_addresses[position] = answered
            else:
                # the cap is on distinct endpoints, so updating one
                # already held (above) does not spend it -- only a
                # genuinely new one can run the table out of room,
                # `add_addresses`'s own cap check reads the same way.
                if len(self.active_addresses) >= _MAX_ADDRESSES:
                    return False
                self._active_index[key] = len(self.active_addresses)
                self.active_addresses.append(answered)
            if self.db is not None:
                self.db.put(_ANSWERED + key, answered.serialize(check_validity=False))
        return True

    def connected(self, address: NetworkAddressV2) -> None:
        """Move the time of `address`'s endpoint forward to now, if it is stale.

        Core's `AddrManImpl::Connected_` (`src/addrman.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which
        `FinalizeNode` calls for a full outbound peer alone: `nTime` moves
        only where it is older than `_CONNECTED_UPDATE_INTERVAL`, and an
        endpoint the table does not hold is left out. The known and the
        answered row are both written, standing for Core's one entry.
        Takes `_addresses_lock`, then `_active_lock`, the two never
        nested.
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
            self.addresses.discard(existing)
            self.addresses.add(known)
            if wb is not None:
                wb.put(_KNOWN + key, known.serialize(check_validity=False))
        with self._active_lock:
            position = self._active_index.get(key)
            if position is not None:
                row = self.active_addresses[position]
                self._set_answered(position, replace(row, timestamp=now))
