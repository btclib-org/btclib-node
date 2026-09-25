# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What `rpc.auth.RpcAuth` accepts, and the cookie it writes and deletes."""

import base64
import hmac
import os
import re
import stat
from typing import TYPE_CHECKING

import pytest

from btclib_node.rpc.auth import (
    COOKIE_FILE,
    COOKIE_USER,
    RpcAuth,
    RpcAuthEntry,
    password_hmac,
)
from tests import RPCAUTH

if TYPE_CHECKING:
    from pathlib import Path


def basic(userpass: bytes) -> str:
    """Return the `Authorization` value HTTP Basic makes of `userpass`."""
    return "Basic " + base64.b64encode(userpass).decode()


def test_password_hmac_is_what_cores_rpcauth_script_writes() -> None:
    """HMAC-SHA256 keyed by the salt's hex text, as lowercase hex.

    The expected value is `password_to_hmac("00" * 16, "pytest")` of
    `share/rpcauth/rpcauth.py`, run at
    bitcoin/bitcoin@9be056a8a7 -- v31.1, the release
    `integration-bitcoind.yml` pins.
    """
    expected = b"0902338bb5203e7438a3b150acc15cfb8aea3f8881b65ebf6e2fd36cb5a53529"
    assert password_hmac(b"00" * 16, b"pytest") == expected


def test_parse_splits_user_salt_and_hmac() -> None:
    """`<user>:<salt>$<hmac>`, each field kept as written."""
    assert RpcAuthEntry.parse("alice:5a1t$abcd") == RpcAuthEntry(
        b"alice", b"5a1t", b"abcd"
    )


@pytest.mark.parametrize(
    "value",
    ["", "alice", "alice:salt", "alice:salt$hmac$more", "a:b:salt$hmac"],
)
def test_parse_refuses_what_core_refuses(value: str) -> None:
    """Not two `:` fields, or then not two `$` fields: fatal, as in Core."""
    with pytest.raises(ValueError, match=r"^Invalid -rpcauth argument\.$"):
        RpcAuthEntry.parse(value)


def test_a_right_password_is_accepted() -> None:
    """The user and password `RPCAUTH` was made from."""
    auth = RpcAuth((RpcAuthEntry.parse(RPCAUTH),))
    assert auth.authorized(basic(b"pytest:pytest"))


def test_a_password_holding_a_colon_is_split_at_the_first_one() -> None:
    """Split at the first `:`, as Core splits, so a password may hold one."""
    auth = RpcAuth((RpcAuthEntry.from_password("pytest", "pass:word"),))
    assert auth.authorized(basic(b"pytest:pass:word"))


@pytest.mark.parametrize(
    "authorization",
    [
        basic(b"pytest:wrong"),
        basic(b"someone:pytest"),
        basic(b"pytest"),
        "Bearer " + base64.b64encode(b"pytest:pytest").decode(),
        "Basic not*base64",
        "Basic é",
        "Basic \xa0" + base64.b64encode(b"pytest:pytest").decode(),
        "",
    ],
    ids=[
        "wrong password",
        "wrong user",
        "no colon",
        "not basic",
        "not base64",
        "not ascii",
        "no-break space before a right credential",
        "empty",
    ],
)
def test_anything_else_is_refused(authorization: str) -> None:
    """A wrong credential and a header of the wrong shape are both refused."""
    auth = RpcAuth((RpcAuthEntry.parse(RPCAUTH),))
    assert not auth.authorized(authorization)


def test_the_whitespace_core_trims_is_trimmed() -> None:
    """Core's `TrimStringView` set, around a right credential, is accepted."""
    auth = RpcAuth((RpcAuthEntry.parse(RPCAUTH),))
    encoded = base64.b64encode(b"pytest:pytest").decode()
    assert auth.authorized("Basic  \f\t\v" + encoded + "\r\n ")


def test_no_entry_accepts_nobody() -> None:
    """With no `-rpcauth` and no cookie yet, nothing is accepted."""
    assert not RpcAuth().authorized(basic(b"pytest:pytest"))


def test_every_comparison_is_hmac_compare_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user and the HMAC are both compared by `hmac.compare_digest`.

    Core's `TimingResistantEqual`. Wrapping `hmac.compare_digest`, which
    `rpc.auth` calls through the `hmac` module, records what reached it:
    a wrong user stops at the user's comparison, a right one goes on to
    the HMAC's, and a right password is accepted only by that second
    call.
    """
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    entry = RpcAuthEntry.parse(RPCAUTH)
    auth = RpcAuth((entry,))

    assert not auth.authorized(basic(b"someone:pytest"))
    assert calls == [(b"pytest", b"someone")]

    calls.clear()
    assert auth.authorized(basic(b"pytest:pytest"))
    assert calls == [(b"pytest", b"pytest"), (entry.hmac, entry.hmac)]

    calls.clear()
    assert not auth.authorized(basic(b"pytest:wrong"))
    assert calls == [
        (b"pytest", b"pytest"),
        (password_hmac(entry.salt, b"wrong"), entry.hmac),
    ]


def test_generate_cookie_writes_a_cookie_its_password_opens(tmp_path: Path) -> None:
    """`__cookie__:` and 64 hex digits in `.cookie`, its password accepted.

    What is kept is the password's salted HMAC and not the password,
    as Core hashes a generated one before storage.
    """
    auth = RpcAuth()
    path = auth.generate_cookie(tmp_path)
    assert path == tmp_path / COOKIE_FILE
    assert auth.cookie_path == path
    cookie = path.read_text(encoding="ascii")
    assert re.fullmatch(COOKIE_USER + ":[0-9a-f]{64}", cookie)
    assert not (tmp_path / (COOKIE_FILE + ".tmp")).exists()
    assert auth.authorized(basic(cookie.encode()))
    password = cookie.partition(":")[2].encode()
    assert all(password not in entry.hmac for entry in auth.entries)


def test_a_cookie_is_added_beside_every_rpcauth_user(tmp_path: Path) -> None:
    """Core writes the cookie whether or not `-rpcauth` is set."""
    auth = RpcAuth((RpcAuthEntry.parse(RPCAUTH),))
    cookie = auth.generate_cookie(tmp_path).read_bytes()
    assert auth.authorized(basic(cookie))
    assert auth.authorized(basic(b"pytest:pytest"))


def test_a_fresh_cookie_replaces_the_last_one(tmp_path: Path) -> None:
    """A second node start on one data directory writes a new password."""
    first = RpcAuth()
    old = first.generate_cookie(tmp_path).read_bytes()
    second = RpcAuth()
    new = second.generate_cookie(tmp_path).read_bytes()
    assert old != new
    assert not second.authorized(basic(old))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_cookie_is_mode_0600_on_posix(tmp_path: Path) -> None:
    """Mode 0600, Core's own default, even over a leftover wider `.tmp`.

    Under umask 0, so the mode is the one `generate_cookie` asks for:
    a restrictive umask would mask a wider one down to 0600 as well.
    """
    leftover = tmp_path / (COOKIE_FILE + ".tmp")
    leftover.write_text("stale", encoding="ascii")
    leftover.chmod(0o644)
    previous = os.umask(0)
    try:
        path = RpcAuth().generate_cookie(tmp_path)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_generate_cookie_raises_where_it_cannot_write(tmp_path: Path) -> None:
    """A data directory that is not there raises, and leaves no cookie."""
    auth = RpcAuth()
    with pytest.raises(FileNotFoundError):
        auth.generate_cookie(tmp_path / "missing")
    assert auth.cookie_path is None
    assert auth.entries == []


def test_delete_cookie_removes_what_generate_cookie_wrote(tmp_path: Path) -> None:
    """And a second call, or a cookie already gone, is not an error."""
    auth = RpcAuth()
    path = auth.generate_cookie(tmp_path)
    auth.delete_cookie()
    assert not path.exists()
    assert auth.cookie_path is None
    auth.delete_cookie()

    path = auth.generate_cookie(tmp_path)
    path.unlink()
    auth.delete_cookie()


def test_delete_cookie_leaves_a_cookie_it_did_not_write(tmp_path: Path) -> None:
    """Core's `g_generated_cookie`: only a cookie this wrote is removed."""
    other = tmp_path / COOKIE_FILE
    other.write_text(COOKIE_USER + ":" + "0" * 64, encoding="ascii")
    RpcAuth().delete_cookie()
    assert other.exists()
