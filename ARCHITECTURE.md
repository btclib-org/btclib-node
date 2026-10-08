# Architecture

btclib-node is a full node: it runs the loop that drives
[btclib](https://github.com/btclib-org/btclib)'s objects, and
`src/btclib_node/p2p/` and `src/btclib_node/rpc/` are what an untrusted
peer and a local caller each reach it through. This page is its
high-level design: the loop, the threads and the state each one may
touch, the store, and what is validated where. What a user can expect of
it in terms of security is [SECURITY](./SECURITY.md), and why those
expectations hold is the [assurance case](./ASSURANCE_CASE.md).

## The loop

`Node` (`src/btclib_node/__init__.py`) is a thread running one loop: it
drains the handshake queue, then a share of the RPC queue and a share of
the peer-to-peer queue, steps the RPC requests still waiting, moves the
script checks of relayed transactions along, then steps the download
manager and extends the chain. A message that raises is logged and the
loop continues; a failure in the download manager's step or in
`update_chain` is logged and ends the node, the databases below being
closed on the way out.

## The protocol and the RPC surface

- `src/btclib_node/p2p/` is the protocol — the connections, the peer
  manager, the address book and the message handlers the loop calls.
- `src/btclib_node/rpc/` is the JSON-RPC surface, on the same shape of
  manager and handler.

A peer's messages are handled in the order received. `_hold_message`
(`src/btclib_node/p2p/main.py`) says what makes the later ones wait.

`P2pManager` and `RpcManager` are each a thread of their own, running an
asyncio loop, and a coroutine enters that loop only through
`run_coroutine_threadsafe`. Their plain methods are another matter:
`verack` calls `promote_connection` directly, from `Node`'s thread. So
what decides whether a piece of state needs a lock is which thread
reaches it, never which callback names it — `handle_p2p`,
`handle_p2p_handshake` and `handle_rpc` run on `Node`'s own loop, and so
does `update_chain` beside them. `Mempool` is reached from that one
thread and no other, its `add_tx` and `remove_tx` being called from the
p2p callbacks, the rpc callbacks and `update_chain`: its own "handled in
same thread" comment needs no lock to back it. `PeerDB`, the address
book above, is not so lucky: `add_active_address` arrives from the
`version` callback on `Node`'s thread and from a feeler's draw on
`P2pManager`'s, `resolve_collisions` from `manage_connections` on
`P2pManager`'s, and `add_addresses` from both —
gossip on one thread, a DNS answer on the other. It carries two locks
for that reason, one per table, taken separately and never nested, and
a third, taken first, that a move between the tables holds throughout.

`FeeEstimator` (`src/btclib_node/fee_estimator.py`) is reached from
`Node`'s thread alone, so it carries no lock.

While `Node.load` opens the stores, `Node`'s thread reads no queue.
`RpcManager` is then in warmup: `RpcConnection.run` answers each request
on the manager's thread with `RPC_IN_WARMUP` and the current init message,
as Core's `CRPCTable::execute` does, and queues none. `Node.run` ends
warmup once both listeners are up. `queue_lock` is held from reading the
flag to queuing or answering, so a request is answered in warmup or queued
after it, never both.

Core answers each RPC request on an HTTP worker thread, so a call that
waits for the tip, or searches nonces, holds only that thread. Here such
a call stays on `Node`'s thread, which owns the state it reads: it is a
generator, and the loop runs it a step at a time, serving peers and
other requests between steps. `src/btclib_node/rpc/main.py` has how.

`gettxoutsetinfo` with `hash_serialized_3`, Core's default, is the one
call whose work leaves `Node`'s thread. `Node`'s thread flushes the
chainstate and opens a cursor over the stored coins, whose view RocksDB
fixes there; a thread of the call's own hashes them, reading no state of
`Node`'s but `terminate_flag`, and the call is a generator waiting for
it, as the others are. `stop` ends the scan at its next coin.

`dumptxoutset` is not: it writes the snapshot on `Node`'s thread, a step of
coins at a time. A rollback dump invalidates the block after its target,
writes, and reconsiders it, with the network off meanwhile. The invalidate
and the reconsider run without yielding, so a deep rollback holds `Node`'s
loop, where Core does it on an HTTP worker thread.

`scantxoutset` is not either: it walks the same cursor on `Node`'s thread,
a step of coins at a time, and yields between steps. `status` and `abort` are
served between two steps, so the scan's state needs no lock.

## The transport

`src/btclib_node/p2p/transport.py` frames messages for v1 and
`src/btclib_node/p2p/v2transport.py` for BIP324's v2, both Core's
`Transport` without I/O. A `Connection` holds one and feeds it from its
own loop, and `p2p/transport.py`'s module docstring says which thread
touches each half. A `V2Transport` that accepted a connection tells v1
from v2 by the first 16 bytes. With `-v1transport=0`, the default, it has
no v1 transport to hand them to, and the connection is dropped.
SECURITY.md's *Where this node departs from Bitcoin Core* says why.

## The chain state and the store

- `src/btclib_node/chainstate/` is the block index, the UTXO set and the
  compact filter index; `src/btclib_node/block_db/` is the blocks and
  their undo data. **Genesis sits at index 0 of the active chain**, so
  `active_chain[i]` has height `i` and `len(active_chain)` is the height
  a block extending the chain would connect at — which is what a mempool
  check wants, a transaction there being judged as if it were in the
  next block, and is Core's own `GetSpendHeight`
  (`m_chain.Height() + 1`).
- `src/btclib_node/db.py` is the ordered key-value store all of those are
  kept in, RocksDB through `rocksdict`. Its own docstring is where that
  choice is argued against Bitcoin Core's LevelDB, and **key order is
  load-bearing**: a reader that stops at the first key without its
  prefix is truncated by a prefix that sorts before it.

## Validation

`src/btclib_node/interpreter.py` checks a block's transactions against
the consensus rules `btclib.script.engine` implements, fanned out across
`Node.worker_pool` — a process pool under a GIL build, a thread pool
under a free-threaded one, one script check per worker. The rules
themselves, and the objects they run against — a `Tx`, a `Block`, a
script — are btclib's; what is here is the dispatch across workers and
the chain state a verdict is checked against. `-assumevalid` skips a
block's script checks, and only those, under Core's conditions.

A transaction a peer relays has its scripts checked on `Node.worker_pool`
too, one candidate at a time (a transaction, or a parent with its child),
so the loop goes on serving other peers while they run, the sending
peer's later messages waiting for the verdict and for any orphan it is to
reconsider (`src/btclib_node/p2p/tx_checks.py`). `Node`'s thread runs
every other check first, and applies the verdict once it is in, after
running those checks again against the chain and the mempool as they
are then. The worker reads only the transactions and their prevouts; the
queue of candidates, the mempool and the chain state stay on `Node`'s
thread. `sendrawtransaction`, `submitpackage`, `testmempoolaccept` and the
transactions a reorg puts back in the mempool are checked on `Node`'s
thread, scripts included.

## What is delegated, and what is not

Every object on the wire — a message, a block, a transaction, a script,
a filter — and its serialization come from btclib, and so does the
consensus rule that decides whether a transaction or a block is valid.
btclib-node does not reimplement any of that: it is the loop above, the
threads and the locking that loop needs, the store, and the two surfaces
a peer and a caller reach it through. `tests/unit/` mirrors the layout
above, `tests/functional/` builds a node and speaks to it over a socket,
and `tests/integration/` runs one against a real `bitcoind`
(`.github/workflows/integration-bitcoind.yml`).
