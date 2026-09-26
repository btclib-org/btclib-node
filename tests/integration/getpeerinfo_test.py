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
    bitcoind: Bitcoind, tmp_path: Path, mined: int = 0
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Dial `bitcoind` over 127.0.0.1 and read each side's entry for the other.

    Read once each side has had its own ping answered. Where `bitcoind`
    mines `mined` blocks first, this node's answer is read once it has
    connected them and its `synced_blocks` has caught up, and bitcoind's
    once a ping this node sends after that is answered: whatever this
    node sent bitcoind about those blocks is ahead of that ping on the
    wire.
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

        def our_peer() -> dict[str, Any]:
            _, body = rpc_client(node).call_raw(
                "getpeerinfo", jsonrpc="1.0", request_timeout=2
            )
            (entry,) = body["result"]
            return cast("dict[str, Any]", entry)

        if mined:
            anyone = bitcoind.rpc("getdescriptorinfo", ["raw(51)"])
            descriptor = cast("dict[str, str]", anyone)["descriptor"]
            bitcoind.rpc("generatetodescriptor", [mined, descriptor])
            wait_until(lambda: our_peer()["synced_blocks"] == mined)
            conn.send_ping()
            wait_until(lambda: not conn.ping_sent)

        (theirs,) = their_peers()
        ours = our_peer()
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
    """Every key Core always pushes is here, and no other.

    A key both answer holds a value of the same type on each side. Over
    127.0.0.1 both sides name the network unroutable, and bitcoind's
    `version` names the unspecified address for this node, 127.0.0.1 not
    being routable, so this node answers no `addrlocal`.
    """
    ours, theirs = _both_answers(bitcoind, tmp_path)

    assert set(theirs) - _OPTIONAL_FIELDS - set(ours) == set()
    assert set(ours) - _OPTIONAL_FIELDS <= set(theirs)
    shared = set(ours) & set(theirs)
    assert {key: type(ours[key]) for key in shared} == {
        key: type(theirs[key]) for key in shared
    }
    assert ours["network"] == theirs["network"] == "not_publicly_routable"
    assert "addrlocal" not in ours
    assert ours["connection_type"] == "manual"
    assert theirs["connection_type"] == "inbound"
    assert ours["bytessent"] > 0
    assert ours["bytesrecv"] > 0
    assert set(ours["bytesrecv_per_msg"]) >= {"version", "verack", "pong"}
    # this node dialled, and its `getaddr` reached bitcoind ahead of the
    # ping both answers wait on: Core's `SetupAddressRelay` on either side
    # (btclib-org/btclib-node#1178)
    assert ours["addr_relay_enabled"] is theirs["addr_relay_enabled"] is True


def test_the_synced_heights_are_what_a_bitcoind_peer_answers(
    bitcoind: Bitcoind, tmp_path: Path
) -> None:
    """After syncing blocks bitcoind mined, each side answers as Core does.

    This node's `synced_headers` and `synced_blocks` for bitcoind are the
    height it synced to; bitcoind's for this node stay -1, this node
    announcing back none of the blocks bitcoind sent it. A second
    bitcoind in this node's place, measured on the same exchange,
    answers the same on both sides (btclib-org/btclib-node#1105,
    btclib-org/btclib-node#1160).
    """
    ours, theirs = _both_answers(bitcoind, tmp_path, mined=3)
    assert (ours["synced_headers"], ours["synced_blocks"]) == (3, 3)
    assert (theirs["synced_headers"], theirs["synced_blocks"]) == (-1, -1)
