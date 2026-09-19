# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Offline unit tests for the Oracle-passthrough example backend's error relay.

The passthrough is the Mirror's conformance harness (it relays each statement to
a real Oracle). A real Oracle error already reads ``ORA-NNNNN: ...``; the Mirror
(``BackendError``) re-adds that prefix from the code, so relaying the text
verbatim doubled it. ``_relay_error`` strips the leading prefix so exactly one is
emitted — the behaviour these tests pin without needing a database.
"""

from __future__ import annotations

import sys
from pathlib import Path

import seerdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'examples'))
from oracle_passthrough_backend import (  # noqa: E402
    OraclePassthroughBackend,
    _relay_error,
)

from seerdb.common.tns_consts import (  # noqa: E402
    FIELD_VERSION_11_2,
    FIELD_VERSION_12_1,
)
from seerdb.server.backend import BindVar  # noqa: E402


def test_strips_the_redundant_ora_prefix():
    exc = seerdb.DatabaseError('ORA-00904: "X": invalid identifier', 904)
    err = _relay_error(exc)
    # The Mirror re-adds "ORA-00904: " from the code — the relayed message must be
    # bare so the final text carries the prefix exactly once.
    assert err.ora_code == 904
    assert err.ora_message == 'ORA-00904: "X": invalid identifier'


def test_recovers_the_code_from_the_prefix_when_absent():
    # Some client exceptions carry no numeric code; take it from the text.
    exc = seerdb.DatabaseError('ORA-01008: not all variables bound', None)
    err = _relay_error(exc)
    assert err.ora_code == 1008
    assert err.ora_message == 'ORA-01008: not all variables bound'


def test_message_without_a_prefix_is_left_alone():
    exc = seerdb.DatabaseError('some non-Oracle failure', None)
    err = _relay_error(exc)
    assert err.ora_code == 900  # ORA-00900, the BackendError default
    assert err.ora_message == 'ORA-00900: some non-Oracle failure'


def test_code_argument_wins_over_the_prefix_digits():
    # exc.code is authoritative when present, even if the text's digits differ.
    exc = seerdb.DatabaseError('ORA-00942: table or view does not exist', 942)
    err = _relay_error(exc)
    assert err.ora_code == 942
    assert err.ora_message == 'ORA-00942: table or view does not exist'


def test_relays_the_error_offset():
    # oracledb's DatabaseError.offset (the parse offset) is carried into the
    # BackendError so the Mirror draws its caret under the right column.
    exc = seerdb.DatabaseError('ORA-00904: "X": invalid identifier', 904)
    exc.offset = 7
    err = _relay_error(exc)
    assert err.error_offset == 7


def test_missing_offset_is_none():
    exc = seerdb.DatabaseError('ORA-00942: table or view does not exist', 942)
    # .offset defaults to None on a DatabaseError with no parse position.
    err = _relay_error(exc)
    assert err.error_offset is None


class _FakeVar:
    def __init__(self, value=None):
        self._value = value

    def setvalue(self, _pos, value):
        self._value = value

    def getvalue(self, _pos=0):
        # A real Var takes the iteration position; the RETURNING path passes it.
        return self._value


class _FakeCursor:
    """Records what _execute_plsql binds, without a database."""

    def __init__(self, out_values=None):
        self.bound = None
        self.declared = None
        self.description = None
        self.rowcount = 0
        self._out_values = out_values or {}

    def var(self, dbtype, size=None):
        return _FakeVar()

    def arrayvar(self, dbtype, value_or_numelements, size=None):
        var = _FakeVar()
        var.capacity = value_or_numelements
        return var

    def setinputsizes(self, *args):
        self.declared = args

    def execute(self, sql, variables):
        self.bound = list(variables)
        # Give each Var the fake OUT value the "block" assigned it; a Var the
        # block left alone keeps what it was seeded with, as on a real server.
        for i, v in enumerate(self.bound):
            if isinstance(v, _FakeVar) and i in self._out_values:
                v.setvalue(0, self._out_values[i])


def _plsql_binds(*binds):
    from seerdb.server.backend import BindVar

    return [BindVar(value=v, tns_type=t, max_size=m) for (v, t, m) in binds]


def test_typed_null_of_an_ordinary_statement_is_declared_upstream():
    # A NULL bind arrives as a BindVar with the type the client declared for
    # it (#699); the passthrough declares that type on its own cursor the same
    # way (setinputsizes) and binds the NULL, rather than taking the BindVar
    # for a PL/SQL OUT bind.
    from seerdb.common.tns_consts import TNS_TYPE_NUMBER
    from seerdb.server.backend import BindVar

    backend = OraclePassthroughBackend(host='h', port=1, service='s', credentials={})
    cursor = _FakeCursor()
    backend._conn = type('Conn', (), {'cursor': lambda self: cursor})()
    (typed,) = _plsql_binds((None, TNS_TYPE_NUMBER, 22))
    result = backend.execute('SELECT id FROM t WHERE :1 IS NULL', [typed, 'x'])
    assert cursor.declared == (seerdb.DB_TYPE_NUMBER, None)
    assert cursor.bound == [None, 'x']
    assert result.rowcount == 0 and not result.out_binds
    assert not isinstance(cursor.bound[0], BindVar)


def test_array_bind_registers_an_array_var_of_the_declared_capacity():
    # An associative-array BindVar (#743) becomes an arrayvar of the client's
    # capacity, seeded with the elements sent; its list comes back as the OUT.
    from seerdb.common.tns_consts import TNS_TYPE_NUMBER

    backend = OraclePassthroughBackend(host='h', port=1, service='s', credentials={})
    cursor = _FakeCursor(out_values={1: [7, 8, 9]})
    binds = [
        BindVar(value=[1, 2], tns_type=TNS_TYPE_NUMBER, max_size=22, array_size=10),
        BindVar(value=[], tns_type=TNS_TYPE_NUMBER, max_size=22, array_size=4),
    ]
    result = backend._execute_plsql(cursor, 'BEGIN p(:1, :2); END;', binds)
    assert [getattr(v, 'capacity', None) for v in cursor.bound] == [10, 4]
    assert result.out_binds == [[1, 2], [7, 8, 9]]


def test_large_lob_in_bind_is_registered_as_a_lob_var():
    # A large CLOB / BLOB IN bind (#91) resolves to plain str / bytes. It used to
    # be bound as that value directly, which is IN-only -- a block that WRITES to
    # the bind then failed upstream with ORA-06502. It is registered as a LOB Var
    # instead, seeded with the value, so an IN OUT LOB comes back (#979).
    from seerdb.common.tns_consts import TNS_TYPE_CLOB, TNS_TYPE_NUMBER

    backend = OraclePassthroughBackend(host='h', port=1, service='s', credentials={})
    backend._conn = type('Conn', (), {'field_version': FIELD_VERSION_12_1})()
    cursor = _FakeCursor(out_values={1: 42})
    binds = _plsql_binds(
        ('X' * 40000, TNS_TYPE_CLOB, 160000), (None, TNS_TYPE_NUMBER, 1)
    )
    result = backend._execute_plsql(cursor, 'BEGIN :r := f(:p); END;', binds)
    # Both positions are Vars now. The LOB one was seeded with the IN value, so
    # a block that leaves it alone still returns it -- and one that writes to it
    # returns what it wrote, which a bare string bind could never carry back.
    assert isinstance(cursor.bound[0], _FakeVar)
    assert isinstance(cursor.bound[1], _FakeVar)
    assert result.out_binds == ['X' * 40000, 42]


def test_large_lob_in_bind_stays_a_plain_value_below_12_1():
    # Below 12.1 a LOB Var has no bind encoding at all (#902), so the plain
    # str / bytes is all the upstream can take. IN-only, and its OUT slot is
    # None -- which the client discards anyway.
    from seerdb.common.tns_consts import TNS_TYPE_CLOB, TNS_TYPE_NUMBER

    backend = OraclePassthroughBackend(host='h', port=1, service='s', credentials={})
    backend._conn = type('Conn', (), {'field_version': FIELD_VERSION_11_2})()
    cursor = _FakeCursor(out_values={1: 42})
    binds = _plsql_binds(
        ('X' * 40000, TNS_TYPE_CLOB, 160000), (None, TNS_TYPE_NUMBER, 1)
    )
    result = backend._execute_plsql(cursor, 'BEGIN :r := f(:p); END;', binds)
    assert cursor.bound[0] == 'X' * 40000
    assert isinstance(cursor.bound[1], _FakeVar)
    assert result.out_binds == [None, 42]


def test_returning_resolves_an_object_bind_like_execute_does():
    # An INSERT ... RETURNING with an object (ADT) IN bind: execute_returning
    # built its batch straight from the row, so an ObjectImage -- the row
    # decoder's placeholder, not a bindable value -- reached the upstream driver
    # and killed the Mirror connection (DPY-4011). execute() has always run its
    # binds through _resolve_object_binds; the RETURNING path must too (#826).
    # An unresolvable OID resolves to None, which the driver rejects cleanly with
    # ORA-00932 instead of dying.
    from seerdb.common.dbobject import ObjectImage
    from seerdb.common.tns_consts import TNS_TYPE_NUMBER

    backend = OraclePassthroughBackend(host='h', port=1, service='s', credentials={})
    cursor = _FakeCursor()
    backend._conn = type('Conn', (), {'cursor': lambda self: cursor})()
    # No type resolves for this OID, so _object_from_image yields None.
    backend._gettype_by_oid = lambda _oid: None
    image = ObjectImage(b'\x01' * 16, 'PYO', 'UDT_OBJ', 873, b'\x00\x01')
    receiver = BindVar(value=None, tns_type=TNS_TYPE_NUMBER, max_size=22)
    backend.execute_returning(
        'INSERT INTO t VALUES (:1) RETURNING id INTO :2', [[image, receiver]]
    )
    assert not isinstance(cursor.bound[0], ObjectImage)
    assert cursor.bound[0] is None
    assert isinstance(cursor.bound[1], _FakeVar)
