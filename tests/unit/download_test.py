# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Who gets asked for what, and who gets told.

The functional download test drives one node against one peer with
every block to fetch, which is the path where a single queue and a
single answer are indistinguishable from the right ones. What is left
is the arithmetic between peers: who is told about a transaction, who is
asked for it, which peer a block is fetched from, and when a peer that
has stopped sending blocks is let go.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, TypeVar, cast

import pytest
from btclib.fee import FeeRate, fee_from_vsize
from btclib.p2p.inventory import GetData, Inv
from btclib.p2p.limits import MAX_INV_SZ
from btclib.p2p.negotiation import FeeFilter

import btclib_node.download as download_module
from btclib_node.config import DEFAULT_MIN_RELAY_FEERATE
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.download import DownloadManager
from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from btclib_node.p2p.address import peer_address
from tests import generate_random_transaction

if TYPE_CHECKING:
    from collections.abc import Sequence

    from btclib.p2p.addrv2 import NetworkAddressV2

    from btclib_node import Node


def a_hash(n: int) -> bytes:
    """Build a distinct, deterministic 32-byte hash from a small integer."""
    return n.to_bytes(32, "big")


def a_conn(
    conn_id: int,
    *,
    queue: list[bytes] | None = None,
    last_block: float | None = None,
    relay_tx: bool = True,
    feefilter: int = 0,
    inbound: bool = True,
    pending_eviction: bool = False,
    address: NetworkAddressV2 | None = None,
    status: Any = P2pConnStatus.Connected,
    feefilter_sent: int = 0,
    next_feefilter_send_time: float = 0.0,
) -> Any:
    """Build a fake connection, recording every message handed to `send`."""
    sent: list[Any] = []
    return SimpleNamespace(
        id=conn_id,
        send=sent.append,
        sent=sent,
        relay_tx=relay_tx,
        feefilter=feefilter,
        inbound=inbound,
        address=address if address is not None else peer_address("10.0.0.1", 8333),
        download_queue=queue if queue is not None else [],
        pending_eviction=pending_eviction,
        last_block_timestamp=time.time() if last_block is None else last_block,
        stop=lambda: None,
        tx_announce_queue=[],
        next_inv_send_time=0.0,
        tx_requested={},
        status=status,
        feefilter_sent=feefilter_sent,
        next_feefilter_send_time=next_feefilter_send_time,
    )


def make_manager(
    conns: list[Any],
    *,
    status: NodeStatus = NodeStatus.BlockSynced,
    block_index: Any | None = None,
    mempool: Any | None = None,
    warm_worker_pool: Any = None,
    min_relay_feerate: FeeRate = DEFAULT_MIN_RELAY_FEERATE,
) -> DownloadManager:
    """Build a `DownloadManager` over a fake node with the given connections."""
    node = SimpleNamespace(
        status=status,
        p2p_manager=SimpleNamespace(connections={conn.id: conn for conn in conns}),
        chainstate=SimpleNamespace(block_index=block_index),
        mempool=mempool if mempool is not None else Mempool(Logger(debug=True)),
        warm_worker_pool=warm_worker_pool or (lambda: None),
        config=SimpleNamespace(min_relay_feerate=min_relay_feerate),
    )
    return DownloadManager(cast("Node", node), Logger(debug=True))


def hold(manager: DownloadManager, *wtxids: bytes) -> None:
    """Make each wtxid a member of the manager's own mempool, minimally.

    `tx_download` and `_send_due_announcements` now check
    `Mempool.transactions` membership before queuing or sending an
    announcement (btclib-org/btclib-node#294), so a synthetic wtxid this
    file builds with `a_hash` needs an entry there too, or it is read as
    already evicted -- the same guard that stops a real eviction from
    being announced. What is stored under it does not matter to that
    check, only that the key is present, so this skips `Mempool.add_tx`
    and its own `tx.hash == wtxid` invariant rather than manufacturing a
    transaction that hashes to a chosen 32 bytes.
    """
    for wtxid in wtxids:
        cast("Any", manager.node).mempool.transactions[wtxid] = (
            generate_random_transaction()
        )


def hashes_of(message: GetData | Inv) -> list[bytes]:
    """Return the hash carried by each inventory item of `message`."""
    return [item.hash for item in message.items]


# the message type `only` filters `conn.sent` down to
_M = TypeVar("_M")


def only(conn: Any, kind: type[_M]) -> list[_M]:
    """Return the messages of `conn.sent` that are instances of `kind`."""
    return [message for message in conn.sent if isinstance(message, kind)]


def test_a_step_asks_for_neither_kind_while_the_headers_are_syncing() -> None:
    """`step` sends a `feefilter` during header sync, but no `GetData`/`Inv`."""
    # _send_due_feefilters still runs while syncing -- Core's own
    # MaybeSendFeefilter tells a peer MAX_MONEY during IBD rather than
    # skip the send outright, so a step() while headers are syncing is
    # not silent, only silent of GetData/Inv. btclib-org/btclib-node#275
    conn = a_conn(1)
    manager = make_manager([conn], status=NodeStatus.SyncingHeaders)
    manager.inv_txs = [(1, a_hash(1))]
    manager.step()
    assert not only(conn, GetData)
    assert not only(conn, Inv)
    (feefilter_msg,) = only(conn, FeeFilter)
    assert feefilter_msg.feerate == manager._max_feefilter


def test_a_transaction_a_peer_announced_is_asked_of_that_peer() -> None:
    """`tx_download` sends `GetData` to the peer whose `inv` named the wtxid."""
    first, second = a_conn(1), a_conn(2)
    manager = make_manager([first, second])
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    (getdata,) = only(first, GetData)
    assert hashes_of(getdata) == [a_hash(1)]
    assert not second.sent


def test_a_peer_that_announced_the_same_transaction_twice_is_asked_once() -> None:
    """Two `inv`s from the same peer for one wtxid produce one `GetData` ask."""
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1)), (1, a_hash(1))]
    manager.tx_download()
    (getdata,) = only(conn, GetData)
    assert hashes_of(getdata) == [a_hash(1)]


def test_an_announcement_from_a_peer_that_is_gone_asks_nobody() -> None:
    """An `inv` from a connection id no longer in `connections` is dropped."""
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(99, a_hash(1))]
    manager.tx_download()
    assert not conn.sent


def test_the_peer_that_sent_a_transaction_is_not_asked_for_it_again() -> None:
    """The peer that already delivered a wtxid is not sent `GetData` for it."""
    # it is in the mempool now: asking its source for it is a round trip
    # and a second copy of something we already hold
    sender = a_conn(1)
    manager = make_manager([sender])
    manager.inv_txs = [(1, a_hash(1))]
    manager.received_txs = [(1, a_hash(1))]
    manager.tx_download()
    assert not only(sender, GetData)


def test_a_single_transaction_is_announced_rather_than_held_back() -> None:
    """A single received transaction is still announced, not batched away."""
    # one is a whole step's worth of transactions on a quiet network,
    # and the lists are emptied at the end of the step: a batch size
    # held against them is not a throttle but a filter on whether a
    # transaction is ever announced at all
    sender, other = a_conn(1), a_conn(2)
    manager = make_manager([sender, other])
    hold(manager, a_hash(1))
    manager.received_txs = [(1, a_hash(1))]
    manager.tx_download()
    (inv,) = only(other, Inv)
    assert hashes_of(inv) == [a_hash(1)]
    assert not only(sender, Inv)


def test_a_peer_that_already_has_all_of_them_is_told_nothing() -> None:
    """A peer that sent every wtxid gets no `inv`, not an empty one."""
    # and not told with an empty inv, which is a message with nothing in
    # it for the peer to do
    sender = a_conn(1)
    manager = make_manager([sender])
    manager.received_txs = [(1, a_hash(n)) for n in range(1, 4)]
    manager.tx_download()
    assert not sender.sent


def test_a_peer_that_asked_for_no_transactions_is_sent_none() -> None:
    """A peer with `relay_tx` false gets no `Inv`, unlike one that wants it."""
    # BIP37's fRelay, which the version callback puts on the connection:
    # a peer that declined is not sent a shorter inv, it is not sent one
    # at all. With a peer that did ask, so the assertion is about the
    # flag rather than about a step that announced to nobody.
    sender, declined, wants = a_conn(1), a_conn(2, relay_tx=False), a_conn(3)
    manager = make_manager([sender, declined, wants])
    hold(manager, a_hash(1))
    manager.received_txs = [(1, a_hash(1))]
    manager.tx_download()
    assert not only(declined, Inv)
    (inv,) = only(wants, Inv)
    assert hashes_of(inv) == [a_hash(1)]


def test_a_peer_that_declined_relay_is_still_answered_about_what_it_wants() -> None:
    """`relay_tx` false withholds unsolicited sends, not answers to `inv`."""
    # declining transactions is about what it is sent unasked; a peer
    # that announced one is still asked for it
    declined = a_conn(1, relay_tx=False)
    manager = make_manager([declined])
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    (getdata,) = only(declined, GetData)
    assert hashes_of(getdata) == [a_hash(1)]


def test_a_transaction_is_announced_to_the_peers_that_do_not_have_it() -> None:
    """Only the peer that neither sent nor announced a wtxid is told of it."""
    sender, announcer, other = a_conn(1), a_conn(2), a_conn(3)
    manager = make_manager([sender, announcer, other])
    received = [a_hash(n) for n in range(1, 7)]
    hold(manager, *received)
    manager.received_txs = [(1, wtxid) for wtxid in received]
    # peer 2 announced them, so it has them; peer 1 sent them
    manager.inv_txs = [(2, wtxid) for wtxid in received]
    manager.tx_download()
    assert not only(sender, Inv)
    assert not only(announcer, Inv)
    (inv,) = only(other, Inv)
    assert hashes_of(inv) == received


def test_a_peer_s_feefilter_withholds_a_transaction_below_its_rate() -> None:
    """A transaction paying below a peer's `feefilter` is never queued to it."""
    # BIP133: `wants` asked for nothing below 1000 sat/kvB, and this
    # transaction pays less
    sender, wants = a_conn(1), a_conn(2, feefilter=1000)
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, 1)
    manager = make_manager([sender, wants], mempool=mempool)
    manager.received_txs = [(1, tx.hash)]
    manager.tx_download()
    assert not only(wants, Inv)


def test_a_peer_s_feefilter_still_announces_a_transaction_at_its_rate() -> None:
    """A transaction exactly at a peer's `feefilter` is still queued to it."""
    sender, wants = a_conn(1), a_conn(2, feefilter=1000)
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, fee_from_vsize(tx.vsize, FeeRate(sats_per_kvbyte=1000)))
    manager = make_manager([sender, wants], mempool=mempool)
    manager.received_txs = [(1, tx.hash)]
    manager.tx_download()
    (inv,) = only(wants, Inv)
    assert hashes_of(inv) == [tx.hash]


def test_a_wtxid_the_mempool_does_not_hold_is_never_announced() -> None:
    """A wtxid evicted since being received is not announced, mempool absent."""
    # `received_txs` names a wtxid `Mempool.add_tx` accepted at the time
    # it was queued, but eviction (`Mempool._evict_to_limit`) can have
    # taken it back out before `tx_download` next runs -- announcing it
    # regardless would be #277's own defect reached through eviction
    # rather than a full mempool's outright refusal.
    # btclib-org/btclib-node#294
    sender, wants = a_conn(1), a_conn(2)
    manager = make_manager([sender, wants])
    manager.received_txs = [(1, a_hash(1))]
    manager.tx_download()
    assert not only(wants, Inv)


def test_a_peer_still_wanting_something_else_is_asked_for_it() -> None:
    """A received wtxid drops from `GetData`; another is still asked for."""
    sender, wanter = a_conn(1), a_conn(2)
    manager = make_manager([sender, wanter])
    manager.received_txs = [(1, a_hash(1))]
    manager.inv_txs = [(1, a_hash(1)), (2, a_hash(2))]
    manager.tx_download()
    (getdata,) = only(wanter, GetData)
    assert hashes_of(getdata) == [a_hash(2)]
    assert not only(sender, GetData)


def test_a_queue_past_max_inv_sz_is_sent_as_several_invs() -> None:
    """A queue one over `MAX_INV_SZ` is sent as a full `Inv` plus one more."""
    # Inv.assert_valid (btclib.p2p.inventory) refuses more than
    # MAX_INV_SZ entries in one message. btclib-org/btclib-node#282
    other = a_conn(1)
    manager = make_manager([other])
    other.tx_announce_queue = [a_hash(n) for n in range(MAX_INV_SZ + 1)]
    hold(manager, *other.tx_announce_queue)
    manager._send_due_announcements()
    first, second = only(other, Inv)
    assert len(first.items) == MAX_INV_SZ
    assert len(second.items) == 1
    assert hashes_of(second) == [a_hash(MAX_INV_SZ)]
    assert other.tx_announce_queue == []


def test_a_queue_at_exactly_max_inv_sz_is_sent_as_one_inv() -> None:
    """A queue exactly `MAX_INV_SZ` long still fits in a single `Inv`."""
    other = a_conn(1)
    manager = make_manager([other])
    other.tx_announce_queue = [a_hash(n) for n in range(MAX_INV_SZ)]
    hold(manager, *other.tx_announce_queue)
    manager._send_due_announcements()
    (only_inv,) = only(other, Inv)
    assert len(only_inv.items) == MAX_INV_SZ


def test_a_queued_announcement_evicted_before_its_own_schedule_is_not_sent() -> None:
    """A queued wtxid evicted before its send time is filtered out at send."""
    # _send_due_announcements filters conn.tx_announce_queue against
    # current mempool membership at send time, not only at queue time --
    # a wtxid can sit queued for this connection's whole schedule, long
    # enough for a later eviction to take it back out before it is ever
    # sent. btclib-org/btclib-node#294
    other = a_conn(1)
    manager = make_manager([other])
    other.tx_announce_queue = [a_hash(1), a_hash(2)]
    hold(manager, a_hash(2))  # a_hash(1) evicted since it was queued
    manager._send_due_announcements()
    (inv,) = only(other, Inv)
    assert hashes_of(inv) == [a_hash(2)]
    assert other.tx_announce_queue == []


def test_a_queue_left_with_nothing_still_held_sends_no_inv() -> None:
    """A queue whose only entry was evicted sends no `Inv`, only clears."""
    other = a_conn(1)
    manager = make_manager([other])
    other.tx_announce_queue = [a_hash(1)]
    manager._send_due_announcements()
    assert not only(other, Inv)
    assert other.tx_announce_queue == []


def test_a_second_announcement_waits_for_the_peers_own_schedule() -> None:
    """A first send fires at once; a second waits for the peer's own timer."""
    # the first ever announcement to a fresh connection fires at once --
    # its schedule reads 0, "never scheduled", which the due-check
    # always treats as due -- but once a schedule is set, a transaction
    # arriving before it comes due is queued rather than sent straight
    # away, which is the change #141 is about
    other = a_conn(1)
    manager = make_manager([other])
    hold(manager, a_hash(1), a_hash(2))
    manager.received_txs = [(2, a_hash(1))]
    manager.tx_download()
    (first,) = only(other, Inv)
    assert hashes_of(first) == [a_hash(1)]
    assert other.next_inv_send_time > time.time()

    manager.received_txs = [(2, a_hash(2))]
    manager.tx_download()
    assert len(only(other, Inv)) == 1
    assert other.tx_announce_queue == [a_hash(2)]


def test_a_wtxid_already_queued_for_a_peer_is_not_queued_twice() -> None:
    """A wtxid received twice before a peer's schedule fires is queued once."""
    other = a_conn(1)
    manager = make_manager([other])
    hold(manager, a_hash(1), a_hash(2))
    # sent at once, the first ever call to a fresh connection's schedule
    # always being due, so the queue below starts from empty
    manager.received_txs = [(2, a_hash(1))]
    manager.tx_download()
    other.next_inv_send_time = time.time() + 60

    manager.received_txs = [(2, a_hash(2))]
    manager.tx_download()
    manager.received_txs = [(2, a_hash(2))]
    manager.tx_download()
    assert other.tx_announce_queue == [a_hash(2)]


def test_a_queued_announcement_is_sent_once_its_own_schedule_is_due() -> None:
    """A queued wtxid is sent once the peer's `next_inv_send_time` passes."""
    other = a_conn(1)
    manager = make_manager([other])
    hold(manager, a_hash(1), a_hash(2))
    manager.received_txs = [(2, a_hash(1))]
    manager.tx_download()
    other.next_inv_send_time = time.time() - 1

    manager.received_txs = [(2, a_hash(2))]
    manager.tx_download()
    first, second = only(other, Inv)
    assert hashes_of(first) == [a_hash(1)]
    assert hashes_of(second) == [a_hash(2)]


def test_an_outbound_peers_schedule_draws_from_the_shorter_mean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outbound peer draws its schedule from the shorter of the two means."""
    # net_processing.cpp's OUTBOUND_INVENTORY_BROADCAST_INTERVAL/
    # INBOUND_INVENTORY_BROADCAST_INTERVAL, at bitcoin/bitcoin@58a7869f86:
    # an outbound connection's mean is the shorter of the two
    means: list[float] = []

    def record_mean(lambd: float) -> float:
        means.append(1 / lambd)
        return 0.0

    monkeypatch.setattr(download_module._rng, "expovariate", record_mean)
    inbound, outbound = a_conn(1, inbound=True), a_conn(2, inbound=False)
    manager = make_manager([inbound, outbound])
    manager.tx_download()
    assert means == [
        download_module._INBOUND_TX_ANNOUNCE_INTERVAL,
        download_module._OUTBOUND_TX_ANNOUNCE_INTERVAL,
    ]


def test_two_inbound_ipv4_peers_share_one_schedule_regardless_of_subnet() -> None:
    """Two inbound IPv4 peers, different /16s, share one send-time draw."""
    # `CNode::m_network_key` (net.h:755, at bitcoin/bitcoin@58a7869f86) is
    # keyed on the peer's coarse `GetNetClass()` and this node's own bind
    # address, not on anything of the peer's own subnet -- so two inbound
    # IPv4 peers share `NextInvToInbounds`'s one draw even from two
    # different /16s, and a peer opening several inbound connections
    # cannot average several independent draws down to a receipt time
    # finer than one connection's own jitter would allow.
    first = a_conn(1, address=peer_address("10.0.1.1", 8333))
    second = a_conn(2, address=peer_address("11.0.1.1", 8333))
    manager = make_manager([first, second])
    manager.tx_download()
    assert first.next_inv_send_time == second.next_inv_send_time
    assert first.next_inv_send_time > time.time()


def test_two_inbound_ipv6_peers_share_one_schedule_regardless_of_subnet() -> None:
    """Two inbound IPv6 peers, different subnets, share one send-time draw."""
    first = a_conn(1, address=peer_address("2001:db8::1", 8333))
    second = a_conn(2, address=peer_address("2002:db8::1", 8333))
    manager = make_manager([first, second])
    manager.tx_download()
    assert first.next_inv_send_time == second.next_inv_send_time


def test_an_inbound_ipv4_peer_and_an_inbound_ipv6_peer_can_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two address families are keyed, and drawn, separately."""
    draws = iter([1.0, 2.0])
    monkeypatch.setattr(download_module._rng, "expovariate", lambda lambd: next(draws))
    first = a_conn(1, address=peer_address("10.0.1.1", 8333))
    second = a_conn(2, address=peer_address("2001:db8::1", 8333))
    manager = make_manager([first, second])
    manager.tx_download()
    assert first.next_inv_send_time != second.next_inv_send_time


def test_a_transaction_this_node_originated_is_announced_like_any_other() -> None:
    """A locally originated transaction (`conn_id` `None`) is announced too."""
    # conn_id `None` is `P2pManager.broadcast_raw_transaction`'s own
    # marker for a transaction with no peer to exclude, going through
    # the same queue a relayed transaction does rather than a path of
    # its own
    other = a_conn(1)
    manager = make_manager([other])
    hold(manager, a_hash(1))
    manager.received_txs = [(None, a_hash(1))]
    manager.tx_download()
    (inv,) = only(other, Inv)
    assert hashes_of(inv) == [a_hash(1)]


def test_an_outstanding_ask_is_not_repeated_before_it_is_answered() -> None:
    """A second `inv` for an already-asked wtxid draws no second `GetData`."""
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    assert len(only(conn, GetData)) == 1


def test_a_notfound_response_frees_the_wtxid_to_be_asked_for_again() -> None:
    """Clearing `tx_requested`, as a `notfound` would, lets a re-ask happen."""
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    conn.tx_requested.pop(a_hash(1), None)

    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    assert len(only(conn, GetData)) == 2


def test_an_ask_still_within_the_timeout_is_not_repeated() -> None:
    """A wtxid asked for moments ago is not asked for again."""
    conn = a_conn(1)
    conn.tx_requested[a_hash(1)] = time.time()
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    assert not only(conn, GetData)


def test_an_ask_a_peer_never_answered_is_asked_again_once_it_expires() -> None:
    """Past `_TX_REQUEST_TIMEOUT`, an unanswered ask is re-sent, not stuck."""
    # a peer that neither sends the transaction nor answers `notfound`
    # otherwise blocks every future request to it for this wtxid,
    # permanently: btclib-org/btclib-node#289
    conn = a_conn(1)
    conn.tx_requested[a_hash(1)] = time.time() - download_module._TX_REQUEST_TIMEOUT - 1
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    (getdata,) = only(conn, GetData)
    assert hashes_of(getdata) == [a_hash(1)]


def test_an_expired_asks_entry_is_dropped_even_when_nothing_is_re_announced() -> None:
    """An expired `tx_requested` entry is purged even for an unrelated `inv`."""
    conn = a_conn(1)
    conn.tx_requested[a_hash(1)] = time.time() - download_module._TX_REQUEST_TIMEOUT - 1
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(2))]
    manager.tx_download()
    assert a_hash(1) not in conn.tx_requested


def test_receiving_a_transaction_frees_every_peers_outstanding_ask_for_it() -> None:
    """Receiving a wtxid clears `tx_requested` for a peer that never asked."""
    asked = a_conn(1)
    asked.tx_requested[a_hash(1)] = time.time()
    manager = make_manager([asked])
    manager.received_txs = [(2, a_hash(1))]
    manager.tx_download()
    assert a_hash(1) not in asked.tx_requested


def test_both_lists_are_emptied_by_a_step() -> None:
    """`tx_download` clears `inv_txs` and `received_txs` before it returns."""
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1))]
    manager.received_txs = [(1, a_hash(2))]
    manager.tx_download()
    assert manager.inv_txs == []
    assert manager.received_txs == []


def test_the_fee_filter_bucket_set_starts_at_zero_and_half_the_floor() -> None:
    """`_fee_filter_buckets` starts at zero, then half the configured floor."""
    buckets = download_module._fee_filter_buckets(100)
    assert buckets[0] == 0
    assert buckets[1] == 50


def test_the_fee_filter_bucket_set_never_drops_below_one_sat_per_kvb() -> None:
    """A floor of 0 or 1 still gets a real, non-zero second bucket."""
    # Core's own MakeFeeSet: max(CAmount(1), min_incremental_fee/2) --
    # a floor configured at 1 or 0 still gets a real second bucket
    buckets = download_module._fee_filter_buckets(1)
    assert buckets == [0, 1] or buckets[1] == 1


def test_the_fee_filter_bucket_set_is_sorted_and_caps_near_the_ceiling() -> None:
    """The bucket set is sorted, its top just under `_MAX_FILTER_FEERATE`."""
    buckets = download_module._fee_filter_buckets(100)
    assert buckets == sorted(buckets)
    assert buckets[-1] <= download_module._MAX_FILTER_FEERATE
    assert (
        buckets[-1]
        > download_module._MAX_FILTER_FEERATE / download_module._FEE_FILTER_SPACING
    )


def test_rounding_below_the_first_bucket_always_gives_zero() -> None:
    """`_round_fee_filter` of zero rounds to the bucket set's own zero."""
    buckets = download_module._fee_filter_buckets(100)
    assert download_module._round_fee_filter(0, buckets) == 0


def test_rounding_above_the_last_bucket_always_gives_the_top_one() -> None:
    """A rate far past the top bucket still rounds down to that top bucket."""
    buckets = download_module._fee_filter_buckets(100)
    top = int(buckets[-1])
    assert download_module._round_fee_filter(top * 10, buckets) == top


def test_rounding_truncates_the_selected_boundary_rather_than_the_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer boundary truncates only once it is picked."""
    # Core's own MakeFeeSet keeps every boundary as a raw double and
    # truncates only the value FeeFilterRounder::round finally selects
    # (static_cast<CAmount>) -- so a boundary this set built that is not
    # itself a whole number, floating-point arithmetic being what it is,
    # is not rounded to one until it is chosen
    buckets = download_module._fee_filter_buckets(100)
    assert not buckets[2].is_integer()

    monkeypatch.setattr(download_module._rng, "randrange", lambda _: 0)
    assert download_module._round_fee_filter(int(buckets[2]), buckets) == int(
        buckets[2]
    )


def test_rounding_at_a_bucket_boundary_can_go_either_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate exactly on a bucket can still round down, by the coin flip."""
    # FeeFilterRounder::round: a 2-in-3 draw rounds down to the bucket
    # below even when the rate lands exactly on one, so this node's own
    # rolling minimum is not readable exactly from what it tells a peer
    buckets = download_module._fee_filter_buckets(100)
    exact = int(buckets[2])

    monkeypatch.setattr(download_module._rng, "randrange", lambda _: 0)
    assert download_module._round_fee_filter(exact, buckets) == exact

    monkeypatch.setattr(download_module._rng, "randrange", lambda _: 1)
    assert download_module._round_fee_filter(exact, buckets) == int(buckets[1])


def test_a_connection_still_mid_handshake_is_sent_no_feefilter() -> None:
    """A connection not yet `Connected` is sent no `feefilter` at all."""
    conn = a_conn(1, status=P2pConnStatus.Open)
    manager = make_manager([conn])
    manager._send_due_feefilters()
    assert not only(conn, FeeFilter)


def test_a_fresh_connections_first_feefilter_is_sent_immediately() -> None:
    """A never-scheduled connection is sent `feefilter` on the first pass."""
    # next_feefilter_send_time defaults to 0.0, "never scheduled", the
    # same convention next_inv_send_time already uses
    conn = a_conn(1)
    manager = make_manager([conn], min_relay_feerate=FeeRate(sats_per_kvbyte=100))
    manager._send_due_feefilters()
    (sent,) = only(conn, FeeFilter)
    assert sent.feerate == 100  # the mempool's own rolling minimum is 0
    assert conn.feefilter_sent == 100
    assert conn.next_feefilter_send_time > time.time()


def test_a_connection_not_yet_due_is_sent_nothing_again() -> None:
    """A connection whose own schedule has not come due is sent nothing."""
    conn = a_conn(1, feefilter_sent=100, next_feefilter_send_time=time.time() + 1000)
    manager = make_manager([conn])
    manager._send_due_feefilters()
    assert not only(conn, FeeFilter)


def test_an_unchanged_rate_is_not_resent_once_its_own_schedule_comes_due() -> None:
    """An unchanged rate is not resent, but its own timer still moves on."""
    conn = a_conn(1, feefilter_sent=100, next_feefilter_send_time=0.0)
    manager = make_manager([conn], min_relay_feerate=FeeRate(sats_per_kvbyte=100))
    manager._send_due_feefilters()
    assert not only(conn, FeeFilter)
    # the schedule still moves even though nothing was sent
    assert conn.next_feefilter_send_time > time.time()


def test_the_floor_is_never_undercut_even_by_an_empty_mempools_own_zero() -> None:
    """The rolling minimum's zero never sends a filter below the floor."""
    conn = a_conn(1)
    manager = make_manager([conn], min_relay_feerate=FeeRate(sats_per_kvbyte=500))
    manager._send_due_feefilters()
    (sent,) = only(conn, FeeFilter)
    assert sent.feerate == 500


def test_ibd_sends_every_connected_peer_the_top_bucket() -> None:
    """While still syncing, every connected peer is sent the top fee bucket."""
    conn = a_conn(1)
    manager = make_manager([conn], status=NodeStatus.SyncingHeaders)
    manager._send_due_feefilters()
    (sent,) = only(conn, FeeFilter)
    assert sent.feerate == manager._max_feefilter


def test_leaving_ibd_forces_an_immediate_resend_off_the_top_bucket() -> None:
    """Leaving sync forces an immediate resend, off the stale top bucket."""
    conn = a_conn(1)
    manager = make_manager([conn], status=NodeStatus.SyncingHeaders)
    manager._send_due_feefilters()
    assert conn.feefilter_sent == manager._max_feefilter
    conn.sent.clear()
    conn.next_feefilter_send_time = time.time() + 1000  # its ordinary schedule

    manager.node.status = NodeStatus.BlockSynced
    manager._send_due_feefilters()
    (sent,) = only(conn, FeeFilter)
    assert sent.feerate != manager._max_feefilter


def test_a_large_enough_move_pulls_the_next_send_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate risen by over a third pulls the resend forward, unsent yet."""
    # currentFilter > 4 * peer.m_fee_filter_sent / 3: a rate that has
    # risen by more than a third is not left on the peer's own several-
    # minutes-average schedule
    mempool = Mempool(Logger(debug=True))
    mempool._rolling_min_fee_rate = 1000.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._last_rolling_fee_update = time.time()  # nothing decayed yet
    far_future = time.time() + download_module._AVG_FEEFILTER_BROADCAST_INTERVAL
    conn = a_conn(1, feefilter_sent=100, next_feefilter_send_time=far_future)
    manager = make_manager([conn], mempool=mempool)

    monkeypatch.setattr(download_module._rng, "uniform", lambda _lo, _hi: 1.0)
    manager._send_due_feefilters()
    assert not only(conn, FeeFilter)  # still not due, only rescheduled
    assert conn.next_feefilter_send_time == pytest.approx(time.time() + 1.0)


def test_a_small_move_does_not_pull_the_next_send_forward() -> None:
    """A move under the pull-forward threshold leaves the schedule untouched."""
    mempool = Mempool(Logger(debug=True))
    mempool._rolling_min_fee_rate = 110.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._last_rolling_fee_update = time.time()  # nothing decayed yet
    far_future = time.time() + download_module._AVG_FEEFILTER_BROADCAST_INTERVAL
    conn = a_conn(1, feefilter_sent=100, next_feefilter_send_time=far_future)
    manager = make_manager([conn], mempool=mempool)

    manager._send_due_feefilters()
    assert not only(conn, FeeFilter)
    assert conn.next_feefilter_send_time == far_future


def test_a_move_close_to_its_own_schedule_already_is_not_pulled_forward() -> None:
    """A schedule already due soon is left alone, however far the rate moved."""
    mempool = Mempool(Logger(debug=True))
    mempool._rolling_min_fee_rate = 1000.0
    mempool._block_since_last_rolling_fee_bump = True
    mempool._last_rolling_fee_update = time.time()  # nothing decayed yet
    soon = time.time() + 1.0  # inside MAX_FEEFILTER_CHANGE_DELAY already
    conn = a_conn(1, feefilter_sent=100, next_feefilter_send_time=soon)
    manager = make_manager([conn], mempool=mempool)

    manager._send_due_feefilters()
    assert not only(conn, FeeFilter)
    assert conn.next_feefilter_send_time == soon


class FakeBlockIndex:
    """A `BlockIndex` stand-in: fixed candidates, and which of them are held."""

    def __init__(
        self,
        candidates: list[bytes],
        *,
        downloaded: Sequence[bytes] = (),
        active_chain_length: int = 1,
    ) -> None:
        """Hold `candidates` and mark `downloaded` of them as already stored."""
        self.candidates = list(candidates)
        self.downloaded = set(downloaded)
        self.active_chain = [a_hash(0)] * active_chain_length

    def get_download_candidates(self) -> list[bytes]:
        """Return every candidate this index was built with."""
        return list(self.candidates)

    def get_block_info(self, block_hash: bytes) -> Any:
        """Answer whether `block_hash` is downloaded and its candidate index."""
        return SimpleNamespace(
            downloaded=block_hash in self.downloaded,
            index=self.candidates.index(block_hash) + 1,
        )


def test_nothing_is_downloaded_before_the_headers_are_synced() -> None:
    """`block_download` is a no-op before this node's headers are synced."""
    conn = a_conn(1)
    manager = make_manager(
        [conn],
        status=NodeStatus.SyncingHeaders,
        block_index=FakeBlockIndex([a_hash(1)]),
    )
    manager.block_download()
    assert not conn.sent


def test_a_block_is_asked_of_a_peer_with_an_empty_queue() -> None:
    """A peer with an empty queue is handed the whole download window."""
    conn = a_conn(1)
    wanted = [a_hash(n) for n in range(1, 4)]
    manager = make_manager([conn], block_index=FakeBlockIndex(wanted))
    manager.block_download()
    (getdata,) = only(conn, GetData)
    assert hashes_of(getdata) == wanted
    assert conn.download_queue == wanted


def test_asking_for_a_block_warms_the_worker_pool() -> None:
    """Sending a block `GetData` also calls `warm_worker_pool`."""
    # the earliest point a script is actually going to be validated,
    # with the peer's round trip ahead of it as warm-up runway, rather
    # than the moment header sync merely completes -- a node whose
    # headers are synced but which never has a block to ask for never
    # reaches this and never pays for the pool: btclib-org/btclib-node#262
    warmed = []
    conn = a_conn(1)
    wanted = [a_hash(n) for n in range(1, 4)]
    manager = make_manager(
        [conn],
        block_index=FakeBlockIndex(wanted),
        warm_worker_pool=lambda: warmed.append(True),
    )
    manager.block_download()
    assert warmed == [True]


def test_a_header_only_node_with_nothing_to_download_never_warms_the_pool() -> None:
    """A node with no download candidate at all never builds the worker pool."""
    warmed = []
    conn = a_conn(1)
    manager = make_manager(
        [conn],
        block_index=FakeBlockIndex([]),
        warm_worker_pool=lambda: warmed.append(True),
    )
    manager.block_download()
    assert not conn.sent
    assert not warmed


def test_a_node_with_no_peer_to_ask_never_warms_the_pool() -> None:
    """Candidates with no connection to ask never trigger `warm_worker_pool`."""
    warmed = []
    manager = make_manager(
        [],
        block_index=FakeBlockIndex([a_hash(1)]),
        warm_worker_pool=lambda: warmed.append(True),
    )
    manager.block_download()
    assert not warmed


def test_a_block_that_arrived_while_the_window_was_held_is_not_asked_for() -> None:
    """A block downloaded since the window was built is filtered from it."""
    # get_download_candidates never offers a block already stored, so
    # the filter below it is about the window this manager is holding
    # from an earlier pass, across which a block can have arrived
    conn = a_conn(1)
    wanted = [a_hash(1), a_hash(2)]
    manager = make_manager(
        [conn], block_index=FakeBlockIndex(wanted, downloaded=[a_hash(1)])
    )
    manager.block_window = wanted
    manager.block_download()
    (getdata,) = only(conn, GetData)
    assert hashes_of(getdata) == [a_hash(2)]


def test_nothing_left_to_download_asks_for_nothing() -> None:
    """Once its only candidate downloads, the window empties, asking nothing."""
    conn = a_conn(1)
    manager = make_manager(
        [conn], block_index=FakeBlockIndex([a_hash(1)], downloaded=[a_hash(1)])
    )
    manager.block_window = [a_hash(1)]
    manager.block_download()
    assert not conn.sent
    assert manager.block_window == []


def test_a_download_too_far_ahead_of_the_chain_waits() -> None:
    """Past `MAX_DOWNLOAD_WINDOW` ahead of the chain, no request is sent."""
    # the window is filled from the headers, which run far ahead of the
    # blocks; fetching all of them at once is what the bound is for
    conn = a_conn(1)
    wanted = [a_hash(n) for n in range(1, 1200)]
    manager = make_manager([conn], block_index=FakeBlockIndex(wanted))
    manager.block_window = wanted[1025:]
    manager.block_download()
    assert not conn.sent


def test_a_peer_that_is_already_busy_is_not_asked_again() -> None:
    """A peer with a non-empty queue is left alone, given no more work."""
    busy = a_conn(1, queue=[a_hash(1)])
    manager = make_manager([busy], block_index=FakeBlockIndex([a_hash(1), a_hash(2)]))
    manager.block_download()
    assert not busy.sent
    assert busy.download_queue == [a_hash(1)]


def test_a_block_that_arrived_leaves_the_queue_it_was_asked_in() -> None:
    """A downloaded block leaves the queue of the peer it was asked of."""
    conn = a_conn(1, queue=[a_hash(1)])
    manager = make_manager(
        [conn],
        block_index=FakeBlockIndex([a_hash(1), a_hash(2)], downloaded=[a_hash(1)]),
    )
    manager.block_download()
    assert conn.download_queue == [a_hash(2)]


def test_a_peer_that_stopped_sending_blocks_is_marked_and_then_dropped() -> None:
    """A quiet peer is marked for eviction, then dropped past a harder bound."""
    # only while still syncing blocks: a peer with nothing to send is not
    # a peer that has stalled
    quiet = a_conn(1, last_block=time.time() - 200)
    stalled = a_conn(2, last_block=time.time() - 400)
    stopped = []
    stalled.stop = lambda: stopped.append(True)
    manager = make_manager(
        [quiet, stalled],
        status=NodeStatus.HeaderSynced,
        block_index=FakeBlockIndex([a_hash(1)]),
    )
    manager.block_download()
    assert quiet.pending_eviction
    assert stopped == [True]


def test_a_block_only_one_peer_was_asked_for_is_asked_of_a_second() -> None:
    """An idle peer is given a block another peer already carries too."""
    # nothing left in the window that nobody is fetching, and a peer
    # sitting idle: it is given what somebody else is already carrying,
    # which is how a block a peer never sends stops holding the chain up
    busy = a_conn(1, queue=[a_hash(1)])
    idle = a_conn(2)
    manager = make_manager([busy, idle], block_index=FakeBlockIndex([a_hash(1)]))
    manager.block_download()
    # and the peer already carrying it keeps it rather than being asked
    # for it a second time
    assert not busy.sent
    assert busy.download_queue == [a_hash(1)]
    (getdata,) = only(idle, GetData)
    assert hashes_of(getdata) == [a_hash(1)]


def test_a_stalled_peers_own_blocks_are_not_handed_straight_back_to_it() -> None:
    """A stalled peer's emptied queue goes to a healthy peer, not back to it."""
    # the queue emptied for stalling past the 120s mark is not read as
    # "ready for more work": the blocks it was holding go to the healthy
    # peer instead, and the stalled one is asked for nothing at all
    stalled = a_conn(1, last_block=time.time() - 200, queue=[a_hash(1), a_hash(2)])
    healthy = a_conn(2)
    manager = make_manager(
        [stalled, healthy],
        status=NodeStatus.HeaderSynced,
        block_index=FakeBlockIndex([a_hash(1), a_hash(2), a_hash(3)]),
    )
    manager.block_download()
    assert stalled.pending_eviction
    assert not stalled.sent
    (getdata,) = only(healthy, GetData)
    assert hashes_of(getdata) == [a_hash(1), a_hash(2), a_hash(3)]


def test_a_peer_that_is_already_pending_eviction_is_left_alone() -> None:
    """A peer already marked `pending_eviction` keeps its queue, asked none."""
    quiet = a_conn(1, last_block=time.time() - 200, queue=[a_hash(1)])
    quiet.pending_eviction = True
    manager = make_manager(
        [quiet],
        status=NodeStatus.HeaderSynced,
        block_index=FakeBlockIndex([a_hash(1)]),
    )
    manager.block_download()
    # the queue it was already given is not thrown away a second time,
    # so it is not asked for the same block again either
    assert quiet.download_queue == [a_hash(1)]
    assert not quiet.sent


def test_an_idle_peer_is_asked_for_nothing_once_every_block_has_three_takers() -> None:
    """A fourth, idle peer is asked nothing once every block has 3 takers."""
    # three peers already hold the window's one block between them, which
    # is what Counter's `x[1] < 3` reads as fully requested: the fourth,
    # idle peer's own turn in the loop finds neither `waiting` nor
    # `pending` with anything left to hand it. Whether that happens at
    # all otherwise depends on how the window divides across peers at
    # that instant, which is what btclib-org/btclib-node#319 is about.
    takers = [a_conn(n, queue=[a_hash(1)]) for n in (1, 2, 3)]
    idle = a_conn(4)
    manager = make_manager([*takers, idle], block_index=FakeBlockIndex([a_hash(1)]))
    manager.block_download()
    assert not idle.sent
    assert idle.download_queue == []
    for taker in takers:
        assert not taker.sent
        assert taker.download_queue == [a_hash(1)]
