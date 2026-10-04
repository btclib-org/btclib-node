# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`PeerDB`, and dialling a socket for a peer it names.

Two surfaces sharing a module: the table of known and active addresses
and its own two locks, and `dial`'s handling of a peer that refuses,
hangs or is cancelled on. The round trip between BIP155's
`NetworkAddressV2` and the narrower `addr` entry an addrv1 peer is sent
is btclib's own and is tested there (btclib-org/btclib#1581).
"""

import asyncio
import hashlib
import secrets
import socket
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.p2p.address import NetworkAddress, ServiceFlags
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2, is_embedded_ipv6

import btclib_node.p2p.address as address_module
from btclib_node.db import KeyValueStore
from btclib_node.p2p.address import (
    SEEDS_SERVICE_FLAGS,
    PeerDB,
    can_connect,
    dial,
    fixed_seed_addresses,
    ip_and_port,
    peer_address,
)
from btclib_node.p2p.eviction import Network, net_class, net_group
from tests import call_within

if TYPE_CHECKING:
    from pathlib import Path

    from btclib_node.chains import Chain

# BIP155's own examples: an IPv6-mapped IPv4 host, and an address under
# OnionCat's `fd87:d87e:eb43::/48`, once how a TORv2 address was carried
# inside a fake IPv6 one.
_A_V4_MAPPED_ADDRESS = "::ffff:1.2.3.4"
_AN_ONIONCAT_ADDRESS = "fd87:d87e:eb43::1"


# `tests/conftest.py` replaces `_roll` for every test; this is the function
_REAL_ROLL = address_module._roll


def a_peer_db(chain: Any = None, data_dir: Path | None = None) -> PeerDB:
    """Build a `PeerDB`, in memory unless `data_dir` names a store on disk."""
    return PeerDB(cast("Chain", chain), data_dir)


def table_keys(peer_db: PeerDB) -> set[bytes]:
    """Return the keys of the known and answered rows a store holds."""
    assert peer_db.db is not None
    return {key for key, _ in peer_db.db if key.startswith((b"known-", b"answered-"))}


def slots_of(peer_db: PeerDB) -> dict[tuple[int, bytes, int], frozenset[Any]]:
    """Return the new-table slots each endpoint holds."""
    return {endpoint: frozenset(slots) for endpoint, slots in peer_db._slots.items()}


def buckets_used(peer_db: PeerDB) -> set[int]:
    """Return the new-table buckets that hold something."""
    return {slot[0] for slots in peer_db._slots.values() for slot in slots}


def packed(group: bytes) -> bytes:
    """Return a net group as the store keeps it."""
    return bytes([len(group)]) + group


def plant_answered(
    peer_db: PeerDB, address: NetworkAddressV2, *, at: float | None = None
) -> None:
    """Hold `address` as answered at `at`, in the tried table, untried since."""
    peer_db.add_addresses([address], time_penalty=0)
    peer_db._good(address, time.time() if at is None else at, test_before_evict=False)
    peer_db._last_try.pop(address_module.endpoint_key(address), None)


def side_rows(side: Any) -> list[NetworkAddressV2]:
    """Return the addresses a `_Side` holds."""
    return [side.row_of(item) for item in side.occupant.values()]


def an_onion_address(port: int = 8333) -> NetworkAddressV2:
    """Build a TORv3 peer: BIP155's undialable network, for its own tests."""
    return NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, port)


def a_cjdns_address(port: int = 8333) -> NetworkAddressV2:
    """Build a CJDNS peer: sixteen octets, so it is not refused by length alone.

    An onion address is refused wherever an IP address is wanted
    because its length is wrong; this one is the same length as an
    IPv6 address, so it is what tells apart a check that reads the
    network id from one that only checks how many octets there are.
    """
    return NetworkAddressV2(0, 0, BIP155Network.CJDNS, b"\xfc" + b"\x11" * 15, port)


def test_an_address_just_seen_is_active_and_can_be_sent() -> None:
    """An address just handshaked with is active and round-trips as BIP155.

    The stored timestamp is an `int`, not the `float` `time.time()`
    gives: the field is four octets on the wire and a `float` has no
    `to_bytes`, so this is what serving the address over `addr`/`addrv2`
    actually needs.
    """
    peer_db = a_peer_db()
    seen = peer_address("1.2.3.4", 18444, timestamp=int(time.time()))
    peer_db.add_addresses([seen], time_penalty=0)
    peer_db.add_active_address(seen)
    (active,) = peer_db.active_addresses
    # a whole second, because the field is four octets on the wire and a
    # float has no to_bytes: this is what serving the address needs
    assert isinstance(active.timestamp, int)
    assert NetworkAddressV2.parse(active.serialize()) == active


def test_the_table_of_active_addresses_is_bounded() -> None:
    """One group's answered addresses take the buckets they map to, no more.

    Core's `ADDRMAN_TRIED_BUCKETS_PER_GROUP` buckets of 64 positions: the
    ports of one address, offered by many sources and each answered,
    leave 512 positions at most, whatever the test before an eviction
    would have kept waiting.
    """
    peer_db = a_peer_db()
    now = int(time.time())
    for source in range(60):
        peer_db.add_addresses(
            [peer_address("1.2.3.4", port, timestamp=now) for port in range(1000)],
            source=peer_address(f"{source + 20}.1.1.1", 8333),
            time_penalty=0,
        )
    assert len(peer_db.addresses) > 512
    for address in list(peer_db.addresses):
        peer_db.add_active_address(address, test_before_evict=False)
    assert 64 < len(peer_db.active_addresses) <= 512
    assert len({slot[0] for slot in peer_db._tried_occupant}) <= 8


def test_redialling_the_same_endpoint_settles_onto_its_one_row() -> None:
    """Handshaking with the same endpoint three times leaves one active row.

    #270: `add_active_address` used to run once per handshake with no
    check for an endpoint already held, so a peer redialled inside the
    active window grew one row per handshake instead of
    settling on the latest, the way `add_addresses`'s own `by_endpoint`
    already did for the known-address table.
    """
    # #270: add_active_address ran once per handshake, with no check for
    # an endpoint already held, so a peer redialled inside the active
    # window grew one row per handshake instead of settling on the
    # latest the way add_addresses's own by_endpoint already does
    peer_db = a_peer_db()
    peer_db.add_addresses([peer_address("1.2.3.4", 18444)])
    for port in (18444, 18444, 18444):
        peer_db.add_active_address(peer_address("1.2.3.4", port))
    (active,) = peer_db.active_addresses
    assert active.port == 18444


def test_redialling_the_same_endpoint_many_times_still_holds_one_row() -> None:
    """Many redials of one endpoint still leave exactly one active row.

    #270: this many calls against the one endpoint is the same shape
    `test_the_table_of_active_addresses_is_bounded` puts the cap
    through, without a distinct port spending it each time -- and it is
    also the fix's own regression guard against a per-call scan of
    `active_addresses`: `pyproject.toml`'s own per-test `timeout` is
    what such a scan, repeated this many times, would fail on, not the
    assertion below.
    """
    # #270: this many calls against the one endpoint is the same shape
    # `test_the_table_of_active_addresses_is_bounded` puts the cap
    # through, without a distinct port spending it each time. It is also
    # this fix's own regression guard against reintroducing a per-call
    # scan of `active_addresses`: `pyproject.toml`'s own per-test
    # `timeout` is what a scan repeated this many times fails on, not the
    # assertion below.
    peer_db = a_peer_db()
    peer_db.add_addresses([peer_address("1.2.3.4", 18444)])
    for _ in range(10010):
        peer_db.add_active_address(peer_address("1.2.3.4", 18444))
    assert len(peer_db.active_addresses) == 1


def test_add_active_address_waits_out_a_move_already_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`add_active_address` blocks while another is mid-move between tables.

    A handshake moves an entry out of the new table and its evicted
    neighbour back in, in several steps, and `callbacks.version` on
    `Node`'s thread and `P2pManager`'s dial loop each reach them.
    `_return_to_new` is paused here, after the evicted entry has left the
    tried table and before it is put back, which is the gap `_move_lock`
    closes -- a deliberate pause rather than a race against real timing.
    """
    peer_db = a_peer_db()
    now = int(time.time())
    old = peer_address("1.2.3.4", 8333, timestamp=now)
    other = a_tried_collision(peer_db, old)
    third = peer_address("5.6.7.8", 8333, timestamp=now)
    peer_db.add_addresses([old, other, third], time_penalty=0)
    peer_db.add_active_address(old, test_before_evict=False)

    entered = threading.Event()
    release = threading.Event()
    real_return = peer_db._return_to_new

    def paused_return(row: NetworkAddressV2) -> None:
        entered.set()
        assert release.wait(timeout=5)
        real_return(row)

    monkeypatch.setattr(peer_db, "_return_to_new", paused_return)
    evictor = threading.Thread(
        target=peer_db.add_active_address,
        args=(other,),
        kwargs={"test_before_evict": False},
    )
    evictor.start()
    assert entered.wait(timeout=5)

    adder = threading.Thread(target=peer_db.add_active_address, args=(third,))
    adder.start()
    # the lock is what this proves: without it, add_active_address does
    # not wait on anything and this join returns well inside the bound
    adder.join(timeout=0.2)
    assert adder.is_alive()

    release.set()
    evictor.join(timeout=5)
    adder.join(timeout=5)
    assert not adder.is_alive()
    assert {a.port for a in peer_db.active_addresses} == {other.port, third.port}
    assert address_module._endpoint(old) in peer_db._slots


def test_add_addresses_and_random_address_do_not_interleave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`random_address` blocks while a gossiped `add_addresses` is in progress.

    #298: `random_address`'s own dialable-address comprehension walks
    `addresses` on `P2pManager`'s thread while `add_addresses` (gossip,
    from `callbacks.addr`/`addrv2`) mutates the same set on `Node`'s --
    unprotected, CPython raises `RuntimeError: Set changed size during
    iteration` for that pairing rather than merely losing an update.
    `is_embedded_ipv6` is paused here, inside the loop that reads and
    mutates `addresses` before any entry is added, which is a
    deliberate pause rather than a race against real timing.
    """
    # #298: random_address's own dialable-address comprehension walks
    # `addresses` on P2pManager's thread while add_addresses (gossip,
    # from callbacks.addr/addrv2) mutates the same set on Node's --
    # unprotected, CPython raises `RuntimeError: Set changed size during
    # iteration` for that pairing rather than merely losing an update.
    # Paused mid-add_addresses here rather than raced on timing:
    # `is_embedded_ipv6` is where the pause is forced, inside the loop
    # that reads and mutates `addresses`, before any entry is added.
    peer_db = a_peer_db()

    entered_add = threading.Event()
    release_add = threading.Event()
    real_check = is_embedded_ipv6

    def paused_check(address: NetworkAddressV2) -> bool:
        entered_add.set()
        assert release_add.wait(timeout=5)
        return real_check(address)

    monkeypatch.setattr(address_module, "is_embedded_ipv6", paused_check)

    adder = threading.Thread(
        target=peer_db.add_addresses, args=([peer_address("1.2.3.4", 8333)],)
    )
    adder.start()
    assert entered_add.wait(timeout=5)

    reader = threading.Thread(target=peer_db.random_address)
    reader.start()
    # the lock is what this proves: without it, random_address's own
    # comprehension does not wait on anything and this join returns
    # well inside the bound below
    reader.join(timeout=0.2)
    assert reader.is_alive()

    release_add.set()
    adder.join(timeout=5)
    reader.join(timeout=5)
    assert not adder.is_alive()
    assert not reader.is_alive()
    (known,) = peer_db.addresses
    assert known.port == 8333


def test_a_terrible_answered_address_stays_in_the_tried_table() -> None:
    """Core keeps a terrible tried entry until another takes its slot.

    `GetAddr_` leaves it out of an answer, but it holds its tried slot,
    where a test before an eviction can still find it, and `Select_`
    can still draw it.
    """
    peer_db = a_peer_db()
    old = int(time.time()) - 31 * 86400
    stale = peer_address("1.2.3.4", 18444, timestamp=old)
    plant_answered(peer_db, stale, at=old)
    assert peer_db.active_addresses == [stale]
    assert peer_db.get_addr(0, 0) == []
    assert address_module._endpoint(stale) in peer_db._tried_endpoints


@pytest.mark.parametrize(
    ("since_try", "terrible"),
    [
        pytest.param(30, False, id="tried-just-now"),
        pytest.param(61, True, id="tried-a-minute-ago"),
    ],
)
def test_the_recent_try_grace_expires_after_a_minute(
    since_try: int, *, terrible: bool
) -> None:
    """ISS 1435: `IsTerrible`'s `m_last_try` guard comes first."""
    now = time.time()
    address = peer_address("1.2.3.4", 18444, timestamp=int(now) - 31 * 24 * 3600)
    assert address_module._aged_out(address, now, now - since_try) is terrible


def test_the_two_ip_networks_are_told_apart_by_the_text_of_the_address() -> None:
    """`peer_address` reads the network id and the field width off the text.

    An IPv4 literal gets `BIP155Network.IPV4` and four address octets;
    an IPv6 literal gets `IPV6` and sixteen -- BIP155's own split
    between the two networks, from parsing the string alone.
    """
    assert peer_address("1.2.3.4", 8333).network_id == BIP155Network.IPV4
    assert peer_address("2001:db8::1", 8333).network_id == BIP155Network.IPV6
    # four octets and sixteen, which is what BIP155 gives the two ids
    # where an addr version 1 entry maps the v4 one into sixteen
    assert peer_address("1.2.3.4", 8333).address == b"\x01\x02\x03\x04"
    assert len(peer_address("2001:db8::1", 8333).address) == 16


def test_an_address_is_shown_the_way_core_writes_one() -> None:
    """`ip_and_port` formats like Core's `CService::ToStringAddrPort`.

    Bracketed unless the host is IPv4, so that the host of a v6 peer
    can be told from its port; a v4-mapped host is shown as plain IPv4,
    the way Core's own `SetLegacyIPv6` files it under `NET_IPV4`.
    """
    # `CService::ToStringAddrPort`: bracketed unless the host is IPv4,
    # so that the host of a v6 peer can be told from its port
    assert ip_and_port("1.2.3.4", 8333) == "1.2.3.4:8333"
    assert ip_and_port("2001:db8::1", 8333) == "[2001:db8::1]:8333"
    # a mapped host is IPv4 to Core too, `SetLegacyIPv6` filing one
    # under NET_IPV4, and this is the form a `NetworkAddress` hands over
    assert ip_and_port("::ffff:1.2.3.4", 8333) == "1.2.3.4:8333"
    assert str(NetworkAddress(0, "1.2.3.4", 8333).ip) == "::ffff:1.2.3.4"


def test_a_host_that_is_not_an_ip_address_is_refused() -> None:
    """`ip_and_port` refuses a hostname rather than guessing its brackets.

    Nothing in this node reaches `ip_and_port` with one -- a socket
    answers with an address and a `NetworkAddress` holds one -- so the
    refusal is a deliberate boundary and not a case worth handling.
    """
    with pytest.raises(ValueError, match="does not appear to be"):
        ip_and_port("seed.bitcoin.sipa.be", 8333)


def test_the_two_ip_networks_are_dialled_and_an_onion_address_is_not() -> None:
    """`can_connect` accepts both IP networks and refuses an onion address.

    `can_connect` answers a different question from
    `btclib.p2p.addrv2.can_addrv1` -- whether this node has a dial for
    the network, not whether the wire format has room for the address --
    even though both agree on every network this node knows of today.
    """
    assert can_connect(peer_address("1.2.3.4", 8333))
    assert can_connect(peer_address("2001:db8::1", 8333))
    assert not can_connect(an_onion_address())


def test_an_address_that_cannot_be_dialled_says_so_rather_than_trying() -> None:
    """`dial` refuses an onion address up front, without opening a socket."""
    with pytest.raises(ValueError, match="not yet supported"):
        asyncio.run(dial(an_onion_address()))


def test_a_peer_that_is_listening_is_connected_to() -> None:
    """`dial` connects to a real IPv4 listener and hands back that socket."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        address = peer_address("127.0.0.1", port)
        client = asyncio.run(dial(address))
        assert client is not None
        with client:
            assert client.getpeername() == ("127.0.0.1", port)
    finally:
        listener.close()


def test_a_v6_peer_that_is_listening_is_connected_to() -> (
    None
):  # pragma: no cover -- the body needs IPv6
    """`dial` connects to a real IPv6 listener and hands back that socket."""
    try:
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError as refused:
        pytest.skip(f"this host has no IPv6: {refused}")
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("::1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        address = peer_address("::1", port)
        client = asyncio.run(dial(address))
        assert client is not None
        assert client.family == socket.AF_INET6
        with client:
            assert client.getpeername()[:2] == ("::1", port)
    finally:
        listener.close()


def test_a_dial_of_a_family_the_socket_layer_refuses_answers_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1249: a family the socket layer refuses lands `dial` on `None`.

    A host missing the family entirely gets the same `None` a refused
    connect already gets, not the `OSError` `socket.socket` itself
    raises. A real host missing IPv6 support answers this way too, but
    nothing here depends on the host running this test lacking it: the
    failure `socket.socket` itself would raise -- `OSError: [Errno 97]
    Address family not supported by protocol` on Linux -- is reproduced
    directly rather than assumed. Only `AF_INET6` is refused, so the
    event loop's own sockets -- its self-pipe among them -- are
    unaffected.
    """
    real_socket = socket.socket

    def refuses_v6(family: int, *args: Any, **kwargs: Any) -> socket.socket:
        if family == socket.AF_INET6:
            raise OSError(97, "Address family not supported by protocol")
        return real_socket(family, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", refuses_v6)
    address = peer_address("2001:db8::1", 8333)
    assert asyncio.run(dial(address)) is None


def test_a_dial_that_is_given_up_on_closes_the_socket_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `dial` that gives up on an unreachable peer still closes its socket.

    Every `socket.socket()` call is recorded, including the event
    loop's own, so the one this test cares about -- the IPv4 stream
    socket `dial` opened for the connect attempt -- is picked out by
    family and type and checked for a closed file descriptor
    specifically, rather than assuming there is exactly one to check.
    """
    opened: list[socket.socket] = []
    real_socket = socket.socket

    def recording_socket(*args: Any, **kwargs: Any) -> socket.socket:
        sock = real_socket(*args, **kwargs)
        opened.append(sock)
        return sock

    monkeypatch.setattr(socket, "socket", recording_socket)
    listener = real_socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()

    address = peer_address("127.0.0.1", port)
    assert asyncio.run(dial(address)) is None
    # the event loop opens sockets of its own; the dial's is the ipv4
    # stream one, and it is not left behind
    dialled = [
        sock
        for sock in opened
        if sock.family == socket.AF_INET and sock.type == socket.SOCK_STREAM
    ]
    assert dialled
    assert all(sock.fileno() == -1 for sock in dialled)


def test_a_dial_cancelled_in_flight_closes_the_socket_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#312: cancelling `manage_connections` cancels a dial mid-flight too.

    `P2pManager.stop` cancels `manage_connections`, and a dial it is in
    the middle of is cancelled with it. `CancelledError` is not an
    `OSError` and not a `TimeoutError`, so the arm that answers a peer
    which never came up does not see it: the socket opened a few lines
    earlier is reachable from the frame the cancellation unwinds and
    from nowhere else, and the caller that would have closed it is
    never handed it.

    `sock_connect` is replaced with one that never comes back, so the
    dial is certainly still in flight when it is cancelled, rather than
    the test racing a real connect.
    """
    opened: list[socket.socket] = []
    real_socket = socket.socket

    def recording_socket(*args: Any, **kwargs: Any) -> socket.socket:
        sock = real_socket(*args, **kwargs)
        opened.append(sock)
        return sock

    monkeypatch.setattr(socket, "socket", recording_socket)

    async def cancel_a_dial_in_flight() -> None:
        loop = asyncio.get_running_loop()

        async def never_connects(sock: socket.socket, peer: Any) -> None:
            await asyncio.sleep(3600)

        monkeypatch.setattr(loop, "sock_connect", never_connects)
        task = asyncio.ensure_future(dial(peer_address("127.0.0.1", 8333)))
        # the dial's own socket is open and its connect is under way:
        # one pass of the loop is all it takes to get there, and the
        # sleep is what makes the task reach it rather than the cancel
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_a_dial_in_flight())
    dialled = [
        sock
        for sock in opened
        if sock.family == socket.AF_INET and sock.type == socket.SOCK_STREAM
    ]
    assert dialled
    assert all(sock.fileno() == -1 for sock in dialled)


def test_a_peer_that_is_not_listening_is_given_up_on() -> None:
    """`dial` answers `None` for a closed port rather than a dead socket.

    Nothing is bound on the port by the time `dial` is called: the
    connection never completes, and what comes back is nothing rather
    than a socket that exists but is not connected.
    """
    # nothing bound: the connection never completes, and what comes back
    # is nothing rather than a socket that is not connected
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    address = peer_address("127.0.0.1", port)
    assert asyncio.run(dial(address)) is None


def test_a_refused_dial_does_not_cost_the_full_timeout() -> None:
    """A refused connection is noticed well before `_DIAL_TIMEOUT` elapses.

    #90: a poll of ten passes at 0.1s apart cannot tell a refusal from a
    peer that is merely slow to answer, so it always spent the whole
    budget either way. `SO_ERROR`, read through `loop.sock_connect`, is
    answered by the kernel instead -- promptly on POSIX, where a refusal
    is microseconds away, and measurably slower on Windows' own Proactor
    loop (ISS 681), where it is still well short of `_DIAL_TIMEOUT`. The
    bound below is what tells the two apart on either platform: a dial
    that instead ran out `_DIAL_TIMEOUT`'s own clock would answer `None`
    exactly the same way, with nothing else in this test able to see the
    difference.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    address = peer_address("127.0.0.1", port)
    start = time.monotonic()
    assert asyncio.run(dial(address)) is None
    # a full second of margin under `_DIAL_TIMEOUT` (5.0s): generous
    # next to the microseconds a POSIX refusal costs, and next to the
    # ~2s a Windows run measured (btclib-org/btclib-node run
    # 33271519023), while still failing a dial that only gave up on its
    # own deadline
    assert time.monotonic() - start < address_module._DIAL_TIMEOUT - 1.0


def a_seed_host(name: str) -> str:
    """Return the `x9.` subdomain `query_dns_seed` asks of seed `name`.

    `int(SEEDS_SERVICE_FLAGS)` is 9, `NODE_NETWORK | NODE_WITNESS`.
    """
    return f"x{int(SEEDS_SERVICE_FLAGS):x}.{name}"


class FakeLoop:
    """A `getaddrinfo` stand-in answering fixed hosts, no real DNS query."""

    def __init__(self, answers: dict[str, Exception | list[str]]) -> None:
        """Record what each host name should answer with, or raise."""
        self.answers = answers
        self.requested: list[str] = []

    async def getaddrinfo(
        self, host: str, port: int, **kwargs: object
    ) -> list[tuple[None, None, None, None, tuple[str, int]]]:
        """Answer `host` from `self.answers`, in `getaddrinfo`'s own shape."""
        assert kwargs.get("type") == socket.SOCK_STREAM
        self.requested.append(host)
        answer = self.answers[host]
        if isinstance(answer, Exception):
            raise answer
        return [(None, None, None, None, (ip, port)) for ip in answer]


def a_chain(seeds: list[str]) -> Any:
    """Build a chain stand-in naming `seeds` as its DNS seeds.

    Its port is regtest's own 18444, not 8333: the port has to come
    from the chain, and a default would look the same either way.
    """
    return SimpleNamespace(addresses=list(seeds), port=18444)


# What every test below that checks the table's whole content, rather
# than its timestamp specifically, patches `_dns_seed_timestamp` to
# return -- so the table settles on exactly the row `a_seed_answer`
# below builds, rather than one a real, uniform draw would make
# different on every run.
# `test_a_dns_seed_s_answer_is_backdated_with_no_extra_penalty` and
# `test_the_dns_seed_timestamp_is_uniform_between_three_and_seven_days_old`
# are what test the real draw itself.
_A_FIXED_DNS_STAMP = 1_700_000_000


def a_seed_answer(ip: str) -> NetworkAddressV2:
    """Build what a DNS seed's answer of `ip` is recorded as, on regtest's port.

    With Core's `SeedsServiceFlags`, as `ThreadDNSAddressSeed` records
    it, and `_A_FIXED_DNS_STAMP` above for its timestamp.
    """
    return peer_address(
        ip, 18444, timestamp=_A_FIXED_DNS_STAMP, services=SEEDS_SERVICE_FLAGS
    )


def test_a_seed_that_fails_is_returned_and_a_winner_fills_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seed's `x9.` that fails is returned; a winner fills the table.

    `down.example`'s subdomain raises `gaierror` and contributes nothing
    to the table, but is what this returns, for `P2pManager` to queue
    as an addr-fetch; every address `up.example`'s subdomain answers
    with lands in `peer_db.addresses`, on the chain's own port, `18444`,
    and not `8333`.
    """
    peer_db = a_peer_db(a_chain(["down.example", "up.example"]))
    loop = FakeLoop(
        {
            a_seed_host("down.example"): socket.gaierror("no such host"),
            a_seed_host("up.example"): ["1.2.3.4", "5.6.7.8"],
        }
    )
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(
        address_module, "_dns_seed_timestamp", lambda: _A_FIXED_DNS_STAMP
    )
    assert asyncio.run(peer_db.query_dns_seed("down.example")) == "down.example"
    assert asyncio.run(peer_db.query_dns_seed("up.example")) is None
    assert peer_db.addresses == {
        a_seed_answer("1.2.3.4"),
        a_seed_answer("5.6.7.8"),
    }
    # the bare name is never asked: only the `x9.` subdomain is
    assert loop.requested == [a_seed_host("down.example"), a_seed_host("up.example")]


def test_a_seed_answering_nothing_is_returned_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty answer, not only a `gaierror`, is "answered nothing"."""
    peer_db = a_peer_db(a_chain(["empty.example"]))
    loop = FakeLoop({a_seed_host("empty.example"): []})
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    assert asyncio.run(peer_db.query_dns_seed("empty.example")) == "empty.example"
    assert peer_db.addresses == set()


def test_every_seed_queried_is_taken_and_a_host_two_of_them_share_is_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The table after two lookups is the union of both seeds' answers.

    Two seeds share one address here: the table is the union over both
    calls and not only the last one, since a lookup that started over
    per seed would leave a node with whatever the seed queried last
    happened to know.
    """
    peer_db = a_peer_db(a_chain(["one.example", "two.example"]))
    loop = FakeLoop(
        {
            a_seed_host("one.example"): ["1.2.3.4", "5.6.7.8"],
            a_seed_host("two.example"): ["5.6.7.8", "9.10.11.12"],
        }
    )
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(
        address_module, "_dns_seed_timestamp", lambda: _A_FIXED_DNS_STAMP
    )
    assert asyncio.run(peer_db.query_dns_seed("one.example")) is None
    assert asyncio.run(peer_db.query_dns_seed("two.example")) is None
    assert peer_db.addresses == {
        a_seed_answer("1.2.3.4"),
        a_seed_answer("5.6.7.8"),
        a_seed_answer("9.10.11.12"),
    }


def test_a_seed_answering_past_the_cap_is_taken_only_up_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At most 32 of a seed's answers are kept, Core's own `nMaxIPs`."""
    peer_db = a_peer_db(a_chain(["many.example"]))
    ips = [f"1.2.{i}.4" for i in range(40)]
    loop = FakeLoop({a_seed_host("many.example"): ips})
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    taken: list[NetworkAddressV2] = []
    monkeypatch.setattr(
        peer_db, "add_addresses", lambda addresses, **_: taken.extend(addresses)
    )
    assert asyncio.run(peer_db.query_dns_seed("many.example")) is None
    assert len(taken) == 32


def test_a_dns_seed_s_answer_has_the_seed_as_its_internal_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's `ThreadDNSAddressSeed` passes `SetInternal(host)` to `Add`."""
    peer_db = a_peer_db(a_chain(["one.example"]))
    loop = FakeLoop({a_seed_host("one.example"): ["1.2.3.4"]})
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    sources: list[NetworkAddressV2 | None] = []
    monkeypatch.setattr(
        peer_db,
        "add_addresses",
        lambda _addresses, *, source, **_: sources.append(source),
    )
    assert asyncio.run(peer_db.query_dns_seed("one.example")) is None
    assert sources == [address_module.internal_source(a_seed_host("one.example"))]


def test_an_internal_source_is_cores_set_internal() -> None:
    """`CNetAddr::SetInternal`: ten octets of the name's SHA-256, prefixed."""
    source = address_module.internal_source("fixedseeds")
    digest = hashlib.sha256(b"fixedseeds").digest()
    assert source.address == bytes.fromhex("fd6b88c08724") + digest[:10]
    assert net_class(source) == Network.INTERNAL
    assert net_group(source) == bytes([Network.INTERNAL]) + digest[:10]
    assert net_group(address_module.internal_source("other")) != net_group(source)


def test_a_dns_seed_s_answer_is_backdated_with_no_extra_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`query_dns_seed`'s own stamp lands untouched: `time_penalty=0`.

    `_dns_seed_timestamp` is patched to a fixed value so the assertion
    is exact rather than a range: if `query_dns_seed` passed
    `add_addresses`'s own gossip default instead of its explicit `0`,
    this would land at `stamp - 2 * 3600` instead of `stamp`. Core's own
    `addrman.get().Add(vAdd, resolveSource)` (`src/net.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) passes no `time_penalty`
    of its own either, taking `AddrMan::Add`'s default, `0s`.
    """
    peer_db = a_peer_db(a_chain(["one.example"]))
    loop = FakeLoop({a_seed_host("one.example"): ["1.2.3.4"]})
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    stamp = int(time.time()) - 5 * 24 * 3600
    monkeypatch.setattr(address_module, "_dns_seed_timestamp", lambda: stamp)
    assert asyncio.run(peer_db.query_dns_seed("one.example")) is None
    (kept,) = peer_db.addresses
    assert kept.timestamp == stamp


def test_the_dns_seed_timestamp_is_uniform_between_three_and_seven_days_old() -> None:
    """`_dns_seed_timestamp`'s own range, Core's `rand_uniform_delay`.

    `ThreadDNSAddressSeed` (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag): `rng.rand_uniform_delay(Now<NodeSeconds>() - 3 * 24h,
    -4 * 24h)`, uniform over `[now - 7d, now - 3d]`.
    """
    before = time.time()
    draws = [address_module._dns_seed_timestamp() for _ in range(2000)]
    after = time.time()
    assert min(draws) >= before - address_module._DNS_SEED_MAX_AGE - 1
    assert max(draws) <= after - address_module._DNS_SEED_MIN_AGE
    # a real spread, not one fixed value landed on by chance
    assert len(set(draws)) > 1


class FakeIpv6Loop:
    """A `getaddrinfo` stand-in answering the real shape a AAAA record gives."""

    async def getaddrinfo(
        self, host: str, port: int, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int, int, int]]]:
        """Answer with a sockaddr of four fields, as a real AAAA lookup does."""
        assert kwargs.get("type") == socket.SOCK_STREAM
        # what a AAAA record resolves to: a sockaddr of four fields
        # rather than two, the flow info and the scope id being the two
        # a peer table has nowhere to put
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2a01:4f8::1", port, 0, 8))
        ]


def test_a_seed_answering_with_ipv6_gives_up_its_host_and_its_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A four-field IPv6 sockaddr still yields a usable host and port.

    `FakeIpv6Loop` answers the real, wider tuple `getaddrinfo` gives
    for an AAAA record, with flow info and scope id fields a peer entry
    has no room for -- this checks that only the host and the port are
    kept out of it.
    """
    peer_db = a_peer_db(a_chain(["v6.example"]))
    monkeypatch.setattr(asyncio, "get_running_loop", FakeIpv6Loop)
    monkeypatch.setattr(
        address_module, "_dns_seed_timestamp", lambda: _A_FIXED_DNS_STAMP
    )
    assert asyncio.run(peer_db.query_dns_seed("v6.example")) is None
    assert peer_db.addresses == {a_seed_answer("2a01:4f8::1")}


def test_an_address_is_drawn_from_the_ones_that_can_be_dialled() -> None:
    """`random_address` only ever draws the one entry it can dial.

    An onion address sits in the same table, undrawable across twenty
    draws -- enough that a draw not filtering by dialability would
    almost certainly have returned it at least once.
    """
    peer_db = a_peer_db()
    dialable = peer_address("1.2.3.4", 8333)
    peer_db.add_addresses([dialable])
    peer_db.addresses.add(an_onion_address())
    for _ in range(20):
        assert peer_db.random_address() == dialable


def test_a_table_holding_nothing_dialable_answers_that_there_is_nothing() -> None:
    """A table of onion and CJDNS peers alone answers `None`, not forever.

    This is the case a draw-until-one-can-be-dialled would never come
    back from, and one a node reaches in ordinary operation: onion,
    i2p and CJDNS peers gossiped without an IP counterpart fill a table
    with exactly this.
    """
    # the case a draw-until-one-can-be-dialled never comes back from,
    # and a table a node reaches in ordinary operation: onion, i2p and
    # cjdns peers fill it with exactly this
    peer_db = a_peer_db()
    peer_db.addresses.add(an_onion_address())
    peer_db.addresses.add(a_cjdns_address())
    assert call_within(peer_db.random_address) is None


def test_an_empty_table_answers_that_there_is_nothing() -> None:
    """`random_address` on an empty table answers `None`, not `IndexError`.

    The caller guards on `is_empty` before it draws, so this is not a
    path a node takes today; drawing from nothing still has to be an
    answer rather than an exception out of a housekeeping loop.
    """
    assert call_within(a_peer_db().random_address) is None


def test_the_draw_reaches_every_address_that_can_be_dialled() -> None:
    """Enough draws cover every dialable entry, IPv4 and IPv6 alike.

    Over all of them and not just the first one that will do: a node
    that only ever dials one entry of its table is a node with one
    peer. Eighty draws over four dialable entries and one onion address
    is enough that every entry, and only the dialable ones, shows up.
    """
    # over all of them, not the first one that will do: a node that only
    # ever dials one entry of its table is a node with one peer -- and
    # ipv4 and ipv6 are both dialled, not only the first
    peer_db = a_peer_db()
    dialable = {peer_address(f"1.2.3.{host}", 8333) for host in range(1, 4)}
    dialable.add(peer_address("2a01:4f8::1", 8333))
    peer_db.add_addresses(dialable)
    peer_db.addresses.add(an_onion_address())
    assert {peer_db.random_address() for _ in range(80)} == dialable


def a_group_of_addresses(
    prefix: str, count: int, *, now: int
) -> list[NetworkAddressV2]:
    """Return `count` addresses of one `/16`, `prefix` being its two octets."""
    return [
        peer_address(f"{prefix}.{n // 250}.{n % 250 + 1}", 8333, now)
        for n in range(count)
    ]


def a_colliding_address(
    peer_db: PeerDB, held: NetworkAddressV2, source: NetworkAddressV2
) -> NetworkAddressV2:
    """Return an address of `held`'s group in `held`'s slot of the new table."""
    group = net_group(source)
    slot = address_module._new_slot(peer_db._bucket_key, held, group)
    return next(
        other
        for other in (replace(held, port=port) for port in range(held.port + 1, 60000))
        if address_module._new_slot(peer_db._bucket_key, other, group) == slot
    )


def test_one_group_holds_only_the_bucket_it_maps_to() -> None:
    """A group's addresses from one source take the one bucket they map to.

    A `/16` gossiped by a peer of one group maps to the one bucket of 64
    positions, however many addresses it is gossiped as, so the table
    has room left and an address of another group from another source is
    stored.
    """
    peer_db = a_peer_db()
    now = int(time.time())
    source = peer_address("44.0.0.1", 8333)
    crowd = a_group_of_addresses("44.0", 10000, now=now)
    peer_db.add_addresses(crowd, source=source)
    assert 0 < len(peer_db.addresses) <= 64
    other = peer_address("8.8.8.8", 8333, timestamp=now)
    assert peer_db.add_addresses([other], source=peer_address("9.9.9.9", 8333)) == 1
    assert other.address in {a.address for a in peer_db.addresses}


def test_a_source_group_spreads_over_a_bounded_number_of_buckets() -> None:
    """The addresses of every group from one source map to 64 buckets at most.

    Core's `ADDRMAN_NEW_BUCKETS_PER_SOURCE_GROUP`: the groups of a `/8`
    gossiped from a single `/16` take no more of the table than that
    many buckets hold, and a second source's gossip of the same groups
    has buckets of its own.
    """
    peer_db = a_peer_db()
    now = int(time.time())
    groups = [
        peer_address(f"44.{group}.{n}.1", 8333, now)
        for group in range(256)
        for n in range(4)
    ]
    peer_db.add_addresses(groups, source=peer_address("44.0.0.1", 8333))
    buckets = buckets_used(peer_db)
    assert 1 < len(buckets) <= 64
    first = len(peer_db.addresses)
    peer_db.add_addresses(groups, source=peer_address("45.0.0.1", 8333))
    assert len(peer_db.addresses) > first


def test_a_group_with_most_addresses_is_drawn_as_one_bucket() -> None:
    """A bucket is drawn before an address, so a full bucket weighs as one.

    The `/16` holding as many addresses as its bucket takes and an
    address of another group each weigh a bucket, as Core's
    `Select_` has it, and neither is drawn alone.
    """
    peer_db = a_peer_db()
    now = int(time.time())
    peer_db.add_addresses(
        a_group_of_addresses("44.0", 1000, now=now),
        source=peer_address("44.0.0.1", 8333),
    )
    peer_db.add_addresses(
        [peer_address("8.8.8.8", 8333, now)], source=peer_address("9.9.9.9", 8333)
    )
    draw = peer_db.address_sampler()
    drawn = [draw() for _ in range(400)]
    other = sum(1 for a in drawn if a is not None and a.address == bytes([8, 8, 8, 8]))
    assert 100 < other < 300


def test_the_table_of_known_addresses_keeps_to_its_buckets_on_reopening(
    tmp_path: Path,
) -> None:
    """The stored rows keep their slots on a restart.

    The rows of the first run are the rows of the second, and gossip
    from the same group and the same source after the restart still
    leaves room for another group's.
    """
    now = int(time.time())
    source = peer_address("44.0.0.1", 8333)
    first = a_peer_db(data_dir=tmp_path)
    first.add_addresses(a_group_of_addresses("44.0", 10000, now=now), source=source)
    other = peer_address("8.8.8.8", 8333, timestamp=now)
    first.add_addresses([other], source=peer_address("9.9.9.9", 8333))
    rows = set(first.addresses)
    slots = slots_of(first)
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.addresses == rows
    assert slots_of(second) == slots
    second.add_addresses(a_group_of_addresses("44.0", 10000, now=now), source=source)
    assert len(second.addresses) <= len(rows) + 1
    other = peer_address("7.7.7.7", 8333, timestamp=now)
    assert second.add_addresses([other], source=peer_address("6.6.6.6", 8333)) == 1
    second.close()


def test_the_key_placing_the_known_addresses_is_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart places the rows by the key stored, not by a new one."""
    monkeypatch.setattr(
        address_module, "_new_bucket_key", lambda: secrets.token_bytes(32)
    )
    first = a_peer_db(data_dir=tmp_path)
    key = first._bucket_key
    first.close()
    second = a_peer_db(data_dir=tmp_path)
    assert second._bucket_key == key
    second.close()


def test_a_slot_of_an_address_that_is_not_terrible_is_kept() -> None:
    """Core's `AddSingle`: a slot's holder stays, the newcomer is dropped."""
    peer_db = a_peer_db()
    now = int(time.time())
    source = peer_address("9.9.9.9", 8333)
    held = peer_address("1.2.3.4", 8333, timestamp=now)
    newcomer = a_colliding_address(peer_db, held, source)
    assert peer_db.add_addresses([held], source=source, time_penalty=0) == 1
    assert peer_db.add_addresses([newcomer], source=source, time_penalty=0) == 0
    assert peer_db.addresses == {held}


def test_a_slot_of_a_terrible_address_goes_to_the_newcomer(tmp_path: Path) -> None:
    """Core's `AddSingle` overwrites a slot whose holder `IsTerrible` names.

    The holder's rows are gone from the store with it, and from the
    index of the table.
    """
    peer_db = a_peer_db(data_dir=tmp_path)
    now = int(time.time())
    source = peer_address("9.9.9.9", 8333)
    held = peer_address("1.2.3.4", 8333, timestamp=now - 31 * 24 * 3600)
    newcomer = replace(a_colliding_address(peer_db, held, source), timestamp=now)
    peer_db.add_addresses([held], source=source, time_penalty=0)
    assert peer_db.add_addresses([newcomer], source=source, time_penalty=0) == 1
    assert peer_db.addresses == {newcomer}
    assert table_keys(peer_db) == {b"known-" + address_module.endpoint_key(newcomer)}
    assert list(peer_db._slots) == [address_module._endpoint(newcomer)]
    peer_db.close()


def test_an_answered_address_holds_no_slot_of_the_new_table() -> None:
    """`Good_` moves an address out of its buckets: another takes the slot."""
    peer_db = a_peer_db()
    now = int(time.time())
    source = peer_address("9.9.9.9", 8333)
    held = peer_address("1.2.3.4", 8333, timestamp=now - 31 * 24 * 3600)
    newcomer = replace(a_colliding_address(peer_db, held, source), timestamp=now)
    peer_db.add_addresses([held], source=source, time_penalty=0)
    assert peer_db.add_active_address(held)
    assert address_module._endpoint(held) not in peer_db._slots
    assert peer_db.add_addresses([newcomer], source=source, time_penalty=0) == 1
    assert peer_db.addresses == {held, newcomer}


def test_an_overlay_address_is_placed_by_its_own_group() -> None:
    """A Tor, an I2P and a CJDNS address each take a slot of the new table."""
    peer_db = a_peer_db()
    overlay = [
        an_onion_address(),
        NetworkAddressV2(0, 0, BIP155Network.I2P, b"\xa5" * 32, 8333),
        a_cjdns_address(),
    ]
    assert peer_db.add_addresses(overlay) == len(overlay)
    assert len(peer_db._slots) == len(overlay)


def test_a_draw_takes_a_bucket_before_an_address() -> None:
    """`_select` weighs a bucket of one address as it does a bucket of many."""
    one = peer_address("1.2.3.4", 8333)
    peer_db = a_peer_db()
    peer_db.add_addresses([one])
    peer_db.add_addresses(
        [peer_address("5.6.7.8", position) for position in range(40)],
        source=peer_address("9.9.9.9", 1),
    )
    draw = peer_db.address_sampler(new_only=True)
    drawn = [draw() for _ in range(400)]
    assert 80 < sum(1 for a in drawn if a == one) < 320


@pytest.mark.parametrize(("start", "index"), [(0, 0), (11, 1), (12, 1), (13, 0)])
def test_a_draw_takes_the_first_address_from_a_position_on(
    monkeypatch: pytest.MonkeyPatch, start: int, index: int
) -> None:
    """The first position from the one drawn, looping round, as `Select_`."""
    rows = [peer_address("1.2.3.4", 1), peer_address("1.2.3.4", 2)]
    side = address_module._Side(
        {(5, 3): rows[0], (5, 12): rows[1]},
        [5],
        lambda row: row,
        lambda _row: True,
        lambda _row: 1.0,
    )
    monkeypatch.setattr(
        secrets, "choice", lambda seq: start if isinstance(seq, range) else seq[0]
    )
    monkeypatch.setattr(address_module, "_roll", lambda _factor: True)
    assert address_module._select(None, side) == rows[index]


def test_a_held_address_is_updated_where_its_bucket_is_full() -> None:
    """Gossip for an address already held keeps its slot and takes no other."""
    peer_db = a_peer_db()
    now = int(time.time())
    source = peer_address("44.0.0.1", 8333)
    peer_db.add_addresses(a_group_of_addresses("44.0", 1000, now=now), source=source)
    held = next(iter(peer_db.addresses))
    size = len(peer_db.addresses)
    slots = slots_of(peer_db)[address_module._endpoint(held)]
    assert (
        peer_db.add_addresses(
            [replace(held, services=ServiceFlags.NODE_BLOOM)], source=source
        )
        == 0
    )
    assert len(peer_db.addresses) == size
    assert {
        a.services
        for a in peer_db.addresses
        if a.address == held.address and a.port == held.port
    } == {held.services | ServiceFlags.NODE_BLOOM}
    assert slots_of(peer_db)[address_module._endpoint(held)] == slots


def test_a_store_of_an_earlier_release_is_placed_on_load(tmp_path: Path) -> None:
    """Rows with no source or key stored are placed as their own source's.

    A `/16` stored beyond what one bucket holds is cut down to it, the
    rows over it deleted, and an address of another group is kept.
    """
    now = int(time.time())
    store = KeyValueStore(tmp_path / "peers")
    crowd = a_group_of_addresses("44.0", 1000, now=now)
    other = peer_address("8.8.8.8", 8333, timestamp=now)
    for row in (*crowd, other):
        store.put(
            b"known-" + address_module.endpoint_key(row),
            row.serialize(check_validity=False),
        )
    store.close()

    second = a_peer_db(data_dir=tmp_path)
    assert other in second.addresses
    assert 1 < len(second.addresses) <= 64 + 1
    assert table_keys(second) == {
        b"known-" + address_module.endpoint_key(row) for row in second.addresses
    }
    second.close()


def test_a_row_stored_without_a_source_is_its_own_source(tmp_path: Path) -> None:
    """Each group of an earlier release's rows has buckets of its own."""
    now = int(time.time())
    store = KeyValueStore(tmp_path / "peers")
    for group in range(200):
        row = peer_address(f"44.{group}.0.1", 8333, timestamp=now)
        store.put(
            b"known-" + address_module.endpoint_key(row),
            row.serialize(check_validity=False),
        )
    store.close()

    peer_db = a_peer_db(data_dir=tmp_path)
    assert len(peer_db.addresses) > 150
    assert len(buckets_used(peer_db)) > 64
    peer_db.close()


def test_a_source_row_with_no_known_row_is_dropped_on_load(tmp_path: Path) -> None:
    """A stored source group with no known row to belong to is deleted."""
    first = a_peer_db(data_dir=tmp_path)
    assert first.db is not None
    orphan = b"source-" + address_module.endpoint_key(peer_address("1.2.3.4", 8333))
    first.db.put(orphan, b"\x01\x01\x02")
    first.close()
    second = a_peer_db(data_dir=tmp_path)
    assert second.db is not None
    assert orphan not in {key for key, _ in second.db}
    second.close()


def test_a_terrible_holder_keeps_its_slot_on_load(tmp_path: Path) -> None:
    """`Unserialize` keeps the first holder of a slot, terrible or not."""
    now = int(time.time())
    source = peer_address("9.9.9.9", 8333)
    first = a_peer_db(data_dir=tmp_path)
    held = peer_address("1.2.3.4", 8333, timestamp=now - 31 * 24 * 3600)
    newcomer = replace(a_colliding_address(first, held, source), timestamp=now)
    first.add_addresses([held], source=source, time_penalty=0)
    assert first.db is not None
    first.db.put(
        b"known-" + address_module.endpoint_key(newcomer),
        newcomer.serialize(check_validity=False),
    )
    first.db.put(
        b"source-" + address_module.endpoint_key(newcomer),
        packed(net_group(source)) + packed(net_group(source)),
    )
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.addresses == {held}
    keys = {key for key, _ in second.db} if second.db is not None else set()
    assert b"known-" + address_module.endpoint_key(newcomer) not in keys
    second.close()


def test_a_row_whose_stored_slot_is_held_is_tried_at_its_first_source(
    tmp_path: Path,
) -> None:
    """`Unserialize`: a held slot sends the row once to its first source's."""
    now = int(time.time())
    source = peer_address("9.9.9.9", 8333)
    primary = net_group(peer_address("7.7.7.7", 1))
    first = a_peer_db(data_dir=tmp_path)
    held = peer_address("1.2.3.4", 8333, timestamp=now)
    newcomer = a_colliding_address(first, held, source)
    first.add_addresses([held], source=source, time_penalty=0)
    assert first.db is not None
    suffix = address_module.endpoint_key(newcomer)
    first.db.put(b"known-" + suffix, newcomer.serialize(check_validity=False))
    first.db.put(b"source-" + suffix, packed(primary) + packed(net_group(source)))
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    slot = address_module._new_slot(second._bucket_key, newcomer, primary)
    assert second._slots[address_module._endpoint(newcomer)] == {slot: primary}
    assert second._source[address_module._endpoint(newcomer)] == primary
    assert second.db is not None
    assert second.db.get(b"source-" + suffix) == packed(primary) + packed(primary)
    second.close()


def test_a_row_dropped_on_load_is_deleted_from_the_store(tmp_path: Path) -> None:
    """A row whose slot is held by another is deleted, with its source."""
    now = int(time.time())
    source = peer_address("9.9.9.9", 8333)
    first = a_peer_db(data_dir=tmp_path)
    held = peer_address("1.2.3.4", 8333, timestamp=now)
    other = a_colliding_address(first, held, source)
    first.add_addresses([held], source=source, time_penalty=0)
    assert first.db is not None
    first.db.put(
        b"known-" + address_module.endpoint_key(other),
        other.serialize(check_validity=False),
    )
    first.db.put(
        b"source-" + address_module.endpoint_key(other), packed(net_group(source))
    )
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.db is not None
    (survivor,) = second.addresses
    survivor_key = address_module.endpoint_key(survivor)
    assert {key for key, _ in second.db if key.startswith((b"known-", b"source-"))} == {
        b"known-" + survivor_key,
        b"source-" + survivor_key,
    }
    second.close()


def test_a_v4_mapped_ipv6_record_is_not_kept() -> None:
    """A v4-mapped `IPV6` record is dropped by `add_addresses`.

    Not gossiped back either: BIP155 says a client SHOULD ignore an
    `IPV6` entry whose octets are `::ffff:0:0/96`, the IPv4 mapping.
    #151: keeping it is an entry that later writes into an addr version
    1 message as the same sixteen octets an ordinary IPv4 peer does.
    """
    peer_db = a_peer_db()
    peer_db.add_addresses([peer_address(_A_V4_MAPPED_ADDRESS, 8333)])
    assert not peer_db.addresses


def test_an_onioncat_ipv6_record_is_not_kept() -> None:
    """An OnionCat-range `IPV6` record is dropped by `add_addresses` too.

    The other half of the same rule: `fd87:d87e:eb43::/48` is the
    OnionCat range a TORv2 address used to be embedded in as a fake
    IPv6 one, before BIP155 gave onion addresses a network of their
    own.
    """
    # the other half of the same rule: `fd87:d87e:eb43::/48` is where a
    # TORv2 address used to be embedded in a fake IPv6 one
    peer_db = a_peer_db()
    peer_db.add_addresses([peer_address(_AN_ONIONCAT_ADDRESS, 8333)])
    assert not peer_db.addresses


def test_an_ordinary_ipv6_record_is_kept() -> None:
    """An `IPV6` address outside both reserved ranges is kept as-is.

    The rule the two tests above check is about the two reserved
    ranges and not about the network id: an address outside both is an
    ordinary peer, which this checks is not caught by the same filter.
    """
    # the rule is about the two reserved ranges and not about the
    # network id: an address outside both is an ordinary peer
    peer_db = a_peer_db()
    address = peer_address("2a01:4f8::1", 8333)
    peer_db.add_addresses([address])
    assert peer_db.addresses == {address}


@pytest.mark.parametrize(
    "address",
    [
        peer_address("127.0.0.1", 18444),
        peer_address("0.0.0.0", 8333),  # noqa: S104
        peer_address("10.0.0.1", 8333),
        peer_address("192.168.1.1", 8333),
        peer_address("255.255.255.255", 8333),
        peer_address("::1", 8333),
        peer_address("2001:db8::1", 8333),
        peer_address("fe80::1", 8333),
        NetworkAddressV2(0, 0, BIP155Network.CJDNS, b"\x02" * 16, 8333),
        NetworkAddressV2(0, 0, BIP155Network.TORV2, b"\x02" * 10, 8333),
        NetworkAddressV2(0, 0, 7, b"\x02" * 16, 8333),
        NetworkAddressV2(0, 0, 250, b"\x02" * 8, 8333),
    ],
    ids=[
        "loopback",
        "any",
        "rfc1918 10/8",
        "rfc1918 192.168/16",
        "none",
        "ipv6 loopback",
        "rfc3849",
        "rfc4862",
        "cjdns off its prefix",
        "torv2",
        "yggdrasil",
        "an unassigned network",
    ],
)
def test_an_unroutable_address_is_not_kept(address: NetworkAddressV2) -> None:
    """ISS 1091: Core's `AddrManImpl::AddSingle` refuses what is not routable.

    A network id Core does not decode is an address its `IsValid`
    refuses, and so not routable either.
    """
    peer_db = a_peer_db()
    peer_db.add_addresses([address])
    assert not peer_db.addresses


@pytest.mark.parametrize(
    "address",
    [
        peer_address("1.2.3.4", 8333),
        peer_address("2a01:4f8::1", 8333),
        NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x02" * 32, 8333),
        NetworkAddressV2(0, 0, BIP155Network.I2P, b"\x02" * 32, 8333),
        NetworkAddressV2(0, 0, BIP155Network.CJDNS, b"\xfc" + b"\x02" * 15, 8333),
    ],
    ids=["ipv4", "ipv6", "torv3", "i2p", "cjdns"],
)
def test_a_routable_address_of_every_network_core_decodes_is_kept(
    address: NetworkAddressV2,
) -> None:
    """ISS 1091: what `is_routable` lets through, one of each network."""
    peer_db = a_peer_db()
    peer_db.add_addresses([address])
    assert peer_db.addresses == {address}


def test_a_second_gossip_of_one_endpoint_settles_on_the_last_one_processed() -> None:
    """Two records for one endpoint are one member, not two.

    #247: whichever of the two is processed last decides the row's own
    fields other than services, which accumulate (the test beside this
    one covers that); `endpoint_key`'s own equality is what settles
    them onto one row rather than growing the table by one member per
    gossip of the same endpoint.
    """
    peer_db = a_peer_db()
    early = peer_address("1.2.3.4", 8333, timestamp=1)
    late = peer_address("1.2.3.4", 8333, timestamp=2)
    peer_db.add_addresses([early, late, early])
    (kept,) = peer_db.addresses
    assert kept.address == early.address


def test_a_gossiped_address_is_kept_with_a_two_hour_penalty() -> None:
    """Core's own flat discount for a whole gossiped batch.

    `AddrManImpl::AddSingle` (`src/addrman.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `pinfo->nTime =
    max(0, addr.nTime - time_penalty)`, `time_penalty` the two hours
    `net_processing.cpp`'s `ADDR`/`ADDRV2` handler passes for the whole
    batch, `add_addresses`'s own default. btclib-org/btclib-node#1380
    """
    peer_db = a_peer_db()
    now = int(time.time())
    address = peer_address("1.2.3.4", 8333, timestamp=now)
    peer_db.add_addresses([address])
    (kept,) = peer_db.addresses
    assert kept.timestamp == now - 2 * 3600


def test_a_gossiped_timestamp_under_the_penalty_floors_at_zero() -> None:
    """The stored timestamp never goes negative.

    `AddrManImpl::AddSingle`'s own `std::max(NodeSeconds{0s}, ...)`,
    same sha as the test above.
    """
    peer_db = a_peer_db()
    address = peer_address("1.2.3.4", 8333, timestamp=1)
    peer_db.add_addresses([address])
    (kept,) = peer_db.addresses
    assert kept.timestamp == 0


def test_a_self_announced_address_is_kept_with_no_penalty() -> None:
    """A peer's own address, gossiped by itself, costs it no penalty.

    `AddSingle`'s own `if (addr == source) { time_penalty = 0s; }`
    (same sha as the test above): the exemption is `add_addresses`'
    `source` argument, which `callbacks._store_gossip` passes as the
    connection's own address. Compared by host only -- `_host` rather
    than `==` -- since a self-announcement still carries its own
    services, timestamp and, as the test below covers, port, none of
    which are what `CNetAddr::operator==` names the peer by.
    """
    peer_db = a_peer_db()
    now = int(time.time())
    source = peer_address("1.2.3.4", 8333, timestamp=1, services=ServiceFlags.NODE_NONE)
    announced = peer_address("1.2.3.4", 8333, timestamp=now)
    peer_db.add_addresses([announced], source=source)
    (kept,) = peer_db.addresses
    assert kept.timestamp == now


def test_a_self_announcement_on_a_different_port_still_costs_no_penalty() -> None:
    """An inbound peer's own ephemeral source port does not break the exemption.

    `source` for an inbound connection is `conn.address` still carrying
    the peer's ephemeral TCP source port from `sock.accept()`'s own
    peername -- `callbacks.py`'s own `version` handler rewrites
    `conn.address` for an outbound connection alone -- while the address
    a peer announces about itself names its own listening port instead,
    8333 here against a source on an unrelated ephemeral one. Core's own
    comparison never sees either port: `AddrManImpl::AddSingle`'s `addr
    == source` slices `addr`, a `CAddress`, down to its own `CNetAddr`
    base before `CNetAddr::operator==` ever runs (`src/addrman.cpp` and
    `src/netaddress.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag),
    so the exemption still applies (btclib-org/btclib-node#1380, review
    round 2).
    """
    peer_db = a_peer_db()
    now = int(time.time())
    source = peer_address(
        "1.2.3.4", 54321, timestamp=1, services=ServiceFlags.NODE_NONE
    )
    announced = peer_address("1.2.3.4", 8333, timestamp=now)
    peer_db.add_addresses([announced], source=source)
    (kept,) = peer_db.addresses
    assert kept.timestamp == now


def test_a_time_penalty_override_replaces_the_gossip_default() -> None:
    """`time_penalty` is a caller's own choice, not only the gossip default.

    `query_dns_seed` passes `0` -- `AddrMan::Add`'s own default, its
    answer already backdated before it ever reaches `add_addresses`
    (the test for that is `test_a_dns_seed_s_answer_is_backdated`
    below) -- and this is the same knob exercised directly, for an
    arbitrary penalty rather than either of the two production values.
    """
    peer_db = a_peer_db()
    now = int(time.time())
    address = peer_address("1.2.3.4", 8333, timestamp=now)
    peer_db.add_addresses([address], time_penalty=10)
    (kept,) = peer_db.addresses
    assert kept.timestamp == now - 10


_FULL = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_FULL, ServiceFlags.NODE_NONE),
        (ServiceFlags.NODE_NONE, _FULL),
        (ServiceFlags.NODE_NETWORK, ServiceFlags.NODE_WITNESS),
    ],
    ids=["fewer later", "more later", "disjoint"],
)
def test_two_gossiped_records_for_one_endpoint_settle_on_every_service(
    tmp_path: Path, first: int, second: int
) -> None:
    """ISS 1276: Core's `AddSingle` ORs the services of a known endpoint.

    #247: the two records are one member of the table, not one per
    `services` value, and the row on disk carries the same services, so
    a restart reads them back.
    """
    peer_db = a_peer_db(data_dir=tmp_path)
    peer_db.add_addresses([peer_address("1.2.3.4", 8333, services=first)])
    peer_db.add_addresses([peer_address("1.2.3.4", 8333, services=second)])
    (kept,) = peer_db.addresses
    assert kept.services == first | second
    peer_db.close()
    (reloaded,) = a_peer_db(data_dir=tmp_path).addresses
    assert reloaded.services == first | second


def test_a_gossip_adds_its_services_to_the_answered_row_too(tmp_path: Path) -> None:
    """ISS 1276: Core's `AddSingle` ORs gossip into a tried entry as well.

    The answered row stands for Core's tried entry, and its row on disk
    carries the same services, two records of one gossip included.
    """
    peer_db = a_peer_db(data_dir=tmp_path)
    endpoint = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    peer_db.add_addresses([endpoint], time_penalty=0)
    peer_db.add_active_address(endpoint)
    peer_db.add_addresses(
        replace(endpoint, services=services)
        for services in (ServiceFlags.NODE_NETWORK, ServiceFlags.NODE_WITNESS)
    )
    (answered,) = peer_db.active_addresses
    assert answered.services == _FULL
    peer_db.close()
    (reloaded,) = a_peer_db(data_dir=tmp_path).active_addresses
    assert reloaded.services == _FULL


@pytest.mark.parametrize("answered", [False, True], ids=["known", "answered"])
def test_set_services_replaces_what_gossip_had_added(
    tmp_path: Path, *, answered: bool
) -> None:
    """ISS 1276: Core's `SetServices`, which overwrites rather than ORs.

    Both rows the endpoint has are written, in memory and on disk.
    """
    peer_db = a_peer_db(data_dir=tmp_path)
    endpoint = peer_address("1.2.3.4", 8333, services=_FULL)
    peer_db.add_addresses([endpoint])
    if answered:
        peer_db.add_active_address(endpoint)
    peer_db.set_services(
        replace(endpoint, services=ServiceFlags.NODE_NONE),
        ServiceFlags.NODE_NETWORK_LIMITED,
    )
    rows = [*peer_db.addresses, *peer_db.active_addresses]
    assert len(rows) == 1 + answered
    assert {row.services for row in rows} == {ServiceFlags.NODE_NETWORK_LIMITED}
    peer_db.close()
    reloaded = a_peer_db(data_dir=tmp_path)
    rows = [*reloaded.addresses, *reloaded.active_addresses]
    assert {row.services for row in rows} == {ServiceFlags.NODE_NETWORK_LIMITED}


def test_set_services_writes_an_answered_row_held_in_memory_alone() -> None:
    """ISS 1276: a table with no store updates its answered row all the same."""
    peer_db = a_peer_db()
    endpoint = peer_address("1.2.3.4", 8333, services=_FULL)
    peer_db.add_addresses([endpoint])
    peer_db.add_active_address(endpoint)
    peer_db.set_services(endpoint, ServiceFlags.NODE_WITNESS)
    (answered,) = peer_db.active_addresses
    assert answered.services == ServiceFlags.NODE_WITNESS


def test_set_services_records_no_endpoint_the_table_does_not_hold() -> None:
    """ISS 1276: `SetServices_` bails out where `Find` finds nothing."""
    peer_db = a_peer_db()
    peer_db.add_addresses([peer_address("5.6.7.8", 8333)])
    peer_db.set_services(peer_address("1.2.3.4", 8333), _FULL)
    assert peer_db.addresses == {peer_address("5.6.7.8", 8333)}
    assert not peer_db.active_addresses


def test_a_handshake_leaves_the_time_of_the_address_alone(tmp_path: Path) -> None:
    """ISS 1364: Core's `Good_` does not update `nTime`.

    "To avoid leaking information about currently-connected peers": the
    known row keeps the time gossip gave the address, so what `get_addr`
    serves does not say that this node has just connected to it. The
    answered row is stamped now, Core's `m_last_success`, which nothing
    serves.
    """
    peer_db = a_peer_db(data_dir=tmp_path)
    heard = int(time.time()) - 3 * 24 * 3600
    endpoint = peer_address("1.2.3.4", 8333, timestamp=heard)
    peer_db.add_addresses([endpoint], time_penalty=0)
    before = int(time.time())
    assert peer_db.add_active_address(replace(endpoint, timestamp=0))
    (answered,) = peer_db.active_addresses
    assert answered.timestamp >= before
    assert peer_db.get_addr(1000, 100) == [endpoint]
    peer_db.close()


@pytest.mark.parametrize("kind", ["old gossip", "timestamp 1"])
def test_a_handshaken_endpoint_is_not_pruned_for_the_age_of_its_gossip(
    kind: str,
) -> None:
    """ISS 1364: the prune reads `m_last_success`, not the gossiped `nTime`.

    A row gossiped 40 days ago, or at time 1, answers a handshake and
    stays in the answered table; Core never drops a tried entry as
    terrible for its `nTime`.
    """
    peer_db = a_peer_db()
    old = 1 if kind == "timestamp 1" else int(time.time()) - 40 * 24 * 3600
    endpoint = peer_address("1.2.3.4", 8333, timestamp=old)
    peer_db.add_addresses([endpoint], time_penalty=0)
    peer_db.add_active_address(endpoint)
    assert len(peer_db.active_addresses) == 1
    peer_db.add_addresses([replace(endpoint, timestamp=1)], time_penalty=0)
    assert len(peer_db.active_addresses) == 1


def test_an_endpoint_the_table_does_not_hold_is_not_answered() -> None:
    """ISS 1364: `Good_` finds nothing to move, and `False` says so."""
    peer_db = a_peer_db()
    assert not peer_db.add_active_address(peer_address("1.2.3.4", 8333))
    assert not peer_db.active_addresses


@pytest.mark.parametrize("answered", [False, True], ids=["known", "answered"])
def test_connected_moves_a_stale_time_forward(
    tmp_path: Path, *, answered: bool
) -> None:
    """ISS 1364: Core's `Connected_` sets `nTime` to now past twenty minutes.

    The known row is written, in memory and on disk; the answered row's
    own time, `m_last_success`, is not.
    """
    peer_db = a_peer_db(data_dir=tmp_path)
    endpoint = peer_address("1.2.3.4", 8333, timestamp=int(time.time()) - 3 * 24 * 3600)
    peer_db.add_addresses([endpoint], time_penalty=0)
    before_rows: list[NetworkAddressV2] = []
    if answered:
        peer_db.add_active_address(endpoint)
        before_rows = list(peer_db.active_addresses)
    before = int(time.time())
    peer_db.connected(replace(endpoint, timestamp=0))
    (known,) = peer_db.addresses
    assert known.timestamp >= before
    assert peer_db.active_addresses == before_rows
    peer_db.close()
    reloaded = a_peer_db(data_dir=tmp_path)
    (known,) = reloaded.addresses
    assert known.timestamp >= before
    reloaded.close()


def test_connected_waits_out_a_gossip_in_progress() -> None:
    """ISS 1364: `connected` writes `addresses` under `_addresses_lock`."""
    peer_db = a_peer_db()
    endpoint = peer_address("1.2.3.4", 8333, timestamp=int(time.time()) - 24 * 3600)
    peer_db.add_addresses([endpoint], time_penalty=0)
    with peer_db._addresses_lock:
        caller = threading.Thread(target=peer_db.connected, args=(endpoint,))
        caller.start()
        caller.join(timeout=0.2)
        assert caller.is_alive()
    caller.join(timeout=5)
    assert not caller.is_alive()
    (known,) = peer_db.addresses
    assert known.timestamp > endpoint.timestamp


def test_connected_moves_a_stale_time_of_a_table_with_no_store() -> None:
    """ISS 1364: a table kept in memory alone is written all the same."""
    peer_db = a_peer_db()
    endpoint = peer_address("1.2.3.4", 8333, timestamp=int(time.time()) - 24 * 3600)
    peer_db.add_addresses([endpoint], time_penalty=0)
    before = int(time.time())
    peer_db.connected(endpoint)
    (row,) = peer_db.addresses
    assert row.timestamp >= before


def test_connected_leaves_a_time_newer_than_twenty_minutes() -> None:
    """ISS 1364: `Connected_` moves `nTime` only where it is older than that."""
    peer_db = a_peer_db()
    recent = int(time.time()) - address_module._CONNECTED_UPDATE_INTERVAL + 60
    endpoint = peer_address("1.2.3.4", 8333, timestamp=recent)
    peer_db.add_addresses([endpoint], time_penalty=0)
    peer_db.connected(endpoint)
    assert peer_db.addresses == {endpoint}


def test_connected_records_no_endpoint_the_table_does_not_hold() -> None:
    """ISS 1364: `Connected_` bails out where `Find` finds nothing."""
    peer_db = a_peer_db()
    peer_db.connected(peer_address("1.2.3.4", 8333))
    assert not peer_db.addresses
    assert not peer_db.active_addresses


def test_an_older_gossip_does_not_lower_the_time_held() -> None:
    """ISS 1603: `AddSingle` moves `nTime` forward only."""
    peer_db = a_peer_db()
    now = int(time.time())
    endpoint = peer_address("1.2.3.4", 8333, timestamp=now)
    peer_db.add_addresses([endpoint], time_penalty=0)
    peer_db.add_addresses(
        [replace(endpoint, timestamp=now - 20 * 24 * 3600)], time_penalty=0
    )
    (row,) = peer_db.addresses
    assert row.timestamp == now


@pytest.mark.parametrize(
    ("newer_by", "moved"),
    [(1800, False), (2 * 3600, True)],
    ids=["within the hour", "past the hour"],
)
def test_a_newer_gossip_moves_the_time_past_the_update_interval(
    newer_by: int, *, moved: bool
) -> None:
    """ISS 1603: a gossip under a day old moves a time held past an hour."""
    peer_db = a_peer_db()
    now = int(time.time())
    held = now - 3 * 3600
    peer_db.add_addresses(
        [peer_address("1.2.3.4", 8333, timestamp=held)], time_penalty=0
    )
    gossip = peer_address("1.2.3.4", 8333, timestamp=held + newer_by)
    peer_db.add_addresses([gossip], time_penalty=0)
    (row,) = peer_db.addresses
    assert row.timestamp == (gossip.timestamp if moved else held)


@pytest.mark.parametrize(
    ("newer_by", "moved"), [(5000, False), (11_000, True)], ids=["inside", "past"]
)
def test_the_update_interval_counts_the_gossip_penalty(
    newer_by: int, *, moved: bool
) -> None:
    """ISS 1603: `AddSingle` moves `nTime` past `interval + time_penalty`.

    With Core's two hours, a gossip under an hour and a half newer and one
    three hours newer are either side of the hour plus the penalty.
    """
    now = int(time.time())
    held = now - 20_000
    penalty = address_module._GOSSIP_TIME_PENALTY
    row = peer_address("1.2.3.4", 8333, timestamp=held)
    gossip = replace(row, timestamp=held + newer_by)
    expected = max(0, gossip.timestamp - penalty) if moved else held
    assert address_module._held_time(row, gossip, penalty, now) == expected


def test_the_online_window_is_read_off_the_gossiped_time_itself() -> None:
    """ISS 1603: `currently_online` uses `addr.nTime`, not the penalised one.

    A gossip 23.5 hours old is online, so an hour moves a held time; its
    time less the two-hour penalty is more than a day old, which would
    make the interval a day.
    """
    now = int(time.time())
    penalty = address_module._GOSSIP_TIME_PENALTY
    gossiped = now - 23 * 3600 - 1800
    held = gossiped - 3600 - penalty - 100
    row = peer_address("1.2.3.4", 8333, timestamp=held)
    gossip = replace(row, timestamp=gossiped)
    assert address_module._held_time(row, gossip, penalty, now) == gossiped - penalty


def test_a_gossip_a_day_old_moves_the_time_past_a_day_only() -> None:
    """ISS 1603: the interval is a day where the gossip is itself a day old."""
    peer_db = a_peer_db()
    now = int(time.time())
    held = now - 10 * 24 * 3600
    peer_db.add_addresses(
        [peer_address("1.2.3.4", 8333, timestamp=held)], time_penalty=0
    )
    for newer_by, expected in ((3 * 3600, held), (2 * 24 * 3600, held + 2 * 24 * 3600)):
        peer_db.add_addresses(
            [peer_address("1.2.3.4", 8333, timestamp=held + newer_by)], time_penalty=0
        )
        (row,) = peer_db.addresses
        assert row.timestamp == expected


@pytest.mark.parametrize("ip", ["2002:102:304::1", "2001:0:102:304::1", "1.2.3.4"])
def test_a_6to4_or_teredo_address_is_of_the_ipv4_class(ip: str) -> None:
    """ISS 1443: Core's `GetNetClass` of an IPv6 address linked to IPv4."""
    assert address_module.network_class(peer_address(ip, 8333)) == Network.IPV4


def test_get_addr_keeps_to_the_net_class_asked_for() -> None:
    """ISS 1443: `GetAddr_` filters on `GetNetClass`, so 6to4 is IPv4."""
    peer_db = a_peer_db()
    now = int(time.time())
    linked = peer_address("2002:102:304::1", 8333, timestamp=now)
    plain = peer_address("2a01:4f8::1", 8333, timestamp=now)
    peer_db.add_addresses([linked, plain], time_penalty=0)
    assert peer_db.get_addr(0, 0, Network.IPV4) == [linked]
    assert peer_db.get_addr(0, 0, Network.IPV6) == [plain]


def test_get_addr_draws_from_an_address_no_handshake_answered() -> None:
    """ISS 1365: Core's `GetAddr_` draws from every entry, new and tried.

    The table here holds one gossiped address, answered by no handshake,
    and one answered address, and the draw is not limited to either.
    """
    peer_db = a_peer_db()
    now = int(time.time())
    gossiped = peer_address("1.2.3.4", 8333, timestamp=now)
    answered = peer_address("5.6.7.8", 8333, timestamp=now)
    peer_db.add_addresses([gossiped, answered], time_penalty=0)
    peer_db.add_active_address(answered)
    assert set(peer_db.get_addr(1000, 100)) == {gossiped, answered}


def test_get_addr_sizes_its_answer_from_every_entry_and_skips_a_terrible_one() -> None:
    """ISS 1365: a terrible entry is counted in the size, not drawn.

    Ten entries, four of them terrible (timestamp 0), at 30%: the size
    is three, drawn from the six that are not terrible. Sized from those
    six, it would be two.
    """
    peer_db = a_peer_db()
    now = int(time.time())
    fresh = [peer_address(f"1.2.3.{n}", 8333, timestamp=now) for n in range(1, 7)]
    terrible = [peer_address(f"1.2.4.{n}", 8333) for n in range(1, 5)]
    peer_db.add_addresses([*fresh, *terrible], time_penalty=0)
    for _ in range(20):
        sample = peer_db.get_addr(1000, 30)
        assert len(sample) == 3
        assert set(sample) <= set(fresh)


def test_get_addr_answers_less_where_the_table_is_terrible() -> None:
    """ISS 1365: a table of terrible entries answers nothing, as Core's does."""
    peer_db = a_peer_db()
    peer_db.add_addresses(
        [peer_address(f"1.2.3.{n}", 8333) for n in range(1, 5)], time_penalty=0
    )
    assert peer_db.get_addr(1000, 100) == []


def test_get_addr_is_capped_at_the_most_it_may_answer() -> None:
    """ISS 1365: the answer is at most `max_addresses`, whatever the share."""
    peer_db = a_peer_db()
    now = int(time.time())
    peer_db.add_addresses(
        [peer_address(f"1.2.3.{n}", 8333, timestamp=now) for n in range(1, 11)],
        time_penalty=0,
    )
    assert len(peer_db.get_addr(4, 100)) == 4


@pytest.mark.parametrize(("coin", "table"), [(1, "answered"), (0, "gossiped")])
def test_a_coin_decides_between_the_answered_and_the_gossiped_table(
    monkeypatch: pytest.MonkeyPatch, coin: int, table: str
) -> None:
    """ISS 1201: Core's `Select_` flips `randbool()` where both tables hold one.

    The coin fixed each way reaches each table. `addresses` holds both
    endpoints, as it does once a gossiped peer answers, and the answered
    one alone holds `1.2.3.4`: the gossiped side leaves it out, so the
    coin at 0 reaches `5.6.7.8` every time.
    """
    peer_db = a_peer_db()
    answered = peer_address("1.2.3.4", 8333)
    gossiped = peer_address("5.6.7.8", 8333)
    peer_db.add_addresses([answered, gossiped])
    peer_db.add_active_address(answered)
    monkeypatch.setattr(secrets, "randbelow", lambda n: coin)
    drawn = peer_db.random_address()
    assert drawn is not None
    expected = answered if table == "answered" else gossiped
    assert drawn.address == expected.address


def test_an_answered_endpoint_is_drawn_from_the_answered_table_alone() -> None:
    """ISS 1201: Core's `Good_` moves an entry from the new table to tried.

    So the two tables `Select_` flips between never hold one endpoint
    twice. `addresses` keeps the answered endpoint, so the gossiped
    side of the draw leaves it out, with `services` differing to show
    the match is on the endpoint; `5.6.7.8`, gossiped alone, is the
    control.
    """
    peer_db = a_peer_db()
    answered = peer_address("1.2.3.4", 8333)
    gossiped = peer_address("5.6.7.8", 8333)
    peer_db.add_addresses([peer_address("1.2.3.4", 8333, services=1), gossiped])
    peer_db.add_active_address(answered)
    # a `functools.partial` over `_select`, whose two tables are its args
    tried, new = cast("Any", peer_db.address_sampler()).args
    assert [a.address for a in side_rows(tried)] == [answered.address]
    assert [a.address for a in side_rows(new)] == [gossiped.address]


def test_the_tried_side_draws_a_row_terrible_by_age_as_select_does() -> None:
    """ISS 1434: Core's `Select_` never calls `IsTerrible`, unlike `GetAddr_`.

    `terrible`'s stamp is 31 days old, past `_ADDRMAN_HORIZON`: excluded
    from a `getaddr` answer but still drawable from the tried side of
    `address_sampler`.
    """
    peer_db = a_peer_db()
    now = int(time.time())
    terrible = peer_address("1.2.3.4", 8333, timestamp=now - 31 * 24 * 3600)
    plant_answered(peer_db, terrible, at=terrible.timestamp)
    tried, _ = cast("Any", peer_db.address_sampler()).args
    assert [a.address for a in side_rows(tried)] == [terrible.address]
    assert peer_db.get_addr(0, 0) == []


@pytest.mark.parametrize(
    "other",
    [peer_address("1.2.3.4", 8334), peer_address("1.2.3.5", 8333)],
    ids=["port", "address"],
)
def test_the_gossiped_side_leaves_out_the_answered_endpoint_alone(
    other: NetworkAddressV2,
) -> None:
    """ISS 1283: an endpoint differing in its port or its address stays.

    The draw compares `endpoint_key`'s three fields rather than the key.
    The network id cannot differ alone between two dialable rows: IPv4
    and IPv6 addresses differ in length.
    """
    peer_db = a_peer_db()
    answered = peer_address("1.2.3.4", 8333)
    peer_db.add_addresses([answered, other])
    peer_db.add_active_address(answered)
    _, new = cast("Any", peer_db.address_sampler()).args
    assert side_rows(new) == [other]


@pytest.mark.parametrize("table", ["answered", "gossiped"])
def test_a_table_holding_nothing_leaves_the_draw_to_the_other(table: str) -> None:
    """ISS 1201: Core searches the only table holding anything, coin or not."""
    peer_db = a_peer_db()
    address = peer_address("1.2.3.4", 8333)
    peer_db.add_addresses([address])
    if table == "answered":
        peer_db.add_active_address(address)
    for _ in range(8):
        drawn = peer_db.random_address()
        assert drawn is not None
        assert drawn.address == address.address


@pytest.mark.parametrize("table", ["known", "answered", "neither"])
def test_a_try_is_recorded_for_an_endpoint_a_table_holds(table: str) -> None:
    """ISS 1277: Core's `Attempt_` sets `m_last_try` on an entry it finds.

    An endpoint neither table holds gets no record, as `Attempt_` bails
    out where addrman does not find the address. An answered endpoint is
    a known one too, `add_active_address` taking no other.
    """
    peer_db = a_peer_db()
    address = peer_address("1.2.3.4", 8333)
    if table != "neither":
        peer_db.add_addresses([address])
    if table == "answered":
        peer_db.add_active_address(address)
    before = time.time()
    peer_db.attempt(address)
    if table == "neither":
        assert peer_db.last_try(address) == 0.0
    else:
        assert peer_db.last_try(address) >= before


def test_a_try_too_old_to_read_is_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    """ISS 1277: a try `_ADDRMAN_REPLACEMENT` old is dropped at the next one."""
    peer_db = a_peer_db()
    old = peer_address("1.2.3.4", 8333)
    new = peer_address("5.6.7.8", 8333)
    peer_db.add_addresses([old, new])
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now - address_module._ADDRMAN_REPLACEMENT)
    peer_db.attempt(old)
    monkeypatch.setattr(time, "time", lambda: now)
    peer_db.attempt(new)
    assert peer_db.last_try(old) == 0.0
    assert peer_db.last_try(new) == now


def test_a_try_does_not_survive_a_restart(tmp_path: Path) -> None:
    """ISS 1277: `peers.dat` does not serialize `m_last_try`, nor does this.

    `AddrInfo`'s `SERIALIZE_METHODS` writes `m_last_success` and
    `nAttempts` beside the address and its source, and not `m_last_try`
    (`src/addrman_impl.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    first = a_peer_db(data_dir=tmp_path)
    address = peer_address("1.2.3.4", 8333)
    first.add_addresses([address])
    first.attempt(address)
    assert first.last_try(address) > 0
    first.close()
    second = a_peer_db(data_dir=tmp_path)
    assert second.addresses == {address}
    assert second.last_try(address) == 0.0
    second.close()


def test_a_known_address_survives_a_restart(tmp_path: Path) -> None:
    """A gossiped address written before `close` is read back after restart.

    A second `PeerDB` opened on the same `data_dir` as the first, once
    the first has closed, sees exactly the address the first one added.
    """
    first = a_peer_db(data_dir=tmp_path)
    first.add_addresses([peer_address("1.2.3.4", 8333)])
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.addresses == {peer_address("1.2.3.4", 8333)}
    second.close()


def test_an_address_that_answered_survives_a_restart_and_is_drawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which address answered survives a restart, and is drawn as answered.

    Across a `close` and a fresh `PeerDB` on the same store: the
    active-address record is durable, not only a hint kept for the run
    that made it.
    """
    first = a_peer_db(data_dir=tmp_path)
    now = int(time.time())
    answered = peer_address("1.2.3.4", 8333, timestamp=now)
    unconfirmed = peer_address("5.6.7.8", 8333, timestamp=now)
    first.add_addresses([answered, unconfirmed], time_penalty=0)
    first.add_active_address(answered)
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.addresses == {answered, unconfirmed}
    # the coin fixed to the answered table, which only `answered` is in
    monkeypatch.setattr(secrets, "randbelow", lambda n: 1)
    drawn = second.random_address()
    assert drawn is not None
    assert drawn.address == answered.address
    assert drawn.port == answered.port
    second.close()


def test_a_fresh_store_has_size_zero(tmp_path: Path) -> None:
    """A brand-new store starts out at Core's own `addrman.Size()` of 0."""
    peer_db = a_peer_db(data_dir=tmp_path)
    assert peer_db.size == 0
    peer_db.close()


def test_size_counts_unconfirmed_gossip_too(tmp_path: Path) -> None:
    """`size` counts every known address, confirmed or not, as `Size()` does.

    A restart with nothing but gossip, none of it confirmed, is not an
    empty table: `P2pManager._dns_address_seed` waits between batches
    of seeds rather than asking every one of them at once
    (btclib-org/btclib-node#1265).
    """
    first = a_peer_db(data_dir=tmp_path)
    first.add_addresses([peer_address("1.2.3.4", 8333)])
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.size == 1
    second.close()


def test_size_counts_a_gossiped_and_answered_address_once_each(
    tmp_path: Path,
) -> None:
    """One endpoint, gossiped and confirmed, is one row of `size`, not two.

    Both the known row and the answered row are read back after a
    restart, and `size` counts the endpoint once, as Core's
    `addrman.Size()` counts a tried entry once and never as a new one
    too (`AddrManImpl::Good_` moves it rather than duplicating it,
    `src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    This tree's own two-table split leaves the row in both `addresses`
    and `active_addresses` (`address_sampler`'s own docstring says so),
    so counting the union of their keys, not the sum of their lengths,
    is what keeps this test's name true (ISS 1265: a `bitcoind` this
    node has actually handshaken with, previously double-counted here,
    is what pushed `PeerDB.size` past `_DNS_SEEDS_DELAY_PEER_THRESHOLD`
    twice as fast as Core's own table would).
    """
    first = a_peer_db(data_dir=tmp_path)
    answered = peer_address("1.2.3.4", 8333)
    first.add_addresses([answered])
    first.add_active_address(answered)
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.size == 1
    second.close()


def test_size_stays_put_across_a_handshake_with_a_gossiped_endpoint(
    tmp_path: Path,
) -> None:
    """A handshake with an already-gossiped endpoint does not grow `size`.

    One `PeerDB`, no restart: gossip through `add_addresses`, read
    `size`, then a handshake through `add_active_address` for the same
    endpoint, read `size` again. Core's `addrman.Size()` does not grow
    across `Good_` either -- it moves the entry from the new table to
    the tried one rather than adding a second (`AddrManImpl::Good_`,
    `src/addrman.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    peer_db = a_peer_db(data_dir=tmp_path)
    address = peer_address("1.2.3.4", 8333)
    peer_db.add_addresses([address])
    assert peer_db.size == 1
    peer_db.add_active_address(address)
    assert peer_db.size == 1
    peer_db.close()


def test_a_store_answered_a_day_ago_keeps_its_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1318: a node down for a day restarts with its answered rows.

    Core's tried table survives a restart of any length under
    `ADDRMAN_HORIZON`, so `size` still counts the row.
    """
    first = a_peer_db(data_dir=tmp_path)
    a_day_ago = time.time() - 24 * 3600
    answered = peer_address("1.2.3.4", 8333, timestamp=int(a_day_ago))
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: a_day_ago)
        first.add_addresses([answered], time_penalty=0)
        first.add_active_address(answered)
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.size == 1
    assert [a.address for a in second.active_addresses] == [answered.address]
    second.close()


def test_closing_a_peer_db_with_no_store_does_nothing() -> None:
    """`close` on an in-memory `PeerDB`, with no `db`, does not raise."""
    a_peer_db().close()


def test_a_key_this_version_does_not_know_is_left_where_it_is(tmp_path: Path) -> None:
    """A key under neither prefix this version knows is read past, not lost.

    A row is written directly under a key `init_from_db` does not
    recognise, standing in for one another version of this store might
    have written; this checks it is stepped over rather than filed
    under either of the two known prefixes or raising on load.
    """
    first = a_peer_db(data_dir=tmp_path)
    first.add_addresses([peer_address("1.2.3.4", 8333)])
    assert first.db is not None
    first.db.put(b"z", b"from some other version of this store")
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    # stepped over rather than filed under either of the two prefixes
    # the store knows
    assert second.addresses == {peer_address("1.2.3.4", 8333)}
    assert second.active_addresses == []
    second.close()


_UNSTORABLE = [
    pytest.param(peer_address("127.0.0.1", 18444), id="loopback"),
    pytest.param(peer_address("192.168.1.1", 8333), id="rfc1918"),
    pytest.param(peer_address("::1", 8333), id="ipv6 loopback"),
    pytest.param(peer_address(_AN_ONIONCAT_ADDRESS, 8333), id="embedded torv2"),
]


@pytest.mark.parametrize("address", _UNSTORABLE)
def test_an_unroutable_peer_is_not_recorded_as_answered(
    address: NetworkAddressV2,
) -> None:
    """ISS 1140: Core's `AddrManImpl::Good_` updates only what addrman holds.

    `AddSingle` never holds an unroutable address, so an answered
    handshake with one is not recorded, and `random_address` and
    `getaddr` never read it back. A routable peer beside it is the
    control.
    """
    peer_db = a_peer_db()
    routable = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    peer_db.add_addresses([address, routable], time_penalty=0)
    peer_db.add_active_address(address)
    peer_db.add_active_address(routable)
    assert [a.address for a in peer_db.active_addresses] == [routable.address]


@pytest.mark.parametrize("prefix", [b"known-", b"answered-"])
@pytest.mark.parametrize("address", _UNSTORABLE)
def test_an_unroutable_row_is_dropped_from_the_store_on_load(
    tmp_path: Path, prefix: bytes, address: NetworkAddressV2
) -> None:
    """ISS 1140: a row neither table would take now is deleted, not loaded.

    Written directly under the prefix, as a store filled before the
    tables refused such an address holds it. A routable row beside it
    is loaded and kept.
    """
    now = int(time.time())
    routable = peer_address("1.2.3.4", 8333, timestamp=now)
    first = a_peer_db(data_dir=tmp_path)
    assert first.db is not None
    for row in (address, routable):
        stored = NetworkAddressV2(
            now, row.services, row.network_id, row.address, row.port
        )
        key = address_module.endpoint_key(stored)
        first.db.put(prefix + key, stored.serialize(check_validity=False))
    # an answered row is kept only beside a known one (ISS 1189)
    kept = {prefix + address_module.endpoint_key(routable)}
    if prefix == b"answered-":
        known_key = b"known-" + address_module.endpoint_key(routable)
        first.db.put(known_key, routable.serialize(check_validity=False))
        kept.add(known_key)
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    loaded = second.addresses if prefix == b"known-" else second.active_addresses
    assert [a.address for a in loaded] == [routable.address]
    assert table_keys(second) == kept
    second.close()


def test_an_answered_endpoint_not_gossiped_is_not_recorded() -> None:
    """ISS 1189: Core's `AddrManImpl::Good_` updates only what addrman holds.

    `Good_` returns at once where `Find` has no entry for the address,
    so a peer answering a handshake is not added to the tried table on
    that alone. A gossiped endpoint beside it is the control, and its
    record differing in `services` from the gossip shows the match is
    on the endpoint rather than on the whole record.
    """
    peer_db = a_peer_db()
    stranger = peer_address("1.2.3.4", 8333)
    gossiped = peer_address("5.6.7.8", 8333, timestamp=int(time.time()))
    peer_db.add_addresses([gossiped], time_penalty=0)
    peer_db.add_active_address(stranger)
    peer_db.add_active_address(peer_address("5.6.7.8", 8333, services=1))
    assert [a.address for a in peer_db.active_addresses] == [gossiped.address]


def test_an_answered_row_with_no_known_row_is_dropped_on_load(
    tmp_path: Path,
) -> None:
    """ISS 1189: a store holding an answered row alone loses it on load.

    Written directly, as a store filled before `add_active_address`
    asked for a known endpoint holds it. An answered row beside a known
    one for the same endpoint is the control, and it is kept.
    """
    first = a_peer_db(data_dir=tmp_path)
    assert first.db is not None
    now = int(time.time())
    orphan = peer_address("1.2.3.4", 8333, timestamp=now)
    held = peer_address("5.6.7.8", 8333, timestamp=now)
    for key, row in (
        (b"answered-", orphan),
        (b"answered-", held),
        (b"known-", held),
    ):
        first.db.put(key + address_module.endpoint_key(row), row.serialize())
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert [a.address for a in second.active_addresses] == [held.address]
    assert table_keys(second) == {
        b"answered-" + address_module.endpoint_key(held),
        b"known-" + address_module.endpoint_key(held),
    }
    second.close()


def test_a_fixed_seed_decodes_as_cores_convert_seeds_reads_it() -> None:
    """ISS 1099: network id, compact-size length, address, big-endian port.

    The first line of Core's `nodes_main.txt` at
    bitcoin/bitcoin@9be056a8a7, a CJDNS address, then an IPv4 one on a
    port of its own, serialized as `generate_seeds.py` serializes them;
    each gets Core's `SeedsServiceFlags`. The timestamp each is also
    given (issue #1571) is asserted separately below, this one pinned at
    0 first to isolate the rest of the decode from it.
    """
    seeds = bytes.fromhex(
        "0610fc11f76916e6361158ae1d4afcf757a4208d"  # [fc11:...:57a4]:8333
        "0104010203042382"  # 1.2.3.4:9090
    )
    cjdns, ipv4 = fixed_seed_addresses(seeds)
    assert cjdns.network_id == BIP155Network.CJDNS
    assert cjdns.address == bytes.fromhex("fc11f76916e6361158ae1d4afcf757a4")
    assert cjdns.port == 8333
    assert replace(ipv4, timestamp=0) == peer_address(
        "1.2.3.4", 9090, services=ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS
    )


def test_a_fixed_seed_is_backdated_one_to_two_weeks() -> None:
    """#1571: Core's `ConvertSeeds` gives each seed a random past time.

    `rng.rand_uniform_delay(Now<NodeSeconds>() - one_week, -one_week)`
    (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) draws
    uniform over `[now - 14d, now - 7d]`; measured around the call
    rather than pinned, since the draw is a real one and not faked here.
    """
    seeds = bytes.fromhex("0104010203042382")  # 1.2.3.4:9090
    before = time.time()
    (ipv4,) = fixed_seed_addresses(seeds)
    after = time.time()
    age = after - ipv4.timestamp
    # +1 for `int()`'s own truncation of the draw to a whole second, on
    # top of the wall-clock slack between `before` and `after`.
    slack = (after - before) + 1
    assert (
        address_module._FIXED_SEED_MIN_AGE
        <= age
        <= address_module._FIXED_SEED_MAX_AGE + slack
    )


@pytest.mark.parametrize("table", ["addresses", "active_addresses"])
def test_either_table_holding_a_network_holds_it(table: str) -> None:
    """ISS 1099: Core's `addrman.Size(net)` counts new and tried alike.

    `add_active_address` records only an endpoint `addresses` holds
    (ISS 1189), so the answered table is asked alone here by writing the
    endpoint straight into `_known_keys`, past `addresses`.
    """
    peer_db = a_peer_db()
    assert not peer_db.holds_network(BIP155Network.IPV6)
    held = peer_address("2a01:4f8::1", 8333)
    if table == "addresses":
        peer_db.add_addresses([held])
    else:
        peer_db.add_addresses([held])
        peer_db.add_active_address(held)
        peer_db.addresses.clear()
    assert peer_db.holds_network(BIP155Network.IPV6)
    assert not peer_db.holds_network(BIP155Network.IPV4)


def test_a_feeler_draws_what_the_answered_table_does_not_hold() -> None:
    """ISS 1096: Core's `Select(true, ...)`, the new table alone.

    An address answered, whatever timestamp the gossiped copy carries,
    is not drawn, nor is one this node cannot dial; once every dialable
    one is answered there is nothing to draw. The control, the sampler
    without `new_only`, draws the answered address as well.
    """
    peer_db = a_peer_db()
    answered = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    new = peer_address("5.6.7.8", 8333)
    peer_db.add_addresses([replace(answered, timestamp=1), new, an_onion_address()])
    peer_db.add_active_address(answered)
    draw = peer_db.address_sampler(new_only=True)
    assert {draw() for _ in range(40)} == {new}
    both = peer_db.address_sampler()
    drawn = {address_module.endpoint_key(cast("Any", both())) for _ in range(80)}
    assert drawn == {address_module.endpoint_key(a) for a in (answered, new)}
    peer_db.add_active_address(replace(new, timestamp=int(time.time())))
    assert peer_db.address_sampler(new_only=True)() is None


def test_an_extra_network_peer_draws_on_its_network_alone() -> None:
    """ISS 1100: Core's `Select(false, {network})`, over both tables.

    For IPv6 an IPv6 address is drawn, answered or only gossiped, the
    coin deciding between the two tables as `_select` does; nothing is
    drawn for a network the table holds nothing on.
    """
    peer_db = a_peer_db()
    v4 = peer_address("1.2.3.4", 8333)
    v6 = peer_address("2a00::1", 8333)
    answered = peer_address("2a00::2", 8333, timestamp=int(time.time()))
    peer_db.add_addresses([v4, v6, replace(answered, timestamp=1), an_onion_address()])

    def draws(network: Network) -> set[bytes]:
        draw = peer_db.address_sampler(network=network)
        return {cast("NetworkAddressV2", draw()).address for _ in range(80)}

    assert draws(Network.IPV6) == {v6.address, answered.address}
    peer_db.add_active_address(answered)
    assert draws(Network.IPV6) == {v6.address, answered.address}
    assert draws(Network.IPV4) == {v4.address}
    assert peer_db.address_sampler(network=Network.ONION)() is None


def test_a_draw_on_no_network_never_calls_get_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1100: `get_network` runs only where a network is asked for.

    Every dial pass walks both tables under their locks, so a draw on
    no network is left the walk it had before; asked for a network, the
    same table reaches it.
    """
    peer_db = a_peer_db()
    answered = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    peer_db.add_addresses(
        [replace(answered, timestamp=1), peer_address("5.6.7.8", 8333)]
    )
    peer_db.add_active_address(answered)
    asked: list[NetworkAddressV2] = []

    def get_network(address: NetworkAddressV2) -> Network:
        asked.append(address)
        return Network.IPV4

    monkeypatch.setattr(address_module, "get_network", get_network)
    assert peer_db.address_sampler()() is not None
    assert asked == []
    assert peer_db.address_sampler(network=Network.IPV4)() is not None
    assert asked


def sha256d(data: bytes) -> bytes:
    """Return the double SHA-256 `HashWriter::GetHash` is."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def test_the_buckets_are_those_cores_own_tests_expect() -> None:
    """The hashes are Core's: `addrman_tests.cpp`'s `caddrinfo_get_*_bucket`.

    With `nKey1 = (HashWriter{} << 1).GetHash()`, tried bucket 40 for
    250.1.1.1:8333 and new bucket 786 for 250.1.2.1:8333 from a source of
    its own address (`src/test/addrman_tests.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), and a different key
    puts them elsewhere.
    """
    key1 = sha256d((1).to_bytes(4, "little"))
    key2 = sha256d((2).to_bytes(4, "little"))
    tried = peer_address("250.1.1.1", 8333)
    new = peer_address("250.1.2.1", 8333)
    assert address_module._tried_slot(key1, tried)[0] == 40
    assert address_module._new_slot(key1, new, net_group(new))[0] == 786
    assert address_module._tried_slot(key2, tried)[0] != 40
    assert address_module._new_slot(key2, new, net_group(new))[0] != 786


def test_an_address_gossiped_again_may_take_a_further_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AddSingle`: a newer gossip, at odds of one to two to the slots held."""
    peer_db = a_peer_db()
    now = int(time.time())
    address = peer_address("1.2.3.4", 8333, timestamp=now - 7200)
    peer_db.add_addresses([address], source=peer_address("9.9.9.9", 1))
    endpoint = address_module._endpoint(address)
    newer = replace(address, timestamp=now)
    # the odds do not come up
    peer_db.add_addresses([newer], source=peer_address("8.8.8.8", 1))
    assert len(peer_db._slots[endpoint]) == 1
    monkeypatch.setattr(address_module, "_roll", lambda _factor: True)
    # an older gossip does not even draw
    peer_db.add_addresses([address], source=peer_address("7.7.7.7", 1))
    assert len(peer_db._slots[endpoint]) == 1
    for octet in range(10, 40):
        peer_db.add_addresses([newer], source=peer_address(f"{octet}.1.1.1", 1))
        newer = replace(newer, timestamp=newer.timestamp + 1)
    assert 1 < len(peer_db._slots[endpoint]) <= 8
    assert len(peer_db.addresses) == 1


def test_a_slot_of_an_address_held_twice_goes_to_one_held_nowhere() -> None:
    """`AddSingle` overwrites a holder of other buckets, for a stranger."""
    peer_db = a_peer_db()
    now = int(time.time())
    source = peer_address("9.9.9.9", 8333)
    held = peer_address("1.2.3.4", 8333, timestamp=now)
    peer_db.add_addresses([held], source=source, time_penalty=0)
    endpoint = address_module._endpoint(held)
    # a second bucket for it, from another source group
    group = net_group(peer_address("8.8.8.8", 1))
    peer_db._insert_new(held, group, time.time(), None)
    assert len(peer_db._slots[endpoint]) == 2
    stranger = a_colliding_address(peer_db, held, source)
    assert peer_db.add_addresses([stranger], source=source) == 1
    assert len(peer_db._slots[endpoint]) == 1
    # held once, it keeps its slot against the next newcomer
    other = next(
        o
        for o in (
            replace(stranger, port=port) for port in range(stranger.port + 1, 60000)
        )
        if address_module._new_slot(peer_db._bucket_key, o, net_group(source))
        == address_module._new_slot(peer_db._bucket_key, stranger, net_group(source))
    )
    assert peer_db.add_addresses([other], source=source) == 0


def a_tried_collision(peer_db: PeerDB, held: NetworkAddressV2) -> NetworkAddressV2:
    """Return an address that maps to `held`'s slot of the tried table."""
    slot = address_module._tried_slot(peer_db._bucket_key, held)
    return next(
        other
        for other in (replace(held, port=port) for port in range(held.port + 1, 60000))
        if address_module._tried_slot(peer_db._bucket_key, other) == slot
    )


def a_waiting_pair(peer_db: PeerDB) -> tuple[NetworkAddressV2, NetworkAddressV2]:
    """Answer one address, and let another that maps to its slot wait for it."""
    old = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    waiting = a_tried_collision(peer_db, old)
    peer_db.add_addresses([old, waiting], time_penalty=0)
    assert peer_db.add_active_address(old)
    assert not peer_db.add_active_address(waiting)
    return old, waiting


def test_an_answered_address_leaves_the_new_table_for_the_tried_one() -> None:
    """`Good_`: out of every bucket of the new table, into its tried slot."""
    peer_db = a_peer_db()
    address = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    peer_db.add_addresses([address], time_penalty=0)
    endpoint = address_module._endpoint(address)
    assert peer_db.add_active_address(address)
    assert endpoint not in peer_db._slots
    assert endpoint in peer_db._tried_endpoints
    assert peer_db._tried_occupant == {
        address_module._tried_slot(peer_db._bucket_key, address): (
            address_module.endpoint_key(address)
        )
    }
    assert not peer_db.add_active_address(address)
    assert len(peer_db.active_addresses) == 1


def test_an_entry_evicted_from_the_tried_table_goes_back_to_the_new_one() -> None:
    """`MakeTried`: the evicted entry takes its slot of the new table again."""
    peer_db = a_peer_db()
    old = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    other = a_tried_collision(peer_db, old)
    peer_db.add_addresses([old, other], time_penalty=0)
    assert peer_db.add_active_address(old, test_before_evict=False)
    assert peer_db.add_active_address(other, test_before_evict=False)
    assert [a.port for a in peer_db.active_addresses] == [other.port]
    assert address_module._endpoint(old) in peer_db._slots
    assert address_module._endpoint(old) not in peer_db._tried_endpoints
    assert old in peer_db.addresses


def test_an_entry_that_would_evict_waits_for_a_test_of_the_old_one() -> None:
    """`Good` with `test_before_evict` keeps a collision, acting on none."""
    peer_db = a_peer_db()
    old, waiting = a_waiting_pair(peer_db)
    assert peer_db.active_addresses == [
        replace(old, timestamp=peer_db.active_addresses[0].timestamp)
    ]
    assert peer_db._collisions == {address_module._endpoint(waiting)}
    assert peer_db.select_tried_collision() == peer_db.active_addresses[0]


def test_no_collision_names_no_address_to_test() -> None:
    """`SelectTriedCollision` names nothing where nothing waits."""
    peer_db = a_peer_db()
    assert peer_db.select_tried_collision() is None
    old, waiting = a_waiting_pair(peer_db)
    with peer_db._addresses_lock:
        del peer_db._rows[address_module._endpoint(waiting)]
    assert peer_db.select_tried_collision() is None
    assert peer_db._collisions == set()
    peer_db._collisions.add(address_module._endpoint(old))
    peer_db._tried_remove(address_module.endpoint_key(old))
    assert peer_db.select_tried_collision() is None


def test_only_so_many_collisions_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ADDRMAN_SET_TRIED_COLLISION_SIZE`: past it a collision is dropped."""
    monkeypatch.setattr(address_module, "_SET_TRIED_COLLISION_SIZE", 1)
    peer_db = a_peer_db()
    old = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    slot = address_module._tried_slot(peer_db._bucket_key, old)
    waiting = [
        a
        for a in (replace(old, port=port) for port in range(1, 60000))
        if address_module._tried_slot(peer_db._bucket_key, a) == slot
    ]
    assert len(waiting) >= 3
    peer_db.add_addresses([old, *waiting], time_penalty=0)
    assert peer_db.add_active_address(waiting[0])
    for address in waiting[1:]:
        assert not peer_db.add_active_address(address)
    assert len(peer_db._collisions) == 1


@pytest.mark.parametrize(
    ("old_success", "old_try", "waiting_success", "replaced", "settled"),
    [
        pytest.param(60, None, 7200, False, True, id="old-answered-recently"),
        pytest.param(None, 600, 7200, True, True, id="old-tried-and-failed"),
        pytest.param(None, 30, 7200, False, False, id="old-tried-just-now"),
        pytest.param(
            None, None, 7200, True, True, id="old-idle-and-new-answered-long-ago"
        ),
        pytest.param(None, None, 600, False, False, id="old-idle-and-new-too-recent"),
    ],
)
def test_a_collision_is_resolved_as_core_does(
    *,
    old_success: int | None,
    old_try: int | None,
    waiting_success: int,
    replaced: bool,
    settled: bool,
) -> None:
    """`ResolveCollisions_`: the old entry stays, or the waiting one wins."""
    peer_db = a_peer_db()
    old, waiting = a_waiting_pair(peer_db)
    now = time.time()
    old_endpoint = address_module._endpoint(old)
    waiting_endpoint = address_module._endpoint(waiting)
    peer_db._stats[old_endpoint].last_success = now - (old_success or 86400)
    peer_db._stats[waiting_endpoint].last_success = now - waiting_success
    peer_db._last_try.pop(address_module.endpoint_key(old), None)
    if old_try is not None:
        peer_db._last_try[address_module.endpoint_key(old)] = now - old_try
    peer_db.resolve_collisions()
    assert (waiting_endpoint in peer_db._tried_endpoints) is replaced
    assert (old_endpoint in peer_db._tried_endpoints) is not replaced
    assert (waiting_endpoint not in peer_db._collisions) is settled


def test_a_collision_with_a_vanished_old_entry_or_waiting_one_is_dropped() -> None:
    """The slot free, or the waiting endpoint gone: nothing left to wait for."""
    peer_db = a_peer_db()
    old, waiting = a_waiting_pair(peer_db)
    peer_db._tried_remove(address_module.endpoint_key(old))
    peer_db.resolve_collisions()
    assert address_module._endpoint(waiting) in peer_db._tried_endpoints
    peer_db = a_peer_db()
    old, waiting = a_waiting_pair(peer_db)
    with peer_db._addresses_lock:
        del peer_db._rows[address_module._endpoint(waiting)]
    peer_db.resolve_collisions()
    assert peer_db._collisions == set()


@pytest.mark.parametrize(
    ("since_try", "attempts", "expected"),
    [
        pytest.param(3600, 0, 1.0, id="idle"),
        pytest.param(60, 0, 0.01, id="tried-just-now"),
        pytest.param(3600, 2, 0.66**2, id="two-failures"),
        pytest.param(3600, 20, 0.66**8, id="failures-are-capped-at-eight"),
    ],
)
def test_the_chance_of_a_draw_is_cores(
    since_try: int, attempts: int, expected: float
) -> None:
    """`AddrInfo::GetChance`."""
    now = 1_000_000.0
    assert address_module._chance(now, now - since_try, attempts) == pytest.approx(
        expected
    )


@pytest.mark.parametrize(
    ("attempts", "last_success_ago", "terrible"),
    [
        pytest.param(3, None, True, id="three-tries-and-never-answered"),
        pytest.param(2, None, False, id="two-tries"),
        pytest.param(10, 8 * 86400, True, id="ten-failures-over-a-week"),
        pytest.param(10, 6 * 86400, False, id="ten-failures-within-a-week"),
        pytest.param(9, 8 * 86400, False, id="nine-failures"),
    ],
)
def test_failed_attempts_make_an_address_terrible_as_core_counts(
    attempts: int, last_success_ago: int | None, *, terrible: bool
) -> None:
    """`IsTerrible`'s `ADDRMAN_RETRIES` and `ADDRMAN_MAX_FAILURES` tests."""
    now = time.time()
    address = peer_address("1.2.3.4", 8333, timestamp=int(now))
    stats = address_module._Stats(
        last_success=0.0 if last_success_ago is None else now - last_success_ago,
        attempts=attempts,
    )
    assert address_module._aged_out(address, now, 0.0, stats) is terrible


def test_a_failed_attempt_is_counted_once_between_two_answers() -> None:
    """`Attempt_`: `fCountFailure` counts once for each `Good_`."""
    peer_db = a_peer_db()
    address = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    peer_db.add_addresses([address], time_penalty=0)
    endpoint = address_module._endpoint(address)
    # `m_last_good` starts at one, so a first failure is counted
    assert peer_db._last_good == 1.0
    peer_db.attempt(address, count_failure=True)
    peer_db.attempt(address, count_failure=True)
    peer_db.attempt(address)
    assert peer_db._stats[endpoint].attempts == 1
    peer_db.add_active_address(address)
    assert peer_db._stats[endpoint].attempts == 0
    peer_db._stats[endpoint].last_count_attempt = peer_db._last_good - 1
    peer_db.attempt(address, count_failure=True)
    assert peer_db._stats[endpoint].attempts == 1


def test_an_answer_from_an_unknown_endpoint_still_moves_last_good() -> None:
    """`Good_` sets `m_last_good` ahead of its `Find`, so a miss moves it."""
    peer_db = a_peer_db()
    stranger = peer_address("9.9.9.9", 8333)
    assert not peer_db.add_active_address(stranger)
    assert peer_db._last_good > 1.0
    assert address_module._endpoint(stranger) not in peer_db._tried_endpoints


def test_a_slot_taken_over_leaves_the_first_source_of_its_holder() -> None:
    """Core's `AddrInfo::source` never changes, whatever buckets are lost."""
    peer_db = a_peer_db()
    now = int(time.time())
    first_source = peer_address("9.9.9.9", 1)
    held = peer_address("1.2.3.4", 8333, timestamp=now)
    peer_db.add_addresses([held], source=first_source, time_penalty=0)
    endpoint = address_module._endpoint(held)
    second_group = net_group(peer_address("8.8.8.8", 1))
    peer_db._insert_new(held, second_group, time.time(), None)
    stranger = a_colliding_address(peer_db, held, first_source)
    assert peer_db.add_addresses([stranger], source=first_source) == 1
    assert list(peer_db._slots[endpoint].values()) == [second_group]
    assert peer_db._source[endpoint] == net_group(first_source)


def test_the_draw_keeps_an_address_by_its_chance() -> None:
    """`Select_` keeps an address with the probability `GetChance` gives."""
    peer_db = a_peer_db()
    idle = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    tried = peer_address("5.6.7.8", 8333, timestamp=int(time.time()))
    peer_db.add_addresses(
        [idle, tried], time_penalty=0, source=peer_address("9.9.9.9", 1)
    )
    peer_db.attempt(tried)
    draw = peer_db.address_sampler(new_only=True)
    drawn = [draw() for _ in range(300)]
    assert sum(1 for a in drawn if a == idle) > 250


def test_a_stored_table_comes_back_with_its_tried_entries_and_counts(
    tmp_path: Path,
) -> None:
    """The tried table, sources, attempts and key survive a restart."""
    first = a_peer_db(data_dir=tmp_path)
    now = int(time.time())
    kept = peer_address("1.2.3.4", 8333, timestamp=now)
    failing = peer_address("5.6.7.8", 8333, timestamp=now)
    first.add_addresses(
        [kept, failing], time_penalty=0, source=peer_address("9.9.9.9", 1)
    )
    first.add_active_address(kept)
    first.add_active_address(peer_address("1.2.3.4", 8333, timestamp=now))
    first.attempt(failing, count_failure=True)
    first._stats[address_module._endpoint(failing)].last_count_attempt = 0
    first.attempt(failing, count_failure=True)
    slots = slots_of(first)
    attempts = first._stats[address_module._endpoint(failing)].attempts
    first.close()
    second = a_peer_db(data_dir=tmp_path)
    assert slots_of(second) == slots
    assert [a.port for a in second.active_addresses] == [8333]
    assert second._stats[address_module._endpoint(failing)].attempts == attempts
    assert second._bucket_key == first._bucket_key
    assert second._source == first._source
    second.close()


def test_gossip_through_the_source_that_gave_a_slot_takes_no_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A further bucket drawn to the slot the endpoint holds adds nothing."""
    peer_db = a_peer_db()
    now = int(time.time())
    source = peer_address("9.9.9.9", 1)
    address = peer_address("1.2.3.4", 8333, timestamp=now - 7200)
    peer_db.add_addresses([address], source=source)
    monkeypatch.setattr(address_module, "_roll", lambda _factor: True)
    again = replace(address, timestamp=now)
    assert peer_db.add_addresses([again], source=source) == 0
    assert len(peer_db._slots[address_module._endpoint(address)]) == 1


def test_an_evicted_entry_takes_back_its_slot_of_the_new_table(
    tmp_path: Path,
) -> None:
    """`MakeTried` clears what holds the slot, and the store follows."""
    peer_db = a_peer_db(data_dir=tmp_path)
    now = int(time.time())
    source = peer_address("9.9.9.9", 1)
    old = peer_address("1.2.3.4", 8333, timestamp=now)
    other = a_tried_collision(peer_db, old)
    peer_db.add_addresses([old, other], source=source, time_penalty=0)
    peer_db.add_active_address(old, test_before_evict=False)
    stranger = a_colliding_address(peer_db, old, source)
    assert peer_db.add_addresses([stranger], source=source, time_penalty=0) == 1
    peer_db.add_active_address(other, test_before_evict=False)
    assert old in peer_db.addresses
    assert stranger not in peer_db.addresses
    peer_db.close()
    again = a_peer_db(data_dir=tmp_path)
    assert [a.port for a in again.active_addresses] == [other.port]
    assert old in again.addresses
    assert stranger not in again.addresses
    again.close()


def test_a_position_is_the_hash_core_takes_of_the_bucket_and_the_endpoint() -> None:
    """`GetBucketPosition`: `N` or `K`, the bucket, and the endpoint's key."""
    key = sha256d((1).to_bytes(4, "little"))
    # `GetAddrBytes` gives an IPv4 address mapped into sixteen octets
    octets = b"\x00" * 10 + b"\xff\xff" + bytes([250, 1, 1, 1])
    octets += (8333).to_bytes(2, "big")
    for tag in (b"N", b"K"):
        digest = sha256d(
            key + tag + (7).to_bytes(4, "little") + bytes([len(octets)]) + octets
        )
        assert address_module._position(key, tag, 7, octets) == (
            int.from_bytes(digest[:8], "little") % 64
        )


def test_a_draw_grows_its_acceptance_with_every_address_it_declines() -> None:
    """`Select_`: the factor grows by a fifth, so a rare address is reached."""
    peer_db = a_peer_db()
    address = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    peer_db.add_addresses([address], time_penalty=0)
    peer_db.attempt(address)
    calls: list[None] = []

    def random() -> float:
        calls.append(None)
        assert len(calls) < 100
        return 0.5

    draw = peer_db.address_sampler()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(address_module, "_RNG", SimpleNamespace(random=random))
        assert draw() == address
    # 1.2 ** 22 * 0.01 is the first to pass one half
    assert len(calls) == 23


def test_a_holder_of_several_slots_keeps_them_on_load(tmp_path: Path) -> None:
    """`Unserialize` gives a slot to the first row that asks for it."""
    first = a_peer_db(data_dir=tmp_path)
    now = int(time.time())
    source = peer_address("9.9.9.9", 1)
    held = peer_address("1.2.3.4", 8333, timestamp=now)
    first.add_addresses([held], source=source, time_penalty=0)
    first._insert_new(held, net_group(peer_address("8.8.8.8", 1)), time.time(), None)
    first._put_sources(address_module._endpoint(held), first.db)
    stranger = a_colliding_address(first, held, source)
    assert first.db is not None
    first.db.put(
        b"known-" + address_module.endpoint_key(stranger),
        stranger.serialize(check_validity=False),
    )
    first.db.put(
        b"source-" + address_module.endpoint_key(stranger), packed(net_group(source))
    )
    first.close()
    second = a_peer_db(data_dir=tmp_path)
    assert second.addresses == {held}
    assert len(second._slots[address_module._endpoint(held)]) == 2
    second.close()


def test_the_odds_of_a_further_bucket_are_a_draw_of_one_in_a_power_of_two() -> None:
    """`_roll(factor)` is `randrange(factor) == 0`."""
    draws: list[int] = []

    def randrange(factor: int) -> int:
        draws.append(factor)
        return 0

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(address_module, "_RNG", SimpleNamespace(randrange=randrange))
        assert _REAL_ROLL(4)
    assert draws == [4]


def test_a_tried_row_whose_slot_is_held_goes_to_the_new_table_on_load(
    tmp_path: Path,
) -> None:
    """`MakeTried` sends a displaced entry to its first source's slot."""
    first = a_peer_db(data_dir=tmp_path)
    now = int(time.time())
    source = peer_address("9.9.9.9", 1)
    old = peer_address("1.2.3.4", 8333, timestamp=now)
    other = a_tried_collision(first, old)
    first.add_addresses([old, other], source=source, time_penalty=0)
    first.add_active_address(old, test_before_evict=False)
    assert first.db is not None
    suffix = address_module.endpoint_key(other)
    first.db.put(b"answered-" + suffix, other.serialize(check_validity=False))
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.addresses == {old, other}
    assert [a.port for a in second.active_addresses] == [old.port]
    endpoint = address_module._endpoint(other)
    slot = address_module._new_slot(second._bucket_key, other, net_group(source))
    assert second._slots[endpoint] == {slot: net_group(source)}
    assert endpoint not in second._tried_endpoints
    assert second.db is not None
    assert b"answered-" + suffix not in {key for key, _ in second.db}
    second.close()

    third = a_peer_db(data_dir=tmp_path)
    assert third._stats[endpoint].last_success == other.timestamp
    third.close()


def test_a_stored_row_takes_no_more_slots_than_core_allows(tmp_path: Path) -> None:
    """`Unserialize` stops at `ADDRMAN_NEW_BUCKETS_PER_ADDRESS` buckets."""
    first = a_peer_db(data_dir=tmp_path)
    address = peer_address("1.2.3.4", 8333, timestamp=int(time.time()))
    first.add_addresses([address], time_penalty=0)
    assert first.db is not None
    groups = [net_group(peer_address(f"{octet}.1.1.1", 1)) for octet in range(20, 32)]
    first.db.put(
        b"source-" + address_module.endpoint_key(address),
        b"".join(packed(group) for group in groups),
    )
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert len(second._slots[address_module._endpoint(address)]) == 8
    second.close()
