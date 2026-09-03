# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`handle_rpc`, called once per pass of `Node`'s loop.

Pops one request off `RpcManager.messages`, validates its JSON-RPC
shape with `is_valid_rpc`, and dispatches it through
`rpc.callbacks.callbacks` by method name, answering an unknown method or
a malformed request with an `RpcError` rather than raising past the
loop.
"""

from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import RPCErrorCode

from btclib_node.rpc.callbacks import callbacks
from btclib_node.rpc.errors import RpcError, error_msg

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection
    from btclib_node.rpc.manager import RpcManager

__all__ = ["get_connection", "handle_rpc", "is_valid_rpc"]


def get_connection(manager: RpcManager, connection_id: int) -> RpcConnection | None:
    """Look up `connection_id` in `manager.connections`, or `None`."""
    try:
        return manager.connections[connection_id]
    except KeyError:
        return None


def is_valid_rpc(request: object) -> bool:
    """Check `request` for JSON-RPC 2.0's own required `method` and `id`."""
    if not isinstance(request, dict):
        return False
    if "method" not in request:
        return False
    if not isinstance(request["method"], str):
        # a JSON-RPC method is a name, and JSON admits an array or an
        # object anywhere a string is expected: `request["method"] not
        # in callbacks` below then uses it as a dict key, which raises
        # `TypeError: unhashable type` for either shape, unhandled by
        # `handle_rpc`'s own `except Exception` -- that one guards a
        # callback's own body, not the dispatch in front of it
        return False
    return "id" in request


def handle_rpc(node: Node) -> None:
    """Pop one request batch off `node.rpc_manager.messages` and answer it.

    Validates each request in the batch with `is_valid_rpc`, dispatches
    a valid one by method name through `rpc.callbacks.callbacks`, and
    answers an unknown method, an invalid request or a raising callback
    with a JSON-RPC error rather than raising past `Node`'s own loop --
    except a `stop` request, whose own reply is waited on before
    `node.stop()` runs, so the client sees it before the loop it arrived
    on is torn down.

    `conn_id` is left in `manager.connections` -- `RpcConnection.async_send`
    is what removes it, on the branch that actually closes `conn`, once
    `conn` is done answering rather than the instant this function has
    merely scheduled that answer. This function used to pop it here,
    unconditionally, on the theory that every reply eventually closes;
    once a reply could keep the connection open instead (issue #640),
    that pop raced `async_send`'s own re-entry into `RpcConnection.run`
    for the *next* request on the same kept-alive connection, on
    `RpcManager`'s own thread -- `conn.send` below only schedules
    `async_send`, it does not wait for it, so nothing orders this
    function's own next line against how far across that coroutine the
    other thread has already run by the time it executes. Where
    `async_send` won the race -- wrote the reply, re-armed `conn` and
    read the next request whole, all inside one burst neither
    `sock_sendall` nor an already-buffered `sock_recv` had to suspend
    for -- this function's pop then removed the entry `async_send` had
    just put back for that next request's own benefit, not the stale one
    it was meant to remove, and the request already queued behind it was
    answered by nobody: `rpc.main.get_connection` found no connection
    for it and `handle_rpc` silently returned, which is what a client
    pooling one connection across many calls
    (`tests/functional/rpc/connections_test.py`'s
    `test_many_unpaced_calls_over_one_session_transport_do_not_reset`)
    saw as one call in a few hundred stalling for its own full timeout
    with nothing logged on either side (issue #688).
    """
    data, conn_id = node.rpc_manager.messages.popleft()
    conn = get_connection(node.rpc_manager, conn_id)
    if not conn:
        return

    node.logger.debug("Received rpc message: %s", conn_id)

    response: list[dict[str, Any]] = []
    # An empty batch -- `data == []` only where the client's own JSON
    # was `[]`, `run` wrapping every lone object into a one-element list
    # before this is ever reached -- adds nothing here and the loop
    # below runs zero times, so `response` reaches `async_send` empty.
    # `RpcConnection.is_batch`, `True` for this request the same as for
    # any other array, is what keeps that from being written back
    # unwrapped: `response` stays `[]` on the wire, matching Core's own
    # `ExecuteHTTPRPC`, which answers an empty client-sent array with an
    # empty array too (`src/httprpc.cpp:135-185`, at
    # bitcoin/bitcoin@ca7162cde5) rather than the single `Invalid
    # request` object a literal reading of JSON-RPC 2.0 section 6's own
    # wording would give it (issue #669).
    for request in data:
        if not is_valid_rpc(request):
            response.append(error_msg(RPCErrorCode.INVALID_REQUEST, "Invalid request"))
        elif request["method"] not in callbacks:
            response.append(
                error_msg(
                    RPCErrorCode.METHOD_NOT_FOUND, "Method not found", request["id"]
                )
            )
        else:
            try:
                params = request.get("params", [])
                response.append(
                    {
                        "jsonrpc": "2.0",
                        "result": callbacks[request["method"]](node, conn, params),
                        "id": request["id"],
                    }
                )
            # a callback naming its own refusal, which is the request
            # being wrong and not this node: logged as nothing, since a
            # client asking for what is not there is not an event of the
            # node's
            except RpcError as error:
                response.append(error_msg(error.code, error.message, request["id"]))
            except Exception:
                node.logger.exception("Exception occurred")
                response.append(
                    error_msg(
                        RPCErrorCode.INTERNAL_ERROR, "Internal Error", request["id"]
                    )
                )

    # asked of the batch, not of whichever request came last: reading
    # the loop variable after the loop is unbound on an empty batch and
    # is a str, not a dict, on a batch ending in one.
    if any(is_valid_rpc(request) and request["method"] == "stop" for request in data):
        conn.send_and_wait(response)
        node.stop()
    else:
        conn.send(response)
    node.logger.debug("Finished rpc\n")
