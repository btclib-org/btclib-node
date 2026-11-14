# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A JSON integer past CPython's digit limit, as bitcoind answers it.

ISS 2456. Each body is written as text, since `json.dumps` cannot write
such an integer, and sent to this node and to a real bitcoind; the
status and the body of the two replies are held equal.
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

# a key no method reads, a height (`getInt`), a threshold (`get_real`),
# an amount, and an id
_BODIES = [
    f'{{"method":"getblockcount","id":1,"x":{_DIGITS}}}',
    f'{{"method":"getblockcount","id":1,"x":-{_DIGITS}}}',
    f'{{"method":"getblockcount","id":1,"x":[{{"y":[{_DIGITS}]}}]}}',
    f'{{"method":"getblockhash","params":[{_DIGITS}],"id":1}}',
    f'{{"method":"getblockhash","params":[-{_DIGITS}],"id":1}}',
    f'{{"method":"getblockhash","params":{{"height":{_DIGITS}}},"id":1}}',
    f'{{"jsonrpc":"2.0","method":"getblockhash","params":[{_DIGITS}],"id":1}}',
    f'{{"method":"estimaterawfee","params":[1,{_DIGITS}],"id":1}}',
    f'{{"method":"estimaterawfee","params":[1,-{_DIGITS}],"id":1}}',
    f'{{"method":"sendrawtransaction","params":["{_TX}",{_DIGITS}],"id":1}}',
    f'{{"method":"getblockcount","id":{_DIGITS}}}',
    f'{{"method":"getblockcount","id":-{_DIGITS}}}',
    f'{{"method":"getblockcount","id":[1,{{"a":{_DIGITS},"a":-{_DIGITS}}}]}}',
    (
        f'[{{"method":"getblockcount","id":1}},'
        f'{{"method":"getblockhash","params":[{_DIGITS}],"id":{_DIGITS}}}]'
    ),
]


def _answer(port: int, authorization_value: str, body: str) -> tuple[int, bytes]:
    """Return the status and the body `body` is answered with."""
    return http_request(
        f"http://127.0.0.1:{port}",
        data=body.encode(),
        headers={"Authorization": authorization_value},
        timeout=10,
    )


def test_a_long_integer_is_answered_as_bitcoind_answers_it(
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
