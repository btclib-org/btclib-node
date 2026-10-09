# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.rolling_bloom`.

The filter is checked against Bitcoin Core's own, run on the same keys
with the same tweak: `_data/core_rolling_bloom_runs.txt`, which
`tests/_data/README.md` says how to make.
"""

from pathlib import Path

import pytest

from btclib_node.rolling_bloom import RollingBloomFilter

_DATA = Path(__file__).parent / "_data"


# `src/test/hash_tests.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag:
# the hash Core expects, the seed, and the data in hex
_MURMUR_VECTORS = (
    (0x00000000, 0x00000000, ""),
    (0x6A396F08, 0xFBA4C795, ""),
    (0x81F16F39, 0xFFFFFFFF, ""),
    (0x514E28B7, 0x00000000, "00"),
    (0xEA3F0B17, 0xFBA4C795, "00"),
    (0xFD6CF10D, 0x00000000, "ff"),
    (0x16C6B7AB, 0x00000000, "0011"),
    (0x8EB51C3D, 0x00000000, "001122"),
    (0xB4471BF8, 0x00000000, "00112233"),
    (0xE2301FA8, 0x00000000, "0011223344"),
    (0xFC2E4A15, 0x00000000, "001122334455"),
    (0xB074502C, 0x00000000, "00112233445566"),
    (0x8034D2A0, 0x00000000, "0011223344556677"),
    (0xB4698DEF, 0x00000000, "001122334455667788"),
)


@pytest.mark.parametrize(("expected", "seed", "data"), _MURMUR_VECTORS)
def test_murmurhash3_matches_core_s_vectors(
    expected: int, seed: int, data: str
) -> None:
    """One hash function's seed is the tweak, so its lane is MurmurHash3's."""
    one_lane = RollingBloomFilter(1, 0.5, tweak=seed)
    assert one_lane._hashes(bytes.fromhex(data)) == expected


def _fnv1a64(octets: bytes) -> int:
    value = 0xCBF29CE484222325
    for octet in octets:
        value = ((value ^ octet) * 0x100000001B3) & 0xFFFF_FFFF_FFFF_FFFF
    return value


def _core_words(bloom: RollingBloomFilter) -> bytes:
    """Return Core's `data`, the even and odd words interleaved."""
    lo, hi = bloom._planes  # type: ignore[misc]
    return b"".join(
        w.to_bytes(8, "little") for pair in zip(lo, hi, strict=True) for w in pair
    )


def _core_runs() -> list[tuple[str, str]]:
    lines = (_DATA / "core_rolling_bloom_runs.txt").read_text().splitlines()
    return list(zip(lines[::2], lines[1::2], strict=True))


_CORE_RUNS = _core_runs()


@pytest.mark.parametrize(("case", "answer"), _CORE_RUNS, ids=range(len(_CORE_RUNS)))
def test_core_s_own_runs(case: str, answer: str) -> None:
    """Core v31.1's `CRollingBloomFilter`, run on the same keys and tweaks.

    Its sizes, its generation, its words and its answer to every query,
    across a `reset` to a given tweak.
    """
    n, fp, tweak, *ops = case.split()
    bloom = RollingBloomFilter(int(n), float(fp), tweak=int(tweak))
    answers = []
    tokens = iter(ops)
    for op in tokens:
        if op[0] == "!":
            bloom.reset(tweak=int(op[1:]))
            continue
        if op[1:] == "n":
            start, count = int(next(tokens)), int(next(tokens))
            keys = [i.to_bytes(32, "little") for i in range(start, start + count)]
        else:
            keys = [bytes.fromhex(op[1:])]
        for key in keys:
            if op[0] == "+":
                bloom.add(key)
            else:
                answers.append("1" if key in bloom else "0")
    state = (
        bloom._lane_bytes // 8,
        bloom._per_generation,
        bloom._size,
        bloom._generation,
        bloom._this_generation,
        _fnv1a64(_core_words(bloom)),
    )
    assert f"{' '.join(map(str, state))} | {''.join(answers)}" == answer


def test_a_filter_holds_no_words_before_its_first_key() -> None:
    """Nothing is allocated until a key is added, and nothing is found."""
    bloom = RollingBloomFilter(50_000, 0.000_001)
    assert bytes(32) not in bloom
    assert bloom._planes is None
    bloom.add(bytes(32))
    assert bytes(32) in bloom


def test_the_latest_keys_are_always_held() -> None:
    """Core's own `rolling_bloom` check: the last `n_elements`, any tweak."""
    bloom = RollingBloomFilter(100, 0.01)
    keys = [i.to_bytes(32, "little") for i in range(399)]
    for key in keys:
        bloom.add(key)
    assert all(key in bloom for key in keys[299:])


def test_a_reset_frees_the_words() -> None:
    """Core's `reset` zeroes its words; here they go until the next key."""
    bloom = RollingBloomFilter(100, 0.01)
    bloom.add(bytes(32))
    bloom.reset()
    assert bloom._planes is None
    assert bytes(32) not in bloom


def a_small_filter(held: bytes) -> RollingBloomFilter:
    """Return a filter holding `held` that often finds a key it never held.

    One hash function over one pair of words: a key never added is found
    about one time in 64, so a few hundred candidates hold a false
    positive. A site's tests put it in place of the site's filter.
    """
    bloom = RollingBloomFilter(1, 0.5, tweak=0)
    bloom.add(held)
    return bloom


def test_a_small_filter_finds_a_key_it_never_held() -> None:
    """The false positive the sites' tests rely on is there to be found."""
    bloom = a_small_filter(bytes(32))
    keys = (i.to_bytes(32, "little") for i in range(1, 1000))
    assert any(key in bloom for key in keys)
