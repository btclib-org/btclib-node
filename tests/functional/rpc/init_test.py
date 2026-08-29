# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The stop RPC, and a node's own shutdown through it, over a real node."""

import time
from typing import TYPE_CHECKING

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.rpc.manager import RpcManager
from tests import (
    generate_random_chain,
    get_random_port,
    rpc_client,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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

    wait_until_listening(node.rpc_manager)

    _, body = rpc_client(node).call_raw("stop", jsonrpc="1.0", request_timeout=2)

    assert body["result"] == "Btclib node stopping"

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
