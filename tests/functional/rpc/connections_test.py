# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""RpcManager.connections does not grow without bound, over a real node."""

import json
import socket
from typing import TYPE_CHECKING

from tests import post, wait_until, wait_until_listening

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

    good = {"jsonrpc": "2.0", "id": "a", "method": "getbestblockhash"}
    for _ in range(11):
        assert json.loads(post(node, good))["result"]

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

    good = {"jsonrpc": "2.0", "id": "a", "method": "getbestblockhash"}
    assert json.loads(post(node, good))["result"]
