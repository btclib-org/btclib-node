# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The p2p payloads this node carries itself: none.

Every wire message this node speaks is `btclib.p2p`'s, imported where
it is used -- BIP61's `reject` included, `Reject` and `RejectCode`
being `btclib.p2p.reject`'s. This package holds no payload of its own,
and `p2p.callbacks` is where every command is dispatched to a handler.
"""

__all__ = []
