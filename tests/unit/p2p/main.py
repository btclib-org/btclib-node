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

from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.main import (
    callbacks,
    handle_p2p,
    handle_p2p_handshake,
    handshake_callbacks,
)


def make_node(queue_name, item, *, status, present=True):
    stopped = []
    conn = SimpleNamespace(status=status, stop=lambda: stopped.append(True))
    manager = SimpleNamespace(
        messages=deque(),
        handshake_messages=deque(),
        connections={0: conn} if present else {},
    )
    getattr(manager, queue_name).append(item)
    node = SimpleNamespace(
        p2p_manager=manager,
        logger=SimpleNamespace(
            info=lambda *a: None, debug=lambda *a: None, exception=lambda *a: None
        ),
    )
    return node, stopped


def test_a_handshake_message_reaches_its_callback(monkeypatch):
    seen = []
    monkeypatch.setitem(
        handshake_callbacks,
        "verack",
        lambda node, msg, conn: seen.append(msg),
    )
    node, stopped = make_node(
        "handshake_messages", ("verack", b"", 0), status=P2pConnStatus.Open
    )
    handle_p2p_handshake(node)
    assert seen == [b""]
    assert not stopped


def test_a_handshake_message_on_a_closed_connection_is_dropped():
    node, stopped = make_node(
        "handshake_messages", ("verack", b"", 0), status=P2pConnStatus.Closed
    )
    handle_p2p_handshake(node)
    assert not stopped


def test_a_handshake_message_on_a_connected_one_drops_the_peer():
    # the handshake is over: a second version or verack is a peer not
    # speaking the protocol
    node, stopped = make_node(
        "handshake_messages", ("verack", b"", 0), status=P2pConnStatus.Connected
    )
    handle_p2p_handshake(node)
    assert stopped == [True]


def test_a_handshake_callback_that_raises_drops_the_peer(monkeypatch):
    def boom(node, msg, conn):
        raise RuntimeError("no")

    monkeypatch.setitem(
        handshake_callbacks,
        "verack",
        boom,
    )
    node, stopped = make_node(
        "handshake_messages", ("verack", b"", 0), status=P2pConnStatus.Open
    )
    handle_p2p_handshake(node)
    assert stopped == [True]


def test_a_handshake_message_for_a_connection_that_is_gone_is_dropped():
    node, stopped = make_node(
        "handshake_messages",
        ("verack", b"", 7),
        status=P2pConnStatus.Open,
        present=False,
    )
    handle_p2p_handshake(node)
    assert not stopped


def test_a_message_reaches_its_callback(monkeypatch):
    seen = []
    monkeypatch.setitem(
        callbacks,
        "ping",
        lambda node, msg, conn: seen.append(msg),
    )
    node, stopped = make_node(
        "messages", ("ping", b"x", 0), status=P2pConnStatus.Connected
    )
    handle_p2p(node)
    assert seen == [b"x"]
    assert not stopped


def test_a_message_before_the_handshake_is_over_drops_the_peer():
    node, stopped = make_node("messages", ("ping", b"", 0), status=P2pConnStatus.Open)
    handle_p2p(node)
    assert stopped == [True]


def test_a_message_on_a_closed_connection_is_dropped():
    node, stopped = make_node("messages", ("ping", b"", 0), status=P2pConnStatus.Closed)
    handle_p2p(node)
    assert not stopped


def test_a_command_nothing_dispatches_is_ignored():
    node, stopped = make_node(
        "messages", ("nosuchcommand", b"", 0), status=P2pConnStatus.Connected
    )
    handle_p2p(node)
    assert not stopped


def test_a_callback_that_raises_drops_the_peer(monkeypatch):
    def boom(node, msg, conn):
        raise RuntimeError("no")

    monkeypatch.setitem(
        callbacks,
        "ping",
        boom,
    )
    node, stopped = make_node(
        "messages", ("ping", b"", 0), status=P2pConnStatus.Connected
    )
    handle_p2p(node)
    assert stopped == [True]


def test_a_message_for_a_connection_that_is_gone_is_dropped():
    node, stopped = make_node(
        "messages", ("ping", b"", 7), status=P2pConnStatus.Connected, present=False
    )
    handle_p2p(node)
    assert not stopped
