# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""A live seerdb client logs into the Mirror end-to-end.

The Mirror's own client is an independent implementation of the same protocol,
so a successful login exercises the whole server login path (handshake +
O5LOGON) against a real client.
"""

from __future__ import annotations

import socket
import threading
from typing import Any

import pytest

import seerdb
from seerdb.common.exceptions import InterfaceError, Truncated
from seerdb.common.tns import ColumnMeta
from seerdb.common.tns_consts import (
    FIELD_VERSION_12_2,
    FIELD_VERSION_21_1,
    FIELD_VERSION_23_4,
    TNS_DATA,
    TNS_TYPE_VARCHAR,
    TTI_FUN,
    TTI_MSG_TYPE_PIGGYBACK,
    VERSION_11_2_0_2,
    VERSION_12_1_0_2,
    VERSION_12_2_0_1,
    VERSION_23_1_162_0,
)
from seerdb.server.backend import (
    Capability,
    Result,
    UnsupportedFeature,
    credential_lookup,
)
from seerdb.server.framing import PacketStream
from seerdb.server.session import handle_login, serve_session

_CREDS = {'PYO': 'pyo123'}


class _DualBackend:
    # A trivial Backend: DUAL returns 'X'; anything else is refused with a clean
    # ORA error (so the Mirror answers, never desyncs).
    capabilities: frozenset[Capability] = frozenset()

    def authenticate(self, username: str) -> str | None:
        return credential_lookup(_CREDS, username)

    def execute(self, sql: str, binds=()) -> Result:
        if 'dual' in sql.lower():
            col = ColumnMeta(
                name=b'DUMMY', data_type=TNS_TYPE_VARCHAR, data_length=1, max_size=1
            )
            return Result(columns=[col], rows=[('X',)])
        raise UnsupportedFeature(f'the DUAL backend only knows DUAL: {sql!r}')

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass

    def set_end_to_end(self, attrs: dict) -> None:
        # Record what the Mirror hands over from a tracing piggyback.
        self.tracing = {**getattr(self, 'tracing', {}), **attrs}

    def sessionless_begin(self, transaction_id: bytes, timeout: int) -> None:
        self.sessionless: tuple[str, bytes | None, int] = (
            'begin',
            transaction_id,
            timeout,
        )

    def sessionless_resume(self, transaction_id: bytes, timeout: int) -> None:
        self.sessionless = ('resume', transaction_id, timeout)

    def sessionless_suspend(self) -> None:
        self.sessionless = ('suspend', None, 0)


def _run_mirror(listen: socket.socket, result: dict) -> None:
    conn, _ = listen.accept()
    stream = PacketStream(conn)
    try:
        (result['user'], _sqlplus, _conn_key, result['fv']) = handle_login(
            stream, _DualBackend()
        )
        # Block on the client's logoff / EOF so the socket stays open until the
        # client has read the auth result and returned from connect().
        stream.read_packet()
    except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
        result['error'] = exc
    finally:
        conn.close()


def test_live_seerdb_login() -> None:
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    server = threading.Thread(target=_run_mirror, args=(listen, result), daemon=True)
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        # A live connection whose field version negotiated down to 11g (6).
        assert conn is not None
        assert conn.field_version == 6
        assert conn.server_version == VERSION_11_2_0_2
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()

    assert result.get('error') is None, result.get('error')
    assert result.get('user') == 'PYO'


def _run_mirror_session(listen: socket.socket, result: dict) -> None:
    conn, _ = listen.accept()
    try:
        result['user'] = serve_session(PacketStream(conn), _DualBackend())
    except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
        result['error'] = exc
    finally:
        conn.close()


class _BadOutBindBackend(_DualBackend):
    # A PL/SQL block returns an OUT-bind value the wire cannot encode (a bare
    # object has no _encode_value branch), standing in for a backend that hands
    # back a value the Mirror can't represent — e.g. an INTERVAL YEAR TO MONTH that
    # a foreign backend surfaces as a plain timedelta. Everything else behaves like
    # _DualBackend.
    def execute(self, sql: str, binds=()) -> Result:
        if sql.lstrip().upper().startswith('BEGIN'):
            return Result(out_binds=[object()])
        return super().execute(sql, binds)


def _run_bad_out_bind_session(listen: socket.socket, result: dict) -> None:
    conn, _ = listen.accept()
    try:
        result['user'] = serve_session(PacketStream(conn), _BadOutBindBackend())
    except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
        result['error'] = exc
    finally:
        conn.close()


def _run_mirror_login_at(listen: socket.socket, result: dict, version: int) -> None:
    conn, _ = listen.accept()
    try:
        result['user'] = serve_session(
            PacketStream(conn), _DualBackend(), field_version=version
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
        result['error'] = exc
    finally:
        conn.close()


@pytest.mark.parametrize('version', [7, 8, 16, 17])  # 12.1, 12.2, 21c, 23ai
def test_live_seerdb_login_at_a_higher_field_version(version: int) -> None:
    # A Mirror advertising a 12c+ field version: the real client negotiates to it
    # and logs in — a 12.1+ client length-prefixes the username in OSESSKEY /
    # AUTH, which the auth parsers must honour for that version. The O5LOGON
    # crypto itself is the same 11g SHA-1 verifier scheme at every version.
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    server = threading.Thread(
        target=_run_mirror_login_at, args=(listen, result, version), daemon=True
    )
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        assert conn.field_version == version
        # …and the query round-trip in that version's request / describe / OER
        # layouts, with and without a bind (12.2+ reshapes all three).
        cursor = conn.cursor()
        cursor.execute('select * from dual')
        assert cursor.fetchone() == ('X',)
        cursor.execute('select :b from dual', ['abc'])
        assert cursor.fetchone() == ('X',)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()

    assert result.get('error') is None, result.get('error')
    assert result.get('user') == 'PYO'


class _Present12cBackend(_DualBackend):
    # Declares a server identity (12.1) independent of its wire field version, which
    # stays the 11.2 default -- the decoupling a PostgreSQL-backed Mirror uses to get
    # the dialect's native OFFSET/FETCH without changing the wire (#33).
    from seerdb.server.identity import IDENTITY_12_1

    server_identity = IDENTITY_12_1


def _run_present_12c_session(listen: socket.socket, result: dict) -> None:
    conn, _ = listen.accept()
    try:
        result['user'] = serve_session(PacketStream(conn), _Present12cBackend())
    except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
        result['error'] = exc
    finally:
        conn.close()


def test_backend_presents_a_release_above_its_wire_version() -> None:
    # A backend that declares server_identity is introduced at that release while the
    # wire still negotiates its (lower) field version: the client reads the version
    # from the login banner, so conn.version is 12.1 though the wire is 11.2 (fv 6).
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    server = threading.Thread(
        target=_run_present_12c_session, args=(listen, result), daemon=True
    )
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        # Reported release is 12.1.0.2.0, but the negotiated wire is still 11.2 (fv 6).
        assert conn.server_version == VERSION_12_1_0_2
        assert conn.field_version == 6
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()

    assert result.get('error') is None, result.get('error')
    assert result.get('user') == 'PYO'


class _RecordingArrayBackend(_DualBackend):
    # Counts the INSERTs it is handed, so a test can see how many iterations of an
    # array execute actually reached the backend.
    def __init__(self) -> None:
        self.inserts: list = []

    def execute(self, sql: str, binds=()) -> Result:
        if 'insert' in sql.lower():
            self.inserts.append(list(binds))
            return Result(rowcount=1)
        return super().execute(sql, binds)


def _run_recording_session(
    listen: socket.socket, result: dict, backend: _RecordingArrayBackend
) -> None:
    conn, _ = listen.accept()
    try:
        result['user'] = serve_session(PacketStream(conn), backend)
    except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
        result['error'] = exc
    finally:
        conn.close()


def test_empty_row_array_execute_runs_once_per_iteration() -> None:
    # executemany of a no-bind INSERT ([[], [], []]) sends no TTI_RXD row data, only
    # the al8i4 iteration count; the Mirror must still apply it once per iteration
    # rather than collapsing it to a single execute (#33).
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    backend = _RecordingArrayBackend()
    server = threading.Thread(
        target=_run_recording_session, args=(listen, result, backend), daemon=True
    )
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        cursor = conn.cursor()
        cursor.executemany('INSERT INTO t (id) VALUES (1)', [[], [], []])
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()

    assert result.get('error') is None, result.get('error')
    # All three empty iterations reached the backend, not just one.
    assert len(backend.inserts) == 3


def _exec_body() -> bytes:
    from seerdb.common.tns import encode_dictionary_exec

    return encode_dictionary_exec(
        {
            'field_version': 17,
            'seq': 5,
            'query': {
                'type': 'select',
                'auto': 0,
                'fetch': 15,
                'server_version': 0,
                'cursor': 0,
                'query': 'select * from dual',
                'bind': [],
                'batch': [],
                'def': [],
                'batcherrors': None,
                'arraydmlrowcounts': None,
                'return_binds': None,
                'scrollable': False,
                'scroll': None,
            },
        }
    )


@pytest.mark.parametrize(
    'attrs',
    [
        {'module': 'm'},
        {
            'client_identifier': 'c',
            'module': 'mod',
            'action': 'a',
            'client_info': 'i',
            'dbop': 'd',
        },
        {'module': None, 'action': 'act'},  # a cleared attribute carries no value
        {'client_info': 'x' * 300},  # a long value rides the chunked DALC form
    ],
)
def test_skip_piggybacks_walks_the_12c_tracing_and_session_state(attrs: dict) -> None:
    # The 12.1+ client puts the end-to-end tracing (135) and request-boundary
    # session-state (176) piggybacks in front of a call; the Mirror keeps no such
    # state and must land exactly on the call that follows.
    from seerdb.common.tns import (
        _DECODE_FIELD_VERSION,
        encode_close_cursors_piggyback,
        encode_end_to_end_piggyback,
        encode_session_state_piggyback,
    )
    from seerdb.server.session import _skip_piggybacks

    body = _exec_body()
    token = _DECODE_FIELD_VERSION.set(17)
    try:
        e2e = encode_end_to_end_piggyback(4, 17, attrs)
        state = encode_session_state_piggyback(4, 17, 0x44)
        close = encode_close_cursors_piggyback(4, 17, [3, 9])
        backend = _DualBackend()
        assert _skip_piggybacks(e2e + body, backend) == body
        # …and the attributes (a cleared one as None) reach the backend.
        assert backend.tracing == dict(attrs)
        assert _skip_piggybacks(state + body) == body
        assert _skip_piggybacks(close + e2e + state + body) == body
        # an unknown piggyback is left in place, as before
        assert (
            _skip_piggybacks(bytes([0x11, 0xCD, 4]) + body)
            == bytes([0x11, 0xCD, 4]) + body
        )
    finally:
        _DECODE_FIELD_VERSION.reset(token)


# The close-temp-LOBs piggyback a thin client sent a live 23ai in front of its
# second large-LOB insert (fv24: the ub8 token follows the sequence byte): a
# FREE_TEMP | ARRAY over one 40-byte (ub2-prefixed) locator, then `03 04 ...`, the
# re-execute it rides on.
_CLOSE_TEMP_LOBS_23AI = bytes.fromhex(
    '11 60 0a 00 01 01 28 00 00 00 00 00 00 00 04 00 08 01 11 00 00 00 00 00'
    '00 00 00 00 00 00 00 26 00 01 81 08 00 03 00 01 6a 51 00 00 00 67 00 00 00'
    '01 00 00 00 0a 00 00 00 01 00 00 1a ab 82 f1 00 00 00 01 00 00'
)


@pytest.mark.parametrize('field_version', [17, 24])
def test_skip_piggybacks_frees_the_closed_temp_lobs(field_version: int) -> None:
    # A client that let its temp LOB objects go out of scope frees them on its
    # next call as a piggyback (func 96, #852) rather than a FREE_TEMP call of
    # their own. The Mirror has to walk it -- it used to stop there, and the
    # second large-LOB insert of every session was refused -- and it drops the
    # buffers, as the FREE_TEMP call does.
    from seerdb.common.tns import (
        _DECODE_FIELD_VERSION,
        encode_free_temp_lobs_piggyback,
        parse_free_temp_lobs_piggyback,
    )
    from seerdb.server.session import _skip_piggybacks, _TempLobs

    body = _exec_body()
    temp_lobs = _TempLobs()
    first, second, kept = (temp_lobs.mint(True) for _ in range(3))
    temp_lobs.append(first, b'a')
    temp_lobs.append(second, b'b')
    token = _DECODE_FIELD_VERSION.set(field_version)
    try:
        free = encode_free_temp_lobs_piggyback(4, field_version, [first, second])
        assert _skip_piggybacks(free + body, None, temp_lobs) == body
        assert (temp_lobs.content(first), temp_lobs.content(second)) == (b'', b'')
        # A locator the Mirror never saw (a client can free what it never wrote)
        # is not an error, and nothing else is disturbed.
        stray = encode_free_temp_lobs_piggyback(4, field_version, [first])
        assert _skip_piggybacks(stray + body, None, temp_lobs) == body
        assert temp_lobs.content(kept) == b''
        # An empty array walks too.
        assert (
            _skip_piggybacks(
                encode_free_temp_lobs_piggyback(4, field_version, []) + body
            )
            == body
        )
    finally:
        _DECODE_FIELD_VERSION.reset(token)

    # The parser lands on the call behind the live 23ai bytes, and the encoder
    # reproduces them from the captured locator -- to the byte, except that the
    # client writes the operation ub4 at full width (04 00 08 01 11) where
    # encode_sb4 compacts it (03 08 01 11); same value, so compare parsed.
    locator = _CLOSE_TEMP_LOBS_23AI[-38:]
    freed, rest = parse_free_temp_lobs_piggyback(_CLOSE_TEMP_LOBS_23AI[4:] + body)
    assert freed == [locator]
    assert rest == body
    mine = encode_free_temp_lobs_piggyback(10, 24, [locator])
    assert mine[:14] == _CLOSE_TEMP_LOBS_23AI[:14]
    assert mine[-51:] == _CLOSE_TEMP_LOBS_23AI[-51:]
    assert parse_free_temp_lobs_piggyback(mine[4:] + body) == (freed, body)
    # …and a piggyback cut short is reported, not mis-read as a call.
    with pytest.raises(Truncated):
        parse_free_temp_lobs_piggyback(_CLOSE_TEMP_LOBS_23AI[4:-1])


def test_live_seerdb_tracing_attributes_at_a_higher_field_version() -> None:
    # A real 23ai-negotiated client sets tracing attributes; the next execute
    # carries the SET_END_TO_END_ATTR piggyback, and the query still answers.
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]
    result: dict = {}
    backend = _DualBackend()

    def run() -> None:
        conn, _ = listen.accept()
        try:
            result['user'] = serve_session(
                PacketStream(conn), backend, field_version=17
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
            result['error'] = exc
        finally:
            conn.close()

    server = threading.Thread(target=run, daemon=True)
    server.start()
    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        conn.module = 'seerdb-test'
        conn.client_identifier = 'tracer'
        cursor = conn.cursor()
        cursor.execute('select * from dual')
        assert cursor.fetchone() == ('X',)
        assert backend.tracing == {
            'module': 'seerdb-test',
            'action': None,
            'client_identifier': 'tracer',
        }
        conn.module = None  # a clear rides the next call too
        cursor.execute('select * from dual')
        assert cursor.fetchone() == ('X',)
        assert backend.tracing['module'] is None
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()
    assert result.get('error') is None, result.get('error')


def test_live_seerdb_sessionless_transaction_at_23ai() -> None:
    # A real 23ai-negotiated client begins a sessionless transaction, runs a
    # statement, suspends it, and each TPC switch reaches the backend.
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]
    result: dict = {}
    backend = _DualBackend()

    def run() -> None:
        conn, _ = listen.accept()
        try:
            result['user'] = serve_session(
                PacketStream(conn), backend, field_version=17
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
            result['error'] = exc
        finally:
            conn.close()

    server = threading.Thread(target=run, daemon=True)
    server.start()
    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        txnid = conn.begin_sessionless_transaction('sl-live', timeout=45)
        assert backend.sessionless == ('begin', txnid, 45)
        cursor = conn.cursor()
        cursor.execute('select * from dual')
        assert cursor.fetchone() == ('X',)
        conn.suspend_sessionless_transaction()
        assert backend.sessionless[0] == 'suspend'
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()
    assert result.get('error') is None, result.get('error')


def test_live_seerdb_dual_query() -> None:
    # The 2.1.0 capstone: a real client runs SELECT * FROM DUAL against the
    # Mirror (no Oracle, no Postgres) and gets the DUMMY 'X' row back.
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    server = threading.Thread(
        target=_run_mirror_session, args=(listen, result), daemon=True
    )
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        cursor = conn.cursor()
        cursor.execute('select * from dual')
        row = cursor.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()

    assert result.get('error') is None, result.get('error')
    assert row == ('X',)


def test_live_seerdb_ping() -> None:
    # A real client's ping() (keepalive / pool health check) round-trips against
    # the Mirror and the session stays usable for a following query.
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    server = threading.Thread(
        target=_run_mirror_session, args=(listen, result), daemon=True
    )
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        conn.ping()  # must not hang
        cursor = conn.cursor()
        cursor.execute('select * from dual')
        row = cursor.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()

    assert result.get('error') is None, result.get('error')
    assert row == ('X',)


def test_unsupported_query_errors_but_keeps_connection() -> None:
    # The cardinal rule: a refused query is an ORA error on a HEALTHY
    # connection — never a desync. After the error, the connection still works.
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    server = threading.Thread(
        target=_run_mirror_session, args=(listen, result), daemon=True
    )
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        cursor = conn.cursor()
        with pytest.raises(seerdb.DatabaseError) as excinfo:
            cursor.execute('select * from something_the_backend_refuses')
        assert 'ORA-03001' in str(excinfo.value)
        # The connection survived the error — a valid query still works.
        cursor.execute('select * from dual')
        row = cursor.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()

    assert result.get('error') is None, result.get('error')
    assert row == ('X',)


def test_unencodable_out_bind_errors_but_keeps_connection() -> None:
    # The never-desync rule extends to encoding the reply: a backend that returns
    # an OUT-bind value the wire can't carry must surface a clean ORA error on a
    # HEALTHY connection, not drop it mid-response. The OUT-bind encode step used
    # to run outside the guard, so it desynced (#535).
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    server = threading.Thread(
        target=_run_bad_out_bind_session, args=(listen, result), daemon=True
    )
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        cursor = conn.cursor()
        with pytest.raises(seerdb.DatabaseError) as excinfo:
            cursor.callproc('P', [cursor.var(seerdb.DB_TYPE_NUMBER)])
        # ORA-03115, not ORA-00600: a value the wire cannot carry is a feature
        # gap, and ORA-00600 is Oracle's INTERNAL-error code, which real clients
        # treat as fatal -- so reporting it here killed the session the rest of
        # this test is about proving survives (#875).
        assert 'ORA-03115' in str(excinfo.value)
        # The connection survived the encoding failure — a valid query still works.
        cursor.execute('select * from dual')
        row = cursor.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()

    assert result.get('error') is None, result.get('error')
    assert row == ('X',)


def test_free_temp_drops_the_buffer_and_state_ops_ack() -> None:
    # A programmatic client's FREE_TEMP releases the temp LOB (the Mirror drops
    # its buffer) and OPEN / CLOSE / TRIM / GET_CHUNK_SIZE are acknowledged rather
    # than mis-routed to the read path — no desync (#417). Driven at the handler
    # level: no client in the matrix sends these against the Mirror.
    import struct

    from seerdb.common.tns import _fun_header, decode_lobops_oer, encode_sb4
    from seerdb.common.tns_consts import (
        FIELD_VERSION_11_2,
        TNS_DATA,
        TNS_LOB_OP_FREE_TEMP,
        TNS_LOB_OP_OPEN,
        TTI_LOBOPS,
    )
    from seerdb.server.session import _answer_lobops

    def op_request(operation: int, locator: bytes) -> bytes:
        body = _fun_header(TTI_LOBOPS, 1, FIELD_VERSION_11_2)
        body += bytes([1]) + encode_sb4(len(locator) + 2) + bytes([0])
        body += encode_sb4(0) * 3 + bytes([0, 0, 0]) + encode_sb4(operation)
        body += bytes([0, 0]) + encode_sb4(0) * 2 + bytes([0])
        body += struct.pack('>HHH', 0, 0, 0)
        body += struct.pack('>H', len(locator)) + locator
        return body

    class _FakeStream:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def write_packet(self, packet_type: int, body: bytes) -> None:
            assert packet_type == TNS_DATA
            self.sent.append(body)

    from seerdb.server.session import _TempLobs

    temp_lobs = _TempLobs()
    locator = temp_lobs.mint(is_blob=True)
    temp_lobs.append(locator, b'written-bytes')
    stream: Any = _FakeStream()

    # FREE_TEMP drops the buffer and replies with a success ack.
    _answer_lobops(stream, op_request(TNS_LOB_OP_FREE_TEMP, locator), [], temp_lobs)
    assert temp_lobs.content(locator) == b''
    assert decode_lobops_oer(stream.sent[-1], 6)[0] in (0, 1403)

    # A state op (OPEN) is acknowledged, buffer untouched, no desync.
    temp_lobs.append(locator, b'x')
    _answer_lobops(stream, op_request(TNS_LOB_OP_OPEN, locator), [], temp_lobs)
    assert temp_lobs.content(locator) == b'x'  # OPEN doesn't free
    assert decode_lobops_oer(stream.sent[-1], 6)[0] in (0, 1403)


def _lobops_request(operation: int, locator: bytes, **extra) -> bytes:
    """A real TTI_LOBOPS request, built with the client's own encoder.

    Hand-rolling the body here silently produced one with an EMPTY locator
    field, which every locator-keyed assertion would then have passed or failed
    for the wrong reason."""
    from seerdb.common.tns import encode_dictionary_lobops

    return encode_dictionary_lobops(
        {'seq': 1, 'locator': locator, 'operation': operation, **extra}
    )


class _CollectingStream:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def write_packet(self, packet_type: int, body: bytes) -> None:
        from seerdb.common.tns_consts import TNS_DATA

        assert packet_type == TNS_DATA
        self.sent.append(body)


def test_a_column_lob_read_is_answered_by_its_own_locator() -> None:
    # A fetched column LOB is read by the locator the Mirror minted for it, not
    # by arrival order. The old path popped a row-major queue, which is only
    # right when the client reads every LOB exactly once in the order the
    # locators went out. Read them in any other order -- which the reference
    # client does -- and each got another row's content, or nothing at all once
    # the queue ran dry (#826).
    from seerdb.common.tns import LobEmitLog
    from seerdb.common.tns_consts import TNS_LOB_OP_READ
    from seerdb.server.session import _answer_lobops, _TempLobs

    log = LobEmitLog()
    first = log.record(b'first-row-content', is_clob=False)
    second = log.record(b'second-row-content', is_clob=False)
    assert first != second  # each emitted LOB gets its own locator (#888)

    stream: Any = _CollectingStream()

    def read(locator: bytes) -> bytes:
        # The row-major queue is primed with a decoy. Anything answered from it
        # rather than from the locator shows up as this content, which is what
        # the old path would hand back for every one of these reads.
        _answer_lobops(
            stream,
            _lobops_request(TNS_LOB_OP_READ, locator),
            [(b'decoy-from-the-queue', False)],
            _TempLobs(),
            lob_emit_log=log,
        )
        return stream.sent[-1]

    # The SECOND one first: order must not matter, and the queue is empty
    # throughout, so on master both of these come back with no content.
    reply = read(second)
    assert b'second-row-content' in reply
    assert b'first-row-content' not in reply
    assert b'decoy-from-the-queue' not in reply
    assert b'first-row-content' in read(first)
    # Re-reading one already read still works -- there is no queue to exhaust.
    assert b'second-row-content' in read(second)


def test_a_column_lob_reports_its_own_size_not_zero() -> None:
    # `lob.size()` on a FETCHED LOB consulted the temp-LOB store, which knows
    # nothing about a column locator, so every such size() came back 0 (#826).
    from seerdb.common.tns import LobEmitLog, decode_ub4
    from seerdb.common.tns_consts import TNS_LOB_OP_GET_LENGTH
    from seerdb.server.session import _answer_lobops, _TempLobs

    log = LobEmitLog()
    # A CLOB counts CHARACTERS: the log keeps the `str`, so a multi-byte
    # character must not inflate the answer the way its UTF-16BE buffer would.
    clob = log.record('caf\u00e9' + 'x' * 96, is_clob=True)
    blob = log.record(b'\x00' * 250, is_clob=False)
    stream: Any = _CollectingStream()

    def length_of(locator: bytes) -> int:
        # The locator goes RAW, and the reply echoes it VERBATIM. This fixture
        # used to send it ub2-prefixed, on the #826 reading that GET_LENGTH
        # differed from READ. It does not: a real Oracle locator OPENS with its
        # own 2-byte length (`00 70` for a 114-byte one), which reads exactly
        # like a framing prefix and is not one. Stripping it and re-adding a
        # prefix happens to reproduce the bytes for such a locator, which is why
        # the mistake survived -- and corrupts any locator whose first two bytes
        # are something else, such as the Mirror's own (#903/#887).
        _answer_lobops(
            stream,
            _lobops_request(TNS_LOB_OP_GET_LENGTH, locator, locator_prefixed=False),
            [],
            _TempLobs(),
            lob_emit_log=log,
        )
        # The reply is the echoed locator then a ub4 length (§14): skip the
        # verbatim locator echo -- no length prefix -- and read what follows.
        body = stream.sent[-1]
        head = 1 + len(locator)
        return decode_ub4(body[head:])[0]

    assert length_of(clob) == 100
    assert length_of(blob) == 250


# --- PL/SQL OUT-bind helpers (#483) --------------------------------------------


def test_is_plsql_block_detects_begin_and_declare() -> None:
    from seerdb.server.session import _is_plsql_block

    assert _is_plsql_block('BEGIN p(:1); END;')
    assert _is_plsql_block('  declare x number; begin null; end;')
    assert not _is_plsql_block('SELECT 1 FROM dual')
    assert not _is_plsql_block('INSERT INTO t VALUES (:1)')


def test_bind_vars_wraps_block_binds_with_type_and_size() -> None:
    from seerdb.common.tns import ExecRequest
    from seerdb.common.tns_consts import (
        TNS_TYPE_NUMBER,
        TNS_TYPE_REFCURSOR,
        TNS_TYPE_VARCHAR,
    )
    from seerdb.server.backend import BindVar
    from seerdb.server.session import _bind_vars

    block = ExecRequest(
        sql='BEGIN p(:1, :2); END;',
        cursor=0,
        bind_count=2,
        fetch=0,
        binds=[7, None],
        bind_meta=[(TNS_TYPE_NUMBER, 22), (TNS_TYPE_VARCHAR, 32767)],
    )
    wrapped = _bind_vars(block)
    assert all(isinstance(b, BindVar) for b in wrapped)
    assert [b.array_size for b in wrapped] == [0, 0]  # scalars
    assert (wrapped[0].value, wrapped[0].tns_type, wrapped[0].max_size) == (
        7,
        TNS_TYPE_NUMBER,
        22,
    )
    assert wrapped[1].max_size == 32767  # the OUT VARCHAR buffer size

    # A non-block statement passes its plain values through unchanged.
    dml = ExecRequest(
        sql='INSERT INTO t VALUES (:1)',
        cursor=0,
        bind_count=1,
        fetch=0,
        binds=[7],
        bind_meta=[(TNS_TYPE_NUMBER, 22)],
    )
    assert _bind_vars(dml) == [7]

    # A REF CURSOR bind also rides the OUT path — the backend opens the cursor
    # and its rows come back on a parked cursor id (#483).
    refc = ExecRequest(
        sql='BEGIN p(:1); END;',
        cursor=0,
        bind_count=1,
        fetch=0,
        binds=[None],
        bind_meta=[(TNS_TYPE_REFCURSOR, 1)],
    )
    wrapped_rc = _bind_vars(refc)
    assert len(wrapped_rc) == 1
    assert wrapped_rc[0].tns_type == TNS_TYPE_REFCURSOR


def test_bind_vars_carries_an_array_binds_capacity() -> None:
    # An associative-array bind's elements and capacity reach the backend (#743).
    from seerdb.common.tns import ExecRequest
    from seerdb.common.tns_consts import TNS_TYPE_NUMBER
    from seerdb.server.session import _bind_vars

    block = ExecRequest(
        sql='BEGIN p(:1, :2); END;',
        cursor=0,
        bind_count=2,
        fetch=0,
        binds=[[1, 2], []],
        bind_meta=[(TNS_TYPE_NUMBER, 22), (TNS_TYPE_NUMBER, 22)],
        bind_arrays=[10, 4],
    )
    wrapped = _bind_vars(block)
    assert [(b.value, b.array_size) for b in wrapped] == [([1, 2], 10), ([], 4)]


def test_bind_vars_wraps_only_a_null_of_an_ordinary_statement() -> None:
    # A NULL bind carries no type of its own, so it goes over with the type the
    # client declared for it; a value goes over bare (#699).
    from seerdb.common.tns import ExecRequest
    from seerdb.common.tns_consts import TNS_TYPE_NUMBER, TNS_TYPE_VARCHAR
    from seerdb.server.backend import BindVar
    from seerdb.server.session import _bind_vars

    select = ExecRequest(
        sql='SELECT id FROM t WHERE CASE WHEN :1 IS NOT NULL THEN :1 ELSE d END = :2',
        cursor=0,
        bind_count=2,
        fetch=0,
        binds=[None, 'x'],
        bind_meta=[(TNS_TYPE_NUMBER, 22), (TNS_TYPE_VARCHAR, 32)],
    )
    wrapped = _bind_vars(select)
    assert wrapped[1] == 'x'
    assert isinstance(wrapped[0], BindVar)
    assert (wrapped[0].value, wrapped[0].tns_type) == (None, TNS_TYPE_NUMBER)
    # A shape mismatch passes everything through untouched.
    mismatched = ExecRequest(
        sql=select.sql,
        cursor=0,
        bind_count=2,
        fetch=0,
        binds=[None, 'x'],
        bind_meta=[(TNS_TYPE_NUMBER, 22)],
    )
    assert _bind_vars(mismatched) == [None, 'x']


def test_oci_sequence_advances_per_reply() -> None:
    # The per-session OER end-to-end sequence counter yields a fresh, advancing
    # value on each call, starting at 1 — the Mirror threads seq.next() into every
    # OCI status reply so the field advances like a real server's instead of
    # repeating the frozen capture constant (§36).
    from seerdb.common.tns import encode_status_oci
    from seerdb.server.session import _OciSequence

    seq = _OciSequence()
    assert [seq.next() for _ in range(4)] == [1, 2, 3, 4]

    # Threading one counter through consecutive status replies advances the OER
    # sequence byte (frame offset 40: the compact OER sits at offset 32 with three
    # leading pad bytes, and the sequence is at the OER's own offset 5) reply over
    # reply — and nothing else in the frame changes.
    seq2 = _OciSequence()
    first = encode_status_oci(seq2.next())
    second = encode_status_oci(seq2.next())
    assert first[40] == 1
    assert second[40] == 2
    assert [i for i in range(len(first)) if first[i] != second[i]] == [40]


# --- sqlplus PASSWORD / OCI changepassword (#21) ------------------------------


def _oci_changepassword_frame(
    conn_key: bytes, user: bytes, old: str, new: str
) -> bytes:
    """Build a synthetic OCI (deadbeef) changepassword TTI_AUTH, as sqlplus's
    PASSWORD sends inside its TTI_80SES piggyback (unwrapped): AUTH_PASSWORD
    (current) + AUTH_NEWPASSWORD (new), each hex(AES-cipher under conn_key)."""
    from seerdb.common import oci
    from seerdb.common.crypto import encrypt_password
    from seerdb.server.auth import encode_kv_oci

    ind = oci.OCI_INDICATOR
    # header: TTI_FUN + AUTH subtype, indicators at offsets 3/19/35/43, then the
    # ub1-length username at offset 51 (see auth._parse_oci_fun_username).
    header = (
        b'\x03\x73\x00'
        + ind
        + b'\x00' * 8
        + ind
        + b'\x00' * 8
        + ind
        + ind
        + bytes([len(user)])
        + user
    )
    old_hex = encrypt_password(conn_key, old.encode()).hex().upper().encode()
    new_hex = encrypt_password(conn_key, new.encode()).hex().upper().encode()
    return (
        header
        + encode_kv_oci(b'AUTH_PASSWORD', old_hex)
        + encode_kv_oci(b'AUTH_NEWPASSWORD', new_hex)
    )


def test_is_version_call_oci_distinguishes_changepassword() -> None:
    # The version call and the changepassword both arrive as a TTI_80SES
    # (0x11 0x6b) piggyback with the same 15-byte prefix; only the wrapped TTI
    # function differs (0x3b version vs. TTI_AUTH). is_version_call_oci must key
    # on the inner function, else a changepassword is answered with the banner.
    from seerdb.common.tns import is_version_call_oci, strip_oci_piggyback

    prefix = bytes.fromhex('116b043b000000e507000001000000')
    version = prefix + bytes.fromhex('033b') + b'\x00' * 4
    change = prefix + bytes.fromhex('0373') + b'\x00' * 4
    assert is_version_call_oci(version)
    assert not is_version_call_oci(change)
    # strip_oci_piggyback unwraps the TTI_80SES wrapper to the inner TTI_FUN call.
    assert strip_oci_piggyback(change)[:2] == bytes.fromhex('0373')
    assert strip_oci_piggyback(version)[:2] == bytes.fromhex('033b')


def test_oci_changepassword_decrypts_and_applies() -> None:
    from seerdb.common.tns import encode_changepassword_status_oci
    from seerdb.server.auth import parse_changepassword_oci
    from seerdb.server.session import _answer_changepassword_oci

    conn_key = bytes(range(24))
    frame = _oci_changepassword_frame(conn_key, b'PWTEST', 'oldpw', 'newpw')

    # The parser recovers the two ciphertexts (un-hexed).
    from seerdb.common.crypto import encrypt_password

    user, old_c, new_c = parse_changepassword_oci(frame)
    assert user == b'PWTEST'
    assert old_c == encrypt_password(conn_key, b'oldpw')
    assert new_c == encrypt_password(conn_key, b'newpw')

    calls: list = []

    class _Backend:
        def change_password(self, u: str, old: str, new: str) -> None:
            calls.append((u, old, new))

    class _Stream:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def write_packet(self, ptype: int, body: bytes) -> None:
            self.sent.append(body)

    class _Seq:
        def next(self) -> int:
            return 1

    stream: Any = _Stream()
    backend: Any = _Backend()
    seq: Any = _Seq()
    _answer_changepassword_oci(stream, backend, frame, conn_key, 'PWTEST', seq)
    # decrypted the pair and drove the backend change, then acked with the
    # OCIPasswordChange success frame sqlplus renders as "Password changed".
    assert calls == [('PWTEST', 'oldpw', 'newpw')]
    assert stream.sent[-1] == encode_changepassword_status_oci()


def test_oci_changepassword_unsupported_backend_replies_ora_error() -> None:
    from seerdb.server.session import _answer_changepassword_oci

    conn_key = bytes(range(24))
    frame = _oci_changepassword_frame(conn_key, b'PWTEST', 'oldpw', 'newpw')

    class _Stream:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def write_packet(self, ptype: int, body: bytes) -> None:
            self.sent.append(body)

    class _Seq:
        def next(self) -> int:
            return 1

    stream: Any = _Stream()
    # A backend with no change_password: reply with an ORA error, never desync.
    no_change_password: Any = object()
    seq: Any = _Seq()
    _answer_changepassword_oci(
        stream, no_change_password, frame, conn_key, 'PWTEST', seq
    )
    assert stream.sent, 'must answer even when unsupported'
    assert b'ORA-01031' in stream.sent[-1]


class _LeakyBackend(_DualBackend):
    # A backend that embeds a seerdb client talking to a NEWER Oracle (the
    # passthrough in front of a 23ai) leaves the codec's field-version context
    # variables at the upstream's version after every call — decode_packet /
    # encode_dictionary_exec set them per message in the calling thread. This
    # stand-in does exactly that on each execute and records the binds it got.
    def __init__(self) -> None:
        self.binds: list = []

    def execute(self, sql: str, binds=()) -> Result:
        from seerdb.common.tns import _DECODE_FIELD_VERSION, _ENCODE_FIELD_VERSION
        from seerdb.common.tns_consts import FIELD_VERSION_23_1

        self.binds.append(list(binds))
        _DECODE_FIELD_VERSION.set(FIELD_VERSION_23_1)
        _ENCODE_FIELD_VERSION.set(FIELD_VERSION_23_1)
        if sql.lstrip().upper().startswith('INSERT'):
            return Result(rowcount=1)
        return super().execute(sql, binds)


def test_backend_codec_context_does_not_leak_into_the_session() -> None:
    # A 32K+ string bind goes out in the 11g chunked LONG layout. If the backend's
    # own codec state (field version 23ai) leaked into the session thread, the
    # Mirror would decode the client's 11g chunks with the 12.2+ framing and hand
    # the backend a garbled value (observed live: 51951 chars for 51200, first
    # character dropped). The backend must see the client's exact value.
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    backend = _LeakyBackend()
    result: dict = {}

    def run() -> None:
        conn, _ = listen.accept()
        try:
            result['user'] = serve_session(PacketStream(conn), backend)
        except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
            result['error'] = exc
        finally:
            conn.close()

    server = threading.Thread(target=run, daemon=True)
    server.start()

    text = '0123456789abcdef' * (50 * 1024 // 16)  # 51200 chars, chunked on the wire
    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        cursor = conn.cursor()
        cursor.execute('select * from dual')  # the backend pollutes the context
        cursor.execute('INSERT INTO t (c) VALUES (:c)', {'c': text})
        cursor.execute('select * from dual')  # and the response path still works
        row = cursor.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()

    assert result.get('error') is None, result.get('error')
    assert row == ('X',)
    assert backend.binds[1] == [text]


# The end-to-end tracing piggyback modern sqlplus sends immediately after login,
# captured live (sqlplus 23.26, field version 6): the 0x11 0x87 header, a fixed
# block whose marshalled fields include client pointer values, the module name
# "SQL*Plus" as its only set value, and then the real call -- 03 0e, a commit.
_OCI_E2E_PIGGYBACK_SQLPLUS = bytes.fromhex(
    '1187020000000000000000000000000000000018000000000000000000000000'
    '0000000000000000000000feffffffffffffff18000000000000002806734615'
    '5600000000000000000000000000000000000000000000000001000000000000'
    '0000000000000000000000000000000000000000000000000000000000000000'
    '00000000000000000000000853514c2a506c7573030e03'
)


def test_oci_tracing_piggyback_is_walked_to_the_call_behind_it() -> None:
    # Modern sqlplus bundles its tracing attributes as a piggyback in FRONT of a
    # real call. The OCI loop stripped only the close-cursors and TTI_80SES
    # wrappers, so this one fell through and the session was closed on the
    # client -- sqlplus then reported ORA-03114 for every statement (#825).
    #
    # Its thick-OCI block is not decoded: only one live sample exists and
    # several layouts fit it. The walker reads the length-prefixed values and
    # then CHECKS its landing, returning None rather than guessing, so an
    # unknown shape becomes a refusal instead of a desynchronised stream.
    from seerdb.common.oci import strip_oci_e2e_piggyback
    from seerdb.common.tns_consts import TTI_COMMIT, TTI_FUN

    behind = strip_oci_e2e_piggyback(_OCI_E2E_PIGGYBACK_SQLPLUS)
    assert behind == bytes([TTI_FUN, TTI_COMMIT, 3])

    # A shape it cannot walk is reported, never guessed at: a block whose values
    # run off the end, and one that never reaches a TTI_FUN.
    assert strip_oci_e2e_piggyback(_OCI_E2E_PIGGYBACK_SQLPLUS[:145]) is None
    assert strip_oci_e2e_piggyback(bytes([0x11, 0x87]) + bytes(200)) is None


def test_oci_loop_refuses_an_unknown_call_instead_of_closing_the_session() -> None:
    # The OCI loop used to `return` on any call it did not implement, which
    # closes the connection: sqlplus renders that as ORA-03113 / ORA-03114 for
    # everything afterwards, indistinguishable from the server crashing, and one
    # unimplemented call takes the whole session with it. The thin loop was
    # taught to refuse in #832/#836; this is the same rule for the thick one.
    from seerdb.common.tns_consts import TNS_DATA
    from seerdb.server.session import _serve_oci_session

    unknown = bytes([TTI_FUN, 0xFA, 1])  # a function the Mirror does not serve

    class _Stream:
        def __init__(self) -> None:
            self.inbox = [(TNS_DATA, unknown), (TNS_DATA, unknown), None]
            self.sent: list[tuple[int, bytes]] = []

        def read_packet(self, **_kw):
            return self.inbox.pop(0)

        def write_packet(self, ptype: int, body: bytes, **_kw) -> None:
            self.sent.append((ptype, body))

    stream: Any = _Stream()
    backend: Any = object()
    assert _serve_oci_session(stream, backend, 'PYO') == 'PYO'
    # BOTH calls were answered -- the session survived the first refusal, which
    # is the whole point -- and each answer names the error the client knows.
    assert len(stream.sent) == 2
    for _ptype, body in stream.sent:
        assert b'ORA-03115' in body


def test_oci_loop_answers_a_break_marker() -> None:
    """A break / reset marker gets a marker back, not silence.

    The thick-OCI client sends one to resynchronise the line and then blocks
    until the server answers; ignoring it wedged the session until the client
    timed out.
    """
    from seerdb.common.tns_consts import TNS_MARKER, TNS_MARKER_TYPE_RESET
    from seerdb.server.session import _serve_oci_session

    class _Stream:
        def __init__(self) -> None:
            self.inbox = [(TNS_MARKER, bytes([1, 0, TNS_MARKER_TYPE_RESET])), None]
            self.sent: list[tuple[int, bytes]] = []

        def read_packet(self, **_kw):
            return self.inbox.pop(0)

        def write_packet(self, ptype: int, body: bytes, **_kw) -> None:
            self.sent.append((ptype, body))

    stream: Any = _Stream()
    backend: Any = object()
    assert _serve_oci_session(stream, backend, 'PYO') == 'PYO'
    # Exactly one reply, and it is a reset marker: replying to every marker
    # ping-pongs the two ends into a reset storm.
    assert stream.sent == [(TNS_MARKER, bytes([1, 0, TNS_MARKER_TYPE_RESET]))]


def _run_mirror_at_tns_version(listen: socket.socket, result: dict, tns: int) -> None:
    conn, _ = listen.accept()
    try:
        result['user'] = serve_session(
            PacketStream(conn), _DualBackend(), field_version=8, tns_version=tns
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
        result['error'] = exc
    finally:
        conn.close()


def test_live_seerdb_over_large_sdu_framing() -> None:
    """A 12.2 Mirror frames the DATA stream with the 4-byte packet length.

    The version alone drives it: at >= 315 both ends switch after the ACCEPT, so
    a login plus a query round-trip here proves the server's read and write paths
    agree with the real client's on the wider header.
    """
    from seerdb.server.handshake import TNS_VERSION_12_2

    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    server = threading.Thread(
        target=_run_mirror_at_tns_version,
        args=(listen, result, TNS_VERSION_12_2),
        daemon=True,
    )
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        assert conn._large_packets is True, 'client must switch to the 4-byte header'
        cursor = conn.cursor()
        cursor.execute('select * from dual')
        assert cursor.fetchone() == ('X',)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()
    assert result.get('error') is None, result.get('error')


def _run_mirror_identity_at(listen: socket.socket, result: dict, version: int) -> None:
    conn, _ = listen.accept()
    try:
        result['user'] = serve_session(
            PacketStream(conn), _DualBackend(), field_version=version
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
        result['error'] = exc
    finally:
        conn.close()


@pytest.mark.parametrize(
    ('version', 'expected'), [(6, '11.2.0.2.0'), (8, '12.2.0.1.0')]
)
def test_live_client_reads_the_release_for_the_field_version(
    version: int, expected: str
) -> None:
    """A real client's connection.version reflects what the Mirror advertises.

    The release rides in the login result as AUTH_VERSION_NO, so this proves the
    identity reaches the wire rather than only the table that builds it.
    """
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    server = threading.Thread(
        target=_run_mirror_identity_at, args=(listen, result, version), daemon=True
    )
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        assert conn.version == expected
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()
    assert result.get('error') is None, result.get('error')


@pytest.mark.parametrize(('version', 'expected'), [(6, b'11g'), (8, b'12c')])
def test_oci_banner_follows_the_advertised_release(
    version: int, expected: bytes
) -> None:
    """sqlplus's "Connected to:" banner reports the release, not a fixed 11g one.

    Driven offline through the OCI loop: a version call in, the banner reply out.
    """
    from seerdb.common import oci
    from seerdb.common.tns import _OCI_80SES_FIXED
    from seerdb.server.identity import server_identity
    from seerdb.server.session import _serve_oci_session

    version_call = oci.OCI_PIGGYBACK_80SES + bytes(_OCI_80SES_FIXED - 2) + b'\x03\x3b'

    class _Stream:
        def __init__(self) -> None:
            self.inbox = [(TNS_DATA, version_call), None]
            self.sent: list[bytes] = []

        def read_packet(self, **_kw):
            return self.inbox.pop(0)

        def write_packet(self, ptype: int, body: bytes, **_kw) -> None:
            self.sent.append(body)

    stream: Any = _Stream()
    backend: Any = object()
    _serve_oci_session(stream, backend, 'PYO', None, server_identity(version))
    assert stream.sent, 'the version call must be answered with a banner'
    assert expected in stream.sent[0]


class _VersionedBackend(_DualBackend):
    # A backend that declares the protocol version it presents, the way the
    # PostgreSQL demo pins 11.2 and a passthrough presents its target's release.
    field_version = FIELD_VERSION_12_2


def _run_mirror_versioned_backend(listen: socket.socket, result: dict) -> None:
    conn, _ = listen.accept()
    try:
        # No field_version argument: the backend's declaration must drive it.
        result['user'] = serve_session(PacketStream(conn), _VersionedBackend())
    except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
        result['error'] = exc
    finally:
        conn.close()


def test_backend_declared_field_version_drives_the_session() -> None:
    # The backend, not the serve() flag, chooses the advertised version: a
    # _VersionedBackend pinning 12.2 makes a live client negotiate to 12.2 and
    # read the 12.2 release, though serve_session was given no field_version.
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]
    result: dict = {}
    server = threading.Thread(
        target=_run_mirror_versioned_backend, args=(listen, result), daemon=True
    )
    server.start()
    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        assert conn.field_version == FIELD_VERSION_12_2
        assert conn.server_version == VERSION_12_2_0_1
        cursor = conn.cursor()
        cursor.execute('select * from dual')
        assert cursor.fetchone() == ('X',)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()
    assert result.get('error') is None, result.get('error')
    assert result.get('user') == 'PYO'


class _FastAuthBackend(_DualBackend):
    # Declares a 23ai field version, so a live client negotiates fv 24 and logs in
    # through the 23ai FAST_AUTH bundle rather than the legacy three messages.
    field_version = FIELD_VERSION_23_4


def _run_mirror_fast_auth(listen: socket.socket, result: dict) -> None:
    conn, _ = listen.accept()
    try:
        result['user'] = serve_session(PacketStream(conn), _FastAuthBackend())
    except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
        result['error'] = exc
    finally:
        conn.close()


def test_live_seerdb_fast_auth_login_at_23ai() -> None:
    # 23ai server-side fast-auth (§20): a real client at field version 24 cannot
    # use the legacy OSESSKEY handshake, so it sends one FAST_AUTH packet bundling
    # PRO + DTY + OSESSKEY. The Mirror unbundles it, replies with the three
    # responses concatenated, and finishes O5LOGON — the client logs in and reads
    # the 23ai release. (The fv 24 query path is a separate increment.)
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]
    result: dict = {}
    server = threading.Thread(
        target=_run_mirror_fast_auth, args=(listen, result), daemon=True
    )
    server.start()
    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        assert conn.field_version == FIELD_VERSION_23_4
        assert conn.server_version == VERSION_23_1_162_0
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()
    assert result.get('error') is None, result.get('error')
    assert result.get('user') == 'PYO'


class _Fv16Backend(_DualBackend):
    field_version = FIELD_VERSION_21_1  # 21c


def test_passthrough_detect_version_reads_the_negotiated_version() -> None:
    # The passthrough auto-detects its target's release by probing it once
    # (OraclePassthroughBackend.detect_version). Point it at a Mirror advertising
    # 21c (fv 16) and it reads that back — the value it then presents to its own
    # clients so a Mirror in front of a real 21c server is 21c.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'examples'))
    from oracle_passthrough_backend import OraclePassthroughBackend

    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    def run() -> None:
        conn, _ = listen.accept()
        try:
            serve_session(PacketStream(conn), _Fv16Backend())
        except Exception:  # noqa: BLE001 - the probe just logs in and disconnects
            pass
        finally:
            conn.close()

    server = threading.Thread(target=run, daemon=True)
    server.start()
    try:
        version = OraclePassthroughBackend.detect_version(
            '127.0.0.1', port, 'XE', 'PYO', 'pyo123', timeout=5000
        )
        assert version == FIELD_VERSION_21_1
    finally:
        server.join(timeout=5)
        listen.close()

    # An unreachable target probes to None (the caller falls back to the default).
    assert (
        OraclePassthroughBackend.detect_version(
            '127.0.0.1', 1, 'XE', 'PYO', 'pyo123', timeout=1000
        )
        is None
    )


def test_unimplemented_ttc_function_is_refused_not_ignored() -> None:
    # The cardinal rule again, one layer down: a call the Mirror does not
    # implement must be ANSWERED. The dispatch chain used to have no else, so an
    # unknown TTC function fell through to the next read and the client blocked
    # forever waiting for a reply that was never coming (#832). A hang is the
    # worst possible failure here -- it stalls whatever is driving the session
    # rather than failing one call -- so this asserts the refusal arrives, and
    # arrives quickly.
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    server = threading.Thread(
        target=_run_mirror_session, args=(listen, result), daemon=True
    )
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        # A function code no client sends, on purpose: this test used a real
        # unimplemented function twice (78 until #833 implemented it, then 4
        # until #854 did) and each time it quietly stopped testing anything
        # until someone noticed. The refusal path is the same for any code.
        conn.send(TNS_DATA, bytes([TTI_FUN, 250]) + bytes(8))
        received = conn._next_data_packet()
        assert received is not False, 'the Mirror answered nothing -- it hung'
        assert b'ORA-03115' in received[1]
        # ... and the session is still usable, exactly as after any other error.
        cursor = conn.cursor()
        cursor.execute('select * from dual')
        row = cursor.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()

    assert row == ('X',)


def test_unhandled_piggyback_is_refused_not_dropped() -> None:
    # The sibling of the test above (#836). _skip_piggybacks stops on a piggyback
    # it does not know rather than guess its length -- guessing would desync the
    # stream -- which left the dispatch unable to reach the call behind it. The
    # message was then dropped silently and the client blocked forever, because a
    # piggyback is only a PREFIX to a real call whose reply is already awaited.
    #
    # Refusing keeps the stream in sync, so the session answers the next
    # statement normally. (A real client that set a connection attribute will
    # re-send the same piggyback on every call, so ITS connection stays unusable
    # until the piggyback is implemented -- but that is the client holding a
    # pending attribute, not a desync here.)
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(1)
    port = listen.getsockname()[1]

    result: dict = {}
    server = threading.Thread(
        target=_run_mirror_session, args=(listen, result), daemon=True
    )
    server.start()

    conn = seerdb.connect(
        host='127.0.0.1',
        port=port,
        user='PYO',
        password='pyo123',
        service_name='XE',
        timeout=5000,
    )
    try:
        # 205 is the end-user security context piggyback: real, tcps-only, and
        # deliberately not handled here -- so it stands in for "a piggyback the
        # Mirror does not know". (This test used SET_SCHEMA until #837
        # implemented it; the example has to be one that is still unhandled, or
        # the test quietly stops testing anything.)
        conn.send(TNS_DATA, bytes([TTI_MSG_TYPE_PIGGYBACK, 205]) + bytes(8))
        received = conn._next_data_packet()
        assert received is not False, 'the Mirror answered nothing -- it hung'
        assert b'ORA-03115' in received[1]
        # The refusal names the piggyback, so the gap is identifiable from the
        # error alone rather than needing a packet capture.
        assert b'piggyback 205' in received[1]
        # The stream is still in sync: an ordinary statement runs.
        cursor = conn.cursor()
        cursor.execute('select * from dual')
        row = cursor.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.join(timeout=5)
        listen.close()

    assert row == ('X',)


def test_each_query_gets_its_own_cursor_id() -> None:
    # A client reads the server's cursor id out of the reply terminator and
    # caches it against the statement, then re-executes by that id. The
    # terminator was a replayed 11g capture with cursor_id=1 baked in, so every
    # query in a session claimed to be cursor 1 -- and a re-execute by id would
    # have run whichever statement was most recently called 1 (#840).
    #
    # Asserted through _Cursors rather than the wire: the ids are what must
    # differ, and the terminator merely carries them.
    from seerdb.server.session import _Cursors

    cursors = _Cursors()
    first = cursors.open_query('select 7 from dual')
    second = cursors.open_query('select 42 from dual')
    assert first != second
    assert cursors.query_sql(first) == 'select 7 from dual'
    assert cursors.query_sql(second) == 'select 42 from dual'
    # An id nobody minted resolves to nothing, rather than to a stale statement.
    assert cursors.query_sql(9999) is None
    # A parked result keeps its statement too, so draining it and re-executing
    # by the same id stay consistent.
    parked = cursors.open([], [(1,)], sql='select 99 from dual')
    assert cursors.query_sql(parked) == 'select 99 from dual'
    assert parked not in (first, second)


def test_the_end_of_fetch_terminator_carries_the_cursor_id() -> None:
    # The captured 11g terminator stays byte-identical when the caller has no
    # cursor of its own, so nothing changes for the paths that never had one;
    # a real id changes only that field.
    from seerdb.common.tns import _ENCODE_OER_SEQ, _END_OF_FETCH, _end_of_fetch

    assert _end_of_fetch() == _END_OF_FETCH
    assert _end_of_fetch(1) == _END_OF_FETCH

    # Pinned to the sequence the capture was taken at, a real cursor id changes
    # that field and nothing else -- the terminator keeps its captured shape.
    token = _ENCODE_OER_SEQ.set(4)
    try:
        assert _end_of_fetch(7) != _END_OF_FETCH
        assert len(_end_of_fetch(7)) == len(_END_OF_FETCH)
    finally:
        _ENCODE_OER_SEQ.reset(token)


def test_the_thin_terminator_advances_its_oer_sequence() -> None:
    # The thin reply path used to emit the frozen sequence every captured status
    # was decoded with, so a whole session repeated one number while the
    # thick/OCI path advanced a real counter (#842). The field is diagnostic --
    # no client validates it -- so what is asserted is that it MOVES and that it
    # reaches the wire, not any particular value.
    from seerdb.common.tns import _ENCODE_OER_SEQ, _end_of_fetch, encode_status

    seen = set()
    for n in (1, 2, 3):
        token = _ENCODE_OER_SEQ.set(n)
        try:
            seen.add((encode_status(1, cursor_id=9), _end_of_fetch(7)))
        finally:
            _ENCODE_OER_SEQ.reset(token)
    # Three sequence values, three distinct pairs of encoded replies.
    assert len(seen) == 3

    # A caller that pins `seq` is reproducing a captured frame and is unaffected
    # by the session counter -- that is what keeps _END_OF_FETCH verbatim.
    from seerdb.common.tns import _encode_oer

    token = _ENCODE_OER_SEQ.set(99)
    try:
        assert _encode_oer(1, 0, 0, b'', seq=4) == _encode_oer(1, 0, 0, b'', seq=4)
        assert _encode_oer(1, 0, 0, b'', seq=4) != _encode_oer(1, 0, 0, b'')
    finally:
        _ENCODE_OER_SEQ.reset(token)


def test_reexecute_runs_the_cursors_own_statement() -> None:
    # A re-execute names a cursor and carries no SQL, so it can only be served if
    # the cursor identifies one statement (#833, on top of #840). The danger this
    # guards is not a crash but a WRONG ANSWER: re-executing cursor 2 must run
    # cursor 2's query, not whatever ran most recently.
    from seerdb.common.tns import _DECODE_FIELD_VERSION, parse_reexecute
    from seerdb.server.session import _Cursors

    cursors = _Cursors()
    first = cursors.open_query('select 7 from dual')
    second = cursors.open_query('select 42 from dual')
    assert cursors.query_sql(first) == 'select 7 from dual'
    assert cursors.query_sql(second) == 'select 42 from dual'

    # The request layout, byte-for-byte from a live 23ai capture: cursor 1,
    # two iterations, options 0x20. It only decodes at the field version the
    # session negotiated -- the fv24 token ahead of the fields shifts everything
    # by one byte -- so the parse is version-sensitive and worth pinning.
    raw = bytes.fromhex('034e040001010102012000')
    _DECODE_FIELD_VERSION.set(24)
    request = parse_reexecute(raw)
    assert (request.cursor, request.fetch, request.options) == (1, 2, 0x20)

    # Re-parking under the SAME id: the client is never told a new one, so its
    # follow-up fetches would otherwise address a cursor it does not hold.
    cursors.reopen(first, [], [(7,), (8,)], sql='select 7 from dual')
    assert cursors.has(first)
    assert cursors.query_sql(first) == 'select 7 from dual'
    # Draining it leaves the statement resolvable, so a later re-execute still
    # knows what to run.
    cursors.take(first, 99)
    assert cursors.query_sql(first) == 'select 7 from dual'


def test_parse_reexecute_decodes_fresh_bind_rows_by_the_cached_types() -> None:
    # The plain re-execute (func 4, #854) carries one RXD row per execution and
    # no OACs: the values are typed by the execute that opened the cursor. Three
    # requests byte-for-byte from a live 23ai (fv24, so the ub8 token follows
    # the sequence byte), each with the bind types its opening execute declared.
    from seerdb.common.tns import _DECODE_FIELD_VERSION, parse_reexecute
    from seerdb.common.tns_consts import TNS_TYPE_NUMBER, TNS_TYPE_VARCHAR

    number = (TNS_TYPE_NUMBER, 1, 22, b'')
    text = (TNS_TYPE_VARCHAR, 1, 20, b'')
    insert = bytes.fromhex('03 04 08 00 01 03 01 01 00 00 07 02 c1 03 04 72 6f 77 32')
    update = bytes.fromhex('03 04 0b 00 01 05 01 01 00 00 07 01 79 02 c1 06')
    select = bytes.fromhex('03 04 0d 00 01 04 01 01 00 00 07 02 c1 04')
    token = _DECODE_FIELD_VERSION.set(24)
    try:
        # INSERT ... VALUES (:1, :2) re-run with (2, 'row2') on cursor 3.
        request = parse_reexecute(insert, bind_types=[number, text])
        assert (request.cursor, request.fetch, request.autocommit) == (3, 1, False)
        assert request.bind_rows == [[2, 'row2']]
        # UPDATE ... SET s = :1 WHERE id > :2 with ('y', 5): the row follows the
        # bind order, not the type.
        request = parse_reexecute(update, bind_types=[text, number])
        assert request.cursor == 5
        assert request.bind_rows == [['y', 5]]
        # A query re-executed with prefetching off is the same message.
        request = parse_reexecute(select, bind_types=[number])
        assert (request.cursor, request.bind_rows) == (4, [[3]])
        # Without the types only the header is read -- what the dispatch does
        # first, to learn which cursor's types to look up.
        assert parse_reexecute(insert).cursor == 3
        assert parse_reexecute(insert).bind_rows == []
        # executemany's second run: `iterations` executions, one row each; and
        # options_2 bit 0 is the client's autocommit.
        two = insert[:6] + bytes.fromhex('01 02 00 01 01') + insert[10:] + insert[10:]
        request = parse_reexecute(two, bind_types=[number, text])
        assert (request.fetch, request.autocommit) == (2, True)
        assert request.bind_rows == [[2, 'row2'], [2, 'row2']]
        # A row cut short is reported, so the spanning reader fetches the rest.
        with pytest.raises(Truncated):
            parse_reexecute(insert[:-2], bind_types=[number, text])
    finally:
        _DECODE_FIELD_VERSION.reset(token)


def test_a_zero_prefetch_execute_sends_no_rows_but_parks_them() -> None:
    # An execute's fetch field is the client's PREFETCH, and a zero there means
    # "send me no rows on the execute" -- the client has allocated no fetch
    # buffer and will ask with TTI_FETCH. Reading it as "send everything"
    # overran the reference thin client's define array and killed it inside its
    # own row decoder (IndexError in _process_row_data), before the reply could
    # become an error anyone could read (#856). A real 23ai answers a prefetch-0
    # execute with describe + status and no row data at all.
    from seerdb.common.tns import ExecRequest, encode_query_response
    from seerdb.common.tns_consts import TNS_TYPE_NUMBER
    from seerdb.server.session import _answer_query, _Cursors, _prefetch_batch

    column = ColumnMeta(
        name=b'ID', data_type=TNS_TYPE_NUMBER, data_length=22, max_size=22
    )

    class _Rows:
        capabilities: frozenset = frozenset()

        def execute(self, sql: str, binds=()) -> Result:
            return Result(columns=[column], rows=[(1,), (2,), (3,)])

        def commit(self) -> None:
            pass

    class _Stream:
        def __init__(self) -> None:
            self.sent: list[tuple[int, bytes]] = []

        def write_packet(self, packet_type: int, body: bytes) -> None:
            self.sent.append((packet_type, body))

    def run(fetch: int) -> tuple[Any, Any]:
        stream: Any = _Stream()
        cursors = _Cursors()
        request = ExecRequest(
            sql='select id from t', cursor=0, bind_count=0, fetch=fetch
        )
        _answer_query(stream, _Rows(), request, cursors)  # type: ignore[arg-type]
        return stream, cursors

    # Prefetch 0: describe and a "more rows on cursor 1" terminator, not one row.
    stream, cursors = run(0)
    assert stream.sent == [
        (TNS_DATA, encode_query_response([column], [], cursor_id=1, more=True))
    ]
    # ...and every row waits on the cursor the terminator named, so the client's
    # TTI_FETCH gets them. They are delivered, not dropped.
    _columns, parked = cursors.take(1, 10)
    assert parked == [(1,), (2,), (3,)]

    # A positive prefetch is untouched: that many inline, the rest parked.
    stream, cursors = run(2)
    assert stream.sent == [
        (
            TNS_DATA,
            encode_query_response([column], [(1,), (2,)], cursor_id=1, more=True),
        )
    ]
    assert cursors.take(1, 10)[1] == [(3,)]

    # The rule itself, at its edges: a zero keeps nothing back for later only
    # because the caller parks it; a prefetch wider than the result is clamped.
    assert _prefetch_batch(0, 3) == 0
    assert _prefetch_batch(2, 3) == 2
    assert _prefetch_batch(99, 3) == 3


def test_a_freed_temp_lob_never_hands_its_locator_to_the_next_one() -> None:
    # Locators used to be numbered by the COUNT of live temp LOBs, so freeing one
    # reissued its index and the next CREATE_TEMP collided with a locator still
    # in use (#857). Replay of the live sequence that exposed it -- a third large
    # LOB insert on one connection:
    #
    #   insert 1  mint L0
    #   insert 2  mint L1, then the close-temp-LOBs piggyback frees L0
    #   insert 3  mint ... -> handed back L1 under the count, overwriting the
    #             buffer insert 2 was holding; the piggyback riding on insert 3
    #             then freed the locator insert 3 was about to bind, and the
    #             value reached the backend as NULL (ORA-01400).
    from seerdb.server.session import _TempLobs

    temp_lobs = _TempLobs()
    first = temp_lobs.mint(is_blob=True)
    second = temp_lobs.mint(is_blob=True)
    temp_lobs.append(second, b'insert-2-payload')
    temp_lobs.free(first)  # the piggyback riding on insert 2

    third = temp_lobs.mint(is_blob=True)
    assert third not in (first, second)
    # The live buffer is untouched -- the collision used to reset it to empty.
    assert temp_lobs.content(second) == b'insert-2-payload'
    temp_lobs.append(third, b'insert-3-payload')
    assert temp_lobs.content(third) == b'insert-3-payload'
    assert temp_lobs.content(second) == b'insert-2-payload'

    # The index keeps climbing however much is freed: reuse is what caused the
    # collision, so nothing may bring it back, including freeing everything.
    for locator in (second, third):
        temp_lobs.free(locator)
    minted = [temp_lobs.mint(is_blob=False) for _ in range(3)]
    assert len(set(minted + [first, second, third])) == 6
    # Freeing a locator the Mirror never saw is still not an error.
    temp_lobs.free(b'never-minted')
    assert temp_lobs.content(b'never-minted') == b''


def test_a_break_episode_is_answered_with_reset_then_cancel() -> None:
    # connection.cancel() sends its break in-band AFTER the call's reply arrives
    # (no out-of-band support is advertised), then waits. The thin loop dropped
    # every marker into its "not DATA, ignore it" branch, so the client waited
    # forever (#844). A real 23ai answers the episode like this:
    #
    #     client -> MARKER 01 00 03   interrupt
    #     client -> MARKER 01 00 02   reset
    #     server -> MARKER 01 00 02   reset
    #     server -> DATA   OER        ORA-01013
    from seerdb.common.tns_consts import TNS_MARKER, TNS_MARKER_TYPE_RESET
    from seerdb.server.session import _answer_marker

    class _FakeStream:
        def __init__(self) -> None:
            self.sent: list[tuple[int, bytes]] = []

        def write_packet(self, packet_type: int, body: bytes) -> None:
            self.sent.append((packet_type, body))

    stream: Any = _FakeStream()

    # The interrupt opens the episode and draws a reset -- not an error yet.
    _answer_marker(stream, bytes([1, 0, 3]))
    assert stream.sent == [(TNS_MARKER, bytes([1, 0, TNS_MARKER_TYPE_RESET]))]

    # The client's own reset closes it, and only then is the cancel reported --
    # which is what the client is waiting to read.
    _answer_marker(stream, bytes([1, 0, TNS_MARKER_TYPE_RESET]))
    packet_type, body = stream.sent[-1]
    assert packet_type == TNS_DATA
    assert b'ORA-01013' in body
    assert b'User requested cancel of current operation.' in body
    # Exactly one reset for the whole episode: echoing each marker would
    # ping-pong the two sides into a reset storm.
    assert sum(1 for t, _ in stream.sent if t == TNS_MARKER) == 1


def test_complete_message_reassembles_a_spanning_request() -> None:
    # A TTC message can span several TNS packets with no continuation flag, so
    # read_packet hands back only the first and parsing it runs off the end. The
    # loop reads more packets until the parser stops reporting Truncated (#848).
    from seerdb.server.session import _complete_message

    # A stream that yields a message in three DATA packets.
    parts = [b'\x03\x60\x00\x01', b'aaaa', b'bbbb-END']

    class _Stream:
        def __init__(self) -> None:
            self.reads = 0

        def read_packet(self, **_kw):
            self.reads += 1
            # parts[0] is the caller's `body`; hand out parts[1:] on demand.
            return (TNS_DATA, parts[self.reads])

    # parse() is truncated until the assembled body ends with the sentinel; it is
    # pure, so the loop may call it repeatedly with no side effect.
    def parse(b: bytes) -> object:
        if not b.endswith(b'-END'):
            raise Truncated('need the rest')
        return b

    stream: Any = _Stream()
    full = _complete_message(stream, parts[0], parse)
    assert full == b''.join(parts)
    assert stream.reads == 2  # exactly the two continuation packets, no more


def test_complete_message_returns_a_single_packet_untouched() -> None:
    # The common case: the message fits one packet, parse succeeds first try, and
    # the stream is never touched.
    from seerdb.server.session import _complete_message

    class _Stream:
        def read_packet(self, **_kw):  # pragma: no cover - must not be called
            raise AssertionError('read_packet called for a complete message')

    stream: Any = _Stream()
    body = b'\x03\x60\x00\x01whole'
    assert _complete_message(stream, body, lambda b: b) is body


def test_complete_message_refuses_a_message_that_never_completes() -> None:
    # `Truncated` means "the parser ran off the end", which a message cut by the
    # transport and a DECODE FAULT inside a complete message produce alike. The
    # reader used to wait for a continuation either way, so a fault hung the
    # session while the client blocked on its reply -- the silent hang #832/#836
    # removed, back through another door, and one wedge stalls a whole suite run
    # (#868; the fault that found it was a NULL BOOLEAN bind, #869).
    #
    # A real continuation is already in flight, so the wait is bounded: when it
    # expires the request is refused like any other the Mirror cannot serve, and
    # the session stays usable.
    from seerdb.common.exceptions import Truncated
    from seerdb.common.tns_consts import TNS_DATA
    from seerdb.server.session import _complete_message

    class _Stream:
        def __init__(self) -> None:
            self.sent: list[tuple[int, bytes]] = []
            self.waits: list[float | None] = []

        def read_packet(self, *, within: float | None = None):
            # Nothing more is coming -- exactly what a decode fault looks like.
            self.waits.append(within)
            raise TimeoutError

        def write_packet(self, packet_type: int, body: bytes) -> None:
            self.sent.append((packet_type, body))

    stream: Any = _Stream()
    answer = _complete_message(
        stream,
        b'\x03\x5eunparsable',
        lambda b: (_ for _ in ()).throw(Truncated('DALC: 253 bytes, 1 left')),
    )
    # It gives up rather than blocking...
    assert answer is None
    # ...having BOUNDED the wait (the whole point -- an unbounded read here is
    # the hang), and having answered the client, which is still waiting.
    assert stream.waits and all(w is not None for w in stream.waits)
    assert len(stream.sent) == 1
    packet_type, body = stream.sent[0]
    assert packet_type == TNS_DATA
    assert b'ORA-03115' in body
    # The refusal names what could not be read, so the gap is identifiable from
    # the error alone rather than needing a packet capture.
    assert b'DALC: 253 bytes, 1 left' in body


def test_complete_message_raises_when_the_client_vanishes_mid_message() -> None:
    # A half-sent message whose rest never arrives must fail, not spin: read_packet
    # returning None (EOF) becomes a clean InterfaceError.
    from seerdb.common.exceptions import Truncated
    from seerdb.server.session import _complete_message

    class _Stream:
        def read_packet(self, **_kw):
            return None

    stream: Any = _Stream()
    with pytest.raises(InterfaceError):
        _complete_message(
            stream, b'\x03\x60partial', lambda b: (_ for _ in ()).throw(Truncated('x'))
        )


def test_backend_fault_error_reports_ora600_without_recursing() -> None:
    # An exception the backend lets escape must become a one-statement error the
    # session survives, not a crash. A NotSupportedError is a feature gap ->
    # ORA-03115; anything else is genuinely unexpected -> ORA-00600. The
    # non-NotSupportedError branch used to `return _backend_fault_error(exc)`,
    # an infinite self-call that raised RecursionError and tore the connection
    # down (DPY-4011) for every unexpected fault (#904 follow-up).
    from seerdb.common.exceptions import NotSupportedError
    from seerdb.server.session import _backend_fault_error

    gap = _backend_fault_error(NotSupportedError('no wire encoding for X'))
    assert b'ORA-03115' in gap

    # A ValueError (e.g. an encoder raising on a bad length) must not recurse.
    fault = _backend_fault_error(ValueError('encoded number data too long'))
    assert b'ORA-00600' in fault
    assert b'encoded number data too long' in fault


def test_a_login_failure_reports_why_it_failed() -> None:
    # ORA-01017 is reserved for a credential that did not verify. It used to be
    # sent for EVERY login-time failure, including ones where the password was
    # perfectly good and the server simply had no session to give -- a full
    # database (ORA-12516) reported as "invalid username/password". The one
    # thing a client cannot do with that is tell the two apart, and it cost two
    # investigations before the Mirror's own log gave it away (#1006).
    from seerdb.common.exceptions import InterfaceError
    from seerdb.common.tns import decode_token_oer
    from seerdb.server.session import _deny_login

    def reported(**kw) -> tuple:
        stream: Any = _CollectingStream()
        try:
            _deny_login(stream, 'reason', **kw)
        except InterfaceError:
            pass
        decoded = decode_token_oer(stream.sent[0], (0, [], []))
        return (decoded[1], decoded[5])

    # A credential that did not verify still says so, and still says it the way
    # Oracle does -- without distinguishing user from password.
    code, message = reported()
    assert code == 1017
    assert 'invalid username/password' in message

    # A backend that refused says what the BACKEND said, so a client behind a
    # passthrough sees what a direct connection would have seen.
    assert reported(
        ora_code=12516, message='ORA-12516: connection refused by the listener'
    ) == (12516, 'ORA-12516: connection refused by the listener')


def test_a_backend_failure_keeps_its_own_ora_code() -> None:
    # Where the code comes from: an upstream driver's error already carries the
    # server's own, and relaying it is the whole point. Anything without one
    # becomes ORA-01034 -- the server exists but has no session to give, which
    # is true of every case that lands here (#1006).
    import seerdb
    from seerdb.server.session import _login_failure_error

    upstream = seerdb.DatabaseError(
        'ORA-12516: connection refused by the listener', code=12516
    )
    assert _login_failure_error(upstream) == {
        'ora_code': 12516,
        'message': 'ORA-12516: connection refused by the listener',
    }

    # A socket error has no ORA code to relay, and inventing a specific one
    # would be a different lie from the one this fixes.
    fallback = _login_failure_error(OSError('connection refused'))
    assert fallback['ora_code'] == 1034
    assert fallback['message'].startswith('ORA-01034:')


def test_a_parse_fault_refuses_the_call_instead_of_killing_the_session() -> None:
    # A message cut by the transport says so with Truncated, and _complete_message
    # waits for the rest. Anything ELSE a parser raises is a bug in that parser --
    # but it must not take the connection down with it: the Mirror's contract is
    # that it never desyncs, and an unhandled exception here killed the whole
    # session (#1000). Answer the way any unservable call is answered, and let
    # the client carry on.
    from seerdb.server.session import _complete_message

    stream: Any = _CollectingStream()

    def exploding_parse(body: bytes) -> object:
        raise ValueError('a parser bug, not a short message')

    assert _complete_message(stream, b'anything', exploding_parse) is None
    # The client was ANSWERED -- the failure mode this replaces was silence.
    assert stream.sent, 'a parse fault left the client with no reply'


def test_a_cursors_bind_format_is_refreshed_when_the_client_redescribes() -> None:
    # An OAC-less re-execute decodes its rows with the bind format recorded
    # against the cursor. That was recorded once, at open -- so when a client
    # re-describes its binds mid-stream, the NEXT OAC-less execute still decoded
    # with the original types. A column that is all-NULL in the first
    # executemany batch and carries numbers in a later one is exactly that: the
    # numbers came back as their raw NUMBER bytes read as text, and the upstream
    # answered ORA-01722 (#999).
    from seerdb.common.tns_consts import TNS_TYPE_NUMBER, TNS_TYPE_VARCHAR
    from seerdb.server.session import _Cursors

    cursors = _Cursors()
    opened = [(TNS_TYPE_VARCHAR, 1, 40, b'')]
    cursor_id = cursors.open_dml('insert into t (a, b) values (:1, :2)', opened)
    assert cursors.bind_types(cursor_id) == opened

    redescribed = [(TNS_TYPE_NUMBER, 1, 22, b'')]
    cursors.set_bind_types(cursor_id, redescribed)
    assert cursors.bind_types(cursor_id) == redescribed

    # An execute carrying NO OACs is asking to reuse the format, not to clear
    # it -- the same rule a define follows.
    cursors.set_bind_types(cursor_id, [])
    assert cursors.bind_types(cursor_id) == redescribed

    # The statement the cursor stands for is untouched by any of this.
    assert cursors.dml_sql(cursor_id) == 'insert into t (a, b) values (:1, :2)'


def test_a_define_stands_for_the_life_of_the_cursor() -> None:
    # A client applies a define ONCE, on the round-trip that follows the first
    # describe, and it then stands: every later execute of that cursor carries
    # none. A server that forgets reverts to LOB-class handling -- another
    # describe the client is not expecting, then a locator for a column it is
    # reading as a string or bytes, and the row stream desyncs (#826).
    from seerdb.common.tns_consts import TNS_TYPE_LONG
    from seerdb.server.session import _Cursors

    cursors = _Cursors()
    cursor_id = cursors.open_query('select ClobCol from TestClobs')
    assert cursors.defines(cursor_id) == []
    cursors.set_defines(cursor_id, [(TNS_TYPE_LONG, 1)])
    assert cursors.defines(cursor_id) == [(TNS_TYPE_LONG, 1)]
    # An execute carrying no defines is an ordinary call, NOT the client
    # withdrawing them.
    cursors.set_defines(cursor_id, [])
    assert cursors.defines(cursor_id) == [(TNS_TYPE_LONG, 1)]
    # An id nobody defined has none, rather than inheriting a neighbour's.
    assert cursors.defines(cursor_id + 1) == []


def test_a_later_fetch_honours_the_define_too() -> None:
    # The rows of a LOB-class result are parked by the EXECUTE, before the define
    # exists: such a result defers its rows, so the client gets a describe-only
    # reply and sends the define on the round-trip after it. The re-execute that
    # carries the define applied it only to the batch it served itself, so a
    # result larger than one batch went out in two framings -- the first rows
    # inline, every later row a locator the client was no longer expecting. It
    # reported that as an unknown message type whose number was the locator's own
    # length byte, which is why it read as a decoder gap (#982).
    from seerdb.common.tns import ColumnMeta
    from seerdb.common.tns_consts import TNS_TYPE_CLOB, TNS_TYPE_LONG
    from seerdb.server.session import FetchRequest, _answer_fetch, _Cursors

    columns = [
        ColumnMeta(
            name=b'CLOBCOL',
            data_type=TNS_TYPE_CLOB,
            data_length=4000,
            max_size=4000,
            csfrm=1,
        )
    ]
    cursors = _Cursors()
    cursor_id = cursors.open(columns, [('first',), ('second',)])
    cursors.set_defines(cursor_id, [(TNS_TYPE_LONG, 1)])

    stream: Any = _CollectingStream()
    lobs = _answer_fetch(stream, FetchRequest(cursor=cursor_id, fetch=1), cursors)
    # Served inline, so there is no locator and nothing queued for a LOB read --
    # a client that defined the column as LONG issues none.
    assert lobs == []
    assert b'first' in stream.sent[0]

    # And the SAME again for the batch after it, which is the half that used to
    # revert.
    stream = _CollectingStream()
    lobs = _answer_fetch(stream, FetchRequest(cursor=cursor_id, fetch=1), cursors)
    assert lobs == []
    assert b'second' in stream.sent[0]


def test_defines_travel_to_the_id_a_re_run_mints() -> None:
    # Re-running a cached query mints a FRESH cursor id and reports that, so the
    # defines standing on the id the client re-executed have to travel with it.
    # Without that the next re-execute found none and desynced on the third
    # execute of a statement, not the second -- which is what made it look like a
    # bind-type problem rather than a lost define (#826).
    from seerdb.common.tns_consts import TNS_TYPE_LONG
    from seerdb.server.session import _Cursors

    cursors = _Cursors()
    first = cursors.open_query('select ClobCol from TestClobs')
    cursors.set_defines(first, [(TNS_TYPE_LONG, 1)])
    second = cursors.open_query('select ClobCol from TestClobs')
    assert second != first
    cursors.set_defines(second, cursors.defines(first))
    assert cursors.defines(second) == [(TNS_TYPE_LONG, 1)]
