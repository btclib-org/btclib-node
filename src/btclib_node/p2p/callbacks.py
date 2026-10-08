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
from typing import TYPE_CHECKING, cast

from btclib import var_int
from btclib.amount import valid_sats_amount
from btclib.exceptions import BTClibException, BTClibValueError
from btclib.p2p.address import Addr, ServiceFlags
from btclib.p2p.addrv2 import (
    AddrV2,
    BIP155Network,
    NetworkAddressV2,
    SendAddrV2,
    addr_entry,
    can_addrv1,
    peer_from_addr_entry,
)
from btclib.p2p.block_filters import (
    BlockFilterType,
    CFCheckpt,
    CFHeaders,
    CFilter,
    GetCFCheckpt,
    GetCFHeaders,
    GetCFilters,
)
from btclib.p2p.compact_blocks import (
    BlockTxn,
    CmpctBlock,
    GetBlockTxn,
    PartialBlock,
    SendCmpct,
    reconstruct,
)
from btclib.p2p.data import BlockPayload as BlockMsg
from btclib.p2p.data import TxPayload as TxMsg
from btclib.p2p.handshake import Verack, Version
from btclib.p2p.inventory import (
    GetBlocks,
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
    MAX_ADDRV2_SIZE,
    MAX_BLOCK_TX_INDEX,
    MAX_GETCFHEADERS_SIZE,
    MAX_GETCFILTERS_SIZE,
    MAX_HEADERS_RESULTS,
    MAX_INV_SZ,
    MAX_LOCATOR_SZ,
    PROTOCOL_VERSION,
)
from btclib.p2p.negotiation import FeeFilter, GetAddr, WtxidRelay

from btclib_node.chainstate.block_index import (
    BlockStatus,
    block_time,
    calculate_work,
    check_headers_pow,
)
from btclib_node.chainstate.filter_index import NO_PREVIOUS_FILTER_HEADER
from btclib_node.constants import MIN_BLOCKS_TO_KEEP, NodeStatus, P2pConnStatus
from btclib_node.exceptions import (
    ChainstateInconsistencyError,
    LowWorkHeaderError,
    MisbehavingError,
    MissingPrevoutError,
    PackageRefusedError,
    TxRejectedError,
)
from btclib_node.main import (
    activate_best_chain,
    assert_valid_block,
    check_fork_warning_conditions,
    is_block_failed,
    is_block_mutated,
    is_cached_invalid,
    new_pow_valid_block,
    passes_check_block,
    pre_verify_mempool_acceptance,
    pre_verify_package,
)
from btclib_node.mempool import package_hash
from btclib_node.p2p.address import AddrResponseCache, ip_and_port, peer_address
from btclib_node.p2p.block_availability import (
    in_flight_from,
    remove_block_request,
    update_block_availability,
)
from btclib_node.p2p.chain_sync import (
    disconnect_if_insufficient_work,
    protect_if_caught_up,
)
from btclib_node.p2p.compact_block import (
    MAX_CMPCTBLOCKS_INFLIGHT_PER_BLOCK,
    compact_block,
)
from btclib_node.p2p.eviction import is_routable
from btclib_node.p2p.headers_sync import (
    ChainStart,
    HeadersSyncState,
    State,
    anti_dos_work_threshold,
)
from btclib_node.p2p.messages import FinalAlert
from btclib_node.p2p.permissions import NetPermissionFlags
from btclib_node.p2p.protocol_version import (
    BIP0031_VERSION,
    MIN_PEER_PROTO_VERSION,
    SHORT_IDS_BLOCKS_VERSION,
    WTXID_RELAY_VERSION,
    common_version,
)
from btclib_node.p2p.tx_checks import TxCheck

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from btclib.block import Block, BlockHeader
    from btclib.tx.tx import Tx

    from btclib_node import Node
    from btclib_node.chainstate.block_index import BlockIndex
    from btclib_node.main import MempoolCandidate
    from btclib_node.p2p.connection import Connection

__all__ = [
    "CMPCTBLOCKS_VERSION",
    "MAX_BLOCKTXN_DEPTH",
    "MAX_CMPCTBLOCK_DEPTH",
    "addr",
    "addrv2",
    "advance_cfilters",
    "advance_getdata",
    "already_judged",
    "block",
    "blocktxn",
    "callbacks",
    "cmpctblock",
    "feefilter",
    "get_cfcheckpt",
    "get_cfheaders",
    "get_cfilters",
    "getaddr",
    "getblocks",
    "getblocktxn",
    "getdata",
    "getheaders",
    "handshake_callbacks",
    "has_all_desirable_services",
    "headers",
    "inv",
    "maybe_send_getheaders",
    "not_found",
    "ping",
    "pong",
    "process_orphan",
    "sendaddrv2",
    "sendcmpct",
    "sendheaders",
    "settle_tx",
    "tx",
    "verack",
    "version",
    "wtxidrelay",
]


# The octets one entry takes on the wire, where every entry takes the same
# and Core reads any octets there: an `inv`/`getdata` item's four-octet
# type and 32-octet hash, an `addr` entry's time, services, sixteen-octet
# address and port, and a locator's hash.
_INV_ENTRY_SIZE = 36
_ADDR_ENTRY_SIZE = 30
_HASH_SIZE = 32

# An `addrv2` entry's octets vary: a four-octet time, a CompactSize of
# services, a one-octet network id, the address behind a CompactSize
# length, and a two-octet port. The fewest it takes is nine, and the
# length a network id fixes is BIP155's table, which btclib keeps private.
_ADDRV2_MIN_ENTRY_SIZE = 9
_BIP155_ADDRESS_SIZE: dict[int, int] = {
    BIP155Network.IPV4: 4,
    BIP155Network.IPV6: 16,
    BIP155Network.TORV2: 10,
    BIP155Network.TORV3: 32,
    BIP155Network.I2P: 32,
    BIP155Network.CJDNS: 16,
    BIP155Network.YGGDRASIL: 16,
}
# A CompactSize's marker octet, the width of the number behind it, and
# the smallest number that width may carry, below which the encoding is
# not the canonical one and is refused, as `var_int.parse` refuses it.
_COMPACT_SIZE_WIDTHS = {0xFD: (2, 0xFD), 0xFE: (4, 0x1_0000), 0xFF: (8, 0x1_0000_0000)}


def _compact_size(msg: bytes, pos: int) -> tuple[int, int]:
    """Return the CompactSize at `pos` and the offset after it.

    The offset is -1 where no octet is at `pos` or the encoding is not
    the canonical one. A number cut short by the end of `msg` answers an
    offset past that end, which every caller compares with `len(msg)`.
    """
    if pos >= len(msg):
        return 0, -1
    marker = msg[pos]
    if marker not in _COMPACT_SIZE_WIDTHS:
        return marker, pos + 1
    width, least = _COMPACT_SIZE_WIDTHS[marker]
    end = pos + 1 + width
    value = int.from_bytes(msg[pos + 1 : end], "little")
    return value, end if value >= least else -1


def _skip_addrv2_entry(msg: bytes, pos: int) -> int:
    """Return the offset after the `addrv2` entry at `pos`, or -1.

    -1 where `NetworkAddressV2.parse` would refuse the entry: short, a
    CompactSize not canonical, an address past `MAX_ADDRV2_SIZE`, or one
    whose length is not the one its network fixes. Nothing is built,
    only the length fields read.
    """
    _, pos = _compact_size(msg, pos + 4)
    if pos < 0 or pos >= len(msg):
        return -1
    network = msg[pos]
    size, pos = _compact_size(msg, pos + 1)
    fixed = _BIP155_ADDRESS_SIZE.get(network)
    if pos < 0 or size > MAX_ADDRV2_SIZE or fixed not in (None, size):
        return -1
    pos += size + 2
    return pos if pos <= len(msg) else -1


def _count_past(msg: bytes, bound: int, entry_size: int, offset: int = 0) -> int:
    """Return the count of the vector at `offset`, where it passes `bound`.

    Zero where it does not, or where the rest of `msg` does not hold
    that many entries of `entry_size` octets: Core reads the whole
    vector before comparing its size with the bound
    (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), so such a payload throws first and is only logged, and it is
    left to btclib's own parse here too. btclib's parsers refuse a count
    past the bound as they refuse a truncated payload, which is why the
    count is read ahead of them.
    """
    stream = BytesIO(msg)
    stream.seek(offset)
    count = var_int.parse(stream)
    if count > bound and len(msg) - stream.tell() >= count * entry_size:
        return count
    return 0


def _addrv2_count_past(msg: bytes, bound: int) -> int:
    """Return an `addrv2` count past `bound`, as `_count_past` does.

    The count decides first where it can: within the bound, or past
    what the rest of `msg` could hold at the fewest octets an entry
    takes. Only then are the entries walked, by their length fields,
    each one read as `NetworkAddressV2.parse` would refuse it.
    """
    count, pos = _compact_size(msg, 0)
    if pos < 0 or count <= bound:
        return 0
    if len(msg) - pos < count * _ADDRV2_MIN_ENTRY_SIZE:
        return 0
    for _ in range(count):
        pos = _skip_addrv2_entry(msg, pos)
        if pos < 0:
            return 0
    return count


def _refuse_past_bound(msg_type: str, count: int) -> None:
    """Raise `MisbehavingError` for a vector counting `count` past its bound.

    Core's `ProcessMessage` calls `Misbehaving` for an `addr`, `addrv2`,
    `inv`, `getdata` or `headers` holding more entries than its bound
    allows, with the message this raises. A `count` of zero raises
    nothing, `_count_past` answering zero within the bound.
    """
    if count:
        err_msg = f"{msg_type} message size = {count}"
        raise MisbehavingError(err_msg)


def has_all_desirable_services(node: Node, services: int) -> bool:
    """Core's `HasAllDesirableServiceFlags`, argued in `version` below."""
    desirable = ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_WITNESS
    if (
        services & ServiceFlags.NODE_NETWORK_LIMITED
        and node.status >= NodeStatus.BlockSynced
    ):
        desirable = ServiceFlags.NODE_NETWORK_LIMITED | ServiceFlags.NODE_WITNESS
    return not desirable & ~services


# Core's `HEADERS_RESPONSE_TIME` (`net_processing.cpp`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), in seconds: how long a
# `getheaders` a peer has not answered holds off the next one to it.
_HEADERS_RESPONSE_TIME = 2 * 60


def maybe_send_getheaders(node: Node, conn: Connection, locator: list[bytes]) -> bool:
    """Send `conn` a `getheaders` unless one to it is still in flight.

    Core's `MaybeSendGetHeaders` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), through which every
    `getheaders` this node sends goes. A request is in flight from when it
    is sent until `headers` clears it or `_HEADERS_RESPONSE_TIME` passes.
    Returns whether it was sent.
    """
    now = time.time()
    timestamps = node.download_manager.last_getheaders_timestamps
    if now - timestamps.get(conn.id, 0.0) > _HEADERS_RESPONSE_TIME:
        conn.send(GetHeaders(PROTOCOL_VERSION, locator, b"\x00" * 32))
        timestamps[conn.id] = now
        return True
    return False


# The common version at or below which `version` sends the final
# `alert`: Core's literal 70012 (`src/net_processing.cpp`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), named for nothing else.
_FINAL_ALERT_VERSION = 70012


def _expects_services(conn: Connection) -> bool:
    """Answer Core's `ExpectServicesFromConn` for `conn`'s own kind.

    False for `INBOUND`, `MANUAL` and `FEELER`; true for
    `OUTBOUND_FULL_RELAY`, `BLOCK_RELAY`, `ADDR_FETCH` and
    `PRIVATE_BROADCAST` (`src/net.h:838-848`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Of this node's own
    connections that is `conn.automatic` -- what `_maybe_dial_more_peers`
    dials, a feeler excepted below, and never a `-connect` or `-addnode`
    peer (btclib-org/btclib-node#725) -- or `conn.addr_fetch`, this tree
    having no `PRIVATE_BROADCAST` counterpart.
    """
    return (conn.automatic or conn.addr_fetch) and not conn.feeler


def _refuses(node: Node, conn: Connection, version_msg: Version) -> bool:
    """Answer whether `version` drops the peer, and discourages nobody.

    The refusals of Core's `VERSION` handling, which answers a
    self-connect, an obsolete version and missing services with
    `fDisconnect` alone (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Records whether the peer
    has every service this node wants, once the refusals ahead of that
    check have kept it.
    """
    # `is_self_connect_nonce` replaces a fixed-size ring of recently
    # sent nonces, which a burst of outbound connects could evict a
    # still-outstanding attempt's own nonce from before its `version`
    # came back (btclib-org/btclib-node#448) -- its own docstring is
    # where the search it runs is argued against Core's.
    if node.p2p_manager.is_self_connect_nonce(version_msg.nonce):
        return True

    # Core's floor: a peer older than `MIN_PEER_PROTO_VERSION` is
    # dropped, and every feature newer than that is gated per peer on
    # `common_version` (`p2p/protocol_version.py`)
    if version_msg.version < MIN_PEER_PROTO_VERSION:
        return True
    # `NODE_WITNESS` is in every set `GetDesirableServiceFlags` answers,
    # so Core requires it of every connection `_expects_services` covers
    # -- an automatic outbound peer or an addr-fetch one, whatever this
    # node's own sync state -- and of no other: an inbound peer, a
    # manual one, or a feeler is kept without it. `DownloadManager` asks
    # such a peer for `MSG_BLOCK` rather than `MSG_WITNESS_BLOCK`, and
    # stops its walk over the peer's chain at SegWit's own activation
    # height (btclib-org/btclib-node#1208).
    if _expects_services(conn) and not version_msg.services & ServiceFlags.NODE_WITNESS:
        return True
    # Core disconnects for missing services only where
    # `_expects_services` holds -- an automatic outbound connection or an
    # addr-fetch one, never a feeler, and not a `-connect` or `-addnode`
    # peer (btclib-org/btclib-node#725, btclib-org/btclib-node#1284).
    #
    # `has_all_desirable_services`' own `desirable` (above) is
    # `GetDesirableServiceFlags`'s shape
    # (`net_processing.cpp:1861-1869`): `NODE_NETWORK | NODE_WITNESS`
    # ordinarily, or `NODE_NETWORK_LIMITED | NODE_WITNESS` -- satisfied
    # by a `NODE_NETWORK_LIMITED`-only peer -- once this node's own
    # `ApproximateBestBlockDepth()` is under
    # `NODE_NETWORK_LIMITED_ALLOW_CONN_BLOCKS` (144). This tree computes
    # no block-time depth estimate; `node.status >= NodeStatus.BlockSynced`
    # stands in for "close to the tip" instead, kept as the gate on the
    # whole check rather than only on the substitution there, matching
    # this rule's own pre-#725 scope of tolerating a missing service
    # until this node actually wants blocks -- a one-way latch
    # `main.finish_sync` sets once `_ready_fork` finds no candidate left
    # to beat the active chain (`main.settle_at_no_candidate`'s own
    # docstring), stricter than Core's own 144-block allowance (true
    # only once this node is fully caught up, not merely close) but the
    # only "caught up" signal this tree already carries without
    # computing a new one. The comparison itself is
    # `HasAllDesirableServiceFlags`'s own shape (`net_processing.cpp:3850`,
    # `!(desirable & ~services)`): `NODE_WITNESS` is already required of
    # every connection the check below covers, so it is never the bit
    # that trips this once reached, but it is kept in `desirable` for
    # the same shape Core's own check has rather than a narrower one
    # this tree invented.
    #
    # The same answer is what Core records as `m_has_all_wanted_services`
    # for every connection, inbound included, and reads when choosing an
    # inbound peer to evict.
    conn.has_all_wanted_services = has_all_desirable_services(
        node, version_msg.services
    )
    # `ExpectServicesFromConn` also holds for `ADDR_FETCH` (`src/net.h`,
    # same sha), covered by `_expects_services` along with every
    # automatic outbound connection: an addr-fetch peer short of
    # `NODE_NETWORK` is dropped here rather than kept, on top of being
    # refused above for missing `NODE_WITNESS`, and dropped on its own
    # short life by `_ADDR_FETCH_TIMEOUT` regardless
    # (btclib-org/btclib-node#1284, btclib-org/btclib-node#1138).
    return (
        _expects_services(conn)
        and node.status >= NodeStatus.BlockSynced
        and not conn.has_all_wanted_services
    )


def _see_local(node: Node, conn: Connection, version_msg: Version) -> None:
    """Raise the score of the address an inbound peer says it reached us at.

    Core's `SeenLocal` of a routable `addrMe`, ahead of `PushNodeVersion`
    (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag).
    """
    if conn.inbound:
        seen = version_msg.addr_recv
        seen_ip = seen.ip.ipv4_mapped or seen.ip
        if is_routable(peer_address(str(seen_ip), seen.port)):
            node.p2p_manager.seen_local(seen_ip)


def version(node: Node, msg: bytes, conn: Connection) -> None:
    """Handle a peer's `version`: refuse an incompatible peer, else continue.

    A second `version` ahead of this connection's own `verack` is
    ignored outright -- Core's own guard, `pfrom.nVersion != 0`
    (`net_processing.cpp:3823`, at bitcoin/bitcoin@5f45583e43), which
    logs and returns before doing anything else. `conn.status` stays
    `Open` until `verack` promotes it, so a repeat sent before that
    point reaches this callback, and unguarded would resend
    `WtxidRelay`, `SendAddrV2` and `Verack` in answer.
    btclib-org/btclib-node#482

    Continuing means answering `verack`, with `wtxidrelay` and
    `sendaddrv2` ahead of it where the common version reaches
    `WTXID_RELAY_VERSION` and, to an inbound peer, this node's own
    `version` ahead of all three; setting up address relay with a peer
    this node dialled, and asking it for addresses; recording a peer
    this node dialled as answered, block-relay-only peers and feelers
    included, right where Core calls `AddrMan::Good` -- not waiting for
    its own `verack`, which may never come (#1169); and recording whether
    the peer asked to have transactions relayed. A feeler is then dropped
    once those are written, as Core's `VERSION` handler ends one
    (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag).
    """
    if conn.version_message is not None:
        return
    version_msg = Version.parse(msg)

    conn.version_message = version_msg
    # Core's `SetServices` of an outbound peer's own services, ahead of
    # every refusal below (`src/net_processing.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a table row gossip
    # mislabelled is corrected here, the peer dropped or not
    if not conn.inbound:
        node.p2p_manager.peer_db.set_services(conn.address, version_msg.services)
    if _refuses(node, conn, version_msg):
        conn.stop()
        return

    _see_local(node, conn, version_msg)

    # Core's `PushNodeVersion` for an inbound peer: its `version` is
    # answered only once every check above has kept it
    if conn.inbound:
        conn.send(conn.own_version())

    # Core sends `sendaddrv2` from 70016 up too, "as a courtesy" to
    # software that rejects a message it does not know.
    if common_version(conn) >= WTXID_RELAY_VERSION:
        conn.send(WtxidRelay())
        conn.send(SendAddrV2())
    conn.send(Verack())

    # Core's `VERSION` handler, right after `VERACK`, calls
    # `SetupAddressRelay` for a peer this node dialled, and sends it a
    # `getaddr` with room for the answer past
    # `_MAX_ADDR_PROCESSING_TOKEN_BUCKET` (`net_processing.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag). An inbound peer keeps
    # the one token it started with, and waits for its own first `addr`,
    # `addrv2` or `getaddr`. `SetupAddressRelay` answers false, and so
    # sends no `getaddr`, for a block-relay-only peer.
    if not conn.inbound and not conn.block_relay:
        conn.addr_relay_enabled = True
        conn.send(GetAddr())
        conn.addr_token_bucket += MAX_ADDR_TO_SEND

    # Right after that `getaddr`, Core calls `m_addrman.Good(pfrom.addr)`
    # for a peer this node dialled (`net_processing.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), under
    # `!pfrom.IsInboundConn()` alone -- not waiting for this connection's
    # own `verack`, which a peer that answers `version` and then stalls
    # never sends, and which `manager.py`'s own 60-second drop of a
    # pending connection (btclib-org/btclib-node#1169) makes a real,
    # reachable case here. `conn.address` is what this node dialled, and
    # the socket connecting there already answered.
    # Every peer this node dialled is recorded, block-relay-only ones
    # and feelers included: Core's comment there says not moving the
    # address to the tried table is also harmful, new-table entries being
    # evictable on collision. `Good_` leaves `nTime` alone, so recording
    # one advertises nothing; `AddrMan::Connected`, which does, is never
    # called for a block-relay-only peer or a feeler (`P2pManager._finalize`).
    # An inbound peer is not recorded here or anywhere: its connection
    # proves only that it can reach this node, not that this node can
    # reach it back. btclib-org/btclib-node#1229
    if not conn.inbound:
        address = replace(conn.address, services=version_msg.services)
        conn.address = address
        node.p2p_manager.peer_db.add_active_address(address)

    # relay_tx, which is the attribute Connection defines: the name this
    # wrote before was one letter different, so what the peer asked for
    # landed on an attribute nothing reads and the connection's own flag
    # stayed true for its whole life. is_relay_requested and not relay
    # because an absent flag means true, which is BIP37's default and
    # Core's. A block-relay-only connection or a feeler relays no
    # transaction whatever the peer asked for: Core's `VERSION` handler
    # builds no `TxRelay` for either (`net_processing.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), so nothing is announced
    # to it and a `getdata` of its for a transaction goes unanswered.
    conn.relay_tx = (
        version_msg.is_relay_requested and not conn.block_relay and not conn.feeler
    )
    # where Core's `ProcessMessage` sets `m_time_offset`, once every
    # refusal above is behind it
    conn.stats.time_offset = version_msg.timestamp - int(time.time())
    # and where it sends the final `alert` to a peer "old enough to have
    # the old alert system". btclib-org/btclib-node#1205
    if common_version(conn) <= _FINAL_ALERT_VERSION:
        conn.send(FinalAlert())
    _end_if_feeler(node, conn)


def _end_if_feeler(node: Node, conn: Connection) -> None:
    """Drop a feeler once its `version` is handled, as Core's handler ends one.

    Any other connection is left as it is. `SetupAddressRelay` holds
    for a feeler, so `version` has asked it for addresses already, and
    has recorded its address as answered. Split out of `version` for
    ruff's complexity ceiling.
    """
    if not conn.feeler:
        return
    node.logger.log_debug("net", "feeler connection completed, peer=%s", conn.id)
    conn.stop_when_sent()


def verack(node: Node, msg: bytes, conn: Connection) -> None:
    """Complete a peer's handshake: promote it and send the follow-up messages.

    Ignores a `verack` ahead of `version`, as Core's `ProcessMessage`
    ignores any message there (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). A peer that sent no
    `wtxidrelay` ahead of it completes its handshake all the same, and
    has transactions relayed by txid, as in Core.
    """
    if not conn.version_message:
        return
    conn.status = P2pConnStatus.Connected
    # Ahead of `promote_connection`: `P2pManager._keep_alive`, on another
    # thread, pings a connection whose `ping_start` is old, and would
    # ping this one as it arrives. btclib-org/btclib-node#1768
    conn.ping_start = time.time()
    # out of P2pManager.pending_connections and into connections, the
    # dict every send iterates: btclib-org/btclib-node#131
    node.p2p_manager.promote_connection(conn.id)

    # `sendheaders` is `DownloadManager`'s to send, once this peer's best
    # known block has the minimum chain work, as Core's
    # `MaybeSendSendHeaders` does
    if common_version(conn) >= SHORT_IDS_BLOCKS_VERSION:
        conn.send(SendCmpct(announce=False, version=CMPCTBLOCKS_VERSION))
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
    # No `getaddr` here either: `version` sends it, as Core's does.
    # No `getheaders` here: whether this peer is asked for headers is
    # `DownloadManager.sync_headers`'s decision, made on the next pass
    # of `Node`'s own loop, as Core makes it in `SendMessages` rather
    # than in its own `verack` handler.
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
    """Record that the peer relays transactions by wtxid (BIP339).

    Ignored from a peer whose common version is below
    `WTXID_RELAY_VERSION`, as Core ignores it.
    """
    if common_version(conn) >= WTXID_RELAY_VERSION:
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


# `sendcmpct`'s payload: the announce octet and the eight of the version
_SENDCMPCT_SIZE = 9


def sendcmpct(node: Node, msg: bytes, conn: Connection) -> None:
    """Record whether the peer wants new blocks announced as `cmpctblock`.

    Core's `SENDCMPCT` handler (`src/net_processing.cpp`) on master,
    at bitcoin/bitcoin@ba8fdb9717: the announce octet is read as a
    `uint8_t`, not a `bool`, so a value above one is refused as
    "invalid sendcmpct announce field" before a version other than
    `CMPCTBLOCKS_VERSION` is ignored; otherwise the announce octet is
    the peer's choice of this node as a BIP152 high-bandwidth peer,
    which a later `sendcmpct` can take back. Either way the peer is one
    that provides compact blocks, which `cmpctblock` below and
    `DownloadManager.headers_direct_fetch` read.

    v31.1, at bitcoin/bitcoin@9be056a8a7, the release
    `.github/workflows/integration-bitcoind.yml` pins and the
    integration tests run against, still reads the octet as a plain
    `bool` and never refuses one above one: the `uint8_t` read and the
    `Misbehaving` call are Core commit 2d0dce0af5, on master and in
    `v32.0rc1`, not yet in a release.
    """
    # read as Core's `vRecv >> sendcmpct_hb >> sendcmpct_version` reads
    # it, bytes past the ninth left unread; btclib's `SendCmpct.parse`
    # is not used here because BTClibValueError leaves the peer
    # undiscouraged (`p2p.main._drop`), where Core's own refusal is a
    # `Misbehaving` call
    if len(msg) < _SENDCMPCT_SIZE:
        err_msg = f"sendcmpct payload of {len(msg)} bytes"
        raise BTClibValueError(err_msg)
    announce = msg[0]
    if announce > 1:
        err_msg = f"invalid sendcmpct announce field: {announce}"
        raise MisbehavingError(err_msg)
    if int.from_bytes(msg[1:_SENDCMPCT_SIZE], "little") != CMPCTBLOCKS_VERSION:
        return
    conn.provides_cmpctblocks = True
    conn.requested_hb_cmpctblocks = announce != 0


def ping(node: Node, msg: bytes, conn: Connection) -> None:
    """Answer a `ping` with a `pong` carrying the same nonce.

    Unanswered at a common version of `BIP0031_VERSION` or below, whose
    `ping` carries no nonce and which Core does not answer either.
    """
    if common_version(conn) <= BIP0031_VERSION:
        return
    nonce = Ping.parse(msg).nonce
    conn.send(Pong(nonce))


# the octets of a `pong`'s nonce, Core's `sizeof(nonce)`
_PONG_NONCE_SIZE = 8


def pong(node: Node, msg: bytes, conn: Connection) -> None:
    """Finish the outstanding `ping` a `pong` answers, recording the round trip.

    Core's `PONG` (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag) punishes no peer for a `pong`. A nonce matching no
    outstanding `ping` leaves it outstanding, mismatches being "normal
    when pings are overlapping"; a zero nonce, or a payload too short to
    hold one, finishes it with no round trip recorded; and a `pong` with
    no `ping` outstanding is ignored.
    """
    # Core reads the nonce off the payload's first octets and ignores
    # the rest. A shorter payload finishes the ping as a zero nonce does.
    head = msg[:_PONG_NONCE_SIZE]
    nonce = Pong.parse(head).nonce if len(head) == _PONG_NONCE_SIZE else 0
    # The read that decides which of ping_sent/ping_nonce apply and the
    # clear that answers it are one step under conn._ping_lock, against
    # Connection.send_ping's own pair of writes on the other thread:
    # unlocked, a send_ping slipped in between this method's own two
    # statements would have ping_nonce cleared to 0 out from under the
    # ping it had just sent. btclib-org/btclib-node#357
    with conn._ping_lock:  # noqa: SLF001 -- the comment above is why
        ping_sent = conn.ping_sent
        if not ping_sent or nonce not in (0, conn.ping_nonce):
            return
        conn.ping_sent = 0
        conn.ping_nonce = 0
        if nonce:
            # Core's `CNode::PongReceived` (`src/net.h`,
            # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) records the round
            # trip and keeps the lowest for eviction, and `ProcessMessage`
            # calls it only for a round trip that is not negative: a clock
            # stepped back between ping and pong finishes the ping and
            # records nothing. Both ends are on the wall clock, and the
            # round trip ends when the pong was read off the socket, not
            # when `Node`'s loop reached it: `ping_end = time_received`
            # there, `Connection.time_received` here.
            ping_time = conn.time_received - ping_sent
            if ping_time >= 0:
                conn.latency = ping_time
                conn.min_ping_time = min(conn.min_ping_time, ping_time)


# Core's own MAX_PCT_ADDR_TO_SEND (net_processing.cpp, 58a7869f86):
# answering with the whole table on demand is what an observer mapping
# the network wants, so a getaddr answer is a sample of it instead.
# btclib-org/btclib-node#71
_MAX_PCT_ADDR_TO_SEND = 23


# How long a drawn sample is served again rather than redrawn: shared by
# every connection answered in between, not per connection -- the
# once-per-connection flag already stops one peer asking twice, this is
# what stops two peers connecting close together from being handed two
# different draws to compare. Core's own CachedAddrResponse expiration
# (src/net.cpp, 58a7869f86): held for `_ADDR_SAMPLE_LIFETIME` plus a fresh
# random point across `_ADDR_SAMPLE_JITTER` drawn again every time the
# cache is recomputed, rather than a fixed lifetime alone. A refresh
# landing at a predictable wall-clock offset would itself be a signal to
# whatever is scraping this answer over time, the same attacker Core's own
# comment there reasons about for the duration alone -- the cache exists
# to be unpredictable, not merely stable. btclib-org/btclib-node#71
_ADDR_SAMPLE_LIFETIME = 3600 * 21
_ADDR_SAMPLE_JITTER = 3600 * 6


def _draw_sample(node: Node) -> list[NetworkAddressV2]:
    """Draw what a `getaddr` answers, without a discouraged or banned host.

    Core's `GetAddressesUnsafe` (`src/net.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) filters what addrman drew
    the same way.
    """
    manager = node.p2p_manager
    return [
        address
        for address in manager.peer_db.get_addr(MAX_ADDR_TO_SEND, _MAX_PCT_ADDR_TO_SEND)
        if not manager.is_discouraged(address)
        and not manager.ban_man.is_peer_banned(address)
    ]


def _cached_sample(node: Node, conn: Connection) -> list[NetworkAddressV2]:
    """Return the sample cached under `conn`'s key, redrawn once expired."""
    peer_db = node.p2p_manager.peer_db
    now = time.time()
    # Every inbound connection this callback ever reaches carries a key
    # -- `P2pManager.server`/`create_connection` set it on acceptance,
    # the only path into an inbound `Connection` -- so this is never
    # `None` here; the cast is what tells mypy the same thing, `conn`
    # typed `None` for an outbound connection's sake
    # (`addr_cache_key`'s own docstring).
    key = cast("tuple[int, str, int]", conn.addr_cache_key)
    cache = peer_db.addr_response_caches.setdefault(key, AddrResponseCache())
    if now >= cache.expiration:
        cache.sample = _draw_sample(node)
        # The sample can go on naming an endpoint the table has
        # since aged out or dropped, for as long as this cache is still
        # good: intended, not overlooked -- the cache is not what a
        # `getaddr` answer's freshness rests on, an `addr` entry already
        # carries its own timestamp for whoever receives it to judge
        # staleness by, and shortening this lifetime to track the table
        # more closely would give back the privacy this cache exists for
        # to buy an accuracy guarantee gossip never promised in the
        # first place.
        jitter = secrets.SystemRandom().uniform(0, _ADDR_SAMPLE_JITTER)
        cache.expiration = now + _ADDR_SAMPLE_LIFETIME + jitter
    return cache.sample


def getaddr(node: Node, msg: bytes, conn: Connection) -> None:
    """Answer a peer's `getaddr` with a sample of every known address, once.

    A peer holding `ADDR` is answered from a draw of its own. For any
    other the sample is a cache, shared and redrawn only once its own
    lifetime and jitter expire -- `_cached_sample` argues why -- and
    kept one per `conn.addr_cache_key` rather than one for every
    connection. Core's `CConnman::GetAddresses` (`src/net.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) keys
    `m_addr_response_caches` by `requestor.m_network_key`, itself keyed
    by the connection's network (onion for an inbound onion listener,
    `Connection.inbound_onion`) and the local bind address and port the
    peer reached it on (`CreateNodeFromAcceptedSocket`, same file and
    sha): "Addr responses
    stored in different caches per (network, local socket) prevent
    cross-network node identification. If a node for example is
    multi-homed under Tor and IPv6, a single cache (or no cache at all)
    would let an attacker to easily detect that it is the same node by
    comparing responses." (`m_addr_response_caches`'s own comment,
    `src/net.h`, same sha.) `conn.addr_cache_key`'s own docstring
    (`connection.py`) is where the key is built; this node's own plain
    tuple stands in for Core's SipHash-keyed `uint64_t`, which buys
    unpredictability against a peer that could read the key off the
    wire -- this key never leaves the process, so nothing here needs
    that property, only that two different (network, local socket)
    triples land on two different dict entries.
    """
    # Core's `GETADDR` handler (`src/net_processing.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) ignores one from a
    # connection it opened itself: answering it would let a peer plant
    # addresses and read them back from a node that only dials out.
    if not conn.inbound:
        return
    conn.addr_relay_enabled = True
    # Once per connection, matching the flag's own docstring
    # (connection.py): a peer asking in a loop is served the table once
    # rather than once per ask. btclib-org/btclib-node#71
    if conn.answered_getaddr:
        return
    conn.answered_getaddr = True

    if NetPermissionFlags.ADDR in conn.permissions:
        # Core's `GetAddressesUnsafe`: a peer holding `ADDR` is answered
        # from a draw of its own, and no cache of it is kept
        sample = _draw_sample(node)
    else:
        sample = _cached_sample(node, conn)
    # either message class, and not whichever the first branch names:
    # Addr and AddrV2 are siblings under Payload rather than one a
    # subclass of the other, so each is built from its own list rather
    # than through a shared name of a type the other could not accept.
    # `PeerDB.get_addr` already keeps this under MAX_ADDR_TO_SEND, the
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
    """Merge the addr-version-1 entries a peer gossiped into the table.

    Ignored from a block-relay-only peer once parsed, as Core's `ADDR`
    handler (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag) ignores it, `SetupAddressRelay` refusing the peer ahead
    of the `MAX_ADDR_TO_SEND` check. A payload the parse refuses is
    logged by `main.handle_p2p` and costs the peer nothing, as in Core,
    whose parse reads a count past that bound where btclib's refuses it.
    """
    if conn.block_relay:
        Addr.parse(BytesIO(msg))
        return
    # Addr.parse(msg) would refuse an octet past the last address
    # (btclib's own assert_no_trailing, a malleability guard that holds
    # across the library) by raising out of this callback, which
    # main.handle_p2p logs, keeping the peer: gossip this node could
    # read in full would be thrown away for one octet after it. Core does
    # not: ProcessMessage reads AddrMan-worth of entries out of vRecv and
    # never checks for anything left. Wrapping the payload in a stream is
    # btclib's own answer for exactly this -- assert_no_trailing's
    # docstring calls a stream "the caller's", the same shape a
    # transaction inside a block is read through, with nothing after it
    # checked -- so this reads every address BIP155 defines and silently
    # drops whatever else the peer appended, matching Core's leniency
    # without a second copy of Addr's codec. btclib-org/btclib-node#149
    _refuse_past_bound("addr", _count_past(msg, MAX_ADDR_TO_SEND, _ADDR_ENTRY_SIZE))
    entries = Addr.parse(BytesIO(msg)).addresses
    conn.addr_relay_enabled = True
    # BIP155's record is what the table holds, an addr version 1 entry
    # having no room for the networks a peer may yet gossip
    _store_gossip(node, conn, (peer_from_addr_entry(entry) for entry in entries))


def addrv2(node: Node, msg: bytes, conn: Connection) -> None:
    """Merge the BIP155 entries a peer gossiped into the address table.

    Ignored from a block-relay-only peer once parsed, as `addr` above
    ignores it.
    """
    if conn.block_relay:
        AddrV2.parse(BytesIO(msg))
        return
    # the same leniency as addr above, and the same reason: BIP155
    # entries fully read, anything past them left unchecked rather than
    # costing this node the gossip. btclib-org/btclib-node#149
    _refuse_past_bound("addrv2", _addrv2_count_past(msg, MAX_ADDR_TO_SEND))
    addresses = AddrV2.parse(BytesIO(msg)).addresses
    conn.addr_relay_enabled = True
    _store_gossip(node, conn, addresses)


# Core's `MAX_ADDR_RATE_PER_SECOND` and `MAX_ADDR_PROCESSING_TOKEN_BUCKET`
# (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
# tag): the rate a peer's address tokens refill at, and the ceiling that
# refill stops at, which the `MAX_ADDR_TO_SEND` added by `version`'s own
# `getaddr` may exceed.
_MAX_ADDR_RATE_PER_SECOND = 0.1
_MAX_ADDR_PROCESSING_TOKEN_BUCKET = MAX_ADDR_TO_SEND


# `ProcessMessage`'s plausibility bounds for a gossiped time
# (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
# tag): at or before this, or more than ten minutes ahead of the clock,
# it is replaced by five days ago
_GOSSIP_MIN_TIME = 100_000_000
_GOSSIP_REDATE = 5 * 24 * 3600


def _store_gossip(
    node: Node, conn: Connection, addresses: Iterable[NetworkAddressV2]
) -> None:
    """Merge gossiped `addresses` into the table, in Core's order of filters.

    Core's `ADDR`/`ADDRV2` loop (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) tops up the peer's
    `m_addr_token_bucket`, shuffles the message, and then takes each
    address in turn: one without a token is dropped and counted in
    `m_addr_rate_limited`, unless the peer holds `ADDR`; one with a token
    spends it, and is then skipped if its services carry neither
    `NODE_NETWORK` nor `NODE_NETWORK_LIMITED`, skipped if `IsDiscouraged`
    answers for it, and otherwise counted in `m_addr_processed` before
    `AddrMan` refuses any of it. An addr-fetch connection is dropped once
    this answers with more than one address, "to avoid disconnecting on
    self-announcements" (same loop, same sha) -- of `addresses` as
    received, ahead of every filter above, matching Core's own
    `vAddr.size()` (btclib-org/btclib-node#1284). A time Core finds
    implausible is replaced first (btclib-org/btclib-node#1605).
    """
    now = time.time()
    if conn.addr_token_bucket < _MAX_ADDR_PROCESSING_TOKEN_BUCKET:
        elapsed = max(now - conn.addr_token_timestamp, 0)
        conn.addr_token_bucket = min(
            conn.addr_token_bucket + elapsed * _MAX_ADDR_RATE_PER_SECOND,
            _MAX_ADDR_PROCESSING_TOKEN_BUCKET,
        )
    conn.addr_token_timestamp = now
    received = list(addresses)
    secrets.SystemRandom().shuffle(received)
    manager = node.p2p_manager
    kept: list[NetworkAddressV2] = []
    rate_limited = 0
    for address in received:
        # a peer holding `ADDR` is never rate limited, and a bucket
        # without a token is not spent into debt for it
        if conn.addr_token_bucket >= 1:
            conn.addr_token_bucket -= 1
        elif NetPermissionFlags.ADDR not in conn.permissions:
            rate_limited += 1
            continue
        # Core's `!MayHaveUsefulAddressDB && !HasAllDesirableServiceFlags`:
        # every set `GetDesirableServiceFlags` answers holds one of these
        # two flags, so the second half never keeps what the first drops
        if not address.services & (
            ServiceFlags.NODE_NETWORK | ServiceFlags.NODE_NETWORK_LIMITED
        ):
            continue
        # Core re-dates a time at or before 3 March 1973, or ahead of the
        # clock, ahead of the discouraged and banned check
        dated = address
        if address.timestamp <= _GOSSIP_MIN_TIME or address.timestamp > now + 600:
            dated = replace(address, timestamp=int(now - _GOSSIP_REDATE))
        if manager.is_discouraged(dated) or manager.ban_man.is_peer_banned(dated):
            continue
        kept.append(dated)
    conn.stats.addr_processed += len(kept)
    conn.stats.addr_rate_limited += rate_limited
    # `source=conn.address`: Core's own `m_addrman.Add(vAddrOk,
    # pfrom.addr, /*time_penalty=*/2h)` (same loop, same sha) passes the
    # connection's own address as `AddSingle`'s `source`, which exempts
    # a self-announcement -- an address equal to the peer's own host,
    # port aside -- from the batch's flat two-hour penalty,
    # `add_addresses`' own default; `add_addresses`'s own docstring is
    # where the port is argued out of the comparison
    # (btclib-org/btclib-node#1380, review round 2).
    manager.peer_db.add_addresses(kept, source=conn.address)
    if conn.addr_fetch and len(received) > 1:
        node.logger.log_debug("net", "addrfetch connection completed, peer=%s", conn.id)
        conn.stop()


def feefilter(node: Node, msg: bytes, conn: Connection) -> None:
    """Record the peer's own BIP133 minimum feerate, ignoring an invalid one."""
    # BIP133: a peer asking not to be told about a transaction paying
    # less. Stored on the connection, the same shape relay_tx above
    # already is; read by DownloadManager.tx_download, through
    # Mempool.meets_fee_rate, against the fee
    # main.verify_mempool_acceptance now hands back and Mempool keeps
    # per transaction. btclib-org/btclib-node#260
    #
    # Core assigns a received rate only within MoneyRange -- 0 to
    # MAX_MONEY inclusive (net_processing.cpp's NetMsgType::FEEFILTER,
    # consensus/amount.h's MoneyRange, at bitcoin/bitcoin@9be056a8a7) --
    # and leaves the filter it already holds in place for a rate outside
    # it. valid_sats_amount is that same range with its upper bound
    # un-exported by name (btclib.amount's own _MAX_SATOSHI), so it is
    # what stands in for MoneyRange here.
    try:
        conn.feefilter = valid_sats_amount(FeeFilter.parse(msg).feerate)
    except BTClibValueError:
        return


def tx(node: Node, msg: bytes, conn: Connection) -> None:
    """Check an unsolicited transaction, and queue its scripts to be checked.

    A no-op in initial block download, if the mempool already holds this
    wtxid, has recently refused it or the orphanage keeps it, or if the
    transaction fails a check other than its scripts. One whose inputs are
    not found is kept as an orphan, and one refused for a fee floor is tried
    with a child the peer sent, if the peer sent one (`_start_package`).
    Otherwise its scripts are queued in `node.tx_checks`
    (`p2p/tx_checks.py`), and `settle_tx` decides it once they are checked.

    A block-relay-only peer sending one is disconnected, and not
    discouraged, as Core's `TX` handler does first of all where
    `RejectIncomingTxs` holds (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    if conn.block_relay:
        node.logger.log_debug(
            "net", "transaction sent in violation of protocol, peer=%s", conn.id
        )
        conn.stop()
        return
    # Core's own early return in IBD, before it even parses the payload:
    # "we don't have enough information to validate it yet"
    # (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    # tag) -- the utxo set is still catching up, so a prevout this
    # rejects for lacking may only be missing because sync has not
    # reached it. An unsolicited transaction this early is not a
    # protocol violation there either, so this drops it rather than the
    # peer. btclib-org/btclib-node#129
    if node.is_initial_block_download:
        return
    tx = TxMsg.parse(msg).tx
    # Core's `AddKnownTx`: the sender has what it sent, under the hash it
    # announces by.
    conn.known_tx_inventory.add(tx.hash if conn.wtxidrelay_received else tx.id)
    # Core's `ReceivedTx` completes the sender's announcement first of all,
    # whatever becomes of the transaction, a script check included.
    node.download_manager.received_tx_response(conn.id, tx.id, tx.hash)
    if already_judged(node, tx, conn) or node.download_manager.orphanage.have_tx(
        tx.hash
    ):
        return
    if node.mempool.was_recently_rejected_reconsiderable(tx.hash):
        # Core's `ReceivedTx`: not submitted by itself again, but a child
        # the peer sent may pay for it
        package = node.download_manager.find_1p1c_package(tx, conn.id)
        if package is not None:
            _start_package(node, conn, *package)
        return
    candidate = _pre_verify(node, tx, conn)
    if candidate is not None:
        node.tx_checks.queue(TxCheck(conn, tx, candidate.prev_outputs))


def already_judged(node: Node, tx: Tx, conn: Connection) -> bool:
    """Answer whether the mempool holds `tx` or has recently refused it.

    Either way it is not verified again. `Mempool` is reached from
    `Node`'s thread alone (its own module docstring), as this is.
    """
    if node.mempool.contains_tx(tx) or node.mempool.was_recently_rejected(tx.hash):
        # a `FORCE_RELAY` peer's transaction is announced to the others
        # all the same, where the mempool holds it, as Core's
        # `InitiateTxBroadcastToAll` does. Core skips a peer whose
        # `m_tx_inventory_known_filter` holds the hash
        # (`net_processing.cpp:2262`, same sha), as `known_tx_inventory`
        # does here.
        if NetPermissionFlags.FORCE_RELAY in conn.permissions:
            node.download_manager.received_txs.append((conn.id, tx.hash))
        return True
    return False


def _pre_verify(
    node: Node, tx: Tx, conn: Connection, *, first_time: bool = True
) -> MempoolCandidate | None:
    """Run every mempool check but the scripts; take in a refusal of `tx`.

    A refusal goes to `_rejected`. `first_time` is whether `tx` is new to
    this node and not taken from the orphanage or a package.
    """
    try:
        return pre_verify_mempool_acceptance(node, tx)
    except (MissingPrevoutError, BTClibValueError) as refusal:
        # A `TxRejectedError`, and so a `BTClibValueError`, is every other
        # refusal `pre_verify_mempool_acceptance` can make, and a script
        # refusal `settle_tx` reads -- a relay-policy-only one
        # (`NonStandardTxError`, or a fee below
        # either floor) exactly as much as a genuine consensus one
        # (non-final, a coinbase spent too soon, a bad sequence lock, or
        # a script failure `_consensus_accepts` also refuses). Core
        # punishes neither: "Tx failures never trigger
        # disconnections/bans ... either due to non-consensus relay
        # policies ... or due to new consensus rules introduced in soft
        # forks" (`src/validation.cpp:2112-2117`,
        # at bitcoin/bitcoin@4519933391), and
        # `PeerManagerImpl::ProcessInvalidTx` (`src/net_processing.cpp`, same
        # commit) calls nothing punitive for a transaction failure -- there is
        # no `MaybePunishNodeForTx`, where `MaybePunishNodeForBlock` exists and
        # is called. None of these is a `MisbehavingError`, so
        # `p2p.main.handle_p2p` would not discourage the peer either; caught
        # here for the record in `_rejected`. btclib-org/btclib-node#843
        _rejected(node, tx, refusal, conn, first_time=first_time)
        return None


def _rejected(
    node: Node, tx: Tx, error: Exception, conn: Connection, *, first_time: bool
) -> None:
    """Record why `tx` was refused, and try the package it may belong to.

    Core's `ProcessInvalidTx`, the package it answers processed as
    `ProcessPackageResult` does: `DownloadManager.mempool_rejected_tx` says
    what each kind of refusal comes to. Where the refusal can be undone with
    a child the peer sent, that package is started now.
    btclib-org/btclib-node#845
    """
    package = node.download_manager.mempool_rejected_tx(
        tx, error, conn.id, first_time=first_time
    )
    if package is not None:
        _start_package(node, conn, *package)


def _accepted(node: Node, tx: Tx, conn: Connection) -> None:
    """Take in that `tx` is in the mempool, and have it announced.

    Core's `ProcessValidTx`: the orphans that spend it are to be
    reconsidered, and every other peer is told of it.
    """
    node.download_manager.mempool_accepted_tx(tx)
    node.download_manager.received_txs.append((conn.id, tx.hash))


def _start_package(node: Node, conn: Connection, parent: Tx, child: Tx) -> None:
    """Verify a parent refused for a fee floor with a child, and queue them.

    Core's `ProcessNewPackage` of what `Find1P1CPackage` found, up to the
    scripts: `settle_tx` adds the package once they are checked. A parent
    that passes by itself is queued alone, its child to be taken up once it
    is held. `conn` has no check queued, as it sent what is being handled.
    """
    try:
        candidate = pre_verify_package(node, parent, child)
    except PackageRefusedError as refused:
        _package_refused(node, conn, parent, child, refused.errors)
        return
    if candidate.child is None:
        node.tx_checks.queue(
            TxCheck(conn, parent, candidate.parent.prev_outputs, first_time=False)
        )
        return
    parent_check = (parent, candidate.parent.prev_outputs)
    node.tx_checks.queue(
        TxCheck(
            conn,
            child,
            candidate.child.prev_outputs,
            parent=parent_check,
            first_time=False,
        )
    )


def _package_refused(
    node: Node,
    conn: Connection,
    parent: Tx,
    child: Tx,
    errors: dict[bytes, Exception],
) -> None:
    """Record a package refused, and what each of its transactions answers.

    Core's `ProcessPackageResult` for an invalid package: its hash is not
    tried again, and each transaction with a result is taken in, the child
    first, so that the parent is no longer an orphan's missing input
    (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    """
    node.mempool.mark_rejected_reconsiderable(package_hash([parent.hash, child.hash]))
    for member in (child, parent):
        error = errors.get(member.hash)
        if error is not None:
            _rejected(node, member, error, conn, first_time=False)


def process_orphan(node: Node, conn: Connection) -> bool:
    """Take up an orphan `conn` is to reconsider, as far as its scripts.

    Core's `ProcessOrphanTx` (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the oldest is verified, and
    one still missing an input stays an orphan and the next is tried.
    Answers whether one was refused or queued for its scripts, as Core
    answers that it processed one.
    """
    orphanage = node.download_manager.orphanage
    while (orphan := orphanage.get_tx_to_reconsider(conn.id)) is not None:
        try:
            candidate = pre_verify_mempool_acceptance(node, orphan)
        except MissingPrevoutError:
            continue
        except BTClibValueError as refusal:
            _rejected(node, orphan, refusal, conn, first_time=False)
            return True
        node.tx_checks.queue(
            TxCheck(conn, orphan, candidate.prev_outputs, first_time=False)
        )
        return True
    return False


def settle_tx(node: Node, check: TxCheck, refusal: Exception | None) -> None:
    """Keep and announce a transaction whose scripts pass, or record why not.

    On `Node`'s thread, once its scripts are checked: `refusal` is what
    the check raised, if anything. The chain and the mempool
    may have moved while the scripts ran, so every other check runs
    again first, against them as they are now. The scripts' verdict
    still holds if they pass: a prevout is fixed by its outpoint, and
    `interpreter.STANDARD_FLAGS` reads no height.
    """
    if check.parent is not None:
        _settle_package(node, check, check.parent[0], refusal)
        return
    tx, conn = check.tx, check.conn
    if already_judged(node, tx, conn):
        return
    candidate = _pre_verify(node, tx, conn, first_time=check.first_time)
    if candidate is None:
        return
    if isinstance(refusal, BTClibValueError):
        # recorded and the peer kept, `_pre_verify`'s comment says why
        _rejected(node, tx, refusal, conn, first_time=check.first_time)
        return
    if refusal is not None:
        raise refusal
    # `add_tx`'s own return value is the gate: a silent no-op for one
    # `Mempool._evict_to_limit` (btclib-org/btclib-node#294) takes right
    # back out for being the worst transaction held once its own add put
    # the mempool past `bytesize_limit` -- and a transaction this node
    # declined to keep is not one to tell every other peer about, a peer
    # that then asks for it getting `notfound` for its trouble.
    # btclib-org/btclib-node#277
    tip_height = len(node.chainstate.block_index.active_chain) - 1
    if not node.mempool.add_tx(
        tx, candidate.fee, candidate.vsize, height=tip_height, weight=candidate.weight
    ):
        # Core's `TX_RECONSIDERABLE` "mempool full": it no longer meets the
        # minimum the eviction left, though a child may pay for it
        _rejected(
            node, tx, TxRejectedError("mempool full"), conn, first_time=check.first_time
        )
        return
    if check.first_time:
        # novel and accepted into the mempool: what Core's own
        # `m_last_tx_time` records for eviction (`net_processing.cpp`'s
        # `ProcessMessage`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
        conn.last_novel_tx_time = int(time.time())
    _accepted(node, tx, conn)


def _settle_package(
    node: Node, check: TxCheck, parent: Tx, refusal: Exception | None
) -> None:
    """Keep a parent and its child together, or record why not.

    `settle_tx` for a package, `check.tx` being the child. The checks but
    the scripts run again first, as they do for one transaction, and a
    package whose member the mempool holds or refused is dropped. A script
    refusal belongs to the transaction `check.failed` names, and the
    other answers what it did when the package was verified: the parent a
    fee floor, the child a missing input.
    """
    child, conn = check.tx, check.conn
    if already_judged(node, parent, conn) or already_judged(node, child, conn):
        # taken in or refused meanwhile, by another peer's copy or a call
        return
    try:
        candidate = pre_verify_package(node, parent, child)
    except PackageRefusedError as refused:
        _package_refused(node, conn, parent, child, refused.errors)
        return
    if candidate.child is None:
        # the parent now passes by itself, which the package is for only
        # where it does not
        node.tx_checks.queue(
            TxCheck(conn, parent, candidate.parent.prev_outputs, first_time=False)
        )
        return
    if refusal is not None and not isinstance(refusal, BTClibValueError):
        raise refusal
    errors: dict[bytes, Exception]
    if refusal is None:
        members = [
            (tx, member.fee, member.vsize, member.weight)
            for tx, member in ((parent, candidate.parent), (child, candidate.child))
        ]
        tip_height = len(node.chainstate.block_index.active_chain) - 1
        if node.mempool.add_package(members, height=tip_height):
            # Core iterates backwards, so that the child leaves the
            # orphanage before it can be marked for reconsidering
            for member in (child, parent):
                _accepted(node, member, conn)
            return
        # not `TxRejectedError`, so not reconsiderable: `AcceptPackage`'s
        # "mempool full" is `TX_MEMPOOL_POLICY` (`src/validation.cpp:1748`,
        # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
        errors = {
            member.hash: BTClibValueError("mempool full") for member in (parent, child)
        }
    else:
        assert candidate.parent_error is not None  # noqa: S101
        errors = {
            parent.hash: candidate.parent_error,
            child.hash: MissingPrevoutError(),
            (parent, child)[check.failed].hash: refusal,
        }
    _package_refused(node, conn, parent, child, errors)


def _unrequested_block_refused(node: Node, block_hash: bytes) -> bool:
    """Whether a block nobody asked for is left unprocessed, as Core leaves it.

    Core's `AcceptBlock` (`validation.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag) returns without storing an unrequested block with less
    work than the active tip, more than `MIN_BLOCKS_TO_KEEP` above it, or
    below the minimum chain work. Its other refusal, a block processed
    before and pruned since, falls under the first: pruning stays
    `MIN_BLOCKS_TO_KEEP` below the tip, so such a block has less work.
    """
    block_index = node.chainstate.block_index
    active_chain = block_index.active_chain
    work = block_index.chainwork[block_hash]
    height = block_index.get_block_info(block_hash).index
    return (
        work < block_index.chainwork[active_chain[-1]]
        or height > len(active_chain) - 1 + MIN_BLOCKS_TO_KEEP
        or work < node.config.minimum_chain_work
    )


def _refuse_before_indexing(node: Node, block: Block, conn: Connection) -> None:
    """Refuse what Core refuses of a `block` before its header is indexed."""
    block_hash = block.header.hash
    block_index = node.chainstate.block_index
    # Core's `BLOCK` arm refuses a mutated body before the header is
    # looked at, once the parent is known, and punishes the peer without
    # touching the index (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7)
    parent = block_index.header_dict.get(block.header.previous_block_hash)
    segwit = parent is not None and (
        parent.index + 1 >= node.chain.consensus.segwit_height
    )
    if parent is not None and is_block_mutated(block, check_witness_root=segwit):
        err_msg = f"mutated block {block_hash.hex()}"
        raise MisbehavingError(err_msg)
    _check_block(node, block, conn, via_compact_block=False)


def _block_refusal(err_msg: str, *, punish: bool) -> BTClibValueError:
    """Return a block's refusal: a `MisbehavingError` where the peer pays."""
    if punish:
        return MisbehavingError(err_msg)
    return BTClibValueError(err_msg)


def _check_block(
    node: Node, block: Block, conn: Connection, *, via_compact_block: bool
) -> None:
    """Refuse a body failing `CheckBlock`, or one under a header marked invalid.

    Neither costs the peer where `via_compact_block`: Core's
    `MaybePunishNodeForBlock` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) punishes nobody for such a
    block that came through `cmpctblock`, BIP152 letting a peer relay a
    block whose header alone it checked.
    """
    block_hash = block.header.hash
    # Core's `ProcessNewBlock` asks `CheckBlock` before `AcceptBlock`, so a
    # body failing it is refused, and its peer punished, before its header
    # is indexed or its being unrequested is looked at; Core never marks
    # such a block failed (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7).
    # `assert_valid_block`, not `passes_check_block`'s own `Block.assert_valid`,
    # so a signet block failing both this and the signet solution answers
    # `bad-signet-blksig` first, `CheckSignetBlockSolution` running ahead of
    # the merkle root in Core's own `CheckBlock` too.
    if not passes_check_block(block):
        try:
            assert_valid_block(block, node.chain)
        except BTClibException as e:
            raise _block_refusal(str(e), punish=not via_compact_block) from e
    # Core's `duplicate-invalid`, before the stored block is looked at:
    # `MaybePunishNodeForBlock` punishes `BLOCK_CACHED_INVALID` from an
    # outbound peer alone, so an inbound one is refused and kept
    if is_cached_invalid(node.chainstate.block_index, block):
        err_msg = f"duplicate-invalid: {block_hash.hex()}"
        raise _block_refusal(err_msg, punish=not (via_compact_block or conn.inbound))


def _min_pow_checked(node: Node, header: BlockHeader) -> bool:
    """Whether a block's chain clears the anti-DoS work threshold.

    Core's `min_pow_checked` in its `BLOCK` arm (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the parent is indexed,
    and its work plus the block's own reaches
    `headers_sync.anti_dos_work_threshold`.
    """
    block_index = node.chainstate.block_index
    parent = header.previous_block_hash
    return parent in block_index.header_dict and (
        block_index.chainwork[parent] + calculate_work(header)
        >= anti_dos_work_threshold(block_index, node.config.minimum_chain_work)
    )


def block(node: Node, msg: bytes, conn: Connection) -> None:
    """Store a requested block once its proof of work checks out.

    A body its header does not commit to (`main.is_block_mutated`), on
    a parent this node knows, is refused first, whatever is already
    stored under that hash: it says nothing about the header. A body
    failing `CheckBlock` (`main.passes_check_block`) is refused next,
    requested or not, with its header left unindexed; then a body under
    a header marked invalid, `duplicate-invalid`
    (`main.is_cached_invalid`). Past that, a no-op if this block is
    already marked downloaded. A body failing a check is refused, and
    the block asked of another peer, with the index left alone except
    where Core marks the block failed (`main.is_block_failed`).

    An unsolicited block whose own header this node has never indexed is not
    read as though `getdata` or `headers` already vouched for it:
    `PeerManagerImpl::ProcessMessage`'s own `NetMsgType::BLOCK` arm
    (`net_processing.cpp`, at bitcoin/bitcoin@ca7162cde5) runs every block
    through `ChainstateManager::AcceptBlock` once `ProcessNewBlock`'s
    `CheckBlock` has passed -- which this function asks first too, in
    `_refuse_before_indexing` -- and `AcceptBlock` calls `AcceptBlockHeader`
    (`validation.cpp`, same sha) on the block's own header before the body's
    remaining checks -- a header already known is accepted outright, and one
    that is not has its own parent looked up, refused with
    `BLOCK_MISSING_PREV` where that parent is unknown too. Core punishes
    that refusal: `MaybePunishNodeForBlock`'s own switch
    (`net_processing.cpp`, same sha) calls `Misbehaving` for
    `BLOCK_MISSING_PREV`, unlike an unconnecting *headers* batch, which
    `ProcessHeadersMessage`'s own `HandleUnconnectingHeaders` answers by
    asking for more rather than by punishing -- the same asymmetry this file
    already carries between `headers` below, which never discourages a batch
    connecting to nothing this node knows (btclib-org/btclib-node#233), and
    this function, which does. `block_index.add_headers([block.header])` is
    `AcceptBlockHeader`'s own shape: it indexes the header where the parent
    is known, raises where the header itself is invalid -- a
    `MisbehavingError` where Core's `MaybePunishNodeForBlock` punishes,
    which `main.handle_p2p`'s own `except` answers by dropping and
    discouraging the peer, as it does for a block failing its own checks
    below -- and, for a single header whose parent is missing, returns
    `None` rather than raising, which is `headers`'s own "ask again" case
    and not this one's: a `MisbehavingError` is raised here instead,
    matching `Misbehaving`. btclib-org/btclib-node#711

    A header new here whose chain is below the anti-DoS work threshold
    (`_min_pow_checked`) is not indexed, and the block is dropped with it,
    its peer kept: Core's `too-little-chainwork`
    (btclib-org/btclib-node#1505).
    """
    # btclib's BlockPayload validates against mainnet's pow limit by
    # default, which no regtest or signet block meets. Its own docstring
    # names the shape: build unchecked and ask afterwards, which is what
    # block.assert_valid below does, against this chain's limit.
    block = BlockMsg.parse(msg, check_validity=False).block
    block_hash = block.header.hash

    # a snapshot, as every other reader on Node's thread takes: P2pManager's
    # thread pops from the dict itself when it drops a stale connection,
    # `conn` among those it could already have dropped by now -- which is
    # why this reads `conn.download_queue` directly rather than through
    # the snapshot too. Reused below, only Node's thread adding to a queue.
    connections = list(node.p2p_manager.connections.values())
    # Core's `IsBlockRequested`: asked of any peer, read before this one's
    # request is removed
    requested = block_hash in conn.download_queue or any(
        block_hash in other.download_queue for other in connections
    )
    # no longer awaited from this peer, whatever it turns out to be: the
    # `RemoveBlockRequest` of Core's `BLOCK` handling (`net_processing.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
    remove_block_request(connections, block_hash, time.time(), conn.id)

    block_index = node.chainstate.block_index
    _refuse_before_indexing(node, block, conn)
    if block_hash not in block_index.header_dict:
        try:
            tip = block_index.add_headers(
                [block.header], min_pow_checked=_min_pow_checked(node, block.header)
            )
        except LowWorkHeaderError as e:
            # Core's `ProcessNewBlock` logs "AcceptBlock FAILED", and its
            # `MaybePunishNodeForBlock` punishes nobody for this result: a
            # refusal it expects, so the line and not a traceback
            node.logger.error("AcceptBlock FAILED (%s)", e)  # noqa: TRY400
            return
        if tip is None:
            err_msg = (
                f"block {block_hash.hex()} has prev block not found: "
                f"{block.header.previous_block_hash.hex()}"
            )
            raise MisbehavingError(err_msg)
    _accept_block(node, block, conn, requested=requested, via_compact_block=False)


def _accept_block(
    node: Node,
    block: Block,
    conn: Connection,
    *,
    requested: bool,
    via_compact_block: bool,
) -> None:
    """Store a block whose header is indexed, where it is new.

    The tail of `block` above, which `cmpctblock` and `blocktxn` reach
    too with a block they rebuilt: Core's `ProcessBlock`, with
    `force_processing` where `requested`. `via_compact_block` spares the
    peer a block failing a check, as `_check_block` says.
    """
    block_hash = block.header.hash
    block_index = node.chainstate.block_index
    block_info = block_index.get_block_info(block_hash)

    if block_info.downloaded or (
        not requested and _unrequested_block_refused(node, block_hash)
    ):
        return
    # a block that does not hold up is nobody's: the raise reaches
    # p2p.main.handle_p2p, which drops the peer that sent it, unless the
    # block came through BIP152, where a plain `BTClibValueError` keeps
    # it. Invalidated first where Core marks it failed
    # (`main.is_block_failed`), so the next peer offering it is refused
    # before it is asked to send it. A `MisbehavingError` outside
    # BIP152: the body having passed `_check_block`, this is
    # `ContextualCheckBlock`'s `bad-blk-weight`, whose `BLOCK_CONSENSUS`
    # `MaybePunishNodeForBlock` punishes (btclib-org/btclib-node#1170).
    # `assert_valid_block` refuses a version only through the
    # height-gated `bad-version` that `add_headers` already applied to
    # this block's header (btclib-org/btclib-node#1511).
    try:
        assert_valid_block(block, node.chain)
    except BTClibException as e:
        segwit = _segwit_after_parent(node, block.header)
        if is_block_failed(block, check_witness_root=segwit):
            block_index.invalidate(block_hash)
            # Core's own `InvalidChainFound` call, same citation as
            # `main.check_fork_warning_conditions`'s own docstring
            check_fork_warning_conditions(node)
        raise _block_refusal(str(e), punish=not via_compact_block) from e
    node.block_db.add_block(block)
    # novel, past its own checks and on disk: what Core's own
    # `m_last_block_time` records for eviction, whether or not the
    # block later connects (`PeerManagerImpl::ProcessBlock`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag)
    conn.last_novel_block_time = int(time.time())
    node.logger.info("Received new block with hash:%s", block_hash.hex())
    block_index.set_downloaded(block_hash)
    # Core's `AcceptBlock` calls `NewPoWValidBlock` from inside
    # `ProcessNewBlock`, ahead of `ProcessBlock`'s own
    # `RemoveBlockRequest` below, at bitcoin/bitcoin@9be056a8a7
    # (`net_processing.cpp`, the v31.1 tag)
    new_pow_valid_block(node, block)
    # stored, so awaited from nobody: Core's `ProcessBlock`
    connections = list(node.p2p_manager.connections.values())
    remove_block_request(connections, block_hash, time.time())


# The transaction items Core's `IsGenTxMsg` answers for, and the one its
# `INV` loop skips before asking, by whether the peer sent `wtxidrelay`:
# `MSG_TX` from one that did, `MSG_WTX` from one that did not.
_TX_TYPES = frozenset(
    {InventoryType.MSG_TX, InventoryType.MSG_WTX, InventoryType.MSG_WITNESS_TX}
)


def _first_rejected_item(conn: Connection, items: tuple[Inventory, ...]) -> int | None:
    """Return the index of a block-relay-only peer's first transaction item."""
    if not conn.block_relay:
        return None
    skipped = (
        InventoryType.MSG_TX if conn.wtxidrelay_received else InventoryType.MSG_WTX
    )
    return next(
        (
            position
            for position, item in enumerate(items)
            if item.type_code in _TX_TYPES and item.type_code != skipped
        ),
        None,
    )


def inv(node: Node, msg: bytes, conn: Connection) -> None:
    """Ask for headers behind an announced block, queue missing transactions.

    The `INV` branch of Core's `ProcessMessage` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). The last block announced
    that has no header here is asked for with a `getheaders` from the best
    header, whatever the sync state, where `sync_headers` has already asked
    this peer for headers; where it has not, once per peer and once per
    new block, so that header sync takes on one more peer for each block
    found. Either way `maybe_send_getheaders` drops the request while one
    to this peer is in flight, and a peer not yet syncing has its turn
    spent all the same, as in Core. Transactions are queued only out of
    initial block download, where Core calls `AddTxAnnouncement`.

    A block-relay-only peer announcing a transaction is disconnected
    there and then, as Core's loop does on the first such item where
    `RejectIncomingTxs` holds: what came before it is taken, and nothing
    after it, `getheaders` included. An item of the kind Core skips for
    the peer's `wtxidrelay` is not one: `MSG_TX` from a peer that sent
    it, `MSG_WTX` from one that did not.
    """
    _refuse_past_bound("inv", _count_past(msg, MAX_INV_SZ, _INV_ENTRY_SIZE))
    inv = Inv.parse(msg)
    rejected = _first_rejected_item(conn, inv.items)

    block_index = node.chainstate.block_index
    for item in inv.items[:rejected]:
        if item.type_code == InventoryType.MSG_BLOCK:
            update_block_availability(block_index, conn.block_availability, item.hash)
    if rejected is not None:
        node.logger.log_debug(
            "net", "transaction inv sent in violation of protocol, peer=%s", conn.id
        )
        conn.stop()
        return
    unknown = [
        x.hash
        for x in inv.items
        if x.type_code == InventoryType.MSG_BLOCK
        and x.hash not in block_index.header_dict
    ]
    if unknown:
        manager = node.download_manager
        sync_started = conn.id in manager.headers_sync_timeouts
        if sync_started or (
            conn.id not in manager.inv_triggered_getheaders
            and unknown[-1] != manager.last_block_inv_triggering_headers_sync
        ):
            block_locators = block_index.get_block_locator_hashes()
            maybe_send_getheaders(node, conn, block_locators)
            if not sync_started:
                manager.inv_triggered_getheaders.add(conn.id)
                manager.last_block_inv_triggering_headers_sync = unknown[-1]

    # Core skips `MSG_TX` from a peer that sent `wtxidrelay` and `MSG_WTX`
    # from one that did not, and reads `MSG_WITNESS_TX` as a txid from
    # either (`net_processing.cpp` and `protocol.cpp`, at
    # bitcoin/bitcoin@9be056a8a7, the v31.1 tag). The announcement keeps
    # which of the two it is: a wtxid-relay peer's `MSG_WITNESS_TX` is
    # looked up and asked for as a txid. btclib-org/btclib-node#1774
    skipped = (
        InventoryType.MSG_TX if conn.wtxidrelay_received else InventoryType.MSG_WTX
    )
    announced = [
        (x.hash, x.type_code != InventoryType.MSG_WTX)
        for x in inv.items
        if x.type_code in _TX_TYPES and x.type_code != skipped
    ]
    # Core's `AddKnownTx` runs on each such item before its IBD check:
    # a peer that announced a transaction has it.
    for announced_hash, _ in announced:
        conn.known_tx_inventory.add(announced_hash)
    if node.is_initial_block_download:
        return
    mempool = node.mempool
    missing_txids = set(
        mempool.get_missing([h for h, txid in announced if txid], wtxid=False)
    )
    missing_wtxids = set(
        mempool.get_missing([h for h, txid in announced if not txid], wtxid=True)
    )
    node.download_manager.inv_txs.extend(
        (conn.id, h, txid)
        for h, txid in announced
        if h in (missing_txids if txid else missing_wtxids)
    )


# The two families `advance_getdata` below dispatches on -- everything
# else a `getdata` may name (`MSG_FILTERED_BLOCK`, `UNDEFINED`, an
# unrecognised code) is neither, and is popped off the front of the
# pending items and otherwise ignored, the same silence `_filter_range`
# already answers a request it declines with elsewhere in this module.
_GETDATA_TX_TYPES = (
    InventoryType.MSG_TX,
    InventoryType.MSG_WTX,
    InventoryType.MSG_WITNESS_TX,
)
_GETDATA_BLOCK_TYPES = (
    InventoryType.MSG_BLOCK,
    InventoryType.MSG_WITNESS_BLOCK,
    InventoryType.MSG_CMPCT_BLOCK,
)

# BIP152's compact blocks as Core serves them (`net_processing.cpp`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the one version Core speaks,
# the depth past which a `MSG_CMPCT_BLOCK` is answered with the full block
# instead, and the depth past which a `getblocktxn` is answered with the
# full block too
CMPCTBLOCKS_VERSION = 2
MAX_CMPCTBLOCK_DEPTH = 5
MAX_BLOCKTXN_DEPTH = 10
# Core's `CanDirectFetch`: the tip is within this many target spacings
# of now
_DIRECT_FETCH_SPACINGS = 20


def _can_direct_fetch(node: Node) -> bool:
    """Core's `CanDirectFetch`: whether this node's tip is recent."""
    block_index = node.chainstate.block_index
    tip = block_index.header_dict[block_index.active_chain[-1]].header
    spacing = node.chain.consensus.pow_target_spacing
    return tip.time.timestamp() > time.time() - spacing * _DIRECT_FETCH_SPACINGS


def _block_answer(node: Node, item: Inventory, block: Block) -> BlockMsg | CmpctBlock:
    """Answer a block item as Core's `ProcessGetBlockData` does.

    A `MSG_CMPCT_BLOCK` for a block at most `MAX_CMPCTBLOCK_DEPTH` below
    a recent tip gets a `cmpctblock`, the one `new_pow_valid_block` built
    where the block is `node.most_recent_block` and one under a fresh
    nonce otherwise; one for an older block gets the full block with
    witnesses, "we're almost guaranteed they won't have a useful mempool
    to match against".
    """
    if item.type_code == InventoryType.MSG_CMPCT_BLOCK:
        block_index = node.chainstate.block_index
        height = block_index.header_dict[item.hash].index
        tip_height = len(block_index.active_chain) - 1
        if _can_direct_fetch(node) and height >= tip_height - MAX_CMPCTBLOCK_DEPTH:
            recent = node.most_recent_block
            if recent is not None and recent.hash == item.hash:
                return recent.compact
            return compact_block(block, secrets.randbits(64))
        include_witness = True
    else:
        include_witness = item.type_code == InventoryType.MSG_WITNESS_BLOCK
    return BlockMsg(block, include_witness=include_witness, check_validity=False)


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
    (`connection.py`'s own `own_version`, gated on `Config.pruned`
    the identical way), so this reads `node.config.pruned` directly
    rather than a per-connection record of what was sent. `+ 2` is
    Core's own buffer, "for possible races". `block_hash` is indexed:
    `_serve_getdata_block` has already answered Core's own `if (!pindex)
    return;`.
    """
    block_index = node.chainstate.block_index
    block_info = block_index.get_block_info(block_hash)
    tip_height = len(block_index.active_chain) - 1
    return tip_height - block_info.index > MIN_BLOCKS_TO_KEEP + 2


def _find_tx_for_getdata(node: Node, tx_hash: bytes, *, wtxid: bool) -> Tx | None:
    """Return the mempool's transaction, else the most recent block's.

    Core's `FindTxForGetData`: the most recent block's transactions are
    `m_most_recent_block_txs`.
    """
    tx = node.mempool.get_tx(tx_hash, wtxid=wtxid)
    if tx is None and node.most_recent_block is not None:
        tx = node.most_recent_block.txs.get((wtxid, tx_hash))
    return tx


def _serve_getdata_tx(
    node: Node, conn: Connection, item: Inventory, not_found: list[Inventory]
) -> None:
    """Serve one transaction item, appending a miss to `not_found`."""
    if not conn.relay_tx:
        return
    wtxid = item.type_code == InventoryType.MSG_WTX
    tx = _find_tx_for_getdata(node, item.hash, wtxid=wtxid)
    if tx:
        include_witness = item.type_code in (
            InventoryType.MSG_WITNESS_TX,
            InventoryType.MSG_WTX,
        )
        conn.send(TxMsg(tx, include_witness=include_witness))
        # Core's own `m_mempool.RemoveUnbroadcastTx(tx->GetHash())`
        # (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7,
        # the v31.1 tag): this peer's own `getdata` is the
        # acknowledgment `getmempoolinfo`'s own `unbroadcastcount`
        # waits for. `tx->GetHash()` is a txid, matching what
        # `mark_broadcast` reads `tx.id` by, not `item.hash`, which
        # is a wtxid for a `MSG_WTX` request.
        # btclib-org/btclib-node#1421
        node.mempool.mark_broadcast(tx.id)
    else:
        not_found.append(item)


def _serve_getdata_block(node: Node, conn: Connection, item: Inventory) -> None:
    """Serve one block item, as Core's `ProcessGetBlockData` does.

    Core (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7)
    ignores a block it has no index entry for, then one
    `BlockRequestAllowed` refuses -- off the active chain and not
    recently valid -- before the prune threshold.
    """
    if item.hash not in node.chainstate.block_index.header_dict:
        return
    if not _block_request_allowed(node, item.hash):
        return
    if (
        node.config.pruned
        and NetPermissionFlags.NO_BAN not in conn.permissions
        and _below_prune_threshold(node, item.hash)
    ):
        conn.stop()
        return
    # Core's `a_recent_block`, ahead of the read
    recent = node.most_recent_block
    block = (
        recent.block
        if recent is not None and recent.hash == item.hash
        else node.block_db.get_block(item.hash)
    )
    if block:
        conn.send(_block_answer(node, item, block))
        # Core's `m_continuation_block`, right after the block and even
        # where redundant; a block with no data returns before it
        if item.hash == conn.continuation_block:
            tip = node.chainstate.block_index.active_chain[-1]
            conn.send(Inv([Inventory(InventoryType.MSG_BLOCK, tip)]))
            conn.continuation_block = None


def advance_getdata(node: Node, conn: Connection, items: deque[Inventory]) -> bool:
    """Serve from the front of `items` as Core's `ProcessGetData` does.

    Shared by `getdata` below, dispatching a request for the first time,
    and by `p2p.main.resume_getdata`, Core's next call, which serves a
    paused request on a later pass of `Node`'s loop. Each pops what it
    serves off the front of the same `deque`. Answers whether `items` is
    now empty.

    Core's `ProcessGetData` (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) serves the transactions
    at the front, then one other item, checking `fPauseSend` before each:
    "the send buffer provides backpressure". `conn.pause_send` is that
    flag. `conn.send` counts each item on this thread before scheduling
    its write, so the item that sets it pauses the next.

    A transaction is served from the mempool, then from the most recent
    block, only if the peer wants it relayed, answered `notfound` on a
    miss; a requested block not held
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

    The misses of a call go in one `notfound`, sent once the call has
    served what it could, as Core's `vNotFound` is a per-call local.
    """
    not_found: list[Inventory] = []
    while items and items[0].type_code in _GETDATA_TX_TYPES:
        if conn.status == P2pConnStatus.Closed:
            return True
        if conn.pause_send:
            break
        _serve_getdata_tx(node, conn, items.popleft(), not_found)
    if items and not conn.pause_send:
        # Core's one block per call; an item of neither family is
        # popped and otherwise ignored, as Core erases it
        item = items.popleft()
        if item.type_code in _GETDATA_BLOCK_TYPES:
            _serve_getdata_block(node, conn, item)
    if not_found:
        conn.send(NotFound(not_found))
    return not items


def getdata(node: Node, msg: bytes, conn: Connection) -> None:
    """Answer a peer's request for the transactions and blocks it named.

    `advance_getdata` above is where every item is actually served, and
    where this request's own place in Core's `getdata` semantics is
    argued; this only starts a request, and leaves on
    `node.pending_getdata` what it could not finish.

    Core's `ProcessMessages` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) returns before
    `PollMessage` whenever `m_getdata_requests` is non-empty or
    `fPauseSend` is set: "this maintains the order of responses and
    prevents m_getdata_requests to grow unbounded". It never backlogs more
    than one request's own `MAX_INV_SZ` items per connection, and reads
    nothing more from it, `getdata` included, until the current one has
    drained.

    This ports the first condition (btclib-org/btclib-node#1775); the
    second is `_hold_message`'s (btclib-org/btclib-node#1796). While
    `conn` has an entry on `node.pending_getdata`, `_hold_message`
    (`p2p/main.py`) holds its later messages, in order, and
    `resume_tx_checks` reads them once `resume_getdata` has finished
    the answer. So an entry is never
    extended by a second request, and holds one request's items at most.

    The messages held are weighed against `conn.queued_recv_bytes`, so
    reads from the connection pause past `conn.recv_flood_size`: that
    bounds what a peer can pile up behind a paused answer.
    """
    _refuse_past_bound("getdata", _count_past(msg, MAX_INV_SZ, _INV_ENTRY_SIZE))
    items = deque(GetData.parse(msg).items)
    if not advance_getdata(node, conn, items):
        node.pending_getdata[conn.id] = (conn, items)


def headers(node: Node, msg: bytes, conn: Connection) -> None:
    """Index a batch of headers, ask for more, or mark header sync finished.

    Core's `ProcessHeadersMessage` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), in its order. An empty
    batch is the peer having nothing to give: it ends a low-work sync
    with it and asks for nothing more. A batch is checked for proof of
    work and continuity, then handed to the peer's low-work sync where
    one runs (`_is_continuation_of_low_work_headers_sync`), which may
    keep it all. A batch connecting to nothing known asks again from
    what this node already has. A connecting batch whose chain has less
    work than `headers_sync.anti_dos_work_threshold` is not indexed: a
    full one starts a low-work sync, a short one is ignored
    (`_try_low_work_headers_sync`), and neither costs the peer anything.
    A batch past all that is indexed, asks for the next one where it was
    full and no sync is asking already, and ends header sync where it
    was short; it is then handed to
    `DownloadManager.headers_direct_fetch`.

    An empty batch, or one that connects, answers the `getheaders` in
    flight to this peer, as does a batch a low-work sync takes; one
    connecting to nothing may be an announcement, and answers nothing.

    A peer holding `NO_BAN` skips the work check, as in Core.
    """
    # Core reads the count alone before it compares, so no entry is
    # needed in the payload for it to call `Misbehaving`
    _refuse_past_bound("headers", _count_past(msg, MAX_HEADERS_RESULTS, 0))
    # Unchecked, as Core's own `CBlockHeader` read checks nothing:
    # btclib's `BlockHeader.assert_valid` refuses a time before genesis,
    # which Core leaves to `ContextualCheckBlockHeader`'s `time-too-old`,
    # a `Misbehaving`, and that alone is reason enough to keep this
    # unchecked. It used to also refuse a version of zero or below on
    # its own, where Core leaves that to the same function's
    # `bad-version` -- fixed
    # at btclib 2026.9.29 (btclib-org/btclib@bbb1ad71, closing
    # btclib-org/btclib#2309; btclib-org/btclib-node#1511). The count
    # and the transaction counts are bounded either way, and
    # `add_headers` checks the work and both of Core's own contextual
    # refusals.
    headers: Sequence[BlockHeader] = Headers.parse(msg, check_validity=False).headers
    _process_headers(node, conn, headers, via_compact_block=False)


def _process_headers(
    node: Node,
    conn: Connection,
    headers: Sequence[BlockHeader],
    *,
    via_compact_block: bool,
) -> None:
    """Process a batch of headers as `headers` above says.

    Also called by `cmpctblock` with its one header, as Core's
    `CMPCTBLOCK` handler calls `ProcessHeadersMessage` with
    `via_compact_block`, which spares the peer a header already marked
    invalid.
    """
    # what the message carried, which the rest reads whatever a low-work
    # sync hands back in its place: Core's `nCount`
    n_count = len(headers)
    timestamps = node.download_manager.last_getheaders_timestamps
    if not headers:
        # "Nothing interesting. Stop asking this peers for more headers."
        # The peer may have reorganized onto this node's own chain, so a
        # low-work sync with it ends too.
        conn.headers_sync = None
        timestamps.pop(conn.id, None)
        return
    block_index = node.chainstate.block_index
    check_headers_pow(headers, node.chain.pow_limit_bits)
    already_validated_work, headers = _is_continuation_of_low_work_headers_sync(
        node, conn, headers
    )
    if not headers:
        return
    have_headers_sync = conn.headers_sync is not None
    chain_start = headers[0].previous_block_hash
    if chain_start not in block_index.header_dict:
        # Core's `HandleUnconnectingHeaders`: maybe an announcement, so a
        # `getheaders` from what this node has, whatever the batch's
        # length (btclib-org/btclib-node#233), and the batch's last header
        # kept as a block the peer has, unknown until it is indexed
        maybe_send_getheaders(node, conn, block_index.get_block_locator_hashes())
        update_block_availability(
            block_index, conn.block_availability, headers[-1].hash
        )
        return
    timestamps.pop(conn.id, None)
    # Core's `IsAncestorOfBestHeaderOrTip`: a batch this node already
    # holds on its best header chain or its active one costs no memory,
    # and is processed whatever its work
    last = headers[-1].hash
    already_validated_work = (
        already_validated_work
        or last in block_index.header_index_pos
        or _height_on_the_active_chain(node, last) is not None
        # "a trusted peer on startup", Core says: it saves bandwidth
        or NetPermissionFlags.NO_BAN in conn.permissions
    )
    if not already_validated_work and _try_low_work_headers_sync(
        node, conn, chain_start, headers
    ):
        return
    received_new_header = last not in block_index.header_dict
    # add_headers raises on a batch it refuses, and the raise is left to
    # reach handle_p2p, which discourages the peer for a
    # `MisbehavingError` the same way block's own does: a peer that sent
    # it is not one telling us it has nothing left, and this is not the
    # ordinary end of a sync. btclib-org/btclib-node#75
    # A header already marked invalid costs an outbound peer alone, as
    # Core's `MaybePunishNodeForBlock` has it for `BLOCK_CACHED_INVALID`.
    # The batch connects, so add_headers answers a hash.
    tip = cast(
        "bytes",
        block_index.add_headers(
            headers, punish_cached_invalid=not (conn.inbound or via_compact_block)
        ),
    )
    # Core's `m_last_block_announcement`, stamped where its last header
    # was new and it has more work than the active tip
    if (
        received_new_header
        and block_index.chainwork[tip]
        > block_index.chainwork[block_index.active_chain[-1]]
    ):
        conn.last_block_announcement = int(time.time())
    # The batch's last header is a block the peer has: Core's
    # `UpdatePeerStateForReceivedHeaders`
    update_block_availability(block_index, conn.block_availability, tip)
    # Core protects only a peer it did not just drop, and asks whether
    # to drop it only where the message was short of a full one
    if not (
        n_count < MAX_HEADERS_RESULTS and disconnect_if_insufficient_work(node, conn)
    ):
        protect_if_caught_up(node, conn)
    _ask_for_more_headers(node, conn, n_count, tip, have_headers_sync=have_headers_sync)
    # Core's `ProcessHeadersMessage` ends by considering "immediately
    # downloading blocks", `HeadersDirectFetchBlocks`
    node.download_manager.headers_direct_fetch(conn, tip)


def _is_continuation_of_low_work_headers_sync(
    node: Node, conn: Connection, headers: Sequence[BlockHeader]
) -> tuple[bool, Sequence[BlockHeader]]:
    """Hand `headers` to `conn`'s low-work sync, where one runs.

    Core's `IsContinuationOfLowWorkHeadersSync` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Answers whether the sync
    took the batch, and the headers left for `headers` to process: those
    the sync released for indexing where it took the batch, none of them
    in `PRESYNC`, and the batch itself where no sync runs or the sync
    refused it. A batch taken answers the `getheaders` in flight; one the
    sync asks more for sends the next; a sync that ended is dropped.

    Core also ranks every peer's `PRESYNC` progress, for the "Pre-synchronizing
    blockheaders" line its log and its GUI show; this node shows no such
    line, so it keeps no such ranking.
    """
    sync = conn.headers_sync
    if sync is None:
        return False, headers
    result = sync.process_next_headers(
        headers, full_headers_message=len(headers) == MAX_HEADERS_RESULTS
    )
    if result.success:
        node.download_manager.last_getheaders_timestamps.pop(conn.id, None)
    if result.request_more:
        maybe_send_getheaders(node, conn, sync.next_headers_request_locator())
    if sync.state is State.FINAL:
        conn.headers_sync = None
    if result.success:
        return True, result.pow_validated_headers
    return False, headers


def _try_low_work_headers_sync(
    node: Node, conn: Connection, chain_start: bytes, headers: Sequence[BlockHeader]
) -> bool:
    """Keep a low-work batch out of the index, syncing its chain where it may.

    Core's `TryLowWorkHeadersSync` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `chain_start`, where the
    batch connects, plus the work the batch claims, below
    `anti_dos_work_threshold` is a chain this node does not index yet. A
    full batch starts `conn`'s low-work sync from `chain_start`, towards
    that threshold as it stands now, and hands it the batch; a short one
    has nothing behind it to reach the threshold with, and is ignored.
    Answers whether the batch was kept out, which is the end of it for
    `headers` either way.
    """
    block_index = node.chainstate.block_index
    total_work = block_index.chainwork[chain_start] + sum(
        calculate_work(header) for header in headers
    )
    threshold = anti_dos_work_threshold(block_index, node.config.minimum_chain_work)
    if total_work >= threshold:
        return False
    if len(headers) == MAX_HEADERS_RESULTS:
        conn.headers_sync = HeadersSyncState(
            node.chain.consensus,
            node.chain.headers_sync_params,
            ChainStart.from_index(block_index, chain_start),
            threshold,
        )
        _is_continuation_of_low_work_headers_sync(node, conn, headers)
    else:
        node.logger.log_debug(
            "net",
            "Ignoring low-work chain (height=%d) from peer=%d",
            block_index.get_block_info(chain_start).index + len(headers),
            conn.id,
        )
    return True


def _ask_for_more_headers(
    node: Node,
    conn: Connection,
    batch_size: int,
    tip: bytes,
    *,
    have_headers_sync: bool,
) -> None:
    """Ask `conn` for the headers past a batch, or mark header sync finished.

    `tip` is what `add_headers` answered for a message of `batch_size`
    headers. A low-work sync still running with `conn` asks for its own
    next batch, so `have_headers_sync` is what keeps this from asking too.
    """
    block_index = node.chainstate.block_index
    if batch_size == MAX_HEADERS_RESULTS:  # the peer may have more to give us
        if have_headers_sync:
            return
        # [tip] only for a live fork below header_index's own tip: that
        # is the one case get_block_locator_hashes cannot reach on its
        # own, since header_index only moves for a header extending it
        # or beating its chainwork, and a locator built from it would
        # ask for this same batch again and stall short of the fork's
        # own tip. An ordinary batch extending header_index already gets
        # header_index's own richer, multi-entry locator, unchanged. A
        # batch on a branch this node proved invalid never gets here:
        # add_headers refuses it. btclib-org/btclib-node#122
        if tip != block_index.header_index[-1]:
            block_locators = [tip]
        else:
            block_locators = block_index.get_block_locator_hashes()
        maybe_send_getheaders(node, conn, block_locators)
    elif node.status == NodeStatus.SyncingHeaders:
        node.status = NodeStatus.HeaderSynced


# Core's `STALE_RELAY_AGE_LIMIT` (`src/net_processing.cpp`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): how old, in time and in
# proof-equivalent time, a block off the active chain may be and still
# be served.
_STALE_RELAY_AGE_LIMIT = 30 * 24 * 60 * 60


def _find_fork_in_global_index(node: Node, locator: Sequence[bytes]) -> bytes:
    """Return the last block of the active chain the locator names.

    Core's `Chainstate::FindForkInGlobalIndex` (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the first entry known
    here that is on the active chain, or the active tip where the entry
    descends from it, and genesis where no entry is either.
    """
    block_index = node.chainstate.block_index
    active_chain = block_index.active_chain
    tip_height = len(active_chain) - 1
    for block_hash in locator:
        if _height_on_the_active_chain(node, block_hash) is not None:
            return block_hash
        if (
            block_hash in block_index.header_dict
            and block_index.get_ancestor(block_hash, tip_height) == active_chain[-1]
        ):
            return active_chain[-1]
    return active_chain[0]


def _block_request_allowed(node: Node, block_hash: bytes) -> bool:
    """Whether a peer may be served `block_hash`: Core's `BlockRequestAllowed`.

    A block of the active chain is; one off it is where it passed
    validation (`BlockStatus.valid`, Core's `BLOCK_VALID_SCRIPTS`) and is
    less than `_STALE_RELAY_AGE_LIMIT` older than the
    best header, by its timestamp and by Core's
    `GetBlockProofEquivalentTime`, the chain work between the two in
    blocks of the best header's own work, each `pow_target_spacing`
    long.
    """
    if _height_on_the_active_chain(node, block_hash) is not None:
        return True
    block_index = node.chainstate.block_index
    block_info = block_index.get_block_info(block_hash)
    if block_info.status != BlockStatus.valid:
        return False
    best_hash = block_index.header_index[-1]
    best = block_index.get_block_info(best_hash).header
    chainwork = block_index.chainwork
    spacing = node.chain.consensus.pow_target_spacing
    proof_time = (
        (chainwork[best_hash] - chainwork[block_hash]) * spacing // calculate_work(best)
    )
    return (
        block_time(best) - block_time(block_info.header) < _STALE_RELAY_AGE_LIMIT
        and proof_time < _STALE_RELAY_AGE_LIMIT
    )


def getheaders(node: Node, msg: bytes, conn: Connection) -> None:
    """Answer a peer's `getheaders` off the active chain, as Core does.

    The `GETHEADERS` branch of Core's `ProcessMessage`
    (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag). An active chain with less work than the minimum chain work is
    answered with an empty `headers`, unless the peer holds `DOWNLOAD`. An
    empty locator asks for the
    `hash_stop` header alone, answered only where it is known and
    `_block_request_allowed`, and not at all otherwise. Any other
    locator is answered with the active chain after
    `_find_fork_in_global_index`'s block, up to `MAX_HEADERS_RESULTS`
    headers and up to `hash_stop`: empty where that block is the tip.
    The peer's `best_header_sent` becomes the last header sent, or the
    tip where none is.
    """
    # Core drops a peer whose locator passes `MAX_LOCATOR_SZ`, and does
    # not discourage it. The locator follows a four-octet version, and
    # `hash_stop` follows the locator.
    if _count_past(msg[:-_HASH_SIZE], MAX_LOCATOR_SZ, _HASH_SIZE, offset=4):
        conn.stop()
        return
    getheaders = GetHeaders.parse(msg)
    block_index = node.chainstate.block_index
    active_chain = block_index.active_chain
    tip = active_chain[-1]
    if (
        block_index.chainwork[tip] < node.config.minimum_chain_work
        and NetPermissionFlags.DOWNLOAD not in conn.permissions
    ):
        conn.send(Headers([]))
        return
    stop = getheaders.hash_stop
    if not getheaders.locator:
        if stop not in block_index.header_dict or not _block_request_allowed(
            node, stop
        ):
            return
        to_send = [stop]
    else:
        fork = _find_fork_in_global_index(node, getheaders.locator)
        start = block_index.get_block_info(fork).index + 1
        end = start + MAX_HEADERS_RESULTS
        stop_height = _height_on_the_active_chain(node, stop)
        if stop_height is not None and start <= stop_height < end:
            end = stop_height + 1
        to_send = active_chain[start:end]
    conn.block_availability.best_header_sent = to_send[-1] if to_send else tip
    conn.send(
        Headers(
            [block_index.get_block_info(block_hash).header for block_hash in to_send]
        )
    )


# Core's `nLimit` in the `GETBLOCKS` branch of `ProcessMessage`: how many
# blocks one `getblocks` is answered with.
_GETBLOCKS_LIMIT = 500


def getblocks(node: Node, msg: bytes, conn: Connection) -> None:
    """Answer a peer's `getblocks` with an `inv`, as Core does.

    The `GETBLOCKS` branch of Core's `ProcessMessage`
    (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag). A locator past `MAX_LOCATOR_SZ` drops the peer, undiscouraged.
    Any other first activates the best chain, so that a block just
    announced is on it, and is answered with the `MSG_BLOCK` hashes of the
    active chain
    after `_find_fork_in_global_index`'s block, up to `_GETBLOCKS_LIMIT`
    of them and up to `hash_stop`, which is not sent. A pruned node also
    stops at a block it holds no data for, or one so deep that a peer
    could soon find it gone: `MIN_BLOCKS_TO_KEEP` less an hour's blocks
    behind the tip. The last block sent at the limit
    is the peer's `continuation_block`, which `_serve_getdata_block`
    answers with an `inv` of the tip, for the peer's next `getblocks`.
    Core sends the `inv` on its next `SendMessages`; here it is sent
    at once, nothing else being queued ahead of it.
    """
    if _count_past(msg[:-_HASH_SIZE], MAX_LOCATOR_SZ, _HASH_SIZE, offset=4):
        conn.stop()
        return
    request = GetBlocks.parse(msg)
    # Core's `ActivateBestChain(a_recent_block)`: a block announced from
    # `block` is connected only in `update_chain`, after this share of the
    # loop, so without it the `inv` would leave out the block this node
    # has just announced. A failure ends the node, as in `rpc.callbacks`.
    try:
        activate_best_chain(node)
    except Exception:
        node.terminate_flag.set()
        raise
    block_index = node.chainstate.block_index
    active_chain = block_index.active_chain
    fork = _find_fork_in_global_index(node, request.locator)
    start = block_index.get_block_info(fork).index + 1
    tip_height = len(active_chain) - 1
    keep = MIN_BLOCKS_TO_KEEP - 3600 // node.chain.consensus.pow_target_spacing
    inventory: list[Inventory] = []
    for height in range(start, tip_height + 1):
        block_hash = active_chain[height]
        if block_hash == request.hash_stop:
            break
        if node.config.pruned and (
            not block_index.get_block_info(block_hash).downloaded
            or height <= tip_height - keep
        ):
            break
        inventory.append(Inventory(InventoryType.MSG_BLOCK, block_hash))
        if len(inventory) == _GETBLOCKS_LIMIT:
            conn.continuation_block = block_hash
            break
    if inventory:
        conn.send(Inv(inventory))


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


def _ancestor(block_index: BlockIndex, block_hash: bytes, height: int) -> bytes:
    """`block_hash`'s ancestor at `height`, where the caller has bounded it.

    `BlockIndex.get_ancestor` answers `None` only above `block_hash`'s
    own height or below zero; every call site below keeps `height`
    inside `[0, block_hash`'s own height]` before asking, so `None` here
    would be a bug in the caller's own bound, not a gap in what this
    node has indexed -- the same shape `block_availability._successors`
    asserts, for the same reason.
    """
    ancestor = block_index.get_ancestor(block_hash, height)
    assert ancestor is not None  # noqa: S101 -- bounded by the caller
    return ancestor


# Core's own `PrepareBlockFilterRequest` passes this for `getcfcheckpt`'s
# `max_height_diff` (`net_processing.cpp:3400`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a checkpoint chain has
# no range bound of its own, BIP157 answering only "the chain's length
# is the bound" (`get_cfcheckpt`'s own comment below), so the range
# check `_prepare_filter_request` shares with `getcfilters`/
# `getcfheaders` is given a ceiling no chain height reaches instead of a
# third branch.
_NO_HEIGHT_DIFF_LIMIT = 2**32 - 1


def _prepare_filter_request(  # noqa: PLR0913, PLR0917
    node: Node,
    conn: Connection,
    filter_type: BlockFilterType | int,
    start_height: int,
    stop_hash: bytes,
    max_height_diff: int,
) -> int | None:
    """Validate a BIP157 filter request, answering the stop block's height.

    Core's `PrepareBlockFilterRequest` (`net_processing.cpp:3265-3312`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): each of the four
    checks below disconnects the peer rather than answering silently,
    logging the line Core logs for it (ISS 1477). A fifth check of
    Core's, the filter index for a supported type not existing, is not
    one of these -- it is this node's own configuration rather than a
    fault the peer caused, and every type `node.config.peerblockfilters`
    lets through here has an index, so it never reaches this function at
    all.

    `not node.config.peerblockfilters` joins the first count rather than
    opening a fifth: Core's own `PrepareBlockFilterRequest` folds
    `filter_type == BASIC` and `peer.m_our_services & NODE_COMPACT_FILTERS`
    into one `supported_filter_type`, and answers a peer that asked for a
    type this node never advertised the same way it answers one that
    asked for a type BIP157 has no other name for.

    The stop block's own height is Core's `BlockRequestAllowed`
    (`_block_request_allowed` above) gating which stale block a peer may
    still be served -- the same gate `getheaders`' own empty-locator
    branch already asks of a `hash_stop`, called at the same sha
    `PrepareBlockFilterRequest` calls it at (ISS 1476).
    """
    if filter_type != BlockFilterType.BASIC or not node.config.peerblockfilters:
        node.logger.log_debug(
            "net",
            "peer requested unsupported block filter type: %s, peer=%s",
            int(filter_type),
            conn.id,
        )
        conn.stop()
        return None
    block_index = node.chainstate.block_index
    if stop_hash not in block_index.header_dict or not _block_request_allowed(
        node, stop_hash
    ):
        node.logger.log_debug(
            "net",
            "peer requested invalid block hash: %s, peer=%s",
            stop_hash.hex(),
            conn.id,
        )
        conn.stop()
        return None
    stop_height = block_index.get_block_info(stop_hash).index
    # BIP157: "The height of the block with hash StopHash MUST be
    # greater than or equal to StartHeight". Only the upper end is
    # checked: the field is unsigned on the wire and these requests are
    # always parsed, so a negative start cannot arrive; `get_cfcheckpt`'s
    # own `start_height` is always zero, so this never trips for it.
    if start_height > stop_height:
        node.logger.log_debug(
            "net",
            "peer sent invalid getcfilters/getcfheaders with start height "
            "%d and stop height %d, peer=%s",
            start_height,
            stop_height,
            conn.id,
        )
        conn.stop()
        return None
    # "and the difference MUST be strictly less than 1,000" -- 2,000 for
    # getcfheaders, and no bound at all for getcfcheckpt
    # (`_NO_HEIGHT_DIFF_LIMIT` above). Strictly, so a range whose ends
    # differ by exactly the bound is one block too many.
    if stop_height - start_height >= max_height_diff:
        node.logger.log_debug(
            "net",
            "peer requested too many cfilters/cfheaders: %d / %d, peer=%s",
            stop_height - start_height + 1,
            max_height_diff,
            conn.id,
        )
        conn.stop()
        return None
    return stop_height


def _filter_range(  # noqa: PLR0913, PLR0917
    node: Node,
    conn: Connection,
    filter_type: BlockFilterType | int,
    start_height: int,
    stop_hash: bytes,
    limit: int,
) -> list[bytes] | None:
    """Return the block hashes a BIP157 range names, on the stop block's chain.

    `None` for a request `_prepare_filter_request` refuses -- which has
    already disconnected the peer where Core would.

    A range is a start height and the hash of the block it ends at, so
    turning it into block hashes is the one thing
    `btclib.p2p.block_filters` leaves to a caller: only a node holds the
    chain that says what height a hash is at. Resolving it walks the
    stop block's own ancestry (`_ancestor` above) rather than
    `active_chain`: a stop hash naming a block this node has since
    reorged away from still names a chain, the one Core's own
    `ProcessGetCFilters`/`ProcessGetCFHeaders` resolve the same way, from
    `stop_index` rather than from `ActiveChain()` (`net_processing.cpp`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag). BIP157 names it "the
    chain terminating in StopHash" and asks only that the hash be "known
    to belong to a block accepted by the receiving peer" -- which a
    block this node has reorged away from still is (ISS 1476).
    """
    stop_height = _prepare_filter_request(
        node, conn, filter_type, start_height, stop_hash, limit
    )
    if stop_height is None:
        return None
    block_index = node.chainstate.block_index
    return [
        _ancestor(block_index, stop_hash, height)
        for height in range(start_height, stop_height + 1)
    ]


def advance_cfilters(node: Node, conn: Connection, block_hashes: deque[bytes]) -> bool:
    """Send from the front of `block_hashes` until `conn.pause_send` is set.

    Shared by `get_cfilters`, dispatching a request for the first time,
    and by `p2p.main.resume_cfilters`, retrying one already paused, on a
    later pass of `Node`'s loop. Each pops what it sends off the front of
    the same `deque`. Answers whether `block_hashes` is now empty.

    Core's `ProcessGetCFilters` (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) queues every filter of
    the answer at once, and `ProcessMessages` reads nothing more from the
    peer until its send buffer is back within `nSendBufferMaxSize`. This
    checks `conn.pause_send` before each filter instead, as
    `advance_getdata` checks it before each item, and `_hold_message`
    (`p2p/main.py`) holds the peer while filters are left. The peer sees
    the same filters, in the same order, and its next message is read at
    the same point: once the last filter is queued and the buffer is back
    within the bound. What differs is memory: an answer of
    `MAX_GETCFILTERS_SIZE` filters is never queued whole.

    `conn.status` is read unlocked: seen one turn late it costs a filter
    serialized for a socket already closed, which `Connection._deliver`
    suppresses.
    """
    filter_index = node.chainstate.filter_index
    while block_hashes:
        if conn.status == P2pConnStatus.Closed:
            return True
        if conn.pause_send:
            return False
        block_hash = block_hashes.popleft()
        # `_filter_range` only ever names a block this node has indexed
        # and that `_block_request_allowed` still permits serving -- on
        # the active chain, or off it and validated, where its filter
        # was computed while it was still connected and stays keyed by
        # hash afterwards (`chainstate.filter_index`'s own module
        # docstring)
        block_filter = filter_index.get_filter(block_hash)
        if block_filter is None:
            err_msg = f"no filter for a block this node has indexed: {block_hash.hex()}"
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
    """Answer a BIP157 `getcfilters` with one `cfilter` per requested block.

    Disconnects on a request `_filter_range` refuses; see its own
    docstring and `_prepare_filter_request`'s. "sequentially in order by
    block height" is BIP157's own words and the reason this is the one
    request answered by many messages rather than one; `_filter_range`
    already bounds how many, and `advance_cfilters` above pauses at the
    send buffer's bound, registering what it could not finish on
    `node.pending_cfilters` for `p2p.main.resume_cfilters` to complete.

    While `conn` has an entry on `node.pending_cfilters`,
    `_hold_message` (`p2p/main.py`) holds its later messages, in
    order, and `resume_tx_checks` reads them once `resume_cfilters` has
    finished the answer (btclib-org/btclib-node#1789). So an entry is
    never extended by a second request, and holds one range at most.

    The messages held are weighed against `conn.queued_recv_bytes`, so
    reads from the connection pause past `conn.recv_flood_size`: that
    bounds what a peer can pile up behind a paused answer.
    """
    request = GetCFilters.parse(msg)
    block_hashes = _filter_range(
        node,
        conn,
        request.filter_type,
        request.start_height,
        request.stop_hash,
        MAX_GETCFILTERS_SIZE,
    )
    if block_hashes is None:
        return
    pending = deque(block_hashes)
    if not advance_cfilters(node, conn, pending):
        node.pending_cfilters[conn.id] = (conn, pending)


def get_cfheaders(node: Node, msg: bytes, conn: Connection) -> None:
    """Answer a BIP157 `getcfheaders` with the requested range's filter headers.

    Disconnects on a request `_filter_range` refuses; see its own
    docstring and `_prepare_filter_request`'s.
    """
    request = GetCFHeaders.parse(msg)
    block_hashes = _filter_range(
        node,
        conn,
        request.filter_type,
        request.start_height,
        request.stop_hash,
        MAX_GETCFHEADERS_SIZE,
    )
    if block_hashes is None:
        return
    block_index = node.chainstate.block_index
    filter_index = node.chainstate.filter_index
    start = request.start_height
    # the header of the block before the range, which is what the
    # hashes below chain onto -- resolved along the stop block's own
    # ancestry, the same as every hash in `block_hashes` (`_filter_range`'s
    # own docstring). BIP157: "The previous filter header used to
    # calculate that of the genesis block is defined to be the 32-byte
    # array of 0's."
    previous = (
        filter_index.get_header(_ancestor(block_index, request.stop_hash, start - 1))
        if start
        else NO_PREVIOUS_FILTER_HEADER
    )
    # every block `_filter_range` named has been connected at some point
    # -- `_block_request_allowed` -- so its filter and its header are
    # still in the index (`chainstate.filter_index`'s own module
    # docstring)
    if previous is None:
        err_msg = "no filter header for the parent of the requested range"
        raise ChainstateInconsistencyError(err_msg)
    filter_hashes = []
    for block_hash in block_hashes:
        filter_hash = filter_index.get_filter_hash(block_hash)
        if filter_hash is None:
            err_msg = f"no filter for a block this node has indexed: {block_hash.hex()}"
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

    Disconnects for an unsupported filter type, a type not advertised
    under `-peerblockfilters`, or a stop hash `_block_request_allowed`
    refuses -- `_prepare_filter_request`'s own docstring, matching Core
    (ISS 1477).
    """
    request = GetCFCheckpt.parse(msg)
    # not _filter_range: this request carries no start height, a
    # checkpoint chain always beginning at the genesis block, so the
    # start-height and range-size checks `_prepare_filter_request` folds
    # in for getcfilters/getcfheaders never trip here --
    # `_NO_HEIGHT_DIFF_LIMIT`'s own comment says why a limit is still
    # passed rather than a third branch opened for their absence.
    stop_height = _prepare_filter_request(
        node, conn, request.filter_type, 0, request.stop_hash, _NO_HEIGHT_DIFF_LIMIT
    )
    if stop_height is None:
        return
    block_index = node.chainstate.block_index
    filter_index = node.chainstate.filter_index
    # BIP157: "FilterHeaders MUST have exactly one entry for each block
    # on the chain terminating in StopHash, where the block height is a
    # multiple of 1,000 greater than 0" -- so the range starts at the
    # interval and not at zero, and the stop block is an entry when its
    # own height falls on one. No bound: the chain's length is the
    # bound, which is BIP157's answer too. Resolved along the stop
    # block's own ancestry, as `_filter_range` resolves a range for the
    # other two requests, and for the same reason (ISS 1476).
    checkpoints = []
    for height in range(CFCHECKPT_INTERVAL, stop_height + 1, CFCHECKPT_INTERVAL):
        block_hash = _ancestor(block_index, request.stop_hash, height)
        header = filter_index.get_header(block_hash)
        if header is None:
            err_msg = "no filter header for a block this node has indexed: "
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


def getblocktxn(node: Node, msg: bytes, conn: Connection) -> None:
    """Answer the transactions of a block a peer's `cmpctblock` lacked.

    Core's `GETBLOCKTXN` handling (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): silence for a block not
    held; a `blocktxn` for `node.most_recent_block`, ahead of any lookup,
    or for one at most `MAX_BLOCKTXN_DEPTH` below the tip, an index past
    its last transaction being misbehaviour; and for an older one the
    full block, queued as a `MSG_WITNESS_BLOCK` item of this connection's
    own `getdata`, whose serving pays for the disk read the request cost.

    An empty `indexes` is dropped, undiscouraged, ahead of any lookup:
    Core commit 28641fd195db2a175fd43fee2e32758aef9816a6 ("p2p: reject
    empty getblocktxn requests"), on master and not yet in a release,
    at bitcoin/bitcoin@28641fd195db -- "No legitimate reason to send
    indexes empty" -- sets `fDisconnect` rather than calling
    `Misbehaving`, so this raises no `MisbehavingError` and instead
    drops the peer directly, matching `tx`'s own block-relay-only
    refusal above. v31.1, at bitcoin/bitcoin@9be056a8a7, answers an
    empty request the same as any other and keeps the peer, as this
    node did before this check.
    """
    request = GetBlockTxn.parse(msg)
    if not request.indexes:
        node.logger.log_debug(
            "net", "getblocktxn received with no transaction indexes, peer=%s", conn.id
        )
        conn.stop()
        return
    recent = node.most_recent_block
    if recent is not None and recent.hash == request.block_hash:
        # Core answers from `m_most_recent_block` ahead of the lookups below
        _send_block_transactions(conn, request, recent.block)
        return
    block_index = node.chainstate.block_index
    block_info = block_index.header_dict.get(request.block_hash)
    if block_info is None:
        return
    # Core's `!(pindex->nStatus & BLOCK_HAVE_DATA)` return, ahead of any
    # depth: a block never downloaded or pruned away is not queued for the
    # `getdata` below, whose prune threshold would drop the connection
    if not node.block_db.has_block(request.block_hash):
        return
    if block_info.index < len(block_index.active_chain) - 1 - MAX_BLOCKTXN_DEPTH:
        item = Inventory(InventoryType.MSG_WITNESS_BLOCK, request.block_hash)
        getdata(node, GetData([item]).serialize(), conn)
        return
    block = node.block_db.get_block(request.block_hash)
    if block is None:
        return
    _send_block_transactions(conn, request, block)


def _send_block_transactions(
    conn: Connection, request: GetBlockTxn, block: Block
) -> None:
    """Answer `request` from `block`, Core's `SendBlockTransactions`."""
    transactions = block.transactions
    if any(index >= len(transactions) for index in request.indexes):
        err_msg = "getblocktxn with out-of-bounds tx indices"
        raise MisbehavingError(err_msg)
    answer = [transactions[index] for index in request.indexes]
    conn.send(BlockTxn(request.block_hash, answer))


def _ask_full_block(conn: Connection, block_hash: bytes) -> None:
    """Ask `conn` for `block_hash` as a block, leaving the request as it is.

    Core's `getdata` of `MSG_BLOCK | GetFetchFlags(peer)`, sent where a
    compact block cannot be used, without calling `BlockRequested`.
    """
    # deferred: `download` imports this module
    from btclib_node.download import block_inventory_type  # noqa: PLC0415

    conn.send(GetData([Inventory(block_inventory_type(conn), block_hash)]))


def _reconstruct(node: Node, compact: CmpctBlock) -> PartialBlock | None:
    """Rebuild what `compact` can from the mempool and the extra transactions.

    Core's `PartiallyDownloadedBlock::InitData` (`blockencodings.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which reads the mempool
    and then `DownloadManager.extra_txns`. `None` for a short-id
    collision, Core's `READ_STATUS_FAILED`.

    `InitData`'s `READ_STATUS_INVALID` is a `MisbehavingError` here,
    asked before `reconstruct`: no transaction, a prefilled transaction
    with neither input nor output, or a prefilled index past the short
    ids. Its bound of 100000 transactions is left out: `cmpctblock` has
    refused more than 65535 already. A prefilled index past 65535 is
    INVALID too, but btclib's parse refuses it first, and the peer is
    kept (btclib-org/btclib#2572). What `reconstruct` still refuses with
    `BTClibValueError` is then the short-id collision. Once a btclib
    release carries `ShortIdCollisionError` (btclib-org/btclib#2570),
    this catches that instead.

    The short ids of the whole pool are hashed on `Node`'s thread, as
    ARCHITECTURE.md says.
    """
    short_ids = len(compact.short_ids)
    if not compact.tx_count or any(
        not (prefilled_tx.tx.vin or prefilled_tx.tx.vout)
        or prefilled_tx.index > short_ids + i
        for i, prefilled_tx in enumerate(compact.prefilled_txns)
    ):
        err_msg = "invalid compact block"
        raise MisbehavingError(err_msg)
    pool = [*node.mempool.transactions.values(), *node.download_manager.extra_txns]
    try:
        return reconstruct(compact, pool)
    except BTClibValueError:
        return None


def _prefilled_only(compact: CmpctBlock) -> PartialBlock:
    """Return `compact` with only its prefilled transactions in place.

    What Core's `InitData` leaves in a `PartiallyDownloadedBlock` it
    answers `READ_STATUS_FAILED` for, which a later `blocktxn` fills.
    """
    transactions: list[Tx | None] = [None] * compact.tx_count
    for prefilled in compact.prefilled_txns:
        transactions[prefilled.index] = prefilled.tx
    return PartialBlock(compact.header, transactions, check_validity=False)


def _segwit_after_parent(node: Node, header: BlockHeader) -> bool:
    """Whether segwit binds a block on `header`'s parent, which is indexed."""
    parent = node.chainstate.block_index.header_dict[header.previous_block_hash]
    return parent.index + 1 >= node.chain.consensus.segwit_height


def cmpctblock(node: Node, msg: bytes, conn: Connection) -> None:
    """Rebuild a block from a `cmpctblock`, and ask for what it lacks.

    Core's `CMPCTBLOCK` handler (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Its header is taken
    first (`_index_compact_header`), and the block goes further only
    where it is not held, has more work than the tip, and is in flight
    or the tip is recent.

    At most two blocks past the tip, the block is queued from this peer
    where it is asked of fewer than `MAX_CMPCTBLOCKS_INFLIGHT_PER_BLOCK`
    peers and this one has room, or it is queued from this peer already
    (`_queue_compact_block`). Otherwise it is rebuilt all the same, and
    taken where nothing is missing (`_reconstruct_optimistically`).
    Further past the tip, a block queued from this peer is asked for as a
    block, and one not asked of this peer is read as a header.

    The message is parsed unchecked, as Core's deserializer reads it:
    `add_headers` checks the header and `_reconstruct` the rest. More
    than 65535 transactions is refused here and the peer kept, as Core's
    deserializer throws "indexes overflowed 16 bits".
    """
    # deferred: `download` imports this module
    from btclib_node.download import MAX_BLOCKS_IN_TRANSIT_PER_PEER  # noqa: PLC0415

    compact = CmpctBlock.parse(msg, check_validity=False)
    if compact.tx_count > MAX_BLOCK_TX_INDEX:
        err_msg = "indexes overflowed 16 bits"
        raise BTClibValueError(err_msg)
    header = compact.header
    block_hash = header.hash
    if not _index_compact_header(node, conn, header):
        return
    block_index = node.chainstate.block_index
    chainwork = block_index.chainwork
    holders = in_flight_from(node.p2p_manager.connections.copy().values(), block_hash)
    requested = block_hash in conn.download_queue
    # Core also asks this of a block it held and pruned, which is
    # `MIN_BLOCKS_TO_KEEP` below the tip and so has less work
    if chainwork[block_hash] <= chainwork[block_index.active_chain[-1]]:
        if requested:
            _ask_full_block(conn, block_hash)
        return
    if not holders and not _can_direct_fetch(node):
        return
    if block_index.get_block_info(block_hash).index > len(block_index.active_chain) + 1:
        if requested:
            _ask_full_block(conn, block_hash)
        else:
            _process_headers(node, conn, [header], via_compact_block=True)
    elif requested or (
        len(holders) < MAX_CMPCTBLOCKS_INFLIGHT_PER_BLOCK
        and len(conn.download_queue) < MAX_BLOCKS_IN_TRANSIT_PER_PEER
    ):
        _queue_compact_block(node, conn, compact, holders)
    else:
        _reconstruct_optimistically(node, compact, conn)


def _index_compact_header(node: Node, conn: Connection, header: BlockHeader) -> bool:
    """Take a `cmpctblock`'s header, and answer whether its block is wanted.

    A header on a parent not indexed asks for headers, out of initial
    block download, and one below the anti-DoS work threshold is
    ignored. The header is indexed otherwise, a header already marked
    invalid costing the peer nothing, and the peer has the block. The
    block is wanted where it is not held.
    """
    block_hash = header.hash
    block_index = node.chainstate.block_index
    if header.previous_block_hash not in block_index.header_dict:
        # "Doesn't connect (or is genesis)", in Core's words
        if not node.is_initial_block_download:
            maybe_send_getheaders(node, conn, block_index.get_block_locator_hashes())
        return False
    if not _min_pow_checked(node, header):
        node.logger.log_debug(
            "net", "Ignoring low-work compact block from peer %d", conn.id
        )
        return False
    received_new_header = block_hash not in block_index.header_dict
    block_index.add_headers([header], punish_cached_invalid=False)
    update_block_availability(block_index, conn.block_availability, block_hash)
    chainwork = block_index.chainwork
    if (
        received_new_header
        and chainwork[block_hash] > chainwork[block_index.active_chain[-1]]
    ):
        conn.last_block_announcement = int(time.time())
    return not block_index.get_block_info(block_hash).downloaded


def _queue_compact_block(
    node: Node, conn: Connection, compact: CmpctBlock, holders: list[Connection]
) -> None:
    """Queue a block from `conn`, rebuild it, and ask for what it lacks.

    `holders` are the peers it was asked of before, the first asked
    first. Nothing for a block whose partial block `conn` holds already.
    Rebuilt whole, the block is taken (`_process_compact_block_txns`);
    short, its missing transactions are asked for where `conn` was asked
    first, or is a high-bandwidth peer that may take one more slot. A
    collision asks for the block instead, where `conn` was asked first.
    Otherwise the block is no longer asked of `conn`.
    """
    block_hash = compact.header.hash
    first_in_flight = not holders or holders[0] is conn
    partials = conn.block_availability.partial_blocks
    if block_hash in conn.download_queue:
        if block_hash in partials:
            node.logger.log_debug(
                "net", "Peer sent us compact block we were already syncing!"
            )
            return
    else:
        # Core's `BlockRequested`
        if not conn.download_queue:
            conn.block_availability.downloading_since = time.time()
        conn.download_queue.append(block_hash)
        order = next(node.download_manager.request_orders)
        conn.block_availability.request_order[block_hash] = order
        node.warm_worker_pool()
    connections = list(node.p2p_manager.connections.values())
    try:
        partial = _reconstruct(node, compact)
    except MisbehavingError:
        remove_block_request(connections, block_hash, time.time(), conn.id)
        raise
    if partial is None:
        if first_in_flight:
            partials[block_hash] = _prefilled_only(compact)
            _ask_full_block(conn, block_hash)
        else:
            remove_block_request(connections, block_hash, time.time(), conn.id)
        return
    partials[block_hash] = partial
    missing = partial.missing_indexes
    if not missing:
        _process_compact_block_txns(node, conn, block_hash, ())
    elif first_in_flight or (
        conn.bip152_highbandwidth_to
        and (
            not conn.inbound
            or any(not holder.inbound for holder in holders)
            or len(holders) < MAX_CMPCTBLOCKS_INFLIGHT_PER_BLOCK - 1
        )
    ):
        conn.send(GetBlockTxn(block_hash, missing))
    else:
        remove_block_request(connections, block_hash, time.time(), conn.id)


def _reconstruct_optimistically(
    node: Node, compact: CmpctBlock, conn: Connection
) -> None:
    """Take a block in flight from other peers if nothing of it is missing.

    Core's "optimistic" reconstruction in its `CMPCTBLOCK` handler: a
    failure is ignored, and a block rebuilt whole is processed as asked
    for.
    """
    try:
        partial = _reconstruct(node, compact)
    except MisbehavingError:
        return
    if partial is None or partial.missing_indexes:
        return
    block = partial.fill((), check_validity=False)
    segwit = _segwit_after_parent(node, block.header)
    if is_block_mutated(block, check_witness_root=segwit):
        return
    _check_block(node, block, conn, via_compact_block=True)
    _accept_block(node, block, conn, requested=True, via_compact_block=True)


def blocktxn(node: Node, msg: bytes, conn: Connection) -> None:
    """Finish the block a `cmpctblock` from this peer left short.

    Core's `BLOCKTXN` handler (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). The transactions are read
    unchecked, as Core's deserializer reads them: one that is not valid
    leaves a block `_check_block` refuses.
    """
    answer = BlockTxn.parse(msg, check_validity=False)
    _process_compact_block_txns(node, conn, answer.block_hash, answer.transactions)


def _process_compact_block_txns(
    node: Node, conn: Connection, block_hash: bytes, transactions: Sequence[Tx]
) -> None:
    """Fill the partial block `conn` is rebuilding, and take the block.

    Core's `ProcessCompactBlockTxns` (`net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Ignored for a block with
    no partial block from this peer, and a `MisbehavingError` for one
    already filled, or for transactions other than those missing. A block
    its header does not commit to may be a short-id collision: it is
    asked for as a block where this peer was asked first, and otherwise
    left to the others. A block that holds up is taken as one asked for,
    its failures costing the peer nothing.
    """
    connections = list(node.p2p_manager.connections.values())
    holders = in_flight_from(connections, block_hash)
    first_in_flight = not holders or holders[0] is conn
    partials = conn.block_availability.partial_blocks
    if block_hash not in conn.download_queue or block_hash not in partials:
        node.logger.log_debug(
            "net",
            "Peer %d sent us block transactions for block we weren't expecting",
            conn.id,
        )
        return
    partial = partials[block_hash]
    if partial is None:
        remove_block_request(connections, block_hash, time.time(), conn.id)
        err_msg = "previous compact block reconstruction attempt failed"
        raise MisbehavingError(err_msg)
    try:
        block = partial.fill(transactions, check_validity=False)
    except BTClibValueError as e:
        remove_block_request(connections, block_hash, time.time(), conn.id)
        err_msg = "invalid compact block/non-matching block transactions"
        raise MisbehavingError(err_msg) from e
    # Core's `FillBlock` nulls the header, so that it is not filled twice
    partials[block_hash] = None
    segwit = _segwit_after_parent(node, block.header)
    if is_block_mutated(block, check_witness_root=segwit):
        # "Possible Short ID collision", in Core's words
        if first_in_flight:
            _ask_full_block(conn, block_hash)
        else:
            remove_block_request(connections, block_hash, time.time(), conn.id)
        return
    remove_block_request(connections, block_hash, time.time(), conn.id)
    _check_block(node, block, conn, via_compact_block=True)
    _accept_block(node, block, conn, requested=True, via_compact_block=True)


def not_found(node: Node, msg: bytes, conn: Connection) -> None:
    """Complete the announcements of the transactions the peer could not answer.

    A block item carries no such bookkeeping to complete -- the comment
    below argues why.
    """
    missing = NotFound.parse(msg)
    # `TxDownloadManagerImpl::ReceivedNotFound`, net_processing.cpp
    # (at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a `notfound` for a
    # transaction this node asked for completes that announcement, so the
    # next announcer is asked instead of the node waiting out the request.
    # `DownloadManager.received_not_found` reads only the transaction
    # items, as Core's `IsGenTxMsg` does: `MSG_BLOCK` was never requested
    # through a mechanism a `notfound` could complete.
    # btclib-org/btclib-node#144
    node.download_manager.received_not_found(conn.id, missing.items)
    # A count at debug rather than the items: Core's one line for a
    # `notfound` is ProcessMessage's own `received: notfound (N bytes)`,
    # under `-debug=net` (net_processing.cpp, at bitcoin/bitcoin@9be056a8a7),
    # and the items are the peer's to size.
    node.logger.log_debug("net", "notfound of %d items", len(missing.items))


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
    "getblocktxn": getblocktxn,
    "cmpctblock": cmpctblock,
    "blocktxn": blocktxn,
    "getblocks": getblocks,
    "getheaders": getheaders,
    "headers": headers,
    "addr": addr,
    "addrv2": addrv2,
    "getaddr": getaddr,
    "sendheaders": sendheaders,
    "sendcmpct": sendcmpct,
    "getcfilters": get_cfilters,
    "getcfheaders": get_cfheaders,
    "getcfcheckpt": get_cfcheckpt,
    "notfound": not_found,
    "feefilter": feefilter,
}
