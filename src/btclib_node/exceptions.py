# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Exception classes.

`TRY002`/`TRY003` (issue #284) ask two things of a `raise`: that its
class not be the bare `Exception`/`BaseException` every `except
Exception` also catches, and that a message built ad hoc at the call
site move into the class that carries it, so the same failure reads
the same way everywhere it is raised. What is below groups by what
actually went wrong rather than by which module raises it, since two
call sites in different files can be the same failure -- `db.py`'s two
"the store is closed" sites are one class for exactly that reason.

Grouped this way rather than one class per call site: most of these
raise once, are never caught by type (they propagate out of
`update_chain` or a p2p/RPC handler and end the node or the one
connection, per this file's own docstring elsewhere in this tree), and
a class per site would be a name invented for a message read exactly
once. What groups here shares an actual class of failure instead --
`ChainstateInconsistencyError` is every site downstream of
`update_chain` finding that its own index promised something the data
underneath it does not have, whichever index asked -- except where the
data in question is a candidate block's own, not yet validated at all:
`InvalidBlockInputError` is that one, `UtxoIndex.add_block`'s own two
sites, which fire on a peer's bad block rather than this tree's own
bug.
"""

from btclib.exceptions import BTClibValueError

__all__ = [
    "ChainstateInconsistencyError",
    "IncompatibleStoreError",
    "InvalidBlockInputError",
    "InvalidChainTypeError",
    "InvalidRejectPayloadError",
    "MissingPrevoutError",
    "NodeShutdownTimeoutError",
    "PrevoutCountMismatchError",
    "PruningNotImplementedError",
    "ReimportedMainProcessError",
    "StoreClosedError",
    "StoreCorruptionError",
    "UnknownChainError",
    "UnsupportedAddressTypeError",
    "WrongNetworkMagicError",
]


class MissingPrevoutError(ValueError):
    """A transaction's input spends an output this node cannot find.

    Raised only by `verify_mempool_acceptance` (`main.py`), while it
    walks a candidate mempool transaction's own inputs against the UTXO
    set and the mempool together and neither has the prevout --
    `InvalidBlockInputError` below is the same check, made instead while
    a freshly-downloaded candidate block is first connected.
    """


class ChainstateInconsistencyError(RuntimeError):
    """The node's own index promised something its data does not have.

    Raised only where an earlier check already established the
    invariant this violates -- a block marked downloaded that
    `block_db` does not hold, a reverse patch `set_status` already
    trusts that is not on disk, a UTXO `apply_rev_block` is asked to
    remove that this node's own earlier `add_block` did not just add, a
    stored `utxo-` record `add_block`'s own prevout resolution or
    `UtxoIndex.get_coin` reads back that does not parse. Those
    last two differ from the other three in shape -- the key is found,
    so nothing upstream misnamed it, only the bytes underneath are
    wrong -- but not in kind: the same test that separates this class
    from `InvalidBlockInputError` below, which shares two of its
    messages ("prevout not found", "prevout already spent in this
    batch") but not the invariant, applies here too. `apply_rev_block`
    only ever inverts a reverse patch this node wrote for a block it
    already validated and connected, and a `utxo-` key is one only this
    node's own `finalize` ever writes, whichever of the two reads it,
    so a failure at any of these is this tree's own bookkeeping
    disagreeing with itself. A peer's bad data, or a submitted
    transaction's own content, is refused earlier and differently
    (`BTClibException`, a `None` handled in place, `InvalidBlockInputError`,
    or `MissingPrevoutError`); reaching here is never that.

    Core's own read path answers a fault of this shape by treating the
    record as absent, but it is not answering *this* fault, and the
    difference between the two trees is where the detection sits rather
    than a choice either made. `CDBWrapper::Read`
    (`src/dbwrapper.h:220-237`, at bitcoin/bitcoin@05e49b342f) catches a
    deserialize failure inside its own `try` and returns `false`, and
    `CCoinsViewDB::GetCoin` (`src/txdb.cpp:88-95`) turns that `false`
    into `std::nullopt` -- the coin reads back as absent, never as an
    error. Core's own `Read` never actually sees storage-layer
    corruption there: `CDBWrapper` reads with `verify_checksums = true`
    (`src/dbwrapper.cpp:248`), and a checksum mismatch reaches
    `HandleError` (`src/dbwrapper.cpp:46-53`), which throws
    `dbwrapper_error` and ends the process, before `ReadImpl`
    (`src/dbwrapper.cpp:346-357`) ever returns and `Read`'s own `try`
    runs at all -- the deserialize failure that `try` actually catches
    is a format mismatch on an intact, checksummed read, not bit rot.
    `db.py`'s own store now carries an equivalent guard beneath it -- a
    per-block checksum, verified on every read, the same mechanism
    `verify_checksums = true` gives Core (btclib-org/btclib-node#641) --
    so a genuinely corrupted `utxo-` record is caught there, as
    `StoreCorruptionError`, before either `Coin.parse` call above ever
    runs on it. What that leaves unsettled is what a `Coin.parse`
    failure means on a record the guard above already passed as intact,
    which is not answered here and is tracked at
    btclib-org/btclib-node#650. Answering Core's own way -- silently
    absent -- would fold that node-owned fault into an ordinary "prevout
    missing" refusal, indistinguishable from a legitimate one and
    invisible to whoever operates this node; raising instead puts a name
    in the log to grep for.

    What Core's node does *after* its own storage layer throws is not
    settled here, and this class does not rest on it: `rpc/server.cpp`'s
    `JSONRPCExec` and `net_processing.cpp`'s own per-message dispatch
    each catch `std::exception`, which `dbwrapper_error` is, so those
    two paths may absorb it where Core's `AbortNode`/`FatalError`
    machinery and its own "Fatal LevelDB error" wording say the intent
    is to stop. Settling that needs more of Core's history than either
    of this tree's two call sites depends on: what they rest on is the
    paragraph above, which is measured -- Core detects at the storage
    layer, and this tree's own store now does too
    (btclib-org/btclib-node#641), so what a `Coin.parse` failure at
    either of these two sites still means is btclib-org/btclib-node#650's
    own question, not this class's. (btclib-org/btclib-node#636)

    `message` and not a structured payload per call site: what is
    inconsistent (a hash, a count, a status) differs by call site, and
    every one of them is read exactly once, in whatever it raises to.

    Unlike most of this file's classes, this one does not always end
    the node: `verify_mempool_acceptance` (`main.py`) raises it too, and
    every caller that reaches it already answers an exception outside
    `MissingPrevoutError` and `BTClibValueError` its own way, by a
    design none of them had to change for this. `update_chain`'s own
    reorg reconciliation lets it propagate and end the node, the same as
    every other site above -- reasonably so: it fires there mid-way
    through applying a fork to this node's own already-committed
    chainstate, and continuing to run this node once an index has been
    found disagreeing with its own data mid-mutation is not safe.
    Refusing one mempool entrant carries none of that risk -- nothing
    downstream of `verify_mempool_acceptance` is mid-mutation of anything
    -- which is what lets its own three callers answer this without
    ending the node. The p2p `tx` handler (`p2p/callbacks.py`)
    lets it propagate into `handle_p2p`'s own generic catch, which stops
    that one connection without discouraging the peer,
    `isinstance(e, BTClibException)` being false for it.
    `send_raw_transaction` (`rpc/callbacks.py`) has no catch of its own
    for it, so it propagates further, into `handle_rpc`'s own generic
    catch, answered to the caller as an internal-error verdict rather
    than a refusal of the transaction; `test_mempool_accept`'s own
    per-entry loop already carries that same catch-all itself, one
    layer closer, answering the one entry `"Unknown error"` rather than
    ending the batch. (btclib-org/btclib-node#631)
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class InvalidBlockInputError(ValueError):
    """A candidate block's own transactions fail a UTXO consistency check.

    Raised only by `UtxoIndex.add_block`, while it is walking a
    freshly-downloaded candidate block's own transactions against the
    UTXO set for the first time -- before `check_transactions` (script
    and signature checks) even runs, per `update_chain`'s own sequence
    in `main.py`. An input spending an output this index cannot find,
    or spending one a transaction earlier in the same block already
    spent, is exactly what a malicious or malformed block looks like,
    not a bug in this node's own bookkeeping -- `ValueError`, like
    `MissingPrevoutError` above (the same failure, checked at a
    different point: mempool reprocessing after a reorg, not connecting
    a new block), and not `ChainstateInconsistencyError`, whose whole
    point is the opposite claim.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class NodeShutdownTimeoutError(TimeoutError):
    """`Node.stop()`'s own join outlived `STOP_TIMEOUT`.

    The thread it waited for is still running once this is raised, so
    -- unlike `ChainstateInconsistencyError` -- nothing downstream of
    this call can trust the chainstate or the databases the wedged
    thread might still be writing.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ReimportedMainProcessError(RuntimeError):
    """`Node()` ran off the main process without saying that was meant.

    Raised where `multiprocessing.current_process().name` is not
    `"MainProcess"` and the active start method is not `fork` -- exactly
    the shape a `Pool` worker's own bootstrap produces
    (`multiprocessing.spawn.import_main_path` re-importing `__main__` to
    find the target it was asked to run), which is what let
    `scripts/chains/*.py` build a second `Node` on the same data
    directory in every worker `Node.worker_pool` spawned before those
    three scripts guarded their own module body (issue #579). This is
    the same failure caught one layer up, for every caller rather than
    only the three this tree ships (issue #589) -- unless the caller
    passed `Node(..., allow_reimported_main=True)`, which this class
    never sees raised against, since `Node.__init__` checks that flag
    before either of the two calls this docstring names. A caller that
    reaches this exception has not opted in, so the two things the
    message offers -- a module-body guard, or that same flag -- are
    both live for it, whichever this actually was.
    """

    def __init__(self, process_name: str) -> None:
        super().__init__(
            f"Node built inside process {process_name!r}, which "
            "multiprocessing re-imported __main__ to create. If this is "
            'unintended, guard the caller with `if __name__ == "__main__":`; '
            "if it is deliberate -- a supervisor building a Node inside its "
            "own pool worker, say -- pass "
            "`Node(..., allow_reimported_main=True)`."
        )


class StoreClosedError(ValueError):
    """A `KeyValueStore` method was called after `close()`.

    `ValueError`, matching the standard library's own `io` objects:
    reading or writing a closed file raises `ValueError: I/O operation
    on closed file`, not a bespoke class, and a `KeyValueStore` is the
    same shape of resource.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class StoreCorruptionError(RuntimeError):
    """The store itself found its own bytes unreadable, at a read.

    Raised only by `db.KeyValueStore`, at every point it reads from the
    RocksDB store beneath it -- `get`, `__iter__`, and the open itself
    -- on a `rocksdict` exception whose message begins `Corruption:`.
    `rocksdict` gives no typed class for that fault (only `DbClosedError`
    is typed, per its own `.pyi`), so `db.py`'s own classification is a
    string match on the message, argued where it sits.

    A `KeyValueStore` is `PeerDB`'s own store as well as every
    chainstate index's, so this is deliberately not
    `ChainstateInconsistencyError`: a corrupted address book is a p2p
    concern, not a chainstate one, and folding the two into one class
    would make every catch of it answer a question about which store it
    was. `RuntimeError`, matching `ChainstateInconsistencyError`'s own
    choice and Core's `dbwrapper_error` (`src/dbwrapper.h`), for the
    same reason: whichever caller this reaches, continuing to run past
    a store that has just reported disagreeing with its own bytes is
    not safe, and nothing here narrows that per call site the way
    `ChainstateInconsistencyError`'s own three exceptions from ending
    the node do.

    `db.py`'s own module docstring is where the checksum this class
    detects is argued against Core's `verify_checksums = true`
    (btclib-org/btclib-node#641, closing btclib-org/btclib-node#637);
    what a chainstate caller does with this once it is raised --
    whether it is ever mapped onto `ChainstateInconsistencyError` rather
    than left to propagate as itself -- is unsettled and tracked at
    btclib-org/btclib-node#650, the same issue
    `ChainstateInconsistencyError`'s own docstring points at for the
    related question of what a `Coin.parse` failure means once this
    guard has already passed a record as intact.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class IncompatibleStoreError(RuntimeError):
    """A data directory holds a store this version cannot open.

    Two cases raise this: a directory `db.KeyValueStore` wrote a
    `sqlite3` file into, the format this class replaced
    (#107, #641) and this one cannot read, and a `KeyValueStore` written
    by a version that kept a different shape under one of its keys or
    in a `block_db` flat file -- `db.py`'s own `_SCHEMA_VERSION` is
    where that second case is checked and argued.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class UnknownChainError(ValueError):
    """`Config`'s own `chain` string names no chain this tree knows."""

    def __init__(self, chain: str) -> None:
        super().__init__(f"unknown chain: {chain!r}")


class InvalidChainTypeError(TypeError):
    """`Config`'s own `chain` is neither a `Chain` nor a `str`.

    `TypeError` and not `ValueError`: this is the argument being the
    wrong *type* entirely, which `TRY004` is what noticed the tree
    already had a `ValueError` for -- `unknown chain`, `UnknownChainError`
    above, is the right *type* with the wrong *value*, and stays one.
    """

    def __init__(self, chain: object) -> None:
        super().__init__(f"chain must be a Chain or str, not {type(chain).__name__}")


class PruningNotImplementedError(NotImplementedError):
    """`Config`'s own `pruned` was given `True`, and nothing here prunes.

    A Bitcoin Core node that prunes drops `NODE_NETWORK` from the
    services its own `version` advertises, keeping only
    `NODE_NETWORK_LIMITED` -- a promise of at least the last
    `MIN_BLOCKS_TO_KEEP` (288, two days), never of the whole chain --
    and deletes a block file once it falls further behind the tip than
    that (`src/init.cpp`, `src/validation.h`, at
    bitcoin/bitcoin@05e49b342f). `BlockDB` here deletes nothing, and
    `p2p/connection.py`'s `send_version` sets both bits unconditionally,
    so honouring `pruned=True` today would mean serving a promise this
    tree cannot keep. `pruned` stays a reserved name rather than a
    silently-ignored one -- btclib-org/btclib-node#601 is where the rest
    of it is built; until it lands, `Config` accepts only its default,
    `False`.
    """

    def __init__(self) -> None:
        super().__init__(
            "pruning is not implemented (btclib-org/btclib-node#601); "
            "Config(pruned=True) is refused rather than silently ignored"
        )


class PrevoutCountMismatchError(ValueError):
    """`check_transactions` was handed a prevout list the wrong length."""

    def __init__(self) -> None:
        super().__init__("prevout count does not match input count")


class UnsupportedAddressTypeError(ValueError):
    """`dial` was asked to connect an address family it does not speak.

    Every address `dial` reaches has already passed a network filter
    upstream (only the two families `_IP_NETWORKS` names are ever
    handed to it), so reaching here is that filter's own invariant
    broken, not a peer's address.
    """

    def __init__(self) -> None:
        super().__init__("Address type not yet supported")


class WrongNetworkMagicError(BTClibValueError):
    """A message's magic names a chain other than the one this node runs.

    `BTClibValueError` and not a plain `ValueError`: `Connection.run`'s
    own catch discourages the peer on `isinstance(e, BTClibException)`,
    the same test it uses for `Message.parse`'s own refusals, and this
    is the network-magic check that runs right after -- both are the
    peer's envelope being wrong, and both have to satisfy the same
    `isinstance` for the same reason.
    """

    def __init__(self, magic: bytes) -> None:
        super().__init__(f"message for another network: {magic.hex()}")


class InvalidRejectPayloadError(BTClibValueError):
    """A peer's `reject` payload is not the message BIP61 describes.

    Raised only by `Reject.parse` (`p2p/messages/errors.py`), over
    octets a peer chose: a field the payload is too short to hold, a
    `message` or a `reason` no utf-8 decodes, a code outside the set
    BIP61 names, or a trailing hash that is neither absent nor the
    32 octets of one.

    `BTClibValueError` and not a plain `ValueError`, for
    `WrongNetworkMagicError`'s own reason above: `handle_p2p`
    (`p2p/main.py`) discourages the peer on
    `isinstance(e, BTClibException)` and reads anything else as this
    node's own code failing on content that was fine, so a refusal of
    a peer's octets outside that family is logged against the wrong
    party.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"invalid reject payload: {detail}")
