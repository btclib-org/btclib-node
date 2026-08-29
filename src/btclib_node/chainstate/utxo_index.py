# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`UtxoIndex`, the set of transaction outputs a spend may still reference.

`add_block` applies one block's own spends and creations, returning the
prevouts each transaction consumed -- what `interpreter.check_transactions`
validates against -- and the `block_db.RevBlock` a reorg away from this
block would need to undo it.
"""

from typing import TYPE_CHECKING, Any

from btclib.exceptions import BTClibValueError
from btclib.tx.out_point import OutPoint

from btclib_node.block_db import Coin, RevBlock
from btclib_node.exceptions import ChainstateInconsistencyError, InvalidBlockInputError

if TYPE_CHECKING:
    from btclib.block import Block
    from btclib.tx.tx import Tx

    from btclib_node.db import KeyValueStore
    from btclib_node.log import Logger

__all__ = ["UtxoIndex"]

# What `_undo_log` records in place of a dict entry's own prior value,
# for a key `_set` finds absent -- `None` cannot stand in for this, a
# `Coin` being a legitimate value nothing else ever stores under a key
# and `None` still being a distinct, real "was there" answer for the
# set entries `_add` logs the same way.
_UNSET = object()

# how many entries removed_utxos and updated_utxo_set may hold together
# before should_flush says it is time to write them out. In entries and
# not in bytes: Core's own -dbcache bounds CCoinsViewCache in memory,
# 450 MiB by default (validation.h, DEFAULT_DB_CACHE), but what backs
# these two here is a pair of plain Python dicts, and a dict entry's
# real footprint -- the Coin, the key, the object header, the table
# slot -- is not one a reader can check the way an entry count can be
# counted directly off should_flush's own two len() calls. A wrong
# bound in entries costs memory or flushes; a wrong bound in bytes,
# believed as bytes, costs a reader trusting an estimate nobody can
# verify against the object it is about.
#
# Measured rather than estimated: 500,000 (serialized OutPoint, Coin)
# pairs in a plain dict, built and held while `tracemalloc` traces the
# process, come to about 229 MB -- the same order as Core's own 450 MiB
# default above, and the bound this tree chose costs proportionately
# less because `updated_utxo_set` never holds a whole UTXO set at once,
# only what has connected since the last flush.
#
# 500,000, sized against the block this tree has actually measured
# (btclib-org/btclib-node#586): height 964,000 staged 7,778 deletes and
# 8,100 puts, 15,878 entries, so the bound holds a little over thirty
# blocks that dense before a flush is due -- amortizing each flush's own
# fixed cost (one write_batch, one BlockIndex/FilterIndex write) across
# that many blocks instead of one. A block far earlier in the chain, an
# order of magnitude smaller, holds proportionally more of them staged
# at once, which is the other side of the same bound rather than a
# second one: what it costs on a crash is argued in db.py's docstring.
_FLUSH_BOUND = 500_000


class UtxoIndex:
    """The set of spendable outputs, staged in memory until `finalize`.

    `removed_utxos` and `updated_utxo_set` hold what a batch of
    `add_block`/`apply_rev_block` calls has changed since the last
    `finalize`; the module docstring above is where `add_block`'s own
    return value is argued. The staging now survives more than one
    block -- `should_flush` is what tells a caller it is time to stop
    piling more of it on and write, and `db.py`'s docstring is where
    what a crash before that costs is decided.

    That survival is what makes `rollback` unable to stay a blanket
    wipe: a trial `main.update_chain` rolls back may run against
    staging several *earlier*, already-succeeded blocks left behind,
    unflushed, and wiping the two dicts to empty would discard those
    too -- state a failed trial never touched and has no claim over.
    `_undo_log` is what tells the two apart: every mutation `add_block`
    and `apply_rev_block` make is recorded there as it happens, and
    `rollback(mark)` undoes only what was recorded since `mark`
    (`trial_mark`'s own reading, taken before the trial that might fail
    began), in reverse, leaving anything recorded before it standing.
    """

    def __init__(self, parent_db: KeyValueStore, logger: Logger) -> None:
        """Start with nothing staged, using `parent_db` for reads and writes."""
        self.db = parent_db

        self.removed_utxos: set[bytes] = set()
        self.updated_utxo_set: dict[bytes, Coin] = {}
        self._undo_log: list[tuple[dict[bytes, Any] | set[bytes], bytes, Any]] = []

        self.logger = logger

    def _bip30_violation(self, out_point_bytes: bytes) -> bool:
        """Whether `out_point_bytes` already names a still-unspent coin.

        `_check_bip30` below asks this of every output the block is
        about to create, before staging any of them -- the same "not
        yet mutated" state `add_block`'s own prevout resolution reads,
        so a transaction earlier in this same block being processed can
        never make a later one's check see its own not-yet-applied
        write.

        Checking `removed_utxos` before `updated_utxo_set` is safe only
        because the two are disjoint: no outpoint bytes value is ever
        staged in both at once, because every `_put` call site in this
        module -- `apply_rev_block`'s own `to_add` loop and both of
        `add_block`'s own creation loops -- runs `_unmark_removed` on
        that same key first. A key that reaches `_put` still marked
        removed would otherwise answer `False` here on that account
        alone, before this order ever reaches `updated_utxo_set` or the
        store, hiding a genuine BIP30 duplicate of that prevout rather
        than only the double-spend-guard failure `apply_rev_block`'s own
        docstring names (btclib-org/btclib-node#586).
        """
        if out_point_bytes in self.removed_utxos:
            return False
        return out_point_bytes in self.updated_utxo_set or bool(
            self.db.get(b"utxo-" + out_point_bytes)
        )

    def _check_bip30(self, block: Block) -> None:
        """Refuse `block` if it duplicates a still-unspent output.

        A method of its own rather than a loop inline in `add_block`,
        which ruff's own `complex-structure`/`too-many-branches` already
        count every statement here against -- `add_block`'s own
        docstring is where the check itself, its ordering and its two
        historical exceptions are all argued.
        """
        for tx in block.transactions:
            for i in range(len(tx.vout)):
                out_point_bytes = OutPoint(tx.id, i, check_validity=False).serialize(
                    check_validity=False
                )
                if self._bip30_violation(out_point_bytes):
                    err_msg = "bad-txns-BIP30"
                    raise InvalidBlockInputError(err_msg)

    def trial_mark(self) -> int:
        """Return a point in the undo log a failed trial can be rolled back to.

        Taken before `add_block`/`apply_rev_block` are ever called for
        that trial; `rollback` undoes back to exactly this point,
        `main.update_chain`'s own docstring above being the one caller.
        """
        return len(self._undo_log)

    def _put(self, out_point_bytes: bytes, coin: Coin) -> None:
        """`updated_utxo_set[out_point_bytes] = coin`, logged for `rollback`."""
        self._undo_log.append(
            (
                self.updated_utxo_set,
                out_point_bytes,
                self.updated_utxo_set.get(out_point_bytes, _UNSET),
            )
        )
        self.updated_utxo_set[out_point_bytes] = coin

    def _pop(self, out_point_bytes: bytes) -> Coin:
        """`updated_utxo_set.pop(out_point_bytes)`, logged for `rollback`."""
        coin = self.updated_utxo_set.pop(out_point_bytes)
        self._undo_log.append((self.updated_utxo_set, out_point_bytes, coin))
        return coin

    def _mark_removed(self, out_point_bytes: bytes) -> None:
        """`removed_utxos.add(out_point_bytes)`, logged for `rollback`."""
        self._undo_log.append(
            (self.removed_utxos, out_point_bytes, out_point_bytes in self.removed_utxos)
        )
        self.removed_utxos.add(out_point_bytes)

    def _unmark_removed(self, out_point_bytes: bytes) -> None:
        """`removed_utxos.discard(out_point_bytes)`, logged for `rollback`.

        `apply_rev_block` below is the one caller: restoring a prevout a
        block spent has to undo whichever of `_pop` or `_mark_removed`
        `add_block` used to stage that spend, and only `_mark_removed`
        touches `removed_utxos` -- so this runs unconditionally, the same
        way `_mark_removed` itself logs unconditionally, and is a no-op
        precisely when the spend it undoes never reached `removed_utxos`
        in the first place.
        """
        self._undo_log.append(
            (self.removed_utxos, out_point_bytes, out_point_bytes in self.removed_utxos)
        )
        self.removed_utxos.discard(out_point_bytes)

    def should_flush(self) -> bool:
        """Whether the staged size has reached `_FLUSH_BOUND`.

        `main._finalize_fork` is the one caller, and it is not asking
        this alone: `Chainstate.flush` writes `BlockIndex` and
        `FilterIndex` in the same batch this triggers, which is what
        keeps the three in step (`db.py`'s own docstring argues why).
        """
        return len(self.removed_utxos) + len(self.updated_utxo_set) >= _FLUSH_BOUND

    def add_block(
        self, block: Block, height: int, *, check_bip30: bool = True
    ) -> tuple[list[tuple[list[Coin], Tx]], RevBlock]:
        """Apply `block`'s own spends and creations, staged rather than written.

        `height` is this block's own height, on whichever branch it is
        being tried -- what every output it creates is stamped with, coin
        and coinbase alike, and never the height a later reorg
        disconnects or reconnects it at: `apply_rev_block` below restores
        a `Coin` exactly as this call staged it for removal, height and
        coinbase bit included, rather than recomputing either.

        `check_bip30` refuses a block that "overwrites" an output still
        unspent from anywhere earlier on the chain -- Core's own
        `bad-txns-BIP30` (`ConnectBlock`, `src/validation.cpp:2401-2431`,
        at bitcoin/bitcoin@204256c73f), CVE-2012-1909's shape: without
        it, a coinbase sharing an already-mined, still-unspent txid
        overwrites that output in place, and a reorg away from the
        second block deletes an output the first block's own branch
        still carries. Checked over every transaction the block carries,
        coinbase included, matching Core's own loop -- and before either
        of the two loops below stages a single write, since Core's own
        check runs against the view exactly as it stood before this
        block, coinbase and ordinary spends alike. `False` only for the
        two 2010 blocks `Chain.bip30_exceptions` names, which predate
        BIP34 (btclib-org/btclib-node#571) and so predate the property
        that makes a new violation of this kind unreachable once BIP34
        binds: a block's own coinbase commits to its own real height,
        which two different heights can never share, so the outpoint a
        block's own coinbase creates cannot already belong to an earlier
        block's coinbase -- and `UtxoIndex.add_block` un-stages an
        entire block atomically on any raise, this one included, so a
        refused duplicate never reaches the two loops below that would
        otherwise stage a write over it.

        Both loops below call `_unmark_removed` on an outpoint's own
        bytes before every `_put` of it, the same order
        `apply_rev_block`'s own `to_add` loop uses to restore one: a key
        this call creates can coincide with one `removed_utxos` still
        carries only when the two share a txid, `check_bip30` above
        being what refuses that for every case but the two historical
        exceptions -- so the unmark is a no-op everywhere else, and is
        what keeps `removed_utxos` and `updated_utxo_set` disjoint
        rather than leaving a recreated outpoint staged in both at once
        (`_bip30_violation`'s own docstring is where that invariant is
        used, and btclib-org/btclib-node#586 is where staging it in
        both broke a later `apply_rev_block` on a coin that was
        legitimately unspent).

        Returns each non-coinbase transaction paired with the prevouts
        its own inputs consumed -- what `interpreter.check_transactions`
        validates against -- and the `RevBlock` that undoes this call.
        """
        if check_bip30:
            self._check_bip30(block)

        removed: list[tuple[OutPoint, Coin]] = []
        added: list[OutPoint] = []
        complete_transactions: list[tuple[list[Coin], Tx]] = []

        for i, tx_out in enumerate(block.transactions[0].vout):
            out_point = OutPoint(block.transactions[0].id, i, check_validity=False)
            coin = Coin(tx_out, height, is_coinbase=True)
            out_point_bytes = out_point.serialize(check_validity=False)
            self._unmark_removed(out_point_bytes)
            self._put(out_point_bytes, coin)
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
                    coin = self._pop(prevout_bytes)
                    prev_coins.append(coin)
                else:
                    prevout_data = self.db.get(b"utxo-" + prevout_bytes)
                    if prevout_data:
                        # prevout_data is present under a key only this
                        # node's own finalize() ever writes -- the
                        # candidate block never supplied these bytes, so
                        # a Coin.parse this raises on is this node's own
                        # stored record being corrupted, not the block's
                        # content, unlike every other raise in this loop
                        # (btclib-org/btclib-node#620). Raising where
                        # CDBWrapper::Read/CCoinsViewDB::GetCoin answer
                        # absent is not a departure from Core: LevelDB's
                        # own checksum makes corruption fatal before
                        # that deserialize is ever reached, so absent is
                        # Core's answer to a format mismatch on an
                        # intact record and never to this one. What
                        # differs between the two trees is where the
                        # fault is caught, not what either chose;
                        # ChainstateInconsistencyError's own docstring
                        # argues it (btclib-org/btclib-node#636)
                        try:
                            coin = Coin.parse(prevout_data, check_validity=False)
                        except BTClibValueError as exc:
                            err_msg = "stored utxo- record failed to parse"
                            raise ChainstateInconsistencyError(err_msg) from exc
                        prev_coins.append(coin)
                        self._mark_removed(prevout_bytes)
                    else:
                        err_msg = "prevout not found"
                        raise InvalidBlockInputError(err_msg)

                removed.append((tx_in.prev_out, coin))

            for i, tx_out in enumerate(tx.vout):
                out_point = OutPoint(tx_id, i, check_validity=False)
                out_point_bytes = out_point.serialize(check_validity=False)
                self._unmark_removed(out_point_bytes)
                self._put(out_point_bytes, Coin(tx_out, height, is_coinbase=False))
                added.append(out_point)

            complete_transactions.append((prev_coins, tx))

        rev_block = RevBlock(hash=block.header.hash, to_add=removed, to_remove=added)

        return complete_transactions, rev_block

    def apply_rev_block(self, rev_block: RevBlock) -> None:
        """Undo `add_block` for the block `rev_block` was returned for.

        Removes every outpoint it created and restores every prevout it
        spent, staged the same way `add_block` stages its own changes.

        `to_add` runs before `to_remove`, not the reverse order
        `add_block` itself builds the two lists in, because an ordinary
        chained transaction -- one spending an output another
        transaction earlier in the *same* block created -- puts that
        output's outpoint in both: `to_remove` from being created,
        `to_add` from being spent before this block ever finalized it
        to disk. Popping it in `to_remove` first would look it up while
        it is in neither `updated_utxo_set` nor the database -- its net
        effect on the persisted set is nothing, both before this block
        and after it -- and raise `"output not found"` on a block that
        did nothing wrong. Restoring it in `to_add` first stages it
        back into `updated_utxo_set`, where `to_remove`'s own `_pop`
        then finds and removes it, netting to the same nothing
        `add_block` itself computed. Every other entry is unaffected by
        the order: `to_add`'s outpoints predate this block and never
        collide with `to_remove`'s own, which this block alone created,
        a valid block spending a given outpoint at most once. Core's
        `DisconnectBlock` (`src/validation.cpp`, at
        bitcoin/bitcoin@05e49b342f) reaches the same result walking one
        transaction at a time in reverse block order -- spend its own
        outputs, then restore its own inputs -- rather than in the two
        flat passes here (btclib-org/btclib-node#634).

        A restored prevout is unmarked from `removed_utxos` before it is
        put back, not merely put back: `add_block` staged that spend
        with `_mark_removed` whenever the prevout was already durable
        (found in `self.db` rather than in `updated_utxo_set`), and
        leaving that flag set here would put the same outpoint bytes in
        both `removed_utxos` and `updated_utxo_set` at once -- still
        "removed" as far as a later `add_block` call's own guard is
        concerned, even though this call just made it spendable again.
        Staging now survives across trials (this outpoint can sit
        restored for up to `_FLUSH_BOUND` entries' worth of blocks
        before `finalize` clears both dicts), so a stale flag here is no
        longer erased by the next trial boundary the way the old,
        per-trial `finalize` used to erase it -- it stays wrong until a
        legitimate later spend of the same output hits `add_block`'s
        `"prevout already spent in this batch"` guard and gets rejected
        as a double spend, invalidating that block and, through
        `update_header_index` -> `BlockIndex.invalidate`, everything
        built on it. Independently of that, the same stale flag hides a
        genuine BIP30 duplicate too: `_bip30_violation` reads
        `removed_utxos` first and answers "no violation" on a hit, so a
        block recreating the restored outpoint -- still unspent once
        this call has put it back -- would connect instead of being
        refused `bad-txns-BIP30` (btclib-org/btclib-node#586).
        """
        for out_point, coin in rev_block.to_add:
            out_point_bytes = out_point.serialize(check_validity=False)
            self._unmark_removed(out_point_bytes)
            self._put(out_point_bytes, coin)

        for out_point in rev_block.to_remove:
            out_point_bytes = out_point.serialize(check_validity=False)

            if out_point_bytes in self.removed_utxos:
                err_msg = "output already removed"
                raise ChainstateInconsistencyError(err_msg)
            if out_point_bytes in self.updated_utxo_set:
                self._pop(out_point_bytes)
            elif self.db.get(b"utxo-" + out_point_bytes):
                self._mark_removed(out_point_bytes)
            else:
                err_msg = "output not found"
                raise ChainstateInconsistencyError(err_msg)

    def get_coin(self, prevout_bytes: bytes) -> Coin | None:
        """Return the `Coin` a serialized outpoint still resolves to, or `None`.

        Checks `updated_utxo_set` and `removed_utxos` first, the same
        order `add_block` and `apply_rev_block` already read staged
        state in, before falling to the store: a coin several blocks'
        own worth of staging have created or already taken is real
        regardless of whether `finalize` has written it out yet, and a
        caller reading `self.db` directly -- `main.verify_mempool_acceptance`
        used to -- would miss exactly what staying staged across more
        than one block (btclib-org/btclib-node#586) makes ordinary.
        """
        if prevout_bytes in self.updated_utxo_set:
            return self.updated_utxo_set[prevout_bytes]
        if prevout_bytes in self.removed_utxos:
            return None
        coin_data = self.db.get(b"utxo-" + prevout_bytes)
        if coin_data is None:
            return None
        # coin_data is present under a key only this node's own finalize
        # ever writes -- no caller here supplied these bytes, so a
        # Coin.parse failure is this node's own stored record being
        # corrupted, not a candidate block's or a mempool transaction's
        # content, the same distinction UtxoIndex.add_block's own read
        # of the same kind of record draws (btclib-org/btclib-node#620,
        # btclib-org/btclib-node#631). Raising where
        # CDBWrapper::Read/CCoinsViewDB::GetCoin answer absent is not a
        # departure from Core: LevelDB's own checksum makes corruption
        # fatal before that deserialize is ever reached, so absent is
        # Core's answer to a format mismatch on an intact record and
        # never to this one. What differs between the two trees is
        # where the fault is caught, not what either chose;
        # ChainstateInconsistencyError's own docstring argues it
        # (btclib-org/btclib-node#636)
        try:
            return Coin.parse(coin_data, check_validity=False)
        except BTClibValueError as exc:
            err_msg = "stored utxo- record failed to parse"
            raise ChainstateInconsistencyError(err_msg) from exc

    def finalize(self, wb: KeyValueStore | None = None) -> None:
        """Write every staged change into `wb`, or into `self.db` if none.

        Everything staged is durable after this, so nothing recorded
        before it can ever be rolled back to: `_undo_log` is cleared
        along with the two dicts it was tracking.
        """
        db = wb or self.db
        for x in self.removed_utxos:
            db.delete(b"utxo-" + x)
        for out_point_bytes, coin in self.updated_utxo_set.items():
            db.put(b"utxo-" + out_point_bytes, coin.serialize())
        self.removed_utxos = set()
        self.updated_utxo_set = {}
        self._undo_log = []

    def rollback(self, mark: int = 0) -> None:
        """Undo every mutation recorded since `mark`, in reverse.

        `mark` defaults to the very start -- a caller with nothing
        staged before its own trial began, which is every direct test
        of this method -- and `trial_mark`'s own docstring is where a
        caller with something to protect gets a real one from.
        """
        while len(self._undo_log) > mark:
            container, key, prior = self._undo_log.pop()
            if isinstance(container, set):
                if prior:
                    container.add(key)
                else:
                    container.discard(key)
            elif prior is _UNSET:
                container.pop(key, None)
            else:
                container[key] = prior
