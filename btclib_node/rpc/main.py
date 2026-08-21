# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.rpc.callbacks import callbacks


def get_connection(manager, id):
    try:
        conn = manager.connections[id]
        return conn
    except Exception:
        return None


def is_valid_rpc(request):
    if not isinstance(request, dict):
        return False
    if "method" not in request:
        return False
    if "id" not in request:
        return False
    return True


def error_msg(code):
    error_messages = {
        -32600: "Invalid request",
        -32601: "Method not found",
        -32603: "Internal Error",
    }
    if code not in error_messages:
        code = -32603
    error = error_messages[code]
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": error},
        "id": None,
    }


def handle_rpc(node):
    data, conn_id = node.rpc_manager.messages.popleft()
    conn = get_connection(node.rpc_manager, conn_id)
    if not conn:
        return

    node.logger.debug(f"Received rpc message: {conn_id}")

    response = []
    for request in data:
        if not is_valid_rpc(request):
            response.append(error_msg(-32600))
        elif request["method"] not in callbacks:
            response.append(error_msg(-32601))
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
            except Exception:
                node.logger.exception("Exception occurred")
                response.append(error_msg(-32603))

    if request.get("method") == "stop":
        conn.send_and_wait(response)
        node.stop()
    else:
        conn.send(response)
    node.logger.debug("Finished rpc\n")
    # node.rpc_manager.connections.pop(conn_id)
