# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`getnodeaddresses` and `addpeeraddress`, Core's RPCs over the table."""

import logging
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from bitcoin_core_rpc import RPCErrorCode
from btclib.p2p.address import ServiceFlags
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2

from btclib_node.p2p.address import PeerDB, peer_address
from btclib_node.p2p.banman import BanMan, lookup_subnet
from btclib_node.rpc.callbacks import (
    add_peer_address,
    callbacks,
    get_node_addresses,
)
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import HELP_TEXT

if TYPE_CHECKING:
    from pathlib import Path

    from btclib_node import Node
    from btclib_node.chains import Chain
    from btclib_node.rpc.connection import RpcConnection

# none of these callbacks reads the connection it is handed
_CONN = cast("RpcConnection", None)
_FULL = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS
_ONION = "pg6mmjiyjmcrsslvykfwnntlaru7p5svn6y2ymmju6nubxndf4pscryd.onion"
_I2P = "ukeu3k5oycgaauneqgtnvselmt4yemvoilkln7jpvamvfx7dnkdq.b32.i2p"


def a_node(
    *, banned: str | None = None, discouraged: tuple[str, ...] = ()
) -> tuple[Node, PeerDB]:
    """Build a node double holding a table, and a ban list banning `banned`."""
    peer_db = PeerDB(cast("Chain", None), cast("Path", None))
    ban_man = BanMan(None, logging.getLogger(__name__))
    if banned is not None:
        subnet = lookup_subnet(banned)
        assert subnet is not None
        ban_man.ban(subnet)
    manager = SimpleNamespace(
        peer_db=peer_db,
        ban_man=ban_man,
        is_discouraged=lambda address: _text(address) in discouraged,
    )
    return cast("Node", SimpleNamespace(p2p_manager=manager)), peer_db


def _text(address: NetworkAddressV2) -> str:
    """Return the IP text of an IPv4 `address`, what `a_node` discourages by."""
    return ".".join(str(octet) for octet in address.address)


def known(peer_db: PeerDB, *addresses: NetworkAddressV2) -> None:
    """Make `peer_db` hold `addresses`, as gossiped with their own times."""
    peer_db.add_addresses(addresses, time_penalty=0)


def seen(ip: str, *, age: int = 0, port: int = 8333) -> NetworkAddressV2:
    """Build a full node at `ip`, last seen `age` seconds ago."""
    return peer_address(ip, port, timestamp=int(time.time()) - age, services=int(_FULL))


def test_both_are_served() -> None:
    """ISS 1443: each is a key of the table requests are resolved through."""
    assert callbacks["getnodeaddresses"] is get_node_addresses
    assert callbacks["addpeeraddress"] is add_peer_address


def test_getnodeaddresses_answers_one_address_by_default() -> None:
    """ISS 1443: `count` is `1` where it is omitted or `null`."""
    node, peer_db = a_node()
    known(peer_db, seen("1.2.3.4"), seen("1.2.3.5"))
    assert len(get_node_addresses(node, _CONN, [])) == 1
    assert len(get_node_addresses(node, _CONN, [None, "ipv4"])) == 1


def test_getnodeaddresses_answers_each_field_core_does() -> None:
    """ISS 1443: time, services, address, port and network, of an IP address."""
    node, peer_db = a_node()
    heard = seen("1.2.3.4", age=3600)
    known(peer_db, heard)
    assert get_node_addresses(node, _CONN, [0]) == [
        {
            "time": heard.timestamp,
            "services": int(_FULL),
            "address": "1.2.3.4",
            "port": 8333,
            "network": "ipv4",
        }
    ]


def test_getnodeaddresses_names_an_ipv6_onion_and_i2p_address() -> None:
    """ISS 1443: `ToStringAddr` and `GetNetworkName` of each network."""
    node, peer_db = a_node()
    now = int(time.time())
    onion = NetworkAddressV2(now, 0, BIP155Network.TORV3, b"\x11" * 32, 8333)
    i2p = NetworkAddressV2(now, 0, BIP155Network.I2P, b"\x22" * 32, 0)
    known(peer_db, seen("2a01:4f8::1"), onion, i2p)
    answered = {
        entry["network"]: entry["address"]
        for entry in get_node_addresses(node, _CONN, [0])
    }
    assert answered["ipv6"] == "2a01:4f8::1"
    assert answered["onion"].endswith(".onion")
    assert answered["i2p"].endswith(".b32.i2p")
    assert [e["network"] for e in get_node_addresses(node, _CONN, [0, "I2P"])] == [
        "i2p"
    ]


def test_getnodeaddresses_count_zero_answers_every_address() -> None:
    """ISS 1443: `GetAddr_` of `max_addresses` and `max_pct` `0` draws all."""
    node, peer_db = a_node()
    known(peer_db, *(seen(f"1.2.3.{n}") for n in range(1, 21)))
    assert len(get_node_addresses(node, _CONN, [0])) == 20
    assert len(get_node_addresses(node, _CONN, [7])) == 7


def test_getnodeaddresses_leaves_out_a_terrible_discouraged_or_banned_address() -> None:
    """ISS 1443: after filtering for quality and recency, as `getaddr`'s is."""
    node, peer_db = a_node(banned="5.6.0.0/16", discouraged=("1.2.3.6",))
    fresh = seen("1.2.3.4")
    known(
        peer_db,
        fresh,
        seen("1.2.3.5", age=31 * 24 * 3600),
        seen("1.2.3.6"),
        seen("5.6.7.8"),
    )
    assert [e["address"] for e in get_node_addresses(node, _CONN, [0])] == ["1.2.3.4"]


def test_getnodeaddresses_keeps_to_the_network_asked_for() -> None:
    """ISS 1443: `network` keeps one of Core's names, in any case."""
    node, peer_db = a_node()
    known(peer_db, seen("1.2.3.4"), seen("2a01:4f8::1"))
    for name, expected in (("ipv4", "1.2.3.4"), ("IPv6", "2a01:4f8::1")):
        (entry,) = get_node_addresses(node, _CONN, [0, name])
        assert entry["address"] == expected
    assert get_node_addresses(node, _CONN, [0, "onion"]) == []


@pytest.mark.parametrize("network", ["tor", "", "not_publicly_routable"])
def test_getnodeaddresses_refuses_a_network_it_does_not_know(network: str) -> None:
    """ISS 1443: `ParseNetwork` answers `NET_UNROUTABLE`, which is refused."""
    node, _ = a_node()
    with pytest.raises(RpcError) as raised:
        get_node_addresses(node, _CONN, [1, network])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == f"Network not recognized: {network}"


@pytest.mark.parametrize(
    ("params", "code", "message"),
    [
        ([-1], RPCErrorCode.INVALID_PARAMETER, "Address count out of range"),
        ([1 << 31], RPCErrorCode.MISC_ERROR, "JSON integer out of range"),
        ([5.5], RPCErrorCode.MISC_ERROR, "JSON integer out of range"),
    ],
)
def test_getnodeaddresses_refuses_a_count_out_of_range(
    params: list[Any], code: RPCErrorCode, message: str
) -> None:
    """ISS 1443: Core's two refusals of `count`, as bitcoind v31.1.0 answers."""
    node, _ = a_node()
    with pytest.raises(RpcError) as raised:
        get_node_addresses(node, _CONN, params)
    assert (raised.value.code, raised.value.message) == (code, message)


def test_getnodeaddresses_refuses_an_argument_of_the_wrong_type() -> None:
    """ISS 1443: `HandleRequest`'s type check, every mismatch named."""
    node, _ = a_node()
    with pytest.raises(RpcError) as raised:
        get_node_addresses(node, _CONN, ["x", 5])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        "Wrong type passed:\n{\n"
        '    "Position 1 (count)": "JSON value of type string is not of'
        ' expected type number",\n'
        '    "Position 2 (network)": "JSON value of type number is not of'
        ' expected type string"\n}'
    )
    with pytest.raises(RpcError) as bool_raised:
        get_node_addresses(node, _CONN, [True])
    assert bool_raised.value.code == RPCErrorCode.TYPE_ERROR


def test_addpeeraddress_adds_an_address_to_the_table() -> None:
    """ISS 1443: `NODE_NETWORK | NODE_WITNESS`, seen now, itself the source."""
    node, peer_db = a_node()
    before = int(time.time())
    assert add_peer_address(node, _CONN, ["1.2.3.4", 8333]) == {"success": True}
    (row,) = peer_db.addresses
    assert (row.address, row.port) == (bytes([1, 2, 3, 4]), 8333)
    assert row.services == _FULL
    # no gossip penalty, the address being its own source
    assert row.timestamp >= before
    assert not peer_db.active_addresses


def test_addpeeraddress_with_tried_answers_the_address_too() -> None:
    """ISS 1443: `tried` moves the new address to the answered table."""
    node, peer_db = a_node()
    assert add_peer_address(node, _CONN, ["1.2.3.4", 8333, True]) == {"success": True}
    (row,) = peer_db.active_addresses
    assert row.address == bytes([1, 2, 3, 4])


@pytest.mark.parametrize("tried", [False, True])
def test_addpeeraddress_refuses_an_address_already_held(*, tried: bool) -> None:
    """ISS 1443: `Add` answers false for it, `tried` or not."""
    node, _ = a_node()
    add_peer_address(node, _CONN, ["1.2.3.4", 8333])
    assert add_peer_address(node, _CONN, ["1.2.3.4", 8333, tried]) == {
        "error": "failed-adding-to-new",
        "success": False,
    }


@pytest.mark.parametrize("host", ["10.0.0.1", "127.0.0.1", "fc00::1"])
def test_addpeeraddress_refuses_an_address_nobody_could_dial(host: str) -> None:
    """ISS 1443: `AddSingle` refuses what `IsRoutable` does."""
    node, peer_db = a_node()
    assert add_peer_address(node, _CONN, [host, 8333]) == {
        "error": "failed-adding-to-new",
        "success": False,
    }
    assert not peer_db.addresses


def test_addpeeraddress_reports_an_address_that_cannot_be_moved_to_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1443: `Good` answers false, and the new-table add is kept."""
    node, peer_db = a_node()
    monkeypatch.setattr(PeerDB, "add_active_address", lambda self, address: False)
    assert add_peer_address(node, _CONN, ["1.2.3.4", 8333, True]) == {
        "error": "failed-adding-to-tried",
        "success": False,
    }
    assert len(peer_db.addresses) == 1


def test_addpeeraddress_takes_an_onion_and_an_ipv6_host() -> None:
    """ISS 1443: `LookupHost` reads a v3 onion name and an IPv6 address."""
    node, peer_db = a_node()
    assert add_peer_address(node, _CONN, ["2a01:4f8::1", 8333]) == {"success": True}
    assert add_peer_address(node, _CONN, [_ONION, 8333]) == {"success": True}
    assert add_peer_address(node, _CONN, [_I2P, 0]) == {"success": True}
    assert {row.network_id for row in peer_db.addresses} == {
        BIP155Network.IPV6,
        BIP155Network.TORV3,
        BIP155Network.I2P,
    }


def test_addpeeraddress_refuses_a_host_it_cannot_read() -> None:
    """ISS 1443: `RPC_CLIENT_INVALID_IP_OR_SUBNET`, as bitcoind v31.1.0."""
    node, _ = a_node()
    with pytest.raises(RpcError) as raised:
        add_peer_address(node, _CONN, ["bad", 8333])
    assert raised.value.code == RPCErrorCode.CLIENT_INVALID_IP_OR_SUBNET
    assert raised.value.message == "Invalid IP address"


@pytest.mark.parametrize("port", [-1, 65536, 8333.5])
def test_addpeeraddress_refuses_a_port_out_of_range(port: float) -> None:
    """ISS 1443: `getInt<uint16_t>`, "JSON integer out of range"."""
    node, _ = a_node()
    with pytest.raises(RpcError) as raised:
        add_peer_address(node, _CONN, ["1.2.3.4", port])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "JSON integer out of range"


def test_addpeeraddress_refuses_arguments_of_the_wrong_type() -> None:
    """ISS 1443: `HandleRequest`'s type check, every mismatch named."""
    node, _ = a_node()
    with pytest.raises(RpcError) as raised:
        add_peer_address(node, _CONN, [1, "x", "y"])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    for position in ("1 (address)", "2 (port)", "3 (tried)"):
        assert f'"Position {position}"' in raised.value.message


def test_addpeeraddress_refuses_a_call_short_of_its_arguments() -> None:
    """ISS 1443: the full help text, under `RPC_MISC_ERROR`."""
    node, _ = a_node()
    with pytest.raises(RpcError) as raised:
        add_peer_address(node, _CONN, ["1.2.3.4"])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT["addpeeraddress"]


def test_what_addpeeraddress_adds_getnodeaddresses_answers() -> None:
    """ISS 1443: the two together, as Core's `p2p_dns_seeds.py` uses them."""
    node, _ = a_node()
    add_peer_address(node, _CONN, ["1.2.3.4", 8333])
    add_peer_address(node, _CONN, ["2a01:4f8::1", 8333, True])
    answered = get_node_addresses(node, _CONN, [0])
    assert {entry["address"] for entry in answered} == {"1.2.3.4", "2a01:4f8::1"}


def test_getnodeaddresses_names_a_6to4_and_teredo_address_ipv4() -> None:
    """ISS 1443: `GetNetClass`, as bitcoind v31.1.0, in field and filter."""
    node, peer_db = a_node()
    known(peer_db, seen("2002:102:304::1"), seen("2001:0:102:304::1"))
    answered = get_node_addresses(node, _CONN, [0, "ipv4"])
    assert {entry["network"] for entry in answered} == {"ipv4"}
    assert len(answered) == 2
    assert get_node_addresses(node, _CONN, [0, "ipv6"]) == []
