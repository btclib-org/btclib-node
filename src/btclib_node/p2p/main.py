# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`handle_p2p`, `handle_p2p_handshake` and the three `resume_` functions.

The first two pop one message off their own queue -- `P2pManager.messages`
or `P2pManager.handshake_messages` -- and dispatch it through
`p2p.callbacks.callbacks` or `p2p.callbacks.handshake_callbacks`
depending on the connection's own `P2pConnStatus`. An exception raised
by a callback ends that connection's message rather than the loop: it
goes to `P2pManager.maybe_discourage_and_disconnect` where it is a
`MisbehavingError`, and is logged with the peer kept otherwise: on one
line under the `net` debug category where it is a `BTClibValueError`,
btclib's parse error, as Core logs what `ProcessMessage` throws;
with its traceback where it is anything else.

Each also weighs its own queued item's weight back off the
connection it came from, `queued_recv_bytes`, resuming that connection's
own reads (`Connection.run`) once enough of what it queued is off
either queue -- the other end of the pacing `Connection.parse_messages`
and `Connection.recv_flood_size` (`p2p/connection.py`) start, argued
there.
btclib-org/btclib-node#462, btclib-org/btclib-node#482

`resume_cfilters` and `resume_getdata` instead drain `node.pending_cfilters`
and `node.pending_getdata`, the connections `p2p.callbacks.get_cfilters`
and `p2p.callbacks.getdata` paused mid-answer rather than scheduling ahead
of what a peer has drained -- nothing queued triggers either, so both are
called once every pass of `run`'s own loop regardless.

`resume_tx_checks` moves `node.tx_checks` (`p2p/tx_checks.py`) along,
also once every pass.
"""

from collections import deque
from typing import TYPE_CHECKING

from btclib.exceptions import BTClibValueError

from btclib_node.constants import P2pConnStatus
from btclib_node.exceptions import MisbehavingError
from btclib_node.p2p.callbacks import (
    advance_cfilters,
    advance_getdata,
    already_judged,
    callbacks,
    handshake_callbacks,
    process_orphan,
    settle_tx,
)
from btclib_node.p2p.tx_checks import TX_CHECK_DEADLINE

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.p2p.connection import Connection
    from btclib_node.p2p.manager import P2pManager

__all__ = [
    "handle_p2p",
    "handle_p2p_handshake",
    "resume_cfilters",
    "resume_getdata",
    "resume_tx_checks",
]

# Core's `ProcessMessage` handles `sendheaders`, `sendcmpct`, `wtxidrelay`,
# `sendaddrv2` and `sendtxrcncl` after `version` and before `verack`
# (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
# `sendheaders` and `sendcmpct` are the two of them `callbacks` holds:
# `wtxidrelay` and `sendaddrv2` are `handshake_callbacks`', and this node
# does not handle `sendtxrcncl`.
_BEFORE_VERACK = frozenset({"sendheaders", "sendcmpct"})


def _drop(manager: P2pManager, conn: Connection, e: Exception) -> bool:
    """Punish `conn` over `e`, and answer whether its host was discouraged.

    A `MisbehavingError` goes to `maybe_discourage_and_disconnect`, and
    anything else leaves the peer as it is, `handle_p2p`'s own `except`
    explaining why.
    """
    if isinstance(e, MisbehavingError):
        return manager.maybe_discourage_and_disconnect(conn)
    return False


def _verdict(*, discourage: bool) -> str:
    """Return the end of a failure's line: what became of the peer."""
    return "peer discouraged" if discourage else "peer not discouraged"


def _weigh_off(conn: Connection, size: int) -> None:
    """Take a handled message's `size` off `conn.queued_recv_bytes`.

    Resuming the connection's reads once it is back under the bound.
    `Connection`'s own backpressure pair, crossed from this thread on
    purpose: connection.py argues both where it defines them.
    """
    with conn._recv_lock:  # noqa: SLF001
        conn.queued_recv_bytes -= size
        resume = conn.queued_recv_bytes <= conn.recv_flood_size
    if resume:
        conn.loop.call_soon_threadsafe(conn._recv_resume.set)  # noqa: SLF001


def _hold_message(
    node: Node, conn: Connection, conn_id: int, held: tuple[str, bytes, int, float]
) -> bool:
    """Hold a message back while the peer has work before it.

    That is a check queued, a message held, an orphan to reconsider, a
    `getdata` or `getcfilters` answer paused on `node.pending_getdata`
    or `node.pending_cfilters`, or `conn.pause_send`, Core's
    `fPauseSend`.
    Answers whether it did. Core handles a peer's messages in the order
    received, decides a `tx` before it reads the next one, and reads
    nothing more from a peer while its `getdata` requests are unserved
    or its send buffer is full (`ProcessMessages`,
    `src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), so a `pong` tells the peer its `tx` was handled and its items
    were sent (btclib-org/btclib-node#1739, btclib-org/btclib-node#1775,
    btclib-org/btclib-node#1789, btclib-org/btclib-node#1796).

    A paused `getcfilters` answer holds the peer where Core's full send
    buffer would (`advance_cfilters`, `p2p/callbacks.py`), and keeps a
    second request from replacing the paused one.

    `held` is the command, the payload, its weight and the time it was
    read; it stays weighed against the peer's `queued_recv_bytes`, which
    pauses the connection's reads past `recv_flood_size`, Core's
    `-maxreceivebuffer`, until `resume_tx_checks` reads it.
    """
    if not (
        node.tx_checks.busy(conn_id)
        or node.download_manager.orphanage.have_tx_to_reconsider(conn_id)
        or conn_id in node.pending_getdata
        or conn_id in node.pending_cfilters
        or conn.pause_send
    ):
        return False
    node.tx_checks.waiting.setdefault(conn_id, deque()).append(held)
    return True


def _after_verack(conn: Connection, msg_type: str) -> None:
    """Answer a handshake command from a connection past its `verack`.

    A second `version` or `verack` is ignored, and a `wtxidrelay` or
    `sendaddrv2` drops the peer undiscouraged, as Core's `ProcessMessage`
    does (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag).
    """
    if msg_type in ("wtxidrelay", "sendaddrv2"):
        conn.stop()


def handle_p2p_handshake(node: Node) -> None:
    """Pop one queued handshake message and dispatch it, or drop the peer.

    A message of another command is here too, queued behind the
    handshake commands of a connection that was still `Open` when it was
    read: it is dispatched as `handle_p2p` would, and none overtakes the
    connection's `verack`.
    btclib-org/btclib-node#1657

    Once `verack` has promoted the connection, a second `version` or
    `verack` is ignored and a `wtxidrelay` or `sendaddrv2` drops the
    peer undiscouraged, as Core's `ProcessMessage` answers each
    (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag). A callback that raises is `_drop`'s.

    Weighs the item's own weight back off the connection's
    `queued_recv_bytes` the moment it is popped, or when
    `resume_tx_checks` reads it if `_hold_message` held it back, as
    `handle_p2p` below does and for the same reason.
    btclib-org/btclib-node#482
    """
    msg_type, msg, conn_id, size, received = (
        node.p2p_manager.handshake_messages.popleft()
    )
    manager = node.p2p_manager
    # a connection still finishing its handshake, which is where every
    # one of these four commands is answered, or one already promoted
    # that a peer sent a second version/verack/wtxidrelay/sendaddrv2 to
    conn = manager.pending_connections.get(conn_id) or manager.connections.get(conn_id)
    if conn is not None:
        if _hold_message(node, conn, conn_id, (msg_type, msg, size, received)):
            return
        _weigh_off(conn, size)
        node.logger.log_debug(
            "net", "received: %s (%d bytes) peer=%d", msg_type, len(msg), conn_id
        )
        if msg_type not in handshake_callbacks:
            conn.time_received = received
            _dispatch(node, conn, conn_id, msg_type, msg)
            return
        try:
            # A feeler past its `version` is being dropped, and Core's
            # `ProcessMessages` reads nothing more of a peer once
            # `fDisconnect` is set: a `verack` behind it would promote it.
            if conn.status == P2pConnStatus.Open and not (
                conn.feeler and conn.version_message is not None
            ):
                handshake_callbacks[msg_type](node, msg, conn)
            elif conn.status == P2pConnStatus.Connected:
                _after_verack(conn, msg_type)
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
            # logged as `_dispatch`'s `except` below logs it
            if isinstance(e, BTClibValueError):
                node.logger.log_debug(
                    "net",
                    "Handling %s from connection %s failed (%.200s), %s",
                    msg_type,
                    conn_id,
                    e,
                    _verdict(discourage=discourage),
                )
            else:
                node.logger.exception(
                    "Handling %s from connection %s failed, %s",
                    msg_type,
                    conn_id,
                    _verdict(discourage=discourage),
                )


def _dispatch(
    node: Node, conn: Connection, conn_id: int, msg_type: str, msg: bytes
) -> None:
    """Run the `callbacks` entry for `msg_type`, or ignore it ahead of `verack`.

    A callback that raises is `_drop`'s, the comment below arguing its
    split.
    """
    manager = node.p2p_manager
    try:
        if msg_type in callbacks:
            if conn.status == P2pConnStatus.Connected or (
                msg_type in _BEFORE_VERACK
                and conn.status == P2pConnStatus.Open
                and conn.version_message is not None
                and not conn.feeler
            ):
                callbacks[msg_type](node, msg, conn)
            node.logger.log_debug("net", "Finished p2p\n")
    except Exception as e:
        # A `MisbehavingError` -- a header or a block failing a
        # consensus check, a message past Core's own size bound -- is
        # where Core calls `Misbehaving`, so the peer is discouraged.
        # Anything else, a payload that does not parse or this node's
        # own code failing on content that was fine, is logged and the
        # peer kept: Core's `ProcessMessages` catches every exception
        # out of `ProcessMessage`, `catch (...)` included, logs it and
        # keeps the peer (`src/net_processing.cpp`,
        # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag;
        # btclib-org/btclib-node#1170, btclib-org/btclib-node#1233).
        discourage = _drop(manager, conn, e)
        # `conn_id`, not `conn.address`: same reasoning as
        # `handle_p2p_handshake` above (#526)
        # Core's `ProcessMessages` logs what `ProcessMessage` throws as one
        # `net` debug line, text only (`src/net_processing.cpp`, at
        # bitcoin/bitcoin@9be056a8a7, the v31.1 tag). A
        # `BTClibValueError` is btclib's refusal of a payload and gets
        # that line, cut at 200 characters (this node's own cut, not
        # Core's); anything else is this node's own code failing and
        # keeps its traceback.
        if isinstance(e, BTClibValueError):
            node.logger.log_debug(
                "net",
                "Handling %s from connection %s failed (%.200s), %s",
                msg_type,
                conn_id,
                e,
                _verdict(discourage=discourage),
            )
        else:
            node.logger.exception(
                "Handling %s from connection %s failed, %s",
                msg_type,
                conn_id,
                _verdict(discourage=discourage),
            )


def handle_p2p(node: Node) -> None:
    """Pop one queued message and dispatch it, once its handshake is done.

    A message ahead of `verack` is ignored, as Core's `ProcessMessage`
    ignores it (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag), except a `sendheaders` or a `sendcmpct` after
    `version`, which Core records there. A callback that raises is
    `_drop`'s, the comment below arguing its split.

    Weighs the item's own weight back off the connection's
    `queued_recv_bytes` the moment it is popped, whatever happens to it
    next -- dispatched, or ignored for want of a callback or of a
    completed handshake -- since what
    `recv_flood_size` paces is how much of a connection's own
    traffic sits unprocessed, not how that traffic was resolved. A
    connection paused there is resumed, via `call_soon_threadsafe`
    rather than a direct `set()`, from `Node`'s own thread onto the
    connection's (`Connection.__init__`'s own comment on `_recv_resume`
    argues why the indirection is required). btclib-org/btclib-node#462

    A message held back by `_hold_message` is the exception: it is
    weighed off when `resume_tx_checks` reads it, being unprocessed
    until then.
    """
    msg_type, msg, conn_id, size, received = node.p2p_manager.messages.popleft()
    manager = node.p2p_manager
    # a connection still pending is still found here, so that what it
    # sends before `verack` is weighed off its own `queued_recv_bytes`
    # below before being ignored
    conn = manager.connections.get(conn_id) or manager.pending_connections.get(conn_id)
    if conn is not None:
        if _hold_message(node, conn, conn_id, (msg_type, msg, size, received)):
            return
        _weigh_off(conn, size)
        node.logger.log_debug(
            "net", "received: %s (%d bytes) peer=%d", msg_type, len(msg), conn_id
        )
        conn.time_received = received
        _dispatch(node, conn, conn_id, msg_type, msg)


def resume_cfilters(node: Node) -> bool:
    """Advance every paused `getcfilters` answer by what now fits.

    Answers whether anything did -- a connection dropped from
    `node.pending_cfilters` counts, same as one whose block hashes
    shrank -- so this only answers `False` where every paused
    connection was tried and stayed exactly as paused as it already was.
    `node.pending_cfilters` maps a connection id to the connection
    itself and the block hashes `advance_cfilters` (`p2p.callbacks`) has
    not yet sent -- entered there only when that call paused rather than
    finished. It is written only here and in `get_cfilters`, and read
    there and by `_hold_message` and `_read_held`, all on `Node`'s own
    thread, so nothing here needs a lock.

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
    for conn_id, (conn, block_hashes) in list(node.pending_cfilters.items()):
        if conn.status == P2pConnStatus.Closed:
            done.append(conn_id)
            progressed = True
            continue
        before = len(block_hashes)
        try:
            if advance_cfilters(node, conn, block_hashes):
                done.append(conn_id)
                progressed = True
        except Exception as e:
            done.append(conn_id)
            progressed = True
            discourage = _drop(manager, conn, e)
            # `conn_id`, not `conn.address`: same reasoning as
            # `handle_p2p_handshake` above (#526)
            # logged as `_dispatch`'s `except` above logs it
            if isinstance(e, BTClibValueError):
                node.logger.log_debug(
                    "net",
                    "Resuming cfilters for connection %s failed (%.200s), %s",
                    conn_id,
                    e,
                    _verdict(discourage=discourage),
                )
            else:
                node.logger.exception(
                    "Resuming cfilters for connection %s failed, %s",
                    conn_id,
                    _verdict(discourage=discourage),
                )
        if len(block_hashes) != before:
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
            # logged as `_dispatch`'s `except` above logs it
            if isinstance(e, BTClibValueError):
                node.logger.log_debug(
                    "net",
                    "Resuming getdata for connection %s failed (%.200s), %s",
                    conn_id,
                    e,
                    _verdict(discourage=discourage),
                )
            else:
                node.logger.exception(
                    "Resuming getdata for connection %s failed, %s",
                    conn_id,
                    _verdict(discourage=discourage),
                )
        if len(items) != before:
            progressed = True
    for conn_id in done:
        del node.pending_getdata[conn_id]
    return progressed


def _take_up_orphans(node: Node) -> bool:
    """Take up an orphan for each peer that has one and no check queued.

    Answers whether any was. A peer gone is passed over: `DownloadManager`
    erases its orphans.
    """
    progressed = False
    for conn_id in node.download_manager.orphanage.peers_to_reconsider():
        conn = node.p2p_manager.connections.get(conn_id)
        if conn is None or conn_id in node.tx_checks.queued:
            continue
        progressed = True
        try:
            process_orphan(node, conn)
        except Exception as e:
            discourage = _drop(node.p2p_manager, conn, e)
            node.logger.exception(
                "Taking up an orphan from connection %s failed, %s",
                conn_id,
                _verdict(discourage=discourage),
            )
    return progressed


def _read_held(node: Node) -> bool:
    """Read the oldest held message of each peer free to have one read.

    A peer is free once nothing `_hold_message` names holds it but its
    held messages. A drain on `P2pManager`'s thread needs no signal:
    `Node`'s loop calls this every pass.
    Answers whether any was read, or dropped for being gone.
    """
    checks = node.tx_checks
    manager = node.p2p_manager
    progressed = False
    for conn_id, waiting in list(checks.waiting.items()):
        if conn_id in checks.queued:
            continue
        conn = manager.connections.get(conn_id)
        if conn is None or conn.status == P2pConnStatus.Closed:
            progressed = True
            del checks.waiting[conn_id]
            continue
        if (
            node.download_manager.orphanage.have_tx_to_reconsider(conn_id)
            or conn_id in node.pending_getdata
            or conn_id in node.pending_cfilters
            or conn.pause_send
        ):
            continue
        progressed = True
        msg_type, msg, size, received = waiting.popleft()
        if not waiting:
            del checks.waiting[conn_id]
        _weigh_off(conn, size)
        node.logger.log_debug(
            "net", "received: %s (%d bytes) peer=%d", msg_type, len(msg), conn_id
        )
        conn.time_received = received
        if msg_type in handshake_callbacks:
            # held only once past `verack`
            _after_verack(conn, msg_type)
        else:
            _dispatch(node, conn, conn_id, msg_type, msg)
    return progressed


def resume_tx_checks(node: Node) -> bool:
    """Settle the check in flight, read held messages, start the next.

    Answers whether anything moved. Each step runs on `Node`'s thread:

    - a check whose verdict is in goes to `p2p.callbacks.settle_tx`;
    - a check past `TX_CHECK_DEADLINE` is dropped and logged, its worker
      presumed gone;
    - each peer with nothing queued and an orphan to reconsider takes one
      up, ahead of its held messages, as Core's `ProcessMessages`
      takes up an orphan before the next message
      (`p2p.callbacks.process_orphan`);
    - each peer free to have one read, as `_read_held` says, has its
      oldest held message read, one per peer per pass, as Core's message
      handler reads one message per peer per pass;
    - with no check in flight, the oldest candidate queued is handed to
      `node.worker_pool`, or dropped if its peer is gone or the mempool
      has judged it meanwhile, a copy from another peer included.

    One check in flight at a time, so a block's
    `interpreter.check_scripts`, which shares the pool, does not wait
    behind a queue of them; one dropped at its deadline may still be
    running.

    An exception out of `settle_tx`, out of an orphan or out of a held message
    is handled as `handle_p2p`'s own is.
    """
    checks = node.tx_checks
    manager = node.p2p_manager
    progressed = False
    done = checks.finish()
    if done is not None:
        progressed = True
        check, refusal = done
        try:
            settle_tx(node, check, refusal)
        except Exception as e:
            discourage = _drop(manager, check.conn, e)
            node.logger.exception(
                "Settling tx from connection %s failed, %s",
                check.conn.id,
                _verdict(discourage=discourage),
            )
    overdue = checks.drop_overdue()
    if overdue is not None:
        progressed = True
        node.logger.warning(
            "Script check of tx %s from connection %s gave no verdict in %ss, dropped",
            overdue.tx.hash.hex(),
            overdue.conn.id,
            TX_CHECK_DEADLINE,
        )
    progressed |= _take_up_orphans(node)
    progressed |= _read_held(node)
    while not checks.checking and checks.queued:
        progressed = True
        check = next(iter(checks.queued.values()))
        if check.conn.status == P2pConnStatus.Closed or already_judged(
            node, check.tx, check.conn
        ):
            checks.unqueue(check.conn.id)
            continue
        checks.start(node, check)
    return progressed
