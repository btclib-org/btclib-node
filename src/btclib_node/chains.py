# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The networks this node can join, and the genesis block of each.

`Chain` and its four leaves -- `Main`, `TestNet`, `SigNet`, `RegTest` --
carry a network's magic, its seed addresses, its own genesis block, and
what identifies this node's own copy of it -- a pruning floor and the
port it dials. Consensus, an activation height, a subsidy interval, the
easiest target and the exceptions a chain's own history forces, is
`btclib.consensus`'s, reached below through `Chain.consensus` keyed by
the same `name` `magic_from_network` already resolves. `config.py`'s
`_resolve_chain` is what turns a chain's name, read from the command
line or a functional test, into one of these.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from btclib.block import Block, BlockHeader, merkle_root_and_mutated_from_transactions
from btclib.consensus import CONSENSUS_PARAMS, ConsensusParams
from btclib.p2p.magic import magic_from_network
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

__all__ = ["Chain", "Main", "RegTest", "SigNet", "TestNet"]


def create_genesis(
    time: int, nonce: int, difficulty: int, version: int, reward: int
) -> Block:
    """Build a network's genesis block from its own header fields and reward.

    The same coinbase text and public key on every network -- Bitcoin's
    own genesis message and Satoshi's pubkey -- since what makes one
    network's genesis differ from another's is the header alone: its
    time, nonce, starting difficulty and version, plus how much the one
    coinbase output pays.
    """
    script_sig = script.serialize(
        [
            "FFFF001D",
            b"\x04",
            b"The Times 03/Jan/2009 Chancellor on brink of second bailout for banks",
        ]
    )
    script_pub_key = script.serialize(
        [
            "04678afdb0fe5548271967f1a67130b7105cd6a828e03909a67962e0ea1f61deb649f6bc3f4cef38c4f35504e51ec112de5c384df7ba0b8d578a4c702b6bf11d5f",
            "OP_CHECKSIG",
        ]
    )
    tx_in = TxIn(
        prev_out=OutPoint(),
        script_sig=script_sig,
        sequence=0xFFFFFFFF,
    )
    tx_out = TxOut(
        value=reward,
        script_pub_key=script_pub_key,
    )
    tx = Tx(
        version=1,
        lock_time=0,
        vin=[tx_in],
        vout=[tx_out],
    )
    header = BlockHeader(
        version=version,
        previous_block_hash="00" * 32,
        merkle_root="00" * 32,
        time=datetime.fromtimestamp(time, UTC),
        bits=difficulty.to_bytes(4, "big"),
        nonce=nonce,
        check_validity=False,
    )
    # btclib's own, so that the root this builds and the root
    # Block.assert_valid compares against are one implementation
    header.merkle_root = merkle_root_and_mutated_from_transactions([tx])[0]
    header.assert_valid()
    # the block and not the header alone: the coinbase above is the only
    # copy of the genesis block anywhere in this node -- no peer serves
    # it and no `getdata` asks for it -- and the block filter of height
    # zero is built from its output script like every other block's
    return Block(header, [tx], check_validity=False)


@dataclass
class Chain:
    """A network this node can join: its magic, its seeds and its genesis.

    `Main`, `TestNet`, `SigNet` and `RegTest` below are its four leaves,
    each hardcoding one network's own constants in its `__init__` rather
    than taking them as arguments, since there is exactly one of each
    and nothing else ever builds one.
    """

    name: str
    port: int
    # Core's own rpc port for this chain, `CreateBaseChainParams`
    # (`src/chainparamsbase.cpp`, at bitcoin/bitcoin@05e49b342f): one
    # below `port` on every leaf below, not `port + 1` -- the client on
    # the other end is `bitcoin-cli` or another program written against
    # Core, so CLAUDE.md's own `Following Bitcoin Core` is what decides
    # this rather than any pattern internal to this file.
    # `config.py`'s `Config.__init__` used to compute `port + 1`
    # instead (btclib-org/btclib-node#605), which happens to be Core's
    # own *Tor* incoming-connection port for each of these four chains
    # (the comment directly above `CreateBaseChainParams` names them:
    # 8334/18334/38334/18445), a port this node does not listen on at
    # all.
    rpc_port: int
    addresses: list[str]
    genesis_block: Block
    # Core's `nPruneAfterHeight` (`src/kernel/chainparams.cpp`, at
    # bitcoin/bitcoin@ca7162cde5): `rpc.callbacks.prune_blockchain`'s own
    # "Blockchain is too short for pruning." floor, below which Core
    # refuses `pruneblockchain` outright rather than merely clamping it.
    # Each leaf's own `__init__` below cites the line its value comes
    # from.
    prune_after_height: int

    @property
    def genesis(self) -> BlockHeader:
        """Return the genesis header, which is what most callers want."""
        return self.genesis_block.header

    @property
    def consensus(self) -> ConsensusParams:
        """This chain's consensus row, from btclib's own per-network table.

        `btclib.consensus.CONSENSUS_PARAMS` is keyed by the same `name`
        `magic` below already resolves through `btclib.network`, so the
        two never disagree about which networks exist. Every activation
        height, the subsidy interval, the easiest target, and the
        exceptions this chain's own history forces are read off it
        rather than held here a second time.
        """
        return CONSENSUS_PARAMS[self.name]

    def subsidy(self, height: int) -> int:
        """Return the block reward at `height`: fifty bitcoin, halved by height.

        Core's `GetBlockSubsidy` (`src/validation.cpp:1844`,
        at bitcoin/bitcoin@204256c73f): fifty bitcoin, right-shifted once
        per `subsidy_halving_interval` blocks, forced to zero once that
        shift is undefined for a native int.
        """
        halvings = height // self.consensus.subsidy_halving_interval
        if halvings >= 64:  # noqa: PLR2004
            return 0
        return (50 * 10**8) >> halvings

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
        # the limit is already stated once, in create_genesis' argument.
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
        self.rpc_port = 8332
        self.addresses = [
            "seed.bitcoin.sipa.be",
            "dnsseed.bluematt.me",
            "dnsseed.bitcoin.dashjr.org",
            "seed.bitcoinstats.com",
            "seed.bitcoin.jonasschnelli.ch",
            "seed.btc.petertodd.org",
            "seed.bitcoin.sprovoost.nl",
            "dnsseed.emzy.de",
            "seed.bitcoin.wiz.biz",
        ]
        self.genesis_block = create_genesis(
            1231006505, 2083236893, 0x1D00FFFF, 1, 50 * 10**8
        )
        # src/kernel/chainparams.cpp:154, at bitcoin/bitcoin@ca7162cde5
        self.prune_after_height = 100000


@dataclass
class TestNet(Chain):
    """Testnet3: the long-running public test chain."""

    # the class docstring above already says which network this is; the
    # fields below are literal constants, not a decision this __init__
    # makes that a docstring would need to explain
    def __init__(self) -> None:  # noqa: D107
        self.name = "testnet"
        self.port = 18333
        self.rpc_port = 18332
        self.addresses = [
            "testnet-seed.bitcoin.jonasschnelli.ch",
            "seed.tbtc.petertodd.org",
            "seed.testnet.bitcoin.sprovoost.nl",
            "testnet-seed.bluematt.me",
        ]
        self.genesis_block = create_genesis(
            1296688602, 414098458, 0x1D00FFFF, 1, 50 * 10**8
        )
        # src/kernel/chainparams.cpp:273, at bitcoin/bitcoin@ca7162cde5
        self.prune_after_height = 1000


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
        self.rpc_port = 38332
        self.addresses = ["178.128.221.177"]
        self.genesis_block = create_genesis(
            1598918400, 52613770, 0x1E0377AE, 1, 50 * 10**8
        )
        # src/kernel/chainparams.cpp:518, at bitcoin/bitcoin@ca7162cde5
        self.prune_after_height = 1000


@dataclass
class RegTest(Chain):
    """A local, disposable chain: no seeds, an easy target, no retargeting.

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
        self.rpc_port = 18443
        self.addresses = []
        self.genesis_block = create_genesis(1296688602, 2, 0x207FFFFF, 1, 50 * 10**8)
        # src/kernel/chainparams.cpp:601, at bitcoin/bitcoin@ca7162cde5:
        # `opts.fastprune ? 100 : 1000` -- this tree never passes
        # `-fastprune`, Core's own knob for a lower regtest value in its
        # test suite, so 1000 is the one value that applies here
        self.prune_after_height = 1000
