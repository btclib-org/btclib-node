# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The stop RPC, and a node's own shutdown through it, over a real node."""

import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.rpc.callbacks import callbacks
from btclib_node.rpc.manager import RpcManager
from tests import (
    ListenerEndedError,
    generate_random_chain,
    get_random_port,
    rpc_client,
    taken_loopbacks,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_a_listener_that_cannot_bind_is_reported_at_once(tmp_path: Path) -> None:
    """ISS 1361: a taken port ends the wait with the failure, not the timeout.

    The node's RPC bind fails on every loopback it tries, held by
    `taken_loopbacks` -- `::1` and `127.0.0.1` both, as the listener
    itself binds -- and its manager's thread ends: waiting the twenty
    seconds out would report the failure as a listener too slow to come
    up.
    """
    with taken_loopbacks() as port:
        node = Node(
            config=Config(
                chain="regtest", data_dir=tmp_path, allow_p2p=False, rpc_port=port
            )
        )
        node.start()
        start = time.monotonic()
        try:
            with pytest.raises(ListenerEndedError, match=f"port {port} ended"):
                wait_until_listening(node.rpc_manager)
        finally:
            node.stop()
    assert time.monotonic() - start < 10


def test_init(tmp_path: Path) -> None:
    """`stop` answers before the node goes down, then the node stops for real.

    A port of its own; see `tests/functional/p2p/init_test.py`.
    """
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
        )
    )
    node.start()
    try:
        wait_until_listening(node.rpc_manager)

        _, body = rpc_client(node).call_raw("stop", jsonrpc="1.0", request_timeout=2)

        assert body["result"] == "Btclib node stopping"
    finally:
        node.stop()

    # the node was already asked to stop from inside its own loop,
    # which is the one caller that cannot wait for it; asking again
    # from outside is what waits
    assert not node.is_alive()


def test_a_slow_manager_start_cannot_still_clobber_the_status_it_raced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wait_until_listening` returning implies `status` is already set.

    btclib-org/btclib-node#398: `Node.run` used to start the managers and
    only then assign `self.status`, on `Node`'s own thread; a manager's
    `listening` event is set on the manager's own thread and said nothing
    about whether that assignment had already run. Widening the window
    between `start()` returning and the assignment after it -- standing in
    for `Node`'s thread being descheduled there -- reproduces the race a
    test's own `node.status = NodeStatus.HeaderSynced` used to lose:
    `Node`'s late write landed after it and put `status` back to
    `SyncingHeaders`, and `_ready_fork` never returns past that again.
    """
    original_start = RpcManager.start

    def slow_start(self: RpcManager) -> None:
        original_start(self)
        time.sleep(0.3)

    monkeypatch.setattr(RpcManager, "start", slow_start)

    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
        )
    )
    node.start()
    try:
        wait_until_listening(node.rpc_manager)

        chain = generate_random_chain(1, RegTest().genesis.hash)
        block_index = node.chainstate.block_index
        block_index.add_headers([block.header for block in chain])
        node.status = NodeStatus.HeaderSynced
        for block in chain:
            node.block_db.add_block(block)
            block_index.set_downloaded(block.header.hash)

        # The chain growing and the status reaching `BlockSynced` are
        # two writes on the node's own thread, in that order and a few
        # statements apart -- `update_chain` commits the fork, then
        # calls `finish_sync` -- so waiting on the first and sampling
        # the second reads the status the node had before it got there:
        # btclib-org/btclib-node#525. The status is what this test is
        # about, and waiting on it is what says the clobber did not
        # happen. It still fails where the clobber does happen, as a
        # timeout rather than as an assertion: `_ready_fork` returns at
        # its own first guard for anything below `HeaderSynced`, so
        # `finish_sync` is never reached again and the wait runs out.
        wait_until(lambda: node.status == NodeStatus.BlockSynced)
    finally:
        node.stop()


def test_a_request_queued_when_shutdown_starts_is_still_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1506: a request already queued when shutdown starts is not closed.

    Core's `ThreadPool::Stop` runs every task already on its own work
    queue -- "Help draining queue", `while (ProcessTask()) {}`,
    `src/util/threadpool.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag -- before `StopHTTPServer` unlistens its sockets
    (`src/httpserver.cpp`, same tag), so a request already parsed when
    shutdown begins is executed and answered rather than left on a
    connection that is about to close.

    `getbestblockhash` is held inside its own handler on an event while
    still on `Node`'s own thread, a `getblockcount` is sent and queues
    behind it on `rpc_manager.messages`, and `node.stop()` runs from
    another thread while the first handler is still blocked -- reaching
    `terminate_flag.set()` before the handler, and so before
    `getblockcount`, is ever released. `getblockcount` must still get an
    answer once released, not a connection closed with no reply.
    """
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            allow_p2p=False,
            rpc_port=get_random_port(),
        )
    )

    entered = threading.Event()
    release = threading.Event()
    original = callbacks["getbestblockhash"]

    def blocking_get_best_block_hash(node: Any, conn: Any, params: Any) -> Any:
        entered.set()
        release.wait(10)
        return original(node, conn, params)

    monkeypatch.setitem(callbacks, "getbestblockhash", blocking_get_best_block_hash)

    node.start()
    try:
        wait_until_listening(node.rpc_manager)

        first = threading.Thread(
            target=lambda: rpc_client(node).call_raw("getbestblockhash"),
            daemon=True,
        )
        first.start()
        wait_until(entered.is_set)

        second_status: list[int] = []
        second_body: list[Any] = []

        def call_second() -> None:
            status, body = rpc_client(node).call_raw("getblockcount")
            second_status.append(status)
            second_body.append(body)

        second = threading.Thread(target=call_second, daemon=True)
        second.start()
        wait_until(lambda: len(node.rpc_manager.messages) == 1)

        stop_thread = threading.Thread(target=node.stop, daemon=True)
        stop_thread.start()
        wait_until(node.terminate_flag.is_set)

        release.set()
        first.join(15)
        second.join(15)
        stop_thread.join(15)

        assert second_status == [200]
        assert second_body[0]["result"] == 0
    finally:
        node.stop()
