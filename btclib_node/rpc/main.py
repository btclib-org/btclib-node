# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from typing import TYPE_CHECKING, Any

from btclib_node.rpc.callbacks import callbacks
from btclib_node.rpc.errors import RpcError, RpcErrorCode

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import Connection
    from btclib_node.rpc.manager import RpcManager


def get_connection(manager: RpcManager, id: int) -> Connection | None:
    try:
        conn = manager.connections[id]
        return conn
    except Exception:
        return None


def is_valid_rpc(request: Any) -> bool:
    if not isinstance(request, dict):
        return False
    if "method" not in request:
        return False
    if "id" not in request:
        return False
    return True


def error_msg(code: RpcErrorCode, message: str, id: Any = None) -> dict[str, Any]:
    """The error response of JSON-RPC 2.0's section 5, code and message given.

    The specification requires the answer to carry the id of the request
    it answers, and reserves null for a request whose id could not be
    read out of it -- which is what its own example for an invalid
    request object shows. So a caller passes the id wherever
    `is_valid_rpc` has already found one, and leaves it out where the
    request is what was wrong.
    """
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": id,
    }


def handle_rpc(node: Node) -> None:
    data, conn_id = node.rpc_manager.messages.popleft()
    conn = get_connection(node.rpc_manager, conn_id)
    if not conn:
        return

    node.logger.debug(f"Received rpc message: {conn_id}")

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
                if "params" in request:
                    params = request["params"]
                else:
                    params = []
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
