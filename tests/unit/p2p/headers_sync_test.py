# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `p2p.headers_sync`, Core's `HeadersSyncState` (ISS 1246).

`HeadersSyncState` checks no header's own proof of work -- its caller
does -- so the chains here are built unsolved, which is what lets them
be as long as Core's own test chains.
"""

import tracemalloc
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from btclib.block import BlockHeader
from btclib.consensus import CONSENSUS_PARAMS
from btclib.exceptions import BTClibValueError
from btclib.hashes import siphash
from btclib.p2p.limits import MAX_HEADERS_RESULTS

from btclib_node.chains import HeadersSyncParams, RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate.block_index import calculate_work
from btclib_node.log import Logger
from btclib_node.p2p.headers_sync import (
    ChainStart,
    HeadersSyncState,
    ProcessingResult,
    State,
    _BitQueue,
    _compress,
    _full_header,
    anti_dos_work_threshold,
    permitted_difficulty_transition,
)
from tests import generate_random_header_chain

if TYPE_CHECKING:
    from pathlib import Path

_GENESIS = RegTest().genesis
_WORK = calculate_work(_GENESIS)  # every regtest header's own work
_START = ChainStart(_GENESIS, 0, _WORK, 0, (_GENESIS.hash,))
_SALT = (0x0706050403020100, 0x0F0E0D0C0B0A0908)


def unsolved_chain(
    count: int,
    start: BlockHeader = _GENESIS,
    *,
    merkle_root: bytes = b"\x00" * 32,
    bits: bytes | None = None,
) -> list[BlockHeader]:
    """Return `count` headers off `start`, a second apart, none of them mined.

    `merkle_root` tells two chains off the same start apart, as Core's own
    test does.
    """
    chain: list[BlockHeader] = []
    previous = start
    for _ in range(count):
        header = BlockHeader(
            version=start.version,
            previous_block_hash=previous.hash,
            merkle_root=merkle_root,
            time=previous.time + timedelta(seconds=1),
            bits=start.bits if bits is None else bits,
            nonce=0,
            check_validity=False,
        )
        chain.append(header)
        previous = header
    return chain


def state_of(sync: HeadersSyncState) -> State:
    """Read `sync.state` again, which mypy would otherwise take as unchanged.

    An `assert` on the state narrows it, and a later method call moving it
    does not widen it back.
    """
    return sync.state


def a_sync(
    required_blocks: int,
    *,
    period: int = 600,
    buffer: int = 100,
    offset: int = 0,
    consensus: Any = None,
    now: float = 10**9,
    salt: tuple[int, int] = _SALT,
) -> HeadersSyncState:
    """Build a sync from regtest's genesis, towards `required_blocks` of work.

    `required_blocks` counts the genesis, whose work the chain start
    carries.
    """
    return HeadersSyncState(
        RegTest().consensus if consensus is None else consensus,
        HeadersSyncParams(commitment_period=period, redownload_buffer_size=buffer),
        _START,
        required_blocks * _WORK,
        now=now,
        commit_offset=offset,
        salt=salt,
    )


def check(
    result: ProcessingResult,
    sync: HeadersSyncState,
    state: State,
    *,
    success: bool,
    request_more: bool,
    released: int,
    first_released_prev: bytes | None,
    locator_head: bytes | None,
) -> None:
    """Core's `CHECK_RESULT`, argument for argument."""
    assert sync.state is state
    assert result.success is success
    assert result.request_more is request_more
    assert len(result.pow_validated_headers) == released
    if first_released_prev is None:
        assert released == 0
    else:
        assert result.pow_validated_headers[0].previous_block_hash == (
            first_released_prev
        )
    if locator_head is None:
        assert state is State.FINAL
    else:
        assert sync.next_headers_request_locator()[0] == locator_head


# Core's own `headers_sync_chainwork_tests`
# (`src/test/headers_sync_chainwork_tests.cpp`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), constant for constant
_TARGET_BLOCKS = 15_000
_REDOWNLOAD_BUFFER_SIZE = _TARGET_BLOCKS - (MAX_HEADERS_RESULTS + 123)
_COMMITMENT_PERIOD = 600


@pytest.fixture(scope="module")
def first_chain() -> list[BlockHeader]:
    """Core's `FirstChain`: the work Core's test asks for, genesis in."""
    return unsolved_chain(_TARGET_BLOCKS - 1)


@pytest.fixture(scope="module")
def second_chain() -> list[BlockHeader]:
    """Core's `SecondChain`: another root, one header short of the work."""
    return unsolved_chain(_TARGET_BLOCKS - 2, merkle_root=b"\x01" + b"\x00" * 31)


def core_s_state(offset: int = 0) -> HeadersSyncState:
    """Core's `CreateState`, the offset and the salt fixed."""
    return a_sync(
        _TARGET_BLOCKS,
        period=_COMMITMENT_PERIOD,
        buffer=_REDOWNLOAD_BUFFER_SIZE,
        offset=offset,
    )


def test_sneaky_redownload(
    first_chain: list[BlockHeader], second_chain: list[BlockHeader]
) -> None:
    """Core's `sneaky_redownload`: another chain in REDOWNLOAD is caught.

    The salt is fixed here, so the one chance in 2^25 that every
    commitment matches by accident, which Core's test runs, is not run.
    """
    sync = core_s_state()
    check(
        sync.process_next_headers(first_chain[:1], full_headers_message=True),
        sync,
        State.PRESYNC,
        success=True,
        request_more=True,
        released=0,
        first_released_prev=None,
        locator_head=first_chain[0].hash,
    )
    check(
        sync.process_next_headers(first_chain[1:], full_headers_message=True),
        sync,
        State.REDOWNLOAD,
        success=True,
        request_more=True,
        released=0,
        first_released_prev=None,
        locator_head=_GENESIS.hash,
    )
    check(
        sync.process_next_headers(second_chain, full_headers_message=True),
        sync,
        State.FINAL,
        success=False,
        request_more=False,
        released=0,
        first_released_prev=None,
        locator_head=None,
    )


@pytest.mark.parametrize("full_headers_message", [False, True])
def test_happy_path(
    first_chain: list[BlockHeader],
    full_headers_message: bool,  # noqa: FBT001
) -> None:
    """Core's `happy_path`: the chain redownloaded is released, all of it."""
    sync = core_s_state()
    check(
        sync.process_next_headers(
            first_chain, full_headers_message=full_headers_message
        ),
        sync,
        State.REDOWNLOAD,
        success=True,
        request_more=True,
        released=0,
        first_released_prev=None,
        locator_head=_GENESIS.hash,
    )
    check(
        sync.process_next_headers(
            first_chain[:_REDOWNLOAD_BUFFER_SIZE], full_headers_message=True
        ),
        sync,
        State.REDOWNLOAD,
        success=True,
        request_more=True,
        released=0,
        first_released_prev=None,
        locator_head=first_chain[_REDOWNLOAD_BUFFER_SIZE - 1].hash,
    )
    check(
        sync.process_next_headers(
            first_chain[_REDOWNLOAD_BUFFER_SIZE : _REDOWNLOAD_BUFFER_SIZE + 1],
            full_headers_message=True,
        ),
        sync,
        State.REDOWNLOAD,
        success=True,
        request_more=True,
        released=1,
        first_released_prev=_GENESIS.hash,
        locator_head=first_chain[_REDOWNLOAD_BUFFER_SIZE].hash,
    )
    result = sync.process_next_headers(
        first_chain[_REDOWNLOAD_BUFFER_SIZE + 1 :],
        full_headers_message=full_headers_message,
    )
    check(
        result,
        sync,
        State.FINAL,
        success=True,
        request_more=False,
        released=len(first_chain) - 1,
        first_released_prev=first_chain[0].hash,
        locator_head=None,
    )
    # and the headers released are the chain's own, byte for byte
    assert [h.hash for h in result.pow_validated_headers] == [
        h.hash for h in first_chain[1:]
    ]


def test_too_little_work(second_chain: list[BlockHeader]) -> None:
    """Core's `too_little_work`: a chain ending short of the work, no error."""
    sync = core_s_state()
    assert sync.state is State.PRESYNC
    check(
        sync.process_next_headers(second_chain[:1], full_headers_message=True),
        sync,
        State.PRESYNC,
        success=True,
        request_more=True,
        released=0,
        first_released_prev=None,
        locator_head=second_chain[0].hash,
    )
    check(
        sync.process_next_headers(second_chain[1:], full_headers_message=False),
        sync,
        State.FINAL,
        success=True,
        request_more=False,
        released=0,
        first_released_prev=None,
        locator_head=None,
    )


def test_the_commitment_is_core_s_siphash_of_the_uint256() -> None:
    """The bit is SipHash's over the hash in Core's own byte order.

    Core's `PresaltedSipHasher` vector (`src/test/hash_tests.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag): its `uint256` is written
    the way `BlockHeader.hash` displays a hash. Over a chain's hashes the
    bit is the one of that order, and not of the displayed one, which
    gives a different bit for some of them.
    """
    displayed = bytes.fromhex(
        "1f1e1d1c1b1a191817161514131211100f0e0d0c0b0a09080706050403020100"
    )
    assert siphash(*_SALT, displayed[::-1]) == 0x7127512F72F27CCE
    sync = a_sync(1)
    hashes = [header.hash for header in unsolved_chain(32)]
    assert [sync._commitment(h) for h in hashes] == [
        siphash(*_SALT, h[::-1]) & 1 for h in hashes
    ]
    assert any(siphash(*_SALT, h[::-1]) & 1 != siphash(*_SALT, h) & 1 for h in hashes)


def bounded(now: int, median_time_past: int = 0) -> HeadersSyncState:
    """Return a sync committing every sixth header, at `now`.

    At a period of six, Core's bound of six blocks a second since the
    start, plus `MAX_FUTURE_BLOCK_TIME`, is one commitment a second.
    """
    return HeadersSyncState(
        RegTest().consensus,
        HeadersSyncParams(commitment_period=6, redownload_buffer_size=10),
        replace(_START, median_time_past=median_time_past),
        1000 * _WORK,
        now=now,
        commit_offset=0,
        salt=_SALT,
    )


@pytest.mark.parametrize(("now", "taken"), [(-7189, True), (-7190, False)])
def test_the_bound_on_commitments_is_six_blocks_a_second_since_the_start(
    now: int,
    taken: bool,  # noqa: FBT001
) -> None:
    """Core's `m_max_commitments`, the future-time allowance included.

    Sixty-six headers commit eleven times at a period of six: eleven
    seconds' worth of chain is enough, ten is one commitment past the
    bound, and the sync ends there.
    """
    sync = bounded(now)
    result = sync.process_next_headers(unsolved_chain(66), full_headers_message=True)
    assert result.success is taken
    assert (sync.state is State.FINAL) is not taken


@pytest.mark.parametrize(("now", "taken"), [(-7189, True), (-7190, False)])
def test_the_bound_counts_from_the_chain_start_s_median_time_past(
    now: int,
    taken: bool,  # noqa: FBT001
) -> None:
    """The start's median time past, not the clock alone, bounds the chain."""
    late = 10**9
    sync = bounded(late + now, median_time_past=late)
    result = sync.process_next_headers(unsolved_chain(66), full_headers_message=True)
    assert result.success is taken


def test_the_offset_and_the_salt_are_drawn_where_not_given() -> None:
    """Unset, the offset falls in the period and the salt is two 64-bit keys.

    Across 64 syncs, every key is below 2^64 and some reaches 2^63: a key
    of any other width fails one or the other with a chance of 2^-64.
    """
    keys = []
    for _ in range(64):
        sync = HeadersSyncState(
            RegTest().consensus, HeadersSyncParams(7, 10), _START, 0
        )
        assert 0 <= sync.commit_offset < 7
        keys.append(sync._salt)
    for column in zip(*keys, strict=True):
        assert all(0 <= key < 2**64 for key in column)
        assert any(key >= 2**63 for key in column)


def test_the_clock_the_offset_and_the_salt_are_keywords_alone() -> None:
    """What a test injects cannot be passed by position by mistake."""
    with pytest.raises(TypeError):
        HeadersSyncState(  # type: ignore[call-arg]
            RegTest().consensus, HeadersSyncParams(7, 10), _START, 0, 10**9
        )
    with pytest.raises(TypeError):
        a_sync(1).process_next_headers(unsolved_chain(1), True)  # type: ignore[call-arg]  # noqa: FBT003


def test_a_full_presync_batch_asks_on_from_its_last_header() -> None:
    """PRESYNC asks from the last header taken, then the start's own locator."""
    start_locator = (b"\x0a" * 32, _GENESIS.hash)
    sync = HeadersSyncState(
        RegTest().consensus,
        HeadersSyncParams(600, 100),
        replace(_START, locator=start_locator),
        100 * _WORK,
        now=10**9,
    )
    chain = unsolved_chain(3)
    check(
        sync.process_next_headers(chain, full_headers_message=True),
        sync,
        State.PRESYNC,
        success=True,
        request_more=True,
        released=0,
        first_released_prev=None,
        locator_head=chain[-1].hash,
    )
    assert sync.next_headers_request_locator() == [chain[-1].hash, *start_locator]
    assert sync.presync_height == 3
    assert sync.presync_work == 4 * _WORK
    assert sync.presync_time == int(chain[-1].time.timestamp())


def test_a_short_presync_batch_ends_the_sync_without_an_error() -> None:
    """A short batch below the work ends it: taken, nothing released."""
    sync = a_sync(100)
    check(
        sync.process_next_headers(unsolved_chain(3), full_headers_message=False),
        sync,
        State.FINAL,
        success=True,
        request_more=False,
        released=0,
        first_released_prev=None,
        locator_head=None,
    )


def off(parent: bytes) -> BlockHeader:
    """Return a header on `parent`, a hash this index need not know."""
    return BlockHeader(
        version=1,
        previous_block_hash=parent,
        merkle_root=b"\x00" * 32,
        time=_GENESIS.time + timedelta(seconds=1),
        bits=_GENESIS.bits,
        nonce=0,
        check_validity=False,
    )


_BELOW, _ABOVE = b"\x00" * 32, b"\xff" * 32


@pytest.mark.parametrize("parent", [_BELOW, _ABOVE], ids=["below", "above"])
def test_a_presync_batch_not_building_on_the_last_header_ends_the_sync(
    parent: bytes,
) -> None:
    """PRESYNC's own continuity, against the last header it took."""
    sync = a_sync(100)
    assert sync.process_next_headers(
        unsolved_chain(2), full_headers_message=True
    ).success
    result = sync.process_next_headers([off(parent)], full_headers_message=True)
    assert result == ProcessingResult([], success=False, request_more=False)
    assert sync.state is State.FINAL
    # Core's `Finalize` zeroes the height, as its getter then reports
    assert sync.presync_height == 0


def _no_min_difficulty() -> Any:
    """Regtest's consensus, told to check difficulty transitions."""
    return replace(RegTest().consensus, pow_allow_min_difficulty_blocks=False)


# A start at mainnet's own starting target, since four times regtest's
# overflows the 256 bits Core's retarget arithmetic wraps at, and a
# target one step harder, which a retarget may move to and nothing else
_HARD = BlockHeader(
    version=1,
    previous_block_hash=b"\x00" * 32,
    merkle_root=b"\x00" * 32,
    time=_GENESIS.time,
    bits=bytes.fromhex("1d00ffff"),
    nonce=0,
    check_validity=False,
)
_HARDER = bytes.fromhex("1d00fffe")
_HARDEST = bytes.fromhex("1d00fffd")


def at_143() -> HeadersSyncState:
    """Return a sync checking transitions, from `_HARD` at regtest's 143."""
    work = calculate_work(_HARD)
    return HeadersSyncState(
        _no_min_difficulty(),
        HeadersSyncParams(600, 100),
        ChainStart(_HARD, 143, work, 0, (_HARD.hash,)),
        3 * work,
        now=10**9,
    )


def test_a_presync_header_changing_its_bits_off_a_retarget_ends_the_sync() -> None:
    """PRESYNC asks `permitted_difficulty_transition` of every header."""
    chain = unsolved_chain(2)
    chain += unsolved_chain(1, chain[-1], bits=bytes.fromhex("207ffffe"))
    assert a_sync(100).process_next_headers(chain, full_headers_message=True).success
    strict = a_sync(100, consensus=_no_min_difficulty())
    result = strict.process_next_headers(chain, full_headers_message=True)
    assert result.success is False
    assert strict.state is State.FINAL
    # the transition is asked of the header's own height: regtest retargets
    # every 144, so a sync starting at 143 takes the change at 144
    changed = unsolved_chain(1, _HARD, bits=_HARDER)
    assert at_143().process_next_headers(changed, full_headers_message=True).success


def test_the_work_reaching_the_threshold_exactly_moves_to_redownload() -> None:
    """At the threshold, not above it, and even from a short batch.

    A short batch reaching the work still asks for more: the chain is
    wanted again from its start.
    """
    below = a_sync(5)
    result = below.process_next_headers(unsolved_chain(3), full_headers_message=True)
    assert below.state is State.PRESYNC
    assert result.request_more
    exact = a_sync(5)
    result = exact.process_next_headers(unsolved_chain(4), full_headers_message=False)
    assert exact.state is State.REDOWNLOAD
    assert result == ProcessingResult([], success=True, request_more=True)
    assert exact.next_headers_request_locator() == [_GENESIS.hash, _GENESIS.hash]


def redownloading(
    required_blocks: int, chain: list[BlockHeader], **kwargs: Any
) -> HeadersSyncState:
    """Return a sync that took `chain` in PRESYNC and now redownloads it."""
    sync = a_sync(required_blocks, **kwargs)
    assert sync.process_next_headers(chain, full_headers_message=True).success
    assert sync.state is State.REDOWNLOAD
    return sync


def test_redownload_holds_the_buffer_and_releases_past_it() -> None:
    """Held while the buffer is not over its size, released one past it.

    The first released header is rebuilt on the chain start, and each
    later one on the one before it.
    """
    chain = unsolved_chain(20)
    sync = redownloading(21, chain, buffer=5)
    check(
        sync.process_next_headers(chain[:5], full_headers_message=True),
        sync,
        State.REDOWNLOAD,
        success=True,
        request_more=True,
        released=0,
        first_released_prev=None,
        locator_head=chain[4].hash,
    )
    result = sync.process_next_headers(chain[5:7], full_headers_message=True)
    check(
        result,
        sync,
        State.REDOWNLOAD,
        success=True,
        request_more=True,
        released=2,
        first_released_prev=_GENESIS.hash,
        locator_head=chain[6].hash,
    )
    assert [h.hash for h in result.pow_validated_headers] == [
        chain[0].hash,
        chain[1].hash,
    ]
    assert sync.next_headers_request_locator() == [chain[6].hash, _GENESIS.hash]
    # the height PRESYNC reached is what is reported while redownloading
    assert sync.presync_height == 20


def test_reaching_the_target_releases_every_header_held() -> None:
    """The header reaching the work releases the whole buffer and ends it."""
    chain = unsolved_chain(20)
    sync = redownloading(21, chain, buffer=100)
    assert not sync.process_next_headers(
        chain[:19], full_headers_message=True
    ).pow_validated_headers
    result = sync.process_next_headers(chain[19:], full_headers_message=True)
    check(
        result,
        sync,
        State.FINAL,
        success=True,
        request_more=False,
        released=20,
        first_released_prev=_GENESIS.hash,
        locator_head=None,
    )


def test_a_header_past_the_target_is_released_with_it() -> None:
    """A chain grown since PRESYNC runs out of commitments past the target.

    The target is reached at the tenth header; the commitments cover what
    PRESYNC saw and no more, and none is asked from the target on.
    """
    chain = unsolved_chain(30)
    sync = redownloading(11, chain[:10], period=2, offset=1)
    result = sync.process_next_headers(chain, full_headers_message=False)
    check(
        result,
        sync,
        State.FINAL,
        success=True,
        request_more=False,
        released=30,
        first_released_prev=_GENESIS.hash,
        locator_head=None,
    )


def differing_salt(first: BlockHeader, second: BlockHeader) -> tuple[int, int]:
    """Return a salt under which the two headers' commitment bits differ."""
    return next(
        (k, k)
        for k in range(100)
        if siphash(k, k, first.hash[::-1]) & 1 != siphash(k, k, second.hash[::-1]) & 1
    )


def test_the_target_header_itself_is_not_checked_against_a_commitment() -> None:
    """Core sets the flag before the commitment is asked, at the same header.

    PRESYNC reaches the work at the tenth header, which is also its one
    commitment at a period of ten. The redownload serves another tenth
    header, whose bit differs: it reaches the target, so its commitment is
    never asked, and the sync ends with it released.
    """
    chain = unsolved_chain(10)
    (other,) = unsolved_chain(1, chain[8], merkle_root=b"\x01" * 32)
    salt = differing_salt(chain[9], other)
    sync = redownloading(11, chain, period=10, offset=0, salt=salt)
    result = sync.process_next_headers([*chain[:9], other], full_headers_message=True)
    assert result.success
    assert [h.hash for h in result.pow_validated_headers] == [
        *(h.hash for h in chain[:9]),
        other.hash,
    ]


def a_bit_apart(first: BlockHeader, second: BlockHeader, bit: int) -> tuple[int, int]:
    """Return a salt committing `first` to `bit` and `second` to the other."""
    return next(
        (k, k)
        for k in range(200)
        if siphash(k, k, first.hash[::-1]) & 1 == bit
        and siphash(k, k, second.hash[::-1]) & 1 != bit
    )


@pytest.mark.parametrize("kept", [0, 1])
def test_another_chain_in_redownload_is_a_commitment_mismatch(kept: int) -> None:
    """A redownloaded header whose bit is not the one kept ends the sync.

    Every header commits at a period of one; the fifth header of another
    chain, the last of its batch, carries the other bit, whichever of the
    two PRESYNC kept, after four that match.
    """
    chain = unsolved_chain(10)
    (other,) = unsolved_chain(1, chain[3], merkle_root=b"\x01" * 32)
    salt = a_bit_apart(chain[4], other, kept)
    sync = redownloading(11, chain, period=1, offset=0, salt=salt)
    result = sync.process_next_headers([*chain[:4], other], full_headers_message=True)
    assert result == ProcessingResult([], success=False, request_more=False)
    assert sync.state is State.FINAL


def test_a_commitment_asked_with_none_left_is_an_overrun() -> None:
    """More committed heights in REDOWNLOAD than PRESYNC kept ends the sync.

    PRESYNC reaches the work in a chain whose commitments this test then
    empties; the first committed height of the redownload, the last of
    its batch, finds none.
    """
    chain = unsolved_chain(10)
    sync = redownloading(11, chain, period=1, offset=0)
    sync._commitments = _BitQueue()
    result = sync.process_next_headers(chain[:1], full_headers_message=True)
    assert result.success is False
    assert sync.state is State.FINAL


@pytest.mark.parametrize("parent", [_BELOW, _ABOVE], ids=["below", "above"])
def test_a_redownloaded_header_not_building_on_the_buffer_ends_the_sync(
    parent: bytes,
) -> None:
    """REDOWNLOAD's own continuity, against the last header buffered."""
    chain = unsolved_chain(10)
    sync = redownloading(11, chain)
    assert sync.process_next_headers(chain[:3], full_headers_message=True).success
    result = sync.process_next_headers([off(parent)], full_headers_message=True)
    assert result.success is False
    assert sync.state is State.FINAL


def test_a_redownload_crossing_the_threshold_without_meeting_it_releases_all() -> None:
    """The target is reached at or past the work, whichever lands first.

    An odd threshold, which regtest's work of two a block never lands on.
    """
    chain = unsolved_chain(5)
    sync = HeadersSyncState(
        RegTest().consensus,
        HeadersSyncParams(600, 100),
        _START,
        5 * _WORK + 1,
        now=10**9,
    )
    assert sync.process_next_headers(chain, full_headers_message=True).success
    assert sync.state is State.REDOWNLOAD
    result = sync.process_next_headers(chain, full_headers_message=True)
    assert len(result.pow_validated_headers) == 5
    assert state_of(sync) is State.FINAL


def test_an_empty_buffer_before_the_target_still_asks_for_more() -> None:
    """Core ends the sync on an empty buffer only once the target is reached.

    A buffer of none releases every header as it is checked, the target
    still ahead.
    """
    chain = unsolved_chain(10)
    sync = redownloading(11, chain, buffer=0)
    result = sync.process_next_headers(chain[:3], full_headers_message=True)
    assert len(result.pow_validated_headers) == 3
    assert result.request_more
    assert sync.state is State.REDOWNLOAD


def test_a_redownloaded_header_changing_its_bits_ends_the_sync() -> None:
    """REDOWNLOAD asks the transition of every header, as PRESYNC does."""
    chain = unsolved_chain(10)
    sync = redownloading(11, chain, consensus=_no_min_difficulty())
    assert sync.process_next_headers(chain[:2], full_headers_message=True).success
    changed = unsolved_chain(1, chain[1], bits=bytes.fromhex("207ffffe"))
    assert not sync.process_next_headers(changed, full_headers_message=True).success
    assert sync.state is State.FINAL


def test_the_first_redownloaded_header_is_weighed_against_the_chain_start() -> None:
    """An empty buffer takes the chain start's own bits as the previous ones."""
    chain = unsolved_chain(10)
    sync = redownloading(11, chain, consensus=_no_min_difficulty())
    changed = unsolved_chain(1, bits=bytes.fromhex("207ffffe"))
    assert not sync.process_next_headers(changed, full_headers_message=True).success


def test_a_later_redownloaded_header_is_weighed_against_the_buffer() -> None:
    """Past the first, the previous bits are the buffer's last header's.

    Regtest retargets every 144 blocks. From a start at 143, the header
    at 144 changes its bits, as a retarget may, and so does the one at
    288; the one at 289 keeps them. Weighed against the start's own bits,
    or against the buffer's first header, it would be refused.
    """
    chain = unsolved_chain(1, _HARD, bits=_HARDER)
    chain += unsolved_chain(144, chain[-1], bits=_HARDER)[:143]
    chain += unsolved_chain(2, chain[-1], bits=_HARDEST)
    sync = at_143()
    sync.minimum_required_work = sum(calculate_work(h) for h in [_HARD, *chain])
    assert sync.process_next_headers(chain, full_headers_message=True).success
    assert sync.state is State.REDOWNLOAD
    assert sync.process_next_headers(chain, full_headers_message=True).success


def test_a_short_redownload_batch_before_the_target_ends_the_sync() -> None:
    """A peer declining to serve again what it served once is given up on.

    Taken, and what the buffer released is still released.
    """
    chain = unsolved_chain(20)
    sync = redownloading(21, chain, buffer=2)
    result = sync.process_next_headers(chain[:5], full_headers_message=False)
    check(
        result,
        sync,
        State.FINAL,
        success=True,
        request_more=False,
        released=3,
        first_released_prev=_GENESIS.hash,
        locator_head=None,
    )


def test_an_ended_sync_takes_nothing_and_asks_nothing() -> None:
    """Core's `Assume` guards, answered without ending anything further."""
    sync = a_sync(100)
    assert sync.process_next_headers([], full_headers_message=True) == (
        ProcessingResult([], success=False, request_more=False)
    )
    assert sync.state is State.PRESYNC
    sync.process_next_headers(unsolved_chain(1), full_headers_message=False)
    assert state_of(sync) is State.FINAL
    assert sync.next_headers_request_locator() == []
    assert sync.process_next_headers(unsolved_chain(1), full_headers_message=True) == (
        ProcessingResult([], success=False, request_more=False)
    )


def test_a_compressed_header_is_48_bytes_and_rebuilds_the_header() -> None:
    """Core's `CompressedHeader`, and `GetFullHeader` back to the same bytes."""
    (header,) = generate_random_header_chain(1, _GENESIS.hash)
    compressed = _compress(header)
    assert len(compressed) == 48
    rebuilt = _full_header(compressed, header.previous_block_hash)
    assert rebuilt.serialize(check_validity=False) == header.serialize(
        check_validity=False
    )
    assert rebuilt.hash == header.hash


def test_a_header_btclib_would_refuse_survives_the_buffer() -> None:
    """Compressed and rebuilt unchecked, as Core copies the fields it holds.

    A time before genesis is btclib's own refusal, which Core leaves to
    `ContextualCheckBlockHeader`'s `time-too-old` when the header is
    indexed, so the buffer does not ask btclib either way.
    """
    early = _GENESIS.time.replace(year=2000)
    header = BlockHeader(
        version=4,
        previous_block_hash=_GENESIS.hash,
        merkle_root=b"\x00" * 32,
        time=early,
        bits=_GENESIS.bits,
        nonce=0,
        check_validity=False,
    )
    with pytest.raises(BTClibValueError, match="before genesis"):
        header.assert_valid()
    rebuilt = _full_header(_compress(header), header.previous_block_hash)
    assert rebuilt.time == early
    assert rebuilt.hash == header.hash


def test_the_bit_queue_gives_back_what_it_took_in_order() -> None:
    """Packed eight to an octet, across octet boundaries, first in first out."""
    bits = [1, 0, 1, 1, 0, 0, 0, 1, 1, 0, 1]
    queue = _BitQueue()
    for bit in bits:
        queue.append(bit)
    assert len(queue) == len(bits)
    assert len(queue._octets) == 2
    assert [queue.popleft() for _ in range(4)] == bits[:4]
    assert len(queue) == len(bits) - 4
    assert [queue.popleft() for _ in bits[4:]] == bits[4:]
    assert not queue


def test_the_redownload_buffer_holds_headers_compressed() -> None:
    """What a full buffer costs is what 48-byte headers cost, not a header's.

    Measured with `tracemalloc` around the buffering of a redownload held
    short of both the buffer's size and the target: a `BlockHeader` per
    entry would cost several times the bound here.
    """
    buffered = 2_000
    chain = unsolved_chain(buffered + 10)
    sync = redownloading(buffered + 11, chain, buffer=buffered)
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    result = sync.process_next_headers(chain[:buffered], full_headers_message=True)
    grown = tracemalloc.get_traced_memory()[0] - before
    tracemalloc.stop()
    assert not result.pow_validated_headers
    assert len(sync._redownloaded_headers) == buffered
    assert grown < 128 * buffered


def test_a_commitment_offset_past_256_is_kept_and_checked() -> None:
    """The height is compared by value: a small-int identity stops at 256.

    Kept at height 300 in PRESYNC, and checked there in REDOWNLOAD: a
    header of another chain at 300, its bit differing, is refused.
    """
    chain = unsolved_chain(310)
    (other,) = unsolved_chain(1, chain[298], merkle_root=b"\x01" * 32)
    salt = differing_salt(chain[299], other)
    sync = redownloading(311, chain, period=400, offset=300, salt=salt)
    assert len(sync._commitments) == 1
    result = sync.process_next_headers([*chain[:299], other], full_headers_message=True)
    assert result.success is False


def test_the_commitments_cost_one_bit_each() -> None:
    """A commitment per header of a long chain costs an eighth of an octet."""
    count = 8_000
    chain = unsolved_chain(count)
    sync = a_sync(count + 2, period=1, offset=0)
    assert sync.process_next_headers(chain, full_headers_message=True).success
    assert len(sync._commitments) == count
    assert len(sync._commitments._octets) == count // 8


# Core's `pow_tests` (`src/test/pow_tests.cpp`, at bitcoin/bitcoin@9be056a8a7,
# the v31.1 tag): each case's `nHeight + 1`, `nBits` and expected bits, the
# expected bits minus or plus one being the transition refused beyond it
_MAINNET = CONSENSUS_PARAMS["mainnet"]


@pytest.mark.parametrize(
    ("height", "old", "new", "refused"),
    [
        (32256, 0x1D00FFFF, 0x1D00D86A, None),
        (2016, 0x1D00FFFF, 0x1D00FFFF, None),
        (68544, 0x1C05A3F4, 0x1C0168FD, 0x1C0168FD - 1),
        (46368, 0x1C387F6F, 0x1D00E1FD, 0x1D00E1FD + 1),
    ],
    ids=["get_next_work", "pow_limit", "lower_limit", "upper_limit"],
)
def test_core_s_permitted_difficulty_transitions(
    height: int, old: int, new: int, refused: int | None
) -> None:
    """Core's vectors, and the one step past each bound Core refuses."""
    old_bits, new_bits = old.to_bytes(4, "big"), new.to_bytes(4, "big")
    assert permitted_difficulty_transition(_MAINNET, height, old_bits, new_bits)
    if refused is not None:
        assert not permitted_difficulty_transition(
            _MAINNET, height, old_bits, refused.to_bytes(4, "big")
        )


def test_off_a_retarget_the_bits_may_not_move() -> None:
    """Between retargets the bits stay; min-difficulty chains pass anyway."""
    bits, other = bytes.fromhex("1d00ffff"), bytes.fromhex("1c00ffff")
    assert permitted_difficulty_transition(_MAINNET, 2017, bits, bits)
    assert not permitted_difficulty_transition(_MAINNET, 2017, bits, other)
    assert not permitted_difficulty_transition(_MAINNET, 2017, other, bits)
    testnet = CONSENSUS_PARAMS["testnet"]
    assert permitted_difficulty_transition(testnet, 2017, bits, other)


# Core multiplies an `arith_uint256` by a `uint32_t`, which drops the
# carry: each case below is one where that 256-bit wrap decides the
# answer. Signet reaches it with a real target; regtest only with its
# minimum-difficulty rule switched off, where its easiest target is
# refused at every retarget.
_NO_MIN_DIFFICULTY_REGTEST = replace(
    CONSENSUS_PARAMS["regtest"], pow_allow_min_difficulty_blocks=False
)


@pytest.mark.parametrize(
    ("chain", "old", "new", "permitted"),
    [
        ("signet", 0x1E020000, 0x1E0377AE, True),
        ("regtest", 0x207FFFFF, 0x207FFFFF, False),
        ("regtest", 0x1E3A7717, 0x1E3A7717, False),
        ("regtest", 0x1F01FEF4, 0x1E66014D, False),
        ("regtest", 0x1F03986E, 0x1E23ED3A, True),
    ],
)
def test_a_retarget_is_bounded_in_core_s_256_bit_arithmetic(
    chain: str, old: int, new: int, *, permitted: bool
) -> None:
    """The bounds wrap past 2**256 as Core's do, and only there."""
    consensus = (
        CONSENSUS_PARAMS[chain] if chain == "signet" else _NO_MIN_DIFFICULTY_REGTEST
    )
    old_bits, new_bits = old.to_bytes(4, "big"), new.to_bytes(4, "big")
    interval = consensus.difficulty_adjustment_interval
    result = permitted_difficulty_transition(consensus, interval, old_bits, new_bits)
    assert result is permitted


def an_index(heights: int) -> Any:
    """Stand in for `BlockIndex`, its active chain `heights` past genesis."""
    return SimpleNamespace(
        active_chain=[b"tip"],
        chainwork={b"tip": (heights + 1) * _WORK},
        get_block_info=lambda _: SimpleNamespace(header=_GENESIS),
    )


@pytest.mark.parametrize(
    ("heights", "minimum", "expected_blocks"),
    [
        (199, 0, 200 - 144),
        (144, 0, 1),
        (143, 0, 0),
        (10, 0, 0),
        (199, 100, 100),
        (10, 7, 7),
    ],
)
def test_the_threshold_is_the_tip_less_144_blocks_or_the_minimum(
    heights: int, minimum: int, expected_blocks: int
) -> None:
    """Core's `GetAntiDoSWorkThreshold`, in blocks of regtest's own work."""
    threshold = anti_dos_work_threshold(an_index(heights), minimum * _WORK)
    assert threshold == expected_blocks * _WORK


def test_the_threshold_weighs_144_blocks_at_the_tip_s_own_work() -> None:
    """The 144 blocks are the tip's own proof, not the chain's average.

    And the tip is the active chain's last block, genesis being its first.
    """
    index = an_index(199)
    index.active_chain = [b"genesis", b"tip"]
    index.chainwork[b"genesis"] = _WORK
    index.chainwork[b"tip"] = 10**6
    assert anti_dos_work_threshold(index, 0) == 10**6 - 144 * _WORK


def test_a_chain_start_is_read_off_the_index(tmp_path: Path) -> None:
    """The header, its height, work and median time past, and its locator."""
    chainstate = Chainstate(tmp_path, RegTest(), Logger(debug=True))
    block_index = chainstate.block_index
    chain = generate_random_header_chain(12, _GENESIS.hash)
    block_index.add_headers(chain)
    start = ChainStart.from_index(block_index, chain[-1].hash)
    assert start.header == chain[-1]
    assert start.height == 12
    assert start.chainwork == 13 * _WORK
    # the eleven latest, a second apart: the median is the sixth from the top
    assert start.median_time_past == int(chain[6].time.timestamp())
    assert start.locator == tuple(block_index.locator_entries(chain[-1].hash))
    # Core holds `m_chain_start` as a `const CBlockIndex&`
    # (`src/headerssync.h:223`, at bitcoin/bitcoin@9be056a8a7)
    with pytest.raises(FrozenInstanceError):
        start.height = 0  # type: ignore[misc]
    chainstate.close()
