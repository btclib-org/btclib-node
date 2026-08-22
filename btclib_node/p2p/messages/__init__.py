# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The p2p payloads btclib does not define.

Most of what this node speaks is `btclib.p2p`'s, imported where it is
used. What is here is the remainder: those that carry
`btclib_node.p2p.address.NetworkAddress`, which is still this package's;
BIP61's `reject`, which Bitcoin Core removed; and the commands whose
payload is empty.

Each subclasses `btclib.p2p.payload.Payload` and owns the `command` its
octets travel under, so the name a payload serializes under and the name
p2p.callbacks dispatches on are one constant. The four header fields are
`btclib.p2p.message.Message`'s, and p2p.connection.Connection is the
single place that puts them on and takes them off.
"""
