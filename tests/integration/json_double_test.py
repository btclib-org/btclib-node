# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A JSON number no double holds, as bitcoind answers it.

`prioritisetransaction`'s `dummy` is the one argument here read with
`UniValue::get_real`. Each body is written as text, since a Python
float cannot spell `1e400`, and sent to this node and to a real bitcoind;
the answers are held equal.
"""

import base64
import json
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import http_request

from btclib_node import Node
from btclib_node.config import Config
from tests import authorization, get_random_port, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

_TXID = "1d1d4e24ed99057e84c3f80fd8fbec79ed9e1acee37da269356ecea000000000"

# either side of the largest double, zero spelled several ways, the
# exponents past what any parser holds, a whole number too large for a
# double, and the constants JSON has not. No underflow (`1e-400`):
# macOS's libc++ build refuses it and the Linux release accepts it,
# which `tests/unit/rpc/jsonrpc_test.py` pins.
_DUMMIES = [
    "1" + "0" * 400,
    "-1" + "0" * 400,
    "1" + "0" * 308,
    "1" + "0" * 309,
    "NaN",
    "Infinity",
    "-Infinity",
    "1e400",
    "-1e400",
    "1.7976931348623157e308",
    "1.7976931348623158e308",
    "1.7976931348623159e308",
    "1.8e308",
    "1e99999999999",
    "0",
    "0.0",
    "-0.0",
    "0e-400",
    "0e400",
    "0e999999",
    "1e-5",
    "0.5",
    "1e0",
    "null",
]


def _answer(port: int, authorization_value: str, body: str) -> object:
    """Return the `result` or the `error` that `body` is answered with."""
    _, reply = http_request(
        f"http://127.0.0.1:{port}",
        data=body.encode(),
        headers={"Authorization": authorization_value},
        timeout=10,
    )
    answer: dict[str, Any] = json.loads(reply)
    return answer["error"] or answer["result"]


def test_a_double_out_of_range_is_refused_as_bitcoind_refuses_it(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Each `dummy` is answered alike, alone and before a bad `fee_delta`."""
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
        theirs = "Basic " + base64.b64encode(bitcoind.cookie_path.read_bytes()).decode()
        for dummy in _DUMMIES:
            for fee_delta in ("0", "1.5"):
                body = (
                    '{"method":"prioritisetransaction","params":'
                    f'["{_TXID}",{dummy},{fee_delta}]}}'
                )
                assert _answer(node.rpc_port, ours, body) == _answer(
                    bitcoind.rpc_port, theirs, body
                ), body
    finally:
        node.stop()
        node.join()
