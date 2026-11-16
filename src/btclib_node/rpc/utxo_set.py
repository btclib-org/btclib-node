# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The RPCs that read the UTXO set whole: `scantxoutset`.

Core's `scantxoutset` is in `src/rpc/blockchain.cpp`, with the descriptor
arguments in `src/rpc/util.cpp`, both at bitcoin/bitcoin@f0de87f040, the
v32.1 tag. Its `InferDescriptor` is bip32_descriptors' `infer_descriptor`.
Each handler has `rpc.callbacks`' signature and runs on `Node`'s
thread (`ARCHITECTURE.md`).

A scan is a generator that `rpc.main` resumes on each pass of `Node`'s
loop. It walks the stored coins `_STEP` at a time and yields between
steps, so the loop serves other requests meanwhile, `status` and `abort`
among them. Core runs the scan on an HTTP worker and the two others on
workers of their own, which share atomics; here all three run on `Node`'s
thread, and the scan's state needs no lock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from bip32_descriptors.bip32 import BIP32KeyData
from bip32_descriptors.descriptors import (
    Descriptor,
    Provider,
    infer_descriptor,
    multipath_descriptors,
    strip_checksum,
)
from bip32_descriptors.descriptors import parse as parse_descriptor
from bitcoin_core_rpc import RPCErrorCode
from btclib import b58
from btclib.exceptions import BTClibException, BTClibValueError
from btclib.network import NETWORKS
from ecc.exceptions import EccException
from ecc.ssa import point_from_bip340pub_key_var

from btclib_node.block_db import Coin
from btclib_node.constants import MAX_QUOTED_NAME, quoted_name
from btclib_node.rpc.connection import btc_amount
from btclib_node.rpc.errors import RpcError, json_type_name, type_errors
from btclib_node.rpc.help import HELP_TEXT

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from btclib_node import Node
    from btclib_node.rpc.connection import RpcConnection

__all__ = ["scan_tx_out_set"]

# Coins read between two yields, and between two looks at `terminate_flag`
# and the abort flag. Core looks every 8192 coins (`FindScriptPubKey`),
# which is a longer pause than `Node`'s loop should be asked to wait here.
_STEP = 1024

# Keys derived between two yields of the expansion. One index's keys are
# derived in a single step, however many there are.
_EXPAND_STEP = 16

# A merge of fewer providers than this does not yield
_MERGE_STEP = 2**14

# `FindScriptPubKey` updates the progress every 256 coins
_PROGRESS_EVERY = 256

# `RPCArg::Default{1000}`, the range a ranged descriptor is expanded over
_DEFAULT_RANGE_END = 1000

# `ParseDescriptorRange`'s own bound
_MAX_RANGE_SIZE = 1_000_000

_INT64 = range(-(2**63), 2**63)

# `ParseScriptContext`: where in a descriptor an expression stands, and
# where `ParsePubkeyInner` reads a key
_TOP, _P2SH, _P2WSH, _P2TR = "TOP", "P2SH", "P2WSH", "P2TR"
_P2WPKH, _MUSIG = "P2WPKH", "MUSIG"

# the contexts `sh()`, `wsh()` and the tree of `tr()` give what they hold
_INNER = {"sh": _P2SH, "wsh": _P2WSH, "tr": _P2TR}

# A function of the draft BIP of bitcoin/bips#1721 where a miniscript
# fragment can begin: first, or after `(`, `,` or a wrapper's `:`
_DRAFT_FUNCTION = re.compile(r"(?:^|[(,:])(?P<name>raw(?:pkh|node|leaf))\(")

# what `ParseScript` refuses an expression with in `sh()` and in `wsh()`,
# where neither a function nor a miniscript reads it
_NEEDED = {
    _P2SH: "A function is needed within P2SH",
    _P2WSH: "A function is needed within P2WSH",
}

# what splits a descriptor into the functions it opens and closes, and
# the arguments between them
_DELIMITER = re.compile(r"([(),{}])")

# what `IsHex` reads as hex: `ParsePubkeyInner` reads it as a public key,
# whatever its length, where no path follows it
_HEX = re.compile(r"(?:[0-9a-fA-F]{2})+")

# a key origin as `ParsePubkey` reads it, each step of whose path it reads
# only below 2**31
_ORIGIN = re.compile(r"\[[0-9a-fA-F]{8}(?P<path>(?:/[0-9]+['h]?)*)\]")

# a derivation path, of multipath and hardened steps or not, as it may
# follow a key or a `musig()`
_PATH = re.compile(r"(?:/(?:[0-9]+|<[0-9]+['h]?(?:;[0-9]+['h]?)+>)['h]?)*(?:/\*['h]?)?")

# the digits of 2**31 - 1, the largest step Core reads
_STEP_DIGITS = 10

# what `IsSpace` reads as whitespace
_SPACES = " \f\n\r\t\v"
_X_ONLY_KEY_BYTES = 32
_COMPRESSED_KEY_BYTES = 33
_SEC_KEY_BYTES = 65
_HYBRID_PREFIXES = {0x06, 0x07}

# a part of each message bip32_descriptors raises as BTClibValueError, not
# ecc's EccException, where Core refuses a key or a `musig()` with words of
# its own, those of `ParsePubkey` and `ParsePubkeyInner`, and of each
# message it raises where Core may refuse a key before what it refuses: a
# miniscript outside `wsh()` and `tr()`, whose keys Core reads first, and
# a `musig(...)/0/*` where a script belongs, which bip32_descriptors
# refuses in Core's words.
_BTCLIB_KEY_REFUSALS = (
    "x-only public keys",
    "musig(): not a public key",
    "invalid public key prefix",
    "invalid public key length",
    "uncompressed public keys are not allowed",
    "musig() is only allowed in tr() and rawtr()",
    "musig() is a key expression",
    "musig() cannot be nested inside a key origin",
    "musig() derivation requires every participant to be extended",
    "Too many ')' in musig() expression",
    "missing ']' in the key origin",
    "Key is invalid due to whitespace",
    "key is not valid",
    "key: WIF prefix",
    "key: version",
    "invalid key expression",
    "not a key expression",
    "derivation path after a key that cannot derive",
    "No key provided",
    "Miniscript expressions can only be used in wsh or tr.",
    "'musig(...)",
)

# the end of the message both bip32_descriptors and Core refuse an
# expression no function reads with
_NOT_A_FUNCTION = "is not a valid descriptor function"

# `ParsePubkeyInner`'s message for text it reads as no key
_NO_KEY = "key '...' is not valid"

# `ParsePubkey`'s message for a `musig()` it reads outside `P2TR`
_MUSIG_PLACE = "musig() is only allowed in tr() and rawtr()"

# the functions `ParseScript` reads a key in, naming themselves in its
# message
_KEY_FUNCTIONS = {"pk", "pkh", "combo", "wpkh", "tr", "rawtr"}
_MULTI_FUNCTIONS = {"multi", "sortedmulti", "multi_a", "sortedmulti_a"}

# the contexts where `ParsePubkeyInner` reads an uncompressed key
_UNCOMPRESSED_CONTEXTS = {_TOP, _P2SH}

# the functions `_core_refusal` refuses where another holds them, which
# Core meets before any key that follows them
_DRAFT_NAMES = {"raw", "rawpkh", "rawnode", "rawleaf"}

# the functions of one key, whose whole argument `ParsePubkey` reads as
# the key, commas included: `Func` hands it on whole in `ParseScript`, and
# so does `ParseKey` in miniscript, after `Expr` stops at the `)` that
# closes it. `tr()` reads its key up to a comma, `rawtr()` refuses a comma
# with words of its own, and `multi()` and `musig()` split at each.
_ONE_KEY_FUNCTIONS = {"pk", "pkh", "wpkh", "combo", "pk_k", "pk_h"}

# every function whose argument may be a key Core refuses with
# `ParsePubkeyInner`'s message, miniscript's and `musig()` included
_KEY_HOLDERS = (
    _KEY_FUNCTIONS
    | _MULTI_FUNCTIONS
    | {
        "musig",
        "pk_k",
        "pk_h",
        "unused",
    }
)


@dataclass
class _Scan:
    """The scan `Node` runs, as `status` and `abort` see it."""

    progress: int = 0
    abort: bool = False


# `g_scan_in_progress`, `g_scan_progress` and `g_should_abort_scan` in Core,
# which are process globals; one scan at a time per node here
_scans: WeakKeyDictionary[Node, _Scan] = WeakKeyDictionary()


@dataclass(frozen=True)
class _Source:
    """A parsed descriptor, and the private keys it was read with."""

    descriptor: Descriptor
    prv_keys: dict[str, str]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int64(value: object) -> int:
    """`UniValue::getInt<int64_t>()`: an integer number in range."""
    if not isinstance(value, int) or value not in _INT64:
        raise RpcError(RPCErrorCode.MISC_ERROR, "JSON integer out of range")
    return value


def _range(value: object) -> tuple[int, int]:
    """Return a `range` as Core's `ParseDescriptorRange` reads it."""
    if _is_number(value):
        low, high = 0, _int64(value)
    elif (
        isinstance(value, list)
        and len(value) == 2  # noqa: PLR2004
        and all(_is_number(end) for end in value)
    ):
        low, high = _int64(value[0]), _int64(value[1])
        if low > high:
            raise RpcError(
                RPCErrorCode.INVALID_PARAMETER,
                "Range specified as [begin,end] must not have begin after end",
            )
    else:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Range must be specified as end or as [begin,end]",
        )
    if low < 0:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER, "Range should be greater or equal than 0"
        )
    if high >> 31:
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, "End of range is too high")
    if high >= low + _MAX_RANGE_SIZE:
        raise RpcError(RPCErrorCode.INVALID_PARAMETER, "Range is too large")
    return low, high


def _expr(text: str) -> tuple[str, str]:
    """Return `text`'s first expression and what follows it: Core's `Expr`."""
    level = 0
    for at, char in enumerate(text):
        if char in "({":
            level += 1
        elif level and char in ")}":
            level -= 1
        elif not level and char in "),}":
            return text[:at], text[at:]
    return text, ""


def _argument(text: str) -> str:
    """Return `text` up to the `)` closing the call it is the argument of."""
    level = 0
    for at, char in enumerate(text):
        if char in "({":
            level += 1
        elif char in ")}":
            if not level:
                return text[:at]
            level -= 1
    return text


def _core_refusal(expr: str, ctx: str, network: str | None = None) -> str | None:
    """Return the message for what Core refuses in `expr`, else None.

    `expr` is a descriptor bip32_descriptors has read, or an expression
    inside one. bip32_descriptors follows the draft BIP of bitcoin/bips#1721
    and reads `raw()` inside `sh()`, `wsh()` and `tr()`, and `rawpkh()`,
    `rawnode()` and `rawleaf()`. `ParseScript` in
    `src/script/descriptor.cpp` at bitcoin/bitcoin@f0de87f040, the v32.1
    tag, reads none of them. It refuses the first it meets, with a message
    that depends on where it stands: this walks `expr` as it does.

    `ParseScript` reads no `musig()` where a script belongs either, after
    a key origin or not, and refuses it in the same words.
    bip32_descriptors refuses one there with a message of its own, which
    `_parse` replaces with this function's where the two differ.

    Where Core quotes the expression, which may hold a key, this writes it
    as `_quoted` does. Core quotes the expression whole, so a draft
    function inside a miniscript is named by the miniscript's function, as
    `and_v` is in `tr(K,and_v(v:rawpkh(H),pk(K)))`.

    `network`, the node's, is given where bip32_descriptors has refused
    `expr`, which `_tree_refusal` then reads as it says.

    `Func` reads `expr` as a call of `sh()`, `wsh()`, `tr()` or `raw()`
    only where `expr` ends with `)`. Where text follows the call, as `/0`
    in `tr(K,raw(51))/0`, Core refuses `expr` whole as no function, and
    this does not read the call either.
    """
    name, _, args = expr[:-1].partition("(")
    called = expr.endswith(")")
    if called and name in _INNER:
        if name == "tr":
            return _tree_refusal(args, network)
        return _core_refusal(args, _INNER[name], network)
    if called and name == "raw":
        return None if ctx == _TOP else "Can only have raw() at top level"
    if name.rpartition("]")[2] != "musig" and _DRAFT_FUNCTION.search(expr) is None:
        return None
    # neither a function `ParseScript` knows nor a miniscript
    quoted = _quoted(name, expr.rpartition(")")[2])
    return _NEEDED.get(ctx, f"{quoted} is not a valid descriptor function")


def _fits(path: str) -> bool:
    """Say whether each step of `path` is below 2**31, as Core reads one.

    A step may have any number of digits, more than `int` converts from a
    string (`sys.get_int_max_str_digits`), so this converts only a step of
    at most `_STEP_DIGITS` digits once its leading zeros are dropped.
    """
    for step in re.findall("[0-9]+", path):
        digits = step.lstrip("0")
        if len(digits) > _STEP_DIGITS or int(digits or "0") >> 31:
            return False
    return True


def _quoted(name: str, tail: str) -> str:
    """Return `'name(...)tail'`, with `...` for a part that may hold a key.

    `name` is what comes before an expression's first `(`, and `tail` what
    comes after its last `)`. The name is kept where it has at most
    `MAX_QUOTED_NAME` characters, as `quoted_name` keeps one, or where it
    is `musig` after a key origin Core reads. The tail is kept where it is
    a path of steps below 2**31. A key written in hex, as a WIF or as an
    extended key is neither such an origin nor such a path, so a key given
    by mistake in either place is written `...` (SECURITY.md, "Where this
    node departs from Bitcoin Core").
    """
    origin = _ORIGIN.fullmatch(name.removesuffix("musig"))
    if len(name) > MAX_QUOTED_NAME and not (
        name.endswith("]musig") and origin is not None and _fits(origin["path"])
    ):
        name = "..."
    if _PATH.fullmatch(tail) is None or not _fits(tail):
        tail = "..."
    return f"'{name}(...){tail}'"


def _refuses_key(err: BTClibException | EccException) -> bool:
    """Say whether `err` is ecc's or one of `_BTCLIB_KEY_REFUSALS`."""
    message = str(err)
    return isinstance(err, EccException) or any(
        refused in message for refused in _BTCLIB_KEY_REFUSALS
    )


def _read_alone(text: str, network: str | None) -> tuple[bool, str | None]:
    """Say whether bip32_descriptors refuses `text`, and the walk's message.

    `text` is a `tr()` of one leaf at most, read for `network`. The walk's
    message is `_invalid_key_refusal`'s, where bip32_descriptors refuses
    `text` for a key. Without `network`, bip32_descriptors has read the
    whole descriptor, and the answer is that it reads `text`.
    """
    if network is None:
        return False, None
    try:
        for one in multipath_descriptors(text):
            parse_descriptor(one, network, {})
    except (BTClibException, EccException) as err:
        return True, _invalid_key_refusal(text, network) if _refuses_key(err) else None
    return False, None


def _tree_refusal(args: str, network: str | None = None) -> str | None:
    """Return the message for the internal key or the first leaf Core refuses.

    Where `network` is given, bip32_descriptors has refused the whole
    descriptor. This then reads the internal key alone, and each leaf as
    the one leaf of a tree, with `_read_alone`. In a leaf, Core refuses
    first a key `_read_alone` names, then what `_core_refusal` names.
    Where bip32_descriptors refuses the internal key or a leaf otherwise,
    this answers None, for `_parse` to give bip32_descriptors' message:
    Core refuses such a leaf before any leaf after it, as `foo(1)` in
    `tr(K,{foo(1),raw(51)})`.
    """
    # the internal key, then the tree's leaves, each in braces or not
    internal, tree = _expr(args)
    refused, walked = _read_alone(f"tr({internal})", network)
    if refused:
        return walked
    while tree:
        leaf, rest = _expr(tree.lstrip(",{"))
        refused, walked = _read_alone(f"tr({internal},{leaf})", network)
        if walked or (refusal := _core_refusal(leaf, _P2TR)):
            return walked or refusal
        if refused:
            return None
        rest = rest.lstrip("}")
        # a `)` at level 0, which no tree bip32_descriptors reads holds
        if rest == tree:
            return None
        tree = rest
    return None


def _is_point(key: bytes) -> bool:
    """Say whether `key`, x-only or SEC, is a point of secp256k1."""
    try:
        point_from_bip340pub_key_var(key)
    except EccException:
        return False
    return True


def _script_function(calls: list[str]) -> tuple[str, int]:
    """Return the context and the depth of the function holding a key.

    `calls` are the functions open around the key, outermost first.
    `sh()`, `wsh()` and the tree of `tr()` hold the function, which is
    `calls[depth]`.
    """
    ctx, depth = _TOP, 0
    while calls[depth] in {"sh", "wsh"} or (
        calls[depth] == "tr" and depth < len(calls) - 1
    ):
        ctx = _INNER[calls[depth]]
        depth += 1
    return ctx, depth


def _reads_own_keys(name: str, ctx: str) -> bool:
    """Say whether `ParseScript` reads the keys of `name` in `ctx`.

    Miniscript reads them where it does not.
    """
    # `pkh()` in a tree is miniscript's
    return name in _MULTI_FUNCTIONS | {"unused"} or (
        name in _KEY_FUNCTIONS and (name != "pkh" or ctx != _P2TR)
    )


def _key_prefix(calls: list[str]) -> str:
    """Return what `ParseScript` writes before the message of a key.

    `calls` are the functions open around the key, outermost first.
    `sh()`, `wsh()` and the tree of `tr()` pass the message on as it is.
    The function they hold then names itself, as `Multi` for one of
    `_MULTI_FUNCTIONS`, unless miniscript reads it, which names nothing.
    """
    # `ParsePubkey` writes `musig(): ` before a participant's message, and
    # the function that holds the `musig()` writes its own before that
    if calls[-1].rpartition(":")[2] == "musig":
        return f"{_key_prefix(calls[:-1])}musig(): "

    ctx, depth = _script_function(calls)
    name = calls[depth]
    if name in _MULTI_FUNCTIONS:
        return "Multi: "
    if name in _KEY_FUNCTIONS and _reads_own_keys(name, ctx):
        return f"{name}(): "
    return ""


def _key_context(calls: list[str]) -> str:
    """Return the context `ParsePubkeyInner` reads a key of `calls` in.

    `calls` are the functions open around the key, its holder last.
    Miniscript reads its keys in `P2WSH` inside `wsh()` and in `P2TR`
    elsewhere, `sh()` and the top level included (`KeyParser`).
    """
    if calls[-1].rpartition(":")[2] == "musig":
        return _MUSIG
    ctx, depth = _script_function(calls)
    name = calls[depth]
    if name == "wpkh":
        return _P2WPKH
    if name in {"tr", "rawtr"}:
        return _P2TR
    if _reads_own_keys(name, ctx):
        return ctx
    return _P2WSH if ctx == _P2WSH else _P2TR


def _is_key_position(calls: list[str], commas: list[int]) -> bool:
    """Say whether the argument `calls` and `commas` stand at is a key.

    `commas` are as `_invalid_key_refusal` counts them. A function of
    `_MULTI_FUNCTIONS` reads its keys after the threshold, `musig()` reads
    a key in every argument, and the rest of `_KEY_HOLDERS` in the first.
    """
    holder = calls[-1].rpartition(":")[2]
    if holder == "musig":
        return True
    if holder in _MULTI_FUNCTIONS:
        return commas[-1] > 0
    return holder in _KEY_HOLDERS and not commas[-1]


def _key_item(text: str) -> str | None:
    """Return the key of `text` as `ParsePubkeyInner` splits it off.

    That is the text after the key origin, if any, up to the first `/`.
    None where Core refuses the origin, with a message this does not give.
    """
    if "]" not in text:
        return text.partition("/")[0]
    origin = _ORIGIN.match(text)
    if origin is None or "]" in text[origin.end() :] or not _fits(origin["path"]):
        return None
    return text[origin.end() :].partition("/")[0]


def _is_extended_key(key: str, network: str) -> bool:
    """Say whether `key` is an xpub or an xprv of `network`.

    `DecodeExtKey` and `DecodeExtPubKey` read the one version of each that
    the network has.
    """
    try:
        version = BIP32KeyData.b58decode(key).version
    except BTClibException:
        return False
    return version in {NETWORKS[network].bip32_pub, NETWORKS[network].bip32_prv}


def _hex_key_refusal(key: bytes, ctx: str) -> str | None:
    """Return `ParsePubkeyInner`'s message for the hex key `key`, else None."""
    # `CPubKey::IsValid` reads the size alone: such a key may be no point
    if len(key) == _SEC_KEY_BYTES and key[0] in _HYBRID_PREFIXES:
        return "Hybrid public keys are not allowed"
    if len(key) == _X_ONLY_KEY_BYTES:
        return None if ctx == _P2TR and _is_point(key) else "Pubkey '...' is invalid"
    if len(key) not in {_COMPRESSED_KEY_BYTES, _SEC_KEY_BYTES} or not _is_point(key):
        return "Pubkey '...' is invalid"
    if len(key) == _SEC_KEY_BYTES and ctx not in _UNCOMPRESSED_CONTEXTS:
        return "Uncompressed keys are not allowed"
    return None


def _key_refusal(text: str, calls: list[str], network: str) -> str | None:
    """Return `ParsePubkeyInner`'s message for the key `text`, else None.

    `text` is what Core reads as one key, a `musig()` aside. `calls` are
    the functions open around it, its holder last, and `network` the
    node's. Core reads a key of no path as hex, then as a WIF of
    `network`'s prefix alone (`DecodeSecret`), then as an extended key of
    `network`, and refuses what is none of these. Core quotes the key, or
    what it reads as one, which may be a private key given by mistake,
    where this writes `...` (SECURITY.md, "Where this node departs from
    Bitcoin Core"). "" where Core refuses the key's origin, with a message
    this does not give.
    """
    key = _key_item(text)
    if key is None:
        return ""
    if not key:
        return "No key provided"
    if key[0] in _SPACES or key[-1] in _SPACES:
        return "Key '...' is invalid due to whitespace"
    if key == text.rpartition("]")[2]:
        return _pathless_key_refusal(key, _key_context(calls), network)
    return None if _is_extended_key(key, network) else _NO_KEY


def _pathless_key_refusal(key: str, ctx: str, network: str) -> str | None:
    """Return `ParsePubkeyInner`'s message for `key`, which no path follows."""
    if _HEX.fullmatch(key):
        return _hex_key_refusal(bytes.fromhex(key), ctx)
    try:
        wif = b58.prv_key_data_from_wif(key, network)
    except BTClibException:
        return None if _is_extended_key(key, network) else _NO_KEY
    if wif.compressed or ctx in _UNCOMPRESSED_CONTEXTS:
        return None
    return "Uncompressed keys are not allowed"


def _opened_refusal(name: str, calls: list[str], commas: list[int]) -> str | None:
    """Return what Core refuses the function `name` opened in `calls` with.

    `_MUSIG_PLACE`, after `_key_prefix`, for a `musig()` a function reads as
    a key outside `P2TR`. "" for a function of `_DRAFT_NAMES`, and for a
    `musig()` where a script belongs, which `_core_refusal` refuses. None
    where Core reads on. `commas` are as `_invalid_key_refusal` counts them.
    """
    if name != "musig":
        return "" if name in _DRAFT_NAMES else None
    if not calls or not _is_key_position(calls, commas):
        return ""
    if _key_context(calls) == _P2TR:
        return None
    return f"{_key_prefix(calls)}{_MUSIG_PLACE}"


@dataclass
class _KeyWalk:
    """What `_invalid_key_refusal` has read of `text` so far.

    Each method reads one part of `text` and answers Core's message where
    Core refuses it, "" where the walk stops with no message, and None
    where it reads on. `text` may hold a private key, and so may `calls`,
    so `repr` and `str` leave them out (section 7 of the organization's
    standard, *A refusal carries no secret*).
    """

    text: str = field(repr=False)
    network: str
    # the functions open, outermost first
    calls: list[str] = field(default_factory=list, repr=False)
    # the commas read in each open call: after the first, a `tr()` reads
    # its tree
    commas: list[int] = field(default_factory=list)
    # for each open `musig()`: whether a path follows it, and whether a
    # participant is no extended key
    musigs: list[list[bool]] = field(default_factory=list)

    def open(self, word: str, at: int) -> str | None:
        """Read the `(` at `at`, after `word`."""
        name = word.rpartition(":")[2]
        if (refusal := _opened_refusal(name, self.calls, self.commas)) is not None:
            return refusal
        if name == "musig":
            span = _expr(self.text[at - len(word) :])[0]
            # `ParsePubkey` splits the `musig()` at each `)`
            if span.count(")") > 1:
                return f"{_key_prefix(self.calls)}Too many ')' in musig() expression"
            self.musigs.append([span.partition(")")[2].startswith("/"), False])
        self.calls.append(word)
        self.commas.append(0)
        return None

    def close(self) -> str | None:
        """Read a `)`."""
        if not self.calls:
            return ""
        if self.calls[-1].rpartition(":")[2] == "musig" and all(self.musigs.pop()):
            message = "derivation requires all participants to be xpubs or xprvs"
            return f"{_key_prefix(self.calls)}{message}"
        self.calls.pop()
        self.commas.pop()
        return None

    def comma(self) -> None:
        """Read a `,`."""
        if self.commas:
            self.commas[-1] += 1

    def key(self, at: int) -> str | None:
        """Read the argument at `at`, if it is a key, a `musig()` aside.

        The key of a function of `_ONE_KEY_FUNCTIONS` runs to the `)` that
        closes the function, commas included; any other runs to a comma.
        """
        if not self.calls or not _is_key_position(self.calls, self.commas):
            return None
        if self.calls[-1].rpartition(":")[2] in _ONE_KEY_FUNCTIONS:
            span = _argument(self.text[at:])
        else:
            span = _expr(self.text[at:])[0]
        if span.startswith("musig("):
            return None
        refusal = _key_refusal(span, self.calls, self.network)
        if refusal is not None:
            return refusal and f"{_key_prefix(self.calls)}{refusal}"
        key = _key_item(span) or ""
        if self.calls[-1].rpartition(":")[2] == "musig" and not _is_extended_key(
            key, self.network
        ):
            self.musigs[-1][1] = True
        return None


def _invalid_key_refusal(text: str, network: str) -> str | None:
    """Return Core's message for the first key or `musig()` it refuses.

    Core refuses a key as `_key_refusal` says, a `musig()` outside `P2TR`,
    one whose text holds a `)` after the one closing it, and one followed
    by a path whose participants are not all extended keys. ecc refuses a
    hex key that is no point with `EccException`, and bip32_descriptors
    refuses the rest with `BTClibValueError`, each in words of its own.
    Core's message is `ParsePubkeyInner`'s for a key and `ParsePubkey`'s
    for a `musig()`, and `ParseScript` puts `_key_prefix` before it, all in
    `src/script/descriptor.cpp` at bitcoin/bitcoin@f0de87f040, the v32.1
    tag.

    Core reads `text` in order and refuses the first thing it cannot read,
    a `musig()` before the keys inside it. So this answers None where it
    first meets a function of `_DRAFT_NAMES`, a `musig()` where a script
    belongs or a key origin Core refuses, as it does where `text` holds no
    key or `musig()` Core refuses.
    """
    walk = _KeyWalk(text, network)
    word = ""
    at = 0
    for part in _DELIMITER.split(text):
        refusal = None
        if part == "(":
            refusal = walk.open(word, at)
        elif part == ")":
            refusal = walk.close()
        elif part == ",":
            walk.comma()
        elif word in {"(", ","}:
            refusal = walk.key(at)
        if refusal is not None:
            return refusal or None
        word = part
        at += len(part)
    return None


def _parse(node: Node, text: str) -> list[_Source]:
    """Parse `text`, which is one descriptor or a multipath one's several.

    Core's `Parse`. Its message names what it found wrong and is
    bip32_descriptors' here, which words it differently, except for what
    `_core_refusal` refuses, whose message is Core's, shortened where Core
    quotes the expression. Where ecc refuses a hex key as no point, or
    bip32_descriptors refuses as `_BTCLIB_KEY_REFUSALS` says, the message
    is that of whichever of `_invalid_key_refusal` and `_core_refusal` Core
    meets first in `text`. Where bip32_descriptors refuses an expression no
    function reads, the message is `_core_refusal`'s where it has one:
    `_invalid_key_refusal` reads on past such an expression, to keys Core
    does not reach. bip32_descriptors checks a key's network only once it
    has read the whole descriptor, so a later refusal it raises comes
    first, where Core refuses the key. Unless `_invalid_key_refusal` or
    `_tree_refusal` reads that key, the message is then bip32_descriptors'
    or `_core_refusal`'s, not Core's (btclib-org/btclib-node#2555).
    """
    prv_keys: dict[str, str] = {}
    stripped: str | None = None
    try:
        stripped = strip_checksum(text)
        sources = [
            _Source(parse_descriptor(one, node.chain.name, prv_keys), prv_keys)
            for one in multipath_descriptors(stripped)
        ]
    except (BTClibException, EccException) as err:
        message = str(err)
        network = node.chain.name
        if stripped is not None and _refuses_key(err):
            message = (
                _invalid_key_refusal(stripped, network)
                or _core_refusal(stripped, _TOP, network)
                or message
            )
        elif stripped is not None and message.endswith(_NOT_A_FUNCTION):
            message = _core_refusal(stripped, _TOP, network) or message
        raise RpcError(RPCErrorCode.INVALID_ADDRESS_OR_KEY, message) from err
    if refusal := _core_refusal(stripped, _TOP):
        raise RpcError(RPCErrorCode.INVALID_ADDRESS_OR_KEY, refusal)
    return sources


def _scan_object(node: Node, scan_object: object) -> tuple[list[_Source], int, int]:
    """Return a scan object's descriptors and expansion indexes.

    Core's `EvalDescriptorStringOrObject`.
    """
    low, high = 0, _DEFAULT_RANGE_END
    if isinstance(scan_object, str):
        text = scan_object
    elif isinstance(scan_object, dict):
        desc = scan_object.get("desc")
        if desc is None:
            raise RpcError(
                RPCErrorCode.INVALID_PARAMETER,
                "Descriptor needs to be provided in scan object",
            )
        if not isinstance(desc, str):
            raise RpcError(
                RPCErrorCode.TYPE_ERROR,
                f"JSON value of type {json_type_name(desc)} "
                "is not of expected type string",
            )
        text = desc
        if scan_object.get("range") is not None:
            low, high = _range(scan_object["range"])
    else:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            "Scan object needs to be either a string or an object",
        )
    sources = _parse(node, text)
    if not sources[0].descriptor.is_ranged:
        low = high = 0
    return sources, low, high


def scan_tx_out_set(
    node: Node, conn: RpcConnection, params: list[Any]
) -> dict[str, Any] | bool | Generator[bool, None, dict[str, Any]] | None:
    """Answer `scantxoutset`: report on, stop, or run a scan of the UTXO set.

    `start` searches the stored coins for the scripts of the scan objects'
    descriptors, a ranged one expanded over its `range` and `combo` over
    its scripts, and answers each coin found with the descriptor that
    describes its script. A coin is listed once, as Core's `std::map` has
    it, and is found in the set as it was when the scan began: the
    chainstate is flushed and a cursor opened, whose view RocksDB fixes
    (`UtxoIndex.cursor`). `success` is false for a scan that was aborted
    or met a record it cannot read, and the rest of the answer is what was
    found by then.

    A coin's `desc` is Core's `InferDescriptor` of its script, read with
    what the scan object's descriptors tell a provider at each index of
    its range. Core infers every script before the walk; here only a
    script a coin has is inferred, which answers the same.

    A descriptor parse error has Core's code; `_parse` says whose message.
    """
    if not params:
        raise RpcError(RPCErrorCode.MISC_ERROR, HELP_TEXT["scantxoutset"])
    action = params[0]
    scan_objects = params[1] if len(params) > 1 else None
    mismatches: list[tuple[int, str, object, str]] = []
    if not isinstance(action, str):
        mismatches.append((1, "action", action, "string"))
    if scan_objects is not None and not isinstance(scan_objects, list):
        mismatches.append((2, "scanobjects", scan_objects, "array"))
    if mismatches:
        raise type_errors(*mismatches)

    scan = _scans.get(node)
    if action == "status":
        return None if scan is None else {"progress": scan.progress}
    if action == "abort":
        if scan is None:
            return False
        scan.abort = True
        return True
    if action == "start":
        return _start(node, params)
    # Core quotes the action whole (`src/rpc/blockchain.cpp:2508`, at
    # bitcoin/bitcoin@f0de87f040, the v32.1 tag); this quotes it through
    # `quoted_name` (SECURITY.md, "Where this node departs from Bitcoin
    # Core"; section 7 of the organization's standard, *A refusal carries
    # no secret*)
    raise RpcError(
        RPCErrorCode.INVALID_PARAMETER, f"Invalid action '{quoted_name(action)}'"
    )


def _start(node: Node, params: list[Any]) -> Generator[bool, None, dict[str, Any]]:
    """Run `scantxoutset`'s `start`, `_Scan` held for as long as it runs."""
    if node in _scans:
        raise RpcError(
            RPCErrorCode.INVALID_PARAMETER,
            'Scan already in progress, use action "abort" or "status"',
        )
    scan = _scans[node] = _Scan()
    try:
        return (yield from _scan(node, scan, params))
    finally:
        del _scans[node]


def _interruption_point(node: Node) -> None:
    """Raise where the node is stopping, as Core's interruption point does."""
    if node.terminate_flag.is_set():
        raise RpcError(RPCErrorCode.CLIENT_NOT_CONNECTED, "Shutting down")


class _Merged:
    """Providers merged in the order they are added, the first entry winning.

    Core's `FlatSigningProvider::Merge` into one provider. `Provider.merged`
    copies both sides, so merging each into the whole is quadratic in the
    range; merging equal sizes, as a binary counter carries, is not.

    Both methods yield after a merge of `_MERGE_STEP` providers or more.
    One merge cannot be split, and the last ones copy every key of the
    range, so the longest step grows with the range.
    """

    def __init__(self, node: Node) -> None:
        self._node = node
        self._stack: list[tuple[int, Provider]] = []

    def _merged(
        self, earlier: Provider, later: Provider, size: int
    ) -> Generator[bool, None, Provider]:
        merged = earlier.merged(later)
        if size >= _MERGE_STEP:
            yield True
            _interruption_point(self._node)
        return merged

    def add(self, provider: Provider) -> Generator[bool]:
        size = 1
        while self._stack and self._stack[-1][0] == size:
            earlier_size, earlier = self._stack.pop()
            size += earlier_size
            provider = yield from self._merged(earlier, provider, size)
        self._stack.append((size, provider))

    def provider(self) -> Generator[bool, None, Provider]:
        if not self._stack:
            return Provider()
        size, merged = self._stack[0]
        for later_size, later in self._stack[1:]:
            size += later_size
            merged = yield from self._merged(merged, later, size)
        return merged


def _expand(
    node: Node, scan: _Scan, scan_objects: list[Any]
) -> Generator[bool, None, tuple[dict[bytes, int], list[Provider]]]:
    """Return the scripts the scan objects describe, and their providers.

    Each script maps to the first scan object describing it, as Core's
    `descriptors.emplace` keeps the first, and that scan object's provider
    is the one its `desc` is inferred with.
    """
    needles: dict[bytes, int] = {}
    providers: list[Provider] = []
    keys = 0
    for scan_object in scan_objects:
        sources, low, high = _scan_object(node, scan_object)
        merged = _Merged(node)
        for index in range(low, high + 1):
            for source in sources:
                try:
                    scripts, provider = source.descriptor.expand(index, source.prv_keys)
                except BTClibException as err:
                    # Core's rpc/util.cpp:1376 (at bitcoin/bitcoin@f0de87f040,
                    # the v32.1 tag) quotes the descriptor. This tree departs on
                    # purpose: the descriptor may hold private keys
                    # (SECURITY.md, "Where this node departs from Bitcoin
                    # Core"). Core's rpc/mining.cpp:231 and
                    # rpc/output_script.cpp:241 quote nothing.
                    raise RpcError(
                        RPCErrorCode.INVALID_ADDRESS_OR_KEY,
                        "Cannot derive script without private keys",
                    ) from err
                yield from merged.add(provider)
                for script in scripts:
                    needles.setdefault(script.script, len(providers))
                keys += max(1, len(source.descriptor.key_expressions))
            if keys >= _EXPAND_STEP:
                keys = 0
                yield True
                _interruption_point(node)
        providers.append((yield from merged.provider()))
    return needles, providers


def _walk(
    node: Node,
    scan: _Scan,
    cursor: Iterator[tuple[bytes, bytes]],
    needles: dict[bytes, int],
    found: dict[tuple[bytes, int], Coin],
) -> Generator[bool, None, tuple[bool, int]]:
    """Read `cursor`'s coins into `found` where a script is `needles`'.

    Return whether the walk was whole, and the coins read: Core's
    `FindScriptPubKey`.
    """
    prefix = len(b"utxo-")
    count = 0
    for key, value in cursor:
        try:
            coin = Coin.parse(value, check_validity=False)
        except BTClibValueError:
            return False, count
        count += 1
        if count % _STEP == 0:
            yield True
            _interruption_point(node)
            if scan.abort:
                return False, count
        if count % _PROGRESS_EVERY == 0:
            scan.progress = int(
                (0x100 * key[prefix] + key[prefix + 1]) * 100.0 / 65536.0 + 0.5
            )
        if coin.tx_out.script_pub_key.script in needles:
            out_point = key[prefix:]
            found[out_point[:32], int.from_bytes(out_point[32:], "little")] = coin
    scan.progress = 100
    return True, count


def _scan(
    node: Node, scan: _Scan, params: list[Any]
) -> Generator[bool, None, dict[str, Any]]:
    """Expand the scan objects, walk the coins and answer, as `start` does."""
    # Core's `MaybeArg` answers a null as it answers an argument not given
    if len(params) < 2 or params[1] is None:  # noqa: PLR2004
        raise RpcError(
            RPCErrorCode.MISC_ERROR,
            "scanobjects argument is required for the start action",
        )
    needles, providers = yield from _expand(node, scan, params[1])
    # `g_should_abort_scan = false`: an abort during the expansion is lost
    scan.abort = False
    node.chainstate.flush()
    hashes = list(node.chainstate.block_index.active_chain)
    cursor = node.chainstate.utxo_index.cursor()
    found: dict[tuple[bytes, int], Coin] = {}
    walked = yield from _walk(node, scan, cursor, needles, found)
    success, count = walked
    tip_height = len(hashes) - 1
    unspents: list[dict[str, Any]] = []
    total = 0
    # `std::map<COutPoint, Coin>`: by txid as stored, then by index
    for (txid, vout), coin in sorted(found.items(), key=lambda item: item[0]):
        script = coin.tx_out.script_pub_key.script
        total += coin.tx_out.value
        unspents.append(
            {
                "txid": txid[::-1],
                "vout": vout,
                "scriptPubKey": script,
                "desc": infer_descriptor(
                    script, providers[needles[script]], node.chain.name
                ),
                "amount": btc_amount(coin.tx_out.value),
                "coinbase": coin.is_coinbase,
                "height": coin.height,
                "blockhash": hashes[coin.height],
                "confirmations": tip_height - coin.height + 1,
            }
        )
        if len(unspents) % _STEP == 0:
            yield True
    return {
        "success": success,
        "txouts": count,
        "height": tip_height,
        "bestblock": hashes[-1],
        "unspents": unspents,
        "total_amount": btc_amount(total),
    }
