# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib.tx import TxOut

from btclib_node.chainstate.block_index import BlockStatus
from btclib_node.constants import NodeStatus
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.interpreter import check_transaction, check_transactions


def update_block_status(index, hash, status, wb):
    block_info = index.get_block_info(hash)
    block_info.status = status
    index.insert_block_info(block_info, wb)


# a stub: nothing invalidates the headers built on a failed block.
# btclib-org/btclib-node#120
def update_header_index(index):
    pass


def finish_sync(node):
    if node.status == NodeStatus.BlockSynced:
        return
    node.status = NodeStatus.BlockSynced
    # start new connections with tx relay enabled
    node.p2p_manager.stop_all()


def update_chain(node):
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
    to_add = [node.block_db.get_block(hash) for hash in to_add_hash]
    # tip first: an output the branch created may have been spent again
    # further along it, and the block that spent it has to be undone
    # before the block that made it. `remove_from_active_chain` asks for
    # the same order, and refuses anything but the tip
    to_remove = [node.block_db.get_rev_block(hash) for hash in reversed(to_remove_hash)]
    node.logger.debug("Got all blocks")

    node.logger.debug("Start chainstate test")

    success = True
    generated_rev_patches = []
    try:
        for rev_block in to_remove:
            utxo_index.apply_rev_block(rev_block)
        for block_hash, block in zip(to_add_hash, to_add):
            transactions, rev_patch = utxo_index.add_block(block)
            index = block_index.get_block_info(block_hash).index
            check_transactions(transactions, index, node)
            update_block_status(block_index, block_hash, BlockStatus.valid, None)

            node.block_db.add_rev_block(rev_patch)
            generated_rev_patches.append(rev_patch)
            # here and not on a pass of its own: the patch names the
            # output every input of this block spent, which is what a
            # BIP158 filter is built from and what a block does not
            # carry. Read back off the disk it would be the same octets
            # fetched twice.
            filter_index.add_connected_block(block, rev_patch)

    except Exception:
        node.logger.exception("Exception occurred")
        success = False
    finally:
        if success:
            node.logger.debug("Start chainstate finalize")
            with node.chainstate.db.write_batch() as wb:
                for rev_block in to_remove:
                    block_index.remove_from_active_chain(rev_block.hash)
                    update_block_status(
                        block_index, rev_block.hash, BlockStatus.valid, wb
                    )
                    node.logger.debug(f"Removed block {rev_block.hash.hex()}")
                for block in to_add:
                    block_hash = block.header.hash
                    block_index.add_to_active_chain(block_hash)
                    update_block_status(
                        block_index, block_hash, BlockStatus.in_active_chain, wb
                    )
                    node.logger.info(f"Added block {block_hash.hex()}")
                utxo_index.finalize(wb)
                filter_index.finalize(wb)
            node.logger.debug("End chainstate finalize")
        else:
            node.logger.debug("Start chainstate rollback")
            utxo_index.rollback()
            filter_index.rollback()
            node.logger.debug("End chainstate rollback")

    node.logger.info("End block validation")

    if not success:
        node.logger.debug("Start updating index")
        update_header_index(block_index)

    if success and node.status == NodeStatus.BlockSynced:
        for rev_block in to_remove:
            block = node.block_db.get_block(rev_block.hash)
            for tx in block.transactions[1:]:
                node.mempool.add_tx(tx)
        for rev_block, block in zip(generated_rev_patches, to_add):
            for tx in block.transactions:
                node.mempool.remove_tx(tx)

    node.logger.debug("Finished main\n")

    if not block_index.get_first_candidate():
        return finish_sync(node)


def verify_mempool_acceptance(node, tx):
    prev_outputs = []

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
