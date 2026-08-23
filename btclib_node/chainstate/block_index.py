# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import enum
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from btclib import var_int
from btclib.block import BlockHeader
from btclib.block.proof_of_work import block_work
from btclib.exceptions import BTClibValueError
from btclib.utils import bytesio_from_binarydata

from btclib_node.chains import Chain
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

        # the network's easiest target, which every header this index
        # accepts has to beat; btclib defaults it to mainnet's
        self.pow_limit_bits = chain.pow_limit_bits

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
        self.header_dict[block_info.header.hash] = block_info
        key = b"blkinfo-" + block_info.header.hash
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
        # Before anything is indexed, and all of them: chainwork below is
        # credited from the header's own `bits`, so an unchecked header
        # can claim any amount of work and become the best chain. The
        # message is taken or refused whole -- a peer that sent one bad
        # header is not one to keep the rest of the batch from.
        #
        # This is CheckProofOfWork, not ContextualCheckBlockHeader: the
        # target a header is *required* to have at its height, and the
        # median-time-past it must follow, go unchecked.
        # btclib-org/btclib-node#118
        for header in headers:
            try:
                header.assert_valid_pow(self.pow_limit_bits)
            except BTClibValueError as e:
                self.logger.warning(f"Refused a header batch: {e}")
                return False

        added = False  # flag that signals if there is a new header in this message
        current_work = self.get_block_info(self.active_chain[-1]).chainwork
        for header in headers:
            header_hash = header.hash

            if header_hash in self.header_dict:
                continue
            if header.previous_block_hash not in self.header_dict:
                continue
            added = True
            previous_block_info = self.get_block_info(header.previous_block_hash)
            new_work = previous_block_info.chainwork + calculate_work(header)
            block_info = BlockInfo(
                header,
                previous_block_info.index + 1,
                BlockStatus.valid_header,
                False,
                new_work,
            )
            self._insert_block_info(block_info)

            if new_work > current_work:
                self.block_candidates.append([header_hash, new_work])

            best_header = self.header_index[-1]
            if header.previous_block_hash == best_header:
                self.header_index.append(header_hash)
            elif new_work > self.get_block_info(best_header).chainwork:
                add, remove = self.get_fork_details(header_hash, self.header_index)
                self.header_index = self.header_index[: -len(remove)]
                self.header_index.extend(add)

        return added

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
                if candidate.downloaded:
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
