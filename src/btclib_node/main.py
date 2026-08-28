# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`update_chain`, called once per pass of `Node`'s own loop.

Builds a fork's contextual detail, validates it block by block through
`interpreter.check_transactions`, reconciles the mempool across
whatever it adds and removes, and announces every added block to every
connected peer. `verify_mempool_acceptance` is the same validation path
entered from a single transaction instead, for the RPC and p2p callbacks
that relay one.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from btclib.block.block_context import BlockContext
from btclib.exceptions import BTClibValueError
from btclib.p2p.inventory import Headers, Inv, Inventory, InventoryType

from btclib_node.chainstate.block_index import BlockIndex, BlockStatus
from btclib_node.chainstate.contextual import (
    block_time,
    header_at_height,
    median_time_past,
)
from btclib_node.constants import NodeStatus
from btclib_node.exceptions import (
    ChainstateInconsistencyError,
    InvalidBlockInputError,
    MissingPrevoutError,
    PrevoutCountMismatchError,
)
from btclib_node.interpreter import (
    check_coinbase_maturity,
    check_coinbase_value,
    check_final_transactions,
    check_sequence_locks,
    check_transaction,
    check_transactions,
    get_flags,
    is_final_tx,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from btclib.block import Block, BlockHeader
    from btclib.tx.tx import Tx
    from btclib.tx.tx_out import TxOut

    from btclib_node import Node
    from btclib_node.block_db import Coin, RevBlock
    from btclib_node.chainstate.filter_index import FilterIndex
    from btclib_node.chainstate.utxo_index import UtxoIndex

__all__ = ["update_chain", "verify_mempool_acceptance"]


# update_chain calls this on the failure path, naming the block whose
# contextual validation just failed. BlockIndex.invalidate is where
# what that costs is decided -- the block itself and every candidate
# already built on top of it; this is the one caller of it that has a
# freshly-failed hash to hand it. btclib-org/btclib-node#120
def update_header_index(index: BlockIndex, invalid_hash: bytes) -> None:
    """Invalidate the block `update_chain`'s own trial loop just failed on."""
    index.invalidate(invalid_hash)


# update_chain calls this with every block one of its own calls just put
# on the active chain, never an empty list: get_fork_details' own add
# list always carries at least the candidate's own hash. So every peer
# this node has a live connection to hears about it -- by header where
# sendheaders (callbacks.sendheaders) asked for that, by inventory
# otherwise, the same per-connection shape DownloadManager.tx_download
# already uses to announce a transaction. Every connection, including
# whichever one the block itself arrived on: unlike Core, nothing here
# tracks what a given peer already knows, so the peer that sent it this
# block hears about its own block back. Building that tracking is a
# larger, separate piece of work than #202 asks for; the cost today is
# a redundant message, not a correctness gap. btclib-org/btclib-node#202
def _announce_added_blocks(node: Node, blocks: list[Block]) -> None:
    headers = [block.header for block in blocks]
    inventory = [Inventory(InventoryType.MSG_BLOCK, header.hash) for header in headers]
    for conn in node.p2p_manager.connections.copy().values():
        if conn.prefers_headers:
            conn.send(Headers(headers))
        else:
            conn.send(Inv(inventory))


def finish_sync(node: Node) -> None:
    """Mark the node `BlockSynced`, once there is no candidate left to try.

    A no-op past the first call: nothing here needs undoing if a later
    reorg leaves the chain with a candidate again, `NodeStatus` having
    no state to walk back to from `BlockSynced`.
    """
    if node.status == NodeStatus.BlockSynced:
        return
    node.status = NodeStatus.BlockSynced


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


# update_chain's own post-commit step, once a fork has actually
# connected: every abandoned block's own transactions rejoin the
# mempool where they still verify, and every newly-connected block's
# own transactions leave it, mirroring what connecting them to the
# chain already made true of the UTXO set they are checked against.
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
                fee = verify_mempool_acceptance(node, tx)
            except MissingPrevoutError, BTClibValueError:
                continue
            node.mempool.add_tx(tx, fee)
    for block in to_add:
        for tx in block.transactions[1:]:
            node.mempool.remove_tx(tx)
        # Core's own `removeForBlock` (`src/txmempool.cpp:405-427`,
        # at bitcoin/bitcoin@58a7869f86): once per block connected,
        # whether or not it held anything this mempool was also
        # holding, restarting `Mempool.get_min_fee_rate`'s own decay
        # clock -- not folded into `remove_tx` above, which already
        # runs once per transaction rather than once per block.
        # btclib-org/btclib-node#294
        node.mempool.note_block_connected()
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
    if utxo_index.should_flush():
        node.chainstate.flush()
    node.logger.debug("End chainstate finalize")


# update_chain's own trial marks, taken before a trial starts: should_flush
# may have left utxo_index and filter_index each holding an earlier,
# already-succeeded trial's own staged changes, unflushed, and a rollback
# on failure must undo only what this trial itself stages --
# UtxoIndex.trial_mark's own docstring argues why a blanket wipe is no
# longer safe once staging survives more than one trial (btclib-org/btclib-node#586).
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
def _ready_fork(node: Node) -> tuple[list[bytes], list[bytes]] | None:
    if node.status < NodeStatus.HeaderSynced:
        return None

    block_index = node.chainstate.block_index
    first_candidate = block_index.get_first_candidate()
    if not first_candidate:
        finish_sync(node)
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


# both _validate_block and verify_mempool_acceptance below need a way to
# walk from a known header back to an ancestor's, for median_time_past:
# header_dict holds every header this node has ever indexed, active
# chain or not, so this reaches a trial fork's own earlier blocks as
# readily as long-committed history -- unlike active_chain, which still
# reads as the chain before this trial until _finalize_fork runs
def _parent_of(node: Node) -> Callable[[BlockHeader], BlockHeader]:
    header_dict = node.chainstate.block_index.header_dict

    def parent_of(header: BlockHeader) -> BlockHeader:
        return header_dict[header.previous_block_hash].header

    return parent_of


# the two 2010 blocks Chain.bip30_exceptions names are the only ones
# this node ever lets past UtxoIndex.add_block's own BIP30 check --
# add_block's own docstring is where that check and the exception are
# argued. A function of its own rather than inline in update_chain,
# which ruff's own too-many-statements already counts every statement
# gained here against.
def _check_bip30(node: Node, index: int, block_hash: bytes) -> bool:
    """Whether `block_hash`, connecting at `index`, is checked for BIP30."""
    return (index, block_hash) not in node.chain.bip30_exceptions


# update_chain's own per-block gate, once a candidate's spends and
# creations are staged and its own height is known: script and amounts
# (interpreter.check_transactions), a coinbase paying more than subsidy
# plus fees (interpreter.check_coinbase_value), a spend of a coinbase not
# yet COINBASE_MATURITY deep (interpreter.check_coinbase_maturity), the
# two rules a height and a clock decide on their own
# (Block.assert_valid_contextual) -- time-too-new, already checked on the
# header path (chainstate/contextual.py), and bad-cb-height, wherever
# BIP34 binds (Chain.bip34_height, per network) -- and now every
# transaction's own finality (interpreter.check_final_transactions,
# BIP113-aware) and BIP68 relative lock (interpreter.check_sequence_locks).
# BIP30 runs earlier still, inside utxo_index.add_block, before this is
# ever called: its own docstring is where that ordering and the two 2010
# exceptions are argued. A function of its own rather than statements
# inline: update_chain's own trial loop is already long enough that
# PLR0915 counts every statement gained here against it.
def _validate_block(
    node: Node, block: Block, transactions: list[tuple[list[Coin], Tx]], index: int
) -> None:
    block.assert_valid_contextual(
        BlockContext(index, datetime.now(UTC), node.chain.bip34_height)
    )

    block_index = node.chainstate.block_index
    parent_header = block_index.header_dict[block.header.previous_block_hash].header
    parent_height = index - 1
    parent_of = _parent_of(node)
    parent_mtp = median_time_past(parent_header, parent_height, parent_of)

    # Core deploys BIP68, BIP112 (the CHECKSEQUENCEVERIFY opcode) and
    # BIP113 (this cutoff) together, as one soft fork -- this tree has
    # no BIP9 deployment tracking of its own, so the height Chain.flags
    # already turns the opcode on at is read here too, rather than a
    # second activation table naming the same height for the same fork.
    # interpreter.check_sequence_locks' own docstring argues this the
    # same way.
    bip113_active = "CHECKSEQUENCEVERIFY" in get_flags(node.config, index)
    lock_time_cutoff = parent_mtp if bip113_active else block_time(block.header)
    check_final_transactions(block.transactions, index, lock_time_cutoff)

    def ancestor_median_time_past(height: int) -> int:
        header = header_at_height(parent_header, parent_height, height, parent_of)
        return median_time_past(header, height, parent_of)

    check_sequence_locks(
        transactions,
        index,
        enforce_bip68=bip113_active,
        tip_median_time_past=parent_mtp,
        ancestor_median_time_past=ancestor_median_time_past,
    )

    for prevouts, _tx in transactions:
        check_coinbase_maturity(prevouts, index)
    check_transactions(transactions, index, node)
    check_coinbase_value(block.transactions[0], transactions, index, node)


def _record_rejection(
    node: Node, failed_hash: bytes | None, exc: BaseException
) -> None:
    """Record the block `failed_hash` names as refused, and why.

    `Node.__init__`'s own comment beside `last_rejected_block` says who
    reads it: a rejection test, asserting the rule that refused a block
    rather than only that one did. Never set on a raise before any
    block in this fork started -- `failed_hash` is still `None` there,
    the same guard `update_header_index`'s own call below reads.
    """
    if failed_hash is not None:
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
# StoreClosedError, whatever sqlite3 or the filesystem raises out of a
# KeyValueStore read or write, and ChainstateInconsistencyError -- and
# that holds even where the call that raised it also raises one of the
# three above for a different reason, utxo_index.add_block's own
# self.db.get() being exactly that call. update_chain's own except below
# is what tells the two apart, by type rather than by call site, and
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
    already at. `failed_hash is None` is exactly the to_remove loop above
    `update_chain`'s own trial, where `_record_rejection`'s own guard
    already turns this into a no-op regardless; `isinstance(exc,
    _CONTENT_FAILURE)` is the to_add loop's own exceptions, argued where
    `_CONTENT_FAILURE` is declared. `raise exc` and not a bare `raise`:
    this is not itself an except block, so a bare `raise` here has no
    currently-handled exception of its own to reach for -- `exc` already
    carries the traceback `update_chain`'s own except caught it with, and
    raising it explicitly extends that same traceback rather than
    starting a new one.
    """
    if failed_hash is None or isinstance(exc, _CONTENT_FAILURE):
        _record_rejection(node, failed_hash, exc)
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
    succeeds, `_finalize_fork` commits it, the mempool is reconciled
    against whatever it added and removed, and `_announce_added_blocks`
    tells every connected peer.
    """
    fork = _ready_fork(node)
    if fork is None:
        return None
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
            _finalize_fork(node, to_add, to_remove)
        else:
            node.logger.debug("Start chainstate rollback")
            _rollback_trial(node, utxo_mark, filter_mark)
            node.logger.debug("End chainstate rollback")

    node.logger.info("End block validation")

    if not success and failed_hash is not None:
        node.logger.debug("Start updating index")
        update_header_index(block_index, failed_hash)

    if success and node.status == NodeStatus.BlockSynced:
        _reconcile_mempool_for_reorg(node, to_remove, to_add)

    node.logger.debug("Finished main\n")

    if not block_index.get_first_candidate():
        return finish_sync(node)
    return None


def verify_mempool_acceptance(node: Node, tx: Tx) -> int:
    """Verify a transaction against its prevouts and return its fee.

    The fee is the same sum-of-inputs-less-sum-of-outputs
    `btclib.script.engine.verify_amounts` already computes and discards
    inside `check_transaction` below; recomputed here from the same
    `prev_outputs` this function built for that call, rather than
    threaded back out of btclib's engine, which returns nothing.
    btclib-org/btclib-node#260

    Checks finality and BIP68 against the tip Core's own mempool policy
    does (`CheckFinalTxAtTip`/`STANDARD_LOCKTIME_VERIFY_FLAGS`,
    `src/validation.cpp:156-175` and `policy/policy.h:137`,
    at bitcoin/bitcoin@204256c73f), both unconditionally rather than
    gated on any activation height, unlike `main._validate_block`'s own
    block-connect path: a mempool never holds a transaction from before
    a soft fork it has already activated, so Core's own mempool code
    does not ask either.
    """
    prev_outputs: list[TxOut] = []
    # only the prevouts this reads off the UTXO set, since a mempool
    # ancestor's own output can never be a coinbase's: a coinbase's
    # null prevout resolves through neither branch below and so never
    # reaches the mempool for check_coinbase_maturity to skip
    coins_from_utxo_set: list[Coin] = []
    # every prevout, coinbase or mempool-parented alike, aligned with
    # tx.vin one for one -- what check_sequence_locks below needs and
    # coins_from_utxo_set above does not carry, since it drops a
    # mempool-parented input rather than pairing it with a placeholder.
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
    # one block past the real next height, invisible everywhere else
    # this reaches (get_flags below) only because every regtest flag
    # activates at height 0 regardless, and wrong by exactly one block
    # for check_coinbase_maturity, which is what surfaced it
    # (btclib-org/btclib-node#569)
    spend_height = len(block_index.active_chain)

    for tx_in in tx.vin:
        prevout_bytes = tx_in.prev_out.serialize(check_validity=False)
        # UtxoIndex.get_coin, and not a bare self.db.get: a coin several
        # blocks' own worth of staging created or already spent is real
        # before UtxoIndex.finalize ever writes it out, staying staged
        # across more than one block being what btclib-org/btclib-node#586
        # is about.
        coin = utxo_index.get_coin(prevout_bytes)
        if coin:
            coins_from_utxo_set.append(coin)
            prev_outputs.append(coin.tx_out)
            prevout_coins.append(coin)
        else:
            previous_tx = mempool.get_tx(tx_in.prev_out.tx_id)
            if previous_tx:
                tx_out = previous_tx.vout[tx_in.prev_out.vout]
                prev_outputs.append(tx_out)
                prevout_coins.append(Coin(tx_out, spend_height, is_coinbase=False))
            else:
                raise MissingPrevoutError

    check_coinbase_maturity(coins_from_utxo_set, spend_height)
    check_transaction(prev_outputs, tx, spend_height, node)

    tip_hash = block_index.active_chain[-1]
    tip_header = block_index.header_dict[tip_hash].header
    tip_height = spend_height - 1
    parent_of = _parent_of(node)
    tip_mtp = median_time_past(tip_header, tip_height, parent_of)

    if not is_final_tx(tx, spend_height, tip_mtp):
        err_msg = "bad-txns-nonfinal"
        raise BTClibValueError(err_msg)

    def ancestor_median_time_past(height: int) -> int:
        header = header_at_height(tip_header, tip_height, height, parent_of)
        return median_time_past(header, height, parent_of)

    check_sequence_locks(
        [(prevout_coins, tx)],
        spend_height,
        enforce_bip68=True,
        tip_median_time_past=tip_mtp,
        ancestor_median_time_past=ancestor_median_time_past,
    )

    return sum(x.value for x in prev_outputs) - sum(x.value for x in tx.vout)
