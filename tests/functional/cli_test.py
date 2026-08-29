# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A run through the console script stays one process (issue #579).

`tests/unit/scripts_test.py` used to answer this statically, for the
three now-deleted `scripts/chains/*.py`; this is the dynamic half ISS
579 itself asked for -- "a run through the command is shown to stay one
process past the start of block download" -- and it did not exist for
any entry point before this branch, static or dynamic, this one
included.

`btclib-node`, not `python -m btclib_node`: a spawned pool worker whose
parent was started through `-m` never re-executes `__main__.py`'s body
at all -- `multiprocessing.spawn`'s own `_fixup_main_from_name` exempts
every module named `*.__main__`, which is what `__main__.py`'s own
module docstring reads out of the interpreter's source -- so that route
carries nothing for this test to catch a regression in. The console
script does: it is a plain script rather than a `-m`-named module, so a
worker re-executes it under `_fixup_main_from_path`, and what stops
`main()` running a second time there is that generated shim's own
`if __name__ == "__main__":` guard (`cli.py`'s own module docstring).

The measurement is ISS 579's own: `Node.warm_worker_pool`'s only caller
is `download_manager.block_download`, right before the first real
`GetData`, so a subprocess started through the console script and
pointed at a peer that actually has blocks to serve is carried past the
same point the original defect fired on. `history.log`'s own
`"Start Index initialization"` count is what ISS 579 measured a second
`Node` by -- nine of them, once, unguarded -- and `"Address already in
use"` is the other half, the two ports the second `Node` collided on.
"""

import subprocess
import sys
from pathlib import Path

from bitcoin_core_rpc import BitcoinCoreRpcClient, FetchError

from btclib_node import Node
from btclib_node.chains import RegTest
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.main import update_chain
from tests import (
    generate_random_chain,
    get_random_port,
    wait_until,
    wait_until_listening,
)

# Five, not `test_download`'s 3000: what this test is measuring is the
# process count once a block is actually fetched, not the download
# path's own throughput -- `warm_worker_pool` fires on the first real
# `GetData` regardless of how many more are still owed.
_CHAIN_LENGTH = 5

# `.venv/bin/btclib-node` beside the interpreter running this suite:
# `uv sync` installs the console script there, into the same
# virtualenv `uv run pytest` runs from, so this is the shim built off
# this checkout's own `[project.scripts]` entry rather than one PyPI
# published.
_CONSOLE_SCRIPT = Path(sys.executable).parent / "btclib-node"


def test_a_run_through_the_console_script_stays_one_process(tmp_path: Path) -> None:
    """`btclib-node -regtest -connect=<peer>` stays a single `Node`.

    The peer is a bootstrap node built and fully synced by hand, the
    same shape `tests/functional/p2p/download_test.py`'s own
    `bootstrap_node` is -- headers added and every block marked
    downloaded, so the subprocess under test has real blocks to fetch
    the moment it dials it. `-connect`, not the ordinary DNS-seed/addrman
    draw: this test's own regtest peer is the only address the
    subprocess is ever given, `Config.chain`'s own DNS seeds
    (`chains.py`) being empty for regtest in any case.

    Not guarded by an existence check on `_CONSOLE_SCRIPT`: `uv sync`
    installing it is this whole gate's own precondition, so a missing
    shim is `subprocess.Popen`'s own `FileNotFoundError`, as loud a
    failure as a check written here would be and one this suite does
    not have to carry an unreachable branch to produce.
    """
    chain = generate_random_chain(_CHAIN_LENGTH, RegTest().genesis.hash)
    headers = [block.header for block in chain]

    bootstrap = Node(
        config=Config(
            chain="regtest",
            data_dir=tmp_path / "bootstrap",
            p2p_port=get_random_port(),
            allow_rpc=False,
        )
    )
    bootstrap.status = NodeStatus.HeaderSynced
    block_index = bootstrap.chainstate.block_index
    block_index.add_headers(headers)
    for block_hash in block_index.header_dict:
        block_index.set_downloaded(block_hash)
    for block in chain:
        bootstrap.block_db.add_block(block)
    for _ in range(len(chain)):
        update_chain(bootstrap)
    assert bootstrap.status == NodeStatus.BlockSynced
    bootstrap.start()
    wait_until_listening(bootstrap.p2p_manager)

    data_dir = tmp_path / "main"
    rpc_port = get_random_port()
    process = subprocess.Popen(  # noqa: S603
        [
            str(_CONSOLE_SCRIPT),
            "-regtest",
            f"-datadir={data_dir}",
            f"-port={get_random_port()}",
            f"-rpcport={rpc_port}",
            f"-connect=127.0.0.1:{bootstrap.p2p_port}",
        ],
    )
    client = BitcoinCoreRpcClient(
        f"http://127.0.0.1:{rpc_port}",
        user="pytest",
        password="pytest",  # noqa: S106
        timeout=5,
    )

    def rpc_ready() -> bool:
        try:
            client.call("getblockcount")
        except FetchError:
            return False
        return True

    try:
        wait_until(rpc_ready)
        wait_until(lambda: client.call("getblockcount") == len(chain))
        client.call("stop")
        process.wait(timeout=30)
    finally:
        # a no-op on the process this try already waited for: `Popen`'s
        # own `send_signal` only signals a process whose `returncode`
        # is still `None`, so this is what actually stops one left
        # running by a failure above it, on this test's own machine,
        # rather than a second signal reaching the one already stopped
        process.terminate()
        process.wait(timeout=30)
        bootstrap.stop()
        bootstrap.join()

    log_text = (data_dir / "regtest" / "history.log").read_text(encoding="utf-8")
    assert log_text.count("Start Index initialization") == 1
    assert "Address already in use" not in log_text
