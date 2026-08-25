# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""A node speaking p2p to a real peer, over an actual socket.

Each module here starts one or more `Node`s and drives them through a
handshake, a download or a request/answer exchange the way another
implementation on the wire would, rather than calling their internals
directly the way `tests/unit/p2p/` does.
"""
