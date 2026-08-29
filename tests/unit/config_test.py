# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Config`'s chain resolution, path arithmetic, ports and feerate floor."""

from pathlib import Path

import pytest
from bitcoin_core_rpc import rpc_port_from_chain
from btclib.fee import FeeRate

from btclib_node.chains import Main, RegTest, SigNet, TestNet
from btclib_node.config import DEFAULT_MIN_RELAY_FEERATE, Config, split_host_port


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
    # #27: the rpc listener is an unauthenticated control plane, not a
    # peer-to-peer one, so its default is not the P2P listener's
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
    `_resolve_listen`, argued against `connect_given` above one layer up
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
