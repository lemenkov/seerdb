# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""What the Mirror launchers in this directory share (#1682): their logging,
and the accounts they serve."""

from __future__ import annotations

import logging
import os


def setup_logging() -> None:
    """Log at INFO with timestamps, as every launcher does."""
    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s'
    )


def mirror_credentials(user: str, password: str) -> dict[str, str]:
    """The accounts a Mirror serves: ``user`` / ``password``, and any more
    MIRROR_USERS names as a comma-separated list of ``user:password`` pairs.
    Names are upper-cased, as Oracle's are.

    Build it ONCE and pass the same dict to every session's backend, never a
    copy per session. A changepassword on one connection has to be visible to
    the next (#21/#486/#515). A hand-written launcher for #826 used
    `credentials=dict(CREDS)` so each session got its own copy; the suite's
    change-password test then moved the real password upstream while every
    other session kept the stale one, and 2393 logins failed with ORA-01017
    before anyone noticed. The cascade looks exactly like a driver bug and is
    not one.
    """
    credentials = {user.upper(): password}
    for pair in filter(None, os.environ.get('MIRROR_USERS', '').split(',')):
        extra_user, colon, extra_password = pair.partition(':')
        if not extra_user or not colon:
            raise SystemExit(f'MIRROR_USERS entry is not user:password: {pair!r}')
        credentials[extra_user.strip().upper()] = extra_password
    return credentials
