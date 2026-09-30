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
init aborts. `-minrelaytxfee` is BTC/kvB, as Core's, and
`Config.min_relay_feerate` the same rate in sat/kvB. Two `Config` fields
have no option here: `log_path` (this command always takes
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
`-connect`, `-addnode`, `-seednode`, `-rpcauth`, `-rpcwhitelist`,
`-rpcbind`, `-rpcallowip` and `-debug` are lists, every value from
every level applying.

Not every option answers to the file the same way once the chain is
not `main`: `-port`, `-rpcport`, `-rpcbind`, `-connect` and `-addnode`
are each registered `NETWORK_ONLY` in Core, so the default section's own
value for one of these is ignored once running testnet, signet or
regtest -- only that chain's own section and the command line still
reach it, and a negation in the default section still does. The
`network_only` column of `_OPTIONS` is where that is written down.
Ignored is not silent: `_check_network_only_args` refuses to start
where that leaves such an option set only in the default section, as
`AppInitParameterInteraction` refuses it (btclib-org/btclib-node#1327).
`-bind` is `NETWORK_ONLY` in Core too and is not among `_OPTIONS` at
all, a gap of its own this does not close.

`includeconf=<file>`, resolved relative to the data directory the way
Core resolves it, is read from the root file's section for the chain it
selects, then from its default section, as Core reads it. One inside
an included file is warned about and ignored, as Core warns
(`ReadConfigFiles`, same file). On the command
line `-includeconf` is refused unless negated, and `-noincludeconf`
reads no included file, both as `ParseParameters` and `ReadConfigFiles`
have it; `-noconf` reads no file at all. `conf=` inside a file is
refused -- fatally, in Core's words, pointing at `includeconf=` -- and
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

An unrecognised key in the file is warned about, in the log alone as
Core logs it, and ignored -- Core's own default (`ReadConfigFiles(error,
/*ignore_invalid_keys=*/true)`, called this way from `bitcoin.cpp`,
`common/init.cpp` and `bitcoin-cli.cpp` alike, same sha) rather than
the fatal alternative that flag also allows. An unrecognised option on
the command line is refused the way Core's `InitError` refuses it,
"Error: Error parsing command line arguments: Invalid parameter
<argument>" on stderr and exit 1.

A boolean, wherever it is read from, is Core's `InterpretBool`
(`src/common/args.cpp`, same sha): `_interpret_bool` below.
"""

import io
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from btclib.fee import FeeRate

from btclib_node import Node, install_signal_handlers
from btclib_node.block_db import blocks_directory
from btclib_node.config import (
    DEFAULT_MAX_PEER_CONNECTIONS,
    DEFAULT_MIN_RELAY_FEERATE,
    Config,
    get_path_arg,
    split_host_port,
)
from btclib_node.constants import MIN_PRUNE_TARGET_MIB
from btclib_node.dirlock import DirectoryLock, lock_directories
from btclib_node.exceptions import DirectoryLockError
from btclib_node.p2p.banman import DEFAULT_MISBEHAVING_BANTIME

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
# `LocaleIndependentAtoi`: what `TrimStringView` trims by default, and
# the integer `std::from_chars` reads at the start of what is left
_TRIMMED = " \f\n\r\t\v"
_LEADING_INTEGER = re.compile(r"-?[0-9]+")
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
_INT_MIN = -(2**31)
# the MiB a `uint64_t` byte count wraps at
_PRUNE_MIB_WRAP = 2**64 // 2**20

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
_NODE_RELAY_TITLE = "Node relay options:"
_RPC_TITLE = "RPC server options:"
_TITLES = (
    _OPTIONS_TITLE,
    _CONNECTION_TITLE,
    _DEBUG_TEST_TITLE,
    _CHAINPARAMS_TITLE,
    _NODE_RELAY_TITLE,
    _RPC_TITLE,
)

# `ParseArgs`'s own prefix on a `ParseParameters` refusal.
_PARSE_ERROR = "Error parsing command line arguments: "

# `ToIntegral<uint16_t>`'s own bound on a port (`CheckHostPortOptions`).
_MAX_PORT = 0xFFFF

# `COIN` and `MAX_MONEY` (`src/consensus/amount.h`), what `ParseMoney` reads
# an amount against
_COIN = 100_000_000
_MAX_MONEY = 21_000_000 * _COIN
# the digits `ParseMoney` reads after the point, and before it: its own
# "guard against 63 bit overflow"
_MONEY_DECIMALS = 8
_MONEY_WHOLE_DIGITS = 10


def _format_money(amount: int) -> str:
    """Return Core's `FormatMoney`: eight decimals, trimmed down to two."""
    whole, fraction = divmod(amount, _COIN)
    return f"{whole}.{f'{fraction:08d}'.rstrip('0').ljust(2, '0')}"


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
    "bantime": _Option(
        "=<n>",
        "Default duration (in seconds) of manually configured bans (default: "
        f"{DEFAULT_MISBEHAVING_BANTIME})",
        _CONNECTION_TITLE,
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
    "discover": _Option(
        "",
        "Discover own IP addresses (default: 1 when listening)",
        _CONNECTION_TITLE,
    ),
    "dnsseed": _Option(
        "",
        "Query for peer addresses via DNS lookup, if low on addresses "
        "(default: 1 unless -connect used or -maxconnections=0)",
        _CONNECTION_TITLE,
    ),
    "fixedseeds": _Option(
        "",
        "Allow fixed seeds if DNS seeds don't provide peers (default: 1)",
        _CONNECTION_TITLE,
    ),
    "forcednsseed": _Option(
        "",
        "Always query for peer addresses via DNS lookup (default: 0)",
        _CONNECTION_TITLE,
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
    "minrelaytxfee": _Option(
        "=<amt>",
        "Fees (in BTC/kvB) smaller than this are considered zero fee for "
        "relaying, mining and transaction creation (default: "
        f"{_format_money(DEFAULT_MIN_RELAY_FEERATE.sats_per_kvbyte)})",
        _NODE_RELAY_TITLE,
    ),
    "maxconnections": _Option(
        "=<n>",
        "Maintain at most <n> automatic connections to peers (default: "
        f"{DEFAULT_MAX_PEER_CONNECTIONS}); does not limit a peer dialled through "
        "-connect or -addnode",
        _CONNECTION_TITLE,
    ),
    "peerblockfilters": _Option(
        "",
        "Serve compact block filters to peers per BIP 157 (default: 0)",
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
    "seednode": _Option(
        "=<ip>[:port]",
        "Connect to a node to retrieve peer addresses, and disconnect. This "
        "option can be specified multiple times to connect to multiple nodes. "
        "During startup, seednodes will be tried before dnsseeds.",
        _CONNECTION_TITLE,
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

# The sections `GetUnrecognizedSections` (`src/common/args.cpp`, same
# sha) does not warn about: every `ChainTypeToString`, `testnet4`
# included though this node runs no such chain.
_RECOGNIZED_SECTIONS = frozenset({"main", "test", "testnet4", "signet", "regtest"})

# Where a section of a file was named: Core's `SectionInfo`, its name,
# the file as `ReadConfigFiles` names it, and the line.
_SectionInfo = tuple[str, str, int]

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
    # Core's `m_config_sections`: every section the files read named
    config_sections: list[_SectionInfo] = field(default_factory=list)
    # what Core logs of its settings: the warnings it buffers while
    # reading them, then the unrecognised-section warning, which it logs
    # after its version line (a line history.log does not have, #1309).
    # `Node` logs them in that order once its own log is open
    log_warnings: list[str] = field(default_factory=list)


def _interpret_key(key: str) -> _KeyInfo:
    """Return Core's `InterpretKey` of `key`: `section.`, then `no`."""
    section, dot, name = key.partition(".")
    if not dot:
        section, name = "", key
    negated = name.startswith("no")
    return _KeyInfo(name[2:] if negated else name, section, negated)


def _interpret_value(
    info: _KeyInfo,
    value: str | None,
    option: _Option,
    warnings: list[str],
) -> _Value:
    """Return Core's `InterpretValue`: `False` negated, `True` doubly so.

    Raises `ValueError` on a negation `option` forbids. A double
    negative, `-nofoo=0`, is `True`, and its warning is appended to
    `warnings`, for the log alone, as Core's `LogWarning`
    (`src/common/args.cpp`, at bitcoin/bitcoin@9be056a8a7).
    """
    if info.negated:
        if option.disallow_negation:
            err_msg = f"Negating of -{info.name} is meaningless and therefore forbidden"
            raise ValueError(err_msg)
        if value is not None and not _interpret_bool(value):
            # Core's warning writes a `SENSITIVE` option's value in clear:
            # this one writes the `****` Core's `LogArgs` writes instead.
            shown = "****" if option.sensitive else value
            warnings.append(
                f"Parsed potentially confusing double-negative -{info.name}={shown}"
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
    argv: Sequence[str], warnings: list[str]
) -> tuple[dict[str, list[_Value]], str | None]:
    """Return the options `argv` sets, and its first argument not an option.

    `warnings` is `_interpret_value`'s.

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
            value = _interpret_value(info, text if equals else None, option, warnings)
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


def _config_options(
    text: str, sections: list[_SectionInfo] | None = None, filepath: str = ""
) -> list[tuple[str, str]]:
    """Return every `key=value` of `text`, with its section prefix.

    Every section named is appended to `sections`, where given, with
    `filepath` and its line: a `[section]` line, and the part of a key
    before its last `.` where that `.` sits at or past the length of the
    `[section]` prefix, as `GetConfigOptions` appends to Core's
    `sections`.

    Core's own config-file grammar (`GetConfigOptions`,
    `src/common/config.cpp`, at bitcoin/bitcoin@9be056a8a7): a key under
    a `[section]` line is returned as `section.key`. Raises `ValueError`
    in Core's words on a line that is neither `key=value` nor
    `[section]`, on one starting with `-` (an option is named without it
    in a file), and on a key naming `rpcpassword` on a line holding a `#`
    anywhere, the last because a `#` may be part of the password or start
    a comment. A line ends at a newline alone, as `std::getline` ends
    one, so that the number in a refusal is the one Core counts.
    """
    options: list[tuple[str, str]] = []
    prefix = ""
    for lineno, raw in enumerate(text.split("\n"), start=1):
        line, used_hash, _ = raw.partition("#")
        line = line.strip(" \t\r\n")
        if not line:
            continue
        if line[0] == "[" and line[-1] == "]":
            prefix = line[1:-1] + "."
            if sections is not None:
                sections.append((line[1:-1], filepath, lineno))
            continue
        if line[0] == "-":
            err_msg = (
                f"parse error on line {lineno}: {line}, options in "
                "configuration file must be specified without leading -"
            )
            raise ValueError(err_msg)
        if "=" not in line:
            err_msg = f"parse error on line {lineno}: {line}"
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
                f"parse error on line {lineno}, using # in rpcpassword can be "
                "ambiguous and should be avoided"
            )
            raise ValueError(err_msg)
        options.append((name, value.strip(" \t\r\n")))
        dot = name.rfind(".")
        if sections is not None and dot != -1 and len(prefix) <= dot:
            sections.append((name[:dot], filepath, lineno))
    return options


def _parse_conf_text(
    text: str,
    sections: list[_SectionInfo] | None = None,
    filepath: str = "",
    *,
    warnings: list[str],
) -> _RoConfig:
    """Parse `text` into `{section: {name: [values]}}`, in file order.

    `sections` and `filepath` are `_config_options`', `warnings`
    `_interpret_value`'s.

    `ReadConfigStream` (`src/common/config.cpp`, at
    bitcoin/bitcoin@9be056a8a7) over `_config_options`: `InterpretKey`
    and `InterpretValue` on each key. Raises `ValueError` where
    `_config_options` does, on a `conf=` key, and on a negation an
    option forbids, each in `IsConfSupported`'s and `InterpretValue`'s
    words, which name no line. An unknown key is left out, and its
    warning appended to `warnings` for the log alone, as Core's
    `LogWarning` there; `datadir` is left out, and warned about on stderr.
    """
    config: _RoConfig = {}
    for name, value in _config_options(text, sections, filepath):
        info = _interpret_key(name)
        if info.name == "conf":
            err_msg = (
                "conf cannot be set in the configuration file; use includeconf= "
                "if you want to include additional config files"
            )
            raise ValueError(err_msg)
        option = _OPTIONS.get(info.name)
        if option is None:
            warnings.append(f"Ignoring unknown configuration value {name}")
            continue
        setting = _interpret_value(info, value, option, warnings)
        if info.name == "datadir":
            sys.stderr.write(
                "warning: -datadir cannot be set in a configuration file, "
                "since the file itself has to be found first\n"
            )
            continue
        config.setdefault(info.section, {}).setdefault(info.name, []).append(setting)
    return config


def _read_conf_file(  # noqa: PLR0913
    path: str | Path,
    *,
    required: bool,
    include: str | None = None,
    sections: list[_SectionInfo] | None = None,
    filepath: str = "",
    warnings: list[str],
) -> _RoConfig:
    """Read and parse `path`; `{}` if it cannot be read and is not `required`.

    `sections` and `filepath` are `_config_options`', `filepath` being
    the name `path` is given in a warning, and `warnings`
    `_interpret_value`'s.

    Core's own "ok to not have a config file" (`ReadConfigFiles`,
    `src/common/config.cpp`) for the default filename, which is what
    `required=False` is for. `required=True` is what `-conf` explicitly
    naming a file gets instead: a missing or unreadable one is fatal
    there, the same as Core's own "specified config file ... could not
    be opened". Core asks `stream.good()` of every file, so any `OSError`
    the read raises is what "could not be opened" is here, a missing
    file and one the process may not read alike.

    A directory is checked with `os.path.isdir` before the file is
    opened, matching `ReadConfigFiles`'s own `fs::is_directory(conf_path)`
    guard, which runs before the stream is ever opened rather than
    reading the failure an open attempt raises. Catching the open failure
    instead would depend on the platform: opening a directory raises
    `IsADirectoryError` (`errno.EISDIR`) on POSIX and `PermissionError`
    (`errno.EACCES`) on Windows, so a handler for one platform's
    exception class is not reached by the other's error.

    `path` is taken as given -- a plain string where the caller built one
    by joining onto a base directory, since `pathlib.Path` drops a `.`
    component on construction and on `/` alike (btclib-org/btclib-node#1273),
    where `os.path.isdir` and `open` resolve it exactly as the platform
    resolves `fs::is_directory`/`std::ifstream` and are indifferent to
    which one they are given.

    The refusals are `ReadConfigFiles`'s own words, those of an included
    file where `include` is the `includeconf` value that named it.
    """
    if os.path.isdir(path):  # noqa: PTH112
        kind = "Config" if include is None else "Included config"
        err_msg = f'{kind} file "{path}" is a directory.'
        raise ValueError(err_msg)
    try:
        # `newline=""`: universal newlines would end a line at a lone
        # `\r` too, where `std::getline` ends one at `\n` alone.
        # `surrogateescape`: Core reads the file as bytes and decodes
        # nothing, so a byte UTF-8 does not accept is kept, as a lone
        # surrogate `rpc.auth.to_bytes` and the streams `main` writes turn
        # back into that byte
        with open(  # noqa: PTH123
            path, encoding="utf-8", errors="surrogateescape", newline=""
        ) as conf_file:
            text = conf_file.read()
    except OSError:
        if include is not None:
            err_msg = f"Failed to include configuration file {include}"
            raise ValueError(err_msg) from None
        if required:
            err_msg = f'specified config file "{path}" could not be opened.'
            raise ValueError(err_msg) from None
        return {}
    return _parse_conf_text(text, sections, filepath, warnings=warnings)


def _load_conf_tree(  # noqa: PLR0913
    conf_path: str | Path,
    *,
    conf_explicit: bool,
    base_dir: str | Path,
    use_includes: bool,
    sections: list[_SectionInfo] | None = None,
    command_line: dict[str, list[_Value]] | None = None,
    warnings: list[str],
) -> _RoConfig:
    """Read `conf_path`, then every `includeconf` it names for its chain.

    `ReadConfigFiles` (`src/common/config.cpp`, at bitcoin/bitcoin@9be056a8a7)
    in its order: the root file, then the `includeconf` values of the
    section of the chain `command_line` and the root file select, then
    those of the default section. A negated `includeconf` discards the
    names before it, as that function's own `SettingsSpan` does, and
    `use_includes=False`, for `-noincludeconf`, reads none. The chain is
    resolved before any included file is read, so a conflicting one is
    refused first, as Core refuses it.

    Each included file's own sections are merged into the same tree,
    appended after the values already there -- `ReadConfigStream`
    appends into one shared `ro_config[section][key]` list regardless of
    which file contributed a value. An `includeconf` an included file
    adds to either section is warned about and not read, and so is every
    one of the chain an included file switched to.

    Every section named is appended to `sections`, where given: the root
    file under its path, an included one under its name as written, as
    `ReadConfigFiles` passes each to `ReadConfigStream`. `warnings` is
    `_interpret_value`'s.

    `conf_path` and `base_dir` are taken as given -- a caller building one
    by joining a value onto a base passes a plain string, `os.path.join`
    rather than `Path`'s own `/`, for the same reason `_read_conf_file`
    above takes one: Core does not lexically normalise an `includeconf`
    value at all (`ReadConfigFiles`, same file), so `Path("./confdir")`
    already reads wrong before it is ever joined onto anything
    (btclib-org/btclib-node#1273).
    """
    tree = _read_conf_file(
        conf_path,
        required=conf_explicit,
        sections=sections,
        filepath=str(conf_path),
        warnings=warnings,
    )
    if not use_includes:
        return tree
    settings = _Settings(command_line or {}, tree)

    def add_includes(network: str, names: list[str], skip: int = 0) -> int:
        """Append `network`'s names past `skip` to `names`: Core's lambda."""
        values = tree.get(network, {}).get("includeconf", [])
        names.extend(
            _setting_to_str(value) for value in values[max(skip, _negated(values)) :]
        )
        return len(values)

    chain_id = _chain_section(settings)
    names: list[str] = []
    chain_includes = add_includes(chain_id, names)
    default_includes = add_includes("", names)
    for include in names:
        include_path = (
            include
            if os.path.isabs(include)  # noqa: PTH117
            else os.path.join(str(base_dir), include)  # noqa: PTH118
        )
        included = _read_conf_file(
            include_path,
            required=True,
            include=include,
            sections=sections,
            filepath=include,
            warnings=warnings,
        )
        for section, keys in included.items():
            dest = tree.setdefault(section, {})
            for key, values in keys.items():
                dest.setdefault(key, []).extend(values)
    names = []
    add_includes(chain_id, names, chain_includes)
    add_includes("", names, default_includes)
    chain_id_final = _chain_section(settings)
    if chain_id_final != chain_id:
        add_includes(chain_id_final, names)
    for name in names:
        sys.stderr.write(
            "warning: -includeconf cannot be used from included files; "
            f"ignoring -includeconf={name}\n"
        )
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


def _atoi64(text: str) -> int:
    """Return Core's `LocaleIndependentAtoi<int64_t>` of `text`.

    `src/util/strencodings.h`, at bitcoin/bitcoin@9be056a8a7: the
    whitespace `TrimStringView` trims is dropped, then one leading `+`
    (and `+-` is `0`), then `std::from_chars` reads the ASCII digits it
    starts with, an optional `-` ahead of them, ignoring whatever
    follows. No digit is `0`, and a value past the `int64_t` range is
    that range's end on its side.
    """
    trimmed = text.strip(_TRIMMED)
    if trimmed.startswith("+"):
        if trimmed[1:2] == "-":
            return 0
        trimmed = trimmed[1:]
    match = _LEADING_INTEGER.match(trimmed)
    if match is None:
        return 0
    return max(_INT64_MIN, min(_INT64_MAX, int(match.group())))


def _get_int(settings: _Settings, name: str) -> int | None:
    """Return `name` as Core's `GetIntArg` does, `None` where nothing sets it.

    `0` negated and `1` doubly so, and a string `_atoi64`, as
    `SettingTo<int64_t>` reads it (`src/common/args.cpp`, at
    bitcoin/bitcoin@9be056a8a7).
    """
    value = _get_setting(settings, name)
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    return _atoi64(value)


def _to_int(value: int) -> int:
    """Return C++'s conversion of the `int64_t` `value` to a 32-bit `int`.

    Modular since C++20, which is how `AppInitParameterInteraction` reads
    `-maxconnections` into its `int user_max_connection`.
    """
    return (value - _INT_MIN) % 2**32 + _INT_MIN


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

    `""` is true, and anything else is true where `_atoi64` reads a
    non-zero integer: `false`, `no`, `yes` and `00` are false.
    """
    return not value or _atoi64(value) != 0


class _ChainError(ValueError):
    """The combination of chain selectors `GetChainArg` refuses.

    Thrown rather than returned in Core (`src/common/args.cpp`, at
    bitcoin/bitcoin@9be056a8a7), so `InitConfig` shows it without the
    prefix it puts on a `ReadConfigFiles` refusal, even where
    `ReadConfigFiles` asks first, through `GetChainTypeString`.
    """


# what `_chain_arg` puts ahead of a `-chain` Core does not know, which
# no chain this node knows starts with
_UNKNOWN_CHAIN = "\0"


def _chain_arg(settings: _Settings) -> str:
    """Resolve `-chain`/`-testnet`/`-signet`/`-regtest`: `GetChainArg`.

    `chain`/`testnet`/`signet`/`regtest` are read from the file's
    default section only, never a chain's own section -- Core's own
    `get_net` lambda passes an empty section for exactly this lookup
    (`GetChainArg`, `src/common/args.cpp`, at bitcoin/bitcoin@9be056a8a7),
    which is what lets a file decide the chain before any section but
    the default one can mean anything; and a negated selector on the
    command line is skipped there, as Core skips it. At most one of the
    five may resolve true; more is the same "Invalid combination" Core
    refuses, in Core's own words, `-testnet4` named among the five
    selectors although this node reads no such option of its own --
    `get_net` above never sees it, so a `-testnet4` given alone still
    silently selects mainnet, a gap of its own and not what this fixes
    (btclib-org/btclib-node#1311). A `-chain` Core does not know is
    returned as given, behind `_UNKNOWN_CHAIN`, as `GetChainArg` returns
    it.
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
        # Core's own words (`GetChainArg`, same citation as above),
        # `-testnet4` named among the selectors even though this node's
        # `get_net` never reads one (btclib-org/btclib-node#1311)
        err_msg = (
            "Invalid combination of -regtest, -signet, -testnet, -testnet4 "
            "and -chain. Can use at most one."
        )
        raise _ChainError(err_msg)
    if chain_alias is not None:
        return _CHAIN_ALIASES.get(chain_alias, _UNKNOWN_CHAIN + chain_alias)
    if regtest:
        return "regtest"
    if signet:
        return "signet"
    if testnet:
        return "testnet"
    return "mainnet"


def _resolve_chain_name(settings: _Settings) -> str:
    """Return `_chain_arg`'s chain, refusing one Core does not know.

    `GetChainType`'s refusal (`src/common/args.cpp`, at
    bitcoin/bitcoin@9be056a8a7), asked once the files are read: Core's
    own `strprintf("Unknown chain %s.", ...)`, the name written in as
    given rather than quoted (btclib-org/btclib-node#1311).
    """
    chain_name = _chain_arg(settings)
    if chain_name.startswith(_UNKNOWN_CHAIN):
        err_msg = f"Unknown chain {chain_name.removeprefix(_UNKNOWN_CHAIN)}."
        raise ValueError(err_msg)
    return chain_name


def _chain_section(settings: _Settings) -> str:
    """Return Core's `GetChainTypeString`: the section of `_chain_arg`'s chain.

    A `-chain` Core does not know is its own section name, as that
    function returns it, so that `ReadConfigFiles` reads the
    `includeconf` of a `[bogus]` section for `-chain=bogus`.
    """
    chain_name = _chain_arg(settings)
    return (
        chain_name.removeprefix(_UNKNOWN_CHAIN)
        if chain_name.startswith(_UNKNOWN_CHAIN)
        else _CHAIN_SECTION[chain_name]
    )


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
    settings: _Settings, base_dir: Path, conf_path: str | Path | None
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
    `-allowignoredconf` makes the refusal a warning for the log alone,
    appended to `settings.log_warnings`, as Core logs it. Core's other source,
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
        # not `Path(conf_path).samefile(...)`, ruff's own PTH121 fix:
        # constructing a `Path` from `conf_path` is the very drop this
        # module works around elsewhere (btclib-org/btclib-node#1273)
        if os.path.samefile(conf_path, base_config):  # noqa: PTH121
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
        settings.log_warnings.append(error)
        return
    error += (
        "\n- Set allowignoredconf=1 option to treat this condition as a warning, "
        "not an error."
    )
    raise ValueError(error)


def _parse_money(value: str) -> int | None:
    """Return Core's `ParseMoney` of `value` in satoshi, `None` where it fails.

    `src/util/moneystr.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag: whitespace trimmed at either end, then ASCII digits with at
    most one `.` and eight digits after it, at most ten before it, and a
    total within `MAX_MONEY`. No sign and no exponent; `.` alone is `0`.
    Core's own refusal of a NUL is the digit check's here.
    """
    text = value.strip(" \f\n\r\t\v")
    whole, _, fraction = text.partition(".")
    digits = "0123456789"
    if (
        not text
        or len(whole) > _MONEY_WHOLE_DIGITS
        or len(fraction) > _MONEY_DECIMALS
        or not all(c in digits for c in whole + fraction)
    ):
        return None
    amount = int(whole or "0") * _COIN + int(fraction.ljust(_MONEY_DECIMALS, "0"))
    return amount if amount <= _MAX_MONEY else None


def _get_min_relay_feerate(settings: _Settings) -> FeeRate:
    """Return `-minrelaytxfee` as a rate, Core's `ApplyArgsManOptions`.

    `src/node/mempool_args.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag: BTC/kvB through `ParseMoney`, refused with `AmountErrMsg`'s
    words; a negation reads as `0`. Core's `-incrementalrelayfee` raising
    this floor where it is not given has no option here to do so.
    """
    value = _get_arg(settings, "minrelaytxfee")
    if value is None:
        return DEFAULT_MIN_RELAY_FEERATE
    amount = _parse_money(value)
    if amount is None:
        err_msg = f"Invalid amount for -minrelaytxfee=<amount>: '{value}'"
        raise ValueError(err_msg)
    return FeeRate(sats_per_kvbyte=amount)


def _prune_target_mib(prune: int) -> int:
    """Return the MiB `-prune` asks for, refusing what Core refuses.

    `ApplyArgsManOptions` (`node/blockmanager_args.cpp`, at
    bitcoin/bitcoin@9be056a8a7) multiplies it by a MiB into a `uint64_t`,
    which wraps: the target is `prune` modulo `_PRUNE_MIB_WRAP` MiB, `0`
    being no pruning. `1` is manual pruning, and returned as it is.
    """
    if prune < 0:
        # Core's own wording, node::ApplyArgsManOptions
        # (node/blockmanager_args.cpp:23-25, at bitcoin/bitcoin@ca7162cde5):
        # `if (nPruneArg < 0) return util::Error{_("Prune cannot be
        # configured with a negative value.")};` -- bitcoind refuses to
        # start rather than treating a negative value as "pruning is on".
        err_msg = "Prune cannot be configured with a negative value."
        raise ValueError(err_msg)
    if prune == 1:
        return 1
    target_mib = prune % _PRUNE_MIB_WRAP
    if 0 < target_mib < MIN_PRUNE_TARGET_MIB:
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
    return target_mib


def _read_settings(argv: Sequence[str]) -> tuple[_Settings, Path, str]:
    """Return `argv`'s settings, file included, data directory and chain.

    `ParseArgs` (`src/bitcoind.cpp`, at bitcoin/bitcoin@9be056a8a7) in
    its order: the command line, then the file and the chain it selects,
    then the refusal of an argument that is not an option; and after it
    `ProcessInitCommands`'s help, printed to stdout with exit `0`.
    """
    warnings: list[str] = []
    options, token = _parse_parameters(argv, warnings)
    settings = _Settings(options, log_warnings=warnings)

    # `-datadir` and `-conf` are read by `get_path_arg`, lexically normal
    # before the file system is asked anything: `missing/..` and
    # `symlink/..` are the directory they are written in. `-datadir` is
    # made absolute as `GetDataDir` makes it (`src/common/args.cpp`, at
    # bitcoin/bitcoin@9be056a8a7). A value that is `.` once normal is
    # where a path a refusal names still differed from Core's: Core joins
    # the `.` on (`fs::absolute`, `AbsPathForConfigVal`, both a literal
    # `operator/` with no lexical pass of their own) and `pathlib.Path`
    # drops it on construction and on `/` alike, so `base_display` below
    # is the same join done as a string, kept alongside the `Path` every
    # actual filesystem read and `Config` still use
    # (btclib-org/btclib-node#1273).
    datadir = _get_arg(settings, "datadir")
    base_dir = Path.home() / ".btclib"
    base_display = str(base_dir)
    if datadir:
        normalized_datadir = get_path_arg(datadir)
        base_dir = Path(normalized_datadir)
        if base_dir.is_absolute():
            base_display = normalized_datadir
        else:
            base_dir = Path.cwd() / base_dir
            base_display = os.path.join(  # noqa: PTH118
                str(Path.cwd()), normalized_datadir
            )
        _check_datadir(base_dir, datadir)
    conf_path = None
    if not _is_negated(settings, "conf"):
        conf = _get_arg(settings, "conf")
        normalized_conf = get_path_arg(conf) if conf else _DEFAULT_CONF_FILENAME
        conf_path = (
            normalized_conf
            if os.path.isabs(normalized_conf)  # noqa: PTH117
            else os.path.join(base_display, normalized_conf)  # noqa: PTH118
        )
        try:
            settings.ro_config = _load_conf_tree(
                conf_path,
                conf_explicit=_is_set(settings, "conf"),
                base_dir=base_display,
                use_includes="includeconf" not in settings.command_line,
                sections=settings.config_sections,
                command_line=settings.command_line,
                warnings=settings.log_warnings,
            )
        except _ChainError:
            raise
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

    `prune` is `_prune_target_mib`'s. `directories` is a `Config` of the
    chain, the data directory and `-blocksdir`, the fields that name the
    directories `Node.__init__` locks, and of `-dnsseed`'s soft-set,
    `-forcednsseed` and `-maxconnections`, which `Config.__init__`
    refuses in that order just after a missing blocks directory, as
    Core does.
    """

    settings: _Settings
    base_dir: Path
    chain_name: str
    blocksdir: str | None
    # `-maxconnections` as `GetIntArg` reads it, which the soft-set of
    # `-listen` compares with zero, and as the `int` the limit is
    max_connections_arg: int
    max_connections: int
    debug: bool
    prune: int
    min_relay_feerate: FeeRate
    directories: Config


def _unsuitable_section_only_args(settings: _Settings) -> list[str]:
    """Return Core's `GetUnsuitableSectionOnlyArgs` (`common/args.cpp:134`).

    `main`'s own default section is never questioned, its own early
    return there; off `main`, a `network_only` name whose only non-empty
    source is the default section is what it collects --
    `OnlyHasDefaultSectionSetting` (`src/common/settings.cpp`, same sha),
    over the same three sources `_sources` above already reads. A
    source ending in a negation is empty there too, `SettingsSpan::empty`,
    same file, which is why a value is skipped rather than counted where
    `values[-1] is False`, the idiom `_get_setting` above already uses
    for it. `_OPTIONS` is walked in this module's own insertion order
    rather than Core's `std::set<std::string>`'s alphabetical one, which
    changes only the order `_check_network_only_args` below joins the
    lines in, never which names are collected.
    """
    if settings.network == "main":
        return []
    names: list[str] = []
    for name, option in _OPTIONS.items():
        if not option.network_only:
            continue
        has_default_section_setting = False
        has_other_setting = False
        for values, source in _sources(settings, name, settings.network):
            if values[-1] is False:
                continue
            if source == _DEFAULT_SECTION:
                has_default_section_setting = True
            else:
                has_other_setting = True
        if has_default_section_setting and not has_other_setting:
            names.append(name)
    return names


def _check_network_only_args(settings: _Settings) -> None:
    r"""Refuse a `NETWORK_ONLY` option set only in the default section.

    `AppInitParameterInteraction`'s own loop over
    `GetUnsuitableSectionOnlyArgs` (`src/init.cpp:936-950`, at
    bitcoin/bitcoin@9be056a8a7), asked before the unrecognised-section
    warning below, as Core asks it. One line per option, in Core's own
    words, each ending in the newline that loop's own
    `+ Untranslated("\n")` appends: `main`'s own `f"Error: {error}\n"`
    around this refusal is what turns a single line's own trailing
    newline into the blank line `bitcoind` v31.1.0 shows after it
    (btclib-org/btclib-node#1327). `-bind` is among Core's own
    `NETWORK_ONLY` options and is not among this node's -- `_OPTIONS`
    has no such entry -- so it is never named here, a gap of its own and
    not what this fixes.
    """
    names = _unsuitable_section_only_args(settings)
    if not names:
        return
    chain = settings.network
    lines = "".join(
        f"Config setting for -{name} only applied on {chain} network when "
        f"in [{chain}] section.\n"
        for name in names
    )
    raise ValueError(lines)


def _warn_unrecognized_sections(settings: _Settings) -> None:
    """Warn of every section that names no chain, as Core does.

    `AppInitParameterInteraction`'s one `InitWarning` over
    `GetUnrecognizedSections` (`src/init.cpp`, at bitcoin/bitcoin@9be056a8a7):
    a line per section, each ending in a newline, printed after
    `Warning: ` with a newline of its own, as `noui_ThreadSafeMessageBox`
    prints it, and appended to `settings.log_warnings` too, that
    function logging it as well.
    """
    lines = "".join(
        f"{filepath}:{lineno} Section [{name}] is not recognized.\n"
        for name, filepath, lineno in settings.config_sections
        if name not in _RECOGNIZED_SECTIONS
    )
    if lines:
        sys.stderr.write(f"Warning: {lines}\n")
        settings.log_warnings.append(lines)


def _before_lock(argv: Sequence[str]) -> _BeforeLock:
    """Read `argv` and its file, and refuse what Core refuses before its lock.

    `InitConfig`, then `AppInitParameterInteraction` (`src/init.cpp`, at
    bitcoin/bitcoin@9be056a8a7) in its order: a `NETWORK_ONLY` option set
    only in the default section off `main`, the warning about a section
    naming no chain, a missing blocks directory, `-forcednsseed` beside
    a `-dnsseed` that is off, a negative `-maxconnections`, `-debug`'s
    categories, `-prune`, `-minrelaytxfee`.
    """
    settings, base_dir, chain_name = _read_settings(argv)
    _check_network_only_args(settings)
    _warn_unrecognized_sections(settings)
    # `GetBlocksDirPath`: a negated `-blocksdir` is an empty path, which
    # `fs::absolute` reads as the working directory
    blocksdir = _get_arg(settings, "blocksdir")
    if _is_negated(settings, "blocksdir"):
        blocksdir = ""
    max_connections_arg = _get_int(settings, "maxconnections")
    if max_connections_arg is None:
        max_connections_arg = DEFAULT_MAX_PEER_CONNECTIONS
    max_connections = _to_int(max_connections_arg)
    connect = _get_args(settings, "connect")
    connect_negated = _is_negated(settings, "connect")
    # `InitParameterInteraction`'s own soft-set (`src/init.cpp`, same
    # sha), over the `int64_t` `-maxconnections` arg itself, as Core's
    # own soft-set does -- but only where `-dnsseed` was not given a
    # value of its own: `SoftSetBoolArg` never overwrites an arg already
    # set, so an explicit `-dnsseed=1` reaches `Config.__init__`'s own
    # `-forcednsseed` refusal as `True` even under `-connect`
    # (btclib-org/btclib-node#1265, review round 3). `_after_lock`
    # recomputes the identical value from `max_connections_arg` below,
    # once this returns it.
    dnsseed = _get_bool(settings, "dnsseed")
    if dnsseed is None:
        dnsseed = not connect and not connect_negated and max_connections_arg > 0
    directories = Config(
        chain=chain_name,
        data_dir=base_dir,
        blocks_dir=blocksdir,
        max_connections=max_connections,
        dnsseed=dnsseed,
        forcednsseed=bool(_get_bool(settings, "forcednsseed")),
    )
    debug = _resolve_debug(settings)
    prune = _prune_target_mib(_get_int(settings, "prune") or 0)
    # after `-debug`'s categories, where `AppInitParameterInteraction`
    # applies the mempool's options (btclib-org/btclib-node#1332)
    min_relay_feerate = _get_min_relay_feerate(settings)
    return _BeforeLock(
        settings,
        base_dir,
        chain_name,
        blocksdir,
        max_connections_arg,
        max_connections,
        debug,
        prune,
        min_relay_feerate,
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
    order: `CheckHostPortOptions`'s `-port`, `-rpcport` and `-rpcbind`.
    `-rpccookieperms` and `-rpcauth` are refused later, by
    `RpcAuth.start`, as `StartHTTPRPC` refuses them.
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
        listen = not connect and not connect_negated and before.max_connections_arg > 0
    # The same `if` as `-listen`'s soft-set, over the same `int64_t`
    # (ISS 1324: `before.max_connections_arg`, not the 32-bit-narrowed
    # `before.max_connections` `Config`'s own fallback for a `None`
    # `dnsseed` would read), which an explicit `-dnsseed`/`-nodnsseed`
    # wins over.
    dnsseed = _get_bool(settings, "dnsseed")
    if dnsseed is None:
        no_peers = not connect and not connect_negated
        dnsseed = no_peers and before.max_connections_arg > 0
    # `-fixedseeds`'s own explicit value; `DEFAULT_FIXEDSEEDS` (true)
    # where it is not given.
    fixedseeds = _get_bool(settings, "fixedseeds")
    if fixedseeds is None:
        fixedseeds = True
    # `Config.__init__`'s own soft-set reads `listen` above, already
    # resolved, rather than repeating `InitParameterInteraction`'s
    # `-listen=0` condition here
    discover = _get_bool(settings, "discover")
    peerblockfilters = bool(_get_bool(settings, "peerblockfilters"))
    # `GetAuthCookieFile` (`src/rpc/request.cpp`, same sha): negated, no cookie
    rpccookiefile = (
        None
        if _is_negated(settings, "rpccookiefile")
        else _get_arg(settings, "rpccookiefile") or ""
    )
    server = _get_bool(settings, "server")
    # `-bantime`, which `AppInitMain` hands to `BanMan` at step 6
    # (`src/init.cpp:1644`, same sha)
    ban_time = _get_int(settings, "bantime")
    if ban_time is None:
        ban_time = DEFAULT_MISBEHAVING_BANTIME
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
        seednode=_get_args(settings, "seednode"),
        listen=listen,
        discover=discover,
        peerblockfilters=peerblockfilters,
        max_connections=before.max_connections,
        dnsseed=dnsseed,
        forcednsseed=bool(_get_bool(settings, "forcednsseed")),
        fixed_seeds=fixedseeds,
        ban_time=ban_time,
        min_relay_feerate=before.min_relay_feerate,
        rpcauth=_get_args(settings, "rpcauth"),
        rpcuser=_get_arg(settings, "rpcuser") or "",
        rpcpassword=_get_arg(settings, "rpcpassword") or "",
        rpccookiefile=rpccookiefile,
        rpccookieperms=_get_arg(settings, "rpccookieperms"),
        rpcwhitelist=_get_args(settings, "rpcwhitelist"),
        rpcwhitelistdefault=_get_bool(settings, "rpcwhitelistdefault"),
        log_warnings=settings.log_warnings,
    )


def build_config(argv: Sequence[str] | None = None) -> Config:
    """Parse `argv` (`sys.argv[1:]` if `None`) and its `-conf` into a `Config`.

    Raises `ValueError` on a malformed argument, a malformed
    configuration file, or an unknown chain, in the order `bitcoind`
    refuses them, and `SystemExit(0)` once the help is printed. No lock
    is taken: `main` takes it between `_before_lock` and `_after_lock`.
    """
    return _after_lock(_before_lock(sys.argv[1:] if argv is None else argv))


# Core's `SetupEnvironment` (`src/common/system.cpp`, at
# bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which `bitcoind`'s own
# `main` calls right after building its `interfaces::Init`
# (`src/bitcoind.cpp`, same sha): the process umask becomes 0077
# everywhere but Windows, so every directory and file it creates is its
# owner's alone.
# Core has no option to keep the caller's: `-sysperms` is gone by v31.1.
_PRIVATE_UMASK = 0o077


def _setup_environment() -> None:
    """Make the process umask owner-only, as Core's `SetupEnvironment` does.

    Called by `main` alone: a caller building a `Node` in its own
    process keeps its own umask, the reason `RpcAuth.generate_cookie`
    sets the cookie's mode on the file.
    """
    if sys.platform != "win32":
        os.umask(_PRIVATE_UMASK)


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
    _setup_environment()
    # a byte of `bitcoin.conf` or of `argv` that is not UTF-8 reaches a
    # message as a lone surrogate; written back as that byte, as Core
    # writes the bytes it read
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(errors="surrogateescape")
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
