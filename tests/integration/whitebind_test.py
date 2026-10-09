# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`-whitebind` on this node, a bitcoind dialling in.

The peer's address is banned, so the permissions its listener grants
are what lets it in.
"""

from typing import TYPE_CHECKING, Any, cast

from btclib_node import Node
from btclib_node.config import Config
from tests import get_random_port, rpc_client, wait_until, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind


def test_a_peer_of_a_whitebind_listener_holds_its_permissions(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """A banned peer reaches a `noban` listener, holding what it grants."""
    white = get_random_port()
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
            bind=[f"127.0.0.1:{get_random_port()}"],
            whitebind=[f"noban,addr@127.0.0.1:{white}"],
        )
    )
    node.start()
    try:
        wait_until_listening(node.p2p_manager)
        wait_until_listening(node.rpc_manager)
        client = rpc_client(node)
        client.call("setban", ["127.0.0.1", "add"])
        bitcoind.rpc("addnode", [f"127.0.0.1:{white}", "onetry"])

        seen: list[dict[str, Any]] = []

        def handshaken() -> bool:
            seen[:] = cast("list[dict[str, Any]]", client.call("getpeerinfo"))
            return (
                len(seen) == 1
                and seen[0]["version"] != 0
                and seen[0]["transport_protocol_type"] != "detecting"
            )

        wait_until(handshaken)
        assert set(seen[0]["permissions"]) == {"noban", "download", "addr"}
    finally:
        node.stop()
        node.join()
