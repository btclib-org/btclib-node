# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""getconnectioncount, getpeerinfo, addnode and getnetworkinfo, two nodes.

Each test connects two live nodes over p2p and asks one of them, over
its own RPC socket, what its p2p side reports about the other.
"""

from typing import TYPE_CHECKING

from btclib_node.constants import P2pConnStatus
from tests import (
    local_addr,
    rpc_client,
    wait_until,
    wait_until_listening,
)
from tests.conftest import node_context

if TYPE_CHECKING:
    from pathlib import Path


def test_get_connection_count(tmp_path: Path) -> None:
    """getconnectioncount, live, counts a peer dialled and handshaken."""
    with (
        node_context(tmp_path / "node1") as node1,
        node_context(tmp_path / "node2") as node2,
    ):
        # both listeners of both nodes: the RPC one because the assertion is
        # asked over it, and the P2P one because the dial below is what the
        # assertion is about. They are bound by two threads that do not wait
        # for each other, so the RPC socket being up says nothing about the
        # P2P socket -- and a dial that arrives first is refused once and
        # silently.
        wait_until_listening(node1.rpc_manager)
        wait_until_listening(node2.rpc_manager)
        wait_until_listening(node1.p2p_manager)
        wait_until_listening(node2.p2p_manager)

        node2.p2p_manager.connect(local_addr(node1.p2p_port))
        # each side's own `connections` only holds a peer past its own
        # `verack`, and the two handshakes complete independently, so each
        # is waited for on its own rather than assuming one implies the other
        wait_until(lambda: len(node1.p2p_manager.connections))
        connection = node1.p2p_manager.connections[0]
        wait_until(lambda: connection.status == P2pConnStatus.Connected)
        wait_until(lambda: len(node2.p2p_manager.connections))
        connection = node2.p2p_manager.connections[0]
        wait_until(lambda: connection.status == P2pConnStatus.Connected)

        _, body = rpc_client(node1).call_raw(
            "getconnectioncount", jsonrpc="1.0", request_timeout=2
        )

        assert body["result"] == 1


def test_get_peer_info(tmp_path: Path) -> None:
    """getpeerinfo, live, names each side's own view of the other peer.

    Checked from both ends of one connection: the dialling node's own
    answer names the peer inbound `False`, and the accepting node's
    names it `True`, with `addr`/`addrbind`/`addrlocal` swapped between
    the two views of the same socket pair.
    """
    with (
        node_context(tmp_path / "node1") as node1,
        node_context(tmp_path / "node2") as node2,
    ):
        # see test_get_connection_count above: the P2P listeners as well,
        # because the dial is what this asserts on
        wait_until_listening(node1.rpc_manager)
        wait_until_listening(node2.rpc_manager)
        wait_until_listening(node1.p2p_manager)
        wait_until_listening(node2.p2p_manager)

        node2.p2p_manager.connect(local_addr(node1.p2p_port))
        # each side's own `connections` only holds a peer past its own
        # `verack`, and the two handshakes complete independently, so each
        # is waited for on its own rather than assuming one implies the other
        wait_until(lambda: len(node1.p2p_manager.connections))
        connection = node1.p2p_manager.connections[0]
        wait_until(lambda: connection.status == P2pConnStatus.Connected)
        wait_until(lambda: len(node2.p2p_manager.connections))
        connection = node2.p2p_manager.connections[0]
        wait_until(lambda: connection.status == P2pConnStatus.Connected)

        local_port = node1.p2p_manager.connections[0].client.getpeername()[1]

        _, body = rpc_client(node2).call_raw(
            "getpeerinfo", jsonrpc="1.0", request_timeout=2
        )
        assert body["result"][0]["id"] == 0
        assert body["result"][0]["addr"] == f"127.0.0.1:{node1.p2p_port}"
        assert body["result"][0]["addrbind"] == f"127.0.0.1:{local_port}"
        assert body["result"][0]["addrlocal"] == f"127.0.0.1:{local_port}"
        assert body["result"][0]["network"] == "not_publicly_routable"
        # network | witness | compact_filters | network_limited, which
        # is what own_version advertises, over the 64-bit field
        assert body["result"][0]["services"] == "0000000000000449"
        assert body["result"][0]["servicesnames"] == [
            "NETWORK",
            "WITNESS",
            "COMPACT_FILTERS",
            "NETWORK_LIMITED",
        ]
        assert not body["result"][0]["inbound"]

        _, body = rpc_client(node1).call_raw(
            "getpeerinfo", jsonrpc="1.0", request_timeout=2
        )
        assert body["result"][0]["id"] == 0
        assert body["result"][0]["addr"] == f"127.0.0.1:{local_port}"
        assert body["result"][0]["addrbind"] == f"127.0.0.1:{node1.p2p_port}"
        assert body["result"][0]["addrlocal"] == f"127.0.0.1:{node1.p2p_port}"
        assert body["result"][0]["network"] == "not_publicly_routable"
        # network | witness | compact_filters | network_limited, which
        # is what own_version advertises, over the 64-bit field
        assert body["result"][0]["services"] == "0000000000000449"
        assert body["result"][0]["servicesnames"] == [
            "NETWORK",
            "WITNESS",
            "COMPACT_FILTERS",
            "NETWORK_LIMITED",
        ]
        assert body["result"][0]["inbound"]


def test_addnode_onetry_dials_and_connects_the_other_node(tmp_path: Path) -> None:
    """`addnode "host:port" "onetry"`, live, is `connect_nodes`'s own dial.

    `test_framework.py`'s own `connect_nodes` (`:568-594`, at
    bitcoin/bitcoin@bb529657) calls exactly this shape -- an IP:port
    literal, the string `"onetry"` -- to wire two nodes together, and
    this is that call driven over a real RPC socket rather than through
    `P2pManager.connect` directly the way `test_get_connection_count`
    and `test_get_peer_info` above do.
    """
    with (
        node_context(tmp_path / "node1") as node1,
        node_context(tmp_path / "node2") as node2,
    ):
        wait_until_listening(node1.rpc_manager)
        wait_until_listening(node2.rpc_manager)
        wait_until_listening(node1.p2p_manager)
        wait_until_listening(node2.p2p_manager)

        _, body = rpc_client(node2).call_raw(
            "addnode",
            [f"127.0.0.1:{node1.p2p_port}", "onetry"],
            jsonrpc="1.0",
            request_timeout=2,
        )
        assert body["result"] is None

        wait_until(lambda: len(node1.p2p_manager.connections))
        wait_until(
            lambda: node1.p2p_manager.connections[0].status == P2pConnStatus.Connected
        )
        wait_until(lambda: len(node2.p2p_manager.connections))
        wait_until(
            lambda: node2.p2p_manager.connections[0].status == P2pConnStatus.Connected
        )


def test_get_network_info_s_subversion_matches_what_a_peer_sees(
    tmp_path: Path,
) -> None:
    """`getnetworkinfo`'s own `subversion` is what a peer's `subver` names.

    `connect_nodes` matches the two, off each node's own RPC socket, to
    tell its own connection apart from any other in a peer's list --
    this is that match, checked directly.
    """
    with (
        node_context(tmp_path / "node1") as node1,
        node_context(tmp_path / "node2") as node2,
    ):
        wait_until_listening(node1.rpc_manager)
        wait_until_listening(node2.rpc_manager)
        wait_until_listening(node1.p2p_manager)
        wait_until_listening(node2.p2p_manager)

        node2.p2p_manager.connect(local_addr(node1.p2p_port))
        wait_until(lambda: len(node1.p2p_manager.connections))
        wait_until(
            lambda: node1.p2p_manager.connections[0].status == P2pConnStatus.Connected
        )

        _, network_info = rpc_client(node2).call_raw(
            "getnetworkinfo", jsonrpc="1.0", request_timeout=2
        )
        _, peer_info = rpc_client(node1).call_raw(
            "getpeerinfo", jsonrpc="1.0", request_timeout=2
        )

        assert peer_info["result"][0]["subver"] == network_info["result"]["subversion"]
