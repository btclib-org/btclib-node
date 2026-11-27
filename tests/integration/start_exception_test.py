# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A start refused by an exception `AppInit` catches, as bitcoind prints it.

Each case is set up for a real bitcoind and for this node, each started
on regtest with no listener and no RPC server, and the exit status,
stdout and stderr of the two are held equal: a `settings.json` number
`UniValue::getInt` refuses (ISS 2457), and a blocks directory
`fs::create_directories` cannot make (ISS 2536).
"""

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "settings",
    [
        '{"maxconnections": 9223372036854775808}',
        '{"maxconnections": 2147483648}',
        '{"rpcmaxconnections": 1.5}',
        '{"bantime": 9223372036854775808}',
    ],
    ids=["int64", "int", "fraction", "after the lock"],
)
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bitcoind names its executable's path there, as cli._exception_block says",
)
def test_a_start_getint_refuses_prints_what_bitcoind_prints(
    bitcoind_path: str, tmp_path: Path, settings: str
) -> None:
    """`PrintExceptionContinue`'s block on stderr, exit `1`.

    `-printtoconsole=0` for bitcoind alone: this node writes no log to
    the console, as `cli`'s docstring says, and bitcoind does once the
    log has started, as it has where it reads `-bantime`.
    """
    commands = {
        "bitcoind": [bitcoind_path, "-printtoconsole=0"],
        "node": [sys.executable, "-m", "btclib_node"],
    }
    results = {}
    for name, command in commands.items():
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
        results[name] = (result.returncode, result.stdout, result.stderr)
    assert results["node"] == results["bitcoind"]
    assert results["bitcoind"][0] == 1
    assert "EXCEPTION: St13runtime_error" in results["bitcoind"][2]


@pytest.mark.parametrize(
    ("blocksdir", "debug"),
    [(False, []), (False, ["-debug=bogus"]), (True, [])],
    ids=["chain directory", "before -debug", "-blocksdir"],
)
@pytest.mark.skipif(
    sys.platform != "linux",
    reason="bitcoind's exception is libc++'s on macOS, and names its path on Windows",
)
def test_a_blocks_directory_that_cannot_be_made_prints_what_bitcoind_prints(
    bitcoind_path: str, tmp_path: Path, *, blocksdir: bool, debug: list[str]
) -> None:
    """`create_directories`'s block on stderr, exit `1`, ISS 2536.

    A regular file stands where `blocks`, or the chain directory above it,
    goes. bitcoind names the path whole, and this node by its length;
    `core` and `node` give the two paths one length.
    """
    commands = {
        "core": [bitcoind_path, "-printtoconsole=0"],
        "node": [sys.executable, "-m", "btclib_node"],
    }
    results = {}
    paths = {}
    for name, command in commands.items():
        base = tmp_path / name / "key"
        blocks = base / "regtest" / "blocks"
        if blocksdir:
            blocks.parent.mkdir(parents=True)
            blocks.write_text("not a directory", encoding="utf-8")
            (tmp_path / name / "data").mkdir()
            options = [f"-datadir={tmp_path / name / 'data'}", f"-blocksdir={base}"]
        else:
            base.mkdir(parents=True)
            blocks.parent.write_text("not a directory", encoding="utf-8")
            options = [f"-datadir={base}"]
        argv = [*options, "-regtest", "-listen=0", "-server=0", "-nosettings"]
        result = subprocess.run(  # noqa: S603
            [*command, *argv, *debug],
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )
        results[name] = (result.returncode, result.stdout, result.stderr)
        paths[name] = str(blocks)
    code, out, err = results["core"]
    assert paths["core"] in err
    shown = f"<{len(paths['node'])} characters>"
    assert results["node"] == (code, out, err.replace(paths["core"], shown))
    assert code == 1
    assert "filesystem_error" in err
