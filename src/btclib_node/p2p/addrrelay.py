# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Which peers a gossiped address is relayed to, as Core's `RelayAddress`.

`RelayAddress` (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7,
the v31.1 tag) picks the same one or two peers for an address for 24
hours at a time, so a peer's `addr_known` keeps the address from going
to it twice, and a peer that sends its own address often gains no reach.
It hashes with SipHash under `P2pManager`'s per-process key, as Core does
with `nSeed0` and `nSeed1`, writing the same 8-byte little-endian words.
"""

from typing import TYPE_CHECKING

from btclib.hashes import siphash
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2

from btclib_node.p2p.eviction import Network

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["is_relayable", "relay_destinations"]

# Core's `ROTATE_ADDR_RELAY_DEST_INTERVAL`, in seconds
_ROTATE_INTERVAL = 24 * 3600

# Core's `RANDOMIZER_ID_ADDRESS_RELAY` (`src/net_processing.cpp`, same sha)
_RANDOMIZER_ID_ADDRESS_RELAY = 0x3CAC0035B5866B90

# `CNetAddr::m_net` of the BIP155 ids that have one here
_CORE_NETWORK: dict[int, Network] = {
    BIP155Network.IPV4: Network.IPV4,
    BIP155Network.IPV6: Network.IPV6,
    BIP155Network.TORV3: Network.ONION,
    BIP155Network.I2P: Network.I2P,
    BIP155Network.CJDNS: Network.CJDNS,
}

_RELAYABLE_NETWORKS = frozenset(
    {
        BIP155Network.IPV4,
        BIP155Network.IPV6,
        BIP155Network.TORV3,
        BIP155Network.I2P,
        BIP155Network.CJDNS,
    }
)


def is_relayable(address: NetworkAddressV2) -> bool:
    """Core's `CNetAddr::IsRelayable`: IPv4, IPv6, Tor, I2P or CJDNS."""
    return address.network_id in _RELAYABLE_NETWORKS


def _word(value: int) -> bytes:
    """Return `value` as `CSipHasher::Write(uint64_t)` takes it."""
    return (value % 2**64).to_bytes(8, "little")


def _service_hash(address: NetworkAddressV2) -> int:
    """Return `CServiceHash(0, 0)(address)`: network, port and octets."""
    network = _CORE_NETWORK.get(address.network_id, Network.UNROUTABLE)
    return siphash(0, 0, _word(network) + _word(address.port) + address.address)


def relay_destinations(
    key: tuple[int, int],
    address: NetworkAddressV2,
    peer_ids: Iterable[int],
    *,
    reachable: bool,
    now: float,
) -> list[int]:
    """Return the ids of the peers to relay `address` to, at most two.

    A reachable address goes to two peers; any other to one or two, by
    one bit of the hash. A peer ranks by the hash of the address, the
    24-hour window and its id; one that hashes to 0 is passed over, and a
    tie goes to the lower id. The address's unkeyed hash offsets the
    window, so addresses rotate their peers at different times.
    """
    address_hash = _service_hash(address)
    window = (int(now) + address_hash) % 2**64 // _ROTATE_INTERVAL
    seed = _word(_RANDOMIZER_ID_ADDRESS_RELAY) + _word(address_hash) + _word(window)
    count = 2 if reachable or siphash(*key, seed) & 1 else 1
    ranked = sorted(
        (
            (siphash(*key, seed + _word(peer_id)), peer_id)
            for peer_id in sorted(peer_ids)
        ),
        key=lambda ranked_peer: -ranked_peer[0],
    )
    return [peer_id for rank, peer_id in ranked[:count] if rank]
