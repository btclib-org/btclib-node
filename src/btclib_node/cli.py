# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`main`, the command line `pip install btclib-node` installs.

`argparse`, not click: `pyproject.toml`'s `[project.scripts]` entry
below is the only new surface this adds, and this module is what it
points at. click would be this wheel's second runtime dependency --
`.github/scripts/generate_sbom.py` names exactly one today, `btclib`
itself -- for what a flag list this size does not need: `argparse` costs
nothing on the SBOM and reads a single-dash long option
(`parser.add_argument("-datadir")`) the same way Core's own `ArgsManager`
does, which is why every flag below is spelled `-datadir` rather than
`--datadir`, though the double-dash form is accepted too (Core
normalises a leading `--` to `-` before parsing a key, `src/common/
args.cpp:221-222`, at bitcoin/bitcoin@ca7162cde5, and `add_argument`
below registers both spellings for the same reason). The decision and
its SBOM consequence are btclib-org/btclib-node#583's own to record;
this sentence is this module's copy of it, not a second decision.

Every flag below is one of `Config`'s own fields (`config.py`), named
and defaulted the way `SetupServerArgs` (`src/init.cpp`, same sha)
names and defaults its own -- `-datadir=<dir>`, `-blocksdir=<dir>`,
`-conf=<file>`, `-chain=`/`-testnet`/`-signet`/`-regtest`, `-port=`,
`-rpcport=`, `-rpcbind=`, `-prune=`, `-debug`, `-connect=`,
`-addnode=`, `-listen=`/`-nolisten`. Three `Config` fields have no flag
here: `min_relay_feerate` (Core's own
`-minrelaytxfee` is BTC/kvB and this field is priced in sat/kvB
already, `config.py`'s own comment on `DEFAULT_MIN_RELAY_FEERATE`
argues why nothing enforces it yet; the unit translation is deferred
rather than done half-heartedly), `log_path` (this command always
takes `Config`'s own default -- a file under the data directory -- an
operator who wants console output can read it from there, and
`scripts/chains/`'s three deleted files are what used to make that
choice for a caller who ran them directly instead), and `allow_p2p`/
`allow_rpc` (both listeners are always requested, matching every
`Config()` call this module makes; there is no flag equivalent to
`allow_rpc=False` here because Core has none either -- the RPC server
not starting is a consequence of a bind failure, not a flag).

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
(`src/common/config.cpp`, same sha) read it, the default path itself
computed by `ArgsManager::GetConfigFilePath`
(`src/common/args.cpp:897`, same sha): a
`key=value` line per option, named without the leading `-` this
module's own flags carry; `#` starts a comment that runs to the end of
the line; a blank line and a comment-only line are skipped; a
`[section]` line switches which section the lines under it belong to,
until the next one. Every chain has its own section, `main` included
(`ChainTypeToString`, `src/util/chaintype.cpp`) -- not only the three
alternate chains -- and the *default*, unlabelled section at the top of
the file applies everywhere `chain`/`testnet`/`signet`/`regtest`
themselves are never read from a chain's own section, only from the
default one and the command line, which is what lets a file decide the
chain in the first place rather than needing the chain decided already
to know which section answers that question.

Precedence is the command line over the file, always. Within the file,
a value in the active chain's own section beats one in the default
section, and where a scalar option (`-port`, `-rpcport`, `-rpcbind`,
`-prune`) is named more than once at one precedence level the last one
in the file wins -- Core reverses that for backward compatibility
(`GetSetting`'s own "Weird behavior preserved" comment,
`src/common/settings.cpp:170-177`, same sha); this reader does not
replicate the reversal, there being no existing file this tree has to
stay compatible with. `-connect` and `-addnode` are not scalars: every
value from every source that applies is dialled, none of them
replacing another, which is Core's own `GetArgs`/`GetSettingsList`
shape for a repeatable option (`src/common/settings.cpp:210-246`).

Not every option answers to the file the same way once the chain is
not `main`: `-port`, `-rpcport`, `-rpcbind`, `-connect` and `-addnode`
are each declared `NETWORK_ONLY` in Core (`src/init.cpp`'s own
`AddArg` calls for each), so the default section's own value for one of
these five is ignored once running testnet, signet or regtest --
only that chain's own section and the command line still reach it.
`-prune` and `-debug` are not network-only and are read from the
default section on every chain. This reader keeps exactly that split;
`_NETWORK_ONLY_KEYS` below is where it is written down.

`includeconf=<file>`, resolved relative to the data directory the way
Core resolves it, is read only from the root file's own default
section: Core additionally honours one named inside the active chain's
own section, and one nested inside an included file is warned about
and ignored rather than followed (`ReadConfigFiles`, same file,
161-224) -- neither of those two narrower cases is replicated here,
the common shape being one `includeconf=` naming a secrets file from
the top of an otherwise ordinary `bitcoin.conf`. `-includeconf` is not
a flag of this module's own: Core accepts it on the command line only
negated (`-noincludeconf`), and this module has no generic negation --
`-nolisten` is the one negated spelling it registers, by hand, because
Core's own `-connect` interaction turns `-listen` off and an operator
needs a way to say so -- so `-includeconf` is simply not registered as
a flag here at all, which refuses it exactly where the negated case
would have covered no other command line spelling anyway. `conf=`
inside a file is refused the way Core refuses it -- fatally, "conf
cannot be set in a configuration file" -- and `datadir=` inside one is
not read at all
(unlike Core, which lets a file move the data directory read *after*
the file naming it was found): this module needs a `-datadir` before it
can know a file's own default path, so a value the file might carry for
it can never be the one that located that same file, and honouring it
for anything read afterwards would make the same key mean two
different things depending on when it is read. Warned about on stderr
with its own message rather than the generic one below, since `datadir`
is a real, documented flag and not a typo the generic message would
have a reader believe it was.

An unrecognised key in the file is warned about, on stderr, and
ignored -- Core's own default (`ReadConfigFiles(error,
/*ignore_invalid_keys=*/true)`, called this way from `bitcoin.cpp`,
`common/init.cpp` and `bitcoin-cli.cpp` alike, same sha) rather than
the fatal alternative that flag also allows. An unrecognised key on the
command line is refused by `argparse` itself, the same way Core refuses
one there too ("Invalid parameter %s").
"""

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from btclib_node import Node, install_signal_handlers
from btclib_node.config import Config, split_host_port

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
# `-chain=` flag and a file's own `chain=` value both go through.
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

# `NETWORK_ONLY` in Core's own `AddArg` calls for each
# (`src/init.cpp:607` `-port`, `:744` `-rpcbind`, `:750` `-rpcport`,
# `:566` `-addnode`, `:577` `-connect`, same sha): the default section
# of a config file answers for one of these only while running
# mainnet; on any other chain, only that chain's own section and the
# command line still reach it. `-prune` and `-debug` are `ALLOW_ANY`
# with no `NETWORK_ONLY`, so the default section always answers for
# them.
_NETWORK_ONLY_KEYS = frozenset({"port", "rpcport", "rpcbind", "connect", "addnode"})

# Every key this reader honours from a file, chain selectors and
# `includeconf` included even though neither is read back out of
# `_collect_file_values` below -- both are consumed earlier, and
# staying in this set is what keeps either from being warned about as
# unrecognised.
_RECOGNIZED_KEYS = frozenset(
    {
        "chain",
        "testnet",
        "signet",
        "regtest",
        "port",
        "rpcport",
        "rpcbind",
        "prune",
        "debug",
        "connect",
        "addnode",
        "listen",
        "blocksdir",
        "includeconf",
    }
)

# What `_resolve_bool` below treats as false: Core's own `InterpretBool`
# (`src/common/args.cpp`) special-cases a handful of spellings and
# raises on the rest; a file in this tree instead follows the one
# spelling every example in Core's own documentation and every example
# in this module's own tests uses, `<key>=0` for false and anything
# else -- `1` above all -- for true.
_FALSE = "0"

_ConfSection = dict[str, list[str]]
_ConfTree = dict[str | None, _ConfSection]


def _parse_conf_text(text: str, path: str) -> _ConfTree:
    """Parse `text` into `{section: {key: [values]}}`, in file order.

    Core's own config-file grammar (`GetConfigOptions`,
    `src/common/config.cpp:32-75`, at bitcoin/bitcoin@ca7162cde5): one
    `key=value` or `[section]` per non-blank, non-comment line. Raises
    `ValueError` on a line matching neither shape, on one starting with
    `-` (an option is named without it in a file), and on a `conf=`
    key -- the three parse errors Core's own reader raises for too.
    """
    sections: _ConfTree = {None: {}}
    section: str | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line[0] == "[" and line[-1] == "]":
            section = line[1:-1]
            sections.setdefault(section, {})
            continue
        if line[0] == "-":
            err_msg = (
                f"{path}:{lineno}: options in a configuration file are "
                "given without a leading -"
            )
            raise ValueError(err_msg)
        if "=" not in line:
            err_msg = f"{path}:{lineno}: not a key=value line: {line!r}"
            raise ValueError(err_msg)
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "conf":
            err_msg = f"{path}:{lineno}: conf cannot be set in a configuration file"
            raise ValueError(err_msg)
        sections.setdefault(section, {}).setdefault(key, []).append(value)
    return sections


def _read_conf_file(path: Path, *, required: bool) -> _ConfTree:
    """Read and parse `path`; `{None: {}}` if it is missing and not `required`.

    Core's own "ok to not have a config file" (`ReadConfigFiles`, same
    file, line 156) for the default filename, which is what
    `required=False` is for. `required=True` is what `-conf`
    explicitly naming a file gets instead: a missing or unreadable one
    is fatal there, the same as Core's own "specified config file ...
    could not be opened".

    A directory is checked with `is_dir()` before the file is opened,
    matching `ReadConfigFiles`'s own `fs::is_directory(conf_path)` guard
    (same file, line 145, at bitcoin/bitcoin@ca7162cde5), which runs
    before the stream is ever opened rather than reading the failure an
    open attempt raises. Catching the open failure instead would depend
    on the platform: opening a directory raises `IsADirectoryError`
    (`errno.EISDIR`) on POSIX and `PermissionError` (`errno.EACCES`) on
    Windows, so a handler for one platform's exception class is not
    reached by the other's error.
    """
    if path.is_dir():
        err_msg = f"configuration file {path} is a directory"
        raise ValueError(err_msg)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            err_msg = f"specified configuration file {path} could not be opened"
            raise ValueError(err_msg) from None
        return {None: {}}
    return _parse_conf_text(text, str(path))


def _load_conf_tree(
    conf_path: Path, *, conf_explicit: bool, base_dir: Path
) -> _ConfTree:
    """Read `conf_path`, then every `includeconf` its default section names.

    Each included file's own sections are merged into the same tree,
    appended after the root file's own values for that section --
    `ReadConfigStream` appends into one shared `ro_config[section][key]`
    list regardless of which file contributed a value, root first
    (`ReadConfigFiles`, same file, 132-225).
    """
    tree = _read_conf_file(conf_path, required=conf_explicit)
    for name in tree.get(None, {}).get("includeconf", []):
        include_path = Path(name)
        if not include_path.is_absolute():
            include_path = base_dir / include_path
        included = _read_conf_file(include_path, required=True)
        for section, keys in included.items():
            dest = tree.setdefault(section, {})
            for key, values in keys.items():
                if key == "includeconf":
                    continue
                dest.setdefault(key, []).extend(values)
    return tree


def _resolve_bool(cli_value: bool, key: str, default_section: _ConfSection) -> bool:  # noqa: FBT001
    """Return whether `key` is true, `cli_value` over the file's own last value.

    `cli_value` wins if the flag was passed; otherwise the file's own
    last `key=value` in the default section; otherwise `False`.
    """
    if cli_value:
        return True
    values = default_section.get(key)
    if not values:
        return False
    return values[-1] != _FALSE


def _resolve_listen(
    cli_value: str | None, default_section: _ConfSection, *, connect_given: bool
) -> bool:
    """Return whether to bind and accept inbound connections.

    Unlike `_resolve_bool` above, `-listen`'s own default is not always
    `False`: Core's `DEFAULT_LISTEN` is true, except under `-connect`,
    where `InitParameterInteraction` (`src/init.cpp:814-819`, at
    bitcoin/bitcoin@ca7162cde5) soft-sets it false -- soft, meaning an
    explicit `-listen`/`-nolisten`/`-listen=0`, from the command line or
    the file's own default section, still wins either way. `cli_value`
    is `None` when neither `-listen` nor `-nolisten` was given, `"0"`
    for `-nolisten`/`-listen=0`, and the flag's own text otherwise.
    """
    if cli_value is not None:
        return cli_value != _FALSE
    values = default_section.get("listen")
    if values:
        return values[-1] != _FALSE
    return not connect_given


def _resolve_chain_name(args: argparse.Namespace, default_section: _ConfSection) -> str:
    """Resolve `-chain`/`-testnet`/`-signet`/`-regtest`, cli over file.

    `chain`/`testnet`/`signet`/`regtest` are read from the file's
    default section only, never a chain's own section -- Core's own
    `get_net` lambda passes an empty section for exactly this lookup
    (`GetChainArg`, `src/common/args.cpp:922-950`, same sha), which is
    what lets a file decide the chain before any section but the
    default one can mean anything. At most one of the four may resolve
    true; more is the same "Invalid combination" Core refuses.
    """
    chain_values = default_section.get("chain")
    chain_alias = args.chain or (chain_values[-1] if chain_values else None)
    testnet = _resolve_bool(args.testnet, "testnet", default_section)
    signet = _resolve_bool(args.signet, "signet", default_section)
    regtest = _resolve_bool(args.regtest, "regtest", default_section)
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


def _collect_file_values(tree: _ConfTree, chain_name: str) -> dict[str, list[str]]:
    """Merge the default and chain-specific sections into one value per key.

    Every value from every applicable source, in the order
    `_NETWORK_ONLY_KEYS`'s own docstring above argues -- default
    section first (skipped for a network-only key off `mainnet`), then
    the chain's own section. A scalar reader takes the last entry; a
    list reader (`-connect`, `-addnode`) takes all of them. Also warns,
    on stderr, about every key present in either section that this
    reader does not recognise -- Core's own default
    (`ignore_invalid_keys=True`, this module's own docstring).
    """
    default_section = tree.get(None, {})
    chain_section = tree.get(_CHAIN_SECTION[chain_name], {})
    collected: dict[str, list[str]] = {}
    for key in _RECOGNIZED_KEYS:
        values: list[str] = []
        if chain_name == "mainnet" or key not in _NETWORK_ONLY_KEYS:
            values.extend(default_section.get(key, []))
        values.extend(chain_section.get(key, []))
        if values:
            collected[key] = values
    for section in (default_section, chain_section):
        for key in section:
            if key == "datadir":
                sys.stderr.write(
                    "warning: -datadir cannot be set in a configuration file, "
                    "since the file itself has to be found first\n"
                )
            elif key not in _RECOGNIZED_KEYS:
                sys.stderr.write(
                    f"warning: ignoring unknown configuration value {key}\n"
                )
    return collected


def _resolve_str(
    cli_value: str | None, collected: dict[str, list[str]], key: str
) -> str | None:
    """Return `cli_value`, or the file's own last collected value for `key`."""
    if cli_value is not None:
        return cli_value
    values = collected.get(key)
    return values[-1] if values else None


def _resolve_int(
    cli_value: int | None, collected: dict[str, list[str]], key: str
) -> int | None:
    """Return `cli_value`, or the file's own last value for `key`, as an int."""
    if cli_value is not None:
        return cli_value
    values = collected.get(key)
    if not values:
        return None
    text = values[-1]
    try:
        return int(text)
    except ValueError:
        err_msg = f"{key}={text!r} is not an integer"
        raise ValueError(err_msg) from None


def _resolve_list(
    cli_values: list[str], collected: dict[str, list[str]], key: str
) -> list[str]:
    """Return `cli_values` followed by every collected value for `key`."""
    return [*cli_values, *collected.get(key, [])]


def _build_parser() -> argparse.ArgumentParser:
    """Return the parser, one argument per `Config` field this module owns.

    Every flag is registered under both a `-` and a `--` spelling
    (module docstring); `allow_abbrev=False` because Core's own parser
    does not treat a prefix of an option as the option either.
    """
    parser = argparse.ArgumentParser(
        prog="btclib-node",
        description="Run a bitcoin full node over btclib.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "-conf",
        "--conf",
        metavar="<file>",
        help=f"Specify path to configuration file (default: {_DEFAULT_CONF_FILENAME})",
    )
    parser.add_argument(
        "-datadir", "--datadir", metavar="<dir>", help="Specify data directory"
    )
    parser.add_argument(
        "-blocksdir",
        "--blocksdir",
        metavar="<dir>",
        help="Specify directory to hold blocks subdirectory for *.dat files "
        "(default: <datadir>)",
    )
    parser.add_argument(
        "-chain",
        "--chain",
        metavar="<chain>",
        choices=sorted(_CHAIN_ALIASES),
        help="Use the chain <chain>",
    )
    parser.add_argument(
        "-testnet", "--testnet", action="store_true", help="Use the test chain"
    )
    parser.add_argument(
        "-signet", "--signet", action="store_true", help="Use the signet chain"
    )
    parser.add_argument(
        "-regtest", "--regtest", action="store_true", help="Enter regression test mode"
    )
    parser.add_argument(
        "-port",
        "--port",
        metavar="<port>",
        type=int,
        help="Listen for peer connections on <port>",
    )
    parser.add_argument(
        "-rpcport",
        "--rpcport",
        metavar="<port>",
        type=int,
        help="Listen for JSON-RPC connections on <port>",
    )
    parser.add_argument(
        "-rpcbind",
        "--rpcbind",
        metavar="<addr>[:port]",
        help=(
            "Bind to given address to listen for JSON-RPC connections; "
            "port is optional and overrides -rpcport"
        ),
    )
    parser.add_argument(
        "-prune",
        "--prune",
        metavar="<n>",
        type=int,
        default=0,
        help=(
            "Reduce storage requirements by pruning old blocks: any nonzero value "
            "keeps the last 288 blocks and their undo data (about two days) and "
            "deletes the rest as the chain advances; Core's own -prune=<n> MiB "
            "target is not read, only whether <n> is zero"
        ),
    )
    parser.add_argument(
        "-debug",
        "--debug",
        action="store_true",
        help="Log at DEBUG level rather than INFO",
    )
    parser.add_argument(
        "-connect",
        "--connect",
        metavar="<ip>[:port]",
        action="append",
        default=[],
        help=(
            "Connect only to the specified node; disables DNS seeding and automatic "
            "connections. May be given multiple times."
        ),
    )
    parser.add_argument(
        "-addnode",
        "--addnode",
        metavar="<ip>[:port]",
        action="append",
        default=[],
        help=(
            "Add a node to connect to, alongside automatic connections. May be given "
            "multiple times."
        ),
    )
    parser.add_argument(
        "-listen",
        "--listen",
        dest="listen",
        metavar="<n>",
        nargs="?",
        const="1",
        default=None,
        help=(
            "Accept connections from outside (default: 1, unless -connect is given, "
            "which defaults it to 0)"
        ),
    )
    parser.add_argument(
        "-nolisten",
        "--nolisten",
        dest="listen",
        action="store_const",
        const="0",
        help="Same as -listen=0",
    )
    return parser


def _check_datadir(base_dir: Path) -> None:
    """Refuse an explicit `-datadir` that is not an existing directory.

    Core's own `CheckDataDirOption` (`src/common/args.cpp:891`, at
    bitcoin/bitcoin@ca7162cde5) -- `datadir.empty() ||
    fs::is_directory(fs::absolute(datadir))` -- validates `-datadir` as
    a directory separately from reading the config file, and
    `ReadConfigFiles` (`src/common/config.cpp:230-232`, same sha)
    answers "specified data directory ... does not exist." when it
    fails, called again there because a `datadir=` line inside the
    config file can still change it after the command-line value
    already passed this same check once. This function is
    `build_config`'s counterpart of the first call, ahead of
    `_load_conf_tree`; there is no second call here because a
    `datadir=` line inside a configuration file never reaches
    `base_dir` at all -- `_collect_file_values` (below) recognises the
    key, warns on stderr, and drops it rather than ever applying it.

    Missing and blocked-by-a-file are the same refusal here, matching
    Core exactly: `fs::is_directory` answers `False` for both, and so
    does `is_dir()`, so nothing here needs to tell them apart. The
    default (unset `-datadir`) path is never checked at all, matching
    Core's own `datadir.empty()` bypass -- `build_config` below only
    calls this when `args.datadir` was given -- and keeps the lazy
    creation `Node.__init__`'s own `mkdir(exist_ok=True, parents=True)`
    (`__init__.py`) already gives it, the same shape Core's own default
    path gets from `GetBlocksDirPath`'s `fs::create_directories`.
    """
    if not base_dir.is_dir():
        err_msg = f'specified data directory "{base_dir}" does not exist.'
        raise ValueError(err_msg)


def build_config(argv: Sequence[str] | None = None) -> Config:
    """Parse `argv` (`sys.argv[1:]` if `None`) and its `-conf` into a `Config`.

    Raises `ValueError` on a malformed argument, a malformed
    configuration file, or an unknown chain.
    """
    args = _build_parser().parse_args(argv)

    base_dir = Path(args.datadir) if args.datadir else Path.home() / ".btclib"
    if args.datadir:
        _check_datadir(base_dir)
    conf_explicit = args.conf is not None
    conf_value = Path(args.conf) if args.conf else Path(_DEFAULT_CONF_FILENAME)
    conf_path = conf_value if conf_value.is_absolute() else base_dir / conf_value

    tree = _load_conf_tree(conf_path, conf_explicit=conf_explicit, base_dir=base_dir)
    default_section = tree.get(None, {})
    chain_name = _resolve_chain_name(args, default_section)
    collected = _collect_file_values(tree, chain_name)

    p2p_port = _resolve_int(args.port, collected, "port")
    rpc_port = _resolve_int(args.rpcport, collected, "rpcport")
    rpc_host = "127.0.0.1"
    rpcbind = _resolve_str(args.rpcbind, collected, "rpcbind")
    if rpcbind is not None:
        rpc_host, rpcbind_port = split_host_port(rpcbind, 0)
        if rpcbind_port:
            rpc_port = rpcbind_port

    prune = _resolve_int(args.prune, collected, "prune")
    debug = _resolve_bool(args.debug, "debug", default_section)
    connect = _resolve_list(args.connect, collected, "connect")
    listen = _resolve_listen(args.listen, default_section, connect_given=bool(connect))
    blocksdir = _resolve_str(args.blocksdir, collected, "blocksdir")

    return Config(
        chain=chain_name,
        data_dir=base_dir,
        blocks_dir=blocksdir,
        p2p_port=p2p_port,
        rpc_port=rpc_port,
        rpc_host=rpc_host,
        pruned=bool(prune),
        debug=debug,
        connect=connect,
        addnode=_resolve_list(args.addnode, collected, "addnode"),
        listen=listen,
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Build a `Config` from the command line and `bitcoin.conf`, and run it.

    `Node` is a non-daemon thread (`__init__.py`'s own module
    docstring): once `node.start()` returns, this function itself has
    nothing left to do, and the interpreter stays up on that thread
    alone until a signal `install_signal_handlers` below caught stops
    it -- the same shape `scripts/chains/`'s three now-deleted files
    had, moved here.
    """
    try:
        config = build_config(argv)
    except ValueError as error:
        sys.stderr.write(f"btclib-node: {error}\n")
        raise SystemExit(1) from error

    node = Node(config=config)
    install_signal_handlers(node)
    node.start()
