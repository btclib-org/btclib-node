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

from itertools import chain
from typing import TYPE_CHECKING

from btclib.script.engine import verify_amounts, verify_input, verify_transaction

from btclib_node.exceptions import PrevoutCountMismatchError

if TYPE_CHECKING:
    from btclib.tx.tx import Tx
    from btclib.tx.tx_out import TxOut

    from btclib_node import Node
    from btclib_node.config import Config


def get_flags(config: Config, index: int) -> tuple[str, ...]:
    return tuple(f for (i, f) in config.chain.flags if index >= i)


def f(prevouts: list[TxOut], tx: Tx, i: int, flags: tuple[str, ...]) -> None:
    # no need to deepcopy the values as they are not reused
    verify_input(prevouts, tx, i, flags)


def warm() -> None:
    """Do nothing, once a worker process has imported this module to run it.

    `Node.warm_worker_pool` dispatches several of these across the pool
    so that every worker process pays the import of this module -- and
    of `btclib.script.engine` above, the expensive part of it -- before
    `check_transactions` below ever needs one of them for real
    (btclib-org/btclib-node#262).
    """


def check_transactions(
    transaction_data: list[tuple[list[TxOut], Tx]], index: int, node: Node
) -> None:
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
    node.worker_pool.starmap(
        f,
        chain.from_iterable(
            ((x[0], x[1], i, flags) for i in range(len(x[0]))) for x in transaction_data
        ),
    )


def check_transaction(prevouts: list[TxOut], tx: Tx, index: int, node: Node) -> None:
    # No copy: btclib's engine leaves the transaction alone -- sig_hash
    # builds the blanked transaction each preimage commits to rather
    # than editing the one it was handed. What the copy paid for was a
    # defect that is not there, once per mempool acceptance.
    flags = get_flags(node.config, index)
    verify_transaction(prevouts, tx, flags)
