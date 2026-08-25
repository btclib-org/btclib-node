# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Script and transaction validation, dispatched across `Node.worker_pool`.

`get_flags` reads which script rules are active at a given height off
`Config.chain.flags`; `check_transactions` fans a block's inputs out
across the worker pool and `warm` is what a fresh worker process runs
once, on `Node.warm_worker_pool`'s dispatch, so the cost of importing
`btclib.script.engine` is paid before a real check ever needs it
(btclib-org/btclib-node#262).
"""

from typing import TYPE_CHECKING

from btclib.script.engine import verify_amounts, verify_input, verify_transaction
from btclib.script.sig_hash import PrecomputedTxData

from btclib_node.exceptions import PrevoutCountMismatchError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from btclib.tx.tx import Tx
    from btclib.tx.tx_out import TxOut

    from btclib_node import Node
    from btclib_node.config import Config


def get_flags(config: Config, index: int) -> tuple[str, ...]:
    """Return every script flag already active at block height `index`.

    `config.chain.flags` is a chain's own `(height, name)` pairs,
    ordered by activation height; a flag activated at or before `index`
    is one that applies to a block at that height and every one after.
    """
    return tuple(f for (i, f) in config.chain.flags if index >= i)


def f(
    prevouts: list[TxOut],
    tx: Tx,
    i: int,
    flags: tuple[str, ...],
    precomputed: PrecomputedTxData,
) -> None:
    """Verify input `i` of `tx` against its own prevout, one `starmap` task."""
    # no need to deepcopy the values as they are not reused
    verify_input(prevouts, tx, i, flags, precomputed)


def warm() -> None:
    """Do nothing, once a worker process has imported this module to run it.

    `Node.warm_worker_pool` dispatches several of these across the pool
    so that every worker process pays the import of this module -- and
    of `btclib.script.engine` above, the expensive part of it -- before
    `check_transactions` below ever needs one of them for real
    (btclib-org/btclib-node#262).
    """


def _tasks(
    transaction_data: list[tuple[list[TxOut], Tx]], flags: tuple[str, ...]
) -> Iterator[tuple[list[TxOut], Tx, int, tuple[str, ...], PrecomputedTxData]]:
    """One `f` task per input, carrying its own transaction's precomputed data.

    Core keeps this same pair of properties -- per-input granularity and
    one precomputation per transaction -- because its checks share a raw
    `PrecomputedTransactionData*` into a per-block
    `std::vector<PrecomputedTransactionData> txsdata(block.vtx.size())`
    across the threads its `CCheckQueue` runs them on (`validation.cpp`'s
    `ConnectBlock` and `validation.h`'s `CScriptCheck::txdata`,
    bitcoin/bitcoin@794a753958). `Node.worker_pool` is a process pool
    rather than threads, so nothing here can be shared by pointer: what
    a thread reads through `txdata` a process has to receive as its own
    pickled copy, one per task. That copy is a handful of hashes, cheap
    next to the whole transaction every task already carries and far
    cheaper than the re-serialization per input it replaces
    (btclib-org/btclib-node#385).

    Built once per transaction and shared, by reference, across that
    transaction's own tasks -- never rebuilt per input, which would be
    the same Θ(N²) this exists to remove. Sharing it this way is sound
    only because it is pickled untouched into every task from this one
    snapshot of `tx` and `prevouts`: `sig_hash.from_tx`'s docstring says
    a precomputed "must describe this very tx, and nothing here can tell
    whether it does", and `PrecomputedTxData`'s own says why -- a hash
    computed lazily out of a mutable `Tx` can change under its caller
    (btclib-org/btclib#140). Nothing between the construction below and the
    worker that consumes it can mutate `tx` to break that.
    """
    for prevouts, tx in transaction_data:
        precomputed = PrecomputedTxData(tx, prevouts)
        yield from ((prevouts, tx, i, flags, precomputed) for i in range(len(prevouts)))


def check_transactions(
    transaction_data: list[tuple[list[TxOut], Tx]], index: int, node: Node
) -> None:
    """Verify a candidate block's own transactions, fanned out across the pool.

    Raises on the first bad input `node.worker_pool.starmap` reaches --
    `main.update_chain`'s own caller is what rolls the chainstate back
    and leaves the block off the active chain once this does. Amounts
    are checked here, per transaction and outside the pool, since
    script validation alone never reads them.
    """
    if not transaction_data:
        return
    if any(len(x[0]) != len(x[1].vin) for x in transaction_data):
        raise PrevoutCountMismatchError

    flags = get_flags(node.config, index)

    # Script validation never reads the amounts except through the
    # sig_hash, so a block's transactions have to be checked against
    # their prevouts separately or a block may print money. Per
    # transaction, and cheap, so it stays out of the worker pool.
    for prevouts, tx in transaction_data:
        verify_amounts(prevouts, tx)

    # Raising is the point: an input that does not verify has to reach
    # main.update_chain, which rolls the chainstate back and leaves the
    # block off the active chain.
    node.worker_pool.starmap(f, _tasks(transaction_data, flags))


def check_transaction(prevouts: list[TxOut], tx: Tx, index: int, node: Node) -> None:
    """Verify one transaction against its prevouts, on the caller's own thread.

    Not routed through `Node.worker_pool`, unlike `check_transactions`
    above: this runs once per mempool acceptance rather than once per
    block's worth of inputs, so the pool's own process-pickling cost
    would outweigh what it buys here.
    """
    # No copy: btclib's engine leaves the transaction alone -- sig_hash
    # builds the blanked transaction each preimage commits to rather
    # than editing the one it was handed. What the copy paid for was a
    # defect that is not there, once per mempool acceptance.
    flags = get_flags(node.config, index)
    verify_transaction(prevouts, tx, flags)
