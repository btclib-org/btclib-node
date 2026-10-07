# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`getorphantxs`, in Core's three verbosities and its refusals."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from bitcoin_core_rpc import RPCErrorCode

from btclib_node.orphanage import TxOrphanage
from btclib_node.rpc.callbacks import arg_names, callbacks, get_orphan_txs
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT, answer_help
from tests.unit.orphanage_test import an_orphan

_CONN = cast("Any", None)


def a_node(*orphans: tuple[Any, list[int]]) -> Any:
    """Build a node double whose orphanage holds each tx for its announcers."""
    orphanage = TxOrphanage()
    for tx, peers in orphans:
        for peer in peers:
            orphanage.add_tx(tx, peer)
    return SimpleNamespace(download_manager=SimpleNamespace(orphanage=orphanage))


def test_an_empty_orphanage_answers_an_empty_list_at_every_level() -> None:
    """Core's answer, with no argument, null or a level."""
    node = a_node()
    for params in ([], [None], [0], [1], [2]):
        assert get_orphan_txs(node, _CONN, params) == []


def test_verbosity_0_answers_the_txids_in_the_order_of_the_wtxids() -> None:
    """The order is Core's, which is of the reversed display bytes."""
    orphans = [an_orphan() for _ in range(6)]
    node = a_node(*((tx, [1]) for tx in orphans))
    expected = [tx.id.hex() for tx in sorted(orphans, key=lambda tx: tx.hash[::-1])]
    assert get_orphan_txs(node, _CONN, []) == expected
    assert get_orphan_txs(node, _CONN, [0]) == expected


def test_verbosity_1_answers_an_object_each_and_2_adds_the_hex() -> None:
    """The fields Core names, with the peers that announced it."""
    tx = an_orphan()
    node = a_node((tx, [4, 2]))
    entry = {
        "txid": tx.id.hex(),
        "wtxid": tx.hash.hex(),
        "bytes": tx.size,
        "vsize": tx.vsize,
        "weight": tx.weight,
        "from": [2, 4],
    }
    assert get_orphan_txs(node, _CONN, [1]) == [entry]
    assert get_orphan_txs(node, _CONN, [2]) == [
        {**entry, "hex": tx.serialize(include_witness=True).hex()}
    ]


@pytest.mark.parametrize("verbosity", [3, -1])
def test_another_number_is_an_invalid_parameter(verbosity: int) -> None:
    """Measured against bitcoind v31.1.0: -8 and the number named."""
    with pytest.raises(RpcError) as raised:
        get_orphan_txs(a_node(), _CONN, [verbosity])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == f"Invalid verbosity value {verbosity}"


@pytest.mark.parametrize("verbosity", [True, False])
def test_a_bool_is_refused_where_getrawmempool_takes_it(*, verbosity: bool) -> None:
    """Measured against bitcoind v31.1.0: -3 and Core's message."""
    with pytest.raises(RpcError) as raised:
        get_orphan_txs(a_node(), _CONN, [verbosity])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == "Verbosity was boolean but only integer allowed"


def test_the_command_is_served_named_and_documented() -> None:
    """It is dispatched, takes `verbosity` by name, and is hidden."""
    assert callbacks["getorphantxs"] is get_orphan_txs
    assert arg_names["getorphantxs"] == ("verbosity",)
    assert CATEGORY["getorphantxs"] == "hidden"
    assert answer_help(["getorphantxs"]) == HELP_TEXT["getorphantxs"].rstrip("\n")
    assert "getorphantxs" not in answer_help([])
