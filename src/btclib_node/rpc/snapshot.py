# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`dumptxoutset`: write the UTXO set to a snapshot file.

Core's `dumptxoutset` is in `src/rpc/blockchain.cpp`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag, and the file is
`btclib_node.snapshot`'s. The handler has `rpc.callbacks`' signature and
runs on `Node`'s thread (`ARCHITECTURE.md`): a generator that
`rpc.main` resumes on each pass of `Node`'s loop, which writes
`_STEP` coins between two yields, so the loop serves other requests
meanwhile.

A `rollback` dump leaves the chain and the network as they are. Core's
`CreateRolledBackUTXOSnapshot` copies the UTXO set to a temporary
database and disconnects the blocks above the target there. This node
copies only what those blocks changed: their undo data, applied tip
first to a temporary store, which the write merges into the stored
coins. Copying every coin would be a pass over the whole set in Python,
and the file comes out the same. The store is a RocksDB beside the
output file, named `temp_utxo_<height>` as Core names its database, or
a dict where `in_memory` is true.

The store is built without yielding, so a deep rollback holds the loop,
where Core does it on an HTTP worker thread. Pruning runs on the same
loop, so it cannot remove the undo data meanwhile; Core holds a prune
lock for that.
"""

from __future__ import annotations

import hashlib
import heapq
import os
from functools import partial
from itertools import groupby
from operator import itemgetter
from typing import TYPE_CHECKING, Any, cast

from bitcoin_core_rpc import RPCErrorCode
from btclib.coinstats import tx_out_ser
from btclib.exceptions import BTClibValueError
from btclib.hashes import sha256

from btclib_node.block_db import Coin
from btclib_node.chainstate.utxo_index import UtxoIndex
from btclib_node.db import KeyValueStore
from btclib_node.rpc.errors import (
    RpcError,
    is_hex,
    json_type_name,
    type_errors,
)
from btclib_node.rpc.help import HELP_TEXT
from btclib_node.snapshot import (
    serialize_metadata,
    serialize_tx_coins,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator
    from pathlib import Path

    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

__all__ = ["dump_tx_out_set"]

# Coins written, and blocks counted, between two yields and two looks at
# `terminate_flag`. Core looks every 5000 coins.
_STEP = 1024

# `CChainParams::GetAvailableSnapshotHeights`, the heights of
# `m_assumeutxo_data` (`src/kernel/chainparams.cpp`,
# at bitcoin/bitcoin@f0de87f040, the v32.1 tag), keyed by this tree's
# chain names
_SNAPSHOT_HEIGHTS = {
    "mainnet": (840_000, 880_000, 910_000, 935_000, 965_000),
    "testnet": (2_500_000, 4_840_000, 5_125_000),
    "testnet4": (90_000, 120_000, 150_000),
    "signet": (160_000, 290_000, 320_000),
    "regtest": (110, 200, 299),
}

_INT32 = range(-(2**31), 2**31)


def _target(node: Node, snapshot_type: str, options: dict[str, Any]) -> int | None:
    """Return the height of the block the snapshot is of.

    None is the tip, which is read when the dump starts to write: a block
    may arrive while the transactions are counted.
    """
    if "rollback" in options:
        if snapshot_type not in ("", "rollback"):
            raise RpcError(
                RPCErrorCode.INVALID_PARAMETER,
                f'Invalid snapshot type "{snapshot_type}" '
                "specified with rollback option",
            )
        return _hash_or_height(node, options["rollback"])
    if snapshot_type == "rollback":
        return _hash_or_height(node, max(_SNAPSHOT_HEIGHTS[node.chain.name]))
    if snapshot_type == "latest":
        return None
    raise RpcError(
        RPCErrorCode.INVALID_PARAMETER,
        f'Invalid snapshot type "{snapshot_type}" specified. '
        'Please specify "rollback" or "latest"',
    )


def _hash_or_height(node: Node, value: object) -> int:
    """Return the height of the active-chain block `value` names.

    Core's `ParseHashOrHeight`: a number is a height, and a string is
    the hash of a block this node knows. Core dereferences null for a block
    off the active chain; this refuses.
    """
    block_index = node.chainstate.block_index
    active_chain = block_index.active_chain
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not isinstance(value, int) or value not in _INT32:
            raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
        if value < 0:
            raise RpcError(
                RPCErrorCode.INVALID_PARAMETER,
                f"Target block height {value} is negative",
            )
        if value >= len(active_chain):
            raise RpcError(
                RPCErrorCode.INVALID_PARAMETER,
                f"Target block height {value} after current tip "
                f"{len(active_chain) - 1}",
            )
        return value
    if not isinstance(value, str):
        raise RpcError(
            RPCErrorCode.TYPE_ERROR,
            f"JSON value of type {json_type_name(value)} "
            "is not of expected type string",
        )
    if len(value) != 64:  # noqa: PLR2004
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"hash_or_height must be of length 64 (not {len(value)}, for '{value}')",
        )
    if not is_hex(value):
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"hash_or_height must be hexadecimal string (not '{value}')",
        )
    info = block_index.header_dict.get(bytes.fromhex(value))
    if info is None:
        raise RpcError(RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Block not found")
    if active_chain[info.index : info.index + 1] != [bytes.fromhex(value)]:
        raise RpcError(RPCErrorCode.MISC_ERROR, "Block is not in the active chain")
    return info.index


def _chain_tx_count(node: Node, first: int, last: int) -> Generator[bool, None, int]:
    """Return the transactions in the active-chain blocks `first` to `last`.

    Core's `m_chain_tx_count`, summed. This node keeps no count per block,
    so each block's own is read, and a block that has been pruned cannot be.
    """
    chain = node.chainstate.block_index.active_chain
    total = 0
    for number, block_hash in enumerate(chain[first : last + 1]):
        count = node.block_db.tx_count(block_hash)
        if count is None:
            raise RpcError(
                RPCErrorCode.MISC_ERROR,
                "Could not count the transactions of the chain: "
                "block data is already pruned.",
            )
        total += count
        if number % _STEP == _STEP - 1:
            _interruption_point(node)
            yield True
    return total


def _counted(node: Node, target: int | None) -> Generator[bool, None, tuple[int, int]]:
    """Return the transactions up to the base block, and its height.

    The base is `target`, or for `latest` the tip once it holds still.
    The count yields, so the chain may change meanwhile.

    A height is pinned by its block. Blocks added on top do not matter,
    and a reorg that removes the block refuses the dump.

    For `latest` the tip is pinned, then blocks above the counted ones are
    counted and the tip pinned again, until it has not moved. A reorg that
    removes the pinned block makes the count start again from genesis.
    The dump is then of the tip, as Core's is, and is never rolled back.
    """
    chain = node.chainstate.block_index.active_chain
    if target is not None:
        pinned = chain[target]
        count = yield from _chain_tx_count(node, 0, target)
        chain = node.chainstate.block_index.active_chain
        if chain[target : target + 1] != [pinned]:
            raise RpcError(RPCErrorCode.MISC_ERROR, "Block is not in the active chain")
        return count, target
    counted = 0
    total = 0
    while True:
        tip = len(chain) - 1
        pinned = chain[tip]
        total += yield from _chain_tx_count(node, counted, tip)
        counted = tip + 1
        chain = node.chainstate.block_index.active_chain
        if chain[tip : tip + 1] != [pinned]:
            counted = total = 0
        elif len(chain) - 1 == tip:
            return total, tip


def _interruption_point(node: Node) -> None:
    """Raise where the node is stopping, as Core's interruption point does."""
    if node.terminate_flag.is_set():
        raise RpcError(RPCErrorCode.CLIENT_NOT_CONNECTED, "Shutting down")


def dump_tx_out_set(
    node: Node, conn: RpcConnection, params: list[Any]
) -> Generator[bool, None, dict[str, Any]]:
    """Answer `dumptxoutset`: write the UTXO set of a block to `path`.

    `latest` is the active tip's, and a `rollback` option or type is an
    earlier block's. `path` is relative to the data directory. The file
    is written to `path` and `.incomplete` and renamed once whole, and a
    path that exists is refused.

    `nchaintx` is a sum of every block's transaction count up to the base
    block, read from the stored blocks. A node whose blocks are pruned
    has not those, and refuses with `RPC_MISC_ERROR` where Core, which
    keeps the count with each header, answers.

    `in_memory` is read only for a rollback, as Core reads it.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["dumptxoutset"])
    path_arg = params[0]
    type_arg = params[1] if len(params) > 1 and params[1] is not None else ""
    options = params[2] if len(params) > 2 and params[2] is not None else {}  # noqa: PLR2004
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(path_arg, str):
        mismatches.append((1, "path", path_arg, "string"))
    if not isinstance(type_arg, str):
        mismatches.append((2, "type", type_arg, "string"))
    if not isinstance(options, dict):
        mismatches.append((3, "options", options, "object"))
    if mismatches:
        raise type_errors(*mismatches)

    target = _target(node, type_arg, options)
    path = node.data_dir / path_arg
    temp_path = path.with_name(path.name + ".incomplete")
    if path.exists():
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"{path} already exists. If you are sure this is what you want, "
            "move it out of the way first",
        )
    chain_tx_count, height = yield from _counted(node, target)
    try:
        file = temp_path.open("wb")
    except OSError as err:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"Couldn't open file {temp_path} for writing.",
        ) from err
    with file:
        result = yield from _rolled_back_write(node, height, options, file, temp_path)
    temp_path.replace(path)
    return {
        "coins_written": result["coins_written"],
        "base_hash": result["base_hash"],
        "base_height": result["base_height"],
        "path": str(path),
        "txoutset_hash": result["txoutset_hash"],
        "nchaintx": chain_tx_count,
    }


def _rolled_back_write(
    node: Node,
    height: int,
    options: dict[str, Any],
    file: Any,  # noqa: ANN401
    temp_path: Path,
) -> Generator[bool, None, dict[str, Any]]:
    """Write the coins at `height`, rolling them back to it if need be.

    The chainstate is flushed and a cursor opened, whose view is fixed
    there, as Core's `PrepareUTXOSnapshot` does. Nothing yields between
    the flush, the cursor and the read of the chain, so the cursor holds
    the coins of that chain's tip.
    """
    node.chainstate.flush()
    chain = node.chainstate.block_index.active_chain
    utxo_index = node.chainstate.utxo_index
    if height == len(chain) - 1:
        return (
            yield from _write(
                node,
                file,
                (chain[height], height),
                utxo_index.cursor(),
                utxo_index.coin_stats.transaction_output_count,
            )
        )
    if node.block_db.pruned_up_to >= height:
        raise RpcError(
            RPCErrorCode.MISC_ERROR,
            "Could not roll back to requested height since "
            "necessary block data is already pruned.",
        )
    in_memory = options.get("in_memory", False)
    if not isinstance(in_memory, bool):
        raise RpcError(
            RPCErrorCode.TYPE_ERROR,
            f"JSON value of type {json_type_name(in_memory)} "
            "is not of expected type bool",
        )
    cursor = utxo_index.cursor()
    db_path = temp_path.parent / f"temp_utxo_{height}"
    if in_memory:
        node.logger.info(
            "Using in-memory database for UTXO-set rollback "
            "(this may require significant RAM)."
        )
        changes: dict[bytes, bytes] = {}
        _disconnect(node, chain, height, changes.update)
        return (
            yield from _write(
                node,
                file,
                (chain[height], height),
                _merged(cursor, iter(sorted(changes.items()))),
            )
        )
    KeyValueStore.destroy(db_path)
    store = KeyValueStore(db_path)
    scan = None
    try:
        _disconnect(node, chain, height, partial(_put_all, store))
        # closed below, so that the store's directory `LOCK` is let go
        scan = cast("Generator[tuple[bytes, bytes]]", store.scan_prefix(b"utxo-"))
        return (
            yield from _write(
                node, file, (chain[height], height), _merged(cursor, scan)
            )
        )
    finally:
        if scan is not None:
            scan.close()
        store.close()
        try:
            KeyValueStore.destroy(db_path)
        except Exception:  # noqa: BLE001
            node.logger.info(
                "Failed to clean up temporary UTXO database at %s, "
                "please remove it manually.",
                db_path,
            )


def _put_all(store: KeyValueStore, changes: dict[bytes, bytes]) -> None:
    """Write `changes` into `store`, in one batch."""
    with store.write_batch() as batch:
        for key, value in changes.items():
            batch.put(key, value)


def _disconnect(
    node: Node,
    chain: list[bytes],
    height: int,
    put: Callable[[dict[bytes, bytes]], None],
) -> None:
    """Pass `put` what each block above `height` changed, tip first.

    A change maps a `utxo-` key to its coin at `height`, or to `b""`
    where it has none. Each block's undo data restores the coins it
    spent, then drops the coins it created, so an output created and
    spent within the block ends dropped. A later change replaces an
    earlier one, so the block lower in the chain decides, as Core's
    `DisconnectBlock` does walking down from the tip.
    """
    for block_height in range(len(chain) - 1, height, -1):
        _interruption_point(node)
        rev_block = node.block_db.get_rev_block(chain[block_height])
        if rev_block is None or rev_block.hash != chain[block_height]:
            raise RpcError(
                RPCErrorCode.INTERNAL_ERROR,
                f"Failed to roll back block at height {block_height}",
            )
        changes = {
            b"utxo-" + out_point.serialize(check_validity=False): coin.serialize()
            for out_point, coin in rev_block.to_add
        }
        for out_point in rev_block.to_remove:
            changes[b"utxo-" + out_point.serialize(check_validity=False)] = b""
        put(changes)


def _merged(
    stored: Iterator[tuple[bytes, bytes]], changes: Iterator[tuple[bytes, bytes]]
) -> Iterator[tuple[bytes, bytes]]:
    """Yield the coins of `stored` as `changes` leave them, in key order.

    Both are in key order. A change replaces the stored coin of its key,
    or drops it where its value is `b""`.
    """
    merged = heapq.merge(
        ((key, 0, value) for key, value in changes),
        ((key, 1, value) for key, value in stored),
    )
    for key, group in groupby(merged, key=itemgetter(0)):
        _, _, value = next(group)
        if value:
            yield key, value


def _write(
    node: Node,
    file: Any,  # noqa: ANN401
    base: tuple[bytes, int],
    cursor: Iterator[tuple[bytes, bytes]],
    coins_count: int | None = None,
) -> Generator[bool, None, dict[str, Any]]:
    """Write the metadata and every coin of `cursor` to `file`.

    Core's `WriteUTXOSnapshot`: the same pass hashes the coins as
    `hash_serialized_3` does and writes them. `base` is the hash and the
    height of the block whose coins `cursor` walks, and `coins_count`
    is how many it holds, as the chainstate counts them. Where it is
    `None` the coins are counted as they are written, and the count in
    the metadata is written once they are all in, where Core counts them
    in a first pass over the temporary database.
    """
    base_hash, base_height = base
    file.write(serialize_metadata(node.chain.magic, base_hash, coins_count or 0))
    hasher = hashlib.sha256()
    written = 0
    for rows in UtxoIndex.by_txid(cursor):
        coins = []
        for n, out_point_bytes, value in rows:
            try:
                coin = Coin.parse(value, check_validity=False)
            except BTClibValueError as err:
                raise RpcError(
                    RPCErrorCode.INTERNAL_ERROR, "Unable to read UTXO set"
                ) from err
            hasher.update(tx_out_ser(out_point_bytes, coin))
            coins.append((n, coin))
        file.write(serialize_tx_coins(rows[0][1][:32], coins))
        written += len(coins)
        if written // _STEP != (written - len(coins)) // _STEP:
            _interruption_point(node)
            yield True
    if coins_count is None:
        file.seek(0)
        file.write(serialize_metadata(node.chain.magic, base_hash, written))
        file.seek(0, os.SEEK_END)
    elif written != coins_count:
        raise RpcError(RPCErrorCode.INTERNAL_ERROR, "Unable to read UTXO set")
    file.flush()
    os.fsync(file.fileno())
    return {
        "coins_written": written,
        "base_hash": base_hash,
        "base_height": base_height,
        "txoutset_hash": sha256(hasher.digest())[::-1],
    }
