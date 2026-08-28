# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The ordered key-value store every index of this node is kept in.

Read a key, write one, delete one, write several as one, walk the whole
store in key order, and close: that is everything any index here asks
of it. They are behind one class so that what implements them is one
decision in one file -- `src/btclib_node/db.py` -- rather than a library
named in as many modules as import it.

The implementation is SQLite, and the reason is not speed. It is the
standard library, so there is no wheel to be missing on a platform, no
compiler to be present, and no stubs to be absent; the three of those
are what LevelDB through `plyvel` cost here, measured in
btclib-org/btclib-node#107. A table of `BLOB PRIMARY KEY` declared
`WITHOUT ROWID` *is* a B-tree on the key, so the ordered walk below is
the table's own order and not a sort.

**Key order is load-bearing.** `BlockIndex.init_from_db` reads until
the first key that is not a `blkinfo-`, so a prefix that sorted before
that one would stop it early; `BlockDB.init_from_db` walks the whole
store and dispatches on the prefix, which is slower and cannot be
tripped that way.

Bitcoin Core keeps the same data in LevelDB -- `src/leveldb/`, vendored
into its own tree and wrapped by `CDBWrapper` -- which is the strongest
argument for the other choice and is answered in that issue: Core
compiles its own, so the packaging problem this store exists to remove
is not one Core has.

**`_SCHEMA_VERSION` guards the shape of what is inside the table, not
the table itself.** SQLite's own `PRAGMA user_version` -- four bytes in
the file header, untouched by anything this class writes into `kv` --
is where it is kept, rather than a row: a row would sit inside the very
key order the paragraph above depends on, and `BlockIndex.init_from_db`
stopping at the first key that is not a `blkinfo-` is exactly the kind
of reader a stray version key could confuse. A fresh `user_version` of
`0` is not on its own evidence of anything -- SQLite starts every new
file there -- so what tells an empty store from one written before
this class carried a version at all is whether `kv` already holds a
row: empty, `__init__` stamps `_SCHEMA_VERSION` in; non-empty, it is a
store this class predates, refused the same way the LevelDB marker
above is. btclib-org/btclib-node#569 is the first thing this had to
guard: the coin record `chainstate/utxo_index.py` keeps under a
`utxo-` key, and the `RevBlock` `block_db/__init__.py` keeps in a
`.rev` file, both changed shape there, and neither a `TxOut.parse` nor
a `RevBlock.deserialize` written for the new shape says why it fails
against the old one.

**A crash before `Chainstate.flush` writes costs whatever is staged
since the last one, and never a torn store.** `main._finalize_fork` no
longer writes `BlockIndex`'s and `FilterIndex`'s own changes on every
connected block: `BlockIndex.stage_status` and `FilterIndex`'s own
`pending` (`filter_index.py`'s docstring) hold them the way `UtxoIndex`
already held its own spends and creations, and `Chainstate.flush` writes
all three into the one `write_batch` this store already gives a caller
-- one SQLite transaction, committed whole or not at all (`write_batch`'s
own docstring below) -- once `UtxoIndex.should_flush` says the staged
UTXO cache has reached its own bound (`utxo_index.py`'s `_FLUSH_BOUND`),
or once `Chainstate.close` is called. This is what
btclib-org/btclib-node#586 measured: 3.53 billion inputs, one `db.get`
and one `db.delete` apiece, and holding several blocks' own changes
staged rather than committing each block's alone is the only lever on
that, blocks connecting one at a time on this store's own single writer.

So a block whose own status change never reached this store is, after an
unclean stop, still whatever `init_from_db` last read for it --
`valid_header` rather than `in_active_chain` -- with nothing else on
disk recording that it was ever tried. `generate_active_chain` and
`generate_block_candidates` (`chainstate/block_index.py`) then rebuild
`active_chain` and `block_candidates` without it, exactly as they would
for a block this store has never seen; `get_first_candidate` offers it
again, and `update_chain` (`main.py`) revalidates it in full,
`check_transactions` included, and re-stages the identical `Coin`s and
the identical filter, both being pure functions of the block and its
ancestry. Nothing is corrupted by this -- what the last flush wrote is
self-consistent, being one transaction, and everything after it is
redone rather than read back. A clean stop costs none of it:
`Chainstate.close` calls `flush` first, so the cost above is what a kill
or a crash that never reaches `close` leaves for the next start-up,
bounded by `_FLUSH_BOUND` and paid in `Node.worker_pool`'s own parallel
validation rather than in this store's single-threaded writes -- the
resource #586 is about spending less of.

Bitcoin Core pays a narrower version of the same cost differently,
because its own equivalent of these three indexes is not one store. Its
block index (`CBlockTreeDB`) and its coins cache (`CCoinsViewDB`) are two
separate LevelDB instances, and `Chainstate::FlushStateToDisk`
(`validation.cpp`, read at bitcoin/bitcoin@05e49b342f) writes them in
sequence rather than together -- `WriteBlockIndexDB()` first,
`CoinsTip().Flush()`/`.Sync()` second, both gated by the same
`should_write` -- so blocks connected since the last such flush are lost
to a crash exactly the way this store loses them, and Core redoes them
the same way, through the ordinary `ActivateBestChain` path rather than
through anything below. What is narrower is the coins write itself:
`CCoinsViewDB::BatchWrite` (`txdb.cpp`) splits a large flush into several
separately-committed LevelDB batches capped by `-dbbatchsize`, so a
crash *during* that one flush -- after the block index's own write
already landed, mid-way through the coins side of it -- leaves the two
out of step by less than a whole flush interval. `DB_HEAD_BLOCKS`, a
marker `BatchWrite` writes before its first batch and erases after its
last, is what records that transition is in progress;
`Chainstate::ReplayBlocks` reads it at start-up and, finding it set,
re-applies (`RollforwardBlock`) every block between the old coins tip
and the new one directly onto the coins cache, no script re-verification,
because the block index -- already durable when this coins write began
-- had already recorded them as validated.

This store never needs that: `write_batch` is one SQLite transaction
regardless of how many keys it touches, so there is no sequence of
several separately-committed writes inside one flush for a crash to land
between, and so no marker to record one in progress. Bounding by entries
rather than by write-batch bytes (`utxo_index.py` argues that choice) is
part of the same shape -- one flush, one commit, the whole of it or
none.
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from btclib_node.exceptions import IncompatibleStoreError, StoreClosedError

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["KeyValueStore"]

# What a LevelDB directory always holds, and this one never will. A
# datadir written before this store existed cannot be read by it, and
# starting an empty chain over the top of one is a worse answer than
# saying so.
_LEVELDB_MARKER = "CURRENT"

_SCHEMA = "CREATE TABLE IF NOT EXISTS kv (k BLOB PRIMARY KEY, v BLOB NOT NULL)"
# the point of the table, and not a detail: without it SQLite keeps a
# rowid table with a separate index, and the key order below becomes a
# lookup per row instead of the order the table is already in
_SCHEMA += " WITHOUT ROWID"

# Bumped whenever a caller changes the shape of what it keeps under a
# key, or the shape a file this class does not itself hold (block_db's
# own .blk/.rev files) is read against. The module docstring above is
# where checking it against PRAGMA user_version, rather than a row, is
# argued.
_SCHEMA_VERSION = 1


class KeyValueStore:
    """An ordered store of octets by octets, in one file.

    One connection, and a lock around every use of it. SQLite's own
    thread checking is off because the node opens the store in the
    thread that builds it and uses it in the thread that runs it, and
    the tests write from both -- and once that check is off, serializing
    is the caller's job. Doing it here rather than per thread is what
    makes `close` safe: a connection per thread meant closing one that
    another thread was inside, which CPython's sqlite3 answers with a
    segmentation fault, not an exception. What one connection gives up
    is two transactions at once, which nothing here asks for.

    Reentrant, because a batch holds the lock for its whole block and
    the writes inside it come back through the same door.
    """

    def __init__(self, path: str | Path) -> None:
        """Open (or create) the store at `path`, refusing a LevelDB one."""
        self.path = Path(path)
        self.path.mkdir(exist_ok=True, parents=True)
        if (self.path / _LEVELDB_MARKER).exists():
            err_msg = f"{self.path} holds a LevelDB database, which this "
            err_msg += "version cannot read: delete the directory and sync "
            err_msg += "again"
            raise IncompatibleStoreError(err_msg)
        self.file = self.path / "index.sqlite"

        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.file, isolation_level=None, check_same_thread=False
        )
        # WAL, so that the writer does not hold the file against a
        # reader in another process. NORMAL is what LevelDB gives by
        # default too -- a write is not fsynced, and a kill loses the
        # last transactions rather than corrupting the store.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute(_SCHEMA)
        self._check_schema_version()

    def _check_schema_version(self) -> None:
        """Refuse a store this version's own shape cannot make sense of.

        `PRAGMA user_version` answers `0` for a file SQLite has never
        been asked to stamp, which is both a brand-new store and one
        written before this existed -- `kv` already holding a row is
        what tells the two apart, the module docstring's own argument
        for keeping the version out of `kv` in the first place.

        A refusal closes the connection it just opened before raising:
        `__init__` never hands this object back to whoever asked for
        one, so nothing else is left holding a reference to close it,
        and an open, unclosed connection is what a bare raise here
        would leave for the garbage collector to find on its own time
        -- CPython's own sqlite3 warns rather than closing quietly when
        that happens.
        """
        ((version,),) = self._rows("PRAGMA user_version")
        if version == _SCHEMA_VERSION:
            return
        if version == 0 and not self._rows("SELECT 1 FROM kv LIMIT 1"):
            self._run(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            return
        err_msg = f"{self.path} holds a version {version} store, which this "
        err_msg += f"version ({_SCHEMA_VERSION}) cannot read: delete the "
        err_msg += "directory and sync again"
        self.close()
        raise IncompatibleStoreError(err_msg)

    def _run(self, statement: str, parameters: tuple[bytes, ...] = ()) -> None:
        """Execute a statement that answers nothing."""
        with self._lock:
            if self._closed:
                err_msg = f"the store at {self.path} is closed"
                raise StoreClosedError(err_msg)
            self._connection.execute(statement, parameters)

    def _rows(
        self, statement: str, parameters: tuple[bytes, ...] = ()
    ) -> list[tuple[Any, ...]]:
        """Execute a statement and read its answer, both under the lock.

        Both, and not the execute alone: a cursor read after the lock is
        released is a cursor another thread can step on -- one
        connection means one statement at a time, and `fetchone` is part
        of the statement.

        The row shape is `Any`, and genuinely so: this runs whatever SQL
        it is given, `PRAGMA` statements the tests read included, and
        not only the two `kv` shapes the methods below build on.
        """
        with self._lock:
            if self._closed:
                err_msg = f"the store at {self.path} is closed"
                raise StoreClosedError(err_msg)
            return self._connection.execute(statement, parameters).fetchall()

    def get(self, key: bytes) -> bytes | None:
        """Return the value stored under a key, or None."""
        rows = self._rows("SELECT v FROM kv WHERE k = ?", (key,))
        return cast("bytes", rows[0][0]) if rows else None

    def put(self, key: bytes, value: bytes) -> None:
        """Store a value under a key, replacing what was there."""
        self._run("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))

    def delete(self, key: bytes) -> None:
        """Remove a key, whether or not it was there."""
        self._run("DELETE FROM kv WHERE k = ?", (key,))

    def __iter__(self) -> Iterator[tuple[bytes, bytes]]:
        """Walk every pair, in ascending key order.

        Read whole under the lock rather than handed out as a cursor: a
        caller that walked lazily would hold nothing while another
        thread wrote underneath it, and both readers of this today read
        the store once at startup.
        """
        with self._lock:
            rows = self._rows("SELECT k, v FROM kv ORDER BY k")
            return iter(cast("list[tuple[bytes, bytes]]", rows))

    @contextmanager
    def write_batch(self) -> Iterator[KeyValueStore]:
        """Write everything in the block, or nothing at all.

        The lock is held for the whole batch, so nothing else reaches
        the connection mid-transaction; the writes inside re-enter it,
        which is what makes it an RLock.

        `BEGIN IMMEDIATE` and not a plain `BEGIN`: the write lock is
        taken when the batch opens rather than at its first write, so a
        second writer in another process waits at the start instead of
        failing partway through with nothing done.
        """
        with self._lock:
            self._run("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                # BaseException and not Exception: a KeyboardInterrupt
                # or a cancelled task through here would otherwise leave
                # the transaction open and the connection unusable
                self._run("ROLLBACK")
                raise
            self._run("COMMIT")

    def close(self) -> None:
        """Close the connection, once, and refuse every later use."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    @property
    def closed(self) -> bool:
        """Whether `close` has already been called."""
        return self._closed
