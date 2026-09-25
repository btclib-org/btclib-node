# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Which message reaches a callback, and what happens when one raises.

The dispatch is gated on the connection's status, and the functional
tests only ever drive a connection that reaches Connected: the states
either side of it, and the raise, are what is left.
"""

import threading
from collections import deque
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from btclib.exceptions import BTClibValueError
from btclib.p2p.addrv2 import NetworkAddressV2
from btclib.p2p.data import TxPayload as TxMsg

import btclib_node.p2p.callbacks as cb
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from btclib_node.p2p import main as main_module
from btclib_node.p2p.callbacks import callbacks, handshake_callbacks
from btclib_node.p2p.connection import MAX_QUEUED_RECV_BYTES
from btclib_node.p2p.main import (
    handle_p2p,
    handle_p2p_handshake,
    resume_cfilters,
    resume_getdata,
)
from tests import discourage_recorder, generate_random_transaction, log_recorder

if TYPE_CHECKING:
    from pathlib import Path

    from btclib_node import Node
    from btclib_node.p2p.connection import Connection


_AN_ADDRESS = NetworkAddressV2(0, 0, 1, b"\x01\x02\x03\x04", 18444)


def make_node(
    queue_name: str,
    item: tuple[str, bytes, int, int] | tuple[str, bytes, int, int, float],
    *,
    status: P2pConnStatus,
    present: bool = True,
    pending: bool = False,
    queued_recv_bytes: int = 0,
    logger: Any = None,
) -> tuple[Any, list[bool]]:
    """Build a node stand-in, one queued `item` on a `Connection` at `status`.

    `present` files the connection under `connections` or leaves it out entirely
    (a peer already gone by the time its message is handled); `pending` moves it
    into `pending_connections` instead, for the handshake's own in-between
    state. `queued_recv_bytes` seeds `handle_p2p`'s own byte accounting, which
    every stub connection carries whether or not a given test exercises it --
    `_recv_lock` a real lock, matching `Connection`'s own, and `loop` a stand-in
    whose `call_soon_threadsafe` runs its argument immediately rather than
    through a real event loop, `handle_p2p` never awaiting the result. `logger`
    defaults to a stand-in recording nothing, but a real `Logger` is what
    #526's own tests below hand in instead, `Node.logger` never reaching
    `caplog` for `log.py`'s own reason. Returns the node alongside the list
    `conn.stop` was told to record onto.
    """
    stopped: list[bool] = []
    discouraged, record = discourage_recorder()
    resumed: list[bool] = []
    conn = SimpleNamespace(
        status=status,
        address=_AN_ADDRESS,
        stop=lambda: stopped.append(True),
        queued_recv_bytes=queued_recv_bytes,
        _recv_lock=threading.Lock(),
        _recv_resume=SimpleNamespace(set=lambda: resumed.append(True)),
        loop=SimpleNamespace(call_soon_threadsafe=lambda fn: fn()),
        resumed=resumed,
    )
    manager = SimpleNamespace(
        messages=deque(),
        handshake_messages=deque(),
        connections={0: conn} if present and not pending else {},
        pending_connections={0: conn} if present and pending else {},
        maybe_discourage_and_disconnect=record,
        discouraged=discouraged,
    )
    getattr(manager, queue_name).append(item)
    node = SimpleNamespace(
        p2p_manager=manager,
        logger=logger
        if logger is not None
        else SimpleNamespace(
            info=lambda *a: None, debug=lambda *a: None, exception=lambda *a: None
        ),
    )
    return node, stopped


def test_a_handshake_message_reaches_its_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued handshake message on an `Open` connection reaches its callback.

    `Open` is before the handshake completes, which is exactly where a
    handshake message is expected and dispatched rather than dropped.
    """
    seen: list[bytes] = []

    def a_callback(node: Node, msg: bytes, conn: Connection) -> None:
        seen.append(msg)

    monkeypatch.setitem(handshake_callbacks, "verack", a_callback)
    node, stopped = make_node(
        "handshake_messages", ("verack", b"", 0, 1), status=P2pConnStatus.Open
    )
    handle_p2p_handshake(node)
    assert seen == [b""]
    assert not stopped


def test_a_handshake_message_on_a_closed_connection_is_dropped() -> None:
    """A handshake message for a `Closed` connection is dropped, not dispatched.

    Nothing to stop twice: `stopped` stays empty rather than recording
    a second `stop`.
    """
    node, stopped = make_node(
        "handshake_messages", ("verack", b"", 0, 1), status=P2pConnStatus.Closed
    )
    handle_p2p_handshake(node)
    assert not stopped


@pytest.mark.parametrize(
    ("command", "dropped"),
    [("version", False), ("verack", False), ("wtxidrelay", True), ("sendaddrv2", True)],
)
def test_a_handshake_message_on_a_connected_one_discourages_nobody(
    command: str,
    dropped: bool,  # noqa: FBT001
) -> None:
    """ISS 1133: a handshake command after `verack` is answered as Core does.

    Core's `ProcessMessage` ignores a second `version` or `verack`, and
    drops a peer sending `wtxidrelay` or `sendaddrv2` after `verack`
    without discouraging it.
    """
    node, stopped = make_node(
        "handshake_messages", (command, b"", 0, 1), status=P2pConnStatus.Connected
    )
    handle_p2p_handshake(node)
    assert stopped == ([True] if dropped else [])
    assert not node.p2p_manager.discouraged


def test_a_handshake_callback_that_raises_drops_the_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handshake callback's own bare exception drops the peer, undiscouraged.

    #283: not every exception is the peer's fault, and a bare `RuntimeError` is
    not one btclib raised over the peer's own content, so the connection is
    dropped but the peer is not discouraged for it.
    """

    def boom(node: Node, msg: bytes, conn: Connection) -> None:
        raise RuntimeError("no")

    monkeypatch.setitem(handshake_callbacks, "verack", boom)
    node, stopped = make_node(
        "handshake_messages", ("verack", b"", 0, 1), status=P2pConnStatus.Open
    )
    handle_p2p_handshake(node)
    assert stopped == [True]
    # #283: not every exception is the peer's fault, and a bare
    # RuntimeError is not one btclib raised over the peer's own content
    assert not node.p2p_manager.discouraged


def test_a_handshake_callback_that_raises_a_btclib_exception_costs_the_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handshake callback raising a btclib exception drops and discourages.

    Unlike the bare `RuntimeError` above, `BTClibValueError` is what
    btclib itself raises over content it refused, so #283 counts this
    one against the peer.
    """

    def boom(node: Node, msg: bytes, conn: Connection) -> None:
        raise BTClibValueError("no")

    monkeypatch.setitem(handshake_callbacks, "verack", boom)
    node, stopped = make_node(
        "handshake_messages", ("verack", b"", 0, 1), status=P2pConnStatus.Open
    )
    handle_p2p_handshake(node)
    assert stopped == [True]
    assert node.p2p_manager.discouraged == [_AN_ADDRESS]  # #283


def test_a_handshake_message_for_a_connection_that_is_gone_is_dropped() -> None:
    """A handshake message naming a connection id nobody holds is dropped.

    `present=False` stands in for a connection that was already closed
    and removed by the time its queued message is handled; nothing is
    stopped since nothing is found.
    """
    node, stopped = make_node(
        "handshake_messages",
        ("verack", b"", 7, 1),
        status=P2pConnStatus.Open,
        present=False,
    )
    handle_p2p_handshake(node)
    assert not stopped


def test_a_handshake_message_reaches_a_connection_still_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handshake message is dispatched for a connection still `pending`.

    Every handshake command is answered before `verack` promotes a
    connection out of `pending_connections`, so this is where every one
    of them, `verack` itself included, actually has to be looked up.
    """
    seen: list[bytes] = []

    def a_callback(node: Node, msg: bytes, conn: Connection) -> None:
        seen.append(msg)

    monkeypatch.setitem(handshake_callbacks, "verack", a_callback)
    node, stopped = make_node(
        "handshake_messages",
        ("verack", b"", 0, 1),
        status=P2pConnStatus.Open,
        pending=True,
    )
    handle_p2p_handshake(node)
    assert seen == [b""]
    assert not stopped


def test_handle_p2p_handshake_weighs_the_message_off_the_connections_own_queued_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The popped handshake item's own size is subtracted too.

    `handshake_messages` now shares `MAX_QUEUED_RECV_BYTES`
    (`p2p/connection.py`) with `messages`, so what a connection has
    queued there and not yet had `handle_p2p_handshake` look at counts
    the same way `handle_p2p`'s own weighing already does.
    btclib-org/btclib-node#482
    """
    monkeypatch.setitem(handshake_callbacks, "verack", lambda *_a: None)
    node, stopped = make_node(
        "handshake_messages",
        ("verack", b"", 0, 1_000),
        status=P2pConnStatus.Open,
        queued_recv_bytes=1_500,
    )
    handle_p2p_handshake(node)
    assert node.p2p_manager.connections[0].queued_recv_bytes == 500
    assert not stopped


def test_handle_p2p_handshake_resumes_a_connection_back_under_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping back to the bound schedules `_recv_resume.set` once.

    Mirrors `test_handle_p2p_resumes_a_connection_back_under_the_bound`
    above, over `handshake_messages` instead. btclib-org/btclib-node#482
    """
    monkeypatch.setitem(handshake_callbacks, "verack", lambda *_a: None)
    node, stopped = make_node(
        "handshake_messages",
        ("verack", b"", 0, 1),
        status=P2pConnStatus.Open,
        queued_recv_bytes=MAX_QUEUED_RECV_BYTES + 1,
    )
    handle_p2p_handshake(node)
    conn = node.p2p_manager.connections[0]
    assert conn.queued_recv_bytes == MAX_QUEUED_RECV_BYTES
    assert conn.resumed == [True]
    assert not stopped


def test_handle_p2p_handshake_does_not_resume_a_connection_still_over_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Still over `MAX_QUEUED_RECV_BYTES` after the pop: no resume scheduled.

    Mirrors `test_handle_p2p_does_not_resume_a_connection_still_over_the_bound`
    above, over `handshake_messages` instead. btclib-org/btclib-node#482
    """
    monkeypatch.setitem(handshake_callbacks, "verack", lambda *_a: None)
    node, stopped = make_node(
        "handshake_messages",
        ("verack", b"", 0, 1),
        status=P2pConnStatus.Open,
        queued_recv_bytes=MAX_QUEUED_RECV_BYTES + 2,
    )
    handle_p2p_handshake(node)
    conn = node.p2p_manager.connections[0]
    assert conn.queued_recv_bytes == MAX_QUEUED_RECV_BYTES + 1
    assert not conn.resumed
    assert not stopped


def test_a_message_reaches_its_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ordinary message on a `Connected` connection reaches its callback."""
    seen: list[bytes] = []

    def a_callback(node: Node, msg: bytes, conn: Connection) -> None:
        seen.append(msg)

    monkeypatch.setitem(callbacks, "ping", a_callback)
    node, stopped = make_node(
        "messages", ("ping", b"x", 0, 1, 0.0), status=P2pConnStatus.Connected
    )
    handle_p2p(node)
    assert seen == [b"x"]
    assert not stopped


def test_a_callback_reads_when_its_message_was_read_off_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The item's receive time is `conn.time_received` when the callback runs.

    Core's `ProcessMessage` takes it as `time_received`, and `pong` ends a
    round trip there rather than when `Node`'s loop reaches the message.
    """
    seen: list[float] = []

    def a_callback(node: Node, msg: bytes, conn: Any) -> None:
        seen.append(conn.time_received)

    monkeypatch.setitem(callbacks, "ping", a_callback)
    node, _stopped = make_node(
        "messages", ("ping", b"", 0, 1, 123.25), status=P2pConnStatus.Connected
    )
    handle_p2p(node)
    assert seen == [123.25]


@pytest.mark.parametrize("pending", [False, True])
def test_a_message_before_the_handshake_is_over_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    pending: bool,  # noqa: FBT001
) -> None:
    """ISS 1133: an ordinary message ahead of `verack` is ignored, as in Core.

    Not dispatched, the peer neither dropped nor discouraged, and its
    size weighed off the connection's `queued_recv_bytes` whether the
    connection is found in `connections` or still in
    `pending_connections`.
    """
    seen: list[bytes] = []
    monkeypatch.setitem(callbacks, "ping", lambda node, msg, conn: seen.append(msg))
    node, stopped = make_node(
        "messages",
        ("ping", b"", 0, 1_000, 0.0),
        status=P2pConnStatus.Open,
        pending=pending,
        queued_recv_bytes=1_500,
    )
    handle_p2p(node)
    assert not seen
    assert not stopped
    assert not node.p2p_manager.discouraged
    conn = (node.p2p_manager.pending_connections or node.p2p_manager.connections)[0]
    assert conn.queued_recv_bytes == 500


@pytest.mark.parametrize("versioned", [True, False])
def test_a_sendheaders_ahead_of_verack_is_recorded_once_version_is_in(
    versioned: bool,  # noqa: FBT001
) -> None:
    """A `sendheaders` between `version` and `verack` is recorded, as in Core.

    Core's `ProcessMessage` sets `m_prefers_headers` for a `sendheaders`
    once `version` is in, before the handshake completes, and ignores one
    ahead of `version`. Reachable here: `verack` is queued on
    `handshake_messages` and `sendheaders` on `messages`, and one read can
    frame both between `Node._drain_message_queues`' two drains, so the
    `sendheaders` is handled while the connection is still `Open`.
    """
    node, stopped = make_node(
        "messages", ("sendheaders", b"", 0, 1, 0.0), status=P2pConnStatus.Open
    )
    conn = node.p2p_manager.connections[0]
    conn.version_message = object() if versioned else None
    conn.prefers_headers = False
    handle_p2p(node)
    assert conn.prefers_headers is versioned
    assert not stopped
    assert not node.p2p_manager.discouraged


def test_a_message_on_a_closed_connection_is_dropped() -> None:
    """An ordinary message for a `Closed` connection is dropped, not run."""
    node, stopped = make_node(
        "messages", ("ping", b"", 0, 1, 0.0), status=P2pConnStatus.Closed
    )
    handle_p2p(node)
    assert not stopped


def test_a_command_nothing_dispatches_is_ignored() -> None:
    """A command with no entry in `callbacks` is ignored, not dropped."""
    node, stopped = make_node(
        "messages", ("nosuchcommand", b"", 0, 1, 0.0), status=P2pConnStatus.Connected
    )
    handle_p2p(node)
    assert not stopped


def test_a_callback_that_raises_drops_the_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A callback raising a bare exception drops the peer, undiscouraged.

    #283: an internal failure on content that was fine is this node's
    own bug, not cause to discourage the peer that triggered it.
    """

    def boom(node: Node, msg: bytes, conn: Connection) -> None:
        raise RuntimeError("no")

    monkeypatch.setitem(callbacks, "ping", boom)
    node, stopped = make_node(
        "messages", ("ping", b"", 0, 1, 0.0), status=P2pConnStatus.Connected
    )
    handle_p2p(node)
    assert stopped == [True]
    # #283: an internal failure on content that was fine is this node's
    # own bug, not cause to discourage the peer that triggered it
    assert not node.p2p_manager.discouraged


def test_a_callback_that_raises_a_btclib_exception_costs_the_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A btclib exception out of a callback drops the peer, discouraged."""

    def boom(node: Node, msg: bytes, conn: Connection) -> None:
        raise BTClibValueError("no")

    monkeypatch.setitem(callbacks, "ping", boom)
    node, stopped = make_node(
        "messages", ("ping", b"", 0, 1, 0.0), status=P2pConnStatus.Connected
    )
    handle_p2p(node)
    assert stopped == [True]
    assert node.p2p_manager.discouraged == [_AN_ADDRESS]  # #283


def test_a_peer_the_manager_spares_is_logged_as_not_discouraged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1090: the verdict logged is the one the manager answered.

    A btclib exception from a manual or a local peer drops it without
    discouraging it, and the line says so.
    """

    def boom(node: Node, msg: bytes, conn: Connection) -> None:
        raise BTClibValueError("no")

    def spare(conn: Any) -> bool:
        conn.stop()
        return False

    monkeypatch.setitem(callbacks, "ping", boom)
    logged, record = log_recorder()
    node, stopped = make_node(
        "messages",
        ("ping", b"", 0, 1, 0.0),
        status=P2pConnStatus.Connected,
        logger=SimpleNamespace(
            info=lambda *a: None, debug=lambda *a: None, exception=record
        ),
    )
    node.p2p_manager.maybe_discourage_and_disconnect = spare
    handle_p2p(node)
    assert stopped == [True]
    (line,) = logged
    assert line.endswith("peer not discouraged")


def test_a_consensus_invalid_transaction_costs_the_peer_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mempool candidate failing a consensus check drops the tx, not the peer.

    Unlike the two tests above, whose stand-in callback raises straight
    into `handle_p2p`'s own generic catch: `tx` (`p2p/callbacks.py`) is
    the real callback here, dispatched exactly as `callbacks["tx"]`
    reaches it, so its own `except BTClibValueError` is what has to
    keep this from ever reaching that catch at all. Core does not
    discourage a peer for a transaction failure of any kind,
    consensus-level ones included -- `test_a_transaction_only_relay_
    policy_refuses_costs_the_peer_nothing` (`p2p/callbacks_test.py`) is
    the same claim for the standardness-only half, by calling `tx`
    directly rather than through this dispatch. btclib-org/btclib-node#843
    """

    def consensus_invalid(node: Node, transaction: Any) -> None:
        err_msg = "bad-txns-nonfinal"
        raise BTClibValueError(err_msg)

    monkeypatch.setattr(cb, "verify_mempool_acceptance", consensus_invalid)
    transaction = generate_random_transaction()
    payload = TxMsg(transaction, include_witness=True).serialize()
    node, stopped = make_node(
        "messages",
        ("tx", payload, 0, len(payload), 0.0),
        status=P2pConnStatus.Connected,
    )
    node.status = NodeStatus.BlockSynced
    node.mempool = Mempool(Logger(debug=True))
    handle_p2p(node)
    assert not stopped
    assert not node.p2p_manager.discouraged


def test_a_message_for_a_connection_that_is_gone_is_dropped() -> None:
    """A message naming an unknown connection id is dropped, not run."""
    node, stopped = make_node(
        "messages",
        ("ping", b"", 7, 1, 0.0),
        status=P2pConnStatus.Connected,
        present=False,
    )
    handle_p2p(node)
    assert not stopped


def test_handle_p2p_weighs_the_message_off_the_connections_own_queued_bytes() -> None:
    """The popped item's own size is subtracted from `queued_recv_bytes`.

    Whatever happens to the message next -- here, an ordinary dispatch --
    `MAX_QUEUED_RECV_BYTES` (`p2p/connection.py`) paces what a connection
    has queued and not yet had `handle_p2p` look at, not what became of
    it once looked at.
    """
    node, stopped = make_node(
        "messages",
        ("nosuchcommand", b"", 0, 1_000, 0.0),
        status=P2pConnStatus.Connected,
        queued_recv_bytes=1_500,
    )
    handle_p2p(node)
    assert node.p2p_manager.connections[0].queued_recv_bytes == 500
    assert not stopped


def test_handle_p2p_resumes_a_connection_back_under_the_bound() -> None:
    """Dropping back to the bound schedules `_recv_resume.set` once.

    Seeded one octet over `MAX_QUEUED_RECV_BYTES` -- `Connection.__init__`'s
    `queued_recv_bytes` docstring is the constant this mirrors -- so that
    subtracting the popped item's own size lands it exactly on the bound,
    which `handle_p2p`'s own `<=` treats as resumed.
    """
    node, stopped = make_node(
        "messages",
        ("nosuchcommand", b"", 0, 1, 0.0),
        status=P2pConnStatus.Connected,
        queued_recv_bytes=MAX_QUEUED_RECV_BYTES + 1,
    )
    handle_p2p(node)
    conn = node.p2p_manager.connections[0]
    assert conn.queued_recv_bytes == MAX_QUEUED_RECV_BYTES
    assert conn.resumed == [True]
    assert not stopped


def test_handle_p2p_does_not_resume_a_connection_still_over_the_bound() -> None:
    """Still over `MAX_QUEUED_RECV_BYTES` after the pop: no resume scheduled.

    The mirror of the test above -- one octet short of the bound rather
    than landing on it -- so a connection that queued far more than one
    message's worth stays paused until enough of the rest drains too.
    """
    node, stopped = make_node(
        "messages",
        ("nosuchcommand", b"", 0, 1, 0.0),
        status=P2pConnStatus.Connected,
        queued_recv_bytes=MAX_QUEUED_RECV_BYTES + 2,
    )
    handle_p2p(node)
    conn = node.p2p_manager.connections[0]
    assert conn.queued_recv_bytes == MAX_QUEUED_RECV_BYTES + 1
    assert not conn.resumed
    assert not stopped


def a_pending_node(
    conn: Any, heights: deque[int], *, logger: Any = None
) -> tuple[Any, list[Any], list[Any]]:
    """Build a node stand-in with one connection on `pending_cfilters`.

    Returns the node alongside the lists its discouraging stand-in and
    `logger.exception` calls are recorded into -- `logged` stays empty
    where `logger` is a real `Logger`, since nothing appends to it then.
    """
    discouraged, record = discourage_recorder()
    logged: list[Any] = []
    node = SimpleNamespace(
        pending_cfilters={conn.id: (conn, heights)},
        p2p_manager=SimpleNamespace(maybe_discourage_and_disconnect=record),
        logger=logger
        if logger is not None
        else SimpleNamespace(exception=lambda *a: logged.append(a)),
    )
    return node, discouraged, logged


def test_resume_cfilters_drops_a_finished_connection_from_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished connection is dropped from `pending_cfilters`.

    `progressed` answers `True`: `heights` shrank to nothing.
    """
    stopped: list[Any] = []
    conn = SimpleNamespace(
        id=1, status=P2pConnStatus.Open, stop=lambda: stopped.append(True)
    )
    heights = deque([1, 2, 3])
    node, discouraged, logged = a_pending_node(conn, heights)
    monkeypatch.setattr(main_module, "advance_cfilters", lambda *_a: True)
    assert resume_cfilters(node) is True
    assert node.pending_cfilters == {}
    assert not stopped
    assert not discouraged
    assert not logged


def test_resume_cfilters_leaves_a_still_paused_connection_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection `advance_cfilters` has not finished stays registered.

    `progressed` still answers `True`: `heights` shrank, even though the
    answer as a whole is not done.
    """
    conn = SimpleNamespace(id=2, status=P2pConnStatus.Open, stop=lambda: None)
    heights = deque([1, 2, 3])

    def paused(_node: Any, _conn: Any, hs: deque[int]) -> bool:
        hs.popleft()
        return False

    node, _discouraged, _logged = a_pending_node(conn, heights)
    monkeypatch.setattr(main_module, "advance_cfilters", paused)
    assert resume_cfilters(node) is True
    assert node.pending_cfilters[2] == (conn, deque([2, 3]))


def test_resume_cfilters_answers_false_when_nothing_advanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection still too congested to send anything reports no progress."""
    conn = SimpleNamespace(id=3, status=P2pConnStatus.Open, stop=lambda: None)
    heights = deque([1, 2, 3])
    node, _discouraged, _logged = a_pending_node(conn, heights)
    monkeypatch.setattr(main_module, "advance_cfilters", lambda *_a: False)
    assert resume_cfilters(node) is False
    assert node.pending_cfilters[3] == (conn, heights)


def test_resume_cfilters_drops_an_already_closed_connection_without_advancing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection closed since it paused is dropped, unasked.

    `stop` can be called from `P2pManager`'s own thread between one turn
    of `Node`'s own loop and the next, so this is read the same way
    `advance_cfilters` itself already reads it mid-answer.
    """
    conn = SimpleNamespace(id=4, status=P2pConnStatus.Closed, stop=lambda: None)
    heights = deque([1, 2, 3])
    node, discouraged, logged = a_pending_node(conn, heights)
    called: list[Any] = []

    def unreached(*a: Any) -> bool:
        called.append(a)  # pragma: no cover -- advance_cfilters is not called
        return True  # pragma: no cover -- advance_cfilters answers nothing here

    monkeypatch.setattr(main_module, "advance_cfilters", unreached)
    # dropping it is progress in its own right, whether or not it ever
    # sent anything
    assert resume_cfilters(node) is True
    assert node.pending_cfilters == {}
    assert not called
    assert not discouraged
    assert not logged


def test_resume_cfilters_stops_the_peer_on_a_bare_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare exception out of `advance_cfilters` drops the peer, undiscouraged.

    Mirrors `handle_p2p`'s own split (#283): a bug in this node's own
    code, not the peer's doing.
    """
    stopped: list[Any] = []
    conn = SimpleNamespace(
        id=5, status=P2pConnStatus.Open, stop=lambda: stopped.append(True)
    )
    heights = deque([1, 2, 3])
    node, discouraged, logged = a_pending_node(conn, heights)

    def boom(*_a: Any) -> bool:
        raise RuntimeError("no")

    monkeypatch.setattr(main_module, "advance_cfilters", boom)
    assert resume_cfilters(node) is True
    assert stopped == [True]
    assert node.pending_cfilters == {}
    assert not discouraged
    assert logged


def test_resume_cfilters_discourages_the_peer_on_a_btclib_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A btclib exception out of `advance_cfilters` discourages the peer too."""
    stopped: list[Any] = []
    conn = SimpleNamespace(
        id=6,
        status=P2pConnStatus.Open,
        address=_AN_ADDRESS,
        stop=lambda: stopped.append(True),
    )
    heights = deque([1, 2, 3])
    node, discouraged, logged = a_pending_node(conn, heights)

    def boom(*_a: Any) -> bool:
        raise BTClibValueError("no")

    monkeypatch.setattr(main_module, "advance_cfilters", boom)
    assert resume_cfilters(node) is True
    assert stopped == [True]
    assert discouraged == [_AN_ADDRESS]
    assert logged


def a_pending_getdata_node(
    conn: Any, items: deque[Any], *, logger: Any = None
) -> tuple[Any, list[Any], list[Any]]:
    """Build a node stand-in with one connection on `pending_getdata`.

    The same shape as `a_pending_node` above, over `pending_getdata`
    instead: returns the node alongside the lists its
    discouraging stand-in and `logger.exception` calls are recorded
    into.
    """
    discouraged, record = discourage_recorder()
    logged: list[Any] = []
    node = SimpleNamespace(
        pending_getdata={conn.id: (conn, items)},
        p2p_manager=SimpleNamespace(maybe_discourage_and_disconnect=record),
        logger=logger
        if logger is not None
        else SimpleNamespace(exception=lambda *a: logged.append(a)),
    )
    return node, discouraged, logged


def test_resume_getdata_drops_a_finished_connection_from_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished connection is dropped from `pending_getdata`.

    `progressed` answers `True`: `items` shrank to nothing.
    """
    stopped: list[Any] = []
    conn = SimpleNamespace(
        id=1, status=P2pConnStatus.Open, stop=lambda: stopped.append(True)
    )
    items = deque([1, 2, 3])
    node, discouraged, logged = a_pending_getdata_node(conn, items)
    monkeypatch.setattr(main_module, "advance_getdata", lambda *_a: True)
    assert resume_getdata(node) is True
    assert node.pending_getdata == {}
    assert not stopped
    assert not discouraged
    assert not logged


def test_resume_getdata_leaves_a_still_paused_connection_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection `advance_getdata` has not finished stays registered.

    `progressed` still answers `True`: `items` shrank, even though the
    answer as a whole is not done.
    """
    conn = SimpleNamespace(id=2, status=P2pConnStatus.Open, stop=lambda: None)
    items = deque([1, 2, 3])

    def paused(_node: Any, _conn: Any, it: deque[Any]) -> bool:
        it.popleft()
        return False

    node, _discouraged, _logged = a_pending_getdata_node(conn, items)
    monkeypatch.setattr(main_module, "advance_getdata", paused)
    assert resume_getdata(node) is True
    assert node.pending_getdata[2] == (conn, deque([2, 3]))


def test_resume_getdata_answers_false_when_nothing_advanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection still too congested to send anything reports no progress."""
    conn = SimpleNamespace(id=3, status=P2pConnStatus.Open, stop=lambda: None)
    items = deque([1, 2, 3])
    node, _discouraged, _logged = a_pending_getdata_node(conn, items)
    monkeypatch.setattr(main_module, "advance_getdata", lambda *_a: False)
    assert resume_getdata(node) is False
    assert node.pending_getdata[3] == (conn, items)


def test_resume_getdata_drops_an_already_closed_connection_without_advancing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection closed since it paused is dropped, unasked.

    `stop` can be called from `P2pManager`'s own thread between one turn
    of `Node`'s own loop and the next, so this is read the same way
    `advance_getdata` itself already reads it mid-answer.
    """
    conn = SimpleNamespace(id=4, status=P2pConnStatus.Closed, stop=lambda: None)
    items = deque([1, 2, 3])
    node, discouraged, logged = a_pending_getdata_node(conn, items)
    called: list[Any] = []

    def unreached(*a: Any) -> bool:
        called.append(a)  # pragma: no cover -- advance_getdata is not called
        return True  # pragma: no cover -- advance_getdata answers nothing here

    monkeypatch.setattr(main_module, "advance_getdata", unreached)
    # dropping it is progress in its own right, whether or not it ever
    # sent anything
    assert resume_getdata(node) is True
    assert node.pending_getdata == {}
    assert not called
    assert not discouraged
    assert not logged


def test_resume_getdata_stops_the_peer_on_a_bare_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare exception out of `advance_getdata` drops the peer, undiscouraged.

    Mirrors `handle_p2p`'s own split (#283): a bug in this node's own
    code, not the peer's doing.
    """
    stopped: list[Any] = []
    conn = SimpleNamespace(
        id=5, status=P2pConnStatus.Open, stop=lambda: stopped.append(True)
    )
    items = deque([1, 2, 3])
    node, discouraged, logged = a_pending_getdata_node(conn, items)

    def boom(*_a: Any) -> bool:
        raise RuntimeError("no")

    monkeypatch.setattr(main_module, "advance_getdata", boom)
    assert resume_getdata(node) is True
    assert stopped == [True]
    assert node.pending_getdata == {}
    assert not discouraged
    assert logged


def test_resume_getdata_discourages_the_peer_on_a_btclib_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A btclib exception out of `advance_getdata` discourages the peer too."""
    stopped: list[Any] = []
    conn = SimpleNamespace(
        id=6,
        status=P2pConnStatus.Open,
        address=_AN_ADDRESS,
        stop=lambda: stopped.append(True),
    )
    items = deque([1, 2, 3])
    node, discouraged, logged = a_pending_getdata_node(conn, items)

    def boom(*_a: Any) -> bool:
        raise BTClibValueError("no")

    monkeypatch.setattr(main_module, "advance_getdata", boom)
    assert resume_getdata(node) is True
    assert stopped == [True]
    assert discouraged == [_AN_ADDRESS]
    assert logged


# #526: the four tests below are what actually reads the line each of the
# four handlers logs, rather than a stand-in's record of having been
# called. `Node.logger` is a `Logger(logging.Logger)` built directly
# (`log.py`), never through `logging.getLogger`, so it has no `parent`
# and no record from it ever reaches the root logger `caplog` sits on --
# asserting against `caplog.records` here would pass empty, on every one
# of these four handlers, whether or not the line below was ever
# written. A real `Logger` writing to a real file, read back after, is
# what actually observes it, the same way `log_test.py` already proves
# `Logger` itself against a file rather than against `caplog`.
#
# Proven against a mutation, run and reverted by hand rather than
# argued from the diff: reducing `handle_p2p_handshake`'s own site back
# to the shared `node.logger.exception("Exception occurred")` this issue
# opened against makes `test_handle_p2p_handshake_log_line_...` below
# fail, both lines in the file it reads back becoming the same one.


def test_handle_p2p_handshake_log_line_distinguishes_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discouraged run and one that is not read back as two different lines.

    Done when (#526): the two runs below are told apart from the log
    alone, not from a second channel a real `debug.log` does not carry.
    """
    log_path = tmp_path / "debug.log"
    logger = Logger(log_path, debug=True)
    for exc in (RuntimeError("no"), BTClibValueError("no")):

        def raiser(*_a: Any, exc: Exception = exc) -> None:
            raise exc

        monkeypatch.setitem(handshake_callbacks, "verack", raiser)
        node, _stopped = make_node(
            "handshake_messages",
            ("verack", b"", 0, 1),
            status=P2pConnStatus.Open,
            logger=logger,
        )
        handle_p2p_handshake(node)
    logger.close()
    lines = [
        line
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if "Handling verack from connection 0 failed" in line
    ]
    assert len(lines) == 2
    not_discouraged, discouraged = lines
    assert "peer not discouraged" in not_discouraged
    assert "peer not discouraged" not in discouraged
    assert "peer discouraged" in discouraged
    assert not_discouraged != discouraged


def test_handle_p2p_log_line_distinguishes_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same proof as `handle_p2p_handshake`'s, for `handle_p2p`."""
    log_path = tmp_path / "debug.log"
    logger = Logger(log_path, debug=True)
    for exc in (RuntimeError("no"), BTClibValueError("no")):

        def raiser(*_a: Any, exc: Exception = exc) -> None:
            raise exc

        monkeypatch.setitem(callbacks, "ping", raiser)
        node, _stopped = make_node(
            "messages",
            ("ping", b"", 0, 1, 0.0),
            status=P2pConnStatus.Connected,
            logger=logger,
        )
        handle_p2p(node)
    logger.close()
    lines = [
        line
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if "Handling ping from connection 0 failed" in line
    ]
    assert len(lines) == 2
    not_discouraged, discouraged = lines
    assert "peer not discouraged" in not_discouraged
    assert "peer not discouraged" not in discouraged
    assert "peer discouraged" in discouraged
    assert not_discouraged != discouraged


def test_resume_cfilters_log_line_distinguishes_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same proof as `handle_p2p_handshake`'s, for `resume_cfilters`."""
    log_path = tmp_path / "debug.log"
    logger = Logger(log_path, debug=True)
    for conn_id, exc in ((1, RuntimeError("no")), (2, BTClibValueError("no"))):
        conn = SimpleNamespace(
            id=conn_id,
            status=P2pConnStatus.Open,
            address=_AN_ADDRESS,
            stop=lambda: None,
        )
        node, _discouraged, _logged = a_pending_node(conn, deque([1]), logger=logger)

        def boom(*_a: Any, exc: Exception = exc) -> bool:
            raise exc

        monkeypatch.setattr(main_module, "advance_cfilters", boom)
        resume_cfilters(node)
    logger.close()
    text = log_path.read_text(encoding="utf-8")
    (not_discouraged,) = [
        line
        for line in text.splitlines()
        if "Resuming cfilters for connection 1 failed" in line
    ]
    (discouraged,) = [
        line
        for line in text.splitlines()
        if "Resuming cfilters for connection 2 failed" in line
    ]
    assert "peer not discouraged" in not_discouraged
    assert "peer discouraged" in discouraged
    assert "peer not discouraged" not in discouraged


def test_resume_getdata_log_line_distinguishes_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same proof as `handle_p2p_handshake`'s, for `resume_getdata`."""
    log_path = tmp_path / "debug.log"
    logger = Logger(log_path, debug=True)
    for conn_id, exc in ((1, RuntimeError("no")), (2, BTClibValueError("no"))):
        conn = SimpleNamespace(
            id=conn_id,
            status=P2pConnStatus.Open,
            address=_AN_ADDRESS,
            stop=lambda: None,
        )
        node, _discouraged, _logged = a_pending_getdata_node(
            conn, deque([1]), logger=logger
        )

        def boom(*_a: Any, exc: Exception = exc) -> bool:
            raise exc

        monkeypatch.setattr(main_module, "advance_getdata", boom)
        resume_getdata(node)
    logger.close()
    text = log_path.read_text(encoding="utf-8")
    (not_discouraged,) = [
        line
        for line in text.splitlines()
        if "Resuming getdata for connection 1 failed" in line
    ]
    (discouraged,) = [
        line
        for line in text.splitlines()
        if "Resuming getdata for connection 2 failed" in line
    ]
    assert "peer not discouraged" in not_discouraged
    assert "peer discouraged" in discouraged
    assert "peer not discouraged" not in discouraged
