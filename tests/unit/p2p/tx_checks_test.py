# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`p2p.main.resume_tx_checks`: one script check at a time, peers in turn."""

import multiprocessing
import os
import threading
from collections import deque
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from btclib.p2p.block_filters import BlockFilterType, CFilter, GetCFilters
from btclib.p2p.data import TxPayload as TxMsg
from btclib.p2p.inventory import GetData, Inventory, InventoryType
from btclib.p2p.keepalive import Ping, Pong
from btclib.tx.limits import COINBASE_MATURITY

import btclib_node.p2p.callbacks as cb
from btclib_node.chains import RegTest
from btclib_node.constants import P2pConnStatus
from btclib_node.exceptions import TxRejectedError
from btclib_node.interpreter import check_transaction
from btclib_node.main import MempoolCandidate, verify_mempool_acceptance
from btclib_node.p2p import tx_checks
from btclib_node.p2p.main import (
    handle_p2p,
    handle_p2p_handshake,
    resume_cfilters,
    resume_getdata,
    resume_tx_checks,
)
from btclib_node.p2p.protocol_version import BIP0031_VERSION
from tests import (
    build_block,
    generate_coinbase,
    generate_random_chain,
    generate_random_transaction,
    wait_until,
)
from tests.unit.main_test import connect, spend
from tests.unit.p2p.callbacks_test import (
    a_data_node,
    a_filters_node,
    a_parsed_version,
    a_peer,
    a_transaction,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from btclib.tx.tx import Tx

    from btclib_node import Node


class Pending:
    """A verdict the test hands in when it chooses."""

    def __init__(self) -> None:
        """Start not ready."""
        self.done = False
        self.error: Exception | None = None

    def resolve(self, error: Exception | None = None) -> None:
        """Make it ready: `error` is what `get` raises, if any."""
        self.done, self.error = True, error

    def ready(self) -> bool:
        """Answer whether `resolve` ran."""
        return self.done

    def get(self) -> None:
        """Raise the error `resolve` was given, if any."""
        if self.error is not None:
            raise self.error


class Pool:
    """A pool whose script checks wait on a `Pending`; a block's run inline."""

    def __init__(self) -> None:
        """Start with no check."""
        self.checks: list[tuple[Any, Tx, Pending]] = []

    def apply_async(self, function: Any, args: tuple[Any, Tx]) -> Pending:
        """Record the check, and answer its `Pending`."""
        verdict = Pending()
        self.checks.append((function, args[1], verdict))
        return verdict

    def starmap(self, function: Any, tasks: Any) -> list[Any]:
        """Run a block's checks here and now."""
        return [function(*task) for task in tasks]

    def terminate(self) -> None:
        """Nothing runs, so nothing to stop."""

    def join(self) -> None:
        """Nothing runs, so nothing to wait for."""

    def checked(self) -> list[Tx]:
        """Answer the transactions handed over, in order."""
        return [transaction for _, transaction, _ in self.checks]


def a_relay_node(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a node out of initial block download that passes every candidate.

    `pre_verify_calls` records each candidate checked, `received` the
    size of each message logged as received, and `worker_pool` is a
    `Pool`.
    """
    node = a_data_node()
    node.pre_verify_calls = []

    def passes(node: Any, transaction: Tx) -> MempoolCandidate:
        node.pre_verify_calls.append(transaction)
        return MempoolCandidate(0, transaction.vsize, [])

    monkeypatch.setattr(cb, "pre_verify_mempool_acceptance", passes)
    node.p2p_manager.messages = deque()
    node.p2p_manager.handshake_messages = deque()
    node.p2p_manager.pending_connections = {}
    node.worker_pool = Pool()
    logged: list[tuple[Any, ...]] = []
    node.logger.exception = lambda *args: logged.append(args)
    node.logged = logged
    node.received = []

    def log_debug(category: str, msg: str, *args: Any) -> None:
        if msg.startswith("received: "):
            node.received.append(args[1])

    node.logger.log_debug = log_debug
    return node


def a_relay_peer(node: Any, conn_id: int) -> Any:
    """Connect a peer to `node`, with the byte count `handle_p2p` reads."""
    peer = a_peer(
        id=conn_id,
        status=P2pConnStatus.Connected,
        queued_recv_bytes=0,
        _recv_lock=threading.Lock(),
        _recv_resume=SimpleNamespace(set=lambda: None),
        loop=SimpleNamespace(call_soon_threadsafe=lambda fn: fn()),
    )
    node.p2p_manager.connections[conn_id] = peer
    return peer


def send(node: Any, peer: Any, transaction: Tx, queue: str = "messages") -> int:
    """Queue `transaction` from `peer` as `parse_messages` would; handle it."""
    payload = TxMsg(transaction, include_witness=True).serialize()
    peer.queued_recv_bytes += len(payload)
    getattr(node.p2p_manager, queue).append(("tx", payload, peer.id, len(payload), 0.0))
    if queue == "messages":
        handle_p2p(node)
    else:
        handle_p2p_handshake(node)
    return len(payload)


def test_a_check_runs_on_the_pool_and_its_verdict_is_applied_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tx` queues the scripts; `resume_tx_checks` starts, then settles them."""
    node = a_relay_node(monkeypatch)
    peer = a_relay_peer(node, 3)
    transaction = a_transaction()
    send(node, peer, transaction)
    assert node.worker_pool.checks == []
    assert resume_tx_checks(node)
    ((function, checked, verdict),) = node.worker_pool.checks
    assert function is check_transaction
    assert checked == transaction
    assert not resume_tx_checks(node)
    assert not node.mempool.contains_tx(transaction)
    verdict.resolve()
    assert resume_tx_checks(node)
    assert node.mempool.contains_tx(transaction)
    assert node.download_manager.received_txs == [(3, transaction.hash)]
    assert not node.tx_checks.queued
    assert not node.tx_checks.pending(transaction.hash)
    assert not node.tx_checks.pending(transaction.id)


def test_a_second_tx_from_a_peer_waits_for_the_first_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not checked, nor weighed off the peer's receive bound, until then.

    One held message is read per pass, the next waiting behind it, each
    logged as received once it is read.
    """
    node = a_relay_node(monkeypatch)
    peer = a_relay_peer(node, 3)
    first, second, third = a_transaction(), a_transaction(), a_transaction()
    send(node, peer, first)
    resume_tx_checks(node)
    first_size = node.received[0]
    second_size = send(node, peer, second)
    size = second_size + send(node, peer, third)
    assert node.received == [first_size]
    assert node.pre_verify_calls == [first]
    assert peer.queued_recv_bytes == size
    assert not resume_tx_checks(node)
    assert node.pre_verify_calls == [first]
    node.worker_pool.checks[0][2].resolve()
    assert resume_tx_checks(node)
    # settled, its checks run again; then the second read and started
    assert node.pre_verify_calls == [first, first, second]
    assert peer.queued_recv_bytes == size - len(
        TxMsg(second, include_witness=True).serialize()
    )
    assert node.worker_pool.checked() == [first, second]
    assert len(node.tx_checks.waiting[3]) == 1
    assert node.received == [first_size, second_size]


def queue_message(node: Any, peer: Any, msg_type: str, payload: bytes) -> None:
    """Queue a message from `peer` as `parse_messages` would; handle it."""
    peer.queued_recv_bytes += len(payload)
    node.p2p_manager.messages.append((msg_type, payload, peer.id, len(payload), 0.0))
    handle_p2p(node)


def test_a_message_behind_a_tx_is_handled_after_its_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1739: any command waits, so a `pong` follows the verdict.

    Another peer's message does not wait, and a message held weighs on
    its peer's receive bound until it is read, so a flood is paused at
    the bound as any other.
    """
    node = a_relay_node(monkeypatch)
    peer, other = a_relay_peer(node, 3), a_relay_peer(node, 4)
    handled: list[tuple[int, str]] = []

    def recording(command: str) -> Callable[[Node, bytes, Any], None]:
        def record(node: Node, msg: bytes, conn: Any) -> None:
            handled.append((conn.id, command))

        return record

    for command in ("ping", "getdata"):
        monkeypatch.setitem(cb.callbacks, command, recording(command))
    transaction = a_transaction()
    send(node, peer, transaction)
    resume_tx_checks(node)
    queue_message(node, peer, "ping", b"12345678")
    queue_message(node, peer, "getdata", b"\0")
    queue_message(node, other, "ping", b"12345678")
    assert handled == [(4, "ping")]
    assert peer.queued_recv_bytes == len(b"12345678") + len(b"\0")
    assert not resume_tx_checks(node)
    node.worker_pool.checks[0][2].resolve()
    assert resume_tx_checks(node)
    assert node.mempool.contains_tx(transaction)
    assert handled == [(4, "ping"), (3, "ping")]
    assert resume_tx_checks(node)
    assert handled == [(4, "ping"), (3, "ping"), (3, "getdata")]
    assert peer.queued_recv_bytes == 0
    assert not node.tx_checks.waiting


def test_a_message_behind_a_paused_getdata_is_handled_after_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1775: a `pong` follows the items of the `getdata` before the `ping`.

    A second `getdata` waits too, so it is answered after the `pong` and
    the paused entry never holds more than one request. Another peer is
    not held.
    """
    node = a_relay_node(monkeypatch)
    peer, other = a_relay_peer(node, 3), a_relay_peer(node, 4)
    for conn in (peer, other):
        conn.version_message = a_parsed_version(protocol=BIP0031_VERSION + 1)
    transaction = a_transaction()
    node.mempool.add_tx(transaction)
    request = GetData([Inventory(InventoryType.MSG_WTX, transaction.hash)])
    nonce = 12345678
    peer.queued_send_bytes = cb.MAX_GETDATA_INFLIGHT_BYTES
    queue_message(node, peer, "getdata", request.serialize())
    assert not peer.sent
    assert len(node.pending_getdata[3][1]) == 1
    queue_message(node, peer, "ping", Ping(nonce).serialize())
    queue_message(node, peer, "getdata", request.serialize())
    queue_message(node, other, "ping", Ping(nonce).serialize())
    assert [type(sent) for sent in other.sent] == [Pong]
    assert not peer.sent
    assert len(node.pending_getdata[3][1]) == 1
    assert len(node.tx_checks.waiting[3]) == 2
    assert not resume_getdata(node)
    assert not resume_tx_checks(node)
    assert not peer.sent
    peer.queued_send_bytes = 0
    assert resume_getdata(node)
    assert [type(sent) for sent in peer.sent] == [TxMsg]
    assert resume_tx_checks(node)
    assert [type(sent) for sent in peer.sent] == [TxMsg, Pong]
    peer.queued_send_bytes = cb.MAX_GETDATA_INFLIGHT_BYTES
    assert resume_tx_checks(node)
    assert 3 in node.pending_getdata
    assert not resume_tx_checks(node)
    peer.queued_send_bytes = 0
    assert resume_getdata(node)
    assert [type(sent) for sent in peer.sent] == [TxMsg, Pong, TxMsg]
    assert peer.queued_recv_bytes == 0
    assert not node.tx_checks.waiting


def test_a_message_behind_a_paused_getcfilters_is_handled_after_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1789: a `pong` follows the filters of the `getcfilters` before it.

    A second `getcfilters` waits too, so it is answered after the `pong`
    and the paused entry never holds more than one range. Another peer is
    not held.
    """
    node = a_relay_node(monkeypatch)
    filters = a_filters_node(length=8)
    node.chainstate, node.config.peerblockfilters = filters.chainstate, True
    peer, other = a_relay_peer(node, 3), a_relay_peer(node, 4)
    for conn in (peer, other):
        conn.version_message = a_parsed_version(protocol=BIP0031_VERSION + 1)
    stop = node.chainstate.block_index.active_chain
    first = GetCFilters(BlockFilterType.BASIC, 2, stop[3]).serialize()
    second = GetCFilters(BlockFilterType.BASIC, 5, stop[6]).serialize()
    nonce = 12345678
    peer.queued_send_bytes = cb.MAX_CFILTERS_INFLIGHT_BYTES
    queue_message(node, peer, "getcfilters", first)
    assert not peer.sent
    assert len(node.pending_cfilters[3][1]) == 2
    queue_message(node, peer, "ping", Ping(nonce).serialize())
    queue_message(node, peer, "getcfilters", second)
    queue_message(node, other, "ping", Ping(nonce).serialize())
    assert [type(sent) for sent in other.sent] == [Pong]
    assert not peer.sent
    assert len(node.pending_cfilters[3][1]) == 2
    assert len(node.tx_checks.waiting[3]) == 2
    assert not resume_cfilters(node)
    assert not resume_tx_checks(node)
    assert not peer.sent
    peer.queued_send_bytes = 0
    assert resume_cfilters(node)
    assert [type(sent) for sent in peer.sent] == [CFilter, CFilter]
    assert 3 not in node.pending_cfilters
    assert resume_tx_checks(node)
    assert [type(sent) for sent in peer.sent] == [CFilter, CFilter, Pong]
    peer.queued_send_bytes = cb.MAX_CFILTERS_INFLIGHT_BYTES
    assert resume_tx_checks(node)
    assert 3 in node.pending_cfilters
    assert not resume_tx_checks(node)
    peer.queued_send_bytes = 0
    assert resume_cfilters(node)
    sent = [type(message) for message in peer.sent]
    assert sent == [CFilter, CFilter, Pong, CFilter, CFilter]
    assert peer.queued_recv_bytes == 0
    assert not node.tx_checks.waiting


def test_a_handshake_command_behind_a_tx_stops_the_peer_after_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1739: a `sendaddrv2` is handled after the `tx` and `ping` before it.

    Past `verack` it drops the peer, as in Core, which handles what came
    before it and then disconnects.
    """
    node = a_relay_node(monkeypatch)
    peer = a_relay_peer(node, 3)
    handled: list[str] = []
    monkeypatch.setitem(
        cb.callbacks, "ping", lambda node, msg, conn: handled.append("ping")
    )
    transaction = a_transaction()
    send(node, peer, transaction)
    resume_tx_checks(node)
    queue_message(node, peer, "ping", b"12345678")
    queue_message(node, peer, "sendaddrv2", b"")
    assert not peer.stopped
    node.worker_pool.checks[0][2].resolve()
    resume_tx_checks(node)
    assert node.mempool.contains_tx(transaction)
    assert handled == ["ping"]
    assert not peer.stopped
    resume_tx_checks(node)
    assert peer.stopped == [True]


def test_a_tx_behind_the_handshake_waits_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `tx` queued behind a `verack` waits as one in `messages` does."""
    node = a_relay_node(monkeypatch)
    peer = a_relay_peer(node, 3)
    first, second = a_transaction(), a_transaction()
    first_size = send(node, peer, first, "handshake_messages")
    second_size = send(node, peer, second, "handshake_messages")
    assert peer.queued_recv_bytes == second_size
    assert node.received == [first_size]
    assert node.pre_verify_calls == [first]
    assert len(node.tx_checks.waiting[3]) == 1


def test_peers_take_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer that sends again goes behind every peer already queued."""
    node = a_relay_node(monkeypatch)
    a, b = a_relay_peer(node, 1), a_relay_peer(node, 2)
    a1, a2, b1 = a_transaction(), a_transaction(), a_transaction()
    send(node, a, a1)
    send(node, b, b1)
    resume_tx_checks(node)
    send(node, a, a2)
    node.worker_pool.checks[0][2].resolve()
    resume_tx_checks(node)
    assert node.worker_pool.checked() == [a1, b1]
    node.worker_pool.checks[1][2].resolve()
    resume_tx_checks(node)
    assert node.worker_pool.checked() == [a1, b1, a2]


def test_what_a_peer_gone_left_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its queued candidate is never checked, and its held `tx`s never read.

    The check already in flight is settled all the same.
    """
    node = a_relay_node(monkeypatch)
    a, b = a_relay_peer(node, 1), a_relay_peer(node, 2)
    a1, a2, b1 = a_transaction(), a_transaction(), a_transaction()
    send(node, a, a1)
    resume_tx_checks(node)
    send(node, a, a2)
    send(node, b, b1)
    a.status = b.status = P2pConnStatus.Closed
    node.worker_pool.checks[0][2].resolve()
    assert resume_tx_checks(node)
    assert node.mempool.contains_tx(a1)
    assert node.worker_pool.checked() == [a1]
    assert node.pre_verify_calls == [a1, b1, a1]
    assert not node.tx_checks.queued
    assert not node.tx_checks.waiting
    assert not node.tx_checks.pending(b1.hash)
    assert not node.tx_checks.pending(b1.id)


def test_a_tx_kept_meanwhile_is_not_judged_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kept from another peer while its scripts ran, it is left as it is."""
    node = a_relay_node(monkeypatch)
    peer = a_relay_peer(node, 3)
    transaction = a_transaction()
    send(node, peer, transaction)
    resume_tx_checks(node)
    node.mempool.add_tx(transaction)
    node.worker_pool.checks[0][2].resolve()
    assert resume_tx_checks(node)
    assert node.pre_verify_calls == [transaction]
    assert node.download_manager.received_txs == []


def test_a_script_refusal_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not kept, and not checked again when another peer sends it."""
    node = a_relay_node(monkeypatch)
    peer = a_relay_peer(node, 3)
    transaction = a_transaction()
    send(node, peer, transaction)
    resume_tx_checks(node)
    reason = "mempool-script-verify-flag-failed (Signature must be zero)"
    node.worker_pool.checks[0][2].resolve(TxRejectedError(reason))
    resume_tx_checks(node)
    assert not node.mempool.contains_tx(transaction)
    assert node.mempool.was_recently_rejected(transaction.hash)
    send(node, a_relay_peer(node, 4), transaction)
    assert node.pre_verify_calls == [transaction, transaction]


def test_a_settle_that_raises_is_logged_and_the_peer_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As `handle_p2p` logs a callback that raises, the queue moving on."""
    node = a_relay_node(monkeypatch)
    peer = a_relay_peer(node, 3)
    send(node, peer, a_transaction())
    resume_tx_checks(node)
    node.worker_pool.checks[0][2].resolve(RuntimeError("unexpected"))
    assert resume_tx_checks(node)
    ((_, conn_id, verdict),) = node.logged
    assert conn_id == 3
    assert verdict == "peer not discouraged"
    assert not node.p2p_manager.discouraged
    assert not node.tx_checks.queued


def test_a_conflict_accepted_meanwhile_refuses_the_checked_tx(
    regtest_node: Callable[[], Node],
) -> None:
    """The real checks run again once the scripts pass, against the mempool now.

    A spend of the same coin accepted while the scripts ran is a
    conflict, so the checked transaction is refused, not kept beside it.
    """
    node = regtest_node()
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    node.is_initial_block_download = False
    pool = Pool()
    cast("Any", node)._worker_pool = pool
    funding = chain[0].transactions[0]
    checked = generate_random_transaction(
        funding.id, value=funding.vout[0].value - 1000
    )
    conflict = generate_random_transaction(
        funding.id, value=funding.vout[0].value - 1000
    )
    peer = a_peer(id=3, status=P2pConnStatus.Connected)
    cb.tx(node, TxMsg(checked, include_witness=True).serialize(), peer)
    resume_tx_checks(node)
    assert pool.checked() == [checked]
    assert node.mempool.add_tx(conflict, *verify_mempool_acceptance(node, conflict))
    pool.checks[0][2].resolve()
    resume_tx_checks(node)
    assert not node.mempool.contains_tx(checked)
    assert node.mempool.contains_tx(conflict)
    # "insufficient fee", which Core's `PaysForRBF` gives as `TX_RECONSIDERABLE`
    assert node.mempool.was_recently_rejected_reconsiderable(checked.hash)
    assert not node.mempool.was_recently_rejected(checked.hash)


def test_a_coin_a_block_spent_meanwhile_refuses_the_checked_tx(
    regtest_node: Callable[[], Node],
) -> None:
    """A block spending its coin while the scripts ran leaves it missing."""
    node = regtest_node()
    chain = generate_random_chain(COINBASE_MATURITY, RegTest().genesis.hash)
    connect(node, chain)
    node.is_initial_block_download = False
    pool = Pool()
    cast("Any", node)._worker_pool = pool
    funding = chain[0].transactions[0]
    checked = generate_random_transaction(
        funding.id, value=funding.vout[0].value - 1000
    )
    peer = a_peer(id=3, status=P2pConnStatus.Connected)
    cb.tx(node, TxMsg(checked, include_witness=True).serialize(), peer)
    resume_tx_checks(node)
    mined = spend(funding, funding.vout[0].value)
    coinbase = generate_coinbase(height=len(chain) + 1)
    connect(node, [build_block(chain[-1].header.hash, [coinbase, mined], len(chain))])
    pool.checks[0][2].resolve()
    resume_tx_checks(node)
    assert not node.mempool.contains_tx(checked)
    assert not node.mempool.was_recently_rejected(checked.hash)


def test_a_copy_queued_behind_its_twin_is_not_checked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused while the copy waited, the copy is dropped before the pool."""
    node = a_relay_node(monkeypatch)
    a, b = a_relay_peer(node, 1), a_relay_peer(node, 2)
    transaction = a_transaction()
    send(node, a, transaction)
    resume_tx_checks(node)
    send(node, b, transaction)
    reason = "mempool-script-verify-flag-failed (Signature must be zero)"
    node.worker_pool.checks[0][2].resolve(TxRejectedError(reason))
    resume_tx_checks(node)
    assert node.worker_pool.checked() == [transaction]
    assert not node.tx_checks.queued
    assert not node.tx_checks.pending(transaction.hash)
    assert not node.tx_checks.pending(transaction.id)


def exit_worker(prev_outputs: list[Any], tx: Tx) -> None:
    """Exit the pool worker running the check, as a crash would."""
    os._exit(1)  # pragma: no cover -- a pool worker's, which coverage skips


def test_a_check_whose_worker_dies_is_dropped_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logged and dropped, and the next candidate started, on a real pool."""
    monkeypatch.setattr(tx_checks, "check_transaction", exit_worker)
    monkeypatch.setattr(tx_checks, "TX_CHECK_DEADLINE", 1)
    node = a_relay_node(monkeypatch)
    warned: list[tuple[Any, ...]] = []
    node.logger.warning = lambda *args: warned.append(args)
    a, b = a_relay_peer(node, 1), a_relay_peer(node, 2)
    first, second = a_transaction(), a_transaction()
    with multiprocessing.get_context("spawn").Pool(1) as pool:
        (worker,) = cast("Any", pool)._pool
        node.worker_pool = pool
        send(node, a, first)
        send(node, b, second)
        resume_tx_checks(node)
        wait_until(lambda: not worker.is_alive())
        wait_until(lambda: resume_tx_checks(node) and warned)
        ((_, wtxid, conn_id, _),) = warned
        assert (wtxid, conn_id) == (first.hash.hex(), 1)
        assert list(node.tx_checks.queued) == [2]
        assert node.tx_checks.checking
        assert not node.tx_checks.pending(first.hash)
        assert not node.tx_checks.pending(first.id)
