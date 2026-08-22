# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import time

from btclib.p2p.addrv2 import SendAddrV2
from btclib.p2p.block_filters import (
    BlockFilterType,
    CFCheckpt,
    CFHeaders,
    CFilter,
    GetCFCheckpt,
    GetCFHeaders,
    GetCFilters,
)
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
from btclib.p2p.limits import (
    CFCHECKPT_INTERVAL,
    MAX_GETCFHEADERS_SIZE,
    MAX_GETCFILTERS_SIZE,
)

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


def _filter_range(node, filter_type, start_height, stop_hash, limit):
    """Return the active-chain heights a BIP157 request names, or None.

    A range is a start height and the hash of the block it ends at, so
    turning it into heights is the one thing `btclib.p2p.block_filters`
    leaves to a caller: only a node holds the chain that says what
    height a hash is at.

    Answered with nothing rather than with an error, which is what
    BIP157 asks for -- "MUST NOT" send the response, and no message is
    defined for saying why. A peer asking for a filter type this node
    does not serve, for a block it does not have on its active chain,
    or for more blocks than the limit gets silence.
    """
    if filter_type != BlockFilterType.BASIC:
        return None
    block_index = node.chainstate.block_index
    if stop_hash not in block_index.header_dict:
        return None
    stop_height = block_index.get_block_info(stop_hash).index
    active_chain = block_index.active_chain
    if stop_height >= len(active_chain) or active_chain[stop_height] != stop_hash:
        return None
    # only the upper end: the field is unsigned on the wire and these
    # requests are always parsed, so a negative start cannot arrive
    if start_height > stop_height:
        return None
    # BIP157 states the bound as a difference and strictly: "the
    # difference between StopHeight and StartHeight MUST be strictly
    # less than" it, so a range of exactly the limit is one too many
    if stop_height - start_height >= limit:
        return None
    return range(start_height, stop_height + 1)


def get_cfilters(node, msg, conn):
    request = GetCFilters.parse(msg)
    heights = _filter_range(
        node,
        request.filter_type,
        request.start_height,
        request.stop_hash,
        MAX_GETCFILTERS_SIZE,
    )
    if heights is None:
        return
    active_chain = node.chainstate.block_index.active_chain
    filter_index = node.chainstate.filter_index
    # "sequentially in order by block height", which is BIP157's own
    # words and the reason this is the one request answered by many
    # messages rather than one
    for height in heights:
        block_hash = active_chain[height]
        conn.send(
            CFilter(
                BlockFilterType.BASIC,
                block_hash,
                filter_index.get_filter(block_hash),
            )
        )


def get_cfheaders(node, msg, conn):
    request = GetCFHeaders.parse(msg)
    heights = _filter_range(
        node,
        request.filter_type,
        request.start_height,
        request.stop_hash,
        MAX_GETCFHEADERS_SIZE,
    )
    if heights is None:
        return
    active_chain = node.chainstate.block_index.active_chain
    filter_index = node.chainstate.filter_index
    start = heights.start
    # the header of the block before the range, which is what the
    # hashes below chain onto; thirty-two zeros where the range starts
    # at the genesis block, BIP157 defining the header before the first
    previous = (
        filter_index.get_header(active_chain[start - 1]) if start else b"\x00" * 32
    )
    conn.send(
        CFHeaders(
            BlockFilterType.BASIC,
            request.stop_hash,
            previous,
            [filter_index.get_filter_hash(active_chain[h]) for h in heights],
        )
    )


def get_cfcheckpt(node, msg, conn):
    request = GetCFCheckpt.parse(msg)
    if request.filter_type != BlockFilterType.BASIC:
        return
    block_index = node.chainstate.block_index
    if request.stop_hash not in block_index.header_dict:
        return
    stop_height = block_index.get_block_info(request.stop_hash).index
    active_chain = block_index.active_chain
    if (
        stop_height >= len(active_chain)
        or active_chain[stop_height] != request.stop_hash
    ):
        return
    filter_index = node.chainstate.filter_index
    # "one entry for each block on the chain terminating in StopHash,
    # where the block height is a multiple of 1,000 greater than 0", so
    # the range starts at the interval and not at zero. No bound: the
    # chain's own length is the bound, which is BIP157's answer too.
    conn.send(
        CFCheckpt(
            BlockFilterType.BASIC,
            request.stop_hash,
            [
                filter_index.get_header(active_chain[height])
                for height in range(
                    CFCHECKPT_INTERVAL, stop_height + 1, CFCHECKPT_INTERVAL
                )
            ],
        )
    )


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
    "getcfilters": get_cfilters,
    "getcfheaders": get_cfheaders,
    "getcfcheckpt": get_cfcheckpt,
    "notfound": not_found,
    "reject": reject,
}
