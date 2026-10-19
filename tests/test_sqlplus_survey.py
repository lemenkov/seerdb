# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Offline guard for tools/sqlplus_survey.py (#2022): how it splits the corpus
and what it counts as a difference. Running it needs sqlplus and a database."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib

_PATH = os.path.join(os.path.dirname(__file__), '..', 'tools', 'sqlplus_survey.py')


def _load():
    spec = importlib.util.spec_from_file_location('seerdb_sqlplus_survey', _PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_each_section_runs_behind_the_header() -> None:
    mod = _load()
    text = 'set echo on\nprompt ### 01 one\nselect 1 from dual;\nprompt ### 02 two\nselect 2 from dual;\nexit\n'
    got = mod.sections(text)
    assert list(got) == ['01', '02']
    assert got['01'] == 'set echo on\nprompt ### 01 one\nselect 1 from dual;\nexit\n'
    # The corpus's own closing exit is not run twice.
    assert got['02'].count('exit') == 1


def test_the_corpus_splits() -> None:
    mod = _load()
    got = mod.sections(mod._CORPUS.read_text())
    assert len(got) >= 15
    assert all(script.endswith('exit\n') for script in got.values())


def test_banners_and_constraint_names_are_not_differences(
    tmp_path: pathlib.Path,
) -> None:
    mod = _load()
    for label, release, constraint in (
        ('a', '23.26.2', 'SYS_C0055192'),
        ('b', '23.26.3', 'SYS_C0011'),
    ):
        target = tmp_path / label
        target.mkdir()
        (target / 'sec01.txt').write_text(
            f'SQL*Plus: Release {release}\nConnected to:\nORA-00001: unique constraint (PYO.{constraint}) violated\n1 row created.\n'
        )
        (target / 'exits.json').write_text(json.dumps({'01': 0}))
    assert mod.compare(tmp_path / 'a', tmp_path / 'b', show=False) == 0
    (tmp_path / 'b' / 'sec01.txt').write_text('Table created.\n')
    assert mod.compare(tmp_path / 'a', tmp_path / 'b', show=False) == 1
