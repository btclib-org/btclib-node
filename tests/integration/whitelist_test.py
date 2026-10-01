# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`-whitelist` on this node, a bitcoind dialling in: Core's `rpc_setban`.

Core's `rpc_setban.py` restarts its node with `-whitelist=127.0.0.1` and
asserts that a banned peer reconnects holding `noban`, the ban still
listed.
"""

from typing import TYPE_CHECKING, Any, cast

from btclib_node import Node
from btclib_node.config import Config
from tests import get_random_port, rpc_client, wait_until, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind


def test_a_banned_whitelisted_peer_connects_holding_noban(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """The ban stands and the whitelisted peer connects past it.

    The peer is banned before it first connects, so it is the whitelist
    alone that lets it in.
    """
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
            whitelist=["127.0.0.1"],
        )
    )
    node.start()
    try:
        wait_until_listening(node.p2p_manager)
        wait_until_listening(node.rpc_manager)
        client = rpc_client(node)
        client.call("setban", ["127.0.0.1", "add"])
        bitcoind.rpc("addnode", [f"127.0.0.1:{node.p2p_manager.port}", "onetry"])

        def peers() -> list[dict[str, Any]]:
            return cast("list[dict[str, Any]]", client.call("getpeerinfo"))

        wait_until(lambda: len(peers()) == 1)
        (peer,) = peers()
        assert set(peer["permissions"]) == {"noban", "download", "relay", "mempool"}
        assert [entry["address"] for entry in client.call("listbanned")] == [
            "127.0.0.1/32"
        ]
    finally:
        node.stop()
        node.join()
