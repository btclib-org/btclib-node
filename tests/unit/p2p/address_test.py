# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from btclib.p2p.address import NetworkAddress
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2

from btclib_node.chains import Chain
from btclib_node.p2p.address import (
    PeerDB,
    addr_entry,
    can_addrv1,
    can_connect,
    dial,
    ip_and_port,
    peer_address,
    peer_from_addr_entry,
)
from tests.helpers import call_within

# BIP155's own examples: an IPv6-mapped IPv4 host, and an address under
# OnionCat's `fd87:d87e:eb43::/48`, once how a TORv2 address was carried
# inside a fake IPv6 one.
_A_V4_MAPPED_ADDRESS = "::ffff:1.2.3.4"
_AN_ONIONCAT_ADDRESS = "fd87:d87e:eb43::1"


def a_peer_db(chain: Any = None, data_dir: Path | None = None) -> PeerDB:
    return PeerDB(cast(Chain, chain), data_dir)


def an_onion_address(port: int = 8333) -> NetworkAddressV2:
    return NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, port)


def a_cjdns_address(port: int = 8333) -> NetworkAddressV2:
    # sixteen octets, which is what makes it the interesting one: an
    # onion address is refused by its length wherever an IP address is
    # wanted, and this is not
    return NetworkAddressV2(0, 0, BIP155Network.CJDNS, b"\xfc" + b"\x11" * 15, port)


def test_an_address_just_seen_is_active_and_can_be_sent() -> None:
    peer_db = a_peer_db()
    peer_db.add_active_address(peer_address("1.2.3.4", 18444))
    (active,) = peer_db.get_active_addresses()
    # a whole second, because the field is four octets on the wire and a
    # float has no to_bytes: this is what serving the address needs
    assert isinstance(active.timestamp, int)
    assert NetworkAddressV2.parse(active.serialize()) == active


def test_the_table_of_active_addresses_is_bounded() -> None:
    # #71: the cap is on distinct endpoints, so this many distinct ports
    # each run the table a step closer to it rather than settling onto
    # one row the way redialling the same endpoint does, below
    peer_db = a_peer_db()
    limit = 10000
    for port in range(limit + 10):
        peer_db.add_active_address(peer_address("1.2.3.4", port))
    assert len(peer_db.active_addresses) == limit


def test_redialling_the_same_endpoint_settles_onto_its_one_row() -> None:
    # #270: add_active_address ran once per handshake, with no check for
    # an endpoint already held, so a peer redialled inside the three-hour
    # window grew one row per handshake instead of settling on the
    # latest the way add_addresses's own by_endpoint already does
    peer_db = a_peer_db()
    for port in (18444, 18444, 18444):
        peer_db.add_active_address(peer_address("1.2.3.4", port))
    (active,) = peer_db.active_addresses
    assert active.port == 18444


def test_redialling_the_same_endpoint_many_times_still_holds_one_row() -> None:
    # #270: this many calls against the one endpoint is the same shape
    # `test_the_table_of_active_addresses_is_bounded` puts the cap
    # through, without a distinct port spending it each time. It is also
    # this fix's own regression guard against reintroducing a per-call
    # scan of `active_addresses`: `pyproject.toml`'s own per-test
    # `timeout` is what a scan repeated this many times fails on, not the
    # assertion below.
    peer_db = a_peer_db()
    for _ in range(10010):
        peer_db.add_active_address(peer_address("1.2.3.4", 18444))
    assert len(peer_db.active_addresses) == 1


def test_add_active_address_waits_out_a_prune_already_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A review finding on #71's own timer: add_active_address reads
    # _active_index and then writes into active_addresses at the
    # position found, and get_active_addresses reassigns the list and
    # then rebuilds the index -- both two statements, not one, and
    # reachable from two different threads (callbacks.verack on Node's,
    # manage_connections on P2pManager's). Paused mid-prune here rather
    # than raced on timing: `_reindex_active` is where the pause is
    # forced, after the list has already been reassigned but before the
    # index is rebuilt against it, which is the exact gap the finding
    # traced.
    peer_db = a_peer_db()
    stale = peer_address("9.9.9.9", 1, timestamp=int(time.time()) - 3600 * 4)
    peer_db.active_addresses.append(stale)

    entered_prune = threading.Event()
    release_prune = threading.Event()
    real_reindex = peer_db._reindex_active

    def paused_reindex() -> None:
        entered_prune.set()
        assert release_prune.wait(timeout=5)
        real_reindex()

    monkeypatch.setattr(peer_db, "_reindex_active", paused_reindex)

    pruner = threading.Thread(target=peer_db.get_active_addresses)
    pruner.start()
    assert entered_prune.wait(timeout=5)

    adder = threading.Thread(
        target=peer_db.add_active_address, args=(peer_address("1.2.3.4", 18444),)
    )
    adder.start()
    # the lock is what this proves: without it, add_active_address's own
    # list.append/index write does not wait on anything and this join
    # returns well inside the bound below
    adder.join(timeout=0.2)
    assert adder.is_alive()

    release_prune.set()
    pruner.join(timeout=5)
    adder.join(timeout=5)
    assert not adder.is_alive()
    # a corrupted list -- an IndexError inside add_active_address, or
    # the stale row surviving the prune it raced -- fails one of these
    # two rather than passing on the wrong data
    (active,) = peer_db.active_addresses
    assert active.port == 18444


def test_an_address_not_seen_for_three_hours_stops_being_active() -> None:
    peer_db = a_peer_db()
    fresh = peer_address("1.2.3.4", 18444, timestamp=int(time.time()) - 3600)
    stale = peer_address("5.6.7.8", 18444, timestamp=int(time.time()) - 3600 * 4)
    peer_db.active_addresses += [fresh, stale]
    assert peer_db.get_active_addresses() == [fresh]
    # and it is dropped, not merely left out of the answer
    assert peer_db.active_addresses == [fresh]


def test_the_two_ip_networks_are_told_apart_by_the_text_of_the_address() -> None:
    assert peer_address("1.2.3.4", 8333).network_id == BIP155Network.IPV4
    assert peer_address("2001:db8::1", 8333).network_id == BIP155Network.IPV6
    # four octets and sixteen, which is what BIP155 gives the two ids
    # where an addr version 1 entry maps the v4 one into sixteen
    assert peer_address("1.2.3.4", 8333).address == b"\x01\x02\x03\x04"
    assert len(peer_address("2001:db8::1", 8333).address) == 16


def test_only_an_ip_address_fits_in_an_addr_version_1() -> None:
    assert can_addrv1(peer_address("1.2.3.4", 8333))
    # the other half of the same question, and the half that says the
    # filter is about the network rather than about being dialable
    assert can_addrv1(peer_address("2001:db8::1", 8333))
    assert not can_addrv1(an_onion_address())
    assert not can_addrv1(a_cjdns_address())


def test_an_address_of_no_ip_network_has_no_addr_version_1_form() -> None:
    # the refusal is the network id's and not the length's: cjdns is
    # sixteen octets, so IPv6Address would take one for an IP address
    # and answer with a peer nobody gossiped
    for address in (an_onion_address(), a_cjdns_address()):
        with pytest.raises(ValueError, match="not an ip address"):
            addr_entry(address)


def test_a_v4_peer_survives_the_round_trip_through_an_addr_version_1_entry() -> None:
    # the mapping is where it could not: an entry holds every address in
    # sixteen octets, so what comes back has to be the v4 record again
    for text in ("1.2.3.4", "2001:db8::1"):
        address = peer_address(text, 8333, timestamp=7, services=9)
        assert peer_from_addr_entry(addr_entry(address)) == address


def test_an_address_is_shown_the_way_core_writes_one() -> None:
    # `CService::ToStringAddrPort`: bracketed unless the host is IPv4,
    # so that the host of a v6 peer can be told from its port
    assert ip_and_port("1.2.3.4", 8333) == "1.2.3.4:8333"
    assert ip_and_port("2001:db8::1", 8333) == "[2001:db8::1]:8333"
    # a mapped host is IPv4 to Core too, `SetLegacyIPv6` filing one
    # under NET_IPV4, and this is the form a `NetworkAddress` hands over
    assert ip_and_port("::ffff:1.2.3.4", 8333) == "1.2.3.4:8333"
    assert str(NetworkAddress(0, "1.2.3.4", 8333).ip) == "::ffff:1.2.3.4"


def test_a_host_that_is_not_an_ip_address_is_refused() -> None:
    # nothing reaches `ip_and_port` with one -- a socket answers with an
    # address and a `NetworkAddress` holds one -- and the refusal is
    # what says so rather than a hostname being shown with brackets
    # guessed at
    with pytest.raises(ValueError, match="does not appear to be"):
        ip_and_port("seed.bitcoin.sipa.be", 8333)


def test_the_two_ip_networks_are_dialled_and_an_onion_address_is_not() -> None:
    assert can_connect(peer_address("1.2.3.4", 8333))
    assert can_connect(peer_address("2001:db8::1", 8333))
    assert not can_connect(an_onion_address())


def test_an_address_that_cannot_be_dialled_says_so_rather_than_trying() -> None:
    with pytest.raises(ValueError, match="not yet supported"):
        asyncio.run(dial(an_onion_address()))


def test_a_peer_that_is_listening_is_connected_to() -> None:
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


def test_a_v6_peer_that_is_listening_is_connected_to() -> None:
    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
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


def test_a_dial_that_is_given_up_on_closes_the_socket_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_a_peer_that_is_not_listening_is_given_up_on() -> None:
    # nothing bound: the connection never completes, and what comes back
    # is nothing rather than a socket that is not connected
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    address = peer_address("127.0.0.1", port)
    assert asyncio.run(dial(address)) is None


def test_a_refused_dial_does_not_cost_the_old_poll_s_full_second() -> None:
    # #90: a poll of ten passes at 0.1s apart cannot tell a refusal from
    # a peer that is merely slow to answer, so it always spent the whole
    # second either way. `SO_ERROR`, read through `loop.sock_connect`,
    # is answered by the kernel as soon as the refusal happens.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    address = peer_address("127.0.0.1", port)
    start = time.monotonic()
    assert asyncio.run(dial(address)) is None
    # generous next to the microseconds a refusal actually takes,
    # measured directly outside the suite, and still far under the
    # second the old poll spent
    assert time.monotonic() - start < 0.5


class FakeLoop:
    def __init__(self, answers: dict[str, Exception | list[str]]) -> None:
        self.answers = answers

    async def getaddrinfo(
        self, host: str, port: int
    ) -> list[tuple[None, None, None, None, tuple[str, int]]]:
        answer = self.answers[host]
        if isinstance(answer, Exception):
            raise answer
        return [(None, None, None, None, (ip, port)) for ip in answer]


def a_chain(seeds: list[str]) -> Any:
    # not 8333: the port has to come from the chain, and a default would
    # look the same
    return SimpleNamespace(addresses=list(seeds), port=18444)


def test_the_seeds_that_answer_fill_the_table_and_the_rest_are_passed_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer_db = a_peer_db(a_chain(["down.example", "up.example"]))
    loop = FakeLoop(
        {
            "down.example": socket.gaierror("no such host"),
            "up.example": ["1.2.3.4", "5.6.7.8"],
        }
    )
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    asyncio.run(peer_db.get_addr_from_dns())
    assert peer_db.addresses == {
        peer_address("1.2.3.4", 18444),
        peer_address("5.6.7.8", 18444),
    }


def test_every_seed_that_answers_is_taken_and_a_host_two_of_them_share_is_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the table is the union over the seeds and not the last answer:
    # a lookup that starts over per seed leaves a node with whatever
    # the seed at the end of the list happened to know
    peer_db = a_peer_db(a_chain(["one.example", "two.example"]))
    loop = FakeLoop(
        {
            "one.example": ["1.2.3.4", "5.6.7.8"],
            "two.example": ["5.6.7.8", "9.10.11.12"],
        }
    )
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    asyncio.run(peer_db.get_addr_from_dns())
    assert peer_db.addresses == {
        peer_address("1.2.3.4", 18444),
        peer_address("5.6.7.8", 18444),
        peer_address("9.10.11.12", 18444),
    }


class FakeIpv6Loop:
    async def getaddrinfo(
        self, host: str, port: int
    ) -> list[tuple[int, int, int, str, tuple[str, int, int, int]]]:
        # what a AAAA record resolves to: a sockaddr of four fields
        # rather than two, the flow info and the scope id being the two
        # a peer table has nowhere to put
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 8))
        ]


def test_a_seed_answering_with_ipv6_gives_up_its_host_and_its_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer_db = a_peer_db(a_chain(["v6.example"]))
    monkeypatch.setattr(asyncio, "get_running_loop", FakeIpv6Loop)
    asyncio.run(peer_db.get_addr_from_dns())
    assert peer_db.addresses == {peer_address("2001:db8::1", 18444)}


def test_a_node_that_already_knows_peers_does_not_ask_the_seeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer_db = a_peer_db(a_chain(["up.example"]))
    peer_db.addresses.add(peer_address("1.2.3.4", 8333))
    peer_db.ask_dns_nodes = False
    # the seed lookup fails where it happens rather than being recorded
    # and asserted about afterwards: `asked.append(True) or FakeLoop({})`
    # said the same thing through the right-hand side of an `or` whose
    # left one is `None` every time -- `list.append` returns nothing,
    # so the fallback was the whole of it.
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: pytest.fail("asked the seeds")
    )
    asyncio.run(peer_db.get_addr_from_dns())


def test_an_address_is_drawn_from_the_ones_that_can_be_dialled() -> None:
    peer_db = a_peer_db()
    dialable = peer_address("1.2.3.4", 8333)
    peer_db.addresses.add(dialable)
    peer_db.addresses.add(an_onion_address())
    for _ in range(20):
        assert peer_db.random_address() == dialable


def test_a_table_holding_nothing_dialable_answers_that_there_is_nothing() -> None:
    # the case a draw-until-one-can-be-dialled never comes back from,
    # and a table a node reaches in ordinary operation: onion, i2p and
    # cjdns peers fill it with exactly this
    peer_db = a_peer_db()
    peer_db.addresses.add(an_onion_address())
    peer_db.addresses.add(a_cjdns_address())
    assert call_within(peer_db.random_address) is None


def test_an_empty_table_answers_that_there_is_nothing() -> None:
    # the caller guards on `is_empty` before it draws, so this is not a
    # path the node takes today; drawing from nothing still has to be an
    # answer rather than an IndexError out of a housekeeping loop
    assert call_within(a_peer_db().random_address) is None


def test_the_draw_reaches_every_address_that_can_be_dialled() -> None:
    # over all of them, not the first one that will do: a node that only
    # ever dials one entry of its table is a node with one peer -- and
    # ipv4 and ipv6 are both dialled, not only the first
    peer_db = a_peer_db()
    dialable = {peer_address(f"1.2.3.{host}", 8333) for host in range(1, 4)}
    dialable.add(peer_address("2001:db8::1", 8333))
    peer_db.addresses |= dialable
    peer_db.addresses.add(an_onion_address())
    assert {peer_db.random_address() for _ in range(80)} == dialable


def test_the_table_of_known_addresses_is_bounded() -> None:
    peer_db = a_peer_db()
    limit = 10000
    peer_db.add_addresses([peer_address("1.2.3.4", port) for port in range(limit + 10)])
    assert len(peer_db.addresses) == limit


def test_a_v4_mapped_ipv6_record_is_not_kept() -> None:
    # BIP155: a client SHOULD ignore an IPV6 entry whose octets are
    # `::ffff:0:0/96`, the IPv4 mapping -- keeping it is #151, an entry
    # that later writes into an addr version 1 message as the same
    # sixteen octets an ordinary IPv4 peer does
    peer_db = a_peer_db()
    peer_db.add_addresses([peer_address(_A_V4_MAPPED_ADDRESS, 8333)])
    assert not peer_db.addresses


def test_an_onioncat_ipv6_record_is_not_kept() -> None:
    # the other half of the same rule: `fd87:d87e:eb43::/48` is where a
    # TORv2 address used to be embedded in a fake IPv6 one
    peer_db = a_peer_db()
    peer_db.add_addresses([peer_address(_AN_ONIONCAT_ADDRESS, 8333)])
    assert not peer_db.addresses


def test_an_ordinary_ipv6_record_is_kept() -> None:
    # the rule is about the two reserved ranges and not about the
    # network id: an address outside both is an ordinary peer
    peer_db = a_peer_db()
    address = peer_address("2001:db8::1", 8333)
    peer_db.add_addresses([address])
    assert peer_db.addresses == {address}


def test_an_address_a_peer_told_us_about_is_kept_without_its_timestamp() -> None:
    # a peer's word for when it last saw an address is not evidence, and
    # keeping it would make the same address several entries
    peer_db = a_peer_db()
    early = peer_address("1.2.3.4", 8333, timestamp=1)
    late = peer_address("1.2.3.4", 8333, timestamp=2)
    peer_db.add_addresses([early, late, early])
    (kept,) = peer_db.addresses
    assert kept.timestamp == 0
    assert kept.address == early.address


def test_two_gossiped_records_for_one_endpoint_settle_on_the_latest_services() -> None:
    # #247: two records for the same network id, address and port but
    # different `services` used to become two members of the table
    # instead of one settling on the endpoint's latest `services`
    peer_db = a_peer_db()
    old = peer_address("1.2.3.4", 8333, services=0)
    new = peer_address("1.2.3.4", 8333, services=1)
    peer_db.add_addresses([old, new])
    (kept,) = peer_db.addresses
    assert kept.services == 1


def test_updating_an_endpoint_already_known_does_not_spend_the_cap() -> None:
    # a second gossip for an endpoint the table already holds is not a
    # new endpoint, so it must not be turned away as though the cap had
    # run out on it
    peer_db = a_peer_db()
    peer_db.addresses = {peer_address("1.2.3.4", port) for port in range(10000)}
    peer_db.add_addresses([peer_address("1.2.3.4", 0, services=1)])
    assert len(peer_db.addresses) == 10000
    (updated,) = [addr for addr in peer_db.addresses if addr.port == 0]
    assert updated.services == 1


def test_an_address_that_answered_is_preferred_within_the_same_run() -> None:
    # #123: dialling should not draw uniformly over a table that already
    # knows which of its entries actually answered, even before any of
    # it is read back from a restart
    peer_db = a_peer_db()
    answered = peer_address("1.2.3.4", 8333)
    gossiped = peer_address("5.6.7.8", 8333)
    peer_db.addresses |= {answered, gossiped}
    peer_db.add_active_address(answered)
    for _ in range(20):
        drawn = peer_db.random_address()
        assert drawn is not None
        assert drawn.address == answered.address
        assert drawn.port == answered.port


def test_a_known_address_survives_a_restart(tmp_path: Path) -> None:
    first = a_peer_db(data_dir=tmp_path)
    first.add_addresses([peer_address("1.2.3.4", 8333)])
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.addresses == {peer_address("1.2.3.4", 8333)}
    second.close()


def test_an_address_that_answered_survives_a_restart_and_is_preferred(
    tmp_path: Path,
) -> None:
    first = a_peer_db(data_dir=tmp_path)
    answered = peer_address("1.2.3.4", 8333)
    unconfirmed = peer_address("5.6.7.8", 8333)
    first.add_addresses([answered, unconfirmed])
    first.add_active_address(answered)
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.addresses == {answered, unconfirmed}
    drawn = second.random_address()
    assert drawn is not None
    assert drawn.address == answered.address
    assert drawn.port == answered.port
    second.close()


def test_a_fresh_store_asks_the_seeds(tmp_path: Path) -> None:
    peer_db = a_peer_db(data_dir=tmp_path)
    assert peer_db.ask_dns_nodes
    peer_db.close()


def test_a_store_holding_only_unconfirmed_gossip_still_asks_the_seeds(
    tmp_path: Path,
) -> None:
    # #89: a table that is not empty but is not dialable either -- a
    # seed that answered with AAAA records alone leaves exactly this --
    # is not a reason to skip the seeds
    first = a_peer_db(data_dir=tmp_path)
    first.add_addresses([peer_address("2001:db8::1", 8333)])
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.ask_dns_nodes
    second.close()


def test_a_store_with_a_recently_answered_address_skips_the_seeds(
    tmp_path: Path,
) -> None:
    first = a_peer_db(data_dir=tmp_path)
    answered = peer_address("1.2.3.4", 8333)
    first.add_addresses([answered])
    first.add_active_address(answered)
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert not second.ask_dns_nodes
    second.close()


def test_a_stale_answered_address_no_longer_holds_off_the_seeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = a_peer_db(data_dir=tmp_path)
    stale = peer_address("1.2.3.4", 8333)
    four_hours_ago = time.time() - 3600 * 4
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: four_hours_ago)
        first.add_active_address(stale)
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    assert second.ask_dns_nodes
    second.close()


def test_get_active_addresses_deletes_a_stale_row_from_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #253: nothing in `add_active_address` ever bounded the durable
    # `answered-` rows the way `add_addresses`'s 10000-entry cap bounds
    # `known-` ones
    peer_db = a_peer_db(data_dir=tmp_path)
    stale = peer_address("1.2.3.4", 8333)
    four_hours_ago = time.time() - 3600 * 4
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: four_hours_ago)
        peer_db.add_active_address(stale)
    assert peer_db.db is not None
    assert list(peer_db.db)
    peer_db.get_active_addresses()
    assert not list(peer_db.db)
    peer_db.close()


def test_a_stale_answered_row_does_not_survive_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = a_peer_db(data_dir=tmp_path)
    stale = peer_address("1.2.3.4", 8333)
    four_hours_ago = time.time() - 3600 * 4
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: four_hours_ago)
        first.add_active_address(stale)
    first.close()

    second = a_peer_db(data_dir=tmp_path)
    # `__init__` already calls `get_active_addresses` once, to decide
    # `ask_dns_nodes`, so the row is gone from the store by the time
    # construction returns
    assert second.db is not None
    assert not list(second.db)
    second.close()


def test_closing_a_peer_db_with_no_store_does_nothing() -> None:
    a_peer_db().close()


def test_a_key_this_version_does_not_know_is_left_where_it_is(tmp_path: Path) -> None:
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
