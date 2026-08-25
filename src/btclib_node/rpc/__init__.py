# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The JSON-RPC surface this node serves.

Connections, the RPC manager, the errors JSON-RPC 2.0 defines and the
method handlers `Node`'s loop calls. `manager.RpcManager` is the
thread; `connection.Connection` is one request's own socket;
`callbacks.callbacks` is the method-name table `main.handle_rpc`
dispatches through; `errors.RpcError` is what a handler raises to
answer with a JSON-RPC error object instead of a result.
"""
