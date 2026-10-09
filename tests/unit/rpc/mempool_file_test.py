# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`savemempool` and `importmempool`.

The refusals, their order and their words were measured against bitcoind
v31.1.0 on regtest; `tests/integration/mempool_persist_test.py` asks
bitcoind the same calls.
"""

import errno
import os
import secrets
import time
from io import SEEK_CUR, SEEK_SET, BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, override

import pytest
from bitcoin_core_rpc import RPCErrorCode

from btclib_node import mempool_persist
from btclib_node.mempool_persist import read_mempool_file, serialize_mempool
from btclib_node.rpc.callbacks import (
    arg_names,
    callbacks,
    import_mempool,
    named_only,
    save_mempool,
)
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT
from btclib_node.rpc.jsonrpc import transform_named_arguments
from tests import finish
from tests.unit.mempool_persist_test import (  # noqa: F401  (a fixture)
    funded,
    spend,
)

if TYPE_CHECKING:
    from btclib.tx.tx import Tx

    from btclib_node import Node

_CONN = cast("Any", None)
_KEY = bytes(range(8))


def refused(node: Node, params: Any) -> RpcError:
    """Return what `importmempool` refuses `params` with."""
    with pytest.raises(RpcError) as raised:
        finish(import_mempool(node, _CONN, params))
    return raised.value


def test_both_are_served_under_the_names_and_arguments_core_gives() -> None:
    """`options` carries the three named options, both in Blockchain."""
    assert callbacks["savemempool"] is save_mempool
    assert callbacks["importmempool"] is import_mempool
    assert arg_names["savemempool"] == ()
    assert named_only["importmempool"] == (
        "use_current_time",
        "apply_fee_delta_priority",
        "apply_unbroadcast_set",
    )
    params = transform_named_arguments(
        {"filepath": "f", "apply_unbroadcast_set": True},
        arg_names["importmempool"],
        named_only["importmempool"],
    )
    assert params == ["f", {"apply_unbroadcast_set": True}]
    assert CATEGORY["savemempool"] == CATEGORY["importmempool"] == "Blockchain"
    assert HELP_TEXT["importmempool"].startswith(
        'importmempool "filepath" ( options )\n'
    )


def test_savemempool_waits_for_the_load_then_names_the_file(
    funded: tuple[Node, Tx],  # noqa: F811
) -> None:
    """Refused until the load at start ends; `-persistmempoolv1` is read."""
    node, fan = funded
    with pytest.raises(RpcError) as raised:
        save_mempool(node, _CONN, [])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "The mempool was not loaded yet"
    node.mempool.load_tried = True
    node.mempool.add_tx(fan, 4_000)
    path = node.data_dir / "mempool.dat"
    assert save_mempool(node, _CONN, []) == {"filename": str(path)}
    read = read_mempool_file(path.read_bytes())
    assert read.key is not None
    assert [tx.id for tx, _, _ in read.entries] == [fan.id]
    node.config.persist_mempool_v1 = True
    save_mempool(node, _CONN, [])
    assert read_mempool_file(path.read_bytes()).key is None


def test_savemempool_refuses_where_the_file_cannot_be_written(
    funded: tuple[Node, Tx],  # noqa: F811
) -> None:
    """A directory in the way of the `.new` file."""
    node, _ = funded
    node.mempool.load_tried = True
    (node.data_dir / "mempool.dat.new").mkdir()
    with pytest.raises(RpcError) as raised:
        save_mempool(node, _CONN, [])
    assert raised.value.code == RPCErrorCode.MISC_ERROR
    assert raised.value.message == "Unable to dump mempool to disk"


def test_importmempool_refuses_in_core_s_order(
    funded: tuple[Node, Tx],  # noqa: F811
) -> None:
    """The types, then the block download, then each option in turn.

    A null option is its default, an unknown one is ignored, and a file
    that cannot be loaded is the last refusal.
    """
    node, _ = funded
    missing = str(node.data_dir / "missing.dat")
    assert refused(node, []).message == HELP_TEXT["importmempool"]
    wrong = refused(node, [1, 5])
    assert wrong.code == RPCErrorCode.TYPE_ERROR
    assert wrong.message == (
        "Wrong type passed:\n{\n"
        '    "Position 1 (filepath)": "JSON value of type number is not of '
        'expected type string",\n'
        '    "Position 2 (options)": "JSON value of type number is not of '
        'expected type object"\n}'
    )
    assert "type null" in refused(node, [None, {}]).message
    node.is_initial_block_download = True
    in_ibd = refused(node, [missing, {"use_current_time": 1}])
    assert in_ibd.code == RPCErrorCode.CLIENT_IN_INITIAL_DOWNLOAD
    assert in_ibd.message == (
        "Can only import the mempool after the block download and sync is done."
    )
    node.is_initial_block_download = False
    options = {"use_current_time": 1, "apply_unbroadcast_set": "x"}
    first = refused(node, [missing, options])
    assert first.code == RPCErrorCode.TYPE_ERROR
    assert first.message == "JSON value of type number is not of expected type bool"
    options = {"use_current_time": None, "apply_fee_delta_priority": [], "foo": 1}
    assert refused(node, [missing, options]).message == (
        "JSON value of type array is not of expected type bool"
    )
    options = {"use_current_time": None, "apply_unbroadcast_set": "x"}
    assert "type string" in refused(node, [missing, options]).message
    unloadable = refused(node, [missing, {"use_current_time": None, "foo": 1}])
    assert unloadable.code == RPCErrorCode.MISC_ERROR
    assert unloadable.message == (
        "Unable to import mempool file, see debug.log for details."
    )


@pytest.fixture
def synced(funded: tuple[Node, Tx]) -> tuple[Node, Tx]:  # noqa: F811
    """Give `funded`'s node out of initial block download."""
    funded[0].is_initial_block_download = False
    return funded


def a_file(path: Path, fan: Tx) -> None:
    """Write a file of `fan` an hour old, deltas and an unbroadcast mark."""
    entries = [(fan, int(time.time()) - 3600, 500)]
    unheld = secrets.token_bytes(32)
    path.write_bytes(serialize_mempool(entries, {unheld: 9}, [fan.id], _KEY))


def test_importmempool_by_default_takes_the_transactions_alone(
    synced: tuple[Node, Tx],
) -> None:
    """The current time, no delta, no unbroadcast mark: Core's defaults."""
    node, fan = synced
    path = node.data_dir / "other.dat"
    a_file(path, fan)
    before = int(time.time())
    assert finish(import_mempool(node, _CONN, [str(path)])) == {}
    mempool = node.mempool
    assert mempool.entry_times[fan.hash] >= before
    assert mempool.deltas == {}
    assert mempool.unbroadcast == set()


def test_importmempool_takes_what_its_options_name(
    synced: tuple[Node, Tx],
) -> None:
    """The file's time, its deltas and its unbroadcast set."""
    node, fan = synced
    path = node.data_dir / "other.dat"
    a_file(path, fan)
    options = {
        "use_current_time": False,
        "apply_fee_delta_priority": True,
        "apply_unbroadcast_set": True,
    }
    assert finish(import_mempool(node, _CONN, [str(path), options])) == {}
    mempool = node.mempool
    assert mempool.entry_times[fan.hash] < time.time() - 3500
    assert mempool.delta(fan.id) == 500
    assert len(mempool.deltas) == 2
    assert mempool.unbroadcast == {fan.id}


def test_importmempool_replaces_what_a_file_s_transaction_pays_for(
    synced: tuple[Node, Tx],
) -> None:
    """Core's `LoadMempool` accepts each one as `AcceptToMemoryPool` does."""
    node, fan = synced
    tip = len(node.chainstate.block_index.active_chain) - 1
    held = spend(fan, 0, 1_000)
    node.mempool.add_tx(fan, 4_000, height=tip)
    node.mempool.add_tx(held, 1_000, height=tip)
    rival = spend(fan, 0, 50_000)
    path = node.data_dir / "other.dat"
    entries = [(rival, int(time.time()), 0)]
    path.write_bytes(serialize_mempool(entries, {}, [], _KEY))
    assert finish(import_mempool(node, _CONN, [str(path)])) == {}
    assert node.mempool.contains_tx(rival)
    assert not node.mempool.contains_tx(held)


def test_importmempool_ends_refused_where_the_node_stops(
    synced: tuple[Node, Tx],
) -> None:
    """The step after `terminate_flag` is set adds nothing more."""
    node, fan = synced
    path = node.data_dir / "other.dat"
    a_file(path, fan)
    job = import_mempool(node, _CONN, [str(path)])
    node.terminate_flag.set()
    with pytest.raises(RpcError) as raised:
        finish(job)
    assert raised.value.message == (
        "Unable to import mempool file, see debug.log for details."
    )
    assert node.mempool.size == 0


_NOT_LOADED = "Unable to import mempool file, see debug.log for details."


def test_importmempool_refuses_a_path_that_is_no_file_as_core_does(
    synced: tuple[Node, Tx],
) -> None:
    """A NUL and a directory are the refusal of a file that is not there."""
    node, _ = synced
    for path in ("\x00", str(node.data_dir)):
        refused_as = refused(node, [path])
        assert refused_as.code == RPCErrorCode.MISC_ERROR
        assert refused_as.message == _NOT_LOADED


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes")
@pytest.mark.parametrize("writer", [False, True])
def test_importmempool_refuses_a_pipe_before_reading_it(
    synced: tuple[Node, Tx], *, writer: bool
) -> None:
    """With a writer or none, and nothing of a valid file in it is added."""
    node, fan = synced
    pipe = node.data_dir / "pipe.dat"
    os.mkfifo(pipe)
    held = os.open(pipe, os.O_RDWR | os.O_NONBLOCK)
    try:
        if writer:
            a_file(node.data_dir / "other.dat", fan)
            os.write(held, (node.data_dir / "other.dat").read_bytes())
        else:
            os.close(held)
            held = -1
        assert refused(node, [str(pipe)]).message == _NOT_LOADED
    finally:
        if held >= 0:
            os.close(held)
    assert node.mempool.size == 0


def test_importmempool_refuses_a_file_the_system_stops_reading(
    synced: tuple[Node, Tx], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that fails after the open is the refusal of an unreadable file."""

    class Failing(BytesIO):
        @override
        def read(self, size: int | None = -1, /) -> bytes:
            raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(mempool_persist, "_open", lambda _: Failing())
    assert refused(synced[0], ["any"]).message == _NOT_LOADED


def test_a_stream_gives_back_only_what_it_has_just_read() -> None:
    """`Tx.parse` steps back over a marker; nothing else is a seek here."""
    stream = mempool_persist._Stream(BytesIO(b"abcdef"))
    assert stream.read(3) == b"abc"
    stream.seek(-2, SEEK_CUR)
    assert stream.read(4) == b"bcde"
    for step in ((1, SEEK_CUR), (0, SEEK_CUR), (-5, SEEK_CUR), (0, SEEK_SET)):
        with pytest.raises(ValueError, match="step back"):
            stream.seek(*step)


@pytest.mark.skipif(not Path("/dev/zero").exists(), reason="no /dev/zero")
def test_importmempool_refuses_an_endless_device_before_reading_it(
    synced: tuple[Node, Tx],
) -> None:
    """An endless device is refused before any read, with Core's answer."""
    assert refused(synced[0], ["/dev/zero"]).code == RPCErrorCode.MISC_ERROR


def test_a_file_is_deobfuscated_across_the_pieces_it_is_read_in(
    synced: tuple[Node, Tx], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pieces of three bytes cut the key, the fields and the marker."""
    node, fan = synced
    path = node.data_dir / "other.dat"
    a_file(path, fan)
    monkeypatch.setattr(mempool_persist, "_CHUNK", 3)
    assert finish(import_mempool(node, _CONN, [str(path)])) == {}
    assert node.mempool.get_tx(fan.id) is not None
    read = mempool_persist.read_mempool_file(path.read_bytes())
    assert [tx.id for tx, _, _ in read.entries] == [fan.id]
