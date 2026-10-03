# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""This node and a real bitcoind speak BIP324, whichever of them dials.

Each test ends with a run of `ping`s, more than a rekey interval of
them, which bitcoind answers with as many `pong`s: each direction
crosses the rekey with bitcoind's own cipher on the other end.
"""

from typing import TYPE_CHECKING, Any, cast

from btclib.p2p.address import ServiceFlags
from btclib.p2p.bip324 import EXPANSION, contents_from_message
from btclib.p2p.keepalive import Ping, Pong

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.address import peer_address
from btclib_node.p2p.transport import TransportProtocolType
from tests import get_random_port, wait_until, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path

    from btclib_node.p2p.connection import Connection
    from tests.integration.conftest import Bitcoind

# BIP324 rekeys every 224 packets, in each direction
_PINGS = 224 + 76
# what a `pong` takes on the wire: its short type id and nonce, and the
# header and tag every packet carries
_PONG_SIZE = len(contents_from_message("pong", Pong(1).serialize())) + EXPANSION


def _node(tmp_path: Path, **config: Any) -> Node:
    """Return a started regtest node, listening."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
            **config,
        )
    )
    node.start()
    wait_until_listening(node.p2p_manager)
    return node


def _connected(node: Node) -> Connection:
    """Wait for `node`'s one peer to pass its handshake, and return it."""
    wait_until(lambda: len(node.p2p_manager.connections) == 1)
    (conn,) = node.p2p_manager.connections.values()
    wait_until(lambda: conn.status == P2pConnStatus.Connected)
    return conn


def _their_peer(bitcoind: Bitcoind) -> dict[str, Any]:
    """Return bitcoind's one peer, once it has one."""
    wait_until(lambda: len(cast("list[Any]", bitcoind.rpc("getpeerinfo"))) == 1)
    (peer,) = cast("list[dict[str, Any]]", bitcoind.rpc("getpeerinfo"))
    return peer


def _agree_on_v2(bitcoind: Bitcoind, conn: Connection) -> None:
    """Hold both ends to v2 and to the one session id."""

    def ours() -> TransportProtocolType:
        return conn.transport.get_info().transport_type

    wait_until(lambda: ours() is TransportProtocolType.V2)
    session_id = conn.transport.get_info().session_id
    assert session_id is not None
    wait_until(lambda: _their_peer(bitcoind)["transport_protocol_type"] == "v2")
    assert _their_peer(bitcoind)["session_id"] == session_id.hex()


def _cross_the_rekey(bitcoind: Bitcoind, conn: Connection) -> None:
    """Send `_PINGS` `ping`s, and wait for bitcoind to take and answer them."""
    for nonce in range(_PINGS):
        conn.send(Ping(1 + nonce))
    wait_until(
        lambda: conn.stats.bytes_recv_per_msg["pong"] >= _PINGS * _PONG_SIZE,
        timeout=60,
    )
    wait_until(
        lambda: (
            _their_peer(bitcoind)["bytesrecv_per_msg"].get("ping", 0)
            >= _PINGS * _PONG_SIZE
        ),
        timeout=60,
    )


def test_this_node_dials_bitcoind_over_v2(bitcoind: Bitcoind, tmp_path: Path) -> None:
    """An address advertising `NODE_P2P_V2` is dialled with BIP324."""
    node = _node(tmp_path)
    try:
        node.p2p_manager.connect(
            peer_address(
                "127.0.0.1",
                bitcoind.p2p_port,
                0,
                int(ServiceFlags.NODE_P2P_V2),
            )
        )
        conn = _connected(node)
        _agree_on_v2(bitcoind, conn)
        _cross_the_rekey(bitcoind, conn)
    finally:
        node.stop()
        node.join()


def test_bitcoind_dials_this_node_over_v2(bitcoind: Bitcoind, tmp_path: Path) -> None:
    """A bitcoind that offers v2 reaches this node over it, with no retry."""
    node = _node(tmp_path)
    try:
        bitcoind.rpc("addnode", [f"127.0.0.1:{node.config.p2p_port}", "onetry"])
        conn = _connected(node)
        _agree_on_v2(bitcoind, conn)
        _cross_the_rekey(bitcoind, conn)
    finally:
        node.stop()
        node.join()


def test_bitcoind_dials_this_node_over_v1_where_it_is_asked_to(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """A v1 peer of a node with `-v1transport` is answered in v1."""
    node = _node(tmp_path, v1transport=True)
    try:
        address = f"127.0.0.1:{node.config.p2p_port}"
        bitcoind.rpc("addnode", [address, "onetry", False])
        conn = _connected(node)
        assert _their_peer(bitcoind)["transport_protocol_type"] == "v1"
        assert conn.transport.get_info().transport_type is TransportProtocolType.V1
        assert _their_peer(bitcoind)["session_id"] == ""
    finally:
        node.stop()
        node.join()
