# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Config`, the settings one `Node` is built from.

Which chain to join, where its data lives, which listeners to start and
on which interfaces, and the feerate floor it tells a peer about in
`feefilter` -- `DEFAULT_MIN_RELAY_FEERATE` below, Core's own
`DEFAULT_MIN_RELAY_TX_FEE`. `_resolve_chain` is what turns a chain
already built, or a network's name, into the `Chain` a `Config` carries.
`pruned` is reserved rather than honoured: see its own field comment.
`split_host_port` is `cli.py`'s own splitter for `-rpcbind`'s optional
port too, which is why it is public here rather than named with a
leading underscore.
"""

from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING

from btclib.fee import FeeRate

from btclib_node.chains import Chain, Main, RegTest, SigNet, TestNet
from btclib_node.exceptions import (
    InvalidChainTypeError,
    PruningNotImplementedError,
    UnknownChainError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["DEFAULT_MIN_RELAY_FEERATE", "Config", "split_host_port"]

# Core's own floor, `DEFAULT_MIN_RELAY_TX_FEE` (`src/policy/policy.h`,
# read at bitcoin/bitcoin@58a7869f86): 100 sat/kvB. This node prices
# nothing at mempool acceptance yet (issue #85 is the open question of
# what a rejected or evicted transaction costs), so the value below is
# only ever the floor this node tells a peer about in `feefilter`
# (btclib-org/btclib-node#94) -- it is not enforced anywhere else.
DEFAULT_MIN_RELAY_FEERATE = FeeRate(sats_per_kvbyte=100)
# A named module-level singleton rather than `Main()` written straight
# into __init__'s own signature below: a call there is made once, at
# import time, and B008 is what a reader would otherwise have to notice
# on their own -- this is the fix ruff's own message suggests, and the
# shape `DEFAULT_MIN_RELAY_FEERATE` above already uses for the same
# reason.
DEFAULT_CHAIN = Main()


def split_host_port(spec: str, default_port: int) -> tuple[str, int]:
    """Split "host[:port]" the way Core's own `SplitHostPort` does.

    The last colon is the port separator, unless it is not the only one
    and does not close an IPv6 literal's own `[...]` -- an IPv6 address
    given without brackets and without a port is read whole rather than
    split on one of its own colons, exactly what
    `src/util/strencodings.cpp`'s `SplitHostPort` does (read at
    bitcoin/bitcoin@ca7162cde5). `default_port` is what a spec naming
    none falls back to: Core's own callers pre-fill the port before
    calling `SplitHostPort`, which only overwrites it when the spec
    actually names one (`ConnectNode`, `src/net.cpp:505-507`, same sha)
    -- `-connect=1.2.3.4` and `-addnode=1.2.3.4` both dial the chain's
    own default P2P port this way.
    """
    host = spec
    port = default_port
    colon = spec.rfind(":")
    if colon != -1:
        bracketed = spec.startswith("[") and spec[:colon].endswith("]")
        multi_colon = spec.rfind(":", 0, colon) != -1
        if colon == 0 or bracketed or not multi_colon:
            host, port_text = spec[:colon], spec[colon + 1 :]
            try:
                port = int(port_text)
            except ValueError:
                port = -1
            if not 0 < port <= 0xFFFF:  # noqa: PLR2004
                err_msg = f"{spec!r} names an invalid port"
                raise ValueError(err_msg)
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, port


def _resolve_peers(
    specs: Sequence[str], default_port: int
) -> tuple[tuple[str, int], ...]:
    """Split every spec in `specs` and check its host is a literal IP.

    A hostname is not resolved here, unlike Core's own `-connect`/
    `-addnode`, which dial through `CConnman::ConnectNode` and resolve
    one via `Resolve` (`src/net.cpp`) same as any other peer. This
    node's own dial route -- `p2p_manager.connect(peer_address(...))`,
    the one ISS 573 (btclib-org/btclib-node#573) asks these two fields
    to use -- takes a `NetworkAddressV2` built straight off a parsed IP
    (`p2p/address.py`'s `peer_address`), and nothing in this node's
    synchronous startup path resolves a name into one: the only DNS
    lookup here is `PeerDB.get_addr_from_dns`'s own coroutine, on
    `P2pManager`'s asyncio loop, which is not reachable before that
    manager's thread exists. Widening `peer_address` or plumbing an
    async resolve into `Node.run` for two config fields is a larger
    change than this branch's scope; a hostname is refused up front,
    at `Config` construction, rather than dialled wrong or silently
    dropped later.
    """
    peers: list[tuple[str, int]] = []
    for spec in specs:
        host, port = split_host_port(spec, default_port)
        ip_address(host)  # raises ValueError on a hostname or garbage
        peers.append((host, port))
    return tuple(peers)


def _resolve_chain(chain: Chain | str) -> Chain:
    if isinstance(chain, Chain):
        return chain
    if not isinstance(chain, str):
        raise InvalidChainTypeError(chain)
    if chain == "mainnet":
        return Main()
    if chain == "testnet":
        return TestNet()
    if chain == "signet":
        return SigNet()
    if chain == "regtest":
        return RegTest()
    raise UnknownChainError(chain)


@dataclass
class Config:
    """Every setting one `Node` is built from, flat and keyword-only.

    Built by `__init__` below rather than by the fields' own defaults,
    since a chain given as a name has to resolve to a `Chain` first, and
    a port left unset by `allow_p2p=False`/`allow_rpc=False` has to
    become `None` rather than the class's own declared `int`.
    """

    chain: Chain
    # a Path, which is what __init__ below stores and what every reader
    # of it does path arithmetic on
    data_dir: Path
    # `None` and not an int is the whole of what `allow_p2p=False` and
    # `allow_rpc=False` do: __init__ below leaves the port unset, and
    # Node reads it as the answer to whether that listener is started
    # at all. Declared `int` these two said the opposite of what they
    # hold, and every reader believing the annotation would take a
    # disallowed port for a port to bind.
    p2p_port: int | None
    rpc_port: int | None
    # what RpcManager binds instead of every interface: an RPC server is
    # this node's control plane, not a peer-to-peer listener, and
    # rpc/callbacks.py carries no authentication of its own -- so the
    # interface it is reachable from is the one thing between an
    # unauthenticated caller and the network. Bitcoin Core's own
    # `rpcbind`/`rpcallowip` default to localhost for the same reason;
    # P2pManager.server binds every interface unconditionally, and is
    # right to, since a peer listener is supposed to accept a stranger.
    rpc_host: str
    # always `False`: `__init__` below refuses `True` with
    # `PruningNotImplementedError` (btclib-org/btclib-node#574) rather
    # than accepting it and pruning nothing. The field stays, and so
    # does the parameter below, because nothing is removed from
    # `Config`'s public API -- only the one value that would have
    # written every block of the chain to disk while a caller believed
    # it would not.
    pruned: bool
    debug: bool
    min_relay_feerate: FeeRate
    # (ip, port) pairs, resolved by `_resolve_peers` above: Core's own
    # `-connect`, which dials these alone and turns off DNS seeding and
    # every automatically-drawn outbound connection
    # (`InitParameterInteraction`, `src/init.cpp:814-819`, and
    # `connOptions.m_use_addrman_outgoing = false`, `src/init.cpp:2337`,
    # both at bitcoin/bitcoin@ca7162cde5). Empty for `-connect=0` too --
    # Core's own "dial nobody, but still on the -connect arm" spelling
    # (`connect.size() != 1 || connect[0] != "0"`, `src/init.cpp:2333`,
    # same sha) -- which is why `connect_given` below, not this tuple's
    # truthiness, is what `P2pManager` reads to decide the two above.
    connect: tuple[tuple[str, int], ...]
    # Whether `-connect` was named at all, `["0"]` included: Core's own
    # `!args.GetArgs("-connect").empty()`, read off the raw sequence
    # `__init__` below was given rather than off `connect` above, since
    # the two disagree on exactly that one value.
    connect_given: bool
    # the same pairs, dialled alongside the ordinary draw rather than
    # instead of it: Core's own `-addnode`
    # (`connOptions.m_added_nodes`, `src/init.cpp:2193-2198`, same sha).
    addnode: tuple[tuple[str, int], ...]
    # Core's own `-listen`, `DEFAULT_LISTEN` (`src/net.h`) true unless
    # `-connect` is given, in which case `InitParameterInteraction`
    # (`src/init.cpp:814-819`, same sha) soft-sets it false -- a default
    # `cli.py`'s own `_resolve_listen` computes the same way, an explicit
    # `-listen`/`-nolisten` always winning over it. `False` here means
    # Core's own `-listen=0`: no bound listening socket, outbound
    # connections still made -- not `allow_p2p=False`, which unsets the
    # port and starts no `P2pManager` at all, so nothing could dial out
    # either.
    listen: bool

    # every parameter here is one independent setting, not a group of
    # related ones this signature happens to expose together: `chain` is
    # not `data_dir`'s business, `rpc_host` is not `debug`'s, and nesting
    # them into sub-objects would only move each still-independent knob
    # behind one more name for every caller, all of which already read
    # this constructor by keyword (`grep -rn "Config(" tests/
    # src/btclib_node/` finds no positional call). PLR0913/PLR0917 measure a
    # count this object's whole purpose is to be flat, not a shape it
    # backed into. Keyword-only throughout (issue #341's own FBT round)
    # for the same reason: `allow_p2p`/`allow_rpc`/`pruned`/`debug` are
    # what that round's own findings are, but every other parameter here
    # is already keyword at every call site the grep above found, so
    # making only the booleans keyword-only would leave the same
    # constructor answering `Config("regtest")` for one parameter and
    # refusing it for the next -- which also drops PLR0917 (too many
    # positional arguments) below to zero, keyword-only meaning there
    # is no longer a positional count to measure.
    def __init__(  # noqa: PLR0913
        self,
        *,
        chain: Chain | str = DEFAULT_CHAIN,
        data_dir: str | Path | None = None,
        p2p_port: int | None = None,
        rpc_port: int | None = None,
        rpc_host: str = "127.0.0.1",
        allow_p2p: bool = True,
        allow_rpc: bool = True,
        pruned: bool = False,
        debug: bool = False,
        log_path: str | None = "history.log",
        min_relay_feerate: FeeRate = DEFAULT_MIN_RELAY_FEERATE,
        connect: Sequence[str] = (),
        addnode: Sequence[str] = (),
        listen: bool = True,
    ) -> None:
        """Resolve `chain` and ports, and refuse `pruned=True`."""
        self.chain = _resolve_chain(chain)

        data_dir = Path(data_dir) if data_dir else Path.home() / ".btclib"
        self.data_dir = data_dir.absolute() / self.chain.name

        self.connect_given = bool(connect)
        # Core's own "-connect=0": still the -connect arm above, but
        # nobody named to dial -- `_resolve_peers` never sees the "0"
        # itself, since `ip_address("0")` is not a valid literal and
        # would raise where Core instead special-cases the value.
        self.connect = (
            () if list(connect) == ["0"] else _resolve_peers(connect, self.chain.port)
        )
        self.addnode = _resolve_peers(addnode, self.chain.port)
        self.listen = listen

        self.p2p_port = None
        if allow_p2p:
            self.p2p_port = self.chain.port
            if p2p_port:
                self.p2p_port = p2p_port

        self.rpc_port = None
        if allow_rpc:
            self.rpc_port = self.chain.rpc_port
            if rpc_port:
                self.rpc_port = rpc_port

        self.rpc_host = rpc_host

        if pruned:
            raise PruningNotImplementedError
        self.pruned = pruned

        self.debug = debug
        self.log_path = log_path
        self.min_relay_feerate = min_relay_feerate
