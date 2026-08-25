# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""RpcManager.connections does not grow without bound, over a real node."""

import json
from typing import TYPE_CHECKING

from tests.helpers import post, wait_until, wait_until_listening

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
