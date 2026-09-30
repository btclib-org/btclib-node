# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A real bitcoind picks this node as a BIP152 high-bandwidth peer.

bitcoind sends `sendcmpct(1, 2)` to a peer that has just given it a new
block (`MaybeSetPeerAsAnnouncingHeaderAndIDs`, `src/net_processing.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag). This node then sends it the next
block as a `cmpctblock` before connecting it (`main.new_pow_valid_block`,
Core's `NewPoWValidBlock`), which bitcoind takes without a `getdata`.
The blocks are handed to this node with `submitblock`, so that they reach
bitcoind from this node alone.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.address import peer_address
from tests import (
    build_block,
    generate_coinbase,
    get_random_port,
    rpc_client,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind


def test_bitcoind_is_announced_a_new_block_as_a_cmpctblock(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """The second block reaches bitcoind as a `cmpctblock`, unasked for."""
    # a recent tip takes both sides out of initial block download, where
    # neither announces a block
    anyone = cast("dict[str, str]", bitcoind.rpc("getdescriptorinfo", ["raw(51)"]))
    bitcoind.rpc("generatetodescriptor", [1, anyone["descriptor"]])
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
        )
    )
    node.start()
    try:
        wait_until_listening(node.p2p_manager)
        wait_until_listening(node.rpc_manager)
        node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))
        wait_until(lambda: len(node.p2p_manager.connections))
        (conn,) = node.p2p_manager.connections.values()
        wait_until(lambda: conn.status == P2pConnStatus.Connected)
        client = rpc_client(node)
        wait_until(lambda: client.call("getblockcount") == 1)

        def theirs() -> dict[str, Any]:
            (entry,) = cast("list[dict[str, Any]]", bitcoind.rpc("getpeerinfo"))
            return entry

        def submit(height: int) -> None:
            tip = bytes.fromhex(str(client.call("getbestblockhash")))
            block = build_block(
                tip,
                [generate_coinbase(height=height)],
                height,
                datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=height),
            )
            client.call("submitblock", [block.serialize(check_validity=False).hex()])
            wait_until(lambda: bitcoind.rpc("getblockcount") == height)

        assert not theirs()["bip152_hb_to"]
        submit(2)
        wait_until(lambda: conn.requested_hb_cmpctblocks)
        assert theirs()["bip152_hb_to"]
        before = theirs()

        submit(3)
        # new_pow_valid_block got past every gate it has before the peers
        assert node.highest_fast_announce == 3
        after = theirs()
        received, sent = "bytesrecv_per_msg", "bytessent_per_msg"
        # absent from `bytesrecv_per_msg` until one arrives
        cmpctblocks = after[received].get("cmpctblock", 0)
        assert cmpctblocks > before[received].get("cmpctblock", 0)
        assert after[sent].get("getdata") == before[sent].get("getdata")
        assert after[received].get("headers") == before[received].get("headers")
    finally:
        node.stop()
        node.join()
