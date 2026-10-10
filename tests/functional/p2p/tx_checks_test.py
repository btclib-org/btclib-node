# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A relayed transaction's script check leaves the node's loop free."""

import threading
import time
from multiprocessing.pool import Pool, ThreadPool
from typing import TYPE_CHECKING, Any, cast

from btclib.p2p.data import TxPayload as TxMsg
from btclib.p2p.keepalive import Ping, Pong

import btclib_node.p2p.callbacks as cb
from btclib_node.constants import P2pConnStatus
from btclib_node.main import MempoolCandidate
from btclib_node.p2p import tx_checks
from btclib_node.p2p.callbacks import callbacks
from tests import (
    generate_random_transaction,
    local_addr,
    wait_until,
    wait_until_listening,
)
from tests.conftest import node_context

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from btclib.tx.tx import Tx
    from btclib.tx.tx_out import TxOut

    from btclib_node import Node
    from btclib_node.p2p.connection import Connection


# no nonce either node draws for a ping of its own
_NONCE = 0x5EED


def endless_check(prev_outputs: list[TxOut], tx: Tx) -> None:
    """Check no script, and outlast the node running the check."""
    time.sleep(600)  # pragma: no cover -- a pool worker's, which coverage skips


def connected(node1: Node, node2: Node) -> int:
    """Connect `node2` to `node1`; answer `node2`'s connection id."""
    wait_until_listening(node1.p2p_manager)
    wait_until_listening(node2.p2p_manager)
    node2.p2p_manager.connect(local_addr(node1.p2p_port))
    wait_until(lambda: len(node1.p2p_manager.connections))
    wait_until(lambda: len(node2.p2p_manager.connections))
    (conn_id,) = node2.p2p_manager.connections
    conn = node2.p2p_manager.connections[conn_id]
    wait_until(lambda: conn.status == P2pConnStatus.Connected)
    (conn1,) = node1.p2p_manager.connections.values()
    wait_until(lambda: conn1.status == P2pConnStatus.Connected)
    return conn_id


def test_a_ping_waits_for_the_verdict_on_the_tx_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1739: a `pong` follows the verdict, and another peer's needs no wait.

    Every check but the scripts is stood in for, and the scripts by a
    check that waits for the test to release it. The second peer's `ping`
    is answered while the first peer's check runs, so the loop is free.
    """
    monkeypatch.setattr(
        cb,
        "pre_verify_mempool_acceptance",
        lambda node, tx: MempoolCandidate(0, tx.vsize, []),
    )
    started, release = threading.Event(), threading.Event()

    def held(prev_outputs: list[TxOut], tx: Tx) -> None:
        started.set()
        release.wait(timeout=120)

    monkeypatch.setattr(tx_checks, "check_transaction", held)
    answered: list[tuple[Node, int]] = []
    original = callbacks["pong"]

    def recording(node: Node, msg: bytes, conn: Connection) -> None:
        original(node, msg, conn)
        answered.append((node, Pong.parse(msg).nonce))

    monkeypatch.setitem(callbacks, "pong", recording)
    with (
        node_context(tmp_path / "node1", allow_rpc=False) as node1,
        node_context(tmp_path / "node2", allow_rpc=False) as node2,
        node_context(tmp_path / "node3", allow_rpc=False) as node3,
    ):
        node1._worker_pool = ThreadPool(1)
        node1.is_initial_block_download = False
        conn_id = connected(node1, node2)
        node3.p2p_manager.connect(local_addr(node1.p2p_port))
        wait_until(lambda: len(node1.p2p_manager.connections) == 2)
        wait_until(lambda: len(node3.p2p_manager.connections) == 1)
        (other_id,) = node3.p2p_manager.connections
        wait_until(
            lambda: all(
                conn.status == P2pConnStatus.Connected
                for conn in (
                    *node1.p2p_manager.connections.values(),
                    *node3.p2p_manager.connections.values(),
                )
            )
        )
        try:
            transaction = generate_random_transaction()
            node2.p2p_manager.send(TxMsg(transaction, include_witness=True), conn_id)
            assert started.wait(timeout=60)
            node2.p2p_manager.send(Ping(_NONCE), conn_id)
            node3.p2p_manager.send(Ping(_NONCE + 1), other_id)
            wait_until(lambda: (node3, _NONCE + 1) in answered)
            assert (node2, _NONCE) not in answered
            assert not node1.mempool.contains_tx(transaction)
        finally:
            release.set()
        wait_until(lambda: (node2, _NONCE) in answered)
        assert node1.mempool.contains_tx(transaction)


def test_a_node_stops_with_a_script_check_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stop` returns, the worker running the check terminated with the pool."""
    monkeypatch.setattr(
        cb,
        "pre_verify_mempool_acceptance",
        lambda node, tx: MempoolCandidate(0, tx.vsize, []),
    )
    monkeypatch.setattr(tx_checks, "check_transaction", endless_check)
    with (
        node_context(tmp_path / "node1", allow_rpc=False) as node1,
        node_context(tmp_path / "node2", allow_rpc=False) as node2,
    ):
        pool = Pool(1)
        (worker,) = cast("Any", pool)._pool
        node1._worker_pool = pool
        node1.is_initial_block_download = False
        conn_id = connected(node1, node2)
        transaction = generate_random_transaction()
        node2.p2p_manager.send(TxMsg(transaction, include_witness=True), conn_id)
        wait_until(lambda: node1.tx_checks.checking)
        node1.stop()
        assert not node1.is_alive()
        assert cast("Any", node1)._worker_pool is None
        assert not worker.is_alive()
        assert not node1.mempool.contains_tx(transaction)
