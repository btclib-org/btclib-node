# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`anchors.dat`, in the format bitcoind v31.1.0 reads and writes."""

from typing import TYPE_CHECKING

import pytest
from btclib.hashes import hash256
from btclib.p2p.address import ServiceFlags

from btclib_node.chains import Main, RegTest
from btclib_node.p2p.address import peer_address
from btclib_node.p2p.anchors import dump_anchors, read_anchors

if TYPE_CHECKING:
    from pathlib import Path

REGTEST = RegTest().magic

# What bitcoind v31.1.0 wrote on a clean `stop` holding two
# block-relay-only peers it had been told to open with `addconnection`,
# to 127.0.0.1 on ports 60765 and 60768: the magic, a count of two, each
# address behind the disk version `0x20035b60`, a timestamp of Core's
# `TIME_INIT` and no services, and the checksum.
BITCOIND = bytes.fromhex(
    "fabfb5da02"
    "605b032000e1f5050001047f000001ed5d"
    "605b032000e1f5050001047f000001ed60"
    "7fc3191f025dc4378a0fa97b833d98d2ef8e1b9c37784ecfaccac8481915de61"
)
BITCOIND_ANCHORS = [
    peer_address("127.0.0.1", 60765, timestamp=100_000_000),
    peer_address("127.0.0.1", 60768, timestamp=100_000_000),
]


def test_bitcoind_s_file_is_read_and_written_back_octet_for_octet(
    tmp_path: Path,
) -> None:
    """The two programs read each other's file; reading it deletes it.

    The read is logged in Core's own line.
    """
    path = tmp_path / "anchors.dat"
    path.write_bytes(BITCOIND)
    logged: list[str] = []
    assert (
        read_anchors(path, REGTEST, lambda fmt, *args: logged.append(fmt % args))
        == BITCOIND_ANCHORS
    )
    assert logged == ['Loaded 2 addresses from "anchors.dat"']
    assert not path.exists()
    dump_anchors(path, REGTEST, BITCOIND_ANCHORS)
    assert path.read_bytes() == BITCOIND
    assert [p.name for p in tmp_path.iterdir()] == ["anchors.dat"]


def _sealed(body: bytes) -> bytes:
    return body + hash256(body)


@pytest.mark.parametrize(
    "data",
    [
        BITCOIND[:-1],
        BITCOIND[:-1] + b"\x00",
        _sealed(Main().magic + BITCOIND[4:-32]),
        _sealed(BITCOIND[:5] + (1 << 30).to_bytes(4, "little") + BITCOIND[9:-32]),
        _sealed(BITCOIND[:4] + b"\x03" + BITCOIND[5:-32]),
        b"",
    ],
    ids=["truncated", "checksum", "network", "version", "count", "empty"],
)
def test_a_file_short_of_whole_answers_no_anchor_and_goes(
    tmp_path: Path, data: bytes
) -> None:
    """Every exception of Core's `DeserializeFileDB` is `ReadAnchors`' empty."""
    path = tmp_path / "anchors.dat"
    path.write_bytes(data)
    logged: list[object] = []
    assert read_anchors(path, REGTEST, lambda *args: logged.append(args)) == []
    assert not path.exists()
    assert not logged


def test_no_file_answers_no_anchor(tmp_path: Path) -> None:
    """A first start, or one after a crash: nothing to read, nothing raised."""
    assert read_anchors(tmp_path / "anchors.dat", REGTEST) == []


def test_what_follows_the_checksum_is_not_read(tmp_path: Path) -> None:
    """`DeserializeDB` reads the checksum and stops there."""
    path = tmp_path / "anchors.dat"
    path.write_bytes(BITCOIND + b"\x42")
    assert read_anchors(path, REGTEST) == BITCOIND_ANCHORS


def test_an_addr_encoded_entry_is_read_and_the_low_version_bits_ignored(
    tmp_path: Path,
) -> None:
    """`CAddress`'s disk version: zero high bits are the `addr` encoding.

    The low bits, `DISK_VERSION_IGNORE_MASK`'s, are ignored whatever they
    hold, here one where Core writes 220000.
    """
    entry = (
        (1).to_bytes(4, "little")
        + (1_700_000_000).to_bytes(4, "little")
        + int(ServiceFlags.NODE_NETWORK).to_bytes(8, "little")
        + bytes(10)
        + b"\xff\xff\x01\x02\x03\x04"
        + (8333).to_bytes(2, "big")
    )
    path = tmp_path / "anchors.dat"
    path.write_bytes(_sealed(REGTEST + b"\x01" + entry))
    assert read_anchors(path, REGTEST) == [
        peer_address(
            "1.2.3.4", 8333, timestamp=1_700_000_000, services=ServiceFlags.NODE_NETWORK
        )
    ]


def test_a_write_that_fails_leaves_no_temporary_file(tmp_path: Path) -> None:
    """`SerializeFileDB` removes its temporary file where the rename fails."""
    path = tmp_path / "anchors.dat"
    path.mkdir()
    with pytest.raises(OSError):  # noqa: PT011
        dump_anchors(path, REGTEST, BITCOIND_ANCHORS)
    assert [p.name for p in tmp_path.iterdir()] == ["anchors.dat"]
