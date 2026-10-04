# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`TxRequestTracker`, against the rules of Core's `txrequest.h` and a model.

The first half is one test per rule the header of Core's `txrequest.h`
states. The second runs random operations through the tracker and through
`Model`, which holds the same rules as a plain table and works out who is
to be asked afresh at every query, and compares their answers. The
tracker's own invariants (`txrequest.cpp`'s `SanityCheck`) are checked
after every operation.
"""

import random
from dataclasses import dataclass

import pytest

from btclib_node.txrequest import TxRequestTracker, _State


def a_hash(n: int) -> bytes:
    """Build a distinct, deterministic 32-byte hash from a small integer."""
    return n.to_bytes(32, "big")


def a_tracker() -> TxRequestTracker:
    """Build a tracker whose tie-break key is zero, as Core's test one."""
    return TxRequestTracker(deterministic=True)


def requestable(tracker: TxRequestTracker, peer: int, now: float) -> list[bytes]:
    """Return what `tracker` says `peer` is to be asked for at `now`."""
    hashes, _ = tracker.get_requestable(peer, now)
    tracker_is_consistent(tracker)
    return hashes


def best_of(tracker: TxRequestTracker, peers: list[int], txhash: bytes) -> int:
    """Return the peer with the highest priority, none being preferred."""
    return max(
        peers, key=lambda p: tracker.compute_priority(txhash, p, preferred=False)
    )


def tracker_is_consistent(tracker: TxRequestTracker) -> None:
    """Check what Core's `SanityCheck` checks, in the tracker's tables."""
    waiting = sum(
        a.state in (_State.DELAYED, _State.REQUESTED)
        for peer in tracker._peers.values()
        for a in peer.announcements.values()
    )
    assert tracker._waiting == waiting
    for peer_id, peer in tracker._peers.items():
        assert peer.announcements
        states = [a.state for a in peer.announcements.values()]
        assert peer.requested == states.count(_State.REQUESTED)
        assert peer.completed == states.count(_State.COMPLETED)
        assert set(peer.best) == {
            a.txhash for a in peer.announcements.values() if a.state is _State.BEST
        }
        for txhash, announcement in peer.announcements.items():
            assert announcement.peer == peer_id
            assert announcement.txhash == txhash
            assert announcement.alive
            assert tracker._by_hash[txhash][peer_id] is announcement
    for holders in tracker._by_hash.values():
        states = [a.state for a in holders.values()]
        # the last one that is not COMPLETED takes the others with it
        assert any(state is not _State.COMPLETED for state in states)
        selected = states.count(_State.BEST) + states.count(_State.REQUESTED)
        assert selected <= 1
        if _State.READY in states:
            assert selected == 1
        best = [a for a in holders.values() if a.state is _State.BEST]
        ready = [a for a in holders.values() if a.state is _State.READY]
        if best and ready:
            assert max(tracker._priority(a) for a in ready) <= tracker._priority(
                best[0]
            )
        for announcement in holders.values():
            if announcement.state in (_State.DELAYED, _State.REQUESTED):
                assert any(
                    event[2] is announcement and event[0] == announcement.time
                    for event in tracker._events
                )


def test_answered_requests_leave_no_events_behind() -> None:
    """A peer announcing, being asked and answering again and again.

    Each cycle leaves its `REQUESTED` entries stale for a minute, which must
    not pile up in `_events` while nothing is tracked.
    """
    tracker = a_tracker()
    now = 1_000.0
    for cycle in range(30):
        hashes = [a_hash(cycle * 1000 + n) for n in range(1000)]
        for h in hashes:
            tracker.received_inv(1, h, preferred=False, reqtime=now + 2)
        now += 2
        asked = requestable(tracker, 1, now)
        for h in asked:
            tracker.requested_tx(1, h, now + 60)
        for h in asked:
            tracker.received_response(1, h)
        assert len(tracker._events) <= 2 * tracker._waiting + 64 + 2 * 1000
    tracker.get_requestable(1, now)
    assert tracker.size() == 0
    assert len(tracker._events) <= 64


def test_an_announcement_is_asked_for_once_its_reqtime_has_passed() -> None:
    """`reqtime` is the earliest the transaction may be asked for."""
    tracker = a_tracker()
    tracker.received_inv(1, a_hash(1), preferred=True, reqtime=10.0)
    assert requestable(tracker, 1, 9.9) == []
    assert requestable(tracker, 1, 10.0) == [a_hash(1)]
    assert requestable(tracker, 1, 10.0) == [a_hash(1)]


def test_a_request_is_outstanding_until_its_expiry() -> None:
    """Asked for, it is not offered again, and expiry is reported once."""
    tracker = a_tracker()
    tracker.received_inv(1, a_hash(1), preferred=True, reqtime=0.0)
    assert requestable(tracker, 1, 0.0) == [a_hash(1)]
    tracker.requested_tx(1, a_hash(1), expiry=60.0)
    assert tracker.count_in_flight(1) == 1
    assert tracker.get_requestable(1, 59.9) == ([], [])
    assert tracker.get_requestable(1, 60.0) == ([], [(1, a_hash(1))])
    assert tracker.get_requestable(1, 61.0) == ([], [])
    # the only announcement, expired: nothing is left tracked
    assert tracker.size() == 0
    assert tracker.count_in_flight(1) == 0


@pytest.mark.parametrize("state", ["delayed", "due", "requested", "completed"])
def test_a_second_announcement_of_the_same_pair_is_ignored(state: str) -> None:
    """A peer cannot be given a second chance, whatever its state."""
    tracker = a_tracker()
    tracker.received_inv(1, a_hash(1), preferred=False, reqtime=5.0)
    tracker.received_inv(2, a_hash(1), preferred=False, reqtime=0.0)
    if state != "delayed":
        tracker.get_requestable(1, 5.0)
    if state == "requested":
        tracker.requested_tx(1, a_hash(1), expiry=90.0)
    if state == "completed":
        tracker.received_response(1, a_hash(1))
    before = (tracker.count(1), tracker.size(), tracker.count_in_flight(1))
    tracker.received_inv(1, a_hash(1), preferred=True, reqtime=0.0)
    tracker_is_consistent(tracker)
    assert (tracker.count(1), tracker.size(), tracker.count_in_flight(1)) == before


def test_a_preferred_peer_beats_a_non_preferred_one_whatever_the_order() -> None:
    """Non-preferred peers are not considered while a preferred one is."""
    tracker = a_tracker()
    for peers in [(1, 2), (2, 1)]:
        tracker = a_tracker()
        for peer in peers:
            tracker.received_inv(peer, a_hash(1), preferred=peer == 2, reqtime=0.0)
        assert requestable(tracker, 1, 0.0) == []
        assert requestable(tracker, 2, 0.0) == [a_hash(1)]


def test_a_later_better_candidate_takes_the_place_of_the_best() -> None:
    """A preferred announcement past its `reqtime` replaces a ready one."""
    tracker = a_tracker()
    tracker.received_inv(1, a_hash(1), preferred=False, reqtime=0.0)
    tracker.received_inv(2, a_hash(1), preferred=True, reqtime=5.0)
    assert requestable(tracker, 1, 1.0) == [a_hash(1)]
    assert requestable(tracker, 2, 1.0) == []
    assert requestable(tracker, 1, 5.0) == []
    assert requestable(tracker, 2, 5.0) == [a_hash(1)]


def test_a_later_worse_candidate_leaves_the_best_in_place() -> None:
    """A non-preferred announcement past its `reqtime` does not displace."""
    tracker = a_tracker()
    tracker.received_inv(1, a_hash(1), preferred=True, reqtime=0.0)
    tracker.received_inv(2, a_hash(1), preferred=False, reqtime=5.0)
    assert requestable(tracker, 1, 6.0) == [a_hash(1)]
    assert requestable(tracker, 2, 6.0) == []


def test_ties_between_equals_go_to_the_higher_priority() -> None:
    """Without preference, the higher `siphash` priority is asked."""
    tracker = a_tracker()
    peers = [1, 2, 3, 4]
    for peer in peers:
        tracker.received_inv(peer, a_hash(1), preferred=False, reqtime=0.0)
    winner = best_of(tracker, peers, a_hash(1))
    assert [p for p in peers if requestable(tracker, p, 0.0)] == [winner]


def test_the_priority_is_the_siphash_under_the_key_with_the_preference_on_top() -> None:
    """Core's `PriorityComputer`: `preferred` on top of a 63-bit hash."""
    tracker = a_tracker()
    low = tracker.compute_priority(a_hash(1), 3, preferred=False)
    high = tracker.compute_priority(a_hash(1), 3, preferred=True)
    assert low < 1 << 63
    assert high == low | 1 << 63
    assert tracker.compute_priority(a_hash(1), 4, preferred=False) != low
    assert TxRequestTracker().compute_priority(
        a_hash(1), 3, preferred=False
    ) != TxRequestTracker().compute_priority(a_hash(1), 3, preferred=False)


def test_the_next_announcer_is_asked_when_the_request_expires() -> None:
    """One request at a time; the next best takes over at expiry."""
    tracker = a_tracker()
    peers = [1, 2, 3]
    for peer in peers:
        tracker.received_inv(peer, a_hash(1), preferred=False, reqtime=0.0)
    first = best_of(tracker, peers, a_hash(1))
    assert requestable(tracker, first, 0.0) == [a_hash(1)]
    tracker.requested_tx(first, a_hash(1), expiry=60.0)
    others = [p for p in peers if p != first]
    assert not any(requestable(tracker, p, 59.0) for p in others)
    second = best_of(tracker, others, a_hash(1))
    assert requestable(tracker, second, 60.0) == [a_hash(1)]
    assert not requestable(tracker, first, 60.0)


def test_a_response_hands_the_transaction_on_and_the_last_one_leaves_nothing() -> None:
    """`notfound`, a reply, or a timeout completes; the last takes the rest."""
    tracker = a_tracker()
    for peer in (1, 2):
        tracker.received_inv(peer, a_hash(1), preferred=False, reqtime=0.0)
    first = best_of(tracker, [1, 2], a_hash(1))
    second = 3 - first
    assert requestable(tracker, first, 0.0)
    tracker.requested_tx(first, a_hash(1), expiry=60.0)
    tracker.received_response(first, a_hash(1))
    assert tracker.count(first) == 1
    assert tracker.count_candidates(first) == 0
    assert requestable(tracker, second, 0.0) == [a_hash(1)]
    tracker.requested_tx(second, a_hash(1), expiry=60.0)
    tracker.received_response(second, a_hash(1))
    assert tracker.size() == 0


def test_a_peer_asked_once_is_not_asked_again_after_it_announces_again() -> None:
    """Its completed announcement stays, so a repeated `inv` changes nothing."""
    tracker = a_tracker()
    for peer in (1, 2):
        tracker.received_inv(peer, a_hash(1), preferred=False, reqtime=0.0)
    first = best_of(tracker, [1, 2], a_hash(1))
    tracker.requested_tx(first, a_hash(1), expiry=60.0)
    tracker.received_response(first, a_hash(1))
    tracker.received_inv(first, a_hash(1), preferred=True, reqtime=0.0)
    assert not requestable(tracker, first, 0.0)
    assert tracker.get_candidate_peers(a_hash(1)) == [3 - first]


def test_forgetting_a_hash_forgets_it_for_every_peer() -> None:
    """`forget_tx_hash` leaves the other hashes alone."""
    tracker = a_tracker()
    for peer in (1, 2):
        tracker.received_inv(peer, a_hash(1), preferred=False, reqtime=0.0)
        tracker.received_inv(peer, a_hash(2), preferred=False, reqtime=0.0)
    tracker.forget_tx_hash(a_hash(1))
    tracker.forget_tx_hash(a_hash(3))
    tracker_is_consistent(tracker)
    assert tracker.size() == 2
    assert tracker.get_candidate_peers(a_hash(1)) == []
    assert sorted(tracker.get_candidate_peers(a_hash(2))) == [1, 2]


def test_a_disconnected_peer_hands_its_request_on_and_leaves_no_trace() -> None:
    """Its announcements go, and what it was asked for is asked of the next."""
    tracker = a_tracker()
    for peer in (1, 2):
        tracker.received_inv(peer, a_hash(1), preferred=False, reqtime=0.0)
    first = best_of(tracker, [1, 2], a_hash(1))
    tracker.received_inv(first, a_hash(2), preferred=False, reqtime=0.0)
    tracker.requested_tx(first, a_hash(1), expiry=60.0)
    tracker.disconnected_peer(first)
    tracker.disconnected_peer(first)
    tracker_is_consistent(tracker)
    assert tracker.peers() == [3 - first]
    assert requestable(tracker, 3 - first, 0.0) == [a_hash(1)]
    assert tracker.count(first) == 0


def test_a_disconnected_peer_with_a_completed_announcement_is_erased() -> None:
    """A `COMPLETED` announcement of the peer goes, the others stay."""
    tracker = a_tracker()
    for peer in (1, 2, 3):
        tracker.received_inv(peer, a_hash(1), preferred=False, reqtime=0.0)
    first = best_of(tracker, [1, 2, 3], a_hash(1))
    tracker.requested_tx(first, a_hash(1), expiry=60.0)
    tracker.received_response(first, a_hash(1))
    tracker.disconnected_peer(first)
    tracker_is_consistent(tracker)
    assert sorted(tracker.get_candidate_peers(a_hash(1))) == sorted(
        p for p in (1, 2, 3) if p != first
    )


def test_requestable_comes_in_announcement_order() -> None:
    """Dependent transactions announced together are asked for in that order."""
    tracker = a_tracker()
    order = [5, 3, 9, 1]
    for n in order:
        tracker.received_inv(1, a_hash(n), preferred=True, reqtime=0.0)
    assert requestable(tracker, 1, 0.0) == [a_hash(n) for n in order]


def test_requesting_what_nobody_announced_does_nothing() -> None:
    """`requested_tx` does nothing without a candidate of the peer."""
    tracker = a_tracker()
    tracker.requested_tx(1, a_hash(1), expiry=60.0)
    tracker.received_inv(2, a_hash(1), preferred=False, reqtime=0.0)
    tracker.requested_tx(1, a_hash(1), expiry=60.0)
    tracker_is_consistent(tracker)
    assert tracker.count_in_flight(1) == 0
    assert tracker.count_in_flight(2) == 0
    tracker.requested_tx(2, a_hash(1), expiry=60.0)
    # already requested: the later expiry is not taken
    tracker.requested_tx(2, a_hash(1), expiry=99.0)
    assert tracker.get_requestable(2, 59.0) == ([], [])
    assert tracker.get_requestable(2, 60.0) == ([], [(2, a_hash(1))])


def test_an_unexpected_request_completes_the_one_outstanding() -> None:
    """A request for a second peer completes the first one outstanding."""
    tracker = a_tracker()
    for peer in (1, 2):
        tracker.received_inv(peer, a_hash(1), preferred=False, reqtime=0.0)
    first = best_of(tracker, [1, 2], a_hash(1))
    tracker.requested_tx(first, a_hash(1), expiry=60.0)
    tracker.requested_tx(3 - first, a_hash(1), expiry=60.0)
    tracker_is_consistent(tracker)
    assert tracker.count_in_flight(first) == 0
    assert tracker.count_in_flight(3 - first) == 1


def test_an_unexpected_request_demotes_the_best_to_a_candidate() -> None:
    """Asking a peer that is not the best leaves the best a candidate."""
    tracker = a_tracker()
    peers = [1, 2, 3]
    for peer in peers:
        tracker.received_inv(peer, a_hash(1), preferred=False, reqtime=0.0)
    best = best_of(tracker, peers, a_hash(1))
    assert requestable(tracker, best, 0.0)
    other = next(p for p in peers if p != best)
    tracker.requested_tx(other, a_hash(1), expiry=60.0)
    tracker_is_consistent(tracker)
    assert not requestable(tracker, best, 1.0)
    assert tracker.count_candidates(best) == 1


def test_a_candidate_whose_time_comes_back_is_delayed_again() -> None:
    """If time goes backwards, the best is the next that is still due."""
    tracker = a_tracker()
    tracker.received_inv(1, a_hash(1), preferred=True, reqtime=10.0)
    tracker.received_inv(2, a_hash(1), preferred=False, reqtime=5.0)
    assert requestable(tracker, 1, 10.0) == [a_hash(1)]
    assert requestable(tracker, 2, 7.0) == [a_hash(1)]
    assert requestable(tracker, 1, 7.0) == []
    assert requestable(tracker, 1, 10.0) == [a_hash(1)]
    assert requestable(tracker, 2, 10.0) == []
    assert requestable(tracker, 2, 1.0) == []
    assert requestable(tracker, 1, 10.0) == [a_hash(1)]


def test_the_counts_follow_the_states() -> None:
    """Core's `CountInFlight`, `CountCandidates`, `Count` and `Size`."""
    tracker = a_tracker()
    assert (tracker.count(1), tracker.count_in_flight(1)) == (0, 0)
    assert tracker.count_candidates(1) == 0
    for n in range(4):
        tracker.received_inv(1, a_hash(n), preferred=True, reqtime=0.0)
    tracker.received_inv(2, a_hash(0), preferred=True, reqtime=0.0)
    assert (tracker.count(1), tracker.count_candidates(1)) == (4, 4)
    tracker.requested_tx(1, a_hash(0), expiry=60.0)
    tracker.requested_tx(1, a_hash(1), expiry=60.0)
    tracker.received_inv(3, a_hash(1), preferred=True, reqtime=0.0)
    tracker.received_response(1, a_hash(1))
    assert tracker.count(1) == 4
    assert tracker.count_in_flight(1) == 1
    assert tracker.count_candidates(1) == 2
    assert tracker.size() == 6
    assert tracker.peers() == [1, 2, 3]


@dataclass
class Row:
    """An announcement as `Model` holds it; `time` is `reqtime` or expiry."""

    phase: str
    time: float
    preferred: bool
    sequence: int


class Model:
    """The rules of `txrequest.h` as a table, with nothing incremental.

    An announcement is a candidate, requested, or completed. Who is to be
    asked is worked out from the table at every query: per hash, nothing
    while a request is outstanding, else the candidate past its `reqtime`
    of the highest priority.
    """

    def __init__(self, tracker: TxRequestTracker) -> None:
        """Take the priorities of `tracker`, which are not what is modelled."""
        self.priority = tracker.compute_priority
        self.rows: dict[tuple[int, bytes], Row] = {}
        self.sequence = 0

    def tidy(self) -> None:
        """Drop the hashes with nothing but completed announcements."""
        live = {h for (_, h), row in self.rows.items() if row.phase != "completed"}
        self.rows = {k: v for k, v in self.rows.items() if k[1] in live}

    def received_inv(
        self, peer: int, txhash: bytes, *, preferred: bool, reqtime: float
    ) -> None:
        """Add a candidate, unless the pair is known."""
        if (peer, txhash) not in self.rows:
            self.rows[peer, txhash] = Row(
                "candidate", reqtime, preferred, self.sequence
            )
            self.sequence += 1

    def requested_tx(self, peer: int, txhash: bytes, expiry: float) -> None:
        """Request a candidate, completing the request outstanding."""
        row = self.rows.get((peer, txhash))
        if row is None or row.phase != "candidate":
            return
        for (_, other_hash), other in self.rows.items():
            if other_hash == txhash and other.phase == "requested":
                other.phase = "completed"
        row.phase = "requested"
        row.time = expiry

    def received_response(self, peer: int, txhash: bytes) -> None:
        """Complete the announcement."""
        if (peer, txhash) in self.rows:
            self.rows[peer, txhash].phase = "completed"
            self.tidy()

    def forget_tx_hash(self, txhash: bytes) -> None:
        """Drop every announcement of the hash."""
        self.rows = {k: v for k, v in self.rows.items() if k[1] != txhash}

    def disconnected_peer(self, peer: int) -> None:
        """Drop every announcement of the peer."""
        self.rows = {k: v for k, v in self.rows.items() if k[0] != peer}
        self.tidy()

    def get_requestable(
        self, peer: int, now: float
    ) -> tuple[list[bytes], list[tuple[int, bytes]]]:
        """Expire, then work out whom each hash is to be asked of."""
        expired = sorted(
            (
                k
                for k, r in self.rows.items()
                if r.phase == "requested" and r.time <= now
            ),
            key=lambda k: (self.rows[k].time, self.rows[k].sequence),
        )
        for key in expired:
            self.rows[key].phase = "completed"
        self.tidy()
        chosen = []
        for txhash in {h for _, h in self.rows}:
            holders = {p: r for (p, h), r in self.rows.items() if h == txhash}
            if any(r.phase == "requested" for r in holders.values()):
                continue
            ready = {
                p: r
                for p, r in holders.items()
                if r.phase == "candidate" and r.time <= now
            }
            if not ready:
                continue
            best = max(
                ready,
                key=lambda p: self.priority(
                    txhash, p, preferred=bool(ready[p].preferred)
                ),
            )
            if best == peer:
                chosen.append((holders[peer].sequence, txhash))
        return [txhash for _, txhash in sorted(chosen)], list(expired)

    def count(self, peer: int) -> int:
        """Count the peer's announcements."""
        return sum(1 for p, _ in self.rows if p == peer)

    def count_in_flight(self, peer: int) -> int:
        """Count the peer's requested announcements."""
        return sum(
            1 for (p, _), r in self.rows.items() if p == peer and r.phase == "requested"
        )

    def count_candidates(self, peer: int) -> int:
        """Count the peer's candidate announcements."""
        return sum(
            1 for (p, _), r in self.rows.items() if p == peer and r.phase == "candidate"
        )

    def candidates(self, txhash: bytes) -> list[int]:
        """List the peers whose announcement of the hash is not completed."""
        return sorted(
            p
            for (p, h), r in self.rows.items()
            if h == txhash and r.phase != "completed"
        )


@dataclass
class Run:
    """One random operation sequence applied to the tracker and the model."""

    seed: int

    def __call__(self) -> None:
        """Run the sequence, comparing the two after every operation."""
        rng = random.Random(self.seed)
        tracker = a_tracker()
        model = Model(tracker)
        now = 0.0
        peers, hashes = range(1, 5), [a_hash(n) for n in range(5)]
        for _ in range(300):
            peer, txhash = rng.choice(peers), rng.choice(hashes)
            op = rng.choice(
                [
                    "inv",
                    "inv",
                    "inv",
                    "ask",
                    "ask",
                    "unasked",
                    "response",
                    "forget",
                    "gone",
                    "tick",
                ]
            )
            if op == "inv":
                preferred = rng.random() < 0.5
                reqtime = now + rng.choice([0, 0, 1, 3, 9])
                tracker.received_inv(peer, txhash, preferred=preferred, reqtime=reqtime)
                model.received_inv(peer, txhash, preferred=preferred, reqtime=reqtime)
            elif op == "ask":
                got = tracker.get_requestable(peer, now)
                wanted = model.get_requestable(peer, now)
                assert got[0] == wanted[0], (self.seed, now)
                assert sorted(got[1]) == sorted(wanted[1]), (self.seed, now)
                for h in got[0] if rng.random() < 0.8 else []:
                    expiry = now + rng.choice([1, 5, 60])
                    tracker.requested_tx(peer, h, expiry)
                    model.requested_tx(peer, h, expiry)
            elif op == "unasked":
                # a request `get_requestable` did not advise
                expiry = now + rng.choice([1, 5, 60])
                tracker.requested_tx(peer, txhash, expiry)
                model.requested_tx(peer, txhash, expiry)
            elif op == "response":
                tracker.received_response(peer, txhash)
                model.received_response(peer, txhash)
            elif op == "forget":
                tracker.forget_tx_hash(txhash)
                model.forget_tx_hash(txhash)
            elif op == "gone":
                tracker.disconnected_peer(peer)
                model.disconnected_peer(peer)
            else:
                now += rng.choice([0, 1, 2, 10, -3])
            tracker_is_consistent(tracker)
            assert tracker.count(peer) == model.count(peer)
            assert tracker.count_in_flight(peer) == model.count_in_flight(peer)
            assert tracker.count_candidates(peer) == model.count_candidates(peer)
            assert sorted(tracker.get_candidate_peers(txhash)) == model.candidates(
                txhash
            )


@pytest.mark.parametrize("seed", range(150))
def test_the_tracker_agrees_with_the_model_on_random_operations(seed: int) -> None:
    """Random operations, time running backwards too, give the same answers."""
    Run(seed)()
