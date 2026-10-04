# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The mining RPCs: `generatetoaddress`, `generateblock` and `getblocktemplate`.

Core's `src/rpc/mining.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
tag. Each handler has `rpc.callbacks`' signature and runs on `Node`'s
thread, so it may build a block and hand it to `update_chain` without a
lock (`ARCHITECTURE.md`). `btclib_node.mining` builds and checks the
blocks.

`getblocktemplate` serves `proposal` mode and `template` mode without
`longpollid`: a long poll waits for a new tip, and nothing here may wait
on `Node`'s thread. The nonce search of `generatetoaddress` and
`generateblock` does hold it, up to `maxtries` hashes (see
`btclib_node.mining`), where Core's RPC threads leave its message loop
free.
"""

from __future__ import annotations

import re
import string
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import RPCErrorCode
from btclib import b32
from btclib.block import Block
from btclib.block.limits import MAX_BLOCK_SIGOPS_COST
from btclib.consensus import MAX_BLOCK_WEIGHT, WITNESS_SCALE_FACTOR
from btclib.exceptions import BTClibException
from btclib.script.script_pub_key import ScriptPubKey
from btclib.tx import Tx
from btclib_wallet.descriptors import parse as parse_descriptor

from btclib_node.chainstate.block_index import BlockStatus, block_time
from btclib_node.mining import (
    DEFAULT_MAX_TRIES,
    BlockTemplate,
    TemplateError,
    accept_block,
    block_with_transactions,
    check_block_validity,
    create_new_block,
    solve_block,
)
from btclib_node.rpc.errors import (
    RpcError,
    bool_param,
    json_type_name,
    type_error,
    type_errors,
)
from btclib_node.rpc.help import HELP_TEXT
from btclib_node.signet import SIGNET_CHALLENGE

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

__all__ = ["generate_block", "generate_to_address", "get_block_template"]

_HEX_DIGITS = frozenset(string.hexdigits)

# `getInt<int>()`'s own range
_INT_RANGE = range(-(2**31), 2**31)

_UINT64_MASK = 2**64 - 1

# `MAX_BLOCK_SERIALIZED_SIZE` (`src/consensus/consensus.h`, at
# bitcoin/bitcoin@9be056a8a7), which `btclib.block.limits` leaves out
_MAX_BLOCK_SERIALIZED_SIZE = 4_000_000


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int_param(value: object) -> int:
    """`UniValue::getInt<int>()`: a number with an integer value in range."""
    if not isinstance(value, int) or value not in _INT_RANGE:
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    return value


def _address_script(node: Node, text: str) -> ScriptPubKey | None:
    """Return the script `text` pays here, `None` for a refused address.

    Core's `DecodeDestination`, which takes the prefixes of the chain it
    runs on and no other. The address is decoded by btclib, then spelled
    again for this chain, and it is this chain's if the two agree.
    """
    try:
        decoded = ScriptPubKey.from_address(text)
    except BTClibException:
        return None
    here = ScriptPubKey(decoded.script, node.chain.name)
    spelled = here.address
    same = spelled == text.lower() if b32.is_segwit_prefixed(text) else spelled == text
    return here if same else None


# an extended key as written in a descriptor, whatever follows it
_EXTENDED_KEY = re.compile(r"[a-zA-Z]{4}[1-9A-HJ-NP-Za-km-z]{100,}")


def _has_key_of_another_chain(node: Node, text: str) -> bool:
    """Say whether `text` holds an extended key that is not this chain's.

    Core refuses it in `Parse`, before any other check; btclib_wallet's
    `parse` refuses a multipath expression first.
    """
    for key in _EXTENDED_KEY.findall(text):
        prv_keys: dict[str, str] = {}
        try:
            parse_descriptor(f"pkh({key})", node.chain.name, prv_keys).script_pub_keys(
                0, prv_keys
            )
        except BTClibException as err:
            return "key: version" in str(err)
    return False


def _descriptor_script(node: Node, text: str) -> ScriptPubKey | None:
    """Return the script of the descriptor `text`, `None` where it is not one.

    Core's `getScriptFromDescriptor`: a `combo` descriptor holds two
    scripts, or four for an uncompressed key, and the p2wpkh one is taken
    from four and the p2pkh one from two.

    A key of another chain is refused: a WIF by `parse`, an extended key by
    `_has_key_of_another_chain`.
    """
    # Core's order: a key of another chain fails `Parse`, then a multipath
    # descriptor, then a ranged one, then `Expand`
    if _has_key_of_another_chain(node, text):
        return None
    try:
        # `parse` moves each xprv into `prv_keys`, which a hardened step needs
        prv_keys: dict[str, str] = {}
        descriptor = parse_descriptor(text, node.chain.name, prv_keys)
    except BTClibException as err:
        if "multipath" in str(err):
            raise RpcError(
                RPCErrorCode.INVALID_PARAMETER, "Multipath descriptor not accepted"
            ) from err
        return None
    if descriptor.is_ranged:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Ranged descriptor not accepted. Maybe pass through deriveaddresses first?",
        )
    try:
        scripts = descriptor.script_pub_keys(0, prv_keys)
    except BTClibException as err:
        # Core's `Expand` fails the same way for any key it cannot derive
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY,
            "Cannot derive script without private keys",
        ) from err
    if len(scripts) == 1:
        return scripts[0]
    return scripts[2] if len(scripts) == 4 else scripts[1]  # noqa: PLR2004


def _new_block(node: Node, script_pub_key: ScriptPubKey) -> BlockTemplate:
    """Build a template, refused as Core's `std::runtime_error` is."""
    try:
        return create_new_block(node, script_pub_key)
    except TemplateError as err:
        raise RpcError(RPCErrorCode.MISC_ERROR, str(err)) from err


def _submit(node: Node, block: Block) -> None:
    """Hand a solved block to the chain, as `ProcessNewBlock` does."""
    if accept_block(node, block) is not None:
        raise RpcError(
            RPCErrorCode.INTERNAL_ERROR, "ProcessNewBlock, block not accepted"
        )


def generate_to_address(
    node: Node, conn: RpcConnection, params: list[Any]
) -> list[bytes]:
    """Answer `generatetoaddress`: mine `nblocks` blocks paying `address`.

    Core's `generateBlocks` (`src/rpc/mining.cpp`): each block holds what
    the mempool offers, and mining stops early where `maxtries` is spent,
    answering the hashes of the blocks found. A negative `maxtries` is
    read as the unsigned number it converts to, as Core's `uint64_t` does.
    Core refuses no chain. Here a search holds `Node`'s thread for up to
    `maxtries` hashes, and until a block is found for a negative one
    (btclib-org/btclib-node#1622).
    """
    if len(params) < 2:  # noqa: PLR2004
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["generatetoaddress"])
    nblocks, address = params[0], params[1]
    max_tries = params[2] if len(params) > 2 else None  # noqa: PLR2004
    mismatches: list[tuple[int, str, object, str]] = []
    if not _is_number(nblocks):
        mismatches.append((1, "nblocks", nblocks, "number"))
    if not isinstance(address, str):
        mismatches.append((2, "address", address, "string"))
    if max_tries is not None and not _is_number(max_tries):
        mismatches.append((3, "maxtries", max_tries, "number"))
    if mismatches:
        raise type_errors(*mismatches)

    remaining = _int_param(nblocks)
    tries = (
        DEFAULT_MAX_TRIES if max_tries is None else _int_param(max_tries) & _UINT64_MASK
    )
    script_pub_key = _address_script(node, address)
    if script_pub_key is None:
        raise RpcError(RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Error: Invalid address")

    hashes: list[bytes] = []
    while remaining > 0 and not node.terminate_flag.is_set():
        template = _new_block(node, script_pub_key)
        block, tries = solve_block(node, template.block, tries)
        if block is None:
            if tries == 0 or node.terminate_flag.is_set():
                break
            continue
        _submit(node, block)
        remaining -= 1
        hashes.append(block.header.hash)
    return hashes


def _is_hex(text: str) -> bool:
    """Core's `IsHex`: a non-empty even-length string of hexadecimal digits."""
    return bool(text) and len(text) % 2 == 0 and all(c in _HEX_DIGITS for c in text)


def _parse_hex_tx(text: str) -> Tx | None:
    """Decode a hex transaction as `DecodeHexTx` does, `None` where it fails."""
    if not _is_hex(text):
        return None
    try:
        return Tx.parse(bytes.fromhex(text), check_validity=False)
    except BTClibException:
        return None


def _parse_hex_block(text: str) -> Block | None:
    """Decode a hex block as `DecodeHexBlk` does, `None` where it fails.

    Core ignores bytes after the block; btclib's parser refuses them, so
    here a block with a trailing byte fails to decode.
    """
    if not _is_hex(text):
        return None
    try:
        return Block.parse(bytes.fromhex(text), check_validity=False)
    except BTClibException:
        return None


def _string_item(value: object) -> str:
    """Return `value` where it is a string, else Core's `get_str` error."""
    if not isinstance(value, str):
        raise RpcError(
            RPCErrorCode.TYPE_ERROR,
            f"JSON value of type {json_type_name(value)} is not of expected type string",
        )
    return value


def _generateblock_transaction(node: Node, text: str) -> Tx:
    """Return one transaction of `generateblock`'s list, a txid or a raw one."""
    if len(text) == 64 and _is_hex(text):  # noqa: PLR2004
        tx = node.mempool.get_tx(bytes.fromhex(text))
        if tx is None:
            raise RpcError(
                RPCErrorCode.INVALID_ADDRESS_OR_KEY,
                f"Transaction {text} not in mempool.",
            )
        return tx
    tx = _parse_hex_tx(text)
    if tx is None:
        raise RpcError(
            RPCErrorCode.DESERIALIZATION_ERROR,
            f"Transaction decode failed for {text}. "
            "Make sure the tx has at least one input.",
        )
    return tx


def _output_script(node: Node, output: str) -> ScriptPubKey:
    """Return the script `generateblock` pays, of a descriptor or an address."""
    script_pub_key = _descriptor_script(node, output) or _address_script(node, output)
    if script_pub_key is None:
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Error: Invalid address or descriptor"
        )
    return script_pub_key


def generate_block(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `generateblock`: mine one block of exactly the given transactions.

    Core's `generateblock` (`src/rpc/mining.cpp`): the block pays the
    subsidy to `output`, an address or a descriptor, and holds the
    transactions in the order given, a txid being looked up in the
    mempool. The block is checked as `TestBlockValidity` checks it and
    refused with `RPC_VERIFY_ERROR` and the reason, which is Core's word
    where `btclib_node.mining` has one. Core's debug message after the
    reason is left off: `check_block_validity` answers the reason alone,
    as BIP22 does, where Core's `ToString` adds the debug text here. It
    is submitted unless `submit` is false, and answered as `hex` then too.
    """
    if len(params) < 2:  # noqa: PLR2004
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["generateblock"])
    output, transactions = params[0], params[1]
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(output, str):
        mismatches.append((1, "output", output, "string"))
    if not isinstance(transactions, list):
        mismatches.append((2, "transactions", transactions, "array"))
    if mismatches:
        raise type_errors(*mismatches)
    submit = bool_param(params, 2, name="submit", default=True)

    script_pub_key = _output_script(node, output)
    txs = [
        _generateblock_transaction(node, _string_item(item)) for item in transactions
    ]
    block = block_with_transactions(node, script_pub_key, txs)
    reason = check_block_validity(node, block, check_merkle_root=False)
    if reason is not None:
        raise RpcError(RPCErrorCode.VERIFY_ERROR, f"TestBlockValidity failed: {reason}")
    solved, _ = solve_block(node, block, DEFAULT_MAX_TRIES)
    if solved is None:
        raise RpcError(RPCErrorCode.MISC_ERROR, "Failed to make block.")
    if submit:
        _submit(node, solved)
        return {"hash": solved.header.hash}
    return {
        "hash": solved.header.hash,
        "hex": solved.serialize(check_validity=False).hex(),
    }


def _proposal(node: Node, request: dict[str, Any]) -> str | None:
    """`getblocktemplate`'s `proposal` mode: why the block in `data` is refused.

    `"duplicate"`, `"duplicate-invalid"` or `"duplicate-inconclusive"`
    for a block this node indexes, by how far the block got: connected
    once, marked invalid, or neither. Otherwise BIP22's answer to
    `check_block_validity`, null for a valid block and the reason for
    the rest.
    """
    data = request.get("data")
    if not isinstance(data, str):
        raise RpcError(RPCErrorCode.TYPE_ERROR, "Missing data String key for proposal")
    block = _parse_hex_block(data)
    if block is None:
        raise RpcError(RPCErrorCode.DESERIALIZATION_ERROR, "Block decode failed")

    known = node.chainstate.block_index.header_dict.get(block.header.hash)
    if known is not None:
        # Core's genesis was never connected by `ConnectBlock`
        if block.header.hash == node.chain.genesis.hash:
            return "duplicate-inconclusive"
        if known.status in (BlockStatus.valid, BlockStatus.in_active_chain):
            return "duplicate"
        if known.status == BlockStatus.invalid:
            return "duplicate-invalid"
        return "duplicate-inconclusive"
    return check_block_validity(node, block, check_merkle_root=True)


def _check_template_readiness(node: Node) -> None:
    """Refuse a mainnet template without a peer, or during initial sync."""
    if node.chain.name != "mainnet":
        return
    manager = node.p2p_manager
    if not manager.connections and not manager.pending_connections:
        raise RpcError(
            RPCErrorCode.CLIENT_NOT_CONNECTED, "Btclib node is not connected!"
        )
    if node.is_initial_block_download:
        raise RpcError(
            RPCErrorCode.CLIENT_IN_INITIAL_DOWNLOAD,
            "Btclib node is in initial sync and waiting for blocks...",
        )


def _template(node: Node, client_rules: set[str]) -> dict[str, Any]:
    """`getblocktemplate`'s `template` mode, keyed in Core's order."""
    consensus = node.chain.consensus
    signet = node.chain.name == "signet"
    if signet and "signet" not in client_rules:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "getblocktemplate must be called with the signet rule set "
            '(call with {"rules": ["segwit", "signet"]})',
        )
    if "segwit" not in client_rules:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "getblocktemplate must be called with the segwit rule set "
            '(call with {"rules": ["segwit"]})',
        )

    # Core passes no output script: the template names only its value
    template = _new_block(node, ScriptPubKey(b"", node.chain.name))
    block = template.block
    header = block.header
    height = len(node.chainstate.block_index.active_chain)
    pre_segwit = height < consensus.segwit_height

    transactions: list[dict[str, Any]] = []
    index_of = {block.transactions[0].id: 0}
    for position, tx in enumerate(block.transactions[1:], start=1):
        index_of[tx.id] = position
        sigops = template.sigops[position - 1]
        transactions.append(
            {
                "data": tx.serialize(include_witness=True).hex(),
                "txid": tx.id.hex(),
                "hash": tx.hash.hex(),
                "depends": [
                    index_of[tx_in.prev_out.tx_id]
                    for tx_in in tx.vin
                    if tx_in.prev_out.tx_id in index_of
                ],
                "fee": template.fees[position - 1],
                "sigops": sigops // WITNESS_SCALE_FACTOR if pre_segwit else sigops,
                "weight": tx.weight,
            }
        )

    block_index = node.chainstate.block_index
    status = node.unknown_activations.status(
        block_index.active_chain, block_index.header_dict
    )
    rules = ["csv"]
    if not pre_segwit:
        rules.append("!segwit")
    if signet:
        rules.append("!signet")
    rules.extend(status.active)
    scale = WITNESS_SCALE_FACTOR if pre_segwit else 1
    result: dict[str, Any] = {
        "capabilities": ["proposal"],
        "version": header.version,
        "rules": rules,
        "vbavailable": {**status.signalling, **status.locked_in},
        "vbrequired": 0,
        "previousblockhash": header.previous_block_hash,
        "transactions": transactions,
        "coinbaseaux": {},
        "coinbasevalue": block.transactions[0].vout[0].value,
        "longpollid": header.previous_block_hash.hex() + str(node.mempool.sequence - 1),
        "target": header.target.hex(),
        "mintime": template.min_time,
        "mutable": ["time", "transactions", "prevblock"],
        "noncerange": "00000000ffffffff",
        "sigoplimit": MAX_BLOCK_SIGOPS_COST // scale,
        "sizelimit": _MAX_BLOCK_SERIALIZED_SIZE // scale,
    }
    if not pre_segwit:
        result["weightlimit"] = MAX_BLOCK_WEIGHT
    result["curtime"] = block_time(header)
    result["bits"] = header.bits.hex()
    result["height"] = height
    if signet:
        result["signet_challenge"] = SIGNET_CHALLENGE.hex()
    result["default_witness_commitment"] = (
        block.transactions[0].vout[-1].script_pub_key.script.hex()
    )
    return result


def get_block_template(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any] | str | None:
    """Answer `getblocktemplate`: a template, or a verdict on a block.

    Core's `getblocktemplate` (`src/rpc/mining.cpp`, BIPs 22 and 23). With
    `mode` `"proposal"` the block in `data` is checked on top of the tip
    and not stored, answered null where valid. Otherwise the answer is a
    template built from the mempool, which on mainnet needs a peer and a
    node out of initial block download.

    A `longpollid` is refused: Core's call waits for a new tip or a
    changed mempool, and here nothing may wait on `Node`'s own thread
    (btclib-org/btclib-node#1606).
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["getblocktemplate"])
    request = params[0]
    if not isinstance(request, dict):
        raise type_error(1, "template_request", request, "object")
    mode = request.get("mode")
    if mode is None:
        mode = "template"
    elif not isinstance(mode, str):
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, "Invalid mode")
    if mode == "proposal":
        return _proposal(node, request)

    rules = request.get("rules")
    client_rules = (
        {_string_item(rule) for rule in rules} if isinstance(rules, list) else set()
    )
    if mode != "template":
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, "Invalid mode")
    _check_template_readiness(node)
    if request.get("longpollid") is not None:
        raise RpcError(RPCErrorCode.MISC_ERROR, "longpollid is not supported")
    return _template(node, client_rules)
