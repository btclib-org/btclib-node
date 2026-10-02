# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The permissions `-whitelist` grants a peer, as Core's `NetPermissionFlags`.

`NetPermissionFlags` is Core's enum, `Whitelist` is `-whitelist` parsed as
`NetWhitelistPermissions::TryParse` does (`src/net_permissions.cpp`) and
`Whitelist.flags` is `CConnman::AddWhitelistPermissionFlags` (`src/net.cpp`),
all read at bitcoin/bitcoin@9be056a8a7, the v31.1 tag. A value is read by
`lookup_subnet`, the ban list's `LookupSubNet`.

`-whitebind` is not read: btclib-org/btclib-node#1625.

Each permission does here what it does in Core, wherever this node has
the behaviour it changes: `NO_BAN` (ban, discouragement, eviction, the
headers work check, the preferred download peer, the stalled headers
sync, the transaction trickle, the pruned block threshold), `DOWNLOAD`
(`getheaders` below the minimum chain work), `ADDR` (address rate limit
and the `getaddr` cache), `RELAY` (the limit on announcements) and
`FORCE_RELAY` (relay of a known transaction, no `feefilter`).
`BLOOM_FILTER` and `MEMPOOL` are accepted and listed by `getpeerinfo`
and change nothing: this node answers no BIP37 request and no BIP35
`mempool`, whoever asks. `DOWNLOAD`'s `-maxuploadtarget` and `RELAY`'s
`-blocksonly` are not options of this node.
"""

from dataclasses import dataclass
from enum import IntFlag
from typing import TYPE_CHECKING

from btclib_node.p2p.banman import Subnet, lookup_subnet

if TYPE_CHECKING:
    from collections.abc import Iterable

    from btclib.p2p.addrv2 import NetworkAddressV2

__all__ = [
    "NET_PERMISSIONS_DOC",
    "NetPermissionFlags",
    "Whitelist",
    "WhitelistEntry",
    "permission_names",
]


class NetPermissionFlags(IntFlag):
    """Core's `NetPermissionFlags`, bit for bit.

    A composite holds the permission it implies: `NO_BAN` holds `DOWNLOAD`
    and `FORCE_RELAY` holds `RELAY`. `IMPLICIT` marks a `-whitelist` value
    that named no permission, so that `Whitelist.flags` grants the default
    set.
    """

    NONE = 0
    BLOOM_FILTER = 1 << 1
    RELAY = 1 << 3
    FORCE_RELAY = (1 << 2) | RELAY
    DOWNLOAD = 1 << 6
    NO_BAN = (1 << 4) | DOWNLOAD
    MEMPOOL = 1 << 5
    ADDR = 1 << 7
    IMPLICIT = 1 << 31
    ALL = BLOOM_FILTER | FORCE_RELAY | RELAY | NO_BAN | MEMPOOL | DOWNLOAD | ADDR


# `NetPermissions::ToStrings`' names and their order
_NAMES = (
    (NetPermissionFlags.BLOOM_FILTER, "bloomfilter"),
    (NetPermissionFlags.NO_BAN, "noban"),
    (NetPermissionFlags.FORCE_RELAY, "forcerelay"),
    (NetPermissionFlags.RELAY, "relay"),
    (NetPermissionFlags.MEMPOOL, "mempool"),
    (NetPermissionFlags.DOWNLOAD, "download"),
    (NetPermissionFlags.ADDR, "addr"),
)

# `TryParsePermissionFlags`' names; `bloom` is a second one for `bloomfilter`
_BY_NAME = {
    "bloomfilter": NetPermissionFlags.BLOOM_FILTER,
    "bloom": NetPermissionFlags.BLOOM_FILTER,
    "noban": NetPermissionFlags.NO_BAN,
    "forcerelay": NetPermissionFlags.FORCE_RELAY,
    "mempool": NetPermissionFlags.MEMPOOL,
    "download": NetPermissionFlags.DOWNLOAD,
    "all": NetPermissionFlags.ALL,
    "relay": NetPermissionFlags.RELAY,
    "addr": NetPermissionFlags.ADDR,
}


# What `-help` says of each permission: Core's `NET_PERMISSIONS_DOC`
# (`src/net_permissions.cpp`), cut to what this node does. It has no BIP37
# or BIP35, no `-blocksonly` and no `-maxuploadtarget`.
NET_PERMISSIONS_DOC = (
    "bloomfilter (accepted, does nothing: no BIP37)",
    "noban (do not ban for misbehavior; implies download)",
    "forcerelay (relay transactions that are already in the mempool; implies relay)",
    "relay (unlimited transaction announcements)",
    "mempool (accepted, does nothing: no BIP35)",
    "download (allow getheaders during IBD)",
    (
        "addr (responses to GETADDR avoid hitting the cache and contain random"
        " records with the most up-to-date info)"
    ),
)


def permission_names(flags: NetPermissionFlags) -> list[str]:
    """Return `getpeerinfo`'s names, Core's `NetPermissions::ToStrings`."""
    return [name for flag, name in _NAMES if flag in flags]


@dataclass(frozen=True)
class WhitelistEntry:
    """Core's `NetWhitelistPermissions`: a subnet and what it is granted."""

    subnet: Subnet
    flags: NetPermissionFlags


def _parse(text: str) -> tuple[WhitelistEntry, bool, bool]:
    """Read one `-whitelist` value, as Core's `TryParse` does.

    Returns the entry and whether it applies to incoming and to outgoing
    connections. Raises `ValueError` with Core's message.
    """
    flags = NetPermissionFlags.NONE
    incoming = outgoing = False
    at = text.find("@")
    if at < 0:
        flags |= NetPermissionFlags.IMPLICIT
    else:
        for name in text[:at].split(","):
            if name in _BY_NAME:
                flags |= _BY_NAME[name]
            elif name == "in":
                incoming = True
            elif name == "out":
                outgoing = True
            elif name:
                msg = f"Invalid P2P permission: '{name}'"
                raise ValueError(msg)
    if not incoming and not outgoing:
        # a whitelist applies to incoming connections alone by default
        incoming = True
    elif flags == NetPermissionFlags.NONE:
        msg = f"Only direction was set, no permissions: '{text}'"
        raise ValueError(msg)
    network = text[at + 1 :]
    subnet = lookup_subnet(network)
    if subnet is None:
        msg = f"Invalid netmask specified in -whitelist: '{network}'"
        raise ValueError(msg)
    return WhitelistEntry(subnet, flags), incoming, outgoing


@dataclass(frozen=True)
class Whitelist:
    """The `-whitelist` values, split by the direction they apply to.

    `relay` and `force_relay` are `-whitelistrelay` and
    `-whitelistforcerelay`, which add to the default set a value naming no
    permission is granted.
    """

    incoming: tuple[WhitelistEntry, ...] = ()
    outgoing: tuple[WhitelistEntry, ...] = ()
    relay: bool = True
    force_relay: bool = False

    @classmethod
    def parse(
        cls, values: Iterable[str], *, relay: bool = True, force_relay: bool = False
    ) -> Whitelist:
        """Read every `-whitelist` value, in order.

        Raises `ValueError` with Core's message for the first one refused.
        """
        incoming: list[WhitelistEntry] = []
        outgoing: list[WhitelistEntry] = []
        for value in values:
            entry, is_incoming, is_outgoing = _parse(value)
            if is_incoming:
                incoming.append(entry)
            if is_outgoing:
                outgoing.append(entry)
        return cls(tuple(incoming), tuple(outgoing), relay, force_relay)

    def flags(
        self, address: NetworkAddressV2, *, inbound: bool, manual: bool = False
    ) -> NetPermissionFlags:
        """Return what a peer at `address` is granted.

        Core's `AddWhitelistPermissionFlags`.

        An inbound peer is matched against the incoming values and a
        manual outbound one against the outgoing values; no other
        connection is granted anything.
        """
        if inbound:
            entries = self.incoming
        elif manual:
            entries = self.outgoing
        else:
            return NetPermissionFlags.NONE
        flags = NetPermissionFlags.NONE
        for entry in entries:
            if entry.subnet.matches_peer(address):
                flags |= entry.flags
        if NetPermissionFlags.IMPLICIT in flags:
            flags &= ~NetPermissionFlags.IMPLICIT
            if self.force_relay:
                flags |= NetPermissionFlags.FORCE_RELAY
            if self.relay:
                flags |= NetPermissionFlags.RELAY
            flags |= NetPermissionFlags.MEMPOOL | NetPermissionFlags.NO_BAN
        return flags
