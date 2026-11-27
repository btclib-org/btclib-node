# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A start that UniValue refuses, as bitcoind prints it.

ISS 2457 and ISS 2497: `UniValue::getInt` refuses a number out of range,
and `UniValue::checkType` a value of the wrong type. Each `settings.json`
is written for a real bitcoind and for this node, each started on
regtest with no listener and no RPC server. The exit status, stdout and
stderr of the two are held equal, and so is whether the log holds the
block.
"""

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _starts(
    bitcoind_path: str, tmp_path: Path, settings: str
) -> dict[str, tuple[int, str, str, bool]]:
    """Return what bitcoind and this node exit with, write, and log.

    For each: the exit status, stdout, stderr, and whether its log,
    `debug.log` or `history.log`, holds an `EXCEPTION:` block.

    `-printtoconsole=0` for bitcoind alone: this node writes no log to
    the console, as `cli`'s docstring says, and bitcoind does once the
    log has started, as it has where it reads `-bantime`.
    """
    commands = {
        "bitcoind": ([bitcoind_path, "-printtoconsole=0"], "debug.log"),
        "node": ([sys.executable, "-m", "btclib_node"], "history.log"),
    }
    results = {}
    for name, (command, log_name) in commands.items():
        datadir = tmp_path / name
        (datadir / "regtest").mkdir(parents=True)
        (datadir / "regtest" / "settings.json").write_text(settings, encoding="utf-8")
        result = subprocess.run(  # noqa: S603
            [*command, f"-datadir={datadir}", "-regtest", "-listen=0", "-server=0"],
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )
        log = datadir / "regtest" / log_name
        logged = log.exists() and "EXCEPTION:" in log.read_text(encoding="utf-8")
        results[name] = (result.returncode, result.stdout, result.stderr, logged)
    return results


@pytest.mark.parametrize(
    ("settings", "name"),
    [
        ('{"maxconnections": 9223372036854775808}', "St13runtime_error"),
        ('{"maxconnections": 2147483648}', "St13runtime_error"),
        ('{"rpcmaxconnections": 1.5}', "St13runtime_error"),
        ('{"bantime": 9223372036854775808}', "St13runtime_error"),
        ('{"maxconnections": {}}', "N8UniValue10type_errorE"),
        ('{"bantime": {}}', "N8UniValue10type_errorE"),
        ('{"signet": 1}', "N8UniValue10type_errorE"),
        ('{"dnsseed": {}}', "N8UniValue10type_errorE"),
        ('{"dnsseed": 1}', "N8UniValue10type_errorE"),
        ('{"dnsseed": {}, "forcednsseed": true}', "N8UniValue10type_errorE"),
        ('{"reindex-chainstate": {}}', "N8UniValue10type_errorE"),
        ('{"prune": 550, "reindex-chainstate": {}}', "N8UniValue10type_errorE"),
    ],
    ids=[
        "int64",
        "int",
        "fraction",
        "after the lock",
        "object",
        "object after the lock",
        "chain selector",
        "dnsseed",
        "dnsseed number",
        "dnsseed beside forcednsseed",
        "reindex-chainstate",
        "reindex-chainstate under prune",
    ],
)
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bitcoind names its executable's path there, as cli._exception_block says",
)
def test_a_start_univalue_refuses_prints_what_bitcoind_prints(
    bitcoind_path: str, tmp_path: Path, settings: str, name: str
) -> None:
    """`PrintExceptionContinue`'s block on stderr, exit `1`."""
    results = _starts(bitcoind_path, tmp_path, settings)
    assert results["node"] == results["bitcoind"]
    assert results["bitcoind"][0] == 1
    assert f"EXCEPTION: {name}" in results["bitcoind"][2]


@pytest.mark.parametrize(
    "settings",
    ['{"settings": {}}', '{"settings": ["a.json"]}'],
    ids=["object", "array"],
)
def test_a_wrong_typed_settings_key_prints_what_bitcoind_prints(
    bitcoind_path: str, tmp_path: Path, settings: str
) -> None:
    """`InitConfig`'s `catch` takes the `type_error`: `Error:`, exit `1`."""
    results = _starts(bitcoind_path, tmp_path, settings)
    assert results["node"] == results["bitcoind"]
    assert results["bitcoind"][0] == 1
    assert results["bitcoind"][2].startswith("Error: JSON value of type ")
