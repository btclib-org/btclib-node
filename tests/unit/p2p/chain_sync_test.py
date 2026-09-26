# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.p2p.chain_sync`, Core's `ConsiderEviction`."""

from types import SimpleNamespace
from typing import Any

import pytest

from btclib_node.constants import P2pConnStatus
from btclib_node.p2p.block_availability import BlockAvailability
from btclib_node.p2p.chain_sync import (
    CHAIN_SYNC_TIMEOUT,
    HEADERS_RESPONSE_TIME,
    MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT,
    ChainSyncTimeoutState,
    consider_eviction,
    protect_if_caught_up,
)
from btclib_node.p2p.chain_sync import _locator as locator_of


def a_hash(height: int) -> bytes:
    """Name the block at `height` of the test chain, never all zeros."""
    return (height + 1).to_bytes(32, "big")


def a_node(tip_height: int, *conns: Any, sync_started: bool = True) -> Any:
    """Build a node whose active chain is `tip_height` blocks tall.

    A block's chain work is its height plus one, so a peer's best known
    block has the tip's work exactly where it is the tip.
    """
    active_chain = [a_hash(h) for h in range(tip_height + 1)]
    header_dict = {
        a_hash(h): SimpleNamespace(
            index=h,
            header=SimpleNamespace(
                previous_block_hash=a_hash(h - 1) if h else b"\x00" * 32
            ),
        )
        for h in range(tip_height + 1)
    }
    block_index = SimpleNamespace(
        active_chain=active_chain,
        header_index=list(active_chain),
        header_index_pos={block: h for h, block in enumerate(active_chain)},
        header_dict=header_dict,
        chainwork={a_hash(h): h + 1 for h in range(tip_height + 1)},
    )
    logged: list[str] = []
    return SimpleNamespace(
        chainstate=SimpleNamespace(block_index=block_index),
        download_manager=SimpleNamespace(
            headers_sync_timeouts={conn.id: 0.0 for conn in conns if sync_started}
        ),
        p2p_manager=SimpleNamespace(connections={conn.id: conn for conn in conns}),
        logger=SimpleNamespace(info=lambda *args: logged.append(args[0])),
        logged=logged,
    )


def a_conn(conn_id: int = 1, *, best: int | None = None, automatic: bool = True) -> Any:
    """Build an outbound connection whose best known block is at `best`."""
    stopped: list[bool] = []
    return SimpleNamespace(
        id=conn_id,
        automatic=automatic,
        status=P2pConnStatus.Connected,
        chain_sync=ChainSyncTimeoutState(),
        block_availability=BlockAvailability(
            best_known=None if best is None else a_hash(best)
        ),
        stop=lambda: stopped.append(True),
        stopped=stopped,
    )


def a_recorder() -> tuple[list[tuple[int, list[bytes]]], Any]:
    """Return what `send_getheaders` was called with, and the function."""
    calls: list[tuple[int, list[bytes]]] = []

    def send_getheaders(node: Any, conn: Any, locator: list[bytes]) -> bool:
        calls.append((conn.id, locator))
        return True

    return calls, send_getheaders


def test_a_peer_behind_the_tip_is_asked_once_then_dropped() -> None:
    """ISS 1154: `CHAIN_SYNC_TIMEOUT`, one `getheaders`, then the drop.

    The unit test the issue names: a mocked clock past 22 minutes.
    """
    conn = a_conn(best=5)
    node = a_node(10, conn)
    calls, send = a_recorder()
    start = 1000.0
    consider_eviction(node, conn, start, send)
    assert conn.chain_sync == ChainSyncTimeoutState(
        timeout=start + CHAIN_SYNC_TIMEOUT, work_header=a_hash(10)
    )
    consider_eviction(node, conn, start + CHAIN_SYNC_TIMEOUT, send)
    assert not calls
    asked = start + CHAIN_SYNC_TIMEOUT + 1
    consider_eviction(node, conn, asked, send)
    block_index = node.chainstate.block_index
    assert calls == [(1, locator_of(block_index, a_hash(9)))]
    assert conn.chain_sync.sent_getheaders
    assert conn.chain_sync.timeout == asked + HEADERS_RESPONSE_TIME
    consider_eviction(node, conn, asked + HEADERS_RESPONSE_TIME, send)
    assert not conn.stopped
    consider_eviction(node, conn, asked + HEADERS_RESPONSE_TIME + 1, send)
    assert conn.stopped == [True]
    assert len(calls) == 1
    (logged,) = node.logged
    assert logged.startswith("Outbound peer has old chain")


def test_a_peer_that_knows_no_block_is_behind_the_tip() -> None:
    """ISS 1154: Core's null `pindexBestKnownBlock` is behind any tip."""
    conn = a_conn(best=None)
    node = a_node(3, conn)
    consider_eviction(node, conn, 100.0, a_recorder()[1])
    assert conn.chain_sync.timeout == 100.0 + CHAIN_SYNC_TIMEOUT


def test_a_peer_catching_up_to_the_tip_clears_its_deadline() -> None:
    """ISS 1154: the timeout, work header and `getheaders` flag all reset."""
    conn = a_conn(best=5)
    node = a_node(10, conn)
    conn.chain_sync = ChainSyncTimeoutState(
        timeout=50.0, work_header=a_hash(10), sent_getheaders=True
    )
    conn.block_availability.best_known = a_hash(10)
    consider_eviction(node, conn, 60.0, a_recorder()[1])
    assert conn.chain_sync == ChainSyncTimeoutState()
    assert not conn.stopped


def test_a_peer_at_the_tip_with_no_deadline_is_left_be() -> None:
    """ISS 1154: nothing to reset for a peer that was never behind."""
    conn = a_conn(best=10)
    node = a_node(10, conn)
    consider_eviction(node, conn, 60.0, a_recorder()[1])
    assert conn.chain_sync == ChainSyncTimeoutState()


def test_a_peer_at_the_old_tip_s_work_gets_a_new_deadline() -> None:
    """ISS 1154: the tip moved on, so the deadline restarts against it."""
    conn = a_conn(best=10)
    node = a_node(12, conn)
    conn.chain_sync = ChainSyncTimeoutState(
        timeout=50.0, work_header=a_hash(10), sent_getheaders=True
    )
    consider_eviction(node, conn, 60.0, a_recorder()[1])
    assert conn.chain_sync == ChainSyncTimeoutState(
        timeout=60.0 + CHAIN_SYNC_TIMEOUT, work_header=a_hash(12)
    )


def test_a_deadline_not_yet_passed_does_nothing() -> None:
    """ISS 1154: before the timeout, a peer still behind is left be."""
    conn = a_conn(best=5)
    node = a_node(10, conn)
    state = ChainSyncTimeoutState(timeout=500.0, work_header=a_hash(10))
    conn.chain_sync = ChainSyncTimeoutState(**vars(state))
    calls, send = a_recorder()
    consider_eviction(node, conn, 400.0, send)
    assert conn.chain_sync == state
    assert not calls


@pytest.mark.parametrize(
    ("protect", "automatic", "sync_started"),
    [(True, True, True), (False, False, True), (False, True, False)],
    ids=["protected", "manual-or-inbound", "sync-not-started"],
)
def test_a_peer_outside_core_s_condition_is_never_considered(
    *, protect: bool, automatic: bool, sync_started: bool
) -> None:
    """ISS 1154: not protected, outbound, and with header sync started."""
    conn = a_conn(best=1, automatic=automatic)
    conn.chain_sync.protect = protect
    node = a_node(10, conn, sync_started=sync_started)
    consider_eviction(node, conn, 100.0, a_recorder()[1])
    assert conn.chain_sync.timeout == 0


def test_the_locator_is_core_s_locator_entries() -> None:
    """Ten blocks back one by one, then doubling, then genesis."""
    node = a_node(30)
    heights = [30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 17, 13, 5, 0]
    block_index = node.chainstate.block_index
    assert locator_of(block_index, a_hash(30)) == [a_hash(h) for h in heights]
    assert locator_of(block_index, a_hash(0)) == [a_hash(0)]


def a_fork_hash(height: int) -> bytes:
    """Name the block at `height` of a competing branch."""
    return b"\xff" + height.to_bytes(31, "big")


@pytest.mark.parametrize("fork_tip", [8, 10, 12], ids=["shorter", "equal", "taller"])
def test_the_getheaders_follows_a_tip_reorged_out_since(fork_tip: int) -> None:
    """ISS 1154: Core's `GetLocator(m_work_header->pprev)` walks ancestors.

    The tip measured against, height 10, has since left the active chain
    for a branch forking at 7 with more work, shorter, as tall or taller.
    """
    conn = a_conn(best=None)
    node = a_node(10, conn)
    block_index = node.chainstate.block_index
    branch = [a_fork_hash(h) for h in range(8, fork_tip + 1)]
    for height, block in zip(range(8, fork_tip + 1), branch, strict=True):
        parent = a_hash(7) if height == 8 else a_fork_hash(height - 1)
        block_index.header_dict[block] = SimpleNamespace(
            index=height, header=SimpleNamespace(previous_block_hash=parent)
        )
        block_index.chainwork[block] = 100 + height
    block_index.active_chain = [a_hash(h) for h in range(8)] + branch
    block_index.header_index = list(block_index.active_chain)
    block_index.header_index_pos = {
        block: h for h, block in enumerate(block_index.header_index)
    }
    conn.chain_sync = ChainSyncTimeoutState(timeout=10.0, work_header=a_hash(10))
    calls, send = a_recorder()
    consider_eviction(node, conn, 11.0, send)
    assert calls == [(1, [a_hash(h) for h in range(9, -1, -1)])]


def test_a_tip_at_genesis_is_asked_about_with_an_empty_locator() -> None:
    """Core's `GetLocator(nullptr)`: genesis has no parent to start from."""
    conn = a_conn(best=None)
    node = a_node(0, conn)
    conn.chain_sync = ChainSyncTimeoutState(timeout=10.0, work_header=a_hash(0))
    calls, send = a_recorder()
    consider_eviction(node, conn, 11.0, send)
    assert calls == [(1, [])]


def test_up_to_four_outbound_peers_at_the_tip_are_protected() -> None:
    """ISS 1154: `MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT`."""
    conns = [a_conn(i, best=10) for i in range(6)]
    node = a_node(10, *conns)
    for conn in conns:
        protect_if_caught_up(node, conn)
    protected = [conn.chain_sync.protect for conn in conns]
    limit = MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT
    assert protected == [True] * limit + [False] * (len(conns) - limit)


@pytest.mark.parametrize(
    ("best", "automatic", "status"),
    [
        (9, True, P2pConnStatus.Connected),
        (None, True, P2pConnStatus.Connected),
        (10, False, P2pConnStatus.Connected),
        (10, True, P2pConnStatus.Closed),
    ],
    ids=["behind", "nothing-known", "not-drawn", "disconnecting"],
)
def test_a_peer_is_protected_only_as_core_protects_it(
    *, best: int | None, automatic: bool, status: P2pConnStatus
) -> None:
    """ISS 1154: a full outbound peer, not disconnecting, at the tip's work."""
    conn = a_conn(best=best, automatic=automatic)
    conn.status = status
    node = a_node(10, conn)
    protect_if_caught_up(node, conn)
    assert not conn.chain_sync.protect
