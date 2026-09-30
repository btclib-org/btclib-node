# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""One handler per JSON-RPC method, and `callbacks`, the table dispatching them.

Every handler shares the signature `(node, conn, params)` that
`rpc.main.handle_rpc` calls each one with, whether or not its own body
reads every argument -- the same shared-signature reasoning `p2p.callbacks`
carries for its own two tables. A request reaches an entry here only
once `rpc.connection.RpcConnection.run` has accepted its credential and
`-rpcwhitelist` its method (`rpc.auth`): a user no whitelist names may
call every entry, `stop` included, unless `-rpcwhitelistdefault` holds.
"""

import math
import time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import RPCErrorCode, chain_from_network
from btclib.block import Block, median_time_past
from btclib.exceptions import BTClibException
from btclib.fee import FeeRate, fee_from_vsize
from btclib.p2p.address import ServiceFlags
from btclib.p2p.limits import PROTOCOL_VERSION
from btclib.script.script import script_to_dict
from btclib.script.script_pub_key import ScriptPubKey, p2ms_m_and_keys, type_and_payload
from btclib.script.spendability import is_unspendable
from btclib.tx import Tx
from btclib.tx.out_point import OutPoint
from btclib_wallet.descriptors import add_checksum, from_address

from btclib_node.block_db import Coin
from btclib_node.chainstate.block_index import BlockStatus, block_time
from btclib_node.config import split_host_port
from btclib_node.constants import MIN_BLOCKS_TO_KEEP, USER_AGENT
from btclib_node.exceptions import MissingPrevoutError, TxRejectedError
from btclib_node.main import (
    assert_valid_block,
    is_block_failed,
    is_cached_invalid,
    new_pow_valid_block,
    parent_lookup,
    passes_check_block,
    prune_up_to_height,
    update_chain,
    verify_mempool_acceptance,
)
from btclib_node.p2p.address import ip_and_port
from btclib_node.p2p.banman import Subnet, is_valid_host, lookup_host, lookup_subnet
from btclib_node.p2p.connection import local_services
from btclib_node.p2p.eviction import Network, is_valid, net_class
from btclib_node.rpc.connection import RawJSON
from btclib_node.rpc.errors import (
    RpcError,
    bool_mismatch,
    bool_param,
    json_type_name,
    type_error,
    type_errors,
)
from btclib_node.rpc.help import HELP_TEXT, answer_help

if TYPE_CHECKING:
    from btclib.block import BlockHeader

    from btclib_node import Node
    from btclib_node.chainstate.block_index import BlockIndex
    from btclib_node.p2p.block_availability import BlockAvailability
    from btclib_node.p2p.connection import Connection
    from btclib_node.rpc.connection import RpcConnection

__all__ = [
    "add_node",
    "arg_names",
    "callbacks",
    "clear_banned",
    "disconnect_node",
    "get_best_block_hash",
    "get_block",
    "get_block_count",
    "get_block_hash",
    "get_block_header",
    "get_blockchain_info",
    "get_chain_tips",
    "get_connection_count",
    "get_mempool_entry",
    "get_mempool_info",
    "get_network_info",
    "get_peer_info",
    "get_raw_mempool",
    "get_raw_transaction",
    "get_tx_out",
    "get_tx_out_set_info",
    "help_rpc",
    "list_banned",
    "ping",
    "prune_blockchain",
    "send_raw_transaction",
    "service_names",
    "set_ban",
    "stop",
    "stop_wait_param",
    "submit_block",
    "test_mempool_accept",
]


def get_best_block_hash(node: Node, conn: RpcConnection, _: list[Any]) -> bytes:
    """Answer `getbestblockhash` with the active chain's own tip."""
    return node.chainstate.block_index.active_chain[-1]


def get_block_count(node: Node, conn: RpcConnection, _: list[Any]) -> int:
    """Answer `getblockcount` with the active chain's own height."""
    # the genesis block is active_chain[0] and Core's own height for it
    # is 0 (src/validation.h's nHeight on the genesis CBlockIndex), so
    # the count is the list's own last index, not its length
    return len(node.chainstate.block_index.active_chain) - 1


def get_blockchain_info(
    node: Node, conn: RpcConnection, _: list[Any]
) -> dict[str, Any]:
    """Answer `getblockchaininfo` with Core's own members this node can answer.

    `chain`: `BitcoinCoreFetcher.assert_network` (btclib) and
    `BitcoinCoreRpcClient.assert_chain` (`bitcoin_core_rpc`) call this
    once before their first fetch, by default, and read `chain` alone --
    proven by asking a real client of a real node here for
    `get_best_block_id` before this callback existed: the very first
    call failed `-32601 Method not found` on `getblockchaininfo`, not on
    the method it asked for. That is why `chain` could not be left out,
    not a reason the rest stayed absent.

    `blocks` is `active_chain`'s own last index, matching
    `get_block_count` above: Core's own "the height of the most-work
    fully-validated chain" (src/rpc/blockchain.cpp:1427, at
    bitcoin/bitcoin@ca7162cde5). `headers` is `header_index`'s own last
    index the same way -- `header_index` is this node's own best known
    header chain, tracked separately from `active_chain` (`BlockIndex`'s
    own class docstring) the way Core's `m_best_header` is tracked
    separately from `ActiveChain()`'s own tip, and answered the same way
    Core answers it: `chainman.m_best_header->nHeight` (src/rpc/
    blockchain.cpp:1428, at bitcoin/bitcoin@ca7162cde5). `bestblockhash`
    is `active_chain`'s own tip, matching `get_best_block_hash` above
    (src/rpc/blockchain.cpp:1429, at bitcoin/bitcoin@ca7162cde5) -- both
    already the display byte order Core's own `GetHex()` answers,
    `BlockHeader.hash` (btclib) being the reversed hash rather than the
    wire's own, confirmed against a real `bitcoind`'s identical
    expression at `tests/integration/bitcoind_test.py:66`.

    `bits` is the tip header's own compact target, `header.bits`, hex
    (Core's `strprintf("%08x", tip.nBits)`, src/rpc/blockchain.cpp:1430,
    at bitcoin/bitcoin@ca7162cde5). `target` is `header.target`
    (btclib), 32 bytes already in the same big-endian order Core's own
    `GetTarget(...).GetHex()` answers (src/rpc/blockchain.cpp:1431, same
    commit) -- `target_from_bits` (`btclib.block.proof_of_work`) is
    Core's `SetCompact`, and `arith_uint256::GetHex` writes each 32-bit
    limb little-endian into a `base_blob` and then reverses that whole
    blob (`src/arith_uint256.cpp:141`, `src/uint256.cpp:11`, same
    commit), which is a plain big-endian print of the magnitude and not
    the reversal a hash's own `GetHex` answers. `difficulty` is
    `header.target`'s ratio against the genesis target, `header.difficulty`
    (btclib) -- the same ratio Core's own `GetDifficulty` computes by
    repeated `*=`/`/=` 256.0 from the compact exponent
    (src/rpc/blockchain.cpp:106, same commit), verified bit for bit
    against that literal loop on regtest's own genesis bits `0x207fffff`
    in this callback's own unit test.

    `time` is the tip header's own timestamp, `block_index.block_time`
    (Core's `CBlockHeader::GetBlockTime`, src/rpc/blockchain.cpp:1433,
    same commit). `mediantime` is btclib's own `median_time_past` of the
    tip, over `main.parent_lookup`'s own walk -- the same call
    `main.verify_mempool_acceptance` already makes of the tip, for
    Core's own `CBlockIndex::GetMedianTimePast` (src/rpc/blockchain.cpp
    :1434, same commit). `chainwork` is `block_index.chainwork`'s own
    entry for the tip, hex and zero-padded to 64 digits the way Core's
    `nChainWork.GetHex()` prints a plain magnitude (src/rpc/
    blockchain.cpp:1450, same commit) -- `get_block_header` above
    answers its own `chainwork` the same way now, closing what used to
    be a divergence from Core between the two (btclib-org/btclib-node#658).

    `initialblockdownload` is `node.is_initial_block_download`,
    `main.update_ibd_status`'s own latch, matching Core's own
    `IsInitialBlockDownload` (src/rpc/blockchain.cpp:1436, at
    bitcoin/bitcoin@ca7162cde5) field for field: chain work against
    `Chain.consensus.minimum_chain_work` and tip age against
    `MAX_TIP_AGE`, not merely whether this node has run out of
    candidates to try.
    `size_on_disk` is `block_db.BlockDB.current_usage`, Core's own
    `CalculateCurrentUsage` (src/rpc/blockchain.cpp:1451, same commit).
    `pruned` is `Config.pruned` (src/rpc/blockchain.cpp:1452, same
    commit); `pruneheight`, present only where `pruned` is true, is the
    first height `block_db.BlockDB.prune_up_to` has not deleted --
    `pruned_up_to + 1`, Core's own "the first block unpruned, all
    previous blocks were pruned" (src/rpc/blockchain.cpp:1455, same
    commit, `prune_height.value() + 1`). `automatic_pruning`, present
    alongside it, is whether `Config.prune_target_mib` is set -- Core's
    own `GetPruneTarget() != PRUNE_TARGET_MANUAL`
    (src/rpc/blockchain.cpp:1457, same commit); `prune_target_size`,
    present only where that is true, is `prune_target_mib` in bytes,
    Core's own unit for the member of the same name.

    Absent, each for its own reason rather than by oversight:
    `verificationprogress`, Core's own `GuessVerificationProgress`
    (src/validation.cpp:5519, at bitcoin/bitcoin@ca7162cde5)
    extrapolating from `ChainTxData`, an assumed transaction rate for
    the chain as a whole, against each block's own accumulated
    transaction count (`CBlockIndex::m_chain_tx_count`) -- `chains.py`
    carries neither the per-chain assumption nor a per-block count, so
    answering this member under Core's own name would answer a number
    carrying none of Core's meaning behind it, rather than a truthful
    one; `warnings`, this node raising none of its own; `signet_challenge`,
    `SigNet` here carrying no configurable challenge (chains.py's own
    genesis is the one public signet); `backgroundvalidation`, present
    on Core's own side only behind an assumeutxo snapshot this node has
    no counterpart to.
    """
    block_index = node.chainstate.block_index
    active_chain = block_index.active_chain
    tip_hash = active_chain[-1]
    tip_header = block_index.header_dict[tip_hash].header
    tip_height = len(active_chain) - 1
    tip_mtp = median_time_past(tip_header, tip_height, parent_lookup(node))
    out: dict[str, Any] = {
        "chain": chain_from_network(node.chain.name),
        "blocks": tip_height,
        "headers": len(block_index.header_index) - 1,
        "bestblockhash": tip_hash,
        "bits": tip_header.bits,
        "target": tip_header.target,
        "difficulty": tip_header.difficulty,
        "time": block_time(tip_header),
        "mediantime": tip_mtp,
        "chainwork": f"{block_index.chainwork[tip_hash]:064x}",
        "initialblockdownload": node.is_initial_block_download,
        "size_on_disk": node.block_db.current_usage(),
        "pruned": node.config.pruned,
    }
    if node.config.pruned:
        out["pruneheight"] = node.block_db.pruned_up_to + 1
        prune_target_mib = node.config.prune_target_mib
        out["automatic_pruning"] = prune_target_mib is not None
        if prune_target_mib is not None:
            out["prune_target_size"] = prune_target_mib * 1024 * 1024
    return out


# Core's own single-argument NUM check, `RPCMethod::HandleRequest`
# against `RPCArg::Type::NUM` -- 1e9 is Core's own boundary between "this
# is a height" and "this is a timestamp" (`rpc/blockchain.cpp:944-945`,
# at bitcoin/bitcoin@ca7162cde5, "Height value more than a billion...");
# `_PRUNE_TIMESTAMP_WINDOW` is Core's own `TIMESTAMP_WINDOW`
# (`chain.h:29,37`, same sha), the two-hour future-drift allowance a
# block's own timestamp may carry, subtracted before the search so a
# block whose real height is later than its timestamp alone would
# suggest is not missed.
_PRUNE_TIMESTAMP_TO_HEIGHT_THRESHOLD = 1_000_000_000
_PRUNE_TIMESTAMP_WINDOW = 2 * 60 * 60


def _height_param(params: list[Any]) -> int:
    """Parse `pruneblockchain`'s own `height` argument, Core's own checks.

    Split out of `prune_blockchain` below only to keep that function's
    own cyclomatic complexity under ruff's `C901` -- every check here is
    still exactly `get_block_hash`'s own, cited there rather than
    repeated in this docstring.
    """
    if not params:
        # a call short of a required argument is `HelpResult{ToString()}`
        # too, the method's own full help text under `RPC_MISC_ERROR`
        # rather than the one-line usage string this used to raise --
        # `rpc.main._execute`'s own docstring is where the sibling case,
        # a call carrying too many, is argued the identical way
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["pruneblockchain"])
    height_param = params[0]
    if isinstance(height_param, bool) or not isinstance(height_param, (int, float)):
        raise type_error(1, "height", height_param, "number")
    if isinstance(height_param, float):
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    if height_param < 0:
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, "Negative block height.")
    return height_param


def _height_from_timestamp(node: Node, timestamp: int) -> int:
    """Find the earliest height whose own time reaches `timestamp` minus drift.

    Core's own `CChain::FindEarliestAtLeast` (`chain.cpp:60-64`, at
    bitcoin/bitcoin@ca7162cde5) binary-searches `GetBlockTimeMax`, a
    running maximum kept for exactly this search to stay valid despite
    the 2-hour drift a timestamp is allowed against its own predecessor;
    `BlockIndex` here carries no counterpart to it, so there is no
    monotonic key left to binary-search on. A linear scan over each
    block's own raw time needs none, at the cost of the same search
    Core answers in `O(log n)` here costing `O(n)`, paid once per call
    rather than a database's own choice.
    """
    target_time = timestamp - _PRUNE_TIMESTAMP_WINDOW
    block_index = node.chainstate.block_index
    active_chain = block_index.active_chain
    found_height = next(
        (
            height
            for height in range(len(active_chain))
            if block_time(block_index.header_dict[active_chain[height]].header)
            >= target_time
        ),
        None,
    )
    if found_height is None:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Could not find block with at least the specified timestamp.",
        )
    return found_height


def prune_blockchain(node: Node, conn: RpcConnection, params: list[Any]) -> int:
    """Answer `pruneblockchain`: manually delete up to `height`, or a timestamp.

    Core's own `pruneblockchain` (`rpc/blockchain.cpp:918-975`, at
    bitcoin/bitcoin@ca7162cde5). Requires `Config.pruned`, matching
    `IsPruneMode()`'s own refusal (`rpc/blockchain.cpp:936-938`) --
    manual pruning (`Config.prune_target_mib` unset) and automatic
    pruning (set) both answer this RPC the same way, Core drawing no
    such distinction for it either; `main._prune_chain` is the one
    place the two differ.

    `height` above `_PRUNE_TIMESTAMP_TO_HEIGHT_THRESHOLD` is read as a
    block time instead, by `_height_from_timestamp` above.

    Refused the way Core refuses it, in Core's own order and wording:
    a missing or wrongly typed `height` (`RPCMethod::HandleRequest`'s
    own generic argument check, same as `get_block_hash` above), a
    negative one (`rpc/blockchain.cpp:945-947`), a chain shorter than
    `chain.prune_after_height` (`rpc/blockchain.cpp:962-963`, Core's own
    per-chain `nPruneAfterHeight` -- 100000 on mainnet, 1000 elsewhere,
    `chains.py`'s own leaves carrying the line each comes from), and a
    `height` past the tip (`rpc/blockchain.cpp:964-965`). A `height`
    within `MIN_BLOCKS_TO_KEEP` of the tip is not refused, only clamped
    down to it (`rpc/blockchain.cpp:966-969`), and pruning still runs --
    that clamp's own floor is `MIN_BLOCKS_TO_KEEP`, not
    `prune_after_height`, matching Core drawing the two apart too.

    Answers `block_db.BlockDB.pruned_up_to`, Core's own "height of the
    last block pruned" (`rpc/blockchain.cpp:927-928`) -- this store
    already tracks exactly that height, so there is no index scan to
    answer it with the way Core's own `GetPruneHeight` runs one.
    """
    if not node.config.pruned:
        raise RpcError(
            RPCErrorCode.MISC_ERROR,
            "Cannot prune blocks because node is not in prune mode.",
        )
    height_param = _height_param(params)
    if height_param > _PRUNE_TIMESTAMP_TO_HEIGHT_THRESHOLD:
        height_param = _height_from_timestamp(node, height_param)

    chain_height = len(node.chainstate.block_index.active_chain) - 1
    if chain_height < node.chain.prune_after_height:
        err_msg = "Blockchain is too short for pruning."
        raise RpcError(RPCErrorCode.MISC_ERROR, err_msg)
    if height_param > chain_height:
        err_msg = "Blockchain is shorter than the attempted prune height."
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, err_msg)
    height_param = min(height_param, chain_height - MIN_BLOCKS_TO_KEEP)

    prune_up_to_height(node, height_param)
    return node.block_db.pruned_up_to


def get_block_hash(node: Node, conn: RpcConnection, params: list[Any]) -> bytes:
    """Answer `getblockhash`, Core's own checks on `height` in Core's order.

    A missing, wrongly typed, non-integral or out-of-range `height` is
    each refused the way `RPCMethod::HandleRequest` and
    `src/rpc/blockchain.cpp:585-601` refuse it, cited beside each check
    below; a height in range answers `active_chain[height]`.
    """
    active_chain = node.chainstate.block_index.active_chain

    if not params:
        # the same mechanism get_block_header's own missing-argument
        # case answers with: RPCMethod::HandleRequest throws HelpResult
        # for a call short of a required argument, and ExecuteCommand's
        # `catch (const std::exception& e)` turns that into Core's own
        # JSONRPCError call, cited below for the shape rather than left
        # commented out -- ERA001 reads it as Python and is wrong.
        # JSONRPCError(RPC_MISC_ERROR, e.what()), src/rpc/server.cpp  # noqa: ERA001
        # :887, carrying the method's own full help text rather than
        # its bare usage line, `rpc.help.HELP_TEXT`'s own module
        # docstring is where that text is read back from
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["getblockhash"])

    height = params[0]
    if isinstance(height, bool) or not isinstance(height, (int, float)):
        # height is declared RPCArg::Type::NUM (src/rpc/blockchain.cpp
        # :585); RPCMethod::HandleRequest checks a declared argument's
        # JSON type before the handler body runs, src/rpc/util.cpp
        # :653-661 -- a JSON bool is its own VBOOL, not VNUM
        # (src/rpc/util.cpp:878-890), so it is refused here the same
        # way blockhash's own wrong-typed argument is
        raise type_error(1, "height", height, "number")
    if isinstance(height, float):
        # a JSON number literal written with a decimal point or
        # exponent is still VNUM, so it passes the check above, but
        # UniValue::getInt<int>()'s std::from_chars fails on it
        # regardless of its value; the std::runtime_error("JSON integer
        # out of range") it throws is ExecuteCommand's generic
        # `catch (const std::exception&)` case, RPC_MISC_ERROR and not
        # RPC_TYPE_ERROR (src/rpc/server.cpp:884-887, src/univalue
        # /include/univalue.h:139-150)
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")

    if height < 0 or height >= len(active_chain):
        # src/rpc/blockchain.cpp:599-601: one check either direction,
        # and the same message both ways -- height < 0 is what used to
        # read the active chain from its own end instead of raising
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, "Block height out of range")

    return active_chain[height]


def _parse_hash_v(name: str, value: str) -> bytes:
    """Answer Core's own `ParseHashV`: a hash from its own 64-character hex.

    `ParseHashV` (`src/rpc/util.cpp:116-124`, at bitcoin/bitcoin@9be056a8a7)
    checks the string's own length before it ever tries to decode it:
    `uint256::FromHex` answers `nullopt` for anything but exactly 64
    characters, so a wrong length is `"<name> must be of length 64 (not
    <n>, for '<value>')"` even where every character is a valid hex
    digit (`"aabb"`, four of them) -- and only a 64-character string
    that still fails to decode reaches the second message, `"<name>
    must be hexadecimal string (not '<value>')"`, `bytes.fromhex`'s own
    `ValueError` standing in for `FromHex`'s own failure at that point.
    `name` is each call site's own choice, matching Core's: `"hash"` for
    `getblockheader`, `"blockhash"` for `getblock`, `"txid"` for
    `gettxout` (`src/rpc/blockchain.cpp:678,828,1258`, same sha), and
    `"parameter 1"`/`"parameter 3"` for `getrawtransaction`'s own two
    hash arguments, Core's own names for them
    (`src/rpc/rawtransaction.cpp:304,317`, same sha).
    """
    if len(value) != 64:  # noqa: PLR2004
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"{name} must be of length 64 (not {len(value)}, for '{value}')",
        )
    try:
        return bytes.fromhex(value)
    except ValueError as error:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"{name} must be hexadecimal string (not '{value}')",
        ) from error


def get_block_header(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any] | str:
    """Answer `getblockheader` for `params[0]`, verbose by Core's own default.

    Not verbose, answers the same eighty bytes a peer is sent on the
    wire, hex-encoded; verbose, answers the object `blockheaderToJSON`
    does, height and confirmations included for a header off the active
    chain as much as for one on it -- each field's own Core citation is
    beside where it is built, below.

    `nTx` is the one member `blockheaderToJSON` answers that this does
    not (`src/rpc/blockchain.cpp:185`, at bitcoin/bitcoin@ca7162cde5):
    Core reads it off `CBlockIndex::nTx`, a count kept beside the header
    once the block is received; `BlockInfo` (`chainstate/block_index.py`)
    carries no such count, only `header`, `index`, `status` and
    `downloaded`, so answering it here would mean parsing the whole
    block body off `block_db` for every call -- a header lookup paying
    a block's own cost, and one that still has nothing to answer for a
    header whose block was never downloaded. Left absent rather than
    answered at that price.
    """
    block_index = node.chainstate.block_index

    if not params:
        # Core answers a missing required argument with its own help
        # text under RPC_MISC_ERROR: RPCMethod::HandleRequest throws
        # HelpResult for a call short of its required arguments, and
        # ExecuteCommand's `catch (const std::exception& e)` is what
        # turns that into JSONRPCError(RPC_MISC_ERROR, e.what()) --
        # read at bitcoin/bitcoin@b91d983f66, src/rpc/server.cpp
        # :874-887
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["getblockheader"])

    # RPCMethod::HandleRequest checks every declared argument's JSON
    # type before the handler body runs at all, src/rpc/util.cpp
    # :653-661 -- both are checked, and every mismatch named, before
    # either is raised on, the way `disconnect_node` above already
    # does for its own two arguments (`type_errors`' own docstring).
    # blockhash is declared RPCArg::Type::STR_HEX, so one of any other
    # JSON type never reaches ParseHashV, refused here before
    # bytes.fromhex sees it; verbose is RPCArg::Type::BOOL,
    # RPCArg::Default{true} (src/rpc/blockchain.cpp:617)
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(params[0], str):
        mismatches.append((1, "blockhash", params[0], "string"))
    verbose_mismatch = bool_mismatch(params, 1, name="verbose")
    if verbose_mismatch is not None:
        mismatches.append(verbose_mismatch)
    if mismatches:
        raise type_errors(*mismatches)

    verbose = bool_param(params, 1, name="verbose", default=True)

    # ParseHashV, src/rpc/util.cpp:116-124, "hash" -- getblockheader's own
    # label, not getblock's "blockhash"
    block_hash = _parse_hash_v("hash", params[0])
    try:
        block_info = block_index.get_block_info(block_hash)
    except KeyError as error:
        # a hash nothing indexed is a question about a block, not a
        # fault of this node: src/rpc/blockchain.cpp:664-665, at
        # bitcoin/bitcoin@ca7162cde5
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Block not found"
        ) from error
    header = block_info.header

    if not verbose:
        # src/rpc/blockchain.cpp:668-673: the same eighty bytes a peer
        # is sent on the wire, hex-encoded rather than the JSON object.
        # Checked here now. It used to be serialized unchecked: a
        # version of zero or below, which Core takes below BIP34's
        # height, was one btclib's own check refused on its own -- fixed
        # at btclib 2026.9.29 (btclib-org/btclib@bbb1ad71, closing
        # btclib-org/btclib#2309; btclib-org/btclib-node#1511). The
        # index only ever stores a header past `add_headers`'s own
        # height-gated `bad-version` check, so nothing this validates
        # can fail on one read back from here.
        return header.serialize().hex()

    # the blocks this node has validated and connected, which is what
    # Core hands blockheaderToJSON: `ActiveChain().Tip()`, at
    # src/rpc/blockchain.cpp:661
    active_chain = block_index.active_chain

    # the block's own height, which is what Core answers with for a
    # block off the active chain as much as for one on it. `BlockInfo`
    # carries it for every header the index holds, where a position in
    # active_chain is a number only the validated ones have.
    height = block_info.index
    on_active_chain = height < len(active_chain) and active_chain[height] == block_hash

    out: dict[str, Any] = {
        # src/rpc/blockchain.cpp:170
        "hash": header.hash,
        # Core's ComputeNextBlockAndDepth, src/rpc/blockchain.cpp:126: a
        # depth is counted from the active chain's tip, and a block
        # that chain does not hold at its own height is answered with
        # -1 rather than a number -- a header whose block was never
        # downloaded is one of those, so header sync alone reports
        # nothing as confirmed (src/rpc/blockchain.cpp:172-173)
        "confirmations": len(active_chain) - height if on_active_chain else -1,
        # src/rpc/blockchain.cpp:174
        "height": height,
        # src/rpc/blockchain.cpp:175
        "version": header.version,
        # strprintf("%08x", nVersion), src/rpc/blockchain.cpp:176 --
        # Core's int32_t printed as its 32 bits, so a negative version,
        # which the index stores below BIP34's height, is `ffffffff` for
        # -1 rather than Python's signed `-0000001`
        "versionHex": f"{header.version & 0xFFFFFFFF:08x}",
        # src/rpc/blockchain.cpp:177 -- Core's own name, not btclib's
        # `to_dict`'s `merkle_root`
        "merkleroot": header.merkle_root,
        # CBlockHeader::GetBlockTime, src/rpc/blockchain.cpp:178 -- the
        # header's own raw timestamp, not `to_dict`'s ISO 8601 string
        "time": block_time(header),
        # CBlockIndex::GetMedianTimePast, src/rpc/blockchain.cpp:179 --
        # the same call `get_blockchain_info` makes of its own tip,
        # walking back from this block instead, on or off the active
        # chain either way, `parent_lookup` reaching either
        "mediantime": median_time_past(header, height, parent_lookup(node)),
        # src/rpc/blockchain.cpp:180
        "nonce": header.nonce,
        # strprintf("%08x", nBits), src/rpc/blockchain.cpp:181 --
        # `header.bits` is already those same four bytes in display
        # order (`block_header.py`'s own class docstring), matching
        # `get_blockchain_info`'s identical `bits` field
        "bits": header.bits,
        # GetTarget(...).GetHex(), src/rpc/blockchain.cpp:182 -- see
        # `get_blockchain_info`'s own citation for why `header.target`
        # already matches Core's `SetCompact`/`GetHex` here
        "target": header.target,
        # GetDifficulty, src/rpc/blockchain.cpp:106 and :183
        "difficulty": header.difficulty,
        # nChainWork.GetHex(), src/rpc/blockchain.cpp:184 -- hex,
        # zero-padded to 64 digits, matching `get_blockchain_info`'s own
        # `chainwork` rather than the plain int this answered before
        # (closes #658)
        "chainwork": f"{block_index.chainwork[block_hash]:064x}",
    }
    if height > 0:
        # the header's own parent, which for a block on the active
        # chain is active_chain[height - 1] and for one off it is the
        # fork's ancestor: Core answers with pprev either way
        # (src/rpc/blockchain.cpp:187-188)
        out["previousblockhash"] = header.previous_block_hash
    # `next` is the active chain's block at height + 1 and only where
    # this block is its parent, which is the same condition read the
    # other way round: nothing follows a block that chain does not hold
    # (src/rpc/blockchain.cpp:189-190)
    if on_active_chain and height < len(active_chain) - 1:
        out["nextblockhash"] = active_chain[height + 1]

    return out


def get_chain_tips(
    node: Node, conn: RpcConnection, _: list[Any]
) -> list[dict[str, Any]]:
    """Answer `getchaintips`: every known tip, Core's own status vocabulary.

    Core's own `getchaintips` (`rpc/blockchain.cpp:1556-1651`, at
    bitcoin/bitcoin@9be056a8a7) finds a tip by one pass over every header
    it knows, keeping the ones nothing else names as its own parent, and
    always adds the active chain's own tip. `block_index.children` is
    this index's own parent -> children table, already kept for
    `BlockIndex.invalidate`'s own walk (that class's own comment on it),
    so a tip here is exactly a hash with no entry in it, or an empty
    one -- the active tip included, nothing ever being staged on top of
    it until a block extends it, so it needs no separate case the way
    Core's own `setTips.insert(active_chain.Tip())` does past its own
    orphans-only loop.

    `branchlen` is the fork's own length (`CChain::FindFork`'s height
    difference, `:1618`) -- `BlockIndex.get_fork_details` already
    returns that same branch, oldest first, for a header not on
    `active_chain`, and its own length is this call's `branchlen`; the
    active tip's own fork is empty by definition, so it is answered `0`
    without calling that method at all.

    `status`, in Core's own priority order (`:1622-1639`): `"active"`
    for the chain's own tip; `"invalid"` for a tip `BlockStatus.invalid`
    already marks, itself or an ancestor, `invalidate` above already
    propagating that mark down every child rather than leaving it for a
    walk here; `"headers-only"` where the fork's own blocks are not
    every one downloaded (Core's own `HaveNumChainTxs`, which needs the
    whole branch's data and not merely this tip's -- `_branch_is_downloaded`'s
    own reasoning, inlined here rather than called, since its own fork
    is this call's own `fork` already); `"valid-fork"` for
    `BlockStatus.valid`, the one status `_finalize_fork`'s own
    `to_remove` loop sets, for a block that was connected once and a
    reorg has since dropped (Core's `IsValid(BLOCK_VALID_SCRIPTS)`); and
    `"valid-headers"` for a fully downloaded `BlockStatus.valid_header`
    that never was (Core's `IsValid(BLOCK_VALID_TREE)`) -- the one status
    left once the other four are ruled out, since every header this
    index carries already passed `_validate_header_batch`'s own
    contextual checks, which is what Core's own final "unknown" case
    answers a status below that nothing indexed here ever reaches.
    """
    block_index = node.chainstate.block_index
    active_chain = block_index.active_chain
    active_tip = active_chain[-1]
    tips = [h for h in block_index.header_dict if not block_index.children.get(h)]

    out: list[dict[str, Any]] = []
    for tip_hash in tips:
        block_info = block_index.get_block_info(tip_hash)
        entry: dict[str, Any] = {"height": block_info.index, "hash": tip_hash}
        if tip_hash == active_tip:
            entry["branchlen"] = 0
            entry["status"] = "active"
        else:
            fork, _tail = block_index.get_fork_details(tip_hash)
            entry["branchlen"] = len(fork)
            if block_info.status == BlockStatus.invalid:
                entry["status"] = "invalid"
            elif not all(block_index.get_block_info(h).downloaded for h in fork):
                entry["status"] = "headers-only"
            elif block_info.status == BlockStatus.valid:
                entry["status"] = "valid-fork"
            else:
                entry["status"] = "valid-headers"
        out.append(entry)
    # Core's own CompareBlocksByHeight: height descending, ties broken so
    # that unequal blocks at one height never compare equal -- pointer
    # identity there, the hash itself here, both being arbitrary but
    # stable within one answer.
    out.sort(key=lambda entry: (-entry["height"], entry["hash"]))
    return out


def _coinbase_tx_dict(coinbase: Tx) -> dict[str, Any]:
    """Answer `getblock`'s own `coinbase_tx`, Core's `coinbaseTxToJSON`.

    `coinbaseTxToJSON` (`src/rpc/blockchain.cpp:183-199`, at
    bitcoin/bitcoin@9be056a8a7): the coinbase input's own version,
    locktime, sequence and `scriptSig` -- named `coinbase` here, not
    `scriptSig`, the one field this whole method answers under a name
    that is not the script's own -- plus its witness stack's one
    element, where segwit gives it one at all.
    """
    vin0 = coinbase.vin[0]
    out: dict[str, Any] = {
        "version": coinbase.version,
        "locktime": coinbase.lock_time,
        "sequence": vin0.sequence,
        "coinbase": vin0.script_sig.hex(),
    }
    stack = vin0.script_witness.stack
    if stack:
        # `CHECK_NONFATAL(witness_stack.size() == 1)` (same file, :195):
        # a coinbase carries at most the one segwit commitment element.
        out["witness"] = stack[0].hex()
    return out


def _block_json_header(
    node: Node, block_hash: bytes, header: BlockHeader, height: int, n_tx: int
) -> dict[str, Any]:
    """Answer the header fields `blockToJSON` shares with `blockheaderToJSON`.

    Mirrors `get_block_header`'s own verbose branch field for field,
    each one's own Core citation living there rather than repeated
    here -- `nTx` is the one field that branch leaves out, its own
    docstring arguing why: no block body to count transactions from.
    `get_block` always has the body by the time this runs, so it is
    answered here rather than left absent a second time.
    """
    block_index = node.chainstate.block_index
    active_chain = block_index.active_chain
    on_active_chain = height < len(active_chain) and active_chain[height] == block_hash
    out: dict[str, Any] = {
        "hash": header.hash,
        "confirmations": len(active_chain) - height if on_active_chain else -1,
        "height": height,
        "version": header.version,
        "versionHex": f"{header.version & 0xFFFFFFFF:08x}",
        "merkleroot": header.merkle_root,
        "time": block_time(header),
        "mediantime": median_time_past(header, height, parent_lookup(node)),
        "nonce": header.nonce,
        "bits": header.bits,
        "target": header.target,
        "difficulty": header.difficulty,
        "chainwork": f"{block_index.chainwork[block_hash]:064x}",
        "nTx": n_tx,
    }
    if height > 0:
        out["previousblockhash"] = header.previous_block_hash
    if on_active_chain and height < len(active_chain) - 1:
        out["nextblockhash"] = active_chain[height + 1]
    return out


def _parse_get_block_params(params: list[Any]) -> tuple[bytes, int]:
    """Validate `getblock`'s own two arguments; answer `(blockhash, verbosity)`.

    `ParseVerbosity` (`rpc/util.cpp:89-102`, at bitcoin/bitcoin@9be056a8a7)
    is what a missing argument answers with `default_verbosity`, 1 here,
    and what a JSON bool degrades to (`true` as `1`, `false` as `0`)
    under this argument's own `skip_type_check` (`:772`) -- the same
    allowance `get_raw_transaction`'s own `verbose` argument does not
    carry, argued in that function's own missing-argument comment. Refusing
    anything but 0, 1 or 2 is this tree's own boundary, argued in
    `get_block`'s docstring below.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["getblock"])
    if not isinstance(params[0], str):
        raise type_error(1, "blockhash", params[0], "string")
    # `ParseHashV(request.params[0], "blockhash")` (`rpc/blockchain.cpp
    # :828`, same sha) -- `getblock`'s own label, not `get_block_header`'s
    # "hash", the two RPCs naming the same positional argument differently.
    block_hash = _parse_hash_v("blockhash", params[0])

    has_verbosity = len(params) > 1 and params[1] is not None
    verbosity_param: Any = params[1] if has_verbosity else 1
    if isinstance(verbosity_param, bool):
        verbosity = int(verbosity_param)
    elif isinstance(verbosity_param, float):
        # `UniValue::getInt<int>()` (`src/univalue/include/univalue.h
        # :142-153`), which `ParseVerbosity` calls once `skip_type_check`
        # has let a non-bool, non-null value through unchecked: a
        # non-integral number is `RPC_MISC_ERROR`, the same message
        # `get_block_hash`'s own float check answers above.
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    elif isinstance(verbosity_param, int):
        verbosity = verbosity_param
    else:
        raise type_error(2, "verbosity", verbosity_param, "number")

    if verbosity not in (0, 1, 2):
        raise RpcError(
            RPCErrorCode.MISC_ERROR,
            "getblock: only verbosity 0, 1 and 2 are served here",
        )
    return block_hash, verbosity


def get_block(
    node: Node, conn: RpcConnection, params: list[Any]
) -> str | dict[str, Any]:
    """Answer `getblock` at verbosity 0, 1 or 2: hex, or Core's own JSON shape.

    Core's own `getblock` (`rpc/blockchain.cpp:761-841`, calling
    `blockToJSON`, `:200-243`, at bitcoin/bitcoin@9be056a8a7) answers
    verbosity 1 with the header fields `blockheaderToJSON` answers
    (`get_block_header`'s own verbose branch, mirrored by
    `_block_json_header` above) plus `strippedsize`, `size`, `weight`,
    `coinbase_tx` and `tx` as an array of txids (`TxVerbosity::SHOW_TXID`);
    verbosity 2 the same with `tx` as an array of decoded transactions
    instead, `getrawtransaction`'s own verbose shape
    (`get_raw_transaction`'s own `tx.to_dict()` plus `hex`) rather than a
    second rendering of one call's own shape, `TxVerbosity::SHOW_DETAILS`
    -- one field short of Core's, `tx[].fee`, which needs the block's own
    undo data to compute even at this verbosity and not only at 3, left
    absent along with verbosity 3 itself for the same reason
    (btclib-org/btclib-node#1446).
    """
    block_hash, verbosity = _parse_get_block_params(params)

    block_index = node.chainstate.block_index
    try:
        block_info = block_index.get_block_info(block_hash)
    except KeyError as error:
        # `getblock`'s own throw, `rpc/blockchain.cpp:806`, same sha
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Block not found"
        ) from error

    block = node.block_db.get_block(block_hash)
    if block is None:
        # `CheckBlockDataAvailability`'s own two messages
        # (`rpc/blockchain.cpp`, same sha), matching `_find_transaction`
        # below for the identical distinction
        if block_info.index <= node.block_db.pruned_up_to:
            raise RpcError(RPCErrorCode.MISC_ERROR, "Block not available (pruned data)")
        raise RpcError(
            RPCErrorCode.MISC_ERROR, "Block not available (not fully downloaded)"
        )

    if verbosity == 0:
        return block.serialize(check_validity=False).hex()

    out = _block_json_header(
        node, block_hash, block.header, block_info.index, len(block.transactions)
    )
    out["strippedsize"] = block.stripped_size
    out["size"] = block.size
    out["weight"] = block.weight
    out["coinbase_tx"] = _coinbase_tx_dict(block.transactions[0])
    if verbosity == 1:
        out["tx"] = [tx.id for tx in block.transactions]
    else:
        txs: list[dict[str, Any]] = []
        for tx in block.transactions:
            tx_dict: dict[str, Any] = tx.to_dict()
            tx_dict["hex"] = tx.serialize(include_witness=True).hex()
            txs.append(tx_dict)
        out["tx"] = txs
    return out


def _index_submitted_header(block_index: BlockIndex, block: Block) -> str | None:
    """Index a submitted block's header if new; answer why to stop, or None.

    `"duplicate"` for a block already downloaded whose body passes
    `CheckBlock` (`main.passes_check_block`), `"prev-blk-not-found"` for
    a header whose parent is unknown, and btclib's own message for a
    header `add_headers` refuses; `submit_block` argues each.

    A header this node has never indexed is indexed only once its own
    body passes `CheckBlock`: Core's `ProcessNewBlock` asks `CheckBlock`
    before `AcceptBlock`, so a body failing it never reaches
    `AcceptBlockHeader` at all, and the header stays unindexed
    (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
    -- `p2p.callbacks.block`'s own `_refuse_before_indexing` already
    asks the wire path's body in the same order, for the same reason
    (btclib-org/btclib-node#1247). ISS 1339.
    """
    block_hash = block.header.hash
    if block_hash in block_index.header_dict:
        # Core's `ProcessNewBlock` asks `CheckBlock` of every body before
        # `AcceptBlock` finds it already stored, so a body failing it under
        # a stored hash is refused for that reason, and a stored block left
        # as it is (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7)
        if block_index.get_block_info(block_hash).downloaded and passes_check_block(
            block
        ):
            return "duplicate"
    elif passes_check_block(block):
        try:
            if block_index.add_headers([block.header]) is None:
                return "prev-blk-not-found"
        except BTClibException as error:
            # the header itself fails a range/proof-of-work check
            # `_validate_header_batch` makes before anything is indexed
            # -- caught here rather than left to propagate the way
            # `p2p.callbacks.block` lets it, because that callback's own
            # caller punishes the peer for it and `submitblock` has no
            # peer to punish, only a reason to answer
            return str(error)
    return None


def _validate_extending_tip(node: Node, block_hash: bytes) -> str | None:
    """Run `main.update_chain` here, and answer its verdict on `block_hash`.

    Called only where the block just stored extends the active tip --
    `submit_block`'s own docstring argues why, and is where the caller
    already knows that. A failure the trial does not swallow into
    `Node.last_rejected_block` is this node's own storage or bookkeeping
    proving itself unsafe to keep running past, not this submission's
    content, and is left to propagate, `Node.terminate_flag` set first
    so a caller reached from here still stops `Node.run`'s loop for it.
    """
    try:
        update_chain(node)
    except Exception:
        node.terminate_flag.set()
        raise
    failed = node.last_rejected_block
    if failed is not None and failed[0] == block_hash:
        return str(failed[1])
    return None


def submit_block(node: Node, conn: RpcConnection, params: list[Any]) -> str | None:
    """Answer `submitblock`, Core's own two arguments, the second ignored.

    Core's own `submitblock` (`rpc/mining.cpp:1089-1136`, at
    bitcoin/bitcoin@bb529657) decodes, indexes the header if it is new, and
    hands the block to `ProcessNewBlock`: `None` for one accepted,
    `"duplicate"` for one already held, and a reject reason for one refused.
    Some reasons are Core's literally: `"duplicate-invalid"` for one whose
    header is marked invalid (`main.is_cached_invalid`),
    `BlockValidationResult::BLOCK_MISSING_PREV`'s own `"prev-blk-not-found"`
    (`validation.cpp:4225`, same sha), which this node's own
    `block_index.add_headers` answers the identical way
    `p2p.callbacks.block` already reads it (missing rather than invalid),
    `AcceptBlockHeader`'s `"bad-prevblk"` for a parent marked invalid, and
    `ContextualCheckBlockHeader`'s `"bad-version(0x%08x)"`, which
    `add_headers` raises in Core's words (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Any other invalid block is
    answered with btclib's own exception message instead of one of Core's:
    `BlockValidationResult` names dozens of distinct single-word reasons
    across `validation.cpp`, and this tree does not reproduce that
    vocabulary.

    Stores through the same `block_index`/`block_db` calls
    `p2p.callbacks.block` makes for a block delivered over the wire,
    minus that callback's own `Connection`-specific bookkeeping
    (`remove_block_request`), which does not apply to a block submitted
    out of band.

    Where the stored block extends the active tip, `main.update_chain`
    is run right here rather than waited for on `Node`'s next pass:
    Core's own `ProcessNewBlock` calls `AcceptBlock` and then
    `ActivateBestChain` -- contextual and connect validation both --
    before it ever returns to `submitblock` (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), so a caller is answered
    only once that trial has actually run, and never told a block was
    accepted that its own contextual or script check refuses a moment
    later (ISS 1335, ISS 1390). `_ready_fork` is not gated on
    `Node.status` for exactly this reason (its own comment,
    btclib-org/btclib-node#1071): calling it here finds this same
    submission ready, or finds nothing and costs nothing. A block that
    does not extend the tip is stored and left for that later pass, as
    before -- Core's own `ActivateBestChain` runs unconditionally too,
    but a competing, lower branch is not what either issue measured,
    and this tree does not chase it synchronously.

    `main._validate_block`'s own `bad-txns-nonfinal` and `bad-cb-height`
    are Core's literal reasons already; `interpreter.check_transactions`
    wraps a script failure into `BlockScriptVerifyError`, whose own
    `str()` is Core's `block-script-verify-flag-failed (%s)`
    (`CheckInputScripts`, same file and sha) with btclib's own message
    in place of `ScriptErrorString`. Anything else `update_chain`'s
    trial raises for this exact hash is answered with btclib's own
    text, the same divergence the paragraph above already argues for
    every reason this function has no literal word for.

    A failure `update_chain`'s own trial does not swallow into a
    rejection -- this node's own storage or bookkeeping, not the
    submission's content -- is not answered at all: it is left to
    propagate, `Node.terminate_flag` set first, so a caller reached
    from here instead of from `Node`'s own scheduled pass still stops
    the loop rather than running on past it.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["submitblock"])
    if not isinstance(params[0], str):
        raise type_error(1, "hexdata", params[0], "string")
    try:
        block = Block.parse(params[0], check_validity=False)
    except BTClibException as error:
        # src/rpc/mining.cpp:1111-1113, same text
        raise RpcError(
            RPCErrorCode.DESERIALIZATION_ERROR, "Block decode failed"
        ) from error

    block_hash = block.header.hash
    block_index = node.chainstate.block_index

    if is_cached_invalid(block_index, block):
        return "duplicate-invalid"
    refusal = _index_submitted_header(block_index, block)
    if refusal is not None:
        return refusal

    try:
        assert_valid_block(block, node.chain)
    except BTClibException as error:
        # passes_check_block is what is_block_failed itself requires, so
        # a body that never got this far indexed (ISS 1339) has nothing
        # here to invalidate either -- checked before touching the
        # parent, which such a body's own header may never have named
        if passes_check_block(block):
            parent = block_index.get_block_info(block.header.previous_block_hash)
            segwit = parent.index + 1 >= node.chain.consensus.segwit_height
            if is_block_failed(block, check_witness_root=segwit):
                block_index.invalidate(block_hash)
        return str(error)

    extends_tip = block.header.previous_block_hash == block_index.active_chain[-1]
    node.block_db.add_block(block)
    block_index.set_downloaded(block_hash)
    # Core's `AcceptBlock` calls `NewPoWValidBlock` from inside
    # `ProcessNewBlock`, ahead of `ActivateBestChain` (`src/validation.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
    new_pow_valid_block(node, block)
    if extends_tip:
        return _validate_extending_tip(node, block_hash)
    return None


def service_names(services: int) -> list[str]:
    """Return the service bits the way Core's getpeerinfo names them.

    `serviceFlagsToStr`, which is a walk over the set bits from the
    least significant up rather than over the names: a bit a member
    names contributes that name without the ``NODE_`` prefix Core's own
    enum carries, and a bit none names contributes "UNKNOWN[2^n]"
    rather than nothing. Core reserves a range of bits for temporary
    experiments and sends everything else through the BIP process, so a
    bit nobody here has heard of is a service and not an error -- and
    dropping it would report a peer as offering less than it said it
    does.
    """
    names: list[str] = []
    for bit in range(int(services).bit_length()):
        if not services >> bit & 1:
            continue
        flag = ServiceFlags(1 << bit)
        names.append(
            f"UNKNOWN[2^{bit}]"
            if flag.name is None
            else flag.name.removeprefix("NODE_")
        )
    return names


def _network_name(network: Network) -> str:
    """Core's `GetNetworkName`, which names `NET_UNROUTABLE` apart."""
    if network is Network.UNROUTABLE:
        return "not_publicly_routable"
    return network.name.lower()


def _connection_type(p2p_conn: Connection) -> str:
    """Core's `ConnectionTypeAsString` for the six types this node opens.

    An outbound connection `P2pManager` did not draw itself is a
    `-connect`, `-addnode` or `addnode` peer, Core's `MANUAL`.
    """
    if p2p_conn.inbound:
        return "inbound"
    if p2p_conn.block_relay:
        return "block-relay-only"
    if p2p_conn.feeler:
        return "feeler"
    if p2p_conn.addr_fetch:
        return "addr-fetch"
    return "outbound-full-relay" if p2p_conn.automatic else "manual"


def _peer_entry(
    node: Node, connection_id: int, p2p_conn: Connection, addr: str, addrbind: str
) -> dict[str, Any]:
    """Build one `getpeerinfo` entry, in the order Core pushes its keys.

    A connection whose `version` has not arrived answers what Core's
    `CNode` and `Peer` hold before theirs: no `addrlocal`, services and
    version 0, an empty `subver`, and no transaction relay.
    """
    version_message = p2p_conn.version_message
    # Core's `TxRelay` exists only once the peer's `version` asked for
    # relay, this node offering no `NODE_BLOOM`, and never for a
    # block-relay-only peer or a feeler; the fields read off it answer 0
    # or false where it does not.
    relays = (
        version_message is not None
        and version_message.is_relay_requested
        and not p2p_conn.block_relay
        and not p2p_conn.feeler
    )
    services = 0 if version_message is None else version_message.services

    entry: dict[str, Any] = {"id": connection_id, "addr": addr, "addrbind": addrbind}
    # `CopyStats` leaves addrlocal empty unless the address the peer
    # named for this node `IsValid`, and `getpeerinfo` then leaves the
    # key out. Core itself sends the unspecified address where the one
    # it reaches this node at is not routable (`PushNodeVersion`).
    if version_message is not None and is_valid(version_message.addr_recv.ip):
        addr_recv = version_message.addr_recv
        entry["addrlocal"] = ip_and_port(str(addr_recv.ip), addr_recv.port)
    # `ConnectedThroughNetwork`, which is `GetNetClass` of the peer's
    # address, this node having no Tor listener to take an onion
    # inbound on.
    entry["network"] = _network_name(net_class(p2p_conn.address))
    entry["services"] = f"{services:016x}"
    entry["servicesnames"] = service_names(services)
    entry["relaytxes"] = relays
    entry["last_inv_sequence"] = p2p_conn.stats.last_inv_sequence if relays else 0
    entry["inv_to_send"] = len(p2p_conn.tx_announce_queue) if relays else 0
    # Whole seconds, pushed unconditionally, and the ping fields in
    # fractional seconds, each only once it holds a value.
    # `last_block` and `last_transaction` are the last novel block and
    # transaction, `0` until one arrives.
    entry["lastsend"] = int(p2p_conn.last_send)
    entry["lastrecv"] = int(p2p_conn.last_receive)
    entry["last_transaction"] = p2p_conn.last_novel_tx_time
    entry["last_block"] = p2p_conn.last_novel_block_time
    entry["bytessent"] = p2p_conn.stats.bytes_sent
    entry["bytesrecv"] = p2p_conn.stats.bytes_recv
    entry["conntime"] = p2p_conn.connected_time
    entry["timeoffset"] = p2p_conn.stats.time_offset
    if p2p_conn.latency > 0:
        entry["pingtime"] = p2p_conn.latency
    if p2p_conn.min_ping_time < math.inf:
        entry["minping"] = p2p_conn.min_ping_time
    # Nonzero exactly while a ping is outstanding, which is what Core's
    # own test of `m_ping_nonce_sent` asks.
    ping_sent = p2p_conn.ping_sent
    if ping_sent:
        ping_wait = time.time() - ping_sent
        if ping_wait > 0:
            entry["pingwait"] = ping_wait
    entry["version"] = 0 if version_message is None else version_message.version
    # `connect_nodes` (`test_framework.py:568-594`, at
    # bitcoin/bitcoin@bb529657) matches this against the peer's own
    # `getnetworkinfo`-reported `subversion` to find its own connection
    # in the other side's peer list. Core sanitizes the wire bytes
    # through `SanitizeString` before calling this `cleanSubVer`; this
    # node's own user agent is always plain ASCII by construction
    # (`p2p.connection`'s `_USER_AGENT`), so a plain decode already
    # answers what a peer actually announced, unfiltered rather than
    # dropped through a character-class Core built for an arbitrary
    # peer's own claim.
    entry["subver"] = (
        ""
        if version_message is None
        else version_message.user_agent.decode("ascii", errors="replace")
    )
    entry["inbound"] = p2p_conn.inbound
    # This node sends `sendcmpct` announcing low bandwidth, so it selects
    # no high-bandwidth peer: false, what Core answers where it sent none.
    # Whether the peer selected this node is what its own `sendcmpct`
    # asked (p2p.callbacks.sendcmpct), Core's `m_bip152_highbandwidth_from`.
    entry["bip152_hb_to"] = False
    entry["bip152_hb_from"] = p2p_conn.requested_hb_cmpctblocks
    # -1, Core's answer where no low-work headers presync runs, which
    # this node never runs.
    entry["presynced_headers"] = -1
    block_index = node.chainstate.block_index
    entry["synced_headers"], entry["synced_blocks"] = _synced_heights(
        block_index, p2p_conn.block_availability
    )
    entry["inflight"] = [
        block_index.get_block_info(block_hash).index
        for block_hash in p2p_conn.download_queue
    ]
    # Core's `m_addr_relay_enabled`: false for an inbound peer until its
    # first `addr`, `addrv2` or `getaddr` (btclib-org/btclib-node#1178).
    entry["addr_relay_enabled"] = p2p_conn.addr_relay_enabled
    entry["addr_processed"] = p2p_conn.stats.addr_processed
    entry["addr_rate_limited"] = p2p_conn.stats.addr_rate_limited
    # No `-whitelist`/`-whitebind`: no peer holds a permission.
    entry["permissions"] = []
    entry["minfeefilter"] = _btc_amount(p2p_conn.feefilter if relays else 0)
    # Core's tables are `std::map`s, iterated in key order, and push
    # only a type with bytes counted.
    entry["bytessent_per_msg"] = dict(
        sorted(p2p_conn.stats.bytes_sent_per_msg.copy().items())
    )
    entry["bytesrecv_per_msg"] = dict(
        sorted(p2p_conn.stats.bytes_recv_per_msg.copy().items())
    )
    entry["connection_type"] = _connection_type(p2p_conn)
    # No BIP324: every connection is v1, which has no session id.
    entry["transport_protocol_type"] = "v1"
    entry["session_id"] = ""
    return entry


def _synced_heights(
    block_index: BlockIndex, availability: BlockAvailability
) -> tuple[int, int]:
    """Return `synced_headers` and `synced_blocks`, as Core computes them.

    The heights of `pindexBestKnownBlock` and `pindexLastCommonBlock`,
    each -1 where unset (`GetNodeStateStats`, `src/net_processing.cpp`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    best_known, last_common = availability.best_known, availability.last_common
    return (
        -1 if best_known is None else block_index.get_block_info(best_known).index,
        -1 if last_common is None else block_index.get_block_info(last_common).index,
    )


def get_peer_info(
    node: Node, conn: RpcConnection, _: list[Any]
) -> list[dict[str, Any]]:
    """Answer `getpeerinfo`, one entry per connection, in id order.

    A connection still short of `verack` is listed too, as Core lists
    every node `CConnman::GetNodeStats` returns. Each field matches one
    `getpeerinfo` pushes (`src/rpc/net.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag), argued in `_peer_entry` beside where it is built.
    """
    manager = node.p2p_manager
    # The table is built whole, and sorted into a list, before the loop
    # starts: this runs on `Node`'s own loop, under `handle_rpc`, while
    # `P2pManager.remove_connection` and `create_connection` change both
    # dicts on the manager's own loop, and a loop over a live dict is
    # `RuntimeError: dictionary changed size during iteration` the moment
    # one of them does. btclib-org/btclib-node#356
    #
    # `promote_connection` runs on this same thread, under
    # `handle_p2p_handshake`, so no connection moves between the two
    # dicts while they are read. Only a removal or a new connection can
    # land between the two reads: a removed peer is answered as it was or
    # skipped at `getpeername` below, Core's own case of a peer
    # disconnected between `GetNodeStats` and `GetNodeStateStats`.
    peers = {**manager.pending_connections, **manager.connections}
    out: list[dict[str, Any]] = []
    for connection_id, p2p_conn in sorted(peers.items()):
        addresses = _socket_addresses(p2p_conn)
        if addresses is None:
            continue
        addr, addrbind = addresses
        out.append(_peer_entry(node, connection_id, p2p_conn, addr, addrbind))
    return out


def _socket_addresses(p2p_conn: Connection) -> tuple[str, str] | None:
    """Return `getpeerinfo`'s `addr` and `addrbind`, or `None` for a gone peer.

    Core writes addrbind with `CService::ToStringAddrPort`, unconditionally
    -- `addrBind` is set once at construction and never the destination
    string. addr is `m_addr_name`, which is `addrNameIn` where the peer was
    dialled by one and the formatted socket address otherwise
    (`CNode::CNode`, `src/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag): `getpeerinfo` pushes `stats.m_addr_name` as `addr`
    (`src/rpc/net.cpp`, same sha), never recomputing it from the socket.
    `p2p_conn.addr_name` is this node's own `m_addr_name`, `None` where
    Core's is the empty `addrNameIn` that falls back to the socket, so
    addr takes it where set and the formatted `getpeername` otherwise.
    `disconnectnode` matches its `address` against this same `addr`, as
    Core's `CConnman::DisconnectNode` matches `m_addr_name`
    (btclib-org/btclib-node#1301).
    """
    try:
        addr = p2p_conn.client.getpeername()
        addrbind = p2p_conn.client.getsockname()
    # A peer disconnecting mid-lookup is not worth logging a second
    # time; its own connection state already reports it. Deliberately
    # blind (BLE001): a disconnect racing this call can surface as more
    # than one socket error depending on timing and platform, and every
    # one of them means the same "skip this peer, ask the next".
    except Exception:  # noqa: BLE001
        return None
    return (
        p2p_conn.addr_name
        if p2p_conn.addr_name is not None
        else ip_and_port(addr[0], addr[1]),
        ip_and_port(addrbind[0], addrbind[1]),
    )


def get_connection_count(node: Node, conn: RpcConnection, _: list[Any]) -> int:
    """Answer `getconnectioncount`, a pending connection counted too.

    Core's own `getconnectioncount` counts every entry of `m_nodes`
    (`CConnman::GetNodeCount`), which holds a socket from the moment
    it is accepted or dialled -- before its handshake, not only after.
    """
    manager = node.p2p_manager
    return len(manager.connections) + len(manager.pending_connections)


def get_network_info(node: Node, conn: RpcConnection, _: list[Any]) -> dict[str, Any]:
    """Answer `getnetworkinfo` with the two fields `connect_nodes` reads.

    Core's own `getnetworkinfo` (`rpc/net.cpp:674-800`, at
    bitcoin/bitcoin@bb529657) answers two dozen fields, most either this
    node's own configuration it carries no counterpart to -- `-onlynet`,
    `-proxy`, `-asmap` -- or relay-loop state this node does not keep
    (`inv_buckets`, `tx_send_rate`). Serving a placeholder for any of
    those would be the same decoration `get_mempool_info`'s own
    docstring already argues against, so they are left out rather than
    answered with a made-up number.

    `subversion` is what `test_framework.py`'s own `connect_nodes`
    (`:568-594`, same sha) reads off each side before wiring them
    together -- `get_peer_info` above answers the matching `subver` a
    peer sees on the wire, both reading `constants.USER_AGENT`
    (btclib-org/btclib-node#1009) rather than each computing their own.
    `protocolversion` is `PROTOCOL_VERSION` (`btclib.p2p.limits`), the
    same constant every `version` this node sends and every
    `getheaders` it builds already carries.

    `localservices` and `localservicesnames` are `p2p.connection.local_services`
    (`rpc/net.cpp:706-708`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag) -- the services this node's own `version` advertises on every
    connection, one function answering both rather than each computing
    its own (btclib-org/btclib-node#1394), `service_names` above turning
    the same bits into the strings `getpeerinfo`'s own `servicesnames`
    already uses for a peer's.
    """
    services = local_services(node.config)
    return {
        "subversion": USER_AGENT,
        "protocolversion": PROTOCOL_VERSION,
        "localservices": f"{services:016x}",
        "localservicesnames": service_names(services),
    }


# Core's own three `addnode` commands (`rpc/net.cpp:341-415`, at
# bitcoin/bitcoin@bb529657): `add`/`remove` reach `P2pManager`'s own
# `add_added_peer`/`remove_added_peer`, its counterpart to `CConnman`'s
# `AddNode`/`RemoveAddedNode`, and `_open_added_peers`
# (`p2p/manager.py`) is what actually dials whatever the list holds,
# never this function (btclib-org/btclib-node#1350). `onetry` schedules
# the identical one-shot dial Core's `OpenNetworkConnection` does
# (`conn_type=MANUAL`, no persistence, no dedup) -- the one command
# `connect_nodes`, the one caller this node's own tf2 census names for
# this method (`test_framework.py:568-594`, same sha), ever calls.
_ADDNODE_COMMANDS = ("add", "remove", "onetry")


def _parsed_addnode_args(params: list[Any]) -> tuple[str, str]:
    """Return `addnode`'s own `(node, command)`, or raise as Core's parser does.

    Split out of `add_node` below so that function's own three-command
    dispatch stays under `ruff`'s complexity floor; the checks
    themselves are unchanged (`rpc/net.cpp:365-377`, at
    bitcoin/bitcoin@bb529657).
    """
    if len(params) < 2:  # noqa: PLR2004
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["addnode"])
    # every declared argument's type is checked, and every mismatch
    # named, before any of the three is raised on -- `disconnect_node`
    # above already does this for its own two arguments, `type_errors`'
    # own docstring argues the shape
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(params[0], str):
        mismatches.append((1, "node", params[0], "string"))
    if not isinstance(params[1], str):
        mismatches.append((2, "command", params[1], "string"))
    v2transport_mismatch = bool_mismatch(params, 2, name="v2transport")
    if v2transport_mismatch is not None:
        mismatches.append(v2transport_mismatch)
    if mismatches:
        raise type_errors(*mismatches)
    node_arg, command = params[0], params[1]
    if command not in _ADDNODE_COMMANDS:
        # Core's own `command`-validity refusal is the identical
        # `std::runtime_error(self.ToString())` shape as a wrong
        # argument count, `self.ToString()` being this same full help
        # text (measured against a real bitcoind v31.1.0: `addnode
        # "1.2.3.4" "bogus"` answers it byte for byte) rather than the
        # one-line usage string this used to raise
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["addnode"])
    bool_param(params, 2, name="v2transport", default=False)

    if not node_arg.strip():
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER, "Error: Node address cannot be empty"
        )
    return node_arg, command


def add_node(node: Node, conn: RpcConnection, params: list[Any]) -> None:
    """Answer `addnode`'s three commands for real, each against Core's own list.

    The module-level comment above argues the three commands; this
    function is Core's own argument parsing and its two literal error
    messages (`rpc/net.cpp:365-377`, at bitcoin/bitcoin@bb529657). The
    empty-`node` refusal is master's own fix
    (`rpc: reject empty node argument in addnode`,
    at bitcoin/bitcoin@90ce21e21d) rather than this tree's own pinned
    `bitcoind`'s: at bitcoin/bitcoin@9be056a8a7 -- v31.1, the release
    `integration-bitcoind.yml` pins, answers an empty `node` with a
    silent, do-nothing success instead, measured directly against a
    real v31.1.0 (issue #1010). `90ce21e21d` post-dates v31.1's own tag
    commit and is confirmed on `bb529657`'s own ancestry via
    `git merge-base --is-ancestor`.

    Matching master here rather than the release this tree tests
    against is a decision, not an oversight: `CLAUDE.md`'s own
    *Following Bitcoin Core* names matching Core's behaviour as the
    default, and reserves a release-pinned citation for a claim about
    the behaviour of the bitcoind this tree is tested against rather
    than for what this node implements. Master's own code comment names
    why the fix exists -- "Such a node would never resolve, but would
    be retried indefinitely" -- and nothing under `tests/integration/`
    drives `addnode ""`, so this tree's own integration suite, run
    against v31.1, never exercises the one call shape the two
    disagree on. Matching the release instead would mean knowingly
    carrying a defect Core itself already fixed, only to undo that the
    moment the pin advances past it -- issue #1010 is closed on this
    reasoning. `v2transport` is read and type-checked, matching Core's
    own optional third argument, and otherwise unused: BIP324 is not a
    transport this node speaks yet.
    """
    node_arg, command = _parsed_addnode_args(params)

    if command == "add":
        if not node.p2p_manager.add_added_peer(node_arg):
            raise RpcError(
                RPCErrorCode.CLIENT_NODE_ALREADY_ADDED, "Error: Node already added"
            )
        return

    if command == "remove":
        if not node.p2p_manager.remove_added_peer(node_arg):
            raise RpcError(
                RPCErrorCode.CLIENT_NODE_NOT_ADDED,
                "Error: Node could not be removed. It has not been added previously.",
            )
        return

    try:
        split_host_port(node_arg, node.chain.port)
    except ValueError as error:
        # a malformed port alone: a hostname is no longer refused here,
        # `connect_host` resolving one the way `P2pManager`'s own
        # redial and `Node.run`'s startup dial do (btclib-org/btclib-node#1264)
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, str(error)) from error

    # `node_arg` whole, not the `(host, port)` the check above only
    # validated with: Core's own `onetry` passes `node_arg` itself as
    # `pszDest` (`src/rpc/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    # v31.1 tag), so a port the caller gave reaches `addr_name` too
    # (btclib-org/btclib-node#1493).
    node.p2p_manager.connect_host(node_arg, node.chain.port)


# `UniValue::getInt<int64_t>`'s own range, past which it throws "JSON
# integer out of range"
_INT64_BOUND = 2**63


def disconnect_node(node: Node, conn: RpcConnection, params: list[Any]) -> None:
    """Answer `disconnectnode`: drop one connection, by address or by id.

    Core's own (`src/rpc/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag): `address` alone, or an empty or null `address` with
    `nodeid`, and anything else `RPC_INVALID_PARAMS`. A connection still
    short of `verack` is found too, as Core's `m_nodes` holds it. The
    address is matched against `getpeerinfo`'s own `addr`, the id against
    its `id`, and neither found is `RPC_CLIENT_NODE_NOT_CONNECTED`. Named
    arguments reach it mapped onto these two positions by `arg_names`.

    A call carrying more than two positional arguments never reaches
    this function's own body at all: `rpc.main._execute` refuses it
    generically now, for every method `arg_names` declares, rather than
    this function checking its own upper bound the way it used to be
    the only callback here to (`rpc.main._execute`'s own docstring
    argues the generalization, btclib-org/btclib-node#1424).
    """
    address = params[0] if params else None
    node_id = params[1] if len(params) > 1 else None
    # both arguments' types are checked before either is read, and every
    # mismatch is named in one refusal, as `HandleRequest` does
    mismatches: list[tuple[int, str, object, str]] = []
    if address is not None and not isinstance(address, str):
        mismatches.append((1, "address", address, "string"))
    if node_id is not None and (
        isinstance(node_id, bool) or not isinstance(node_id, int | float)
    ):
        mismatches.append((2, "nodeid", node_id, "number"))
    if mismatches:
        raise type_errors(*mismatches)
    if node_id is not None and (
        isinstance(node_id, float) or not -_INT64_BOUND <= node_id < _INT64_BOUND
    ):
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")

    manager = node.p2p_manager
    # Unlocked, the same snapshot `get_peer_info` above takes and for the
    # same reason (btclib-org/btclib-node#356): a connection
    # `create_connection` adds after this read is simply not in it, and
    # is answered `RPC_CLIENT_NODE_NOT_CONNECTED` below, which a retry
    # settles once a later snapshot holds it. One `remove_connection`
    # itself drops between this read and the call below is answered as
    # it still was here, and costs nothing there either:
    # `remove_connection`'s own `pop(..., None)` is already a no-op on
    # an id that is gone.
    peers = {**manager.pending_connections, **manager.connections}
    if address is not None and node_id is None:
        found = [
            connection_id
            for connection_id, p2p_conn in sorted(peers.items())
            if (addresses := _socket_addresses(p2p_conn)) is not None
            and addresses[0] == address
        ][:1]
    elif node_id is not None and not address:
        found = [node_id] if node_id in peers else []
    else:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMS,
            "Only one of address and nodeid should be provided.",
        )
    if not found:
        raise RpcError(
            RPCErrorCode.CLIENT_NODE_NOT_CONNECTED,
            "Node not found in connected nodes",
        )
    manager.remove_connection(found[0])


def _setban_params(params: list[Any]) -> tuple[str, str, int | float | None, bool]:
    """Check `setban`'s arguments as `HandleRequest` does, then `command`.

    The upper bound on `len(params)` is `rpc.main._execute`'s own now,
    checked generically against `arg_names["setban"]` before this
    function ever runs; only the lower bound -- `subnet` and `command`
    both required -- is this function's own to check. Every declared
    argument's type is checked, and every mismatch named, before any of
    them is raised on, the way `disconnect_node` above already does for
    its own two (`type_errors`' own docstring).
    """
    if len(params) < 2:  # noqa: PLR2004
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["setban"])
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(params[0], str):
        mismatches.append((1, "subnet", params[0], "string"))
    if not isinstance(params[1], str):
        mismatches.append((2, "command", params[1], "string"))
    bantime = params[2] if len(params) > 2 else None  # noqa: PLR2004
    if bantime is not None and (
        isinstance(bantime, bool) or not isinstance(bantime, (int, float))
    ):
        mismatches.append((3, "bantime", bantime, "number"))
    absolute_mismatch = bool_mismatch(params, 3, name="absolute")
    if absolute_mismatch is not None:
        mismatches.append(absolute_mismatch)
    if mismatches:
        raise type_errors(*mismatches)
    absolute = bool_param(params, 3, name="absolute", default=False)
    if params[1] not in ("add", "remove"):
        # Core's own `command`-validity refusal is the identical
        # `std::runtime_error(help.ToString())` shape as a wrong
        # argument count, `help.ToString()` being this same full help
        # text (measured against a real bitcoind v31.1.0: `setban
        # "1.2.3.4" "bogus"` answers it byte for byte)
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["setban"])
    return params[0], params[1], bantime, absolute


def _setban_subnet(subnet_arg: str) -> Subnet:
    """Parse `setban`'s `subnet`: a subnet with a slash, else a valid host."""
    subnet: Subnet | None
    if "/" in subnet_arg:
        subnet = lookup_subnet(subnet_arg)
    else:
        ip = lookup_host(subnet_arg)
        subnet = Subnet.of(ip) if ip is not None and is_valid_host(ip) else None
    if subnet is None:
        raise RpcError(
            RPCErrorCode.CLIENT_INVALID_IP_OR_SUBNET, "Error: Invalid IP/Subnet"
        )
    return subnet


def set_ban(node: Node, conn: RpcConnection, params: list[Any]) -> None:
    """Answer `setban`, Core's own checks in Core's own order.

    Core's `setban` (`src/rpc/net.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag). A `subnet` holding a slash is a subnet, and is
    otherwise one address, which has to be a valid one. Adding a ban
    drops every peer it matches.
    """
    subnet_arg, command, bantime, absolute = _setban_params(params)
    subnet = _setban_subnet(subnet_arg)
    ban_man = node.p2p_manager.ban_man
    if command == "remove":
        if not ban_man.unban(subnet):
            raise RpcError(
                RPCErrorCode.CLIENT_INVALID_IP_OR_SUBNET,
                "Error: Unban failed. Requested address/subnet was not"
                " previously manually banned.",
            )
        return
    # a single address is banned already where any ban covers it, a
    # subnet only where that very subnet is banned
    banned = (
        ban_man.is_subnet_banned(subnet)
        if "/" in subnet_arg
        else ban_man.is_banned(subnet.network)
    )
    if banned:
        raise RpcError(
            RPCErrorCode.CLIENT_NODE_ALREADY_ADDED, "Error: IP/Subnet already banned"
        )
    # UniValue's `getInt<int64_t>`, which only `add` calls
    if isinstance(bantime, float) or (
        bantime is not None and not -(1 << 63) <= bantime < 1 << 63
    ):
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    bantime = bantime or 0
    if absolute and bantime < int(time.time()):
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER, "Error: Absolute timestamp is in the past"
        )
    ban_man.ban(subnet, bantime, absolute=absolute)
    node.p2p_manager.disconnect_subnet(subnet)


def list_banned(node: Node, conn: RpcConnection, _: list[Any]) -> list[dict[str, Any]]:
    """Answer `listbanned`, every unexpired ban in the list's own order."""
    now = int(time.time())
    return [
        {
            "address": str(subnet),
            "ban_created": entry.create_time,
            "banned_until": entry.ban_until,
            "ban_duration": entry.ban_until - entry.create_time,
            "time_remaining": entry.ban_until - now,
        }
        for subnet, entry in node.p2p_manager.ban_man.banned()
    ]


def clear_banned(node: Node, conn: RpcConnection, _: list[Any]) -> None:
    """Answer `clearbanned`."""
    node.p2p_manager.ban_man.clear()


def _btc_amount(sats: int) -> RawJSON:
    """Format a non-negative satoshi amount as Core's own exact BTC string.

    Core's own `ValueFromAmount` (`src/core_io.cpp:283-293`,
    at bitcoin/bitcoin@58a7869f86): integer `amount / COIN` and
    `amount % COIN`, formatted `%d.%08d` -- exact at every magnitude,
    where a Python float division (`sats / 1e8`) serializes through
    `repr`, which fixes no decimal places and emits exponent notation
    (`1e-06`) at a magnitude ordinary for a feerate. Takes a
    non-negative amount only, and needs no sign correction Core's own
    version applies for a negative one: every caller here is a feerate,
    which is never negative, and Python's `//`/`%` already agree with
    C++'s truncating division for a non-negative dividend.
    """
    quotient, remainder = divmod(sats, 100_000_000)
    return RawJSON(f"{quotient}.{remainder:08d}")


def get_mempool_info(node: Node, conn: RpcConnection, _: list[Any]) -> dict[str, Any]:
    """Answer `getmempoolinfo` with the fields this tree backs for real.

    The comment below argues, field by field, why Core's own several
    others are left out rather than answered with a placeholder, and
    why `mempoolminfee` alone among them is BTC/kvB rather than this
    tree's own sat/kvB.
    """
    mempool = node.mempool
    # Core's own MempoolInfoToJSON (`src/rpc/mempool.cpp:1075-1086`,
    # at bitcoin/bitcoin@58a7869f86) answers several fields beyond these
    # five: `usage`, `total_fee`, `permitbaremultisig`,
    # `maxdatacarriersize`, `limitclustercount`, `limitclustersize`,
    # `optimal`, the deprecated `fullrbf`. Every one of those is backed
    # by a concept this tree does not carry -- a cluster mempool graph, a
    # persisted total fee, a bare-multisig policy knob -- and answering
    # any of them with a placeholder would be exactly the decoration
    # this method's own sparse answer already was. `minrelaytxfee` and
    # `incrementalrelayfee` are excluded for a different reason: both
    # are real and cheap to answer here too (`Config.min_relay_feerate`,
    # `mempool.py`'s own incremental-fee constant), left out only
    # because #305 named these two fields and not those. `maxmempool`
    # and `mempoolminfee` are wired in because #294 gave both a real
    # source to read, and `unbroadcastcount` because #1421 gave
    # `Mempool.unbroadcast` one. btclib-org/btclib-node#305
    #
    # `mempoolminfee` is BTC/kvB, matching Core's own
    # `ValueFromAmount`-converted unit rather than this tree's own
    # sat/kvB used everywhere else a feerate is emitted or read
    # (`Mempool.meets_fee_rate`, BIP133's own `feefilter` wire value,
    # `Config.min_relay_feerate`): a client written against Core's own
    # `getmempoolinfo` reads this field expecting BTC/kvB, and Core
    # defines the unit on this particular surface. BIP133's own wire
    # value is unaffected -- `_send_due_feefilters`
    # (`src/btclib_node/download.py`) still sends sat/kvB, because BIP133
    # says so, not because this tree chose a unit. `maxmempool` needs no
    # such divergence: Core's own field is `m_opts.max_size_bytes`,
    # plain bytes with no amount conversion applied to it either.
    mempoolminfee = max(
        mempool.get_min_fee_rate().sats_per_kvbyte,
        node.config.min_relay_feerate.sats_per_kvbyte,
    )
    return {
        "loaded": True,
        "size": mempool.size,
        "bytes": mempool.bytesize,
        "maxmempool": mempool.bytesize_limit,
        "mempoolminfee": _btc_amount(mempoolminfee),
        "unbroadcastcount": len(mempool.unbroadcast),
    }


# ParseHashType's own two names this tree can answer
# (src/rpc/blockchain.cpp:977-987, at bitcoin/bitcoin@ca7162cde5).
# "hash_serialized_3" is a third name Core itself accepts but this tree
# does not implement -- get_tx_out_set_info's own docstring is where
# that refusal, reusing ParseHashType's own error text for a value Core
# would otherwise accept, is argued.
_TX_OUT_SET_HASH_TYPES = {"muhash", "none"}

# Core's own default is "hash_serialized_3"
# (`RPCArg::Default{"hash_serialized_3"}`, `src/rpc/blockchain.cpp:1054`,
# at bitcoin/bitcoin@9be056a8a7); this tree's own default is
# `get_tx_out_set_info`'s own one hash type it can actually answer
# without a live scan -- that docstring is where the departure, and
# what would remove it, is argued.
_DEFAULT_TX_OUT_SET_HASH_TYPE = "muhash"


def get_tx_out_set_info(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `gettxoutsetinfo` from `UtxoIndex`'s own running `CoinStats`.

    Core's own default path recomputes every field from a live scan of
    the coins database (`ComputeUTXOStats`, `kernel/coinstats.cpp`) on
    every call, unless `-coinstatsindex` is running, in which case the
    incrementally-maintained `CoinStatsIndex` answers instead
    (`index/coinstatsindex.cpp`) -- `chainstate/muhash.py`'s own module
    docstring is where `CoinStats` is argued as this tree's equivalent
    of that second path, the only one it implements. `height`,
    `bestblock`, `txouts`, `bogosize`, `total_amount` and (for
    `hash_type: "muhash"`) `muhash` are Core's own field names and
    units, `total_amount` in BTC through `_btc_amount` the way
    `get_mempool_info`'s own `mempoolminfee` already is; `muhash` itself
    is the raw digest bytes reversed before this returns, matching
    `uint256::GetHex()`'s own convention rather than this class's
    `digest` (`chainstate/muhash.py`'s own comment beside
    `is_bip30_unspendable` is where that reversal is confirmed against
    the well-known genesis hash rather than assumed).

    `hash_type: "hash_serialized_3"` -- Core's own default, the legacy
    double-SHA256 scan -- is refused with `ParseHashType`'s own error
    text (`RPC_INVALID_PARAMETER`, `'%s' is not a valid hash_type`),
    reused here for a value Core itself accepts but this tree has no
    accumulator for: `ApplyHash`/`TxOutSer` (`kernel/coinstats.cpp`)
    fold every coin into one incremental hash in the coins-view
    cursor's own order, by txid as the store's own keys sort it and
    then by vout, and answering it here would mean a live, ordered
    walk of every `utxo-` record on every call -- `KeyValueStore`'s own
    `__iter__` (`db.py`) is the one full-store scan this tree carries,
    reads every column family whole into memory, in one array, and
    carries no prefix or streaming cursor a caller could narrow to
    `utxo-` alone. This node answers only from `CoinStats`, the
    incrementally-maintained accumulator `-coinstatsindex` puts in
    front of that same scan on a real `bitcoind`, and building the
    ordered scan `hash_serialized_3` needs beside it, on the tree's own
    ordered store rather than through that one whole-store `__iter__`,
    is its own issue rather than this one's.

    So where Core's own default answers, this one refuses -- the
    departure this docstring argues rather than leaves silent -- and
    where a caller asks for nothing at all, `_DEFAULT_TX_OUT_SET_HASH_TYPE`
    is what this tree answers instead: `"muhash"`, the one hash type
    `CoinStats` already keeps running, so a bare `gettxoutsetinfo`
    answers something rather than the error Core's own unreachable
    default would otherwise still produce here. `hash_type: "none"`
    answers every field but `muhash` itself, the way Core's own
    `CoinStatsHashType::NONE` does.

    `hash_or_height` is refused the way an ordinary `bitcoind`, run
    without `-coinstatsindex`, already refuses it -- `!g_coin_stats_index`
    (`src/rpc/blockchain.cpp:1091-1092`) is Core's own gate, and this
    tree has no such index either: `CoinStats` only ever holds the
    *current* best block's own commitment, nothing keyed by an earlier
    height. `use_index` is read and type-checked the way Core's own
    `RPCArg::Type::BOOL` argument is, but changes nothing here: there is
    no non-indexed path for it to switch this tree onto, `CoinStats`
    being the only one there is.

    `transactions` and `disk_size` are left out of every answer, the way
    Core's own indexed answer already leaves them out
    (`src/rpc/blockchain.cpp:1131-1134`, `if (!stats.index_used) {...}`):
    both are an O(n) count over the whole set, which an incrementally
    maintained accumulator exists specifically to avoid paying on every
    call. `total_unspendable_amount` and `block_info`, `CoinStatsIndex`'s
    own two fields this tree could in principle also answer, are left
    out for a different reason: they need bookkeeping (the subsidy
    schedule, the BIP30/genesis/unclaimed-reward split) this branch does
    not add, and issue #639's own "Not in scope" does not ask for them.
    """
    hash_type = (
        params[0] if params and params[0] is not None else _DEFAULT_TX_OUT_SET_HASH_TYPE
    )
    # hash_type and use_index are checked, and every mismatch named,
    # before either is raised on or any value-level check below runs,
    # the way `disconnect_node` above already does for its own two
    # declared arguments (`type_errors`' own docstring); hash_or_height
    # carries no single declared JSON type of its own to check here
    # (this method's own docstring, on `use_index` beside it, argues why)
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(hash_type, str):
        mismatches.append((1, "hash_type", hash_type, "string"))
    use_index_mismatch = bool_mismatch(params, 2, name="use_index")
    if use_index_mismatch is not None:
        mismatches.append(use_index_mismatch)
    if mismatches:
        raise type_errors(*mismatches)
    if hash_type not in _TX_OUT_SET_HASH_TYPES:
        err_msg = f"'{hash_type}' is not a valid hash_type"
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, err_msg)

    if len(params) > 1 and params[1] is not None:
        err_msg = "Querying specific block heights requires coinstatsindex"
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, err_msg)

    # type-checked above and otherwise unused -- this method's own
    # docstring argues why
    bool_param(params, 2, name="use_index", default=True)

    active_chain = node.chainstate.block_index.active_chain
    coin_stats = node.chainstate.utxo_index.coin_stats
    result: dict[str, Any] = {
        "height": len(active_chain) - 1,
        "bestblock": active_chain[-1],
        "txouts": coin_stats.transaction_output_count,
        "bogosize": coin_stats.bogo_size,
    }
    if hash_type == "muhash":
        result["muhash"] = coin_stats.digest[::-1]
    result["total_amount"] = _btc_amount(coin_stats.total_amount)
    return result


# Core's own literal sentinel, `MEMPOOL_HEIGHT` (`src/txmempool.h:50`, at
# bitcoin/bitcoin@9be056a8a7): a `Coin` built from a mempool transaction's
# own output rather than from the confirmed set carries this height
# instead of a real one, and `gettxout` reads it back to answer
# `confirmations: 0` (`rpc/blockchain.cpp:1243-1247`). Not
# `main.py`'s own `spend_height` -- that is `CalculatePrevHeights`'s
# *different* convention, "assume a mempool parent confirms in the next
# block" for a sequence-lock height, and reads this same sentinel back
# out rather than storing it.
_MEMPOOL_HEIGHT = 0x7FFF_FFFF

# GetTxnOutputType's own vocabulary (`src/script/solver.cpp:18-34`, at
# bitcoin/bitcoin@9be056a8a7), keyed on this library's own
# `type_and_payload` names (`btclib.script.script_pub_key`) -- the two
# agree in spelling for nothing, "nulldata" being the closest case and
# still its own key below rather than assumed. `_script_pub_key_dict`
# below is the one place this tree renders a `scriptPubKey`'s own
# `type` in Core's words rather than this library's; ISS 1440 is where
# `get_raw_transaction`'s own verbose form, which answers this library's
# names instead and at the wrong nesting level, is filed for the
# sibling mismatch this table does not reach.
_CORE_SCRIPT_TYPES: dict[str, str] = {
    "p2pk": "pubkey",
    "p2pkh": "pubkeyhash",
    "p2sh": "scripthash",
    "p2ms": "multisig",
    "nulldata": "nulldata",
    "p2wpkh": "witness_v0_keyhash",
    "p2wsh": "witness_v0_scripthash",
    "p2tr": "witness_v1_taproot",
    "witness_unknown": "witness_unknown",
    "unknown": "nonstandard",
}

# `CScript::IsPayToAnchor` (`src/script/script.cpp:207-213`, at
# bitcoin/bitcoin@9be056a8a7): the literal four-byte P2A script, OP_1
# followed by its own fixed two-byte push. `Solver` carves this one
# script out of what would otherwise be `TxoutType::WITNESS_UNKNOWN`
# (`src/script/solver.cpp:167-171`, same sha) into its own
# `TxoutType::ANCHOR`; this library's own `type_and_payload` answers
# "witness_unknown" for it same as any other non-p2tr version-1
# program, making no such cut of its own.
_ANCHOR_SCRIPT = bytes.fromhex("51024e73")


def _core_script_type(script_pub_key: ScriptPubKey) -> str:
    """Answer `GetTxnOutputType`'s own name for `script_pub_key`'s own type.

    `_CORE_SCRIPT_TYPES` carries every other name across one for one;
    `"anchor"` is the one Core name with no btclib type behind it, so
    it is matched here directly against the literal script byte for
    byte, ahead of the table.
    """
    if script_pub_key.script == _ANCHOR_SCRIPT:
        return "anchor"
    return _CORE_SCRIPT_TYPES[script_pub_key.type]


def _infer_descriptor(script_pub_key: ScriptPubKey) -> str:
    """Answer `gettxout`'s own `desc`, Core's `InferDescriptor` with no wallet.

    Core's `InferDescriptor` (`src/script/descriptor.cpp:3037`, calling
    `InferScript`, at bitcoin/bitcoin@9be056a8a7) is handed
    `DUMMY_SIGNING_PROVIDER` here -- this node keeps no wallet keys to
    hand it a real one either, so every branch of `InferScript` that
    consults the provider takes its "not found" path, and what is left
    is exactly what each standard script type answers with no provider
    at all:

    - p2pk and p2ms carry their own pubkeys in the script bytes, no
      provider needed (`InferPubkey`, same file, :2252-2265, called
      unconditionally once `Solver` names the type) -- `pk(...)` and
      `multi(...)`.
    - p2pkh, p2wpkh, p2sh and p2wsh each ask the provider for the
      pubkey or the redeem script behind the hash and get nothing back,
      so `InferScript` falls through every one of its own `if`s to the
      top-level `ExtractDestination` case at the bottom of the function
      -- `addr(...)`, `btclib_wallet.descriptors.from_address` already
      producing that exact string. A witness program past version 0 that
      is not p2tr
      -- this library's own "witness_unknown", the P2A anchor output
      among them -- reaches that identical fallback: Core's own
      `ExtractDestination` answers a destination for both
      `TxoutType::ANCHOR` and `TxoutType::WITNESS_UNKNOWN` the same way
      it does for the four named above (`src/addresstype.cpp:90-93`,
      same sha), and `ScriptPubKey.address` already answers one for
      every such program, so `addr(...)` is what a P2A output answers
      here too, matching a real `bitcoind` rather than diverging from
      it -- `_core_script_type` above is what still answers `"anchor"`
      for `scriptPubKey.type` on the same output, `Solver`'s own cut
      this library's `type_and_payload` does not make.
    - p2tr likewise finds no `TaprootSpendData` and falls to its own
      narrower fallback, two branches above the general one -- `rawtr(...)`,
      the bare x-only key.
    - nulldata and anything else `Solver` does not classify at all
      extract no destination and reach `RawDescriptor` -- `raw(...)`,
      the whole script; `ScriptPubKey.address`'s own docstring names
      p2pk, p2ms, nulldata and unknown as the four types it answers ""
      for.

    None of the five needs a key this node does not have; a checksum is
    added the way `Descriptor::ToString()`'s own default argument adds
    one (`btclib_wallet.descriptors.add_checksum`).
    """
    script = script_pub_key.script
    script_type, payload = type_and_payload(script)
    if script_type == "p2pk":
        return add_checksum(f"pk({payload.hex()})")
    if script_type == "p2ms":
        threshold, keys = p2ms_m_and_keys(script)
        key_list = ",".join(key.hex() for key in keys)
        return add_checksum(f"multi({threshold},{key_list})")
    if script_type == "p2tr":
        return add_checksum(f"rawtr({payload.hex()})")
    address = script_pub_key.address
    if address:
        return from_address(address)
    return add_checksum(f"raw({script.hex()})")


def _script_pub_key_dict(script_pub_key: ScriptPubKey) -> dict[str, Any]:
    """Answer `gettxout`'s own `scriptPubKey`, Core's nested shape.

    Core's `ScriptToUniv` (`src/core_io.cpp:411-428`, at
    bitcoin/bitcoin@9be056a8a7) nests `asm`, `desc`, `hex`, `type` and
    `address` inside `scriptPubKey` itself, unlike this tree's own
    `TxOut.to_dict`, which `get_raw_transaction`'s verbose form already
    answers with and which keeps `type`/`addresses`/`network` as that
    method's own siblings instead (ISS 1440, filed for that mismatch
    rather than carried into this new RPC). `address` is answered only
    where one exists, matching `ScriptToUniv`'s own `type !=
    TxoutType::PUBKEY` exclusion -- `ScriptPubKey.address` already
    answers `""` for p2pk without that check repeated here, `address`'s
    own docstring naming p2pk, p2ms, nulldata and unknown as the four
    types it has none for.
    """
    script_dict = script_to_dict(script_pub_key.script)
    out: dict[str, Any] = {
        "asm": script_dict["asm"],
        "desc": _infer_descriptor(script_pub_key),
        "hex": script_dict["hex"],
        "type": _core_script_type(script_pub_key),
    }
    address = script_pub_key.address
    if address:
        out["address"] = address
    return out


def _parse_get_tx_out_params(params: list[Any]) -> tuple[bytes, int, bool]:
    """Check `gettxout`'s own three arguments, Core's checks in Core's order.

    `txid` and `n` are both required (`RPCArg::Optional::NO`,
    `rpc/blockchain.cpp:1184-1188`, at bitcoin/bitcoin@9be056a8a7):
    `RPCMethod::HandleRequest` throws its own `HelpResult` for either
    missing, before either argument's own type is read (`src/rpc/util.cpp`),
    so a call short of `n` is refused the same generic way as one short
    of both. Every declared argument's JSON type is then checked before
    the handler body runs at all (`src/rpc/util.cpp:653-661`), so a
    wrongly typed `txid` and a wrongly typed `n` are reported together,
    `type_errors`'s own shape.
    """
    if len(params) < 2:  # noqa: PLR2004
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["gettxout"])
    txid_param, n_param = params[0], params[1]
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(txid_param, str):
        mismatches.append((1, "txid", txid_param, "string"))
    if isinstance(n_param, bool) or not isinstance(n_param, (int, float)):
        mismatches.append((2, "n", n_param, "number"))
    if mismatches:
        raise type_errors(*mismatches)
    txid = _parse_hash_v("txid", txid_param)
    # `UniValue::getInt<uint32_t>()` (`src/univalue/include/univalue.h
    # :142-153`): a non-integral number, or one outside uint32_t's own
    # range, is `RPC_MISC_ERROR` ("JSON integer out of range") -- the
    # same message `get_block_hash`'s own float check above answers,
    # and not `RPC_TYPE_ERROR`, the type check having already passed.
    if isinstance(n_param, float) or not 0 <= n_param < 2**32:
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    include_mempool = bool_param(params, 2, name="include_mempool", default=True)
    return txid, n_param, include_mempool


def _mempool_spends(node: Node, txid: bytes, out_point_bytes: bytes) -> bool:
    """Whether a mempool transaction spends this outpoint -- Core's `isSpent`.

    Core's `CTxMemPool::isSpent` (`src/txmempool.cpp:190-194`, at
    bitcoin/bitcoin@9be056a8a7) is a single lookup into `mapNextTx`,
    keyed on the whole outpoint. `Mempool.spent_by` (`mempool.py`) is
    keyed on the prevout's own txid alone -- more than one mempool
    transaction spending different outputs of one parent being the
    case its own docstring names -- so answering the identical question
    here costs a scan of however many candidates share that txid rather
    than Core's O(1) lookup, never more than the handful of mempool
    transactions actually spending that one parent's outputs.
    """
    for wtxid in node.mempool.spent_by.get(txid, ()):
        tx = node.mempool.transactions.get(wtxid)
        if tx is not None and any(
            vin.prev_out.serialize(check_validity=False) == out_point_bytes
            for vin in tx.vin
        ):
            return True
    return False


def get_tx_out(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any] | None:
    """Answer `gettxout`: one outpoint's own unspent coin, or null.

    Core's own `gettxout` (`rpc/blockchain.cpp:1182-1261`, at
    bitcoin/bitcoin@9be056a8a7). `include_mempool`, true by default,
    overlays this node's own mempool on the confirmed UTXO set the same
    two ways Core's `CCoinsViewMemPool` does: an outpoint a mempool
    transaction spends answers null before either view is read
    (`isSpent`, `_mempool_spends` above), and one a mempool transaction
    creates -- not yet in `UtxoIndex` at all -- answers that
    transaction's own output, at Core's own sentinel height
    `MEMPOOL_HEIGHT`, ahead of the confirmed set
    (`CCoinsViewMemPool::GetCoin`, `src/txmempool.cpp:790-811`) --
    `Mempool.get_tx` by txid is that same lookup, Core's own
    `mempool.get(outpoint.hash)`.

    `confirmations` is `0` for a coin at that sentinel height, matching
    Core's own `coin->nHeight == MEMPOOL_HEIGHT` check, and otherwise
    the active chain's own height past the coin's, `pindex->nHeight -
    coin->nHeight + 1` -- `active_chain`'s own length already being the
    tip's height plus one, the "+ 1" is folded into the subtraction the
    same way `get_block_header`'s own `confirmations` folds it in above.
    """
    txid, n, include_mempool = _parse_get_tx_out_params(params)
    out_point_bytes = OutPoint(txid, n, check_validity=False).serialize(
        check_validity=False
    )

    coin: Coin | None
    if not include_mempool:
        coin = node.chainstate.utxo_index.get_coin(out_point_bytes)
    elif _mempool_spends(node, txid, out_point_bytes):
        coin = None
    else:
        mempool_tx = node.mempool.get_tx(txid)
        if mempool_tx is None:
            coin = node.chainstate.utxo_index.get_coin(out_point_bytes)
        elif n < len(mempool_tx.vout):
            coin = Coin(
                mempool_tx.vout[n],
                _MEMPOOL_HEIGHT,
                is_coinbase=False,
                check_validity=False,
            )
        else:
            coin = None

    if coin is None:
        return None

    active_chain = node.chainstate.block_index.active_chain
    confirmations = (
        0 if coin.height == _MEMPOOL_HEIGHT else len(active_chain) - coin.height
    )
    return {
        "bestblock": active_chain[-1],
        "confirmations": confirmations,
        "value": _btc_amount(coin.tx_out.value),
        "scriptPubKey": _script_pub_key_dict(coin.tx_out.script_pub_key),
        "coinbase": coin.is_coinbase,
    }


def get_raw_mempool(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any] | list[str]:
    """Answer `getrawmempool`, Core's own three shapes by `params`.

    `verbose` alone answers one object per mempool transaction; neither
    flag answers a plain array of txids; `mempool_sequence` alone adds
    `node.mempool.sequence` beside that array. The two together are
    refused outright, matching `MempoolToJSON`'s own combination check.
    """
    # verbose and mempool_sequence, both RPCArg::Type::BOOL,
    # RPCArg::Default{false}: src/rpc/mempool.cpp:659-660, at
    # bitcoin/bitcoin@9be056a8a7, the v31.1 tag. Both are
    # checked, and every mismatch named, before either is raised on,
    # the way `disconnect_node` above already does for its own two
    # (`type_errors`' own docstring).
    mismatches: list[tuple[int, str, object, str]] = []
    verbose_mismatch = bool_mismatch(params, 0, name="verbose")
    if verbose_mismatch is not None:
        mismatches.append(verbose_mismatch)
    sequence_mismatch = bool_mismatch(params, 1, name="mempool_sequence")
    if sequence_mismatch is not None:
        mismatches.append(sequence_mismatch)
    if mismatches:
        raise type_errors(*mismatches)
    verbose = bool_param(params, 0, name="verbose", default=False)
    include_sequence = bool_param(params, 1, name="mempool_sequence", default=False)

    if verbose and include_sequence:
        # MempoolToJSON refuses the combination outright rather than
        # answering one and dropping the other: src/rpc/mempool.cpp
        # :608-611
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Verbose results cannot contain mempool sequence values.",
        )

    if verbose:
        return {
            tx.id.hex(): {
                "size": tx.size,
                # Core's `GetTxSize`, the sigop-adjusted one.
                # btclib-org/btclib-node#1357
                "vsize": node.mempool.vsizes[wtxid],
                "weight": tx.weight,
                "wtxid": tx.hash.hex(),
            }
            for wtxid, tx in node.mempool.transactions.items()
        }

    txids = [txid.hex() for txid in node.mempool.txid_index]
    if not include_sequence:
        # MempoolToJSON's plain-array answer, src/rpc/mempool.cpp:624-634
        return txids
    # MempoolToJSON's other shape, src/rpc/mempool.cpp:635-639
    return {"txids": txids, "mempool_sequence": node.mempool.sequence}


def get_mempool_entry(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `getmempoolentry`: one held transaction's own accounting.

    Core's own shape (`entryToJSON`, `src/rpc/mempool.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), less `chunkweight` and
    the `fees` object's own `chunk` -- `Mempool.entry`'s own docstring is
    where that is argued. A `txid` this mempool does not hold is Core's
    own `RPC_INVALID_ADDRESS_OR_KEY`, "Transaction not in mempool".
    btclib-org/btclib-node#1397
    """
    if not params:
        # the same mechanism get_block_header's own missing-argument case
        # answers with, above: RPCMethod::HandleRequest's HelpResult,
        # RPC_MISC_ERROR (src/rpc/server.cpp:887). `txid` is declared
        # RPCArg::Type::STR_HEX and Optional::NO, so it renders quoted
        # and outside any `( ... )` group --
        # read at bitcoin/bitcoin@b91d983f66, src/rpc/mempool.cpp:869-870
        raise RpcError(RPCErrorCode.MISC_ERROR, 'getmempoolentry "txid"')
    if not isinstance(params[0], str):
        raise type_error(1, "txid", params[0], "string")
    try:
        txid = bytes.fromhex(params[0])
    except ValueError as error:
        # ParseHashV, src/rpc/util.cpp:125, down to the sentence -- the
        # same simplification `get_block_header`'s own `blockhash` above
        # takes, an odd-length or non-hex string refused and a
        # wrong-length-but-valid-hex one not
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"txid must be hexadecimal string (not '{params[0]}')",
        ) from error
    mempool = node.mempool
    wtxid = mempool.txid_index.get(txid)
    if wtxid is None:
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Transaction not in mempool"
        )
    entry = mempool.entry(wtxid)
    return {
        "vsize": entry.vsize,
        "weight": entry.weight,
        "time": entry.time,
        "height": entry.height,
        "descendantcount": entry.descendant_count,
        "descendantsize": entry.descendant_size,
        "ancestorcount": entry.ancestor_count,
        "ancestorsize": entry.ancestor_size,
        "wtxid": entry.wtxid,
        "fees": {
            "base": _btc_amount(entry.fee),
            "modified": _btc_amount(entry.modified_fee),
            "ancestor": _btc_amount(entry.ancestor_fees),
            "descendant": _btc_amount(entry.descendant_fees),
        },
        "depends": entry.depends,
        "spentby": entry.spent_by,
        "bip125-replaceable": entry.bip125_replaceable,
        "unbroadcast": entry.unbroadcast,
    }


def _decode_txid(txid_arg: str) -> bytes:
    """Hex-decode `getrawtransaction`'s own `txid`, already type-checked.

    `ParseHashV(request.params[0], "parameter 1")` (`rpc/rawtransaction.cpp
    :304`, at bitcoin/bitcoin@9be056a8a7) -- Core's own name for this
    argument here, not "txid".
    """
    return _parse_hash_v("parameter 1", txid_arg)


def _decode_optional_block_hash(params: list[Any]) -> bytes | None:
    """Hex-decode `getrawtransaction`'s own `blockhash`, already type-checked.

    index 2 is this RPC's own third positional, "blockhash" in
    get_raw_transaction's own help string -- naming it would give a
    second name to what that string already names, tied to this one
    method's own argument list and not reusable past it
    """
    if len(params) <= 2 or params[2] is None:  # noqa: PLR2004
        return None
    # `ParseHashV(request.params[2], "parameter 3")` (`rpc/rawtransaction.cpp
    # :317`, at bitcoin/bitcoin@9be056a8a7) -- Core's own name here too.
    return _parse_hash_v("parameter 3", params[2])


def _find_transaction(
    node: Node, txid: bytes, block_hash: bytes | None
) -> tuple[Tx, int | None]:
    """Return the transaction `txid` names, and its block height if named."""
    if block_hash is None:
        tx = node.mempool.get_tx(txid)
        if tx is None:
            raise RpcError(
                RPCErrorCode.INVALID_ADDRESS_OR_KEY,
                "No such mempool transaction. This node keeps no "
                "transaction index; name the block it confirmed in to "
                "look there instead. Use gettransaction for wallet "
                "transactions.",
            )
        return tx, None

    try:
        block_info = node.chainstate.block_index.get_block_info(block_hash)
    except KeyError as error:
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Block hash not found"
        ) from error
    block = node.block_db.get_block(block_hash)
    if block is None:
        # Core's own `CheckBlockDataAvailability` (`rpc/blockchain.cpp`,
        # at bitcoin/bitcoin@ca7162cde5): "Block not available (pruned
        # data)" once `blockman.IsBlockPruned` says the store deleted it
        # rather than never having had it, "Block not available (not
        # fully downloaded)" otherwise -- `block_info.index` against
        # `block_db.pruned_up_to` is this store's own version of that
        # same distinction, `BlockDB.prune_up_to`'s own docstring is
        # where deleting by height rather than by file is argued.
        if block_info.index <= node.block_db.pruned_up_to:
            raise RpcError(RPCErrorCode.MISC_ERROR, "Block not available (pruned data)")
        raise RpcError(
            RPCErrorCode.MISC_ERROR, "Block not available (not fully downloaded)"
        )
    tx = next((t for t in block.transactions if t.id == txid), None)
    if tx is None:
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY,
            "No such transaction found in the provided block. Use "
            "gettransaction for wallet transactions.",
        )
    return tx, block_info.index


def get_raw_transaction(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any] | str:
    """`getrawtransaction`, for a mempool transaction or a named block's.

    No `-txindex` equivalent: this node keeps no lookup from every
    txid it has ever confirmed to the block that holds it, so a
    transaction is answered for exactly the two cases Core itself
    falls back to without one -- the mempool by itself
    (`src/rpc/rawtransaction.cpp:313-314`, `!g_txindex`), and a block
    named explicitly, searched rather than indexed. Both are read-only
    lookups against `block_index` and `block_db`, which already hold
    every validated block for reasons of their own; this adds no store.

    `btclib`'s own `BitcoinCoreFetcher.get_tx` calls this with a txid
    alone, verbosity 0 being its `_call`'s implicit default -- the
    shape it always gets, unconditionally, below.
    """
    if not params:
        # the same shape as getblockheader's own missing-argument case:
        # RPCMethod::HandleRequest's HelpResult, RPC_MISC_ERROR
        # (src/rpc/server.cpp:887). Core's own first name for this
        # argument is "verbosity" (declared "verbosity|verbose",
        # src/rpc/rawtransaction.cpp:247); this node keeps its own
        # "verbose" instead, because `verbose` below reads only the
        # boolean shape Core's `RPCArg::Default{0}` degrades to under
        # `allow_bool=true`, not the full 0/1/2 verbosity Core's name
        # is for -- read at bitcoin/bitcoin@b91d983f66
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["getrawtransaction"])
    # txid, verbose and blockhash are checked, and every mismatch
    # named, before any of them is raised on or any value-level check
    # below runs (the genesis exception, the two hex decodes), the way
    # `disconnect_node` above already does for its own two declared
    # arguments (`type_errors`' own docstring). txid and blockhash are
    # each declared RPCArg::Type::STR_HEX, type-checked before the
    # handler body runs
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(params[0], str):
        mismatches.append((1, "txid", params[0], "string"))
    verbose_mismatch = bool_mismatch(params, 1, name="verbose")
    if verbose_mismatch is not None:
        mismatches.append(verbose_mismatch)
    if len(params) > 2 and params[2] is not None and not isinstance(params[2], str):  # noqa: PLR2004
        mismatches.append((3, "blockhash", params[2], "string"))
    if mismatches:
        raise type_errors(*mismatches)

    txid = _decode_txid(params[0])
    # Core's own exception, ahead of every other argument
    # (`src/rpc/rawtransaction.cpp:290-293`, at bitcoin/bitcoin@9be056a8a7,
    # the v31.1 tag), compared there against the genesis merkle root:
    # the genesis block's one transaction, whose txid that root is.
    if txid == node.chain.genesis_block.transactions[0].id:
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY,
            "The genesis block coinbase is not considered an ordinary "
            "transaction and cannot be retrieved",
        )
    # Core declares this argument NUM with allow_bool=true
    # (src/rpc/rawtransaction.cpp:286); this node answers only the
    # default and the boolean shape every other verbose flag here
    # already takes, and not Core's 2 -- fee and prevout data come from
    # undo data this node does not keep alongside a block
    verbose = bool_param(params, 1, name="verbose", default=False)
    block_hash = _decode_optional_block_hash(params)
    tx, block_height = _find_transaction(node, txid, block_hash)

    if not verbose:
        return tx.serialize(include_witness=True).hex()

    # to_dict()'s own keys are Core's decoderawtransaction ones (its own
    # docstring), str | int | list -- narrower than this callback's
    # declared return, so the three below need the wider type spelled
    # out, the same as get_block_header's out: dict[str, Any] above it
    out: dict[str, Any] = tx.to_dict()
    out["hex"] = tx.serialize(include_witness=True).hex()
    if block_hash is not None and block_height is not None:
        active_chain = node.chainstate.block_index.active_chain
        on_active_chain = (
            block_height < len(active_chain)
            and active_chain[block_height] == block_hash
        )
        out["in_active_chain"] = on_active_chain
        out["blockhash"] = block_hash.hex()
        out["confirmations"] = (
            len(active_chain) - block_height if on_active_chain else -1
        )
    return out


# Core's own reason for a missing input, the one `PreChecks` gives
# (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag),
# which `sendrawtransaction` answers and `testmempoolaccept` replaces
# with "missing-inputs". btclib-org/btclib-node#1328
_MISSING_INPUTS_REASON = "bad-txns-inputs-missingorspent"
# Core's own reject reason for the same refusal, `TxValidationResult::
# TX_RECONSIDERABLE`/`TX_MEMPOOL_POLICY` invalidated with "mempool
# full" (`validation.cpp`, at bitcoin/bitcoin@58a7869f86) once
# `LimitMempoolSize` has run and the transaction just submitted is not
# among what it kept -- `HandleATMPError` (`node/transaction.cpp`, same
# commit) turns that into `TransactionError::MEMPOOL_REJECTED`, and
# `RPCErrorFromTransactionError` (`rpc/util.cpp`) answers it with
# `RPC_TRANSACTION_REJECTED`, which `rpc/protocol.h` declares as a bare
# alias of `RPC_VERIFY_REJECTED` (`-26`) -- the same code
# `RPCErrorCode.VERIFY_REJECTED` (`bitcoin_core_rpc`) already answers a
# transaction the mempool refused with, above. btclib-org/btclib-node#293
_MEMPOOL_FULL_REASON = "mempool full"
# Core's own `MAX_PACKAGE_COUNT` (`src/policy/packages.h`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): how many `rawtx` one
# `testmempoolaccept` takes. btclib-org/btclib-node#1329
_MAX_PACKAGE_COUNT = 25

# Core's own `COIN` and `MAX_MONEY` (`src/consensus/amount.h`, same
# tag): `MoneyRange`'s own bound, what `AmountFromValue` refuses an
# out-of-range `maxfeerate` or `maxburnamount` against.
_COIN = 100_000_000
_MAX_MONEY = 21_000_000 * _COIN
# Core's own `DEFAULT_MAX_RAW_TX_FEE_RATE` (`src/node/transaction.h`,
# same tag): `sendrawtransaction` and `testmempoolaccept`'s own default
# `maxfeerate`, 0.1 BTC/kvB.
_DEFAULT_MAX_RAW_TX_FEE_RATE = _COIN // 10
# Core's own `DEFAULT_MAX_BURN_AMOUNT` (same file): `sendrawtransaction`'s
# own default `maxburnamount`, zero.
_DEFAULT_MAX_BURN_AMOUNT = 0
# Core's own error, `AmountFromValue`'s (`src/rpc/util.cpp`, same tag)
# ahead of `ParseFeeRate`'s own bound, both of `test_mempool_accept` and
# `send_raw_transaction` below reading it through `_amount_param`.
_AMOUNT_NOT_NUMBER_OR_STRING = "Amount is not a number or string"


def _amount_param(params: list[Any], position: int, *, name: str, default: int) -> int:
    """Read an `RPCArg::Type::AMOUNT` argument, Core's own `AmountFromValue`.

    `src/rpc/util.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag:
    `RPCArg::Type::AMOUNT` is exempt from `RPCMethod::HandleRequest`'s
    own pre-check (`ExpectedType` answers `std::nullopt` for it, "VNUM
    or VSTR, checked inside AmountFromValue()"), so a JSON number or
    string is read here as a decimal BTC amount, exact to eight
    decimals, and refused as `RPC_TYPE_ERROR`: "Amount is not a number
    or string" for a JSON value of neither type -- `bool` included,
    `bool` being `int`'s own subclass in Python and no JSON bool ever
    being a number to Core's own `UniValue` -- "Invalid amount" for one
    that does not parse as a decimal, is not finite, or is not a whole
    number of satoshi, and "Amount out of range" for one that parses but
    falls outside `MoneyRange`, 0 through `MAX_MONEY`.

    `btclib.amount.valid_btc_amount` parses and range-checks the same
    grammar -- `Decimal`, finite, at most eight decimals, 0 through the
    21 million cap -- but folds Core's own two distinct messages above
    into the one `BTClibValueError` it always raises, which is why this
    reads the value with `Decimal` directly instead. `ParseFixedPoint`'s
    own digit-by-digit overflow guard (`src/util/strencodings.cpp`, same
    tag) is not replayed either: `Decimal.as_integer_ratio` is exact
    with no fixed width to overflow, the way `btclib.fee.FeeRate`'s own
    `from_sats_per_vbyte` already reads a decimal quote, and the
    `MoneyRange` check below is what `AmountFromValue` bounds its own
    result against either way -- the same value refused, whichever the
    parser.
    """
    if len(params) <= position or params[position] is None:
        return default
    value = params[position]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RpcError(RPCErrorCode.TYPE_ERROR, _AMOUNT_NOT_NUMBER_OR_STRING)
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation:
        raise RpcError(RPCErrorCode.TYPE_ERROR, "Invalid amount") from None
    if not decimal_value.is_finite():
        raise RpcError(RPCErrorCode.TYPE_ERROR, "Invalid amount")
    numerator, denominator = decimal_value.as_integer_ratio()
    amount, remainder = divmod(numerator * _COIN, denominator)
    if remainder:
        raise RpcError(RPCErrorCode.TYPE_ERROR, "Invalid amount")
    if not 0 <= amount <= _MAX_MONEY:
        raise RpcError(RPCErrorCode.TYPE_ERROR, "Amount out of range")
    return amount


def _parse_max_fee_rate(params: list[Any], position: int) -> int:
    """Read `maxfeerate`, Core's own `ParseFeeRate` over `_amount_param`.

    `src/rpc/util.cpp`, same tag: a rate of 1 BTC/kvB or more is
    refused, `RPC_INVALID_PARAMETER`, in Core's own words -- "Set to 0
    to accept any fee rate" is `_exceeds_max_fee` below's own reading of
    a zero rate as no cap, not a refusal here.
    """
    rate = _amount_param(
        params, position, name="maxfeerate", default=_DEFAULT_MAX_RAW_TX_FEE_RATE
    )
    if rate >= _COIN:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Fee rates larger than or equal to 1BTC/kvB are not accepted",
        )
    return rate


def _exceeds_max_fee(vsize: int, fee: int, max_raw_tx_fee_rate: int) -> bool:
    """Whether `fee` for `vsize` exceeds `max_raw_tx_fee_rate`, Core's check.

    Core's own `max_raw_tx_fee = max_raw_tx_fee_rate.GetFee(virtual_size)`
    then `if (max_raw_tx_fee && fee > max_raw_tx_fee)`
    (`src/rpc/mempool.cpp`/`src/node/transaction.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a `max_raw_tx_fee_rate`
    of zero makes `max_raw_tx_fee` zero too, and the `&&` reads that as
    no cap rather than a cap of zero satoshi. Unlike Core's own
    `sendrawtransaction`, which estimates `virtual_size` from the
    transaction's own weight alone (`GetVirtualTransactionSize(*tx)`,
    sigops uncounted) before it knows whether the transaction even
    verifies, and only later compares the cap this estimate derives
    against the real fee `ProcessTransaction` computes -- this tree
    verifies once, not test-accept-then-submit, so both callers below
    pass the same sigop-adjusted `vsize`
    `main.verify_mempool_acceptance` already computed and the mempool
    itself prices by, rather than a second, coarser estimate that exists
    in Core only because its own two-call shape has no other vsize to
    reach for yet.
    """
    max_raw_tx_fee = fee_from_vsize(vsize, FeeRate(sats_per_kvbyte=max_raw_tx_fee_rate))
    return bool(max_raw_tx_fee) and fee > max_raw_tx_fee


def _exceeds_max_burn(tx: Tx, max_burn_amount: int) -> bool:
    """Whether an unspendable output of `tx` exceeds `max_burn_amount`.

    Core's own check (`sendrawtransaction`'s handler, `src/rpc/mempool.cpp`,
    same tag): `out.scriptPubKey.IsUnspendable() ||
    !out.scriptPubKey.HasValidOps()`, `out.nValue > max_burn_amount`,
    refused as soon as one output matches. `!HasValidOps()` -- a push
    past the script's own end, or an opcode no table names -- is not
    replayed here: `btclib`'s own `Script.assert_valid` deliberately
    answers no such question (its own docstring, "there is no other
    question to ask: Bitcoin Core has no validity notion for a script
    either"), and `is_unspendable` is the one script-level predicate this
    tree's own dependency already carries the way `IsUnspendable()` is
    Core's -- an output whose script is grammatically malformed but not
    `is_unspendable` is a narrower case this check leaves to whatever
    this node already does with such a script elsewhere, rather than
    adding the opcode-validity scan `btclib` does not expose.
    """
    return any(
        is_unspendable(tx_out.script_pub_key.script) and tx_out.value > max_burn_amount
        for tx_out in tx.vout
    )


def test_mempool_accept(
    node: Node, conn: RpcConnection, params: list[Any]
) -> list[dict[str, Any]]:
    """Answer `testmempoolaccept`, one verdict per raw tx in `params[0]`.

    Runs `verify_mempool_acceptance` without calling `Mempool.add_tx`,
    so a transaction it verifies is reported allowed without being
    added -- the same reject reasons `send_raw_transaction` raises are
    reported here per entry instead, neither ending the whole batch.
    A fault that is neither of those two propagates and does end it,
    matching Core's own `testmempoolaccept`, which has no per-tx
    catch-all either (btclib-org/btclib-node#668).
    """
    if not params:
        # the same mechanism get_block_hash's own missing-argument case
        # answers with: RPCMethod::HandleRequest throws HelpResult for a
        # call short of a required argument, and ExecuteCommand's
        # `catch (const std::exception& e)` turns that into Core's own
        # JSONRPCError call, cited below for the shape rather than left
        # commented out -- ERA001 reads it as Python and is wrong.
        # JSONRPCError(RPC_MISC_ERROR, e.what()), src/rpc/server.cpp  # noqa: ERA001
        # :887, carrying the method's own full help text
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["testmempoolaccept"])
    rawtxs = params[0]
    if not isinstance(rawtxs, list):
        # rawtxs is declared RPCArg::Type::ARR, type-checked before the
        # handler body runs, the same as blockhash and txid elsewhere in
        # this file
        raise type_error(1, "rawtxs", rawtxs, "array")
    # Core's own handler (`src/rpc/mempool.cpp`, at bitcoin/bitcoin@9be056a8a7,
    # the v31.1 tag) bounds the array by `MAX_PACKAGE_COUNT` and then reads
    # every `rawtx` in order, through `UniValue::get_str` and `DecodeHexTx`,
    # before it validates any: the first element of the wrong type or that
    # does not decode ends the whole call. btclib-org/btclib-node#1253,
    # btclib-org/btclib-node#1329
    if not 1 <= len(rawtxs) <= _MAX_PACKAGE_COUNT:
        err_msg = f"Array must contain between 1 and {_MAX_PACKAGE_COUNT} transactions."
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, err_msg)
    # Core's own order: `maxfeerate` is parsed here, ahead of the decode
    # loop below, the same as `ParseFeeRate`'s own call sits ahead of
    # Core's own loop (`src/rpc/mempool.cpp`, same tag).
    # btclib-org/btclib-node#1371
    max_raw_tx_fee_rate = _parse_max_fee_rate(params, 1)
    txs: list[Tx] = []
    for rawtx in rawtxs:
        if not isinstance(rawtx, str):
            # the accessor's own message, unwrapped: an array's elements
            # are not among what the argument type check reads
            message = (
                f"JSON value of type {json_type_name(rawtx)} is not of expected "
                "type string"
            )
            raise RpcError(RPCErrorCode.TYPE_ERROR, message)
        try:
            txs.append(Tx.parse(rawtx))
        except BTClibException as error:
            # `BTClibException`, `send_raw_transaction`'s own clause below:
            # a script shorter than its declared length raises
            # `BTClibRuntimeError`, not `BTClibValueError`
            err_msg = (
                f"TX decode failed: {rawtx} Make sure the tx has at least one input."
            )
            raise RpcError(RPCErrorCode.DESERIALIZATION_ERROR, err_msg) from error
    return [_mempool_accept_verdict(node, tx, max_raw_tx_fee_rate) for tx in txs]


def _mempool_accept_verdict(
    node: Node, tx: Tx, max_raw_tx_fee_rate: int
) -> dict[str, Any]:
    """Return `test_mempool_accept`'s own per-tx verdict for `tx`.

    Only these two, matching Core's own shape: testmempoolaccept's
    per-tx loop (src/rpc/mempool.cpp:379-430, at
    bitcoin/bitcoin@ca7162cde5) never catches anything itself -- it
    only ever branches on the TxValidationResult ProcessTransaction
    always returns rather than raises, so a genuine C++ exception
    escaping that loop is not one tx's own verdict, it propagates out
    of the RPC call entirely, to ExecuteCommand's own catch
    (src/rpc/server.cpp:874-887, same commit), which is this tree's
    handle_rpc (rpc/main.py) -- already logging and answering
    INTERNAL_ERROR for exactly this, the same uniform catch
    send_raw_transaction below already relies on for anything past its
    own two excepts (btclib-org/btclib-node#668).

    `max_raw_tx_fee_rate`'s own refusal is not a third exception: Core's
    own fee-cap check runs after its candidate already verified
    (`src/rpc/mempool.cpp`, same tag), so `_exceeds_max_fee` below reads
    `verify_mempool_acceptance`'s own successful answer rather than
    catching anything. btclib-org/btclib-node#1371
    """
    tx_res: dict[str, Any] = {
        "txid": tx.id,
        "wtxid": tx.hash,
        "allowed": False,
    }
    try:
        # `vsize` for an accepted one alone, as Core answers it: the
        # sigop-adjusted size, known once the prevouts are read.
        # btclib-org/btclib-node#1357
        fee, vsize = verify_mempool_acceptance(node, tx)
        if _exceeds_max_fee(vsize, fee, max_raw_tx_fee_rate):
            # Core's own reject-reason for this one alone, with no
            # reject-details (`src/rpc/mempool.cpp`, same tag): the
            # candidate itself verified, so there is no `TxRejectedError`
            # to read one from. btclib-org/btclib-node#1371
            tx_res["reject-reason"] = "max-fee-exceeded"
        else:
            tx_res["vsize"] = vsize
            tx_res["allowed"] = True
    except TxRejectedError as exc:
        # Core's own pair for every reason but `missing-inputs`
        # (`src/rpc/mempool.cpp`, at bitcoin/bitcoin@9be056a8a7, the
        # v31.1 tag). btclib-org/btclib-node#1245, btclib-org/btclib-node#1328
        tx_res["reject-reason"] = exc.reason
        tx_res["reject-details"] = str(exc)
    except MissingPrevoutError:
        # and that one alone, with no details. btclib-org/btclib-node#1328
        tx_res["reject-reason"] = "missing-inputs"
    return tx_res


def _already_confirmed(node: Node, tx: Tx) -> bool:
    """Whether an unspent output of `tx`'s own is already in the UTXO set.

    Core's own `BroadcastTransaction` (`node/transaction.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) walks `tx->vout` against
    the coins tip before anything else: "If the transaction is already
    confirmed in the chain, don't do anything and return early." An
    output the active chain spent again since is gone from the UTXO set
    the same as one this transaction never had, so only an *unspent* one
    of this transaction's own outputs says it already confirmed.
    """
    utxo_index = node.chainstate.utxo_index
    return any(
        utxo_index.get_coin(
            OutPoint(tx.id, vout, check_validity=False).serialize(check_validity=False)
        )
        is not None
        for vout in range(len(tx.vout))
    )


# Core's own `TransactionErrorString(TransactionError::ALREADY_IN_UTXO_SET)`
# (`src/common/messages.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
# tag): `RPCErrorFromTransactionError` maps that error to
# `RPC_VERIFY_ALREADY_IN_UTXO_SET` (-27),
# `RPCErrorCode.VERIFY_ALREADY_IN_UTXO_SET` here.
# btclib-org/btclib-node#1373
_ALREADY_IN_UTXO_SET_REASON = "Transaction outputs already in utxo set"
# Core's own `TransactionErrorString(TransactionError::MAX_FEE_EXCEEDED)`,
# same file: `RPCErrorFromTransactionError`'s own `default` case answers
# it, like every `TransactionError` but the two named there,
# `RPC_TRANSACTION_ERROR` -- a bare alias of `RPC_VERIFY_ERROR` (-25,
# `src/rpc/protocol.h`). btclib-org/btclib-node#1371
_MAX_FEE_EXCEEDED_REASON = (
    "Fee exceeds maximum configured by user (e.g. -maxtxfee, maxfeerate)"
)
# Core's own `TransactionErrorString(TransactionError::MAX_BURN_EXCEEDED)`,
# same file and same `RPC_TRANSACTION_ERROR` mapping.
# btclib-org/btclib-node#1371
_MAX_BURN_EXCEEDED_REASON = (
    "Unspendable output exceeds maximum configured by user (maxburnamount)"
)


def _decode_and_precheck_raw_tx(node: Node, params: list[Any]) -> tuple[Tx, int]:
    """Decode `sendrawtransaction`'s `hexstring`, and its two early refusals.

    Everything `send_raw_transaction` below does ahead of the
    held-in-mempool check: the no-argument usage string, `hexstring`'s
    own type check, decoding it, and the two refusals Core's own
    `BroadcastTransaction` and its handler make on the decoded
    transaction alone -- `MAX_BURN_EXCEEDED` and the already-confirmed
    early return -- ahead of anything that reads the mempool.
    Returns the transaction and `maxfeerate`, Core's own `max_raw_tx_fee_rate`,
    read but not yet acted on: `_exceeds_max_fee` below is not one of
    Core's own two decoded-transaction-only checks, needing the mempool's
    own verified fee and vsize instead.
    """
    if not params:
        # the same mechanism get_block_hash's own missing-argument case
        # answers with: RPCMethod::HandleRequest throws HelpResult for a
        # call short of a required argument, and ExecuteCommand's
        # `catch (const std::exception& e)` turns that into Core's own
        # JSONRPCError call, cited below for the shape rather than left
        # commented out -- ERA001 reads it as Python and is wrong.
        # JSONRPCError(RPC_MISC_ERROR, e.what()), src/rpc/server.cpp  # noqa: ERA001
        # :887, carrying the method's own full help text
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["sendrawtransaction"])
    rawtx = params[0]
    if not isinstance(rawtx, str):
        # hexstring is declared RPCArg::Type::STR_HEX
        # (src/rpc/mempool.cpp:72), type-checked before the handler
        # body runs, the same as blockhash and txid above
        raise type_error(1, "hexstring", rawtx, "string")
    # Core's own order: `maxburnamount` is parsed here, ahead of the
    # decode below, the same as its own read of `request.params[2]` sits
    # ahead of `DecodeHexTx` (`src/rpc/mempool.cpp`, same tag).
    # btclib-org/btclib-node#1371
    max_burn_amount = _amount_param(
        params, 2, name="maxburnamount", default=_DEFAULT_MAX_BURN_AMOUNT
    )
    try:
        tx = Tx.parse(rawtx)
    except BTClibException as error:
        # Core's own RPC_DESERIALIZATION_ERROR, src/rpc/mempool.cpp: a
        # rawtx that never was a transaction, not one the mempool below
        # looked at and refused. Tx.parse raises BTClibValueError for a
        # string it cannot even decode and BTClibRuntimeError for one
        # too short for what it declares -- `BTClibException`, neither
        # itself raised, is the base both share and the one clause this
        # catches them with
        raise RpcError(
            RPCErrorCode.DESERIALIZATION_ERROR,
            "TX decode failed. Make sure the tx has at least one input.",
        ) from error
    if _exceeds_max_burn(tx, max_burn_amount):
        # Core's own `MAX_BURN_EXCEEDED`, checked on the decoded
        # transaction ahead of everything below it, `maxfeerate`
        # included (`src/rpc/mempool.cpp`, same tag).
        # btclib-org/btclib-node#1371
        raise RpcError(RPCErrorCode.VERIFY_ERROR, _MAX_BURN_EXCEEDED_REASON)
    max_raw_tx_fee_rate = _parse_max_fee_rate(params, 1)
    if _already_confirmed(node, tx):
        # Core's own early return, ahead of the held-in-mempool check
        # below and of verification itself (`_already_confirmed`'s own
        # docstring). btclib-org/btclib-node#1373
        raise RpcError(
            RPCErrorCode.VERIFY_ALREADY_IN_UTXO_SET, _ALREADY_IN_UTXO_SET_REASON
        )
    return tx, max_raw_tx_fee_rate


def send_raw_transaction(node: Node, conn: RpcConnection, params: list[Any]) -> str:
    """Answer `sendrawtransaction`: verify, add to the mempool, announce.

    A transaction that fails to decode, that `verify_mempool_acceptance`
    refuses, or that `Mempool.add_tx` evicts right back out under its
    own size limit is each refused with the reject reason and code
    cited beside its own raise, below; one kept is broadcast to peers
    and its txid answered, and so is one whose txid is already held,
    without being verified again. One whose own outputs are already in
    the UTXO set, or whose burned or feerate cost exceeds `maxburnamount`
    or `maxfeerate`, is refused before either of those two, the order
    Core's own `BroadcastTransaction` checks them in --
    `_decode_and_precheck_raw_tx` above is that first half.
    """
    tx, max_raw_tx_fee_rate = _decode_and_precheck_raw_tx(node, params)
    held = node.mempool.get_tx(tx.id)
    if held is not None:
        # This txid is already held, possibly under a different witness
        # -- and therefore a different wtxid -- than what was just
        # resubmitted. Core's `BroadcastTransaction` (`node/transaction.cpp`,
        # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) does not submit it
        # to the mempool again, where it would now be judged afresh -- a
        # feerate floor risen since, or a transaction a reorg re-added under
        # no floor at all (btclib-org/btclib-node#1245) -- and reannounces
        # the mempool's own copy: "Use the mempool's wtxid for
        # reannouncement". Announcing the resubmitted object's own wtxid
        # would queue a wtxid nothing holds: `Mempool.add_tx`'s own comment
        # on #277 is that defect, one call site over.
        # btclib-org/btclib-node#293
        node.p2p_manager.broadcast_raw_transaction(held, node.mempool.fees[held.hash])
        return tx.id.hex()
    try:
        fee, vsize = verify_mempool_acceptance(node, tx)
    except MissingPrevoutError as exc:
        # Core's own missing-inputs code, RPC_VERIFY_ERROR
        # (src/rpc/protocol.h): a transaction this node cannot verify
        # for want of what it spends, not one it refuses
        raise RpcError(RPCErrorCode.VERIFY_ERROR, _MISSING_INPUTS_REASON) from exc
    except TxRejectedError as exc:
        # Core's own RPC_VERIFY_REJECTED, with Core's own reason and
        # details as the message, `state.ToString()`: every refusal
        # `verify_mempool_acceptance` makes but a missing input.
        # btclib-org/btclib-node#1245, btclib-org/btclib-node#1328
        raise RpcError(RPCErrorCode.VERIFY_REJECTED, str(exc)) from exc
    if _exceeds_max_fee(vsize, fee, max_raw_tx_fee_rate):
        # Core's own `MAX_FEE_EXCEEDED`, `_exceeds_max_fee`'s own
        # docstring is where reading it against the real, verified
        # `vsize` and `fee` rather than a pre-verification estimate is
        # argued. btclib-org/btclib-node#1371
        raise RpcError(RPCErrorCode.VERIFY_ERROR, _MAX_FEE_EXCEEDED_REASON)
    # `Mempool.add_tx` now evicts to make room rather than refusing
    # outright past its old `is_full()` gate (btclib-org/btclib-node#294),
    # so whether this call is answered with the refusal below is no
    # longer knowable before making it: a transaction that clears
    # whatever eviction would otherwise remove is kept even into a
    # mempool already at its limit, and one that does not is evicted
    # right back out, `add_tx` answering `False` either way a caller
    # of this method could tell apart before calling it. Answering
    # `tx.id.hex()` regardless of that boolean would tell the caller
    # this transaction was kept when it was not -- the same defect #277
    # fixed on the peer-to-peer path, `p2p/callbacks.py`'s `tx` handler.
    tip_height = len(node.chainstate.block_index.active_chain) - 1
    if not node.mempool.add_tx(tx, fee, vsize, height=tip_height):
        # Not kept: `Mempool._evict_to_limit` ran
        # and took this transaction right back out for being the worst
        # one held once `Mempool.bytesize_limit` was restored -- exactly
        # the case `_MEMPOOL_FULL_REASON`'s own comment names, Core's
        # `TX_RECONSIDERABLE` "mempool full". btclib-org/btclib-node#294
        raise RpcError(RPCErrorCode.VERIFY_REJECTED, _MEMPOOL_FULL_REASON)
    # Core's own `AddUnbroadcastTx`, called only here -- never for a
    # transaction a peer handed this node over the wire.
    # btclib-org/btclib-node#1421
    node.mempool.mark_broadcast_locally(tx.id)
    node.p2p_manager.broadcast_raw_transaction(tx, fee)
    return tx.id.hex()


def ping(node: Node, conn: RpcConnection, _: list[Any]) -> None:
    """Answer `ping` by sending every peer a fresh one, via `ping_all`.

    A peer at `BIP0031_VERSION` or below is sent a `ping` with no nonce,
    as in Core. btclib-org/btclib-node#1204

    Called on `Node`'s own thread, `handle_rpc`'s the same as every
    handler here; `ping_all` is defined on `P2pManager` but reaches this
    one call site as a plain method call, not a coroutine scheduled on
    that manager's own loop.
    """
    node.p2p_manager.ping_all()


def stop_wait_param(params: list[Any]) -> int | None:
    """Read `stop`'s own hidden `wait`, or `None` where none was given.

    `RPCArg::Type::NUM`, `RPCArg::Optional::OMITTED`, hidden from help
    (`src/rpc/server.cpp:155`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag): omitted or explicit `null` reads as `isNum()` false there, so
    neither delays the reply. Anything else that is not a JSON number is
    `RPC_TYPE_ERROR`, the same check `RPCMethod::HandleRequest` makes
    for every declared argument before the handler ever runs
    (`src/rpc/util.cpp:653-661`); a JSON float, or an integer outside
    C `int`'s range, is refused the way `_height_param` above already
    refuses a float, `UniValue::getInt<int>`'s own "JSON integer out of
    range" (`univalue.h`), thrown where `std::from_chars` cannot consume
    the number in full or reports it out of range.

    Called twice for one request that reaches it: here, to decide
    whether `stop` itself succeeds, and again by `rpc.main._answer_one`
    once it has, to read the same already-valid `wait` back out and
    schedule the delayed reply `stop`'s own docstring explains
    (btclib-org/btclib-node#1467) -- this function's own return value is
    not `stop`'s, which is the RPC's `result` field and cannot also
    carry it.
    """
    if not params or params[0] is None:
        return None
    value = params[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise type_error(1, "wait", value, "number")
    if isinstance(value, float) or not -(2**31) <= value < 2**31:
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    return value


def stop(node: Node, conn: RpcConnection, params: list[Any]) -> str:
    """Answer `stop`; `handle_rpc` delays this reply by `wait`, then stops.

    A `wait` in milliseconds holds the reply back that long, Core's own
    hidden testing argument (`src/rpc/server.cpp:155-166`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): "'stop 1000' makes the
    call wait 1 second before returning to the client". Read and
    validated here, through `stop_wait_param`, exactly as every other
    declared argument is validated by the callback that owns it; not
    slept on here, unlike Core's own `UninterruptibleSleep`, which this
    function has no equivalent of at all.

    Core's sleep runs once `stop` has already requested shutdown, on
    the request's own HTTP worker thread, which that shutdown joins
    before it goes on (`StopHTTPServer`, `src/httpserver.cpp`, same
    tag). A `time.sleep` here would run before `handle_rpc` requested
    this node's shutdown at all, on the one thread that carries RPC, P2P
    and chain work alike (`ARCHITECTURE.md`, "The loop").
    `rpc.main._answer_one` reads this same `wait` again once this call
    is known to have succeeded; `handle_rpc` hands the delayed reply to
    `RpcConnection.send_and_close_after` and stops the node at once, and
    `RpcManager.stop` finishes that reply the way Core's shutdown
    finishes its worker.
    """
    stop_wait_param(params)
    return "Btclib node stopping"


def help_rpc(node: Node, conn: RpcConnection, params: list[Any]) -> str:
    """Answer `help`; `rpc.help.answer_help` is the actual answer, node-free.

    Every handler here shares this module's own `(node, conn, params)`
    signature (this module's own docstring); `answer_help` needs none of
    it, reading only `params` and the two tables `rpc.help` builds from
    `HELP_TEXT` and `CATEGORY`.
    """
    return answer_help(params)


callbacks = {
    "getbestblockhash": get_best_block_hash,
    "getblockcount": get_block_count,
    "getblockchaininfo": get_blockchain_info,
    "pruneblockchain": prune_blockchain,
    "getblockhash": get_block_hash,
    "getblockheader": get_block_header,
    "getblock": get_block,
    "getchaintips": get_chain_tips,
    "submitblock": submit_block,
    "getpeerinfo": get_peer_info,
    "getconnectioncount": get_connection_count,
    "getnetworkinfo": get_network_info,
    "addnode": add_node,
    "disconnectnode": disconnect_node,
    "setban": set_ban,
    "listbanned": list_banned,
    "clearbanned": clear_banned,
    "getmempoolinfo": get_mempool_info,
    "getrawmempool": get_raw_mempool,
    "getmempoolentry": get_mempool_entry,
    "getrawtransaction": get_raw_transaction,
    "gettxout": get_tx_out,
    "gettxoutsetinfo": get_tx_out_set_info,
    "testmempoolaccept": test_mempool_accept,
    "sendrawtransaction": send_raw_transaction,
    "ping": ping,
    "stop": stop,
    "help": help_rpc,
}

# Each method's parameter names, in the order of its positions, as its
# `RPCHelpMan` declares them and `CRPCCommand::argNames` carries them
# (`src/rpc/server.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): what
# `rpc.jsonrpc.transform_named_arguments` maps an object's keys onto.
# `a|b` is two names for one position. None of these methods takes an
# `OBJ_NAMED_PARAMS` options object, so no name here is named-only.
# `bitcoind`'s own table is what `help dump_all_command_conversions`
# answers, and `tests/integration/rpc_framing_test.py` holds this one to it.
arg_names: dict[str, tuple[str, ...]] = {
    "getbestblockhash": (),
    "getblockcount": (),
    "getblockchaininfo": (),
    "pruneblockchain": ("height",),
    "getblockhash": ("height",),
    "getblockheader": ("blockhash", "verbose"),
    "getblock": ("blockhash", "verbosity|verbose"),
    "getchaintips": (),
    "submitblock": ("hexdata", "dummy"),
    "getpeerinfo": (),
    "getconnectioncount": (),
    "getnetworkinfo": (),
    "addnode": ("node", "command", "v2transport"),
    "disconnectnode": ("address", "nodeid"),
    "setban": ("subnet", "command", "bantime", "absolute"),
    "listbanned": (),
    "clearbanned": (),
    "getmempoolinfo": (),
    "getrawmempool": ("verbose", "mempool_sequence"),
    "getmempoolentry": ("txid",),
    "getrawtransaction": ("txid", "verbosity|verbose", "blockhash"),
    "gettxout": ("txid", "n", "include_mempool"),
    "gettxoutsetinfo": ("hash_type", "hash_or_height", "use_index"),
    "testmempoolaccept": ("rawtxs", "maxfeerate"),
    "sendrawtransaction": ("hexstring", "maxfeerate", "maxburnamount"),
    "ping": (),
    "stop": ("wait",),
    "help": ("command",),
}
