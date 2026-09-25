# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Whom a real node's RPC listener answers: its cookie and `-rpcauth`."""

import base64
import json
from typing import TYPE_CHECKING

from bitcoin_core_rpc import BitcoinCoreRpcClient, http_request

from btclib_node import Node
from btclib_node.config import Config
from tests import (
    RPCAUTH,
    RPCAUTH_PASSWORD,
    RPCAUTH_USER,
    cookie_path,
    get_random_port,
    rpc_client,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path

REQUEST = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getblockcount"}).encode()


def status_of(node: Node, headers: dict[str, str]) -> int:
    """POST `REQUEST` to `node` with `headers` alone; return the HTTP status."""
    status, _ = http_request(
        f"http://127.0.0.1:{node.rpc_port}", data=REQUEST, headers=headers, timeout=5
    )
    return status


def test_a_request_with_no_credential_is_refused(rpc_node: Node) -> None:
    """401, where every other test here reaches the node with its cookie."""
    wait_until_listening(rpc_node.rpc_manager)
    assert status_of(rpc_node, {}) == 401


def test_a_wrong_password_is_refused(rpc_node: Node) -> None:
    """The cookie's own user, with a password that is not the cookie's."""
    wait_until_listening(rpc_node.rpc_manager)
    wrong = base64.b64encode(b"__cookie__:" + b"0" * 64).decode()
    assert status_of(rpc_node, {"Authorization": "Basic " + wrong}) == 401


def test_the_cookie_is_accepted(rpc_node: Node) -> None:
    """A client that reads `.cookie` gets an answer."""
    wait_until_listening(rpc_node.rpc_manager)
    assert rpc_client(rpc_node).call("getblockcount") == 0


def test_an_rpcauth_user_is_accepted_beside_the_cookie(tmp_path: Path) -> None:
    """`-rpcauth`'s user and password, and the cookie Core writes beside it."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
            rpcauth=[RPCAUTH],
        )
    )
    node.start()
    try:
        wait_until_listening(node.rpc_manager)
        client = BitcoinCoreRpcClient(
            f"http://127.0.0.1:{node.rpc_port}",
            user=RPCAUTH_USER,
            password=RPCAUTH_PASSWORD,
            timeout=5,
        )
        assert client.call("getblockcount") == 0
        assert rpc_client(node).call("getblockcount") == 0
    finally:
        node.stop()


def test_a_stopped_node_leaves_no_cookie(tmp_path: Path) -> None:
    """Written at start, deleted once the node stops, as `bitcoind` does."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
        )
    )
    path = cookie_path(node.config.data_dir)
    node.start()
    try:
        wait_until_listening(node.rpc_manager)
        assert path.exists()
    finally:
        node.stop()
    assert not path.exists()
