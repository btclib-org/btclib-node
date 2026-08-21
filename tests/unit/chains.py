# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from btclib_node.chains import Main, SigNet, TestNet


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
