# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""How a callback refuses a request rather than failing on it."""

from typing import Any

from bitcoin_core_rpc import RPCErrorCode

__all__ = ["RpcError", "bool_param", "type_error", "type_errors"]


class RpcError(Exception):
    """A request this node refuses, named by the answer it is owed.

    `rpc.jsonrpc.JsonRpcRequest.reply` turns it into the reply's error
    object, so raising it is how a callback says which of the two was
    wrong, the request or the node. Opposite in direction from
    `bitcoin_core_rpc.RpcError`, which names an error a node's answer
    already carries -- this one is raised here, not read off a reply.
    """

    def __init__(self, code: RPCErrorCode, message: str) -> None:
        """Name the refusal `code` and `message`, `handle_rpc` reads back."""
        super().__init__(f"{code.name}: {message}")
        self.code = code
        self.message = message


# univalue's own names for the six JSON types, `uvTypeName` in
# src/univalue/lib/univalue.cpp -- the vocabulary RPC_TYPE_ERROR's
# message speaks. Looked up along `type(value).__mro__`, nearest class
# first, rather than by `isinstance` in table order, so a bool reads as
# "bool" and not "number": bool is int's own subclass in Python, and the
# nearest class is bool itself.
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

    `value` is always a decoded JSON value here: `connection.py`
    decodes every request with `rpc.jsonrpc.decode`, whose objects are
    `JsonObject`, a `dict` subclass, and whose other values are the
    types `json.loads` produces. A subclass is named by its nearest
    class in the table.
    """
    return next(
        _JSON_TYPE_NAMES[cls] for cls in type(value).__mro__ if cls in _JSON_TYPE_NAMES
    )


def type_error(position: int, name: str, value: object, expected: str) -> RpcError:
    r"""Refuse a declared argument's own JSON type, Core's own wrapped shape.

    `RPCMethod::HandleRequest`'s own type check (`src/rpc/util.cpp`
    :652-661, read at `bitcoin/bitcoin@b91d983f66`) collects every
    mismatched argument into one `UniValue` object, keyed
    `strprintf("Position %s (%s)", i + 1, arg.m_names)`, and wraps it in
    `strprintf("Wrong type passed:\n%s", arg_mismatch.write(4))` --
    `UniValue::write`'s own four-space indent and lack of a trailing
    newline after the closing brace
    (`src/univalue/lib/univalue_write.cpp`), reproduced literally in
    `type_errors` rather than through a JSON encoder: every key and
    value is text this tree writes itself, with nothing to escape. Measured
    against a real `bitcoind` (v31.1.0, `-regtest`) answering a raw
    `testmempoolaccept`, `getblockheader`, `getblockhash`,
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
    return type_errors((position, name, value, expected))


def type_errors(*mismatches: tuple[int, str, object, str]) -> RpcError:
    """Refuse every mismatched argument at once, in one object.

    Each mismatch is `type_error`'s own four arguments, in the order
    Core checks the arguments, first to last: its object holds one key
    per argument that failed, as the one `type_error` builds holds one.
    """
    entries = ",\n".join(
        f'    "Position {position} ({name})": '
        f'"JSON value of type {json_type_name(value)} is '
        f'not of expected type {expected}"'
        for position, name, value, expected in mismatches
    )
    return RpcError(RPCErrorCode.TYPE_ERROR, f"Wrong type passed:\n{{\n{entries}\n}}")


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
