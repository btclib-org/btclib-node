# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import signal
import threading
import time
from math import log as ln
from multiprocessing.pool import Pool

from btclib_node.block_db import BlockDB
from btclib_node.chainstate import Chainstate
from btclib_node.config import Config
from btclib_node.constants import NodeStatus
from btclib_node.download import DownloadManager
from btclib_node.log import Logger
from btclib_node.main import update_chain
from btclib_node.mempool import Mempool
from btclib_node.p2p.address import PeerDB
from btclib_node.p2p.main import handle_p2p, handle_p2p_handshake
from btclib_node.p2p.manager import P2pManager
from btclib_node.rpc.main import handle_rpc
from btclib_node.rpc.manager import RpcManager

# How long `stop` waits for the loop to come back before saying it did
# not. The flag is read at the top of the loop, so the wait is however
# long the pass already running takes: `update_chain` validates a whole
# fork through a blocking `worker_pool.starmap` and checks nothing in
# between, which is the term that sets the scale -- an idle stop costs
# milliseconds. Well under the per-test limit `pyproject.toml` sets, so
# that a node which will not stop is reported here rather than by
# whichever bound expires first; `tests/unit/__init__.py` asserts that
# ordering rather than leaving it to this comment.
STOP_TIMEOUT = 30


class Node(threading.Thread):
    def __init__(self, config=Config()):
        super().__init__()

        def stop_handler(signal, frame):
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

        self.worker_pool = Pool(processes=8)

        self.status = NodeStatus.Starting

        self.download_manager = DownloadManager(self, self.logger)

        if config.p2p_port:
            self.p2p_port = config.p2p_port
        else:
            self.p2p_port = None
        peer_db = PeerDB(self.chain, self.data_dir)
        self.p2p_manager = P2pManager(self, self.p2p_port, peer_db)

        if config.rpc_port:
            self.rpc_port = config.rpc_port
        else:
            self.rpc_port = None
        self.rpc_manager = RpcManager(self, self.rpc_port)

    def run(self):
        self.logger.info("Starting main loop")

        if self.p2p_port:
            self.p2p_manager.start()
        if self.rpc_port:
            self.rpc_manager.start()
        self.status = NodeStatus.SyncingHeaders
        while not self.terminate_flag.is_set():
            wait = True
            # One message must not end the node. handle_p2p and
            # handle_p2p_handshake already answer a bad message by
            # dropping the peer, but what reaches here is whatever they
            # did not expect -- and leaving the loop by exception skips
            # every close below it, so the databases would stay open.
            try:
                while len(self.p2p_manager.handshake_messages):
                    handle_p2p_handshake(self)
                    wait = False
                for _ in range(int(ln(len(self.rpc_manager.messages) + 1, 2))):
                    handle_rpc(self)
                    wait = False
                for _ in range(int(ln(len(self.p2p_manager.messages) + 1, 2))):
                    handle_p2p(self)
                    wait = False
            except Exception:
                self.logger.exception("Exception occurred handling a message")
            if wait:
                time.sleep(0.0001)
            try:
                self.download_manager.step()
                update_chain(self)
            except Exception:
                self.logger.exception("Exception occurred")
                break
        self.p2p_manager.stop()
        self.rpc_manager.stop()

        self.chainstate.close()
        self.block_db.close()

        self.worker_pool.terminate()

        self.logger.info("Stopping node")
        self.logger.close()

    def stop(self):
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
                raise Exception(err_msg)
