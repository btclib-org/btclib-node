# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Unit tests for `btclib_node.chainstate`.

Covers `Chainstate`'s own open and close, `BlockIndex`, `UtxoIndex`,
`FilterIndex`, and the contextual checks `BlockIndex` calls before extending
the active chain.
"""
