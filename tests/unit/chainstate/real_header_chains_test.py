# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Real header chains through `BlockIndex.add_headers`.

Every other test of header acceptance feeds it a chain this suite mined
at regtest difficulty, where the target never moves. These two were
mined by the network, so they carry what a regtest chain cannot: a
retarget that changes the target, and on testnet4 a block mined at the
minimum difficulty, with a retarget after it.

The files in `_data/` hold the headers of heights 1 up, the genesis being
the chain's own. A record is the 48 bytes of a header that its
predecessor does not already give: version, merkle root, time, bits and
nonce. `_headers` puts the previous hash back, so a record is read only
as the continuation of the one before. Both files are `xz`.

- `mainnet_headers_1_32300.xz` is `getblockheader <hash> false` of the
  heights 1 to 32,300, read from a Bitcoin Core v31.1.0 node on mainnet.
  Height 32,256 is the first retarget that changes the target, and 32,300
  is far enough past it to carry the new one on. The stretch starts at
  the genesis because an index grows from nothing else.
- `testnet4_headers_1_8100.xz` is a testnet4 peer's answer
  (`seed.testnet4.wiz.biz`) to `getheaders` from the genesis. The heights
  1 to 8,100 of `https://mempool.space/testnet4/api/v1/blocks/` are the
  same bytes. Height 7,465 is a minimum difficulty block, 49,537 seconds
  after its parent, in a period of target `1c03fffc`; height 8,064 is the
  next retarget, which scales that period's first target.

The expected hashes, bits and chain work are Core's `getblockheader`
answers. Mainnet's are the node's own. Testnet4's are those of a second
Core v31.1.0 on testnet4 with no peer, which took the headers by
`submitheader` and accepted all of them. Bitcoin Core's `chainparams.cpp`
holds no checkpoint to compare a hash with: the chain is authenticated by
being one chain from the genesis, each header meeting its target.

The refused headers are real ones with other bits and a nonce found
offline for those bits. Each meets its own target, so that only the
context can refuse it: a nonce that did not would be refused as
`high-hash`, and the test would not be about the rule it names. The same
three headers are refused `bad-diffbits` by `submitheader` on the
Core v31.1.0 nodes above, the mainnet chain loaded on a second one.
"""

import hashlib
import lzma
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from btclib.block import BlockHeader

from btclib_node.chains import Main, TestNet4
from btclib_node.chainstate import Chainstate
from btclib_node.exceptions import MisbehavingError
from btclib_node.log import Logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from btclib_node.chains import Chain

_DATA = Path(__file__).parent / "_data"

# add_headers is given what a peer gives it, a `headers` message
_BATCH = 2000


@dataclass(frozen=True)
class Milestone:
    """What Core's `getblockheader` answers for one header of a chain."""

    height: int
    hash: str
    bits: str
    chainwork: int


@dataclass(frozen=True)
class RealChain:
    """A vendored stretch of a real chain and what Core says of it."""

    name: str
    file: str
    sha256: str
    chain: Callable[[], Chain]
    milestones: tuple[Milestone, ...]

    @property
    def tip(self) -> Milestone:
        """The last header of the file."""
        return self.milestones[-1]


MAINNET = RealChain(
    name="mainnet",
    file="mainnet_headers_1_32300.xz",
    sha256="ce13c5e5b6be2a2022542151b4039dd8b4d8492a793caa876e1960e9d8d42274",
    chain=Main,
    milestones=(
        # the last header of the first period, at the genesis target
        Milestone(
            32255,
            "00000000984f962134a7291e3693075ae03e521f0ee33378ec30a334d860034b",
            "1d00ffff",
            0x7E007E007E00,
        ),
        # the first retarget that changes the target
        Milestone(
            32256,
            "000000004f2886a170adb7204cb0c7a824217dd24d11a74423d564c4e0904967",
            "1d00d86a",
            0x7E01ACD42DD2,
        ),
        Milestone(
            32300,
            "000000006aabd4d47b6c19f13d562f7d1563d92ab7708efceb28590a5b9161cf",
            "1d00d86a",
            0x7E35B93665EA,
        ),
    ),
)

TESTNET4 = RealChain(
    name="testnet4",
    file="testnet4_headers_1_8100.xz",
    sha256="860b6d82c242de36076b1229c2444c09e1a39419718f3dbfeaa7c6829be2a591",
    chain=TestNet4,
    milestones=(
        Milestone(
            7464,
            "0000000001f772e0fe2d2cb7465dfad41f8e824a2dd38138736f3d1d184c06d5",
            "1c03fffc",
            0x207A207A207A0,
        ),
        # a minimum difficulty block, in a period whose target is 1c03fffc
        Milestone(
            7465,
            "000000002e50fb23e4a4eb84c63c78133546a3340c411e809099d8d44ab5ab20",
            "1d00ffff",
            0x207A307A307A1,
        ),
        Milestone(
            8063,
            "00000000001ef2e4c2fc174354ed357cf313725fc336092733b2699d36342ff8",
            "1c03fffc",
            0x29D239D239D21,
        ),
        # the retarget after it, from the period's first block's bits
        Milestone(
            8064,
            "00000000002edbad13892451b053e4e75ec54b1f3ce5d00f808baa1e4e9bc65e",
            "1c00ffff",
            0x29E239E239E21,
        ),
        Milestone(
            8100,
            "000000000016e48df3bed4ce1386441e767600c862cc8323dd5a5ade8906aaad",
            "1c00ffff",
            0x2C223C223C221,
        ),
    ),
)

REAL_CHAINS = (MAINNET, TESTNET4)


def _headers(real: RealChain) -> list[BlockHeader]:
    """Read the vendored file as the headers of heights 1 up."""
    data = lzma.decompress((_DATA / real.file).read_bytes())
    previous = real.chain().genesis.hash
    headers = []
    for start in range(0, len(data), 48):
        record = data[start : start + 48]
        # the file keeps the hashes in wire order, `hash` is display order
        header = BlockHeader.parse(record[:4] + previous[::-1] + record[4:])
        headers.append(header)
        previous = header.hash
    return headers


@dataclass(frozen=True)
class Accepted:
    """A chainstate that took a whole real chain, and the chain."""

    chainstate: Chainstate
    headers: list[BlockHeader]
    tip: bytes | None


@pytest.fixture(scope="module")
def accept(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Callable[..., Accepted]]:
    """Give `add_headers` a real chain once, a batch at a time as a peer does.

    Module scoped, since taking a chain is most of what this module costs
    and nothing a test does to what it is given outlasts the test: a
    refused header is not indexed.
    """
    done: dict[str, Accepted] = {}

    def accept_chain(real: RealChain) -> Accepted:
        if real.name not in done:
            headers = _headers(real)
            chainstate = Chainstate(
                tmp_path_factory.mktemp(real.name), real.chain(), Logger(debug=True)
            )
            tip = None
            for start in range(0, len(headers), _BATCH):
                batch = headers[start : start + _BATCH]
                tip = chainstate.block_index.add_headers(batch)
            done[real.name] = Accepted(chainstate, headers, tip)
        return done[real.name]

    yield accept_chain
    for accepted in done.values():
        accepted.chainstate.close()


@pytest.mark.parametrize("real", REAL_CHAINS, ids=lambda real: real.name)
def test_the_vendored_file_is_the_one_pinned(real: RealChain) -> None:
    """Fail on any change to a vendored file."""
    assert hashlib.sha256((_DATA / real.file).read_bytes()).hexdigest() == real.sha256


@pytest.mark.parametrize("real", REAL_CHAINS, ids=lambda real: real.name)
def test_a_real_chain_is_accepted_with_the_tip_and_work_core_gives(
    real: RealChain, accept: Callable[..., Accepted]
) -> None:
    """Take every header of the file, ending on Core's tip with Core's work."""
    accepted = accept(real)
    block_index = accepted.chainstate.block_index

    assert accepted.tip == bytes.fromhex(real.tip.hash)
    assert len(accepted.headers) == real.tip.height
    assert block_index.header_index[-1] == accepted.tip

    for milestone in real.milestones:
        header = accepted.headers[milestone.height - 1]
        assert header.hash == bytes.fromhex(milestone.hash)
        assert header.bits == bytes.fromhex(milestone.bits)
        assert block_index.header_dict[header.hash].index == milestone.height
        assert block_index.chainwork[header.hash] == milestone.chainwork


@dataclass(frozen=True)
class Corruption:
    """A real header with other bits, and a nonce found for those bits."""

    real: RealChain
    height: int
    bits: str
    nonce: int


# Each is refused as `bad-diffbits`, by a different rule of
# `next_bits_required`: the first skips the retarget, keeping the target
# of the period before; the second is a minimum difficulty block that
# does not carry the minimum difficulty exactly; the third claims the
# minimum difficulty less than 20 minutes after its parent.
CORRUPTIONS = {
    "retarget-skipped": Corruption(MAINNET, 32256, "1d00ffff", 2330451811),
    "minimum-difficulty-bits-wrong": Corruption(TESTNET4, 7465, "1d00fffe", 111802143),
    "minimum-difficulty-without-the-gap": Corruption(
        TESTNET4, 7466, "1d00ffff", 1708199092
    ),
}


@pytest.mark.parametrize("name", CORRUPTIONS)
def test_a_real_header_with_the_wrong_bits_is_refused_bad_diffbits(
    name: str, accept: Callable[..., Accepted]
) -> None:
    """Refuse a header that meets its own target but not the required one."""
    corruption = CORRUPTIONS[name]
    accepted = accept(corruption.real)
    block_index = accepted.chainstate.block_index
    header = replace(
        accepted.headers[corruption.height - 1],
        bits=bytes.fromhex(corruption.bits),
        nonce=corruption.nonce,
    )
    # the only thing wrong with it is the context
    header.assert_valid_pow(corruption.real.chain().pow_limit_bits)

    with pytest.raises(MisbehavingError, match=r"^bad-diffbits$"):
        block_index.add_headers([header])
    assert header.hash not in block_index.header_dict


@pytest.mark.parametrize("real", REAL_CHAINS, ids=lambda real: real.name)
def test_a_real_chain_missing_a_header_is_refused_as_not_continuous(
    real: RealChain, accept: Callable[..., Accepted]
) -> None:
    """Refuse real headers with one cut out of the middle of them."""
    accepted = accept(real)
    # the retarget header is the one cut
    cut = real.milestones[1 if real is MAINNET else 3].height
    batch = accepted.headers[cut - 6 : cut - 1] + accepted.headers[cut : cut + 5]

    with pytest.raises(MisbehavingError, match=r"^non-continuous headers sequence$"):
        accepted.chainstate.block_index.add_headers(batch)
