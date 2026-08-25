# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Config`'s chain resolution, path arithmetic, ports and feerate floor."""

from pathlib import Path

import pytest
from btclib.fee import FeeRate

from btclib_node.chains import Main, RegTest, SigNet, TestNet
from btclib_node.config import DEFAULT_MIN_RELAY_FEERATE, Config


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


def test_port() -> None:
    """A given `p2p_port` or `rpc_port` is stored back unchanged."""
    assert Config(chain="regtest", p2p_port=1).p2p_port == 1
    assert Config(chain="regtest", rpc_port=1).rpc_port == 1


def test_min_relay_feerate_defaults_to_cores_own_floor() -> None:
    """`min_relay_feerate` defaults to Core's own floor, 100 sat/kvB."""
    # bitcoin/bitcoin@58a7869f86's DEFAULT_MIN_RELAY_TX_FEE, src/policy/policy.h
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
