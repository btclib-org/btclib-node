# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The RPCs that read the UTXO set whole: `scantxoutset`.

Core's `scantxoutset` is in `src/rpc/blockchain.cpp`, with the descriptor
arguments in `src/rpc/util.cpp`, both at bitcoin/bitcoin@9be056a8a7, the
v31.1 tag. Its `InferDescriptor` is btclib_wallet's `infer_descriptor`.
Each handler has `rpc.callbacks`' signature and runs on `Node`'s
thread (`ARCHITECTURE.md`).

A scan is a generator that `rpc.main` resumes on each pass of `Node`'s
loop. It walks the stored coins `_STEP` at a time and yields between
steps, so the loop serves other requests meanwhile, `status` and `abort`
among them. Core runs the scan on an HTTP worker and the two others on
workers of their own, which share atomics; here all three run on `Node`'s
thread, and the scan's state needs no lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from bitcoin_core_rpc import RPCErrorCode
from btclib.exceptions import BTClibException, BTClibValueError
from btclib_wallet.descriptors import (
    Descriptor,
    Provider,
    infer_descriptor,
    multipath_descriptors,
    strip_checksum,
)
from btclib_wallet.descriptors import parse as parse_descriptor

from btclib_node.block_db import Coin
from btclib_node.rpc.connection import btc_amount
from btclib_node.rpc.errors import RpcError, json_type_name, type_errors
from btclib_node.rpc.help import HELP_TEXT

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

__all__ = ["scan_tx_out_set"]

# Coins read between two yields, and between two looks at `terminate_flag`
# and the abort flag. Core looks every 8192 coins (`FindScriptPubKey`),
# which is a longer pause than `Node`'s loop should be asked to wait here.
_STEP = 1024

# Keys derived between two yields of the expansion. One index's keys are
# derived in a single step, however many there are.
_EXPAND_STEP = 16

# A merge of fewer providers than this does not yield
_MERGE_STEP = 2**14

# `FindScriptPubKey` updates the progress every 256 coins
_PROGRESS_EVERY = 256

# `RPCArg::Default{1000}`, the range a ranged descriptor is expanded over
_DEFAULT_RANGE_END = 1000

# `ParseDescriptorRange`'s own bound
_MAX_RANGE_SIZE = 1_000_000

_INT64 = range(-(2**63), 2**63)


@dataclass
class _Scan:
    """The scan `Node` runs, as `status` and `abort` see it."""

    progress: int = 0
    abort: bool = False


# `g_scan_in_progress`, `g_scan_progress` and `g_should_abort_scan` in Core,
# which are process globals; one scan at a time per node here
_scans: WeakKeyDictionary[Node, _Scan] = WeakKeyDictionary()


@dataclass(frozen=True)
class _Source:
    """A parsed descriptor, and the private keys it was read with."""

    descriptor: Descriptor
    prv_keys: dict[str, str]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int64(value: object) -> int:
    """`UniValue::getInt<int64_t>()`: an integer number in range."""
    if not isinstance(value, int) or value not in _INT64:
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    return value


def _range(value: object) -> tuple[int, int]:
    """Return a `range` as Core's `ParseDescriptorRange` reads it."""
    if _is_number(value):
        low, high = 0, _int64(value)
    elif (
        isinstance(value, list)
        and len(value) == 2  # noqa: PLR2004
        and all(_is_number(end) for end in value)
    ):
        low, high = _int64(value[0]), _int64(value[1])
        if low > high:
            raise RpcError(
                RPCErrorCode.INVALID_PARAMETER,
                "Range specified as [begin,end] must not have begin after end",
            )
    else:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Range must be specified as end or as [begin,end]",
        )
    if low < 0:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER, "Range should be greater or equal than 0"
        )
    if high >> 31:
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, "End of range is too high")
    if high >= low + _MAX_RANGE_SIZE:
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, "Range is too large")
    return low, high


def _parse(node: Node, text: str) -> list[_Source]:
    """Parse `text`, which is one descriptor or a multipath one's several.

    Core's `Parse`. Its message names what it found wrong and is
    btclib_wallet's here, which words it differently.
    """
    prv_keys: dict[str, str] = {}
    try:
        sources = [
            _Source(parse_descriptor(one, node.chain.name, prv_keys), prv_keys)
            for one in multipath_descriptors(strip_checksum(text))
        ]
    except BTClibException as err:
        raise RpcError(RPCErrorCode.INVALID_ADDRESS_OR_KEY, str(err)) from err
    return sources


def _scan_object(node: Node, scan_object: object) -> tuple[list[_Source], int, int]:
    """Return a scan object's descriptors and expansion indexes.

    Core's `EvalDescriptorStringOrObject`.
    """
    low, high = 0, _DEFAULT_RANGE_END
    if isinstance(scan_object, str):
        text = scan_object
    elif isinstance(scan_object, dict):
        desc = scan_object.get("desc")
        if desc is None:
            raise RpcError(
                RPCErrorCode.INVALID_PARAMETER,
                "Descriptor needs to be provided in scan object",
            )
        if not isinstance(desc, str):
            raise RpcError(
                RPCErrorCode.TYPE_ERROR,
                f"JSON value of type {json_type_name(desc)} "
                "is not of expected type string",
            )
        text = desc
        if scan_object.get("range") is not None:
            low, high = _range(scan_object["range"])
    else:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Scan object needs to be either a string or an object",
        )
    sources = _parse(node, text)
    if not sources[0].descriptor.is_ranged:
        low = high = 0
    return sources, low, high


def scan_tx_out_set(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any] | bool | Generator[bool, None, dict[str, Any]] | None:
    """Answer `scantxoutset`: report on, stop, or run a scan of the UTXO set.

    `start` searches the stored coins for the scripts of the scan objects'
    descriptors, a ranged one expanded over its `range` and `combo` over
    its scripts, and answers each coin found with the descriptor that
    describes its script. A coin is listed once, as Core's `std::map` has
    it, and is found in the set as it was when the scan began: the
    chainstate is flushed and a cursor opened, whose view RocksDB fixes
    (`UtxoIndex.cursor`). `success` is false for a scan that was aborted
    or met a record it cannot read, and the rest of the answer is what was
    found by then.

    A coin's `desc` is Core's `InferDescriptor` of its script, read with
    what the scan object's descriptors tell a provider at each index of
    its range. Core infers every script before the walk; here only a
    script a coin has is inferred, which answers the same.

    A descriptor parse error has Core's code and btclib_wallet's message.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["scantxoutset"])
    action = params[0]
    scan_objects = params[1] if len(params) > 1 else None
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(action, str):
        mismatches.append((1, "action", action, "string"))
    if scan_objects is not None and not isinstance(scan_objects, list):
        mismatches.append((2, "scanobjects", scan_objects, "array"))
    if mismatches:
        raise type_errors(*mismatches)

    scan = _scans.get(node)
    if action == "status":
        return None if scan is None else {"progress": scan.progress}
    if action == "abort":
        if scan is None:
            return False
        scan.abort = True
        return True
    if action == "start":
        return _start(node, params)
    raise RpcError(RPCErrorCode.INVALID_PARAMETER, f"Invalid action '{action}'")


def _start(node: Node, params: list[Any]) -> Generator[bool, None, dict[str, Any]]:
    """Run `scantxoutset`'s `start`, `_Scan` held for as long as it runs."""
    if node in _scans:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            'Scan already in progress, use action "abort" or "status"',
        )
    scan = _scans[node] = _Scan()
    try:
        return (yield from _scan(node, scan, params))
    finally:
        del _scans[node]


def _interruption_point(node: Node) -> None:
    """Raise where the node is stopping, as Core's interruption point does."""
    if node.terminate_flag.is_set():
        raise RpcError(RPCErrorCode.CLIENT_NOT_CONNECTED, "Shutting down")


class _Merged:
    """Providers merged in the order they are added, the first entry winning.

    Core's `FlatSigningProvider::Merge` into one provider. `Provider.merged`
    copies both sides, so merging each into the whole is quadratic in the
    range; merging equal sizes, as a binary counter carries, is not.

    Both methods yield after a merge of `_MERGE_STEP` providers or more.
    One merge cannot be split, and the last ones copy every key of the
    range, so the longest step grows with the range.
    """

    def __init__(self, node: Node) -> None:
        self._node = node
        self._stack: list[tuple[int, Provider]] = []

    def _merged(
        self, earlier: Provider, later: Provider, size: int
    ) -> Generator[bool, None, Provider]:
        merged = earlier.merged(later)
        if size >= _MERGE_STEP:
            yield True
            _interruption_point(self._node)
        return merged

    def add(self, provider: Provider) -> Generator[bool]:
        size = 1
        while self._stack and self._stack[-1][0] == size:
            earlier_size, earlier = self._stack.pop()
            size += earlier_size
            provider = yield from self._merged(earlier, provider, size)
        self._stack.append((size, provider))

    def provider(self) -> Generator[bool, None, Provider]:
        if not self._stack:
            return Provider()
        size, merged = self._stack[0]
        for later_size, later in self._stack[1:]:
            size += later_size
            merged = yield from self._merged(merged, later, size)
        return merged


def _expand(
    node: Node, scan: _Scan, scan_objects: list[Any]
) -> Generator[bool, None, tuple[dict[bytes, int], list[Provider]]]:
    """Return the scripts the scan objects describe, and their providers.

    Each script maps to the first scan object describing it, as Core's
    `descriptors.emplace` keeps the first, and that scan object's provider
    is the one its `desc` is inferred with.
    """
    needles: dict[bytes, int] = {}
    providers: list[Provider] = []
    keys = 0
    for scan_object in scan_objects:
        sources, low, high = _scan_object(node, scan_object)
        merged = _Merged(node)
        for index in range(low, high + 1):
            for source in sources:
                try:
                    scripts = source.descriptor.script_pub_keys(index, source.prv_keys)
                    provider = source.descriptor.provider(index, source.prv_keys)
                except BTClibException as err:
                    # Core's rpc/util.cpp:1370 (bitcoin/bitcoin@db0bde16b9)
                    # quotes the descriptor. This tree departs on purpose, by
                    # the maintainer's decision: the descriptor may hold
                    # private keys. Core's rpc/mining.cpp:229 and
                    # rpc/output_script.cpp:241 quote nothing.
                    raise RpcError(
                        RPCErrorCode.INVALID_ADDRESS_OR_KEY,
                        "Cannot derive script without private keys",
                    ) from err
                yield from merged.add(provider)
                for script in scripts:
                    needles.setdefault(script.script, len(providers))
                keys += max(1, len(source.descriptor.key_expressions))
            if keys >= _EXPAND_STEP:
                keys = 0
                yield True
                _interruption_point(node)
        providers.append((yield from merged.provider()))
    return needles, providers


def _walk(
    node: Node,
    scan: _Scan,
    cursor: Iterator[tuple[bytes, bytes]],
    needles: dict[bytes, int],
    found: dict[tuple[bytes, int], Coin],
) -> Generator[bool, None, tuple[bool, int]]:
    """Read `cursor`'s coins into `found` where a script is `needles`'.

    Return whether the walk was whole, and the coins read: Core's
    `FindScriptPubKey`.
    """
    prefix = len(b"utxo-")
    count = 0
    for key, value in cursor:
        try:
            coin = Coin.parse(value, check_validity=False)
        except BTClibValueError:
            return False, count
        count += 1
        if count % _STEP == 0:
            yield True
            _interruption_point(node)
            if scan.abort:
                return False, count
        if count % _PROGRESS_EVERY == 0:
            scan.progress = int(
                (0x100 * key[prefix] + key[prefix + 1]) * 100.0 / 65536.0 + 0.5
            )
        if coin.tx_out.script_pub_key.script in needles:
            out_point = key[prefix:]
            found[out_point[:32], int.from_bytes(out_point[32:], "little")] = coin
    scan.progress = 100
    return True, count


def _scan(
    node: Node, scan: _Scan, params: list[Any]
) -> Generator[bool, None, dict[str, Any]]:
    """Expand the scan objects, walk the coins and answer, as `start` does."""
    if len(params) < 2:  # noqa: PLR2004
        raise RpcError(
            RPCErrorCode.MISC_ERROR,
            "scanobjects argument is required for the start action",
        )
    if params[1] is None:
        raise RpcError(
            RPCErrorCode.TYPE_ERROR,
            "JSON value of type null is not of expected type array",
        )
    needles, providers = yield from _expand(node, scan, params[1])
    # `g_should_abort_scan = false`: an abort during the expansion is lost
    scan.abort = False
    node.chainstate.flush()
    hashes = list(node.chainstate.block_index.active_chain)
    cursor = node.chainstate.utxo_index.cursor()
    found: dict[tuple[bytes, int], Coin] = {}
    walked = yield from _walk(node, scan, cursor, needles, found)
    success, count = walked
    tip_height = len(hashes) - 1
    unspents: list[dict[str, Any]] = []
    total = 0
    # `std::map<COutPoint, Coin>`: by txid as stored, then by index
    for (txid, vout), coin in sorted(found.items(), key=lambda item: item[0]):
        script = coin.tx_out.script_pub_key.script
        total += coin.tx_out.value
        unspents.append(
            {
                "txid": txid[::-1],
                "vout": vout,
                "scriptPubKey": script,
                "desc": infer_descriptor(
                    script, providers[needles[script]], node.chain.name
                ),
                "amount": btc_amount(coin.tx_out.value),
                "coinbase": coin.is_coinbase,
                "height": coin.height,
                "blockhash": hashes[coin.height],
                "confirmations": tip_height - coin.height + 1,
            }
        )
        if len(unspents) % _STEP == 0:
            yield True
    return {
        "success": success,
        "txouts": count,
        "height": tip_height,
        "bestblock": hashes[-1],
        "unspents": unspents,
        "total_amount": btc_amount(total),
    }
