# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio
import socket
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


def a_peer_db(chain: Any = None) -> PeerDB:
    return PeerDB(cast(Chain, chain), cast(Path, None))


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


def test_only_ipv4_is_dialled_for_now() -> None:
    assert can_connect(peer_address("1.2.3.4", 8333))
    assert not can_connect(peer_address("2001:db8::1", 8333))
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
    # and a table a node reaches in ordinary operation: a seed answering
    # with AAAA records alone fills it with exactly this
    peer_db = a_peer_db()
    peer_db.addresses.add(peer_address("2001:db8::1", 8333))
    peer_db.addresses.add(an_onion_address())
    assert call_within(peer_db.random_address) is None


def test_an_empty_table_answers_that_there_is_nothing() -> None:
    # the caller guards on `is_empty` before it draws, so this is not a
    # path the node takes today; drawing from nothing still has to be an
    # answer rather than an IndexError out of a housekeeping loop
    assert call_within(a_peer_db().random_address) is None


def test_the_draw_reaches_every_address_that_can_be_dialled() -> None:
    # over all of them, not the first one that will do: a node that only
    # ever dials one entry of its table is a node with one peer
    peer_db = a_peer_db()
    dialable = {peer_address(f"1.2.3.{host}", 8333) for host in range(1, 4)}
    peer_db.addresses |= dialable
    peer_db.addresses.add(peer_address("2001:db8::1", 8333))
    assert {peer_db.random_address() for _ in range(60)} == dialable


def test_the_table_of_known_addresses_is_bounded() -> None:
    peer_db = a_peer_db()
    limit = 10000
    peer_db.add_addresses([peer_address("1.2.3.4", port) for port in range(limit + 10)])
    assert len(peer_db.addresses) == limit


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
