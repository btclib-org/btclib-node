# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The store every index of this node is kept in.

The operations, and the two properties the indexes rely on that a
dictionary would not give: keys come back in order, and a batch is all
or nothing. The rest of the suite exercises this through the indexes;
what is here is the store on its own, including the paths no index
reaches -- a datadir written by the version before it, a batch that
raises, a second thread, and a store whose own bytes come back
corrupted.
"""

import threading
from typing import TYPE_CHECKING

import pytest
from rocksdict import Options, Rdict, RdictIter

from btclib_node import db as db_module
from btclib_node.db import KeyValueStore
from btclib_node.exceptions import (
    IncompatibleStoreError,
    StoreClosedError,
    StoreCorruptionError,
)

if TYPE_CHECKING:
    from pathlib import Path


def a_store(tmp_path: Path, name: str = "store") -> KeyValueStore:
    """Open a `KeyValueStore` in its own subdirectory of `tmp_path`."""
    return KeyValueStore(tmp_path / name)


def flip_a_bit_of(directory: Path, needle: bytes) -> None:
    """Flip one bit of the first `needle` found in a `*.sst` under `directory`.

    What every corruption test below shares: `needle` has to be
    findable uncompressed on disk -- `db.py`'s own module docstring
    argues why compression is off, and this is where that choice pays
    for itself -- and the flip has to land inside it rather than beside
    it.
    """
    matches = [sst for sst in directory.glob("*.sst") if needle in sst.read_bytes()]
    assert matches, f"{needle!r} not found uncompressed in any *.sst under {directory}"
    target = matches[0]
    data = bytearray(target.read_bytes())
    offset = data.index(needle)
    data[offset] ^= 0xFF
    target.write_bytes(bytes(data))


def test_what_is_put_comes_back(tmp_path: Path) -> None:
    """`get` returns exactly what `put` stored under the same key."""
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    assert store.get(b"k") == b"v"
    store.close()


def test_a_key_nobody_wrote_is_answered_with_nothing(tmp_path: Path) -> None:
    """`get` on a key that was never `put` returns `None`, not an error."""
    store = a_store(tmp_path)
    assert store.get(b"absent") is None
    store.close()


def test_an_empty_value_is_not_the_same_as_no_key(tmp_path: Path) -> None:
    """A key `put` with `b""` reads back as `b""`, not as absent.

    The SQLite store this class replaced pinned this the same fact by
    declaring `v BLOB NOT NULL` in its own schema; RocksDB draws no
    schema this class could read back the same way, so this pins the
    behaviour it stated instead.
    """
    store = a_store(tmp_path)
    store.put(b"k", b"")
    assert store.get(b"k") == b""
    assert store.get(b"absent") is None
    store.close()


def test_writing_a_key_twice_keeps_the_second(tmp_path: Path) -> None:
    """`put` on an existing key replaces its value, not adding a second row."""
    store = a_store(tmp_path)
    store.put(b"k", b"first")
    store.put(b"k", b"second")
    assert store.get(b"k") == b"second"
    store.close()


def test_a_key_deleted_is_gone_and_deleting_it_again_is_nothing(tmp_path: Path) -> None:
    """`delete` removes a key, and deleting an absent key raises nothing."""
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store.delete(b"k")
    assert store.get(b"k") is None
    store.delete(b"k")
    store.close()


def test_the_walk_is_in_key_order(tmp_path: Path) -> None:
    """Iterating the store yields keys sorted, not in insertion order."""
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


def test_values_are_stored_raw_not_pickled_or_type_tagged(tmp_path: Path) -> None:
    """`raw_mode=True` is what every `Options` this store builds carries.

    `rocksdict`'s own default mode pickles a value that is not one of a
    handful of native types, and tags even those with a type byte --
    read back independently, through a fresh handle opened the same
    `raw_mode=True` way rather than through `KeyValueStore` itself, a
    value put through the store comes back exactly the bytes it was
    put as, with nothing of either kind wrapped around it.
    """
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store.close()

    direct = Rdict(str(tmp_path / "store"), Options(raw_mode=True))
    assert direct[b"k"] == b"v"
    direct.close()


def test_a_batch_is_written_whole(tmp_path: Path) -> None:
    """Every `put` inside a `write_batch` block is visible once it exits."""
    store = a_store(tmp_path)
    with store.write_batch() as batch:
        batch.put(b"a", b"1")
        batch.put(b"b", b"2")
    assert store.get(b"a") == b"1"
    assert store.get(b"b") == b"2"
    store.close()


def test_a_batch_that_raises_leaves_nothing_behind(tmp_path: Path) -> None:
    """An exception inside `write_batch` rolls back every write it made."""
    # what the chainstate needs of it: a block that fails validation
    # partway through a branch must not leave the writes before it
    store = a_store(tmp_path)
    store.put(b"before", b"kept")

    # batch.put/batch.delete have to run inside the open batch, before
    # the raise that unwinds it: that is what is under test, so they
    # cannot move out of the block PT012 wants single-statement.
    with pytest.raises(RuntimeError, match="no"), store.write_batch() as batch:  # noqa: PT012
        batch.put(b"a", b"1")
        batch.delete(b"before")
        raise RuntimeError("no")

    assert store.get(b"a") is None
    assert store.get(b"before") == b"kept"
    store.close()


def test_a_datadir_from_before_this_store_is_refused(tmp_path: Path) -> None:
    """A directory with sqlite3's own `index.sqlite` raises, not opens."""
    # a sqlite3 datadir, which this store cannot read. Starting an
    # empty chain over the top of one is the wrong failure: it looks
    # like a node that has never synced rather than one that cannot.
    # The marker inverted with the engine: RocksDB writes CURRENT,
    # the file the LevelDB-shaped marker this replaced used to refuse
    # -- db.py's own module docstring argues the swap.
    directory = tmp_path / "old"
    directory.mkdir()
    (directory / "index.sqlite").write_bytes(b"SQLite format 3\x00")
    with pytest.raises(IncompatibleStoreError, match="holds a sqlite3 database"):
        KeyValueStore(directory)


def test_a_pre_versioning_store_with_data_is_refused(tmp_path: Path) -> None:
    """A version-less store already holding a row is refused, not kept.

    An absent version key is also what a brand-new store answers before
    anything has stamped it -- `kv` (the default column family) already
    holding a row is what tells the two apart, `db.py`'s own module
    docstring argument for keeping the version out of it in the first
    place. A store from before this class ever stamped one is simulated
    here rather than fixture data, since nothing this class writes today
    can produce it: `_check_schema_version` stamps the version before
    `__init__` ever hands the object back.
    """
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store._meta.delete(db_module._VERSION_KEY)
    store.close()

    with pytest.raises(IncompatibleStoreError, match="version 0 store"):
        a_store(tmp_path)


def test_a_store_from_a_newer_schema_version_is_refused(tmp_path: Path) -> None:
    """A store stamped with a version this code does not know is refused."""
    store = a_store(tmp_path)
    store._meta.put(db_module._VERSION_KEY, (999).to_bytes(4, "big"))
    store.close()

    with pytest.raises(IncompatibleStoreError, match="version 999 store"):
        a_store(tmp_path)


def test_a_closed_store_refuses_to_be_used(tmp_path: Path) -> None:
    """A closed store reports `closed`; `get` raises `StoreClosedError`."""
    # the flag alone is what a close that closed nothing would also set.
    # The connections are asserted separately, below: here it is the
    # store's own answer, which is what every caller meets first.
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store.close()
    assert store.closed
    with pytest.raises(StoreClosedError, match="is closed"):
        store.get(b"k")


def test_a_thread_that_never_used_the_store_is_refused_after_it_closes(
    tmp_path: Path,
) -> None:
    """A thread with no connection is refused once the store closes."""
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
        except StoreClosedError as error:
            refused.append(str(error))

    thread = threading.Thread(target=late)
    thread.start()
    thread.join()

    assert refused
    assert "is closed" in refused[0]


def test_closing_twice_is_closing_once(tmp_path: Path) -> None:
    """A second `close` is a no-op, not an error on a closed connection."""
    store = a_store(tmp_path)
    store.close()
    store.close()
    assert store.closed


def test_a_store_is_read_and_written_from_more_than_one_thread(tmp_path: Path) -> None:
    """A second thread's `get`, `put` and `write_batch` reach the same store."""
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
    """The reason there is one lock around every use of the store.

    `RLock` is what keeps a `close()` from ever racing a `get()` still
    in progress on another thread: measured directly, without it, that
    race raises a raw `RuntimeError` from `rocksdict`'s own Rust layer
    (`"Already mutably borrowed"`) rather than anything a caller here
    can be expected to catch -- `db.py`'s own module docstring is where
    that measurement is recorded. With the lock, whatever a reader meets
    here is instead one of two things it already knows how to catch.
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
            except StoreClosedError as error:
                outcomes.add(str(error).split(" at ")[0])
                return

    thread = threading.Thread(target=read_until_closed)
    thread.start()
    for _ in range(200):
        store.get(b"k")
    store.close()
    thread.join(timeout=10)

    assert not thread.is_alive()
    # a value, or this store's own refusal, and nothing else: a raw
    # RuntimeError from rocksdict's own layer would mean the lock let
    # the race through, which is the state the lock exists to prevent
    assert outcomes <= {b"v", "the store"}


def test_what_is_written_is_still_there_when_it_is_opened_again(tmp_path: Path) -> None:
    """A value survives closing the store and reopening on the same path."""
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store.close()

    reopened = KeyValueStore(tmp_path / "store")
    assert reopened.get(b"k") == b"v"
    reopened.close()


def test_close_waits_for_whoever_is_using_the_connection(tmp_path: Path) -> None:
    """`close` blocks on the same lock a caller elsewhere already holds."""
    # the guard the race above is prevented by, pinned rather than
    # raced for: with the lock held, `close` has to wait
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
    """A `KeyboardInterrupt` mid-batch rolls back, leaving the store usable."""
    # BaseException and not Exception: a KeyboardInterrupt or a
    # cancelled task through an open batch would otherwise leave
    # self._pending_batch set to a batch nothing ever commits or clears
    store = a_store(tmp_path)
    store.put(b"before", b"kept")

    # batch.delete has to run inside the open batch, before the raise
    # that unwinds it: that is what is under test, so it cannot move
    # out of the block PT012 wants single-statement.
    with pytest.raises(KeyboardInterrupt), store.write_batch() as batch:  # noqa: PT012
        batch.delete(b"before")
        raise KeyboardInterrupt

    assert store.get(b"before") == b"kept"
    store.put(b"after", b"also kept")
    assert store.get(b"after") == b"also kept"
    store.close()


def test_the_store_takes_the_directory_lock_when_it_opens(tmp_path: Path) -> None:
    """A second handle on the same directory is refused from the start.

    SQLite's own `BEGIN IMMEDIATE` took the write lock when a batch
    opened rather than at its first write, so a second writer in
    another process waited from the start of the batch rather than
    failing partway through with nothing done. RocksDB draws that same
    line earlier still: the directory `LOCK` is exclusive from the
    moment this store's own `__init__` opens it, well before any batch,
    so there is no window for a second writer to find open that this
    test's own second `Rdict` does not already meet closed.
    """
    store = a_store(tmp_path)
    with pytest.raises(Exception, match="lock"):
        Rdict(str(tmp_path / "store"), Options(raw_mode=True))
    store.close()


def test_a_write_does_not_wait_for_the_disk(tmp_path: Path) -> None:
    """`WriteOptions`' own default, `sync=False`, is what every write uses."""
    # sync=False, which is what LevelDB's own NORMAL synchronous mode
    # gave the sqlite3 store too: a kill loses the last writes rather
    # than corrupting the store, and the alternative costs an fsync per
    # write
    assert db_module._WRITE_OPTIONS.sync is False


def test_a_flipped_bit_in_an_sst_raises_this_tree_s_own_error(tmp_path: Path) -> None:
    """A corrupted value's own bytes are found unreadable, not read wrong.

    This is the guarantee the whole move to RocksDB bought
    (btclib-org/btclib-node#641, closing btclib-org/btclib-node#637): a
    flipped bit on disk is an exception at the read, not a wrong answer
    nothing notices.
    """
    store = a_store(tmp_path)
    value = b"V" * 39
    for i in range(2000):
        store.put(i.to_bytes(4, "big"), value + i.to_bytes(4, "big"))
    store._db.flush()
    store.close()

    flip_a_bit_of(tmp_path / "store", value)
    reopened = KeyValueStore(tmp_path / "store")

    # key 0's own record: `flip_a_bit_of` flips the first match of
    # `value` in the file, and every record here shares that 39-byte
    # prefix, so the first match on disk is the smallest key's own
    with pytest.raises(StoreCorruptionError, match="Corruption"):
        reopened.get((0).to_bytes(4, "big"))


def test_a_flipped_bit_is_caught_by_the_iterator_too(tmp_path: Path) -> None:
    """A corrupted block stops the walk, and it does not stop it silently.

    `db.py`'s own module docstring is where this is measured against
    the higher-level `Rdict.items`, which does not raise here at all --
    it answers a corrupted block the way it answers the genuine end of
    the store, silently short. `__iter__`'s own walk goes through the
    lower-level `Rdict.iter` instead, checking `status()` once after,
    which is what turns that same corruption into an exception here.
    """
    store = a_store(tmp_path)
    value = b"V" * 39
    for i in range(2000):
        store.put(i.to_bytes(4, "big"), value + i.to_bytes(4, "big"))
    store._db.flush()
    store.close()

    flip_a_bit_of(tmp_path / "store", value)
    reopened = KeyValueStore(tmp_path / "store")

    with pytest.raises(StoreCorruptionError, match="Corruption"):
        list(reopened)


def test_a_flipped_bit_is_caught_while_checking_whether_a_version_less_store_has_data(
    tmp_path: Path,
) -> None:
    """`_has_any_data`'s own scan raises too, rather than reading as empty.

    The same silent-truncation risk `__iter__`'s own test above pins,
    for `Rdict.keys` rather than `Rdict.items`: over a store whose only
    block is the corrupted one, `keys()` answers nothing at all,
    indistinguishable from a genuinely empty store -- and this is
    exactly the read `_check_schema_version` trusts to tell an empty
    store from one this class predates. A version-less store already
    holding data has to be refused, per
    `test_a_pre_versioning_store_with_data_is_refused` above; this pins
    that it is refused as `StoreCorruptionError` rather than silently
    re-stamped current when what holds the data is unreadable.
    """
    store = a_store(tmp_path)
    value = b"V" * 39
    for i in range(2000):
        store.put(i.to_bytes(4, "big"), value + i.to_bytes(4, "big"))
    store._db.flush()
    store._meta.delete(db_module._VERSION_KEY)
    store.close()

    flip_a_bit_of(tmp_path / "store", value)

    with pytest.raises(StoreCorruptionError, match="Corruption"):
        KeyValueStore(tmp_path / "store")


def test_a_corrupted_meta_column_family_is_caught_while_opening(tmp_path: Path) -> None:
    """The schema-version check's own read raises on a corrupted stamp.

    Targets the `_META_COLUMN_FAMILY`'s own tiny SST specifically --
    found through `Rdict.live_files`'s own `start_key`, rather than
    guessed by size -- so what is corrupted is the version stamp
    `_check_schema_version` reads before `__init__` ever hands the
    store back, not the default column family's own data.
    """
    store = a_store(tmp_path)
    store.put(b"k", b"v")
    store._db.flush()
    store._meta.flush()
    (meta_file,) = (
        f["name"].removeprefix("/")
        for f in store._db.live_files()
        if f["start_key"] == b"schema-version"
    )
    store.close()

    needle = db_module._SCHEMA_VERSION.to_bytes(4, "big")
    target = tmp_path / "store" / meta_file
    data = bytearray(target.read_bytes())
    offset = data.index(needle)
    data[offset] ^= 0xFF
    target.write_bytes(bytes(data))

    with pytest.raises(StoreCorruptionError, match="Corruption"):
        KeyValueStore(tmp_path / "store")


def test_a_corrupted_manifest_is_caught_at_the_open_itself(tmp_path: Path) -> None:
    """A structurally corrupted store raises out of the open, not a read.

    `Rdict`'s own constructor is the third of `db.py`'s three read
    paths: RocksDB reads its `MANIFEST` while opening to learn its own
    structure, and a corrupted one is a fault this class meets before
    `__init__` ever gets past building `self._db`.
    """
    store = a_store(tmp_path)
    for i in range(500):
        store.put(i.to_bytes(4, "big"), b"V" * 39)
    store._db.flush()
    store.close()

    directory = tmp_path / "store"
    (manifest,) = directory.glob("MANIFEST-*")
    data = bytearray(manifest.read_bytes())
    offset = len(data) // 2
    data[offset] ^= 0xFF
    manifest.write_bytes(bytes(data))

    with pytest.raises(StoreCorruptionError, match="Corruption"):
        KeyValueStore(directory)


def test_a_non_corruption_exception_is_never_reclassified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a message beginning `Corruption:` becomes `StoreCorruptionError`.

    Exercised at each of the five points `db.py` reads through
    `_raise_if_corrupted`: the open itself, the schema-version check's
    own read, `_has_any_data`'s own scan, `get`, and `__iter__`. A
    genuine defect this guards against: swallowing an unrelated fault
    -- a disk-full `OSError`, a permission error -- under the same
    classification as a checksummed corruption would hide it from
    whoever reads the log for the name that is supposed to be specific.
    """

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    with monkeypatch.context() as patched:
        patched.setattr(db_module, "Rdict", boom)
        with pytest.raises(RuntimeError, match="boom"):
            KeyValueStore(tmp_path / "open")

    with monkeypatch.context() as patched:
        patched.setattr(Rdict, "get", boom)
        with pytest.raises(RuntimeError, match="boom"):
            a_store(tmp_path, "schema-version-read")

    with monkeypatch.context() as patched:
        # `status()`, called after the walk `_has_any_data` makes,
        # rather than `Rdict.iter` itself: `iter()` only ever
        # constructs the iterator, never reads anything, so patching it
        # would never reach `_raise_if_corrupted` at all
        patched.setattr(RdictIter, "status", boom)
        with pytest.raises(RuntimeError, match="boom"):
            a_store(tmp_path, "has-any-data-scan")

    store = a_store(tmp_path, "already-open")
    with monkeypatch.context() as patched:
        patched.setattr(Rdict, "get", boom)
        with pytest.raises(RuntimeError, match="boom"):
            store.get(b"k")

    with monkeypatch.context() as patched:
        patched.setattr(RdictIter, "status", boom)
        with pytest.raises(RuntimeError, match="boom"):
            list(store)

    store.close()
