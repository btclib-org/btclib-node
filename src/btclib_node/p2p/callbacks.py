# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""One handler per p2p message type, and the two tables that dispatch to them.

`callbacks` is read by `p2p.main.handle_p2p` for a connection past its
handshake; `handshake_callbacks` is read by `p2p.main.handle_p2p_handshake`
for a connection still completing one. Every handler shares the same
signature, `(node, msg, conn)`, whether or not its own body reads every
argument -- the dispatch table calls each one uniformly, and an unread
`msg` or `conn` documents that rather than a mistake.

`advance_getdata` and `advance_cfilters` are the two exceptions to "one
handler, one message": `getdata` and `get_cfilters` below, and
`p2p.main.resume_getdata` and `resume_cfilters`, each call one of them to
pace an answer against the connection's own send queue, across however
many turns of `Node`'s own loop that answer takes to drain.
"""

import secrets
import time
from collections import deque
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
    MAX_INV_SZ,
    MAX_PROTOCOL_MESSAGE_LENGTH,
)
from btclib.p2p.negotiation import FeeFilter, GetAddr, SendHeaders, WtxidRelay

from btclib_node.chainstate.block_index import BlockStatus
from btclib_node.chainstate.filter_index import NO_PREVIOUS_FILTER_HEADER
from btclib_node.constants import (
    MIN_BLOCKS_TO_KEEP,
    NodeStatus,
    P2pConnStatus,
    ProtocolVersion,
)
from btclib_node.exceptions import ChainstateInconsistencyError, MissingPrevoutError
from btclib_node.main import verify_mempool_acceptance
from btclib_node.p2p.address import (
    addr_entry,
    can_addrv1,
    ip_and_port,
    peer_from_addr_entry,
)
from btclib_node.p2p.filter_size import ONE_BUSY_MODERN_BLOCK_FILTER_BYTES
from btclib_node.p2p.messages.errors import Reject

if TYPE_CHECKING:
    from btclib_node import Node
    from btclib_node.p2p.connection import Connection

__all__ = [
    "MAX_CFILTERS_INFLIGHT_BYTES",
    "MAX_GETDATA_INFLIGHT_BYTES",
    "MAX_PENDING_CFILTERS_HEIGHTS",
    "MAX_PENDING_GETDATA_ITEMS",
    "addr",
    "addrv2",
    "advance_cfilters",
    "advance_getdata",
    "block",
    "callbacks",
    "feefilter",
    "get_cfcheckpt",
    "get_cfheaders",
    "get_cfilters",
    "getaddr",
    "getdata",
    "getheaders",
    "handshake_callbacks",
    "headers",
    "inv",
    "not_found",
    "ping",
    "pong",
    "reject",
    "sendaddrv2",
    "sendheaders",
    "tx",
    "verack",
    "version",
    "wtxidrelay",
]


def version(node: Node, msg: bytes, conn: Connection) -> None:
    """Handle a peer's `version`: refuse an incompatible peer, else continue.

    A second `version` ahead of this connection's own `verack` is
    ignored outright -- Core's own guard, `pfrom.nVersion != 0`
    (`net_processing.cpp:3823`, at bitcoin/bitcoin@5f45583e43), which
    logs and returns before doing anything else. `conn.status` stays
    `Open` until `verack` promotes it, so #283's own discourage-and-drop
    for a handshake command out of order never reaches a repeat sent
    before that point -- unguarded, every repeat would resend
    `WtxidRelay`, `SendAddrV2` and `Verack` in answer.
    btclib-org/btclib-node#482

    Continuing means answering `wtxidrelay`, `sendaddrv2` and `verack`,
    and recording whether the peer asked to have transactions relayed.
    """
    if conn.version_message is not None:
        return
    version_msg = Version.parse(msg)

    conn.version_message = version_msg
    # Every refusal below is discouraged, and not only a protocol
    # violation: Core's own discouragement covers "incompatible or
    # broken peers" alike (banman.h, at bitcoin/bitcoin@58a7869f86), and a
    # peer stopped here is redialled from the address it dialled or was
    # accepted on, not one a later `verack` may still rewrite (#70).
    # btclib-org/btclib-node#283
    #
    # `is_self_connect_nonce` replaces a fixed-size ring of recently
    # sent nonces, which a burst of outbound connects could evict a
    # still-outstanding attempt's own nonce from before its `version`
    # came back (btclib-org/btclib-node#448) -- its own docstring is
    # where the search it runs is argued against Core's.
    if node.p2p_manager.is_self_connect_nonce(version_msg.nonce):
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
    """Complete a peer's handshake: promote it and send the follow-up messages.

    Refuses a `verack` ahead of its own `version`/`wtxidrelay`, and
    records the peer's own address as reachable once promoted -- the
    comment below is where that recording is argued.
    """
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
    # (net_processing.cpp, at bitcoin/bitcoin@58a7869f86) is reached from
    # the ordinary per-peer message loop once a peer is
    # fSuccessfullyConnected, not from a one-time handshake action.
    # btclib-org/btclib-node#275
    conn.send_ping()
    conn.send(GetAddr())
    block_locators = node.chainstate.block_index.get_block_locator_hashes()
    conn.send(GetHeaders(ProtocolVersion, block_locators, b"\x00" * 32))
    sockaddr = conn.client.getpeername()
    # the connection id beside the address, once, is what makes the
    # id-keyed lines everywhere else resolvable back to a peer -- #526's
    # own four verdict lines among them. Core pairs them the other way
    # round and only on request: `CNode::LogPeer` (`src/net.cpp`,
    # at bitcoin/bitcoin@05e49b342f) writes `peer=%d` alone and appends
    # `peeraddr=` only under `fLogIPs`, whose default is off. This tree
    # logs the address here already, so withholding the id bought no
    # privacy and only cost the correlation. What this line marks is the
    # handshake completing, not the pairing: `P2pManager.create_connection`
    # logs the same id beside the same address as soon as the connection
    # exists, which is what makes an id resolvable for a handshake that
    # never gets this far (btclib-org/btclib-node#611)
    node.logger.info(
        "Connected to %s, connection %s",
        ip_and_port(sockaddr[0], sockaddr[1]),
        conn.id,
    )


def wtxidrelay(node: Node, msg: bytes, conn: Connection) -> None:
    """Record that the peer relays transactions by wtxid (BIP339)."""
    conn.wtxidrelay_received = True


def sendaddrv2(node: Node, msg: bytes, conn: Connection) -> None:
    """Record that the peer wants `addrv2` gossip rather than `addr`."""
    conn.prefer_addressv2 = True


def sendheaders(node: Node, msg: bytes, conn: Connection) -> None:
    """Record that the peer wants new blocks announced as headers (BIP130)."""
    # BIP130: an empty payload, so nothing to parse -- the message
    # itself is the request. Core's own handler does the same one
    # thing and nothing else (net_processing.cpp). btclib-org/btclib-node#202
    conn.prefers_headers = True


def ping(node: Node, msg: bytes, conn: Connection) -> None:
    """Answer a `ping` with a `pong` carrying the same nonce."""
    nonce = Ping.parse(msg).nonce
    conn.send(Pong(nonce))


def pong(node: Node, msg: bytes, conn: Connection) -> None:
    """Match a `pong` to the outstanding `ping` and record the round trip.

    A nonce that does not match the one this node last sent is a
    protocol violation, discouraged and dropped rather than matched.
    """
    nonce = Pong.parse(msg).nonce
    # The read that decides which of ping_sent/ping_nonce apply and the
    # clear that answers it are one step under conn._ping_lock, against
    # Connection.send_ping's own pair of writes on the other thread:
    # unlocked, a send_ping slipped in between this method's own two
    # statements used to clear ping_nonce to 0 out from under a ping
    # send_ping had just sent, discouraging (#283) and dropping a peer
    # for a nonce this node itself changed. btclib-org/btclib-node#357
    with conn._ping_lock:  # noqa: SLF001 -- the comment above is why
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
    """Answer a peer's `getaddr` with a sample of known addresses, once.

    The sample itself is a cache, shared and redrawn only once its own
    lifetime and jitter expire -- the comment below argues why.
    """
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
    """Merge the addr-version-1 entries a peer gossiped into the table."""
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
    """Merge the BIP155 entries a peer gossiped into the address table."""
    # the same leniency as addr above, and the same reason: BIP155
    # entries fully read, anything past them left unchecked rather than
    # costing the peer its connection. btclib-org/btclib-node#149
    addresses = AddrV2.parse(BytesIO(msg)).addresses
    node.p2p_manager.peer_db.add_addresses(addresses)


def feefilter(node: Node, msg: bytes, conn: Connection) -> None:
    """Record the peer's own BIP133 minimum feerate, or none if invalid."""
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
    """Validate an unsolicited transaction and queue it for announcement.

    A no-op before this node's own chain is synced, or if the mempool
    already holds it, or if `add_tx` itself declines to keep it.
    """
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
    """Store a requested block once its proof of work checks out.

    A no-op if this block is already marked downloaded. Invalidates it
    first and re-raises on a failed check, so the next peer offering
    the same block is refused before being asked for it.

    An unsolicited block whose own header this node has never indexed
    is not read as though `getdata` or `headers` already vouched for
    it: `PeerManagerImpl::ProcessMessage`'s own `NetMsgType::BLOCK` arm
    (`net_processing.cpp`, at bitcoin/bitcoin@ca7162cde5) runs every
    block through `ChainstateManager::AcceptBlock`, which calls
    `AcceptBlockHeader` (`validation.cpp`, same sha) on the block's own
    header before anything else -- a header already known is accepted
    outright, and one that is not has its own parent looked up, refused
    with `BLOCK_MISSING_PREV` where that parent is unknown too. Core
    punishes that refusal: `MaybePunishNodeForBlock`'s own switch
    (`net_processing.cpp`, same sha) calls `Misbehaving` for
    `BLOCK_MISSING_PREV`, unlike an unconnecting *headers* batch, which
    `ProcessHeadersMessage`'s own `HandleUnconnectingHeaders` answers by
    asking for more rather than by punishing -- the same asymmetry this
    file already carries between `headers` below, which never
    discourages a batch connecting to nothing this node knows
    (btclib-org/btclib-node#233), and this function, which does.
    `block_index.add_headers([block.header])` is `AcceptBlockHeader`'s
    own shape: it indexes the header where the parent is known, raises
    a `BTClibException` where the header itself is invalid -- `main.
    handle_p2p`'s own `except` already drops and discourages the peer
    for either, the same way it already does for a block failing its
    own proof of work below -- and, for a single header whose parent is
    missing, returns `None` rather than raising, which is `headers`'s
    own "ask again" case and not this one's: `BTClibValueError` is
    raised here instead, for `main.handle_p2p`'s same `except` to
    discourage the peer over, matching `Misbehaving`.
    btclib-org/btclib-node#711
    """
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

    block_index = node.chainstate.block_index
    if (
        block_hash not in block_index.header_dict
        and block_index.add_headers([block.header]) is None
    ):
        err_msg = (
            f"block {block_hash.hex()} has prev block not found: "
            f"{block.header.previous_block_hash.hex()}"
        )
        raise BTClibValueError(err_msg)

    block_info = block_index.get_block_info(block_hash)

    if not block_info.downloaded:
        # a block that does not hold up is nobody's: the raise reaches
        # main.handle_p2p, which drops the peer that sent it. Invalidate
        # first and re-raise, so the next peer offering the same block
        # is refused before it is asked to send it: btclib-org/btclib-node#77
        try:
            block.assert_valid(node.chain.pow_limit_bits)
        except BTClibException:
            block_index.invalidate(block_hash)
            raise
        node.block_db.add_block(block)
        node.logger.info("Received new block with hash:%s", block_hash.hex())
        block_index.set_downloaded(block_hash)


def inv(node: Node, msg: bytes, conn: Connection) -> None:
    """Ask for headers behind an announced block, queue missing transactions.

    A no-op before this node's own chain is synced.
    """
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


# The two families `advance_getdata` below dispatches on -- everything
# else a `getdata` may name (`MSG_FILTERED_BLOCK`, `MSG_CMPCT_BLOCK`,
# `UNDEFINED`, an unrecognised code) is neither, and is popped off the
# front of the pending items and otherwise ignored, the same silence
# `_filter_range` already answers a request it declines with elsewhere
# in this module.
_GETDATA_TX_TYPES = (
    InventoryType.MSG_TX,
    InventoryType.MSG_WTX,
    InventoryType.MSG_WITNESS_TX,
)
_GETDATA_BLOCK_TYPES = (InventoryType.MSG_BLOCK, InventoryType.MSG_WITNESS_BLOCK)

# Room to schedule ahead of a peer's own draining before `advance_getdata`
# pauses and hands the rest to `node.pending_getdata`, for `resume_getdata`
# (`p2p.main`) to finish -- the same idea `MAX_CFILTERS_INFLIGHT_BYTES`
# below applies to `get_cfilters`, sized against a `getdata` answer's own
# largest item instead of a filter's: a block, up to
# `MAX_PROTOCOL_MESSAGE_LENGTH`. Twice that is the same "one draining, one
# already serialized behind it" margin `MAX_CFILTERS_INFLIGHT_BYTES` gives
# a filter, scaled to this answer's own larger item.
#
# Core's own analogue, `ProcessGetData`'s "only process one BLOCK item per
# call" (`net_processing.cpp:2798`, at bitcoin/bitcoin@b91d983f66), is a
# hard count instead of a byte bound, because Core's own next call is
# `ProcessMessages` looping back over every connection regardless of what
# this one has queued. A byte bound reproduces the same shape without a
# second, item-type-specific count to keep in step with
# `MAX_QUEUED_SEND_BYTES`
# (`connection.py`): a transaction item is cheap and small, so many of
# them fit under this bound in one pass, matching Core's own "process as
# many TX items as possible" (`:2772`, checked against `fPauseSend`
# before each one, `:2776`); a block item is large enough on its own that
# one or two exhaust it, without this function ever counting block items
# by hand the way Core's own count does.
MAX_GETDATA_INFLIGHT_BYTES = int(2 * MAX_PROTOCOL_MESSAGE_LENGTH)

# What one entry costs inside a `notfound`, read off `Inventory.serialize`
# rather than hardcoded: a type code (four octets) and a hash (thirty-two),
# fixed width whatever the entry names. `advance_getdata` below sums this
# over every miss it has collected but not yet sent, so that a `notfound`
# still being assembled counts against `MAX_GETDATA_INFLIGHT_BYTES` the
# same way a block or a transaction already sent does -- nothing else made
# a miss cost anything, and btclib-org/btclib-node#529 is a peer dropped by
# a `notfound` for exactly that reason: every item this call could not
# serve, batched with no pacing check in front of the send.
#
# `Message`'s own envelope and the `var_int` length prefix ahead of the
# entries are both left out of this per-item figure: at
# `MAX_GETDATA_INFLIGHT_BYTES`'s own scale (megabytes), the few dozen
# octets either adds is immaterial to when the check below trips -- the
# same magnitude argument this function's own docstring already makes
# about a missed `queued_send_bytes` increment being one ping's worth
# against a whole block's.
_NOTFOUND_ITEM_BYTES = len(Inventory().serialize())


def _notfound_pace(
    conn: Connection, not_found_bytes: int, not_found_len: int
) -> tuple[bool, bool]:
    """Answer whether `advance_getdata`'s pending batch should flush or pause.

    Pulled out of the loop below rather than inlined, alongside
    `_serve_getdata_item`: `advance_getdata` itself is what ruff's own
    complexity check counts branches against, and every `if` moved into
    a helper is one fewer counted there, whichever helper it lands in.
    Neither of the two questions here reads or writes anything the loop
    itself needs to -- both are pure functions of the three numbers a
    caller already has to hand. "Flush" wins over "pause" where both
    would otherwise apply: sending what is already owed, even while
    over budget, is what lets the very next check see a `not_found`
    that is empty again, rather than looping on the same decision.

    **This pacing has no counterpart in Bitcoin Core, and the reason is
    a difference in what a full send buffer does.** Core's
    `ProcessGetData` (`src/net_processing.cpp`,
    at bitcoin/bitcoin@05e49b342f) checks `pfrom.fPauseSend` before every
    item, hit or miss, but a miss only does `vNotFound.push_back` and
    `fPauseSend` is driven by `m_send_memusage` -- bytes already handed
    to the transport -- so a request answered entirely in misses costs
    that signal nothing, and the `notfound` at the end of the loop is
    pushed unconditionally. Core can afford that: `nSendBufferMaxSize`
    is only ever read to set `fPauseSend`, which makes the node stop
    *reading* from that peer, and nothing anywhere disconnects on send
    volume. This tree's `MAX_QUEUED_SEND_BYTES`
    (`p2p/connection.py`) does disconnect, so the same unbounded batch
    that merely pauses Core drops an honest peer here
    (btclib-org/btclib-node#529). The pause point is what this tree owes
    for having that bound at all; it is not a rule Core has and this
    tree was missing.
    """
    over_budget = conn.queued_send_bytes + not_found_bytes >= MAX_GETDATA_INFLIGHT_BYTES
    should_flush = bool(not_found_len) and (over_budget or not_found_len >= MAX_INV_SZ)
    return should_flush, over_budget


def _below_prune_threshold(node: Node, block_hash: bytes) -> bool:
    """Whether `block_hash` falls more than `MIN_BLOCKS_TO_KEEP` behind the tip.

    Core's own `ProcessGetBlockData` (`net_processing.cpp`, at
    bitcoin/bitcoin@ca7162cde5): "Avoid leaking prune-height by never
    sending blocks below the NODE_NETWORK_LIMITED threshold", checked
    against `peer.m_our_services` -- what this node told the requesting
    peer during its own handshake -- rather than what is actually still
    on disk, and fired whether or not the block asked for happens to
    still be there: a pruned node that still holds it this once is not
    to be relied on for it the next time either. Every connection of a
    pruned node is told the same `NODE_NETWORK_LIMITED`-only services
    (`connection.py`'s own `send_version`, gated on `Config.pruned`
    the identical way), so this reads `node.config.pruned` directly
    rather than a per-connection record of what was sent. `+ 2` is
    Core's own buffer, "for possible races". Answers `False` for a hash
    this index has never indexed, matching Core's own `if (!pindex)
    return;` immediately above the check this mirrors.
    """
    block_index = node.chainstate.block_index
    block_info = block_index.header_dict.get(block_hash)
    if block_info is None:
        return False
    tip_height = len(block_index.active_chain) - 1
    return tip_height - block_info.index > MIN_BLOCKS_TO_KEEP + 2


def _serve_getdata_item(
    node: Node,
    conn: Connection,
    item: Inventory,
    not_found: list[Inventory],
    not_found_bytes: int,
) -> int:
    """Serve one popped item, appending a miss to `not_found` in place.

    The other half of `advance_getdata`'s own body pulled out for the
    same reason `_notfound_pace` above was: what item type dispatches to
    what answer does not need to be inline for the loop around it to
    read correctly, and keeping it out is what holds `advance_getdata`
    itself under ruff's own complexity bound. Returns the running
    `not_found_bytes` total, grown by `_NOTFOUND_ITEM_BYTES` on a miss
    and left alone otherwise -- `not_found` itself is mutated in place,
    a `list` being one of the few values this tree passes that way
    rather than returning a new one, since the caller's own loop already
    holds no other reference to it worth preserving unmutated.
    """
    if item.type_code in _GETDATA_TX_TYPES:
        if not conn.relay_tx:
            return not_found_bytes
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
            not_found_bytes += _NOTFOUND_ITEM_BYTES
    elif item.type_code in _GETDATA_BLOCK_TYPES:
        if node.config.pruned and _below_prune_threshold(node, item.hash):
            conn.stop()
            return not_found_bytes
        block = node.block_db.get_block(item.hash)
        if block:
            include_witness = item.type_code == InventoryType.MSG_WITNESS_BLOCK
            conn.send(
                BlockMsg(block, include_witness=include_witness, check_validity=False)
            )
    # else: neither family, popped and otherwise ignored -- see the
    # comment beside _GETDATA_TX_TYPES above.
    return not_found_bytes


def advance_getdata(node: Node, conn: Connection, items: deque[Inventory]) -> bool:
    """Serve from the front of `items` while `conn`'s own queue has room.

    Shared by `getdata` below, dispatching a request for the first time,
    and by `p2p.main.resume_getdata`, retrying one already paused -- each
    pops what it serves off the front of the same `deque`, the shape
    `advance_cfilters` below already gives `get_cfilters`.

    A transaction is served from the mempool only if the peer wants it
    relayed, answered `notfound` on a miss; a requested block not held
    is silent. Both match Core -- BIP37's `fRelay` is written about
    announcements, "broadcast transactions will not be announced", and
    says nothing about a transaction a peer asks for by hash, but Core
    answers nothing anyway: with `fRelay` false and `NODE_BLOOM` not
    offered, `ProcessGetData` skips every transaction item outright, and
    where `NODE_BLOOM` is offered, `FindTxForGetData` gates on
    `m_last_inv_sequence`, which never advances for a peer nothing is
    announced to. This node follows Core rather than the sentence, and
    the reason is what the sentence does not cover: serving the mempool
    by hash to a peer that declined announcements answers, for anyone
    willing to ask, whether a given transaction reached this node -- and
    a peer that declined is the one with no other reason to be asking.
    Blocks are not affected: a peer that wants no transactions is still
    a peer syncing the chain. A block this node does not hold gets no
    `notfound` either: `ProcessGetBlockData` returns on one with no
    `notfound` of its own, `vNotFound` being `ProcessGetData`'s own local
    and never touched by the function it calls out to for a block item.
    `_below_prune_threshold`'s own docstring is where a pruned node's
    other answer to a block item -- disconnecting rather than staying
    silent -- is argued against the same function.

    `conn.queued_send_bytes` is read the same way `advance_cfilters`
    below reads it, and holds what `conn.send` has counted -- this
    loop's own previous items among them, since it counts on this
    thread before scheduling anything on `P2pManager`'s. A check
    reading only what that loop had got round to writing would see none
    of them and serve the whole request as fast as it can pop it, past
    `MAX_QUEUED_SEND_BYTES` and into the drop, for a peer asking for
    the blocks this node asks its own peers for
    (btclib-org/btclib-node#512).

    What the read can miss is either half of a count it did not make. A
    drain is the loop's -- `conn.send` counts on this thread, but the
    decrement once the write completes is not -- and that direction is
    the safe one, an unseen decrement making the number too large and
    this pause sooner. An increment can also be missed, and that one is
    not: `P2pManager`'s thread reaches `_queue` too, through
    `_prune_stale_connections`'s `send_ping`, so a read here can predate
    a ping and pause later rather than sooner. What makes that
    immaterial is the magnitude rather than the direction: one ping is a
    bare envelope and a nonce, where what `MAX_QUEUED_SEND_BYTES` leaves
    above this loop's own bound is a whole block message and the room
    over it (`connection.py`) -- so the read needs no lock, and a torn
    one is not a risk to guard against either (CPython never hands back
    a value that was not, at some point, actually written).

    `notfound` batches whatever this call found missing, sent once this
    call is done serving -- whether `items` ran out or this paused --
    rather than once for the whole original request: Core's own
    `vNotFound` is a per-call local too, built and sent fresh by every
    `ProcessGetData` call rather than carried across them.

    **A miss is paced too, against the same bound, though nothing is
    sent for one the moment it is found.** `not_found_bytes` is this
    call's own running total of what a `notfound` batching every miss
    collected so far would cost -- `_NOTFOUND_ITEM_BYTES` per entry,
    counted the instant a miss joins `not_found` rather than once the
    batch is finally sent. Read together with `conn.queued_send_bytes`
    at the top of the loop, it is what makes a run of misses pause the
    same way a run of blocks already does, rather than accumulating
    for free and landing in one send with no pacing check in front of
    it (btclib-org/btclib-node#529): before this, nothing charged a
    miss anything, so `conn.queued_send_bytes` could still read zero
    after fifty thousand of them, and the loop had no reason to stop
    before popping every item this request named.

    A batch is also flushed -- sent and reset, without pausing the
    call -- once it reaches `MAX_INV_SZ` on its own, whatever
    `conn.queued_send_bytes` reads: `NotFound.assert_valid` refuses
    more entries than that, and `node.pending_getdata` can hand this
    function a backlog of `MAX_PENDING_GETDATA_ITEMS` (`getdata` below),
    twice `MAX_INV_SZ`, drawn from two stacked requests rather than the
    one this bound was sized against. All of that many being misses is
    an entirely mundane way to reach it -- every hash in both requests
    having left the mempool between the first `getdata` and the second
    is enough -- and at `_NOTFOUND_ITEM_BYTES` apiece the byte bound
    above alone would let it happen: the whole backlog's own worth of
    misses is still short of `MAX_GETDATA_INFLIGHT_BYTES`. Chunking on
    the item count this class already enforces is what keeps that
    backlog from reaching `NotFound`'s own constructor as one batch
    that raises instead of one this connection can be paced on.
    """
    not_found: list[Inventory] = []
    not_found_bytes = 0
    while items:
        if conn.status == P2pConnStatus.Closed:
            return True
        should_flush, should_pause = _notfound_pace(
            conn, not_found_bytes, len(not_found)
        )
        if should_flush:
            conn.send(NotFound(not_found))
            not_found = []
            not_found_bytes = 0
            continue
        if should_pause:
            break
        item = items.popleft()
        not_found_bytes = _serve_getdata_item(
            node, conn, item, not_found, not_found_bytes
        )
    if not_found:
        conn.send(NotFound(not_found))
    return not items


# How many items one connection's own entry on `node.pending_getdata`
# may hold at once, `getdata` below extending an existing one rather
# than answering a second `getdata` that arrives while the first is
# still paused -- sized the way `MAX_PENDING_CFILTERS_HEIGHTS` above
# is: two full requests, `MAX_INV_SZ` apiece, `GetData.parse` already
# bounding any one message to that many. `getdata`'s own docstring below
# is where this tree's own need for a numeric cap here, where Core's
# real protection is not one, is argued.
#
# Unlike `MAX_PENDING_CFILTERS_HEIGHTS`'s own plain `int`s, an `Inventory`
# is not negligible to hold: measured directly in this tree's own venv,
# `tracemalloc` gives roughly 161 bytes per live instance, so this bound's
# own 100,000 items cost roughly 16.1 MB of interpreter memory per
# connection -- the same order as `MAX_QUEUED_SEND_BYTES` itself, not two
# orders of magnitude below it the way the cfilters analogy alone would
# suggest.
MAX_PENDING_GETDATA_ITEMS = 2 * MAX_INV_SZ


def getdata(node: Node, msg: bytes, conn: Connection) -> None:
    """Answer a peer's request for the transactions and blocks it named.

    `advance_getdata` above is where every item is actually served, and
    where this request's own place in Core's `getdata` semantics is
    argued; this is only where a fresh request joins whatever this
    connection has not yet finished serving.

    A second `getdata` arriving while `conn`'s own entry on
    `node.pending_getdata` is still paused extends the same `deque`
    rather than replacing it, up to `MAX_PENDING_GETDATA_ITEMS` -- past
    which a third stacked request is silent, the same answer
    `get_cfilters` below already gives a request past its own
    `MAX_PENDING_CFILTERS_HEIGHTS`, and for the same reason: dropping
    the connection over pipelining this node already tolerates
    elsewhere would be disproportionate to what tripped it, and
    `MAX_QUEUED_SEND_BYTES` (`connection.py`) is still underneath this
    to catch a peer that is actually abusive.

    Core's own protection here is not a numeric cap either, whatever
    reading only `Peer.m_getdata_requests` (appended to at
    `net_processing.cpp:4472`) suggests. `ProcessMessages`
    (`net_processing.cpp:5429-5436`, at bitcoin/bitcoin@b91d983f66) is
    where it actually lives: "this maintains the order of responses and
    prevents m_getdata_requests to grow unbounded", by returning before
    `PollMessage` -- the call that reads this connection's own next
    message off the wire -- whenever `m_getdata_requests` is still
    non-empty, and again whenever `fPauseSend` is set. Core therefore
    never backlogs more than one request's own `MAX_INV_SZ` items per
    connection: it simply stops reading that connection's next message,
    `getdata` included, until the current one has drained.

    That discipline does not port here without a larger redesign:
    `P2pManager.messages` (`p2p/manager.py`) is one `deque` shared by
    every connection, and `handle_p2p` (`p2p/main.py`) pops one message
    off its front regardless of which connection sent it, where Core's
    own `m_getdata_requests` and `PollMessage` are both per connection
    to begin with -- there is no single connection this node could
    "stop reading from" without reordering that shared queue or giving
    each connection a backlog of its own. `MAX_PENDING_GETDATA_ITEMS`
    above is this tree's own bound in place of that redesign.
    """
    getdata = GetData.parse(msg)
    existing = node.pending_getdata.get(conn.id)
    if existing is None:
        items = deque(getdata.items)
    else:
        _, items = existing
        if len(items) + len(getdata.items) > MAX_PENDING_GETDATA_ITEMS:
            return
        items.extend(getdata.items)
    if not advance_getdata(node, conn, items):
        node.pending_getdata[conn.id] = (conn, items)


def headers(node: Node, msg: bytes, conn: Connection) -> None:
    """Index a batch of headers, ask for more, or mark header sync finished.

    A batch connecting to nothing known asks again from what this node
    already has; a full-sized batch asks for the next one; a shorter
    batch that still connected means the peer has nothing more to give,
    which is what finishes header sync.
    """
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
    """Answer a peer's `getheaders` with what its own locator resolves to.

    Silent where the locator names nothing this node's own `header_index`
    holds -- there is nothing to answer with, not a refusal.
    """
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


# Where `get_cfilters` below pauses mid-answer rather than scheduling
# the rest of a range in one go, the way it used to
# (btclib-org/btclib-node#442): Core's own analogue is `fPauseSend`
# (`net.cpp:4205`, read at b91d983f66), tripped once a connection's own
# send buffer passes `-maxsendbuffer` and cleared as the socket drains
# (`net.cpp:1677`) -- checked, and re-checked, from `ProcessMessages`'s
# own loop over each connection's queued work (`net_processing.cpp:2776`),
# which is one thread calling back into the same connection repeatedly.
#
# This node has no such loop to call back into: `get_cfilters` is one
# call, made once, on `Node`'s own thread under `handle_p2p`, and what
# it could not finish it has no second chance at from inside itself.
# Core's "next call" is therefore not `get_cfilters` called again: it is
# `resume_cfilters` (`p2p.main`), invoked once every pass of `Node`'s
# own loop regardless of whether this connection has sent anything
# meanwhile, since a peer already served everything it asked for need
# not ask again for this node to keep answering it. `advance_cfilters`
# below is the one piece of logic both `get_cfilters` and
# `resume_cfilters` call, so the pacing is the same whichever of the two
# resumes it.
#
# `MAX_CFILTERS_INFLIGHT_BYTES` is the pause point itself: how far ahead
# of a peer's own draining `advance_cfilters` is allowed to schedule
# before it stops and hands the rest to `node.pending_cfilters`, for
# `resume_cfilters` to pick up. Twice one busy modern block's own filter
# (`filter_size.ONE_BUSY_MODERN_BLOCK_FILTER_BYTES`, the same estimate
# `connection.py`'s own `MAX_QUEUED_SEND_BYTES` is sized from) is room
# for one filter to finish draining and a second, already serialized,
# to be on its way behind it -- far below `MAX_QUEUED_SEND_BYTES`
# itself, which is what makes this a real pause rather than the whole
# answer the bound it used to lean on already was.
MAX_CFILTERS_INFLIGHT_BYTES = int(2 * ONE_BUSY_MODERN_BLOCK_FILTER_BYTES)

# How many heights one connection's own entry on `node.pending_cfilters`
# may hold at once, `get_cfilters` extending an existing one rather than
# answering a second `getcfilters` that arrives while the first is still
# paused. Core has nothing here to diverge from: `ProcessGetCFilters`
# (`net_processing.cpp:3556`, b91d983f66) calls `LookupFilterRange` and
# pushes every filter it returns in one call, with no pending state of
# its own to collide with a second `getcfilters` from the same peer --
# each is answered to completion, in turn, before the next is looked at,
# relying only on `nSendBufferMaxSize`/`fPauseSend` to bound how much of
# that can queue at the socket. This node's own pause point is per
# request rather than per byte queued at the socket, so it needs a bound
# of its own kind, and BIP157 says nothing about how many `getcfilters`
# one connection may have outstanding at once for a reader to diverge
# from either. Two full requests -- `MAX_GETCFILTERS_SIZE` apiece -- is
# the room this bound gives on its own terms: enough for a `getcfilters`
# already draining and a second one the same peer sends before the first
# finishes to both extend the one pending entry, rather than have either
# dropped. Past it, a third stacked request is silence -- `_filter_range`
# below already answers this way for a request it declines on other
# grounds, and a peer pipelining past what two full answers cover is the
# same kind of request: one this node will not serve, with no refusal
# message BIP157 defines to send instead. This file has a second idiom
# for "won't serve", not just `_filter_range`'s: `MAX_QUEUED_SEND_BYTES`
# drops the connection outright, for a capacity refusal much like this
# one rather than a protocol-validity check. Silence is preferred here
# because that byte bound is still underneath this one to catch a peer
# that is actually abusive; dropping the connection over ordinary
# pipelining this node already tolerates elsewhere would be
# disproportionate to what tripped it.
MAX_PENDING_CFILTERS_HEIGHTS = 2 * MAX_GETCFILTERS_SIZE


def advance_cfilters(node: Node, conn: Connection, heights: deque[int]) -> bool:
    """Send from the front of `heights` while `conn`'s own queue has room.

    Shared by `get_cfilters`, dispatching a request for the first time,
    and by `p2p.main.resume_cfilters`, retrying one already paused --
    each pops what it sends off the front of the same `deque`, so a
    later call, on a later turn of `Node`'s own loop, picks up exactly
    where the last one left off rather than resending or skipping a
    height. Answers whether `heights` is now empty.

    Checked before every send rather than after, against the same field
    `advance_getdata` above paces on, unlocked for the reason argued
    there: `conn.send` counts a filter on this thread before scheduling
    it, and what the read can still miss is a drain, which only ever
    makes this pause sooner. `conn.status` beside it is read the same
    way: seen one turn late it costs a filter serialized for a socket
    already closed, which `Connection._send` suppresses.
    """
    active_chain = node.chainstate.block_index.active_chain
    filter_index = node.chainstate.filter_index
    while heights:
        if conn.status == P2pConnStatus.Closed:
            return True
        if conn.queued_send_bytes >= MAX_CFILTERS_INFLIGHT_BYTES:
            return False
        height = heights.popleft()
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
    return True


def get_cfilters(node: Node, msg: bytes, conn: Connection) -> None:
    """Answer a BIP157 `getcfilters` with one `cfilter` per requested height.

    Silent on a request `_filter_range` refuses. "sequentially in order
    by block height" is BIP157's own words and the reason this is the
    one request answered by many messages rather than one; `_filter_range`
    already bounds how many, and `advance_cfilters` above is where the
    rate they are produced at is bounded too, registering what it could
    not finish on `node.pending_cfilters` for `p2p.main.resume_cfilters`
    to complete.

    A second `getcfilters` arriving while `conn`'s own entry there is
    still paused extends that same `deque` rather than replacing it --
    `MAX_PENDING_CFILTERS_HEIGHTS`, beside `advance_cfilters` above, is
    where that bound and the reasoning behind it are. `_filter_range`
    has already validated and bounded this request's own range before
    that check runs, so what is refused there is refused whole: no
    partial answer is ever started for a range this node will not
    finish.
    """
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
    existing = node.pending_cfilters.get(conn.id)
    if existing is None:
        pending = deque(heights)
    else:
        _, pending = existing
        if len(pending) + len(heights) > MAX_PENDING_CFILTERS_HEIGHTS:
            return
        pending.extend(heights)
    if not advance_cfilters(node, conn, pending):
        node.pending_cfilters[conn.id] = (conn, pending)


def get_cfheaders(node: Node, msg: bytes, conn: Connection) -> None:
    """Answer a BIP157 `getcfheaders` with the requested range's filter headers.

    Silent on a request `_filter_range` refuses.
    """
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
    """Answer a BIP157 `getcfcheckpt` with one filter header per checkpoint.

    Silent for an unsupported filter type or an unknown stop hash.
    """
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
    """Clear the in-flight record for a transaction the peer could not answer.

    A block item carries no such bookkeeping to clear -- the comment
    below argues why.
    """
    missing = NotFound.parse(msg)
    # `TxDownloadManagerImpl::ReceivedNotFound`, net_processing.cpp
    # (at bitcoin/bitcoin@58a7869f86): a `notfound` for a transaction this
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
    """Log a peer's `reject` message."""
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
