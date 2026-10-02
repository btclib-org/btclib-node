# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Config`'s chain resolution, path arithmetic, ports and feerate floor."""

import os
import re
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path

import pytest
from bitcoin_core_rpc import rpc_port_from_chain
from btclib.fee import FeeRate

from btclib_node.chains import Main, RegTest, SigNet, TestNet, TestNet4
from btclib_node.config import (
    DEFAULT_MAX_PEER_CONNECTIONS,
    DEFAULT_MIN_RELAY_FEERATE,
    BindAddress,
    Config,
    get_path_arg,
    listen_port,
    lookup_host_port,
    lookup_service,
    parse_bind,
    parse_whitebind,
    service_text,
    split_host_port,
)
from btclib_node.p2p.permissions import permission_names
from btclib_node.rpc.auth import COOKIE_FILE, RpcAuthEntry, password_hmac
from tests import RPCAUTH


def test_chain_selection() -> None:
    """`_resolve_chain` accepts a `Chain` or its name; else raises."""
    assert Config(chain="mainnet") == Config(chain=Main())
    assert Config(chain="testnet") == Config(chain=TestNet())
    assert Config(chain="signet") == Config(chain=SigNet())
    assert Config(chain="regtest") == Config(chain=RegTest())
    assert Config(chain="testnet4") == Config(chain=TestNet4())
    with pytest.raises(TypeError, match="chain must be a Chain or str"):
        Config(chain=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown chain"):
        Config(chain="wrongchain")


def test_data_dir() -> None:
    """`data_dir` becomes absolute, with the chain's own name appended."""
    config = Config(chain="regtest", data_dir="dir")
    # what it is, and not `!= "dir"`: a Path is never equal to a str,
    # so that comparison held whatever __init__ had done to it -- an
    # assertion no change to the two things it is about could fail
    assert config.data_dir == Path("dir").absolute() / "regtest"


def test_blocks_dir_defaults_to_none() -> None:
    """Not given: `Config` leaves it for `BlockDB`'s own default."""
    assert Config(chain="regtest").blocks_dir is None


def test_blocks_dir_absolute_and_chain_suffixed(tmp_path: Path) -> None:
    """Given, and it exists: absolute, chain-suffixed the same as `data_dir`."""
    config = Config(chain="regtest", blocks_dir=str(tmp_path))
    assert config.blocks_dir == tmp_path.absolute() / "regtest"


def test_blocks_dir_missing_raises(tmp_path: Path) -> None:
    """A `blocks_dir` that does not exist is fatal, not silently created."""
    missing = tmp_path / "nope"
    with pytest.raises(ValueError, match="does not exist"):
        Config(chain="regtest", blocks_dir=str(missing))


def test_port() -> None:
    """A given `p2p_port` or `rpc_port` is stored back unchanged."""
    assert Config(chain="regtest", p2p_port=1).p2p_port == 1
    assert Config(chain="regtest", rpc_port=1).rpc_port == 1


@pytest.mark.parametrize(
    ("chain_name", "core_chain_name"),
    [
        ("mainnet", "main"),
        ("testnet", "test"),
        ("signet", "signet"),
        ("regtest", "regtest"),
        ("testnet4", "testnet4"),
    ],
)
def test_default_rpc_port_is_cores_own(chain_name: str, core_chain_name: str) -> None:
    """The default `rpc_port` is Core's own for the chain, not `p2p_port + 1`.

    `rpc_port_from_chain` (`bitcoin_core_rpc`) is Core's own table, read
    independently of this node's `Config` -- a client built the way
    `bitcoin-cli` is, against Core's own default rather than against
    `node.rpc_port` read back from this node (btclib-org/btclib-node#605).
    """
    assert Config(chain=chain_name).rpc_port == rpc_port_from_chain(core_chain_name)


def test_min_relay_feerate_defaults_to_cores_own_floor() -> None:
    """`min_relay_feerate` defaults to Core's own floor, 100 sat/kvB."""
    # at bitcoin/bitcoin@58a7869f86's DEFAULT_MIN_RELAY_TX_FEE,
    # src/policy/policy.h
    assert Config(chain="regtest").min_relay_feerate == DEFAULT_MIN_RELAY_FEERATE
    assert DEFAULT_MIN_RELAY_FEERATE.sats_per_kvbyte == 100


def test_min_relay_feerate_is_configurable() -> None:
    """A `min_relay_feerate` passed in overrides the default floor."""
    rate = FeeRate(sats_per_kvbyte=1000)
    assert Config(chain="regtest", min_relay_feerate=rate).min_relay_feerate == rate


def test_relay_policy_options_default_to_cores_own() -> None:
    """Core's defaults, `src/policy/policy.h` at bitcoin/bitcoin@9be056a8a7."""
    config = Config(chain="regtest")
    assert config.incremental_relay_feerate.sats_per_kvbyte == 100
    assert config.dust_relay_feerate.sats_per_kvbyte == 3000
    assert config.permit_bare_multisig is True
    assert config.max_datacarrier_bytes == 100_000
    assert config.require_standard is True


def test_rpc_host_defaults_to_localhost_not_every_interface() -> None:
    """`rpc_host` defaults to loopback; an explicit host still wins."""
    # #27: the rpc listener is a control plane, not a peer-to-peer
    # one, so its default is not the P2P listener's
    assert Config(chain="regtest").rpc_host is None
    assert (
        Config(chain="regtest", rpc_host="0.0.0.0").rpc_host  # noqa: S104
        == "0.0.0.0"  # noqa: S104
    )


def test_pruned_true_builds_a_config() -> None:
    """`pruned=True` constructs rather than refusing, closing #601."""
    assert Config(chain="regtest", pruned=True).pruned is True


def test_pruned_false_still_constructs() -> None:
    """`pruned=False`, the default, builds a `Config` as before."""
    assert Config(chain="regtest").pruned is False
    assert Config(chain="regtest", pruned=False).pruned is False


def test_prune_target_mib_defaults_to_none() -> None:
    """Unset, `prune_target_mib` is `None`, matching every earlier caller."""
    assert Config(chain="regtest").prune_target_mib is None
    assert Config(chain="regtest", pruned=True).prune_target_mib is None


def test_prune_target_mib_reaches_config_unchanged() -> None:
    """An explicit `prune_target_mib` constructs rather than being collapsed."""
    config = Config(chain="regtest", pruned=True, prune_target_mib=700)
    assert config.prune_target_mib == 700


def test_a_disallowed_port_is_none_rather_than_some_other_number() -> None:
    """`allow_p2p=False`/`allow_rpc=False` force the matching port to `None`.

    A port both given explicitly and disallowed is still forced to
    `None`, not just a defaulted one -- `Node` reads `None` as "do not
    start this listener", so anything else would have it bind a port
    the caller just said to leave closed.
    """
    # `is None` and not `!= 1`: None is what Node reads as "do not
    # start this listener", and the declaration in Config says so now.
    # Any other number would satisfy `!= 1` and be a port to bind.
    assert Config(chain="regtest", p2p_port=1, allow_p2p=False).p2p_port is None
    assert Config(chain="regtest", rpc_port=1, allow_rpc=False).rpc_port is None
    # the chain's own port, which is what an allowed one falls back to,
    # is not a value a disallowed one can take either
    assert Config(chain="regtest", allow_p2p=False).p2p_port is None
    assert Config(chain="regtest", allow_rpc=False).rpc_port is None


def test_connect_and_addnode_default_to_empty() -> None:
    """Neither field is given: both resolve to an empty tuple."""
    config = Config(chain="regtest")
    assert config.connect == ()
    assert config.addnode == ()
    assert config.connect_given is False


def test_connect_zero_dials_nobody_but_still_counts_as_given() -> None:
    """Core's own `-connect=0`: an empty dial list, not a raised error.

    `ip_address("0")` is not a valid literal, so `_resolve_peers` never
    sees it -- `Config` special-cases the one-element `["0"]` list the
    same way `CConnman`'s own options builder does
    (`connect.size() != 1 || connect[0] != "0"`, `src/init.cpp:2333`,
    at bitcoin/bitcoin@ca7162cde5) before resolving anything.
    `connect_given` stays `True`: this is still the `-connect` arm,
    dialling nobody rather than never having been asked to.
    """
    config = Config(chain="regtest", connect=["0"])
    assert config.connect == ()
    assert config.connect_given is True
    assert config.connect_args == ()


def test_listen_defaults_to_true() -> None:
    """Core's own `DEFAULT_LISTEN`, unless a caller says otherwise."""
    assert Config(chain="regtest").listen is True


def test_listen_false_is_taken_as_given() -> None:
    """`listen=False` is stored as given, not resolved by `Config` itself.

    The `-connect`-implies-`-listen=0` default is `cli.py`'s own
    `build_config`, argued against `connect_given` above one layer up
    from here -- `Config` only ever stores what it is given.
    """
    assert Config(chain="regtest", connect=["127.0.0.1"], listen=False).listen is False
    assert Config(chain="regtest", connect=["127.0.0.1"]).listen is True


def test_discover_defaults_to_listen() -> None:
    """ISS 1330: `discover=None` follows `listen`, Core's own soft-set."""
    assert Config(chain="regtest", listen=True).discover is True
    assert Config(chain="regtest", listen=False).discover is False


def test_discover_explicit_wins_over_listen() -> None:
    """An explicit `-discover` always wins, even against `-listen=0`."""
    assert Config(chain="regtest", listen=False, discover=True).discover is True
    assert Config(chain="regtest", listen=True, discover=False).discover is False


def test_externalip_turns_discover_off_unless_discover_is_given() -> None:
    """ISS 1445: `InitParameterInteraction` soft-sets `-discover=0`."""
    assert Config(chain="regtest", externalip=["8.8.8.8"]).discover is False
    wins = Config(chain="regtest", externalip=["8.8.8.8"], discover=True)
    assert wins.discover is True
    assert Config(chain="regtest").externalip == ()


def test_bind_beside_listen_0_is_refused_between_the_dnsseed_and_connections() -> None:
    """ISS 1257: the refusal comes after `-forcednsseed`'s own."""
    message = "Cannot set -bind or -whitebind together with -listen=0"
    with pytest.raises(ValueError, match=message):
        Config(chain="regtest", bind=["127.0.0.1"], listen=False)
    assert Config(chain="regtest", bind=["127.0.0.1"]).bind == ("127.0.0.1",)
    with pytest.raises(ValueError, match="-forcednsseed"):
        Config(
            chain="regtest",
            bind=["127.0.0.1"],
            listen=False,
            dnsseed=False,
            forcednsseed=True,
        )
    with pytest.raises(ValueError, match="-bind or -whitebind"):
        Config(chain="regtest", bind=["127.0.0.1"], listen=False, max_connections=-1)


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("127.0.0.1", BindAddress(IPv4Address("127.0.0.1"), 8333, onion=False)),
        ("127.0.0.1:99", BindAddress(IPv4Address("127.0.0.1"), 99, onion=False)),
        ("[::1]", BindAddress(IPv6Address("::1"), 8333, onion=False)),
        ("[::1]:99", BindAddress(IPv6Address("::1"), 99, onion=False)),
        ("::1", BindAddress(IPv6Address("::1"), 8333, onion=False)),
        ("127.0.0.1=onion", BindAddress(IPv4Address("127.0.0.1"), 8334, onion=True)),
        ("127.0.0.1:99=onion", BindAddress(IPv4Address("127.0.0.1"), 99, onion=True)),
    ],
)
def test_parse_bind_reads_an_address_port_and_tag(
    arg: str, expected: BindAddress
) -> None:
    """ISS 1257: `-port` is the default; an `=onion` one is that plus one."""
    assert parse_bind(arg, 8333) == expected


@pytest.mark.parametrize(
    "arg", ["", "localhost", "127.0.0.1=", "127.0.0.1=tor", "=onion", "127.0.0.1:x"]
)
def test_parse_bind_refuses_what_lookup_does_not_find(arg: str) -> None:
    """ISS 1257: `Lookup` without DNS; any tag but `onion` is no address."""
    message = f"Cannot resolve -bind address: '{arg}'"
    with pytest.raises(ValueError, match=f"^{message}$"):
        parse_bind(arg, 8333)


def test_lookup_service_reads_an_address_and_a_port() -> None:
    """ISS 1445: `Lookup`, with its port from the spec or the default."""
    assert lookup_service("1.2.3.4", 8333) == (IPv4Address("1.2.3.4"), 8333)
    assert lookup_service("[::1]:7", 8333) == (IPv6Address("::1"), 7)
    assert lookup_service("localhost", 8333) is None


def test_service_text_brackets_an_ipv6_host() -> None:
    """`CService::ToStringAddrPort`."""
    assert service_text(IPv4Address("1.2.3.4"), 7) == "1.2.3.4:7"
    assert service_text(IPv6Address("::1"), 7) == "[::1]:7"


def test_peerblockfilters_defaults_to_false() -> None:
    """Core's own `DEFAULT_PEERBLOCKFILTERS`."""
    assert Config(chain="regtest").peerblockfilters is False


def test_connect_resolves_to_the_chains_own_default_port() -> None:
    """A spec naming no port falls back to the chain's own P2P port."""
    config = Config(chain="regtest", connect=["127.0.0.1"])
    assert config.connect == (("127.0.0.1", RegTest().port),)


def test_connect_explicit_port_overrides_the_default() -> None:
    """A spec naming a port keeps it rather than the chain's own."""
    config = Config(chain="regtest", connect=["127.0.0.1:9999"])
    assert config.connect == (("127.0.0.1", 9999),)


def test_addnode_is_the_same_shape_as_connect() -> None:
    """`addnode` resolves the same way `connect` does, independently."""
    config = Config(chain="regtest", addnode=["10.0.0.1:1", "10.0.0.2"])
    assert config.addnode == (("10.0.0.1", 1), ("10.0.0.2", RegTest().port))
    assert config.connect == ()


def test_addnode_keeps_its_values_as_given() -> None:
    """ISS 1224: `AddedNodesContain` compares the values as given."""
    config = Config(chain="regtest", addnode=["10.0.0.1:1", "10.0.0.2"])
    assert config.addnode_args == ("10.0.0.1:1", "10.0.0.2")


def test_connect_keeps_its_values_as_given() -> None:
    """ISS 1493: `m_addr_name` is `pszDest` verbatim, a port kept when given.

    `connect` itself (above) already splits and defaults the port for
    dialling; `connect_args` is the same raw strings `addnode_args`
    already kept, one per spec, port included where the spec names one
    and omitted where it does not -- `async_connect_host`'s own `dest`.
    """
    config = Config(chain="regtest", connect=["10.0.0.1:1", "10.0.0.2"])
    assert config.connect_args == ("10.0.0.1:1", "10.0.0.2")


def test_seednode_keeps_its_values_as_given() -> None:
    """ISS 1493: `-seednode`'s own raw spec, mirroring `connect_args`."""
    config = Config(chain="regtest", seednode=["10.0.0.1:1", "10.0.0.2"])
    assert config.seednode_args == ("10.0.0.1:1", "10.0.0.2")


def test_connect_and_addnode_both_take_several_entries() -> None:
    """Every spec given is resolved, in order, not only the last one."""
    config = Config(
        chain="regtest",
        connect=["10.0.0.1", "10.0.0.2"],
        addnode=["10.0.0.3"],
    )
    assert len(config.connect) == 2
    assert config.addnode == (("10.0.0.3", RegTest().port),)


def test_seednode_is_the_same_shape_as_connect() -> None:
    """`seednode` resolves the same way `connect`/`addnode` do."""
    config = Config(chain="regtest", seednode=["10.0.0.1:1", "10.0.0.2"])
    assert config.seednode == (("10.0.0.1", 1), ("10.0.0.2", RegTest().port))
    assert config.connect == ()
    assert config.addnode == ()


def test_seednode_defaults_to_empty() -> None:
    """Not given: an empty tuple, the same default `connect`/`addnode` take."""
    assert Config(chain="regtest").seednode == ()


def test_seednode_takes_a_hostname() -> None:
    """`_split_peers` splits a hostname the same as a literal IP (ISS 1264).

    Core resolves `-seednode`'s own value at dial time
    (`CConnman::ThreadOpenConnections`, `src/net.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag); this node does the same,
    on `P2pManager`'s own loop, so `Config` itself refuses nothing here.
    """
    config = Config(chain="regtest", seednode=["example.com"])
    assert config.seednode == (("example.com", RegTest().port),)


def test_fixed_seeds_defaults_to_true() -> None:
    """Core's own `DEFAULT_FIXEDSEEDS`, unless a caller says otherwise."""
    assert Config(chain="regtest").fixed_seeds is True


def test_fixed_seeds_false_is_taken_as_given() -> None:
    """`fixed_seeds=False` is stored as given, `-fixedseeds=0`'s reader."""
    assert Config(chain="regtest", fixed_seeds=False).fixed_seeds is False


def test_dnsseed_explicit_value_is_taken_as_given() -> None:
    """An explicit `dnsseed` is stored, `-connect`'s own soft-set aside.

    `cli.py`'s own `-dnsseed`/`-nodnsseed` reader is what actually wins
    over `-connect`'s soft-set (ISS 1324's own `int64_t` reason); this
    is `Config`'s own fallback, exercised by a direct caller that passes
    neither.
    """
    config = Config(chain="regtest", connect=["10.0.0.1"], dnsseed=True)
    assert config.dnsseed is True


def test_connect_takes_a_hostname() -> None:
    """A spec whose host is not an IP literal is split, not refused (ISS 1264).

    `p2p_manager.connect_host(host, port)` -- the route `Node.run`
    dials `connect`/`addnode` through -- resolves the host itself, on
    `P2pManager`'s own loop, rather than taking an already-parsed IP the
    way `peer_address` needs, so a hostname here is not this function's
    to refuse.
    """
    config = Config(chain="regtest", connect=["example.com"])
    assert config.connect == (("example.com", RegTest().port),)


def test_split_host_port_bare_host_takes_the_default_port() -> None:
    """No colon at all: the default port, host unchanged."""
    assert split_host_port("127.0.0.1", 8333) == ("127.0.0.1", 8333)


def test_split_host_port_reads_an_explicit_port() -> None:
    """A colon followed by digits: that port, not the default."""
    assert split_host_port("127.0.0.1:9000", 8333) == ("127.0.0.1", 9000)


def test_split_host_port_reads_a_bracketed_ipv6_address_and_port() -> None:
    """`[::1]:9000` splits on the colon after the closing bracket."""
    assert split_host_port("[::1]:9000", 8333) == ("::1", 9000)


def test_split_host_port_an_unbracketed_ipv6_address_has_no_port_of_its_own() -> None:
    """An IPv6 literal's own colons need brackets to be a port separator."""
    assert split_host_port("::1", 8333) == ("::1", 8333)


def test_split_host_port_rejects_a_non_numeric_port() -> None:
    """A port that does not parse as an integer raises."""
    with pytest.raises(ValueError, match="invalid port"):
        split_host_port("127.0.0.1:notaport", 8333)


def test_split_host_port_rejects_port_zero() -> None:
    """Port `0` is not a port to bind or dial, and is refused."""
    with pytest.raises(ValueError, match="invalid port"):
        split_host_port("127.0.0.1:0", 8333)


def test_split_host_port_rejects_a_port_past_the_ceiling() -> None:
    """A port above 65535 does not fit a `uint16_t`, and is refused."""
    with pytest.raises(ValueError, match="invalid port"):
        split_host_port("127.0.0.1:70000", 8333)


@pytest.mark.parametrize(
    "port",
    ["+80", " 80", "80 ", "8_0", "\u0668\u0660", "\u00b2", "99999999999999999999"],
)
def test_split_host_port_reads_ascii_digits_alone(port: str) -> None:
    """`ToIntegral<uint16_t>`: what `int` reads beyond ASCII digits is refused.

    Each refused by `bitcoind` v31.1.0 as `-rpcbind=127.0.0.1:<port>`, the
    last two read from `std::from_chars` rather than measured.
    """
    with pytest.raises(ValueError, match="invalid port"):
        split_host_port(f"127.0.0.1:{port}", 8333)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("127.0.0.1:+80", ("127.0.0.1:+80", 8333)),
        ("127.0.0.1:0x50", ("127.0.0.1:0x50", 8333)),
        ("127.0.0.1: 80", ("127.0.0.1: 80", 8333)),
        ("127.0.0.1:", ("127.0.0.1:", 8333)),
        ("127.0.0.1:70000", ("127.0.0.1:70000", 8333)),
        pytest.param("127.0.0.1:" + "0" * 5000 + "1", ("127.0.0.1", 1), id="zeros"),
        pytest.param(
            "127.0.0.1:" + "9" * 5000, ("127.0.0.1:" + "9" * 5000, 8333), id="nines"
        ),
        ("[::1]:+80", ("[::1]:+80", 8333)),
        ("[::1]:0", ("::1", 0)),
        ("127.0.0.1:0", ("127.0.0.1", 0)),
        ("127.0.0.1:9000", ("127.0.0.1", 9000)),
        ("[::1]", ("::1", 8333)),
    ],
)
def test_lookup_host_port_reads_a_bad_port_as_part_of_the_name(
    spec: str, expected: tuple[str, int]
) -> None:
    """ISS 1292: `Lookup` ignores `SplitHostPort`'s answer, keeping its host.

    A port that is no `uint16_t` leaves the whole spec as the host and
    the default port; port 0 is split off, and is not valid.
    """
    assert lookup_host_port(spec, 8333) == expected


def test_split_host_port_reads_leading_zeros() -> None:
    """`bitcoind` v31.1.0 binds `-rpcbind=127.0.0.1:031301` at port 31301."""
    assert split_host_port("127.0.0.1:031301", 8333) == ("127.0.0.1", 31301)
    assert split_host_port("127.0.0.1:0000000000000000045000", 1) == (
        "127.0.0.1",
        45000,
    )


def test_max_connections_defaults_to_core_s_own() -> None:
    """Core's own `DEFAULT_MAX_PEER_CONNECTIONS`, at the pinned release."""
    assert DEFAULT_MAX_PEER_CONNECTIONS == 125
    assert Config(chain="regtest").max_connections == DEFAULT_MAX_PEER_CONNECTIONS


def test_max_connections_given_is_stored_back() -> None:
    """Zero included: Core accepts `-maxconnections=0` too."""
    assert Config(chain="regtest", max_connections=7).max_connections == 7
    assert Config(chain="regtest", max_connections=0).max_connections == 0


def test_max_connections_negative_raises() -> None:
    """Core's own `InitError`, not a limit nothing could ever satisfy."""
    with pytest.raises(ValueError, match="greater or equal than zero"):
        Config(chain="regtest", max_connections=-1)


def test_forcednsseed_defaults_to_false() -> None:
    """Core's own `DEFAULT_FORCEDNSSEED`, at bitcoin/bitcoin@9be056a8a7."""
    assert Config(chain="regtest").forcednsseed is False


def test_forcednsseed_true_alongside_dnsseed_is_stored_back() -> None:
    """`-forcednsseed` alongside a `-dnsseed` that is on: no refusal."""
    config = Config(chain="regtest", dnsseed=True, forcednsseed=True)
    assert config.forcednsseed is True
    assert config.dnsseed is True


def test_forcednsseed_true_alongside_dnsseed_false_raises() -> None:
    """ISS 1265: Core's own wording (`AppInitParameterInteraction`)."""
    with pytest.raises(
        ValueError,
        match=r"^Cannot set -forcednsseed to true when setting -dnsseed to false\.$",
    ):
        Config(chain="regtest", dnsseed=False, forcednsseed=True)


def test_forcednsseed_true_alongside_the_dnsseed_soft_set_off_raises() -> None:
    """The refusal reaches the soft-set too, `dnsseed` left at `None`.

    `-connect` alone turns the soft-set off (`Config.dnsseed`'s own
    docstring), with no explicit `-dnsseed` needed to trigger it.
    """
    with pytest.raises(
        ValueError,
        match=r"^Cannot set -forcednsseed to true when setting -dnsseed to false\.$",
    ):
        Config(chain="regtest", connect=["1.2.3.4"], forcednsseed=True)


def test_an_explicit_dnsseed_true_wins_over_connects_soft_set_off() -> None:
    """`-dnsseed=1 -forcednsseed=1 -connect=x`: no refusal, both on.

    `InitParameterInteraction`'s own `SoftSetBoolArg` (`src/init.cpp`,
    at bitcoin/bitcoin@9be056a8a7) never overwrites an arg already set,
    so `-connect`'s soft-set-off of `-dnsseed` never reaches an explicit
    `-dnsseed=1` -- `AppInitParameterInteraction`'s own forcednsseed
    check (same sha) then reads that explicit `True` back, past
    btclib-org/btclib-node#1192's own `-dnsseed` soft-set order.
    """
    config = Config(
        chain="regtest", dnsseed=True, forcednsseed=True, connect=["1.2.3.4"]
    )
    assert config.dnsseed is True
    assert config.forcednsseed is True


def test_rpcauth_is_parsed_into_rpc_auth() -> None:
    """Each `-rpcauth` value becomes one `RpcAuthEntry`, none by default."""
    assert Config(chain="regtest").rpc_auth == ()
    assert Config(chain="regtest", rpcauth=[RPCAUTH]).rpc_auth == (
        RpcAuthEntry.parse(RPCAUTH),
    )


def test_a_malformed_rpcauth_is_left_for_the_rpc_listener() -> None:
    """Core refuses one once bound, so `Config` keeps the values before it."""
    assert not Config(chain="regtest", rpcauth=[RPCAUTH]).rpc_auth_invalid
    config = Config(chain="regtest", rpcauth=[RPCAUTH, "pytest", RPCAUTH])
    assert config.rpc_auth_invalid
    assert config.rpc_auth == (RpcAuthEntry.parse(RPCAUTH),)
    assert not Config(
        chain="regtest", rpcauth=["pytest"], allow_rpc=False
    ).rpc_auth_invalid


def test_rpcpassword_is_kept_hashed_and_empty_is_unset() -> None:
    """Only the salted HMAC is kept, and `""` is no password, as in Core."""
    assert Config(chain="regtest").rpc_password_entry is None
    assert Config(chain="regtest", rpcpassword="").rpc_password_entry is None
    config = Config(chain="regtest", rpcuser="alice", rpcpassword="s3cr3t")
    entry = config.rpc_password_entry
    assert entry is not None
    assert entry.user == b"alice"
    assert entry.hmac == password_hmac(entry.salt, b"s3cr3t")
    assert "s3cr3t" not in repr(config)


def test_rpccookiefile_resolves_against_the_chain_s_data_dir(tmp_path: Path) -> None:
    """`AbsPathForConfigVal`: relative under `data_dir`, absolute as given."""
    config = Config(chain="regtest", data_dir=tmp_path)
    assert config.rpc_cookie_file == config.data_dir / COOKIE_FILE
    for value in ("", COOKIE_FILE):
        config = Config(chain="regtest", data_dir=tmp_path, rpccookiefile=value)
        assert config.rpc_cookie_file == config.data_dir / COOKIE_FILE
    config = Config(chain="regtest", data_dir=tmp_path, rpccookiefile="sub/c")
    assert config.rpc_cookie_file == config.data_dir / "sub" / "c"
    config = Config(chain="regtest", data_dir=tmp_path, rpccookiefile=tmp_path / "c")
    assert config.rpc_cookie_file == tmp_path / "c"


def test_no_rpccookiefile_is_no_cookie() -> None:
    """`-norpccookiefile`, which `None` stands for here."""
    config = Config(chain="regtest", rpccookiefile=None)
    assert config.rpc_cookie_file is None
    assert config.rpc_cookie_tmp is None


@pytest.mark.parametrize(
    ("value", "tmp"),
    [
        ("", COOKIE_FILE + ".tmp"),
        ("sub/", "sub.tmp"),
        ("..", "...tmp"),
        (".", "..tmp"),
        ("a/..", "..tmp"),
    ],
)
def test_the_cookie_s_tmp_is_core_s_get_auth_cookie_file_true(
    tmp_path: Path, value: str, tmp: str
) -> None:
    """`.tmp` appended to the normalised value, then resolved.

    `bitcoind` v31.1.0 writes `-rpccookiefile=.` and `=a/..` to
    `<chain dir>/..tmp`, inside the chain directory it then cannot
    rename it over.
    """
    config = Config(chain="regtest", data_dir=tmp_path, rpccookiefile=value)
    assert config.rpc_cookie_tmp == config.data_dir / tmp


@pytest.mark.skipif(os.name == "nt", reason="`/` names no drive, so is relative")
def test_the_tmp_of_an_absolute_rpccookiefile_is_beside_it(tmp_path: Path) -> None:
    """`/` is `/.tmp`, and the chain directory's own path its sibling.

    As `bitcoind` v31.1.0 names both, in the warning it logs before
    refusing to start.
    """
    config = Config(chain="regtest", data_dir=tmp_path, rpccookiefile="/")
    assert config.rpc_cookie_file == Path("/")
    assert config.rpc_cookie_tmp == Path("/.tmp")
    chain_dir = tmp_path / "regtest"
    config = Config(chain="regtest", data_dir=tmp_path, rpccookiefile=chain_dir)
    assert config.rpc_cookie_tmp == tmp_path / "regtest.tmp"


def test_rpccookieperms_is_parsed_unless_rpcpassword_is_set() -> None:
    """Core reads it only on the way to a cookie, refusing a bad one there."""
    assert Config(chain="regtest").rpc_cookie_perms is None
    assert Config(chain="regtest", rpccookieperms="group").rpc_cookie_perms == 0o640
    assert (
        Config(chain="regtest", rpccookieperms="group").rpc_cookie_perms_error is None
    )
    config = Config(chain="regtest", rpccookieperms="bogus")
    assert config.rpc_cookie_perms is None
    assert config.rpc_cookie_perms_error == (
        "Invalid -rpccookieperms=bogus; must be one of 'owner', 'group', or 'all'."
    )
    config = Config(chain="regtest", rpcpassword="pw", rpccookieperms="bogus")
    assert config.rpc_cookie_perms is None
    assert config.rpc_cookie_perms_error is None


def test_rpcwhitelistdefault_defaults_to_whether_a_whitelist_is_set() -> None:
    """Core's `GetBoolArg("-rpcwhitelistdefault", !GetArgs(...).empty())`."""
    assert Config(chain="regtest").rpc_whitelist == {}
    assert not Config(chain="regtest").rpc_whitelist_default
    config = Config(chain="regtest", rpcwhitelist=["alice:a,b", "alice:b"])
    assert config.rpc_whitelist == {b"alice": frozenset({"b"})}
    assert config.rpc_whitelist_default
    config = Config(
        chain="regtest", rpcwhitelist=["alice:a"], rpcwhitelistdefault=False
    )
    assert not config.rpc_whitelist_default
    assert Config(chain="regtest", rpcwhitelistdefault=True).rpc_whitelist_default


@pytest.mark.parametrize(
    ("value", "name"),
    [("missing/../mycookie", "mycookie"), ("sub/", "sub"), ("./a//b/.", "a/b")],
)
def test_rpccookiefile_is_normalised_as_core_s_get_path_arg(
    tmp_path: Path, value: str, name: str
) -> None:
    """Lexical, as `lexically_normal`: `bitcoind` v31.1.0 writes `mycookie`.

    Measured there with `-rpccookiefile=nonexist/../mycookie` and
    `-rpccookiefile=sub/`, the second writing a file named `sub`.
    """
    config = Config(chain="regtest", data_dir=tmp_path, rpccookiefile=value)
    assert config.rpc_cookie_file == config.data_dir / name


@pytest.mark.skipif(os.name == "nt", reason="`//` starts a UNC path on Windows")
def test_a_leading_double_slash_is_collapsed_as_lexically_normal_does(
    tmp_path: Path,
) -> None:
    """`GetPathArg` names `//<X>/c` as `/<X>/c`, where `normpath` keeps `//`.

    Measured on `bitcoind` v31.1.0 with `-conf` under `-datadir=//<X>/d`,
    named "/<X>/d/missing.conf"; three slashes are one to both.
    """
    assert get_path_arg(f"/{tmp_path}/c") == f"{tmp_path}/c"
    assert get_path_arg(f"//{tmp_path}/c") == f"{tmp_path}/c"
    config = Config(chain="regtest", data_dir=tmp_path, rpccookiefile=f"/{tmp_path}/c")
    assert str(config.rpc_cookie_file) == f"{tmp_path}/c"


@pytest.mark.parametrize(
    ("arg", "host", "port", "names"),
    [
        ("127.0.0.1:99", "127.0.0.1", 99, []),
        ("noban@127.0.0.1:99", "127.0.0.1", 99, ["noban", "download"]),
        ("relay,in@[::1]:7", "::1", 7, ["relay"]),
        ("@127.0.0.1:99", "127.0.0.1", 99, []),
    ],
)
def test_parse_whitebind_reads_permissions_an_address_and_a_port(
    arg: str, host: str, port: int, names: list[str]
) -> None:
    """ISS 1625: `NetWhitebindPermissions::TryParse`."""
    parsed = parse_whitebind(arg)
    assert (str(parsed.host), parsed.port) == (host, port)
    assert permission_names(parsed.flags) == names


@pytest.mark.parametrize(
    ("arg", "message"),
    [
        ("127.0.0.1", "Need to specify a port with -whitebind: '127.0.0.1'"),
        ("noban@[::1]", "Need to specify a port with -whitebind: '[::1]'"),
        ("localhost:7", "Cannot resolve -whitebind address: 'localhost:7'"),
        ("999.1.1.1:5", "Cannot resolve -whitebind address: '999.1.1.1:5'"),
        (
            "out@127.0.0.1:7",
            'whitebind may only be used for incoming connections ("out" was passed)',
        ),
        ("bogus@127.0.0.1:7", "Invalid P2P permission: 'bogus'"),
        ("in@127.0.0.1:7", "Only direction was set, no permissions: 'in@127.0.0.1:7'"),
    ],
)
def test_parse_whitebind_refuses_in_core_s_words(arg: str, message: str) -> None:
    """ISS 1625: the texts are those of `bitcoind` v31.1.0."""
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        parse_whitebind(arg)


def test_listen_port_takes_the_first_whitebind_that_grants_no_noban() -> None:
    """ISS 1625: `GetListenPort` skips a `noban` one and what is refused."""
    whitebind = [
        "bogus@1.2.3.4:5",
        "noban@1.2.3.4:6",
        "download@1.2.3.4:7",
        "1.2.3.4:8",
    ]
    assert listen_port([], 100, whitebind) == 7
    assert listen_port(["1.2.3.4:9"], 100, whitebind) == 9
    assert listen_port([], 100, whitebind[:2]) == 100


def test_a_value_naming_no_permission_is_a_listen_port_though_it_is_noban() -> None:
    """ISS 1625: the defaults are added after `GetListenPort` looks."""
    assert listen_port([], 100, ["1.2.3.4:8"]) == 8


def test_config_refuses_whitebind_beside_listen_0() -> None:
    """ISS 1625: Core's one refusal covers `-bind` and `-whitebind`."""
    message = "Cannot set -bind or -whitebind together with -listen=0"
    with pytest.raises(ValueError, match=message):
        Config(chain="regtest", whitebind=["127.0.0.1:9"], listen=False)
    config = Config(chain="regtest", whitebind=["127.0.0.1:9"])
    assert config.whitebind == ("127.0.0.1:9",)
    assert Config(chain="regtest").whitebind == ()
