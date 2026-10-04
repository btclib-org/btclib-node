# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`gettxoutsetinfo`'s `hash_serialized_3` scan, live.

The scan runs on a thread of its own and the request waits in the node's
loop, so the loop goes on serving, and a `stop` ends the request. The scan
itself is a stand-in here, gated by events: the hash is checked against
Core's in `tests/unit`.
"""

import threading
import time
from typing import TYPE_CHECKING, Any

from btclib_node.chainstate.utxo_index import UtxoIndex
from tests import rpc_client, wait_until_listening

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from btclib_node import Node

# how long a test waits for a request it started, or for its scan to begin
BOUND = 30


def call(node: Node, method: str, params: list[object]) -> dict[str, Any]:
    """Send one request, on a connection of its own, and return the reply."""
    reply: dict[str, Any]
    _, reply = rpc_client(node, BOUND).call_raw(method, params, request_timeout=BOUND)
    return reply


def scan_by(
    monkeypatch: pytest.MonkeyPatch,
    fake: Callable[[object, Callable[[], None]], bytes | None],
) -> threading.Event:
    """Replace the UTXO scan with `fake`; return an event set as it begins."""
    walking = threading.Event()

    def begin(cursor: object, interruption_point: Callable[[], None]) -> bytes | None:
        walking.set()
        return fake(cursor, interruption_point)

    monkeypatch.setattr(UtxoIndex, "serialized_hash", staticmethod(begin))
    return walking


def test_the_loop_serves_another_request_while_the_scan_runs(
    rpc_node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request made during the scan is answered before the scan is."""
    node = rpc_node
    wait_until_listening(node.rpc_manager)
    release = threading.Event()

    def fake(*_: object) -> bytes:
        release.wait(BOUND)
        return b"\x02" * 32

    walking = scan_by(monkeypatch, fake)
    replies: list[dict[str, Any]] = []
    asker = threading.Thread(
        target=lambda: replies.append(call(node, "gettxoutsetinfo", []))
    )
    asker.start()
    assert walking.wait(BOUND)

    assert call(node, "getblockcount", [])["result"] == 0
    assert replies == []

    release.set()
    asker.join(BOUND)
    [reply] = replies
    assert reply["result"]["hash_serialized_3"] == (b"\x02" * 32)[::-1].hex()


def test_stop_ends_a_scan_in_progress(
    rpc_node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan's interruption point answers `Shutting down` at `stop`."""
    node = rpc_node
    wait_until_listening(node.rpc_manager)

    def fake(_: object, interruption_point: Callable[[], None]) -> bytes:
        while True:
            interruption_point()
            time.sleep(0.001)

    walking = scan_by(monkeypatch, fake)
    replies: list[dict[str, Any]] = []
    asker = threading.Thread(
        target=lambda: replies.append(call(node, "gettxoutsetinfo", []))
    )
    asker.start()
    assert walking.wait(BOUND)

    assert call(node, "stop", [])["result"] == "Btclib node stopping"

    asker.join(BOUND)
    [reply] = replies
    assert reply["error"] == {"code": -9, "message": "Shutting down"}
