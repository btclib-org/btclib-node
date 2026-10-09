# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The mining RPCs, and the three that wait on the same tip.

Core's `generatetoaddress`, `generateblock` and `getblocktemplate` are in
`src/rpc/mining.cpp`, and `waitfornewblock`, `waitforblock` and
`waitforblockheight` in `src/rpc/blockchain.cpp`, where each waits
through the mining interface's `waitTipChanged`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag. Each handler has
`rpc.callbacks`' signature and runs on `Node`'s thread, so it may build a
block and hand it to `update_chain` without a lock (`ARCHITECTURE.md`).
`btclib_node.mining` builds and checks the blocks.

The nonce search of `generatetoaddress` and `generateblock`, a
`getblocktemplate` long poll and the three waits are generators that
`rpc.main` resumes on each pass of `Node`'s loop, where Core runs them on
an RPC thread of their own.
"""

from __future__ import annotations

import re
from time import monotonic
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
    is_hex,
    json_type_name,
    parse_hash_v,
    type_error,
    type_errors,
)
from btclib_node.rpc.help import HELP_TEXT
from btclib_node.signet import SIGNET_CHALLENGE

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

__all__ = [
    "generate_block",
    "generate_to_address",
    "get_block_template",
    "wait_for_block",
    "wait_for_block_height",
    "wait_for_new_block",
]

# `getInt<int>()`'s own range
_INT_RANGE = range(-(2**31), 2**31)

_UINT64_MASK = 2**64 - 1

# `MAX_BLOCK_SERIALIZED_SIZE` (`src/consensus/consensus.h`, at
# bitcoin/bitcoin@9be056a8a7), which `btclib.block.limits` leaves out
_MAX_BLOCK_SERIALIZED_SIZE = 4_000_000

# A long poll's first look at the mempool, and every one after it, in
# seconds (`getblocktemplate`, `src/rpc/mining.cpp`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
_LONG_POLL_FIRST_CHECK = 60
_LONG_POLL_CHECK = 10

# `unsigned int`, the type Core keeps the long poll's counter in
_UINT32_MASK = 2**32 - 1

# what `TrimStringView` trims, and what `std::from_chars` reads
_ATOI_SPACE = " \f\n\r\t\v"
_ATOI_DIGITS = re.compile(r"-?[0-9]+")
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


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
) -> Generator[bool, None, list[bytes]]:
    """Answer `generatetoaddress`: mine `nblocks` blocks paying `address`.

    Core's `generateBlocks` (`src/rpc/mining.cpp`): each block holds what
    the mempool offers, and mining stops early where `maxtries` is spent,
    or the node stops, answering the hashes of the blocks found. A
    negative `maxtries` is read as the unsigned number it converts to, as
    Core's `uint64_t` does. Core refuses no chain. The arguments are
    checked here, and the mining is the generator returned.
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
    return _generate_blocks(node, script_pub_key, remaining, tries)


def _generate_blocks(
    node: Node, script_pub_key: ScriptPubKey, remaining: int, tries: int
) -> Generator[bool, None, list[bytes]]:
    """Mine `remaining` blocks, `tries` hashes at most, yielding after each."""
    hashes: list[bytes] = []
    while remaining > 0 and not node.terminate_flag.is_set():
        template = _new_block(node, script_pub_key)
        block, tries = yield from solve_block(node, template.block, tries)
        if block is None:
            if tries == 0 or node.terminate_flag.is_set():
                break
            continue
        _submit(node, block)
        remaining -= 1
        hashes.append(block.header.hash)
        yield True
    return hashes


def _parse_hex_tx(text: str) -> Tx | None:
    """Decode a hex transaction as `DecodeHexTx` does, `None` where it fails."""
    if not is_hex(text):
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
    if not is_hex(text):
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
    if len(text) == 64 and is_hex(text):  # noqa: PLR2004
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
) -> Generator[bool, None, dict[str, Any]]:
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
    The block is checked here, and the search is the generator returned.
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
    return _generate_block(node, block, submit=submit)


def _generate_block(
    node: Node, block: Block, *, submit: bool
) -> Generator[bool, None, dict[str, Any]]:
    """Solve `block`, and submit it where `submit` holds."""
    solved, _ = yield from solve_block(node, block, DEFAULT_MAX_TRIES)
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

    # Core's `nTransactionsUpdatedLast`, read before the block is built
    updated = node.template_transactions_updated = _transactions_updated(node)
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
        "longpollid": header.previous_block_hash.hex() + str(updated),
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


def _transactions_updated(node: Node) -> int:
    """Return the mempool's counter as Core's `unsigned int` holds it."""
    return node.mempool.transactions_updated & _UINT32_MASK


def _atoi(text: str) -> int:
    """Core's `LocaleIndependentAtoi<int64_t>`: the leading integer, else 0."""
    digits = text.strip(_ATOI_SPACE)
    if digits.startswith("+"):
        if digits.startswith("+-"):
            return 0
        digits = digits[1:]
    match = _ATOI_DIGITS.match(digits)
    if match is None:
        return 0
    return min(max(int(match.group()), _INT64_MIN), _INT64_MAX)


def _long_poll_id(node: Node, long_poll_id: object) -> tuple[bytes, int]:
    """Return the tip and the counter a `longpollid` asks to wait past.

    Core's reading: a string is a tip hash, read by `ParseHashV` from its
    first 64 bytes, then the counter, read by `LocaleIndependentAtoi` and
    kept as an `unsigned int`. Anything else names the tip and the
    counter of the last template.
    """
    if not isinstance(long_poll_id, str):
        tip = node.chainstate.block_index.active_chain[-1]
        return tip, node.template_transactions_updated
    raw = long_poll_id.encode()
    head = raw[:64].decode(errors="replace")
    if len(raw) < 64:  # noqa: PLR2004
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"longpollid must be of length 64 (not {len(raw)}, for '{head}')",
        )
    if not is_hex(head):
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"longpollid must be hexadecimal string (not '{head}')",
        )
    counter = _atoi(raw[64:].decode(errors="replace")) & _UINT32_MASK
    return bytes.fromhex(head), counter


def _long_poll(
    node: Node, client_rules: set[str], watched: bytes, counter: int, check_at: float
) -> Generator[bool, None, dict[str, Any]]:
    """Wait as Core's long poll waits, then answer the template.

    Until the tip is no longer `watched`, or the mempool's counter is no
    longer `counter`, looked at from `check_at` on and every ten seconds
    after each look. A node that stops meanwhile answers Core's
    `RPC_CLIENT_NOT_CONNECTED`.
    """
    while not node.terminate_flag.is_set():
        if node.chainstate.block_index.active_chain[-1] != watched:
            break
        if monotonic() >= check_at:
            if _transactions_updated(node) != counter:
                break
            check_at = monotonic() + _LONG_POLL_CHECK
        yield False
    if node.terminate_flag.is_set():
        raise RpcError(RPCErrorCode.CLIENT_NOT_CONNECTED, "Shutting down")
    return _template(node, client_rules)


def get_block_template(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any] | str | Generator[bool, None, dict[str, Any]] | None:
    """Answer `getblocktemplate`: a template, or a verdict on a block.

    Core's `getblocktemplate` (`src/rpc/mining.cpp`, BIPs 22 and 23). With
    `mode` `"proposal"` the block in `data` is checked on top of the tip
    and not stored, answered null where valid. Otherwise the answer is a
    template built from the mempool, which on mainnet needs a peer and a
    node out of initial block download. With a `longpollid`, the answer
    is `_long_poll`, the generator that waits before building it.
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
    long_poll_id = request.get("longpollid")
    if long_poll_id is None:
        return _template(node, client_rules)
    watched, counter = _long_poll_id(node, long_poll_id)
    check_at = monotonic() + _LONG_POLL_FIRST_CHECK
    return _long_poll(node, client_rules, watched, counter, check_at)


def _deadline(timeout: object) -> float | None:
    """Return when a wait of `timeout` milliseconds ends, `None` for none.

    Core reads the number with `getInt<int>()`, refuses a negative one,
    and takes 0, or no number, as no timeout.
    """
    if timeout is None:
        return None
    milliseconds = _int_param(timeout)
    if milliseconds < 0:
        raise RpcError(RPCErrorCode.MISC_ERROR, "Negative timeout")
    return monotonic() + milliseconds / 1000 if milliseconds else None


def wait_for_block_height(
    node: Node, conn: RpcConnection, params: list[Any]
) -> Generator[bool, None, dict[str, Any]]:
    """Answer `waitforblockheight`: the tip, once it is at `height` or above.

    Core's `waitforblockheight`: `timeout` is in milliseconds, 0 for
    none, and the tip is answered at the timeout too, and once the node
    stops. The arguments are checked here, and the wait is the generator
    returned.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["waitforblockheight"])
    height = params[0]
    timeout = params[1] if len(params) > 1 else None
    mismatches: list[tuple[int, str, object, str]] = []
    if not _is_number(height):
        mismatches.append((1, "height", height, "number"))
    if timeout is not None and not _is_number(timeout):
        mismatches.append((2, "timeout", timeout, "number"))
    if mismatches:
        raise type_errors(*mismatches)
    target = _int_param(height)
    deadline = _deadline(timeout)
    return _wait_for_tip(
        node,
        lambda _tip, tip_height: tip_height >= target,
        deadline,
    )


def wait_for_new_block(
    node: Node, conn: RpcConnection, params: list[Any]
) -> Generator[bool, None, dict[str, Any]]:
    """Answer `waitfornewblock`: the tip, once it is not `current_tip`.

    Core's `waitfornewblock`: `current_tip` is the tip at the call where
    it is left out, and a `current_tip` that is not the tip is answered
    at once. `timeout` is `waitforblockheight`'s, and where the node stops
    the answer is the tip at the call (`_wait_for_tip`).
    """
    timeout = params[0] if params else None
    current_tip = params[1] if len(params) > 1 else None
    mismatches: list[tuple[int, str, object, str]] = []
    if timeout is not None and not _is_number(timeout):
        mismatches.append((1, "timeout", timeout, "number"))
    if current_tip is not None and not isinstance(current_tip, str):
        mismatches.append((2, "current_tip", current_tip, "string"))
    if mismatches:
        raise type_errors(*mismatches)
    deadline = _deadline(timeout)
    if current_tip is None:
        tip = node.chainstate.block_index.active_chain[-1]
    else:
        tip = parse_hash_v("current_tip", current_tip)
    return _wait_for_tip(node, lambda hash_, _height: hash_ != tip, deadline)


def wait_for_block(
    node: Node, conn: RpcConnection, params: list[Any]
) -> Generator[bool, None, dict[str, Any]]:
    """Answer `waitforblock`: the tip, once it is `blockhash`.

    Core's `waitforblock`, whose `blockhash` is read before `timeout`.
    `timeout` is `waitforblockheight`'s, and the tip is compared once per
    pass of the loop (`_wait_for_tip`).
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["waitforblock"])
    block_hash = params[0]
    timeout = params[1] if len(params) > 1 else None
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(block_hash, str):
        mismatches.append((1, "blockhash", block_hash, "string"))
    if timeout is not None and not _is_number(timeout):
        mismatches.append((2, "timeout", timeout, "number"))
    if mismatches:
        raise type_errors(*mismatches)
    wanted = parse_hash_v("blockhash", block_hash)
    deadline = _deadline(timeout)
    return _wait_for_tip(node, lambda hash_, _height: hash_ == wanted, deadline)


def _wait_for_tip(
    node: Node,
    done: Callable[[bytes, int], bool],
    deadline: float | None,
) -> Generator[bool, None, dict[str, Any]]:
    """Wait until `done(hash, height)` holds of the tip, or until `deadline`.

    The tip is read once per pass of `Node`'s loop. A tip that is `done`
    and moves on within one pass is never seen, where Core's
    `WaitTipChanged` wakes on a condition variable and misses only a
    notification that arrives before the waiter runs
    (`src/node/miner.cpp`, at bitcoin/bitcoin@9be056a8a7).

    A timeout answers the tip. Where the node stops, which Core's
    `WaitTipChanged` reports as no tip, the answer is the tip
    of the previous pass, as `waitforblock` and `waitforblockheight` keep
    it. For `waitfornewblock`, which ends at the first tip that differs,
    that is the tip at the call. A stop wins over a pass that finds the
    wait `done`, as it does there.
    """
    block_index = node.chainstate.block_index

    def read() -> tuple[bytes, int]:
        return block_index.active_chain[-1], len(block_index.active_chain) - 1

    current = read()
    while not done(*current):
        if deadline is not None and monotonic() >= deadline:
            break
        yield False
        if node.terminate_flag.is_set():
            break
        current = read()
    return {"hash": current[0], "height": current[1]}
