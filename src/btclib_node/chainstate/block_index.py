# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`BlockIndex`, every header this node has seen and which chain is active.

`BlockStatus` tracks a header from `valid_header` up through however far
its block has been validated; `get_download_candidates` and
`MAX_DOWNLOAD_WINDOW` are what bound how far ahead of the active chain a
download is allowed to run, read from both `download.py` and here.
`invalidate` is what a failed contextual check calls, through
`main.update_header_index`, to drop a header and everything built on it.
`stage_status` and `finalize` are `set_status` split into its two halves
-- the in-memory move and the disk write -- so that `main._finalize_fork`
can hold the second half back across more than one block; `db.py`'s own
docstring is where that staging, shared with `UtxoIndex`, is argued.

`set_downloaded` writes straight through unconditionally, and correctly
so: its one caller, `p2p.callbacks.block`, only ever runs it on a hash
not yet downloaded, and every hash `stage_status` puts in `pending` came
from `_finalize_fork`'s own to_add/to_remove loop, which only ever
offers `main.update_chain` a hash already downloaded -- `_ready_fork`
requires it. So a hash `set_downloaded` targets is never one `pending`
holds.

`set_status` cannot make that same assumption, because `invalidate`'s
own caller can name a hash that already connected once. `update_chain`
sets `failed_hash` to a block across `utxo_index.add_block`,
`_validate_block`, `block_db.add_rev_block` and
`filter_index.add_connected_block` alike, so a fault in either of the
last two -- an I/O failure, nothing to do with the block's own content
-- invalidates a block exactly as an actual validation failure would.
Reached during a chain-tip flip-flop -- a hash `stage_status` staged,
disconnected by a later trial that re-stages it there, then offered
again -- that is a hash `pending` still holds, unflushed. `set_status`
therefore checks `pending` itself: a hash already staged there is
updated in `pending`, exactly as `stage_status` would leave it, rather
than written straight through, so the next `finalize` writes the
invalidation instead of clobbering it with the stale entry write-through
would otherwise race against. btclib-org/btclib-node#586.
"""

import enum
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from btclib import var_int
from btclib.block import BlockHeader
from btclib.block.proof_of_work import block_work
from btclib.exceptions import BTClibValueError
from btclib.utils import bytesio_from_binarydata

from btclib_node.chainstate.contextual import assert_valid_in_context
from btclib_node.exceptions import ChainstateInconsistencyError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from btclib_node.chains import Chain
    from btclib_node.db import KeyValueStore
    from btclib_node.log import Logger

__all__ = [
    "MAX_DOWNLOAD_WINDOW",
    "BlockIndex",
    "BlockInfo",
    "BlockStatus",
    "calculate_work",
]

# `get_download_candidates`'s own cap on how many hashes it hands back
# at once, and `download.py`'s `block_download` reads the same number
# to decide whether the download window has run too far ahead of the
# active chain to keep extending it -- one bound on how large that
# window is ever allowed to get, read from both ends of it.
MAX_DOWNLOAD_WINDOW = 1024


def calculate_work(header: BlockHeader) -> int:
    """Return the work `header`'s own target represents."""
    return block_work(header.bits)


class BlockStatus(enum.IntEnum):
    """Where a block stands relative to the active chain.

    `valid_header` is a header on its own, content not yet checked;
    `in_active_chain` is on the active chain now; `valid` is a block
    whose content passed validation but that a reorg has since removed
    from the active chain (`_finalize_fork`'s own `to_remove` loop is
    the only place that sets it). `invalid` is terminal, set on a block
    itself or on any block built on one already marked `invalid`.
    """

    valid_header = 1
    invalid = 2
    valid = 3
    in_active_chain = 4


# Frozen, so that the index can hand out what it stores: what a caller
# reads is the index's own object and there is nothing it can do to it.
# `header` is btclib's own dataclass and not frozen, so the header
# inside the record is the one thing a caller still holds a handle on.
#
# `chainwork` is not a field here: it is derived from the headers this
# index holds, not stored with any of them, and `BlockIndex.chainwork`
# is where it lives -- a `dict[bytes, int]` written into directly,
# beside `header_dict` rather than inside its records.
# btclib-org/btclib-node#201
@dataclass(frozen=True)
class BlockInfo:
    """One header this index has indexed: its height, status and download state.

    `header` is the parsed header; `index` is its height; `status` and
    `downloaded` are this index's own bookkeeping about it. `chainwork`
    is deliberately not a field here -- the comment above argues why.
    """

    header: BlockHeader
    index: int
    status: BlockStatus = BlockStatus.valid_header
    downloaded: bool = False

    @classmethod
    def deserialize(cls, data: bytes, *, check_validity: bool = True) -> BlockInfo:
        """Parse a `BlockInfo` from the bytes `serialize` produced."""
        stream = bytesio_from_binarydata(data)
        header = BlockHeader.parse(stream, check_validity=check_validity)
        index = var_int.parse(stream)
        status = BlockStatus.from_bytes(stream.read(1), "little")
        downloaded = bool(int.from_bytes(stream.read(1), "little"))
        return cls(header, index, status, downloaded)

    def serialize(self) -> bytes:
        """Serialize this record to the bytes stored under `blkinfo-<hash>`."""
        out = self.header.serialize()
        out += var_int.serialize(self.index)
        out += self.status.to_bytes(1, "little")
        out += int(self.downloaded).to_bytes(1, "little")
        return out


class BlockIndex:
    """Every header this node has seen, and which chain among them is active.

    `header_dict` maps a hash to its `BlockInfo`; `chainwork` holds each
    one's cumulative work, kept apart from the record itself since it is
    derived rather than stored (issue #201). `active_chain` is the
    current best chain by hash; `block_candidates` is every other header
    that might still beat it once downloaded; `header_index` is the
    best known header chain, tracked separately since a header being
    known does not make its own block downloaded, let alone valid.
    `header_index_pos` is `header_index`'s own hash -> position, kept
    beside it the same way `chainwork` is kept beside `header_dict`
    (issue #439).
    """

    def __init__(self, parent_db: KeyValueStore, chain: Chain, logger: Logger) -> None:
        """Seed the index with `chain`'s own genesis, then load the store."""
        self.logger = logger

        self.db = parent_db

        # the network, for what `add_headers` requires of a header
        # besides the eighty bytes: its easiest target, and the two
        # consensus parameters that decide how the target moves
        self.chain = chain

        genesis = chain.genesis
        genesis_info = BlockInfo(
            genesis, 0, BlockStatus.in_active_chain, downloaded=True
        )

        self.header_dict: dict[bytes, BlockInfo] = {genesis.hash: genesis_info}

        # each header's cumulative work, keyed by hash rather than kept
        # on its BlockInfo: calculate_chainwork below writes into this
        # directly, so a start-up rebuild touches one int per header and
        # not a whole new frozen record. btclib-org/btclib-node#201
        self.chainwork: dict[bytes, int] = {}

        # the actual block chain; it contains only valid blocks
        self.active_chain: list[bytes] = []

        # blocks that are waiting to be connected to the active chain,
        # each a [hash, chainwork] pair
        self.block_candidates: deque[list[Any]] = deque()

        # list all header hashes, even if not already checked, needed for
        # the block locators
        self.header_index: list[bytes] = []

        # header_index's own hash -> position, kept beside it rather
        # than computed from it: get_headers_from_locators resolves a
        # peer's locator against this index once per message, and
        # header_index holds one entry per header this node has ever
        # indexed -- the whole known chain -- so a membership test or a
        # position lookup done against the list itself is an O(n) scan
        # repeated for every entry of the locator.
        # btclib-org/btclib-node#439, following chainwork (#201) and
        # children (#125) in keeping a derived index beside the primary
        # structure rather than recomputing it on every read. Maintained
        # incrementally at the same three sites that mutate header_index
        # -- generate_header_index, _extend_header_index and
        # _insert_pending_headers -- rather than rebuilt whole after each:
        # the append case those sites share is the ordinary one, one new
        # block at a time, and rebuilding a dict the size of the whole
        # chain for that would trade the scan this fixes for a
        # dict-construction of the same size on every block.
        self.header_index_pos: dict[bytes, int] = {}

        # the reverse of previous_block_hash, kept so invalidate can walk
        # forward from a bad block to what is really built on it instead
        # of scanning header_dict whole: btclib-org/btclib-node#125
        self.children: dict[bytes, list[bytes]] = {}

        # a status `stage_status` has set in header_dict but not yet
        # written to the store, the way FilterIndex.pending holds a
        # filter (filter_index.py's own module docstring). Only
        # `_finalize_fork`'s own to_add/to_remove loop stages here --
        # every other caller of set_status writes straight through --
        # so what accumulates is exactly the status change a block's
        # own connection or disconnection made, for `finalize` below to
        # write together with UtxoIndex's own flush. btclib-org/btclib-node#586
        self.pending: dict[bytes, BlockInfo] = {}

        self.init_from_db()

    def init_from_db(self) -> None:
        """Load every stored header into `header_dict`, then derive the rest.

        Stops at the first key that is not a `blkinfo-` record: the
        shared store's own key order (`db.py`'s docstring) sorts this
        index's own keys ahead of the filter and UTXO indexes sharing
        the same store.
        """
        self.logger.info("Start Index initialization")
        for key, value in self.db:
            prefix, block_hash = key[:8], key[8:]
            if prefix != b"blkinfo-":  # utxo_index
                break
            self.header_dict[block_hash] = BlockInfo.deserialize(
                value, check_validity=False
            )

        self.sorted_header_dict: list[bytes] = sorted(
            self.header_dict, key=lambda x: self.header_dict[x].index
        )

        self.logger.info("Start calculate_chainwork")
        self.calculate_chainwork()
        self.logger.info("Start generate_active_chain")
        self.generate_active_chain()
        self.logger.info("Start generate_block_candidates")
        self.generate_block_candidates()
        self.logger.info("Start generate_header_index")
        self.generate_header_index()
        self.logger.info("Finished Index initialization")

        self.sorted_header_dict = []

    def calculate_chainwork(self) -> None:
        """Compute every header's cumulative work into `chainwork`.

        Backfills `children` along the way, one entry per header visited.
        """
        for block_hash in self.sorted_header_dict:
            block_info = self.get_block_info(block_hash)
            if block_info.index == 0:  # genesis
                old_work = 0
            else:
                previousblockhash = block_info.header.previous_block_hash
                old_work = self.chainwork[previousblockhash]
                self.children.setdefault(previousblockhash, []).append(block_hash)
            # written into self.chainwork directly, not through
            # BlockInfo/_insert_block_info: chainwork is not part of
            # the stored record, so this loop touches one int per
            # header rather than replacing the record itself
            self.chainwork[block_hash] = old_work + calculate_work(block_info.header)

    def generate_active_chain(self) -> None:
        """Rebuild `active_chain` from every header marked `in_active_chain`."""
        chain_dict: dict[int, bytes] = {}
        for block_hash, block_info in self.header_dict.items():
            if block_info.status == BlockStatus.in_active_chain:
                chain_dict[block_info.index] = block_hash
        for index in sorted(chain_dict.keys()):
            self.active_chain.append(chain_dict[index])

    def generate_block_candidates(self) -> None:
        """Rebuild `block_candidates` from every `valid_header` past the tip."""
        active_chain_set = set(self.active_chain)
        current_work = self.chainwork[self.active_chain[-1]]
        for block_hash in self.sorted_header_dict:
            if block_hash in active_chain_set:
                continue
            block_info = self.get_block_info(block_hash)
            if block_info.status != BlockStatus.valid_header:
                continue
            work = self.chainwork[block_hash]
            if work > current_work:
                self.block_candidates.append([block_hash, work])

    def generate_header_index(self) -> None:
        """Rebuild `header_index`, seeded from `active_chain` then extended."""
        self.header_index = self.active_chain[:]
        self.header_index_pos = {h: i for i, h in enumerate(self.header_index)}
        self._extend_header_index(self.sorted_header_dict)

    # extends self.header_index, already seeded by the caller, with
    # whichever of `candidates` continues its current tip or beats it on
    # work -- skipping one already there and, since an invalidated chain
    # is never the header chain this index reports as its best known
    # one, one marked BlockStatus.invalid too. `candidates` has to be in
    # height order for the incremental fork comparison below to see a
    # header's own parent before the header itself.
    # btclib-org/btclib-node#218
    def _extend_header_index(self, candidates: Iterable[bytes]) -> None:
        # tracks the same membership header_index_pos's own keys do,
        # kept separate rather than reusing it here: the two move
        # together at every append and every fork rewrite below, and a
        # future mutation site that updates one without the other is
        # what to guard against, not a rewrite of either alone.
        header_index_set = set(self.header_index)
        for block_hash in candidates:
            if block_hash in header_index_set:
                continue
            block_info = self.get_block_info(block_hash)
            if block_info.status == BlockStatus.invalid:
                continue
            header = block_info.header
            best_header = self.header_index[-1]
            if header.previous_block_hash == best_header:
                self.header_index.append(block_hash)
                self.header_index_pos[block_hash] = len(self.header_index) - 1
                header_index_set.add(block_hash)
            elif self.chainwork[block_hash] > self.chainwork[best_header]:
                add, remove = self.get_fork_details(block_hash, self.header_index)
                for removed_hash in remove:
                    del self.header_index_pos[removed_hash]
                self.header_index = self.header_index[: -len(remove)]
                base = len(self.header_index)
                self.header_index.extend(add)
                for offset, added_hash in enumerate(add):
                    self.header_index_pos[added_hash] = base + offset
                header_index_set = set(self.header_index)

    # header_dict (and, the one time a hash is new, children) moving is
    # not conditioned on anything below: a status staged rather than
    # written straight through still has to be the one get_first_candidate,
    # active_chain and every other in-memory reader see for the rest of
    # this process's own life, `stage_status` below staging only the disk
    # write and never this.
    def _record_block_info(self, block_info: BlockInfo) -> None:
        block_hash = block_info.header.hash
        # a genuinely new hash, and not set_status/set_downloaded
        # overwriting the record already there for it: children is the
        # index invalidate walks, and a hash already present had its
        # parentage recorded the one time it was new
        if block_hash not in self.header_dict:
            self.children.setdefault(block_info.header.previous_block_hash, []).append(
                block_hash
            )
        self.header_dict[block_hash] = block_info

    # `wb` is a write batch: given one, the database moves when that
    # batch commits, where `header_dict` moves now either way
    def _insert_block_info(
        self, block_info: BlockInfo, wb: KeyValueStore | None = None
    ) -> None:
        self._record_block_info(block_info)
        db = wb or self.db
        db.put(b"blkinfo-" + block_info.header.hash, block_info.serialize())

    # what stage_status and set_status's own pending branch below both
    # reduce to: record the change in memory now, and leave its write
    # for finalize to make later
    def _stage(self, block_info: BlockInfo) -> None:
        self._record_block_info(block_info)
        self.pending[block_info.header.hash] = block_info

    # the fields a caller changes, read here rather than by the caller,
    # so that what goes back is the record the index holds now
    def set_status(
        self, block_hash: bytes, status: BlockStatus, wb: KeyValueStore | None = None
    ) -> None:
        """Set `block_hash`'s own status, replacing its stored `BlockInfo`.

        Writes straight through to the store (or to a caller's own
        `wb`) unless `pending` already holds this hash -- in which case
        the change is folded into that pending entry instead, exactly
        as `stage_status` below would leave it, and `wb` goes unused:
        the write is deferred to `finalize`'s own flush rather than
        happening now at all, since a write-through here would only be
        undone the next time `finalize` writes that pending entry's
        stale value over it. The module docstring argues why this
        happens -- `invalidate`'s own caller, `update_chain`.
        """
        block_info = replace(self.get_block_info(block_hash), status=status)
        if block_info.header.hash in self.pending:
            self._stage(block_info)
            return
        self._insert_block_info(block_info, wb)

    def stage_status(self, block_hash: bytes, status: BlockStatus) -> None:
        """Set `block_hash`'s status now, its write staged for `finalize`.

        `set_status` above writes through to the store (or to a caller's
        own `wb`) the moment it is called, unless `pending` already
        holds the hash; this stages the write into `pending`
        unconditionally, for `finalize` to write out whenever it next
        runs -- `_finalize_fork`'s own to_add/to_remove loop is the one
        caller, once per block a fork connects or disconnects, so that a
        block's own status reaches disk only together with the UTXO
        cache's flush rather than one write_batch per block. A later
        call for the same hash before that flush -- a reorg undoing a
        connection this process staged and never wrote -- simply
        replaces the pending entry, which is correct: only the state
        `finalize` is about to write ever needs to reach disk at all.
        """
        self._stage(replace(self.get_block_info(block_hash), status=status))

    def finalize(self, wb: KeyValueStore | None = None) -> None:
        """Write every status `stage_status` staged, into `wb` if there is one.

        Mirrors `FilterIndex.finalize`: a write_batch of its own when no
        `wb` is given, one write inside a caller's own batch otherwise.
        """
        if wb is not None:
            self._write(wb)
            return
        with self.db.write_batch() as batch:
            self._write(batch)

    def _write(self, db: KeyValueStore) -> None:
        for block_hash, block_info in self.pending.items():
            db.put(b"blkinfo-" + block_hash, block_info.serialize())
        self.pending = {}

    def set_downloaded(self, block_hash: bytes, *, downloaded: bool = True) -> None:
        """Set `block_hash`'s `downloaded` flag, replacing its `BlockInfo`."""
        block_info = self.get_block_info(block_hash)
        self._insert_block_info(replace(block_info, downloaded=downloaded))

    def get_block_info(self, block_hash: bytes) -> BlockInfo:
        """Return the `BlockInfo` stored for `block_hash`."""
        return self.header_dict[block_hash]

    # what a block failing validation costs: itself, and every header
    # this index has ever indexed on top of it, candidate or not.
    # Finding them costs the size of the bad lineage and not the size of
    # the index: `children` is walked rather than `header_dict`, and
    # `add_headers` refuses to build a valid_header on an invalid
    # parent, which is what keeps a header arriving *after* this call
    # from needing to be walked here. No hash is ever pushed twice:
    # `_insert_block_info` records a hash as a child the one time it is
    # new, so it is a value of `children` under exactly one parent, and
    # the walk below cannot reach it a second time. Sweeping them out of
    # `block_candidates` is not bounded the same way: the scan below is
    # over the whole deque, not the bad lineage.
    # btclib-org/btclib-node#77, #120, #125
    def invalidate(self, block_hash: bytes) -> None:
        """Mark `block_hash` invalid, and everything indexed on top of it.

        Walks `children` rather than `header_dict`, so the cost is the
        size of the bad lineage rather than of the whole index. Every
        invalidated hash is dropped from `block_candidates`; `header_index`
        is rebuilt from `active_chain` only if it held one of them.
        """
        to_invalidate = [block_hash]
        invalidated: set[bytes] = set()
        while to_invalidate:
            current = to_invalidate.pop()
            invalidated.add(current)
            self.set_status(current, BlockStatus.invalid)
            to_invalidate.extend(self.children.get(current, ()))
        self.block_candidates = deque(
            [h, w] for h, w in self.block_candidates if h not in invalidated
        )
        # header_index is the best known header chain, tracked
        # independently of block_candidates -- Core's own InvalidateBlock
        # (src/validation.cpp) recomputes m_best_header the same way, for
        # the same reason: what this index reports as its best known
        # header chain cannot still be one it has just proved bad. Left
        # alone in the ordinary case, invalidating a losing candidate
        # branch that header_index never held, since a rescan of the
        # whole index costs the size of the index and not the bad
        # lineage. btclib-org/btclib-node#218
        if invalidated.intersection(self.header_index):
            self.header_index = self.active_chain[:]
            self.header_index_pos = {h: i for i, h in enumerate(self.header_index)}
            self._extend_header_index(
                sorted(self.header_dict, key=lambda h: self.header_dict[h].index)
            )

    # returns the active chain and the forked chain from the common ancestor
    def get_fork_details(
        self, header_hash: bytes, chain: list[bytes] | None = None
    ) -> tuple[list[bytes], list[bytes]]:
        """Split `chain` at its common ancestor with `header_hash`.

        `chain` defaults to `active_chain`. Returns the branch from that
        ancestor up to `header_hash` (ancestor excluded, oldest first)
        and the tail of `chain` that branch would replace.
        """
        if not chain:
            chain = self.active_chain
        fork: list[bytes] = [header_hash]
        while True:
            block_info = self.get_block_info(header_hash)
            header_hash = block_info.header.previous_block_hash
            if (
                block_info.index <= len(chain)
                and header_hash == chain[block_info.index - 1]
            ):
                # the common ancestor is at block_info.index - 1, so
                # what the fork replaces is the chain from its own
                # index on. Returned here rather than after the loop:
                # the break carried that index out in a name only this
                # one path ever binds, and the read of it was three
                # lines from anything saying so.
                return fork[::-1], chain[block_info.index :]
            fork.append(header_hash)

    # unsafe: doesn't perform any check
    def add_to_active_chain(self, block_hash: bytes) -> None:
        """Append `block_hash` to `active_chain`, with no check it connects."""
        self.active_chain.append(block_hash)

    def remove_from_active_chain(self, block_hash: bytes) -> None:
        """Pop `active_chain`'s tip if it is `block_hash`, else raise.

        `ChainstateInconsistencyError`, since a caller removing anything
        else is reorganizing the chain out of order.
        """
        if block_hash != self.active_chain[-1]:
            err_msg = "block_hash is not the active chain's tip"
            raise ChainstateInconsistencyError(err_msg)
        self.active_chain.pop()

    # add_headers' own validation stage: every header in the batch,
    # checked and weighed against either the index already on disk or a
    # parent earlier in this same batch, without indexing any of them
    # yet. `pending` is what a header brought by this batch is weighed
    # against, its parent being as likely to be a header two lines
    # above as one already indexed. A header whose parent is in
    # neither is left out of it: there is no chain to weigh it
    # against, and nothing to give it a height -- unless that parent
    # is itself later in this same batch, in which case there *is* a
    # chain to weigh it against, just not yet processed, and this is
    # not the peer's ordinary "connects to nothing I know" case:
    # refusing the whole batch rather than dropping the one header
    # silently is what Core's own per-message continuity check
    # (`CheckHeadersAreContinuous`, `net_processing.cpp`) enforces
    # unconditionally, whether or not the batch would otherwise
    # connect to known history. btclib-org/btclib-node#214
    #
    # A refusal raises rather than answers False: it is a peer that
    # sent a header failing on its own terms, not the ordinary end
    # of a sync, and the caller needs to be able to tell the two
    # apart. btclib-org/btclib-node#75
    def _validate_header_batch(
        self, headers: list[BlockHeader]
    ) -> dict[bytes, tuple[BlockHeader, int]]:
        now = datetime.now(UTC)
        pow_limit_bits = self.chain.pow_limit_bits
        pending: dict[bytes, tuple[BlockHeader, int]] = {}
        # every header's hash, shrunk as each is visited: what is still
        # in here when a header is looked at is strictly later in the
        # batch, not merely unresolved -- a header already visited and
        # left unresolved (a batch that connects to nothing at all) is
        # not in here either, so it does not trip the check below.
        not_yet_visited = {header.hash for header in headers}

        def parent_of(header: BlockHeader) -> BlockHeader:
            previous = header.previous_block_hash
            if previous in pending:
                return pending[previous][0]
            return self.header_dict[previous].header

        for header in headers:
            header_hash = header.hash
            not_yet_visited.discard(header_hash)
            try:
                header.assert_valid_pow(pow_limit_bits)
                if header_hash in self.header_dict or header_hash in pending:
                    continue
                found = pending.get(header.previous_block_hash)
                if found is None:
                    block_info = self.header_dict.get(header.previous_block_hash)
                    if block_info is None:
                        if header.previous_block_hash in not_yet_visited:
                            # kept inside the try, against TRY301: the
                            # except right below logs every refusal this
                            # loop finds the same way, whether it is
                            # this raise or assert_valid_in_context's
                            # own, and abstracting this one out would
                            # split that one log line into two shapes
                            # for no reader's benefit.
                            err_msg = "a header's parent is later in the same batch"
                            raise BTClibValueError(err_msg)  # noqa: TRY301
                        continue
                    found = (block_info.header, block_info.index)
                parent, parent_height = found
                assert_valid_in_context(
                    self.chain, header, parent, parent_height, parent_of, now
                )
            except BTClibValueError as e:
                self.logger.warning("Refused a header batch: %s", e)
                raise
            pending[header_hash] = (header, parent_height + 1)

        return pending

    # add_headers' own indexing stage, once every header in the batch has
    # passed `_validate_header_batch` above: nothing here is checked any
    # more, only written -- to `header_dict`, `chainwork`,
    # `block_candidates`, and `header_index` where the batch's own work
    # actually beats what each already holds.
    def _insert_pending_headers(
        self, pending: dict[bytes, tuple[BlockHeader, int]]
    ) -> None:
        current_work = self.chainwork[self.active_chain[-1]]
        for header_hash, (header, height) in pending.items():
            previous_block_info = self.get_block_info(header.previous_block_hash)
            new_work = self.chainwork[header.previous_block_hash] + calculate_work(
                header
            )
            # a header built on an invalid one is invalid itself, without
            # a walk: previous_block_hash is already indexed by the time
            # this runs, so the parent's status is already settled.
            # btclib-org/btclib-node#120
            invalid = previous_block_info.status == BlockStatus.invalid
            status = BlockStatus.invalid if invalid else BlockStatus.valid_header
            block_info = BlockInfo(
                header,
                height,
                status,
                downloaded=False,
            )
            self._insert_block_info(block_info)
            self.chainwork[header_hash] = new_work

            if not invalid and new_work > current_work:
                self.block_candidates.append([header_hash, new_work])

            # a peer sending more of an already-invalidated fork must not
            # grow header_index onto it, work alone deciding nothing here
            # any more than it did for block_candidates above.
            # btclib-org/btclib-node#218
            if not invalid:
                best_header = self.header_index[-1]
                if header.previous_block_hash == best_header:
                    self.header_index.append(header_hash)
                    self.header_index_pos[header_hash] = len(self.header_index) - 1
                elif new_work > self.chainwork[best_header]:
                    add, remove = self.get_fork_details(header_hash, self.header_index)
                    for removed_hash in remove:
                        del self.header_index_pos[removed_hash]
                    self.header_index = self.header_index[: -len(remove)]
                    base = len(self.header_index)
                    self.header_index.extend(add)
                    for offset, added_hash in enumerate(add):
                        self.header_index_pos[added_hash] = base + offset

    def add_headers(self, headers: Iterable[BlockHeader]) -> bytes | None:
        """Validate `headers` as one batch, then index every one of them.

        Returns the highest header this batch carried that is indexed
        now (new or already known), or `None` if the batch connects to
        nothing this index knows at all.
        """
        # Nothing is indexed until every header has been checked, and the
        # batch is taken or refused whole: chainwork is credited from the
        # header's own `bits`, so a header that keeps a target the chain
        # does not require becomes the best chain on work nobody agreed
        # to. A peer that sent one such header is not one to keep the
        # rest of the batch from either.
        headers = list(headers)
        pending = self._validate_header_batch(headers)
        self._insert_pending_headers(pending)

        # The header a caller should resume a sync from: the highest one
        # this batch carried that is indexed now, new or already known.
        # Not header_index[-1] -- a fork below the active chain's tip
        # never moves header_index, so a locator built from it would ask
        # for this same batch again and stall short of the fork's own
        # tip. None only for a batch that connects to nothing this index
        # knows at all. btclib-org/btclib-node#122
        for header in reversed(headers):
            if header.hash in self.header_dict:
                return header.hash
        return None

    # whether hash and everything back to the active chain has arrived,
    # not just hash itself -- a hole behind a downloaded tip is still a
    # hole, and get_first_candidate used to ask only the tip:
    # btclib-org/btclib-node#121
    def _branch_is_downloaded(self, block_hash: bytes) -> bool:
        to_add, _ = self.get_fork_details(block_hash)
        return all(self.get_block_info(h).downloaded for h in to_add)

    def get_first_candidate(self) -> BlockInfo | None:
        """Return the first downloaded candidate outweighing the active chain.

        Pops every stale entry (work below the active chain's own) off
        the front of `block_candidates`, then scans up to the 100 left:
        among those still ahead on work, the first whose whole branch is
        downloaded is returned, or the very first of them if none is,
        or `None` if there is no candidate ahead at all.
        """
        chainwork = self.chainwork[self.active_chain[-1]]
        while self.block_candidates and self.block_candidates[0][1] < chainwork:
            self.block_candidates.popleft()
        if not self.block_candidates:
            return None
        best_candidate = None
        for i in range(min(100, len(self.block_candidates))):
            block_hash, work = self.block_candidates[i]
            if work > chainwork:
                candidate = self.get_block_info(block_hash)
                if not best_candidate:
                    best_candidate = candidate
                if self._branch_is_downloaded(block_hash):
                    return candidate
        return best_candidate

    # return a list of blocks that have to be downloaded
    def get_download_candidates(self) -> list[bytes]:
        """Return every undownloaded block a candidate branch still needs.

        Walks each entry of `block_candidates` back from its own tip,
        collecting every not-yet-downloaded hash until it reaches one
        already seen or already on the active chain, then returns the
        union in height order, capped at `MAX_DOWNLOAD_WINDOW`.
        """
        chainwork = self.chainwork[self.active_chain[-1]]
        candidates: list[bytes] = []
        seen = set()
        i = -1
        while len(candidates) < MAX_DOWNLOAD_WINDOW:
            i += 1
            if i >= len(self.block_candidates):
                break
            candidate_hash, candidate_chainwork = self.block_candidates[i]
            if candidate_chainwork <= chainwork:
                continue
            while True:
                block_info = self.get_block_info(candidate_hash)
                if (
                    candidate_hash in seen
                    or block_info.status == BlockStatus.in_active_chain
                ):
                    break
                if not block_info.downloaded:
                    candidates.append(candidate_hash)
                seen.add(candidate_hash)
                candidate_hash = block_info.header.previous_block_hash
        candidates.sort(key=lambda x: self.get_block_info(x).index)
        return candidates[:MAX_DOWNLOAD_WINDOW]

    # return a list of block hashes looking at the current best chain
    def get_block_locator_hashes(self) -> list[bytes]:
        """Return a block locator over `header_index`, its own best known chain.

        Exponentially sparser going back from its own tip, always
        including its genesis -- the shape Core's own `LocatorEntries`
        builds, cited in the comment below.
        """
        i = 1
        step = 1
        block_locators: list[bytes] = []
        while True:
            if i > len(self.header_index):
                break
            block_locators.append(self.header_index[-i])
            # Core's own LocatorEntries (src/chain.cpp, aed80c7395):
            # `if (have.size() > 10) step *= 2`, a bare, unnamed 10 there
            # too -- matched rather than named, since naming it here
            # would claim a meaning Core's own algorithm never gave it
            if i >= 10:  # noqa: PLR2004
                step *= 2
            i += step
        if self.header_index[0] not in block_locators:
            block_locators.append(self.header_index[0])
        return block_locators

    def get_headers_from_locators(
        self, block_locators: Sequence[bytes], stop: bytes
    ) -> list[BlockHeader]:
        """Return up to 2000 headers after the first locator this index knows.

        `block_locators` is read in the caller's own order, so the
        first one found in `header_index` is where the answer resumes
        from. Stops at `stop` if reached first, and returns nothing if
        none of `block_locators` is known.

        Membership and position both come from `header_index_pos`
        rather than a scan of `header_index` itself
        (btclib-org/btclib-node#439). The slice is capped at 2000
        before `stop` is looked for, rather than after: `stop` is
        looked for inside the capped slice, not the whole of
        `header_index`, which is what btclib-org/btclib-node#434 raised
        `ValueError` on -- a `stop` at or below `block_locator`'s own
        height is never in the slice taken after it, so it is simply
        not found rather than raising, and the answer is the slice
        unchanged: empty where the locator is already this index's own
        tip, Core's own "nothing to send" for the same request.
        """
        output: list[bytes] = []
        for block_locator in block_locators:
            start = self.header_index_pos.get(block_locator)
            if start is None:
                continue
            output = self.header_index[start + 1 : start + 1 + 2000]
            if stop in output:
                output = output[: output.index(stop) + 1]
            break
        return [self.get_block_info(x).header for x in output]
