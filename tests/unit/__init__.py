# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests mostly mirroring `src/btclib_node/`'s own layout, module for module.

`tests/functional/` is the counterpart that only ever reaches a `Node`
from outside it, over its p2p and RPC sockets; a test here is free to
reach into the object under test directly, even where -- as in
`init_test.py` -- that object is a fully started node of its own.
"""
