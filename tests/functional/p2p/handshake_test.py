# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Two real nodes complete a handshake, and a node refuses one with itself."""

from typing import TYPE_CHECKING, override

import pytest

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus, P2pConnStatus
from btclib_node.main import update_chain
from btclib_node.p2p.connection import Connection
from tests import (
    WaitTimeoutError,
    generate_random_chain,
    get_random_port,
    local_addr,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _wait_or_describe(
    predicate: Callable[[], object],
    describe: Callable[[], str],
    timeout: float = 60,
) -> None:
    """`wait_until(predicate)`, naming the state on a timeout.

    `describe` is read only once the wait has already failed, so it
    costs nothing on the pass this test expects.
    """
    try:
        wait_until(predicate, timeout=timeout)
    except WaitTimeoutError as exc:
        msg = f"{exc} -- {describe()}"
        raise WaitTimeoutError(msg) from exc


def test_a_stalled_wait_names_what_it_was_waiting_for() -> None:
    """`_wait_or_describe`'s own timeout names whatever `describe` says.

    Tripped directly rather than by forcing the real 600-second hang
    ISS 1020 is about: what is under test here is that the helper reads
    `describe` and appends it, not the race it would one day describe.
    """
    with pytest.raises(WaitTimeoutError, match="created 1 of 2"):
        _wait_or_describe(lambda: False, lambda: "created 1 of 2", timeout=0.05)


class _RecordingPendingConnections(dict[int, Connection]):
    """`P2pManager.pending_connections`, plus which connections it ever held.

    `pending_connections` itself rises and falls -- a connection is
    added and then promoted or dropped out of it -- so polling its own
    live length past a peak is exactly the race issue #1020's own
    poison harness reproduced deterministically: a sample can land
    before the peak and the next after it, and the predicate is then
    never true again, at any bound, while the connection underneath it
    was made and torn down correctly the whole time.
    `tests/functional/p2p/pruning_test.py`'s own `_RecordingDeque` is
    the same idea for a queue that drains; this is a dict that empties
    the same way. `created` only ever grows, recording each
    connection's own `inbound` the moment it is added, so waiting on
    its length is monotone and cannot be walked past.
    """

    def __init__(self) -> None:
        """Start empty: nothing has been created before the first insert."""
        super().__init__()
        self.created: list[bool] = []

    @override
    def __setitem__(self, key: int, value: Connection) -> None:
        self.created.append(value.inbound)
        super().__setitem__(key, value)


def test_simple_connection(tmp_path: Path) -> None:
    """Two real nodes, connected over a socket, each reach `Connected`.

    Each side's own handshake completes independently -- `connections`
    on either node only holds the peer once its own `verack` has been
    processed -- so both are waited for on their own rather than one
    being assumed once the other is seen.
    """
    node1 = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node1",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    node2 = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node2",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    node1.start()
    node2.start()

    wait_until_listening(node1.p2p_manager)
    wait_until_listening(node2.p2p_manager)

    node2.p2p_manager.connect(local_addr(node1.p2p_port))
    # each side's own `connections` only holds a peer past its own
    # `verack`, and the two handshakes complete independently, so each
    # is waited for on its own rather than assuming one implies the other
    wait_until(lambda: len(node1.p2p_manager.connections))
    connection = node1.p2p_manager.connections[0]
    wait_until(lambda: connection.status == P2pConnStatus.Connected)
    wait_until(lambda: len(node2.p2p_manager.connections))
    connection = node2.p2p_manager.connections[0]
    wait_until(lambda: connection.status == P2pConnStatus.Connected)

    node1.stop()
    node2.stop()


def test_a_connecting_node_carries_its_own_real_tip_height(tmp_path: Path) -> None:
    """A node past genesis carries its own real height (closes #722).

    `chain_length` blocks are added and validated through a real
    `update_chain` loop, the same shape
    `tests/functional/p2p/pruning_test.py`'s own fixture builds a synced
    server with, so `node1.best_height` is proven driven by
    `main._finalize_fork` itself rather than written directly by this
    test -- and the peer this connects to reads it off the wire, not off
    `node1`'s own attribute, so what is checked is what `send_version`
    actually put in the `version` message.
    """
    chain_length = 5
    chain = generate_random_chain(chain_length, RegTest().genesis.hash)
    node1 = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node1",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    block_index = node1.chainstate.block_index
    block_index.add_headers([block.header for block in chain])
    node1.status = NodeStatus.HeaderSynced
    for block in chain:
        node1.block_db.add_block(block)
        block_index.set_downloaded(block.header.hash)
    for _ in range(len(chain)):
        update_chain(node1)
    assert node1.best_height == chain_length
    node1.chainstate.flush()
    node1.start()
    wait_until_listening(node1.p2p_manager)

    node2 = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node2",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    node2.start()
    wait_until_listening(node2.p2p_manager)

    node2.p2p_manager.connect(local_addr(node1.p2p_port))
    wait_until(lambda: len(node2.p2p_manager.connections))
    connection = node2.p2p_manager.connections[0]
    wait_until(lambda: connection.status == P2pConnStatus.Connected)

    version = connection.version_message
    assert version is not None
    assert version.start_height == chain_length

    node1.stop()
    node2.stop()


def test_connection_to_ourselves(tmp_path: Path) -> None:
    """A node that dials its own address drops the connection, never adds it.

    `p2p.callbacks.version` recognises its own nonce and stops the
    connection there, before `verack` could ever promote it into
    `connections` -- so `pending_connections` emptying out, not
    `connections` staying at zero, is what proves the drop actually
    happened rather than the connection never having been attempted.
    Two connections are waited to have been *created* first, because
    the loopback dial reaches this same node's listener too, and each
    side of that pair has to exist before the drain that follows means
    anything -- read off `_RecordingPendingConnections.created` and not
    off `pending_connections`'s own live length, which rises to 2 and
    falls again as the self-connect is torn down, both inside one
    uninterrupted burst of activity on the manager's own thread with
    nothing in this test to catch it between the two (issue #1020).
    """
    node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path,
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    recording = _RecordingPendingConnections()
    node.p2p_manager.pending_connections = recording
    node.start()

    wait_until_listening(node.p2p_manager)

    node.p2p_manager.connect(local_addr(node.p2p_port))

    _wait_or_describe(
        lambda: len(recording.created) >= 2,
        lambda: f"created {len(recording.created)} of 2, inbound={recording.created}",
    )
    # a connection to itself is stopped inside `version`, before its own
    # `verack` could ever promote it: it never reaches `connections`, so
    # `pending_connections` draining to empty -- and staying there -- is
    # what proves the drop actually happened. Safe to poll for, unlike
    # the peak above, and not because no dialling runs: with no
    # `connect=` in this `Config`, `use_addrman_outgoing` is true and
    # `_maybe_dial_more_peers` is passing every 100 ms for the whole
    # life of the test. It simply never finds an address to draw.
    # That loop returns on `peer_db.is_empty`, which reads `addresses`
    # -- every endpoint heard about -- and not `active_addresses`, the
    # separate table `callbacks.verack` fills through
    # `add_active_address`, so whether this handshake reaches `verack`
    # decides nothing here. `addresses` gains entries from three places
    # and this test drives none of them: `init_from_db` loads a datadir
    # that `tmp_path` has just created empty, `callbacks.addr` and
    # `callbacks.addrv2` need a peer to gossip and the one connection
    # attempted is the self-connect refused above, and
    # `get_addr_from_dns` iterates `RegTest.addresses`, which is empty
    # (`chains.py`). `discourage` on the self-connect is a second
    # filter, never reached in this test because `is_empty` returns
    # first. A test that later gains a `connect=`, an `addnode`, a
    # gossiping peer or a seeded chain is standing on all of that and
    # has to re-establish it for itself.
    _wait_or_describe(
        lambda: not len(node.p2p_manager.pending_connections),
        lambda: (
            f"pending_connections still holds "
            f"{len(node.p2p_manager.pending_connections)}, inbound="
            f"{sorted(c.inbound for c in node.p2p_manager.pending_connections.values())}"
        ),
    )
    assert not node.p2p_manager.connections

    node.stop()
