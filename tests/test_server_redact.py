# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Passwords out of what the Mirror keeps of a statement (GHSA-27rj-h7p6-pmqf)."""

from __future__ import annotations

import pytest

from seerdb.server.redact import redact_passwords


@pytest.mark.parametrize(
    ('sql', 'kept'),
    [
        ('create user u identified by "S3cret pw"', 'create user u identified by ***'),
        ('CREATE USER u IDENTIFIED BY plain1', 'CREATE USER u IDENTIFIED BY ***'),
        (
            'ALTER USER u IDENTIFIED BY new1 REPLACE old1',
            'ALTER USER u IDENTIFIED BY *** REPLACE ***',
        ),
        (
            "create user u identified by values 'S:ABC;T:DEF'",
            'create user u identified by values ***',
        ),
        (
            "create database link l connect to x identified by pw using 'svc'",
            "create database link l connect to x identified by *** using 'svc'",
        ),
        (
            'alter user u identified by "a""b" account unlock',
            'alter user u identified by *** account unlock',
        ),
        # Nothing else is touched: no password, or REPLACE outside ALTER USER.
        ('select 1 from dual', 'select 1 from dual'),
        (
            "select replace(name, 'a') from t where x = 'identified'",
            "select replace(name, 'a') from t where x = 'identified'",
        ),
    ],
)
def test_a_password_is_taken_out(sql: str, kept: str) -> None:
    assert redact_passwords(sql) == kept
