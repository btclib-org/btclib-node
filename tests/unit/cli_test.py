# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`cli.py`: argument parsing, `bitcoin.conf` reading, and `main`'s dispatch."""

import functools
import io
import os
import re
import runpy
import socket
import stat
import sys
import tempfile
from contextlib import redirect_stderr, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from btclib_node import Node, cli
from btclib_node.chains import Main, RegTest, SigNet, TestNet, TestNet4
from btclib_node.config import DEFAULT_MAX_PEER_CONNECTIONS, DEFAULT_MAX_TIP_AGE, Config
from btclib_node.constants import CLIENT_NAME, MIN_PRUNE_TARGET_MIB, default_data_dir
from btclib_node.rpc.auth import COOKIE_FILE, RpcAuthEntry, password_hmac, to_bytes
from btclib_node.settings_file import read_settings
from tests import (
    RPCAUTH,
    cookie_path,
    get_random_port,
    held_by_another_process,
    lock_from_another_process,
    wait_until_listening,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


@pytest.fixture(autouse=True)
def home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Make `~` an empty directory of the test's own.

    `-datadir` defaults to `~/.btclib`, where every start writes
    `settings.json` and reads `bitcoin.conf`.
    """
    path = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    return path


@pytest.fixture(autouse=True)
def caller_umask() -> Iterator[None]:
    """Give back, after each test, the umask `cli.main` makes owner-only.

    The umask is the process's, so a test calling `main` would otherwise
    leave it on every test after it in the same worker.
    """
    umask = os.umask(0o022)
    os.umask(umask)
    yield
    os.umask(umask)


def test_parse_conf_text_reads_a_key_value_pair_in_the_default_section() -> None:
    """A bare `key=value` line lands in the `""` (default) section."""
    assert cli._parse_conf_text("port=9000\n", warnings=[]) == {"": {"port": ["9000"]}}


def test_parse_conf_text_reads_a_section() -> None:
    """A `[section]` line switches which section later lines belong to."""
    tree = cli._parse_conf_text("[regtest]\nport=9000\n", warnings=[])
    assert tree == {"regtest": {"port": ["9000"]}}


def test_parse_conf_text_reads_a_section_prefix_in_the_key() -> None:
    """`regtest.port=` in the default section is `port=` in `[regtest]`."""
    tree = cli._parse_conf_text("regtest.port=9000\n", warnings=[])
    assert tree == {"regtest": {"port": ["9000"]}}


def test_parse_conf_text_strips_a_trailing_comment() -> None:
    """`#` starts a comment that runs to the end of the line."""
    tree = cli._parse_conf_text("port=9000 # the p2p port\n", warnings=[])
    assert tree == {"": {"port": ["9000"]}}


def test_parse_conf_text_skips_blank_and_comment_only_lines() -> None:
    """A blank line and a comment-only line contribute nothing."""
    tree = cli._parse_conf_text("\n# a comment\n   \nport=9000\n", warnings=[])
    assert tree == {"": {"port": ["9000"]}}


def test_parse_conf_text_collects_repeated_keys_in_order() -> None:
    """Every occurrence of one key is kept, in the order it was read."""
    tree = cli._parse_conf_text("addnode=1.2.3.4\naddnode=5.6.7.8\n", warnings=[])
    assert tree[""]["addnode"] == ["1.2.3.4", "5.6.7.8"]


@pytest.mark.parametrize(
    ("text", "value"),
    [("nolisten=1\n", False), ("nolisten=0\n", True), ("nolisten=\n", False)],
)
def test_parse_conf_text_reads_a_no_prefix_as_a_negation(
    text: str, *, value: bool
) -> None:
    """`no<key>` is `<key>` negated, `False`; a double negative is `True`."""
    assert cli._parse_conf_text(text, warnings=[]) == {"": {"listen": [value]}}


# `GetConfigOptions`, `IsConfSupported` and `InterpretValue`'s words
# (`src/common/config.cpp`, `src/common/args.cpp`,
# at bitcoin/bitcoin@9be056a8a7), each measured on `bitcoind` v31.1.0 with
# the line below `regtest=1`, which is what numbers it 2
@pytest.mark.parametrize(
    ("line", "refusal"),
    [
        pytest.param("foo", "parse error on line 2: foo", id="no equals sign"),
        pytest.param("  foo # c", "parse error on line 2: foo", id="trimmed"),
        pytest.param("[regtest", "parse error on line 2: [regtest", id="half section"),
        pytest.param(
            "nofoo",
            "parse error on line 2: nofoo, if you intended to specify a negated "
            "option, use nofoo=1 instead",
            id="bare negation",
        ),
        pytest.param(
            "no",
            "parse error on line 2: no, if you intended to specify a negated "
            "option, use no=1 instead",
            id="bare no",
        ),
        pytest.param(
            "  -foo = 1 # c",
            "parse error on line 2: -foo = 1, options in configuration file must "
            "be specified without leading -",
            id="leading dash",
        ),
        pytest.param(
            "rpcpassword=a#b",
            "parse error on line 2, using # in rpcpassword can be ambiguous and "
            "should be avoided",
            id="hash in rpcpassword",
        ),
        pytest.param(
            "conf=x.conf",
            "conf cannot be set in the configuration file; use includeconf= if "
            "you want to include additional config files",
            id="conf",
        ),
        pytest.param(
            "noconf=1",
            "conf cannot be set in the configuration file; use includeconf= if "
            "you want to include additional config files",
            id="negated conf",
        ),
        pytest.param(
            "nodatadir=1",
            "Negating of -datadir is meaningless and therefore forbidden",
            id="negated datadir",
        ),
    ],
)
def test_parse_conf_text_refuses_a_line_in_core_s_words(
    line: str, refusal: str
) -> None:
    """ISS 1267: Core's message, numbered as Core numbers it, naming no path."""
    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
        cli._parse_conf_text(f"regtest=1\n{line}\n", warnings=[])


@pytest.mark.parametrize(
    ("line", "refusal"),
    [
        pytest.param(
            "-rpcpassword=hunter2",
            "parse error on line 2: -rpcpassword, options in configuration file "
            "must be specified without leading -",
            id="leading dash",
        ),
        pytest.param(
            "rpcpasswordhunter2",
            "parse error on line 2: rpcpassword",
            id="no equals sign",
        ),
        pytest.param(
            "norpcpassword hunter2",
            "parse error on line 2: norpcpassword, if you intended to specify a "
            "negated option, use norpcpassword=1 instead",
            id="negated",
        ),
        pytest.param(
            "-norpcuser = hunter2",
            "parse error on line 2: -norpcuser, options in configuration file "
            "must be specified without leading -",
            id="negated user",
        ),
        pytest.param(
            "rpcauthhunter2",
            "parse error on line 2: rpcauth",
            id="rpcauth",
        ),
        pytest.param(
            "-main.rpcpassword=hunter2",
            "parse error on line 2: -main.rpcpassword, options in configuration "
            "file must be specified without leading -",
            id="section and dash",
        ),
        pytest.param(
            "regtest.rpcauthhunter2",
            "parse error on line 2: regtest.rpcauth",
            id="section",
        ),
        pytest.param(
            "test.norpcuser hunter2",
            "parse error on line 2: test.norpcuser",
            id="section and negated",
        ),
        pytest.param(
            "foo.rpcpassword hunter2",
            "parse error on line 2: foo.rpcpassword",
            id="unknown section",
        ),
        pytest.param(
            "-foo.rpcpassword=hunter2",
            "parse error on line 2: -foo.rpcpassword, options in configuration "
            "file must be specified without leading -",
            id="unknown section and dash",
        ),
        pytest.param(
            "testnet3.rpcauth hunter2",
            "parse error on line 2: testnet3.rpcauth",
            id="old section name",
        ),
        pytest.param(
            "mainnet.rpcpasswordhunter2",
            "parse error on line 2: mainnet.rpcpassword",
            id="made-up section",
        ),
        pytest.param(
            "- rpcpassword=hunter2",
            "parse error on line 2: - rpcpassword, options in configuration "
            "file must be specified without leading -",
            id="dash and space",
        ),
    ],
)
def test_parse_conf_text_leaves_a_sensitive_value_out_of_a_refusal(
    line: str, refusal: str
) -> None:
    """Core quotes the whole line; this tree quotes the option name alone."""
    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$") as caught:
        cli._parse_conf_text(f"regtest=1\n{line}\n", warnings=[])
    assert "hunter2" not in str(caught.value)


def test_parse_conf_text_ends_a_line_at_a_newline_alone() -> None:
    """ISS 1267: `std::getline`'s lines, so a form feed ends none.

    Measured on `bitcoind` v31.1.0: `foo`, a form feed and `bar=1` on
    line 2 are one line, and `bad` below it is refused as line 3.
    """
    with pytest.raises(ValueError, match=r"^parse error on line 3: bad$"):
        cli._parse_conf_text("regtest=1\nfoo\fbar=1\nbad\n", warnings=[])


@pytest.mark.parametrize(
    ("content", "refusal"),
    [
        (b"regtest=1\n# x\xe9y\nbad\n", b"parse error on line 3: bad"),
        (b"regtest=1\nx\xe9y\n", b"parse error on line 2: x\xe9y"),
    ],
    ids=["in a comment", "in the line refused"],
)
def test_read_conf_file_reads_a_byte_utf8_refuses(
    tmp_path: Path, content: bytes, refusal: bytes
) -> None:
    """ISS 1290: the file is read as `bitcoind` reads it, as bytes.

    `0xe9`, Latin-1's `e` acute, is no UTF-8. `bitcoind` v31.1.0 reads
    past it in a comment, and quotes it in the line it refuses; here it
    is the lone surrogate `surrogateescape` keeps it as, compared as the
    byte it stands for: a surrogate in a failure's text is one `xdist`
    cannot send back from its worker.
    """
    path = tmp_path / "bitcoin.conf"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="parse error") as raised:
        cli._read_conf_file(path, required=True, warnings=[])
    assert to_bytes(str(raised.value)) == refusal


@pytest.mark.usefixtures("no_node")
def test_main_writes_a_byte_utf8_refuses_as_that_byte(
    tmp_path: Path, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    """ISS 1290: stderr holds the byte the file held, as `bitcoind` writes it.

    Measured on `bitcoind` v31.1.0 over this file: `Error: Error reading
    configuration file: parse error on line 2: x<0xe9>y`, the prefix
    being `InitConfig`'s.
    """
    (tmp_path / "bitcoin.conf").write_bytes(b"regtest=1\nx\xe9y\n")
    with pytest.raises(SystemExit):
        cli.main([f"-datadir={tmp_path}"])
    assert capfdbinary.readouterr().err.endswith(b"parse error on line 2: x\xe9y\n")


@pytest.mark.usefixtures("no_node")
def test_main_writes_to_a_stderr_it_cannot_reconfigure(tmp_path: Path) -> None:
    """ISS 1290: a stream with no encoding of its own is written as it is.

    `io.StringIO`, which `redirect_stderr` puts in place, keeps text
    rather than bytes, so there is no error handler to set on it.
    """
    (tmp_path / "bitcoin.conf").write_text("regtest=1\nbad\n", encoding="utf-8")
    with redirect_stderr(io.StringIO()) as err, pytest.raises(SystemExit):
        cli.main([f"-datadir={tmp_path}"])
    assert err.getvalue().endswith("parse error on line 2: bad\n")


@pytest.mark.parametrize(
    ("content", "line"),
    [
        (b"regtest=1\rfoo\nbad\n", "line 2: bad"),
        (b"regtest=1\r\nfoo\r\n", "line 2: foo"),
        (b"regtest=1\n\rfoo\r\n", "line 2: foo"),
    ],
    ids=["lone CR", "CRLF", "CR opening a line"],
)
def test_read_conf_file_ends_a_line_at_a_newline_alone(
    tmp_path: Path, content: bytes, line: str
) -> None:
    """ISS 1267: a lone carriage return ends no line, as `bitcoind` reads it.

    Each measured on `bitcoind` v31.1.0, which names the same line.
    """
    path = tmp_path / "bitcoin.conf"
    path.write_bytes(content)
    with pytest.raises(ValueError, match=f"^parse error on {line}$"):
        cli._read_conf_file(path, required=True, warnings=[])


def test_parse_conf_text_warns_about_an_unknown_key_with_its_section(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ISS 1295: an unknown key is dropped, and warned about for the log alone.

    As written, section and all: `bitcoind` v31.1.0 logs `[y]`'s `bar=2`
    as "Ignoring unknown configuration value y.bar", and writes nothing
    on stderr.
    """
    warnings: list[str] = []
    assert cli._parse_conf_text("[regtest]\nwalletnotify=x\n", warnings=warnings) == {}
    assert warnings == ["Ignoring unknown configuration value regtest.walletnotify"]
    assert capsys.readouterr().err == ""


def test_parse_conf_text_warns_specifically_about_datadir(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`datadir=` gets its own message, not the generic unrecognised one.

    `datadir` is a real, documented option -- unlike `walletnotify`
    above, which this reader genuinely does not know -- so the message
    it gets says why it is never read from a file rather than implying
    it is a typo.
    """
    assert cli._parse_conf_text("datadir=/x\n", warnings=[]) == {}
    err = capsys.readouterr().err
    assert "cannot be set in a configuration file" in err
    assert "unknown configuration value" not in err


def test_read_conf_file_missing_and_not_required_is_empty(tmp_path: Path) -> None:
    """A missing default-named file is not an error: an empty tree."""
    assert (
        cli._read_conf_file(tmp_path / "bitcoin.conf", required=False, warnings=[])
        == {}
    )


def test_read_conf_file_missing_and_required_raises(tmp_path: Path) -> None:
    """A missing file explicitly named by `-conf` is fatal."""
    with pytest.raises(ValueError, match="could not be opened"):
        cli._read_conf_file(tmp_path / "nope.conf", required=True, warnings=[])


def test_read_conf_file_a_directory_raises(tmp_path: Path) -> None:
    """`-conf` naming a directory is refused rather than read."""
    with pytest.raises(ValueError, match="is a directory"):
        cli._read_conf_file(tmp_path, required=False, warnings=[])


def test_read_conf_file_parses_an_existing_file(tmp_path: Path) -> None:
    """An existing file is read and parsed."""
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("port=9000\n", encoding="utf-8")
    assert cli._read_conf_file(conf, required=False, warnings=[]) == {
        "": {"port": ["9000"]}
    }


def _load(conf: Path, *, use_includes: bool = True) -> cli._RoConfig:
    """Call `_load_conf_tree` on `conf`, not explicit, beside `conf` itself."""
    return cli._load_conf_tree(
        conf,
        conf_explicit=False,
        base_dir=conf.parent,
        use_includes=use_includes,
        warnings=[],
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
    # kept, as `ReadConfigStream` keeps it, and read by nothing
    assert tree[""]["includeconf"] == ["secrets.conf", "third.conf"]
    assert capsys.readouterr().err == (
        "warning: -includeconf cannot be used from included files; "
        "ignoring -includeconf=third.conf\n"
    )


_NESTED = (
    "warning: -includeconf cannot be used from included files; ignoring -includeconf="
)


def test_load_conf_tree_reads_the_chain_sections_includes_first(tmp_path: Path) -> None:
    """ISS 1302: the chain's own section's `includeconf`, then the default's.

    As `ReadConfigFiles`'s `add_includes(chain_id)` before
    `add_includes({})`: `bitcoind` v31.1.0 warns of `a.conf`'s section
    before `b.conf`'s with this root file.
    """
    (tmp_path / "a.conf").write_text("port=1\n", encoding="utf-8")
    (tmp_path / "b.conf").write_text("port=2\n", encoding="utf-8")
    conf = tmp_path / "bitcoin.conf"
    conf.write_text(
        "regtest=1\nincludeconf=b.conf\n[regtest]\nincludeconf=a.conf\n",
        encoding="utf-8",
    )
    assert _load(conf)[""]["port"] == ["1", "2"]


def test_load_conf_tree_reads_the_section_of_the_command_lines_chain(
    tmp_path: Path,
) -> None:
    """ISS 1302: the chain is the command line's and the root file's both."""
    (tmp_path / "a.conf").write_text("port=1\n", encoding="utf-8")
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("[regtest]\nincludeconf=a.conf\n", encoding="utf-8")
    assert "port" not in _load(conf).get("", {})
    command_line, _ = cli._parse_parameters(["-regtest"], [])
    tree = cli._load_conf_tree(
        conf,
        conf_explicit=False,
        base_dir=tmp_path,
        use_includes=True,
        command_line=command_line,
        warnings=[],
    )
    assert tree[""]["port"] == ["1"]


def test_load_conf_tree_warns_of_the_includes_of_either_section_it_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ISS 1302: the chain's section's first, and never another chain's.

    Measured on `bitcoind` v31.1.0 with these files: it warns of `y.conf`,
    then `x.conf`, and says nothing of `z.conf`.
    """
    (tmp_path / "inc.conf").write_text(
        "includeconf=x.conf\n[regtest]\nincludeconf=y.conf\n[main]\nincludeconf=z.conf\n",
        encoding="utf-8",
    )
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("regtest=1\n[regtest]\nincludeconf=inc.conf\n", encoding="utf-8")
    _load(conf)
    assert capsys.readouterr().err == f"{_NESTED}y.conf\n{_NESTED}x.conf\n"


def test_load_conf_tree_warns_of_the_includes_of_a_chain_an_include_chose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ISS 1302: `chain_id_final`'s every `includeconf`, the root file's too.

    Measured on `bitcoind` v31.1.0 with these files: `r.conf` is warned
    about and not read.
    """
    (tmp_path / "inc.conf").write_text("regtest=1\n", encoding="utf-8")
    (tmp_path / "r.conf").write_text("port=1\n", encoding="utf-8")
    conf = tmp_path / "bitcoin.conf"
    conf.write_text(
        "includeconf=inc.conf\n[regtest]\nincludeconf=r.conf\n", encoding="utf-8"
    )
    assert "port" not in _load(conf)[""]
    assert capsys.readouterr().err == f"{_NESTED}r.conf\n"


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
    """An `includeconf` naming a file that does not exist is refused.

    In `ReadConfigFiles`'s words, naming the value as written, as
    `bitcoind` v31.1.0 names `inc/../nosuch.conf`.
    """
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("includeconf=missing.conf\n", encoding="utf-8")
    with pytest.raises(
        ValueError, match=r"^Failed to include configuration file missing\.conf$"
    ):
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


@pytest.mark.parametrize("port", ["+80", " 80", "8_0", "\u0668\u0660"])
def test_build_config_an_rpcbind_port_int_would_read_is_refused(
    tmp_path: Path, port: str
) -> None:
    """`CheckHostPortOptions`' refusal, as `bitcoind` v31.1.0 words each."""
    value = f"127.0.0.1:{port}"
    expected = re.escape(f"Invalid port specified in -rpcbind: '{value}'")
    with pytest.raises(ValueError, match=f"^{expected}$"):
        _build(tmp_path, "-regtest", f"-rpcbind={value}")


@pytest.mark.parametrize("option", ["connect", "addnode"])
def test_build_config_a_peer_s_port_int_would_read_is_a_name(
    tmp_path: Path, option: str
) -> None:
    """Kept whole, as a name, where `int` would dial port 80 (ISS 1292).

    `bitcoind` v31.1.0 starts with `-connect` or `-addnode` at
    `127.0.0.1:+<port>` or `127.0.0.1:0x50` and never connects.
    """
    config = _build(
        tmp_path, "-regtest", f"-{option}=127.0.0.1:+80", f"-{option}=127.0.0.1:0x50"
    )
    assert getattr(config, f"{option}_args") == ("127.0.0.1:+80", "127.0.0.1:0x50")
    assert getattr(config, option) == (
        ("127.0.0.1:+80", config.chain.port),
        ("127.0.0.1:0x50", config.chain.port),
    )


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
    assert cli._parse_parameters(argv, []) == (options, token)


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
        cli._parse_parameters(argv, [])


@pytest.mark.parametrize(
    ("arg", "message"),
    [
        ("-main.rpcpassword=hunter2", "Invalid parameter -main.rpcpassword"),
        ("-foo.rpcauth=hunter2", "Invalid parameter -foo.rpcauth"),
    ],
)
def test_parse_parameters_leaves_a_sensitive_value_out_of_a_refusal(
    arg: str, message: str
) -> None:
    """Core quotes the whole argument; this tree stops at the option name."""
    full = f"Error parsing command line arguments: {message}"
    with pytest.raises(ValueError, match=f"^{re.escape(full)}$") as caught:
        cli._parse_parameters([arg], [])
    assert "hunter2" not in str(caught.value)


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


@pytest.mark.parametrize(
    ("argv", "conf", "ban_time"),
    [
        ([], "", 86400),
        (["-bantime=100"], "", 100),
        ([], "bantime=5\n", 5),
        # ISS 1324: `GetIntArg` saturates at the `int64_t` end
        (["-bantime=99999999999999999999"], "", 2**63 - 1),
    ],
    ids=["Core's default", "command line", "file", "past int64"],
)
def test_build_config_reads_bantime(
    tmp_path: Path, argv: list[str], conf: str, ban_time: int
) -> None:
    """ISS 1219: `-bantime` is a `setban` ban's default length, as in Core."""
    assert _build(tmp_path, *argv, conf=conf).ban_time == ban_time


@pytest.mark.parametrize(
    ("argv", "conf", "send", "receive"),
    [
        ([], "", 1_000_000, 5_000_000),
        (["-maxsendbuffer=8000", "-maxreceivebuffer=1"], "", 8_000_000, 1_000),
        ([], "maxsendbuffer=0\nmaxreceivebuffer=2\n", 0, 2_000),
        # `unsigned int`, as `AppInitMain` stores `1000 * GetIntArg`
        (["-maxsendbuffer=-1"], "", 2**32 - 1_000, 5_000_000),
    ],
    ids=["Core's defaults", "command line", "file", "negative wraps"],
)
def test_build_config_reads_the_buffer_options_in_thousands_of_bytes(
    tmp_path: Path, argv: list[str], conf: str, send: int, receive: int
) -> None:
    """ISS 1812: `-maxsendbuffer` and `-maxreceivebuffer`, `<n>*1000` bytes."""
    config = _build(tmp_path, *argv, conf=conf)
    assert config.send_buffer_max_size == send
    assert config.receive_flood_size == receive


def test_help_names_the_buffer_options() -> None:
    """ISS 1812: in `bitcoind` v31.1.0's words, among the connection options."""
    message = " ".join(cli._help_message(show_debug=False).split())
    assert (
        "-maxreceivebuffer=<n> Maximum per-connection receive buffer, <n>*1000 "
        "bytes (default: 5000)"
    ) in message
    assert (
        "-maxsendbuffer=<n> Maximum per-connection memory usage for the send "
        "buffer, <n>*1000 bytes (default: 1000)"
    ) in message


def test_help_names_bantime() -> None:
    """ISS 1219: in Core's words, among the connection options."""
    assert (
        "Default duration (in seconds) of manually configured bans (default: 86400)"
        in " ".join(cli._help_message(show_debug=False).split())
    )


@pytest.mark.parametrize(
    ("option", "field"),
    [
        ("blocknotify", "block_notify"),
        ("startupnotify", "startup_notify"),
        ("alertnotify", "alert_notify"),
    ],
    ids=["blocknotify", "startupnotify", "alertnotify"],
)
def test_build_config_reads_a_scalar_notify_option(
    tmp_path: Path, option: str, field: str
) -> None:
    """ISS 1519, ISS 1449, ISS 1475: `""` unset, the command line's value set.

    Core reads each of these three with `GetArg`, so only the last
    command-line value survives -- `notify.py`'s own module docstring.
    """
    assert getattr(_build(tmp_path, "-regtest"), field) == ""
    config = _build(tmp_path, "-regtest", f"-{option}=echo one", f"-{option}=echo two")
    assert getattr(config, field) == "echo two"


def test_build_config_reads_shutdownnotify_as_a_list(tmp_path: Path) -> None:
    """ISS 1519: `-shutdownnotify` is Core's own `GetArgs`, every value kept.

    Unlike `-blocknotify`/`-startupnotify`/`-alertnotify` above, which
    each keep only the command line's last value.
    """
    assert _build(tmp_path, "-regtest").shutdown_notify == ()
    config = _build(
        tmp_path, "-regtest", "-shutdownnotify=echo one", "-shutdownnotify=echo two"
    )
    assert config.shutdown_notify == ("echo one", "echo two")


def test_help_names_the_four_notify_options() -> None:
    """ISS 1519, ISS 1449, ISS 1475: each in Core's own words."""
    message = " ".join(cli._help_message(show_debug=False).split())
    assert (
        "Execute command when an alert is raised (%s in cmd is replaced by "
        "message)" in message
    )
    assert (
        "Execute command when the best block changes (%s in cmd is replaced "
        "by block hash)" in message
    )
    assert "Execute command on startup." in message
    assert (
        "Execute command immediately before beginning shutdown. The need "
        "for shutdown may be urgent" in message
    )


@pytest.mark.parametrize(
    ("argv", "conf", "rpcservertimeout"),
    [
        ([], "", 30),
        (["-rpcservertimeout=99000"], "", 99000),
        ([], "rpcservertimeout=5\n", 5),
        (["-rpcservertimeout=0"], "", 0),
        (["-rpcservertimeout=-1"], "", -1),
    ],
    ids=["Core's default", "command line", "file", "zero", "negative one"],
)
def test_build_config_reads_rpcservertimeout(
    tmp_path: Path, argv: list[str], conf: str, rpcservertimeout: int
) -> None:
    """ISS 1548: `-rpcservertimeout` reaches `Config`, as `-bantime` does."""
    assert _build(tmp_path, *argv, conf=conf).rpcservertimeout == rpcservertimeout


def test_help_names_rpcservertimeout_under_debug_alone() -> None:
    """`-rpcservertimeout` is `DEBUG_ONLY` in Core, as `-regtest` is."""
    message = " ".join(cli._help_message(show_debug=True).split())
    assert "Timeout during HTTP requests (default: 30)" in message
    assert "-rpcservertimeout" not in cli._help_message(show_debug=False)


def test_help_names_dnsseed_fixedseeds_and_seednode() -> None:
    """ISS 1192: `-dnsseed`, `-fixedseeds` and `-seednode`, in Core's words."""
    message = " ".join(cli._help_message(show_debug=False).split())
    assert "Query for peer addresses via DNS lookup, if low on addresses" in message
    assert "Allow fixed seeds if DNS seeds don't provide peers" in message
    assert "Connect to a node to retrieve peer addresses, and disconnect" in message


def test_build_config_reads_a_double_dash_option(tmp_path: Path) -> None:
    """`--name=value` is `-name=value`."""
    assert _build(tmp_path, "--maxconnections=7").max_connections == 7


def test_build_config_warns_about_a_double_negative(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-nolisten=0` is `-listen=1`, warned about in the log alone, as in Core.

    ISS 1295.
    """
    config = _build(tmp_path, "-connect=0", "-nolisten=0")
    assert config.listen is True
    assert config.log_warnings == (
        "Parsed potentially confusing double-negative -listen=0",
    )
    assert capsys.readouterr().err == ""


def test_build_config_echoes_a_double_negative_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The warning echoes the value of an option that is not `SENSITIVE`."""
    config = _build(tmp_path, "-connect=0", "-nolisten=garbage")
    assert config.listen is True
    assert config.log_warnings == (
        "Parsed potentially confusing double-negative -listen=garbage",
    )
    assert capsys.readouterr().err == ""


def test_build_config_orders_the_log_warnings_as_bitcoind_logs_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ISS 1295: the command line's, the file's in its order, then the sections.

    Measured on `bitcoind` v31.1.0 with this file and `-nolisten=0`: its
    `debug.log` opens, after five blank lines, on the first four, then
    its version line, then the section warning, which its stderr holds
    alone (ISS 1309's own version line and ISS 1305's own `LogArgs` sit
    between `log_warnings` and `section_warning`, `Config`'s own fields
    for them).
    """
    conf = "regtest=1\nfoo=1\nnoserver=0\n[x]\n[y]\nbar=2\n"
    config = _build(tmp_path, "-nolisten=0", conf=conf)
    path = tmp_path / "bitcoin.conf"
    sections = (
        f"{path}:4 Section [x] is not recognized.\n"
        f"{path}:5 Section [y] is not recognized.\n"
    )
    assert config.log_warnings == (
        "Parsed potentially confusing double-negative -listen=0",
        "Ignoring unknown configuration value foo",
        "Parsed potentially confusing double-negative -server=0",
        "Ignoring unknown configuration value y.bar",
    )
    assert config.section_warning == sections
    assert capsys.readouterr().err == f"Warning: {sections}\n"


def test_build_config_logs_its_config_file_and_command_line_args(
    tmp_path: Path,
) -> None:
    """ISS 1305: `LogArgs`'s two prefixes, `[section] ` and a written value.

    `ArgsManager::LogArgs`/`logArgsPrefix` (`src/common/args.cpp`, at
    bitcoin/bitcoin@9be056a8a7): the config file's args, then the command
    line's; a plain value quoted as `SettingsValue::write()` writes it, a
    negation's `true`, and `-rpcpassword`'s masked to `****` on either.
    `SettingsValue::write()`'s own `json_escape`
    (`src/univalue/lib/univalue_write.cpp`, same sha) doubles a backslash
    like any other JSON string writer, so `datadir` is compared through
    `cli._setting_to_write_str` rather than against the raw path: on
    `windows-latest`, where `tmp_path` carries real backslashes, a plain
    f-string would compare the doubled logged form against the
    undoubled one (ISS 1509).
    """
    conf = "regtest=1\n[regtest]\nrpcbind=127.0.0.1:8332\n"
    config = _build(tmp_path, "-nolisten=0", "-rpcpassword=hunter2", conf=conf)
    assert config.config_args == (
        'Config file arg: regtest="1"',
        'Config file arg: [regtest] rpcbind="127.0.0.1:8332"',
        f"Command-line arg: datadir={cli._setting_to_write_str(str(tmp_path))}",
        "Command-line arg: listen=true",
        "Command-line arg: rpcpassword=****",
    )


def test_log_args_is_config_file_first_then_command_line(tmp_path: Path) -> None:
    """ISS 1305: `std::map` order, a section then a name inside it, sorted.

    `m_settings.ro_config`/`command_line_options` (`src/common/settings.h`,
    same sha): the default section (`""`) sorts ahead of a named one, and
    a name sorts within its own section, whichever order they were set in.
    `datadir` is compared through `cli._setting_to_write_str`, for the
    same reason given in `test_build_config_logs_its_config_file_and_
    command_line_args` above (ISS 1509).
    """
    settings = cli._Settings(
        command_line={"rpcpassword": ["hunter2"], "datadir": [str(tmp_path)]},
        ro_config={
            "regtest": {"rpcbind": ["127.0.0.1:8332"]},
            "": {"regtest": ["1"], "listen": [True]},
        },
    )
    assert cli._log_args(settings) == (
        "Config file arg: listen=true",
        'Config file arg: regtest="1"',
        'Config file arg: [regtest] rpcbind="127.0.0.1:8332"',
        f"Command-line arg: datadir={cli._setting_to_write_str(str(tmp_path))}",
        "Command-line arg: rpcpassword=****",
    )


def test_log_args_escapes_a_backslash_in_datadir() -> None:
    r"""ISS 1509: `value.write()` JSON-escapes a backslash, doubling it.

    A manufactured Windows-style path exercises `SettingsValue::write()`'s
    `json_escape` (`src/univalue/lib/univalue_write.cpp`, at
    bitcoin/bitcoin@9be056a8a7, `escapes[0x5c] == "\\"`) without a
    Windows runner: a literal single backslash in `datadir` is doubled in
    the logged line, exactly as any other string value is. The expected
    value is a hand-written literal, not `_setting_to_write_str` again,
    so the test cannot pass a change that stops escaping it.
    """
    windows_datadir = r"C:\Users\runneradmin\datadir"
    settings = cli._Settings(command_line={"datadir": [windows_datadir]}, ro_config={})
    assert cli._log_args(settings) == (
        'Command-line arg: datadir="C:\\\\Users\\\\runneradmin\\\\datadir"',
    )


@pytest.mark.parametrize("name", ["rpcauth", "rpcpassword", "rpcuser"])
def test_interpret_value_masks_a_sensitive_double_negative(name: str) -> None:
    """A `SENSITIVE` option's value is `****` in the warning, unlike Core."""
    info = cli._interpret_key(f"no{name}")
    warnings: list[str] = []
    assert cli._interpret_value(info, "hunter2", cli._OPTIONS[name], warnings) is True
    assert warnings == [f"Parsed potentially confusing double-negative -{name}=****"]


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
    config = _build(tmp_path, "-connect=0", *argv, conf=conf)
    written = capsys.readouterr().err + "".join(config.log_warnings)
    assert "hunter2" not in written
    assert "-rpcpassword=****" in written


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
        (["-testnet4=0"], "", "mainnet"),
        (["-notestnet4"], "testnet4=1\n", "testnet4"),
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
        "-testnet4=0",
        "-notestnet4 skipped",
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
        ([], "regtest=1\n[regtest]\nconnect=10.0.0.1\n", True, False),
    ],
    ids=[
        "-noconnect",
        "-noconnect over the file",
        "a value after the negation",
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


def test_build_config_a_network_only_value_given_on_the_command_line_too(
    tmp_path: Path,
) -> None:
    """A `network_only` default-section value is skipped once given elsewhere.

    `_check_network_only_args` does not refuse this: `-connect` is also
    given on the command line, so `OnlyHasDefaultSectionSetting` is
    false and `GetUnsuitableSectionOnlyArgs` does not name it -- but
    `GetSettingsList`'s own `UseDefaultSection` skip still drops the
    default section's own value, as it does when nothing else sets the
    option at all.
    """
    config = _build(tmp_path, "-connect=10.0.0.2", conf="regtest=1\nconnect=10.0.0.1\n")
    assert config.connect == (("10.0.0.2", RegTest().port),)


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
    "name", ["port", "rpcport", "rpcbind", "bind", "connect", "addnode"]
)
def test_build_config_refuses_every_network_only_option_off_main(
    tmp_path: Path, name: str
) -> None:
    """ISS 1327: `bitcoind` v31.1.0 refuses to start on this, not only `-port`.

    Each of this node's own `network_only` options, set only in the
    default section while regtest is the chain: Core's own words for
    each, `AppInitParameterInteraction`'s `GetUnsuitableSectionOnlyArgs`
    loop (`src/init.cpp:936-950`, at bitcoin/bitcoin@9be056a8a7).
    """
    value = "127.0.0.1:99999" if name == "rpcbind" else "127.0.0.1"
    message = (
        f"Config setting for -{name} only applied on regtest network "
        "when in [regtest] section.\n"
    )
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        _build(tmp_path, conf=f"regtest=1\n{name}={value}\n")


def test_build_config_refuses_a_network_only_value_before_a_blocksdir_check(
    tmp_path: Path,
) -> None:
    """ISS 1327: `bitcoind` v31.1.0 refuses this before naming `-blocksdir`.

    Measured on `bitcoind` v31.1.0 with `-datadir=<dir> -listen=0
    -rpcport=47391`, `regtest=1` and `port=18999` both in the default
    section, and `-blocksdir=/nonexistent`: the network-only refusal,
    not "Specified blocks directory ... does not exist.", `init.cpp`
    asking `GetUnsuitableSectionOnlyArgs` first.
    """
    message = (
        "Config setting for -port only applied on regtest network when "
        "in [regtest] section.\n"
    )
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        _build(
            tmp_path,
            "-blocksdir=/nonexistent",
            conf="regtest=1\nport=18999\n",
        )


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
    assert not config.rpc_auth_invalid
    assert config.rpc_cookie_perms_error is None
    config = _build(tmp_path, argument)
    assert config.rpc_auth_invalid or config.rpc_cookie_perms_error is not None


@pytest.mark.parametrize(
    "argument", ["-rpcauth=bogus", "-rpccookieperms=bogus"], ids=["rpcauth", "perms"]
)
def test_main_a_refused_rpc_credential_prints_core_s_one_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argument: str,
) -> None:
    """Stderr holds "Unable to start HTTP server" alone, as `bitcoind` has it.

    `bitcoind` v31.1.0 with either value, and with both, prints this line
    alone and exits 1; the value's own line is in its log.
    """
    monkeypatch.setattr(cli, "install_signal_handlers", lambda node: None)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                f"-datadir={tmp_path}",
                "-regtest",
                "-listen=0",
                f"-rpcport={get_random_port()}",
                argument,
            ]
        )
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == (
        "Error: Unable to start HTTP server. See debug log for details.\n"
    )


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


@pytest.mark.parametrize(
    ("value", "read"),
    [
        ("7x", 7),
        (" 12", 12),
        ("12 x", 12),
        ("+5", 5),
        ("-7x", -7),
        ("+-5", 0),
        ("x", 0),
        ("", 0),
        ("0x10", 0),
        ("99999999999999999999", 2**63 - 1),
        ("-99999999999999999999", -(2**63)),
    ],
)
def test_an_integer_is_read_as_core_s_atoi64_reads_it(value: str, read: int) -> None:
    """ISS 1313, ISS 1324: the leading integer, saturated to `int64_t`."""
    assert cli._atoi64(value) == read


# measured on `bitcoind` v31.1.0 with `-regtest -listen=0`: the limit its
# "Using at most <n> automatic connections" line gives, `None` where
# it refuses "-maxconnections must be greater or equal than zero"
@pytest.mark.parametrize(
    ("value", "limit"),
    [
        ("7x", 7),
        (" 12", 12),
        ("12 x", 12),
        ("+5", 5),
        ("+-5", 0),
        ("x", 0),
        ("", 0),
        ("0x10", 0),
        ("99999999999999999999", None),
        ("-99999999999999999999", 0),
        ("4294967296", 0),
        ("4294967297", 1),
        ("-4294967295", 1),
        ("2147483648", None),
        ("-7x", None),
    ],
)
def test_maxconnections_is_the_int_bitcoind_reads(
    tmp_path: Path, value: str, limit: int | None
) -> None:
    """ISS 1313, ISS 1324: `GetIntArg`, then C++'s conversion to `int`."""
    if limit is None:
        with pytest.raises(
            ValueError, match=r"^-maxconnections must be greater or equal than zero$"
        ):
            _build(tmp_path, f"-maxconnections={value}")
    else:
        assert _build(tmp_path, f"-maxconnections={value}").max_connections == limit


@pytest.mark.parametrize(
    ("value", "listen"),
    [("4294967296", True), ("-4294967295", False)],
)
def test_the_listen_soft_set_reads_maxconnections_before_the_int(
    tmp_path: Path, value: str, *, listen: bool
) -> None:
    """ISS 1324: `InitParameterInteraction` compares the `int64_t` with zero.

    Measured on `bitcoind` v31.1.0: `-maxconnections=4294967296` logs no
    "setting -listen=0" though its limit is 0, and `-4294967295` logs
    it though its limit is 1.
    """
    assert _build(tmp_path, f"-maxconnections={value}").listen is listen


@pytest.mark.parametrize(
    ("value", "dnsseed"),
    [("4294967296", True), ("-4294967295", False)],
)
def test_the_dnsseed_soft_set_reads_maxconnections_before_the_int(
    tmp_path: Path, value: str, *, dnsseed: bool
) -> None:
    """ISS 1324: the same `if` as `-listen`'s, over the same `int64_t`.

    Measured on `bitcoind` v31.1.0: `-maxconnections=4294967296` logs
    "dnsseed thread start", and `-4294967295` logs "setting -dnsseed=0".
    """
    assert _build(tmp_path, f"-maxconnections={value}").dnsseed is dnsseed


def test_forcednsseed_defaults_to_false(tmp_path: Path) -> None:
    """`DEFAULT_FORCEDNSSEED` (`src/net.h`, at bitcoin/bitcoin@9be056a8a7)."""
    assert _build(tmp_path).forcednsseed is False


def test_forcednsseed_is_read(tmp_path: Path) -> None:
    """ISS 1265: `-forcednsseed=1` no longer "Invalid parameter"."""
    assert _build(tmp_path, "-forcednsseed=1").forcednsseed is True


@pytest.mark.parametrize(
    "argv",
    [("-forcednsseed=1", "-connect=1.2.3.4"), ("-forcednsseed=1", "-maxconnections=0")],
    ids=["-connect", "-maxconnections=0"],
)
def test_forcednsseed_is_refused_alongside_the_dnsseed_soft_set_off(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    """ISS 1265: refused as `bitcoind` v31.1.0 refuses it, its own wording.

    Measured there with `-forcednsseed=1 -dnsseed=0`: exit 1,
    "Error: Cannot set -forcednsseed to true when setting -dnsseed to
    false." No explicit `-dnsseed` is given here, so it is the soft-set
    that turns it off -- `-connect` or `-maxconnections=0`
    (btclib-org/btclib-node#1192's own `-dnsseed`); an explicit
    `-dnsseed=1` instead is the test below this one.
    """
    expected = re.escape(
        "Cannot set -forcednsseed to true when setting -dnsseed to false."
    )
    with pytest.raises(ValueError, match=f"^{expected}$"):
        _build(tmp_path, *argv)


@pytest.mark.parametrize(
    "argv",
    [
        ("-dnsseed=1", "-forcednsseed=1", "-connect=1.2.3.4"),
        ("-dnsseed=1", "-forcednsseed=1", "-maxconnections=0"),
    ],
    ids=["-connect", "-maxconnections=0"],
)
def test_an_explicit_dnsseed_true_wins_over_the_soft_set(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    """ISS 1265, review round 3: `-dnsseed=1` wins over the soft-set-off.

    `InitParameterInteraction`'s `SoftSetBoolArg` (`src/init.cpp`, at
    bitcoin/bitcoin@9be056a8a7) never overwrites an arg already set,
    whichever condition would otherwise have turned `-dnsseed` off.
    This reaches `_before_lock`'s own early `Config(...)` call: a
    version reading the raw soft-set alone, ignoring the explicit
    value, refused this combination in error.
    """
    built = _build(tmp_path, *argv)
    assert built.dnsseed is True
    assert built.forcednsseed is True


def test_forcednsseed_refusal_precedes_the_maxconnections_refusal(
    tmp_path: Path,
) -> None:
    """ISS 1265: `AppInitParameterInteraction`'s own order (same sha).

    `-maxconnections=-1` alone raises its own message
    (`test_maxconnections_is_the_int_bitcoind_reads` above) -- it is
    also what turns the `-dnsseed` soft-set off here, `-1 > 0` being
    false, so paired with `-forcednsseed=1` the `-forcednsseed` refusal
    is the one that surfaces, Core checking it first.
    """
    expected = re.escape(
        "Cannot set -forcednsseed to true when setting -dnsseed to false."
    )
    with pytest.raises(ValueError, match=f"^{expected}$"):
        _build(tmp_path, "-forcednsseed=1", "-maxconnections=-1")


# measured on `bitcoind` v31.1.0 with `-regtest -listen=0`:
# `getblockchaininfo`'s `pruned` and `prune_target_size`, the target in
# MiB here, and `None` for a start it refuses as below the minimum
@pytest.mark.parametrize(
    ("value", "pruned", "target_mib"),
    [
        ("+-5", False, None),
        ("x", False, None),
        ("", False, None),
        ("0x10", False, None),
        ("1", True, None),
        ("99999999999999999999", True, 2**44 - 1),
        ("4294967296", True, 2**32),
        ("17592186044416", False, None),
        ("17592186044966", True, 550),
    ],
)
def test_prune_is_the_target_bitcoind_reads(
    tmp_path: Path, value: str, *, pruned: bool, target_mib: int | None
) -> None:
    """ISS 1313, ISS 1324: `GetIntArg`, then a wrapped `uint64_t` count."""
    config = _build(tmp_path, f"-prune={value}")
    assert config.pruned is pruned
    assert config.prune_target_mib == target_mib


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            "7x",
            "Prune configured below the minimum of 550 MiB.  Please use a higher number.",
        ),
        (
            " 12",
            "Prune configured below the minimum of 550 MiB.  Please use a higher number.",
        ),
        (
            "17592186044417",
            "Prune configured below the minimum of 550 MiB.  Please use a higher number.",
        ),
        ("-7x", "Prune cannot be configured with a negative value."),
        ("-99999999999999999999", "Prune cannot be configured with a negative value."),
    ],
)
def test_prune_is_refused_as_bitcoind_refuses_it(
    tmp_path: Path, value: str, message: str
) -> None:
    """ISS 1313, ISS 1324: measured on `bitcoind` v31.1.0, the same words."""
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        _build(tmp_path, f"-prune={value}")


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
    assert "\nRPC server options:\n\n  -rpcallowip=<ip>\n       Allow JSON-RPC" in out
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
        (["-testnet4"], "testnet4"),
        (["-chain=main"], "mainnet"),
        (["-chain=test"], "testnet"),
        (["-chain=signet"], "signet"),
        (["-chain=regtest"], "regtest"),
        (["-chain=testnet4"], "testnet4"),
    ],
)
def test_build_config_selects_the_chain(
    tmp_path: Path, argv: list[str], chain_name: str
) -> None:
    """A selector, or `-chain=<alias>` in Core's own external vocabulary."""
    assert _build(tmp_path, *argv).chain.name == chain_name


@pytest.mark.parametrize(
    ("argv", "conf"),
    [
        (["-testnet", "-signet"], ""),
        (["-testnet"], "signet=1\n"),
        (["-testnet4", "-testnet"], ""),
    ],
    ids=["two on the command line", "one each side", "-testnet4 and -testnet"],
)
def test_build_config_refuses_two_chain_selectors(
    tmp_path: Path, argv: list[str], conf: str
) -> None:
    """More than one selector, counted over the command line and the file.

    `bitcoind` v31.1.0's own words, `-testnet4` named among the five
    selectors (btclib-org/btclib-node#1311).
    """
    with pytest.raises(
        ValueError,
        match=(
            r"^Invalid combination of -regtest, -signet, -testnet, "
            r"-testnet4 and -chain\. Can use at most one\.$"
        ),
    ):
        _build(tmp_path, *argv, conf=conf)


@pytest.mark.parametrize(
    ("argv", "conf", "alias"),
    [([], "chain=bogus\n", "bogus"), (["-nochain"], "", "0")],
)
def test_build_config_refuses_an_unknown_chain(
    tmp_path: Path, argv: list[str], conf: str, alias: str
) -> None:
    """An alias outside Core's five, `-nochain`'s `0` among them.

    `bitcoind` v31.1.0's own words: "Unknown chain bogus.", not quoted
    and ending in a full stop (btclib-org/btclib-node#1311).
    """
    with pytest.raises(ValueError, match=f"^Unknown chain {alias}\\.$"):
        _build(tmp_path, *argv, conf=conf)


def test_build_config_with_nothing_given_uses_every_default(tmp_path: Path) -> None:
    """No flags, no file: every `Config` field takes its own default."""
    config = cli.build_config([f"-datadir={tmp_path}"])
    assert config.chain.name == "mainnet"
    assert config.rpc_host is None
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


@pytest.mark.parametrize(
    ("value", "sats_per_kvbyte"),
    [
        ("0.00002", 2000),
        (" 0.00001", 1000),
        ("0.00001 ", 1000),
        (".", 0),
        ("1.", 100_000_000),
        (".5", 50_000_000),
        ("21000000", 21_000_000 * 100_000_000),
    ],
)
def test_build_config_minrelaytxfee_is_read_in_btc_per_kvb(
    tmp_path: Path, value: str, sats_per_kvbyte: int
) -> None:
    """`-minrelaytxfee` sets `min_relay_feerate`, as `ParseMoney` reads it.

    `bitcoind` v31.1.0 starts with each of these, and answers
    `-minrelaytxfee=0.00002` with a `getmempoolinfo` `minrelaytxfee` of
    0.00002000 (btclib-org/btclib-node#1332).
    """
    config = _build(tmp_path, "-regtest", f"-minrelaytxfee={value}")
    assert config.min_relay_feerate.sats_per_kvbyte == sats_per_kvbyte


def test_build_config_minrelaytxfee_defaults_negates_and_reads_the_file(
    tmp_path: Path,
) -> None:
    """Unset, Core's default; negated, `0`; read from `bitcoin.conf`.

    `0` for the negation is what `bitcoind` answers.
    """
    assert _build(tmp_path, "-regtest").min_relay_feerate.sats_per_kvbyte == 100
    negated = _build(tmp_path, "-regtest", "-nominrelaytxfee")
    assert negated.min_relay_feerate.sats_per_kvbyte == 0
    from_file = _build(tmp_path, conf="regtest=1\nminrelaytxfee=0.00003\n")
    assert from_file.min_relay_feerate.sats_per_kvbyte == 3000


@pytest.mark.parametrize(
    "value",
    [
        "abc",
        "-1",
        "+1",
        "0.000000001",
        "1e-5",
        "21000001",
        "00000000001",
        "12345678901",
        "",
        "1.5.3",
        "1\x000",
    ],
)
def test_build_config_minrelaytxfee_that_is_no_amount_is_refused(
    tmp_path: Path, value: str
) -> None:
    """`AmountErrMsg`'s words, as `bitcoind` v31.1.0 refuses each at start.

    `1.5.3` and a NUL were not run against `bitcoind`, whose `ParseMoney`
    stops at the second `.` and refuses a string holding a NUL.
    """
    expected = re.escape(f"Invalid amount for -minrelaytxfee=<amount>: '{value}'")
    with pytest.raises(ValueError, match=f"^{expected}$"):
        _build(tmp_path, "-regtest", f"-minrelaytxfee={value}")


def test_build_config_help_lists_minrelaytxfee_under_node_relay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under Core's own title and in Core's words, `-help`'s layout."""
    with pytest.raises(SystemExit):
        _build(tmp_path, "-help")
    out = capsys.readouterr().out
    assert (
        "\n\n  -minrelaytxfee=<amt>\n       Fees (in BTC/kvB) "
        "smaller than this are considered zero fee for\n       relaying, mining "
        "and transaction creation (default: 0.000001)\n\n  -permitbaremultisig"
    ) in out


@pytest.mark.parametrize(
    ("option", "field", "default"),
    [
        ("incrementalrelayfee", "incremental_relay_feerate", 100),
        ("dustrelayfee", "dust_relay_feerate", 3000),
    ],
)
def test_build_config_a_relay_feerate_is_read_in_btc_per_kvb(
    tmp_path: Path, option: str, field: str, default: int
) -> None:
    """Read as `-minrelaytxfee` is: Core's default, a value, `0`, the file.

    btclib-org/btclib-node#1596 is `-incrementalrelayfee`'s.
    """
    assert getattr(_build(tmp_path, "-regtest"), field).sats_per_kvbyte == default
    given = _build(tmp_path, "-regtest", f"-{option}=0.00002")
    assert getattr(given, field).sats_per_kvbyte == 2000
    negated = _build(tmp_path, "-regtest", f"-no{option}")
    assert getattr(negated, field).sats_per_kvbyte == 0
    from_file = _build(tmp_path, conf=f"regtest=1\n{option}=0.00003\n")
    assert getattr(from_file, field).sats_per_kvbyte == 3000


@pytest.mark.parametrize("option", ["incrementalrelayfee", "dustrelayfee"])
def test_build_config_a_relay_feerate_that_is_no_amount_is_refused(
    tmp_path: Path, option: str
) -> None:
    """`AmountErrMsg`'s words, naming the option."""
    expected = re.escape(f"Invalid amount for -{option}=<amount>: 'abc'")
    with pytest.raises(ValueError, match=f"^{expected}$"):
        _build(tmp_path, "-regtest", f"-{option}=abc")


def test_build_config_incrementalrelayfee_is_refused_before_minrelaytxfee(
    tmp_path: Path,
) -> None:
    """`ApplyArgsManOptions` reads `-incrementalrelayfee` first."""
    with pytest.raises(ValueError, match="-incrementalrelayfee"):
        _build(tmp_path, "-regtest", "-minrelaytxfee=abc", "-incrementalrelayfee=abc")


def test_build_config_incrementalrelayfee_raises_a_minrelaytxfee_not_given(
    tmp_path: Path,
) -> None:
    """Core lets `-incrementalrelayfee` alone raise the floor, never lower it.

    A `-minrelaytxfee` given, even a lower one, stays as it is.
    """
    raised = _build(tmp_path, "-regtest", "-incrementalrelayfee=0.00002")
    assert raised.min_relay_feerate.sats_per_kvbyte == 2000
    lowered = _build(tmp_path, "-regtest", "-incrementalrelayfee=0.00000050")
    assert lowered.min_relay_feerate.sats_per_kvbyte == 100
    given = _build(
        tmp_path,
        "-regtest",
        "-incrementalrelayfee=0.00002",
        "-minrelaytxfee=0.00001",
    )
    assert given.min_relay_feerate.sats_per_kvbyte == 1000


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ((), 100_000),
        (("-nodatacarrier",), None),
        (("-nodatacarrier", "-datacarriersize=5"), None),
        (("-datacarrier=1", "-datacarriersize=83"), 83),
        (("-datacarriersize=0",), 0),
        (("-nodatacarriersize",), 0),
        (("-datacarriersize=-1",), 2**32 - 1),
        (("-datacarriersize=4294967297",), 1),
    ],
)
def test_build_config_datacarrier_options(
    tmp_path: Path, argv: tuple[str, ...], expected: int | None
) -> None:
    """`max_datacarrier_bytes`: `None` where off, else the size mod 2**32.

    Core stores `-datacarriersize`'s `int64_t` in an `unsigned int`.
    """
    assert _build(tmp_path, "-regtest", *argv).max_datacarrier_bytes == expected


def test_build_config_permitbaremultisig_defaults_negates_and_reads_the_file(
    tmp_path: Path,
) -> None:
    """On unless negated or set false (btclib-org/btclib-node#1497)."""
    assert _build(tmp_path, "-regtest").permit_bare_multisig is True
    assert (
        _build(tmp_path, "-regtest", "-permitbaremultisig=0").permit_bare_multisig
        is False
    )
    assert (
        _build(tmp_path, "-regtest", "-nopermitbaremultisig").permit_bare_multisig
        is False
    )
    from_file = _build(tmp_path, conf="regtest=1\npermitbaremultisig=0\n")
    assert from_file.permit_bare_multisig is False


def test_build_config_acceptnonstdtxn_is_for_test_chains_only(tmp_path: Path) -> None:
    """`require_standard` is off on a test chain and refused on `main`."""
    assert _build(tmp_path, "-regtest").require_standard is True
    assert _build(tmp_path, "-regtest", "-acceptnonstdtxn").require_standard is False
    assert (
        _build(tmp_path, "-chain=main", "-acceptnonstdtxn=0").require_standard is True
    )
    expected = re.escape("acceptnonstdtxn is not currently supported for main chain")
    with pytest.raises(ValueError, match=f"^{expected}$"):
        _build(tmp_path, "-acceptnonstdtxn")


def test_build_config_help_lists_the_relay_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-help` shows the non-debug relay options, `-help-debug` the rest."""
    with pytest.raises(SystemExit):
        _build(tmp_path, "-help")
    out = capsys.readouterr().out
    for shown in ("-datacarrier", "-datacarriersize=<n>", "-permitbaremultisig"):
        assert f"\n  {shown}\n" in out
    for hidden in ("-acceptnonstdtxn", "-incrementalrelayfee", "-dustrelayfee"):
        assert hidden not in out
    with pytest.raises(SystemExit):
        _build(tmp_path, "-help-debug")
    out = capsys.readouterr().out
    for shown in (
        "-acceptnonstdtxn",
        "-incrementalrelayfee=<amt>",
        "-dustrelayfee=<amt>",
    ):
        assert f"\n  {shown}\n" in out
    assert "(default: 0.000001)" in out
    assert "(default: 0.00003)" in out


def test_build_config_maxtipage_defaults_negates_and_reads_in_seconds(
    tmp_path: Path,
) -> None:
    """ISS 1474: `-maxtipage` is `Config.max_tip_age`, in seconds.

    Unset, `DEFAULT_MAX_TIP_AGE` (a day); negated, `0`, as a negated
    `GetIntArg` reads; read from `bitcoin.conf` like any other option.
    """
    assert _build(tmp_path, "-regtest").max_tip_age == DEFAULT_MAX_TIP_AGE
    assert _build(tmp_path, "-regtest", "-maxtipage=3600").max_tip_age == 3600
    negated = _build(tmp_path, "-regtest", "-nomaxtipage")
    assert negated.max_tip_age == 0
    from_file = _build(tmp_path, conf="regtest=1\nmaxtipage=120\n")
    assert from_file.max_tip_age == 120


def test_build_config_acceptstalefeeestimates_is_a_debug_only_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Core's `-acceptstalefeeestimates`: off unless set, in `-help-debug`."""
    assert not _build(tmp_path, "-regtest").accept_stale_fee_estimates
    flag = _build(tmp_path, "-regtest", "-acceptstalefeeestimates")
    assert flag.accept_stale_fee_estimates
    with pytest.raises(SystemExit):
        _build(tmp_path, "-help")
    assert "-acceptstalefeeestimates" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        _build(tmp_path, "-help-debug")
    # bitcoind v31.1.0's own `-help-debug`
    assert (
        "  -acceptstalefeeestimates\n"
        "       Read fee estimates even if they are stale (regtest only; default: "
        "0) fee\n"
        "       estimates are considered stale if they are 60 hours old\n"
    ) in capsys.readouterr().out


def test_build_config_maxtipage_is_debug_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-maxtipage` is `DEBUG_ONLY`: listed under `-help-debug` alone."""
    with pytest.raises(SystemExit):
        _build(tmp_path, "-help")
    assert "-maxtipage" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        _build(tmp_path, "-help-debug")
    out = capsys.readouterr().out
    assert (
        "-maxtipage=<n>\n       Maximum tip age in seconds to consider node in "
        f"initial block download\n       (default: {DEFAULT_MAX_TIP_AGE})"
    ) in out


@pytest.mark.parametrize(
    ("chain_flag", "chain"),
    [
        ("-regtest", RegTest()),
        ("-testnet", TestNet()),
        ("-signet", SigNet()),
        ("-testnet4", TestNet4()),
    ],
)
def test_build_config_minimumchainwork_defaults_to_the_chain_s_own(
    tmp_path: Path, chain_flag: str, chain: Any
) -> None:
    """ISS 1500: unset, `-minimumchainwork` defaults per chain."""
    config = _build(tmp_path, chain_flag)
    assert config.minimum_chain_work == chain.consensus.minimum_chain_work


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10", 0x10),
        ("0x10", 0x10),
        ("", 0),
        ("f" * 64, int("f" * 64, 16)),
    ],
)
def test_build_config_minimumchainwork_reads_hex_and_an_optional_0x_prefix(
    tmp_path: Path, value: str, expected: int
) -> None:
    """`uint256::FromUserHex`: an optional `0x`, short values padded."""
    config = _build(tmp_path, "-regtest", f"-minimumchainwork={value}")
    assert config.minimum_chain_work == expected


def test_build_config_minimumchainwork_uppercase_0x_is_not_a_prefix(
    tmp_path: Path,
) -> None:
    """`RemovePrefixView`'s own "0x" is a literal, lowercase match only.

    `0X10` is refused rather than read as `0x10`: the `X` is not itself
    a hex digit, and nothing strips it first.
    """
    expected = re.escape(
        "Invalid minimum work specified (0X10), must be up to 64 hex digits"
    )
    with pytest.raises(ValueError, match=f"^{expected}$"):
        _build(tmp_path, "-regtest", "-minimumchainwork=0X10")


def test_build_config_minimumchainwork_negated_reads_as_zero(tmp_path: Path) -> None:
    """A negation reads as `0`, the way every other `GetArg` string does."""
    config = _build(tmp_path, "-regtest", "-nominimumchainwork")
    assert config.minimum_chain_work == 0


@pytest.mark.parametrize("value", ["z" * 10, "0xgg", "a" * 65, "0x" + "a" * 65])
def test_build_config_minimumchainwork_that_is_not_valid_hex_is_refused(
    tmp_path: Path, value: str
) -> None:
    """Core's own words: refused past 64 hex digits, or on a non-hex one."""
    expected = re.escape(
        f"Invalid minimum work specified ({value}), must be up to 64 hex digits"
    )
    with pytest.raises(ValueError, match=f"^{expected}$"):
        _build(tmp_path, "-regtest", f"-minimumchainwork={value}")


def test_build_config_minimumchainwork_is_debug_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-minimumchainwork` is `DEBUG_ONLY` in Core too."""
    with pytest.raises(SystemExit):
        _build(tmp_path, "-help")
    assert "-minimumchainwork" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        _build(tmp_path, "-help-debug")
    assert "-minimumchainwork=<hex>" in capsys.readouterr().out


def test_build_config_assumevalid_is_off_when_not_given(tmp_path: Path) -> None:
    """ISS 1576: until Core's default per network is read, it is off."""
    assert _build(tmp_path, "-regtest").assume_valid is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", "00" * 31 + "01"),
        ("0x10", "00" * 31 + "10"),
        ("0000000000000000000" + "1" * 45, "0000000000000000000" + "1" * 45),
        ("f" * 64, "ff" * 32),
    ],
)
def test_build_config_assumevalid_reads_the_block_hash_as_displayed(
    tmp_path: Path, value: str, expected: str
) -> None:
    """`uint256::FromUserHex`: an optional `0x`, short values padded."""
    config = _build(tmp_path, "-regtest", f"-assumevalid={value}")
    assert config.assume_valid == bytes.fromhex(expected.rjust(64, "0"))


@pytest.mark.parametrize("arg", ["-assumevalid=0", "-assumevalid=", "-noassumevalid"])
def test_build_config_assumevalid_zero_is_off(tmp_path: Path, arg: str) -> None:
    """`0`, empty and a negation all read as zero: Core's "verify all"."""
    assert _build(tmp_path, "-regtest", arg).assume_valid is None


@pytest.mark.parametrize("value", ["z" * 10, "0xgg", "0X10", "a" * 65])
def test_build_config_assumevalid_that_is_not_valid_hex_is_refused(
    tmp_path: Path, value: str
) -> None:
    """Core's own words, refused past 64 hex digits or on a non-hex one."""
    expected = re.escape(
        f"Invalid assumevalid block hash specified ({value}), must be up to "
        "64 hex digits (or 0 to disable)"
    )
    with pytest.raises(ValueError, match=f"^{expected}$"):
        _build(tmp_path, "-regtest", f"-assumevalid={value}")


def test_build_config_assumevalid_help_is_cores(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-help` lists `-assumevalid=<hex>` as Core does."""
    with pytest.raises(SystemExit):
        _build(tmp_path, "-help")
    out = " ".join(capsys.readouterr().out.split())
    assert (
        "-assumevalid=<hex> If this block is in the chain assume that it and "
        "its ancestors are valid and potentially skip their script "
        "verification (0 to verify all, default: " + "0" * 64
    ) in out


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


@pytest.mark.parametrize(
    ("argv", "conf", "users"),
    [
        (["-rpcauth={}", "-norpcauth"], "", []),
        (["-norpcauth", "-rpcauth={}"], "", ["pytest"]),
        (["-norpcauth"], "rpcauth={}\n", []),
        (["-norpcauth", "-rpcauth=other:aa$bb"], "rpcauth={}\n", ["other", "pytest"]),
    ],
    ids=[
        "after a value",
        "before a value",
        "over the file",
        "over the file, a value after it",
    ],
)
def test_build_config_norpcauth_is_get_settings_list(
    tmp_path: Path, argv: list[str], conf: str, users: list[str]
) -> None:
    """`-norpcauth` discards every `-rpcauth` before it, and the file's.

    `gArgs.GetArgs("-rpcauth")` (`src/httprpc.cpp`, at
    bitcoin/bitcoin@9be056a8a7) is `GetSettingsList`'s: a value after the
    negation still brings the file's back. `bitcoind` v31.1.0 answers a
    request with the credential each case leaves out 401, and with one it
    keeps 200.
    """
    (tmp_path / "bitcoin.conf").write_text(
        "regtest=1\n" + conf.format(RPCAUTH), encoding="utf-8"
    )
    argv = [arg.format(RPCAUTH) for arg in argv]
    config = cli.build_config([f"-datadir={tmp_path}", *argv])
    assert [entry.user.decode() for entry in config.rpc_auth] == users


def test_build_config_a_malformed_rpcauth_in_the_file_is_kept(tmp_path: Path) -> None:
    """A malformed `rpcauth=` is left for the RPC listener to refuse."""
    (tmp_path / "bitcoin.conf").write_text(
        "regtest=1\nrpcauth=pytest:no-dollar-sign\n", encoding="utf-8"
    )
    assert cli.build_config([f"-datadir={tmp_path}"]).rpc_auth_invalid


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


def _ignored_conf(datadir: str, config: str, conf: str) -> str:
    """Return `InitConfig`'s refusal of an ignored `bitcoin.conf`, as measured.

    `bitcoind` v31.1.0 printed it after `Error: `, the paths absolute.
    """
    return (
        f'Data directory "{datadir}" contains a "bitcoin.conf" file which is '
        f'ignored, because a different configuration file "{config}" from command '
        f'line argument "-conf={conf}" is being used instead. Possible ways to '
        "address this would be to:\n"
        f'- Delete or rename the "bitcoin.conf" file in data directory "{datadir}".\n'
        "- Change datadir= or conf= options to specify one configuration file, not "
        "two, and use includeconf= to include any other configuration files.\n"
        "- Set allowignoredconf=1 option to treat this condition as a warning, not "
        "an error."
    )


@pytest.mark.parametrize(
    ("datadir", "conf", "shown_datadir", "shown_config"),
    [
        ("{d}", "other.conf", "{d}", "{d}{s}other.conf"),
        ("{d}/", "./sub/../other.conf", "{d}", "{d}{s}other.conf"),
        ("{d}", "{d}/other.conf", "{d}", "{d}{s}other.conf"),
        ("d", "other.conf", "{d}", "{d}{s}other.conf"),
        ("d", "../d/other.conf", "{d}", "{d}{s}..{s}d{s}other.conf"),
    ],
    ids=["relative", "normalised", "absolute", "relative -datadir", "up and back"],
)
def test_build_config_an_ignored_bitcoin_conf_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    datadir: str,
    conf: str,
    shown_datadir: str,
    shown_config: str,
) -> None:
    """A `bitcoin.conf` `-conf` leaves unread stops the node, as it stops Core.

    Each case is one `bitcoind` v31.1.0 was run against, from the same
    working directory, with `-help`: Core refuses ahead of the help.
    """
    data = tmp_path / "d"
    (data / "sub").mkdir(parents=True)
    (data / "bitcoin.conf").write_text("regtest=1\n", encoding="utf-8")
    (data / "other.conf").write_text("regtest=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    d = str(data)
    argv = [f"-datadir={datadir.format(d=d)}", f"-conf={conf.format(d=d)}", "-h"]
    expected = _ignored_conf(
        shown_datadir.format(d=d), shown_config.format(d=d, s=os.sep), conf.format(d=d)
    )
    with pytest.raises(ValueError, match=r"^Data directory") as error:
        cli.build_config(argv)
    assert str(error.value) == expected


def test_build_config_an_ignored_bitcoin_conf_keeps_a_dot_datadir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-datadir=.` is shown as `fs::absolute` shows it, the `.` kept."""
    (tmp_path / "bitcoin.conf").write_text("", encoding="utf-8")
    (tmp_path / "other.conf").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=r"^Data directory") as error:
        cli.build_config(["-datadir=.", "-conf=other.conf"])
    dot = os.path.join(str(tmp_path), ".")  # noqa: PTH118
    other_conf = os.path.join(dot, "other.conf")  # noqa: PTH118
    assert str(error.value) == _ignored_conf(dot, other_conf, "other.conf")


def test_build_config_an_ignored_bitcoin_conf_in_the_default_datadir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no `-datadir`, the default data directory is the one checked."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    data = tmp_path / ".btclib"
    data.mkdir()
    (data / "bitcoin.conf").write_text("", encoding="utf-8")
    (data / "other.conf").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^Data directory") as error:
        cli.build_config(["-conf=other.conf"])
    expected = _ignored_conf(str(data), str(data / "other.conf"), "other.conf")
    assert str(error.value) == expected


@pytest.mark.skipif(os.name == "nt", reason='`"` names no Windows file')
def test_build_config_an_ignored_bitcoin_conf_is_quoted_as_core_quotes(
    tmp_path: Path,
) -> None:
    """A `"` or a `&` in a path is escaped with `&`, as `fs::quoted` does."""
    (tmp_path / "bitcoin.conf").write_text("", encoding="utf-8")
    (tmp_path / 'o"t&.conf').write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^Data directory") as error:
        cli.build_config([f"-datadir={tmp_path}", '-conf=o"t&.conf'])
    assert f'file "{tmp_path}/o&"t&&.conf" from' in str(error.value)
    assert 'argument "-conf=o&"t&&.conf" is' in str(error.value)


def test_build_config_an_ignored_bitcoin_conf_directory_is_refused(
    tmp_path: Path,
) -> None:
    """`fs::exists` is true of a directory, and no file is equivalent to it."""
    (tmp_path / "bitcoin.conf").mkdir()
    (tmp_path / "other.conf").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^Data directory"):
        cli.build_config([f"-datadir={tmp_path}", "-conf=other.conf"])


def test_build_config_an_ignored_bitcoin_conf_comes_before_the_token(
    tmp_path: Path,
) -> None:
    """`InitConfig` runs inside `ParseArgs`, ahead of its "unexpected token"."""
    (tmp_path / "bitcoin.conf").write_text("", encoding="utf-8")
    (tmp_path / "other.conf").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^Data directory"):
        cli.build_config([f"-datadir={tmp_path}", "-conf=other.conf", "token"])


@pytest.mark.parametrize(
    ("argv", "other"),
    [
        (["-allowignoredconf"], ""),
        (["-allowignoredconf=1"], ""),
        ([], "allowignoredconf=1\n"),
        ([], "[regtest]\nallowignoredconf=1\n"),
    ],
    ids=["bare", "=1", "in the file", "in the chain's section"],
)
def test_build_config_allowignoredconf_warns_instead(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    other: str,
) -> None:
    """`-allowignoredconf` makes the refusal a warning, `-conf`'s file read."""
    (tmp_path / "bitcoin.conf").write_text("", encoding="utf-8")
    (tmp_path / "other.conf").write_text(
        "regtest=1\n" + other + "[regtest]\nport=9123\n", encoding="utf-8"
    )
    config = cli.build_config([f"-datadir={tmp_path}", "-conf=other.conf", *argv])
    assert config.p2p_port == 9123
    other_conf = str(tmp_path / "other.conf")
    expected = _ignored_conf(str(tmp_path), other_conf, "other.conf")
    # ISS 1295: for the log alone, as Core's `LogWarning` there
    assert config.log_warnings == (expected.rpartition("\n")[0],)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("argv", [["-allowignoredconf=0"], ["-noallowignoredconf"]])
def test_build_config_allowignoredconf_false_still_refuses(
    tmp_path: Path, argv: list[str]
) -> None:
    """`-allowignoredconf=0` and `-noallowignoredconf` refuse, as in Core."""
    (tmp_path / "bitcoin.conf").write_text("", encoding="utf-8")
    (tmp_path / "other.conf").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^Data directory"):
        cli.build_config([f"-datadir={tmp_path}", "-conf=other.conf", *argv])


@pytest.mark.parametrize(
    ("argv", "link"),
    [
        (["-conf=./bitcoin.conf"], False),
        (["-conf={d}/bitcoin.conf"], False),
        (["-conf="], False),
        (["-conf=other.conf"], True),
        (["-noconf", "-regtest"], False),
    ],
    ids=["./bitcoin.conf", "absolute", "empty", "a hard link to it", "-noconf"],
)
def test_build_config_bitcoin_conf_itself_is_not_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], *, link: bool
) -> None:
    """The data directory's own file, by any name, is the file in use."""
    (tmp_path / "bitcoin.conf").write_text("regtest=1\n", encoding="utf-8")
    if link:
        (tmp_path / "other.conf").hardlink_to(tmp_path / "bitcoin.conf")
    argv = [arg.format(d=tmp_path) for arg in argv]
    config = cli.build_config([f"-datadir={tmp_path}", *argv])
    assert config.chain.name == "regtest"
    assert capsys.readouterr().err == ""


@pytest.mark.skipif(os.name == "nt", reason="a symbolic link needs a privilege")
@pytest.mark.parametrize(
    ("argv", "files", "refused"),
    [
        (["-datadir={x}/a/sym/.."], ["real/bitcoin.conf"], False),
        (["-datadir={x}/a/sym/.."], ["real/bitcoin.conf", "a/bitcoin.conf"], False),
        (
            ["-datadir={x}/D", "-conf=sym/../other.conf"],
            ["D/bitcoin.conf", "real/other.conf"],
            True,
        ),
    ],
    ids=["datadir", "datadir, a bitcoin.conf beside the link", "conf"],
)
def test_build_config_an_ignored_bitcoin_conf_through_a_symbolic_link(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    files: list[str],
    *,
    refused: bool,
) -> None:
    """The file compared with `bitcoin.conf` is the file that was read.

    `a/sym` and `D/sym` link to `real/inner`, so `sym/..` is `real` to the
    operating system and `a` or `D` to Core's lexical normalisation.
    `bitcoind` v31.1.0 starts on the first two, and refuses the third
    because `D/other.conf` could not be opened, its message measured.
    """
    for directory in ("real/inner", "a", "D"):
        (tmp_path / directory).mkdir(parents=True)
    for link in ("a/sym", "D/sym"):
        (tmp_path / link).symlink_to(tmp_path / "real" / "inner")
    for name in files:
        (tmp_path / name).write_text("regtest=1\n", encoding="utf-8")
    argv = [arg.format(x=tmp_path) for arg in argv]
    if refused:
        with pytest.raises(ValueError, match=r"^Error reading") as error:
            cli.build_config([*argv, "-h"])
        assert str(error.value) == (
            "Error reading configuration file: specified config file "
            f'"{tmp_path / "D" / "other.conf"}" could not be opened.'
        )
        return
    with pytest.raises(SystemExit) as stop:
        cli.build_config([*argv, "-h"])
    assert stop.value.code == 0
    assert capsys.readouterr().err == ""


def test_build_config_an_ignored_bitcoin_conf_os_error_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `OSError` comparing the files is refused, as Core's `catch` does."""

    def refuse(*_: object) -> bool:
        raise PermissionError(13, "Permission denied")

    (tmp_path / "bitcoin.conf").write_text("", encoding="utf-8")
    (tmp_path / "other.conf").write_text("", encoding="utf-8")
    monkeypatch.setattr(os.path, "samefile", refuse)
    with pytest.raises(ValueError, match=r"^\[Errno 13\] Permission denied$"):
        cli.build_config([f"-datadir={tmp_path}", "-conf=other.conf"])


def test_build_config_an_absolute_datadir_needs_no_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute `-datadir` asks for no working directory: `fs::absolute`."""
    (tmp_path / "bitcoin.conf").write_text("", encoding="utf-8")
    (tmp_path / "other.conf").write_text("", encoding="utf-8")
    # records a call and answers one, as a working directory
    calls: dict[str, str] = {}
    getcwd = functools.partial(calls.setdefault, "getcwd", str(tmp_path))
    monkeypatch.setattr(os, "getcwd", getcwd)
    with pytest.raises(ValueError, match=r"^Data directory"):
        cli.build_config([f"-datadir={tmp_path}", "-conf=other.conf"])
    assert calls == {}


_SYMLINK = pytest.mark.skipif(
    os.name == "nt", reason="a symbolic link needs a privilege"
)


@pytest.fixture
def paths(tmp_path: Path) -> Path:
    """`d` with a regtest `bitcoin.conf`, `b`, and `sym` to `real/inner`."""
    for directory in ("d/cdir", "d/incd", "b", "real/inner"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "d" / "bitcoin.conf").write_text("regtest=1\n", encoding="utf-8")
    # refused on Windows without the privilege, where `_SYMLINK` skips
    # every case that reads it
    with suppress(OSError):
        (tmp_path / "sym").symlink_to(tmp_path / "real" / "inner")
    return tmp_path


# `GetPathArg`, measured on `bitcoind` v31.1.0 over the same layout: each
# `..` is taken off the path before the file system is asked, so neither a
# missing directory nor a symbolic link before it changes where it lands
@pytest.mark.parametrize(
    "datadir",
    ["{x}/nosuch/../d", "{x}/d/", pytest.param("{x}/sym/../d", marks=_SYMLINK)],
)
def test_build_config_datadir_is_lexically_normal(paths: Path, datadir: str) -> None:
    """`-datadir` is the directory it names once its `..` are taken off."""
    config = cli.build_config([f"-datadir={datadir.format(x=paths)}"])
    assert config.chain.name == "regtest"
    assert config.data_dir == paths / "d" / "regtest"


@pytest.mark.parametrize("datadir", ["{x}/nosuch/", "{x}/d/../nosuch"])
def test_build_config_a_missing_datadir_is_named_as_given(
    paths: Path, datadir: str
) -> None:
    """`InitConfig`'s refusal names `-datadir` as written, not normalised."""
    datadir = datadir.format(x=paths)
    expected = re.escape(f'Specified data directory "{datadir}" does not exist.')
    with pytest.raises(ValueError, match=f"^{expected}$"):
        cli.build_config([f"-datadir={datadir}"])


@pytest.mark.parametrize(
    ("blocksdir", "under"),
    [("{x}/b/nosuch/..", "b"), pytest.param("{x}/sym/..", "", marks=_SYMLINK)],
)
def test_build_config_blocksdir_is_lexically_normal(
    paths: Path, blocksdir: str, under: str
) -> None:
    """`-blocksdir` too: `bitcoind` put `blocks` under `b` and beside `sym`."""
    argv = [f"-datadir={paths / 'd'}", f"-blocksdir={blocksdir.format(x=paths)}"]
    config = cli.build_config(argv)
    assert config.blocks_dir == paths / under / "regtest"
    before = cli._before_lock(argv)
    assert before.directories.blocks_dir == config.blocks_dir


@pytest.mark.parametrize("blocksdir", ["{x}/nosuch/x/..", "{x}/b/nosuch/"])
def test_build_config_a_missing_blocksdir_is_named_as_given(
    paths: Path, blocksdir: str
) -> None:
    """`bitcoind` v31.1.0 names `-blocksdir` as written, `..` and all."""
    blocksdir = blocksdir.format(x=paths)
    expected = re.escape(f'Specified blocks directory "{blocksdir}" does not exist.')
    with pytest.raises(ValueError, match=f"^{expected}$"):
        cli.build_config([f"-datadir={paths / 'd'}", f"-blocksdir={blocksdir}"])


@pytest.mark.parametrize(
    ("argv", "conf", "refusal"),
    [
        (["-conf=nosuch/../cdir"], "", 'Config file "{d}{s}cdir" is a directory.'),
        (
            ["-conf=missing.conf"],
            "",
            'specified config file "{d}{s}missing.conf" could not be opened.',
        ),
        (
            ["-conf=j.conf"],
            "regtest=1\nincludeconf=incd\n",
            'Included config file "{d}{s}incd" is a directory.',
        ),
        (
            ["-conf=j.conf"],
            "regtest=1\nincludeconf=inc/../nosuch.conf\n",
            "Failed to include configuration file inc/../nosuch.conf",
        ),
    ],
    ids=["a directory", "missing", "an included directory", "an included missing"],
)
def test_build_config_a_configuration_file_refusal_is_core_s(
    paths: Path, argv: list[str], conf: str, refusal: str
) -> None:
    """`ReadConfigFiles`'s words after `InitConfig`'s prefix, each measured."""
    data = paths / "d"
    (data / "j.conf").write_text(conf, encoding="utf-8")
    refusal = "Error reading configuration file: " + refusal.format(d=data, s=os.sep)
    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
        cli.build_config([f"-datadir={data}", *argv])


def test_build_config_a_datadir_that_normalises_to_dot_keeps_it_in_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-datadir=.` keeps its `.` when joined onto a missing `-conf` value.

    Measured on `bitcoind` v31.1.0: `-datadir=. -conf=missing.conf` names
    "<cwd>/./missing.conf", not "<cwd>/missing.conf" -- `fs::absolute`
    joins the `.` on literally, where `pathlib.Path` drops it
    (btclib-org/btclib-node#1273).
    """
    (tmp_path / "bitcoin.conf").write_text("regtest=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    refusal = (
        "Error reading configuration file: specified config file "
        f'"{tmp_path}{os.sep}.{os.sep}missing.conf" could not be opened.'
    )
    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
        cli.build_config(["-datadir=.", "-conf=missing.conf"])


def test_build_config_a_conf_that_normalises_to_dot_keeps_it_in_a_refusal(
    paths: Path,
) -> None:
    """`-conf=nosuch/..` is `.` once normal, kept joined onto `-datadir`.

    Measured on `bitcoind` v31.1.0: names "<X>/.", not "<X>" --
    `AbsPathForConfigVal`'s join is as literal as `fs::absolute`'s above
    (btclib-org/btclib-node#1273).
    """
    data = paths / "d"
    refusal = (
        f'Error reading configuration file: Config file "{data}{os.sep}." '
        "is a directory."
    )
    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
        cli.build_config([f"-datadir={data}", "-conf=nosuch/.."])


@pytest.mark.parametrize(
    "include", ["./confdir", "sub/./../confdir", "sub//../confdir"]
)
def test_build_config_an_includeconf_value_is_not_lexically_normalised(
    paths: Path, include: str
) -> None:
    """Core never normalises `includeconf`, unlike `-datadir` and `-conf`.

    Measured on `bitcoind` v31.1.0 with a directory `confdir` and a
    directory `sub` under the data directory: each of these three names
    the directory `includeconf=confdir` also names, and each is refused
    with its own value joined onto the data directory exactly as
    written, `.` segments and the repeated `/` both kept
    (btclib-org/btclib-node#1273, comment).
    """
    data = paths / "d"
    (data / "confdir").mkdir()
    (data / "sub").mkdir()
    (data / "j.conf").write_text(
        f"regtest=1\nincludeconf={include}\n", encoding="utf-8"
    )
    refusal = (
        "Error reading configuration file: Included config file "
        f'"{data}{os.sep}{include}" is a directory.'
    )
    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
        cli.build_config([f"-datadir={data}", "-conf=j.conf"])


def test_build_config_a_relative_datadir_is_refused_by_its_full_path(
    paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GetDataDir`'s `fs::absolute`: the working directory joins the path.

    Measured on `bitcoind` v31.1.0, which names the file in full.
    """
    monkeypatch.chdir(paths)
    missing = Path.cwd() / "d" / "missing.conf"
    refusal = (
        f'Error reading configuration file: specified config file "{missing}" '
        "could not be opened."
    )
    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
        cli.build_config(["-datadir=d", "-conf=missing.conf"])


_UNREADABLE = pytest.mark.skipif(
    os.name == "nt", reason="mode 000 does not stop a read on Windows"
)


@_UNREADABLE
@pytest.mark.parametrize(
    ("argv", "conf", "refusal"),
    [
        (
            ["-conf=other.conf"],
            "other.conf",
            'specified config file "{d}/other.conf" could not be opened.',
        ),
        (
            ["-conf=j.conf"],
            "inc.conf",
            "Failed to include configuration file inc.conf",
        ),
    ],
    ids=["-conf", "an include"],
)
def test_build_config_an_unreadable_configuration_file_is_refused_as_core_s(
    paths: Path, argv: list[str], conf: str, refusal: str
) -> None:
    """Core's `!stream.good()`: a file it may not read is one it cannot open.

    Measured on `bitcoind` v31.1.0 with the file at mode 000.
    """
    data = paths / "d"
    (data / "j.conf").write_text("regtest=1\nincludeconf=inc.conf\n", encoding="utf-8")
    (data / "other.conf").write_text("regtest=1\n", encoding="utf-8")
    (data / "inc.conf").write_text("port=1\n", encoding="utf-8")
    (data / conf).chmod(0)
    refusal = "Error reading configuration file: " + refusal.format(d=data)
    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
        cli.build_config([f"-datadir={data}", *argv])


@_UNREADABLE
def test_build_config_an_unreadable_default_file_is_left_unread(tmp_path: Path) -> None:
    """`bitcoind` v31.1.0 runs on mainnet past a `regtest=1` it cannot read."""
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("regtest=1\n", encoding="utf-8")
    conf.chmod(0)
    assert cli.build_config([f"-datadir={tmp_path}"]).chain.name == "mainnet"


@pytest.mark.skipif(os.name == "nt", reason="`//` starts a UNC path on Windows")
def test_build_config_names_a_leading_double_slash_as_one(paths: Path) -> None:
    """`lexically_normal` collapses `//`, `bitcoind` naming "/<X>/d/..."."""
    missing = paths / "d" / "missing.conf"
    refusal = (
        f'Error reading configuration file: specified config file "{missing}" '
        "could not be opened."
    )
    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
        cli.build_config([f"-datadir=/{paths}/d", "-conf=missing.conf"])
    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
        cli.build_config([f"-datadir={paths}/d", f"-conf=/{missing}"])


@pytest.mark.skipif(os.name == "nt", reason="`//` starts a UNC path on Windows")
def test_build_config_names_an_ignored_conf_s_double_slash_as_one(paths: Path) -> None:
    """`bitcoind` v31.1.0 names "/<X>/d" for `-datadir=//<X>/d` here too."""
    data = paths / "d"
    (data / "other.conf").write_text("regtest=1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f'^Data directory "{re.escape(str(data))}"'):
        cli.build_config([f"-datadir=/{data}", "-conf=other.conf"])


def test_build_config_conf_with_no_bitcoin_conf_beside_it(tmp_path: Path) -> None:
    """With no `bitcoin.conf` in the data directory, `-conf` refuses nothing."""
    (tmp_path / "other.conf").write_text("regtest=1\n", encoding="utf-8")
    config = cli.build_config([f"-datadir={tmp_path}", "-conf=other.conf"])
    assert config.chain.name == "regtest"


def test_build_config_cli_port_overrides_the_file(tmp_path: Path) -> None:
    """`-port` on the command line wins over the file's own value."""
    (tmp_path / "bitcoin.conf").write_text("port=1111\n", encoding="utf-8")
    config = cli.build_config([f"-datadir={tmp_path}", "-port=2222"])
    assert config.p2p_port == 2222


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["-rpcbind=0.0.0.0"], id="every interface"),
        pytest.param(["-rpcbind=127.0.0.1:9998"], id="a port of its own"),
        pytest.param(["-rpcbind=127.0.0.2", "-rpcbind=[::1]:9997"], id="several"),
    ],
)
def test_build_config_rpcbind_is_ignored_without_rpcallowip(argv: list[str]) -> None:
    """ISS 1211: Core binds `-rpcbind` only beside `-rpcallowip`.

    bitcoind v31.1.0 given each of these and no `-rpcallowip` listens on
    loopback at `-rpcport`, and logs a warning. The values are kept, for
    `RpcManager` to warn over.
    """
    config = cli.build_config(["-regtest", *argv, "-rpcport=9999"])
    assert config.rpc_host is None
    assert config.rpc_port == 9999
    assert config.rpcbind == tuple(value.removeprefix("-rpcbind=") for value in argv)


@pytest.mark.parametrize(
    ("argv", "conf", "values"),
    [
        (["-rpcbind=127.0.0.2"], "rpcbind=127.0.0.3\n", ("127.0.0.2", "127.0.0.3")),
        ([], "rpcbind=127.0.0.3\nrpcbind=127.0.0.4\n", ("127.0.0.3", "127.0.0.4")),
        (["-norpcbind"], "rpcbind=127.0.0.3\n", ()),
    ],
    ids=["the command line then the file", "the file's every value", "negated"],
)
def test_build_config_rpcbind_is_read_as_a_list(
    tmp_path: Path, argv: list[str], conf: str, values: tuple[str, ...]
) -> None:
    """ISS 1211: Core's `GetArgs("-rpcbind")`, every value from every level."""
    assert _build(tmp_path, *argv, conf=conf).rpcbind == values


@pytest.mark.parametrize(
    ("argv", "conf", "values"),
    [
        (
            ["-rpcallowip=10.0.0.0/8"],
            "rpcallowip=10.1.0.0/16\n",
            ("10.0.0.0/8", "10.1.0.0/16"),
        ),
        (["-norpcallowip"], "rpcallowip=10.1.0.0/16\n", ()),
    ],
    ids=["the command line then the file", "negated"],
)
def test_build_config_rpcallowip_is_read_as_a_list(
    tmp_path: Path, argv: list[str], conf: str, values: tuple[str, ...]
) -> None:
    """ISS 1268: Core's `GetArgs("-rpcallowip")`, not `NETWORK_ONLY`.

    So on regtest the file's default section still reaches it.
    """
    assert _build(tmp_path, "-regtest", *argv, conf=conf).rpcallowip == values


def test_build_config_every_rpcbind_value_is_checked() -> None:
    """ISS 1211: `CheckHostPortOptions` checks every value, bound or not.

    bitcoind v31.1.0 refuses `-rpcbind=1.2.3.4:0 -rpcbind=127.0.0.1` with
    "Invalid port specified in -rpcbind: '1.2.3.4:0'", where only the
    last value was read here.
    """
    argv = ["-regtest", "-rpcbind=1.2.3.4:0", "-rpcbind=127.0.0.1"]
    with pytest.raises(
        ValueError, match=re.escape("Invalid port specified in -rpcbind: '1.2.3.4:0'")
    ):
        cli.build_config(argv)


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


def test_build_config_discover_defaults_to_listen() -> None:
    """ISS 1330: no explicit `-discover` follows the resolved `-listen`."""
    assert cli.build_config(["-regtest", "-listen=1"]).discover is True
    assert cli.build_config(["-regtest", "-connect=10.0.0.1"]).discover is False


def test_build_config_nodiscover_forces_discover_false_under_listen() -> None:
    """`-nodiscover` wins over a `-listen=1` that would default it on."""
    config = cli.build_config(["-regtest", "-listen=1", "-nodiscover"])
    assert config.listen is True
    assert config.discover is False


def test_build_config_discover_wins_over_listen_0() -> None:
    """`-discover=1` under `-listen=0`: Core's own explicit-wins-soft-set."""
    config = cli.build_config(["-regtest", "-connect=10.0.0.1", "-discover=1"])
    assert config.listen is False
    assert config.discover is True


def test_build_config_bind_is_read_as_a_list_and_turns_listen_on() -> None:
    """ISS 1257: `-bind` soft-sets `-listen` on, ahead of `-connect`."""
    argv = ["-regtest", "-connect=10.0.0.1", "-bind=127.0.0.1", "-bind=[::1]:99"]
    config = cli.build_config(argv)
    assert config.bind == ("127.0.0.1", "[::1]:99")
    assert config.listen is True


def test_build_config_bind_is_empty_by_default() -> None:
    """ISS 1257: no `-bind` is every interface."""
    assert cli.build_config(["-regtest"]).bind == ()


def test_build_config_bind_beside_listen_0_is_refused() -> None:
    """ISS 1257: `AppInitParameterInteraction`'s refusal, in Core's words."""
    message = "Cannot set -bind or -whitebind together with -listen=0"
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        cli.build_config(["-regtest", "-bind=127.0.0.1", "-nolisten"])


@pytest.mark.parametrize("value", ["127.0.0.1:0", "127.0.0.1:x", "[::1]:", ":+1"])
@pytest.mark.parametrize("tag", ["", "=onion"])
def test_build_config_a_bind_with_no_port_is_refused(value: str, tag: str) -> None:
    """ISS 1257: `CheckHostPortOptions` reads the value without its tag."""
    message = f"Invalid port specified in -bind: '{value}{tag}'"
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        cli.build_config(["-regtest", f"-bind={value}{tag}"])


@pytest.mark.parametrize("value", ["localhost", "1.2.3.4=tor", "", "=onion"])
def test_build_config_a_bind_that_is_no_numeric_address_is_refused(value: str) -> None:
    """ISS 1257: `Lookup(addr, port, fAllowLookup=false)` answers nothing."""
    message = f"Cannot resolve -bind address: '{value}'"
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        cli.build_config(["-regtest", f"-bind={value}"])


@pytest.mark.parametrize(
    ("argv", "duplicate"),
    [
        (["-bind=1.2.3.4", "-bind=1.2.3.4:18444"], "1.2.3.4:18444"),
        (["-bind=[::1]:7", "-bind=[0::1]:7"], "[::1]:7"),
        (["-bind=1.2.3.4:18445=onion", "-bind=1.2.3.4=onion"], "1.2.3.4:18445"),
        (["-bind=1.2.3.4:18445", "-bind=1.2.3.4=onion"], "1.2.3.4:18445"),
    ],
)
def test_build_config_a_bind_named_twice_is_refused(
    argv: list[str], duplicate: str
) -> None:
    """ISS 1257: `CheckBindingConflicts`; `=onion` defaults to `-port` + 1."""
    message = (
        f"Duplicate binding configuration for address {duplicate}. Please check "
        "your -bind, -bind=...=onion and -whitebind settings."
    )
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        cli.build_config(["-regtest", *argv])


def test_build_config_a_bind_defaults_its_port_to_dash_port() -> None:
    """ISS 1257: `default_bind_port` is `-port`: these are one address."""
    with pytest.raises(ValueError, match="Duplicate binding configuration"):
        cli.build_config(["-regtest", "-port=9", "-bind=1.2.3.4:9", "-bind=1.2.3.4"])


def test_build_config_whitebind_is_a_list_and_turns_listen_on() -> None:
    """ISS 1625: `-whitebind` soft-sets `-listen` on, ahead of `-connect`."""
    argv = ["-regtest", "-connect=10.0.0.1", "-whitebind=noban@127.0.0.1:7"]
    config = cli.build_config([*argv, "-whitebind=[::1]:8"])
    assert config.whitebind == ("noban@127.0.0.1:7", "[::1]:8")
    assert config.listen is True
    assert cli.build_config(["-regtest"]).whitebind == ()


def test_build_config_whitebind_does_not_overrule_listen_0() -> None:
    """ISS 1625: the soft-set yields, and `-listen=0` is then refused."""
    message = "Cannot set -bind or -whitebind together with -listen=0"
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        cli.build_config(["-regtest", "-whitebind=127.0.0.1:7", "-nolisten"])


@pytest.mark.parametrize(
    "value", ["127.0.0.1:0", "noban@127.0.0.1:0", "[::1]:x", "noban@127.0.0.1:7,"]
)
def test_build_config_a_whitebind_with_a_bad_port_is_refused(value: str) -> None:
    """ISS 1625: `CheckHostPortOptions` reads the value with its permissions."""
    message = f"Invalid port specified in -whitebind: '{value}'"
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        cli.build_config(["-regtest", f"-whitebind={value}"])


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("127.0.0.1", "Need to specify a port with -whitebind: '127.0.0.1'"),
        ("localhost:7", "Cannot resolve -whitebind address: 'localhost:7'"),
        (
            "out@127.0.0.1:7",
            'whitebind may only be used for incoming connections ("out" was passed)',
        ),
        ("bogus@127.0.0.1:7", "Invalid P2P permission: 'bogus'"),
    ],
)
def test_build_config_a_whitebind_core_refuses_is_refused_in_its_words(
    value: str, message: str
) -> None:
    """ISS 1625: `NetWhitebindPermissions::TryParse`'s errors."""
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        cli.build_config(["-regtest", f"-whitebind={value}"])


@pytest.mark.parametrize(
    ("argv", "duplicate"),
    [
        (["-whitebind=1.2.3.4:7", "-whitebind=noban@1.2.3.4:7"], "1.2.3.4:7"),
        (["-whitebind=1.2.3.4:7", "-bind=1.2.3.4:7"], "1.2.3.4:7"),
        (["-whitebind=1.2.3.4:18445", "-bind=1.2.3.4=onion"], "1.2.3.4:18445"),
        # the default onion bind, which `-whitebind` alone leaves in place
        (["-whitebind=127.0.0.1:18445"], "127.0.0.1:18445"),
        (["-port=9", "-whitebind=127.0.0.1:10"], "127.0.0.1:10"),
    ],
)
def test_build_config_an_address_bound_twice_with_a_whitebind_is_refused(
    argv: list[str], duplicate: str
) -> None:
    """ISS 1625: `CheckBindingConflicts` sees the `-whitebind` ones first."""
    message = (
        f"Duplicate binding configuration for address {duplicate}. Please check "
        "your -bind, -bind=...=onion and -whitebind settings."
    )
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        cli.build_config(["-regtest", *argv])


def test_build_config_a_bind_beside_a_whitebind_adds_no_onion_bind() -> None:
    """ISS 1625: `AppInitMain` adds the onion bind where no `-bind` is."""
    argv = ["-regtest", "-bind=127.0.0.2:5", "-whitebind=127.0.0.1:18445"]
    assert cli.build_config(argv).whitebind == ("127.0.0.1:18445",)


def test_build_config_externalip_is_at_the_whitebind_listen_port() -> None:
    """ISS 1625: `GetListenPort` takes a `-whitebind` that grants no `noban`."""
    argv = ["-regtest", "-externalip=8.8.8.8", "-whitebind=noban@127.0.0.1:7"]
    assert cli.build_config([*argv, "-whitebind=127.0.0.1:77"]).externalip == (
        "8.8.8.8:77",
    )
    assert cli.build_config(argv).externalip == ("8.8.8.8:18444",)


def test_build_config_externalip_turns_discover_off() -> None:
    """ISS 1445: the soft-set yields to an explicit value."""
    config = cli.build_config(["-regtest", "-externalip=8.8.8.8"])
    assert config.externalip == ("8.8.8.8:18444",)
    assert config.discover is False
    wins = cli.build_config(["-regtest", "-externalip=8.8.8.8", "-discover=1"])
    assert wins.discover is True


def _bad_port_warning(option: str, port: int) -> str:
    return (
        f"Warning: {option} request to listen on port {port}. This port is "
        'considered "bad" and thus it is unlikely that any peer will connect '
        "to it. See doc/p2p-bad-ports.md for details and a full list.\n"
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["-port=22"], _bad_port_warning("-port", 22)),
        (["-port=8333"], ""),
        (["-bind=127.0.0.1:22"], _bad_port_warning("-bind", 22)),
        (["-port=22", "-bind=127.0.0.1"], _bad_port_warning("-bind", 22)),
        (["-port=22", "-bind=127.0.0.1:8333"], ""),
        (["-port=22", "-whitebind=127.0.0.1:8333"], ""),
        (["-bind=127.0.0.1:22=onion"], ""),
        (
            ["-bind=127.0.0.1:22", "-bind=127.0.0.2:25"],
            _bad_port_warning("-bind", 22) + _bad_port_warning("-bind", 25),
        ),
    ],
)
def test_build_config_warns_of_a_bad_port_as_core_does(
    argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """ISS 1645: each plain `-bind`, and `-port` only where no `-bind` is.

    `-whitebind` is never warned of, and ignores `-port` as `-bind` does.
    """
    cli.build_config(["-regtest", *argv])
    assert capsys.readouterr().err == expected


def test_build_config_externalip_is_a_list_at_dash_port() -> None:
    """ISS 1445: each value is looked up at `-port`; its own port wins."""
    argv = ["-regtest", "-port=99", "-externalip=8.8.8.8", "-externalip=[2001::1]:7"]
    assert cli.build_config(argv).externalip == ("8.8.8.8:99", "[2001::1]:7")


def test_build_config_externalip_looks_a_name_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1445: `Lookup` with `-dns`'s default, so a name is resolved here."""
    asked: list[bytes] = []

    def getaddrinfo(host: bytes, *_: object, **__: object) -> list[Any]:
        asked.append(host)
        return [(2, 1, 6, "", ("8.8.4.4", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    config = cli.build_config(["-regtest", "-externalip=node.example"])
    assert config.externalip == ("8.8.4.4:18444",)
    assert asked == [b"node.example"]


@pytest.mark.parametrize(
    "value",
    [
        "0.0.0.0",  # noqa: S104
        "::",
        "255.255.255.255",
        "2001:db8::1",
    ],
)
def test_build_config_an_invalid_externalip_is_refused(value: str) -> None:
    """ISS 1445: `Lookup` finds it, `CNetAddr::IsValid` refuses it."""
    message = f"Cannot resolve -externalip address: '{value}'"
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        cli.build_config(["-regtest", f"-externalip={value}"])


def test_build_config_an_externalip_that_resolves_to_nothing_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1445: Core's `ResolveErrMsg`, naming the value as given."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_, **__: [])
    message = "Cannot resolve -externalip address: 'nowhere.invalid'"
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        cli.build_config(["-regtest", "-externalip=nowhere.invalid"])


def test_build_config_peerblockfilters_defaults_to_false() -> None:
    """Core's own `DEFAULT_PEERBLOCKFILTERS`."""
    assert cli.build_config(["-regtest"]).peerblockfilters is False


def test_build_config_peerblockfilters_reads_the_flag() -> None:
    """`-peerblockfilters` given bare is Core's own bare boolean flag."""
    assert cli.build_config(["-regtest", "-peerblockfilters"]).peerblockfilters is True


def test_build_config_v2transport_defaults_to_true() -> None:
    """Core's own `DEFAULT_V2_TRANSPORT`."""
    assert cli.build_config(["-regtest"]).v2transport is True


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("-v2transport=0", False), ("-nov2transport", False), ("-v2transport", True)],
)
def test_build_config_v2transport_reads_the_flag(flag: str, expected: bool) -> None:  # noqa: FBT001
    """`-v2transport` is a boolean flag, `-nov2transport` its negation."""
    assert cli.build_config(["-regtest", flag]).v2transport is expected


def test_build_config_v1transport_defaults_to_false() -> None:
    """v1 is off unless asked for."""
    assert cli.build_config(["-regtest"]).v1transport is False


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("-v1transport=0", False), ("-nov1transport", False), ("-v1transport", True)],
)
def test_build_config_v1transport_reads_the_flag(flag: str, expected: bool) -> None:  # noqa: FBT001
    """`-v1transport` is a boolean flag, `-nov1transport` its negation."""
    assert cli.build_config(["-regtest", flag]).v1transport is expected


def test_build_config_v2transport_off_alone_leaves_v1_on() -> None:
    """`-v2transport=0` alone switches v1 on."""
    assert cli.build_config(["-regtest", "-v2transport=0"]).v1transport is True


def test_build_config_refuses_both_transports_off() -> None:
    """The refusal reaches `build_config`'s caller in its own words."""
    with pytest.raises(ValueError, match="Cannot set -v1transport to false"):
        cli.build_config(["-regtest", "-v2transport=0", "-v1transport=0"])


def test_help_names_v1transport() -> None:
    """The option is listed beside `-v2transport`, in Core's shape."""
    message = " ".join(cli._help_message(show_debug=False).split())
    assert "Support v1 transport (default: 0)" in message


def test_build_config_seednode_reaches_config() -> None:
    """`-seednode` on the command line resolves through to `Config`."""
    config = cli.build_config(
        ["-regtest", "-seednode=10.0.0.1", "-seednode=10.0.0.2:2"]
    )
    assert config.seednode == (("10.0.0.1", RegTest().port), ("10.0.0.2", 2))


def test_build_config_dnsseed_explicit_wins_over_connect(tmp_path: Path) -> None:
    """`-connect` soft-sets `-dnsseed=0`; an explicit `-dnsseed=1` wins."""
    off = _build(tmp_path, "-connect=10.0.0.1")
    assert off.dnsseed is False
    on = _build(tmp_path, "-connect=10.0.0.1", "-dnsseed=1")
    assert on.dnsseed is True


def test_build_config_nodnsseed_wins_with_no_connect(tmp_path: Path) -> None:
    """No `-connect`: `-dnsseed` defaults true, `-nodnsseed` overrides it."""
    assert _build(tmp_path).dnsseed is True
    assert _build(tmp_path, "-nodnsseed").dnsseed is False


def test_build_config_fixedseeds_defaults_to_true(tmp_path: Path) -> None:
    """Not given: `Config.fixed_seeds` is Core's own `DEFAULT_FIXEDSEEDS`."""
    assert _build(tmp_path).fixed_seeds is True


def test_build_config_fixedseeds_zero_reaches_config(tmp_path: Path) -> None:
    """`-fixedseeds=0` turns fixed seeds off."""
    assert _build(tmp_path, "-fixedseeds=0").fixed_seeds is False


def test_main_builds_a_node_and_starts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main` builds a `Node` from the parsed config, starts it and waits."""
    built: list[Any] = []

    class FakeNode:
        init_errors: tuple[str, ...] = ()

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


def test_main_a_node_that_failed_to_start_exits_one_with_its_init_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each of `Node.init_errors` is a line, in order, and the status is `1`.

    `bitcoind` v31.1.0's two lines for a taken P2P port, named for this node.
    """

    class FakeNode:
        init_errors = (
            (
                "Unable to bind to 0.0.0.0:8333 on this computer."
                " btclib-node is probably already running."
            ),
            "Failed to listen on any port. Use -listen=0 if you want this.",
        )

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
    assert capsys.readouterr().err == (
        "Error: Unable to bind to 0.0.0.0:8333 on this computer."
        " btclib-node is probably already running.\n"
        "Error: Failed to listen on any port. Use -listen=0 if you want this.\n"
    )


# Each row measured on `bitcoind` v31.1.0, a first instance running over
# the data directory: the options Core refuses after `AppInitLockDirectories`
# are answered with the lock, the others with their own refusal
_AFTER_THE_LOCK = [
    ["-port=0"],
    ["-rpcport=0"],
    ["-port=abc"],
    ["-rpcbind=1.2.3.4:0"],
    ["-rpcauth=bogus"],
    ["-rpccookieperms=bogus"],
    ["-port=0", "-rpcauth=bogus"],
    ["-rpcport=0", "-port=0"],
]
_BEFORE_THE_LOCK = [
    (["-prune=-1"], "Prune cannot be configured with a negative value."),
    (["-debug=bogus"], "Unsupported logging category -debug=bogus."),
    (["-maxconnections=-1"], "-maxconnections must be greater or equal than zero"),
    (
        ["-blocksdir={x}/nosuch"],
        'Specified blocks directory "{x}/nosuch" does not exist.',
    ),
    (["-prune=-1", "-port=0"], "Prune cannot be configured with a negative value."),
]


@pytest.mark.usefixtures("no_node")
@pytest.mark.parametrize("argv", _AFTER_THE_LOCK)
def test_main_a_held_directory_is_refused_before_these_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """`AppInitMain`'s refusals come after `AppInitLockDirectories`'s."""
    data_dir = tmp_path / "d"
    (data_dir / "regtest").mkdir(parents=True)
    with (
        held_by_another_process(data_dir / "regtest"),
        pytest.raises(SystemExit) as excinfo,
    ):
        cli.main([f"-datadir={data_dir}", "-regtest", *argv])
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == (
        f"Error: Cannot obtain a lock on directory {data_dir / 'regtest'}. "
        "btclib-node is probably already running.\n"
    )


@pytest.mark.usefixtures("no_node")
@pytest.mark.parametrize(("argv", "refusal"), _BEFORE_THE_LOCK)
def test_main_these_options_are_refused_before_a_held_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    refusal: str,
) -> None:
    """`AppInitParameterInteraction`'s refusals come before the lock."""
    data_dir = tmp_path / "d"
    (data_dir / "regtest").mkdir(parents=True)
    argv = [arg.format(x=tmp_path) for arg in argv]
    with (
        held_by_another_process(data_dir / "regtest"),
        pytest.raises(SystemExit) as excinfo,
    ):
        cli.main([f"-datadir={data_dir}", "-regtest", *argv])
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == f"Error: {refusal.format(x=tmp_path)}\n"


@pytest.mark.parametrize(
    ("argv", "refusal"),
    [
        (
            ["-blocksdir={x}/nosuch", "-maxconnections=-1"],
            'Specified blocks directory "{x}/nosuch" does not exist.',
        ),
        (
            ["-maxconnections=-1", "-debug=bogus"],
            "-maxconnections must be greater or equal than zero",
        ),
        (
            ["-debug=bogus", "-prune=-1"],
            "Unsupported logging category -debug=bogus.",
        ),
        (
            ["-rpcbind=1.2.3.4:0"],
            "Invalid port specified in -rpcbind: '1.2.3.4:0'",
        ),
        (
            ["-rpcbind=1.2.3.4:0", "-rpcport=0"],
            "Invalid port specified in -rpcport: '0'",
        ),
    ],
    ids=[
        "blocksdir, maxconnections",
        "maxconnections, debug",
        "debug, prune",
        "rpcbind",
        "rpcport, rpcbind",
    ],
)
def test_build_config_refuses_in_core_order(
    tmp_path: Path, argv: list[str], refusal: str
) -> None:
    """Two refusals in one command line: the one `bitcoind` names first.

    Each measured on `bitcoind` v31.1.0.
    """
    argv = [arg.format(x=tmp_path) for arg in argv]
    expected = re.escape(refusal.format(x=tmp_path))
    with pytest.raises(ValueError, match=f"^{expected}$"):
        cli.build_config([f"-datadir={tmp_path}", "-regtest", *argv])


def test_main_releases_its_lock_once_the_node_holds_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock `main` takes ahead of `Node` is not held past it.

    `FakeNode` takes no lock of its own, so while it runs another
    process is refused only where `main` still holds one.
    """
    answers: list[str] = []

    class FakeNode:
        init_errors: tuple[str, ...] = ()

        def __init__(self, config: Any) -> None:
            self.data_dir = config.data_dir

        def start(self) -> None:
            answers.append(lock_from_another_process(self.data_dir))

        def join(self) -> None:
            pass

    monkeypatch.setattr(cli, "Node", FakeNode)
    monkeypatch.setattr(cli, "install_signal_handlers", lambda _: None)
    cli.main([f"-datadir={tmp_path}", "-regtest"])
    assert answers == ["locked"]


@pytest.fixture
def no_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove `cli.Node`: an argument `main` does not refuse is a `NameError`.

    With `Node` in place such an argument starts a node that never stops.
    """
    monkeypatch.delattr(cli, "Node")


@pytest.mark.usefixtures("no_node")
def test_main_reads_an_include_of_the_chains_own_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ISS 1302: what it names is read, and refused as `bitcoind` refuses it."""
    (tmp_path / "bitcoin.conf").write_text(
        "regtest=1\n[regtest]\nincludeconf=inc.conf\n", encoding="utf-8"
    )
    (tmp_path / "inc.conf").write_text("maxconnections=-1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main([f"-datadir={tmp_path}"])
    assert capsys.readouterr().err == (
        "Error: -maxconnections must be greater or equal than zero\n"
    )


@pytest.mark.usefixtures("no_node")
@pytest.mark.parametrize(
    ("argv", "conf", "refusal"),
    [
        (
            ["-chain=bogus"],
            "includeconf=nosuch.conf\n",
            "Failed to include configuration file nosuch.conf",
        ),
        ([], "chain=bogus\nincludeconf=inc.conf\n", "parse error on line 1: bad"),
        (
            ["-chain=bogus"],
            "[bogus]\nincludeconf=inc.conf\n",
            "parse error on line 1: bad",
        ),
    ],
    ids=["missing include", "the file's chain", "the unknown chain's section"],
)
def test_main_reads_the_includes_of_a_chain_core_does_not_know(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    conf: str,
    refusal: str,
) -> None:
    """ISS 1302: `GetChainTypeString` names it; `GetChainType` refuses later.

    Measured on `bitcoind` v31.1.0 with these files: the include's own
    refusal, and not "Unknown chain bogus.", which it gives with no
    include at all.
    """
    (tmp_path / "bitcoin.conf").write_text(conf, encoding="utf-8")
    (tmp_path / "inc.conf").write_text("bad\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main([f"-datadir={tmp_path}", *argv])
    assert capsys.readouterr().err == (
        f"Error: Error reading configuration file: {refusal}\n"
    )


@pytest.mark.usefixtures("no_node")
def test_main_refuses_a_conflicting_chain_before_any_include(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ISS 1302: the chain `ReadConfigFiles` resolves first, with no prefix.

    `bitcoind` v31.1.0 refuses the combination, not the missing file,
    and without `InitConfig`'s "Error reading configuration file: ",
    the refusal being thrown rather than returned; its words are
    `bitcoind`'s own (btclib-org/btclib-node#1311).
    """
    (tmp_path / "bitcoin.conf").write_text(
        "regtest=1\nincludeconf=nosuch.conf\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        cli.main([f"-datadir={tmp_path}", "-testnet"])
    assert capsys.readouterr().err.startswith("Error: Invalid combination of ")


def test_config_options_records_every_section_as_core_does() -> None:
    """ISS 1271: `GetConfigOptions`' `sections`, a dotted key's included.

    A `[section]` line, and a key's part before its last `.` where that
    `.` sits at or past the `[section]` prefix's length: `regtest.x`
    under `[regtest]`, `regtest` for a top-level `regtest.port`, and
    `x.` for `.foo` under `[x]`, as `bitcoind` v31.1.0 names it, but not
    `x` again for `foo` under `[x]`.
    """
    sections: list[tuple[str, str, int]] = []
    text = "regtest.port=1\n[x]\nfoo=1\n.foo=1\n[regtest]\nx.bar=1\nbar=2\n"
    cli._config_options(text, sections, "f.conf")
    assert sections == [
        ("regtest", "f.conf", 1),
        ("x", "f.conf", 2),
        ("x.", "f.conf", 4),
        ("regtest", "f.conf", 5),
        ("regtest.x", "f.conf", 6),
    ]


def test_warn_unrecognized_sections_is_one_core_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ISS 1271: `InitWarning`'s lines, a chain's own section left out.

    `testnet4` is one of Core's chains, and so is not warned about.
    """
    settings = cli._Settings({})
    settings.config_sections = [
        ("x", "a.conf", 2),
        ("testnet4", "a.conf", 3),
        ("y", "b", 1),
    ]
    cli._warn_unrecognized_sections(settings)
    lines = (
        "a.conf:2 Section [x] is not recognized.\nb:1 Section [y] is not recognized.\n"
    )
    assert capsys.readouterr().err == f"Warning: {lines}\n"
    # ISS 1295: logged too, as `noui_ThreadSafeMessageBox` logs a warning
    assert settings.section_warning == lines
    settings = cli._Settings({})
    settings.config_sections = [("main", "a.conf", 1)]
    cli._warn_unrecognized_sections(settings)
    assert capsys.readouterr().err == ""
    assert settings.section_warning == ""


def test_unsuitable_section_only_args_is_core_s_own_check() -> None:
    """ISS 1327: `GetUnsuitableSectionOnlyArgs`, several options and a negation.

    `main`'s own default section is never asked, `m_network.empty()`'s
    early return applying here too; a negated default-section value is
    `SettingsSpan::empty()`, not `OnlyHasDefaultSectionSetting`'s
    concern; several unsuitable names come back in `_OPTIONS`'s own
    order, `connect` ahead of `port` there.
    """
    on_main = cli._Settings({}, ro_config={"": {"port": ["1"]}}, network="main")
    assert cli._unsuitable_section_only_args(on_main) == []

    negated = cli._Settings({}, ro_config={"": {"port": [False]}}, network="regtest")
    assert cli._unsuitable_section_only_args(negated) == []

    several = cli._Settings(
        {},
        ro_config={"": {"connect": ["10.0.0.1"], "port": ["1"]}},
        network="regtest",
    )
    assert cli._unsuitable_section_only_args(several) == ["connect", "port"]


def test_check_network_only_args_joins_one_line_per_option() -> None:
    """ISS 1327: Core's own words, one full sentence per option, joined."""
    settings = cli._Settings(
        {},
        ro_config={"": {"connect": ["10.0.0.1"], "port": ["1"]}},
        network="regtest",
    )
    message = (
        "Config setting for -connect only applied on regtest network when "
        "in [regtest] section.\n"
        "Config setting for -port only applied on regtest network when in "
        "[regtest] section.\n"
    )
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        cli._check_network_only_args(settings)


def test_get_setting_skips_a_network_only_default_section_value() -> None:
    """`GetSetting`'s own `ignore_default`, called directly for it.

    Core keeps this skip in `GetSetting` itself, independent of
    `GetUnsuitableSectionOnlyArgs`, and so does `_get_setting`: reached
    through `build_config`'s own pipeline, this exact input -- a
    single-valued `network_only` option set only in the default
    section, off `main` -- is always refused first by
    `_check_network_only_args`, which is what makes this line
    unreachable from there and worth calling directly
    (btclib-org/btclib-node#1327).
    """
    settings = cli._Settings({}, ro_config={"": {"port": ["1"]}}, network="regtest")
    assert cli._get_setting(settings, "port") is None


@pytest.mark.usefixtures("no_node")
def test_main_warns_of_an_unrecognized_section_before_refusing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ISS 1271: the warning, then the blocks directory, as `bitcoind` prints.

    Measured on `bitcoind` v31.1.0 with this file and include: the root
    file is named by its path, the included one as `includeconf=` names
    it.
    """
    (tmp_path / "bitcoin.conf").write_text(
        "regtest=1\nincludeconf=inc.conf\n[x]\n", encoding="utf-8"
    )
    (tmp_path / "inc.conf").write_text("[z]\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main([f"-datadir={tmp_path}", f"-blocksdir={tmp_path}/nosuch"])
    assert capsys.readouterr().err == (
        f"Warning: {tmp_path / 'bitcoin.conf'}:3 Section [x] is not recognized.\n"
        "inc.conf:1 Section [z] is not recognized.\n\n"
        f'Error: Specified blocks directory "{tmp_path}/nosuch" does not exist.\n'
    )


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


@pytest.mark.usefixtures("no_node")
def test_main_a_refusal_after_the_lock_reaches_history_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ISS 1306: `CheckHostPortOptions` fails after the log has started.

    `init::StartLogging` (`src/init.cpp:1436`, at bitcoin/bitcoin@9be056a8a7)
    runs ahead of `CheckHostPortOptions` (`:1525`, same file), so
    `bitcoind`'s own `debug.log` already holds a `[error]` line for this
    refusal, `LogError` reaching it through `noui_ThreadSafeMessageBox`
    the way it reaches stderr.
    """
    with pytest.raises(SystemExit) as excinfo:
        cli.main([f"-datadir={tmp_path}", "-port=abc"])
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == "Error: Invalid port specified in -port: 'abc'\n"
    log_text = (tmp_path / "mainnet" / "history.log").read_text(encoding="utf-8")
    assert "[error] Invalid port specified in -port: 'abc'" in log_text


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
    err_msg = (
        "^Error reading configuration file: "
        f"parse error on line {line}, using # in rpcpassword can be ambiguous "
        "and should be avoided$"
    )
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
    config = cli.build_config([f"-datadir={tmp_path}", "-rpccookieperms=x"])
    assert config.rpc_cookie_perms_error is not None
    assert config.rpc_cookie_perms_error.startswith("Invalid -rpccookieperms=x;")


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_main_makes_what_the_node_creates_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISS 1198: Core's `SetupEnvironment` umask, set by `main` first.

    Under a group- and world-readable umask, what is created after `main`
    is 0700 for a directory and 0600 for a file, as `bitcoind` v31.1.0
    leaves its own chain directory and `debug.log`.
    """
    os.umask(0o022)
    monkeypatch.setattr(cli, "_before_lock", _refused)
    with pytest.raises(SystemExit):
        cli.main([])
    directory = tmp_path / "chain"
    directory.mkdir()
    (directory / "history.log").write_text("")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "history.log").stat().st_mode) == 0o600


def _refused(argv: Sequence[str]) -> Any:
    """Stand in for `_before_lock`, refusing whatever it is given."""
    raise ValueError(argv)


def test_setup_environment_leaves_the_umask_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISS 1198: Core's `SetupEnvironment` sets no umask under `WIN32`.

    The calls are recorded rather than read back from the process, so
    the test answers the same on every platform; the POSIX call after
    it is the control.
    """
    calls: list[int] = []
    monkeypatch.setattr(os, "umask", calls.append)
    monkeypatch.setattr(sys, "platform", "win32")
    cli._setup_environment()
    monkeypatch.setattr(sys, "platform", "linux")
    cli._setup_environment()
    assert calls == [0o077]


@pytest.mark.parametrize(
    ("argv", "categories"),
    [
        (["-debug"], set()),
        (["-debug=1"], set()),
        (["-debug=all", "-debug=net"], set()),
        (["-debug=net"], {"net"}),
        (["-debug=net", "-debug=rpc"], {"net", "rpc"}),
        (["-debug=net", "-debug=none", "-debug=http"], {"http"}),
        (["-debug=none"], set()),
    ],
)
def test_build_config_keeps_the_categories_debug_names(
    tmp_path: Path, argv: list[str], categories: set[str]
) -> None:
    """ISS 1322: `-debug=net` selects `net` alone, `-debug` every category.

    Empty is every category where `debug` is on, as `Logger.log_debug` reads
    it.
    """
    assert _build(tmp_path, *argv).debug_categories == categories


def test_build_config_names_the_config_file_it_read(tmp_path: Path) -> None:
    """ISS 1444: `StartLogging`'s "Config file: <path>" for a file found."""
    config = _build(tmp_path)
    assert config.config_file_line == (
        f"Config file: {os.path.join(tmp_path, 'bitcoin.conf')}"  # noqa: PTH118
    )


def test_build_config_names_the_config_file_it_did_not_find(tmp_path: Path) -> None:
    """ISS 1444: the default file's absence is `(not found, skipping)`."""
    config = cli.build_config([f"-datadir={tmp_path}"])
    assert config.config_file_line == (
        f"Config file: {os.path.join(tmp_path, 'bitcoin.conf')} "  # noqa: PTH118
        "(not found, skipping)"
    )


def test_build_config_says_no_config_file_was_read_under_noconf(
    tmp_path: Path,
) -> None:
    """ISS 1444: `-noconf` is `Config file: <disabled>`."""
    config = cli.build_config([f"-datadir={tmp_path}", "-noconf"])
    assert config.config_file_line == "Config file: <disabled>"


def test_main_history_log_names_the_data_directory_and_config_file(
    tmp_path: Path,
) -> None:
    """ISS 1444: the lines `bitcoind`'s `debug.log` opens on, in its order.

    `init::StartLogging` (`src/init/common.cpp:107-142`, at
    bitcoin/bitcoin@9be056a8a7): "Default data directory", "Using data
    directory", "Config file:", then `LogArgs`'s lines. A refusal after the
    lock opens the same log, and is what reaches it here without a node.
    """
    with pytest.raises(SystemExit):
        cli.main([f"-datadir={tmp_path}", "-port=abc"])
    log_path = tmp_path / "mainnet" / "history.log"
    lines = [
        line.split(" ", 1)[1]
        for line in log_path.read_text(encoding="utf-8").splitlines()[5:]
    ]
    assert lines[1:4] == [
        f"Default data directory {default_data_dir()}",
        f"Using data directory {tmp_path / 'mainnet'}",
        (
            f"Config file: {os.path.join(tmp_path, 'bitcoin.conf')} "  # noqa: PTH118
            "(not found, skipping)"
        ),
    ]
    assert lines[4].startswith("Command-line arg: datadir=")


@pytest.mark.parametrize(
    ("argv", "debug", "categories", "excluded"),
    [
        (["-debug=1", "-debugexclude=net"], True, set(), {"net"}),
        (["-debug=net", "-debugexclude=rpc"], True, {"net"}, {"rpc"}),
        (["-debugexclude=net", "-debugexclude=http"], False, set(), {"net", "http"}),
        (["-debug", "-debugexclude=all"], True, set(), set(cli._LOG_CATEGORIES)),
        (["-debug", "-debugexclude=1"], True, set(), set(cli._LOG_CATEGORIES)),
        (["-debug=net", "-debugexclude=net"], True, {"net"}, {"net"}),
    ],
)
def test_build_config_keeps_the_categories_debugexclude_names(
    tmp_path: Path,
    argv: list[str],
    categories: set[str],
    excluded: set[str],
    *,
    debug: bool,
) -> None:
    """ISS 1609: `SetLoggingCategories` removes them after `-debug`'s."""
    config = _build(tmp_path, *argv)
    assert config.debug is debug
    assert config.debug_categories == categories
    assert config.debug_exclude == excluded


@pytest.mark.parametrize(
    ("argv", "expected"),
    [([], True), (["-logratelimit"], True), (["-nologratelimit"], False)],
)
def test_build_config_reads_logratelimit(
    tmp_path: Path, argv: list[str], *, expected: bool
) -> None:
    """`-logratelimit` is on unless `-nologratelimit` (Core's default)."""
    assert _build(tmp_path, *argv).log_rate_limit is expected


@pytest.mark.parametrize("category", ["bogus", "0", "none"])
def test_build_config_refuses_a_debugexclude_category_core_does_not_know(
    tmp_path: Path, category: str
) -> None:
    """ISS 1609: Core's message, `GetLogCategory` knowing no `0` or `none`."""
    expected = re.escape(f"Unsupported logging category -debugexclude={category}.")
    with pytest.raises(ValueError, match=f"^{expected}$"):
        _build(tmp_path, f"-debugexclude={category}")


def test_help_names_debugexclude_under_debug_alone() -> None:
    """ISS 1609: `-debugexclude` is a `DEBUG_TEST` option, as `-debug` is."""
    assert "-debugexclude=<category>" in cli._help_message(show_debug=True)


@pytest.mark.parametrize(
    ("argv", "conf", "values"),
    [
        (
            ["-whitelist=noban@10.0.0.0/8"],
            "whitelist=1.2.3.4\n",
            ("noban@10.0.0.0/8", "1.2.3.4"),
        ),
        (["-nowhitelist"], "whitelist=1.2.3.4\n", ()),
    ],
    ids=["the command line then the file", "negated"],
)
def test_build_config_whitelist_is_read_as_a_list(
    tmp_path: Path, argv: list[str], conf: str, values: tuple[str, ...]
) -> None:
    """ISS 1320: Core's `GetArgs("-whitelist")`, not `NETWORK_ONLY`."""
    assert _build(tmp_path, "-regtest", *argv, conf=conf).whitelist == values


@pytest.mark.parametrize(
    ("argv", "relay", "force_relay"),
    [
        ([], True, False),
        (["-nowhitelistrelay"], False, False),
        (["-whitelistforcerelay"], True, True),
        (["-whitelistrelay=0", "-whitelistforcerelay=1"], False, True),
    ],
)
def test_build_config_whitelistrelay_and_whitelistforcerelay_default_as_core_does(
    tmp_path: Path, argv: list[str], *, relay: bool, force_relay: bool
) -> None:
    """ISS 1320: relay is on by default, and force relay off."""
    config = _build(tmp_path, "-regtest", *argv)
    assert (config.whitelist_relay, config.whitelist_force_relay) == (
        relay,
        force_relay,
    )


def test_help_names_the_whitelist_options() -> None:
    """ISS 1320: Core's words, cut to what this node does."""
    message = " ".join(cli._help_message(show_debug=False).split())
    assert (
        "-whitelist=<[permissions@]IP address or network> Add permission "
        "flags to the peers using the given IP address (e.g. 1.2.3.4) or "
        "CIDR-notated network (e.g. 1.2.3.0/24). Allowed permissions: "
        "bloomfilter (accepted, does nothing: no BIP37), noban (do not ban "
        "for misbehavior; implies download), forcerelay (relay "
        "transactions that are already in the mempool; implies relay), "
        "relay (unlimited transaction announcements), mempool (accepted, "
        "does nothing: no BIP35), download (allow getheaders during IBD), "
        "addr (responses to GETADDR avoid hitting the cache and contain "
        "random "
        "records with the most up-to-date info). Specify multiple "
        "permissions separated by commas (default: "
        "download,noban,mempool,relay). "
        'Additional flags "in" and "out" control whether '
        "permissions apply to incoming connections and/or manual (default: "
        "incoming only). Can be specified multiple times." in message
    )
    assert (
        "-whitelistforcerelay Add 'forcerelay' permission to whitelisted "
        "peers with default permissions. This will relay transactions even "
        "if the transactions were already in the mempool. (default: 0)" in message
    )
    assert (
        "-whitelistrelay Add 'relay' permission to whitelisted peers with "
        "default permissions. This lifts the limit on their transaction "
        "announcements (default: 1)" in message
    )


# ISS 1523: `settings.json`

_SETTINGS_WARNING = (
    f"This file is automatically generated and updated by {CLIENT_NAME}. "
    "Please do not edit this file while the node is running, as any changes "
    "might be ignored or overwritten."
)


def _settings(tmp_path: Path, content: str | None = None) -> Path:
    """Return the regtest `settings.json` in `tmp_path`, holding `content`."""
    path = tmp_path / "regtest" / "settings.json"
    if content is not None:
        path.parent.mkdir(exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return path


def _read(tmp_path: Path, *argv: str, conf: str = "") -> cli._Settings:
    """Read `argv` over `tmp_path`, as `_read_settings` does."""
    (tmp_path / "bitcoin.conf").write_text(conf, encoding="utf-8")
    return cli._read_settings([f"-datadir={tmp_path}", "-regtest", *argv])[0]


def test_a_start_writes_the_settings_file_under_the_warning(tmp_path: Path) -> None:
    """`bitcoind` v31.1.0 writes `_warning_` alone at a first start."""
    _read(tmp_path)
    assert _settings(tmp_path).read_bytes().decode("utf-8") == (
        f'{{\n    "_warning_": "{_SETTINGS_WARNING}"\n}}\n'.replace("\n", os.linesep)
    )


def test_a_start_writes_a_file_without_the_warning_back_with_it(
    tmp_path: Path,
) -> None:
    """Every init writes the file, keys in order, `_warning_` first."""
    _settings(tmp_path, '{"b":1,"a":"x"}')
    _read(tmp_path)
    assert _settings(tmp_path).read_bytes().decode("utf-8") == (
        f'{{\n    "_warning_": "{_SETTINGS_WARNING}",\n    "a": "x",\n    "b": 1\n}}\n'
    ).replace("\n", os.linesep)


def test_nosettings_reads_and_writes_nothing(tmp_path: Path) -> None:
    """A malformed file does not refuse the start, and is not touched."""
    _settings(tmp_path, "invalid json")
    settings = _read(tmp_path, "-nosettings")
    assert settings.rw_settings == {}
    assert _settings(tmp_path).read_text(encoding="utf-8") == "invalid json"


def test_nosettings_in_bitcoin_conf_is_read_too(tmp_path: Path) -> None:
    """Core's functional test puts `nosettings=1` in the chain's section."""
    _settings(tmp_path, "invalid json")
    settings = _read(tmp_path, conf="[regtest]\nnosettings=1\n")
    assert cli._log_args(settings)[0] == "Config file arg: [regtest] settings=false"
    assert _settings(tmp_path).read_text(encoding="utf-8") == "invalid json"


def test_nosettings_still_creates_the_directory_without_wallets(
    tmp_path: Path,
) -> None:
    """`InitConfig` makes the chain's directory before it asks for the file.

    It also makes `wallets` there, which this node, having no wallet, does not.
    """
    _read(tmp_path, "-nosettings")
    assert list((tmp_path / "regtest").iterdir()) == []


@pytest.mark.parametrize("relative", [True, False])
def test_settings_names_another_file(tmp_path: Path, *, relative: bool) -> None:
    """A relative path is the chain's directory's, an absolute one stands."""
    name = "other.json" if relative else str(tmp_path / "abs.json")
    other = tmp_path / ("regtest" if relative else ".") / Path(name).name
    (tmp_path / "regtest").mkdir()
    other.write_text('{"key":"value"}', encoding="utf-8")
    settings = _read(tmp_path, f"-settings={name}")
    assert settings.rw_settings == {"key": "value"}
    assert not _settings(tmp_path).exists()
    assert "_warning_" in other.read_text(encoding="utf-8")


def test_an_empty_settings_is_the_default_file(tmp_path: Path) -> None:
    """`-settings=` names nothing, and `GetPathArg` takes the default."""
    _read(tmp_path, "-settings=")
    assert _settings(tmp_path).exists()


@pytest.mark.parametrize(
    ("content", "detail"),
    [
        ("invalid json", "Settings file {p} does not contain valid JSON."),
        ('"string"', 'Found non-object value "string" in settings file {p}'),
        ('{"key": 1, "key": 2}', "Found duplicate key key in settings file {p}"),
    ],
)
def test_a_malformed_file_refuses_the_start(
    tmp_path: Path, content: str, detail: str
) -> None:
    """Core's words, and the file is left as it was."""
    path = _settings(tmp_path, content)
    with pytest.raises(ValueError, match=r".") as refused:
        _read(tmp_path)
    assert str(refused.value).startswith(
        "Settings file could not be read:\n- " + detail.format(p=path)
    )
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.usefixtures("no_node")
def test_main_prints_a_malformed_file_as_core_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`bitcoind` v31.1.0 prints the refusal and its detail on stderr."""
    _settings(tmp_path, '{"key": 1, "key": 2}')
    with pytest.raises(SystemExit) as exit_:
        cli.main([f"-datadir={tmp_path}", "-regtest"])
    assert exit_.value.code == 1
    assert capsys.readouterr().err == (
        "Error: Settings file could not be read:\n"
        f"- Found duplicate key key in settings file {_settings(tmp_path)}\n"
    )


def test_a_file_that_cannot_be_written_refuses_the_start(tmp_path: Path) -> None:
    """`bitcoind` v31.1.0 for `-settings=sub/s.json` with no `sub`."""
    with pytest.raises(ValueError, match=r".") as refused:
        _read(tmp_path, "-settings=sub/s.json")
    assert str(refused.value) == (
        "Settings file could not be written:\n- Error: Unable to open settings "
        f"file {tmp_path / 'regtest' / 'sub' / 's.json'}.tmp for writing"
    )


def test_a_chain_directory_that_cannot_be_made_refuses_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python's words for what `create_directories` throws."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", refuse)
    with pytest.raises(ValueError, match="Permission denied"):
        _read(tmp_path)


def test_the_settings_file_is_written_ahead_of_help_and_a_stray_token(
    tmp_path: Path,
) -> None:
    """`InitConfig` runs before both, as `bitcoind` v31.1.0 does."""
    with pytest.raises(SystemExit):
        _read(tmp_path, "-help")
    assert _settings(tmp_path).exists()
    _settings(tmp_path).unlink()
    with pytest.raises(ValueError, match="unexpected token"):
        _read(tmp_path, "token")
    assert _settings(tmp_path).exists()


def test_a_settings_key_moves_the_file_the_write_goes_to(tmp_path: Path) -> None:
    """`WriteSettingsFile` reads `-settings` again, and the file now sets it."""
    _settings(tmp_path, '{"settings":"other.json"}')
    _read(tmp_path)
    assert (tmp_path / "regtest" / "other.json").exists()
    assert (
        _settings(tmp_path).read_text(encoding="utf-8") == '{"settings":"other.json"}'
    )


def test_a_false_settings_key_refuses_the_write(tmp_path: Path) -> None:
    """`bitcoind` v31.1.0: the `logic_error` `WriteSettingsFile` throws."""
    _settings(tmp_path, '{"settings":false}')
    with pytest.raises(
        ValueError, match=r"^Attempt to write settings file when dynamic settings"
    ):
        _read(tmp_path)


def test_an_array_settings_key_is_refused_as_core_refuses_it(tmp_path: Path) -> None:
    """`Error: JSON value of type array is not of expected type string`."""
    _settings(tmp_path, '{"settings":["a.json"]}')
    with pytest.raises(ValueError, match="type array is not of expected type string"):
        _read(tmp_path)


def test_a_chain_selector_in_the_file_is_refused_beside_the_command_line_one(
    tmp_path: Path,
) -> None:
    """`GetChainType` is asked again once the file is read, as Core does."""
    _settings(tmp_path, '{"testnet":true}')
    with pytest.raises(ValueError, match=r"^Invalid combination of -regtest, "):
        _read(tmp_path)


def test_a_chain_selector_in_the_file_is_refused_as_a_number(tmp_path: Path) -> None:
    """`GetChainArg` reads `value.get_str()`, which a number is not."""
    _settings(tmp_path, '{"signet":1}')
    with pytest.raises(ValueError, match="type number is not of expected type string"):
        _read(tmp_path)


def test_a_negated_chain_selector_in_the_file_is_skipped(tmp_path: Path) -> None:
    """`GetSetting` skips a negated chain selector at every level.

    `bitcoind` v31.1.0 refuses `regtest=1` in `bitcoin.conf` beside
    `{"regtest": false, "signet": true}` in `settings.json`: the file's
    negation does not hide the conf's `regtest`.
    """
    _settings(tmp_path, '{"regtest":false,"signet":true}')
    (tmp_path / "bitcoin.conf").write_text("regtest=1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^Invalid combination of -regtest, "):
        cli._read_settings([f"-datadir={tmp_path}"])


def test_the_values_are_logged_and_the_unknown_names_warned_of(
    tmp_path: Path,
) -> None:
    """Core's own `_VALUES`, `LogArgs`' "Setting file arg:" in key order."""
    _settings(
        tmp_path,
        '{"string":"string","num":5,"bool":true,"null":null,"list":[6,7],'
        '"nolisten":true,"regtest.port":5,"rpcuser":"x","nozzz":1}',
    )
    settings = _read(tmp_path, conf="rpcuser=conf\n")
    assert settings.log_warnings == [
        "Ignoring unknown rw_settings value bool",
        "Ignoring unknown rw_settings value list",
        "Ignoring unknown rw_settings value nozzz",
        "Ignoring unknown rw_settings value null",
        "Ignoring unknown rw_settings value num",
        "Ignoring unknown rw_settings value string",
    ]
    lines = cli._log_args(settings)
    assert lines[:2] == (
        "Config file arg: rpcuser=****",
        "Setting file arg: bool = true",
    )
    assert lines[2:11] == (
        "Setting file arg: list = [6,7]",
        "Setting file arg: nolisten = true",
        "Setting file arg: nozzz = 1",
        "Setting file arg: null = null",
        "Setting file arg: num = 5",
        "Setting file arg: regtest.port = 5",
        'Setting file arg: rpcuser = "x"',
        'Setting file arg: string = "string"',
        f"Command-line arg: datadir={cli._setting_to_write_str(str(tmp_path))}",
    )


def test_the_help_names_settings_as_core_does() -> None:
    """`bitcoind` v31.1.0's `-help` text for `-settings`."""
    message = " ".join(cli._help_message(show_debug=False).split())
    assert (
        "-settings=<file> Specify path to dynamic settings data file. Can be "
        "disabled with -nosettings. File is written at runtime and not meant "
        "to be edited by users (use bitcoin.conf instead for custom "
        "settings). Relative paths will be prefixed by datadir location. "
        "(default: settings.json)" in message
    )


def _file_settings(content: str, *argv: str, conf: str = "") -> cli._Settings:
    """Return the settings of a command line, a file and a `bitcoin.conf`."""
    options, _ = cli._parse_parameters(argv, [])
    warnings: list[str] = []
    settings = cli._Settings(
        options,
        cli._parse_conf_text(conf, warnings=warnings),
        network="regtest",
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        path.write_text(content, encoding="utf-8")
        settings.rw_settings = read_settings(str(path))
    return settings


def test_a_value_of_the_file_is_between_the_command_line_and_the_conf() -> None:
    """`MergeSettings`: forced, command line, `settings.json`, the conf."""
    conf = "rpcuser=conf\n"
    file = '{"rpcuser":"file"}'
    assert cli._get_arg(_file_settings(file, conf=conf), "rpcuser") == "file"
    assert (
        cli._get_arg(_file_settings(file, "-rpcuser=cli", conf=conf), "rpcuser")
        == "cli"
    )
    assert cli._get_arg(_file_settings("{}", conf=conf), "rpcuser") == "conf"


def test_a_false_in_the_file_negates_the_conf() -> None:
    """`bitcoind` v31.1.0: `{"blocksonly": false}` beats `blocksonly=1`."""
    settings = _file_settings('{"listen":false}', conf="listen=1\n")
    assert cli._get_bool(settings, "listen") is False
    assert cli._is_negated(settings, "listen")


def test_the_values_of_a_list_option_are_the_command_line_the_file_the_conf() -> None:
    """`bitcoind` v31.1.0: `(cli; rw1; rw2; conf)`; an array is its values."""
    settings = _file_settings(
        '{"rpcwhitelist":["rw1","rw2"]}',
        "-rpcwhitelist=cli",
        conf="rpcwhitelist=conf\n",
    )
    assert cli._get_args(settings, "rpcwhitelist") == ["cli", "rw1", "rw2", "conf"]


def test_a_value_of_the_file_is_not_a_zombie() -> None:
    """`bitcoind` v31.1.0: a negation brings the conf back, not the file."""
    settings = _file_settings(
        '{"rpcwhitelist":"rw"}',
        "-norpcwhitelist",
        "-rpcwhitelist=cli",
        conf="rpcwhitelist=conf\n",
    )
    assert cli._get_args(settings, "rpcwhitelist") == ["cli", "conf"]


def test_a_negation_on_the_command_line_hides_the_file_and_the_conf() -> None:
    """`bitcoind` v31.1.0: `-nouacomment` leaves neither."""
    settings = _file_settings(
        '{"rpcwhitelist":"rw"}', "-norpcwhitelist", conf="rpcwhitelist=conf\n"
    )
    assert cli._get_args(settings, "rpcwhitelist") == []


def test_a_false_in_the_file_hides_the_conf_in_a_list() -> None:
    """`bitcoind` v31.1.0: `{"uacomment": false}` with `uacomment=conf`."""
    settings = _file_settings('{"rpcwhitelist":false}', conf="rpcwhitelist=conf\n")
    assert cli._get_args(settings, "rpcwhitelist") == []


def test_a_null_is_no_value_and_hides_the_conf() -> None:
    """One value reads it as unset, as `bitcoind` v31.1.0 reads `settings`."""
    settings = _file_settings('{"rpcuser":null}', conf="rpcuser=conf\n")
    assert cli._get_arg(settings, "rpcuser") is None
    assert not cli._is_set(settings, "rpcuser")


@pytest.mark.parametrize(
    ("name", "content", "reader"),
    [
        ("listen", "1", cli._get_bool),
        ("listen", "{}", cli._get_bool),
        ("listen", "[]", cli._get_bool),
        ("rpcuser", "{}", cli._get_arg),
        ("rpcuser", "[]", cli._get_arg),
        ("maxconnections", "{}", cli._get_int),
        ("maxconnections", "[1]", cli._get_int),
        ("connect", "5", cli._get_args),
        ("connect", "null", cli._get_args),
        ("connect", "{}", cli._get_args),
        ("connect", "[5]", cli._get_args),
        ("connect", "[null]", cli._get_args),
    ],
)
def test_a_json_type_an_option_cannot_read_is_refused(
    name: str, content: str, reader: object
) -> None:
    """`bitcoind` v31.1.0 throws `JSON value of type <type> is not ...`."""
    settings = _file_settings(f'{{"{name}":{content}}}')
    with pytest.raises(ValueError, match="is not of expected type string"):
        reader(settings, name)  # type: ignore[operator]


def test_a_status_that_cannot_be_read_is_refused_as_the_library_words_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fs::exists` throws; `InitConfig` shows the exception's text alone."""

    def refuse(_path: str) -> dict[str, object]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli, "read_settings", refuse)
    path = tmp_path / "regtest" / "settings.json"
    with pytest.raises(
        ValueError, match=r"^filesystem error: cannot get file status: "
    ) as refused:
        _read(tmp_path)
    assert str(refused.value) == (
        f"filesystem error: cannot get file status: Permission denied [{path}]"
    )


def test_get_args_refuses_the_first_value_it_cannot_read() -> None:
    """`GetArgs` throws in list order: `{}` ahead of `5` is an object."""
    settings = _file_settings('{"connect":[{},5]}')
    with pytest.raises(ValueError, match="type object is not of expected type string"):
        cli._get_args(settings, "connect")


@pytest.mark.parametrize(
    ("content", "expected"),
    [("12", 12), ('"12"', 12), ("-0", 0), ("9223372036854775807", 2**63 - 1)],
)
def test_a_number_is_an_integer_option(content: str, expected: int) -> None:
    """`bitcoind` v31.1.0: `{"maxconnections": 12}` and `"12"` alike."""
    settings = _file_settings(f'{{"maxconnections":{content}}}')
    assert cli._get_int(settings, "maxconnections") == expected


@pytest.mark.parametrize(
    "content",
    ["1.5", "1e1", "12.0", "9223372036854775808", "-9223372036854775809"],
)
def test_a_number_that_is_no_int64_is_out_of_range(content: str) -> None:
    """`bitcoind` v31.1.0: `JSON integer out of range`."""
    settings = _file_settings(f'{{"maxconnections":{content}}}')
    with pytest.raises(ValueError, match=r"^JSON integer out of range$"):
        cli._get_int(settings, "maxconnections")


def test_a_number_is_a_string_option_in_its_own_text() -> None:
    """`bitcoind` v31.1.0 names the file `5` for `{"settings": 5}`."""
    assert cli._get_arg(_file_settings('{"rpcuser":1.50}'), "rpcuser") == "1.50"


def test_a_value_of_the_file_is_another_source_of_a_network_only_option() -> None:
    """`OnlyHasDefaultSectionSetting`: the file is a source beside the conf."""
    conf = "port=1234\n"
    assert cli._unsuitable_section_only_args(_file_settings("{}", conf=conf)) == [
        "port"
    ]
    file = '{"port":5}'
    assert cli._unsuitable_section_only_args(_file_settings(file, conf=conf)) == []
