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
    DESERIALIZATION_ERROR = -22
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
        """Name the refusal `code` and `message`, `handle_rpc` reads back."""
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


def json_type_name(value: object) -> str:
    """Name a decoded JSON value the way Core's RPC_TYPE_ERROR names it.

    `value` is always one of the six JSON types here: `connection.py`
    decodes every request with the standard library's `json.loads`,
    which produces no other Python type. Named that narrowly in the
    docstring rather than in the signature: `_JSON_TYPE_NAMES` is
    keyed on `type(value)` alone, so nothing this function does needs
    `value` to be any narrower than `object`.
    """
    return _JSON_TYPE_NAMES[type(value)]


def type_error(position: int, name: str, value: object, expected: str) -> RpcError:
    """Refuse a declared argument's own JSON type, Core's own wrapped shape.

    `RPCMethod::HandleRequest`'s own type check (`src/rpc/util.cpp`
    :652-661, read at `bitcoin/bitcoin@b91d983f66`) collects every
    mismatched argument into one `UniValue` object, keyed
    `strprintf("Position %s (%s)", i + 1, arg.m_names)`, and wraps it in
    `strprintf("Wrong type passed:\\n%s", arg_mismatch.write(4))` --
    `UniValue::write`'s own four-space indent and lack of a trailing
    newline after the closing brace
    (`src/univalue/lib/univalue_write.cpp`), reproduced literally below
    rather than through a JSON encoder, because every caller here checks
    exactly one declared argument and raises before a second could ever
    join it in the same object -- there is never a second key to encode.
    Measured against a real `bitcoind` (v31.1.0, `-regtest`) answering a
    raw `testmempoolaccept`, `getblockheader`, `getblockhash`,
    `getrawtransaction` and `sendrawtransaction` call each with one
    argument of the wrong JSON type.

    `position` is the argument's own one-based position among the
    method's declared arguments, the way Core counts it (`i + 1`), and
    `name` is Core's own declared name for it -- `arg.m_names` itself,
    the raw field the key above is built from, not `GetFirstName()`'s
    `|`-trimmed form (`m_names.substr(0, m_names.find('|'))`,
    `src/rpc/util.cpp:917-920`), which only `RPCArg::ToString` reads,
    for the usage string, and which `HandleRequest`'s own type check
    never calls. The two coincide for every argument checked here except
    `getrawtransaction`'s own second one, declared `"verbosity|verbose"`
    -- `get_raw_transaction`'s own `verbose` is neither the raw
    `m_names` this key is built from nor `GetFirstName()`'s trimmed
    form, for the reason `_parse_txid`'s own usage-string comment
    argues.
    """
    return RpcError(
        RpcErrorCode.TYPE_ERROR,
        "Wrong type passed:\n{\n"
        f'    "Position {position} ({name})": '
        f'"JSON value of type {json_type_name(value)} is '
        f'not of expected type {expected}"'
        "\n}",
    )


def error_msg(
    code: RpcErrorCode, message: str, request_id: object = None
) -> dict[str, Any]:
    """Build the error response of JSON-RPC 2.0's section 5, code and message.

    The specification requires the answer to carry the id of the request
    it answers, and reserves null for a request whose id could not be
    read out of it -- which is what its own example for an invalid
    request object shows. So a caller passes the id wherever
    `is_valid_rpc` has already found one, and leaves it out where the
    request -- or, for `PARSE_ERROR`, the body before it was even a
    request -- is what was wrong. Nothing here reads `request_id` beyond
    embedding it in the response unchanged, so `object` is as much as
    the signature needs -- the specification lets a request's `id` be
    any JSON scalar, and this node does not itself validate the field
    before echoing it back.
    """
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": request_id,
    }


def bool_param(params: list[Any], position: int, *, name: str, default: bool) -> bool:
    """Read a declared `RPCArg::Type::BOOL` parameter, Core's own way.

    Omitted or explicit `null` both stand for the argument's own
    declared `default`. Anything else is read, and refused with
    `RPC_TYPE_ERROR` where it is not an actual JSON bool -- the same
    check `RPCMethod::HandleRequest` makes for every declared argument
    before the handler body runs at all (`src/rpc/util.cpp:653-661`),
    applied here to the one JSON type this helper's every caller
    declares. `position` is the zero-based index into `params`, the way
    every caller here already addresses it; `type_error` wants Core's
    own one-based count, so it is passed `position + 1`.
    """
    if len(params) <= position or params[position] is None:
        return default
    value = params[position]
    if not isinstance(value, bool):
        raise type_error(position + 1, name, value, "bool")
    return value
