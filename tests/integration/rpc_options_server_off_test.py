# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A start with `-server=0` reads none of four RPC options, as bitcoind.

ISS 2538. The same JSON number in `-rpcallowip`, `-rpcauth`,
`-rpcwhitelist` or `-rpcwhitelistdefault` is written to the
`settings.json` of a real bitcoind and of this node, each started on
regtest with no listener and no RPC server: both start, and both exit
`0` once stopped.
"""

import sys
from typing import TYPE_CHECKING

import pytest

from tests.integration import start_and_stop

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "name", ["rpcallowip", "rpcauth", "rpcwhitelist", "rpcwhitelistdefault"]
)
@pytest.mark.skipif(
    sys.platform == "win32", reason="`terminate` kills a Windows process outright"
)
def test_a_start_without_the_server_ignores_rpc_options(
    bitcoind_path: str, tmp_path: Path, name: str
) -> None:
    """Both start, and exit `0` on `SIGTERM` with nothing on stderr.

    `-printtoconsole=0` keeps bitcoind's log off the console, as this
    node writes none there.
    """
    commands = {
        "bitcoind": [bitcoind_path, "-printtoconsole=0"],
        "node": [sys.executable, "-m", "btclib_node"],
    }
    results = {}
    for program, command in commands.items():
        datadir = tmp_path / program
        (datadir / "regtest").mkdir(parents=True)
        (datadir / "regtest" / "settings.json").write_text(
            f'{{"{name}": 5}}', encoding="utf-8"
        )
        results[program] = start_and_stop(command, datadir)
    assert results["node"] == results["bitcoind"] == (True, 0, "")
