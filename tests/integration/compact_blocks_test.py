# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A real bitcoind and this node pick each other as BIP152 high-bandwidth peers.

Each sends `sendcmpct(1, 2)` to a peer that has just given it a new
block (`MaybeSetPeerAsAnnouncingHeaderAndIDs`, `src/net_processing.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag), and that peer then sends it the
next block as a `cmpctblock` before connecting it (Core's
`NewPoWValidBlock`, here `main.new_pow_valid_block`), taken without a
`getdata`. The first test hands the blocks to this node with
`submitblock`, so that they reach bitcoind from this node alone; the
second mines them in bitcoind.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from btclib.tx.limits import COINBASE_MATURITY
from btclib.tx.tx import Tx

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.address import peer_address
from tests import (
    anyone_can_spend,
    build_block,
    generate_coinbase,
    generate_random_transaction,
    get_random_port,
    rpc_client,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind


def raw(tx: Tx) -> str:
    """Return `tx` as `sendrawtransaction` and `generateblock` take it."""
    return tx.serialize(include_witness=True).hex()


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


def test_a_block_of_bitcoind_s_is_received_as_a_cmpctblock(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """This node rebuilds bitcoind's block, and then picks bitcoind.

    The first block carries a transaction this node holds and one it
    lacks, so it takes a `cmpctblock` and a `blocktxn`. Once it is
    connected, this node sends bitcoind `sendcmpct(1, 2)`, and bitcoind
    sends the next block as a `cmpctblock` unasked.
    """
    info = cast(
        "dict[str, str]",
        bitcoind.rpc("getdescriptorinfo", [f"raw({anyone_can_spend().hex()})"]),
    )
    descriptor = info["descriptor"]
    hashes = cast(
        "list[str]",
        bitcoind.rpc("generatetodescriptor", [COINBASE_MATURITY + 2, descriptor]),
    )
    coinbases = [
        Tx.parse(
            cast("dict[str, Any]", bitcoind.rpc("getblock", [h, 2]))["tx"][0]["hex"]
        )
        for h in hashes[:2]
    ]
    held, lacked = (
        generate_random_transaction(coinbase.id, coinbase.vout[0].value - 10_000)
        for coinbase in coinbases
    )
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
        wait_until(lambda: client.call("getblockcount") == len(hashes))
        wait_until(lambda: not node.is_initial_block_download)
        client.call("sendrawtransaction", [raw(held)])

        def ours() -> dict[str, Any]:
            entries = cast("list[dict[str, Any]]", client.call("getpeerinfo"))
            # bitcoind may also dial the address this node advertised
            (entry,) = (entry for entry in entries if entry["id"] == conn.id)
            return entry

        before = ours()
        bitcoind.rpc("generateblock", [descriptor, [raw(held), raw(lacked)]])
        wait_until(lambda: client.call("getblockcount") == len(hashes) + 1)
        after = ours()
        received, sent = "bytesrecv_per_msg", "bytessent_per_msg"
        for kind, command in (
            (received, "cmpctblock"),
            (received, "blocktxn"),
            (sent, "getblocktxn"),
        ):
            assert after[kind].get(command, 0) > before[kind].get(command, 0)
        assert after[received].get("block") == before[received].get("block")

        def chosen_by_us() -> bool:
            entries = cast("list[dict[str, Any]]", bitcoind.rpc("getpeerinfo"))
            return any(entry["bip152_hb_from"] for entry in entries)

        wait_until(chosen_by_us)
        assert ours()["bip152_hb_to"]

        before = ours()
        spend = generate_random_transaction(held.id, held.vout[0].value - 10_000)
        client.call("sendrawtransaction", [raw(spend)])
        bitcoind.rpc("generateblock", [descriptor, [raw(spend)]])
        wait_until(lambda: client.call("getblockcount") == len(hashes) + 2)
        after = ours()
        assert after[received]["cmpctblock"] > before[received]["cmpctblock"]
        for command in ("getdata", "getblocktxn"):
            assert after[sent].get(command) == before[sent].get(command)
    finally:
        node.stop()
        node.join()
