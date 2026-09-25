# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.
# Copyright (c) 2014-present The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file BITCOIN_CORE_COPYING or https://opensource.org/license/mit.

r"""Write `src/btclib_node/_chainparamsseeds.py` from the lists beside it.

Bitcoin Core's `contrib/seeds/generate-seeds.py` (at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which writes
`src/chainparamsseeds.h`: the same parsing and the same BIP155
serialization of each `(networkID, addr, port)` tuple, one line per
seed, emitted as Python rather than as C. The three inputs are Core's
own `contrib/seeds/nodes_main.txt`, `nodes_signet.txt` and
`nodes_test.txt` at that commit; Core's `nodes_testnet4.txt` is left
out, this node having no testnet4.

    uv run python scripts/seeds/generate_seeds.py scripts/seeds \
        > src/btclib_node/_chainparamsseeds.py

Refreshing the seeds is copying Core's newer `nodes_*.txt` here and
running that line, as Core refreshes its header.
"""

import re
import sys
from base64 import b32decode
from enum import IntEnum
from pathlib import Path

_TORV3_LENGTH = 35
_TORV3_VERSION = 3
_TORV2_LENGTH = 10
_I2P_LENGTH = 32
_IPV6_LENGTH = 16
_CJDNS_PREFIX = 0xFC
_ONE_OCTET = 253
_TWO_OCTETS = 0x10000


class BIP155Network(IntEnum):
    """BIP155's network ids, as Core's script names them."""

    IPV4 = 1
    IPV6 = 2
    TORV2 = 3  # no longer supported
    TORV3 = 4
    I2P = 5
    CJDNS = 6


def name_to_bip155(addr: str) -> tuple[BIP155Network, bytes]:
    """Convert an address string to a BIP155 `(networkID, addr)` tuple."""
    if addr.endswith(".onion"):
        onion = b32decode(addr[:-6], casefold=True)
        if len(onion) == _TORV3_LENGTH:
            assert onion[34] == _TORV3_VERSION  # noqa: S101
            return BIP155Network.TORV3, onion[:32]
        if len(onion) == _TORV2_LENGTH:
            return BIP155Network.TORV2, onion
        err_msg = f"Invalid onion {onion!r}"
        raise ValueError(err_msg)
    if addr.endswith(".b32.i2p"):
        i2p = b32decode(addr[:-8] + "====", casefold=True)
        if len(i2p) == _I2P_LENGTH:
            return BIP155Network.I2P, i2p
        err_msg = f"Invalid I2P {i2p!r}"
        raise ValueError(err_msg)
    if "." in addr:
        return BIP155Network.IPV4, bytes(int(x) for x in addr.split("."))
    if ":" in addr:
        return _ipv6_to_bip155(addr)
    err_msg = f"Could not parse address {addr}"
    raise ValueError(err_msg)


def _ipv6_to_bip155(addr: str) -> tuple[BIP155Network, bytes]:
    """Convert an IPv6 or CJDNS address string, the `:` arm of the above."""
    sub: list[list[int]] = [[], []]  # prefix, suffix
    x = 0
    components = addr.split(":")
    for i, comp in enumerate(components):
        if not comp:
            # an empty component at the beginning or the end is skipped
            if i in {0, len(components) - 1}:
                continue
            x += 1  # `::` skips to the suffix
            assert x < 2  # noqa: PLR2004, S101
        else:  # two bytes per component
            val = int(comp, 16)
            sub[x].append(val >> 8)
            sub[x].append(val & 0xFF)
    nullbytes = _IPV6_LENGTH - len(sub[0]) - len(sub[1])
    assert (x == 0 and nullbytes == 0) or (x == 1 and nullbytes > 0)  # noqa: S101
    addr_bytes = bytes(sub[0] + [0] * nullbytes + sub[1])
    if addr_bytes[0] == _CJDNS_PREFIX:
        # Assume that seeds with fc00::/8 addresses belong to CJDNS,
        # not to the publicly unroutable "Unique Local Unicast" network,
        # see RFC4193
        return BIP155Network.CJDNS, addr_bytes
    return BIP155Network.IPV6, addr_bytes


def parse_spec(s: str) -> tuple[BIP155Network, bytes, int] | None:
    """Convert an endpoint string to a `(networkID, addr, port)` tuple."""
    match = re.match(r"\[([0-9a-fA-F:]+)\](?::([0-9]+))?$", s)
    if match:  # ipv6
        host, port = match.group(1), match.group(2)
    elif s.count(":") > 1:  # ipv6, no port
        host, port = s, ""
    else:
        host, _, port = s.partition(":")
    network, addr = name_to_bip155(host)
    if network == BIP155Network.TORV2:
        return None  # TORV2 is no longer supported, so it is ignored
    return network, addr, int(port) if port else 0


def ser_compact_size(length: int) -> bytes:
    """Serialize a length as Bitcoin's compact size."""
    if length < _ONE_OCTET:
        return length.to_bytes(1, "little")
    if length < _TWO_OCTETS:
        return (253).to_bytes(1, "little") + length.to_bytes(2, "little")
    return (254).to_bytes(1, "little") + length.to_bytes(4, "little")


def bip155_serialize(spec: tuple[BIP155Network, bytes, int]) -> bytes:
    """Serialize a `(networkID, addr, port)` tuple to BIP155's binary format."""
    network, addr, port = spec
    return (
        network.to_bytes(1, "little")
        + ser_compact_size(len(addr))
        + addr
        + port.to_bytes(2, "big")
    )


def process_nodes(lines: list[str], name: str) -> list[str]:
    """Return the Python lines of one chain's constant, one seed per line."""
    out = [f"{name} = bytes.fromhex("]
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        spec = parse_spec(line)
        if spec is None:  # an entry no longer supported, such as TORV2
            continue
        out.append(f'    "{bip155_serialize(spec).hex()}"')
    out.append(")")
    return out


_HEADER = '''# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.
# Copyright (c) The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file scripts/seeds/BITCOIN_CORE_COPYING or https://opensource.org/license/mit.

"""The fixed seed nodes of each network, Core's `src/chainparamsseeds.h`.

AUTOGENERATED by `scripts/seeds/generate_seeds.py` from Bitcoin Core's own
`contrib/seeds/` lists, vendored beside it. Each line is one BIP155
serialized `(networkID, addr, port)` tuple.
"""
'''

_CHAINS = (
    ("nodes_main.txt", "CHAINPARAMS_SEED_MAIN"),
    ("nodes_signet.txt", "CHAINPARAMS_SEED_SIGNET"),
    ("nodes_test.txt", "CHAINPARAMS_SEED_TEST"),
)


def generate(indir: Path) -> str:
    """Return the text of `_chainparamsseeds.py` from the lists in `indir`."""
    out = [_HEADER]
    for filename, name in _CHAINS:
        with (indir / filename).open(encoding="utf-8") as f:
            out.append("\n".join(process_nodes(f.readlines(), name)) + "\n")
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:  # noqa: PLR2004
        sys.stderr.write(f"Usage: {sys.argv[0]} <path_to_nodes_txt>\n")
        sys.exit(1)
    # bytes, so that no platform turns the newlines into its own
    sys.stdout.buffer.write(generate(Path(sys.argv[1])).encode())
