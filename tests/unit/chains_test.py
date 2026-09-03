# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Every `Chain` subclass's own constants, checked against Bitcoin Core's own.

Consensus -- an activation height, the easiest target, the subsidy
interval, the exceptions a chain's own history forces -- is
`btclib.consensus`'s, transcribed and tested there; what this file
still owns is what identifies a *node*'s own copy of a network rather
than the network itself: the genesis block and the magic built from it,
and that `Chain.consensus` reaches the row of the chain it is asked of.
"""

from btclib.consensus import CONSENSUS_PARAMS

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


def test_consensus_is_the_row_of_the_same_name_in_btclibs_own_table() -> None:
    """`Chain.consensus` reaches `btclib.consensus.CONSENSUS_PARAMS` by name.

    Not a copy: the same frozen row every other reader of that table
    gets, so a value read off `chain.consensus` and one read off
    `CONSENSUS_PARAMS[chain.name]` can never disagree.
    """
    for chain in CHAINS:
        assert chain.consensus is CONSENSUS_PARAMS[chain.name], chain.name
