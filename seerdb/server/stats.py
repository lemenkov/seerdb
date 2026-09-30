# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Per-session statistics the Mirror keeps about its own sessions (#1324).

Only the Mirror sees a session's round trips: a backend sees the statements,
not the wire exchanges that carried them. So the counts live here, in the
Mirror process, keyed by the session id each session's backend reported at
login (the one the login reply carries and ``sys_context('userenv', 'sid')``
names). A backend that serves ``V$SESSTAT`` reads them through
:func:`snapshot`, which is how one session reads another's counts, as Oracle's
tests do: the counts of the session under test, read from an admin session.

The server runs one thread per connection, so the registry is guarded by a
lock. A session is tracked from its login to its end; one whose backend
reports no session id (0) is not tracked at all.
"""

from __future__ import annotations

import threading
from typing import Final

# Oracle's statistic names and numbers, as 23ai numbers them.
ROUND_TRIPS: Final = 'SQL*Net roundtrips to/from client'
STATISTIC_NUMBERS: Final = {ROUND_TRIPS: 1445}

_lock = threading.Lock()
_sessions: dict[int, dict[str, int]] = {}


def open_session(session_id: int) -> None:
    """Start tracking a session, every statistic at zero."""
    if not session_id:
        return
    with _lock:
        _sessions[session_id] = dict.fromkeys(STATISTIC_NUMBERS, 0)


def close_session(session_id: int) -> None:
    """Stop tracking a session that has ended."""
    with _lock:
        _sessions.pop(session_id, None)


def add(session_id: int, name: str, count: int = 1) -> None:
    """Add to one statistic of a tracked session; an untracked one is ignored."""
    with _lock:
        stats = _sessions.get(session_id)
        if stats is not None:
            stats[name] = stats.get(name, 0) + count


def snapshot() -> dict[int, dict[str, int]]:
    """Every tracked session's statistics, as they stand now."""
    with _lock:
        return {sid: dict(stats) for sid, stats in _sessions.items()}
