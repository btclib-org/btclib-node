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
#: A compressed WIF of another fixed random key, which is also an I2P host's
#: label: `SpecialAddress.parse` reads base32 in either case, and an I2P label
#: has no checksum.
LEAK_I2P_WIF = "KxtMbjALuPLnk7RcdWLdVCP6Q2gywHUTLmc63LKVsMYTGo5MtWWq"

#: A key given where an address belongs, as a numeric lookup reads it, each
#: beside the spelling of the key it holds: macOS reads the decimal, and the
#: hex after `0x`, as an IPv4 address, and any text after an IPv6 address's
#: `%` as its scope, and every platform reads an I2P host.
LEAK_ADDRESSES = (
    (str(int(LEAK_HEX, 16)), str(int(LEAK_HEX, 16))),
    (f"0x{LEAK_HEX}", LEAK_HEX),
    (f"fe80::1%{LEAK_WIF}", LEAK_WIF),
    (f"{LEAK_I2P_WIF}.b32.i2p", LEAK_I2P_WIF),
)
