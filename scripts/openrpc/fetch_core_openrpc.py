# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Write the OpenRPC document a Bitcoin Core `bitcoind` describes itself with.

Start the given `bitcoind` on regtest in a throwaway data directory, wait
for its RPC server, call `getopenrpcinfo` with `show_hidden` set, and write
the result to the output file. The wallet is left enabled, so the document
lists the wallet RPCs too.

The output is the result object written back with `json.dumps(indent=2)`
and a final newline, so that a later release's document diffs line by
line against this one. Core already sorts its methods by name
(`CRPCTable::buildOpenRPCDoc`, `src/rpc/server.cpp`, at
bitcoin/bitcoin@f0de87f040, the v32.1 tag).

It runs a downloaded binary, so it runs on a CI runner:
`tests/_data/README.md` says how the committed document was made.

    uv run --no-project --python 3.14 \
        scripts/openrpc/fetch_core_openrpc.py <bitcoind> <output.json>
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Callable

# A regtest bitcoind with an empty chain answers in seconds; this bounds
# a node that never comes up.
STARTUP_TIMEOUT = 60.0
POLL_INTERVAL = 0.2
# Core's RPC_IN_WARMUP: the server answers, the node is still loading
RPC_IN_WARMUP = -28


class WarmingUpError(OSError):
    """bitcoind answered that it is still starting; ask again."""


def free_port() -> int:
    """Return a TCP port on the loopback that nothing listens on now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def rpc_call(port: int, cookie: Path, method: str, params: list[object]) -> object:
    """Call `method` on the bitcoind at `port`, and return its result.

    Raise `OSError` where bitcoind is not ready yet: the cookie is not
    written, nothing listens, or it answers `RPC_IN_WARMUP`. Raise
    `RuntimeError` on any other RPC error.
    """
    auth = base64.b64encode(cookie.read_bytes().strip()).decode("ascii")
    body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": method, "params": params})
    request = Request(
        f"http://127.0.0.1:{port}/",
        data=body.encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=STARTUP_TIMEOUT) as response:  # noqa: S310
            reply = json.loads(response.read())
    except HTTPError as error:
        # an answer all the same: bitcoind puts the error object in the
        # body of a non-200 reply
        with error:
            reply = json.loads(error.read())
    error_object = reply.get("error")
    if error_object is not None:
        msg = f"{method}: {error_object}"
        if error_object.get("code") == RPC_IN_WARMUP:
            raise WarmingUpError(msg)
        raise RuntimeError(msg)
    return reply["result"]


def wait_for(
    call: Callable[[], object],
    *,
    alive: Callable[[], bool],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = STARTUP_TIMEOUT,
) -> object:
    """Return `call()` once it stops raising `OSError`, within `timeout`.

    Raise `RuntimeError` where the process stops (`alive` is false) or the
    deadline passes first.
    """
    deadline = clock() + timeout
    while True:
        if not alive():
            msg = "bitcoind exited before its RPC server answered"
            raise RuntimeError(msg)
        try:
            return call()
        except OSError:
            if clock() >= deadline:
                msg = f"bitcoind did not answer within {timeout}s"
                raise RuntimeError(msg) from None
            sleep(POLL_INTERVAL)


def fetch(bitcoind: Path, datadir: Path) -> object:
    """Start `bitcoind` in `datadir`, and return its `getopenrpcinfo` answer."""
    port = free_port()
    process = subprocess.Popen(  # noqa: S603
        [
            str(bitcoind),
            "-regtest",
            f"-datadir={datadir}",
            "-listen=0",
            "-rpcbind=127.0.0.1",
            "-rpcallowip=127.0.0.1",
            f"-rpcport={port}",
            "-daemon=0",
            "-printtoconsole=0",
        ],
    )
    cookie = datadir / "regtest" / ".cookie"
    try:
        return wait_for(
            lambda: rpc_call(port, cookie, "getopenrpcinfo", [True]),
            alive=lambda: process.poll() is None,
        )
    finally:
        process.terminate()
        process.wait(timeout=STARTUP_TIMEOUT)


def write_document(document: object, output: Path) -> None:
    """Write `document` as indented JSON with a final newline."""
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    output.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Read the bitcoind and the output path, and write the document."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bitcoind", type=Path, help="the bitcoind to start")
    parser.add_argument("output", type=Path, help="where to write the document")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as datadir:
        document = fetch(args.bitcoind, Path(datadir))
    write_document(document, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
