# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The store every index of this node is kept in.

The operations, and the two properties the indexes rely on that a
dictionary would not give: keys come back in order, and a batch is all
or nothing. The rest of the suite exercises this through the indexes;
what is here is the store on its own, including the paths no index
reaches -- a datadir written by the version before it, a batch that
raises, and a second thread.
"""

import sqlite3
import threading
from pathlib import Path

import pytest

from btclib_node.db import KeyValueStore


def a_store(tmp_path: Path, name: str = "store") -> KeyValueStore:
    store = KeyValueStore(tmp_path / name)
    return store


def test_what_is_put_comes_back(tmp_path: Path) -> None:
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    assert store.get(b"k") == b"v"
    store.close()


def test_a_key_nobody_wrote_is_answered_with_nothing(tmp_path: Path) -> None:
    store = a_store(tmp_path)
    assert store.get(b"absent") is None
    store.close()


def test_writing_a_key_twice_keeps_the_second(tmp_path: Path) -> None:
    store = a_store(tmp_path)
    store.put(b"k", b"first")
    store.put(b"k", b"second")
    assert store.get(b"k") == b"second"
    store.close()


def test_a_key_deleted_is_gone_and_deleting_it_again_is_nothing(tmp_path: Path) -> None:
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store.delete(b"k")
    assert store.get(b"k") is None
    store.delete(b"k")
    store.close()


def test_the_walk_is_in_key_order(tmp_path: Path) -> None:
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


def test_a_batch_is_written_whole(tmp_path: Path) -> None:
    store = a_store(tmp_path)
    with store.write_batch() as batch:
        batch.put(b"a", b"1")
        batch.put(b"b", b"2")
    assert store.get(b"a") == b"1"
    assert store.get(b"b") == b"2"
    store.close()


def test_a_batch_that_raises_leaves_nothing_behind(tmp_path: Path) -> None:
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


def test_a_datadir_from_before_this_store_is_refused(tmp_path: Path) -> None:
    # a LevelDB directory, which this store cannot read. Starting an
    # empty chain over the top of one is the wrong failure: it looks
    # like a node that has never synced rather than one that cannot
    directory = tmp_path / "old"
    directory.mkdir()
    (directory / "CURRENT").write_text("MANIFEST-000001\n", encoding="ascii")
    with pytest.raises(Exception, match="holds a LevelDB database"):
        KeyValueStore(directory)


def test_a_closed_store_refuses_to_be_used(tmp_path: Path) -> None:
    # the flag alone is what a close that closed nothing would also set.
    # The connections are asserted separately, below: here it is the
    # store's own answer, which is what every caller meets first.
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store.close()
    assert store.closed
    with pytest.raises(Exception, match="is closed"):
        store.get(b"k")


def test_a_thread_that_never_used_the_store_is_refused_after_it_closes(
    tmp_path: Path,
) -> None:
    # what `closed` has to mean for the store and not only for the
    # connections it happened to be holding: a thread with none of its
    # own would otherwise be handed a working one and write through a
    # store everything else believes is shut
    store = a_store(tmp_path)
    store.close()
    refused = []

    def late() -> None:
        try:
            store.put(b"k", b"v")
        except Exception as error:  # noqa: BLE001
            refused.append(str(error))

    thread = threading.Thread(target=late)
    thread.start()
    thread.join()

    assert refused and "is closed" in refused[0]


def test_closing_twice_is_closing_once(tmp_path: Path) -> None:
    store = a_store(tmp_path)
    store.close()
    store.close()
    assert store.closed


def test_a_store_is_read_and_written_from_more_than_one_thread(tmp_path: Path) -> None:
    # the node opens its databases in the thread that builds it and uses
    # them in the thread that runs it, and the tests write from both
    store = a_store(tmp_path)
    store.put(b"from-main", b"v")
    seen: list[bytes | str | None] = []

    def other_thread() -> None:
        # no try/except: an exception here leaves "done" off the list,
        # which the assertions below catch. Catching it instead would be
        # a branch nothing takes on a green run.
        seen.append(store.get(b"from-main"))
        store.put(b"from-other", b"v")
        with store.write_batch() as batch:
            batch.put(b"batched-elsewhere", b"v")
        seen.append("done")

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()

    assert seen == [b"v", "done"]
    assert store.get(b"from-other") == b"v"
    assert store.get(b"batched-elsewhere") == b"v"
    store.close()


def test_closing_while_another_thread_reads_does_not_take_the_process_down(
    tmp_path: Path,
) -> None:
    """The reason there is one connection and a lock, and not one each.

    A connection per thread meant `close` reaching into a connection
    another thread was inside, and CPython's sqlite3 answers that with a
    segmentation fault rather than an exception -- the whole process,
    from a fixture teardown. Whatever a reader meets here it has to be
    something it can catch.
    """
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    outcomes: set[bytes | str | None] = set()

    def read_until_closed() -> None:
        # `while True`, because the refusal is the only way out: a loop
        # with a second exit would leave the reader able to finish
        # before the close it is here to race
        while True:
            try:
                outcomes.add(store.get(b"k"))
            except Exception as error:  # noqa: BLE001
                outcomes.add(str(error).split(" at ")[0])
                return

    thread = threading.Thread(target=read_until_closed)
    thread.start()
    for _ in range(200):
        store.get(b"k")
    store.close()
    thread.join(timeout=10)

    assert not thread.is_alive()
    # a value, or this store's own refusal, and nothing else: SQLite's
    # own ProgrammingError would mean the connection was closed under
    # somebody, which is the state that used to crash
    assert outcomes <= {b"v", "the store"}


def test_what_is_written_is_still_there_when_it_is_opened_again(tmp_path: Path) -> None:
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store.close()

    reopened = KeyValueStore(tmp_path / "store")
    assert reopened.get(b"k") == b"v"
    reopened.close()


def test_close_waits_for_whoever_is_using_the_connection(tmp_path: Path) -> None:
    # the guard the segfault above is prevented by, pinned rather than
    # raced for: with the lock held, `close` has to wait, because
    # closing a connection out from under a statement is what CPython's
    # sqlite3 answers with a crash
    store = a_store(tmp_path)
    closed = threading.Event()

    def close_from_the_other_thread() -> None:
        store.close()
        closed.set()

    with store._lock:
        thread = threading.Thread(target=close_from_the_other_thread)
        thread.start()
        assert not closed.wait(timeout=0.2)
        assert not store.closed

    thread.join(timeout=10)
    assert closed.is_set()
    assert store.closed


def test_a_batch_rolls_back_on_what_is_not_an_exception_either(tmp_path: Path) -> None:
    # BaseException and not Exception: a KeyboardInterrupt or a
    # cancelled task through an open batch would otherwise leave the
    # transaction open and the connection unusable for everything after
    store = a_store(tmp_path)
    store.put(b"before", b"kept")

    with pytest.raises(KeyboardInterrupt), store.write_batch() as batch:
        batch.delete(b"before")
        raise KeyboardInterrupt

    assert store.get(b"before") == b"kept"
    store.put(b"after", b"also kept")
    assert store.get(b"after") == b"also kept"
    store.close()


def test_a_batch_takes_the_write_lock_when_it_opens(tmp_path: Path) -> None:
    # BEGIN IMMEDIATE and not a plain BEGIN. With one connection it is
    # another process that this is about -- a second node on the same
    # datadir -- so the second writer is a second connection here.
    store = a_store(tmp_path)
    other = sqlite3.connect(store.file, isolation_level=None)
    other.execute("PRAGMA busy_timeout=0")
    try:
        with store.write_batch() as batch:
            # before the batch has written anything: a plain BEGIN takes
            # no lock until its first write, and would let this through
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("INSERT INTO kv VALUES (?, ?)", (b"theirs", b"v"))
            batch.put(b"mine", b"v")
    finally:
        other.close()
    assert store.get(b"mine") == b"v"
    assert store.get(b"theirs") is None
    store.close()


def test_a_commit_does_not_wait_for_the_disk(tmp_path: Path) -> None:
    # synchronous=NORMAL, which is what LevelDB gives by default too: a
    # kill loses the last transactions rather than corrupting the store,
    # and the alternative costs an fsync per block connected
    store = a_store(tmp_path)
    ((synchronous,),) = store._rows("PRAGMA synchronous")
    ((journal,),) = store._rows("PRAGMA journal_mode")
    assert synchronous == 1  # NORMAL
    assert journal == "wal"
    store.close()


def test_the_table_is_the_key_s_own_b_tree(tmp_path: Path) -> None:
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
    # and a value is never absent, which is what `get` reads as "no key"
    assert "v BLOB NOT NULL" in schema
    store.close()
