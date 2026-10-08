# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Core's `mempool.dat`: the mempool written at shutdown and read at start.

The format is Core's `DumpMempool` and `LoadMempool`
(`src/node/mempool_persist.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
tag). The file opens with its version as a little-endian `u64`. Version 2
follows it with an obfuscation key and XORs the rest of the file with it
(`btclib.obfuscation`); version 1 has no key. Then come the number of
transactions and, for each, the transaction with its witness, its entry
time in seconds and its fee delta, both `int64`; the deltas of
transactions not held, Core's `std::map<Txid, CAmount>`; and the
unbroadcast set, Core's `std::set<Txid>`. Both are ordered by the txid's
internal bytes, the reverse of the display bytes this tree keeps.

Core writes the transactions in `CompareMainOrder`, the order a block
takes them; `Mempool.mining_order_keys` gives it, parents first.

`load_mempool` re-validates each transaction as a new one, as Core's
`AcceptToMemoryPool` does with `bypass_limits` false: the mempool is
trimmed to its limit after each, and the fee estimator is told. It is a
generator so that `Node` serves its peers between transactions, as Core
loads on a thread of its own.
"""

import os
import struct
import time
from collections import Counter
from io import BytesIO
from typing import TYPE_CHECKING, NamedTuple

from btclib import var_int
from btclib.exceptions import BTClibTypeError, BTClibValueError
from btclib.obfuscation import KEY_SIZE, obfuscate, parse_key, serialize_key
from btclib.tx.tx import Tx

from btclib_node.exceptions import MissingPrevoutError
from btclib_node.fee_estimator import track_accepted
from btclib_node.main import verify_mempool_acceptance

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Mapping
    from pathlib import Path

    from btclib_node import Node
    from btclib_node.log import Logger
    from btclib_node.mempool import Mempool

__all__ = [
    "FILENAME",
    "MEMPOOL_DUMP_VERSION",
    "MEMPOOL_DUMP_VERSION_NO_XOR_KEY",
    "MempoolFile",
    "dump_mempool",
    "load_mempool",
    "read_mempool_file",
    "serialize_mempool",
]

# Core's two versions, and its default `-mempoolexpiry` of 336 hours
# (`src/kernel/mempool_options.h`, same tag): an entry older than that is
# not loaded. A constant, as this node has no `-mempoolexpiry`: its held
# transactions never expire (btclib-org/btclib-node#1815).
MEMPOOL_DUMP_VERSION_NO_XOR_KEY = 1
MEMPOOL_DUMP_VERSION = 2
_EXPIRY = 336 * 60 * 60

# `MEMPOOL_FILENAME`, in the chain's own data directory
FILENAME = "mempool.dat"

_U64 = struct.Struct("<Q")
_I64 = struct.Struct("<q")


class MempoolFile(NamedTuple):
    """What a `mempool.dat` holds.

    `key` is `None` for version 1. `entries` are `(tx, time, delta)`;
    `deltas` and `unbroadcast` are keyed by txid, in this tree's display
    bytes.
    """

    key: bytes | None
    entries: list[tuple[Tx, int, int]]
    deltas: dict[bytes, int]
    unbroadcast: list[bytes]


def serialize_mempool(
    entries: Iterable[tuple[Tx, int, int]],
    deltas: Mapping[bytes, int],
    unbroadcast: Iterable[bytes],
    key: bytes | None,
) -> bytes:
    """Return a `mempool.dat`, version 1 where `key` is `None`.

    `entries` are written in the order given; `deltas` and `unbroadcast`
    are sorted as Core's containers are.
    """
    entries = list(entries)
    body = [_U64.pack(len(entries))]
    for tx, entry_time, delta in entries:
        body += [tx.serialize(include_witness=True), _I64.pack(entry_time)]
        body.append(_I64.pack(delta))
    body.append(var_int.serialize(len(deltas)))
    for txid in sorted(deltas, key=lambda txid: txid[::-1]):
        body += [txid[::-1], _I64.pack(deltas[txid])]
    held = sorted(txid[::-1] for txid in unbroadcast)
    body += [var_int.serialize(len(held)), *held]
    payload = b"".join(body)
    if key is None:
        return _U64.pack(MEMPOOL_DUMP_VERSION_NO_XOR_KEY) + payload
    head = _U64.pack(MEMPOOL_DUMP_VERSION) + serialize_key(key)
    return head + obfuscate(payload, key, len(head))


def dump_mempool(mempool: Mempool, path: Path, *, v1: bool = False) -> bool:
    """Write `mempool` to `path`, and answer whether it was written.

    Core's `DumpMempool`: a fresh random key unless `v1`, the file
    written beside `path` with `.new` appended, synced, then renamed
    over `path`. Where the `.new` file cannot be opened the answer is
    false and nothing is logged, as in Core; any later failure is logged.
    Either leaves `path` as it was. The delta written beside a
    transaction is its modified fee less its fee, as Core's
    `TxMempoolInfo::nFeeDelta`.
    """
    logger = mempool.logger
    start = time.monotonic()
    entries = []
    keys = mempool.mining_order_keys(mempool.transactions)
    for wtxid in sorted(keys, key=keys.__getitem__):
        delta = mempool.modified_fee(wtxid) - mempool.fees[wtxid]
        entry_time = int(mempool.entry_times[wtxid])
        entries.append((mempool.transactions[wtxid], entry_time, delta))
    deltas = {
        txid: delta
        for txid, delta in mempool.deltas.items()
        if txid not in mempool.txid_index
    }
    unbroadcast = set(mempool.unbroadcast)
    mid = time.monotonic()
    new = path.with_name(path.name + ".new")
    try:
        file = new.open("wb")
    except OSError:
        return False
    logger.info("Writing %d mempool transactions to file...", len(entries))
    logger.info("Writing %d unbroadcast transactions to file.", len(unbroadcast))
    key = None if v1 else os.urandom(KEY_SIZE)
    data = serialize_mempool(entries, deltas, unbroadcast, key)
    try:
        with file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        new.replace(path)
    except OSError as error:
        logger.info("Failed to dump mempool: %s. Continuing anyway.", error)
        return False
    logger.info(
        "Dumped mempool: %.3fs to copy, %.3fs to dump, %d bytes dumped to file",
        mid - start,
        time.monotonic() - mid,
        len(data),
    )
    return True


class _Reader:
    """A `mempool.dat` read field by field, as `LoadMempool` reads it.

    Each read raises at the end of the data or on a field that does not
    parse, as Core's `AutoFile` does.
    """

    def __init__(self, data: bytes) -> None:
        """Read the version and the key, and the count of transactions.

        `version` is `None` for a version Core does not know, and
        nothing past it is read.
        """
        self.count = 0
        stream = BytesIO(data)
        self.version: int | None = _U64.unpack(self._read(stream, 8))[0]
        if self.version == MEMPOOL_DUMP_VERSION_NO_XOR_KEY:
            self.key: bytes | None = None
        elif self.version == MEMPOOL_DUMP_VERSION:
            self.key = parse_key(stream)
        else:
            self.version = None
            return
        offset = stream.tell()
        payload = data[offset:]
        if self.key is not None:
            payload = obfuscate(payload, self.key, offset)
        self.stream = BytesIO(payload)
        self.count = _U64.unpack(self._read(self.stream, 8))[0]

    @staticmethod
    def _read(stream: BytesIO, size: int) -> bytes:
        data = stream.read(size)
        if len(data) != size:
            err_msg = "end of file"
            raise EOFError(err_msg)
        return data

    def entry(self) -> tuple[Tx, int, int]:
        """Read one transaction with its time and its delta."""
        tx = Tx.parse(self.stream, check_validity=False)
        entry_time = _I64.unpack(self._read(self.stream, 8))[0]
        return tx, entry_time, _I64.unpack(self._read(self.stream, 8))[0]

    def deltas(self) -> dict[bytes, int]:
        """Read the deltas of the transactions not held."""
        deltas = {}
        for _ in range(var_int.parse(self.stream)):
            txid = self._read(self.stream, 32)[::-1]
            deltas[txid] = _I64.unpack(self._read(self.stream, 8))[0]
        return deltas

    def unbroadcast(self) -> list[bytes]:
        """Read the unbroadcast set."""
        count = var_int.parse(self.stream)
        return [self._read(self.stream, 32)[::-1] for _ in range(count)]


# what a file that does not parse raises: a short read, or a field btclib
# refuses, a key of the wrong size among them
_UNREADABLE = (EOFError, BTClibValueError)


def read_mempool_file(data: bytes) -> MempoolFile:
    """Return what `data` holds, raising where it is not a `mempool.dat`."""
    reader = _Reader(data)
    if reader.version is None:
        err_msg = "unknown version"
        raise BTClibValueError(err_msg)
    entries = [reader.entry() for _ in range(reader.count)]
    return MempoolFile(reader.key, entries, reader.deltas(), reader.unbroadcast())


def _outcome(node: Node, tx: Tx, entry_time: int, now: int) -> str:
    """Try one transaction of the file, and return what became of it.

    It is added as a new transaction held from `entry_time`, unless it
    entered the mempool `_EXPIRY` or more before `now`.
    """
    if entry_time <= now - _EXPIRY:
        return "expired"
    mempool = node.mempool
    try:
        tx.assert_valid()
        fee, vsize, weight = verify_mempool_acceptance(node, tx)
    except MissingPrevoutError, BTClibValueError, BTClibTypeError:
        return "failed" if mempool.get_tx(tx.id) is None else "already there"
    tip_height = len(node.chainstate.block_index.active_chain) - 1
    if not mempool.add_tx(tx, fee, vsize, height=tip_height, weight=weight):
        return "failed"
    mempool.entry_times[tx.hash] = entry_time
    # Core's `AcceptToMemoryPool` signals `TransactionAddedToMempool` as
    # for any other transaction
    track_accepted(node, tx)
    return "succeeded"


def _progress(logger: Logger, tried: int, total: int) -> None:
    """Log each tenth of a load the first time it is passed, as Core does."""
    done = 100 * tried // total
    if done // 10 > 100 * max(tried - 1, 0) // total // 10:
        logger.info(
            "Progress loading mempool transactions from file: "
            "%d%% (tried %d, %d remaining)",
            done,
            tried,
            total - tried,
        )


def load_mempool(
    node: Node,
    path: Path,
    *,
    use_current_time: bool = False,
    apply_fee_delta_priority: bool = True,
    apply_unbroadcast_set: bool = True,
) -> Generator[None, None, bool]:
    """Load `path` into `node.mempool`, a transaction per step.

    Core's `LoadMempool` with its `ImportMempoolOptions`, whose defaults
    are the load at start. Answers whether the whole file was read. A
    transaction refused, expired or already held is counted and skipped;
    a file that does not parse ends the load where it stops parsing,
    what was added before staying.
    """
    logger = node.logger
    mempool = node.mempool
    try:
        data = path.read_bytes()
    except OSError:
        logger.info("Failed to open mempool file. Continuing anyway.")
        return False
    now = int(time.time())
    counts = Counter[str]()
    try:
        reader = _Reader(data)
        if reader.version is None:
            return False
        logger.info("Loading %d mempool transactions from file...", reader.count)
        for tried in range(reader.count):
            _progress(logger, tried, reader.count)
            tx, entry_time, delta = reader.entry()
            if delta and apply_fee_delta_priority:
                mempool.prioritise(tx.id, delta)
            counts[
                _outcome(node, tx, now if use_current_time else entry_time, now)
            ] += 1
            yield
        deltas = reader.deltas()
        for txid, delta in deltas.items() if apply_fee_delta_priority else ():
            mempool.prioritise(txid, delta)
        txids = reader.unbroadcast()
    except _UNREADABLE as error:
        logger.info(
            "Failed to deserialize mempool data on file: %s. Continuing anyway.", error
        )
        return False
    if apply_unbroadcast_set:
        counts["unbroadcast"] = len(txids)
        for txid in txids:
            mempool.mark_broadcast_locally(txid)
    logger.info(
        "Imported mempool transactions from file: %d succeeded, %d failed, "
        "%d expired, %d already there, %d waiting for initial broadcast",
        counts["succeeded"],
        counts["failed"],
        counts["expired"],
        counts["already there"],
        counts["unbroadcast"],
    )
    return True
