# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A node meets a peer's chain through its low-work headers sync (ISS 1246).

`node_a` asks for more work than regtest does; `node_b` holds a real
chain on plain regtest and serves its headers. `MAX_HEADERS_RESULTS` is
lowered for both, in this one process, so that a short chain still comes
in full batches, which is what starts a sync at all.
"""

from contextlib import ExitStack
from dataclasses import replace
from typing import TYPE_CHECKING, override

from btclib.p2p.inventory import Headers

import btclib_node.p2p.callbacks as cb
from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.chainstate.block_index import calculate_work
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from tests import (
    generate_random_chain,
    get_random_port,
    local_addr,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from btclib.consensus import ConsensusParams

    from btclib_node.p2p.connection import Connection

_BATCH = 10
_LENGTH = 25


class _Demanding(RegTest):
    """Regtest, asking `blocks` blocks of work where regtest asks none."""

    def __init__(self, blocks: int) -> None:
        super().__init__()
        self.blocks = blocks

    @property
    @override
    def consensus(self) -> ConsensusParams:
        work = calculate_work(self.genesis)
        return replace(super().consensus, minimum_chain_work=self.blocks * work)


def _node(tmp_path: Path, name: str, chain: RegTest) -> Node:
    node = Node(
        config=Config(
            chain=chain,
            data_dir=tmp_path / name,
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    node.load()
    return node


def _sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required_blocks: int
) -> tuple[Node, Node, list[tuple[int, bool]], ExitStack]:
    """Start `node_a` and `node_b`, `node_a` dialling, and return them running.

    Every `headers` `node_a` handles is recorded as its size and whether a
    sync was left running with the peer.
    """
    monkeypatch.setattr(cb, "MAX_HEADERS_RESULTS", _BATCH)
    handled: list[tuple[int, bool]] = []
    original = cb.callbacks["headers"]

    def recording(node: Node, msg: bytes, conn: Connection) -> None:
        try:
            original(node, msg, conn)
        finally:
            if node is node_a:
                size = len(Headers.parse(msg, check_validity=False).headers)
                handled.append((size, conn.headers_sync is not None))

    monkeypatch.setitem(cb.callbacks, "headers", recording)
    chain = generate_random_chain(_LENGTH, RegTest().genesis.hash)
    node_b = _node(tmp_path, "node_b", RegTest())
    block_index = node_b.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    for block in chain:
        node_b.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    node_a = _node(tmp_path, "node_a", _Demanding(required_blocks))
    stack = ExitStack()
    for node in (node_b, node_a):
        node.start()
        stack.callback(node.stop)
        wait_until_listening(node.p2p_manager)
    wait_until(lambda: len(node_b.chainstate.block_index.active_chain) == _LENGTH + 1)
    node_a.p2p_manager.connect(local_addr(node_b.p2p_port))
    return node_a, node_b, handled, stack


def test_a_chain_below_the_threshold_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presync runs to the chain's end and stops, with only genesis indexed.

    `node_b`'s whole chain is lighter than `node_a` asks: its full batches
    are counted and kept out, its short last one ends the sync, and
    `node_b` is still connected and not discouraged.
    """
    node_a, _, handled, stack = _sync(tmp_path, monkeypatch, _LENGTH + 10)
    with stack:
        wait_until(lambda: (_LENGTH % _BATCH, False) in handled)
        assert handled[:2] == [(_BATCH, True), (_BATCH, True)]
        assert list(node_a.chainstate.block_index.header_dict) == [
            RegTest().genesis.hash
        ]
        (conn,) = node_a.p2p_manager.connections.values()
        assert conn.headers_sync is None
        assert not node_a.p2p_manager.is_discouraged(conn.address)
        assert node_a.status == NodeStatus.SyncingHeaders


def test_a_chain_clearing_the_threshold_is_synced_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presync reaches the work, the redownload is indexed, then the blocks."""
    node_a, _, handled, stack = _sync(tmp_path, monkeypatch, _LENGTH - 5)
    with stack:
        block_index = node_a.chainstate.block_index
        wait_until(lambda: len(block_index.active_chain) == _LENGTH + 1)
        # two batches of presync, then the redownload from genesis
        assert handled[:3] == [(_BATCH, True), (_BATCH, True), (_BATCH, True)]
        (conn,) = node_a.p2p_manager.connections.values()
        assert conn.headers_sync is None
