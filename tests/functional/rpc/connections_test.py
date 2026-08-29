# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""RpcManager.connections does not grow without bound, over a real node.

Every entry sheds once answered, and never survives to be reused: this
node's own `RpcConnection.async_send` closes its socket unconditionally
after one reply, so a client pooling a connection across several calls
still pays a fresh accept every time.
"""

import socket
from typing import TYPE_CHECKING

from bitcoin_core_rpc import SessionTransport

from tests import rpc_client, wait_until, wait_until_listening

if TYPE_CHECKING:
    from btclib_node import Node


def test_connections_do_not_outlive_the_answer_they_carried(rpc_node: Node) -> None:
    """A live node's own connections table sheds every entry once answered.

    Every request used to open a connection that was answered and had
    its socket closed by `async_send`, and the entry in
    `RpcManager.connections` stayed anyway -- an unbounded dict keyed on
    a counter that only grows. The port binds every interface with no
    authentication (issue #27), so this was a leak any client could
    drive, not just an internal bookkeeping detail (issue #64).
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    client = rpc_client(node)

    for _ in range(11):
        _, body = client.call_raw("getbestblockhash", jsonrpc="2.0")
        assert body["result"]

    wait_until(lambda: not node.rpc_manager.connections)


def test_a_client_that_sends_nothing_is_dropped_within_the_deadline(
    rpc_node: Node,
) -> None:
    """A client that connects and never sends a byte is dropped, not kept.

    ISS 437: `RpcConnection.run` used to await `sock_recv` with no
    deadline anywhere in it, so a client shaped exactly like this one
    held its socket -- and its entry in `RpcManager.connections` -- for
    the life of the node. `request_timeout` is lowered on the live
    manager before the stalled connection is opened, rather than left at
    `REQUEST_TIMEOUT`'s own real, Core-matching default, so this test
    does not itself wait thirty seconds to prove it.

    A well-behaved request made against the same, still-lowered deadline
    is answered normally, which is the other half of this: the fix
    bounds an unproductive read and does not touch an ordinary one.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    node.rpc_manager.request_timeout = 0.5

    # 60s, not a bound close to the 0.5s deadline under test: this is the
    # client's own patience, and it must not be what expires first on a
    # machine slow enough to delay RpcManager's own loop past a tight
    # margin -- wait_until's own default below is headroom measured the
    # same way, against this suite's own worst tail under load.
    stalled = socket.create_connection(("127.0.0.1", node.rpc_port), timeout=60)
    try:
        # the server closing its end is read here as EOF, b"" -- not as
        # data, and not as the read simply never returning, which a
        # missing deadline is what this issue was
        assert stalled.recv(1) == b""
    finally:
        stalled.close()
    wait_until(lambda: not node.rpc_manager.connections)

    _, body = rpc_client(node).call_raw("getbestblockhash", jsonrpc="2.0")
    assert body["result"]


def test_one_session_transport_still_opens_a_socket_per_call(rpc_node: Node) -> None:
    """One `SessionTransport`, three calls: the listener is asked for three.

    `SessionTransport` keeps a connection open across calls and probes
    it before reusing it (its own docstring), which is what lets a
    caller against Core skip a handshake per call -- Core keeps an rpc
    connection alive and drops an idle one with a plain `close()`. This
    node's own `RpcConnection.async_send` closes its socket
    unconditionally after every reply, with no idling and no keep-alive
    to probe for, so the same `SessionTransport` instance still pays a
    fresh accept on every call: the probe evicts the connection this
    node already closed and reconnects rather than finding one to reuse.
    `RpcManager.last_connection_id` is incremented once per accepted
    socket (`rpc/manager.py`), so three distinct values across three
    calls is three distinct connections, not one kept open.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    client = rpc_client(node)
    transport = SessionTransport()
    client.transport = transport

    accepted: list[int] = []
    try:
        for _ in range(3):
            _, body = client.call_raw("getbestblockhash", jsonrpc="2.0")
            assert body["result"]
            # waited for before the next call, so the next call's own
            # probe meets a socket this node has already closed rather
            # than racing the close still in flight
            wait_until(lambda: not node.rpc_manager.connections)
            accepted.append(node.rpc_manager.last_connection_id)
    finally:
        # otherwise the pooled connection's own socket is closed only
        # when the garbage collector reaches it, which is what
        # `filterwarnings = ["error", ...]` (pyproject.toml) turns a
        # bare `ResourceWarning` on that late a close into
        transport.close()

    assert accepted == [0, 1, 2]
