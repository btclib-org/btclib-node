# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What a malformed or refused JSON-RPC request answers with, over a real node.

A missing method or id, an unknown method, an empty batch, a method
name of the wrong JSON type, and a refusal a callback raises on
purpose -- each checked to answer the client without ending the node's
own loop.
"""

import contextlib
import json
from typing import TYPE_CHECKING, Any

import requests

from btclib_node import Node
from btclib_node.config import Config
from tests import get_random_port, post, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path


def test_no_method(tmp_path: Path) -> None:
    """A request with no method is answered Invalid request, live."""
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
    """A request with no id is answered Invalid request, live."""
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
    """A request naming an unknown method is answered Method not found, live."""
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


def test_an_empty_batch_does_not_end_the_node(rpc_node: Node) -> None:
    """An empty batch is answered Invalid request, and the node's loop survives.

    `[]` is legal JSON and legal JSON-RPC, and it used to leave
    `Node.run` by exception -- ending the thread and skipping every
    close after the loop. The node answering the request after it is
    what says the loop survived (issue #55).
    """
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
    """A request whose method raises inside the dispatch does not end the node.

    A method that is not a string reaches `request["method"] not in
    callbacks` and raises `TypeError: unhashable`. `Node.run`'s guard is
    what keeps that to one logged line. It gets no answer, which is its
    own defect and its own issue -- what is asserted here is only that
    the node is still there afterwards.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    with contextlib.suppress(requests.exceptions.RequestException):
        post(node, [{"jsonrpc": "2.0", "id": "a", "method": ["not", "hashable"]}], 2)

    assert node.is_alive()
    good = {"jsonrpc": "2.0", "id": "b", "method": "getbestblockhash"}
    assert json.loads(post(node, good))["result"]


def test_a_request_the_node_can_refuse_is_not_answered_internal_error(
    rpc_node: Node,
) -> None:
    """A request the node itself refuses carries Core's code, not -32603.

    A hash nothing indexed, a parameter that is not hex and no parameter
    at all are the client being wrong, and each carries the code Bitcoin
    Core gives it. -32603 is what this node owes a fault of its own, so
    a client can still tell a typo from a broken node (issue #179).
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    def refusal(params: list[Any]) -> Any:
        request = {
            "jsonrpc": "2.0",
            "id": "a",
            "method": "getblockheader",
            "params": params,
        }
        answer = json.loads(post(node, request))
        assert answer["id"] == "a"
        return answer["error"]

    assert refusal(["11" * 32]) == {"code": -5, "message": "Block not found"}
    assert refusal(["zz"])["code"] == -8
    assert refusal([])["code"] == -1

    assert node.is_alive()


def test_a_missing_argument_is_not_answered_internal_error(rpc_node: Node) -> None:
    """`testmempoolaccept` and `sendrawtransaction` short of an argument, live.

    Both used to reach `params[0]` unguarded, raising `IndexError` for an
    empty `params` -- caught only by `handle_rpc`'s own catch-all, which
    answers `-32603 Internal Error`, the code this node owes its own
    fault rather than a call short of a required argument (issue #443).
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    def refusal(method: str, params: list[Any]) -> Any:
        request = {"jsonrpc": "2.0", "id": "a", "method": method, "params": params}
        answer = json.loads(post(node, request))
        assert answer["id"] == "a"
        return answer["error"]

    assert refusal("testmempoolaccept", []) == {
        "code": -1,
        "message": 'testmempoolaccept ["rawtx",...] ( maxfeerate )',
    }
    assert refusal("testmempoolaccept", ["not an array"]) == {
        "code": -3,
        "message": (
            'Wrong type passed:\n{\n    "Position 1 (rawtxs)": "JSON value '
            'of type string is not of expected type array"\n}'
        ),
    }
    assert refusal("sendrawtransaction", []) == {
        "code": -1,
        "message": 'sendrawtransaction "hexstring" ( maxfeerate maxburnamount )',
    }

    assert node.is_alive()
