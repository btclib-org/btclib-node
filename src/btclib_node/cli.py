# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`main`, the command line `pip install btclib-node` installs.

The command line is read the way Core's `ArgsManager::ParseParameters`
reads it (`src/common/args.cpp`, at bitcoin/bitcoin@9be056a8a7), and
`_parse_parameters` below is that function ported over `_OPTIONS`, the
table of the options this module knows. A value follows `=` and never
the next argument; `--name` is `-name`; `-noname` is `-name` negated,
`_interpret_value` below; and the first argument that does not start
with `-` ends the options, refused once the configuration file has been
read, where `ParseArgs` (`src/bitcoind.cpp`, same sha) refuses it.
`argparse` is not what reads it: it takes a value from the next argument
as readily as from after `=`, and it has no negation. `-h`, `-?`,
`-help` and `-help-debug` print `_help_message` and exit `0`, as
`ProcessInitCommands` (same file) does.

Each option is registered with the flags `SetupServerArgs`
(`src/init.cpp`, same sha), `SetupChainParamsBaseOptions`
(`src/chainparamsbase.cpp`) and `AddLoggingArgs` (`src/init/common.cpp`)
give it, and each is read the way Core's own code reads it --
`build_config` below names the reader at each one. `-maxconnections`'s
default is the pinned release's rather than Core `master`'s,
`config.py`'s comment on `DEFAULT_MAX_PEER_CONNECTIONS` saying why.
`-server` is `Config.allow_rpc`, on unless negated or set false, as
`bitcoind` soft-sets it on (`src/bitcoind.cpp`). An RPC or P2P listener
that cannot start stops the node, `main` below exiting `1`, as Core's
init aborts. Three `Config` fields have no option here: `min_relay_feerate`
(Core's own `-minrelaytxfee` is BTC/kvB and this field is priced in
sat/kvB already, `config.py`'s own comment on `DEFAULT_MIN_RELAY_FEERATE`
argues why nothing enforces it yet; the unit translation is deferred
rather than done half-heartedly), `log_path` (this command always takes
`Config`'s own default -- a file under the data directory -- an operator
who wants console output can read it from there), and `allow_p2p` (the
P2P listener is always requested; `-listen=0` is what keeps it from
accepting connections).

`-debug=<category>` takes Core's logging category names
(`LOG_CATEGORIES_BY_STR`, `src/logging.cpp`, same sha) and refuses any
other with Core's own "Unsupported logging category" message
(`SetLoggingCategories`, `src/init/common.cpp`). This node has no
categories of its own, so any category turns `Config.debug` on for all
of its logging, and `0` or `none` discards the categories before it.

`-blocksdir=<dir>` names the base `BlockDB` (`block_db/__init__.py`)
writes its own files under, Core's own "default: <datadir>" applying
when it is not given -- unlike `-datadir`, it is read from
`bitcoin.conf` normally, since which file to read never depends on it
the way it depends on `-datadir`. `-blocksdir` naming a directory that
does not already exist is fatal, `Config.__init__`'s own refusal,
matching Core's "Specified blocks directory ... does not exist"
(`src/init.cpp:1006`, same sha) rather than creating one silently.

Reading Core's own `blk*.dat` is not implemented, and is not started
here either: the files are Core's format and the validation order is
this node's own, and `-connect`/`-addnode` below already deliver the
same blocks over loopback p2p with no new parser -- `Node.run`'s own
comment on dialling them is where that route is wired in.
btclib-org/btclib-node#573 is the issue this records the decision
against.

A run through this command stays one process past the start of block
download for two reasons, one enforced regardless of the other.
`Node.__init__` refuses outright inside a re-imported `__main__`
(`ReimportedMainProcessError`, issue #589 -- the guard ISS 579 asked
for, now enforced by `Node` itself rather than by a module-body
`if __name__ == "__main__":` every future caller has to remember), and
that backstop holds whichever entry point built the `Node`. The
console script `[project.scripts]` installs also never reaches that
backstop in the first place: its own generated shim carries the same
`if __name__ == "__main__":` guard `pytest`'s own `.venv/bin/pytest`
has (ISS 583's own body quotes it), and a `multiprocessing` pool
worker spawned from it re-executes that shim under `__mp_main__`
(`_fixup_main_from_path`, `multiprocessing/spawn.py`), never taking
the guard's own branch. `python -m btclib_node` needs neither
argument: `__main__.py`'s own module docstring is where the third,
narrower mechanism that exempts it -- `multiprocessing.spawn`'s
special case for any module named `*.__main__` -- is read from the
interpreter's own source rather than assumed.

## `bitcoin.conf`

Read the way Core's own `ReadConfigFiles`/`ReadConfigStream`
(`src/common/config.cpp`, at bitcoin/bitcoin@9be056a8a7) read it, the
default path itself computed by `ArgsManager::GetConfigFilePath`
(`src/common/args.cpp`, same sha): a `key=value` line per option, named
without the leading `-` an option carries on the command line; `#`
starts a comment that runs to the end of the line; a blank line and a
comment-only line are skipped; a `[section]` line switches which section
the lines under it belong to, until the next one. A key goes through
`_interpret_key` and its value through `_interpret_value`, as on the
command line: `nolisten=1` is `-listen` negated, and `regtest.port=` in
the default section is `port=` in `[regtest]`. Every chain has its own
section, `main` included (`ChainTypeToString`, `src/util/chaintype.cpp`)
-- not only the three alternate chains -- and the *default*, unlabelled
section at the top of the file applies to every chain. `chain`,
`testnet`, `signet` and `regtest` themselves are read only from the
default section and the command line, never from a chain's own section,
which is what lets a file decide the chain in the first place rather
than needing the chain decided already to know which section answers
that question.

Precedence is Core's `GetSetting` and `GetSettingsList`
(`src/common/settings.cpp`, same sha), ported as `_get_setting` and
`_get_settings_list` below: the command line over the active chain's
section over the default section; within the command line the last
value, within a file the first, the chain selectors aside; and a
negation discarding every value named before it at its own level.
`-connect`, `-addnode`, `-rpcauth`, `-rpcwhitelist`, `-rpcbind`,
`-rpcallowip` and `-debug` are lists, every value from every level
applying.

Not every option answers to the file the same way once the chain is
not `main`: `-port`, `-rpcport`, `-rpcbind`, `-connect` and `-addnode`
are each registered `NETWORK_ONLY` in Core, so the default section's own
value for one of these is ignored once running testnet, signet or
regtest -- only that chain's own section and the command line still
reach it, and a negation in the default section still does. The
`network_only` column of `_OPTIONS` is where that is written down.

`includeconf=<file>`, resolved relative to the data directory the way
Core resolves it, is read only from the root file's own default
section: Core additionally honours one named inside the active chain's
own section, which is not replicated here, the common shape being one
`includeconf=` naming a secrets file from the top of an otherwise
ordinary `bitcoin.conf`. One inside an included file is warned about and
ignored, as Core warns (`ReadConfigFiles`, same file). On the command
line `-includeconf` is refused unless negated, and `-noincludeconf`
reads no included file, both as `ParseParameters` and `ReadConfigFiles`
have it; `-noconf` reads no file at all. `conf=` inside a file is
refused -- fatally, "conf cannot be set in a configuration file" -- and
`datadir=` inside one is not read at all (unlike Core, which lets a file
move the data directory read *after* the file naming it was found): this
module needs a `-datadir` before it can know a file's own default path,
so a value the file might carry for it can never be the one that located
that same file, and honouring it for anything read afterwards would make
the same key mean two different things depending on when it is read.
Warned about on stderr with its own message rather than the generic one
below, since `datadir` is a real, documented option and not a typo the
generic message would have a reader believe it was.

A `bitcoin.conf` in the data directory that `-conf` leaves unread, by
naming another file, is refused as `InitConfig` (`src/common/init.cpp`,
same sha) refuses it, and `-allowignoredconf` makes that a warning:
`_check_ignored_conf` below.

An unrecognised key in the file is warned about, on stderr, and
ignored -- Core's own default (`ReadConfigFiles(error,
/*ignore_invalid_keys=*/true)`, called this way from `bitcoin.cpp`,
`common/init.cpp` and `bitcoin-cli.cpp` alike, same sha) rather than
the fatal alternative that flag also allows. An unrecognised option on
the command line is refused the way Core's `InitError` refuses it,
"Error: Error parsing command line arguments: Invalid parameter
<argument>" on stderr and exit 1.

A boolean, wherever it is read from, is Core's `InterpretBool`
(`src/common/args.cpp`, same sha): `_interpret_bool` below.
"""

import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from btclib_node import Node, install_signal_handlers
from btclib_node.block_db import blocks_directory
from btclib_node.config import (
    DEFAULT_MAX_PEER_CONNECTIONS,
    Config,
    get_path_arg,
    split_host_port,
)
from btclib_node.constants import MIN_PRUNE_TARGET_MIB
from btclib_node.dirlock import DirectoryLock, lock_directories
from btclib_node.exceptions import DirectoryLockError

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["build_config", "main"]

# Core's own default filename, `BITCOIN_CONF_FILENAME` (`src/init.cpp`,
# at bitcoin/bitcoin@ca7162cde5) -- kept so that an operator's existing
# `bitcoin.conf` is the file this command reads without being told to,
# which is the whole point of it reading one at all.
_DEFAULT_CONF_FILENAME = "bitcoin.conf"

# Every chain this node has a section for, `main` included -- Core's
# own `ChainTypeToString` (`src/util/chaintype.cpp`, same sha). This
# node's internal chain names (`config.py`'s `_resolve_chain`) are not
# these strings; `_CHAIN_ALIASES` below is the translation the
# `-chain=` option and a file's own `chain=` value both go through.
_CHAIN_SECTION = {
    "mainnet": "main",
    "testnet": "test",
    "signet": "signet",
    "regtest": "regtest",
}

# Core's own external `-chain=` vocabulary (`ChainTypeFromString`,
# same file), which is not this node's internal one: `main`/`test`
# rather than `mainnet`/`testnet`, predating this module and not
# renamed for it.
_CHAIN_ALIASES = {
    "main": "mainnet",
    "test": "testnet",
    "signet": "signet",
    "regtest": "regtest",
}

# `LOG_CATEGORIES_BY_STR` (`src/logging.cpp`, at
# bitcoin/bitcoin@9be056a8a7), `lock` left out as a release build leaves
# it out, it being compiled in only under `DEBUG_LOCKCONTENTION`.
_LOG_CATEGORIES = frozenset(
    {
        "net",
        "tor",
        "mempool",
        "http",
        "bench",
        "zmq",
        "walletdb",
        "rpc",
        "estimatefee",
        "addrman",
        "selectcoins",
        "reindex",
        "cmpctblock",
        "rand",
        "prune",
        "proxy",
        "mempoolrej",
        "libevent",
        "coindb",
        "qt",
        "leveldb",
        "validation",
        "i2p",
        "ipc",
        "blockstorage",
        "txreconciliation",
        "scan",
        "txpackages",
        "kernel",
        "privatebroadcast",
    }
)

# `GetLogCategory`'s own spellings of every category at once, and the two
# `SetLoggingCategories` reads as discarding the categories before them.
_DEBUG_ALL = frozenset({"", "1", "all"})
_DEBUG_NONE = frozenset({"0", "none"})

# `OptionsCategory`'s own titles (`GetHelpMessage`, `src/common/args.cpp`),
# in that enum's order, for the categories an option here belongs to.
_OPTIONS_TITLE = "Options:"
_CONNECTION_TITLE = "Connection options:"
_DEBUG_TEST_TITLE = "Debugging/Testing options:"
_CHAINPARAMS_TITLE = "Chain selection options:"
_RPC_TITLE = "RPC server options:"
_TITLES = (
    _OPTIONS_TITLE,
    _CONNECTION_TITLE,
    _DEBUG_TEST_TITLE,
    _CHAINPARAMS_TITLE,
    _RPC_TITLE,
)

# `ParseArgs`'s own prefix on a `ParseParameters` refusal.
_PARSE_ERROR = "Error parsing command line arguments: "

# `ToIntegral<uint16_t>`'s own bound on a port (`CheckHostPortOptions`).
_MAX_PORT = 0xFFFF


@dataclass(frozen=True)
class _Option:
    """One registered option: Core's `AddArg` arguments this module reads.

    `title` is `None` for a hidden option (`AddHiddenArgs`), which the
    help leaves out, and `sensitive` is Core's `SENSITIVE` flag.
    """

    param: str
    help: str
    title: str | None
    network_only: bool = False
    disallow_negation: bool = False
    debug_only: bool = False
    sensitive: bool = False


_OPTIONS: dict[str, _Option] = {
    "?": _Option("", "", None),
    "addnode": _Option(
        "=<ip>[:port]",
        "Add a node to connect to, alongside automatic connections. This option "
        "can be specified multiple times.",
        _CONNECTION_TITLE,
        network_only=True,
    ),
    "allowignoredconf": _Option(
        "",
        f"For backwards compatibility, treat an unused {_DEFAULT_CONF_FILENAME} "
        "file in the datadir as a warning, not an error.",
        _OPTIONS_TITLE,
    ),
    "blocksdir": _Option(
        "=<dir>",
        "Specify directory to hold blocks subdirectory for *.dat files "
        "(default: <datadir>)",
        _OPTIONS_TITLE,
    ),
    "chain": _Option(
        "=<chain>",
        "Use the chain <chain> (default: main). Allowed values: "
        + ", ".join(sorted(_CHAIN_ALIASES)),
        _CHAINPARAMS_TITLE,
    ),
    "conf": _Option(
        "=<file>",
        f"Specify path to read-only configuration file (default: "
        f"{_DEFAULT_CONF_FILENAME}). Relative paths will be prefixed by datadir "
        "location. Pass -noconf to read no configuration file.",
        _OPTIONS_TITLE,
    ),
    "connect": _Option(
        "=<ip>[:port]",
        "Connect only to the specified node; -noconnect disables automatic "
        "connections. This option can be specified multiple times.",
        _CONNECTION_TITLE,
        network_only=True,
    ),
    "datadir": _Option(
        "=<dir>", "Specify data directory", _OPTIONS_TITLE, disallow_negation=True
    ),
    "debug": _Option(
        "=<category>",
        "Log at DEBUG level rather than INFO (default: -nodebug, supplying "
        "<category> is optional). Any <category> turns it on for all of this "
        'node\'s logging; 0 or "none" discards the categories given before it. '
        "Valid values for <category> are Core's: 1, all, "
        + ", ".join(sorted(_LOG_CATEGORIES))
        + ". This option can be specified multiple times.",
        _DEBUG_TEST_TITLE,
    ),
    "h": _Option("", "", None),
    "help": _Option(
        "", "Print this help message and exit (also -h or -?)", _OPTIONS_TITLE
    ),
    "help-debug": _Option(
        "",
        "Print help message with debugging options and exit",
        _DEBUG_TEST_TITLE,
    ),
    "includeconf": _Option(
        "=<file>",
        "Specify additional configuration file, relative to the -datadir path "
        "(only usable from configuration file, not command line)",
        _OPTIONS_TITLE,
    ),
    "listen": _Option(
        "",
        "Accept connections from outside (default: 1 if no -connect or "
        "-maxconnections=0)",
        _CONNECTION_TITLE,
    ),
    "maxconnections": _Option(
        "=<n>",
        "Maintain at most <n> automatic connections to peers (default: "
        f"{DEFAULT_MAX_PEER_CONNECTIONS}); does not limit a peer dialled through "
        "-connect or -addnode",
        _CONNECTION_TITLE,
    ),
    "port": _Option(
        "=<port>",
        "Listen for connections on <port>",
        _CONNECTION_TITLE,
        network_only=True,
    ),
    "prune": _Option(
        "=<n>",
        "Reduce storage requirements by pruning old blocks: 1 allows manual "
        "pruning via the pruneblockchain RPC and deletes nothing on its own; "
        f"{MIN_PRUNE_TARGET_MIB} or above automatically prunes to roughly <n> "
        "MiB on disk, tracked against actual bytes under blocks/ and never "
        f"past the last 288 blocks; a value from 2 to {MIN_PRUNE_TARGET_MIB - 1} "
        "refuses to start; a negative <n> refuses to start too -- all matching "
        "Core",
        _OPTIONS_TITLE,
    ),
    "regtest": _Option(
        "",
        "Enter regression test mode, which uses a special chain in which blocks "
        "can be solved instantly. Equivalent to -chain=regtest.",
        _CHAINPARAMS_TITLE,
        debug_only=True,
    ),
    "rpcallowip": _Option(
        "=<ip>",
        "Allow JSON-RPC connections from specified source. Valid values for "
        "<ip> are a single IP (e.g. 1.2.3.4), a network/netmask (e.g. "
        "1.2.3.4/255.255.255.0), a network/CIDR (e.g. 1.2.3.4/24), all ipv4 "
        "(0.0.0.0/0), or all ipv6 (::/0). RFC4193 is allowed only if "
        "-cjdnsreachable=0. This option can be specified multiple times",
        _RPC_TITLE,
    ),
    "rpcauth": _Option(
        "=<userpw>",
        "Username and HMAC-SHA-256 hashed password for JSON-RPC connections. "
        "The field <userpw> comes in the format: <USERNAME>:<SALT>$<HASH>. "
        "A canonical python script is included in Bitcoin Core's "
        "share/rpcauth. This option can be specified multiple times",
        _RPC_TITLE,
        sensitive=True,
    ),
    "rpcbind": _Option(
        "=<addr>[:port]",
        "Bind to given address to listen for JSON-RPC connections. Do not "
        "expose the RPC server to untrusted networks such as the public "
        "internet! This option is ignored unless -rpcallowip is also passed. "
        "Port is optional and overrides -rpcport. Use [host]:port notation "
        "for IPv6. This option can be specified multiple times (default: "
        "127.0.0.1 and ::1 i.e., localhost)",
        _RPC_TITLE,
        network_only=True,
    ),
    "rpccookiefile": _Option(
        "=<loc>",
        "Location of the auth cookie. Relative paths will be prefixed by a "
        "net-specific datadir location. (default: data dir)",
        _RPC_TITLE,
    ),
    "rpccookieperms": _Option(
        "=<readable-by>",
        "Set permissions on the RPC auth cookie file so that it is readable by "
        "[owner|group|all] (default: owner)",
        _RPC_TITLE,
    ),
    "rpcpassword": _Option(
        "=<pw>", "Password for JSON-RPC connections", _RPC_TITLE, sensitive=True
    ),
    "rpcport": _Option(
        "=<port>",
        "Listen for JSON-RPC connections on <port>",
        _RPC_TITLE,
        network_only=True,
    ),
    "rpcuser": _Option(
        "=<user>", "Username for JSON-RPC connections", _RPC_TITLE, sensitive=True
    ),
    "rpcwhitelist": _Option(
        "=<whitelist>",
        "Set a whitelist to filter incoming RPC calls for a specific user. The "
        "field <whitelist> comes in the format: <USERNAME>:<rpc 1>,<rpc 2>,...,"
        "<rpc n>. If multiple whitelists are set for a given user, they are "
        "set-intersected. See -rpcwhitelistdefault documentation for "
        "information on default whitelist behavior.",
        _RPC_TITLE,
    ),
    "rpcwhitelistdefault": _Option(
        "",
        "Sets default behavior for rpc whitelisting. Unless rpcwhitelistdefault "
        "is set to 0, if any -rpcwhitelist is set, the rpc server acts as if all "
        "rpc users are subject to empty-unless-otherwise-specified whitelists. "
        "If rpcwhitelistdefault is set to 1 and no -rpcwhitelist is set, rpc "
        "server acts as if all rpc users are subject to empty whitelists.",
        _RPC_TITLE,
    ),
    "server": _Option("", "Accept JSON-RPC commands", _RPC_TITLE),
    "signet": _Option(
        "", "Use the signet chain. Equivalent to -chain=signet.", _CHAINPARAMS_TITLE
    ),
    "testnet": _Option(
        "",
        "Use the testnet3 chain. Equivalent to -chain=test.",
        _CHAINPARAMS_TITLE,
    ),
}

# A setting's value once read: a string, `False` for a negation, and
# `True` for a double negative -- Core's `SettingsValue` as
# `InterpretValue` fills it.
_Value = str | bool
# Every section of every file read, `""` the default one: Core's
# `Settings::ro_config`.
_RoConfig = dict[str, dict[str, list[_Value]]]

_COMMAND_LINE = "command line"
_NETWORK_SECTION = "network section"
_DEFAULT_SECTION = "default section"


@dataclass(frozen=True)
class _KeyInfo:
    """Core's `KeyInfo`: an option's name, its section, and its negation."""

    name: str
    section: str
    negated: bool


@dataclass
class _Settings:
    """Core's `Settings`, with `m_network`: every source a getter merges."""

    command_line: dict[str, list[_Value]]
    ro_config: _RoConfig = field(default_factory=dict)
    network: str = ""


def _interpret_key(key: str) -> _KeyInfo:
    """Return Core's `InterpretKey` of `key`: `section.`, then `no`."""
    section, dot, name = key.partition(".")
    if not dot:
        section, name = "", key
    negated = name.startswith("no")
    return _KeyInfo(name[2:] if negated else name, section, negated)


def _interpret_value(info: _KeyInfo, value: str | None, option: _Option) -> _Value:
    """Return Core's `InterpretValue`: `False` negated, `True` doubly so.

    Raises `ValueError` on a negation `option` forbids. A double
    negative, `-nofoo=0`, is warned about on stderr as Core warns about
    it, and is `True`.
    """
    if info.negated:
        if option.disallow_negation:
            err_msg = f"Negating of -{info.name} is meaningless and therefore forbidden"
            raise ValueError(err_msg)
        if value is not None and not _interpret_bool(value):
            # Core's warning writes a `SENSITIVE` option's value in clear:
            # this one writes the `****` Core's `LogArgs` writes instead.
            shown = "****" if option.sensitive else value
            sys.stderr.write(
                "warning: parsed potentially confusing double-negative "
                f"-{info.name}={shown}\n"
            )
            return True
        return False
    return "" if value is None else value


def _negated(values: list[_Value]) -> int:
    """Return how many of `values` a negation discards: Core's `negated()`."""
    for index in range(len(values), 0, -1):
        if values[index - 1] is False:
            return index
    return 0


def _parse_parameters(
    argv: Sequence[str],
) -> tuple[dict[str, list[_Value]], str | None]:
    """Return the options `argv` sets, and its first argument not an option.

    `ArgsManager::ParseParameters` (`src/common/args.cpp`, at
    bitcoin/bitcoin@9be056a8a7): a lone `-` or the first argument not
    starting with `-` ends the options, and `--name` is `-name`. Raises
    `ValueError` under `ParseArgs`'s own prefix on an unknown option or
    one naming a section -- "Invalid parameter", the argument quoted
    whole -- on a negation the option forbids, and on `-includeconf`
    not negated. The second value is `ParseArgs`'s own "unexpected
    token", the first argument anywhere in `argv` not starting with `-`,
    which `build_config` refuses once the file is read.
    """
    options: dict[str, list[_Value]] = {}
    for arg in argv:
        if arg == "-":
            break
        key, equals, text = arg.partition("=")
        if not key.startswith("-"):
            break
        if key.startswith("--"):
            key = key[1:]
        info = _interpret_key(key[1:])
        option = _OPTIONS.get(info.name)
        if option is None or info.section:
            err_msg = f"{_PARSE_ERROR}Invalid parameter {arg}"
            raise ValueError(err_msg)
        try:
            value = _interpret_value(info, text if equals else None, option)
        except ValueError as error:
            err_msg = f"{_PARSE_ERROR}{error}"
            raise ValueError(err_msg) from None
        options.setdefault(info.name, []).append(value)
    includes = options.get("includeconf", [])
    live = includes[_negated(includes) :]
    if live:
        first = "true" if live[0] is True else json.dumps(live[0], ensure_ascii=False)
        err_msg = (
            f"{_PARSE_ERROR}-includeconf cannot be used from commandline; "
            f"-includeconf={first}"
        )
        raise ValueError(err_msg)
    token = next((arg for arg in argv if not arg.startswith("-")), None)
    return options, token


def _config_options(text: str, path: str) -> list[tuple[int, str, str]]:
    """Return every `key=value` of `text`, its line and section prefix with it.

    Core's own config-file grammar (`GetConfigOptions`,
    `src/common/config.cpp`, at bitcoin/bitcoin@9be056a8a7): a key under
    a `[section]` line is returned as `section.key`. Raises `ValueError`
    on a line that is neither `key=value` nor `[section]`, on one
    starting with `-` (an option is named without it in a file), and on
    a key naming `rpcpassword` on a line holding a `#` anywhere, the
    last because a `#` may be part of the password or start a comment.
    """
    options: list[tuple[int, str, str]] = []
    prefix = ""
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line, used_hash, _ = raw.partition("#")
        line = line.strip(" \t\r\n")
        if not line:
            continue
        if line[0] == "[" and line[-1] == "]":
            prefix = line[1:-1] + "."
            continue
        if line[0] == "-":
            err_msg = (
                f"{path}:{lineno}: options in a configuration file are "
                "given without a leading -"
            )
            raise ValueError(err_msg)
        if "=" not in line:
            err_msg = f"{path}:{lineno}: not a key=value line: {line!r}"
            if line.startswith("no"):
                err_msg += (
                    ", if you intended to specify a negated option, use "
                    f"{line}=1 instead"
                )
            raise ValueError(err_msg)
        key, _, value = line.partition("=")
        name = prefix + key.strip(" \t\r\n")
        if used_hash and "rpcpassword" in name:
            err_msg = (
                f"{path}:{lineno}: using # in rpcpassword can be ambiguous and "
                "should be avoided"
            )
            raise ValueError(err_msg)
        options.append((lineno, name, value.strip(" \t\r\n")))
    return options


def _parse_conf_text(text: str, path: str) -> _RoConfig:
    """Parse `text` into `{section: {name: [values]}}`, in file order.

    `ReadConfigStream` (`src/common/config.cpp`, at
    bitcoin/bitcoin@9be056a8a7) over `_config_options`: `InterpretKey`
    and `InterpretValue` on each key. Raises `ValueError` where
    `_config_options` does, on a `conf=` key, and on a negation an
    option forbids. An unknown key, and `datadir`, are warned about on
    stderr and left out.
    """
    config: _RoConfig = {}
    for lineno, name, value in _config_options(text, path):
        info = _interpret_key(name)
        if info.name == "conf":
            err_msg = f"{path}:{lineno}: conf cannot be set in a configuration file"
            raise ValueError(err_msg)
        option = _OPTIONS.get(info.name)
        if option is None:
            sys.stderr.write(f"warning: ignoring unknown configuration value {name}\n")
            continue
        try:
            setting = _interpret_value(info, value, option)
        except ValueError as error:
            err_msg = f"{path}:{lineno}: {error}"
            raise ValueError(err_msg) from None
        if info.name == "datadir":
            sys.stderr.write(
                "warning: -datadir cannot be set in a configuration file, "
                "since the file itself has to be found first\n"
            )
            continue
        config.setdefault(info.section, {}).setdefault(info.name, []).append(setting)
    return config


def _read_conf_file(
    path: Path, *, required: bool, include: str | None = None
) -> _RoConfig:
    """Read and parse `path`; `{}` if it cannot be read and is not `required`.

    Core's own "ok to not have a config file" (`ReadConfigFiles`,
    `src/common/config.cpp`) for the default filename, which is what
    `required=False` is for. `required=True` is what `-conf` explicitly
    naming a file gets instead: a missing or unreadable one is fatal
    there, the same as Core's own "specified config file ... could not
    be opened". Core asks `stream.good()` of every file, so any `OSError`
    the read raises is what "could not be opened" is here, a missing
    file and one the process may not read alike.

    A directory is checked with `is_dir()` before the file is opened,
    matching `ReadConfigFiles`'s own `fs::is_directory(conf_path)` guard,
    which runs before the stream is ever opened rather than reading the
    failure an open attempt raises. Catching the open failure instead
    would depend on the platform: opening a directory raises
    `IsADirectoryError` (`errno.EISDIR`) on POSIX and `PermissionError`
    (`errno.EACCES`) on Windows, so a handler for one platform's
    exception class is not reached by the other's error.

    The refusals are `ReadConfigFiles`'s own words, those of an included
    file where `include` is the `includeconf` value that named it.
    """
    if path.is_dir():
        kind = "Config" if include is None else "Included config"
        err_msg = f'{kind} file "{path}" is a directory.'
        raise ValueError(err_msg)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        if include is not None:
            err_msg = f"Failed to include configuration file {include}"
            raise ValueError(err_msg) from None
        if required:
            err_msg = f'specified config file "{path}" could not be opened.'
            raise ValueError(err_msg) from None
        return {}
    return _parse_conf_text(text, str(path))


def _load_conf_tree(
    conf_path: Path, *, conf_explicit: bool, base_dir: Path, use_includes: bool
) -> _RoConfig:
    """Read `conf_path`, then every `includeconf` its default section names.

    Each included file's own sections are merged into the same tree,
    appended after the root file's own values for that section --
    `ReadConfigStream` appends into one shared `ro_config[section][key]`
    list regardless of which file contributed a value, root first
    (`ReadConfigFiles`, `src/common/config.cpp`, at
    bitcoin/bitcoin@9be056a8a7). A negated `includeconf` discards the
    names before it, as that function's own `SettingsSpan` does, and
    `use_includes=False`, for `-noincludeconf`, reads none.
    """
    tree = _read_conf_file(conf_path, required=conf_explicit)
    if not use_includes:
        return tree
    includes = tree.get("", {}).get("includeconf", [])
    for name in includes[_negated(includes) :]:
        include = _setting_to_str(name)
        include_path = Path(include)
        if not include_path.is_absolute():
            include_path = base_dir / include_path
        included = _read_conf_file(include_path, required=True, include=include)
        for section, keys in included.items():
            dest = tree.setdefault(section, {})
            for key, values in keys.items():
                if key == "includeconf":
                    for value in values:
                        sys.stderr.write(
                            "warning: -includeconf cannot be used from included "
                            f"files; ignoring -includeconf={_setting_to_str(value)}\n"
                        )
                    continue
                dest.setdefault(key, []).extend(values)
    return tree


def _sources(
    settings: _Settings, name: str, section: str
) -> list[tuple[list[_Value], str]]:
    """Return `name`'s values at each level Core's `MergeSettings` merges.

    Highest first: the command line, the network section of the file
    (where `section` names one), and its default section.
    """
    sources: list[tuple[list[_Value], str]] = []
    if name in settings.command_line:
        sources.append((settings.command_line[name], _COMMAND_LINE))
    if section and name in settings.ro_config.get(section, {}):
        sources.append((settings.ro_config[section][name], _NETWORK_SECTION))
    if name in settings.ro_config.get("", {}):
        sources.append((settings.ro_config[""][name], _DEFAULT_SECTION))
    return sources


def _use_default_section(settings: _Settings, name: str) -> bool:
    """Return Core's `UseDefaultSection`: on `main`, or not network-only."""
    return settings.network == "main" or not _OPTIONS[name].network_only


def _get_setting(
    settings: _Settings, name: str, *, get_chain_type: bool = False
) -> _Value | None:
    """Return `name`'s one value, Core's `GetSetting` (`common/settings.cpp`).

    The highest level naming it decides. There, the last value after the
    last negation, or the first in a file where `get_chain_type` is not
    set; `False` where a negation is last. A default section is skipped
    for a network-only option off `main` unless it ends negated, and
    `get_chain_type` -- `GetChainArg`'s own read -- reads the file's
    default section alone and skips a command line ending negated.
    """
    section = "" if get_chain_type else settings.network
    ignore_default = not get_chain_type and not _use_default_section(settings, name)
    for values, source in _sources(settings, name, section):
        last_negated = values[-1] is False
        if ignore_default and source == _DEFAULT_SECTION and not last_negated:
            continue
        if get_chain_type and source == _COMMAND_LINE and last_negated:
            continue
        live = values[_negated(values) :]
        if not live:
            return False
        first_wins = source != _COMMAND_LINE and not get_chain_type
        return live[0] if first_wins else live[-1]
    return None


def _get_settings_list(settings: _Settings, name: str) -> list[_Value]:
    """Return `name`'s values, Core's `GetSettingsList` (`common/settings.cpp`).

    Every level's values after its own last negation, highest level
    first. A negation stops the levels below it, except that a file's
    values still apply after a command line whose negation was followed
    by a value of its own, Core's own "zombie" values.
    """
    ignore_default = not _use_default_section(settings, name)
    result: list[_Value] = []
    done = False
    prev_negated_empty = False
    for values, source in _sources(settings, name, settings.network):
        add_zombie = source != _COMMAND_LINE and not prev_negated_empty
        if ignore_default and source == _DEFAULT_SECTION:
            continue
        if not done or add_zombie:
            result.extend(values[_negated(values) :])
        done = done or _negated(values) > 0
        prev_negated_empty = prev_negated_empty or (values[-1] is False and not result)
    return result


def _setting_to_str(value: _Value) -> str:
    """Return Core's `SettingToString`: `"0"` negated, `"1"` doubly so."""
    if value is True:
        return "1"
    if value is False:
        return "0"
    return value


def _get_arg(settings: _Settings, name: str) -> str | None:
    """Return Core's `GetArg` of `name`, `None` where nothing sets it."""
    value = _get_setting(settings, name)
    return None if value is None else _setting_to_str(value)


def _get_args(settings: _Settings, name: str) -> list[str]:
    """Return Core's `GetArgs` of `name`: every value, as strings."""
    return [_setting_to_str(value) for value in _get_settings_list(settings, name)]


def _get_bool(settings: _Settings, name: str) -> bool | None:
    """Return Core's `GetBoolArg` of `name`, `None` where nothing sets it."""
    value = _get_setting(settings, name)
    if value is None or isinstance(value, bool):
        return value
    return _interpret_bool(value)


def _get_int(settings: _Settings, name: str) -> int | None:
    """Return `name` as an integer, `None` where nothing sets it.

    `0` negated and `1` doubly so, as Core's `GetIntArg` has them. A
    string that is not an integer is refused, where Core's
    `LocaleIndependentAtoi` would read what digits it starts with.
    """
    value = _get_setting(settings, name)
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    try:
        return int(value)
    except ValueError:
        err_msg = f"{name}={value!r} is not an integer"
        raise ValueError(err_msg) from None


def _get_port(settings: _Settings, name: str) -> int | None:
    """Return `name` as a port, `None` where nothing sets it.

    `CheckHostPortOptions` (`src/init.cpp`, at bitcoin/bitcoin@9be056a8a7):
    anything but the digits of a number from 1 to 65535 is refused, `0`
    -- which a negation reads as -- included.
    """
    value = _get_arg(settings, name)
    if value is None:
        return None
    if not (value.isascii() and value.isdigit()) or not 0 < int(value) <= _MAX_PORT:
        err_msg = f"Invalid port specified in -{name}: '{value}'"
        raise ValueError(err_msg)
    return int(value)


def _is_negated(settings: _Settings, name: str) -> bool:
    """Return Core's `IsArgNegated`: the value that decides is a negation."""
    return _get_setting(settings, name) is False


def _is_set(settings: _Settings, name: str) -> bool:
    """Return Core's `IsArgSet`: something sets `name`, a negation included."""
    return _get_setting(settings, name) is not None


def _interpret_bool(value: str) -> bool:
    """Return Core's `InterpretBool` of `value`.

    `""` is true, and anything else is true where `LocaleIndependentAtoi`
    reads a non-zero integer off its front, once the whitespace Core
    trims and a leading `+` are gone: `false`, `no`, `yes` and `00` are
    false.
    """
    if not value:
        return True
    text = value.strip(" \f\n\r\t\v")
    if text.startswith("+-"):
        return False
    digits = re.match(r"-?[0-9]+", text.removeprefix("+"))
    return digits is not None and int(digits.group()) != 0


def _resolve_chain_name(settings: _Settings) -> str:
    """Resolve `-chain`/`-testnet`/`-signet`/`-regtest`: `GetChainArg`.

    `chain`/`testnet`/`signet`/`regtest` are read from the file's
    default section only, never a chain's own section -- Core's own
    `get_net` lambda passes an empty section for exactly this lookup
    (`GetChainArg`, `src/common/args.cpp`, at bitcoin/bitcoin@9be056a8a7),
    which is what lets a file decide the chain before any section but
    the default one can mean anything; and a negated selector on the
    command line is skipped there, as Core skips it. At most one of the
    four may resolve true; more is the same "Invalid combination" Core
    refuses.
    """

    def get_net(name: str) -> bool:
        value = _get_setting(settings, name, get_chain_type=True)
        if value is None or isinstance(value, bool):
            return bool(value)
        return _interpret_bool(value)

    chain_alias = _get_arg(settings, "chain")
    testnet = get_net("testnet")
    signet = get_net("signet")
    regtest = get_net("regtest")
    if sum([chain_alias is not None, testnet, signet, regtest]) > 1:
        err_msg = "invalid combination of -regtest, -signet, -testnet and -chain: use at most one"
        raise ValueError(err_msg)
    if chain_alias is not None:
        if chain_alias not in _CHAIN_ALIASES:
            err_msg = f"unknown chain {chain_alias!r}"
            raise ValueError(err_msg)
        return _CHAIN_ALIASES[chain_alias]
    if regtest:
        return "regtest"
    if signet:
        return "signet"
    if testnet:
        return "testnet"
    return "mainnet"


def _resolve_debug(settings: _Settings) -> bool:
    """Return whether any logging category is on: `SetLoggingCategories`.

    `src/init/common.cpp`, at bitcoin/bitcoin@9be056a8a7: the categories
    after the last `0` or `none`, each refused unless `GetLogCategory`
    (`src/logging.cpp`) knows it.
    """
    categories = _get_args(settings, "debug")
    discard = [i for i, category in enumerate(categories) if category in _DEBUG_NONE]
    enabled = categories[discard[-1] + 1 :] if discard else categories
    for category in enabled:
        if category not in _DEBUG_ALL and category not in _LOG_CATEGORIES:
            err_msg = f"Unsupported logging category -debug={category}."
            raise ValueError(err_msg)
    return bool(enabled)


def _help_message(*, show_debug: bool) -> str:
    """Return the help `-h` prints: `GetHelpMessage`'s groups and layout.

    `src/common/args.cpp`, at bitcoin/bitcoin@9be056a8a7: a title per
    category, the options under it in name order, each on its own line
    indented two spaces with its text below indented seven and wrapped
    to the width `HelpMessageOpt` wraps it to. An option registered
    `DEBUG_ONLY` is shown under `-help-debug` alone.
    """
    parts = ["Run a bitcoin full node over btclib.\n\nUsage: btclib-node [options]\n\n"]
    for title in _TITLES:
        parts.append(f"{title}\n\n")
        for name in sorted(_OPTIONS):
            option = _OPTIONS[name]
            if option.title != title or (option.debug_only and not show_debug):
                continue
            text = "\n       ".join(textwrap.wrap(option.help, 72))
            parts.append(f"  -{name}{option.param}\n       {text}\n\n")
    return "".join(parts)


def _check_datadir(base_dir: Path, datadir: str) -> None:
    """Refuse an explicit `-datadir` that is not an existing directory.

    Core's own `CheckDataDirOption` (`src/common/args.cpp:891`, at
    bitcoin/bitcoin@ca7162cde5) -- `datadir.empty() ||
    fs::is_directory(fs::absolute(datadir))` -- validates `-datadir` as
    a directory separately from reading the config file, and
    `InitConfig` (`src/common/init.cpp`, at bitcoin/bitcoin@9be056a8a7)
    answers "Specified data directory ... does not exist." when it
    fails, naming `datadir` as it was given; `ReadConfigFiles` checks
    again because a `datadir=` line inside the config file can still
    change it after the command-line value already passed this same
    check once. This function is
    `build_config`'s counterpart of the first call, ahead of
    `_load_conf_tree`; there is no second call here because a
    `datadir=` line inside a configuration file never reaches
    `base_dir` at all -- `_parse_conf_text` (above) recognises the
    key, warns on stderr, and drops it rather than ever applying it.

    Missing and blocked-by-a-file are the same refusal here, matching
    Core exactly: `fs::is_directory` answers `False` for both, and so
    does `is_dir()`, so nothing here needs to tell them apart. The
    default (unset `-datadir`) path is never checked at all, matching
    Core's own `datadir.empty()` bypass -- `build_config` below only
    calls this when `-datadir` names a directory -- and keeps the lazy
    creation `Node.__init__`'s own `mkdir(exist_ok=True, parents=True)`
    (`__init__.py`) already gives it, the same shape Core's own default
    path gets from `GetBlocksDirPath`'s `fs::create_directories`.
    """
    if not base_dir.is_dir():
        err_msg = f'Specified data directory "{datadir}" does not exist.'
        raise ValueError(err_msg)


def _quoted(text: str) -> str:
    """Return Core's `fs::quoted`: `std::quoted` with `&` as its escape."""
    return '"' + text.replace("&", "&&").replace('"', '&"') + '"'


def _check_ignored_conf(
    settings: _Settings, base_dir: Path, conf_path: Path | None
) -> None:
    """Refuse a `bitcoin.conf` in `base_dir` that `-conf` leaves unread.

    `InitConfig` (`src/common/init.cpp`, at bitcoin/bitcoin@9be056a8a7), and
    its message. `conf_path`, the file read, `None` under `-noconf`, is
    compared with `base_dir`'s own as `fs::equivalent` compares them, and an
    `OSError` is refused as `InitConfig`'s `catch` refuses an exception, in
    Python's words rather than the C++ library's. The message's paths are
    Core's: `-datadir` and `-conf` lexically normal (`GetPathArg`), the
    first made absolute and a relative `-conf` joined to it
    (`AbsPathForConfigVal`), as `_read_settings` reads them.
    `-allowignoredconf` makes the refusal a warning on stderr, as this
    module's other warnings are, where Core logs it. Core's other source,
    "data directory", is a `datadir=` line that moved the data directory,
    which `_parse_conf_text` drops; and the line Core logs under `-noconf`
    is not written.
    """
    base_config = base_dir / _DEFAULT_CONF_FILENAME
    if conf_path is None or not base_config.exists():
        return
    # strings rather than `Path`, which drops the `.` segment Core keeps:
    # `-datadir=.` is shown as "<cwd>/.", and `-conf=other.conf` under it
    # as "<cwd>/./other.conf"; `fs::absolute` asks for the working
    # directory only where the path is relative
    base = str(base_dir)
    try:
        if conf_path.samefile(base_config):
            return
        if datadir := _get_arg(settings, "datadir"):
            base = get_path_arg(datadir)
            if not os.path.isabs(base):  # noqa: PTH117
                base = os.path.join(os.getcwd(), base)  # noqa: PTH109, PTH118
    except OSError as os_error:
        raise ValueError(str(os_error)) from None
    conf = _get_arg(settings, "conf") or ""
    config = os.path.join(base, get_path_arg(conf or _DEFAULT_CONF_FILENAME))  # noqa: PTH118
    name = _quoted(_DEFAULT_CONF_FILENAME)
    error = (
        f"Data directory {_quoted(base)} contains a {name} file which is ignored, "
        f"because a different configuration file {_quoted(config)} from command "
        f"line argument {_quoted('-conf=' + conf)} is being used instead. Possible "
        "ways to address this would be to:\n"
        f"- Delete or rename the {name} file in data directory {_quoted(base)}.\n"
        "- Change datadir= or conf= options to specify one configuration file, not "
        "two, and use includeconf= to include any other configuration files."
    )
    if _get_bool(settings, "allowignoredconf"):
        sys.stderr.write(f"warning: {error}\n")
        return
    error += (
        "\n- Set allowignoredconf=1 option to treat this condition as a warning, "
        "not an error."
    )
    raise ValueError(error)


def _check_prune(prune: int) -> None:
    """Refuse a `-prune` Core refuses (`node/blockmanager_args.cpp`)."""
    if prune < 0:
        # Core's own wording, node::ApplyArgsManOptions
        # (node/blockmanager_args.cpp:23-25, at bitcoin/bitcoin@ca7162cde5):
        # `if (nPruneArg < 0) return util::Error{_("Prune cannot be
        # configured with a negative value.")};` -- bitcoind refuses to
        # start rather than treating a negative value as "pruning is on".
        err_msg = "Prune cannot be configured with a negative value."
        raise ValueError(err_msg)
    if 1 < prune < MIN_PRUNE_TARGET_MIB:
        # Core's own wording, node::ApplyArgsManOptions
        # (node/blockmanager_args.cpp:31-33, at bitcoin/bitcoin@ca7162cde5):
        # `return util::Error{strprintf(_("Prune configured below the
        # minimum of %d MiB.  Please use a higher number."),
        # MIN_DISK_SPACE_FOR_BLOCK_FILES / 1_MiB)};` -- 1 is manual
        # pruning, not a MiB target, so it is the one value below this
        # floor Core still accepts.
        err_msg = (
            f"Prune configured below the minimum of {MIN_PRUNE_TARGET_MIB} MiB.  "
            "Please use a higher number."
        )
        raise ValueError(err_msg)


def _read_settings(argv: Sequence[str]) -> tuple[_Settings, Path, str]:
    """Return `argv`'s settings, file included, data directory and chain.

    `ParseArgs` (`src/bitcoind.cpp`, at bitcoin/bitcoin@9be056a8a7) in
    its order: the command line, then the file and the chain it selects,
    then the refusal of an argument that is not an option; and after it
    `ProcessInitCommands`'s help, printed to stdout with exit `0`.
    """
    options, token = _parse_parameters(argv)
    settings = _Settings(options)

    # `-datadir` and `-conf` are read by `get_path_arg`, lexically normal
    # before the file system is asked anything: `missing/..` and
    # `symlink/..` are the directory they are written in. `-datadir` is
    # made absolute as `GetDataDir` makes it (`src/common/args.cpp`, at
    # bitcoin/bitcoin@9be056a8a7). A value that is `.` once normal is
    # where a path a refusal names still differs from Core's: Core joins
    # the `.` on and `Path` drops it, so `-datadir=.` names "<cwd>/x"
    # where Core names "<cwd>/./x" (btclib-org/btclib-node#1273).
    datadir = _get_arg(settings, "datadir")
    base_dir = Path.home() / ".btclib"
    if datadir:
        base_dir = Path(get_path_arg(datadir))
        if not base_dir.is_absolute():
            base_dir = Path.cwd() / base_dir
        _check_datadir(base_dir, datadir)
    conf_path = None
    if not _is_negated(settings, "conf"):
        conf = _get_arg(settings, "conf")
        conf_value = Path(get_path_arg(conf) if conf else _DEFAULT_CONF_FILENAME)
        conf_path = conf_value if conf_value.is_absolute() else base_dir / conf_value
        try:
            settings.ro_config = _load_conf_tree(
                conf_path,
                conf_explicit=_is_set(settings, "conf"),
                base_dir=base_dir,
                use_includes="includeconf" not in settings.command_line,
            )
        except ValueError as error:
            # `InitConfig`'s own prefix on a `ReadConfigFiles` refusal
            err_msg = f"Error reading configuration file: {error}"
            raise ValueError(err_msg) from None
    chain_name = _resolve_chain_name(settings)
    settings.network = _CHAIN_SECTION[chain_name]
    _check_ignored_conf(settings, base_dir, conf_path)

    if token is not None:
        err_msg = (
            f"Command line contains unexpected token '{token}', "
            "see btclib-node -h for a list of options."
        )
        raise ValueError(err_msg)
    if any(_is_set(settings, name) for name in ("?", "h", "help", "help-debug")):
        sys.stdout.write(
            _help_message(show_debug=bool(_get_bool(settings, "help-debug")))
        )
        raise SystemExit(0)
    return settings, base_dir, chain_name


@dataclass(frozen=True)
class _BeforeLock:
    """What `_before_lock` read, for `_after_lock` to finish the `Config`.

    `directories` is a `Config` of the chain, the data directory and
    `-blocksdir`, the fields that name the directories `Node.__init__`
    locks, and of `-maxconnections`, which `Config.__init__` refuses just
    after a missing blocks directory, as Core does.
    """

    settings: _Settings
    base_dir: Path
    chain_name: str
    blocksdir: str | None
    max_connections: int
    debug: bool
    prune: int
    directories: Config


def _before_lock(argv: Sequence[str]) -> _BeforeLock:
    """Read `argv` and its file, and refuse what Core refuses before its lock.

    `InitConfig`, then `AppInitParameterInteraction` (`src/init.cpp`, at
    bitcoin/bitcoin@9be056a8a7) in its order: a missing blocks directory,
    a negative `-maxconnections`, `-debug`'s categories, `-prune`.
    """
    settings, base_dir, chain_name = _read_settings(argv)
    # `GetBlocksDirPath`: a negated `-blocksdir` is an empty path, which
    # `fs::absolute` reads as the working directory
    blocksdir = _get_arg(settings, "blocksdir")
    if _is_negated(settings, "blocksdir"):
        blocksdir = ""
    max_connections = _get_int(settings, "maxconnections")
    if max_connections is None:
        max_connections = DEFAULT_MAX_PEER_CONNECTIONS
    directories = Config(
        chain=chain_name,
        data_dir=base_dir,
        blocks_dir=blocksdir,
        max_connections=max_connections,
    )
    debug = _resolve_debug(settings)
    prune = _get_int(settings, "prune") or 0
    _check_prune(prune)
    return _BeforeLock(
        settings,
        base_dir,
        chain_name,
        blocksdir,
        max_connections,
        debug,
        prune,
        directories,
    )


def _lock(directories: Config) -> tuple[DirectoryLock, ...]:
    """Lock the directories `Node.__init__` locks, as `Node.__init__` does.

    Core's `AppInitLockDirectories` (`src/init.cpp`, at
    bitcoin/bitcoin@9be056a8a7), between `_before_lock` and `_after_lock`.
    `Node.__init__` takes the same locks again, which a process already
    holding them is granted (`dirlock`), so `main` releases these once
    the `Node` holds its own.
    """
    blocks_dir = blocks_directory(directories.data_dir, directories.blocks_dir)
    directories.data_dir.mkdir(exist_ok=True, parents=True)
    blocks_dir.mkdir(exist_ok=True, parents=True)
    return lock_directories(directories.data_dir, blocks_dir)


def _after_lock(before: _BeforeLock) -> Config:
    """Refuse what Core refuses after its lock, and return the `Config`.

    `AppInitMain` (`src/init.cpp`, at bitcoin/bitcoin@9be056a8a7) in its
    order: `CheckHostPortOptions`'s `-port`, `-rpcport` and `-rpcbind`,
    then `Config.__init__`'s `-rpccookieperms` and `-rpcauth`, which
    `StartHTTPRPC` reads in that order.
    """
    settings = before.settings
    p2p_port = _get_port(settings, "port")
    rpc_port = _get_port(settings, "rpcport")
    # Every `-rpcbind` value is checked, as `CheckHostPortOptions` checks
    # it; `RpcManager` binds them beside `-rpcallowip`, as
    # `HTTPBindAddresses` (`src/httpserver.cpp`, same sha) does
    rpcbind = _get_args(settings, "rpcbind")
    for value in rpcbind:
        try:
            split_host_port(value, 0)
        except ValueError:
            err_msg = f"Invalid port specified in -rpcbind: '{value}'"
            raise ValueError(err_msg) from None

    connect = _get_args(settings, "connect")
    # `-noconnect` is Core's `-connect=0`: no automatic connection, and
    # nobody named (`src/init.cpp`, at bitcoin/bitcoin@9be056a8a7)
    connect_negated = _is_negated(settings, "connect")
    # `InitParameterInteraction`'s soft-set, which an explicit value wins
    # over (`src/init.cpp`, same sha)
    listen = _get_bool(settings, "listen")
    if listen is None:
        listen = not connect and not connect_negated and before.max_connections > 0
    # `GetAuthCookieFile` (`src/rpc/request.cpp`, same sha): negated, no cookie
    rpccookiefile = (
        None
        if _is_negated(settings, "rpccookiefile")
        else _get_arg(settings, "rpccookiefile") or ""
    )
    server = _get_bool(settings, "server")
    prune = before.prune

    return Config(
        chain=before.chain_name,
        data_dir=before.base_dir,
        blocks_dir=before.blocksdir,
        p2p_port=p2p_port,
        rpc_port=rpc_port,
        rpcbind=tuple(rpcbind),
        rpcallowip=_get_args(settings, "rpcallowip"),
        allow_rpc=server is None or server,
        pruned=bool(prune),
        prune_target_mib=prune if prune >= MIN_PRUNE_TARGET_MIB else None,
        debug=before.debug,
        connect=connect or (["0"] if connect_negated else []),
        addnode=_get_args(settings, "addnode"),
        listen=listen,
        max_connections=before.max_connections,
        rpcauth=_get_args(settings, "rpcauth"),
        rpcuser=_get_arg(settings, "rpcuser") or "",
        rpcpassword=_get_arg(settings, "rpcpassword") or "",
        rpccookiefile=rpccookiefile,
        rpccookieperms=_get_arg(settings, "rpccookieperms"),
        rpcwhitelist=_get_args(settings, "rpcwhitelist"),
        rpcwhitelistdefault=_get_bool(settings, "rpcwhitelistdefault"),
    )


def build_config(argv: Sequence[str] | None = None) -> Config:
    """Parse `argv` (`sys.argv[1:]` if `None`) and its `-conf` into a `Config`.

    Raises `ValueError` on a malformed argument, a malformed
    configuration file, or an unknown chain, in the order `bitcoind`
    refuses them, and `SystemExit(0)` once the help is printed. No lock
    is taken: `main` takes it between `_before_lock` and `_after_lock`.
    """
    return _after_lock(_before_lock(sys.argv[1:] if argv is None else argv))


def main(argv: Sequence[str] | None = None) -> None:
    """Build a `Config` from the command line and `bitcoin.conf`, and run it.

    Waits on the node's thread until a signal `install_signal_handlers`
    below caught, or the `stop` RPC, stops it. A `build_config` refusal, the
    `DirectoryLockError` of a directory another process holds, taken between
    the refusals Core makes before its lock and those it makes after
    (`_before_lock` and `_after_lock` above), or each of `Node.init_errors`
    where the node's start-up failed, is printed as `Error: <message>` and
    the exit status is `1`: Core's `InitError`, and `CConnman`'s own
    `MSG_ERROR` for a failed bind, reach stderr through
    `noui_ThreadSafeMessageBox` with that caption (`src/noui.cpp:22-46`, at
    bitcoin/bitcoin@9be056a8a7), and `bitcoind` exits `EXIT_FAILURE`.
    """
    try:
        before = _before_lock(sys.argv[1:] if argv is None else argv)
        locks = _lock(before.directories)
    except (ValueError, DirectoryLockError) as error:
        sys.stderr.write(f"Error: {error}\n")
        raise SystemExit(1) from error
    # `Node.__init__` takes the same locks, and holds them once these go
    try:
        try:
            config = _after_lock(before)
        except ValueError as error:
            sys.stderr.write(f"Error: {error}\n")
            raise SystemExit(1) from error
        node = Node(config=config)
    finally:
        for lock in locks:
            lock.release()
    install_signal_handlers(node)
    node.start()
    node.join()
    if node.init_errors:
        for message in node.init_errors:
            sys.stderr.write(f"Error: {message}\n")
        raise SystemExit(1)
