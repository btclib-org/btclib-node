# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import os
import signal
import threading
import time
from math import log2
from multiprocessing.pool import Pool
from typing import TYPE_CHECKING, override

from btclib_node.block_db import BlockDB
from btclib_node.chainstate import Chainstate
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.download import DownloadManager
from btclib_node.exceptions import NodeShutdownTimeoutError
from btclib_node.interpreter import warm
from btclib_node.log import Logger
from btclib_node.main import update_chain
from btclib_node.mempool import Mempool
from btclib_node.p2p.address import PeerDB
from btclib_node.p2p.main import handle_p2p, handle_p2p_handshake
from btclib_node.p2p.manager import P2pManager
from btclib_node.rpc.main import handle_rpc
from btclib_node.rpc.manager import RpcManager

if TYPE_CHECKING:
    from types import FrameType

# Everything above this line is imported for `Node` to build on, not to
# be handed to a caller: `handle_p2p`, `RpcManager` and the rest are
# named here so this module can wire them together, and nothing outside
# this repository has ever imported one of them from the package root
# rather than from its own module. `Node` is the one name a caller
# reaches for -- `scripts/chains/` and every functional test do exactly
# that -- so it is the whole of `__all__` (btclib-org/.github#239).
__all__ = ["Node"]

# How long `stop` waits for the loop to come back before saying it did
# not. The flag is read at the top of the loop, so the wait is however
# long the pass already running takes: `update_chain` validates a fork
# block by block, and checks this same flag between blocks, so what sets
# the scale is one block's own `worker_pool.starmap` over its inputs --
# thousands of signature checks on mainnet -- rather than the whole fork
# a deep reorg walks. An idle stop costs milliseconds. Well under the
# per-test limit `pyproject.toml` sets, so that a node which will not
# stop is reported here rather than by whichever bound expires first;
# `tests/unit/init_test.py` asserts that ordering rather than leaving it
# to this comment.
STOP_TIMEOUT = 30


def _default_worker_processes() -> int:
    """How many processes `Node.worker_pool` spawns.

    Eight outside of a test run, unconditionally. Under `pytest-xdist`,
    `PYTEST_XDIST_WORKER_COUNT` is the number of worker processes the
    run was split across (`xdist/remote.py` sets it in the worker's own
    environment before any test module is imported), and every one of
    them builds its own `Node`s and, through them, its own pools: eight
    processes each, on top of `-n auto`'s one worker per core, is what
    starves a `wait_until` on a machine that has the cores for the
    xdist workers alone and not for eight more processes per worker on
    top of that (btclib-org/btclib-node#46). Dividing the machine's own
    core count across the workers instead keeps the total the run
    spawns near that core count, whatever `-n` is set to, and leaves a
    node running outside of pytest at the flat eight.
    """
    xdist_workers = os.environ.get("PYTEST_XDIST_WORKER_COUNT")
    if xdist_workers is None:
        return 8
    return max(1, (os.cpu_count() or 8) // int(xdist_workers))


# `Node.worker_pool`'s own size, named so `warm_worker_pool` can compute
# a warm-up call count from it without a second literal to drift from.
_WORKER_PROCESSES = _default_worker_processes()


class Node(threading.Thread):
    def __init__(self, config: Config | None = None) -> None:
        super().__init__()

        if config is None:
            config = Config()

        # signal.signal's own calling convention, POSIX's SIG_DFL/SIG_IGN
        # only alternative being no handler object at all: every handler
        # is called with (signum, frame), unread here since the three
        # signals below share this one and stop() takes neither.
        def stop_handler(_signum: int, _frame: FrameType | None) -> None:
            self.stop()

        signal.signal(signal.SIGINT, stop_handler)
        signal.signal(signal.SIGTERM, stop_handler)
        # for hibernation
        signal.signal(signal.SIGTSTP, stop_handler)

        self.config = config
        self.chain = config.chain
        self.data_dir = config.data_dir
        self.data_dir.mkdir(exist_ok=True, parents=True)

        self.terminate_flag = threading.Event()
        log_path = self.data_dir / config.log_path if config.log_path else None
        self.logger = Logger(log_path, config.debug)

        self.chainstate = Chainstate(self.data_dir, self.chain, self.logger)
        self.block_db = BlockDB(self.data_dir, self.logger)
        # the two halves of a filter live in different databases -- the
        # block and its reverse patch in one, the index in the other --
        # so catching up is here, where both are built, and before
        # anything is listening: the version message this node sends
        # says it serves filters for the whole chain
        self.chainstate.filter_index.catch_up(
            self.chainstate.block_index.active_chain, self.block_db
        )
        self.mempool = Mempool(self.logger)

        # Built on first use, by the property below: the pool is
        # interpreters, spawned rather than forked wherever that is the
        # platform's default, and a node that never validates a script
        # should not pay for them. Which is most of them -- a node
        # serving headers, a node under test, a node that has nothing to
        # connect -- and each one that does pay competes for the cores
        # with the nodes that are actually validating.
        self._worker_pool: Pool | None = None
        self._worker_pool_lock = threading.Lock()
        # Set by `warm_worker_pool` and joined before `run`'s own
        # teardown reads `_worker_pool` below: without that join, a
        # warm-up still building the pool when the loop stops would
        # race the `is not None` check there, and the pool it goes on
        # to build would never be handed to `terminate`.
        self._worker_pool_warmup: threading.Thread | None = None

        self.status = NodeStatus.Starting

        self.download_manager = DownloadManager(self, self.logger)

        self.p2p_port: int | None
        if config.p2p_port:
            self.p2p_port = config.p2p_port
        else:
            self.p2p_port = None
        peer_db = PeerDB(self.chain, self.data_dir)
        self.p2p_manager = P2pManager(self, self.p2p_port, peer_db)

        self.rpc_port: int | None
        if config.rpc_port:
            self.rpc_port = config.rpc_port
        else:
            self.rpc_port = None
        self.rpc_manager = RpcManager(self, self.rpc_port)

    @property
    def worker_pool(self) -> Pool:
        # under the lock, so that two callers get one pool: the second
        # would otherwise leave a pool with nothing holding it and
        # nothing to terminate it
        with self._worker_pool_lock:
            if self._worker_pool is None:
                self._worker_pool = Pool(processes=_WORKER_PROCESSES)
            return self._worker_pool

    def _close_worker_pool(self) -> None:
        # read, not asked for: the property would build the pool this
        # exists to take down. `terminate()` alone left `_worker_pool`
        # referencing a pool whose worker processes and handler threads
        # it had not waited for; `join()` waits for them here, and
        # dropping the reference is what stops whatever collects the
        # `Node` afterwards from finding one still to take down.
        if self._worker_pool is not None:
            self._worker_pool.terminate()
            self._worker_pool.join()
            self._worker_pool = None

    def __del__(self) -> None:
        # a backstop for a `Node` built and used without ever being
        # `start()`ed -- `tests/unit/main_test.py` calls `update_chain`
        # against one directly, on the thread that built it, to reach a
        # block's own validation without a loop around it, and that
        # builds a real `worker_pool` that `run`'s own teardown below
        # never runs to take back down. Without this, that pool is
        # still in `Pool.RUN` state whenever something finally collects
        # it, which is what made `Pool.__del__` warn and reach for a
        # queue of its own on an xdist worker's stderr, reported
        # against btclib-org/btclib-node#195. `getattr` rather than the
        # attribute itself: `__init__` can raise before `_worker_pool`
        # is set, and a constructor's exception should not come back
        # paired with a second one out of here.
        if getattr(self, "_worker_pool", None) is not None:
            self._close_worker_pool()

    def warm_worker_pool(self) -> None:
        """Build the worker pool now, on a thread of its own, and warm it.

        `check_transactions`' own first call used to be what built and
        warmed `worker_pool`, on whatever thread called it -- `run`'s
        own loop below, the same one that drains
        `p2p_manager.handshake_messages` and promotes a connection once
        its `verack` arrives. Each of the pool's processes pays its own
        import of `btclib_node.interpreter` (and, through it,
        `btclib.script.engine`) the first time it is dispatched a task,
        and while that first dispatch is running, the loop below cannot
        drain that queue: a peer whose `verack` the kernel already
        delivered sits unpromoted until the call returns
        (btclib-org/btclib-node#262).

        `download_manager.block_download` is the only caller, right
        before it sends the first real `GetData` for a block this node
        does not have -- the earliest point a script is actually going
        to be validated, with a peer's round trip ahead of it as extra
        runway, rather than the moment header sync merely completes.
        Reaching `HeaderSynced` is not enough on its own: the comment on
        `_worker_pool` above is that a node which never validates a
        script should not pay for the pool, and a node whose headers are
        synced but which never has a block to fetch -- a header-only
        peer under test, a peer whose counterpart stops serving blocks
        -- is exactly that. The guard below makes a second call a no-op,
        since `block_download` runs on every pass of the loop below and
        would otherwise ask for a second thread once the first has
        already built the pool.
        """
        if self._worker_pool_warmup is not None:
            return

        def build_and_warm() -> None:
            self.worker_pool.starmap(warm, [()] * (_WORKER_PROCESSES * 4))

        self._worker_pool_warmup = threading.Thread(target=build_and_warm, daemon=True)
        self._worker_pool_warmup.start()

    def _drain_message_queues(self) -> bool:
        """Handle whatever is waiting, and answer whether nothing was.

        One message must not end the node. `handle_p2p` and
        `handle_p2p_handshake` already answer a bad message by dropping
        the peer, but what reaches here is whatever they did not expect
        -- and leaving `run`'s own loop by exception skips every close
        below it, so the databases would stay open.
        """
        wait = True
        try:
            while len(self.p2p_manager.handshake_messages):
                handle_p2p_handshake(self)
                wait = False
            for _ in range(int(log2(len(self.rpc_manager.messages) + 1))):
                handle_rpc(self)
                wait = False
            for _ in range(int(log2(len(self.p2p_manager.messages) + 1))):
                handle_p2p(self)
                wait = False
        except Exception:
            self.logger.exception("Exception occurred handling a message")
        return wait

    def _step_chain(self) -> bool:
        """Advance the chain one step, and answer whether `run` should stop."""
        try:
            self.download_manager.step()
            update_chain(self)
        except Exception:
            self.logger.exception("Exception occurred")
            return True
        return False

    @override
    def run(self) -> None:
        self.logger.info("Starting main loop")

        if self.p2p_port:
            self.p2p_manager.start()
        if self.rpc_port:
            self.rpc_manager.start()
        self.status = NodeStatus.SyncingHeaders
        while not self.terminate_flag.is_set():
            if self._drain_message_queues():
                time.sleep(0.0001)
            if self._step_chain():
                break
        self.p2p_manager.stop()
        self.rpc_manager.stop()

        self.p2p_manager.peer_db.close()
        self.chainstate.close()
        self.block_db.close()

        # joined before the read below, not asked for: the same race
        # the attribute's own comment above names
        if self._worker_pool_warmup is not None:
            self._worker_pool_warmup.join()
        self._close_worker_pool()

        self.logger.info("Stopping node")
        self.logger.close()

    def stop(self) -> None:
        """Ask the main loop to stop, and wait up to `STOP_TIMEOUT` for it.

        Raises if the loop has not come back by then, the node having
        no way to be sure of its chainstate or its databases while a
        thread is still inside them.

        Signalling alone lets the caller go on while the node is still
        there. A test that returns then is torn down around a thread
        that goes on logging into a harness that has moved on, which is
        where `ValueError: I/O operation on closed file` came from; and
        a loop that cannot come back at all holds the interpreter open
        after the last test, this being a non-daemon thread. No
        per-test limit reaches that second one -- the test it belongs
        to has already passed -- so waiting here is what puts the wait
        inside the test, where a limit can name it
        (btclib-org/btclib-node#98).

        The bound is the point rather than a precaution:
        `pytest-timeout` arms one timer per test, so a limit already
        spent in the call phase is not there for the teardown, and an
        unbounded wait in a teardown is a run that stops instead of
        failing (btclib-org/btclib-node#115).

        The node's own thread is the one caller that cannot wait, the
        `stop` RPC being handled inside the loop it stops. It gets the
        signal alone, and reaches the end of `run` by itself. A signal
        handler is the other caller worth naming: this raising there
        makes an operator's interrupt loud, and it does not make the
        process able to exit, the wedged thread being non-daemon.
        """
        self.terminate_flag.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=STOP_TIMEOUT)
            if self.is_alive():
                # named by its data directory, which is what tells one
                # node from another where several are running
                err_msg = f"the node at {self.data_dir} did not stop: its "
                err_msg += "thread is still running after the flag was set. "
                err_msg += "Nothing after this can trust the chainstate or "
                err_msg += "the databases"
                raise NodeShutdownTimeoutError(err_msg)
