# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`btclib_node.p2p.messages`: the payloads this node defines on its own.

`init_test.py` reaches past this package too, into
`btclib_node.p2p.connection`: the command every payload of this
package or of `btclib.p2p` travels under, and how `Connection` frames,
buffers and dispatches messages built from those commands.
`errors_test.py` is `errors.py`'s own module, named the same way it is.
"""
