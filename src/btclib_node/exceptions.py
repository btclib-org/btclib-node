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
    remove that this node's own earlier `add_block` did not just add.
    That last one is the test that actually separates this class from
    `InvalidBlockInputError` below, which shares its two messages
    ("prevout not found", "prevout already spent in this batch") but
    not the invariant: `apply_rev_block` only ever inverts a reverse
    patch this node wrote for a block it already validated and
    connected, so a failure there is this tree's own bookkeeping
    disagreeing with itself. A peer's bad data is refused earlier and
    differently (`BTClibException`, a `None` handled in place, or
    `InvalidBlockInputError`); reaching here is never that.

    `message` and not a structured payload per call site: what is
    inconsistent (a hash, a count, a status) differs by call site, and
    every one of them is read exactly once, in whatever it raises to.
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


class IncompatibleStoreError(RuntimeError):
    """A data directory holds a store this version cannot open.

    Two cases raise this: a LevelDB directory, the format the store
    `db.KeyValueStore` replaced (#107) used and this one cannot read,
    and a `KeyValueStore` written by a version that kept a different
    shape under one of its keys or in a `block_db` flat file --
    `db.py`'s own `_SCHEMA_VERSION` is where that second case is
    checked and argued.
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
