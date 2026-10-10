# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`prioritisetransaction` and `getprioritisedtransactions`.

The refusals, their order and their words were measured against bitcoind
v31.1.0 on regtest.
"""

import secrets
from types import SimpleNamespace
from typing import Any, cast

import pytest
from bitcoin_core_rpc import RPCErrorCode
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node.config import DEFAULT_DUST_RELAY_FEERATE
from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from btclib_node.rpc.callbacks import (
    arg_names,
    callbacks,
    get_mempool_entry,
    get_prioritised_transactions,
    prioritise_transaction,
)
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT
from btclib_node.rpc.jsonrpc import decode, transform_named_arguments
from tests import (
    anyone_can_spend,
    anyone_can_spend_script_sig,
    txids_the_two_orders_disagree_on,
)

_CONN = cast("Any", None)
_TXID = "1d1d4e24ed99057e84c3f80fd8fbec79ed9e1acee37da269356ecea000000000"


def a_tx(*, dust: bool = False) -> Tx:
    """Return a transaction spending an outpoint nothing holds."""
    return Tx(
        version=1,
        lock_time=0,
        vin=[
            TxIn(
                OutPoint(secrets.token_bytes(32), 0),
                anyone_can_spend_script_sig(),
                0xFFFFFFFF,
            )
        ],
        vout=[TxOut(0 if dust else 1_000, anyone_can_spend())],
    )


def a_node(*, require_standard: bool = True) -> Any:
    """Build a node double with an empty mempool."""
    return SimpleNamespace(
        mempool=Mempool(Logger(debug=True)),
        config=SimpleNamespace(
            require_standard=require_standard,
            dust_relay_feerate=DEFAULT_DUST_RELAY_FEERATE,
        ),
    )


def refused(params: list[Any]) -> RpcError:
    """Return what `prioritisetransaction` refuses `params` with."""
    with pytest.raises(RpcError) as raised:
        prioritise_transaction(a_node(), _CONN, params)
    return raised.value


def test_both_are_served_under_the_names_and_arguments_core_gives() -> None:
    """`dummy` is the second position, and both are in the Mining category."""
    assert callbacks["prioritisetransaction"] is prioritise_transaction
    assert callbacks["getprioritisedtransactions"] is get_prioritised_transactions
    assert arg_names["prioritisetransaction"] == ("txid", "dummy", "fee_delta")
    assert arg_names["getprioritisedtransactions"] == ()
    assert CATEGORY["prioritisetransaction"] == "Mining"
    assert CATEGORY["getprioritisedtransactions"] == "Mining"
    assert HELP_TEXT["prioritisetransaction"].startswith(
        'prioritisetransaction "txid" ( dummy ) fee_delta\n'
    )


def test_a_delta_is_added_and_the_answer_is_true() -> None:
    """Deltas stack, as `bitcoind` v31.1 answers `-5` after `5` and `-1`."""
    node = a_node()
    assert prioritise_transaction(node, _CONN, [_TXID, None, 5]) is True
    assert prioritise_transaction(node, _CONN, [_TXID, 0, -10]) is True
    assert prioritise_transaction(node, _CONN, [_TXID, 0.0, 1]) is True
    assert get_prioritised_transactions(node, _CONN, []) == {
        _TXID: {"fee_delta": -4, "in_mempool": False}
    }


def test_a_delta_that_comes_back_to_zero_is_not_listed() -> None:
    """The entry is dropped."""
    node = a_node()
    prioritise_transaction(node, _CONN, [_TXID, None, 5])
    prioritise_transaction(node, _CONN, [_TXID, None, -5])
    assert get_prioritised_transactions(node, _CONN, []) == {}
    prioritise_transaction(node, _CONN, [_TXID, None, 0])
    assert get_prioritised_transactions(node, _CONN, []) == {}


def test_a_held_transaction_is_listed_with_its_modified_fee() -> None:
    """`modified_fee` is in satoshi, and only for one in the mempool."""
    node = a_node()
    held, other = a_tx(), secrets.token_bytes(32)
    assert node.mempool.add_tx(held, 1_000)
    prioritise_transaction(node, _CONN, [held.id.hex(), None, 250])
    prioritise_transaction(node, _CONN, [other.hex(), None, -7])
    assert get_prioritised_transactions(node, _CONN, []) == {
        held.id.hex(): {"fee_delta": 250, "in_mempool": True, "modified_fee": 1_250},
        other.hex(): {"fee_delta": -7, "in_mempool": False},
    }


def test_the_list_is_in_the_order_of_the_internal_txid_bytes() -> None:
    """Core's `std::map<Txid, CAmount>` is not in the displayed order."""
    node = a_node()
    txids = txids_the_two_orders_disagree_on()
    for txid in txids:
        prioritise_transaction(node, _CONN, [txid.hex(), None, 1])
    listed = list(get_prioritised_transactions(node, _CONN, []))
    assert listed == [txid.hex() for txid in sorted(txids, key=lambda txid: txid[::-1])]
    assert listed != [txid.hex() for txid in sorted(txids)]


def test_the_fees_of_a_held_transaction_carry_the_delta() -> None:
    """`getmempoolentry` is where a caller reads it."""
    node = a_node()
    tx = a_tx()
    node.mempool.add_tx(tx, 1_000)
    prioritise_transaction(node, _CONN, [tx.id.hex(), None, 2_500])
    fees = get_mempool_entry(node, _CONN, [tx.id.hex()])["fees"]
    assert {key: value.text for key, value in fees.items()} == {
        "base": "0.00001000",
        "modified": "0.00003500",
        "ancestor": "0.00003500",
        "descendant": "0.00003500",
        "chunk": "0.00003500",
    }


@pytest.mark.parametrize("params", [[], [_TXID], [_TXID, 0], [_TXID, 0, 5, 7]])
def test_a_wrong_number_of_arguments_is_the_help_text(params: list[Any]) -> None:
    """Core answers its whole help, `-1`, for each."""
    error = refused(params)
    assert error.code == RPCErrorCode.MISC_ERROR
    assert error.message == HELP_TEXT["prioritisetransaction"]


def test_named_arguments_leave_the_dummy_out() -> None:
    """`fee_delta` named alone reaches the handler as a null `dummy`."""
    node = a_node()
    params = transform_named_arguments(
        {"txid": _TXID, "fee_delta": 5}, arg_names["prioritisetransaction"]
    )
    assert params == [_TXID, None, 5]
    assert prioritise_transaction(node, _CONN, params) is True


def test_every_wrong_type_is_named_at_once() -> None:
    """In Core's shape, ahead of anything the values say."""
    error = refused(["foo", "x", "y"])
    assert error.code == RPCErrorCode.TYPE_ERROR
    assert error.message == (
        "Wrong type passed:\n{\n"
        '    "Position 2 (dummy)": "JSON value of type string is not of expected '
        'type number",\n'
        '    "Position 3 (fee_delta)": "JSON value of type string is not of '
        'expected type number"\n}'
    )


@pytest.mark.parametrize(
    ("value", "name"),
    [(None, "null"), (True, "bool"), ("5", "string"), ([5], "array")],
)
def test_a_fee_delta_that_is_no_number_is_a_type_error(value: Any, name: str) -> None:
    """It is required, so a null is a type error too, unlike `dummy`."""
    error = refused([_TXID, None, value])
    assert error.code == RPCErrorCode.TYPE_ERROR
    assert error.message == (
        'Wrong type passed:\n{\n    "Position 3 (fee_delta)": "JSON value of type '
        f'{name} is not of expected type number"\n}}'
    )


@pytest.mark.parametrize(("value", "name"), [(12, "number"), (None, "null")])
def test_a_txid_that_is_no_string_is_a_type_error(value: Any, name: str) -> None:
    """Position 1 is named."""
    error = refused([value, None, 5])
    assert error.code == RPCErrorCode.TYPE_ERROR
    assert error.message == (
        'Wrong type passed:\n{\n    "Position 1 (txid)": "JSON value of type '
        f'{name} is not of expected type string"\n}}'
    )


@pytest.mark.parametrize(
    ("txid", "message"),
    [
        ("foo", "txid must be of length 64 (not 3, for 'foo')"),
        ("Z" + _TXID[1:], f"txid must be hexadecimal string (not 'Z{_TXID[1:]}')"),
    ],
)
def test_a_bad_txid_is_invalid_parameter(txid: str, message: str) -> None:
    """`ParseHashV`'s two words."""
    error = refused([txid, None, 5])
    assert error.code == RPCErrorCode.INVALID_PARAMETER
    assert error.message == message


@pytest.mark.parametrize("fee_delta", [1.5, 5.0, 1e3, 2**63, -(2**63) - 1])
def test_a_fee_delta_that_is_no_int64_is_out_of_range(fee_delta: float) -> None:
    """Core's `getInt<int64_t>`: `-1` and its message, a real or too large."""
    error = refused([_TXID, None, fee_delta])
    assert error.code == RPCErrorCode.MISC_ERROR
    assert error.message == "JSON integer out of range"


@pytest.mark.parametrize("fee_delta", [2**63 - 1, -(2**63)])
def test_a_fee_delta_at_the_int64_bounds_is_taken(fee_delta: int) -> None:
    """Both ends are in range."""
    node = a_node()
    assert prioritise_transaction(node, _CONN, [_TXID, None, fee_delta]) is True
    assert node.mempool.delta(bytes.fromhex(_TXID)) == fee_delta


@pytest.mark.parametrize("dummy", [1, -1, 0.5])
def test_a_dummy_that_is_not_zero_is_refused(dummy: float) -> None:
    """Core: priority is gone, `-8` and its message."""
    error = refused([_TXID, dummy, 5])
    assert error.code == RPCErrorCode.INVALID_PARAMETER
    assert error.message == (
        "Priority is no longer supported, dummy argument to prioritisetransaction "
        "must be 0."
    )


@pytest.mark.parametrize("dummy", ["1e400", "-1e400", "1" + "0" * 400])
def test_a_dummy_no_double_holds_is_out_of_range(dummy: str) -> None:
    """Core's `get_real`: `-1` and its message, before `fee_delta` is read."""
    params = decode(f'["{_TXID}", {dummy}, 1.5]'.encode())
    error = refused(params)
    assert error.code == RPCErrorCode.MISC_ERROR
    assert error.message == "JSON double out of range"


@pytest.mark.parametrize(
    "dummy", ["0e-400", "1e-400", "-1e-400", "0.0", "-0e999", "0e400", "0"]
)
def test_a_zero_dummy_is_taken_however_it_is_written(dummy: str) -> None:
    """Zero is taken, and so is an underflow, which reads as zero."""
    node = a_node()
    params = decode(f'["{_TXID}", {dummy}, 5]'.encode())
    assert prioritise_transaction(node, _CONN, params) is True


def test_the_arguments_are_read_in_core_s_order() -> None:
    """A bad txid is named before the fee delta, which is before the dummy."""
    assert refused(["foo", 1, 1.5]).message.startswith("txid must be of length 64")
    assert refused([_TXID, 1, 1.5]).message == "JSON integer out of range"
    assert refused([_TXID, 1, 5]).message.startswith("Priority is no longer")


def test_a_refused_call_leaves_no_delta() -> None:
    """Nothing is kept for a call that raised."""
    node = a_node()
    for params in ([_TXID, 1, 5], [_TXID, None, 1.5], ["foo", None, 5]):
        with pytest.raises(RpcError):
            prioritise_transaction(node, _CONN, params)
    assert node.mempool.deltas == {}


def test_a_held_transaction_with_a_dust_output_is_refused() -> None:
    """Core: it would not be accepted with a fee, so it is not prioritised."""
    node = a_node()
    dusty = a_tx(dust=True)
    assert node.mempool.add_tx(dusty, 0)
    with pytest.raises(RpcError) as raised:
        prioritise_transaction(node, _CONN, [dusty.id.hex(), None, 5])
    assert raised.value.code == RPCErrorCode.INVALID_PARAMETER
    assert raised.value.message == (
        "Priority is not supported for transactions with dust outputs."
    )
    assert node.mempool.deltas == {}


def test_a_dust_transaction_not_held_is_prioritised() -> None:
    """Core looks in the mempool alone."""
    node = a_node()
    assert prioritise_transaction(node, _CONN, [a_tx(dust=True).id.hex(), None, 5])


def test_a_held_dust_transaction_is_prioritised_where_nonstandard_is_accepted() -> None:
    """The refusal needs `-acceptnonstdtxn` off."""
    node = a_node(require_standard=False)
    dusty = a_tx(dust=True)
    assert node.mempool.add_tx(dusty, 0)
    assert prioritise_transaction(node, _CONN, [dusty.id.hex(), None, 5])
    assert node.mempool.delta(dusty.id) == 5
