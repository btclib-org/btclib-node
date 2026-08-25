# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Unit tests for `btclib_node.rpc`: connection, manager, main and callbacks.

Each module here drives its own piece of `src/btclib_node/rpc/` in
isolation, against doubles rather than a live socket -- `tests/
functional/rpc/` is what drives the same surface end to end over one.
"""
