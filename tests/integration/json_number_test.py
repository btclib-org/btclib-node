# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A number in a request's `id`, written back as bitcoind writes it.

ISS 2469. Each body is written as text, since `json.dumps` writes a
number as Python reads it, and sent to this node and to a real
bitcoind; the status and the body of the two replies are held equal.
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

_DIGITS = "9" * 5000

# the issue's numbers, an exponent's sign, nested ids beside a
# long integer, both envelopes, a batch, and `-0` and `0.0` read as a
# height
_BODIES = [
    '{"method":"getblockcount","id":1.50}',
    '{"method":"getblockcount","id":1e2}',
    '{"method":"getblockcount","id":-0.0}',
    '{"method":"getblockcount","id":-0}',
    '{"method":"getblockcount","id":1E400}',
    '{"method":"getblockcount","id":1.0E+2}',
    '{"method":"getblockcount","id":[1.50,{"a":-0,"a":1e2},[[1E-400]]]}',
    f'{{"method":"getblockcount","id":[{_DIGITS},-0,1.50]}}',
    '{"jsonrpc":"2.0","method":"getblockcount","id":1e-2}',
    '[{"method":"getblockcount","id":-0},{"method":"nosuch","id":0.10}]',
    '{"method":"getblockhash","params":[-0],"id":-0}',
    '{"method":"getblockhash","params":[0.0],"id":1.50}',
]


def _answer(port: int, authorization_value: str, body: str) -> tuple[int, bytes]:
    """Return the status and the body `body` is answered with."""
    return http_request(
        f"http://127.0.0.1:{port}",
        data=body.encode(),
        headers={"Authorization": authorization_value},
        timeout=10,
    )


def test_a_number_in_an_id_is_written_back_as_bitcoind_writes_it(
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
            ), body[:80]
    finally:
        node.stop()
        node.join()
