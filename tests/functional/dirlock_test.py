# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A second `btclib-node` over a data directory a first one holds.

The shape of Core's own `feature_filelock.py`: the first node runs in
this process, the second is started as `bitcoind` would be, and what is
asserted is its exit status and the whole of what it prints.
"""

import subprocess
import sys
from typing import TYPE_CHECKING

from tests.conftest import node_context

if TYPE_CHECKING:
    from pathlib import Path


def test_a_second_node_over_the_same_data_directory_exits_with_cores_refusal(
    tmp_path: Path,
) -> None:
    """`bitcoind` v31.1.0's stderr and exit status, named for this node.

    The same ports as the first: Core locks the directory before it binds
    anything, so the refusal is the lock's and not a bind's.
    """
    with node_context(tmp_path) as node:
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "btclib_node",
                "-regtest",
                f"-datadir={tmp_path}",
                f"-port={node.p2p_port}",
                f"-rpcport={node.rpc_port}",
            ],
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )
    assert result.returncode == 1
    assert not result.stdout
    assert result.stderr == (
        f"Error: Cannot obtain a lock on directory {tmp_path / 'regtest'}."
        " btclib-node is probably already running.\n"
    )
