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

import json
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import http_request

from tests import authorization, post, rpc_client, wait_until_listening

if TYPE_CHECKING:
    from btclib_node import Node


def test_no_method(rpc_node: Node) -> None:
    """A request with no method is answered Core's "Missing method", live."""
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    # `call_raw` always sets its own `method`, so a request missing the
    # key entirely has nothing to build it with -- `post` stays on the
    # raw envelope for that reason
    response = json.loads(post(node, {"jsonrpc": "1.0", "id": "pytest"}))

    assert response["error"] == {"code": -32600, "message": "Missing method"}


def test_no_id(rpc_node: Node) -> None:
    """A legacy request with no id is run, and answered with no id, live."""
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    # `call_raw` always sets its own `id`, so a request missing the key
    # entirely has nothing to build it with either
    response = json.loads(post(node, {"jsonrpc": "1.0", "method": "getpeerinfo"}))

    assert response == {"result": [], "error": None}


def test_invalid_method(rpc_node: Node) -> None:
    """An unknown method is answered Method not found, live.

    404 for a legacy request and 200 for a 2.0 one, as `bitcoind` v31.1.0
    answers them (issue #1109).
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    client = rpc_client(node)

    for jsonrpc, expected in (("1.0", 404), (None, 404), ("2.0", 200)):
        status, body = client.call_raw(
            "notavalidmethod", jsonrpc=jsonrpc, request_timeout=2
        )
        assert status == expected
        assert body["error"]["message"] == "Method not found"


def test_a_legacy_request_core_refuses_is_answered_its_refusal(
    rpc_node: Node,
) -> None:
    """A legacy request Core's parse refuses gets its refusal (issue #1109).

    `post` stays on the raw envelope: `call_raw` refuses `params` that
    are not an array or an object before anything is sent.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    request = {"id": 1, "method": "getblockcount", "params": 5}
    assert json.loads(post(node, request)) == {
        "result": None,
        "error": {"code": -32600, "message": "Params must be an array or object"},
        "id": 1,
    }


def test_an_empty_batch_answers_an_empty_array(rpc_node: Node) -> None:
    """An empty batch is answered `[]`, matching Core; the node's loop survives.

    `[]` is legal JSON and legal JSON-RPC, and it used to leave
    `Node.run` by exception -- ending the thread and skipping every
    close after the loop (issue #55); the node answering the request
    after it is what says the loop survived, still. What it answers
    changed since: this used to be a single `Invalid request` object, a
    literal reading of JSON-RPC 2.0 section 6's own wording for a batch
    that "fails to be recognized... as an Array with at least one
    value" -- but Core's own `ExecuteHTTPRPC` does not read it that way,
    answering an empty client-sent array with an empty array instead
    (`src/httprpc.cpp:135-185`, at bitcoin/bitcoin@ca7162cde5), and
    CLAUDE.md's Following Bitcoin Core section leaves no room for a
    convention of this tree's own on an axis Core's own behaviour
    already decides (issue #669).
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    client = rpc_client(node)

    _, body = client.call_raw("getbestblockhash", jsonrpc="2.0")
    assert body["result"]

    # neither `call_raw` (one object per post) nor `call_batch` (refuses
    # an empty `calls`) can send a bare `[]`, so this stays on `post`
    answer = json.loads(post(node, []))
    assert answer == []

    assert node.is_alive()
    _, body = client.call_raw("getbestblockhash", jsonrpc="2.0")
    assert body["result"]


def test_a_request_the_handler_cannot_read_does_not_end_the_node(
    rpc_node: Node,
) -> None:
    """A method that is not a string is answered, and the node survives it.

    `JSONRPCRequest::parse` refuses it, and inside a batch that refusal
    is the member's answer. `call_raw` refuses a non-string `method`
    itself, before anything is sent (`BtcRpcTypeError`) -- a conformance
    case its own docstring disclaims -- so this stays on `post`.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    request = {"jsonrpc": "2.0", "id": "a", "method": ["not", "hashable"]}
    assert json.loads(post(node, [request], 2)) == [
        {
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Method must be a string"},
            "id": "a",
        }
    ]

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


def test_an_object_argument_is_named_an_object(rpc_node: Node) -> None:
    """`getblockhash` given an object is Core's type error, live (issue #1151).

    Measured against a real `bitcoind` v31.1.0: -3, naming the object
    `object`, a key named twice in it or not.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    message = (
        'Wrong type passed:\n{\n    "Position 1 (height)": "JSON value of type '
        'object is not of expected type number"\n}'
    )
    # raw bytes rather than `post`'s `json.dumps`, which cannot write a
    # key twice
    for argument in (b"{}", b'{"a":1,"a":2}'):
        _, body = http_request(
            f"http://127.0.0.1:{node.rpc_port}",
            data=b'{"jsonrpc":"2.0","id":1,"method":"getblockhash","params":[%s]}'
            % argument,
            headers={"Authorization": authorization(node.config.data_dir)},
            timeout=5,
        )
        assert json.loads(body)["error"] == {"code": -3, "message": message}

    assert node.is_alive()
