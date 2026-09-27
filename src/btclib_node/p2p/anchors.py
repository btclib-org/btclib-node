# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The block-relay-only peers kept across a restart, in `anchors.dat`.

`dump_anchors` is Core's `DumpAnchors` and `read_anchors` its
`ReadAnchors` (`src/addrdb.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
tag), in the same file format, so that either program reads the other's:
the network magic, a `CompactSize` count, each address as
`CAddress::V2_DISK` writes it -- a four-octet disk version, then the
`addrv2` entry -- and the double SHA-256 of everything before it.
"""

import os
import secrets
from io import BytesIO
from typing import TYPE_CHECKING

from btclib import var_int
from btclib.hashes import hash256
from btclib.p2p.address import TimestampedNetworkAddress
from btclib.p2p.addrv2 import NetworkAddressV2, peer_from_addr_entry
from btclib.utils import read_exactly

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = [
    "ANCHORS_DATABASE_FILENAME",
    "MAX_BLOCK_RELAY_ONLY_ANCHORS",
    "dump_anchors",
    "read_anchors",
]

# Core's `MAX_BLOCK_RELAY_ONLY_ANCHORS` and `ANCHORS_DATABASE_FILENAME`
# (`src/net.cpp`)
MAX_BLOCK_RELAY_ONLY_ANCHORS = 2
ANCHORS_DATABASE_FILENAME = "anchors.dat"

# `CAddress`'s `DISK_VERSION_INIT` with `DISK_VERSION_ADDRV2` set, and
# the mask of the low bits Core ignores on reading (`src/protocol.h`)
_DISK_VERSION_INIT = 220000
_DISK_VERSION_ADDRV2 = 1 << 29
_DISK_VERSION_IGNORE_MASK = 0x7FFFF
_DISK_VERSION_SIZE = 4
_CHECKSUM_SIZE = 32


def dump_anchors(path: Path, magic: bytes, anchors: list[NetworkAddressV2]) -> None:
    """Write `anchors` to `path`, through a temporary file renamed over it.

    `SerializeFileDB`'s own sequence: a randomly named file beside the
    target, synced, then renamed into place, and removed where any step
    fails before the error is raised on.
    """
    data = magic + var_int.serialize(len(anchors))
    version = (_DISK_VERSION_INIT | _DISK_VERSION_ADDRV2).to_bytes(
        _DISK_VERSION_SIZE, "little"
    )
    for address in anchors:
        data += version + address.serialize()
    data += hash256(data)
    temporary = path.with_name(f"anchors.{secrets.randbelow(1 << 16):04x}")
    try:
        with temporary.open("wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _read_address(stream: BytesIO) -> NetworkAddressV2:
    """Read one `CAddress::V2_DISK` entry, either encoding its version names.

    Core writes the `addrv2` encoding alone, and reads the `addr` one as
    well where the version's high bits are clear.
    """
    version = int.from_bytes(
        read_exactly(stream, _DISK_VERSION_SIZE, "disk version"), "little"
    )
    version &= ~_DISK_VERSION_IGNORE_MASK
    if version == _DISK_VERSION_ADDRV2:
        return NetworkAddressV2.parse(stream)
    if version == 0:
        return peer_from_addr_entry(TimestampedNetworkAddress.parse(stream))
    err_msg = "Unsupported CAddress disk format version"
    raise ValueError(err_msg)


def read_anchors(
    path: Path, magic: bytes, log: Callable[..., object] | None = None
) -> list[NetworkAddressV2]:
    """Return the addresses `path` holds, then delete it.

    A whole file is reported to `log`, where one is given, in the line
    Core's `ReadAnchors` logs.

    Anything short of a whole file for this network, checksum included,
    answers no address, as Core's `ReadAnchors` catches every exception
    of its read; what follows the checksum is not read, as Core does not
    read it. The file goes either way, so an anchor is tried on one
    start only.
    """
    try:
        data = path.read_bytes()
        stream = BytesIO(data)
        if read_exactly(stream, len(magic), "network magic") != magic:
            return []
        anchors = [_read_address(stream) for _ in range(var_int.parse(stream))]
        end = stream.tell()
        if read_exactly(stream, _CHECKSUM_SIZE, "checksum") != hash256(data[:end]):
            return []
        if log is not None:
            log('Loaded %i addresses from "%s"', len(anchors), path.name)
    except Exception:  # noqa: BLE001
        return []
    finally:
        path.unlink(missing_ok=True)
    return anchors
