# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The warning for unused version bits, and the version a miner sets.

`UnknownActivations` is `VersionBitsCache::CheckUnknownActivations` with
its `WarningBitsConditionChecker` (`src/versionbits.cpp`, at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a BIP9 state machine per
version bit, counting over each period the blocks that signal a bit
no deployment in `_DEPLOYMENTS` uses, a bit being unknown from
`Chain.min_bip9_warning_height` on. It reaches `LOCKED_IN` and then
`ACTIVE` as any deployment does, with the same threshold and period.

`check_unknown_activations` is what `main._after_tip_change` calls out
of initial block download, where `Chainstate::UpdateTip`
(`src/validation.cpp`, same sha) asks: a bit that is `ACTIVE` sets the
warning, which runs `-alertnotify` the first time, and one that is
`LOCKED_IN` is logged.

`UnknownActivations.status` is `ComputeBlockVersion` and `GBTStatus`
(same file and sha): the version a block on the tip carries, and the
deployments `getblocktemplate` reports. Core's two deployments are both
`gbt_optional_rule`, so a client's `rules` change neither the version nor
the answer.

This node enforces no deployment by signalling, each being buried
(`btclib.consensus`). The deployments below are the ones Core still
counts signals for, whose bits `ComputeBlockVersion` excludes from the
warning; Core's others are always active or never, which excludes
nothing.
"""

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from btclib.block.header_context import median_time_past

from btclib_node.notify import alert_notify

if TYPE_CHECKING:
    from collections.abc import Mapping

    from btclib.block import BlockHeader

    from btclib_node import Node
    from btclib_node.chains import Chain
    from btclib_node.chainstate.block_index import BlockInfo

__all__ = ["DeploymentStatus", "UnknownActivations", "check_unknown_activations"]

# `VERSIONBITS_TOP_BITS`, `VERSIONBITS_TOP_MASK` and
# `VERSIONBITS_NUM_BITS` (`src/versionbits.h`, same sha)
_TOP_BITS = 0x20000000
_TOP_MASK = 0xE0000000
_NUM_BITS = 29

# `std::numeric_limits<int64_t>::max()`, the end time of a deployment
# that never times out, and of the warning checker
_NO_TIMEOUT = 2**63 - 1

# Core's `kernel::Warning::UNKNOWN_NEW_RULES_ACTIVATED`
# (`src/kernel/warning.h`, same sha): one id for every bit, so the first
# to become active is the message the warning keeps
_WARNING_ID = "unknown_new_rules_activated"


class _State(enum.Enum):
    """Core's `ThresholdState` (`src/versionbits.h`, same sha)."""

    DEFINED = enum.auto()
    STARTED = enum.auto()
    LOCKED_IN = enum.auto()
    ACTIVE = enum.auto()
    FAILED = enum.auto()


@dataclass(frozen=True)
class _Deployment:
    """A BIP9 deployment whose signals Core still counts: `vDeployments`."""

    name: str
    bit: int
    start_time: int
    timeout: int
    min_activation_height: int


# `src/kernel/chainparams.cpp`, same sha, `DEPLOYMENT_TAPROOT` on main
# (`:111-113`) and on testnet (`:242-244`), `DEPLOYMENT_TESTDUMMY` on
# regtest (`:582-585`). Signet and testnet4 carry neither one alone: their
# `DEPLOYMENT_TAPROOT` is always active and `DEPLOYMENT_TESTDUMMY` never.
_TAPROOT_BIT = 2
_TESTDUMMY_BIT = 28
_DEPLOYMENTS: Mapping[str, tuple[_Deployment, ...]] = {
    "mainnet": (_Deployment("taproot", _TAPROOT_BIT, 1619222400, 1628640000, 709632),),
    "testnet": (_Deployment("taproot", _TAPROOT_BIT, 1619222400, 1628640000, 0),),
    "regtest": (_Deployment("testdummy", _TESTDUMMY_BIT, 0, _NO_TIMEOUT, 0),),
}

# `DEPLOYMENT_TAPROOT` is `ALWAYS_ACTIVE` on these chains
# (`src/kernel/chainparams.cpp`, same sha), which `GBTStatus` lists as
# active; mainnet and testnet are above, and the other `testdummy`s are
# never active
_TAPROOT_ALWAYS_ACTIVE = frozenset({"regtest", "signet", "testnet4"})


class DeploymentStatus(NamedTuple):
    """What `UnknownActivations.status` answers for a block on the tip.

    `signalling` and `locked_in` map a deployment's name to its bit, and
    `active` lists names, each in name order, as Core's `std::map` does.
    """

    version: int
    signalling: dict[str, int]
    locked_in: dict[str, int]
    active: list[str]


class _Boundary(NamedTuple):
    """The states in effect for the period that follows a period boundary."""

    warning: tuple[_State, ...]
    deployments: tuple[_State, ...]


def _advance(  # noqa: PLR0913, PLR0917
    state: _State,
    count: int,
    threshold: int,
    mtp: int,
    height: int,
    deployment: _Deployment,
) -> _State:
    """Return the state after a period, `GetStateFor`'s own transitions.

    `mtp` and `height` are those of the last block of the period.
    """
    if state is _State.DEFINED:
        return _State.STARTED if mtp >= deployment.start_time else state
    if state is _State.STARTED:
        if count >= threshold:
            return _State.LOCKED_IN
        return _State.FAILED if mtp >= deployment.timeout else state
    if state is _State.LOCKED_IN and height + 1 >= deployment.min_activation_height:
        return _State.ACTIVE
    return state


_WARNING_DEPLOYMENT = _Deployment("", -1, 0, _NO_TIMEOUT, 0)


class UnknownActivations:
    """The state of each unknown version bit, kept per period boundary.

    A state is a function of the blocks up to its boundary block, so it
    is cached by the boundary block's hash and a reorganisation reuses
    what it shares with the chain it left.
    """

    def __init__(self, chain: Chain) -> None:
        """Take `chain`'s period, threshold, warning height and deployments."""
        # Core's `WarningBitsConditionChecker` constructor: 90% of 2016 on
        # main, and a test chain's own retarget interval at 75%
        # (`src/versionbits.cpp`, same sha); regtest's is 144
        self._period = 144 if chain.name == "regtest" else 2016
        self._threshold = 1815 if chain.name == "mainnet" else self._period * 3 // 4
        self._min_height = chain.min_bip9_warning_height
        self._deployments = _DEPLOYMENTS.get(chain.name, ())
        self._always_active = (
            ["taproot"] if chain.name in _TAPROOT_ALWAYS_ACTIVE else []
        )
        # the genesis block's predecessor, which is `DEFINED` by definition
        self._cache: dict[bytes | None, _Boundary] = {
            None: _Boundary(
                (_State.DEFINED,) * _NUM_BITS,
                (_State.DEFINED,) * len(self._deployments),
            )
        }

    def check(
        self,
        active_chain: list[bytes],
        header_dict: Mapping[bytes, BlockInfo],
        height: int | None = None,
    ) -> list[tuple[int, bool]]:
        """Return each bit `LOCKED_IN` or `ACTIVE` after the block at `height`.

        `height` is the tip's by default. `(bit, active)`, in bit order:
        `CheckUnknownActivations`' own answer. The state is that of the
        period the next block is in, `GetStateFor` of that block.
        """
        warning = self._boundary(active_chain, header_dict, height).warning
        return [
            (bit, state is _State.ACTIVE)
            for bit, state in enumerate(warning)
            if state in {_State.ACTIVE, _State.LOCKED_IN}
        ]

    def status(
        self, active_chain: list[bytes], header_dict: Mapping[bytes, BlockInfo]
    ) -> DeploymentStatus:
        """Return the version and deployments of a block on the tip.

        The states are those of the period the next block is in, as
        `ComputeBlockVersion` and `GBTStatus` read them: the version sets
        the bit of each deployment that is `STARTED` or `LOCKED_IN`.
        """
        boundary = self._boundary(active_chain, header_dict, None)
        found: dict[str, tuple[_Deployment, _State]] = {
            deployment.name: (deployment, state)
            for deployment, state in zip(
                self._deployments, boundary.deployments, strict=True
            )
        }
        version = _TOP_BITS
        signalling: dict[str, int] = {}
        locked_in: dict[str, int] = {}
        active = list(self._always_active)
        for name in sorted(found):
            deployment, state = found[name]
            if state in {_State.STARTED, _State.LOCKED_IN}:
                version |= 1 << deployment.bit
            if state is _State.STARTED:
                signalling[name] = deployment.bit
            elif state is _State.LOCKED_IN:
                locked_in[name] = deployment.bit
            elif state is _State.ACTIVE:
                active.append(name)
        return DeploymentStatus(version, signalling, locked_in, sorted(active))

    def _boundary(
        self,
        active_chain: list[bytes],
        header_dict: Mapping[bytes, BlockInfo],
        height: int | None,
    ) -> _Boundary:
        """Return the states in effect after the block at `height`."""
        if height is None:
            height = len(active_chain) - 1
        last = height - ((height + 1) % self._period)
        missing = []
        cached = last
        while cached >= 0 and active_chain[cached] not in self._cache:
            missing.append(cached)
            cached -= self._period
        for end in reversed(missing):
            self._cache[active_chain[end]] = self._period_ending_at(
                end, active_chain, header_dict
            )
        return self._cache[active_chain[last] if last >= 0 else None]

    def _period_ending_at(
        self,
        end: int,
        active_chain: list[bytes],
        header_dict: Mapping[bytes, BlockInfo],
    ) -> _Boundary:
        """Return the states after the period whose last block is at `end`."""
        previous_end = end - self._period
        previous = self._cache[
            active_chain[previous_end] if previous_end >= 0 else None
        ]
        header = header_dict[active_chain[end]].header

        def parent_of(header: BlockHeader) -> BlockHeader:
            return header_dict[header.previous_block_hash].header

        mtp = median_time_past(header, end, parent_of)
        # `ComputeBlockVersion` sets the bit of a deployment that is
        # `STARTED` or `LOCKED_IN`, which the warning then does not count
        known = {
            deployment.bit
            for deployment, state in zip(
                self._deployments, previous.deployments, strict=True
            )
            if state in {_State.STARTED, _State.LOCKED_IN}
        }
        counts = [0] * _NUM_BITS
        warning_counts = [0] * _NUM_BITS
        if end >= self._min_height or known:
            for height in range(previous_end + 1, end + 1):
                version = header_dict[active_chain[height]].header.version & 0xFFFFFFFF
                if version & _TOP_MASK != _TOP_BITS:
                    continue
                signals = version & ~_TOP_MASK
                while signals:
                    low = signals & -signals
                    bit = low.bit_length() - 1
                    counts[bit] += 1
                    if height >= self._min_height and bit not in known:
                        warning_counts[bit] += 1
                    signals ^= low
        return _Boundary(
            tuple(
                _advance(state, count, self._threshold, mtp, end, _WARNING_DEPLOYMENT)
                for state, count in zip(previous.warning, warning_counts, strict=True)
            ),
            tuple(
                _advance(
                    state, counts[deployment.bit], self._threshold, mtp, end, deployment
                )
                for deployment, state in zip(
                    self._deployments, previous.deployments, strict=True
                )
            ),
        )


def check_unknown_activations(node: Node, blocks: int) -> None:
    """Warn of each unknown bit after each of the last `blocks` blocks.

    `Chainstate::UpdateTip`'s own loop (`src/validation.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), run for each block a
    commit connected, as `ConnectTip` does. A bit that is `ACTIVE` sets
    the warning, `node.warnings.set_warning`'s dedup running
    `-alertnotify` once, as `KernelNotifications::warningSet` does. One
    that is `LOCKED_IN` goes to the log at info level, where Core puts it
    in `UpdateTip`'s own line, which this node does not write.
    """
    block_index = node.chainstate.block_index
    tip = len(block_index.active_chain) - 1
    for height in range(tip - blocks + 1, tip + 1):
        for bit, active in node.unknown_activations.check(
            block_index.active_chain, block_index.header_dict, height
        ):
            message = f"Unknown new rules activated (versionbit {bit})"
            if not active:
                node.logger.info(message)
            elif node.warnings.set_warning(_WARNING_ID, message):
                alert_notify(node.logger, node.config.alert_notify, message)
