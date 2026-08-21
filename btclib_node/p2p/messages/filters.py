# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from dataclasses import dataclass

from btclib_node.p2p.messages import add_headers


@dataclass
class Filterload:
    @classmethod
    def deserialize(cls, data):
        return cls()

    def serialize(self):
        return add_headers("filterload", b"")


@dataclass
class Filteradd:
    @classmethod
    def deserialize(cls, data):
        return cls()

    def serialize(self):
        return add_headers("filteradd", b"")


@dataclass
class Filterclear:
    @classmethod
    def deserialize(cls, data):
        return cls()

    def serialize(self):
        return add_headers("filterclear", b"")


@dataclass
class Merkleblock:
    @classmethod
    def deserialize(cls, data):
        return cls()

    def serialize(self):
        return add_headers("merkleblock", b"")


@dataclass
class Feefilter:
    @classmethod
    def deserialize(cls, data):
        return cls()

    def serialize(self):
        return add_headers("feefilter", b"")
