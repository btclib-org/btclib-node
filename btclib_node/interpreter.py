# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from copy import deepcopy
from itertools import chain

from btclib.script.engine import verify_amounts, verify_input, verify_transaction


def get_flags(config, index) -> tuple[str, ...]:
    return tuple(f for (i, f) in config.chain.flags if index >= i)


def f(prevouts, tx, i, flags):
    # no need to deepcopy the values as they are not reused
    verify_input(prevouts, tx, i, flags)


def check_transactions(transaction_data, index, node):
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


def check_transaction(prevouts, tx, index, node):
    # TODO: we need to deepcopy the transaction because
    # verify_transaction modifies it. To fix upstream
    tx = deepcopy(tx)
    flags = get_flags(node.config, index)
    verify_transaction(prevouts, tx, flags)
