# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`handle_p2p`, `handle_p2p_handshake`, `resume_cfilters` and `resume_getdata`.

The first two pop one message off their own queue -- `P2pManager.messages`
or `P2pManager.handshake_messages` -- and dispatch it through
`p2p.callbacks.callbacks` or `p2p.callbacks.handshake_callbacks`
depending on the connection's own `P2pConnStatus`. An exception raised
by a callback ends that connection's message rather than the loop: it
goes to `P2pManager.maybe_discourage_and_disconnect` where it is a
`MisbehavingError`, is logged with the peer kept where it is any other
`BTClibException`, and stops the connection where it is a bug in the
handler.

Each also weighs its own queued item's wire size back off the
connection it came from, `queued_recv_bytes`, resuming that connection's
own reads (`Connection.run`) once enough of what it queued is off
either queue -- the other end of the pacing `Connection.parse_messages`
and `MAX_QUEUED_RECV_BYTES` (`p2p/connection.py`) start, argued there.
btclib-org/btclib-node#462, btclib-org/btclib-node#482

`resume_cfilters` and `resume_getdata` instead drain `node.pending_cfilters`
and `node.pending_getdata`, the connections `p2p.callbacks.get_cfilters`
and `p2p.callbacks.getdata` paused mid-answer rather than scheduling ahead
of what a peer has drained -- nothing queued triggers either, so both are
called once every pass of `run`'s own loop regardless.
"""

from typing import TYPE_CHECKING

from btclib.exceptions import BTClibException

from btclib_node.constants import P2pConnStatus
from btclib_node.exceptions import MisbehavingError
from btclib_node.p2p.callbacks import (
    advance_cfilters,
    advance_getdata,
    callbacks,
    handshake_callbacks,
)
from btclib_node.p2p.connection import MAX_QUEUED_RECV_BYTES

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.p2p.connection import Connection
    from btclib_node.p2p.manager import P2pManager

__all__ = ["handle_p2p", "handle_p2p_handshake", "resume_cfilters", "resume_getdata"]

# Core's `ProcessMessage` handles `sendheaders`, `sendcmpct`, `wtxidrelay`,
# `sendaddrv2` and `sendtxrcncl` after `version` and before `verack`
# (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
# `sendheaders` is the one of them `callbacks` holds: `wtxidrelay` and
# `sendaddrv2` are `handshake_callbacks`', and the other two this node
# does not handle.
_BEFORE_VERACK = frozenset({"sendheaders"})


def _drop(manager: P2pManager, conn: Connection, e: Exception) -> bool:
    """Punish `conn` over `e`, and answer whether its host was discouraged.

    A `MisbehavingError` goes to `maybe_discourage_and_disconnect`, any
    other `BTClibException` leaves the peer as it is, and anything else
    stops the connection, `handle_p2p`'s own `except` explaining why.
    """
    if isinstance(e, MisbehavingError):
        return manager.maybe_discourage_and_disconnect(conn)
    if not isinstance(e, BTClibException):
        conn.stop()
    return False


def handle_p2p_handshake(node: Node) -> None:
    """Pop one queued handshake message and dispatch it, or drop the peer.

    Once `verack` has promoted the connection, a second `version` or
    `verack` is ignored and a `wtxidrelay` or `sendaddrv2` drops the
    peer undiscouraged, as Core's `ProcessMessage` answers each
    (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag). A callback that raises is `_drop`'s.

    Weighs the item's own size back off the connection's
    `queued_recv_bytes` the moment it is popped, the same as `handle_p2p`
    below and for the same reason -- argued there.
    btclib-org/btclib-node#482
    """
    msg_type, msg, conn_id, size = node.p2p_manager.handshake_messages.popleft()
    manager = node.p2p_manager
    # a connection still finishing its handshake, which is where every
    # one of these four commands is answered, or one already promoted
    # that a peer sent a second version/verack/wtxidrelay/sendaddrv2 to
    conn = manager.pending_connections.get(conn_id) or manager.connections.get(conn_id)
    if conn is not None:
        # Connection's own backpressure pair, crossed from this thread on
        # purpose: connection.py argues both where it defines them
        with conn._recv_lock:  # noqa: SLF001
            conn.queued_recv_bytes -= size
            resume = conn.queued_recv_bytes <= MAX_QUEUED_RECV_BYTES
        if resume:
            conn.loop.call_soon_threadsafe(conn._recv_resume.set)  # noqa: SLF001
        node.logger.info("Received p2p message: %s, %s", msg_type, conn_id)
        try:
            if conn.status == P2pConnStatus.Open:
                handshake_callbacks[msg_type](node, msg, conn)
            elif conn.status == P2pConnStatus.Connected and msg_type in (
                "wtxidrelay",
                "sendaddrv2",
            ):
                conn.stop()
        except Exception as e:
            # `handle_p2p`'s own `except` below explains which
            # exceptions discourage the peer
            discourage = _drop(manager, conn, e)
            # `conn_id`, not `conn.address`: this line is what
            # distinguishes the two branches above on disk (#526), and
            # the verdict and the command are what that takes -- the
            # peer's own address is not, on a line every exception here
            # writes to `debug.log` whether or not this peer is at
            # fault. Core keys the same judgment the same way:
            # `PeerManagerImpl::Misbehaving` (`src/net_processing.cpp`,
            # at bitcoin/bitcoin@05e49b342f) logs `peer=%d` and nothing
            # else, and `CNode::LogPeer` appends the address only under
            # `fLogIPs`, off by default. An id here resolves to a
            # peer whether or not the handshake ever finished:
            # `P2pManager.create_connection` logs it beside the address
            # as soon as the connection exists, which is what this
            # block -- where a handshake that raised before `verack`
            # lands -- needs it to (btclib-org/btclib-node#611)
            node.logger.exception(
                "Handling %s from connection %s failed, %s",
                msg_type,
                conn_id,
                "peer discouraged" if discourage else "peer not discouraged",
            )


def handle_p2p(node: Node) -> None:
    """Pop one queued message and dispatch it, once its handshake is done.

    A message ahead of `verack` is ignored, as Core's `ProcessMessage`
    ignores it (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag), except a `sendheaders` after `version`, which Core
    records there. A callback that raises is `_drop`'s, the comment
    below arguing its split.

    Weighs the item's own size back off the connection's
    `queued_recv_bytes` the moment it is popped, whatever happens to it
    next -- dispatched, or ignored for want of a callback or of a
    completed handshake -- since what
    `MAX_QUEUED_RECV_BYTES` paces is how much of a connection's own
    traffic sits unprocessed, not how that traffic was resolved. A
    connection paused there is resumed, via `call_soon_threadsafe`
    rather than a direct `set()`, from `Node`'s own thread onto the
    connection's (`Connection.__init__`'s own comment on `_recv_resume`
    argues why the indirection is required). btclib-org/btclib-node#462
    """
    msg_type, msg, conn_id, size, received = node.p2p_manager.messages.popleft()
    manager = node.p2p_manager
    # a connection still pending is still found here, so that what it
    # sends before `verack` is weighed off its own `queued_recv_bytes`
    # below before being ignored
    conn = manager.connections.get(conn_id) or manager.pending_connections.get(conn_id)
    if conn is not None:
        # the same backpressure pair as handle_p2p_handshake above, for
        # the same reason
        with conn._recv_lock:  # noqa: SLF001
            conn.queued_recv_bytes -= size
            resume = conn.queued_recv_bytes <= MAX_QUEUED_RECV_BYTES
        if resume:
            conn.loop.call_soon_threadsafe(conn._recv_resume.set)  # noqa: SLF001
        node.logger.info("Received p2p message: %s, %s", msg_type, conn_id)
        conn.time_received = received
        try:
            if msg_type in callbacks:
                if conn.status == P2pConnStatus.Connected or (
                    msg_type in _BEFORE_VERACK
                    and conn.status == P2pConnStatus.Open
                    and conn.version_message is not None
                ):
                    callbacks[msg_type](node, msg, conn)
                node.logger.debug("Finished p2p\n")
        except Exception as e:
            # A `BTClibException` is btclib refusing this peer's own
            # wire content. Where it is a `MisbehavingError`, a header
            # or a block failing a consensus check or a message past
            # Core's own size bound, the peer is discouraged, as Core
            # calls `Misbehaving` there. Any other, a payload that does
            # not parse among them, is logged and the peer kept, as
            # Core's `ProcessMessages` only logs what `ProcessMessage`
            # throws (`src/net_processing.cpp`, at
            # bitcoin/bitcoin@9be056a8a7, the v31.1 tag;
            # btclib-org/btclib-node#1170). Anything else caught here is
            # this node's own code failing on content that was fine --
            # `get_cfilters`'s "no filter for a block on the active
            # chain" among them -- and stops the connection without
            # discouraging the peer that merely triggered it.
            # btclib-org/btclib-node#283
            discourage = _drop(manager, conn, e)
            # `conn_id`, not `conn.address`: same reasoning as
            # `handle_p2p_handshake` above (#526)
            node.logger.exception(
                "Handling %s from connection %s failed, %s",
                msg_type,
                conn_id,
                "peer discouraged" if discourage else "peer not discouraged",
            )


def resume_cfilters(node: Node) -> bool:
    """Advance every paused `getcfilters` answer by what now fits.

    Answers whether anything did -- a connection dropped from
    `node.pending_cfilters` counts, same as one whose `heights` shrank
    from this function's own vantage point (a `getcfilters` extending
    it runs inside `get_cfilters`, strictly before this is called
    again, so growth is never what a pass here sees), so this only
    answers `False` where every paused connection was tried and stayed
    exactly as paused as it already was.
    `node.pending_cfilters` maps a connection id to the connection
    itself and the heights `advance_cfilters` (`p2p.callbacks`) has not
    yet sent -- entered there only when that call paused rather than
    finished, and read and written only here and in `get_cfilters`
    itself, both on `Node`'s own thread, so nothing here needs a lock
    any more than `get_cfilters`'s own loop over a fresh request does.

    A connection already closed is dropped without trying it -- `stop`
    can be called from `P2pManager`'s own thread too, but the flag it
    sets, `P2pConnStatus.Closed`, is read here the same way
    `advance_cfilters` already reads it mid-answer. An exception out of
    `advance_cfilters` is handled the same way `handle_p2p`'s own is
    above, since it is the same call raising it, just on a later turn.
    """
    manager = node.p2p_manager
    done: list[int] = []
    progressed = False
    for conn_id, (conn, heights) in list(node.pending_cfilters.items()):
        if conn.status == P2pConnStatus.Closed:
            done.append(conn_id)
            progressed = True
            continue
        before = len(heights)
        try:
            if advance_cfilters(node, conn, heights):
                done.append(conn_id)
                progressed = True
        except Exception as e:
            done.append(conn_id)
            progressed = True
            discourage = _drop(manager, conn, e)
            # `conn_id`, not `conn.address`: same reasoning as
            # `handle_p2p_handshake` above (#526)
            node.logger.exception(
                "Resuming cfilters for connection %s failed, %s",
                conn_id,
                "peer discouraged" if discourage else "peer not discouraged",
            )
        if len(heights) != before:
            progressed = True
    for conn_id in done:
        del node.pending_cfilters[conn_id]
    return progressed


def resume_getdata(node: Node) -> bool:
    """Advance every paused `getdata` answer by what now fits.

    The same shape as `resume_cfilters` above, over `node.pending_getdata`
    and `advance_getdata` (`p2p.callbacks`) instead: answers whether
    anything did, a connection dropped counting the same as one whose
    `items` shrank; a connection already closed is dropped without
    trying it; and an exception out of `advance_getdata` is handled the
    same way `handle_p2p`'s own is above, being the same call raising it
    on a later turn.
    """
    manager = node.p2p_manager
    done: list[int] = []
    progressed = False
    for conn_id, (conn, items) in list(node.pending_getdata.items()):
        if conn.status == P2pConnStatus.Closed:
            done.append(conn_id)
            progressed = True
            continue
        before = len(items)
        try:
            if advance_getdata(node, conn, items):
                done.append(conn_id)
                progressed = True
        except Exception as e:
            done.append(conn_id)
            progressed = True
            discourage = _drop(manager, conn, e)
            # `conn_id`, not `conn.address`: same reasoning as
            # `handle_p2p_handshake` above (#526)
            node.logger.exception(
                "Resuming getdata for connection %s failed, %s",
                conn_id,
                "peer discouraged" if discourage else "peer not discouraged",
            )
        if len(items) != before:
            progressed = True
    for conn_id in done:
        del node.pending_getdata[conn_id]
    return progressed
