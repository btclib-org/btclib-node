# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import secrets
import time
from dataclasses import replace
from io import BytesIO
from typing import TYPE_CHECKING

from btclib.amount import valid_sats_amount
from btclib.exceptions import BTClibException, BTClibValueError
from btclib.p2p.address import Addr, ServiceFlags
from btclib.p2p.addrv2 import AddrV2, NetworkAddressV2, SendAddrV2
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
from btclib.p2p.handshake import Verack, Version
from btclib.p2p.inventory import (
    GetData,
    GetHeaders,
    Headers,
    Inv,
    Inventory,
    InventoryType,
    NotFound,
)
from btclib.p2p.keepalive import Ping, Pong
from btclib.p2p.limits import (
    CFCHECKPT_INTERVAL,
    MAX_ADDR_TO_SEND,
    MAX_GETCFHEADERS_SIZE,
    MAX_GETCFILTERS_SIZE,
    MAX_HEADERS_RESULTS,
)
from btclib.p2p.negotiation import FeeFilter, GetAddr, SendHeaders, WtxidRelay

from btclib_node.chainstate.block_index import BlockStatus
from btclib_node.chainstate.filter_index import NO_PREVIOUS_FILTER_HEADER
from btclib_node.constants import NodeStatus, P2pConnStatus, ProtocolVersion
from btclib_node.exceptions import ChainstateInconsistencyError, MissingPrevoutError
from btclib_node.main import verify_mempool_acceptance
from btclib_node.p2p.address import (
    addr_entry,
    can_addrv1,
    ip_and_port,
    peer_from_addr_entry,
)
from btclib_node.p2p.messages.errors import Reject

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.p2p.connection import Connection


def version(node: Node, msg: bytes, conn: Connection) -> None:
    version_msg = Version.parse(msg)

    conn.version_message = version_msg
    # Every refusal below is discouraged, and not only a protocol
    # violation: Core's own discouragement covers "incompatible or
    # broken peers" alike (banman.h, bitcoin/bitcoin@58a7869f86), and a
    # peer stopped here is redialled from the address it dialled or was
    # accepted on, not one a later `verack` may still rewrite (#70).
    # btclib-org/btclib-node#283
    if version_msg.nonce in node.p2p_manager.nonces:  # connection to ourselves
        node.p2p_manager.discourage(conn.address)
        conn.stop()
        return

    # For simplicity we only allow current protocol version
    if version_msg.version < ProtocolVersion:
        node.p2p_manager.discourage(conn.address)
        conn.stop()
        return
    # we only connect to witness nodes
    if not version_msg.services & ServiceFlags.NODE_WITNESS:
        node.p2p_manager.discourage(conn.address)
        conn.stop()
        return
    if (
        not version_msg.services & ServiceFlags.NODE_NETWORK
        and node.status >= NodeStatus.BlockSynced
    ):
        node.p2p_manager.discourage(conn.address)
        conn.stop()
        return

    conn.send(WtxidRelay())
    conn.send(SendAddrV2())
    conn.send(Verack())

    # relay_tx, which is the attribute Connection defines: the name this
    # wrote before was one letter different, so what the peer asked for
    # landed on an attribute nothing reads and the connection's own flag
    # stayed true for its whole life. is_relay_requested and not relay
    # because an absent flag means true, which is BIP37's default and
    # Core's.
    conn.relay_tx = version_msg.is_relay_requested


def verack(node: Node, msg: bytes, conn: Connection) -> None:
    if not conn.version_message or not conn.wtxidrelay_received:
        # a `verack` ahead of the `version`/`wtxidrelay` it depends on:
        # out of handshake order, and discouraged for it (#283)
        node.p2p_manager.discourage(conn.address)
        conn.stop()
        return
    conn.status = P2pConnStatus.Connected
    # out of P2pManager.pending_connections and into connections, the
    # dict every send iterates: btclib-org/btclib-node#131
    node.p2p_manager.promote_connection(conn.id)

    # What a completed handshake is evidence this peer is reachable and
    # listening at, recorded once, here, rather than at the point the
    # connection ends -- so a peer this node refused earlier in the
    # handshake, above, is never recorded at all. An outbound connection
    # is its own evidence: conn.address is what this node dialled, and a
    # socket connecting there already answered. An inbound one only
    # proves the IP; sock_accept's own port is the peer's ephemeral one
    # and nothing this node could ever dial back on, so the port instead
    # is the one the peer's own version names as addr_from, or nothing
    # where addr_from names none. btclib-org/btclib-node#70
    services = conn.version_message.services
    if conn.inbound:
        port = conn.version_message.addr_from.port
        if port:
            address = replace(conn.address, port=port, services=services)
            # conn.address itself moves to the resolved endpoint, and not
            # only the row add_active_address stores it under: manager.py's
            # already_connected still compares conn.address against a
            # draw from this same table, and an inbound connection's own
            # copy would otherwise keep the ephemeral port forever, never
            # matching its own gossiped address and inviting a second,
            # redundant dial-out to a peer this node already holds a
            # connection with.
            conn.address = address
            node.p2p_manager.peer_db.add_active_address(address)
    else:
        address = replace(conn.address, services=services)
        conn.address = address
        node.p2p_manager.peer_db.add_active_address(address)

    conn.send(SendHeaders())
    conn.send(SendCmpct(announce=False, version=1))
    # BIP133's own floor is not sent here: DownloadManager._send_due_feefilters
    # (src/btclib_node/download.py) reaches every connected connection on the
    # very next step(), Connection.next_feefilter_send_time defaulting to
    # 0.0, "never scheduled", the same convention next_inv_send_time
    # already uses -- so a second, special-cased first send here would
    # duplicate rather than precede it. Core does not send one from its
    # own verack handler either: PeerManagerImpl::MaybeSendFeefilter
    # (net_processing.cpp, bitcoin/bitcoin@58a7869f86) is reached from
    # the ordinary per-peer message loop once a peer is
    # fSuccessfullyConnected, not from a one-time handshake action.
    # btclib-org/btclib-node#275
    conn.send_ping()
    conn.send(GetAddr())
    block_locators = node.chainstate.block_index.get_block_locator_hashes()
    conn.send(GetHeaders(ProtocolVersion, block_locators, b"\x00" * 32))
    sockaddr = conn.client.getpeername()
    node.logger.info("Connected to %s", ip_and_port(sockaddr[0], sockaddr[1]))


def wtxidrelay(node: Node, msg: bytes, conn: Connection) -> None:
    conn.wtxidrelay_received = True


def sendaddrv2(node: Node, msg: bytes, conn: Connection) -> None:
    conn.prefer_addressv2 = True


def sendheaders(node: Node, msg: bytes, conn: Connection) -> None:
    # BIP130: an empty payload, so nothing to parse -- the message
    # itself is the request. Core's own handler does the same one
    # thing and nothing else (net_processing.cpp). btclib-org/btclib-node#202
    conn.prefers_headers = True


def ping(node: Node, msg: bytes, conn: Connection) -> None:
    nonce = Ping.parse(msg).nonce
    conn.send(Pong(nonce))


def pong(node: Node, msg: bytes, conn: Connection) -> None:
    nonce = Pong.parse(msg).nonce
    # The read that decides which of ping_sent/ping_nonce apply and the
    # clear that answers it are one step under conn._ping_lock, against
    # Connection.send_ping's own pair of writes on the other thread:
    # unlocked, a send_ping slipped in between this method's own two
    # statements used to clear ping_nonce to 0 out from under a ping
    # send_ping had just sent, discouraging (#283) and dropping a peer
    # for a nonce this node itself changed. btclib-org/btclib-node#357
    with conn._ping_lock:
        ping_sent = conn.ping_sent
        if not ping_sent:
            return
        matched = conn.ping_nonce == nonce
        if matched:
            conn.ping_sent = 0
            conn.ping_nonce = 0
    if not matched:
        # a nonce this node never sent: a protocol violation, and
        # discouraged for it (#283)
        node.p2p_manager.discourage(conn.address)
        conn.stop()
        return
    conn.latency = time.time() - ping_sent


# Core's own MAX_PCT_ADDR_TO_SEND (net_processing.cpp, 58a7869f86):
# answering with the whole table on demand is what an observer mapping
# the network wants, so a getaddr answer is a sample of it instead.
# AddrManImpl::GetAddr_ (src/addrman.cpp, same sha) truncates
# `len * pct // 100` down; `_addresses_to_send` below rounds up instead,
# since a table of a handful of addresses -- every functional test's own
# two-node regtest -- would otherwise be answered with none at all.
# btclib-org/btclib-node#71
_MAX_PCT_ADDR_TO_SEND = 23


def _addresses_to_send(active: list[NetworkAddressV2]) -> list[NetworkAddressV2]:
    """Return what a `getaddr` answers with: a sample, not the table."""
    size = min(MAX_ADDR_TO_SEND, -(-len(active) * _MAX_PCT_ADDR_TO_SEND // 100))
    if size >= len(active):
        return active
    return secrets.SystemRandom().sample(active, size)


# How long a drawn sample is served again rather than redrawn: shared by
# every connection answered in between, not per connection -- the once-
# per-connection flag already stops one peer asking twice, this is what
# stops two peers connecting close together from being handed two
# different draws to compare. Core's own CachedAddrResponse expiration
# (src/net.cpp, 58a7869f86): held for `_ADDR_SAMPLE_LIFETIME` plus a
# fresh random point across `_ADDR_SAMPLE_JITTER` drawn again every time
# the cache is recomputed, rather than a fixed lifetime alone. A refresh
# landing at a predictable wall-clock offset would itself be a signal to
# whatever is scraping this answer over time, the same attacker Core's
# own comment there reasons about for the duration alone -- the cache
# exists to be unpredictable, not merely stable. btclib-org/btclib-node#71
_ADDR_SAMPLE_LIFETIME = 3600 * 21
_ADDR_SAMPLE_JITTER = 3600 * 6


def getaddr(node: Node, msg: bytes, conn: Connection) -> None:
    # Once per connection, matching the flag's own docstring
    # (connection.py): a peer asking in a loop is served the table once
    # rather than once per ask. btclib-org/btclib-node#71
    if conn.answered_getaddr:
        return
    conn.answered_getaddr = True

    peer_db = node.p2p_manager.peer_db
    now = time.time()
    if now >= peer_db.addr_sample_expiration:
        peer_db.addr_sample = _addresses_to_send(peer_db.get_active_addresses())
        # The sample can go on naming an endpoint `active_addresses` has
        # since aged out or dropped, for as long as this cache is still
        # good: intended, not overlooked -- the cache is not what a
        # `getaddr` answer's freshness rests on, an `addr` entry already
        # carries its own timestamp for whoever receives it to judge
        # staleness by, and shortening this lifetime toward the active
        # table's own three-hour window would give back the privacy this
        # cache exists for to buy an accuracy guarantee gossip never
        # promised in the first place.
        jitter = secrets.SystemRandom().uniform(0, _ADDR_SAMPLE_JITTER)
        peer_db.addr_sample_expiration = now + _ADDR_SAMPLE_LIFETIME + jitter
    sample = peer_db.addr_sample
    # either message class, and not whichever the first branch names:
    # Addr and AddrV2 are siblings under Payload rather than one a
    # subclass of the other, so each is built from its own list rather
    # than through a shared name of a type the other could not accept.
    # `_addresses_to_send` already keeps this under MAX_ADDR_TO_SEND, the
    # bound btclib's Addr and AddrV2 refuse a longer message than, so one
    # message is always enough.
    if conn.prefer_addressv2:
        if sample:
            conn.send(AddrV2(sample))
    else:
        # an addr version 1 message has nowhere to put a tor, i2p or
        # cjdns address, so those are left out rather than made up
        entries = [addr_entry(addr) for addr in sample if can_addrv1(addr)]
        if entries:
            conn.send(Addr(entries))


def addr(node: Node, msg: bytes, conn: Connection) -> None:
    # Addr.parse(msg) would refuse an octet past the last address
    # (btclib's own assert_no_trailing, a malleability guard that holds
    # across the library) by raising out of this callback, which
    # main.handle_p2p turns into conn.stop(): a peer dropped for gossip
    # this node could simply not fully read. Core does not: ProcessMessage
    # reads AddrMan-worth of entries out of vRecv and never checks for
    # anything left. Wrapping the payload in a stream is btclib's own
    # answer for exactly this -- assert_no_trailing's docstring calls a
    # stream "the caller's", the same shape a transaction inside a block
    # is read through, with nothing after it checked -- so this reads
    # every address BIP155 defines and silently drops whatever else the
    # peer appended, matching Core's leniency without a second copy of
    # Addr's codec. btclib-org/btclib-node#149
    entries = Addr.parse(BytesIO(msg)).addresses
    # BIP155's record is what the table holds, an addr version 1 entry
    # having no room for the networks a peer may yet gossip
    node.p2p_manager.peer_db.add_addresses(
        peer_from_addr_entry(entry) for entry in entries
    )


def addrv2(node: Node, msg: bytes, conn: Connection) -> None:
    # the same leniency as addr above, and the same reason: BIP155
    # entries fully read, anything past them left unchecked rather than
    # costing the peer its connection. btclib-org/btclib-node#149
    addresses = AddrV2.parse(BytesIO(msg)).addresses
    node.p2p_manager.peer_db.add_addresses(addresses)


def feefilter(node: Node, msg: bytes, conn: Connection) -> None:
    # BIP133: a peer asking not to be told about a transaction paying
    # less. Stored on the connection, the same shape relay_tx above
    # already is; read by DownloadManager.tx_download, through
    # Mempool.meets_fee_rate, against the fee
    # main.verify_mempool_acceptance now hands back and Mempool keeps
    # per transaction. btclib-org/btclib-node#260
    #
    # Core acts on a received rate only within MoneyRange -- 0 to
    # MAX_MONEY inclusive (net_processing.cpp's NetMsgType::FEEFILTER,
    # consensus/amount.h's MoneyRange) -- and leaves a rate outside it
    # parsed but unused. valid_sats_amount is that same range with its
    # upper bound un-exported by name (btclib.amount's own _MAX_SATOSHI),
    # so it is what stands in for MoneyRange here; a rate it refuses
    # is read as no filter, BIP133's and Core's own answer for one that
    # would fail a comparison against any real, non-negative fee anyway.
    try:
        conn.feefilter = valid_sats_amount(FeeFilter.parse(msg).feerate)
    except BTClibValueError:
        conn.feefilter = 0


def tx(node: Node, msg: bytes, conn: Connection) -> None:
    # Core's own reason for the same early return, before it even
    # parses the payload: "we don't have enough information to validate
    # it yet" (net_processing.cpp, MSG_TX) -- the utxo set is still
    # catching up, so a prevout this rejects for lacking may only be
    # missing because sync has not reached it. An unsolicited
    # transaction this early is not a protocol violation there either,
    # so this drops it rather than the peer. btclib-org/btclib-node#129
    if node.status < NodeStatus.BlockSynced:
        return
    tx = TxMsg.parse(msg).tx
    try:
        fee = verify_mempool_acceptance(node, tx)
    except MissingPrevoutError:
        # We don't have the parents in the mempool
        return
    # Queuing this for announcement is gated on `add_tx` actually having
    # added it, and not merely on the pre-call `contains_tx`: `add_tx`
    # is a silent no-op both for a txid already held and for one
    # `Mempool._evict_to_limit` (btclib-org/btclib-node#294) takes right
    # back out for being the worst transaction held once its own add put
    # the mempool past `bytesize_limit` -- and a transaction this node
    # declined to keep is not one to tell every other peer about, a peer
    # that then asks for it getting `notfound` for its trouble.
    # btclib-org/btclib-node#277
    if not node.mempool.contains_tx(tx) and node.mempool.add_tx(tx, fee):
        node.download_manager.received_txs.append((conn.id, tx.hash))


def block(node: Node, msg: bytes, conn: Connection) -> None:
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
        # main.handle_p2p, which drops the peer that sent it. Invalidate
        # first and re-raise, so the next peer offering the same block
        # is refused before it is asked to send it: btclib-org/btclib-node#77
        try:
            block.assert_valid(node.chain.pow_limit_bits)
        except BTClibException:
            node.chainstate.block_index.invalidate(block_hash)
            raise
        node.block_db.add_block(block)
        node.logger.info("Received new block with hash:%s", block_hash.hex())
        node.chainstate.block_index.set_downloaded(block_hash)


def inv(node: Node, msg: bytes, conn: Connection) -> None:
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


def getdata(node: Node, msg: bytes, conn: Connection) -> None:
    getdata = GetData.parse(msg)

    tx_types = (
        InventoryType.MSG_TX,
        InventoryType.MSG_WTX,
        InventoryType.MSG_WITNESS_TX,
    )
    # BIP37's fRelay is written about announcements -- "broadcast
    # transactions will not be announced" -- and says nothing about a
    # transaction a peer asks for by hash. Core answers nothing anyway:
    # with fRelay false and NODE_BLOOM not offered, `ProcessGetData`
    # skips every transaction item outright, and where NODE_BLOOM is
    # offered, `FindTxForGetData` gates on `m_last_inv_sequence`, which
    # never advances for a peer nothing is announced to. This node
    # follows Core rather than the sentence, and the reason is what the
    # sentence does not cover: serving the mempool by hash to a peer
    # that declined announcements answers, for anyone willing to ask,
    # whether a given transaction reached this node -- and a peer that
    # declined is the one with no other reason to be asking.
    #
    # Blocks are not affected. A peer that wants no transactions is
    # still a peer syncing the chain.
    #
    # A hash this node does not hold, once relay is wanted, is answered
    # with `notfound` rather than silence: `FindTxForGetData` returning
    # null is `vNotFound`'s only source in Core's own `ProcessGetData`
    # (src/net_processing.cpp), so a miss is told apart from a peer that
    # is merely slow. The declined-relay peer above gets none of this
    # either, matching Core's own `continue` on that path.
    not_found: list[Inventory] = []
    if conn.relay_tx:
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
            else:
                not_found.append(item)

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
        # else: silence, matching Core -- `ProcessGetBlockData` returns
        # on a block it does not hold with no `notfound` of its own,
        # `vNotFound` being `ProcessGetData`'s own local and never
        # touched by the function it calls out to for a block item.

    if not_found:
        conn.send(NotFound(not_found))


def headers(node: Node, msg: bytes, conn: Connection) -> None:
    headers = Headers.parse(msg).headers
    # add_headers raises on a batch it refuses -- a header failing its
    # own proof of work or context check -- and the raise is left to
    # reach handle_p2p, which drops the connection the same way block's
    # own does: a peer that sent it is not one telling us it has
    # nothing left, and this is not the ordinary end of a sync.
    # btclib-org/btclib-node#75
    block_index = node.chainstate.block_index
    tip = block_index.add_headers(headers)
    if tip is None:
        # a batch connecting to nothing this node knows, whatever its
        # length: get_block_locator_hashes asks from what this node
        # already has, the same request Core's own
        # HandleUnconnectingHeaders sends regardless of batch size
        # (src/net_processing.cpp), rather than a short, BIP130-style
        # announcement being silently dropped for missing its own
        # ancestors. btclib-org/btclib-node#233
        block_locators = block_index.get_block_locator_hashes()
        conn.send(GetHeaders(ProtocolVersion, block_locators, b"\x00" * 32))
    elif len(headers) == MAX_HEADERS_RESULTS:  # the peer may have more to give us
        # [tip] only for a live fork below header_index's own tip: that
        # is the one case get_block_locator_hashes cannot reach on its
        # own, since header_index only moves for a header extending it
        # or beating its chainwork, and a locator built from it would
        # ask for this same batch again and stall short of the fork's
        # own tip. An ordinary batch extending header_index already gets
        # header_index's own richer, multi-entry locator, unchanged; a
        # batch built on a parent this node already proved invalid does
        # too, rather than this node asking the same peer for more of a
        # branch it has already proved bad, with no misbehaviour scoring
        # anywhere in this tree to ever stop it otherwise.
        # btclib-org/btclib-node#122
        if (
            tip != block_index.header_index[-1]
            and block_index.get_block_info(tip).status != BlockStatus.invalid
        ):
            block_locators = [tip]
        else:
            block_locators = block_index.get_block_locator_hashes()
        conn.send(GetHeaders(ProtocolVersion, block_locators, b"\x00" * 32))
    elif node.status == NodeStatus.SyncingHeaders:
        node.status = NodeStatus.HeaderSynced


def getheaders(node: Node, msg: bytes, conn: Connection) -> None:
    getheaders = GetHeaders.parse(msg)
    headers = node.chainstate.block_index.get_headers_from_locators(
        getheaders.locator, getheaders.hash_stop
    )
    if headers:
        conn.send(Headers(headers))


def _height_on_the_active_chain(node: Node, block_hash: bytes) -> int | None:
    """Return the height of a block this node has on its chain, or None.

    Two questions and not one: a hash can be known and not be the block
    the active chain holds at that height, which is what a stop hash
    naming an abandoned branch looks like. Answering the second from
    `header_dict` alone would serve the filters of blocks the peer did
    not ask about.
    """
    block_index = node.chainstate.block_index
    if block_hash not in block_index.header_dict:
        return None
    height = block_index.get_block_info(block_hash).index
    active_chain = block_index.active_chain
    if height >= len(active_chain) or active_chain[height] != block_hash:
        return None
    return height


def _filter_range(
    node: Node,
    filter_type: BlockFilterType | int,
    start_height: int,
    stop_hash: bytes,
    limit: int,
) -> range | None:
    """Return the active-chain heights a BIP157 request names, or None.

    A range is a start height and the hash of the block it ends at, so
    turning it into heights is the one thing `btclib.p2p.block_filters`
    leaves to a caller: only a node holds the chain that says what
    height a hash is at.

    Nothing is sent for a request this cannot answer. BIP157 asks for
    that on the first two counts -- a filter type not supported and a
    StopHash not known are each "SHOULD NOT respond" -- and says nothing
    at all about the third, the range being too long, where Core
    disconnects instead. Silence is a choice there rather than the
    letter of the specification, and it is the same answer as the other
    two because there is no message defined for saying why.
    """
    if filter_type != BlockFilterType.BASIC:
        return None
    stop_height = _height_on_the_active_chain(node, stop_hash)
    if stop_height is None:
        return None
    # BIP157: "The height of the block with hash StopHash MUST be
    # greater than or equal to StartHeight". Only the upper end is
    # checked: the field is unsigned on the wire and these requests are
    # always parsed, so a negative start cannot arrive.
    if start_height > stop_height:
        return None
    # "and the difference MUST be strictly less than 1,000" -- 2,000 for
    # getcfheaders. Strictly, so a range whose ends differ by exactly
    # the bound is one block too many.
    if stop_height - start_height >= limit:
        return None
    return range(start_height, stop_height + 1)


def get_cfilters(node: Node, msg: bytes, conn: Connection) -> None:
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
    # messages rather than one -- and the reason a status worth checking
    # mid-loop exists here and not in get_cfheaders or get_cfcheckpt
    # below, which build their one answer and call conn.send once: a
    # peer whose answer trips Connection's send-buffer bound
    # (MAX_QUEUED_SEND_BYTES, connection.py) partway through is
    # conn.status == P2pConnStatus.Closed already, and every height
    # still to come would otherwise be serialized into a CFilter and
    # scheduled onto a connection nothing more will ever reach.
    for height in heights:
        if conn.status == P2pConnStatus.Closed:
            break
        block_hash = active_chain[height]
        # every block on the active chain is caught up before the node
        # starts listening, and kept up as blocks connect
        block_filter = filter_index.get_filter(block_hash)
        if block_filter is None:
            err_msg = f"no filter for a block on the active chain: {block_hash.hex()}"
            raise ChainstateInconsistencyError(err_msg)
        conn.send(
            CFilter(
                BlockFilterType.BASIC,
                block_hash,
                block_filter,
            )
        )


def get_cfheaders(node: Node, msg: bytes, conn: Connection) -> None:
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
    # hashes below chain onto. BIP157: "The previous filter header used
    # to calculate that of the genesis block is defined to be the
    # 32-byte array of 0's."
    previous = (
        filter_index.get_header(active_chain[start - 1])
        if start
        else NO_PREVIOUS_FILTER_HEADER
    )
    # every block on the active chain is caught up before the node
    # starts listening, and kept up as blocks connect
    if previous is None:
        err_msg = "no filter header for the parent of the requested range"
        raise ChainstateInconsistencyError(err_msg)
    filter_hashes = []
    for h in heights:
        filter_hash = filter_index.get_filter_hash(active_chain[h])
        if filter_hash is None:
            block_hash = active_chain[h]
            err_msg = f"no filter for a block on the active chain: {block_hash.hex()}"
            raise ChainstateInconsistencyError(err_msg)
        filter_hashes.append(filter_hash)
    conn.send(
        CFHeaders(
            BlockFilterType.BASIC,
            request.stop_hash,
            previous,
            filter_hashes,
        )
    )


def get_cfcheckpt(node: Node, msg: bytes, conn: Connection) -> None:
    request = GetCFCheckpt.parse(msg)
    # not _filter_range: this request carries no start height, a
    # checkpoint chain always beginning at the genesis block, so the two
    # refusals it shares are asked for directly and there is no third
    if request.filter_type != BlockFilterType.BASIC:
        return
    stop_height = _height_on_the_active_chain(node, request.stop_hash)
    if stop_height is None:
        return
    active_chain = node.chainstate.block_index.active_chain
    filter_index = node.chainstate.filter_index
    # BIP157: "FilterHeaders MUST have exactly one entry for each block
    # on the chain terminating in StopHash, where the block height is a
    # multiple of 1,000 greater than 0" -- so the range starts at the
    # interval and not at zero, and the stop block is an entry when its
    # own height falls on one. No bound: the chain's length is the
    # bound, which is BIP157's answer too.
    # every block on the active chain is caught up before the node
    # starts listening, and kept up as blocks connect
    checkpoints = []
    for height in range(CFCHECKPT_INTERVAL, stop_height + 1, CFCHECKPT_INTERVAL):
        block_hash = active_chain[height]
        header = filter_index.get_header(block_hash)
        if header is None:
            err_msg = "no filter header for a block on the active chain: "
            err_msg += block_hash.hex()
            raise ChainstateInconsistencyError(err_msg)
        checkpoints.append(header)
    conn.send(
        CFCheckpt(
            BlockFilterType.BASIC,
            request.stop_hash,
            checkpoints,
        )
    )


def not_found(node: Node, msg: bytes, conn: Connection) -> None:
    missing = NotFound.parse(msg)
    # `TxDownloadManagerImpl::ReceivedNotFound`, net_processing.cpp
    # (bitcoin/bitcoin@58a7869f86): a `notfound` for a transaction this
    # node asked for is what tells it the ask will go unanswered, so the
    # peer's own entry in `DownloadManager.tx_download`'s in-flight
    # table (`conn.tx_requested`, which is what keeps that ask from
    # being repeated while it is outstanding) is cleared early rather
    # than sitting there until it would otherwise be overwritten by a
    # fresh one. A block item carries no such bookkeeping to clear here:
    # Core's own `NOTFOUND` handling reads only `IsGenTxMsg` items too,
    # `MSG_BLOCK` never having been requested through a mechanism a
    # `notfound` could complete. btclib-org/btclib-node#144
    for item in missing.items:
        if item.type_code in (
            InventoryType.MSG_TX,
            InventoryType.MSG_WTX,
            InventoryType.MSG_WITNESS_TX,
        ):
            conn.tx_requested.pop(item.hash, None)
    node.logger.warning("Missing objects:%s", missing)


def reject(node: Node, msg: bytes, conn: Connection) -> None:
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
    "sendheaders": sendheaders,
    "getcfilters": get_cfilters,
    "getcfheaders": get_cfheaders,
    "getcfcheckpt": get_cfcheckpt,
    "notfound": not_found,
    "reject": reject,
    "feefilter": feefilter,
}
