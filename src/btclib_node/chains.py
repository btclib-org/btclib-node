# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The networks this node can join.

`Chain` and its five leaves -- `Main`, `TestNet`, `SigNet`, `RegTest`,
`TestNet4` -- carry a network's magic, its seed addresses, its genesis
block, and what identifies this node's own copy of it -- a pruning floor
and the port it dials. The genesis block is `btclib.block.genesis_block`'s
and the default RPC port `bitcoin_core_rpc.rpc_port_from_chain`'s, both
keyed by `name`. Consensus, an activation height, a subsidy interval,
the easiest target and the exceptions a chain's own history forces, is
`btclib.consensus`'s, reached below through `Chain.consensus` keyed by
the same `name` `magic_from_network` already resolves. `config.py`'s
`_resolve_chain` is what turns a chain's name, read from the command
line or a functional test, into one of these.

`Chain.with_activation_heights` is Core's `-testactivationheight`.
"""

import copy
from dataclasses import dataclass, replace
from functools import cache, cached_property
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Self

from bitcoin_core_rpc import chain_from_network, rpc_port_from_chain
from btclib.block import Block, BlockHeader, genesis_block
from btclib.consensus import (
    CONSENSUS_PARAMS,
    HEADERS_SYNC_PARAMS,
    ConsensusParams,
    HeadersSyncParams,
)
from btclib.p2p.magic import magic_from_network

from btclib_node._chainparamsseeds import (
    CHAINPARAMS_SEED_MAIN,
    CHAINPARAMS_SEED_SIGNET,
    CHAINPARAMS_SEED_TEST,
    CHAINPARAMS_SEED_TESTNET4,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "BURIED_DEPLOYMENTS",
    "Chain",
    "Main",
    "RegTest",
    "SigNet",
    "TestNet",
    "TestNet4",
]

# The names `-testactivationheight` takes, Core's `GetBuriedDeployment`
# (`src/deploymentinfo.cpp`, at bitcoin/bitcoin@f0de87f040, the v32.1
# tag), each with the `ConsensusParams` field it moves
BURIED_DEPLOYMENTS: Mapping[str, str] = MappingProxyType(
    {
        "segwit": "segwit_height",
        "bip34": "bip34_height",
        "dersig": "bip66_height",
        "cltv": "bip65_height",
        "csv": "csv_height",
    }
)


@cache
def _moved_consensus(
    name: str, activation_heights: tuple[tuple[str, int], ...]
) -> ConsensusParams:
    """Return `name`'s row with `activation_heights`' fields replaced.

    Cached, so that reading `Chain.consensus` builds no new row.
    """
    moved: dict[str, Any] = dict(activation_heights)
    return replace(CONSENSUS_PARAMS[name], **moved)


@dataclass
class Chain:
    """A network this node can join: its magic, its seeds and its genesis.

    Each leaf below sets one network's own constants in its `__init__`
    rather than taking them as arguments, since there is exactly one of
    each and nothing else ever builds one.
    """

    name: str
    port: int
    # Core's `vSeeds` (`src/kernel/chainparams.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), the DNS seeds, in
    # Core's order and with its trailing dot, which makes each name
    # fully qualified for the resolver
    addresses: list[str]
    # Core's `vFixedSeeds` (`src/kernel/chainparams.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): BIP155 serialized
    # endpoints, `p2p.address.fixed_seed_addresses`'s to decode.
    fixed_seeds: bytes
    # Core's `nPruneAfterHeight` (`src/kernel/chainparams.cpp`,
    # at bitcoin/bitcoin@ca7162cde5): `rpc.callbacks.prune_blockchain`'s own
    # "Blockchain is too short for pruning." floor, below which Core
    # refuses `pruneblockchain` outright rather than merely clamping it.
    # Each leaf's own `__init__` below cites the line its value comes
    # from.
    prune_after_height: int
    # Core's `MinBIP9WarningHeight`, the height below which no block
    # counts towards the unknown-versionbit warning
    # (`versionbits.check_unknown_activations`); each leaf's own
    # `__init__` cites the line it copies. `btclib.consensus` leaves it
    # out, as a warning and not a rule.
    min_bip9_warning_height: int
    # the `ConsensusParams` fields `with_activation_heights` moved, with
    # their heights, sorted by field; empty on a chain nothing moved
    activation_heights: tuple[tuple[str, int], ...] = ()

    @cached_property
    def genesis_block(self) -> Block:
        """This chain's genesis block, `btclib.block.genesis_block`'s.

        The block and not the header alone: `Node.load` writes it to
        `block_db`, and the block filter of height zero is built from its
        output script like every other block's. Cached because header
        checks read it through `pow_limit_bits`.
        """
        return genesis_block(self.name)

    @property
    def genesis(self) -> BlockHeader:
        """Return the genesis header, which is what most callers want."""
        return self.genesis_block.header

    @property
    def rpc_port(self) -> int:
        """Core's default RPC port for this chain, bitcoin-core-rpc's."""
        # Core's and not a pattern of this file's, such as `port + 1`:
        # the client on the other end is `bitcoin-cli` or another program
        # written against Core (CONTRIBUTING.md's `Following Bitcoin Core`)
        return rpc_port_from_chain(chain_from_network(self.name))

    @property
    def consensus(self) -> ConsensusParams:
        """This chain's consensus row, from btclib's own per-network table.

        `btclib.consensus.CONSENSUS_PARAMS` is keyed by the same `name`
        `magic` below already resolves through `btclib.network`, so the
        two never disagree about which networks exist. Every activation
        height, the subsidy interval, the easiest target, and the
        exceptions this chain's own history forces are read off it
        rather than held here a second time. A chain
        `with_activation_heights` built answers a row of its own instead,
        the shared one with those heights replaced.
        """
        if not self.activation_heights:
            return CONSENSUS_PARAMS[self.name]
        return _moved_consensus(self.name, self.activation_heights)

    def with_activation_heights(self, heights: Mapping[str, int]) -> Self:
        """Return a copy of this chain whose deployments bind at `heights`.

        Core's `-testactivationheight=name@height`, applied by
        `CChainParams::ApplyDeploymentOptions`
        (`src/kernel/chainparams.cpp`, at bitcoin/bitcoin@f0de87f040, the
        v32.1 tag): `heights` maps a key of `BURIED_DEPLOYMENTS` to the
        first height its rule binds at, on top of what this chain
        already moved. This chain and the shared row are left as they
        are, so two nodes in one process can differ.

        Core's kernel applies such heights on every chain, mainnet
        included (`CChainParams::Main`), and so does this; `cli.py`
        refuses the option on mainnet, as Core's init does.
        """
        moved = dict(self.activation_heights)
        moved.update((BURIED_DEPLOYMENTS[name], h) for name, h in heights.items())
        chain = copy.copy(self)
        chain.activation_heights = tuple(sorted(moved.items()))
        return chain

    @property
    def headers_sync_params(self) -> HeadersSyncParams:
        """This chain's row of `btclib.consensus.HEADERS_SYNC_PARAMS`.

        What `btclib.p2p.headers_sync` spends on one peer, keyed by `name`
        as `consensus` is.
        """
        return HEADERS_SYNC_PARAMS[self.name]

    @property
    def magic(self) -> bytes:
        """The network's four-byte magic, the octets a message starts with."""
        # bytes, and the four octets that go on the wire: a hex string
        # here is what made the resynchronisation in
        # p2p.connection.Connection.parse_messages hunt for ASCII inside
        # a binary buffer.
        #
        # For signet this is the default signet's, which is what
        # magic_from_network answers; a custom signet derives its own
        # from its challenge, via magic_from_signet_challenge.
        return magic_from_network(self.name)

    @property
    def pow_limit_bits(self) -> bytes:
        """The network's easiest target, as its genesis block's own bits."""
        # A genesis block is mined at its network's easiest target, so
        # the limit is already stated once, in the genesis header's bits.
        # btclib's validation defaults to mainnet's, which would reject
        # every regtest and signet block, so it has to be passed in.
        return self.genesis.bits


@dataclass
class Main(Chain):
    """Mainnet: the chain real bitcoin moves on."""

    # the class docstring above already says which network this is; the
    # fields below are literal constants, not a decision this __init__
    # makes that a docstring would need to explain
    def __init__(self) -> None:  # noqa: D107
        self.name = "mainnet"
        self.port = 8333
        self.addresses = [
            "seed.bitcoin.sipa.be.",
            "dnsseed.bluematt.me.",
            "seed.bitcoin.jonasschnelli.ch.",
            "seed.btc.petertodd.net.",
            "seed.bitcoin.sprovoost.nl.",
            "dnsseed.emzy.de.",
            "seed.bitcoin.wiz.biz.",
            "seed.mainnet.achownodes.xyz.",
        ]
        self.fixed_seeds = CHAINPARAMS_SEED_MAIN
        # src/kernel/chainparams.cpp:154, at bitcoin/bitcoin@ca7162cde5
        self.prune_after_height = 100000
        # src/kernel/chainparams.cpp:95, at bitcoin/bitcoin@9be056a8a7
        self.min_bip9_warning_height = 483840


@dataclass
class TestNet(Chain):
    """Testnet3: the long-running public test chain."""

    # the class docstring above already says which network this is; the
    # fields below are literal constants, not a decision this __init__
    # makes that a docstring would need to explain
    def __init__(self) -> None:  # noqa: D107
        self.name = "testnet"
        self.port = 18333
        self.addresses = [
            "testnet-seed.bitcoin.jonasschnelli.ch.",
            "seed.tbtc.petertodd.net.",
            "seed.testnet.bitcoin.sprovoost.nl.",
            "testnet-seed.bluematt.me.",
            "seed.testnet.achownodes.xyz.",
        ]
        self.fixed_seeds = CHAINPARAMS_SEED_TEST
        # src/kernel/chainparams.cpp:273, at bitcoin/bitcoin@ca7162cde5
        self.prune_after_height = 1000
        # src/kernel/chainparams.cpp:226, at bitcoin/bitcoin@9be056a8a7
        self.min_bip9_warning_height = 836640


@dataclass
class SigNet(Chain):
    """The default public signet, not a custom one built from its own challenge.

    `btclib.consensus.CONSENSUS_PARAMS["signet"]` gates every buried
    deployment at height 1, since signet is a fresh chain each time it is
    reset rather than one carrying mainnet's own history.
    """

    # the class docstring above already says which network this is; the
    # fields below are literal constants, not a decision this __init__
    # makes that a docstring would need to explain
    def __init__(self) -> None:  # noqa: D107
        self.name = "signet"
        self.port = 38333
        self.addresses = [
            "seed.signet.bitcoin.sprovoost.nl.",
            "seed.signet.achownodes.xyz.",
        ]
        self.fixed_seeds = CHAINPARAMS_SEED_SIGNET
        # src/kernel/chainparams.cpp:518, at bitcoin/bitcoin@ca7162cde5
        self.prune_after_height = 1000
        # src/kernel/chainparams.cpp:490, at bitcoin/bitcoin@9be056a8a7
        self.min_bip9_warning_height = 0


@dataclass
class RegTest(Chain):
    """A local, disposable chain: no real seed, an easy target, no retargeting.

    P2SH, segwit v0 and taproot are on from the genesis block on every
    chain btclib's own `ConsensusParams.script_flags_at` answers for;
    `btclib.consensus.CONSENSUS_PARAMS["regtest"]` gates DERSIG,
    CHECKLOCKTIMEVERIFY and CHECKSEQUENCEVERIFY at height 1 and
    NULLDUMMY at height 0, and the difficulty never moves off the
    genesis's own, so a functional test can mine past any of them in as
    few blocks as it needs, on demand.
    """

    # the class docstring above already says which network this is; the
    # fields below are literal constants, not a decision this __init__
    # makes that a docstring would need to explain
    def __init__(self) -> None:  # noqa: D107
        self.name = "regtest"
        self.port = 18444
        # Core's own placeholder, a name under the `.invalid` top-level
        # domain, which RFC 6761 reserves to resolve to nothing
        self.addresses = ["dummySeed.invalid."]
        self.fixed_seeds = b""
        # src/kernel/chainparams.cpp:601, at bitcoin/bitcoin@ca7162cde5:
        # `opts.fastprune ? 100 : 1000` -- this tree never passes
        # `-fastprune`, Core's own knob for a lower regtest value in its
        # test suite, so 1000 is the one value that applies here
        self.prune_after_height = 1000
        # src/kernel/chainparams.cpp:574, at bitcoin/bitcoin@9be056a8a7
        self.min_bip9_warning_height = 0


@dataclass
class TestNet4(Chain):
    """Testnet4: the public test chain that replaces the deprecated testnet3.

    `btclib.consensus.CONSENSUS_PARAMS["testnet4"]` is the one row this
    package's four other chains all leave at its default,
    `enforce_bip94=True`: the timewarp mitigation BIP94 was written for
    this network, and `btclib.block.header_context.assert_not_timewarp`
    reads that flag generically, off `Chain.consensus` like every other
    field, so nothing here has to know about it.
    """

    # the class docstring above already says which network this is; the
    # fields below are literal constants, not a decision this __init__
    # makes that a docstring would need to explain
    def __init__(self) -> None:  # noqa: D107
        self.name = "testnet4"
        self.port = 48333
        self.addresses = [
            "seed.testnet4.bitcoin.sprovoost.nl.",
            "seed.testnet4.wiz.biz.",
        ]
        self.fixed_seeds = CHAINPARAMS_SEED_TESTNET4
        # src/kernel/chainparams.cpp:374, at bitcoin/bitcoin@ca7162cde5
        self.prune_after_height = 1000
        # src/kernel/chainparams.cpp:333, at bitcoin/bitcoin@9be056a8a7
        self.min_bip9_warning_height = 0
