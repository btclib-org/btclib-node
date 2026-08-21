# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.chains import Main, RegTest, SigNet, TestNet


def test_pow_limit_bits():
    # Each network's easiest target, as Bitcoin Core's chainparams.cpp
    # states it in `consensus.powLimit`. Chain.pow_limit_bits reads it
    # off the genesis header, which holds only because a genesis is
    # mined at exactly that target -- an invariant of the four chains
    # below, not of the property, so a fifth chain has to be checked
    # here before it can be trusted to validate blocks correctly.
    assert Main().pow_limit_bits.hex() == "1d00ffff"
    assert TestNet().pow_limit_bits.hex() == "1d00ffff"
    assert SigNet().pow_limit_bits.hex() == "1e0377ae"
    assert RegTest().pow_limit_bits.hex() == "207fffff"


def test_genesis():
    assert (
        Main().genesis.hash.hex()
        == "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
    )
    assert (
        TestNet().genesis.hash.hex()
        == "000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943"
    )
    assert (
        SigNet().genesis.hash.hex()
        == "00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6"
    )
