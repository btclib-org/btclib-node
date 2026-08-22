# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The payloads this node speaks, one class per command.

Each subclasses `btclib.p2p.payload.Payload`: it owns the `command` its
octets travel under, so the name a payload serializes under and the name
p2p.callbacks dispatches on are one constant; `serialize` returns the
payload *alone*; and `to_message` frames it for a network.

The four header fields are `btclib.p2p.message.Message`'s, and
p2p.connection.Connection is the single place that puts them on and
takes them off.
"""
