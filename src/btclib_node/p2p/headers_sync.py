# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The low-work headers sync: a peer's chain downloaded twice before it is kept.

`HeadersSyncState` is Core's class of the same name (`src/headerssync.h`
and `src/headerssync.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
tag), and `anti_dos_work_threshold` is its `GetAntiDoSWorkThreshold`
(`src/net_processing.cpp`, same sha). `callbacks.headers` starts one
per peer whose headers connect to this node's index on a chain with
less work than that threshold, and hands it every batch that peer sends
until it ends: in `PRESYNC` the headers are counted and, every
`commitment_period` headers from a secret offset, one salted bit of a
header's hash is kept; once the work clears the threshold, `REDOWNLOAD`
asks for the same chain again from its start, checks each kept bit
against it, and hands headers back for indexing only once
`redownload_buffer_size` more have been checked behind them, or once the
redownloaded chain clears the threshold itself. Nothing of the chain is
indexed before that, so a chain that never clears it costs this node
the bits and the buffer and nothing on disk.

What differs from Core is the language's and is said where it is: the
buffer, the bit queue, the randomness, the result and the end of an
object, each beside the code it shapes.

Every object here is reached from `Node`'s thread alone -- `headers`
writes `Connection.headers_sync`, and `getpeerinfo`, which reads it,
runs on the same loop -- so none takes the lock Core's
`m_headers_sync_mutex` is: ARCHITECTURE.md's *The protocol and the RPC
surface* is that argument.
"""

from __future__ import annotations

import enum
import secrets
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from btclib.block import BlockHeader, median_time_past
from btclib.block.limits import MAX_FUTURE_BLOCK_TIME
from btclib.block.proof_of_work import permitted_difficulty_transition
from btclib.hashes import siphash

from btclib_node.chainstate.block_index import calculate_work

if TYPE_CHECKING:
    from collections.abc import Sequence

    from btclib.consensus import ConsensusParams

    from btclib_node.chains import HeadersSyncParams
    from btclib_node.chainstate.block_index import BlockIndex

__all__ = [
    "ChainStart",
    "HeadersSyncState",
    "ProcessingResult",
    "State",
    "anti_dos_work_threshold",
]

# Core's own buffer below the tip, in blocks, inside
# `GetAntiDoSWorkThreshold`: a chain forking this near the tip is weighed
# against the tip's work less this many of its blocks
_NEAR_TIP_BLOCKS = 144

# How many headers a second the median-time-past rule lets a chain carry
# at most, which bounds how long a chain can be at any moment: Core's
# literal 6 in the `HeadersSyncState` constructor
_MAX_BLOCKS_PER_SECOND = 6

# Core's `CompressedHeader` is 48 bytes: the serialized header less the
# 32 bytes of its parent's hash, which the buffer holds once for the
# first header and derives for every later one
_VERSION_SIZE = 4
_PREVIOUS_HASH_END = _VERSION_SIZE + 32
_COMPRESSED_BITS = slice(40, 44)


def anti_dos_work_threshold(block_index: BlockIndex, minimum_chain_work: int) -> int:
    """Return the work below which a chain is not indexed unseen.

    Core's `GetAntiDoSWorkThreshold` (`src/net_processing.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): the active tip's work
    less 144 blocks of the tip's own work, and never less than
    `minimum_chain_work`, which callers pass as `Config.minimum_chain_work`
    (`-minimumchainwork`), Core's `MinimumChainWork`. The active chain
    always holds genesis here, where Core allows for no tip at all.
    """
    tip = block_index.active_chain[-1]
    tip_work = block_index.chainwork[tip]
    tip_proof = calculate_work(block_index.get_block_info(tip).header)
    near_tip_work = tip_work - min(_NEAR_TIP_BLOCKS * tip_proof, tip_work)
    return max(near_tip_work, minimum_chain_work)


class State(enum.Enum):
    """Core's `HeadersSyncState::State`.

    `PRESYNC` counts the peer's chain and keeps commitments to it,
    `REDOWNLOAD` downloads it again and checks them, and `FINAL` is an
    ended sync, whose object is dropped.
    """

    PRESYNC = enum.auto()
    REDOWNLOAD = enum.auto()
    FINAL = enum.auto()


@dataclass(frozen=True)
class ChainStart:
    """The indexed block a low-work chain builds on: Core's `m_chain_start`.

    Core keeps a reference to the `CBlockIndex` and reads what it needs off
    it; this carries those same values, read off `BlockIndex` once by
    `from_index`: the header, its height and work, its median time past,
    and the locator Core rebuilds from it on every request, which is the
    same list each time.
    """

    header: BlockHeader
    height: int
    chainwork: int
    median_time_past: int
    locator: tuple[bytes, ...]

    @classmethod
    def from_index(cls, block_index: BlockIndex, block_hash: bytes) -> ChainStart:
        """Read the chain start `block_hash` names off `block_index`."""
        block_info = block_index.get_block_info(block_hash)

        def parent_of(header: BlockHeader) -> BlockHeader:
            return block_index.get_block_info(header.previous_block_hash).header

        return cls(
            header=block_info.header,
            height=block_info.index,
            chainwork=block_index.chainwork[block_hash],
            median_time_past=median_time_past(
                block_info.header, block_info.index, parent_of
            ),
            locator=tuple(block_index.locator_entries(block_hash)),
        )


class ProcessingResult(NamedTuple):
    """Core's `HeadersSyncState::ProcessingResult`.

    Returned rather than swapped into the caller's own list, which is
    what Core's caller does with it.
    """

    pow_validated_headers: list[BlockHeader]
    success: bool
    request_more: bool


class _BitQueue:
    """Core's `bitdeque`, as far as `HeadersSyncState` uses it.

    One bit per commitment, packed eight to a byte: a list of `bool`
    would take a pointer per bit, and mainnet's bound on commitments per
    peer runs to millions. Bits are appended during `PRESYNC` and taken
    from the front during `REDOWNLOAD`, never both, so what has been
    taken is not reclaimed until the queue is dropped.
    """

    def __init__(self) -> None:
        self._octets = bytearray()
        self._head = 0
        self._tail = 0

    def __len__(self) -> int:
        return self._tail - self._head

    def append(self, bit: int) -> None:
        index, offset = divmod(self._tail, 8)
        if not offset:
            self._octets.append(0)
        self._octets[index] |= bit << offset
        self._tail += 1

    def popleft(self) -> int:
        index, offset = divmod(self._head, 8)
        self._head += 1
        return (self._octets[index] >> offset) & 1


def _compress(header: BlockHeader) -> bytes:
    """Return Core's `CompressedHeader`: `header` less its parent's hash."""
    raw = header.serialize(check_validity=False)
    return raw[:_VERSION_SIZE] + raw[_PREVIOUS_HASH_END:]


def _full_header(compressed: bytes, previous_block_hash: bytes) -> BlockHeader:
    """Return Core's `GetFullHeader`: `_compress`'s header, parent put back."""
    raw = compressed[:_VERSION_SIZE] + previous_block_hash[::-1]
    raw += compressed[_VERSION_SIZE:]
    return BlockHeader.parse(raw, check_validity=False)


class HeadersSyncState:
    """One peer's low-work headers sync: Core's `HeadersSyncState`."""

    def __init__(  # noqa: PLR0913
        self,
        consensus: ConsensusParams,
        params: HeadersSyncParams,
        chain_start: ChainStart,
        minimum_required_work: int,
        *,
        now: float | None = None,
        commit_offset: int | None = None,
        salt: tuple[int, int] | None = None,
    ) -> None:
        """Start a sync from `chain_start`, towards `minimum_required_work`.

        `minimum_required_work` is the threshold the peer's chain has to
        clear, fixed when the sync starts. `now`, `commit_offset` and
        `salt` are Core's clock, its `randrange` and its
        `SaltedUint256Hasher`'s two keys, drawn from `time` and `secrets`
        unless a test passes them: the offset and the salt are what a
        peer must not learn, and `secrets` is this tree's source for what
        must not be guessed.
        """
        self.consensus = consensus
        self.params = params
        self.chain_start = chain_start
        self.minimum_required_work = minimum_required_work
        period = params.commitment_period
        self.commit_offset = (
            secrets.randbelow(period) if commit_offset is None else commit_offset
        )
        self._salt = (
            (secrets.randbits(64), secrets.randbits(64)) if salt is None else salt
        )
        now = time.time() if now is None else now
        # the longest chain the median-time-past rule allows between the
        # chain start and now, at six blocks a second, in commitments:
        # a peer offering more is offering what cannot be a valid chain
        max_seconds_since_start = (
            int(now) - chain_start.median_time_past + MAX_FUTURE_BLOCK_TIME
        )
        self._max_commitments = (
            _MAX_BLOCKS_PER_SECOND * max_seconds_since_start // period
        )
        self.state = State.PRESYNC
        self._commitments = _BitQueue()
        # PRESYNC: the work, the last header and its height so far
        self._current_chain_work = self.chain_start.chainwork
        self._last_header_received = self.chain_start.header
        self._last_header_received_hash = self.chain_start.header.hash
        self._current_height = self.chain_start.height
        # REDOWNLOAD: set as it begins
        self._redownloaded_headers: deque[bytes] = deque()
        self._redownload_buffer_last_height = 0
        self._redownload_buffer_last_hash = b""
        self._redownload_buffer_first_prev_hash = b""
        self._redownload_chain_work = 0
        self._process_all_remaining_headers = False

    @property
    def presync_height(self) -> int:
        """Core's `GetPresyncHeight`: the height `PRESYNC` has reached."""
        return self._current_height

    @property
    def presync_time(self) -> int:
        """Core's `GetPresyncTime`: the last `PRESYNC` header's timestamp."""
        return int(self._last_header_received.time.timestamp())

    @property
    def presync_work(self) -> int:
        """Core's `GetPresyncWork`: the work `PRESYNC` has counted."""
        return self._current_chain_work

    def _commitment(self, header_hash: bytes) -> int:
        # Core's `SaltedUint256Hasher` hashes the `uint256` in its own
        # byte order, the reverse of the one `BlockHeader.hash` displays
        k0, k1 = self._salt
        return siphash(k0, k1, header_hash[::-1]) & 1

    def _finalize(self) -> None:
        """Core's `Finalize`: free what the sync holds, and end it.

        Core also nulls the object's own hashes so it cannot be reused
        with the same salt; the caller dropping it does that here.
        """
        self._commitments = _BitQueue()
        self._redownloaded_headers = deque()
        self._process_all_remaining_headers = False
        self._current_height = 0
        self.state = State.FINAL

    def process_next_headers(
        self, headers: Sequence[BlockHeader], *, full_headers_message: bool
    ) -> ProcessingResult:
        """Take the peer's next batch: Core's `ProcessNextHeaders`.

        The caller has checked each header's own proof of work and that
        the batch is continuous. `full_headers_message` is whether the
        batch held `MAX_HEADERS_RESULTS` headers, the peer then maybe
        having more. The result says whether the batch was taken,
        whether to ask for more with `next_headers_request_locator`, and
        which headers are ready to be indexed; the sync ends, `FINAL`,
        wherever it is not both taken and asking for more.

        An empty batch, or one given to an ended sync, is refused without
        ending anything: Core's `Assume` guards, which no caller here
        reaches.
        """
        # read once: the PRESYNC branch below moves `self.state` itself
        state = self.state
        if not headers or state is State.FINAL:
            return ProcessingResult([], success=False, request_more=False)
        validated: list[BlockHeader] = []
        request_more = False
        if state is State.PRESYNC:
            success = self._validate_and_store_headers_commitments(headers)
            # a full batch may have more behind it, and a sync just moved
            # to REDOWNLOAD asks again from the start whatever the size;
            # a short batch still in PRESYNC is a chain that ended short
            # of the work, and the sync ends without an error
            request_more = success and (
                full_headers_message or self.state is State.REDOWNLOAD
            )
        else:
            success = all(
                self._validate_and_store_redownloaded_header(header)
                for header in headers
            )
            if success:
                validated = self._pop_headers_ready_for_acceptance()
                # every header released at the target is the end of the
                # sync; before it, a short batch is a peer declining to
                # serve again the chain it served once, and ends it too
                request_more = full_headers_message and not (
                    self._process_all_remaining_headers
                    and not self._redownloaded_headers
                )
        if not (success and request_more):
            self._finalize()
        return ProcessingResult(validated, success=success, request_more=request_more)

    def _validate_and_store_headers_commitments(
        self, headers: Sequence[BlockHeader]
    ) -> bool:
        """Core's `ValidateAndStoreHeadersCommitments`, in `PRESYNC`.

        Core's `Assume` guards on an empty batch and on the state are
        not carried: `process_next_headers` is the one caller, and has
        already answered both.
        """
        if headers[0].previous_block_hash != self._last_header_received_hash:
            return False
        if not all(self._validate_and_process_single_header(h) for h in headers):
            return False
        if self._current_chain_work >= self.minimum_required_work:
            start = self.chain_start
            self._redownloaded_headers = deque()
            self._redownload_buffer_last_height = start.height
            self._redownload_buffer_first_prev_hash = start.header.hash
            self._redownload_buffer_last_hash = start.header.hash
            self._redownload_chain_work = start.chainwork
            self.state = State.REDOWNLOAD
        return True

    def _validate_and_process_single_header(self, header: BlockHeader) -> bool:
        """Core's `ValidateAndProcessSingleHeader`, one `PRESYNC` header."""
        next_height = self._current_height + 1
        if not permitted_difficulty_transition(
            self.consensus, next_height, self._last_header_received.bits, header.bits
        ):
            return False
        header_hash = header.hash
        if next_height % self.params.commitment_period == self.commit_offset:
            self._commitments.append(self._commitment(header_hash))
            if len(self._commitments) > self._max_commitments:
                return False
        self._current_chain_work += calculate_work(header)
        self._last_header_received = header
        self._last_header_received_hash = header_hash
        self._current_height = next_height
        return True

    def _validate_and_store_redownloaded_header(self, header: BlockHeader) -> bool:
        """Core's `ValidateAndStoreRedownloadedHeader`, in `REDOWNLOAD`."""
        next_height = self._redownload_buffer_last_height + 1
        if header.previous_block_hash != self._redownload_buffer_last_hash:
            return False
        if self._redownloaded_headers:
            previous_bits = self._redownloaded_headers[-1][_COMPRESSED_BITS][::-1]
        else:
            previous_bits = self.chain_start.header.bits
        if not permitted_difficulty_transition(
            self.consensus, next_height, previous_bits, header.bits
        ):
            return False
        self._redownload_chain_work += calculate_work(header)
        # set before the commitment is asked, so that the header reaching
        # the target is not checked: a peer whose chain grew since PRESYNC
        # runs out of commitments past that point, and is not refused
        if self._redownload_chain_work >= self.minimum_required_work:
            self._process_all_remaining_headers = True
        header_hash = header.hash
        if (
            not self._process_all_remaining_headers
            and next_height % self.params.commitment_period == self.commit_offset
        ):
            if not self._commitments:
                return False
            if self._commitment(header_hash) != self._commitments.popleft():
                return False
        self._redownloaded_headers.append(_compress(header))
        self._redownload_buffer_last_height = next_height
        self._redownload_buffer_last_hash = header_hash
        return True

    def _pop_headers_ready_for_acceptance(self) -> list[BlockHeader]:
        """Core's `PopHeadersReadyForAcceptance`, the headers checked enough."""
        ready: list[BlockHeader] = []
        buffer = self._redownloaded_headers
        while len(buffer) > self.params.redownload_buffer_size or (
            buffer and self._process_all_remaining_headers
        ):
            header = _full_header(
                buffer.popleft(), self._redownload_buffer_first_prev_hash
            )
            ready.append(header)
            self._redownload_buffer_first_prev_hash = header.hash
        return ready

    def next_headers_request_locator(self) -> list[bytes]:
        """Return the locator to ask for the next batch: Core's own.

        The last header `PRESYNC` took, or the last one `REDOWNLOAD`
        buffered, then the chain start's own locator. Empty for an ended
        sync, Core's `Assume` guard, which no caller here reaches.
        """
        if self.state is State.FINAL:
            return []
        if self.state is State.PRESYNC:
            head = self._last_header_received_hash
        else:
            head = self._redownload_buffer_last_hash
        return [head, *self.chain_start.locator]
