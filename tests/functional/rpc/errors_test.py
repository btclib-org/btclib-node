# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What a malformed or refused JSON-RPC request answers with, over a real node.

A missing method or id, an unknown method, an empty batch, a method
name of the wrong JSON type, and a refusal a callback raises on
purpose -- each checked to answer the client without ending the node's
own loop.

Most of these are driven through `BitcoinCoreRpcClient.call_raw`, which
sends a request identical to `post`'s own construction but interprets
nothing of the reply. Two shapes it cannot build stay on `post`: a
request missing `method` or `id` -- `call_raw` always carries both --
and the bare `[]` empty batch, which is legal JSON-RPC but not a shape
either `call_raw` (one object) or `call_batch` (refuses an empty
`calls`) can send.
"""

import contextlib
import json
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import FetchError

from btclib_node import Node
from btclib_node.config import Config
from tests import get_random_port, post, rpc_client, wait_until_listening

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

    # `call_raw` always sets its own `method`, so a request missing the
    # key entirely has nothing to build it with -- `post` stays on the
    # raw envelope for that reason
    response = json.loads(post(node, {"jsonrpc": "1.0", "id": "pytest"}))

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

    # `call_raw` always sets its own `id`, so a request missing the key
    # entirely has nothing to build it with either
    response = json.loads(post(node, {"jsonrpc": "1.0", "method": "getpeerinfo"}))

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

    _, body = rpc_client(node).call_raw(
        "notavalidmethod", jsonrpc="1.0", request_timeout=2
    )

    assert body["error"]["message"] == "Method not found"

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
    client = rpc_client(node)

    _, body = client.call_raw("getbestblockhash", jsonrpc="2.0")
    assert body["result"]

    # neither `call_raw` (one object per post) nor `call_batch` (refuses
    # an empty `calls`) can send a bare `[]`, so this stays on `post`
    answer = json.loads(post(node, []))
    assert answer["error"]["message"] == "Invalid request"

    assert node.is_alive()
    _, body = client.call_raw("getbestblockhash", jsonrpc="2.0")
    assert body["result"]


def test_a_request_the_handler_cannot_read_does_not_end_the_node(
    rpc_node: Node,
) -> None:
    """A request whose method raises inside the dispatch does not end the node.

    A method that is not a string reaches `request["method"] not in
    callbacks` and raises `TypeError: unhashable`. `Node.run`'s guard is
    what keeps that to one logged line. It gets no answer, which is its
    own defect and its own issue -- what is asserted here is only that
    the node is still there afterwards.

    `call_raw` refuses a non-string `method` itself, before anything is
    sent (`BtcRpcTypeError`) -- a conformance case its own docstring
    disclaims -- so this stays on `post`, and the timeout this used to
    read as a `requests` exception is `http_request`'s own `FetchError`.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    with contextlib.suppress(FetchError):
        post(node, [{"jsonrpc": "2.0", "id": "a", "method": ["not", "hashable"]}], 2)

    assert node.is_alive()
    _, body = rpc_client(node).call_raw("getbestblockhash", jsonrpc="2.0")
    assert body["result"]


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
    client = rpc_client(node)

    def refusal(params: list[Any]) -> Any:
        # `call_raw` mints its own `id`, so the reply is read straight
        # off this one HTTP exchange rather than correlated by a value
        # this caller chose -- there is no second reply it could be
        _, body = client.call_raw("getblockheader", params, jsonrpc="2.0")
        return body["error"]

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
    client = rpc_client(node)

    def refusal(method: str, params: list[Any]) -> Any:
        _, body = client.call_raw(method, params, jsonrpc="2.0")
        return body["error"]

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
