# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`RpcAuth`, who may call the JSON-RPC listener: a cookie and `-rpcauth`.

Bitcoin Core's own scheme, read at
bitcoin/bitcoin@9be056a8a7 -- v31.1, the release
`integration-bitcoind.yml` pins: `InitRPCAuthentication`,
`RPCAuthorized` and `CheckUserAuthorized` in `src/httprpc.cpp`,
`GenerateAuthCookie` and `DeleteAuthCookie` in `src/rpc/request.cpp`.
Every request carries HTTP Basic credentials, checked against a list of
`RpcAuthEntry`, each a user, a salt and the HMAC-SHA256 of the password
keyed by that salt. `-rpcauth=<user>:<salt>$<hmac>` adds one as
written. The cookie adds another: `generate_cookie` writes
`__cookie__:<64 hex digits>` to `.cookie` in the chain's data
directory, mode 0600 on POSIX, where on Windows the data directory's
ACL decides who reads it, and keeps only the salted HMAC of that
password, the way Core hashes it before storage. Core writes
the cookie unless `-rpcpassword` is set, whether or not `-rpcauth` is,
and this node has no `-rpcpassword`, so it always writes one.

The user and the HMAC are both compared with `hmac.compare_digest`,
Core's own `TimingResistantEqual`, so the time a refusal takes says
nothing about how much of either matched.

What this node does not take of Core's: `-rpcuser`/`-rpcpassword`,
`-rpccookiefile`/`-norpccookiefile`, `-rpccookieperms` and
`-rpcwhitelist` (btclib-org/btclib-node#1070).
"""

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "COOKIE_FILE",
    "COOKIE_USER",
    "FAILED_ATTEMPT_DELAY",
    "WWW_AUTHENTICATE",
    "RpcAuth",
    "RpcAuthEntry",
    "password_hmac",
]

# Core's `COOKIEAUTH_USER` and `COOKIEAUTH_FILE`
COOKIE_USER = "__cookie__"
COOKIE_FILE = ".cookie"
# Core's `WWW_AUTH_HEADER_DATA`, sent with every 401
WWW_AUTHENTICATE = 'Basic realm="jsonrpc"'
# Seconds: Core's `UninterruptibleSleep(250ms)` before answering a wrong
# credential, its comment's "Deter brute-forcing". Core's sleep holds
# one of its `-rpcthreads` workers (`DEFAULT_HTTP_THREADS`), so wrong
# attempts arrive no faster than its workers can sleep through them.
# `RpcConnection` awaits this instead, holding only the connection that
# sent the credential: connections opened in parallel each pay it, and
# nothing caps wrong attempts across them. That matters for an
# `-rpcauth` password weak enough to guess, not for the cookie's
# random one.
FAILED_ATTEMPT_DELAY = 0.25
# `GenerateAuthCookie`'s `COOKIE_SIZE`, in bytes, and the 16-byte salt
# `InitRPCAuthentication` hashes a generated password with
_COOKIE_SIZE = 32
_SALT_SIZE = 16


def password_hmac(salt: bytes, password: bytes) -> bytes:
    """Return HMAC-SHA256 of `password` keyed by `salt`, as lowercase hex.

    What an `-rpcauth` line's third field holds, and what `share/rpcauth/`
    in Core's source computes for one.
    """
    # A single HMAC-SHA256 and not a slow password hash: it is Core's
    # `-rpcauth` format (`CheckUserAuthorized`), which `rpcauth.py`
    # lines already written in a `bitcoin.conf` depend on. The cookie's
    # password is 256 random bits, and `rpcauth.py` generates a 256-bit
    # one where it is not handed one.
    return hmac.new(salt, password, hashlib.sha256).hexdigest().encode()


@dataclass(frozen=True)
class RpcAuthEntry:
    """One user this listener accepts: a name, a salt and a password's HMAC.

    Held as `bytes`, which is what `hmac.compare_digest` compares for
    any content: it refuses a `str` holding anything but ASCII, and a
    user name comes off the wire as whatever octets the client sent.
    """

    user: bytes
    salt: bytes
    hmac: bytes

    @classmethod
    def parse(cls, value: str) -> RpcAuthEntry:
        """Parse one `-rpcauth` value, `<user>:<salt>$<hmac>`.

        Core's split: on `:` into exactly two fields, then the second on
        `$` into exactly two. Anything else is Core's fatal
        "Invalid -rpcauth argument.", so a user name cannot hold a `:`.
        """
        fields = value.split(":")
        salt_hmac = fields[-1].split("$")
        if len(fields) != 2 or len(salt_hmac) != 2:  # noqa: PLR2004
            err_msg = "Invalid -rpcauth argument."
            raise ValueError(err_msg)
        user, salt, digest = fields[0], *salt_hmac
        return cls(user.encode(), salt.encode(), digest.encode())

    @classmethod
    def from_password(cls, user: str, password: str) -> RpcAuthEntry:
        """Hash `password` with a fresh random salt, as Core does a cookie's."""
        salt = secrets.token_hex(_SALT_SIZE).encode()
        return cls(user.encode(), salt, password_hmac(salt, password.encode()))


class RpcAuth:
    """The credentials `RpcConnection.run` checks every request against.

    `entries` is every `-rpcauth` value `Config.rpc_auth` holds, and the
    cookie's own once `generate_cookie` has run. `RpcManager.run` calls
    that on the manager's thread before binding its listener, and every
    read of `entries` is a request's, on that same thread, afterwards;
    `RpcManager.stop` calls `delete_cookie` once that thread is joined.
    """

    def __init__(self, entries: tuple[RpcAuthEntry, ...] = ()) -> None:
        """Start from `entries`, with no cookie written yet."""
        self.entries = list(entries)
        # where `generate_cookie` wrote, and what `delete_cookie`
        # removes: Core's `g_generated_cookie`, so a file this process
        # did not write is never deleted
        self.cookie_path: Path | None = None

    def generate_cookie(self, data_dir: Path) -> Path:
        """Write `data_dir / COOKIE_FILE` and accept the password it holds.

        Written to a `.tmp` sibling and renamed over the real name, as
        Core does, so a client never reads a half-written file. Created
        with mode 0600 on POSIX: Core's own default is owner-only
        through the process umask 0077 (`-rpccookieperms`' "default:
        owner"), and this sets the mode on the file rather than the
        umask of a process this node shares with its caller. On Windows
        `os.open` takes only the read-only flag from a mode, so the data
        directory's ACL, which the file inherits, decides who reads it.
        A leftover `.tmp` is
        removed first, since `O_CREAT`'s mode applies only to a file it
        creates. Raises `OSError` where the file cannot be written.
        """
        path = data_dir / COOKIE_FILE
        tmp = data_dir / (COOKIE_FILE + ".tmp")
        password = secrets.token_hex(_COOKIE_SIZE)
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as file:
            file.write(f"{COOKIE_USER}:{password}")
        tmp.replace(path)
        self.entries.append(RpcAuthEntry.from_password(COOKIE_USER, password))
        self.cookie_path = path
        return path

    def delete_cookie(self) -> None:
        """Remove the cookie `generate_cookie` wrote, if it wrote one.

        A cookie already gone is not an error, as `fs::remove` returns
        false rather than raising for it; any other failure raises
        `OSError`, which Core logs as a warning.
        """
        if self.cookie_path is not None:
            self.cookie_path.unlink(missing_ok=True)
            self.cookie_path = None

    def authorized(self, authorization: str) -> bool:
        """Return whether an `Authorization` header names an accepted user.

        Core's `RPCAuthorized`: the `Basic ` scheme, base64 of
        `<user>:<password>` split at its first `:`, then
        `CheckUserAuthorized` over every entry. A header that is not
        this shape is a wrong credential, not a malformed request.
        """
        if not authorization.startswith("Basic "):
            return False
        # Core's `TrimStringView`, which trims these six and no other
        # whitespace, where a bare `str.strip()` also trims `\xa0`
        encoded = authorization[6:].strip(" \f\n\r\t\v")
        try:
            userpass = base64.b64decode(encoded, validate=True)
        # `binascii.Error` for bad base64, a plain `ValueError` for a
        # header holding anything but ASCII
        except ValueError:
            return False
        user, colon, password = userpass.partition(b":")
        if not colon:
            return False
        return self._check_user(user, password)

    def _check_user(self, user: bytes, password: bytes) -> bool:
        """Core's `CheckUserAuthorized`: user and HMAC in constant time."""
        for entry in self.entries:
            if not hmac.compare_digest(entry.user, user):
                continue
            if hmac.compare_digest(password_hmac(entry.salt, password), entry.hmac):
                return True
        return False
