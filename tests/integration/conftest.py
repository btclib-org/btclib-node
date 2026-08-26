# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What `bitcoind_test.py` needs, and the switch that makes it skip.

Two things have to hold before the test below actually runs: the opt-in
itself, and a real `bitcoind` to point it at. `bitcoind_path` is where
both are asked for, an opt-in that failed for a missing binary being a
defect report that is not one; its skip message names the switch, the
way section 7 of the organization standard asks for.
"""

import os
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

import pytest
import requests

from tests import get_random_port

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# How long a freshly started bitcoind is given to answer its first RPC
# call, and how long it is given to exit once asked to. A regtest node
# with an all but empty chain does both in well under a second on any
# machine able to run this suite at all -- measured directly, starting
# bitcoind v31.1.0 and reaching a first successful `getblockchaininfo`
# call took under two seconds on an otherwise idle machine -- so this
# bounds the failure rather than the ordinary case.
_STARTUP_TIMEOUT = 30.0

# The two switches this module's skip message names: whether to run at
# all, and where the daemon is, read separately so that turning the
# first one on without the second fails loudly rather than silently
# finding whatever `bitcoind` happens to be on PATH.
_INTEGRATION_SWITCH = "BTCLIB_NODE_INTEGRATION"
_BITCOIND_PATH_VAR = "BTCLIB_NODE_BITCOIND"


@pytest.fixture(scope="session")
def bitcoind_path() -> str:
    """Return the bitcoind to run, or skip every test that asks for one."""
    if not os.environ.get(_INTEGRATION_SWITCH):
        pytest.skip(f"set {_INTEGRATION_SWITCH}=1 to run the integration tests")
    path = os.environ.get(_BITCOIND_PATH_VAR) or shutil.which("bitcoind")
    if path is None:
        pytest.skip(f"no bitcoind: name one in {_BITCOIND_PATH_VAR} or put it on PATH")
    return path


class Bitcoind:
    """A live regtest `bitcoind`, reachable over its cookie-authenticated RPC.

    `p2p_port` is read once, by the caller, to build the address
    `P2pManager.connect` dials; `rpc` is everything else this fixture is
    for.
    """

    def __init__(self, rpc_port: int, p2p_port: int, cookie_path: Path) -> None:
        """Hold the two ports `bitcoind_test.py` needs and the cookie's path."""
        self.rpc_port = rpc_port
        self.p2p_port = p2p_port
        self._cookie_path = cookie_path

    def rpc(self, method: str, params: list[object] | None = None) -> object:
        """Call `method` over bitcoind's JSON-RPC, and return its result.

        The cookie is read again on every call rather than cached at
        construction: bitcoind writes that file only once its RPC server
        is already listening, which is exactly the interval `_wait_for`
        below polls through.
        """
        user, _, password = self._cookie_path.read_text(encoding="utf-8").partition(":")
        response = requests.post(
            f"http://127.0.0.1:{self.rpc_port}/",
            json={
                "jsonrpc": "1.0",
                "id": "btclib-node-integration",
                "method": method,
                "params": params or [],
            },
            auth=(user, password),
            timeout=_STARTUP_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        if body["error"]:
            raise RuntimeError(body["error"])
        return body["result"]


def _wait_for_rpc(node: Bitcoind, process: subprocess.Popen[bytes]) -> None:
    """Poll until bitcoind answers an RPC call, or fail with what stopped it."""
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"bitcoind exited with {process.returncode}")
        try:
            node.rpc("getblockchaininfo")
        # every failure before the node is up is the same failure: the
        # cookie file `rpc` reads is not written yet, or the socket
        # behind it is not listening
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
        else:
            return
    pytest.fail(f"bitcoind did not answer within {_STARTUP_TIMEOUT}s")


@pytest.fixture
def bitcoind(bitcoind_path: str, tmp_path: Path) -> Iterator[Bitcoind]:
    """Start a disposable regtest bitcoind, and stop it once the test is done.

    Its own datadir, under pytest's own `tmp_path`, and both of its ports
    drawn the way every other fixture in this suite that starts a
    listener draws one -- `get_random_port` -- rather than a fixed pair
    that would fight another worker's under `-n auto` the same way a
    fixed p2p port already would.
    """
    rpc_port = get_random_port()
    p2p_port = get_random_port()
    datadir = tmp_path / "bitcoind"
    datadir.mkdir()
    process = subprocess.Popen(  # noqa: S603
        [
            bitcoind_path,
            "-regtest",
            f"-datadir={datadir}",
            "-listen=1",
            f"-bind=127.0.0.1:{p2p_port}",
            "-rpcbind=127.0.0.1",
            f"-rpcport={rpc_port}",
            "-rpcallowip=127.0.0.1",
            "-daemon=0",
            "-printtoconsole=0",
        ],
    )
    node = Bitcoind(rpc_port, p2p_port, datadir / "regtest" / ".cookie")
    try:
        _wait_for_rpc(node, process)
        yield node
    finally:
        process.terminate()
        process.wait(timeout=_STARTUP_TIMEOUT)
