# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""A Mirror backend that relays to a real Oracle database via seerdb thin.

Turns the Mirror into a transparent Oracle-to-Oracle relay: a client speaks to
the Mirror, the Mirror runs each statement on a real Oracle and returns the real
results. Its purpose is conformance testing — running the integration suite
against the Mirror so that any failure isolates a *Mirror protocol* gap rather
than a backend SQL-dialect limitation (which is what the SQLite backend hits).

One backend (and one upstream Oracle connection) per Mirror session; the
credential map supplies the O5LOGON secret the Mirror needs to authenticate the
client, and the same credentials open the upstream connection.
"""

from __future__ import annotations

import functools
import re
import struct
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import TypeVar

import seerdb
from seerdb.common.datatypes import BFile, TempLob, dbtype_for_oracle_type
from seerdb.common.dbobject import (
    DbObject,
    DbObjectType,
    ObjectImage,
    decode_collection_keyed,
    decode_object_image,
)
from seerdb.common.sqltext import is_plsql
from seerdb.common.tns import AL16UTF16_CHARSET, ColumnMeta
from seerdb.common.tns_consts import (
    FIELD_VERSION_12_1,
    TNS_TYPE_ADT,
    TNS_TYPE_BFILE,
    TNS_TYPE_BLOB,
    TNS_TYPE_CLOB,
    TNS_TYPE_REF,
    TNS_TYPE_REFCURSOR,
)
from seerdb.common.types import reset_decode_bc_dates, set_decode_bc_dates
from seerdb.server.backend import (
    BackendError,
    BindVar,
    BlobValue,
    Capability,
    ClobValue,
    CursorResult,
    LtzValue,
    Result,
    SessionInfo,
)

_T = TypeVar('_T')


def _carries_bc_dates(method: Callable[..., _T]) -> Callable[..., _T]:
    # Decode a date before year 1 from upstream as a BcDate, not an error, for
    # the length of one backend call: the client decides what to do with it, as
    # it does against the real server (#1069). Per call, because the Mirror runs
    # every backend call in a copy of its own context, so a switch set in one
    # call is gone by the next. Every upstream fetch happens inside one of the
    # calls this wraps: rows, implicit results and nested cursors are drained
    # before it returns.
    @functools.wraps(method)
    def wrapper(*args: object, **kwargs: object) -> _T:
        token = set_decode_bc_dates(True)
        try:
            return method(*args, **kwargs)
        finally:
            reset_decode_bc_dates(token)

    return wrapper


class OraclePassthroughBackend:
    """Relays statements to a real Oracle at ``(host, port, service)``."""

    capabilities: frozenset[Capability] = frozenset()

    @staticmethod
    def detect_version(
        host: str,
        port: int,
        service: str,
        user: str,
        password: str,
        *,
        timeout: int = 15000,
    ) -> int | None:
        """Probe the target once and return the field version it negotiates, so a
        Mirror in front of it presents the same release (§ ``Backend.field_version``).

        A session's own upstream connection opens in :meth:`authenticate`, which
        runs mid-login — too late to drive the handshake the Mirror already sent.
        So the version is learned once at startup instead, from one short-lived
        connection. Returns ``None`` if the probe fails (the target is down, the
        credentials are wrong); the caller then falls back to the Mirror's default.
        """
        try:
            conn = seerdb.connect(
                host=host,
                port=port,
                service_name=service,
                user=user,
                password=password,
                timeout=timeout,
            )
        except Exception:
            return None
        try:
            return conn.field_version
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def __init__(
        self,
        *,
        host: str,
        port: int,
        service: str,
        credentials: dict[str, str],
        field_version: int | None = None,
        tns_version: int | None = None,
    ) -> None:
        # The Oracle release this passthrough presents to its own clients — set
        # it to match the target so the Mirror advertises what the real server
        # behind it speaks (the Mirror reads these off the backend). Left None,
        # the Mirror falls back to its launch default.
        self.field_version = field_version
        self.tns_version = tns_version
        self._host = host
        self._port = port
        self._service = service
        # Held by reference (not copied) so a changepassword updates the same map
        # every session's backend authenticates against — a fresh connection then
        # sees the new password (#21/#486). Keys are upper-cased in place.
        self._credentials = credentials
        # What the client said it is, filled in before authenticate() (#826).
        self._client_identity: dict[str, str] = {}
        for name in list(self._credentials):
            if name != name.upper():
                self._credentials[name.upper()] = self._credentials.pop(name)
        self._conn: seerdb.OracleConnect | None = None
        self._username = ''
        self._password = ''

    def authenticate(self, username: str) -> str | None:
        password = self._credentials.get(username.upper())
        if password is None:
            return None
        # The upstream connection is NOT opened here. It used to be, but the
        # driver banner and the edition arrive later, in the login AUTH, and
        # they are connect-time attributes upstream -- unreachable once the
        # session is open. `open_session` runs after that AUTH; this call only
        # answers with the secret the Mirror needs to build its O5LOGON
        # challenge, which it needs BEFORE the AUTH exists (#826).
        self._username = username
        self._password = password
        return password

    def open_session(self, attrs: dict | None = None) -> None:
        """Open the upstream session, now that the AUTH has been read (#826).

        Called after the client's proof is verified, so everything it declared
        at connect -- identity, driver banner, edition -- is in hand and can be
        passed to a connect rather than applied to an already-open session.
        """
        username = self._username
        password = self._password
        extra: dict = {}
        declared = (attrs or {}).get('driver_name')
        if declared:
            # Report the CLIENT's banner upstream, so v$session_connect_info
            # names the application rather than this relay.
            extra['driver_name'] = declared
        edition = (attrs or {}).get('edition')
        if edition:
            # The edition is chosen at LOGIN; deferring this connect until after
            # the AUTH is what makes it reachable at all.
            extra['edition'] = edition
        # autocommit=False so the client drives the upstream transaction through
        # the Mirror: an explicit commit / rollback reaches the backend, and an
        # autocommit client still commits because the Mirror calls backend.commit()
        # per statement. With the driver default (autocommit=True) every statement
        # would commit upstream and a client rollback would be a no-op.
        # A proxy login stays one upstream (#1093): seerdb's `user[proxy]` form
        # authenticates as `user` and opens the session as `proxy`, which is what
        # the client asked the Mirror for.
        proxy = (attrs or {}).get('proxy_client_name')
        self._conn = seerdb.connect(
            host=self._host,
            port=self._port,
            user=f'{username}[{proxy}]' if proxy else username,
            password=password,
            service_name=self._service,
            autocommit=False,
            # The backend wants LOB VALUES, not LOB objects: it reads each LOB's
            # content to queue for the Mirror's own read path. seerdb defaults
            # fetch_lobs=True now (#964), so this has to be explicit -- the same
            # one-line opt-out any consumer that wants str / bytes needs.
            fetch_lobs=False,
            # Present the CLIENT's identity upstream, not this process's, so
            # v$session shows who actually connected (#826). Each is None when
            # the client declared nothing, and the driver falls back to its own.
            **self._client_identity,
            **extra,
        )

    def set_client_identity(self, identity: dict) -> None:
        """The session identity the client declared, for the upstream connect.

        Called before :meth:`authenticate`, because that is where the upstream
        connection opens and program / machine / terminal / osuser are login-time
        attributes there as much as here -- they cannot be set afterwards (#826).
        """
        self._client_identity = {
            key: value
            for key, value in identity.items()
            if key in ('program', 'machine', 'terminal', 'osuser')
        }

    def bfile_exists(self, directory: str, filename: str) -> bool:
        """Does this BFILE's file exist upstream? (#1102)

        Asked of the real server, so its own answer reaches the client --
        including ORA-22285 for a DIRECTORY object that does not exist, which is
        a different thing from a file that is simply absent.
        """
        assert self._conn is not None  # authenticate() ran before this
        cursor = self._conn.cursor()
        try:
            # Asked in SQL: DBMS_LOB.FILEEXISTS answers 1 / 0 for a file, and
            # raises ORA-22285 itself when the DIRECTORY object does not exist --
            # which is the answer the client is waiting for.
            cursor.execute(
                'SELECT DBMS_LOB.FILEEXISTS(BFILENAME(:1, :2)) FROM dual',
                [directory, filename],
            )
            (present,) = cursor.fetchone()
            return bool(present)
        except seerdb.DatabaseError as exc:
            raise _relay_error(exc) from exc

    def ping(self) -> None:
        """Prove the UPSTREAM session is alive, not just this process (#826).

        A pool's health check is asking whether the database is reachable; a
        Mirror that answers from memory would keep handing out a connection
        whose upstream had gone away.
        """
        assert self._conn is not None  # authenticate() ran before this
        try:
            self._conn.ping()
        except seerdb.DatabaseError as exc:
            raise _relay_error(exc) from exc

    def set_app_context(self, entries: list[tuple[str, str, str]]) -> None:
        """Apply the application context the client declared at connect (#826).

        A real server takes these in the login itself; this session is already
        open by the time they arrive, so they go in through DBMS_SESSION, which
        is the supported way to set a CLIENTCONTEXT namespace on a live session
        and is what `sys_context()` then reads back.
        """
        assert self._conn is not None  # authenticate() ran before this
        cursor = self._conn.cursor()
        for namespace, attribute, value in entries:
            cursor.execute(
                'begin dbms_session.set_context(:1, :2, :3); end;',
                [namespace, attribute, value],
            )

    def session_info(self) -> SessionInfo:
        """The upstream session's real identity, for the Mirror's login reply.

        A client reads session_id / serial_num / instance_name / db_name /
        service_name from that reply alone -- never by querying -- so relaying
        the upstream's own values is the only way they come out right (#826).
        """
        assert self._conn is not None  # authenticate() ran before any execute
        cursor = self._conn.cursor()
        cursor.execute("""
            select sys_context('userenv', 'sid'),
                   sys_context('userenv', 'instance_name'),
                   sys_context('userenv', 'db_unique_name'),
                   sys_context('userenv', 'db_domain'),
                   sys_context('userenv', 'service_name')
              from dual""")
        sid, instance, db_name, db_domain, service = cursor.fetchone()
        # The serial has no sys_context equivalent. DBMS_DEBUG_JDWP reports it
        # to any user; v$session needs a grant an ordinary account does not have
        # (ORA-00942), and relying on it alone left every such client with the
        # Mirror's placeholder serial (#1236). A login must not fail over it.
        serial = 0
        for query in (
            'select dbms_debug_jdwp.current_session_serial from dual',
            "select serial# from v$session where sid = sys_context('userenv', 'sid')",
        ):
            try:
                cursor.execute(query)
                row = cursor.fetchone()
            except seerdb.DatabaseError:
                continue
            if row and row[0] is not None:
                serial = int(row[0])
                break
        return SessionInfo(
            session_id=int(sid) if sid else 0,
            serial_num=serial,
            instance_name=instance,
            db_name=db_name,
            db_domain=db_domain,
            service_name=service,
        )

    def _gettype_by_oid(self, oid: bytes) -> DbObjectType | None:
        # Resolve a type's 16-byte OID to its DbObjectType via all_types (the bind
        # OAC carries only the OID, not the name). None if it cannot be resolved.
        assert self._conn is not None
        if not oid:
            return None
        # A value's toid is the constructed 36-byte form (00 22 02 08 + OID +
        # extent); the OAC carries the bare 16-byte OID. Accept either.
        if len(oid) >= 20:
            oid = oid[4:20]
        probe = self._conn.cursor()
        probe.execute(
            'SELECT owner, type_name FROM all_types WHERE type_oid = :1', [oid]
        )
        row = probe.fetchone()
        if row is not None:
            owner, name = row
            return self._conn.gettype(f'{owner}.{name}')
        # A PL/SQL package-level type is not in all_types; it lives in
        # all_plsql_types, which carries a type_oid for records and collections
        # alike. The OID is all the bind OAC gives, so the name comes from there
        # and the driver describes it (#888/#1030).
        probe.execute(
            'SELECT owner, package_name, type_name '
            'FROM all_plsql_types WHERE type_oid = :1',
            [oid],
        )
        prow = probe.fetchone()
        if prow is None:
            return None
        return self._conn.gettype('.'.join(prow))

    def _object_from_image(self, image: ObjectImage) -> object:
        # Turn an inbound object (ADT) bind's image back into a DbObject the
        # upstream connection can bind (#888): resolve the type, decode the image
        # against its layout, and rebuild the object (or collection). None if the
        # OID cannot be resolved -- the caller then binds NULL rather than
        # desyncing.
        typ = self._gettype_by_oid(image.type_oid or b'')
        if typ is None:
            return None
        lob_contents = getattr(image, 'lob_contents', None) or {}
        if getattr(typ, 'is_collection', False):
            (elements, keys) = decode_collection_keyed(image.image, typ.element)
            obj = typ.newobject(list(elements), keys=keys)
        else:
            attrs = decode_object_image(image.image, typ.attrs)
            obj = typ.newobject(dict(attrs))
        self._materialize_bind_object_lobs(obj, lob_contents)
        return obj

    def _materialize_bind_object_lobs(self, value: object, lob_contents: dict) -> None:
        # Replace each LOB attribute's locator (a Mirror locator the client echoed
        # from a fetch or a createlob) with an upstream LOB carrying the content
        # the Mirror served, so the upstream bind sees a real LOB rather than a
        # dangling locator (#888). A locator with no known content binds NULL --
        # far better than an ORA-22275 desync. Nested objects / collections recurse.
        if not isinstance(value, DbObject):
            return
        typ = value._dbtype
        if typ is not None and typ.is_collection:
            element = typ.element or {}
            for idx, elem in enumerate(value._elements):
                value._elements[idx] = self._materialize_member_lob(
                    elem, element, lob_contents
                )
            return
        if typ is None:
            return
        for attr in typ.attrs:
            name = attr['name']
            value._attrs[name] = self._materialize_member_lob(
                value._attrs.get(name), attr, lob_contents
            )

    def _materialize_member_lob(
        self, value: object, attr: dict, lob_contents: dict
    ) -> object:
        from seerdb.common.lob import LOB

        if attr.get('object_type') is not None:
            if value is not None:
                self._materialize_bind_object_lobs(value, lob_contents)
            return value
        if value is None:
            return value
        data_type = attr.get('data_type')
        if data_type not in (TNS_TYPE_CLOB, TNS_TYPE_BLOB) or not isinstance(
            value, (bytes, bytearray)
        ):
            return value
        # `value` is the LOB locator the client sent inside the image. Resolve it
        # to the content the Mirror served, then stream that into an upstream temp
        # LOB and bind the object attribute to it.
        entry = _lookup_bind_lob(lob_contents, bytes(value))
        if entry is None:
            return None
        content, _is_clob = entry
        is_blob = data_type == TNS_TYPE_BLOB
        assert self._conn is not None
        locator = self._conn.create_temp_lob(is_blob=is_blob)
        if content and isinstance(content, (str, bytes)):
            self._conn.write_temp_lob(locator, content, is_blob=is_blob)
        return LOB(data_type, locator, self._conn)

    def _resolve_fetched_object_lobs(self, columns: list, rows: list) -> list:
        # An object (ADT) column's LOB attributes decode upstream to bare locator
        # bytes (seerdb leaves a LOB attribute's content unread). The external
        # client, though, reads each such attribute back over TTI_LOBOPS against
        # the Mirror -- which serves LOB content it has already read, not upstream
        # locators -- so resolve every object LOB attribute to its content now,
        # while the upstream connection is in hand (#888). Non-object columns and
        # objects without LOB attributes are untouched.
        adt_positions = [
            i for i, col in enumerate(columns) if int(col.data_type) == TNS_TYPE_ADT
        ]
        if not adt_positions:
            return rows
        resolved: list = []
        for row in rows:
            cells = list(row)
            for i in adt_positions:
                if cells[i] is not None:
                    self._resolve_object_lobs(cells[i])
            resolved.append(tuple(cells))
        return resolved

    def _resolve_object_lobs(self, value: object) -> None:
        # Walk an object (or collection) value in place, replacing each LOB
        # attribute's locator bytes with the content read from upstream. Nested
        # objects and collections recurse. Mirrors _object_lob_contents' walk.
        if not isinstance(value, DbObject):
            return
        typ = value._dbtype
        if typ is not None and typ.is_collection:
            element = typ.element or {}
            for idx, elem in enumerate(value._elements):
                value._elements[idx] = self._resolve_member_lob(elem, element)
            return
        if typ is None:
            return
        for attr in typ.attrs:
            name = attr['name']
            value._attrs[name] = self._resolve_member_lob(value._attrs.get(name), attr)

    def _resolve_member_lob(self, value: object, attr: dict) -> object:
        # One attribute / element: recurse into a nested object / collection, read
        # a LOB attribute's content, or leave a plain scalar as-is.
        from seerdb.common.lob import LOB

        if attr.get('object_type') is not None:
            if value is not None:
                self._resolve_object_lobs(value)
            return value
        if value is None:
            return value
        data_type = attr.get('data_type')
        if data_type in (TNS_TYPE_CLOB, TNS_TYPE_BLOB) and isinstance(
            value, (bytes, bytearray)
        ):
            # The decoded attribute is the raw LOB locator; read its content over
            # the upstream connection so the Mirror can serve it back.
            return LOB(data_type, bytes(value), self._conn).read()
        return value

    def _upstream_temp_lob(self, value: object, is_blob: bool) -> TempLob:
        # Make an upstream temp LOB holding `value` and return the marker that
        # binds it: seerdb has no CLOB / BLOB value bind of its own.
        assert self._conn is not None
        locator = self._conn.create_temp_lob(is_blob=is_blob)
        if value and isinstance(value, (str, bytes)):
            self._conn.write_temp_lob(locator, value, is_blob=is_blob)
        return TempLob(locator, is_blob)

    def _upstream_lob_binds(self, binds: Sequence) -> list:
        # A LOB the client bound into an ordinary statement arrives as its
        # content, marked ClobValue / BlobValue (#1067). Bound on as a str it
        # would be a VARCHAR2, which the upstream refuses past 32 KB (ORA-01461),
        # so bind it as a LOB again. A PL/SQL block's binds are BindVars, whose
        # LOB-ness the PL/SQL path already keeps (#981), and an upstream older
        # than 12.1 has no temp LOB to put it in (#91), so both keep the value.
        if not any(isinstance(b, (ClobValue, BlobValue)) for b in binds):
            return list(binds)
        if getattr(self._conn, 'field_version', 0) < FIELD_VERSION_12_1:
            return list(binds)
        return [
            self._upstream_temp_lob(b, isinstance(b, BlobValue))
            if isinstance(b, (ClobValue, BlobValue))
            else b
            for b in binds
        ]

    @staticmethod
    def _declare_ltz_binds(cursor: object, rows: Sequence[Sequence]) -> None:
        # A TIMESTAMP WITH LOCAL TIME ZONE bind arrives as an LtzValue (#1222).
        # Bound on as a bare datetime it would be a TIMESTAMP, which the upstream
        # reads in the session zone rather than the database's, so declare each
        # position that holds one as LTZ again (#1225).
        width = max((len(row) for row in rows), default=0)
        ltz = {
            i
            for row in rows
            for i, value in enumerate(row)
            if isinstance(value, LtzValue)
        }
        if ltz:
            cursor.setinputsizes(  # type: ignore[attr-defined]
                *(
                    seerdb.DB_TYPE_TIMESTAMP_LTZ if i in ltz else None
                    for i in range(width)
                )
            )

    def _resolve_object_binds(self, binds: Sequence) -> list:
        # Replace any object (ADT) bind -- a bare ObjectImage, or one wrapped in a
        # BindVar -- with the DbObject the upstream binds. A NULL object arrives
        # as None already, so only a populated image needs resolving (#888).
        out: list = []
        for b in binds:
            value = b.value if isinstance(b, BindVar) else b
            if isinstance(value, ObjectImage):
                obj = self._object_from_image(value)
                out.append(replace(b, value=obj) if isinstance(b, BindVar) else obj)
            else:
                out.append(b)
        return out

    @_carries_bc_dates
    def open_ref_cursor(self, sql: str, skip: int = 0) -> object:
        # A cursor to hand a PL/SQL block as an open `sys_refcursor` IN
        # parameter (#1048). It has to be left OPEN with its rows unread, which
        # is exactly what a scrollable cursor is: the ordinary path drains a
        # query to EOF and queues the cursor for close, so its id would name
        # nothing the block could fetch from. The driver refuses to bind
        # anything else (#1047), so this is not a preference.
        assert self._conn is not None  # authenticate() ran before any execute
        cursor = self._conn.cursor(scrollable=True)
        # `skip` rows have already gone to the client under this cursor id, and
        # the block resumes after them -- so the open consumes exactly that many
        # and leaves the rest. Zero reads nothing, which is the common case.
        cursor.prefetchrows = skip
        try:
            cursor.execute(sql)
        except seerdb.DatabaseError as exc:
            raise _relay_error(exc) from exc
        return cursor

    def parse(self, sql: str) -> None:
        # `cursor.parse()` of anything that is not a query (#1019). The upstream
        # is a real Oracle, so the honest answer is to ask it — the driver's own
        # `cursor.parse()` sends PARSE without EXECUTE (#1018), which parses the
        # statement and runs nothing. Without this the Mirror answers its own
        # bare success status and every parse-time error is lost: a client asking
        # whether `returning IntCol into :ROWID` is valid is told yes, where a
        # real server answers ORA-01745.
        assert self._conn is not None  # authenticate() ran before any execute
        cursor = self._conn.cursor()
        try:
            cursor.parse(sql)
        except seerdb.DatabaseError as exc:
            raise _relay_error(exc) from exc

    @_carries_bc_dates
    def execute(self, sql: str, binds: Sequence = ()) -> Result:
        assert self._conn is not None  # authenticate() ran before any execute
        cursor = self._conn.cursor()
        binds = self._resolve_object_binds(binds)
        binds = self._upstream_lob_binds(binds)
        # A PL/SQL block hands its binds over as BindVar (value + type + buffer
        # size) so OUT binds can be registered correctly (#483). Bind each as an
        # OUT-capable Var seeded with the input value, run, and return every Var's
        # value; the Mirror marks them OUT and the client keeps its own positions.
        if any(isinstance(b, BindVar) for b in binds):
            if is_plsql(sql):
                return self._execute_plsql(cursor, sql, binds)
            # An ordinary statement's BindVar is either a typed NULL (#699) or a
            # LOB the Mirror could not bind as a bare value -- an empty CLOB /
            # BLOB, which stores NULL when bound as a plain '' / b'' (#903).
            # Route a LOB-typed bind through an upstream temp LOB so it stores as
            # a non-NULL LOB (seerdb has no CLOB / BLOB Var-bind); declare every
            # other typed NULL the way the client did and bind its value.
            sizes: list = []
            resolved: list = []
            for b in binds:
                if isinstance(b, BindVar) and b.tns_type in (
                    TNS_TYPE_CLOB,
                    TNS_TYPE_BLOB,
                ):
                    sizes.append(None)
                    resolved.append(
                        self._upstream_temp_lob(b.value, b.tns_type == TNS_TYPE_BLOB)
                    )
                elif isinstance(b, BindVar):
                    sizes.append(dbtype_for_oracle_type(b.tns_type, b.csfrm))
                    resolved.append(b.value)
                else:
                    sizes.append(
                        seerdb.DB_TYPE_TIMESTAMP_LTZ
                        if isinstance(b, LtzValue)
                        else None
                    )
                    resolved.append(b)
            cursor.setinputsizes(*sizes)
            binds = resolved
        else:
            self._declare_ltz_binds(cursor, [binds])
        try:
            cursor.execute(sql, list(binds))
        except seerdb.DatabaseError as exc:
            raise _relay_error(exc) from exc
        if cursor.description:
            columns = [_to_column_meta(desc) for desc in cursor.description]
            rows = cursor.fetchall()
            columns = _enrich_ref_columns(columns, rows)
            rows = self._resolve_fetched_object_lobs(columns, rows)
            rows = _drain_nested_cursors(columns, rows)
            rows = _resolve_bfiles(rows)
            return Result(columns=columns, rows=rows)
        implicit = self._drain_implicit_results(cursor)
        if implicit:
            return Result(implicit_results=implicit)
        # A CREATE whose PL/SQL object compiled with errors succeeded upstream
        # and said so on the upstream cursor; relay it so the client sees the
        # same warning it would from the real server (#995).
        return Result(
            rowcount=cursor.rowcount or 0,
            compilation_warning=getattr(cursor, 'warning', None) is not None,
            # The row the statement touched, as the upstream reported it (#1077).
            last_rowid=getattr(cursor, 'lastrowid', None),
        )

    def _drain_implicit_results(
        self, cursor: object
    ) -> list[tuple[list[ColumnMeta], list[tuple]]]:
        # The result sets a PL/SQL block returned through DBMS_SQL.RETURN_RESULT
        # (#121/#826). seerdb's cursor surfaces them PEP 249 style: nextset()
        # makes each current in turn. A block that returned none leaves the list
        # empty and nothing changes.
        # Guarded rather than assumed: nextset() is optional on a DB-API cursor,
        # and a statement that returned nothing must not fail over its absence.
        nextset = getattr(cursor, 'nextset', None)
        if nextset is None:
            return []
        out: list[tuple[list[ColumnMeta], list[tuple]]] = []
        while nextset():
            columns = [
                _to_column_meta(desc)
                for desc in cursor.description  # type: ignore[attr-defined]
            ]
            out.append((columns, cursor.fetchall()))  # type: ignore[attr-defined]
        return out

    def execute_many(self, sql: str, rows: Sequence[Sequence]) -> Result:
        # Array DML (executemany): send the whole batch upstream in one round-trip
        # through seerdb's own cursor.executemany — one parse, len(rows) iterations
        # — instead of the Mirror's per-row fallback (one upstream round-trip per
        # bind row, which paid the network latency once per row). Returns the total
        # affected-row count. The Mirror calls this only for the non-batcherrors
        # path, where an upstream failure aborts the whole batch — exactly Oracle's
        # own non-batcherrors semantics.
        assert self._conn is not None  # authenticate() ran before any execute
        cursor = self._conn.cursor()
        self._declare_ltz_binds(cursor, rows)
        try:
            cursor.executemany(sql, [list(row) for row in rows])
        except seerdb.DatabaseError as exc:
            # The batch aborted part-way, and the rows before the failing one
            # really were applied -- the upstream cursor's rowcount says how
            # many, which is exactly what a real server reports (#998). Clamped
            # at zero: DB-API leaves rowcount -1 when it means nothing, and a
            # negative count is not a smaller number of rows, it is no answer.
            raise _relay_error(exc, max(cursor.rowcount or 0, 0)) from exc
        # With the last inserted row's rowid, as the upstream reported it (#1077).
        return Result(
            rowcount=cursor.rowcount or 0, last_rowid=getattr(cursor, 'lastrowid', None)
        )

    def execute_many_rowcounts(
        self, sql: str, rows: Sequence[Sequence]
    ) -> tuple[int, list[int]]:
        # Array DML with the per-iteration affected-row counts (arraydmlrowcounts,
        # #18): run the batch upstream asking for them, and return the total plus
        # the count list the client reads back through getarraydmlrowcounts().
        assert self._conn is not None  # authenticate() ran before any execute
        cursor = self._conn.cursor()
        self._declare_ltz_binds(cursor, rows)
        try:
            cursor.executemany(sql, [list(row) for row in rows], arraydmlrowcounts=True)
        except seerdb.DatabaseError as exc:
            # The batch aborted part-way, and the rows before the failing one
            # applied. The upstream cursor still holds their per-iteration
            # counts -- the client asked for them, so they belong in the error
            # reply as much as in a successful one (#1031).
            error = _relay_error(exc, max(cursor.rowcount or 0, 0))
            try:
                error.row_counts = list(cursor.getarraydmlrowcounts())
            except seerdb.DatabaseError:
                pass  # the upstream has none to give; the error still stands
            raise error from exc
        return cursor.rowcount or 0, list(cursor.getarraydmlrowcounts())

    @_carries_bc_dates
    def execute_returning(self, sql: str, rows: Sequence[Sequence]) -> Result:
        # DML ... RETURNING col INTO :b (#689). The upstream is a real Oracle, so
        # the statement goes over unchanged; only the binds the clause fills need
        # building, as Vars of the type the client declared. One Var per return
        # bind is shared across the whole batch, which is how the driver reports
        # per-iteration values: getvalue(i) is what iteration i returned.
        assert self._conn is not None  # authenticate() ran before any execute
        cursor = self._conn.cursor()
        positions = [i for i, b in enumerate(rows[0]) if isinstance(b, BindVar)]
        receivers = {}
        for i in positions:
            bind = rows[0][i]
            # An object / collection receiver has to be typed, or the upstream
            # builds an untyped Var and the server refuses the statement with
            # ORA-00932 "... which is incompatible with expected data type CHAR".
            # The client sends the type OID in the bind's OAC and the Mirror now
            # threads it through (#826).
            objtype = self._gettype_by_oid(bind.toid) if bind.toid else None
            if objtype is not None:
                receivers[i] = cursor.var(objtype)
                continue
            dbtype = dbtype_for_oracle_type(bind.tns_type, bind.csfrm)
            # NOT sized to `bind.max_size`. That is the *client's* buffer, and
            # sizing the upstream receiver with it made the UPSTREAM truncate:
            # the Mirror then had the cut-down value and no idea of the real
            # length, so it could not tell the client its variable was too small
            # (#1023). Take the whole value here and let the response encoder cut
            # it to the client's size, the way a real server does.
            receivers[i] = (
                cursor.var(dbtype, _RECEIVER_SIZE)
                if dbtype is not None
                else cursor.var(str, _RECEIVER_SIZE)
            )
        # Each row's remaining (input) binds go through the same object
        # resolution execute() applies: an ObjectImage is the row decoder's
        # placeholder, not something the upstream driver can bind, and handing
        # one over kills the connection rather than raising (#826).
        batch = [
            self._resolve_object_binds(
                [
                    receivers[i] if i in receivers else value
                    for i, value in enumerate(row)
                ]
            )
            for row in rows
        ]
        self._declare_ltz_binds(cursor, batch)
        try:
            if len(batch) > 1:
                cursor.executemany(sql, batch)
            else:
                cursor.execute(sql, batch[0])
        except seerdb.DatabaseError as exc:
            raise _relay_error(exc) from exc
        # Per iteration, each receiver yields the list of values for the rows that
        # iteration affected. Transpose to rows so every returned row carries one
        # value per return bind, in bind order.
        returned = []
        for iteration in range(len(batch)):
            columns = [receivers[i].getvalue(iteration) or [] for i in positions]
            returned.append([tuple(values) for values in zip(*columns)])
        return Result(rowcount=cursor.rowcount or 0, returned_rows=returned)

    def _execute_plsql(self, cursor, sql: str, binds: Sequence) -> Result:
        # Each PL/SQL bind is registered as an OUT-capable Var (the wire carries
        # no direction), including a resolved temp-LOB CLOB / BLOB value (#91):
        # a block that WRITES to such a bind needs its new content back, and a
        # LOB Var now round-trips one (#978/#979). seerdb re-promotes the value
        # through an upstream temp LOB either way.
        variables = []
        for bind in binds:
            if (
                bind.tns_type in (TNS_TYPE_CLOB, TNS_TYPE_BLOB)
                and bind.value is not None
            ):
                if getattr(self._conn, 'field_version', 0) < FIELD_VERSION_12_1:
                    # Below 12.1 a LOB Var has no bind encoding at all (#902),
                    # so bind the plain str / bytes. That is IN-only -- an
                    # IN OUT LOB is simply out of reach on such an upstream --
                    # but it is what the server can take.
                    variables.append(bind.value)
                    continue
                Var = cursor.var(
                    seerdb.DB_TYPE_BLOB
                    if bind.tns_type == TNS_TYPE_BLOB
                    else seerdb.DB_TYPE_CLOB
                )
                Var.setvalue(0, bind.value)
                variables.append(Var)
                continue
            if bind.tns_type == TNS_TYPE_ADT:
                # An object (ADT) bind (#888). A populated IN value was already
                # turned into a DbObject by _resolve_object_binds and binds as a
                # plain value. A None value is an object OUT bind (a function
                # returning an object/collection) or a typed NULL: register a Var
                # of the type so the result comes back typed and a NULL carries
                # its type for overload resolution. The OAC's OID rides on the
                # BindVar; if it cannot be resolved, fall through to bind None.
                if bind.value is not None:
                    variables.append(bind.value)
                    continue
                objtype = self._gettype_by_oid(bind.toid)
                if objtype is not None:
                    variables.append(cursor.var(objtype))
                    continue
            dbtype = dbtype_for_oracle_type(bind.tns_type, bind.csfrm)
            if bind.array_size:
                # An associative-array bind (#743): an array variable of the
                # declared capacity, seeded with the elements the client sent
                # (none for a pure OUT); getvalue() returns the list afterwards.
                var = cursor.arrayvar(
                    dbtype if dbtype is not None else str, bind.array_size
                )
                if bind.value:
                    var.setvalue(0, list(bind.value))
                variables.append(var)
                continue
            if bind.tns_type == TNS_TYPE_REFCURSOR:
                if bind.value is not None and not isinstance(bind.value, int):
                    # An IN bind the Mirror already resolved: it turned the
                    # client's cursor id into an open upstream cursor,
                    # positioned where the client left off, and put the cursor
                    # object here (#1047/#1048). A bare int means it did not:
                    # that is the decoded cursor id, and the OUT form's is 0.
                    variables.append(bind.value)
                    continue
                # A REF CURSOR OUT param: the DB opens the cursor, so bind a
                # cursor var and don't seed it.
                var = cursor.var(seerdb.DB_TYPE_CURSOR)
            else:
                size = bind.max_size if bind.max_size and bind.max_size > 0 else None
                var = (
                    cursor.var(dbtype, size) if dbtype is not None else cursor.var(str)
                )
                if bind.value is not None:
                    var.setvalue(0, bind.value)
            variables.append(var)
        try:
            cursor.execute(sql, variables)
        except seerdb.DatabaseError as exc:
            raise _relay_error(exc) from exc
        # A Var yields its assigned value (a nested cursor for a REF CURSOR).
        # An object bound as a bare DbObject has no getvalue() -- the driver
        # writes an OUT value straight into it (#1029) -- so the object IS the
        # value. Reading it through getvalue() reported None, and the Mirror
        # then sent nothing back: the client saw its own input unchanged, which
        # is the OUT half of a callproc silently doing nothing (#1032). Any
        # other plain-value position (a LOB IN) genuinely has no OUT value.
        return Result(
            out_binds=[
                _out_value(v.getvalue())
                if hasattr(v, 'getvalue')
                else (v if isinstance(v, DbObject) else None)
                for v in variables
            ],
            # What the upstream server said each bind was. Only it knows -- the
            # wire carries no direction on the way in -- so relaying this is the
            # difference between telling the client the truth and marking every
            # bind OUT (#1064).
            bind_directions=tuple(getattr(cursor, 'bind_directions', None) or ()),
        )

    def sessionless_begin(self, transaction_id: bytes, timeout: int) -> None:
        # Start a sessionless transaction on the upstream connection so the
        # client's work is isolated there and resumable from another session.
        assert self._conn is not None
        try:
            self._conn.begin_sessionless_transaction(transaction_id, timeout=timeout)
        except seerdb.DatabaseError as exc:
            raise _relay_error(exc) from exc

    def sessionless_resume(self, transaction_id: bytes, timeout: int) -> None:
        assert self._conn is not None
        try:
            self._conn.resume_sessionless_transaction(transaction_id, timeout=timeout)
        except seerdb.DatabaseError as exc:
            raise _relay_error(exc) from exc

    def sessionless_suspend(self) -> None:
        assert self._conn is not None
        try:
            self._conn.suspend_sessionless_transaction()
        except seerdb.DatabaseError as exc:
            raise _relay_error(exc) from exc

    def change_password(
        self, username: str, old_password: str, new_password: str
    ) -> None:
        # Relay the change as the same protocol call the client made, through the
        # upstream connection's own changepassword; the live upstream session
        # stays authenticated. Not as ALTER USER ... REPLACE: the server rejects
        # the two routes with different codes -- ORA-28218 for the SQL, where
        # the protocol call gets the ORA-01017 a client expects (#1089). Then
        # update the shared credential map so a fresh Mirror session
        # authenticates (O5LOGON) with the new password and the old one is
        # rejected (#21/#486).
        assert self._conn is not None  # authenticate() ran before any execute
        try:
            self._conn.changepassword(old_password, new_password)
        except seerdb.DatabaseError as exc:
            raise _relay_error(exc) from exc
        self._credentials[username.upper()] = new_password

    # The client's attribute names for the end-to-end tracing slots the Mirror
    # hands over; client_info is spelled clientinfo on the connection.
    _END_TO_END_ATTR = {'client_info': 'clientinfo'}

    def set_end_to_end(self, attrs: dict[str, str | None]) -> None:
        # Apply the session's end-to-end tracing attributes (module, action,
        # client_identifier, client_info, dbop) to the upstream connection, so
        # SYS_CONTEXT('USERENV', …) on the real Oracle reflects what the client
        # set through the Mirror. An upstream below 12.1 has no way to carry them
        # (the client raises NotSupportedError); that is a limit of the upstream,
        # not a Mirror failure, so it is ignored.
        assert self._conn is not None  # authenticate() ran before any call
        for name, value in attrs.items():
            try:
                setattr(self._conn, self._END_TO_END_ATTR.get(name, name), value)
            except seerdb.NotSupportedError:
                return

    def commit(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    def rollback(self) -> None:
        if self._conn is not None:
            self._conn.rollback()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


# Oracle's own error text already begins with "ORA-NNNNN: "; the Mirror
# (BackendError) re-adds that prefix from the code, so relaying str(exc) verbatim
# doubles it ("ORA-00904: ORA-00904: ..."). Strip the leading prefix and take the
# code from it (falling back to exc.code, then ORA-00900) so the Mirror emits
# exactly one, matching a real server.
_ORA_PREFIX = re.compile(r'^ORA-(\d{5}):\s*')


# How big a RETURNING receiver the passthrough declares upstream. The client's
# own declared size must not be used (see execute_returning), and a VARCHAR2 /
# RAW column cannot exceed 32767 bytes even with extended sizes on, so this
# takes any value a return bind can carry.
_RECEIVER_SIZE = 32767


def _resolve_bfiles(rows: list) -> list:
    """Hand a BFILE over as its two names (#1102).

    A BFILE has no content to read here -- reading one opens a file on the
    server -- so the upstream LOB is turned into the pair the Mirror builds a
    locator from, and the client's later FILE_* calls come back through
    ``bfile_exists``.
    """
    out = []
    for row in rows:
        if not any(getattr(v, 'is_file', False) for v in row):
            out.append(row)
            continue
        out.append(
            tuple(
                BFile(v.directory_name, v.filename)
                if getattr(v, 'is_file', False)
                else v
                for v in row
            )
        )
    return out


def _relay_error(exc: 'seerdb.DatabaseError', rowcount: int = 0) -> BackendError:
    text = str(exc)
    match = _ORA_PREFIX.match(text)
    code = getattr(exc, 'code', None)
    if code is None and match is not None:
        code = int(match.group(1))
    if match is not None:
        text = text[match.end() :]
    # Relay the parse offset (oracledb's DatabaseError.offset) so the Mirror draws
    # the error caret under the same column the real server flagged.
    return BackendError(
        text,
        ora_code=code or 900,
        error_offset=getattr(exc, 'offset', None),
        rowcount=rowcount,
    )


def _enrich_ref_columns(columns: list, rows: list) -> list:
    # A REF column's type identity (type_name / schema / OID) is not in the
    # PEP-249 description — only in the DbRef values — so copy it from the first
    # non-null value into the ColumnMeta the describe carries (#494).
    out = list(columns)
    for idx, col in enumerate(out):
        if col.data_type != TNS_TYPE_REF:
            continue
        for row in rows:
            ref = row[idx]
            if ref is not None and hasattr(ref, 'type_name'):
                out[idx] = replace(
                    col,
                    type_name=(ref.type_name or '').encode('ascii'),
                    type_schema=(ref.type_schema or '').encode('ascii'),
                    type_oid=getattr(ref, 'type_oid', b'') or b'',
                )
                break
    return out


def _lookup_bind_lob(lob_contents: dict, locator: bytes) -> tuple[object, bool] | None:
    # The content the Mirror served under an object-bind LOB attribute's locator.
    # A temp LOB's locator rides in the image behind a ub2 length prefix (the
    # server hands temp locators out ub2-prefixed, and the client echoes that
    # into the image), while a fetched column LOB's is bare -- so try the locator
    # as-is, then with a leading ub2 length prefix stripped (#888).
    entry = lob_contents.get(locator)
    if entry is not None:
        return entry
    if len(locator) >= 2 and struct.unpack('>H', locator[:2])[0] == len(locator) - 2:
        return lob_contents.get(locator[2:])
    return None


def _drain_nested_cursors(columns: list, rows: list) -> list:
    # A nested-cursor cell -- `select ..., CURSOR(select ...) from ...` -- comes
    # back as a fetchable cursor. Drain each into a CursorResult the Mirror parks
    # and names, the same shape a REF CURSOR OUT param produces (#826).
    positions = [
        index
        for index, col in enumerate(columns)
        if col.data_type == TNS_TYPE_REFCURSOR
    ]
    if not positions:
        return rows
    out = []
    for row in rows:
        cells = list(row)
        for index in positions:
            value = cells[index] if index < len(cells) else None
            if value is not None:
                cells[index] = _out_value(value)
        out.append(tuple(cells))
    return out


def _out_value(value: object) -> object:
    # A REF CURSOR OUT param resolves to a nested cursor; drain its describe +
    # rows into a CursorResult the Mirror can park and hand back. Any other OUT
    # value is a plain scalar the Mirror encodes by the bind's declared type.
    if hasattr(value, 'description') and hasattr(value, 'fetchall'):
        columns = [_to_column_meta(desc) for desc in value.description]
        # Recursive: a nested cursor may itself select a CURSOR(...) column, so
        # drain its rows the same way (#826).
        return CursorResult(
            columns=columns, rows=_drain_nested_cursors(columns, value.fetchall())
        )
    return value


def _to_column_meta(desc: tuple) -> ColumnMeta:
    # PEP-249 description tuple: (name, type_code, display_size, internal_size,
    # precision, scale, null_ok). type_code is a seerdb DB_TYPE carrying the raw
    # wire tns_type; the sizes give the declared/buffer length.
    name, type_code, display_size, internal_size, precision, scale, null_ok = desc
    tns_type = getattr(type_code, 'tns_type', type_code)
    csfrm = getattr(type_code, 'csfrm', 1)
    # A native VECTOR column's element format (FetchInfo.vector_format, #55) so the
    # Mirror re-encodes the value image with the right element type; None when the
    # upstream describe does not report it (falls back to FLOAT32).
    vector_format = getattr(desc, 'vector_format', None)
    vector_dimensions = getattr(desc, 'vector_dimensions', None)
    # The flags byte says whether the dimension count means anything: a flexible
    # column allows ANY number, and its count is 0 (#1013).
    vector_flags = getattr(desc, 'vector_flags', None)
    # An object (ADT) column carries its type identity in the describe (unlike a
    # REF, whose identity is enriched from values below); re-emit it so the
    # external client can resolve the object type (#888). FetchInfo exposes it.
    type_oid = type_schema = type_name = b''
    if int(tns_type) == TNS_TYPE_ADT:
        type_oid = getattr(desc, 'type_oid', None) or b''
        type_schema = (getattr(desc, 'type_schema', None) or '').encode('ascii')
        type_name = (getattr(desc, 'type_name', None) or '').encode('ascii')
        # DB_TYPE_OBJECT and DB_TYPE_XMLTYPE share one wire type number (ADT);
        # the external client tells them apart by the character-set form, mapping
        # csfrm 0 to a generic object and csfrm 1 (implicit) to XMLType (its later
        # SYS.XMLTYPE name check only tags the type, it never undoes that dbtype).
        # A real server describes an object column with csfrm 0 and charset 0, so
        # emit those here -- the FetchInfo's bare wire type carries no csfrm, and
        # the default (1) would surface every object as XMLType (#888).
        csfrm = 0
    elif int(tns_type) == TNS_TYPE_BFILE:
        # A BFILE describes with charset 0 / csfrm 0 too: it carries the two
        # names, not character data (#1102).
        csfrm = 0
    byte_size = internal_size or display_size or 0
    if csfrm == 2:
        # National char (NCHAR / NVARCHAR2): UTF-16BE in AL16UTF16. data_length is
        # the byte buffer (internal_size), max_size the declared character length.
        data_length, max_size = byte_size, (display_size or byte_size)
        charset = AL16UTF16_CHARSET
    elif int(tns_type) in (TNS_TYPE_ADT, TNS_TYPE_BFILE):
        # A BFILE describes with charset 0 / csfrm 0, like an object: it carries
        # no character data of its own, only the two names (#1102).
        data_length = max_size = byte_size
        charset = 0
    else:
        data_length = max_size = byte_size
        charset = ColumnMeta.charset
    return ColumnMeta(
        name=name.encode('utf-8'),
        data_type=int(tns_type),
        data_length=data_length,
        max_size=max_size,
        precision=precision or 0,
        scale=scale or 0,
        charset=charset,
        csfrm=csfrm,
        null_ok=int(bool(null_ok)),
        vector_format=vector_format,
        vector_dimensions=vector_dimensions,
        vector_flags=vector_flags,
        type_oid=type_oid,
        type_schema=type_schema,
        type_name=type_name,
        # Re-mark a JSON / OSON column so the external client decodes it (#826).
        is_json=bool(getattr(desc, 'is_json', False)),
        is_oson=bool(getattr(desc, 'is_oson', False)),
        # A 23ai column's SQL domain and annotations, relayed as described (#1082).
        domain_schema=(getattr(desc, 'domain_schema', None) or '').encode('utf-8'),
        domain_name=(getattr(desc, 'domain_name', None) or '').encode('utf-8'),
        annotations=tuple(
            (key.encode('utf-8'), (value or '').encode('utf-8'))
            for key, value in (getattr(desc, 'annotations', None) or {}).items()
        ),
    )
