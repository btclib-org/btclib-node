# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from itertools import chain
from typing import TYPE_CHECKING

from btclib.script.engine import verify_amounts, verify_input, verify_transaction
from btclib.tx.tx import Tx
from btclib.tx.tx_out import TxOut

from btclib_node.config import Config

if TYPE_CHECKING:
    from btclib_node import Node


def get_flags(config: Config, index: int) -> tuple[str, ...]:
    return tuple(f for (i, f) in config.chain.flags if index >= i)


def f(prevouts: list[TxOut], tx: Tx, i: int, flags: tuple[str, ...]) -> None:
    # no need to deepcopy the values as they are not reused
    verify_input(prevouts, tx, i, flags)


def check_transactions(
    transaction_data: list[tuple[list[TxOut], Tx]], index: int, node: Node
) -> None:
    if not transaction_data:
        return
    if any(len(x[0]) != len(x[1].vin) for x in transaction_data):
        raise ValueError

    FLAGS = get_flags(node.config, index)

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
            ((x[0], x[1], i, FLAGS) for i in range(len(x[0]))) for x in transaction_data
        ),
    )


def check_transaction(prevouts: list[TxOut], tx: Tx, index: int, node: Node) -> None:
    # No copy: btclib's engine leaves the transaction alone -- sig_hash
    # builds the blanked transaction each preimage commits to rather
    # than editing the one it was handed. What the copy paid for was a
    # defect that is not there, once per mempool acceptance.
    flags = get_flags(node.config, index)
    verify_transaction(prevouts, tx, flags)
