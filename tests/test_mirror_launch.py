# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The Mirror launchers' shared account setup (#1682)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'examples'))

from mirror_launch import mirror_credentials  # noqa: E402


def test_the_default_account_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('MIRROR_USERS', raising=False)
    assert mirror_credentials('pyo', 'pyo123') == {'PYO': 'pyo123'}


def test_more_accounts_from_mirror_users(monkeypatch: pytest.MonkeyPatch) -> None:
    # Names upper-cased as Oracle's are; a password keeps its case and may
    # hold a colon; an empty entry is skipped.
    monkeypatch.setenv('MIRROR_USERS', 'pythontest:Secret,, pyoadm:a:b')
    assert mirror_credentials('PYO', 'pyo123') == {
        'PYO': 'pyo123',
        'PYTHONTEST': 'Secret',
        'PYOADM': 'a:b',
    }


def test_an_entry_without_a_password_stops_the_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('MIRROR_USERS', 'pythontest')
    with pytest.raises(SystemExit, match='not user:password'):
        mirror_credentials('PYO', 'pyo123')
