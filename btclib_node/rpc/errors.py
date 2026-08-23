# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""How a callback refuses a request rather than failing on it."""

import enum


class RpcErrorCode(enum.IntEnum):
    """The codes of Bitcoin Core's RPCErrorCode, `src/rpc/protocol.h`.

    A client reads the code before it reads the message, so a refusal
    this node makes carries the number Core gives the same refusal.
    `INTERNAL_ERROR` is what Core's own header reserves for a genuine
    fault of the server, which is why nothing here answers a bad request
    with it.
    """

    MISC_ERROR = -1
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
