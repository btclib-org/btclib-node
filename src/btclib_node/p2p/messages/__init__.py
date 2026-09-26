# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The p2p payloads this node carries itself: none.

Every wire message this node speaks is `btclib.p2p`'s, imported where
it is used. This package holds no payload of its own, and
`p2p.callbacks` is where every command is dispatched to a handler.
BIP61's `reject` has none: Core's `ProcessMessage` ignores it as an
unknown type (`src/net_processing.cpp`, at bitcoin/bitcoin@9be056a8a7).
"""

__all__ = []
