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
"""

from dataclasses import dataclass
from pathlib import Path

from btclib.fee import FeeRate

from btclib_node.chains import Chain, Main, RegTest, SigNet, TestNet
from btclib_node.exceptions import (
    InvalidChainTypeError,
    PruningNotImplementedError,
    UnknownChainError,
)

__all__ = ["DEFAULT_MIN_RELAY_FEERATE", "Config"]

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
    ) -> None:
        """Resolve `chain` and ports, and refuse `pruned=True`."""
        self.chain = _resolve_chain(chain)

        data_dir = Path(data_dir) if data_dir else Path.home() / ".btclib"
        self.data_dir = data_dir.absolute() / self.chain.name

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
