# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The ordered key-value store every index of this node is kept in.

Read a key, write one, delete one, write several as one, walk the whole
store in key order, and close: that is everything any index here asks
of it. They are behind one class so that what implements them is one
decision in one file -- `src/btclib_node/db.py` -- rather than a library
named in as many modules as import it. None of that surface moved with
this file's own implementation, below.

The implementation is RocksDB, through `rocksdict`, and the reason is
not speed, even though it is markedly faster
(btclib-org/btclib-node#641's own measurement: load, flush and get all
several times sqlite3's own, on this tree's own workload). It is
integrity. btclib-org/btclib-node#107 chose sqlite3 over LevelDB --
Bitcoin Core's own store, `src/leveldb/`, vendored into its tree and
wrapped by `CDBWrapper` -- to avoid a compiled dependency: no wheel
missing on a platform, no compiler, no stubs. That measurement never
weighed what LevelDB gives Core in exchange:
`CDBWrapper` reads with `verify_checksums = true`
(`src/dbwrapper.cpp:248`, at bitcoin/bitcoin@ca7162cde5) over data
LevelDB itself checksums per block (CRC32C), so a bit flipped on disk
is an exception at the read, never a wrong answer. sqlite3, as this
store used it, checksummed nothing: a flipped bit in a value read back
as a different, valid-looking value, silently, and a flipped bit in a
key made the record it named cease to exist, both measured directly
(btclib-org/btclib-node#641). Only the first of those two is even in
reach of a scheme built by hand inside a value's own bytes
(btclib-org/btclib-node#637's own per-record CRC): a corrupted key is
never read under its own name, so nothing carried inside the record it
pointed to can ever see it, and that is the fault that silently forks
a node off the network, a UTXO its own consensus rules still consider
spendable simply gone. `rocksdict` was chosen over `plyvel` -- the
library #107 measured LevelDB itself through -- because `plyvel` is
dead against this tree's `requires-python = ">=3.14"`: no cp314 wheel
at all, one platform, last released 2024-01
(btclib-org/btclib-node#641). `rocksdict` ships cp314 wheels on nine
platforms (`win_arm64` alone building from source), `py.typed` plus a
`.pyi`, current releases -- #107's own packaging argument, answered on
today's numbers rather than assumed to still hold.

What is accepted in exchange, stated whole rather than left implicit: a
compiled dependency returns, narrowing "wherever Python runs" to
"wherever a wheel exists"; RSS several times sqlite3's own (memtables
and a block cache, sized below the way Core's own `-dbcache` sizes the
same knobs, and tunable the same way); `rocksdict`'s own exceptions
arrive untyped, a bare `Exception` string-matched into this tree's own
`StoreCorruptionError` (`exceptions.py`); a reader in another process,
which sqlite3's WAL let in beside the writer, is refused outright by
RocksDB's own directory `LOCK` -- nothing in this tree opens the store
from a second process, so what is given up is a capability and not a
caller; and one datadir migration, the same shape #107 itself already
cost once. None of that is a departure
from *Following Bitcoin Core* -- it is Core's own choice, reached this
time by measurement rather than assumed, and the packaging half of it
is the "Python-native" axis `CLAUDE.md` already carves out for this
file, argued in the same terms `db.py` used against LevelDB in the
first place. What was never available before now is: a wheel that
carries LevelDB's own fork, typed, on the platforms this tree ships to.

**Key order is load-bearing**, exactly as before: `BlockIndex.init_from_db`
reads until the first key that is not a `blkinfo-`, so a prefix that
sorted before that one would stop it early; `BlockDB.init_from_db` walks
the whole store and dispatches on the prefix, which is slower and
cannot be tripped that way. RocksDB keeps a key's own bytes in
lexicographic order the same way LevelDB does -- the comparator neither
side changes -- so nothing here depends on this store's own choice of
engine.

**`raw_mode=True`, on every `Options` and `WriteBatch` this file
builds.** `rocksdict`'s own default mode pickles a value that is not
one of a handful of native types it recognizes, and tags even the ones
it does with a type byte -- an encoding this store never asked for and
would pay for silently: every key and value here is already the caller's
own serialized bytes, and a comparison of `rocksdict` against sqlite3
that left the default mode on measured that tag-and-pickle overhead
alongside the two stores rather than between them
(btclib-org/btclib-node#641's own comment names this the trap the
benchmark could have fallen into).

## The configuration, matched to Core's `GetOptions`/`DBParams` line by line

`src/dbwrapper.cpp` and `src/dbwrapper.h`, read at
bitcoin/bitcoin@ca7162cde5:

- `options.compression = leveldb::kNoCompression`
  (`src/dbwrapper.cpp:145`) -> `DBCompressionType.none()`. Compression is
  not network-visible, so it could in principle be a local choice, but
  the rule this file is written under is match unless something forces
  otherwise, and nothing does -- it is also what keeps a corrupted
  record's own bytes findable on disk by search rather than only by
  decompressing every block, which is what the corruption test below
  relies on.
- `options.paranoid_checks = true` (`src/dbwrapper.cpp:150,162`) ->
  `Options.set_paranoid_checks(True)`.
- `readoptions.verify_checksums = true` and
  `iteroptions.verify_checksums = true` (`src/dbwrapper.cpp:248-249`) ->
  `ReadOptions.set_verify_checksums(True)`, on the options this file
  passes to every `get` and to the scan `__iter__` builds. `rocksdict`'s
  own `ReadOptions` default is already `True`; it is set here anyway,
  the way Core's own constructor sets it explicitly rather than relying
  on LevelDB's default, so a reader of this file sees the decision
  rather than has to already know the vendor's default to trust it.
  `iteroptions.fill_cache = false` (`src/dbwrapper.cpp:250`) is matched
  too, on the scan alone: a full walk of the store is not the working
  set this store's own block cache exists to hold hot, and Core does
  not let one displace it either.
- `DBParams.bloom_filter = true` by default (`src/dbwrapper.h:54`) ->
  `NewBloomFilterPolicy(10)` (`src/dbwrapper.cpp:144`) ->
  `BlockBasedOptions.set_bloom_filter(10, False)`, reached through
  `Options.set_block_based_table_factory`. This is the storage engine's
  own bloom filter, over the bytes of every key this store holds, built
  and consulted entirely inside RocksDB -- unrelated to BIP37's own,
  deprecated peer-relay bloom filter, which is a network message this
  node's own peers exchange and this store never sees.
- `block_cache = NewLRUCache(nCacheSize / 2)` and
  `write_buffer_size = nCacheSize / 4` (`src/dbwrapper.cpp:142-143`),
  `nCacheSize` being `-dbcache`, which this tree has no flag for.
  `nCacheSize` here is instead what Core's own **default** `-dbcache`
  gives the coins database specifically, computed the way Core computes
  it and not guessed: `node::GetDefaultDBCache`
  (`src/node/caches.cpp`, same sha) answers `1_GiB` on a 64-bit build
  with at least `4_GiB` of RAM, `DEFAULT_KERNEL_CACHE` (`450_MiB`)
  otherwise; `kernel::CacheSizes` (`src/kernel/caches.h`, same sha)
  then takes `block_tree_db = min(total / 8, 2_MiB)` off the top and
  gives the coins database `coins_db = min(remainder / 2, 8_MiB)` --
  and `remainder / 2` clears `8_MiB` under *either* default
  (roughly `510_MiB` and `224_MiB`), so the coins database's own share
  is the cap, `MAX_COINS_DB_CACHE`, `8_MiB`, regardless of which
  default this tree would have read with no `-dbcache` of its own.
  `_N_CACHE_SIZE` below is that `8_MiB`, and the block cache and write
  buffer this file builds from it are the same fractions of it Core
  takes of `nCacheSize`.
- `raw_mode=True` is argued above, on both `Options` and `WriteBatch`.
- `WriteOptions`' own default, `sync=False`, is the counterpart of the
  `synchronous=NORMAL` the sqlite3 store ran under: a write reaches the
  operating system's own page cache and is not fsynced before this
  store answers its caller, so a kill loses whatever was not yet
  flushed to disk and never corrupts what was. It is passed explicitly
  below, unchanged from its own default, for the same reason the read
  options above are passed rather than left to a default nothing here
  states.

## The datadir marker inverts

`_SQLITE_MARKER` refuses the file this store itself used to write,
`index.sqlite`, the same way `_LEVELDB_MARKER = "CURRENT"` used to
refuse the file LevelDB wrote before that -- RocksDB being LevelDB's own
fork, it writes `CURRENT` too, so a marker built the old way would now
refuse this store's own datadir on its second open. Refusing
`index.sqlite` by name is the same shape of guard for the same reason:
a datadir written by the version before this one is not a store this
version can silently start an empty chain over the top of, and the
message stays "delete the directory and sync again" -- there is nothing
here to migrate a `sqlite3` file's own rows into RocksDB's own SST
format, and no attempt is made to.

## `_SCHEMA_VERSION` moves to its own column family

The old argument for keeping a schema version out of `kv`'s own key
order -- `PRAGMA user_version`, four bytes in the SQLite file's own
header, untouched by anything this class wrote into the table --
carries over whole, and RocksDB's own native answer to the same
constraint is a second **column family**: `_META_COLUMN_FAMILY`, kept
open beside the default one every `get`/`put`/`delete`/`__iter__` above
reads and writes, and never walked by any of them -- `BlockIndex
.init_from_db` stopping at the first key that is not its own `blkinfo-`
never sees it, the same guarantee `PRAGMA user_version` gave by sitting
outside `kv` entirely. `_VERSION_KEY` inside that column family holds
`_SCHEMA_VERSION`, four bytes, big-endian, the same width `PRAGMA
user_version` answered in.

The two states `PRAGMA user_version == 0` used to leave ambiguous --
a brand-new file, answering `0` because SQLite starts every one there,
and a store written before this class carried a version at all -- have
a narrower RocksDB shape. A **fresh** store is a directory RocksDB has
never opened: `Options.create_missing_column_families(True)` (below)
creates `_META_COLUMN_FAMILY` itself, empty, on that first open, so the
version key is absent there for the same reason it is absent from a
brand-new SQLite file -- nothing has written it yet -- and `kv` (the
default column family) is checked for a row exactly as before, to tell
that state apart from the other one still possible: a version key
absent from a column family this store's own default policy would
otherwise have already created empty, with data already sitting in
`kv`. A store written by any version of *this* class, in contrast,
cannot be version-less at all: `_check_schema_version` stamps
`_SCHEMA_VERSION` into the meta column family before `__init__` ever
hands the object back to a caller, on every store this class has ever
opened, so the "pre-versioning store" `PRAGMA user_version` had to
distinguish by content has no RocksDB analogue to keep -- it is
simulated in the tests below, as it already was for SQLite, since
nothing in this tree still writes one on purpose.

## Corruption is an error this tree now classifies itself

`rocksdict` raises a bare `Exception` on a checksum mismatch, its
message beginning `Corruption:` -- only `DbClosedError` is typed, per
`rocksdict`'s own `.pyi`. `get`, `__iter__` and the open itself
(`__init__`, including the schema-version check it runs before handing
the object back) each catch it there and raise this tree's own
`StoreCorruptionError` instead, string-matched rather than typed for
the same reason `rocksdict` itself gives it no type.
`exceptions.py`'s own docstring for that class argues why it is not
`ChainstateInconsistencyError`: this store is `PeerDB`'s own as well as
every chainstate index's, and a corrupted address book is not a
chainstate concern.

**A point lookup and a scan report corruption differently, and only one
of them raises on its own.** `Rdict.get` -- what `get` and the
schema-version check above both call -- raises the moment it meets a
corrupted block, measured directly. `Rdict.items` and `Rdict.keys` do
not: RocksDB's own iterator, underneath either, answers a corrupted
block the way it answers the genuine end of the store -- `Valid()`
turns false, and the fault is only ever in a separate `status()` call,
which neither wrapper makes. Measured directly, twice: over a store
with one corrupted block partway through, `items()` returned every pair
before that block and then stopped, silently, and `keys()` on a store
whose only intact block was the corrupted one returned nothing at all,
indistinguishable from a genuinely empty store. The second is not a
theoretical risk -- `_has_any_data` is exactly the read
`_check_schema_version` trusts to tell an empty store from one this
class predates, so trusting `keys()` there would have let a corrupted,
version-less store be silently stamped current rather than refused.
`__iter__` and `_has_any_data` below both go through the lower-level
`Rdict.iter` instead -- `seek_to_first`, walk `valid()`, and call
`status()` once after -- which is what makes them raise the same way
`get` already does.

## The lock stays, for a different reason than before

RocksDB is internally thread-safe -- unlike SQLite's connection under
`check_same_thread=False`, nothing here needs the `RLock` to serialize
an ordinary `get`/`put`/`delete` across threads. Two measurements taken
directly against this file's own shape are why it stays regardless.
First: a live iterator still holds the directory `LOCK` after `close()`
returns -- `Rdict.close()` on the handle this file calls `self._db`
succeeds and returns, but a second `Rdict` opened on the same path
still refuses with the OS-level lock held, for as long as anything --
an iterator, a second column-family handle obtained through
`get_column_family` -- keeps the underlying handle alive from Python's
own side. `__iter__` below reads the whole store into a list under the
lock and drops the iterator before returning, exactly as it already did
for SQLite's own, different reason (a cursor stepped on by a second
statement on the same connection); `close` closes both the default and
the meta column-family handles this file keeps open, for the same
reason. Second: `close()` racing an unguarded `get()` from another
thread does not corrupt anything or crash the process the way CPython's
own `sqlite3` used to -- but it is not silent either, measured directly,
three runs, same result each time: `RuntimeError: Already mutably
borrowed`, `rocksdict`'s own Rust layer refusing the race rather than
letting it through undefined. The `RLock` below is what keeps a
caller from ever seeing that message: every read and every close take
it, so a `close()` from one thread simply waits for a `get()` already
in progress on another, the same shape `test_close_waits_for_whoever
_is_using_the_connection` already pins.

**A crash before `Chainstate.flush` writes costs whatever is staged
since the last one, and never a torn store.** `main._finalize_fork` no
longer writes `BlockIndex`'s and `FilterIndex`'s own changes on every
connected block: `BlockIndex.stage_status` and `FilterIndex`'s own
`pending` (`filter_index.py`'s docstring) hold them the way `UtxoIndex`
already held its own spends and creations, and `Chainstate.flush` writes
all three into the one `write_batch` this store already gives a caller
-- one RocksDB `WriteBatch`, committed whole or not at all (`write_batch`'s
own docstring below) -- once `UtxoIndex.should_flush` says the staged
UTXO cache has reached its own bound (`utxo_index.py`'s `_FLUSH_BOUND`),
or once `Chainstate.close` is called. This is what
btclib-org/btclib-node#586 measured: 3.53 billion inputs, one `db.get`
and one `db.delete` apiece, and holding several blocks' own changes
staged rather than committing each block's alone is the only lever on
that, blocks connecting one at a time on this store's own single writer.
Crash atomicity itself -- the property this whole recovery design rests
on -- was measured directly against both stores rather than assumed to
carry over: a child process writing two-key batches forever, `kill -9`
at a random moment, reopen, check every pair whole, six runs each,
**0 torn batches** of roughly 570 000 committed on RocksDB against
roughly 165 000 on sqlite3 in the same wall time
(btclib-org/btclib-node#641).

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
block index (`BlockTreeDB`) and its coins cache (`CCoinsViewDB`) are two
separate LevelDB instances, and `Chainstate::FlushStateToDisk`
(`validation.cpp`, read at bitcoin/bitcoin@ca7162cde5) writes them in
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

This store never needs that: `write_batch` is one RocksDB `WriteBatch`
regardless of how many keys it touches, so there is no sequence of
several separately-committed writes inside one flush for a crash to land
between, and so no marker to record one in progress. Bounding by entries
rather than by write-batch bytes (`utxo_index.py` argues that choice) is
part of the same shape -- one flush, one commit, the whole of it or
none.
"""

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rocksdict import (
    BlockBasedOptions,
    Cache,
    DBCompressionType,
    Options,
    Rdict,
    ReadOptions,
    WriteBatch,
    WriteOptions,
)

from btclib_node.exceptions import (
    IncompatibleStoreError,
    StoreClosedError,
    StoreCorruptionError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["KeyValueStore"]

# What this store itself wrote before this class existed, and this one
# never will. A datadir written by the sqlite3 store this class
# replaced (#107, #641) cannot be read by it, and starting an empty
# chain over the top of one is a worse answer than saying so. The
# module docstring's own "The datadir marker inverts" is where this is
# argued against the LevelDB-shaped marker it replaces.
_SQLITE_MARKER = "index.sqlite"

# The module docstring's own "_SCHEMA_VERSION moves to its own column
# family" is where keeping this out of the default column family's own
# key order, rather than a row in it, is argued.
_META_COLUMN_FAMILY = "meta"
_VERSION_KEY = b"schema-version"

# Bumped whenever a caller changes the shape of what it keeps under a
# key, or the shape a file this class does not itself hold (block_db's
# own .blk/.rev files) is read against.
_SCHEMA_VERSION = 1

# Core's own default `-dbcache`, and what it gives the coins database
# specifically: the module docstring's own configuration section
# derives this arithmetic from `node/caches.cpp` and `kernel/caches.h`,
# both read at bitcoin/bitcoin@ca7162cde5.
_N_CACHE_SIZE = 8 * 1024 * 1024  # 8 MiB


def _make_options() -> Options:
    """Build the `Options` every column family of this store opens with.

    Every setting here is Core's own, cited in the module docstring's
    own configuration section rather than repeated beside each line.
    """
    options = Options(raw_mode=True)
    options.create_if_missing(create_if_missing=True)
    options.create_missing_column_families(create_missing_cfs=True)
    # paranoid_checks = true (dbwrapper.cpp:150,162),
    # at bitcoin/bitcoin@ca7162cde5
    options.set_paranoid_checks(True)
    # compression = kNoCompression (dbwrapper.cpp:145),
    # at bitcoin/bitcoin@ca7162cde5
    options.set_compression_type(DBCompressionType.none())
    # write_buffer_size = nCacheSize / 4 (dbwrapper.cpp:143),
    # at bitcoin/bitcoin@ca7162cde5
    options.set_write_buffer_size(_N_CACHE_SIZE // 4)

    block_based = BlockBasedOptions()
    # 10 bits/key is LevelDB's own NewBloomFilterPolicy(10), and
    # DBParams.bloom_filter defaults to true (dbwrapper.h:54,
    # dbwrapper.cpp:144, at bitcoin/bitcoin@ca7162cde5). This is the
    # storage engine's own bloom filter, over the bytes of every key
    # this store holds, unrelated to BIP37's deprecated,
    # peer-relay one.
    block_based.set_bloom_filter(bits_per_key=10, block_based=False)
    # block_cache = NewLRUCache(nCacheSize / 2) (dbwrapper.cpp:142),
    # at bitcoin/bitcoin@ca7162cde5
    block_based.set_block_cache(Cache(_N_CACHE_SIZE // 2))
    options.set_block_based_table_factory(block_based)
    return options


# rocksdict's own ReadOptions default already verifies checksums; set
# explicitly anyway, the way Core's own CDBWrapper constructor does
# rather than relying on the vendor's default (dbwrapper.cpp:248-249,
# at bitcoin/bitcoin@ca7162cde5). The iterator's own copy additionally
# turns off fill_cache, matching iteroptions.fill_cache = false
# (dbwrapper.cpp:250, same sha): a full walk of the store is not this
# store's own working set.
_READ_OPTIONS = ReadOptions()
_READ_OPTIONS.set_verify_checksums(True)

_ITER_READ_OPTIONS = ReadOptions()
_ITER_READ_OPTIONS.set_verify_checksums(True)
# rocksdict's own .pyi types this as a no-argument method; the
# installed extension itself takes the bool its own docstring
# describes ("Default: true"), measured directly against
# rocksdict==0.3.29 rather than trusted from the stub -- mypy is told
# to trust the stub anyway, since the stub and not the compiled
# extension is what it can see
_ITER_READ_OPTIONS.fill_cache(v=False)  # type: ignore[call-arg]

# sync=False is WriteOptions' own default; passed explicitly so a
# reader sees the decision the module docstring's own configuration
# section argues, rather than a default nothing here states.
_WRITE_OPTIONS = WriteOptions()


class KeyValueStore:
    """An ordered store of octets by octets, in one directory.

    One `Rdict` handle on the default column family, one more on the
    `_META_COLUMN_FAMILY` beside it, and a lock around every use of
    either. RocksDB is internally thread-safe on its own -- the module
    docstring's own "The lock stays, for a different reason than before"
    is where the two measurements that keep the `RLock` here anyway are
    argued.

    Reentrant, because a batch holds the lock for its whole block and
    the writes inside it come back through the same door.
    """

    def __init__(self, path: str | Path) -> None:
        """Open (or create) the store at `path`, refusing a sqlite3 one."""
        self.path = Path(path)
        self.path.mkdir(exist_ok=True, parents=True)
        if (self.path / _SQLITE_MARKER).exists():
            err_msg = f"{self.path} holds a sqlite3 database, which this "
            err_msg += "version cannot read: delete the directory and sync "
            err_msg += "again"
            raise IncompatibleStoreError(err_msg)

        self._lock = threading.RLock()
        self._closed = False
        self._pending_batch: WriteBatch | None = None
        try:
            self._db = Rdict(
                str(self.path),
                _make_options(),
                column_families={_META_COLUMN_FAMILY: _make_options()},
            )
        except Exception as exc:
            # "Corruption is an error this tree now classifies itself"
            # argues the string match this raises through
            self._raise_if_corrupted(exc)
            raise
        self._meta = self._db.get_column_family(_META_COLUMN_FAMILY)
        self._check_schema_version()

    def _raise_if_corrupted(self, exc: Exception) -> None:
        """Turn `rocksdict`'s own corruption report into this tree's.

        `rocksdict` gives no type to string-match against -- only
        `DbClosedError` is typed, per its own `.pyi` -- so the message
        itself, always beginning `Corruption:` on a checksum mismatch,
        is what this reads. A caller that reaches here without a match
        falls through and re-raises the original exception unchanged,
        this method having nothing more to say about it.
        """
        if str(exc).startswith("Corruption:"):
            err_msg = f"{self.path}: {exc}"
            raise StoreCorruptionError(err_msg) from exc

    def _has_any_data(self) -> bool:
        """Whether the default column family already holds a key.

        One key, peeled off the front of an ascending walk, rather than
        every key counted. Through `Rdict.iter` and not the
        higher-level `Rdict.keys`, for the same reason `__iter__` below
        is: `keys()` answers a corrupted first block the same way it
        answers a genuinely empty store, nothing there to see, and this
        is exactly the read `_check_schema_version` trusts to tell an
        empty store from one this class predates -- trusting `keys()`
        here would let a corrupted, version-less store be silently
        stamped current instead of refused.
        """
        iterator = self._db.iter(read_opt=_READ_OPTIONS)
        iterator.seek_to_first()
        valid = iterator.valid()
        try:
            iterator.status()
        except Exception as exc:
            self._raise_if_corrupted(exc)
            raise
        return valid

    def _check_schema_version(self) -> None:
        """Refuse a store this version's own shape cannot make sense of.

        The module docstring's own "`_SCHEMA_VERSION` moves to its own
        column family" argues the two states an absent `_VERSION_KEY`
        can mean, and why a store this class itself ever wrote cannot
        be one of them by the time this runs.

        A refusal closes the connection it just opened before raising:
        `__init__` never hands this object back to whoever asked for
        one, so nothing else is left holding a reference to close it.
        """
        try:
            stored = self._meta.get(_VERSION_KEY, read_opt=_READ_OPTIONS)
        except Exception as exc:
            self._raise_if_corrupted(exc)
            raise
        if stored == _SCHEMA_VERSION.to_bytes(4, "big"):
            return
        if stored is None and not self._has_any_data():
            self._meta.put(
                _VERSION_KEY,
                _SCHEMA_VERSION.to_bytes(4, "big"),
                write_opt=_WRITE_OPTIONS,
            )
            return
        version = int.from_bytes(stored, "big") if stored else 0
        err_msg = f"{self.path} holds a version {version} store, which this "
        err_msg += f"version ({_SCHEMA_VERSION}) cannot read: delete the "
        err_msg += "directory and sync again"
        self.close()
        raise IncompatibleStoreError(err_msg)

    def _ensure_open(self) -> None:
        if self._closed:
            err_msg = f"the store at {self.path} is closed"
            raise StoreClosedError(err_msg)

    def get(self, key: bytes) -> bytes | None:
        """Return the value stored under a key, or None."""
        with self._lock:
            self._ensure_open()
            try:
                value = self._db.get(key, read_opt=_READ_OPTIONS)
            except Exception as exc:
                self._raise_if_corrupted(exc)
                raise
            return cast("bytes | None", value)

    def put(self, key: bytes, value: bytes) -> None:
        """Store a value under a key, replacing what was there."""
        with self._lock:
            self._ensure_open()
            if self._pending_batch is not None:
                self._pending_batch.put(key, value)
                return
            self._db.put(key, value, write_opt=_WRITE_OPTIONS)

    def delete(self, key: bytes) -> None:
        """Remove a key, whether or not it was there."""
        with self._lock:
            self._ensure_open()
            if self._pending_batch is not None:
                self._pending_batch.delete(key)
                return
            self._db.delete(key, write_opt=_WRITE_OPTIONS)

    def __iter__(self) -> Iterator[tuple[bytes, bytes]]:
        """Walk every pair, in ascending key order.

        Read whole under the lock rather than handed out as an
        iterator: the module docstring's own "The lock stays, for a
        different reason than before" is where a live iterator being
        found to keep this store's own directory `LOCK` held past
        `close()` is measured -- the reason this still runs the whole
        walk to a list before returning has changed, but the shape has
        not.

        Walked through `Rdict.iter`, not the higher-level `Rdict.items`:
        RocksDB's own iterator does not raise when it meets a corrupted
        block, it stops -- `valid()` turns `False` the same way it would
        at the genuine end of the store, and the only place the reason
        ever surfaces is `status()`, called once here after the walk
        rather than left unread. `items()` wraps that same iterator and
        never calls `status()` of its own -- measured directly: over a
        store with one corrupted block partway through, it returned
        every pair before the corrupted one and then stopped, silently,
        which would make a truncated store look like the whole of it to
        every caller of this method.
        """
        with self._lock:
            self._ensure_open()
            rows: list[tuple[bytes, bytes]] = []
            iterator = self._db.iter(read_opt=_ITER_READ_OPTIONS)
            iterator.seek_to_first()
            while iterator.valid():
                rows.append((iterator.key(), iterator.value()))
                iterator.next()
            try:
                iterator.status()
            except Exception as exc:
                self._raise_if_corrupted(exc)
                raise
            return iter(rows)

    @contextmanager
    def write_batch(self) -> Iterator[KeyValueStore]:
        """Write everything in the block, or nothing at all.

        The lock is held for the whole batch, so nothing else reaches
        either column-family handle mid-batch; the writes inside
        re-enter it through `put`/`delete` above, which is what makes it
        an `RLock`.

        Every write inside the block goes into one `rocksdict`
        `WriteBatch`, held here rather than written through as it
        arrives, and reaches the store in one call to `Rdict.write` only
        once the block exits without raising -- an exception unwinding
        it instead leaves `self._pending_batch` cleared and nothing
        written, `put`/`delete` never having reached `self._db` at all.
        There is no RocksDB counterpart to `BEGIN IMMEDIATE`'s own
        timing (SQLite's write lock taken when a batch opens rather
        than at its first write, so a second writer in another process
        waited from the start): RocksDB's own directory `LOCK` is
        already held exclusively from the moment this store's own
        `__init__` opened it, well before any batch, so there is no
        window for a second writer to find open here that opening this
        store at all has not already closed.

        A batch does not nest. One slot holds the pending batch, so an
        inner `write_batch` would commit its own writes on its own exit,
        outside the outer block's atomicity and with nothing to say so;
        it raises instead, the way SQLite's own nested `BEGIN` did.
        """
        with self._lock:
            self._ensure_open()
            if self._pending_batch is not None:
                err_msg = "write_batch does not nest"
                raise RuntimeError(err_msg)
            batch = WriteBatch(raw_mode=True)
            self._pending_batch = batch
            try:
                yield self
            except BaseException:
                # BaseException and not Exception: a KeyboardInterrupt
                # or a cancelled task through here would otherwise leave
                # `self._pending_batch` set to a batch nothing ever
                # commits or clears
                self._pending_batch = None
                raise
            self._pending_batch = None
            self._db.write(batch, write_opt=_WRITE_OPTIONS)

    def close(self) -> None:
        """Close both column-family handles, once, and refuse later use."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._db.close()
            self._meta.close()

    @property
    def closed(self) -> bool:
        """Whether `close` has already been called."""
        return self._closed
