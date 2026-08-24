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

import time
from collections.abc import Sequence
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.fee import FeeRate, fee_from_vsize
from btclib.p2p.addrv2 import NetworkAddressV2
from btclib.p2p.inventory import GetData, Inv

import btclib_node.download as download_module
from btclib_node.constants import NodeStatus
from btclib_node.download import DownloadManager
from btclib_node.log import Logger
from btclib_node.mempool import Mempool
from btclib_node.p2p.address import peer_address
from tests.helpers import generate_random_transaction

if TYPE_CHECKING:
    from btclib_node import Node


def a_hash(n: int) -> bytes:
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
) -> Any:
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
    )


def make_manager(
    conns: list[Any],
    *,
    status: NodeStatus = NodeStatus.BlockSynced,
    block_index: Any | None = None,
    mempool: Any | None = None,
    warm_worker_pool: Any = None,
) -> DownloadManager:
    node = SimpleNamespace(
        status=status,
        p2p_manager=SimpleNamespace(connections={conn.id: conn for conn in conns}),
        chainstate=SimpleNamespace(block_index=block_index),
        mempool=mempool if mempool is not None else Mempool(Logger(debug=True)),
        warm_worker_pool=warm_worker_pool or (lambda: None),
    )
    return DownloadManager(cast("Node", node), Logger(debug=True))


def hashes_of(message: GetData | Inv) -> list[bytes]:
    return [item.hash for item in message.items]


def only[M](conn: Any, kind: type[M]) -> list[M]:
    return [message for message in conn.sent if isinstance(message, kind)]


def test_a_step_asks_for_neither_kind_while_the_headers_are_syncing() -> None:
    conn = a_conn(1)
    manager = make_manager([conn], status=NodeStatus.SyncingHeaders)
    manager.inv_txs = [(1, a_hash(1))]
    manager.step()
    assert not conn.sent


def test_a_transaction_a_peer_announced_is_asked_of_that_peer() -> None:
    first, second = a_conn(1), a_conn(2)
    manager = make_manager([first, second])
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    (getdata,) = only(first, GetData)
    assert hashes_of(getdata) == [a_hash(1)]
    assert not second.sent


def test_a_peer_that_announced_the_same_transaction_twice_is_asked_once() -> None:
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1)), (1, a_hash(1))]
    manager.tx_download()
    (getdata,) = only(conn, GetData)
    assert hashes_of(getdata) == [a_hash(1)]


def test_an_announcement_from_a_peer_that_is_gone_asks_nobody() -> None:
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(99, a_hash(1))]
    manager.tx_download()
    assert not conn.sent


def test_the_peer_that_sent_a_transaction_is_not_asked_for_it_again() -> None:
    # it is in the mempool now: asking its source for it is a round trip
    # and a second copy of something we already hold
    sender = a_conn(1)
    manager = make_manager([sender])
    manager.inv_txs = [(1, a_hash(1))]
    manager.received_txs = [(1, a_hash(1))]
    manager.tx_download()
    assert not only(sender, GetData)


def test_a_single_transaction_is_announced_rather_than_held_back() -> None:
    # one is a whole step's worth of transactions on a quiet network,
    # and the lists are emptied at the end of the step: a batch size
    # held against them is not a throttle but a filter on whether a
    # transaction is ever announced at all
    sender, other = a_conn(1), a_conn(2)
    manager = make_manager([sender, other])
    manager.received_txs = [(1, a_hash(1))]
    manager.tx_download()
    (inv,) = only(other, Inv)
    assert hashes_of(inv) == [a_hash(1)]
    assert not only(sender, Inv)


def test_a_peer_that_already_has_all_of_them_is_told_nothing() -> None:
    # and not told with an empty inv, which is a message with nothing in
    # it for the peer to do
    sender = a_conn(1)
    manager = make_manager([sender])
    manager.received_txs = [(1, a_hash(n)) for n in range(1, 4)]
    manager.tx_download()
    assert not sender.sent


def test_a_peer_that_asked_for_no_transactions_is_sent_none() -> None:
    # BIP37's fRelay, which the version callback puts on the connection:
    # a peer that declined is not sent a shorter inv, it is not sent one
    # at all. With a peer that did ask, so the assertion is about the
    # flag rather than about a step that announced to nobody.
    sender, declined, wants = a_conn(1), a_conn(2, relay_tx=False), a_conn(3)
    manager = make_manager([sender, declined, wants])
    manager.received_txs = [(1, a_hash(1))]
    manager.tx_download()
    assert not only(declined, Inv)
    (inv,) = only(wants, Inv)
    assert hashes_of(inv) == [a_hash(1)]


def test_a_peer_that_declined_relay_is_still_answered_about_what_it_wants() -> None:
    # declining transactions is about what it is sent unasked; a peer
    # that announced one is still asked for it
    declined = a_conn(1, relay_tx=False)
    manager = make_manager([declined])
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    (getdata,) = only(declined, GetData)
    assert hashes_of(getdata) == [a_hash(1)]


def test_a_transaction_is_announced_to_the_peers_that_do_not_have_it() -> None:
    sender, announcer, other = a_conn(1), a_conn(2), a_conn(3)
    manager = make_manager([sender, announcer, other])
    received = [a_hash(n) for n in range(1, 7)]
    manager.received_txs = [(1, wtxid) for wtxid in received]
    # peer 2 announced them, so it has them; peer 1 sent them
    manager.inv_txs = [(2, wtxid) for wtxid in received]
    manager.tx_download()
    assert not only(sender, Inv)
    assert not only(announcer, Inv)
    (inv,) = only(other, Inv)
    assert hashes_of(inv) == received


def test_a_peer_s_feefilter_withholds_a_transaction_below_its_rate() -> None:
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
    sender, wants = a_conn(1), a_conn(2, feefilter=1000)
    mempool = Mempool(Logger(debug=True))
    tx = generate_random_transaction()
    mempool.add_tx(tx, fee_from_vsize(tx.vsize, FeeRate(sats_per_kvbyte=1000)))
    manager = make_manager([sender, wants], mempool=mempool)
    manager.received_txs = [(1, tx.hash)]
    manager.tx_download()
    (inv,) = only(wants, Inv)
    assert hashes_of(inv) == [tx.hash]


def test_a_transaction_unknown_to_the_mempool_is_announced_regardless_of_a_filter() -> (
    None
):
    # a wtxid this mempool holds no fee for -- an edge the production
    # path does not reach, since received_txs only ever names a
    # transaction Mempool.add_tx has just accepted -- clears every
    # rate rather than being silently withheld
    sender, wants = a_conn(1), a_conn(2, feefilter=1000)
    manager = make_manager([sender, wants])
    manager.received_txs = [(1, a_hash(1))]
    manager.tx_download()
    (inv,) = only(wants, Inv)
    assert hashes_of(inv) == [a_hash(1)]


def test_a_peer_still_wanting_something_else_is_asked_for_it() -> None:
    sender, wanter = a_conn(1), a_conn(2)
    manager = make_manager([sender, wanter])
    manager.received_txs = [(1, a_hash(1))]
    manager.inv_txs = [(1, a_hash(1)), (2, a_hash(2))]
    manager.tx_download()
    (getdata,) = only(wanter, GetData)
    assert hashes_of(getdata) == [a_hash(2)]
    assert not only(sender, GetData)


def test_a_second_announcement_waits_for_the_peers_own_schedule() -> None:
    # the first ever announcement to a fresh connection fires at once --
    # its schedule reads 0, "never scheduled", which the due-check
    # always treats as due -- but once a schedule is set, a transaction
    # arriving before it comes due is queued rather than sent straight
    # away, which is the change #141 is about
    other = a_conn(1)
    manager = make_manager([other])
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
    other = a_conn(1)
    manager = make_manager([other])
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
    other = a_conn(1)
    manager = make_manager([other])
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
    # net_processing.cpp's OUTBOUND_INVENTORY_BROADCAST_INTERVAL/
    # INBOUND_INVENTORY_BROADCAST_INTERVAL, bitcoin/bitcoin@58a7869f86:
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
    # `CNode::m_network_key` (net.h:755, bitcoin/bitcoin@58a7869f86) is
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
    first = a_conn(1, address=peer_address("2001:db8::1", 8333))
    second = a_conn(2, address=peer_address("2002:db8::1", 8333))
    manager = make_manager([first, second])
    manager.tx_download()
    assert first.next_inv_send_time == second.next_inv_send_time


def test_an_inbound_ipv4_peer_and_an_inbound_ipv6_peer_can_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draws = iter([1.0, 2.0])
    monkeypatch.setattr(download_module._rng, "expovariate", lambda lambd: next(draws))
    first = a_conn(1, address=peer_address("10.0.1.1", 8333))
    second = a_conn(2, address=peer_address("2001:db8::1", 8333))
    manager = make_manager([first, second])
    manager.tx_download()
    assert first.next_inv_send_time != second.next_inv_send_time


def test_a_transaction_this_node_originated_is_announced_like_any_other() -> None:
    # conn_id `None` is `P2pManager.broadcast_raw_transaction`'s own
    # marker for a transaction with no peer to exclude, going through
    # the same queue a relayed transaction does rather than a path of
    # its own
    other = a_conn(1)
    manager = make_manager([other])
    manager.received_txs = [(None, a_hash(1))]
    manager.tx_download()
    (inv,) = only(other, Inv)
    assert hashes_of(inv) == [a_hash(1)]


def test_an_outstanding_ask_is_not_repeated_before_it_is_answered() -> None:
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    assert len(only(conn, GetData)) == 1


def test_a_notfound_response_frees_the_wtxid_to_be_asked_for_again() -> None:
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    conn.tx_requested.pop(a_hash(1), None)

    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    assert len(only(conn, GetData)) == 2


def test_receiving_a_transaction_frees_every_peers_outstanding_ask_for_it() -> None:
    asked = a_conn(1)
    asked.tx_requested[a_hash(1)] = time.time()
    manager = make_manager([asked])
    manager.received_txs = [(2, a_hash(1))]
    manager.tx_download()
    assert a_hash(1) not in asked.tx_requested


def test_both_lists_are_emptied_by_a_step() -> None:
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1))]
    manager.received_txs = [(1, a_hash(2))]
    manager.tx_download()
    assert manager.inv_txs == []
    assert manager.received_txs == []


class FakeBlockIndex:
    def __init__(
        self,
        candidates: list[bytes],
        *,
        downloaded: Sequence[bytes] = (),
        active_chain_length: int = 1,
    ) -> None:
        self.candidates = list(candidates)
        self.downloaded = set(downloaded)
        self.active_chain = [a_hash(0)] * active_chain_length

    def get_download_candidates(self) -> list[bytes]:
        return list(self.candidates)

    def get_block_info(self, block_hash: bytes) -> Any:
        return SimpleNamespace(
            downloaded=block_hash in self.downloaded,
            index=self.candidates.index(block_hash) + 1,
        )


def test_nothing_is_downloaded_before_the_headers_are_synced() -> None:
    conn = a_conn(1)
    manager = make_manager(
        [conn],
        status=NodeStatus.SyncingHeaders,
        block_index=FakeBlockIndex([a_hash(1)]),
    )
    manager.block_download()
    assert not conn.sent


def test_a_block_is_asked_of_a_peer_with_an_empty_queue() -> None:
    conn = a_conn(1)
    wanted = [a_hash(n) for n in range(1, 4)]
    manager = make_manager([conn], block_index=FakeBlockIndex(wanted))
    manager.block_download()
    (getdata,) = only(conn, GetData)
    assert hashes_of(getdata) == wanted
    assert conn.download_queue == wanted


def test_asking_for_a_block_warms_the_worker_pool() -> None:
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
    warmed = []
    manager = make_manager(
        [],
        block_index=FakeBlockIndex([a_hash(1)]),
        warm_worker_pool=lambda: warmed.append(True),
    )
    manager.block_download()
    assert not warmed


def test_a_block_that_arrived_while_the_window_was_held_is_not_asked_for() -> None:
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
    conn = a_conn(1)
    manager = make_manager(
        [conn], block_index=FakeBlockIndex([a_hash(1)], downloaded=[a_hash(1)])
    )
    manager.block_window = [a_hash(1)]
    manager.block_download()
    assert not conn.sent
    assert manager.block_window == []


def test_a_download_too_far_ahead_of_the_chain_waits() -> None:
    # the window is filled from the headers, which run far ahead of the
    # blocks; fetching all of them at once is what the bound is for
    conn = a_conn(1)
    wanted = [a_hash(n) for n in range(1, 1200)]
    manager = make_manager([conn], block_index=FakeBlockIndex(wanted))
    manager.block_window = wanted[1025:]
    manager.block_download()
    assert not conn.sent


def test_a_peer_that_is_already_busy_is_not_asked_again() -> None:
    busy = a_conn(1, queue=[a_hash(1)])
    manager = make_manager([busy], block_index=FakeBlockIndex([a_hash(1), a_hash(2)]))
    manager.block_download()
    assert not busy.sent
    assert busy.download_queue == [a_hash(1)]


def test_a_block_that_arrived_leaves_the_queue_it_was_asked_in() -> None:
    conn = a_conn(1, queue=[a_hash(1)])
    manager = make_manager(
        [conn],
        block_index=FakeBlockIndex([a_hash(1), a_hash(2)], downloaded=[a_hash(1)]),
    )
    manager.block_download()
    assert conn.download_queue == [a_hash(2)]


def test_a_peer_that_stopped_sending_blocks_is_marked_and_then_dropped() -> None:
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
