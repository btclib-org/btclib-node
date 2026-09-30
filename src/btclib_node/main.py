# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`update_chain`, called once per pass of `Node`'s own loop.

Builds a fork's contextual detail, validates it block by block through
`interpreter.check_transactions`, reconciles the mempool across
whatever it adds and removes, and announces the added blocks to every
connected peer that lacks them once the node is out of initial block
download.
`verify_mempool_acceptance` is the same validation path
entered from a single transaction instead, for the RPC and p2p callbacks
that relay one.
"""

import secrets
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple, cast

from btclib.block import (
    coinbase_witness_commitment,
    header_at_height,
    median_time_past,
)
from btclib.block.block_context import BlockContext
from btclib.block.limits import MAX_BLOCK_SIGOPS_COST
from btclib.consensus import MAX_BLOCK_WEIGHT, WITNESS_SCALE_FACTOR, subsidy
from btclib.exceptions import BTClibException, BTClibValueError
from btclib.fee import fee_from_vsize
from btclib.p2p.inventory import Headers, Inv, Inventory, InventoryType
from btclib.script.engine.flags import ScriptFlag
from btclib.tx.limits import COINBASE_MATURITY
from btclib.tx.tx_context import (
    assert_coinbase_maturity,
    assert_coinbase_value,
    assert_sequence_locks,
    is_final,
)

from btclib_node.block_db import Coin
from btclib_node.chains import SigNet
from btclib_node.chainstate.block_index import BlockIndex, BlockStatus, block_time
from btclib_node.constants import (
    MAX_TIP_AGE,
    MIN_BLOCKS_TO_KEEP,
    NodeStatus,
    P2pConnStatus,
)
from btclib_node.exceptions import (
    ChainstateInconsistencyError,
    InvalidBlockInputError,
    MissingPrevoutError,
    PrevoutCountMismatchError,
    TxRejectedError,
)
from btclib_node.interpreter import (
    check_transaction,
    check_transactions,
    get_flags,
    sig_op_cost,
)
from btclib_node.mempool import format_money
from btclib_node.p2p.block_availability import (
    peer_has_header,
    process_block_availability,
)
from btclib_node.p2p.compact_block import compact_block
from btclib_node.p2p.protocol_version import (
    INVALID_CB_NO_BAN_VERSION,
    common_version,
)
from btclib_node.signet import assert_valid_solution

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib.block import Block, BlockHeader
    from btclib.p2p.compact_blocks import CmpctBlock
    from btclib.tx.tx import Tx
    from btclib.tx.tx_out import TxOut

    from btclib_node import Node
    from btclib_node.block_db import RevBlock
    from btclib_node.chains import Chain
    from btclib_node.chainstate.filter_index import FilterIndex
    from btclib_node.chainstate.utxo_index import UtxoIndex
    from btclib_node.p2p.block_availability import BlockAvailability

__all__ = [
    "MempoolAcceptance",
    "assert_valid_block",
    "contextual_check_block",
    "is_block_failed",
    "is_block_mutated",
    "is_cached_invalid",
    "new_pow_valid_block",
    "parent_lookup",
    "passes_check_block",
    "prune_up_to_height",
    "update_chain",
    "verify_mempool_acceptance",
]


# update_chain calls this on the failure path, naming the block whose
# contextual validation just failed. BlockIndex.invalidate is where
# what that costs is decided -- the block itself and every candidate
# already built on top of it; this is the one caller of it that has a
# freshly-failed hash to hand it. btclib-org/btclib-node#120
def update_header_index(index: BlockIndex, invalid_hash: bytes) -> None:
    """Invalidate the block `update_chain`'s own trial loop just failed on."""
    index.invalidate(invalid_hash)


# Core's own `MAX_BLOCKS_TO_ANNOUNCE` (`src/net_processing.cpp:152`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
_MAX_BLOCKS_TO_ANNOUNCE = 8


# _after_tip_change calls this, out of initial block download, with
# every block one of update_chain's own calls just put on the active
# chain, never an empty list: get_fork_details' own add list always
# carries at least the candidate's own hash. Core's `UpdatedBlockTip`
# queues the newest `_MAX_BLOCKS_TO_ANNOUNCE` of them for every peer,
# and `SendMessages` announces them (`src/net_processing.cpp`, "Try
# sending block announcements via headers", same tag): to a peer that
# asked for headers (callbacks.sendheaders), every header from the
# first one it does not have, where that one's parent is a header it
# has; to any other peer, or where nothing connects, an `inv` of the
# tip, unless the peer has it. To a peer that chose this node as a
# high-bandwidth peer (callbacks.sendcmpct), a single header to send goes
# as the tip's `cmpctblock` instead, and a single new block is sent that
# way whether or not the peer asked for headers. A peer that announced
# the blocks to this node therefore hears nothing back, and so does a
# peer `new_pow_valid_block` below already sent the block to.
# btclib-org/btclib-node#202, btclib-org/btclib-node#1160,
# btclib-org/btclib-node#1223
def _announce_added_blocks(node: Node, blocks: list[Block]) -> None:
    block_index = node.chainstate.block_index
    headers = [block.header for block in blocks[-_MAX_BLOCKS_TO_ANNOUNCE:]]
    tip_hash = headers[-1].hash
    # one nonce for every peer, as Core's `m_most_recent_compact_block`
    # gives where it holds the block; this node keeps none between calls
    # (btclib-org/btclib-node#1336)
    compact: CmpctBlock | None = None
    for conn in node.p2p_manager.connections.copy().values():
        state = conn.block_availability
        process_block_availability(block_index, state)
        high_bandwidth = conn.requested_hb_cmpctblocks
        to_send = (
            _headers_to_announce(block_index, state, headers)
            if conn.prefers_headers or (high_bandwidth and len(headers) == 1)
            else None
        )
        if to_send is None:
            if not peer_has_header(block_index, state, tip_hash):
                conn.send(Inv([Inventory(InventoryType.MSG_BLOCK, tip_hash)]))
        elif len(to_send) == 1 and high_bandwidth:
            if compact is None:
                compact = compact_block(blocks[-1], secrets.randbits(64))
            conn.send(compact)
            state.best_header_sent = tip_hash
        elif to_send:
            conn.send(Headers(to_send))
            state.best_header_sent = to_send[-1].hash


def new_pow_valid_block(node: Node, block: Block) -> None:
    """Send a block just stored to every high-bandwidth peer, before connecting.

    Core's `AcceptBlock` calls `NewPoWValidBlock` for a new block that
    passed `CheckBlock` and `ContextualCheckBlock`, out of initial block
    download and where the block extends the active tip
    (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    `NewPoWValidBlock` (`src/net_processing.cpp`, same tag) goes no lower
    than the highest block it already announced this way, nor below
    segwit's height. It sends the block's `cmpctblock` to every fully
    connected peer from `INVALID_CB_NO_BAN_VERSION` up that asked for high
    bandwidth, has the parent and lacks the block. That peer then counts
    as having the block, so `_announce_added_blocks` does not send it
    again once the block is connected.

    `callbacks.block` and `submitblock` call this with a block that passed
    `Block.assert_valid`. A block failing `contextual_check_block` is not
    announced, and `update_chain` refuses it when it reaches it.
    """
    block_index = node.chainstate.block_index
    previous_hash = block.header.previous_block_hash
    if node.is_initial_block_download or previous_hash != block_index.active_chain[-1]:
        return
    block_hash = block.header.hash
    height = block_index.get_block_info(block_hash).index
    try:
        contextual_check_block(node, block, height)
    except BTClibException:
        return
    if height <= node.highest_fast_announce:
        return
    node.highest_fast_announce = height
    if height < node.chain.consensus.segwit_height:
        return
    # one nonce for every peer, as Core's `pcmpctblock`, which Core also
    # keeps for later requests and this node does not
    # (btclib-org/btclib-node#1336)
    compact: CmpctBlock | None = None
    for conn in node.p2p_manager.connections.copy().values():
        if (
            conn.status != P2pConnStatus.Connected
            or common_version(conn) < INVALID_CB_NO_BAN_VERSION
        ):
            continue
        state = conn.block_availability
        process_block_availability(block_index, state)
        if (
            conn.requested_hb_cmpctblocks
            and not peer_has_header(block_index, state, block_hash)
            and peer_has_header(block_index, state, previous_hash)
        ):
            if compact is None:
                compact = compact_block(block, secrets.randbits(64))
            conn.send(compact)
            state.best_header_sent = block_hash


def _headers_to_announce(
    block_index: BlockIndex, state: BlockAvailability, headers: list[BlockHeader]
) -> list[BlockHeader] | None:
    """Return the headers from the first one the peer lacks, `None` for an inv.

    Empty where the peer has every one of them.
    """
    for i, header in enumerate(headers):
        if peer_has_header(block_index, state, header.hash):
            continue
        if peer_has_header(block_index, state, header.previous_block_hash):
            return headers[i:]
        return None
    return []


def finish_sync(node: Node) -> None:
    """Mark the node `BlockSynced`, once there is no candidate left to try.

    Only from `HeaderSynced`: a fresh node finds no candidate on its
    first pass, before any peer could have sent it a header, and a node
    with no peer stays at `SyncingHeaders` whatever `submitblock` hands
    it -- neither has finished a sync.

    A no-op past the first call: nothing here needs undoing if a later
    reorg leaves the chain with a candidate again, `NodeStatus` having
    no state to walk back to from `BlockSynced`.
    """
    if node.status != NodeStatus.HeaderSynced:
        return
    node.status = NodeStatus.BlockSynced


def update_ibd_status(node: Node) -> None:
    """Latch `node.is_initial_block_download` to `False`, and never back.

    Core's own `ChainstateManager::UpdateIBDStatus`
    (`src/validation.cpp:3302`, at bitcoin/bitcoin@ca7162cde5): once the
    active chain's own tip carries at least `node.chain.consensus`'s own
    `minimum_chain_work` (`btclib.consensus`) and is no older than
    `MAX_TIP_AGE` (`constants.py`), this node counts as caught up --
    `CChain::IsTipRecent`, `src/chain.h:431`, same commit. A no-op past
    the first call for the same reason `finish_sync` above is one:
    Core's function never sets its own cached flag back to `true`
    either, `UpdateIBDStatus`'s own comment naming that explicitly.

    Called by `_after_tip_change` whenever a fork commits, where Core's
    `ConnectTip` and `DisconnectTip` call it, and by
    `settle_at_no_candidate`, which reaches a tip no fork has moved yet:
    the one loaded from disk, where Core's `LoadChainTip` calls it
    (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    if not node.is_initial_block_download:
        return
    block_index = node.chainstate.block_index
    tip_hash = block_index.active_chain[-1]
    if block_index.chainwork[tip_hash] < node.chain.consensus.minimum_chain_work:
        return
    tip_header = block_index.header_dict[tip_hash].header
    if datetime.now(UTC) - tip_header.time > MAX_TIP_AGE:
        return
    node.is_initial_block_download = False


def settle_at_no_candidate(node: Node) -> None:
    """Run `finish_sync` and `update_ibd_status` together, at one call site.

    The two are separate latches over separate conditions, and "there
    is no candidate left to beat the active chain right now" is reason
    to check both, so this is what `_ready_fork` and `update_chain`
    below each call.
    """
    finish_sync(node)
    update_ibd_status(node)


# every hash here was just checked downloaded, or was on the active
# chain this is replacing, so block_db holds it; the type is wider than
# that invariant
def _blocks_to_add(node: Node, to_add_hash: list[bytes]) -> list[Block]:
    to_add: list[Block] = []
    for block_hash in to_add_hash:
        block = node.block_db.get_block(block_hash)
        if block is None:
            err_msg = f"block just checked downloaded is missing: {block_hash.hex()}"
            raise ChainstateInconsistencyError(err_msg)
        to_add.append(block)
    return to_add


# tip first: an output the branch created may have been spent again
# further along it, and the block that spent it has to be undone before
# the block that made it. `remove_from_active_chain` asks for the same
# order, and refuses anything but the tip
def _rev_blocks_to_remove(node: Node, to_remove_hash: list[bytes]) -> list[RevBlock]:
    to_remove: list[RevBlock] = []
    for block_hash in reversed(to_remove_hash):
        rev_block = node.block_db.get_rev_block(block_hash)
        if rev_block is None:
            err_msg = (
                f"no reverse patch for a block on the active chain: {block_hash.hex()}"
            )
            raise ChainstateInconsistencyError(err_msg)
        to_remove.append(rev_block)
    return to_remove


# _after_tip_change's own step, once a fork has actually connected:
# every abandoned block's own transactions rejoin the mempool where they
# still verify, and every newly-connected block's own transactions
# leave it, mirroring what connecting them to the chain already made
# true of the UTXO set they are checked against.
def _reconcile_mempool_for_reorg(
    node: Node, to_remove: list[RevBlock], to_add: list[Block]
) -> None:
    # oldest-abandoned-block first, the opposite of to_remove's own
    # tip-first order above: a transaction from a later abandoned
    # block may spend an output only an earlier abandoned block's
    # transaction created, and verify_mempool_acceptance below has
    # to find that parent already back in the mempool or it reads
    # as one more permanently invalid transaction. Core re-adds the
    # same way, walking its disconnectpool "in reverse, so that we
    # add transactions back to the mempool starting with the
    # earliest transaction that had been previously seen in a
    # block" (MaybeUpdateMempoolForReorg, src/validation.cpp).
    for rev_block in reversed(to_remove):
        removed_block = node.block_db.get_block(rev_block.hash)
        if removed_block is None:
            err_msg = f"block just removed is missing: {rev_block.hash.hex()}"
            raise ChainstateInconsistencyError(err_msg)
        for tx in removed_block.transactions[1:]:
            # a coinbase is never a mempool entrant on any path
            # into it, and one that is only valid on the branch
            # just abandoned is never valid again: the output it
            # spent no longer exists on any chain. Every other
            # entrant is checked before it is trusted, and this is
            # the one path into the mempool that skipped that.
            # btclib-org/btclib-node#85
            try:
                # Core's own `bypass_limits=true` for this re-add
                # (`MaybeUpdateMempoolForReorg`, `src/validation.cpp`,
                # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a
                # transaction a block already carried is not held to the
                # feerate floor a newcomer is. btclib-org/btclib-node#1245
                fee, vsize = verify_mempool_acceptance(node, tx, bypass_limits=True)
            except MissingPrevoutError, BTClibValueError:
                continue
            # Core's own `nHeight`, the active chain's own tip height at
            # acceptance (`Mempool.heights`' own docstring,
            # btclib-org/btclib-node#1397): read again here rather than
            # carried from `verify_mempool_acceptance`'s own
            # `spend_height`, one past it, because this loop moves the
            # active chain one block at a time as it re-adds.
            tip_height = len(node.chainstate.block_index.active_chain) - 1
            node.mempool.add_tx(tx, fee, vsize, height=tip_height)
    for block in to_add:
        # an empty mempool holds none of them, and `remove_tx` hashes
        # each transaction to ask, which a block connected during
        # initial block download would pay for every transaction
        if node.mempool.size:
            for tx in block.transactions[1:]:
                node.mempool.remove_tx(tx)
                node.mempool.remove_conflicts(tx)
        # Core's own `removeForBlock` (`src/txmempool.cpp:405-427`,
        # at bitcoin/bitcoin@58a7869f86): once per block connected,
        # whether or not it held anything this mempool was also
        # holding, restarting `Mempool.get_min_fee_rate`'s own decay
        # clock -- not folded into `remove_tx` above, which already
        # runs once per transaction rather than once per block.
        # btclib-org/btclib-node#294
        node.mempool.note_block_connected()


# update_chain's own step once a fork has committed, whatever
# `node.status` says. Core's `ConnectTip` and `DisconnectTip` run
# `UpdateIBDStatus` as the tip moves, and the mempool is reconciled with
# no gate, by `ConnectTip`'s `removeForBlock` and
# `ActivateBestChainStep`'s `MaybeUpdateMempoolForReorg`;
# `PeerManagerImpl::UpdatedBlockTip` then reads that latch: "Don't relay
# inventory during initial block download." (`src/validation.cpp`,
# `src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
# tag). `PeerManagerImpl::BlockConnected`, once per block connected,
# decays the block stalling timeout. btclib-org/btclib-node#1144,
# btclib-org/btclib-node#1148, btclib-org/btclib-node#1179
def _after_tip_change(
    node: Node, to_remove: list[RevBlock], to_add: list[Block]
) -> None:
    update_ibd_status(node)
    for _ in to_add:
        node.download_manager.block_connected()
    _reconcile_mempool_for_reorg(node, to_remove, to_add)
    if not node.is_initial_block_download:
        _announce_added_blocks(node, to_add)


# update_chain's own commit step, once the trial loop above has gone
# through every block in the fork without raising or being asked to
# stop: block_db is its own KeyValueStore, on its own datadir file, so
# it cannot share chainstate's write_batch here -- but it gets the same
# held-until-known-good treatment: the reverse patches add_rev_block
# buffered during the trial only reach disk once the branch they belong
# to is the one that connected. btclib-org/btclib-node#200
#
# block_index's own status change is staged, not written, on every
# call -- stage_status rather than set_status -- and chainstate.flush
# only runs once utxo_index.should_flush says the staged UTXO cache has
# reached its own bound, writing block_index and filter_index in the
# same batch the UTXO cache flushes in rather than once per block. This
# is what btclib-org/btclib-node#586 is about, and db.py's own docstring
# is where what a crash before that flush costs is decided: block_db's
# own rev patches above are not held back the same way, and do not need
# to be -- the docstring argues why. add_rev_block is idempotent against
# a hash already on disk, which is what a redo after such a crash relies
# on rather than anything special this function does for it.
def _finalize_fork(node: Node, to_add: list[Block], to_remove: list[RevBlock]) -> None:
    block_index = node.chainstate.block_index
    utxo_index = node.chainstate.utxo_index
    node.logger.debug("Start chainstate finalize")
    node.block_db.finalize()
    for rev_block in to_remove:
        block_index.remove_from_active_chain(rev_block.hash)
        block_index.stage_status(rev_block.hash, BlockStatus.valid)
        node.logger.debug("Removed block %s", rev_block.hash.hex())
    for block in to_add:
        block_hash = block.header.hash
        block_index.add_to_active_chain(block_hash)
        block_index.stage_status(block_hash, BlockStatus.in_active_chain)
        node.logger.info("Added block %s", block_hash.hex())
        # Core's `BlockConnected` stamping `m_last_tip_update`
        node.download_manager.last_tip_update = time.time()
    # `Node.best_height`'s own comment (`__init__.py`) is where reading
    # this cross-thread, off `active_chain` rather than off a lock, is
    # argued -- this call is the "tip changed" moment that comment cites.
    # btclib-org/btclib-node#722
    node.best_height = len(block_index.active_chain) - 1
    if utxo_index.should_flush():
        node.chainstate.flush()
    node.logger.debug("End chainstate finalize")


def prune_up_to_height(node: Node, target_height: int) -> None:
    """Delete block and undo data up to `target_height`, clearing `downloaded`.

    The one write path `_prune_chain`'s own automatic-target walk below
    and `rpc.callbacks.prune_blockchain`'s manual call share: both need
    the same pairing, in the same order.

    Core's own `BlockManager::PruneOneBlockFile`
    (`node/blockstorage.cpp:270-286`, at bitcoin/bitcoin@ca7162cde5) clears
    `BLOCK_HAVE_DATA`/`BLOCK_HAVE_UNDO` on the `CBlockIndex` entry it
    prunes, "any block we prune would have to be downloaded again in
    order to consider its chain" -- matched here by clearing
    `BlockInfo.downloaded` for the same range `block_db.prune_up_to`
    below is about to delete, over `block_db.pruned_up_to` the same way
    that call's own idempotency check is, before the data itself is
    gone. `p2p.callbacks.block`'s own no-op-if-downloaded guard is the
    reader this matters to: without this, a block re-offered after its
    data was pruned would be silently discarded rather than re-stored.

    Height 0 included: genesis is in `block_db` like any other block
    (`Node.load`), and Core's own `GetPruneRange`
    (`src/validation.cpp:6382`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag) starts the prunable range at height 0 on a chain not built from
    a snapshot.
    """
    block_index = node.chainstate.block_index
    for height in range(node.block_db.pruned_up_to + 1, target_height + 1):
        block_hash = block_index.active_chain[height]
        block_index.set_downloaded(block_hash, downloaded=False)
    node.block_db.prune_up_to(target_height, block_index.active_chain.__getitem__)


# update_chain's own finalize-branch step, run right after _finalize_fork:
# Core's own prune step inside Chainstate::FlushStateToDisk
# (src/validation.cpp, at bitcoin/bitcoin@ca7162cde5) -- a no-op unless
# fPruneMode/Config.pruned says this node prunes at all, and never
# reaching back past MIN_BLOCKS_TO_KEEP (constants.py, 288), the same
# depth Core's own FindFilesToPrune is bounded by. A fork replacing the
# last MIN_BLOCKS_TO_KEEP blocks still finds what it needs on disk, the
# same guarantee that retained depth gives Core's own pruned node against
# an ordinary reorg; a reorg deeper than that finds its own missing
# blocks and fails on this node exactly as it does on Core's -- pruning
# trades away that depth of reorg safety by its own nature, on both, and
# nothing here is a new gap this call opens.
def _prune_chain(node: Node) -> None:
    """Delete block and undo data, never `MIN_BLOCKS_TO_KEEP` behind the tip.

    `Config.prune_target_mib` set (Core's own `-prune=<n>`,
    `n >= MIN_PRUNE_TARGET_MIB`) routes to `_prune_to_target` below,
    which stops once actual usage is back under the target rather than
    always reaching every height this bound would allow. `None` -- Core's
    own manual pruning, `-prune=1` -- deletes nothing here on its own at
    all; only `rpc.callbacks.prune_blockchain` does, and only when asked.
    """
    if not node.config.pruned:
        return
    prune_target_mib = node.config.prune_target_mib
    if prune_target_mib is None:
        return
    block_index = node.chainstate.block_index
    max_height = len(block_index.active_chain) - 1 - MIN_BLOCKS_TO_KEEP
    if max_height < 0:
        return
    _prune_to_target(node, max_height, prune_target_mib)


def _prune_to_target(node: Node, max_height: int, prune_target_mib: int) -> None:
    """Delete oldest-first until `current_usage` is under the MiB target.

    Core's own `BlockManager::FindFilesToPrune`
    (`node/blockstorage.cpp:332-386`, at bitcoin/bitcoin@ca7162cde5) walks
    its block files oldest first, pruning whole files -- and stopping,
    file by file, the moment `nCurrentUsage` (`CalculateCurrentUsage`,
    same file:811-818) is back under `target` -- never past
    `last_block_can_prune` (`Chainstate::GetPruneRange`,
    `validation.cpp:6376-6395`, same sha), the same `MIN_BLOCKS_TO_KEEP`
    depth `max_height` above already is here. This store's own files
    rotate by append order rather than by height
    (`block_db`'s own module docstring), so there is no file-by-file walk
    to mirror directly; walking height by height instead and checking
    `block_db.current_usage` after every one reaches the same "stop once
    under target" behaviour, at finer granularity than Core's own
    per-file check rather than coarser -- actual bytes still only drop
    once a file's own last live block or reverse patch is gone
    (`block_db._release`), the same as Core's.

    Not reproduced: Core's own `nBuffer`, headroom left under `target`
    for the next `BLOCKFILE_CHUNK_SIZE`/`UNDOFILE_CHUNK_SIZE`
    preallocation before the next check
    (`node/blockstorage.cpp:363-364`, same sha). This store never
    preallocates -- `block_db`'s own `__add_data_to_file` appends exactly
    what it is given -- and `_prune_chain` above calls this after every
    connected block, so there is no gap between one check and the next
    for un-budgeted growth to hide in the way a buffer would guard
    against.
    """
    target_bytes = prune_target_mib * 1024 * 1024
    height = node.block_db.pruned_up_to + 1
    while height <= max_height and node.block_db.current_usage() >= target_bytes:
        prune_up_to_height(node, height)
        height += 1


def _finalize_fork_and_prune(
    node: Node, to_add: list[Block], to_remove: list[RevBlock]
) -> None:
    """Commit a trial, then prune -- one call for `update_chain` below.

    Pulled apart into `_finalize_fork` and `_prune_chain` above, each unit
    tested on its own; folded back into one call here only so `update_chain`
    counts one statement for both, under ruff's own `PLR0915`.
    """
    _finalize_fork(node, to_add, to_remove)
    _prune_chain(node)


# update_chain's own trial marks, taken before a trial starts: should_flush
# may have left utxo_index and filter_index each holding an earlier,
# already-succeeded trial's own staged changes, unflushed, and a rollback
# on failure must undo only what this trial itself stages --
# UtxoIndex.trial_mark's own docstring argues why a blanket wipe is no
# longer safe once staging survives more than one trial
# (btclib-org/btclib-node#586).
def _pre_trial_marks(
    utxo_index: UtxoIndex, filter_index: FilterIndex
) -> tuple[int, int]:
    """Read the rollback marks this trial would undo to, if it fails."""
    return utxo_index.trial_mark(), filter_index.trial_mark()


# update_chain's own failure path: block_db whole, the other two back to
# the marks _pre_trial_marks read before the trial started. block_db
# needs no mark of its own: _finalize_fork calls block_db.finalize on
# every success, unconditionally, so pending_rev_blocks is always empty
# by the time a new trial starts.
def _rollback_trial(node: Node, utxo_mark: int, filter_mark: int) -> None:
    """Undo a failed trial, without touching an earlier trial's own staging."""
    node.block_db.rollback()
    node.chainstate.utxo_index.rollback(utxo_mark)
    node.chainstate.filter_index.rollback(filter_mark)


# update_chain's own leading gate: whether there is a fork to try at
# all, and whether every block it would need has actually arrived.
# `finish_sync` is called here rather than merely signalled, since a
# missing candidate and a candidate not yet fully downloaded mean
# different things to update_chain's own caller but the same thing to
# this one -- "nothing to do yet" -- and only the first of them is also
# "nothing left to ever do until a new header arrives".
#
# Not gated on `node.status`: Core's `ProcessNewBlock` hands every block
# it accepts to `ActivateBestChain` (`src/validation.cpp:4481`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) whether or not header sync
# has finished and whether or not any peer is connected, which is what
# lets `submitblock` on a node with no peer connect its own block
# (btclib-org/btclib-node#1071).
def _ready_fork(node: Node) -> tuple[list[bytes], list[bytes]] | None:
    block_index = node.chainstate.block_index
    first_candidate = block_index.get_first_candidate()
    if not first_candidate:
        settle_at_no_candidate(node)
        return None

    to_add_hash, to_remove_hash = block_index.get_fork_details(
        first_candidate.header.hash
    )

    for block_hash in to_add_hash:
        if not block_index.get_block_info(block_hash).downloaded:
            # get_first_candidate prefers a branch whose tip has
            # arrived, so a branch missing its tip is stepped over; a
            # branch missing a block behind its tip is not, and until
            # that block arrives nothing queued behind it connects,
            # however complete: btclib-org/btclib-node#121
            return None

    return to_add_hash, to_remove_hash


def parent_lookup(node: Node) -> Callable[[BlockHeader], BlockHeader]:
    """Return a callable stepping from a known header back to its parent's.

    `_validate_block` and `verify_mempool_acceptance` below, and
    `rpc.callbacks.get_blockchain_info`, each need this for
    `median_time_past`: `header_dict` holds every header this node has
    ever indexed, active chain or not, so this reaches a trial fork's
    own earlier blocks as readily as long-committed history -- unlike
    `active_chain`, which still reads as the chain before this trial
    until `_finalize_fork` runs. Not underscore-prefixed:
    `rpc.callbacks` is a different module, and importing a name it does
    not own would be the private-name import this codebase's own ruff
    configuration (`select = ["ALL"]`) already refuses elsewhere.
    """
    header_dict = node.chainstate.block_index.header_dict

    def parent_of(header: BlockHeader) -> BlockHeader:
        return header_dict[header.previous_block_hash].header

    return parent_of


# the two 2010 blocks Chain.consensus.bip30_exceptions names are the
# only ones this node ever lets past UtxoIndex.add_block's own BIP30
# check -- add_block's own docstring is where that check and the
# exception are argued. A function of its own rather than inline in
# update_chain, which ruff's own too-many-statements already counts
# every statement gained here against.
def _check_bip30(node: Node, index: int, block_hash: bytes) -> bool:
    """Whether `block_hash`, connecting at `index`, is checked for BIP30."""
    return (index, block_hash) not in node.chain.consensus.bip30_exceptions


# the non-witness size of a transaction that could pass for an inner merkle
# node, and the size of the witness reserved value, in `IsBlockMutated` and
# `CheckWitnessMalleation` (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7)
_INNER_NODE_SIZE = 64
_WITNESS_NONCE_SIZE = 32


def is_block_mutated(block: Block, *, check_witness_root: bool) -> bool:
    """Whether `block`'s body is not the one its header commits to.

    Core's `IsBlockMutated` (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a merkle root that is not
    the transactions', or CVE-2012-2459's duplicate; a block without a
    coinbase holding a 64-byte transaction; a witness commitment the
    coinbase witness does not match, read only where `check_witness_root`
    (segwit active after the parent); and a witness in a block no
    commitment was read for. Anyone can pair an honest header with such a
    body, so it says nothing about the header: Core refuses the body and
    leaves the header's status alone.
    """
    transactions = block.transactions
    if not transactions:
        # Core's merkle root of no transactions is the null hash, where
        # btclib's refuses to compute one
        return block.header.merkle_root != bytes(32)
    try:
        block.assert_valid_merkle_root()
    except BTClibValueError:
        return True
    if not transactions[0].is_coinbase:
        return any(
            len(tx.serialize(include_witness=False, check_validity=False))
            == _INNER_NODE_SIZE
            for tx in transactions
        )
    commitment = block.witness_commitment if check_witness_root else None
    if commitment is None:
        return any(tx.is_segwit for tx in transactions)
    stack = transactions[0].vin[0].script_witness.stack
    if len(stack) != 1 or len(stack[0]) != _WITNESS_NONCE_SIZE:
        return True
    return coinbase_witness_commitment(transactions, stack[0]) != commitment


def passes_check_block(block: Block) -> bool:
    """Whether `block` passes what Core's `CheckBlock` asks of a body.

    The merkle root, `bad-blk-length`, `bad-cb-missing`,
    `bad-cb-multiple`, each transaction's `CheckTransaction` and
    `bad-blk-sigops`, in Core's order (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). The header is checked
    elsewhere: by `add_headers` if it is new, or when it was first
    indexed.
    """
    transactions = block.transactions
    try:
        block.assert_valid_merkle_root()
        block.assert_valid_length()
        if not transactions or not transactions[0].is_coinbase:
            return False
        if any(tx.is_coinbase for tx in transactions[1:]):
            return False
        for tx in transactions:
            tx.assert_valid()
        block.assert_valid_sig_op_count()
    except BTClibException:
        return False
    return True


def assert_valid_block(block: Block, chain: Chain) -> None:
    """Assert what `Block.assert_valid` asks, the signet solution spliced in.

    `Block.assert_valid`'s own three steps -- the header, its proof of
    work, then every other rule `passes_check_block` above already
    names -- with `signet.assert_valid_solution` run between the second
    and the third, on a signet chain only: Core's own `CheckBlock` asks
    it there too, gated on `consensusParams.signet_blocks`
    (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), right after the header's proof of work and before the merkle
    root. btclib carries no signet concept to splice this into
    `Block.assert_valid` itself, hence the decomposition here instead
    of a fourth argument to it -- the same three calls that function
    already makes, in the same order, for every chain but this one.
    """
    block.header.assert_valid()
    block.header.assert_valid_pow(chain.pow_limit_bits)
    if isinstance(chain, SigNet):
        assert_valid_solution(block, chain)
    block.assert_valid_structure()


def is_block_failed(block: Block, *, check_witness_root: bool) -> bool:
    """Whether `block`, failing `Block.assert_valid`, is marked failed.

    Core's `ProcessNewBlock` (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) never marks a block failing
    `CheckBlock`, "protective against consensus failure if there are any
    unknown forms of block malleability", and `AcceptBlock` marks one
    failing `ContextualCheckBlock` unless the failure is `BLOCK_MUTATED`.
    Of what `assert_valid` asks, that leaves the weight, which Core asks
    after the witness commitment that makes it a property of the header:
    a body that is not mutated, passes `CheckBlock` and is over the weight.
    """
    return (
        not is_block_mutated(block, check_witness_root=check_witness_root)
        and passes_check_block(block)
        and block.weight > MAX_BLOCK_WEIGHT
    )


def is_cached_invalid(block_index: BlockIndex, block: Block) -> bool:
    """Whether `block` is Core's `duplicate-invalid`, `BLOCK_CACHED_INVALID`.

    Its header indexed and marked invalid already, and its body passing
    `CheckBlock`, which Core's `ProcessNewBlock` asks before
    `AcceptBlock` reaches `AcceptBlockHeader` (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a body failing it is
    refused for that reason instead.
    """
    known = block_index.header_dict.get(block.header.hash)
    return (
        known is not None
        and known.status == BlockStatus.invalid
        and passes_check_block(block)
    )


# update_chain's own per-block gate, once a candidate's spends and
# creations are staged and its own height is known: every transaction's
# own finality via btclib.tx.tx_context.is_final (BIP113-aware) and its
# BIP68 relative lock via btclib.tx.tx_context.assert_sequence_locks,
# the two rules a height and a clock decide on their own through
# Block.assert_valid_contextual -- time-too-new, already checked on the
# header path (chainstate/block_index.py's own header validation), and
# bad-cb-height, wherever BIP34 binds (Chain.consensus.bip34_height, per
# network) -- a spend of a coinbase not yet COINBASE_MATURITY deep via
# btclib.tx.tx_context.assert_coinbase_maturity, this block's own
# scripts and amounts via interpreter.check_transactions, and a
# coinbase paying more than subsidy plus fees via
# btclib.tx.tx_context.assert_coinbase_value. BIP30 runs earlier still,
# inside utxo_index.add_block, before this is ever called: its own
# docstring is where that ordering and the two 2010 exceptions are
# argued. A function of its own rather than statements inline:
# update_chain's own trial loop is already long enough that PLR0915
# counts every statement gained here against it.
def _validate_block(
    node: Node, block: Block, transactions: list[tuple[list[Coin], Tx]], index: int
) -> None:
    block_hash = block.header.hash
    parent_mtp, bip113_active = contextual_check_block(node, block, index)

    block_index = node.chainstate.block_index
    parent_header = block_index.header_dict[block.header.previous_block_hash].header
    parent_height = index - 1
    parent_of = parent_lookup(node)

    def ancestor_median_time_past(height: int) -> int:
        header = header_at_height(parent_header, parent_height, height, parent_of)
        return median_time_past(header, height, parent_of)

    if bip113_active:
        for prevouts, tx in transactions:
            assert_sequence_locks(
                tx, prevouts, index, parent_mtp, ancestor_median_time_past
            )

    for prevouts, _tx in transactions:
        assert_coinbase_maturity(prevouts, index)
    check_transactions(transactions, index, node, block_hash)

    fees = sum(
        sum(coin.tx_out.value for coin in prevouts) - sum(x.value for x in tx.vout)
        for prevouts, tx in transactions
    )
    block_subsidy = subsidy(index, node.chain.consensus.subsidy_halving_interval)
    assert_coinbase_value(block.transactions[0], block_subsidy, fees)


def contextual_check_block(node: Node, block: Block, index: int) -> tuple[int, bool]:
    """Refuse what Core's `ContextualCheckBlock` refuses of `block` at `index`.

    `bad-txns-nonfinal`, then `bad-cb-height` wherever BIP34 binds, in
    Core's order (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag). The witness commitment and the weight, the other two rules
    there, are `Block.assert_valid`'s. `Block.assert_valid_contextual`,
    which asks `bad-cb-height`, asks `time-too-new` first: Core asks that
    one of the header, in `ContextualCheckBlockHeader`, which
    `BlockIndex.add_headers` has already done here, so a block reaching
    this passes it unless the clock went back. The block's parent is
    indexed, and the block need not be on the active chain. Answers the
    parent's median time past and whether BIP113 binds, which
    `_validate_block`'s sequence locks read too. Every caller reaches
    `bad-cb-height` through this one call: there is no second
    `assert_valid_contextual` left in `_validate_block` to translate it.
    """
    block_hash = block.header.hash
    block_index = node.chainstate.block_index
    parent_header = block_index.header_dict[block.header.previous_block_hash].header
    parent_mtp = median_time_past(parent_header, index - 1, parent_lookup(node))

    # Core deploys BIP68, BIP112 (the CHECKSEQUENCEVERIFY opcode) and
    # BIP113 (this cutoff) together, as one soft fork -- this tree has
    # no BIP9 deployment tracking of its own, so the height get_flags
    # already turns the opcode on at is read here too, rather than a
    # second activation table naming the same height for the same fork.
    # btclib.tx.tx_context's own module docstring argues this the same
    # way, for why assert_sequence_locks in _validate_block takes no
    # enforce_bip68 flag of its own: the caller skips the call entirely
    # rather than passing one.
    bip113_active = ScriptFlag.CHECKSEQUENCEVERIFY in get_flags(
        node.config, index, block_hash
    )
    lock_time_cutoff = parent_mtp if bip113_active else block_time(block.header)
    for tx in block.transactions:
        if not is_final(tx, index, lock_time_cutoff):
            err_msg = "bad-txns-nonfinal"
            raise BTClibValueError(err_msg)
    # Core's own ContextualCheckBlock checks finality before the
    # coinbase height commitment (src/validation.cpp,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which is why this
    # sits after the loop above rather than ahead of it. bad-cb-height is
    # Core's own literal reason for the one failure this call can
    # actually reach: bad-diffbits and time-too-old stay unchecked here
    # (median_time_past and required_bits are never supplied), and
    # time-too-new -- the one rule this call still asks unconditionally
    # -- is already refused on the header path, so it cannot be why this
    # ever raises; the message is checked before translating it rather
    # than assumed, in case that invariant is ever wrong.
    try:
        block.assert_valid_contextual(
            BlockContext(index, datetime.now(UTC), node.chain.consensus.bip34_height)
        )
    except BTClibValueError as error:
        if index >= node.chain.consensus.bip34_height and "coinbase height" in str(
            error
        ):
            err_msg = "bad-cb-height"
            raise BTClibValueError(err_msg) from error
        raise  # pragma: no cover -- unreachable per the comment above
    return parent_mtp, bip113_active


def _record_rejection(node: Node, failed_hash: bytes, exc: BaseException) -> None:
    """Record the block `failed_hash` names as refused, and why.

    `Node.load`'s own comment beside `last_rejected_block` says who
    reads it: a rejection test, asserting the rule that refused a block
    rather than only that one did. `_resolve_trial_exception`'s own
    call below is this function's only caller, and reaches it only once
    `isinstance(exc, _CONTENT_FAILURE)` already holds, which never
    happens before the to_add loop's own iteration has set
    `failed_hash` -- so unlike before btclib-org/btclib-node#623, there
    is no longer a raise this can be reached on where `failed_hash` is
    still `None`, and nothing here has to guard against one.
    """
    node.last_rejected_block = (failed_hash, exc)


# What the trial loop's to_add iteration deliberately raises to say a
# candidate's own content is bad, and nothing else: InvalidBlockInputError
# is utxo_index.add_block's own two checks (BIP30, a double spend inside
# the same block); PrevoutCountMismatchError and BTClibValueError are
# check_transactions and everything _validate_block calls -- amounts,
# scripts, coinbase value and maturity, finality, sequence locks, and
# Block.assert_valid_contextual, all of which raise BTClibValueError,
# btclib's own worker_pool.starmap round trip included, since
# btclib.exceptions' own docstring is why a btclib exception survives a
# process pool's pickling unchanged.
#
# Everything else the same iteration can raise is this node's own
# storage or bookkeeping, not a verdict on the candidate: db.py's
# StoreClosedError and StoreCorruptionError, whatever RocksDB or the
# filesystem raises out of a KeyValueStore read or write, and
# ChainstateInconsistencyError -- classification is by exception type,
# not by call site, which utxo_index.add_block's own self.db.get()
# still shows even though what it illustrates changed
# (btclib-org/btclib-node#650): that one read can raise
# StoreCorruptionError (storage, not a verdict) or feed a
# checksum-clean record Coin.parse still cannot parse into
# InvalidBlockInputError (one of the three above, matching
# CDBWrapper::Read/CCoinsViewDB::GetCoin's own "absent" rather than
# raising ChainstateInconsistencyError the way it used to before the
# store carried its own checksum). update_chain's own except below is
# what tells the two apart, by type rather than by call site, and
# never invalidates a block for the second kind. Core keeps the same
# distinction at the equivalent point of ConnectBlock (src/validation.cpp,
# at bitcoin/bitcoin@b91d983f66): every ordinary CheckBlock failure
# returns false and the block is rejected, but a BLOCK_MUTATED result --
# "we don't write down blocks to disk if they may have been corrupted, so
# this should be impossible unless we're having hardware problems" -- is
# FatalError instead. btclib-org/btclib-node#620
_CONTENT_FAILURE = (BTClibValueError, InvalidBlockInputError, PrevoutCountMismatchError)


def _resolve_trial_exception(
    node: Node, failed_hash: bytes | None, exc: Exception
) -> None:
    """Record `exc` against `failed_hash`, or re-raise it -- never both.

    A function of its own and not the `if`/`else` inline in `update_chain`'s
    own except -- ruff's own `too-many-branches`/`complex-structure`
    already count a branch gained there against a ceiling that call is
    already at. `isinstance(exc, _CONTENT_FAILURE)` is the to_add loop's
    own exceptions, argued where `_CONTENT_FAILURE` is declared, and is
    the only case this records and swallows; `cast` and not a runtime
    check narrows `failed_hash` for that call, because the to_add
    loop's own iteration always sets it before raising one of those
    three, the same invariant `_record_rejection`'s own docstring
    argues. The to_remove loop above `update_chain`'s own trial never
    raises one of those three -- `apply_rev_block` raises only
    `ChainstateInconsistencyError` or a storage failure -- so
    `failed_hash is None` here always falls to `raise exc` below, same
    as any other non-content failure from the to_add loop: undoing an
    already-connected block failing is this node's own bookkeeping,
    never a verdict on a new block, so nothing here would ever have
    recorded a rejection for it, but the raise itself no longer stops
    there either. Core's own equivalent -- `DisconnectTip` returning
    false is fatal one level up, in `ActivateBestChainStep`
    (`src/validation.cpp`, at bitcoin/bitcoin@b91d983f66) -- stops
    rather than keeps trying on top of storage it just proved
    inconsistent, the same conclusion already drawn above
    `to_add`/`to_remove` themselves for a read failure at the same
    citation. `raise exc` and not a bare `raise`: this is not itself an
    except block, so a bare `raise` here has no currently-handled
    exception of its own to reach for -- `exc` already carries the
    traceback `update_chain`'s own except caught it with, and raising
    it explicitly extends that same traceback rather than starting a
    new one.
    """
    if isinstance(exc, _CONTENT_FAILURE):
        _record_rejection(node, cast("bytes", failed_hash), exc)
        return
    raise exc


def update_chain(node: Node) -> None:
    """Try the best ready fork block by block, and commit or roll it back.

    Called once per pass of `Node`'s own loop. `_ready_fork` answers
    whether there is a fork worth trying at all; if there is, every
    block on it is applied to the UTXO set and validated in turn, a
    shutdown between two blocks stopping the trial without failing it.
    Every other exception rolls every index back to where it stood
    before this call; whether it also invalidates the block it happened
    on, or instead propagates out of this call once the rollback has
    run, is `_CONTENT_FAILURE`'s own distinction above. Once a trial
    succeeds, `_finalize_fork` commits it and `_after_tip_change` runs
    what Core runs as the tip moves.
    """
    fork = _ready_fork(node)
    if fork is None:
        return
    to_add_hash, to_remove_hash = fork

    block_index = node.chainstate.block_index
    utxo_index = node.chainstate.utxo_index
    filter_index = node.chainstate.filter_index

    node.logger.info("Start block validation")

    node.logger.debug("Start getting blocks")
    # Deliberately outside the try below, so a raise from either call
    # propagates out of update_chain, into Node._step_chain and out of
    # Node.run's own loop, rather than being caught and rolled back the
    # way a raise inside the trial is. Every hash the two functions are
    # given names a block this node already validated and wrote for
    # itself -- _blocks_to_add's and _rev_blocks_to_remove's own
    # comments say so -- so a raise here is this node's own storage
    # failing to give back what it wrote (a corrupt file, a disk error,
    # get_block/get_rev_block finding block_db's index disagreeing with
    # chainstate's), never the fork's content turning out bad: that
    # question is check_transactions', inside the try, and is answered
    # by rejecting the fork rather than by stopping the node.
    #
    # Bitcoin Core's own split (src/validation.cpp,
    # read at bitcoin/bitcoin@b91d983f66) is not symmetric between the two
    # directions this function tries a block in, and is cited as it
    # actually reads rather than tidied into one: ConnectTip answers a
    # failed read immediately, with FatalError. DisconnectTip answers
    # the same failure by returning plainly from inside itself --
    # FatalError for a disconnect lives one level up, in
    # ActivateBestChainStep, and covers a failed read, a failed
    # DisconnectBlock and a failed FlushStateToDisk alike, one fatal
    # condition over that caller's whole walk rather than over the read
    # alone. ActivateBestChainStep trying a heavier candidate block by
    # block is the path update_chain mirrors; DisconnectTip's other
    # caller, InvalidateBlock, answers that same read failure by
    # returning to the RPC layer instead, because an operator's own
    # explicit command is not the chain trying to advance itself, a
    # distinction this function has no counterpart to. So what Core
    # holds fatal is failing to walk its own chain while advancing it,
    # on either side of that walk, not a read specifically -- which is
    # the same claim made of _blocks_to_add and _rev_blocks_to_remove
    # above: stop rather than reject, because the question here is this
    # node's own storage, not the fork's content. btclib-org/btclib-node#452
    to_add = _blocks_to_add(node, to_add_hash)
    to_remove = _rev_blocks_to_remove(node, to_remove_hash)
    node.logger.debug("Got all blocks")

    node.logger.debug("Start chainstate test")

    success = True
    # set the moment a block starts and cleared once it is fully
    # through: an exception anywhere in its own iteration leaves it
    # naming the block that failed, which is what update_header_index
    # invalidates below. Never set by the to_remove loop -- a rollback
    # failing there is not a new block being bad.
    failed_hash: bytes | None = None
    utxo_mark, filter_mark = _pre_trial_marks(utxo_index, filter_index)
    # the block index's database write moves into the batch below and
    # nowhere in here: a status written on the way through reaches the
    # database before the branch is known to connect, and refusing the
    # branch does not take it back
    try:
        for rev_block in to_remove:
            utxo_index.apply_rev_block(rev_block)
        for block_hash, block in zip(to_add_hash, to_add, strict=True):
            # checked between blocks and not inside one: check_transactions
            # below is the blocking worker_pool.starmap over a whole
            # block's inputs, thousands of signature checks on mainnet,
            # and it is what makes a wait for this loop scale with the
            # fork rather than with one block. failed_hash is still the
            # previous iteration's None here, so breaking this way never
            # reaches update_header_index below: a shutdown is not a
            # validation failure, and must not invalidate the block it
            # happened to land on. btclib-org/btclib-node#139
            if node.terminate_flag.is_set():
                node.logger.info("Stopping mid-fork: rolling the trial back")
                success = False
                break
            failed_hash = block_hash
            index = block_index.get_block_info(block_hash).index
            transactions, rev_patch = utxo_index.add_block(
                block, index, check_bip30=_check_bip30(node, index, block_hash)
            )
            _validate_block(node, block, transactions, index)

            node.block_db.add_rev_block(rev_patch)
            # here and not on a pass of its own: the patch names the
            # output every input of this block spent, which is what a
            # BIP158 filter is built from and what a block does not
            # carry. Read back off the disk it would be the same octets
            # fetched twice.
            filter_index.add_connected_block(block, rev_patch)
            failed_hash = None

    except Exception as exc:
        node.logger.exception("Exception occurred")
        success = False
        _resolve_trial_exception(node, failed_hash, exc)
    finally:
        if success:
            _finalize_fork_and_prune(node, to_add, to_remove)
        else:
            node.logger.debug("Start chainstate rollback")
            _rollback_trial(node, utxo_mark, filter_mark)
            node.logger.debug("End chainstate rollback")

    node.logger.info("End block validation")

    if not success and failed_hash is not None:
        node.logger.debug("Start updating index")
        update_header_index(block_index, failed_hash)

    if success:
        _after_tip_change(node, to_remove, to_add)

    node.logger.debug("Finished main\n")

    if not block_index.get_first_candidate():
        settle_at_no_candidate(node)


# Core's own `MAX_STANDARD_TX_SIGOPS_COST` and `DEFAULT_BYTES_PER_SIGOP`
# (`src/policy/policy.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag):
# the most sigop cost a standard transaction may carry, and the bytes each
# unit of it weighs in the vsize. btclib-org/btclib-node#1357
_MAX_STANDARD_TX_SIGOPS_COST = MAX_BLOCK_SIGOPS_COST // 5
_BYTES_PER_SIGOP = 20


class MempoolAcceptance(NamedTuple):
    """What `verify_mempool_acceptance` answers for a candidate it accepts.

    `fee` in satoshi, and `vsize` Core's `GetVirtualTransactionSize(weight,
    sigop cost, DEFAULT_BYTES_PER_SIGOP)`, the size the mempool prices and
    counts the transaction by. btclib-org/btclib-node#1357
    """

    fee: int
    vsize: int


def verify_mempool_acceptance(
    node: Node, tx: Tx, *, bypass_limits: bool = False
) -> MempoolAcceptance:
    """Verify a transaction against its prevouts, return its fee and vsize.

    The fee is the sum of the inputs less the sum of the outputs, Core's
    own `CheckTxInputs` tally, refused where it is negative.
    btclib-org/btclib-node#260

    Checks finality and BIP68 against the tip Core's own mempool policy
    does (`CheckFinalTxAtTip`/`STANDARD_LOCKTIME_VERIFY_FLAGS`,
    `src/validation.cpp:156-175` and `policy/policy.h:137`,
    at bitcoin/bitcoin@204256c73f), both unconditionally rather than
    gated on any activation height, unlike `main._validate_block`'s own
    block-connect path: a mempool never holds a transaction from before
    a soft fork it has already activated, so Core's own mempool code
    does not ask either. `interpreter.check_transaction` reads the
    scripts the same way, against a flag set that consults no height.

    Each refusal is a `TxRejectedError` in Core's words, in the order
    Core's `MemPoolAccept` makes them (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), but for a missing input,
    `MissingPrevoutError`, which each caller answers for itself.
    btclib-org/btclib-node#1328

    Refuses a candidate whose txid the mempool already holds, as Core's
    `PreChecks` does ahead of its conflict checks, and one spending an
    outpoint a mempool transaction already spends,
    `Mempool.check_replacement` saying in whose words.

    Refuses a fee below the mempool's own rolling minimum or
    `Config.min_relay_feerate` for the transaction's vsize, Core's own
    `CheckFeeRate`, unless `bypass_limits` -- Core's own flag, set where
    a disconnected block's transactions rejoin the mempool.
    btclib-org/btclib-node#1245
    """
    prev_outputs: list[TxOut] = []
    # every prevout, coinbase or mempool-parented alike, aligned with
    # tx.vin one for one -- what assert_sequence_locks below needs.
    # A mempool parent's own height is not yet real, so it is stood in
    # for with spend_height itself: Core's own MEMPOOL_HEIGHT convention
    # (CalculatePrevHeights, src/validation.cpp:203-206, same commit) --
    # "assume all mempool transaction confirm in the next block" -- and
    # spend_height below is exactly that next block's own height.
    prevout_coins: list[Coin] = []

    block_index = node.chainstate.block_index
    utxo_index = node.chainstate.utxo_index
    mempool = node.mempool
    # the height a block extending the active chain would connect at:
    # active_chain[i] is the block at real height i (BlockIndex.__init__
    # seeds it with the genesis at index 0), so its own length already
    # is the tip's height plus one -- a further "+ 1" here would answer
    # one block past the real next height, and be wrong by exactly one
    # block for the coinbase maturity check, which is what surfaced it
    # (btclib-org/btclib-node#569)
    spend_height = len(block_index.active_chain)

    tip_hash = block_index.active_chain[-1]
    tip_header = block_index.header_dict[tip_hash].header
    tip_height = spend_height - 1
    parent_of = parent_lookup(node)
    tip_mtp = median_time_past(tip_header, tip_height, parent_of)

    if not is_final(tx, spend_height, tip_mtp):
        reason = "non-final"
        raise TxRejectedError(reason)

    # Core's own `PreChecks` order, after finality and ahead of its
    # conflict checks (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7,
    # the v31.1 tag): the held copy of this very transaction, same witness
    # or not, is no conflict of it. btclib-org/btclib-node#1244
    if mempool.contains_tx(tx):
        reason = "txn-already-in-mempool"
        raise TxRejectedError(reason)
    if tx.id in mempool.txid_index:
        reason = "txn-same-nonwitness-data-in-mempool"
        raise TxRejectedError(reason)

    for tx_in in tx.vin:
        prevout_bytes = tx_in.prev_out.serialize(check_validity=False)
        # UtxoIndex.get_coin, and not a bare self.db.get: a coin several
        # blocks' own worth of staging created or already spent is real
        # before UtxoIndex.finalize ever writes it out, staying staged
        # across more than one block being what btclib-org/btclib-node#586
        # is about. A stored utxo- record this reads back that fails
        # to parse answers None here too, the same as a genuinely
        # missing one: get_coin's own store fallback matches
        # CDBWrapper::Read/CCoinsViewDB::GetCoin's own "absent" rather
        # than raising, RocksDB's own checksum (#641) being what now
        # catches a genuinely corrupted record before this call is
        # ever reached (btclib-org/btclib-node#620,
        # btclib-org/btclib-node#631, btclib-org/btclib-node#650).
        coin = utxo_index.get_coin(prevout_bytes)
        if coin:
            prev_outputs.append(coin.tx_out)
            prevout_coins.append(coin)
        else:
            previous_tx = mempool.get_tx(tx_in.prev_out.tx_id)
            # an output the parent does not have is a missing input, as
            # Core's own `CCoinsViewMemPool::GetCoin` answers it
            # (`src/txmempool.cpp`, at bitcoin/bitcoin@9be056a8a7, the
            # v31.1 tag), not an index error. btclib-org/btclib-node#1252
            if previous_tx and tx_in.prev_out.vout < len(previous_tx.vout):
                tx_out = previous_tx.vout[tx_in.prev_out.vout]
                prev_outputs.append(tx_out)
                prevout_coins.append(Coin(tx_out, spend_height, is_coinbase=False))
            else:
                raise MissingPrevoutError

    def ancestor_median_time_past(height: int) -> int:
        header = header_at_height(tip_header, tip_height, height, parent_of)
        return median_time_past(header, height, parent_of)

    try:
        assert_sequence_locks(
            tx, prevout_coins, spend_height, tip_mtp, ancestor_median_time_past
        )
    except BTClibValueError as refusal:
        reason = "non-BIP68-final"
        raise TxRejectedError(reason) from refusal

    _check_tx_inputs(prevout_coins, tx, spend_height)
    fee = sum(x.value for x in prev_outputs) - sum(x.value for x in tx.vout)
    vsize = _sigop_adjusted_vsize(tx, prev_outputs)
    if not bypass_limits:
        _check_fee_rate(node, vsize, fee)
    # Core's own `ReplacementChecks`, after `PreChecks` and before the
    # scripts, `bypass_limits` or not. btclib-org/btclib-node#1244
    mempool.check_replacement(tx, fee, vsize)

    # Checked last, after the cheap finality and sequence-lock checks
    # above: Core defers its own script checks the same way, to spend no
    # signature verification on a candidate a comparison of two integers
    # already refuses (`PolicyScriptChecks`, `src/validation.cpp:1378`,
    # at bitcoin/bitcoin@4519933391).
    check_transaction(prev_outputs, tx)

    return MempoolAcceptance(fee, vsize)


def _sigop_adjusted_vsize(tx: Tx, prev_outputs: list[TxOut]) -> int:
    """Return Core's vsize for `tx`, refusing one over the sigop ceiling.

    `PreChecks` (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag), `bypass_limits` or not: `GetTransactionSigOpCost` under the
    standard flags, the vsize `GetVirtualTransactionSize` adjusts by it,
    and "bad-txns-too-many-sigops" past `MAX_STANDARD_TX_SIGOPS_COST`.
    btclib-org/btclib-node#1357
    """
    cost = sig_op_cost(tx, prev_outputs)
    if cost > _MAX_STANDARD_TX_SIGOPS_COST:
        reason, details = "bad-txns-too-many-sigops", str(cost)
        raise TxRejectedError(reason, details)
    adjusted_weight = max(tx.weight, cost * _BYTES_PER_SIGOP)
    return -(-adjusted_weight // WITNESS_SCALE_FACTOR)


def _check_tx_inputs(prevout_coins: list[Coin], tx: Tx, spend_height: int) -> None:
    """Refuse an immature coinbase spend or outputs over the inputs.

    Core's own `Consensus::CheckTxInputs` (`src/consensus/tx_verify.cpp`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), in its order and its
    words. A mempool parent's output is never a coinbase's: a coinbase
    is never held. btclib-org/btclib-node#1328
    """
    for coin in prevout_coins:
        depth = spend_height - coin.height
        if coin.is_coinbase and depth < COINBASE_MATURITY:
            reason = "bad-txns-premature-spend-of-coinbase"
            details = f"tried to spend coinbase at depth {depth}"
            raise TxRejectedError(reason, details)
    value_in = sum(coin.tx_out.value for coin in prevout_coins)
    value_out = sum(tx_out.value for tx_out in tx.vout)
    if value_in < value_out:
        reason = "bad-txns-in-belowout"
        details = (
            f"value in ({format_money(value_in)}) < "
            f"value out ({format_money(value_out)})"
        )
        raise TxRejectedError(reason, details)


def _check_fee_rate(node: Node, vsize: int, fee: int) -> None:
    """Refuse a fee below either floor, Core's own `CheckFeeRate`.

    `MemPoolAccept::CheckFeeRate` (`src/validation.cpp`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the mempool's own
    rolling minimum first, then the relay floor, each rounded up for
    `vsize` as `CFeeRate::GetFee` rounds it and each refused with Core's
    own reason and "<fee> < <floor>". Core also asks whether the rolling
    minimum is positive, which a fee never negative here makes
    redundant: `_check_tx_inputs` has already refused one. `vsize` is
    the sigop-adjusted one, Core's `GetTxSize`: btclib-org/btclib-node#1357.
    """
    mempool_reject_fee = fee_from_vsize(vsize, node.mempool.get_min_fee_rate())
    if fee < mempool_reject_fee:
        reason, details = "mempool min fee not met", f"{fee} < {mempool_reject_fee}"
        raise TxRejectedError(reason, details)
    min_relay_fee = fee_from_vsize(vsize, node.config.min_relay_feerate)
    if fee < min_relay_fee:
        reason, details = "min relay fee not met", f"{fee} < {min_relay_fee}"
        raise TxRejectedError(reason, details)
