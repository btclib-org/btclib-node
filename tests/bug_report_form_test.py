# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

"""The bug report form tracks the store a checkout and a PyPI install use."""

from pathlib import Path

_FORM = Path(__file__).parents[1] / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml"


def test_the_bug_report_form_names_rocksdict_for_a_pypi_install() -> None:
    """ISS 2478: the form no longer says the PyPI release imports sqlite3.

    A checkout and a PyPI install both depend on `rocksdict`. The form
    should say so, and should not tell a reporter to skip the version
    command because `db.py` imported the stdlib instead.
    """
    text = _FORM.read_text(encoding="utf-8")
    assert "its `db.py` imports the stdlib `sqlite3`" not in text
    assert 'print(version("rocksdict"))' in text
    assert "sqlite3" not in text
