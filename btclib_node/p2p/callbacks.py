# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import time

from btclib_node.constants import NodeStatus, P2pConnStatus, ProtocolVersion, Services
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.main import verify_mempool_acceptance
from btclib_node.p2p.messages.address import Addr, AddrV2, Getaddr
from btclib_node.p2p.messages.compact import Sendcmpct
from btclib_node.p2p.messages.data import Block as BlockMsg
from btclib_node.p2p.messages.data import Headers, Inv
from btclib_node.p2p.messages.data import Tx as TxMsg
from btclib_node.p2p.messages.errors import Notfound, Reject
from btclib_node.p2p.messages.getdata import (
    Getdata,
    Getheaders,
    InventoryType,
    Sendheaders,
)
from btclib_node.p2p.messages.handshake import Sendaddrv2, Verack, Version, Wtxidrelay
from btclib_node.p2p.messages.ping import Ping, Pong


def version(node, msg, conn):
    version_msg = Version.deserialize(msg)

    conn.version_message = version_msg
    if version_msg.nonce in node.p2p_manager.nonces:  # connection to ourselves
        conn.stop()
        return

    # For semplicity we only allow current protocol version
    if version_msg.version < ProtocolVersion:
        conn.stop()
        return
    if not version_msg.services & Services.witness:  # we only connect to witness nodes
        conn.stop()
        return
    if (
        not version_msg.services & Services.network
        and node.status >= NodeStatus.BlockSynced
    ):
        conn.stop()
        return

    conn.send(Wtxidrelay())
    conn.send(Sendaddrv2())
    conn.send(Verack())

    conn.relay_txs = version_msg.relay


def verack(node, msg, conn):
    if not conn.version_message or not conn.wtxidrelay_received:
        conn.stop()
        return
    conn.status = P2pConnStatus.Connected
    conn.send(Sendheaders())
    conn.send(Sendcmpct(0, 1))
    conn.send_ping()
    conn.send(Getaddr())
    block_locators = node.chainstate.block_index.get_block_locator_hashes()
    conn.send(Getheaders(ProtocolVersion, block_locators, b"\x00" * 32))
    node.logger.info(
        f"Connected to {conn.client.getpeername()[0]}:{conn.client.getpeername()[1]}"
    )


def wtxidrelay(node, msg, conn):
    conn.wtxidrelay_received = True


def sendaddrv2(node, msg, conn):
    conn.prefer_addressv2 = True


def ping(node, msg, conn):
    nonce = Ping.deserialize(msg).nonce
    conn.send(Pong(nonce))


def pong(node, msg, conn):
    nonce = Pong.deserialize(msg).nonce
    if conn.ping_sent:
        if conn.ping_nonce != nonce:
            conn.stop()
            return
        conn.latency = time.time() - conn.ping_sent
        conn.ping_sent = 0
        conn.ping_nonce = 0


def getaddr(node, msg, conn):
    addresses = node.p2p_manager.peer_db.get_active_addresses()
    if conn.prefer_addressv2:
        addr_cls = AddrV2
    else:
        addr_cls = Addr
        addresses = [addr for addr in addresses if addr.can_addrv1]
    for x in range(0, len(addresses), 1000):
        conn.send(addr_cls(headers[x : x + 1000]))


def addr(node, msg, conn):
    addresses = Addr.deserialize(msg).addresses
    node.p2p_manager.peer_db.add_addresses(addresses)


def addrv2(node, msg, conn):
    addresses = AddrV2.deserialize(msg).addresses
    node.p2p_manager.peer_db.add_addresses(addresses)


def tx(node, msg, conn):
    tx = TxMsg.deserialize(msg).tx
    try:
        verify_mempool_acceptance(node, tx)
    except MissingPrevoutError:
        # We don't have the parents in the mempool
        return
    if not node.mempool.contains_tx(tx):
        node.mempool.add_tx(tx)
        node.download_manager.received_txs.append((conn.id, tx.hash))


def block(node, msg, conn):
    block = BlockMsg.deserialize(msg).block
    block_hash = block.header.hash

    if block_hash in conn.download_queue:
        conn.download_queue.remove(block_hash)

    conn.last_block_timestamp = time.time()
    conn.pending_eviction = False

    block_info = node.chainstate.block_index.get_block_info(block_hash)

    if not block_info.downloaded:
        try:
            block.assert_valid(node.chain.pow_limit_bits)
        except Exception as e:  # should set block to invalid
            raise e
        node.block_db.add_block(block)
        node.logger.info(f"Received new block with hash:{block_hash.hex()}")
        block_info.downloaded = True
        node.chainstate.block_index.insert_block_info(block_info)


def inv(node, msg, conn):
    if node.status < NodeStatus.BlockSynced:
        return
    inv = Inv.deserialize(msg)

    blocks = [x[1] for x in inv.inventory if x[0] == InventoryType.block]
    if blocks:
        block_locators = node.chainstate.block_index.get_block_locator_hashes()
        conn.send(Getheaders(ProtocolVersion, block_locators, blocks[-1]))

    wtransactions = [x[1] for x in inv.inventory if x[0] == InventoryType.wtx]
    missing_tx = node.mempool.get_missing(wtransactions, wtxid=True)
    if missing_tx:
        node.download_manager.inv_txs.extend([(conn.id, wtxid) for wtxid in missing_tx])


def getdata(node, msg, conn):
    getdata = Getdata.deserialize(msg)

    transactions = [
        x
        for x in getdata.inventory
        if x[0] in (InventoryType.tx, InventoryType.wtx, InventoryType.witness_tx)
    ]
    for inv_type, txid in transactions:
        wtxid = inv_type == InventoryType.wtx
        tx = node.mempool.get_tx(txid, wtxid=wtxid)
        if tx:
            include_witness = inv_type in (InventoryType.witness_tx, InventoryType.wtx)
            conn.send(TxMsg(tx, include_witness=include_witness))

    blocks = [
        x
        for x in getdata.inventory
        if x[0] in (InventoryType.block, InventoryType.witness_block)
    ]
    for inv_type, block_hash in blocks:
        block = node.block_db.get_block(block_hash)
        if block:
            include_witness = inv_type == InventoryType.witness_block
            conn.send(BlockMsg(block, include_witness=include_witness))


def headers(node, msg, conn):
    headers = Headers.deserialize(msg).headers
    added = node.chainstate.block_index.add_headers(headers)
    # TODO: now it doesn't support long reorganizations (> 2000 headers)
    if len(headers) == 2000 and added:  # we have to require more headers
        block_locators = node.chainstate.block_index.get_block_locator_hashes()
        conn.send(Getheaders(ProtocolVersion, block_locators, b"\x00" * 32))
    elif node.status == NodeStatus.SyncingHeaders:
        node.status = NodeStatus.HeaderSynced


def getheaders(node, msg, conn):
    getheaders = Getheaders.deserialize(msg)
    headers = node.chainstate.block_index.get_headers_from_locators(
        getheaders.block_hashes, getheaders.hash_stop
    )
    if headers:
        conn.send(Headers(headers))


def not_found(node, msg, conn):
    missing = Notfound.deserialize(msg)
    node.logger.warning(f"Missing objects:{missing}")


def reject(node, msg, conn):
    reject = Reject.deserialize(msg)
    err_msg = (
        f"Reject received: {reject.code.name}, {reject.reason}, {reject.data.hex()}"
    )
    node.logger.warning(err_msg)


handshake_callbacks = {
    "version": version,
    "verack": verack,
    "wtxidrelay": wtxidrelay,
    "sendaddrv2": sendaddrv2,
}

callbacks = {
    "ping": ping,
    "pong": pong,
    "inv": inv,
    "tx": tx,
    "block": block,
    "getdata": getdata,
    "getheaders": getheaders,
    "headers": headers,
    "addr": addr,
    "addrv2": addrv2,
    "getaddr": getaddr,
    "notfound": not_found,
    "reject": reject,
}
