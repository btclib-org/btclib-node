# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio
import socket
import time
from types import SimpleNamespace

import pytest
from btclib import var_bytes, var_int

# aliased: pytest tries to collect a class whose name starts with Test
from btclib_node.chains import Main, SigNet
from btclib_node.chains import TestNet as TestNetwork
from btclib_node.p2p.address import NetworkAddress, NetworkID, PeerDB


def test_serialization():
    for netid in NetworkID:
        start = 1 if netid != NetworkID.ipv6 else 49
        for addrv2 in (True, False):
            if not addrv2 and not netid.can_addrv1:
                continue
            for x in range(start, netid.addr_bytesize * 8 + 1):
                addr = (2**x - 1).to_bytes(netid.addr_bytesize, "big")
                for y in range(10):
                    services = 2**y
                    for z in range(1, 17):
                        port = 2**z - 1
                        network_address = NetworkAddress(0, services, netid, addr, port)
                        assert network_address == NetworkAddress.deserialize(
                            network_address.serialize(addrv2=addrv2), addrv2=addrv2
                        )


def test_an_address_just_seen_is_active_and_can_be_sent():
    peer_db = PeerDB(None, None)
    peer_db.add_active_address(NetworkAddress.from_ip_and_port("1.2.3.4", 18444))
    (active,) = peer_db.get_active_addresses()
    # a whole second, because the field is four octets on the wire and a
    # float has no to_bytes: this is what serving the address needs
    assert isinstance(active.time, int)
    assert NetworkAddress.deserialize(active.serialize()) == active


def test_an_address_not_seen_for_three_hours_stops_being_active():
    peer_db = PeerDB(None, None)
    fresh = NetworkAddress.from_ip_and_port(
        "1.2.3.4", 18444, time=int(time.time()) - 3600
    )
    stale = NetworkAddress.from_ip_and_port(
        "5.6.7.8", 18444, time=int(time.time()) - 3600 * 4
    )
    peer_db.active_addresses += [fresh, stale]
    assert peer_db.get_active_addresses() == [fresh]
    # and it is dropped, not merely left out of the answer
    assert peer_db.active_addresses == [fresh]


@pytest.mark.remote_data
def test_main_bootstrap_nodes():
    peer_db = PeerDB(Main(), None)
    peer_db.ask_dns_nodes = True
    asyncio.run(peer_db.get_addr_from_dns())
    assert not peer_db.is_empty


@pytest.mark.remote_data
def test_testnet_bootstrap_nodes():
    peer_db = PeerDB(SigNet(), None)
    peer_db.ask_dns_nodes = True
    asyncio.run(peer_db.get_addr_from_dns())
    assert not peer_db.is_empty


@pytest.mark.remote_data
def test_signet_bootstrap_nodes():
    peer_db = PeerDB(TestNetwork(), None)
    peer_db.ask_dns_nodes = True
    asyncio.run(peer_db.get_addr_from_dns())
    assert not peer_db.is_empty


def test_an_ipv6_address_is_recognised_from_its_text():
    address = NetworkAddress.from_ip_and_port("2001:db8::1", 8333)
    assert address.netid == NetworkID.ipv6
    assert repr(address) == "2001:db8::1:8333"


def test_an_address_of_the_wrong_length_for_its_network_is_refused():
    short = NetworkAddress(netid=NetworkID.ipv6, addr=b"\x00" * 4)
    with pytest.raises(ValueError):
        short.serialize(addrv2=True)

    payload = (
        (0).to_bytes(4, "little")
        + var_int.serialize(0)
        + NetworkID.ipv6.to_bytes(1, "big")
        + var_bytes.serialize(b"\x00" * 4)
        + (8333).to_bytes(2, "big")
    )
    with pytest.raises(ValueError, match="Invalid address byte length"):
        NetworkAddress.deserialize(payload, addrv2=True)


def test_an_onion_address_cannot_be_put_in_an_addr_version_1():
    onion = NetworkAddress(netid=NetworkID.torv3, addr=b"\x11" * 32, port=8333)
    with pytest.raises(ValueError, match="cannot be serialized"):
        onion.serialize()
    onion.serialize(addrv2=True)


def test_a_network_id_nothing_knows_the_size_of_is_refused():
    # every member is answered above it; the guard is what stops a new
    # one being added without a size, and only a non-member reaches it
    with pytest.raises(ValueError):
        NetworkID.addr_bytesize.fget(object())


def test_only_ipv4_is_dialled_for_now():
    assert NetworkAddress.from_ip_and_port("1.2.3.4", 8333).can_connect
    assert not NetworkAddress.from_ip_and_port("2001:db8::1", 8333).can_connect
    onion = NetworkAddress(netid=NetworkID.torv3, addr=b"\x11" * 32, port=8333)
    assert not onion.can_connect
    assert repr(onion) == f"{onion.addr.hex()}:8333"


def test_an_address_that_cannot_be_dialled_says_so_rather_than_trying():
    onion = NetworkAddress(netid=NetworkID.torv3, addr=b"\x11" * 32, port=8333)
    with pytest.raises(ValueError, match="not yet supported"):
        asyncio.run(onion.connect())


def test_a_peer_that_is_listening_is_connected_to():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        address = NetworkAddress.from_ip_and_port("127.0.0.1", port)
        client = asyncio.run(address.connect())
        assert client is not None
        assert client.getpeername() == ("127.0.0.1", port)
        client.close()
    finally:
        listener.close()


def test_a_peer_that_is_not_listening_is_given_up_on():
    # nothing bound: the connection never completes, and what comes back
    # is nothing rather than a socket that is not connected
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    address = NetworkAddress.from_ip_and_port("127.0.0.1", port)
    assert asyncio.run(address.connect()) is None


class FakeLoop:
    def __init__(self, answers):
        self.answers = answers

    async def getaddrinfo(self, host, port):
        answer = self.answers[host]
        if isinstance(answer, Exception):
            raise answer
        return [(None, None, None, None, (ip, port)) for ip in answer]


def a_chain(seeds):
    return SimpleNamespace(addresses=list(seeds), port=8333)


def test_the_seeds_that_answer_fill_the_table_and_the_rest_are_passed_over(
    monkeypatch,
):
    peer_db = PeerDB(a_chain(["down.example", "up.example"]), None)
    loop = FakeLoop(
        {
            "down.example": socket.gaierror("no such host"),
            "up.example": ["1.2.3.4", "5.6.7.8"],
        }
    )
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    asyncio.run(peer_db.get_addr_from_dns())
    assert {repr(address) for address in peer_db.addresses} == {
        "1.2.3.4:8333",
        "5.6.7.8:8333",
    }


def test_a_node_that_already_knows_peers_does_not_ask_the_seeds(monkeypatch):
    peer_db = PeerDB(a_chain(["up.example"]), None)
    peer_db.addresses.add(NetworkAddress.from_ip_and_port("1.2.3.4", 8333))
    peer_db.ask_dns_nodes = False
    asked = []
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: asked.append(True) or FakeLoop({})
    )
    asyncio.run(peer_db.get_addr_from_dns())
    assert not asked


def test_an_address_is_drawn_from_the_ones_that_can_be_dialled():
    peer_db = PeerDB(None, None)
    dialable = NetworkAddress.from_ip_and_port("1.2.3.4", 8333)
    peer_db.addresses.add(dialable)
    peer_db.addresses.add(
        NetworkAddress(netid=NetworkID.torv3, addr=b"\x11" * 32, port=8333)
    )
    for _ in range(20):
        assert peer_db.random_address() == dialable


def test_the_table_of_known_addresses_is_bounded():
    peer_db = PeerDB(None, None)
    limit = 10000
    peer_db.add_addresses(
        [NetworkAddress.from_ip_and_port("1.2.3.4", port) for port in range(limit + 10)]
    )
    assert len(peer_db.addresses) == limit


def test_an_address_already_known_is_not_added_twice():
    peer_db = PeerDB(None, None)
    address = NetworkAddress.from_ip_and_port("1.2.3.4", 8333)
    peer_db.add_addresses([address, address])
    assert len(peer_db.addresses) == 1
