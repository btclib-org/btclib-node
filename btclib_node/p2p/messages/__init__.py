# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The p2p payloads this node carries itself.

Most of what this node speaks is `btclib.p2p`'s, imported where it is
used. What is here is BIP61's `reject`, which Bitcoin Core removed and
btclib therefore never carried, and the commands whose payload is
empty. btclib carries a codec for those too, in
`btclib.p2p.negotiation`, and btclib-org/btclib-node#196 is where the
duplication is decided.

Each subclasses `btclib.p2p.payload.Payload` and owns the `command` its
octets travel under, so the name a payload serializes under and the name
p2p.callbacks dispatches on are one constant. The four header fields are
`btclib.p2p.message.Message`'s, and p2p.connection.Connection is the
single place that puts them on and takes them off.
"""
