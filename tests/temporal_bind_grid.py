# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The grid of temporal bind encodings, and a Mirror that records what a client
sent for each cell (#1338).

Date and time have more wire forms than any other family: three widths (7, 11
and 13 bytes), three zone semantics, a fraction or none, AD or BC, and a handful
of contexts a value can ride in. Each encoding bug so far lived in one cell of
that grid. This module names every cell and runs it on a DB-API connection --
seerdb's or python-oracledb's, the code is the same -- against an in-process
Mirror whose bind reader is wrapped to keep the raw bytes of each value.

``tools/temporal_bind_reference.py`` runs python-oracledb through it and writes
``tests/temporal_binds_reference.py``; ``tests/test_temporal_bind_grid.py`` runs
seerdb through it and compares cell by cell.
"""

from __future__ import annotations

import contextlib
import datetime
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from seerdb.common import tns
from seerdb.common.types import BcDate, reset_decode_bc_dates, set_decode_bc_dates

# --- The values ------------------------------------------------------------

_EAST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_WEST = datetime.timezone(-datetime.timedelta(hours=8))

# One value per kind of temporal input. 2020-02-29 is a leap day, 23:59:59 sets
# every time byte, and the fraction has all six digits.
VALUES: dict[str, Any] = {
    'date': datetime.date(2020, 2, 29),
    'midnight': datetime.datetime(2020, 2, 29),
    'whole': datetime.datetime(2020, 2, 29, 23, 59, 59),
    'fraction': datetime.datetime(2020, 2, 29, 23, 59, 59, 123456),
    'east': datetime.datetime(2020, 2, 29, 23, 59, 59, 123456, tzinfo=_EAST),
    'west_whole': datetime.datetime(2020, 2, 29, 23, 59, 59, tzinfo=_WEST),
    'bc': BcDate(-44, 3, 15, 12, 30, 45),
    'bc_fraction': BcDate(-44, 3, 15, 12, 30, 45, 500000),
}

# The values python-oracledb has no type for: it cannot bind a date before
# year 1 at all.
SEERDB_ONLY_VALUES = frozenset({'bc', 'bc_fraction'})

# A column of rows whose widths differ, for the executemany case that once sent
# a 7-byte value under an 11-byte descriptor (#1098).
MIXED_ROWS: dict[str, tuple[str, str]] = {
    'whole+fraction': ('whole', 'fraction'),
    'fraction+midnight': ('fraction', 'midnight'),
    'bc+bc_fraction': ('bc', 'bc_fraction'),
}

# --- The declarations ------------------------------------------------------

# A bind's declared type, by the DB_TYPE_* name both drivers export; None is an
# undeclared bind, typed from its value.
DECLARED: tuple[str | None, ...] = (
    None,
    'DB_TYPE_DATE',
    'DB_TYPE_TIMESTAMP',
    'DB_TYPE_TIMESTAMP_LTZ',
    'DB_TYPE_TIMESTAMP_TZ',
)

# The SQL type of an object attribute (or a scratch column) with each
# declaration, and the short name its object type takes.
SQL_TYPE = {
    'DB_TYPE_DATE': ('DATE', 'D'),
    'DB_TYPE_TIMESTAMP': ('TIMESTAMP', 'TS'),
    'DB_TYPE_TIMESTAMP_LTZ': ('TIMESTAMP WITH LOCAL TIME ZONE', 'LTZ'),
    'DB_TYPE_TIMESTAMP_TZ': ('TIMESTAMP WITH TIME ZONE', 'TZ'),
}

# The scratch objects a live run creates (and drops): one object type per
# declaration, holding a single attribute A of that type.
OBJECT_TYPE_PREFIX = 'PYO_TBG_'


def object_type_name(declared: str) -> str:
    return OBJECT_TYPE_PREFIX + SQL_TYPE[declared][1]


# --- The cells -------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    """One point of the grid: a context, a declaration and a value (or, for
    mixed rows, a pair of them)."""

    context: str
    declared: str | None
    value: str

    @property
    def key(self) -> str:
        return f'{self.context}/{self.declared or "undeclared"}/{self.value}'

    @property
    def values(self) -> list[Any]:
        if self.context == 'executemany_mixed':
            return [VALUES[V] for V in MIXED_ROWS[self.value]]
        if self.context == 'executemany':
            return [VALUES[self.value], VALUES[self.value]]
        return [VALUES[self.value]]

    @property
    def seerdb_only(self) -> bool:
        names = MIXED_ROWS.get(self.value, (self.value,))
        return any(N in SEERDB_ONLY_VALUES for N in names)


def cells() -> list[Cell]:
    """Every cell of the grid, in a fixed order."""
    out = []
    for declared in DECLARED:
        for value in VALUES:
            out.append(Cell('execute', declared, value))
            out.append(Cell('executemany', declared, value))
        for value in MIXED_ROWS:
            out.append(Cell('executemany_mixed', declared, value))
        if declared is None:
            # An object attribute and an IN OUT variable always have a type.
            continue
        for value in VALUES:
            out.append(Cell('object', declared, value))
            out.append(Cell('inout', declared, value))
    return out


# --- What a cell means -----------------------------------------------------

# How the server prints each type back, to the microsecond and with the offset
# where the type has one.
TO_CHAR_FORMAT = {
    'DB_TYPE_DATE': 'SYYYY-MM-DD HH24:MI:SS',
    'DB_TYPE_TIMESTAMP': 'SYYYY-MM-DD HH24:MI:SS.FF6',
    'DB_TYPE_TIMESTAMP_LTZ': 'SYYYY-MM-DD HH24:MI:SS.FF6',
    'DB_TYPE_TIMESTAMP_TZ': 'SYYYY-MM-DD HH24:MI:SS.FF6 TZH:TZM',
}


def kind(cell: Cell, value: Any) -> str:
    """The type a cell's value is bound as: its declaration, or for an
    undeclared bind the type seerdb picks from the value -- WITH TIME ZONE for an
    aware datetime, TIMESTAMP for one with a fraction, DATE otherwise."""
    if cell.declared is not None:
        return cell.declared
    if getattr(value, 'tzinfo', None) is not None:
        return 'DB_TYPE_TIMESTAMP_TZ'
    if any(getattr(V, 'microsecond', 0) for V in cell.values):
        # executemany gives a column one type, the widest any row needs (#1098).
        return 'DB_TYPE_TIMESTAMP'
    return 'DB_TYPE_DATE'


def expected_text(cell: Cell, value: Any) -> str:
    """What the server should print for ``value`` bound in ``cell``, in
    ``TO_CHAR_FORMAT[kind(cell, value)]``.

    A DATE keeps whole seconds. A TIMESTAMP and a LOCAL TIME ZONE keep the wall
    clock and drop any zone (an LTZ value is read in the session's zone and
    printed in it). WITH TIME ZONE keeps an aware value's offset and reads a
    naive one as UTC."""
    as_kind = kind(cell, value)
    if not isinstance(value, (datetime.datetime, BcDate)):
        value = datetime.datetime(value.year, value.month, value.day)
    sign = '-' if value.year < 0 else ' '
    text = (
        f'{sign}{abs(value.year):04d}-{value.month:02d}-{value.day:02d} '
        f'{value.hour:02d}:{value.minute:02d}:{value.second:02d}'
    )
    if as_kind == 'DB_TYPE_DATE':
        return text
    text += f'.{value.microsecond:06d}'
    if as_kind != 'DB_TYPE_TIMESTAMP_TZ':
        return text
    offset = value.utcoffset() if getattr(value, 'tzinfo', None) else None
    minutes = int(offset.total_seconds() // 60) if offset else 0
    hours, minutes = divmod(abs(minutes), 60)
    sign = '-' if offset and offset < datetime.timedelta(0) else '+'
    return f'{text} {sign}{hours:02d}:{minutes:02d}'


# --- Running a cell --------------------------------------------------------


def run_cell(conn: Any, driver: Any, cell: Cell) -> None:
    """Run ``cell`` on a DB-API connection of ``driver`` (the seerdb or the
    oracledb module). The statement carries the cell's key, so no two cells
    share a cached cursor."""
    tag = f'/* {cell.key} */'
    cursor = conn.cursor()
    try:
        declared = getattr(driver, cell.declared) if cell.declared else None
        if cell.context == 'execute':
            if declared is not None:
                cursor.setinputsizes(declared)
            cursor.execute(f'select dump(:b) {tag} from dual', cell.values)
            cursor.fetchall()
        elif cell.context in ('executemany', 'executemany_mixed'):
            if declared is not None:
                cursor.setinputsizes(declared)
            column = SQL_TYPE[cell.declared or 'DB_TYPE_TIMESTAMP'][1]
            cursor.executemany(
                f'insert into {OBJECT_TYPE_PREFIX}T ({column}) values (:b) {tag}',
                [[V] for V in cell.values],
            )
        elif cell.context == 'object':
            assert cell.declared is not None
            obj_type = conn.gettype(object_type_name(cell.declared))
            obj = obj_type.newobject()
            obj.A = cell.values[0]
            cursor.execute(
                f'declare o {object_type_name(cell.declared)} := :b; '
                f'begin null; end; {tag}',
                [obj],
            )
        elif cell.context == 'inout':
            var = cursor.var(declared)
            var.setvalue(0, cell.values[0])
            cursor.execute(f'begin :b := :b; end; {tag}', [var])
        else:
            raise AssertionError(cell.context)
    finally:
        cursor.close()


# --- Recording what the Mirror received -----------------------------------


@dataclass
class Received:
    """What one execute carried: each bind's (type, max size) from its OAC, and
    each row's values as the raw bytes on the wire, length prefix included."""

    oacs: list[tuple[int, int]]
    rows: list[list[bytes]]


@contextlib.contextmanager
def recording() -> Iterator[list[Received]]:
    """Wrap the Mirror's bind reader so every execute's binds are kept as raw
    bytes, as well as decoded. Yields the list each execute is appended to."""
    log: list[Received] = []
    read_rows = tns._read_bind_rows
    read_value = tns._read_bind_value

    def read_rows_recording(after, types, capacities, order):
        received = Received(oacs=[(T[0], T[2]) for T in types], rows=[])
        raw: list[bytes] = []

        def read_value_recording(data_type, csfrm, data, toid=b''):
            # A BC date is a value too: decode it rather than refuse the bind.
            token = set_decode_bc_dates(True)
            try:
                value, rest = read_value(data_type, csfrm, data, toid)
            finally:
                reset_decode_bc_dates(token)
            raw.append(bytes(data[: len(data) - len(rest)]))
            return value, rest

        tns._read_bind_value = read_value_recording  # type: ignore[assignment]
        try:
            rows, rest = read_rows(after, types, capacities, order)
        finally:
            tns._read_bind_value = read_value  # type: ignore[assignment]
        per_row = len(order)
        received.rows = [raw[I : I + per_row] for I in range(0, len(raw), per_row)]
        log.append(received)
        return rows, rest

    tns._read_bind_rows = read_rows_recording  # type: ignore[assignment]
    try:
        yield log
    finally:
        tns._read_bind_rows = read_rows  # type: ignore[assignment]


@contextlib.contextmanager
def mirror(serve: Callable[[socket.socket], None]) -> Iterator[int]:
    """Listen on a loopback port and hand each accepted connection to ``serve``
    in its own thread. Yields the port."""
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(('127.0.0.1', 0))
    listen.listen(4)

    def accept_loop() -> None:
        while True:
            try:
                conn, _ = listen.accept()
            except OSError:
                return

            def serve_one(conn: socket.socket = conn) -> None:
                try:
                    serve(conn)
                except Exception:  # noqa: BLE001 - the client sees the hang-up
                    pass
                finally:
                    conn.close()

            threading.Thread(target=serve_one, daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    try:
        yield listen.getsockname()[1]
    finally:
        listen.close()


def capture(conn: Any, driver: Any, log: list[Received], cell: Cell) -> dict:
    """Run ``cell`` and return what reached the Mirror: ``{'oac': [type, size],
    'rows': [hex, ...]}`` for the temporal bind, or ``{'error': text}`` when the
    client refused the cell before sending it."""
    start = len(log)
    try:
        run_cell(conn, driver, cell)
    except Exception as exc:  # noqa: BLE001 - the error is the cell's result
        if len(log) == start:
            return {'error': f'{type(exc).__name__}: {exc}'.splitlines()[0]}
    sent = [R for R in log[start:] if R.rows]
    if not sent:
        return {'error': 'no bind values reached the server'}
    # The cell's own execute is the last one carrying values: a driver's
    # dictionary lookups (gettype) run before it.
    received = sent[-1]
    return {
        'oac': list(received.oacs[0]),
        'rows': [R[0].hex() for R in received.rows],
    }


# --- Known gaps ------------------------------------------------------------


def gap(cell: Cell, part: str) -> int | None:
    """The issue tracking a cell seerdb does not get right yet, or None.

    ``part`` is what is compared: ``'oac'`` the bind's type and size, ``'bytes'``
    the value bytes (both offline), ``'server'`` what a live server reads."""
    if cell.context == 'object' and part == 'oac':
        # A date before year 1 into a WITH TIME ZONE attribute is refused before
        # there is an OAC; every other object bind's OAC carries a size where
        # python-oracledb's is 0.
        if cell.seerdb_only and cell.declared == 'DB_TYPE_TIMESTAMP_TZ':
            return None
        return 1806
    return None
