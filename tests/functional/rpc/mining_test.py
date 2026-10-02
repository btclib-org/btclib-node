# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The mining RPCs, live: mine over the socket, then read the chain back.

A request that waits, or searches without end, is answered while the
node goes on serving: each runs in a thread of the test's own, and an
event set at the request's first step says it is under way.
"""

import threading
import time
from typing import TYPE_CHECKING, Any

from btclib.p2p.keepalive import Ping
from btclib.script.script_pub_key import ScriptPubKey

import btclib_node.rpc.mining as rpc_mining
from btclib_node import mining
from btclib_node.constants import P2pConnStatus
from tests import (
    anyone_can_spend,
    local_addr,
    rpc_client,
    wait_until,
    wait_until_listening,
)
from tests.conftest import node_context

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    import pytest

    from btclib_node import Node

ADDRESS = ScriptPubKey(anyone_can_spend(), "regtest").address

# how long a test waits for a request it started, or for its first step
BOUND = 30


def call(node: Node, method: str, params: list[object]) -> dict[str, Any]:
    """Send one request, on a connection of its own, and return the reply."""
    reply: dict[str, Any]
    _, reply = rpc_client(node, BOUND).call_raw(method, params, request_timeout=BOUND)
    return reply


class Pending:
    """A request sent on a thread of its own, its reply read once it ends."""

    def __init__(self, node: Node, method: str, params: list[object]) -> None:
        """Send `method` to `node` now."""
        self.reply: dict[str, Any] | None = None
        self.thread = threading.Thread(target=self._send, args=(node, method, params))
        self.thread.start()

    def _send(self, node: Node, method: str, params: list[object]) -> None:
        self.reply = call(node, method, params)

    def result(self) -> dict[str, Any]:
        """Return the reply, once it has come."""
        self.thread.join(BOUND)
        assert self.reply is not None
        return self.reply


def started(monkeypatch: pytest.MonkeyPatch, name: str) -> threading.Event:
    """Return an event set by the first step of `rpc.mining.<name>`'s job."""
    event = threading.Event()
    build = getattr(rpc_mining, name)

    def spy(*args: Any) -> Generator[bool, None, Any]:
        job = build(*args)
        event.set()
        return (yield from job)

    monkeypatch.setattr(rpc_mining, name, spy)
    return event


def test_mined_blocks_are_the_chain_and_the_template_builds_on_it(
    rpc_node: Node,
) -> None:
    """`generatetoaddress` and `generateblock` extend the chain they answer for.

    The template then builds on the new tip, and `submitblock` finds the
    mined block already held.
    """
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    address = ScriptPubKey(anyone_can_spend(), "regtest").address
    client = rpc_client(node)

    def call(method: str, params: list[object]) -> object:
        _, body = client.call_raw(method, params, jsonrpc="1.0", request_timeout=30)
        assert body["error"] is None
        return body["result"]

    hashes = call("generatetoaddress", [3, address])
    assert isinstance(hashes, list)
    assert call("getblockcount", []) == 3
    assert call("getbestblockhash", []) == hashes[-1]

    mined = call("generateblock", [address, []])
    assert isinstance(mined, dict)
    assert call("getbestblockhash", []) == mined["hash"]

    template = call("getblocktemplate", [{"rules": ["segwit"]}])
    assert isinstance(template, dict)
    assert template["previousblockhash"] == mined["hash"]
    assert template["height"] == 5

    block_hex = call("getblock", [mined["hash"], 0])
    assert call("submitblock", [block_hex]) == "duplicate"


def test_a_search_without_end_leaves_the_node_serving_until_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another request, a peer and `stop` are served while it searches.

    No nonce solves the block, and `maxtries` is unbounded, so the search
    ends only at `stop`, answering the blocks it found: none.
    """
    searching = threading.Event()

    def nothing_found(_header: object, _tries: int) -> None:
        searching.set()

    monkeypatch.setattr(mining, "mine", nothing_found)
    with (
        node_context(tmp_path / "miner") as miner,
        node_context(tmp_path / "peer", allow_rpc=False) as peer,
    ):
        wait_until_listening(miner.rpc_manager)
        wait_until_listening(miner.p2p_manager)
        wait_until_listening(peer.p2p_manager)
        search = Pending(miner, "generatetoaddress", [1, ADDRESS, -1])
        assert searching.wait(BOUND)

        assert call(miner, "getblockcount", [])["result"] == 0

        peer.p2p_manager.connect(local_addr(miner.p2p_port))
        wait_until(lambda: len(peer.p2p_manager.connections))
        conn = peer.p2p_manager.connections[0]
        wait_until(lambda: conn.status == P2pConnStatus.Connected)
        wait_until(lambda: conn.ping_nonce == 0)
        conn.ping_sent = time.time()
        conn.ping_nonce = 1
        conn.send(Ping(1))
        wait_until(lambda: conn.latency)

        assert call(miner, "stop", [])["result"] == "Btclib node stopping"
        assert search.result()["result"] == []


def test_a_wait_and_a_long_poll_are_answered_by_a_block_mined_meanwhile(
    rpc_node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both wait on the node's loop, and the block ends both waits."""
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    waiting = started(monkeypatch, "_wait_for_height")
    polling = started(monkeypatch, "_long_poll")
    template = call(node, "getblocktemplate", [{"rules": ["segwit"]}])["result"]
    wait = Pending(node, "waitforblockheight", [1])
    poll = Pending(
        node,
        "getblocktemplate",
        [{"rules": ["segwit"], "longpollid": template["longpollid"]}],
    )
    assert waiting.wait(BOUND)
    assert polling.wait(BOUND)

    [mined] = call(node, "generatetoaddress", [1, ADDRESS])["result"]

    assert wait.result()["result"] == {"hash": mined, "height": 1}
    assert poll.result()["result"]["previousblockhash"] == mined


def test_stop_answers_a_wait_and_a_long_poll(
    rpc_node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As Core answers them at shutdown: the tip, and `Shutting down`."""
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    waiting = started(monkeypatch, "_wait_for_height")
    polling = started(monkeypatch, "_long_poll")
    tip = call(node, "getbestblockhash", [])["result"]
    wait = Pending(node, "waitforblockheight", [100])
    poll = Pending(node, "getblocktemplate", [{"rules": ["segwit"], "longpollid": 0}])
    assert waiting.wait(BOUND)
    assert polling.wait(BOUND)

    assert call(node, "stop", [])["result"] == "Btclib node stopping"

    assert wait.result()["result"] == {"hash": tip, "height": 0}
    assert poll.result()["error"] == {"code": -9, "message": "Shutting down"}
