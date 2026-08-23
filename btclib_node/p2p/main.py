# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from typing import TYPE_CHECKING

from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.callbacks import callbacks, handshake_callbacks

if TYPE_CHECKING:
    from btclib_node import Node


def handle_p2p_handshake(node: Node) -> None:
    msg_type, msg, conn_id = node.p2p_manager.handshake_messages.popleft()
    manager = node.p2p_manager
    # a connection still finishing its handshake, which is where every
    # one of these four commands is answered, or one already promoted
    # that a peer sent a second version/verack/wtxidrelay/sendaddrv2 to
    conn = manager.pending_connections.get(conn_id) or manager.connections.get(conn_id)
    if conn is not None:
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


def handle_p2p(node: Node) -> None:
    msg_type, msg, conn_id = node.p2p_manager.messages.popleft()
    manager = node.p2p_manager
    # a connection still pending is still found here, so that anything
    # other than the four handshake commands it sends before `verack`
    # reaches the same `conn.stop()` a status of `Open` already gets
    # below, rather than being silently dropped along with the lookup
    conn = manager.connections.get(conn_id) or manager.pending_connections.get(conn_id)
    if conn is not None:
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
