# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

from dataclasses import dataclass

from btclib.p2p.payload import Payload


@dataclass
class Filterload(Payload):
    command = "filterload"

    @classmethod
    def parse(cls, data, *, check_validity: bool = True):
        return cls()

    def serialize(self, *, check_validity: bool = True) -> bytes:
        return b""


@dataclass
class Filteradd(Payload):
    command = "filteradd"

    @classmethod
    def parse(cls, data, *, check_validity: bool = True):
        return cls()

    def serialize(self, *, check_validity: bool = True) -> bytes:
        return b""


@dataclass
class Filterclear(Payload):
    command = "filterclear"

    @classmethod
    def parse(cls, data, *, check_validity: bool = True):
        return cls()

    def serialize(self, *, check_validity: bool = True) -> bytes:
        return b""


@dataclass
class Merkleblock(Payload):
    command = "merkleblock"

    @classmethod
    def parse(cls, data, *, check_validity: bool = True):
        return cls()

    def serialize(self, *, check_validity: bool = True) -> bytes:
        return b""


@dataclass
class Feefilter(Payload):
    command = "feefilter"

    @classmethod
    def parse(cls, data, *, check_validity: bool = True):
        return cls()

    def serialize(self, *, check_validity: bool = True) -> bytes:
        return b""
