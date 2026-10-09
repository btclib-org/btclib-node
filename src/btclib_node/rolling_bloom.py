# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Bitcoin Core's `CRollingBloomFilter`.

`src/common/bloom.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag: the
same parameters, hash functions, generations and bits. Given Core's tweak,
a filter here answers every query as Core's does.

Core runs MurmurHash3 (`src/hash.cpp`, same sha) once per hash function.
Here one Python int holds every run, 64 bits a lane, each lane one run's
32-bit state. A shift, a mask or a multiplication of that int is the same
operation in every lane, and no lane's product reaches the next: one
Python operation does the work of all the runs.
"""

import math
import secrets
import struct
import sys
from array import array
from decimal import ROUND_HALF_UP, Decimal

__all__ = ["RollingBloomFilter"]

_MASK32 = 0xFFFF_FFFF
_C1 = 0xCC9E_2D51
_C2 = 0x1B87_3593


class RollingBloomFilter:
    """The keys inserted lately, with about `fp_rate` false positives.

    The latest `n_elements` keys are always found. A key inserted
    `3 * ((n_elements + 1) // 2)` insertions ago or earlier is forgotten,
    and is found as rarely as one never inserted. Core's `insert` is `add`,
    its `contains` is `in`.

    The tweak is Core's `nTweak`, drawn at random as Core draws it unless
    given. Core's `data` is two arrays here, its even words and its odd,
    allocated at the first `add`.
    """

    __slots__ = (
        "_add",
        "_bit_mask",
        "_generation",
        "_index_mask",
        "_lane_bytes",
        "_lane_mask",
        "_lanes",
        "_per_generation",
        "_planes",
        "_seeds",
        "_size",
        "_this_generation",
        "_unpack",
        "_words",
    )

    def __init__(
        self, n_elements: int, fp_rate: float, *, tweak: int | None = None
    ) -> None:
        """Size the filter as Core's constructor does."""
        log_fp_rate = math.log(fp_rate)
        exact = Decimal(log_fp_rate / math.log(0.5))
        # C's `round`: a half goes away from zero, where Python's goes even
        rounded = int(exact.to_integral_value(ROUND_HALF_UP))
        hash_funcs = max(1, min(rounded, 50))
        self._per_generation = (n_elements + 1) // 2
        filter_bits = math.ceil(
            -1.0
            * hash_funcs
            * (self._per_generation * 3)
            / math.log(1.0 - math.exp(log_fp_rate / hash_funcs))
        )
        self._words = (filter_bits + 63) // 64
        # Core's `data.size()`, the range `FastRange32` maps a hash to
        self._size = self._words * 2
        self._planes: tuple[array[int], array[int]] | None = None
        self._generation = 1
        self._this_generation = 0

        self._lanes = sum(1 << (64 * n) for n in range(hash_funcs))
        self._lane_mask = _MASK32 * self._lanes
        self._index_mask = (_MASK32 >> 1) * self._lanes
        self._bit_mask = 63 * self._lanes
        self._add = 0xE654_6B64 * self._lanes
        self._lane_bytes = 8 * hash_funcs
        self._unpack = struct.Struct("<" + "I4x" * hash_funcs).unpack
        self._set_tweak(tweak)

    def _set_tweak(self, tweak: int | None) -> None:
        """Seed the hash functions from `tweak`, drawn at random if `None`."""
        if tweak is None:
            tweak = secrets.randbits(32)
        # Core's `RollingBloomHash` seeds hash function n with this
        self._seeds = sum(
            ((n * 0xFBA4_C795 + tweak) & _MASK32) << (64 * n)
            for n in range(self._lane_bytes // 8)
        )

    def reset(self, *, tweak: int | None = None) -> None:
        """Forget every key, as Core's `reset` does, with a new tweak.

        The tweak is `tweak` where given, drawn at random otherwise.

        The words are freed, and allocated again at the next `add`.
        """
        self._set_tweak(tweak)
        self._planes = None
        self._generation = 1
        self._this_generation = 0

    def _hashes(self, key: bytes) -> int:
        """Return MurmurHash3 of `key` under every seed, lane n under seed n."""
        lanes = self._lanes
        lane_mask = self._lane_mask
        h = self._seeds
        size = len(key)
        body = size & ~3
        for (block,) in struct.iter_unpack("<I", key[:body]):
            k1 = (block * _C1) & _MASK32
            k1 = ((k1 << 15) | (k1 >> 17)) & _MASK32
            h ^= ((k1 * _C2) & _MASK32) * lanes
            h = ((h << 13) | (h >> 19)) & lane_mask
            h = (h * 5 + self._add) & lane_mask
        if size & 3:
            k1 = (int.from_bytes(key[body:], "little") * _C1) & _MASK32
            k1 = ((k1 << 15) | (k1 >> 17)) & _MASK32
            h ^= ((k1 * _C2) & _MASK32) * lanes
        h ^= (size & _MASK32) * lanes
        h = (h ^ (h >> 16)) & lane_mask
        h = (h * 0x85EB_CA6B) & lane_mask
        h = (h ^ (h >> 13)) & lane_mask
        h = (h * 0xC2B2_AE35) & lane_mask
        return (h ^ (h >> 16)) & lane_mask

    def _positions(self, key: bytes) -> zip[tuple[int, int]]:
        """Return the word and the bit each hash function picks for `key`.

        The word is Core's `FastRange32(h, data.size()) >> 1`, the index of
        its pair, and the bit is `h & 0x3F`.
        """
        h = self._hashes(key)
        n_bytes = self._lane_bytes
        index = ((h * self._size) >> 33) & self._index_mask
        bit = h & self._bit_mask
        unpack = self._unpack
        return zip(
            unpack(index.to_bytes(n_bytes, "little")),
            unpack(bit.to_bytes(n_bytes, "little")),
            strict=True,
        )

    def _next_generation(self, lo_words: array[int], hi_words: array[int]) -> None:
        """Start the next generation.

        It clears the bits the previous generation of that number set. Each
        plane is one int for the wipe, and is written back in place.
        """
        self._this_generation = 0
        self._generation = self._generation % 3 + 1
        lo = int.from_bytes(lo_words, sys.byteorder)
        hi = int.from_bytes(hi_words, sys.byteorder)
        # Core's masks, -1 being every bit set
        keep = (lo ^ -(self._generation & 1)) | (hi ^ -(self._generation >> 1))
        n_bytes = 8 * self._words
        memoryview(lo_words).cast("B")[:] = (lo & keep).to_bytes(n_bytes, sys.byteorder)
        memoryview(hi_words).cast("B")[:] = (hi & keep).to_bytes(n_bytes, sys.byteorder)

    def add(self, key: bytes) -> None:
        """Insert `key`, Core's `insert`."""
        if self._planes is None:
            # repeated rather than built from bytes, which over-allocates
            self._planes = (
                array("Q", [0]) * self._words,
                array("Q", [0]) * self._words,
            )
        lo, hi = self._planes
        if self._this_generation == self._per_generation:
            self._next_generation(lo, hi)
        self._this_generation += 1
        set_lo = self._generation & 1
        set_hi = self._generation >> 1
        for index, bit in self._positions(key):
            clear = ~(1 << bit)
            lo[index] = (lo[index] & clear) | (set_lo << bit)
            hi[index] = (hi[index] & clear) | (set_hi << bit)

    def __contains__(self, key: bytes) -> bool:
        """Answer whether `key` may have been inserted, Core's `contains`."""
        if self._planes is None:
            return False
        lo, hi = self._planes
        for index, bit in self._positions(key):
            if not ((lo[index] | hi[index]) >> bit) & 1:
                return False
        return True
