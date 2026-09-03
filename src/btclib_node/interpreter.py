# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Script and transaction validation, dispatched across `Node.worker_pool`.

`get_flags` is `Config.chain.consensus.script_flags_at`, Bitcoin Core's
own `GetBlockScriptFlags`: `check_transactions` fans a block's inputs out
across the worker pool and `warm` is what a fresh worker process runs
once, under `Node.worker_pool`'s process arm, on `Node.warm_worker_pool`'s
dispatch, so the cost of importing `btclib.script.engine` is paid before
a real check ever needs it (btclib-org/btclib-node#262). Under the
thread arm `_pool_factory` picks on a free-threaded interpreter
(btclib-org/btclib-node#388), that import is already paid by the time
this module's own is, so `warm` still runs there but has nothing left to
pay for.
"""

from typing import TYPE_CHECKING

from btclib.exceptions import BTClibValueError
from btclib.script.engine import verify_amounts, verify_input, verify_transaction
from btclib.script.sig_hash import PrecomputedTxData

from btclib_node.constants import COINBASE_MATURITY
from btclib_node.exceptions import PrevoutCountMismatchError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from btclib.script.engine.flags import ScriptFlag, ScriptFlags
    from btclib.tx.tx import Tx
    from btclib.tx.tx_out import TxOut

    from btclib_node import Node
    from btclib_node.block_db import Coin
    from btclib_node.config import Config

__all__ = [
    "check_coinbase_maturity",
    "check_coinbase_value",
    "check_final_transactions",
    "check_sequence_locks",
    "check_transaction",
    "check_transactions",
    "f",
    "get_flags",
    "is_final_tx",
    "warm",
]

# Core's `LOCKTIME_THRESHOLD` (`src/script/script.h:48`,
# at bitcoin/bitcoin@204256c73f): a `lock_time` below this is a block
# height, at or above it a unix timestamp. btclib's own
# `op_checklocktimeverify`/`op_checksequenceverify`
# (`script/engine/script_op_codes.py`) inline this threshold and the
# sequence bit layout below as bare literals rather than naming them;
# this module names them once since `is_final_tx` and
# `check_sequence_locks` below each read more than one of them.
_LOCKTIME_THRESHOLD = 500_000_000
_SEQUENCE_FINAL = 0xFFFFFFFF
# Core's `CTxIn::SEQUENCE_LOCKTIME_*` (`src/primitives/transaction.h:93-114`,
# same commit): bit 31 opts a whole input out of BIP68, bit 22 picks
# time over block-height units, and the low sixteen bits are the actual
# relative lock, in whichever unit bit 22 named.
_SEQUENCE_LOCKTIME_DISABLE_FLAG = 1 << 31
_SEQUENCE_LOCKTIME_TYPE_FLAG = 1 << 22
_SEQUENCE_LOCKTIME_MASK = 0x0000FFFF
_SEQUENCE_LOCKTIME_GRANULARITY = 9


def get_flags(
    config: Config, index: int, block_hash: bytes | None = None
) -> ScriptFlag:
    """Return every script rule active at block height `index`.

    `config.chain.consensus.script_flags_at` is Bitcoin Core's own
    `GetBlockScriptFlags`: P2SH, segwit v0 and taproot are on for every
    block, and `index` decides only the four rules gated on a buried
    deployment. `block_hash` is what answers for the handful of blocks
    that predate one of the three always-on rules and would fail it,
    exempted by hash rather than by height -- `None` where no block is
    being connected, as for a mempool candidate.
    """
    return config.chain.consensus.script_flags_at(index, block_hash)


def f(
    prevouts: list[TxOut],
    tx: Tx,
    i: int,
    flags: ScriptFlags,
    precomputed: PrecomputedTxData,
) -> None:
    """Verify input `i` of `tx` against its own prevout, one `starmap` task."""
    # no need to deepcopy: btclib's script engine never writes to `tx`,
    # `prevouts` or `precomputed`, established rather than assumed of
    # every concurrent caller a `ThreadPool` can hand this to
    # (`_pool_factory`'s own docstring, btclib-org/btclib-node#388) --
    # not only that a process pool's own copy is not reused, which is
    # what would have to hold under threads too and does not on its own
    verify_input(prevouts, tx, i, flags, precomputed)


def warm() -> None:
    """Do nothing, once a worker has imported this module to run it.

    `Node.warm_worker_pool` dispatches several of these across the pool
    so that every worker pays the import of this module -- and of
    `btclib.script.engine` above, the expensive part of it -- before
    `check_transactions` below ever needs one of them for real
    (btclib-org/btclib-node#262). Only a genuine cost under
    `Node.worker_pool`'s process arm: a worker thread shares the one
    import its own process already paid, so the dispatch reaches it too
    but finds nothing left to do (btclib-org/btclib-node#388).
    """


def _tasks(
    transaction_data: list[tuple[list[Coin], Tx]], flags: ScriptFlag
) -> Iterator[tuple[list[TxOut], Tx, int, ScriptFlag, PrecomputedTxData]]:
    """One `f` task per input, carrying its own transaction's precomputed data.

    Core keeps this same pair of properties -- per-input granularity and
    one precomputation per transaction -- because its checks share a raw
    `PrecomputedTransactionData*` into a per-block
    `std::vector<PrecomputedTransactionData> txsdata(block.vtx.size())`
    across the threads its `CCheckQueue` runs them on (`validation.cpp`'s
    `ConnectBlock` and `validation.h`'s `CScriptCheck::txdata`,
    at bitcoin/bitcoin@794a753958). Under `_pool_factory`'s process arm
    nothing here can be shared by pointer the way Core's threads share
    `txdata`: what a thread reads through it a process has to receive as
    its own pickled copy, one per task. That copy is a handful of
    hashes, cheap next to the whole transaction every task already
    carries and far cheaper than the re-serialization per input it
    replaces (btclib-org/btclib-node#385). Under the thread arm, this
    generator's own `precomputed` is that pointer: every task built from
    one iteration of the loop below shares the identical object, exactly
    as Core's `CScriptCheck`s share `txdata[i]` (btclib-org/btclib-node#388).

    Built once per transaction and shared, by reference, across that
    transaction's own tasks -- never rebuilt per input, which would be
    the same Θ(N²) this exists to remove. Sharing it this way is sound
    whether a task's own copy is pickled from this snapshot or is this
    snapshot, because nothing between the construction below and the
    worker that consumes it -- in either arm -- mutates `tx` or
    `precomputed` to break it: `sig_hash.from_tx`'s docstring says a
    precomputed "must describe this very tx, and nothing here can tell
    whether it does", `PrecomputedTxData`'s own says why -- a hash
    computed lazily out of a mutable `Tx` can change under its caller
    (btclib-org/btclib#140) -- and `_pool_factory`'s own docstring is
    where that claim was established for the thread arm rather than
    assumed of it (btclib-org/btclib-node#388).
    """
    for prevouts, tx in transaction_data:
        tx_outs = [coin.tx_out for coin in prevouts]
        precomputed = PrecomputedTxData(tx, tx_outs)
        yield from ((tx_outs, tx, i, flags, precomputed) for i in range(len(tx_outs)))


def check_transactions(
    transaction_data: list[tuple[list[Coin], Tx]],
    index: int,
    node: Node,
    block_hash: bytes,
) -> None:
    """Verify a candidate block's own transactions, fanned out across the pool.

    Raises on the first bad input `node.worker_pool.starmap` reaches --
    `main.update_chain`'s own caller is what rolls the chainstate back
    and leaves the block off the active chain once this does. Amounts
    are checked here, per transaction and outside the pool, since
    script validation alone never reads them. `transaction_data` carries
    each prevout as a `Coin` -- what `check_coinbase_maturity` below
    needs of it -- and every btclib call here wants a bare `TxOut`, so
    each is unwrapped where it is used rather than threaded through as
    two parallel lists. `block_hash` is the candidate block's own --
    `main._validate_block`'s caller already has it -- so `get_flags`
    below can answer for the handful of blocks the buried heights alone
    get wrong.
    """
    if not transaction_data:
        return
    if any(len(x[0]) != len(x[1].vin) for x in transaction_data):
        raise PrevoutCountMismatchError

    flags = get_flags(node.config, index, block_hash)

    # Script validation never reads the amounts except through the
    # sig_hash, so a block's transactions have to be checked against
    # their prevouts separately or a block may print money. Per
    # transaction, and cheap, so it stays out of the worker pool.
    for prevouts, tx in transaction_data:
        verify_amounts([coin.tx_out for coin in prevouts], tx)

    # Raising is the point: an input that does not verify has to reach
    # main.update_chain, which rolls the chainstate back and leaves the
    # block off the active chain.
    node.worker_pool.starmap(f, _tasks(transaction_data, flags))


def check_transaction(
    prevouts: list[TxOut], tx: Tx, index: int, node: Node, tip_hash: bytes
) -> None:
    """Verify one transaction against its prevouts, on the caller's own thread.

    Not routed through `Node.worker_pool`, unlike `check_transactions`
    above: this runs once per mempool acceptance rather than once per
    block's worth of inputs, so the pool's own process-pickling cost
    would outweigh what it buys here.

    `tip_hash` is the active chain's own tip, not a hash of `tx` or of
    the candidate block it would join: there is no candidate block,
    `index` already being one past the tip (`main.verify_mempool_acceptance`'s
    own `spend_height`), so no block hash names the transaction being
    checked. Core's own re-check of a mempool candidate against
    consensus (`ConsensusScriptChecks`, `src/validation.cpp:1175-1185`,
    at bitcoin/bitcoin@9be056a8a7) reads `GetBlockScriptFlags` off
    `m_chain.Tip()` for the same reason -- the exception table is a
    lookup by hash and the tip is the one real, connected block this
    check has -- so `get_flags` below is asked the same way.
    """
    # No copy: btclib's engine leaves the transaction alone -- sig_hash
    # builds the blanked transaction each preimage commits to rather
    # than editing the one it was handed. What the copy paid for was a
    # defect that is not there, once per mempool acceptance.
    flags = get_flags(node.config, index, tip_hash)
    verify_transaction(prevouts, tx, flags)


def check_coinbase_value(
    coinbase: Tx,
    transaction_data: list[tuple[list[Coin], Tx]],
    index: int,
    node: Node,
) -> None:
    """Refuse a coinbase paying more than the subsidy plus the fees it collects.

    Core's `bad-cb-amount` (`ConnectBlock`, `src/validation.cpp:2619-2621`,
    at bitcoin/bitcoin@204256c73f): `nFees + GetBlockSubsidy(...)` is the
    ceiling. The fee sum is recomputed here from `transaction_data`'s own
    prevouts and outputs -- the same shape `main.verify_mempool_acceptance`
    already uses to recover a single transaction's own fee -- rather than
    threaded out of `verify_amounts` above, which returns nothing.
    """
    fees = sum(
        sum(coin.tx_out.value for coin in prevouts) - sum(x.value for x in tx.vout)
        for prevouts, tx in transaction_data
    )
    coinbase_value = sum(x.value for x in coinbase.vout)
    ceiling = node.chain.subsidy(index) + fees
    if coinbase_value > ceiling:
        err_msg = f"coinbase pays too much: {coinbase_value} instead of {ceiling}"
        raise BTClibValueError(err_msg)


def check_coinbase_maturity(prevouts: list[Coin], spend_height: int) -> None:
    """Refuse a spend of a coinbase output not yet `COINBASE_MATURITY` deep.

    Core's `bad-txns-premature-spend-of-coinbase` (`Consensus::CheckTxInputs`,
    `src/consensus/tx_verify.cpp:185-186`, at bitcoin/bitcoin@204256c73f):
    `nSpendHeight - coin.nHeight < COINBASE_MATURITY`. Called once per
    transaction rather than once per block, because `spend_height` is not
    the same number for both of this tree's own callers: `main._validate_block`
    passes the height of the block connecting the spend, and
    `main.verify_mempool_acceptance` passes one past the active chain's own
    tip -- the height a mempool transaction would have if it were mined
    next, matching Core's own `AcceptToMemoryPoolWorker`
    (`src/validation.cpp:897`, same commit).
    """
    for coin in prevouts:
        if coin.is_coinbase and spend_height - coin.height < COINBASE_MATURITY:
            err_msg = "bad-txns-premature-spend-of-coinbase"
            raise BTClibValueError(err_msg)


def is_final_tx(tx: Tx, height: int, block_time: int) -> bool:
    """Whether `tx` is final at `height`, against a cutoff of `block_time`.

    Core's `IsFinalTx` (`src/consensus/tx_verify.cpp:23-42`,
    at bitcoin/bitcoin@204256c73f): a zero `lock_time` is always final;
    otherwise it is a block height below `_LOCKTIME_THRESHOLD` and a
    unix timestamp at or above it, and `tx` is final once `height` or
    `block_time` -- whichever `lock_time`'s own units name -- has passed
    it. Still final regardless, if every one of `tx`'s own inputs opts
    out of `lock_time` by carrying `_SEQUENCE_FINAL`: OP_CHECKLOCKTIMEVERIFY
    depends on this escape hatch never firing for an input it itself
    guards, which is why it also refuses a final sequence on its own
    input (`btclib.script.engine.script_op_codes.op_checklocktimeverify`).
    """
    if tx.lock_time == 0:
        return True
    cutoff = height if tx.lock_time < _LOCKTIME_THRESHOLD else block_time
    if tx.lock_time < cutoff:
        return True
    return all(tx_in.sequence == _SEQUENCE_FINAL for tx_in in tx.vin)


def check_final_transactions(
    transactions: list[Tx], height: int, block_time: int
) -> None:
    """Refuse a block carrying a transaction that is not final.

    Core's `bad-txns-nonfinal` (`ContextualCheckBlock`,
    `src/validation.cpp:4158-4166`, at bitcoin/bitcoin@204256c73f): every
    transaction the block carries, coinbase included -- unlike
    `check_sequence_locks` below, which Core itself only ever asks of
    the non-coinbase ones. `block_time` is the cutoff `is_final_tx`
    checks `lock_time` against: `main._validate_block`'s and
    `main.verify_mempool_acceptance`'s own docstrings say what each
    passes and why.
    """
    for tx in transactions:
        if not is_final_tx(tx, height, block_time):
            err_msg = "bad-txns-nonfinal"
            raise BTClibValueError(err_msg)


def check_sequence_locks(
    transaction_data: list[tuple[list[Coin], Tx]],
    height: int,
    *,
    enforce_bip68: bool,
    tip_median_time_past: int,
    ancestor_median_time_past: Callable[[int], int],
) -> None:
    """Refuse a non-coinbase transaction whose BIP68 relative lock is unmet.

    Core's `SequenceLocks`/`CalculateSequenceLocks`/`EvaluateSequenceLocks`
    (`src/consensus/tx_verify.cpp:45-115`, at bitcoin/bitcoin@204256c73f),
    over each input's own `Coin.height` rather than a freshly-read
    `CCoinsViewCache` -- the same prevouts `check_transactions` above
    already carries per transaction, so this reads them rather than
    asking the UTXO set again.

    `enforce_bip68` is Core's own
    `DeploymentActiveAt(pindex, ..., DEPLOYMENT_CSV)`: this tree has no
    BIP9 deployment tracking of its own, so `main.py`'s own callers pass
    whether `ScriptFlag.CHECKSEQUENCEVERIFY` is in `get_flags`'s answer
    instead -- sound because Core deploys BIP68, BIP112 and BIP113
    together as one soft fork, so the height that turns on the opcode is
    the height that turns on this. A transaction below version 2, or an
    input whose sequence carries `_SEQUENCE_LOCKTIME_DISABLE_FLAG`, is
    skipped rather than refused, matching BIP68.

    `tip_median_time_past` is Core's own `block.pprev->GetMedianTimePast()`
    -- the reference a height-based lock is compared against directly,
    and a time-based one after `ancestor_median_time_past` has already
    turned each input's own relative lock into an absolute one.
    `ancestor_median_time_past(h)` returns the median time past of the
    block at height `h`; time-based locks are measured from the block
    before the one that confirmed the coin (`max(coin.height - 1, 0)`),
    matching Core's own comment on why -- "the smallest allowed
    timestamp of the block containing the txout being spent".
    """
    if not enforce_bip68:
        return
    for prevouts, tx in transaction_data:
        if tx.version < 2:  # noqa: PLR2004
            continue
        min_height = -1
        min_time = -1
        for tx_in, coin in zip(tx.vin, prevouts, strict=True):
            sequence = tx_in.sequence
            if sequence & _SEQUENCE_LOCKTIME_DISABLE_FLAG:
                continue
            if sequence & _SEQUENCE_LOCKTIME_TYPE_FLAG:
                coin_time = ancestor_median_time_past(max(coin.height - 1, 0))
                min_time = max(
                    min_time,
                    coin_time
                    + (
                        (sequence & _SEQUENCE_LOCKTIME_MASK)
                        << _SEQUENCE_LOCKTIME_GRANULARITY
                    )
                    - 1,
                )
            else:
                min_height = max(
                    min_height, coin.height + (sequence & _SEQUENCE_LOCKTIME_MASK) - 1
                )
        if min_height >= height or min_time >= tip_median_time_past:
            err_msg = "bad-txns-nonfinal"
            raise BTClibValueError(err_msg)
