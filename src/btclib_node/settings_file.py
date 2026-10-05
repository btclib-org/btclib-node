# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

r"""`settings.json`: Core's read-write settings file, and the JSON it holds.

`common::ReadSettings` and `common::WriteSettings` (`src/common/settings.cpp`)
and `ArgsManager::WriteSettingsFile` (`src/common/args.cpp`), all at
bitcoin/bitcoin@9be056a8a7, the v31.1 tag. The file is one JSON object, read
at every start and written back whole, under a `_warning_` key Core adds and
drops again on the next read.

The JSON is Core's UniValue (`src/univalue/lib/`, same sha), ported rather
than read through `json` with hooks, for exactness: UniValue keeps a first
surrogate half pending across an ASCII character or an escape
(`"\ud800x\udc00"` is valid) and lexes a lone `-` as a number, which `json`
cannot be made to do. Matching Core is the rule here (*Following Bitcoin
Core*, `CONTRIBUTING.md`).

The file is read as bytes and a string's UTF-8 is read by UniValue's own
`JSONUTF8StringFilter`, so an overlong form (`C0 80` is U+0000) and a
surrogate pair written as two UTF-8 sequences (`ED A0 BD ED B8 80` is
U+1F600) are accepted, as Core accepts them. One thing differs: the filter
also accepts a code point above U+10FFFF, which a `str` cannot hold, and
that file is refused as not valid JSON.

The file is written in text mode, as Core's `std::ofstream` is, so its line
ends are `os.linesep`.

Writing goes through `<file>.tmp` and a rename over the file, as Core does,
so a crash leaves the old file whole.
"""

import os
import re
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from btclib_node.constants import CLIENT_NAME

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "SETTINGS_FILENAME",
    "Json",
    "Number",
    "Object",
    "read_settings",
    "type_name",
    "write_json",
    "write_settings",
]

# `BITCOIN_SETTINGS_FILENAME` (`src/common/args.cpp`)
SETTINGS_FILENAME = "settings.json"

# `SETTINGS_WARN_MSG_KEY` (`src/common/settings.cpp`)
_WARNING_KEY = "_warning_"

# `MAX_JSON_DEPTH` (`src/univalue/lib/univalue_read.cpp`): the most arrays
# and objects open at once
_MAX_DEPTH = 512

_WHITESPACE = " \t\n\r"
_DIGITS = "0123456789"
_HEX4 = re.compile("[0-9a-fA-F]{4}")
_ESCAPED = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n"}
_ESCAPED |= {"r": "\r", "t": "\t"}

# `escapes` (`src/univalue/include/univalue_escapes.h`): a control character
# is `\u00xx` but for the five with a letter, and `"`, `\` and DEL are
# escaped; every other character is written as it is
_ESCAPES = {code: f"\\u{code:04x}" for code in [*range(32), 127]}
_ESCAPES |= {8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r"}
_ESCAPES |= {ord('"'): '\\"', ord("\\"): "\\\\"}


class Number(str):
    """A JSON number, as the text it was written in: `1.50` stays `1.50`.

    UniValue's `VNUM` holds the text, and writes it back unchanged.
    """

    __slots__ = ()


@dataclass(frozen=True)
class Object:
    """A JSON object: every key and value in file order, a repeated key too."""

    pairs: tuple[tuple[str, Json], ...]


type Json = str | bool | Number | list[Json] | Object | None


class _InvalidJsonError(ValueError):
    """The text is not what UniValue's `read` accepts."""


def type_name(value: Json) -> str:
    """Return UniValue's `uvTypeName` of `value`."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, Number):
        return "number"
    if isinstance(value, str):
        return "string"
    return "object" if isinstance(value, Object) else "array"


def _is_digit(text: str, index: int) -> bool:
    return index < len(text) and text[index] in _DIGITS


def _digits(text: str, end: int) -> int:
    """Return where the digits starting at `end` stop."""
    while _is_digit(text, end):
        end += 1
    return end


def _number(text: str, start: int) -> tuple[Number, int]:
    """Return the number at `start` and where it ends, as UniValue lexes it.

    A `-` ending the whole text is a number, `-`: UniValue refuses a sign
    only where a character follows it that is not a digit.
    """
    sign = text[start] == "-"
    if text[start + sign : start + sign + 1] == "0" and _is_digit(
        text, start + sign + 1
    ):
        raise _InvalidJsonError
    end = start + 1
    if sign and end < len(text) and not _is_digit(text, end):
        raise _InvalidJsonError
    end = _digits(text, end)
    if text[end : end + 1] == ".":
        end += 1
        if not _is_digit(text, end):
            raise _InvalidJsonError
        end = _digits(text, end)
    if text[end : end + 1] in {"e", "E"}:
        end += 1
        if text[end : end + 1] in {"+", "-"}:
            end += 1
        if not _is_digit(text, end):
            raise _InvalidJsonError
        end = _digits(text, end)
    return Number(text[start:end]), end


# a byte inside a UTF-8 sequence is `10xxxxxx`; the leading bytes, in turn,
# are each: the first byte below it, the payload mask, and the bits to come
_MID_MASK = 0xC0
_MID_TAG = 0x80
_LEADS = ((0xE0, 0x1F, 6), (0xF0, 0x0F, 12), (0xF8, 0x07, 18))

# the UTF-16 surrogate ranges: first halves, then second halves
_FIRST_HALVES = range(0xD800, 0xDC00)
_SECOND_HALVES = range(0xDC00, 0xE000)


class _Chars:
    r"""A string's characters, `JSONUTF8StringFilter`'s decoding of UTF-8.

    A first surrogate half is kept until a second half arrives. A plain ASCII
    byte is written at once, even while one is kept, as the filter writes it:
    `"\\ud800x\\udc00"` is `x` and one character. UTF-8 is decoded without
    the checks a decoder makes, as the filter does: `C0 80` is U+0000, and a
    surrogate written in UTF-8 is a half like a `\\u` escape.
    """

    def __init__(self) -> None:
        self.chars: list[str] = []
        self.first_half = 0
        self.codepoint = 0
        self.state = 0  # the top bit the next UTF-8 byte fills, or 0

    def byte(self, ch: int) -> None:
        """Take one byte, `push_back`."""
        if self.state:
            if ch & _MID_MASK != _MID_TAG:
                raise _InvalidJsonError
            self.state -= 6
            self.codepoint |= (ch & 0x3F) << self.state
            if self.state == 0:
                self.code_point(self.codepoint)
        elif ch < _MID_TAG:
            self.chars.append(chr(ch))
        else:
            for below, mask, state in _LEADS:
                if _MID_MASK <= ch < below:
                    self.codepoint = (ch & mask) << state
                    self.state = state
                    return
            raise _InvalidJsonError

    def code_point(self, code: int) -> None:
        """Take a code point, `push_back_u`."""
        if self.state:
            raise _InvalidJsonError
        if code in _FIRST_HALVES:
            if self.first_half:
                raise _InvalidJsonError
            self.first_half = code
        elif code in _SECOND_HALVES:
            if not self.first_half:
                raise _InvalidJsonError
            self.chars.append(
                chr(
                    0x10000
                    | ((self.first_half - _FIRST_HALVES.start) << 10)
                    | (code - _SECOND_HALVES.start)
                )
            )
            self.first_half = 0
        elif self.first_half or code > sys.maxunicode:
            raise _InvalidJsonError
        else:
            self.chars.append(chr(code))

    def finalize(self) -> None:
        """Refuse a string that ends inside a sequence or a pair."""
        if self.state or self.first_half:
            raise _InvalidJsonError


def _string(text: str, start: int) -> tuple[str, int]:
    """Return the string whose opening quote is before `start`, and its end.

    `text` holds one character to the byte, as `read_settings` decodes it.
    """
    chars = _Chars()
    index = start
    while True:
        if index >= len(text) or text[index] < " ":
            raise _InvalidJsonError
        char = text[index]
        index += 1
        if char == '"':
            break
        if char == "\\":
            escape = text[index : index + 1]
            if escape == "u":
                if not _HEX4.fullmatch(text, index + 1, index + 5):
                    raise _InvalidJsonError
                chars.code_point(int(text[index + 1 : index + 5], 16))
                index += 5
            elif escape in _ESCAPED:
                chars.byte(ord(_ESCAPED[escape]))
                index += 1
            else:
                raise _InvalidJsonError
        else:
            chars.byte(ord(char))
    chars.finalize()
    return "".join(chars.chars), index


# what `_token` returns for the end of the text
_END = "end"
# what it returns for a string, and for a number, `true`, `false` or `null`
_STRING = "string"
_SCALAR = "scalar"


def _token(text: str, pos: int) -> tuple[str, Json, int]:
    """Return the token at `pos`: its kind, its value and where it ends."""
    while pos < len(text) and text[pos] in _WHITESPACE:
        pos += 1
    if pos >= len(text):
        return _END, None, pos
    char = text[pos]
    if char in "{}[]:,":
        return char, None, pos + 1
    if char == '"':
        value, end = _string(text, pos + 1)
        return _STRING, value, end
    if char == "-" or char in _DIGITS:
        number, end = _number(text, pos)
        return _SCALAR, number, end
    for word, scalar in (("null", None), ("true", True), ("false", False)):
        if text.startswith(word, pos):
            return _SCALAR, scalar, pos + len(word)
    raise _InvalidJsonError


# what the next token may be, in `_parse`
_VALUE = "a value"
_ELEMENT = "an element, or the end of the array"
_NAME = "a name"
_NAME_OR_END = "a name, or the end of the object"
_COLON = "a colon"
_AFTER = "a comma, or the end of the array or object"


@dataclass
class _Open:
    """An array or object being read."""

    is_object: bool
    values: list[Json] = field(default_factory=list)
    pairs: list[tuple[str, Json]] = field(default_factory=list)
    name: str = ""

    def add(self, value: Json) -> None:
        if self.is_object:
            self.pairs.append((self.name, value))
        else:
            self.values.append(value)

    def close(self) -> Json:
        return Object(tuple(self.pairs)) if self.is_object else self.values


def _parse(text: str) -> Json:  # noqa: C901, PLR0912 -- the one state machine
    """Return the JSON value `text` holds, as UniValue's `read` reads it.

    Raises `_InvalidJsonError` where UniValue's `read` returns `false`.
    The first of `stack` is the root, holding the one value `text` is.
    """
    stack = [_Open(is_object=False)]
    expect = _VALUE
    pos = 0
    while True:
        kind, value, pos = _token(text, pos)
        if expect == _AFTER and len(stack) == 1:
            if kind == _END:
                return stack[0].values[0]
            raise _InvalidJsonError
        if kind == _END:
            raise _InvalidJsonError
        if expect == _COLON:
            if kind != ":":
                raise _InvalidJsonError
            expect = _VALUE
            continue
        if expect in {_NAME, _NAME_OR_END}:
            if kind == _STRING:
                stack[-1].name = str(value)
                expect = _COLON
                continue
            if kind != "}" or expect == _NAME:
                raise _InvalidJsonError
        elif expect == _AFTER:
            if kind == ",":
                expect = _NAME if stack[-1].is_object else _VALUE
                continue
            if kind != ("}" if stack[-1].is_object else "]"):
                raise _InvalidJsonError
        elif kind == "]" and expect != _ELEMENT:
            raise _InvalidJsonError
        elif kind in {"{", "["}:
            stack.append(_Open(is_object=kind == "{"))
            if len(stack) > _MAX_DEPTH + 1:
                raise _InvalidJsonError
            expect = _NAME_OR_END if kind == "{" else _ELEMENT
            continue
        elif kind in {_STRING, _SCALAR}:
            stack[-1].add(value)
            expect = _AFTER
            continue
        elif kind != "]":
            raise _InvalidJsonError
        # a closer: the one `expect` allowed
        closed = stack.pop().close()
        stack[-1].add(closed)
        expect = _AFTER


def _escape(text: str) -> str:
    return text.translate(_ESCAPES)


def _write(value: Json, indent: int, level: int, out: list[str]) -> None:
    """Append `value` to `out`: `UniValue::write` with `prettyIndent=indent`."""
    if value is None:
        out.append("null")
    elif isinstance(value, bool):
        out.append("true" if value else "false")
    elif isinstance(value, Number):
        out.append(str(value))
    elif isinstance(value, str):
        out.append(f'"{_escape(value)}"')
    else:
        space = " " if indent else ""
        if isinstance(value, Object):
            opener, closer = "{", "}"
            items = [(f'"{_escape(key)}":{space}', item) for key, item in value.pairs]
        else:
            opener, closer = "[", "]"
            items = [("", item) for item in value]
        newline = "\n" if indent else ""
        out.append(opener + newline)
        for index, (prefix, item) in enumerate(items):
            out.append(" " * (indent * level) + prefix)
            _write(item, indent, level + 1, out)
            out.append(("," if index < len(items) - 1 else "") + newline)
        out.append(" " * (indent * (level - 1)) + closer)


def write_json(value: Json, *, indent: int = 0) -> str:
    """Return `value` as UniValue's `write` writes it.

    Compact where `indent` is `0`. Otherwise one element or member to a line,
    `indent` spaces to a level, the whole at one level in already: an empty
    array is `[`, a line break and `]`, as Core writes it.
    """
    out: list[str] = []
    _write(value, indent, 1, out)
    return "".join(out)


def read_settings(path: str) -> dict[str, Json]:
    """Return the settings in the file at `path`; none where it is absent.

    `common::ReadSettings`. Raises `ValueError` in its words where the file
    cannot be opened, is not JSON, is not an object, or names a key twice. The
    `_warning_` key is left out, and the rest is in key order, as Core's
    `std::map` holds it.

    Core opens a directory and reads nothing from it, so a directory is not
    valid JSON; opening one fails here with `IsADirectoryError`, and with a
    `PermissionError` on Windows, where it is a refusal to open.

    Where the file's status cannot be read, for want of permission on its
    directory, `OSError` is raised, as `fs::exists` throws.
    """
    try:
        os.stat(path)  # noqa: PTH116
    except FileNotFoundError, NotADirectoryError:
        return {}
    try:
        with open(path, "rb") as file:  # noqa: PTH123
            data = file.read()
    except IsADirectoryError:
        data = b""
    except OSError:
        err_msg = f"{path}. Please check permissions."
        raise ValueError(err_msg) from None
    try:
        value = _parse(data.decode("latin-1"))
    except _InvalidJsonError:
        err_msg = (
            f"Settings file {path} does not contain valid JSON. This may be "
            "caused by a crash, power loss, full disk, or storage error, and can "
            "be fixed by removing the file, which will reset settings to default "
            "values."
        )
        raise ValueError(err_msg) from None
    if not isinstance(value, Object):
        err_msg = f"Found non-object value {write_json(value)} in settings file {path}"
        raise ValueError(err_msg)  # noqa: TRY004 -- a refusal of the file
    values: dict[str, Json] = {}
    for key, item in value.pairs:
        if key in values:
            err_msg = f"Found duplicate key {key} in settings file {path}"
            raise ValueError(err_msg)
        values[key] = item
    values.pop(_WARNING_KEY, None)
    return dict(sorted(values.items()))


def write_settings(path: str, values: Mapping[str, Json]) -> None:
    """Write `values` to the file at `path`, under the `_warning_` key.

    `ArgsManager::WriteSettingsFile` over `common::WriteSettings`: `<path>.tmp`
    is written and renamed over `path`, and a failure of either raises
    `ValueError` in Core's words, the `.tmp` left where it is. Core's
    "Unable to write" and "Unable to close" are one failure here, Python
    writing a buffer at the close.
    """
    warning = (
        f"This file is automatically generated and updated by {CLIENT_NAME}. "
        "Please do not edit this file while the node is running, as any changes "
        "might be ignored or overwritten."
    )
    document = Object(((_WARNING_KEY, warning), *values.items()))
    tmp = f"{path}.tmp"
    try:
        file = open(tmp, "w", encoding="utf-8")  # noqa: PTH123, SIM115
    except OSError:
        err_msg = f"Error: Unable to open settings file {tmp} for writing"
        raise ValueError(err_msg) from None
    try:
        with file:
            file.write(f"{write_json(document, indent=4)}\n")
    except OSError:
        err_msg = f"Error: Unable to write settings file {tmp}"
        raise ValueError(err_msg) from None
    try:
        os.replace(tmp, path)  # noqa: PTH105
    except OSError:
        err_msg = f"Failed renaming settings file {tmp} to {path}\n"
        raise ValueError(err_msg) from None
