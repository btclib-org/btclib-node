# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Node`, the thread that drives everything else in this package.

One loop: drain the handshake queue, then a share of the RPC queue and
a share of the peer-to-peer queue, then step the download manager and
extend the chain. A message that raises is logged and the loop
continues; a failure under `update_chain` leaves the loop, because the
databases the submodules below open have to be closed on the way out.

`P2pManager` and `RpcManager` are each a thread of their own, running
an asyncio loop of their own; this module is what calls into them and
what they hand work back to.
"""

import os
import signal
import sys
import threading
import time
from math import log2
from multiprocessing.pool import Pool, ThreadPool
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
from btclib_node.p2p.main import handle_p2p, handle_p2p_handshake, resume_cfilters
from btclib_node.p2p.manager import P2pManager
from btclib_node.rpc.main import handle_rpc
from btclib_node.rpc.manager import RpcManager

if TYPE_CHECKING:
    from collections import deque
    from types import FrameType

    from btclib_node.p2p.connection import Connection

# Everything above this line is imported for `Node` to build on, not to
# be handed to a caller: `handle_p2p`, `RpcManager` and the rest are
# named here so this module can wire them together, and nothing outside
# this repository has ever imported one of them from the package root
# rather than from its own module. `Node` and `install_signal_handlers`
# below are the two names a caller reaches for -- `scripts/chains/`
# reaches for both, and every functional test builds a `Node` without
# ever calling the other -- so together they are the whole of `__all__`
# (btclib-org/.github#239).
__all__ = ["Node", "install_signal_handlers"]

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

# How long the loop below sleeps once a pass finds nothing waiting in
# either queue. The figure it replaces, 0.0001, sat below the
# platform timer's own granularity, so what set the pace of an idle
# node was the OS rather than the number, and every one of those passes
# still ran `download_manager.step()` and `update_chain()` in full
# (btclib-org/btclib-node#440).
#
# Core's own message loop takes this shape as a wait on a condition
# variable that a producer signals, so a node with work is woken at
# once and one without costs nothing until the wait's own ceiling
# (`CConnman::ThreadMessageHandler`, `src/net.cpp`, up to 100 ms,
# bitcoin/bitcoin@b91d983f66). A plain sleep cannot do that -- nothing
# wakes it early -- so raising it trades idle CPU for latency on
# whatever arrives while it is asleep, in a way Core's own wait does
# not have to. Giving `step()` its own cadence or signalling the loop
# from the queues would close that gap; neither is done here, so the
# gap stays and this is a divergence from what is cited above rather
# than a match to it.
#
# 5 ms rather than a larger figure in the 5-20 ms range the issue
# above named: an idle node's own CPU cost falls to about the same
# small fraction of its former self at 5, 10 and 20 ms alike, measured
# by comparing resource.getrusage across an idle run at each -- so
# nothing past 5 ms buys back more of it. What does move past 5 ms is
# the very latency the comment above says this sleep trades for that
# CPU, measured end to end on a real RPC round trip: it grows with the
# sleep, so the smallest figure that already captures the CPU win is
# the one that pays least for it, and it still leaves comfortable
# headroom under `tests/helpers.py`'s own 25 ms poll in `wait_until`
# and `wait_until_listening`, so nothing in the suite can resolve it.
IDLE_SLEEP_SECONDS = 0.005


def _default_worker_count() -> int:
    """How many workers `Node.worker_pool` spawns.

    Eight outside of a test run, unconditionally. Under `pytest-xdist`,
    `PYTEST_XDIST_WORKER_COUNT` is the number of worker processes the
    run was split across (`xdist/remote.py` sets it in the worker's own
    environment before any test module is imported), and every one of
    them builds its own `Node`s and, through them, its own pools: eight
    workers each, on top of `-n auto`'s one xdist worker per core, is
    what starves a `wait_until` on a machine that has the cores for the
    xdist workers alone and not for eight more of `Node.worker_pool`'s
    own per xdist worker on top of that (btclib-org/btclib-node#46).
    Dividing the machine's own core count across the xdist workers
    instead keeps the total the run spawns near that core count,
    whatever `-n` is set to, and leaves a node running outside of pytest
    at the flat eight.

    The reasoning is the same whether `_pool_factory` below hands
    `worker_pool` a process pool or a thread pool: an xdist worker is
    always an OS process, and what starves it is the machine's core
    count being oversubscribed by whatever `Node.worker_pool` spawns
    under it, a real OS thread competing for a core exactly as a real OS
    process does once the interpreter that spawns it is free-threaded
    (btclib-org/btclib-node#388).
    """
    xdist_workers = os.environ.get("PYTEST_XDIST_WORKER_COUNT")
    if xdist_workers is None:
        return 8
    return max(1, (os.cpu_count() or 8) // int(xdist_workers))


# `Node.worker_pool`'s own size, named so `warm_worker_pool` can compute
# a warm-up call count from it without a second literal to drift from.
_WORKER_COUNT = _default_worker_count()


def _pool_factory(*, gil_enabled: bool) -> type[Pool]:
    """Return the pool type `Node.worker_pool` builds, chosen by `gil_enabled`.

    `Pool` under a GIL build, `ThreadPool` under a free-threaded one --
    `sys._is_gil_enabled()` is `worker_pool`'s own caller for
    `gil_enabled`, kept out of this function so that both arms are
    reachable, and asserted, on a single interpreter (issue #388): a
    `ThreadPool` constructs on a GIL build as readily as a `Pool` does,
    so a parametrized test exercises both without a second interpreter.

    Two conditions license a `ThreadPool` at all, and both were
    established by reading and by running rather than by trust of a
    process-era comment (issue #388): `btclib.script.engine` never
    writes to the `Tx`, `TxIn` or `PrecomputedTxData` a task is handed
    -- confirmed by a concurrent run over a real, mixed-flavour,
    signed transaction with `Tx.__setattr__`/`TxIn.__setattr__`
    instrumented to catch one, on both interpreters -- and
    `btclib-secp256k1` verifies through one shared, read-only
    libsecp256k1 context that its own "Thread safety" section documents
    as safe for concurrent calls. Under a GIL build the choice does not
    matter for correctness and matters for speed: the libsecp256k1 cffi
    call does not release the GIL, so a `ThreadPool` there is threads
    taking turns behind a process pool's own real parallelism, which is
    why the GIL build keeps `Pool`.

    Core's own `CCheckQueue` always shares one
    `PrecomputedTransactionData` by pointer across its worker threads
    (`validation.h`'s `CScriptCheck::txdata`,
    bitcoin/bitcoin@794a753958); a `ThreadPool` is this tree's way of
    doing the same once the interpreter running it makes a real thread
    as parallel as one of Core's.
    """
    return Pool if gil_enabled else ThreadPool


class Node(threading.Thread):
    """A bitcoin full node, and the thread that runs its main loop.

    `config` (or a default `Config` when none is given) is what says
    which chain, which data directory and which ports; `__init__` opens
    every database under that directory and wires the p2p and RPC
    managers to this node before `start()` ever runs `run`'s loop.

    Building one touches no process-wide state: `install_signal_handlers`
    below is the separate, explicit call a caller makes for that, and
    this object never makes it on its own behalf (issue #436).
    """

    def __init__(self, config: Config | None = None) -> None:
        """Open every database `config` names, and wire the two managers up."""
        super().__init__()

        if config is None:
            config = Config()

        self.config = config
        self.chain = config.chain
        self.data_dir = config.data_dir
        self.data_dir.mkdir(exist_ok=True, parents=True)

        self.terminate_flag = threading.Event()
        log_path = self.data_dir / config.log_path if config.log_path else None
        self.logger = Logger(log_path, debug=config.debug)

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

        # A `getcfilters` answer `p2p.callbacks.get_cfilters` could not
        # finish scheduling under its own pacing bound, keyed by
        # connection id: the connection itself and the heights still
        # owed. `p2p.callbacks.advance_cfilters` and
        # `p2p.main.resume_cfilters` are the only two that read or write
        # this, and both run on this thread -- `run`'s own loop below,
        # under `handle_p2p` or under `resume_cfilters` directly -- so
        # nothing here needs a lock. btclib-org/btclib-node#442
        self.pending_cfilters: dict[int, tuple[Connection, deque[int]]] = {}

        # Built on first use, by the property below: the pool is
        # interpreters under a GIL build (spawned rather than forked
        # wherever that is the platform's default) or threads under a
        # free-threaded one (issue #388), and a node that never
        # validates a script should not pay for either. Which is most
        # of them -- a node serving headers, a node under test, a node
        # that has nothing to connect -- and each one that does pay
        # competes for the cores with the nodes that are actually
        # validating.
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
        """The pool `interpreter.py` validates a script in, built on first use.

        `_pool_factory` picks the type against `sys._is_gil_enabled()`
        read here, once, rather than inside that function: a `Pool`
        under a GIL build, a `ThreadPool` under a free-threaded one
        (issue #388). Under the lock, so that two callers building it at
        once get one pool between them: the second would otherwise leave
        a pool with nothing holding it and nothing to terminate it.
        """
        with self._worker_pool_lock:
            if self._worker_pool is None:
                self._worker_pool = _pool_factory(gil_enabled=sys._is_gil_enabled())(
                    processes=_WORKER_COUNT
                )
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
        """Terminate the worker pool if `run` never got to, as a backstop.

        For a `Node` built and used without ever being `start()`ed --
        `tests/unit/main_test.py` calls `update_chain` against one
        directly, on the thread that built it, to reach a block's own
        validation without a loop around it, and that builds a real
        `worker_pool` that `run`'s own teardown below never runs to take
        back down. Without this, that pool is still in `Pool.RUN` state
        whenever something finally collects it, which is what made
        `Pool.__del__` warn and reach for a queue of its own on an xdist
        worker's stderr, reported against btclib-org/btclib-node#195.
        `getattr` rather than the attribute itself: `__init__` can raise
        before `_worker_pool` is set, and a constructor's exception
        should not come back paired with a second one out of here.
        """
        if getattr(self, "_worker_pool", None) is not None:
            self._close_worker_pool()

    def warm_worker_pool(self) -> None:
        """Build the worker pool now, on a thread of its own, and warm it.

        `check_transactions`' own first call used to be what built and
        warmed `worker_pool`, on whatever thread called it -- `run`'s
        own loop below, the same one that drains
        `p2p_manager.handshake_messages` and promotes a connection once
        its `verack` arrives. Under `_pool_factory`'s process arm, each
        of the pool's own processes pays its own import of
        `btclib_node.interpreter` (and, through it,
        `btclib.script.engine`) the first time it is dispatched a task,
        and while that first dispatch is running, the loop below cannot
        drain that queue: a peer whose `verack` the kernel already
        delivered sits unpromoted until the call returns
        (btclib-org/btclib-node#262). Under the thread arm every worker
        already shares the one import this process paid when `Node`
        itself was imported, so the same dispatch below costs this
        module nothing there -- and is still made, both arms being one
        call site and the warm-up being harmless where it is not needed.

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
            self.worker_pool.starmap(warm, [()] * (_WORKER_COUNT * 4))

        self._worker_pool_warmup = threading.Thread(target=build_and_warm, daemon=True)
        self._worker_pool_warmup.start()

    def _drain_message_queues(self) -> bool:
        """Handle whatever is waiting, and answer whether nothing was.

        One message must not end the node. `handle_p2p` and
        `handle_p2p_handshake` already answer a bad message by dropping
        the peer, but what reaches here is whatever they did not expect
        -- and leaving `run`'s own loop by exception skips every close
        below it, so the databases would stay open.

        `resume_cfilters` is last and unconditional, not one more queue
        to size a share from: nothing is queued to trigger it, a paused
        `getcfilters` answer being owed regardless of what else this
        pass finds waiting.
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
            if resume_cfilters(self):
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

        # Set before either manager starts, on this thread rather than
        # a manager's own: `listening` is set on a manager's thread, so
        # a caller whose wait ends there learns nothing about whether
        # this assignment has run yet, and a write racing it here can
        # put `status` back below `HeaderSynced` for good (#398).
        self.status = NodeStatus.SyncingHeaders
        if self.p2p_port:
            self.p2p_manager.start()
        if self.rpc_port:
            self.rpc_manager.start()
        while not self.terminate_flag.is_set():
            if self._drain_message_queues():
                time.sleep(IDLE_SLEEP_SECONDS)
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


def install_signal_handlers(node: Node) -> None:
    """Stop `node` on SIGINT, SIGTERM and SIGTSTP, process-wide.

    `signal.signal` keeps one handler per signal per process, replacing
    whatever was there before, so this is for the one caller in a
    process that wants an operator's interrupt to reach a node at all --
    `scripts/chains/`, and whatever a future CLI ends up being. Calling
    it a second time, for a second node, replaces the first node's
    handler rather than adding to it: that is the same `signal.signal`
    the first call made, not a defect this function introduces.

    Kept out of `Node.__init__` for two reasons (issue #436). A second
    `Node` built in one process used to silently disown the first,
    every call installing a fresh handler bound to the newer node with
    nothing to say the first one's databases were still open behind it
    -- every functional p2p test builds two nodes, so the first node's
    handlers survived only for the length of the second one's
    constructor call. And `signal.signal` raises outside the main
    thread of the main interpreter, so a `Node` could not be built at
    all from a worker thread, whether or not that caller ever wanted a
    process-wide interrupt.

    SIG_DFL/SIG_IGN are `signal.signal`'s own other two handler
    arguments, POSIX's only alternatives to a callable one; every
    handler is called with `(signum, frame)`, unread here since the
    three signals below share one handler and `stop` takes neither.
    SIGTSTP is for hibernation and does not exist on Windows
    (btclib-org/btclib-node#429).
    """

    def stop_handler(_signum: int, _frame: FrameType | None) -> None:
        node.stop()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGTSTP, stop_handler)
