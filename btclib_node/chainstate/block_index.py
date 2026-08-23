# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import enum
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from btclib import var_int
from btclib.block import BlockHeader
from btclib.block.proof_of_work import block_work
from btclib.exceptions import BTClibValueError
from btclib.utils import bytesio_from_binarydata

from btclib_node.chains import Chain
from btclib_node.chainstate.contextual import assert_valid_in_context
from btclib_node.db import KeyValueStore
from btclib_node.log import Logger


def calculate_work(header: BlockHeader) -> int:
    return block_work(header.bits)


class BlockStatus(enum.IntEnum):
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
    header: BlockHeader
    index: int
    status: BlockStatus = BlockStatus(1)
    downloaded: bool = False

    @classmethod
    def deserialize(cls, data: bytes, check_validity: bool = True) -> BlockInfo:
        stream = bytesio_from_binarydata(data)
        header = BlockHeader.parse(stream, check_validity=check_validity)
        index = var_int.parse(stream)
        status = BlockStatus.from_bytes(stream.read(1), "little")
        downloaded = bool(int.from_bytes(stream.read(1), "little"))
        return cls(header, index, status, downloaded)

    def serialize(self) -> bytes:
        out = self.header.serialize()
        out += var_int.serialize(self.index)
        out += self.status.to_bytes(1, "little")
        out += int(self.downloaded).to_bytes(1, "little")
        return out


class BlockIndex:
    def __init__(self, parent_db: KeyValueStore, chain: Chain, logger: Logger) -> None:
        self.logger = logger

        self.db = parent_db

        # the network, for what `add_headers` requires of a header
        # besides the eighty bytes: its easiest target, and the two
        # consensus parameters that decide how the target moves
        self.chain = chain

        genesis = chain.genesis
        genesis_info = BlockInfo(genesis, 0, BlockStatus.in_active_chain, True)

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

        # the reverse of previous_block_hash, kept so invalidate can walk
        # forward from a bad block to what is really built on it instead
        # of scanning header_dict whole: btclib-org/btclib-node#125
        self.children: dict[bytes, list[bytes]] = {}

        self.init_from_db()

    def init_from_db(self) -> None:
        self.logger.info("Start Index initialization")
        for key, value in self.db:
            prefix, key = key[:8], key[8:]
            if prefix != b"blkinfo-":  # utxo_index
                break
            self.header_dict[key] = BlockInfo.deserialize(value, check_validity=False)

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
        chain_dict: dict[int, bytes] = {}
        for block_hash, block_info in self.header_dict.items():
            if block_info.status == BlockStatus.in_active_chain:
                chain_dict[block_info.index] = block_hash
        for index in sorted(chain_dict.keys()):
            self.active_chain.append(chain_dict[index])

    def generate_block_candidates(self) -> None:
        active_chain_set = set(self.active_chain)
        current_work = self.chainwork[self.active_chain[-1]]
        for block_hash in self.sorted_header_dict:
            if block_hash in active_chain_set:
                continue
            block_info = self.get_block_info(block_hash)
            if block_info.status != BlockStatus.valid_header:
                continue
            # header = block_info.header
            work = self.chainwork[block_hash]
            if work > current_work:
                self.block_candidates.append([block_hash, work])

    def generate_header_index(self) -> None:
        self.header_index = self.active_chain[:]
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
                header_index_set.add(block_hash)
            elif self.chainwork[block_hash] > self.chainwork[best_header]:
                add, remove = self.get_fork_details(block_hash, self.header_index)
                self.header_index = self.header_index[: -len(remove)]
                self.header_index.extend(add)
                header_index_set = set(self.header_index)

    # `wb` is a write batch: given one, the database moves when that
    # batch commits, where `header_dict` moves now either way
    def _insert_block_info(
        self, block_info: BlockInfo, wb: KeyValueStore | None = None
    ) -> None:
        hash = block_info.header.hash
        # a genuinely new hash, and not set_status/set_downloaded
        # overwriting the record already there for it: children is the
        # index invalidate walks, and a hash already present had its
        # parentage recorded the one time it was new
        if hash not in self.header_dict:
            self.children.setdefault(block_info.header.previous_block_hash, []).append(
                hash
            )
        self.header_dict[hash] = block_info
        key = b"blkinfo-" + hash
        value = block_info.serialize()
        db = wb or self.db
        db.put(key, value)

    # the fields a caller changes, read here rather than by the caller,
    # so that what goes back is the record the index holds now
    def set_status(
        self, hash: bytes, status: BlockStatus, wb: KeyValueStore | None = None
    ) -> None:
        self._insert_block_info(replace(self.get_block_info(hash), status=status), wb)

    def set_downloaded(self, hash: bytes, downloaded: bool = True) -> None:
        block_info = self.get_block_info(hash)
        self._insert_block_info(replace(block_info, downloaded=downloaded))

    def get_block_info(self, hash: bytes) -> BlockInfo:
        return self.header_dict[hash]

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
    def invalidate(self, hash: bytes) -> None:
        to_invalidate = [hash]
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
            self._extend_header_index(
                sorted(self.header_dict, key=lambda h: self.header_dict[h].index)
            )

    # returns the active chain and the forked chain from the common ancestor
    def get_fork_details(
        self, header_hash: bytes, chain: list[bytes] | None = None
    ) -> tuple[list[bytes], list[bytes]]:
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
        self.active_chain.append(block_hash)

    def remove_from_active_chain(self, block_hash: bytes) -> None:
        if block_hash != self.active_chain[-1]:
            raise Exception
        self.active_chain.pop()

    def add_headers(self, headers: Iterable[BlockHeader]) -> bytes | None:
        # Nothing is indexed until every header has been checked, and the
        # batch is taken or refused whole: chainwork below is credited
        # from the header's own `bits`, so a header that keeps a target
        # the chain does not require becomes the best chain on work
        # nobody agreed to. A peer that sent one such header is not one
        # to keep the rest of the batch from either.
        #
        # `pending` is what a header brought by this batch is weighed
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
        headers = list(headers)
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
                            raise BTClibValueError(
                                "a header's parent is later in the same batch"
                            )
                        continue
                    found = (block_info.header, block_info.index)
                parent, parent_height = found
                assert_valid_in_context(
                    self.chain, header, parent, parent_height, parent_of, now
                )
            except BTClibValueError as e:
                self.logger.warning(f"Refused a header batch: {e}")
                raise
            pending[header_hash] = (header, parent_height + 1)

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
                False,
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
                elif new_work > self.chainwork[best_header]:
                    add, remove = self.get_fork_details(header_hash, self.header_index)
                    self.header_index = self.header_index[: -len(remove)]
                    self.header_index.extend(add)

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
    def _branch_is_downloaded(self, hash: bytes) -> bool:
        to_add, _ = self.get_fork_details(hash)
        return all(self.get_block_info(h).downloaded for h in to_add)

    def get_first_candidate(self) -> BlockInfo | None:
        chainwork = self.chainwork[self.active_chain[-1]]
        while self.block_candidates and self.block_candidates[0][1] < chainwork:
            self.block_candidates.popleft()
        if not self.block_candidates:
            return None
        best_candidate = None
        for i in range(min(100, len(self.block_candidates))):
            hash, work = self.block_candidates[i]
            if work > chainwork:
                candidate = self.get_block_info(hash)
                if not best_candidate:
                    best_candidate = candidate
                if self._branch_is_downloaded(hash):
                    return candidate
        return best_candidate

    # return a list of blocks that have to be downloaded
    def get_download_candidates(self) -> list[bytes]:
        chainwork = self.chainwork[self.active_chain[-1]]
        candidates: list[bytes] = []
        seen = set()
        i = -1
        while len(candidates) < 1024:
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
        return candidates[:1024]

    # return a list of block hashes looking at the current best chain
    def get_block_locator_hashes(self) -> list[bytes]:
        i = 1
        step = 1
        block_locators: list[bytes] = []
        while True:
            if i > len(self.header_index):
                break
            block_locators.append(self.header_index[-i])
            if i >= 10:
                step *= 2
            i += step
        if self.header_index[0] not in block_locators:
            block_locators.append(self.header_index[0])
        return block_locators

    def get_headers_from_locators(
        self, block_locators: Sequence[bytes], stop: bytes
    ) -> list[BlockHeader]:
        output: list[bytes] = []
        for block_locator in block_locators:
            if block_locator not in self.header_index:
                continue
            start = self.header_index.index(block_locator)
            output = self.header_index[start + 1 :]
            if stop in self.header_index:
                end = output.index(stop)
                output = output[: end + 1]
            output = output[:2000]
            break
        return [self.get_block_info(x).header for x in output]
