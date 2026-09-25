# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Whom a real node's RPC listener answers, and what it lets them call."""

import base64
import json
import os
import stat
from typing import TYPE_CHECKING, Any

import pytest
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


def regtest_node(tmp_path: Path, **kwargs: Any) -> Node:
    """Return a node on `tmp_path`, RPC only, with `kwargs` for its `Config`."""
    return Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
            **kwargs,
        )
    )


def test_rpcpassword_is_accepted_and_the_whitelist_refuses_stop(
    tmp_path: Path,
) -> None:
    """No cookie beside `-rpcpassword`, and a 403 for a method not listed.

    The node still answers afterwards: the refused `stop` never ran.
    """
    node = regtest_node(
        tmp_path,
        rpcuser=RPCAUTH_USER,
        rpcpassword=RPCAUTH_PASSWORD,
        rpcwhitelist=[RPCAUTH_USER + ":getblockcount"],
    )
    node.start()
    try:
        wait_until_listening(node.rpc_manager)
        assert not cookie_path(node.config.data_dir).exists()
        userpass = f"{RPCAUTH_USER}:{RPCAUTH_PASSWORD}".encode()
        headers = {"Authorization": "Basic " + base64.b64encode(userpass).decode()}
        stop = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "stop"}).encode()
        url = f"http://127.0.0.1:{node.rpc_port}"
        assert http_request(url, data=stop, headers=headers, timeout=5) == (403, b"")
        client = BitcoinCoreRpcClient(
            url, user=RPCAUTH_USER, password=RPCAUTH_PASSWORD, timeout=5
        )
        assert client.call("getblockcount") == 0
    finally:
        node.stop()


def test_norpccookiefile_writes_none_and_rpcauth_still_answers(
    tmp_path: Path,
) -> None:
    """`-norpccookiefile` beside `-rpcauth`: the user, and no cookie."""
    node = regtest_node(tmp_path, rpcauth=[RPCAUTH], rpccookiefile=None)
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
        assert not cookie_path(node.config.data_dir).exists()
    finally:
        node.stop()


def test_rpccookiefile_places_the_cookie_a_client_then_reads(tmp_path: Path) -> None:
    """Written where `-rpccookiefile` says, and deleted there at stop."""
    node = regtest_node(tmp_path, rpccookiefile="mine")
    path = node.config.data_dir / "mine"
    node.start()
    try:
        wait_until_listening(node.rpc_manager)
        client = BitcoinCoreRpcClient(
            f"http://127.0.0.1:{node.rpc_port}", cookie_path=path, timeout=5
        )
        assert client.call("getblockcount") == 0
        assert not cookie_path(node.config.data_dir).exists()
    finally:
        node.stop()
    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_rpccookieperms_widens_the_cookie_a_node_writes(tmp_path: Path) -> None:
    """`-rpccookieperms=group` is mode 0640, as `bitcoind` v31.1.0 leaves it."""
    node = regtest_node(tmp_path, rpccookieperms="group")
    node.start()
    try:
        wait_until_listening(node.rpc_manager)
        path = cookie_path(node.config.data_dir)
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
    finally:
        node.stop()


def test_an_rpccookiefile_through_a_missing_directory_still_starts(
    tmp_path: Path,
) -> None:
    """`missing/../mycookie` is `mycookie`, as `bitcoind` v31.1.0 writes it."""
    node = regtest_node(tmp_path, rpccookiefile="missing/../mycookie")
    node.start()
    try:
        wait_until_listening(node.rpc_manager)
        assert (node.config.data_dir / "mycookie").exists()
    finally:
        node.stop()
