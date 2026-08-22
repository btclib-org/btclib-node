# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The store every index of this node is kept in.

Six operations, and the two properties the indexes rely on that a
dictionary would not give: keys come back in order, and a batch is all
or nothing. The rest of the suite exercises this through the indexes;
what is here is the store on its own, including the paths no index
reaches -- a datadir written by the version before it, a batch that
raises, and a second thread.
"""

import sqlite3
import threading

import pytest

from btclib_node.db import KeyValueStore


def a_store(tmp_path, name="store"):
    store = KeyValueStore(tmp_path / name)
    return store


def test_what_is_put_comes_back(tmp_path):
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    assert store.get(b"k") == b"v"
    store.close()


def test_a_key_nobody_wrote_is_answered_with_nothing(tmp_path):
    store = a_store(tmp_path)
    assert store.get(b"absent") is None
    store.close()


def test_writing_a_key_twice_keeps_the_second(tmp_path):
    store = a_store(tmp_path)
    store.put(b"k", b"first")
    store.put(b"k", b"second")
    assert store.get(b"k") == b"second"
    store.close()


def test_a_key_deleted_is_gone_and_deleting_it_again_is_nothing(tmp_path):
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store.delete(b"k")
    assert store.get(b"k") is None
    store.delete(b"k")
    store.close()


def test_the_walk_is_in_key_order(tmp_path):
    # load-bearing, and not only for the walk: both `init_from_db`
    # methods read until the first key that is not theirs, so a store
    # that answered in insertion order would stop them early or late
    store = a_store(tmp_path)
    for key in (b"c", b"a", b"blkinfo-2", b"b", b"blkinfo-1"):
        store.put(key, b"v")
    assert [key for key, _ in store] == [
        b"a",
        b"b",
        b"blkinfo-1",
        b"blkinfo-2",
        b"c",
    ]
    store.close()


def test_a_batch_is_written_whole(tmp_path):
    store = a_store(tmp_path)
    with store.write_batch() as batch:
        batch.put(b"a", b"1")
        batch.put(b"b", b"2")
    assert store.get(b"a") == b"1"
    assert store.get(b"b") == b"2"
    store.close()


def test_a_batch_that_raises_leaves_nothing_behind(tmp_path):
    # what the chainstate needs of it: a block that fails validation
    # partway through a branch must not leave the writes before it
    store = a_store(tmp_path)
    store.put(b"before", b"kept")

    with pytest.raises(RuntimeError, match="no"), store.write_batch() as batch:
        batch.put(b"a", b"1")
        batch.delete(b"before")
        raise RuntimeError("no")

    assert store.get(b"a") is None
    assert store.get(b"before") == b"kept"
    store.close()


def test_a_datadir_from_before_this_store_is_refused(tmp_path):
    # a LevelDB directory, which this store cannot read. Starting an
    # empty chain over the top of one is the wrong failure: it looks
    # like a node that has never synced rather than one that cannot
    directory = tmp_path / "old"
    directory.mkdir()
    (directory / "CURRENT").write_text("MANIFEST-000001\n", encoding="ascii")
    with pytest.raises(Exception, match="holds a LevelDB database"):
        KeyValueStore(directory)


def test_a_closed_store_has_closed_its_connections(tmp_path):
    # the flag alone is what a close that closed nothing would also set,
    # so what is asserted is the connection: SQLite refuses a closed one
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store.close()
    assert store.closed
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        store.get(b"k")


def test_closing_twice_is_closing_once(tmp_path):
    store = a_store(tmp_path)
    store.close()
    store.close()
    assert store.closed


def test_a_store_is_read_and_written_from_more_than_one_thread(tmp_path):
    # the node opens its databases in the thread that builds it and uses
    # them in the thread that runs it, and the tests write from both. A
    # single shared connection is what SQLite refuses; a connection per
    # thread is what this store does instead.
    store = a_store(tmp_path)
    store.put(b"from-main", b"v")
    seen = []

    theirs = []

    def other_thread():
        # no try/except: an exception here leaves "done" off the list,
        # which the assertions below catch. Catching it instead would be
        # a branch nothing takes on a green run.
        seen.append(store.get(b"from-main"))
        store.put(b"from-other", b"v")
        with store.write_batch() as batch:
            batch.put(b"batched-elsewhere", b"v")
        theirs.append(store._connection())
        seen.append("done")

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()

    assert seen == [b"v", "done"]
    assert store.get(b"from-other") == b"v"
    assert store.get(b"batched-elsewhere") == b"v"

    # and closing takes down the connection that thread opened, not
    # only this one's: the thread is gone, so nothing else ever will
    store.close()
    (their_connection,) = theirs
    for connection in (their_connection, store._local.connection):
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


def test_what_is_written_is_still_there_when_it_is_opened_again(tmp_path):
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store.close()

    reopened = KeyValueStore(tmp_path / "store")
    assert reopened.get(b"k") == b"v"
    reopened.close()


def test_the_table_is_the_key_s_own_b_tree(tmp_path):
    # WITHOUT ROWID is what makes the ordered walk the table's own order
    # rather than a sort, and it is not visible from any of the six
    # operations -- so it is read off the schema
    store = a_store(tmp_path)
    connection = sqlite3.connect(store.file)
    (schema,) = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'kv'"
    ).fetchone()
    connection.close()
    assert "WITHOUT ROWID" in schema
    store.close()
