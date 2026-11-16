# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The bug report form asks every reporter for the `rocksdict` version."""

from pathlib import Path

_FORM = Path(__file__).parents[1] / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml"


def test_the_bug_report_form_names_rocksdict_for_a_pypi_install() -> None:
    """ISS 2478: the form gives the `rocksdict` version command to everyone.

    A checkout and every PyPI release from 2026.9.4 on depend on
    `rocksdict`, so the form names no `sqlite3` store for a reporter to
    give in place of the command's answer.
    """
    text = _FORM.read_text(encoding="utf-8")
    assert 'print(version("rocksdict"))' in text
    assert "sqlite3" not in text
