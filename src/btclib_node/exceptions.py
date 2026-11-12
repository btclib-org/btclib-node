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

from btclib_node.chains import Main, RegTest, SigNet, TestNet, TestNet4

__all__ = [
    "BlockScriptVerifyError",
    "ChainstateInconsistencyError",
    "DirectoryLockError",
    "IncompatibleStoreError",
    "IncompleteRequestHeadError",
    "InvalidBlockInputError",
    "InvalidChainTypeError",
    "LowWorkHeaderError",
    "MalformedRequestHeadError",
    "MisbehavingError",
    "MissingPrevoutError",
    "NodeShutdownTimeoutError",
    "NonStandardTxError",
    "OversizedRequestBodyError",
    "PackageRefusedError",
    "PrevoutCountMismatchError",
    "ReimportedMainProcessError",
    "RejectedMessageError",
    "RpcCredentialRefusedError",
    "StoreClosedError",
    "StoreCorruptionError",
    "TxRejectedError",
    "UnknownChainError",
    "UnsupportedAddressTypeError",
    "WrongNetworkMagicError",
]


class BlockScriptVerifyError(BTClibValueError):
    """A candidate block's own script check failed while connecting it.

    Raised only by `interpreter.check_scripts`, wrapping the `ScriptError`
    `Node.worker_pool.starmap` propagates out of `verify_input`
    (`interpreter.f`) once a candidate's structure, its context, its
    amounts and its sigop cost have already passed.
    Composed as Core's own wire format for it,
    `block-script-verify-flag-failed (%s)` (`CheckInputScripts`,
    `src/validation.cpp`, at bitcoin/bitcoin@f0de87f040, the v32.1 tag),
    with `ScriptErrorString`, which btclib's `ScriptError.code` carries
    as `description`.

    `BTClibValueError`, so `main._resolve_trial_exception`'s own
    `_CONTENT_FAILURE` tuple keeps catching it as the fork's content
    being bad rather than this node's own bookkeeping.
    """

    def __init__(self, message: str) -> None:
        super().__init__(f"block-script-verify-flag-failed ({message})")


class MisbehavingError(BTClibValueError):
    """A peer's message of the kind Core answers with `Misbehaving`.

    `p2p.main` discourages the peer for this class alone. Any other
    `BTClibException` out of a callback, a payload btclib cannot parse
    among them, is logged and the peer kept, as Core's
    `PeerManagerImpl::ProcessMessages` catches an exception out of
    `ProcessMessage` and only logs it (`src/net_processing.cpp`, at
    bitcoin/bitcoin@f0de87f040, the v32.1 tag). Raised where Core calls
    `Misbehaving`, directly or through `MaybePunishNodeForBlock`.
    `BTClibValueError`, so that a caller refusing a header or a block
    for any reason still catches it as one.
    """


class LowWorkHeaderError(BTClibValueError):
    """A new header refused for want of the anti-DoS proof of work.

    Core's `BLOCK_HEADER_LOW_WORK`, "too-little-chainwork", which
    `AcceptBlockHeader` answers for a header whose caller has not checked
    that its chain clears `GetAntiDoSWorkThreshold`, and which
    `MaybePunishNodeForBlock` does not punish (`src/validation.cpp` and
    `src/net_processing.cpp`, at bitcoin/bitcoin@f0de87f040, the v32.1
    tag). Not a `MisbehavingError` for that reason.
    """

    def __init__(self) -> None:
        """Carry Core's own reason."""
        super().__init__("too-little-chainwork")


class MissingPrevoutError(ValueError):
    """A transaction's input spends an output this node cannot find.

    Raised only by `pre_verify_mempool_acceptance` (`main.py`), while it
    walks a candidate mempool transaction's own inputs against the UTXO
    set and the mempool together and neither has the prevout --
    `InvalidBlockInputError` below is the same check, made instead while
    a freshly-downloaded candidate block is first connected.
    """


# The reasons whose Core result is `TX_RECONSIDERABLE`, which `PreChecks`,
# `CheckFeeRate`, `ReplacementChecks` and the trim after an accepted
# transaction give (`src/validation.cpp`, at bitcoin/bitcoin@f0de87f040, the
# v32.1 tag): a refusal that a package with a child paying for it can undo.
_RECONSIDERABLE_REASONS = frozenset(
    {
        "mempool min fee not met",
        "min relay fee not met",
        "insufficient fee",
        "insufficient fee (including sibling eviction)",
        "replacement-failed",
        "mempool full",
    }
)


class TxRejectedError(BTClibValueError):
    """A mempool candidate refused with one of Core's own reject reasons.

    `reason` is `ValidationState::GetRejectReason` and `details` its debug
    message; `str()` is `ToString`, the two joined by ", ", or the reason
    alone where there is no debug message (`src/consensus/validation.h`, at
    bitcoin/bitcoin@f0de87f040, the v32.1 tag). `rpc.callbacks` answers
    `testmempoolaccept`'s `reject-reason` and `reject-details` with them,
    and `sendrawtransaction`'s `-26` with `str()`. `BTClibValueError`, so
    `p2p.callbacks.tx` hands it to `DownloadManager.mempool_rejected_tx`,
    which says what it records until the next block. A fee floor's refusal
    is `reconsiderable`: Core keeps it apart in
    `RecentRejectsReconsiderableFilter` and looks for a child that pays for
    it, as `DownloadManager.mempool_rejected_tx` does. `details` defaults to
    empty for a reason Core's own checks never attach a debug message to
    (`non-final`, `txn-already-in-mempool`), matching `ToString`'s own
    fallback rather than printing a trailing ", ".

    `witness_stripped` is Core's `TX_WITNESS_STRIPPED`, which
    `interpreter.check_transaction` sets. A transaction with its witness
    stripped has the txid of the one with the witness, so
    `DownloadManager.mempool_rejected_tx` records the refusal nowhere.
    """

    def __init__(
        self, reason: str, details: str = "", *, witness_stripped: bool = False
    ) -> None:
        super().__init__(f"{reason}, {details}" if details else reason)
        self.reason = reason
        self.details = details
        self.witness_stripped = witness_stripped

    @property
    def reconsiderable(self) -> bool:
        """Whether Core's result for this refusal is `TX_RECONSIDERABLE`."""
        return self.reason in _RECONSIDERABLE_REASONS


class PackageRefusedError(BTClibValueError):
    """A package of transactions refused, and what each of them answers.

    Raised by `main.pre_verify_package`. `errors` maps the wtxid of each
    transaction that has a verdict to it, which `p2p.callbacks` treats as
    Core's `ProcessPackageResult` treats the result of that transaction.
    A refusal of the package as a whole, which names no transaction,
    has none.

    `package_level` is whether the refusal is one only the package reveals
    (Core's `PackageTRUCChecks` and `PackageRBFChecks`, and its cluster
    limit), which names the package and no transaction in its answer, and
    is not that transaction's own.
    """

    def __init__(
        self, errors: dict[bytes, Exception], *, package_level: bool = False
    ) -> None:
        super().__init__("package refused")
        self.errors = errors
        self.package_level = package_level


class NonStandardTxError(TxRejectedError):
    """A mempool candidate the relay rules refuse and the consensus rules take.

    Raised only by `interpreter.check_transaction`, for a candidate that
    fails `interpreter.STANDARD_FLAGS` and passes
    `btclib.script.engine.flags.ALL_FLAGS`: the scripts are ones a block
    may carry, so the refusal is this node's own relay policy rather
    than anything the peer that relayed it did wrong. Core says so where
    it defines the same set -- "we do not ban/disconnect nodes that
    forward txs violating the additional (non-mandatory) rules here, to
    improve forwards and backwards compatibility"
    (`src/policy/policy.h:112-117`, at bitcoin/bitcoin@f0de87f040, the
    v32.1 tag) -- and `p2p.callbacks.tx` is what honours it, catching
    this and dropping the transaction alone.

    A `TxRejectedError`, so both RPC paths answer it in Core's words,
    as they answer any other refusal. Not a `MisbehavingError`, so
    `p2p.main.handle_p2p` would not discourage the peer for it either,
    and `tx`'s catch hands it to `DownloadManager.mempool_rejected_tx`,
    which says what it records.
    """


class ChainstateInconsistencyError(RuntimeError):
    """The node's own index promised something its data does not have.

    Raised only where an earlier check already established the invariant
    this violates -- a block marked downloaded that `block_db` does not
    hold, or holds as a record `stored_block.read_held_block` refuses, a
    reverse patch `set_status` already trusts that is not on disk, a
    UTXO `apply_rev_block` is asked to remove that this node's own
    earlier `add_block` did not just add. `apply_rev_block` only ever
    inverts a reverse patch this node wrote for a block it already
    validated and connected, so a failure there is this tree's own
    bookkeeping disagreeing with itself, the same test that separates
    this class from `InvalidBlockInputError` below, which shares two of
    its messages ("prevout not found", "prevout already spent in this
    batch") but not the invariant. A peer's bad data, or a submitted
    transaction's own content, is refused earlier and differently
    (`BTClibException`, a `None` handled in place,
    `InvalidBlockInputError`, or `MissingPrevoutError`); reaching here
    is never that.

    A stored `utxo-` record `Coin.parse` refuses is raised as this
    class too, by `UtxoIndex._parse_stored`, whichever `UtxoIndex` read
    reaches it. `UtxoIndex._stored_prevout`'s docstring says why it
    stops the node, as Core's `CCoinsViewDB::GetCoin` does
    (`src/txdb.cpp:88-108`, at bitcoin/bitcoin@f0de87f040, the v32.1
    tag), rather than answer the coin absent.

    `message` and not a structured payload per call site: what is
    inconsistent (a hash, a count, a status) differs by call site, and
    every one of them is read exactly once, in whatever it raises to.

    Unlike most of this file's classes, this one does not always end the
    node: `update_chain`'s own reorg reconciliation lets it propagate
    and end the node -- reasonably so, since every raise above fires
    mid-way through applying a fork to this node's own already-committed
    chainstate, and continuing to run this node once an index has been
    found disagreeing with its own data mid-mutation is not safe. The
    p2p filter callbacks (`get_cfilters`, `get_cfheaders`,
    `get_cfcheckpt` in `p2p/callbacks.py`) raise it too, over a filter
    or a filter header missing for a block already on the active chain,
    and carry none of that risk -- nothing downstream of answering one
    peer's BIP157 request is mid-mutation of anything. `handle_p2p`'s
    own generic catch is what lets them answer this without ending the
    node: it logs it and keeps the peer, this not being a
    `MisbehavingError`.

    `getblocktxn` raises it for a record `read_held_block` refuses, and
    sets `terminate_flag` first, as Core asserts that read.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class InvalidBlockInputError(ValueError):
    """A candidate block's own transactions fail a UTXO consistency check.

    Raised only by `UtxoIndex.add_block`, while it is walking a
    freshly-downloaded candidate block's own transactions against the
    UTXO set for the first time -- before `check_scripts` (script
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
    """`Node.stop()` waited `STOP_TIMEOUT`, and no store had begun to close.

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
    `UtxoIndex` does not map this onto `ChainstateInconsistencyError`: a
    read by outpoint that raises it stops the node and raises it again,
    as `UtxoIndex._stored_prevout`'s docstring says. A record this guard
    passes as intact and `Coin.parse` still refuses raises
    `ChainstateInconsistencyError`.
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


class DirectoryLockError(RuntimeError):
    """A directory `Node` keeps its data in cannot be locked for it.

    Raised by `dirlock.DirectoryLock`, with Core's own message for each
    of its two refusals, and printed by `cli.main` the way Core's
    `InitError` is.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class RpcCredentialRefusedError(ValueError):
    """`InitRPCAuthentication` refuses a `-rpcauth` or `-rpccookieperms` value.

    Raised by `rpc.auth.RpcAuth.start` once it has logged Core's line for
    the value, and read by `rpc.manager.RpcManager` as a listener that
    did not come up, which `Node` answers with Core's "Unable to start
    HTTP server. See debug log for details."
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
    """`check_scripts` was handed a prevout list the wrong length."""

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

    `BTClibValueError` and not a plain `ValueError`: it is the peer's
    envelope being wrong, as `Message.parse`'s own refusals are, and
    `fuzz/fuzz_framing.py` tells a refusal from a finding by
    `isinstance(e, BTClibException)`.
    """

    def __init__(self, magic: bytes) -> None:
        # section 7 of the organization's standard, *A refusal carries no
        # secret*: a refused magic is by definition not this node's, so it
        # may hold anything, key bytes included, and is printed only where
        # it is one of the magics `btclib_node.chains` defines
        known = {cls().magic for cls in (Main, TestNet, TestNet4, SigNet, RegTest)}
        suffix = f": {magic.hex()}" if magic in known else ""
        super().__init__(f"message for another network{suffix}")


class RejectedMessageError(BTClibValueError):
    """A whole message was framed and is dropped, its sender kept.

    Core's `reject_message` (`V1Transport::GetReceivedMessage`,
    `src/net.cpp`, at bitcoin/bitcoin@f0de87f040, the v32.1 tag): a
    checksum that does not match the payload, or a command
    `IsMessageTypeValid` refuses. `size` is the message's own octets,
    header included, which the stream has been moved past.
    """

    def __init__(self, size: int) -> None:
        super().__init__(f"message of {size} bytes rejected")
        self.size = size


class IncompleteRequestHeadError(BTClibRuntimeError):
    """`parse_request_head` was handed octets holding no whole header section.

    `IncompleteMessageError`'s own reason applies here unchanged: a
    connection reading its header section a chunk at a time is the
    ordinary case, not a hostile one, so this is `BTClibRuntimeError`
    and not `BTClibValueError` -- more octets can still answer it, where
    the errors below cannot. `RpcConnection.run` never triggers this
    itself, since it reads more of the section instead of calling
    `parse_request_head`; it exists for `parse_request_head`'s caller,
    `fuzz/fuzz_rpc_head.py`, which hands it whatever octets the fuzzer
    drew.
    """

    def __init__(self) -> None:
        super().__init__("no whole header section yet")


class MalformedRequestHeadError(BTClibValueError):
    """A request Core's HTTP server cannot read: it answers 400 Bad Request.

    A request line, a header field or a `Content-Length` that
    `rpc.connection.parse_request_head` refuses, and a chunked body
    `rpc.connection` cannot read. `BTClibValueError` and not a plain
    `ValueError`, for `WrongNetworkMagicError`'s own reason above:
    `fuzz/fuzz_rpc_head.py` and `tests/fuzz_corpus_test.py`'s own
    `_parsed` narrow on `BTClibException`.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"malformed request head: {detail}")


class OversizedRequestBodyError(BTClibValueError):
    """A request's body is past the limit Core's HTTP server answers 413 past.

    A `Content-Length`, or a chunked body's sizes summed, past
    `rpc.connection.MAX_BODY_BYTES`. A class of its own rather than a
    `MalformedRequestHeadError`, since the status it is answered with is
    another.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"request body refused: {detail}")
