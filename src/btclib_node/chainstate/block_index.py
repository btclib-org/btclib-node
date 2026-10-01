# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`BlockIndex`, every header this node has seen and which chain is active.

`BlockStatus` tracks a header from `valid_header` up through however far
its block has been validated. `invalidate` is what a failed contextual
check calls, through `main.update_header_index`, to drop a header and
everything built on it. `stage_status` and `finalize` are `set_status`
split into its two halves -- the in-memory move and the disk write -- so
that `main._finalize_fork` can hold the second half back across more
than one block; `db.py`'s own docstring is where that staging, shared
with `UtxoIndex`, is argued.

`set_downloaded` and `set_status` both check `pending` before writing
through, and for the same reason: either can be asked to change a hash
`pending` already holds, unflushed, and a write-through there would only
be undone the next time `finalize` writes that pending entry's own stale
snapshot back over it.

For `set_status` this is reached because `invalidate`'s own caller can
name a hash that already connected once. `update_chain` sets
`failed_hash` to a block across `utxo_index.add_block`,
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

For `set_downloaded` no caller reaches this today: `main.prune_up_to_height`
flushes the chainstate first, which empties `pending`, and
`p2p.callbacks.block` sets the flag only on a hash not yet downloaded. The
check stays so that a caller that does not flush first cannot have its write
undone by the next `finalize`.
"""

import enum
import itertools
import math
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from btclib import var_int
from btclib.block import (
    BlockHeader,
    ParentOf,
    median_time_past,
    next_bits_required,
)
from btclib.block.proof_of_work import block_work
from btclib.exceptions import BTClibValueError
from btclib.utils import bytesio_from_binarydata

from btclib_node.exceptions import (
    ChainstateInconsistencyError,
    LowWorkHeaderError,
    MisbehavingError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from btclib_node.chains import Chain
    from btclib_node.db import KeyValueStore
    from btclib_node.log import Logger

__all__ = [
    "BlockIndex",
    "BlockInfo",
    "BlockStatus",
    "block_time",
    "calculate_work",
    "check_headers_pow",
    "locator_entries",
]


def calculate_work(header: BlockHeader) -> int:
    """Return the work `header`'s own target represents."""
    return block_work(header.bits)


def _invert_lowest_one(n: int) -> int:
    return n & (n - 1)


def _skip_height(height: int) -> int:
    """Return the height a skip pointer jumps back to: Core's `GetSkipHeight`.

    `src/chain.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag.
    """
    if height < 2:  # noqa: PLR2004
        return 0
    if height & 1:
        return _invert_lowest_one(_invert_lowest_one(height - 1)) + 1
    return _invert_lowest_one(height)


def locator_entries(block_index: BlockIndex, block_hash: bytes) -> list[bytes]:
    """Return a block locator from `block_hash`: Core's `LocatorEntries`.

    Core's is in `src/chain.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag: `block_hash` and its ancestors, one height apart for the
    first ten and twice as far apart for each one after, down to
    genesis. This is the tree's one copy of it (btclib-org/btclib-node#1530):
    `BlockIndex.locator_entries`, `BlockIndex.get_block_locator_hashes`
    and `p2p.chain_sync`'s own are its callers.

    Core reaches each ancestor with `GetAncestor`. Where `block_hash` is
    on `header_index`, which holds the best header chain by height, the
    ancestor at a height is that entry of it, read in constant time; a
    block anywhere else is walked back through `get_ancestor`'s skip
    pointers, the same blocks at a cost logarithmic in the distance.
    """
    height = block_index.header_dict[block_hash].index
    on_header_index = block_index.header_index_pos.get(block_hash) == height
    step = 1
    have: list[bytes] = []
    while True:
        have.append(block_hash)
        if height == 0:
            return have
        height = max(height - step, 0)
        if on_header_index:
            block_hash = block_index.header_index[height]
        else:
            # always found: `height` is below `block_hash`'s own
            block_hash = cast("bytes", block_index.get_ancestor(block_hash, height))
        if len(have) > 10:  # noqa: PLR2004 -- Core's own bare 10
            step *= 2


def block_time(header: BlockHeader) -> int:
    """Return the second the header's four timestamp bytes hold.

    Core's `CBlockHeader::GetBlockTime`. `BlockHeader.serialize` writes
    `int(time.timestamp())`, so that is the value the rules here
    compare: a header is weighed as it goes on the wire and not as it
    was built. `main.py` and `rpc.callbacks` read it from here rather
    than from a module of their own, `chainstate` being the lowest
    layer both already depend on.
    """
    return int(header.time.timestamp())


def _refusal_text(error: BaseException) -> str:
    """Return `error`'s reason, then the detail it was raised from, for a log.

    A header is refused with Core's reject reason alone, the word
    `submitheader` and `submitblock` answer. The detail Core's reason
    leaves out, btclib's message or the two numbers compared, is the
    refusal's `__cause__`.
    """
    cause = error.__cause__
    return str(error) if cause is None else f"{error} ({cause})"


def _assert_valid_pow(header: BlockHeader, pow_limit_bits: bytes) -> None:
    """Assert `header`'s own proof of work, as Core's `CheckHeadersPoW`.

    Core calls `Misbehaving` for a header failing it
    (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), so the refusal is a `MisbehavingError`. Its reason is
    `CheckBlockHeader`'s `high-hash` (`src/validation.cpp`, same sha),
    which Core answers for every way `CheckProofOfWork` fails, an
    out-of-range target included.
    """
    try:
        header.assert_valid_pow(pow_limit_bits)
    except BTClibValueError as e:
        err_msg = "high-hash"
        raise MisbehavingError(err_msg) from e


def check_headers_pow(headers: Sequence[BlockHeader], pow_limit_bits: bytes) -> None:
    """Core's `CheckHeadersPoW`: each header's proof of work, then continuity.

    Both are `Misbehaving` in Core (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), so both refusals are a
    `MisbehavingError`. `BlockIndex.add_headers` asks it too, of every
    batch it is given.
    """
    for header in headers:
        _assert_valid_pow(header, pow_limit_bits)
    _assert_continuous(headers)


def _assert_continuous(headers: Sequence[BlockHeader]) -> None:
    """Assert each header builds on the one before it in `headers`.

    Core's `CheckHeadersAreContinuous`, which `CheckHeadersPoW` answers
    with `Misbehaving` ("non-continuous headers sequence") ahead of any
    `AcceptBlockHeader` (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), so the refusal is a
    `MisbehavingError`.
    """
    for previous, header in itertools.pairwise(headers):
        if header.previous_block_hash != previous.hash:
            err_msg = "non-continuous headers sequence"
            raise MisbehavingError(err_msg)


def _assert_valid_in_context(  # noqa: PLR0913, PLR0917
    chain: Chain,
    header: BlockHeader,
    parent: BlockHeader,
    parent_height: int,
    parent_of: ParentOf,
    now: datetime,
) -> None:
    """Assert what the chain before `header` requires of it.

    Core's `ContextualCheckBlockHeader`: `GetNextWorkRequired` for the
    target and `CBlockIndex::GetMedianTimePast` for the timestamp, both
    btclib's own (`btclib.block.next_bits_required`,
    `median_time_past`), over `chain.consensus` -- the per-network row
    that tells `next_bits_required` which of Core's branches apply,
    `pow_no_retargeting` and `pow_allow_min_difficulty_blocks` among
    them, in Core's own order rather than one this tree chooses.
    `BlockHeader.assert_valid_time` is the one check that needs no
    chain at all. Last, a version BIP34, BIP66 or BIP65 made obsolete is
    refused from the height each binds at, `chain.consensus`'s
    `bip34_height`, `bip66_height` and `bip65_height`, as Core's
    `bad-version` (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag). `BlockHeader.assert_valid_pow` answers the other half
    of the proof-of-work question -- whether the hash meets the target
    the header itself claims -- and `_validate_header_batch`'s own loop
    has already asked it of `header`, ahead of this.

    The target, the median time, the version and BIP94's own timewarp
    bound are Core's `bad-diffbits`, `time-too-old`, `bad-version` and
    `time-timewarp-attack`, every one of them `BLOCK_INVALID_HEADER`,
    which Core's `MaybePunishNodeForBlock` answers with `Misbehaving`, so
    they raise `MisbehavingError`. `next_bits_required` raises a bare
    `BTClibValueError` for the timewarp bound, having no
    `MisbehavingError` of its own to raise -- this tree's exception and
    not btclib's -- so it is caught and re-raised as one here, the way
    `_assert_valid_pow` already does for `assert_valid_pow`'s own.
    `time-too-new` is `BLOCK_TIME_FUTURE`, which it does not punish, so
    it stays a bare `BTClibValueError` (`src/validation.cpp` and
    `src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag).

    Each is raised with Core's reject reason as its whole message, the
    word `submitheader` answers, the detail kept as the `__cause__`
    `_refusal_text` reads. `next_bits_required` raises the timewarp bound
    before it answers anything, so a header breaking it and the target or
    the median time too is refused `time-timewarp-attack`, where Core
    reports `bad-diffbits` or `time-too-old` (btclib-org/btclib#2465).
    The timewarp bound is the only raise of `next_bits_required` reachable
    for a header and a parent this index holds.
    """
    try:
        required = next_bits_required(
            header, parent, parent_height, parent_of, chain.consensus
        )
    except BTClibValueError as e:
        err_msg = "time-timewarp-attack"
        raise MisbehavingError(err_msg) from e
    if header.bits != required:
        detail = f"proof-of-work target not the required one: {header.bits.hex()}"
        detail += f" instead of {required.hex()}"
        err_msg = "bad-diffbits"
        raise MisbehavingError(err_msg) from BTClibValueError(detail)

    median = median_time_past(parent, parent_height, parent_of)
    time = block_time(header)
    if time <= median:
        detail = f"invalid timestamp (not after the median past): {time}"
        detail += f" <= {median}"
        err_msg = "time-too-old"
        raise MisbehavingError(err_msg) from BTClibValueError(detail)

    try:
        header.assert_valid_time(now)
    except BTClibValueError as e:
        err_msg = "time-too-new"
        raise BTClibValueError(err_msg) from e

    # the least version a header may carry once each of BIP34, BIP66 and
    # BIP65 binds, and the height it binds from
    consensus = chain.consensus
    for least, binds_at in (
        (2, consensus.bip34_height),
        (3, consensus.bip66_height),
        (4, consensus.bip65_height),
    ):
        if header.version < least and parent_height + 1 >= binds_at:
            err_msg = f"bad-version(0x{header.version & 0xFFFFFFFF:08x})"
            raise MisbehavingError(err_msg)


class BlockStatus(enum.IntEnum):
    """Where a block stands relative to the active chain.

    `valid_header` is a header on its own, content not yet checked;
    `in_active_chain` is on the active chain now; `valid` is a block
    whose content passed validation but that a reorg has since removed
    from the active chain (`_finalize_fork`'s own `to_remove` loop is
    the only place that sets it). `invalid` is set on a block itself or
    on any block built on one already marked `invalid` -- not terminal,
    since `reconsider` below is exactly what clears it again.
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
        """Serialize this record to the bytes stored under `blkinfo-<hash>`.

        The header checked here now. It used to be serialized unchecked:
        a header of a version zero or below is Core's to take below
        BIP34's height, and btclib's `BlockHeader.assert_valid` refused
        it on its own -- fixed at btclib 2026.9.29
        (btclib-org/btclib@bbb1ad71, closing btclib-org/btclib#2309;
        btclib-org/btclib-node#1511). A header reaches a `BlockInfo` only
        past `add_headers`'s own height-gated `bad-version` check
        (`_assert_valid_in_context`), so nothing `assert_valid` still
        checks can refuse one that got here honestly.
        """
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
    (issue #439), and `skip` is each header's skip pointer, Core's
    `pskip`, which `get_ancestor` and `last_common_ancestor` follow.
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

        # each header's ancestor at `_skip_height` of its own height:
        # Core's `CBlockIndex::pskip`, which `get_ancestor` follows. Kept
        # beside header_dict, as chainwork is, and built where chainwork
        # is: Core does not store it either, and rebuilds it on load
        # (`BuildSkip`). Genesis has none.
        self.skip: dict[bytes, bytes] = {}

        # the actual block chain; it contains only valid blocks
        self.active_chain: list[bytes] = []

        # blocks that are waiting to be connected to the active chain,
        # each a [hash, chainwork] pair
        self.block_candidates: deque[list[Any]] = deque()

        # list all header hashes, even if not already checked, needed for
        # the block locators
        self.header_index: list[bytes] = []

        # header_index's own hash -> position, kept beside it rather
        # than computed from it: `locator_entries` and
        # `p2p.block_availability`'s block download ask where a block
        # is on header_index, which holds one entry per header this node
        # has ever indexed -- the whole known chain -- so a membership
        # test done against the list itself is an O(n) scan.
        # btclib-org/btclib-node#439, following chainwork (#201) and
        # children (#125) in keeping a derived index beside the primary
        # structure rather than recomputing it on every read. Maintained
        # incrementally at the same three sites that mutate header_index
        # -- generate_header_index, _extend_header_index and
        # _insert_valid_headers -- rather than rebuilt whole after each:
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

        # Core's own `ChainstateManager::m_best_invalid` (`src/validation.h`,
        # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
        # `calculate_chainwork` below sets it on load to the invalid block
        # with the most chainwork, as Core's `LoadBlockIndex` scan does
        # (`src/validation.cpp:4964-4965`, same sha). At runtime
        # `invalidate` weighs only the block it is handed, as Core's
        # `InvalidChainFound` does, and `reconsider` resets it. `None`
        # where nothing has been marked. `main.check_fork_warning_conditions`
        # is the only reader, for btclib-org/btclib-node#1522.
        self.best_invalid: bytes | None = None

        # Core's own `nSequenceId` (`src/chain.h`,
        # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), for each block
        # whose data, and every ancestor's, has arrived: the tie-break
        # beneath chainwork in `_outranks`, the lower number winning. In
        # memory only, as in Core. `_load_sequence_ids` numbers the
        # stored blocks 0 and 1, `_link` numbers each later arrival from
        # 2 up, and `precious` hands out the negative numbers, Core's
        # `nBlockReverseSequenceId` and `nLastPreciousChainwork` being its
        # two fields below.
        self.sequence_id: dict[bytes, int] = {}
        self._next_sequence_id = 2
        self._precious_sequence_id = -1
        self._precious_chainwork = 0

        # Core's own `m_blocks_unlinked` (`src/node/blockstorage.h`, same
        # sha): each block whose data arrived while its parent had no
        # number, under that parent and in the order its data arrived,
        # which is the order `_link` numbers them in.
        self._unlinked: dict[bytes, list[bytes]] = {}

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
        self._load_sequence_ids()
        self.logger.info("Start generate_block_candidates")
        self.generate_block_candidates()
        self.logger.info("Start generate_header_index")
        self.generate_header_index()
        self.logger.info("Finished Index initialization")

        self.sorted_header_dict = []

    def calculate_chainwork(self) -> None:
        """Compute every header's cumulative work into `chainwork`.

        Backfills `children` along the way, one entry per header visited,
        and `best_invalid` the same way Core's own `LoadBlockIndex` scan
        does (`src/validation.cpp:4964-4965`, at bitcoin/bitcoin@9be056a8a7,
        the v31.1 tag): a block already marked invalid when this index
        was last written beats whatever `best_invalid` already holds if
        its chainwork is greater.
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
            work = old_work + calculate_work(block_info.header)
            self.chainwork[block_hash] = work
            self._build_skip(block_hash, block_info)
            if block_info.status == BlockStatus.invalid and (
                self.best_invalid is None or work > self.chainwork[self.best_invalid]
            ):
                self.best_invalid = block_hash

    def _build_skip(self, block_hash: bytes, block_info: BlockInfo) -> None:
        """Set `block_hash`'s skip pointer: Core's `BuildSkip`, parent first."""
        if block_info.index:
            self.skip[block_hash] = self._ancestor(
                block_info.header.previous_block_hash,
                block_info.index - 1,
                _skip_height(block_info.index),
            )

    def get_ancestor(self, block_hash: bytes, height: int) -> bytes | None:
        """Return `block_hash`'s ancestor at `height`: Core's `GetAncestor`.

        Core's is in `src/chain.cpp`, at bitcoin/bitcoin@9be056a8a7, the
        v31.1 tag.

        The block itself at its own height, and `None` for a height
        above it or below zero, where Core answers `nullptr`. Follows
        skip pointers, so the cost grows with the logarithm of the
        distance rather than with the distance.
        """
        own_height = self.header_dict[block_hash].index
        if height > own_height or height < 0:
            return None
        return self._ancestor(block_hash, own_height, height)

    def _ancestor(self, block_hash: bytes, walk_height: int, height: int) -> bytes:
        header_dict = self.header_dict
        skip = self.skip
        walk = block_hash
        while walk_height > height:
            skip_height = _skip_height(walk_height)
            skip_height_prev = _skip_height(walk_height - 1)
            # Core's condition: only follow the skip pointer where the
            # parent's is not a better jump
            if walk in skip and (
                skip_height == height
                or (
                    skip_height > height
                    and not (
                        skip_height_prev < skip_height - 2
                        and skip_height_prev >= height
                    )
                )
            ):
                walk = skip[walk]
                walk_height = skip_height
            else:
                walk = header_dict[walk].header.previous_block_hash
                walk_height -= 1
        return walk

    def locator_entries(self, block_hash: bytes) -> list[bytes]:
        """Return a block locator from `block_hash`: `locator_entries`'s."""
        return locator_entries(self, block_hash)

    def last_common_ancestor(self, first: bytes, second: bytes) -> bytes:
        """Return the fork point of two blocks: Core's `LastCommonAncestor`.

        Core's is in `src/chain.cpp`, at bitcoin/bitcoin@9be056a8a7, the
        v31.1 tag.

        Both are brought to the lower one's height, then walked back
        through their skip pointers while those differ, and one parent
        at a time where they agree.
        """
        header_dict = self.header_dict
        first_height = header_dict[first].index
        second_height = header_dict[second].index
        if first_height > second_height:
            first = self._ancestor(first, first_height, second_height)
        elif second_height > first_height:
            second = self._ancestor(second, second_height, first_height)
        skip = self.skip
        while first != second:
            while skip.get(first) != skip.get(second):
                first = skip[first]
                second = skip[second]
            first = header_dict[first].header.previous_block_hash
            second = header_dict[second].header.previous_block_hash
        return first

    def generate_active_chain(self) -> None:
        """Rebuild `active_chain` from every header marked `in_active_chain`."""
        chain_dict: dict[int, bytes] = {}
        for block_hash, block_info in self.header_dict.items():
            if block_info.status == BlockStatus.in_active_chain:
                chain_dict[block_info.index] = block_hash
        for index in sorted(chain_dict.keys()):
            self.active_chain.append(chain_dict[index])

    def _load_sequence_ids(self) -> None:
        """Give each stored block the number Core gives it on load.

        `SEQ_ID_BEST_CHAIN_FROM_DISK`, 0, for the active chain, and
        `SEQ_ID_INIT_FROM_DISK`, 1, for every other block whose data, and
        every ancestor's, has arrived (`src/chain.h` and `LoadChainTip`
        in `src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag): a tie at start-up keeps the tip. A block whose data arrived
        and whose parent has no number goes to `_unlinked`, in height
        order, as `BlockManager::LoadBlockIndex` puts it in
        `m_blocks_unlinked` (`src/node/blockstorage.cpp`, same sha).
        """
        active_chain_set = set(self.active_chain)
        for block_hash in self.sorted_header_dict:
            block_info = self.header_dict[block_hash]
            parent = block_info.header.previous_block_hash
            if block_hash in active_chain_set:
                self.sequence_id[block_hash] = 0
            elif not block_info.downloaded:
                continue
            elif parent in self.sequence_id:
                self.sequence_id[block_hash] = 1
            else:
                self._unlinked.setdefault(parent, []).append(block_hash)

    def generate_block_candidates(self) -> None:
        """Rebuild `block_candidates` over every `valid_header`/`valid` entry.

        `init_from_db` calls this once at start-up, over every header the
        store holds; `reconsider` above and `main.invalidate_chain` call
        it again once disconnecting or reconnecting has moved the active
        chain's own tip work, since a candidate `get_first_candidate`
        already evicted as stale against the old tip is gone from the
        deque for good once popped -- reachable again only by rebuilding
        from `header_dict` whole, the way this does. Sorts `header_dict`
        itself rather than reading `sorted_header_dict`, the start-up-only
        list `init_from_db` frees right after this call returns there, so
        that a later caller finds the same list this one would have.
        Always starts from an empty deque rather than appending onto
        whatever is there, which only matters past start-up:
        `block_candidates` is empty already the one time `init_from_db`
        calls this.

        `precious` calls this too, once it has renumbered a block that
        may now outrank the tip.

        `valid` is offered alongside `valid_header` so that a branch a
        reorg has since displaced -- `_finalize_fork`'s own `to_remove`
        loop is what sets it -- becomes a candidate again once whatever
        displaced it is itself invalidated. Core's own `InvalidateBlock`
        (`src/validation.cpp:3663-3684`, at bitcoin/bitcoin@9be056a8a7,
        the v31.1 tag) re-inserts such an out-of-chain header into
        `setBlockIndexCandidates` only where
        `candidate->IsValid(BLOCK_VALID_TRANSACTIONS) &&
        candidate->HaveNumChainTxs()` both hold (`:3674-3677`) -- so a
        `valid` block this index still holds the data for is offered,
        matched here by requiring `downloaded` as well as the status,
        and one a completed prune has since cleared `downloaded` on
        (`main.prune_up_to_height`) is not, matching Core rather than
        the un-downloaded `valid_header` candidates this deque already
        carries for a different reason: a `valid_header` was never
        `BLOCK_VALID_TRANSACTIONS` in the first place, so it is not
        this same reinsertion Core's source is arguing, and changing
        that pre-existing, separately-argued divergence is out of
        scope here. btclib-org/btclib-node#1561

        A block is offered where it outranks the active tip (`_outranks`),
        so one of the tip's own work is offered where its `sequence_id`
        is the lower.
        """
        self.block_candidates = deque()
        active_chain_set = set(self.active_chain)
        tip = self.active_chain[-1]
        for block_hash in sorted(
            self.header_dict, key=lambda h: self.header_dict[h].index
        ):
            if block_hash in active_chain_set:
                continue
            block_info = self.get_block_info(block_hash)
            if block_info.status == BlockStatus.valid and not block_info.downloaded:
                continue
            if block_info.status not in (BlockStatus.valid_header, BlockStatus.valid):
                continue
            if self._outranks(block_hash, tip):
                self.block_candidates.append([block_hash, self.chainwork[block_hash]])

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
        """Set `block_hash`'s `downloaded` flag, replacing its `BlockInfo`.

        Checks `pending` first, the same shape `set_status` above already
        does; the module docstring's last paragraph says why, and that no
        caller reaches it today.

        A block whose data arrives here is numbered by `_link`.
        """
        arrived = downloaded and not self.get_block_info(block_hash).downloaded
        block_info = replace(self.get_block_info(block_hash), downloaded=downloaded)
        if block_info.header.hash in self.pending:
            self._stage(block_info)
        else:
            self._insert_block_info(block_info)
        if arrived:
            self._link(block_hash)

    def _link(self, block_hash: bytes) -> None:
        """Give a block whose data arrived the next number, if it can take one.

        Core's `ReceivedBlockTransactions` (`src/validation.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Where its parent has
        no number yet, the block waits in `_unlinked`. Otherwise it takes
        the next number, and so does each block that waited on it,
        breadth first and, under one parent, in the order their data
        arrived, as Core's queue over `m_blocks_unlinked` numbers them.
        """
        parent = self.header_dict[block_hash].header.previous_block_hash
        if parent not in self.sequence_id:
            self._unlinked.setdefault(parent, []).append(block_hash)
            return
        queue = deque([block_hash])
        while queue:
            current = queue.popleft()
            self.sequence_id[current] = self._next_sequence_id
            self._next_sequence_id += 1
            queue.extend(self._unlinked.pop(current, ()))

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

        Compares `block_hash` alone against `best_invalid`, as Core's own
        `InvalidChainFound` does (`src/validation.cpp:1971-1974`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag): its
        `SetBlockFailureFlags` marks the descendants without touching
        `m_best_invalid`. `calculate_chainwork` is the load-time path,
        which weighs every invalid block, descendants included.
        """
        self.weigh_invalid(block_hash)
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

    def weigh_invalid(self, block_hash: bytes) -> None:
        """Make `block_hash` the `best_invalid` if it carries more work.

        Core's own comparison wherever it writes `m_best_invalid`
        (`src/validation.cpp:1971-1973`, at bitcoin/bitcoin@9be056a8a7,
        the v31.1 tag): strictly greater, so a tie keeps the first.
        """
        if self.best_invalid is None or (
            self.chainwork[block_hash] > self.chainwork[self.best_invalid]
        ):
            self.best_invalid = block_hash

    def _shares_lineage(
        self, other_hash: bytes, target: bytes, target_height: int
    ) -> bool:
        """Whether `other_hash` is `target`'s own ancestor, descendant or self.

        Core's `ResetBlockFailureFlags` own condition
        (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag): `other_hash` is a descendant where its ancestor at
        `target`'s height is `target` itself, and an ancestor (`other_hash`
        is equal or below `target`'s own height) where `target`'s own
        ancestor at `other_hash`'s height is `other_hash`. `other_hash ==
        target` is the first clause's own degenerate case -- a hash is
        its own ancestor at its own height -- so nothing here
        special-cases it.
        """
        other_height = self.header_dict[other_hash].index
        return (
            self.get_ancestor(other_hash, target_height) == target
            or self.get_ancestor(target, other_height) == other_hash
        )

    def reconsider(self, block_hash: bytes) -> None:
        """Undo `invalidate`'s mark on `block_hash`'s own lineage, then rebuild.

        Core's own `Chainstate::ResetBlockFailureFlags` (`src/validation
        .cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): every header
        this index holds that is `block_hash` itself, one of its
        ancestors, or one of its descendants, and that `invalidate` above
        marked, has that mark cleared -- not only `block_hash` itself,
        since an ancestor `invalidate` reached through `block_hash` and a
        descendant built on it are both still wrongly `invalid` once
        `block_hash` no longer is. `block_candidates` and `header_index`
        are rebuilt whole afterward rather than patched: a header this
        clears may now be the best known header chain, or a legitimate
        candidate `get_first_candidate` already evicted as permanently
        stale against a tip the clearing has not yet moved.

        `best_invalid` is reset to `None` where it names a header this
        clears, as Core's own loop resets `m_best_invalid` to `nullptr`
        (`:3772-3775`), without looking for the next-best invalid header.
        """
        target_height = self.get_block_info(block_hash).index
        for other_hash, other_info in list(self.header_dict.items()):
            if other_info.status == BlockStatus.invalid and self._shares_lineage(
                other_hash, block_hash, target_height
            ):
                self.set_status(other_hash, BlockStatus.valid_header)
                if other_hash == self.best_invalid:
                    self.best_invalid = None
        self.generate_block_candidates()
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

    # add_headers' own precondition stage, over the whole batch and ahead
    # of indexing any of it: each header's own proof of work, then
    # continuity -- Core's own `CheckHeadersPoW` (`net_processing.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which checks both
    # over the whole message before any header of it reaches
    # `AcceptBlockHeader` -- so a header failing either leaves nothing
    # of the batch indexed, the valid headers ahead of it included.
    #
    # A refusal raises rather than answers False: it is a peer that
    # sent a header failing on its own terms, not the ordinary end
    # of a sync, and the caller needs to be able to tell the two
    # apart. btclib-org/btclib-node#75
    def _validate_header_batch(self, headers: list[BlockHeader]) -> None:
        try:
            check_headers_pow(headers, self.chain.pow_limit_bits)
        except BTClibValueError as e:
            self.logger.warning("Refused a header batch: %s", _refusal_text(e))
            raise

    # add_headers' own indexing stage, once `_validate_header_batch`
    # above has cleared the whole batch's own proof of work and
    # continuity: each header is checked against the index -- a known
    # header marked invalid is `duplicate-invalid`
    # (`_is_indexed`), one whose parent is marked invalid is
    # `bad-prevblk` (`_indexed_parent`), one whose parent is not indexed
    # at all is left alone, there being no chain yet to weigh it
    # against -- and, where it passes every one of those, indexed
    # immediately: to `header_dict`, `chainwork`, `block_candidates` and
    # `header_index` where the batch's own work actually beats what
    # each already holds -- before the next header of the batch is even
    # looked at. Core's own `AcceptBlockHeader`, called once per header
    # from `ProcessNewBlockHeaders` (`validation.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): it stops at the first
    # header failing `ContextualCheckBlockHeader` and returns, so the
    # headers already accepted ahead of it, indexed one at a time as
    # they were accepted, stay indexed. btclib-org/btclib-node#1348
    #
    # `min_pow_checked` is Core's own parameter of the same name: where it
    # is false, a new header passing every check above is refused as
    # `too-little-chainwork` rather than indexed, after those checks as in
    # Core, so a header that is also invalid is refused for that instead.
    def _insert_valid_headers(
        self,
        headers: list[BlockHeader],
        *,
        punish_cached_invalid: bool,
        min_pow_checked: bool,
    ) -> None:
        now = datetime.now(UTC)
        current_work = self.chainwork[self.active_chain[-1]]

        # every header already indexed by this same call is in
        # header_dict by the time a header built on it is reached, the
        # write below being immediate rather than deferred -- so this
        # needs no fallback of its own the way the old pending dict did.
        def parent_of(header: BlockHeader) -> BlockHeader:
            return self.header_dict[header.previous_block_hash].header

        for header in headers:
            if self._is_indexed(header, punish_cached_invalid=punish_cached_invalid):
                continue
            found = self._indexed_parent(header)
            if found is None:
                continue
            parent, parent_height = found
            try:
                _assert_valid_in_context(
                    self.chain, header, parent, parent_height, parent_of, now
                )
            except BTClibValueError as e:
                self.logger.warning(
                    "Refused a header, keeping the ones before it: %s",
                    _refusal_text(e),
                )
                raise
            if not min_pow_checked:
                self.logger.log_debug(
                    "validation",
                    "AcceptBlockHeader: not adding new block header %s, "
                    "missing anti-dos proof-of-work validation",
                    header.hash.hex(),
                )
                raise LowWorkHeaderError

            header_hash = header.hash
            height = parent_height + 1
            new_work = self.chainwork[header.previous_block_hash] + calculate_work(
                header
            )
            # never on an invalid parent: `_indexed_parent` above refuses
            # that header before this point is ever reached
            block_info = BlockInfo(
                header,
                height,
                BlockStatus.valid_header,
                downloaded=False,
            )
            self._insert_block_info(block_info)
            self.chainwork[header_hash] = new_work
            self._build_skip(header_hash, block_info)

            if new_work > current_work:
                self.block_candidates.append([header_hash, new_work])
            self._grow_header_index(header, header_hash, new_work)

    def _is_indexed(self, header: BlockHeader, *, punish_cached_invalid: bool) -> bool:
        """Answer whether `header` is indexed already, refusing it if invalid.

        Core's `duplicate-invalid`, `BLOCK_CACHED_INVALID`: a
        `MisbehavingError` where `punish_cached_invalid`, a plain
        `BTClibValueError` otherwise. The reason is Core's word alone,
        the hash going to the debug line Core's `AcceptBlockHeader` logs
        (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
        tag).
        """
        header_hash = header.hash
        known = self.header_dict.get(header_hash)
        if known is None:
            return False
        if known.status == BlockStatus.invalid:
            self.logger.log_debug(
                "validation",
                "AcceptBlockHeader: block %s is marked invalid",
                header_hash.hex(),
            )
            err_msg = "duplicate-invalid"
            if punish_cached_invalid:
                raise MisbehavingError(err_msg)
            raise BTClibValueError(err_msg)
        return True

    def _indexed_parent(self, header: BlockHeader) -> tuple[BlockHeader, int] | None:
        """Answer `header`'s indexed parent and its height, None if unknown.

        Core's `bad-prevblk`, `BLOCK_INVALID_PREV`, where the parent is
        marked invalid: a `MisbehavingError`, its reason Core's word alone
        and the two hashes on Core's debug line.
        """
        block_info = self.header_dict.get(header.previous_block_hash)
        if block_info is None:
            return None
        if block_info.status == BlockStatus.invalid:
            self.logger.log_debug(
                "validation",
                "header %s has prev block invalid: %s",
                header.hash.hex(),
                header.previous_block_hash.hex(),
            )
            err_msg = "bad-prevblk"
            raise MisbehavingError(err_msg)
        return block_info.header, block_info.index

    # a peer sending more of an already-invalidated fork must not grow
    # header_index onto it, work alone deciding nothing here any more
    # than it did for block_candidates in the caller above.
    # btclib-org/btclib-node#218
    def _grow_header_index(
        self, header: BlockHeader, header_hash: bytes, new_work: int
    ) -> None:
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

    def add_headers(
        self,
        headers: Iterable[BlockHeader],
        *,
        punish_cached_invalid: bool = False,
        min_pow_checked: bool = True,
    ) -> bytes | None:
        """Validate `headers` as one batch, then index each in turn.

        Returns the highest header this batch carried that is indexed
        now (new or already known), or `None` if the batch connects to
        nothing this index knows at all. `punish_cached_invalid` is
        whether a header already marked invalid is a `MisbehavingError`,
        as Core has it for an outbound peer, rather than a
        `BTClibValueError`. `min_pow_checked` is whether the caller has
        checked the chain's work against `p2p.headers_sync`'s anti-DoS
        threshold, a `LowWorkHeaderError` being what a new header gets
        where it has not. True by default: `p2p.callbacks.headers` checks
        before calling, and Core's RPCs pass true, which leaves
        `p2p.callbacks.block` the one caller that can pass false.
        """
        # The batch's own proof of work and continuity are taken or
        # refused whole, ahead of indexing anything: chainwork is
        # credited from the header's own `bits`, so a header that keeps
        # a target the chain does not require becomes the best chain on
        # work nobody agreed to, and a peer sending headers out of order
        # is not sending the continuous chain this index can weigh at
        # all. A header failing its contextual check does not get the
        # same treatment: the headers ahead of it, already indexed one
        # at a time by then, stay indexed. btclib-org/btclib-node#1348
        headers = list(headers)
        self._validate_header_batch(headers)
        self._insert_valid_headers(
            headers,
            punish_cached_invalid=punish_cached_invalid,
            min_pow_checked=min_pow_checked,
        )

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

    def _outranks(self, block_hash: bytes, other: bytes) -> bool:
        """Answer whether `block_hash` sorts above `other`.

        Core's `CBlockIndexWorkComparator` (`node/blockstorage.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag). More chainwork wins.
        At equal chainwork the lower `sequence_id` wins, and a block
        without one, its data or an ancestor's still missing, loses.
        Where both tie, neither wins: Core breaks that tie by pointer
        address, which has no counterpart here, so the block already
        ahead stays ahead.
        """
        work, other_work = self.chainwork[block_hash], self.chainwork[other]
        if work != other_work:
            return work > other_work
        return self.sequence_id.get(block_hash, math.inf) < self.sequence_id.get(
            other, math.inf
        )

    def precious(self, block_hash: bytes) -> bool:
        """Treat `block_hash` as received before its rivals: `PreciousBlock`.

        `Chainstate::PreciousBlock` (`src/validation.cpp`, at
        bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Nothing for a block
        with less work than the tip, and `False`. Otherwise the block
        takes the next negative number, counting down from -1 again
        wherever the tip gained work since the last call,
        `block_candidates` is rebuilt so that it is offered where it now
        outranks the tip, and `True`.

        A block whose data, or an ancestor's, is missing keeps no
        number: Core gives it one that its data's arrival overwrites
        before anything compares it. Core stops counting down at the
        `int32_t` minimum, against overflow, which a Python int does not
        have.
        """
        tip_work = self.chainwork[self.active_chain[-1]]
        if self.chainwork[block_hash] < tip_work:
            return False
        if tip_work > self._precious_chainwork:
            self._precious_sequence_id = -1
        self._precious_chainwork = tip_work
        if block_hash in self.sequence_id:
            self.sequence_id[block_hash] = self._precious_sequence_id
        self._precious_sequence_id -= 1
        self.generate_block_candidates()
        return True

    def get_first_candidate(self) -> BlockInfo | None:
        """Return the first downloaded candidate outranking the active tip.

        Pops every stale entry (work below the active chain's own) off
        the front of `block_candidates`, then scans up to the 100 left:
        among those that outrank the tip (`_outranks`), the first whose
        whole branch is downloaded is returned, or the very first of
        them if none is, or `None` if none outranks the tip. A later
        downloaded entry of the same work replaces it where it outranks
        it, which is Core's tie-break; one of more work does not, where
        Core's `FindMostWorkChain` takes the most-work candidate.
        """
        tip = self.active_chain[-1]
        chainwork = self.chainwork[tip]
        while self.block_candidates and self.block_candidates[0][1] < chainwork:
            self.block_candidates.popleft()
        first: bytes | None = None
        ready: bytes | None = None
        for i in range(min(100, len(self.block_candidates))):
            block_hash, work = self.block_candidates[i]
            if not self._outranks(block_hash, tip):
                continue
            first = first or block_hash
            if (
                ready is None
                or (work == self.chainwork[ready] and self._outranks(block_hash, ready))
            ) and self._branch_is_downloaded(block_hash):
                ready = block_hash
        chosen = ready or first
        return None if chosen is None else self.get_block_info(chosen)

    # return a list of block hashes looking at the current best chain
    def get_block_locator_hashes(self, start: bytes | None = None) -> list[bytes]:
        """Return a block locator over `header_index`, its own best known chain.

        `locator_entries` from `start`, a header of `header_index`, or
        from its tip where none is given.
        """
        return locator_entries(self, self.header_index[-1] if start is None else start)
