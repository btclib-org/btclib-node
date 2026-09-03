# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`btclib_node.p2p.messages`: empty, `btclib.p2p` carrying every payload.

`init_test.py` reaches past this package too, into
`btclib_node.p2p.connection`: the command every payload of `btclib.p2p`
travels under, and how `Connection` frames, buffers and dispatches
messages built from those commands.
"""
