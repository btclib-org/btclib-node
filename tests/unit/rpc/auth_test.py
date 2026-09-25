# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""What `rpc.auth.RpcAuth` accepts, and the cookie it writes and deletes.

What it lets an accepted user call is `whitelist_test.py`'s.
"""

import base64
import hmac
import os
import re
import stat
from typing import TYPE_CHECKING, cast

import pytest

from btclib_node.config import Config
from btclib_node.rpc.auth import (
    COOKIE_FILE,
    COOKIE_USER,
    RPCPASSWORD_WARNING,
    RpcAuth,
    RpcAuthEntry,
    cookie_perms,
    password_hmac,
)
from tests import RPCAUTH, RPCAUTH_PASSWORD

if TYPE_CHECKING:
    import logging
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
    assert auth.authenticated_user(basic(b"pytest:pytest")) == b"pytest"


def test_a_password_holding_a_colon_is_split_at_the_first_one() -> None:
    """Split at the first `:`, as Core splits, so a password may hold one."""
    auth = RpcAuth((RpcAuthEntry.from_password("pytest", "pass:word"),))
    assert auth.authenticated_user(basic(b"pytest:pass:word")) == b"pytest"


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
    assert auth.authenticated_user(authorization) is None


def test_the_whitespace_core_trims_is_trimmed() -> None:
    """Core's `TrimStringView` set, around a right credential, is accepted."""
    auth = RpcAuth((RpcAuthEntry.parse(RPCAUTH),))
    encoded = base64.b64encode(b"pytest:pytest").decode()
    assert auth.authenticated_user("Basic  \f\t\v" + encoded + "\r\n ") == b"pytest"


def test_no_entry_accepts_nobody() -> None:
    """With no `-rpcauth` and no cookie yet, nothing is accepted."""
    assert RpcAuth().authenticated_user(basic(b"pytest:pytest")) is None


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

    assert auth.authenticated_user(basic(b"someone:pytest")) is None
    assert calls == [(b"pytest", b"someone")]

    calls.clear()
    assert auth.authenticated_user(basic(b"pytest:pytest")) == b"pytest"
    assert calls == [(b"pytest", b"pytest"), (entry.hmac, entry.hmac)]

    calls.clear()
    assert auth.authenticated_user(basic(b"pytest:wrong")) is None
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
    path = auth.generate_cookie(tmp_path / COOKIE_FILE)
    assert path == tmp_path / COOKIE_FILE
    assert auth.cookie_path == path
    cookie = path.read_text(encoding="ascii")
    assert re.fullmatch(COOKIE_USER + ":[0-9a-f]{64}", cookie)
    assert not (tmp_path / (COOKIE_FILE + ".tmp")).exists()
    assert auth.authenticated_user(basic(cookie.encode())) == COOKIE_USER.encode()
    password = cookie.partition(":")[2].encode()
    assert all(password not in entry.hmac for entry in auth.entries)


def test_a_cookie_is_added_beside_every_rpcauth_user(tmp_path: Path) -> None:
    """Core writes the cookie whether or not `-rpcauth` is set."""
    auth = RpcAuth((RpcAuthEntry.parse(RPCAUTH),))
    cookie = auth.generate_cookie(tmp_path / COOKIE_FILE).read_bytes()
    assert auth.authenticated_user(basic(cookie)) == COOKIE_USER.encode()
    assert auth.authenticated_user(basic(b"pytest:pytest")) == b"pytest"


def test_a_fresh_cookie_replaces_the_last_one(tmp_path: Path) -> None:
    """A second node start on one data directory writes a new password."""
    first = RpcAuth()
    old = first.generate_cookie(tmp_path / COOKIE_FILE).read_bytes()
    second = RpcAuth()
    new = second.generate_cookie(tmp_path / COOKIE_FILE).read_bytes()
    assert old != new
    assert second.authenticated_user(basic(old)) is None


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
        path = RpcAuth().generate_cookie(tmp_path / COOKIE_FILE)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_generate_cookie_raises_where_it_cannot_write(tmp_path: Path) -> None:
    """A data directory that is not there raises, and leaves no cookie."""
    auth = RpcAuth()
    with pytest.raises(FileNotFoundError):
        auth.generate_cookie(tmp_path / "missing" / COOKIE_FILE)
    assert auth.cookie_path is None
    assert auth.entries == []


def test_delete_cookie_removes_what_generate_cookie_wrote(tmp_path: Path) -> None:
    """And a second call, or a cookie already gone, is not an error."""
    auth = RpcAuth()
    path = auth.generate_cookie(tmp_path / COOKIE_FILE)
    auth.delete_cookie()
    assert not path.exists()
    assert auth.cookie_path is None
    auth.delete_cookie()

    path = auth.generate_cookie(tmp_path / COOKIE_FILE)
    path.unlink()
    auth.delete_cookie()


def test_delete_cookie_leaves_a_cookie_it_did_not_write(tmp_path: Path) -> None:
    """Core's `g_generated_cookie`: only a cookie this wrote is removed."""
    other = tmp_path / COOKIE_FILE
    other.write_text(COOKIE_USER + ":" + "0" * 64, encoding="ascii")
    RpcAuth().delete_cookie()
    assert other.exists()


@pytest.mark.parametrize(
    ("value", "mode"), [("owner", 0o600), ("group", 0o640), ("all", 0o644)]
)
def test_cookie_perms_is_cores_interpret_perm_string(value: str, mode: int) -> None:
    """Owner read and write, then group read, then everybody's read."""
    assert cookie_perms(value) == mode


@pytest.mark.parametrize("value", ["", "Owner", "0644", "world"])
def test_cookie_perms_refuses_anything_else(value: str) -> None:
    """Core's own message, fatal there too."""
    err_msg = (
        f"^Invalid -rpccookieperms={value}; must be one of 'owner', 'group', "
        r"or 'all'\.$"
    )
    with pytest.raises(ValueError, match=err_msg):
        cookie_perms(value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("value", ["owner", "group", "all"])
def test_rpccookieperms_sets_the_cookie_s_mode_on_posix(
    tmp_path: Path, value: str
) -> None:
    """The mode `bitcoind` v31.1.0 leaves on its cookie for each value.

    Measured there: 0600, 0640 and 0644. Under a umask of 0o077, the one
    Core sets, so a mode wider than 0600 is the explicit one's.
    """
    previous = os.umask(0o077)
    try:
        path = RpcAuth().generate_cookie(tmp_path / COOKIE_FILE, cookie_perms(value))
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == cookie_perms(value)


def test_the_cookie_goes_where_it_is_told(tmp_path: Path) -> None:
    """`-rpccookiefile`: any name, its `.tmp` beside it, then renamed."""
    path = tmp_path / "elsewhere"
    auth = RpcAuth()
    assert auth.generate_cookie(path) == path
    assert auth.cookie_path == path
    assert not (tmp_path / "elsewhere.tmp").exists()
    assert not (tmp_path / COOKIE_FILE).exists()
    assert auth.authenticated_user(basic(path.read_bytes())) == COOKIE_USER.encode()


class Recorder:
    """A logger's `info` and `warning`, each call kept as its arguments."""

    def __init__(self) -> None:
        """Keep nothing yet."""
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def info(self, *args: object) -> None:
        """Keep an `info` call."""
        self.calls.append(("info", args))

    def warning(self, *args: object) -> None:
        """Keep a `warning` call."""
        self.calls.append(("warning", args))


def start(auth: RpcAuth) -> list[tuple[str, tuple[object, ...]]]:
    """Run `auth.start` and return what it logged."""
    recorder = Recorder()
    auth.start(cast("logging.Logger", recorder))
    return recorder.calls


def test_start_writes_the_cookie_and_logs_what_core_logs(tmp_path: Path) -> None:
    """`InitRPCAuthentication`'s lines where it writes a cookie."""
    path = tmp_path / COOKIE_FILE
    auth = RpcAuth((RpcAuthEntry.parse(RPCAUTH),), cookie_file=path)
    calls = start(auth)
    assert auth.cookie_path == path
    perms = stat.filemode(path.stat().st_mode)[1:]
    assert calls == [
        ("info", ("Generated RPC authentication cookie %s", path)),
        ("info", ("Permissions used for cookie: %s", perms)),
        ("info", ("Using random cookie authentication.",)),
        ("info", ("Using rpcauth authentication.",)),
    ]


def test_start_writes_no_cookie_where_it_is_disabled(tmp_path: Path) -> None:
    """`-norpccookiefile`: no file and no cookie user, only Core's line."""
    auth = RpcAuth()
    assert start(auth) == [
        ("info", ("RPC authentication cookie file generation is disabled.",))
    ]
    assert auth.cookie_path is None
    assert auth.entries == []
    assert list(tmp_path.iterdir()) == []


def test_rpcpassword_stops_the_cookie_and_is_warned_about(tmp_path: Path) -> None:
    """Core writes no cookie where `-rpcpassword` is set, and says why not."""
    path = tmp_path / COOKIE_FILE
    password = RpcAuthEntry.from_password("alice", "pw")
    auth = RpcAuth(password=password, cookie_file=path)
    assert start(auth) == [
        ("info", ("Using rpcuser/rpcpassword authentication.",)),
        ("warning", (RPCPASSWORD_WARNING,)),
    ]
    assert not path.exists()
    assert auth.entries == [password]
    assert auth.authenticated_user(basic(b"alice:pw")) == b"alice"


def test_rpcpassword_s_warning_is_core_s_wording() -> None:
    """`InitRPCAuthentication`'s own warning, as `bitcoind` v31.1.0 logs it."""
    assert RPCPASSWORD_WARNING == (
        "The use of rpcuser/rpcpassword is less secure, because credentials are "
        "configured in plain text. It is recommended that locally-run instances "
        "switch to cookie-based auth, or otherwise to use hashed rpcauth "
        "credentials. See share/rpcauth in the source directory for more "
        "information."
    )


def test_rpcpassword_with_no_rpcuser_is_the_empty_user() -> None:
    """Core hashes `-rpcuser`'s default, `""`, with the password."""
    auth = RpcAuth(password=RpcAuthEntry.from_password("", "pw"))
    assert auth.authenticated_user(basic(b":pw")) == b""
    assert auth.authenticated_user(basic(b"alice:pw")) is None


def test_from_config_carries_every_setting(tmp_path: Path) -> None:
    """The password's entry first, then every `-rpcauth`, as Core's list."""
    config = Config(
        chain="regtest",
        data_dir=tmp_path,
        rpcauth=[RPCAUTH],
        rpcuser="alice",
        rpcpassword=RPCAUTH_PASSWORD,
        rpcwhitelist=["alice:getblockcount"],
    )
    auth = RpcAuth.from_config(config)
    assert auth.entries == [config.rpc_password_entry, *config.rpc_auth]
    assert auth.password_set
    assert auth.rpcauth_set
    assert auth.cookie_file == config.rpc_cookie_file
    assert auth.whitelist == {b"alice": frozenset({"getblockcount"})}
    assert auth.whitelist_default
