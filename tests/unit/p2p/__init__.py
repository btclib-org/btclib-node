# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`btclib_node.p2p`: the protocol side, module for module.

Each `*_test.py` here is named for the module of `btclib_node/p2p/` it
exercises, and `messages/` nests the same way the package under test
does. `tests/functional/p2p/` is the counterpart that reaches the same
code only from the far end of a socket.
"""
