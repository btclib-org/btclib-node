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

from btclib.exceptions import BTClibRuntimeError, BTClibValueError

__all__ = [
    "ChainstateInconsistencyError",
    "IncompatibleStoreError",
    "IncompleteRequestHeadError",
    "InvalidBlockInputError",
    "InvalidChainTypeError",
    "MalformedRequestHeadError",
    "MissingPrevoutError",
    "NodeShutdownTimeoutError",
    "NonStandardTxError",
    "PrevoutCountMismatchError",
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


class NonStandardTxError(BTClibValueError):
    """A mempool candidate the relay rules refuse and the consensus rules take.

    Raised only by `interpreter.check_transaction`, for a candidate that
    fails `interpreter.STANDARD_FLAGS` and passes
    `btclib.script.engine.flags.ALL_FLAGS`: the scripts are ones a block
    may carry, so the refusal is this node's own relay policy rather
    than anything the peer that relayed it did wrong. Core says so where
    it defines the same set -- "we do not ban/disconnect nodes that
    forward txs violating the additional (non-mandatory) rules here, to
    improve forwards and backwards compatibility"
    (`src/policy/policy.h:112-117`, at bitcoin/bitcoin@9be056a8a7) --
    and `p2p.callbacks.tx` is what honours it, catching this and
    dropping the transaction alone.

    `BTClibValueError`, so that both RPC paths answer this through the
    clause they already answer a refused candidate with:
    `rpc.callbacks.test_mempool_accept` reports the entry not allowed
    and `send_raw_transaction` answers `VERIFY_REJECTED`. The cost of
    that base is that `p2p.main.handle_p2p`'s own `isinstance(e,
    BTClibException)` would discourage the peer for it, which is why
    `tx`'s catch is what keeps the peer and has a test of its own.
    """


class ChainstateInconsistencyError(RuntimeError):
    """The node's own index promised something its data does not have.

    Raised only where an earlier check already established the
    invariant this violates -- a block marked downloaded that
    `block_db` does not hold, a reverse patch `set_status` already
    trusts that is not on disk, a UTXO `apply_rev_block` is asked to
    remove that this node's own earlier `add_block` did not just add.
    `apply_rev_block` only ever inverts a reverse patch this node
    wrote for a block it already validated and connected, so a
    failure there is this tree's own bookkeeping disagreeing with
    itself, the same test that separates this class from
    `InvalidBlockInputError` below, which shares two of its messages
    ("prevout not found", "prevout already spent in this batch") but
    not the invariant. A peer's bad data, or a submitted transaction's
    own content, is refused earlier and differently
    (`BTClibException`, a `None` handled in place,
    `InvalidBlockInputError`, or `MissingPrevoutError`); reaching here
    is never that.

    Two call sites used to belong on the list above: a stored `utxo-`
    record `UtxoIndex.add_block`'s own prevout resolution or
    `UtxoIndex.get_coin` reads back that `Coin.parse` cannot read.
    Neither raises this class any more (btclib-org/btclib-node#650),
    and the reasoning is Core's own. `CDBWrapper::Read`
    (`src/dbwrapper.h:220-237`, at bitcoin/bitcoin@ca7162cde5) catches
    a deserialize failure inside its own `try` and returns `false`,
    and `CCoinsViewDB::GetCoin` (`src/txdb.cpp:88-95`) turns that
    `false` into `std::nullopt` -- the coin reads back as absent,
    never as an error. `CDBWrapper` reads with `verify_checksums =
    true` (`src/dbwrapper.cpp:248`), and a checksum mismatch reaches
    `HandleError` (`src/dbwrapper.cpp:46-53`), which throws
    `dbwrapper_error` and ends the process, before `ReadImpl`
    (`src/dbwrapper.cpp:346-357`) ever returns and `Read`'s own `try`
    runs at all -- so the deserialize failure that `try` actually
    catches is a format mismatch on an intact, checksummed read,
    never bit rot. `db.py`'s own store now carries the same
    separation: RocksDB verifies an equivalent per-block checksum on
    every read (btclib-org/btclib-node#641), so a genuinely corrupted
    `utxo-` record is caught there, as `StoreCorruptionError`, before
    either `Coin.parse` call in `UtxoIndex` ever runs on it -- and
    what reaches `Coin.parse` is therefore exactly the case Core
    answers absent to. `UtxoIndex.get_coin` now returns `None` for
    it, and `UtxoIndex.add_block` folds it into the same
    `InvalidBlockInputError` ("prevout not found") a genuinely
    missing prevout already raises, matching `GetCoin`'s own
    `std::nullopt` rather than raising a class of this tree's own
    that Core has no equivalent of at that call.

    What that answer costs is real, and Core pays it too: a coin this
    node wrote itself, that a bug in this node's own serializer alone
    can make unparsable on a checksum-clean read, is from here
    indistinguishable from a coin that never existed, and a later,
    genuinely valid block spending it is rejected. `Consensus::CheckTxInputs`'s
    own `HaveInputs` check (`src/consensus/tx_verify.cpp:169-174`, at
    bitcoin/bitcoin@ca7162cde5) answers a missing coin
    `TX_MISSING_INPUTS`, `"bad-txns-inputs-missingorspent"`, and
    `ConnectBlock` (`src/validation.cpp:2543-2547`) turns that
    failure into `BlockValidationResult::BLOCK_CONSENSUS` -- an
    ordinary consensus rejection, on the wire indistinguishable from
    a block that spent a coin that genuinely never existed. Core
    carries this exposure knowingly, not by oversight: `CDBWrapper::Read`'s
    own `try` has no way to tell "the checksum passed but the bytes
    are not a `Coin`" apart from "nothing is stored here" before it
    ever answers `false` for either, so a local deserialize bug
    surfacing as a rejected valid block is the accepted cost of the
    one read path that keeps every other cause of "missing" simple.
    This tree now pays the identical cost, for the identical reason:
    matching Core's behaviour end to end rather than only the half of
    it that reads comfortably (CLAUDE.md's own *Following Bitcoin
    Core* argues the same trade for `CDBWrapper::Read` in general).

    `message` and not a structured payload per call site: what is
    inconsistent (a hash, a count, a status) differs by call site, and
    every one of them is read exactly once, in whatever it raises to.

    Unlike most of this file's classes, this one does not always end
    the node: `update_chain`'s own reorg reconciliation lets it
    propagate and end the node -- reasonably so, since every raise
    above fires mid-way through applying a fork to this node's own
    already-committed chainstate, and continuing to run this node
    once an index has been found disagreeing with its own data
    mid-mutation is not safe. The p2p filter callbacks (`get_cfilters`,
    `get_cfheaders`, `get_cfcheckpt` in `p2p/callbacks.py`) raise it
    too, over a filter or a filter header missing for a block already
    on the active chain, and carry none of that risk -- nothing
    downstream of answering one peer's BIP157 request is mid-mutation
    of anything. `handle_p2p`'s own generic catch is what lets them
    answer this without ending the node: it stops that one connection
    without discouraging the peer, `isinstance(e, BTClibException)`
    being false for it.
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
    `ChainstateInconsistencyError`'s own call sites, which sometimes
    let it propagate without ending the node, do.

    `db.py`'s own module docstring is where the checksum this class
    detects is argued against Core's `verify_checksums = true`
    (btclib-org/btclib-node#641, closing btclib-org/btclib-node#637).
    Neither of `UtxoIndex`'s two chainstate callers maps this onto
    `ChainstateInconsistencyError`: `add_block` and `get_coin` both
    call `self.db.get` unguarded, so a genuine `StoreCorruptionError`
    there propagates as itself to whatever each caller's own caller
    does with an exception outside its own -- `update_chain`'s trial
    loop for `add_block`, `verify_mempool_acceptance`'s own callers
    for `get_coin`. `ChainstateInconsistencyError`'s own docstring is
    where the distinct, now-resolved question sits: what a
    `Coin.parse` failure means once this guard has already passed a
    record as intact (btclib-org/btclib-node#650).
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


class IncompleteRequestHeadError(BTClibRuntimeError):
    """`parse_request_head` was handed octets with no header terminator yet.

    `IncompleteMessageError`'s own reason applies here unchanged: a
    connection reading its header section a chunk at a time is the
    ordinary case, not a hostile one, so this is `BTClibRuntimeError`
    and not `BTClibValueError` -- more octets can still answer it, where
    the errors below cannot. `RpcConnection.run` never triggers this
    itself, since it only calls `parse_request_head` once `_recv_until`
    has already confirmed the terminator is present; it exists for
    `parse_request_head`'s other caller, `fuzz/fuzz_rpc_head.py`, which
    hands it whatever octets the fuzzer drew.
    """

    def __init__(self) -> None:
        super().__init__("no header terminator yet")


class MalformedRequestHeadError(BTClibValueError):
    """A request's header section names a `Content-Length` this node refuses.

    Not present, defaults to `0` (`RpcConnection.run`'s own prior
    behaviour); present but not an integer, negative, or past
    `rpc.connection.MAX_BODY_BYTES` all raise this instead. `BTClibValueError`
    and not a plain `ValueError`, for `WrongNetworkMagicError`'s own
    reason above: `RpcConnection.run`'s catch is a bare `except
    Exception`, so this class only matters where a caller narrows on
    `BTClibException` the way `fuzz/fuzz_rpc_head.py` and
    `tests/fuzz_corpus_test.py`'s own `_parsed` both do.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"malformed request head: {detail}")
