# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import time

from btclib.p2p.addrv2 import SendAddrV2
from btclib.p2p.compact_blocks import SendCmpct
from btclib.p2p.data import BlockPayload as BlockMsg
from btclib.p2p.data import TxPayload as TxMsg
from btclib.p2p.handshake import Verack
from btclib.p2p.inventory import (
    GetData,
    GetHeaders,
    Headers,
    Inv,
    InventoryType,
    NotFound,
)
from btclib.p2p.keepalive import Ping, Pong

from btclib_node.constants import NodeStatus, P2pConnStatus, ProtocolVersion, Services
from btclib_node.exceptions import MissingPrevoutError
from btclib_node.main import verify_mempool_acceptance
from btclib_node.p2p.messages.address import Addr, AddrV2
from btclib_node.p2p.messages.empty import Getaddr, Sendheaders, Wtxidrelay
from btclib_node.p2p.messages.errors import Reject
from btclib_node.p2p.messages.handshake import Version


def version(node, msg, conn):
    version_msg = Version.parse(msg)

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
    conn.send(SendAddrV2())
    conn.send(Verack())

    conn.relay_txs = version_msg.relay


def verack(node, msg, conn):
    if not conn.version_message or not conn.wtxidrelay_received:
        conn.stop()
        return
    conn.status = P2pConnStatus.Connected
    conn.send(Sendheaders())
    conn.send(SendCmpct(False, 1))
    conn.send_ping()
    conn.send(Getaddr())
    block_locators = node.chainstate.block_index.get_block_locator_hashes()
    conn.send(GetHeaders(ProtocolVersion, block_locators, b"\x00" * 32))
    node.logger.info(
        f"Connected to {conn.client.getpeername()[0]}:{conn.client.getpeername()[1]}"
    )


def wtxidrelay(node, msg, conn):
    conn.wtxidrelay_received = True


def sendaddrv2(node, msg, conn):
    conn.prefer_addressv2 = True


def ping(node, msg, conn):
    nonce = Ping.parse(msg).nonce
    conn.send(Pong(nonce))


def pong(node, msg, conn):
    nonce = Pong.parse(msg).nonce
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
        # an addr version 1 message has nowhere to put a tor, i2p or
        # cjdns address, and Addr.serialize raises rather than invent one
        addresses = [addr for addr in addresses if addr.netid.can_addrv1]
    for x in range(0, len(addresses), 1000):
        conn.send(addr_cls(addresses[x : x + 1000]))


def addr(node, msg, conn):
    addresses = Addr.parse(msg).addresses
    node.p2p_manager.peer_db.add_addresses(addresses)


def addrv2(node, msg, conn):
    addresses = AddrV2.parse(msg).addresses
    node.p2p_manager.peer_db.add_addresses(addresses)


def tx(node, msg, conn):
    tx = TxMsg.parse(msg).tx
    try:
        verify_mempool_acceptance(node, tx)
    except MissingPrevoutError:
        # We don't have the parents in the mempool
        return
    if not node.mempool.contains_tx(tx):
        node.mempool.add_tx(tx)
        node.download_manager.received_txs.append((conn.id, tx.hash))


def block(node, msg, conn):
    # btclib's BlockPayload validates against mainnet's pow limit by
    # default, which no regtest or signet block meets. Its own docstring
    # names the shape: build unchecked and ask afterwards, which is what
    # block.assert_valid below does, against this chain's limit.
    block = BlockMsg.parse(msg, check_validity=False).block
    block_hash = block.header.hash

    if block_hash in conn.download_queue:
        conn.download_queue.remove(block_hash)

    conn.last_block_timestamp = time.time()
    conn.pending_eviction = False

    block_info = node.chainstate.block_index.get_block_info(block_hash)

    if not block_info.downloaded:
        # a block that does not hold up is nobody's: the raise reaches
        # main.handle_p2p, which drops the peer that sent it. Marking
        # the block itself invalid, so the next peer is not asked for it
        # too, is #77
        block.assert_valid(node.chain.pow_limit_bits)
        node.block_db.add_block(block)
        node.logger.info(f"Received new block with hash:{block_hash.hex()}")
        block_info.downloaded = True
        node.chainstate.block_index.insert_block_info(block_info)


def inv(node, msg, conn):
    if node.status < NodeStatus.BlockSynced:
        return
    inv = Inv.parse(msg)

    blocks = [x.hash for x in inv.items if x.type_code == InventoryType.MSG_BLOCK]
    if blocks:
        block_locators = node.chainstate.block_index.get_block_locator_hashes()
        conn.send(GetHeaders(ProtocolVersion, block_locators, blocks[-1]))

    wtransactions = [x.hash for x in inv.items if x.type_code == InventoryType.MSG_WTX]
    missing_tx = node.mempool.get_missing(wtransactions, wtxid=True)
    if missing_tx:
        node.download_manager.inv_txs.extend([(conn.id, wtxid) for wtxid in missing_tx])


def getdata(node, msg, conn):
    getdata = GetData.parse(msg)

    tx_types = (
        InventoryType.MSG_TX,
        InventoryType.MSG_WTX,
        InventoryType.MSG_WITNESS_TX,
    )
    for item in getdata.items:
        if item.type_code not in tx_types:
            continue
        wtxid = item.type_code == InventoryType.MSG_WTX
        tx = node.mempool.get_tx(item.hash, wtxid=wtxid)
        if tx:
            include_witness = item.type_code in (
                InventoryType.MSG_WITNESS_TX,
                InventoryType.MSG_WTX,
            )
            conn.send(TxMsg(tx, include_witness=include_witness))

    block_types = (InventoryType.MSG_BLOCK, InventoryType.MSG_WITNESS_BLOCK)
    for item in getdata.items:
        if item.type_code not in block_types:
            continue
        block = node.block_db.get_block(item.hash)
        if block:
            include_witness = item.type_code == InventoryType.MSG_WITNESS_BLOCK
            conn.send(
                BlockMsg(block, include_witness=include_witness, check_validity=False)
            )


def headers(node, msg, conn):
    headers = Headers.parse(msg).headers
    added = node.chainstate.block_index.add_headers(headers)
    # TODO: now it doesn't support long reorganizations (> 2000 headers)
    if len(headers) == 2000 and added:  # we have to require more headers
        block_locators = node.chainstate.block_index.get_block_locator_hashes()
        conn.send(GetHeaders(ProtocolVersion, block_locators, b"\x00" * 32))
    elif node.status == NodeStatus.SyncingHeaders:
        node.status = NodeStatus.HeaderSynced


def getheaders(node, msg, conn):
    getheaders = GetHeaders.parse(msg)
    headers = node.chainstate.block_index.get_headers_from_locators(
        getheaders.locator, getheaders.hash_stop
    )
    if headers:
        conn.send(Headers(headers))


def not_found(node, msg, conn):
    missing = NotFound.parse(msg)
    node.logger.warning(f"Missing objects:{missing}")


def reject(node, msg, conn):
    reject = Reject.parse(msg)
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
