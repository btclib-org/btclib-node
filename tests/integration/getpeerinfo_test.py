# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`getpeerinfo`'s time fields, this node's against a real bitcoind's.

Each side is asked about the other end of one connection, so the two
answers describe the same socket pair and can be held to the same shape:
which of the time fields are present, and whether each is an integer or
a fraction. The client reads a JSON fraction as a `Decimal`, the same
for either answer.
"""

from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.address import peer_address
from tests import get_random_port, rpc_client, wait_until, wait_until_listening

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

_TIME_FIELDS = (
    "lastsend",
    "lastrecv",
    "last_transaction",
    "last_block",
    "conntime",
    "pingtime",
    "minping",
    "pingwait",
)


def test_the_time_fields_have_bitcoind_s_shape(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Both sides answer the same time fields, each of the same type.

    Read once each side has had its own ping answered, and before either
    sends the other a block or a transaction, so `last_block` and
    `last_transaction` are `0` on both.
    """
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
        wait_until_listening(node.p2p_manager)
        wait_until_listening(node.rpc_manager)
        node.p2p_manager.connect(peer_address("127.0.0.1", bitcoind.p2p_port, 0, 0))

        wait_until(lambda: len(node.p2p_manager.connections))
        (conn,) = node.p2p_manager.connections.values()
        wait_until(lambda: conn.status == P2pConnStatus.Connected)
        wait_until(lambda: conn.latency > 0)

        def their_peers() -> list[dict[str, Any]]:
            return cast("list[dict[str, Any]]", bitcoind.rpc("getpeerinfo"))

        wait_until(lambda: any("pingtime" in peer for peer in their_peers()))

        (theirs,) = their_peers()
        _, body = rpc_client(node).call_raw(
            "getpeerinfo", jsonrpc="1.0", request_timeout=2
        )
        (ours,) = body["result"]
    finally:
        node.stop()
        node.join()

    def shape(info: dict[str, Any]) -> dict[str, type]:
        return {key: type(info[key]) for key in _TIME_FIELDS if key in info}

    assert shape(ours) == shape(theirs)
    assert shape(ours) == {
        "lastsend": int,
        "lastrecv": int,
        "last_transaction": int,
        "last_block": int,
        "conntime": int,
        "pingtime": Decimal,
        "minping": Decimal,
    }
    assert ours["last_block"] == theirs["last_block"] == 0
    assert ours["last_transaction"] == theirs["last_transaction"] == 0
    assert abs(ours["conntime"] - theirs["conntime"]) <= 2
