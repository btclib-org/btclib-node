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

from btclib_node.rpc.callbacks import callbacks
from btclib_node.rpc.errors import RpcError, RpcErrorCode, error_msg

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import Connection
    from btclib_node.rpc.manager import RpcManager


def get_connection(manager: RpcManager, connection_id: int) -> Connection | None:
    try:
        return manager.connections[connection_id]
    except KeyError:
        return None


def is_valid_rpc(request: object) -> bool:
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
    data, conn_id = node.rpc_manager.messages.popleft()
    conn = get_connection(node.rpc_manager, conn_id)
    if not conn:
        return

    node.logger.debug("Received rpc message: %s", conn_id)

    response: list[dict[str, Any]] = []
    # JSON-RPC 2.0: an empty batch is itself an invalid request, and
    # the specification's own example for it is this single object. The
    # append is also what keeps `response` from staying empty, which
    # async_send would put on the wire as a bare `[]` -- no unwrapping,
    # since its one-element case does not match, and no valid answer.
    if not data:
        response.append(error_msg(RpcErrorCode.INVALID_REQUEST, "Invalid request"))
    for request in data:
        if not is_valid_rpc(request):
            response.append(error_msg(RpcErrorCode.INVALID_REQUEST, "Invalid request"))
        elif request["method"] not in callbacks:
            response.append(
                error_msg(
                    RpcErrorCode.METHOD_NOT_FOUND, "Method not found", request["id"]
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
                        RpcErrorCode.INTERNAL_ERROR, "Internal Error", request["id"]
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
    node.rpc_manager.connections.pop(conn_id)
