# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A start reads the RPC login and cookie options only where bitcoind does.

ISS 2585. A value of the wrong JSON type in `-rpcuser`, `-rpcpassword`,
`-rpccookieperms` or `-rpccookiefile` is written to the `settings.json`
of a real bitcoind and of this node, each started on regtest with no
listener. With `-server=0` neither reads any of the four. With the server
on, neither reads `-rpcuser` without a password, nor a cookie option
beside one. Both start, and both exit `0` once stopped.
"""

import sys
from typing import TYPE_CHECKING

import pytest

from tests import get_random_port
from tests.integration.rpcservertimeout_server_off_test import _start_and_stop

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("settings_json", "server"),
    [
        ('{"rpcuser": {}}', False),
        ('{"rpcpassword": ["x"]}', False),
        ('{"rpccookieperms": {}}', False),
        ('{"rpccookiefile": {}}', False),
        ('{"rpcuser": {}}', True),
        ('{"rpcpassword": "x", "rpccookieperms": {}}', True),
        ('{"rpcpassword": "x", "rpccookiefile": {}}', True),
    ],
    ids=[
        "server=0-rpcuser",
        "server=0-rpcpassword",
        "server=0-rpccookieperms",
        "server=0-rpccookiefile",
        "server=1-rpcuser-without-a-password",
        "server=1-rpccookieperms-beside-a-password",
        "server=1-rpccookiefile-beside-a-password",
    ],
)
@pytest.mark.skipif(
    sys.platform == "win32", reason="`terminate` kills a Windows process outright"
)
def test_a_start_ignores_the_login_options_core_does_not_read(
    bitcoind_path: str, tmp_path: Path, settings_json: str, *, server: bool
) -> None:
    """Both start, and exit `0` on `SIGTERM` with nothing on stderr.

    `-printtoconsole=0` keeps bitcoind's log off the console, as this
    node writes none there. With the server on, each program listens on
    an `-rpcport` of its own.
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
            settings_json, encoding="utf-8"
        )
        rpcport = [f"-rpcport={get_random_port()}"] if server else []
        results[program] = _start_and_stop([*command, *rpcport], datadir, server=server)
    assert results["node"] == results["bitcoind"] == (True, 0, "")
