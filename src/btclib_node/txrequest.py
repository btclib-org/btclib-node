# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`TxRequestTracker`, which peer is asked for an announced transaction.

Bitcoin Core's `TxRequestTracker` (`src/txrequest.h` and
`src/txrequest.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), whose
header comment argues the design. Each `(peer, txhash)` pair a peer
announced is an announcement in one of five states:

- `DELAYED`: announced, and not to be asked for before its `reqtime`.
- `READY`: past its `reqtime`, and not the peer to ask.
- `BEST`: past its `reqtime`, and the peer to ask. There is at most one
  per `txhash`, and none while a `REQUESTED` one exists.
- `REQUESTED`: asked for, until its expiry.
- `COMPLETED`: expired, answered, or failed. It stays so that the peer is
  not asked again, and goes with the last announcement of its `txhash`
  that is not `COMPLETED`.

The `BEST` among the `READY` ones is the preferred peer, then the highest
`siphash` of the `txhash` and the peer under a key drawn at start-up.

Where this differs from Core's: time is a plain float of seconds, and
`READY` and `BEST` announcements are found by a scan of the announcers of
one `txhash`, where Core keeps three ordered indexes. A peer's
announcements are also kept per peer. The state machine is the same.

Everything here is reached from `Node`'s thread alone: the p2p callbacks
and `DownloadManager.step` both run on it.
"""

import heapq
import itertools
import secrets
from dataclasses import dataclass, field
from enum import Enum, auto

from btclib.hashes import siphash

__all__ = ["TxRequestTracker"]

_MASK64 = (1 << 64) - 1
_COMPACT_SLACK = 64


class _State(Enum):
    DELAYED = auto()
    READY = auto()
    BEST = auto()
    REQUESTED = auto()
    COMPLETED = auto()


_WAITING = frozenset({_State.DELAYED, _State.REQUESTED})


@dataclass(slots=True, eq=False)
class _Announcement:
    """What one peer announced of one `txhash`."""

    txhash: bytes
    peer: int
    preferred: bool
    sequence: int
    # the `reqtime` of a `DELAYED` announcement, the expiry of a
    # `REQUESTED` one, and what it was last of either otherwise
    time: float
    state: _State = _State.DELAYED
    alive: bool = True
    # Core's `GenTxid` tells a txid from a wtxid: `txhash` is a txid even
    # for a peer that relays wtxids, where an orphan's parent is asked for
    txid: bool = False


@dataclass(slots=True)
class _Peer:
    """The announcements of one peer, and the counts Core keeps beside them."""

    announcements: dict[bytes, _Announcement] = field(default_factory=dict)
    best: dict[bytes, _Announcement] = field(default_factory=dict)
    requested: int = 0
    completed: int = 0


class TxRequestTracker:
    """Track the transactions peers announced and decide who is asked.

    `deterministic` zeroes the key that breaks ties, for tests.
    """

    def __init__(self, *, deterministic: bool = False) -> None:
        """Start empty, with a tie-breaking key of its own."""
        self._k0, self._k1 = (
            (0, 0) if deterministic else (secrets.randbits(64), secrets.randbits(64))
        )
        self._sequence = 0
        self._peers: dict[int, _Peer] = {}
        self._by_hash: dict[bytes, dict[int, _Announcement]] = {}
        # (time, tie, announcement) of the `DELAYED` and `REQUESTED`
        # announcements, which are what time moves. An entry whose
        # announcement has since changed is skipped when it is reached,
        # and dropped sooner by `_compact` once stale entries outnumber the
        # live ones, which `_waiting` counts.
        self._events: list[tuple[float, int, _Announcement]] = []
        self._waiting = 0
        self._tie = itertools.count()
        self._last_now = float("-inf")

    def received_inv(
        self,
        peer: int,
        txhash: bytes,
        *,
        preferred: bool,
        reqtime: float,
        txid: bool = False,
    ) -> None:
        """Add a `DELAYED` announcement, unless `peer` has announced `txhash`.

        Core's `ReceivedInv`: a second announcement of the same pair is
        ignored in whatever state the first is, so a peer cannot get a
        second chance at being asked. `txid` says `txhash` is a txid and
        not a wtxid, whatever the peer relays.
        """
        known = self._peers.get(peer)
        if known is not None and txhash in known.announcements:
            return
        announcement = _Announcement(
            txhash, peer, preferred, self._sequence, reqtime, txid=txid
        )
        self._sequence += 1
        self._peers.setdefault(peer, _Peer()).announcements[txhash] = announcement
        self._waiting += 1
        self._by_hash.setdefault(txhash, {})[peer] = announcement
        self._schedule(announcement)

    def disconnected_peer(self, peer: int) -> None:
        """Forget every announcement of `peer`, handing its requests on."""
        known = self._peers.get(peer)
        if known is None:
            return
        for announcement in list(known.announcements.values()):
            if self._make_completed(announcement):
                self._erase(announcement)

    def forget_tx_hash(self, txhash: bytes) -> None:
        """Forget every announcement of `txhash`, from every peer."""
        for announcement in list(self._by_hash.get(txhash, {}).values()):
            self._erase(announcement)

    def get_requestable(
        self, peer: int, now: float
    ) -> tuple[list[bytes], list[tuple[int, bytes]]]:
        """Return what to ask `peer` for at `now`, and the requests expired.

        Core's `GetRequestable`. Time moves to `now` first: a `REQUESTED`
        announcement expiring completes and hands its `txhash` to the next
        best announcer, and a `DELAYED` one past its `reqtime` becomes a
        candidate. What is returned is `peer`'s `BEST` announcements in the
        order they were announced, so that dependent transactions announced
        together are asked for in that order. Every expired request is
        returned, whichever peer it was made of.
        """
        expired = self._set_time_point(now)
        known = self._peers.get(peer)
        if known is None:
            return [], expired
        best = sorted(known.best.values(), key=lambda a: a.sequence)
        return [a.txhash for a in best], expired

    def requested_tx(self, peer: int, txhash: bytes, expiry: float) -> None:
        """Mark `txhash` as asked of `peer` until `expiry`.

        Core's `RequestedTx`. Without a candidate announcement of `peer`
        for it, nothing happens. Any other request for `txhash` still
        outstanding is completed, no longer being waited for.
        """
        known = self._peers.get(peer)
        announcement = None if known is None else known.announcements.get(txhash)
        if announcement is None or announcement.state in (
            _State.REQUESTED,
            _State.COMPLETED,
        ):
            return
        if announcement.state is not _State.BEST:
            selected = self._selected(txhash)
            if selected is not None:
                self._set_state(
                    selected,
                    _State.READY if selected.state is _State.BEST else _State.COMPLETED,
                )
        self._set_state(announcement, _State.REQUESTED)
        announcement.time = expiry
        self._schedule(announcement)

    def received_response(self, peer: int, txhash: bytes) -> None:
        """Complete `peer`'s announcement of `txhash`: a reply or a `notfound`.

        Core's `ReceivedResponse`.
        """
        known = self._peers.get(peer)
        announcement = None if known is None else known.announcements.get(txhash)
        if announcement is not None:
            self._make_completed(announcement)

    def is_txid(self, peer: int, txhash: bytes) -> bool:
        """Return whether `peer`'s announcement of `txhash` is of a txid."""
        known = self._peers.get(peer)
        announcement = None if known is None else known.announcements.get(txhash)
        return announcement is not None and announcement.txid

    def count_in_flight(self, peer: int) -> int:
        """Return how many requests to `peer` are outstanding."""
        known = self._peers.get(peer)
        return 0 if known is None else known.requested

    def count_candidates(self, peer: int) -> int:
        """Return how many announcements of `peer` are not asked for or done."""
        known = self._peers.get(peer)
        if known is None:
            return 0
        return len(known.announcements) - known.requested - known.completed

    def count(self, peer: int) -> int:
        """Return how many announcements of `peer` are tracked, in any state."""
        known = self._peers.get(peer)
        return 0 if known is None else len(known.announcements)

    def size(self) -> int:
        """Return how many announcements are tracked, across all peers."""
        return sum(len(known.announcements) for known in self._peers.values())

    def peers(self) -> list[int]:
        """Return the peers with an announcement tracked."""
        return list(self._peers)

    def get_candidate_peers(self, txhash: bytes) -> list[int]:
        """Return the peers with a live announcement of `txhash`."""
        return [
            a.peer
            for a in self._by_hash.get(txhash, {}).values()
            if a.state is not _State.COMPLETED
        ]

    def compute_priority(self, txhash: bytes, peer: int, *, preferred: bool) -> int:
        """Return the priority `peer`'s announcement of `txhash` is chosen by.

        The higher is asked first: the top bit is `preferred`, the other 63
        are a `siphash` of `txhash` and `peer`.
        """
        message = txhash + (peer & _MASK64).to_bytes(8, "little")
        return siphash(self._k0, self._k1, message) >> 1 | int(preferred) << 63

    def _priority(self, announcement: _Announcement) -> int:
        return self.compute_priority(
            announcement.txhash, announcement.peer, preferred=announcement.preferred
        )

    def _schedule(self, announcement: _Announcement) -> None:
        heapq.heappush(self._events, (announcement.time, next(self._tie), announcement))
        self._compact()

    def _compact(self) -> None:
        """Rebuild the events from the live announcements if stale ones pile up.

        Erasing or moving an announcement leaves its entry in the heap until
        its time comes, which a peer can make a minute away, over and over.
        Rebuilding costs one pass over the live ones and happens only after
        as many stale entries as live ones plus a constant have been left, so
        each stale entry pays O(1) amortized.
        """
        if len(self._events) <= 2 * self._waiting + _COMPACT_SLACK:
            return
        self._events = [
            (a.time, next(self._tie), a)
            for known in self._peers.values()
            for a in known.announcements.values()
            if a.state in _WAITING
        ]
        heapq.heapify(self._events)

    def _selected(self, txhash: bytes) -> _Announcement | None:
        """Return the `BEST` or `REQUESTED` announcement of `txhash`, if any."""
        for announcement in self._by_hash.get(txhash, {}).values():
            if announcement.state in (_State.BEST, _State.REQUESTED):
                return announcement
        return None

    def _set_state(self, announcement: _Announcement, state: _State) -> None:
        """Change the state, keeping the peer's counts and `BEST` table.

        A `COMPLETED` announcement is never changed, only erased.
        """
        old = announcement.state
        peer = self._peers[announcement.peer]
        self._waiting += (state in _WAITING) - (old in _WAITING)
        if old is _State.BEST:
            del peer.best[announcement.txhash]
        elif old is _State.REQUESTED:
            peer.requested -= 1
        announcement.state = state
        if state is _State.BEST:
            peer.best[announcement.txhash] = announcement
        elif state is _State.REQUESTED:
            peer.requested += 1
        elif state is _State.COMPLETED:
            peer.completed += 1

    def _erase(self, announcement: _Announcement) -> None:
        """Remove the announcement, and its peer and `txhash` with the last."""
        peer = self._peers[announcement.peer]
        self._waiting -= announcement.state in _WAITING
        if announcement.state is _State.BEST:
            del peer.best[announcement.txhash]
        elif announcement.state is _State.REQUESTED:
            peer.requested -= 1
        elif announcement.state is _State.COMPLETED:
            peer.completed -= 1
        del peer.announcements[announcement.txhash]
        if not peer.announcements:
            del self._peers[announcement.peer]
        holders = self._by_hash[announcement.txhash]
        del holders[announcement.peer]
        if not holders:
            del self._by_hash[announcement.txhash]
        announcement.alive = False

    def _promote(self, announcement: _Announcement) -> None:
        """Make a `DELAYED` announcement a candidate, and the best if it is.

        It is the best where nothing is selected for its `txhash`, or where
        the `BEST` there has a lower priority.
        """
        self._set_state(announcement, _State.READY)
        selected = self._selected(announcement.txhash)
        if selected is None:
            self._set_state(announcement, _State.BEST)
        elif selected.state is _State.BEST and self._priority(
            announcement
        ) > self._priority(selected):
            self._set_state(selected, _State.READY)
            self._set_state(announcement, _State.BEST)

    def _change_and_reselect(self, announcement: _Announcement, state: _State) -> None:
        """Move a selected announcement out of selection, and select the next.

        `state` is `COMPLETED` or `DELAYED`. The `READY` announcement of the
        highest priority, if there is one, becomes the `BEST`.
        """
        if announcement.state in (_State.BEST, _State.REQUESTED):
            ready = [
                a
                for a in self._by_hash[announcement.txhash].values()
                if a.state is _State.READY
            ]
            if ready:
                self._set_state(max(ready, key=self._priority), _State.BEST)
        self._set_state(announcement, state)

    def _make_completed(self, announcement: _Announcement) -> bool:
        """Complete the announcement, and return whether it still exists.

        The last announcement of a `txhash` that was not `COMPLETED` takes
        all of them with it.
        """
        if announcement.state is _State.COMPLETED:
            return True
        if all(
            a is announcement or a.state is _State.COMPLETED
            for a in self._by_hash[announcement.txhash].values()
        ):
            self.forget_tx_hash(announcement.txhash)
            return False
        self._change_and_reselect(announcement, _State.COMPLETED)
        return True

    def _set_time_point(self, now: float) -> list[tuple[int, bytes]]:
        """Bring the announcements to `now`, and return the requests expired.

        A `REQUESTED` announcement expiring is completed. A `DELAYED` one
        reaching its `reqtime` becomes a candidate. Where time went
        backwards, a candidate whose `reqtime` is again in the future
        returns to `DELAYED`.
        """
        if now < self._last_now:
            for known in list(self._peers.values()):
                for announcement in list(known.announcements.values()):
                    if (
                        announcement.state in (_State.READY, _State.BEST)
                        and announcement.time > now
                    ):
                        self._change_and_reselect(announcement, _State.DELAYED)
                        self._schedule(announcement)
        self._last_now = now
        expired: list[tuple[int, bytes]] = []
        while self._events and self._events[0][0] <= now:
            time, _, announcement = heapq.heappop(self._events)
            if not announcement.alive or announcement.time != time:
                continue
            if announcement.state is _State.DELAYED:
                self._promote(announcement)
            elif announcement.state is _State.REQUESTED:
                expired.append((announcement.peer, announcement.txhash))
                self._make_completed(announcement)
        self._compact()
        return expired
