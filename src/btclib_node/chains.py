# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from dataclasses import dataclass
from datetime import UTC, datetime

from btclib.block import Block, BlockHeader, merkle_root_and_mutated_from_transactions
from btclib.p2p.magic import magic_from_network
from btclib.script import script
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut


def create_genesis(
    time: int, nonce: int, difficulty: int, version: int, reward: int
) -> Block:
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
    name: str
    port: int
    addresses: list[str]
    genesis_block: Block
    # (height, name) pairs, each read by interpreter.get_flags as the
    # activation height of a script flag; every leaf below sets this in
    # its own __init__, which is why it carries no default here.
    flags: list[tuple[int, str]]
    # Core's fPowAllowMinDifficultyBlocks: a block more than two target
    # spacings after its parent may be mined at the network's easiest
    # target, so that a chain nobody is hashing still moves.
    pow_allow_min_difficulty_blocks: bool
    # Core's fPowNoRetargeting: the target never moves off the one the
    # genesis carries.
    pow_no_retargeting: bool

    @property
    def genesis(self) -> BlockHeader:
        """Return the genesis header, which is what most callers want."""
        return self.genesis_block.header

    @property
    def magic(self) -> bytes:
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
        # A genesis block is mined at its network's easiest target, so
        # the limit is already stated once, in create_genesis' argument.
        # btclib's validation defaults to mainnet's, which would reject
        # every regtest and signet block, so it has to be passed in.
        return self.genesis.bits


@dataclass
class Main(Chain):
    def __init__(self) -> None:
        self.name = "mainnet"
        self.port = 8333
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
        self.flags = [
            (170061, "P2SH"),
            (363725, "DERSIG"),
            (388381, "CHECKLOCKTIMEVERIFY"),
            (419328, "CHECKSEQUENCEVERIFY"),
            (481824, "WITNESS"),
            (481824, "NULLDUMMY"),
            (709632, "TAPROOT"),
        ]
        self.pow_allow_min_difficulty_blocks = False
        self.pow_no_retargeting = False


@dataclass
class TestNet(Chain):
    def __init__(self) -> None:
        self.name = "testnet"
        self.port = 18333
        self.addresses = [
            "testnet-seed.bitcoin.jonasschnelli.ch",
            "seed.tbtc.petertodd.org",
            "seed.testnet.bitcoin.sprovoost.nl",
            "testnet-seed.bluematt.me",
        ]
        self.genesis_block = create_genesis(
            1296688602, 414098458, 0x1D00FFFF, 1, 50 * 10**8
        )
        self.flags = [
            (395, "P2SH"),
            (330776, "DERSIG"),
            (581885, "CHECKLOCKTIMEVERIFY"),
            (770112, "CHECKSEQUENCEVERIFY"),
            (834624, "WITNESS"),
            (834624, "NULLDUMMY"),
            (1628640000, "TAPROOT"),  # wrong, this is the date
        ]
        self.pow_allow_min_difficulty_blocks = True
        self.pow_no_retargeting = False


@dataclass
class SigNet(Chain):
    def __init__(self) -> None:
        self.name = "signet"
        self.port = 38333
        self.addresses = ["178.128.221.177"]
        self.genesis_block = create_genesis(
            1598918400, 52613770, 0x1E0377AE, 1, 50 * 10**8
        )
        self.flags = [
            (0, "P2SH"),
            (0, "DERSIG"),
            (0, "CHECKLOCKTIMEVERIFY"),
            (0, "CHECKSEQUENCEVERIFY"),
            (0, "WITNESS"),
            (0, "NULLDUMMY"),
            (0, "TAPROOT"),
        ]
        self.pow_allow_min_difficulty_blocks = False
        self.pow_no_retargeting = False


@dataclass
class RegTest(Chain):
    def __init__(self) -> None:
        self.name = "regtest"
        self.port = 18444
        self.addresses = []
        self.genesis_block = create_genesis(1296688602, 2, 0x207FFFFF, 1, 50 * 10**8)
        self.flags = [
            (0, "P2SH"),
            (0, "DERSIG"),
            (0, "CHECKLOCKTIMEVERIFY"),
            (0, "CHECKSEQUENCEVERIFY"),
            (0, "WITNESS"),
            (0, "NULLDUMMY"),
            (0, "TAPROOT"),
        ]
        self.pow_allow_min_difficulty_blocks = True
        self.pow_no_retargeting = True
