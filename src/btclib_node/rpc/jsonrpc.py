# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Core's JSON-RPC envelope: what a request is read as, and how it is answered.

Read at bitcoin/bitcoin@9be056a8a7, the v31.1 tag: `JSONRPCRequest`
and `JSONRPCReplyObj` in `src/rpc/request.h` and `src/rpc/request.cpp`,
and `JSONErrorReply` in `src/httprpc.cpp`. Core answers a request by the
version it names. A request with no `jsonrpc`, or with `"1.0"`, gets
the legacy answer: an error is an HTTP error status, and the reply
carries `result` and `error` both. A `"2.0"` request gets the JSON-RPC
2.0 envelope, and HTTP 200 for any error its method raises; a 2.0
request with no `id` is a notification, run and answered with no body.
"""

import json
from typing import Any, NamedTuple, override

from bitcoin_core_rpc import RPCErrorCode

from btclib_node.rpc.errors import RpcError

__all__ = [
    "NO_CONTENT",
    "OK",
    "HttpReply",
    "JsonObject",
    "JsonRpcRequest",
    "decode",
    "error_reply",
    "error_status",
    "transform_named_arguments",
]

OK = "200 OK"
NO_CONTENT = "204 No Content"


class JsonObject(dict[str, Any]):
    """A JSON object as Core's `UniValue` reads one, a key named twice included.

    `UniValue::read` keeps every pair in the order the text gives them,
    and `find_value`, what `JSONRPCRequest::parse` reads each field
    through, answers the first pair holding a key; `UniValue::write`
    writes every pair back (`src/univalue/lib/`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag). So a lookup here answers a key's first value, and
    `items`, which `json.dumps` writes an object from, answers every pair.

    Every other view -- iteration, `keys`, `values`, `len`, `repr` --
    is the plain `dict`'s, one pair per key, and `items` answers the
    pairs as decoded whatever is written in since. A decoded object is
    read, never mutated.
    """

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        """Hold `pairs`, each key looked up as its first value."""
        super().__init__()
        for key, value in pairs:
            self.setdefault(key, value)
        self._pairs = pairs

    @override
    def items(self) -> list[tuple[str, Any]]:  # type: ignore[override]
        return self._pairs


def decode(body: bytes | bytearray) -> Any:  # noqa: ANN401
    """Decode a request body, each object a `JsonObject`.

    Raises `ValueError` where `json.loads` does.
    """
    return json.loads(body, object_pairs_hook=JsonObject)


class HttpReply(NamedTuple):
    """The status line and the JSON body `RpcConnection.async_send` writes.

    `body` is `None` only for `NO_CONTENT`, which carries no body.
    """

    status: str
    body: dict[str, Any] | list[Any] | None


def error_status(code: int) -> str:
    """Return the HTTP status `JSONErrorReply` answers error `code` with."""
    if code == RPCErrorCode.INVALID_REQUEST:
        return "400 Bad Request"
    if code == RPCErrorCode.METHOD_NOT_FOUND:
        return "404 Not Found"
    return "500 Internal Server Error"


class JsonRpcRequest:
    """Core's `JSONRPCRequest`: the fields `parse` reads off a request object.

    `parse` fills them in the order Core's does and raises `RpcError`
    where Core throws, so what a refused request is answered with is
    what `parse` had read by then. A fresh request has a null `id` and
    reads as legacy, which is what a body that is not a request object
    at all is answered with. Core parses every member of a batch into
    one `JSONRPCRequest`, so a member refused before its own `id` is
    read is answered with the previous member's `id` and version;
    `handle_rpc` reuses one of these across a batch for that reason.
    """

    def __init__(self) -> None:
        """Start as Core's does: a null `id`, legacy, and no method yet."""
        self.id: object = None
        self.has_id = True
        self.v2 = False
        self.method = ""
        self.params: list[Any] | dict[str, Any] = []

    def parse(self, request: object) -> None:
        """Read `request` as `JSONRPCRequest::parse` does."""
        if not isinstance(request, dict):
            raise RpcError(RPCErrorCode.INVALID_REQUEST, "Invalid Request object")
        self.has_id = "id" in request
        self.id = request.get("id")
        self.v2 = False
        # JSON null reads as absent here, as `find_value` returns null
        # for a key that is not there
        version = request.get("jsonrpc")
        if version is not None:
            if not isinstance(version, str):
                message = "jsonrpc field must be a string"
                raise RpcError(RPCErrorCode.INVALID_REQUEST, message)
            if version == "2.0":
                self.v2 = True
            elif version != "1.0":
                message = "JSON-RPC version not supported"
                raise RpcError(RPCErrorCode.INVALID_REQUEST, message)
        method = request.get("method")
        if method is None:
            raise RpcError(RPCErrorCode.INVALID_REQUEST, "Missing method")
        if not isinstance(method, str):
            raise RpcError(RPCErrorCode.INVALID_REQUEST, "Method must be a string")
        self.method = method
        params = request.get("params")
        if params is None:
            self.params = []
        elif isinstance(params, list | dict):
            self.params = params
        else:
            message = "Params must be an array or object"
            raise RpcError(RPCErrorCode.INVALID_REQUEST, message)

    @property
    def is_notification(self) -> bool:
        """Whether Core runs this request and answers nothing for it."""
        return self.v2 and not self.has_id

    def reply(
        self, result: object = None, error: RpcError | None = None
    ) -> dict[str, Any]:
        """Build `JSONRPCReplyObj`'s answer to this request.

        2.0 carries `"jsonrpc":"2.0"` and only one of `result` and
        `error`; legacy carries both, one of them null. `id` is written
        only where the request had one.
        """
        reply: dict[str, Any] = {"jsonrpc": "2.0"} if self.v2 else {}
        if error is None:
            reply["result"] = result
            if not self.v2:
                reply["error"] = None
        else:
            if not self.v2:
                reply["result"] = None
            reply["error"] = {"code": error.code.value, "message": error.message}
        if self.has_id:
            reply["id"] = self.id
        return reply


def transform_named_arguments(
    params: dict[str, Any], arg_names: tuple[str, ...]
) -> list[Any]:
    """Map `params`' keys onto positions as `transformNamedArguments` does.

    `src/rpc/server.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag.
    `arg_names` is the method's own, `a|b` two names for one position.
    A position left out ahead of one given is a JSON null, and one left
    out after the last given is not there at all. An `args` array holds
    the leading positions, the named ones filling in after it, and an
    `args` that is not an array is dropped. Every pair `params` holds is
    read, so a key named twice is refused, as it is in Core; so is a
    position given both ways, and a key no position names.

    Core names the first unknown key its `std::unordered_map` iterates
    over, an order the C++ standard leaves to the library, so no fixed
    order matches Core's: `bitcoind` v31.1.0 on macOS names the last of
    them in the request's own order for some sets of keys and not for
    others. The last is the one named here.
    """
    args_in: dict[str, Any] = {}
    for key, value in params.items():
        if key in args_in:
            message = f"Parameter {key} specified multiple times"
            raise RpcError(RPCErrorCode.INVALID_PARAMETER, message)
        args_in[key] = value
    out: list[Any] = []
    hole = 0
    initial_hole_size = 0
    initial_param: str | None = None
    for pattern in arg_names:
        name = next((n for n in pattern.split("|") if n in args_in), None)
        if name is None:
            hole += 1
            if not out:
                initial_hole_size = hole
            continue
        out.extend([None] * hole)
        hole = 0
        if initial_param is None:
            initial_param = pattern
        out.append(args_in.pop(name))
    positional = args_in.pop("args", None)
    if isinstance(positional, list):
        if initial_param is not None and initial_hole_size < len(positional):
            message = (
                f"Parameter {initial_param} specified twice both as positional"
                " and named argument"
            )
            raise RpcError(RPCErrorCode.INVALID_PARAMETER, message)
        out = positional + out[len(positional) :]
    if args_in:
        message = f"Unknown named parameter {next(reversed(args_in))}"
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, message)
    return out


def error_reply(code: RPCErrorCode, message: str) -> HttpReply:
    """Answer an error raised before any request object was read.

    `JSONErrorReply` with a fresh `JSONRPCRequest`: the legacy envelope,
    `"id":null`, and the status `error_status` maps `code` to.
    """
    body = JsonRpcRequest().reply(error=RpcError(code, message))
    return HttpReply(error_status(code), body)
