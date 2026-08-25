# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Functional tests for the JSON-RPC surface, over a real node and socket.

Each module here starts a live `Node`, opens an RPC connection to it and
drives one or more methods through it end to end -- `tests/unit/rpc/`
is what exercises the same handlers in isolation, against doubles.
"""
