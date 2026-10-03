# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A bitcoind that dials a node without `-v2transport` falls back to v1.

That node speaks v1 only. A v2-capable bitcoind dialling it first sends
its 64-byte ellswift key, which this node cannot parse and drops;
bitcoind's `ShouldReconnectV1` then has it redial the same address with
v1 (btclib-org/btclib-node#1197).
"""

from typing import TYPE_CHECKING, Any, cast

from btclib_node import Node
from btclib_node.config import Config
from tests import get_random_port, wait_until, wait_until_listening

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
