# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A block as the `cmpctblock` this node sends of it.

Its own module because both of its callers need it: `callbacks`, which
serves a `cmpctblock` asked for, and `main`'s block announcement, which
sends one to a high-bandwidth peer. `callbacks` imports `main`, so the
function cannot live there.
"""

from typing import TYPE_CHECKING

from btclib.p2p.compact_blocks import CmpctBlock, PrefilledTransaction

if TYPE_CHECKING:
    from btclib.block import Block

__all__ = ["compact_block"]


def compact_block(block: Block, nonce: int) -> CmpctBlock:
    """Return `block` as a `cmpctblock`, the coinbase alone sent whole.

    Core's `CBlockHeaderAndShortTxIDs` constructor: the coinbase
    prefilled at index 0, and every other transaction by the short id of
    its wtxid under the key `nonce` and the header give.
    """
    coinbase = PrefilledTransaction(0, block.transactions[0])
    keyed = CmpctBlock(block.header, nonce, (), (coinbase,), check_validity=False)
    short_ids = [keyed.short_id(tx.hash) for tx in block.transactions[1:]]
    return CmpctBlock(block.header, nonce, short_ids, (coinbase,))
