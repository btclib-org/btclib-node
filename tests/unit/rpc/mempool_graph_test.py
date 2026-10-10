# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`getmempoolancestors`, `getmempooldescendants` and `gettxspendingprevout`.

The refusals and the order of an answer were measured against bitcoind
v31.1.0 on regtest.
"""

import json
import secrets
from types import SimpleNamespace
from typing import Any, cast

import pytest
from bitcoin_core_rpc import RPCErrorCode
from btclib.script.witness import Witness
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from btclib_node.rpc.callbacks import (
    arg_names,
    callbacks,
    get_mempool_ancestors,
    get_mempool_descendants,
    get_mempool_entry,
    get_tx_spending_prevout,
    named_only,
)
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT, answer_help
from btclib_node.rpc.jsonrpc import decode, transform_named_arguments
from tests import anyone_can_spend, anyone_can_spend_script_sig

_CONN = cast("Any", None)
_ZERO = "00" * 32


def a_tx(*spent: tuple[bytes, int], outputs: int = 1) -> Tx:
    """Build a transaction spending each `(txid, vout)` and paying `outputs`.

    With nothing given it spends an outpoint nothing here holds.
    """
    spent = spent or ((secrets.token_bytes(32), 0),)
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(OutPoint(txid, vout), anyone_can_spend_script_sig(), 0xFFFFFFFF)
            for txid, vout in spent
        ],
        vout=[TxOut(1_000, anyone_can_spend()) for _ in range(outputs)],
    )


def a_node(*txs: Tx) -> Any:
    """Build a node double whose mempool holds `txs`, in this order."""
    mempool = Mempool(Logger(debug=True))
    for number, tx in enumerate(txs, start=1):
        assert mempool.add_tx(tx, 1_000 * number, height=7)
    return SimpleNamespace(mempool=mempool)


def a_diamond() -> tuple[Tx, Tx, Tx, Tx, Tx]:
    """Return `root`, `left` and `right` spending it, `tip` spending both.

    The fifth, `other`, is unrelated to the four.
    """
    root = a_tx(outputs=2)
    left = a_tx((root.id, 0))
    right = a_tx((root.id, 1))
    tip = a_tx((left.id, 0), (right.id, 0))
    return root, left, right, tip, a_tx()


def ids(*txs: Tx) -> list[str]:
    """Return the txids of `txs` as Core lists them: by internal bytes."""
    return [tx.id.hex() for tx in sorted(txs, key=lambda tx: tx.id[::-1])]


def plain(entry: dict[str, Any]) -> dict[str, Any]:
    """Return `entry` with each fee as text: `RawJSON` compares by identity."""
    return {**entry, "fees": {k: v.text for k, v in entry["fees"].items()}}


def test_ancestors_are_those_a_tx_spends_from_without_itself() -> None:
    """A tx with two parents has both and the grandparent, not itself."""
    root, left, right, tip, other = a_diamond()
    node = a_node(root, left, right, tip, other)
    assert get_mempool_ancestors(node, _CONN, [tip.id.hex()]) == ids(root, left, right)
    assert get_mempool_ancestors(node, _CONN, [left.id.hex()]) == ids(root)
    assert get_mempool_ancestors(node, _CONN, [root.id.hex()]) == []
    assert get_mempool_ancestors(node, _CONN, [other.id.hex()]) == []


def test_descendants_are_those_spending_from_a_tx_without_itself() -> None:
    """A tx has its children and their child, not itself."""
    root, left, right, tip, other = a_diamond()
    node = a_node(root, left, right, tip, other)
    assert get_mempool_descendants(node, _CONN, [root.id.hex()]) == ids(
        left, right, tip
    )
    assert get_mempool_descendants(node, _CONN, [right.id.hex()]) == ids(tip)
    assert get_mempool_descendants(node, _CONN, [tip.id.hex()]) == []
    assert get_mempool_descendants(node, _CONN, [other.id.hex()]) == []


def test_the_answer_is_in_the_order_of_the_internal_hash() -> None:
    """Core lists a `std::set` ordered by the hash's internal bytes."""
    root = a_tx(outputs=8)
    kids = [a_tx((root.id, vout)) for vout in range(8)]
    node = a_node(root, *kids)
    expected = ids(*kids)
    assert expected != sorted(expected)
    assert get_mempool_descendants(node, _CONN, [root.id.hex()]) == expected
    spent_by = get_mempool_entry(node, _CONN, [root.id.hex()])["spentby"]
    assert [txid.hex() for txid in spent_by] == expected
    sink = a_tx(*((kid.id, 0) for kid in kids))
    node = a_node(root, *kids, sink)
    assert get_mempool_ancestors(node, _CONN, [sink.id.hex()]) == ids(root, *kids)


def test_verbose_answers_each_one_as_getmempoolentry_does() -> None:
    """The object is keyed by txid and each value is that tx's own entry."""
    root, left, right, tip, other = a_diamond()
    node = a_node(root, left, right, tip, other)
    ancestors = cast(
        "dict[str, Any]", get_mempool_ancestors(node, _CONN, [tip.id.hex(), True])
    )
    descendants = cast(
        "dict[str, Any]", get_mempool_descendants(node, _CONN, [root.id.hex(), True])
    )
    assert list(ancestors) == ids(root, left, right)
    assert list(descendants) == ids(left, right, tip)
    for answer in (ancestors, descendants):
        for txid, entry in answer.items():
            assert plain(entry) == plain(get_mempool_entry(node, _CONN, [txid]))
    assert ancestors[left.id.hex()]["spentby"] == [tip.id]
    assert descendants[tip.id.hex()]["ancestorcount"] == 4


@pytest.mark.parametrize("verbose", [False, None])
def test_bare_and_null_verbose_answer_the_txids(*, verbose: bool | None) -> None:
    """`false` and `null` are the default, an array of txids."""
    root, left, *_ = a_diamond()
    node = a_node(root, left)
    assert get_mempool_descendants(node, _CONN, [root.id.hex(), verbose]) == [
        left.id.hex()
    ]


@pytest.mark.parametrize("method", [get_mempool_ancestors, get_mempool_descendants])
def test_a_tx_not_held_is_not_in_mempool(method: Any) -> None:
    """Core's `-5` and its message."""
    with pytest.raises(RpcError) as raised:
        method(a_node(), _CONN, [_ZERO])
    assert raised.value.code == RPCErrorCode.INVALID_ADDRESS_OR_KEY
    assert raised.value.message == "Transaction not in mempool"


@pytest.mark.parametrize(
    ("method", "name"),
    [
        (get_mempool_ancestors, "getmempoolancestors"),
        (get_mempool_descendants, "getmempooldescendants"),
    ],
)
def test_no_txid_is_the_help_text(method: Any, name: str) -> None:
    """Core answers the whole help, `-1`."""
    with pytest.raises(RpcError) as raised:
        method(a_node(), _CONN, [])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == HELP_TEXT[name]


@pytest.mark.parametrize("method", [get_mempool_ancestors, get_mempool_descendants])
def test_wrong_types_are_all_reported_at_once(method: Any) -> None:
    """Both are named, in Core's shape, before the txid is read."""
    with pytest.raises(RpcError) as raised:
        method(a_node(), _CONN, [None, 5])
    assert raised.value.code == RPCErrorCode.TYPE_ERROR
    assert raised.value.message == (
        "Wrong type passed:\n{\n"
        '    "Position 1 (txid)": "JSON value of type null is not of expected type '
        'string",\n'
        '    "Position 2 (verbose)": "JSON value of type number is not of expected '
        'type bool"\n}'
    )


@pytest.mark.parametrize("method", [get_mempool_ancestors, get_mempool_descendants])
def test_a_txid_of_the_wrong_length_is_invalid_parameter(method: Any) -> None:
    """`ParseHashV`'s `-8`, as `getmempoolentry` gives it."""
    with pytest.raises(RpcError) as raised:
        method(a_node(), _CONN, ["aabb"])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == "txid must be of length 64 (not 4, for 'aabb')"


def test_a_prevout_is_answered_by_the_tx_spending_it() -> None:
    """An outpoint nothing spends has no spender, even beside one that has."""
    root, left, right, *_ = a_diamond()
    node = a_node(root, left, right)
    outputs = [
        {"txid": root.id.hex(), "vout": 0},
        {"txid": root.id.hex(), "vout": 1},
        {"txid": left.id.hex(), "vout": 0},
        {"txid": _ZERO, "vout": 5},
    ]
    assert get_tx_spending_prevout(node, _CONN, [outputs]) == [
        {**outputs[0], "spendingtxid": left.id.hex()},
        {**outputs[1], "spendingtxid": right.id.hex()},
        outputs[2],
        outputs[3],
    ]


def test_a_spender_with_two_inputs_answers_for_each() -> None:
    """Each of a tx's inputs names it."""
    root, left, right, tip, _ = a_diamond()
    node = a_node(root, left, right, tip)
    answer = get_tx_spending_prevout(
        node,
        _CONN,
        [[{"txid": left.id.hex(), "vout": 0}, {"txid": right.id.hex(), "vout": 0}]],
    )
    assert [entry["spendingtxid"] for entry in answer] == [tip.id.hex()] * 2


def test_the_input_objects_are_echoed_as_they_were_given() -> None:
    """Core copies the object: its key order and the case of its txid stay."""
    root, left, *_ = a_diamond()
    node = a_node(root, left)
    given = {"vout": 0, "txid": root.id.hex().upper()}
    (entry,) = get_tx_spending_prevout(node, _CONN, [[given]])
    assert list(entry) == ["vout", "txid", "spendingtxid"]
    assert entry["txid"] == root.id.hex().upper()
    assert entry["spendingtxid"] == left.id.hex()


def test_a_key_named_twice_is_echoed_twice() -> None:
    """Core's `UniValue` holds both pairs, and bitcoind wrote both back."""
    root, left, *_ = a_diamond()
    node = a_node(root, left)
    params = decode(f'[[{{"txid":"{root.id.hex()}","vout":0,"vout":7}}]]'.encode())
    (entry,) = get_tx_spending_prevout(node, _CONN, params)
    assert json.dumps(entry) == (
        f'{{"txid": "{root.id.hex()}", "vout": 0, "vout": 7, '
        f'"spendingtxid": "{left.id.hex()}"}}'
    )


def test_return_spending_tx_adds_the_serialized_spender() -> None:
    """Hex with the witness, after `spendingtxid`, only where there is one."""
    root, left, *_ = a_diamond()
    left.vin[0].script_witness = Witness([b"\x01" * 8])
    node = a_node(root, left)
    spent = {"txid": root.id.hex(), "vout": 0}
    unspent = {"txid": root.id.hex(), "vout": 1}
    answer = get_tx_spending_prevout(
        node, _CONN, [[spent, unspent], {"return_spending_tx": True}]
    )
    assert answer == [
        {
            **spent,
            "spendingtxid": left.id.hex(),
            "spendingtx": left.serialize(include_witness=True).hex(),
        },
        unspent,
    ]
    assert list(answer[0]) == ["txid", "vout", "spendingtxid", "spendingtx"]


def test_mempool_only_false_refuses_an_outpoint_the_mempool_does_not_spend() -> None:
    """With no index to ask, Core's `-1` names the first such outpoint."""
    root, left, *_ = a_diamond()
    node = a_node(root, left)
    spent = {"txid": root.id.hex(), "vout": 0}
    unspent = {"txid": root.id.hex(), "vout": 1}
    options = {"mempool_only": False}
    assert get_tx_spending_prevout(node, _CONN, [[spent], options])
    with pytest.raises(RpcError) as raised:
        get_tx_spending_prevout(node, _CONN, [[spent, unspent], options])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == (
        f"No spending tx for the outpoint {root.id.hex()}:1 in mempool, "
        "and txospenderindex is unavailable."
    )


def test_mempool_only_true_and_null_options_leave_the_unspent_unanswered() -> None:
    """`mempool_only` is true by default, and an `options` of null is `{}`."""
    node = a_node()
    unspent = {"txid": _ZERO, "vout": 1}
    for options in (None, {}, {"mempool_only": True}, {"return_spending_tx": False}):
        assert get_tx_spending_prevout(node, _CONN, [[unspent], options]) == [unspent]
    assert get_tx_spending_prevout(node, _CONN, [[unspent]]) == [unspent]


def refusal(params: list[Any]) -> RpcError:
    """Return what `gettxspendingprevout` refuses `params` with."""
    with pytest.raises(RpcError) as raised:
        get_tx_spending_prevout(a_node(), _CONN, params)
    return raised.value


def test_no_argument_is_the_help_text() -> None:
    """Core answers the whole help, `-1`."""
    error = refusal([])
    assert error.code == RPCErrorCode.MISC_ERROR
    assert error.message == HELP_TEXT["gettxspendingprevout"]


def test_empty_outputs_are_refused() -> None:
    """Core's `-8`, after the argument types."""
    error = refusal([[]])
    assert error.code == RPCErrorCode.INVALID_PARAMETER
    assert error.message == "Invalid parameter, outputs are missing"


def test_wrong_argument_types_are_all_reported_at_once() -> None:
    """`outputs` is an array and `options` an object."""
    error = refusal([5, 5])
    assert error.code == RPCErrorCode.TYPE_ERROR
    assert error.message == (
        "Wrong type passed:\n{\n"
        '    "Position 1 (outputs)": "JSON value of type number is not of expected '
        'type array",\n'
        '    "Position 2 (options)": "JSON value of type number is not of expected '
        'type object"\n}'
    )


_OUT = {"txid": _ZERO, "vout": 1}


@pytest.mark.parametrize(
    ("params", "code", "message"),
    [
        (
            [[5]],
            RPCErrorCode.TYPE_ERROR,
            "JSON value of type number is not of expected type object",
        ),
        ([[{"txid": _ZERO}]], RPCErrorCode.TYPE_ERROR, "Missing vout"),
        ([[{"vout": 1}]], RPCErrorCode.TYPE_ERROR, "Missing txid"),
        (
            [[{"txid": None, "vout": 1}]],
            RPCErrorCode.TYPE_ERROR,
            "Missing txid",
        ),
        (
            [[{"txid": 5, "vout": 1}]],
            RPCErrorCode.TYPE_ERROR,
            "JSON value of type number for field txid is not of expected type string",
        ),
        (
            [[{"txid": _ZERO, "vout": "1"}]],
            RPCErrorCode.TYPE_ERROR,
            "JSON value of type string for field vout is not of expected type number",
        ),
        (
            [[{"txid": _ZERO, "vout": True}]],
            RPCErrorCode.TYPE_ERROR,
            "JSON value of type bool for field vout is not of expected type number",
        ),
        (
            [[{**_OUT, "mempool_only": True}]],
            RPCErrorCode.TYPE_ERROR,
            "Unexpected key mempool_only",
        ),
        (
            [[{"txid": "aa", "vout": 1}]],
            RPCErrorCode.INVALID_PARAMETER,
            "txid must be of length 64 (not 2, for 'aa')",
        ),
        (
            [[{"txid": _ZERO, "vout": -1}]],
            RPCErrorCode.INVALID_PARAMETER,
            "Invalid parameter, vout cannot be negative",
        ),
        (
            [[{"txid": _ZERO, "vout": 1.0}]],
            RPCErrorCode.MISC_ERROR,
            "JSON integer out of range",
        ),
        (
            [[{"txid": _ZERO, "vout": 2**31}]],
            RPCErrorCode.MISC_ERROR,
            "JSON integer out of range",
        ),
        (
            [[_OUT], {"mempool_only": 1}],
            RPCErrorCode.TYPE_ERROR,
            (
                "JSON value of type number for field mempool_only is not of "
                "expected type bool"
            ),
        ),
        (
            [[_OUT], {"mempool_only": "x", "return_spending_tx": 5}],
            RPCErrorCode.TYPE_ERROR,
            (
                "JSON value of type string for field mempool_only is not of "
                "expected type bool"
            ),
        ),
        ([[_OUT], {"zzz": 1}], RPCErrorCode.TYPE_ERROR, "Unexpected key zzz"),
        (
            [[_OUT], {"return_spending_tx": None}],
            RPCErrorCode.TYPE_ERROR,
            "JSON value of type null is not of expected type bool",
        ),
        (
            [[_OUT], {"mempool_only": None}],
            RPCErrorCode.TYPE_ERROR,
            "JSON value of type null is not of expected type bool",
        ),
    ],
)
def test_the_refusals_are_cores(
    params: list[Any], code: RPCErrorCode, message: str
) -> None:
    """Each was measured against bitcoind v31.1.0: its code and its message."""
    error = refusal(params)
    assert error.code == code
    assert error.message == message


def test_the_largest_vout_core_reads_is_accepted() -> None:
    """`getInt<int>` reads up to `INT_MAX`, below a `uint32_t`'s own range."""
    out = {"txid": _ZERO, "vout": 2**31 - 1}
    assert get_tx_spending_prevout(a_node(), _CONN, [[out]]) == [out]


def test_the_commands_are_served_named_and_documented() -> None:
    """Each is dispatched, takes its arguments by name, in `Blockchain`."""
    served = {
        "getmempoolancestors": (get_mempool_ancestors, ("txid", "verbose")),
        "getmempooldescendants": (get_mempool_descendants, ("txid", "verbose")),
        "gettxspendingprevout": (
            get_tx_spending_prevout,
            ("outputs", "options|mempool_only|return_spending_tx"),
        ),
    }
    for name, (callback, names) in served.items():
        assert callbacks[name] is callback
        assert arg_names[name] == names
        assert CATEGORY[name] == "Blockchain"
        assert answer_help([name]) == HELP_TEXT[name].rstrip("\n")
        assert name in answer_help([])


def named(**params: Any) -> list[Any]:
    """Map `params` onto `gettxspendingprevout`'s positions."""
    return transform_named_arguments(
        params, arg_names["gettxspendingprevout"], named_only["gettxspendingprevout"]
    )


@pytest.mark.parametrize(
    ("params", "positions"),
    [
        ({"outputs": [1]}, [[1]]),
        ({"outputs": [1], "options": {"x": 1}}, [[1], {"x": 1}]),
        (
            {"outputs": [1], "return_spending_tx": True},
            [[1], {"return_spending_tx": True}],
        ),
        (
            {"return_spending_tx": True, "mempool_only": False, "outputs": [1]},
            [[1], {"mempool_only": False, "return_spending_tx": True}],
        ),
        ({"mempool_only": True}, [None, {"mempool_only": True}]),
        ({"args": [[1]], "mempool_only": True}, [[1], {"mempool_only": True}]),
    ],
)
def test_named_options_are_gathered_into_the_options_object(
    params: dict[str, Any], positions: list[Any]
) -> None:
    """Measured against bitcoind v31.1.0: the options fill the second place."""
    assert named(**params) == positions


def test_options_and_a_named_option_together_are_refused() -> None:
    """Core names the first option gathered."""
    with pytest.raises(RpcError) as raised:
        named(outputs=[1], options={}, mempool_only=True)
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert (
        raised.value.message
        == "Parameter options conflicts with parameter mempool_only"
    )


def test_an_option_and_the_same_position_by_args_is_refused() -> None:
    """The message names `options`, as Core's does, not every name for it."""
    with pytest.raises(RpcError) as raised:
        named(args=[[1], {}], mempool_only=True)
    assert raised.value.message == (
        "Parameter options specified twice both as positional and named argument"
    )


def test_the_named_options_keep_the_order_they_are_declared_in() -> None:
    """`mempool_only` comes first whatever order the request gave them."""
    options = named(return_spending_tx=True, mempool_only=True)[1]
    assert list(options) == ["mempool_only", "return_spending_tx"]
