# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A JSON-RPC body nested past Core's `MAX_JSON_DEPTH`, as bitcoind answers it.

ISS 2468. Each body is sent to this node and to a real bitcoind, and the
status and the body of the two replies are held equal.
"""

import base64
from typing import TYPE_CHECKING

from bitcoin_core_rpc import http_request

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import MAX_JSON_DEPTH
from tests import authorization, get_random_port, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind


def _bodies(depth: int) -> list[str]:
    """Return bodies holding `depth` arrays and objects open at once.

    A key no method reads holding arrays, and one holding objects, and a
    batch whose one member is nested in arrays.
    """
    nested = depth - 1
    return [
        '{"method":"getblockcount","id":1,"x":' + "[" * nested + "]" * nested + "}",
        '{"method":"getblockcount","id":1,"x":'
        + '{"a":' * (nested - 1)
        + "{}"
        + "}" * (nested - 1)
        + "}",
        "[" * nested + '{"method":"getblockcount","id":1}' + "]" * nested,
    ]


def _answer(port: int, authorization_value: str, body: str) -> tuple[int, bytes]:
    """Return the status and the body `body` is answered with."""
    return http_request(
        f"http://127.0.0.1:{port}",
        data=body.encode(),
        headers={"Authorization": authorization_value},
        timeout=10,
    )


def test_a_body_nested_past_max_json_depth_is_answered_as_bitcoind_answers_it(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Each body is answered alike, as deep as Core reads and past it."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
        )
    )
    node.start()
    try:
        wait_until_listening(node.rpc_manager)
        assert node.rpc_port is not None
        ours = authorization(node.config.data_dir)
        theirs = f"Basic {base64.b64encode(bitcoind.cookie_path.read_bytes()).decode()}"
        for depth in (MAX_JSON_DEPTH, MAX_JSON_DEPTH + 1, 100_000):
            for body in _bodies(depth):
                assert _answer(node.rpc_port, ours, body) == _answer(
                    bitcoind.rpc_port, theirs, body
                ), (depth, body[:40])
    finally:
        node.stop()
        node.join()
