# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The ban list: Core's `CSubNet`, `LookupSubNet` and `BanMan`.

Read at bitcoin/bitcoin@9be056a8a7, the v31.1 tag, the cases come from:

- `subnet_test` (`src/test/netbase_tests.cpp`), its IP subnets and its
  onion one;
- `netbase_dont_resolve_strings_with_embedded_nul_characters`, the same
  file;
- the onion and I2P vectors of `src/test/net_tests.cpp`;
- what a regtest bitcoind v31.1 on macOS answered to `setban`: the
  forms `getaddrinfo` reads, trailing whitespace, a scope, an onion
  name in upper case or with a wrong checksum, and the order of the list.

The rest are built here, not taken from Core: an onion or I2P host
with a prefix or as a netmask, a padded onion name, an I2P name with
bits left over, and an onion suffix in upper case.
"""

import json
import logging
import os
import socket
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import TYPE_CHECKING, Any

import pytest
from btclib.p2p.addrv2 import BIP155Network, NetworkAddressV2

import btclib_node.p2p.banman as banman_module
from btclib_node.constants import CLIENT_NAME
from btclib_node.p2p.address import peer_address
from btclib_node.p2p.banman import (
    DEFAULT_MISBEHAVING_BANTIME,
    BanEntry,
    BanMan,
    Subnet,
    lookup_host,
    lookup_subnet,
)

if TYPE_CHECKING:
    from pathlib import Path

_NOW = 1_700_000_000
# one ban `BanMapFromJson` reads, and unexpired at `_NOW`
_ENTRY = (
    '{"version": 1, "ban_created": 1, "banned_until": 1800000000, "address": "1.2.3.4"}'
)
_LOGGER = logging.getLogger(__name__)
# Core's `net_tests` and `netbase_tests` onion, and its `net_tests` I2P
_ONION = "pg6mmjiyjmcrsslvykfwnntlaru7p5svn6y2ymmju6nubxndf4pscryd.onion"
_OTHER_ONION = "kpgvmscirrdqpekbqjsvw5teanhatztpp2gl6eee4zkowvwfxwenqaid.onion"
_I2P = "udhdrtrcetjm5sxzskjyr5ztpeszydbh4dpl3pl4utgqqw2v4jna.b32.i2p"
# the forms Windows' `getaddrinfo` does not read as an address
_ATON = pytest.mark.skipif(os.name == "nt", reason="the resolver's own forms")


@pytest.fixture(autouse=True)
def a_clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stop the clock `BanMan` reads at `_NOW`, movable through the list."""
    clock = [_NOW]
    monkeypatch.setattr(banman_module, "_now", lambda: clock[0])
    return clock


def a_subnet(text: str) -> Subnet:
    """Parse `text`, asserting it parses."""
    subnet = lookup_subnet(text)
    assert subnet is not None
    return subnet


def ip(text: str) -> IPv4Address | IPv6Address:
    """Parse `text` as an IP address, as Core's `ResolveIP` does."""
    return ip_address(text)


@pytest.mark.parametrize(
    ("subnet", "address"),
    [
        ("1.2.3.0/24", "1.2.3.4"),
        ("1.2.3.4", "1.2.3.4"),
        ("1.2.3.4/32", "1.2.3.4"),
        ("::ffff:127.0.0.1", "127.0.0.1"),
        ("1:2:3:4:5:6:7:8", "1:2:3:4:5:6:7:8"),
        ("1:2:3:4:5:6:7:0/112", "1:2:3:4:5:6:7:1234"),
        ("192.168.0.1/24", "192.168.0.2"),
        ("192.168.0.20/29", "192.168.0.18"),
        ("1.2.2.1/24", "1.2.2.4"),
        ("1.2.2.110/31", "1.2.2.111"),
        ("1.2.2.20/26", "1.2.2.63"),
        ("::/0", "1:2:3:4:5:6:7:1234"),
        ("[1:2:3:4:5:6:7:8]", "1:2:3:4:5:6:7:8"),
    ],
)
def test_a_subnet_matches_the_address_it_holds(subnet: str, address: str) -> None:
    """Core's `subnet_test`, the matches."""
    assert a_subnet(subnet).matches(ip(address))


@pytest.mark.parametrize(
    ("subnet", "address"),
    [
        ("1.2.2.0/24", "1.2.3.4"),
        ("1.2.3.4", "5.6.7.8"),
        ("1.2.3.4/32", "5.6.7.8"),
        ("1:2:3:4:5:6:7:8", "1:2:3:4:5:6:7:9"),
        # `::` and `0.0.0.0` are not valid addresses
        ("::/0", "::"),
        ("0.0.0.0/0", "0.0.0.0"),  # noqa: S104
        # one network's addresses are in none of another's subnets
        ("::/0", "1.2.3.4"),
        ("0.0.0.0/0", "1:2:3:4:5:6:7:1234"),
    ],
)
def test_a_subnet_matches_no_address_it_does_not_hold(
    subnet: str, address: str
) -> None:
    """Core's `subnet_test`, the misses."""
    assert not a_subnet(subnet).matches(ip(address))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "bloop",
        "fuzzy",
        "1.2.3.0/-1",
        "1.2.3.0/+24",
        "1.2.3.0/33",
        "1.2.3.0/300",
        "1:2:3:4:5:6:7:8/-1",
        "1:2:3:4:5:6:7:8/129",
        # a netmask of the other network
        "1.1.1.1/ffff::",
        "::1/255.0.0.0",
        # 1-bits after 0-bits
        "1.2.3.4/255.255.232.0",
        "1.2.3.4/255.0.255.255",
        "1:2:3:4:5:6:7:8/ffff:ffff:ffff:fffe:ffff:ffff:ffff:ff0f",
        # `LookupIntern` never answers an internal address
        "fd6b:88c0:8724::1",
        # not a digit `ToIntegral` reads, nor an address
        "1.2.3.0/²",
        # `CSubNet` takes a prefix or a netmask of an IP address alone
        f"{_ONION}/32",
        f"{_I2P}/0",
        "1.2.3.4/" + _ONION,
        # a checksum `SetTor` refuses, and Core's other refusals of it
        "qg6mmjiyjmcrsslvykfwnntlaru7p5svn6y2ymmju6nubxndf4pscryd.onion",
        "pg6mmjiyjmcrsslvykfwnntlaru7p5svn6y2ymmju6nubxndf4pscsad.onion",
        "pg6mmjiyjmcrsslvykfwnntlaru7p5svn6y2ymmju6nubxndf4pscrye.onion",
        "6hzph5hv6337r6p2.onion",
        "mfrggzak.onion",
        "mf*g zak.onion",
        _ONION + "\0",
        # a length `DecodeBase32` refuses before stripping the padding
        _ONION.removesuffix(".onion") + "=.onion",
        # Core's `netbase_dont_resolve_strings_with_embedded_nul_characters`
        "127.0.0.1\0",
        "127.0.0.1\0example.com",
        "1.2.3.0/24\0",
        _ONION + "\0example.com",
        # and of `SetI2P`, and bits `DecodeBase32` has left over
        _I2P.replace("4jna.", "4jnb."),
        "udhdrtrcetjm5sxzskjyr5ztpeszydbh4dpl3pl4utgqqw2v4jn=.b32.i2p",
        "udhdrtrcetjm5sxzskjyr5ztpeszydbh4dpl3pl4utgqqw2v4jna=.b32.i2p",
        "pg6mmjiyjmcrsslvykfwnntlaru7p5svn6y2ymmju6nubxndf4pscsad.b32.i2p",
        "tp*szydbh4dp.b32.i2p",
        # what `getaddrinfo` reads as no address
        "1.2.3.4 ",
    ],
)
def test_a_subnet_that_does_not_parse_is_none(text: str) -> None:
    """Core's `subnet_test`, the invalid subnets."""
    assert lookup_subnet(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.2.3.0/0", "0.0.0.0/0"),
        ("1.2.3.0/32", "1.2.3.0/32"),
        ("1:2:3:4:5:6:7:8/0", "::/0"),
        ("1:2:3:4:5:6:7:8/33", "1:2::/33"),
        ("1:2:3:4:5:6:7:8/128", "1:2:3:4:5:6:7:8/128"),
        ("127.0.0.1", "127.0.0.1/32"),
        ("1:2:3:4:5:6:7:8", "1:2:3:4:5:6:7:8/128"),
        ("1.2.3.4/8", "1.0.0.0/8"),
        ("1.2.3.4/255.255.255.255", "1.2.3.4/32"),
        ("1.2.3.4/255.255.255.254", "1.2.3.4/31"),
        ("1.2.3.4/255.255.255.248", "1.2.3.0/29"),
        ("1.2.3.4/255.255.254.0", "1.2.2.0/23"),
        ("1.2.3.4/255.252.0.0", "1.0.0.0/14"),
        ("1.2.3.4/254.0.0.0", "0.0.0.0/7"),
        ("1.2.3.4/128.0.0.0", "0.0.0.0/1"),
        ("1.2.3.4/0.0.0.0", "0.0.0.0/0"),
        (
            "1:2:3:4:5:6:7:8/ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
            "1:2:3:4:5:6:7:8/128",
        ),
        ("1:2:3:4:5:6:7:8/ffff:0000:0000:0000:0000:0000:0000:0000", "1::/16"),
        ("1:2:3:4:5:6:7:8/0000:0000:0000:0000:0000:0000:0000:0000", "::/0"),
        # an IPv4 netmask mapped into IPv6 is an IPv4 one
        ("1.2.3.4/::ffff:255.255.0.0", "1.2.0.0/16"),
        # `SetLegacyIPv6` reads the Tor v2 prefix as the unspecified address
        ("fd87:d87e:eb43::1/16", "::/16"),
    ],
)
def test_a_subnet_is_written_as_core_writes_it(text: str, expected: str) -> None:
    """Core's `subnet_test`, `ToString` of a subnet `LookupSubNet` parsed."""
    assert str(a_subnet(text)) == expected


def test_the_same_subnet_written_two_ways_is_one_subnet() -> None:
    """`1.2.3.0/24` and `1.2.3.0/255.255.255.0` are one key of the list."""
    assert a_subnet("1.2.3.0/24") == a_subnet("1.2.3.0/255.255.255.0")
    assert a_subnet("1.2.3.0/24") != a_subnet("1.2.4.0/255.255.255.0")


def test_lookup_host_reads_an_address_as_set_legacy_ipv6_does() -> None:
    """Brackets stripped, IPv4 mapped read as IPv4, the internal refused."""
    assert lookup_host("[::1]") == IPv6Address("::1")
    assert lookup_host("::ffff:1.2.3.4") == IPv4Address("1.2.3.4")
    assert lookup_host("fd6b:88c0:8724::1") is None
    assert lookup_host("fd87:d87e:eb43::1") == IPv6Address("::")


@_ATON
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.2.3", "1.2.0.3"),
        ("0x7f.1", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("01.2.3.4", "1.2.3.4"),
    ],
)
def test_lookup_host_reads_what_getaddrinfo_reads(text: str, expected: str) -> None:
    """ISS 1220: `inet_aton`'s forms, as bitcoind v31.1 bans them on macOS."""
    assert lookup_host(text) == IPv4Address(expected)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fe80::1%1", "fe80::1%1/128"),
        ("[fe80::3%1]", "fe80::3%1/128"),
        ("fe80::4%1/64", "fe80::%1/64"),
        ("fe80::5%1/ffff:ffff:ffff:ffff::", "fe80::%1/64"),
        # bitcoind v31.1 keeps the scope of a Tor v2 address it unsets
        ("fd87:d87e:eb43::1%1", "::%1/128"),
    ],
)
def test_a_scope_is_kept_and_written(text: str, expected: str) -> None:
    """ISS 1220: `CSubNet::ToString` writes the scope `sin6_scope_id` gave."""
    assert str(a_subnet(text)) == expected


@_ATON
def test_a_scope_named_by_its_interface_is_written_as_its_index() -> None:
    """ISS 1220: `fe80::2%lo0` is `fe80::2%1/128` to bitcoind v31.1 on macOS."""
    index, name = socket.if_nameindex()[0]
    assert str(a_subnet(f"fe80::2%{name}")) == f"fe80::2%{index}/128"


def test_a_scope_is_no_part_of_the_subnet(tmp_path: Path) -> None:
    """ISS 1220: `CNetAddr::operator==` does not compare the scope.

    What is banned under one scope is banned, and unbanned, under any,
    and the list written reads back with the scope it was written with.
    """
    path = tmp_path / "banlist.json"
    ban_man = BanMan(path, _LOGGER)
    ban_man.ban(a_subnet("fe80::1%1"))
    assert a_subnet("fe80::1%2") == a_subnet("fe80::1%1")
    assert ban_man.is_subnet_banned(a_subnet("fe80::1%2"))
    assert [str(subnet) for subnet, _ in BanMan(path, _LOGGER).banned()] == [
        "fe80::1%1/128"
    ]
    assert ban_man.unban(a_subnet("fe80::1%2"))
    assert not ban_man.banned()


@pytest.mark.parametrize(
    "text",
    [
        _ONION,
        _ONION.upper().removesuffix(".ONION") + ".onion",
        f"[{_ONION}]",
        _I2P,
        _I2P.upper(),
        f"[{_I2P}]",
    ],
)
def test_an_onion_or_i2p_host_is_its_own_subnet(text: str) -> None:
    """ISS 1218: `CSubNet(addr)` of `SetSpecial`'s host, written in lower case.

    `SetTor` asks the suffix in lower case, `SetI2P` in any case, and
    `DecodeBase32` reads the name in either.
    """
    subnet = a_subnet(text)
    assert subnet.prefix == 0
    assert str(subnet) in (_ONION, _I2P)
    assert lookup_subnet(str(subnet)) == subnet


def test_an_upper_case_onion_suffix_is_no_onion() -> None:
    """`SetTor` asks `.onion` exactly."""
    assert lookup_host(_ONION.removesuffix(".onion") + ".ONION") is None


def test_an_onion_subnet_matches_its_own_host_alone() -> None:
    """Core's `subnet_test`, the non-IP subnets."""
    subnet = a_subnet(_ONION)
    onion = lookup_host(_ONION)
    assert onion is not None
    assert subnet.matches(onion)
    other = lookup_host(_OTHER_ONION)
    assert other is not None
    assert not subnet.matches(other)
    assert not subnet.matches(ip("1.2.3.4"))
    assert not a_subnet("0.0.0.0/0").matches(onion)
    assert not a_subnet("::/0").matches(onion)


def test_a_peer_is_matched_on_its_own_network() -> None:
    """An onion or I2P peer by its key, a mapped IPv4 peer by its IPv4."""
    onion = lookup_host(_ONION)
    assert onion is not None
    peer = NetworkAddressV2(0, 0, BIP155Network.TORV3, onion.packed, 8333)
    assert a_subnet(_ONION).matches_peer(peer)
    assert not a_subnet(_I2P).matches_peer(peer)
    assert not a_subnet("::/0").matches_peer(peer)
    i2p = NetworkAddressV2(0, 0, BIP155Network.I2P, b"\x11" * 32, 0)
    assert not a_subnet(_I2P).matches_peer(i2p)
    cjdns = NetworkAddressV2(0, 0, BIP155Network.CJDNS, b"\xfc" + bytes(15), 0)
    assert not a_subnet("::/0").matches_peer(cjdns)
    assert a_subnet("1.2.3.0/24").matches_peer(peer_address("::ffff:1.2.3.4", 1))


def test_a_banned_onion_peer_is_banned() -> None:
    """`IsBanned` of an onion peer, which Core's `CSubNet::Match` answers."""
    onion = lookup_host(_ONION)
    assert onion is not None
    ban_man = BanMan(None, _LOGGER)
    ban_man.ban(a_subnet(_ONION))
    peer = NetworkAddressV2(0, 0, BIP155Network.TORV3, onion.packed, 8333)
    assert ban_man.is_peer_banned(peer)
    other = NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, 8333)
    assert not ban_man.is_peer_banned(other)


def test_subnets_are_listed_in_core_s_order() -> None:
    """IPv4, IPv6, onion, I2P; then by octets, the wider netmask first.

    bitcoind v31.1 lists an I2P ban after its IPv6 ones.
    """
    ban_man = BanMan(None, _LOGGER)
    texts = [_I2P, _ONION, "::1", "1.2.3.4/32", "1.2.3.0/24", "1.2.3.0/25", "0.0.0.0/0"]
    for text in texts:
        ban_man.ban(a_subnet(text))
    assert [str(subnet) for subnet, _ in ban_man.banned()] == [
        "0.0.0.0/0",
        "1.2.3.0/24",
        "1.2.3.0/25",
        "1.2.3.4/32",
        "::1/128",
        _ONION,
        _I2P,
    ]


def test_a_ban_lasts_the_default_time_unless_given_one(a_clock: list[int]) -> None:
    """Core's `Ban`: an offset from now, or an absolute end."""
    ban_man = BanMan(None, _LOGGER, default_ban_time=100)
    ban_man.ban(a_subnet("1.1.1.1"))
    ban_man.ban(a_subnet("2.2.2.2"), 50)
    ban_man.ban(a_subnet("3.3.3.3"), _NOW + 500, absolute=True)
    # an offset of zero or less is the default, and never absolute
    ban_man.ban(a_subnet("4.4.4.4"), -1, absolute=True)
    assert dict(ban_man.banned()) == {
        a_subnet("1.1.1.1"): BanEntry(_NOW, _NOW + 100),
        a_subnet("2.2.2.2"): BanEntry(_NOW, _NOW + 50),
        a_subnet("3.3.3.3"): BanEntry(_NOW, _NOW + 500),
        a_subnet("4.4.4.4"): BanEntry(_NOW, _NOW + 100),
    }
    assert BanMan(None, _LOGGER).default_ban_time == DEFAULT_MISBEHAVING_BANTIME


def test_a_ban_is_only_replaced_by_one_ending_later(a_clock: list[int]) -> None:
    """Core's `Ban` keeps whichever of the two ends later."""
    ban_man = BanMan(None, _LOGGER)
    subnet = a_subnet("1.2.3.4")
    ban_man.ban(subnet, 100)
    a_clock[0] += 10
    ban_man.ban(subnet, 50)
    assert dict(ban_man.banned()) == {subnet: BanEntry(_NOW, _NOW + 100)}
    ban_man.ban(subnet, 200)
    assert dict(ban_man.banned()) == {subnet: BanEntry(_NOW + 10, _NOW + 210)}


def test_a_ban_past_int64_is_not_taken_and_the_list_still_loads(
    tmp_path: Path,
) -> None:
    """`Ban` wraps an end past `int64_t`, as bitcoind v31.1.0 does.

    Measured there: `setban` succeeds for an offset reaching one second or
    more past the maximum, and the ban is not listed, while an end at the
    maximum exactly, relative or absolute, is kept.
    """
    path = tmp_path / "banlist.json"
    ban_man = BanMan(path, _LOGGER)
    ban_man.ban(a_subnet("5.5.5.5"), 3600)
    path.write_text("left alone", encoding="utf-8")
    int64_max = (1 << 63) - 1
    ban_man.ban(a_subnet("11.0.0.1"), int64_max)
    ban_man.ban(a_subnet("11.0.0.2"), int64_max - _NOW + 1)
    # a ban not taken leaves the list unchanged, so unwritten
    assert path.read_text(encoding="utf-8") == "left alone"
    ban_man.ban(a_subnet("11.0.0.3"), int64_max - _NOW)
    ban_man.ban(a_subnet("11.0.0.4"), int64_max, absolute=True)
    expected = {
        a_subnet("5.5.5.5"): BanEntry(_NOW, _NOW + 3600),
        a_subnet("11.0.0.3"): BanEntry(_NOW, int64_max),
        a_subnet("11.0.0.4"): BanEntry(_NOW, int64_max),
    }
    assert dict(ban_man.banned()) == expected
    assert dict(BanMan(path, _LOGGER).banned()) == expected


def test_an_address_is_banned_by_any_subnet_holding_it(a_clock: list[int]) -> None:
    """Core's two `IsBanned`: any match for an address, the key for a subnet."""
    ban_man = BanMan(None, _LOGGER)
    ban_man.ban(a_subnet("1.2.3.0/24"), 100)
    assert ban_man.is_banned(ip("1.2.3.4"))
    assert not ban_man.is_banned(ip("1.2.4.4"))
    assert ban_man.is_subnet_banned(a_subnet("1.2.3.0/24"))
    assert not ban_man.is_subnet_banned(a_subnet("1.2.3.4"))
    assert ban_man.is_peer_banned(peer_address("1.2.3.4", 8333))
    onion = NetworkAddressV2(0, 0, BIP155Network.TORV3, b"\x11" * 32, 8333)
    assert not ban_man.is_peer_banned(onion)
    # the second the ban ends it no longer holds, and is not yet swept
    a_clock[0] += 100
    assert not ban_man.is_banned(ip("1.2.3.4"))
    assert not ban_man.is_subnet_banned(a_subnet("1.2.3.0/24"))
    assert len(ban_man.banned()) == 1
    a_clock[0] += 1
    assert ban_man.banned() == []


def test_unban_and_clear() -> None:
    """Core's `Unban` answers whether there was a ban; `ClearBanned` all."""
    ban_man = BanMan(None, _LOGGER)
    ban_man.ban(a_subnet("1.2.3.4"))
    ban_man.ban(a_subnet("5.6.7.8"))
    assert not ban_man.unban(a_subnet("1.2.3.0/24"))
    assert ban_man.unban(a_subnet("1.2.3.4"))
    assert [str(subnet) for subnet, _ in ban_man.banned()] == ["5.6.7.8/32"]
    ban_man.clear()
    assert ban_man.banned() == []


def read(path: Path) -> Any:
    """Return `path` decoded as JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_list_is_written_in_core_s_format(tmp_path: Path) -> None:
    """`CBanDB::Write`: the warning, then `banned_nets`, one entry a ban."""
    path = tmp_path / "banlist.json"
    ban_man = BanMan(path, _LOGGER)
    # a list with nothing to read is recreated, and written at once
    assert read(path)["banned_nets"] == []
    ban_man.ban(a_subnet("1.2.3.0/24"), 100)
    assert read(path) == {
        "_warning_": f"This file is automatically generated and updated by "
        f"{CLIENT_NAME}. Please do not edit this file while the node is "
        "running, as any changes might be ignored or overwritten.",
        "banned_nets": [
            {
                "version": 1,
                "ban_created": _NOW,
                "banned_until": _NOW + 100,
                "address": "1.2.3.0/24",
            }
        ],
    }
    assert dict(BanMan(path, _LOGGER).banned()) == dict(ban_man.banned())


def test_an_entry_core_would_drop_is_dropped(tmp_path: Path) -> None:
    """`BanMapFromJson` skips an unknown version and an unparsable address."""
    path = tmp_path / "banlist.json"
    kept = {"version": 1, "ban_created": 1, "banned_until": _NOW + 1}
    path.write_text(
        json.dumps(
            {
                "banned_nets": {
                    "a": {**kept, "address": "1.2.3.4"},
                    "b": {**kept, "version": 2, "address": "5.6.7.8"},
                    "c": {**kept, "address": "bloop"},
                }
            }
        ),
        encoding="utf-8",
    )
    assert [str(subnet) for subnet, _ in BanMan(path, _LOGGER).banned()] == [
        "1.2.3.4/32"
    ]


def test_a_key_twice_in_an_entry_is_read_as_univalue_reads_it(
    tmp_path: Path,
) -> None:
    """The first value under a key is read, and an object's values all.

    Measured on bitcoind v31.1.0: `1.2.3.4` alone for the first file,
    both addresses for the second.
    """
    path = tmp_path / "banlist.json"
    kept = '"version": 1, "ban_created": 1, "banned_until": 1800000000'
    path.write_text(
        f'{{"banned_nets": [{{{kept}, "address": "1.2.3.4", "address": "5.6.7.8"}}]}}',
        encoding="utf-8",
    )
    assert [str(subnet) for subnet, _ in BanMan(path, _LOGGER).banned()] == [
        "1.2.3.4/32"
    ]
    path.write_text(
        f'{{"banned_nets": {{"a": {{{kept}, "address": "1.2.3.4"}},'
        f' "a": {{{kept}, "address": "5.6.7.8"}}}}}}',
        encoding="utf-8",
    )
    assert [str(subnet) for subnet, _ in BanMan(path, _LOGGER).banned()] == [
        "1.2.3.4/32",
        "5.6.7.8/32",
    ]


def test_an_expired_entry_is_swept_on_loading(tmp_path: Path) -> None:
    """`LoadBanlist` sweeps, and the constructor's dump writes that back."""
    path = tmp_path / "banlist.json"
    entry = {"version": 1, "ban_created": 1, "banned_until": _NOW - 1}
    path.write_text(
        json.dumps({"banned_nets": [{**entry, "address": "1.2.3.4"}]}),
        encoding="utf-8",
    )
    assert BanMan(path, _LOGGER).banned() == []
    assert read(path)["banned_nets"] == []


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        "[]",
        "{}",
        '{"banned_nets": 1}',
        '{"banned_nets": [1]}',
        '{"banned_nets": [{"version": "1"}]}',
        '{"banned_nets": [{"version": 1.0}]}',
        '{"banned_nets": [{"version": true}]}',
        '{"banned_nets": [{"version": 4294967296}]}',
        '{"banned_nets": [{"version": 1, "address": 1}]}',
        '{"banned_nets": [{"version": 1, "address": "1.2.3.4"}]}',
        # `ReadSettings` refuses a top-level key held twice, whatever it holds
        '{"banned_nets": [%s], "banned_nets": [%s]}' % ((_ENTRY,) * 2),
    ],
)
def test_a_file_core_cannot_read_is_recreated_empty(
    tmp_path: Path, document: str
) -> None:
    """`CBanDB::Read` fails the whole file on any value it cannot read."""
    path = tmp_path / "banlist.json"
    path.write_text(document, encoding="utf-8")
    assert BanMan(path, _LOGGER).banned() == []
    assert read(path)["banned_nets"] == []


def test_a_list_unchanged_is_not_written_again(tmp_path: Path) -> None:
    """`DumpBanlist` writes only a list marked dirty."""
    path = tmp_path / "banlist.json"
    ban_man = BanMan(path, _LOGGER)
    path.write_text("left alone", encoding="utf-8")
    ban_man.dump()
    assert path.read_text(encoding="utf-8") == "left alone"


def test_a_failed_write_is_retried_at_the_next_dump(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`DumpBanlist` marks the list dirty again where the write failed."""
    path = tmp_path / "missing" / "banlist.json"
    with caplog.at_level(logging.ERROR):
        ban_man = BanMan(path, _LOGGER)
    assert caplog.records
    path.parent.mkdir()
    ban_man.dump()
    assert read(path)["banned_nets"] == []
