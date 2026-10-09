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
init aborts. `-minrelaytxfee`, `-incrementalrelayfee` and `-dustrelayfee`
are BTC/kvB, as Core's, and `Config`'s `min_relay_feerate`,
`incremental_relay_feerate` and `dust_relay_feerate` the same rates in
sat/kvB. Two `Config` fields
have no option here: `log_path` (this command always takes
`Config`'s own default -- a file under the data directory -- an operator
who wants console output can read it from there), and `allow_p2p` (the
P2P listener is always requested; `-listen=0` is what keeps it from
accepting connections).

`-debug=<category>` takes Core's logging category names
(`LOG_CATEGORIES_BY_STR`, `src/logging.cpp`, same sha) and refuses any
other with Core's own "Unsupported logging category" message
(`SetLoggingCategories`, `src/init/common.cpp`), and `0` or `none`
discards the categories before it. Each debug line is written under
Core's category for it (`Logger.log_debug`), and `-debug` with no
category, `1` or `all` selects every one. `-debugexclude=<category>` takes
a category out after `-debug`'s, refused the same way.

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
`_get_settings_list` below: the command line over `settings.json` over
the active chain's section over the default section; within the command
line the last value, within a file the first, the chain selectors
aside; and a negation discarding every value named before it at its own
level.
`-connect`, `-addnode`, `-seednode`, `-rpcauth`, `-rpcwhitelist`,
`-rpcbind`, `-rpcallowip`, `-whitelist`, `-whitebind`, `-debug` and
`-shutdownnotify` are lists, every value from every level applying --
`-shutdownnotify` alone among the four notify options below, Core reading
it with `GetArgs` rather than the `GetArg` the other three are read with
(`notify.py`'s own module docstring).

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
`-bind` is `NETWORK_ONLY` in Core too, and is among `_OPTIONS`.

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

`settings.json` is Core's read-write file (`settings_file.py`), in the
chain's data directory, or where `-settings=<path>` names it, a relative
path being joined to that directory; `-nosettings` reads and writes none.
`_init_settings_file` reads it and writes it back at every start, after
`bitcoin.conf` and ahead of `-help`, as `InitConfig` does. An option looks
its bare name up in it, never `section.name` or `noname`, and what it finds
is read as Core reads it, by the getter that asks. A `false` is the
negation and a string the value. A number is the value of an option read
as a string or an integer, "JSON integer out of range" where it is
outside the `int64_t` range or written with a fraction or an exponent,
and is refused as a bool or among several values. An array holds the
values of an option that takes several, and is refused as one value. A
`null` is no value where one is read, and hides the levels below the file,
and is refused among several; an object is refused wherever it is read.
Every refusal is Core's `JSON value of type <type> is not of expected
type string`, where the option is read.

A `settings` key in the file moves the file the write goes to, since Core
reads `-settings` again for the write, and `{"settings": false}` refuses
the start. The chain is resolved before the file is read and asked again
after it, as `AppInitParameterInteraction` asks (`src/init.cpp`): a file
naming a second chain selector refuses the start with the invalid
combination, and the chain the first ask gave is the one used.

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
from btclib_node.chains import Main, SigNet, TestNet, TestNet4
from btclib_node.config import (
    DEFAULT_DUST_RELAY_FEERATE,
    DEFAULT_INCREMENTAL_RELAY_FEERATE,
    DEFAULT_MAX_DATACARRIER_BYTES,
    DEFAULT_MAX_PEER_CONNECTIONS,
    DEFAULT_MAX_TIP_AGE,
    DEFAULT_MIN_RELAY_FEERATE,
    BindAddress,
    Config,
    WhitebindAddress,
    default_onion_bind,
    get_path_arg,
    listen_port,
    lookup_service,
    parse_bind,
    parse_whitebind,
    service_text,
    split_host_port,
)
from btclib_node.constants import (
    DEFAULT_MAXRECEIVEBUFFER,
    DEFAULT_MAXSENDBUFFER,
    DEFAULT_MEMPOOL_EXPIRY_HOURS,
    DIR_MODE,
    MIN_PRUNE_TARGET_MIB,
    default_data_dir,
)
from btclib_node.dirlock import DirectoryLock, lock_directories
from btclib_node.exceptions import DirectoryLockError
from btclib_node.fee_estimator import MAX_FILE_AGE_HOURS
from btclib_node.log import open_history_log
from btclib_node.p2p.address import BAD_PORTS
from btclib_node.p2p.banman import DEFAULT_MISBEHAVING_BANTIME, is_valid_host
from btclib_node.p2p.permissions import NET_PERMISSIONS_DOC
from btclib_node.rpc.connection import REQUEST_TIMEOUT
from btclib_node.settings_file import (
    SETTINGS_FILENAME,
    Json,
    Number,
    read_settings,
    type_name,
    write_json,
    write_settings,
)

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
    "testnet4": "testnet4",
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
    "testnet4": "testnet4",
}

# `LOG_CATEGORIES_BY_STR` (`src/logging.cpp`,
# at bitcoin/bitcoin@9be056a8a7), `lock` left out as a release build leaves
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


# `uint256::size() * 2` (`src/uint256.h`, at bitcoin/bitcoin@9be056a8a7,
# the v31.1 tag): the most hex digits `-minimumchainwork` accepts, and
# what Core's own refusal of a longer value names.
_MIN_WORK_HEX_DIGITS = 64

# Every chain's own `minimum_chain_work` (`btclib.consensus`), read once
# at import for `-minimumchainwork`'s own help text, Core's `-help`
# listing `defaultChainParams`, `testnetChainParams`, `testnet4ChainParams`
# and `signetChainParams` the same way (`src/init.cpp`, same sha) --
# `regtest`'s own is not among them there either.
_MIN_CHAIN_WORK_MAIN = Main().consensus.minimum_chain_work
_MIN_CHAIN_WORK_TESTNET = TestNet().consensus.minimum_chain_work
_MIN_CHAIN_WORK_TESTNET4 = TestNet4().consensus.minimum_chain_work
_MIN_CHAIN_WORK_SIGNET = SigNet().consensus.minimum_chain_work


# What `-assumevalid` defaults to until btclib's `ConsensusParams` carries
# Core's own per network (btclib-org/btclib-node#1576, a later pull
# request): no block, which Core's `GetHex` of a zero hash prints as these
# digits. Core's `-help` lists the default per network here; that listing
# is what that pull request fills in.
_ASSUME_VALID_NONE = "0" * _MIN_WORK_HEX_DIGITS


def _parse_hex_uint256(value: str) -> int | None:
    """Return Core's `uint256::FromUserHex` of `value`, `None` where it fails.

    `src/uint256.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag: an
    optional literal `0x` prefix (lowercase only, `RemovePrefixView`
    being a plain `starts_with`) is dropped, and what is left is refused
    past `_MIN_WORK_HEX_DIGITS` hex digits or containing anything but
    one -- `IsHex`'s own parity check never fires, since a value under
    that length is read as though left-padded with zeroes to it, which
    is always even. Empty is `0`, `int`'s own refusal of it taken here
    rather than reached.
    """
    text = value.removeprefix("0x")
    if len(text) > _MIN_WORK_HEX_DIGITS or not re.fullmatch("[0-9A-Fa-f]*", text):
        return None
    return int(text, 16) if text else 0


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
    "acceptnonstdtxn": _Option(
        "",
        'Relay and mine "non-standard" transactions (test networks only; default: 0)',
        _NODE_RELAY_TITLE,
        debug_only=True,
    ),
    "acceptstalefeeestimates": _Option(
        "",
        "Read fee estimates even if they are stale (regtest only; default: 0) fee "
        f"estimates are considered stale if they are {MAX_FILE_AGE_HOURS} hours old",
        _DEBUG_TEST_TITLE,
        debug_only=True,
    ),
    "addnode": _Option(
        "=<ip>[:port]",
        "Add a node to connect to, alongside automatic connections. This option "
        "can be specified multiple times.",
        _CONNECTION_TITLE,
        network_only=True,
    ),
    "alertnotify": _Option(
        "=<cmd>",
        "Execute command when an alert is raised (%s in cmd is replaced by message)",
        _OPTIONS_TITLE,
    ),
    "allowignoredconf": _Option(
        "",
        f"For backwards compatibility, treat an unused {_DEFAULT_CONF_FILENAME} "
        "file in the datadir as a warning, not an error.",
        _OPTIONS_TITLE,
    ),
    "assumevalid": _Option(
        "=<hex>",
        "If this block is in the chain assume that it and its ancestors are "
        "valid and potentially skip their script verification (0 to verify "
        f"all, default: {_ASSUME_VALID_NONE}, testnet3: {_ASSUME_VALID_NONE}, "
        f"testnet4: {_ASSUME_VALID_NONE}, signet: {_ASSUME_VALID_NONE})",
        _OPTIONS_TITLE,
    ),
    "bantime": _Option(
        "=<n>",
        "Default duration (in seconds) of manually configured bans (default: "
        f"{DEFAULT_MISBEHAVING_BANTIME})",
        _CONNECTION_TITLE,
    ),
    "bind": _Option(
        "=<addr>[:<port>][=onion]",
        "Bind to given address and always listen on it (default: 0.0.0.0). "
        "Use [host]:port notation for IPv6. Append =onion to tag any "
        "incoming connections to that address and port as incoming Tor "
        "connections (default: 127.0.0.1:<port + 1>=onion, where no -bind "
        "is given). This option can be specified multiple times",
        _CONNECTION_TITLE,
        network_only=True,
    ),
    "blocknotify": _Option(
        "=<cmd>",
        "Execute command when the best block changes (%s in cmd is replaced "
        "by block hash)",
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
    "datacarrier": _Option(
        "",
        "Relay and mine data carrier transactions (default: 1)",
        _NODE_RELAY_TITLE,
    ),
    "datacarriersize": _Option(
        "=<n>",
        "Relay and mine transactions whose data-carrying raw scriptPubKeys in "
        "aggregate are of this size or less, allowing multiple outputs "
        f"(default: {DEFAULT_MAX_DATACARRIER_BYTES})",
        _NODE_RELAY_TITLE,
    ),
    "datadir": _Option(
        "=<dir>", "Specify data directory", _OPTIONS_TITLE, disallow_negation=True
    ),
    "debug": _Option(
        "=<category>",
        "Log at DEBUG level rather than INFO (default: -nodebug, supplying "
        "<category> is optional). If <category> is not supplied or if <category> "
        'is 1 or "all", output all debug logging; 0 or "none" discards the '
        "categories given before it. "
        "Valid values for <category> are Core's: 1, all, "
        + ", ".join(sorted(_LOG_CATEGORIES))
        + ". This option can be specified multiple times.",
        _DEBUG_TEST_TITLE,
    ),
    "debugexclude": _Option(
        "=<category>",
        "Exclude debug logging for a category. Can be used in conjunction "
        "with -debug=1 to output debug logging for all categories except "
        "the specified category. This option can be specified multiple "
        'times to exclude multiple categories. This takes priority over "-debug"',
        _DEBUG_TEST_TITLE,
    ),
    "discover": _Option(
        "",
        "Discover own IP addresses (default: 1 when listening and no -externalip)",
        _CONNECTION_TITLE,
    ),
    "dnsseed": _Option(
        "",
        "Query for peer addresses via DNS lookup, if low on addresses "
        "(default: 1 unless -connect used or -maxconnections=0)",
        _CONNECTION_TITLE,
    ),
    "dustrelayfee": _Option(
        "=<amt>",
        "Fee rate (in BTC/kvB) used to define dust, the value of an output such "
        "that it will cost more than its value in fees at this fee rate to "
        "spend it. (default: "
        f"{_format_money(DEFAULT_DUST_RELAY_FEERATE.sats_per_kvbyte)})",
        _NODE_RELAY_TITLE,
        debug_only=True,
    ),
    "externalip": _Option(
        "=<ip>",
        "Specify your own public address. This option can be specified multiple times",
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
    "incrementalrelayfee": _Option(
        "=<amt>",
        "Fee rate (in BTC/kvB) used to define cost of relay, used for mempool "
        "limiting and replacement policy. (default: "
        f"{_format_money(DEFAULT_INCREMENTAL_RELAY_FEERATE.sats_per_kvbyte)})",
        _NODE_RELAY_TITLE,
        debug_only=True,
    ),
    "listen": _Option(
        "",
        "Accept connections from outside (default: 1 if no -connect or "
        "-maxconnections=0)",
        _CONNECTION_TITLE,
    ),
    "logratelimit": _Option(
        "",
        "Apply rate limiting to unconditional logging (default: 1)",
        _DEBUG_TEST_TITLE,
    ),
    "minimumchainwork": _Option(
        "=<hex>",
        "Minimum work assumed to exist on a valid chain in hex (default: "
        f"{_MIN_CHAIN_WORK_MAIN:064x}, testnet3: {_MIN_CHAIN_WORK_TESTNET:064x}, "
        f"testnet4: {_MIN_CHAIN_WORK_TESTNET4:064x}, signet: "
        f"{_MIN_CHAIN_WORK_SIGNET:064x})",
        _OPTIONS_TITLE,
        debug_only=True,
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
    "maxreceivebuffer": _Option(
        "=<n>",
        "Maximum per-connection receive buffer, <n>*1000 bytes (default: "
        f"{DEFAULT_MAXRECEIVEBUFFER})",
        _CONNECTION_TITLE,
    ),
    "maxsendbuffer": _Option(
        "=<n>",
        "Maximum per-connection memory usage for the send buffer, <n>*1000 "
        f"bytes (default: {DEFAULT_MAXSENDBUFFER})",
        _CONNECTION_TITLE,
    ),
    "maxtipage": _Option(
        "=<n>",
        "Maximum tip age in seconds to consider node in initial block "
        f"download (default: {DEFAULT_MAX_TIP_AGE})",
        _DEBUG_TEST_TITLE,
        debug_only=True,
    ),
    "mempoolexpiry": _Option(
        "=<n>",
        "Do not keep transactions in the mempool longer than <n> hours (default: "
        f"{DEFAULT_MEMPOOL_EXPIRY_HOURS})",
        _OPTIONS_TITLE,
    ),
    "peerblockfilters": _Option(
        "",
        "Serve compact block filters to peers per BIP 157 (default: 0)",
        _CONNECTION_TITLE,
    ),
    "permitbaremultisig": _Option(
        "",
        "Relay transactions creating non-P2SH multisig outputs (default: 1)",
        _NODE_RELAY_TITLE,
    ),
    "persistmempool": _Option(
        "",
        "Whether to save the mempool on shutdown and load on restart (default: 1)",
        _OPTIONS_TITLE,
    ),
    "persistmempoolv1": _Option(
        "",
        "Whether a mempool.dat file created by -persistmempool or the savemempool "
        "RPC will be written in the legacy format (version 1) or the current "
        "format (version 2). This temporary option will be removed in the "
        "future. (default: 0)",
        _OPTIONS_TITLE,
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
    "rpcservertimeout": _Option(
        "=<n>",
        f"Timeout during HTTP requests (default: {int(REQUEST_TIMEOUT)})",
        _RPC_TITLE,
        debug_only=True,
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
    "settings": _Option(
        "=<file>",
        "Specify path to dynamic settings data file. Can be disabled with "
        "-nosettings. File is written at runtime and not meant to be edited by "
        f"users (use {_DEFAULT_CONF_FILENAME} instead for custom settings). "
        "Relative paths will be prefixed by datadir location. "
        f"(default: {SETTINGS_FILENAME})",
        _OPTIONS_TITLE,
    ),
    "shutdownnotify": _Option(
        "=<cmd>",
        "Execute command immediately before beginning shutdown. The need for "
        "shutdown may be urgent, so be careful not to delay it long (if the "
        "command doesn't require interaction with the server, consider having "
        "it fork into the background).",
        _OPTIONS_TITLE,
    ),
    "signet": _Option(
        "", "Use the signet chain. Equivalent to -chain=signet.", _CHAINPARAMS_TITLE
    ),
    "startupnotify": _Option("=<cmd>", "Execute command on startup.", _OPTIONS_TITLE),
    "testnet": _Option(
        "",
        "Use the testnet3 chain. Equivalent to -chain=test. Support for "
        "testnet3 is deprecated and will be removed in an upcoming release. "
        "Consider moving to testnet4 now by using -testnet4.",
        _CHAINPARAMS_TITLE,
    ),
    "testnet4": _Option(
        "",
        "Use the testnet4 chain. Equivalent to -chain=testnet4.",
        _CHAINPARAMS_TITLE,
    ),
    "v1transport": _Option(
        "",
        "Support v1 transport (default: 0)",
        _CONNECTION_TITLE,
    ),
    "v2transport": _Option(
        "",
        "Support v2 transport (default: 1)",
        _CONNECTION_TITLE,
    ),
    "whitebind": _Option(
        "=<[permissions@]addr>",
        "Bind to the given address and add permission flags to the peers "
        "connecting to it. Use [host]:port notation for IPv6. Allowed "
        "permissions: " + ", ".join(NET_PERMISSIONS_DOC) + ". Specify "
        "multiple permissions separated by commas (default: "
        "download,noban,mempool,relay). Can be specified multiple times.",
        _CONNECTION_TITLE,
    ),
    "whitelist": _Option(
        "=<[permissions@]IP address or network>",
        "Add permission flags to the peers using the given IP address (e.g. "
        "1.2.3.4) or CIDR-notated network (e.g. 1.2.3.0/24). Allowed "
        "permissions: " + ", ".join(NET_PERMISSIONS_DOC) + ". Specify "
        "multiple permissions separated by commas (default: "
        "download,noban,mempool,relay). "
        'Additional flags "in" and "out" '
        "control whether permissions apply to incoming connections and/or "
        "manual (default: incoming only). Can be specified multiple times.",
        _CONNECTION_TITLE,
    ),
    "whitelistforcerelay": _Option(
        "",
        "Add 'forcerelay' permission to whitelisted peers with default "
        "permissions. This will relay transactions even if the transactions "
        "were already in the mempool. (default: 0)",
        _NODE_RELAY_TITLE,
    ),
    "whitelistrelay": _Option(
        "",
        "Add 'relay' permission to whitelisted peers with default "
        "permissions. This lifts the limit on their transaction "
        "announcements (default: 1)",
        _NODE_RELAY_TITLE,
    ),
}

# The sections `GetUnrecognizedSections` (`src/common/args.cpp`, same
# sha) does not warn about: every `ChainTypeToString`.
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
_SETTINGS_FILE = "settings file"
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
    # the warnings Core buffers while it reads its settings, in order;
    # `open_history_log` logs each once its own log is open, ahead of
    # its version line
    log_warnings: list[str] = field(default_factory=list)
    # `_warn_unrecognized_sections`'s one warning, logged after the
    # version line; `""` where no section is unrecognised
    section_warning: str = ""
    # `init::StartLogging`'s "Config file:" line, logged after the data
    # directory's; `_read_settings` sets it
    config_file_line: str = ""
    # `settings.json`'s values by name, in key order: Core's
    # `Settings::rw_settings`, empty until `_init_settings_file` reads it
    rw_settings: dict[str, Json] = field(default_factory=dict)


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


def _negated(values: Sequence[Json]) -> int:
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
    one naming a section -- "Invalid parameter", the argument quoted by
    `_quoted_line` -- on a negation the option forbids, and on `-includeconf`
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
            err_msg = f"{_PARSE_ERROR}Invalid parameter {_quoted_line(arg)}"
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


def _quoted_line(line: str) -> str:
    """Return `line` for a parse error, cut after a sensitive name.

    Core's `GetConfigOptions` (`src/common/config.cpp`) and
    `ParseParameters` (`src/common/args.cpp`) quote the whole line or
    argument. This tree departs on purpose: it may hold a password, and the
    refusal reaches stderr and logs (SECURITY.md, "Where this node departs
    from Bitcoin Core"). A line holding the name of a `sensitive` option
    (`rpcauth`, `rpcpassword`, `rpcuser`) is quoted up to the first such
    name, wherever it sits: after a `-`, a section prefix or `no`, or after
    text that is none of these.
    """
    ends = [
        line.find(name) + len(name)
        for name, option in _OPTIONS.items()
        if option.sensitive and name in line
    ]
    return line[: min(ends)] if ends else line


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
                f"parse error on line {lineno}: {_quoted_line(line)}, options in "
                "configuration file must be specified without leading -"
            )
            raise ValueError(err_msg)
        if "=" not in line:
            shown = _quoted_line(line)
            err_msg = f"parse error on line {lineno}: {shown}"
            if line.startswith("no"):
                err_msg += (
                    ", if you intended to specify a negated option, use "
                    f"{shown}=1 instead"
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


def _wrong_type(value: Json) -> ValueError:
    """Return the error Core's `UniValue::get_str` throws on `value`."""
    err_msg = f"JSON value of type {type_name(value)} is not of expected type string"
    return ValueError(err_msg)


def _sources(
    settings: _Settings, name: str, section: str
) -> list[tuple[list[Json], str]]:
    """Return `name`'s values at each level Core's `MergeSettings` merges.

    Highest first: the command line, `settings.json`, the network section
    of the file (where `section` names one), and its default section.
    """
    sources: list[tuple[list[Json], str]] = []
    if name in settings.command_line:
        sources.append((list(settings.command_line[name]), _COMMAND_LINE))
    if name in settings.rw_settings:
        sources.append(([settings.rw_settings[name]], _SETTINGS_FILE))
    if section and name in settings.ro_config.get(section, {}):
        sources.append((list(settings.ro_config[section][name]), _NETWORK_SECTION))
    if name in settings.ro_config.get("", {}):
        sources.append((list(settings.ro_config[""][name]), _DEFAULT_SECTION))
    return sources


def _use_default_section(settings: _Settings, name: str) -> bool:
    """Return Core's `UseDefaultSection`: on `main`, or not network-only."""
    return settings.network == "main" or not _OPTIONS[name].network_only


def _get_setting(
    settings: _Settings, name: str, *, get_chain_type: bool = False
) -> Json:
    """Return `name`'s one value, Core's `GetSetting` (`common/settings.cpp`).

    The highest level naming it decides. There, the last value after the
    last negation, or the first in a file where `get_chain_type` is not
    set; `False` where a negation is last. A default section is skipped
    for a network-only option off `main` unless it ends negated, and
    `get_chain_type` -- `GetChainArg`'s own read -- reads the file's
    default section alone and skips a level ending negated, whatever
    its source.
    """
    section = "" if get_chain_type else settings.network
    ignore_default = not get_chain_type and not _use_default_section(settings, name)
    for values, source in _sources(settings, name, section):
        last_negated = values[-1] is False
        if ignore_default and source == _DEFAULT_SECTION and not last_negated:
            continue
        if get_chain_type and last_negated:
            continue
        live = values[_negated(values) :]
        if not live:
            return False
        first_wins = source != _COMMAND_LINE and not get_chain_type
        return live[0] if first_wins else live[-1]
    return None


def _get_settings_list(settings: _Settings, name: str) -> list[Json]:
    """Return `name`'s values, Core's `GetSettingsList` (`common/settings.cpp`).

    Every level's values after its own last negation, highest level
    first. A negation stops the levels below it, except that a file's
    values still apply after a command line whose negation was followed
    by a value of its own, Core's own "zombie" values.
    """
    ignore_default = not _use_default_section(settings, name)
    result: list[Json] = []
    done = False
    prev_negated_empty = False
    for values, source in _sources(settings, name, settings.network):
        add_zombie = (
            source in {_NETWORK_SECTION, _DEFAULT_SECTION} and not prev_negated_empty
        )
        if ignore_default and source == _DEFAULT_SECTION:
            continue
        if not done or add_zombie:
            for value in values[_negated(values) :]:
                # an array of the file is the values it holds
                result.extend(value if isinstance(value, list) else [value])
        done = done or _negated(values) > 0
        prev_negated_empty = prev_negated_empty or (values[-1] is False and not result)
    return result


def _setting_to_str(value: Json) -> str:
    """Return Core's `SettingToString`: `"0"` negated, `"1"` doubly so.

    A number is its text. Raises `_wrong_type` for an array and an object.
    """
    if value is True:
        return "1"
    if value is False:
        return "0"
    if isinstance(value, str):
        return value
    raise _wrong_type(value)


def _setting_to_write_str(value: _Value) -> str:
    """Return Core's `SettingsValue::write()`: `true`/`false`, else JSON.

    `ArgsManager::LogArgs`'s own `logArgsPrefix` (`src/common/args.cpp`, at
    bitcoin/bitcoin@9be056a8a7) writes a negation's `bool` this way, and a
    plain string as a JSON string literal -- unlike `_setting_to_str`
    above, which is `GetArg`'s own `SettingToString`, a different Core
    function reached by a different reader.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


def _log_args(settings: _Settings) -> tuple[str, ...]:
    """Return Core's `LogArgs` lines: config file, settings file, command line.

    `ArgsManager::LogArgs`/`logArgsPrefix` (`src/common/args.cpp`, at
    bitcoin/bitcoin@9be056a8a7): `std::map` order -- a section, then a
    name inside it, each sorted, and each value in the order it was
    read -- masked to `****` where `_Option.sensitive` is Core's own
    `SENSITIVE` flag. Every name reaching this is already in `_OPTIONS`:
    `_parse_conf_text` drops an unknown config key with its own warning,
    and `_parse_parameters` refuses an unknown command-line one outright,
    so Core's own `if (flags)` guard around this has nothing left here to
    be false for. `LogArgs`'s middle category, "Setting file arg:", is
    `settings.json`'s: every name it holds, known or not, in key order,
    its value as `write_json` writes it.
    """
    lines: list[str] = []
    for section, args in sorted(settings.ro_config.items()):
        prefix = f"[{section}] " if section else ""
        for name, values in sorted(args.items()):
            sensitive = _OPTIONS[name].sensitive
            for value in values:
                shown = "****" if sensitive else _setting_to_write_str(value)
                lines.append(f"Config file arg: {prefix}{name}={shown}")
    lines.extend(
        f"Setting file arg: {name} = {write_json(value)}"
        for name, value in settings.rw_settings.items()
    )
    for name, values in sorted(settings.command_line.items()):
        sensitive = _OPTIONS[name].sensitive
        for value in values:
            shown = "****" if sensitive else _setting_to_write_str(value)
            lines.append(f"Command-line arg: {name}={shown}")
    return tuple(lines)


def _get_arg(settings: _Settings, name: str) -> str | None:
    """Return Core's `GetArg` of `name`, `None` where nothing sets it."""
    value = _get_setting(settings, name)
    return None if value is None else _setting_to_str(value)


def _get_args(settings: _Settings, name: str) -> list[str]:
    """Return Core's `GetArgs` of `name`: every value, as strings.

    `GetArgs` throws on the first of them that is a number, a `null`, an
    array or an object.
    """
    result = []
    for value in _get_settings_list(settings, name):
        if isinstance(value, Number):
            raise _wrong_type(value)
        result.append(_setting_to_str(value))
    return result


def _setting_to_bool(value: Json) -> bool | None:
    """Return Core's `SettingToBool`, `None` for a `null`.

    A number is refused, as `get_str` refuses it.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Number) or not isinstance(value, str):
        raise _wrong_type(value)
    return _interpret_bool(value)


def _get_bool(settings: _Settings, name: str) -> bool | None:
    """Return Core's `GetBoolArg` of `name`, `None` where nothing sets it."""
    return _setting_to_bool(_get_setting(settings, name))


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
    if isinstance(value, Number):
        if (
            not _LEADING_INTEGER.fullmatch(value)
            or not _INT64_MIN <= int(value) <= _INT64_MAX
        ):
            err_msg = "JSON integer out of range"
            raise ValueError(err_msg)
        return int(value)
    if not isinstance(value, str):
        raise _wrong_type(value)
    return _atoi64(value)


def _to_int(value: int) -> int:
    """Return C++'s conversion of the `int64_t` `value` to a 32-bit `int`.

    Modular since C++20, which is how `AppInitParameterInteraction` reads
    `-maxconnections` into its `int user_max_connection`.
    """
    return (value - _INT_MIN) % 2**32 + _INT_MIN


def _get_thousands(settings: _Settings, name: str, default: int) -> int:
    """Return `1000 * GetIntArg(name, default)` as an `unsigned int` holds it.

    How `AppInitMain` reads `-maxsendbuffer` and `-maxreceivebuffer` into
    `nSendBufferMaxSize` and `nReceiveFloodSize` (`src/init.cpp` and
    `src/net.h`, at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): a negative
    value wraps, as it does there.
    """
    value = _get_int(settings, name)
    return (1000 * (default if value is None else value)) % 2**32


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
    """Resolve the chain selectors, `-chain` among them: `GetChainArg`.

    `chain`/`testnet`/`signet`/`regtest`/`testnet4` are read from the
    file's default section only, never a chain's own section -- Core's
    own `get_net` lambda passes an empty section for exactly this lookup
    (`GetChainArg`, `src/common/args.cpp`, at bitcoin/bitcoin@9be056a8a7),
    which is what lets a file decide the chain before any section but
    the default one can mean anything; and a selector negated at any
    level is skipped there, as Core skips it. At most one of the
    five may resolve true; more is the same "Invalid combination" Core
    refuses, in Core's own words. A `-chain` Core does not know is
    returned as given, behind `_UNKNOWN_CHAIN`, as `GetChainArg` returns
    it.
    """

    def get_net(name: str) -> bool:
        return bool(_setting_to_bool(_get_setting(settings, name, get_chain_type=True)))

    regtest = get_net("regtest")
    signet = get_net("signet")
    testnet = get_net("testnet")
    testnet4 = get_net("testnet4")
    chain_alias = _get_arg(settings, "chain")
    if sum([chain_alias is not None, testnet, signet, regtest, testnet4]) > 1:
        # Core's own words (`GetChainArg`, same citation as above)
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
    if testnet4:
        return "testnet4"
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


def _resolve_debug(
    settings: _Settings,
) -> tuple[bool, frozenset[str], frozenset[str]]:
    """Return whether `-debug` is on, for which categories, and but for which.

    `SetLoggingCategories` (`src/init/common.cpp`, at
    bitcoin/bitcoin@9be056a8a7): the categories after the last `0` or
    `none`, each refused unless `GetLogCategory` (`src/logging.cpp`)
    knows it. The first set is empty where every category is on, `-debug`,
    `-debug=1` or `-debug=all` being among them. The second is
    `-debugexclude`'s, applied after `-debug`'s and refused the same way
    for a category Core does not know; `all` excludes every one.
    """
    categories = _get_args(settings, "debug")
    discard = [i for i, category in enumerate(categories) if category in _DEBUG_NONE]
    enabled = categories[discard[-1] + 1 :] if discard else categories
    for category in enabled:
        if category not in _DEBUG_ALL and category not in _LOG_CATEGORIES:
            err_msg = f"Unsupported logging category -debug={category}."
            raise ValueError(err_msg)
    excluded: set[str] = set()
    for category in _get_args(settings, "debugexclude"):
        if category in _DEBUG_ALL:
            excluded |= _LOG_CATEGORIES
        elif category in _LOG_CATEGORIES:
            excluded.add(category)
        else:
            err_msg = f"Unsupported logging category -debugexclude={category}."
            raise ValueError(err_msg)
    if any(category in _DEBUG_ALL for category in enabled):
        return True, frozenset(), frozenset(excluded)
    return bool(enabled), frozenset(enabled), frozenset(excluded)


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
    creation `Node.__init__`'s own `mkdir` (`__init__.py`) already gives it,
    the same shape Core's own default path gets from `GetBlocksDirPath`'s
    `fs::create_directories`.
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


def _settings_path(settings: _Settings, net_dir: str) -> str | None:
    """Return Core's `GetSettingsPath`; `None` under `-nosettings`.

    `GetPathArg("-settings", "settings.json")` (`src/common/args.cpp`, at
    bitcoin/bitcoin@9be056a8a7) joined to the chain's data directory
    `net_dir`, an absolute `-settings` standing as it is.
    """
    if _is_negated(settings, "settings"):
        return None
    name = _get_arg(settings, "settings")
    return os.path.join(net_dir, get_path_arg(name or SETTINGS_FILENAME))  # noqa: PTH118


def _make_data_dirs(net_dir: str) -> None:
    """Make the chain's data directory where missing, as `InitConfig` does.

    It makes no `wallets` directory, which `SECURITY.md`'s *Where this node
    departs from Bitcoin Core* explains. A failure is Python's words rather
    than the C++ library's.
    """
    if not os.path.exists(net_dir):  # noqa: PTH110
        try:
            Path(net_dir).mkdir(exist_ok=True, parents=True)
        except OSError as os_error:
            raise ValueError(str(os_error)) from None


def _init_settings_file(settings: _Settings, net_dir: str) -> None:
    """Read `settings.json`, warn of its unknown names, and write it back.

    `InitConfig` (`src/common/init.cpp`, at bitcoin/bitcoin@9be056a8a7)
    over `ArgsManager::ReadSettingsFile` and `WriteSettingsFile`
    (`src/common/args.cpp`), each failure refused in Core's words: "Settings
    file could not be read", or "written", and what `settings_file.py` says.
    A name no option has is warned of in the log alone. The path is asked
    for again by the write, as `WriteSettingsFile` does, after the file's
    own `settings` key has had its say.
    """
    path = _settings_path(settings, net_dir)
    if path is None:
        return
    try:
        settings.rw_settings = read_settings(path)
    except OSError as os_error:
        # `fs::exists` throws, and `InitConfig` shows libstdc++'s `what()`
        err_msg = (
            f"filesystem error: cannot get file status: {os_error.strerror} [{path}]"
        )
        raise ValueError(err_msg) from None
    except ValueError as error:
        err_msg = f"Settings file could not be read:\n- {error}"
        raise ValueError(err_msg) from None
    settings.log_warnings.extend(
        f"Ignoring unknown rw_settings value {name}"
        for name in settings.rw_settings
        if _interpret_key(name).name not in _OPTIONS
    )
    path = _settings_path(settings, net_dir)
    if path is None:
        err_msg = "Attempt to write settings file when dynamic settings are disabled."
        raise ValueError(err_msg)
    try:
        write_settings(path, settings.rw_settings)
    except ValueError as error:
        err_msg = f"Settings file could not be written:\n- {error}"
        raise ValueError(err_msg) from None


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


def _get_minimum_chain_work(settings: _Settings) -> int | None:
    """Return `-minimumchainwork` as an int, `None` where it is not given.

    `node::ApplyArgsManOptions` (`src/node/chainstatemanager_args.cpp`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `uint256::FromUserHex`
    through `_parse_hex_uint256`, refused in Core's own words. A `None`
    return is `Config.__init__`'s own to default, to the chain's own
    `minimum_chain_work` -- which chain that is is not decided until
    then.
    """
    value = _get_arg(settings, "minimumchainwork")
    if value is None:
        return None
    work = _parse_hex_uint256(value)
    if work is None:
        err_msg = (
            f"Invalid minimum work specified ({value}), must be up to "
            f"{_MIN_WORK_HEX_DIGITS} hex digits"
        )
        raise ValueError(err_msg)
    return work


def _get_assume_valid(settings: _Settings) -> bytes | None:
    """Return `-assumevalid` as a block hash, `None` where it is off.

    `node::ApplyArgsManOptions` (`src/node/chainstatemanager_args.cpp`,
    at bitcoin/bitcoin@9be056a8a7, the v31.1 tag): `uint256::FromUserHex`
    through `_parse_hex_uint256`, refused in Core's own words. Zero --
    `0`, `-noassumevalid`, a bare `-assumevalid` -- is Core's "verify all",
    and is `None` here, as is a value not given, until Core's default
    per network is read (btclib-org/btclib-node#1576, a later pull
    request).
    """
    value = _get_arg(settings, "assumevalid")
    if value is None:
        return None
    block_hash = _parse_hex_uint256(value)
    if block_hash is None:
        err_msg = (
            f"Invalid assumevalid block hash specified ({value}), must be up "
            f"to {_MIN_WORK_HEX_DIGITS} hex digits (or 0 to disable)"
        )
        raise ValueError(err_msg)
    return block_hash.to_bytes(_MIN_WORK_HEX_DIGITS // 2) if block_hash else None


def _get_max_tip_age(settings: _Settings) -> int:
    """Return `-maxtipage` in seconds, Core's `ApplyArgsManOptions`.

    `src/node/chainstatemanager_args.cpp`, at bitcoin/bitcoin@9be056a8a7,
    the v31.1 tag: `GetIntArg`, through `_get_int` above, which already
    reads a negation as `0` and a double negation as `1` the way Core's
    own `SettingToInt` does.
    """
    value = _get_int(settings, "maxtipage")
    return DEFAULT_MAX_TIP_AGE if value is None else value


def _get_feerate(settings: _Settings, name: str) -> FeeRate | None:
    """Return `-name` as a rate, `None` where it is not given.

    BTC/kvB through `ParseMoney`, refused with `AmountErrMsg`'s words
    (`src/common/messages.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag); a negation reads as `0`.
    """
    value = _get_arg(settings, name)
    if value is None:
        return None
    amount = _parse_money(value)
    if amount is None:
        err_msg = f"Invalid amount for -{name}=<amount>: '{value}'"
        raise ValueError(err_msg)
    return FeeRate(sats_per_kvbyte=amount)


@dataclass(frozen=True)
class _MempoolOptions:
    """The `MemPoolOptions` this node carries.

    The fields are `Config`'s of the same names.
    """

    min_relay_feerate: FeeRate
    incremental_relay_feerate: FeeRate
    dust_relay_feerate: FeeRate
    permit_bare_multisig: bool
    max_datacarrier_bytes: int | None
    require_standard: bool
    mempool_expiry: int


def _get_mempool_options(settings: _Settings, chain_name: str) -> _MempoolOptions:
    """Return the mempool's options, Core's `ApplyArgsManOptions`.

    `src/node/mempool_args.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    tag, in its order of refusals: `-incrementalrelayfee`, `-minrelaytxfee`,
    `-dustrelayfee`, then `-acceptnonstdtxn` on a chain that is not a test
    chain. `-incrementalrelayfee` above `-minrelaytxfee` raises it where
    `-minrelaytxfee` is not given, which Core also logs; this node does
    not, the value being in `getmempoolinfo`'s `minrelaytxfee`.
    `-datacarriersize` is a signed 64-bit read stored in an unsigned
    32-bit field, so it wraps; `-nodatacarrier` is `None`.
    `-mempoolexpiry` is read in hours and kept in seconds.
    """
    hours = _get_int(settings, "mempoolexpiry")
    expiry = 3600 * (DEFAULT_MEMPOOL_EXPIRY_HOURS if hours is None else hours)
    incremental = _get_feerate(settings, "incrementalrelayfee")
    if incremental is None:
        incremental = DEFAULT_INCREMENTAL_RELAY_FEERATE
    min_relay = _get_feerate(settings, "minrelaytxfee")
    if min_relay is None:
        min_relay = DEFAULT_MIN_RELAY_FEERATE
        if incremental.sats_per_kvbyte > min_relay.sats_per_kvbyte:
            min_relay = incremental
    dust = _get_feerate(settings, "dustrelayfee")
    if dust is None:
        dust = DEFAULT_DUST_RELAY_FEERATE
    datacarrier = _get_bool(settings, "datacarrier")
    max_datacarrier_bytes: int | None = None
    if datacarrier is None or datacarrier:
        size = _get_int(settings, "datacarriersize")
        max_datacarrier_bytes = (
            DEFAULT_MAX_DATACARRIER_BYTES if size is None else size % 2**32
        )
    permit_bare_multisig = _get_bool(settings, "permitbaremultisig")
    require_standard = not _get_bool(settings, "acceptnonstdtxn")
    if chain_name == "mainnet" and not require_standard:
        err_msg = (
            "acceptnonstdtxn is not currently supported for "
            f"{_CHAIN_SECTION[chain_name]} chain"
        )
        raise ValueError(err_msg)
    return _MempoolOptions(
        min_relay,
        incremental,
        dust,
        permit_bare_multisig is None or permit_bare_multisig,
        max_datacarrier_bytes,
        require_standard,
        expiry,
    )


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
    # made absolute as `GetDataDir` makes it (`src/common/args.cpp`,
    # at bitcoin/bitcoin@9be056a8a7). A value that is `.` once normal is
    # where a path a refusal names still differed from Core's: Core joins
    # the `.` on (`fs::absolute`, `AbsPathForConfigVal`, both a literal
    # `operator/` with no lexical pass of their own) and `pathlib.Path`
    # drops it on construction and on `/` alike, so `base_display` below
    # is the same join done as a string, kept alongside the `Path` every
    # actual filesystem read and `Config` still use
    # (btclib-org/btclib-node#1273).
    datadir = _get_arg(settings, "datadir")
    base_dir = default_data_dir()
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
    settings.config_file_line = "Config file: <disabled>"
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
        # `StartLogging`'s other two lines, for a directory and for a
        # `-conf` that is absent, are refused above by `_read_conf_file`
        # before any log is open, as `ReadConfigFiles` refuses them
        # before `StartLogging`
        settings.config_file_line = (
            f"Config file: {conf_path}"
            if os.path.exists(conf_path)  # noqa: PTH110
            else f"Config file: {conf_path} (not found, skipping)"
        )
    chain_name = _resolve_chain_name(settings)
    settings.network = _CHAIN_SECTION[chain_name]
    net_dir = os.path.join(base_display, chain_name)  # noqa: PTH118
    _make_data_dirs(net_dir)
    _check_ignored_conf(settings, base_dir, conf_path)
    _init_settings_file(settings, net_dir)
    # `AppInitParameterInteraction` asks again, with the file's values in
    _resolve_chain_name(settings)

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
    debug_categories: frozenset[str]
    debug_exclude: frozenset[str]
    # `-minimumchainwork`'s own `None` for "not given", `Config.__init__`
    # left to default it once `chain_name` above resolves
    minimum_chain_work: int | None
    assume_valid: bytes | None
    max_tip_age: int
    prune: int
    mempool: _MempoolOptions
    directories: Config


def _resolve_listen(settings: _Settings, max_connections_arg: int) -> bool:
    """Return `-listen` as `InitParameterInteraction` leaves it.

    `src/init.cpp`, at bitcoin/bitcoin@9be056a8a7: a `-bind` or a
    `-whitebind` soft-sets it on, ahead of `-connect` or a `-maxconnections`
    of zero or less soft-setting it off, and an explicit value wins over
    both.
    """
    listen = _get_bool(settings, "listen")
    if listen is None:
        connect = _get_args(settings, "connect")
        listen = (
            bool(_get_args(settings, "bind"))
            or bool(_get_args(settings, "whitebind"))
            or (
                not connect
                and not _is_negated(settings, "connect")
                and max_connections_arg > 0
            )
        )
    return listen


def _unsuitable_section_only_args(settings: _Settings) -> list[str]:
    """Return Core's `GetUnsuitableSectionOnlyArgs` (`common/args.cpp:134`).

    `main`'s own default section is never questioned, its own early
    return there; off `main`, a `network_only` name whose only non-empty
    source is the default section is what it collects --
    `OnlyHasDefaultSectionSetting` (`src/common/settings.cpp`, same sha),
    over the same sources `_sources` above already reads. A
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
    `NETWORK_ONLY` options, as it is among this node's.
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
    prints it, and set as `settings.section_warning` too, that function
    logging it as well -- after Core's own version line, `open_history_log`'s
    own order for it.
    """
    lines = "".join(
        f"{filepath}:{lineno} Section [{name}] is not recognized.\n"
        for name, filepath, lineno in settings.config_sections
        if name not in _RECOGNIZED_SECTIONS
    )
    if lines:
        sys.stderr.write(f"Warning: {lines}\n")
        settings.section_warning = lines


def _before_lock(argv: Sequence[str]) -> _BeforeLock:
    """Read `argv` and its file, and refuse what Core refuses before its lock.

    `InitConfig`, then `AppInitParameterInteraction` (`src/init.cpp`, at
    bitcoin/bitcoin@9be056a8a7) in its order: a `NETWORK_ONLY` option set
    only in the default section off `main`, the warning about a section
    naming no chain, a missing blocks directory, `-forcednsseed` beside
    a `-dnsseed` that is off, a negative `-maxconnections`, `-debug`'s
    categories, `-minimumchainwork`, `-assumevalid`, `-maxtipage` (the order
    `node::ApplyArgsManOptions`'s own chainstate-manager options are
    read in, `src/node/chainstatemanager_args.cpp`, same sha), `-prune`,
    the mempool's options.
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
        listen=_resolve_listen(settings, max_connections_arg),
        bind=_get_args(settings, "bind"),
        whitebind=_get_args(settings, "whitebind"),
    )
    debug, debug_categories, debug_exclude = _resolve_debug(settings)
    # after `-debug`'s categories, where `AppInitParameterInteraction`
    # applies the chainstate manager's options, ahead of the blockmanager's
    # (`-prune`, below) and the mempool's (btclib-org/btclib-node#1332)
    minimum_chain_work = _get_minimum_chain_work(settings)
    assume_valid = _get_assume_valid(settings)
    max_tip_age = _get_max_tip_age(settings)
    prune = _prune_target_mib(_get_int(settings, "prune") or 0)
    mempool = _get_mempool_options(settings, chain_name)
    return _BeforeLock(
        settings,
        base_dir,
        chain_name,
        blocksdir,
        max_connections_arg,
        max_connections,
        debug,
        debug_categories,
        debug_exclude,
        minimum_chain_work,
        assume_valid,
        max_tip_age,
        prune,
        mempool,
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
    directories.data_dir.mkdir(mode=DIR_MODE, exist_ok=True, parents=True)
    blocks_dir.mkdir(mode=DIR_MODE, exist_ok=True, parents=True)
    return lock_directories(directories.data_dir, blocks_dir)


def _resolve_externalip(values: list[str], default_port: int) -> list[str]:
    """Return every `-externalip` value as an address and port, looked up.

    `AppInitMain` (`src/init.cpp`, at bitcoin/bitcoin@9be056a8a7): each
    value is looked up at `-port`, names allowed (`-dns`'s default), and
    refused where nothing answers or the address is not valid. The port
    is `GetListenPort`'s, which `listen_port` reads.
    """
    resolved: list[str] = []
    for value in values:
        service = lookup_service(value, default_port, allow_lookup=True)
        if service is None or not is_valid_host(service[0]):
            err_msg = f"Cannot resolve -externalip address: '{value}'"
            raise ValueError(err_msg)
        resolved.append(service_text(*service))
    return resolved


def _warn_bad_port(option: str, port: int) -> None:
    """Warn of a port other services listen on, as `BadPortWarning` does.

    `InitWarning` (`src/init.cpp`, at bitcoin/bitcoin@9be056a8a7) prints
    `Warning: ` and the text on stderr, as `noui_ThreadSafeMessageBox`
    does. Core logs it too, which is not done here.
    """
    if port in BAD_PORTS:
        sys.stderr.write(
            f"Warning: {option} request to listen on port {port}. This port is "
            'considered "bad" and thus it is unlikely that any peer will '
            "connect to it. See doc/p2p-bad-ports.md for details and a full "
            "list.\n"
        )


def _check_bind(
    values: list[str], whitebind: list[str], default_port: int, port: int | None
) -> None:
    """Refuse a `-bind` or `-whitebind` that is none, or an address named twice.

    `AppInitMain`'s `-bind` and `-whitebind` loops and
    `CheckBindingConflicts` (`src/init.cpp`, at
    bitcoin/bitcoin@9be056a8a7), which sees the `-whitebind` addresses,
    then the plain binds, then the `=onion` ones, the default one among
    them, and compares address and port. The loop warns of each plain
    `-bind` on a bad port, and then of `-port` where it is given and
    neither is, `-port` being ignored otherwise.
    """
    parsed = []
    for value in values:
        address = parse_bind(value, default_port)
        if not address.onion:
            _warn_bad_port("-bind", address.port)
        parsed.append(address)
    whitebound = [parse_whitebind(value) for value in whitebind]
    if not values and not whitebind and port is not None:
        _warn_bad_port("-port", port)
    ordered: list[BindAddress | WhitebindAddress] = [
        *whitebound,
        *sorted(parsed, key=lambda a: a.onion),
    ]
    if not values:
        ordered.append(default_onion_bind(default_port))
    seen = set()
    for bound in ordered:
        key = (str(bound.host).partition("%")[0], bound.port)
        if key in seen:
            err_msg = (
                "Duplicate binding configuration for address "
                f"{service_text(bound.host, bound.port)}. Please check "
                "your -bind, -bind=...=onion and -whitebind settings."
            )
            raise ValueError(err_msg)
        seen.add(key)


def _after_lock(before: _BeforeLock) -> Config:
    """Refuse what Core refuses after its lock, and return the `Config`.

    `AppInitMain` (`src/init.cpp`, at bitcoin/bitcoin@9be056a8a7) in its
    order: `CheckHostPortOptions`'s `-port`, `-rpcport`, `-bind`,
    `-rpcbind` and `-whitebind`, then the `-externalip` and `-bind` values
    nothing resolves, a `-whitebind` Core refuses, and an address bound
    twice.
    `-rpccookieperms` and `-rpcauth` are refused later, by
    `RpcAuth.start`, as `StartHTTPRPC` refuses them.
    """
    settings = before.settings
    p2p_port = _get_port(settings, "port")
    rpc_port = _get_port(settings, "rpcport")
    # Every `-bind`, `-rpcbind` and `-whitebind` value is checked, as
    # `CheckHostPortOptions` checks it, `-bind`'s without its `=onion`
    # tag; `RpcManager` binds the second beside `-rpcallowip`, as
    # `HTTPBindAddresses` (`src/httpserver.cpp`, same sha) does
    bind = _get_args(settings, "bind")
    rpcbind = _get_args(settings, "rpcbind")
    whitebind = _get_args(settings, "whitebind")
    for name, values in (
        ("bind", bind),
        ("rpcbind", rpcbind),
        ("whitebind", whitebind),
    ):
        for value in values:
            head, tagged, _ = value.rpartition("=")
            try:
                split_host_port(head if tagged and name == "bind" else value, 0)
            except ValueError:
                err_msg = f"Invalid port specified in -{name}: '{value}'"
                raise ValueError(err_msg) from None
    default_port = p2p_port or before.directories.chain.port
    externalip = _resolve_externalip(
        _get_args(settings, "externalip"),
        listen_port(bind, whitebind, default_port),
    )
    _check_bind(bind, whitebind, default_port, p2p_port)

    connect = _get_args(settings, "connect")
    # `-noconnect` is Core's `-connect=0`: no automatic connection, and
    # nobody named (`src/init.cpp`, at bitcoin/bitcoin@9be056a8a7)
    connect_negated = _is_negated(settings, "connect")
    # `InitParameterInteraction`'s soft-set, which an explicit value wins
    # over (`src/init.cpp`, same sha)
    listen = _resolve_listen(settings, before.max_connections_arg)
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
    # `DEFAULT_V2_TRANSPORT` (true) where `-v2transport` is not given
    v2transport = _get_bool(settings, "v2transport")
    if v2transport is None:
        v2transport = True
    # `Config` resolves an unset `-v1transport`
    v1transport = _get_bool(settings, "v1transport")
    # `DEFAULT_WHITELISTRELAY` (true) where `-whitelistrelay` is not given
    whitelist_relay = _get_bool(settings, "whitelistrelay")
    if whitelist_relay is None:
        whitelist_relay = True
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
    # `-rpcservertimeout`, which `InitHTTPServer` hands to libevent's
    # `evhttp_set_timeout` as a 32-bit `int`
    # (`src/httpserver.cpp`, at bitcoin/bitcoin@9be056a8a7, the v31.1
    # tag): the same `int64_t`-to-`int` narrowing `-maxconnections`
    # above takes through `_to_int`.
    rpcservertimeout = _get_int(settings, "rpcservertimeout")
    rpcservertimeout = (
        int(REQUEST_TIMEOUT) if rpcservertimeout is None else _to_int(rpcservertimeout)
    )

    return Config(
        chain=before.chain_name,
        data_dir=before.base_dir,
        blocks_dir=before.blocksdir,
        p2p_port=p2p_port,
        rpc_port=rpc_port,
        rpcbind=tuple(rpcbind),
        bind=bind,
        whitebind=whitebind,
        externalip=externalip,
        rpcallowip=_get_args(settings, "rpcallowip"),
        whitelist=_get_args(settings, "whitelist"),
        whitelist_relay=whitelist_relay,
        whitelist_force_relay=bool(_get_bool(settings, "whitelistforcerelay")),
        rpcservertimeout=rpcservertimeout,
        allow_rpc=server is None or server,
        pruned=bool(prune),
        prune_target_mib=prune if prune >= MIN_PRUNE_TARGET_MIB else None,
        debug=before.debug,
        debug_categories=before.debug_categories,
        debug_exclude=before.debug_exclude,
        log_rate_limit=_get_bool(settings, "logratelimit") is not False,
        connect=connect or (["0"] if connect_negated else []),
        addnode=_get_args(settings, "addnode"),
        seednode=_get_args(settings, "seednode"),
        listen=listen,
        discover=discover,
        peerblockfilters=peerblockfilters,
        v2transport=v2transport,
        v1transport=v1transport,
        max_connections=before.max_connections,
        dnsseed=dnsseed,
        forcednsseed=bool(_get_bool(settings, "forcednsseed")),
        fixed_seeds=fixedseeds,
        ban_time=ban_time,
        send_buffer_max_size=_get_thousands(
            settings, "maxsendbuffer", DEFAULT_MAXSENDBUFFER
        ),
        receive_flood_size=_get_thousands(
            settings, "maxreceivebuffer", DEFAULT_MAXRECEIVEBUFFER
        ),
        block_notify=_get_arg(settings, "blocknotify") or "",
        startup_notify=_get_arg(settings, "startupnotify") or "",
        shutdown_notify=_get_args(settings, "shutdownnotify"),
        alert_notify=_get_arg(settings, "alertnotify") or "",
        min_relay_feerate=before.mempool.min_relay_feerate,
        incremental_relay_feerate=before.mempool.incremental_relay_feerate,
        dust_relay_feerate=before.mempool.dust_relay_feerate,
        permit_bare_multisig=before.mempool.permit_bare_multisig,
        max_datacarrier_bytes=before.mempool.max_datacarrier_bytes,
        require_standard=before.mempool.require_standard,
        persist_mempool=_get_bool(settings, "persistmempool") is not False,
        persist_mempool_v1=bool(_get_bool(settings, "persistmempoolv1")),
        mempool_expiry=before.mempool.mempool_expiry,
        minimum_chain_work=before.minimum_chain_work,
        assume_valid=before.assume_valid,
        max_tip_age=before.max_tip_age,
        accept_stale_fee_estimates=bool(_get_bool(settings, "acceptstalefeeestimates")),
        rpcauth=_get_args(settings, "rpcauth"),
        rpcuser=_get_arg(settings, "rpcuser") or "",
        rpcpassword=_get_arg(settings, "rpcpassword") or "",
        rpccookiefile=rpccookiefile,
        rpccookieperms=_get_arg(settings, "rpccookieperms"),
        rpcwhitelist=_get_args(settings, "rpcwhitelist"),
        rpcwhitelistdefault=_get_bool(settings, "rpcwhitelistdefault"),
        log_warnings=settings.log_warnings,
        section_warning=settings.section_warning,
        config_args=_log_args(settings),
        config_file_line=settings.config_file_line,
    )


def build_config(argv: Sequence[str] | None = None) -> Config:
    """Parse `argv` (`sys.argv[1:]` if `None`) and its `-conf` into a `Config`.

    Raises `ValueError` on a malformed argument, a malformed
    configuration file, or an unknown chain, in the order `bitcoind`
    refuses them, and `SystemExit(0)` once the help is printed. No lock
    is taken: `main` takes it between `_before_lock` and `_after_lock`.
    """
    return _after_lock(_before_lock(sys.argv[1:] if argv is None else argv))


# Core's `SetupEnvironment` (`src/common/system.cpp`,
# at bitcoin/bitcoin@9be056a8a7, the v31.1 tag), which `bitcoind`'s own
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
    bitcoin/bitcoin@9be056a8a7), and `bitcoind` exits `EXIT_FAILURE`. A
    refusal after the lock reaches `history.log` too, `open_history_log`
    opened for it with no `Node` built, `LogError`'s own second
    destination for the same message.
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
            directories = before.directories
            log_path = (
                directories.data_dir / directories.log_path
                if directories.log_path
                else None
            )
            # `AppInitMain`'s own refusals -- `CheckHostPortOptions`'s,
            # which raises this one -- come after `init::StartLogging`
            # (`src/init.cpp`, at bitcoin/bitcoin@9be056a8a7), so the log
            # is already open by the time one of them fires, and
            # `LogError`, reached through `noui_ThreadSafeMessageBox`,
            # puts the message in it too
            logger = open_history_log(
                log_path,
                debug=before.debug,
                debug_categories=before.debug_categories,
                debug_exclude=before.debug_exclude,
                data_dir=directories.data_dir,
                config_file_line=before.settings.config_file_line,
                log_warnings=before.settings.log_warnings,
                section_warning=before.settings.section_warning,
                config_args=_log_args(before.settings),
                rate_limit=_get_bool(before.settings, "logratelimit") is not False,
            )
            logger.error(str(error))  # noqa: TRY400
            logger.close()
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
