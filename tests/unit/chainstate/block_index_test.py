# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Unit tests for `btclib_node.chainstate.block_index`.

Covers `BlockInfo`'s own serialization, `calculate_work`, and
`BlockIndex`'s header validation and indexing, its active chain and
candidates, `invalidate`, persistence across a restart, and the block
locators it serves.
"""

import secrets
from contextlib import ExitStack, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, override

import pytest
from btclib.block import BlockHeader
from btclib.block.limits import MAX_TIMEWARP
from btclib.block.proof_of_work import REGTEST_POW_LIMIT_BITS
from btclib.exceptions import BTClibValueError

from btclib_node.chains import Main, RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.chainstate.block_index import (
    BlockInfo,
    BlockStatus,
    _skip_height,
    calculate_work,
)
from btclib_node.exceptions import ChainstateInconsistencyError, MisbehavingError
from btclib_node.log import Logger
from tests import brute_force_nonce, generate_random_header_chain

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from btclib.consensus import ConsensusParams

    from btclib_node.chainstate.block_index import BlockIndex


@pytest.fixture
def a_chainstate(tmp_path: Path) -> Iterator[Callable[[Path | None], Chainstate]]:
    """Build a factory for `Chainstate`s under `tmp_path`, closed at teardown.

    A test that checks a chainstate survives being closed and reopened
    closes the first itself and builds a second, at the same path;
    each is closed once more here regardless of what the test already
    did to it, `Chainstate.close` being safe to call twice.
    """
    with ExitStack() as stack:

        def make(path: Path | None = None) -> Chainstate:
            chainstate = Chainstate(
                tmp_path if path is None else path, RegTest(), Logger(debug=True)
            )
            stack.callback(chainstate.close)
            return chainstate

        yield make


def unmined_header(previous_block_hash: bytes, bits: bytes) -> BlockHeader:
    """Build a header claiming `bits`, without mining a nonce that meets it.

    Deliberately not brute_force_nonce'd: the point of every caller here
    is a header carrying a claim its own hash does not back.
    """
    # Deliberately not brute_force_nonce'd: the point of each caller is a
    # header carrying a claim its hash does not back.
    return BlockHeader(
        version=70015,
        previous_block_hash=previous_block_hash,
        merkle_root=secrets.token_bytes(32),
        time=datetime.fromtimestamp(1231006506, UTC),
        bits=bits,
        nonce=1,
        check_validity=False,
    )


def test_calculate_work() -> None:
    """calculate_work on a mined regtest-genesis-target header returns 2.

    2 is Bitcoin Core's own chainwork for the regtest genesis block,
    whose target this header carries.
    """
    header = BlockHeader(
        1,
        "00" * 32,
        "00" * 32,
        datetime.fromtimestamp(1231006506, UTC),
        REGTEST_POW_LIMIT_BITS,
        1,
    )
    brute_force_nonce(header)
    # Bitcoin Core's chainwork for the regtest genesis block, whose
    # target this is: 2^256 / (target + 1), rounded down.
    assert calculate_work(header) == 2


def test_reject_header_claiming_work_it_did_not_do(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A header claiming far more chainwork than it ever mined is refused.

    Bits 0x03000001 is a target of 1: nearly the whole hash space is
    above it, so block_work credits ~2^255 -- more than the real chain
    has ever accumulated. Nothing mined it, and the hash does not meet
    it, which is the only thing standing between a peer and the best
    chain.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    header = unmined_header(RegTest().genesis.hash, b"\x03\x00\x00\x01")
    assert calculate_work(header) > 2**254

    with pytest.raises(MisbehavingError):
        block_index.add_headers([header])
    assert header.hash not in block_index.header_dict
    assert not block_index.block_candidates
    # genesis alone, and its chainwork untouched
    assert len(block_index.header_dict) == 1


def test_a_header_claiming_a_target_it_was_never_mined_to_is_refused(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A header naming mainnet's own limit, unmined, is refused for its hash.

    Mainnet's easiest target is still far harder than regtest's own,
    so this is well inside the range `assert_valid_pow` allows a regtest
    header to claim -- it fails the other half of that same check
    instead, `hash > target`, because an unmined nonce practically never
    satisfies a target this hard.
    """
    # mainnet's easiest target is far harder than regtest's own limit,
    # so this trips the hash-versus-target half of assert_valid_pow
    # rather than the half bounding a claimed target by the chain's own
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    header = unmined_header(RegTest().genesis.hash, b"\x1d\x00\xff\xff")

    with pytest.raises(MisbehavingError):
        block_index.add_headers([header])
    assert len(block_index.header_dict) == 1


def test_reject_header_with_zero_target(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A header naming a zero target is refused rather than treated as free.

    A zero target is unsatisfiable, and block_work raises on it rather
    than reporting the block as free -- so an unchecked one takes the
    node down from the wire instead of merely being wrong.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    header = unmined_header(RegTest().genesis.hash, b"\x01\x00\xff\xff")

    with pytest.raises(MisbehavingError):
        block_index.add_headers([header])
    assert len(block_index.header_dict) == 1


def test_a_header_failing_its_own_pow_refuses_the_whole_batch(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A header failing its own proof of work keeps the whole batch out.

    Core's `CheckHeadersPoW` checks every header's own proof of work
    before any of the batch reaches `AcceptBlockHeader`
    (`net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), so a header failing it leaves the valid prefix ahead of it
    unindexed too -- unlike a header failing only its contextual check,
    `test_a_header_failing_only_its_contextual_check_leaves_the_prefix_indexed`
    below. The same headers sent again on their own are taken.
    btclib-org/btclib-node#1348
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(5, RegTest().genesis.hash)
    bad = unmined_header(chain[-1].hash, b"\x03\x00\x00\x01")

    with pytest.raises(MisbehavingError):
        block_index.add_headers([*chain, bad])
    assert len(block_index.header_dict) == 1
    # and the same batch without it is taken
    assert block_index.add_headers(chain)
    assert len(block_index.header_dict) == 5 + 1


def test_a_header_failing_only_its_contextual_check_leaves_the_prefix_indexed(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A header failing only its contextual check leaves the prefix indexed.

    Core's `ProcessNewBlockHeaders` calls `AcceptBlockHeader` once per
    header and returns at the first one failing
    `ContextualCheckBlockHeader`, so the headers already accepted ahead
    of it stay indexed (`validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) -- unlike a header
    failing its own proof of work, the previous test above.
    btclib-org/btclib-node#1348
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    genesis = RegTest().genesis
    chain = generate_random_header_chain(5, genesis.hash)
    # not later than its own parent's median -- _assert_valid_in_context's
    # own time-too-old check, never assert_valid_pow's
    bad = BlockHeader(
        version=70015,
        previous_block_hash=chain[-1].hash,
        merkle_root=secrets.token_bytes(32),
        time=genesis.time,
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(bad)

    with pytest.raises(MisbehavingError):
        block_index.add_headers([*chain, bad])
    assert bad.hash not in block_index.header_dict
    assert len(block_index.header_dict) == 5 + 1
    assert all(header.hash in block_index.header_dict for header in chain)
    assert block_index.get_block_info(chain[-1].hash).index == 5


def test_a_header_with_valid_pow_but_the_wrong_required_target_is_refused(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A header solved at the wrong target is refused by the contextual check.

    assert_valid_pow and _assert_valid_in_context share one except clause
    in add_headers; this header trips only the second, mining a target
    harder than regtest's own limit but not the one the chain requires,
    so a test that only ever builds a header failing the first check
    could not tell the two apart.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    genesis = RegTest().genesis
    # one target harder than regtest's own limit, so assert_valid_pow
    # (which only bounds the header's claimed target by the network's)
    # takes it, and mine still solves it in the same handful of tries a
    # regtest header always does
    header = BlockHeader(
        version=70015,
        previous_block_hash=genesis.hash,
        merkle_root=secrets.token_bytes(32),
        time=genesis.time + timedelta(seconds=1),
        bits=b"\x20\x7f\xff\xfe",
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)
    assert header.bits != REGTEST_POW_LIMIT_BITS

    with pytest.raises(MisbehavingError):
        block_index.add_headers([header])
    assert header.hash not in block_index.header_dict
    assert len(block_index.header_dict) == 1


def test_a_header_with_valid_pow_but_no_later_than_the_median_is_refused(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A header solved and correctly targeted, but too early, is refused.

    Same wiring question as above, tripped by the other branch of
    _assert_valid_in_context: this header carries the required target
    and a solved nonce, and only its timestamp -- the genesis' own, no
    later than the median of itself alone -- is wrong.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    genesis = RegTest().genesis
    header = BlockHeader(
        version=70015,
        previous_block_hash=genesis.hash,
        merkle_root=secrets.token_bytes(32),
        time=genesis.time,  # not later than the median of itself alone
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)

    with pytest.raises(MisbehavingError):
        block_index.add_headers([header])
    assert header.hash not in block_index.header_dict
    assert len(block_index.header_dict) == 1


class _RegTestWithBip94(RegTest):
    """`RegTest`'s own genesis and limit, with BIP94 forced on.

    No chain this package defines carries both `enforce_bip94=True` and
    a difficulty period short enough to reach in a unit test --
    `testnet4`'s own is real proof-of-work-limit work, 2016 blocks
    apart -- so this overrides the one property `next_bits_required`
    reads `enforce_bip94` off, `chain.consensus`, rather than building a
    fifth chain no `__all__` names. A one-block period
    (`pow_target_spacing == pow_target_timespan`) is what makes every
    header a period boundary, so the bound in `_assert_valid_in_context`
    is reached by the first header past genesis rather than the
    2016th.
    """

    @property
    @override
    def consensus(self) -> ConsensusParams:
        return replace(
            super().consensus,
            enforce_bip94=True,
            pow_target_spacing=1,
            pow_target_timespan=1,
        )


def test_a_header_failing_bip94s_timewarp_bound_becomes_a_misbehaving_error(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """ISS 1442: `next_bits_required`'s own timewarp refusal, translated.

    Core's `time-timewarp-attack` is `BLOCK_INVALID_HEADER`, punished by
    `MaybePunishNodeForBlock` exactly as `bad-diffbits` is
    (`src/validation.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag), but btclib's own `next_bits_required` raises a bare
    `BTClibValueError` for it, having no `MisbehavingError` of its own
    to raise -- this tree's exception, not btclib's. Before this,
    `_assert_valid_in_context` let that bare exception through
    unconverted, which `p2p.main.handle_p2p`'s own `isinstance(e,
    MisbehavingError)` check would not have discouraged the peer for,
    never exercised until a chain with `enforce_bip94=True` existed to
    call it with. Only `testnet4` sets that flag among the chains this
    package defines, and its real difficulty period is 2016 blocks,
    too slow to mine in a unit test -- `_RegTestWithBip94` above is a
    synthetic chain built to reach the same branch without it.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    block_index.chain = _RegTestWithBip94()
    genesis = block_index.chain.genesis
    header = BlockHeader(
        version=1,
        previous_block_hash=genesis.hash,
        merkle_root=secrets.token_bytes(32),
        time=genesis.time - timedelta(seconds=MAX_TIMEWARP + 1),
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=0,
        check_validity=False,
    )
    _mine_in_place(header, REGTEST_POW_LIMIT_BITS)

    with pytest.raises(MisbehavingError, match="timewarp attack"):
        block_index.add_headers([header])
    assert header.hash not in block_index.header_dict
    assert len(block_index.header_dict) == 1


def _mine_in_place(header: BlockHeader, pow_limit_bits: bytes) -> BlockHeader:
    """Search `header.nonce` upward until it meets `pow_limit_bits`, in place.

    The nonce is searched in place rather than by `brute_force_nonce`.
    Before btclib 2026.9.29 (btclib-org/btclib@bbb1ad71, closing
    btclib-org/btclib#2309), `brute_force_nonce`'s own copy would refuse
    a version of zero or below before ever searching, which a block's
    own header reaches `add_headers` with unchecked; that refusal is
    gone now (btclib-org/btclib-node#1511), but this helper's own
    unbounded retry and in-place mutation are kept regardless of it.
    Shared rather than inlined at each caller: a regtest target is met
    about every other nonce, so the retry branch below is a coin flip on
    any one call, and every caller of `a_mined_header` across this file
    already draws enough of those flips between them to make the branch
    a certainty over the whole suite -- the shape a lone caller's own
    coverage cannot rely on for itself.
    """
    while True:
        with suppress(BTClibValueError):
            header.assert_valid_pow(pow_limit_bits)
            return header
        header.nonce += 1


def a_mined_header(parent: BlockHeader, version: int) -> BlockHeader:
    """Mine a regtest header on `parent`, a second later, at `version`."""
    header = BlockHeader(
        version=version,
        previous_block_hash=parent.hash,
        merkle_root=secrets.token_bytes(32),
        time=parent.time + timedelta(seconds=1),
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=0,
        check_validity=False,
    )
    return _mine_in_place(header, REGTEST_POW_LIMIT_BITS)


@pytest.mark.parametrize("version", [-1, 1, 2, 3])
def test_a_header_version_regtest_made_obsolete_is_refused_bad_version(
    a_chainstate: Callable[[Path | None], Chainstate], version: int
) -> None:
    """ISS 1262: regtest binds BIP34, BIP66 and BIP65 from height 1.

    Core's `bad-version`, the version printed as its 32 bits, a
    `MisbehavingError` since `MaybePunishNodeForBlock` punishes it;
    version 4 is taken.
    """
    block_index = a_chainstate(None).block_index
    genesis = RegTest().genesis
    header = a_mined_header(genesis, version)
    with pytest.raises(MisbehavingError) as refusal:
        block_index.add_headers([header])
    assert str(refusal.value) == f"bad-version(0x{version & 0xFFFFFFFF:08x})"
    assert header.hash not in block_index.header_dict
    taken = a_mined_header(genesis, 4)
    assert block_index.add_headers([taken]) == taken.hash


def test_each_bip_refuses_its_obsolete_version_from_its_own_height(
    a_chainstate: Callable[[Path | None], Chainstate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1262: BIP34 from its height, BIP66 and BIP65 from theirs.

    With the three at heights 2, 3 and 4, each height takes the version
    the one before it refuses.
    """
    block_index = a_chainstate(None).block_index
    params = replace(
        block_index.chain.consensus, bip34_height=2, bip66_height=3, bip65_height=4
    )
    # a property of the class, so patched there, for this test only
    monkeypatch.setattr(RegTest, "consensus", property(lambda _: params))
    parent = RegTest().genesis
    for height, least in ((1, 1), (2, 2), (3, 3), (4, 4)):
        if least > 1:
            refused = a_mined_header(parent, least - 1)
            with pytest.raises(BTClibValueError, match="bad-version"):
                block_index.add_headers([refused])
        header = a_mined_header(parent, least)
        assert block_index.add_headers([header]) == header.hash
        assert block_index.get_block_info(header.hash).index == height
        parent = header


def test_a_version_zero_header_below_bip34_is_indexed_and_reloaded(
    a_chainstate: Callable[[Path | None], Chainstate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1262: below BIP34's height Core takes a version of zero.

    `BlockInfo.serialize` and `deserialize` round-trip this header with
    no bypass needed for it: btclib's `BlockHeader.assert_valid` used to
    refuse one on its own regardless of height, which is why the index
    used to store and read it back unchecked, fixed at btclib 2026.9.29
    (btclib-org/btclib@bbb1ad71, closing btclib-org/btclib#2309;
    btclib-org/btclib-node#1511).
    """
    params = replace(
        RegTest().consensus, bip34_height=2, bip66_height=2, bip65_height=2
    )
    monkeypatch.setattr(RegTest, "consensus", property(lambda _: params))
    chainstate = a_chainstate(None)
    header = a_mined_header(RegTest().genesis, 0)
    assert chainstate.block_index.add_headers([header]) == header.hash
    chainstate.close()
    reloaded = a_chainstate(None).block_index
    assert reloaded.get_block_info(header.hash).header.version == 0


def test_a_header_too_far_in_the_future_is_refused_without_misbehaving(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """ISS 1170: Core's `time-too-new` is `BLOCK_TIME_FUTURE`, not punished.

    The header is solved and correctly targeted, three hours ahead of the
    clock, past Core's two: refused, as `bad-diffbits` and `time-too-old`
    above are, and not a `MisbehavingError`, as they are.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    genesis = RegTest().genesis
    header = BlockHeader(
        version=70015,
        previous_block_hash=genesis.hash,
        merkle_root=secrets.token_bytes(32),
        time=datetime.now(UTC) + timedelta(hours=3),
        bits=REGTEST_POW_LIMIT_BITS,
        nonce=1,
        check_validity=False,
    )
    brute_force_nonce(header)

    with pytest.raises(BTClibValueError) as refused:
        block_index.add_headers([header])
    assert not isinstance(refused.value, MisbehavingError)
    assert header.hash not in block_index.header_dict


def test_add_headers_returns_the_batch_s_own_tip(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """add_headers answers with the batch's own tip, new or already known.

    Sending the same batch again, once every header in it is already
    indexed, answers with the same tip rather than `None`.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(5, RegTest().genesis.hash)
    assert block_index.add_headers(chain) == chain[-1].hash
    # already known in full, and still answers with its own tip
    assert block_index.add_headers(chain) == chain[-1].hash


def test_add_headers_resumes_from_a_fork_s_own_tip_not_the_best_chain(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A fork below the active chain's own tip does not move header_index.

    header_index only moves for a header extending it or beating its
    chainwork, so add_headers' own return value -- the fork's own tip,
    not header_index's -- is what a caller has to resume a locator from,
    or it would ask for this same batch again and never reach further
    into the fork. btclib-org/btclib-node#122
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    active = generate_random_header_chain(5, RegTest().genesis.hash)
    block_index.add_headers(active)
    for header in active:
        block_index.add_to_active_chain(header.hash)

    fork = generate_random_header_chain(3, RegTest().genesis.hash)
    assert block_index.add_headers(fork) == fork[-1].hash
    assert fork[-1].hash not in block_index.header_index


def test_simple_init(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """A freshly built index matches one reloaded from the same store.

    2000 headers are indexed, the store is closed, and a second
    `BlockIndex` opened on the same path agrees on every field
    `init_from_db` rebuilds, chainwork included -- recomputed rather
    than persisted (btclib-org/btclib-node#201).
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    block_index.add_headers(generate_random_header_chain(2000, RegTest().genesis.hash))
    chainstate.db.close()
    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert block_index.header_dict == new_block_index.header_dict
    assert block_index.header_index == new_block_index.header_index
    assert block_index.active_chain == new_block_index.active_chain
    assert block_index.block_candidates == new_block_index.block_candidates
    # not persisted, recomputed by calculate_chainwork on each start:
    # btclib-org/btclib-node#201
    assert block_index.chainwork == new_block_index.chainwork


def test_init_with_fork(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """A reloaded index agrees on a forked chain too, candidates included.

    A 2000-header active chain and a five-header fork off its tenth
    block from the tip both survive a close and reopen, `header_dict`,
    `header_index`, `active_chain` and `block_candidates` agreeing
    between the two.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(5, chain[-10].hash, chain[-10].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    chainstate.db.close()
    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert block_index.header_dict == new_block_index.header_dict
    assert block_index.header_index == new_block_index.header_index
    assert block_index.active_chain == new_block_index.active_chain
    assert sorted(block_index.block_candidates) == sorted(
        new_block_index.block_candidates
    )


def test_add_headers_fork(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """header_index grows by the whole batch, chain and fork alike.

    A 2000-header chain and a 200-header fork off its eleventh block
    from the tip are both indexed, and header_index ends up holding
    every one of the 2190 distinct headers plus the genesis.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(200, chain[-10 - 1].hash, chain[-10 - 1].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    assert len(block_index.header_index) == 2190 + 1


def test_generate_block_candidates(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """Marking a chain in_active_chain leaves only the fork as a candidate.

    Once every header of the 2000-header chain is set
    `in_active_chain`, a reload's `generate_block_candidates` rebuilds
    `block_candidates` from what is left: the 200-header fork, minus
    the ten blocks its own branch point sits before the tip.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(200, chain[-10 - 1].hash, chain[-10 - 1].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    for x in chain:
        block_index.set_status(x.hash, BlockStatus.in_active_chain)
    chainstate.db.close()
    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert len(new_block_index.block_candidates) == 190


def test_generate_block_candidates_2(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """Marking the fork invalid leaves the whole active chain as candidates.

    With the 200-header fork marked `invalid` instead, a reload's
    `generate_block_candidates` counts every header of the 2000-header
    chain: `valid_header` status alone is what qualifies a header, and
    the active chain never had its own status set to anything else here.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    fork = generate_random_header_chain(200, chain[-10 - 1].hash, chain[-10 - 1].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    for x in fork:
        block_index.set_status(x.hash, BlockStatus.invalid)
    chainstate.db.close()
    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert len(new_block_index.block_candidates) == 2000


def test_invalidate_marks_every_header_indexed_on_it_not_only_candidates(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """`invalidate` reaches a whole lineage, not only its block_candidates.

    A header enters header_dict, at valid_header, whenever it merely
    arrives -- block_candidates only holds the ones whose own
    cumulative chainwork individually cleared the active chain's at
    the moment they arrived, so a real descendant that never did is
    only reached by walking `children`, not the deque:
    btclib-org/btclib-node#125.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    active = generate_random_header_chain(5, RegTest().genesis.hash)
    block_index.add_headers(active)
    for header in active:
        block_index.add_to_active_chain(header.hash)

    # genesis-rooted, and never its own candidate: five headers' worth
    # of chainwork does not exceed what active's own five already hold
    victim = generate_random_header_chain(5, RegTest().genesis.hash)
    block_index.add_headers(victim)
    victim_hashes = {header.hash for header in victim}
    assert not victim_hashes & {h for h, _ in block_index.block_candidates}

    sibling = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers(sibling)

    block_index.invalidate(victim[0].hash)

    for header in victim:
        assert block_index.get_block_info(header.hash).status == BlockStatus.invalid
    assert (
        block_index.get_block_info(sibling[0].hash).status == BlockStatus.valid_header
    )
    assert not victim_hashes & {h for h, _ in block_index.block_candidates}
    chainstate.close()


def test_a_header_built_on_an_invalid_parent_refuses_the_batch_misbehaving(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """ISS 1233: Core's `bad-prevblk`, `BLOCK_INVALID_PREV`, a `Misbehaving`.

    Invalidating a chain's first header, then sending a header that
    extends its second: refused, and nothing of it indexed.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    block_index.add_headers(chain)
    block_index.invalidate(chain[0].hash)

    extension = generate_random_header_chain(1, chain[1].hash, chain[1].time)
    with pytest.raises(MisbehavingError, match=r"^bad-prevblk$"):
        block_index.add_headers(extension)
    assert extension[0].hash not in block_index.header_dict
    chainstate.close()


def test_invalidate_moves_header_index_off_the_chain_it_was_pointing_at(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """Invalidating header_index's own tip moves header_index off that chain.

    header_index is the best known header chain for locator/announce
    purposes, tracked independently of block_candidates and weighed
    purely by chainwork -- so invalidating the chain it happened to
    end on leaves it no longer pointing at a chain this node has
    refused. btclib-org/btclib-node#218
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.header_index[-1] == chain[-1].hash

    block_index.invalidate(chain[0].hash)

    assert block_index.header_index[-1] != chain[-1].hash
    assert chain[-1].hash not in block_index.get_block_locator_hashes()
    chainstate.close()


def test_invalidate_recomputes_header_index_onto_the_next_best_surviving_chain(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """Invalidating header_index's chain moves it onto the best surviving one.

    The fallback is not always the active chain: a seven-header fork
    with more work than the five-header active chain is what
    header_index recomputes onto once the fork itself is invalidated --
    the same way Core's InvalidateBlock (src/validation.cpp) recomputes
    m_best_header. btclib-org/btclib-node#218
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    active = generate_random_header_chain(5, RegTest().genesis.hash)
    block_index.add_headers(active)
    for header in active:
        block_index.add_to_active_chain(header.hash)

    fork = generate_random_header_chain(7, RegTest().genesis.hash)
    assert block_index.add_headers(fork) == fork[-1].hash
    assert block_index.header_index[-1] == fork[-1].hash

    block_index.invalidate(fork[0].hash)

    assert block_index.header_index == block_index.active_chain
    chainstate.close()


def test_a_batch_extending_an_invalidated_chain_does_not_move_header_index(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """More headers on an already-invalidated chain never move header_index.

    The batch is refused (`bad-prevblk`, btclib-org/btclib-node#1233), so
    a peer sending more of a chain this node has already refused cannot
    grow what this index reports as its best known header chain.
    btclib-org/btclib-node#218
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    block_index.add_headers(chain)
    block_index.invalidate(chain[0].hash)
    header_index_before = list(block_index.header_index)

    extension = generate_random_header_chain(10, chain[1].hash, chain[1].time)
    with pytest.raises(MisbehavingError, match=r"^bad-prevblk$"):
        block_index.add_headers(extension)
    assert block_index.header_index == header_index_before
    chainstate.close()


@pytest.mark.parametrize("punish", [True, False])
def test_an_invalid_header_sent_again_refuses_the_batch(
    a_chainstate: Callable[[Path | None], Chainstate],
    punish: bool,  # noqa: FBT001
) -> None:
    """ISS 1233: Core's `duplicate-invalid`, `BLOCK_CACHED_INVALID`.

    A `MisbehavingError` where the caller asks for it, as Core punishes
    an outbound peer alone; otherwise a refusal that costs nothing. The
    header after it in the batch is not indexed either way.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2, RegTest().genesis.hash)
    block_index.add_headers(chain[:1])
    block_index.invalidate(chain[0].hash)

    with pytest.raises(BTClibValueError, match=r"^duplicate-invalid$") as refused:
        block_index.add_headers(chain, punish_cached_invalid=punish)
    assert isinstance(refused.value, MisbehavingError) is punish
    assert chain[1].hash not in block_index.header_dict
    assert block_index.get_block_info(chain[0].hash).status == BlockStatus.invalid
    chainstate.close()


def test_invalidated_headers_stay_out_of_header_index_after_a_restart(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A restart's rebuilt header_index still excludes an invalidated chain.

    generate_header_index rebuilds from the persisted BlockStatus on
    every start-up, so a chain invalidated before the restart is
    excluded again rather than reappearing because nothing in the
    fresh index remembers the earlier invalidate call.
    btclib-org/btclib-node#218
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    block_index.invalidate(chain[0].hash)
    header_index_before = list(block_index.header_index)
    chainstate.db.close()

    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert new_block_index.header_index == header_index_before
    assert chain[-1].hash not in new_block_index.header_index


def test_first_candidate_skips_a_hole_behind_a_downloaded_tip(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """get_first_candidate skips a branch downloaded only at its own tip.

    get_first_candidate used to ask only whether a candidate's own tip
    had arrived: a branch with a downloaded tip but an undownloaded
    block behind it passed that check and update_chain then stalled on
    the hole every pass, leaving nothing else able to connect --
    btclib-org/btclib-node#121. The complete one-header branch here is
    what it returns instead.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    hole = generate_random_header_chain(2, RegTest().genesis.hash)
    block_index.add_headers(hole)
    block_index.set_downloaded(hole[-1].hash)  # the tip alone

    complete = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers(complete)
    block_index.set_downloaded(complete[0].hash)

    candidate = block_index.get_first_candidate()
    assert candidate is not None
    assert candidate.header.hash == complete[0].hash
    chainstate.close()


def test_block_info_serialization() -> None:
    """A `BlockInfo` round-trips through serialize/deserialize for every status.

    Every `BlockStatus`, both `downloaded` values, and 63 distinct
    `index` values are each built into a `BlockInfo` and checked against
    the record `deserialize` parses back from its own `serialize`.
    """
    header = BlockHeader(
        1,
        "00" * 32,
        "00" * 32,
        datetime.fromtimestamp(1231006506, UTC),
        REGTEST_POW_LIMIT_BITS,
        1,
        check_validity=False,
    )
    brute_force_nonce(header)
    for status in BlockStatus:
        for downloaded in (True, False):
            for x in range(1, 64):
                block_info = BlockInfo(
                    header=header,
                    index=x**2 - 1,
                    status=status,
                    downloaded=downloaded,
                )
                assert block_info == BlockInfo.deserialize(block_info.serialize())


def test_add_old_header(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """Re-sending an already-indexed header answers with its own hash.

    add_headers on a single header already inside a 2000-header chain
    returns that header's own hash -- a real point a caller could
    resume from -- and leaves `header_dict`, `header_index` and
    `block_candidates` at the sizes the original batch already set.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    # already known, and still answers with its own hash: nothing new,
    # but a real point a caller could resume from
    assert block_index.add_headers([chain[10]]) == chain[10].hash
    assert len(block_index.header_dict) == 2000 + 1
    assert len(block_index.header_index) == 2000 + 1
    assert len(block_index.block_candidates) == 2000


def test_add_headers_connecting_to_nothing_known_is_not_a_refusal(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A batch connecting to nothing known answers `None` rather than raising.

    Every header here is validly mined; none of them fails on its own
    terms, so this is the "connects to nothing" branch and not the "a
    header failed a check" one, and it leaves the existing index
    untouched.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(2000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    disconnected_chain = generate_random_header_chain(2000, Main().genesis.hash)
    assert block_index.add_headers(disconnected_chain) is None
    assert len(block_index.header_dict) == 2000 + 1
    assert len(block_index.header_index) == 2000 + 1
    assert len(block_index.block_candidates) == 2000


def test_a_header_before_its_own_new_parent_in_the_batch_refuses_the_batch(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A batch carrying a child before its own new parent is refused whole.

    Core's `CheckHeadersAreContinuous` asks each header to build on the
    one before it, and this one does not: btclib-org/btclib-node#214.
    Both headers stay out of `header_dict`.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    parent, child = generate_random_header_chain(2, RegTest().genesis.hash)

    with pytest.raises(MisbehavingError, match="non-continuous headers sequence"):
        block_index.add_headers([child, parent])
    assert child.hash not in block_index.header_dict
    assert parent.hash not in block_index.header_dict
    assert len(block_index.header_dict) == 1


def test_a_batch_jumping_to_a_known_header_s_child_is_refused(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """ISS 1233: Core's "non-continuous headers sequence", a `Misbehaving`.

    Every header here connects to something indexed on its own: the
    second builds on a header this node holds, not on the first.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers(chain)
    first = generate_random_header_chain(1, RegTest().genesis.hash)
    jump = generate_random_header_chain(1, chain[0].hash, chain[0].time)

    with pytest.raises(MisbehavingError, match="non-continuous headers sequence"):
        block_index.add_headers([*first, *jump])
    assert first[0].hash not in block_index.header_dict
    assert jump[0].hash not in block_index.header_dict
    chainstate.close()


def test_add_headers_short(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """Ten batches of 2000 headers each add up the same as one big one.

    Sent 2000 at a time, a 20000-header chain still leaves
    `header_dict`, `header_index` and `block_candidates` at the sizes
    a single batch of the same chain would.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    length = 10
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    assert len(block_index.header_dict) == 2000 * length + 1
    assert len(block_index.header_index) == 2000 * length + 1
    assert len(block_index.block_candidates) == 2000 * length


def test_add_headers_medium(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """The same batching check as above, at 40 batches of 2000 headers."""
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    length = 40  # 400
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    assert len(block_index.header_dict) == 2000 * length + 1
    assert len(block_index.header_index) == 2000 * length + 1
    assert len(block_index.block_candidates) == 2000 * length


def test_add_headers_long(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """The same batching check as above, at 50 batches of 2000 headers."""
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    length = 50  # 2000
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    assert len(block_index.header_dict) == 2000 * length + 1
    assert len(block_index.header_index) == 2000 * length + 1
    assert len(block_index.block_candidates) == 2000 * length


def test_long_init(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """A 100000-header index reloads from disk agreeing on every field.

    The same close-and-reopen check as test_simple_init, at 50 batches
    of 2000 headers each, so a start-up rebuild is also exercised at a
    size closer to what a real sync produces.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    length = 50  # 2000
    chain = generate_random_header_chain(2000 * length, RegTest().genesis.hash)
    for x in range(length):
        block_index.add_headers(chain[x * 2000 : (x + 1) * 2000])
    chainstate.db.close()
    new_chainstate = a_chainstate(None)
    new_block_index = new_chainstate.block_index
    assert block_index.header_dict == new_block_index.header_dict
    assert block_index.header_index == new_block_index.header_index
    assert block_index.active_chain == new_block_index.active_chain
    assert block_index.block_candidates == new_block_index.block_candidates
    # not persisted, recomputed by calculate_chainwork on each start:
    # btclib-org/btclib-node#201
    assert block_index.chainwork == new_block_index.chainwork
    # rebuilt on each start as well, as Core's `BuildSkip` is on load
    assert block_index.skip == new_block_index.skip


def test_block_locators(a_chainstate: Callable[[Path | None], Chainstate]) -> None:
    """A 24-header chain's locator carries 14 entries.

    Ten dense entries near the tip, then a step that doubles each time,
    reaching back to the genesis in four more -- the shape
    get_block_locator_hashes' own docstring names.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(24, RegTest().genesis.hash)
    block_index.add_headers(chain)
    locators = block_index.get_block_locator_hashes()
    assert len(locators) == 14


def test_a_locator_from_a_start_header_is_that_header_s_own_tip_locator(
    a_chainstate: Callable[[Path | None], Chainstate],
    tmp_path: Path,
) -> None:
    """A locator from `start` is the one an index ending at `start` builds."""
    chain = generate_random_header_chain(24, RegTest().genesis.hash)
    block_index = a_chainstate(None).block_index
    block_index.add_headers(chain)
    shorter = a_chainstate(tmp_path / "shorter").block_index
    shorter.add_headers(chain[:-1])
    locators = block_index.get_block_locator_hashes(chain[-2].hash)
    assert locators == shorter.get_block_locator_hashes()
    assert locators[0] == chain[-2].hash


def test_only_the_tip_can_leave_the_active_chain(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """remove_from_active_chain refuses anything but the chain's own tip.

    A reorg unwinds from the tip: removing anything else would leave
    the chain with a hole nothing else here checks for, so it raises
    `ChainstateInconsistencyError` instead.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    for header in chain:
        block_index.add_to_active_chain(header.hash)

    with pytest.raises(
        ChainstateInconsistencyError, match="not the active chain's tip"
    ):
        block_index.remove_from_active_chain(chain[0].hash)
    block_index.remove_from_active_chain(chain[-1].hash)
    assert chain[-1].hash not in block_index.active_chain
    chainstate.close()


def test_nothing_is_offered_when_there_are_no_candidates(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """get_first_candidate answers `None` on a genesis-only index."""
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    assert block_index.get_first_candidate() is None
    chainstate.close()


def test_no_candidate_is_offered_when_none_outweighs_the_chain(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """get_first_candidate stops offering a chain once caught up to.

    A header is a candidate when it arrives and stops being one once
    the active chain has caught up to it: equal work is not more work,
    or the node would keep offering the block it is already on.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(3, RegTest().genesis.hash)
    block_index.add_headers(chain)
    assert block_index.get_first_candidate() is not None

    for header in chain:
        block_index.add_to_active_chain(header.hash)
    assert block_index.get_first_candidate() is None
    chainstate.close()


def test_a_header_that_does_not_outweigh_the_chain_is_not_a_candidate(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A fork with less work than the active chain is indexed but not offered.

    A candidate is a chain worth switching to. A fork branching off the
    genesis carries less accumulated work than a ten-header active
    chain, so it is indexed -- it may yet be extended -- without being
    added to `block_candidates`.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    chain = generate_random_header_chain(10, RegTest().genesis.hash)
    block_index.add_headers(chain)
    for header in chain:
        block_index.add_to_active_chain(header.hash)
    block_index.block_candidates.clear()

    short_fork = generate_random_header_chain(1, RegTest().genesis.hash)
    assert block_index.add_headers(short_fork)
    assert short_fork[0].hash in block_index.header_dict
    assert not block_index.block_candidates
    chainstate.close()


def test_header_index_pos_agrees_with_header_index(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """header_index_pos always maps header_index's own hashes to their position.

    Checked after add_headers builds a fork that beats the active
    chain's own tip (moving header_index by more than one append) and
    again after invalidate rebuilds header_index from scratch.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index

    def assert_consistent() -> None:
        assert block_index.header_index_pos == {
            h: i for i, h in enumerate(block_index.header_index)
        }

    assert_consistent()
    active = generate_random_header_chain(5, RegTest().genesis.hash)
    block_index.add_headers(active)
    for header in active:
        block_index.add_to_active_chain(header.hash)
    assert_consistent()

    fork = generate_random_header_chain(7, RegTest().genesis.hash)
    block_index.add_headers(fork)
    assert block_index.header_index[-1] == fork[-1].hash  # the fork won
    assert_consistent()

    block_index.invalidate(fork[0].hash)
    assert_consistent()
    chainstate.close()


def test_the_locators_of_a_node_that_holds_only_the_genesis_block(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """get_block_locator_hashes on a genesis-only index names the genesis once.

    The list ends at the oldest header this node has, and here the walk
    back has already named it, it being the newest as well. What keeps
    it off the end a second time -- and the peer from being asked the
    same question twice -- is the guard on that last append.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    assert block_index.get_block_locator_hashes() == [RegTest().genesis.hash]
    chainstate.close()


def test_finalize_with_no_batch_opens_its_own_and_writes_pending(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """`finalize()`, called with no `wb`, still writes and clears `pending`.

    `main._finalize_fork` always hands `finalize` the batch
    `UtxoIndex`/`FilterIndex` share (`Chainstate.flush`), so this is the
    other path: a caller with no batch of its own, mirroring
    `FilterIndex.finalize`'s own bare form.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    (header,) = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers([header])
    block_index.stage_status(header.hash, BlockStatus.in_active_chain)
    assert header.hash in block_index.pending

    block_index.finalize()

    assert block_index.pending == {}
    data = block_index.db.get(b"blkinfo-" + header.hash)
    assert data is not None
    stored = BlockInfo.deserialize(data, check_validity=False)
    assert stored.status == BlockStatus.in_active_chain
    chainstate.close()


def test_invalidate_after_stage_status_is_not_undone_by_a_later_finalize(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """A write-through invalidate on a staged hash must survive the next flush.

    `stage_status` stages a hash in `pending` without writing it. If
    `invalidate` (through `set_status`) then targeted that same hash --
    reachable in `main.update_chain` through a chain-tip flip-flop, or
    through an I/O fault in `block_db.add_rev_block` or
    `filter_index.add_connected_block` that has nothing to do with the
    block's own content -- writing straight through used to leave
    `pending` holding a stale entry that the next `finalize` wrote back
    over the invalidation, undoing it silently. btclib-org/btclib-node#586.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    (header,) = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers([header])
    block_index.stage_status(header.hash, BlockStatus.in_active_chain)
    assert header.hash in block_index.pending

    block_index.invalidate(header.hash)

    block_index.finalize()

    assert block_index.pending == {}
    data = block_index.db.get(b"blkinfo-" + header.hash)
    assert data is not None
    stored = BlockInfo.deserialize(data, check_validity=False)
    assert stored.status == BlockStatus.invalid
    chainstate.close()


def test_set_downloaded_after_stage_status_is_not_undone_by_a_later_finalize(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """`set_downloaded` on a staged hash survives the next flush.

    Reachable from `main.prune_up_to_height`: `_finalize_fork`'s own
    to_add loop stages every hash a fork connects through `stage_status`,
    before that fork's own `finalize` ever runs, and `to_add` is not
    bounded by `MIN_BLOCKS_TO_KEEP` anywhere -- a fork longer than the
    retained depth stages a hash `prune_up_to_height`, run once at the
    end of that same `update_chain` call, then clears the flag on.
    Writing straight through would leave `pending` holding a stale
    `downloaded=True` entry that the next `finalize` writes back over
    the clear, undoing it silently -- the same shape
    btclib-org/btclib-node#586 fixed for `set_status`.
    """
    chainstate = a_chainstate(None)
    block_index = chainstate.block_index
    (header,) = generate_random_header_chain(1, RegTest().genesis.hash)
    block_index.add_headers([header])
    # downloaded=True before staging: _finalize_fork's own to_add loop
    # only ever stages a hash _ready_fork already required downloaded,
    # so the pending entry stage_status below captures carries that
    # True forward -- the stale value a write-through set_downloaded
    # would otherwise lose to the next finalize.
    block_index.set_downloaded(header.hash)
    block_index.stage_status(header.hash, BlockStatus.in_active_chain)
    assert header.hash in block_index.pending
    assert block_index.pending[header.hash].downloaded is True

    block_index.set_downloaded(header.hash, downloaded=False)

    block_index.finalize()

    assert block_index.pending == {}
    data = block_index.db.get(b"blkinfo-" + header.hash)
    assert data is not None
    stored = BlockInfo.deserialize(data, check_validity=False)
    assert stored.downloaded is False
    chainstate.close()


def _walked_ancestor(block_index: BlockIndex, block_hash: bytes, height: int) -> bytes:
    """Return the ancestor at `height` by parent hash alone, the slow way."""
    while block_index.header_dict[block_hash].index > height:
        block_hash = block_index.header_dict[block_hash].header.previous_block_hash
    return block_hash


def test_the_skip_heights_are_core_s() -> None:
    """`_skip_height` is Core's `GetSkipHeight`, at heights worked by hand.

    Below 2 it is 0; at an even height the lowest set bit is cleared; at
    an odd one the two lowest set bits of the height below are, plus one.
    """
    assert [_skip_height(h) for h in range(10)] == [0, 0, 0, 1, 0, 1, 4, 1, 0, 1]
    assert _skip_height(20000) == 19968
    assert _skip_height(20001) == 19457
    assert all(0 <= _skip_height(h) < h for h in range(2, 5000))


def test_every_skip_pointer_is_the_ancestor_at_its_skip_height(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """Core's `skiplist_test`: each pointer lands where `GetSkipHeight` says.

    On a chain and on a fork off its middle, and genesis alone without one.
    """
    block_index = a_chainstate(None).block_index
    chain = generate_random_header_chain(600, RegTest().genesis.hash)
    fork = generate_random_header_chain(300, chain[299].hash, chain[299].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    assert RegTest().genesis.hash not in block_index.skip
    for header in (*chain, *fork):
        block_hash = header.hash
        height = block_index.header_dict[block_hash].index
        skip = block_index.skip[block_hash]
        assert block_index.header_dict[skip].index == _skip_height(height)
        assert skip == _walked_ancestor(block_index, block_hash, _skip_height(height))


def test_get_ancestor_answers_what_the_parent_walk_answers(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """Core's `GetAncestor`, at every height of a block and past both ends.

    `None` above the block's own height and below zero.
    """
    block_index = a_chainstate(None).block_index
    chain = generate_random_header_chain(1100, RegTest().genesis.hash)
    fork = generate_random_header_chain(600, chain[499].hash, chain[499].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    for tip in (chain[-1].hash, fork[-1].hash, fork[0].hash):
        tip_height = block_index.header_dict[tip].index
        for height in range(tip_height + 1):
            assert block_index.get_ancestor(tip, height) == _walked_ancestor(
                block_index, tip, height
            )
        assert block_index.get_ancestor(tip, tip_height + 1) is None
        assert block_index.get_ancestor(tip, -1) is None


def test_the_last_common_ancestor_is_the_fork_point(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """Core's `LastCommonAncestor`, whichever block is the higher one.

    Branches off one block, a block and its own ancestor, and a block
    with itself.
    """
    block_index = a_chainstate(None).block_index
    chain = generate_random_header_chain(1000, RegTest().genesis.hash)
    block_index.add_headers(chain)
    for fork_at, length in ((0, 700), (345, 1), (345, 900), (998, 3)):
        fork = generate_random_header_chain(
            length, chain[fork_at].hash, chain[fork_at].time
        )
        block_index.add_headers(fork)
        for first, second in (
            (chain[-1].hash, fork[-1].hash),
            (fork[-1].hash, chain[-1].hash),
        ):
            fork_point = block_index.last_common_ancestor(first, second)
            assert fork_point == chain[fork_at].hash
    ancestor, descendant = chain[10].hash, chain[700].hash
    assert block_index.last_common_ancestor(ancestor, descendant) == ancestor
    assert block_index.last_common_ancestor(ancestor, ancestor) == ancestor


class _CountingDict(dict[bytes, object]):
    """A dict counting its own item reads, for the cost of a walk."""

    reads = 0

    @override
    def __getitem__(self, key: bytes) -> object:
        _CountingDict.reads += 1
        return super().__getitem__(key)


def test_an_ancestor_far_below_is_reached_in_few_steps(
    a_chainstate: Callable[[Path | None], Chainstate],
) -> None:
    """The skip pointers bound the walk, as Core's `GetAncestor` has them do.

    On a chain of 4000 headers, reaching any height from the tip reads
    far fewer entries than the 4000 a walk by parent hash would, and so
    does the fork point of two branches 2000 blocks long.
    """
    block_index = a_chainstate(None).block_index
    chain = generate_random_header_chain(4000, RegTest().genesis.hash)
    fork = generate_random_header_chain(2000, chain[1999].hash, chain[1999].time)
    block_index.add_headers(chain)
    block_index.add_headers(fork)
    block_index.header_dict = _CountingDict(block_index.header_dict)  # type: ignore[assignment]
    block_index.skip = _CountingDict(block_index.skip)  # type: ignore[assignment]
    most = 0
    for height in range(0, 4001, 37):
        _CountingDict.reads = 0
        block_index.get_ancestor(chain[-1].hash, height)
        most = max(most, _CountingDict.reads)
    assert most < 200
    _CountingDict.reads = 0
    block_index.last_common_ancestor(chain[-1].hash, fork[-1].hash)
    assert _CountingDict.reads < 200
