# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What a peer gets back when it asks this node for addresses.

`getaddr` is the one callback no test ever reached, and every branch of
it was wrong: it read a property off the wrong object, then subscripted
a function. `main.handle_p2p` turns a callback that raises into a
disconnect, so a peer asking a perfectly ordinary question was dropped.
"""

import time
from types import SimpleNamespace

from btclib_node.p2p.address import NetworkAddress, NetworkID, PeerDB
from btclib_node.p2p.callbacks import getaddr
from btclib_node.p2p.messages.address import Addr, AddrV2


def an_address(n=0, netid=NetworkID.ipv4):
    # seen just now: an address the node would not serve is a different
    # test, in tests/unit/p2p/address.py
    return NetworkAddress(
        time=int(time.time()),
        services=0,
        netid=netid,
        addr=n.to_bytes(netid.addr_bytesize, "big"),
        port=18444,
    )


def make_node(addresses, *, prefer_addressv2=False):
    peer_db = PeerDB(None, None)
    for address in addresses:
        peer_db.active_addresses.append(address)
    sent = []
    conn = SimpleNamespace(prefer_addressv2=prefer_addressv2, send=sent.append)
    node = SimpleNamespace(p2p_manager=SimpleNamespace(peer_db=peer_db))
    return node, conn, sent


def test_an_ipv4_address_is_answered_in_an_addr():
    address = an_address()
    node, conn, sent = make_node([address])
    getaddr(node, b"", conn)
    (answer,) = sent
    assert isinstance(answer, Addr)
    assert answer.addresses == [address]
    # and it survives the wire, which is what the netid filter is for
    assert Addr.parse(answer.serialize()).addresses == [address]


def test_a_peer_that_asked_for_addrv2_gets_addrv2():
    address = an_address()
    node, conn, sent = make_node([address], prefer_addressv2=True)
    getaddr(node, b"", conn)
    (answer,) = sent
    assert isinstance(answer, AddrV2)
    assert answer.addresses == [address]


def test_an_address_addr_version_1_cannot_carry_is_left_out():
    # Addr.serialize raises on one of these rather than inventing an
    # address, so sending it would drop the peer we are answering
    onion = an_address(netid=NetworkID.torv3)
    ipv4 = an_address()
    node, conn, sent = make_node([onion, ipv4])
    getaddr(node, b"", conn)
    (answer,) = sent
    assert answer.addresses == [ipv4]
    answer.serialize()


def test_the_same_address_reaches_a_peer_that_can_take_it():
    onion = an_address(netid=NetworkID.torv3)
    node, conn, sent = make_node([onion], prefer_addressv2=True)
    getaddr(node, b"", conn)
    (answer,) = sent
    assert answer.addresses == [onion]


def test_nothing_active_is_answered_with_nothing():
    node, conn, sent = make_node([])
    getaddr(node, b"", conn)
    assert not sent


def test_more_addresses_than_fit_one_message_are_split():
    addresses = [an_address(n) for n in range(2001)]
    node, conn, sent = make_node(addresses)
    getaddr(node, b"", conn)
    assert [len(answer.addresses) for answer in sent] == [1000, 1000, 1]
    served = [address for answer in sent for address in answer.addresses]
    assert served == addresses
