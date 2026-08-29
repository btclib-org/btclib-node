# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The chain-reading RPC methods, over a real node: hashes, headers, count.

`getbestblockhash`, `getblockhash`, `getblockcount`, `getblockheader`
and `getblockchaininfo`, each driven against a node that has actually
validated and connected the chain it is asked about -- except for
`getblockchaininfo`'s own `headers` member, which the header-sync test
below drives against a node given headers and no blocks at all, that
gap being the member's whole reason for existing.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import BitcoinCoreRpcClient
from btclib.fetch.bitcoin_core import BitcoinCoreFetcher

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from tests import (
    build_block,
    generate_coinbase,
    generate_random_chain,
    generate_random_header_chain,
    get_random_port,
    rpc_client,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_best_block_hash(rpc_node: Node) -> None:
    """getbestblockhash, live, answers the connected chain's own tip."""
    node = rpc_node

    wait_until_listening(node.rpc_manager)

    chain = generate_random_chain(100, RegTest().genesis.hash)
    header_chain = [block.header for block in chain]
    block_index = node.chainstate.block_index
    block_index.add_headers(header_chain)
    node.status = NodeStatus.HeaderSynced

    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    wait_until(lambda: len(block_index.active_chain) == 100 + 1)

    _, body = rpc_client(node).call_raw(
        "getbestblockhash", jsonrpc="1.0", request_timeout=2
    )

    assert body["result"] == header_chain[-1].hash.hex()


def test_block_hash(rpc_node: Node) -> None:
    """getblockhash, over a real socket, answers the hash at a given height."""
    node = rpc_node

    wait_until_listening(node.rpc_manager)

    chain = generate_random_chain(100, RegTest().genesis.hash)
    header_chain = [block.header for block in chain]
    block_index = node.chainstate.block_index
    block_index.add_headers(header_chain)
    node.status = NodeStatus.HeaderSynced

    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    wait_until(lambda: len(block_index.active_chain) == 100 + 1)

    _, body = rpc_client(node).call_raw(
        "getblockhash", [50], jsonrpc="1.0", request_timeout=2
    )
    assert body["result"] == header_chain[50 - 1].hash.hex()


def test_block_count(rpc_node: Node) -> None:
    """getblockcount, live, answers the connected chain's own height."""
    node = rpc_node

    wait_until_listening(node.rpc_manager)

    chain = generate_random_chain(10, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    node.status = NodeStatus.HeaderSynced

    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    wait_until(lambda: len(block_index.active_chain) == 10 + 1)

    _, body = rpc_client(node).call_raw("getblockcount", jsonrpc="1.0")
    assert body["result"] == 10


def test_blockchain_info_names_the_chain_btclib_s_fetcher_checks(
    rpc_node: Node,
) -> None:
    """`getblockchaininfo` answers the field the fetcher checks by default.

    `BitcoinCoreFetcher.assert_network` reads this before the fetch it
    was actually asked for -- measured against a real
    `BitcoinCoreFetcher`, not asserted (issue #21).
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    _, body = rpc_client(node).call_raw("getblockchaininfo", jsonrpc="1.0")
    assert body["result"]["chain"] == "regtest"


def test_blockchain_info_s_headers_moves_during_header_sync_while_blocks_does_not(
    rpc_node: Node,
) -> None:
    """Header sync is observable in `headers` alone, which is issue #575.

    Ten headers are indexed and none of their blocks downloaded, so
    `active_chain` never grows past the genesis while `header_index`
    grows by every one of them -- the same gap `getblockcount` alone
    cannot show, `blocks` not moving at all while a sync it has nothing
    to do with the active chain is in progress.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    _, before = rpc_client(node).call_raw("getblockchaininfo", jsonrpc="1.0")
    assert before["result"]["blocks"] == 0
    assert before["result"]["headers"] == 0
    assert before["result"]["initialblockdownload"] is True

    header_chain = generate_random_header_chain(10, RegTest().genesis.hash)
    node.chainstate.block_index.add_headers(header_chain)

    wait_until(lambda: len(node.chainstate.block_index.header_index) == 10 + 1)

    _, after = rpc_client(node).call_raw("getblockchaininfo", jsonrpc="1.0")
    assert after["result"]["blocks"] == 0
    assert after["result"]["headers"] == 10
    assert after["result"]["initialblockdownload"] is True


def test_blockchain_info_s_initialblockdownload_flips_off_once_caught_up_and_recent(
    rpc_node: Node,
) -> None:
    """A regtest node leaves IBD once its own tip is actually recent.

    `RegTest.minimum_chain_work` is 0, trivially met by any block, so
    the tip's own age against `MAX_TIP_AGE` is what this test is
    actually exercising -- `generate_random_chain`'s own blocks all
    carry `GENESIS_TIME`'s 2011 timestamp, which the header-sync test
    above is content with and this one is not: its own single block is
    built directly, timestamped against the real clock.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    genesis = RegTest().genesis
    block = build_block(
        genesis.hash,
        [generate_coinbase(value=RegTest().subsidy(1), height=1)],
        0,
        time=datetime.now(UTC),
    )
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header])
    node.status = NodeStatus.HeaderSynced
    node.block_db.add_block(block)
    block_index.set_downloaded(block.header.hash)

    wait_until(lambda: node.is_initial_block_download is False)

    _, body = rpc_client(node).call_raw("getblockchaininfo", jsonrpc="1.0")
    assert body["result"]["blocks"] == 1
    assert body["result"]["initialblockdownload"] is False


def test_bitcoin_core_fetcher_works_against_this_node_unchanged(
    rpc_node: Node,
) -> None:
    """btclib-org/btclib-node#21, the issue's own title, tested literally.

    The client is btclib's own `BitcoinCoreFetcher`, pointed at this
    node with no adapter -- `verify_network` at its default `True`, so
    `getblockchaininfo` is exercised here too, not only the two methods
    the issue names.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    chain = generate_random_chain(3, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    node.status = NodeStatus.HeaderSynced
    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    wait_until(lambda: len(block_index.active_chain) == 3 + 1)

    # no cookie file on disk here, so credentials rather than
    # `cookie_path` -- this node checks neither, having no
    # authentication of its own (#27's own finding)
    client = BitcoinCoreRpcClient(
        f"http://127.0.0.1:{node.rpc_port}",
        user="pytest",
        password="pytest",  # noqa: S106
    )
    fetcher = BitcoinCoreFetcher(client, network="regtest")

    assert fetcher.get_best_block_id() == chain[-1].header.hash
    assert fetcher.get_block_count() == 3


def get_block_header(node: Node, block_hash: str) -> Any:
    """Call getblockheader on `block_hash`, verbose by default."""
    _, body = rpc_client(node).call_raw("getblockheader", [block_hash], jsonrpc="1.0")
    return body["result"]


def test_block_header_on_the_chain_the_node_validated(rpc_node: Node) -> None:
    """getblockheader, live, names a validated block's own neighbours."""
    node = rpc_node

    wait_until_listening(node.rpc_manager)

    chain = generate_random_chain(100, RegTest().genesis.hash)
    block_index = node.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    node.status = NodeStatus.HeaderSynced

    for block in chain:
        node.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)

    wait_until(lambda: len(block_index.active_chain) == 100 + 1)

    # a depth counts the tip itself, so the tip is one deep and the
    # block halfway along is as deep as the chain above it is long
    tip = get_block_header(node, chain[-1].header.hash.hex())
    assert tip["hash"] == chain[-1].header.hash.hex()
    assert tip["height"] == 100
    assert tip["confirmations"] == 1
    assert tip["previousblockhash"] == chain[-2].header.hash.hex()
    assert "nextblockhash" not in tip

    middle = get_block_header(node, chain[49].header.hash.hex())
    assert middle["height"] == 50
    assert middle["confirmations"] == 51
    assert middle["previousblockhash"] == chain[48].header.hash.hex()
    assert middle["nextblockhash"] == chain[50].header.hash.hex()


def test_block_header_of_a_block_the_node_has_not_validated(tmp_path: Path) -> None:
    """`getblockheader` answers -1 confirmations for a header not yet validated.

    A node that has taken headers and downloaded nothing has an active
    chain that is the genesis alone, so every one of these is off it and
    none of them is confirmed by anything.
    """
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
        )
    )
    node.start()

    wait_until_listening(node.rpc_manager)

    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    node.chainstate.block_index.add_headers(chain)
    assert node.chainstate.block_index.active_chain == [RegTest().genesis.hash]

    last = get_block_header(node, chain[-1].hash.hex())
    assert last["hash"] == chain[-1].hash.hex()
    assert last["height"] == 2000
    assert last["confirmations"] == -1
    assert last["previousblockhash"] == chain[-2].hash.hex()
    assert "nextblockhash" not in last

    # the header index holds the block after this one, and the answer
    # names it only where the node has validated both
    middle = get_block_header(node, chain[-1001].hash.hex())
    assert middle["hash"] == chain[-1001].hash.hex()
    assert middle["height"] == 1000
    assert middle["confirmations"] == -1
    assert middle["previousblockhash"] == chain[-1002].hash.hex()
    assert "nextblockhash" not in middle

    node.stop()
