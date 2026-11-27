# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A start with `-server=0` does not read `-rpcservertimeout`, as bitcoind.

ISS 2500. The same out-of-range `rpcservertimeout` is written to the
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
    "number", ["9223372036854775808", "1.5"], ids=["int64", "fraction"]
)
@pytest.mark.skipif(
    sys.platform == "win32", reason="`terminate` kills a Windows process outright"
)
def test_a_start_without_the_server_ignores_rpcservertimeout(
    bitcoind_path: str, tmp_path: Path, number: str
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
    for name, command in commands.items():
        datadir = tmp_path / name
        (datadir / "regtest").mkdir(parents=True)
        (datadir / "regtest" / "settings.json").write_text(
            f'{{"rpcservertimeout": {number}}}', encoding="utf-8"
        )
        results[name] = start_and_stop(command, datadir)
    assert results["node"] == results["bitcoind"] == (True, 0, "")
