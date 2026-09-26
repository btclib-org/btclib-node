# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`RpcAuth`, who may call the JSON-RPC listener, and which methods.

Bitcoin Core's own scheme, read at
bitcoin/bitcoin@9be056a8a7 -- v31.1, the release
`integration-bitcoind.yml` pins: `InitRPCAuthentication`,
`RPCAuthorized`, `CheckUserAuthorized` and `HTTPReq_JSONRPC` in
`src/httprpc.cpp`, `GetAuthCookieFile`, `GenerateAuthCookie` and
`DeleteAuthCookie` in `src/rpc/request.cpp`. Every request carries HTTP
Basic credentials, checked against a list of `RpcAuthEntry`, each a
user, a salt and the HMAC-SHA256 of the password keyed by that salt.
`-rpcauth=<user>:<salt>$<hmac>` adds one as written; `-rpcuser` and
`-rpcpassword` add one hashed with a fresh random salt, so the
plaintext password is not what is kept. The cookie adds another:
`generate_cookie` writes `__cookie__:<64 hex digits>` to `-rpccookiefile`,
`.cookie` in the chain's data directory by default, and keeps only the
salted HMAC of that password. Core writes the cookie unless
`-rpcpassword` is set or `-norpccookiefile` is given, whether or not
`-rpcauth` is.

The user and the HMAC are both compared with `hmac.compare_digest`,
Core's own `TimingResistantEqual`, so the time a refusal takes says
nothing about how much of either matched.

`-rpcwhitelist=<user>:<method>,...` limits a user to the methods it
names, and `refusal` is Core's per-method check, run on the decoded body
once the credential is accepted.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bitcoin_core_rpc import RPCErrorCode

from btclib_node.exceptions import RpcCredentialRefusedError
from btclib_node.rpc.errors import RpcError, json_type_name
from btclib_node.rpc.jsonrpc import JsonRpcRequest, error_status

if TYPE_CHECKING:
    import logging
    from collections.abc import Mapping, Sequence

    from btclib_node.config import Config

__all__ = [
    "COOKIE_FILE",
    "COOKIE_USER",
    "FAILED_ATTEMPT_DELAY",
    "FORBIDDEN",
    "RPCPASSWORD_WARNING",
    "WWW_AUTHENTICATE",
    "Refusal",
    "RpcAuth",
    "RpcAuthEntry",
    "cookie_perms",
    "parse_whitelist",
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
# `-rpcauth` or `-rpcpassword` password weak enough to guess, not for
# the cookie's random one.
FAILED_ATTEMPT_DELAY = 0.25
# `GenerateAuthCookie`'s `COOKIE_SIZE`, in bytes, and the 16-byte salt
# `InitRPCAuthentication` hashes a generated password with
_COOKIE_SIZE = 32
_SALT_SIZE = 16
# `InterpretPermString`'s modes, `src/util/fs_helpers.cpp`
_COOKIE_PERMS = {"owner": 0o600, "group": 0o640, "all": 0o644}
# `InitRPCAuthentication`'s own warning where `-rpcpassword` is set
RPCPASSWORD_WARNING = (
    "The use of rpcuser/rpcpassword is less secure, because credentials are "
    "configured in plain text. It is recommended that locally-run instances "
    "switch to cookie-based auth, or otherwise to use hashed rpcauth "
    "credentials. See share/rpcauth in the source directory for more "
    "information."
)
# `HTTP_FORBIDDEN`, which Core writes with no body
FORBIDDEN = "403 Forbidden"


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


def cookie_perms(value: str) -> int:
    """Return the mode `-rpccookieperms=<value>` sets: `InterpretPermString`.

    `owner` is 0600, `group` 0640 and `all` 0644; anything else raises
    `ValueError` with Core's own message, fatal there too.
    """
    try:
        return _COOKIE_PERMS[value]
    except KeyError:
        err_msg = (
            f"Invalid -rpccookieperms={value}; must be one of 'owner', 'group', "
            "or 'all'."
        )
        raise ValueError(err_msg) from None


def parse_whitelist(values: Sequence[str]) -> dict[bytes, frozenset[str]]:
    """Parse every `-rpcwhitelist` value into each user's allowed methods.

    `InitRPCAuthentication`'s loop: the user is what precedes the first
    `:`, and the methods are what follows it split at every `,` and
    every space, empty names kept. A second value for a user intersects
    with the first. A value with no `:` gives a user not yet named an
    empty whitelist, and leaves one already named as it was.
    """
    whitelist: dict[bytes, frozenset[str]] = {}
    for value in values:
        name, colon, methods = value.partition(":")
        user = name.encode()
        intersect = user in whitelist
        allowed = whitelist.setdefault(user, frozenset())
        if colon:
            named = frozenset(re.split("[, ]", methods))
            whitelist[user] = named & allowed if intersect else named
    return whitelist


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
        """Hash `password` with a fresh random salt, as Core stores one."""
        salt = secrets.token_hex(_SALT_SIZE).encode()
        return cls(user.encode(), salt, password_hmac(salt, password.encode()))


@dataclass(frozen=True)
class Refusal:
    """A request `-rpcwhitelist` refuses: the status line and body Core sends.

    `body` is `None` for Core's bare 403, and otherwise the JSON-RPC
    error object `JSONErrorReply` writes. `warning` is the format and
    arguments of the line Core logs with a 403, `()` where it logs none.
    """

    status: str
    body: dict[str, Any] | None = None
    warning: tuple[str, ...] = ()


def _parse_refusal(request: Mapping[str, Any]) -> Refusal | None:
    """Return what `JSONRPCRequest::parse` refuses a lone request with, if any.

    Core parses a lone request before it checks the whitelist, so a
    request it refuses is answered by that refusal and not by a 403.
    """
    jreq = JsonRpcRequest()
    try:
        jreq.parse(request)
    except RpcError as error:
        return Refusal(error_status(error.code), jreq.reply(error=error))
    return None


def _batch_error(code: RPCErrorCode, message: str) -> Refusal:
    """Return `JSONErrorReply`'s answer to a batch refused before it runs."""
    body = JsonRpcRequest().reply(error=RpcError(code, message))
    return Refusal(error_status(code), body)


def _request_refusal(
    name: str, allowed: frozenset[str], request: Mapping[str, Any]
) -> Refusal | None:
    """Return what `HTTPReq_JSONRPC` refuses a whitelisted user's request with.

    `jreq.parse` first, and a 403 only for a request it accepts.
    """
    parse_refusal = _parse_refusal(request)
    if parse_refusal is not None:
        return parse_refusal
    method = request["method"]
    if method not in allowed:
        warning = ("RPC User %s not allowed to call method %s", name, method)
        return Refusal(FORBIDDEN, warning=warning)
    return None


def _batch_refusal(
    name: str, allowed: frozenset[str], batch: list[Any]
) -> Refusal | None:
    """Return what `HTTPReq_JSONRPC` refuses a whitelisted user's batch with.

    Every entry is checked, in order, before any runs: an entry that is
    not an object is a 400, one whose method is not a string a 500, and
    one whose method is not allowed a 403, whichever comes first. The
    error replies carry `"id":null`, `jreq` not having parsed any entry
    yet.
    """
    for entry in batch:
        if not isinstance(entry, dict):
            return _batch_error(RPCErrorCode.INVALID_REQUEST, "Invalid Request object")
        method = entry.get("method")
        if not isinstance(method, str):
            # `UniValue::get_str`'s own message, which Core answers with
            # `RPC_PARSE_ERROR`
            message = (
                f"JSON value of type {json_type_name(method)} is not of expected "
                "type string"
            )
            return _batch_error(RPCErrorCode.PARSE_ERROR, message)
        if method not in allowed:
            warning = ("RPC User %s not allowed to call method %s", name, method)
            return Refusal(FORBIDDEN, warning=warning)
    return None


class RpcAuth:
    """The credentials and whitelists `RpcConnection.run` checks against.

    `entries` is the `-rpcuser`/`-rpcpassword` pair's entry where
    `-rpcpassword` is set, every `-rpcauth` value after it, and the
    cookie's own once `generate_cookie` has run. `RpcManager.run` calls
    `start` on the manager's thread after binding its listener and
    before accepting on it, and every read of `entries` is a request's,
    on that same thread, afterwards; `RpcManager.stop` calls
    `delete_cookie` once that thread is joined.
    """

    def __init__(  # noqa: PLR0913
        self,
        entries: Sequence[RpcAuthEntry] = (),
        *,
        password: RpcAuthEntry | None = None,
        cookie_file: Path | None = None,
        cookie_tmp: Path | None = None,
        cookie_perms: int | None = None,
        cookie_perms_error: str | None = None,
        rpcauth_invalid: bool = False,
        whitelist: Mapping[bytes, frozenset[str]] | None = None,
        whitelist_default: bool = False,
    ) -> None:
        """Start from `password` and `entries`, with no cookie written yet.

        `password` set is `-rpcpassword` set, which is what stops `start`
        writing a cookie; `cookie_file` `None` is `-norpccookiefile`.
        `cookie_tmp` is `generate_cookie`'s `tmp`. `cookie_perms_error`
        and `rpcauth_invalid` are what `start` refuses, `Config`'s own
        `rpc_cookie_perms_error` and `rpc_auth_invalid`.
        """
        self.entries = [password] if password is not None else []
        self.entries.extend(entries)
        self.password_set = password is not None
        self.rpcauth_set = bool(entries) or rpcauth_invalid
        self.cookie_file = cookie_file
        self.cookie_tmp = cookie_tmp
        self.cookie_perms = cookie_perms
        self.cookie_perms_error = cookie_perms_error
        self.rpcauth_invalid = rpcauth_invalid
        self.whitelist = dict(whitelist or {})
        self.whitelist_default = whitelist_default
        # where `generate_cookie` wrote, and what `delete_cookie`
        # removes: Core's `g_generated_cookie`, so a file this process
        # did not write is never deleted
        self.cookie_path: Path | None = None

    @classmethod
    def from_config(cls, config: Config) -> RpcAuth:
        """Build the `RpcAuth` a node's `config` asks for."""
        return cls(
            config.rpc_auth,
            password=config.rpc_password_entry,
            cookie_file=config.rpc_cookie_file,
            cookie_tmp=config.rpc_cookie_tmp,
            cookie_perms=config.rpc_cookie_perms,
            cookie_perms_error=config.rpc_cookie_perms_error,
            rpcauth_invalid=config.rpc_auth_invalid,
            whitelist=config.rpc_whitelist,
            whitelist_default=config.rpc_whitelist_default,
        )

    def start(self, logger: logging.Logger) -> None:
        """Write the cookie where Core would, logging what Core logs.

        `InitRPCAuthentication`: no cookie where `-rpcpassword` is set,
        with Core's warning that the password sits in plain text, and
        none where `-norpccookiefile` is given. Raises `generate_cookie`'s
        `OSError` where the cookie cannot be written or its permissions set,
        and `RpcCredentialRefusedError` where Core refuses
        `-rpccookieperms`, before the cookie, or `-rpcauth`, after it, each
        logged at Core's level.
        """
        if self.cookie_perms_error is not None:
            logger.error("%s", self.cookie_perms_error)
            raise RpcCredentialRefusedError(self.cookie_perms_error)
        if self.password_set:
            logger.info("Using rpcuser/rpcpassword authentication.")
            logger.warning(RPCPASSWORD_WARNING)
        elif self.cookie_file is None:
            logger.info("RPC authentication cookie file generation is disabled.")
        else:
            path = self.generate_cookie(
                self.cookie_file, self.cookie_perms, tmp=self.cookie_tmp
            )
            logger.info("Generated RPC authentication cookie %s", path)
            # `PermsToSymbolicString`, the nine characters `filemode`
            # writes after the file type
            perms = stat.filemode(path.stat().st_mode)[1:]
            logger.info("Permissions used for cookie: %s", perms)
            logger.info("Using random cookie authentication.")
        if self.rpcauth_set:
            logger.info("Using rpcauth authentication.")
        if self.rpcauth_invalid:
            err_msg = "Invalid -rpcauth argument."
            logger.warning(err_msg)
            raise RpcCredentialRefusedError(err_msg)

    def generate_cookie(
        self, path: Path, perms: int | None = None, *, tmp: Path | None = None
    ) -> Path:
        """Write the cookie to `path` and accept the password it holds.

        Written to `tmp` and renamed over `path`, as Core does, so a
        client never reads a half-written file. `tmp` is `.tmp` appended
        to `path` unless given: `Config.rpc_cookie_tmp` differs from that
        where `-rpccookiefile` normalises to `.`. Created
        with mode 0600 on POSIX: Core's own default is owner-only
        through the process umask 0077 (`-rpccookieperms`' "default:
        owner"), and this sets the mode on the file rather than the
        umask of a process this node shares with its caller. `perms`,
        where given, then replaces that mode after the rename, as Core's
        `fs::permissions` does. On Windows `os.open` and `os.chmod` take
        only the read-only flag from a mode, and every mode
        `-rpccookieperms` names keeps the owner's write bit, so neither
        changes anything there: the ACL of the directory the file sits
        in, which it inherits, decides who reads it. A leftover `.tmp`
        is removed first, since `O_CREAT`'s mode applies only to a file
        it creates.

        Raises `OSError` with `GenerateAuthCookie`'s own warning where
        the file cannot be opened, renamed or given `perms`, the
        `.tmp` left where the rename fails, as Core leaves it. A path
        holding a NUL byte is refused as one that cannot be opened, the
        `ValueError` Python raises for it forcing a divergence:
        `bitcoind` v31.1.0 writes to the path cut at that byte instead.
        """
        tmp = Path(f"{path}.tmp") if tmp is None else tmp
        password = secrets.token_hex(_COOKIE_SIZE)
        try:
            tmp.unlink(missing_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except (OSError, ValueError) as err:
            msg = f"Unable to open cookie authentication file {tmp} for writing"
            raise OSError(msg) from err
        with os.fdopen(fd, "w", encoding="ascii") as file:
            file.write(f"{COOKIE_USER}:{password}")
        try:
            tmp.replace(path)
        except OSError as err:
            msg = f"Unable to rename cookie authentication file {tmp} to {path}"
            raise OSError(msg) from err
        if perms is not None:
            try:
                path.chmod(perms)
            except OSError as err:
                msg = f"Unable to set permissions on cookie authentication file {path}"
                raise OSError(msg) from err
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

    def authenticated_user(self, authorization: str) -> bytes | None:
        """Return the user an `Authorization` header names, if it is accepted.

        Core's `RPCAuthorized`: the `Basic ` scheme, base64 of
        `<user>:<password>` split at its first `:`, then
        `CheckUserAuthorized` over every entry. A header that is not
        this shape is a wrong credential, not a malformed request.
        `None` is a refusal; `b""` is the empty user an `-rpcpassword`
        with no `-rpcuser` is accepted as.
        """
        if not authorization.startswith("Basic "):
            return None
        # Core's `TrimStringView`, which trims these six and no other
        # whitespace, where a bare `str.strip()` also trims `\xa0`
        encoded = authorization[6:].strip(" \f\n\r\t\v")
        try:
            userpass = base64.b64decode(encoded, validate=True)
        # `binascii.Error` for bad base64, a plain `ValueError` for a
        # header holding anything but ASCII
        except ValueError:
            return None
        user, colon, password = userpass.partition(b":")
        if not colon or not self._check_user(user, password):
            return None
        return user

    def _check_user(self, user: bytes, password: bytes) -> bool:
        """Core's `CheckUserAuthorized`: user and HMAC in constant time."""
        for entry in self.entries:
            if not hmac.compare_digest(entry.user, user):
                continue
            if hmac.compare_digest(password_hmac(entry.salt, password), entry.hmac):
                return True
        return False

    def refusal(self, user: bytes, request: object) -> Refusal | None:
        """Return how `-rpcwhitelist` refuses `user`'s decoded `request`, if so.

        `HTTPReq_JSONRPC`'s check, run once the body has parsed as JSON:
        a user with no whitelist may call every method, unless
        `-rpcwhitelistdefault` holds, which refuses it everything with a
        403. A whitelisted user's lone request is parsed first, answered
        with Core's own error where that parse refuses it, and otherwise
        refused with a 403 where its method is not allowed; a batch is
        checked entry by entry, before any of them runs. Anything else a
        whitelisted user sends is left to the answer anybody else gets.
        """
        # a name some entry holds, so it decodes, for the log line alone
        name = user.decode(errors="replace")
        allowed = self.whitelist.get(user)
        if allowed is None:
            if self.whitelist_default:
                warning = ("RPC User %s not allowed to call any methods", name)
                return Refusal(FORBIDDEN, warning=warning)
            return None
        if isinstance(request, dict):
            return _request_refusal(name, allowed, request)
        if isinstance(request, list):
            return _batch_refusal(name, allowed, request)
        return None
