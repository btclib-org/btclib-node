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
        """Ask the main loop to stop, and wait until it has.

        Signalling alone lets the caller go on while the node is still
        there. A test that returns then is torn down around a thread
        that goes on logging into a harness that has moved on, which is
        where `ValueError: I/O operation on closed file` came from; and
        a loop that cannot come back at all holds the interpreter open
        after the last test, this being a non-daemon thread. No
        per-test limit reaches that second one -- the test it belongs
        to has already passed -- so waiting here is what puts the wait
        inside the test, where a limit can name it
        (btclib-org/btclib_node#98).

        The node's own thread is the one caller that cannot wait, the
        `stop` RPC being handled inside the loop it stops. It gets the
        signal alone, and reaches the end of `run` by itself.
        """
        self.terminate_flag.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join()
