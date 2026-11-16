# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""An amount given as a JSON number, parsed from its text as bitcoind parses it.

ISS 2494. Each body is written as text, since `json.dumps` writes a
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

# a transaction `sendrawtransaction` decodes, so that `maxfeerate` is read:
# version 2, one input spending output 0, one output of 0 paying to `OP_TRUE`
_TX = "".join(
    (
        "02000000",
        "01",
        "11" * 32,
        "00000000",
        "00",
        "ffffffff",
        "01",
        "00" * 8,
        "0151",
        "00000000",
    )
)

# leading zeros past `int`'s digit limit, in an amount of 1 BTC
_ONE = f"0.{'0' * 5000}1e5001"

# digits a double rounds away, as `maxfeerate` and as `maxburnamount`; then
# the controls: the same digits as a string, and amounts both read
_BODIES = [
    '{"method":"testmempoolaccept","params":[["00"],1.00000000000000000001],"id":1}',
    '{"method":"testmempoolaccept","params":[["00"],0.10000000000000000001],"id":1}',
    (
        '{"jsonrpc":"2.0","method":"testmempoolaccept",'
        '"params":{"rawtxs":["00"],"maxfeerate":1.00000000000000000001},"id":1}'
    ),
    f'{{"method":"sendrawtransaction","params":["{_TX}",1.00000000000000000001],"id":1}}',
    (
        f'{{"method":"sendrawtransaction",'
        f'"params":["{_TX}",0.1,0.10000000000000000001],"id":1}}'
    ),
    '{"method":"testmempoolaccept","params":[["00"],"1.00000000000000000001"],"id":1}',
    '{"method":"testmempoolaccept","params":[["00"],1.50],"id":1}',
    f'{{"method":"testmempoolaccept","params":[["00"],{_ONE}],"id":1}}',
]


def _answer(port: int, authorization_value: str, body: str) -> tuple[int, bytes]:
    """Return the status and the body `body` is answered with."""
    return http_request(
        f"http://127.0.0.1:{port}",
        data=body.encode(),
        headers={"Authorization": authorization_value},
        timeout=10,
    )


def test_an_amount_number_is_parsed_from_its_text_as_bitcoind_parses_it(
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
