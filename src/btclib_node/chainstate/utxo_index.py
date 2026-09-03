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
from btclib_node.chainstate.muhash import (
    CoinStats,
    is_bip30_unspendable,
    is_unspendable,
)
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

# UtxoIndex's own key into KeyValueStore's meta column family
# (db.py's own get_meta/put_meta): CoinStats.serialize's own bytes,
# restored on __init__ and written by finalize in the same write_batch
# as the coins it commits to (db.py's docstring argues why).
_COIN_STATS_META_KEY = b"coinstats"

# `_undo_log`'s own entry shape is one of two, discriminated by the
# first element: a dict/set mutation, whose second and third elements
# are the key and the prior value `_put`/`_pop`/`_mark_removed`/
# `_unmark_removed` above already carry; or a `coin_stats` mutation,
# whose second element is `True` for an insert and `False` for a
# remove (`rollback` below reads it that way) and whose third is the
# `(out_point_bytes, coin)` pair `_hash_insert`/`_hash_remove` replay
# the opposite call with -- not a prior value, `coin_stats` needing
# none (the class docstring's own paragraph on it argues why).
_DictOrSetUndoEntry = tuple[dict[bytes, Any] | set[bytes], bytes, Any]
_CoinStatsUndoEntry = tuple[CoinStats, bool, tuple[bytes, Coin]]
_UndoEntry = _DictOrSetUndoEntry | _CoinStatsUndoEntry


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

    `coin_stats` is the running commitment to the same set --
    `chainstate/muhash.py`'s own `CoinStats`, restored from
    `parent_db`'s meta column family here and staged the same way the
    two dicts above are: every `_hash_insert`/`_hash_remove` this class
    makes is logged into the same `_undo_log`, `CoinStats.insert` and
    `.remove` being each other's exact undo regardless of order
    (`muhash.py`'s own docstring argues why), so `rollback` needs no
    prior accumulator state recorded, only that the opposite call
    replays.
    """

    def __init__(self, parent_db: KeyValueStore, logger: Logger) -> None:
        """Start with nothing staged, using `parent_db` for reads and writes."""
        self.db = parent_db

        self.removed_utxos: set[bytes] = set()
        self.updated_utxo_set: dict[bytes, Coin] = {}
        self._undo_log: list[_UndoEntry] = []

        stored_stats = parent_db.get_meta(_COIN_STATS_META_KEY)
        self.coin_stats = (
            CoinStats.deserialize(stored_stats)
            if stored_stats is not None
            else CoinStats()
        )

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

    def _stored_prevout(self, prevout_bytes: bytes) -> Coin | None:
        """Return the `Coin` a stored `utxo-` record still resolves to.

        `None` both for no such key and for a key whose bytes
        `Coin.parse` cannot read: a `utxo-` key is one only this
        node's own `finalize` ever writes, so a parse failure here is
        over this node's own stored bytes, never over a caller's
        content -- but RocksDB's own per-block checksum
        (btclib-org/btclib-node#641) has already turned a genuinely
        corrupted record into `StoreCorruptionError` before this call
        is ever reached, so what a parse failure means here is a
        checksum-clean record that still does not deserialize:
        exactly the fault `CDBWrapper::Read`/`CCoinsViewDB::GetCoin`
        answer absent to (`src/dbwrapper.h`, `src/txdb.cpp`, at
        bitcoin/bitcoin@ca7162cde5). Matching that answer with `None`
        rather than raising `ChainstateInconsistencyError` is
        btclib-org/btclib-node#650's own decision, argued in that
        class's own docstring; `add_block` and `get_coin`, this
        method's own two callers, are where it used to be raised.
        """
        prevout_data = self.db.get(b"utxo-" + prevout_bytes)
        if prevout_data is None:
            return None
        try:
            return Coin.parse(prevout_data, check_validity=False)
        except BTClibValueError:
            return None

    def _hash_insert(self, out_point_bytes: bytes, coin: Coin) -> None:
        """`coin_stats.insert(out_point_bytes, coin)`, logged for `rollback`.

        The class docstring's own paragraph on `coin_stats` is where the
        undo -- `_hash_remove` below, replayed rather than a prior value
        restored -- is argued.
        """
        self.coin_stats.insert(out_point_bytes, coin)
        self._undo_log.append((self.coin_stats, True, (out_point_bytes, coin)))

    def _hash_remove(self, out_point_bytes: bytes, coin: Coin) -> None:
        """`coin_stats.remove(out_point_bytes, coin)`, logged for `rollback`."""
        self.coin_stats.remove(out_point_bytes, coin)
        self._undo_log.append((self.coin_stats, False, (out_point_bytes, coin)))

    def _stage_creation(
        self, out_point_bytes: bytes, coin: Coin, *, hash_it: bool
    ) -> bool:
        """Unmark, put, and usually hash in, one created output.

        Returns whether the output was staged at all: `False`, a
        no-op, for a provably unspendable one (`is_unspendable`) --
        matching `CCoinsViewCache::AddCoin` (`src/coins.cpp:82`, at
        bitcoin/bitcoin@ca7162cde5), which returns without adding such
        an output to Core's own UTXO set in the first place
        (`muhash.py`'s own "What is inserted, and what is not"
        argues why the digest already agreed with Core on this before
        the store did, btclib-org/btclib-node#667). `add_block`'s own
        two creation loops use the return value to keep such an
        outpoint out of `added` too, so `apply_rev_block` is never
        asked to undo a creation this call never staged -- an
        unspendable output that is never stored is never restored.

        `add_block`'s own two creation loops share this rather than
        each repeating `_unmark_removed` + `_put` + a conditional
        `_hash_insert` inline -- what ruff's own `complex-structure`
        already counts every branch of `add_block` itself against.
        `hash_it=False` only ever for the coinbase loop's own
        `is_bip30_unspendable` gate, and moot for an unspendable
        output either way, this method returning before that check is
        ever reached.
        """
        if is_unspendable(coin.tx_out.script_pub_key.script):
            return False
        self._unmark_removed(out_point_bytes)
        self._put(out_point_bytes, coin)
        if hash_it:
            self._hash_insert(out_point_bytes, coin)
        return True

    def _stage_added(
        self, added: list[OutPoint], out_point: OutPoint, coin: Coin, *, hash_it: bool
    ) -> None:
        """Stage a creation and record it in `added` where it was stored.

        `add_block`'s own two creation loops share this the same way
        they already share `_stage_creation` above: an inline `if`
        around each call site is exactly the extra branch ruff's own
        `complex-structure` counts against `add_block` itself, at any
        layout the check were written in instead.
        """
        out_point_bytes = out_point.serialize(check_validity=False)
        if self._stage_creation(out_point_bytes, coin, hash_it=hash_it):
            added.append(out_point)

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
        two 2010 blocks `Chain.consensus.bip30_exceptions` names, which
        predate BIP34 (btclib-org/btclib-node#571) and so predate the property
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

        Every output this stages and every prevout it spends also moves
        `coin_stats`, the running commitment to the set -- inserted for
        a creation, removed for a spend, `Coin.parse`'s own two raises
        above unaffected since they run before either ever touches it.
        The coinbase creation loop's own `_hash_insert` is skipped for
        the two mainnet blocks `is_bip30_unspendable` names, matching
        `CoinStatsIndex::CustomAppend` -- `muhash.py`'s own "The two
        blocks history exempts" argues why, and why this is a different
        pair from `check_bip30`'s own exception above.

        Neither creation loop stages a provably unspendable output at
        all -- `_stage_creation`'s own docstring is where that gate,
        and why it is what keeps `added` (and so `RevBlock.to_remove`)
        from ever naming an outpoint this call never wrote, is argued
        (btclib-org/btclib-node#667). A block's own spend loop below
        needs no matching gate: no valid witness satisfies a provably
        unspendable output's own script, so no block this method is
        ever asked to connect legitimately spends one, and one that
        tried would find it absent -- `"prevout not found"`, the same
        answer Core's own `ConnectBlock` reaches through
        `Consensus::CheckTxInputs`/`HaveInputs` for an output its own
        `AddCoin` never added either.
        """
        if check_bip30:
            self._check_bip30(block)

        removed: list[tuple[OutPoint, Coin]] = []
        added: list[OutPoint] = []
        complete_transactions: list[tuple[list[Coin], Tx]] = []
        block_hash = block.header.hash
        skip_coinbase_hash = is_bip30_unspendable(height, block_hash)

        for i, tx_out in enumerate(block.transactions[0].vout):
            out_point = OutPoint(block.transactions[0].id, i, check_validity=False)
            coin = Coin(tx_out, height, is_coinbase=True)
            self._stage_added(added, out_point, coin, hash_it=not skip_coinbase_hash)

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
                    # _stored_prevout's own docstring is where
                    # answering "prevout not found" for a
                    # checksum-clean record Coin.parse still cannot
                    # read is argued (btclib-org/btclib-node#650).
                    resolved = self._stored_prevout(prevout_bytes)
                    if resolved is None:
                        err_msg = "prevout not found"
                        raise InvalidBlockInputError(err_msg)
                    coin = resolved
                    prev_coins.append(coin)
                    self._mark_removed(prevout_bytes)

                removed.append((tx_in.prev_out, coin))
                self._hash_remove(prevout_bytes, coin)

            for i, tx_out in enumerate(tx.vout):
                out_point = OutPoint(tx_id, i, check_validity=False)
                created_coin = Coin(tx_out, height, is_coinbase=False)
                self._stage_added(added, out_point, created_coin, hash_it=True)

            complete_transactions.append((prev_coins, tx))

        rev_block = RevBlock(hash=block_hash, to_add=removed, to_remove=added)

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

        `coin_stats` moves the opposite way `add_block` moved it for
        the same coin: `to_add` restores a prevout that block spent, so
        it is inserted back into the commitment; `to_remove` drops an
        output that block created, so it is removed from it -- both
        skipped for a coin that is both `coin.is_coinbase` and named by
        `is_bip30_unspendable`, matching `add_block`'s own gate, which
        withholds only the coinbase creation loop
        (`skip_coinbase_hash`, `add_block`'s own docstring) and never an
        ordinary transaction sharing the same block. `is_coinbase`
        alone is what `is_bip30_unspendable` cannot answer for: it
        checks only the coin's own height and the block's hash, so
        without this condition every non-coinbase output *of the exempt
        block itself* -- stamped with the same height by `add_block` --
        would be wrongly withheld here on undo despite having been
        correctly hashed in on the way in, leaving `coin_stats`
        permanently off after a reorg through that block. Matches
        `CoinStatsIndex::CustomAppend`'s own `is_coinbase &&
        IsBIP30Unspendable(...)` (`src/index/coinstatsindex.cpp:129`,
        at bitcoin/bitcoin@ca7162cde5), which gates on exactly the same
        conjunction rather than the hash/height pair alone.
        `to_remove` parses the stored `Coin` for this alone where the
        rest of this loop only ever needed to know the record existed.
        A parse failure here raises `ChainstateInconsistencyError`,
        unlike the identical fault reached through `_stored_prevout`
        (`add_block`'s own prevout resolution, `get_coin`), which
        answers `None` instead (btclib-org/btclib-node#650,
        `_stored_prevout`'s own docstring). The two share only the
        *attribution* -- that the fault is this node's own corrupted
        record, never a caller's content (btclib-org/btclib-node#620,
        btclib-org/btclib-node#631, btclib-org/btclib-node#636) -- not
        the *outcome*: undoing a block this node already connected has
        no legitimate "not found" reading the way an ordinary prevout
        lookup does, this loop's own `"output not found"` raise
        immediately above already treating absence itself as this
        node's own bookkeeping fault rather than a candidate's.
        """
        for out_point, coin in rev_block.to_add:
            out_point_bytes = out_point.serialize(check_validity=False)
            self._unmark_removed(out_point_bytes)
            self._put(out_point_bytes, coin)
            if not (
                coin.is_coinbase and is_bip30_unspendable(coin.height, rev_block.hash)
            ):
                self._hash_insert(out_point_bytes, coin)

        for out_point in rev_block.to_remove:
            out_point_bytes = out_point.serialize(check_validity=False)

            if out_point_bytes in self.removed_utxos:
                err_msg = "output already removed"
                raise ChainstateInconsistencyError(err_msg)
            if out_point_bytes in self.updated_utxo_set:
                coin = self._pop(out_point_bytes)
            else:
                coin_data = self.db.get(b"utxo-" + out_point_bytes)
                if not coin_data:
                    err_msg = "output not found"
                    raise ChainstateInconsistencyError(err_msg)
                try:
                    coin = Coin.parse(coin_data, check_validity=False)
                except BTClibValueError as exc:
                    err_msg = "stored utxo- record failed to parse"
                    raise ChainstateInconsistencyError(err_msg) from exc
                self._mark_removed(out_point_bytes)
            if not (
                coin.is_coinbase and is_bip30_unspendable(coin.height, rev_block.hash)
            ):
                self._hash_remove(out_point_bytes, coin)

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
        # _stored_prevout's own docstring is where answering `None`
        # for a checksum-clean record `Coin.parse` still cannot read
        # is argued (btclib-org/btclib-node#650).
        return self._stored_prevout(prevout_bytes)

    def finalize(self, wb: KeyValueStore | None = None) -> None:
        """Write every staged change into `wb`, or into `self.db` if none.

        Everything staged is durable after this, so nothing recorded
        before it can ever be rolled back to: `_undo_log` is cleared
        along with the two dicts it was tracking. `coin_stats` is
        written alongside them, into the same store's meta column
        family (`db.py`'s own `put_meta`) -- inside `wb`'s own batch
        when a caller passes one, which is what keeps the commitment
        and the coins it commits to landing together or not at all
        (`db.py`'s docstring argues why).
        """
        db = wb or self.db
        for x in self.removed_utxos:
            db.delete(b"utxo-" + x)
        for out_point_bytes, coin in self.updated_utxo_set.items():
            db.put(b"utxo-" + out_point_bytes, coin.serialize())
        db.put_meta(_COIN_STATS_META_KEY, self.coin_stats.serialize())
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
            entry = self._undo_log.pop()
            if isinstance(entry[0], CoinStats):
                _, was_insert, (out_point_bytes, coin) = entry
                if was_insert:
                    # this entry logged a _hash_insert, so undoing it
                    # removes the same (out_point_bytes, coin) pair --
                    # muhash.py's own docstring is where insert and
                    # remove being each other's exact inverse,
                    # regardless of order, is argued
                    self.coin_stats.remove(out_point_bytes, coin)
                else:
                    self.coin_stats.insert(out_point_bytes, coin)
                continue
            container, key, prior = entry
            if isinstance(container, set):
                if prior:
                    container.add(key)
                else:
                    container.discard(key)
            elif prior is _UNSET:
                container.pop(key, None)
            else:
                container[key] = prior
