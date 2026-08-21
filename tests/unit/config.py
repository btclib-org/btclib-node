# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import pytest

from btclib_node.chains import Main, RegTest, SigNet, TestNet
from btclib_node.config import Config


def test_chain_selection():
    assert Config(chain="mainnet") == Config(chain=Main())
    assert Config(chain="testnet") == Config(chain=TestNet())
    assert Config(chain="signet") == Config(chain=SigNet())
    assert Config(chain="regtest") == Config(chain=RegTest())
    with pytest.raises(ValueError):
        Config(chain=None)
    with pytest.raises(ValueError):
        Config(chain="wrongchain")


def test_data_dir():
    config = Config(chain="regtest", data_dir="dir")
    assert config.data_dir != "dir"


def test_port():
    assert Config(chain="regtest", p2p_port=1).p2p_port == 1
    assert Config(chain="regtest", p2p_port=1, allow_p2p=False).p2p_port != 1
    assert Config(chain="regtest", rpc_port=1).rpc_port == 1
    assert Config(chain="regtest", rpc_port=1, allow_rpc=False).rpc_port != 1
