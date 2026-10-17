# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Per-statement statistics for a migration profile (#1881).

The first stage of a migration profiler: run one application against Oracle
through the passthrough Mirror, and once against the Mirror over PostgreSQL,
then compare the two recordings statement by statement.

A :class:`ProfilingBackend` wraps each session's backend and records every
statement the session hands it, into one :class:`Profile` the Mirror process
shares, keyed by the statement's :func:`fingerprint`. Both backends are
recorded the same way, at the backend boundary. A backend that can say more
-- how many statements it sent its own database and how long it waited on it,
and the text it ran -- answers :meth:`profile_counters`, which the PostgreSQL
backend does.

The profile is written as JSON by :meth:`Profile.dump`: the launcher does it at
server stop, on SIGUSR1, and when a session ends, at most every few seconds.
The time the profiler itself takes is recorded with it.
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from seerdb.common.sqltext import is_plsql, sql_tokens
from seerdb.server.backend import Backend, BackendError
from seerdb.server.redact import redact_passwords

# The timings kept per statement for its percentiles: every one up to this
# many, then a uniform sample of them all.
SAMPLES = 1000
# A dump when a session ends waits this long after the one before.
SESSION_END_DUMP_SECONDS = 10.0

# A number literal, once its statement's strings, binds and comments are out
# of the way: not part of a name (T1, "A1" is quoted and so already apart).
_NUMBER = re.compile(
    r'(?<![\w$#.?])(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[fFdD]?(?![\w$#])'
)
_SPACE = re.compile(r'\s+')
# A ? stands for a value and * for every column: words, not punctuation.
_AROUND_PUNCTUATION = re.compile(r' ?([^\w$#"?* ]) ?')


def fingerprint(sql: str) -> str:
    """``sql`` with what varies between runs of one statement taken out: its
    literals and binds each a ``?``, its comments gone, its words in upper
    case and its spacing one blank -- so ``WHERE id = 1`` and ``WHERE id = :x``
    are one statement. A quoted identifier is kept as written."""
    parts: list[str] = []
    for token in sql_tokens(sql):
        text = sql[token.start : token.end]
        if token.kind in ('string', 'bind'):
            parts.append('?')
        elif token.kind == 'word':
            parts.append(text.upper())
        elif token.kind in ('space', 'comment'):
            parts.append(' ')
        elif token.kind == 'identifier':
            # Kept whole, out of the number pass's reach.
            parts.append('\x00' + text + '\x01')
        else:
            parts.append(text)
    text = _SPACE.sub(' ', ''.join(parts))
    pieces = re.split('(\x00[^\x01]*\x01)', text)
    text = ''.join(
        piece[1:-1] if piece.startswith('\x00') else _NUMBER.sub('?', piece)
        for piece in pieces
    )
    # Spacing beside punctuation says nothing: `id = ?` is `id=?`.
    text = _AROUND_PUNCTUATION.sub(r'\1', text)
    return text.strip().rstrip(';').strip()


@dataclass
class _Statement:
    # What a fingerprint has been through so far.
    example: str
    calls: int = 0
    rows_in: int = 0
    rows_out: int = 0
    seconds: float = 0.0
    samples: list[float] = field(default_factory=list)
    kinds: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    backend_statements: int = 0
    backend_seconds: float = 0.0
    translated: str | None = None


class Profile:
    """The statistics of every statement the Mirror's sessions ran, shared by
    them all (one thread per session), and written out by :meth:`dump`."""

    def __init__(self, path: str, description: str = '') -> None:
        self.path = path
        self._description = description
        self._lock = threading.Lock()
        self._statements: dict[str, _Statement] = {}
        self._sessions = 0
        self._overhead = 0.0
        self._started = time.time()
        self._last_dump = 0.0
        self._random = random.Random(1881)

    def record(
        self,
        sql: str,
        kind: str,
        seconds: float,
        *,
        rows_in: int,
        rows_out: int,
        error: int | None,
        backend_statements: int | None,
        backend_seconds: float | None,
        translated: str | None,
        named: bool = False,
    ) -> None:
        """Add one call of ``sql`` to its statement's statistics; ``named``
        for an operation recorded by its name, which is its own key."""
        begun = time.perf_counter()
        # Kept as the client sent it, but for its passwords
        # (GHSA-27rj-h7p6-pmqf), which a quoted name's form leaves even in
        # the fingerprint.
        sql = redact_passwords(sql)
        key = sql if named else fingerprint(sql)
        with self._lock:
            entry = self._statements.get(key)
            if entry is None:
                entry = self._statements[key] = _Statement(example=sql)
            entry.calls += 1
            entry.rows_in += rows_in
            entry.rows_out += rows_out
            entry.seconds += seconds
            if len(entry.samples) < SAMPLES:
                entry.samples.append(seconds)
            else:
                slot = self._random.randrange(entry.calls)
                if slot < SAMPLES:
                    entry.samples[slot] = seconds
            entry.kinds[kind] += 1
            if error is not None:
                entry.errors[error] += 1
            if backend_statements is not None:
                entry.backend_statements += backend_statements
            if backend_seconds is not None:
                entry.backend_seconds += backend_seconds
            if entry.translated is None and translated is not None:
                entry.translated = translated
            self._overhead += time.perf_counter() - begun

    def session_ended(self) -> None:
        """Count a session that ended, and write the profile out unless it was
        written only moments ago."""
        with self._lock:
            self._sessions += 1
            due = time.monotonic() - self._last_dump >= SESSION_END_DUMP_SECONDS
        if due:
            self.dump()

    def snapshot(self) -> dict[str, Any]:
        """The profile as it stands, as the JSON :meth:`dump` writes."""
        with self._lock:
            statements = [
                _statement_json(key, entry)
                for key, entry in sorted(
                    self._statements.items(), key=lambda item: -item[1].seconds
                )
            ]
            return {
                'format': 'seerdb-mirror-profile/1',
                'backend': self._description,
                'started': self._started,
                'written': time.time(),
                'sessions_ended': self._sessions,
                'profiler_seconds': self._overhead,
                'statements': statements,
            }

    def dump(self) -> None:
        """Write the profile to its path, whole: a reader never meets half of
        one."""
        snapshot = self.snapshot()
        temporary = f'{self.path}.{os.getpid()}.tmp'
        with open(temporary, 'w', encoding='utf-8') as out:
            json.dump(snapshot, out, indent=1)
        os.replace(temporary, self.path)
        with self._lock:
            self._last_dump = time.monotonic()


def _percentile(samples: Sequence[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def _statement_json(key: str, entry: _Statement) -> dict[str, Any]:
    out: dict[str, Any] = {
        'fingerprint': key,
        'example': entry.example,
        'calls': entry.calls,
        'kinds': dict(entry.kinds),
        'rows_in': entry.rows_in,
        'rows_out': entry.rows_out,
        'total_ms': entry.seconds * 1000,
        'mean_ms': entry.seconds * 1000 / entry.calls,
        'p50_ms': _percentile(entry.samples, 0.50) * 1000,
        'p95_ms': _percentile(entry.samples, 0.95) * 1000,
        'p99_ms': _percentile(entry.samples, 0.99) * 1000,
        'errors': {f'ORA-{code:05d}': count for code, count in entry.errors.items()},
    }
    if entry.backend_statements or entry.backend_seconds:
        # Over PostgreSQL: the statements the Mirror sent it per call, and
        # the call's time split between waiting on it and the Mirror's own.
        out['backend'] = {
            'statements_per_call': entry.backend_statements / entry.calls,
            'database_ms': entry.backend_seconds * 1000,
            'mirror_ms': max(entry.seconds - entry.backend_seconds, 0.0) * 1000,
        }
    if entry.translated is not None:
        out['translated'] = entry.translated
    return out


def _call_kind(sql: str) -> str:
    return 'plsql' if is_plsql(sql) else 'execute'


def _rows_out(result: object) -> int:
    # The rows a call answered with, or the rows a DML touched.
    if isinstance(result, (bytes, bytearray)):
        return 0
    rows = getattr(result, 'rows', None)
    if rows:
        return len(rows)
    if isinstance(result, tuple) and result and isinstance(result[0], int):
        return result[0]  # execute_many_rowcounts: (total, per row)
    count = result if isinstance(result, int) else getattr(result, 'rowcount', 0)
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


# The backend methods that run statements, by the call kind each records:
# None for execute, whose kind its text says (a PL/SQL block or not).
_STATEMENT_METHODS: dict[str, str | None] = {
    'execute': None,
    'execute_many': 'executemany',
    'execute_many_rowcounts': 'executemany',
    'execute_returning': 'execute',
}
# The LOB operations that reach a backend: a BFILE's, read where the file is.
# The Mirror serves the other LOB operations from what it fetched.
_LOB_METHODS = ('bfile_exists', 'bfile_length', 'bfile_read')


class ProfilingBackend:
    """A session's backend with every statement it runs recorded in
    ``profile`` (#1881). Anything else -- the optional methods the session
    probes for, its attributes -- is the wrapped backend's own, so the session
    sees the backend it would see unprofiled."""

    def __init__(self, backend: Backend, profile: Profile) -> None:
        self._backend = backend
        self._profile = profile
        self._counters: Callable[[], tuple[int, float, str | None]] | None = getattr(
            backend, 'profile_counters', None
        )
        for name, kind in _STATEMENT_METHODS.items():
            method = getattr(backend, name, None)
            if method is not None:
                self.__dict__[name] = self._statement(
                    method, kind, batch=name != 'execute'
                )
        for name in _LOB_METHODS:
            method = getattr(backend, name, None)
            if method is not None:
                self.__dict__[name] = self._lob(method, f'<{name}>')

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    @property
    def capabilities(self) -> Any:
        return self._backend.capabilities

    def authenticate(self, username: str) -> str | None:
        return self._backend.authenticate(username)

    def commit(self) -> None:
        self._backend.commit()

    def rollback(self) -> None:
        self._backend.rollback()

    def close(self) -> None:
        try:
            self._backend.close()
        finally:
            self._profile.session_ended()

    def _timed(
        self,
        text: str,
        kind: str,
        rows_in: int,
        run: Callable[[], Any],
        *,
        named: bool = False,
    ) -> Any:
        before = self._counters() if self._counters is not None else None
        began = time.perf_counter()
        error = None
        result = None
        try:
            result = run()
            return result
        except BackendError as exc:
            error = exc.ora_code
            raise
        finally:
            seconds = time.perf_counter() - began
            after = self._counters() if self._counters is not None else None
            self._profile.record(
                text,
                kind,
                seconds,
                rows_in=rows_in,
                rows_out=_rows_out(result) if result is not None else 0,
                error=error,
                backend_statements=(
                    after[0] - before[0] if before is not None and after else None
                ),
                backend_seconds=(
                    after[1] - before[1] if before is not None and after else None
                ),
                translated=None if named or not after else after[2],
                named=named,
            )

    def _statement(
        self, method: Callable[..., Any], kind: str | None, *, batch: bool
    ) -> Callable[..., Any]:
        def call(sql: str, rows: Sequence = (), *args: Any, **kwargs: Any) -> Any:
            # A batch's rows are its executions; a plain execute's are its binds.
            rows_in = len(rows) if batch else 1
            return self._timed(
                sql,
                kind or _call_kind(sql),
                rows_in,
                lambda: method(sql, rows, *args, **kwargs),
            )

        return call

    def _lob(self, method: Callable[..., Any], text: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            return self._timed(
                text, 'lob', 0, lambda: method(*args, **kwargs), named=True
            )

        return call
