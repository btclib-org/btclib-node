# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`versionbits.py`: Core's warning for version bits no deployment uses.

The chains are `RegTest`'s, whose period is 144 blocks and threshold 108
(`feature_versionbits_warning.py`'s own `VB_PERIOD` and `VB_THRESHOLD`),
and stand-ins for the two other shapes a chain takes.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from btclib.block import BlockHeader

from btclib_node import versionbits
from btclib_node.chains import RegTest
from btclib_node.log import Logger
from btclib_node.notify import Warnings
from btclib_node.versionbits import UnknownActivations, check_unknown_activations

if TYPE_CHECKING:
    from pathlib import Path

    from btclib_node.chains import Chain

_PERIOD = 144
_THRESHOLD = 108
_TOP = 0x20000000
# a bit no deployment uses, the one `feature_versionbits_warning.py` picks
_UNKNOWN = 27
_TESTDUMMY = 28
_START = datetime.fromtimestamp(1_700_000_000, UTC)


def a_chain(
    versions: list[int], *, start: datetime = _START
) -> tuple[list[bytes], dict[bytes, Any]]:
    """Return the hashes and headers of a chain, a second between blocks."""
    active_chain: list[bytes] = []
    header_dict: dict[bytes, Any] = {}
    previous = b"\x00" * 32
    for height, version in enumerate(versions):
        header = BlockHeader(
            version=version,
            previous_block_hash=previous,
            merkle_root=height.to_bytes(32, "big"),
            time=start + timedelta(seconds=height),
            bits=b"\xff\xff\x7f\x20",
            nonce=0,
            check_validity=False,
        )
        previous = header.hash
        active_chain.append(previous)
        header_dict[previous] = SimpleNamespace(header=header)
    return active_chain, header_dict


def periods(*signals: int, plain: int = _TOP) -> list[int]:
    """Return one period of blocks per entry, all of version `entry` or `plain`.

    An entry of `0` is a period that signals nothing.
    """
    return [signal or plain for signal in signals for _ in range(_PERIOD)]


def bit(number: int) -> int:
    """Return the version of a block signalling `number` and nothing else."""
    return _TOP | (1 << number)


def check(versions: list[int], chain: Chain | None = None) -> list[tuple[int, bool]]:
    """Return what a fresh `UnknownActivations` answers at `versions`' tip."""
    active_chain, header_dict = a_chain(versions)
    return UnknownActivations(chain or RegTest()).check(active_chain, header_dict)


def test_a_bit_signalled_below_the_threshold_warns_of_nothing() -> None:
    """ISS 1475: one block short of `VB_THRESHOLD` in a period is no lock-in."""
    short = [bit(_UNKNOWN)] * (_THRESHOLD - 1) + [_TOP] * (_PERIOD - _THRESHOLD + 1)
    assert check(periods(0) + short + periods(0, 0)) == []


def test_a_bit_signalled_at_the_threshold_locks_in_and_then_activates() -> None:
    """ISS 1475: `STARTED` to `LOCKED_IN` to `ACTIVE`, one period each.

    `feature_versionbits_warning.py`'s own layout: a quiet period, one of
    `VB_THRESHOLD` signalling blocks, then quiet ones.
    """
    signalling = [bit(_UNKNOWN)] * _THRESHOLD + [_TOP] * (_PERIOD - _THRESHOLD)
    versions = periods(0) + signalling + periods(0, 0)
    assert check(versions[: 2 * _PERIOD]) == [(_UNKNOWN, False)]
    assert check(versions[: 3 * _PERIOD]) == [(_UNKNOWN, True)]


def test_a_tip_inside_a_period_answers_with_the_period_before_it() -> None:
    """`GetStateFor` reads the state at the last period boundary.

    The period that decides is the one ending at the boundary, whatever
    height the tip is at inside the next.
    """
    versions = periods(0) + periods(bit(_UNKNOWN)) + periods(0, 0)
    assert check(versions[: _PERIOD + 5]) == []
    assert check(versions[: 2 * _PERIOD + 5]) == [(_UNKNOWN, False)]


def test_a_signal_in_the_first_period_is_not_counted() -> None:
    """`DEFINED` becomes `STARTED` at the first boundary, and counts nothing."""
    assert check(periods(bit(_UNKNOWN)) + periods(0, 0, 0)) == []


def test_a_block_without_the_top_bits_signals_nothing() -> None:
    """`VERSIONBITS_TOP_BITS`: a version outside `001` is not BIP9's."""
    versions = periods(0) + periods(1 << _UNKNOWN) + periods(0, 0, 0)
    assert check(versions) == []


def test_a_bit_a_started_deployment_uses_is_not_unknown() -> None:
    """`ComputeBlockVersion` sets `TESTDUMMY`'s bit while it is started.

    Regtest's `DEPLOYMENT_TESTDUMMY` is bit 28, `STARTED` from the first
    boundary. Signalled through that period and the `LOCKED_IN` one, the
    bit warns of nothing; once it is `ACTIVE` it is not set any more, and
    a period that signals it locks in the warning.
    """
    versions = periods(0, bit(_TESTDUMMY), bit(_TESTDUMMY), bit(_TESTDUMMY))
    assert check(versions[: 3 * _PERIOD]) == []
    assert check(versions) == [(_TESTDUMMY, False)]


def test_no_block_below_the_warning_height_counts() -> None:
    """`MinBIP9WarningHeight`: a period half below it is half counted."""
    chain = cast("Chain", SimpleNamespace(name="regtest", min_bip9_warning_height=0))
    signalling = periods(bit(_UNKNOWN))
    versions = periods(0) + signalling + periods(0, 0)
    assert check(versions[: 2 * _PERIOD], chain) == [(_UNKNOWN, False)]
    chain = cast(
        "Chain",
        SimpleNamespace(name="regtest", min_bip9_warning_height=_PERIOD * 2 - 40),
    )
    assert check(versions[: 2 * _PERIOD], chain) == []


def test_main_counts_taproot_s_bit_as_a_known_one() -> None:
    """ISS 1475: `DEPLOYMENT_TAPROOT` is bit 2, signalled in 2021 on main.

    The same blocks on a chain with no such deployment warn: signet's is
    always active, which is not `STARTED`.
    """
    # 2021-04-24, taproot's `nStartTime`, and the two periods mainnet
    # signalled it through
    start = datetime.fromtimestamp(1619222400 + 100_000, UTC)
    versions = [_TOP] * 2016 + [bit(2)] * 2016 * 2 + [_TOP] * 2016 * 2
    active_chain, header_dict = a_chain(versions, start=start)
    for name, length, expected in (
        ("mainnet", 4032, []),
        ("mainnet", 6048, []),
        ("signet", 4032, [(2, False)]),
    ):
        chain = cast("Chain", SimpleNamespace(name=name, min_bip9_warning_height=0))
        found = UnknownActivations(chain).check(active_chain[:length], header_dict)
        assert found == expected, (name, length)


def test_a_fork_is_answered_from_its_own_blocks_after_the_chain_it_left() -> None:
    """A state is cached by its boundary block: a reorganisation reads its own.

    The same `UnknownActivations` sees the signalling chain first, then a
    quiet one sharing its first period.
    """
    first = periods(0)
    loud = first + periods(bit(_UNKNOWN))
    quiet = first + periods(0)
    loud_chain, loud_headers = a_chain(loud)
    quiet_chain, quiet_headers = a_chain(quiet)
    assert loud_chain[_PERIOD - 1] == quiet_chain[_PERIOD - 1]
    assert loud_chain[_PERIOD * 2 - 1] != quiet_chain[_PERIOD * 2 - 1]
    unknown = UnknownActivations(RegTest())
    assert unknown.check(loud_chain, loud_headers) == [(_UNKNOWN, False)]
    assert unknown.check(quiet_chain, quiet_headers) == []
    assert unknown.check(loud_chain, loud_headers) == [(_UNKNOWN, False)]


class _Node:
    """The parts of a `Node` `check_unknown_activations` reads."""

    def __init__(self, versions: list[int], log_path: Path) -> None:
        active_chain, header_dict = a_chain(versions)
        self.chainstate = SimpleNamespace(
            block_index=SimpleNamespace(
                active_chain=active_chain, header_dict=header_dict
            )
        )
        self.unknown_activations = UnknownActivations(RegTest())
        self.warnings = Warnings()
        self.logger = Logger(log_path)
        self.config = SimpleNamespace(alert_notify="notify %s")


def logged(path: Path) -> list[str]:
    """Return the lines of `path` after their time, blank lines left out."""
    return [
        line.split(" ", 1)[1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_an_active_bit_sets_the_warning_and_notifies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `ACTIVE` bit sets the warning and runs `-alertnotify` once."""
    alerts: list[str] = []
    monkeypatch.setattr(
        versionbits,
        "alert_notify",
        lambda _logger, command, message: alerts.append(f"{command}: {message}"),
    )
    versions = periods(0) + periods(bit(_UNKNOWN)) + periods(0, 0)
    log_path = tmp_path / "history.log"
    node = _Node(versions, log_path)
    message = f"Unknown new rules activated (versionbit {_UNKNOWN})"
    check_unknown_activations(cast("Any", node), 1)
    check_unknown_activations(cast("Any", node), 1)
    node.logger.close()
    assert node.warnings.get_messages() == [message]
    assert alerts == [f"notify %s: {message}"]
    assert not logged(log_path)


def test_a_locked_in_bit_is_logged_and_sets_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1475: a `LOCKED_IN` bit is logged at info, as `UpdateTip` does."""
    alerts: list[str] = []
    monkeypatch.setattr(
        versionbits, "alert_notify", lambda *args: alerts.append(args[-1])
    )
    versions = periods(0) + periods(bit(_UNKNOWN)) + periods(0)
    log_path = tmp_path / "history.log"
    node = _Node(versions[: 2 * _PERIOD], log_path)
    check_unknown_activations(cast("Any", node), 1)
    check_unknown_activations(cast("Any", node), 1)
    node.logger.close()
    assert not node.warnings.get_messages()
    assert not alerts
    message = f"Unknown new rules activated (versionbit {_UNKNOWN})"
    assert logged(log_path) == [message, message]


def test_a_quiet_chain_logs_and_warns_of_nothing(tmp_path: Path) -> None:
    """The control for the two above: no signal, no line."""
    log_path = tmp_path / "history.log"
    node = _Node(periods(0, 0, 0), log_path)
    check_unknown_activations(cast("Any", node), 1)
    node.logger.close()
    assert not node.warnings.get_messages()
    assert not logged(log_path)


def test_each_block_a_commit_connected_is_checked_as_core_checks_each(
    tmp_path: Path,
) -> None:
    """ISS 1475: `UpdateTip` runs for each block, so a boundary is seen once.

    The tip is a block past the boundary that locks the bit in, and the
    commit connected three blocks: the first is still `STARTED`, and only
    the other two log.
    """
    versions = periods(0) + periods(bit(_UNKNOWN)) + periods(0)
    log_path = tmp_path / "history.log"
    node = _Node(versions[: 2 * _PERIOD + 1], log_path)
    check_unknown_activations(cast("Any", node), 3)
    node.logger.close()
    message = f"Unknown new rules activated (versionbit {_UNKNOWN})"
    assert logged(log_path) == [message, message]


@pytest.mark.parametrize(
    ("min_activation_height", "warns"),
    [(2 * _PERIOD + _PERIOD, True), (3 * _PERIOD + 1, False)],
)
def test_a_deployment_stays_known_until_its_activation_height(
    monkeypatch: pytest.MonkeyPatch, min_activation_height: int, *, warns: bool
) -> None:
    """`GetStateFor`: `LOCKED_IN` is `ACTIVE` once `height + 1` is the minimum.

    A deployment on `_UNKNOWN`'s own bit is known while it is `STARTED`
    or `LOCKED_IN`, so signalling it warns of nothing, and warns once it is
    `ACTIVE`. One period locks it in; the boundary after it is block
    `3 * _PERIOD - 1`, `ACTIVE` only if the next height reaches the
    minimum. The minimum one above that height is what keeps it known.
    """
    deployment = versionbits._Deployment(
        _UNKNOWN, 0, versionbits._NO_TIMEOUT, min_activation_height
    )
    monkeypatch.setitem(versionbits._DEPLOYMENTS, "regtest", (deployment,))
    versions = periods(0, bit(_UNKNOWN), bit(_UNKNOWN), bit(_UNKNOWN))
    assert bool(check(versions)) is warns
