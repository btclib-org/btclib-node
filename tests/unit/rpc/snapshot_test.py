# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What `dumptxoutset` answers, called as `rpc.main` calls it.

On a regtest node built and driven in this thread. The refusals, and the
shape of the answer, are what `bitcoind` v32.1.0 says for the same call
(`tests/integration/dumptxoutset_test.py` asks it, and compares the files).
"""

from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

import btclib_node.rpc.snapshot as rpc_snapshot
from btclib_node.db import KeyValueStore
from btclib_node.main import invalidate_chain
from btclib_node.rpc.callbacks import arg_names, callbacks, named_only
from btclib_node.rpc.errors import RpcError
from btclib_node.rpc.help import CATEGORY, HELP_TEXT
from btclib_node.rpc.mining import generate_block
from btclib_node.rpc.snapshot import dump_tx_out_set
from btclib_node.stored_block import read_held_block
from tests import finish

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

CONN = cast("RpcConnection", None)

KEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
OTHER_KEY = "02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
MAGIC = "fabfb5da"


@pytest.fixture
def node(regtest_node: Callable[[], Node]) -> Node:
    """Give one regtest node, built fresh for the test."""
    return regtest_node()


def mine(node: Node, blocks: int = 1, key: str = KEY, desc: str = "") -> None:
    """Mine `blocks` blocks, each paying `key`, or `desc` where given."""
    for _ in range(blocks):
        finish(generate_block(node, CONN, [desc or f"pkh({key})", []]))


def dump(node: Node, *params: object) -> dict[str, Any]:
    """Run `dumptxoutset` to its answer."""
    return finish(dump_tx_out_set(node, CONN, list(params)))


def refusal(node: Node, *params: object) -> tuple[int, str]:
    """Return the code and message `dumptxoutset` is refused with."""
    with pytest.raises(RpcError) as caught:
        dump(node, *params)
    return int(caught.value.code), caught.value.message


def height(node: Node) -> int:
    """Return the height of the active tip."""
    return len(node.chainstate.block_index.active_chain) - 1


def test_it_is_served_as_core_names_it() -> None:
    """The method is dispatched, named, helped and listed under Blockchain."""
    assert callbacks["dumptxoutset"] is dump_tx_out_set
    assert arg_names["dumptxoutset"] == (
        "path",
        "type",
        "options|rollback|in_memory",
    )
    assert named_only["dumptxoutset"] == ("rollback", "in_memory")
    assert HELP_TEXT["dumptxoutset"].startswith('dumptxoutset "path" (')
    assert CATEGORY["dumptxoutset"] == "Blockchain"


def test_the_tip_is_written_to_a_file_under_the_data_directory(node: Node) -> None:
    """Core's answer, in Core's order, and a file the answer describes."""
    mine(node, 3)
    result = dump(node, "utxo.dat", "latest")
    path = node.data_dir / "utxo.dat"
    chain = node.chainstate.block_index.active_chain
    assert list(result) == [
        "coins_written",
        "base_hash",
        "base_height",
        "path",
        "txoutset_hash",
        "nchaintx",
    ]
    assert result["coins_written"] == 3
    assert result["base_hash"] == chain[3]
    assert result["base_height"] == 3
    assert result["path"] == str(path)
    assert result["nchaintx"] == 4
    utxo_index = node.chainstate.utxo_index
    expected = utxo_index.serialized_hash(utxo_index.cursor())
    assert expected is not None
    assert result["txoutset_hash"] == expected[::-1]
    data = path.read_bytes()
    assert data[:5] == b"utxo\xff"
    assert data[5:7] == (2).to_bytes(2, "little")
    assert data[7:11].hex() == MAGIC
    assert data[11:43] == chain[3][::-1]
    assert int.from_bytes(data[43:51], "little") == 3
    assert not (node.data_dir / "utxo.dat.incomplete").exists()


def test_an_absolute_path_is_used_as_it_is(node: Node, tmp_path: Path) -> None:
    """Core joins the path to the data directory, and an absolute one wins."""
    mine(node)
    target = tmp_path / "elsewhere.dat"
    assert dump(node, str(target), "latest")["path"] == str(target)
    assert target.exists()


def _spend_and_chain(node: Node, height: int) -> None:
    """Mine a block spending the coinbase at `height`, and spending that again.

    The first transaction pays `OP_TRUE` twice, and the second spends its
    first output, so the block creates an output it also spends.
    """
    chain = node.chainstate.block_index.active_chain
    block = read_held_block(node.block_db, node.chain, chain[height])
    assert block is not None
    coinbase = block.transactions[0]
    first = Tx(
        2,
        0,
        [TxIn(OutPoint(coinbase.id, 0), b"", 0xFFFFFFFF)],
        [TxOut(coinbase.vout[0].value - 10**8, b"\x51"), TxOut(10**7, b"\x51")],
        check_validity=False,
    )
    second = Tx(
        2,
        0,
        [TxIn(OutPoint(first.id, 0), b"", 0xFFFFFFFF)],
        [TxOut(10**8, b"\x51")],
        check_validity=False,
    )
    finish(
        generate_block(
            node,
            CONN,
            [
                f"pkh({KEY})",
                [
                    first.serialize(include_witness=True, check_validity=False).hex(),
                    second.serialize(include_witness=True, check_validity=False).hex(),
                ],
            ],
        )
    )


@pytest.mark.parametrize("in_memory", [False, True])
def test_a_rollback_is_the_dump_its_target_had_and_leaves_the_chain(
    node: Node, monkeypatch: pytest.MonkeyPatch, *, in_memory: bool
) -> None:
    """The file is the one written at the target, and the tip never moves.

    The blocks above the target spend a coin the target holds and create
    coins, one of them spent in its own block.
    """
    mine(node, 101, desc="raw(51)")
    before = dump(node, "before.dat", "latest")
    _spend_and_chain(node, 1)
    mine(node, 2)
    tip = node.chainstate.block_index.active_chain[-1]
    seen: list[tuple[bytes, bool, bool]] = []
    write = rpc_snapshot._write

    def spy(*args: Any) -> Generator[bool, None, dict[str, Any]]:
        seen.append(
            (
                node.chainstate.block_index.active_chain[-1],
                node.p2p_manager.get_network_active(),
                (node.data_dir / "temp_utxo_101").exists(),
            )
        )
        return write(*args)

    monkeypatch.setattr(rpc_snapshot, "_write", spy)
    result = dump(node, "after.dat", "", {"rollback": 101, "in_memory": in_memory})
    assert seen == [(tip, True, not in_memory)]
    assert {k: v for k, v in result.items() if k != "path"} == {
        k: v for k, v in before.items() if k != "path"
    }
    assert (node.data_dir / "after.dat").read_bytes() == (
        node.data_dir / "before.dat"
    ).read_bytes()
    assert not (node.data_dir / "temp_utxo_101").exists()
    assert height(node) == 104
    assert (
        dump(node, "old2.dat", "rollback", {"rollback": result["base_hash"].hex()})[
            "base_height"
        ]
        == 101
    )


def test_in_memory_is_read_for_a_rollback_alone(node: Node) -> None:
    """Core's `get_bool`, after the target and the pruned blocks are checked."""
    mine(node, 2)
    for value, name in (("x", "string"), (None, "null"), (1, "number")):
        assert refusal(node, "x.dat", "", {"rollback": 1, "in_memory": value}) == (
            -3,
            f"JSON value of type {name} is not of expected type bool",
        )
    assert (node.data_dir / "x.dat.incomplete").exists()
    assert dump(node, "t.dat", "latest", {"in_memory": "x"})["base_height"] == 2
    assert (
        dump(node, "u.dat", "", {"rollback": 2, "in_memory": "x"})["base_height"] == 2
    )


def test_a_temporary_store_left_by_an_unclean_stop_is_wiped(node: Node) -> None:
    """Core opens the temporary database with `wipe_data`."""
    mine(node, 3)
    expected = dump(node, "a.dat", "", {"rollback": 1, "in_memory": True})
    stale = KeyValueStore(node.data_dir / "temp_utxo_1")
    stale.put(b"utxo-" + bytes(36), b"\x00")
    stale.close()
    result = dump(node, "b.dat", "", {"rollback": 1})
    assert result["txoutset_hash"] == expected["txoutset_hash"]
    assert not (node.data_dir / "temp_utxo_1").exists()


def test_a_temporary_store_that_cannot_be_removed_is_logged(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `TemporaryUTXODatabase` says to remove it by hand."""
    mine(node, 2)
    destroy = KeyValueStore.destroy
    calls: list[object] = []

    def fail_second(path: Any) -> None:
        calls.append(path)
        if len(calls) > 1:
            raise OSError(path)
        destroy(path)

    monkeypatch.setattr(KeyValueStore, "destroy", fail_second)
    logged: list[str] = []
    monkeypatch.setattr(
        node.logger, "info", lambda msg, *args: logged.append(msg % args)
    )
    assert dump(node, "a.dat", "", {"rollback": 1})["base_height"] == 1
    db_path = node.data_dir / "temp_utxo_1"
    assert logged == [
        (
            f"Failed to clean up temporary UTXO database at {db_path}, "
            "please remove it manually."
        )
    ]
    destroy(db_path)


def test_a_block_without_undo_data_is_refused(node: Node) -> None:
    """Core's `DisconnectBlock` failure, and the temporary store goes."""
    mine(node, 3)
    chain = node.chainstate.block_index.active_chain
    node.block_db.rev_patches.pop(chain[2])
    assert refusal(node, "x.dat", "", {"rollback": 1}) == (
        -32603,
        "Failed to roll back block at height 2",
    )
    assert not (node.data_dir / "temp_utxo_1").exists()
    assert height(node) == 3


def test_undo_data_filed_under_another_block_is_refused(node: Node) -> None:
    """Core's `ReadBlockUndo` checksums the undo data with the parent's hash."""
    mine(node, 3)
    chain = node.chainstate.block_index.active_chain
    node.block_db.rev_patches[chain[3]] = node.block_db.rev_patches[chain[2]]
    assert refusal(node, "x.dat", "", {"rollback": 1}) == (
        -32603,
        "Failed to roll back block at height 3",
    )
    assert not (node.data_dir / "x.dat").exists()
    assert not (node.data_dir / "temp_utxo_1").exists()
    assert height(node) == 3


def test_a_stop_during_the_rollback_ends_it_and_removes_the_store(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback looks at `terminate_flag` at each block."""
    mine(node, 3)
    get_rev_block = node.block_db.get_rev_block

    def stop_after(block_hash: bytes) -> Any:
        node.terminate_flag.set()
        return get_rev_block(block_hash)

    monkeypatch.setattr(node.block_db, "get_rev_block", stop_after)
    assert refusal(node, "x.dat", "", {"rollback": 1}) == (-9, "Shutting down")
    assert not (node.data_dir / "temp_utxo_1").exists()


def test_a_stop_during_a_rollback_write_removes_the_store(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan of the store is closed first, so that it can be removed."""
    monkeypatch.setattr(rpc_snapshot, "_STEP", 1)
    mine(node, 3)
    job = dump_tx_out_set(node, CONN, ["x.dat", "", {"rollback": 2}])
    while not (node.data_dir / "temp_utxo_2").exists():
        next(job)
    node.terminate_flag.set()
    with pytest.raises(RpcError) as caught:
        finish(job)
    assert caught.value.message == "Shutting down"
    assert not (node.data_dir / "temp_utxo_2").exists()


def test_a_block_arriving_during_a_rollback_write_is_not_in_it(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coins are those of the tip the cursor was opened at."""
    monkeypatch.setattr(rpc_snapshot, "_STEP", 1)
    mine(node, 3)
    expected = dump(node, "a.dat", "", {"rollback": 1, "in_memory": True})
    job = dump_tx_out_set(node, CONN, ["b.dat", "", {"rollback": 1}])
    while not (node.data_dir / "temp_utxo_1").exists():
        next(job)
    mine(node)
    result = finish(job)
    assert result["txoutset_hash"] == expected["txoutset_hash"]
    assert (node.data_dir / "b.dat").read_bytes() == (
        node.data_dir / "a.dat"
    ).read_bytes()


def test_the_rollback_type_goes_to_the_last_snapshot_height(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a `rollback` option, the highest height Core could load."""
    mine(node, 4)
    monkeypatch.setitem(rpc_snapshot._SNAPSHOT_HEIGHTS, "regtest", (1, 2))
    assert dump(node, "r.dat", "rollback")["base_height"] == 2


def test_a_rollback_to_the_tip_is_a_latest_dump(node: Node) -> None:
    """Nothing is rolled back for the tip itself."""
    mine(node, 2)
    assert dump(node, "t.dat", "", {"rollback": 2})["base_height"] == 2


def test_a_rollback_past_pruned_blocks_is_refused(node: Node) -> None:
    """Core's pruned-mode check, on the first block still held."""
    mine(node, 3)
    node.block_db.pruned_up_to = 2
    assert refusal(node, "x.dat", "", {"rollback": 2}) == (
        -1,
        (
            "Could not roll back to requested height since "
            "necessary block data is already pruned."
        ),
    )


def test_a_chain_with_pruned_blocks_cannot_be_counted(node: Node) -> None:
    """The transaction count is read from the blocks, which are gone."""
    mine(node, 2)
    node.block_db.blocks.pop(node.chainstate.block_index.active_chain[1])
    code, message = refusal(node, "x.dat", "latest")
    assert code == -1
    assert "pruned" in message


@pytest.mark.parametrize(
    ("params", "code", "message"),
    [
        ([], -1, 'dumptxoutset "path" ( "type" {"rollback":n,"in_memory":bool,...} )'),
        ([1], -3, '"Position 1 (path)": "JSON value of type number'),
        ([None], -3, "JSON value of type null is not of expected type string"),
        (
            [1, 2, 3],
            -3,
            (
                '"Position 1 (path)": "JSON value of type number is not of '
                'expected type string",\n    "Position 2 (type)": "JSON value '
                'of type number is not of expected type string",\n    '
                '"Position 3 (options)": "JSON value of type number is not of '
                'expected type object"'
            ),
        ),
        (["a.dat", "latest", "x"], -3, '"Position 3 (options)": "JSON value of type'),
        (
            ["a.dat"],
            -8,
            'Invalid snapshot type "" specified. Please specify "rollback" or "latest"',
        ),
        (["a.dat", None], -8, 'Invalid snapshot type "" specified.'),
        (["a.dat", "bogus"], -8, 'Invalid snapshot type "bogus" specified.'),
        (
            ["a.dat", "latest", {"rollback": 1}],
            -8,
            'Invalid snapshot type "latest" specified with rollback option',
        ),
        (["a.dat", "", {"rollback": 99}], -8, "Target block height 99 after current"),
        (["a.dat", "", {"rollback": -1}], -8, "Target block height -1 is negative"),
        (["a.dat", "rollback"], -8, "Target block height 299 after current tip"),
        (
            ["a.dat", "", {"rollback": "zz"}],
            -8,
            "hash_or_height must be of length 64 (not 2, for 'zz')",
        ),
        (
            ["a.dat", "", {"rollback": "zz" * 32}],
            -8,
            "hash_or_height must be hexadecimal string",
        ),
        (["a.dat", "", {"rollback": "00" * 32}], -5, "Block not found"),
        (
            ["a.dat", "", {"rollback": True}],
            -3,
            "JSON value of type bool is not of expected type string",
        ),
        (
            ["a.dat", "", {"rollback": None}],
            -3,
            "JSON value of type null is not of expected type string",
        ),
        (["a.dat", "", {"rollback": 1.5}], -1, "JSON integer out of range"),
        (["a.dat", "", {"rollback": 2**31}], -1, "JSON integer out of range"),
    ],
)
def test_the_refusals_are_core_s(
    node: Node, params: list[object], code: int, message: str
) -> None:
    """Each has Core's code and message."""
    mine(node, 1)
    got_code, got_message = refusal(node, *params)
    assert got_code == code
    assert message in got_message


def test_a_block_off_the_active_chain_cannot_be_rolled_back_to(node: Node) -> None:
    """Core dereferences a null block here; this refuses."""
    mine(node, 2)
    stale = node.chainstate.block_index.active_chain[2]
    invalidate_chain(node, stale)
    assert refusal(node, "a.dat", "", {"rollback": stale.hex()}) == (
        -1,
        "Block is not in the active chain",
    )


def test_an_existing_path_is_not_overwritten(node: Node) -> None:
    """Core refuses, and says how to go on."""
    mine(node)
    (node.data_dir / "there.dat").write_bytes(b"x")
    code, message = refusal(node, "there.dat", "latest")
    assert code == -8
    assert message == (
        f"{node.data_dir / 'there.dat'} already exists. If you are sure this "
        "is what you want, move it out of the way first"
    )
    assert (node.data_dir / "there.dat").read_bytes() == b"x"


def test_a_path_that_cannot_be_opened_is_refused(node: Node) -> None:
    """Core names the `.incomplete` file."""
    mine(node)
    target = node.data_dir / "no" / "such" / "dir" / "x.dat"
    assert refusal(node, str(target), "latest") == (
        -8,
        f"Couldn't open file {target}.incomplete for writing.",
    )


def test_a_stopping_node_ends_the_write_and_leaves_the_temporary_file(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temporary name is why an interrupted dump is not mistaken for one."""
    monkeypatch.setattr(rpc_snapshot, "_STEP", 1)
    mine(node, 3)
    job = dump_tx_out_set(node, CONN, ["x.dat", "latest"])
    while not (node.data_dir / "x.dat.incomplete").exists():
        next(job)
    node.terminate_flag.set()
    with pytest.raises(RpcError) as caught:
        finish(job)
    assert caught.value.message == "Shutting down"
    assert (node.data_dir / "x.dat.incomplete").exists()
    assert not (node.data_dir / "x.dat").exists()


def test_the_write_yields_between_steps(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coins and blocks are counted a step at a time."""
    monkeypatch.setattr(rpc_snapshot, "_STEP", 2)
    mine(node, 6)
    job = dump_tx_out_set(node, CONN, ["x.dat", "latest"])
    steps = sum(1 for _ in _steps(job))
    assert steps >= 3


def _steps(job: Generator[bool, None, dict[str, Any]]) -> Generator[bool]:
    """Yield each step of `job`."""
    while True:
        try:
            yield next(job)
        except StopIteration:
            return


def test_a_record_that_does_not_parse_is_an_internal_error(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's `Unable to read UTXO set`."""
    mine(node)
    monkeypatch.setattr(
        type(node.chainstate.utxo_index),
        "cursor",
        lambda _: iter([(b"utxo-" + bytes(36), b"")]),
    )
    assert refusal(node, "x.dat", "latest") == (-32603, "Unable to read UTXO set")


def test_a_count_that_disagrees_with_the_coins_is_an_internal_error(
    node: Node,
) -> None:
    """Core's `CHECK_NONFATAL(written_coins_count == coins_count)`."""
    mine(node)
    node.chainstate.utxo_index.coin_stats.transaction_output_count += 1
    assert refusal(node, "x.dat", "latest") == (-32603, "Unable to read UTXO set")


def test_a_block_arriving_during_the_write_is_not_the_base(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base height is the base hash's, read before the first yield."""
    monkeypatch.setattr(rpc_snapshot, "_STEP", 1)
    mine(node, 3)
    base = node.chainstate.block_index.active_chain[-1]
    arrived: list[None] = []
    point = rpc_snapshot._interruption_point

    def mine_once(target: Node) -> None:
        if not arrived and (target.data_dir / "x.dat.incomplete").exists():
            arrived.append(None)
            mine(node)
        point(target)

    monkeypatch.setattr(rpc_snapshot, "_interruption_point", mine_once)
    result = dump(node, "x.dat", "latest")
    assert arrived
    assert height(node) == 4
    assert (result["base_hash"], result["base_height"]) == (base, 3)


def test_a_block_arriving_during_the_count_extends_it_to_the_new_tip(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new blocks are counted, none again, and nothing is rolled back."""
    monkeypatch.setattr(rpc_snapshot, "_STEP", 1)
    mine(node, 3)
    counts: list[tuple[int, int]] = []
    count = rpc_snapshot._chain_tx_count

    def spy(target: Node, first: int, last: int) -> Generator[bool, None, int]:
        counts.append((first, last))
        return count(target, first, last)

    monkeypatch.setattr(rpc_snapshot, "_chain_tx_count", spy)
    rolled_back: list[object] = []
    monkeypatch.setattr(
        rpc_snapshot, "_disconnect", lambda *args: rolled_back.append(args)
    )
    job = dump_tx_out_set(node, CONN, ["x.dat", "latest"])
    assert next(job) is True
    mine(node)
    result = finish(job)
    assert counts == [(0, 3), (4, 4)]
    assert not rolled_back
    assert (result["base_height"], result["nchaintx"]) == (4, 5)
    assert result["base_hash"] == node.chainstate.block_index.active_chain[-1]


def test_a_reorg_to_the_same_height_during_the_count_counts_again(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned tip left the chain: the count is of the new chain."""
    monkeypatch.setattr(rpc_snapshot, "_STEP", 1)
    mine(node, 3)
    counts: list[tuple[int, int]] = []
    count = rpc_snapshot._chain_tx_count

    def spy(target: Node, first: int, last: int) -> Generator[bool, None, int]:
        counts.append((first, last))
        return count(target, first, last)

    monkeypatch.setattr(rpc_snapshot, "_chain_tx_count", spy)
    job = dump_tx_out_set(node, CONN, ["x.dat", "latest"])
    assert next(job) is True
    invalidate_chain(node, node.chainstate.block_index.active_chain[3])
    mine(node, key=OTHER_KEY)
    new_tip = node.chainstate.block_index.active_chain[-1]
    result = finish(job)
    assert counts == [(0, 3), (0, 3)]
    assert (result["base_hash"], result["base_height"]) == (new_tip, 3)
    assert result["nchaintx"] == 4


def test_a_stop_during_the_count_ends_the_call_before_any_rollback(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count looks at `terminate_flag` at each step, as the write does."""
    monkeypatch.setattr(rpc_snapshot, "_STEP", 1)
    mine(node, 3)
    rolled_back: list[object] = []
    monkeypatch.setattr(
        rpc_snapshot, "_disconnect", lambda *args: rolled_back.append(args)
    )
    job = dump_tx_out_set(node, CONN, ["x.dat", "", {"rollback": 1}])
    assert next(job) is True
    node.terminate_flag.set()
    with pytest.raises(RpcError) as caught:
        finish(job)
    assert caught.value.message == "Shutting down"
    assert not rolled_back


def test_a_chain_shortened_during_the_count_refuses_and_keeps_the_network(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The target block left the chain: refused, with nothing switched off."""
    monkeypatch.setattr(rpc_snapshot, "_STEP", 1)
    mine(node, 5)
    job = dump_tx_out_set(node, CONN, ["x.dat", "", {"rollback": 3}])
    assert next(job) is True
    invalidate_chain(node, node.chainstate.block_index.active_chain[2])
    with pytest.raises(RpcError) as caught:
        finish(job)
    assert caught.value.message == "Block is not in the active chain"
    assert node.p2p_manager.get_network_active() is True
    assert not (node.data_dir / "x.dat.incomplete").exists()
