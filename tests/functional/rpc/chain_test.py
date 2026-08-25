# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import json
from typing import TYPE_CHECKING, Any

import requests
from bitcoin_core_rpc import BitcoinCoreRpcClient
from btclib.fetch.bitcoin_core import BitcoinCoreFetcher

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from tests.helpers import (
    generate_random_chain,
    generate_random_header_chain,
    get_random_port,
    post,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_best_block_hash(rpc_node: Node) -> None:
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

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "getbestblockhash",
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )

    assert response["result"] == header_chain[-1].hash.hex()


def test_block_hash(rpc_node: Node) -> None:
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

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "getblockhash",
                    "params": [50],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )
    assert response["result"] == header_chain[50 - 1].hash.hex()


def test_block_count(rpc_node: Node) -> None:
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

    response = json.loads(
        post(node, {"jsonrpc": "1.0", "id": "pytest", "method": "getblockcount"})
    )
    assert response["result"] == 10


def test_blockchain_info_names_the_chain_btclib_s_fetcher_checks(
    rpc_node: Node,
) -> None:
    # btclib-org/btclib-node#21: BitcoinCoreFetcher.assert_network reads
    # this by default, before the fetch it was actually asked for --
    # measured against a real BitcoinCoreFetcher, not asserted
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    response = json.loads(
        post(node, {"jsonrpc": "1.0", "id": "pytest", "method": "getblockchaininfo"})
    )
    assert response["result"] == {"chain": "regtest"}


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
    request = {
        "jsonrpc": "1.0",
        "id": "pytest",
        "method": "getblockheader",
        "params": [block_hash],
    }
    return json.loads(post(node, request))["result"]


def test_block_header_on_the_chain_the_node_validated(rpc_node: Node) -> None:
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
    # a node that has taken headers and downloaded nothing: its active
    # chain is the genesis alone, so every one of these is off it and
    # none of them is confirmed by anything
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
