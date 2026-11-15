# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""Unit tests for `btclib_node.rpc`: connection, manager, main and callbacks.

Each module here drives its own piece of `src/btclib_node/rpc/` in
isolation, against doubles rather than a live socket -- `tests/
functional/rpc/` is what drives the same surface end to end over one.
"""

from btclib.b58 import wif_from_prv_key

#: A fixed random private key, as `assert_secret_free` asks of a secret it
#: is fed, spelled as a caller may pass it where a string belongs: 64 hex
#: digits, the length of a hash, and its WIF.
LEAK_HEX = "84fbcfd8f1d5af981a7994abaf08a471689bd13d24a720dfbd1c73c20579640a"
LEAK_WIF = wif_from_prv_key(LEAK_HEX, "mainnet", compressed=True)
