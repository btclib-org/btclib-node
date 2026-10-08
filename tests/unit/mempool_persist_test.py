# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`mempool.dat`: its format, what is written, and what a load keeps.

The layout was measured on a file bitcoind v31.1.0 wrote with
`savemempool`; `tests/integration/mempool_persist_test.py` compares the
two nodes' files byte for byte.
"""

import re
import secrets
import struct
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from btclib.exceptions import BTClibValueError
from btclib.obfuscation import obfuscate
from btclib.script.witness import Witness
from btclib.tx.limits import COINBASE_MATURITY
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node.chains import RegTest
from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from btclib_node.mempool_persist import (
    MempoolFile,
    dump_mempool,
    load_mempool,
    read_mempool_file,
    serialize_mempool,
)
from tests import (
    LogLines,
    anyone_can_spend,
    anyone_can_spend_script_sig,
    generate_random_chain,
)
from tests.unit.main_test import connect

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from btclib_node import Node

_KEY = bytes.fromhex("0123456789abcdef")


def a_tx(prev_out: tuple[bytes, int] | None = None, value: int = 10_000) -> Tx:
    """Return a spend of `prev_out`, or of an outpoint nothing holds."""
    txid, vout = prev_out or (secrets.token_bytes(32), 0)
    return Tx(
        version=2,
        lock_time=0,
        vin=[TxIn(OutPoint(txid, vout), anyone_can_spend_script_sig(), 0xFFFFFFFF)],
        vout=[TxOut(value, anyone_can_spend())],
    )


def a_witness_tx() -> Tx:
    """Return a transaction whose input carries a witness."""
    tx_in = TxIn(
        OutPoint(secrets.token_bytes(32), 1),
        b"",
        0xFFFFFFFD,
        Witness([b"\x01" * 72, b"\x02" * 33]),
    )
    return Tx(version=2, lock_time=0, vin=[tx_in], vout=[TxOut(1, anyone_can_spend())])


def run(load: Generator[None, None, bool]) -> bool:
    """Step a load to its end and return its answer."""
    while True:
        try:
            next(load)
        except StopIteration as stop:
            return bool(stop.value)


def a_file() -> MempoolFile:
    """Return a file's content with a witness, deltas and an unbroadcast set."""
    entries = [(a_tx(), 1_700_000_000, 0), (a_witness_tx(), 1_700_000_005, -42)]
    deltas = {secrets.token_bytes(32): 7, secrets.token_bytes(32): -(2**63)}
    unbroadcast = [entries[0][0].id, secrets.token_bytes(32)]
    return MempoolFile(_KEY, entries, deltas, unbroadcast)


def test_a_file_reads_back_what_was_written() -> None:
    """Version 2 with its key, version 1 without one."""
    content = a_file()
    for key in (_KEY, None):
        data = serialize_mempool(
            content.entries, content.deltas, content.unbroadcast, key
        )
        read = read_mempool_file(data)
        assert read.key == key
        assert [
            (tx.serialize(include_witness=True), t, d) for tx, t, d in read.entries
        ] == [
            (tx.serialize(include_witness=True), t, d) for tx, t, d in content.entries
        ]
        assert read.deltas == content.deltas
        assert read.unbroadcast == sorted(content.unbroadcast, key=lambda t: t[::-1])


def test_the_layout_is_core_s() -> None:
    """The version, the key in clear, then the rest XORed at its offset.

    The counts are a `u64` for the transactions and a compact size for the
    deltas and the unbroadcast set, whose txids are in internal byte order,
    sorted, as Core's `std::map` and `std::set` serialize.
    """
    tx = a_tx()
    deltas = {b"\x02" + b"\x00" * 31: 5, b"\x01" + b"\x00" * 30 + b"\x01": -1}
    data = serialize_mempool([(tx, 3, 4)], deltas, [tx.id], _KEY)
    assert data[:17] == struct.pack("<Q", 2) + b"\x08" + _KEY
    payload = obfuscate(data[17:], _KEY, 17)
    raw = tx.serialize(include_witness=True)
    expected = struct.pack("<Q", 1) + raw + struct.pack("<qq", 3, 4)
    # sorted by the internal bytes, which reverse the display bytes above
    expected += b"\x02" + b"\x00" * 31 + b"\x02" + struct.pack("<q", 5)
    expected += b"\x01" + b"\x00" * 30 + b"\x01" + struct.pack("<q", -1)
    expected += b"\x01" + tx.id[::-1]
    assert payload == expected
    v1 = serialize_mempool([(tx, 3, 4)], deltas, [tx.id], None)
    assert v1 == struct.pack("<Q", 1) + expected


def test_a_file_that_is_not_one_is_refused() -> None:
    """An unknown version, a key of another size, and a short file."""
    data = serialize_mempool([(a_tx(), 1, 0)], {}, [], _KEY)
    with pytest.raises(BTClibValueError, match="unknown version"):
        read_mempool_file(struct.pack("<Q", 3) + data[8:])
    with pytest.raises(BTClibValueError, match="invalid key size"):
        read_mempool_file(data[:8] + b"\x07" + data[9:])
    for size in (4, 30, len(data) - 1):
        with pytest.raises((EOFError, BTClibValueError)):
            read_mempool_file(data[:size])


def a_mempool() -> Mempool:
    """Return an empty mempool with a logger of its own."""
    return Mempool(Logger(debug=True))


def test_written_in_the_order_a_block_takes_them(tmp_path: Path) -> None:
    """Core's `CompareMainOrder`: a chunk by its feerate, parents first.

    `low` pays least but its child `rich` pays for both, so the two are
    one chunk of feerate 5, written before `mid`, which pays 3. A delta
    puts `other` first. `both` follows `wide` and `high`, its parents.
    """
    mempool = a_mempool()
    high, low, wide, other, mid = a_tx(), a_tx(), a_tx(), a_tx(), a_tx()
    rich = a_tx((low.id, 0))
    both = Tx(
        version=2,
        lock_time=0,
        vin=[TxIn(OutPoint(tx.id, 0), b"", 0xFFFFFFFF) for tx in (wide, high)],
        vout=[TxOut(1, anyone_can_spend())],
    )
    pairs = ((low, 100), (wide, 600), (other, 300), (high, 200), (rich, 900))
    for tx, fee in (*pairs, (mid, 300)):
        assert mempool.add_tx(tx, fee, 100)
    assert mempool.add_tx(both, 5_000, 100)
    mempool.prioritise(other.id, 4_000)
    path = tmp_path / "mempool.dat"
    assert dump_mempool(mempool, path)
    written = [tx.id for tx, _, _ in read_mempool_file(path.read_bytes()).entries]
    assert written[0] == other.id
    assert max(written.index(tx.id) for tx in (wide, high)) < written.index(both.id)
    assert written[-3:] == [low.id, rich.id, mid.id]


def test_what_is_written_is_the_mempool(tmp_path: Path) -> None:
    """Times in whole seconds, deltas beside the held and apart for the rest.

    The delta beside a held transaction is its modified fee less its fee,
    so a delta saturated at the `int64` bound is written as what applies.
    """
    mempool = a_mempool()
    tx, other = a_tx(), a_tx()
    mempool.add_tx(tx, 1_000, 100)
    mempool.add_tx(other, 1_000, 100)
    mempool.entry_times[tx.hash] = 1_700_000_000.9
    mempool.entry_times[other.hash] = 1_700_000_001.0
    mempool.prioritise(other.id, 2**63 - 1)
    unheld = secrets.token_bytes(32)
    mempool.prioritise(unheld, -5)
    mempool.mark_broadcast_locally(tx.id)
    path = tmp_path / "mempool.dat"
    lines = LogLines()
    mempool.logger.addHandler(lines)
    assert dump_mempool(mempool, path)
    read = read_mempool_file(path.read_bytes())
    assert read.key is not None
    assert [(t.id, entry_time, delta) for t, entry_time, delta in read.entries] == [
        (other.id, 1_700_000_001, 2**63 - 1 - 1_000),
        (tx.id, 1_700_000_000, 0),
    ]
    assert read.deltas == {unheld: -5}
    assert read.unbroadcast == [tx.id]
    assert not path.with_name("mempool.dat.new").exists()
    assert "Writing 2 mempool transactions to file..." in lines.messages
    assert "Writing 1 unbroadcast transactions to file." in lines.messages
    size = path.stat().st_size
    pattern = rf"Dumped mempool: [0-9.]+s to copy, [0-9.]+s to dump, {size} bytes"
    assert re.fullmatch(pattern + " dumped to file", lines.messages[-1])
    assert dump_mempool(mempool, path, v1=True)
    assert read_mempool_file(path.read_bytes()).key is None


def test_a_dump_that_fails_leaves_the_file_as_it_was(tmp_path: Path) -> None:
    """A `.new` file that cannot be opened is not logged; a failed rename is."""
    mempool = a_mempool()
    mempool.add_tx(a_tx(), 1_000)
    lines = LogLines()
    mempool.logger.addHandler(lines)
    path = tmp_path / "mempool.dat"
    path.write_bytes(b"old")
    new = tmp_path / "mempool.dat.new"
    new.mkdir()
    assert not dump_mempool(mempool, path)
    assert path.read_bytes() == b"old"
    assert lines.messages == []
    new.rmdir()
    path.unlink()
    path.mkdir()
    (path / "in the way").touch()
    assert not dump_mempool(mempool, path)
    assert (path / "in the way").exists()
    assert lines.messages[-1].startswith("Failed to dump mempool: ")


@pytest.fixture
def funded(regtest_node: Callable[..., Node]) -> tuple[Node, Tx]:
    """Give a node with a mature coinbase and a spend of it into four outputs.

    The fan-out is not held: each test decides.
    """
    node = regtest_node()
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    coinbase = chain[0].transactions[0]
    fan = Tx(
        version=2,
        lock_time=0,
        vin=[TxIn(OutPoint(coinbase.id, 0), anyone_can_spend_script_sig(), 0xFFFFFFFF)],
        vout=[
            TxOut(coinbase.vout[0].value // 4 - 1_000, anyone_can_spend())
            for _ in range(4)
        ],
    )
    return node, fan


def spend(parent: Tx, vout: int, fee: int) -> Tx:
    """Return a spend of `parent`'s output `vout` paying `fee`."""
    return a_tx((parent.id, vout), parent.vout[vout].value - fee)


def write(
    path: Path,
    entries: list[tuple[Tx, int, int]],
    deltas: dict[bytes, int],
    unbroadcast: list[bytes],
) -> None:
    """Write a `mempool.dat` of `entries`, `deltas` and `unbroadcast`."""
    path.write_bytes(serialize_mempool(entries, deltas, unbroadcast, _KEY))


def test_a_load_checks_each_transaction_again(funded: tuple[Node, Tx]) -> None:
    """A valid one is held with its time, delta and unbroadcast mark.

    One spending an output nothing holds fails, one too old expires, and
    one already held counts as already there. The deltas of transactions
    not held are kept too.
    """
    node, fan = funded
    path = node.data_dir / "mempool.dat"
    now = int(time.time())
    child = spend(fan, 0, 2_000)
    orphan = a_tx()
    old = spend(fan, 1, 2_000)
    held = spend(fan, 2, 2_000)
    unheld = secrets.token_bytes(32)
    entries = [
        (fan, now - 60, 300),
        (child, now - 30, 0),
        (orphan, now, 0),
        (old, now - 336 * 3600, 5),
        (held, now, 0),
    ]
    write(path, entries, {unheld: 9}, [fan.id, unheld])
    lines = LogLines()
    node.logger.addHandler(lines)
    tip = len(node.chainstate.block_index.active_chain) - 1
    node.mempool.add_tx(held, 2_000, height=tip)
    assert run(load_mempool(node, path))
    mempool = node.mempool
    assert set(mempool.txid_index) == {fan.id, child.id, held.id}
    assert int(mempool.entry_times[fan.hash]) == now - 60
    assert int(mempool.entry_times[child.hash]) == now - 30
    assert mempool.delta(fan.id) == 300
    assert mempool.delta(old.id) == 5
    assert mempool.delta(unheld) == 9
    assert mempool.unbroadcast == {fan.id}
    progress = "Progress loading mempool transactions from file: "
    assert [line for line in lines.messages if line.startswith(progress)] == [
        f"{progress}{20 * tried}% (tried {tried}, {5 - tried} remaining)"
        for tried in range(1, 5)
    ]
    assert lines.messages[-1] == (
        "Imported mempool transactions from file: 2 succeeded, 1 failed, "
        "1 expired, 1 already there, 2 waiting for initial broadcast"
    )


def test_a_loaded_transaction_is_handed_to_the_fee_estimator(
    funded: tuple[Node, Tx],
) -> None:
    """Core's `AcceptToMemoryPool` signals it as it does any other."""
    node, fan = funded
    path = node.data_dir / "mempool.dat"
    write(path, [(fan, int(time.time()), 0)], {}, [])
    with patch.object(node.fee_estimator, "process_transaction") as process:
        assert run(load_mempool(node, path))
    process.assert_called_once()
    assert process.call_args.args[0] == fan.id


def test_an_import_s_options_leave_out_what_they_name(funded: tuple[Node, Tx]) -> None:
    """The current time for the file's, and no delta nor unbroadcast mark."""
    node, fan = funded
    path = node.data_dir / "mempool.dat"
    write(path, [(fan, 1, 300)], {a_tx().id: 9}, [fan.id])
    before = time.time()
    load = load_mempool(
        node,
        path,
        use_current_time=True,
        apply_fee_delta_priority=False,
        apply_unbroadcast_set=False,
    )
    assert run(load)
    assert node.mempool.entry_times[fan.hash] >= int(before)
    assert node.mempool.deltas == {}
    assert node.mempool.unbroadcast == set()


def test_a_transaction_a_full_mempool_evicts_at_once_fails(
    funded: tuple[Node, Tx],
) -> None:
    """As Core counts one `AcceptToMemoryPool` refuses for room."""
    node, fan = funded
    path = node.data_dir / "mempool.dat"
    write(path, [(fan, int(time.time()), 0)], {}, [])
    node.mempool.bytesize_limit = 1
    lines = LogLines()
    node.logger.addHandler(lines)
    assert run(load_mempool(node, path))
    assert node.mempool.size == 0
    assert lines.messages[-1].startswith(
        "Imported mempool transactions from file: 0 succeeded, 1 failed, "
    )


def test_a_load_that_stops_parsing_keeps_what_it_added(
    funded: tuple[Node, Tx],
) -> None:
    """The transactions before the damage stay; the load answers false."""
    node, fan = funded
    path = node.data_dir / "mempool.dat"
    data = serialize_mempool(
        [(fan, int(time.time()), 0), (spend(fan, 0, 2_000), 0, 0)], {}, [], _KEY
    )
    path.write_bytes(data[:-20])
    lines = LogLines()
    node.logger.addHandler(lines)
    assert not run(load_mempool(node, path))
    assert set(node.mempool.txid_index) == {fan.id}
    assert lines.messages[-1].startswith("Failed to deserialize mempool data on file: ")


def test_a_missing_or_unknown_file_loads_nothing(funded: tuple[Node, Tx]) -> None:
    """A missing file is logged; an unknown version is not."""
    node, _ = funded
    path = node.data_dir / "mempool.dat"
    lines = LogLines()
    node.logger.addHandler(lines)
    assert not run(load_mempool(node, path))
    assert lines.messages == ["Failed to open mempool file. Continuing anyway."]
    path.write_bytes(struct.pack("<Q", 3) + b"\x00" * 16)
    assert not run(load_mempool(node, path))
    assert len(lines.messages) == 1
    assert node.mempool.size == 0
