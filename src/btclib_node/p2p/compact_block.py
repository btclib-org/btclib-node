# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""BIP152 compact blocks, as this node sends and receives them.

`compact_block` is a block as the `cmpctblock` this node sends of it.
`callbacks` serves one asked for, and `main`'s block announcement sends
one to a high-bandwidth peer. `callbacks` imports `main`, so what both
need cannot live there.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from btclib.p2p.compact_blocks import CmpctBlock, PrefilledTransaction

if TYPE_CHECKING:
    from btclib.block import Block
    from btclib.tx import Tx

__all__ = [
    "MAX_CMPCTBLOCKS_INFLIGHT_PER_BLOCK",
    "MAX_EXTRA_TXNS",
    "MAX_EXTRA_TX_WEIGHT",
    "MostRecentBlock",
    "compact_block",
]

# Core's `MAX_CMPCTBLOCKS_INFLIGHT_PER_BLOCK` and
# `DEFAULT_BLOCK_RECONSTRUCTION_EXTRA_TXN` (`src/net_processing.h`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag): how many peers one block is
# asked of through `cmpctblock`, and how many refused transactions are kept
# to rebuild one with. Core's `-blockreconstructionextratxn` sets the
# second, and this tree has no such option.
MAX_CMPCTBLOCKS_INFLIGHT_PER_BLOCK = 3
MAX_EXTRA_TXNS = 100
# Core keeps a refused transaction for that only below 100000 bytes of
# `RecursiveDynamicUsage`. Its orphanage measures a transaction's memory by
# its weight, which "is often higher than the actual memory usage"
# (`src/node/txorphanage.cpp`, same tag), and so does this bound.
MAX_EXTRA_TX_WEIGHT = 100_000


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
