# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Tests that only ever reach a `Node` from outside it.

A test under this package builds a real node and speaks to it over its
p2p or RPC socket, the way a peer or a client on the wire would. The
package's own `p2p/` and `rpc/` halves say in their docstrings which of
the two surfaces each drives. `tests/README.md` declares the split from
`unit/` and `integration/`, and why it exists.
"""
