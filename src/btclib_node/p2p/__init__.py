# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The peer-to-peer protocol this node speaks.

Connections, the peer manager, the address book and the message
handlers `Node`'s loop calls. `manager.P2pManager` is the thread;
`connection.Connection` is one socket on it; `callbacks.callbacks` and
`callbacks.handshake_callbacks` are the dispatch tables
`main.handle_p2p` and `main.handle_p2p_handshake` read; `address.PeerDB`
is the address book gossip and DNS both write to.
"""
