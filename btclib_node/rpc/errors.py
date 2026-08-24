# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""How a callback refuses a request rather than failing on it."""

import enum
from typing import Any


class RpcErrorCode(enum.IntEnum):
    """The codes of Bitcoin Core's RPCErrorCode, `src/rpc/protocol.h`.

    A client reads the code before it reads the message, so a refusal
    this node makes carries the number Core gives the same refusal.
    `INTERNAL_ERROR` is what Core's own header reserves for a genuine
    fault of the server, which is why nothing here answers a bad request
    with it.
    """

    MISC_ERROR = -1
    TYPE_ERROR = -3
    INVALID_ADDRESS_OR_KEY = -5
    INVALID_PARAMETER = -8
    VERIFY_ERROR = -25
    VERIFY_REJECTED = -26
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INTERNAL_ERROR = -32603


class RpcError(Exception):
    """A request this node refuses, named by the answer it is owed.

    `handle_rpc` turns it into the error object of JSON-RPC 2.0's section
    5.1, so raising it is how a callback says which of the two was wrong,
    the request or the node.
    """

    def __init__(self, code: RpcErrorCode, message: str) -> None:
        super().__init__(f"{code.name}: {message}")
        self.code = code
        self.message = message


# univalue's own names for the six JSON types, `uvTypeName` in
# src/univalue/lib/univalue.cpp -- the vocabulary RPC_TYPE_ERROR's
# message speaks. Keyed by `type()` rather than `isinstance`, so a bool
# reads as "bool" and not "number": bool is int's own subclass in
# Python, not a distinct JSON type, and `type()` is exact where
# `isinstance` would let the subclass through.
_JSON_TYPE_NAMES: dict[type, str] = {
    type(None): "null",
    bool: "bool",
    int: "number",
    float: "number",
    str: "string",
    list: "array",
    dict: "object",
}


def json_type_name(value: Any) -> str:
    """Name a decoded JSON value the way Core's RPC_TYPE_ERROR names it.

    `value` is always one of the six JSON types here: `connection.py`
    decodes every request with the standard library's `json.loads`,
    which produces no other Python type.
    """
    return _JSON_TYPE_NAMES[type(value)]


def error_msg(code: RpcErrorCode, message: str, id: Any = None) -> dict[str, Any]:
    """The error response of JSON-RPC 2.0's section 5, code and message given.

    The specification requires the answer to carry the id of the request
    it answers, and reserves null for a request whose id could not be
    read out of it -- which is what its own example for an invalid
    request object shows. So a caller passes the id wherever
    `is_valid_rpc` has already found one, and leaves it out where the
    request -- or, for `PARSE_ERROR`, the body before it was even a
    request -- is what was wrong.
    """
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": id,
    }


def bool_param(params: list[Any], position: int, *, default: bool) -> bool:
    """Read a declared `RPCArg::Type::BOOL` parameter, Core's own way.

    Omitted or explicit `null` both stand for the argument's own
    declared `default`. Anything else is read, and refused with
    `RPC_TYPE_ERROR` where it is not an actual JSON bool -- the same
    check `RPCMethod::HandleRequest` makes for every declared argument
    before the handler body runs at all (`src/rpc/util.cpp:653-661`),
    applied here to the one JSON type this helper's every caller
    declares.
    """
    if len(params) <= position or params[position] is None:
        return default
    value = params[position]
    if not isinstance(value, bool):
        raise RpcError(
            RpcErrorCode.TYPE_ERROR,
            f"JSON value of type {json_type_name(value)} is not of expected type bool",
        )
    return value
