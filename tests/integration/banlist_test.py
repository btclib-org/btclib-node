# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`setban`, `listbanned` and `clearbanned`, this node against a bitcoind.

The same calls go to both, in the same order, and each has to answer
the same result or the same error. The ban list each is left holding
has to name the same subnets, in the same order, and the `banlist.json`
bitcoind wrote has to load here as the list bitcoind lists.
"""

import logging
import shutil
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import RpcError

from btclib_node import Node
from btclib_node.config import Config
from btclib_node.p2p.banman import BanMan
from tests import get_random_port, rpc_client, wait_until_listening

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.integration.conftest import Bitcoind

# 2100-01-01, an absolute end neither side sees pass
_FAR = 4_102_444_800

_CALLS: list[list[Any]] = [
    ["1.2.3.4", "add"],
    ["1.2.3.4", "add"],
    ["1.2.3.0/24", "add", 100],
    ["1.2.3.5", "add"],
    ["1.2.3.0/255.255.255.0", "add"],
    ["1.2.3.0/25", "add"],
    ["::ffff:5.6.7.8", "add"],
    ["[2001:db9::1]", "add"],
    ["2001:db8::1", "add"],
    ["0.0.0.0", "add"],  # noqa: S104
    ["0.0.0.0/32", "add"],
    ["1.2.3.0/33", "add"],
    ["1:2:3:4:5:6:7:8/ffff::", "add"],
    ["1.2.3.4/255.0.255.0", "add"],
    ["bloop", "add"],
    ["fd87:d87e:eb43::1/16", "add"],
    ["fd87:d87e:eb43::1", "add"],
    ["fd6b:88c0:8724::1", "add"],
    ["fd6b:88c0:8724::/48", "add"],
    ["9.9.9.9", "add", 1, True],
    ["9.9.9.9", "add", _FAR, True],
    ["8.8.8.8", "add", 1.5],
    ["8.8.8.8", "add", -5],
    ["1.2.3.4", "remove", 1.5],
    ["1.2.3.4", "remove"],
    [1, "add"],
    ["8.8.4.4", "add", "1"],
]


def _answer(call: Callable[[str, list[Any]], Any], params: list[Any]) -> Any:
    try:
        return ("result", call("setban", params))
    except RpcError as error:
        # past the url, which names each side's own port
        return ("error", error.code, str(error).split(": ", 1)[1])


def test_setban_answers_as_bitcoind_does(bitcoind: Bitcoind, tmp_path: Path) -> None:
    """Every call answers alike, and the lists left name the same subnets.

    `ban_duration` is compared for the bans not given an absolute end,
    which is where it does not depend on the second each side was asked.
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
        wait_until_listening(node.rpc_manager)
        client = rpc_client(node)

        def ours(method: str, params: list[Any]) -> Any:
            return client.call(method, params)

        def theirs(method: str, params: list[Any]) -> Any:
            return bitcoind.rpc(method, params)

        for params in _CALLS:
            assert _answer(ours, params) == _answer(theirs, params), params

        def listed(call: Callable[[str, list[Any]], Any]) -> list[tuple[str, int]]:
            return [
                (entry["address"], entry["ban_duration"])
                for entry in call("listbanned", [])
                if entry["banned_until"] != _FAR
            ]

        assert listed(ours) == listed(theirs)
        assert [entry["address"] for entry in ours("listbanned", [])] == [
            entry["address"] for entry in theirs("listbanned", [])
        ]
        copy = tmp_path / "banlist.json"
        shutil.copyfile(bitcoind.cookie_path.parent / "banlist.json", copy)
        loaded = BanMan(copy, logging.getLogger(__name__))
        assert [
            (str(subnet), entry.create_time, entry.ban_until)
            for subnet, entry in loaded.banned()
        ] == [
            (entry["address"], entry["ban_created"], entry["banned_until"])
            for entry in theirs("listbanned", [])
        ]
        assert ours("clearbanned", []) is theirs("clearbanned", []) is None
        assert ours("listbanned", []) == theirs("listbanned", []) == []
    finally:
        node.stop()
        node.join()
