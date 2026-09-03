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
from btclib.script.engine.flags import ALL_FLAGS, ScriptFlag
from btclib.script.sig_hash import PrecomputedTxData

from btclib_node.exceptions import NonStandardTxError, PrevoutCountMismatchError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from btclib.script.engine.flags import ScriptFlags
    from btclib.tx.tx import Tx
    from btclib.tx.tx_out import TxOut

    from btclib_node import Node
    from btclib_node.block_db import Coin
    from btclib_node.config import Config

__all__ = [
    "STANDARD_FLAGS",
    "check_transaction",
    "check_transactions",
    "f",
    "get_flags",
    "warm",
]

# Core's own `STANDARD_SCRIPT_VERIFY_FLAGS` (`src/policy/policy.h:118-131`,
# at bitcoin/bitcoin@4519933391): what a transaction has to satisfy to be
# relayed and held, over what it has to satisfy to be mined.
# `btclib.script.engine.flags.ALL_FLAGS` is the consensus set and is
# Core's `MANDATORY_SCRIPT_VERIFY_FLAGS` member for member, so the union
# below is Core's own construction rather than a second transcription of
# the seven.
#
# A constant reading neither a height nor a block hash, as Core's
# `constexpr` is: relay policy is not gated on an activation height, and
# a tip that is one of the blocks `ConsensusParams.script_flags_at`
# exempts by hash would otherwise relax it.
#
# `SIGPUSHONLY` is a standardness flag btclib carries and this set leaves
# out, Core leaving it out of `STANDARD_SCRIPT_VERIFY_FLAGS` too.
# `CONST_SCRIPTCODE` is in, and btclib reads it more widely than Core:
# `btclib.script.engine._check_script_sig_policy` refuses a signature
# check carried anywhere in the script_sig, where Core errors inside the
# executed branch its `FindAndDelete` reaches, and that comment calls
# its own rule stricter in one direction and short in another.
STANDARD_FLAGS = (
    ALL_FLAGS
    | ScriptFlag.STRICTENC
    | ScriptFlag.LOW_S
    | ScriptFlag.MINIMALDATA
    | ScriptFlag.DISCOURAGE_UPGRADABLE_NOPS
    | ScriptFlag.CLEANSTACK
    | ScriptFlag.DISCOURAGE_UPGRADABLE_WITNESS_PROGRAM
    | ScriptFlag.MINIMALIF
    | ScriptFlag.NULLFAIL
    | ScriptFlag.WITNESS_PUBKEYTYPE
    | ScriptFlag.CONST_SCRIPTCODE
    | ScriptFlag.DISCOURAGE_UPGRADABLE_PUBKEYTYPE
    | ScriptFlag.DISCOURAGE_OP_SUCCESS
    | ScriptFlag.DISCOURAGE_UPGRADABLE_TAPROOT_VERSION
)


def get_flags(
    config: Config, index: int, block_hash: bytes | None = None
) -> ScriptFlag:
    """Return every script rule active at block height `index`.

    `config.chain.consensus.script_flags_at` is Bitcoin Core's own
    `GetBlockScriptFlags`: P2SH, segwit v0 and taproot are on for every
    block, and `index` decides only the four rules gated on a buried
    deployment. `block_hash` is what answers for the handful of blocks
    that predate one of the three always-on rules and would fail it,
    exempted by hash rather than by height -- `None` names no block at
    all, and answers as a hash on no exception row does.
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
    each prevout as a `Coin` -- what `main._validate_block`'s own
    `btclib.tx.tx_context.assert_coinbase_maturity` call needs of it --
    and every btclib call here wants a bare `TxOut`, so each is unwrapped
    where it is used rather than threaded through as two parallel lists.
    `block_hash` is the candidate block's own --
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


def _consensus_accepts(prevouts: list[TxOut], tx: Tx) -> bool:
    """Answer whether `ALL_FLAGS` takes a transaction `STANDARD_FLAGS` refused.

    Which separates a candidate a block may carry from one no chain
    will hold, and btclib's engine does not: every rule it enforces is
    refused with the same `BTClibValueError`, so the flag set a refusal
    was produced under is all there is to read it by. A second run is
    what that costs, and it is paid only where the candidate is already
    refused.

    `ALL_FLAGS` and not `get_flags`, which is what keeps
    `check_transaction` free of a height: it is the consensus set with
    every buried deployment binding, so it contains whatever
    `get_flags` answers at any height (`interpreter_test.py`) and a
    transaction it takes is one a block at any height carries. The two
    differ only for a transaction from before a soft fork, which
    `main.verify_mempool_acceptance`'s own docstring argues a mempool
    never holds.
    """
    try:
        verify_transaction(prevouts, tx, ALL_FLAGS)
    except BTClibValueError:
        return False
    return True


def check_transaction(prevouts: list[TxOut], tx: Tx) -> None:
    """Verify one mempool candidate against its prevouts, on this thread.

    Not routed through `Node.worker_pool`, unlike `check_transactions`
    above: this runs once per mempool acceptance rather than once per
    block's worth of inputs, so the pool's own process-pickling cost
    would outweigh what it buys here.

    `STANDARD_FLAGS` and not `get_flags`, which is Core's
    `PolicyScriptChecks` (`src/validation.cpp:1129-1150`,
    at bitcoin/bitcoin@4519933391): a candidate is judged against relay
    policy, and the consensus set alone admits to this node's mempool a
    transaction Core will not relay. Nothing here reads the chain, so
    this takes neither a height nor a `Node`.

    Core follows that call with `ConsensusScriptChecks` (`:1152-1183`,
    same commit) and this does not. That second call fills the script
    execution cache `ConnectBlock` reads later, which is not a structure
    this tree has -- `check_transactions` above verifies every input of
    every block transaction, mempool or not. What is left of it is an
    assertion: `STANDARD_FLAGS` is a superset of every set `get_flags`
    can answer, so a candidate this accepts is one the consensus rules
    accept unless the engine below disagrees with itself, which is why
    Core's own failure there is a `LogError("BUG! PLEASE REPORT THIS!")`
    under an `Assume(false)` rather than a verdict on the transaction.
    `interpreter_test.py` holds the superset claim that assertion rests
    on.

    A refusal the consensus set would not have made raises
    `NonStandardTxError` rather than reaching the caller as the engine
    gave it, which is what lets `p2p.callbacks.tx` keep the peer that
    relayed the transaction. Core's own comment above the set this one
    copies asks for that: a node forwarding a transaction which breaks
    one of the non-mandatory rules is neither banned nor disconnected
    (`src/policy/policy.h:112-117`, same commit).
    """
    # No copy: btclib's engine leaves the transaction alone -- sig_hash
    # builds the blanked transaction each preimage commits to rather
    # than editing the one it was handed. What the copy paid for was a
    # defect that is not there, once per mempool acceptance.
    try:
        verify_transaction(prevouts, tx, STANDARD_FLAGS)
    except BTClibValueError as refusal:
        if _consensus_accepts(prevouts, tx):
            raise NonStandardTxError(str(refusal)) from refusal
        raise
