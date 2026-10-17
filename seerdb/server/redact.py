# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Statement text with its secrets taken out, for whatever the Mirror keeps of
it: the log, the migration profile, the translation report
(GHSA-27rj-h7p6-pmqf).

A password travels in a statement's text in Oracle: ``CREATE USER u
IDENTIFIED BY pw``, ``ALTER USER u IDENTIFIED BY new REPLACE old``,
``CREATE DATABASE LINK ... IDENTIFIED BY pw``. Oracle keeps none of them where
a reader of its logs could find them, and neither may the Mirror.
"""

from __future__ import annotations

import re

# `IDENTIFIED BY [VALUES] secret` and an ALTER USER's `REPLACE secret`: the
# secret a quoted name, a quoted literal (a VALUES hash) or one word.
_SECRET = r'("(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'|[^\s;]+)'
_IDENTIFIED_BY = re.compile(rf'(?is)(\bIDENTIFIED\s+BY\s+(?:VALUES\s+)?){_SECRET}')
_REPLACE = re.compile(rf'(?is)(\bREPLACE\s+){_SECRET}')
_ALTER_USER = re.compile(r'(?is)^\s*ALTER\s+USER\b')

REDACTED = '***'


def redact_passwords(sql: str) -> str:
    """``sql`` with every password it carries replaced by ``***``."""
    if 'identified' not in sql.lower():
        return sql
    out = _IDENTIFIED_BY.sub(lambda m: m.group(1) + REDACTED, sql)
    if _ALTER_USER.match(out):
        out = _REPLACE.sub(lambda m: m.group(1) + REDACTED, out)
    return out
