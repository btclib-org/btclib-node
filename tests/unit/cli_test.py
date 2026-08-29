# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""`cli.py`: argument parsing, `bitcoin.conf` reading, and `main`'s dispatch."""

import argparse
import runpy
from typing import TYPE_CHECKING, Any

import pytest

from btclib_node import cli
from btclib_node.chains import RegTest

if TYPE_CHECKING:
    from pathlib import Path


def test_parse_conf_text_reads_a_key_value_pair_in_the_default_section() -> None:
    """A bare `key=value` line lands in the `None` (default) section."""
    tree = cli._parse_conf_text("port=9000\n", "conf")
    assert tree == {None: {"port": ["9000"]}}


def test_parse_conf_text_reads_a_section() -> None:
    """A `[section]` line switches which section later lines belong to."""
    tree = cli._parse_conf_text("[regtest]\nport=9000\n", "conf")
    assert tree == {None: {}, "regtest": {"port": ["9000"]}}


def test_parse_conf_text_strips_a_trailing_comment() -> None:
    """`#` starts a comment that runs to the end of the line."""
    tree = cli._parse_conf_text("port=9000 # the p2p port\n", "conf")
    assert tree == {None: {"port": ["9000"]}}


def test_parse_conf_text_skips_blank_and_comment_only_lines() -> None:
    """A blank line and a comment-only line contribute nothing."""
    tree = cli._parse_conf_text("\n# a comment\n   \nport=9000\n", "conf")
    assert tree == {None: {"port": ["9000"]}}


def test_parse_conf_text_collects_repeated_keys_in_order() -> None:
    """Every occurrence of one key is kept, in the order it was read."""
    tree = cli._parse_conf_text("addnode=1.2.3.4\naddnode=5.6.7.8\n", "conf")
    assert tree[None]["addnode"] == ["1.2.3.4", "5.6.7.8"]


def test_parse_conf_text_rejects_a_leading_dash() -> None:
    """A line starting with `-` is refused: no leading `-` in a file."""
    with pytest.raises(ValueError, match="leading -"):
        cli._parse_conf_text("-port=9000\n", "conf")


def test_parse_conf_text_rejects_a_line_with_no_equals_sign() -> None:
    """A line matching neither `[section]` nor `key=value` is refused."""
    with pytest.raises(ValueError, match="not a key=value line"):
        cli._parse_conf_text("nonsense\n", "conf")


def test_parse_conf_text_rejects_conf_inside_a_file() -> None:
    """`conf=` cannot be set in a configuration file, the same as Core."""
    with pytest.raises(ValueError, match="conf cannot be set"):
        cli._parse_conf_text("conf=other.conf\n", "conf")


def test_read_conf_file_missing_and_not_required_is_empty(tmp_path: Path) -> None:
    """A missing default-named file is not an error: an empty tree."""
    assert cli._read_conf_file(tmp_path / "bitcoin.conf", required=False) == {None: {}}


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
    assert cli._read_conf_file(conf, required=False) == {None: {"port": ["9000"]}}


def test_load_conf_tree_with_no_includeconf_is_the_root_alone(tmp_path: Path) -> None:
    """No `includeconf`: the tree is exactly the root file's own."""
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("port=9000\n", encoding="utf-8")
    tree = cli._load_conf_tree(conf, conf_explicit=False, base_dir=tmp_path)
    assert tree == {None: {"port": ["9000"]}}


def test_load_conf_tree_follows_a_relative_includeconf(tmp_path: Path) -> None:
    """`includeconf=<file>` is resolved against `base_dir` when relative."""
    (tmp_path / "secrets.conf").write_text("rpcport=9001\n", encoding="utf-8")
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("includeconf=secrets.conf\nport=9000\n", encoding="utf-8")
    tree = cli._load_conf_tree(conf, conf_explicit=False, base_dir=tmp_path)
    assert tree[None]["port"] == ["9000"]
    assert tree[None]["rpcport"] == ["9001"]


def test_load_conf_tree_follows_an_absolute_includeconf(tmp_path: Path) -> None:
    """An absolute `includeconf=<file>` is read as-is, not joined to `base_dir`.

    Not through `base_dir` at all: `Path.is_absolute()` short-circuits
    the join `_load_conf_tree` otherwise does for a relative name.
    """
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    (other_dir / "secrets.conf").write_text("rpcport=9001\n", encoding="utf-8")
    conf = tmp_path / "bitcoin.conf"
    conf.write_text(f"includeconf={other_dir / 'secrets.conf'}\n", encoding="utf-8")
    tree = cli._load_conf_tree(conf, conf_explicit=False, base_dir=tmp_path)
    assert tree[None]["rpcport"] == ["9001"]


def test_load_conf_tree_merges_an_included_files_own_section(tmp_path: Path) -> None:
    """A section inside an included file lands in the merged tree too."""
    (tmp_path / "secrets.conf").write_text("[regtest]\nport=9000\n", encoding="utf-8")
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("includeconf=secrets.conf\n", encoding="utf-8")
    tree = cli._load_conf_tree(conf, conf_explicit=False, base_dir=tmp_path)
    assert tree["regtest"]["port"] == ["9000"]


def test_load_conf_tree_ignores_a_nested_includeconf(tmp_path: Path) -> None:
    """An `includeconf` inside an included file is not followed."""
    (tmp_path / "third.conf").write_text("rpcport=9002\n", encoding="utf-8")
    (tmp_path / "secrets.conf").write_text(
        "includeconf=third.conf\nrpcport=9001\n", encoding="utf-8"
    )
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("includeconf=secrets.conf\n", encoding="utf-8")
    tree = cli._load_conf_tree(conf, conf_explicit=False, base_dir=tmp_path)
    # secrets.conf's own value, present
    assert tree[None]["rpcport"] == ["9001"]
    # the root's own includeconf value, unextended by secrets.conf's own
    # (third.conf is never read at all, so 9002 appears nowhere)
    assert tree[None]["includeconf"] == ["secrets.conf"]


def test_load_conf_tree_a_missing_included_file_is_fatal(tmp_path: Path) -> None:
    """An `includeconf` naming a file that does not exist is refused."""
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("includeconf=missing.conf\n", encoding="utf-8")
    with pytest.raises(ValueError, match="could not be opened"):
        cli._load_conf_tree(conf, conf_explicit=False, base_dir=tmp_path)


def test_resolve_bool_cli_value_wins() -> None:
    """`cli_value=True` is true regardless of what the file says."""
    assert cli._resolve_bool(True, "debug", {"debug": ["0"]}) is True  # noqa: FBT003


def test_resolve_bool_falls_back_to_the_files_own_last_value() -> None:
    """`cli_value=False`: the file's own last value for the key decides."""
    assert cli._resolve_bool(False, "debug", {"debug": ["0", "1"]}) is True  # noqa: FBT003
    assert cli._resolve_bool(False, "debug", {"debug": ["1", "0"]}) is False  # noqa: FBT003


def test_resolve_bool_defaults_to_false_when_absent_everywhere() -> None:
    """Neither the flag nor the file names the key: `False`."""
    assert cli._resolve_bool(False, "debug", {}) is False  # noqa: FBT003


def test_resolve_listen_cli_value_wins_true() -> None:
    """An explicit `-listen`/`-listen=1` wins even under `-connect`."""
    assert cli._resolve_listen("1", {}, connect_given=True) is True


def test_resolve_listen_cli_value_wins_false() -> None:
    """An explicit `-nolisten`/`-listen=0` wins even without `-connect`."""
    assert cli._resolve_listen("0", {}, connect_given=False) is False


def test_resolve_listen_falls_back_to_the_files_own_last_value() -> None:
    """The file's own `listen=` wins over the `-connect`-driven default."""
    assert cli._resolve_listen(None, {"listen": ["0"]}, connect_given=False) is False
    assert cli._resolve_listen(None, {"listen": ["1"]}, connect_given=True) is True


def test_resolve_listen_defaults_to_true_without_connect() -> None:
    """Named nowhere, and `-connect` not given: Core's own `DEFAULT_LISTEN`."""
    assert cli._resolve_listen(None, {}, connect_given=False) is True


def test_resolve_listen_defaults_to_false_under_connect() -> None:
    """Named nowhere, `-connect` given: the interaction's own default."""
    assert cli._resolve_listen(None, {}, connect_given=True) is False


def _args(**overrides: Any) -> argparse.Namespace:
    """Build a `Namespace` stand-in for `_resolve_chain_name`'s own `args`."""
    defaults = {"chain": None, "testnet": False, "signet": False, "regtest": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_resolve_chain_name_defaults_to_mainnet() -> None:
    """Nothing named anywhere: `mainnet`."""
    assert cli._resolve_chain_name(_args(), {}) == "mainnet"


@pytest.mark.parametrize(
    ("flag", "chain_name"),
    [("regtest", "regtest"), ("signet", "signet"), ("testnet", "testnet")],
)
def test_resolve_chain_name_from_a_cli_flag(flag: str, chain_name: str) -> None:
    """A single cli boolean flag resolves to its own chain."""
    assert cli._resolve_chain_name(_args(**{flag: True}), {}) == chain_name


@pytest.mark.parametrize(
    ("alias", "chain_name"),
    [
        ("main", "mainnet"),
        ("test", "testnet"),
        ("signet", "signet"),
        ("regtest", "regtest"),
    ],
)
def test_resolve_chain_name_from_a_cli_chain_value(alias: str, chain_name: str) -> None:
    """`-chain=<alias>` translates Core's own external vocabulary."""
    assert cli._resolve_chain_name(_args(chain=alias), {}) == chain_name


def test_resolve_chain_name_from_the_files_own_default_section() -> None:
    """No cli chain selector at all: the file's default section decides."""
    assert cli._resolve_chain_name(_args(), {"regtest": ["1"]}) == "regtest"
    assert cli._resolve_chain_name(_args(), {"chain": ["signet"]}) == "signet"


def test_resolve_chain_name_a_files_own_regtest_0_is_not_regtest() -> None:
    """`regtest=0` in the default section resolves as false, not true."""
    assert cli._resolve_chain_name(_args(), {"regtest": ["0"]}) == "mainnet"


def test_resolve_chain_name_rejects_an_unknown_file_alias() -> None:
    """A file's own `chain=<alias>` outside Core's four is refused.

    Unreachable from the command line, where `-chain`'s own `choices=`
    already refuses one before this function ever runs -- only a file
    can still name a bad one.
    """
    with pytest.raises(ValueError, match="unknown chain"):
        cli._resolve_chain_name(_args(), {"chain": ["bogus"]})


def test_resolve_chain_name_rejects_two_cli_selectors_at_once() -> None:
    """More than one of `-testnet`/`-signet`/`-regtest`/`-chain` is refused."""
    with pytest.raises(ValueError, match="use at most one"):
        cli._resolve_chain_name(_args(testnet=True, signet=True), {})


def test_resolve_chain_name_rejects_a_cli_and_a_file_selector_together() -> None:
    """The count is over cli and file together, not cli-vs-file precedence.

    `-testnet` on the command line and `signet=1` in the file's default
    section are two different named options, each independently
    resolved -- Core counts both, and so does this reader.
    """
    with pytest.raises(ValueError, match="use at most one"):
        cli._resolve_chain_name(_args(testnet=True), {"signet": ["1"]})


def test_collect_file_values_reads_the_default_section_on_mainnet() -> None:
    """On mainnet, even a network-only key answers from the default section."""
    tree = {None: {"port": ["9000"]}, "main": {}}
    assert cli._collect_file_values(tree, "mainnet") == {"port": ["9000"]}


def test_collect_file_values_ignores_the_default_section_for_a_network_only_key() -> (
    None
):
    """Off mainnet, the default section skips a network-only key."""
    tree = {None: {"port": ["9000"]}, "regtest": {}}
    assert cli._collect_file_values(tree, "regtest") == {}


def test_collect_file_values_still_reads_the_default_section_for_prune_and_debug() -> (
    None
):
    """`-prune`/`-debug` are not network-only: the default section counts."""
    tree = {None: {"prune": ["550"], "debug": ["1"]}, "regtest": {}}
    assert cli._collect_file_values(tree, "regtest") == {
        "prune": ["550"],
        "debug": ["1"],
    }


def test_collect_file_values_reads_the_chains_own_section_on_any_chain() -> None:
    """A chain's own section always answers, network-only key or not."""
    tree = {None: {}, "regtest": {"port": ["9001"]}}
    assert cli._collect_file_values(tree, "regtest") == {"port": ["9001"]}


def test_collect_file_values_orders_default_before_chain_section() -> None:
    """Default-section values come before the chain's own -- last wins."""
    tree = {None: {"prune": ["1"]}, "main": {"prune": ["2"]}}
    assert cli._collect_file_values(tree, "mainnet")["prune"] == ["1", "2"]


def test_collect_file_values_warns_about_an_unrecognised_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A key this reader does not recognise is warned about, and dropped."""
    tree = {None: {"walletnotify": ["x"]}, "main": {}}
    collected = cli._collect_file_values(tree, "mainnet")
    assert "walletnotify" not in collected
    assert "walletnotify" in capsys.readouterr().err


def test_collect_file_values_warns_specifically_about_datadir(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`datadir=` gets its own message, not the generic unrecognised one.

    `datadir` is a real, documented flag -- unlike `walletnotify` above,
    which this reader genuinely does not know -- so the message it gets
    says why it is never read from a file rather than implying it is a
    typo.
    """
    tree = {None: {"datadir": ["/x"]}, "main": {}}
    collected = cli._collect_file_values(tree, "mainnet")
    assert "datadir" not in collected
    err = capsys.readouterr().err
    assert "cannot be set in a configuration file" in err
    assert "unknown configuration value" not in err


def test_collect_file_values_reads_listen_on_any_chain_without_a_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`listen` is recognised, not network-only, and never warned about."""
    tree = {None: {"listen": ["0"]}, "regtest": {}}
    assert cli._collect_file_values(tree, "regtest") == {"listen": ["0"]}
    assert capsys.readouterr().err == ""


def test_resolve_str_cli_value_wins() -> None:
    """A given `cli_value` wins over any collected file value."""
    assert cli._resolve_str("cli", {"k": ["file"]}, "k") == "cli"


def test_resolve_str_falls_back_to_the_last_collected_value() -> None:
    """`cli_value=None`: the file's own last collected value."""
    assert cli._resolve_str(None, {"k": ["a", "b"]}, "k") == "b"


def test_resolve_str_absent_everywhere_is_none() -> None:
    """Neither source names the key: `None`."""
    assert cli._resolve_str(None, {}, "k") is None


def test_resolve_int_cli_value_wins() -> None:
    """A given `cli_value` wins over any collected file value."""
    assert cli._resolve_int(1, {"k": ["2"]}, "k") == 1


def test_resolve_int_falls_back_to_the_last_collected_value() -> None:
    """`cli_value=None`: the file's own last collected value, as an int."""
    assert cli._resolve_int(None, {"k": ["1", "2"]}, "k") == 2


def test_resolve_int_absent_everywhere_is_none() -> None:
    """Neither source names the key: `None`."""
    assert cli._resolve_int(None, {}, "k") is None


def test_resolve_int_rejects_a_non_integer_file_value() -> None:
    """A file value that does not parse as an int is refused."""
    with pytest.raises(ValueError, match="is not an integer"):
        cli._resolve_int(None, {"k": ["notanumber"]}, "k")


def test_resolve_list_combines_cli_and_file_values() -> None:
    """Every value from both sources is kept, none replacing another."""
    assert cli._resolve_list(["a"], {"k": ["b", "c"]}, "k") == ["a", "b", "c"]


def test_resolve_list_absent_everywhere_is_empty() -> None:
    """Neither source names the key: an empty list."""
    assert cli._resolve_list([], {}, "k") == []


def test_build_parser_accepts_both_dash_spellings() -> None:
    """`-datadir` and `--datadir` parse to the same value."""
    parser = cli._build_parser()
    assert parser.parse_args(["-datadir", "x"]).datadir == "x"
    assert parser.parse_args(["--datadir", "x"]).datadir == "x"


def test_build_parser_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """`-h` prints usage and exits `0`, `argparse`'s own contract."""
    parser = cli._build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["-h"])
    assert excinfo.value.code == 0
    assert "btclib-node" in capsys.readouterr().out


def test_build_parser_rejects_an_unknown_flag() -> None:
    """An unrecognised flag is refused by `argparse` itself, exit `2`."""
    parser = cli._build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["-notaflag"])
    assert excinfo.value.code == 2


def test_build_parser_rejects_an_unknown_chain_alias() -> None:
    """`-chain`'s own `choices=` refuses an alias outside Core's four."""
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["-chain", "bogus"])


def test_build_config_with_nothing_given_uses_every_default(tmp_path: Path) -> None:
    """No flags, no file: every `Config` field takes its own default."""
    config = cli.build_config(["-datadir", str(tmp_path)])
    assert config.chain.name == "mainnet"
    assert config.rpc_host == "127.0.0.1"
    assert config.pruned is False
    assert config.connect == ()
    assert config.addnode == ()
    assert config.listen is True


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
        cli.build_config(["-datadir", str(datadir)])


def test_build_config_datadir_parent_is_a_file_raises(tmp_path: Path) -> None:
    """`-datadir` naming a path a file blocks higher up is refused too."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("not a directory", encoding="utf-8")
    datadir = blocker / "subdir"
    with pytest.raises(ValueError, match="does not exist"):
        cli.build_config(["-datadir", str(datadir)])


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
        cli.build_config(["-datadir", str(datadir)])


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
    config = cli.build_config(["-datadir", str(tmp_path)])
    assert config.chain.name == "regtest"
    assert config.p2p_port == 9123


def test_build_config_conf_explicit_relative_is_joined_to_datadir(
    tmp_path: Path,
) -> None:
    """A relative `-conf` is resolved against `-datadir`, not against `cwd`."""
    (tmp_path / "mine.conf").write_text("regtest=1\n", encoding="utf-8")
    config = cli.build_config(["-datadir", str(tmp_path), "-conf", "mine.conf"])
    assert config.chain.name == "regtest"


def test_build_config_conf_explicit_and_missing_raises(tmp_path: Path) -> None:
    """`-conf` naming a file that is not there is fatal, not skipped."""
    with pytest.raises(ValueError, match="could not be opened"):
        cli.build_config(["-datadir", str(tmp_path), "-conf", "nope.conf"])


def test_build_config_cli_port_overrides_the_file(tmp_path: Path) -> None:
    """`-port` on the command line wins over the file's own value."""
    (tmp_path / "bitcoin.conf").write_text("port=1111\n", encoding="utf-8")
    config = cli.build_config(["-datadir", str(tmp_path), "-port", "2222"])
    assert config.p2p_port == 2222


def test_build_config_rpcbind_sets_the_host() -> None:
    """`-rpcbind=<addr>` sets `rpc_host`."""
    config = cli.build_config(["-regtest", "-rpcbind", "0.0.0.0"])  # noqa: S104
    assert config.rpc_host == "0.0.0.0"  # noqa: S104


def test_build_config_rpcbind_port_overrides_rpcport() -> None:
    """`-rpcbind`'s own port, when given, wins over `-rpcport`."""
    config = cli.build_config(
        ["-regtest", "-rpcbind", "127.0.0.1:9998", "-rpcport", "9999"]
    )
    assert config.rpc_port == 9998


def test_build_config_rpcbind_without_a_port_leaves_rpcport_alone() -> None:
    """`-rpcbind` naming no port of its own does not touch `-rpcport`."""
    config = cli.build_config(["-regtest", "-rpcbind", "127.0.0.1", "-rpcport", "9999"])
    assert config.rpc_port == 9999


def test_build_config_prune_nonzero_reaches_config_pruned() -> None:
    """A nonzero `-prune` builds a `Config` with `pruned` set, any value alike.

    `Config.pruned` is a flat bool: `cli.py`'s own `-prune` help text is
    where the collapse of Core's own MiB target down to "zero or not" is
    argued.
    """
    assert cli.build_config(["-regtest", "-prune", "550"]).pruned is True
    assert cli.build_config(["-regtest", "-prune", "1"]).pruned is True


def test_build_config_prune_zero_leaves_pruned_false() -> None:
    """`-prune=0`, the default, builds an unpruned `Config`."""
    assert cli.build_config(["-regtest", "-prune", "0"]).pruned is False
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
        cli.build_config(["-regtest", "-prune", "-1"])


def test_build_config_blocksdir_reaches_config(tmp_path: Path) -> None:
    """`-blocksdir` on the command line resolves through to `Config`."""
    config = cli.build_config(["-regtest", "-blocksdir", str(tmp_path)])
    assert config.blocks_dir == tmp_path.absolute() / "regtest"


def test_build_config_without_blocksdir_leaves_it_none(tmp_path: Path) -> None:
    """No `-blocksdir`: `Config`'s own default, `None`."""
    config = cli.build_config(["-datadir", str(tmp_path)])
    assert config.blocks_dir is None


def test_build_config_blocksdir_missing_raises(tmp_path: Path) -> None:
    """`-blocksdir` naming a directory that does not exist is fatal."""
    missing = tmp_path / "nope"
    with pytest.raises(ValueError, match="does not exist"):
        cli.build_config(["-regtest", "-blocksdir", str(missing)])


def test_build_config_reads_blocksdir_from_the_file(tmp_path: Path) -> None:
    """`blocksdir=` in `bitcoin.conf` resolves the same way the flag does."""
    blocks_dir = tmp_path / "elsewhere"
    blocks_dir.mkdir()
    (tmp_path / "bitcoin.conf").write_text(
        f"regtest=1\nblocksdir={blocks_dir}\n", encoding="utf-8"
    )
    config = cli.build_config(["-datadir", str(tmp_path)])
    assert config.blocks_dir == blocks_dir.absolute() / "regtest"


def test_build_config_connect_and_addnode_reach_config() -> None:
    """`-connect`/`-addnode` on the command line resolve through to `Config`."""
    config = cli.build_config(
        ["-regtest", "-connect", "10.0.0.1", "-addnode", "10.0.0.2:2"]
    )
    assert config.connect == (("10.0.0.1", RegTest().port),)
    assert config.addnode == (("10.0.0.2", 2),)


def test_build_config_connect_alone_defaults_listen_to_false() -> None:
    """`-connect` alone: not listening, `config.connect` still carries the peer.

    `Node.run`'s own dial loop reads `config.connect`/`config.addnode`
    unconditionally, regardless of `config.listen` -- this is the "still
    dials" half; `P2pManager`'s own bind gate (`p2p/manager.py`) is the
    "not listening" half, `manager_test.py`'s own concern.
    """
    config = cli.build_config(["-regtest", "-connect", "10.0.0.1"])
    assert config.listen is False
    assert config.connect == (("10.0.0.1", RegTest().port),)


def test_build_config_connect_and_explicit_listen_enables_both() -> None:
    """`-connect` plus `-listen=1`: the explicit flag wins over the default."""
    config = cli.build_config(["-regtest", "-connect", "10.0.0.1", "-listen=1"])
    assert config.listen is True
    assert config.connect == (("10.0.0.1", RegTest().port),)


def test_build_config_nolisten_forces_listen_false() -> None:
    """`-nolisten` reaches `Config` the same way `-listen=0` would."""
    config = cli.build_config(["-regtest", "-nolisten"])
    assert config.listen is False


def test_build_config_bare_listen_means_true() -> None:
    """`-listen` given without a value is Core's own bare boolean flag."""
    config = cli.build_config(["-regtest", "-connect", "10.0.0.1", "-listen"])
    assert config.listen is True


def test_build_config_connect_zero_dials_nobody() -> None:
    """`-connect=0`: still not listening, but nothing is dialled."""
    config = cli.build_config(["-regtest", "-connect", "0"])
    assert config.listen is False
    assert config.connect == ()
    assert config.connect_given is True


def test_main_builds_a_node_and_starts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main` builds a `Node` from the parsed config and starts it, once."""
    built: list[Any] = []

    class FakeNode:
        def __init__(self, config: Any) -> None:
            built.append(config)

        def start(self) -> None:
            built.append("started")

    handlers: list[Any] = []
    monkeypatch.setattr(cli, "Node", FakeNode)
    monkeypatch.setattr(cli, "install_signal_handlers", handlers.append)

    cli.main(["-datadir", str(tmp_path), "-regtest"])

    assert built[0].chain.name == "regtest"
    assert built[-1] == "started"
    assert len(handlers) == 1


def test_main_a_bad_argument_exits_one_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `build_config` failure prints to stderr and exits `1`."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["-datadir", str(tmp_path), "-conf", "nope.conf"])
    assert excinfo.value.code == 1
    assert "btclib-node:" in capsys.readouterr().err


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
