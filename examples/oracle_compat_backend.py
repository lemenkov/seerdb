# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""An Oracle/sqlplus session-bootstrap shim over any Backend.

Classic sqlplus fires a fixed chain of Oracle-specific queries the moment it
connects — ``SELECT USER FROM DUAL``, a ``DECODE()`` compatibility probe,
``SYSTEM.PRODUCT_PRIVS`` lookups, ``DBMS_OUTPUT`` / ``DBMS_APPLICATION_INFO``
PL/SQL calls — before it shows a prompt, and it *aborts* if some of them answer
wrong (the DECODE probe especially). A generic backend (SQLite, PostgreSQL)
can't evaluate those Oracle idioms.

``OracleCompatBackend`` wraps any backend and answers just that fixed bootstrap
set itself, passing every real statement straight through to the inner backend.
It is deliberately **not** a general Oracle→backend SQL translator — only the
narrow, known set sqlplus needs to establish a session. With it, a real sqlplus
reaches the ``SQL>`` prompt against a plain SQLite/PostgreSQL Mirror and can then
run actual DDL/DML/queries.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from seerdb.common.tns_consts import (
    ORA_INSUFFICIENT_PRIVILEGES,
    ORA_TABLE_OR_VIEW_DOES_NOT_EXIST,
    TNS_TYPE_VARCHAR,
)
from seerdb.server import (
    Backend,
    BackendError,
    BindVar,
    Capability,
    ColumnMeta,
    Result,
    UnsupportedFeature,
)

# Oracle's one-row ``DUAL`` table has no equal on a plain backend, but a bare
# ``SELECT <expr>`` (no FROM) is the same thing there — so drop a trailing
# ``FROM DUAL`` before delegating. Only the exact idiom, nothing cleverer.
_FROM_DUAL = re.compile(r'\s+FROM\s+DUAL\s*$', re.IGNORECASE)

# The sqlplus ``VARIABLE v NUMBER`` / ``EXEC :v := 42`` flow sends a PL/SQL block
# that assigns literals to OUT binds — ``BEGIN :n := 42; :s := 'hi'; END;``. A
# non-Oracle backend can't run PL/SQL, but this one idiom (assign a literal to a
# bind) is trivially evaluable, and covers the common case: bind a value, then use
# it in a following statement. Only literal assignments — nothing that reads state.
_OUT_BIND_ASSIGN = re.compile(
    r":\w+\s*:=\s*('(?:[^']|'')*'|-?\d+(?:\.\d+)?)", re.IGNORECASE
)


# sqlplus's own session calls -- `BEGIN DBMS_OUTPUT.DISABLE; END;` and the like --
# which have nothing to do on a non-Oracle backend. Only these blocks, and the
# literal `EXEC :v := ...` idiom, are answered here; any other block is a real one
# and goes to the inner backend, which runs it or says why not (#1281).
_SESSION_CALL = re.compile(
    r'\s*BEGIN\s+DBMS_OUTPUT\.\w+(?:\s*\([^;]*\))?\s*;\s*END\s*;?\s*$', re.IGNORECASE
)
# sqlplus names itself as it logs in: `BEGIN DBMS_APPLICATION_INFO.SET_MODULE(:1,
# NULL); END;` (sqlplus 11.2), with the module bound. These three calls only set
# the session's tracing attributes, which the inner backend keeps if it can
# (set_end_to_end) -- so they are recorded, not run. Passed through instead, a
# PostgreSQL backend refused them and every sqlplus 11.2 login printed "Error
# accessing package DBMS_APPLICATION_INFO" (#1295).
_APP_INFO_CALL = re.compile(
    r'\s*BEGIN\s+DBMS_APPLICATION_INFO\.(SET_MODULE|SET_ACTION|SET_CLIENT_INFO)'
    r'\s*\(([^;]*)\)\s*;\s*END\s*;?\s*$',
    re.IGNORECASE,
)
# The tracing attributes each call sets, in argument order.
_APP_INFO_ATTRIBUTES = {
    'SET_MODULE': ('module', 'action'),
    'SET_ACTION': ('action',),
    'SET_CLIENT_INFO': ('client_info',),
}
_APP_INFO_ARGUMENT = re.compile(
    r"\s*(?::(\w+)|'((?:[^']|'')*)'|(NULL))\s*", re.IGNORECASE
)
_LITERAL_ASSIGNMENTS = re.compile(
    r"\s*BEGIN\s+(?::\w+\s*:=\s*(?:'(?:[^']|'')*'|-?\d+(?:\.\d+)?)\s*;\s*)+END\s*;?\s*$",
    re.IGNORECASE,
)


def _app_info_attributes(sql: str, binds: Sequence) -> dict | None:
    """The tracing attributes a DBMS_APPLICATION_INFO call sets, or ``None``
    when ``sql`` is not one (or passes something other than binds, literals and
    NULLs). Binds are positional, in the order they appear."""
    call = _APP_INFO_CALL.match(sql)
    if call is None:
        return None
    names = _APP_INFO_ATTRIBUTES[call.group(1).upper()]
    arguments = call.group(2).split(',') if call.group(2).strip() else []
    if len(arguments) > len(names):
        return None
    values = iter(binds)
    attrs: dict = {}
    for name, argument in zip(names, arguments):
        parsed = _APP_INFO_ARGUMENT.fullmatch(argument)
        if parsed is None:
            return None
        if parsed.group(1) is not None:
            bound = next(values, None)
            attrs[name] = bound.value if isinstance(bound, BindVar) else bound
        elif parsed.group(2) is not None:
            attrs[name] = parsed.group(2).replace("''", "'")
        else:
            attrs[name] = None
    return attrs


def _plsql_out_bind_values(sql: str) -> list:
    """Extract the OUT values from ``BEGIN :v := <literal>; ... END;``.

    Returns the assigned values in statement (= bind) order. Empty when the block
    has no simple literal assignments (a real PL/SQL call we just acknowledge).
    """
    values: list = []
    for match in _OUT_BIND_ASSIGN.finditer(sql):
        literal = match.group(1)
        if literal.startswith("'"):
            values.append(literal[1:-1].replace("''", "'"))
        elif '.' in literal:
            values.append(float(literal))
        else:
            values.append(int(literal))
    return values


def _varchar(name: bytes, width: int) -> ColumnMeta:
    return ColumnMeta(
        name=name, data_type=TNS_TYPE_VARCHAR, data_length=width, max_size=width
    )


class OracleCompatBackend:
    """Answer sqlplus's session-bootstrap queries; delegate the rest."""

    def __init__(self, inner: Backend) -> None:
        self._inner = inner
        self._user = 'USER'

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self._inner.capabilities

    @property
    def field_version(self) -> int | None:
        # Present whatever the wrapped backend does (the PG/SQLite demos pin 11.2).
        return getattr(self._inner, 'field_version', None)

    @property
    def tns_version(self) -> int | None:
        return getattr(self._inner, 'tns_version', None)

    @property
    def server_identity(self):
        # A backend may present a server release independent of its wire field
        # version (the PG demo reports 12.1 over the 11.2 wire); pass it through.
        return getattr(self._inner, 'server_identity', None)

    def authenticate(self, username: str) -> str | None:
        secret = self._inner.authenticate(username)
        if secret is not None:
            # Oracle folds an unquoted identifier to upper case; SELECT USER
            # reports it that way.
            self._user = username.upper()
        return secret

    def execute(self, sql: str, binds: Sequence = ()) -> Result:
        if getattr(self._inner, 'closed', False):
            # The inner backend's connection is gone -- a killed session (#1367).
            # Nothing may be answered from here then, not even SELECT USER, or
            # the dead session looks alive; the inner backend says why it can't.
            return self._inner.execute(sql, binds)
        normalized = ' '.join(sql.strip().upper().split())
        attrs = _app_info_attributes(sql, binds)
        if attrs is not None:
            record = getattr(self._inner, 'set_end_to_end', None)
            if record is not None:
                record(attrs)
            return Result()
        if normalized.startswith('BEGIN') and not any(
            isinstance(b, BindVar) for b in binds
        ):
            # Only sqlplus's own idioms are handled here: the bind-less
            # ``EXEC :v := <literal>`` assigns the OUT binds the client reads
            # back, and a DBMS_OUTPUT session call has no effect on a non-Oracle
            # backend. Every other block -- a callproc / callfunc, or an
            # anonymous block a script runs -- is the inner backend's to run or
            # refuse; answering it here with a success ran nothing (#1281).
            if _LITERAL_ASSIGNMENTS.match(sql):
                return Result(out_binds=_plsql_out_bind_values(sql))
            if _SESSION_CALL.match(sql):
                return Result()
        if 'PRODUCT_PRIVS' in normalized:
            # sqlplus's PRODUCT_PRIVS lookup, on a backend without the profile
            # table: sqlplus tolerates ORA-00942 there, printing the familiar
            # "Product user profile information not loaded" warning.
            raise BackendError(
                'table or view does not exist',
                ora_code=ORA_TABLE_OR_VIEW_DOES_NOT_EXIST,
            )
        if normalized == 'SELECT USER FROM DUAL':
            return Result(columns=[_varchar(b'USER', 30)], rows=[(self._user,)])
        if "DECODE('A','A','1','2')" in normalized.replace(' ', ''):
            # sqlplus's compatibility probe — DECODE('A','A','1','2') is '1'.
            return Result(columns=[_varchar(b'DECODE', 1)], rows=[('1',)])
        # A bare `SELECT <expr> FROM DUAL` maps to `SELECT <expr>` on the backend.
        if _FROM_DUAL.search(sql):
            return self._inner.execute(_FROM_DUAL.sub('', sql.strip()), binds)
        # Every other statement is a real one — the inner backend runs it (and,
        # being dialect-specific, translates Oracle SQL to its own dialect there).
        return self._inner.execute(sql, binds)

    def execute_many(self, sql: str, rows: Sequence[Sequence]) -> int | Result:
        # Array DML (executemany): hand the whole batch to the inner backend's own
        # array path when it has one (one round-trip instead of per row), else fall
        # back to a per-row loop through this wrapper's execute. Array DML is plain
        # INSERT / UPDATE / DELETE, so the sqlplus idioms execute() handles do not
        # apply — a raw delegate is correct.
        inner_many = getattr(self._inner, 'execute_many', None)
        if inner_many is not None:
            return inner_many(sql, rows)
        return sum(self.execute(sql, row).rowcount for row in rows)

    def execute_returning(self, sql: str, rows: Sequence[Sequence]) -> Result:
        # DML ... RETURNING col INTO :b (#689): the inner backend's own path when
        # it has one. There is nothing to translate here — a RETURNING statement
        # is plain INSERT / UPDATE / DELETE, so none of the sqlplus idioms
        # execute() handles apply. Without an inner path the attribute is absent
        # here too, and the session refuses the statement with an ORA error.
        inner = getattr(self._inner, 'execute_returning', None)
        if inner is None:
            raise UnsupportedFeature('RETURNING is not supported by this backend')
        return inner(sql, rows)

    def __getattr__(self, name: str) -> object:
        """Forward anything this wrapper does not define to the inner backend.

        The Mirror discovers a backend's OPTIONAL capabilities with
        ``getattr(backend, '<hook>', None)`` -- on the backend it was HANDED,
        which is this wrapper. Before this, a hook the wrapper did not name was
        invisible however well the backend beneath implemented it, and it failed
        silently: the Mirror simply concluded the capability was absent. The server
        probes for more than ten such hooks -- parse, describe, open_session,
        ping, bfile_exists, open_ref_cursor and others -- and the wrapper named
        none of them, because it names a fixed set and the server keeps growing
        new ones.

        Forwarding is the right default for a wrapper whose job is to answer a
        handful of sqlplus bootstrap queries and otherwise get out of the way.
        Everything it DOES define still wins -- ``__getattr__`` runs only when
        normal lookup fails -- so the interception this class exists for is
        untouched.

        It also keeps the Mirror's absence check honest: a hook the inner backend
        lacks raises AttributeError here, so ``getattr(..., None)`` returns None
        and the Mirror takes its fallback path, instead of finding a method that
        exists only to refuse.
        """
        return getattr(self._inner, name)

    def change_password(
        self, username: str, old_password: str, new_password: str
    ) -> None:
        # Delegate a password change to the inner backend (#515); a backend that
        # doesn't support it gets a clean ORA-01031 rather than an AttributeError.
        inner_change = getattr(self._inner, 'change_password', None)
        if inner_change is None:
            raise BackendError(
                'password change not supported', ora_code=ORA_INSUFFICIENT_PRIVILEGES
            )
        inner_change(username, old_password, new_password)

    def commit(self) -> None:
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()

    def close(self) -> None:
        self._inner.close()
