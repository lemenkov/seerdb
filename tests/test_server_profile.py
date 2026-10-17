# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The migration profile's recording (#1881): fingerprints, the statistics kept
per statement, the backend wrapper, and the launcher's --profile."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from seerdb.server.backend import BackendError, Result
from seerdb.server.launcher import _profiled, build
from seerdb.server.profile import SAMPLES, Profile, ProfilingBackend, fingerprint


def test_a_fingerprint_is_the_statement_whatever_varies_between_runs() -> None:
    # Literals and binds are each a ?, comments go, words are upper case and
    # spacing is no matter: one statement, however its runs differ. A quoted
    # identifier and a name with digits in it are kept.
    one = fingerprint("SELECT a FROM t WHERE id = 1 AND s = 'x' -- why\n")
    assert one == fingerprint('select a  from t /* c */ where id=:1 and s = :s')
    assert one == 'SELECT A FROM T WHERE ID=? AND S=?'
    assert fingerprint('SELECT "Mixed", t1.c2 FROM t1 WHERE x IN (1, 2.5e3, .5)') == (
        'SELECT "Mixed",T1.C2 FROM T1 WHERE X IN(?,?,?)'
    )
    assert fingerprint("INSERT INTO t VALUES (q'[it's]', N'b', :\"B\")") == (
        'INSERT INTO T VALUES(?,?,?)'
    )
    assert fingerprint('BEGIN p(:1, 42); END;') == 'BEGIN P(?,?);END'


def _record(profile: Profile, sql: str, seconds: float, **kw: Any) -> None:
    fields: dict[str, Any] = {
        'rows_in': 1,
        'rows_out': 0,
        'error': None,
        'backend_statements': None,
        'backend_seconds': None,
        'translated': None,
    }
    fields.update(kw)
    profile.record(sql, kw.pop('kind', 'execute'), seconds, **fields)


def test_a_profile_keeps_each_statements_calls_times_and_errors(
    tmp_path: Path,
) -> None:
    # Per fingerprint: calls, rows in and out, total and mean time, the
    # percentiles of a bounded sample, errors by ORA code, and over a backend
    # that reports them the statements it sent and the time it waited -- the
    # rest of a call's time is the Mirror's own. Written whole, as JSON.
    path = tmp_path / 'profile.json'
    profile = Profile(str(path), 'test backend')
    for n in range(1, 101):
        _record(
            profile,
            f'SELECT * FROM t WHERE id = {n}',
            n / 1000,
            rows_out=2,
            backend_statements=1,
            backend_seconds=n / 2000,
            translated='SELECT * FROM t WHERE id = $1',
        )
    _record(profile, 'SELECT * FROM missing', 0.001, error=942)
    profile.dump()
    written = json.loads(path.read_text())
    assert written['format'] == 'seerdb-mirror-profile/1'
    assert written['backend'] == 'test backend'
    assert written['profiler_seconds'] >= 0
    (hot, missing) = written['statements']
    assert hot['fingerprint'] == 'SELECT * FROM T WHERE ID=?'
    assert (hot['calls'], hot['rows_in'], hot['rows_out']) == (100, 100, 200)
    assert hot['total_ms'] == pytest.approx(5050)
    assert hot['mean_ms'] == pytest.approx(50.5)
    assert (hot['p50_ms'], hot['p95_ms'], hot['p99_ms']) == pytest.approx((51, 96, 100))
    assert hot['backend'] == {
        'statements_per_call': 1.0,
        'database_ms': pytest.approx(2525),
        'mirror_ms': pytest.approx(2525),
    }
    assert hot['translated'] == 'SELECT * FROM t WHERE id = $1'
    assert missing['errors'] == {'ORA-00942': 1}
    assert 'backend' not in missing
    # The sample stays bounded however many calls there are.
    for _ in range(SAMPLES * 2):
        _record(profile, 'SELECT 1 FROM dual', 0.0)
    entry = profile._statements['SELECT ? FROM DUAL']
    assert (entry.calls, len(entry.samples)) == (SAMPLES * 2, SAMPLES)


class _Fake:
    # A backend with the methods a ProfilingBackend records, and one it does
    # not, but no describe: the session probes for that, and must not find it.
    capabilities: frozenset[Any] = frozenset()
    field_version = 99

    def __init__(self) -> None:
        self.statements = 0
        self.waited = 0.0

    def authenticate(self, username: str) -> str | None:
        return 'pw'

    def execute(self, sql: str, binds: Any = ()) -> Result:
        self.statements += 2
        self.waited += 0.25
        if 'missing' in sql:
            raise BackendError('table or view does not exist', ora_code=942)
        return Result(rows=[(1,), (2,), (3,)])

    def execute_many(self, sql: str, rows: Any) -> int:
        return len(rows)

    def bfile_read(
        self, directory: str, filename: str, offset: int, amount: int
    ) -> bytes:
        return b'abc'

    def profile_counters(self) -> tuple[int, float, str | None]:
        return (self.statements, self.waited, 'translated text')

    def session_info(self) -> str:
        return 'the backend own'

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_a_profiled_backend_records_and_is_otherwise_the_backend(
    tmp_path: Path,
) -> None:
    # Each statement is recorded with its kind -- a PL/SQL block apart from a
    # plain execute, an array DML, a BFILE read -- and its error; whatever else
    # the session asks of the backend is the backend's own, and an optional
    # method the backend lacks is still absent.
    profile = Profile(str(tmp_path / 'p.json'))
    backend = ProfilingBackend(_Fake(), profile)  # type: ignore[arg-type]
    assert backend.field_version == 99
    assert backend.session_info() == 'the backend own'
    assert getattr(backend, 'describe', None) is None
    assert len(backend.execute('SELECT n FROM t').rows) == 3
    backend.execute('BEGIN p(:1); END;', [1])
    assert backend.execute_many('INSERT INTO t VALUES (:1)', [[1], [2]]) == 2
    assert backend.bfile_read('D', 'f.txt', 1, 3) == b'abc'
    with pytest.raises(BackendError):
        backend.execute('SELECT * FROM missing')
    backend.close()
    statements = {s['fingerprint']: s for s in profile.snapshot()['statements']}
    assert statements['SELECT N FROM T']['kinds'] == {'execute': 1}
    assert statements['SELECT N FROM T']['rows_out'] == 3
    assert statements['SELECT N FROM T']['backend'] == {
        'statements_per_call': 2.0,
        'database_ms': pytest.approx(250),
        'mirror_ms': pytest.approx(0, abs=250),
    }
    assert statements['SELECT N FROM T']['translated'] == 'translated text'
    assert statements['BEGIN P(?);END']['kinds'] == {'plsql': 1}
    assert statements['INSERT INTO T VALUES(?)']['kinds'] == {'executemany': 1}
    assert statements['INSERT INTO T VALUES(?)']['rows_in'] == 2
    assert statements['<bfile_read>']['kinds'] == {'lob': 1}
    assert statements['SELECT * FROM MISSING']['errors'] == {'ORA-00942': 1}
    assert profile.snapshot()['sessions_ended'] == 1


def test_the_launcher_takes_a_profile_and_sigusr1_writes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --profile, or profile in [server], names the file; the profile wraps
    # every session's backend and SIGUSR1 writes it (#1881).
    users = {'MIRROR_USERS': 'scott:tiger'}
    assert build(['postgres', 'dbname=mirror'], env=users).profile is None
    path = tmp_path / 'profile.json'
    launch = build(['--profile', str(path), 'postgres', 'dbname=mirror'], env=users)
    assert launch.profile == str(path)
    ini = tmp_path / 'mirror.ini'
    ini.write_text(
        f'[server]\nprofile = {path}\n[backend]\ntype = postgres\nconninfo = x\n'
    )
    assert build(['--config', str(ini)], env=users).profile == str(path)
    if not hasattr(signal, 'SIGUSR1'):
        pytest.skip('no SIGUSR1 on this platform')
    previous = signal.getsignal(signal.SIGUSR1)
    try:
        (factory, profile) = _profiled(launch, _Fake)  # type: ignore[arg-type]
        factory().execute('SELECT 1 FROM dual')
        os.kill(os.getpid(), signal.SIGUSR1)
        written = json.loads(path.read_text())
        assert [s['fingerprint'] for s in written['statements']] == [
            'SELECT ? FROM DUAL'
        ]
    finally:
        signal.signal(signal.SIGUSR1, previous)


def test_a_profile_keeps_no_password(tmp_path: Path) -> None:
    # A statement that carries a password is kept without it, in its example
    # and its fingerprint alike (GHSA-27rj-h7p6-pmqf).
    profile = Profile(str(tmp_path / 'p.json'))
    for statement in (
        'create user u identified by "S3cretProbe"',
        'alter user u identified by Other1 replace S3cretProbe',
    ):
        profile.record(
            statement,
            'execute',
            0.001,
            rows_in=0,
            rows_out=0,
            error=None,
            backend_statements=None,
            backend_seconds=None,
            translated=None,
        )
    text = json.dumps(profile.snapshot())
    assert 'S3cretProbe' not in text and 'Other1' not in text
    assert 'identified by ***' in text
