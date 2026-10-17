# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Failed logins, counted per user name, as Oracle's profile counts them
(GHSA-742q-fjfr-w5x3).

A failed login's reply is delayed, the longer the more failures in a row. A
name with ``attempts`` failures in a row is locked until an administrator
unlocks it (``ALTER USER ... ACCOUNT UNLOCK``) or the Mirror restarts. Names
are counted whether or not they are an account's, so the answers tell no
account from no account. ``attempts`` 0 never locks.
"""

from __future__ import annotations

import threading

# Oracle's default profile: FAILED_LOGIN_ATTEMPTS 10.
DEFAULT_ATTEMPTS = 10
# The delay after each failure in a row, in seconds, and its ceiling.
_DELAY_STEP = 0.5
_DELAY_MAX = 5.0

_lock = threading.Lock()
_failures: dict[str, int] = {}
_attempts = DEFAULT_ATTEMPTS


def configure(attempts: int) -> None:
    """Lock a name after ``attempts`` failures in a row; 0 never locks."""
    global _attempts
    _attempts = attempts


def is_locked(name: str) -> bool:
    with _lock:
        return bool(_attempts) and _failures.get(name.upper(), 0) >= _attempts


def failed(name: str) -> float:
    """Count a failed login of ``name``; the seconds to delay its reply."""
    with _lock:
        count = _failures.get(name.upper(), 0) + 1
        _failures[name.upper()] = count
    return min(_DELAY_STEP * count, _DELAY_MAX)


def succeeded(name: str) -> None:
    with _lock:
        _failures.pop(name.upper(), None)


def unlock(name: str) -> None:
    """``ALTER USER name ACCOUNT UNLOCK``: its count starts over."""
    succeeded(name)
