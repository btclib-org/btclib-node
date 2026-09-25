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
from typing import TYPE_CHECKING, Any, cast

from bitcoin_core_rpc import RPCErrorCode, chain_from_network
from btclib.block import Block, median_time_past
from btclib.exceptions import BTClibException, BTClibValueError
from btclib.p2p.address import ServiceFlags
from btclib.p2p.limits import PROTOCOL_VERSION
from btclib.tx import Tx

from btclib_node.chainstate.block_index import block_time
from btclib_node.config import split_host_port
from btclib_node.constants import MIN_BLOCKS_TO_KEEP, USER_AGENT, P2pConnStatus
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.main import (
    parent_lookup,
    prune_up_to_height,
    verify_mempool_acceptance,
)
from btclib_node.p2p.address import ip_and_port, peer_address
from btclib_node.p2p.eviction import Network, is_valid, net_class
from btclib_node.rpc.connection import RawJSON
from btclib_node.rpc.errors import RpcError, bool_param, type_error

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.chainstate.block_index import BlockIndex
    from btclib_node.p2p.block_availability import BlockAvailability
    from btclib_node.p2p.connection import Connection
    from btclib_node.rpc.connection import RpcConnection

__all__ = [
    "add_node",
    "callbacks",
    "get_best_block_hash",
    "get_block",
    "get_block_count",
    "get_block_hash",
    "get_block_header",
    "get_blockchain_info",
    "get_connection_count",
    "get_mempool_info",
    "get_network_info",
    "get_peer_info",
    "get_raw_mempool",
    "get_raw_transaction",
    "get_tx_out_set_info",
    "ping",
    "prune_blockchain",
    "send_raw_transaction",
    "service_names",
    "stop",
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
        raise RpcError(RPCErrorCode.MISC_ERROR, "pruneblockchain height")
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
        # :887. Unquoted, unlike blockhash's own usage string:
        # RPCArg::ToString(oneline=true) quotes an argument's name only
        # for Type::STR/STR_HEX, and height is Type::NUM
        # (src/rpc/blockchain.cpp:585), which formats bare
        # (src/rpc/util.cpp:1265-1286)
        raise RpcError(RPCErrorCode.MISC_ERROR, "getblockhash height")

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
        # turns that into JSONRPCError(RPC_MISC_ERROR, e.what()). Both
        # arguments render the way RPCArg::ToString(oneline=true) does:
        # blockhash quoted for being STR_HEX, verbose bare and grouped
        # in its own trailing `( ... )` for being optional --
        # read at bitcoin/bitcoin@b91d983f66, src/rpc/blockchain.cpp:614-617
        raise RpcError(
            RPCErrorCode.MISC_ERROR, 'getblockheader "blockhash" ( verbose )'
        )
    if not isinstance(params[0], str):
        # RPCMethod::HandleRequest checks a declared argument's JSON
        # type before the handler body runs at all, src/rpc/util.cpp
        # :653-661 -- blockhash is declared RPCArg::Type::STR_HEX, so a
        # blockhash of any other JSON type never reaches ParseHashV and
        # is refused here the same way, before bytes.fromhex sees it
        raise type_error(1, "blockhash", params[0], "string")

    # verbose, src/rpc/blockchain.cpp:617: RPCArg::Type::BOOL,
    # RPCArg::Default{true}. Read and type-checked up front, the same
    # as blockhash above and for the same reason: HandleRequest checks
    # every declared argument's type before any of the handler's own
    # work runs, not only the first one
    verbose = bool_param(params, 1, name="verbose", default=True)

    try:
        block_hash = bytes.fromhex(params[0])
    except ValueError as error:
        # ParseHashV, src/rpc/util.cpp:125, down to the sentence
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"hash must be hexadecimal string (not '{params[0]}')",
        ) from error
    try:
        block_info = block_index.get_block_info(block_hash)
    except KeyError as error:
        # a hash nothing indexed is a question about a block, not a
        # fault of this node: src/rpc/blockchain.cpp:695
        raise RpcError(
            RPCErrorCode.INVALID_ADDRESS_OR_KEY, "Block not found"
        ) from error
    header = block_info.header

    if not verbose:
        # src/rpc/blockchain.cpp:668-673: the same eighty bytes a peer
        # is sent on the wire, hex-encoded rather than the JSON object
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
        # btclib bounds `version` to `0 < version <= 0x7FFFFFFF`
        # (block_header.py's own `assert_valid`), so the top bit is
        # never set and a plain positive format matches what Core's
        # signed `%x` prints
        "versionHex": f"{header.version:08x}",
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


def get_block(node: Node, conn: RpcConnection, params: list[Any]) -> str:
    """Answer `getblock` at verbosity 0: the block's own wire bytes, hex.

    Core's own `getblock` (`rpc/blockchain.cpp:835-920`, at
    bitcoin/bitcoin@bb529657) answers verbosity 1, 2 and 3 with a JSON
    object too -- `blockToJSON`'s own field-by-field accounting, one
    `TxVerbosity` deeper each time. `get_block_header` above is where
    that same accounting already lives for a header alone; tf2's own
    first family reads only the hex form (`btclib-org/btclib-node#1006`),
    so that is the one verbosity served here. Verbosity 1 default
    included: `ParseVerbosity` (`rpc/util.cpp:89-102`, same sha) answers
    a missing argument with 1, and this refuses that default rather than
    silently answering hex for it.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, 'getblock "blockhash" ( verbosity )')
    if not isinstance(params[0], str):
        raise type_error(1, "blockhash", params[0], "string")
    try:
        block_hash = bytes.fromhex(params[0])
    except ValueError as error:
        # `ParseHashV(request.params[0], "blockhash")` (`rpc/blockchain.cpp`,
        # line 882, same sha) -- `getblock`'s own label, not
        # `get_block_header`'s "hash" (line 679, same sha), the two RPCs
        # naming the same positional argument differently.
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"blockhash must be hexadecimal string (not '{params[0]}')",
        ) from error

    # `ParseVerbosity` (`rpc/util.cpp:89-102`) answers a missing or
    # `null` argument with `default_verbosity`, 1 here -- read the same
    # way, so that omitting the argument is refused below exactly as
    # asking for verbosity 1 explicitly would be, rather than silently
    # answered with hex. `false`/`0` alone pass; `true` or any other
    # verbosity is refused too.
    has_verbosity = len(params) > 1 and params[1] is not None
    verbosity = params[1] if has_verbosity else 1
    if verbosity not in (0, False):
        raise RpcError(
            RPCErrorCode.MISC_ERROR,
            "getblock: only verbosity 0 (hex-encoded data) is served here",
        )

    block_index = node.chainstate.block_index
    try:
        block_info = block_index.get_block_info(block_hash)
    except KeyError as error:
        # `getblock`'s own throw, `rpc/blockchain.cpp:895`, same sha
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
    return block.serialize(check_validity=False).hex()


def submit_block(node: Node, conn: RpcConnection, params: list[Any]) -> str | None:
    """Answer `submitblock`, Core's own two arguments, the second ignored.

    Core's own `submitblock` (`rpc/mining.cpp:1089-1136`, at
    bitcoin/bitcoin@bb529657) decodes, indexes the header if it is new,
    and hands the block to `ProcessNewBlock`: `None` for one accepted,
    `"duplicate"` for one already held, and a reject reason for one
    refused -- `BlockValidationResult::BLOCK_MISSING_PREV`'s own
    `"prev-blk-not-found"` (`validation.cpp:4225`, same sha) is the one
    reason this tree reproduces literally, being the one this node's own
    `block_index.add_headers` answers the identical way `p2p.callbacks
    .block` already reads it (missing rather than invalid). A
    structurally invalid block is answered with btclib's own exception
    message instead of one of Core's: `BlockValidationResult` names
    dozens of distinct single-word reasons across `validation.cpp`, and
    this tree does not reproduce that vocabulary.

    Stores through the same `block_index`/`block_db` calls
    `p2p.callbacks.block` makes for a block delivered over the wire,
    minus that callback's own `Connection`-specific bookkeeping
    (`remove_block_request`), which does not apply to a block submitted
    out of band. Connecting
    the block to the active chain, on either path, is `main.
    update_chain`'s own job, run once every pass of `Node`'s loop
    rather than inline here -- the same pass this callback's own return
    runs in, `Node.run`'s `_step_chain` following `_drain_message_queues`
    unconditionally.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, 'submitblock "hexdata" ( "dummy" )')
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

    if block_hash in block_index.header_dict:
        if block_index.get_block_info(block_hash).downloaded:
            return "duplicate"
    else:
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

    try:
        block.assert_valid(node.chain.pow_limit_bits)
    except BTClibException as error:
        block_index.invalidate(block_hash)
        return str(error)

    node.block_db.add_block(block)
    block_index.set_downloaded(block_hash)
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
    """Core's `ConnectionTypeAsString` for the three types this node opens.

    An outbound connection `P2pManager` did not draw itself is a
    `-connect`, `-addnode` or `addnode` peer, Core's `MANUAL`.
    """
    if p2p_conn.inbound:
        return "inbound"
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
    # relay, this node offering no `NODE_BLOOM`, and the fields read off
    # it answer 0 or false where it does not.
    relays = version_message is not None and version_message.is_relay_requested
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
    # This node sends `sendcmpct` announcing low bandwidth and reads no
    # `sendcmpct` a peer sends, so it neither selects nor takes up a
    # high-bandwidth peer: false both ways, what Core answers where it
    # sent none and where it ignored a `sendcmpct` of a version it does
    # not speak.
    entry["bip152_hb_to"] = False
    entry["bip152_hb_from"] = False
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
    # True for every handshake-complete peer, where Core waits on an
    # inbound one's first `addr`, `addrv2` or `getaddr`
    # (btclib-org/btclib-node#1178).
    entry["addr_relay_enabled"] = p2p_conn.status == P2pConnStatus.Connected
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
        try:
            addr = p2p_conn.client.getpeername()
            addrbind = p2p_conn.client.getsockname()
        # A peer disconnecting mid-lookup is not worth logging a
        # second time; its own connection state already reports it.
        # Deliberately blind (BLE001) alongside S112: a disconnect
        # racing this call can surface as more than one socket
        # error depending on timing and platform, and every one of
        # them means the same "skip this peer, ask the next".
        except Exception:  # noqa: S112, BLE001
            continue
        # Core writes addrbind with `CService::ToStringAddrPort`, and
        # addrlocal from the string `CopyStats` builds with it; its addr
        # is `m_addr_name`, which is that same string only where the peer
        # was not dialled by name. Here addr is `getpeername`'s and never
        # a name, so one formatter serves them all.
        entry = _peer_entry(
            node,
            connection_id,
            p2p_conn,
            ip_and_port(addr[0], addr[1]),
            ip_and_port(addrbind[0], addrbind[1]),
        )
        out.append(entry)
    return out


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
    """
    return {
        "subversion": USER_AGENT,
        "protocolversion": PROTOCOL_VERSION,
    }


# Core's own three `addnode` commands (`rpc/net.cpp:341-415`, at
# bitcoin/bitcoin@bb529657); `add`/`remove` mutate `CConnman`'s own
# persistent added-node list, which this node has no counterpart to --
# `Config.addnode`, its own equivalent of `-addnode`, is a tuple
# resolved once at startup (`config.py`'s `_resolve_peers`) and dialled
# through `P2pManager`'s own redial set, never grown or shrunk at
# runtime. `connect_nodes`, the one caller this node's own tf2 census
# names for this method (`test_framework.py:568-594`, same sha), only
# ever calls `onetry`, which is the one command below with a real
# effect: it schedules the identical one-shot dial `onetry` gets in
# Core (`OpenNetworkConnection`, `conn_type=MANUAL`, no persistence, no
# dedup). `add` is accepted and scheduled the same way rather than
# raising, since refusing an otherwise-valid command would be less
# faithful to Core than dialling once and not persisting; `remove`
# answers Core's own `RPC_CLIENT_NODE_NOT_ADDED` every time, there being
# no added-node list here for it to find an entry in.
_ADDNODE_COMMANDS = ("add", "remove", "onetry")


def add_node(node: Node, conn: RpcConnection, params: list[Any]) -> None:
    """Answer `addnode`, `onetry` for real and the other two commands honestly.

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
    if len(params) < 2:  # noqa: PLR2004
        raise RpcError(
            RPCErrorCode.MISC_ERROR, 'addnode "node" "command" ( v2transport )'
        )
    if not isinstance(params[0], str):
        raise type_error(1, "node", params[0], "string")
    if not isinstance(params[1], str):
        raise type_error(2, "command", params[1], "string")
    node_arg, command = params[0], params[1]
    if command not in _ADDNODE_COMMANDS:
        raise RpcError(
            RPCErrorCode.MISC_ERROR, 'addnode "node" "command" ( v2transport )'
        )
    bool_param(params, 2, name="v2transport", default=False)

    if not node_arg.strip():
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER, "Error: Node address cannot be empty"
        )

    if command == "remove":
        raise RpcError(
            RPCErrorCode.CLIENT_NODE_NOT_ADDED,
            "Error: Node could not be removed. It has not been added previously.",
        )

    try:
        host, port = split_host_port(node_arg, node.chain.port)
        address = peer_address(host, port)
    except ValueError as error:
        # a hostname, or a malformed port: `_resolve_peers` (config.py)
        # refuses `-addnode`'s own spec the identical way and for the
        # identical reason -- this node's synchronous RPC path resolves
        # no DNS
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, str(error)) from error

    node.p2p_manager.connect(address)


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
    # four: `usage`, `total_fee`, `unbroadcastcount`,
    # `permitbaremultisig`, `maxdatacarriersize`, `limitclustercount`,
    # `limitclustersize`, `optimal`, the deprecated `fullrbf`. Every one
    # of those is backed by a concept this tree does not carry -- a
    # cluster mempool graph, a persisted total fee, unbroadcast-tx
    # tracking, a bare-multisig policy knob -- and answering any of them
    # with a placeholder would be exactly the decoration this method's
    # own sparse answer already was. `minrelaytxfee` and
    # `incrementalrelayfee` are excluded for a different reason: both
    # are real and cheap to answer here too (`Config.min_relay_feerate`,
    # `mempool.py`'s own incremental-fee constant), left out only
    # because #305 named these two fields and not those. `maxmempool`
    # and `mempoolminfee` are wired in because #294 gave both a real
    # source to read. btclib-org/btclib-node#305
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
    }


# ParseHashType's own two names this tree can answer
# (src/rpc/blockchain.cpp:977-987, at bitcoin/bitcoin@ca7162cde5).
# "hash_serialized_3" is a third name Core itself accepts but this tree
# does not implement -- get_tx_out_set_info's own docstring is where
# that refusal, reusing ParseHashType's own error text for a value Core
# would otherwise accept, is argued.
_TX_OUT_SET_HASH_TYPES = {"muhash", "none"}


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
    accumulator for: this node answers only from `CoinStats`, never
    from a live scan, so there is no second computation to answer that
    hash type with. `hash_type: "none"` answers every field but
    `muhash` itself, the way Core's own `CoinStatsHashType::NONE` does.

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
    hash_type = params[0] if params and params[0] is not None else "hash_serialized_3"
    if not isinstance(hash_type, str):
        raise type_error(1, "hash_type", hash_type, "string")
    if hash_type not in _TX_OUT_SET_HASH_TYPES:
        err_msg = f"'{hash_type}' is not a valid hash_type"
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, err_msg)

    if len(params) > 1 and params[1] is not None:
        err_msg = "Querying specific block heights requires coinstatsindex"
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, err_msg)

    # type-checked and otherwise unused -- this method's own docstring
    # argues why
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
    # RPCArg::Default{false}: src/rpc/mempool.cpp:694-695
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
                "vsize": tx.vsize,
                "weight": tx.weight,
                "wtxid": tx.hash.hex(),
            }
            for tx in node.mempool.transactions.values()
        }

    txids = [txid.hex() for txid in node.mempool.txid_index]
    if not include_sequence:
        # MempoolToJSON's plain-array answer, src/rpc/mempool.cpp:624-634
        return txids
    # MempoolToJSON's other shape, src/rpc/mempool.cpp:635-639
    return {"txids": txids, "mempool_sequence": node.mempool.sequence}


def _parse_txid(params: list[Any]) -> bytes:
    if not params:
        # the same shape as getblockheader's own missing-argument case:
        # RPCMethod::HandleRequest's HelpResult, RPC_MISC_ERROR
        # (src/rpc/server.cpp:887). RPCMethod::ToString opens a `( ` on
        # the first optional argument and closes it once, after the
        # loop (src/rpc/util.cpp:775-798), so verbose and blockhash --
        # both optional here -- render inside one group, not two.
        # Core's own first name for this argument is "verbosity"
        # (declared "verbosity|verbose", src/rpc/rawtransaction.cpp
        # :246); this node keeps its own "verbose" instead, because
        # `verbose` below reads only the boolean shape Core's
        # `RPCArg::Default{0}` degrades to under `allow_bool=true`, not
        # the full 0/1/2 verbosity Core's name is for --
        # read at bitcoin/bitcoin@b91d983f66
        raise RpcError(
            RPCErrorCode.MISC_ERROR,
            'getrawtransaction "txid" ( verbose "blockhash" )',
        )
    if not isinstance(params[0], str):
        # txid is declared RPCArg::Type::STR_HEX, type-checked before
        # the handler body runs, same as blockhash below
        raise type_error(1, "txid", params[0], "string")
    try:
        return bytes.fromhex(params[0])
    except ValueError as error:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"parameter 1 must be hexadecimal string (not '{params[0]}')",
        ) from error


def _parse_optional_block_hash(params: list[Any]) -> bytes | None:
    # index 2 is this RPC's own third positional, "blockhash" in
    # get_raw_transaction's own help string -- naming it would give a
    # second name to what that string already names, tied to this one
    # method's own argument list and not reusable past it
    if len(params) <= 2 or params[2] is None:  # noqa: PLR2004
        return None
    if not isinstance(params[2], str):
        raise type_error(3, "blockhash", params[2], "string")
    try:
        return bytes.fromhex(params[2])
    except ValueError as error:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            f"parameter 3 must be hexadecimal string (not '{params[2]}')",
        ) from error


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
    txid = _parse_txid(params)
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
    block_hash = _parse_optional_block_hash(params)
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


# the two reject reasons `verify_mempool_acceptance` can fail with,
# named once so that `test_mempool_accept` and `send_raw_transaction`
# answer the same verdict about the same transaction rather than
# drifting apart the way btclib-org/btclib-node#83 found them
_MISSING_PREVOUTS_REASON = "Missing prevouts"
_INVALID_SCRIPT_REASON = "Invalid signatures or script"
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
_MEMPOOL_FULL_REASON = "Mempool is full"


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
        # :887. `rawtxs` is declared RPCArg::Type::ARR of one
        # STR_HEX `rawtx`, which RPCArg::ToString(oneline=true) renders
        # `["rawtx",...]` (src/rpc/util.cpp:1265-1301); `maxfeerate` is
        # RPCArg::Type::AMOUNT, formatted bare and grouped in its own
        # `( ... )` for being optional --
        # read at bitcoin/bitcoin@b91d983f66, src/rpc/mempool.cpp:291-298
        raise RpcError(
            RPCErrorCode.MISC_ERROR,
            'testmempoolaccept ["rawtx",...] ( maxfeerate )',
        )
    rawtxs = params[0]
    if not isinstance(rawtxs, list):
        # rawtxs is declared RPCArg::Type::ARR, type-checked before the
        # handler body runs, the same as blockhash and txid elsewhere in
        # this file
        raise type_error(1, "rawtxs", rawtxs, "array")
    out: list[dict[str, Any]] = []
    for rawtx in rawtxs:
        try:
            tx = Tx.parse(rawtx)
        except BTClibValueError:
            out.append({"allowed": False, "reject-reason": "Invalid serialization"})
            continue

        tx_res: dict[str, Any] = {
            "txid": tx.id,
            "wtxid": tx.hash,
            "allowed": False,
            "vsize": tx.vsize,
        }
        # Only these two, matching Core's own shape: testmempoolaccept's
        # per-tx loop (src/rpc/mempool.cpp:379-430, at
        # bitcoin/bitcoin@ca7162cde5) never catches anything itself --
        # it only ever branches on the TxValidationResult
        # ProcessTransaction always returns rather than raises, so a
        # genuine C++ exception escaping that loop is not one tx's own
        # verdict, it propagates out of the RPC call entirely, to
        # ExecuteCommand's own catch (src/rpc/server.cpp:874-887, same
        # commit), which is this tree's handle_rpc (rpc/main.py) --
        # already logging and answering INTERNAL_ERROR for exactly this,
        # the same uniform catch send_raw_transaction below already
        # relies on for anything past its own two excepts
        # (btclib-org/btclib-node#668).
        try:
            verify_mempool_acceptance(node, tx)
            tx_res["allowed"] = True
        except BTClibValueError:
            tx_res["reject-reason"] = _INVALID_SCRIPT_REASON
        except MissingPrevoutError:
            tx_res["reject-reason"] = _MISSING_PREVOUTS_REASON
        out.append(tx_res)
    return out


def send_raw_transaction(node: Node, conn: RpcConnection, params: list[Any]) -> str:
    """Answer `sendrawtransaction`: verify, add to the mempool, announce.

    A transaction that fails to decode, that `verify_mempool_acceptance`
    refuses, or that `Mempool.add_tx` evicts right back out under its
    own size limit is each refused with the reject reason and code
    cited beside its own raise, below; one kept is broadcast to peers
    and its txid answered.
    """
    if not params:
        # the same mechanism get_block_hash's own missing-argument case
        # answers with: RPCMethod::HandleRequest throws HelpResult for a
        # call short of a required argument, and ExecuteCommand's
        # `catch (const std::exception& e)` turns that into Core's own
        # JSONRPCError call, cited below for the shape rather than left
        # commented out -- ERA001 reads it as Python and is wrong.
        # JSONRPCError(RPC_MISC_ERROR, e.what()), src/rpc/server.cpp  # noqa: ERA001
        # :887. `hexstring` is declared RPCArg::Type::STR_HEX,
        # quoted the way blockhash's own usage string already is;
        # `maxfeerate` and `maxburnamount` are both RPCArg::Type::AMOUNT
        # with a Default, formatted bare and grouped in one `( ... )`
        # for being consecutively optional --
        # read at bitcoin/bitcoin@b91d983f66, src/rpc/mempool.cpp:72-77
        raise RpcError(
            RPCErrorCode.MISC_ERROR,
            'sendrawtransaction "hexstring" ( maxfeerate maxburnamount )',
        )
    rawtx = params[0]
    if not isinstance(rawtx, str):
        # hexstring is declared RPCArg::Type::STR_HEX
        # (src/rpc/mempool.cpp:72), type-checked before the handler
        # body runs, the same as blockhash and txid above
        raise type_error(1, "hexstring", rawtx, "string")
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
    try:
        fee = verify_mempool_acceptance(node, tx)
    except MissingPrevoutError as exc:
        # Core's own missing-inputs code, RPC_VERIFY_ERROR
        # (src/rpc/protocol.h): a transaction this node cannot verify
        # for want of what it spends, not one it refuses
        raise RpcError(RPCErrorCode.VERIFY_ERROR, _MISSING_PREVOUTS_REASON) from exc
    except BTClibValueError as exc:
        # Core's own RPC_VERIFY_REJECTED: the mempool looked at the
        # transaction and refused it
        raise RpcError(RPCErrorCode.VERIFY_REJECTED, _INVALID_SCRIPT_REASON) from exc
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
    kept = node.mempool.add_tx(tx, fee)
    if kept:
        to_announce = tx
    elif tx.id in node.mempool.txid_index:
        # `add_tx` declined for the other reason it can: this txid is
        # already held, possibly under a different witness -- and
        # therefore a different wtxid -- than what was just resubmitted.
        # Announcing the resubmitted object's own wtxid here, rather
        # than the mempool's, would queue a wtxid nothing holds:
        # `Mempool.add_tx`'s own comment on #277 is the defect this
        # substitution avoids, one call site over. `BroadcastTransaction`
        # (`node/transaction.cpp`, at bitcoin/bitcoin@58a7869f86) makes the
        # identical substitution for the identical reason -- "Use the
        # mempool's wtxid for reannouncement" -- rather than
        # reannouncing what was just submitted. The type is wider than
        # the invariant: `get_tx` cannot answer `None` once `txid_index`
        # holds `tx.id`, checked on this very branch, so this is a cast
        # rather than a check dead on every path that reaches it,
        # matching `Connection.send_version`'s own `self.manager.port`.
        # btclib-org/btclib-node#293
        to_announce = cast("Tx", node.mempool.get_tx(tx.id))
    else:
        # Neither already held nor kept: `Mempool._evict_to_limit` ran
        # and took this transaction right back out for being the worst
        # one held once `Mempool.bytesize_limit` was restored -- exactly
        # the case `_MEMPOOL_FULL_REASON`'s own comment names, Core's
        # `TX_RECONSIDERABLE` "mempool full". btclib-org/btclib-node#294
        raise RpcError(RPCErrorCode.VERIFY_REJECTED, _MEMPOOL_FULL_REASON)
    node.p2p_manager.broadcast_raw_transaction(to_announce, fee)
    return tx.id.hex()


def ping(node: Node, conn: RpcConnection, _: list[Any]) -> None:
    """Answer `ping` by sending every peer a fresh one, via `ping_all`.

    A peer at `BIP0031_VERSION` or below is sent none, where Core sends
    it a `ping` with no nonce. btclib-org/btclib-node#1204

    Called on `Node`'s own thread, `handle_rpc`'s the same as every
    handler here; `ping_all` is defined on `P2pManager` but reaches this
    one call site as a plain method call, not a coroutine scheduled on
    that manager's own loop.
    """
    node.p2p_manager.ping_all()


def stop(node: Node, conn: RpcConnection, _: list[Any]) -> str:
    """Answer `stop`; `handle_rpc` waits for this reply before stopping."""
    return "Btclib node stopping"


callbacks = {
    "getbestblockhash": get_best_block_hash,
    "getblockcount": get_block_count,
    "getblockchaininfo": get_blockchain_info,
    "pruneblockchain": prune_blockchain,
    "getblockhash": get_block_hash,
    "getblockheader": get_block_header,
    "getblock": get_block,
    "submitblock": submit_block,
    "getpeerinfo": get_peer_info,
    "getconnectioncount": get_connection_count,
    "getnetworkinfo": get_network_info,
    "addnode": add_node,
    "getmempoolinfo": get_mempool_info,
    "getrawmempool": get_raw_mempool,
    "getrawtransaction": get_raw_transaction,
    "gettxoutsetinfo": get_tx_out_set_info,
    "testmempoolaccept": test_mempool_accept,
    "sendrawtransaction": send_raw_transaction,
    "ping": ping,
    "stop": stop,
}
