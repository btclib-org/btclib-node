# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`getpeerinfo`, this node's answer against a real bitcoind's.

Each side is asked about the other end of one connection, so the two
answers describe the same socket pair and can be held to the same shape:
which fields are present, and whether each time field is an integer or
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

# What Core pushes only where it holds a value, `startingheight` only
# under `-deprecatedrpc`
_OPTIONAL_FIELDS = {
    "addrbind",
    "addrlocal",
    "mapped_as",
    "pingtime",
    "minping",
    "pingwait",
    "startingheight",
}


def _both_answers(
    bitcoind: Bitcoind, tmp_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Dial `bitcoind` over 127.0.0.1 and read each side's entry for the other.

    Read once each side has had its own ping answered, and before either
    sends the other a block or a transaction.
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
    return ours, theirs


def test_the_time_fields_have_bitcoind_s_shape(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Both sides answer the same time fields, each of the same type.

    `last_block` and `last_transaction` are `0` on both, nothing having
    been relayed yet.
    """
    ours, theirs = _both_answers(bitcoind, tmp_path)

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


def test_the_fields_are_bitcoind_s_and_so_is_a_loopback_peer_s_network(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """Every key Core always pushes is here but three, and no other.

    `synced_headers`, `synced_blocks` and `addr_processed` are what this
    node keeps no state to answer with. Over 127.0.0.1 both sides name
    the network unroutable, and bitcoind's `version` names the
    unspecified address for this node, 127.0.0.1 not being routable, so
    this node answers no `addrlocal`.
    """
    ours, theirs = _both_answers(bitcoind, tmp_path)

    assert set(theirs) - _OPTIONAL_FIELDS - set(ours) == {
        "synced_headers",
        "synced_blocks",
        "addr_processed",
    }
    assert set(ours) - _OPTIONAL_FIELDS <= set(theirs)
    assert ours["network"] == theirs["network"] == "not_publicly_routable"
    assert "addrlocal" not in ours
    assert ours["connection_type"] == "manual"
    assert theirs["connection_type"] == "inbound"
    assert ours["bytessent"] > 0
    assert ours["bytesrecv"] > 0
    assert set(ours["bytesrecv_per_msg"]) >= {"version", "verack", "pong"}
