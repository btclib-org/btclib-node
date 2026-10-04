# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A stopped node's process exits while a DNS lookup is still waiting.

Issue #1274 reports a node alive over 30 s after `SIGTERM`. A regtest node
asks for the seed `dummySeed.invalid.` as it starts, and a lookup slow to
answer does the same.
"""

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from tests import get_random_port, wait_until

if TYPE_CHECKING:
    from pathlib import Path

# Patches `socket.getaddrinfo` before the node runs: the seed's lookup
# says it has begun, then waits far longer than the test does.
_RUNNER = """
import socket
import sys
import time
from pathlib import Path

from btclib_node.cli import main

lookup = socket.getaddrinfo
began = Path(sys.argv.pop())


def getaddrinfo(host, *args, **kwargs):
    if "dummySeed" in str(host):
        began.touch()
        time.sleep(120)
    return lookup(host, *args, **kwargs)


socket.getaddrinfo = getaddrinfo
if __name__ == "__main__":
    main()
"""

# far under the lookup's 120 s, far over an ordinary exit
_EXIT_WITHIN = 20


@pytest.mark.skipif(
    sys.platform == "win32", reason="`terminate` kills a Windows process outright"
)
def test_a_lookup_in_progress_does_not_hold_the_process_open(tmp_path: Path) -> None:
    """`SIGTERM` ends the process with the seed's lookup still waiting."""
    data_dir = tmp_path / "datadir"
    data_dir.mkdir()
    began = tmp_path / "lookup-began"
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            _RUNNER,
            "-regtest",
            f"-datadir={data_dir}",
            f"-port={get_random_port()}",
            f"-rpcport={get_random_port()}",
            str(began),
        ],
    )
    try:
        wait_until(began.exists)
        process.terminate()
        assert process.wait(timeout=_EXIT_WITHIN) == 0
    finally:
        # a no-op on a process that has exited
        process.kill()
        process.wait(timeout=30)
