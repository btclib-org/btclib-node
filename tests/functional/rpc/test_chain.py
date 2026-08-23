# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import json
from pathlib import Path

import requests

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from tests.helpers import (
    generate_random_chain,
    generate_random_header_chain,
    get_random_port,
    wait_until,
    wait_until_listening,
)


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
        block_info = block_index.get_block_info(block.header.hash)
        block_info.downloaded = True
        block_index.insert_block_info(block_info)

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
        block_info = block_index.get_block_info(block.header.hash)
        block_info.downloaded = True
        block_index.insert_block_info(block_info)

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


def test_block_header_last(tmp_path: Path) -> None:
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

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "getblockheader",
                    "params": [chain[-1].hash.hex()],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )

    assert response["result"]["hash"] == chain[-1].hash.hex()
    assert response["result"]["height"] == 2000
    assert response["result"]["previousblockhash"] == chain[-2].hash.hex()
    assert "nextblockhash" not in response["result"]

    node.stop()


def test_block_header_middle(tmp_path: Path) -> None:
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

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                    "method": "getblockheader",
                    "params": [chain[-1001].hash.hex()],
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )

    assert response["result"]["hash"] == chain[-1001].hash.hex()
    assert response["result"]["height"] == 1000
    assert response["result"]["previousblockhash"] == chain[-1002].hash.hex()
    assert response["result"]["nextblockhash"] == chain[-1000].hash.hex()

    node.stop()
