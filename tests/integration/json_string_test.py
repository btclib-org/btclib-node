# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

r"""A string in a reply, written as bitcoind writes it.

ISS 2492. Each body is sent to this node and to a real bitcoind, with and
without `-rpcwhitelist`, and the status and the bytes of the two replies
are held equal: a character outside ASCII as its UTF-8, DEL and a control
character as a `\u` escape.
"""

import base64
from typing import TYPE_CHECKING

from bitcoin_core_rpc import http_request

from btclib_node import Node
from btclib_node.config import Config
from tests import authorization, get_random_port, wait_until_listening
from tests.integration.conftest import COOKIE_WHITELIST

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

_ID = '"é\\u007f\\u0001"'
# every ASCII character, each as its escape
_ASCII = '"' + "".join(f"\\u{code:04x}" for code in range(0x80)) + '"'
_BODIES = [
    f'{{"method":"getblockcount","id":{_ID}}}',
    '{"method":"getblockcount","id":"\x7f"}',
    f'{{"method":"getblockcount","id":{_ASCII}}}',
    '{"method":"getblockcount","id":{"é\\u007f\\u0001":"…\U0001f600"}}',
    # refused as `JSONRPCRequest::parse` refuses it, which a whitelisted
    # user's request meets before the whitelist
    f'{{"method":1,"id":{_ID}}}',
]


def _answer(port: int, authorization_value: str, body: str) -> tuple[int, bytes]:
    """Return the status and the body `body` is answered with."""
    return http_request(
        f"http://127.0.0.1:{port}",
        data=body.encode(),
        headers={"Authorization": authorization_value},
        timeout=10,
    )


def _held_equal(bitcoind: Bitcoind, tmp_path: Path, *rpcwhitelist: str) -> None:
    """Send each body to this node and to `bitcoind`, and compare replies."""
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node",
            p2p_port=get_random_port(),
            rpc_port=get_random_port(),
            rpcwhitelist=list(rpcwhitelist),
        )
    )
    node.start()
    try:
        wait_until_listening(node.rpc_manager)
        assert node.rpc_port is not None
        ours = authorization(node.config.data_dir)
        theirs = f"Basic {base64.b64encode(bitcoind.cookie_path.read_bytes()).decode()}"
        for body in _BODIES:
            assert _answer(node.rpc_port, ours, body) == _answer(
                bitcoind.rpc_port, theirs, body
            ), body
    finally:
        node.stop()
        node.join()


def test_a_string_is_written_as_bitcoind_writes_it(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Each body is answered alike."""
    _held_equal(bitcoind, tmp_path)


def test_a_whitelisted_user_is_answered_as_bitcoind_answers_it(
    bitcoind_whitelisted: Bitcoind, tmp_path: Path
) -> None:
    """Each body is answered alike under `-rpcwhitelist`."""
    _held_equal(bitcoind_whitelisted, tmp_path, COOKIE_WHITELIST)
