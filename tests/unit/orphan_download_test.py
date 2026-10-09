# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What `DownloadManager` does with orphans, as Core's `TxDownloadManager` does.

Who is asked for the parents of an orphan, what is kept and what is
refused, and which child is tried with a parent that pays too little.
"""

import secrets
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.p2p.inventory import GetData, InventoryType
from btclib.script.witness import Witness

import btclib_node.download as download_module
from btclib_node.exceptions import MissingPrevoutError, TxRejectedError
from btclib_node.mempool import package_hash
from btclib_node.p2p.compact_block import MAX_EXTRA_TX_WEIGHT, MAX_EXTRA_TXNS
from btclib_node.p2p.permissions import NetPermissionFlags
from tests.unit.download_test import (
    a_conn,
    a_hash,
    getdata_hashes,
    make_manager,
    only,
)
from tests.unit.orphanage_test import a_child, an_orphan
from tests.unit.rolling_bloom_test import a_small_filter

if TYPE_CHECKING:
    from btclib.block import Block
    from btclib.tx.tx import Tx


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Hold `download`'s clock at `clock.now` until a test moves it."""
    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(
        download_module, "time", SimpleNamespace(time=lambda: clock.now)
    )
    return clock


def announced(manager: Any, peer: int, txhash: bytes) -> Any:
    """Return `peer`'s tracked announcement of `txhash`, or `None`."""
    known = manager.tx_requests._peers.get(peer)
    return None if known is None else known.announcements.get(txhash)


def a_parent_and_child() -> tuple[Tx, Tx]:
    """Return a transaction and one that spends it."""
    parent = an_orphan()
    return parent, a_child(parent)


def a_segwit(tx: Tx) -> Tx:
    """Give `tx` a witness, which tells its wtxid from its txid."""
    tx.vin[0].script_witness = Witness([secrets.token_bytes(8)])
    return tx


def test_an_orphan_is_already_had_by_either_hash() -> None:
    """Core asks the orphanage by wtxid whatever kind of hash it is given."""
    manager = make_manager([a_conn(1)])
    tx = an_orphan()
    manager.orphanage.add_tx(tx, 1)
    for wtxid in (True, False):
        assert manager.already_have_tx(
            tx.hash, wtxid=wtxid, include_reconsiderable=False
        )


def test_the_reconsiderable_filter_counts_only_where_asked() -> None:
    """`include_reconsiderable` is what separates announcing from asking."""
    manager = make_manager([a_conn(1)])
    manager.node.mempool.mark_rejected_reconsiderable(a_hash(1))
    assert manager.already_have_tx(a_hash(1), wtxid=True, include_reconsiderable=True)
    assert not manager.already_have_tx(
        a_hash(1), wtxid=True, include_reconsiderable=False
    )


def test_a_recent_reject_is_already_had() -> None:
    """The first cache counts either way."""
    manager = make_manager([a_conn(1)])
    manager.node.mempool.mark_rejected(a_hash(1))
    assert manager.already_have_tx(a_hash(1), wtxid=False, include_reconsiderable=False)


def test_a_recently_confirmed_transaction_is_already_had_by_either_hash() -> None:
    """ISS 1851: Core's `BlockConnected` records the txid and the wtxid."""
    manager = make_manager([a_conn(1)])
    tx = a_segwit(an_orphan())
    manager.confirm_block(cast("Block", SimpleNamespace(transactions=[tx])))
    assert manager.already_have_tx(tx.id, wtxid=False, include_reconsiderable=False)
    assert manager.already_have_tx(tx.hash, wtxid=True, include_reconsiderable=False)


def test_a_confirmed_transaction_is_forgotten_by_either_hash() -> None:
    """ISS 1871: Core's `BlockConnected` calls `ForgetTxHash` on both."""
    manager = make_manager([a_conn(1), a_conn(2)])
    tx = a_segwit(an_orphan())
    kept = a_hash(9)
    for peer in (1, 2):
        for txhash in (tx.id, tx.hash, kept):
            manager.tx_requests.received_inv(peer, txhash, preferred=True, reqtime=0)
    manager.confirm_block(cast("Block", SimpleNamespace(transactions=[tx])))
    for peer in (1, 2):
        assert announced(manager, peer, tx.id) is None
        assert announced(manager, peer, tx.hash) is None
        assert announced(manager, peer, kept) is not None


def test_the_recently_confirmed_filter_is_sized_as_core_s() -> None:
    """ISS 1851: Core's `{48'000, 0.000'001}` filter.

    The figures are what Core's own filter printed for those parameters, in
    `tests/unit/_data/core_rolling_bloom_runs.txt`.
    """
    bloom = make_manager([a_conn(1)]).recent_confirmed
    assert (bloom._lane_bytes // 8, bloom._per_generation, bloom._size) == (
        20,
        24_000,
        64_700,
    )


@pytest.mark.parametrize("site", ["confirmed", "rejects", "reconsiderable"])
def test_what_a_filter_wrongly_finds_is_already_had(site: str) -> None:
    """ISS 1851: Core's `AlreadyHaveTx` counts a false positive as had."""
    manager = make_manager([a_conn(1)])
    bloom = a_small_filter(a_hash(1))
    mempool = manager.node.mempool
    if site == "confirmed":
        manager.recent_confirmed = bloom
    elif site == "rejects":
        mempool._recent_rejects = bloom
    else:
        mempool._recent_rejects_reconsiderable = bloom
    never = next(a_hash(i) for i in range(2, 1000) if a_hash(i) in bloom)
    assert manager.already_have_tx(never, wtxid=True, include_reconsiderable=True)


def test_the_mempool_is_asked_by_the_kind_of_hash_given() -> None:
    """A wtxid is a key of the transactions, a txid of the txid index."""
    manager = make_manager([a_conn(1)])
    tx = a_segwit(an_orphan())
    manager.node.mempool.add_tx(tx)
    assert manager.already_have_tx(tx.hash, wtxid=True, include_reconsiderable=True)
    assert manager.already_have_tx(tx.id, wtxid=False, include_reconsiderable=True)
    assert not manager.already_have_tx(tx.id, wtxid=True, include_reconsiderable=True)
    assert not manager.already_have_tx(
        tx.hash, wtxid=False, include_reconsiderable=True
    )


def test_the_unique_parents_are_each_listed_once_in_order() -> None:
    """Two inputs of one parent name it once, in Core's `Txid` order."""
    from tests.unit.orphanage_test import a_tx  # noqa: PLC0415

    # Displayed bytes order `b` before `a`; internal bytes order `a` before `b`.
    # The inputs name `b` first, so only the internal order lists `a` first.
    a = b"\x01" + bytes(31)
    b = bytes(31) + b"\xff"
    tx = a_tx((b, 0), (a, 0), (b, 1))
    assert download_module.DownloadManager.unique_parents(tx) == sorted(
        {a, b}, key=lambda txid: txid[::-1]
    )


def test_a_transaction_missing_inputs_is_kept_for_the_peer_that_sent_it() -> None:
    """Its parents are asked of that peer, by txid, and it is not refused."""
    manager = make_manager([a_conn(1, inbound=False)])
    parent, child = a_parent_and_child()
    assert (
        manager.mempool_rejected_tx(child, MissingPrevoutError("x"), 1, first_time=True)
        is None
    )
    assert manager.orphanage.have_tx_from_peer(child.hash, 1)
    assert announced(manager, 1, parent.id) is not None
    assert manager.tx_requests.is_txid(1, parent.id)
    assert not manager.node.mempool.was_recently_rejected(child.hash)


def test_a_transaction_missing_inputs_is_kept_only_the_first_time() -> None:
    """One taken from the orphanage is not kept again, nor refused."""
    manager = make_manager([a_conn(1)])
    _, child = a_parent_and_child()
    manager.mempool_rejected_tx(child, MissingPrevoutError("x"), 1, first_time=False)
    assert not manager.orphanage.have_tx(child.hash)


def test_a_transaction_refused_before_is_not_kept_as_an_orphan() -> None:
    """A wtxid in the first cache stays out."""
    manager = make_manager([a_conn(1)])
    _, child = a_parent_and_child()
    manager.node.mempool.mark_rejected(child.hash)
    manager.mempool_rejected_tx(child, MissingPrevoutError("x"), 1, first_time=True)
    assert not manager.orphanage.have_tx(child.hash)


def test_the_other_announcers_of_an_orphan_are_candidates_to_resolve_it() -> None:
    """Whoever announced the txid or the wtxid is asked for the parents too."""
    manager = make_manager([a_conn(1), a_conn(2), a_conn(3), a_conn(4)])
    parent = an_orphan()
    child = a_segwit(a_child(parent))
    manager.tx_requests.received_inv(2, child.id, preferred=False, reqtime=0)
    manager.tx_requests.received_inv(3, child.hash, preferred=False, reqtime=0)
    manager.mempool_rejected_tx(child, MissingPrevoutError("x"), 1, first_time=True)
    assert sorted(
        peer
        for peer in range(1, 5)
        if manager.orphanage.have_tx_from_peer(child.hash, peer)
    ) == [1, 2, 3]
    # the announcements of the child itself are forgotten
    assert announced(manager, 2, child.id) is None
    assert announced(manager, 3, child.hash) is None


def test_a_parent_already_had_is_not_asked_for() -> None:
    """Only what the node lacks is announced."""
    manager = make_manager([a_conn(1)])
    held, lacking = an_orphan(), an_orphan()
    manager.node.mempool.add_tx(held)
    child = a_tx_of(held, lacking)
    manager.mempool_rejected_tx(child, MissingPrevoutError("x"), 1, first_time=True)
    assert announced(manager, 1, held.id) is None
    assert announced(manager, 1, lacking.id) is not None


def a_tx_of(*parents: Tx) -> Tx:
    """Return a transaction spending the first output of each of `parents`."""
    from tests.unit.orphanage_test import a_tx  # noqa: PLC0415

    return a_tx(*((parent.id, 0) for parent in parents))


def test_a_child_of_a_refused_parent_is_refused_under_both_hashes() -> None:
    """Nothing the parent undoes, so nothing to keep."""
    sender = a_conn(1)
    manager = make_manager([sender])
    parent, child = a_parent_and_child()
    child = a_segwit(child)
    manager.node.mempool.mark_rejected(parent.id)
    manager.mempool_rejected_tx(child, MissingPrevoutError("x"), 1, first_time=True)
    mempool = manager.node.mempool
    assert mempool.was_recently_rejected(child.id)
    assert mempool.was_recently_rejected(child.hash)
    assert not manager.orphanage.have_tx(child.hash)
    # Core clears `unique_parents` in this branch: no parent is recorded
    assert parent.id not in sender.known_tx_inventory


def test_a_child_of_one_reconsiderable_parent_is_kept_and_of_two_refused() -> None:
    """One parent and one child are a package; two parents are not."""
    manager = make_manager([a_conn(1)])
    first, second = an_orphan(), an_orphan()
    mempool = manager.node.mempool
    mempool.mark_rejected_reconsiderable(first.id)
    one = a_tx_of(first)
    manager.mempool_rejected_tx(one, MissingPrevoutError("x"), 1, first_time=True)
    assert manager.orphanage.have_tx(one.hash)
    mempool.mark_rejected_reconsiderable(second.id)
    two = a_tx_of(first, second)
    manager.mempool_rejected_tx(two, MissingPrevoutError("x"), 1, first_time=True)
    assert not manager.orphanage.have_tx(two.hash)
    assert mempool.was_recently_rejected(two.hash)


def test_a_reconsiderable_parent_the_mempool_holds_does_not_count() -> None:
    """Once it is in, it is no parent left to resolve."""
    manager = make_manager([a_conn(1)])
    parent = an_orphan()
    mempool = manager.node.mempool
    mempool.add_tx(parent)
    mempool.mark_rejected_reconsiderable(parent.id)
    other = an_orphan()
    mempool.mark_rejected_reconsiderable(other.id)
    child = a_tx_of(parent, other)
    manager.mempool_rejected_tx(child, MissingPrevoutError("x"), 1, first_time=True)
    assert manager.orphanage.have_tx(child.hash)


def reconsiderable() -> TxRejectedError:
    """Return the refusal a package can undo."""
    return TxRejectedError("insufficient fee")


def test_a_reconsiderable_refusal_is_recorded_and_finds_a_child() -> None:
    """The wtxid is recorded as undoable, and the child comes back."""
    manager = make_manager([a_conn(1)])
    parent, child = a_parent_and_child()
    manager.orphanage.add_tx(child, 1)
    manager.tx_requests.received_inv(1, parent.hash, preferred=True, reqtime=0)
    package = manager.mempool_rejected_tx(parent, reconsiderable(), 1, first_time=True)
    assert package == (parent, child)
    mempool = manager.node.mempool
    assert mempool.was_recently_rejected_reconsiderable(parent.hash)
    assert not mempool.was_recently_rejected(parent.hash)
    assert announced(manager, 1, parent.hash) is None


def test_a_reconsiderable_refusal_finds_no_child_the_second_time() -> None:
    """A transaction from the orphanage or a package is not paired again."""
    manager = make_manager([a_conn(1)])
    parent, child = a_parent_and_child()
    manager.orphanage.add_tx(child, 1)
    assert (
        manager.mempool_rejected_tx(parent, reconsiderable(), 1, first_time=False)
        is None
    )


def test_another_refusal_is_recorded_and_ends_the_orphan() -> None:
    """The first cache takes it, and the orphanage drops it."""
    manager = make_manager([a_conn(1)])
    _, child = a_parent_and_child()
    manager.orphanage.add_tx(child, 1)
    for error in (TxRejectedError("bad-txns"), ValueError("x")):
        assert manager.mempool_rejected_tx(child, error, 1, first_time=True) is None
    mempool = manager.node.mempool
    assert mempool.was_recently_rejected(child.hash)
    assert not manager.orphanage.have_tx(child.hash)


def test_a_child_is_found_only_from_the_peer_and_once_per_pairing() -> None:
    """Another peer's child, a refused child or a tried pair is passed over."""
    manager = make_manager([a_conn(1)])
    parent = an_orphan()
    mempool = manager.node.mempool
    assert manager.find_1p1c_package(parent, 1) is None
    other, tried, refused, fresh = (a_child(parent, vout=i) for i in range(4))
    manager.orphanage.add_tx(other, 2)
    manager.orphanage.add_tx(tried, 1)
    manager.orphanage.add_tx(refused, 1)
    assert manager.find_1p1c_package(parent, 1) is not None
    mempool.mark_rejected_reconsiderable(package_hash([parent.hash, refused.hash]))
    mempool.mark_rejected(tried.id)
    assert manager.find_1p1c_package(parent, 1) is None
    manager.orphanage.add_tx(fresh, 1)
    assert manager.find_1p1c_package(parent, 1) == (parent, fresh)


def test_an_accepted_transaction_is_forgotten_and_wakes_its_children() -> None:
    """It is asked of no one, its orphans are marked, and it is no orphan."""
    manager = make_manager([a_conn(1)])
    parent, child = a_parent_and_child()
    manager.orphanage.add_tx(parent, 2)
    manager.orphanage.add_tx(child, 1)
    manager.tx_requests.received_inv(1, parent.id, preferred=True, reqtime=0)
    manager.tx_requests.received_inv(1, parent.hash, preferred=True, reqtime=0)
    manager.mempool_accepted_tx(parent)
    assert manager.tx_requests.size() == 0
    assert manager.orphanage.peers_to_reconsider() == [1]
    assert not manager.orphanage.have_tx(parent.hash)


def test_an_announcement_of_an_orphan_asks_the_announcer_for_its_parents() -> None:
    """The announcer is asked for the parents, not for the orphan."""
    first, second = a_conn(1, inbound=False), a_conn(2, inbound=False)
    manager = make_manager([first, second])
    parent, child = a_parent_and_child()
    manager.orphanage.add_tx(child, 1)
    manager.inv_txs = [(2, child.hash, False)]
    manager.tx_download()
    assert manager.orphanage.have_tx_from_peer(child.hash, 2)
    assert announced(manager, 2, child.hash) is None
    assert announced(manager, 2, parent.id) is not None


def test_an_announcement_of_an_orphan_with_every_parent_had_asks_nothing() -> None:
    """There is nothing left to resolve."""
    manager = make_manager([a_conn(1), a_conn(2, inbound=False)])
    parent, child = a_parent_and_child()
    manager.orphanage.add_tx(child, 1)
    manager.node.mempool.add_tx(parent)
    manager.inv_txs = [(2, child.hash, False)]
    manager.tx_download()
    assert not manager.orphanage.have_tx_from_peer(child.hash, 2)
    assert manager.tx_requests.size() == 0


def test_a_peer_without_wtxid_relay_is_not_made_a_resolver_by_a_txid() -> None:
    """The orphanage is looked up by wtxid only for a peer that relays them."""
    conn = a_conn(2, inbound=False, wtxidrelay_received=False)
    manager = make_manager([a_conn(1), conn])
    child = an_orphan()
    manager.orphanage.add_tx(child, 1)
    manager.inv_txs = [(2, child.hash, True)]
    manager.tx_download()
    # it is already had as an orphan, and so asked of no one
    assert manager.tx_requests.size() == 0
    assert not manager.orphanage.have_tx_from_peer(child.hash, 2)


def test_an_announcement_of_what_is_already_had_is_dropped() -> None:
    """A reconsiderable refusal is not asked for again."""
    conn = a_conn(1, inbound=False)
    manager = make_manager([conn])
    manager.node.mempool.mark_rejected_reconsiderable(a_hash(1))
    manager.inv_txs = [(1, a_hash(1), False)]
    manager.tx_download()
    assert manager.tx_requests.size() == 0
    assert not only(conn, GetData)


def test_the_parent_of_an_orphan_is_asked_for_by_txid_of_a_wtxid_peer(
    clock: SimpleNamespace,
) -> None:
    """Core's `GenTxid::Txid`: `MSG_TX` with the witness flag, not `MSG_WTX`."""
    conn = a_conn(1, inbound=False)
    manager = make_manager([conn])
    parent, child = a_parent_and_child()
    manager.mempool_rejected_tx(child, MissingPrevoutError("x"), 1, first_time=True)
    clock.now += 10
    manager.tx_download()
    (getdata,) = only(conn, GetData)
    (item,) = getdata.items
    assert item.hash == parent.id
    assert item.type_code == InventoryType.MSG_WITNESS_TX


def test_a_parent_that_arrived_meanwhile_is_forgotten_not_asked(
    clock: SimpleNamespace,
) -> None:
    """`already_have_tx` is asked by txid for what is announced by txid."""
    conn = a_conn(1, inbound=False)
    manager = make_manager([conn])
    parent, child = a_parent_and_child()
    manager.mempool_rejected_tx(child, MissingPrevoutError("x"), 1, first_time=True)
    manager.node.mempool.add_tx(parent)
    clock.now += 10
    manager.tx_download()
    assert not getdata_hashes(conn)
    assert announced(manager, 1, parent.id) is None


def test_an_orphan_of_a_peer_that_left_is_dropped_by_the_next_step() -> None:
    """Core's `FinalizeNode` forgets them, here the step does."""
    manager = make_manager([a_conn(1)])
    tx = an_orphan()
    manager.orphanage.add_tx(tx, 1)
    manager.orphanage.add_tx(tx, 9)
    manager.tx_download()
    assert manager.orphanage.get_orphan_transactions() == [(tx, [1])]


def a_resolution(
    manager: Any, peer: int, parents: list[bytes], wtxid: bytes | None = None
) -> bool:
    """Run `_maybe_add_orphan_resolution_candidate` as the manager would."""
    return bool(
        manager._maybe_add_orphan_resolution_candidate(
            parents, wtxid or a_hash(99), peer, 1000.0, 0
        )
    )


def test_a_peer_that_left_or_announced_the_orphan_is_not_a_candidate() -> None:
    """Neither is asked."""
    manager = make_manager([a_conn(1)])
    assert not a_resolution(manager, 7, [a_hash(1)])
    tx = an_orphan()
    manager.orphanage.add_tx(tx, 1)
    assert not a_resolution(manager, 1, [a_hash(1)], tx.hash)
    assert manager.tx_requests.size() == 0


def test_a_peer_with_too_many_announcements_is_not_a_candidate_unless_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's bound, which `RELAY` is exempt from."""
    monkeypatch.setattr(download_module, "_MAX_PEER_TX_ANNOUNCEMENTS", 2)
    plain = a_conn(1)
    relay = a_conn(2, permissions=NetPermissionFlags.RELAY)
    manager = make_manager([plain, relay])
    parents = [a_hash(1), a_hash(2), a_hash(3)]
    assert not a_resolution(manager, 1, parents)
    assert a_resolution(manager, 2, parents)
    assert manager.tx_requests.count(2) == 3


def test_the_delays_of_a_candidate_add_up_as_for_an_announcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not preferred, a wtxid peer connected, overloaded: two seconds each."""
    manager = make_manager([a_conn(1, inbound=False), a_conn(2, inbound=True)])
    assert a_resolution(manager, 1, [a_hash(1)])
    assert announced(manager, 1, a_hash(1)).time == 1000.0
    manager._maybe_add_orphan_resolution_candidate(
        [a_hash(2)], a_hash(98), 2, 1000.0, 1
    )
    assert announced(manager, 2, a_hash(2)).time == 1004.0
    monkeypatch.setattr(download_module, "_MAX_PEER_TX_REQUEST_IN_FLIGHT", 0)
    manager._maybe_add_orphan_resolution_candidate(
        [a_hash(3)], a_hash(97), 2, 1000.0, 1
    )
    assert announced(manager, 2, a_hash(3)).time == 1006.0
    relay = a_conn(3, permissions=NetPermissionFlags.RELAY)
    manager.node.p2p_manager.connections[3] = relay
    manager._maybe_add_orphan_resolution_candidate(
        [a_hash(4)], a_hash(96), 3, 1000.0, 0
    )
    assert announced(manager, 3, a_hash(4)).time == 1002.0


def test_the_wtxid_peers_are_counted() -> None:
    """Core's `m_num_wtxid_peers`."""
    manager = make_manager([a_conn(1), a_conn(2, wtxidrelay_received=False), a_conn(3)])
    assert manager._wtxid_peer_count() == 2


def test_the_sender_of_an_orphan_is_known_to_have_its_missing_parents() -> None:
    """ISS 1630: Core's `AddKnownTx` on the parents, for a child sent to us."""
    sender = a_conn(1)
    manager = make_manager([sender])
    parent, child = a_parent_and_child()
    manager.mempool_rejected_tx(child, MissingPrevoutError("x"), 1, first_time=True)
    assert parent.id in sender.known_tx_inventory
    assert child.id not in sender.known_tx_inventory


def test_an_orphan_whose_sender_is_gone_is_taken_in_without_recording() -> None:
    """ISS 1630: Core records the parents `if (peer)` and goes on without."""
    manager = make_manager([a_conn(2)])
    _, child = a_parent_and_child()
    refused = manager.mempool_rejected_tx(
        child, MissingPrevoutError("x"), 1, first_time=True
    )
    assert refused is None


def test_a_first_refusal_is_kept_to_rebuild_compact_blocks_with() -> None:
    """Core's `vExtraTxnForCompact`: the last `MAX_EXTRA_TXNS` refused.

    A transaction refused again, from the orphanage, is not kept again.
    """
    manager = make_manager([a_conn(1)])
    refused = [an_orphan() for _ in range(MAX_EXTRA_TXNS + 1)]
    for tx in refused:
        manager.mempool_rejected_tx(tx, TxRejectedError("x"), 1, first_time=True)
    assert list(manager.extra_txns) == refused[1:]
    again = an_orphan()
    manager.mempool_rejected_tx(again, TxRejectedError("x"), 1, first_time=False)
    assert again not in manager.extra_txns


def test_an_orphan_is_kept_to_rebuild_compact_blocks_with_once() -> None:
    """Core keeps no orphan the orphanage held already, from another peer."""
    manager = make_manager([a_conn(1), a_conn(2)])
    _, child = a_parent_and_child()
    for conn_id in (1, 2):
        manager.mempool_rejected_tx(
            child, MissingPrevoutError("x"), conn_id, first_time=True
        )
    assert manager.orphanage.have_tx_from_peer(child.hash, 2)
    assert list(manager.extra_txns) == [child]


def test_an_orphan_of_a_refused_parent_is_kept_to_rebuild_with() -> None:
    """Not kept as an orphan, it is kept for compact blocks all the same."""
    manager = make_manager([a_conn(1)])
    parent, child = a_parent_and_child()
    manager.node.mempool.mark_rejected(parent.id)
    manager.mempool_rejected_tx(child, MissingPrevoutError("x"), 1, first_time=True)
    assert not manager.orphanage.have_tx(child.hash)
    assert list(manager.extra_txns) == [child]


def test_a_heavy_refusal_is_not_kept_to_rebuild_compact_blocks_with() -> None:
    """Core's bound of 100000 bytes, read here as weight, is exclusive."""
    manager = make_manager([a_conn(1)])
    padding = (MAX_EXTRA_TX_WEIGHT - an_orphan().weight) // 4
    heavy, lighter = an_orphan(padding=padding - 2), an_orphan(padding=padding - 3)
    assert heavy.weight == MAX_EXTRA_TX_WEIGHT
    assert lighter.weight < MAX_EXTRA_TX_WEIGHT
    for tx in (heavy, lighter):
        manager.mempool_rejected_tx(tx, TxRejectedError("x"), 1, first_time=True)
    assert list(manager.extra_txns) == [lighter]
