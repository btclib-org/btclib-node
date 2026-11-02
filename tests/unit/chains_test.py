# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Every `Chain` subclass, checked against Bitcoin Core.

Consensus, the headers-sync parameters, the genesis block and the magic
are btclib's, looked up by `name`. This file checks that each
subclass's name reaches Core's genesis hash, magic and easiest target,
and btclib's own rows. Its seeds and its `min_bip9_warning_height`,
which the subclass holds itself, are checked against Core's.
"""

import runpy
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from btclib.consensus import CONSENSUS_PARAMS, HEADERS_SYNC_PARAMS, ConsensusParams
from btclib.script.engine.flags import ScriptFlag

from btclib_node.chains import (
    BURIED_DEPLOYMENTS,
    Chain,
    Main,
    RegTest,
    SigNet,
    TestNet,
    TestNet4,
)

CHAINS = (Main(), TestNet(), SigNet(), RegTest(), TestNet4())

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
    "testnet4": {
        "pow_limit_bits": "1d00ffff",
        "magic": "1c163f28",
        "genesis": "00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043",
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
    # takes its own fields, so building one out of `__subclasses__`
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
    # without it hashes the same and is still wrong: it is the copy
    # `Node.load` writes to `block_db` and serves to peers, and the
    # BIP158 filter of height zero is built from its outputs
    for chain in CHAINS:
        (coinbase,) = chain.genesis_block.transactions
        assert coinbase.is_coinbase, chain.name
        # one transaction, so the merkle root is its own id
        assert chain.genesis.merkle_root == coinbase.id, chain.name
        assert coinbase.vout[0].script_pub_key.script, chain.name


def test_consensus_is_the_row_of_the_same_name_in_btclibs_own_table() -> None:
    """`Chain.consensus` reaches `btclib.consensus.CONSENSUS_PARAMS` by name.

    On a chain no height was moved on, not a copy: the same frozen row
    every other reader of that table gets, so a value read off
    `chain.consensus` and one read off `CONSENSUS_PARAMS[chain.name]`
    can never disagree.
    """
    for chain in CHAINS:
        assert chain.consensus is CONSENSUS_PARAMS[chain.name], chain.name


def test_buried_deployments_name_every_activation_height() -> None:
    """Each of Core's names moves its own field of `ConsensusParams`."""
    heights = {f.name for f in fields(ConsensusParams) if f.name.endswith("_height")}
    assert set(BURIED_DEPLOYMENTS.values()) == heights
    for name, field_name in BURIED_DEPLOYMENTS.items():
        moved = RegTest().with_activation_heights({name: 7}).consensus
        changes: dict[str, Any] = {field_name: 7}
        assert moved == replace(RegTest().consensus, **changes), name


def test_moved_heights_are_the_copy_s_alone() -> None:
    """ISS 2155: the copy carries a row of its own, the original the shared one.

    A second call moves on top of the first, a later height for the same
    name winning, so two nodes in one process can carry different rows.
    """
    regtest = RegTest()
    moved = regtest.with_activation_heights({"segwit": 432, "cltv": 1351})
    again = moved.with_activation_heights({"csv": 432, "segwit": 500})
    assert regtest.consensus is CONSENSUS_PARAMS["regtest"]
    assert regtest != moved
    assert moved.consensus == replace(
        regtest.consensus, segwit_height=432, bip65_height=1351
    )
    assert again.consensus == replace(
        regtest.consensus, segwit_height=500, bip65_height=1351, csv_height=432
    )
    assert moved.consensus is moved.consensus
    assert (moved.name, moved.port, moved.genesis) == (
        regtest.name,
        regtest.port,
        regtest.genesis,
    )


def test_moved_cltv_binds_the_script_flags_from_its_height() -> None:
    """`script_flags_at`, `interpreter.get_flags`'s answer, follows the row."""
    consensus = RegTest().with_activation_heights({"cltv": 10}).consensus
    assert not consensus.script_flags_at(9) & ScriptFlag.CHECKLOCKTIMEVERIFY
    assert consensus.script_flags_at(10) & ScriptFlag.CHECKLOCKTIMEVERIFY


def test_the_fixed_seeds_regenerate_from_cores_lists() -> None:
    """ISS 1099: `_chainparamsseeds.py` is what its generator writes.

    `scripts/seeds/generate_seeds.py` is Core's `generate-seeds.py`, and
    its inputs are Core's `nodes_*.txt`, so a hand edit to the module, or
    a list refreshed without regenerating it, fails here.
    """
    root = Path(__file__).parents[2]
    generate = runpy.run_path(str(root / "scripts/seeds/generate_seeds.py"))["generate"]
    # text on both sides, so that a checkout's own line endings, CRLF on
    # a Windows runner, decide nothing
    committed = (root / "src/btclib_node/_chainparamsseeds.py").read_text("utf-8")
    assert generate(root / "scripts/seeds") == committed, (
        "regenerate with: uv run python scripts/seeds/generate_seeds.py"
        " scripts/seeds > src/btclib_node/_chainparamsseeds.py"
    )


def test_regtest_alone_has_no_fixed_seed() -> None:
    """ISS 1099: Core clears `vFixedSeeds` for regtest alone of these five."""
    assert {chain.name: bool(chain.fixed_seeds) for chain in CHAINS} == {
        "mainnet": True,
        "testnet": True,
        "signet": True,
        "regtest": False,
        "testnet4": True,
    }


def test_each_chain_asks_core_s_dns_seeds() -> None:
    """ISS 1303: Core's `vSeeds` at bitcoin/bitcoin@9be056a8a7, the v31.1 tag.

    Read from `src/kernel/chainparams.cpp` there, in its order and with
    its trailing dots, for every chain this package defines.
    """
    assert {chain.name: chain.addresses for chain in CHAINS} == {
        "mainnet": [
            "seed.bitcoin.sipa.be.",
            "dnsseed.bluematt.me.",
            "seed.bitcoin.jonasschnelli.ch.",
            "seed.btc.petertodd.net.",
            "seed.bitcoin.sprovoost.nl.",
            "dnsseed.emzy.de.",
            "seed.bitcoin.wiz.biz.",
            "seed.mainnet.achownodes.xyz.",
        ],
        "testnet": [
            "testnet-seed.bitcoin.jonasschnelli.ch.",
            "seed.tbtc.petertodd.net.",
            "seed.testnet.bitcoin.sprovoost.nl.",
            "testnet-seed.bluematt.me.",
            "seed.testnet.achownodes.xyz.",
        ],
        "signet": [
            "seed.signet.bitcoin.sprovoost.nl.",
            "seed.signet.achownodes.xyz.",
        ],
        "regtest": ["dummySeed.invalid."],
        "testnet4": [
            "seed.testnet4.bitcoin.sprovoost.nl.",
            "seed.testnet4.wiz.biz.",
        ],
    }


def test_each_chain_carries_btclib_s_headers_sync_params() -> None:
    """Each chain's row of `btclib.consensus.HEADERS_SYNC_PARAMS`, by name."""
    for chain in CHAINS:
        assert chain.headers_sync_params is HEADERS_SYNC_PARAMS[chain.name]


def test_each_chain_carries_core_s_min_bip9_warning_height() -> None:
    """ISS 1475: `MinBIP9WarningHeight`, at bitcoin/bitcoin@9be056a8a7.

    Read from `src/kernel/chainparams.cpp` there, the v31.1 tag, for every
    chain this package defines.
    """
    assert {chain.name: chain.min_bip9_warning_height for chain in CHAINS} == {
        "mainnet": 483840,
        "testnet": 836640,
        "signet": 0,
        "regtest": 0,
        "testnet4": 0,
    }
