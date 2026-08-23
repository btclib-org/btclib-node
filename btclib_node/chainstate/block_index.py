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
@dataclass(frozen=True)
class BlockInfo:
    header: BlockHeader
    index: int
    status: BlockStatus = BlockStatus(1)
    downloaded: bool = False
    chainwork: int = 0

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
                old_work = self.get_block_info(previousblockhash).chainwork
                self.children.setdefault(previousblockhash, []).append(block_hash)
            chainwork = old_work + calculate_work(block_info.header)
            # into the dict and not through `_insert_block_info`:
            # `serialize` does not carry chainwork, so it is derived
            # from the headers rather than stored. Constructed field by
            # field rather than with `replace`, this loop running once
            # per header at every start
            self.header_dict[block_hash] = BlockInfo(
                header=block_info.header,
                index=block_info.index,
                status=block_info.status,
                downloaded=block_info.downloaded,
                chainwork=chainwork,
            )

    def generate_active_chain(self) -> None:
        chain_dict: dict[int, bytes] = {}
        for block_hash, block_info in self.header_dict.items():
            if block_info.status == BlockStatus.in_active_chain:
                chain_dict[block_info.index] = block_hash
        for index in sorted(chain_dict.keys()):
            self.active_chain.append(chain_dict[index])

    def generate_block_candidates(self) -> None:
        active_chain_set = set(self.active_chain)
        current_work = self.get_block_info(self.active_chain[-1]).chainwork
        for block_hash in self.sorted_header_dict:
            if block_hash in active_chain_set:
                continue
            block_info = self.get_block_info(block_hash)
            if block_info.status != BlockStatus.valid_header:
                continue
            # header = block_info.header
            if block_info.chainwork > current_work:
                self.block_candidates.append([block_hash, block_info.chainwork])

    def generate_header_index(self) -> None:
        self.header_index = self.active_chain[:]
        header_index_set = set(self.header_index)
        for block_hash in self.sorted_header_dict:
            if block_hash in header_index_set:
                continue
            block_info = self.get_block_info(block_hash)
            header = block_info.header
            best_header = self.header_index[-1]
            if header.previous_block_hash == self.header_index[-1]:
                self.header_index.append(block_hash)
                header_index_set.add(block_hash)
            elif block_info.chainwork > self.get_block_info(best_header).chainwork:
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
    # this index has ever indexed on top of it, candidate or not --
    # `children` is walked rather than `header_dict` or
    # `block_candidates`, so this costs the size of the bad lineage and
    # not the size of the index. `add_headers` refuses to build a
    # valid_header on an invalid parent, which is what keeps a header
    # arriving *after* this call from needing to be walked here. No hash
    # is ever pushed twice: `_insert_block_info` records a hash as a
    # child the one time it is new, so it is a value of `children` under
    # exactly one parent, and the walk below cannot reach it a second
    # time.
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

    def add_headers(self, headers: Iterable[BlockHeader]) -> bool:
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
        # against, and nothing to give it a height.
        now = datetime.now(UTC)
        pow_limit_bits = self.chain.pow_limit_bits
        pending: dict[bytes, tuple[BlockHeader, int]] = {}

        def parent_of(header: BlockHeader) -> BlockHeader:
            previous = header.previous_block_hash
            if previous in pending:
                return pending[previous][0]
            return self.header_dict[previous].header

        for header in headers:
            header_hash = header.hash
            try:
                header.assert_valid_pow(pow_limit_bits)
                if header_hash in self.header_dict or header_hash in pending:
                    continue
                found = pending.get(header.previous_block_hash)
                if found is None:
                    block_info = self.header_dict.get(header.previous_block_hash)
                    if block_info is None:
                        continue
                    found = (block_info.header, block_info.index)
                parent, parent_height = found
                assert_valid_in_context(
                    self.chain, header, parent, parent_height, parent_of, now
                )
            except BTClibValueError as e:
                self.logger.warning(f"Refused a header batch: {e}")
                return False
            pending[header_hash] = (header, parent_height + 1)

        current_work = self.get_block_info(self.active_chain[-1]).chainwork
        for header_hash, (header, height) in pending.items():
            previous_block_info = self.get_block_info(header.previous_block_hash)
            new_work = previous_block_info.chainwork + calculate_work(header)
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
                new_work,
            )
            self._insert_block_info(block_info)

            if not invalid and new_work > current_work:
                self.block_candidates.append([header_hash, new_work])

            best_header = self.header_index[-1]
            if header.previous_block_hash == best_header:
                self.header_index.append(header_hash)
            elif new_work > self.get_block_info(best_header).chainwork:
                add, remove = self.get_fork_details(header_hash, self.header_index)
                self.header_index = self.header_index[: -len(remove)]
                self.header_index.extend(add)

        # whether the batch carried a header this node did not have
        return bool(pending)

    # whether hash and everything back to the active chain has arrived,
    # not just hash itself -- a hole behind a downloaded tip is still a
    # hole, and get_first_candidate used to ask only the tip:
    # btclib-org/btclib-node#121
    def _branch_is_downloaded(self, hash: bytes) -> bool:
        to_add, _ = self.get_fork_details(hash)
        return all(self.get_block_info(h).downloaded for h in to_add)

    def get_first_candidate(self) -> BlockInfo | None:
        chainwork = self.get_block_info(self.active_chain[-1]).chainwork
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
        chainwork = self.get_block_info(self.active_chain[-1]).chainwork
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
