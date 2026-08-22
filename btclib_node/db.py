# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The ordered key-value store every index of this node is kept in.

Read a key, write one, delete one, write several as one, walk the whole
store in key order, and close: that is everything any index here asks
of it. They are behind one class so that what implements them is one
decision in one file -- `btclib_node/db.py` -- rather than a library
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
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

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

    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(exist_ok=True, parents=True)
        if (self.path / _LEVELDB_MARKER).exists():
            err_msg = f"{self.path} holds a LevelDB database, which this "
            err_msg += "version cannot read: delete the directory and sync "
            err_msg += "again"
            raise Exception(err_msg)
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

    def _run(self, statement, parameters=()):
        """Execute a statement that answers nothing."""
        with self._lock:
            if self._closed:
                raise Exception(f"the store at {self.path} is closed")
            self._connection.execute(statement, parameters)

    def _rows(self, statement, parameters=()):
        """Execute a statement and read its answer, both under the lock.

        Both, and not the execute alone: a cursor read after the lock is
        released is a cursor another thread can step on -- one
        connection means one statement at a time, and `fetchone` is part
        of the statement.
        """
        with self._lock:
            if self._closed:
                raise Exception(f"the store at {self.path} is closed")
            return self._connection.execute(statement, parameters).fetchall()

    def get(self, key):
        """Return the value stored under a key, or None."""
        rows = self._rows("SELECT v FROM kv WHERE k = ?", (key,))
        return rows[0][0] if rows else None

    def put(self, key, value):
        """Store a value under a key, replacing what was there."""
        self._run("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))

    def delete(self, key):
        """Remove a key, whether or not it was there."""
        self._run("DELETE FROM kv WHERE k = ?", (key,))

    def __iter__(self):
        """Walk every pair, in ascending key order.

        Read whole under the lock rather than handed out as a cursor: a
        caller that walked lazily would hold nothing while another
        thread wrote underneath it, and both readers of this today read
        the store once at startup.
        """
        with self._lock:
            return iter(self._rows("SELECT k, v FROM kv ORDER BY k"))

    @contextmanager
    def write_batch(self):
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

    def close(self):
        """Close the connection, once, and refuse every later use."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    @property
    def closed(self):
        return self._closed
