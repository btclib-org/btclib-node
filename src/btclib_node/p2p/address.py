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
from dataclasses import replace
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import TYPE_CHECKING, cast

from btclib.p2p.address import ServiceFlags
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2, is_embedded_ipv6

from btclib_node.db import KeyValueStore
from btclib_node.exceptions import UnsupportedAddressTypeError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from btclib_node.chains import Chain

__all__ = [
    "PeerDB",
    "can_connect",
    "dial",
    "endpoint_key",
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


# Core's own default (`DEFAULT_CONNECT_TIMEOUT`, src/netbase.h at
# bitcoin/bitcoin@ca7162cde5), not the old poll loop's ten-passes-at-0.1s
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
    unreachable family fails the same `sock_connect` a slow or refusing
    peer does, landing on the same `None` `P2pManager` already treats as
    "try someone else". Bitcoin Core's own default (`ReachableNets`,
    src/netbase.h at 58a7869f86: "Everything is reachable by default")
    is the same bet -- reachability is what a dial's outcome says it is,
    not a property guessed at beforehand -- so there is nothing here for
    a heavier check to buy.
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
    client = socket.socket(family, socket.SOCK_STREAM)
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
        # A lock of its own, not `_active_lock` below: `add_addresses`
        # reaches this set from both threads too (#298) -- gossip
        # through `callbacks.addr`/`addrv2` on `Node`'s, DNS seed
        # answers through `get_addr_from_dns` on `P2pManager`'s, and
        # `random_address`'s own dialable-address comprehension on
        # `P2pManager`'s as well, racing against gossip on `Node`'s.
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
        # `init_from_db`'s bulk load and `get_active_addresses`'s prune,
        # both already O(n) over it.
        self._active_index: dict[bytes, int] = {}
        # `add_active_address` reads this index and then writes into
        # `active_addresses` at the position it found -- two statements,
        # not one -- and `get_active_addresses` reassigns the list and
        # then rebuilds the index against it -- likewise two. The first
        # runs on `Node`'s own thread, off `callbacks.verack`; the
        # second runs on `P2pManager`'s, off `manage_connections`, which
        # calls it every few minutes regardless of what else that loop
        # is doing (#71). Interleaved without a lock, a position read
        # before a prune can be written after it, into a list the prune
        # already reshaped: one endpoint's row silently holding another
        # endpoint's data, or an `IndexError`. `KeyValueStore` has its
        # own lock for the store; this one is for these two in-memory
        # structures alone, and is not the same lock.
        self._active_lock = threading.Lock()
        # What `callbacks.getaddr` last answered with, and until when it
        # is still good for: a fresh `secrets.SystemRandom().sample` per
        # connection would let two peers connecting close together
        # compare answers and infer what changed between them, which
        # "once per connection" alone does not stop -- a new connection
        # still draws fresh. `0.0` starts already expired, so the first
        # call computes a sample rather than serving an empty one.
        # btclib-org/btclib-node#71
        self.addr_sample: list[NetworkAddressV2] = []
        self.addr_sample_expiration = 0.0

        # `None` is a table kept in memory only, which is what every
        # test here wants and what `data_dir` was before this: assigned
        # and never opened. `Node` always passes an actual directory
        # (`Config.data_dir` has no `None` of its own), so this is the
        # one place that distinction is made.
        self.db = KeyValueStore(data_dir / "peers") if data_dir is not None else None

        self.init_from_db()
        # DNS is asked only where the durable table came back with
        # nothing this node has itself confirmed working recently:
        # `get_active_addresses` is what "recently" already means, and
        # `can_connect` is what catches a table `add_addresses` filled
        # with tor, i2p or an ipv6-only answer from a seed -- #89, where
        # a nonempty table was exactly the case DNS was skipped for and
        # none of it was dialable.
        self.ask_dns_nodes = not any(
            can_connect(address) for address in self.get_active_addresses()
        )

    def init_from_db(self) -> None:
        """Load every stored address into `addresses` or `active_addresses`.

        One store keyed by two prefixes (the comment on `_KNOWN` and
        `_ANSWERED` above argues why), so this walks it whole and
        dispatches on the prefix rather than stopping at the first key
        without one.
        """
        if self.db is None:
            return
        for key, value in self.db:
            if key.startswith(_KNOWN):
                self.addresses.add(NetworkAddressV2.parse(value, check_validity=False))
            elif key.startswith(_ANSWERED):
                self.active_addresses.append(
                    NetworkAddressV2.parse(value, check_validity=False)
                )
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

    async def get_addr_from_dns(self) -> None:
        """Resolve every chain DNS seed and feed the answers to `add_addresses`.

        A no-op unless `ask_dns_nodes` said, at construction time, that
        the durable table came back with nothing dialable.
        """
        if not self.ask_dns_nodes:
            return
        chain = self.chain
        loop = asyncio.get_running_loop()
        # what a seed answers with, deduplicated: seeds overlap, and one
        # of them answers with the same host over several records.
        endpoints: set[tuple[str, int]] = set()
        for dns_server in chain.addresses:
            try:
                answers = await loop.getaddrinfo(dns_server, chain.port)
            except socket.gaierror:
                continue
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
        # through add_addresses, and not a bare add to the set: a seed
        # is gossip like a peer's is, and belongs in the durable table
        # the same way, so a later restart has it without asking again
        self.add_addresses(peer_address(ip, port) for ip, port in endpoints)

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

    def random_address(self) -> NetworkAddressV2 | None:
        """Return a random dialable address, or `None` if there is none.

        Preferred from `get_active_addresses`'s own dialable subset;
        falls back to `addresses` whole, locked, only if that is empty.
        """
        # Preferred: an address this node has itself dialled and heard
        # back from recently, over one merely gossiped -- #123, so that
        # a run draws on what it already knows works rather than on the
        # whole table uniformly, even before a restart ever reads any
        # of it back.
        preferred = [addr for addr in self.get_active_addresses() if can_connect(addr)]
        if preferred:
            return secrets.choice(preferred)
        # Drawn from the addresses that can be dialled, rather than from
        # the whole table with a retry on the ones that cannot: a table
        # holding none of them -- a seed answering with AAAA records
        # alone is enough, and `add_addresses` takes whatever tor, i2p
        # and cjdns a peer sends -- made that retry a loop with no exit,
        # in the caller's event loop. Nothing to dial is an answer, and
        # `None` is it.
        # Locked, unlike `is_empty` above: this walks the set rather
        # than asking its length, and add_addresses (#298) reaches it
        # from Node's own thread while this runs on P2pManager's --
        # unprotected, that is CPython's `RuntimeError: Set changed
        # size during iteration`, not merely a stale answer.
        with self._addresses_lock:
            dialable = [address for address in self.addresses if can_connect(address)]
        if not dialable:
            return None
        return secrets.choice(dialable)

    def add_addresses(self, addresses: Iterable[NetworkAddressV2]) -> None:
        """Merge `addresses` into `self.addresses`, checked and deduplicated.

        BIP155's embedded-IPv6 records are dropped; every other address
        settles onto its own `endpoint_key` row, up to `_MAX_ADDRESSES`
        distinct endpoints, past which a genuinely new one is dropped
        too. Locked with `_addresses_lock`.
        """
        # a peer's word for when it last saw an address is not evidence,
        # and keeping it would make the one address several entries
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
                if is_embedded_ipv6(address):
                    continue
                known = replace(address, timestamp=0)
                key = endpoint_key(known)
                existing = by_endpoint.get(key)
                # the cap is on distinct endpoints, so updating one
                # already held does not spend it -- only a genuinely new
                # endpoint can run the table out of room
                if existing is None and len(self.addresses) >= _MAX_ADDRESSES:
                    break
                if existing is not None:
                    self.addresses.discard(existing)
                self.addresses.add(known)
                by_endpoint[key] = known
                if wb is not None:
                    value = known.serialize(check_validity=False)
                    wb.put(_KNOWN + key, value)

    def get_active_addresses(self) -> list[NetworkAddressV2]:
        """Return `active_addresses`, pruned of every entry older than 3 hours.

        A pruned entry's durable `answered-` row is deleted too. Locked
        with `_active_lock`.
        """
        now = time.time()
        with self._active_lock:
            # active if seen within the last three hours; an entry that
            # ages out here loses its `answered-` row too, so the
            # durable store stays bounded by what is still active rather
            # than by every endpoint this node has ever dialled and
            # heard back from over its whole lifetime (#253)
            active: list[NetworkAddressV2] = []
            for addr in self.active_addresses:
                if now - addr.timestamp < 3600 * 3:
                    active.append(addr)
                elif self.db is not None:
                    self.db.delete(_ANSWERED + endpoint_key(addr))
            self.active_addresses = active
            self._reindex_active()
            return self.active_addresses

    def add_active_address(self, addr: NetworkAddressV2) -> None:
        """Record `addr` as dialled and answered, just now.

        A repeat handshake with an already-held endpoint settles onto
        its one row rather than growing the table. Locked with
        `_active_lock`.
        """
        # a whole second: the field is four octets on the wire
        answered = replace(addr, timestamp=int(time.time()))
        key = endpoint_key(answered)
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
                    return
                self._active_index[key] = len(self.active_addresses)
                self.active_addresses.append(answered)
            if self.db is not None:
                self.db.put(_ANSWERED + key, answered.serialize(check_validity=False))
