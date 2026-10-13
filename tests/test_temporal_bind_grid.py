# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Every temporal bind encoding, byte for byte against python-oracledb (#1338).

seerdb runs each cell of the grid in ``temporal_bind_grid`` against an
in-process Mirror that keeps the raw bytes of every bind, and each cell is
compared with what python-oracledb sent for it (``temporal_binds_reference``,
captured by ``tools/temporal_bind_reference.py``): the bind's OAC type and size,
and each row's value bytes.

Where seerdb sends something else on purpose, the cell's expected bytes are
written out below from the rule instead (``_seerdb_rule``). python-oracledb has
no type for a date before year 1, so those cells exist only here; it drops an
aware datetime's zone, which seerdb keeps; and it truncates an undeclared
datetime's fraction by binding it as a DATE, where seerdb binds a TIMESTAMP.
"""

from __future__ import annotations

import datetime

import pytest
from temporal_bind_grid import (
    VALUES,
    Cell,
    capture,
    cells,
    gap,
    kind,
    mirror,
    object_type_name,
    recording,
)
from temporal_binds_reference import OBJECT_TYPES, REFERENCE

import seerdb
from seerdb.common.dbobject import DbObjectType
from seerdb.common.tns import ColumnMeta
from seerdb.common.tns_consts import (
    FIELD_VERSION_23_3_EXT_6,
    TNS_TYPE_ADT,
    TNS_TYPE_DATE,
    TNS_TYPE_TIMESTAMP,
    TNS_TYPE_TIMESTAMPLTZ,
    TNS_TYPE_TIMESTAMPTZ,
    TNS_TYPE_VARCHAR,
)
from seerdb.server.backend import Result, credential_lookup
from seerdb.server.framing import PacketStream
from seerdb.server.session import serve_session


class _GridBackend:
    # Answers whatever a cell runs: a query returns one row, a DML counts its
    # rows, and a PL/SQL block leaves its OUT binds NULL.
    capabilities: frozenset = frozenset()

    def authenticate(self, username: str) -> str | None:
        return credential_lookup({'PYO': 'pyo123'}, username)

    def execute(self, sql: str, binds=()) -> Result:
        head = sql.lstrip().split(None, 1)[0].lower()
        if head == 'select':
            col = ColumnMeta(
                name=b'D', data_type=TNS_TYPE_VARCHAR, data_length=1, max_size=1
            )
            return Result(columns=[col], rows=[('X',)])
        if head in ('begin', 'declare'):
            return Result(out_binds=[None] * len(binds))
        return Result(rowcount=1)

    def execute_many(self, sql: str, rows) -> int:
        return len(rows)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def _object_type(name: str) -> DbObjectType:
    spec = OBJECT_TYPES[name]
    return DbObjectType(
        spec['schema'], name, bytes.fromhex(spec['oid']), spec['version'], spec['attrs']
    )


@pytest.fixture(scope='module')
def received() -> dict:
    """What seerdb sent for every cell."""

    def serve(sock) -> None:
        serve_session(
            PacketStream(sock), _GridBackend(), field_version=FIELD_VERSION_23_3_EXT_6
        )

    out = {}
    with recording() as log, mirror(serve) as port:
        conn = seerdb.connect(
            host='127.0.0.1',
            port=port,
            user='PYO',
            password='pyo123',
            service_name='FREEPDB1',
            timeout=5000,
        )
        # The cells' object types as the reference server described them: the
        # type OID rides in every object bind.
        conn.gettype = _object_type  # type: ignore[method-assign]
        try:
            for cell in cells():
                out[cell.key] = capture(conn, seerdb, log, cell)
        finally:
            conn.close()
    return out


# --- seerdb's own rules ----------------------------------------------------

_OAC = {
    'DB_TYPE_DATE': [TNS_TYPE_DATE, 7],
    'DB_TYPE_TIMESTAMP': [TNS_TYPE_TIMESTAMP, 11],
    'DB_TYPE_TIMESTAMP_LTZ': [TNS_TYPE_TIMESTAMPLTZ, 11],
    'DB_TYPE_TIMESTAMP_TZ': [TNS_TYPE_TIMESTAMPTZ, 13],
}


def _date_bytes(value) -> bytes:
    # Century and year of century, each biased by 100 and counting down below
    # the bias for a year before 1; then month, day, and the time biased by 1.
    if not isinstance(value, datetime.datetime) and not hasattr(value, 'hour'):
        value = datetime.datetime(value.year, value.month, value.day)
    century, year = divmod(abs(value.year), 100)
    sign = -1 if value.year < 0 else 1
    return bytes(
        [
            100 + sign * century,
            100 + sign * year,
            value.month,
            value.day,
            value.hour + 1,
            value.minute + 1,
            value.second + 1,
        ]
    )


def _value_bytes(value, as_kind: str) -> bytes:
    """The value bytes of ``value`` sent as ``as_kind``: DATE drops the
    fraction; TIMESTAMP and LTZ append the fraction in nanoseconds, and only
    when it is not zero (#1337); WITH TIME ZONE sends the UTC wall clock, the
    nanoseconds and the offset's hours and minutes biased by 20 and 60."""
    if as_kind == 'DB_TYPE_DATE':
        return _date_bytes(value)
    nanos = getattr(value, 'microsecond', 0) * 1000
    if as_kind != 'DB_TYPE_TIMESTAMP_TZ':
        return _date_bytes(value) + (nanos.to_bytes(4, 'big') if nanos else b'')
    offset = value.utcoffset() or datetime.timedelta(0)
    utc = value.replace(tzinfo=None) - offset
    minutes = int(offset.total_seconds() // 60)
    hours, minutes = divmod(abs(minutes), 60)
    sign = -1 if offset < datetime.timedelta(0) else 1
    return (
        _date_bytes(utc)
        + nanos.to_bytes(4, 'big')
        + bytes([20 + sign * hours, 60 + sign * minutes])
    )


def _with_length(data: bytes) -> str:
    return (bytes([len(data)]) + data).hex()


def _object_image(declared: str, attribute: bytes) -> str:
    """An object bind holding one attribute, as python-oracledb frames it: the
    type's own head (its OID among it) from the reference, then the image whose
    three length fields count the attribute plus 7."""
    whole = REFERENCE[f'object/{declared}/whole']['rows'][0]
    head = whole[: whole.index('00000001', 76)]
    size = f'{len(attribute) + 1 + 7:02x}'
    return (
        head + f'00000001{size}0101{size}8401fe000000{size}' + _with_length(attribute)
    )


def _seerdb_rule(cell: Cell) -> dict | None:
    """The cells where seerdb sends something other than python-oracledb, on
    purpose; None for every other cell."""
    values = cell.values
    if cell.seerdb_only and cell.declared == 'DB_TYPE_TIMESTAMP_TZ':
        # A date before year 1 has no time zone rules to apply.
        return {
            'error': 'DataError: a date before year 1 cannot be sent with a time zone'
        }
    aware = any(getattr(V, 'tzinfo', None) is not None for V in values)
    if cell.declared is None:
        if not (cell.seerdb_only or aware or kind(cell, values[0]) != 'DB_TYPE_DATE'):
            return None
    elif not (cell.seerdb_only or (aware and cell.declared == 'DB_TYPE_TIMESTAMP_TZ')):
        return None
    as_kind = kind(cell, values[0])
    if cell.context == 'object':
        return {
            'oac': [TNS_TYPE_ADT, 0],
            'rows': [_object_image(as_kind, _value_bytes(values[0], as_kind))],
        }
    return {
        'oac': _OAC[as_kind],
        'rows': [_with_length(_value_bytes(V, as_kind)) for V in values],
    }


def _expected(cell: Cell) -> dict:
    rule = _seerdb_rule(cell)
    return rule if rule is not None else REFERENCE[cell.key]


# --- The tests -------------------------------------------------------------


def _compare(got: dict, expected: dict, field: str) -> None:
    # A cell is either refused with a given error or sends the expected field.
    if 'error' in expected:
        assert got.get('error', '').startswith(expected['error']), got
    else:
        assert got.get(field) == expected[field], got.get('error')


def _mark(cell: Cell, part: str) -> pytest.MarkDecorator | tuple:
    issue = gap(cell, part)
    if issue is None:
        return ()
    return pytest.mark.xfail(reason=f'#{issue}', strict=True)


def test_every_reference_cell_is_in_the_grid() -> None:
    # A cell renamed in the grid but not recaptured would silently compare
    # against nothing.
    keys = {C.key for C in cells()}
    assert set(REFERENCE) <= keys
    assert {C.key for C in cells() if not C.seerdb_only} == set(REFERENCE)


def test_the_object_image_rule_rebuilds_every_reference_object() -> None:
    # _object_image frames seerdb's expected object binds; check it against
    # every object python-oracledb sent before trusting it with the rest.
    for cell in cells():
        if cell.context != 'object' or cell.seerdb_only:
            continue
        expected = REFERENCE[cell.key]['rows'][0]
        # The attribute follows the last of the image's three length fields.
        attribute = bytes.fromhex(expected[expected.rindex('8401fe000000') + 14 :])
        assert _object_image(cell.declared or '', attribute[1:]) == expected, cell.key


@pytest.mark.parametrize(
    'cell',
    [pytest.param(C, id=C.key, marks=_mark(C, 'oac')) for C in cells()],
)
def test_the_bind_is_described_as_python_oracledb_describes_it(
    cell: Cell, received: dict
) -> None:
    _compare(received[cell.key], _expected(cell), 'oac')


@pytest.mark.parametrize(
    'cell',
    [pytest.param(C, id=C.key, marks=_mark(C, 'bytes')) for C in cells()],
)
def test_the_value_bytes_are_python_oracledbs(cell: Cell, received: dict) -> None:
    _compare(received[cell.key], _expected(cell), 'rows')


def test_the_values_cover_every_width() -> None:
    # The grid is only as good as its values: 7, 11 and 13 bytes, AD and BC,
    # each zone semantics.
    assert {type(V).__name__ for V in VALUES.values()} >= {'date', 'datetime', 'BcDate'}
    assert any(getattr(V, 'microsecond', 0) for V in VALUES.values())
    assert any(getattr(V, 'tzinfo', None) for V in VALUES.values())
    assert object_type_name('DB_TYPE_DATE') in OBJECT_TYPES
