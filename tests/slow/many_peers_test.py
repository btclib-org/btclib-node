# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Ten peers, nine of them lying, over full header batches (ISS 1894).

`tests/functional/p2p/multi_peer_headers_test.py` runs the same ten peers
over a chain of three batches of ten, with `MAX_HEADERS_RESULTS` lowered
so that a short chain is several batches. Here nothing is lowered: the
chain is two full batches of `MAX_HEADERS_RESULTS` headers and a third
of 500, the liars lie from the second batch on, and the node validates
every block of the honest chain, which is what takes the time.
`tests/README.md`'s *Slow tests* says why the default run does not lose
a line by skipping it.
"""

from typing import TYPE_CHECKING

import pytest
from btclib.p2p.limits import MAX_HEADERS_RESULTS

from tests import wait_until
from tests.functional.p2p import (
    Lie,
    a_node_among_peers,
    shorten_block_download_timeout,
)

if TYPE_CHECKING:
    from pathlib import Path

_LENGTH = 2 * MAX_HEADERS_RESULTS + 500

# The wait for the honest tip lasts as long as the node takes to validate
# `_LENGTH` blocks, which scales with the runner, and is longer than
# `wait_until`'s default. It stays under `timeout` in pyproject.toml.
_SYNC_TIMEOUT = 240


@pytest.mark.slow
def test_nine_lying_peers_and_one_honest_one_end_on_the_honest_tip_over_full_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each liar is dropped in turn, and the node ends on the honest tip."""
    shorten_block_download_timeout(monkeypatch)
    lies = [*Lie, *list(Lie)[:4]]
    with a_node_among_peers(
        tmp_path, [*lies, None], length=_LENGTH, lies_from=MAX_HEADERS_RESULTS
    ) as (node, (*liars, honest), honest_tip):
        block_index = node.chainstate.block_index
        wait_until(
            lambda: block_index.active_chain[-1] == honest_tip, timeout=_SYNC_TIMEOUT
        )
        assert len(block_index.active_chain) == _LENGTH + 1
        wait_until(lambda: all(liar.dropped.is_set() for liar in liars))
        assert all(liar.lied.is_set() for liar in liars)
        assert all(liar.forged.isdisjoint(block_index.header_dict) for liar in liars)
        wait_until(lambda: len(node.p2p_manager.connections) == 1)
        (conn,) = node.p2p_manager.connections.values()
        assert not honest.dropped.is_set()
        assert not node.p2p_manager.is_discouraged(conn.address)
