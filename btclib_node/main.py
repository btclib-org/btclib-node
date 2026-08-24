# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from typing import TYPE_CHECKING

from btclib.block import Block
from btclib.exceptions import BTClibValueError
from btclib.p2p.inventory import Headers, Inv, Inventory, InventoryType
from btclib.tx import TxOut
from btclib.tx.tx import Tx

from btclib_node.block_db import RevBlock
from btclib_node.chainstate.block_index import BlockIndex, BlockStatus
from btclib_node.constants import NodeStatus
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.interpreter import check_transaction, check_transactions

if TYPE_CHECKING:
    from btclib_node import Node


# update_chain calls this on the failure path, naming the block whose
# contextual validation just failed. BlockIndex.invalidate is where
# what that costs is decided -- the block itself and every candidate
# already built on top of it; this is the one caller of it that has a
# freshly-failed hash to hand it. btclib-org/btclib-node#120
def update_header_index(index: BlockIndex, invalid_hash: bytes) -> None:
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
    if node.status == NodeStatus.BlockSynced:
        return
    node.status = NodeStatus.BlockSynced


def update_chain(node: Node) -> None:
    if node.status < NodeStatus.HeaderSynced:
        return None

    block_index = node.chainstate.block_index
    utxo_index = node.chainstate.utxo_index
    filter_index = node.chainstate.filter_index

    first_candidate = block_index.get_first_candidate()
    if not first_candidate:
        return finish_sync(node)

    to_add_hash, to_remove_hash = block_index.get_fork_details(
        first_candidate.header.hash
    )

    for hash in to_add_hash:
        if not block_index.get_block_info(hash).downloaded:
            # get_first_candidate prefers a branch whose tip has
            # arrived, so a branch missing its tip is stepped over; a
            # branch missing a block behind its tip is not, and until
            # that block arrives nothing queued behind it connects,
            # however complete: btclib-org/btclib-node#121
            return None

    node.logger.info("Start block validation")

    node.logger.debug("Start getting blocks")
    # every hash here was just checked downloaded, or was on the active
    # chain this is replacing, so block_db holds it; the type is wider
    # than that invariant
    to_add: list[Block] = []
    for hash in to_add_hash:
        block = node.block_db.get_block(hash)
        if block is None:
            err_msg = f"block just checked downloaded is missing: {hash.hex()}"
            raise Exception(err_msg)
        to_add.append(block)
    # tip first: an output the branch created may have been spent again
    # further along it, and the block that spent it has to be undone
    # before the block that made it. `remove_from_active_chain` asks for
    # the same order, and refuses anything but the tip
    to_remove: list[RevBlock] = []
    for hash in reversed(to_remove_hash):
        rev_block = node.block_db.get_rev_block(hash)
        if rev_block is None:
            err_msg = f"no reverse patch for a block on the active chain: {hash.hex()}"
            raise Exception(err_msg)
        to_remove.append(rev_block)
    node.logger.debug("Got all blocks")

    node.logger.debug("Start chainstate test")

    success = True
    # set the moment a block starts and cleared once it is fully
    # through: an exception anywhere in its own iteration leaves it
    # naming the block that failed, which is what update_header_index
    # invalidates below. Never set by the to_remove loop -- a rollback
    # failing there is not a new block being bad.
    failed_hash: bytes | None = None
    # the block index's database write moves into the batch below and
    # nowhere in here: a status written on the way through reaches the
    # database before the branch is known to connect, and refusing the
    # branch does not take it back
    try:
        for rev_block in to_remove:
            utxo_index.apply_rev_block(rev_block)
        for block_hash, block in zip(to_add_hash, to_add):
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
            transactions, rev_patch = utxo_index.add_block(block)
            index = block_index.get_block_info(block_hash).index
            check_transactions(transactions, index, node)

            node.block_db.add_rev_block(rev_patch)
            # here and not on a pass of its own: the patch names the
            # output every input of this block spent, which is what a
            # BIP158 filter is built from and what a block does not
            # carry. Read back off the disk it would be the same octets
            # fetched twice.
            filter_index.add_connected_block(block, rev_patch)
            failed_hash = None

    except Exception:
        node.logger.exception("Exception occurred")
        success = False
    finally:
        if success:
            node.logger.debug("Start chainstate finalize")
            # block_db is its own KeyValueStore, on its own datadir file,
            # so it cannot share chainstate's write_batch below -- but it
            # gets the same held-until-known-good treatment: the reverse
            # patches add_rev_block buffered during the trial only reach
            # disk once the branch they belong to is the one that
            # connected. btclib-org/btclib-node#200
            node.block_db.finalize()
            with node.chainstate.db.write_batch() as wb:
                for rev_block in to_remove:
                    block_index.remove_from_active_chain(rev_block.hash)
                    block_index.set_status(rev_block.hash, BlockStatus.valid, wb)
                    node.logger.debug(f"Removed block {rev_block.hash.hex()}")
                for block in to_add:
                    block_hash = block.header.hash
                    block_index.add_to_active_chain(block_hash)
                    block_index.set_status(block_hash, BlockStatus.in_active_chain, wb)
                    node.logger.info(f"Added block {block_hash.hex()}")
                utxo_index.finalize(wb)
                filter_index.finalize(wb)
            node.logger.debug("End chainstate finalize")
        else:
            node.logger.debug("Start chainstate rollback")
            node.block_db.rollback()
            utxo_index.rollback()
            filter_index.rollback()
            node.logger.debug("End chainstate rollback")

    node.logger.info("End block validation")

    if not success and failed_hash is not None:
        node.logger.debug("Start updating index")
        update_header_index(block_index, failed_hash)

    if success and node.status == NodeStatus.BlockSynced:
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
                raise Exception(err_msg)
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
            # bitcoin/bitcoin@58a7869f86): once per block connected,
            # whether or not it held anything this mempool was also
            # holding, restarting `Mempool.get_min_fee_rate`'s own decay
            # clock -- not folded into `remove_tx` above, which already
            # runs once per transaction rather than once per block.
            # btclib-org/btclib-node#294
            node.mempool.note_block_connected()
        _announce_added_blocks(node, to_add)

    node.logger.debug("Finished main\n")

    if not block_index.get_first_candidate():
        return finish_sync(node)


def verify_mempool_acceptance(node: Node, tx: Tx) -> int:
    """Verify a transaction against its prevouts and return its fee.

    The fee is the same sum-of-inputs-less-sum-of-outputs
    `btclib.script.engine.verify_amounts` already computes and discards
    inside `check_transaction` below; recomputed here from the same
    `prev_outputs` this function built for that call, rather than
    threaded back out of btclib's engine, which returns nothing.
    btclib-org/btclib-node#260
    """
    prev_outputs: list[TxOut] = []

    block_index = node.chainstate.block_index
    utxo_index = node.chainstate.utxo_index
    mempool = node.mempool

    for tx_in in tx.vin:
        prevout_bytes = tx_in.prev_out.serialize(check_validity=False)
        serialized_txout = utxo_index.db.get(b"utxo-" + prevout_bytes)
        if serialized_txout:
            txout = TxOut.parse(serialized_txout, check_validity=False)
            prev_outputs.append(txout)
        else:
            previous_tx = mempool.get_tx(tx_in.prev_out.tx_id)
            if previous_tx:
                prev_outputs.append(previous_tx.vout[tx_in.prev_out.vout])
            else:
                raise MissingPrevoutError

    check_transaction(prev_outputs, tx, len(block_index.active_chain) + 1, node)
    return sum(x.value for x in prev_outputs) - sum(x.value for x in tx.vout)
