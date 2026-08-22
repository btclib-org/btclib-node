# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.chains import Chain, Main, RegTest, SigNet, TestNet

CHAINS = (Main(), TestNet(), SigNet(), RegTest())

# Bitcoin Core's chainparams.cpp, per chain: `consensus.powLimit` as
# compact bits, `pchMessageStart` and the genesis block hash. Every
# chain this package defines is named here, and the test below fails if
# one is added without its entry -- naming only some is how a wrong
# constant survives a green suite.
EXPECTED = {
    "mainnet": {
        "pow_limit_bits": "1d00ffff",
        "magic": "f9beb4d9",
        "genesis": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
    },
    "testnet": {
        "pow_limit_bits": "1d00ffff",
        "magic": "0b110907",
        "genesis": "000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943",
    },
    "signet": {
        "pow_limit_bits": "1e0377ae",
        "magic": "0a03cf40",
        "genesis": "00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6",
    },
    "regtest": {
        "pow_limit_bits": "207fffff",
        "magic": "fabfb5da",
        "genesis": "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206",
    },
}


def test_every_chain_is_covered():
    assert {chain.name for chain in CHAINS} == set(EXPECTED)
    assert {cls().name for cls in Chain.__subclasses__()} == set(EXPECTED)


def test_pow_limit_bits():
    # Chain.pow_limit_bits reads the limit off the genesis header, which
    # holds only because a genesis is mined at exactly its network's
    # easiest target. That is an invariant of the chains defined here
    # rather than of the property, so a chain added without an entry
    # above cannot be trusted to validate blocks until it is checked.
    for chain in CHAINS:
        expected = EXPECTED[chain.name]["pow_limit_bits"]
        assert chain.pow_limit_bits.hex() == expected, chain.name


def test_magic():
    # the four octets that open every p2p message: a wrong one is a node
    # talking to nobody, and nothing else in this tree asserts them now
    # that they are a lookup rather than literals
    for chain in CHAINS:
        assert chain.magic.hex() == EXPECTED[chain.name]["magic"], chain.name


def test_genesis():
    for chain in CHAINS:
        assert chain.genesis.hash.hex() == EXPECTED[chain.name]["genesis"], chain.name
