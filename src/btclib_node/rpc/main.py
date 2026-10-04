# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`handle_rpc` and `resume_rpc`, called on each pass of `Node`'s loop.

`handle_rpc` pops one decoded request body off `RpcManager.messages`,
reads it the way Core's `HTTPReq_JSONRPC` does (`src/httprpc.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag), and dispatches each request
through `rpc.callbacks.callbacks` by method name. `rpc.jsonrpc` is
where a request is parsed and where the answer's envelope and HTTP
status come from.

Core runs each request on an HTTP worker thread of its own, so a method
that waits for the tip, or searches nonces, holds that thread alone.
Here every method runs on `Node`'s thread, which owns the state it
reads (`ARCHITECTURE.md`). Such a method returns a generator instead of
its result: each step yields `True` where it did work and `False` where
it only checked whether to go on waiting, and the generator returns the
result. `handle_rpc` runs a request's first step; a request not answered
by then waits in `Node.pending_rpc`, and `resume_rpc` runs one step of
each such request on every pass, so the loop goes on serving peers,
other requests and `stop` between steps. Once `Node.terminate_flag` is
set, each of these methods ends at its next step, as Core's ends on
`m_interrupt`.
"""

import time
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import RPCErrorCode

from btclib_node.rpc.callbacks import arg_names, callbacks, stop_wait_param
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import HELP_TEXT
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

__all__ = ["Job", "answer_warmup", "get_connection", "handle_rpc", "resume_rpc"]

# A request being answered: it yields as a waiting method does, and
# returns the reply, whether to stop the node and the delay before it.
type Job = Generator[bool, None, tuple[HttpReply, bool, float]]


def get_connection(manager: RpcManager, connection_id: int) -> RpcConnection | None:
    """Look up `connection_id` in `manager.connections`, or `None`."""
    try:
        return manager.connections[connection_id]
    except KeyError:
        return None


def _execute(
    node: Node, conn: RpcConnection, request: JsonRpcRequest
) -> Generator[bool, None, object]:
    """Run `request`'s method as `CRPCTable::execute` does, or raise `RpcError`.

    A callback raising anything but `RpcError` is a fault of this node:
    it is logged and answered `INTERNAL_ERROR`, where Core answers a C++
    exception with its own message: `RPC_MISC_ERROR` where `JSONRPCExec`
    catches it (`src/rpc/server.cpp`), for a 2.0 request and a batch
    member, and `RPC_PARSE_ERROR` from `HTTPReq_JSONRPC`'s last catch
    for a lone legacy one.

    While the node is in warmup that is the first thing it does, whatever
    the method, as `CRPCTable::execute` does (`src/rpc/server.cpp`, same
    tag): `RPC_IN_WARMUP` and the message `RpcManager.warmup_status` holds.

    Named parameters are mapped onto positions once the method is found,
    as `ExecuteCommand` maps them.

    A call carrying more positional arguments than the method declares
    is refused here, before the callback ever runs -- `IsValidNumArgs`'s
    own `num_args <= m_args.size()` (`src/rpc/util.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), checked once for every
    method rather than by each callback's own body: `RPCMethod::
    HandleRequest` makes this same check ahead of every declared
    argument's own type check, and ahead of the handler running at all,
    for every method alike, so there is exactly one place for it here
    too. `arg_names[request.method]`'s own length is that method's
    declared count, named positions and positional-only alike, which is
    what `IsValidNumArgs` compares `num_args` against. The refusal
    itself is `HelpResult{ToString()}`, caught by `ExecuteCommand`
    (`src/rpc/server.cpp:874-887`, at bitcoin/bitcoin@b91d983f66) and
    turned into `RPC_MISC_ERROR` carrying the method's own full help
    text, matching `disconnect_node`'s own such check before this
    function carried it for every method (btclib-org/btclib-node#1424).

    A method that waits returns a generator, which this runs to its end,
    yielding as it yields (the module docstring).
    """
    manager = node.rpc_manager
    if manager.in_warmup:
        raise RpcError(RPCErrorCode.IN_WARMUP, manager.warmup_status)
    callback = callbacks.get(request.method)
    if callback is None:
        raise RpcError(RPCErrorCode.METHOD_NOT_FOUND, "Method not found")
    # `rpc.callbacks.get_rpc_info`'s own `active_commands`, appended for
    # exactly the span Core's `RPCCommandExecution` covers: from here,
    # the method already resolved to a command as `ExecuteCommand` is
    # only ever reached for one, to this function's own return or raise,
    # which is where that guard's destructor runs
    # (`src/rpc/server.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    # tag) -- the argument-count refusal below included, `HandleRequest`
    # throwing its own `HelpResult` from inside that same guard's scope.
    # Removed by value, since a waiting request can end after one that
    # started later; two equal entries are interchangeable.
    command = (request.method, time.monotonic())
    node.active_rpc_commands.append(command)
    try:
        params = request.params
        if isinstance(params, dict):
            params = transform_named_arguments(params, arg_names[request.method])
        if len(params) > len(arg_names[request.method]):
            raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT[request.method])
        try:
            result = callback(node, conn, params)
            if isinstance(result, Generator):
                result = yield from result
        except RpcError:
            raise
        except Exception as e:
            node.logger.exception("Exception occurred")
            raise RpcError(RPCErrorCode.INTERNAL_ERROR, "Internal Error") from e
        return result
    finally:
        node.active_rpc_commands.remove(command)


def _exec(
    node: Node, conn: RpcConnection, request: JsonRpcRequest, *, catch_errors: bool
) -> Generator[bool, None, dict[str, Any]]:
    """Answer `request` as `JSONRPCExec` does.

    Where `catch_errors` does not hold, an error is raised to the caller,
    which answers it with an HTTP error status.
    """
    try:
        result = yield from _execute(node, conn, request)
    except RpcError as error:
        if not catch_errors:
            raise
        return request.reply(error=error)
    return request.reply(result)


def _stop_delay_seconds(request: JsonRpcRequest) -> float:
    """Read a successful `stop` call's own `wait`, in seconds, or `0.0`.

    Only ever called once `request.method == "stop"` has already been
    dispatched and answered without error, so the read below cannot
    raise: `stop`'s own callback already ran `stop_wait_param` on the
    same value and would have refused the call otherwise. The
    named-argument transform is repeated rather than read off
    `_execute`'s own local variable, which reaches nowhere outside that
    function -- `request.params` itself is still whatever shape the
    request gave it (btclib-org/btclib-node#1467).
    """
    params = request.params
    if isinstance(params, dict):
        params = transform_named_arguments(params, arg_names["stop"])
    wait_ms = stop_wait_param(params)
    return 0.0 if wait_ms is None else max(0, wait_ms) / 1000


def _answer_one(node: Node, conn: RpcConnection, body: dict[str, Any]) -> Job:
    """Answer a lone request object, and say whether -- and how late -- to stop.

    Legacy errors are an HTTP error status; 2.0 errors are HTTP 200, and
    a 2.0 notification is 204 with no body, having run.

    `stop` is true only where `reply` carries no error: a refused call
    -- too many arguments, an unknown named one, any `RpcError` `_exec`
    catches -- never reaches `stop`'s own handler, matching Core's
    `stop()` (`src/rpc/server.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag), whose handler calls `shutdown_request` only once
    `RPCMethod::HandleRequest`'s own argument checks have passed
    (`src/rpc/util.cpp`, same tag): a throw from those checks propagates
    out of `ExecuteCommand` before the handler lambda is ever entered
    (btclib-org/btclib-node#1441). The delay `stop`'s own `wait` asks
    for is read only once `stop` is already known true
    (btclib-org/btclib-node#1467).
    """
    request = JsonRpcRequest()
    try:
        request.parse(body)
        reply = yield from _exec(node, conn, request, catch_errors=request.v2)
    except RpcError as error:
        # Core's `JSONErrorReply` `Assume`s this is never a 2.0 request,
        # which a release build does not enforce, and a 2.0 request
        # `parse` refuses reaches it: `bitcoind` v31.1.0 answers it in
        # the 2.0 envelope with the legacy status
        return (
            HttpReply(error_status(error.code), request.reply(error=error)),
            False,
            0.0,
        )
    stop = request.method == "stop" and reply.get("error") is None
    delay = _stop_delay_seconds(request) if stop else 0.0
    if request.is_notification:
        return HttpReply(NO_CONTENT, None), stop, delay
    return HttpReply(OK, reply), stop, delay


def _answer_batch(node: Node, conn: RpcConnection, body: list[Any]) -> Job:
    """Answer a batch, and say whether -- and how late -- to stop.

    Every member is answered inside HTTP 200, whatever its version. One
    `JsonRpcRequest` is parsed into for the whole batch, as in Core, so
    a member refused before its own `id` is read carries the previous
    member's `id` and version -- and is dropped where that previous
    member was a notification.

    As in `_answer_one`, a member only sets `stop` where its own answer
    carries no error (btclib-org/btclib-node#1441): a member whose
    `request.parse` itself raises is answered from the previous member's
    still-held method and version and never sets `stop`, since it is not
    that method's own reply. `stop` latches: once one member has set it,
    a later member's own `wait` -- or refusal -- is never read, the same
    way a later member's method name never was before it either.

    Members run in order, as Core runs them on one worker: a member that
    waits holds back the ones after it, and the reply. Where `stop`
    latches, `terminate_flag` is set at once, so a later member that
    waits ends at its next step, as `bitcoind` answers both together
    and exits; `_step` still sends the reply and calls `node.stop()`.
    """
    request = JsonRpcRequest()
    replies: list[dict[str, Any]] = []
    stop = False
    delay = 0.0
    for member in body:
        try:
            request.parse(member)
            response = yield from _exec(node, conn, request, catch_errors=True)
        except RpcError as error:
            response = request.reply(error=error)
        else:
            if not stop and request.method == "stop" and response.get("error") is None:
                stop = True
                delay = _stop_delay_seconds(request)
                # a later member that waits ends at its next step
                node.terminate_flag.set()
        if not request.is_notification:
            replies.append(response)
    if body and not replies:
        return HttpReply(NO_CONTENT, None), stop, delay
    return HttpReply(OK, replies), stop, delay


def _answer(node: Node, conn: RpcConnection, body: object) -> Job:
    """Answer a request body: an object, an array, or neither."""
    if isinstance(body, dict):
        return (yield from _answer_one(node, conn, body))
    if isinstance(body, list):
        return (yield from _answer_batch(node, conn, body))
    reply = error_reply(RPCErrorCode.PARSE_ERROR, "Top-level object parse error")
    return reply, False, 0.0


def _step(node: Node, conn: RpcConnection, job: Job) -> bool | None:
    """Run `job` one step: `None` once it is answered, else what it yielded."""
    try:
        return next(job)
    except StopIteration as done:
        reply, stop, delay = done.value
    if stop:
        if delay:
            conn.send_and_close_after(reply, delay)
        else:
            conn.send_and_wait(reply)
        node.stop()
    else:
        conn.send(reply)
    node.logger.log_debug("rpc", "Finished rpc\n")
    return None


def answer_warmup(node: Node, conn: RpcConnection, body: object) -> HttpReply:
    """Answer `body` while the node is in warmup, from the RPC manager's thread.

    `Node`'s thread is opening the stores then, so `RpcConnection.run`
    calls this instead of queuing the request. `_execute` refuses at its
    first line, so no step yields and no callback runs here.
    """
    job = _answer(node, conn, body)
    while True:
        try:
            next(job)
        except StopIteration as done:
            reply: HttpReply = done.value[0]
            return reply


def handle_rpc(node: Node) -> None:
    """Pop one request body off `node.rpc_manager.messages` and answer it.

    An object is a lone request, an array a batch, and anything else is
    `PARSE_ERROR`'s "Top-level object parse error", as in Core. A `stop`
    request's reply reaches the client before the loop it arrived on is
    torn down: waited on before `node.stop()` runs, or, where `stop`
    carries a positive `wait`, handed to
    `RpcConnection.send_and_close_after`, whose delayed write
    `RpcManager.stop` finishes, with `node.stop()` run at once -- Core's
    own `stop` requests shutdown before it sleeps
    (btclib-org/btclib-node#1467).

    A request still waiting after its first step goes to
    `node.pending_rpc`, for `resume_rpc`.

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

    node.logger.log_debug("rpc", "Received rpc message: %s", conn_id)
    job = _answer(node, conn, body)
    if _step(node, conn, job) is not None:
        node.pending_rpc.append((conn, job))


def resume_rpc(node: Node) -> bool:
    """Run each request in `node.pending_rpc` one step, answering those done.

    Answers whether any step did work, an answer included, which keeps
    `Node`'s loop from sleeping. Each answer pushes `rpc_manager`'s reply
    deadline forward, as `Node._drain_message_queues` does for a request
    `handle_rpc` answers (btclib-org/btclib-node#1651). A step that raises
    drops its request, which is taken off the queue first.
    """
    worked = False
    for _ in range(len(node.pending_rpc)):
        conn, job = node.pending_rpc.popleft()
        step = _step(node, conn, job)
        if step is None:
            node.rpc_manager.extend_reply_deadline(time.monotonic())
            worked = True
        else:
            node.pending_rpc.append((conn, job))
            worked = worked or step
    return worked
