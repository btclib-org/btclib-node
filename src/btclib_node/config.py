# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`Config`, the settings one `Node` is built from.

Which chain to join, where its data lives, which listeners to start and
on which interfaces, and the feerate floor its mempool accepts at and
tells a peer about in `feefilter` -- `DEFAULT_MIN_RELAY_FEERATE` below,
Core's own `DEFAULT_MIN_RELAY_TX_FEE`. `_resolve_chain` is what turns a chain
already built, or a network's name, into the `Chain` a `Config` carries.
`split_host_port` and `lookup_host_port` split a "host[:port]": the first
is `-rpcbind`'s, the second a peer's. `lookup_service`, `parse_bind` and
`listen_port` read an address Core's `Lookup` reads, for `-externalip`
and `-bind`.
`get_path_arg` is `cli.py`'s reader of `-datadir`, `-conf` and
`-blocksdir`. All of these are public here because other modules read
them.
"""

import os

# A real import and not TYPE_CHECKING-only like Mapping below, despite
# costing nothing more at runtime than that one would: Config.__init__
# below carries ten `Sequence[str]` parameters, one per repeatable
# setting, and past nine Sphinx's own autodoc -- resolving PEP 649's
# lazy annotations through `annotationlib`, at this Python 3.14 target
# -- answers the tenth with a broken cross-reference to a synthetic
# `__annotationlib_name_N__` placeholder rather than the bare source
# string it falls back to for the first nine, failing the docs gate's
# own `-W`. A name TYPE_CHECKING alone cannot resolve is what forces
# that fallback in the first place (CLAUDE.md's own note on
# `autodoc_typehints_format`); importing it for real removes the need
# for any fallback at all, for every `Sequence[str]` below at once,
# rather than trading the tenth's placeholder for an eleventh later.
# btclib-org/btclib-node#1519
from collections.abc import Collection, Sequence  # noqa: TC003
from dataclasses import dataclass
from ipaddress import IPv6Address
from pathlib import Path
from typing import TYPE_CHECKING

from btclib.fee import FeeRate

from btclib_node.chains import Chain, Main, RegTest, SigNet, TestNet, TestNet4
from btclib_node.constants import MAX_TIP_AGE, default_data_dir
from btclib_node.exceptions import InvalidChainTypeError, UnknownChainError
from btclib_node.p2p.banman import DEFAULT_MISBEHAVING_BANTIME, Host, lookup_host
from btclib_node.rpc.auth import (
    COOKIE_FILE,
    RpcAuthEntry,
    cookie_perms,
    parse_whitelist,
)
from btclib_node.rpc.connection import REQUEST_TIMEOUT

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_DUST_RELAY_FEERATE",
    "DEFAULT_INCREMENTAL_RELAY_FEERATE",
    "DEFAULT_MAX_DATACARRIER_BYTES",
    "DEFAULT_MAX_PEER_CONNECTIONS",
    "DEFAULT_MAX_TIP_AGE",
    "DEFAULT_MIN_RELAY_FEERATE",
    "BindAddress",
    "Config",
    "get_path_arg",
    "listen_port",
    "lookup_host_port",
    "lookup_service",
    "parse_bind",
    "service_text",
    "split_host_port",
]

# Core's own floor, `DEFAULT_MIN_RELAY_TX_FEE` (`src/policy/policy.h`,
# read at bitcoin/bitcoin@58a7869f86): 100 sat/kvB. The floor
# `main.verify_mempool_acceptance` refuses a candidate under
# (btclib-org/btclib-node#1245), and the one this node tells a peer about
# in `feefilter` (btclib-org/btclib-node#94).
DEFAULT_MIN_RELAY_FEERATE = FeeRate(sats_per_kvbyte=100)
# Core's own `DEFAULT_INCREMENTAL_RELAY_FEE` (`src/policy/policy.h`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag): 100 sat/kvB, the default of
# `-incrementalrelayfee`. Core keeps it apart from the floor above, the two
# merely sharing a value.
DEFAULT_INCREMENTAL_RELAY_FEERATE = FeeRate(sats_per_kvbyte=100)
# Core's own `DUST_RELAY_TX_FEE` (same file and sha): 3000 sat/kvB, the
# default of `-dustrelayfee`.
DEFAULT_DUST_RELAY_FEERATE = FeeRate(sats_per_kvbyte=3000)
# Core's own `MAX_OP_RETURN_RELAY` (same file and sha),
# `MAX_STANDARD_TX_WEIGHT / WITNESS_SCALE_FACTOR`: the default of
# `-datacarriersize`, in bytes.
DEFAULT_MAX_DATACARRIER_BYTES = 400_000 // 4
# Core's own `-maxconnections` default, `DEFAULT_MAX_PEER_CONNECTIONS`
# (`src/net.h`), read at the release `integration-bitcoind.yml` pins,
# v31.1 at bitcoin/bitcoin@9be056a8a7. Core's `master` sets 200 (at
# bitcoin/bitcoin@e8e7e91a11) beside `-inboundrelaypercent`, which by
# default holds transaction-relaying inbound peers to half of the
# inbound slots; this node has no such split, so it takes the value the
# pinned release pairs with no split either.
DEFAULT_MAX_PEER_CONNECTIONS = 125
# A named module-level singleton rather than `Main()` written straight
# into __init__'s own signature below: a call there is made once, at
# import time, and B008 is what a reader would otherwise have to notice
# on their own -- this is the fix ruff's own message suggests, and the
# shape `DEFAULT_MIN_RELAY_FEERATE` above already uses for the same
# reason.
DEFAULT_CHAIN = Main()
# Core's own `DEFAULT_MAX_TIP_AGE` (`src/kernel/chainstatemanager_opts.h`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) in seconds, which is what
# `-maxtipage`'s own default is measured in: `constants.py`'s `MAX_TIP_AGE`
# is the same span as a `timedelta`, for `update_ibd_status`'s own
# unoverridden default before this option existed, and is read here
# rather than restated.
DEFAULT_MAX_TIP_AGE = int(MAX_TIP_AGE.total_seconds())


def get_path_arg(value: str) -> str:
    """Return `value` normalised as Core's `GetPathArg` normalises a path.

    `GetPathArg` (`src/common/args.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag) is `lexically_normal` and a trailing-slash strip, and
    `os.path.normpath` is both, lexical too, so `missing/../name` is
    `name` whether or not `missing` exists. A leading `//` is the one
    difference: POSIX lets `normpath` keep it, `lexically_normal`
    collapses it to `/`, and it is collapsed here, so that a path a
    refusal names is the one `bitcoind` v31.1.0 names. `normpath` gives
    back no other leading `//`: it collapses three or more slashes to
    one, and on Windows it writes backslashes.
    """
    path = os.path.normpath(value)
    return path[1:] if path.startswith("//") else path


_MAX_PORT = 0xFFFF


def _split(spec: str, default_port: int) -> tuple[str, int, bool]:
    """Return `spec`'s host, port and whether Core's `SplitHostPort` is true.

    `SplitHostPort` (`src/util/strencodings.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag) takes the last colon as
    the port separator, unless it is not the only one and does not close
    an IPv6 literal's own `[...]`: an IPv6 address given without
    brackets and without a port is read whole. The port is
    `ToIntegral<uint16_t>`'s: ASCII digits alone, so a sign, a space, a
    `_` or a non-ASCII digit, which `int` would each accept, are no
    port. Where there is none, the host is the whole spec and the port
    `default_port`. Port 0 is split off and is not valid.
    """
    host = spec
    port = default_port
    valid = True
    colon = spec.rfind(":")
    if colon != -1:
        bracketed = spec.startswith("[") and spec[:colon].endswith("]")
        multi_colon = spec.rfind(":", 0, colon) != -1
        if colon == 0 or bracketed or not multi_colon:
            valid = False
            text = spec[colon + 1 :]
            digits = text.lstrip("0")
            if (
                text.isascii()
                and text.isdigit()
                and len(digits) <= len(str(_MAX_PORT))
                and int(digits or "0") <= _MAX_PORT
            ):
                host, port = spec[:colon], int(digits or "0")
                valid = port != 0
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, port, valid


def split_host_port(spec: str, default_port: int) -> tuple[str, int]:
    """Split "host[:port]" as `SplitHostPort` does, refusing where it is false.

    `default_port` is what a spec naming none falls back to: Core's own
    callers pre-fill the port before calling `SplitHostPort`, which only
    overwrites it when the spec actually names one (`ConnectNode`,
    `src/net.cpp:505-507`, at bitcoin/bitcoin@ca7162cde5) --
    `-connect=1.2.3.4` and `-addnode=1.2.3.4` both dial the chain's own
    default P2P port this way. This is for a caller whose option Core
    checks at init, `-rpcbind`; a peer spec is `lookup_host_port`'s.
    """
    host, port, valid = _split(spec, default_port)
    if not valid:
        err_msg = f"{spec!r} names an invalid port"
        raise ValueError(err_msg)
    return host, port


def lookup_host_port(spec: str, default_port: int) -> tuple[str, int]:
    """Split "host[:port]" as Core's `Lookup` does, valid or not.

    `Lookup` (`src/netbase.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag) ignores what `SplitHostPort` returns and looks up the host it
    left. A spec whose port is no port is therefore a host name that
    resolves to nothing: `bitcoind` starts with `-connect=127.0.0.1:+80`
    and never connects. This is for every `-connect`, `-addnode` and
    `-seednode` value and the `addnode` RPC's.
    """
    host, port, _ = _split(spec, default_port)
    return host, port


def lookup_service(
    spec: str, default_port: int, *, allow_lookup: bool = False
) -> tuple[Host, int] | None:
    """Return the address and port `spec` names, as Core's `Lookup`, or `None`.

    `Lookup` (`src/netbase.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag) splits the spec as `lookup_host_port` does and resolves the
    host it left: a name only where `allow_lookup`, a numeric address
    otherwise (`-bind`'s, `-externalip`'s being `-dns`'s default).
    """
    host, port = lookup_host_port(spec, default_port)
    resolved = lookup_host(host, allow_lookup=allow_lookup)
    return None if resolved is None else (resolved, port)


def service_text(host: Host, port: int) -> str:
    """Return `host` and `port` as `CService::ToStringAddrPort` writes them."""
    return f"[{host}]:{port}" if isinstance(host, IPv6Address) else f"{host}:{port}"


@dataclass(frozen=True)
class BindAddress:
    """One `-bind` value: where to listen, and whether it is tagged `=onion`."""

    host: Host
    port: int
    onion: bool


def parse_bind(arg: str, default_port: int) -> BindAddress:
    """Return the address `-bind=<arg>` names, as `InitBinds`' caller reads it.

    Core's warning for a bad port (`BadPortWarning`, same sha) is not
    given: btclib-org/btclib-node#1645.

    `AppInitMain` (`src/init.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag) looks a numeric address up, at `default_port` where the
    value names none; one tagged `=onion` takes `default_port + 1`
    instead, and any other tag resolves nothing. Raises `ValueError` in
    Core's words where the address is none.
    """
    head, equals, tag = arg.rpartition("=")
    onion = bool(equals) and tag == "onion"
    if equals and not onion:
        service = None
    else:
        port = default_port + 1 if onion else default_port
        service = lookup_service(head if equals else arg, port)
    if service is None:
        err_msg = f"Cannot resolve -bind address: '{arg}'"
        raise ValueError(err_msg)
    return BindAddress(*service, onion)


def listen_port(bind: Sequence[str], default_port: int) -> int:
    """Return the port this node is said to listen on, as `GetListenPort` does.

    `GetListenPort` (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the
    v31.1 tag) is the port of the first `-bind` that names one, and
    `default_port` otherwise. An `=onion` value resolves to nothing there,
    and so is passed over.
    """
    for value in bind:
        service = lookup_service(value, 0)
        if service is not None and service[1] != 0:
            return service[1]
    return default_port


def _refuse_bind_without_listen(bind: Sequence[str], *, listen: bool) -> None:
    """Refuse a `-bind` beside `-listen=0`, in the words of Core's refusal.

    `AppInitParameterInteraction` (`src/init.cpp`, at
    bitcoin/bitcoin@9be056a8a7, the v31.1 tag), whose words name
    `-whitebind` too.
    """
    if bind and not listen:
        err_msg = "Cannot set -bind or -whitebind together with -listen=0"
        raise ValueError(err_msg)


def _split_peers(
    specs: Sequence[str], default_port: int
) -> tuple[tuple[str, int], ...]:
    """Split every spec in `specs` into its host and port, host unresolved.

    Core's own `-connect`/`-addnode`/`-seednode` reach
    `CConnman::ConnectNode` as a name and resolve it via `Resolve`
    (`src/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) at
    every dial, same as any other peer -- so neither a hostname nor a
    malformed port is an error here, only a value nothing resolves
    until then (btclib-org/btclib-node#1264,
    btclib-org/btclib-node#1292).
    `P2pManager.async_connect_host` (`p2p/manager.py`) is this node's
    own equivalent: it resolves a host on `P2pManager`'s asyncio loop
    right before dialling.
    """
    return tuple(lookup_host_port(spec, default_port) for spec in specs)


def _read_cookie_perms(value: str) -> tuple[int | None, str | None]:
    """Return `-rpccookieperms`' mode, or `cookie_perms`' refusal of it."""
    try:
        return cookie_perms(value), None
    except ValueError as err:
        return None, str(err)


def _read_rpcauth(values: Sequence[str]) -> tuple[tuple[RpcAuthEntry, ...], bool]:
    """Return the `-rpcauth` entries before a malformed one, and whether one is.

    `InitRPCAuthentication` stops at the first malformed value, refusing it.
    """
    entries: list[RpcAuthEntry] = []
    for value in values:
        try:
            entries.append(RpcAuthEntry.parse(value))
        except ValueError:
            return tuple(entries), True
    return tuple(entries), False


def _resolve_cookie_file(
    value: str | Path | None, data_dir: Path, *, temp: bool = False
) -> Path | None:
    """Return where `-rpccookiefile=<value>` writes, `None` for no cookie.

    Core's `GetAuthCookieFile`: an empty value is `COOKIE_FILE`, any
    other is normalised by `get_path_arg` above, and
    `AbsPathForConfigVal` resolves a relative one against the chain's
    own data directory.

    `temp` is `GetAuthCookieFile(true)`, the file the cookie is written
    to before its rename: `.tmp` appended to the normalised value before
    it is resolved, so `.` is `<chain dir>/..tmp` and `/` is `/.tmp`, as
    `bitcoind` v31.1.0 names them. `Path` drops a trailing `.`, so the
    cookie itself for `.` is the chain directory, where Core's warning
    names `<chain dir>/.`, and `.tmp` appended to that would be
    `<chain dir>.tmp`.
    """
    if value is None:
        return None
    arg = get_path_arg(str(value)) if value else COOKIE_FILE
    path = Path(arg + ".tmp" if temp else arg)
    return path if path.is_absolute() else data_dir / path


def _resolve_chain(chain: Chain | str) -> Chain:
    if isinstance(chain, Chain):
        return chain
    if not isinstance(chain, str):
        raise InvalidChainTypeError(chain)
    if chain == "mainnet":
        return Main()
    if chain == "testnet":
        return TestNet()
    if chain == "signet":
        return SigNet()
    if chain == "regtest":
        return RegTest()
    if chain == "testnet4":
        return TestNet4()
    raise UnknownChainError(chain)


def _dnsseed(
    *,
    dnsseed: bool | None,
    forcednsseed: bool,
    connect_given: bool,
    max_connections: int,
) -> bool:
    """Return `-dnsseed` after its soft-set; refused off with `-forcednsseed`.

    `AppInitParameterInteraction`'s own order (`src/init.cpp`, at
    bitcoin/bitcoin@9be056a8a7): the soft-set runs first -- off under
    `-connect` or a non-positive `-maxconnections`, an explicit
    `dnsseed` winning over it -- and only then is `-forcednsseed`
    refused against the result, in Core's own wording.
    """
    if dnsseed is None:
        dnsseed = not connect_given and max_connections > 0
    if forcednsseed and not dnsseed:
        err_msg = "Cannot set -forcednsseed to true when setting -dnsseed to false."
        raise ValueError(err_msg)
    return dnsseed


# `-v1transport`'s default where `-v2transport` is on: off, a departure
# from Core argued in SECURITY.md's *Where this node departs from Bitcoin
# Core*. `_v1transport` is the one place that reads it.
_DEFAULT_V1_TRANSPORT = False


def _v1transport(*, v1transport: bool | None, v2transport: bool) -> bool:
    """Return `-v1transport`; `-v2transport=0` alone switches it on.

    A node with neither transport would speak to nobody, so an explicit
    `-v1transport=0` with `-v2transport=0` is refused, as `_dnsseed`
    refuses its own contradiction. Core has no `-v1transport`; it is this
    node's, for refusing v1 (btclib-org/btclib-node#1190).
    """
    if v1transport is None:
        return not v2transport or _DEFAULT_V1_TRANSPORT
    if not v1transport and not v2transport:
        err_msg = "Cannot set -v1transport to false when setting -v2transport to false."
        raise ValueError(err_msg)
    return v1transport


@dataclass
class Config:
    """Every setting one `Node` is built from, flat and keyword-only.

    Built by `__init__` below rather than by the fields' own defaults,
    since a chain given as a name has to resolve to a `Chain` first, and
    a port left unset by `allow_p2p=False`/`allow_rpc=False` has to
    become `None` rather than the class's own declared `int`.
    """

    chain: Chain
    # a Path, which is what __init__ below stores and what every reader
    # of it does path arithmetic on
    data_dir: Path
    # `None`, Core's own "default: <datadir>" (`-blocksdir=<dir>`'s own
    # help text, `src/init.cpp:514`, at bitcoin/bitcoin@ca7162cde5),
    # unless a caller names a base directory of its own for `BlockDB`'s
    # files -- chain-suffixed here the same way `data_dir` above is,
    # matching `ArgsManager::GetBlocksDirPath`'s own append of the
    # chain-specific subdirectory (and then `blocks`) onto whichever
    # base it resolved, file first or `-datadir` (`src/common/
    # args.cpp:298-319`, same sha). `BlockDB.__init__` is where the
    # `blocks` leaf itself, and the `data_dir` fallback, are appended --
    # not here, so a caller building a `BlockDB` directly still gets
    # Core's own default without going through `Config` at all.
    blocks_dir: Path | None
    # `None` and not an int is the whole of what `allow_p2p=False` and
    # `allow_rpc=False` do: __init__ below leaves the port unset, and
    # Node reads it as the answer to whether that listener is started
    # at all. Declared `int` these two said the opposite of what they
    # hold, and every reader believing the annotation would take a
    # disallowed port for a port to bind.
    p2p_port: int | None
    rpc_port: int | None
    # what RpcManager binds instead of every interface: an RPC server is
    # this node's control plane, not a peer-to-peer listener, so a
    # caller holding a credential still has to reach it from an
    # interface this names. `None` is Core's own default, `::1` and
    # `127.0.0.1` both (`HTTPBindAddresses`, `src/httpserver.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag); a host a Python
    # caller names is bound alone, where `-rpcbind` and `-rpcallowip`
    # are not both given. P2pManager binds every interface unless
    # `bind` below names some, and is right to, since a peer listener is
    # supposed to accept a stranger.
    rpc_host: str | None
    # Core's `-rpcbind` values, checked by `cli`: `RpcManager` binds
    # them where `rpcallowip` below names something, and warns over them
    # where it does not, as `HTTPBindAddresses` does.
    rpcbind: tuple[str, ...]
    # Core's `-rpcallowip` values, which `RpcManager` parses when it
    # starts, as `InitHTTPServer` parses them, and answers a request
    # from no source they or loopback name with a 403.
    rpcallowip: tuple[str, ...]
    # Core's `-whitelist` values, each exactly as given: `Node.run` reads
    # them with `-whitelistrelay` and `-whitelistforcerelay`
    # (`p2p.permissions.Whitelist.parse`), as `AppInitMain` does once the
    # stores are open, and refuses the node's start for one it cannot.
    whitelist: tuple[str, ...]
    whitelist_relay: bool
    whitelist_force_relay: bool
    # Core's own `-rpcservertimeout`, in seconds: how long
    # `RpcConnection.run` may take reading one request, and the idle gap
    # a kept-alive connection may sit in between two of them, both
    # matched to `REQUEST_TIMEOUT` by default. `RpcManager.__init__` is
    # where `0` and `-1` both read as no bound at all, as they do for
    # Core's own `evhttp_set_timeout` (`rpc.manager._request_timeout`'s
    # own docstring).
    rpcservertimeout: int
    # Core's own `-rpcauth`, one entry per value: users the RPC listener
    # accepts beside the cookie and `rpc_password_entry`. Where a value
    # is malformed, the values ahead of it, and `rpc_auth_invalid` set.
    rpc_auth: tuple[RpcAuthEntry, ...]
    # whether a `-rpcauth` value is malformed, which `RpcAuth.start`
    # refuses once the listener is bound, as Core's
    # `InitRPCAuthentication` does
    rpc_auth_invalid: bool
    # Core's own `-rpcuser`/`-rpcpassword`, hashed with a random salt so
    # that the plaintext password is not what is kept; `None` where
    # `-rpcpassword` is unset or empty. Set, it stops the cookie being
    # written, as it stops Core's.
    rpc_password_entry: RpcAuthEntry | None
    # Core's own `-rpccookiefile`, a relative path resolved against
    # `data_dir`; `None` is `-norpccookiefile`, which writes none.
    rpc_cookie_file: Path | None
    # where the cookie is written before it is renamed to
    # `rpc_cookie_file`; `None` where that is
    rpc_cookie_tmp: Path | None
    # Core's own `-rpccookieperms`, as the mode `rpc.auth.cookie_perms`
    # maps it to; `None` is its default, owner-only, and is what it is
    # wherever `-rpcpassword` is set, Core not reading it then.
    rpc_cookie_perms: int | None
    # `rpc.auth.cookie_perms`' own message where Core reads
    # `-rpccookieperms` and it names no mode, `None` otherwise: refused
    # by `RpcAuth.start` once the listener is bound, as Core refuses it
    rpc_cookie_perms_error: str | None
    # Core's own `-rpcwhitelist`, parsed by `rpc.auth.parse_whitelist`:
    # the methods each user named may call.
    rpc_whitelist: Mapping[bytes, frozenset[str]]
    # Core's own `-rpcwhitelistdefault`: whether a user with no
    # whitelist may call nothing. Its default is whether any
    # `-rpcwhitelist` is given.
    rpc_whitelist_default: bool
    # `True` is Core's own `IsPruneMode()`: some block and undo data may
    # be deleted, `MIN_BLOCKS_TO_KEEP` (constants.py, 288, Core's own two
    # days) behind the tip never among it -- `block_db.BlockDB.prune_up_to`
    # is what actually deletes. `prune_target_mib` below is the other
    # half of Core's own `-prune=<n>`: which of manual (RPC-only) or
    # automatic-to-a-MiB-target pruning this is.
    pruned: bool
    # `None` is Core's own manual pruning (`-prune=1`): nothing is
    # deleted on its own, only `rpc.callbacks.prune_blockchain` deletes,
    # and only when asked. An int is Core's own automatic pruning
    # (`-prune=<n>`, `n >= MIN_PRUNE_TARGET_MIB`): `main._prune_chain`
    # deletes on its own, keeping actual bytes under `blocks/` close to
    # this many MiB, `MIN_BLOCKS_TO_KEEP` behind the tip still never
    # reached. Read only where `pruned` is `True`; `main._prune_chain`
    # and `rpc.callbacks.get_blockchain_info` both check `pruned` first.
    prune_target_mib: int | None
    debug: bool
    # the categories `-debug` names, `log.Logger.log_debug`'s filter:
    # empty where `debug` is on for every category
    debug_categories: frozenset[str]
    # the categories `-debugexclude` names, which `-debug` does not select
    debug_exclude: frozenset[str]
    # `-logratelimit`: `log.LogRateLimiter` limits `history.log`
    log_rate_limit: bool
    # the warnings Core buffers while it reads its settings, in order;
    # `open_history_log` logs each once its own log is open, ahead of
    # its version line
    log_warnings: tuple[str, ...]
    # `AppInitParameterInteraction`'s one warning about a section naming
    # no chain (`cli._warn_unrecognized_sections`), logged after the
    # version line; `""` where no section is unrecognised
    section_warning: str
    # `ArgsManager::LogArgs`'s own lines (`cli._log_args`): the config
    # file's args, then the command line's, logged after
    # `section_warning`
    config_args: tuple[str, ...]
    # `init::StartLogging`'s "Config file:" line (`cli._config_file_line`),
    # logged after the data directory's; `""` for a `Config` that was
    # not built from a command line
    config_file_line: str
    min_relay_feerate: FeeRate
    # Core's own `-incrementalrelayfee`: the extra fee a replacement pays
    # and what an eviction adds to the mempool's rolling minimum
    # (`Mempool`); `MemPoolOptions::incremental_relay_feerate`
    incremental_relay_feerate: FeeRate
    # The standardness options, `MemPoolOptions`' own `dust_relay_feerate`,
    # `permit_bare_multisig`, `max_datacarrier_bytes` (`None` where
    # `-datacarrier` is off) and `require_standard` (`-acceptnonstdtxn`
    # off), which `verify_mempool_acceptance` enforces
    # (btclib-org/btclib-node#1382); `getmempoolinfo` reports
    # `permit_bare_multisig` and `max_datacarrier_bytes`.
    dust_relay_feerate: FeeRate
    permit_bare_multisig: bool
    max_datacarrier_bytes: int | None
    require_standard: bool
    # Core's own `-minimumchainwork`: the chain work below which
    # `main.update_ibd_status` and the `getheaders` handler in
    # `p2p.callbacks` treat the active tip as not caught up, and below
    # which `p2p.chain_sync.disconnect_if_insufficient_work` drops an
    # outbound peer. `None` given to `__init__` is the chain's own
    # `minimum_chain_work` (`btclib.consensus`), resolved once `chain`
    # above is; a value given is read exactly as given, an operator's
    # override not being clamped to the chain's own floor or ceiling,
    # matching `ChainstateManager::MinimumChainWork`'s own unconditional
    # return (`src/validation.h`, at bitcoin/bitcoin@9be056a8a7, the
    # v31.1 tag).
    minimum_chain_work: int
    # Core's own `-assumevalid`: the hash of a block whose ancestors
    # script verification may skip, `None` where it is off (`0`,
    # `-noassumevalid`, or not given: Core's default per network is not
    # read yet, btclib-org/btclib-node#1576). Nothing reads it yet.
    assume_valid: bytes | None
    # Core's own `-maxtipage`, in seconds rather than as a `timedelta`
    # for the same reason `ban_time` above is an `int`: the value is an
    # `int64_t` in Core (`ChainstateManagerOpts::max_tip_age`,
    # `src/kernel/chainstatemanager_opts.h`, same sha) with no upper
    # bound, and `timedelta` overflows past about 2.7 million years
    # where Core's own type does not. `main.update_ibd_status` compares
    # a tip's age against this, in seconds, rather than building a
    # `timedelta` from it.
    max_tip_age: int
    # (host, port) pairs, split by `_split_peers` above, host unresolved:
    # Core's own `-connect`, which dials these alone and turns off DNS
    # seeding and
    # every automatically-drawn outbound connection
    # (`InitParameterInteraction`, `src/init.cpp:814-819`, and
    # `connOptions.m_use_addrman_outgoing = false`, `src/init.cpp:2337`,
    # both at bitcoin/bitcoin@ca7162cde5). Empty for `-connect=0` too --
    # Core's own "dial nobody, but still on the -connect arm" spelling
    # (`connect.size() != 1 || connect[0] != "0"`, `src/init.cpp:2333`,
    # same sha) -- which is why `connect_given` below, not this tuple's
    # truthiness, is what `P2pManager` reads to decide the two above.
    connect: tuple[tuple[str, int], ...]
    # Whether `-connect` was named at all, `["0"]` included: Core's own
    # `!args.GetArgs("-connect").empty()`, read off the raw sequence
    # `__init__` below was given rather than off `connect` above, since
    # the two disagree on exactly that one value.
    connect_given: bool
    # `-connect`, each exactly as given: Core's own `connect`
    # (`connOptions.m_specified_outgoing`, `src/init.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), passed whole as
    # `pszDest` (`ThreadOpenConnections`, `src/net.cpp`, same sha).
    # `P2pManager._connect_peers` reads this, not `connect` above, so a
    # spec naming its own port reaches `addr_name` with it still on
    # (btclib-org/btclib-node#1493) -- `connect` above exists for its
    # own eager malformed-port refusal alone, the same split as
    # `addnode` below for the same reason.
    connect_args: tuple[str, ...]
    # `_split_peers` run over `addnode_args` below, for its own
    # malformed-port refusal alone: `P2pManager` reads `addnode_args`,
    # not this, since `-addnode`'s own list is grown and shrunk at
    # runtime by the `addnode` RPC's `add`/`remove`
    # (`add_added_peer`/`remove_added_peer`, `p2p/manager.py`), which a
    # value split once here could not follow (btclib-org/btclib-node#1350).
    addnode: tuple[tuple[str, int], ...]
    # `-addnode`, each as given: Core's own `m_added_node_params`
    # (`connOptions.m_added_nodes`, `src/init.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), dialled alongside the
    # ordinary draw and compared, as text, against a drawn address by
    # `AddedNodesContain` (`src/net.cpp`, same sha) -- both
    # `P2pManager._open_added_peers` and `_added_node` read this, not
    # `addnode` above, since `add_added_peer`/`remove_added_peer` mutate
    # it at runtime (btclib-org/btclib-node#1350).
    addnode_args: tuple[str, ...]
    # Core's own `-seednode`: peers `P2pManager` opens an `ADDR_FETCH`
    # connection to, one at a time, to draw a `getaddr` answer and
    # disconnect, ahead of the DNS seeds (`CConnman::ThreadOpenConnections`,
    # `src/net.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag).
    # Split by `_split_peers` the same as `connect` and `addnode` above,
    # host unresolved -- for its own eager malformed-port refusal alone,
    # the same reason `connect` above is kept beside `connect_args`.
    seednode: tuple[tuple[str, int], ...]
    # `-seednode`, each exactly as given, passed whole as `pszDest`
    # (`ProcessAddrFetch`, `src/net.cpp`, at bitcoin/bitcoin@9be056a8a7,
    # the v31.1 tag): `P2pManager._seednodes` reads this, not `seednode`
    # above, for the same reason `connect_args` exists beside `connect`
    # (btclib-org/btclib-node#1493).
    seednode_args: tuple[str, ...]
    # Core's own `-listen`, `DEFAULT_LISTEN` (`src/net.h`) true unless
    # `-connect` or `-maxconnections=0` is given, in which case
    # `InitParameterInteraction` (`src/init.cpp`,
    # at bitcoin/bitcoin@9be056a8a7, the v31.1 tag) soft-sets it false --
    # a default `cli.py`'s own `build_config` computes the same way, an
    # explicit `-listen`/`-nolisten` always winning over it. `False` here means
    # Core's own `-listen=0`: no bound listening socket, outbound
    # connections still made -- not `allow_p2p=False`, which unsets the
    # port and starts no `P2pManager` at all, so nothing could dial out
    # either.
    listen: bool
    # Core's own `-bind` values, as given: `cli` refuses a malformed one
    # and `parse_bind` reads each. With any, `P2pManager` binds those
    # and not every interface (`bind_on_any`, `src/init.cpp`, at
    # bitcoin/bitcoin@9be056a8a7, the v31.1 tag). Core refuses one beside
    # `-listen=0`, and its `-whitebind` is not read here.
    bind: tuple[str, ...]
    # Core's own `-externalip` values, each an address `lookup_service`
    # reads without a lookup: `cli` resolves a name before it gets here.
    # `P2pManager` records each as a local address, as `AddLocal` at
    # `LOCAL_MANUAL` does (`src/init.cpp`, same sha).
    externalip: tuple[str, ...]
    # Core's own `-discover`: whether `P2pManager` records this
    # machine's own interface addresses at all (`p2p.netif.local_addresses`,
    # btclib-org/btclib-node#1238). `InitParameterInteraction`
    # (`src/init.cpp:786-817`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    # tag) soft-sets it off under `-proxy`, `-listen=0` or `-externalip`;
    # this node has no `-proxy`, so `-listen` and `externalip` are the
    # conditions `__init__` reads, `-listen` off `self.listen` rather than
    # off the `listen` parameter, so that an explicit `-listen`'s own
    # soft-set (above) is what this one sees. `None` here is the
    # sentinel `dnsseed` above already uses for a soft default an
    # explicit value wins over. Discovery runs whether or not `_bind`
    # itself goes on to succeed, as Core's `Discover()`
    # (`src/net.cpp:3376-3384`, same sha) does: `AppInitMain` calls it
    # off `bind_on_any` (`src/init.cpp:2163`, same sha), never off
    # `fListen`, so `P2pManager` calls it unless `bind` above is given.
    discover: bool
    # Core's own `-peerblockfilters`: whether `NODE_COMPACT_FILTERS` is
    # advertised in `version` and whether a BIP157 request is answered
    # rather than refused (`p2p.connection.local_services`,
    # `p2p.callbacks._filter_range` and `.get_cfcheckpt`). Core also
    # requires `-blockfilterindex=basic` (`src/init.cpp:992-998`, same
    # sha as above) and refuses `-peerblockfilters` without it; this
    # node keeps the basic filter index unconditionally
    # (`chainstate.filter_index.FilterIndex`), so that refusal never
    # applies here.
    peerblockfilters: bool
    # Core's own `-v2transport`, `DEFAULT_V2_TRANSPORT` (`src/net.h`, at
    # bitcoin/bitcoin@9be056a8a7, the v31.1 tag) true: whether this node
    # speaks BIP324 and says so with `NODE_P2P_V2`
    # (`p2p.connection.local_services`).
    v2transport: bool
    # Whether this node speaks BIP324's predecessor, the v1 transport:
    # an inbound v1 peer is refused and no outbound v1 dial is made where
    # it is false. Core has no such option; it is this node's, for refusing
    # v1 (btclib-org/btclib-node#1190). `_v1transport` resolves it.
    v1transport: bool
    # Core's own `-maxconnections`: the automatic connections this node
    # holds at once, inbound and outbound together. It does not limit a
    # `-connect` or `-addnode` dial, which Core makes as a manual
    # connection outside it too. `P2pManager.__init__` divides it into
    # inbound and outbound slots.
    max_connections: int
    # Core's own `-dnsseed`: whether `P2pManager` asks the DNS seeds.
    # `InitParameterInteraction` (`src/init.cpp`, at bitcoin/bitcoin@9be056a8a7)
    # soft-sets it off under `-connect` or a `-maxconnections` whose
    # `int64_t` is not positive; `__init__` below computes that soft-set
    # from `max_connections` where `dnsseed` is given `None`, and takes
    # the explicit value otherwise. `cli`'s own `-listen` fallback
    # pattern, over the un-narrowed `int64_t` `-maxconnections` reads as
    # (ISS 1324), computes the soft-set itself and never leaves
    # `dnsseed` `None`.
    dnsseed: bool
    # Core's own `-forcednsseed`, `DEFAULT_FORCEDNSSEED` (`src/net.h`,
    # same sha) false: whether `P2pManager._dns_address_seed` skips its
    # wait and asks every DNS seed at once regardless of what `PeerDB`
    # already holds. Refused alongside a `dnsseed` that is false
    # (`AppInitParameterInteraction`, `src/init.cpp`, same sha).
    forcednsseed: bool
    # Core's own `-fixedseeds`, `DEFAULT_FIXEDSEEDS` true: whether
    # `P2pManager` may fall back on the chain's fixed seeds once DNS
    # seeding, `-addnode` and `-seednode` have had their chance
    # (`src/net.h`/`src/net.cpp`, same sha).
    fixed_seeds: bool
    # Core's own `-bantime`: how long a `setban` ban lasts, in seconds,
    # where the call names no length. `Node` hands it to its `BanMan`.
    ban_time: int
    # Core's own `-blocknotify`: the command `main._after_tip_change` runs
    # through the shell each time a fork commits outside initial block
    # download, `%s` replaced by the new tip's hash
    # (`notify.run_detached`). `""` is Core's own unset
    # (`GetArg("-blocknotify", "")`), which runs nothing.
    block_notify: str
    # Core's own `-startupnotify`: the command `Node.run` runs once,
    # through the shell, once its RPC listener answers and start-up has
    # finished. `""` runs nothing, as `block_notify` above.
    startup_notify: str
    # Core's own `-shutdownnotify`, one entry per value the option is
    # given -- Core reads it with `GetArgs`, not `GetArg`, unlike the
    # other three notify fields here -- each run through the shell and
    # joined before `Node.stop`'s own shutdown goes on
    # (`notify.run_shutdown_notify`).
    shutdown_notify: tuple[str, ...]
    # Core's own `-alertnotify`: the command
    # `main.check_fork_warning_conditions` runs, through the shell,
    # whenever this node raises a warning of its own, `%s` replaced by
    # the sanitized, single-quoted message (`notify.alert_notify`). `""`
    # runs nothing.
    alert_notify: str

    # every parameter here is one independent setting, not a group of
    # related ones this signature happens to expose together: `chain` is
    # not `data_dir`'s business, `rpc_host` is not `debug`'s, and nesting
    # them into sub-objects would only move each still-independent knob
    # behind one more name for every caller, all of which already read
    # this constructor by keyword (`grep -rn "Config(" tests/
    # src/btclib_node/` finds no positional call). PLR0913/PLR0917 measure a
    # count this object's whole purpose is to be flat, not a shape it
    # backed into. Keyword-only throughout (issue #341's own FBT round)
    # for the same reason: `allow_p2p`/`allow_rpc`/`pruned`/`debug` are
    # what that round's own findings are, but every other parameter here
    # is already keyword at every call site the grep above found, so
    # making only the booleans keyword-only would leave the same
    # constructor answering `Config("regtest")` for one parameter and
    # refusing it for the next -- which also drops PLR0917 (too many
    # positional arguments) below to zero, keyword-only meaning there
    # is no longer a positional count to measure. PLR0915 (too many
    # statements) measures the same flatness from the body's side: one
    # assignment per independent setting, which is what grows with every
    # field this object carries rather than a body that wants splitting.
    def __init__(  # noqa: PLR0913, PLR0915
        self,
        *,
        chain: Chain | str = DEFAULT_CHAIN,
        data_dir: str | Path | None = None,
        blocks_dir: str | Path | None = None,
        p2p_port: int | None = None,
        rpc_port: int | None = None,
        rpc_host: str | None = None,
        rpcbind: Sequence[str] = (),
        rpcallowip: Sequence[str] = (),
        whitelist: Sequence[str] = (),
        whitelist_relay: bool = True,
        whitelist_force_relay: bool = False,
        rpcservertimeout: int = int(REQUEST_TIMEOUT),
        allow_p2p: bool = True,
        allow_rpc: bool = True,
        pruned: bool = False,
        prune_target_mib: int | None = None,
        debug: bool = False,
        debug_categories: Collection[str] = (),
        debug_exclude: Collection[str] = (),
        log_rate_limit: bool = True,
        log_path: str | None = "history.log",
        min_relay_feerate: FeeRate = DEFAULT_MIN_RELAY_FEERATE,
        incremental_relay_feerate: FeeRate = DEFAULT_INCREMENTAL_RELAY_FEERATE,
        dust_relay_feerate: FeeRate = DEFAULT_DUST_RELAY_FEERATE,
        permit_bare_multisig: bool = True,
        max_datacarrier_bytes: int | None = DEFAULT_MAX_DATACARRIER_BYTES,
        require_standard: bool = True,
        minimum_chain_work: int | None = None,
        assume_valid: bytes | None = None,
        max_tip_age: int = DEFAULT_MAX_TIP_AGE,
        connect: Sequence[str] = (),
        addnode: Sequence[str] = (),
        seednode: Sequence[str] = (),
        listen: bool = True,
        bind: Sequence[str] = (),
        externalip: Sequence[str] = (),
        discover: bool | None = None,
        peerblockfilters: bool = False,
        v2transport: bool = True,
        v1transport: bool | None = None,
        max_connections: int = DEFAULT_MAX_PEER_CONNECTIONS,
        dnsseed: bool | None = None,
        forcednsseed: bool = False,
        fixed_seeds: bool = True,
        ban_time: int = DEFAULT_MISBEHAVING_BANTIME,
        block_notify: str = "",
        startup_notify: str = "",
        shutdown_notify: Sequence[str] = (),
        alert_notify: str = "",
        rpcauth: Sequence[str] = (),
        rpcuser: str = "",
        rpcpassword: str = "",
        rpccookiefile: str | Path | None = COOKIE_FILE,
        rpccookieperms: str | None = None,
        rpcwhitelist: Sequence[str] = (),
        rpcwhitelistdefault: bool | None = None,
        log_warnings: Sequence[str] = (),
        section_warning: str = "",
        config_args: Sequence[str] = (),
        config_file_line: str = "",
    ) -> None:
        """Resolve `chain`, `minimum_chain_work`'s own default, and ports."""
        self.chain = _resolve_chain(chain)
        self.minimum_chain_work = (
            self.chain.consensus.minimum_chain_work
            if minimum_chain_work is None
            else minimum_chain_work
        )
        self.assume_valid = assume_valid
        self.max_tip_age = max_tip_age

        data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir = data_dir.absolute() / self.chain.name

        self.blocks_dir = None
        if blocks_dir is not None:
            # Core's own check, before the chain-specific subdirectory
            # below is ever appended to it (`GetBlocksDirPath`, same
            # citation as the field comment): "Specified blocks directory
            # ... does not exist" (`src/init.cpp:1006`, same sha) is fatal
            # there too, not a silent `mkdir` -- unlike `data_dir` above,
            # which every caller here is content to have created on first
            # use. The path asked about is `get_path_arg`'s, as
            # `GetBlocksDirPath` reads `GetPathArg`, and the message names
            # the value as given, as Core names `GetArg("-blocksdir")`.
            resolved = Path(get_path_arg(str(blocks_dir))).absolute()
            if not resolved.is_dir():
                err_msg = f'Specified blocks directory "{blocks_dir}" does not exist.'
                raise ValueError(err_msg)
            self.blocks_dir = resolved / self.chain.name

        self.connect_given = bool(connect)
        # Core's own "-connect=0": still the -connect arm above, but
        # nobody named to dial -- `_split_peers` never sees the "0"
        # itself, checked here the same way Core's own options builder
        # special-cases the value ahead of resolving anything
        # (`connect.size() != 1 || connect[0] != "0"`, `src/init.cpp:2333`,
        # at bitcoin/bitcoin@ca7162cde5).
        self.connect = (
            () if list(connect) == ["0"] else _split_peers(connect, self.chain.port)
        )
        # `["0"]` the same "dial nobody" spelling as `connect` above,
        # rather than a literal peer named `"0"`.
        self.connect_args = () if list(connect) == ["0"] else tuple(connect)
        self.addnode = _split_peers(addnode, self.chain.port)
        self.addnode_args = tuple(addnode)
        self.seednode = _split_peers(seednode, self.chain.port)
        self.seednode_args = tuple(seednode)
        self.listen = listen
        self.bind = tuple(bind)
        self.externalip = tuple(externalip)
        self.discover = (
            self.listen and not self.externalip if discover is None else discover
        )
        self.peerblockfilters = peerblockfilters
        self.v2transport = v2transport
        self.v1transport = _v1transport(
            v1transport=v1transport, v2transport=v2transport
        )

        # `_dnsseed`'s own docstring has `AppInitParameterInteraction`'s
        # order, ahead of the `-maxconnections` refusal below.
        self.dnsseed = _dnsseed(
            dnsseed=dnsseed,
            forcednsseed=forcednsseed,
            connect_given=self.connect_given,
            max_connections=max_connections,
        )
        self.forcednsseed = forcednsseed

        _refuse_bind_without_listen(self.bind, listen=self.listen)

        if max_connections < 0:
            # Core's own wording (`AppInitParameterInteraction`, same
            # sha), fatal there too
            err_msg = "-maxconnections must be greater or equal than zero"
            raise ValueError(err_msg)
        self.max_connections = max_connections
        self.fixed_seeds = fixed_seeds
        self.ban_time = ban_time
        self.block_notify = block_notify
        self.startup_notify = startup_notify
        self.shutdown_notify = tuple(shutdown_notify)
        self.alert_notify = alert_notify

        self.p2p_port = None
        if allow_p2p:
            self.p2p_port = self.chain.port
            if p2p_port:
                self.p2p_port = p2p_port

        self.rpc_port = None
        if allow_rpc:
            self.rpc_port = self.chain.rpc_port
            if rpc_port:
                self.rpc_port = rpc_port

        self.rpc_host = rpc_host
        self.rpcbind = tuple(rpcbind)
        self.rpcallowip = tuple(rpcallowip)
        self.whitelist = tuple(whitelist)
        self.whitelist_relay = whitelist_relay
        self.whitelist_force_relay = whitelist_force_relay
        self.rpcservertimeout = rpcservertimeout
        # Core reads the RPC options below in `StartHTTPRPC` and its
        # `InitRPCAuthentication` (`src/httprpc.cpp`), which `AppInitMain`
        # runs under `-server` alone (`src/init.cpp`, both
        # at bitcoin/bitcoin@9be056a8a7): with no RPC listener a malformed
        # value refuses nothing
        if not allow_rpc:
            rpcauth, rpccookieperms, rpcwhitelist = (), None, ()
        # `InitRPCAuthentication`'s `GetArg("-rpcpassword", "") == ""`:
        # an empty password is no password
        self.rpc_password_entry = (
            RpcAuthEntry.from_password(rpcuser, rpcpassword) if rpcpassword else None
        )
        self.rpc_cookie_file = _resolve_cookie_file(rpccookiefile, self.data_dir)
        self.rpc_cookie_tmp = _resolve_cookie_file(
            rpccookiefile, self.data_dir, temp=True
        )
        # Core refuses a malformed value in `InitRPCAuthentication`,
        # after binding, so its line goes to the log and stderr gets
        # `RPC_INIT_ERROR`: kept here for `RpcAuth.start` rather than
        # raised
        self.rpc_cookie_perms, self.rpc_cookie_perms_error = None, None
        if rpccookieperms is not None and self.rpc_password_entry is None:
            self.rpc_cookie_perms, self.rpc_cookie_perms_error = _read_cookie_perms(
                rpccookieperms
            )
        self.rpc_auth, self.rpc_auth_invalid = _read_rpcauth(rpcauth)
        self.rpc_whitelist = parse_whitelist(rpcwhitelist)
        self.rpc_whitelist_default = (
            bool(rpcwhitelist) if rpcwhitelistdefault is None else rpcwhitelistdefault
        )

        self.pruned = pruned
        self.prune_target_mib = prune_target_mib

        self.debug = debug
        self.debug_categories = frozenset(debug_categories)
        self.debug_exclude = frozenset(debug_exclude)
        self.log_rate_limit = log_rate_limit
        self.log_path = log_path
        self.log_warnings = tuple(log_warnings)
        self.section_warning = section_warning
        self.config_args = tuple(config_args)
        self.config_file_line = config_file_line
        self.min_relay_feerate = min_relay_feerate
        self.incremental_relay_feerate = incremental_relay_feerate
        self.dust_relay_feerate = dust_relay_feerate
        self.permit_bare_multisig = permit_bare_multisig
        self.max_datacarrier_bytes = max_datacarrier_bytes
        self.require_standard = require_standard
