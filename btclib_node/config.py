# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from dataclasses import dataclass
from pathlib import Path

from btclib.fee import FeeRate

from btclib_node.chains import Chain, Main, RegTest, SigNet, TestNet

# Core's own floor, `DEFAULT_MIN_RELAY_TX_FEE` (`src/policy/policy.h`,
# read at bitcoin/bitcoin@58a7869f86): 100 sat/kvB. This node prices
# nothing at mempool acceptance yet (issue #85 is the open question of
# what a rejected or evicted transaction costs), so the value below is
# only ever the floor this node tells a peer about in `feefilter`
# (btclib-org/btclib-node#94) -- it is not enforced anywhere else.
DEFAULT_MIN_RELAY_FEERATE = FeeRate(sats_per_kvbyte=100)


@dataclass
class Config:
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
    pruned: bool
    debug: bool
    min_relay_feerate: FeeRate

    def __init__(
        self,
        chain: Chain | str = Main(),
        data_dir: str | Path | None = None,
        p2p_port: int | None = None,
        rpc_port: int | None = None,
        allow_p2p: bool = True,
        allow_rpc: bool = True,
        pruned: bool = False,
        debug: bool = False,
        log_path: str | None = "history.log",
        min_relay_feerate: FeeRate = DEFAULT_MIN_RELAY_FEERATE,
    ) -> None:
        if isinstance(chain, Chain):
            self.chain = chain
        elif isinstance(chain, str):
            if chain == "mainnet":
                self.chain = Main()
            elif chain == "testnet":
                self.chain = TestNet()
            elif chain == "signet":
                self.chain = SigNet()
            elif chain == "regtest":
                self.chain = RegTest()
            else:
                raise ValueError
        else:
            raise ValueError

        if data_dir:
            data_dir = Path(data_dir)
        else:
            data_dir = Path.home() / ".btclib"
        self.data_dir = data_dir.absolute() / self.chain.name

        self.p2p_port = None
        if allow_p2p:
            self.p2p_port = self.chain.port
            if p2p_port:
                self.p2p_port = p2p_port

        self.rpc_port = None
        if allow_rpc:
            self.rpc_port = self.chain.port + 1
            if rpc_port:
                self.rpc_port = rpc_port

        self.pruned = pruned

        self.debug = debug
        self.log_path = log_path
        self.min_relay_feerate = min_relay_feerate
