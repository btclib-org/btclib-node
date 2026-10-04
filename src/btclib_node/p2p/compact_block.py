# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A block as the `cmpctblock` this node sends of it.

Its own module because both of its callers need it: `callbacks`, which
serves a `cmpctblock` asked for, and `main`'s block announcement, which
sends one to a high-bandwidth peer. `callbacks` imports `main`, so the
function cannot live there.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from btclib.p2p.compact_blocks import CmpctBlock, PrefilledTransaction

if TYPE_CHECKING:
    from btclib.block import Block
    from btclib.tx import Tx

__all__ = ["MostRecentBlock", "compact_block"]


@dataclass(frozen=True, slots=True)
class MostRecentBlock:
    """The block `new_pow_valid_block` last kept, and its `cmpctblock`.

    Core's `m_most_recent_block` and `m_most_recent_compact_block`
    (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), which `m_most_recent_block_hash` names. `txs` is Core's
    `m_most_recent_block_txs`, built with them: every transaction of the
    block under its txid and under its wtxid, the two kept apart as
    Core's `GenTxid` does.
    """

    block: Block
    compact: CmpctBlock
    txs: dict[tuple[bool, bytes], Tx] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Index the block's transactions by txid and by wtxid."""
        txs: dict[tuple[bool, bytes], Tx] = {}
        for tx in self.block.transactions:
            txs.setdefault((False, tx.id), tx)
            txs.setdefault((True, tx.hash), tx)
        object.__setattr__(self, "txs", txs)

    @property
    def hash(self) -> bytes:
        """Return the block's hash, Core's `m_most_recent_block_hash`."""
        return self.block.header.hash


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
