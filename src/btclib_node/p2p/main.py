# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`handle_p2p` and `handle_p2p_handshake`, one pass of `Node`'s own loop.

Each pops one message off its own queue -- `P2pManager.messages` or
`P2pManager.handshake_messages` -- and dispatches it through
`p2p.callbacks.callbacks` or `p2p.callbacks.handshake_callbacks`
depending on the connection's own `P2pConnStatus`. An exception raised
by a callback stops that connection rather than the loop, and is
discouraged for where it is a parse failure from the peer's own bytes
rather than a bug in the handler.
"""

from typing import TYPE_CHECKING

from btclib.exceptions import BTClibException

from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.callbacks import callbacks, handshake_callbacks

if TYPE_CHECKING:
    from btclib_node import Node


def handle_p2p_handshake(node: Node) -> None:
    """Pop one queued handshake message and dispatch it, or drop the peer.

    A message out of handshake order gets the connection discouraged
    and stopped rather than dispatched; a callback that raises stops it
    too, discouraged only where the exception is a `BTClibException`.
    """
    msg_type, msg, conn_id = node.p2p_manager.handshake_messages.popleft()
    manager = node.p2p_manager
    # a connection still finishing its handshake, which is where every
    # one of these four commands is answered, or one already promoted
    # that a peer sent a second version/verack/wtxidrelay/sendaddrv2 to
    conn = manager.pending_connections.get(conn_id) or manager.connections.get(conn_id)
    if conn is not None:
        node.logger.info("Received p2p message: %s, %s", msg_type, conn_id)
        try:
            if conn.status == P2pConnStatus.Open:
                handshake_callbacks[msg_type](node, msg, conn)
            elif conn.status == P2pConnStatus.Closed:
                pass
            else:
                # a second version/verack/wtxidrelay/sendaddrv2, out of
                # handshake order: discouraged for it (#283)
                manager.discourage(conn.address)
                conn.stop()
        except Exception as e:
            conn.stop()
            # discouraged for a parse failure, `handle_p2p`'s own
            # `except` below explaining which exceptions count as one
            if isinstance(e, BTClibException):
                manager.discourage(conn.address)
            node.logger.exception("Exception occurred")


def handle_p2p(node: Node) -> None:
    """Pop one queued message and dispatch it, once its handshake is done.

    A message ahead of `verack`, or one arriving out of order otherwise,
    gets the connection discouraged and stopped rather than dispatched;
    a callback that raises stops it too, discouraged only for a
    `BTClibException` (the comment below argues why that split matters).
    """
    msg_type, msg, conn_id = node.p2p_manager.messages.popleft()
    manager = node.p2p_manager
    # a connection still pending is still found here, so that anything
    # other than the four handshake commands it sends before `verack`
    # reaches the same `conn.stop()` a status of `Open` already gets
    # below, rather than being silently dropped along with the lookup
    conn = manager.connections.get(conn_id) or manager.pending_connections.get(conn_id)
    if conn is not None:
        node.logger.info("Received p2p message: %s, %s", msg_type, conn_id)
        try:
            if msg_type in callbacks:
                if conn.status == P2pConnStatus.Connected:
                    callbacks[msg_type](node, msg, conn)
                elif conn.status == P2pConnStatus.Closed:
                    pass
                else:
                    # a message ahead of `verack`, out of handshake
                    # order: discouraged for it (#283)
                    manager.discourage(conn.address)
                    conn.stop()
                node.logger.debug("Finished p2p\n")
        except Exception as e:
            conn.stop()
            # A `BTClibException` is btclib refusing this peer's own
            # wire content -- a malformed message, or one failing a
            # consensus check such as `add_headers`'s or `assert_valid`'s
            # own. Anything else caught here is this node's own code
            # failing on content that was fine -- `get_cfilters`'s "no
            # filter for a block on the active chain" among them -- and
            # not cause to discourage the peer that merely triggered it.
            # btclib-org/btclib-node#283
            if isinstance(e, BTClibException):
                manager.discourage(conn.address)
            node.logger.exception("Exception occurred")
