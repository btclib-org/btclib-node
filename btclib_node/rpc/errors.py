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
