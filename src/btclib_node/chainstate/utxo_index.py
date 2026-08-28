# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`UtxoIndex`, the set of transaction outputs a spend may still reference.

`add_block` applies one block's own spends and creations, returning the
prevouts each transaction consumed -- what `interpreter.check_transactions`
validates against -- and the `block_db.RevBlock` a reorg away from this
block would need to undo it.
"""

from typing import TYPE_CHECKING

from btclib.tx.out_point import OutPoint

from btclib_node.block_db import Coin, RevBlock
from btclib_node.exceptions import ChainstateInconsistencyError, InvalidBlockInputError

if TYPE_CHECKING:
    from btclib.block import Block
    from btclib.tx.tx import Tx

    from btclib_node.db import KeyValueStore
    from btclib_node.log import Logger

__all__ = ["UtxoIndex"]


class UtxoIndex:
    """The set of spendable outputs, staged in memory until `finalize`.

    `removed_utxos` and `updated_utxo_set` hold what a batch of
    `add_block`/`apply_rev_block` calls has changed since the last
    `finalize`; the module docstring above is where `add_block`'s own
    return value is argued.
    """

    def __init__(self, parent_db: KeyValueStore, logger: Logger) -> None:
        """Start with nothing staged, using `parent_db` for reads and writes."""
        self.db = parent_db

        self.removed_utxos: set[bytes] = set()
        self.updated_utxo_set: dict[bytes, Coin] = {}

        self.logger = logger

    def add_block(
        self, block: Block, height: int
    ) -> tuple[list[tuple[list[Coin], Tx]], RevBlock]:
        """Apply `block`'s own spends and creations, staged rather than written.

        `height` is this block's own height, on whichever branch it is
        being tried -- what every output it creates is stamped with, coin
        and coinbase alike, and never the height a later reorg
        disconnects or reconnects it at: `apply_rev_block` below restores
        a `Coin` exactly as this call staged it for removal, height and
        coinbase bit included, rather than recomputing either.

        Returns each non-coinbase transaction paired with the prevouts
        its own inputs consumed -- what `interpreter.check_transactions`
        validates against -- and the `RevBlock` that undoes this call.
        """
        removed: list[tuple[OutPoint, Coin]] = []
        added: list[OutPoint] = []
        complete_transactions: list[tuple[list[Coin], Tx]] = []

        for i, tx_out in enumerate(block.transactions[0].vout):
            out_point = OutPoint(block.transactions[0].id, i, check_validity=False)
            coin = Coin(tx_out, height, is_coinbase=True)
            self.updated_utxo_set[out_point.serialize(check_validity=False)] = coin
            added.append(out_point)

        for tx in block.transactions[1:]:
            tx_id = tx.id

            prev_coins: list[Coin] = []

            for tx_in in tx.vin:
                prevout_bytes = tx_in.prev_out.serialize(check_validity=False)

                if prevout_bytes in self.removed_utxos:
                    err_msg = "prevout already spent in this batch"
                    raise InvalidBlockInputError(err_msg)
                if prevout_bytes in self.updated_utxo_set:
                    coin = self.updated_utxo_set[prevout_bytes]
                    prev_coins.append(coin)
                    self.updated_utxo_set.pop(prevout_bytes)
                else:
                    prevout_data = self.db.get(b"utxo-" + prevout_bytes)
                    if prevout_data:
                        coin = Coin.parse(prevout_data, check_validity=False)
                        prev_coins.append(coin)
                        self.removed_utxos.add(prevout_bytes)
                    else:
                        err_msg = "prevout not found"
                        raise InvalidBlockInputError(err_msg)

                removed.append((tx_in.prev_out, coin))

            for i, tx_out in enumerate(tx.vout):
                out_point = OutPoint(tx_id, i, check_validity=False)
                self.updated_utxo_set[out_point.serialize(check_validity=False)] = Coin(
                    tx_out, height, is_coinbase=False
                )
                added.append(out_point)

            complete_transactions.append((prev_coins, tx))

        rev_block = RevBlock(hash=block.header.hash, to_add=removed, to_remove=added)

        return complete_transactions, rev_block

    def apply_rev_block(self, rev_block: RevBlock) -> None:
        """Undo `add_block` for the block `rev_block` was returned for.

        Removes every outpoint it created and restores every prevout it
        spent, staged the same way `add_block` stages its own changes.
        """
        for out_point in rev_block.to_remove:
            out_point_bytes = out_point.serialize(check_validity=False)

            if out_point_bytes in self.removed_utxos:
                err_msg = "output already removed"
                raise ChainstateInconsistencyError(err_msg)
            if out_point_bytes in self.updated_utxo_set:
                self.updated_utxo_set.pop(out_point_bytes)
            elif self.db.get(b"utxo-" + out_point_bytes):
                self.removed_utxos.add(out_point_bytes)
            else:
                err_msg = "output not found"
                raise ChainstateInconsistencyError(err_msg)

        for out_point, coin in rev_block.to_add:
            self.updated_utxo_set[out_point.serialize(check_validity=False)] = coin

    def finalize(self, wb: KeyValueStore | None = None) -> None:
        """Write every staged change into `wb`, or into `self.db` if none."""
        db = wb or self.db
        for x in self.removed_utxos:
            db.delete(b"utxo-" + x)
        for out_point_bytes, coin in self.updated_utxo_set.items():
            db.put(b"utxo-" + out_point_bytes, coin.serialize())
        self.removed_utxos = set()
        self.updated_utxo_set = {}

    def rollback(self) -> None:
        """Discard every staged change, mirroring `finalize`."""
        self.removed_utxos = set()
        self.updated_utxo_set = {}
