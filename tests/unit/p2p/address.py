# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import asyncio

import pytest

from btclib_node.chains import Main, SigNet, TestNet
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
    peer_db = PeerDB(TestNet(), None)
    peer_db.ask_dns_nodes = True
    asyncio.run(peer_db.get_addr_from_dns())
    assert not peer_db.is_empty
