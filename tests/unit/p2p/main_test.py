# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Which message reaches a callback, and what happens when one raises.

The dispatch is gated on the connection's status, and the functional
tests only ever drive a connection that reaches Connected: the states
either side of it, and the raise, are what is left.
"""

from collections import deque
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from btclib.exceptions import BTClibValueError
from btclib.p2p.addrv2 import NetworkAddressV2

from btclib_node.constants import P2pConnStatus
from btclib_node.p2p import main as main_module
from btclib_node.p2p.callbacks import callbacks, handshake_callbacks
from btclib_node.p2p.main import handle_p2p, handle_p2p_handshake, resume_cfilters

if TYPE_CHECKING:
    import pytest

    from btclib_node import Node
    from btclib_node.p2p.connection import Connection


_AN_ADDRESS = NetworkAddressV2(0, 0, 1, b"\x01\x02\x03\x04", 18444)


def make_node(
    queue_name: str,
    item: tuple[str, bytes, int],
    *,
    status: P2pConnStatus,
    present: bool = True,
    pending: bool = False,
) -> tuple[Any, list[bool]]:
    """Build a node stand-in, one queued `item` on a `Connection` at `status`.

    `present` files the connection under `connections` or leaves it out entirely
    (a peer already gone by the time its message is handled); `pending` moves it
    into `pending_connections` instead, for the handshake's own in-between
    state. Returns the node alongside the list `conn.stop` was told to record
    onto.
    """
    stopped: list[bool] = []
    discouraged: list[Any] = []
    conn = SimpleNamespace(
        status=status, address=_AN_ADDRESS, stop=lambda: stopped.append(True)
    )
    manager = SimpleNamespace(
        messages=deque(),
        handshake_messages=deque(),
        connections={0: conn} if present and not pending else {},
        pending_connections={0: conn} if present and pending else {},
        discourage=discouraged.append,
        discouraged=discouraged,
    )
    getattr(manager, queue_name).append(item)
    node = SimpleNamespace(
        p2p_manager=manager,
        logger=SimpleNamespace(
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
        "handshake_messages", ("verack", b"", 0), status=P2pConnStatus.Open
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
        "handshake_messages", ("verack", b"", 0), status=P2pConnStatus.Closed
    )
    handle_p2p_handshake(node)
    assert not stopped


def test_a_handshake_message_on_a_connected_one_drops_the_peer() -> None:
    """A handshake message arriving after `Connected` gets the peer dropped.

    The handshake is over: a second `version` or `verack` is a peer not
    speaking the protocol, and discouraged for it -- #283.
    """
    node, stopped = make_node(
        "handshake_messages", ("verack", b"", 0), status=P2pConnStatus.Connected
    )
    handle_p2p_handshake(node)
    assert stopped == [True]
    assert node.p2p_manager.discouraged == [_AN_ADDRESS]


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
        "handshake_messages", ("verack", b"", 0), status=P2pConnStatus.Open
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
        "handshake_messages", ("verack", b"", 0), status=P2pConnStatus.Open
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
        ("verack", b"", 7),
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
        ("verack", b"", 0),
        status=P2pConnStatus.Open,
        pending=True,
    )
    handle_p2p_handshake(node)
    assert seen == [b""]
    assert not stopped


def test_a_message_reaches_its_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ordinary message on a `Connected` connection reaches its callback."""
    seen: list[bytes] = []

    def a_callback(node: Node, msg: bytes, conn: Connection) -> None:
        seen.append(msg)

    monkeypatch.setitem(callbacks, "ping", a_callback)
    node, stopped = make_node(
        "messages", ("ping", b"x", 0), status=P2pConnStatus.Connected
    )
    handle_p2p(node)
    assert seen == [b"x"]
    assert not stopped


def test_a_message_before_the_handshake_is_over_drops_the_peer() -> None:
    """An ordinary message arriving on an `Open` connection drops the peer.

    Anything but a handshake command before `Connected` is a protocol
    violation, discouraged for it -- #283, the mirror of the handshake
    version above.
    """
    node, stopped = make_node("messages", ("ping", b"", 0), status=P2pConnStatus.Open)
    handle_p2p(node)
    assert stopped == [True]
    assert node.p2p_manager.discouraged == [_AN_ADDRESS]  # #283


def test_a_message_on_a_closed_connection_is_dropped() -> None:
    """An ordinary message for a `Closed` connection is dropped, not run."""
    node, stopped = make_node("messages", ("ping", b"", 0), status=P2pConnStatus.Closed)
    handle_p2p(node)
    assert not stopped


def test_a_command_nothing_dispatches_is_ignored() -> None:
    """A command with no entry in `callbacks` is ignored, not dropped."""
    node, stopped = make_node(
        "messages", ("nosuchcommand", b"", 0), status=P2pConnStatus.Connected
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
        "messages", ("ping", b"", 0), status=P2pConnStatus.Connected
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
        "messages", ("ping", b"", 0), status=P2pConnStatus.Connected
    )
    handle_p2p(node)
    assert stopped == [True]
    assert node.p2p_manager.discouraged == [_AN_ADDRESS]  # #283


def test_a_message_for_a_connection_that_is_gone_is_dropped() -> None:
    """A message naming an unknown connection id is dropped, not run."""
    node, stopped = make_node(
        "messages", ("ping", b"", 7), status=P2pConnStatus.Connected, present=False
    )
    handle_p2p(node)
    assert not stopped


def test_a_message_on_a_connection_still_pending_drops_the_peer() -> None:
    """A non-handshake message on a still-`pending` connection drops the peer.

    Anything but the four handshake commands, arriving before `verack`
    promotes the connection: a protocol violation whether the sender is
    found in `connections` or still in `pending_connections`.
    """
    node, stopped = make_node(
        "messages", ("ping", b"", 0), status=P2pConnStatus.Open, pending=True
    )
    handle_p2p(node)
    assert stopped == [True]


def a_pending_node(conn: Any, heights: deque[int]) -> tuple[Any, list[Any], list[Any]]:
    """Build a node stand-in with one connection on `pending_cfilters`.

    Returns the node alongside the lists its `p2p_manager.discourage` and
    `logger.exception` calls are recorded into.
    """
    discouraged: list[Any] = []
    logged: list[Any] = []
    node = SimpleNamespace(
        pending_cfilters={conn.id: (conn, heights)},
        p2p_manager=SimpleNamespace(discourage=discouraged.append),
        logger=SimpleNamespace(exception=logged.append),
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
        called.append(a)  # pragma: no cover -- never reached
        return True  # pragma: no cover -- never reached

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
