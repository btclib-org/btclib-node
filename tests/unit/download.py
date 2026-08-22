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
from types import SimpleNamespace
from typing import Any

from btclib.p2p.inventory import GetData, Inv

from btclib_node.constants import NodeStatus
from btclib_node.download import DownloadManager
from btclib_node.log import Logger


def a_hash(n):
    return n.to_bytes(32, "big")


def a_conn(conn_id, *, queue=None, last_block=None):
    sent: list[Any] = []
    return SimpleNamespace(
        id=conn_id,
        send=sent.append,
        sent=sent,
        download_queue=queue if queue is not None else [],
        pending_eviction=False,
        last_block_timestamp=time.time() if last_block is None else last_block,
        stop=lambda: None,
    )


def make_manager(conns, *, status=NodeStatus.BlockSynced, block_index=None):
    node = SimpleNamespace(
        status=status,
        p2p_manager=SimpleNamespace(connections={conn.id: conn for conn in conns}),
        chainstate=SimpleNamespace(block_index=block_index),
    )
    return DownloadManager(node, Logger(debug=True))


def hashes_of(message):
    return [item.hash for item in message.items]


def only(conn, kind):
    return [message for message in conn.sent if isinstance(message, kind)]


def test_a_step_asks_for_neither_kind_while_the_headers_are_syncing():
    conn = a_conn(1)
    manager = make_manager([conn], status=NodeStatus.SyncingHeaders)
    manager.inv_txs = [(1, a_hash(1))]
    manager.step()
    assert not conn.sent


def test_a_transaction_a_peer_announced_is_asked_of_that_peer():
    first, second = a_conn(1), a_conn(2)
    manager = make_manager([first, second])
    manager.inv_txs = [(1, a_hash(1))]
    manager.tx_download()
    (getdata,) = only(first, GetData)
    assert hashes_of(getdata) == [a_hash(1)]
    assert not second.sent


def test_a_peer_that_announced_the_same_transaction_twice_is_asked_once():
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1)), (1, a_hash(1))]
    manager.tx_download()
    (getdata,) = only(conn, GetData)
    assert hashes_of(getdata) == [a_hash(1)]


def test_an_announcement_from_a_peer_that_is_gone_asks_nobody():
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(99, a_hash(1))]
    manager.tx_download()
    assert not conn.sent


def test_the_peer_that_sent_a_transaction_is_not_asked_for_it_again():
    # it is in the mempool now: asking its source for it is a round trip
    # and a second copy of something we already hold
    sender = a_conn(1)
    manager = make_manager([sender])
    manager.inv_txs = [(1, a_hash(1))]
    manager.received_txs = [(1, a_hash(1))]
    manager.tx_download()
    assert not only(sender, GetData)


def test_a_transaction_is_announced_to_the_peers_that_do_not_have_it():
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


def test_a_peer_still_wanting_something_else_is_asked_for_it():
    sender, wanter = a_conn(1), a_conn(2)
    manager = make_manager([sender, wanter])
    manager.received_txs = [(1, a_hash(1))]
    manager.inv_txs = [(1, a_hash(1)), (2, a_hash(2))]
    manager.tx_download()
    (getdata,) = only(wanter, GetData)
    assert hashes_of(getdata) == [a_hash(2)]
    assert not only(sender, GetData)


def test_both_lists_are_emptied_by_a_step():
    conn = a_conn(1)
    manager = make_manager([conn])
    manager.inv_txs = [(1, a_hash(1))]
    manager.received_txs = [(1, a_hash(2))]
    manager.tx_download()
    assert manager.inv_txs == []
    assert manager.received_txs == []


class FakeBlockIndex:
    def __init__(self, candidates, *, downloaded=(), active_chain_length=1):
        self.candidates = list(candidates)
        self.downloaded = set(downloaded)
        self.active_chain = [a_hash(0)] * active_chain_length

    def get_download_candidates(self):
        return list(self.candidates)

    def get_block_info(self, block_hash):
        return SimpleNamespace(
            downloaded=block_hash in self.downloaded,
            index=self.candidates.index(block_hash) + 1,
        )


def test_nothing_is_downloaded_before_the_headers_are_synced():
    conn = a_conn(1)
    manager = make_manager(
        [conn],
        status=NodeStatus.SyncingHeaders,
        block_index=FakeBlockIndex([a_hash(1)]),
    )
    manager.block_download()
    assert not conn.sent


def test_a_block_is_asked_of_a_peer_with_an_empty_queue():
    conn = a_conn(1)
    wanted = [a_hash(n) for n in range(1, 4)]
    manager = make_manager([conn], block_index=FakeBlockIndex(wanted))
    manager.block_download()
    (getdata,) = only(conn, GetData)
    assert hashes_of(getdata) == wanted
    assert conn.download_queue == wanted


def test_a_block_that_arrived_while_the_window_was_held_is_not_asked_for():
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


def test_nothing_left_to_download_asks_for_nothing():
    conn = a_conn(1)
    manager = make_manager(
        [conn], block_index=FakeBlockIndex([a_hash(1)], downloaded=[a_hash(1)])
    )
    manager.block_window = [a_hash(1)]
    manager.block_download()
    assert not conn.sent
    assert manager.block_window == []


def test_a_download_too_far_ahead_of_the_chain_waits():
    # the window is filled from the headers, which run far ahead of the
    # blocks; fetching all of them at once is what the bound is for
    conn = a_conn(1)
    wanted = [a_hash(n) for n in range(1, 1200)]
    manager = make_manager([conn], block_index=FakeBlockIndex(wanted))
    manager.block_window = wanted[1025:]
    manager.block_download()
    assert not conn.sent


def test_a_peer_that_is_already_busy_is_not_asked_again():
    busy = a_conn(1, queue=[a_hash(1)])
    manager = make_manager([busy], block_index=FakeBlockIndex([a_hash(1), a_hash(2)]))
    manager.block_download()
    assert not busy.sent
    assert busy.download_queue == [a_hash(1)]


def test_a_block_that_arrived_leaves_the_queue_it_was_asked_in():
    conn = a_conn(1, queue=[a_hash(1)])
    manager = make_manager(
        [conn],
        block_index=FakeBlockIndex([a_hash(1), a_hash(2)], downloaded=[a_hash(1)]),
    )
    manager.block_download()
    assert conn.download_queue == [a_hash(2)]


def test_a_peer_that_stopped_sending_blocks_is_marked_and_then_dropped():
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


def test_a_peer_that_is_already_pending_eviction_is_left_alone():
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
