# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests for `btclib_node.rpc.allow`, Core's `-rpcallowip`."""

import os
import re
from ipaddress import ip_network

import pytest

from btclib_node.rpc.allow import allowed_subnets, client_allowed

_LOOPBACK = (ip_network("127.0.0.0/8"), ip_network("::1/128"))
# `inet_aton`'s forms, which Windows' `getaddrinfo` need not read
_ATON = pytest.mark.skipif(os.name == "nt", reason="the resolver's own forms")


@pytest.mark.parametrize(
    ("value", "subnet"),
    [
        ("1.2.3.4", "1.2.3.4/32"),
        ("1.2.3.4/24", "1.2.3.0/24"),
        ("1.2.3.4/024", "1.2.3.0/24"),
        ("1.2.3.4/255.255.255.0", "1.2.3.0/24"),
        ("1.2.3.4/0", "0.0.0.0/0"),
        ("0.0.0.0/0", "0.0.0.0/0"),
        ("::/0", "::/0"),
        ("2001:db8::1/64", "2001:db8::/64"),
        ("2001:db8::1/ffff:ffff::", "2001:db8::/32"),
        ("[::1]", "::1/128"),
        # what `getaddrinfo` reads as an address, as bitcoind v31.1.0 does
        # on macOS: a mask past 255 is not a prefix length and is read as
        # an address, `255.0.0.0` here. The platform's own resolver decides
        pytest.param("127.1", "127.0.0.1/32", marks=_ATON),
        pytest.param("1.2.3.4/4278190080", "1.0.0.0/8", marks=_ATON),
        # `SetLegacyIPv6`: a mapped address is IPv4, one under the Tor v2
        # prefix is the unspecified address
        ("::ffff:1.2.3.4", "1.2.3.4/32"),
        ("::ffff:1.2.3.4/24", "1.2.3.0/24"),
        ("fd87:d87e:eb43::1", "::/128"),
    ],
)
def test_a_value_names_the_subnet_core_s_lookup_subnet_does(
    value: str, subnet: str
) -> None:
    """ISS 1268: an IP, a network and a netmask, or a network and a prefix."""
    assert allowed_subnets((value,)) == (*_LOOPBACK, ip_network(subnet))


@pytest.mark.parametrize(
    "value",
    [
        # refused by bitcoind v31.1.0, measured
        "bogus",
        "1.2.3.4/33",
        "1.2.3.4/255.0.255.0",
        "1.2.3.4/255.254.255.0",
        "::1/255.255.255.0",
        "localhost",
        "1.2.3.4/+8",
        # read from Core's sources
        "",
        "1.2.3.4/",
        "1.2.3.4/256",
        "1.2.3.4/ 8",
        "::1/129",
        "a/b/1.2.3.4",
        "fd6b:88c0:8724::1",
        "1.2.3.4\0",
    ],
)
def test_a_value_naming_no_subnet_is_refused_in_core_s_words(value: str) -> None:
    """ISS 1268: `InitHTTPAllowList`'s message, naming the value."""
    message = (
        f"Invalid -rpcallowip subnet specification: {value}. Valid values are a "
        "single IP (e.g. 1.2.3.4), a network/netmask (e.g. "
        "1.2.3.4/255.255.255.0), a network/CIDR (e.g. 1.2.3.4/24), all ipv4 "
        "(0.0.0.0/0), or all ipv6 (::/0). RFC4193 is allowed only if "
        "-cjdnsreachable=0."
    )
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        allowed_subnets(("1.2.3.4", value))


@pytest.mark.parametrize(
    ("host", "values", "allowed"),
    [
        ("127.0.0.1", (), True),
        ("127.5.6.7", (), True),
        ("::1", (), True),
        ("::ffff:127.0.0.1", (), True),
        ("10.0.0.1", (), False),
        ("10.0.0.1", ("10.0.0.0/8",), True),
        ("::ffff:10.0.0.1", ("10.0.0.0/8",), True),
        ("10.0.0.1", ("::/0",), False),
        ("2001:db9::1", ("::/0",), True),
        ("fe80::1%lo0", ("fe80::/64",), True),
        # `CNetAddr::IsValid` refuses these whatever the subnet
        ("0.0.0.0", ("0.0.0.0/0",), False),  # noqa: S104 -- a peer, not a bind
        ("255.255.255.255", ("0.0.0.0/0",), False),
        ("2001:db8::1", ("::/0",), False),
        ("fd6b:88c0:8724::1", ("::/0",), False),
        ("", ("0.0.0.0/0",), False),
    ],
)
def test_a_client_is_allowed_as_client_allowed_allows_it(
    host: str, values: tuple[str, ...], *, allowed: bool
) -> None:
    """ISS 1268: loopback always, and what an `-rpcallowip` subnet names."""
    assert client_allowed(host, allowed_subnets(values)) is allowed
