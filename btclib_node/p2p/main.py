# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.callbacks import callbacks, handshake_callbacks


def handle_p2p_handshake(node):
    msg_type, msg, conn_id = node.p2p_manager.handshake_messages.popleft()
    if conn_id in node.p2p_manager.connections:
        conn = node.p2p_manager.connections[conn_id]
        node.logger.info(f"Received p2p message: {msg_type}, {conn_id}")
        try:
            if conn.status == P2pConnStatus.Open:
                handshake_callbacks[msg_type](node, msg, conn)
            elif conn.status == P2pConnStatus.Closed:
                pass
            else:
                conn.stop()
        except Exception:
            conn.stop()
            node.logger.exception("Exception occurred")


def handle_p2p(node):
    msg_type, msg, conn_id = node.p2p_manager.messages.popleft()
    if conn_id in node.p2p_manager.connections:
        conn = node.p2p_manager.connections[conn_id]
        node.logger.info(f"Received p2p message: {msg_type}, {conn_id}")
        try:
            if msg_type in callbacks:
                if conn.status == P2pConnStatus.Connected:
                    callbacks[msg_type](node, msg, conn)
                elif conn.status == P2pConnStatus.Closed:
                    pass
                else:
                    conn.stop()
                node.logger.debug("Finished p2p\n")
        except Exception:
            conn.stop()
            node.logger.exception("Exception occurred")
