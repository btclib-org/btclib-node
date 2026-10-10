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

import contextlib
import math
import re
import threading
import time
from io import BytesIO
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from bitcoin_core_rpc import RPCErrorCode, chain_from_network
from btclib import b32, b58
from btclib.block import Block, BlockHeader, median_time_past
from btclib.consensus import MAX_SCRIPT_ELEMENT_SIZE, MAX_SCRIPT_SIZE
from btclib.exceptions import BTClibException, BTClibTypeError, BTClibValueError
from btclib.fee import FeeRate, fee_from_vsize
from btclib.p2p.address import ServiceFlags
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2
from btclib.p2p.limits import PROTOCOL_VERSION
from btclib.policy import dust_outputs
from btclib.script.script import op_code_spans, script_to_dict
from btclib.script.spendability import is_unspendable
from btclib.tx import Tx
from btclib.tx.out_point import OutPoint
from btclib_wallet.descriptors import Provider, infer_descriptor

from btclib_node.block_db import Coin
from btclib_node.chainstate.block_index import BlockStatus, block_time
from btclib_node.constants import MIN_BLOCKS_TO_KEEP, USER_AGENT
from btclib_node.exceptions import MissingPrevoutError, TxRejectedError
from btclib_node.fee_estimator import track_accepted
from btclib_node.main import (
    already_confirmed,
    assert_valid_block,
    check_fork_warning_conditions,
    invalidate_chain,
    is_block_failed,
    is_cached_invalid,
    new_pow_valid_block,
    parent_lookup,
    passes_check_block,
    precious_chain,
    prune_up_to_height,
    reconsider_chain,
    update_chain,
    verify_mempool_acceptance,
)
from btclib_node.mempool_persist import FILENAME as MEMPOOL_FILENAME
from btclib_node.mempool_persist import dump_mempool, load_mempool
from btclib_node.p2p.address import SEEDS_SERVICE_FLAGS, ip_and_port, network_class
from btclib_node.p2p.banman import (
    SpecialAddress,
    Subnet,
    is_valid_host,
    lookup_host,
    lookup_subnet,
)
from btclib_node.p2p.connection import local_services
from btclib_node.p2p.eviction import Network, is_valid
from btclib_node.p2p.permissions import permission_names
from btclib_node.rpc.connection import COIN, RawJSON, btc_amount
from btclib_node.rpc.errors import (
    RpcError,
    bool_mismatch,
    bool_param,
    is_hex,
    json_type_name,
    parse_hash_v,
    type_error,
    type_errors,
)
from btclib_node.rpc.fees import estimate_raw_fee, estimate_smart_fee
from btclib_node.rpc.help import HELP_TEXT, answer_help
from btclib_node.rpc.jsonrpc import JsonObject, get_real
from btclib_node.rpc.mining import (
    generate_block,
    generate_to_address,
    get_block_template,
    wait_for_block,
    wait_for_block_height,
    wait_for_new_block,
)
from btclib_node.rpc.package import (
    Outcome,
    accepted,
    package_test_accept,
    submit_package,
)
from btclib_node.rpc.signing import (
    combine_raw_transaction,
    sign_message_with_privkey,
    sign_raw_transaction_with_key,
    verify_message,
)
from btclib_node.rpc.snapshot import dump_tx_out_set
from btclib_node.rpc.solver import solver
from btclib_node.rpc.utxo_set import scan_tx_out_set

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from btclib_node import Node
    from btclib_node.chainstate.block_index import BlockIndex
    from btclib_node.chainstate.utxo_index import UtxoIndex
    from btclib_node.mempool import Mempool
    from btclib_node.p2p.block_availability import BlockAvailability
    from btclib_node.p2p.connection import Connection
    from btclib_node.rpc.connection import RpcConnection

__all__ = [
    "add_connection",
    "add_node",
    "add_peer_address",
    "arg_names",
    "callbacks",
    "clear_banned",
    "decode_raw_transaction",
    "disconnect_node",
    "get_best_block_hash",
    "get_block",
    "get_block_count",
    "get_block_hash",
    "get_block_header",
    "get_blockchain_info",
    "get_chain_tips",
    "get_connection_count",
    "get_mempool_ancestors",
    "get_mempool_cluster",
    "get_mempool_descendants",
    "get_mempool_entry",
    "get_mempool_feerate_diagram",
    "get_mempool_info",
    "get_network_info",
    "get_node_addresses",
    "get_orphan_txs",
    "get_peer_info",
    "get_prioritised_transactions",
    "get_raw_mempool",
    "get_raw_transaction",
    "get_rpc_info",
    "get_tx_out",
    "get_tx_out_set_info",
    "get_tx_spending_prevout",
    "help_rpc",
    "import_mempool",
    "invalidate_block",
    "list_banned",
    "named_only",
    "ping",
    "precious_block",
    "prioritise_transaction",
    "prune_blockchain",
    "reconsider_block",
    "save_mempool",
    "send_raw_transaction",
    "service_names",
    "set_ban",
    "set_network_active",
    "stop",
    "stop_wait_param",
    "submit_block",
    "submit_header",
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
    `node.config.minimum_chain_work` and tip age against
    `node.config.max_tip_age`, not merely whether this node has run out
    of candidates to try.
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

    `warnings` is `node.warnings.get_messages()`, Core's own
    `node::GetWarningsForRpc(*node.warnings, IsDeprecatedRPCEnabled
    ("warnings"))` (src/rpc/blockchain.cpp:1443, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) -- always the array form:
    this node has no `-deprecatedrpc` of its own, so the single-string
    form `use_deprecated=true` answers with is never reachable here.
    Empty, ordinarily: it takes an invalid chain with more work than this
    node's own tip (`main.check_fork_warning_conditions`,
    btclib-org/btclib-node#1522) or a version bit no deployment uses
    reaching its threshold (`versionbits.check_unknown_activations`,
    btclib-org/btclib-node#1475) to set anything.

    Absent, each for its own reason rather than by oversight:
    `verificationprogress`, Core's own `GuessVerificationProgress`
    (src/validation.cpp:5519, at bitcoin/bitcoin@ca7162cde5)
    extrapolating from `ChainTxData`, an assumed transaction rate for
    the chain as a whole, against each block's own accumulated
    transaction count (`CBlockIndex::m_chain_tx_count`) -- `chains.py`
    carries neither the per-chain assumption nor a per-block count, so
    answering this member under Core's own name would answer a number
    carrying none of Core's meaning behind it, rather than a truthful
    one; `signet_challenge`, `SigNet` here carrying no configurable
    challenge (chains.py's own genesis is the one public signet);
    `backgroundvalidation`, present on Core's own side only behind an
    assumeutxo snapshot this node has no counterpart to.
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
        "warnings": node.warnings.get_messages(),
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
    block_hash = parse_hash_v("hash", params[0])
    try:
        block_info = block_index.get_block_info(block_hash)
    except KeyError as error:
        # a hash nothing indexed is a question about a block, not a
        # fault of this node: src/rpc/blockchain.cpp:664-665,
        # at bitcoin/bitcoin@ca7162cde5
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


def _known_block_hash(node: Node, params: list[Any], method: str) -> bytes:
    """Validate `invalidateblock`/`reconsiderblock`'s own single `blockhash`.

    `preciousblock`'s too. Each takes Core's own one required `STR_HEX`
    argument and answers the same refusals in the same order: a missing
    one is `method`'s own full help text under `RPC_MISC_ERROR`, the
    shape `get_block_hash`'s own missing-argument comment already
    argues; a wrongly typed or wrongly shaped one is
    `type_error`/`parse_hash_v`, exactly as `get_block_header`'s own
    `"hash"`-labelled argument is checked (`ParseHashV`, same label this
    index's own callers use for it); and a 64-character hex string this
    index does not know is `RPC_INVALID_ADDRESS_OR_KEY`,
    `"Block not found"` -- Core's own `LookupBlockIndex` failure in
    `InvalidateBlock`, `ReconsiderBlock` and `preciousblock`
    (`rpc/blockchain.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), matching `get_block`/`get_block_header`'s own identical
    refusal for the identical failure above.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT[method])
    if not isinstance(params[0], str):
        raise type_error(1, "blockhash", params[0], "string")
    block_hash = parse_hash_v("blockhash", params[0])
    try:
        node.chainstate.block_index.get_block_info(block_hash)
    except KeyError as error:
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Block not found"
        ) from error
    return block_hash


def invalidate_block(node: Node, conn: RpcConnection, params: list[Any]) -> None:
    """Answer `invalidateblock`, Core's own single `blockhash` argument.

    Core's own `invalidateblock` (`rpc/blockchain.cpp:1716-1738`, calling
    the free `InvalidateBlock`, `:1695-1714`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `_known_block_hash` above
    is this function's and `reconsider_block`'s own shared argument
    check; `main.invalidate_chain`'s own docstring is where marking the
    block, forcing the chain off it and what a storage failure on the
    way down answers with are each argued -- `RPC_DATABASE_ERROR`, the
    one answer Core gives a failed `ActivateBestChain` here, is not
    reproduced, for the reason that docstring gives.
    """
    block_hash = _known_block_hash(node, params, "invalidateblock")
    invalidate_chain(node, block_hash)


def reconsider_block(node: Node, conn: RpcConnection, params: list[Any]) -> None:
    """Answer `reconsiderblock`, Core's own single `blockhash` argument.

    Core's own `reconsiderblock` (`rpc/blockchain.cpp:1761-1785`, calling
    the free `ReconsiderBlock`, `:1741-1759`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `_known_block_hash` above
    is this function's and `invalidate_block`'s own shared argument
    check; `main.reconsider_chain`'s own docstring is where clearing the
    mark and retrying the chain are argued.
    """
    block_hash = _known_block_hash(node, params, "reconsiderblock")
    reconsider_chain(node, block_hash)


def precious_block(node: Node, conn: RpcConnection, params: list[Any]) -> None:
    """Answer `preciousblock`, Core's own single `blockhash` argument.

    Core's own `preciousblock` (`rpc/blockchain.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `_known_block_hash` checks
    the argument, and `main.precious_chain` prefers the block and
    retries the chain. Core answers `RPC_DATABASE_ERROR` where
    `ActivateBestChain` fails on its own storage; here that failure
    raises out of `update_chain`, and `rpc.main._execute` answers
    `INTERNAL_ERROR`, as for `invalidate_block`.
    """
    block_hash = _known_block_hash(node, params, "preciousblock")
    precious_chain(node, block_hash)


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


def _parse_verbosity(
    params: list[Any], position: int, *, default: int, allow_bool: bool = True
) -> int:
    """Read a `verbosity` argument as Core's `ParseVerbosity` does.

    `ParseVerbosity` (`rpc/util.cpp:83-96`, at bitcoin/bitcoin@9be056a8a7)
    answers `default` for a missing or null argument and a JSON bool as
    1 or 0, the argument being declared `skip_type_check` in `getblock`
    and `getrawtransaction` alike. Anything else goes to
    `UniValue::getInt<int>()` (`src/univalue/include/univalue.h
    :142-153`): a non-integral number, or one outside a 32-bit `int`, is
    `RPC_MISC_ERROR` "JSON integer out of range", and a value that is no
    number is `RPC_TYPE_ERROR` with the bare "JSON value of type ... is
    not of expected type number", not the "Wrong type passed" object of
    a type-checked argument (measured against bitcoind v31.1.0 for both
    RPCs). `getorphantxs` passes `allow_bool=False`, which makes a JSON bool
    `RPC_TYPE_ERROR` "Verbosity was boolean but only integer allowed"
    (measured against the same bitcoind).
    """
    if len(params) <= position or params[position] is None:
        return default
    value = params[position]
    if isinstance(value, bool):
        if not allow_bool:
            raise RpcError(
                RPCErrorCode.TYPE_ERROR,
                "Verbosity was boolean but only integer allowed",
            )
        return int(value)
    if isinstance(value, float):
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    if isinstance(value, int):
        if not -(2**31) <= value < 2**31:
            raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
        return value
    raise RpcError(
        RPCErrorCode.TYPE_ERROR,
        f"JSON value of type {json_type_name(value)} is not of expected type number",
    )


def _parse_get_block_params(params: list[Any]) -> tuple[bytes, int]:
    """Validate `getblock`'s own two arguments; answer `(blockhash, verbosity)`.

    The verbosity is `_parse_verbosity`'s, 1 by default. Any `int` is a
    verbosity, and `get_block` reads one at or below 0 as 0 and one at or
    above 3 as 3, as `getblock` does.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["getblock"])
    if not isinstance(params[0], str):
        raise type_error(1, "blockhash", params[0], "string")
    # `ParseHashV(request.params[0], "blockhash")` (`rpc/blockchain.cpp
    # :828`, same sha) -- `getblock`'s own label, not `get_block_header`'s
    # "hash", the two RPCs naming the same positional argument differently.
    block_hash = parse_hash_v("blockhash", params[0])

    verbosity = _parse_verbosity(params, 1, default=1)
    return block_hash, verbosity


# Core's own refusal when a block's undo data is wanted and cannot be read
# (`src/rpc/blockchain.cpp` and `src/rpc/rawtransaction.cpp`, at
# bitcoin/bitcoin@9be056a8a7)
_UNDO_UNREADABLE = (
    "Undo data expected but can't be read. This could be due to "
    "disk corruption or a conflict with a pruning event."
)


def _block_undo(
    node: Node, block_hash: bytes, block: Block
) -> list[list[Coin] | None] | None:
    """Answer the coins each transaction of `block` spent, in input order.

    Core's `blockToJSON` hands `TxToUniv` the `CTxUndo` of every
    transaction but the coinbase, which has none, when the block's undo
    data is held, and none at all where it is not
    (`src/rpc/blockchain.cpp:223-239`, at bitcoin/bitcoin@9be056a8a7).
    The reverse patch `UtxoIndex.add_block` files for a connected block
    lists the coin of every input of every transaction after the
    coinbase, in block order (`RevBlock.to_add`), so each transaction
    takes the next `len(vin)` of them. A patch that holds another number
    is Core's "Undo data expected but can't be read". None is a block
    whose patch is not held.
    """
    rev_block = node.block_db.get_rev_block(block_hash)
    if rev_block is None:
        return None
    coins = [coin for _, coin in rev_block.to_add]
    if len(coins) != sum(len(tx.vin) for tx in block.transactions[1:]):
        raise RpcError(RPCErrorCode.INTERNAL_ERROR, _UNDO_UNREADABLE)
    undo: list[list[Coin] | None] = [None]
    start = 0
    for tx in block.transactions[1:]:
        undo.append(coins[start : start + len(tx.vin)])
        start += len(tx.vin)
    return undo


def get_block(
    node: Node, conn: RpcConnection, params: list[Any]
) -> str | dict[str, Any]:
    """Answer `getblock`: the block's hex, or Core's own JSON shape.

    Core's own `getblock` (`rpc/blockchain.cpp:761-841`, calling
    `blockToJSON`, `:200-243`, at bitcoin/bitcoin@9be056a8a7) answers
    verbosity 0 or below with the hex, and verbosity 1 with the header
    fields `blockheaderToJSON` answers (`get_block_header`'s own verbose
    branch, mirrored by `_block_json_header` above) plus `strippedsize`,
    `size`, `weight`, `coinbase_tx` and `tx` as an array of txids
    (`TxVerbosity::SHOW_TXID`). Verbosity 2 answers `tx` as an array of
    decoded transactions instead, `getrawtransaction`'s own verbose shape
    (`_tx_to_univ`, with `hex`) plus the `fee` of a transaction whose
    undo data is held; verbosity 3 and above add each input's `prevout`
    (`TxVerbosity::SHOW_DETAILS_AND_PREVOUT`). `_block_undo` is where the
    undo data is read.
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

    if verbosity <= 0:
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
        undo = _block_undo(node, block_hash, block) or [None] * len(block.transactions)
        out["tx"] = [
            _tx_to_univ(
                tx,
                node.chain.name,
                include_hex=True,
                undo=undo[i],
                prevout=verbosity >= 3,  # noqa: PLR2004
            )
            for i, tx in enumerate(block.transactions)
        ]
    return out


def _index_submitted_header(block_index: BlockIndex, block: Block) -> str | None:
    """Index a submitted block's header if new; answer why to stop, or None.

    `"duplicate"` for a block already downloaded whose body passes
    `CheckBlock` (`main.passes_check_block`), `"prev-blk-not-found"` for
    a header whose parent is unknown, and Core's reject reason for a
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
        # Core answers `GetRejectReason()` alone, never its debug message
        if isinstance(failed[1], TxRejectedError):
            return failed[1].reason
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
    `ContextualCheckBlockHeader`'s `bad-diffbits`, `time-too-old`,
    `time-timewarp-attack`, `time-too-new` and `"bad-version(0x%08x)"`,
    and `CheckBlockHeader`'s `high-hash`, which `add_headers` raises in
    Core's words (`src/validation.cpp`, at
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
    are Core's literal reasons already; `interpreter.check_scripts`
    wraps a script failure into `BlockScriptVerifyError`, whose own
    `str()` is Core's `block-script-verify-flag-failed (%s)`
    (`CheckInputScripts`, same file and sha) with `ScriptErrorString`.
    A spend of an immature coinbase and one over its inputs are Core's
    `bad-txns-premature-spend-of-coinbase` and `bad-txns-in-belowout`,
    the reason alone as `GetRejectReason` gives it, and so are
    `bad-txns-accumulated-fee-outofrange` and `bad-blk-sigops`. Anything
    else `update_chain`'s
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
                # Core's own `InvalidChainFound` call, same citation as
                # `main.check_fork_warning_conditions`'s own docstring
                check_fork_warning_conditions(node)
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


def submit_header(node: Node, conn: RpcConnection, params: list[Any]) -> None:
    """Answer `submitheader`: index one header alone, or refuse it.

    Core's own `submitheader` (`rpc/mining.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). `hexdata` is decoded as
    `DecodeHexBlockHeader` decodes it: `is_hex`, then the first eighty
    bytes, any after them ignored as Core's `SpanReader` ignores them.
    A header whose parent this index does not hold is refused before any
    check. `BlockIndex.add_headers` then checks and indexes it, as
    `ProcessNewBlockHeaders` does, and `None` answers a header indexed
    now or already. A refusal answers the reason `add_headers` raises,
    Core's word, such as `duplicate-invalid`, `bad-prevblk`,
    `bad-diffbits` or `high-hash`.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["submitheader"])
    if not isinstance(params[0], str):
        raise type_error(1, "hexdata", params[0], "string")
    decode_failed = RpcError(
        RPCErrorCode.DESERIALIZATION_ERROR, "Block header decode failed"
    )
    if not is_hex(params[0]):
        raise decode_failed
    try:
        header = BlockHeader.parse(
            BytesIO(bytes.fromhex(params[0])), check_validity=False
        )
    except BTClibValueError as error:
        raise decode_failed from error

    block_index = node.chainstate.block_index
    parent = header.previous_block_hash
    if parent not in block_index.header_dict:
        raise RpcError(
            RPCErrorCode.VERIFY_ERROR,
            f"Must submit previous header ({parent.hex()}) first",
        )
    try:
        block_index.add_headers([header])
    except BTClibValueError as error:
        raise RpcError(RPCErrorCode.VERIFY_ERROR, str(error)) from error


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


def _transport_fields(p2p_conn: Connection) -> dict[str, str]:
    """Return `getpeerinfo`'s `transport_protocol_type` and `session_id`.

    Core's `Transport::Info`: "detecting" until a responder has told v1
    from v2, and no session id for anything but v2. Read from this
    thread while the connection's loop writes it, which `V2Transport`'s
    own locks are for.
    """
    info = p2p_conn.transport.get_info()
    return {
        "transport_protocol_type": str(info.transport_type),
        "session_id": "" if info.session_id is None else info.session_id.hex(),
    }


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
    entry["network"] = _network_name(p2p_conn.connected_through_network)
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
    # `connect_nodes` (`test_framework.py:568-594`,
    # at bitcoin/bitcoin@bb529657) matches this against the peer's own
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
    # Core's `m_bip152_highbandwidth_to` and `m_bip152_highbandwidth_from`:
    # whether this node chose the peer as high-bandwidth, which
    # `compact_block.maybe_set_peer_as_announcing_header_and_ids` does, and
    # whether the peer's own `sendcmpct` chose this node, which
    # `p2p.callbacks.sendcmpct` records
    entry["bip152_hb_to"] = p2p_conn.bip152_highbandwidth_to
    entry["bip152_hb_from"] = p2p_conn.requested_hb_cmpctblocks
    # Core's `GetPresyncHeight` while a low-work headers sync runs with
    # the peer, in either of its phases, and -1 where none does
    entry["presynced_headers"] = (
        -1 if p2p_conn.headers_sync is None else p2p_conn.headers_sync.presync_height
    )
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
    entry["permissions"] = permission_names(p2p_conn.permissions)
    entry["minfeefilter"] = btc_amount(p2p_conn.feefilter if relays else 0)
    # Core's tables are `std::map`s, iterated in key order, and push
    # only a type with bytes counted.
    entry["bytessent_per_msg"] = dict(
        sorted(p2p_conn.stats.bytes_sent_per_msg.copy().items())
    )
    entry["bytesrecv_per_msg"] = dict(
        sorted(p2p_conn.stats.bytes_recv_per_msg.copy().items())
    )
    entry["connection_type"] = _connection_type(p2p_conn)
    entry.update(_transport_fields(p2p_conn))
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


def set_network_active(node: Node, conn: RpcConnection, params: list[Any]) -> bool:
    """Answer `setnetworkactive`: disable or enable all p2p activity.

    Core's own (`src/rpc/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag): one required boolean, `state`, read by
    `P2pManager.set_network_active` -- its own docstring argues the
    effect, Core's `CConnman::SetNetworkActive` -- and answered straight
    back, Core's own `connman.GetNetworkActive()` read right after the
    call that set it.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["setnetworkactive"])
    state = params[0]
    if not isinstance(state, bool):
        raise type_error(1, "state", state, "bool")
    node.p2p_manager.set_network_active(active=state)
    return state


def get_network_info(node: Node, conn: RpcConnection, _: list[Any]) -> dict[str, Any]:
    """Answer `getnetworkinfo` with `connect_nodes`'s fields, plus `warnings`.

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

    `warnings` is `node.warnings.get_messages()`, the same array
    `get_blockchain_info`'s own `warnings` answers -- Core's own
    `getnetworkinfo` reads the identical `node.warnings` `rpc/net.cpp`
    does (`:740`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag -- not
    `bb529657` above, this paragraph's own citation), so the two RPCs
    never disagree here either.

    `localaddresses` is what `P2pManager.local_snapshot` holds, as Core's
    `mapLocalHost` is read under its mutex (`rpc/net.cpp:728-738`, same
    sha), in the order of its `std::map` of `CNetAddr`: IPv4 before IPv6,
    then by octets.
    """
    services = local_services(node.config)
    local = sorted(
        node.p2p_manager.local_snapshot().values(),
        key=lambda info: (info.address.version, info.address.packed),
    )
    return {
        "subversion": USER_AGENT,
        "protocolversion": PROTOCOL_VERSION,
        "localservices": f"{services:016x}",
        "localservicesnames": service_names(services),
        "localaddresses": [
            {"address": str(info.address), "port": info.port, "score": info.score}
            for info in local
        ],
        "warnings": node.warnings.get_messages(),
    }


# Core's own three `addnode` commands (`rpc/net.cpp:341-415`,
# at bitcoin/bitcoin@bb529657): `add`/`remove` reach `P2pManager`'s own
# `add_added_peer`/`remove_added_peer`, its counterpart to `CConnman`'s
# `AddNode`/`RemoveAddedNode`, and `_open_added_peers`
# (`p2p/manager.py`) is what actually dials whatever the list holds,
# never this function (btclib-org/btclib-node#1350). `onetry` schedules
# the identical one-shot dial Core's `OpenNetworkConnection` does
# (`conn_type=MANUAL`, no persistence, no dedup) -- the one command
# `connect_nodes`, the one caller this node's own tf2 census names for
# this method (`test_framework.py:568-594`, same sha), ever calls.
_ADDNODE_COMMANDS = ("add", "remove", "onetry")


def _parsed_addnode_args(node: Node, params: list[Any]) -> tuple[str, str, bool]:
    """Return `addnode`'s `(node, command, v2transport)`, or raise as Core does.

    `v2transport` is the argument, or whether this node supports v2 where
    it is omitted or null, and `true` without the support is refused:
    `rpc/net.cpp`'s own check, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag, which `remove` meets too. `false` without `-v1transport` is
    refused the same way, as this node's own mirror of it, for refusing
    v1 (btclib-org/btclib-node#1190).

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

    if not node_arg.strip():
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER, "Error: Node address cannot be empty"
        )
    node_v2transport = bool(local_services(node.config) & ServiceFlags.NODE_P2P_V2)
    use_v2transport = bool_param(
        params, 2, name="v2transport", default=node_v2transport
    )
    if use_v2transport and not node_v2transport:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Error: v2transport requested but not enabled (see -v2transport)",
        )
    if not use_v2transport and not node.config.v1transport:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Error: v1transport requested but not enabled (see -v1transport)",
        )
    return node_arg, command, use_v2transport


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
    against is a decision, not an oversight: `CONTRIBUTING.md`'s own
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
    reasoning. `v2transport` is Core's own optional third argument, kept
    with an added peer and passed to a `onetry` dial.
    """
    node_arg, command, use_v2transport = _parsed_addnode_args(node, params)

    if command == "add":
        if not node.p2p_manager.add_added_peer(
            node_arg, use_v2transport=use_v2transport
        ):
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

    # `node_arg` whole: Core's own `onetry` passes `node_arg` itself as
    # `pszDest` (`src/rpc/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    # v31.1 tag), so a port the caller gave reaches `addr_name` too
    # (btclib-org/btclib-node#1493).
    node.p2p_manager.connect_host(
        node_arg, node.chain.port, use_v2transport=use_v2transport
    )


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


# Core's own four names (`rpc/net.cpp`, at bitcoin/bitcoin@9be056a8a7,
# the v31.1 tag) -- `manual` is v31.1's own fifth `addconnection`
# connection type, past this pinned release (issue #1465's own
# citation), so it is not one of these.
_ADDCONNECTION_TYPES = (
    "outbound-full-relay",
    "block-relay-only",
    "addr-fetch",
    "feeler",
)


def _refuse_unoffered_transport(node: Node, *, v2transport: bool) -> None:
    """Refuse the `v2transport` of `addconnection` this node does not offer."""
    if v2transport and not (local_services(node.config) & ServiceFlags.NODE_P2P_V2):
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Error: Adding v2transport connections requires -v2transport "
            "init flag to be set.",
        )
    if not v2transport and not node.config.v1transport:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Error: Adding v1transport connections requires -v1transport "
            "init flag to be set.",
        )


def add_connection(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `addconnection`: dial one peer of a chosen type, regtest only.

    Core's own (`src/rpc/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag). The three declared arguments are all required, so the
    generic checks `RPCHelpMan::HandleRequest` runs ahead of every
    method's own lambda -- the missing-argument and wrong-JSON-type
    refusals, `_height_param`'s own docstring arguing the shape -- come
    first here too, ahead of the chain gate the real lambda opens on.

    A `connection_type` outside `_ADDCONNECTION_TYPES` is
    `RPC_INVALID_PARAMETER` carrying this method's own full help text,
    `self.ToString()`'s shape (measured against a real bitcoind
    v31.1.0). `v2transport` true is refused the same way Core refuses it
    lacking `NODE_P2P_V2` (`connman.GetLocalServices() & NODE_P2P_V2`),
    which `local_services` (`p2p/connection.py`) sets with `-v2transport`.
    `v2transport` false is refused without `-v1transport`, this node's own
    mirror of that, for refusing v1 (btclib-org/btclib-node#1190).

    Both of `CConnman::AddConnection`'s capacity checks, the per-type
    cap and the shared `semOutbound` pool, are
    `P2pManager.reserve_automatic_slot`'s, which takes the slot in the
    same step. Its docstring says why the check and the reservation are
    one step here, where Core's dial is synchronous. `connect_typed`
    releases the slot once the dial concludes.

    `automatic=True` for a `feeler` too, as `_dial_one_draw` sets it for
    a drawn one: `maybe_discourage_and_disconnect` reads `automatic` as
    "not `MANUAL`", and a feeler is not `MANUAL` in Core either.

    The dial itself, `P2pManager.connect_typed`, runs fire-and-forget,
    the same as `onetry`'s (`add_node` above, `connect_host`): this
    answers once the dial is scheduled, not once it connects, which is
    also why nothing here waits to learn whether it does.
    """
    if len(params) < 3:  # noqa: PLR2004
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["addconnection"])
    address, connection_type, v2transport = params[0], params[1], params[2]
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(address, str):
        mismatches.append((1, "address", address, "string"))
    if not isinstance(connection_type, str):
        mismatches.append((2, "connection_type", connection_type, "string"))
    if not isinstance(v2transport, bool):
        mismatches.append((3, "v2transport", v2transport, "bool"))
    if mismatches:
        raise type_errors(*mismatches)
    # Core's own `util::TrimStringView(..., " \f\n\r\t\v")`
    # (`rpc/net.cpp:414`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag),
    # ahead of the four-way match below and echoed back in the answer
    # the same way: `str.strip` with no argument strips Unicode
    # whitespace Core's own six-character pattern does not, so the
    # characters are named explicitly rather than left to that default.
    connection_type = connection_type.strip(" \f\n\r\t\v")
    if node.chain.name != "regtest":
        raise RpcError(
            RPCErrorCode.MISC_ERROR,
            "addconnection is for regression testing (-regtest mode) only.",
        )
    if connection_type not in _ADDCONNECTION_TYPES:
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, HELP_TEXT["addconnection"])
    _refuse_unoffered_transport(node, v2transport=v2transport)
    manager = node.p2p_manager
    if manager.reserve_automatic_slot(connection_type) is None:
        raise RpcError(
            RPCErrorCode.CLIENT_NODE_CAPACITY_REACHED,
            "Error: Already at capacity for specified connection type.",
        )
    manager.connect_typed(
        address,
        node.chain.port,
        automatic=connection_type
        in {"outbound-full-relay", "block-relay-only", "feeler"},
        block_relay=connection_type == "block-relay-only",
        feeler=connection_type == "feeler",
        addr_fetch=connection_type == "addr-fetch",
        reserved=connection_type,
        use_v2transport=v2transport,
    )
    return {"address": address, "connection_type": connection_type}


# Core's `ParseNetwork` (`src/netbase.cpp`, at bitcoin/bitcoin@9be056a8a7,
# the v31.1 tag): the five names `getnodeaddresses` takes
_NETWORK_NAMES = {
    network.name.lower(): network
    for network in (
        Network.IPV4,
        Network.IPV6,
        Network.ONION,
        Network.I2P,
        Network.CJDNS,
    )
}
_MAX_PORT = 65535


def _number_mismatch(
    params: list[Any], position: int, *, name: str
) -> tuple[int, str, object, str] | None:
    """Return `type_errors`' mismatch for a declared `NUM` that is no number."""
    value = params[position] if len(params) > position else None
    if value is None or (
        not isinstance(value, bool) and isinstance(value, (int, float))
    ):
        return None
    return (position + 1, name, value, "number")


def _integer(value: object, low: int, high: int) -> int:
    """Read a JSON number as UniValue's `getInt` does, from `low` to `high`."""
    if not isinstance(value, int) or not low <= value <= high:
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    return value


def _address_text(address: NetworkAddressV2) -> str:
    """Return `CNetAddr::ToStringAddr` of `address`."""
    if address.network_id in (BIP155Network.TORV3, BIP155Network.I2P):
        return str(SpecialAddress(BIP155Network(address.network_id), address.address))
    return str(ip_address(address.address))


def get_node_addresses(
    node: Node, conn: RpcConnection, params: list[Any]
) -> list[dict[str, Any]]:
    """Answer `getnodeaddresses`: known addresses, after quality and recency.

    Core's own (`src/rpc/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag): `CConnman::GetAddressesUnsafe` of `count` and no
    percentage, which is `PeerDB.get_addr`, a discouraged or banned host
    left out as in a `getaddr` answer. `count` `0` answers every
    address, a negative one is refused, and `network` is one of Core's
    five names in any case.
    """
    mismatches = [
        mismatch
        for mismatch in (
            _number_mismatch(params, 0, name="count"),
            None
            if len(params) < 2 or params[1] is None or isinstance(params[1], str)  # noqa: PLR2004
            else (2, "network", params[1], "string"),
        )
        if mismatch is not None
    ]
    if mismatches:
        raise type_errors(*mismatches)
    count = 1 if len(params) < 1 or params[0] is None else params[0]
    count = _integer(count, -(1 << 31), (1 << 31) - 1)
    if count < 0:
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, "Address count out of range")
    network = None
    if len(params) > 1 and params[1] is not None:
        network = _NETWORK_NAMES.get(params[1].lower())
        if network is None:
            raise RpcError(
                RPCErrorCode.INVALID_PARAMETER,
                f"Network not recognized: {params[1]}",
            )
    manager = node.p2p_manager
    return [
        {
            "time": address.timestamp,
            "services": int(address.services),
            "address": _address_text(address),
            "port": address.port,
            "network": network_class(address).name.lower(),
        }
        for address in manager.peer_db.get_addr(count, 0, network)
        if not manager.is_discouraged(address)
        and not manager.ban_man.is_peer_banned(address)
    ]


def add_peer_address(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `addpeeraddress`: add one address to the table, for testing.

    Core's own, a hidden command (`src/rpc/net.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the address is added
    with `NODE_NETWORK | NODE_WITNESS`, the time now, and itself as its
    source, which is no penalty, and with `tried` moved to the answered
    table as a handshake does. An address the table already holds, or
    refuses, is `failed-adding-to-new`, with `tried` too. The host is
    read as `LookupHost` does without DNS, `lookup_host`; Core flips an
    IPv6 address in `fc00::/8` to CJDNS where `-cjdnsreachable` is set,
    which this node has no counterpart to.
    """
    if len(params) < 2:  # noqa: PLR2004
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["addpeeraddress"])
    mismatches = [
        mismatch
        for mismatch in (
            None if isinstance(params[0], str) else (1, "address", params[0], "string"),
            _number_mismatch(params, 1, name="port"),
            bool_mismatch(params, 2, name="tried"),
        )
        if mismatch is not None
    ]
    if mismatches:
        raise type_errors(*mismatches)
    tried = bool_param(params, 2, name="tried", default=False)
    port = _integer(params[1], 0, _MAX_PORT)
    host = lookup_host(params[0])
    if host is None:
        raise RpcError(RPCErrorCode.CLIENT_INVALID_IP_OR_SUBNET, "Invalid IP address")
    if isinstance(host, SpecialAddress):
        network_id, octets = host.network, host.packed
    else:
        network_id = (
            BIP155Network.IPV4 if host.version == 4 else BIP155Network.IPV6  # noqa: PLR2004
        )
        octets = host.packed
    address = NetworkAddressV2(
        int(time.time()), SEEDS_SERVICE_FLAGS, network_id, octets, port
    )
    peer_db = node.p2p_manager.peer_db
    result: dict[str, Any] = {}
    success = False
    if peer_db.add_addresses([address], source=address):
        success = True
        if tried and not peer_db.add_active_address(address):
            success = False
            result["error"] = "failed-adding-to-tried"
    else:
        result["error"] = "failed-adding-to-new"
    result["success"] = success
    return result


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


def get_mempool_info(node: Node, conn: RpcConnection, _: list[Any]) -> dict[str, Any]:
    """Answer `getmempoolinfo` with the fields this tree backs for real.

    `maxdatacarriersize` is `0` where `-datacarrier` is off, as Core's
    `value_or(0)`.

    The comment below argues, field by field, why Core's own several
    others are left out rather than answered with a placeholder, and
    why `mempoolminfee`, `minrelaytxfee` and `incrementalrelayfee` are
    BTC/kvB rather than this tree's own sat/kvB.
    """
    mempool = node.mempool
    # Core's own MempoolInfoToJSON (`src/rpc/mempool.cpp:1075-1086`,
    # at bitcoin/bitcoin@58a7869f86) answers several fields beyond these:
    # `usage`, `total_fee`, `limitclustercount`, `limitclustersize`,
    # the deprecated `fullrbf`. Each is backed by something this tree does
    # not carry -- a memory count, a persisted total fee, the cluster
    # options (btclib-org/btclib-node#1383) -- and answering any of them
    # with a placeholder would be exactly the decoration this method's own
    # sparse answer already was. `maxmempool` and `mempoolminfee` are
    # wired in because #294 gave both a real source to read,
    # `unbroadcastcount` because #1421 gave `Mempool.unbroadcast` one,
    # the four fields the relay options set because `Config` holds
    # those options (btclib-org/btclib-node#1497, #1596), and `optimal`
    # because `Mempool.graph` knows it, Core's `DoWork(0)`.
    # btclib-org/btclib-node#305
    #
    # `mempoolminfee` is BTC/kvB, matching Core's own
    # `ValueFromAmount`-converted unit rather than this tree's own
    # sat/kvB used everywhere else a feerate is emitted or read
    # (`Mempool.meets_fee_rate`, BIP133's own `feefilter` wire value,
    # `Config.min_relay_feerate`): a client written against Core's own
    # `getmempoolinfo` reads this field expecting BTC/kvB, and Core
    # defines the unit on this particular surface, `minrelaytxfee` and
    # `incrementalrelayfee` too. BIP133's own wire value is unaffected --
    # `_send_due_feefilters` (`src/btclib_node/download.py`) still sends
    # sat/kvB, because BIP133 says so, not because this tree chose a
    # unit. `maxmempool` needs no such divergence: Core's own field is
    # `m_opts.max_size_bytes`, plain bytes with no amount conversion
    # applied to it either.
    mempoolminfee = max(
        mempool.get_min_fee_rate().sats_per_kvbyte,
        node.config.min_relay_feerate.sats_per_kvbyte,
    )
    return {
        # Core's `GetLoadTried`: whether the load of `mempool.dat` ended
        "loaded": mempool.load_tried,
        "size": mempool.size,
        "bytes": mempool.bytesize,
        "maxmempool": mempool.bytesize_limit,
        "mempoolminfee": btc_amount(mempoolminfee),
        "minrelaytxfee": btc_amount(node.config.min_relay_feerate.sats_per_kvbyte),
        "incrementalrelayfee": btc_amount(
            mempool.incremental_relay_feerate.sats_per_kvbyte
        ),
        "unbroadcastcount": len(mempool.unbroadcast),
        "permitbaremultisig": node.config.permit_bare_multisig,
        "maxdatacarriersize": node.config.max_datacarrier_bytes or 0,
        "optimal": mempool.graph.do_work(0),
    }


def save_mempool(node: Node, conn: RpcConnection, _: list[Any]) -> dict[str, str]:
    """Answer `savemempool`: write `mempool.dat` now, and name it.

    Core's own (`src/rpc/mempool.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag): refused until the load at start has ended, and where the
    file cannot be written. `-persistmempoolv1` decides the version, and
    `-persistmempool=0` does not refuse it.
    """
    mempool = node.mempool
    if not mempool.load_tried:
        raise RpcError(RPCErrorCode.MISC_ERROR, "The mempool was not loaded yet")
    path = node.data_dir / MEMPOOL_FILENAME
    if not dump_mempool(mempool, path, v1=node.config.persist_mempool_v1):
        raise RpcError(RPCErrorCode.MISC_ERROR, "Unable to dump mempool to disk")
    return {"filename": str(path)}


def _import_option(options: dict[str, Any], key: str, *, default: bool) -> bool:
    """Read an `importmempool` option: absent or null is `default`."""
    if options.get(key) is None:
        return default
    return _bool_option(options, key, default=default)


def import_mempool(
    node: Node, conn: RpcConnection, params: list[Any]
) -> Generator[bool, None, dict[str, Any]]:
    """Answer `importmempool`: load a `mempool.dat` into the mempool.

    Core's own (`src/rpc/mempool.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag), in its order: the argument types, a refusal during
    initial block download, then the options, each a bool or null for
    its default, unknown ones ignored. A file that cannot be read whole
    is refused. The file is read as the load goes, a transaction a step,
    as the load at start is, so peers and other calls are served between
    steps. The load ends refused where the node stops meanwhile, as
    Core's ends on `m_interrupt`.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["importmempool"])
    filepath = params[0]
    options = params[1] if len(params) > 1 else None
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(filepath, str):
        mismatches.append((1, "filepath", filepath, "string"))
    if options is not None and not isinstance(options, dict):
        mismatches.append((2, "options", options, "object"))
    if mismatches:
        raise type_errors(*mismatches)
    if node.is_initial_block_download:
        raise RpcError(
            RPCErrorCode.CLIENT_IN_INITIAL_DOWNLOAD,
            "Can only import the mempool after the block download and sync is done.",
        )
    options = {} if options is None else options
    load = load_mempool(
        node,
        Path(filepath),
        use_current_time=_import_option(options, "use_current_time", default=True),
        apply_fee_delta_priority=_import_option(
            options, "apply_fee_delta_priority", default=False
        ),
        apply_unbroadcast_set=_import_option(
            options, "apply_unbroadcast_set", default=False
        ),
    )
    loaded = False
    while not node.terminate_flag.is_set():
        try:
            next(load)
        except StopIteration as done:
            loaded = done.value
            break
        yield True
    load.close()
    if not loaded:
        raise RpcError(
            RPCErrorCode.MISC_ERROR,
            "Unable to import mempool file, see debug.log for details.",
        )
    return {}


# ParseHashType's three names (src/rpc/blockchain.cpp:967-978, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag), and Core's own default
# (`RPCArg::Default{"hash_serialized_3"}`, same file, line 1017)
_TX_OUT_SET_HASH_TYPES = {"hash_serialized_3", "muhash", "none"}
_DEFAULT_TX_OUT_SET_HASH_TYPE = "hash_serialized_3"


def get_tx_out_set_info(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any] | Generator[bool, None, dict[str, Any]]:
    """Answer `gettxoutsetinfo` from `UtxoIndex`'s own running `CoinStats`.

    Core's own default path recomputes every field from a live scan of
    the coins database (`ComputeUTXOStats`, `kernel/coinstats.cpp`) on
    every call, unless `-coinstatsindex` is running, in which case the
    incrementally-maintained `CoinStatsIndex` answers instead
    (`index/coinstatsindex.cpp`) -- `chainstate/muhash.py`'s own module
    docstring is where `CoinStats` is argued as this tree's equivalent
    of that second path. `height`, `bestblock`, `txouts`, `bogosize`,
    `total_amount` and `muhash` come from it, and are Core's own field
    names and units, `total_amount` in BTC through `btc_amount` the way
    `get_mempool_info`'s own `mempoolminfee` already is; `muhash` itself
    is the raw digest bytes reversed before this returns, matching
    `uint256::GetHex()`'s own convention rather than this class's
    `digest` (`chainstate/muhash.py`'s own comment beside
    `is_bip30_unspendable` is where that reversal is confirmed against
    the well-known genesis hash rather than assumed).

    `hash_type: "hash_serialized_3"`, Core's default, is the legacy
    double-SHA256 over every coin, which no accumulator keeps: it is a
    scan of the `utxo-` records, `UtxoIndex.serialized_hash`. The
    chainstate is flushed first, as Core's `ForceFlushStateToDisk` does,
    and the cursor is opened on `Node`'s thread together with the other
    fields, so the answer is one set's. The scan is the generator
    `_serialized_hash_job`, on a thread of its own as Core's is on an
    HTTP worker, and `Node`'s loop serves other requests meanwhile. A
    stored record that does not parse is `RPC_INTERNAL_ERROR` "Unable to
    read UTXO set", as in Core. `hash_type: "none"` answers every field
    but a hash, the way Core's own `CoinStatsHashType::NONE` does.

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

    if hash_type == "hash_serialized_3":
        node.chainstate.flush()
    active_chain = node.chainstate.block_index.active_chain
    utxo_index = node.chainstate.utxo_index
    coin_stats = utxo_index.coin_stats
    result: dict[str, Any] = {
        "height": len(active_chain) - 1,
        "bestblock": active_chain[-1],
        "txouts": coin_stats.transaction_output_count,
        "bogosize": coin_stats.bogo_size,
    }
    total_amount = btc_amount(coin_stats.total_amount)
    if hash_type == "hash_serialized_3":
        return _serialized_hash_job(
            utxo_index, utxo_index.cursor(), node, result, total_amount
        )
    if hash_type == "muhash":
        result["muhash"] = coin_stats.digest[::-1]
    result["total_amount"] = total_amount
    return result


def _serialized_hash_job(
    utxo_index: UtxoIndex,
    cursor: Iterator[tuple[bytes, bytes]],
    node: Node,
    result: dict[str, Any],
    total_amount: RawJSON,
) -> Generator[bool, None, dict[str, Any]]:
    """Hash `cursor`'s coins on a thread of its own, and answer when it ends.

    `result` and `cursor` are taken together on `Node`'s thread, so the
    answer is one set's. The thread touches no state of `Node`'s but
    `terminate_flag`, and the store through `cursor`, whose view is
    fixed. A thread, not `Node.worker_pool`: the pool is processes under
    a GIL build, and a cursor cannot cross into one.

    Core's `interruption_point` runs once per coin and, once the RPC
    server stops, throws `RPC_CLIENT_NOT_CONNECTED` "Shutting down"
    (`RpcInterruptionPoint`, `src/rpc/server.cpp`); so does this one, on
    `terminate_flag`, and the thread ends within a coin. A job dropped
    before its thread ends sets `abandoned`, which interrupts the same
    way, and is joined.
    """
    abandoned = threading.Event()

    def interruption_point() -> None:
        if node.terminate_flag.is_set() or abandoned.is_set():
            raise RpcError(RPCErrorCode.CLIENT_NOT_CONNECTED, "Shutting down")

    outcome: list[bytes | BaseException | None] = []

    def scan() -> None:
        try:
            outcome.append(utxo_index.serialized_hash(cursor, interruption_point))
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    thread = threading.Thread(target=scan, name="gettxoutsetinfo", daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            yield False
    finally:
        abandoned.set()
        thread.join()
    [done] = outcome
    if isinstance(done, BaseException):
        raise done
    if done is None:
        raise RpcError(RPCErrorCode.INTERNAL_ERROR, "Unable to read UTXO set")
    return {**result, "hash_serialized_3": done[::-1], "total_amount": total_amount}


# Core's own literal sentinel, `MEMPOOL_HEIGHT` (`src/txmempool.h:50`,
# at bitcoin/bitcoin@9be056a8a7): a `Coin` built from a mempool transaction's
# own output rather than from the confirmed set carries this height
# instead of a real one, and `gettxout` reads it back to answer
# `confirmations: 0` (`rpc/blockchain.cpp:1243-1247`). Not
# `main.py`'s own `spend_height` -- that is `CalculatePrevHeights`'s
# *different* convention, "assume a mempool parent confirms in the next
# block" for a sequence-lock height, and reads this same sentinel back
# out rather than storing it.
_MEMPOOL_HEIGHT = 0x7FFF_FFFF

# `ExtractDestination`'s address for each `Solver` type that has one
# (`src/addresstype.cpp:49-105`, at bitcoin/bitcoin@9be056a8a7): the
# `Solver` solution is the hash or program, and `EncodeDestination`
# spells it base58 or bech32(m). "pubkey" answers none here, as
# `ScriptToUniv`'s own `type != TxoutType::PUBKEY` says, and "multisig",
# "nulldata" and "nonstandard" have no destination at all. "anchor" is
# `PayToAnchor`, which `EncodeDestination` spells as the version 1
# program `4e73`.
_ANCHOR_PROGRAM = bytes.fromhex("4e73")


def _address(script_type: str, solutions: list[bytes], network: str) -> str | None:
    """Answer `ScriptToUniv`'s `address` for a solved script, or None."""
    if script_type == "pubkeyhash":
        return b58.address_from_h160("p2pkh", solutions[0], network)
    if script_type == "scripthash":
        return b58.address_from_h160("p2sh", solutions[0], network)
    # the witness version and program of each type that has one
    witness: tuple[int, bytes] | None = None
    if script_type in {"witness_v0_keyhash", "witness_v0_scripthash"}:
        witness = (0, solutions[0])
    elif script_type == "witness_v1_taproot":
        witness = (1, solutions[0])
    elif script_type == "anchor":
        witness = (1, _ANCHOR_PROGRAM)
    elif script_type == "witness_unknown":
        witness = (solutions[0][0], solutions[1])
    if witness is None:
        return None
    return b32.address_from_witness(*witness, network)


def _script_pub_key_dict(script: bytes, network: str) -> dict[str, Any]:
    """Answer a `scriptPubKey` as Core's `ScriptToUniv` does.

    `ScriptToUniv` (`src/core_io.cpp:409-428`, at
    bitcoin/bitcoin@9be056a8a7) nests `asm`, `desc`, `hex`, `address` and
    `type`, in that order, inside `scriptPubKey` itself, which is what
    `gettxout` and every transaction's `vout` answer here (`TxOut.to_dict` keeps
    `type`, `addresses` and `network` beside it instead). The type is
    `GetTxnOutputType`'s name from `solver`, the address is spelled for
    `network`, and `address` is answered only where one exists. `desc` is
    `InferDescriptor` handed `DUMMY_SIGNING_PROVIDER`, which knows no key
    or script: `infer_descriptor` with an empty `Provider`.
    """
    script_type, solutions = solver(script)
    address = _address(script_type, solutions, network)
    out: dict[str, Any] = {
        "asm": script_to_dict(script)["asm"],
        "desc": infer_descriptor(script, Provider(), network),
        "hex": script.hex(),
    }
    if address is not None:
        out["address"] = address
    out["type"] = script_type
    return out


def _vin_to_univ(
    tx: Tx, network: str, undo: list[Coin] | None, *, prevout: bool
) -> list[dict[str, Any]]:
    """Answer a transaction's `vin` as `TxToUniv` does.

    An ordinary input is `txid`, `vout`, `scriptSig` (`asm` and `hex`)
    and `sequence`; a coinbase's own is `coinbase`, the script's hex,
    where `txid`, `vout` and `scriptSig` have no meaning. `txinwitness`
    is answered only where the witness stack is not empty
    (`src/core_io.cpp:451-493`, at bitcoin/bitcoin@9be056a8a7), whereas
    `TxIn.to_dict` answers `prev_out`, `scriptSig`, `sequence` and
    `txinwitness` for every input, an empty list included. Where `undo`
    holds the coin each input spent and `prevout` asks for it, `prevout`
    follows the witness: `generated`, `height`, `value` and the coin's
    `scriptPubKey`.
    """
    vin: list[dict[str, Any]] = []
    for i, tx_in in enumerate(tx.vin):
        entry: dict[str, Any] = {}
        if tx.is_coinbase:
            entry["coinbase"] = tx_in.script_sig.hex()
        else:
            entry["txid"] = tx_in.prev_out.tx_id.hex()
            entry["vout"] = tx_in.prev_out.vout
            entry["scriptSig"] = script_to_dict(tx_in.script_sig)
        stack = tx_in.script_witness.stack
        if stack:
            entry["txinwitness"] = [item.hex() for item in stack]
        if undo is not None and prevout:
            coin = undo[i]
            entry["prevout"] = {
                "generated": coin.is_coinbase,
                "height": coin.height,
                "value": btc_amount(coin.tx_out.value),
                "scriptPubKey": _script_pub_key_dict(
                    coin.tx_out.script_pub_key.script, network
                ),
            }
        entry["sequence"] = tx_in.sequence
        vin.append(entry)
    return vin


def _vout_to_univ(tx: Tx, network: str) -> list[dict[str, Any]]:
    """Answer a transaction's `vout` as `TxToUniv` does, for `network`.

    Each entry is `value`, `n` and `scriptPubKey`, in that order
    (`src/core_io.cpp:495-519`, at bitcoin/bitcoin@9be056a8a7).
    `ischange`, which `TxToUniv` adds for a wallet-owned output, has no
    wallet to ask here.
    """
    return [
        {
            "value": btc_amount(tx_out.value),
            "n": n,
            "scriptPubKey": _script_pub_key_dict(tx_out.script_pub_key.script, network),
        }
        for n, tx_out in enumerate(tx.vout)
    ]


def _tx_to_univ(
    tx: Tx,
    network: str,
    *,
    include_hex: bool,
    undo: list[Coin] | None = None,
    prevout: bool = False,
) -> dict[str, Any]:
    """Answer a transaction as Core's `TxToUniv` does, addresses for `network`.

    `src/core_io.cpp:430-534`, at bitcoin/bitcoin@9be056a8a7, in its
    own key order. Every field is written here rather than taken from
    `Tx.to_dict`, whose `vin` and `vout` are btclib's own shape and
    whose `value` cannot render a negative amount, which a decoded
    output can carry. `hash` is the witness hash, `size` the size with
    the witness and `vsize` the weight in virtual bytes.

    `undo` is the coin each input spent, which `TxToUniv` takes as
    `txundo`: where it is given, `fee` is those values less the outputs',
    and `prevout` adds each input's coin to `vin`. A coinbase, or a
    transaction whose undo data is not held, is passed none.
    """
    out: dict[str, Any] = {
        "txid": tx.id.hex(),
        "hash": tx.hash.hex(),
        "version": tx.version,
        "size": tx.size,
        "vsize": tx.vsize,
        "weight": tx.weight,
        "locktime": tx.lock_time,
        "vin": _vin_to_univ(tx, network, undo, prevout=prevout),
        "vout": _vout_to_univ(tx, network),
    }
    if undo is not None:
        spent = sum(coin.tx_out.value for coin in undo)
        out["fee"] = btc_amount(spent - sum(tx_out.value for tx_out in tx.vout))
    if include_hex:
        out["hex"] = tx.serialize(include_witness=True, check_validity=False).hex()
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
    txid = parse_hash_v("txid", txid_param)
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
        "value": btc_amount(coin.tx_out.value),
        "scriptPubKey": _script_pub_key_dict(
            coin.tx_out.script_pub_key.script, node.chain.name
        ),
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
    # RPCArg::Default{false}: src/rpc/mempool.cpp:659-660,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag. Both are
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
            tx.id.hex(): _mempool_entry_json(node.mempool, wtxid)
            for wtxid, tx in node.mempool.transactions.items()
        }

    txids = [txid.hex() for txid in node.mempool.txid_index]
    if not include_sequence:
        # MempoolToJSON's plain-array answer, src/rpc/mempool.cpp:624-634
        return txids
    # MempoolToJSON's other shape, src/rpc/mempool.cpp:635-639
    return {"txids": txids, "mempool_sequence": node.mempool.sequence}


def get_orphan_txs(
    node: Node, conn: RpcConnection, params: list[Any]
) -> list[str] | list[dict[str, Any]]:
    """Answer `getorphantxs`: the orphanage, in Core's three verbosities.

    Core's hidden RPC (`src/rpc/mempool.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag): the txids at verbosity 0, an object each with its
    `txid`, `wtxid`, `bytes`, `vsize`, `weight` and the ids of the peers it
    is `from` at 1, and the `hex` too at 2, in the order of the wtxids as
    Core holds them. Any other number is `RPC_INVALID_PARAMETER`, and a bool
    is refused (`_parse_verbosity`). `vsize` is `GetVirtualTransactionSize`
    without sigops, as `getrawmempool`'s is not.
    """
    verbosity = _parse_verbosity(params, 0, default=0, allow_bool=False)
    orphans = node.download_manager.orphanage.get_orphan_transactions()
    if verbosity == 0:
        return [tx.id.hex() for tx, _ in orphans]
    if verbosity not in (1, 2):
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER, f"Invalid verbosity value {verbosity}"
        )
    answer: list[dict[str, Any]] = []
    for tx, announcers in orphans:
        entry: dict[str, Any] = {
            "txid": tx.id.hex(),
            "wtxid": tx.hash.hex(),
            "bytes": tx.size,
            "vsize": tx.vsize,
            "weight": tx.weight,
            "from": announcers,
        }
        if verbosity == 2:  # noqa: PLR2004
            entry["hex"] = tx.serialize(include_witness=True).hex()
        answer.append(entry)
    return answer


def _mempool_entry_json(mempool: Mempool, wtxid: bytes) -> dict[str, Any]:
    """Return one entry in `getmempoolentry`'s own shape.

    The shape `getmempoolancestors` and `getmempooldescendants` give each
    transaction when `verbose` is true, too.
    """
    entry = mempool.entry(wtxid)
    return {
        # `entryToJSON`'s three sizes (`src/rpc/mempool.cpp`, at
        # bitcoin/bitcoin@aef8a04966). btclib-org/btclib-node#1757
        "vsize_adjusted": entry.vsize,
        "vsize": entry.vsize,
        "vsize_bip141": mempool.transactions[wtxid].vsize,
        "weight": entry.weight,
        "time": entry.time,
        "height": entry.height,
        "descendantcount": entry.descendant_count,
        "descendantsize": entry.descendant_size,
        "ancestorcount": entry.ancestor_count,
        "ancestorsize": entry.ancestor_size,
        "wtxid": entry.wtxid,
        "chunkweight": entry.chunk_weight,
        "fees": {
            "base": btc_amount(entry.fee),
            "modified": btc_amount(entry.modified_fee),
            "ancestor": btc_amount(entry.ancestor_fees),
            "descendant": btc_amount(entry.descendant_fees),
            "chunk": btc_amount(entry.chunk_fee),
        },
        "depends": entry.depends,
        "spentby": entry.spent_by,
        "bip125-replaceable": entry.bip125_replaceable,
        "unbroadcast": entry.unbroadcast,
    }


def get_mempool_entry(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `getmempoolentry`: one held transaction's own accounting.

    Core's own shape (`entryToJSON`, `src/rpc/mempool.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), with Core 32's
    `vsize_adjusted` and `vsize_bip141` (at bitcoin/bitcoin@aef8a04966,
    btclib-org/btclib-node#1757). A `txid` this mempool does not hold is Core's
    own `RPC_INVALID_ADDRESS_OR_KEY`, "Transaction not in mempool".
    btclib-org/btclib-node#1397
    """
    if not params:
        # the whole help, as get_block_header's own missing-argument case
        # answers: RPCMethod::HandleRequest's HelpResult, RPC_MISC_ERROR
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["getmempoolentry"])
    if not isinstance(params[0], str):
        raise type_error(1, "txid", params[0], "string")
    txid = parse_hash_v("txid", params[0])
    mempool = node.mempool
    wtxid = mempool.txid_index.get(txid)
    if wtxid is None:
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Transaction not in mempool"
        )
    return _mempool_entry_json(mempool, wtxid)


def get_mempool_cluster(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `getmempoolcluster`: the cluster of a held transaction, by chunk.

    Core's `clusterToJSON` (`src/rpc/mempool.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the sigop-adjusted weight
    and the count of the whole cluster, and each chunk's modified fee,
    weight and txids, in the order a block takes them. A `txid` this
    mempool does not hold is "Transaction not in mempool".
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["getmempoolcluster"])
    if not isinstance(params[0], str):
        raise type_error(1, "txid", params[0], "string")
    txid = parse_hash_v("txid", params[0])
    mempool = node.mempool
    wtxid = mempool.txid_index.get(txid)
    if wtxid is None:
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Transaction not in mempool"
        )
    chunks = mempool.graph.chunks(wtxid)
    return {
        "clusterweight": sum(chunk.feerate.size for chunk in chunks),
        "txcount": sum(len(chunk.refs) for chunk in chunks),
        "chunks": [
            {
                "chunkfee": btc_amount(chunk.feerate.fee),
                "chunkweight": chunk.feerate.size,
                "txs": [mempool.txids[w].hex() for w in chunk.refs],
            }
            for chunk in chunks
        ],
    }


def get_mempool_feerate_diagram(
    node: Node, conn: RpcConnection, _: list[Any]
) -> list[dict[str, Any]]:
    """Answer `getmempoolfeeratediagram`, Core's hidden RPC.

    `GetFeerateDiagram` (`src/txmempool.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag): from zero, the running sigop-adjusted weight and
    modified fee after each chunk, in the order a block takes them.
    """
    points = [{"weight": 0, "fee": btc_amount(0)}]
    weight = fee = 0
    for chunk in node.mempool.graph.mining_order():
        weight += chunk.feerate.size
        fee += chunk.feerate.fee
        points.append({"weight": weight, "fee": btc_amount(fee)})
    return points


def _mempool_relatives(
    node: Node, params: list[Any], *, method: str, ancestors: bool
) -> dict[str, Any] | list[str]:
    """Answer `getmempoolancestors` or `getmempooldescendants`.

    Core's two RPCs (`src/rpc/mempool.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag): the transactions in the mempool that `txid` spends
    from, transitively, or that spend from it, not `txid` itself. A txid
    array unless `verbose`, then an object of each one's `getmempoolentry`
    answer, keyed by txid. Core checks every argument's JSON type, and
    reports every mismatch, before the body runs. btclib-org/btclib-node#1501
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT[method])
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(params[0], str):
        mismatches.append((1, "txid", params[0], "string"))
    verbose_mismatch = bool_mismatch(params, 1, name="verbose")
    if verbose_mismatch is not None:
        mismatches.append(verbose_mismatch)
    if mismatches:
        raise type_errors(*mismatches)
    verbose = bool_param(params, 1, name="verbose", default=False)
    txid = parse_hash_v("txid", params[0])
    mempool = node.mempool
    wtxid = mempool.txid_index.get(txid)
    if wtxid is None:
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Transaction not in mempool"
        )
    related = mempool.related(wtxid, ancestors=ancestors)
    if not verbose:
        return [mempool.transactions[w].id.hex() for w in related]
    return {
        mempool.transactions[w].id.hex(): _mempool_entry_json(mempool, w)
        for w in related
    }


def get_mempool_ancestors(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any] | list[str]:
    """Answer `getmempoolancestors`, `_mempool_relatives`' ancestors."""
    return _mempool_relatives(
        node, params, method="getmempoolancestors", ancestors=True
    )


def get_mempool_descendants(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any] | list[str]:
    """Answer `getmempooldescendants`, `_mempool_relatives`' descendants."""
    return _mempool_relatives(
        node, params, method="getmempooldescendants", ancestors=False
    )


def prioritise_transaction(node: Node, conn: RpcConnection, params: list[Any]) -> bool:
    """Answer `prioritisetransaction`: add a fee delta to a transaction.

    Core's own (`src/rpc/mining.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag), in its order: the argument types, `txid`, `dummy` as a
    double (`get_real`, which refuses `1e400`), `fee_delta` as an `int64`,
    then `dummy` again: refused with `RPC_INVALID_PARAMETER` unless it is
    null, omitted or zero. A transaction held with a dust output is
    refused where the mempool requires standard transactions, since one
    that pays a fee is not allowed to enter with it. The delta is kept for
    a transaction not held, and applied once it is (`Mempool.prioritise`).
    btclib-org/btclib-node#1502
    """
    if len(params) != len(arg_names["prioritisetransaction"]):
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["prioritisetransaction"])
    txid_param, dummy, fee_delta = params
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(txid_param, str):
        mismatches.append((1, "txid", txid_param, "string"))
    dummy_mismatch = _number_mismatch(params, 1, name="dummy")
    if dummy_mismatch is not None:
        mismatches.append(dummy_mismatch)
    # `fee_delta` is required, so a null is a mismatch where `dummy`'s is not
    if fee_delta is None or _number_mismatch(params, 2, name="fee_delta"):
        mismatches.append((3, "fee_delta", fee_delta, "number"))
    if mismatches:
        raise type_errors(*mismatches)
    txid = parse_hash_v("txid", txid_param)
    if dummy is not None:
        get_real(dummy)
    amount = _integer(fee_delta, -_INT64_BOUND, _INT64_BOUND - 1)
    if dummy is not None and dummy != 0:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Priority is no longer supported, dummy argument to "
            "prioritisetransaction must be 0.",
        )
    config = node.config
    tx = node.mempool.get_tx(txid)
    if (
        config.require_standard
        and tx is not None
        and dust_outputs(tx, dust_relay_fee=config.dust_relay_feerate)
    ):
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Priority is not supported for transactions with dust outputs.",
        )
    node.mempool.prioritise(txid, amount)
    return True


def get_prioritised_transactions(
    node: Node, conn: RpcConnection, _: list[Any]
) -> dict[str, dict[str, Any]]:
    """Answer `getprioritisedtransactions`: every fee delta, held or not.

    Core's own (`src/rpc/mining.cpp`, same tag): amounts in satoshi, keyed
    by txid in the order of the txid's internal bytes, and a `modified_fee`
    only for a transaction in the mempool. btclib-org/btclib-node#1502
    """
    answer: dict[str, dict[str, Any]] = {}
    for txid, delta, modified_fee in node.mempool.prioritised():
        entry: dict[str, Any] = {
            "fee_delta": delta,
            "in_mempool": modified_fee is not None,
        }
        if modified_fee is not None:
            entry["modified_fee"] = modified_fee
        answer[txid.hex()] = entry
    return answer


def _check_object(
    obj: dict[str, Any], fields: dict[str, str], *, allow_null: bool
) -> None:
    """Core's `RPCTypeCheckObj` with `fStrict`: each named field, then no other.

    `src/rpc/util.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag.
    `fields` maps a name to the JSON type it must have, named as univalue
    does. A missing field is null: refused as "Missing" unless
    `allow_null`, where it and an explicit null pass. Every error is
    `RPC_TYPE_ERROR`.
    """
    for key, expected in fields.items():
        value = obj.get(key)
        if value is None:
            if allow_null:
                continue
            raise RpcError(RPCErrorCode.TYPE_ERROR, f"Missing {key}")
        if json_type_name(value) != expected:
            raise RpcError(
                RPCErrorCode.TYPE_ERROR,
                f"JSON value of type {json_type_name(value)} for field {key} "
                f"is not of expected type {expected}",
            )
    for key in obj:
        if key not in fields:
            raise RpcError(RPCErrorCode.TYPE_ERROR, f"Unexpected key {key}")


def _bool_option(options: dict[str, Any], key: str, *, default: bool) -> bool:
    """Read `key` as `get_bool` does: absent is `default`, null is refused."""
    if key not in options:
        return default
    value = options[key]
    if not isinstance(value, bool):
        raise RpcError(
            RPCErrorCode.TYPE_ERROR,
            f"JSON value of type {json_type_name(value)} is not of expected type bool",
        )
    return value


_INT_MAX = 2**31 - 1


def _spending_prevout(output: object) -> tuple[bytes, int]:
    """Check one `gettxspendingprevout` outpoint, Core's checks in Core's order.

    An object of exactly `txid`, a string, and `vout`, a number. `vout` is
    read by `UniValue::getInt<int>`: a float, or an integer outside a C
    `int`, is `RPC_MISC_ERROR`, and a negative one `RPC_INVALID_PARAMETER`.
    """
    if not isinstance(output, dict):
        raise RpcError(
            RPCErrorCode.TYPE_ERROR,
            f"JSON value of type {json_type_name(output)} "
            "is not of expected type object",
        )
    _check_object(output, {"txid": "string", "vout": "number"}, allow_null=False)
    txid = parse_hash_v("txid", output["txid"])
    vout = output["vout"]
    if isinstance(vout, float) or not -_INT_MAX - 1 <= vout <= _INT_MAX:
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    if vout < 0:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER, "Invalid parameter, vout cannot be negative"
        )
    return txid, vout


def get_tx_spending_prevout(
    node: Node, conn: RpcConnection, params: list[Any]
) -> list[dict[str, Any]]:
    """Answer `gettxspendingprevout`: the mempool transaction spending each.

    Core's RPC (`src/rpc/mempool.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag), without its `-txospenderindex`, which this node does not
    have: the answer is the mempool's alone, so `blockhash` is never in
    it. Each answer is the outpoint's own object, as given, with
    `spendingtxid` added where a mempool transaction spends it, and
    `spendingtx` too under `return_spending_tx`. Core's `mempool_only`
    defaults to true where there is no index, and false asks for the
    index: an outpoint the mempool does not spend is then refused, as Core
    refuses it with the index unavailable. btclib-org/btclib-node#1501
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["gettxspendingprevout"])
    outputs = params[0]
    options = params[1] if len(params) > 1 else None
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(outputs, list):
        mismatches.append((1, "outputs", outputs, "array"))
    if options is not None and not isinstance(options, dict):
        mismatches.append((2, "options", options, "object"))
    if mismatches:
        raise type_errors(*mismatches)
    if not outputs:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER, "Invalid parameter, outputs are missing"
        )
    options = {} if options is None else options
    _check_object(
        options,
        {"mempool_only": "bool", "return_spending_tx": "bool"},
        allow_null=True,
    )
    mempool_only = _bool_option(options, "mempool_only", default=True)
    return_spending_tx = _bool_option(options, "return_spending_tx", default=False)
    outpoints = [_spending_prevout(output) for output in outputs]

    mempool = node.mempool
    answer: list[dict[str, Any]] = []
    for output, (txid, vout) in zip(outputs, outpoints, strict=True):
        # every pair as given, a key named twice included: Core copies the
        # `UniValue`, which holds both
        pairs = list(output.items())
        wtxid = mempool.outpoint_spender.get((txid, vout))
        if wtxid is not None:
            spender = mempool.transactions[wtxid]
            pairs.append(("spendingtxid", spender.id.hex()))
            if return_spending_tx:
                hex_tx = spender.serialize(include_witness=True).hex()
                pairs.append(("spendingtx", hex_tx))
        elif not mempool_only:
            raise RpcError(
                RPCErrorCode.MISC_ERROR,
                f"No spending tx for the outpoint {txid.hex()}:{vout} in mempool, "
                "and txospenderindex is unavailable.",
            )
        answer.append(JsonObject(pairs))
    return answer


def _decode_txid(txid_arg: str) -> bytes:
    """Hex-decode `getrawtransaction`'s own `txid`, already type-checked.

    `ParseHashV(request.params[0], "parameter 1")` (`rpc/rawtransaction.cpp
    :304`, at bitcoin/bitcoin@9be056a8a7) -- Core's own name for this
    argument here, not "txid".
    """
    return parse_hash_v("parameter 1", txid_arg)


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
    return parse_hash_v("parameter 3", params[2])


class _FoundTransaction(NamedTuple):
    """A transaction and where `_find_transaction` found it.

    `height` and `block` are None for a mempool transaction, whose
    `position` is 0.
    """

    tx: Tx
    height: int | None
    block: Block | None
    position: int


def _find_transaction(
    node: Node, txid: bytes, block_hash: bytes | None
) -> _FoundTransaction:
    """Return the transaction `txid` names, from the mempool or the block."""
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
        return _FoundTransaction(tx, None, None, 0)

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
    position = next((i for i, t in enumerate(block.transactions) if t.id == txid), None)
    if position is None:
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY,
            "No such transaction found in the provided block. Use "
            "gettransaction for wallet transactions.",
        )
    return _FoundTransaction(
        block.transactions[position], block_info.index, block, position
    )


def _raw_transaction_json(
    node: Node, found: _FoundTransaction, verbosity: int
) -> dict[str, Any]:
    """Answer `getrawtransaction`'s JSON at verbosity 1 or 2, `TxToJSON`'s way.

    Core's key order is `in_active_chain` first, then `TxToUniv`'s keys,
    then the block's (`src/rpc/rawtransaction.cpp`, at
    bitcoin/bitcoin@9be056a8a7). Verbosity 2 adds `fee` and each input's
    `prevout` from the block's undo data; a coinbase has none, and for
    any other transaction in a block Core answers a patch it cannot read
    as an error, not as no `fee`. A block off the active chain is 0
    confirmations and names no time.
    """
    tx, block_height, block, position = found
    out: dict[str, Any] = {}
    active_chain = node.chainstate.block_index.active_chain
    on_active_chain = False
    if block is not None and block_height is not None:
        on_active_chain = (
            block_height < len(active_chain)
            and active_chain[block_height] == block.header.hash
        )
        out["in_active_chain"] = on_active_chain
    undo: list[Coin] | None = None
    if verbosity >= 2 and block is not None and not tx.is_coinbase:  # noqa: PLR2004
        block_undo = _block_undo(node, block.header.hash, block)
        if block_undo is None:
            raise RpcError(RPCErrorCode.INTERNAL_ERROR, _UNDO_UNREADABLE)
        undo = block_undo[position]
    out.update(
        _tx_to_univ(
            tx, node.chain.name, include_hex=True, undo=undo, prevout=undo is not None
        )
    )
    if block is not None and block_height is not None:
        out["blockhash"] = block.header.hash.hex()
        if on_active_chain:
            out["confirmations"] = len(active_chain) - block_height
            out["time"] = block_time(block.header)
            out["blocktime"] = block_time(block.header)
        else:
            out["confirmations"] = 0
    return out


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
        # (src/rpc/server.cpp:887)
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["getrawtransaction"])
    # txid and blockhash are checked, and every mismatch named, before
    # any value-level check below runs (the genesis exception, the two
    # hex decodes), the way `disconnect_node` above already does for its
    # own two declared arguments (`type_errors`' own docstring). Each is
    # declared RPCArg::Type::STR_HEX, type-checked before the handler
    # body runs; the verbosity is not (`_parse_verbosity`)
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(params[0], str):
        mismatches.append((1, "txid", params[0], "string"))
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
    # `skip_type_check` (src/rpc/rawtransaction.cpp:233), so the type of
    # this argument is read here, after the genesis exception, as
    # `ParseVerbosity` reads it (`_parse_verbosity`)
    verbosity = _parse_verbosity(params, 1, default=0)
    block_hash = _decode_optional_block_hash(params)
    found = _find_transaction(node, txid, block_hash)

    if verbosity <= 0:
        return found.tx.serialize(include_witness=True).hex()

    return _raw_transaction_json(node, found, verbosity)


def _decode_hex_tx(rawtx: str, *, iswitness: bool | None = True) -> Tx:
    """Decode `rawtx` as Core's `DecodeHexTx` does.

    `iswitness` is `_decode_tx`'s. The default, `True`, is `DecodeHexTx`'s
    own: the extended reading alone.

    `is_hex` refuses any character that is not a hex digit -- a space
    included -- and an odd or zero length, ahead of `ParseHex`
    (`src/core_io.cpp`, same tag).

    `check_validity=False`: `DecodeHexTx` decodes the wire encoding
    alone and never asks whether the transaction it decoded is one
    Core would accept -- `CheckTransaction` is a later, separate step
    of `PreChecks`, and this node's own equivalent is
    `_check_transaction` below, run only by the two callers that need
    it. Answering a well-formed but structurally invalid transaction
    with "TX decode failed" was btclib-org/btclib-node#1375.
    """
    if not is_hex(rawtx):
        err_msg = f"invalid hex string: {rawtx!r}"
        raise BTClibValueError(err_msg)
    return _decode_tx(bytes.fromhex(rawtx), iswitness=iswitness)


# Core's `MAX_OPCODE`, `OP_NOP10` (`src/script/script.h`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the highest opcode
# `CScript::HasValidOps` accepts. btclib-org/btclib-node#1458
_MAX_OPCODE = 0xB9
# the octets that follow OP_PUSHDATA1, OP_PUSHDATA2 and OP_PUSHDATA4
# for the length of the push
_PUSHDATA_LENGTH_OCTETS = {0x4C: 1, 0x4D: 2, 0x4E: 4}


def _has_valid_ops(script: bytes) -> bool:
    """Answer `CScript::HasValidOps` (`src/script/script.cpp`, same tag).

    Every op reads, none is above `_MAX_OPCODE`, and no push carries more
    than `MAX_SCRIPT_ELEMENT_SIZE` bytes. `op_code_spans` stops where an
    op cannot be read, so a script that reads whole is one whose last
    span ends at its end.
    """
    end = 0
    for op_code, start, end in op_code_spans(script):
        header = 1 + _PUSHDATA_LENGTH_OCTETS.get(op_code, 0)
        if op_code > _MAX_OPCODE or end - start - header > MAX_SCRIPT_ELEMENT_SIZE:
            return False
    return end == len(script)


def _check_tx_scripts_sanity(tx: Tx) -> bool:
    """Answer `CheckTxScriptsSanity` (`src/core_io.cpp`, same tag).

    Each output script, and each input script unless `tx` is a
    coinbase, has valid ops and at most `MAX_SCRIPT_SIZE` bytes.
    """
    scripts = [tx_out.script_pub_key.script for tx_out in tx.vout]
    if not tx.is_coinbase:
        scripts += [tx_in.script_sig for tx_in in tx.vin]
    return all(
        len(script) <= MAX_SCRIPT_SIZE and _has_valid_ops(script) for script in scripts
    )


def _decode_tx(data: bytes, *, iswitness: bool | None) -> Tx:
    """Decode `data` as Core's `DecodeTx` does (`src/core_io.cpp`, same tag).

    `iswitness` picks the readings as the handler does: `None` tries
    both, `True` the extended alone, `False` the legacy alone. The
    extended reading is `Tx.parse`, the legacy one is
    `Tx.parse_without_witness`; each is kept only if it consumes `data`
    whole, which both refuse otherwise. The first reading to pass
    `_check_tx_scripts_sanity` wins, extended before legacy; where none
    does, the first to read wins in the same order. `BTClibValueError`
    where neither reads.
    btclib-org/btclib-node#1458
    """
    extended = legacy = None
    if iswitness is not False:
        with contextlib.suppress(BTClibException):
            extended = Tx.parse(data, check_validity=False)
    if extended is not None and _check_tx_scripts_sanity(extended):
        return extended
    if iswitness is not True:
        with contextlib.suppress(BTClibException):
            legacy = Tx.parse_without_witness(data, check_validity=False)
    if legacy is not None and _check_tx_scripts_sanity(legacy):
        return legacy
    if extended is not None:
        return extended
    if legacy is not None:
        return legacy
    err_msg = "transaction decodes under neither reading"
    raise BTClibValueError(err_msg)


# Core's own `MAX_MONEY` (`src/consensus/amount.h`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): 21e6 BTC in satoshi, the
# same bound btclib's own `valid_sats_amount` enforces under a private
# name (`btclib/amount.py`) -- read again here only to tell
# `bad-txns-vout-negative` from `bad-txns-vout-toolarge` apart, one
# shared btclib message answering for both. btclib-org/btclib-node#1375
_MAX_SATOSHI = 21_000_000 * 100_000_000


# Every exact `Tx.assert_valid` message this translates one for one,
# barring the amount-range messages `_amount_reject_reason` below
# disambiguates and the oversize one `_reject_reason` matches by prefix,
# both carrying a value `Tx.assert_valid` computed and this dict cannot
# spell in advance.
_EXACT_REJECT_REASONS = {
    "Missing inputs": "bad-txns-vin-empty",
    "Missing outputs": "bad-txns-vout-empty",
    "the same outpoint is spent twice": "bad-txns-inputs-duplicate",
    "Invalid coinbase script size": "bad-cb-length",
    "coinbase input in a non-coinbase transaction": "bad-txns-prevout-null",
}

# `Tx.assert_valid`'s own oversize message (`btclib/tx/tx.py`, since
# btclib 2026.9.30, btclib-org/btclib#2420): "invalid transaction size:
# {size} * {WITNESS_SCALE_FACTOR} > {MAX_BLOCK_WEIGHT}", the last two
# numbers fixed but the first the transaction's own stripped size, so
# this is a prefix rather than an entry of `_EXACT_REJECT_REASONS`
# above. btclib-org/btclib-node#1447
_OVERSIZE_MESSAGE_PREFIX = "invalid transaction size:"


def _amount_reject_reason(tx: Tx, message: str) -> str | None:
    """Disambiguate the one btclib message shared by two of Core's own reasons.

    `valid_sats_amount` (`btclib/amount.py`) raises the identical
    "invalid satoshi amount" text for a negative value and for one
    above `MAX_MONEY`, where Core's own `CheckTransaction` tells them
    apart as `bad-txns-vout-negative` and `bad-txns-vout-toolarge`; this
    reads the field itself to say which. `None` where `message` is
    neither this nor the total-amount check's own message.
    """
    if message.startswith("invalid total output amount"):
        return "bad-txns-txouttotal-toolarge"
    if message.startswith("invalid satoshi amount"):
        for tx_out in tx.vout:
            if tx_out.value < 0:
                return "bad-txns-vout-negative"
            if tx_out.value > _MAX_SATOSHI:
                return "bad-txns-vout-toolarge"
    return None


def _reject_reason(tx: Tx, error: BTClibException) -> str:
    """Name `error`, a `Tx.assert_valid` refusal, Core's own way.

    `Tx.assert_valid`'s own docstring already claims it checks what
    Core's `CheckTransaction` checks of a lone transaction
    (`src/consensus/tx_check.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag), and since btclib 2026.9.30 in the same order too:
    vin-empty, vout-empty, the size, the outputs, the duplicate inputs,
    the coinbase/prevout-null rule last (btclib-org/btclib#2417,
    btclib-org/btclib#2422). This does not run any rule a second time
    -- `error` is already btclib's own verdict, whichever rule
    `assert_valid` raised first in its own order -- it only answers
    which of Core's own reject reasons that message names, relying on
    that order matching `CheckTransaction`'s for a transaction that
    violates more than one rule at once to answer the same one of the
    two Core would: two inputs, one of them the null outpoint, and no
    outputs now answers `bad-txns-vout-empty` here, as it does in Core,
    `Tx.assert_valid` having reached the empty `vout` before the
    coinbase/prevout-null check reads either input
    (btclib-org/btclib-node#1375).

    Every message `assert_valid` can raise for a `Tx` built by
    `_decode_hex_tx`'s own `check_validity=False` is named below,
    barring the field-range checks on `version`/`lock_time`: `Tx.parse`
    always builds a 4-byte-clean value for both, so neither ever reaches
    this. A message this does not recognize is `error` itself, re-raised
    -- this tree's own equivalent of Core's `Assume(false)` for a
    consensus check the engine disagrees with itself about.
    """
    message = str(error)
    if message in _EXACT_REJECT_REASONS:
        return _EXACT_REJECT_REASONS[message]
    if message.startswith(_OVERSIZE_MESSAGE_PREFIX):
        return "bad-txns-oversize"
    reason = _amount_reject_reason(tx, message)
    if reason is not None:
        return reason
    raise error


def _check_transaction(tx: Tx) -> str | None:
    """Return Core's own `CheckTransaction` reject reason for `tx`, or None.

    `tx.assert_valid()` is the rule, the same one this node already
    trusts to judge a transaction, and this only translates a refusal
    into the reason string Core's own JSON-RPC answers name -- it never
    decides validity on its own account, `ARCHITECTURE.md`'s own
    *What is delegated, and what is not*. `_reject_reason`'s own
    docstring is where a transaction violating more than one of
    `CheckTransaction`'s rules at once is argued: since btclib
    2026.9.30, `assert_valid`'s own check order matches
    `CheckTransaction`'s, so the rule it raises first is the one Core
    would too (btclib-org/btclib#2417, btclib-org/btclib#2422).

    Core's `bad-txns-oversize` -- a transaction whose own non-witness
    size alone already exceeds a block's weight limit -- used to have
    no `Tx.assert_valid` check behind it to translate, and a
    transaction breaking only that rule was answered as one this node
    accepted rather than refused (btclib-org/btclib-node#1447). Closed
    by the same 2026.9.30 that fixed the check order:
    `Tx.assert_valid` now raises its own message for it, ahead of the
    per-output amount checks in `CheckTransaction`'s own order
    (btclib-org/btclib#2420), and `_reject_reason` matches that message
    by its fixed prefix, the transaction's own stripped size being the
    one part of it that varies.
    """
    try:
        tx.assert_valid()
    except (BTClibValueError, BTClibTypeError) as error:
        return _reject_reason(tx, error)
    return None


def decode_raw_transaction(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any]:
    """Answer `decoderawtransaction`: decode `hexstring`, answer its JSON.

    No chain or mempool lookup, and no `CheckTransaction`: Core's own
    handler (`src/rpc/rawtransaction.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag) calls `DecodeHexTx` and `TxToUniv` alone, so a
    well-formed but structurally invalid transaction is answered here
    too, the same as it decodes and displays there --
    btclib-org/btclib-node#1398, and unlike `sendrawtransaction` and
    `testmempoolaccept` above, which run `_check_transaction`.
    The answer is `_tx_to_univ`'s, the one `get_raw_transaction`'s own
    verbose answer builds on, minus the `hex` that call adds and the
    `blockhash` it sometimes does: Core's own `TxToUniv` call here
    passes `include_hex=false` and a null `block_hash`, neither of
    which this RPC is given a block or asked to serialize.

    `iswitness` picks the readings `_decode_tx` tries, as Core's handler
    does (`src/rpc/rawtransaction.cpp`, same tag).
    btclib-org/btclib-node#1458
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
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["decoderawtransaction"])
    hexstring = params[0]
    if not isinstance(hexstring, str):
        # hexstring is declared RPCArg::Type::STR_HEX, type-checked
        # before the handler body runs, the same as blockhash and txid
        # elsewhere in this file
        raise type_error(1, "hexstring", hexstring, "string")
    # Not bool_param: that helper folds "omitted" into its own default,
    # and the three cases -- omitted, explicit true, explicit false --
    # answer differently here, this function's own docstring
    iswitness: bool | None = None
    if len(params) > 1 and params[1] is not None:
        if not isinstance(params[1], bool):
            raise type_error(2, "iswitness", params[1], "bool")
        iswitness = params[1]
    try:
        tx = _decode_hex_tx(hexstring, iswitness=iswitness)
    except BTClibException as error:
        # Core's own bare message, with none of sendrawtransaction's
        # "Make sure the tx has at least one input.": decoderawtransaction's
        # own handler raises `JSONRPCError(RPC_DESERIALIZATION_ERROR,
        # "TX decode failed")` with no further text
        # (src/rpc/rawtransaction.cpp:439, same tag).
        raise RpcError(
            RPCErrorCode.DESERIALIZATION_ERROR, "TX decode failed"
        ) from error
    return _tx_to_univ(tx, node.chain.name, include_hex=False)


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

# Core's own `MAX_MONEY` (`src/consensus/amount.h`, same
# tag): `MoneyRange`'s own bound, what `AmountFromValue` refuses an
# out-of-range `maxfeerate` or `maxburnamount` against.
_MAX_MONEY = 21_000_000 * COIN
# Core's own `DEFAULT_MAX_RAW_TX_FEE_RATE` (`src/node/transaction.h`,
# same tag): `sendrawtransaction` and `testmempoolaccept`'s own default
# `maxfeerate`, 0.1 BTC/kvB.
_DEFAULT_MAX_RAW_TX_FEE_RATE = COIN // 10
# Core's own `DEFAULT_MAX_BURN_AMOUNT` (same file): `sendrawtransaction`'s
# own default `maxburnamount`, zero.
_DEFAULT_MAX_BURN_AMOUNT = 0
# Core's own error, `AmountFromValue`'s (`src/rpc/util.cpp`, same tag)
# ahead of `ParseFeeRate`'s own bound, both of `test_mempool_accept` and
# `send_raw_transaction` below reading it through `_amount_param`.
_AMOUNT_NOT_NUMBER_OR_STRING = "Amount is not a number or string"


# `ParseFixedPoint`'s own grammar (`src/util/strencodings.cpp`, same
# tag): an optional `-`, a lone `0` or digits not starting with `0`, an
# optional `.` followed by at least one digit, an optional exponent, and
# nothing else. `[0-9]`, not `\d`: Python's `\d` reads any Unicode digit.
_FIXED_POINT = re.compile(
    r"-?(?P<int>0|[1-9][0-9]*)(?:\.(?P<frac>[0-9]+))?(?:[eE](?P<exp>[+-]?[0-9]+))?"
)
# `ParseFixedPoint`'s own `UPPER_BOUND`: the largest value it returns,
# and the bound on its mantissa and on its exponent's digits.
_FIXED_POINT_DIGITS = 18
_FIXED_POINT_BOUND = 10**_FIXED_POINT_DIGITS - 1
_BTC_DECIMALS = 8


def _parse_fixed_point(text: str, decimals: int) -> int | None:
    """Answer Core's `ParseFixedPoint`: `text` as an integer of 10^-`decimals`.

    `src/util/strencodings.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag. `None` stands for its `false`: text outside `_FIXED_POINT`,
    more decimals than `decimals` once the trailing zeros are dropped,
    or a mantissa, an exponent or a result past `_FIXED_POINT_BOUND`.
    Python's `Decimal` takes a space, a `+`, an underscore, `1.` and `.5`,
    which Core refuses, so it does not read the text here. The mantissa
    is kept without its trailing zeros, as Core keeps it, so `0e30` is
    refused, as Core refuses it, though its value is zero.
    """
    match = _FIXED_POINT.fullmatch(text)
    if match is None:
        return None
    fraction = match["frac"] or ""
    digits = ("" if match["int"] == "0" else match["int"]) + fraction
    mantissa = digits.rstrip("0")
    exponent_text = match["exp"] or "0"
    exponent_digits = exponent_text.lstrip("+-").lstrip("0")
    if max(len(mantissa.lstrip("0")), len(exponent_digits)) > _FIXED_POINT_DIGITS:
        return None
    exponent = (
        (-1 if exponent_text.startswith("-") else 1) * int(exponent_digits or "0")
        - len(fraction)
        + len(digits)
        - len(mantissa)
        + decimals
    )
    if not 0 <= exponent < _FIXED_POINT_DIGITS:
        return None
    amount: int = int(mantissa or "0") * 10**exponent
    if amount > _FIXED_POINT_BOUND:
        return None
    return -amount if text.startswith("-") else amount


def _amount_param(params: list[Any], position: int, *, name: str, default: int) -> int:
    """Read an `RPCArg::Type::AMOUNT` argument, Core's own `AmountFromValue`.

    `src/rpc/util.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag:
    `RPCArg::Type::AMOUNT` is exempt from `RPCMethod::HandleRequest`'s
    own pre-check (`ExpectedType` answers `std::nullopt` for it, "VNUM
    or VSTR, checked inside AmountFromValue()"), so a JSON number or
    string is read here as a decimal BTC amount by `_parse_fixed_point`,
    and refused as `RPC_TYPE_ERROR`: "Amount is not a number or string"
    for a JSON value of neither type -- `bool` included, `bool` being
    `int`'s own subclass in Python and no JSON bool ever being a number
    to Core's own `UniValue` -- "Invalid amount" for one that
    `_parse_fixed_point` refuses, and "Amount out of range" for one that
    it reads but that falls outside `MoneyRange`, 0 through `MAX_MONEY`.

    `btclib.amount.valid_btc_amount` folds Core's two messages into the
    one `BTClibValueError` it always raises, and reads its text with
    `Decimal`, which is why this does not call it.
    """
    if len(params) <= position or params[position] is None:
        return default
    value = params[position]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RpcError(RPCErrorCode.TYPE_ERROR, _AMOUNT_NOT_NUMBER_OR_STRING)
    amount = _parse_fixed_point(str(value), _BTC_DECIMALS)
    if amount is None:
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
    if rate >= COIN:
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

    One transaction is judged alone (`_test_accept_alone`), and several
    as one package (`package.package_test_accept`), as Core's handler
    does (`src/rpc/mempool.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag). Nothing is added to the mempool. A refusal is reported in the
    entry of the transaction it names; a fault that is not one propagates
    and ends the call, as Core's handler has no catch-all either
    (btclib-org/btclib-node#668).
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
            txs.append(_decode_hex_tx(rawtx))
        except BTClibException as error:
            # `BTClibException`, `send_raw_transaction`'s own clause below:
            # a script shorter than its declared length raises
            # `BTClibRuntimeError`, not `BTClibValueError`
            err_msg = (
                f"TX decode failed: {rawtx} Make sure the tx has at least one input."
            )
            raise RpcError(RPCErrorCode.DESERIALIZATION_ERROR, err_msg) from error
    return _test_accept_results(node, txs, max_raw_tx_fee_rate)


def _test_accept_results(
    node: Node, txs: list[Tx], max_raw_tx_fee_rate: int
) -> list[dict[str, Any]]:
    """Judge `txs`, alone or as a package, and answer an entry for each."""
    if len(txs) == 1:
        package_error, outcomes = None, {txs[0].hash: _test_accept_alone(node, txs[0])}
    else:
        package_error, outcomes = package_test_accept(node, txs)
    results: list[dict[str, Any]] = []
    # Core leaves the entries after one over `maxfeerate` unanswered, its
    # descendants not being submitted with it (`src/rpc/mempool.cpp`)
    exit_early = False
    for tx in txs:
        result: dict[str, Any] = {"txid": tx.id, "wtxid": tx.hash}
        if package_error is not None:
            result["package-error"] = package_error
        outcome = outcomes.get(tx.hash)
        if outcome is not None and not exit_early:
            exit_early = _test_accept_verdict(result, tx, outcome, max_raw_tx_fee_rate)
        results.append(result)
    return results


def _test_accept_alone(node: Node, tx: Tx) -> Outcome:
    """Return what `testmempoolaccept` finds of `tx` alone.

    Core's `ProcessTransaction` with `test_accept` (`src/validation.cpp`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `PreChecks` asks
    `CheckTransaction` first, ahead of everything context-dependent
    (btclib-org/btclib-node#1375). Only a refusal is caught, as a fault
    is no verdict on `tx` (btclib-org/btclib-node#668).
    """
    reason = _check_transaction(tx)
    if reason is not None:
        return Outcome(error=TxRejectedError(reason))
    try:
        # the sigop-adjusted size, known once the prevouts are read
        # (btclib-org/btclib-node#1357)
        candidate = verify_mempool_acceptance(node, tx)
    except (MissingPrevoutError, TxRejectedError) as refusal:
        return Outcome(error=refusal)
    return accepted(node, tx, candidate.fee, candidate.vsize)


def _test_accept_verdict(
    result: dict[str, Any], tx: Tx, outcome: Outcome, max_raw_tx_fee_rate: int
) -> bool:
    """Write the verdict of `outcome` in `result`; say if over `maxfeerate`.

    Core's `testmempoolaccept` (`src/rpc/mempool.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). A refusal is its reason
    and `ToString`, but a missing input is "missing-inputs" alone
    (btclib-org/btclib-node#1245, btclib-org/btclib-node#1328). The fee
    cap is asked of a transaction that passed, with no details
    (btclib-org/btclib-node#1371). The sizes are Core 32's
    (`src/rpc/mempool.cpp`, at bitcoin/bitcoin@aef8a04966,
    btclib-org/btclib-node#1757). The effective feerate is in BTC per kvB,
    rounded down as `CFeeRate::GetFeePerK` rounds it
    (btclib-org/btclib-node#1799).
    """
    result["allowed"] = False
    error = outcome.error
    if isinstance(error, MissingPrevoutError):
        result["reject-reason"] = "missing-inputs"
        return False
    if error is not None:
        result["reject-reason"] = cast("TxRejectedError", error).reason
        result["reject-details"] = str(error)
        return False
    if _exceeds_max_fee(outcome.vsize, outcome.base_fee, max_raw_tx_fee_rate):
        result["reject-reason"] = "max-fee-exceeded"
        return True
    result["vsize_adjusted"] = outcome.vsize
    result["vsize"] = outcome.vsize
    result["vsize_bip141"] = tx.vsize
    result["allowed"] = True
    modified_fee, vsize, wtxids = cast(
        "tuple[int, int, list[bytes]]", outcome.effective
    )
    result["fees"] = {
        "base": btc_amount(outcome.base_fee),
        "effective-feerate": btc_amount(modified_fee * 1000 // vsize),
        "effective-includes": [wtxid.hex() for wtxid in wtxids],
    }
    return False


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
        tx = _decode_hex_tx(rawtx)
    except BTClibException as error:
        # Core's own RPC_DESERIALIZATION_ERROR, src/rpc/mempool.cpp: a
        # rawtx that never was a transaction, not one the mempool below
        # looked at and refused. `_decode_hex_tx` raises BTClibValueError
        # for a string that is not hex or that btclib's `Tx.parse` cannot
        # decode and BTClibRuntimeError for one too short for what it
        # declares -- `BTClibException`, neither itself raised, is the
        # base both share and the one clause this catches them with
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
    if already_confirmed(node, tx):
        # Core's own early return, ahead of the held-in-mempool check
        # below and of verification itself (`already_confirmed`'s own
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
    reason = _check_transaction(tx)
    if reason is not None:
        # `BroadcastTransaction` reads `state.ToString()` off
        # `PreChecks`' own `CheckTransaction` refusal
        # (`src/node/transaction.cpp`, at bitcoin/bitcoin@9be056a8a7,
        # the v31.1 tag) before anything context-dependent runs, the
        # same as `test_mempool_accept` above; that string is the bare
        # reason, `CheckTransaction` attaching no debug message, and
        # `TransactionError::MEMPOOL_REJECTED` is `RPC_TRANSACTION_REJECTED`,
        # `_MEMPOOL_FULL_REASON`'s own alias for `-26`.
        # btclib-org/btclib-node#1375
        raise RpcError(RPCErrorCode.VERIFY_REJECTED, reason)
    try:
        fee, vsize, weight, replaced = verify_mempool_acceptance(node, tx)
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
    if not node.mempool.add_tx(
        tx, fee, vsize, height=tip_height, weight=weight, replaced=replaced
    ):
        # Not kept: `Mempool._evict_to_limit` ran
        # and took this transaction right back out when the trim reached
        # its chunk, once `Mempool.bytesize_limit` was restored -- exactly
        # the case `_MEMPOOL_FULL_REASON`'s own comment names, Core's
        # `TX_RECONSIDERABLE` "mempool full". btclib-org/btclib-node#294
        raise RpcError(RPCErrorCode.VERIFY_REJECTED, _MEMPOOL_FULL_REASON)
    track_accepted(node, tx)
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


def get_rpc_info(node: Node, conn: RpcConnection, _: list[Any]) -> dict[str, Any]:
    """Answer `getrpcinfo`: every RPC call in flight, and where this node logs.

    Core's own `RPCServerInfo.active_commands`/`RPCCommandExecution`
    (`src/rpc/server.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag): `rpc.main._execute` is this node's own `ExecuteCommand`, and
    `node.active_rpc_commands` is its own `active_commands`, appended to
    and removed there rather than guarded by a destructor -- this call's
    own entry is already in it by the time this callback runs, exactly
    as Core's own is by the time its lambda runs. `duration` is
    microseconds, `Ticks<std::chrono::microseconds>` over Core's own
    `SteadyClock::now() - info.start`; `time.monotonic()` is this
    node's own steady clock, elapsed time only and never a wall-clock
    reading, which is what `SteadyClock` is too.

    `logpath` is `node.log_path`, `""` where this node logs to a stream
    rather than a file, as `LogInstance().m_file_path.utf8string()`
    answers for an unset path too.
    """
    now = time.monotonic()
    active_commands = [
        {"method": method, "duration": int((now - start) * 1_000_000)}
        for method, start in node.active_rpc_commands
    ]
    return {
        "active_commands": active_commands,
        "logpath": str(node.log_path) if node.log_path else "",
    }


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
    """Answer `stop`; `rpc.main._step` delays this reply by `wait`, then stops.

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
    tag). A `time.sleep` here would run before `rpc.main._step` requested
    this node's shutdown at all, on the one thread that carries RPC, P2P
    and chain work alike (`ARCHITECTURE.md`, "The loop").
    `rpc.main._answer_one` reads this same `wait` again once this call
    is known to have succeeded; `rpc.main._step` hands the delayed reply to
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
    "waitforblock": wait_for_block,
    "waitforblockheight": wait_for_block_height,
    "waitfornewblock": wait_for_new_block,
    "getblockhash": get_block_hash,
    "getblockheader": get_block_header,
    "getblock": get_block,
    "getchaintips": get_chain_tips,
    "invalidateblock": invalidate_block,
    "reconsiderblock": reconsider_block,
    "preciousblock": precious_block,
    "submitblock": submit_block,
    "submitheader": submit_header,
    "getblocktemplate": get_block_template,
    "generatetoaddress": generate_to_address,
    "generateblock": generate_block,
    "getpeerinfo": get_peer_info,
    "getconnectioncount": get_connection_count,
    "getnetworkinfo": get_network_info,
    "addnode": add_node,
    "disconnectnode": disconnect_node,
    "setnetworkactive": set_network_active,
    "addconnection": add_connection,
    "getnodeaddresses": get_node_addresses,
    "addpeeraddress": add_peer_address,
    "setban": set_ban,
    "listbanned": list_banned,
    "clearbanned": clear_banned,
    "getmempoolinfo": get_mempool_info,
    "savemempool": save_mempool,
    "importmempool": import_mempool,
    "getrawmempool": get_raw_mempool,
    "getorphantxs": get_orphan_txs,
    "getmempoolentry": get_mempool_entry,
    "getmempoolcluster": get_mempool_cluster,
    "getmempoolfeeratediagram": get_mempool_feerate_diagram,
    "getmempoolancestors": get_mempool_ancestors,
    "getmempooldescendants": get_mempool_descendants,
    "prioritisetransaction": prioritise_transaction,
    "getprioritisedtransactions": get_prioritised_transactions,
    "gettxspendingprevout": get_tx_spending_prevout,
    "getrawtransaction": get_raw_transaction,
    "gettxout": get_tx_out,
    "gettxoutsetinfo": get_tx_out_set_info,
    "dumptxoutset": dump_tx_out_set,
    "scantxoutset": scan_tx_out_set,
    "decoderawtransaction": decode_raw_transaction,
    "testmempoolaccept": test_mempool_accept,
    "sendrawtransaction": send_raw_transaction,
    "signmessagewithprivkey": sign_message_with_privkey,
    "verifymessage": verify_message,
    "signrawtransactionwithkey": sign_raw_transaction_with_key,
    "combinerawtransaction": combine_raw_transaction,
    "submitpackage": submit_package,
    "estimatesmartfee": estimate_smart_fee,
    "estimaterawfee": estimate_raw_fee,
    "ping": ping,
    "stop": stop,
    "help": help_rpc,
    "getrpcinfo": get_rpc_info,
}

# Each method's parameter names, in the order of its positions, as its
# `RPCHelpMan` declares them and `CRPCCommand::argNames` carries them
# (`src/rpc/server.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): what
# `rpc.jsonrpc.transform_named_arguments` maps an object's keys onto.
# `a|b` is two names for one position. None of these methods takes an
# `OBJ_NAMED_PARAMS` options object but `dumptxoutset`, `gettxspendingprevout`
# and `importmempool`, whose `options` position carries their options too, as
# `client.cpp` lists them; `named_only` says which are named-only.
# `bitcoind`'s own table is what `help dump_all_command_conversions`
# answers, and `tests/integration/rpc_framing_test.py` holds this one to it.
arg_names: dict[str, tuple[str, ...]] = {
    "getbestblockhash": (),
    "getblockcount": (),
    "getblockchaininfo": (),
    "pruneblockchain": ("height",),
    "waitforblock": ("blockhash", "timeout"),
    "waitforblockheight": ("height", "timeout"),
    "waitfornewblock": ("timeout", "current_tip"),
    "getblockhash": ("height",),
    "getblockheader": ("blockhash", "verbose"),
    "getblock": ("blockhash", "verbosity|verbose"),
    "getchaintips": (),
    "invalidateblock": ("blockhash",),
    "reconsiderblock": ("blockhash",),
    "preciousblock": ("blockhash",),
    "submitblock": ("hexdata", "dummy"),
    "submitheader": ("hexdata",),
    "getblocktemplate": ("template_request",),
    "generatetoaddress": ("nblocks", "address", "maxtries"),
    "generateblock": ("output", "transactions", "submit"),
    "getpeerinfo": (),
    "getconnectioncount": (),
    "getnetworkinfo": (),
    "addnode": ("node", "command", "v2transport"),
    "disconnectnode": ("address", "nodeid"),
    "setnetworkactive": ("state",),
    "addconnection": ("address", "connection_type", "v2transport"),
    "getnodeaddresses": ("count", "network"),
    "addpeeraddress": ("address", "port", "tried"),
    "setban": ("subnet", "command", "bantime", "absolute"),
    "listbanned": (),
    "clearbanned": (),
    "getmempoolinfo": (),
    "savemempool": (),
    "importmempool": (
        "filepath",
        "options|use_current_time|apply_fee_delta_priority|apply_unbroadcast_set",
    ),
    "getrawmempool": ("verbose", "mempool_sequence"),
    "getorphantxs": ("verbosity",),
    "getmempoolentry": ("txid",),
    "getmempoolcluster": ("txid",),
    "getmempoolfeeratediagram": (),
    "getmempoolancestors": ("txid", "verbose"),
    "getmempooldescendants": ("txid", "verbose"),
    "prioritisetransaction": ("txid", "dummy", "fee_delta"),
    "getprioritisedtransactions": (),
    "gettxspendingprevout": (
        "outputs",
        "options|mempool_only|return_spending_tx",
    ),
    "getrawtransaction": ("txid", "verbosity|verbose", "blockhash"),
    "gettxout": ("txid", "n", "include_mempool"),
    "gettxoutsetinfo": ("hash_type", "hash_or_height", "use_index"),
    "dumptxoutset": ("path", "type", "options|rollback"),
    "scantxoutset": ("action", "scanobjects"),
    "decoderawtransaction": ("hexstring", "iswitness"),
    "testmempoolaccept": ("rawtxs", "maxfeerate"),
    "sendrawtransaction": ("hexstring", "maxfeerate", "maxburnamount"),
    "signmessagewithprivkey": ("privkey", "message"),
    "verifymessage": ("address", "signature", "message"),
    "signrawtransactionwithkey": ("hexstring", "privkeys", "prevtxs", "sighashtype"),
    "combinerawtransaction": ("txs",),
    "submitpackage": ("package", "maxfeerate", "maxburnamount"),
    "estimatesmartfee": ("conf_target", "estimate_mode"),
    "estimaterawfee": ("conf_target", "threshold"),
    "ping": (),
    "stop": ("wait",),
    "help": ("command",),
    "getrpcinfo": (),
}

# The names of each method's `OBJ_NAMED_PARAMS` options, which
# `transform_named_arguments` gathers into the options object.
named_only: dict[str, tuple[str, ...]] = {
    "dumptxoutset": ("rollback",),
    "gettxspendingprevout": ("mempool_only", "return_spending_tx"),
    "importmempool": (
        "use_current_time",
        "apply_fee_delta_priority",
        "apply_unbroadcast_set",
    ),
}
