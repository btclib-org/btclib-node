# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Every `Chain` subclass's constants, checked against Bitcoin Core's own."""

from btclib_node.chains import Chain, Main, RegTest, SigNet, TestNet

CHAINS = (Main(), TestNet(), SigNet(), RegTest())

# Bitcoin Core's chainparams.cpp, per chain: `consensus.powLimit` as
# compact bits, `pchMessageStart`, the genesis block hash,
# `fPowAllowMinDifficultyBlocks`, `fPowNoRetargeting`,
# `nSubsidyHalvingInterval` and `BIP34Height`. Every chain this package
# defines is named here, and the test below fails if one is added
# without its entry -- naming only some is how a wrong constant
# survives a green suite.
EXPECTED = {
    "mainnet": {
        "pow_limit_bits": "1d00ffff",
        "magic": "f9beb4d9",
        "genesis": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
        "pow_allow_min_difficulty_blocks": False,
        "pow_no_retargeting": False,
        "subsidy_halving_interval": 210000,
        "bip34_height": 227931,
    },
    "testnet": {
        "pow_limit_bits": "1d00ffff",
        "magic": "0b110907",
        "genesis": "000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943",
        "pow_allow_min_difficulty_blocks": True,
        "pow_no_retargeting": False,
        "subsidy_halving_interval": 210000,
        "bip34_height": 21111,
    },
    "signet": {
        "pow_limit_bits": "1e0377ae",
        "magic": "0a03cf40",
        "genesis": "00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6",
        "pow_allow_min_difficulty_blocks": False,
        "pow_no_retargeting": False,
        "subsidy_halving_interval": 210000,
        "bip34_height": 1,
    },
    "regtest": {
        "pow_limit_bits": "207fffff",
        "magic": "fabfb5da",
        "genesis": "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206",
        "pow_allow_min_difficulty_blocks": True,
        "pow_no_retargeting": True,
        "subsidy_halving_interval": 150,
        "bip34_height": 1,
    },
}


def test_every_chain_is_covered() -> None:
    """`CHAINS` and `Chain.__subclasses__()` both match `EXPECTED`'s own keys.

    Two checks rather than one: the first catches a chain built and
    added to `CHAINS` without an `EXPECTED` entry; the second catches
    the opposite gap, a `Chain` subclass never added to `CHAINS` at
    all, which the first assertion alone could not see.
    """
    assert {chain.name for chain in CHAINS} == set(EXPECTED)
    # by class and not by the name an instance carries: `Chain` itself
    # takes its four fields, so building one out of `__subclasses__`
    # was a call that only the subclasses' own no-argument __init__
    # answers, and nothing said so where it was read.
    assert set(Chain.__subclasses__()) == {type(chain) for chain in CHAINS}


def test_pow_limit_bits() -> None:
    """Each chain's `pow_limit_bits` matches Core's `consensus.powLimit`."""
    # Chain.pow_limit_bits reads the limit off the genesis header, which
    # holds only because a genesis is mined at exactly its network's
    # easiest target. That is an invariant of the chains defined here
    # rather than of the property, so a chain added without an entry
    # above cannot be trusted to validate blocks until it is checked.
    for chain in CHAINS:
        expected = EXPECTED[chain.name]["pow_limit_bits"]
        assert chain.pow_limit_bits.hex() == expected, chain.name


def test_pow_allow_min_difficulty_blocks_and_pow_no_retargeting() -> None:
    """Each chain's two retargeting flags match Core's own chainparams."""
    # Bitcoin Core's fPowAllowMinDifficultyBlocks and fPowNoRetargeting,
    # per chain: src/kernel/chainparams.cpp's CMainParams, CTestNetParams,
    # SigNetParams and CRegTestParams, each setting both fields once in
    # its constructor
    for chain in CHAINS:
        expected = EXPECTED[chain.name]
        assert (
            chain.pow_allow_min_difficulty_blocks
            == expected["pow_allow_min_difficulty_blocks"]
        ), chain.name
        assert chain.pow_no_retargeting == expected["pow_no_retargeting"], chain.name


def test_subsidy_halving_interval_and_bip34_height() -> None:
    """Each chain's halving interval and BIP34 height match Core's own."""
    # Bitcoin Core's nSubsidyHalvingInterval and BIP34Height, per chain:
    # src/kernel/chainparams.cpp's CMainParams, CTestNetParams,
    # SigNetParams and CRegTestParams, each setting both fields once in
    # its constructor -- the same pair of files
    # test_pow_allow_min_difficulty_blocks_and_pow_no_retargeting checks,
    # for the immediately preceding pair of Core-sourced constants.
    for chain in CHAINS:
        expected = EXPECTED[chain.name]
        assert chain.subsidy_halving_interval == expected["subsidy_halving_interval"], (
            chain.name
        )
        assert chain.bip34_height == expected["bip34_height"], chain.name


def test_magic() -> None:
    """Each chain's `magic` matches Core's `pchMessageStart`."""
    # the four octets that open every p2p message: a wrong one is a node
    # talking to nobody, and nothing else in this tree asserts them now
    # that they are a lookup rather than literals
    for chain in CHAINS:
        assert chain.magic.hex() == EXPECTED[chain.name]["magic"], chain.name


def test_genesis() -> None:
    """Each chain's genesis block hash matches Core's own for that chain."""
    for chain in CHAINS:
        assert chain.genesis.hash.hex() == EXPECTED[chain.name]["genesis"], chain.name


def test_the_genesis_block_carries_the_coinbase_its_header_commits_to() -> None:
    """Each chain's genesis block holds one coinbase, and its id is the root."""
    # the header is derived from the transaction, so a block built
    # without it hashes the same and is still wrong: it is the only
    # copy of the genesis block this node has -- no peer serves it --
    # and the BIP158 filter of height zero is built from its outputs
    for chain in CHAINS:
        (coinbase,) = chain.genesis_block.transactions
        assert coinbase.is_coinbase, chain.name
        # one transaction, so the merkle root is its own id
        assert chain.genesis.merkle_root == coinbase.id, chain.name
        assert coinbase.vout[0].script_pub_key.script, chain.name
