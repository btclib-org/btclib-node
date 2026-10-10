# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`TxOrphanage`, the transactions peers sent whose inputs are not found yet.

Bitcoin Core's `TxOrphanage` (`src/node/txorphanage.h` and
`src/node/txorphanage.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag),
whose header comment argues the design. An orphan cannot be told from a
bad transaction with inputs that do not exist, so what is kept is bounded:

- An announcement is one `(wtxid, peer)` pair. A transaction announced by
  several peers is stored once, and stays while any of them announces it.
- A transaction over `MAX_STANDARD_TX_WEIGHT` is never stored.
- Each peer is held to an equal share of two global limits, a latency
  score (one per announcement, one more per ten inputs) and a weight. A
  peer may exceed its share while the global limits hold. Once either is
  exceeded, the peer furthest over its share loses its oldest
  announcement, ones not waiting to be reconsidered first, until the
  limits hold again. No peer's announcements cause another's eviction
  while it stays within its share.

Where this differs from Core's: the two indexes of its multi-index
container are dicts, `_by_wtxid` and `_by_peer`, and the order Core's second
one keeps, by peer, then whether the announcement is to be reconsidered,
then recency, is sorted where it is read. The orphan list is ordered by
wtxid as Core's is, which is the order of the bytes `uint256` holds and so
of the reversed display bytes this tree keeps.

Everything here is reached from `Node`'s thread alone: the p2p callbacks,
`DownloadManager` and the RPC `getorphantxs` all run on it.
"""

import random
from fractions import Fraction
from typing import TYPE_CHECKING

from btclib.policy import MAX_STANDARD_TX_WEIGHT

if TYPE_CHECKING:
    from btclib.block import Block
    from btclib.tx.tx import Tx

__all__ = [
    "DEFAULT_MAX_ORPHANAGE_LATENCY_SCORE",
    "DEFAULT_RESERVED_ORPHAN_WEIGHT_PER_PEER",
    "TxOrphanage",
]

# `DEFAULT_RESERVED_ORPHAN_WEIGHT_PER_PEER` and
# `DEFAULT_MAX_ORPHANAGE_LATENCY_SCORE` (`src/node/txorphanage.h`, same tag):
# the weight each peer may hold, which the global weight limit is that many
# times the number of peers, and the latency score all peers share.
DEFAULT_RESERVED_ORPHAN_WEIGHT_PER_PEER = 404_000
DEFAULT_MAX_ORPHANAGE_LATENCY_SCORE = 3000

_RNG = random.SystemRandom()

# A peer's use over its share, and the share negated. Core orders these
# `FeeFrac`s by ratio, then by the smaller denominator, so at the same ratio
# a peer over its latency share is worse than one over its weight share.
type _Score = tuple[Fraction, int]


class _Announcement:
    """One peer's announcement of one orphan."""

    __slots__ = (
        "announcer",
        "latency_score",
        "reconsider",
        "sequence",
        "tx",
        "weight",
        "wtxid",
    )

    def __init__(self, tx: Tx, wtxid: bytes, announcer: int, sequence: int) -> None:
        self.tx = tx
        self.wtxid = wtxid
        self.announcer = announcer
        self.sequence = sequence
        # Core's `GetMemUsage` and `GetLatencyScore`: the weight, and one
        # plus one per ten inputs
        self.weight = tx.weight
        self.latency_score = 1 + len(tx.vin) // 10
        # Core's `m_reconsider`: set when a parent is accepted, cleared when
        # the peer takes the orphan up. Set for at most one announcement of a
        # wtxid.
        self.reconsider = False


class _PeerUse:
    """What one peer's announcements use, Core's `PeerDoSInfo`."""

    __slots__ = ("count", "latency_score", "weight")

    def __init__(self) -> None:
        self.weight = 0
        self.count = 0
        self.latency_score = 0

    def dos_score(self, max_latency_score: int, max_weight: int) -> _Score:
        """Return the larger of the peer's latency and weight ratios."""
        latency = (Fraction(self.latency_score, max_latency_score), -max_latency_score)
        weight = (Fraction(self.weight, max_weight), -max_weight)
        return max(latency, weight)


class TxOrphanage:
    """The orphans kept, each with the peers that announced it.

    `max_global_latency_score` and `reserved_peer_weight` are Core's two
    limits, as `MakeTxOrphanage` takes them, and `rng` picks which
    announcer is to reconsider an orphan.
    """

    def __init__(
        self,
        max_global_latency_score: int = DEFAULT_MAX_ORPHANAGE_LATENCY_SCORE,
        reserved_peer_weight: int = DEFAULT_RESERVED_ORPHAN_WEIGHT_PER_PEER,
        rng: random.Random = _RNG,
    ) -> None:
        """Start empty."""
        self._max_global_latency_score = max_global_latency_score
        self._reserved_peer_weight = reserved_peer_weight
        self._rng = rng
        self._sequence = 0
        # wtxid -> announcer -> announcement, and announcer -> wtxid ->
        # announcement in the order announced
        self._by_wtxid: dict[bytes, dict[int, _Announcement]] = {}
        self._by_peer: dict[int, dict[bytes, _Announcement]] = {}
        # Core's `m_outpoint_to_orphan_wtxids`: (txid, vout) spent -> wtxids
        self._spenders: dict[tuple[bytes, int], set[bytes]] = {}
        # Core's `m_reconsiderable_wtxids`, with the announcer marked
        self._reconsiderable: dict[bytes, int] = {}
        self._use: dict[int, _PeerUse] = {}
        self._announcements = 0
        # across unique wtxids: their weight, and their latency scores less
        # the one each announcement carries
        self._unique_weight = 0
        self._unique_input_scores = 0

    @property
    def unique_count(self) -> int:
        """Return how many transactions are kept, each once."""
        return len(self._by_wtxid)

    @property
    def announcement_count(self) -> int:
        """Return how many announcements there are, one per peer and wtxid."""
        return self._announcements

    def peers(self) -> list[int]:
        """Return the peers with an announcement."""
        return list(self._by_peer)

    def have_tx(self, wtxid: bytes) -> bool:
        """Return whether `wtxid` is kept."""
        return wtxid in self._by_wtxid

    def have_tx_from_peer(self, wtxid: bytes, peer: int) -> bool:
        """Return whether `peer` announced `wtxid`."""
        return peer in self._by_wtxid.get(wtxid, {})

    def get_tx(self, wtxid: bytes) -> Tx | None:
        """Return the transaction kept under `wtxid`, if any."""
        announcements = self._by_wtxid.get(wtxid)
        if announcements is None:
            return None
        return next(iter(announcements.values())).tx

    def add_tx(self, tx: Tx, peer: int) -> bool:
        """Keep `tx` as announced by `peer`, and answer whether it is new.

        Core's `AddTx`: not stored over `MAX_STANDARD_TX_WEIGHT`, and `False`
        for a wtxid already kept, whether `peer` announced it before or
        not. It trims on every add, as Core's does (`LimitOrphans` at the end
        of `AddTx`, `src/node/txorphanage.cpp` at bitcoin/bitcoin@9be056a8a7,
        the v31.1 tag).
        """
        if tx.weight > MAX_STANDARD_TX_WEIGHT:
            return False
        wtxid = tx.hash
        brand_new = wtxid not in self._by_wtxid
        if not self._announce(tx, wtxid, peer):
            return False
        if brand_new:
            for tx_in in tx.vin:
                key = (tx_in.prev_out.tx_id, tx_in.prev_out.vout)
                self._spenders.setdefault(key, set()).add(wtxid)
            announcement = self._by_wtxid[wtxid][peer]
            self._unique_weight += announcement.weight
            self._unique_input_scores += announcement.latency_score - 1
        self._limit_orphans()
        return brand_new

    def add_announcer(self, wtxid: bytes, peer: int) -> bool:
        """Add `peer` as an announcer of `wtxid`, which has to be kept.

        Core's `AddAnnouncer`: `False` where `wtxid` is not kept or `peer`
        announced it already.
        """
        tx = self.get_tx(wtxid)
        if tx is None or not self._announce(tx, wtxid, peer):
            return False
        self._limit_orphans()
        return True

    def get_tx_to_reconsider(self, peer: int) -> Tx | None:
        """Take the oldest transaction `peer` is to reconsider, if any.

        Core's `GetTxToReconsider`: the announcement stops being one to
        reconsider, whether or not the transaction stays kept.
        """
        announcement = self._next_to_reconsider(peer)
        if announcement is None:
            return None
        announcement.reconsider = False
        del self._reconsiderable[announcement.wtxid]
        return announcement.tx

    def peers_to_reconsider(self) -> list[int]:
        """Return the peers with a transaction to reconsider."""
        return sorted(set(self._reconsiderable.values()))

    def have_tx_to_reconsider(self, peer: int) -> bool:
        """Answer whether `peer` has a transaction to reconsider."""
        return peer in self._reconsiderable.values()

    def erase_tx(self, wtxid: bytes) -> bool:
        """Remove `wtxid` and its announcements; answer whether it was kept."""
        erased = self._erase_wtxid(wtxid)
        self._limit_orphans()
        return erased

    def erase_for_peer(self, peer: int) -> None:
        """Remove `peer`'s announcements, and the orphans only it announced."""
        for announcement in list(self._by_peer.get(peer, {}).values()):
            self._erase(announcement)
        self._limit_orphans()

    def erase_for_block(self, block: Block) -> None:
        """Remove the orphans a block includes or spends an input of."""
        if not self._by_wtxid:
            return
        wtxids: set[bytes] = set()
        for tx in block.transactions:
            for tx_in in tx.vin:
                key = (tx_in.prev_out.tx_id, tx_in.prev_out.vout)
                wtxids.update(self._spenders.get(key, ()))
        for wtxid in wtxids:
            self._erase_wtxid(wtxid)
        self._limit_orphans()

    def add_children_to_work_set(self, tx: Tx) -> list[tuple[bytes, int]]:
        """Mark the orphans spending `tx` to be reconsidered; answer who by.

        Core's `AddChildrenToWorkSet`: one announcer of each, picked at
        random so that a peer cannot hold an orphan back by its choice of
        what to announce, and none for one already marked.
        """
        marked: list[tuple[bytes, int]] = []
        txid = tx.id
        for vout in range(len(tx.vout)):
            for wtxid in sorted(
                self._spenders.get((txid, vout), ()), key=lambda w: w[::-1]
            ):
                if wtxid in self._reconsiderable:
                    continue
                announcers = self._by_wtxid[wtxid]
                peer = self._rng.choice(sorted(announcers))
                announcers[peer].reconsider = True
                self._reconsiderable[wtxid] = peer
                marked.append((wtxid, peer))
        return marked

    def get_children_from_same_peer(self, parent: Tx, peer: int) -> list[Tx]:
        """Return what `peer` announced that spends `parent`, newest first.

        Core's `GetChildrenFromSamePeer`: orphans to be reconsidered come
        before the others.
        """
        parent_txid = parent.id
        announcements = sorted(
            self._by_peer.get(peer, {}).values(),
            key=lambda a: (a.reconsider, a.sequence),
            reverse=True,
        )
        return [
            a.tx
            for a in announcements
            if any(tx_in.prev_out.tx_id == parent_txid for tx_in in a.tx.vin)
        ]

    def get_orphan_transactions(self) -> list[tuple[Tx, list[int]]]:
        """Return each orphan with its announcers, ordered by wtxid.

        Core's `GetOrphanTransactions`, from which `getorphantxs` answers.
        """
        return [
            (next(iter(announcers.values())).tx, sorted(announcers))
            for _, announcers in sorted(
                self._by_wtxid.items(), key=lambda item: item[0][::-1]
            )
        ]

    def _announce(self, tx: Tx, wtxid: bytes, peer: int) -> bool:
        """Record `peer`'s announcement of `tx`, unless it is recorded."""
        announcers = self._by_wtxid.setdefault(wtxid, {})
        if peer in announcers:
            return False
        announcement = _Announcement(tx, wtxid, peer, self._sequence)
        self._sequence += 1
        announcers[peer] = announcement
        self._by_peer.setdefault(peer, {})[wtxid] = announcement
        use = self._use.setdefault(peer, _PeerUse())
        use.weight += announcement.weight
        use.count += 1
        use.latency_score += announcement.latency_score
        self._announcements += 1
        return True

    def _next_to_reconsider(self, peer: int) -> _Announcement | None:
        """Return `peer`'s oldest announcement marked to be reconsidered."""
        marked = (
            self._by_wtxid[wtxid][peer]
            for wtxid, marked_peer in self._reconsiderable.items()
            if marked_peer == peer
        )
        return min(marked, key=lambda a: a.sequence, default=None)

    def _erase_wtxid(self, wtxid: bytes) -> bool:
        """Remove every announcement of `wtxid`, without limiting the rest."""
        announcements = self._by_wtxid.get(wtxid)
        if announcements is None:
            return False
        for announcement in list(announcements.values()):
            self._erase(announcement)
        return True

    def _erase(self, announcement: _Announcement) -> None:
        """Remove one announcement, and the orphan with its last one."""
        wtxid, peer = announcement.wtxid, announcement.announcer
        use = self._use[peer]
        use.weight -= announcement.weight
        use.count -= 1
        use.latency_score -= announcement.latency_score
        self._announcements -= 1
        if not use.count:
            del self._use[peer]
        del self._by_peer[peer][wtxid]
        if not self._by_peer[peer]:
            del self._by_peer[peer]
        announcers = self._by_wtxid[wtxid]
        del announcers[peer]
        if not announcers:
            del self._by_wtxid[wtxid]
            self._unique_weight -= announcement.weight
            self._unique_input_scores -= announcement.latency_score - 1
            for tx_in in announcement.tx.vin:
                key = (tx_in.prev_out.tx_id, tx_in.prev_out.vout)
                self._spenders[key].discard(wtxid)
                if not self._spenders[key]:
                    del self._spenders[key]
        if announcement.reconsider:
            del self._reconsiderable[wtxid]

    def _max_peer_latency_score(self) -> int:
        """Return each peer's share of the latency score."""
        return self._max_global_latency_score // max(len(self._use), 1)

    def _needs_trim(self) -> bool:
        """Return whether a global limit is exceeded."""
        latency_score = self._unique_input_scores + self._announcements
        max_weight = self._reserved_peer_weight * max(len(self._use), 1)
        return (
            latency_score > self._max_global_latency_score
            or self._unique_weight > max_weight
        )

    def _limit_orphans(self) -> None:
        """Evict until the global limits hold, as Core's `LimitOrphans`.

        The shares are those of the peers at the start, as in Core, though
        evicting a peer's last announcement changes how many peers there are.
        """
        max_latency_score = self._max_peer_latency_score()
        max_weight = self._reserved_peer_weight
        over: dict[int, _Score] = {}
        for peer, use in self._use.items():
            score = use.dos_score(max_latency_score, max_weight)
            if score[0] > 1:
                over[peer] = score
        while self._needs_trim():
            # the highest score, the newer peer on a tie
            worst = max(over, key=lambda peer: (over[peer], peer))
            del over[worst]
            threshold = max(over.values(), default=(Fraction(1), -1))
            oldest_first = iter(
                sorted(
                    self._by_peer[worst].values(),
                    key=lambda a: (a.reconsider, a.sequence),
                )
            )
            while self._needs_trim():
                self._erase(next(oldest_first))
                remaining = self._use.get(worst)
                if remaining is None:
                    break
                score = remaining.dos_score(max_latency_score, max_weight)
                if score <= threshold:
                    over[worst] = score
                    break
