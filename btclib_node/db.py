# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The ordered key-value store every index of this node is kept in.

Six operations, which is all any of them asks for: read a key, write
one, delete one, write several as one, walk the whole store in key
order, and close. They are behind one class so that what implements
them is one decision in one file -- `btclib_node/db.py` -- rather than
a library named in four modules.

The implementation is SQLite, and the reason is not speed. It is the
standard library, so there is no wheel to be missing on a platform, no
compiler to be present, and no stubs to be absent; the three of those
are what LevelDB through `plyvel` cost here, measured in
btclib-org/btclib-node#107. A table of `BLOB PRIMARY KEY` declared
`WITHOUT ROWID` *is* a B-tree on the key, so the ordered walk below is
the table's own order and not a sort.

**Key order is load-bearing**, and not only for the walk: both
`init_from_db` methods read until the first key that is not theirs, so
a prefix that sorts before another's is a reader that stops early.

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

    A connection per thread, because the node opens the store in the
    thread that builds it and uses it in the thread that runs it, and
    the tests write from both. SQLite serializes writers itself, which
    a single shared connection would not.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(exist_ok=True, parents=True)
        if (self.path / _LEVELDB_MARKER).exists():
            err_msg = f"{self.path} holds a LevelDB database, which this "
            err_msg += "version cannot read: delete the directory to sync "
            err_msg += "again, or use a release built against plyvel"
            raise Exception(err_msg)
        self.file = self.path / "index.sqlite"

        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections = []
        self._closed = False
        # eagerly, so that the file and the schema exist before another
        # thread's first read rather than being raced into being
        self._connection()

    def _connection(self):
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            return connection
        # check_same_thread is off for `close` alone: every other use is
        # from the thread that opened it, and close is what the owning
        # thread does not always get to do
        connection = sqlite3.connect(
            self.file, isolation_level=None, check_same_thread=False
        )
        # WAL so a reader does not block the writer, and the writer does
        # not block a reader: the node writes from its own loop while a
        # test reads. NORMAL is what LevelDB gives by default too -- a
        # write is not fsynced, and a crash loses the last transactions
        # rather than corrupting the store.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        # a writer waits for the other writer instead of raising
        # `database is locked` at once
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(_SCHEMA)
        self._local.connection = connection
        with self._lock:
            self._connections.append(connection)
        return connection

    def get(self, key):
        """Return the value stored under a key, or None."""
        row = self._connection().execute("SELECT v FROM kv WHERE k = ?", (key,))
        found = row.fetchone()
        return found[0] if found else None

    def put(self, key, value):
        """Store a value under a key, replacing what was there."""
        self._connection().execute(
            "INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value)
        )

    def delete(self, key):
        """Remove a key, whether or not it was there."""
        self._connection().execute("DELETE FROM kv WHERE k = ?", (key,))

    def __iter__(self):
        """Walk every pair, in ascending key order."""
        return iter(self._connection().execute("SELECT k, v FROM kv ORDER BY k"))

    @contextmanager
    def write_batch(self):
        """Write everything in the block, or nothing at all.

        `BEGIN IMMEDIATE` and not a plain `BEGIN`: the write lock is
        taken when the batch opens rather than at its first write, so a
        second writer waits at the start instead of failing partway
        through with nothing done and a lock it cannot upgrade.
        """
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def close(self):
        """Close every connection this store handed out."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            connections, self._connections = self._connections, []
        for connection in connections:
            connection.close()

    @property
    def closed(self):
        return self._closed
