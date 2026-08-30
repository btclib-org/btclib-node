# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A node fetches a whole chain from several real peers at once.

`DownloadManager`'s own unit tests cover which candidate a block is
requested from; what this checks is that the requests it schedules
across many connections actually land a complete, synced chain.
"""

import shutil
import time
from typing import TYPE_CHECKING

import pytest

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.main import update_chain
from tests import (
    generate_random_chain,
    get_random_port,
    local_addr,
    wait_until,
    wait_until_listening,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.order(1)
def test_download(tmp_path: Path) -> None:
    """A fresh node reaches `BlockSynced` downloading from ten full peers.

    One bootstrap node is fully synced by hand -- headers added and
    every block marked downloaded -- and copied ten times so every peer
    already has the whole chain; a fresh `main_node` is then connected
    to all ten at once, which is what exercises spreading the download
    across several connections rather than one. `pytest.mark.order(1)`
    runs it first because it is among the slowest tests in the suite,
    and `tests/README.md` says what that marker holds under the shuffle.
    """
    length = 3000
    chain = generate_random_chain(length, RegTest().genesis.hash)
    headers = [block.header for block in chain]

    bootstrap_node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "node0",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    bootstrap_node.status = NodeStatus.HeaderSynced
    bootstrap_block_index = bootstrap_node.chainstate.block_index
    for start in range(0, length, 2000):
        bootstrap_block_index.add_headers(headers[start : start + 2000])
    for block_hash in bootstrap_block_index.header_dict:
        bootstrap_block_index.set_downloaded(block_hash)
    for block in chain:
        bootstrap_node.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(bootstrap_node)
    assert bootstrap_node.status == NodeStatus.BlockSynced

    # `_finalize_fork` only calls `Chainstate.flush` once
    # `UtxoIndex.should_flush` says the staged UTXO cache has grown
    # past its own bound (`main.py`), which this chain of coinbase-only
    # blocks never reaches: everything `BlockIndex.stage_status` and
    # `FilterIndex`'s own `pending` hold (`db.py`'s docstring) is still
    # Python state, never written to the store, so a copy taken without
    # this call would carry nothing past genesis -- measured directly,
    # `Chainstate.flush` runs zero times over the loop above without it
    # (closes #710). Called here rather than through `close()`:
    # `bootstrap_node` stays running and is never closed, so nothing
    # here reopens btclib-org/btclib-node#703's own Windows question.
    # Core's own `TestFramework._initialize_chain`
    # (`test/functional/test_framework/test_framework.py`) fully stops
    # its cache node before copying its datadir to seed every other
    # one, read at bitcoin/bitcoin@ca7162cde5, rather than flushing a
    # node it leaves running -- this test diverges because
    # `bootstrap_node` has to stay a live peer for the rest of it,
    # which a stopped cache node never needs to be.
    bootstrap_node.chainstate.flush()
    bootstrap_node.start()
    wait_until_listening(bootstrap_node.p2p_manager)

    download_nodes = [bootstrap_node]
    for i in range(1, 10):
        # Not `LOCK`: `bootstrap_node` is running and holds it open, and
        # RocksDB re-creates it fresh on every `Rdict` open regardless of
        # what -- if anything -- was there before, so a copy carries no
        # state a fresh open would not already write itself. Copying it
        # anyway is what raised `shutil.Error: [WinError 32]` on
        # `windows-latest`, Windows refusing to copy a file another
        # handle still holds where POSIX does not (closes #683).
        shutil.copytree(
            tmp_path / "node0",
            tmp_path / f"node{i}",
            ignore=shutil.ignore_patterns("LOCK"),
        )
        node = Node(
            config=Config(
                chain="regtest",
                data_dir=tmp_path / f"node{i}",
                p2p_port=get_random_port(),
                allow_rpc=False,
            )
        )
        # Each copy is asserted whole on its own, before it ever talks
        # to a peer: `main_node`'s own final assertion below is
        # satisfiable through `bootstrap_node` alone, so it cannot
        # answer whether the other nine copies carry the chain they are
        # meant to (closes #710).
        assert len(node.chainstate.block_index.active_chain) == length + 1
        node.start()
        wait_until_listening(node.p2p_manager)
        download_nodes.append(node)

    main_node = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "main",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    main_node.start()
    wait_until_listening(main_node.p2p_manager)

    for node in download_nodes:
        main_node.p2p_manager.connect(local_addr(node.p2p_port))
        time.sleep(0.25)

    block_index = main_node.chainstate.block_index
    wait_until(lambda: len(block_index.active_chain) == length + 1)
    wait_until(lambda: main_node.status == NodeStatus.BlockSynced)

    main_node.stop()
    main_node.join()
    for node in download_nodes:
        node.stop()
        node.join()
