# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`TxOrphanage`, against the rules of Core's `txorphanage.h` and a model.

The first half is one test per rule the header of Core's `txorphanage.h`
and `txorphanage.cpp` state. The second runs random operations through the
orphanage and holds its tables to what its announcements imply, which is
what `txorphanage.cpp`'s `SanityCheck` does after every operation.
"""

import random
import secrets
from collections import Counter
from fractions import Fraction
from typing import TYPE_CHECKING

from btclib.policy import MAX_STANDARD_TX_WEIGHT
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from btclib_node.orphanage import (
    DEFAULT_MAX_ORPHANAGE_LATENCY_SCORE,
    DEFAULT_RESERVED_ORPHAN_WEIGHT_PER_PEER,
    TxOrphanage,
)
from tests import anyone_can_spend, anyone_can_spend_script_sig

if TYPE_CHECKING:
    from collections.abc import Sequence


def a_tx(*spends: tuple[bytes, int], outputs: int = 1, padding: int = 0) -> Tx:
    """Build a transaction spending each `(txid, vout)`, heavier by `padding`.

    Each is told apart from another spending the same by a random
    `script_sig`, and `padding` is added to the first input's.
    """
    vin = [
        TxIn(
            prev_out=OutPoint(txid, vout),
            script_sig=anyone_can_spend_script_sig() + b"\x00" * padding,
            sequence=0xFFFFFFFF,
        )
        for txid, vout in spends
    ]
    vout = [TxOut(value=1000, script_pub_key=anyone_can_spend())] * outputs
    return Tx(version=1, lock_time=0, vin=vin, vout=vout)


def a_child(parent: Tx, vout: int = 0, **kwargs: int) -> Tx:
    """Build a transaction spending output `vout` of `parent`."""
    return a_tx((parent.id, vout), **kwargs)


def an_orphan(**kwargs: int) -> Tx:
    """Build a transaction spending an output nobody has."""
    return a_tx((secrets.token_bytes(32), 0), **kwargs)


def is_consistent(orphanage: TxOrphanage) -> None:
    """Check what Core's `SanityCheck` checks, in the orphanage's tables."""
    announcements = [a for peer in orphanage._by_peer.values() for a in peer.values()]
    assert orphanage.announcement_count == len(announcements)
    by_wtxid = {
        wtxid: set(announcers) for wtxid, announcers in orphanage._by_wtxid.items()
    }
    assert all(by_wtxid.values())
    assert Counter((a.wtxid, a.announcer) for a in announcements) == Counter(
        (w, p) for w, peers in by_wtxid.items() for p in peers
    )
    # per-peer use is the sum of the peer's announcements
    use: dict[int, tuple[int, int, int]] = {}
    for a in announcements:
        weight, count, score = use.get(a.announcer, (0, 0, 0))
        use[a.announcer] = (weight + a.weight, count + 1, score + a.latency_score)
    assert use == {
        peer: (u.weight, u.count, u.latency_score) for peer, u in orphanage._use.items()
    }
    # what is kept once: weight, input scores and who spends what
    unique = {a.wtxid: a for a in announcements}
    assert orphanage.unique_count == len(unique)
    assert orphanage._unique_weight == sum(a.weight for a in unique.values())
    assert orphanage._unique_input_scores == sum(
        a.latency_score - 1 for a in unique.values()
    )
    spenders: dict[tuple[bytes, int], set[bytes]] = {}
    for a in unique.values():
        for tx_in in a.tx.vin:
            key = (tx_in.prev_out.tx_id, tx_in.prev_out.vout)
            spenders.setdefault(key, set()).add(a.wtxid)
    assert orphanage._spenders == spenders
    # at most one announcement per wtxid is to be reconsidered, as marked
    marked = {a.wtxid: a.announcer for a in announcements if a.reconsider}
    assert orphanage._reconsiderable == marked
    assert sum(a.reconsider for a in announcements) == len(marked)
    # and the global limits hold
    assert not orphanage._needs_trim()


def test_an_orphan_is_kept_once_whoever_announces_it() -> None:
    """A wtxid announced by two peers is one orphan with two announcers."""
    orphanage = TxOrphanage()
    orphan = an_orphan()
    assert orphanage.add_tx(orphan, 1)
    assert not orphanage.add_tx(orphan, 2)
    assert not orphanage.add_tx(orphan, 2)
    assert orphanage.unique_count == 1
    assert orphanage.announcement_count == 2
    assert orphanage.have_tx(orphan.hash)
    assert orphanage.have_tx_from_peer(orphan.hash, 1)
    assert orphanage.have_tx_from_peer(orphan.hash, 2)
    assert not orphanage.have_tx_from_peer(orphan.hash, 3)
    assert orphanage.get_tx(orphan.hash) == orphan
    assert orphanage.peers() == [1, 2]
    is_consistent(orphanage)


def test_what_is_not_kept_is_not_found() -> None:
    """A wtxid never added has no transaction and no announcer."""
    orphanage = TxOrphanage()
    assert not orphanage.have_tx(b"\x01" * 32)
    assert not orphanage.have_tx_from_peer(b"\x01" * 32, 1)
    assert orphanage.get_tx(b"\x01" * 32) is None
    assert orphanage.get_orphan_transactions() == []


def test_a_transaction_over_the_standard_weight_is_not_kept() -> None:
    """Core refuses a large orphan to avoid a memory exhaustion attack."""
    orphanage = TxOrphanage()
    # the heaviest that is kept, and the next size up
    padding = (MAX_STANDARD_TX_WEIGHT - an_orphan().weight) // 4
    while an_orphan(padding=padding).weight > MAX_STANDARD_TX_WEIGHT:
        padding -= 1
    assert an_orphan(padding=padding + 1).weight > MAX_STANDARD_TX_WEIGHT
    heaviest = an_orphan(padding=padding)
    assert orphanage.add_tx(heaviest, 1)
    assert not orphanage.add_tx(an_orphan(padding=padding + 1), 1)
    assert orphanage.unique_count == 1
    is_consistent(orphanage)


def test_an_announcer_is_added_to_an_orphan_that_is_kept() -> None:
    """`add_announcer` adds a peer to what is kept, and to nothing else."""
    orphanage = TxOrphanage()
    orphan = an_orphan()
    assert not orphanage.add_announcer(orphan.hash, 2)
    orphanage.add_tx(orphan, 1)
    assert orphanage.add_announcer(orphan.hash, 2)
    assert not orphanage.add_announcer(orphan.hash, 2)
    assert orphanage.get_orphan_transactions() == [(orphan, [1, 2])]
    is_consistent(orphanage)


def test_an_orphan_stays_while_one_peer_announces_it() -> None:
    """Erasing a peer's announcements leaves what another announced."""
    orphanage = TxOrphanage()
    shared, mine = an_orphan(), an_orphan()
    orphanage.add_tx(shared, 1)
    orphanage.add_tx(mine, 1)
    orphanage.add_announcer(shared.hash, 2)
    orphanage.erase_for_peer(1)
    assert orphanage.have_tx(shared.hash)
    assert not orphanage.have_tx(mine.hash)
    assert orphanage.get_orphan_transactions() == [(shared, [2])]
    orphanage.erase_for_peer(1)
    orphanage.erase_for_peer(2)
    assert orphanage.unique_count == 0
    assert orphanage.peers() == []
    is_consistent(orphanage)


def test_erasing_a_wtxid_erases_every_announcement_of_it() -> None:
    """`erase_tx` answers whether anything was kept."""
    orphanage = TxOrphanage()
    orphan = an_orphan()
    orphanage.add_tx(orphan, 1)
    orphanage.add_announcer(orphan.hash, 2)
    assert orphanage.erase_tx(orphan.hash)
    assert not orphanage.erase_tx(orphan.hash)
    assert orphanage.announcement_count == 0
    is_consistent(orphanage)


def test_a_child_of_a_transaction_is_marked_for_one_of_its_announcers() -> None:
    """`add_children_to_work_set` picks an announcer at random, once."""
    parent = a_tx((secrets.token_bytes(32), 0), outputs=2)
    children = [a_child(parent, 0), a_child(parent, 1)]
    unrelated = an_orphan()
    picked = set()
    for seed in range(40):
        orphanage = TxOrphanage(rng=random.Random(seed))
        for child in children:
            orphanage.add_tx(child, 1)
            orphanage.add_announcer(child.hash, 2)
        orphanage.add_tx(unrelated, 1)
        marked = orphanage.add_children_to_work_set(parent)
        assert sorted(wtxid for wtxid, _ in marked) == sorted(c.hash for c in children)
        picked |= {peer for _, peer in marked}
        # an orphan marked is not marked again, whichever announcer
        assert orphanage.add_children_to_work_set(parent) == []
        assert orphanage.peers_to_reconsider() == sorted({p for _, p in marked})
        is_consistent(orphanage)
    assert picked == {1, 2}


def test_a_child_spending_two_outputs_is_marked_once() -> None:
    """The marks of the second output skip what the first marked."""
    parent = a_tx((secrets.token_bytes(32), 0), outputs=2)
    child = a_tx((parent.id, 0), (parent.id, 1))
    orphanage = TxOrphanage()
    orphanage.add_tx(child, 1)
    assert orphanage.add_children_to_work_set(parent) == [(child.hash, 1)]
    is_consistent(orphanage)


def test_a_peer_takes_up_what_is_marked_oldest_first_and_once() -> None:
    """`get_tx_to_reconsider` flips the mark back, as Core's does."""
    parent = a_tx((secrets.token_bytes(32), 0), outputs=2)
    first, second = a_child(parent, 0), a_child(parent, 1)
    orphanage = TxOrphanage()
    orphanage.add_tx(first, 1)
    orphanage.add_tx(second, 1)
    assert orphanage.peers_to_reconsider() == []
    assert orphanage.get_tx_to_reconsider(1) is None
    orphanage.add_children_to_work_set(parent)
    assert orphanage.peers_to_reconsider() == [1]
    assert orphanage.get_tx_to_reconsider(2) is None
    assert orphanage.get_tx_to_reconsider(1) == first
    is_consistent(orphanage)
    assert orphanage.get_tx_to_reconsider(1) == second
    assert orphanage.get_tx_to_reconsider(1) is None
    assert orphanage.peers_to_reconsider() == []
    # still kept, and not to be reconsidered until there is a new reason
    assert orphanage.have_tx(first.hash)
    assert orphanage.add_children_to_work_set(parent) != []
    is_consistent(orphanage)


def test_a_marked_orphan_erased_is_no_longer_marked() -> None:
    """Erasing the announcement that was marked drops the mark with it."""
    parent = a_tx((secrets.token_bytes(32), 0))
    child = a_child(parent)
    orphanage = TxOrphanage()
    orphanage.add_tx(child, 1)
    orphanage.add_children_to_work_set(parent)
    orphanage.erase_tx(child.hash)
    assert orphanage.peers_to_reconsider() == []
    is_consistent(orphanage)


def test_children_of_a_parent_come_newest_first_and_only_from_the_peer() -> None:
    """Orphans marked come first, then the rest, each newest first."""
    parent = a_tx((secrets.token_bytes(32), 0), outputs=3)
    old, middle, new = (a_child(parent, i) for i in range(3))
    other = a_child(parent, 0)
    unrelated = an_orphan()
    orphanage = TxOrphanage(rng=random.Random(1))
    for child in (old, middle, new):
        orphanage.add_tx(child, 1)
    orphanage.add_tx(other, 2)
    orphanage.add_tx(unrelated, 1)
    assert orphanage.get_children_from_same_peer(parent, 1) == [new, middle, old]
    assert orphanage.get_children_from_same_peer(parent, 2) == [other]
    assert orphanage.get_children_from_same_peer(parent, 3) == []
    # the marked one, whichever announcer, goes first for the peer it is for
    orphanage.add_children_to_work_set(a_tx((old.id, 0)))
    assert orphanage.get_children_from_same_peer(parent, 1) == [new, middle, old]
    marked = TxOrphanage()
    marked.add_tx(old, 1)
    marked.add_tx(new, 1)
    marked.add_children_to_work_set(a_tx((secrets.token_bytes(32), 0)))
    assert marked.get_children_from_same_peer(parent, 1) == [new, old]
    spent = a_tx((parent.id, 0), outputs=1)
    marked.add_children_to_work_set(spent)
    assert marked.get_children_from_same_peer(parent, 1) == [new, old]


def test_a_marked_child_comes_before_a_newer_one() -> None:
    """A child to be reconsidered is listed ahead of a newer one that is not."""
    parent = a_tx((secrets.token_bytes(32), 0), outputs=2)
    old, new = a_child(parent, 0), a_child(parent, 1)
    orphanage = TxOrphanage()
    orphanage.add_tx(old, 1)
    orphanage.add_tx(new, 1)
    orphanage.add_children_to_work_set(a_tx((secrets.token_bytes(32), 0)))
    # both are marked by `parent` being accepted
    grandparent_marks = orphanage.add_children_to_work_set(parent)
    assert {wtxid for wtxid, _ in grandparent_marks} == {old.hash, new.hash}
    assert orphanage.get_tx_to_reconsider(1) == old
    orphanage.add_tx(an_orphan(), 1)
    assert orphanage.get_children_from_same_peer(parent, 1) == [new, old]


def test_orphans_are_listed_by_the_bytes_core_holds_the_wtxid_as() -> None:
    """Core's order is the order of the reversed display bytes."""
    orphanage = TxOrphanage()
    orphans = [an_orphan() for _ in range(8)]
    for orphan in orphans:
        orphanage.add_tx(orphan, 1)
    listed = [tx.hash for tx, _ in orphanage.get_orphan_transactions()]
    assert listed == sorted(listed, key=lambda wtxid: wtxid[::-1])
    assert sorted(listed) != listed


class FakeBlock:
    """What `erase_for_block` reads of a block: its transactions."""

    def __init__(self, *txs: Tx) -> None:
        """Hold `txs`."""
        self.transactions = list(txs)


def test_a_block_erases_what_it_includes_and_what_it_conflicts_with() -> None:
    """Orphans spending an outpoint the block spends are erased, a copy too."""
    funding = (secrets.token_bytes(32), 0)
    included = a_tx(funding)
    conflicted = a_tx(funding)
    kept = an_orphan()
    orphanage = TxOrphanage()
    for orphan in (included, conflicted, kept):
        orphanage.add_tx(orphan, 1)
    orphanage.add_announcer(conflicted.hash, 2)
    orphanage.erase_for_block(FakeBlock(included))  # type: ignore[arg-type]
    assert [tx for tx, _ in orphanage.get_orphan_transactions()] == [kept]
    is_consistent(orphanage)
    orphanage.erase_for_block(FakeBlock(a_tx((b"\x07" * 32, 5))))  # type: ignore[arg-type]
    assert orphanage.unique_count == 1
    orphanage.erase_tx(kept.hash)
    orphanage.erase_for_block(FakeBlock(included))  # type: ignore[arg-type]
    assert orphanage.unique_count == 0


# a latency share of 10 announcements per peer and a weight share of two
# standard-size orphans per peer, small enough to reach with a few of them
def a_small_orphanage(peers_latency: int = 20, weight: int = 2000) -> TxOrphanage:
    """Build an orphanage whose limits a few transactions reach."""
    return TxOrphanage(
        max_global_latency_score=peers_latency,
        reserved_peer_weight=weight,
        rng=random.Random(0),
    )


def test_the_defaults_are_cores() -> None:
    """`DEFAULT_RESERVED_ORPHAN_WEIGHT_PER_PEER` and the latency score."""
    assert DEFAULT_RESERVED_ORPHAN_WEIGHT_PER_PEER == 404_000
    assert DEFAULT_MAX_ORPHANAGE_LATENCY_SCORE == 3000


def test_a_peer_over_the_limit_loses_its_oldest_announcements_first() -> None:
    """Past the global latency score, the one peer evicts from its own."""
    orphanage = a_small_orphanage(peers_latency=5, weight=10**9)
    orphans = [an_orphan() for _ in range(6)]
    for orphan in orphans:
        orphanage.add_tx(orphan, 1)
        is_consistent(orphanage)
    assert [tx for tx, _ in orphanage.get_orphan_transactions()] == sorted(
        orphans[1:], key=lambda tx: tx.hash[::-1]
    )


def test_a_peer_within_its_share_is_not_evicted_for_another() -> None:
    """The peer furthest over its share pays, and not the one within it."""
    orphanage = a_small_orphanage(peers_latency=8, weight=10**9)
    quiet = [an_orphan() for _ in range(2)]
    for orphan in quiet:
        orphanage.add_tx(orphan, 1)
    for _ in range(20):
        orphanage.add_tx(an_orphan(), 2)
        is_consistent(orphanage)
        assert all(orphanage.have_tx(orphan.hash) for orphan in quiet)
    assert orphanage._use[2].count == 6


def test_the_weight_is_limited_as_the_latency_score_is() -> None:
    """Past the global weight, the peer over its weight share pays."""
    heavy = an_orphan(padding=200)
    orphanage = a_small_orphanage(peers_latency=10**6, weight=heavy.weight * 2)
    stored = [an_orphan(padding=200) for _ in range(5)]
    for orphan in stored:
        orphanage.add_tx(orphan, 1)
        is_consistent(orphanage)
    assert orphanage._use[1].count == 2
    assert all(orphanage.have_tx(orphan.hash) for orphan in stored[-2:])


def test_a_transaction_with_many_inputs_costs_more_latency() -> None:
    """One more for every ten inputs."""
    orphanage = a_small_orphanage(peers_latency=10**6, weight=10**9)
    many = a_tx(*((secrets.token_bytes(32), 0) for _ in range(25)))
    orphanage.add_tx(many, 1)
    assert orphanage._use[1].latency_score == 3
    assert orphanage._unique_input_scores == 2


def test_the_limits_grow_with_the_peers() -> None:
    """A second peer doubles the weight limit and halves each latency share."""
    orphanage = a_small_orphanage(peers_latency=10, weight=3000)
    orphanage.add_tx(an_orphan(), 1)
    assert orphanage._max_peer_latency_score() == 10
    orphanage.add_tx(an_orphan(), 2)
    assert orphanage._max_peer_latency_score() == 5


def test_a_sticky_orphan_is_evicted_after_the_others() -> None:
    """Announcements to be reconsidered go after those that are not."""
    parent = a_tx((secrets.token_bytes(32), 0))
    child = a_child(parent)
    orphanage = a_small_orphanage(peers_latency=4, weight=10**9)
    orphanage.add_tx(child, 1)
    orphanage.add_children_to_work_set(parent)
    others = [an_orphan() for _ in range(4)]
    for orphan in others:
        orphanage.add_tx(orphan, 1)
        is_consistent(orphanage)
    assert orphanage.have_tx(child.hash)
    assert not orphanage.have_tx(others[0].hash)
    assert orphanage.have_tx(others[-1].hash)


def test_two_peers_over_their_shares_are_trimmed_in_turn() -> None:
    """The worst peer's evicted until the next worst is as bad, and so on."""
    orphanage = a_small_orphanage(peers_latency=12, weight=10**9)
    for peer in (1, 2, 3):
        for _ in range(4):
            orphanage.add_tx(an_orphan(), peer)
    is_consistent(orphanage)
    for _ in range(6):
        orphanage.add_tx(an_orphan(), 1)
        orphanage.add_tx(an_orphan(), 2)
        is_consistent(orphanage)
    assert orphanage._use[3].count == 4


def test_the_worst_peer_is_trimmed_only_down_to_the_next_worst() -> None:
    """Core's `dos_threshold`: two peers over their share are trimmed in turn.

    The shares are 10 each and the limit is 30. Peers 1, 2 and 3 hold 16, 14
    and 5, so 5 are to go. Peer 1 loses 2, down to peer 2's score. The tie
    is broken against peer 2, which loses one. Peer 1 then loses one, down to
    peer 2 again, and the tie costs peer 2 the last. That leaves 13, 12 and
    5. Trimming peer 1 alone would leave 11, 14 and 5.
    """
    orphanage = a_small_orphanage(peers_latency=10**6, weight=10**9)
    for peer, count in ((1, 16), (2, 14), (3, 5)):
        for _ in range(count):
            orphanage.add_tx(an_orphan(), peer)
    orphanage._max_global_latency_score = 30
    orphanage.erase_tx(b"\x01" * 32)
    is_consistent(orphanage)
    assert [orphanage._use[peer].count for peer in (1, 2, 3)] == [13, 12, 5]


def test_a_peer_evicted_to_nothing_leaves_the_others_over_the_limit_trimmed() -> None:
    """A peer whose last announcement goes no longer counts as a peer."""
    orphanage = a_small_orphanage(peers_latency=4, weight=10**9)
    for _ in range(3):
        orphanage.add_tx(an_orphan(), 1)
    orphanage.add_tx(an_orphan(), 2)
    for _ in range(3):
        orphanage.add_tx(an_orphan(), 3)
        is_consistent(orphanage)


def test_equal_scores_are_broken_by_the_newer_peer() -> None:
    """The peer with the larger id is the worse at equal ratios."""
    orphanage = a_small_orphanage(peers_latency=4, weight=10**9)
    orphanage.add_tx(an_orphan(), 5)
    orphanage.add_tx(an_orphan(), 5)
    orphanage.add_tx(an_orphan(), 9)
    orphanage.add_tx(an_orphan(), 9)
    orphanage.add_tx(an_orphan(), 9)
    assert orphanage._use[5].count == 2
    assert orphanage._use[9].count == 2


def test_a_latency_ratio_is_worse_than_a_weight_one_that_equals_it() -> None:
    """Equal ratios are ordered by the smaller share, as Core's `FeeFrac` is."""
    orphanage = a_small_orphanage(peers_latency=10, weight=1000)
    orphanage.add_tx(an_orphan(), 1)
    use = orphanage._use[1]
    latency = Fraction(use.latency_score, 10)
    assert use.dos_score(10, 1000)[0] == max(latency, Fraction(use.weight, 1000))
    use.weight, use.latency_score = 500, 5
    assert use.dos_score(10, 1000) == (Fraction(1, 2), -10)


def test_random_operations_keep_the_tables_consistent() -> None:
    """Whatever is added, marked and erased, the tables stay consistent."""
    rng = random.Random(7)
    orphanage = TxOrphanage(
        max_global_latency_score=30, reserved_peer_weight=1500, rng=random.Random(3)
    )
    pool: list[Tx] = []
    funding = [(secrets.token_bytes(32), i) for i in range(6)]
    for _ in range(600):
        action = rng.choice(
            ["add", "add", "announce", "mark", "take", "erase", "peer", "block"]
        )
        peer = rng.randrange(5)
        if action == "add":
            if pool and rng.random() < 0.3:
                parent = rng.choice(pool)
                spends: Sequence[tuple[bytes, int]] = [(parent.id, 0)]
            else:
                spends = rng.sample(funding, rng.randrange(1, 4))
            tx = a_tx(*spends, outputs=2, padding=rng.choice([0, 0, 100, 400]))
            pool.append(tx)
            orphanage.add_tx(tx, peer)
        elif action == "announce" and pool:
            orphanage.add_announcer(rng.choice(pool).hash, peer)
        elif action == "mark" and pool:
            orphanage.add_children_to_work_set(rng.choice(pool))
        elif action == "take":
            orphanage.get_tx_to_reconsider(peer)
        elif action == "erase" and pool:
            orphanage.erase_tx(rng.choice(pool).hash)
        elif action == "peer":
            orphanage.erase_for_peer(peer)
        elif action == "block":
            orphanage.erase_for_block(FakeBlock(rng.choice(pool or [a_tx(funding[0])])))  # type: ignore[arg-type]
        is_consistent(orphanage)
