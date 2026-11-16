# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""RpcManager.connections does not grow without bound, over a real node.

Every entry sheds once its own connection closes, and a request asking
for `Connection: close` -- every request `tests.rpc_client`'s default
transport sends, `urllib`'s own `do_open` setting the header
unconditionally -- gets exactly that (`rpc.connection.RpcConnection`'s
own module docstring is where a kept-alive request instead is argued,
matching Core's HTTP/1.1 default).
"""

import socket
from typing import TYPE_CHECKING

from bitcoin_core_rpc import SessionTransport

from tests import rpc_client, wait_until, wait_until_listening

if TYPE_CHECKING:
    from btclib_node import Node


def test_connections_do_not_outlive_the_answer_they_carried(rpc_node: Node) -> None:
    """A live node's own connections table sheds every entry once closed.

    Every request here asks for `Connection: close` (the default
    transport's own choice, not this test's), so each one closes its
    socket once answered, and its entry in `RpcManager.connections` goes
    with it: left behind, entries would grow a dict keyed on a counter
    that only grows. An entry is made at accept, before any credential
    is checked, so such a leak is one any client could drive, not just
    an internal bookkeeping detail (issue #64).
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

    ISS 437: `RpcConnection.run` closes a connection idle for longer
    than `idle_timeout`, so a client shaped exactly like this one does
    not hold its socket -- and its entry in `RpcManager.connections` --
    for the life of the node. `idle_timeout` is lowered on the live
    manager before the stalled connection is opened, so that this test
    does not wait out `IDLE_TIMEOUT`.

    A well-behaved request made against the same, still-lowered deadline
    is answered normally, which is the other half of this: the deadline
    bounds an unproductive read and does not touch an ordinary one.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    node.rpc_manager.idle_timeout = 0.5

    # 60s, not a bound close to the 0.5s deadline under test: this is the
    # client's own patience, and it must not be what expires first on a
    # machine slow enough to delay RpcManager's own loop past a tight
    # margin -- wait_until's own default below is headroom measured the
    # same way, against this suite's own worst tail under load.
    stalled = socket.create_connection(("127.0.0.1", node.rpc_port), timeout=60)
    try:
        # the server closing its end is read here as EOF, b"" -- not as
        # data, and not as the read simply never returning, which is
        # what a missing deadline would give
        assert stalled.recv(1) == b""
    finally:
        stalled.close()
    wait_until(lambda: not node.rpc_manager.connections)

    _, body = rpc_client(node).call_raw("getbestblockhash", jsonrpc="2.0")
    assert body["result"]


def test_one_session_transport_keeps_one_socket_across_calls(rpc_node: Node) -> None:
    """One `SessionTransport`, three calls: all three answered on one socket.

    `SessionTransport` keeps a connection open across calls and probes
    it before reusing it (its own docstring), which is what lets a
    caller against Core skip a handshake per call -- Core keeps an rpc
    connection alive and drops an idle one with a plain `close()`. This
    node's own `RpcConnection.async_send` does the same by default
    (issue #640), so the same `SessionTransport` instance finds its
    pooled connection still open on every later call rather than paying
    a fresh accept: `RpcManager.last_connection_id` is incremented once
    per socket `server` takes (`rpc/manager.py`), so it stays at the one
    value the first call gave it.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    client = rpc_client(node)
    transport = SessionTransport()
    client.transport = transport

    try:
        for _ in range(3):
            _, body = client.call_raw("getbestblockhash", jsonrpc="2.0")
            assert body["result"]
    finally:
        # otherwise the pooled connection's own socket is closed only
        # when the garbage collector reaches it, which is what
        # `filterwarnings = ["error", ...]` (pyproject.toml) turns a
        # bare `ResourceWarning` on that late a close into
        transport.close()

    assert node.rpc_manager.last_connection_id == 0


def test_many_unpaced_calls_over_one_session_transport_do_not_reset(
    rpc_node: Node,
) -> None:
    """Hundreds of back-to-back calls over one `SessionTransport`, no reset.

    ISS 640: a reply that closed its socket outright would let a pooled
    client's next call land in the narrow window between this node's own
    close and `SessionTransport`'s own pre-reuse probe seeing it -- a
    bare `ConnectionResetError` out of `getresponse()`, which
    `SessionTransport`'s own docstring says it does not try to recover
    from. Keeping the connection open across replies (`rpc.connection.
    RpcConnection.async_send`) removes the close this race needs: with
    nothing intervening between one reply and the next request on the
    same socket, there is no window left for the probe to race, which
    `last_connection_id` staying put across every one of these calls is
    what confirms -- not a statistical absence of the failure, which
    this many unpaced calls is not enough to reliably reproduce (the
    issue's own report is one failure in roughly two thousand).

    This is 300 sequential real round trips over one socket, each on
    `bitcoin_core_rpc.transport`'s own fresh, per-call timeout rather
    than one shared across all 300, so a single-test `FetchError: ...
    timed out` here means one of 300 individually stalled that long --
    a far larger exposure to external load than any other test carries.

    `rpc.main.handle_rpc` does not pop `manager.connections`'s own entry
    for this connection (issue #688): a pop there, on `Node`'s own
    thread, is not ordered against `RpcConnection.async_send`, on
    `RpcManager`'s own thread, writing the reply, re-arming the
    connection for its next request and reading that next request whole
    -- all in one burst neither a small `sock_sendall` nor an
    already-buffered `sock_recv` has to suspend for. Such a pop landing
    before that next request reaches `Node` would leave
    `rpc.main.get_connection` finding nothing for it and `handle_rpc`
    silently returning, answering nobody, while
    `bitcoin_core_rpc.transport.SessionTransport`'s client-side read
    blocks for the whole of its own per-call timeout with no reply, no
    close and no reset ever arriving. `RpcManager.create_connection`
    puts the entry in once, at accept, and
    `RpcManager.forget_connection` takes it out, called from
    `RpcConnection`'s own close paths and never from `Node`'s thread. A
    failure here, at any load, is read as a defect and not rerun away
    (issue #664, issue #688).
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    client = rpc_client(node)
    transport = SessionTransport()
    client.transport = transport

    try:
        for _ in range(300):
            _, body = client.call_raw("getbestblockhash", jsonrpc="2.0")
            assert body["result"]
    finally:
        transport.close()

    assert node.rpc_manager.last_connection_id == 0
