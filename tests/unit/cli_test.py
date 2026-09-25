# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`cli.py`: argument parsing, `bitcoin.conf` reading, and `main`'s dispatch."""

import re
import runpy
from pathlib import Path
from typing import Any

import pytest

from btclib_node import Node, cli
from btclib_node.chains import Main, RegTest
from btclib_node.config import DEFAULT_MAX_PEER_CONNECTIONS, Config
from btclib_node.constants import MIN_PRUNE_TARGET_MIB
from btclib_node.rpc.auth import COOKIE_FILE, RpcAuthEntry, password_hmac
from tests import RPCAUTH, cookie_path, get_random_port, wait_until_listening


def test_parse_conf_text_reads_a_key_value_pair_in_the_default_section() -> None:
    """A bare `key=value` line lands in the `""` (default) section."""
    assert cli._parse_conf_text("port=9000\n", "conf") == {"": {"port": ["9000"]}}


def test_parse_conf_text_reads_a_section() -> None:
    """A `[section]` line switches which section later lines belong to."""
    tree = cli._parse_conf_text("[regtest]\nport=9000\n", "conf")
    assert tree == {"regtest": {"port": ["9000"]}}


def test_parse_conf_text_reads_a_section_prefix_in_the_key() -> None:
    """`regtest.port=` in the default section is `port=` in `[regtest]`."""
    tree = cli._parse_conf_text("regtest.port=9000\n", "conf")
    assert tree == {"regtest": {"port": ["9000"]}}


def test_parse_conf_text_strips_a_trailing_comment() -> None:
    """`#` starts a comment that runs to the end of the line."""
    tree = cli._parse_conf_text("port=9000 # the p2p port\n", "conf")
    assert tree == {"": {"port": ["9000"]}}


def test_parse_conf_text_skips_blank_and_comment_only_lines() -> None:
    """A blank line and a comment-only line contribute nothing."""
    tree = cli._parse_conf_text("\n# a comment\n   \nport=9000\n", "conf")
    assert tree == {"": {"port": ["9000"]}}


def test_parse_conf_text_collects_repeated_keys_in_order() -> None:
    """Every occurrence of one key is kept, in the order it was read."""
    tree = cli._parse_conf_text("addnode=1.2.3.4\naddnode=5.6.7.8\n", "conf")
    assert tree[""]["addnode"] == ["1.2.3.4", "5.6.7.8"]


@pytest.mark.parametrize(
    ("text", "value"),
    [("nolisten=1\n", False), ("nolisten=0\n", True), ("nolisten=\n", False)],
)
def test_parse_conf_text_reads_a_no_prefix_as_a_negation(
    text: str, *, value: bool
) -> None:
    """`no<key>` is `<key>` negated, `False`; a double negative is `True`."""
    assert cli._parse_conf_text(text, "conf") == {"": {"listen": [value]}}


def test_parse_conf_text_rejects_a_leading_dash() -> None:
    """A line starting with `-` is refused: no leading `-` in a file."""
    with pytest.raises(ValueError, match="leading -"):
        cli._parse_conf_text("-port=9000\n", "conf")


def test_parse_conf_text_rejects_a_line_with_no_equals_sign() -> None:
    """A line matching neither `[section]` nor `key=value` is refused."""
    with pytest.raises(ValueError, match=r"not a key=value line: 'garbage'$"):
        cli._parse_conf_text("garbage\n", "conf")


def test_parse_conf_text_suggests_a_negation_for_a_bare_no_line() -> None:
    """A bare `nolisten` line gets `GetConfigOptions`'s own hint."""
    with pytest.raises(ValueError, match=r"use nolisten=1 instead$"):
        cli._parse_conf_text("nolisten\n", "conf")


@pytest.mark.parametrize("text", ["conf=other.conf\n", "noconf=1\n"])
def test_parse_conf_text_rejects_conf_inside_a_file(text: str) -> None:
    """`conf=` cannot be set in a configuration file, negated or not."""
    with pytest.raises(ValueError, match="conf cannot be set"):
        cli._parse_conf_text(text, "conf")


def test_parse_conf_text_refuses_a_negated_datadir() -> None:
    """`nodatadir=1` is Core's forbidden negation, in a file too."""
    with pytest.raises(ValueError, match=r"^conf:1: Negating of -datadir is "):
        cli._parse_conf_text("nodatadir=1\n", "conf")


def test_parse_conf_text_warns_about_an_unknown_key_with_its_section(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown key is warned about, as written, and dropped."""
    assert cli._parse_conf_text("[regtest]\nwalletnotify=x\n", "conf") == {}
    assert capsys.readouterr().err == (
        "warning: ignoring unknown configuration value regtest.walletnotify\n"
    )


def test_parse_conf_text_warns_specifically_about_datadir(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`datadir=` gets its own message, not the generic unrecognised one.

    `datadir` is a real, documented option -- unlike `walletnotify`
    above, which this reader genuinely does not know -- so the message
    it gets says why it is never read from a file rather than implying
    it is a typo.
    """
    assert cli._parse_conf_text("datadir=/x\n", "conf") == {}
    err = capsys.readouterr().err
    assert "cannot be set in a configuration file" in err
    assert "unknown configuration value" not in err


def test_read_conf_file_missing_and_not_required_is_empty(tmp_path: Path) -> None:
    """A missing default-named file is not an error: an empty tree."""
    assert cli._read_conf_file(tmp_path / "bitcoin.conf", required=False) == {}


def test_read_conf_file_missing_and_required_raises(tmp_path: Path) -> None:
    """A missing file explicitly named by `-conf` is fatal."""
    with pytest.raises(ValueError, match="could not be opened"):
        cli._read_conf_file(tmp_path / "nope.conf", required=True)


def test_read_conf_file_a_directory_raises(tmp_path: Path) -> None:
    """`-conf` naming a directory is refused rather than read."""
    with pytest.raises(ValueError, match="is a directory"):
        cli._read_conf_file(tmp_path, required=False)


def test_read_conf_file_parses_an_existing_file(tmp_path: Path) -> None:
    """An existing file is read and parsed."""
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("port=9000\n", encoding="utf-8")
    assert cli._read_conf_file(conf, required=False) == {"": {"port": ["9000"]}}


def _load(conf: Path, *, use_includes: bool = True) -> cli._RoConfig:
    """Call `_load_conf_tree` on `conf`, not explicit, beside `conf` itself."""
    return cli._load_conf_tree(
        conf, conf_explicit=False, base_dir=conf.parent, use_includes=use_includes
    )


def test_load_conf_tree_with_no_includeconf_is_the_root_alone(tmp_path: Path) -> None:
    """No `includeconf`: the tree is exactly the root file's own."""
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("port=9000\n", encoding="utf-8")
    assert _load(conf) == {"": {"port": ["9000"]}}


def test_load_conf_tree_follows_a_relative_includeconf(tmp_path: Path) -> None:
    """`includeconf=<file>` is resolved against `base_dir` when relative."""
    (tmp_path / "secrets.conf").write_text("rpcport=9001\n", encoding="utf-8")
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("includeconf=secrets.conf\nport=9000\n", encoding="utf-8")
    tree = _load(conf)
    assert tree[""]["port"] == ["9000"]
    assert tree[""]["rpcport"] == ["9001"]


def test_load_conf_tree_follows_an_absolute_includeconf(tmp_path: Path) -> None:
    """An absolute `includeconf=<file>` is read as-is, not under `base_dir`."""
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    (other_dir / "secrets.conf").write_text("rpcport=9001\n", encoding="utf-8")
    conf = tmp_path / "bitcoin.conf"
    conf.write_text(f"includeconf={other_dir / 'secrets.conf'}\n", encoding="utf-8")
    assert _load(conf)[""]["rpcport"] == ["9001"]


def test_load_conf_tree_merges_an_included_files_own_section(tmp_path: Path) -> None:
    """A section inside an included file lands in the merged tree too."""
    (tmp_path / "secrets.conf").write_text("[regtest]\nport=9000\n", encoding="utf-8")
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("includeconf=secrets.conf\n", encoding="utf-8")
    assert _load(conf)["regtest"]["port"] == ["9000"]


def test_load_conf_tree_warns_about_and_ignores_a_nested_includeconf(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An `includeconf` inside an included file is warned about, not read."""
    (tmp_path / "third.conf").write_text("rpcport=9002\n", encoding="utf-8")
    (tmp_path / "secrets.conf").write_text(
        "includeconf=third.conf\nrpcport=9001\n", encoding="utf-8"
    )
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("includeconf=secrets.conf\n", encoding="utf-8")
    tree = _load(conf)
    assert tree[""]["rpcport"] == ["9001"]
    assert tree[""]["includeconf"] == ["secrets.conf"]
    assert capsys.readouterr().err == (
        "warning: -includeconf cannot be used from included files; "
        "ignoring -includeconf=third.conf\n"
    )


@pytest.mark.parametrize(
    ("text", "use_includes"),
    [
        ("includeconf=secrets.conf\nnoincludeconf=1\n", True),
        ("includeconf=secrets.conf\n", False),
    ],
    ids=["negated in the file", "-noincludeconf"],
)
def test_load_conf_tree_reads_no_include_a_negation_discards(
    tmp_path: Path, text: str, *, use_includes: bool
) -> None:
    """A negated `includeconf`, or `-noincludeconf`, reads no included file."""
    (tmp_path / "secrets.conf").write_text("rpcport=9001\n", encoding="utf-8")
    conf = tmp_path / "bitcoin.conf"
    conf.write_text(text, encoding="utf-8")
    assert "rpcport" not in _load(conf, use_includes=use_includes)[""]


def test_load_conf_tree_a_missing_included_file_is_fatal(tmp_path: Path) -> None:
    """An `includeconf` naming a file that does not exist is refused."""
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("includeconf=missing.conf\n", encoding="utf-8")
    with pytest.raises(ValueError, match="could not be opened"):
        _load(conf)


@pytest.mark.parametrize("value", ["false", "no", "yes", "00", "+-1"])
def test_build_config_a_files_chain_selector_is_read_as_interpret_bool(
    tmp_path: Path, value: str
) -> None:
    """`testnet=<value>` is false where `InterpretBool` reads it as false.

    `bitcoind` v31.1.0, run as `-regtest -version` beside a `bitcoin.conf`
    holding each of these, printed its version rather than refusing the
    combination of two chains.
    """
    (tmp_path / "bitcoin.conf").write_text(f"testnet={value}\n", encoding="utf-8")
    assert cli.build_config([f"-datadir={tmp_path}"]).chain.name == "mainnet"


@pytest.mark.parametrize(
    ("argv", "text"),
    [
        (["-listen=false"], ""),
        (["-listen=no"], ""),
        ([], "listen=false\n"),
        ([], "listen=00\n"),
    ],
    ids=["cli false", "cli no", "file false", "file 00"],
)
def test_build_config_listen_is_read_as_interpret_bool(
    tmp_path: Path, argv: list[str], text: str
) -> None:
    """`-listen`, on the command line or in the file, is `InterpretBool`."""
    (tmp_path / "bitcoin.conf").write_text(text, encoding="utf-8")
    assert cli.build_config([f"-datadir={tmp_path}", *argv]).listen is False


def test_build_config_norpccookiefile_false_still_writes_a_cookie(
    tmp_path: Path,
) -> None:
    """`norpccookiefile=false` in the file does not negate the cookie."""
    (tmp_path / "bitcoin.conf").write_text("norpccookiefile=false\n", encoding="utf-8")
    assert cli.build_config([f"-datadir={tmp_path}"]).rpc_cookie_file is not None


def _build(tmp_path: Path, *argv: str, conf: str = "") -> Config:
    """Build a `Config` from `argv`, `conf` in `tmp_path`'s `bitcoin.conf`."""
    (tmp_path / "bitcoin.conf").write_text(conf, encoding="utf-8")
    return cli.build_config([f"-datadir={tmp_path}", *argv])


@pytest.mark.parametrize(
    ("key", "info"),
    [
        ("port", ("port", "", False)),
        ("noport", ("port", "", True)),
        ("regtest.noport", ("port", "regtest", True)),
        ("a.b.c", ("b.c", "a", False)),
    ],
)
def test_interpret_key_splits_the_section_then_the_negation(
    key: str, info: tuple[str, str, bool]
) -> None:
    """`InterpretKey`: the section up to the first `.`, then a `no` prefix."""
    assert cli._interpret_key(key) == cli._KeyInfo(*info)


@pytest.mark.parametrize(
    ("argv", "options", "token"),
    [
        (["-listen=1", "--port=2"], {"listen": ["1"], "port": ["2"]}, None),
        (["-listen"], {"listen": [""]}, None),
        (["-nolisten", "-listen=0"], {"listen": [False, "0"]}, None),
        (["-listen", "foo", "-port=2"], {"listen": [""]}, "foo"),
        (["-listen", "-", "-port=2", "bar"], {"listen": [""]}, "bar"),
        (["-noincludeconf"], {"includeconf": [False]}, None),
    ],
    ids=["=value", "elided", "negated", "a token", "a lone -", "noincludeconf"],
)
def test_parse_parameters_takes_a_value_only_after_the_equals_sign(
    argv: list[str], options: dict[str, list[object]], token: str | None
) -> None:
    """`ParseParameters`: after `=` only; the first non-option ends them."""
    assert cli._parse_parameters(argv) == (options, token)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["-notaflag=1", "foo"], "Invalid parameter -notaflag=1"),
        (["--notaflag"], "Invalid parameter --notaflag"),
        (["-regtest.port=1"], "Invalid parameter -regtest.port=1"),
        (["-nodatadir"], "Negating of -datadir is meaningless and therefore forbidden"),
        (
            ["-includeconf=x"],
            '-includeconf cannot be used from commandline; -includeconf="x"',
        ),
        (
            ["-includeconf=\u00e9"],
            '-includeconf cannot be used from commandline; -includeconf="\u00e9"',
        ),
        (
            ["-noincludeconf=0"],
            "-includeconf cannot be used from commandline; -includeconf=true",
        ),
    ],
    ids=[
        "unknown option",
        "double dash",
        "a section",
        "nodatadir",
        "includeconf",
        "includeconf, not ASCII",
        "a double negative",
    ],
)
def test_parse_parameters_refuses_as_core_does(argv: list[str], message: str) -> None:
    """Each message as `bitcoind` v31.1.0 printed it for the same argument."""
    full = f"Error parsing command line arguments: {message}"
    with pytest.raises(ValueError, match=f"^{re.escape(full)}$"):
        cli._parse_parameters(argv)


@pytest.mark.parametrize(
    "argv",
    [["foo", "-notaflag"], ["-conf", "other.conf"], ["-listen", "0"]],
    ids=["token first", "a value after -conf", "a value after -listen"],
)
def test_build_config_refuses_an_argument_that_is_not_an_option(
    tmp_path: Path, argv: list[str]
) -> None:
    """ISS 1136: `ParseArgs`'s own "unexpected token", no parsing prefix."""
    token = next(arg for arg in argv if not arg.startswith("-"))
    message = (
        f"Command line contains unexpected token '{token}', "
        "see btclib-node -h for a list of options."
    )
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        _build(tmp_path, *argv)


def test_build_config_reads_a_double_dash_option(tmp_path: Path) -> None:
    """`--name=value` is `-name=value`."""
    assert _build(tmp_path, "--maxconnections=7").max_connections == 7


def test_build_config_warns_about_a_double_negative(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-nolisten=0` is `-listen=1`, warned about as Core warns about it."""
    assert _build(tmp_path, "-connect=0", "-nolisten=0").listen is True
    assert capsys.readouterr().err == (
        "warning: parsed potentially confusing double-negative -listen=0\n"
    )


def test_build_config_echoes_a_double_negative_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The warning echoes the value of an option that is not `SENSITIVE`."""
    assert _build(tmp_path, "-connect=0", "-nolisten=garbage").listen is True
    assert capsys.readouterr().err == (
        "warning: parsed potentially confusing double-negative -listen=garbage\n"
    )


@pytest.mark.parametrize("name", ["rpcauth", "rpcpassword", "rpcuser"])
def test_interpret_value_masks_a_sensitive_double_negative(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `SENSITIVE` option's value is `****` in the warning, unlike Core."""
    info = cli._interpret_key(f"no{name}")
    assert cli._interpret_value(info, "hunter2", cli._OPTIONS[name]) is True
    assert capsys.readouterr().err == (
        f"warning: parsed potentially confusing double-negative -{name}=****\n"
    )


@pytest.mark.parametrize(
    ("argv", "conf"),
    [(["-norpcpassword=hunter2"], ""), ([], "norpcpassword=hunter2\n")],
)
def test_build_config_masks_a_double_negative_password(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    conf: str,
) -> None:
    """`-norpcpassword=hunter2` never writes `hunter2`, on either path."""
    _build(tmp_path, "-connect=0", *argv, conf=conf)
    err = capsys.readouterr().err
    assert "hunter2" not in err
    assert "-rpcpassword=****" in err


@pytest.mark.parametrize(
    ("argv", "conf", "chain_name"),
    [
        (["-testnet=0"], "", "mainnet"),
        (["-regtest=0"], "regtest=1\n", "mainnet"),
        (["-noregtest"], "regtest=1\n", "regtest"),
        (["-regtest=1"], "", "regtest"),
        ([], "notestnet=0\n", "testnet"),
        ([], "notestnet=1\n", "mainnet"),
        ([], "testnet=1\nnotestnet=1\n", "mainnet"),
        (["-chain=test"], "chain=regtest\n", "testnet"),
        ([], "chain=regtest\nchain=test\n", "regtest"),
    ],
    ids=[
        "-testnet=0",
        "-regtest=0 over the file",
        "-noregtest skipped",
        "-regtest=1",
        "notestnet=0",
        "notestnet=1",
        "negated after set",
        "-chain over the file",
        "the file's first chain",
    ],
)
def test_build_config_reads_a_chain_selector_as_get_chain_arg(
    tmp_path: Path, argv: list[str], conf: str, chain_name: str
) -> None:
    """ISS 1124 and ISS 1137: a value, a negation, and Core's precedence.

    `-noregtest` on the command line is skipped, Core's own "weird
    behavior preserved" (`GetSetting`, `src/common/settings.cpp`), so
    the file's `regtest=1` still selects regtest; `bitcoind` v31.1.0 read
    `notestnet=0` as selecting testnet.
    """
    assert _build(tmp_path, *argv, conf=conf).chain.name == chain_name


@pytest.mark.parametrize(
    ("argv", "conf", "listen"),
    [
        (["-nolisten"], "", False),
        (["-nolisten=1"], "", False),
        (["-listen=0", "-listen"], "", True),
        ([], "nolisten=1\n", False),
        ([], "listen=1\nnolisten=1\n", False),
        ([], "[main]\nlisten=0\n", False),
        (["-listen=1"], "nolisten=1\n", True),
    ],
    ids=[
        "-nolisten",
        "-nolisten=1",
        "the last on the command line",
        "nolisten=1",
        "a negation after a value",
        "the chain's own section",
        "the command line over the file",
    ],
)
def test_build_config_reads_listen_and_its_negation(
    tmp_path: Path, argv: list[str], conf: str, *, listen: bool
) -> None:
    """ISS 1131 and ISS 1137: `-nolisten=<v>` and `nolisten=1` in the file."""
    assert _build(tmp_path, *argv, conf=conf).listen is listen


def test_build_config_reads_the_first_value_in_a_file(tmp_path: Path) -> None:
    """Core's reversed precedence within a file: the first value wins."""
    assert (
        _build(tmp_path, conf="maxconnections=7\nmaxconnections=8\n").max_connections
        == 7
    )


def test_build_config_a_negated_int_is_zero(tmp_path: Path) -> None:
    """`-nomaxconnections` is `0`, which turns the listener off with it."""
    config = _build(tmp_path, "-nomaxconnections")
    assert config.max_connections == 0
    assert config.listen is False


def test_build_config_a_double_negative_int_is_one(tmp_path: Path) -> None:
    """`-nomaxconnections=0` is `1`."""
    assert _build(tmp_path, "-nomaxconnections=0").max_connections == 1


@pytest.mark.parametrize(
    ("argv", "conf", "given", "listen"),
    [
        (["-noconnect"], "", True, False),
        (["-noconnect"], "connect=10.0.0.1\n", True, False),
        (["-noconnect", "-connect=10.0.0.2"], "connect=10.0.0.1\n", True, False),
        ([], "regtest=1\nconnect=10.0.0.1\n", False, True),
        ([], "regtest=1\n[regtest]\nconnect=10.0.0.1\n", True, False),
    ],
    ids=[
        "-noconnect",
        "-noconnect over the file",
        "a value after the negation",
        "network-only, the default section off main",
        "network-only, the chain's own section",
    ],
)
def test_build_config_reads_connect_as_get_settings_list(
    tmp_path: Path, argv: list[str], conf: str, *, given: bool, listen: bool
) -> None:
    """`-noconnect` is Core's `-connect=0`; a later value brings the file back.

    `GetSettingsList` (`src/common/settings.cpp`): a command line negated
    and then given a value keeps the file's own values, Core's "zombie"
    values, after its own.
    """
    config = _build(tmp_path, *argv, conf=conf)
    assert config.connect_given is given
    assert config.listen is listen
    if argv[1:]:
        assert config.connect == (
            ("10.0.0.2", Main().port),
            ("10.0.0.1", Main().port),
        )


@pytest.mark.parametrize(
    ("argv", "conf", "message"),
    [
        (["-noport"], "", "Invalid port specified in -port: '0'"),
        (["-port"], "", "Invalid port specified in -port: ''"),
        (["-port=+80"], "", "Invalid port specified in -port: '+80'"),
        (["-rpcport=65536"], "", "Invalid port specified in -rpcport: '65536'"),
        ([], "regtest=1\nnoport=1\n", "Invalid port specified in -port: '0'"),
    ],
    ids=["-noport", "elided", "a sign", "too large", "negated in the default section"],
)
def test_build_config_refuses_a_port_as_check_host_port_options(
    tmp_path: Path, argv: list[str], conf: str, message: str
) -> None:
    """Each message as `bitcoind` v31.1.0 printed it for the same argument.

    A negation in the default section reaches a network-only option off
    `main` too, `GetSetting`'s own exception to skipping that section.
    """
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        _build(tmp_path, *argv, conf=conf)


@pytest.mark.parametrize(
    "argv",
    [["-port=0", "-debug=bogus"], ["-prune=5", "-debug=bogus"]],
    ids=["a port", "prune"],
)
def test_build_config_refuses_a_logging_category_first(
    tmp_path: Path, argv: list[str]
) -> None:
    """`-debug` is checked ahead of `-prune` and the ports, as in Core.

    `bitcoind` v31.1.0 named the category for each of these.
    """
    with pytest.raises(ValueError, match=r"^Unsupported logging category"):
        _build(tmp_path, *argv)


def test_build_config_reads_a_port_with_leading_zeros(tmp_path: Path) -> None:
    """`-port=0080` is `80`: `bitcoind` v31.1.0 started on it."""
    assert _build(tmp_path, "-port=0080").p2p_port == 80


@pytest.mark.parametrize(
    ("argv", "conf", "debug"),
    [
        ([], "", False),
        (["-debug"], "", True),
        (["-debug=net"], "", True),
        (["-debug=all"], "", True),
        (["-debug=none"], "", False),
        (["-debug=0"], "", False),
        (["-debug=bogus", "-debug=none"], "", False),
        (["-debug=none", "-debug=net"], "", True),
        (["-nodebug"], "debug=1\n", False),
        (["-nodebug=0"], "", True),
        ([], "debug=0\n", False),
        ([], "debug=validation\n", True),
        ([], "nodebug=1\n", False),
    ],
)
def test_build_config_reads_debug_as_logging_categories(
    tmp_path: Path, argv: list[str], conf: str, *, debug: bool
) -> None:
    """ISS 1123: Core's category names, any on, `0`/`none` discarding."""
    assert _build(tmp_path, *argv, conf=conf).debug is debug


@pytest.mark.parametrize(
    ("argv", "conf", "category"),
    [
        (["-debug=bogus"], "", "bogus"),
        (["-debug=none", "-debug=bogus"], "", "bogus"),
        ([], "debug=false\n", "false"),
        (["-nodebug=0", "-debug=zz"], "", "zz"),
    ],
)
def test_build_config_refuses_an_unknown_logging_category(
    tmp_path: Path, argv: list[str], conf: str, category: str
) -> None:
    """`SetLoggingCategories`' own message, as `bitcoind` v31.1.0 printed it."""
    message = f"Unsupported logging category -debug={category}."
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        _build(tmp_path, *argv, conf=conf)


@pytest.mark.parametrize(
    ("argv", "conf"),
    [(["-server=0"], ""), (["-noserver"], ""), ([], "server=0\n")],
)
def test_build_config_server_off_asks_for_no_rpc_listener(
    tmp_path: Path, argv: list[str], conf: str
) -> None:
    """ISS 1112: `-server=0`, `-noserver` or `server=0` is `allow_rpc=False`."""
    assert _build(tmp_path, *argv, conf=conf).rpc_port is None


def test_build_config_server_is_on_by_default(tmp_path: Path) -> None:
    """`bitcoind`'s soft-set: `-server` is on unless something turns it off."""
    assert _build(tmp_path).rpc_port == Main().rpc_port
    assert _build(tmp_path, "-server").rpc_port == Main().rpc_port


def test_a_node_with_server_off_writes_no_cookie(tmp_path: Path) -> None:
    """ISS 1112: `-server=0` runs with no RPC listener and no `.cookie`."""
    config = cli.build_config(
        [
            "-regtest",
            f"-datadir={tmp_path}",
            "-server=0",
            f"-port={get_random_port()}",
            "-connect=0",
            "-listen=1",
        ]
    )
    node = Node(config=config)
    node.start()
    try:
        wait_until_listening(node.p2p_manager)
        assert not node.rpc_manager.listening.is_set()
        # before `stop`, which deletes a cookie that was written
        assert not cookie_path(node.data_dir).exists()
    finally:
        node.stop()


@pytest.mark.parametrize(
    "argument", ["-rpcauth=bad", "-rpccookieperms=bogus"], ids=["rpcauth", "perms"]
)
def test_build_config_server_off_reads_no_rpc_option(
    tmp_path: Path, argument: str
) -> None:
    """A malformed RPC option refuses nothing without `-server`, as in Core.

    `bitcoind` v31.1.0 started with `-noserver` and each of these, which
    it refuses with the server on.
    """
    config = _build(tmp_path, "-server=0", argument)
    assert config.rpc_auth == ()
    assert config.rpc_cookie_perms is None
    with pytest.raises(ValueError, match="rpc"):
        _build(tmp_path, argument)


def test_build_config_noblocksdir_is_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GetBlocksDirPath` reads a negated `-blocksdir` as an empty path."""
    monkeypatch.chdir(tmp_path)
    config = _build(tmp_path, "-regtest", "-noblocksdir")
    assert config.blocks_dir == Path.cwd() / "regtest"


def test_build_config_noconf_reads_no_file(tmp_path: Path) -> None:
    """`-noconf`: the data directory's own `bitcoin.conf` is not read."""
    assert _build(tmp_path, "-noconf", conf="regtest=1\n").chain.name == "mainnet"


def test_build_config_noincludeconf_reads_no_included_file(tmp_path: Path) -> None:
    """`-noincludeconf`: the file's own `includeconf=` is not followed."""
    (tmp_path / "other.conf").write_text("regtest=1\n", encoding="utf-8")
    config = _build(tmp_path, "-noincludeconf", conf="includeconf=other.conf\n")
    assert config.chain.name == "mainnet"


def test_build_config_reads_prune_from_the_file(tmp_path: Path) -> None:
    """`prune=` in `bitcoin.conf` reaches `Config`, as the option does."""
    config = _build(tmp_path, conf=f"prune={MIN_PRUNE_TARGET_MIB}\n")
    assert config.pruned is True
    assert config.prune_target_mib == MIN_PRUNE_TARGET_MIB


def test_build_config_refuses_a_non_integer(tmp_path: Path) -> None:
    """A value that is not an integer is refused where an integer is read."""
    with pytest.raises(ValueError, match=r"^prune='x' is not an integer$"):
        _build(tmp_path, "-prune=x")


@pytest.mark.parametrize("flag", ["-h", "-?", "-help", "-h=0"])
def test_build_config_help_prints_the_options_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    """`-h`, `-?` and `-help` print the help and exit `0`, as `bitcoind`."""
    with pytest.raises(SystemExit) as excinfo:
        _build(tmp_path, flag)
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("Run a bitcoin full node over btclib.\n\nUsage: btclib-node")
    assert "\nRPC server options:\n\n  -rpcauth=<userpw>\n       Username" in out
    assert "  -server\n" in out
    assert "  -regtest\n" not in out


def test_build_config_help_debug_shows_a_debug_only_option(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-regtest` is `DEBUG_ONLY` in Core: listed under `-help-debug` alone."""
    with pytest.raises(SystemExit):
        _build(tmp_path, "-help-debug")
    assert "  -regtest\n" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "chain_name"),
    [
        ([], "mainnet"),
        (["-testnet"], "testnet"),
        (["-signet"], "signet"),
        (["-regtest"], "regtest"),
        (["-chain=main"], "mainnet"),
        (["-chain=test"], "testnet"),
        (["-chain=signet"], "signet"),
        (["-chain=regtest"], "regtest"),
    ],
)
def test_build_config_selects_the_chain(
    tmp_path: Path, argv: list[str], chain_name: str
) -> None:
    """A selector, or `-chain=<alias>` in Core's own external vocabulary."""
    assert _build(tmp_path, *argv).chain.name == chain_name


@pytest.mark.parametrize(
    ("argv", "conf"),
    [(["-testnet", "-signet"], ""), (["-testnet"], "signet=1\n")],
    ids=["two on the command line", "one each side"],
)
def test_build_config_refuses_two_chain_selectors(
    tmp_path: Path, argv: list[str], conf: str
) -> None:
    """More than one selector, counted over the command line and the file."""
    with pytest.raises(ValueError, match="use at most one"):
        _build(tmp_path, *argv, conf=conf)


@pytest.mark.parametrize(
    ("argv", "conf", "alias"),
    [([], "chain=bogus\n", "bogus"), (["-nochain"], "", "0")],
)
def test_build_config_refuses_an_unknown_chain(
    tmp_path: Path, argv: list[str], conf: str, alias: str
) -> None:
    """An alias outside Core's four, `-nochain`'s `0` among them."""
    with pytest.raises(ValueError, match=f"^unknown chain '{alias}'$"):
        _build(tmp_path, *argv, conf=conf)


def test_build_config_with_nothing_given_uses_every_default(tmp_path: Path) -> None:
    """No flags, no file: every `Config` field takes its own default."""
    config = cli.build_config([f"-datadir={tmp_path}"])
    assert config.chain.name == "mainnet"
    assert config.rpc_host == "127.0.0.1"
    assert config.pruned is False
    assert config.connect == ()
    assert config.addnode == ()
    assert config.listen is True
    assert config.max_connections == DEFAULT_MAX_PEER_CONNECTIONS
    assert config.rpc_auth == ()


def test_build_config_maxconnections_from_the_command_line(tmp_path: Path) -> None:
    """`-maxconnections=<n>` reaches `Config.max_connections`."""
    config = cli.build_config([f"-datadir={tmp_path}", "-maxconnections=7"])
    assert config.max_connections == 7


def test_build_config_maxconnections_from_the_file_on_any_chain(
    tmp_path: Path,
) -> None:
    """Read from the default section off `main` too: Core's `ALLOW_ANY`."""
    (tmp_path / "bitcoin.conf").write_text(
        "regtest=1\nmaxconnections=7\n", encoding="utf-8"
    )
    config = cli.build_config([f"-datadir={tmp_path}"])
    assert config.max_connections == 7


def test_build_config_rpcauth_from_the_command_line_and_the_file(
    tmp_path: Path,
) -> None:
    """Every `-rpcauth` is kept, the command line's first, as for `-connect`.

    Read from the default section off `main`: Core's `-rpcauth` is
    `ALLOW_ANY` and not network-only.
    """
    other = "other:" + RPCAUTH.partition(":")[2]
    (tmp_path / "bitcoin.conf").write_text(
        f"regtest=1\nrpcauth={other}\n", encoding="utf-8"
    )
    config = cli.build_config([f"-datadir={tmp_path}", f"-rpcauth={RPCAUTH}"])
    assert config.rpc_auth == (RpcAuthEntry.parse(RPCAUTH), RpcAuthEntry.parse(other))


def test_build_config_a_malformed_rpcauth_in_the_file_raises(tmp_path: Path) -> None:
    """A malformed `rpcauth=` stops the node starting, as it stops Core."""
    (tmp_path / "bitcoin.conf").write_text(
        "regtest=1\nrpcauth=pytest:no-dollar-sign\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Invalid -rpcauth argument"):
        cli.build_config([f"-datadir={tmp_path}"])


def test_build_config_datadir_a_file_raises(tmp_path: Path) -> None:
    """`-datadir` naming an existing file is refused, not a crash.

    Before btclib-org/btclib-node#693, `conf_path.read_text()` raised
    `NotADirectoryError` uncaught -- neither `_read_conf_file`'s
    `FileNotFoundError` handler nor its `is_dir()` datadir check
    (btclib-org/btclib-node#684) ever sees a datadir this shape, since
    the resulting `conf_path` is not itself a directory and does not
    read as merely missing either.
    """
    datadir = tmp_path / "not-a-dir"
    datadir.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="does not exist"):
        cli.build_config([f"-datadir={datadir}"])


def test_build_config_datadir_parent_is_a_file_raises(tmp_path: Path) -> None:
    """`-datadir` naming a path a file blocks higher up is refused too."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("not a directory", encoding="utf-8")
    datadir = blocker / "subdir"
    with pytest.raises(ValueError, match="does not exist"):
        cli.build_config([f"-datadir={datadir}"])


def test_build_config_datadir_missing_raises(tmp_path: Path) -> None:
    """An explicit `-datadir` that does not exist yet is refused too.

    Matches Core's own `CheckDataDirOption` exactly: `fs::is_directory`
    answers `False` for a missing path the same way it does for one a
    file blocks, and both are fatal there. The default (unset
    `-datadir`) path is not checked at all and keeps its own lazy
    creation -- `test_build_config_with_nothing_given_uses_every_default`
    above already exercises it by passing an existing `tmp_path`, not
    this path.
    """
    datadir = tmp_path / "not-yet-created"
    with pytest.raises(ValueError, match="does not exist"):
        cli.build_config([f"-datadir={datadir}"])


def test_build_config_reads_an_existing_bitcoin_conf(tmp_path: Path) -> None:
    """The default-named file under the data directory is read unasked.

    `port=` sits inside `[regtest]`, not the default section: `-port`
    is network-only, so a default-section value would not apply once
    `regtest=1` (also in the default section, which chain selectors
    always read from) selects a chain that is not `main`.
    """
    (tmp_path / "bitcoin.conf").write_text(
        "regtest=1\n[regtest]\nport=9123\n", encoding="utf-8"
    )
    config = cli.build_config([f"-datadir={tmp_path}"])
    assert config.chain.name == "regtest"
    assert config.p2p_port == 9123


def test_build_config_conf_explicit_relative_is_joined_to_datadir(
    tmp_path: Path,
) -> None:
    """A relative `-conf` is resolved against `-datadir`, not against `cwd`."""
    (tmp_path / "mine.conf").write_text("regtest=1\n", encoding="utf-8")
    config = cli.build_config([f"-datadir={tmp_path}", "-conf=mine.conf"])
    assert config.chain.name == "regtest"


def test_build_config_conf_explicit_and_missing_raises(tmp_path: Path) -> None:
    """`-conf` naming a file that is not there is fatal, not skipped."""
    with pytest.raises(ValueError, match="could not be opened"):
        cli.build_config([f"-datadir={tmp_path}", "-conf=nope.conf"])


def test_build_config_cli_port_overrides_the_file(tmp_path: Path) -> None:
    """`-port` on the command line wins over the file's own value."""
    (tmp_path / "bitcoin.conf").write_text("port=1111\n", encoding="utf-8")
    config = cli.build_config([f"-datadir={tmp_path}", "-port=2222"])
    assert config.p2p_port == 2222


def test_build_config_rpcbind_sets_the_host() -> None:
    """`-rpcbind=<addr>` sets `rpc_host`."""
    config = cli.build_config(["-regtest", "-rpcbind=0.0.0.0"])
    assert config.rpc_host == "0.0.0.0"  # noqa: S104


def test_build_config_rpcbind_port_overrides_rpcport() -> None:
    """`-rpcbind`'s own port, when given, wins over `-rpcport`."""
    config = cli.build_config(["-regtest", "-rpcbind=127.0.0.1:9998", "-rpcport=9999"])
    assert config.rpc_port == 9998


@pytest.mark.parametrize(
    ("argv", "conf", "host"),
    [
        (["-rpcbind=127.0.0.2"], "rpcbind=127.0.0.3\n", "127.0.0.2"),
        ([], "rpcbind=127.0.0.3\nrpcbind=127.0.0.4\n", "127.0.0.3"),
        (["-norpcbind"], "rpcbind=127.0.0.3\n", "127.0.0.1"),
        (["-rpcbind=127.0.0.2", "-rpcbind=127.0.0.5"], "", "127.0.0.5"),
    ],
    ids=[
        "the command line over the file",
        "the file's first",
        "negated",
        "the command line's last",
    ],
)
def test_build_config_rpcbind_is_read_as_one_value(
    tmp_path: Path, argv: list[str], conf: str, host: str
) -> None:
    """The one address bound: the command line's last, or the file's first."""
    assert _build(tmp_path, *argv, conf=conf).rpc_host == host


def test_build_config_rpcbind_without_a_port_leaves_rpcport_alone() -> None:
    """`-rpcbind` naming no port of its own does not touch `-rpcport`."""
    config = cli.build_config(["-regtest", "-rpcbind=127.0.0.1", "-rpcport=9999"])
    assert config.rpc_port == 9999


def test_build_config_prune_nonzero_reaches_config_pruned() -> None:
    """A nonzero `-prune` builds a `Config` with `pruned` set, any value."""
    assert cli.build_config(["-regtest", "-prune=550"]).pruned is True
    assert cli.build_config(["-regtest", "-prune=1"]).pruned is True


def test_build_config_prune_at_or_above_the_minimum_sets_the_mib_target() -> None:
    """`-prune=<n>` for `n >= MIN_PRUNE_TARGET_MIB` reaches `prune_target_mib`.

    Core's own automatic pruning: `<n>` itself is the MiB target, not
    collapsed to a fixed depth -- `node::ApplyArgsManOptions`
    (`node/blockmanager_args.cpp:27-35`, at bitcoin/bitcoin@ca7162cde5)
    stores `nPruneArg * 1_MiB` verbatim as `opts.prune_target` for every
    `<n>` this branch reaches.
    """
    assert (
        cli.build_config(
            ["-regtest", f"-prune={MIN_PRUNE_TARGET_MIB}"]
        ).prune_target_mib
        == MIN_PRUNE_TARGET_MIB
    )
    assert cli.build_config(["-regtest", "-prune=700"]).prune_target_mib == 700


def test_build_config_prune_one_is_manual_and_sets_no_mib_target() -> None:
    """`-prune=1` is Core's own manual pruning: no MiB target, RPC-only.

    `node::ApplyArgsManOptions` (`node/blockmanager_args.cpp:28-29`, at
    bitcoin/bitcoin@ca7162cde5): `nPruneArg == 1` is the one value
    `PRUNE_TARGET_MANUAL` rather than `nPruneArg * 1_MiB` reaches, and
    `main._prune_chain` reads `prune_target_mib is None` as exactly
    this -- nothing deleted automatically, only
    `rpc.callbacks.prune_blockchain`.
    """
    assert cli.build_config(["-regtest", "-prune=1"]).prune_target_mib is None
    assert cli.build_config(["-regtest", "-prune=1"]).pruned is True


def test_build_config_prune_between_two_and_the_minimum_refuses_to_start() -> None:
    """`-prune=<n>` for `2 <= n < MIN_PRUNE_TARGET_MIB` refuses, Core's wording.

    `node::ApplyArgsManOptions` (`node/blockmanager_args.cpp:31-33`, at
    bitcoin/bitcoin@ca7162cde5): too small a target to actually run a
    node on, and Core refuses to start rather than rounding it up to
    the floor or collapsing it to manual pruning.
    """
    for n in (2, 100, MIN_PRUNE_TARGET_MIB - 1):
        with pytest.raises(
            ValueError,
            match=f"Prune configured below the minimum of {MIN_PRUNE_TARGET_MIB} MiB",
        ):
            cli.build_config(["-regtest", f"-prune={n}"])


def test_build_config_prune_zero_leaves_pruned_false() -> None:
    """`-prune=0`, the default, builds an unpruned `Config`."""
    assert cli.build_config(["-regtest", "-prune=0"]).pruned is False
    assert cli.build_config(["-regtest"]).pruned is False


def test_build_config_prune_negative_refuses_to_start() -> None:
    """A negative `-prune` refuses to start, matching Core's own wording.

    `node::ApplyArgsManOptions` (`node/blockmanager_args.cpp:23-25`, at
    bitcoin/bitcoin@ca7162cde5): `if (nPruneArg < 0) return
    util::Error{_("Prune cannot be configured with a negative value.")};`
    """
    with pytest.raises(
        ValueError, match="Prune cannot be configured with a negative value"
    ):
        cli.build_config(["-regtest", "-prune=-1"])


def test_build_config_blocksdir_reaches_config(tmp_path: Path) -> None:
    """`-blocksdir` on the command line resolves through to `Config`."""
    config = cli.build_config(["-regtest", f"-blocksdir={tmp_path}"])
    assert config.blocks_dir == tmp_path.absolute() / "regtest"


def test_build_config_without_blocksdir_leaves_it_none(tmp_path: Path) -> None:
    """No `-blocksdir`: `Config`'s own default, `None`."""
    config = cli.build_config([f"-datadir={tmp_path}"])
    assert config.blocks_dir is None


def test_build_config_blocksdir_missing_raises(tmp_path: Path) -> None:
    """`-blocksdir` naming a directory that does not exist is fatal."""
    missing = tmp_path / "nope"
    with pytest.raises(ValueError, match="does not exist"):
        cli.build_config(["-regtest", f"-blocksdir={missing}"])


def test_build_config_reads_blocksdir_from_the_file(tmp_path: Path) -> None:
    """`blocksdir=` in `bitcoin.conf` resolves the same way the flag does."""
    blocks_dir = tmp_path / "elsewhere"
    blocks_dir.mkdir()
    (tmp_path / "bitcoin.conf").write_text(
        f"regtest=1\nblocksdir={blocks_dir}\n", encoding="utf-8"
    )
    config = cli.build_config([f"-datadir={tmp_path}"])
    assert config.blocks_dir == blocks_dir.absolute() / "regtest"


def test_build_config_connect_and_addnode_reach_config() -> None:
    """`-connect`/`-addnode` on the command line resolve through to `Config`."""
    config = cli.build_config(["-regtest", "-connect=10.0.0.1", "-addnode=10.0.0.2:2"])
    assert config.connect == (("10.0.0.1", RegTest().port),)
    assert config.addnode == (("10.0.0.2", 2),)


def test_build_config_connect_alone_defaults_listen_to_false() -> None:
    """`-connect` alone: not listening, `config.connect` still carries the peer.

    `Node.run`'s own dial loop reads `config.connect`/`config.addnode`
    unconditionally, regardless of `config.listen` -- this is the "still
    dials" half; `P2pManager`'s own bind gate (`p2p/manager.py`) is the
    "not listening" half, `manager_test.py`'s own concern.
    """
    config = cli.build_config(["-regtest", "-connect=10.0.0.1"])
    assert config.listen is False
    assert config.connect == (("10.0.0.1", RegTest().port),)


def test_build_config_connect_and_explicit_listen_enables_both() -> None:
    """`-connect` plus `-listen=1`: the explicit flag wins over the default."""
    config = cli.build_config(["-regtest", "-connect=10.0.0.1", "-listen=1"])
    assert config.listen is True
    assert config.connect == (("10.0.0.1", RegTest().port),)


def test_build_config_maxconnections_zero_defaults_listen_to_false(
    tmp_path: Path,
) -> None:
    """ISS 1066: `-maxconnections=0` alone turns the listener off."""
    config = cli.build_config([f"-datadir={tmp_path}", "-maxconnections=0"])
    assert config.listen is False
    assert config.max_connections == 0


def test_build_config_maxconnections_zero_and_explicit_listen_listens(
    tmp_path: Path,
) -> None:
    """`-maxconnections=0` plus `-listen=1`: the explicit flag wins."""
    config = cli.build_config(
        [f"-datadir={tmp_path}", "-maxconnections=0", "-listen=1"]
    )
    assert config.listen is True


def test_build_config_nolisten_forces_listen_false() -> None:
    """`-nolisten` reaches `Config` the same way `-listen=0` would."""
    config = cli.build_config(["-regtest", "-nolisten"])
    assert config.listen is False


def test_build_config_bare_listen_means_true() -> None:
    """`-listen` given without a value is Core's own bare boolean flag."""
    config = cli.build_config(["-regtest", "-connect=10.0.0.1", "-listen"])
    assert config.listen is True


def test_build_config_connect_zero_dials_nobody() -> None:
    """`-connect=0`: still not listening, but nothing is dialled."""
    config = cli.build_config(["-regtest", "-connect=0"])
    assert config.listen is False
    assert config.connect == ()
    assert config.connect_given is True


def test_main_builds_a_node_and_starts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main` builds a `Node` from the parsed config, starts it and waits."""
    built: list[Any] = []

    class FakeNode:
        init_error = None

        def __init__(self, config: Any) -> None:
            built.append(config)

        def start(self) -> None:
            built.append("started")

        def join(self) -> None:
            built.append("joined")

    handlers: list[Any] = []
    monkeypatch.setattr(cli, "Node", FakeNode)
    monkeypatch.setattr(cli, "install_signal_handlers", handlers.append)

    cli.main([f"-datadir={tmp_path}", "-regtest"])

    assert built[0].chain.name == "regtest"
    assert built[1:] == ["started", "joined"]
    assert len(handlers) == 1


def test_main_a_node_that_failed_to_start_exits_one_with_its_init_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`Node.init_error` is printed and the exit status is `1`, as Core's."""

    class FakeNode:
        init_error = "Unable to start HTTP server. See debug log for details."

        def __init__(self, config: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def join(self) -> None:
            pass

    monkeypatch.setattr(cli, "Node", FakeNode)
    monkeypatch.setattr(cli, "install_signal_handlers", lambda node: None)
    with pytest.raises(SystemExit) as excinfo:
        cli.main([f"-datadir={tmp_path}", "-regtest"])
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == f"Error: {FakeNode.init_error}\n"


@pytest.fixture
def no_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove `cli.Node`: an argument `main` does not refuse is a `NameError`.

    With `Node` in place such an argument starts a node that never stops.
    """
    monkeypatch.delattr(cli, "Node")


@pytest.mark.usefixtures("no_node")
def test_main_a_bad_argument_exits_one_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `build_config` failure prints to stderr and exits `1`."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main([f"-datadir={tmp_path}", "-conf=nope.conf"])
    assert excinfo.value.code == 1
    assert capsys.readouterr().err.startswith("Error: ")


@pytest.mark.parametrize(
    ("argument", "message"),
    [
        (
            "-notaflag",
            "Error parsing command line arguments: Invalid parameter -notaflag",
        ),
        ("-port=abc", "Invalid port specified in -port: 'abc'"),
    ],
)
@pytest.mark.usefixtures("no_node")
def test_main_a_refused_argument_exits_one_as_init_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argument: str, message: str
) -> None:
    """Core's `InitError`: one `Error:` line on stderr, exit `1`."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main([f"-datadir={tmp_path}", argument])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.err == f"Error: {message}\n"
    assert not captured.out


def test_dunder_main_calls_cli_main_under_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`python -m btclib_node` reaches `cli.main` through the guard."""
    calls: list[int] = []
    monkeypatch.setattr(cli, "main", lambda: calls.append(1))
    runpy.run_module("btclib_node.__main__", run_name="__main__")
    assert calls == [1]


def test_dunder_main_imported_plainly_does_not_call_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Imported under its own name, the guard does not fire.

    The positive control for the test above: proves the `if __name__ ==
    "__main__":` branch has a real false arm, rather than the module
    calling `main` unconditionally and the mock above happening to look
    like a guard.
    """
    calls: list[int] = []
    monkeypatch.setattr(cli, "main", lambda: calls.append(1))
    runpy.run_module("btclib_node.__main__", run_name="btclib_node.__main__")
    assert calls == []


def test_build_config_rpcuser_and_rpcpassword_from_the_file_on_any_chain(
    tmp_path: Path,
) -> None:
    """Core's `-rpcuser`/`-rpcpassword` are `ALLOW_ANY` and not network-only."""
    (tmp_path / "bitcoin.conf").write_text(
        "regtest=1\nrpcuser=alice\nrpcpassword=pw\n", encoding="utf-8"
    )
    entry = cli.build_config([f"-datadir={tmp_path}"]).rpc_password_entry
    assert entry is not None
    assert entry.user == b"alice"
    assert entry.hmac == password_hmac(entry.salt, b"pw")


def test_build_config_rpcpassword_on_the_command_line_beats_the_file(
    tmp_path: Path,
) -> None:
    """The command line over the file, as for every other scalar."""
    (tmp_path / "bitcoin.conf").write_text("rpcpassword=file\n", encoding="utf-8")
    argv = [f"-datadir={tmp_path}", "-rpcpassword=cli"]
    entry = cli.build_config(argv).rpc_password_entry
    assert entry is not None
    assert entry.user == b""
    assert entry.hmac == password_hmac(entry.salt, b"cli")


@pytest.mark.parametrize(
    "text",
    ["rpcpassword=ab#c\n", "rpcuser=u\nrpcpassword=abc # a comment\n"],
    ids=["inside the password", "a comment after it"],
)
def test_a_hash_on_an_rpcpassword_line_is_refused(tmp_path: Path, text: str) -> None:
    """Core's parse error: the `#` may be the password's or a comment's."""
    (tmp_path / "bitcoin.conf").write_text(text, encoding="utf-8")
    line = text.count("\n")
    err_msg = f":{line}: using # in rpcpassword can be ambiguous and should be avoided$"
    with pytest.raises(ValueError, match=err_msg):
        cli.build_config([f"-datadir={tmp_path}"])


def test_a_hash_on_another_line_is_a_comment(tmp_path: Path) -> None:
    """Only a key naming `rpcpassword` refuses a `#`, as `bitcoind` does."""
    (tmp_path / "bitcoin.conf").write_text(
        "rpcuser=u # a comment\nrpcpassword=abc\n", encoding="utf-8"
    )
    entry = cli.build_config([f"-datadir={tmp_path}"]).rpc_password_entry
    assert entry is not None
    assert entry.user == b"u"


def test_build_config_rpccookiefile_from_the_command_line(tmp_path: Path) -> None:
    """`-rpccookiefile=<loc>`, relative to the chain's data directory."""
    config = cli.build_config([f"-datadir={tmp_path}", "-rpccookiefile=c"])
    assert config.rpc_cookie_file == tmp_path / "mainnet" / "c"
    config = cli.build_config([f"-datadir={tmp_path}"])
    assert config.rpc_cookie_file == tmp_path / "mainnet" / COOKIE_FILE


@pytest.mark.parametrize(
    ("argv", "text", "written"),
    [
        (["-norpccookiefile"], "", False),
        ([], "norpccookiefile=1\n", False),
        ([], "rpccookiefile=c\nnorpccookiefile=1\n", False),
        ([], "norpccookiefile=0\n", True),
        (["-rpccookiefile=c"], "norpccookiefile=1\n", True),
        (["-rpccookiefile=c", "-norpccookiefile"], "", False),
        (["-norpccookiefile", "-rpccookiefile=c"], "", True),
    ],
    ids=[
        "the flag",
        "the file",
        "the file, over rpccookiefile=",
        "the file, =0",
        "the command line over the file",
        "the last flag, negated",
        "the last flag, a path",
    ],
)
def test_norpccookiefile_writes_no_cookie(
    tmp_path: Path, argv: list[str], text: str, *, written: bool
) -> None:
    """`-norpccookiefile`, `norpccookiefile=1` in the file."""
    (tmp_path / "bitcoin.conf").write_text(text, encoding="utf-8")
    config = cli.build_config([f"-datadir={tmp_path}", *argv])
    assert (config.rpc_cookie_file is not None) == written


def test_build_config_rpccookieperms_from_the_file(tmp_path: Path) -> None:
    """`rpccookieperms=` in the file, and a bad value refused."""
    (tmp_path / "bitcoin.conf").write_text("rpccookieperms=all\n", encoding="utf-8")
    assert cli.build_config([f"-datadir={tmp_path}"]).rpc_cookie_perms == 0o644
    with pytest.raises(ValueError, match=r"^Invalid -rpccookieperms=x;"):
        cli.build_config([f"-datadir={tmp_path}", "-rpccookieperms=x"])


def test_build_config_rpcwhitelist_from_the_command_line_and_the_file(
    tmp_path: Path,
) -> None:
    """Every `-rpcwhitelist` from both, which then intersect for one user."""
    (tmp_path / "bitcoin.conf").write_text(
        "regtest=1\nrpcwhitelist=alice:b,c\n", encoding="utf-8"
    )
    config = cli.build_config([f"-datadir={tmp_path}", "-rpcwhitelist=alice:a,b"])
    assert config.rpc_whitelist == {b"alice": frozenset({"b"})}
    assert config.rpc_whitelist_default


@pytest.mark.parametrize(
    ("argv", "text", "default"),
    [
        ([], "", False),
        (["-rpcwhitelistdefault"], "", True),
        (["-rpcwhitelistdefault=1"], "", True),
        (["-rpcwhitelistdefault=0", "-rpcwhitelist=a:b"], "", False),
        ([], "rpcwhitelistdefault=1\n", True),
        (["-rpcwhitelist=a:b"], "rpcwhitelistdefault=0\n", False),
        (["-rpcwhitelistdefault=1"], "rpcwhitelistdefault=0\n", True),
    ],
)
def test_build_config_rpcwhitelistdefault(
    tmp_path: Path, argv: list[str], text: str, *, default: bool
) -> None:
    """A flag with an optional value, the command line over the file."""
    (tmp_path / "bitcoin.conf").write_text(text, encoding="utf-8")
    config = cli.build_config([f"-datadir={tmp_path}", *argv])
    assert config.rpc_whitelist_default == default


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", True),
        ("1", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("00", False),
        ("+1", True),
        ("+-1", False),
        ("-1", True),
        ("1x", True),
        ("x1", False),
        ("-0", False),
        (" 2", True),
    ],
)
def test_rpcwhitelistdefault_is_read_as_core_s_interpret_bool(
    tmp_path: Path, value: str, *, expected: bool
) -> None:
    """Each value as `bitcoind` v31.1.0 read `-rpcwhitelistdefault=<value>`.

    Measured there with no `-rpcwhitelist`: a 403 for true, a 200 for false.
    """
    argv = [f"-datadir={tmp_path}", f"-rpcwhitelistdefault={value}"]
    assert cli.build_config(argv).rpc_whitelist_default == expected
