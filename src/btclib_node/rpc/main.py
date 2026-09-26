# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`handle_rpc`, called once per pass of `Node`'s loop.

Pops one decoded request body off `RpcManager.messages`, reads it the
way Core's `HTTPReq_JSONRPC` does (`src/httprpc.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag), and dispatches each request
through `rpc.callbacks.callbacks` by method name. `rpc.jsonrpc` is
where a request is parsed and where the answer's envelope and HTTP
status come from.
"""

from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import RPCErrorCode

from btclib_node.rpc.callbacks import arg_names, callbacks
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.jsonrpc import (
    NO_CONTENT,
    OK,
    HttpReply,
    JsonRpcRequest,
    error_reply,
    error_status,
    transform_named_arguments,
)

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection
    from btclib_node.rpc.manager import RpcManager

__all__ = ["get_connection", "handle_rpc"]


def get_connection(manager: RpcManager, connection_id: int) -> RpcConnection | None:
    """Look up `connection_id` in `manager.connections`, or `None`."""
    try:
        return manager.connections[connection_id]
    except KeyError:
        return None


def _execute(node: Node, conn: RpcConnection, request: JsonRpcRequest) -> object:
    """Run `request`'s method as `CRPCTable::execute` does, or raise `RpcError`.

    A callback raising anything but `RpcError` is a fault of this node:
    it is logged and answered `INTERNAL_ERROR`, where Core answers a C++
    exception with its own message: `RPC_MISC_ERROR` where `JSONRPCExec`
    catches it (`src/rpc/server.cpp`), for a 2.0 request and a batch
    member, and `RPC_PARSE_ERROR` from `HTTPReq_JSONRPC`'s last catch
    for a lone legacy one.

    Named parameters are mapped onto positions once the method is found,
    as `ExecuteCommand` maps them.
    """
    callback = callbacks.get(request.method)
    if callback is None:
        raise RpcError(RPCErrorCode.METHOD_NOT_FOUND, "Method not found")
    params = request.params
    if isinstance(params, dict):
        params = transform_named_arguments(params, arg_names[request.method])
    try:
        return callback(node, conn, params)
    except RpcError:
        raise
    except Exception as e:
        node.logger.exception("Exception occurred")
        raise RpcError(RPCErrorCode.INTERNAL_ERROR, "Internal Error") from e


def _exec(
    node: Node, conn: RpcConnection, request: JsonRpcRequest, *, catch_errors: bool
) -> dict[str, Any]:
    """Answer `request` as `JSONRPCExec` does.

    Where `catch_errors` does not hold, an error is raised to the caller,
    which answers it with an HTTP error status.
    """
    try:
        result = _execute(node, conn, request)
    except RpcError as error:
        if not catch_errors:
            raise
        return request.reply(error=error)
    return request.reply(result)


def _answer_one(
    node: Node, conn: RpcConnection, body: dict[str, Any]
) -> tuple[HttpReply, bool]:
    """Answer a lone request object, and say whether it asked to stop.

    Legacy errors are an HTTP error status; 2.0 errors are HTTP 200, and
    a 2.0 notification is 204 with no body, having run.
    """
    request = JsonRpcRequest()
    try:
        request.parse(body)
        reply = _exec(node, conn, request, catch_errors=request.v2)
    except RpcError as error:
        # Core's `JSONErrorReply` `Assume`s this is never a 2.0 request,
        # which a release build does not enforce, and a 2.0 request
        # `parse` refuses reaches it: `bitcoind` v31.1.0 answers it in
        # the 2.0 envelope with the legacy status
        return HttpReply(error_status(error.code), request.reply(error=error)), False
    stop = request.method == "stop"
    if request.is_notification:
        return HttpReply(NO_CONTENT, None), stop
    return HttpReply(OK, reply), stop


def _answer_batch(
    node: Node, conn: RpcConnection, body: list[Any]
) -> tuple[HttpReply, bool]:
    """Answer a batch, and say whether any member asked to stop.

    Every member is answered inside HTTP 200, whatever its version. One
    `JsonRpcRequest` is parsed into for the whole batch, as in Core, so
    a member refused before its own `id` is read carries the previous
    member's `id` and version -- and is dropped where that previous
    member was a notification.
    """
    request = JsonRpcRequest()
    replies: list[dict[str, Any]] = []
    stop = False
    for member in body:
        try:
            request.parse(member)
            stop = stop or request.method == "stop"
            response = _exec(node, conn, request, catch_errors=True)
        except RpcError as error:
            response = request.reply(error=error)
        if not request.is_notification:
            replies.append(response)
    if body and not replies:
        return HttpReply(NO_CONTENT, None), stop
    return HttpReply(OK, replies), stop


def handle_rpc(node: Node) -> None:
    """Pop one request body off `node.rpc_manager.messages` and answer it.

    An object is a lone request, an array a batch, and anything else is
    `PARSE_ERROR`'s "Top-level object parse error", as in Core. A `stop`
    request's reply is waited on before `node.stop()` runs, so the
    client sees it before the loop it arrived on is torn down.

    `conn_id` is left in `manager.connections`: `RpcConnection.async_send`
    removes it, on the branch that closes `conn`, once `conn` is done
    answering. `conn.send` below only schedules that reply, so a pop here
    would race `async_send` reading the next request off a kept-alive
    connection and remove the entry that request's answer needs
    (issue #688).
    """
    body, conn_id = node.rpc_manager.messages.popleft()
    conn = get_connection(node.rpc_manager, conn_id)
    if not conn:
        return

    node.logger.debug("Received rpc message: %s", conn_id)

    if isinstance(body, dict):
        reply, stop = _answer_one(node, conn, body)
    elif isinstance(body, list):
        reply, stop = _answer_batch(node, conn, body)
    else:
        reply = error_reply(RPCErrorCode.PARSE_ERROR, "Top-level object parse error")
        stop = False

    if stop:
        conn.send_and_wait(reply)
        node.stop()
    else:
        conn.send(reply)
    node.logger.debug("Finished rpc\n")
