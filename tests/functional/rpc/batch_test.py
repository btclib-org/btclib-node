# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A well-formed, multi-member JSON-RPC 2.0 batch, over a real node.

`tests/unit/rpc/main_test.py` and `tests/unit/rpc/connection_test.py`'s
own `test_a_batch_is_dispatched_as_it_arrived` already pin `handle_rpc`'s
per-member dispatch and `RpcConnection`'s framing of a batch built
directly as a list; what neither reaches is a real client posting one,
over a real socket, through `BitcoinCoreRpcClient.call_batch` (issue
#642).
"""

import json
from typing import TYPE_CHECKING

from bitcoin_core_rpc import RpcError, RPCErrorCode, SessionTransport

from btclib_node import Node
from btclib_node.config import Config
from tests import get_random_port, post, rpc_client, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path


def test_a_batch_is_answered_in_the_order_it_was_sent(rpc_node: Node) -> None:
    """A well-formed batch's raw reply array follows its own request order.

    `handle_rpc`'s own dispatch (`rpc/main.py`) builds `response` with
    one `append` per member, in the order `data` -- the batch as it
    arrived -- iterates, so this is a property of this listener's own
    implementation and not one JSON-RPC 2.0 itself requires (section 6
    lets replies arrive in any order, matched by id): `call_batch`
    already reorders by id and would not tell the two apart, so this
    reads the raw envelope instead.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    batch = [
        {"jsonrpc": "2.0", "id": "a", "method": "getblockcount"},
        {"jsonrpc": "2.0", "id": "b", "method": "ping"},
        {"jsonrpc": "2.0", "id": "c", "method": "getbestblockhash"},
    ]
    answer = json.loads(post(node, batch))

    assert [entry["id"] for entry in answer] == ["a", "b", "c"]
    assert answer[0]["result"] == 0
    assert answer[1]["result"] is None
    assert answer[2]["result"]


def test_a_one_member_batch_still_answers_as_an_array(rpc_node: Node) -> None:
    """A batch of exactly one member's own reply stays an array.

    `call_batch`'s own `_batch_reply_array` raises `FetchError` where
    the reply is not a json array (`bitcoin_core_rpc.client`) -- this
    used to be exactly that, `RpcConnection.async_send` unwrapping a
    one-member batch's reply the same way it unwraps a lone request's,
    with nothing left by then to tell the two apart. Matches Core's own
    `ExecuteHTTPRPC`, which answers an array of any size as an array
    (`HTTPReq_JSONRPC`, `src/httprpc.cpp:135-169`, at
    bitcoin/bitcoin@ca7162cde5) -- never unwrapped for having only one
    member (issue #653).
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    client = rpc_client(node)

    results = client.call_batch([("ping", None)])

    assert results == [None]


def test_a_mixed_valid_and_invalid_batch_answers_each_member_on_its_own(
    rpc_node: Node,
) -> None:
    """A batch mixing a valid and an unknown method answers each on its own.

    `call_batch` correlates each reply by id and hands back a result or
    an `RpcError` **as a value** per member, so a batch partly failing
    is read the same way a caller reads it: nothing here raises for the
    unknown method, and the valid ones beside it still answer normally.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    client = rpc_client(node)

    results = client.call_batch(
        [("getbestblockhash", None), ("notavalidmethod", None), ("ping", None)]
    )

    assert results[0]
    assert isinstance(results[1], RpcError)
    assert results[1].code == RPCErrorCode.METHOD_NOT_FOUND
    assert results[2] is None


def test_stop_anywhere_in_a_batch_answers_every_member_then_stops(
    tmp_path: Path,
) -> None:
    """`stop` inside a batch does not cut the batch short, and still stops.

    `handle_rpc` checks the whole batch for a `stop` request only once
    every member has been dispatched (`rpc/main.py`), so a request
    placed after it in the array is still answered rather than skipped
    the way it would be had dispatch broken out on `stop` instead. A
    node of its own, not `rpc_node`: this test stops it from inside the
    batch, and `node.stop()` below is what waits for that rather than
    asking a second time (`init_test.py::test_init` is the same shape).
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
    client = rpc_client(node)

    results = client.call_batch(
        [("ping", None), ("stop", None), ("getbestblockhash", None)]
    )

    assert results[0] is None
    assert results[1] == "Btclib node stopping"
    assert results[2]

    node.stop()
    assert not node.is_alive()


def test_a_batch_and_a_call_after_it_share_one_connection(rpc_node: Node) -> None:
    """A batch reply does not end the connection: a call after it reuses it.

    `RpcConnection.async_send` (issue #640) keeps the socket open across
    replies the same way for a batch's own reply as for a single one, so
    a `SessionTransport` sending a batch and then an ordinary call finds
    the same connection still open for the second.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    client = rpc_client(node)
    transport = SessionTransport()
    client.transport = transport

    try:
        results = client.call_batch([("ping", None), ("getbestblockhash", None)])
        assert results[0] is None
        assert results[1]

        _, body = client.call_raw("getblockcount", jsonrpc="2.0")
        assert body["result"] == 0
    finally:
        transport.close()

    assert node.rpc_manager.last_connection_id == 0
