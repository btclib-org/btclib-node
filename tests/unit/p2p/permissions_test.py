# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.p2p.permissions`, Core's `-whitelist`."""

import pytest

from btclib_node.p2p.address import peer_address
from btclib_node.p2p.permissions import (
    NetPermissionFlags,
    Whitelist,
    permission_names,
)

# what a value naming no permission is granted, with `-whitelistrelay` on
_DEFAULT = ["noban", "relay", "mempool", "download"]


def _names(whitelist: Whitelist, host: str, **kwargs: bool) -> list[str]:
    return permission_names(whitelist.flags(peer_address(host, 8333), **kwargs))


@pytest.mark.parametrize(
    ("value", "names"),
    [
        ("noban@1.2.3.4", ["noban", "download"]),
        ("download@1.2.3.4", ["download"]),
        ("bloomfilter@1.2.3.4", ["bloomfilter"]),
        ("bloom@1.2.3.4", ["bloomfilter"]),
        ("relay@1.2.3.4", ["relay"]),
        ("forcerelay@1.2.3.4", ["forcerelay", "relay"]),
        ("mempool@1.2.3.4", ["mempool"]),
        ("addr@1.2.3.4", ["addr"]),
        (
            "all@1.2.3.4",
            [
                "bloomfilter",
                "noban",
                "forcerelay",
                "relay",
                "mempool",
                "download",
                "addr",
            ],
        ),
        # `permission.length() == 0` is allowed, so a name may be empty or
        # repeated, and a list with none names no permission at all
        ("noban,,relay,noban@1.2.3.4", ["noban", "relay", "download"]),
        ("@1.2.3.4", []),
        ("in,noban@1.2.3.4", ["noban", "download"]),
        # the same four names, in the order `ToStrings` lists them
        ("1.2.3.4", _DEFAULT),
    ],
)
def test_a_value_grants_core_s_permissions_in_core_s_order(
    value: str, names: list[str]
) -> None:
    """`TryParsePermissionFlags`'s names in `ToStrings`' order."""
    assert _names(Whitelist.parse([value]), "1.2.3.4", inbound=True) == names


@pytest.mark.parametrize(
    ("value", "message"),
    [
        # measured on bitcoind v31.1.0
        ("bogus@1.2.3.4", "Invalid P2P permission: 'bogus'"),
        ("noban,Relay@1.2.3.4", "Invalid P2P permission: 'Relay'"),
        ("in@1.2.3.4", "Only direction was set, no permissions: 'in@1.2.3.4'"),
        ("out,in@1.2.3.4", "Only direction was set, no permissions: 'out,in@1.2.3.4'"),
        ("noban@nothing", "Invalid netmask specified in -whitelist: 'nothing'"),
        ("1.2.3.4/33", "Invalid netmask specified in -whitelist: '1.2.3.4/33'"),
        ("", "Invalid netmask specified in -whitelist: ''"),
    ],
)
def test_a_value_core_refuses_is_refused_with_core_s_message(
    value: str, message: str
) -> None:
    """ISS 1320: the refusals of `TryParsePermissionFlags` and `TryParse`."""
    with pytest.raises(ValueError, match=f"^{message}$"):
        Whitelist.parse(["noban@5.6.7.8", value])


def test_the_first_refused_value_is_the_one_named() -> None:
    """Core stops at the first value it cannot read."""
    with pytest.raises(ValueError, match="'first'"):
        Whitelist.parse(["noban@1.2.3.4", "first@1.2.3.4", "second@1.2.3.4"])


def test_a_network_grants_every_host_it_holds_and_no_other() -> None:
    """The subnet is read as `LookupSubNet` reads it, a netmask included."""
    whitelist = Whitelist.parse(["noban@1.2.3.0/24", "relay@5.6.0.0/255.255.0.0"])
    assert _names(whitelist, "1.2.3.200", inbound=True) == ["noban", "download"]
    assert _names(whitelist, "1.2.4.1", inbound=True) == []
    assert _names(whitelist, "5.6.7.8", inbound=True) == ["relay"]


def test_every_value_holding_the_host_adds_to_what_it_is_granted() -> None:
    """`AddWhitelistPermissionFlags` ORs the flags of every match."""
    whitelist = Whitelist.parse(["mempool@1.2.3.4", "addr@1.2.3.0/24", "relay@9.9.9.9"])
    assert _names(whitelist, "1.2.3.4", inbound=True) == ["mempool", "addr"]


def test_a_value_naming_no_permission_grants_the_defaults_beside_the_others() -> None:
    """`Implicit` is cleared and the defaults added to whatever matched."""
    whitelist = Whitelist.parse(["addr@1.2.3.4", "1.2.3.0/24"])
    assert _names(whitelist, "1.2.3.4", inbound=True) == [
        "noban",
        "relay",
        "mempool",
        "download",
        "addr",
    ]


@pytest.mark.parametrize(
    ("relay", "force_relay", "names"),
    [
        (True, False, ["noban", "relay", "mempool", "download"]),
        (False, False, ["noban", "mempool", "download"]),
        (True, True, ["noban", "forcerelay", "relay", "mempool", "download"]),
        # `forcerelay` holds `relay`, so `-whitelistrelay=0` does not take it
        (False, True, ["noban", "forcerelay", "relay", "mempool", "download"]),
    ],
)
def test_the_defaults_follow_whitelistrelay_and_whitelistforcerelay(
    *, relay: bool, force_relay: bool, names: list[str]
) -> None:
    """ISS 1320: the two options shape the defaults."""
    whitelist = Whitelist.parse(["1.2.3.4"], relay=relay, force_relay=force_relay)
    assert _names(whitelist, "1.2.3.4", inbound=True) == names


def test_a_value_applies_to_incoming_connections_alone_by_default() -> None:
    """Core: "By default, whitelist only applies to incoming connections"."""
    whitelist = Whitelist.parse(["noban@1.2.3.4"])
    assert _names(whitelist, "1.2.3.4", inbound=True) == ["noban", "download"]
    assert _names(whitelist, "1.2.3.4", inbound=False, manual=True) == []


def test_out_applies_to_manual_connections_and_in_to_incoming_ones() -> None:
    """`out` names the outgoing ranges, `in` the incoming; both may be given."""
    whitelist = Whitelist.parse(
        ["out,noban@1.2.3.4", "in,out,mempool@5.6.7.8", "in,addr@9.9.9.9"]
    )
    assert _names(whitelist, "1.2.3.4", inbound=False, manual=True) == [
        "noban",
        "download",
    ]
    assert _names(whitelist, "1.2.3.4", inbound=True) == []
    assert _names(whitelist, "5.6.7.8", inbound=False, manual=True) == ["mempool"]
    assert _names(whitelist, "5.6.7.8", inbound=True) == ["mempool"]
    assert _names(whitelist, "9.9.9.9", inbound=True) == ["addr"]


def test_an_outbound_connection_that_is_not_manual_is_granted_nothing() -> None:
    """Core passes no ranges for an automatic, feeler or addr-fetch dial."""
    whitelist = Whitelist.parse(["out,noban@1.2.3.4"])
    assert _names(whitelist, "1.2.3.4", inbound=False) == []


def test_composite_flags_hold_what_they_imply() -> None:
    """Core's `NoBan` is `download` too and `ForceRelay` is `relay` too."""
    assert NetPermissionFlags.DOWNLOAD in NetPermissionFlags.NO_BAN
    assert NetPermissionFlags.RELAY in NetPermissionFlags.FORCE_RELAY
    assert NetPermissionFlags.NO_BAN not in NetPermissionFlags.DOWNLOAD
