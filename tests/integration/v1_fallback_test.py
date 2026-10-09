# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A bitcoind that dials a node without `-v2transport` falls back to v1.

That node speaks v1 only. A v2-capable bitcoind dialling it first sends
its 64-byte ellswift key, which this node cannot parse and drops;
bitcoind's `ShouldReconnectV1` then has it redial the same address with
v1 (btclib-org/btclib-node#1197).

The other way round, a bitcoind run with `-v2transport=0` drops this
node's key, and this node retries with v1 as Core does.
"""

from typing import TYPE_CHECKING, Any, cast

from btclib.p2p.address import ServiceFlags

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.address import peer_address
from btclib_node.p2p.transport import TransportProtocolType
from tests import LogLines, get_random_port, wait_until, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

_RETRY = "retrying with v1 transport protocol"


def test_bitcoind_falls_back_to_v1_when_dialling_this_node(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """`addnode onetry` ends in a v1 peer, after bitcoind logged the retry."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
            v2transport=False,
        )
    )
    node.start()
    try:
        wait_until_listening(node.p2p_manager)
        # `net` is the category the retry is logged under
        bitcoind.rpc("logging", [["net"]])
        bitcoind.rpc("addnode", [f"127.0.0.1:{node.config.p2p_port}", "onetry"])

        def peers() -> list[dict[str, Any]]:
            return cast("list[dict[str, Any]]", bitcoind.rpc("getpeerinfo"))

        log_path = tmp_path / "bitcoind" / "regtest" / "debug.log"
        wait_until(lambda: _RETRY in log_path.read_text(), timeout=30)
        wait_until(
            lambda: [p["transport_protocol_type"] for p in peers()] == ["v1"],
            timeout=30,
        )
    finally:
        node.stop()
        node.join()


def test_bitcoind_dialling_in_v1_is_refused_without_v1transport(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """`addnode onetry false` is dropped and logged, and no peer is made."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
            v1transport=False,
            # the refusal is a debug line
            debug=True,
        )
    )
    node.start()
    try:
        wait_until_listening(node.p2p_manager)
        lines = LogLines()
        node.logger.addHandler(lines)
        bitcoind.rpc("addnode", [f"127.0.0.1:{node.config.p2p_port}", "onetry", False])
        wait_until(
            lambda: any(
                "V2 transport error: V1 peer refused (see -v1transport)" in line
                for line in lines.messages
            ),
            timeout=30,
        )
        assert not node.p2p_manager.connections
        # a refused connection waits `Closed` for the housekeeping loop
        wait_until(
            lambda: all(
                c.status is P2pConnStatus.Closed
                for c in node.p2p_manager.pending_connections.values()
            )
        )
        # bitcoind sees the close after we make it
        wait_until(lambda: bitcoind.rpc("getpeerinfo") == [], timeout=30)
    finally:
        node.stop()
        node.join()


def test_this_node_falls_back_to_v1_when_dialling_bitcoind(
    bitcoind_v1_only: Bitcoind, tmp_path: Path
) -> None:
    """An address advertising `NODE_P2P_V2` ends in v1 after a retry."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
            # the retry is a debug line
            debug=True,
            # the retry is v1
            v1transport=True,
        )
    )
    node.start()
    try:
        wait_until_listening(node.p2p_manager)
        lines = LogLines()
        node.logger.addHandler(lines)
        node.p2p_manager.connect(
            peer_address(
                "127.0.0.1",
                bitcoind_v1_only.p2p_port,
                0,
                int(ServiceFlags.NODE_P2P_V2),
            )
        )
        wait_until(lambda: any(_RETRY in line for line in lines.messages), timeout=30)
        wait_until(
            lambda: (
                [
                    c.status
                    for c in node.p2p_manager.connections.values()
                    if c.transport.get_info().transport_type is TransportProtocolType.V1
                ]
                == [P2pConnStatus.Connected]
            ),
            timeout=30,
        )
        peers = cast("list[dict[str, Any]]", bitcoind_v1_only.rpc("getpeerinfo"))
        assert [p["transport_protocol_type"] for p in peers] == ["v1"]
    finally:
        node.stop()
        node.join()
