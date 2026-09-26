# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Config`'s chain resolution, path arithmetic, ports and feerate floor."""

import os
from pathlib import Path
from typing import Any

import pytest
from bitcoin_core_rpc import rpc_port_from_chain
from btclib.fee import FeeRate

from btclib_node.chains import Main, RegTest, SigNet, TestNet
from btclib_node.config import (
    DEFAULT_MAX_PEER_CONNECTIONS,
    DEFAULT_MIN_RELAY_FEERATE,
    Config,
    split_host_port,
)
from btclib_node.rpc.auth import COOKIE_FILE, RpcAuthEntry, password_hmac
from tests import RPCAUTH


def test_chain_selection() -> None:
    """`_resolve_chain` accepts a `Chain` or its name; else raises."""
    assert Config(chain="mainnet") == Config(chain=Main())
    assert Config(chain="testnet") == Config(chain=TestNet())
    assert Config(chain="signet") == Config(chain=SigNet())
    assert Config(chain="regtest") == Config(chain=RegTest())
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


def test_rpc_host_defaults_to_localhost_not_every_interface() -> None:
    """`rpc_host` defaults to loopback; an explicit host still wins."""
    # #27: the rpc listener is a control plane, not a peer-to-peer
    # one, so its default is not the P2P listener's
    assert Config(chain="regtest").rpc_host == "127.0.0.1"
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


def test_connect_and_addnode_both_take_several_entries() -> None:
    """Every spec given is resolved, in order, not only the last one."""
    config = Config(
        chain="regtest",
        connect=["10.0.0.1", "10.0.0.2"],
        addnode=["10.0.0.3"],
    )
    assert len(config.connect) == 2
    assert config.addnode == (("10.0.0.3", RegTest().port),)


def test_connect_rejects_a_hostname() -> None:
    """A spec whose host is not an IP literal raises rather than dialling wrong.

    `p2p_manager.connect(peer_address(...))` -- the route `Node.run`
    dials `connect`/`addnode` through -- takes a parsed IP; this node
    resolves no hostname anywhere in its synchronous startup path
    (`config.py`'s own `_resolve_peers` docstring), so a hostname here
    is refused rather than silently mishandled.
    """
    with pytest.raises(
        ValueError, match="does not appear to be an IPv4 or IPv6 address"
    ):
        Config(chain="regtest", connect=["example.com"])


def test_seednode_is_the_same_shape_as_addnode() -> None:
    """ISS 1192: `seednode` resolves as `addnode` does, a hostname refused."""
    config = Config(chain="regtest", seednode=["10.0.0.1:1", "[::1]"])
    assert config.seednode == (("10.0.0.1", 1), ("::1", RegTest().port))
    assert config.addnode == ()
    with pytest.raises(ValueError, match="does not appear to be an IPv4 or IPv6"):
        Config(chain="regtest", seednode=["example.com"])


@pytest.mark.parametrize(
    ("kwargs", "dnsseed"),
    [
        pytest.param({}, True, id="default"),
        pytest.param({"connect": ["10.0.0.1"]}, False, id="connect"),
        pytest.param({"connect": ["0"]}, False, id="noconnect"),
        pytest.param({"max_connections": 0}, False, id="maxconnections-0"),
        pytest.param({"connect": ["10.0.0.1"], "dnsseed": True}, True, id="explicit"),
        pytest.param({"dnsseed": False}, False, id="off"),
    ],
)
def test_dnsseed_is_soft_set_off_as_core_s(
    kwargs: dict[str, Any], *, dnsseed: bool
) -> None:
    """ISS 1192: `InitParameterInteraction`'s soft-set, which a value overrides.

    Measured on `bitcoind` v31.1.0: `-connect` logs "setting -dnsseed=0"
    and `-dnsseed=1` beside it starts the DNS seed thread all the same.
    """
    assert Config(chain="regtest", **kwargs).dnsseed is dnsseed


def test_fixedseeds_is_on_unless_turned_off() -> None:
    """ISS 1192: Core's `DEFAULT_FIXEDSEEDS`."""
    assert Config(chain="regtest").fixedseeds is True
    assert Config(chain="regtest", fixedseeds=False).fixedseeds is False


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


def test_rpcauth_is_parsed_into_rpc_auth() -> None:
    """Each `-rpcauth` value becomes one `RpcAuthEntry`, none by default."""
    assert Config(chain="regtest").rpc_auth == ()
    assert Config(chain="regtest", rpcauth=[RPCAUTH]).rpc_auth == (
        RpcAuthEntry.parse(RPCAUTH),
    )


def test_a_malformed_rpcauth_raises() -> None:
    """Core refuses to start on one, with this message."""
    with pytest.raises(ValueError, match=r"^Invalid -rpcauth argument\.$"):
        Config(chain="regtest", rpcauth=["pytest"])


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
    with pytest.raises(ValueError, match=r"^Invalid -rpccookieperms=bogus;"):
        Config(chain="regtest", rpccookieperms="bogus")
    config = Config(chain="regtest", rpcpassword="pw", rpccookieperms="bogus")
    assert config.rpc_cookie_perms is None


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
