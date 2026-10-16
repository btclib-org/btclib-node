# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A node among several peers, some of them lying about their chain (ISS 1894).

The peers are `ScriptedPeer`s on real sockets. Every liar is met before
the honest peer, the only order that puts it to the proof, as the
package docstring of `tests/functional/p2p/__init__.py` says.

What Core does with a peer that tells each lie, at v31.1
(bitcoin/bitcoin@9be056a8a7), `src/net_processing.cpp`:

- a header missing its proof of work, or headers that do not follow one
  another, are `Misbehaving` (:2623, :2629), as is a header whose target
  the chain does not require, `BLOCK_INVALID_HEADER` (:1937-1939);
- a block whose body its header does not commit to is `Misbehaving`,
  "mutated block" (:4875-4876);
- a block asked for and never sent costs the peer its connection after
  the download timeout, and nothing else (:6118-6121).

`Misbehaving` makes `MaybeDiscourageAndDisconnect` drop the peer and
discourage its address, except that a local peer is dropped and not
discouraged, "since that would discourage all peers on the same local
address" (:5194-5201). Every peer here is local, so each liar is dropped
and the host stays undiscouraged, which is what the honest peer, on the
same host, needs. `tests/unit/p2p/manager_test.py` holds the discouraging
half.
"""

from typing import TYPE_CHECKING

import pytest

from btclib_node.p2p import callbacks as cb
from tests import wait_until
from tests.functional.p2p import (
    Lie,
    a_node_among_peers,
    shorten_block_download_timeout,
)

if TYPE_CHECKING:
    from pathlib import Path

# three full batches, and the liars start lying with the second
_BATCH = 10
_LENGTH = 3 * _BATCH
_LIES_FROM = _BATCH


@pytest.fixture(autouse=True)
def short_batches_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a short chain several batches, and a block never sent a short wait.

    `MAX_HEADERS_RESULTS` is lowered for the node, in this one process,
    as `low_work_headers_test.py` lowers it, and the peers batch by the
    same number.
    """
    monkeypatch.setattr(cb, "MAX_HEADERS_RESULTS", _BATCH)
    shorten_block_download_timeout(monkeypatch)


@pytest.mark.parametrize("lie", list(Lie), ids=[lie.name for lie in Lie])
def test_a_peer_lying_about_its_chain_is_dropped_and_the_honest_tip_wins(
    tmp_path: Path, lie: Lie
) -> None:
    """Two peers: the one met first lies, and the node ends on the other's tip.

    A header or block liar tells the truth for the first batch, or the
    first blocks, and lies after, so what it sent before the lie is
    already indexed when the node drops it. The fork liar lies from the
    start. A PROOF_OF_WORK or TARGET liar sends a header the honest chain
    lacks, and that header is never indexed: that shows the check its lie
    names refused it.
    """
    with a_node_among_peers(
        tmp_path, [lie, None], length=_LENGTH, lies_from=_LIES_FROM, batch=_BATCH
    ) as (node, (liar, honest), honest_tip):
        block_index = node.chainstate.block_index
        wait_until(lambda: block_index.active_chain[-1] == honest_tip)
        assert len(block_index.active_chain) == _LENGTH + 1
        wait_until(liar.dropped.is_set)
        assert liar.forged.isdisjoint(block_index.header_dict)
        assert liar.lied.is_set()
        wait_until(lambda: len(node.p2p_manager.connections) == 1)
        (conn,) = node.p2p_manager.connections.values()
        assert not honest.dropped.is_set()
        assert not node.p2p_manager.is_discouraged(conn.address)


def test_nine_lying_peers_and_one_honest_one_end_on_the_honest_tip(
    tmp_path: Path,
) -> None:
    """Nine peers lie, each in a way, and the tenth tells the truth."""
    lies = [*Lie, *list(Lie)[:4]]
    with a_node_among_peers(
        tmp_path, [*lies, None], length=_LENGTH, lies_from=_LIES_FROM, batch=_BATCH
    ) as (node, (*liars, honest), honest_tip):
        block_index = node.chainstate.block_index
        wait_until(lambda: block_index.active_chain[-1] == honest_tip)
        assert len(block_index.active_chain) == _LENGTH + 1
        wait_until(lambda: all(liar.dropped.is_set() for liar in liars))
        assert all(liar.lied.is_set() for liar in liars)
        assert all(liar.forged.isdisjoint(block_index.header_dict) for liar in liars)
        wait_until(lambda: len(node.p2p_manager.connections) == 1)
        (conn,) = node.p2p_manager.connections.values()
        assert not honest.dropped.is_set()
        assert not node.p2p_manager.is_discouraged(conn.address)
