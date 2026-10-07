# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The script checks of relayed transactions, run off `Node`'s thread.

`p2p.callbacks` runs every check but the scripts of a relayed
transaction, of an orphan taken up again and of a parent with its child,
on `Node`'s thread, and queues the candidate here.
`p2p.main.resume_tx_checks` hands the scripts to `Node.worker_pool`, one
candidate at a time, and applies the verdict back on `Node`'s thread,
so the loop goes on serving other peers while it runs; the sending
peer's later messages wait for the verdict and for any orphan it is to
reconsider.

At most one candidate per peer is queued or checked at a time. A
peer's later messages of every command wait here, in order, still
weighed against its `queued_recv_bytes`, while `p2p.main._hold_message`
says the peer has work before them.
Candidates are checked in the order they were queued, so a peer that
sends again goes to the back. Core's message handler reads one message
of each peer per pass, its peers shuffled each time
(`CConnman::ThreadMessageHandler`, `src/net.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag); a queue holding each peer at
most once is as fair, without the shuffle.

Everything here is read and written on `Node`'s thread alone, so it
needs no lock. A worker reads only the transaction and its prevouts: a
`TxOut` is frozen, and nothing writes to the `Tx`, which the orphanage
may also hold, until the verdict is in.

`sendrawtransaction` and `testmempoolaccept` check scripts on `Node`'s
thread, as Core's RPC holds `cs_main` across the check and its message
handler waits on that lock (`src/node/transaction.cpp`,
`src/rpc/mempool.cpp` and `PeerManagerImpl::SendMessages`, same tag).
"""

import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from btclib_node.interpreter import check_package, check_transaction

if TYPE_CHECKING:
    from collections import deque
    from multiprocessing.pool import AsyncResult

    from btclib.tx.tx import Tx
    from btclib.tx.tx_out import TxOut

    from btclib_node import Node
    from btclib_node.p2p.connection import Connection

__all__ = ["TX_CHECK_DEADLINE", "TxCheck", "TxChecks"]

# Seconds a check may stay on the pool without a verdict. A worker that
# dies mid-task, killed or crashed, leaves its result unready forever,
# and every peer's `tx` would wait behind it. The bound has to clear the
# costliest standard check on a loaded machine. It matters only once a
# worker is gone, so it is set far above that. A dropped candidate is
# not recorded as refused: its check may only have been slow.
TX_CHECK_DEADLINE = 300


@dataclass
class TxCheck:
    """A relayed transaction whose scripts are queued or being checked.

    `parent` is set for a package: `tx` is then the child, and `parent` the
    transaction it is checked and kept with, and its prevouts. `first_time`
    is `False` for a transaction taken from the orphanage or a package, which
    Core's `ProcessInvalidTx` does not look for a package for. `failed` is
    set by `TxChecks.finish` to the position of the transaction of a package
    whose scripts failed, parents first.
    """

    conn: Connection
    tx: Tx
    prev_outputs: list[TxOut]
    parent: tuple[Tx, list[TxOut]] | None = None
    first_time: bool = True
    failed: int = 0

    def hashes(self) -> set[bytes]:
        """Return the txids and wtxids of what is being checked."""
        txs = [self.tx] if self.parent is None else [self.parent[0], self.tx]
        return {tx_hash for tx in txs for tx_hash in (tx.id, tx.hash)}


class TxChecks:
    """The candidates awaiting a script check, and the messages behind them."""

    def __init__(self) -> None:
        """Start with nothing queued."""
        # by connection id, in the order queued; the one in flight included
        self.queued: dict[int, TxCheck] = {}
        # the txids and wtxids of `queued`, each counted once per candidate
        self._pending: Counter[bytes] = Counter()
        # a peer's messages not read yet: command, payload, weight, time read
        self.waiting: dict[int, deque[tuple[str, bytes, int, float]]] = {}
        # the one check handed to the pool, its pending verdict, and the
        # `time.monotonic()` it is due by
        self._in_flight: tuple[TxCheck, AsyncResult[Any], float] | None = None

    @property
    def checking(self) -> bool:
        """Whether a check is on the pool."""
        return self._in_flight is not None

    def busy(self, conn_id: int) -> bool:
        """Answer whether a check queued or a message held holds this peer."""
        return conn_id in self.queued or conn_id in self.waiting

    def queue(self, check: TxCheck) -> None:
        """Queue `check` behind the candidates already queued."""
        self.queued[check.conn.id] = check
        self._pending.update(check.hashes())

    def unqueue(self, conn_id: int) -> TxCheck:
        """Take this peer's candidate off the queue."""
        check = self.queued.pop(conn_id)
        for tx_hash in check.hashes():
            self._pending[tx_hash] -= 1
            if not self._pending[tx_hash]:
                del self._pending[tx_hash]
        return check

    def pending(self, tx_hash: bytes) -> bool:
        """Answer whether a queued candidate has this txid or wtxid."""
        return tx_hash in self._pending

    def start(self, node: Node, check: TxCheck) -> None:
        """Hand `check`'s scripts to `node.worker_pool`."""
        result: AsyncResult[Any]
        if check.parent is None:
            result = node.worker_pool.apply_async(
                check_transaction, (check.prev_outputs, check.tx)
            )
        else:
            parent, parent_outputs = check.parent
            package = [(parent_outputs, parent), (check.prev_outputs, check.tx)]
            result = node.worker_pool.apply_async(check_package, (package,))
        self._in_flight = (check, result, time.monotonic() + TX_CHECK_DEADLINE)

    def finish(self) -> tuple[TxCheck, Exception | None] | None:
        """Take the check off the queue once its verdict is in.

        Answers the check and what its scripts raised, if anything.
        """
        if self._in_flight is None or not self._in_flight[1].ready():
            return None
        (check, result, _), self._in_flight = self._in_flight, None
        self.unqueue(check.conn.id)
        try:
            failure = result.get()
        except Exception as refusal:  # noqa: BLE001
            return check, refusal
        if failure is None:
            return check, None
        check.failed, failed_with = failure
        return check, failed_with

    def drop_overdue(self) -> TxCheck | None:
        """Drop the check in flight once past its deadline with no verdict."""
        if self._in_flight is None or time.monotonic() < self._in_flight[2]:
            return None
        check, self._in_flight = self._in_flight[0], None
        self.unqueue(check.conn.id)
        return check
