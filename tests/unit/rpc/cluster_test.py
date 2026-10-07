# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`getmempoolcluster`, `getmempoolfeeratediagram` and the chunk fields.

`tests/integration/cluster_test.py` holds the same answers to bitcoind
v31.1.0's.
"""

from typing import Any

import pytest
from bitcoin_core_rpc import RPCErrorCode

from btclib_node.rpc.callbacks import (
    arg_names,
    callbacks,
    get_mempool_cluster,
    get_mempool_entry,
    get_mempool_feerate_diagram,
)
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT, answer_help
from tests.unit.rpc.mempool_graph_test import _CONN, _ZERO, a_node, a_tx


def text(answer: Any) -> Any:
    """Return `answer` with each `RawJSON` amount as its text."""
    if isinstance(answer, dict):
        return {key: text(value) for key, value in answer.items()}
    if isinstance(answer, list):
        return [text(value) for value in answer]
    return getattr(answer, "text", answer)


def test_a_child_paying_for_its_parent_is_one_chunk() -> None:
    """The parent pays 1000 and its child 2000: one chunk, parent first."""
    parent = a_tx()
    child = a_tx((parent.id, 0))
    node = a_node(parent, child, a_tx())
    weight = parent.weight
    assert child.weight == weight
    answer = {
        "clusterweight": 2 * weight,
        "txcount": 2,
        "chunks": [
            {
                "chunkfee": "0.00003000",
                "chunkweight": 2 * weight,
                "txs": [parent.id.hex(), child.id.hex()],
            }
        ],
    }
    for tx in (parent, child):
        assert text(get_mempool_cluster(node, _CONN, [tx.id.hex()])) == answer


def test_a_parent_paying_more_than_its_child_is_a_chunk_of_its_own() -> None:
    """The fee delta makes the parent pay 5000, so each is its own chunk."""
    parent = a_tx()
    child = a_tx((parent.id, 0))
    node = a_node(parent, child)
    node.mempool.prioritise(parent.id, 4_000)
    chunks = text(get_mempool_cluster(node, _CONN, [child.id.hex()]))["chunks"]
    assert [(c["chunkfee"], c["txs"]) for c in chunks] == [
        ("0.00005000", [parent.id.hex()]),
        ("0.00002000", [child.id.hex()]),
    ]
    entry = text(get_mempool_entry(node, _CONN, [child.id.hex()]))
    assert entry["chunkweight"] == child.weight
    assert entry["fees"]["chunk"] == "0.00002000"
    keys = list(entry)
    assert keys.index("chunkweight") == keys.index("wtxid") + 1


def test_the_diagram_adds_up_the_chunks_in_mining_order() -> None:
    """From zero, the 3000 single goes before the 3000 chunk of two."""
    parent = a_tx()
    child = a_tx((parent.id, 0))
    node = a_node(parent, child, a_tx())
    weight = parent.weight
    assert text(get_mempool_feerate_diagram(node, _CONN, [])) == [
        {"weight": 0, "fee": "0.00000000"},
        {"weight": weight, "fee": "0.00003000"},
        {"weight": 3 * weight, "fee": "0.00006000"},
    ]
    assert text(get_mempool_feerate_diagram(a_node(), _CONN, [])) == [
        {"weight": 0, "fee": "0.00000000"}
    ]


def test_a_tx_not_held_is_not_in_mempool() -> None:
    """Core's `-5` and its message."""
    with pytest.raises(RpcError) as raised:
        get_mempool_cluster(a_node(), _CONN, [_ZERO])
    assert raised.value.code == RPCErrorCode.INVALID_ADDRESS_OR_KEY
    assert raised.value.message == "Transaction not in mempool"


@pytest.mark.parametrize(
    ("params", "code", "message"),
    [
        ([], RPCErrorCode.MISC_ERROR, HELP_TEXT["getmempoolcluster"]),
        (
            [12],
            RPCErrorCode.TYPE_ERROR,
            (
                'Wrong type passed:\n{\n    "Position 1 (txid)": "JSON value of '
                'type number is not of expected type string"\n}'
            ),
        ),
        (
            ["aa"],
            RPCErrorCode.INVALID_PARAMETER,
            "txid must be of length 64 (not 2, for 'aa')",
        ),
    ],
)
def test_the_refusals_are_cores(params: list[Any], code: int, message: str) -> None:
    """No txid, a txid of the wrong type, and one of the wrong length."""
    with pytest.raises(RpcError) as raised:
        get_mempool_cluster(a_node(), _CONN, params)
    assert raised.value.code == code
    assert raised.value.message == message


def test_the_commands_are_served_named_and_documented() -> None:
    """`getmempoolcluster` is in `Blockchain`, the diagram is hidden."""
    assert callbacks["getmempoolcluster"] is get_mempool_cluster
    assert callbacks["getmempoolfeeratediagram"] is get_mempool_feerate_diagram
    assert arg_names["getmempoolcluster"] == ("txid",)
    assert arg_names["getmempoolfeeratediagram"] == ()
    assert CATEGORY["getmempoolcluster"] == "Blockchain"
    assert CATEGORY["getmempoolfeeratediagram"] == "hidden"
    listing = answer_help([])
    assert 'getmempoolcluster "txid"' in listing
    assert "getmempoolfeeratediagram" not in listing
    for name in ("getmempoolcluster", "getmempoolfeeratediagram"):
        assert answer_help([name]) == HELP_TEXT[name].rstrip("\n")
