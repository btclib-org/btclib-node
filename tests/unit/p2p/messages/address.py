# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.p2p.address import NetworkAddress, NetworkID
from btclib_node.p2p.messages import get_payload
from btclib_node.p2p.messages.address import Addr, AddrV2, Getaddr


def test_empty_addr():
    msg = Addr([])
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Addr.deserialize(get_payload(msg_bytes)[1])


def test_valid_addr():
    msg = Addr([NetworkAddress(0, 0, NetworkID.ipv4, b"\x00" * 4, 1)])
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Addr.deserialize(get_payload(msg_bytes)[1])


def test_empty_addrv2():
    msg = AddrV2([])
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == AddrV2.deserialize(get_payload(msg_bytes)[1])


def test_valid_addrv2():
    for netid in NetworkID:
        addr_bytes = b"\x00" * netid.addr_bytesize
        msg = AddrV2([NetworkAddress(0, 0, netid, addr_bytes, 1)])
        msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
        assert msg == AddrV2.deserialize(get_payload(msg_bytes)[1])


def test_valid_getaddr():
    msg = Getaddr()
    msg_bytes = bytes.fromhex("00" * 4) + msg.serialize()
    assert msg == Getaddr.deserialize(get_payload(msg_bytes)[1])
