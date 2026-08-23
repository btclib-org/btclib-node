# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import contextlib
import json
from pathlib import Path
from typing import Any

import requests

from btclib_node import Node
from btclib_node.config import Config
from tests.helpers import get_random_port, wait_until_listening


def test_no_method(tmp_path: Path) -> None:
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

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "pytest",
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )

    assert response["error"]["message"] == "Invalid request"

    node.stop()


def test_no_id(tmp_path: Path) -> None:
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

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "method": "getpeerinfo",
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )

    assert response["error"]["message"] == "Invalid request"

    node.stop()


def test_invalid_method(tmp_path: Path) -> None:
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

    response = json.loads(
        requests.post(
            url=f"http://127.0.0.1:{node.rpc_port}",
            data=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "method": "notavalidmethod",
                    "id": "pytest",
                }
            ).encode(),
            headers={"Content-Type": "text/plain"},
            timeout=2,
        ).text
    )

    assert response["error"]["message"] == "Method not found"

    node.stop()


def post(node: Node, payload: Any, timeout: float = 5) -> str:
    return requests.post(
        url=f"http://127.0.0.1:{node.rpc_port}",
        data=json.dumps(payload).encode(),
        timeout=timeout,
    ).text


def test_an_empty_batch_does_not_end_the_node(rpc_node: Node) -> None:
    # This is the whole of #55: `[]` is legal JSON and legal JSON-RPC,
    # and it used to leave Node.run by exception -- ending the thread
    # and skipping every close after the loop. The node answering the
    # request after it is what says the loop survived.
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    good = {"jsonrpc": "2.0", "id": "a", "method": "getbestblockhash"}
    assert json.loads(post(node, good))["result"]

    answer = json.loads(post(node, []))
    assert answer["error"]["message"] == "Invalid request"

    assert node.is_alive()
    assert json.loads(post(node, good))["result"]


def test_a_request_the_handler_cannot_read_does_not_end_the_node(
    rpc_node: Node,
) -> None:
    # A method that is not a string reaches `request["method"] not in
    # callbacks` and raises TypeError: unhashable. Node.run's guard is
    # what keeps that to one logged line. It gets no answer, which is
    # its own defect and its own issue -- what is asserted here is only
    # that the node is still there afterwards.
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    with contextlib.suppress(requests.exceptions.RequestException):
        post(node, [{"jsonrpc": "2.0", "id": "a", "method": ["not", "hashable"]}], 2)

    assert node.is_alive()
    good = {"jsonrpc": "2.0", "id": "b", "method": "getbestblockhash"}
    assert json.loads(post(node, good))["result"]
