# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A request sent while the stores load is answered `RPC_IN_WARMUP`.

ISS 1317. Core's `CRPCTable::execute` answers every method that way until
`AppInitMain` calls `SetRPCWarmupFinished`. The block index is held open
here, as a slow load would hold it, with the RPC listener already bound.
"""

import json
import threading
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import RPCErrorCode

from btclib_node import Node, chainstate
from btclib_node.config import Config
from tests import get_random_port, post, wait_until, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

LOADING = "Loading block index…"


def test_a_request_during_the_load_is_answered_in_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every method is refused with the init message, then served after it."""
    opening = threading.Event()
    release = threading.Event()
    original = chainstate.Chainstate

    def held_open(*args: Any, **kwargs: Any) -> chainstate.Chainstate:
        opening.set()
        release.wait(30)
        return original(*args, **kwargs)

    monkeypatch.setattr("btclib_node.Chainstate", held_open)
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
        )
    )
    starter = threading.Thread(target=node.start)
    starter.start()
    try:
        wait_until_listening(node.rpc_manager, past_warmup=False)
        assert opening.wait(30)

        for method in ("getblockcount", "help", "stop", "nosuchmethod"):
            body = json.loads(post(node, {"method": method, "params": [], "id": 1}))
            assert body["error"] == {
                "code": RPCErrorCode.IN_WARMUP,
                "message": LOADING,
            }
            assert body["id"] == 1

        batch = json.loads(
            post(
                node,
                [
                    {"jsonrpc": "2.0", "id": "a", "method": "getblockcount"},
                    {"jsonrpc": "2.0", "id": "b", "method": "stop"},
                ],
            )
        )
        assert [(m["id"], m["error"]["code"]) for m in batch] == [
            ("a", RPCErrorCode.IN_WARMUP),
            ("b", RPCErrorCode.IN_WARMUP),
        ]

        release.set()
        starter.join(30)
        wait_until(lambda: not node.rpc_manager.in_warmup)

        assert node.is_alive()
        body = json.loads(
            post(node, {"method": "getblockcount", "params": [], "id": 2})
        )
        assert body["error"] is None
        assert body["result"] == 0
    finally:
        release.set()
        starter.join(30)
        node.stop()
