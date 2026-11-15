# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`-0` for an argument read as an unsigned integer, as bitcoind answers it.

ISS 2491. Each body is written as text, since `json.dumps` writes `-0`
as `0`, and sent to this node and to a real bitcoind; the status and
the body of the two replies are held equal.
"""

import base64
from typing import TYPE_CHECKING

from bitcoin_core_rpc import http_request

from btclib_node import Node
from btclib_node.config import Config
from tests import authorization, get_random_port, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

_TXID = "11" * 32

# `n` and `port` as `-0`, by position and by name, in both envelopes; then
# the controls: `0` as `n`, `-0` as a signed count, and the address the
# refused calls name, which neither node holds yet
_BODIES = [
    f'{{"method":"gettxout","params":["{_TXID}",-0],"id":1}}',
    f'{{"method":"gettxout","params":{{"txid":"{_TXID}","n":-0}},"id":1}}',
    f'{{"jsonrpc":"2.0","method":"gettxout","params":["{_TXID}",-0],"id":1}}',
    '{"method":"addpeeraddress","params":["1.2.3.4",-0],"id":1}',
    '{"method":"addpeeraddress","params":{"address":"1.2.3.4","port":-0},"id":1}',
    '{"jsonrpc":"2.0","method":"addpeeraddress","params":["1.2.3.4",-0],"id":1}',
    f'{{"method":"gettxout","params":["{_TXID}",0],"id":1}}',
    '{"method":"getnodeaddresses","params":[-0],"id":1}',
    '{"method":"addpeeraddress","params":["1.2.3.4",8333],"id":1}',
]


def _answer(port: int, authorization_value: str, body: str) -> tuple[int, bytes]:
    """Return the status and the body `body` is answered with."""
    return http_request(
        f"http://127.0.0.1:{port}",
        data=body.encode(),
        headers={"Authorization": authorization_value},
        timeout=10,
    )


def test_negative_zero_is_answered_as_bitcoind_answers_it(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Each body is answered alike."""
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
        for body in _BODIES:
            assert _answer(node.rpc_port, ours, body) == _answer(
                bitcoind.rpc_port, theirs, body
            ), body
    finally:
        node.stop()
        node.join()
