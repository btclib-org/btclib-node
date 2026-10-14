# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The node on blocks that were mined for mainnet, not for a test.

The fixtures are in `_data/btcd/` and `_data/floresta/`, as their projects
ship them; `tests/_data/README.md` pins each one.

btcd's `blk_0_to_4`, `blk_3A`, `blk_4A` and `blk_5A` are the genesis block
and blocks mined on top of it at mainnet's easiest target: one branch ends
at block 4, another forks off block 2 and ends at block 5a. `277647` is
mainnet block 277647 beside the outputs it spends. A block file is a
sequence of `<magic><length><block>` records and a UTXO file a sequence of
`<txid><index><length><entry>` records, each entry btcd's compressed
serialization (`blockchain/chainio.go` and `blockchain/compress.go`, at
btcsuite/btcd@c615bd709). Both are read here and nowhere else.

Floresta's `invalid_block_783426` is a block candidate F2Pool mined with
valid proof of work and Core refused as `bad-blk-sigops`: its sigop cost,
counted over the outputs it spends, is 80,003. The outputs are a JSON list
in the order the block's inputs spend them.
"""

import bz2
import json
import struct
from compression import zstd
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from btclib.block import Block
from btclib.block.limits import MAX_BLOCK_SIGOPS_COST
from btclib.exceptions import BTClibValueError
from btclib.script.engine import sig_op_cost
from btclib.tx.out_point import OutPoint
from btclib.tx.tx_out import TxOut

from btclib_node import Node, main
from btclib_node.block_db import Coin
from btclib_node.chainstate import utxo_index as utxo_index_module
from btclib_node.chainstate.block_index import BlockInfo, BlockStatus
from btclib_node.config import Config
from btclib_node.exceptions import BlockScriptVerifyError
from btclib_node.interpreter import check_scripts, get_flags
from tests import conftest
from tests.unit.main_test import connect

if TYPE_CHECKING:
    from collections.abc import Iterator

    from btclib.tx.tx import Tx

_BTCD = Path(__file__).parent / "_data" / "btcd"
_FLORESTA = Path(__file__).parent / "_data" / "floresta"

# the four bytes opening every record of a block file: btcd's `wire.MainNet`
_MAINNET_MAGIC = bytes.fromhex("f9beb4d9")

# `putCompressedScript`'s special script types, and how many there are
# (`numSpecialScripts`, `compress.go`)
_P2PKH, _P2SH, _P2PK_EVEN, _P2PK_ODD, _P2PK_UNCOMPRESSED_EVEN = 0, 1, 2, 3, 4
_P2PK_UNCOMPRESSED_ODD = 5
_SPECIAL_SCRIPTS = 6

# secp256k1's field prime, for decompressing a public key
_FIELD_PRIME = 2**256 - 2**32 - 977

_EXPONENT_OF_THE_ROUND_AMOUNTS = 9


def read_blocks(name: str) -> list[Block]:
    """Parse the `<magic><length><block>` records of one btcd fixture."""
    data = bz2.decompress((_BTCD / f"{name}.dat.bz2").read_bytes())
    blocks = []
    offset = 0
    while offset < len(data):
        assert data[offset : offset + 4] == _MAINNET_MAGIC
        (length,) = struct.unpack_from("<I", data, offset + 4)
        offset += 8
        blocks.append(Block.parse(data[offset : offset + length]))
        offset += length
    return blocks


def _vlq(data: bytes, offset: int) -> tuple[int, int]:
    """Read btcd's variable-length quantity: its value and the next offset."""
    value = 0
    while True:
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset
        value += 1


def _decompress_amount(compressed: int) -> int:
    """Undo `compressTxOutAmount`, which packs a digit and an exponent."""
    if compressed == 0:
        return 0
    compressed -= 1
    exponent = compressed % 10
    compressed //= 10
    if exponent < _EXPONENT_OF_THE_ROUND_AMOUNTS:
        digit = compressed % 9 + 1
        amount = (compressed // 9) * 10 + digit
    else:
        amount = compressed + 1
    return int(amount * 10**exponent)


def _decompress_script(data: bytes, offset: int) -> tuple[bytes, int]:
    """Undo `putCompressedScript`; answer the script and the new offset."""
    kind, offset = _vlq(data, offset)
    if kind == _P2PKH:
        return b"\x76\xa9\x14" + data[offset : offset + 20] + b"\x88\xac", offset + 20
    if kind == _P2SH:
        return b"\xa9\x14" + data[offset : offset + 20] + b"\x87", offset + 20
    if kind in {_P2PK_EVEN, _P2PK_ODD}:
        return b"\x21" + bytes([kind]) + data[offset : offset + 32] + b"\xac", (
            offset + 32
        )
    if kind in {_P2PK_UNCOMPRESSED_EVEN, _P2PK_UNCOMPRESSED_ODD}:
        x = int.from_bytes(data[offset : offset + 32], "big")
        root = pow(x**3 + 7, (_FIELD_PRIME + 1) // 4, _FIELD_PRIME)
        # the prime is odd, so of the two roots one is even and one odd
        y = sorted((root, _FIELD_PRIME - root), key=lambda v: v % 2)[kind % 2]
        key = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
        return b"\x41" + key + b"\xac", offset + 32
    length = kind - _SPECIAL_SCRIPTS
    return data[offset : offset + length], offset + length


def read_utxo_store(name: str) -> dict[OutPoint, Coin]:
    """Parse the `<txid><index><length><entry>` records of one btcd fixture."""
    data = bz2.decompress((_BTCD / f"{name}.utxostore.bz2").read_bytes())
    coins = {}
    offset = 0
    while offset < len(data):
        # btcd writes the hash as it is on the wire; `OutPoint` takes it
        # the way `Tx.id` shows it
        txid = data[offset : offset + 32][::-1]
        index, length = struct.unpack_from("<II", data, offset + 32)
        offset += 40
        end = offset + length
        header, offset = _vlq(data, offset)
        compressed_amount, offset = _vlq(data, offset)
        script, offset = _decompress_script(data, offset)
        assert offset == end
        coins[OutPoint(txid, index)] = Coin(
            TxOut(_decompress_amount(compressed_amount), script),
            header >> 1,
            is_coinbase=bool(header & 1),
        )
    return coins


@pytest.fixture
def mainnet_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Node]:
    """Give a node on mainnet's parameters, closed at teardown.

    `tests.conftest.unstarted_node_context` builds a regtest node and has
    no parameter for the chain, so the `Config` it builds is swapped for
    one that says mainnet.
    """
    monkeypatch.setattr(
        conftest, "Config", lambda **kwargs: Config(**{**kwargs, "chain": "mainnet"})
    )
    with conftest.unstarted_node_context(tmp_path) as node:
        yield node


_Row = tuple[int, bytes, int, bool]


def _expected_utxo_set(blocks: list[Block]) -> dict[bytes, _Row]:
    """Apply `blocks`, from height 1, to an empty set.

    Answers each unspent outpoint's `(value, script, height, coinbase)`,
    from the transactions alone: it shares nothing with the node's own
    bookkeeping it is compared with.
    """
    unspent: dict[bytes, _Row] = {}
    for height, block in enumerate(blocks, start=1):
        for position, tx in enumerate(block.transactions):
            if position:
                for tx_in in tx.vin:
                    del unspent[tx_in.prev_out.serialize()]
            for vout, tx_out in enumerate(tx.vout):
                unspent[OutPoint(tx.id, vout).serialize()] = (
                    tx_out.value,
                    tx_out.script_pub_key.script,
                    height,
                    position == 0,
                )
    return unspent


def _stored_utxo_set(node: Node) -> dict[bytes, _Row]:
    """Read what the node's chainstate holds, once flushed."""
    node.chainstate.flush()
    stored = {}
    for key, value in node.chainstate.utxo_index.cursor():
        coin = Coin.parse(value)
        stored[key.removeprefix(b"utxo-")] = (
            coin.tx_out.value,
            coin.tx_out.script_pub_key.script,
            coin.height,
            coin.is_coinbase,
        )
    return stored


@pytest.mark.parametrize(
    ("compressed", "amount"),
    [
        (0, 0),
        (4, 1000),
        (5, 10000),
        (111111101, 12345678),
        (48, 5 * 10**7),
        (9, 10**8),
        (49, 5 * 10**8),
        (10, 10**9),
    ],
)
def test_the_amounts_btcd_documents_decompress(compressed: int, amount: int) -> None:
    """The examples in the comment above `compressTxOutAmount`.

    The comment says 47 for 50000000; the code and `TestAmountCompression`
    (`compress_test.go`) say 48.
    """
    assert _decompress_amount(compressed) == amount


# btcd's `TestScriptCompression` (`blockchain/compress_test.go`, at
# btcsuite/btcd@c615bd709): the script, then its compressed form
_COMPRESSED_SCRIPTS = [
    ("nil", "", "06"),
    (
        "pay-to-pubkey-hash 1",
        "76a9141018853670f9f3b0582c5b9ee8ce93764ac32b9388ac",
        "001018853670f9f3b0582c5b9ee8ce93764ac32b93",
    ),
    (
        "pay-to-pubkey-hash 2",
        "76a914e34cce70c86373273efcc54ce7d2a491bb4a0e8488ac",
        "00e34cce70c86373273efcc54ce7d2a491bb4a0e84",
    ),
    (
        "pay-to-script-hash 1",
        "a914da1745e9b549bd0bfa1a569971c77eba30cd5a4b87",
        "01da1745e9b549bd0bfa1a569971c77eba30cd5a4b",
    ),
    (
        "pay-to-script-hash 2",
        "a914f815b036d9bbbce5e9f2a00abd1bf3dc91e9551087",
        "01f815b036d9bbbce5e9f2a00abd1bf3dc91e95510",
    ),
    (
        "pay-to-pubkey compressed 0x02",
        "2102192d74d0cb94344c9569c2e77901573d8d7903c3ebec3a957724895dca52c6b4ac",
        "02192d74d0cb94344c9569c2e77901573d8d7903c3ebec3a957724895dca52c6b4",
    ),
    (
        "pay-to-pubkey compressed 0x03",
        "2103b0bd634234abbb1ba1e986e884185c61cf43e001f9137f23c2c409273eb16e65ac",
        "03b0bd634234abbb1ba1e986e884185c61cf43e001f9137f23c2c409273eb16e65",
    ),
    (
        "pay-to-pubkey uncompressed 0x04 even",
        (
            "4104192d74d0cb94344c9569c2e77901573d8d7903c3ebec3a957724895dca52c6b4"
            "0d45264838c0bd96852662ce6a847b197376830160c6d2eb5e6a4c44d33f453eac"
        ),
        "04192d74d0cb94344c9569c2e77901573d8d7903c3ebec3a957724895dca52c6b4",
    ),
    (
        "pay-to-pubkey uncompressed 0x04 odd",
        (
            "410411db93e1dcdb8a016b49840f8c53bc1eb68a382e97b1482ecad7b148a6909a5c"
            "b2e0eaddfb84ccf9744464f82e160bfa9b8b64f9d4c03f999b8643f656b412a3ac"
        ),
        "0511db93e1dcdb8a016b49840f8c53bc1eb68a382e97b1482ecad7b148a6909a5c",
    ),
    (
        "pay-to-pubkey invalid pubkey",
        "3302aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaac",
        "293302aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaac",
    ),
    (
        "null data",
        "6a200102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20",
        "286a200102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20",
    ),
    (
        "requires 2 size bytes - data push 200 bytes",
        "4cc8" + "00" * 200,
        "80504cc8" + "00" * 200,
    ),
]


@pytest.mark.parametrize(
    ("script", "compressed"),
    [pytest.param(s, c, id=name) for name, s, c in _COMPRESSED_SCRIPTS],
)
def test_the_scripts_btcd_compresses_decompress(script: str, compressed: str) -> None:
    """Every case of btcd's `TestScriptCompression`, read back to its script."""
    data = bytes.fromhex(compressed)
    assert _decompress_script(data, 0) == (bytes.fromhex(script), len(data))


def test_a_side_chain_is_stored_and_the_heavier_branch_takes_over(
    mainnet_node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocks 1 to 4 connect; 3a is stored aside; 3a-4a-5a then outweighs them.

    The tip and the whole UTXO set are checked after every step, the set
    against what the blocks' transactions leave unspent. 4a only equals
    block 4 in work, so the tip stays where it was first seen; 5a is what
    reorganizes. Blocks 2, 4 and 5a spend an earlier block's coinbase, and
    btcd's own tests lower coinbase maturity to 1 for that, as this one
    does, through `main.COINBASE_MATURITY`, the name `_check_tx_inputs`
    reads it by.
    """
    monkeypatch.setattr(main, "COINBASE_MATURITY", 1)
    node = mainnet_node
    block_index = node.chainstate.block_index
    genesis, *main_chain = read_blocks("blk_0_to_4")
    (block_3a,) = read_blocks("blk_3A")
    (block_4a,) = read_blocks("blk_4A")
    (block_5a,) = read_blocks("blk_5A")
    assert genesis.header.hash == node.chain.genesis.hash
    assert block_3a.header.previous_block_hash == main_chain[1].header.hash
    tip = main_chain[-1].header.hash

    connect(node, main_chain)
    assert block_index.active_chain[-1] == tip
    assert _stored_utxo_set(node) == _expected_utxo_set(main_chain)

    connect(node, [block_3a])
    assert block_index.active_chain[-1] == tip
    assert block_3a.header.hash not in block_index.active_chain
    side = block_index.get_block_info(block_3a.header.hash)
    assert side.status != BlockStatus.invalid
    assert _stored_utxo_set(node) == _expected_utxo_set(main_chain)

    connect(node, [block_4a])
    assert block_index.active_chain[-1] == tip
    assert _stored_utxo_set(node) == _expected_utxo_set(main_chain)

    connect(node, [block_5a])
    assert block_index.active_chain[-1] == block_5a.header.hash
    assert tip not in block_index.active_chain
    assert _stored_utxo_set(node) == _expected_utxo_set(
        [*main_chain[:2], block_3a, block_4a, block_5a]
    )


_BLOCK_277647 = 277_647


def _block_277647() -> tuple[Block, list[tuple[list[Coin], Tx]]]:
    """Give block 277647 and each of its spends paired with its prevouts.

    The UTXO file holds every output the block spends, the block's own
    included.
    """
    (block,) = read_blocks("277647")
    coins = read_utxo_store("277647")
    spends = [
        ([coins[i.prev_out] for i in tx.vin], tx) for tx in block.transactions[1:]
    ]
    assert len(coins) == sum(len(prevouts) for prevouts, _ in spends)
    return block, spends


def test_every_input_of_block_277647_verifies(mainnet_node: Node) -> None:
    """The scripts of a mainnet block pass under the flags of its height.

    btcd's `TestCheckBlockScripts` does the same under BIP16 alone. The
    flags here are the node's own for this height and block.
    """
    block, spends = _block_277647()
    flags = get_flags(mainnet_node.config, _BLOCK_277647, block.header.hash)

    check_scripts(spends, flags, mainnet_node)


def test_a_corrupted_input_of_block_277647_is_refused(mainnet_node: Node) -> None:
    """The control of the test above: one byte of one signature fails it."""
    block, spends = _block_277647()
    flags = get_flags(mainnet_node.config, _BLOCK_277647, block.header.hash)
    tx = spends[0][1]
    script_sig = bytearray(tx.vin[0].script_sig)
    script_sig[10] ^= 1
    tx.vin[0].script_sig = bytes(script_sig)

    with pytest.raises(BlockScriptVerifyError):
        check_scripts(spends, flags, mainnet_node)


_CANDIDATE = 783_426
_SIGOP_COST = 80_003


@pytest.fixture
def candidate(
    mainnet_node: Node, monkeypatch: pytest.MonkeyPatch
) -> tuple[Node, Block, list[Coin]]:
    """Give a mainnet node holding the outputs block candidate 783426 spends.

    A unit test has no chain of 783425 blocks to connect it to, so these
    stand in for one:

    - the spent outputs are stored as the chainstate would hold them. The
      list also names outputs an earlier transaction of the candidate
      creates; those are left for the node to create;
    - the parent's header is a placeholder, whatever header, under the
      hash the candidate names;
    - `main.contextual_check_block` is replaced by a parent median
      time past of 0 and BIP113 off, which also skips `_validate_block`'s
      BIP68 locks. The real one needs the eleven headers before the
      candidate. What it refuses itself is `bad-txns-nonfinal` and
      `bad-cb-height`, neither of them the sigop rule.

    MuHash, which the node keeps over the set and which is pure Python
    here, costs seconds a block to update and is not under test.
    """
    block = Block.parse(zstd.decompress((_FLORESTA / "raw.zst").read_bytes()))
    rows = json.loads(zstd.decompress((_FLORESTA / "spent_utxos.zst").read_bytes()))
    coins = [
        Coin(
            TxOut(row["txout"]["value"], bytes.fromhex(row["txout"]["script_pubkey"])),
            row["creation_height"],
            is_coinbase=row["is_coinbase"],
        )
        for row in rows
    ]
    inputs = [i for tx in block.transactions[1:] for i in tx.vin]
    assert len(inputs) == len(coins)
    created = {tx.id for tx in block.transactions}
    store = mainnet_node.chainstate.utxo_index.db
    for tx_in, coin in zip(inputs, coins, strict=True):
        if tx_in.prev_out.tx_id not in created:
            store.put(b"utxo-" + tx_in.prev_out.serialize(), coin.serialize())

    mainnet_node.chainstate.block_index.header_dict[
        block.header.previous_block_hash
    ] = BlockInfo(block.header, _CANDIDATE - 1)
    monkeypatch.setattr(main, "contextual_check_block", lambda *_: (0, False))
    monkeypatch.setattr(utxo_index_module.UtxoIndex, "_hash_insert", lambda *_: None)
    monkeypatch.setattr(utxo_index_module.UtxoIndex, "_hash_remove", lambda *_: None)
    return mainnet_node, block, coins


def _running_cost(node: Node, block: Block, coins: list[Coin]) -> list[int]:
    """Count the candidate's sigop cost up to its coinbase and to each spend."""
    flags = get_flags(node.config, _CANDIDATE, block.header.hash)
    costs = [sig_op_cost([], block.transactions[0], flags)]
    spent = iter(coins)
    for tx in block.transactions[1:]:
        prevouts = [next(spent).tx_out for _ in tx.vin]
        costs.append(costs[-1] + sig_op_cost(prevouts, tx, flags))
    return costs


def test_block_candidate_783426_is_refused_for_its_sigop_cost(
    candidate: tuple[Node, Block, list[Coin]],
) -> None:
    """Core's `bad-blk-sigops`, on the block that was refused for it."""
    node, block, coins = candidate

    assert _running_cost(node, block, coins)[-1] == _SIGOP_COST
    with pytest.raises(BTClibValueError, match=r"^bad-blk-sigops$"):
        main.try_connect_block(node, block, _CANDIDATE)


def test_block_candidate_783426_below_the_limit_is_refused_for_something_else(
    candidate: tuple[Node, Block, list[Coin]],
) -> None:
    """The control of the test above: cut before the spend over the limit.

    The coinbase still claims the fees of the transactions cut, so what
    refuses the block is its amount, which the node asks after the sigop
    cost.
    """
    node, block, coins = candidate
    costs = _running_cost(node, block, coins)
    first_over = next(i for i, cost in enumerate(costs) if cost > MAX_BLOCK_SIGOPS_COST)
    cut = Block(block.header, block.transactions[:first_over], check_validity=False)

    with pytest.raises(BTClibValueError, match=r"^bad-cb-amount"):
        main.try_connect_block(node, cut, _CANDIDATE)
