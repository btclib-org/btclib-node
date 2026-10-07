# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`submitpackage`: a child and its parents, or one transaction, to the mempool.

Core's `submitpackage` (`src/rpc/mempool.cpp`) over `AcceptPackage`
(`src/validation.cpp`), at bitcoin/bitcoin@9be056a8a7, the v31.1 tag. The
handler has `rpc.callbacks`' signature and runs on `Node`'s thread, so it
checks scripts and changes the mempool without a lock (`ARCHITECTURE.md`).
`rpc.callbacks` imports this module, so its helpers are imported inside the
functions that use them.

Package replacement is not served, whatever Core does with the same call
(btclib-org/btclib-node#1334): a transaction that conflicts with a held one
is refused as `sendrawtransaction` refuses it, where Core replaces it or
refuses the package with a "package RBF failed" message. The same holds for
the TRUC sibling eviction, which is a replacement.
`replaced-transactions` is therefore always empty, Core's form for a
package that replaces nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from bitcoin_core_rpc import RPCErrorCode
from btclib.exceptions import BTClibException

from btclib_node.exceptions import (
    MissingPrevoutError,
    PackageRefusedError,
    TxRejectedError,
)
from btclib_node.interpreter import check_package, check_transaction
from btclib_node.main import (
    package_refusal,
    pre_verify_mempool_acceptance,
    pre_verify_subpackage,
)
from btclib_node.rpc.errors import RpcError, json_type_name, type_error
from btclib_node.rpc.help import HELP_TEXT

if TYPE_CHECKING:
    from btclib.tx import Tx

    from btclib_node import Node
    from btclib_node.mempool import Mempool
    from btclib_node.rpc.connection import RpcConnection

__all__ = ["submit_package"]

_TOPOLOGY_REFUSED = (
    "package topology disallowed. not child-with-parents or parents depend on "
    "each other."
)


class _Call(NamedTuple):
    """The arguments of a `submitpackage` that are read.

    `invalid` is each transaction's `CheckTransaction` refusal, keyed by
    wtxid, for the ones it refuses; `max_feerate` is in satoshi per kvB.
    """

    txs: list[Tx]
    max_feerate: int
    invalid: dict[bytes, str]


class _Outcome(NamedTuple):
    """What `submitpackage` answers of one transaction.

    Either `error`, or what the mempool holds of it: its `vsize` and the
    `base_fee` it pays, and `other_wtxid` where it holds another witness of
    it instead. `effective` is the modified fee and the vsize its effective
    feerate is of, with the wtxids they are the sums over, where the
    transaction was taken now and not before.
    """

    error: Exception | None = None
    other_wtxid: bytes | None = None
    vsize: int = 0
    base_fee: int = 0
    effective: tuple[int, int, list[bytes]] | None = None


def submit_package(
    node: Node, _conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `submitpackage`: take what passes, and say what each came to.

    A package refused in whole or in part is a result that says why, not an
    error: `_read` has the errors. Each transaction the mempool then holds
    is announced, one held before the call too, as `BroadcastTransaction`
    announces it for `submitpackage`. None is marked unbroadcast: Core
    holds each in the mempool already when it calls `BroadcastTransaction`,
    which marks only what it adds itself (`src/node/transaction.cpp`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    call = _read(params)
    package_msg, outcomes = _accept(node, call)
    mempool = node.mempool
    for tx in call.txs:
        held = mempool.get_tx(tx.id)
        if held is not None:
            node.p2p_manager.broadcast_raw_transaction(held, mempool.fees[held.hash])
    return {
        "package_msg": package_msg,
        "tx-results": {
            tx.hash.hex(): _tx_result(tx, outcomes[tx.hash]) for tx in call.txs
        },
        "replaced-transactions": [],
    }


def _read(params: list[Any]) -> _Call:
    """Read the arguments of `submitpackage`, or raise Core's refusal of them.

    Core's `submitpackage` (`src/rpc/mempool.cpp`), in its order: the usage
    for no argument, the count, `maxfeerate`, `maxburnamount`, then each
    transaction in turn for its type, its decoding and its burn, and last
    the topology (`IsChildWithParentsTree`, `src/policy/packages.cpp`).
    """
    from btclib_node.rpc.callbacks import (  # noqa: PLC0415 -- it imports this module
        _DEFAULT_MAX_BURN_AMOUNT,
        _MAX_BURN_EXCEEDED_REASON,
        _MAX_PACKAGE_COUNT,
        _amount_param,
        _check_transaction,
        _decode_hex_tx,
        _exceeds_max_burn,
        _parse_max_fee_rate,
    )

    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["submitpackage"])
    raw_txs = params[0]
    if not isinstance(raw_txs, list):
        raise type_error(1, "package", raw_txs, "array")
    if not 1 <= len(raw_txs) <= _MAX_PACKAGE_COUNT:
        err_msg = f"Array must contain between 1 and {_MAX_PACKAGE_COUNT} transactions."
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, err_msg)
    max_feerate = _parse_max_fee_rate(params, 1)
    max_burn_amount = _amount_param(
        params, 2, name="maxburnamount", default=_DEFAULT_MAX_BURN_AMOUNT
    )
    txs: list[Tx] = []
    for raw_tx in raw_txs:
        if not isinstance(raw_tx, str):
            err_msg = (
                f"JSON value of type {json_type_name(raw_tx)} is not of expected "
                "type string"
            )
            raise RpcError(RPCErrorCode.TYPE_ERROR, err_msg)
        try:
            tx = _decode_hex_tx(raw_tx)
        except BTClibException as error:
            err_msg = (
                f"TX decode failed: {raw_tx} Make sure the tx has at least one input."
            )
            raise RpcError(RPCErrorCode.DESERIALIZATION_ERROR, err_msg) from error
        if _exceeds_max_burn(tx, max_burn_amount):
            raise RpcError(RPCErrorCode.VERIFY_ERROR, _MAX_BURN_EXCEEDED_REASON)
        txs.append(tx)
    if len(txs) > 1:
        *parents, child = txs
        spent_by_child = {tx_in.prev_out.tx_id for tx_in in child.vin}
        parent_ids = {parent.id for parent in parents}
        if not parent_ids <= spent_by_child or any(
            tx_in.prev_out.tx_id in parent_ids
            for parent in parents
            for tx_in in parent.vin
        ):
            raise RpcError(RPCErrorCode.VERIFY_ERROR, _TOPOLOGY_REFUSED)
    invalid = {tx.hash: _check_transaction(tx) for tx in txs}
    return _Call(
        txs, max_feerate, {wtxid: why for wtxid, why in invalid.items() if why}
    )


def _tx_result(tx: Tx, outcome: _Outcome) -> dict[str, Any]:
    """Return the JSON of what `outcome` says of `tx`."""
    from btclib_node.rpc.callbacks import (  # noqa: PLC0415 -- it imports this module
        _MISSING_INPUTS_REASON,
        _btc_amount,
    )

    result: dict[str, Any] = {"txid": tx.id.hex()}
    if outcome.error is not None:
        missing = isinstance(outcome.error, MissingPrevoutError)
        result["error"] = _MISSING_INPUTS_REASON if missing else str(outcome.error)
    elif outcome.other_wtxid is not None:
        result["other-wtxid"] = outcome.other_wtxid.hex()
    else:
        fees: dict[str, Any] = {"base": _btc_amount(outcome.base_fee)}
        result["vsize"] = outcome.vsize
        result["fees"] = fees
        if outcome.effective is not None:
            modified_fee, vsize, wtxids = outcome.effective
            # `CFeeRate::GetFeePerK` rounds down
            fees["effective-feerate"] = _btc_amount(modified_fee * 1000 // vsize)
            fees["effective-includes"] = [wtxid.hex() for wtxid in wtxids]
    return result


def _held(mempool: Mempool, tx: Tx) -> _Outcome | None:
    """Return what the mempool says of `tx` if it holds it in some witness."""
    wtxid = tx.hash
    if mempool.contains_tx(tx):
        return _Outcome(vsize=mempool.vsizes[wtxid], base_fee=mempool.fees[wtxid])
    if tx.id in mempool.txid_index:
        return _Outcome(other_wtxid=mempool.txid_index[tx.id])
    return None


def _accept(node: Node, call: _Call) -> tuple[str, dict[bytes, _Outcome]]:
    """Take what passes of `call.txs`, and return the message and each outcome.

    Core's `AcceptPackage` (`src/validation.cpp`, same tag). A package
    `package_refusal` refuses answers each transaction "package-not-validated".
    Otherwise each is taken or refused alone, in turn: one the mempool holds, or
    holds under another witness, is answered as such, and one that is taken
    is in the mempool for the next. A refusal that is not a fee floor or a
    missing input, which a package may undo, ends the package: the others
    are still tried alone, but none with another. The transactions left are
    taken together by `_accept_together`.

    Taking a transaction alone trims nothing: the mempool is trimmed
    once at the end, as Core's `AcceptPackage` does, so a child can pay for
    a parent a full mempool would have refused. The transactions taken
    together keep `Mempool.add_package`'s own trimming, which scores them as
    a whole. Each one gone by the end is refused "mempool full", as Core's
    last pass over the results does.
    """
    txs = call.txs
    refusal = package_refusal(txs)
    if refusal is not None:
        error = TxRejectedError("package-not-validated")
        return refusal, {tx.hash: _Outcome(error=error) for tx in txs}
    mempool = node.mempool
    outcomes: dict[bytes, _Outcome] = {}
    together: list[Tx] = []
    ended = False
    for tx in txs:
        outcome = _held(mempool, tx) or _accept_alone(node, tx, call)
        if outcome.error is not None:
            if len(txs) > 1 and _a_package_may_undo(outcome.error):
                together.append(tx)
            else:
                ended = True
        outcomes[tx.hash] = outcome
    message = "success"
    if ended or len(together) == 1:
        message = "transaction failed"
    elif together:
        message = _accept_together(node, together, call.max_feerate, outcomes)
    # A package whose parent's own feerate is the lowest is evicted whole
    # here, where Core keeps it by its chunk feerate:
    # btclib-org/btclib-node#1740
    mempool.trim()
    for tx in txs:
        if outcomes[tx.hash].error is None and tx.id not in mempool.txid_index:
            outcomes[tx.hash] = _Outcome(error=TxRejectedError("mempool full"))
            message = "transaction failed"
    return message, outcomes


def _a_package_may_undo(error: Exception) -> bool:
    """Whether a child may undo `error`: a fee floor or a missing input."""
    return isinstance(error, MissingPrevoutError) or (
        isinstance(error, TxRejectedError) and error.reconsiderable
    )


def _accept_alone(node: Node, tx: Tx, call: _Call) -> _Outcome:
    """Take `tx` by itself, or return what refuses it.

    Core's `AcceptSingleTransactionInternal`: its checks are
    `pre_verify_mempool_acceptance`'s, `maxfeerate` among them. A missing
    input of a transaction the chain already holds is "txn-already-known",
    which `pre_verify_mempool_acceptance` raises and no package undoes.
    """
    mempool = node.mempool
    if tx.hash in call.invalid:
        return _Outcome(error=TxRejectedError(call.invalid[tx.hash]))
    try:
        candidate = pre_verify_mempool_acceptance(
            node, tx, max_feerate=call.max_feerate
        )
        check_transaction(candidate.prev_outputs, tx)
    except (MissingPrevoutError, TxRejectedError) as refusal:
        return _Outcome(error=refusal)
    tip_height = len(node.chainstate.block_index.active_chain) - 1
    mempool.add_tx(tx, candidate.fee, candidate.vsize, height=tip_height, trim=False)
    effective = (candidate.fee + mempool.delta(tx.id), candidate.vsize, [tx.hash])
    return _Outcome(vsize=candidate.vsize, base_fee=candidate.fee, effective=effective)


def _accept_together(
    node: Node, txs: list[Tx], max_feerate: int, outcomes: dict[bytes, _Outcome]
) -> str:
    """Take `txs`, each refused alone, as a package, and return the message.

    Core's `AcceptMultipleTransactionsInternal`. A refusal sets the outcome
    of the transaction it names, unless Core refuses the package as a whole
    and that transaction keeps its answer from alone: the TRUC rules only the
    package reveals, which Core's message carries, and the cluster limit. The
    dust a parent leaves is "unspent-dust", with the refusal of the child.
    """
    try:
        candidates = pre_verify_subpackage(node, txs, max_feerate=max_feerate)
    except PackageRefusedError as refused:
        ((wtxid, refusal),) = refused.errors.items()
        reason = refusal.reason if isinstance(refusal, TxRejectedError) else ""
        if reason == "TRUC-violation" and refused.package_level:
            return str(refusal)
        if reason == "too-large-cluster":
            return reason
        outcomes[wtxid] = _Outcome(error=refusal)
        if reason == "missing-ephemeral-spends":
            return "unspent-dust"
        return "transaction failed"
    items = [
        (candidate.prev_outputs, tx)
        for tx, candidate in zip(txs, candidates, strict=True)
    ]
    failure = check_package(items)
    if failure is not None:
        index, error = failure
        outcomes[txs[index].hash] = _Outcome(error=error)
        return "transaction failed"
    mempool = node.mempool
    tip_height = len(node.chainstate.block_index.active_chain) - 1
    members = [
        (tx, candidate.fee, candidate.vsize)
        for tx, candidate in zip(txs, candidates, strict=True)
    ]
    mempool.add_package(members, height=tip_height)
    effective = (
        sum(fee + mempool.delta(tx.id) for tx, fee, _ in members),
        sum(vsize for _, _, vsize in members),
        [tx.hash for tx in txs],
    )
    for tx, fee, vsize in members:
        outcomes[tx.hash] = _Outcome(vsize=vsize, base_fee=fee, effective=effective)
    return "success"
