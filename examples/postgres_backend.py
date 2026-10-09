# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""A PostgreSQL backend for the Mirror (psycopg 3).

Point a Mirror at a PostgreSQL database and thin-dialect Oracle clients run real
SQL against it. Result columns map from PostgreSQL type OIDs to Oracle types; a
column whose type the Mirror cannot yet represent is refused with a clean
``ORA-03001`` (unimplemented feature) rather than mis-encoded — the same
capabilities-and-errors contract SQLite uses, just with a different set of
supported types.

Requires the ``psycopg`` package. This is a demo/adapter outside ``seerdb``
core; the driver dependency lives here, not in the library.

**Requires the** `orafce <https://github.com/orafce/orafce>`_ **PostgreSQL
extension** for Oracle-compatible SQL functions (``nvl``, ``decode``,
``to_char`` / ``to_date``, ``add_months``, ``instr``, …). The backend puts its
``oracle`` schema on the search_path and creates the extension if it can, so
those idioms need no hand-rolled translation. Install it on the server (e.g.
Alpine ``apk add postgresql-orafce`` for a matching PG major, or build from
source with PGXS) — see ``examples/mirror-pg.Dockerfile``.

A handful of scalar Oracle functions — ``hextoraw`` / ``rawtohex``,
``empty_clob`` / ``empty_blob``, ``from_tz``, ``rowidtochar`` — the backend installs
itself as real PostgreSQL functions at connect (see ``_HELPER_FUNCTIONS_DDL``), so
those call sites resolve directly with no rewrite, the same way the ``ora_tstz``
composite and the ``ora_clob`` / ``ora_blob`` domains back their types. Only bare
pseudo-columns / -constants (``ROWID``, ``SYSDATE``, ``BINARY_DOUBLE_INFINITY``) and
literal / clause shapes (a negative ``INTERVAL``, the ``1.5f`` suffix, ``CONNECT BY
LEVEL``) — none of which is a call that could resolve to a function — remain regex
rewrites.

**Native PostgreSQL SQL.** An application moving to PostgreSQL can send its own
PostgreSQL over the same Oracle connection, a statement at a time, and keep the
rest Oracle (#1556). A statement carrying the Oracle hint ``/*+ PG */`` -- at its
start or right after its first keyword, where a real Oracle reads hints and ignores
one it does not know -- runs untranslated. ``ALTER SESSION SET seerdb_dialect =
'postgres'`` makes every statement native, until ``= 'oracle'`` switches back.
Binds stay Oracle's (``:name``, ``:1``), each statement keeps its savepoint, and
DDL still commits. What does not change is the client: an Oracle client reads
``::type`` as a bind named ``:type`` and refuses the statement before it is sent,
so a cast is written ``CAST(x AS type)``. It treats a statement as a query only by
its first keyword (``SELECT`` / ``WITH``), so ``VALUES`` and ``TABLE`` return no
rows, and ``RETURNING`` returns them only through ``INTO`` binds. A result of a
type with no Oracle form (``jsonb``, ``json``, ``uuid``, arrays, ranges) is refused
by name.

**Oracle-only ceiling.** A handful of Oracle features a real server offers cannot
be represented faithfully behind an 11.2 Mirror on PostgreSQL. Where the 11.2 suite
has a version guard, the backend rejects the feature so the test *skips* exactly as
it would on a server that lacks it — a SQL domain (23ai) is refused with ORA-00901,
just as the JSON / VECTOR / BOOLEAN column types (21c/23ai) are refused with
ORA-00902. 12.1's ``IS [NOT] JSON`` condition on a VARCHAR2 / CLOB / BLOB -- a
CHECK constraint's, or a query's -- is no such type, and runs as PostgreSQL's,
which tests as Oracle's STRICT does: ``{a:1}``, which Oracle's default LAX takes,
it refuses. The rest have no such guard and simply do not pass; they are the
honest edge of this adapter:

- **``ROWNUM``, as a filter only** — a top-level ``WHERE … AND ROWNUM <= n``
  (``< n``, ``= n``; n a number or a bind) becomes ``LIMIT``, the top-N idiom over
  an ordered inline view included. Oracle numbers rows before it sorts, groups,
  de-duplicates or combines them, and ``LIMIT`` applies after, so with an
  ``ORDER BY``, ``GROUP BY``, ``DISTINCT``, aggregate or set operator at that level,
  an ``OR``, ``FOR UPDATE``, or ``ROWNUM`` anywhere else (a select list,
  nested-ROWNUM pagination), it is refused by name (#1271).
- **UROWID / index-organized rowids** — the physical ``ROWID`` pseudo-column is
  emulated with PostgreSQL's ``ctid``, rendered in Oracle's 18-character extended
  form (the table's oid as the data object), so ``SELECT ROWID``, a bound rowid and
  ``cursor.lastrowid`` all agree. But ``ctid`` is *mutable* — PostgreSQL writes an
  updated row at a new address, and ``VACUUM FULL`` moves rows too — so it is a
  faithful locator only within an unmodified snapshot, not a durable
  cross-transaction handle (a real migration substitutes a surrogate identity key);
  a rowid stored with ``SET r = ROWID`` names the version that update replaced. The UROWID (``*``-prefixed logical rowid) of an ``ORGANIZATION INDEX`` table
  is emulated from the table's primary key (see ``_urowid_expression``): a stable,
  ``*``-prefixed handle that round-trips as a ``WHERE ROWID = :bind``, but not
  Oracle's actual key encoding. ``DBMS_ROWID`` is unimplemented: ``ctid`` exposes
  only a block and a slot, not the data-object# and
  relative-file# that the package's accessors (``ROWID_OBJECT``,
  ``ROWID_RELATIVE_FNO``, …) decompose a physical rowid into.
- **XMLType columns** — an ``XMLTYPE`` column is PostgreSQL's ``xml``, described
  as an ADT of ``SYS.XMLTYPE`` and fetched as the document's text; a string or
  ``SYS.XMLTYPE(...)`` inserts into it, and ``GET_TYPE_SHAPE`` answers for
  ``SYS.XMLTYPE`` as 23ai does. An object attribute or a collection element may
  be an XMLType too: it describes as a reference to ``SYS.XMLTYPE`` and carries
  the document's text. Not yet: its methods (``EXTRACT``, ``GETCLOBVAL``).
- **PL/SQL packages, partly** — a package is a schema of its name, so a caller's
  ``package.member`` is PostgreSQL's ``schema.routine``. The spec puts a stand-in
  for each routine it declares, which raises ``ORA-04067`` until a body replaces
  it; a body that does not compile leaves them raising ``ORA-04063``. Members are
  routines as ``CREATE FUNCTION`` / ``PROCEDURE`` translates them, overloads
  included, each resolving names as its creator's session did, the package first.
  ``DROP PACKAGE [BODY]`` and a call into a package that is not there answer as
  Oracle does. A package's types are PostgreSQL types of its schema: a RECORD a
  composite, an index-by table a composite of its keys and its values (``keys``
  and ``vals`` arrays, so sparse keys and string keys keep their order), a
  nested table or VARRAY a domain over an array, a SUBTYPE or a REF CURSOR a
  domain. A client's type lookup of one, ``"OWNER"."PACKAGE"."TYPE"`` or
  ``PACKAGE.TYPE``, is answered as 23ai answers it, from the declaration as
  written, and ``all_plsql_types`` / ``all_plsql_type_attrs`` /
  ``all_plsql_coll_types`` list a spec's types. A record or an index-by table
  binds and comes back as an object, an index-by table under its own keys; a
  classic PL/SQL array (``cursor.arrayvar``) bound to an index-by table
  parameter is keyed 1..N, and comes back as its values. A body indexes an
  index-by table as PL/SQL does -- ``a(k) := x``, ``a(k)``, ``COUNT``,
  ``FIRST`` / ``LAST`` / ``NEXT`` / ``PRIOR``, ``EXISTS``, ``DELETE`` -- through
  functions each such type has (``t$get``, ``t$set``, ...), the keys kept in
  order; a key not there is ORA-01403. Not yet: a record of a table's row
  (``%ROWTYPE``) as a bind, package variables and an initialization section;
  ``DATE - DATE`` in a body is an interval, not Oracle's number of days; a
  private member is
  callable from outside, where Oracle's is not;
  and PL/pgSQL checks an identifier when the routine runs, not when it is
  created, so a body Oracle would refuse can compile and fail on its first call
  instead.
- **A COMMIT in a routine or a block, deferred** — PostgreSQL ends no
  transaction in a function, nor in a procedure called with one open, and the
  Mirror always has one open. Such a COMMIT asks instead, and the backend
  commits once the client's call has succeeded: the caller's open work and the
  routine's, as Oracle's COMMIT does. A COMMIT that is the routine's last
  statement behaves exactly as Oracle's; one in the middle differs in that the
  work after it is committed too, where Oracle leaves it open, and a call that
  fails after it commits nothing, where Oracle keeps what came before it. A
  ROLLBACK in a routine is not translated and fails.
- **No invalid UTF-8 in a string** -- PostgreSQL never lets text hold a byte
  sequence that is not valid UTF-8, where Oracle stores what
  ``UTL_RAW.CAST_TO_VARCHAR2`` relabels without checking it. Such a value is
  refused (``invalid byte sequence for encoding "UTF8"``) rather than stored
  and handed back for a client to replace (#1659). An application should
  check its text before writing it.
- **DIRECTORY objects, without privileges** -- CREATE / DROP DIRECTORY keep
  a name for a path on the PostgreSQL host, which ALL_DIRECTORIES lists
  (#1668). Any user may create one, where Oracle wants CREATE ANY DIRECTORY.
- **BFILEs read by the PostgreSQL server** -- a BFILE's file is on the
  PostgreSQL host, read through pg_stat_file and pg_read_binary_file
  (#1669, #1672), so the backend's role needs pg_read_server_files (or to be
  a superuser). The client reads one; DBMS_LOB's file routines are not there.
- **Editions, by name only** -- CREATE / DROP EDITION, ALTER SESSION SET
  EDITION and a login's edition keep the session's edition, which
  CURRENT_EDITION_NAME reads (#1662). Objects are not versioned per edition:
  every session sees the same ones whatever edition it is in.
- **Privileges, for type lookup only** -- every Mirror user is the backend's
  one PostgreSQL role. A GRANT or REVOKE on an object is recorded
  (``sys.ora_grants``), and a user sees another user's type -- in ALL_TYPES,
  ALL_TYPE_ATTRS, ALL_COLL_TYPES and a lookup by name, as ``gettype()`` makes
  -- only once granted EXECUTE on it. Using a type through a column or a bind
  is not checked, and table privileges are not kept: another user's table
  named with its schema reads as one's own. A system privilege or a role
  granted is a no-op.
- **Object types, partly** — ``CREATE TYPE ... AS OBJECT`` is a PostgreSQL
  composite, listed in ``all_types`` / ``all_type_attrs`` under an OID that is the
  composite's own ``pg_type`` oid, zero-padded to Oracle's 16 bytes. An object
  binds, returns (``RETURNING o INTO :b``) and fetches, ``CLOB`` / ``BLOB``
  attributes included (an ``NCLOB`` one is stored as a ``CLOB`` and reported as
  ``NCLOB``, as an ``NCLOB`` column is), and attributes that are themselves objects or
  collections. Not yet: type methods. A VARRAY or nested table is a domain over
  an array, listed in ``all_types`` and ``all_coll_types``. A column of one
  fetches as the collection; PostgreSQL describes a domain value by its base
  type, so a computed one has no type to trace and fetches as a plain array --
  except a constructor call or a bound collection as a select item, which names
  its type. A collection of collections is a domain over an
  array of the inner domain, bound as an array literal. The block
  python-oracledb runs to learn a type,
  ``DBMS_PICKLER.GET_TYPE_SHAPE``, is answered from the catalog with the TDS and
  attribute cursor 23ai sends; an attribute is reported as the DDL translation
  stored it. An ``NVARCHAR2`` / ``NCHAR`` / ``NCLOB`` / ``RAW(n)`` / ``FLOAT``
  / ``REAL`` / ``DOUBLE PRECISION`` attribute, and an ``NVARCHAR2`` /
  ``NCHAR`` / ``RAW(n)`` collection element, keep the type the DDL declared; a
  ``DATE`` attribute is ``ora_date``, as a ``DATE`` column is, and reads as
  ``DATE``. A PL/SQL package's types are described too (see PL/SQL packages).
- **``REF`` / ``DEREF``, with a visible object id** — an Oracle object table
  (``CREATE TABLE t OF type``) gives every row a hidden object id that a REF names.
  PostgreSQL's typed tables cannot take a column beyond their type's, and a row's
  physical address (``ctid``) moves on ``UPDATE`` / ``VACUUM FULL``, so an object
  table becomes an ordinary table of the type's columns plus ``sys_nc_oid$`` (a
  uuid; Oracle's own name for the column), recorded in ``sys.ora_object_tables``.
  A REF is the table's oid and that id, carried in each type's ``<type>$ref``
  companion composite; ``DEREF`` is an overloaded ``sys.deref()``, and a REF whose
  row is gone dereferences to NULL, as a dangling Oracle REF does. PostgreSQL has
  no hidden columns, though: ``SELECT *`` from an object table, and the dictionary
  views, show ``sys_nc_oid$`` too.
- **``INVISIBLE`` columns, over one table** — PostgreSQL has no hidden column, so
  the attribute is kept in ``sys.ora_invisible_columns``. An ``INSERT`` with no
  column list and a ``*`` or ``alias.*`` over a single table name the visible
  columns; the dictionary and ``%ROWTYPE`` follow. A join, a subquery or a view
  over such a table is not expanded, so its ``*`` still includes the column. A
  column made ``VISIBLE`` again keeps its place, where Oracle moves it to the end.
- **A deliberately quoted lower-case identifier** — Oracle stores an unquoted
  name upper-case and a quoted one verbatim, so a lower-case name is
  unambiguously a quoted one; PostgreSQL folds *both* an unquoted name and a
  quoted lower-case one to the same stored lower-case, so the two cannot be told
  apart after the fact. ``sys.ora_name`` upper-cases a stored lower-case name to
  Oracle's canonical form — required for the overwhelmingly common unquoted case —
  which means a table or column created as a quoted lower-case ``"t1"`` does not
  round-trip back as ``t1`` (SQLAlchemy ``NormalizedNameTest``). A quoted
  mixed-case or reserved-word name, which PostgreSQL *does* store distinctly, is
  preserved.
- **Fractional seconds stop at six digits** — PostgreSQL keeps microseconds, so a
  ``TIMESTAMP(7)`` to ``TIMESTAMP(9)`` column is a ``TIMESTAMP(6)``: it describes
  and reads back as one, and the extra digits are not stored (#1480).
"""

from __future__ import annotations

import datetime
import decimal
import functools
import hashlib
import json
import re
import select
import struct
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Final, NamedTuple, TypeVar, cast

import psycopg
from psycopg import sql
from psycopg.abc import DumperKey
from psycopg.adapt import Loader, PyFormat
from psycopg.types.composite import CompositeInfo, register_composite

from seerdb.common.datatypes import BcDate, BFile, IntervalYM
from seerdb.common.dbobject import (
    COLLECTION_NESTED_TABLE,
    COLLECTION_PLSQL_INDEX_TABLE,
    COLLECTION_VARRAY,
    DbObject,
    DbObjectType,
    DbRef,
    ObjectImage,
    decode_collection_image,
    decode_collection_keyed,
    decode_object_image,
    map_object_lobs,
    type_name_to_tns,
)
from seerdb.common.sqltext import (
    SqlToken,
    bind_placeholders,
    is_plsql,
    placeholder_count,
    returning_bind_positions,
    sql_tokens,
    strip_non_bind_text,
    strip_returning_into,
)
from seerdb.common.tns import _AUTH_MAX_OPEN_CURSORS
from seerdb.common.tns_consts import (
    AL16UTF16_CHARSET,
    AL32UTF8_CHARSET,
    FIELD_VERSION_12_1,
    ORA_CANNOT_INSERT_NULL,
    ORA_CANNOT_KILL_CURRENT_SESSION,
    ORA_CHECK_CONSTRAINT_VIOLATED,
    ORA_DIVISOR_IS_ZERO,
    ORA_INCONSISTENT_DATATYPES,
    ORA_INVALID_BIND_VARIABLE_NAME,
    ORA_INVALID_CREATE_COMMAND,
    ORA_INVALID_DATATYPE,
    ORA_INVALID_IDENTIFIER,
    ORA_INVALID_NUMBER,
    ORA_INVALID_ROWID,
    ORA_INVALID_SESSION_ID,
    ORA_INVALID_SQL_STATEMENT,
    ORA_INVALID_USERNAME_PASSWORD,
    ORA_NAME_ALREADY_USED,
    ORA_NO_DATA_FOUND,
    ORA_NONEXISTENT_FILE,
    ORA_NOT_ENOUGH_VALUES,
    ORA_NUMERIC_OR_VALUE_ERROR,
    ORA_NUMERIC_OVERFLOW,
    ORA_PARENT_KEY_NOT_FOUND,
    ORA_PLSQL_COMPILATION_ERROR,
    ORA_RESOURCE_BUSY,
    ORA_SAVEPOINT_NEVER_ESTABLISHED,
    ORA_SESSION_ID_DOES_NOT_EXIST,
    ORA_SESSION_TERMINATED,
    ORA_TABLE_OR_VIEW_DOES_NOT_EXIST,
    ORA_TOO_MANY_ROWS,
    ORA_TOO_MANY_VALUES,
    ORA_TYPE_HAS_DEPENDENTS,
    ORA_UNIQUE_CONSTRAINT_VIOLATED,
    ORA_VALUE_LARGER_THAN_PRECISION,
    ORA_VALUE_TOO_LARGE_FOR_COLUMN,
    TNS_TYPE_ADT,
    TNS_TYPE_BDOUBLE,
    TNS_TYPE_BFILE,
    TNS_TYPE_BFLOAT,
    TNS_TYPE_BLOB,
    TNS_TYPE_BOOLEAN,
    TNS_TYPE_CHAR,
    TNS_TYPE_CLOB,
    TNS_TYPE_DATE,
    TNS_TYPE_INT,
    TNS_TYPE_INTERVALDS,
    TNS_TYPE_INTERVALYM,
    TNS_TYPE_LONG,
    TNS_TYPE_LONGRAW,
    TNS_TYPE_NUMBER,
    TNS_TYPE_RAW,
    TNS_TYPE_REF,
    TNS_TYPE_REFCURSOR,
    TNS_TYPE_TIMESTAMP,
    TNS_TYPE_TIMESTAMPLTZ,
    TNS_TYPE_TIMESTAMPTZ,
    TNS_TYPE_VARCHAR,
)
from seerdb.server import (
    BackendError,
    BindVar,
    Capability,
    ColumnMeta,
    Credentials,
    CursorResult,
    LtzValue,
    Result,
    UnsupportedFeature,
    as_declared_type,
    credential_lookup,
    stats,
)
from seerdb.server.backend import BlobValue, SessionInfo, TypeDescription
from seerdb.server.identity import IDENTITY_12_1

# The PostgreSQL composite type that backs Oracle's TIMESTAMP WITH TIME ZONE
# (#519). A native timestamptz stores UTC and hands the value back in the session
# zone, discarding the offset the client entered — but Oracle preserves that
# offset. So a WITH TIME ZONE column becomes this two-field composite: `utc` is
# the instant (a real timestamptz, so the instant is stored correctly) and `off`
# is the entered offset in seconds, which the read path uses to re-tag the value.
_TSTZ_TYPE = 'ora_tstz'
# The database time zone, what DBTIMEZONE answers: the zone TIMESTAMP WITH LOCAL
# TIME ZONE travels in (#1208). A PostgreSQL timestamptz stores the instant, so any
# fixed zone would do; UTC is Oracle's own default.
_DB_TIME_ZONE = datetime.timezone.utc
_DB_TIME_ZONE_NAME = '+00:00'
# A BFILE (#1669): the DIRECTORY object's name and the file's, which is all
# Oracle keeps too -- the file is on the PostgreSQL host, under the directory's
# path. BFILENAME makes one.
_BFILE_TYPE = 'ora_bfile'
_BFILE_TYPE_DDL = (
    'DO $$ BEGIN CREATE TYPE ora_bfile AS (directory text, filename text); '
    'EXCEPTION WHEN duplicate_object THEN NULL; END $$;'
    'CREATE OR REPLACE FUNCTION bfilename(text, text) RETURNS ora_bfile '
    'LANGUAGE sql IMMUTABLE AS $$ SELECT ROW($1, $2)::ora_bfile $$'
)
_TSTZ_TYPE_DDL = (
    'DO $$ BEGIN CREATE TYPE ora_tstz AS (utc timestamptz, off integer); '
    'EXCEPTION WHEN duplicate_object THEN NULL; END $$'
)

# CLOB / BLOB back onto PostgreSQL domains over text / bytea (#534). A plain text
# column can't tell an empty CLOB from a NULL one — a zero-length value encodes as
# NULL on the Oracle wire (empty-string-is-NULL), so an empty LOB came back as None
# instead of '' / b''. A domain is transparent for INSERT (it accepts its base
# type) and for every text / bytea operation, yet a result column still traces back
# through pg_attribute to the domain — so the read path can recognise a LOB column
# and encode it as a real LOB, whose empty value is distinct from NULL. The domain
# is otherwise invisible: values arrive as ordinary str / bytes.
_CLOB_TYPE = 'ora_clob'
_BLOB_TYPE = 'ora_blob'
_LOB_TYPE_DDL = (
    'DO $$ BEGIN CREATE DOMAIN ora_clob AS text; '
    'EXCEPTION WHEN duplicate_object THEN NULL; END $$;'
    'DO $$ BEGIN CREATE DOMAIN ora_blob AS bytea; '
    'EXCEPTION WHEN duplicate_object THEN NULL; END $$;'
)

# INTERVAL YEAR TO MONTH onto a PostgreSQL domain over `interval` (#504). Oracle
# has two interval families — YEAR TO MONTH (a calendar count of months) and DAY
# TO SECOND (an exact duration) — but PostgreSQL has a single `interval` type, so
# both share oid 1186 and neither is distinguishable by wire oid alone. A domain
# lets a YEAR TO MONTH column trace back through pg_attribute to `ora_intervalym`
# (exactly as the LOB domains do), so the read path can encode it as the Oracle
# INTERVAL YEAR TO MONTH type rather than DAY TO SECOND. The months themselves
# survive via a custom interval loader (see OraInterval below); psycopg's default
# loader flattens a year-month interval to a `timedelta`, dropping the months.
_INTERVALYM_TYPE = 'ora_intervalym'
_INTERVALYM_TYPE_DDL = (
    'DO $$ BEGIN CREATE DOMAIN ora_intervalym AS interval; '
    'EXCEPTION WHEN duplicate_object THEN NULL; END $$'
)

# A DATE table column onto a domain over timestamp(0) (#1316). Oracle's DATE
# carries a time of day to the second, which PostgreSQL's own `date` drops, so the
# column is a timestamp(0) -- whose oid is a TIMESTAMP's, and a DATE column was
# described as one. The domain lets the column trace back through pg_attribute to
# `ora_date`, as the LOB and YEAR TO MONTH domains do, and describe as a DATE. Only
# a CREATE TABLE column takes it: a computed value (SYSDATE, TO_DATE) reports its
# base type and still describes as a TIMESTAMP.
_DATE_TYPE = 'ora_date'
_DATE_TYPE_DDL = (
    'DO $$ BEGIN CREATE DOMAIN ora_date AS timestamp(0); '
    'EXCEPTION WHEN duplicate_object THEN NULL; END $$'
)

# The statements the ROWID check reads (#1624): INSERT INTO t (cols) VALUES (,
# and UPDATE t [alias] SET, in a statement whose quoted parts are masked.
_ROWID_INSERT = re.compile(
    r'(?is)\s*INSERT\s+INTO\s+("[^"]*"|[\w$#.]+)\s*\(([^()]*)\)\s*VALUES\s*\('
)
_ROWID_UPDATE = re.compile(
    r'(?is)\s*UPDATE\s+("[^"]*"|[\w$#.]+)(?:\s+(?!SET\b)\w+)?\s+SET\s+'
)


def _pg_name(name: str) -> str:
    # An Oracle (possibly schema-qualified) name as to_regclass reads it: an
    # unquoted part folded to PostgreSQL's lower case, a quoted one kept.
    return '.'.join(
        part if part.startswith('"') else part.lower()
        for part in re.split(r'\s*\.\s*', name.strip())
    )


_NUMBER_LITERAL = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')

# A ROWID column (#1624): its text, which must read as a rowid -- 18 of Oracle's
# base64 characters, the form sys.ora_rowid writes -- or the INSERT / UPDATE is
# ORA-01410, as Oracle's is. The check's name says which error it is.
_ROWID_FORMAT = 'ora_rowid_format'
_ROWID_TYPE_DDL = (
    'DO $$ BEGIN CREATE DOMAIN ora_rowid AS varchar(18) '
    f'CONSTRAINT {_ROWID_FORMAT} CHECK (VALUE ~ ' + "'^[A-Za-z0-9+/]{18}$'); "
    'EXCEPTION WHEN duplicate_object THEN NULL; END $$'
)

# Oracle's DATE arithmetic (#1611): DATE - DATE is a NUMBER of days, fraction
# and all, and DATE + n / n + DATE / DATE - n a DATE n days away. On PostgreSQL
# a DATE is the ora_date domain, or orafce's oracle.date (TO_DATE's), both over
# a timestamp, and the timestamp's own operators answered: an interval for the
# difference, and a plain timestamp for the sum, which is then no DATE to take
# a difference of. An operator on the domain itself is the exact match, so it
# is the one taken, and TIMESTAMP - TIMESTAMP stays the interval Oracle's is.
# Every numeric type gets its own, as a literal 1 would otherwise take the
# timestamp's integer one. Each operator has its own DO block, so one already
# there does not stop the rest, and one over orafce's type is skipped without
# orafce.
_DATE_TYPES = ('ora_date', 'oracle.date')
_DAY_NUMBER_TYPES = ('smallint', 'integer', 'bigint', 'numeric', 'double precision')
_DATE_ARITHMETIC_DDL = (
    'CREATE OR REPLACE FUNCTION sys.ora_date_minus(timestamp, timestamp) '
    'RETURNS numeric LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog '
    'AS $$ SELECT trim_scale(extract(epoch FROM $1 OPERATOR(pg_catalog.-) $2) / 86400) $$;'
    'CREATE OR REPLACE FUNCTION sys.ora_date_plus(timestamp, numeric) '
    'RETURNS ora_date LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog '
    "AS $$ SELECT ($1 OPERATOR(pg_catalog.+) $2 * interval '1 day')::timestamp(0) $$;"
    + ''.join(
        'DO $$ BEGIN '
        f'CREATE FUNCTION sys.ora_date_minus({left}, {right}) RETURNS numeric '
        'LANGUAGE sql IMMUTABLE STRICT AS '
        '$f$ SELECT sys.ora_date_minus($1::timestamp, $2::timestamp) $f$; '
        f'CREATE OPERATOR sys.- (LEFTARG = {left}, RIGHTARG = {right}, '
        'FUNCTION = sys.ora_date_minus); '
        'EXCEPTION WHEN duplicate_function OR undefined_object '
        'OR invalid_schema_name THEN NULL; END $$;'
        for left in _DATE_TYPES
        for right in _DATE_TYPES
    )
    + ''.join(
        'DO $$ BEGIN '
        f'CREATE FUNCTION sys.ora_date_plus({date}, {number}) RETURNS ora_date '
        'LANGUAGE sql IMMUTABLE STRICT AS '
        '$f$ SELECT sys.ora_date_plus($1::timestamp, $2::numeric) $f$; '
        f'CREATE FUNCTION sys.ora_days_plus({number}, {date}) RETURNS ora_date '
        'LANGUAGE sql IMMUTABLE STRICT AS '
        '$f$ SELECT sys.ora_date_plus($2::timestamp, $1::numeric) $f$; '
        f'CREATE FUNCTION sys.ora_date_less({date}, {number}) RETURNS ora_date '
        'LANGUAGE sql IMMUTABLE STRICT AS '
        '$f$ SELECT sys.ora_date_plus($1::timestamp, -$2::numeric) $f$; '
        f'CREATE OPERATOR sys.+ (LEFTARG = {date}, RIGHTARG = {number}, '
        'FUNCTION = sys.ora_date_plus); '
        f'CREATE OPERATOR sys.+ (LEFTARG = {number}, RIGHTARG = {date}, '
        'FUNCTION = sys.ora_days_plus); '
        f'CREATE OPERATOR sys.- (LEFTARG = {date}, RIGHTARG = {number}, '
        'FUNCTION = sys.ora_date_less); '
        'EXCEPTION WHEN duplicate_function OR undefined_object '
        'OR invalid_schema_name THEN NULL; END $$;'
        for date in _DATE_TYPES
        for number in _DAY_NUMBER_TYPES
    )
)

# Oracle scalar functions the backend installs as real PostgreSQL functions,
# rather than rewriting each call site with a regex (#513). A parens-called Oracle
# function — HEXTORAW('..'), EMPTY_CLOB(), FROM_TZ(ts, 'zone') — resolves
# case-insensitively to a same-named function on the search_path, so once these
# exist the call text needs no translation at all. This is the same "install a
# server-side object" pattern the ora_tstz composite and the ora_clob / ora_blob
# domains already use. orafce 4.17 also ships hextoraw / rawtohex / empty_clob /
# empty_blob / from_tz, but as plain text / bytea / timestamptz — the backend keeps
# its own so empty_clob / empty_blob return the ora_clob / ora_blob domains and
# from_tz returns the ora_tstz composite, which the LOB read-back and the
# offset-preserving WITH TIME ZONE round-trip both rely on.
# Only the parens-called functions move here; a bare pseudo-constant (SYSDATE,
# BINARY_DOUBLE_INFINITY) or a literal / clause shape (a negative INTERVAL, the
# `1.5f` suffix, CONNECT BY LEVEL) has no call to resolve and stays a rewrite in
# _translate_idioms. EMPTY_CLOB / EMPTY_BLOB return the LOB domains, so they need
# those to exist first (created just before this in __init__).
# The WITH TIME ZONE comparisons (#1239): (function suffix, operator) and, for
# CREATE OPERATOR, commutator, negator, and selectivity estimators; `=` also
# supports hash and merge joins.
_TSTZ_COMPARISONS = (
    ('eq', '='),
    ('ne', '<>'),
    ('lt', '<'),
    ('le', '<='),
    ('gt', '>'),
    ('ge', '>='),
)
_TSTZ_OPERATORS = (
    ('eq', '=', '=', '<>', 'eqsel', 'eqjoinsel', ', HASHES, MERGES'),
    ('ne', '<>', '<>', '=', 'neqsel', 'neqjoinsel', ''),
    ('lt', '<', '>', '>=', 'scalarltsel', 'scalarltjoinsel', ''),
    ('le', '<=', '>=', '>', 'scalarlesel', 'scalarlejoinsel', ''),
    ('gt', '>', '<', '<=', 'scalargtsel', 'scalargtjoinsel', ''),
    ('ge', '>=', '<=', '<', 'scalargesel', 'scalargejoinsel', ''),
)
_HELPER_FUNCTIONS_DDL = (
    # TO_CHAR(d, '..SYYYY..') → the year signed as Oracle prints it: '-' before
    # a BC year, a space before any other (#1063). The sign goes where SYYYY
    # stood, as quoted literal text in PostgreSQL's format.
    'CREATE OR REPLACE FUNCTION ora_to_char_signed(timestamp, text) RETURNS text '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT to_char($1, regexp_replace($2, '
    "'syyyy', CASE WHEN $1 < '0001-01-01'::timestamp THEN '\"-\"YYYY' "
    "ELSE '\" \"YYYY' END, 'gi')) $$;"
    # HEXTORAW('DEADBEEF') → the RAW/bytea value of a hex string.
    'CREATE OR REPLACE FUNCTION hextoraw(text) RETURNS bytea '
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT decode($1, 'hex') $$;"
    # A value headed for a RAW column or attribute, as Oracle converts it: a
    # character value is hex, as HEXTORAW reads it; a RAW one is itself (#1496).
    # PostgreSQL picks the overload by the value's type -- a string literal or a
    # str bind the text one, a bytes bind the bytea one.
    'CREATE OR REPLACE FUNCTION sys.ora_to_raw(text) RETURNS bytea '
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT decode($1, 'hex') $$;"
    'CREATE OR REPLACE FUNCTION sys.ora_to_raw(bytea) RETURNS bytea '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1 $$;'
    # A RAW(n) column's value must fit n bytes, which a bytea does not enforce
    # (#1415). As Oracle reports it, ORA-12899 -- SQLSTATE 22001, which the
    # error map turns into it -- in Oracle's own wording.
    'CREATE OR REPLACE FUNCTION sys.ora_raw_fits(bytea, integer, text, text) '
    'RETURNS boolean LANGUAGE plpgsql STABLE AS $$ BEGIN '
    "IF octet_length($1) > $2 THEN RAISE EXCEPTION USING ERRCODE = '22001', "
    'MESSAGE = format(\'value too large for column "%s"."%s"."%s" '
    "(actual: %s, maximum: %s)', sys.ora_owner(current_schema()), $3, $4, "
    'octet_length($1), $2); END IF; RETURN true; END $$;'
    # A FLOAT(b) column's value as Oracle stores it, rounded to the significant
    # digits b bits hold, ceil(b * log10(2)), half away from zero (#1422). The
    # trigger a table with a FLOAT column has; each column's b is the one
    # sys.ora_columns records, so an ALTER of the column changes it in step.
    'CREATE OR REPLACE FUNCTION sys.ora_float_round() RETURNS trigger '
    'LANGUAGE plpgsql AS $$ DECLARE c record; v numeric; '
    "rounded jsonb := '{}'; BEGIN FOR c IN SELECT a.attname, o.data_precision "
    'FROM sys.ora_columns o JOIN pg_attribute a ON a.attrelid = o.relid '
    "AND a.attnum = o.attnum WHERE o.relid = TG_RELID AND o.data_type = 'FLOAT' "
    'AND NOT a.attisdropped LOOP v := (to_jsonb(NEW) ->> c.attname)::numeric; '
    "IF v <> 0 AND abs(v) < 'Infinity' THEN rounded := rounded || "
    'jsonb_build_object(c.attname, trim_scale(round(v, '
    'ceil(c.data_precision * log(2::numeric))::integer - 1 '
    '- floor(log(abs(v)))::integer))); END IF; END LOOP; '
    "IF rounded <> '{}' THEN NEW := jsonb_populate_record(NEW, rounded); END IF; "
    'RETURN NEW; END $$;'
    # A call into a package with no usable body (#1605), as Oracle refuses it:
    # ORA-04067 while there is none, ORA-04063 when it did not compile.
    'CREATE OR REPLACE FUNCTION sys.ora_package_unusable(text) RETURNS void '
    'LANGUAGE plpgsql AS $$ DECLARE p record; BEGIN '
    'SELECT owner, body INTO p FROM sys.ora_packages WHERE name = $1; '
    "RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = CASE WHEN p.body = "
    "'INVALID' THEN format('ORA-04063: package body \"%s.%s\" has errors', "
    "p.owner, upper($1)) ELSE format('ORA-04067: not executed, package body "
    '"%s.%s" does not exist\', p.owner, upper($1)) END; END $$;'
    # A COMMIT in a routine or a block (#1630): PostgreSQL ends no transaction
    # there, so it asks the backend to commit once the client's call succeeds.
    'CREATE OR REPLACE FUNCTION sys.ora_commit_request() RETURNS void '
    "LANGUAGE plpgsql AS $$ BEGIN RAISE NOTICE 'seerdb: commit requested'; END $$;"
    # NO_DATA_FOUND out of a function a SQL statement called (#1612): Oracle
    # makes it the function's NULL, where a PL/SQL expression's call raises it.
    # A guarded function's handler asks sys.ora_ndf_to_null(), which reads the
    # call stack: its own frame, the handler's, then the guarded function's
    # caller -- a SQL statement (inside PL/SQL too) or a SQL function, NULL; a
    # PL/pgSQL statement, raise; nothing, the client's own statement, NULL but
    # for one the backend evaluates a PL/SQL expression with, which marks
    # itself with sys.ora_plsql_call() (its statement_timestamp, so the mark
    # dies with the statement).
    'CREATE OR REPLACE FUNCTION sys.ora_plsql_call() RETURNS text '
    "LANGUAGE sql VOLATILE AS $$ SELECT set_config('seerdb.plsql_call', "
    'statement_timestamp()::text, false) $$;'
    'CREATE OR REPLACE FUNCTION sys.ora_ndf_to_null() RETURNS boolean '
    'LANGUAGE plpgsql VOLATILE AS $$ DECLARE ctx text; frames text[]; BEGIN '
    'GET DIAGNOSTICS ctx = PG_CONTEXT; '
    "frames := regexp_split_to_array(ctx, E'\\n(?=PL/pgSQL function |SQL statement "
    '"|SQL function )\'); '
    'IF cardinality(frames) < 3 THEN '
    "RETURN coalesce(current_setting('seerdb.plsql_call', true), '') "
    '<> statement_timestamp()::text; END IF; '
    "RETURN frames[3] NOT LIKE 'PL/pgSQL function %'; END $$;"
    # x IS JSON (#1614), for a value of a domain too: PostgreSQL's own condition
    # takes text and bytea, and resolves no domain over either to it.
    'CREATE OR REPLACE FUNCTION sys.ora_is_json(text) RETURNS boolean '
    'LANGUAGE sql IMMUTABLE AS $$ SELECT $1 IS JSON $$;'
    'CREATE OR REPLACE FUNCTION sys.ora_is_json(bytea) RETURNS boolean '
    'LANGUAGE sql IMMUTABLE AS $$ SELECT $1 IS JSON $$;'
    # RAWTOHEX(x) → the hex text of a bytea. Oracle returns upper-case hex.
    'CREATE OR REPLACE FUNCTION rawtohex(bytea) RETURNS text '
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT upper(encode($1, 'hex')) $$;"
    # EMPTY_CLOB() / EMPTY_BLOB() → an empty LOB (the domain type, so a value
    # stored through one is recognised as a LOB on read-back).
    f'CREATE OR REPLACE FUNCTION empty_clob() RETURNS {_CLOB_TYPE} '
    f"LANGUAGE sql IMMUTABLE AS $$ SELECT ''::{_CLOB_TYPE} $$;"
    f'CREATE OR REPLACE FUNCTION empty_blob() RETURNS {_BLOB_TYPE} '
    f"LANGUAGE sql IMMUTABLE AS $$ SELECT ''::bytea::{_BLOB_TYPE} $$;"
    # FROM_TZ(ts, 'zone') → a TIMESTAMP WITH TIME ZONE: the naive timestamp read as
    # local wall-clock in `zone`, returned as the ora_tstz composite (utc, offset)
    # so it inserts into a WITH TIME ZONE column and round-trips its offset. `zone`
    # may be a named IANA region (US/Eastern), whose offset PostgreSQL resolves at
    # that instant from the live zone database — so the stored offset is DST-correct
    # (EST -05:00 in January, EDT -04:00 in July). The offset is local minus the
    # instant shown as naive UTC. STABLE, not IMMUTABLE: a named region's offset
    # depends on the tz database. The region *name* itself is not preserved — the
    # value carries the resolved offset, exactly like an explicit ±HH:MM literal.
    # A numeric ±HH:MM offset is applied as an interval so it follows Oracle's ISO
    # sign convention (east of UTC is positive); handing it straight to AT TIME ZONE
    # as text would use PostgreSQL's inverted POSIX sign. The zone-applied instant is
    # computed once in the subselect and reused for both composite fields.
    f'CREATE OR REPLACE FUNCTION from_tz(timestamp, text) RETURNS {_TSTZ_TYPE} '
    'LANGUAGE sql STABLE STRICT AS $$ SELECT ROW('
    'z.i, '
    "EXTRACT(EPOCH FROM ($1 - (z.i AT TIME ZONE 'UTC')))::int"
    f')::{_TSTZ_TYPE} FROM (SELECT CASE '
    "WHEN $2 ~ '^[+-]?[0-9]{1,2}:[0-9]{2}$' THEN $1 AT TIME ZONE ($2)::interval "
    'ELSE $1 AT TIME ZONE $2 END) AS z(i) $$;'
    # Oracle's RR year (#1638): a two-digit year read into the century the
    # current year puts it in -- 00-49 this century and 50-99 the last while
    # the current year ends in 00-49, 00-49 the next and 50-99 this one after.
    # A format's RR is parsed as YYYY, which keeps two digits as the year 0-99
    # (00 as 1 BC), and sys.ora_rr_year moves such a year into the window; a
    # year of three or four digits is left as it is, as Oracle leaves it. The
    # century is c - c % 100: this backend's integer / is Oracle's exact one.
    # The shift is that century in years: 60 + 1900 is 1960, and 1 BC (00)
    # + 2000 is 2000, there being no year 0.
    'CREATE OR REPLACE FUNCTION sys.ora_rr_shift(y integer) RETURNS integer '
    'LANGUAGE sql STABLE STRICT AS $$ SELECT CASE WHEN y < -1 OR y > 99 THEN 0 '
    'ELSE (SELECT CASE WHEN c % 100 < 50 THEN CASE WHEN yy < 50 THEN (c - c % 100) '
    'ELSE (c - c % 100) - 100 END ELSE CASE WHEN yy < 50 THEN (c - c % 100) + 100 '
    'ELSE (c - c % 100) END END FROM (SELECT extract(year FROM now())::integer '
    'AS c, CASE WHEN y = -1 THEN 0 ELSE y END AS yy) w) END $$;'
    'CREATE OR REPLACE FUNCTION sys.ora_rr_year(timestamp) RETURNS timestamp '
    'LANGUAGE sql STABLE STRICT AS $$ SELECT $1 + make_interval(years => '
    'sys.ora_rr_shift(extract(year FROM $1)::integer)) $$;'
    'DO $$ BEGIN '
    'CREATE OR REPLACE FUNCTION sys.ora_rr_year(oracle.date) RETURNS oracle.date '
    'LANGUAGE sql STABLE STRICT AS $f$ SELECT ($1::timestamp + make_interval(years => '
    'sys.ora_rr_shift(extract(year FROM $1)::integer)))::oracle.date $f$; '
    'EXCEPTION WHEN undefined_object OR invalid_schema_name THEN NULL; END $$;'
    # sys.ora_rowid(tableoid, ctid): a heap row's ROWID in Oracle's extended
    # form, OOOOOO FFF BBBBBB RRR in Oracle's base64 -- the table's oid as the
    # data object, file 1, and the ctid's block (plus one: a client takes block 0
    # for "no rowid", as it is Oracle's file header) and slot. It is the form a
    # client renders from the rowid an OER carries, so SELECT ROWID, a bound
    # rowid and cursor.lastrowid all speak one language.
    'CREATE OR REPLACE FUNCTION sys.ora_rowid_b64(n bigint, width int) RETURNS text '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT string_agg(substr('
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/', "
    "((n >> (6 * (width - 1 - i))) & 63)::int + 1, 1), '' ORDER BY i) "
    'FROM generate_series(0, width - 1) AS g(i) $$;'
    'CREATE OR REPLACE FUNCTION sys.ora_rowid(tab oid, t tid) RETURNS text '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT sys.ora_rowid_b64(tab::bigint, 6) '
    '|| sys.ora_rowid_b64(1, 3) '
    '|| sys.ora_rowid_b64((t::text::point)[0]::bigint + 1, 6) '
    '|| sys.ora_rowid_b64((t::text::point)[1]::bigint, 3) $$;'
    # ROWIDTOCHAR(rowid) → the VARCHAR2 form of a ROWID. The ROWID pseudo-column is
    # rewritten to text already (sys.ora_rowid), so this is the identity on it.
    'CREATE OR REPLACE FUNCTION rowidtochar(text) RETURNS text '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1 $$;'
    # Oracle's conversion functions orafce does not provide. TO_BINARY_FLOAT /
    # TO_BINARY_DOUBLE are the two float widths, from a number or its text --
    # PostgreSQL's float input already takes Oracle's 'Inf' / '-Inf' / 'NaN'.
    # TO_DSINTERVAL ('8 09:24:18.1') and TO_YMINTERVAL ('8-04') are interval
    # literals PostgreSQL parses as they stand, SQL-standard or ISO 'P...' form
    # alike. TO_BLOB / TO_NCLOB are the identity, as a BLOB is bytea and an
    # NCLOB text here. A bare literal is `unknown` to PostgreSQL, which the text
    # overloads take.
    'CREATE OR REPLACE FUNCTION to_binary_float(numeric) RETURNS real '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1::real $$;'
    'CREATE OR REPLACE FUNCTION to_binary_float(double precision) RETURNS real '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1::real $$;'
    'CREATE OR REPLACE FUNCTION to_binary_float(text) RETURNS real '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1::real $$;'
    'CREATE OR REPLACE FUNCTION to_binary_double(numeric) RETURNS double precision '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1::double precision $$;'
    'CREATE OR REPLACE FUNCTION to_binary_double(double precision) '
    'RETURNS double precision LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1 $$;'
    'CREATE OR REPLACE FUNCTION to_binary_double(text) RETURNS double precision '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1::double precision $$;'
    'CREATE OR REPLACE FUNCTION to_dsinterval(text) RETURNS interval '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1::interval $$;'
    'CREATE OR REPLACE FUNCTION to_yminterval(text) RETURNS interval '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1::interval $$;'
    'CREATE OR REPLACE FUNCTION to_blob(bytea) RETURNS bytea '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1 $$;'
    'CREATE OR REPLACE FUNCTION to_nclob(text) RETURNS text '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT $1 $$;'
    # TO_TIMESTAMP_TZ(text, fmt) → a TIMESTAMP WITH TIME ZONE (#1300). Neither
    # PostgreSQL nor orafce has it. The zone comes off the end of the text: a
    # region for TZR (US/Eastern), an offset for TZH[:TZM] (+01:30, -5), or, with
    # neither, the session's time zone, as Oracle's default. The rest is parsed by
    # PostgreSQL's to_timestamp, with Oracle's FF (fractional seconds) as its US,
    # and from_tz makes the value, so a region gets its DST-correct offset. A zone
    # written anywhere but at the end of the text is not supported.
    f'CREATE OR REPLACE FUNCTION to_timestamp_tz(text, text) RETURNS {_TSTZ_TYPE} '
    'LANGUAGE plpgsql STABLE STRICT AS $f$ DECLARE '
    't text := btrim($1); '
    "f text := regexp_replace($2, 'FF(?![1-9])', 'US', 'gi'); "
    'zone text; m text[]; BEGIN '
    "IF f ~* 'TZR' THEN "
    "zone := substring(t from '(\\S+)$'); "
    "t := regexp_replace(t, '\\s*\\S+$', ''); "
    "f := regexp_replace(f, '\\s*TZR(\\s*TZD)?', '', 'gi'); "
    "ELSIF f ~* 'TZH' THEN "
    "m := regexp_match(t, '([+-]?)(\\d{1,2})(?::?(\\d{2}))?$'); "
    "zone := coalesce(nullif(m[1], ''), '+') || lpad(m[2], 2, '0') || ':' "
    "|| coalesce(m[3], '00'); "
    "t := regexp_replace(t, '\\s*[+-]?\\d{1,2}(:?\\d{2})?$', ''); "
    "f := regexp_replace(f, '\\s*TZH(\\s*:?\\s*TZM)?', '', 'gi'); "
    "ELSE zone := current_setting('TimeZone'); END IF; "
    'RETURN from_tz(to_timestamp(t, f)::timestamp, zone); END $f$;'
    # TO_TIMESTAMP(text, fmt) → a TIMESTAMP (#1321). PostgreSQL's own returns a
    # timestamptz, which described as WITH LOCAL TIME ZONE and could not be passed
    # where a timestamp is expected -- an object constructor's argument, say. Its
    # wall clock is the one the text spells, with Oracle's FF as PostgreSQL's US.
    'CREATE OR REPLACE FUNCTION to_timestamp(text, text) RETURNS timestamp '
    'LANGUAGE sql STABLE STRICT AS $$ SELECT pg_catalog.to_timestamp($1, '
    "regexp_replace($2, 'FF(?![1-9])', 'US', 'gi'))::timestamp $$;"
    # SYSTIMESTAMP / CURRENT_TIMESTAMP are TIMESTAMP WITH TIME ZONE values, the
    # first at the database's offset, the second at the session's; a plain
    # timestamptz is TIMESTAMP WITH LOCAL TIME ZONE here (#1208).
    f'CREATE OR REPLACE FUNCTION ora_systimestamp() RETURNS {_TSTZ_TYPE} '
    f'LANGUAGE sql STABLE AS $$ SELECT ROW(now(), 0)::{_TSTZ_TYPE} $$;'
    f'CREATE OR REPLACE FUNCTION ora_current_timestamp() RETURNS {_TSTZ_TYPE} '
    'LANGUAGE sql STABLE AS $$ SELECT '
    f'ROW(now(), extract(timezone FROM now())::integer)::{_TSTZ_TYPE} $$;'
    # A WITH TIME ZONE value is its instant where a timestamptz is wanted -- in
    # arithmetic, a comparison, an LTZ column -- and its own wall-clock time where
    # a TIMESTAMP is, as Oracle converts it (#1208).
    f'CREATE OR REPLACE FUNCTION ora_tstz_instant({_TSTZ_TYPE}) RETURNS timestamptz '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT ($1).utc $$;'
    f'CREATE OR REPLACE FUNCTION ora_tstz_local({_TSTZ_TYPE}) RETURNS timestamp '
    "LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT (($1).utc AT TIME ZONE 'UTC') "
    '+ make_interval(secs => ($1).off) $$;'
    'DO $$ BEGIN '
    f'CREATE CAST ({_TSTZ_TYPE} AS timestamptz) '
    f'WITH FUNCTION ora_tstz_instant({_TSTZ_TYPE}) AS IMPLICIT; '
    'EXCEPTION WHEN duplicate_object THEN NULL; END $$;'
    'DO $$ BEGIN '
    f'CREATE CAST ({_TSTZ_TYPE} AS timestamp) '
    f'WITH FUNCTION ora_tstz_local({_TSTZ_TYPE}) AS ASSIGNMENT; '
    'EXCEPTION WHEN duplicate_object THEN NULL; END $$;'
    # The other way, a TIMESTAMP or LOCAL TIME ZONE value going into a WITH TIME
    # ZONE column (#1310): Oracle converts it implicitly in the SESSION's zone --
    # a TIMESTAMP's wall-clock time read there, an LTZ instant shown at the
    # session's offset for it. Without these casts PostgreSQL had no way in at
    # all ("column is of type ora_tstz but expression is of type timestamp").
    # Assignment casts, not implicit ones: ora_tstz -> timestamptz already is
    # implicit, and implicit casts both ways would make operators ambiguous.
    f'CREATE OR REPLACE FUNCTION ora_tstz_of_instant(timestamptz) RETURNS {_TSTZ_TYPE} '
    'LANGUAGE sql STABLE STRICT AS $$ '
    f'SELECT ROW($1, extract(timezone FROM $1)::integer)::{_TSTZ_TYPE} $$;'
    f'CREATE OR REPLACE FUNCTION ora_tstz_of_local(timestamp) RETURNS {_TSTZ_TYPE} '
    'LANGUAGE sql STABLE STRICT AS $$ SELECT ora_tstz_of_instant($1::timestamptz) $$;'
    'DO $$ BEGIN '
    f'CREATE CAST (timestamptz AS {_TSTZ_TYPE}) '
    'WITH FUNCTION ora_tstz_of_instant(timestamptz) AS ASSIGNMENT; '
    'EXCEPTION WHEN duplicate_object THEN NULL; END $$;'
    'DO $$ BEGIN '
    f'CREATE CAST (timestamp AS {_TSTZ_TYPE}) '
    'WITH FUNCTION ora_tstz_of_local(timestamp) AS ASSIGNMENT; '
    'EXCEPTION WHEN duplicate_object THEN NULL; END $$;'
    # Oracle compares WITH TIME ZONE values by their instant: 12:00 +00:00 and
    # 14:00 +02:00 are equal, order together and count once in a DISTINCT. The
    # composite's own record comparison would also compare the offsets, and
    # beside the cast to timestamptz above it made `=` ambiguous (#1239). So the
    # comparison operators, and the btree and hash classes ORDER BY, DISTINCT
    # and grouping use, all go by the instant.
    f'CREATE OR REPLACE FUNCTION ora_tstz_cmp({_TSTZ_TYPE}, {_TSTZ_TYPE}) '
    'RETURNS integer LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT CASE '
    'WHEN ($1).utc < ($2).utc THEN -1 WHEN ($1).utc > ($2).utc THEN 1 ELSE 0 END $$;'
    + ''.join(
        f'CREATE OR REPLACE FUNCTION ora_tstz_{name}({_TSTZ_TYPE}, {_TSTZ_TYPE}) '
        f'RETURNS boolean LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT ($1).utc {op} ($2).utc $$;'
        for name, op in _TSTZ_COMPARISONS
    )
    + f'CREATE OR REPLACE FUNCTION ora_tstz_hash({_TSTZ_TYPE}) RETURNS integer '
    'LANGUAGE sql IMMUTABLE STRICT AS $$ '
    'SELECT hashfloat8(extract(epoch FROM ($1).utc)::float8) $$;'
    'DO $$ BEGIN '
    + ''.join(
        f'CREATE OPERATOR {op} (LEFTARG = {_TSTZ_TYPE}, RIGHTARG = {_TSTZ_TYPE}, '
        f'FUNCTION = ora_tstz_{name}, COMMUTATOR = {commutator}, NEGATOR = {negator}, '
        f'RESTRICT = {restrict}, JOIN = {join}{extra}); '
        for name, op, commutator, negator, restrict, join, extra in _TSTZ_OPERATORS
    )
    + 'EXCEPTION WHEN duplicate_function THEN NULL; END $$;'
    'DO $$ BEGIN '
    f'CREATE OPERATOR CLASS ora_tstz_ops DEFAULT FOR TYPE {_TSTZ_TYPE} USING btree AS '
    'OPERATOR 1 <, OPERATOR 2 <=, OPERATOR 3 =, OPERATOR 4 >=, OPERATOR 5 >, '
    f'FUNCTION 1 ora_tstz_cmp({_TSTZ_TYPE}, {_TSTZ_TYPE}); '
    f'CREATE OPERATOR CLASS ora_tstz_hash_ops DEFAULT FOR TYPE {_TSTZ_TYPE} USING hash AS '
    f'OPERATOR 1 =, FUNCTION 1 ora_tstz_hash({_TSTZ_TYPE}); '
    # An operator class needs a superuser; without one the operators still
    # compare by instant, and ORDER BY / DISTINCT keep the record's meaning.
    'EXCEPTION WHEN duplicate_object OR insufficient_privilege THEN NULL; END $$;'
    # A number of days added to a WITH [LOCAL] TIME ZONE value (#1240). Oracle
    # makes it a DATE first, on the session's clock for LTZ and on the value's
    # own offset for TSTZ, dropping the fractional seconds; the result is that
    # DATE. A plain TIMESTAMP already gets this from orafce's DATE arithmetic,
    # which these leave alone: a timestamp matches that exactly.
    'CREATE OR REPLACE FUNCTION ora_ltz_add_days(timestamptz, numeric) '
    'RETURNS timestamp LANGUAGE sql STABLE STRICT AS $$ SELECT '
    "date_trunc('second', date_trunc('second', $1::timestamp) "
    "+ $2 * interval '1 day') $$;"
    f'CREATE OR REPLACE FUNCTION ora_tstz_add_days({_TSTZ_TYPE}, numeric) '
    'RETURNS timestamp LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT '
    "date_trunc('second', date_trunc('second', ora_tstz_local($1)) "
    "+ $2 * interval '1 day') $$;"
    'CREATE OR REPLACE FUNCTION ora_ltz_sub_days(timestamptz, numeric) '
    'RETURNS timestamp LANGUAGE sql STABLE STRICT AS $$ '
    'SELECT ora_ltz_add_days($1, -$2) $$;'
    f'CREATE OR REPLACE FUNCTION ora_tstz_sub_days({_TSTZ_TYPE}, numeric) '
    'RETURNS timestamp LANGUAGE sql IMMUTABLE STRICT AS $$ '
    'SELECT ora_tstz_add_days($1, -$2) $$;'
    'CREATE OR REPLACE FUNCTION ora_days_add_ltz(numeric, timestamptz) '
    'RETURNS timestamp LANGUAGE sql STABLE STRICT AS $$ '
    'SELECT ora_ltz_add_days($2, $1) $$;'
    f'CREATE OR REPLACE FUNCTION ora_days_add_tstz(numeric, {_TSTZ_TYPE}) '
    'RETURNS timestamp LANGUAGE sql IMMUTABLE STRICT AS $$ '
    'SELECT ora_tstz_add_days($2, $1) $$;'
    'DO $$ BEGIN '
    'CREATE OPERATOR + (LEFTARG = timestamptz, RIGHTARG = numeric, '
    'FUNCTION = ora_ltz_add_days, COMMUTATOR = +); '
    'CREATE OPERATOR + (LEFTARG = numeric, RIGHTARG = timestamptz, '
    'FUNCTION = ora_days_add_ltz, COMMUTATOR = +); '
    'CREATE OPERATOR - (LEFTARG = timestamptz, RIGHTARG = numeric, '
    'FUNCTION = ora_ltz_sub_days); '
    f'CREATE OPERATOR + (LEFTARG = {_TSTZ_TYPE}, RIGHTARG = numeric, '
    'FUNCTION = ora_tstz_add_days, COMMUTATOR = +); '
    f'CREATE OPERATOR + (LEFTARG = numeric, RIGHTARG = {_TSTZ_TYPE}, '
    'FUNCTION = ora_days_add_tstz, COMMUTATOR = +); '
    f'CREATE OPERATOR - (LEFTARG = {_TSTZ_TYPE}, RIGHTARG = numeric, '
    'FUNCTION = ora_tstz_sub_days); '
    'EXCEPTION WHEN duplicate_function THEN NULL; END $$;'
    # A WITH TIME ZONE value plus or minus an INTERVAL (#1301): the same instant
    # moved by it, keeping the value's own offset, as Oracle does. Without these
    # the implicit cast to timestamptz took the arithmetic and returned a plain
    # timestamptz, which lost the offset and did not fit a WITH TIME ZONE column.
    # They get their own DO block: one that already ran on a database raises
    # duplicate_function at its first operator and would never reach new ones.
    f'CREATE OR REPLACE FUNCTION ora_tstz_add_interval({_TSTZ_TYPE}, interval) '
    f'RETURNS {_TSTZ_TYPE} LANGUAGE sql IMMUTABLE STRICT AS $$ '
    f'SELECT ROW(($1).utc + $2, ($1).off)::{_TSTZ_TYPE} $$;'
    f'CREATE OR REPLACE FUNCTION ora_interval_add_tstz(interval, {_TSTZ_TYPE}) '
    f'RETURNS {_TSTZ_TYPE} LANGUAGE sql IMMUTABLE STRICT AS $$ '
    'SELECT ora_tstz_add_interval($2, $1) $$;'
    f'CREATE OR REPLACE FUNCTION ora_tstz_sub_interval({_TSTZ_TYPE}, interval) '
    f'RETURNS {_TSTZ_TYPE} LANGUAGE sql IMMUTABLE STRICT AS $$ '
    f'SELECT ROW(($1).utc - $2, ($1).off)::{_TSTZ_TYPE} $$;'
    'DO $$ BEGIN '
    f'CREATE OPERATOR + (LEFTARG = {_TSTZ_TYPE}, RIGHTARG = interval, '
    'FUNCTION = ora_tstz_add_interval, COMMUTATOR = +); '
    f'CREATE OPERATOR + (LEFTARG = interval, RIGHTARG = {_TSTZ_TYPE}, '
    'FUNCTION = ora_interval_add_tstz, COMMUTATOR = +); '
    f'CREATE OPERATOR - (LEFTARG = {_TSTZ_TYPE}, RIGHTARG = interval, '
    'FUNCTION = ora_tstz_sub_interval); '
    'EXCEPTION WHEN duplicate_function THEN NULL; END $$;'
    # Concatenation with a NULL (#1325): Oracle reads the NULL as an empty string,
    # so NULL || 'ab' is 'ab' and only NULL || NULL is NULL; PostgreSQL's || gives
    # NULL for any NULL operand. These shadow PostgreSQL's three text forms --
    # text || text and a text with any other non-array value, either side -- from
    # an earlier schema on the search path, as orafce's do for its varchar2. The
    # bodies name PostgreSQL's own operator, or they would call themselves.
    'CREATE OR REPLACE FUNCTION ora_concat(text, text) RETURNS text '
    'LANGUAGE sql IMMUTABLE AS $$ SELECT CASE WHEN $1 IS NULL THEN $2 '
    'WHEN $2 IS NULL THEN $1 ELSE $1 OPERATOR(pg_catalog.||) $2 END $$;'
    'CREATE OR REPLACE FUNCTION ora_concat(text, anynonarray) RETURNS text '
    'LANGUAGE sql STABLE AS $$ SELECT ora_concat($1, $2::text) $$;'
    'CREATE OR REPLACE FUNCTION ora_concat(anynonarray, text) RETURNS text '
    'LANGUAGE sql STABLE AS $$ SELECT ora_concat($1::text, $2) $$;'
    'DO $$ BEGIN '
    'CREATE OPERATOR || (LEFTARG = text, RIGHTARG = text, FUNCTION = ora_concat); '
    'CREATE OPERATOR || (LEFTARG = text, RIGHTARG = anynonarray, '
    'FUNCTION = ora_concat); '
    'CREATE OPERATOR || (LEFTARG = anynonarray, RIGHTARG = text, '
    'FUNCTION = ora_concat); '
    'EXCEPTION WHEN duplicate_function THEN NULL; END $$;'
    # A quotient as Oracle's NUMBER holds it (#1598): 20 base-100 digits, the
    # pairs aligned on the decimal point, rounded half away from zero -- 40
    # significant decimal digits for 1/3, 39 for 10/7 -- where PostgreSQL keeps
    # 16 to 20. PostgreSQL's own / divides first: it raises on a zero divisor
    # and estimates the quotient's magnitude. div() then gives the quotient
    # truncated past the last digit kept, so the one rounding that follows is
    # exact; the magnitude it rounds by is read from that quotient's digits.
    # Each / is PostgreSQL's by name, as the / below would otherwise call this,
    # and the magnitude is a float8 log: numeric's costs 10 us a call.
    'CREATE OR REPLACE FUNCTION sys.ora_numeric_div(numeric, numeric) '
    'RETURNS numeric LANGUAGE plpgsql IMMUTABLE STRICT AS $$ '
    'DECLARE q numeric := $1 OPERATOR(pg_catalog./) $2; e integer; k integer; '
    "t numeric; BEGIN IF q = 0 OR NOT abs(q) < 'Infinity' THEN RETURN q; END IF; "
    'e := floor(log(abs(q)::float8)); '
    'k := 39 - 2 * floor((e - 1) * 0.5)::integer; '
    "t := div($1 * ('1e' || k)::numeric, $2); "
    'e := length(abs(t)::text) - 1 - k; '
    "RETURN trim_scale(round(t * ('1e' || -k)::numeric, "
    '38 - 2 * floor(e * 0.5)::integer)); END $$;'
    'DO $$ BEGIN CREATE OPERATOR / (LEFTARG = numeric, RIGHTARG = numeric, '
    'FUNCTION = sys.ora_numeric_div); '
    'EXCEPTION WHEN duplicate_function THEN NULL; END $$;'
    # Division (#1361): Oracle's / is exact, so 3 / 2 is 1.5; PostgreSQL divides
    # two integers as integers and gives 1. An integer reaches it as a literal, an
    # int bind, or a function such as count(*), so every pair of PostgreSQL's
    # integer types gets a / that divides as numeric, Oracle's way (#1598),
    # shadowing PostgreSQL's own from an earlier schema on the search path, as ||
    # does above. Each operator has its own DO block, so one already there doesn't
    # stop the rest.
    + ''.join(
        f'CREATE OR REPLACE FUNCTION ora_div({left}, {right}) RETURNS numeric '
        'LANGUAGE sql IMMUTABLE STRICT AS '
        '$$ SELECT sys.ora_numeric_div($1::numeric, $2::numeric) $$;'
        'DO $$ BEGIN '
        f'CREATE OPERATOR / (LEFTARG = {left}, RIGHTARG = {right}, FUNCTION = ora_div); '
        'EXCEPTION WHEN duplicate_function THEN NULL; END $$;'
        for left in ('smallint', 'integer', 'bigint')
        for right in ('smallint', 'integer', 'bigint')
    )
    # POWER of two integers (#1362): PostgreSQL has power() for numeric and for
    # double precision only, and resolves two integers to the double precision
    # one, so POWER(143, 9) lost its low digits. Oracle's POWER of NUMBERs is
    # exact. An overload for each pair of integer types is an exact match, found
    # before either of those, and takes the numeric one.
    + ''.join(
        f'CREATE OR REPLACE FUNCTION power({base}, {exponent}) RETURNS numeric '
        'LANGUAGE sql IMMUTABLE STRICT AS '
        '$$ SELECT pg_catalog.power($1::numeric, $2::numeric) $$;'
        for base in ('smallint', 'integer', 'bigint')
        for exponent in ('smallint', 'integer', 'bigint')
    )
)


# Helpers built on orafce's own functions, so they are installed apart from
# _HELPER_FUNCTIONS_DDL: on a PostgreSQL without orafce they fail, and must not
# take the rest down with them.
#
# TO_DATE of a number (#1299): Oracle converts a numeric first argument to text
# implicitly, so to_date(20021209, 'YYYYMMDD') is a DATE. PostgreSQL has no
# to_date(numeric, text), and does not cast a number to text implicitly, so the
# call found no function at all. This one hands the number's text to orafce's
# to_date, which is what Oracle does.
_ORAFCE_HELPERS_DDL = (
    'CREATE OR REPLACE FUNCTION to_date(numeric, text) RETURNS oracle.date '
    'LANGUAGE sql STABLE STRICT AS $$ SELECT oracle.to_date($1::text, $2) $$;'
    # TRUNC / ROUND of a WITH TIME ZONE value (#1313) work on the value's OWN
    # wall-clock time and return a DATE: TRUNC of 2022-06-08 00:00:10 +05:30 is
    # 2022-06-08. Left to orafce, the ora_tstz composite reached its trunc /
    # round only through the implicit cast to timestamptz, which truncated the
    # instant in the SESSION's zone -- 2022-06-07 in a UTC session.
    + ''.join(
        f'CREATE OR REPLACE FUNCTION {fn}({_TSTZ_TYPE}{arg}) RETURNS timestamp '
        f'LANGUAGE sql IMMUTABLE STRICT AS $$ '
        f'SELECT oracle.{fn}(ora_tstz_local($1){use}) $$;'
        for fn in ('trunc', 'round')
        for arg, use in (('', ''), (', text', ', $2'))
    )
    # SYS.DUAL (#1516): the qualified name sqlplus uses for PRINT and its login
    # query. orafce's DUAL is oracle.dual, which a bare `dual` finds on the
    # search path; `sys` is the dictionary's own schema and had none. Created
    # only when missing: replacing a view on every connect takes a lock that a
    # reader in an open transaction holds up (#1152).
    + "DO $$ BEGIN IF to_regclass('sys.dual') IS NULL THEN "
    'CREATE VIEW sys.dual AS SELECT * FROM oracle.dual; END IF; END $$;'
)


# UTL_RAW — Oracle's RAW/bytea manipulation package (#765). orafce does not ship
# it, so the backend installs it as PostgreSQL functions in a `utl_raw` schema, and
# a schema-qualified Oracle call (UTL_RAW.CAST_TO_RAW(...)) resolves to it
# case-insensitively. A function taking a RAW takes a character value too, as
# hex, as Oracle converts one (#1496): a text overload beside each, which
# PostgreSQL picks for a string literal or a str bind. Bytes are the DB charset (UTF-8) for the varchar2/raw casts;
# BIT_AND/OR/XOR follow Oracle's rule that the unprocessed tail of the longer
# operand is appended after the shorter one runs out. The bodies qualify
# pg_catalog.length so utl_raw.length does not recurse into itself when a
# caller puts utl_raw on the search_path.
_UTL_RAW_DDL = """
CREATE SCHEMA IF NOT EXISTS utl_raw;
CREATE OR REPLACE FUNCTION utl_raw.cast_to_raw(text) RETURNS bytea
  LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT convert_to($1, 'UTF8') $$;
CREATE OR REPLACE FUNCTION utl_raw.cast_to_varchar2(bytea) RETURNS text
  LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT convert_from($1, 'UTF8') $$;
CREATE OR REPLACE FUNCTION utl_raw.length(bytea) RETURNS integer
  LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT pg_catalog.length($1) $$;
CREATE OR REPLACE FUNCTION utl_raw.substr(bytea, integer, integer DEFAULT NULL)
  RETURNS bytea LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN $2 = 0 THEN NULL
      WHEN $2 < 0 THEN substring($1 from pg_catalog.length($1) + $2 + 1 for coalesce($3, pg_catalog.length($1)))
      ELSE substring($1 from $2 for coalesce($3, pg_catalog.length($1))) END $$;
CREATE OR REPLACE FUNCTION utl_raw.concat(VARIADIC bytea[]) RETURNS bytea
  LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(string_agg(x, ''::bytea), ''::bytea) FROM unnest($1) AS x $$;
CREATE OR REPLACE FUNCTION utl_raw._bitop(a bytea, b bytea, op char) RETURNS bytea
  LANGUAGE plpgsql IMMUTABLE AS $$
  DECLARE n int := least(pg_catalog.length(a), pg_catalog.length(b)); r bytea := ''::bytea; i int; v int;
  BEGIN
    FOR i IN 0 .. n - 1 LOOP
      v := CASE op WHEN '&' THEN get_byte(a, i) & get_byte(b, i)
                   WHEN '|' THEN get_byte(a, i) | get_byte(b, i)
                   ELSE get_byte(a, i) # get_byte(b, i) END;
      r := r || decode(lpad(to_hex(v), 2, '0'), 'hex');
    END LOOP;
    IF pg_catalog.length(a) > n THEN r := r || substring(a from n + 1);
    ELSIF pg_catalog.length(b) > n THEN r := r || substring(b from n + 1); END IF;
    RETURN r;
  END $$;
CREATE OR REPLACE FUNCTION utl_raw.bit_and(bytea, bytea) RETURNS bytea
  LANGUAGE sql IMMUTABLE AS $$ SELECT utl_raw._bitop($1, $2, '&') $$;
CREATE OR REPLACE FUNCTION utl_raw.bit_or(bytea, bytea) RETURNS bytea
  LANGUAGE sql IMMUTABLE AS $$ SELECT utl_raw._bitop($1, $2, '|') $$;
CREATE OR REPLACE FUNCTION utl_raw.bit_xor(bytea, bytea) RETURNS bytea
  LANGUAGE sql IMMUTABLE AS $$ SELECT utl_raw._bitop($1, $2, '#') $$;
CREATE OR REPLACE FUNCTION utl_raw.cast_to_varchar2(text) RETURNS text
  LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT utl_raw.cast_to_varchar2(decode($1, 'hex')) $$;
CREATE OR REPLACE FUNCTION utl_raw.length(text) RETURNS integer
  LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT utl_raw.length(decode($1, 'hex')) $$;
CREATE OR REPLACE FUNCTION utl_raw.substr(text, integer, integer DEFAULT NULL)
  RETURNS bytea LANGUAGE sql IMMUTABLE AS $$
    SELECT utl_raw.substr(decode($1, 'hex'), $2, $3) $$;
CREATE OR REPLACE FUNCTION utl_raw.concat(VARIADIC text[]) RETURNS bytea
  LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(string_agg(decode(x, 'hex'), ''::bytea), ''::bytea)
    FROM unnest($1) AS x $$;
CREATE OR REPLACE FUNCTION utl_raw.bit_and(text, text) RETURNS bytea
  LANGUAGE sql IMMUTABLE AS $$ SELECT utl_raw._bitop(decode($1, 'hex'), decode($2, 'hex'), '&') $$;
CREATE OR REPLACE FUNCTION utl_raw.bit_or(text, text) RETURNS bytea
  LANGUAGE sql IMMUTABLE AS $$ SELECT utl_raw._bitop(decode($1, 'hex'), decode($2, 'hex'), '|') $$;
CREATE OR REPLACE FUNCTION utl_raw.bit_xor(text, text) RETURNS bytea
  LANGUAGE sql IMMUTABLE AS $$ SELECT utl_raw._bitop(decode($1, 'hex'), decode($2, 'hex'), '#') $$;
"""


# DBMS_UTILITY — the commonly-called entry points orafce does not already ship
# (it has GET_TIME and FORMAT_CALL_STACK) (#764). Installed into the same
# dbms_utility schema. FORMAT_ERROR_STACK / FORMAT_ERROR_BACKTRACE return the
# empty string a plain SQL context (no active exception) yields in Oracle too.
# DB_VERSION is a procedure with OUT arguments, reached through the callproc
# path; its release string tracks the demo's advertised server_identity (12.1).
# COMMA_TO_TABLE / TABLE_TO_COMMA are left unimplemented: they exchange an Oracle
# collection (DBMS_UTILITY.UNCL_ARRAY / LNAME_ARRAY), a PL/SQL table type the
# Mirror does not model.
# The release in the banner the backend presents (#1603), e.g. 12.1.0.2.0.
def _banner_release(banner: bytes) -> str:
    found = re.search(rb'Release (\S+)', banner)
    if found is None:
        raise ValueError(f'no release in the banner {banner!r}')
    return found.group(1).decode()


_PRESENTED_RELEASE = _banner_release(IDENTITY_12_1.banner)
_V_VERSION_DDL = (
    'CREATE OR REPLACE VIEW sys."v$version" AS SELECT banner, 0::numeric AS con_id '
    'FROM (VALUES '
    f"(1, '{IDENTITY_12_1.banner.decode()}'), "
    f"(2, 'PL/SQL Release {_PRESENTED_RELEASE} - Production'), "
    f"(3, 'CORE' || chr(9) || '{_PRESENTED_RELEASE}' || chr(9) || 'Production'), "
    f"(4, 'TNS for Linux: Version {_PRESENTED_RELEASE} - Production'), "
    f"(5, 'NLSRTL Version {_PRESENTED_RELEASE} - Production')) "
    'AS v(n, banner) ORDER BY n;'
)

_DBMS_UTILITY_DDL = """
CREATE SCHEMA IF NOT EXISTS dbms_utility;
CREATE OR REPLACE FUNCTION dbms_utility.format_error_stack() RETURNS text
  LANGUAGE sql IMMUTABLE AS $$ SELECT ''::text $$;
CREATE OR REPLACE FUNCTION dbms_utility.format_error_backtrace() RETURNS text
  LANGUAGE sql IMMUTABLE AS $$ SELECT ''::text $$;
CREATE OR REPLACE PROCEDURE dbms_utility.db_version(
    INOUT version text, INOUT compatibility text)
  LANGUAGE plpgsql AS $$ BEGIN
    version := '12.1.0.2.0'; compatibility := '12.1.0.0.0';
  END $$;
"""


# DBMS_LOCK.SLEEP and its 18c name DBMS_SESSION.SLEEP (#1511): the standard way
# to make a call take time, which is what a client's cancel and call-timeout
# tests need. orafce ships neither. Seconds may be fractional, as in Oracle;
# double precision takes an integer, numeric or float bind alike, where a
# numeric parameter refused a float one.
_DBMS_SLEEP_DDL = """
CREATE SCHEMA IF NOT EXISTS dbms_lock;
CREATE OR REPLACE PROCEDURE dbms_lock.sleep(seconds double precision)
  LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(seconds); END $$;
CREATE SCHEMA IF NOT EXISTS dbms_session;
CREATE OR REPLACE PROCEDURE dbms_session.sleep(seconds double precision)
  LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(seconds); END $$;
"""

# DBMS_SQL.RETURN_RESULT (#1617): a block hands an open cursor back to its
# client as an implicit result. orafce's DBMS_SQL has no such procedure. This
# one names the cursor's portal in a notice, and the backend fetches the rows
# once the block has run; TO_CLIENT is taken, and every result goes to the
# client, as there is no caller in between to give it to.
_DBMS_SQL_RETURN_RESULT_DDL = """
CREATE SCHEMA IF NOT EXISTS dbms_sql;
CREATE OR REPLACE PROCEDURE dbms_sql.return_result(rc refcursor, to_client boolean DEFAULT true)
  LANGUAGE plpgsql AS $$ BEGIN RAISE NOTICE '%', 'seerdb: implicit result ' || rc; END $$;
"""


# SYS.DBMSOUTPUT_LINESARRAY (#1411): DBMS_OUTPUT.GET_LINES's collection, a
# VARRAY(2147483647) OF VARCHAR2(32767) on Oracle. sqlplus describes it by name
# under `set serveroutput on` and binds the lines as one. Here it is a collection
# like any VARRAY -- a domain over an array with its bound -- so the type
# describe and the binds treat it as they treat a user's. Created only when
# missing: a domain cannot be replaced, and dropping it would break whatever
# uses it.
_DBMS_OUTPUT_LINES_DDL = (
    "DO $$ BEGIN IF to_regtype('sys.dbmsoutput_linesarray') IS NULL THEN "
    'CREATE DOMAIN sys.dbmsoutput_linesarray AS varchar(32767)[] '
    'CHECK (VALUE IS NULL OR array_length(VALUE, 1) <= 2147483647); '
    'END IF; END $$;'
)


# Oracle data-dictionary emulation (#759): the SYS_CONTEXT userenv function and a
# minimal set of Oracle-shaped catalog views over pg_catalog / information_schema,
# so a reflecting client (SQLAlchemy's Oracle dialect, ORMs) finds the metadata it
# queries. Oracle folds unquoted identifiers to upper case and treats the user as
# the schema; the views surface UPPER-cased names and take the current schema as
# the "owner", so a table created through the Mirror shows up under the connected
# user's schema. Installed idempotently at connect alongside the helper functions.
# The comment a user's standalone routine carries (#1606), with its status, so
# ALL_OBJECTS and ALL_PROCEDURES can tell it from the backend's own helpers,
# which share its schemas. DROP takes the comment with the routine.
_ROUTINE_MARK: Final = 'seerdb:routine:'

_ORACLE_DICTIONARY_DDL = (
    # SYS_CONTEXT('userenv', <param>) — the session context the dialect reads to
    # learn its current schema/user before it reflects anything.
    # ora_serial(pid): the session's SERIAL#, stable for its life and different
    # for the next session to reuse the pid -- the backend's start time folded
    # to Oracle's range (#1212).
    'CREATE OR REPLACE FUNCTION sys.ora_serial(integer) RETURNS integer '
    'LANGUAGE sql STABLE AS $$ SELECT (extract(epoch FROM backend_start)::bigint '
    '% 65535)::integer + 1 FROM pg_stat_activity WHERE pid = $1 $$;'
    # What each session's client declared at login (#1212): the identity a
    # v$session row shows, which PostgreSQL's own pg_stat_activity does not
    # carry. Keyed by the backend pid; rows of ended backends are pruned as new
    # sessions record themselves.
    'CREATE TABLE IF NOT EXISTS sys.ora_sessions (pid integer PRIMARY KEY, '
    'username text, program text, machine text, terminal text, osuser text, '
    'driver text);'
    # DIRECTORY objects (#1668): a name for a path on the PostgreSQL host, which
    # a BFILE names its file under. Oracle's are SYS's whoever made them.
    'CREATE TABLE IF NOT EXISTS sys.ora_directories (name text PRIMARY KEY, '
    'path text NOT NULL);'
    "CREATE OR REPLACE VIEW sys.all_directories AS SELECT 'SYS'::text AS owner, "
    'name AS directory_name, path AS directory_path, 0::numeric AS origin_con_id '
    'FROM sys.ora_directories;'
    'CREATE OR REPLACE VIEW sys.dba_directories AS SELECT * FROM sys.all_directories;'
    # Editions (#1662): the names CREATE EDITION made, ORA$BASE always there,
    # and the session's own in seerdb.edition. Only the name is kept: objects
    # are not versioned per edition.
    'CREATE TABLE IF NOT EXISTS sys.ora_editions (name text PRIMARY KEY, parent text);'
    "INSERT INTO sys.ora_editions VALUES ('ORA$BASE', NULL) ON CONFLICT DO NOTHING;"
    'CREATE OR REPLACE FUNCTION sys.ora_set_edition(name text) RETURNS void '
    'LANGUAGE plpgsql AS $$ BEGIN IF NOT EXISTS (SELECT 1 FROM sys.ora_editions e '
    "WHERE e.name = $1) THEN RAISE EXCEPTION USING ERRCODE = 'P0001', "
    "MESSAGE = 'ORA-38802: edition does not exist'; END IF; "
    "PERFORM set_config('seerdb.edition', $1, false); END $$;"
    'CREATE OR REPLACE FUNCTION sys.sys_context(text, text) RETURNS text '
    # A namespace other than USERENV is an application context: what the
    # client declared at login (`connect(appcontext=...)`), kept in a session
    # setting as JSON, NAMESPACE.ATTRIBUTE upper-cased as Oracle names them
    # (#1581).
    "LANGUAGE sql STABLE AS $$ SELECT CASE WHEN upper($1) <> 'USERENV' THEN "
    "nullif(current_setting('seerdb.app_context', true), '')::jsonb "
    "->> (upper($1) || '.' || upper($2)) ELSE CASE lower($2) "
    # The session's SID is its backend's pid, the one the login reply names.
    "WHEN 'sid' THEN pg_backend_pid()::text "
    "WHEN 'current_schema' THEN upper(current_schema()) "
    "WHEN 'current_user' THEN upper(current_user::text) "
    # The session's user is the login's, or under a proxy login the user it
    # acts for, and PROXY_USER the one who logged in (#1620); PostgreSQL's own
    # session_user is the backend's role, which every session shares.
    "WHEN 'session_user' THEN coalesce(nullif(current_setting('seerdb.session_user', "
    "true), ''), upper(session_user::text)) "
    "WHEN 'proxy_user' THEN nullif(current_setting('seerdb.proxy_user', true), '') "
    # The session's edition (#1662), ORA$BASE until one is set.
    "WHEN 'current_edition_name' THEN coalesce(nullif(current_setting('seerdb.edition', "
    "true), ''), 'ORA$BASE') "
    "WHEN 'session_edition_name' THEN coalesce(nullif(current_setting('seerdb.edition', "
    "true), ''), 'ORA$BASE') "
    "WHEN 'current_schemaid' THEN current_setting('search_path') "
    "WHEN 'db_name' THEN upper(current_database()) "
    # The service the client connected to, which the Mirror hands over (#1409).
    "WHEN 'service_name' THEN nullif(current_setting('seerdb.service_name', true), '') "
    "WHEN 'db_unique_name' THEN upper(current_database()) "
    "WHEN 'instance_name' THEN upper(current_database()) "
    "WHEN 'server_host' THEN NULL "
    "WHEN 'host' THEN NULL "
    "WHEN 'ip_address' THEN NULL "
    "WHEN 'lang' THEN 'US' "
    "WHEN 'language' THEN 'AMERICAN_AMERICA.AL32UTF8' "
    # End-to-end application tracing (#183): the client sets these over the 12c
    # tracing piggyback and reads them straight back out of SYS_CONTEXT, so they
    # have to survive in the session. They live in PostgreSQL customised options
    # under `seerdb.`, which are exactly session-scoped GUCs; `true` is
    # missing_ok, so an attribute never set reads as NULL rather than raising,
    # and an attribute CLEARED is stored as '' and mapped back to NULL -- Oracle
    # reports a cleared attribute as NULL, not as an empty string.
    "WHEN 'module' THEN nullif(current_setting('seerdb.module', true), '') "
    "WHEN 'action' THEN nullif(current_setting('seerdb.action', true), '') "
    "WHEN 'client_identifier' THEN "
    "nullif(current_setting('seerdb.client_identifier', true), '') "
    "WHEN 'client_info' THEN "
    "nullif(current_setting('seerdb.client_info', true), '') "
    'ELSE NULL END END $$;'
    # XS_SYS_CONTEXT(namespace, attribute): the Real Application Security
    # context, which sqlplus reads in its login query, `DECODE(USER, 'XS$NULL',
    # XS_SYS_CONTEXT('XS$SESSION', 'USERNAME'), USER) FROM SYS.DUAL`. A session
    # with no RAS session attached reads NULL from it on Oracle; without the
    # function the query fails as soon as SYS.DUAL resolves (#1516).
    'CREATE OR REPLACE FUNCTION sys.xs_sys_context(text, text) RETURNS text '
    'LANGUAGE sql STABLE AS $$ SELECT NULL::text $$;'
    # DBMS_LOB.GETLENGTH(lob): the one DBMS_LOB entry point the suite calls from
    # ordinary SQL rather than from inside a PL/SQL block (#1127). A CLOB is
    # `text` here and a BLOB is `bytea`, so the length is `length` or
    # `octet_length` -- Oracle counts CHARACTERS for a CLOB and BYTES for a BLOB,
    # which is what those two do respectively. NULL in, NULL out, as Oracle does
    # for a NULL locator.
    #
    # The schema is created rather than the function put on the search_path:
    # every call site writes it qualified, `DBMS_LOB.GETLENGTH(c)`, and
    # PostgreSQL folds the unquoted name to `dbms_lob.getlength`. orafce ships
    # dbms_alert / assert / output / pipe / random / sql / utility but no
    # dbms_lob at all, so there is nothing to lean on.
    # DBMS_DEBUG_JDWP.CURRENT_SESSION_ID / _SERIAL (#1355): the no-privilege way
    # a session reads its own id and serial, from the same expressions the login
    # reports them by (session_info). Oracle calls them without parentheses;
    # _translate_idioms adds them.
    'CREATE SCHEMA IF NOT EXISTS dbms_debug_jdwp;'
    'CREATE OR REPLACE FUNCTION dbms_debug_jdwp.current_session_id() '
    'RETURNS integer LANGUAGE sql STABLE AS $$ SELECT pg_backend_pid() $$;'
    'CREATE OR REPLACE FUNCTION dbms_debug_jdwp.current_session_serial() '
    'RETURNS integer LANGUAGE sql STABLE AS '
    '$$ SELECT sys.ora_serial(pg_backend_pid()) $$;'
    'CREATE SCHEMA IF NOT EXISTS dbms_lob;'
    'CREATE OR REPLACE FUNCTION dbms_lob.getlength(text) RETURNS integer '
    'LANGUAGE sql IMMUTABLE AS $$ SELECT length($1) $$;'
    'CREATE OR REPLACE FUNCTION dbms_lob.getlength(bytea) RETURNS integer '
    'LANGUAGE sql IMMUTABLE AS $$ SELECT octet_length($1) $$;'
    # TO_CLOB(x): Oracle promotes a value to a CLOB; a CLOB IS text here, so the
    # conversion is a cast and the function exists only so the name resolves
    # (#1127). Declared for text and for the untyped literal a bare
    # `TO_CLOB('x')` produces -- PostgreSQL resolves `unknown` to no function at
    # all otherwise, which is what `ORA-00904: function to_clob(unknown) does
    # not exist` was. orafce does not provide it.
    # SYS.XMLTYPE(text): the XMLType constructor, an xml value here (#1536).
    'CREATE OR REPLACE FUNCTION sys.xmltype(text) RETURNS xml LANGUAGE sql '
    'IMMUTABLE AS $$ SELECT $1::xml $$;'
    'CREATE OR REPLACE FUNCTION sys.to_clob(text) RETURNS text LANGUAGE sql '
    'IMMUTABLE AS $$ SELECT $1 $$;'
    'CREATE OR REPLACE FUNCTION sys.to_clob(anyelement) RETURNS text LANGUAGE sql '
    'IMMUTABLE AS $$ SELECT $1::text $$;'
    # ora_cursor(doc, names): a CURSOR(subquery) expression's value (#1461). `doc`
    # is [column types, rows] -- the subquery evaluated in place, so it may name
    # the outer row's columns -- and the cursor opens over those rows, typed and
    # named as the subquery's columns. A PL/pgSQL function cannot take the rows
    # as a record[], hence the jsonb.
    'CREATE OR REPLACE FUNCTION sys.ora_cursor(doc jsonb, names text[]) '
    'RETURNS refcursor LANGUAGE plpgsql AS $$ DECLARE r refcursor; defs text; '
    "cols text; BEGIN SELECT string_agg(format('c%s %s', i, CASE t "
    # pg_typeof() names a char(n) `character`, which is char(1) as a type: a
    # CHAR column of the rows is the unbounded bpchar instead (#1494).
    "WHEN 'character' THEN 'bpchar' ELSE t END), ', ' ORDER BY i), "
    "string_agg(format('c%s AS %I', i, names[i]), ', ' ORDER BY i) INTO defs, cols "
    'FROM jsonb_array_elements_text(doc->0) WITH ORDINALITY AS x(t, i); OPEN r FOR '
    "EXECUTE format('SELECT %s FROM jsonb_to_recordset($1) AS t(%s)', cols, defs) "
    'USING doc->1; RETURN r; END $$;'
    # ora_owner(schema): the Oracle owner for a PostgreSQL schema — the current
    # schema for a session-local (pg_temp) object, so GLOBAL TEMPORARY tables and
    # their indexes/constraints report under the user's schema like Oracle (#759).
    'CREATE OR REPLACE FUNCTION sys.ora_owner(text) RETURNS text LANGUAGE sql '
    "STABLE AS $$ SELECT CASE WHEN $1 LIKE 'pg_temp%' THEN upper(current_schema()) "
    'ELSE upper($1) END $$;'
    # ora_name(name): fold a PostgreSQL identifier to Oracle's stored form.
    # Oracle stores an unquoted identifier upper-case and a quoted one verbatim;
    # PostgreSQL stores an unquoted identifier lower-case and a quoted one
    # verbatim. A name that is a legal unquoted identifier (all lower-case, no
    # dots or other specials) came from an unquoted name, so upper-case it to
    # Oracle's canonical form; anything else (mixed case, dots) was quoted, so
    # keep it exactly. A reserved word (asc, key, ...) must be quoted in Oracle
    # too, so it is also left as-is. This matches the dialect's normalize/
    # denormalize round trip, so quoted mixed-case, dotted and reserved-word
    # identifiers reflect back unchanged.
    'CREATE OR REPLACE FUNCTION sys.ora_name(text) RETURNS text LANGUAGE sql '
    "IMMUTABLE AS $$ SELECT CASE WHEN $1 ~ '^[a-z][a-z0-9_$#]*$' "
    "AND upper($1) <> ALL (ARRAY['ALL','ALTER','AND','ANY','AS','ASC','BETWEEN','BY','CHAR','CHECK','CLUSTER','COMMENT','COMPRESS','CONNECT','CREATE','CURRENT','DATE','DECIMAL','DEFAULT','DELETE','DESC','DISTINCT','DROP','ELSE','EXCLUSIVE','EXISTS','FLOAT','FOR','FROM','GRANT','GROUP','HAVING','IDENTIFIED','IN','INDEX','INSERT','INTEGER','INTERSECT','INTO','IS','LEVEL','LIKE','LOCK','LONG','MINUS','MODE','NOCOMPRESS','NOT','NOWAIT','NULL','NUMBER','OF','ON','OPTION','OR','ORDER','PCTFREE','PRIOR','PUBLIC','RAW','RENAME','RESOURCE','REVOKE','SELECT','SET','SHARE','SIZE','SMALLINT','START','SYNONYM','TABLE','THEN','TO','TRIGGER','UID','UNION','UNIQUE','UPDATE','USER','VALUES','VARCHAR','VARCHAR2','VIEW','WHERE','WITH']) THEN upper($1) "
    'ELSE $1 END $$;'
    # ora_type_name(data_type): the Oracle type name for an information_schema
    # data_type, shared by the table-column and the object-attribute views so
    # the two report one type the same way.
    'CREATE OR REPLACE FUNCTION sys.ora_type_name(text) RETURNS text LANGUAGE sql '
    "IMMUTABLE AS $$ SELECT CASE $1 WHEN 'numeric' THEN 'NUMBER' "
    "WHEN 'integer' THEN 'NUMBER' "
    "WHEN 'bigint' THEN 'NUMBER' WHEN 'smallint' THEN 'NUMBER' "
    "WHEN 'double precision' THEN 'BINARY_DOUBLE' WHEN 'real' THEN 'BINARY_FLOAT' "
    "WHEN 'character varying' THEN 'VARCHAR2' WHEN 'character' THEN 'CHAR' "
    "WHEN 'text' THEN 'CLOB' WHEN 'date' THEN 'DATE' "
    "WHEN 'timestamp without time zone' THEN 'TIMESTAMP' "
    # A timestamptz is TIMESTAMP WITH LOCAL TIME ZONE (#1208); WITH TIME ZONE is
    # the ora_tstz composite, named where it is met (#1272).
    "WHEN 'timestamp with time zone' THEN 'TIMESTAMP WITH LOCAL TIME ZONE' "
    "WHEN 'bytea' THEN 'BLOB' WHEN 'boolean' THEN 'NUMBER' "
    'ELSE upper($1) END $$;'
    # Oracle-shaped catalog views over information_schema / pg_catalog. Oracle
    # treats the user as the schema and folds names upper-case, so `owner` and the
    # object names are UPPER(pg schema/relation), and a client that filters
    # `owner = SYS_CONTEXT('userenv','current_schema')` (which returns UPPER'd
    # current schema) sees objects in its own schema. Enough columns for the
    # SQLAlchemy Oracle dialect's reflection (get_table_names / get_columns /
    # get_pk_constraint / get_indexes).
    'CREATE OR REPLACE VIEW sys.all_tables AS SELECT upper(table_schema) AS owner, '
    'ora_name(table_name) AS table_name, NULL::text AS tablespace_name, '
    'NULL::text AS iot_name, NULL::text AS duration, '
    'NULL::text AS compression, NULL::text AS compress_for '
    "FROM information_schema.tables WHERE table_type='BASE TABLE' "
    "AND table_schema NOT IN ('pg_catalog','information_schema','oracle','sys') "
    # Oracle GLOBAL TEMPORARY tables are PostgreSQL temporary tables (session-local,
    # in a pg_temp schema); report them under the current schema like Oracle does.
    'UNION ALL SELECT upper(current_schema()), ora_name(table_name), NULL, NULL, '
    "'SYS$SESSION', NULL, NULL FROM information_schema.tables "
    "WHERE table_type='LOCAL TEMPORARY';"
    'CREATE OR REPLACE VIEW sys.user_tables AS SELECT table_name, tablespace_name, '
    'iot_name, duration FROM all_tables WHERE owner=upper(current_schema());'
    'CREATE OR REPLACE VIEW sys.all_views AS SELECT upper(table_schema) AS owner, '
    'ora_name(table_name) AS view_name, view_definition AS text '
    'FROM information_schema.views '
    "WHERE table_schema NOT IN ('pg_catalog','information_schema','oracle','sys');"
    'CREATE OR REPLACE VIEW sys.all_sequences AS SELECT upper(sequence_schema) AS '
    'sequence_owner, ora_name(sequence_name) AS sequence_name, '
    'minimum_value::numeric AS min_value, maximum_value::numeric AS max_value, '
    'increment::numeric AS increment_by, '
    "CASE cycle_option WHEN 'YES' THEN 'Y' ELSE 'N' END AS cycle_flag, "
    "'N' AS order_flag, 20::numeric AS cache_size, start_value::numeric AS last_number "
    'FROM information_schema.sequences '
    "WHERE sequence_schema NOT IN ('pg_catalog','information_schema','oracle','sys');"
    'CREATE OR REPLACE VIEW sys.user_sequences AS SELECT sequence_name, min_value, '
    'max_value, increment_by, cycle_flag, order_flag, cache_size, last_number '
    'FROM all_sequences WHERE sequence_owner=upper(current_schema());'
    'CREATE OR REPLACE VIEW sys.all_mviews AS SELECT upper(schemaname) AS owner, '
    'ora_name(matviewname) AS mview_name, definition AS query FROM pg_matviews;'
    'CREATE OR REPLACE VIEW sys.all_mview_comments AS SELECT upper(schemaname) AS owner, '
    "ora_name(matviewname) AS mview_name, obj_description((quote_ident(schemaname)||'.'||"
    'quote_ident(matviewname))::regclass) AS comments FROM pg_matviews;'
    # INVISIBLE columns (#1195): a 12.1 column attribute PostgreSQL has no equal
    # for, recorded by the column's own identity so a rename keeps it.
    'CREATE TABLE IF NOT EXISTS sys.ora_invisible_columns ('
    'relid oid NOT NULL, attnum smallint NOT NULL, PRIMARY KEY (relid, attnum));'
    # The Oracle type a column was declared with, where PostgreSQL keeps less of
    # it than Oracle reports -- a RAW(n)'s length, which bytea has no room for
    # (#1386). Keyed by the column's own identity, so a rename keeps it.
    'CREATE TABLE IF NOT EXISTS sys.ora_columns ('
    'relid oid NOT NULL, attnum smallint NOT NULL, data_type text NOT NULL, '
    'data_length integer, data_precision integer, data_scale integer, '
    'PRIMARY KEY (relid, attnum));'
    # The type a collection's elements were declared as, where PostgreSQL keeps
    # less of it: an NVARCHAR2 / NCHAR (#1437), a RAW(n) and its n (#1545). A
    # collection is a domain over an array, with no relation for sys.ora_columns
    # to key a row by, so it is kept here by the domain's oid.
    'CREATE TABLE IF NOT EXISTS sys.ora_collection_elements ('
    'typid oid PRIMARY KEY, data_type text NOT NULL);'
    'ALTER TABLE sys.ora_collection_elements '
    'ADD COLUMN IF NOT EXISTS data_length integer;'
    # The translation report (#1557): per normalised statement, the translation
    # rules it needed, how often it ran and failed, and whether it came as native
    # PostgreSQL (#1556). Keyed by a hash, as a statement may be longer than an
    # index entry can be.
    'CREATE TABLE IF NOT EXISTS sys.ora_translation_log ('
    'statement_key text PRIMARY KEY, statement text NOT NULL, '
    'rules text[] NOT NULL, runs bigint NOT NULL, failures bigint NOT NULL, '
    'last_error text, native boolean NOT NULL, '
    'first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL);'
    # The fractional-seconds precision a TIMESTAMP(n) WITH TIME ZONE column was
    # declared with (#1308). WITH TIME ZONE is the ora_tstz composite, which has
    # no type modifier to hold it, so it is kept here; a column not listed has
    # Oracle's default, 6. Keyed by the column's own
    # identity; all_tab_cols reads it (#1480).
    'CREATE TABLE IF NOT EXISTS sys.ora_tstz_precision ('
    'relid oid NOT NULL, attnum smallint NOT NULL, prec smallint NOT NULL, '
    'PRIMARY KEY (relid, attnum));'
    # The columns created with a quoted all-lower-case name (#1204). PostgreSQL
    # stores "abc" exactly as it stores an unquoted abc, which Oracle would have
    # folded to ABC, so the difference has to be kept here. Keyed by PostgreSQL's
    # own column identity; rows of dropped tables are pruned on the next write.
    'CREATE TABLE IF NOT EXISTS sys.ora_quoted_names ('
    'relid oid NOT NULL, attnum smallint NOT NULL, PRIMARY KEY (relid, attnum));'
    # The PL/SQL packages (#1605), each a schema of its name: its owner, the DDL
    # of the stand-ins its spec declares -- routines that raise until a body
    # replaces them -- and whether its spec and body compiled. A body is NULL
    # until there is one, INVALID when it did not compile.
    'CREATE TABLE IF NOT EXISTS sys.ora_packages ('
    'name text PRIMARY KEY, owner text NOT NULL, stubs text NOT NULL, '
    "spec text NOT NULL DEFAULT 'VALID', body text);"
    # The types a package declares (#1607), each a PostgreSQL type of its
    # schema, with the PL/SQL it was declared as -- what the dictionary and a
    # client's type metadata report -- in declaration order. A body's own types
    # are private: the dictionary does not list them.
    'CREATE TABLE IF NOT EXISTS sys.ora_plsql_types ('
    'package text NOT NULL, name text NOT NULL, declaration text NOT NULL, '
    'public boolean NOT NULL, ord integer NOT NULL, PRIMARY KEY (package, name));'
    # What a type's declaration says, read once (#1607): a record's fields, a
    # collection's kind, bound, key and element, each field or element a
    # built-in's name, length, precision, scale and character set or a named
    # type -- what the views below and GET_TYPE_SHAPE report.
    'ALTER TABLE sys.ora_plsql_types ADD COLUMN IF NOT EXISTS meta jsonb;'
    # A column's name as the dictionary lists it: a quoted all-lower-case one as
    # written, any other as ora_name folds it (#1599).
    'CREATE OR REPLACE FUNCTION sys.ora_column_name(oid, text) RETURNS text '
    'LANGUAGE sql STABLE AS $$ SELECT CASE WHEN EXISTS (SELECT 1 FROM '
    'sys.ora_quoted_names q JOIN pg_attribute a ON a.attrelid = q.relid '
    'AND a.attnum = q.attnum WHERE q.relid = $1 AND a.attname = $2) THEN $2 '
    'ELSE sys.ora_name($2) END $$;'
    'CREATE OR REPLACE VIEW sys.all_tab_cols AS SELECT '
    "CASE WHEN c.table_schema LIKE 'pg_temp%' THEN upper(current_schema()) "
    'ELSE upper(c.table_schema) END AS owner, '
    'ora_name(c.table_name) AS table_name, ora_column_name((quote_ident('
    "c.table_schema) || '.' || quote_ident(c.table_name))::regclass, "
    'c.column_name) AS column_name, '
    # An INVISIBLE column has no COLUMN_ID, and the visible ones are numbered
    # without it, as Oracle numbers them (#1195). The type stays the one the
    # view always had: CREATE OR REPLACE VIEW cannot change a column's type.
    '(CASE WHEN h.relid IS NULL THEN row_number() OVER (PARTITION BY c.table_schema, '
    'c.table_name, h.relid IS NULL ORDER BY c.ordinal_position) END)::integer'
    '::information_schema.cardinal_number AS column_id, '
    # A collection (an array domain) or an object type is reported by its type's
    # name, as Oracle names it, and its schema is DATA_TYPE_OWNER (#1251). The
    # WITH TIME ZONE composite is the backend's own and reads as the built-in.
    # A DATE column is the ora_date domain over a timestamp, and reads as the
    # 7-byte DATE it is (#1318).
    "CASE WHEN c.data_type = 'ARRAY' AND c.domain_name IS NOT NULL "
    'THEN ora_name(c.domain_name) '
    f"WHEN c.domain_name = '{_DATE_TYPE}' THEN 'DATE' "
    # A TIMESTAMP names its fractional-seconds precision, as Oracle's
    # TIMESTAMP(6) does, and keeps it as DATA_SCALE; its length is that of its
    # wire form -- 11 bytes, 7 with no fraction, 13 WITH TIME ZONE. A BINARY_FLOAT / BINARY_DOUBLE (real / double
    # precision) has no precision in Oracle, and 4 or 8 bytes (#1480).
    "WHEN c.data_type = 'USER-DEFINED' AND c.udt_name = 'ora_tstz' "
    "THEN 'TIMESTAMP(' || coalesce(p.prec, 6) || ') WITH TIME ZONE' "
    "WHEN c.data_type = 'USER-DEFINED' THEN ora_name(c.udt_name) "
    "WHEN o.data_type IS NULL AND c.data_type = 'timestamp without time zone' "
    "THEN 'TIMESTAMP(' || c.datetime_precision || ')' "
    "WHEN o.data_type IS NULL AND c.data_type = 'timestamp with time zone' "
    "THEN 'TIMESTAMP(' || c.datetime_precision || ') WITH LOCAL TIME ZONE' "
    # A type recorded as declared reads as declared (#1386).
    'ELSE coalesce(o.data_type, ora_type_name(c.data_type)) END AS data_type, '
    f"CASE WHEN c.domain_name = '{_DATE_TYPE}' THEN 7 "
    # A CLOB / BLOB, the ora_clob / ora_blob domains, is listed 4000 long, as
    # Oracle lists a LOB column (#1566); an NCLOB's record says so too.
    f"WHEN c.domain_name IN ('{_CLOB_TYPE}', '{_BLOB_TYPE}') THEN 4000 "
    "WHEN c.data_type = 'USER-DEFINED' AND c.udt_name = 'ora_tstz' THEN 13 "
    'WHEN o.data_type IS NULL AND c.data_type IN '
    "('timestamp without time zone', 'timestamp with time zone') "
    'THEN CASE c.datetime_precision WHEN 0 THEN 7 ELSE 11 END '
    "WHEN o.data_type IS NULL AND c.data_type = 'real' THEN 4 "
    "WHEN o.data_type IS NULL AND c.data_type = 'double precision' THEN 8 "
    # A NUMBER is 22 bytes whatever its precision (#1483).
    "WHEN o.data_type IS NULL AND c.data_type = 'numeric' THEN 22 ELSE "
    'coalesce(o.data_length, c.character_maximum_length, c.numeric_precision, 22) '
    'END AS data_length, '
    # A type recorded as declared keeps its precision and scale as recorded, a
    # NULL one included: INTEGER is numeric(38, 0) here but has none (#1483).
    '(CASE WHEN o.data_type IS NOT NULL THEN o.data_precision '
    "WHEN c.data_type IN ('real', 'double precision') "
    'THEN NULL ELSE c.numeric_precision END)'
    '::information_schema.cardinal_number AS data_precision, '
    f"(CASE WHEN c.domain_name = '{_DATE_TYPE}' THEN NULL "
    "WHEN c.data_type = 'USER-DEFINED' AND c.udt_name = 'ora_tstz' "
    'THEN coalesce(p.prec, 6) '
    'WHEN o.data_type IS NULL AND c.data_type IN '
    "('timestamp without time zone', 'timestamp with time zone') "
    'THEN c.datetime_precision '
    'WHEN o.data_type IS NOT NULL THEN o.data_scale ELSE c.numeric_scale END)'
    '::information_schema.cardinal_number AS data_scale, '
    # CHAR_LENGTH is 0 for a type with no character length, not NULL (#1418).
    'coalesce(c.character_maximum_length, 0)'
    '::information_schema.cardinal_number AS char_length, '
    "CASE c.is_nullable WHEN 'YES' THEN 'Y' ELSE 'N' END AS nullable, "
    'c.column_default AS data_default, '
    "CASE WHEN h.relid IS NULL THEN 'NO' ELSE 'YES' END AS hidden_column, "
    "'NO' AS virtual_column, 'NO' AS identity_column, NULL::text AS default_on_null, "
    # Appended: CREATE OR REPLACE VIEW can only add a column at the end.
    "CASE WHEN c.data_type = 'ARRAY' AND c.domain_name IS NOT NULL "
    'THEN ora_owner(c.domain_schema) '
    "WHEN c.data_type = 'USER-DEFINED' AND c.udt_name <> 'ora_tstz' "
    'THEN ora_owner(c.udt_schema) END AS data_type_owner, '
    # The declared length of a character column, in bytes for a CHAR-semantics
    # one -- its record's 4n -- and in characters otherwise; a CLOB's 4000, an
    # NCLOB's 2000, a LONG's 0. CHAR_USED: C for CHAR semantics and the national
    # types, B for BYTE; NULL for anything not character (#1451, measured on
    # 23ai). Appended, as above.
    "(CASE WHEN o.data_type IN ('VARCHAR2', 'CHAR') THEN o.data_length "
    "WHEN o.data_type = 'NCLOB' THEN 2000 WHEN o.data_type = 'LONG' THEN 0 "
    f"WHEN c.domain_name = '{_CLOB_TYPE}' THEN 4000 "
    "WHEN c.data_type IN ('character varying', 'character') "
    'THEN c.character_maximum_length END)'
    '::information_schema.cardinal_number AS char_col_decl_length, '
    "CASE WHEN o.data_type IN ('VARCHAR2', 'CHAR', 'NVARCHAR2', 'NCHAR') THEN 'C' "
    "WHEN c.data_type IN ('character varying', 'character') THEN 'B' "
    'END::text AS char_used '
    'FROM information_schema.columns c '
    'LEFT JOIN sys.ora_invisible_columns h ON h.relid = '
    "(quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass "
    'AND h.attnum = c.ordinal_position '
    'LEFT JOIN sys.ora_columns o ON o.relid = '
    "(quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass "
    'AND o.attnum = c.ordinal_position '
    'LEFT JOIN sys.ora_tstz_precision p ON p.relid = '
    "(quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass "
    'AND p.attnum = c.ordinal_position '
    "WHERE c.table_schema NOT IN ('pg_catalog','information_schema','oracle','sys');"
    'CREATE OR REPLACE VIEW sys.user_tab_cols AS SELECT * FROM all_tab_cols '
    'WHERE owner=upper(current_schema());'
    # Both list an INVISIBLE column, with no COLUMN_ID; only *_TAB_COLS say
    # HIDDEN_COLUMN in Oracle (#1195).
    'CREATE OR REPLACE VIEW sys.all_tab_columns AS SELECT * FROM all_tab_cols;'
    'CREATE OR REPLACE VIEW sys.user_tab_columns AS SELECT * FROM all_tab_cols '
    'WHERE owner=upper(current_schema());'
    'CREATE OR REPLACE VIEW sys.all_col_comments AS SELECT ora_owner(n.nspname) AS owner, '
    'ora_name(c.relname) AS table_name, '
    'ora_column_name(a.attrelid, a.attname) AS column_name, '
    'col_description(c.oid, a.attnum) AS comments '
    'FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
    'JOIN pg_attribute a ON a.attrelid=c.oid '
    "WHERE a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','v','m') "
    "AND n.nspname NOT IN ('pg_catalog','information_schema','oracle','sys');"
    'CREATE OR REPLACE VIEW sys.all_tab_comments AS SELECT ora_owner(n.nspname) AS owner, '
    'ora_name(c.relname) AS table_name, '
    "CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' "
    "ELSE 'TABLE' END AS table_type, obj_description(c.oid) AS comments "
    'FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
    "WHERE c.relkind IN ('r','v','m') "
    "AND n.nspname NOT IN ('pg_catalog','information_schema','oracle','sys');"
    # all_users: every schema is an Oracle user. get_schema_names()/has_schema()
    # read username from here; the emulation schemas (oracle, sys) and PostgreSQL's
    # own (pg_*, information_schema) are hidden, so a reflecting client sees the
    # real schemas (public, test_schema, ...) under Oracle's upper-cased names.
    # NLS parameters: the database character set and the formats a client may
    # read before it does anything else -- the reference thin client's test
    # harness asks nls_database_parameters for NLS_CHARACTERSET while it sets up,
    # so without the view every one of its tests failed ORA-00942 before its
    # body ran. The values are Oracle's defaults, which is what this backend
    # behaves as: AL32UTF8 data, AL16UTF16 national data, AMERICAN formats. The
    # session and instance views carry no character sets, as Oracle's do not.
    'CREATE OR REPLACE VIEW sys.nls_database_parameters AS SELECT * FROM (VALUES '
    "('NLS_LANGUAGE', 'AMERICAN'), ('NLS_TERRITORY', 'AMERICA'), "
    "('NLS_CURRENCY', '$'), ('NLS_ISO_CURRENCY', 'AMERICA'), "
    "('NLS_NUMERIC_CHARACTERS', '.,'), ('NLS_CHARACTERSET', 'AL32UTF8'), "
    "('NLS_CALENDAR', 'GREGORIAN'), ('NLS_DATE_FORMAT', 'DD-MON-RR'), "
    "('NLS_DATE_LANGUAGE', 'AMERICAN'), ('NLS_SORT', 'BINARY'), "
    "('NLS_TIME_FORMAT', 'HH.MI.SSXFF AM'), "
    "('NLS_TIMESTAMP_FORMAT', 'DD-MON-RR HH.MI.SSXFF AM'), "
    "('NLS_TIME_TZ_FORMAT', 'HH.MI.SSXFF AM TZR'), "
    "('NLS_TIMESTAMP_TZ_FORMAT', 'DD-MON-RR HH.MI.SSXFF AM TZR'), "
    "('NLS_DUAL_CURRENCY', '$'), ('NLS_COMP', 'BINARY'), "
    "('NLS_LENGTH_SEMANTICS', 'BYTE'), ('NLS_NCHAR_CONV_EXCP', 'FALSE'), "
    "('NLS_NCHAR_CHARACTERSET', 'AL16UTF16')"
    ') AS p(parameter, value);'
    # The session's NLS_DATE_FORMAT is the one ALTER SESSION set (#1616).
    'CREATE OR REPLACE VIEW sys.nls_session_parameters AS SELECT parameter, '
    "CASE WHEN parameter = 'NLS_DATE_FORMAT' THEN coalesce(nullif("
    "current_setting('seerdb.nls_date_format', true), ''), value) ELSE value END "
    'AS value FROM nls_database_parameters WHERE parameter NOT IN '
    "('NLS_CHARACTERSET', 'NLS_NCHAR_CHARACTERSET');"
    'CREATE OR REPLACE VIEW sys.nls_instance_parameters AS SELECT * FROM '
    'nls_session_parameters;'
    'CREATE OR REPLACE VIEW sys.all_users AS SELECT upper(nspname) AS username, '
    'oid::bigint AS user_id, NULL::timestamp AS created FROM pg_namespace '
    "WHERE nspname NOT LIKE 'pg\\_%' "
    "AND nspname NOT IN ('information_schema','oracle','sys') "
    # A package's schema is the package's, not a user (#1605).
    'AND nspname NOT IN (SELECT name FROM sys.ora_packages);'
    # all_tab_identity_cols: an identity column is a PostgreSQL identity column
    # (pg_attribute.attidentity 'a'=ALWAYS, 'd'=BY DEFAULT). The dialect JOINs this
    # on every get_columns once it believes the server is 12c, so it must exist or
    # reflection raises ORA-00942. Options are reported as Oracle's defaults for now.
    """CREATE OR REPLACE VIEW sys.all_tab_identity_cols AS SELECT ora_owner(n.nspname) AS owner, ora_name(c.relname) AS table_name, ora_column_name(a.attrelid, a.attname) AS column_name, CASE a.attidentity WHEN 'a' THEN 'ALWAYS' ELSE 'BY DEFAULT' END AS generation_type, ora_name(c.relname || '_' || a.attname || '_seq') AS sequence_name, 'START WITH: 1, INCREMENT BY: 1, MAX_VALUE: 9999999999999999999999999999, MIN_VALUE: 1, CYCLE_FLAG: N, CACHE_SIZE: 20, ORDER_FLAG: N' AS identity_options FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE a.attidentity IN ('a','d') AND NOT a.attisdropped AND a.attnum>0 AND n.nspname NOT IN ('pg_catalog','information_schema','oracle','sys');"""
    'CREATE OR REPLACE VIEW sys.all_objects AS SELECT '
    "CASE WHEN n.nspname LIKE 'pg_temp%' THEN upper(current_schema()) "
    'ELSE upper(n.nspname) END AS owner, '
    'ora_name(c.relname) AS object_name, NULL::text AS subobject_name, '
    'c.oid::bigint AS object_id, '
    "CASE c.relkind WHEN 'r' THEN 'TABLE' WHEN 'v' THEN 'VIEW' "
    "WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'i' THEN 'INDEX' "
    "WHEN 'S' THEN 'SEQUENCE' ELSE upper(c.relkind::text) END AS object_type, "
    "'VALID' AS status, "
    "CASE WHEN c.relpersistence='t' THEN 'Y' ELSE 'N' END AS temporary, "
    "'N' AS generated, 'N' AS secondary "
    'FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
    "WHERE c.relkind IN ('r','v','m','i','S') "
    "AND n.nspname NOT IN ('pg_catalog','information_schema','oracle','sys') "
    # A package's spec and body, VALID or INVALID as they compiled (#1605).
    'UNION ALL SELECT k.owner, upper(k.name), NULL::text, n.oid::bigint, '
    "t.object_type, t.status, 'N', 'N', 'N' FROM sys.ora_packages k "
    'JOIN pg_namespace n ON n.nspname = k.name CROSS JOIN LATERAL '
    "(VALUES ('PACKAGE', k.spec), ('PACKAGE BODY', k.body)) t(object_type, status) "
    'WHERE t.status IS NOT NULL '
    # A user's standalone procedure or function, VALID or INVALID (#1606).
    'UNION ALL SELECT ora_owner(n.nspname), ora_name(p.proname), NULL::text, '
    "p.oid::bigint, CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END, "
    f"substr(d.description, {len(_ROUTINE_MARK) + 1}), 'N', 'N', 'N' "
    'FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace '
    "JOIN pg_description d ON d.objoid = p.oid AND d.classoid = 'pg_proc'::regclass "
    f"AND d.objsubid = 0 WHERE d.description LIKE '{_ROUTINE_MARK}%';"
    # orafce has a user_objects too, which lists every schema's objects under
    # their PostgreSQL names; this one is the current schema's, Oracle's way.
    'CREATE OR REPLACE VIEW sys.user_objects AS SELECT * FROM all_objects '
    'WHERE owner=upper(current_schema());'
    # A package and the routines its spec declares, overloads numbered in the
    # order the spec gives them (#1605), and a user's valid standalone routine,
    # under its own name with no procedure name (#1606).
    'CREATE OR REPLACE VIEW sys.all_procedures AS SELECT k.owner, '
    'upper(k.name) AS object_name, NULL::text AS procedure_name, '
    'n.oid::bigint AS object_id, 0 AS subprogram_id, NULL::text AS overload, '
    "'PACKAGE' AS object_type, 'NO' AS aggregate, 'NO' AS pipelined, "
    "'NO' AS parallel, 'NO' AS interface, 'NO' AS deterministic, "
    "'DEFINER' AS authid FROM sys.ora_packages k "
    'JOIN pg_namespace n ON n.nspname = k.name '
    'UNION ALL SELECT owner, object_name, procedure_name, object_id, '
    '(row_number() OVER (PARTITION BY object_id ORDER BY declared, oid))::integer, '
    'CASE WHEN count(*) OVER (PARTITION BY object_id, procedure_name) > 1 THEN '
    '(row_number() OVER (PARTITION BY object_id, procedure_name '
    'ORDER BY declared, oid))::text END, '
    "'PACKAGE', 'NO', 'NO', 'NO', 'NO', 'NO', 'DEFINER' FROM ("
    'SELECT k.owner, upper(k.name) AS object_name, upper(p.proname) AS '
    'procedure_name, n.oid::bigint AS object_id, p.oid, '
    "position(('.' || p.proname || '(') IN lower(k.stubs)) AS declared "
    'FROM sys.ora_packages k JOIN pg_namespace n ON n.nspname = k.name '
    'JOIN pg_proc p ON p.pronamespace = n.oid) m WHERE declared > 0 '
    'UNION ALL SELECT ora_owner(n.nspname), ora_name(p.proname), NULL::text, '
    'p.oid::bigint, 1, NULL::text, '
    "CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END, "
    "'NO', 'NO', 'NO', 'NO', 'NO', 'DEFINER' "
    'FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace '
    "JOIN pg_description d ON d.objoid = p.oid AND d.classoid = 'pg_proc'::regclass "
    f"AND d.objsubid = 0 WHERE d.description = '{_ROUTINE_MARK}VALID';"
    'CREATE OR REPLACE VIEW sys.user_procedures AS SELECT * FROM all_procedures '
    'WHERE owner=upper(current_schema());'
    # The types a package's spec declares (#1607), as 23ai lists them; a named
    # field or element is the package owner's. The OID is the PostgreSQL type's,
    # zero-padded to 16 bytes, as all_types gives a composite's.
    'CREATE OR REPLACE VIEW sys.all_plsql_types AS SELECT k.owner, '
    'upper(t.package) AS package_name, t.name AS type_name, '
    "decode(lpad(to_hex(to_regtype(t.package || '.' || lower(t.name))::oid::bigint), 32, "
    "'0'), 'hex') AS type_oid, t.meta->>'typecode' AS typecode, "
    "(t.meta->>'attributes')::integer AS attributes, "
    "t.meta->>'contains_plsql' AS contains_plsql "
    'FROM sys.ora_plsql_types t JOIN sys.ora_packages k ON k.name = t.package '
    "WHERE t.public AND t.meta ? 'typecode';"
    'CREATE OR REPLACE VIEW sys.all_plsql_type_attrs AS SELECT k.owner, '
    'upper(t.package) AS package_name, t.name AS type_name, '
    "a.v->>'name' AS attr_name, NULL::text AS attr_type_mod, "
    "CASE WHEN (a.v->'type'->>'named')::boolean THEN k.owner END AS attr_type_owner, "
    "a.v->'type'->>'package' AS attr_type_package, "
    "a.v->'type'->>'name' AS attr_type_name, (a.v->'type'->>'length')::integer "
    "AS length, (a.v->'type'->>'precision')::integer AS precision, "
    "(a.v->'type'->>'scale')::integer AS scale, "
    "a.v->'type'->>'charset' AS character_set_name, a.n::integer AS attr_no, "
    "coalesce(a.v->'type'->>'char_used', 'B') AS char_used "
    'FROM sys.ora_plsql_types t JOIN sys.ora_packages k ON k.name = t.package '
    "CROSS JOIN LATERAL jsonb_array_elements(t.meta->'attrs') "
    'WITH ORDINALITY a(v, n) WHERE t.public;'
    'CREATE OR REPLACE VIEW sys.all_plsql_coll_types AS SELECT k.owner, '
    'upper(t.package) AS package_name, t.name AS type_name, '
    "t.meta->>'coll_type' AS coll_type, (t.meta->>'upper_bound')::integer "
    'AS upper_bound, NULL::text AS elem_type_mod, '
    "CASE WHEN (t.meta->'elem'->>'named')::boolean THEN k.owner END "
    "AS elem_type_owner, t.meta->'elem'->>'package' AS elem_type_package, "
    "t.meta->'elem'->>'name' AS elem_type_name, "
    "(t.meta->'elem'->>'length')::integer AS length, "
    "(t.meta->'elem'->>'precision')::integer AS precision, "
    "(t.meta->'elem'->>'scale')::integer AS scale, "
    "t.meta->'elem'->>'charset' AS character_set_name, NULL::text AS elem_storage, "
    "'YES' AS nulls_stored, coalesce(t.meta->'elem'->>'char_used', 'B') "
    "AS char_used, t.meta->>'index_by' AS index_by "
    'FROM sys.ora_plsql_types t JOIN sys.ora_packages k ON k.name = t.package '
    "WHERE t.public AND t.meta ? 'coll_type';"
    'CREATE OR REPLACE VIEW sys.all_constraints AS SELECT ora_owner(tc.constraint_schema) '
    'AS owner, ora_name(tc.constraint_name) AS constraint_name, '
    "CASE tc.constraint_type WHEN 'PRIMARY KEY' THEN 'P' WHEN 'FOREIGN KEY' THEN 'R' "
    "WHEN 'UNIQUE' THEN 'U' WHEN 'CHECK' THEN 'C' ELSE '?' END AS constraint_type, "
    'ora_owner(tc.table_schema) AS table_schema, ora_name(tc.table_name) AS table_name, '
    'NULL::text AS search_condition, '
    'upper(rc.unique_constraint_schema) AS r_owner, '
    'ora_name(rc.unique_constraint_name) AS r_constraint_name, '
    "'NO ACTION' AS delete_rule, 'ENABLED' AS status, 'VALIDATED' AS validated "
    'FROM information_schema.table_constraints tc '
    'LEFT JOIN information_schema.referential_constraints rc '
    'ON rc.constraint_schema=tc.constraint_schema '
    'AND rc.constraint_name=tc.constraint_name '
    "WHERE tc.constraint_schema NOT IN ('pg_catalog','information_schema','oracle','sys');"
    'CREATE OR REPLACE VIEW sys.all_cons_columns AS SELECT ora_owner(kcu.constraint_schema) '
    'AS owner, ora_name(kcu.constraint_name) AS constraint_name, '
    'ora_name(kcu.table_name) AS table_name, ora_column_name((quote_ident('
    "kcu.table_schema) || '.' || quote_ident(kcu.table_name))::regclass, "
    'kcu.column_name) AS column_name, '
    'kcu.ordinal_position AS position '
    'FROM information_schema.key_column_usage kcu '
    "WHERE kcu.constraint_schema NOT IN ('pg_catalog','information_schema','oracle','sys');"
    'CREATE OR REPLACE VIEW sys.all_indexes AS SELECT ora_owner(n.nspname) AS owner, '
    'ora_name(ic.relname) AS index_name, ora_owner(tn.nspname) AS table_owner, '
    'ora_name(tc.relname) AS table_name, '
    "CASE WHEN ix.indisunique THEN 'UNIQUE' ELSE 'NONUNIQUE' END AS uniqueness, "
    "'NORMAL' AS index_type, 'VALID' AS status, "
    'NULL::text AS compression, NULL::int AS prefix_length, '
    'NULL::text AS tablespace_name, NULL::text AS ityp_owner, '
    'NULL::text AS ityp_name, NULL::text AS parameters '
    'FROM pg_index ix JOIN pg_class ic ON ic.oid=ix.indexrelid '
    'JOIN pg_namespace n ON n.oid=ic.relnamespace '
    'JOIN pg_class tc ON tc.oid=ix.indrelid '
    'JOIN pg_namespace tn ON tn.oid=tc.relnamespace '
    "WHERE n.nspname NOT IN ('pg_catalog','information_schema','oracle','sys');"
    'CREATE OR REPLACE VIEW sys.all_ind_columns AS SELECT ora_owner(n.nspname) AS index_owner, '
    'ora_name(ic.relname) AS index_name, ora_owner(tn.nspname) AS table_owner, '
    'ora_name(tc.relname) AS table_name, '
    'ora_column_name(a.attrelid, a.attname) AS column_name, '
    "k.n AS column_position, CASE WHEN (k.opt & 1) = 1 THEN 'DESC' ELSE 'ASC' END AS descend "
    'FROM pg_index ix JOIN pg_class ic ON ic.oid=ix.indexrelid '
    'JOIN pg_namespace n ON n.oid=ic.relnamespace '
    'JOIN pg_class tc ON tc.oid=ix.indrelid '
    'JOIN pg_namespace tn ON tn.oid=tc.relnamespace '
    'CROSS JOIN LATERAL unnest(ix.indkey::int2[], ix.indoption::int2[]) WITH ORDINALITY AS k(attnum, opt, n) '
    'JOIN pg_attribute a ON a.attrelid=tc.oid AND a.attnum=k.attnum '
    "WHERE n.nspname NOT IN ('pg_catalog','information_schema','oracle','sys');"
    # A DESC column in an index is a function-based index in Oracle: the column
    # appears here as the quoted expression "COL" at its position, so reflection
    # (which LEFT JOINs on column_position) renders it as an expression with DESC
    # sorting. PostgreSQL stores it as a plain descending key column, so emit a row
    # only for descending columns (indoption bit 0x01), matching Oracle's shape.
    'CREATE OR REPLACE VIEW sys.all_ind_expressions AS SELECT '
    'ora_owner(n.nspname) AS index_owner, ora_name(ic.relname) AS index_name, '
    'ora_owner(tn.nspname) AS table_owner, ora_name(tc.relname) AS table_name, '
    "'\"' || ora_column_name(a.attrelid, a.attname) || '\"' AS column_expression, "
    'k.n AS column_position '
    'FROM pg_index ix JOIN pg_class ic ON ic.oid=ix.indexrelid '
    'JOIN pg_namespace n ON n.oid=ic.relnamespace '
    'JOIN pg_class tc ON tc.oid=ix.indrelid '
    'JOIN pg_namespace tn ON tn.oid=tc.relnamespace '
    'CROSS JOIN LATERAL unnest(ix.indkey::int2[], ix.indoption::int2[]) '
    'WITH ORDINALITY AS k(attnum, opt, n) '
    'JOIN pg_attribute a ON a.attrelid=tc.oid AND a.attnum=k.attnum '
    'WHERE (k.opt & 1) = 1 '
    "AND n.nspname NOT IN ('pg_catalog','information_schema','oracle','sys');"
    # v$session / v$session_connect_info (#1212): the sessions of this database,
    # a SID being the backend's pid (as the login reply names it) and the
    # identity columns the ones the client declared.
    # Oracle compares a NUMBER with a VARCHAR2 by converting the string, so
    # `sid = sys_context('userenv', 'sid')` works there; PostgreSQL has no
    # integer = text. The operator pair converts as Oracle does, a non-number
    # failing with 22P02, ORA-01722 (#1212).
    'CREATE OR REPLACE FUNCTION sys.ora_eq_int_text(integer, text) RETURNS boolean '
    'LANGUAGE sql IMMUTABLE AS $$ SELECT $1::numeric = $2::numeric $$;'
    'CREATE OR REPLACE FUNCTION sys.ora_eq_text_int(text, integer) RETURNS boolean '
    'LANGUAGE sql IMMUTABLE AS $$ SELECT $1::numeric = $2::numeric $$;'
    'DO $$ BEGIN '
    "IF NOT EXISTS (SELECT FROM pg_operator WHERE oprname = '=' "
    "AND oprleft = 'integer'::regtype AND oprright = 'text'::regtype) THEN "
    'CREATE OPERATOR sys.= (LEFTARG = integer, RIGHTARG = text, '
    'FUNCTION = sys.ora_eq_int_text, COMMUTATOR = OPERATOR(sys.=)); '
    'CREATE OPERATOR sys.= (LEFTARG = text, RIGHTARG = integer, '
    'FUNCTION = sys.ora_eq_text_int, COMMUTATOR = OPERATOR(sys.=)); END IF; '
    'END $$;'
    'CREATE OR REPLACE VIEW sys."v$session" AS SELECT a.pid AS sid, '
    'sys.ora_serial(a.pid) AS "serial#", '
    'coalesce(s.username, upper(a.usename::text)) AS username, '
    "CASE WHEN a.state = 'active' THEN 'ACTIVE' ELSE 'INACTIVE' END AS status, "
    "'USER'::text AS type, s.program, s.machine, s.terminal, s.osuser, "
    'NULL::text AS ecid, NULL::text AS module, NULL::text AS action, '
    'a.backend_start AS logon_time '
    'FROM pg_stat_activity a LEFT JOIN sys.ora_sessions s ON s.pid = a.pid '
    "WHERE a.datname = current_database() AND a.backend_type = 'client backend';"
    # v$version (#1603): the release the Mirror presents, as 12.1 lists it --
    # 11g's five component rows, and the CON_ID 12c added (18c went to one row
    # with BANNER_FULL and BANNER_LEGACY).
    + _V_VERSION_DDL
    + 'CREATE OR REPLACE VIEW sys."v$session_connect_info" AS SELECT a.pid AS sid, '
    'sys.ora_serial(a.pid) AS "serial#", s.driver AS client_driver '
    'FROM pg_stat_activity a LEFT JOIN sys.ora_sessions s ON s.pid = a.pid '
    "WHERE a.datname = current_database() AND a.backend_type = 'client backend';"
    # v$sql_monitor (#1619): the statement a session is running under the
    # database operation its client named (connection.dbop), EXECUTING. Only
    # the reading session's own: another session's operation name is its own
    # setting, which this one cannot see.
    'CREATE OR REPLACE VIEW sys."v$sql_monitor" AS SELECT a.pid AS sid, '
    'sys.ora_serial(a.pid) AS "session_serial#", '
    'coalesce(s.username, upper(a.usename::text)) AS username, '
    "'EXECUTING'::text AS status, "
    "current_setting('seerdb.dbop') AS dbop_name, "
    "nullif(current_setting('seerdb.module', true), '') AS module, "
    "nullif(current_setting('seerdb.action', true), '') AS action, "
    "nullif(current_setting('seerdb.client_info', true), '') AS client_info, "
    "nullif(current_setting('seerdb.client_identifier', true), '') "
    'AS client_identifier, a.query AS sql_text, a.query_start AS sql_exec_start '
    'FROM pg_stat_activity a LEFT JOIN sys.ora_sessions s ON s.pid = a.pid '
    "WHERE a.pid = pg_backend_pid() AND nullif(current_setting('seerdb.dbop', "
    "true), '') IS NOT NULL;"
    # v$parameter (#1353): the instance parameters a client can also read off its
    # login, from the same sources, so the two never disagree -- open_cursors is
    # the AUTH_MAX_OPEN_CURSORS the Mirror reports, db_domain the backend's
    # SessionInfo.db_domain, which this backend leaves unset. Oracle's type codes:
    # 2 for a string, 3 for an integer.
    'CREATE OR REPLACE VIEW sys."v$parameter" AS SELECT p.name::text AS name, '
    'p.type::integer AS type, p.value::text AS value, '
    "p.value::text AS display_value, 'TRUE'::text AS isdefault FROM (VALUES "
    f"('open_cursors', 3, '{_AUTH_MAX_OPEN_CURSORS}'), "
    "('db_domain', 2, NULL)) AS p(name, type, value);"
    # v$database (#1354): the database's name as the login reports it -- the same
    # upper(current_database()) session_info gives SessionInfo.db_name -- and the
    # database's oid as its dbid.
    'CREATE OR REPLACE VIEW sys."v$database" AS SELECT d.oid::bigint AS dbid, '
    'upper(d.datname::text) AS name, upper(d.datname::text) AS db_unique_name '
    'FROM pg_database d WHERE d.datname = current_database();'
    # v$statname / v$sesstat (#1324): the sessions' statistics, which only the
    # Mirror can count (seerdb.server.stats). v$statname names the ones it keeps,
    # numbered as 23ai numbers them. sys.ora_sesstat() unpacks a snapshot of
    # every session's counts that _translate_admin writes into a statement naming
    # v$sesstat as it goes out -- the counts live in the Mirror process, not in
    # the database. The view itself, with no snapshot, is empty.
    'CREATE OR REPLACE VIEW sys."v$statname" AS SELECT s.n AS "statistic#", '
    's.name::text AS name, s.class FROM (VALUES '
    + ', '.join(
        f"({number}, '{name}', 1)" for name, number in stats.STATISTIC_NUMBERS.items()
    )
    + ') AS s(n, name, class);'
    'CREATE OR REPLACE FUNCTION sys.ora_sesstat(text) '
    'RETURNS TABLE (sid integer, "statistic#" integer, value numeric) '
    'LANGUAGE sql IMMUTABLE AS $$ SELECT (e->>0)::integer, (e->>1)::integer, '
    '(e->>2)::numeric FROM jsonb_array_elements($1::jsonb) e $$;'
    'CREATE OR REPLACE VIEW sys."v$sesstat" AS SELECT * FROM sys.ora_sesstat(\'[]\');'
    # Object types (#1127): `CREATE TYPE ... AS OBJECT` is a standalone composite
    # (relkind 'c' -- a table's own row type is not one). A client resolves a
    # type through these two views before it binds or reads a value of it, so a
    # type missing here is `object type ... not found`. Oracle identifies a type
    # by a 16-byte OID; here it is PostgreSQL's own type oid, zero-padded, so the
    # backend can turn an OID a bind carries straight back into the type. A
    # built-in attribute type has no owner, as in Oracle.
    # Object privileges a GRANT gave (#1621): owner, object and grantee as
    # Oracle names them. A user sees a type -- in ALL_TYPES and the views like
    # it, and by name, as gettype() looks one up -- that is its own, PUBLIC's
    # or SYS's, or one it or PUBLIC was granted EXECUTE on.
    'CREATE TABLE IF NOT EXISTS sys.ora_grants (owner text, object_name text, '
    'privilege text, grantee text, '
    'PRIMARY KEY (owner, object_name, privilege, grantee));'
    'CREATE OR REPLACE FUNCTION sys.ora_type_visible(owner text, type_name text) '
    "RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT $1 IN ('PUBLIC', 'SYS', "
    "sys.sys_context('userenv', 'session_user')) OR EXISTS (SELECT 1 FROM "
    'sys.ora_grants g WHERE g.owner = $1 AND g.object_name = $2 '
    "AND g.privilege IN ('EXECUTE', 'ALL') AND g.grantee IN ('PUBLIC', "
    "sys.sys_context('userenv', 'session_user'))) $$;"
    'CREATE OR REPLACE VIEW sys.all_types AS SELECT * FROM ('
    'SELECT ora_owner(n.nspname) AS owner, '
    'ora_name(t.typname) AS type_name, '
    "decode(lpad(to_hex(t.oid::bigint), 32, '0'), 'hex') AS type_oid, "
    "'OBJECT'::text AS typecode, "
    '(SELECT count(*) FROM pg_attribute a WHERE a.attrelid = t.typrelid '
    'AND a.attnum > 0 AND NOT a.attisdropped) AS attributes '
    'FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace '
    'JOIN pg_class c ON c.oid = t.typrelid '
    "WHERE t.typtype = 'c' AND c.relkind = 'c' "
    f"AND t.typname <> '{_TSTZ_TYPE}' AND t.typname !~ '[$]ref$' "
    "AND n.nspname NOT IN ('pg_catalog','information_schema','oracle','sys') "
    # A package's types are its own, listed in all_plsql_types (#1607).
    'AND n.nspname NOT IN (SELECT name FROM sys.ora_packages) '
    # A VARRAY or nested table, a domain over an array, is a COLLECTION with no
    # attributes of its own (#1206).
    'UNION ALL SELECT ora_owner(n.nspname), ora_name(d.typname), '
    "decode(lpad(to_hex(d.oid::bigint), 32, '0'), 'hex'), 'COLLECTION', 0 "
    'FROM pg_type d JOIN pg_namespace n ON n.oid = d.typnamespace '
    "JOIN pg_type b ON b.oid = d.typbasetype AND b.typcategory = 'A' "
    "WHERE d.typtype = 'd' "
    "AND n.nspname NOT IN ('pg_catalog','information_schema','oracle','sys') "
    'AND n.nspname NOT IN (SELECT name FROM sys.ora_packages)) v WHERE sys.ora_type_visible(v.owner, v.type_name);'
    'CREATE OR REPLACE VIEW sys.user_types AS SELECT * FROM all_types '
    'WHERE owner=upper(current_schema());'
    'CREATE OR REPLACE VIEW sys.all_type_attrs AS SELECT * FROM ('
    'SELECT '
    'ora_owner(a.udt_schema) AS owner, ora_name(a.udt_name) AS type_name, '
    'ora_name(a.attribute_name) AS attr_name, '
    # The ora_tstz composite is this backend's TIMESTAMP WITH TIME ZONE, not an
    # object type an attribute holds.
    # Nor are the ora_clob / ora_blob domains: they are CLOB and BLOB (#1256).
    # The zones take Oracle's attribute spellings, and a timestamptz is WITH
    # LOCAL TIME ZONE here (#1208, #1270).
    f"CASE WHEN a.attribute_udt_name = '{_TSTZ_TYPE}' THEN 'TIMESTAMP WITH TZ' "
    # An NCLOB is an ora_clob too, told apart by its record (#1431).
    f"WHEN a.attribute_udt_name = '{_CLOB_TYPE}' AND o.data_type = 'NCLOB' "
    "THEN 'NCLOB' "
    f"WHEN a.attribute_udt_name = '{_CLOB_TYPE}' THEN 'CLOB' "
    # A RAW(n) is a bytea, told apart by its record (#1544); a FLOAT / REAL /
    # DOUBLE PRECISION is a numeric, listed by its declared name (#1423).
    "WHEN o.data_type IN ('NVARCHAR2', 'NCHAR', 'RAW', 'REAL', "
    "'DOUBLE PRECISION', 'FLOAT') THEN o.data_type "
    f"WHEN a.attribute_udt_name = '{_BLOB_TYPE}' THEN 'BLOB' "
    # The ora_date domain is DATE (#1548).
    f"WHEN a.attribute_udt_name = '{_DATE_TYPE}' THEN 'DATE' "
    "WHEN a.data_type = 'USER-DEFINED' THEN ora_name(a.attribute_udt_name) "
    "WHEN a.data_type = 'timestamp with time zone' THEN 'TIMESTAMP WITH LOCAL TZ' "
    # An XMLType attribute is SYS.XMLTYPE's (#1537).
    "WHEN a.data_type = 'xml' THEN 'XMLTYPE' "
    'ELSE ora_type_name(a.data_type) END AS attr_type_name, '
    "CASE WHEN a.data_type = 'xml' THEN 'SYS' "
    "WHEN a.data_type = 'USER-DEFINED' AND a.attribute_udt_name NOT IN "
    f"('{_TSTZ_TYPE}', '{_CLOB_TYPE}', '{_BLOB_TYPE}', '{_DATE_TYPE}') "
    'THEN ora_owner(a.attribute_udt_schema) END AS attr_type_owner, '
    # A RAW(n)'s length is its record's; the cast keeps the view column's type.
    "CASE WHEN o.data_type = 'RAW' THEN o.data_length::information_schema.cardinal_number "
    'ELSE a.character_maximum_length END AS length, '
    # A FLOAT(b)'s precision is its b; a REAL's, a DOUBLE PRECISION's and a
    # bare FLOAT's is NULL, as Oracle lists them (#1423).
    "CASE WHEN o.data_type IN ('REAL', 'DOUBLE PRECISION', 'FLOAT') "
    'THEN o.data_precision::information_schema.cardinal_number '
    'ELSE a.numeric_precision END AS precision, '
    # A TIMESTAMP(n)'s scale is its n (#1550): PostgreSQL's own precision for a
    # timestamp, the recorded one for the ora_tstz WITH TIME ZONE (#1308),
    # Oracle's 6 when none was declared.
    f"CASE WHEN a.attribute_udt_name = '{_TSTZ_TYPE}' "
    'THEN coalesce(p.prec, 6)'
    '::information_schema.cardinal_number '
    "WHEN a.data_type LIKE 'timestamp%' THEN a.datetime_precision "
    'ELSE a.numeric_scale END AS scale, a.ordinal_position AS attr_no, '
    # The character set a character attribute takes; a client reads the national
    # form (NCHAR_CS) from here (#1433). Appended: CREATE OR REPLACE VIEW can
    # only add a column at the end.
    # A national attribute -- NVARCHAR2 or NCLOB, told apart by its record --
    # takes NCHAR_CS, which the client reads its AL16UTF16 form from (#1383).
    "CASE WHEN o.data_type IN ('NVARCHAR2', 'NCHAR', 'NCLOB') THEN 'NCHAR_CS' "
    "WHEN a.data_type IN ('character varying', 'character', 'text') "
    f"OR a.attribute_udt_name = '{_CLOB_TYPE}' THEN 'CHAR_CS' "
    'END::text AS character_set_name, '
    # How a character attribute's length was declared: C for CHAR semantics,
    # recorded with the DDL (#1451), and the national types; B for every other
    # attribute, a number or a date included -- unlike all_tab_cols, which
    # leaves those NULL; measured on 23ai (#1573). Appended, as above.
    "CASE WHEN o.data_type IN ('VARCHAR2', 'CHAR', 'NVARCHAR2', 'NCHAR') THEN 'C' "
    "ELSE 'B' END::text AS char_used "
    'FROM information_schema.attributes a '
    'LEFT JOIN sys.ora_columns o ON o.relid = '
    "(quote_ident(a.udt_schema) || '.' || quote_ident(a.udt_name))::regclass "
    'AND o.attnum = a.ordinal_position '
    'LEFT JOIN sys.ora_tstz_precision p ON p.relid = '
    "(quote_ident(a.udt_schema) || '.' || quote_ident(a.udt_name))::regclass "
    'AND p.attnum = a.ordinal_position '
    f"WHERE a.udt_name <> '{_TSTZ_TYPE}' AND a.udt_name !~ '[$]ref$' "
    'AND a.udt_schema NOT IN (SELECT name FROM sys.ora_packages) '
    "AND a.udt_schema NOT IN ('pg_catalog','information_schema','oracle','sys')) v WHERE sys.ora_type_visible(v.owner, v.type_name);"
    'CREATE OR REPLACE VIEW sys.user_type_attrs AS SELECT * FROM all_type_attrs '
    'WHERE owner=upper(current_schema());'
    # Collection types (#1134): a VARRAY or nested table is a domain over an
    # array, its bound the CHECK a VARRAY carries. A client reads the element's
    # type here once the type shape has told it the element is an object.
    'CREATE OR REPLACE VIEW sys.all_coll_types AS SELECT * FROM ('
    'SELECT ora_owner(n.nspname) AS owner, '
    'ora_name(d.typname) AS type_name, '
    "CASE WHEN k.bound IS NULL THEN 'TABLE' ELSE 'VARYING ARRAY' END AS coll_type, "
    'k.bound AS upper_bound, '
    "CASE WHEN e.typtype = 'c' OR (e.typtype = 'd' AND eb.typcategory = 'A') "
    'THEN ora_owner(en.nspname) '
    # An XMLType element is SYS.XMLTYPE's (#1537).
    "WHEN e.oid = 142 THEN 'SYS' END AS elem_type_owner, "
    "CASE WHEN e.typtype = 'c' OR (e.typtype = 'd' AND eb.typcategory = 'A') "
    'THEN ora_name(e.typname) '
    "WHEN e.oid = 142 THEN 'XMLTYPE' "
    # An NVARCHAR2 / NCHAR (#1437) or RAW(n) (#1545) element, told apart by its
    # record.
    'WHEN x.data_type IS NOT NULL THEN x.data_type '
    f"WHEN e.typname = '{_CLOB_TYPE}' THEN 'CLOB' WHEN e.typname = '{_BLOB_TYPE}' THEN 'BLOB' "
    'ELSE ora_type_name(format_type(e.oid, NULL)) END AS elem_type_name, '
    'NULL::text AS elem_type_package, '
    # The element's size, which the domain's typmod carries for its elements: a
    # character length, or a NUMBER's precision and scale (#1206).
    "CASE WHEN x.data_type = 'RAW' THEN x.data_length "
    'WHEN e.oid IN (1042, 1043) AND d.typtypmod > 4 '
    'THEN d.typtypmod - 4 END AS length, '
    'CASE WHEN e.oid = 1700 AND d.typtypmod >= 4 '
    'THEN (d.typtypmod - 4) >> 16 END AS precision, '
    'CASE WHEN e.oid = 1700 AND d.typtypmod >= 4 '
    'THEN (d.typtypmod - 4) & 65535 END AS scale, '
    # The element's character set, as all_type_attrs gives an attribute's (#1433).
    "CASE WHEN x.data_type IN ('NVARCHAR2', 'NCHAR') THEN 'NCHAR_CS' "
    f"WHEN e.oid IN (25, 1042, 1043) OR e.typname = '{_CLOB_TYPE}' "
    "THEN 'CHAR_CS' END::text AS character_set_name "
    'FROM pg_type d JOIN pg_namespace n ON n.oid = d.typnamespace '
    "JOIN pg_type b ON b.oid = d.typbasetype AND b.typcategory = 'A' "
    'JOIN pg_type e ON e.oid = b.typelem '
    'JOIN pg_namespace en ON en.oid = e.typnamespace '
    'LEFT JOIN pg_type eb ON eb.oid = e.typbasetype '
    'LEFT JOIN sys.ora_collection_elements x ON x.typid = d.oid '
    'LEFT JOIN LATERAL (SELECT substring(pg_get_constraintdef(c.oid) '
    "FROM '<=\\s*([0-9]+)')::int AS bound FROM pg_constraint c "
    'WHERE c.contypid = d.oid LIMIT 1) k ON true '
    "WHERE d.typtype = 'd' "
    "AND n.nspname NOT IN ('pg_catalog','information_schema','oracle','sys') "
    'AND n.nspname NOT IN (SELECT name FROM sys.ora_packages)) v WHERE sys.ora_type_visible(v.owner, v.type_name);'
    'CREATE OR REPLACE VIEW sys.user_coll_types AS SELECT * FROM all_coll_types '
    'WHERE owner=upper(current_schema());'
    # REFs (#1127): which tables are Oracle object tables, and of what type --
    # what PostgreSQL's reloftype said while they were typed tables -- and the
    # one lookup every per-type sys.deref() makes: the object's attributes, as
    # the type's text form, from the row whose hidden id the REF carries. A REF
    # to a row that is gone yields NULL, as Oracle's dangling REF does.
    'CREATE TABLE IF NOT EXISTS sys.ora_object_tables '
    '(relid oid PRIMARY KEY, typ oid NOT NULL);'
    'CREATE OR REPLACE FUNCTION sys.ora_deref_row(tab oid, id uuid, typ regtype) '
    'RETURNS text LANGUAGE plpgsql STABLE AS $$ DECLARE cols text; res text; BEGIN '
    'IF tab IS NULL OR id IS NULL THEN RETURN NULL; END IF; '
    "SELECT string_agg(quote_ident(attname), ',' ORDER BY attnum) INTO cols "
    'FROM pg_attribute WHERE attrelid = (SELECT typrelid FROM pg_type '
    'WHERE oid = typ) AND attnum > 0 AND NOT attisdropped; '
    'EXECUTE format(\'SELECT ROW(%s)::%s::text FROM %s WHERE "sys_nc_oid$" = $1\', '
    'cols, typ, tab::regclass) INTO res USING id; RETURN res; END $$;'
)

# What the installed dictionary is stamped with, as the `sys` schema's comment:
# a digest of the DDL itself, so any change to a view or function reinstalls it
# and an unchanged one is left alone (#1152).
_DICTIONARY_STAMP = (
    'seerdb dictionary ' + hashlib.sha256(_ORACLE_DICTIONARY_DDL.encode()).hexdigest()
)
# The views the dictionary script defines, in the order it creates them (#1571).
_DICTIONARY_VIEWS: Final = tuple(
    dict.fromkeys(
        re.findall(r'CREATE OR REPLACE VIEW (sys\.\w+)', _ORACLE_DICTIONARY_DDL)
    )
)

# The PostgreSQL `interval` OID (pg_type.oid) — the base type ora_intervalym is a
# domain over, so both YEAR TO MONTH and DAY TO SECOND columns report it on the
# wire.
_INTERVAL_OID = 1186
# PostgreSQL `date` and `timestamp` (without time zone): the ora_date domain is
# over the timestamp, and a DATE column describes as a bare date would (#1316).
_DATE_OID = 1082
_TIMESTAMP_OID = 1114


class OraInterval(datetime.timedelta):
    """A ``timedelta`` that also carries the interval's whole-month count.

    psycopg's default loader turns a PostgreSQL ``interval`` into a plain
    ``timedelta``, which has no notion of months — so a YEAR TO MONTH interval
    (``3-7``) arrives as an approximate day count and its calendar months are
    lost. The loaders below return this subclass instead, capturing ``months``
    from the raw value while still being a real ``timedelta``: a DAY TO SECOND
    interval keeps its exact duration with ``months == 0`` (so the existing
    INTERVALDS encode path, which tests ``isinstance(value, timedelta)``, is
    untouched), and a YEAR TO MONTH interval carries its months for the read path
    to turn into an :class:`IntervalYM`.
    """

    months: int

    def __new__(cls, *, months: int, td: datetime.timedelta) -> 'OraInterval':
        self = super().__new__(
            cls, days=td.days, seconds=td.seconds, microseconds=td.microseconds
        )
        self.months = months
        return self


# `<n> years <m> mons` in a PostgreSQL interval's text form (either field may be
# signed and either may be absent). Their sum is the whole-month count.
_PG_INTERVAL_YEARS = re.compile(r'(-?\d+)\s+years?')
_PG_INTERVAL_MONS = re.compile(r'(-?\d+)\s+mons?')


# A BC value in PostgreSQL's ISO text form: '4712-01-01 BC', or with a time and
# an optional fraction for a timestamp.
_PG_BC_TEXT = re.compile(
    r'^(\d+)-(\d\d)-(\d\d)(?: (\d\d):(\d\d):(\d\d)(?:\.(\d{1,6}))?)? BC$'
)


def _bc_date_loader(base: type) -> type:
    # A text loader for `date` / `timestamp` that falls back to a BcDate where
    # psycopg's own refuses the value: a year before 1, which no datetime can
    # hold (#1063). PostgreSQL numbers BC years as Oracle does, without a year 0,
    # so 4712 BC is -4712. Every other value is psycopg's.
    class _Loader(base):  # type: ignore[valid-type, misc]
        def load(self, data):
            try:
                return super().load(data)
            except psycopg.DataError:
                match = _PG_BC_TEXT.match(bytes(data).decode())
                if match is None:
                    raise
                (year, month, day, hour, minute, second, frac) = match.groups()
                return BcDate(
                    -int(year),
                    int(month),
                    int(day),
                    int(hour or 0),
                    int(minute or 0),
                    int(second or 0),
                    int((frac or '0').ljust(6, '0')),
                )

    return _Loader


class _IntervalMonthsTextLoader(Loader):
    # Parse the month fields out of the text form, delegating the duration to
    # psycopg's built-in text interval loader.
    format = psycopg.pq.Format.TEXT

    def __init__(self, oid: int, context=None) -> None:
        super().__init__(oid, context)
        from psycopg.types.datetime import IntervalLoader

        self._base = IntervalLoader(oid, context)

    def load(self, data) -> OraInterval:
        text = bytes(data).decode()
        years = int(m.group(1)) if (m := _PG_INTERVAL_YEARS.search(text)) else 0
        mons = int(m.group(1)) if (m := _PG_INTERVAL_MONS.search(text)) else 0
        return OraInterval(months=years * 12 + mons, td=self._base.load(data))


class _IntervalMonthsBinaryLoader(Loader):
    # The binary form is int64 microseconds, int32 days, int32 months; take the
    # months field and delegate the duration to the built-in binary loader.
    format = psycopg.pq.Format.BINARY

    def __init__(self, oid: int, context=None) -> None:
        super().__init__(oid, context)
        from psycopg.types.datetime import IntervalBinaryLoader

        self._base = IntervalBinaryLoader(oid, context)

    def load(self, data) -> OraInterval:
        _micros, _days, months = struct.unpack('!qii', data)
        return OraInterval(months=months, td=self._base.load(data))


def _to_interval_ym(value: 'OraInterval | None') -> 'IntervalYM | None':
    # An OraInterval → an IntervalYM built from its whole-month count; IntervalYM
    # normalises the split (0, 43) → 3y 7m and shares the sign, so a negative
    # (0, -14) → -1y -2m. A None (SQL NULL) passes through.
    if value is None:
        return None
    return IntervalYM(0, getattr(value, 'months', 0))


# One Oracle bind reference: `:` + an identifier or number (`:x`, `:my_var`,
# `:1`), or a quoted name (`:"desc"`), which is how a client reaches a name the
# plain form cannot express (#686). A `::` cast is left alone (handled by the
# scan below, which only starts a bind where the previous char isn't `:`).
_BIND_REF = re.compile(r':(?:"([^"\n]+)"|(\w+))')


def _bind_name(match: 're.Match') -> str:
    # The name either spelling refers to. The quotes are not part of it.
    return match.group(1) if match.group(1) is not None else match.group(2)


def _bind_key(name: str) -> str:
    # A psycopg dict key for a bind name — a numbered bind (:1), and a quoted
    # name that is not a plain identifier, aren't valid placeholder keys, so
    # prefix them (b1). Ordinary named binds keep their name.
    return name if name.isidentifier() else f'b{name}'


def _bind_spans(sql: str) -> Iterator[tuple[str, str, str | None]]:
    # Each span of `sql` as (kind, text, bind name) -- the name only for a bind
    # -- read by the shared tokenizer, so a `:x` inside a comment or a literal
    # is text and an apostrophe in a comment opens nothing (#1692). A bind the
    # tokenizer allows but Oracle's binds here do not (a quoted name spaced off
    # its colon) is text too.
    for token in sql_tokens(sql):
        text = sql[token.start : token.end]
        match = _BIND_REF.fullmatch(text) if token.kind == 'bind' else None
        yield (token.kind, text, _bind_name(match) if match else None)


# An alternative-quoting literal, q'[...]', whatever its delimiter (#1692).
_Q_LITERAL = re.compile(r"(?s)([nN]?)[qQ]'(.)(.*)(.)'")


def _plain_quoting(sql: str) -> str:
    """`sql` with each alternative-quoting literal -- `q'[it's]'`, Oracle's way
    of writing an apostrophe without doubling it -- as the ordinary literal it
    means, `'it''s'` (#1692). PostgreSQL has no such syntax, and every later
    rewrite reads ordinary literals only, so this runs first. One that does not
    close is left as it is, to fail as sent."""
    if "q'" not in sql.lower():
        return sql
    out = []
    for token in sql_tokens(sql):
        text = sql[token.start : token.end]
        match = _Q_LITERAL.fullmatch(text) if token.kind == 'string' else None
        if match is not None:
            (national, _opening, body, _closing) = match.groups()
            text = national + "'" + body.replace("'", "''") + "'"
        out.append(text)
    return ''.join(out)


_BackendMethod = TypeVar('_BackendMethod', bound=Callable[..., Any])


def _plainly_quoted(method: _BackendMethod) -> _BackendMethod:
    # A backend entry point taking the statement first: it sees the statement
    # with its q-literals made ordinary ones (#1692), before any rewrite.
    @functools.wraps(method)
    def wrapper(self: Any, sql: str, *args: Any, **kwargs: Any) -> Any:
        return method(self, _plain_quoting(sql), *args, **kwargs)

    return cast(_BackendMethod, wrapper)


# The PostgreSQL type a NULL bind is cast to, from the type the client declared
# for it (#699). A NULL carries no type of its own: PostgreSQL either refuses a
# parameter it cannot infer ("could not determine data type of parameter") or
# infers text where the statement needs a number. Oracle reads the type off the
# bind descriptor; the Mirror hands it over as a BindVar, and the cast says it.
# The string types are deliberately absent: a NULL nobody declared travels as
# VARCHAR too, so a text cast would turn `id = :x` on a NUMBER column into
# `numeric = text` and break it, while an uncast placeholder lets PostgreSQL
# infer the column's type from context, which is what an Oracle NULL does.
_NULL_CASTS = {
    TNS_TYPE_NUMBER: 'numeric',
    TNS_TYPE_BFLOAT: 'real',
    TNS_TYPE_BDOUBLE: 'double precision',
    TNS_TYPE_DATE: 'timestamp',
    TNS_TYPE_TIMESTAMP: 'timestamp',
    TNS_TYPE_TIMESTAMPTZ: _TSTZ_TYPE,
    TNS_TYPE_TIMESTAMPLTZ: 'timestamptz',
    TNS_TYPE_INTERVALYM: 'interval',
    TNS_TYPE_INTERVALDS: 'interval',
    TNS_TYPE_RAW: 'bytea',
    TNS_TYPE_LONGRAW: 'bytea',
    TNS_TYPE_BLOB: 'bytea',
    TNS_TYPE_BOOLEAN: 'boolean',
}


# A transaction a savepoint taken in it still belongs to: open, or failed.
_OPEN_TRANSACTION = frozenset(
    {psycopg.pq.TransactionStatus.INTRANS, psycopg.pq.TransactionStatus.INERROR}
)


class _PortalName(str):
    """A REF CURSOR bound IN (#1609): the name of the portal the backend opened
    for the client's cursor, sent as PostgreSQL's refcursor -- a routine's
    SYS_REFCURSOR parameter -- not as text."""


class _PortalNameDumper(psycopg.adapt.Dumper):
    oid = 1790  # refcursor

    def dump(self, obj: object) -> bytes:
        return str(obj).encode('utf-8')


class _TypedNull:
    """A NULL object bind (#1622): NULL, but of the object's PostgreSQL type,
    which the client's bind names. Untyped, the NULL resolved no overload of a
    routine that takes several object types."""

    __slots__ = ('oid',)

    def __init__(self, oid: int) -> None:
        self.oid = oid


class _TypedNullDumper(psycopg.adapt.Dumper):
    # One dumper per type: the NULL goes out with the type's oid. psycopg
    # caches dumpers by this key and takes any hashable, though its type names
    # only types.
    def get_key(self, obj: object, format: PyFormat) -> DumperKey:
        return cast(
            DumperKey, (_TypedNull, obj.oid if isinstance(obj, _TypedNull) else 0)
        )

    def upgrade(self, obj: object, format: PyFormat) -> '_TypedNullDumper':
        dumper = _TypedNullDumper(_TypedNull)
        dumper.oid = obj.oid if isinstance(obj, _TypedNull) else 0
        return dumper

    def dump(self, obj: object) -> None:
        return None


@dataclass(frozen=True)
class _CollectionBind:
    """A VARRAY / nested-table bind: the array its domain is over, and the
    domain, which the placeholder is cast to. PostgreSQL types a bare
    placeholder from its value, so `SELECT :o FROM dual` described a plain
    array; cast, the select item names its collection type (#1541)."""

    value: object
    domain: str


def _translate_binds(sql: str, binds: Sequence) -> tuple[str, dict]:
    """Rewrite Oracle bind references to psycopg named placeholders and build the
    parameter dict (#516). Oracle binds by name, so a bind repeated in the text
    (``:x … :x``) is one value, and a ``:`` inside a string literal is not a bind
    — both of which a blind ``:name`` → ``%s`` substitution gets wrong. Distinct
    binds map to ``binds`` in first-appearance order (positional ``:1 :2`` and a
    single dict/list of values both land correctly). A literal ``%`` in the text
    (a LIKE pattern, a column name) is doubled: psycopg treats the bound query as
    a format string, so a bare ``%`` would be read as a broken placeholder."""
    values = list(binds)
    names: list[str] = []  # distinct bind names, in first-appearance order
    out: list[str] = []
    tstz_keys: set[str] = set()  # bind keys whose value is an aware datetime
    ltz_keys: set[str] = set()  # bind keys whose value is an LtzValue
    intervalym_keys: set[str] = set()  # bind keys whose value is an IntervalYM
    for _kind, text, name in _bind_spans(sql):
        if name is None:
            # Everything that is not a bind -- a literal, a quoted identifier,
            # a comment (a `:x` in any of them is text) -- goes as it is, its
            # `%` doubled for psycopg's format-string parsing.
            out.append(text.replace('%', '%%'))
            continue
        if name not in names:
            names.append(name)
        key = _bind_key(name)
        value = values[names.index(name)] if names.index(name) < len(values) else None
        if isinstance(value, BindVar):
            # A typed NULL (#699): the value is None; the cast carries the
            # declared type, where there is a PostgreSQL type to cast to.
            cast = _NULL_CASTS.get(value.tns_type)
            out.append(f'%({key})s::{cast}' if cast else f'%({key})s')
        elif isinstance(value, LtzValue):
            # A TIMESTAMP WITH LOCAL TIME ZONE bind is the instant in the
            # database time zone, where a TIMESTAMP bind is a wall-clock time
            # in the session's (#1208, #1222).
            out.append(f'%({key})s::timestamptz')
            ltz_keys.add(key)
        elif isinstance(value, datetime.datetime) and value.tzinfo is not None:
            # An aware datetime binds a TIMESTAMP WITH TIME ZONE — build the
            # offset-preserving composite so the entered offset survives the
            # round trip rather than being normalised to UTC (#519).
            out.append(f'ROW(%({key})s, %({key}__off)s)::{_TSTZ_TYPE}')
            tstz_keys.add(key)
        elif isinstance(value, _CollectionBind):
            out.append(f'%({key})s::' + value.domain.replace('%', '%%'))
        elif isinstance(value, BlobValue):
            # A BLOB bind -- a temporary LOB the client wrote -- describes
            # as a BLOB, as Oracle's does, where its bytea alone describes
            # as a RAW (#1625): TO_BLOB around it is the item the describe
            # knows as one (_computed_column_types). The value is unchanged.
            out.append(f'to_blob(%({key})s)')
        elif isinstance(value, IntervalYM):
            # An IntervalYM binds an INTERVAL YEAR TO MONTH — send its whole-month
            # count and rebuild a PostgreSQL interval, so the months survive
            # (psycopg has no dumper for IntervalYM) (#504).
            out.append(f'make_interval(months => %({key})s)')
            intervalym_keys.add(key)
        else:
            out.append(f'%({key})s')
    params: dict = {}
    for idx, name in enumerate(names):
        if idx >= len(values):
            continue
        key = _bind_key(name)
        value = values[idx]
        params[key] = (
            value.value if isinstance(value, (BindVar, _CollectionBind)) else value
        )
        if key in ltz_keys:
            params[key] = datetime.datetime.combine(
                value.date(), value.time(), _DB_TIME_ZONE
            )
        elif key in tstz_keys:
            params[f'{key}__off'] = int(values[idx].utcoffset().total_seconds())
        elif key in intervalym_keys:
            params[key] = values[idx].years * 12 + values[idx].months
    return ''.join(out), params


# Oracle → PostgreSQL column-type rewrites for CREATE TABLE (#500). Applied in
# order, so a multi-word / longer keyword comes before a shorter one it contains
# (LONG RAW before RAW / LONG, TIMESTAMP WITH TIME ZONE before TIMESTAMP,
# NVARCHAR2 before VARCHAR2, NCLOB before CLOB). A size suffix the target type
# keeps (VARCHAR2(10) → varchar(10), NUMBER(p,s) → numeric(p,s)) rides along
# because only the keyword is replaced; one PostgreSQL rejects (RAW(16)) is
# matched with its parens and dropped. Word boundaries keep column names and
# other tokens untouched; only CREATE TABLE is rewritten, so a type keyword used
# as an identifier elsewhere is left alone.
# A table's DATE column is the ora_date domain (#1316); elsewhere -- an object
# attribute, a collection element, a routine parameter -- DATE stays the plain
# timestamp(0) of _DDL_TYPE_REWRITES.
_DDL_DATE_COLUMN = re.compile(r'\bDATE\b', re.IGNORECASE)
# A table's FLOAT[(b)], REAL and DOUBLE PRECISION column is Oracle's NUMBER of
# binary precision b, not binary floating point: numeric, its b in sys.ora_columns
# (#1384). Elsewhere -- an object attribute, a routine parameter -- they stay
# PostgreSQL's own float types, which nothing records a precision for.
_DDL_FLOAT_COLUMN = re.compile(
    r'\b(?:FLOAT\b(?:\s*\(\s*\d+\s*\))?|REAL\b|DOUBLE\s+PRECISION\b)',
    re.IGNORECASE,
)
# The Oracle string types and the CHAR / BYTE length qualifier, rewritten in a
# CREATE TABLE's columns and in a DML CAST alike.
_CHAR_BYTE_LENGTH = re.compile(r'\(\s*(\d+)\s+(?:CHAR|BYTE)\s*\)', re.IGNORECASE)
_NVARCHAR2_WORD = re.compile(r'\bNVARCHAR2\b', re.IGNORECASE)
_VARCHAR2_WORD = re.compile(r'\bVARCHAR2\b', re.IGNORECASE)
_DDL_TYPE_REWRITES = [
    # Oracle character-length semantics: VARCHAR2(20 CHAR) / CHAR(1 BYTE) — the
    # `CHAR` / `BYTE` length qualifier PostgreSQL has no syntax for; drop it so the
    # length maps to a plain varchar(n) / char(n) (#759, the reflection fixtures
    # declare columns this way).
    (_CHAR_BYTE_LENGTH, r'(\1)'),
    # SYS_REFCURSOR (a REF CURSOR OUT param) → PostgreSQL's refcursor (#518).
    (re.compile(r'\bSYS_REFCURSOR\b', re.IGNORECASE), 'refcursor'),
    # A `REF <object type>` column (#139): the type's companion `<type>$ref`
    # composite -- the object table's oid and the row's stable id -- which the
    # overloaded sys.deref() resolves (#1127). The column name before REF is
    # kept: it anchors the match to a REF *type*, so a column merely *named* `ref`
    # (ref INTEGER) is left alone. `REF(` (a REF() call) has no space and is not
    # matched.
    (re.compile(r'\b(\w+)\s+REF\s+(\w+)', re.IGNORECASE), r'\1 \2$ref'),
    # ROWID / UROWID column types hold a rowid's text form. Without this the
    # ROWID pseudo-column rewrite reached the column's TYPE and the CREATE
    # failed. A UROWID can hold an index-organized table's logical rowid, which
    # is longer than the 18 characters of a heap one.
    # Anchored to a column definition -- after `(` or `,` -- so a `SELECT ROWID`
    # in CREATE TABLE ... AS SELECT is left to the pseudo-column rewrite.
    (re.compile(r'([(,]\s*\w+)\s+UROWID\b', re.IGNORECASE), r'\1 varchar(4000)'),
    # An XMLType -- a column's, an object attribute's, a collection's element
    # type -- is PostgreSQL's xml, which the describe reports back as
    # SYS.XMLTYPE (#1536, #1537). The type name only: one followed by `(` is the
    # SYS.XMLTYPE(...) constructor, which a CREATE TABLE ... AS SELECT may call.
    (re.compile(r'\b(?:SYS\.)?XMLTYPE\b(?!\s*\()', re.IGNORECASE), 'xml'),
    (re.compile(r'([(,]\s*\w+)\s+ROWID\b', re.IGNORECASE), r'\1 ora_rowid'),
    # A BFILE column holds the two names (#1669).
    (re.compile(r'([(,]\s*\w+)\s+BFILE\b', re.IGNORECASE), r'\1 ora_bfile'),
    (re.compile(r'\bLONG\s+RAW\b', re.IGNORECASE), 'bytea'),
    (re.compile(r'\bRAW\s*\(\s*\d+\s*\)', re.IGNORECASE), 'bytea'),
    (re.compile(r'\bRAW\b', re.IGNORECASE), 'bytea'),
    # WITH LOCAL TIME ZONE normalises to the session zone (like PostgreSQL's own
    # timestamptz), so map it there. WITH TIME ZONE instead *preserves* the entered
    # offset — which timestamptz cannot — so it maps to the `ora_tstz` composite
    # (utc, offset) that carries the offset across the round trip (#519). LOCAL is
    # matched first (it is the more specific keyword).
    (
        re.compile(
            r'\bTIMESTAMP\s*(\(\s*\d+\s*\))?\s+WITH\s+LOCAL\s+TIME\s+ZONE\b',
            re.IGNORECASE,
        ),
        r'timestamptz\1',
    ),
    (
        re.compile(
            r'\bTIMESTAMP\s*(?:\(\s*\d+\s*\))?\s+WITH\s+TIME\s+ZONE\b', re.IGNORECASE
        ),
        _TSTZ_TYPE,
    ),
    (re.compile(r'\bTIMESTAMP\b', re.IGNORECASE), 'timestamp'),
    (re.compile(r'\bDATE\b', re.IGNORECASE), 'timestamp(0)'),
    (
        re.compile(
            r'\bINTERVAL\s+DAY(?:\s*\(\d+\))?\s+TO\s+SECOND(?:\s*\(\d+\))?\b',
            re.IGNORECASE,
        ),
        'interval',
    ),
    # INTERVAL YEAR TO MONTH → the ora_intervalym domain over interval, so the read
    # path can tell it from a DAY TO SECOND interval and preserve the months (#504).
    (
        re.compile(r'\bINTERVAL\s+YEAR(?:\s*\(\d+\))?\s+TO\s+MONTH\b', re.IGNORECASE),
        _INTERVALYM_TYPE,
    ),
    (_NVARCHAR2_WORD, 'varchar'),
    (_VARCHAR2_WORD, 'varchar'),
    (re.compile(r'\bNCHAR\b', re.IGNORECASE), 'char'),
    # NUMBER(*) is a plain NUMBER and NUMBER(*, s) a NUMBER(38, s), as Oracle
    # reads them; PostgreSQL has no `*` precision (#1443).
    (re.compile(r'\bNUMBER\s*\(\s*\*\s*\)', re.IGNORECASE), 'numeric'),
    (
        re.compile(r'\bNUMBER\s*\(\s*\*\s*,\s*(-?\d+)\s*\)', re.IGNORECASE),
        r'numeric(38, \1)',
    ),
    (re.compile(r'\bNUMBER\b', re.IGNORECASE), 'numeric'),
    # INTEGER / INT / SMALLINT are Oracle's names for NUMBER(38) (#1326), not
    # PostgreSQL's integers: those took no numeric value as a function argument, and
    # an integer none as a smallint one -- an object type's constructor, say. An
    # identity column stays PostgreSQL's integer, the only kind it can be.
    (
        re.compile(r'\b(?:INTEGER|INT|SMALLINT)\b(?!\s+GENERATED\b)', re.IGNORECASE),
        'numeric(38)',
    ),
    # CLOB / NCLOB / BLOB → domains over text / bytea, so the read path can tell a
    # LOB column from a plain VARCHAR2 / RAW and preserve empty-vs-NULL (#534).
    (re.compile(r'\bNCLOB\b', re.IGNORECASE), _CLOB_TYPE),
    (re.compile(r'\bCLOB\b', re.IGNORECASE), _CLOB_TYPE),
    (re.compile(r'\bBLOB\b', re.IGNORECASE), _BLOB_TYPE),
    (re.compile(r'\bLONG\b', re.IGNORECASE), 'text'),
    (re.compile(r'\bBINARY_FLOAT\b', re.IGNORECASE), 'real'),
    (re.compile(r'\bBINARY_DOUBLE\b', re.IGNORECASE), 'double precision'),
]
# Oracle table clauses PostgreSQL has no equal for — dropped (the resulting plain
# table is close enough for the suite): an index-organized table is just a table
# (a PRIMARY KEY already gives the index), and GLOBAL TEMPORARY maps to a plain
# TEMPORARY table (ON COMMIT ... ROWS is already valid PostgreSQL).
_DDL_ORG_INDEX = re.compile(r'\s+ORGANIZATION\s+INDEX\b', re.IGNORECASE)
_DDL_GLOBAL_TEMPORARY = re.compile(r'\bGLOBAL\s+TEMPORARY\b', re.IGNORECASE)
# `NESTED TABLE c STORE AS s [( properties )] [RETURN [AS] LOCATOR | VALUE]`, once
# per nested-table column: Oracle requires it to name the column's storage table.
# A nested table is an array domain here, stored in the row (#1194), so there is
# nothing for it to name, and it goes (#1253). The parenthesized properties may
# hold a nested table's own clause for a collection of collections (#1275).
_DDL_NESTED_TABLE_STORE = re.compile(
    r'\s+NESTED\s+TABLE\s+("[^"]+"|[\w$#]+)\s+STORE\s+AS\s+("[^"]+"|[\w$#.]+)',
    re.IGNORECASE,
)
_DDL_NESTED_TABLE_RETURN = re.compile(
    r'\s+RETURN\s+(?:AS\s+)?(?:LOCATOR|VALUE)\b', re.IGNORECASE
)


def _strip_nested_table_storage(sql: str) -> str:
    # Each NESTED TABLE ... STORE AS clause, with the balanced parenthesized
    # properties that may follow it -- a regex cannot balance them -- and then
    # an optional RETURN AS. Parentheses inside quotes do not count.
    while (head := _DDL_NESTED_TABLE_STORE.search(sql)) is not None:
        end = head.end()
        rest = len(sql) - len(sql[end:].lstrip())
        if rest < len(sql) and sql[rest] == '(':
            depth, i, quote = 0, rest, ''
            while i < len(sql):
                ch = sql[i]
                if quote:
                    if ch == quote:
                        quote = ''
                elif ch in '"\'':
                    quote = ch
                elif ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
                i += 1
        tail = _DDL_NESTED_TABLE_RETURN.match(sql, end)
        if tail is not None:
            end = tail.end()
        sql = sql[: head.start()] + sql[end:]
    return sql


# Table compression (#1182) is a storage hint no query can see, and PostgreSQL
# compresses large values on its own. The clause is recognised straight after
# the column list's closing parenthesis, where Oracle puts it, so a column or a
# string that merely says "compress" is left alone.
_DDL_COMPRESSION = re.compile(
    r'\)\s*(?:NOCOMPRESS|(?:ROW\s+STORE\s+)?COMPRESS'
    r'(?:\s+(?:BASIC|ADVANCED|FOR\s+(?:OLTP|ALL\s+OPERATIONS)))?)\b',
    re.IGNORECASE,
)
# Index-organized tables: Oracle gives their rows a logical UROWID — a
# '*'-prefixed base64 of the primary key — where a heap table has a physical
# ROWID. PostgreSQL has neither, so the backend remembers which tables a session
# created ORGANIZATION INDEX and their primary-key columns, and renders ROWID on
# those as '*' || base64(primary key): a stable, '*'-prefixed handle that
# round-trips through a `WHERE ROWID = :bind` because the same expression stands
# on both sides. It is not Oracle's key encoding, just its shape. The primary key
# is read from an inline `col type PRIMARY KEY` or a `PRIMARY KEY (cols)`
# constraint; a column whose type carries parentheses (NUMBER(10,2)) is not
# matched inline, and a table with no recognised key keeps the heap ctid form.
_CREATE_TABLE_NAME = re.compile(
    r'\s*CREATE\s+(?:GLOBAL\s+TEMPORARY\s+)?TABLE\s+([\w.]+)', re.IGNORECASE
)
_PK_CONSTRAINT = re.compile(r'\bPRIMARY\s+KEY\s*\(([^)]+)\)', re.IGNORECASE)
_PK_INLINE = re.compile(r'[(,]\s*(\w+)\s+[^,()]*?\bPRIMARY\s+KEY\b', re.IGNORECASE)
_DROP_TABLE_NAME = re.compile(r'\s*DROP\s+TABLE\s+([\w.]+)', re.IGNORECASE)
# DROP TABLE ... PURGE drops without keeping the table in the recycle bin
# (#1207). PostgreSQL has none, so its DROP TABLE already is that.
_DROP_TABLE_PURGE = re.compile(
    r'(\s*DROP\s+TABLE\s+.+?)\s+PURGE\s*$', re.IGNORECASE | re.DOTALL
)
_STATEMENT_TABLE = re.compile(r'\b(?:FROM|UPDATE|INTO)\s+([\w.]+)', re.IGNORECASE)
# A DML statement, and the RETURNING that reports the rowid of each row it
# touched: Oracle hands the last one back with every INSERT / UPDATE / DELETE
# (cursor.lastrowid), in the same form SELECT ROWID gives.
_DML_HEAD = re.compile(r'\s*(INSERT|UPDATE|DELETE)\b', re.IGNORECASE)
_HAS_RETURNING = re.compile(r'\bRETURNING\b', re.IGNORECASE)
_ROWID_RETURNING = ' RETURNING sys.ora_rowid(tableoid, ctid)'
_ROWID_WORD = re.compile(r'\bROWID\b', re.IGNORECASE)


def _bare_table(name: str) -> str:
    return name.split('.')[-1].upper()


def _iot_primary_key(sql: str) -> tuple[str, list[str]] | None:
    """The (table, primary-key columns) of a ``CREATE TABLE … ORGANIZATION INDEX``,
    or None for any other statement or an IOT whose key isn't recognised."""
    if not _IS_CREATE_TABLE.match(sql) or not _DDL_ORG_INDEX.search(sql):
        return None
    name = _CREATE_TABLE_NAME.match(sql)
    if name is None:
        return None
    constraint = _PK_CONSTRAINT.search(sql)
    if constraint is not None:
        cols = [c.strip() for c in constraint.group(1).split(',') if c.strip()]
    else:
        inline = _PK_INLINE.search(sql)
        cols = [inline.group(1)] if inline is not None else []
    return (_bare_table(name.group(1)), cols) if cols else None


def _urowid_expression(pk_columns: list[str]) -> str:
    """The SQL rendering an IOT row's logical rowid from its primary key."""
    key = ', '.join(pk_columns)
    return f"('*' || encode(convert_to(ROW({key})::text, 'UTF8'), 'base64'))"


_IS_CREATE_TABLE = re.compile(
    r'\s*CREATE\s+(?:GLOBAL\s+TEMPORARY\s+)?TABLE\b', re.IGNORECASE
)
# The table a CREATE TABLE names, and the quoted all-lower-case identifiers in it
# -- the column names Oracle keeps in lower case (#1204).
_CREATE_TABLE_NAME = re.compile(
    r'\s*CREATE\s+(?:GLOBAL\s+TEMPORARY\s+)?TABLE\s+'
    r'((?:"[^"]+"|[\w$#]+)(?:\.(?:"[^"]+"|[\w$#]+))?)',
    re.IGNORECASE,
)
_QUOTED_LOWER_NAME = re.compile(r'"([a-z][a-z0-9_$#]*)"')
# A column declared TIMESTAMP(n) WITH TIME ZONE, for its precision (#1308).
_TSTZ_PRECISION_COLUMN = re.compile(
    r'("[^"]+"|[A-Za-z_][\w$#]*)\s+TIMESTAMP\s*\(\s*(\d)\s*\)\s+WITH\s+TIME\s+ZONE\b',
    re.IGNORECASE,
)
# Oracle's fractional-seconds precision when a TIMESTAMP declares none.
_DEFAULT_FRACTIONAL_PRECISION = 6

# INVISIBLE / VISIBLE columns (#1195). The attribute follows a column's data type
# in CREATE TABLE, ALTER TABLE ... ADD and ALTER TABLE ... MODIFY; PostgreSQL has
# none, so it is taken out of the statement and kept in sys.ora_invisible_columns.
_TABLE_NAME = r'((?:"[^"]+"|[\w$#]+)(?:\.(?:"[^"]+"|[\w$#]+))?)'
_ALTER_TABLE_COLUMNS = re.compile(
    rf'\s*ALTER\s+TABLE\s+{_TABLE_NAME}\s+(ADD|MODIFY)\b\s*', re.IGNORECASE
)
_VISIBILITY_WORD = re.compile(r'\b(IN)?VISIBLE\b', re.IGNORECASE)
_COLUMN_NAME = re.compile(r'\s*("[^"]+"|[\w$#]+)')
# INSERT with no column list, and a query whose whole select list is `*` or
# `alias.*` over one table: what an INVISIBLE column is left out of.
_INSERT_NO_COLUMNS = re.compile(
    rf'(\s*INSERT\s+INTO\s+){_TABLE_NAME}\s*(?=(?:VALUES|SELECT|WITH)\b|\(\s*(?:SELECT|WITH)\b)',
    re.IGNORECASE,
)
_SELECT_STAR = re.compile(
    r'(\s*SELECT\s+(?:(?:DISTINCT|ALL|UNIQUE)\s+)?)(.+?)(\s+FROM\s+)'
    rf'{_TABLE_NAME}'
    r'(?:\s+(?:AS\s+)?(?!(?:WHERE|ORDER|GROUP|FOR|CONNECT|START|FETCH|OFFSET|HAVING)\b)'
    r'(\w+))?\s*(?=$|;|(?:WHERE|ORDER|GROUP|FOR|CONNECT|START|FETCH|OFFSET|HAVING)\b)',
    re.IGNORECASE | re.DOTALL,
)
_NESTED_QUERY = re.compile(r'\b(?:SELECT|FROM)\b', re.IGNORECASE)


@functools.lru_cache(maxsize=32)
def _token_tuple(text: str) -> tuple[SqlToken, ...]:
    # The shared tokenizer's spans of `text`, kept for the next call on the same
    # text: the structure helpers below are called many times per statement.
    return tuple(sql_tokens(text))


def _structure_marks(text: str, start: int, end: int) -> Iterator[tuple[int, str]]:
    # (position, character) of each parenthesis and comma in text[start:end]
    # that is structure: not inside a literal, a quoted identifier or a
    # comment (#1695).
    for token in _token_tuple(text):
        if token.end <= start or token.kind != 'other':
            continue
        if token.start >= end:
            break
        char = text[token.start]
        if char in '(),':
            yield token.start, char


def _top_level_items(text: str, start: int, end: int) -> list[tuple[int, int]]:
    # The comma-separated items of text[start:end], ignoring commas inside
    # parentheses, literals, quoted identifiers and comments.
    items, depth, item_start = [], 0, start
    for at, char in _structure_marks(text, start, end):
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif depth == 0:
            items.append((item_start, at))
            item_start = at + 1
    items.append((item_start, end))
    return items


def _matching_paren(text: str, open_at: int) -> int:
    # The `)` closing the `(` at `open_at`, past literals, quoted identifiers
    # and comments; the end of the text when none does.
    depth = 0
    for at, char in _structure_marks(text, open_at, len(text)):
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return at
    return len(text)


class _Call(NamedTuple):
    # One call found by _calls: its name as written, where the name starts,
    # its `(` and `)`, and the (start, end) span of each top-level argument.
    name: str
    start: int
    open_at: int
    close_at: int
    args: list[tuple[int, int]]


def _calls(sql: str, names: frozenset[str], not_after: str) -> Iterator[_Call]:
    """Each call to one of `names` (upper case) in `sql`, outermost first and
    in text order, outside literals, quoted identifiers and comments (#1695).

    A call is the name as a whole word, then only whitespace, then `(`. One
    right after an alphanumeric character or a character of `not_after` is
    part of something else -- a qualified `pkg.decode(`, say -- and is not
    one. A call that never closes ends the search: the statement is left to
    fail as it was sent.
    """
    tokens = _token_tuple(sql)
    for index, token in enumerate(tokens):
        if token.kind != 'word' or sql[token.start : token.end].upper() not in names:
            continue
        before = sql[token.start - 1 : token.start]
        if before and (before.isalnum() or before in not_after):
            continue
        following = index + 1
        while following < len(tokens) and tokens[following].kind == 'space':
            following += 1
        if following == len(tokens) or sql[tokens[following].start] != '(':
            continue
        open_at = tokens[following].start
        close_at = _matching_paren(sql, open_at)
        if close_at >= len(sql):
            return
        yield _Call(
            sql[token.start : token.end],
            token.start,
            open_at,
            close_at,
            _top_level_items(sql, open_at + 1, close_at),
        )


def _rewrite_calls(
    sql: str,
    names: frozenset[str],
    rewrite: Callable[[str, list[str]], str | None],
    not_after: str,
) -> str:
    # `sql` with each call to one of `names` replaced by `rewrite(name,
    # arguments)`, the arguments as written; None leaves the call, and the
    # calls inside it are still visited. `rewrite` handles the calls nested in
    # a call it replaces, which is why they are not visited again here.
    (out, pos) = ([], 0)
    for call in _calls(sql, names, not_after):
        if call.start < pos:
            continue  # inside a call already replaced
        replacement = rewrite(call.name, [sql[a:b] for a, b in call.args])
        if replacement is None:
            continue
        out.append(sql[pos : call.start])
        out.append(replacement)
        pos = call.close_at + 1
    out.append(sql[pos:])
    return ''.join(out)


def _ddl_column_spans(sql: str) -> tuple[str, list[tuple[int, int]], bool] | None:
    # The table a CREATE TABLE or ALTER TABLE ... ADD / MODIFY names, the span of
    # each column it declares, and whether it is a MODIFY; None for anything else.
    create = _CREATE_TABLE_NAME.match(sql)
    alter = _ALTER_TABLE_COLUMNS.match(sql) if create is None else None
    if create is not None:
        open_at = sql.find('(', create.end())
        if open_at < 0:
            return None
        spans = _top_level_items(sql, open_at + 1, _matching_paren(sql, open_at))
        return create.group(1), spans, False
    if alter is None:
        return None
    rest = alter.end()
    if sql.startswith('(', rest):
        spans = _top_level_items(sql, rest + 1, _matching_paren(sql, rest))
    else:
        spans = [(rest, len(sql.rstrip().rstrip(';')))]
    return alter.group(1), spans, alter.group(2).upper() == 'MODIFY'


# A declared type PostgreSQL keeps less of than Oracle reports, as recorded in
# sys.ora_columns: (data_type, data_length, data_precision, data_scale) (#1386).
_RAW_DECLARED = re.compile(r'\s*RAW\s*\(\s*(\d+)\s*\)', re.IGNORECASE)
# LONG and LONG RAW, which the rewrite stores as text and bytea (#1382).
_LONG_TYPES = {'LONG': TNS_TYPE_LONG, 'LONG RAW': TNS_TYPE_LONGRAW}
_LONG_DECLARED = re.compile(r'\s*LONG(?:\s+(RAW))?\b', re.IGNORECASE)
# NCLOB, which the rewrite stores as the ora_clob domain CLOB is (#1369). On the
# wire it is a CLOB in the national character set form.
_NCLOB_DECLARED = re.compile(r'\s*NCLOB\b', re.IGNORECASE)
# NVARCHAR2(n) and NCHAR[(n)], which the rewrite stores as varchar(n) and
# char(n) (#1383, #1416). Oracle measures them in AL16UTF16: n characters, 2n
# bytes. An NCHAR with no length is NCHAR(1).
_NVARCHAR2_DECLARED = re.compile(
    r'\s*NVARCHAR2\s*\(\s*(\d+)\s*(?:CHAR\s*)?\)', re.IGNORECASE
)
_NCHAR_DECLARED = re.compile(
    r'\s*NCHAR\b(?!\s+VARYING)\s*(?:\(\s*(\d+)\s*(?:CHAR\s*)?\))?', re.IGNORECASE
)
# VARCHAR2(n CHAR) / CHAR(n CHAR): character-length semantics, which the rewrite's
# varchar(n) / char(n) drop (#1451). Oracle sizes such a column at the database
# character set's widest character, four bytes in AL32UTF8: DATA_LENGTH and
# CHAR_COL_DECL_LENGTH are 4n, CHAR_USED is C.
_CHAR_SEMANTICS_DECLARED = re.compile(
    r'\s*(VARCHAR2|VARCHAR|CHARACTER|CHAR)\s*\(\s*(\d+)\s+CHAR\s*\)', re.IGNORECASE
)
# The national type each of PostgreSQL's character types stands for.
_NATIONAL_OF = {TNS_TYPE_VARCHAR: 'NVARCHAR2', TNS_TYPE_CHAR: 'NCHAR'}
_CSFRM_NATIONAL = 2
# An INTERVAL's precisions, which the rewrite's interval / ora_intervalym drop
# (#1381). Oracle's defaults: DAY(2) TO SECOND(6), YEAR(2) TO MONTH.
_INTERVAL_DS_DECLARED = re.compile(
    r'\s*INTERVAL\s+DAY\s*(?:\(\s*(\d+)\s*\))?\s*TO\s+SECOND\b\s*(?:\(\s*(\d+)\s*\))?',
    re.IGNORECASE,
)
# FLOAT[(b)] and its synonyms, which the rewrite stores as numeric (#1384): REAL is
# FLOAT(63), DOUBLE PRECISION and a bare FLOAT are FLOAT(126).
_FLOAT_DECLARED = re.compile(
    r'\s*(?:FLOAT\b\s*(?:\(\s*(\d+)\s*\))?|(REAL)\b|DOUBLE\s+PRECISION\b)',
    re.IGNORECASE,
)
_INTERVAL_YM_DECLARED = re.compile(
    r'\s*INTERVAL\s+YEAR\s*(?:\(\s*(\d+)\s*\))?\s*TO\s+MONTH\b', re.IGNORECASE
)
# A NUMBER with no declared precision but a fixed scale (#1483): INTEGER, INT and
# SMALLINT, a bare DECIMAL / DEC / NUMERIC (scale 0), and NUMBER(*, s). Oracle
# lists their precision as NULL; the rewrite stores numeric(38, s), which is
# NUMBER(38, s)'s too, so only the declaration tells them apart.
_NUMBER_UNSIZED_DECLARED = re.compile(
    r'\s*(?:(?:INTEGER|INT|SMALLINT)\b|(?:DECIMAL|DEC|NUMERIC)\b(?!\s*\()'
    r'|NUMBER\s*\(\s*\*\s*,\s*(-?\d+)\s*\))',
    re.IGNORECASE,
)
# A MODIFY that leaves the column's type alone -- a constraint, a default.
_MODIFY_WITHOUT_TYPE = re.compile(
    r'\s*(?:$|(?:NOT|NULL|DEFAULT|CONSTRAINT|CHECK|UNIQUE|PRIMARY|REFERENCES|'
    r'VISIBLE|INVISIBLE|ENABLE|DISABLE)\b)',
    re.IGNORECASE,
)


def _declared_type(
    definition: str,
) -> tuple[str, int | None, int | None, int | None] | None:
    raw = _RAW_DECLARED.match(definition)
    if raw is not None:
        return ('RAW', int(raw.group(1)), None, None)
    long = _LONG_DECLARED.match(definition)
    if long is not None:
        # Oracle lists a LONG / LONG RAW column with data_length 0.
        return ('LONG RAW' if long.group(1) else 'LONG', 0, None, None)
    if _NCLOB_DECLARED.match(definition):
        return ('NCLOB', 4000, None, None)
    semantics = _CHAR_SEMANTICS_DECLARED.match(definition)
    if semantics is not None:
        kind = (
            'VARCHAR2' if semantics.group(1).upper().startswith('VARCHAR') else 'CHAR'
        )
        return (kind, 4 * int(semantics.group(2)), None, None)
    national = _NVARCHAR2_DECLARED.match(definition)
    if national is not None:
        return ('NVARCHAR2', 2 * int(national.group(1)), None, None)
    national = _NCHAR_DECLARED.match(definition)
    if national is not None:
        return ('NCHAR', 2 * int(national.group(1) or 1), None, None)
    floating = _FLOAT_DECLARED.match(definition)
    if floating is not None:
        bits = int(floating.group(1) or (63 if floating.group(2) else 126))
        return ('FLOAT', 22, bits, None)
    ds = _INTERVAL_DS_DECLARED.match(definition)
    if ds is not None:
        day, second = int(ds.group(1) or 2), int(ds.group(2) or 6)
        return (f'INTERVAL DAY({day}) TO SECOND({second})', 11, day, second)
    ym = _INTERVAL_YM_DECLARED.match(definition)
    if ym is not None:
        year = int(ym.group(1) or 2)
        return (f'INTERVAL YEAR({year}) TO MONTH', 5, year, 0)
    unsized = _NUMBER_UNSIZED_DECLARED.match(definition)
    if unsized is not None:
        return ('NUMBER', 22, None, int(unsized.group(1) or 0))
    return None


# CREATE [OR REPLACE] TYPE t AS VARRAY(n) | TABLE OF NVARCHAR2 / NCHAR / RAW(n):
# a collection whose element sys.ora_collection_elements records (#1437, #1545).
_CREATE_RECORDED_COLLECTION = re.compile(
    r'\s*CREATE\s+(?:OR\s+REPLACE\s+)?TYPE\s+(\S+)\s+(?:FORCE\s+)?(?:AS|IS)\s+'
    r'(?:VARRAY\s*\(\s*\d+\s*\)|TABLE)\s+OF\s+(NVARCHAR2|NCHAR|RAW)\b'
    r'(?:\s*\(\s*(\d+)\s*\))?',
    re.IGNORECASE,
)


# An object attribute's FLOAT[(b)], REAL or DOUBLE PRECISION, which Oracle lists
# by the name it was declared with -- and FLOAT's b only when given -- where a
# table column is a FLOAT of b bits (#1423).
_FLOAT_ATTRIBUTE_DECLARED = re.compile(
    r'\s*(?:(REAL)\b|(DOUBLE\s+PRECISION)\b|FLOAT\b\s*(?:\(\s*(\d+)\s*\))?)',
    re.IGNORECASE,
)
_FLOAT_ATTRIBUTE_TYPES = ('REAL', 'DOUBLE PRECISION', 'FLOAT')


def _attribute_declared_type(
    definition: str,
) -> tuple[str, int | None, int | None, int | None] | None:
    floating = _FLOAT_ATTRIBUTE_DECLARED.match(definition)
    if floating is None:
        return _declared_type(definition)
    if floating.group(1):
        return ('REAL', 22, None, None)
    if floating.group(2):
        return ('DOUBLE PRECISION', 22, None, None)
    bits = floating.group(3)
    return ('FLOAT', 22, int(bits) if bits else None, None)


# CREATE [OR REPLACE] TYPE t [FORCE] AS|IS OBJECT (attributes): its attributes are
# recorded in sys.ora_columns as a table's columns are, keyed by the composite's
# relid (#1431).
_CREATE_OBJECT_TYPE_NAME = re.compile(
    r'\s*CREATE\s+(?:OR\s+REPLACE\s+)?TYPE\s+([\w."$#]+)\s+(?:FORCE\s+)?'
    r'(?:AS|IS)\s+OBJECT\s*\(',
    re.IGNORECASE,
)


def _object_type_spans(sql: str) -> tuple[str, list[tuple[int, int]], bool] | None:
    created = _CREATE_OBJECT_TYPE_NAME.match(sql)
    if created is None:
        return None
    open_at = created.end() - 1
    spans = _top_level_items(sql, open_at + 1, _matching_paren(sql, open_at))
    return created.group(1), spans, False


def _declared_columns(sql: str) -> tuple[str, dict[str, tuple | None]] | None:
    """The table a CREATE TABLE / ALTER TABLE ... ADD / MODIFY names, or the object
    type a CREATE TYPE ... AS OBJECT does (#1431), and for each column or attribute
    it declares a type for, what sys.ora_columns records -- None for a type it
    records nothing for (#1386). A MODIFY that changes no type is left out, so a
    column's record survives `MODIFY (c NOT NULL)`. A name is PostgreSQL's
    spelling: a quoted one as written, an unquoted one in lower case.
    """
    table_spans = _ddl_column_spans(sql)
    found = table_spans or _object_type_spans(sql)
    if found is None:
        return None
    table, spans, modify = found
    declare = _declared_type if table_spans else _attribute_declared_type
    columns: dict[str, tuple | None] = {}
    for start, end in spans:
        name = _COLUMN_NAME.match(sql, start, end)
        if name is None:
            continue
        definition = sql[name.end() : end]
        if modify and _MODIFY_WITHOUT_TYPE.match(definition):
            continue
        column = name.group(1)
        if column.upper() in ('CONSTRAINT', 'PRIMARY', 'UNIQUE', 'FOREIGN', 'CHECK'):
            continue
        columns[column[1:-1] if column.startswith('"') else column.lower()] = declare(
            definition
        )
    return table, columns


def _column_visibility(sql: str) -> tuple[str, tuple[str, list, list, bool] | None]:
    """The statement without its VISIBLE / INVISIBLE column attributes, and what
    they said: (table, columns made invisible, columns made visible, whether the
    statement changed nothing else), or None when it names none (#1195).

    A name is PostgreSQL's spelling: a quoted one as written, an unquoted one in
    lower case.
    """
    if not _VISIBILITY_WORD.search(sql):
        return sql, None
    parsed = _ddl_column_spans(sql)
    if parsed is None:
        return sql, None
    table, spans, modify = parsed
    hidden: list[str] = []
    shown: list[str] = []
    cuts: list[tuple[int, int]] = []
    bare = True
    for start, end in spans:
        name = _COLUMN_NAME.match(sql, start, end)
        if name is None:
            continue
        depth, found = 0, None
        for word in _VISIBILITY_WORD.finditer(sql, name.end(), end):
            depth = sql.count('(', name.end(), word.start()) - sql.count(
                ')', name.end(), word.start()
            )
            if depth == 0:
                found = word
                break
        if found is None:
            bare = False
            continue
        column = name.group(1)
        column = column[1:-1] if column.startswith('"') else column.lower()
        (hidden if found.group(1) else shown).append(column)
        cuts.append((found.start(), found.end()))
        remainder = sql[name.end() : found.start()] + sql[found.end() : end]
        bare = bare and not remainder.strip()
    if not cuts:
        return sql, None
    out, last = [], 0
    for cut_start, cut_end in cuts:
        out.append(sql[last:cut_start])
        last = cut_end
    out.append(sql[last:])
    only_visibility = modify and bare
    return ''.join(out), (table, hidden, shown, only_visibility)


# Oracle auto-commits DDL (an implicit COMMIT before and after), so a DDL statement
# is never rolled back and any pending DML committed with it. PostgreSQL keeps DDL
# transactional, so the Mirror commits after a successful DDL to match — a later
# rollback then discards only the DML, not the table (#532).
_IS_DDL = re.compile(
    r'\s*(CREATE|ALTER|DROP|TRUNCATE|RENAME|COMMENT|GRANT|REVOKE)\b', re.IGNORECASE
)
# Oracle's DDL does not wait for a lock (DDL_LOCK_TIMEOUT is 0): a TRUNCATE, DROP
# or ALTER of a table another session holds fails at once with ORA-00054, where
# PostgreSQL waits for ever (#1191). The wait is bounded, briefly rather than not
# at all, so a lock being released that very moment does not fail the DDL.
_DDL_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '1s'"

# Transaction control sent as SQL text (#1181). Every other statement runs inside
# the `_mirror_stmt` savepoint (see execute), and these cannot: a COMMIT or
# ROLLBACK ends the transaction and the savepoint with it, so the RELEASE after
# it failed and the session was lost; a user SAVEPOINT taken inside it died with
# its RELEASE; and a ROLLBACK TO an earlier savepoint destroys it.
# The notice sys.ora_commit_request() raises (#1630).
_COMMIT_REQUEST = 'seerdb: commit requested'
# The notice dbms_sql.return_result() raises, before the portal's name (#1617).
_IMPLICIT_RESULT = 'seerdb: implicit result '
_TRANSACTION_END = re.compile(r'\s*(COMMIT|ROLLBACK)(?:\s+WORK)?\s*\Z', re.IGNORECASE)
_SAVEPOINT_NAME = r'("[^"]+"|[A-Za-z][\w$]*)'
_SAVEPOINT = re.compile(rf'\s*SAVEPOINT\s+{_SAVEPOINT_NAME}\s*\Z', re.IGNORECASE)
# ALTER SYSTEM KILL SESSION 'sid,serial[,@inst]' [IMMEDIATE | NOREPLAY] (#1212).
_KILL_SESSION = re.compile(
    r"\s*ALTER\s+SYSTEM\s+KILL\s+SESSION\s+'([^']*)'(?:\s+(?:IMMEDIATE|NOREPLAY))*\s*\Z",
    re.IGNORECASE,
)
_KILL_SESSION_WAIT_MS = 5000  # how long a kill waits for the victim to go (#1367)
_KILL_SESSION_ID = re.compile(r'\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*@\d+\s*)?\Z')
_ROLLBACK_TO = re.compile(
    rf'\s*ROLLBACK(?:\s+WORK)?\s+TO\s+(?:SAVEPOINT\s+)?{_SAVEPOINT_NAME}\s*\Z',
    re.IGNORECASE,
)


# An Oracle object type — `CREATE [OR REPLACE] TYPE name AS OBJECT (attrs)` — maps
# to a PostgreSQL composite type (`CREATE TYPE name AS (attrs)`), which a typed
# table (`CREATE TABLE t OF name`) can then be built on. It is not a true Oracle
# object type (no methods, no REF), but it carries the attribute structure and the
# type identity a `SELECT REF(p)` describe reports — enough for the REF tests to
# reach their 11g self-skip (#139).
_CREATE_TYPE_OBJECT = re.compile(
    r'(\s*CREATE\s+(?:OR\s+REPLACE\s+)?TYPE\b.*?\bAS)\s+OBJECT\b',
    re.IGNORECASE | re.DOTALL,
)


# An Oracle VARRAY -- `CREATE TYPE name AS VARRAY(n) OF elem` -- maps to a
# PostgreSQL DOMAIN over an array of the element type, with a CHECK carrying the
# bound: `CREATE DOMAIN name AS elem[] CHECK (VALUE IS NULL OR
# array_length(VALUE, 1) <= n)`. That is the mapping Oracle-to-PostgreSQL
# migrations settle on, and the sibling of the OBJECT rewrite above (#1193).
#
# Measured against the running server rather than assumed: the CHECK does
# enforce the bound (n+1 elements are refused, as Oracle refuses them), and a
# plain `DROP TYPE name` removes the domain, so a caller's teardown needs no
# translation of its own.
#
# Type DDL is compiled like PL/SQL, so Oracle takes a trailing `;` on it, and
# scripts carry one; it is left out of the element type.
#
# Only the plain form. `CREATE OR REPLACE TYPE ... AS VARRAY` is not matched:
# PostgreSQL has no CREATE OR REPLACE DOMAIN, and emitting a plain CREATE would
# quietly drop the replace semantics -- failing on an existing type where Oracle
# succeeds. Better to leave that statement untranslated and let it fail honestly.
_CREATE_TYPE_VARRAY = re.compile(
    r'\s*CREATE\s+TYPE\s+(\S+)\s+AS\s+VARRAY\s*\(\s*(\d+)\s*\)\s+OF\s+(.+?)\s*;?\s*$',
    re.IGNORECASE | re.DOTALL,
)
# A nested table type -- `CREATE TYPE name AS TABLE OF elem` -- is the same
# mapping without the bound: a nested table has no maximum size (#1194). The
# element may itself be a collection type: PostgreSQL takes an array of a domain
# over an array, jagged inner collections and all.
_CREATE_TYPE_TABLE_OF = re.compile(
    r'\s*CREATE\s+TYPE\s+(\S+)\s+AS\s+TABLE\s+OF\s+(.+?)\s*;?\s*$',
    re.IGNORECASE | re.DOTALL,
)


# The type descriptor (TDS) DBMS_PICKLER.GET_TYPE_SHAPE returns: a client reads
# from it whether a type is a collection -- then its bound, its kind and its
# element -- and each attribute's precision, scale and maximum length. Layout
# measured against 23ai, which the offline tests reproduce byte for byte:
#
#   0   ub4  length of what follows          10  flags: 00 object, ff collection
#   4   26 <version>, 1 or 2                 11  29, start of the ADT
#   6   00 01                                12  00 00
#   8   ub2  number of leaf attributes       14  ub4  index table position - 13
#   18  the attributes, 2a, then the blocks a reference points at, then the
#       index table: a ub2 per leaf, its position less 11.
#
# An object's object-typed attribute is embedded, 27 ... 28; a collection-typed
# one, and a collection's named element, is a reference: 1b, the ub4 absolute
# position of an fd block, then fa for an object or fb for a collection. A
# collection's block is fd and its TDS; an object's is fd, a ub4 length, its TDS
# and its null-image TDS, whose leaves are all 1a (one for the object itself).
# The version is 2 when a leaf is of a type newer than Oracle 8.0 (the
# timestamps, BINARY_FLOAT / BINARY_DOUBLE).
@dataclass(frozen=True)
class _TdsLeaf:
    code: bytes
    newer: bool = False


@dataclass(frozen=True)
class _TdsObject:
    attrs: tuple


@dataclass(frozen=True)
class _TdsCollection:
    varray: bool
    bound: int
    element: object
    # A PL/SQL index-by table (#1607): kind 1, no bound.
    index_table: bool = False


_TDS_NULL_LEAF = _TdsLeaf(b'\x1a')


@dataclass(frozen=True)
class _TdsXml:
    """An XMLType attribute or element: a reference to SYS.XMLTYPE's opaque
    descriptor rather than a leaf (#1537)."""


_TDS_XMLTYPE = _TdsXml()
# What an XMLType reference names, measured on a live 23ai: the opaque type's
# block, `fd`, a ub4 13 and these 13 bytes -- the same one SYS.XMLTYPE's own TDS
# carries (_XMLTYPE_TDS). The reference ends 3a where an object's ends fa and a
# collection's fb.
_XMLTYPE_TDS_BLOCK = (
    b'\xfd' + (13).to_bytes(4, 'big') + bytes.fromhex('01000000070000000000000009')
)


def _tds_header(
    body: bytes, leaves: list[int], *, collection: bool, version: int
) -> bytes:
    # `body` starts at absolute offset 18; `leaves` are absolute positions.
    index_at = 18 + len(body)
    index = b''.join((pos - 11).to_bytes(2, 'big') for pos in leaves)
    head = (
        bytes([0x26, version, 0x00, 0x01])
        + len(leaves).to_bytes(2, 'big')
        + bytes([0xFF if collection else 0x00, 0x29, 0x00, 0x00])
        + (index_at - 13).to_bytes(4, 'big')
    )
    rest = head + body + index
    return len(rest).to_bytes(4, 'big') + rest


def _tds_is_newer(shape) -> bool:
    if isinstance(shape, _TdsLeaf):
        return shape.newer
    if isinstance(shape, _TdsObject):
        return any(
            _tds_is_newer(a) for a in shape.attrs if not isinstance(a, _TdsCollection)
        )
    return False


def _tds_reference(shape, block_at: int) -> tuple[bytes, bytes]:
    # A reference to a named object or collection, and the fd block it names.
    if isinstance(shape, _TdsXml):
        return (b'\x1b' + block_at.to_bytes(4, 'big') + b'\x3a', _XMLTYPE_TDS_BLOCK)
    if isinstance(shape, _TdsCollection):
        return (b'\x1b' + block_at.to_bytes(4, 'big') + b'\xfb', b'\xfd' + _tds(shape))
    image = _tds(shape) + _tds_null(shape)
    return (
        b'\x1b' + block_at.to_bytes(4, 'big') + b'\xfa',
        b'\xfd' + len(image).to_bytes(4, 'big') + image,
    )


def _tds(shape) -> bytes:
    """The TDS of an object (_TdsObject) or a collection (_TdsCollection)."""
    if isinstance(shape, _TdsCollection):
        body = bytearray(b'\x1c' + (29).to_bytes(4, 'big'))
        kind = 1 if shape.index_table else 3 if shape.varray else 2
        body += shape.bound.to_bytes(4, 'big') + bytes([kind])
        body += b'\x2a'
        element = shape.element
        if isinstance(element, _TdsLeaf):
            body += element.code
        else:
            # The reference's block follows it directly, at 35.
            (ref, block) = _tds_reference(element, 18 + len(body) + 6)
            body += ref + block
        # An index-by table of a record with a newer leaf is version 2, as 23ai
        # sends one (#1607).
        version = 2 if shape.index_table and _tds_is_newer(element) else 1
        return _tds_header(bytes(body), [18], collection=True, version=version)
    body = bytearray()
    leaves: list[int] = []
    pending: list[tuple[int, object]] = []

    def walk(attrs) -> None:
        for attr in attrs:
            if isinstance(attr, _TdsLeaf):
                leaves.append(18 + len(body))
                body.extend(attr.code)
            elif isinstance(attr, _TdsObject):
                body.append(0x27)
                walk(attr.attrs)
                body.append(0x28)
            else:
                leaves.append(18 + len(body))
                pending.append((len(body) + 1, attr))
                body.extend(b'\x1b\x00\x00\x00\x00\xfb')

    walk(shape.attrs)
    body.append(0x2A)
    for slot, attr in pending:
        block_at = 18 + len(body)
        (ref, block) = _tds_reference(attr, block_at)
        # The whole reference, not just its position: an XMLType's ends 3a where
        # a collection's ends fb (#1537).
        body[slot - 1 : slot + 5] = ref
        body.extend(block)
    version = 2 if _tds_is_newer(shape) else 1
    return _tds_header(bytes(body), leaves, collection=False, version=version)


def _tds_null(shape: _TdsObject) -> bytes:
    # The null-image TDS of an object: a 1a for the object and for each
    # attribute, an embedded object's own set between 27 and 28.
    def nulls(obj: _TdsObject) -> tuple:
        out: list = [_TDS_NULL_LEAF]
        for attr in obj.attrs:
            out.append(
                _TdsObject(nulls(attr))
                if isinstance(attr, _TdsObject)
                else _TDS_NULL_LEAF
            )
        return tuple(out)

    return _tds(_TdsObject(nulls(shape)))


# The metadata block a python-oracledb client runs before it binds or reads an
# object: it calls DBMS_PICKLER.GET_TYPE_SHAPE for the type's OID, version, TDS
# and a cursor of its attributes, then looks the type's own name up. Answered
# here from the catalog, as a whole; the block's text is the client's, fixed.
_TYPE_SHAPE_BLOCK = re.compile(
    r'\bdbms_pickler\s*\.\s*get_type_shape\s*\(', re.IGNORECASE
)
# What GET_TYPE_SHAPE returns for a type that does not exist, as measured.
_TYPE_SHAPE_NOT_FOUND = 1001
# The built-in types' OIDs, as the attribute cursor reports them: 16 bytes,
# zero but for the last.
_BUILTIN_TYPE_OID_BYTE = {
    'NUMBER': 0x0F,
    'INTEGER': 0x16,
    'VARCHAR2': 0x19,
    'CHAR': 0x1A,
    'BINARY_FLOAT': 0x44,
    'BINARY_DOUBLE': 0x45,
    'DATE': 0x08,  # measured on 23ai (#1474)
    'TIMESTAMP': 0x3D,
    'TIMESTAMP WITH TZ': 0x3E,
    'TIMESTAMP WITH LOCAL TZ': 0x41,
    'CLOB': 0x22,
    'NCLOB': 0x22,  # the same built-in OID as CLOB's, measured on 23ai (#1431)
    'NVARCHAR2': 0x19,  # the same as VARCHAR2's, measured on 23ai (#1383)
    'NCHAR': 0x1A,  # the same as CHAR's (#1416)
    'BLOB': 0x23,
    'RAW': 0x17,  # measured on 23ai (#1544)
    # Measured on 23ai, each under its own OID (#1423).
    'REAL': 0x0C,
    'DOUBLE PRECISION': 0x0D,
    'FLOAT': 0x0E,
    # A package type's field or element, measured on 23ai (#1607).
    'BOOLEAN': 0x2E,
    'PL/SQL PLS INTEGER': 0x33,
    'PL/SQL BINARY INTEGER': 0x32,
}


def _tds_number(precision: int = 0, scale: int = -127) -> _TdsLeaf:
    return _TdsLeaf(bytes([0x06, precision & 0xFF, scale & 0xFF]))


def _national_leaf(declared: str | None, typmod: int) -> _TdsLeaf | None:
    # An NVARCHAR2(n) / NCHAR(n) attribute or element is a varchar(n) / char(n)
    # here; its leaf is the national one, 2n bytes, as 23ai sends it (#1383,
    # #1416, #1437). None for any other.
    if declared not in ('NVARCHAR2', 'NCHAR') or typmod <= 4:
        return None
    code = 0x07 if declared == 'NVARCHAR2' else 0x01
    return _tds_chars(code, 2 * (typmod - 4), True)


def _declared_leaf(
    declared: tuple[str, int | None, int | None] | None, typmod: int
) -> _TdsLeaf | None:
    # An attribute's leaf where its declaration says more than the PostgreSQL
    # type does: a RAW(n), a bytea here, is `13` and a ub2 n, as 23ai sends it
    # (#1544); a FLOAT / REAL / DOUBLE PRECISION, a numeric, is `05` and the
    # declared binary precision, 0 for none (#1423); a national one is
    # _national_leaf's. None for any other.
    if declared is None:
        return None
    (data_type, length, precision) = declared
    if data_type == 'RAW' and length:
        return _TdsLeaf(b'\x13' + length.to_bytes(2, 'big'))
    if data_type in _FLOAT_ATTRIBUTE_TYPES:
        return _TdsLeaf(bytes([0x05, precision or 0]))
    return _national_leaf(data_type, typmod)


def _tds_chars(code: int, byte_length: int, national: bool) -> _TdsLeaf:
    return _TdsLeaf(
        bytes([code])
        + byte_length.to_bytes(2, 'big')
        + (b'\x82' if national else b'\x01')
        + b'\x00\x00'
    )


def _tds_timestamp(code: int, scale: int) -> _TdsLeaf:
    return _TdsLeaf(bytes([code, scale]), newer=True)


def _tds_scalar(pg_name: str, typmod: int) -> tuple[_TdsLeaf, str]:
    """A scalar PostgreSQL type's TDS leaf and the Oracle name the dictionary
    reports it by (all_type_attrs), or raise for one with no Oracle equal."""
    if pg_name == 'numeric':
        if typmod < 0:
            return (_tds_number(), 'NUMBER')
        precision = ((typmod - 4) >> 16) & 0xFFFF
        scale = (typmod - 4) & 0xFFFF
        return (
            _tds_number(precision, scale - 0x10000 if scale > 0x7FFF else scale),
            'NUMBER',
        )
    if pg_name in ('int2', 'int4', 'int8'):
        return (_tds_number(0, 0), 'INTEGER')
    if pg_name == 'float4':
        return (_TdsLeaf(b'\x25', newer=True), 'BINARY_FLOAT')
    if pg_name == 'float8':
        return (_TdsLeaf(b'\x2d', newer=True), 'BINARY_DOUBLE')
    if pg_name == 'varchar':
        return (_tds_chars(0x07, typmod - 4 if typmod > 4 else 4000, False), 'VARCHAR2')
    if pg_name == 'bpchar':
        return (_tds_chars(0x01, typmod - 4 if typmod > 4 else 1, False), 'CHAR')
    if pg_name in ('text', _CLOB_TYPE):
        return (_TdsLeaf(b'\x1d'), 'CLOB')
    if pg_name in ('bytea', _BLOB_TYPE):
        return (_TdsLeaf(b'\x1e'), 'BLOB')
    if pg_name == 'timestamp':
        return (_tds_timestamp(0x15, typmod if typmod >= 0 else 6), 'TIMESTAMP')
    if pg_name == 'timestamptz':
        return (
            _tds_timestamp(0x21, typmod if typmod >= 0 else 6),
            'TIMESTAMP WITH LOCAL TZ',
        )
    if pg_name == _TSTZ_TYPE:
        return (_tds_timestamp(0x17, 6), 'TIMESTAMP WITH TZ')
    if pg_name == _DATE_TYPE:
        # A table's DATE column (#1316), as a %ROWTYPE attribute: the TDS code
        # alone, nothing after it, measured on 23ai (#1474).
        return (_TdsLeaf(b'\x02'), 'DATE')
    raise UnsupportedFeature(f'type shape: {pg_name} has no Oracle attribute type')


# Oracle session / user admin statements the provisioning issues, mapped to their
# PostgreSQL equivalent or a no-op (#759). Oracle treats a user as a schema, so a
# CREATE USER becomes a CREATE SCHEMA; ALTER SESSION SET CURRENT_SCHEMA points
# unqualified name resolution at a schema, which is PostgreSQL's search_path; the
# tablespace / grant / password admin has no PostgreSQL analogue and becomes a
# harmless no-op so the statement succeeds.
_ALTER_SESSION_SCHEMA = re.compile(
    r'\s*ALTER\s+SESSION\s+SET\s+CURRENT_SCHEMA\s*=\s*"?(\w+)"?\s*$', re.IGNORECASE
)
# ALTER SESSION SET TIME_ZONE -- which a 12.1+ client also sends at login, pinned
# to its own UTC offset. PostgreSQL's own spelling of an offset is POSIX and
# INVERTS the sign (`SET TIME ZONE '+05:30'` runs the session at -05:30), so an
# offset is set as an explicit POSIX spec instead. The zone is also kept as
# Oracle spelled it, which is what SESSIONTIMEZONE reports back. The pattern
# admits no quote, so the zone is safe to inline as a literal.
_ALTER_SESSION_TIME_ZONE = re.compile(
    r"\s*ALTER\s+SESSION\s+SET\s+TIME_ZONE\s*=\s*'([^']+)'\s*;?\s*$", re.IGNORECASE
)
_TZ_OFFSET = re.compile(r'([+-])(\d{1,2}):(\d{2})$')


def _translate_time_zone(zone: str) -> str:
    zone = zone.strip()
    m = _TZ_OFFSET.match(zone)
    if m:
        sign, hh, mm = m.group(1), int(m.group(2)), m.group(3)
        zone = f'{sign}{hh:02d}:{mm}'
        flipped = '-' if sign == '+' else '+'
        posix = f'<{zone}>{flipped}{hh:02d}:{mm}'
    else:
        posix = zone  # a region name, e.g. Europe/Moscow, means the same in both
    # A DO block, not a SELECT: ALTER SESSION is not a query, and answering it
    # with a row failed the reference thin client (#1153).
    return (
        f"DO $$ BEGIN PERFORM set_config('TimeZone', '{posix}', false); "
        f"PERFORM set_config('seerdb.time_zone', '{zone}', false); END $$"
    )


_CREATE_USER = re.compile(r'\s*CREATE\s+USER\s+"?(\w+)"?\b', re.IGNORECASE)
_CREATE_INDEX_QUALIFIED = re.compile(
    r'(\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+)"?\w+"?\.("?\w+"?\s+ON\s+.*)$',
    re.IGNORECASE | re.DOTALL,
)
_ADMIN_NOOP = re.compile(
    r'\s*(ALTER\s+USER|GRANT|REVOKE|ALTER\s+SESSION|CREATE\s+ROLE|DROP\s+USER)\b',
    re.IGNORECASE,
)


# The schemas every session resolves unqualified names through, after the
# session's own. `pg_catalog` is named LAST on purpose. A search_path that does
# not name it has PostgreSQL search it FIRST, and then its built-ins beat orafce
# wherever both define a function with the same signature: TO_DATE came back as
# PostgreSQL's date-only one, and every time of day was silently dropped
# (#1305). Named after `oracle`, orafce's Oracle versions win, as orafce's own
# documentation sets it up.
_SEARCH_PATH_TAIL: Final = 'public, sys, oracle, pg_catalog'

# orafce's TO_DATE refuses a date before 1100-03-01 (or 1582-10-05 for 'J'),
# because it cannot VERIFY such dates against Oracle, not because Oracle
# refuses them: Oracle takes them back to 4712 BC. With orafce's to_date the one
# in use (above), that refusal reached clients as an error Oracle never raises,
# so the Mirror turns it off for its sessions. It is orafce's own setting, and
# a PostgreSQL without orafce accepts it as a harmless placeholder.
_ORAFCE_SESSION_SETTINGS_BASE: Final = (
    'SET orafce.oracle_compatibility_date_limit = off'
)

# A session's NLS_DATE_FORMAT (#1616): orafce's one-argument TO_CHAR and TO_DATE
# follow orafce.nls_date_format, and seerdb.nls_date_format keeps the format as
# Oracle spells it, for NLS_SESSION_PARAMETERS. PostgreSQL's formats have no RR
# or RRRR -- printed, they are the letters -- so the setting takes the YY /
# YYYY that Oracle prints for them.
_ALTER_SESSION_NLS_DATE_FORMAT = re.compile(
    r"\s*ALTER\s+SESSION\s+SET\s+NLS_DATE_FORMAT\s*=\s*'((?:[^']|'')*)'\s*;?\s*$",
    re.IGNORECASE,
)
_RR_YEAR = re.compile(r'RR(RR)?', re.IGNORECASE)


def _nls_date_format_settings(oracle_format: str) -> str:
    printed = _RR_YEAR.sub(lambda m: 'YYYY' if m.group(1) else 'YY', oracle_format)
    quoted = oracle_format.replace("'", "''")
    printed = printed.replace("'", "''")
    return (
        f"SET orafce.nls_date_format = '{printed}'; "
        f"SET seerdb.nls_date_format = '{quoted}'"
    )


# Oracle's own default, which every session starts with.
_ORAFCE_SESSION_SETTINGS: Final = (
    f'{_ORAFCE_SESSION_SETTINGS_BASE}; {_nls_date_format_settings("DD-MON-RR")}'
)


# GRANT / REVOKE of privileges ON an object (#1621): what sys.ora_grants keeps.
# A system privilege or a role (GRANT CREATE SESSION TO u) names no ON, and
# stays the no-op every GRANT was.
_OBJECT_GRANT = re.compile(
    r'(?is)\s*(GRANT|REVOKE)\s+(.+?)\s+ON\s+((?:"[^"]+"|[\w$#]+)'
    r'(?:\s*\.\s*(?:"[^"]+"|[\w$#]+))?)\s+(TO|FROM)\s+(.+?)'
    r'(?:\s+WITH\s+(?:GRANT|HIERARCHY)\s+OPTION)?\s*;?\s*$'
)


def _oracle_name(name: str) -> str:
    # An identifier as Oracle stores it: a quoted one verbatim, else upper case.
    name = name.strip()
    return name[1:-1] if name.startswith('"') else name.upper()


def _object_grant(match: re.Match[str]) -> str:
    # The rows a GRANT adds to sys.ora_grants, or a REVOKE takes away (#1621):
    # one per privilege and grantee. ALL [PRIVILEGES] is kept as ALL.
    (verb, privileges, target, _to, grantees) = match.groups()
    parts = [p for p in re.split(r'\s*\.\s*', target.strip())]
    owner_sql = (
        sql.Literal(_oracle_name(parts[0])).as_string()
        if len(parts) == 2
        else 'sys.ora_owner(current_schema())'
    )
    name = sql.Literal(_oracle_name(parts[-1])).as_string()
    privs = [
        'ALL' if p.strip().upper().startswith('ALL') else p.strip().upper()
        for p in privileges.split(',')
    ]
    users = [_oracle_name(g) for g in grantees.split(',')]
    rows = [
        f'({owner_sql}, {name}, {sql.Literal(p).as_string()}, '
        f'{sql.Literal(u).as_string()})'
        for p in privs
        for u in users
    ]
    if verb.upper() == 'GRANT':
        return (
            'INSERT INTO sys.ora_grants (owner, object_name, privilege, grantee) '
            f'VALUES {", ".join(rows)} ON CONFLICT DO NOTHING'
        )
    return (
        'DELETE FROM sys.ora_grants WHERE (owner, object_name, privilege, grantee) '
        f'IN ({", ".join(rows)})'
    )


# CREATE / DROP EDITION and ALTER SESSION SET EDITION (#1662).
_EDITION_NAME = r'("[^"]+"|[\w$#]+)'
_CREATE_EDITION = re.compile(
    rf'(?is)\s*CREATE\s+EDITION\s+{_EDITION_NAME}(?:\s+AS\s+CHILD\s+OF\s+{_EDITION_NAME})?'
    r'\s*;?\s*$'
)
_DROP_EDITION = re.compile(
    rf'(?is)\s*DROP\s+EDITION\s+{_EDITION_NAME}(?:\s+CASCADE)?\s*;?\s*$'
)
_ALTER_SESSION_EDITION = re.compile(
    rf'(?is)\s*ALTER\s+SESSION\s+SET\s+EDITION\s*=\s*{_EDITION_NAME}\s*;?\s*$'
)


def _edition_statement(sql: str) -> str | None:
    # An edition statement as PL/pgSQL (#1662): Oracle's errors raised as its
    # ORA text, which names the code. None for any other statement.
    m = _CREATE_EDITION.match(sql)
    if m:
        name = _sql_text(_oracle_name(m.group(1)))
        parent = _sql_text(_oracle_name(m.group(2)) if m.group(2) else 'ORA$BASE')
        return (
            f'DO $$ BEGIN IF EXISTS (SELECT 1 FROM sys.ora_editions WHERE name = {name}) '
            "THEN RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = "
            "'ORA-00955: name is already used by an existing object'; END IF; "
            f'INSERT INTO sys.ora_editions VALUES ({name}, {parent}); END $$'
        )
    m = _DROP_EDITION.match(sql)
    if m:
        name = _sql_text(_oracle_name(m.group(1)))
        return (
            f'DO $$ BEGIN DELETE FROM sys.ora_editions WHERE name = {name} '
            "AND name <> 'ORA$BASE'; IF NOT FOUND THEN RAISE EXCEPTION USING "
            "ERRCODE = 'P0001', MESSAGE = 'ORA-38802: edition does not exist'; "
            'END IF; END $$'
        )
    m = _ALTER_SESSION_EDITION.match(sql)
    if m:
        name = _sql_text(_oracle_name(m.group(1)))
        return f'DO $$ BEGIN PERFORM sys.ora_set_edition({name}); END $$'
    return None


# CREATE [OR REPLACE] DIRECTORY name AS 'path' and DROP DIRECTORY name (#1668).
_CREATE_DIRECTORY = re.compile(
    rf'(?is)\s*CREATE\s+(OR\s+REPLACE\s+)?DIRECTORY\s+{_EDITION_NAME}\s+AS\s+'
    r"'((?:[^']|'')*)'\s*;?\s*$"
)
_DROP_DIRECTORY = re.compile(rf'(?is)\s*DROP\s+DIRECTORY\s+{_EDITION_NAME}\s*;?\s*$')


def _directory_statement(sql: str) -> str | None:
    # A DIRECTORY statement as PL/pgSQL (#1668), Oracle's errors raised as their
    # ORA text. None for any other statement.
    m = _CREATE_DIRECTORY.match(sql)
    if m:
        name = _sql_text(_oracle_name(m.group(2)))
        path = _sql_text(m.group(3).replace("''", "'"))
        if m.group(1):
            return (
                f'INSERT INTO sys.ora_directories VALUES ({name}, {path}) '
                'ON CONFLICT (name) DO UPDATE SET path = EXCLUDED.path'
            )
        return (
            f'DO $$ BEGIN IF EXISTS (SELECT 1 FROM sys.ora_directories WHERE name = {name}) '
            "THEN RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = "
            "'ORA-00955: name is already used by an existing object'; END IF; "
            f'INSERT INTO sys.ora_directories VALUES ({name}, {path}); END $$'
        )
    m = _DROP_DIRECTORY.match(sql)
    if m:
        name = _oracle_name(m.group(1))
        return (
            f'DO $$ BEGIN DELETE FROM sys.ora_directories WHERE name = {_sql_text(name)}; '
            "IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = "
            f'{_sql_text(f"ORA-04043: object {name} does not exist")}; END IF; END $$'
        )
    return None


def _sql_text(value: str) -> str:
    # A Python string as a PostgreSQL string literal.
    return "'" + value.replace("'", "''") + "'"


def _translate_admin(sql: str) -> str:
    m = _OBJECT_GRANT.match(sql)
    if m:
        return _object_grant(m)
    edition = _edition_statement(sql)
    if edition is not None:
        return edition
    directory = _directory_statement(sql)
    if directory is not None:
        return directory
    m = _ALTER_SESSION_SCHEMA.match(sql)
    if m:
        return f'SET search_path TO {m.group(1).lower()}, {_SEARCH_PATH_TAIL}'
    m = _ALTER_SESSION_TIME_ZONE.match(sql)
    if m:
        return _translate_time_zone(m.group(1))
    m = _ALTER_SESSION_NLS_DATE_FORMAT.match(sql)
    if m:
        return _nls_date_format_settings(m.group(1).replace("''", "'"))
    m = _CREATE_USER.match(sql)
    if m:
        return f'CREATE SCHEMA IF NOT EXISTS {m.group(1).lower()}'
    m = _CREATE_INDEX_QUALIFIED.match(sql)
    if m:
        # Oracle allows a schema-qualified index name (CREATE INDEX s.i ON s.t);
        # PostgreSQL puts the index in the table's schema and rejects the prefix.
        return m.group(1) + m.group(2)
    if _ADMIN_NOOP.match(sql):
        # No PostgreSQL equivalent: succeed and do nothing -- and return nothing.
        # A `SELECT 1` here answered a GRANT or an ALTER SESSION with a row, which
        # a real server never does; seerdb's client let it pass, but the
        # reference thin client decoded the row against a statement it expected
        # none from and failed with a TypeError.
        return _NO_OP
    return _with_session_stats(sql)


# V$SESSTAT named in a statement, bare or as SYS.V$SESSTAT (#1324).
_V_SESSTAT = re.compile(r'(?<![\w$"])(?:sys\.)?v\$sesstat\b', re.IGNORECASE)


def _with_session_stats(sql: str) -> str:
    # A statement reading V$SESSTAT reads a snapshot of every session's
    # statistics as they stand now, taken from the Mirror (seerdb.server.stats)
    # and written into the statement: the counts live in the Mirror process,
    # where the database cannot see them (#1324).
    if not _V_SESSTAT.search(sql):
        return sql
    rows = [
        [sid, stats.STATISTIC_NUMBERS[name], value]
        for sid, counts in stats.snapshot().items()
        for name, value in counts.items()
        if name in stats.STATISTIC_NUMBERS
    ]
    return _V_SESSTAT.sub(f"sys.ora_sesstat('{json.dumps(rows)}')", sql)


# A statement PostgreSQL runs to no effect and answers with no result set.
_NO_OP = 'DO $$ BEGIN END $$'


# Oracle's negative / unbounded / caching keywords in CREATE/ALTER SEQUENCE are
# single words (NOMINVALUE, NOMAXVALUE, NOCYCLE, NOCACHE); PostgreSQL spells the
# first three as two words and has no NOCACHE (its minimum cache is 1). ORDER /
# NOORDER is an Oracle RAC ordering hint PostgreSQL has no equal for, so it is
# dropped. MINVALUE/MAXVALUE/CYCLE/CACHE/START WITH/INCREMENT BY are shared.
_IS_SEQUENCE_DDL = re.compile(r'\s*(?:CREATE|ALTER)\s+SEQUENCE\b', re.IGNORECASE)
_SEQUENCE_KEYWORD_REWRITES = [
    (re.compile(r'\bNOMINVALUE\b', re.IGNORECASE), 'NO MINVALUE'),
    (re.compile(r'\bNOMAXVALUE\b', re.IGNORECASE), 'NO MAXVALUE'),
    (re.compile(r'\bNOCYCLE\b', re.IGNORECASE), 'NO CYCLE'),
    (re.compile(r'\bNOCACHE\b', re.IGNORECASE), 'CACHE 1'),
    (re.compile(r'\bNOORDER\b', re.IGNORECASE), ''),
    (re.compile(r'\bORDER\b', re.IGNORECASE), ''),
]


# CREATE OR REPLACE VIEW whose columns change. Oracle replaces the view whatever
# its new columns; PostgreSQL's OR REPLACE refuses to change a column's type, or
# to drop or rename one (42P16, invalid_table_definition) -- and the reference
# thin client's suite redefines one view with a different type per test. So the
# replacement is tried as written and, on that refusal alone, the view is
# dropped and created afresh. A plain DROP, not CASCADE: a view other views
# depend on still refuses loudly rather than taking them with it.
_CREATE_OR_REPLACE_VIEW = re.compile(
    r'\s*CREATE\s+OR\s+REPLACE\s+(?:(?:NO)?FORCE\s+)?VIEW\s+([\w."$#]+)',
    re.IGNORECASE,
)
_VIEW_BODY_QUOTE = '$seerdb_view$'
# Any CREATE VIEW, whose columns take what sys.ora_columns has for the columns
# they come from (#1425).
_CREATE_VIEW_NAME = re.compile(
    r'\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:NO)?FORCE\s+)?VIEW\s+([\w."$#]+)',
    re.IGNORECASE,
)


def _translate_replace_view(sql: str) -> str | None:
    match = _CREATE_OR_REPLACE_VIEW.match(sql)
    if match is None or _VIEW_BODY_QUOTE in sql:
        return None
    name = match.group(1)
    body = _CREATE_OR_REPLACE_VIEW.sub(f'CREATE OR REPLACE VIEW {name}', sql, count=1)
    quote = _VIEW_BODY_QUOTE
    return (
        f'DO $$ BEGIN EXECUTE {quote}{body}{quote}; '
        'EXCEPTION WHEN invalid_table_definition THEN '
        f'EXECUTE {quote}DROP VIEW {name}{quote}; EXECUTE {quote}{body}{quote}; '
        'END $$'
    )


# CREATE OR REPLACE TYPE (#1197). PostgreSQL has no such statement, so the old
# type is dropped and the new one created, translated exactly as a plain CREATE
# TYPE is. No CASCADE: Oracle refuses to replace a type another type or a table
# depends on, even with the same spec (ORA-02303), and so does the drop. The
# statement savepoint undoes the drop if the create then fails. `... FORCE AS`
# is not matched, and a change of kind (OBJECT to VARRAY), which Oracle refuses
# with ORA-06545, is replaced here.
_CREATE_OR_REPLACE_TYPE = re.compile(
    r'(\s*CREATE)\s+OR\s+REPLACE\s+(TYPE\s+([\w."$#]+)\s+AS\b)', re.IGNORECASE
)
# Oracle REFs (#1127). An Oracle object table gives every row a hidden object
# id, and a REF names the table and that id. PostgreSQL's typed tables cannot
# take a column beyond their type's, and a row's physical address (ctid) moves on
# UPDATE / VACUUM FULL, so an object table becomes an ordinary table of the
# type's columns plus a hidden `sys_nc_oid$` uuid -- Oracle's own name for the
# column -- and is recorded in sys.ora_object_tables with its type. Each object
# type gets a companion `<type>$ref` composite (table oid, row id) and an
# overloaded sys.deref() returning the object, so `DEREF(r).attr` resolves by
# the stable id wherever the REF came from.
_CREATE_OBJECT_TABLE = re.compile(
    r'\s*CREATE\s+TABLE\s+([\w."$#]+)\s+OF\s+([\w."$#]+)\s*$', re.IGNORECASE
)
# `DROP TYPE name [FORCE]`: what was made with the type depends on it, so it goes
# first, or PostgreSQL refuses the drop.
_DROP_TYPE = re.compile(
    r'\s*DROP\s+TYPE\s+([\w."$#]+)(?:\s+FORCE)?\s*;?\s*$', re.IGNORECASE
)
_TYPE_NAME_OF_CREATE = re.compile(
    r'\s*CREATE\s+(?:OR\s+REPLACE\s+)?TYPE\s+([\w."$#]+)', re.IGNORECASE
)
_OBJECT_ID_COLUMN = '"sys_nc_oid$"'


def _ref_type_name(type_name: str) -> str:
    # `<type>$ref`, inside the quotes of a quoted name so the case survives.
    if type_name.endswith('"'):
        return type_name[:-1] + '$ref"'
    return f'{type_name}$ref'


def _object_type_companions(type_name: str) -> str:
    ref = _ref_type_name(type_name)
    return (
        f'; CREATE TYPE {ref} AS (tab oid, id uuid)'
        f'; CREATE FUNCTION sys.deref(r {ref}) RETURNS {type_name} LANGUAGE sql '
        f"STABLE AS $$ SELECT sys.ora_deref_row(r.tab, r.id, '{type_name}'::regtype)"
        f'::{type_name} $$'
    )


def _drop_type(type_name: str, *, if_exists: bool = False) -> str:
    # A type's drop, what was made with it first: its REF companions -- the
    # deref() function returns the type -- and a collection type's constructors,
    # the functions named as the type and returning it, never a user's other
    # functions (#1206). Either would keep the type from being dropped or
    # replaced. No CASCADE: a REF column of the type still holds it, and Oracle
    # refuses that drop too (ORA-02303).
    ref = _ref_type_name(type_name)
    literal = type_name.replace("'", "''")
    exists = ' IF EXISTS' if if_exists else ''
    return (
        f"DO $$ BEGIN IF to_regtype('{ref}') IS NOT NULL THEN "
        f'DROP FUNCTION sys.deref({ref}); DROP TYPE {ref}; END IF; END $$'
        '; DO $$ DECLARE f regprocedure; BEGIN FOR f IN SELECT p.oid::regprocedure '
        f"FROM pg_proc p WHERE p.prorettype = to_regtype('{literal}') "
        f"AND p.oid::regprocedure::text LIKE split_part(to_regtype('{literal}')::text, "
        "'(', 1) || '(%' LOOP EXECUTE 'DROP FUNCTION ' || f; END LOOP; END $$"
        f'; DROP TYPE{exists} {type_name}'
    )


def _translate_object_ddl(sql: str) -> str | None:
    """CREATE TABLE ... OF and DROP TYPE, for REFs (#1127); None for other SQL."""
    table = _CREATE_OBJECT_TABLE.match(sql)
    if table:
        name, type_name = table.groups()
        return (
            f'CREATE TABLE {name} (LIKE {type_name}, '
            f'{_OBJECT_ID_COLUMN} uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE)'
            '; DELETE FROM sys.ora_object_tables o WHERE NOT EXISTS '
            '(SELECT 1 FROM pg_class c WHERE c.oid = o.relid)'
            f"; INSERT INTO sys.ora_object_tables VALUES ('{name}'::regclass, "
            f"'{type_name}'::regtype) ON CONFLICT (relid) DO UPDATE "
            'SET typ = excluded.typ'
        )
    drop = _DROP_TYPE.match(sql)
    if drop:
        return _drop_type(drop.group(1))
    return None


# A call with one string literal, `name('x')`: PostgreSQL reads it as a cast to
# `name` when a type of that name exists, before it looks for a function, so a
# one-element collection constructor became a malformed array literal (#1435).
_ONE_LITERAL_CALL = re.compile(
    r"(?<![\w$#.\"])((?:[A-Za-z_][\w$#]*\.)?[A-Za-z_][\w$#]*)\s*\(\s*([Nn]?'(?:[^']|'')*')\s*\)"
)


def _spell_one_element_constructors(sql: str, collections: frozenset[str]) -> str:
    """`name('x')` for a collection type `name`, spelt so PostgreSQL calls the
    constructor rather than casting the literal (#1435). Names compare in lower
    case, schema-qualified or not."""
    if not collections or "'" not in sql:
        return sql

    def respell(match: re.Match) -> str:
        name, literal = match.group(1), match.group(2)
        if name.lower() not in collections:
            return match.group(0)
        return f'{name}(VARIADIC ARRAY[{literal}])'

    return _ONE_LITERAL_CALL.sub(respell, sql)


def _collection_constructors(name: str, element: str) -> str:
    """The constructors Oracle gives a collection type (#1206): `name(e1, e2, …)`
    and the empty `name()`, each returning the type, so a VARRAY's bound still
    applies to what they build."""
    return (
        f'; CREATE OR REPLACE FUNCTION {name}(VARIADIC {element}[]) RETURNS {name} '
        f'LANGUAGE sql IMMUTABLE AS $$ SELECT $1::{name} $$'
        f'; CREATE OR REPLACE FUNCTION {name}() RETURNS {name} '
        f"LANGUAGE sql IMMUTABLE AS $$ SELECT '{{}}'::{element}[]::{name} $$"
    )


def _object_constructor(name: str) -> str:
    """The constructor Oracle gives an object type (#1206): `name(a1, a2, …)`,
    one argument per attribute in order, returning the object. It is built
    from the catalog once the type exists, so the attribute types are the ones
    the type was given."""
    literal = name.replace("'", "''")
    return (
        '; DO $$ DECLARE t regtype := ' + f"to_regtype('{literal}'); "
        'a text; v text; BEGIN '
        "SELECT string_agg(format_type(atttypid, NULL), ', ' ORDER BY attnum), "
        "string_agg('$' || row_number, ', ' ORDER BY attnum) INTO a, v FROM ("
        'SELECT atttypid, attnum, row_number() OVER (ORDER BY attnum) '
        'FROM pg_attribute WHERE attrelid = (SELECT typrelid FROM pg_type '
        'WHERE oid = t) AND attnum > 0 AND NOT attisdropped) x; '
        "EXECUTE format('CREATE OR REPLACE FUNCTION %s(%s) RETURNS %s LANGUAGE sql "
        "IMMUTABLE AS $f$ SELECT ROW(%s)::%s $f$', t, a, t, v, t); END $$"
    )


# What each argument of DBMS_PICKLER.GET_TYPE_SHAPE is, in order (#1542).
_TYPE_SHAPE_ROLES: Final = (
    'full_name',
    'oid',
    'version',
    'tds',
    'instantiable',
    'supertype_owner',
    'supertype_name',
    'attrs_rc',
    'subtype_rc',
)
_TYPE_SHAPE_CALL = re.compile(
    r'(?:(:\w+|:"[^"]+")\s*:=\s*)?\bdbms_pickler\s*\.\s*get_type_shape\s*\(',
    re.IGNORECASE,
)


def _type_shape_roles(sql: str) -> tuple[dict[str, str], str | None]:
    # Each bind of a GET_TYPE_SHAPE call by the argument it is (#1542): the
    # call's return target is ret_val, its arguments the roles above, in order;
    # and the type's name when the call gives it as a literal.
    call = _TYPE_SHAPE_CALL.search(sql)
    if call is None:
        return ({}, None)
    roles: dict[str, str] = {}
    if call.group(1):
        roles[call.group(1)[1:].strip('"').lower()] = 'ret_val'
    open_at = call.end() - 1
    literal: str | None = None
    for position, (start, end) in enumerate(
        _top_level_items(sql, open_at + 1, _matching_paren(sql, open_at))
    ):
        argument = sql[start:end].strip()
        if position >= len(_TYPE_SHAPE_ROLES):
            break
        if argument.startswith(':'):
            roles[argument[1:].strip('"').lower()] = _TYPE_SHAPE_ROLES[position]
        elif position == 0 and argument.startswith("'") and argument.endswith("'"):
            literal = argument[1:-1].replace("''", "'")
    return (roles, literal)


def _translate_ddl(sql: str) -> str:
    """Rewrite an Oracle ``CREATE TABLE`` / object ``CREATE TYPE`` to PostgreSQL:
    map the column/attribute types and drop the clauses PostgreSQL has no equal
    for (#500). Other SQL is returned unchanged."""
    purged = _DROP_TABLE_PURGE.match(sql)
    if purged:
        return purged.group(1)
    altered = _translate_alter_columns(sql)
    if altered is not None:
        return altered
    if _IS_SEQUENCE_DDL.match(sql):
        for pattern, replacement in _SEQUENCE_KEYWORD_REWRITES:
            sql = pattern.sub(replacement, sql)
        return re.sub(r'\s{2,}', ' ', sql).rstrip()
    replaced = _CREATE_OR_REPLACE_TYPE.match(sql)
    if replaced:
        plain = _translate_ddl(
            f'{replaced.group(1)} {replaced.group(2)}{sql[replaced.end() :]}'
        )
        return f'{_drop_type(replaced.group(3), if_exists=True)}; {plain}'
    varray = _CREATE_TYPE_VARRAY.match(sql)
    if varray:
        name, bound, element = varray.groups()
        for pattern, replacement in _DDL_TYPE_REWRITES:
            element = pattern.sub(replacement, element)
        return (
            f'CREATE DOMAIN {name} AS {element}[] '
            f'CHECK (VALUE IS NULL OR array_length(VALUE, 1) <= {bound})'
        ) + _collection_constructors(name, element)
    nested = _CREATE_TYPE_TABLE_OF.match(sql)
    if nested:
        name, element = nested.groups()
        for pattern, replacement in _DDL_TYPE_REWRITES:
            element = pattern.sub(replacement, element)
        return f'CREATE DOMAIN {name} AS {element}[]' + _collection_constructors(
            name, element
        )
    object_ddl = _translate_object_ddl(sql)
    if object_ddl is not None:
        return object_ddl
    if _CREATE_TYPE_OBJECT.match(sql):
        # `... AS OBJECT (attrs)` → `... AS (attrs)`, then map the attribute types
        # (NUMBER → numeric, VARCHAR2(n) → varchar(n), …) the same way as a table.
        out = _CREATE_TYPE_OBJECT.sub(r'\1', sql, count=1)
        # A DATE attribute is the ora_date domain, as a table's DATE column is,
        # so it describes as DATE rather than TIMESTAMP (#1316, #1548).
        out = _DDL_DATE_COLUMN.sub(_DATE_TYPE, out)
        # A FLOAT / REAL / DOUBLE PRECISION attribute is a NUMBER of binary
        # precision, numeric as a table's column is (#1384, #1423); before the
        # type rewrites, which make BINARY_FLOAT a real.
        out = _DDL_FLOAT_COLUMN.sub('numeric', out)
        for pattern, replacement in _DDL_TYPE_REWRITES:
            out = pattern.sub(replacement, out)
        named = _TYPE_NAME_OF_CREATE.match(sql)
        if named is None:
            return out
        return (
            out.rstrip().rstrip(';')
            + _object_type_companions(named.group(1))
            + _object_constructor(named.group(1))
        )
    view = _translate_replace_view(sql)
    if view is not None:
        return view
    if not _IS_CREATE_TABLE.match(sql):
        return sql
    out = _DDL_GLOBAL_TEMPORARY.sub('TEMPORARY', _with_raw_length_checks(sql))
    out = _DDL_ORG_INDEX.sub('', out)
    out = _strip_nested_table_storage(out)
    out = _DDL_COMPRESSION.sub(')', out)
    table = _CREATE_TABLE_NAME.match(sql)
    if table is None or not _DDL_FLOAT_COLUMN.search(out):
        return _translate_column_types(out)
    return _translate_column_types(out).rstrip().rstrip(';') + _float_rounding(
        table.group(1)
    )


def _float_rounding(table: str) -> str:
    # The trigger that rounds a FLOAT(b) column's values as Oracle stores them
    # (#1422), for a table that has or gains one. It reads which columns are
    # FLOAT, and their b, from sys.ora_columns, so one per table serves them all
    # and a column no longer FLOAT is passed over.
    return (
        '; CREATE OR REPLACE TRIGGER ora_float_round BEFORE INSERT OR UPDATE '
        f'ON {table} FOR EACH ROW EXECUTE FUNCTION sys.ora_float_round()'
    )


def _oracle_identifier_text(name: str) -> str:
    # A name as Oracle stores it, for a message: quoted as written, else upper.
    name = name.strip()
    return name[1:-1] if name.startswith('"') else name.upper()


def _raw_check_name(column: str) -> str:
    # The constraint holding a RAW(n) column to n bytes, named for the column so
    # a MODIFY can find it (#1415).
    pg = column[1:-1] if column.startswith('"') else column.lower()
    return '"' + f'ora_raw_len_{pg}'[:63].replace('"', '""') + '"'


def _raw_length_check(table: str, column: str, length: int) -> str:
    table_text = _oracle_identifier_text(table.split('.')[-1]).replace("'", "''")
    column_text = _oracle_identifier_text(column).replace("'", "''")
    return (
        f'CONSTRAINT {_raw_check_name(column)} CHECK (sys.ora_raw_fits('
        f"{column}, {length}, '{table_text}', '{column_text}'))"
    )


def _with_raw_length_checks(sql: str) -> str:
    # A CREATE TABLE's RAW(n) columns, each with the check that holds it to n
    # bytes (#1415).
    parsed = _ddl_column_spans(sql)
    if parsed is None:
        return sql
    (table, spans, _modify) = parsed
    for start, end in sorted(spans, reverse=True):
        name = _COLUMN_NAME.match(sql, start, end)
        if name is None:
            continue
        raw = _RAW_DECLARED.match(sql, name.end(), end)
        if raw is None:
            continue
        check = _raw_length_check(table, name.group(1), int(raw.group(1)))
        stop = end
        while stop > start and sql[stop - 1].isspace():
            stop -= 1
        sql = f'{sql[:stop]} {check}{sql[stop:]}'
    return sql


def _translate_column_types(text: str) -> str:
    # A table's column types, Oracle's to PostgreSQL's: CREATE TABLE's columns
    # and those an ALTER TABLE adds or modifies (#1414).
    text = _DDL_DATE_COLUMN.sub(_DATE_TYPE, text)
    # Before the type rewrites: BINARY_FLOAT / BINARY_DOUBLE become real and
    # double precision there, which this would catch.
    text = _DDL_FLOAT_COLUMN.sub('numeric', text)
    for pattern, replacement in _DDL_TYPE_REWRITES:
        text = pattern.sub(replacement, text)
    return text


# ALTER TABLE t DROP (c1, c2): Oracle's column-list DROP (#1414).
_ALTER_TABLE_DROP_LIST = re.compile(
    rf'\s*ALTER\s+TABLE\s+{_TABLE_NAME}\s+DROP\s*\(', re.IGNORECASE
)
# An ADD item that is a table constraint, not a column.
_ADD_CONSTRAINT_ITEM = re.compile(
    r'\s*(?:CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK)\b', re.IGNORECASE
)
# A MODIFY item's trailing NOT NULL / NULL.
_TRAILING_NULLITY = re.compile(r'(?:^|\s)(NOT\s+NULL|NULL)\s*$', re.IGNORECASE)


def _translate_alter_columns(sql: str) -> str | None:
    """Oracle's ALTER TABLE column forms in PostgreSQL's spelling (#1414), or
    None for any other statement or a form this does not model.

    ``ADD (c1 t1, c2 t2)`` is ``ADD COLUMN c1 t1', ADD COLUMN c2 t2'``, the types
    translated as CREATE TABLE's are, and an ADD of a constraint stays one.
    ``MODIFY (c type DEFAULT x NOT NULL)`` is ``ALTER COLUMN c TYPE type',
    ALTER COLUMN c SET DEFAULT x, ALTER COLUMN c SET NOT NULL`` -- each part
    only where given, NULL dropping the constraint -- and ``DROP (c1, c2)`` is
    ``DROP COLUMN c1, DROP COLUMN c2``. A MODIFY with anything else in it (a
    named constraint, a CHECK) is not modelled and runs as written, to fail
    honestly.
    """
    dropped = _ALTER_TABLE_DROP_LIST.match(sql)
    if dropped is not None:
        open_at = dropped.end() - 1
        close = _matching_paren(sql, open_at)
        names = [sql[a:b].strip() for a, b in _top_level_items(sql, open_at + 1, close)]
        return f'ALTER TABLE {dropped.group(1)} ' + ', '.join(
            f'DROP COLUMN {name}' for name in names
        )
    parsed = _ddl_column_spans(sql)
    if parsed is None or _CREATE_TABLE_NAME.match(sql):
        return None
    (table, spans, modify) = parsed
    actions: list[str] = []
    for start, end in spans:
        item = sql[start:end].strip()
        if not item:
            continue
        if not modify:
            if _ADD_CONSTRAINT_ITEM.match(item):
                actions.append(f'ADD {item}')
            else:
                column = _translate_column_types(f'({item})')[1:-1]
                name = _COLUMN_NAME.match(item)
                raw = _RAW_DECLARED.match(item, name.end()) if name else None
                if name is not None and raw is not None:
                    # A RAW(n) column held to n bytes (#1415).
                    check = _raw_length_check(table, name.group(1), int(raw.group(1)))
                    column = f'{column} {check}'
                actions.append(f'ADD COLUMN {column}')
            continue
        name = _COLUMN_NAME.match(item)
        if name is None:
            return None
        modified = _modify_actions(table, name.group(1), item[name.end() :].strip())
        if modified is None:
            return None
        actions.extend(modified)
    if not actions:
        return None
    altered = f'ALTER TABLE {table} ' + ', '.join(actions)
    if _DDL_FLOAT_COLUMN.search(sql[spans[0][0] : spans[-1][1]]):
        altered += _float_rounding(table)
    return altered


def _modify_actions(table: str, column: str, rest: str) -> list[str] | None:
    # One MODIFY item's ALTER COLUMN actions (#1414): its type, default and
    # nullity, each where given; None for a part this does not model.
    nullity = None
    default = None
    at = re.search(r'\bDEFAULT\b', rest, re.IGNORECASE)
    head = rest if at is None else rest[: at.start()]
    tail = '' if at is None else rest[at.end() :].strip()
    trailing = _TRAILING_NULLITY.search(tail if at is not None else head)
    if trailing is not None and (at is None or tail[: trailing.start()].strip()):
        nullity = trailing.group(1).upper()
        if at is None:
            head = head[: trailing.start()]
        else:
            tail = tail[: trailing.start()]
    if at is not None:
        default = tail.strip()
    type_text = head.strip()
    if re.search(
        r'\b(?:CONSTRAINT|CHECK|UNIQUE|PRIMARY|REFERENCES)\b', rest, re.IGNORECASE
    ):
        return None
    actions = []
    if type_text:
        typed = _translate_column_types(f'({column} {type_text})')[1:-1]
        actions.append(f'ALTER COLUMN {column} TYPE {typed[len(column) :].strip()}')
        # A RAW(n) column's check follows its n; another type has none (#1415).
        actions.append(f'DROP CONSTRAINT IF EXISTS {_raw_check_name(column)}')
        raw = _RAW_DECLARED.match(type_text)
        if raw is not None:
            actions.append(f'ADD {_raw_length_check(table, column, int(raw.group(1)))}')
    if default is not None:
        actions.append(f'ALTER COLUMN {column} SET DEFAULT {default}')
    if nullity is not None:
        verb = 'SET' if nullity.startswith('NOT') else 'DROP'
        actions.append(f'ALTER COLUMN {column} {verb} NOT NULL')
    return actions


# `SELECT REF(<alias>) FROM <table> <alias> [rest]` — the object-REF fetch (#139).
# PostgreSQL has no REF, so the row's identity (its ctid) stands in for the opaque
# locator and the referenced object type is recovered from the typed table's
# catalog entry (pg_class.reloftype). Only this single-REF-column shape is handled
# (all the suite issues); anything else falls through to the ordinary path.
_REF_SELECT = re.compile(
    r'\s*SELECT\s+REF\s*\(\s*(\w+)\s*\)\s+FROM\s+([\w.]+)\s+(\w+)\b(.*)$',
    re.IGNORECASE | re.DOTALL,
)


def _xmlelement_name(match: re.Match[str]) -> str:
    # XMLElement's name as PostgreSQL's NAME takes it: quoted as written, or an
    # unquoted one upper-cased, as Oracle folds it (#1554).
    name = match.group(1)
    return 'XMLELEMENT(NAME ' + (name if name.startswith('"') else f'"{name.upper()}"')


# Oracle SQL functions / literal idioms → PostgreSQL (#502). Each is a function
# call or a literal keyword the suite uses; the rewrites are anchored on the
# call's `(` or a word boundary, so ordinary identifiers are left alone. Applied
# to every statement (a DEFAULT SYSDATE in DDL is rewritten too).
_IDIOM_REWRITES: list[
    tuple[str, re.Pattern[str], str | Callable[[re.Match[str]], str]]
] = [
    # (HEXTORAW, RAWTOHEX, EMPTY_CLOB / EMPTY_BLOB and FROM_TZ are installed as
    # real PostgreSQL functions — see _HELPER_FUNCTIONS_DDL / __init__ — so their
    # call sites resolve directly and need no rewrite here. TO_CHAR,
    # TO_DATE, ADD_MONTHS, INSTR, … come from the orafce extension the same way.
    # Only bare pseudo-constants and literal / clause shapes remain below.)
    # DELETE without FROM (#1407): Oracle takes `DELETE t [WHERE ...]`, PostgreSQL
    # only `DELETE FROM t`. Anchored at the statement's start, after an optional
    # hint, and only where a table name follows.
    (
        'delete-without-from',
        re.compile(r'(?is)^(\s*DELETE(?:\s+/\*.*?\*/)?)\s+(?!FROM\b)(?=[\w"])'),
        r'\1 FROM ',
    ),
    # A cursor's attributes in PL/SQL (#1608): c%FOUND / c%NOTFOUND are PL/pgSQL's
    # FOUND, which a FETCH sets -- and a DML statement or SELECT INTO, so
    # SQL%FOUND is the same. FOUND is the last statement's where Oracle keeps
    # one per cursor; they agree on a test right after the FETCH, the usual
    # place for one. c%ISOPEN is whether c's portal is open; SQL%ISOPEN is FALSE,
    # as in Oracle. %ROWCOUNT is not translated.
    (
        'cursor-attribute',
        re.compile(
            r'(?<![\w$#"])([A-Za-z_][\w$#]*|"[^"]*")\s*%\s*(FOUND|NOTFOUND|ISOPEN)\b',
            re.IGNORECASE,
        ),
        lambda m: (
            'FOUND'
            if m.group(2).upper() == 'FOUND'
            else '(NOT FOUND)'
            if m.group(2).upper() == 'NOTFOUND'
            else 'FALSE'
            if m.group(1).upper() == 'SQL'
            else f'(SELECT count(*) > 0 FROM pg_cursors WHERE name = {m.group(1)}::text)'
        ),
    ),
    # A COMMIT statement in a routine or a block (#1630) as a request that the
    # backend commit once the client's call has succeeded: PostgreSQL commits in
    # no function, nor in a procedure CALLed in an open transaction, and the
    # Mirror's always is. A COMMIT the client sends alone is no statement in a
    # block, and ON COMMIT follows no statement boundary.
    (
        'commit-in-block',
        re.compile(
            r'((?:;|\b(?:BEGIN|THEN|ELSE|LOOP)\b)\s*)COMMIT(?:\s+WORK)?\s*;',
            re.IGNORECASE,
        ),
        r'\1PERFORM sys.ora_commit_request();',
    ),
    # x IS [NOT] JSON (#1614) as sys.ora_is_json(x): PostgreSQL 16's own takes
    # text and bytea but not a CLOB's or BLOB's domain over them, and has no
    # Oracle's FORMAT JSON or STRICT / LAX. The test is PostgreSQL's, Oracle's
    # STRICT: Oracle's default, LAX, also takes an unquoted name or a
    # single-quoted string ({a:1}, {'a':1}), which this refuses.
    (
        'is-json',
        re.compile(
            r'((?:"[^"]*"|[A-Za-z_][\w$#]*)(?:\s*\.\s*(?:"[^"]*"|[A-Za-z_][\w$#]*))*'
            r'|\([^()]*\))\s+IS\s+(NOT\s+)?JSON\b(?:\s+FORMAT\s+JSON\b)?'
            r'(?:\s+(?:STRICT|LAX)\b)?',
            re.IGNORECASE,
        ),
        lambda m: f'{"NOT " if m.group(2) else ""}sys.ora_is_json({m.group(1)})',
    ),
    # NVL is the exception: orafce offers four overloads — nvl(anyelement,
    # anyelement), nvl(bigint, integer), nvl(integer, integer) and nvl(numeric,
    # integer) — and a PostgreSQL literal starts out as `unknown`, so a call with
    # bare literals (NVL(NULL, 'ok'), the most ordinary Oracle there is) matches
    # several candidates and the resolver refuses to pick. COALESCE is native,
    # accepts untyped literals, and for the two arguments NVL takes means exactly
    # the same thing — so sidestep overload resolution rather than adding a fifth
    # candidate to it (#819).
    ('nvl', re.compile(r'\bNVL\s*\(', re.IGNORECASE), 'COALESCE('),
    # RAISE_APPLICATION_ERROR(-20101, 'Test!') -- a user error, ORA-20000..20999
    # (#1323). PL/pgSQL has no such procedure; raise P0001 with the Oracle code as
    # the message's prefix, which _application_error reads back into the error
    # the client gets. A third argument (keep the error stack) has no counterpart.
    (
        'raise-application-error',
        re.compile(
            r'\braise_application_error\s*\(\s*([^,]+?)\s*,\s*(.+?)\s*'
            r'(?:,\s*(?:true|false)\s*)?\)\s*;',
            re.IGNORECASE | re.DOTALL,
        ),
        r"RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = "
        r"'ORA-' || lpad(abs((\1))::text, 5, '0') || ': ' || (\2);",
    ),
    # BINARY_DOUBLE/FLOAT special values → IEEE-754 float literals.
    (
        'binary-infinity',
        re.compile(r'\bbinary_(?:double|float)_infinity\b', re.IGNORECASE),
        "'Infinity'::float8",
    ),
    (
        'binary-nan',
        re.compile(r'\bbinary_(?:double|float)_nan\b', re.IGNORECASE),
        "'NaN'::float8",
    ),
    # A negative INTERVAL DAY TO SECOND literal. Oracle's leading `-` negates the
    # whole interval — INTERVAL '-1 02:03:04' DAY TO SECOND is -(1d 2h3m4s) — but
    # PostgreSQL applies the sign only to the field it prefixes (the days), leaving
    # the time part positive. Lift the inner `-` out to a unary minus on the whole
    # literal, which negates every field the way Oracle does (#520).
    (
        'negative-interval',
        re.compile(
            r"\bINTERVAL\s+'-([^']*)'\s+"
            r'(DAY(?:\s*\(\d+\))?\s+TO\s+SECOND(?:\s*\(\d+\))?)\b',
            re.IGNORECASE,
        ),
        r"- INTERVAL '\1' \2",
    ),
    # XMLElement("name", ...) / XMLElement(name, ...): PostgreSQL's XMLELEMENT
    # takes the name after the NAME keyword (#1554). Oracle folds an unquoted
    # name to upper case, as it does any identifier, where PostgreSQL would
    # fold it to lower; it goes quoted, upper-cased. One already spelt
    # `NAME x`, which Oracle takes too, and Oracle's EVALNAME are left alone.
    (
        'xmlelement-name',
        re.compile(
            r'\bXMLELEMENT\s*\(\s*(?!(?:NAME|EVALNAME)\b)("[^"]*"|[A-Za-z_][\w$#]*)',
            re.IGNORECASE,
        ),
        _xmlelement_name,
    ),
    # SYSDATE / SYSTIMESTAMP → the session clock (SYSDATE is to-the-second).
    (
        'systimestamp',
        re.compile(r'\bsystimestamp\b', re.IGNORECASE),
        'ora_systimestamp()',
    ),
    # DBMS_DEBUG_JDWP.CURRENT_SESSION_ID / _SERIAL are called without parentheses
    # in Oracle, which PostgreSQL would read as a column (#1355).
    (
        'dbms-debug-jdwp',
        re.compile(
            r'\bdbms_debug_jdwp\s*\.\s*(current_session_id|current_session_serial)\b'
            r'(?!\s*\()',
            re.IGNORECASE,
        ),
        r'dbms_debug_jdwp.\1()',
    ),
    # CURRENT_TIMESTAMP [(p)] is the session's TIMESTAMP WITH TIME ZONE (#1208).
    (
        'current-timestamp',
        re.compile(r'\bcurrent_timestamp\b(?:\s*\(\s*\d+\s*\))?', re.IGNORECASE),
        'ora_current_timestamp()',
    ),
    (
        'dbtimezone',
        re.compile(r'\bdbtimezone\b', re.IGNORECASE),
        f"'{_DB_TIME_ZONE_NAME}'::text",
    ),
    # CAST(x AS TIMESTAMP [(p)] WITH LOCAL TIME ZONE): the DDL type rewrite only
    # runs on DDL, so a query's cast is translated here (#1208).
    (
        'cast-local-time-zone',
        re.compile(
            r'\bAS\s+TIMESTAMP\s*(\(\s*\d+\s*\))?\s+WITH\s+LOCAL\s+TIME\s+ZONE\b',
            re.IGNORECASE,
        ),
        r'AS timestamptz\1',
    ),
    # SYSDATE is a DATE, so SYSDATE - d is a number of days (#1611).
    (
        'sysdate',
        re.compile(r'\bsysdate\b', re.IGNORECASE),
        f'localtimestamp(0)::{_DATE_TYPE}',
    ),
    # SESSIONTIMEZONE → the zone as ALTER SESSION spelled it, or, before any was
    # set, the session's current offset in Oracle's `+hh:mm` form: the zone a
    # TIMESTAMP is read in on its way into an LTZ value (#1208).
    (
        'sessiontimezone',
        re.compile(r'\bsessiontimezone\b', re.IGNORECASE),
        "coalesce(nullif(current_setting('seerdb.time_zone', true), ''), "
        "to_char(now(), 'TZH:TZM'))",
    ),
    # The ROWID pseudo-column → the row's ctid, in Oracle's extended form
    # (sys.ora_rowid). This one rewrite serves a SELECT (returns the str), a
    # `WHERE ROWID = :bind` (compares the bound text) and `SET col = ROWID`, and
    # it is the form cursor.lastrowid reports, so each can be fed to the other. The word boundary keeps it off ROWIDTOCHAR (no
    # boundary mid-token) and UROWID (a word char precedes ROWID). ctid is a
    # physical, *mutable* address — it changes on UPDATE / VACUUM FULL — so it is a
    # faithful row locator only within an unmodified snapshot, which is all the
    # read-then-bind suite needs; it is not a durable cross-transaction handle like
    # Oracle's ROWID (a real migration uses a surrogate identity key instead). An
    # index-organized table's ROWID is rewritten earlier, per session, from its
    # primary key (PostgresBackend._rewrite_iot_rowid), so this only sees heap
    # tables.
    ('rowid', re.compile(r'\bROWID\b', re.IGNORECASE), 'sys.ora_rowid(tableoid, ctid)'),
    # A BINARY_DOUBLE / BINARY_FLOAT numeric literal suffix (1234.5678d, 1.5f) —
    # PostgreSQL has no such suffix, so drop it. A decimal point is required so
    # this never touches an identifier or a plain integer.
    ('float-literal-suffix', re.compile(r'\b(\d+\.\d+)[dfDF]\b'), r'\1'),
    # Oracle's MINUS set operator is PostgreSQL's EXCEPT (#759, reflection uses it).
    ('minus', re.compile(r'\bMINUS\b', re.IGNORECASE), 'EXCEPT'),
    # Sequence pseudo-columns: Oracle's `seq.nextval` / `seq.currval` are
    # PostgreSQL's `nextval('seq')` / `currval('seq')` function calls. The captured
    # name (optionally schema-qualified) becomes the regclass argument; it is
    # created and referenced lower-case, so an unquoted regclass literal resolves.
    (
        'nextval',
        re.compile(r'\b([A-Za-z_][\w$#.]*)\.nextval\b', re.IGNORECASE),
        r"nextval('\1')",
    ),
    (
        'currval',
        re.compile(r'\b([A-Za-z_][\w$#.]*)\.currval\b', re.IGNORECASE),
        r"currval('\1')",
    ),
    # The SQL-standard OFFSET/FETCH the 12c dialect emits: PostgreSQL accepts only a
    # restricted expression before ROWS, so `OFFSET 1 + 2 ROWS` is a syntax error
    # while `OFFSET (1 + 2) ROWS` is fine. Wrap the operand in parentheses (a bare
    # literal or bind is already valid, and the extra parens are harmless there).
    (
        'offset-rows',
        re.compile(r'\bOFFSET\s+(.+?)\s+ROWS\b', re.IGNORECASE),
        r'OFFSET (\1) ROWS',
    ),
    (
        'fetch-first',
        re.compile(r'\bFETCH\s+(FIRST|NEXT)\s+(.+?)\s+ROWS\b', re.IGNORECASE),
        r'FETCH \1 (\2) ROWS',
    ),
    # A CAST to an Oracle string type in DML (CAST(x AS VARCHAR2(50 CHAR))): the
    # column-type rewrites only fire on CREATE TABLE, so translate the string type
    # and drop the CHAR/BYTE length qualifier here too. VARCHAR2 / NVARCHAR2 are
    # never valid identifiers, and the qualifier shape is specific, so this is safe
    # on any statement (a DDL CAST is already varchar by the time it reaches here).
    ('cast-nvarchar2', _NVARCHAR2_WORD, 'varchar'),
    ('cast-varchar2', _VARCHAR2_WORD, 'varchar'),
    ('char-byte-length', _CHAR_BYTE_LENGTH, r'(\1)'),
    # A CAST to an Oracle numeric or raw type in DML (CAST(:1 AS NUMBER(15))),
    # which PostgreSQL does not know (#1329). Anchored to `AS` and, for the types
    # that are not reserved words, to the cast's closing parenthesis, so an alias
    # is left alone.
    ('cast-number', re.compile(r'\bAS\s+NUMBER\b', re.IGNORECASE), 'AS numeric'),
    (
        'cast-raw',
        re.compile(r'\bAS\s+RAW\s*(?:\(\s*\d+\s*\))?', re.IGNORECASE),
        'AS bytea',
    ),
    (
        'cast-binary-float',
        re.compile(r'\bAS\s+BINARY_FLOAT(?=\s*\))', re.IGNORECASE),
        'AS real',
    ),
    (
        'cast-binary-double',
        re.compile(r'\bAS\s+BINARY_DOUBLE(?=\s*\))', re.IGNORECASE),
        'AS double precision',
    ),
]


# An Oracle TIMESTAMP literal carrying an explicit offset — TIMESTAMP '<ts> ±HH:MM'
# — is a TIMESTAMP WITH TIME ZONE value. PostgreSQL's `TIMESTAMP '…'` keyword
# parses as *without* time zone and silently drops the offset (wrong instant), so
# such a literal is rewritten to build the offset-preserving `ora_tstz` composite:
# the instant via `TIMESTAMPTZ '…'` (which does honour the offset) and the offset
# itself in seconds. A TIMESTAMP literal with no offset is an ordinary timestamp
# and is left untouched (#519).
_TSTZ_LITERAL = re.compile(r"\bTIMESTAMP\s*'([^']*)'", re.IGNORECASE)
_OFFSET_TAIL = re.compile(r'([+-])(\d{2}):(\d{2})\s*$')


def _tstz_literal_sub(match: 're.Match') -> str:
    content = match.group(1)
    tail = _OFFSET_TAIL.search(content)
    if tail is None:
        return match.group(0)  # a plain TIMESTAMP literal — not WITH TIME ZONE
    sign = -1 if tail.group(1) == '-' else 1
    seconds = sign * (int(tail.group(2)) * 3600 + int(tail.group(3)) * 60)
    return f"ROW(TIMESTAMPTZ '{content}', {seconds})::{_TSTZ_TYPE}"


# CONNECT BY -> WITH RECURSIVE (#760). Oracle's hierarchical query has no
# PostgreSQL keyword; the common single-table shape maps to a recursive CTE.
# Correct-or-passthrough: this only fires on a query containing CONNECT BY, and
# either produces a faithful WITH RECURSIVE for a shape it fully recognises or
# returns the query untouched (which then errors on PostgreSQL exactly as before)
# -- it never yields a wrong-but-successful result. The projection and FROM body
# are reused verbatim; only the clause structure, the single table, and the
# single-equality CONNECT BY are parsed. Unsupported and passed through: multiple
# tables / joins, a compound or non-equality CONNECT BY, SELECT *, ORDER SIBLINGS
# BY, and more than one distinct SYS_CONNECT_BY_PATH / CONNECT_BY_ROOT.
_HAS_CONNECT_BY = re.compile(r'\bCONNECT\s+BY\b', re.IGNORECASE)
_HIER_QUERY = re.compile(
    r'(?is)^\s*SELECT\s+(?P<proj>.+?)\s+FROM\s+(?P<from>.+?)'
    r'(?:\s+WHERE\s+(?P<where>.+?))?'
    r'(?:'
    r'\s+START\s+WITH\s+(?P<sw_a>.+?)\s+CONNECT\s+BY\s+(?P<cb_a>.+?)'
    r'|\s+CONNECT\s+BY\s+(?P<cb_b>.+?)\s+START\s+WITH\s+(?P<sw_b>.+?)'
    r'|\s+CONNECT\s+BY\s+(?P<cb_c>.+?)'
    r')'
    r'(?:\s+ORDER\s+(?P<siblings>SIBLINGS\s+)?BY\s+(?P<order>.+?))?'
    r'\s*;?\s*$'
)
_HIER_SINGLE_TABLE = re.compile(
    r'^\s*(?P<table>[A-Za-z_][\w$#]*)(?:\s+(?P<alias>[A-Za-z_][\w$#]*))?\s*$'
)
_HIER_CB_COND = re.compile(
    r'^\s*(?:NOCYCLE\s+)?(?P<l>.+?)\s*=\s*(?P<r>.+?)\s*$', re.IGNORECASE
)
_HIER_PRIOR = re.compile(r'^\s*PRIOR\s+(?P<col>.+?)\s*$', re.IGNORECASE)
_HIER_SIMPLE_COL = re.compile(r'^[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?$')
_HIER_STAR = re.compile(r'(^|,)\s*(\w+\s*\.\s*)?\*\s*(,|$)')
_HIER_LEVEL = re.compile(r'\bLEVEL\b', re.IGNORECASE)
_HIER_PATH = re.compile(
    r"\bSYS_CONNECT_BY_PATH\s*\(\s*(?P<col>[\w.]+)\s*,\s*'(?P<sep>[^']*)'\s*\)",
    re.IGNORECASE,
)
_HIER_ROOT = re.compile(r'\bCONNECT_BY_ROOT\s+(?P<col>[\w.]+)', re.IGNORECASE)
_HIER_COMPOUND = re.compile(r'\b(AND|OR)\b', re.IGNORECASE)


def _hier_colname(ref: str) -> str:
    return ref.split('.')[-1].strip()


def _translate_connect_by(sql: str) -> str:
    """Rewrite an Oracle CONNECT BY hierarchical query to a PostgreSQL WITH
    RECURSIVE CTE, or return it unchanged when it is not a shape we translate."""
    if _HAS_CONNECT_BY.search(sql) is None:
        return sql
    match = _HIER_QUERY.match(sql)
    if match is None or match.group('siblings'):
        return sql
    proj = match.group('proj').strip()
    from_clause = match.group('from').strip()
    where = match.group('where')
    start_with = match.group('sw_a') or match.group('sw_b')
    connect_by = (
        match.group('cb_a') or match.group('cb_b') or match.group('cb_c')
    ).strip()
    order = match.group('order')

    table_match = _HIER_SINGLE_TABLE.match(from_clause)
    if table_match is None:  # a join, subquery or comma-list is not a single table
        return sql
    table = table_match.group('table')
    alias = table_match.group('alias') or table

    if _HIER_COMPOUND.search(connect_by):  # a compound CONNECT BY is not modelled
        return sql
    cond = _HIER_CB_COND.match(connect_by)
    if cond is None:
        return sql
    left, right = cond.group('l').strip(), cond.group('r').strip()
    left_prior, right_prior = _HIER_PRIOR.match(left), _HIER_PRIOR.match(right)
    # PRIOR must be on exactly one side.
    if left_prior is not None and right_prior is None:
        prior_side, child_side = left_prior.group('col'), right
    elif right_prior is not None and left_prior is None:
        prior_side, child_side = right_prior.group('col'), left
    else:
        return sql
    if not (_HIER_SIMPLE_COL.match(prior_side) and _HIER_SIMPLE_COL.match(child_side)):
        return sql  # both operands must be plain column references
    parent_col, child_col = _hier_colname(prior_side), _hier_colname(child_side)

    if _HIER_STAR.search(proj):  # SELECT * would leak the CTE's computed columns
        return sql

    scan = ' '.join(part for part in (proj, where, order) if part)
    paths = {(col.lower(), sep) for col, sep in _HIER_PATH.findall(scan)}
    roots = {col.lower() for col in _HIER_ROOT.findall(scan)}
    if len(paths) > 1 or len(roots) > 1:  # v1 handles one distinct path / root
        return sql

    def rewrite(text: str) -> str:
        text = _HIER_LEVEL.sub('__level', text)
        text = _HIER_PATH.sub('__path', text)
        return _HIER_ROOT.sub('__root', text)

    anchor_cols = ['1 AS __level']
    rec_cols = ['__p.__level + 1']
    if paths:
        path_match = _HIER_PATH.search(scan)
        assert path_match is not None
        pcol, sep = _hier_colname(path_match.group('col')), path_match.group('sep')
        anchor_cols.append(f"'{sep}' || {alias}.{pcol} AS __path")
        rec_cols.append(f"__p.__path || '{sep}' || {alias}.{pcol}")
    if roots:
        root_match = _HIER_ROOT.search(scan)
        assert root_match is not None
        anchor_cols.append(
            f'{alias}.{_hier_colname(root_match.group("col"))} AS __root'
        )
        rec_cols.append('__p.__root')

    anchor_where = f' WHERE {start_with.strip()}' if start_with else ''
    join = f'__p.{parent_col} = {alias}.{child_col}'
    outer_where = f' WHERE {rewrite(where).strip()}' if where else ''
    outer_order = f' ORDER BY {rewrite(order).strip()}' if order else ''
    return (
        'WITH RECURSIVE __hcte AS ('
        f'SELECT {alias}.*, '
        + ', '.join(anchor_cols)
        + f' FROM {table} {alias}{anchor_where}'
        ' UNION ALL '
        f'SELECT {alias}.*, '
        + ', '.join(rec_cols)
        + f' FROM {table} {alias} JOIN __hcte __p ON {join}'
        f') SELECT {rewrite(proj)} FROM __hcte {alias}{outer_where}{outer_order}'
    )


# The Oracle date functions whose format can carry a signed year (SYYYY).
_FORMAT_CALL_NAMES = frozenset({'TO_CHAR', 'TO_DATE', 'TO_TIMESTAMP'})
_SIGNED_YEAR = re.compile('syyyy', re.IGNORECASE)


def _translate_signed_year(sql: str) -> str:
    """Give Oracle's signed year, ``SYYYY``, a PostgreSQL meaning (#1063).

    PostgreSQL knows no ``S``. Reading a date it dropped the sign, so 4712 BC
    became 4712 AD; writing one it printed a literal ``S``. PostgreSQL's own
    ``YYYY`` already reads ``-4712`` as 4712 BC, so a parsing format just loses
    the ``S``. Printing needs the sign itself, which only the value knows, so
    that call goes to ``ora_to_char_signed``. Only a format given as a literal
    is rewritten; nothing else is touched.
    """
    if not _SIGNED_YEAR.search(sql):
        return sql

    def rewrite(name: str, args: list[str], fmt: str) -> str | None:
        if not _SIGNED_YEAR.search(fmt):
            return None
        inner = [_translate_signed_year(a) for a in args]
        if name.lower() == 'to_char':
            return f'ora_to_char_signed({inner[0]}, {fmt})'
        parsed = _SIGNED_YEAR.sub('YYYY', fmt)
        return f'{name}({", ".join([inner[0], parsed, *inner[2:]])})'

    return _rewrite_format_calls(sql, rewrite, '_')


def _rewrite_format_calls(
    sql: str, rewrite: Callable[[str, list[str], str], str | None], not_after: str
) -> str:
    # `sql` with each TO_CHAR / TO_DATE / TO_TIMESTAMP call whose format is a
    # literal replaced by what `rewrite(name, args, format)` makes of it; None
    # leaves the call as it is. A call right after an alphanumeric character
    # or one of `not_after` is part of another name.
    def one(name: str, args: list[str]) -> str | None:
        fmt = args[1].strip() if len(args) >= 2 else ''
        if not (fmt.startswith("'") and fmt.endswith("'")):
            return None
        return rewrite(name, args, fmt)

    return _rewrite_calls(sql, _FORMAT_CALL_NAMES, one, not_after)


# An RR / RRRR year in a format (#1638), outside a double-quoted text part.
_RR_FORMAT_YEAR = re.compile(r'"[^"]*"|RR(?:RR)?', re.IGNORECASE)


def _rr_format(fmt: str, parsing: bool) -> str:
    # A literal format with its RR years in PostgreSQL's terms: printed, RRRR is
    # YYYY and RR is YY; parsed, both are YYYY, the window applied after.
    def one(m: re.Match[str]) -> str:
        if m.group(0).startswith('"'):
            return m.group(0)
        return 'YYYY' if parsing or len(m.group(0)) == 4 else 'YY'

    return _RR_FORMAT_YEAR.sub(one, fmt)


def _translate_rr_year(sql: str) -> str:
    """Give Oracle's RR and RRRR years a PostgreSQL meaning (#1638).

    PostgreSQL knows neither: printing one it wrote the letters, and reading
    one it took no year at all, year 1. TO_CHAR prints RR as YY and RRRR as
    YYYY; TO_DATE and TO_TIMESTAMP read them as YYYY and put a two-digit year
    into Oracle's window (``sys.ora_rr_year``). Only a format given as a
    literal is rewritten, as for the signed year.
    """
    if 'rr' not in sql.lower():
        return sql

    def rewrite(name: str, args: list[str], fmt: str) -> str | None:
        if _rr_format(fmt, True) == fmt:
            return None
        inner = [_translate_rr_year(a) for a in args]
        parsing = name.lower() != 'to_char'
        call = f'{name}({", ".join([inner[0], _rr_format(fmt, parsing), *inner[2:]])})'
        return f'sys.ora_rr_year({call})' if parsing else call

    return _rewrite_format_calls(sql, rewrite, '_.')


# DECODE(expr, search1, result1, ..., [default]) becomes a CASE (#822). orafce's
# decode is declared over polymorphic parameters, which PostgreSQL resolves from
# the argument types: all-untyped literals give it nothing to resolve from, and
# mixed types -- DECODE(MOD(i, 2), 0, NULL, POWER(143, i)) -- match no candidate.
# CASE takes both. IS NOT DISTINCT FROM, not `=`, because DECODE matches a NULL
# against a NULL. Two known differences remain: `expr` is repeated once per
# search, so a volatile one is evaluated more than once; and CASE types the
# result from all its branches, where Oracle takes the first result's type and
# makes a leading NULL one VARCHAR2. The expression and each search go in
# parentheses, as IS NOT DISTINCT FROM binds tighter than `=`, AND or OR.
# PostgreSQL's own two-argument decode(data, format) is left alone, as is a
# schema-qualified call.
_DECODE_NAMES = frozenset({'DECODE'})

# A DECODE result that converts a literal NULL -- TO_TIMESTAMP(NULL, 'YYYYMMDD')
# -- is a NULL of that function's type in PostgreSQL, and a CASE cannot unify
# it with a branch of another category: with a WITH TIME ZONE value (the
# ora_tstz composite) it is refused outright, "CASE types ora_tstz and timestamp
# with time zone cannot be matched" (#1306). Oracle types a DECODE by its FIRST
# result and converts the others to it, so when that first result is such a
# NULL, its type is known and applied the same way: the NULL is typed, every
# other result is cast to the type. A live 23ai shows it matters: a WITH TIME
# ZONE value behind a leading TO_TIMESTAMP(NULL) keeps its wall-clock time and
# loses its offset. A converted NULL anywhere else becomes a bare NULL, which a
# CASE types from its other branches -- the value is the same NULL. Only
# DECODE's results are rewritten: elsewhere the function's own type is what a
# column is described as.
_CONVERTED_NULL = re.compile(
    r'(to_timestamp_tz|to_timestamp|to_date|to_char|to_number|to_dsinterval'
    r'|to_yminterval|to_binary_float|to_binary_double)\s*\(\s*null\s*'
    r'(?:,[^()]*)?\)',
    re.IGNORECASE,
)
_CONVERTED_NULL_TYPE = {
    'to_timestamp_tz': _TSTZ_TYPE,
    'to_timestamp': 'timestamp',
    'to_date': 'oracle.date',
    'to_char': 'text',
    'to_number': 'numeric',
    'to_dsinterval': 'interval',
    'to_yminterval': 'interval',
    'to_binary_float': 'real',
    'to_binary_double': 'double precision',
}


def _decode_results(results: list[str]) -> list[str]:
    # DECODE's results (default included) typed as Oracle types them (above).
    first = _CONVERTED_NULL.fullmatch(results[0])
    if first is None:
        return ['NULL' if _CONVERTED_NULL.fullmatch(r) else r for r in results]
    pg_type = _CONVERTED_NULL_TYPE[first.group(1).lower()]
    return [f'NULL::{pg_type}'] + [
        'NULL' if _CONVERTED_NULL.fullmatch(r) else f'CAST(({r}) AS {pg_type})'
        for r in results[1:]
    ]


def _translate_decode(sql: str) -> str:
    if 'decode' not in sql.lower():
        return sql

    def case(_name: str, args: list[str]) -> str | None:
        if len(args) < 3:
            return None
        (expr, *rest) = [_translate_decode(a).strip() for a in args]
        default = rest.pop() if len(rest) % 2 else None
        results = _decode_results(
            rest[1::2] + ([default] if default is not None else [])
        )
        if default is not None:
            default = results.pop()
        rest[1::2] = results
        branches = ' '.join(
            f'WHEN ({expr}) IS NOT DISTINCT FROM ({search}) THEN {result}'
            for search, result in zip(rest[::2], rest[1::2])
        )
        otherwise = f' ELSE {default}' if default is not None else ''
        return f'CASE {branches}{otherwise} END'

    return _rewrite_calls(sql, _DECODE_NAMES, case, '_$#."')


# An Oracle identifier may carry `#` (and `$`) after its first character:
# serial#, statistic#, obj#. PostgreSQL takes `$` but not `#` (#1249).
_HASH_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_$#]*')


def _quote_hash_identifiers(sql: str) -> str:
    """Quote each unquoted identifier containing `#` in PostgreSQL's spelling of
    an unquoted name: lower case, so `serial#` names the dictionary's "serial#"
    and a column made as `obj#` reads back as OBJ# (#1249).

    String literals, quoted identifiers and comments are copied as they are, and
    a bind name (`:x#`) is left to the bind rewrite -- the shared tokenizer's
    reading of each (#1693).
    """
    if '#' not in sql:
        return sql
    out: list[str] = []
    for token in sql_tokens(sql):
        text = sql[token.start : token.end]
        if token.kind == 'word' and '#' in text:
            text = f'"{text.lower()}"'
        out.append(text)
    return ''.join(out)


# The words PostgreSQL reserves that Oracle takes as a column or attribute name,
# measured on 11g and 23ai (#1595); PostgreSQL reads them as names only quoted.
_PG_RESERVED_NAMES = frozenset(
    {
        'analyse', 'analyze', 'array', 'asymmetric', 'authorization', 'binary',
        'both', 'case', 'cast', 'collate', 'collation', 'concurrently',
        'constraint', 'cross', 'current_catalog', 'current_date', 'current_role',
        'current_schema', 'current_time', 'current_timestamp', 'current_user',
        'deferrable', 'do', 'end', 'except', 'false', 'fetch', 'foreign',
        'freeze', 'full', 'ilike', 'initially', 'inner', 'isnull', 'join',
        'lateral', 'leading', 'left', 'limit', 'localtime', 'localtimestamp',
        'natural', 'notnull', 'offset', 'only', 'outer', 'overlaps', 'placing',
        'primary', 'references', 'returning', 'right', 'session_user', 'similar',
        'some', 'symmetric', 'system_user', 'tablesample', 'trailing', 'true',
        'using', 'variadic', 'verbose', 'when', 'window',
    }
)  # fmt: skip
# Of those, the ones a statement may use bare as a name: Oracle SQL gives them
# no part, or a part a name can't be confused with -- a join's words come before
# JOIN, OFFSET and LIMIT before a value, ARRAY and LATERAL before a bracket. The
# rest -- CASE, END, CAST, FETCH, CURRENT_DATE, ... -- are Oracle's own words
# too, and are quoted only where nothing else can stand: after a dot, or as the
# name a CREATE TABLE, ALTER TABLE or CREATE TYPE declares.
_BARE_RESERVED_NAMES = frozenset(
    {
        'analyse', 'analyze', 'array', 'asymmetric', 'authorization',
        'collation', 'concurrently', 'cross', 'current_catalog', 'current_role',
        'freeze', 'full', 'ilike', 'inner', 'isnull', 'lateral', 'left', 'limit',
        'natural', 'notnull', 'offset', 'outer', 'overlaps', 'placing', 'right',
        'similar', 'symmetric', 'system_user', 'tablesample', 'verbose', 'window',
    }
)  # fmt: skip
# What may follow a name, and no keyword: punctuation, an operator, the end, or
# a word that continues an expression or opens the next clause.
_NAME_FOLLOWER = re.compile(
    r'\s*(?:[,)=<>!+\-*/|;]|$|(?:FROM|AS|IS|IN|NOT|BETWEEN|LIKE|DESC|ASC|NULLS'
    r'|AND|OR|WHERE|ORDER|GROUP|HAVING|INTO|THEN|ELSE|END|WHEN|UNION|MINUS'
    r'|INTERSECT|FOR|CONNECT|START|VALUES|SET)\b)',
    re.IGNORECASE,
)
_RESERVED_SCAN = re.compile(
    r'(--[^\n]*|/\*.*?\*/)|(?<![\w$#])([A-Za-z_][\w$#]*)', re.DOTALL
)
# The declarations a CREATE TYPE ... AS OBJECT lists, the names an INSERT's
# column list holds, and the types a column's name is followed by where it
# could be a constraint's word.
_OBJECT_ATTRIBUTES_OPEN = re.compile(r'\bAS\s+OBJECT\s*\(', re.IGNORECASE)
_INSERT_COLUMN_LIST = re.compile(
    rf'\s*INSERT\s+INTO\s+{_TABLE_NAME}'
    r'(?:\s+(?!VALUES\b|SELECT\b|WITH\b)[A-Za-z_][\w$#]*)?\s*\(',
    re.IGNORECASE,
)
_BUILTIN_TYPE_WORD = re.compile(
    r'\s+(?:NUMBER|N?VARCHAR2?|N?CHAR|DATE|TIMESTAMP|INTERVAL|N?CLOB|BLOB|RAW|LONG'
    r'|FLOAT|REAL|DOUBLE|INTEGER|INT|SMALLINT|DECIMAL|BINARY_\w+|U?ROWID)\b',
    re.IGNORECASE,
)


def _declared_name_positions(masked: str) -> set[int]:
    # Where a CREATE TABLE, an ALTER TABLE ... ADD / MODIFY or a CREATE TYPE ...
    # AS OBJECT declares a column or attribute, or an INSERT names one: each
    # item's first word. One of
    # CONSTRAINT / PRIMARY / FOREIGN is a constraint unless a type follows it.
    spans: list[tuple[int, int]] = []
    parsed = _ddl_column_spans(masked)
    if parsed is not None:
        spans = parsed[1]
    elif _CREATE_TYPE_OBJECT.match(masked):
        found = _OBJECT_ATTRIBUTES_OPEN.search(masked)
        if found is not None:
            open_at = found.end() - 1
            spans = _top_level_items(
                masked, open_at + 1, _matching_paren(masked, open_at)
            )
    # An INSERT's column list holds names only.
    insert = _INSERT_COLUMN_LIST.match(masked)
    if insert is not None:
        open_at = insert.end() - 1
        spans += _top_level_items(masked, open_at + 1, _matching_paren(masked, open_at))
    positions = set()
    for start, end in spans:
        at = start + len(masked[start:end]) - len(masked[start:end].lstrip())
        if masked.startswith(('"', "'"), at):
            continue
        word = _HASH_IDENTIFIER.match(masked, at)
        if word is None:
            continue
        if word.group().lower() in ('constraint', 'primary', 'foreign') and (
            _BUILTIN_TYPE_WORD.match(masked, word.end()) is None
        ):
            continue
        positions.add(at)
    return positions


def _quote_reserved_names(sql: str) -> str:
    """Quote a column or attribute name that PostgreSQL reserves and Oracle
    does not, INNER or WINDOW, in PostgreSQL's spelling of an unquoted name:
    lower case (#1595). Where it is declared, after a dot, and -- for the words
    Oracle SQL leaves free -- wherever what follows it shows it is a name.

    Runs on the Oracle statement, before any other rewrite: the translation
    writes PostgreSQL's own keywords (LIMIT, ARRAY[...]) that this must not see.
    """
    lowered = sql.lower()
    if not any(word in lowered for word in _PG_RESERVED_NAMES):
        return sql
    (masked, contents) = _mask_quoted(sql)
    declared = _declared_name_positions(masked)
    first = _HASH_IDENTIFIER.search(masked)
    out: list[str] = []
    pos = 0
    for match in _RESERVED_SCAN.finditer(masked):
        word = match.group(2)
        if word is None or word.lower() not in _PG_RESERVED_NAMES:
            continue
        at, end = match.start(2), match.end(2)
        before = masked[:at].rstrip()[-1:]
        after = masked[end:].lstrip()[:1]
        if before == ':' or after in ('(', '['):
            continue
        if not (
            at in declared
            or before == '.'
            or (
                word.lower() in _BARE_RESERVED_NAMES
                and not (first is not None and first.start() == at)
                and _NAME_FOLLOWER.match(masked, end)
            )
        ):
            continue
        out.append(f'{masked[pos:at]}"\x00{len(contents)}\x00"')
        contents.append(word.lower())
        pos = end
    if not out:
        return sql
    return _unmask_quoted(''.join(out) + masked[pos:], contents)


_ROWNUM_WORD = re.compile(r'\bROWNUM\b', re.IGNORECASE)
_ROWNUM_PREDICATE = re.compile(r'ROWNUM\s*(<=|<|=)\s*(\d+|:\w+)', re.IGNORECASE)
# The top-level words under which ROWNUM and LIMIT part ways: Oracle numbers the
# rows before it sorts, groups, de-duplicates or combines them, LIMIT after; an
# OR could bind the filter to one side only (#1271).
_ROWNUM_BLOCKERS = frozenset(
    {
        'ORDER', 'GROUP', 'HAVING', 'UNION', 'INTERSECT', 'MINUS', 'EXCEPT',
        'CONNECT', 'START', 'FOR', 'OR', 'FETCH', 'OFFSET', 'LIMIT', 'MODEL',
    }
)  # fmt: skip
_ROWNUM_AGGREGATES = re.compile(
    r'\b(?:COUNT|SUM|AVG|MIN|MAX|LISTAGG|STDDEV|VARIANCE|MEDIAN|COLLECT|XMLAGG|'
    r'JSON_ARRAYAGG|JSON_OBJECTAGG|ARRAY_AGG|STRING_AGG|CORR|COVAR_POP|COVAR_SAMP|'
    r'REGR_\w+|PERCENTILE_\w+|RANK|DENSE_RANK|CUME_DIST|PERCENT_RANK)\s*\(|\bOVER\b',
    re.IGNORECASE,
)


def _top_level_words(sql: str) -> tuple[list[tuple[int, str]], list[int]]:
    # (position, UPPER word) of every word outside parentheses, quotes and
    # comments, and the position of every ROWNUM outside quotes and comments at
    # any depth -- the shared tokenizer's words and parentheses (#1693).
    words: list[tuple[int, str]] = []
    rownums: list[int] = []
    depth = 0
    for token in sql_tokens(sql):
        if token.kind == 'other':
            char = sql[token.start]
            depth += 1 if char == '(' else -1 if char == ')' else 0
        elif token.kind == 'word':
            word = sql[token.start : token.end].upper()
            if word == 'ROWNUM':
                rownums.append(token.start)
            if depth == 0:
                words.append((token.start, word))
    return words, rownums


# A select-list item that is one LOB-producing call, optionally aliased (#1351).
_LOB_CALL_ITEM = re.compile(
    r'(?:sys\s*\.\s*)?(to_clob|empty_clob|to_blob|empty_blob)\s*\(', re.IGNORECASE
)
# An IntervalYM bind as the translation writes it (_translate_binds).
_INTERVALYM_BIND_ITEM = re.compile(r'make_interval\(months => %\(\w+\)s\)')
_ITEM_ALIAS = re.compile(r'(?:AS\s+)?(?:"[^"]*"|[A-Za-z_][\w$#]*)?', re.IGNORECASE)
# A select-list item that is one call, `name(...)` or `schema.name(...)`,
# optionally aliased: a type constructor when `name` is a type (#1473).
_CONSTRUCTOR_CALL_ITEM = re.compile(
    r'([A-Za-z_][\w$#]*(?:\s*\.\s*[A-Za-z_][\w$#]*)?)\s*\('
)
# A collection bind as _translate_binds casts it to its domain (#1541).
_CAST_BIND_ITEM = re.compile(r'%\(\w+\)s::([\w."$#]+)')


# CAST(<expr> AS NVARCHAR2(n) | NCHAR[(n)]): the national target the translation
# turns into varchar(n) / char(n) (#1440).
_CAST_ITEM = re.compile(r'CAST\s*\(', re.IGNORECASE)
# A select item that is one national string literal, N'...', and its alias.
_NATIONAL_LITERAL_ITEM = re.compile(r"[Nn]'((?:[^']|'')*)'(.*)", re.DOTALL)
_NATIONAL_CAST_TARGET = re.compile(
    r'\bAS\s+(?:NVARCHAR2\s*\(\s*(\d+)|NCHAR\b(?!\s+VARYING)\s*(?:\(\s*(\d+))?)'
    r'[^()]*\)?\s*\Z',
    re.IGNORECASE,
)


# A numeric literal, which is no identifier for the constant check below.
_NUMERIC_LITERAL = re.compile(
    r'(?<![\w$#.])\d+(?:\.\d*)?(?:[eE][+-]?\d+)?[dfDF]?\b|\.\d+'
)
# A select item's trailing alias, `expr [AS] name`, after an expression that
# ends as one can -- a literal, a closing parenthesis or a quote.
_CONSTANT_ITEM_ALIAS = re.compile(
    r'(?<=[\w\)\'])\s+(?:AS\s+)?(?:"[^"]*"|[A-Za-z_][\w$#]*)\s*\Z|(?<=\))\s*"[^"]*"\s*\Z',
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(r'(?<![\w$#])[A-Za-z_][\w$#]*')


# A select item that is a column, bare, qualified or in parentheses; and the
# pseudo-columns, which are no column but are named the way one is (#1449).
_PLAIN_COLUMN_ITEM = re.compile(
    r'\(*\s*(?:(?:"[^"]*"|[A-Za-z_][\w$#]*)\s*\.\s*)*("[^"]*"|[A-Za-z_][\w$#]*)\s*\)*'
)
_ORACLE_PSEUDO_COLUMNS = frozenset(
    {
        'SYSDATE',
        'SYSTIMESTAMP',
        'CURRENT_DATE',
        'CURRENT_TIMESTAMP',
        'LOCALTIMESTAMP',
        'USER',
        'UID',
        'ROWNUM',
        'LEVEL',
        'ROWID',
        'SESSIONTIMEZONE',
        'DBTIMEZONE',
    }
)


def _expression_names(sql: str) -> dict[int, str]:
    """The names Oracle gives an Oracle query's unaliased computed select items,
    by position (#1449): the item's own text, every space dropped and the rest
    upper-cased -- its string literals and comments too -- where PostgreSQL
    names a call by its function and anything else ?column?. A pseudo-column
    is its name. A column, or an aliased item, keeps the name it has.
    Measured on 23ai.
    """
    (masked, contents) = _mask_quoted(sql)
    words, _rownums = _top_level_words(masked)
    if not words or words[0][1] != 'SELECT':
        return {}
    start = words[0][0] + len('SELECT')
    if len(words) > 1 and words[1][1] in ('DISTINCT', 'UNIQUE', 'ALL'):
        start = words[1][0] + len(words[1][1])
    end = next((pos for pos, word in words if word == 'FROM'), len(masked))
    items = [masked[s:e].strip() for s, e in _top_level_items(masked, start, end)]
    if any(item == '*' or item.endswith('.*') for item in items):
        return {}
    names = {}
    for index, item in enumerate(items):
        column = _PLAIN_COLUMN_ITEM.fullmatch(item)
        if column is not None:
            if column.group(1).upper() in _ORACLE_PSEUDO_COLUMNS:
                names[index] = column.group(1).upper()
            continue
        if _CONSTANT_ITEM_ALIAS.search(item):
            continue
        names[index] = ''.join(_unmask_quoted(item, contents).split()).upper()
    return names


def _constant_items(sql: str) -> set[int]:
    """The select-list positions of an Oracle query that are constant: literals,
    operators and calls of literals, nothing else -- no column, bind,
    pseudo-column, subquery or aggregate. Oracle folds such an item, and a
    NUMBER one describes with precision 0 and scale -127, where a number
    computed from a column has neither (#1444, measured on 23ai).
    """
    # Positions on the masked text throughout: masking changes its length.
    (masked, _contents) = _mask_quoted(sql)
    words, _rownums = _top_level_words(masked)
    if not words or words[0][1] != 'SELECT':
        return set()
    start = words[0][0] + len('SELECT')
    end = next((pos for pos, word in words if word == 'FROM'), len(masked))
    items = [masked[s:e].strip() for s, e in _top_level_items(masked, start, end)]
    if any(item == '*' or item.endswith('.*') for item in items):
        return set()
    found = set()
    for index, item in enumerate(items):
        expression = _CONSTANT_ITEM_ALIAS.sub('', item)
        if (
            not expression
            or ':' in expression
            or '"' in expression
            or _ROWNUM_AGGREGATES.search(expression)
        ):
            continue
        bare = _NUMERIC_LITERAL.sub(' ', expression)
        if all(
            bare[m.end() :].lstrip().startswith('(') for m in _IDENTIFIER.finditer(bare)
        ) and not re.search(r'\bSELECT\b', bare, re.IGNORECASE):
            found.add(index)
    return found


def _computed_national_columns(sql: str) -> dict[int, int]:
    """The select-list positions of an Oracle query that CAST a value to
    NVARCHAR2(n) or NCHAR(n), each with its n (#1440), or that are a national
    literal N'...', NCHAR of its length (#1586).

    The translation makes the target varchar(n) / char(n), so the column comes
    back as the database character set's; Oracle describes the national type, n
    characters of AL16UTF16. Only an item that is the whole cast is recognised.
    """
    words, _rownums = _top_level_words(sql)
    if not words or words[0][1] != 'SELECT':
        return {}
    start = words[0][0] + len('SELECT')
    end = next((pos for pos, word in words if word == 'FROM'), len(sql))
    items = [sql[s:e].strip() for s, e in _top_level_items(sql, start, end)]
    if any(item == '*' or item.endswith('.*') for item in items):
        return {}  # the positions are the expanded columns', not the items'
    found = {}
    for index, item in enumerate(items):
        national = _NATIONAL_LITERAL_ITEM.fullmatch(item)
        if national is not None and _ITEM_ALIAS.fullmatch(national.group(2).strip()):
            if national.group(1):
                found[index] = len(national.group(1).replace("''", "'"))
            continue
        cast = _CAST_ITEM.match(item)
        if cast is None:
            continue
        close = _matching_paren(item, cast.end() - 1)
        if close >= len(item) or not _ITEM_ALIAS.fullmatch(item[close + 1 :].strip()):
            continue
        target = _NATIONAL_CAST_TARGET.search(item, cast.end(), close)
        if target is not None:
            found[index] = int(target.group(1) or target.group(2) or 1)
    return found


def _computed_column_types(sql: str) -> dict[int, int]:
    """The select-list positions of a query whose Oracle type the item says.

    An IntervalYM bind reaches PostgreSQL as `make_interval(months => ...)`, a
    plain interval that would describe as DAY TO SECOND and read its months as
    days; the item alone says it is YEAR TO MONTH (#1401). The rest are LOB
    calls, as follows.

    PostgreSQL describes a computed value by its base type even when the function
    returns the ora_clob / ora_blob domain, so TO_CLOB('x') or EMPTY_BLOB() would
    describe as text / bytea -- a VARCHAR or a RAW -- where Oracle describes a
    CLOB or a BLOB (#1351). Only an item that is the whole call is recognised; a
    call inside an expression, or a statement that is not a plain SELECT, maps
    nothing. TO_NCLOB is left out: an NCLOB is a CLOB in the national character
    set, which the LOB column shape does not carry.
    """
    words, _rownums = _top_level_words(sql)
    if not words or words[0][1] != 'SELECT':
        return {}
    start = words[0][0] + len('SELECT')
    end = next((pos for pos, word in words if word == 'FROM'), len(sql))
    items = [sql[s:e].strip() for s, e in _top_level_items(sql, start, end)]
    if any(item == '*' or item.endswith('.*') for item in items):
        return {}  # the positions are the expanded columns', not the items'
    found = {}
    for index, item in enumerate(items):
        ym = _INTERVALYM_BIND_ITEM.match(item)
        if ym is not None and _ITEM_ALIAS.fullmatch(item[ym.end() :].strip()):
            found[index] = TNS_TYPE_INTERVALYM
            continue
        call = _LOB_CALL_ITEM.match(item)
        if call is None:
            continue
        close = _matching_paren(item, call.end() - 1)
        if close >= len(item) or not _ITEM_ALIAS.fullmatch(item[close + 1 :].strip()):
            continue
        name = call.group(1).lower()
        found[index] = TNS_TYPE_CLOB if name.endswith('clob') else TNS_TYPE_BLOB
    return found


def _constructor_items(sql: str) -> dict[int, str]:
    """The select-list positions of a query that are one call, by the name called.

    PostgreSQL describes a computed value by its domain's base type, so a
    collection constructor -- `t_arr(5, 10)`, a function returning the
    collection's domain -- reads as a bare array with no table column to trace
    the domain through (#1473). The name is what says which collection it is;
    the caller keeps it only when it names a collection type.
    """
    words, _rownums = _top_level_words(sql)
    if not words or words[0][1] != 'SELECT':
        return {}
    start = words[0][0] + len('SELECT')
    end = next((pos for pos, word in words if word == 'FROM'), len(sql))
    items = [sql[s:e].strip() for s, e in _top_level_items(sql, start, end)]
    if any(item == '*' or item.endswith('.*') for item in items):
        return {}  # the positions are the expanded columns', not the items'
    found = {}
    for index, item in enumerate(items):
        cast = _CAST_BIND_ITEM.match(item)
        if cast is not None and _ITEM_ALIAS.fullmatch(item[cast.end() :].strip()):
            # A collection bind, cast to its domain, which names the type (#1541).
            found[index] = cast.group(1)
            continue
        call = _CONSTRUCTOR_CALL_ITEM.match(item)
        if call is None:
            continue
        close = _matching_paren(item, call.end() - 1)
        if close < len(item) and _ITEM_ALIAS.fullmatch(item[close + 1 :].strip()):
            found[index] = re.sub(r'\s+', '', call.group(1))
    return found


# FROM dual CONNECT BY LEVEL | ROWNUM <= | < n -- Oracle's row generator, which
# counts 1..n. PostgreSQL has no CONNECT BY; the counter maps to generate_series
# aliased `level`, so a bare LEVEL in the select list resolves to its column
# (#531). n may be a literal or a bind (#1558).
_ROW_GENERATOR = re.compile(
    r'\bFROM\s+dual\s+CONNECT\s+BY\s+(?:LEVEL|ROWNUM)\s*(<=|<)\s*(\d+|:\w+)',
    re.IGNORECASE,
)


# The statements whose values may be headed for a RAW target (#1496), read on
# the text _mask_quoted leaves: INSERT INTO t [(cols)] VALUES (...), UPDATE t SET.
_INSERT_VALUES_HEAD = re.compile(
    rf'\s*INSERT\s+INTO\s+{_TABLE_NAME}\s*(?:\(([^()]*)\))?\s*VALUES\s*\(',
    re.IGNORECASE,
)
_UPDATE_SET_HEAD = re.compile(
    rf'\s*UPDATE\s+{_TABLE_NAME}(?:\s+(?!SET\b)[\w$#]+)?\s+SET\b', re.IGNORECASE
)
# A single-table statement's table and alias, on masked text (#1496): SELECT ...
# FROM t [a], UPDATE t [a] SET, DELETE [FROM] t [a].
_SINGLE_TABLE_FROM = re.compile(
    rf'\bFROM\s+{_TABLE_NAME}(?:\s+(?!WHERE\b|ORDER\b|GROUP\b|FOR\b)([A-Za-z_][\w$#]*))?'
    r'\s*(?=WHERE\b|ORDER\b|GROUP\b|FOR\b|$|\))',
    re.IGNORECASE,
)
_DELETE_FROM_HEAD = re.compile(
    rf'\s*DELETE\s+(?:FROM\s+)?{_TABLE_NAME}(?:\s+(?!WHERE\b)([A-Za-z_][\w$#]*))?',
    re.IGNORECASE,
)
# A comparison of a column with a value, either way round, and an IN list
# (#1496); the value a literal (as masked) or a bind.
_RAW_VALUE = r'(?:[Nn]?\'\x00\d+\x00\'|:(?:\w+|"[^"]+"))'
_RAW_COMPARISON = re.compile(
    rf'(?:([A-Za-z_][\w$#]*)\s*\.\s*)?([A-Za-z_][\w$#]*|"[^"]+")\s*'
    rf'(?:=|<>|!=|\^=|<=|>=|<|>)\s*({_RAW_VALUE})'
)
_RAW_COMPARISON_REVERSED = re.compile(
    rf'({_RAW_VALUE})\s*(?:=|<>|!=|\^=|<=|>=|<|>)\s*'
    r'(?:([A-Za-z_][\w$#]*)\s*\.\s*)?([A-Za-z_][\w$#]*|"[^"]+")(?![\w$#]|\s*\()'
)
_RAW_IN_LIST = re.compile(
    r'(?:([A-Za-z_][\w$#]*)\s*\.\s*)?([A-Za-z_][\w$#]*|"[^"]+")\s+IN\s*\(',
    re.IGNORECASE,
)
# A value Oracle converts to RAW as hex: a string literal (as masked) or a bind.
_RAW_TARGET_VALUE = re.compile(r'\s*(?:[Nn]?\'\x00\d+\x00\'|:(?:\w+|"[^"]+"))\s*')
_CALL_HEAD = re.compile(r'(?<![\w$#."])([A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?)\s*\(')


def _pg_identifier(name: str) -> str:
    # A column name as PostgreSQL stores it: quoted as written, else lower case.
    name = name.strip()
    return name[1:-1] if name.startswith('"') else name.lower()


def _translate_row_generator(sql: str) -> str:
    """The row generator as generate_series (#531, #1558). Its ROWNUM numbers
    the rows as LEVEL does, so ROWNUM there is the `level` column too; `< n` is
    n - 1 rows. A bound n is taken as Oracle compares a NUMBER with it: LEVEL <=
    2.5 stops at 2, LEVEL < 2.5 at 2 as well."""
    (masked, contents) = _mask_quoted(sql)
    found = _ROW_GENERATOR.search(masked)
    if found is None:
        return sql
    (op, bound) = found.groups()
    if bound.isdigit():
        upper = str(int(bound) - 1) if op == '<' else bound
    elif op == '<=':
        upper = f'floor(({bound})::numeric)::bigint'
    else:
        upper = f'ceil(({bound})::numeric)::bigint - 1'
    masked = (
        masked[: found.start()]
        + f'FROM generate_series(1, {upper}) AS level'
        + masked[found.end() :]
    )
    return _unmask_quoted(_ROWNUM_WORD.sub('level', masked), contents)


def _rewrite_rownum(sql: str) -> str:
    """Translate `... WHERE a AND ROWNUM <= n` to `... WHERE a LIMIT n` (#1271).

    Only where the two mean the same: a top-level SELECT whose WHERE is a chain
    of ANDs with one `ROWNUM <= n`, `< n` or `= n` in it (n a number or a bind),
    and nothing that Oracle applies after numbering the rows -- no ORDER BY,
    GROUP BY, DISTINCT, aggregate or analytic function, set operator,
    hierarchical query, FOR UPDATE or OR. Nested-ROWNUM pagination has no
    faithful rewrite (#33); every ROWNUM this leaves is refused by name, rather
    than reaching PostgreSQL as an unknown column.
    """
    if not _ROWNUM_WORD.search(sql):
        return sql
    words, rownums = _top_level_words(sql)
    if not rownums:
        return sql  # only in a string or a comment
    refusal = UnsupportedFeature(
        'ROWNUM is translated only as a top-level `AND ROWNUM <= n` filter of a '
        'query with no ORDER BY, GROUP BY, DISTINCT, aggregate or set operator'
    )
    names = [w for _, w in words]
    if (
        len(rownums) != 1
        or not names
        or names[0] != 'SELECT'
        or 'WHERE' not in names
        or 'FROM' not in names
        or _ROWNUM_BLOCKERS.intersection(names)
    ):
        raise refusal
    first_at = dict((w, p) for p, w in reversed(words))
    where = first_at['WHERE']
    select_list = sql[words[0][0] + len('SELECT') : first_at['FROM']]
    pos = rownums[0]
    first = select_list.split(None, 1)[0].upper() if select_list.split() else ''
    if (
        pos < where
        or (pos, 'ROWNUM') not in words
        or first in ('DISTINCT', 'UNIQUE')
        or _ROWNUM_AGGREGATES.search(select_list)
    ):
        raise refusal
    predicate = _ROWNUM_PREDICATE.match(sql, pos)
    if predicate is None:
        raise refusal
    before = sql[where + len('WHERE') : pos].rstrip()
    after = sql[predicate.end() :].rstrip().rstrip(';').rstrip()
    joined_before = re.search(r'\bAND$', before, re.IGNORECASE)
    joined_after = re.match(r'\s*AND\b', after, re.IGNORECASE)
    if (before and not joined_before) or (after and not joined_after):
        raise refusal
    op, bound = predicate.group(1), predicate.group(2)
    if bound.isdigit():
        count = int(bound)
        limit = str(
            count if op == '<=' else max(count - 1, 0) if op == '<' else int(count == 1)
        )
    elif op == '<=':
        limit = bound
    elif op == '<':
        limit = f'greatest({bound} - 1, 0)'
    else:
        limit = f'CASE WHEN {bound} = 1 THEN 1 ELSE 0 END'
    if before:
        rest = before[: joined_before.start()].rstrip() if joined_before else before
        rest = f'{rest} {after}' if after else rest
    else:
        rest = after[joined_after.end() :].strip() if joined_after else after
    head = sql[:where].rstrip()
    body = f'{head} WHERE {rest.strip()}' if rest.strip() else head
    return f'{body} LIMIT {limit}'


# Not after a `.`: the generated `sys.deref(` is already the translation.
_DEREF_NAMES = frozenset({'DEREF'})


def _translate_deref(sql: str) -> str:
    # DEREF(x) → (sys.deref(x)) (#1127). The parentheses round the call are what
    # PostgreSQL needs before a field selection: Oracle's `DEREF(r).name` is
    # `(sys.deref(r)).name`. A DEREF inside the argument is translated too.
    return _rewrite_calls(
        sql,
        _DEREF_NAMES,
        lambda _name, args: f'(sys.deref({_translate_deref(",".join(args))}))',
        '._',
    )


# A table's correlation name, in a FROM or JOIN list or an UPDATE's head (#1434);
# the words that may follow a table in its stead.
_CORRELATION_NAME = re.compile(
    rf'(?:\bFROM|\bJOIN|\bUPDATE|,)\s+{_TABLE_NAME}\s+'
    r'(?!(?:WHERE|ON|USING|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|NATURAL|OUTER|SET|GROUP'
    r'|ORDER|HAVING|CONNECT|START|UNION|MINUS|INTERSECT|EXCEPT|FOR|FETCH|OFFSET'
    r'|PARTITION|SAMPLE|PIVOT|UNPIVOT|MODEL|WITH|RETURNING|LOG|INTO|VALUES)\b)'
    r'([A-Za-z_][\w$#]*)',
    re.IGNORECASE,
)
# alias.column.attribute[.attribute...], on masked text; a method call is not one.
_ATTRIBUTE_PART = r'(?:[A-Za-z_][\w$#]*|"\x00\d+\x00")'
_ATTRIBUTE_PATH = re.compile(
    rf'(?<![\w$#."@:])([A-Za-z_][\w$#]*)\s*\.\s*({_ATTRIBUTE_PART})'
    rf'((?:\s*\.\s*{_ATTRIBUTE_PART})+)(?![\w$#.]|\s*\(|\s*\.)'
)
_ATTRIBUTE_DOT = re.compile(r'\s*\.\s*')
_UPDATE_HEAD = re.compile(r'\s*UPDATE\b', re.IGNORECASE)
_SET_TARGET_BEFORE = re.compile(r'(?:\bSET|,)\s*$', re.IGNORECASE)
_SET_TARGET_AFTER = re.compile(r'\s*=')


def _translate_attribute_access(sql: str) -> str:
    """Oracle's object attribute access, `alias.column.attribute`, in
    PostgreSQL's spelling `(alias.column).attribute`, which reads the Oracle
    one as schema, table and column (#1434). Oracle takes it through a table's
    correlation name only, so only one declared in the statement is rewritten.
    An UPDATE's SET target is `column.attribute` in PostgreSQL; an unaliased
    select item is named as Oracle names it, the path but the alias: O.V.
    """
    (masked, contents) = _mask_quoted(sql)
    aliases = {m.group(2).upper() for m in _CORRELATION_NAME.finditer(masked)}
    if not aliases:
        return sql
    update = _UPDATE_HEAD.match(masked) is not None
    words, _rownums = _top_level_words(masked)
    where = next((p for p, w in words if w == 'WHERE'), len(masked))
    named: dict[int, str] = {}
    if words and words[0][1] == 'SELECT':
        start = words[0][0] + len('SELECT')
        if len(words) > 1 and words[1][1] in ('DISTINCT', 'UNIQUE', 'ALL'):
            start = words[1][0] + len(words[1][1])
        end = next((p for p, w in words if w in _SELECT_LIST_ENDS), len(masked))
        for s_at, e_at in _top_level_items(masked, start, end):
            item = masked[s_at:e_at]
            path = _ATTRIBUTE_PATH.fullmatch(item.strip())
            if path is not None:
                named[s_at + len(item) - len(item.lstrip())] = '.'.join(
                    part[1:-1] if part.startswith('"') else part.upper()
                    for part in _ATTRIBUTE_DOT.split(
                        _unmask_quoted(path.group(2) + path.group(3), contents)
                    )
                )
    out: list[str] = []
    pos = 0
    for match in _ATTRIBUTE_PATH.finditer(masked):
        if match.group(1).upper() not in aliases:
            continue
        attributes = _ATTRIBUTE_DOT.split(match.group(3))[1:]
        if (
            update
            and match.start() < where
            and _SET_TARGET_BEFORE.search(masked, 0, match.start())
            and _SET_TARGET_AFTER.match(masked, match.end())
        ):
            rewritten = '.'.join([match.group(2), *attributes])
        else:
            rewritten = f'{match.group(1)}.{match.group(2)}'
            for attribute in attributes:
                rewritten = f'({rewritten}).{attribute}'
        if match.start() in named:
            name = named[match.start()].replace('"', '""')
            rewritten += f' AS "\x00{len(contents)}\x00"'
            contents.append(name)
        out.append(masked[pos : match.start()] + rewritten)
        pos = match.end()
    if not out:
        return sql
    return _unmask_quoted(''.join(out) + masked[pos:], contents)


# CURSOR(SELECT ...), a cursor-valued select-list item (#1461).
_CURSOR_EXPRESSION = re.compile(r'\bCURSOR\s*\((?=\s*SELECT\b)', re.IGNORECASE)
# A select-list item's trailing alias: `expr alias` or `expr AS alias`.
_TRAILING_ALIAS = re.compile(
    r'(?is)^(.*?[\w)\'"])\s+(?:AS\s+)?("[^"]+"|[A-Za-z_][\w$#]*)$'
)


def _select_item_name(item: str) -> str:
    # The name Oracle gives a select-list item: its alias, a column's own name,
    # else the expression's text, upper-cased with its spaces gone.
    match = _TRAILING_ALIAS.match(item)
    if match is not None and match.group(2).upper() != 'END':
        alias = match.group(2)
        return alias[1:-1] if alias.startswith('"') else alias.upper()
    if re.fullmatch(r'[\w$#.]+', item):
        return item.rsplit('.', 1)[-1].upper()
    return re.sub(r'\s+', '', item).upper()


def _translate_cursor_expressions(sql: str) -> str:
    # CURSOR(sub) → sys.ora_cursor(doc, names) (#1461). PostgreSQL has no cursor
    # expression, and `sub` usually names the outer row's columns, so it cannot
    # run apart. It runs in place instead, inside one scalar subquery that
    # gathers its rows and its column types into a jsonb document --
    # LEFT JOINed to a single row, so an empty result still has its types --
    # and sys.ora_cursor opens a cursor over that document. The result path
    # drains it as it does a REF CURSOR (#518). A nested CURSOR(...) in `sub`
    # translates first, its cursor a value of the outer one's rows.
    out: list[str] = []
    pos = 0
    for match in _CURSOR_EXPRESSION.finditer(sql):
        if match.start() < pos:
            continue  # inside a subquery already translated
        close = _matching_paren(sql, match.end() - 1)
        if close >= len(sql):
            return sql  # unbalanced: leave the statement to fail as is
        sub = sql[match.end() : close].strip()
        words, _rownums = _top_level_words(sub)
        end = next((at for at, word in words if word == 'FROM'), len(sub))
        start = words[0][0] + len('SELECT')
        if len(words) > 1 and words[1][1] in ('DISTINCT', 'UNIQUE', 'ALL'):
            start = words[1][0] + len(words[1][1])
        items = [sub[s:e].strip() for s, e in _top_level_items(sub, start, end)]
        if any(item == '*' or item.endswith('.*') for item in items):
            continue  # the columns are not the items: leave it to fail
        names = ', '.join(
            "'" + _select_item_name(item).replace("'", "''") + "'" for item in items
        )
        columns = ', '.join(f'c{i}' for i in range(1, len(items) + 1))
        types = ', '.join(
            f'(array_agg(pg_typeof(_s.c{i})))[1]::text'
            for i in range(1, len(items) + 1)
        )
        doc = (
            f'(SELECT jsonb_build_array(jsonb_build_array({types}), '
            f"COALESCE(jsonb_agg(to_jsonb(_s) - '_m' - '_n' ORDER BY _s._n) "
            f"FILTER (WHERE _s._m), '[]')) FROM (SELECT 1) _d LEFT JOIN "
            f'(SELECT true AS _m, row_number() OVER () AS _n, _x.* FROM '
            f'({_translate_cursor_expressions(sub)}) _x({columns})) _s ON true)'
        )
        out.append(sql[pos : match.start()] + f'sys.ora_cursor({doc}, ARRAY[{names}])')
        pos = close + 1
    return ''.join(out) + sql[pos:]


# A masked quoted region's contents: NUL, the region's index, NUL (#1481).
_QUOTED_MASK = re.compile('\x00(\\d+)\x00')


def _mask_quoted(sql: str) -> tuple[str, list[str]]:
    # `sql` with each string literal's and quoted identifier's contents replaced
    # by a numbered mask, and the contents in mask order. The quotes stay, so a
    # rewrite still sees that a literal or an identifier is there; comments are
    # left as they are, an apostrophe in one opening nothing. The spans are the
    # shared tokenizer's (#1693): a literal's N or q prefix stays outside its
    # mask, and a quoted bind name is masked as the identifier it is spelled as.
    out: list[str] = []
    contents: list[str] = []

    def mask(text: str, opening: int, closing: int) -> None:
        # `text` with all but its first `opening` and last `closing` characters
        # masked.
        out.append(text[:opening])
        out.append(f'\x00{len(contents)}\x00')
        contents.append(text[opening : len(text) - closing])
        out.append(text[len(text) - closing :])

    for token in sql_tokens(sql):
        text = sql[token.start : token.end]
        if token.kind == 'string':
            opening = text.index("'") + 1
            closed = len(text) > opening and text.endswith("'")
            mask(text, opening, 1 if closed else 0)
        elif token.kind == 'identifier':
            closed = len(text) > 1 and text.endswith('"')
            mask(text, 1, 1 if closed else 0)
        elif token.kind == 'bind' and text.endswith('"'):
            mask(text, text.index('"') + 1, 1)
        else:
            out.append(text)
    return ''.join(out), contents


def _unmask_quoted(sql: str, contents: list[str]) -> str:
    return _QUOTED_MASK.sub(lambda m: contents[int(m.group(1))], sql)


_SELECT_WORD = re.compile(r'\bSELECT\b', re.IGNORECASE)
# A SELECT that is a branch of a set operation: Oracle types a literal there by
# the widest branch, a VARCHAR when they differ, which PostgreSQL's text is.
_SET_OPERATOR_BEFORE = re.compile(
    r'\b(?:UNION(?:\s+ALL)?|INTERSECT|EXCEPT|MINUS)\s*\(?\s*$', re.IGNORECASE
)
# A dollar-quote delimiter, $$ or $tag$.
_DOLLAR_QUOTE = re.compile(r'\$\w*\$')
# The clauses that can follow a select list.
_SELECT_LIST_ENDS = frozenset(
    {'FROM', 'INTO', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'FETCH', 'OFFSET', 'LIMIT'}
)
# A select item that is one masked string literal, optionally aliased.
_LITERAL_ITEM = re.compile("'\x00(\\d+)\x00'(.*)", re.DOTALL)
# A select item that concatenates string literals and nothing else, which
# Oracle types CHAR as it does one literal (#1587).
_LITERAL_CONCAT_ITEM = re.compile(
    "('\x00\\d+\x00'(?:\\s*\\|\\|\\s*'\x00\\d+\x00')+)(.*)", re.DOTALL
)


def _cast_literal_items(sql: str) -> str:
    # A select item that is one non-empty string literal is CHAR(n) in Oracle,
    # n its length; PostgreSQL resolves it to text, a VARCHAR (#1494). Each
    # becomes CAST(lit AS char(n)), in every select list -- a subquery's, a
    # cursor's a block opens -- but a set operation's, where Oracle types it by
    # the widest branch.
    (masked, contents) = _mask_quoted(sql)
    edits: list[tuple[int, int, str]] = []
    for match in _SELECT_WORD.finditer(masked):
        if _SET_OPERATOR_BEFORE.search(masked, 0, match.start()):
            continue
        # The statement this SELECT starts ends at its closing parenthesis, a
        # `;` or a dollar quote: a DO block -- a CREATE OR REPLACE VIEW is one --
        # can hold the same select list twice, and one with no FROM (the compat
        # layer drops `FROM dual`) would otherwise run on into the next.
        depth, end = 0, len(masked)
        for i in range(match.end(), len(masked)):
            if masked[i] == '(':
                depth += 1
            elif masked[i] == ')':
                depth -= 1
                if depth < 0:
                    end = i
                    break
            elif depth == 0 and (
                masked[i] == ';' or _DOLLAR_QUOTE.match(masked, i) is not None
            ):
                end = i
                break
        segment = masked[match.start() : end]
        words, _rownums = _top_level_words(segment)
        if any(w in ('UNION', 'INTERSECT', 'EXCEPT', 'MINUS') for _p, w in words):
            continue
        # The list ends at the first clause -- FROM, or an INTO in PL/SQL -- or
        # at the statement's end: the compat layer drops a `FROM dual`.
        stop = next((p for p, w in words[1:] if w in _SELECT_LIST_ENDS), len(segment))
        start = len('SELECT')
        if len(words) > 1 and words[1][1] in ('DISTINCT', 'UNIQUE', 'ALL'):
            start = words[1][0] + len(words[1][1])
        for s_at, e_at in _top_level_items(segment, start, stop):
            item = segment[s_at:e_at].strip()
            concat = _LITERAL_CONCAT_ITEM.fullmatch(item)
            if concat is not None and _ITEM_ALIAS.fullmatch(concat.group(2).strip()):
                # The concatenation's length is its literals' together; '' is
                # NULL in Oracle, which a concatenation passes over (#1587).
                length = sum(
                    len(contents[int(i)].replace("''", "'"))
                    for i in re.findall('\x00(\\d+)\x00', concat.group(1))
                )
                if length:
                    at = match.start() + segment.index(item, s_at)
                    expression = concat.group(1)
                    edits.append(
                        (
                            at,
                            at + len(expression),
                            f'CAST(({expression}) AS char({length}))',
                        )
                    )
                continue
            literal = _LITERAL_ITEM.fullmatch(item)
            if literal is None or not _ITEM_ALIAS.fullmatch(literal.group(2).strip()):
                continue
            text = contents[int(literal.group(1))]
            length = len(text.replace("''", "'"))
            if not length:
                continue  # '' is NULL in Oracle, of no length
            at = match.start() + segment.index(item, s_at)
            mask = item[: item.index("'", 1) + 1]
            edits.append((at, at + len(mask), f'CAST({mask} AS char({length}))'))
    for at, until, replacement in sorted(set(edits), reverse=True):
        masked = masked[:at] + replacement + masked[until:]
    return _unmask_quoted(masked, contents)


# The translation report (#1557): the names of the translation rules that have
# changed the statement in flight, or None while no report is being taken.
_TRANSLATION_RULES: ContextVar[list[str] | None] = ContextVar(
    '_TRANSLATION_RULES', default=None
)


def _note_rule(name: str) -> None:
    rules = _TRANSLATION_RULES.get()
    if rules is not None and name not in rules:
        rules.append(name)


def _ruled(name: str, translated: str, sql: str) -> str:
    # `translated`, noting the rule `name` when it changed `sql` (#1557).
    if translated != sql:
        _note_rule(name)
    return translated


def _translated_batch(sql: str) -> str:
    # An array DML statement's translation, each pass noted (#1557).
    sql = _ruled('reserved-name', _quote_reserved_names(sql), sql)
    sql = _ruled('ddl', _translate_ddl(sql), sql)
    sql = _ruled('routine-ddl', _translate_routine_ddl(sql), sql)
    sql = _ruled('plsql-block', _translate_plsql_block(sql), sql)
    return _translate_idioms(sql)


# A SELECT that starts a statement of a PL/pgSQL body (#1650) -- not a
# subquery's, not an INSERT's.
_PLSQL_STATEMENT_SELECT = re.compile(
    r'(?is)(?:^|;|\b(?:BEGIN|THEN|ELSE|LOOP|EXCEPTION)\b)\s*SELECT\b'
)
# What the scan from such a SELECT looks at: a parenthesis, the end of the
# statement, or the select list's INTO / FROM.
_SELECT_INTO_SCAN = re.compile(r'(?i)[();]|\bINTO\b|\bFROM\b')
# A statement that carries a PL/pgSQL body: a DO block, a routine, or a
# package's statements, which open with one.
_PLPGSQL_STATEMENT = re.compile(r'(?is)^\s*(?:DO\b|CREATE\b)|\bLANGUAGE\s+plpgsql\b')


def _strict_select_into(sql: str) -> str:
    """A PL/SQL ``SELECT ... INTO`` as PL/pgSQL's ``INTO STRICT`` (#1650).

    PL/SQL's needs exactly one row: none is NO_DATA_FOUND (ORA-01403), more
    TOO_MANY_ROWS (ORA-01422). PL/pgSQL's plain INTO takes the first row or
    leaves its targets NULL and raises nothing; STRICT raises the two
    conditions Oracle does. Only a statement with a PL/pgSQL body is touched:
    a top-level SELECT ... INTO is PostgreSQL's CREATE TABLE AS.
    """
    if not _PLPGSQL_STATEMENT.search(sql):
        return sql
    (masked, contents) = _mask_quoted(sql)
    inserts = []
    for start in _PLSQL_STATEMENT_SELECT.finditer(masked):
        depth = 0
        for token in _SELECT_INTO_SCAN.finditer(masked, start.end()):
            word = token.group(0).upper()
            if word == '(':
                depth += 1
            elif word == ')':
                depth -= 1
                if depth < 0:
                    break
            elif word == ';' or (depth == 0 and word == 'FROM'):
                break
            elif depth == 0 and word == 'INTO':
                if not re.match(r'(?i)\s+STRICT\b', masked[token.end() :]):
                    inserts.append(token.end())
                break
    if not inserts:
        return sql
    for at in reversed(inserts):
        masked = masked[:at] + ' STRICT' + masked[at:]
    return _unmask_quoted(masked, contents)


# %ROWCOUNT (#1608) in a PL/pgSQL body: the attribute, a dollar-quoted body, a
# SQL statement that starts a statement of one (after which SQL%ROWCOUNT is
# its row count), and a cursor's OPEN and FETCH.
_ROWCOUNT_ATTRIBUTE = re.compile(r'(?i)(?<![\w$#"])([A-Za-z_][\w$#]*)\s*%\s*ROWCOUNT\b')
_DOLLAR_BODY = re.compile(r'(?s)(\$[A-Za-z_]*\$)(.*?)\1')
_ROWCOUNT_STATEMENT = re.compile(
    r'(?is)(?:^|;|\b(?:BEGIN|THEN|ELSE|LOOP)\b)\s*'
    r'(INSERT|UPDATE|DELETE|MERGE|SELECT|EXECUTE|OPEN|FETCH)\b'
)
_ROWCOUNT_CURSOR = re.compile(r'(?is)\s*(?:OPEN|FETCH)\s+([A-Za-z_][\w$#]*)')


def _statement_close(body: str, start: int) -> int | None:
    # The `;` that ends the statement at `start`, past parentheses; None if none.
    depth = 0
    for i in range(start, len(body)):
        if body[i] == '(':
            depth += 1
        elif body[i] == ')':
            depth -= 1
        elif body[i] == ';' and depth == 0:
            return i
    return None


def _rowcount_body(body: str) -> str:
    # One PL/pgSQL body with its %ROWCOUNT attributes kept (#1608).
    cursors = {
        m.group(1).lower()
        for m in _ROWCOUNT_ATTRIBUTE.finditer(body)
        if m.group(1).upper() != 'SQL'
    }
    sql_count = any(
        m.group(1).upper() == 'SQL' for m in _ROWCOUNT_ATTRIBUTE.finditer(body)
    )
    inserts: list[tuple[int, str]] = []
    for m in _ROWCOUNT_STATEMENT.finditer(body):
        word = m.group(1).upper()
        close = _statement_close(body, m.start(1))
        if close is None:
            continue
        if word in ('OPEN', 'FETCH'):
            cursor = _ROWCOUNT_CURSOR.match(body, m.start(1))
            name = cursor.group(1).lower() if cursor else ''
            if name not in cursors:
                continue
            counter = f'ora_rowcount_{name}'
            if word == 'OPEN':
                inserts.append((close + 1, f' {counter} := 0;'))
            elif not re.search(r'(?i)\bBULK\b', body[m.start(1) : close]):
                inserts.append(
                    (close + 1, f' IF FOUND THEN {counter} := {counter} + 1; END IF;')
                )
        elif sql_count:
            inserts.append(
                (close + 1, ' GET DIAGNOSTICS ora_sql_rowcount = ROW_COUNT;')
            )
    for at, text in sorted(inserts, reverse=True):
        body = body[:at] + text + body[at:]
    body = _ROWCOUNT_ATTRIBUTE.sub(
        lambda m: (
            'ora_sql_rowcount'
            if m.group(1).upper() == 'SQL'
            else f'ora_rowcount_{m.group(1).lower()}'
        ),
        body,
    )
    declared = ('ora_sql_rowcount integer; ' if sql_count else '') + ''.join(
        f'ora_rowcount_{name} integer; ' for name in sorted(cursors)
    )
    stripped = body.lstrip()
    lead = body[: len(body) - len(stripped)]
    if stripped[:7].upper() == 'DECLARE':
        return f'{lead}DECLARE {declared}{stripped[7:]}'
    return f'{lead}DECLARE {declared}{stripped}'


def _rowcount_attributes(sql: str) -> str:
    """PL/SQL's %ROWCOUNT in a PL/pgSQL body (#1608). SQL%ROWCOUNT is the row
    count of the body's last SQL statement -- an INSERT, UPDATE, DELETE, MERGE,
    SELECT INTO or EXECUTE -- read with GET DIAGNOSTICS after each, NULL before
    any; a cursor's is the rows fetched since it was opened, counted after each
    FETCH that found one. PL/pgSQL keeps neither, so the body keeps them in
    variables of its own.
    """
    if not _PLPGSQL_STATEMENT.search(sql) or not _ROWCOUNT_ATTRIBUTE.search(sql):
        return sql
    (masked, contents) = _mask_quoted(sql)
    rewritten = _DOLLAR_BODY.sub(
        lambda m: (
            m.group(1) + _rowcount_body(m.group(2)) + m.group(1)
            if _ROWCOUNT_ATTRIBUTE.search(m.group(2))
            else m.group(0)
        ),
        masked,
    )
    if rewritten == masked:
        return sql
    return _unmask_quoted(rewritten, contents)


def _translate_idioms(sql: str) -> str:
    """Rewrite the Oracle SQL functions / literal idioms the suite uses to their
    PostgreSQL equivalents (#502). Applied to every statement. Each step that
    changes the text is noted by name for the translation report (#1557)."""
    sql = _ruled('hash-identifier', _quote_hash_identifiers(sql), sql)
    # Not a rule: it types a select-list literal as Oracle describes one, which
    # portable SQL needs as much as Oracle SQL does.
    sql = _cast_literal_items(sql)
    sql = _ruled('row-generator', _translate_row_generator(sql), sql)
    sql = _ruled('rownum', _rewrite_rownum(sql), sql)
    sql = _ruled('deref', _translate_deref(sql), sql)
    sql = _ruled('attribute-access', _translate_attribute_access(sql), sql)
    sql = _ruled('connect-by', _translate_connect_by(sql), sql)
    sql = _ruled('signed-year', _translate_signed_year(sql), sql)
    sql = _ruled('rr-year', _translate_rr_year(sql), sql)
    sql = _ruled('decode', _translate_decode(sql), sql)
    # The rewrites change Oracle words into PostgreSQL ones, and a string
    # literal or a quoted identifier holding such a word is data, not SQL:
    # `data_type = 'VARCHAR2'` was rewritten to `= 'varchar'` and matched
    # nothing (#1481). Each rule runs with the quoted regions masked, but for one
    # whose own pattern reads into a literal (a negative INTERVAL '-...').
    for name, pattern, replacement in _IDIOM_REWRITES:
        if "'" in pattern.pattern:
            sql = _ruled(name, pattern.sub(replacement, sql), sql)
            continue
        (masked, contents) = _mask_quoted(sql)
        sql = _ruled(
            name, _unmask_quoted(pattern.sub(replacement, masked), contents), sql
        )
    sql = _ruled('timestamp-tz-literal', _TSTZ_LITERAL.sub(_tstz_literal_sub, sql), sql)
    sql = _ruled('select-into-strict', _strict_select_into(sql), sql)
    sql = _ruled('rowcount-attribute', _rowcount_attributes(sql), sql)
    return _ruled('cursor-expression', _translate_cursor_expressions(sql), sql)


# Column types that are Oracle-only *for the version the Mirror advertises*
# (11.2) — native JSON is 21c+, VECTOR and BOOLEAN are 23ai+. The Mirror pins
# field version 11.2, so a real Oracle at that version rejects such a column with
# ORA-00902 (invalid datatype). PostgreSQL would instead accept JSON / BOOLEAN
# and reject VECTOR as an unknown type, so the suite's version guards (which skip
# on ORA-00902) never fired. Reject them here so those tests skip exactly as they
# do against a real pre-21c/23ai Oracle, rather than failing on a value the
# backend can't faithfully represent (#504). This is the honest ceiling: a
# PostgreSQL backend behind an 11.2 Mirror does not offer these types.
_ORACLE_ONLY_DDL_TYPES = re.compile(r'\b(JSON|VECTOR|BOOLEAN)\b', re.IGNORECASE)
# An IS [NOT] JSON condition -- a CHECK constraint's, which 12.1 has on a
# VARCHAR2 / CLOB / BLOB column -- names no JSON column (#1614).
_IS_JSON_PREDICATE = re.compile(
    r'\bIS\s+(?:NOT\s+)?JSON\b(?:\s+FORMAT\s+JSON\b)?', re.IGNORECASE
)


# A SQL domain (CREATE DOMAIN) is 23ai — the 11.2 Mirror's server doesn't know the
# command, so a real one raises ORA-00901 ("invalid CREATE command"). PostgreSQL
# *does* have CREATE DOMAIN, so without this it would run (and then fail on the
# Oracle type name), never letting the suite's version guard skip. Reject it with
# ORA-00901 so the SQL-domain test skips exactly as on a pre-23ai server (#512).
_IS_CREATE_DOMAIN = re.compile(r'\s*CREATE\s+DOMAIN\b', re.IGNORECASE)


def _reject_unsupported_ddl_types(sql: str) -> None:
    if _IS_CREATE_DOMAIN.match(sql):
        raise BackendError(
            'invalid CREATE command: SQL domains need a 23ai server',
            ora_code=ORA_INVALID_CREATE_COMMAND,
        )
    if not _IS_CREATE_TABLE.match(sql):
        return
    match = _ORACLE_ONLY_DDL_TYPES.search(_IS_JSON_PREDICATE.sub(' ', sql))
    if match is not None:
        raise BackendError(
            f'invalid datatype: {match.group(1).upper()} is not available on '
            f'this server version',
            ora_code=ORA_INVALID_DATATYPE,
        )


# --- PL/SQL: CREATE PROCEDURE / FUNCTION and callproc / callfunc (#503) ---------

# Oracle `CREATE [OR REPLACE] PROCEDURE|FUNCTION name (params) [RETURN t] AS|IS
# <body>`. The signature is close to PostgreSQL's; the body (BEGIN … END) is
# valid PL/pgSQL for the simple assignment / RETURN cases the suite uses. The
# parameter list ends at the parenthesis matching its opening one, found apart
# from the pattern: a greedy `\((.*)\)` ran on to the last `) AS` of the BODY --
# `count(*) AS n` -- and split the routine there (#1467).
_ROUTINE_HEAD = re.compile(
    r'(?is)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(PROCEDURE|FUNCTION)\s+([\w.]+)\s*'
)
_ROUTINE_TAIL = re.compile(
    r'(?is)\s*(?:RETURN\s+([\w ]+?)\s+)?(?:AS|IS)\s+(.*?)\s*;?\s*$'
)
# CREATE [OR REPLACE] TYPE t ... -- not a TYPE BODY -- whose failure to compile
# leaves an invalid type behind in Oracle (#1499).
_INVALID_TYPE_DDL = re.compile(
    r'(?is)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?TYPE\s+(?!BODY\b)([\w.$#]+)'
)
# The codes a DDL statement that does not compile maps to: PostgreSQL's class 42
# (syntax, unknown object, unknown type) and the PL/SQL compile error (#1499).
_COMPILE_ERROR_CODES = frozenset(
    {
        ORA_INVALID_SQL_STATEMENT,
        ORA_INVALID_IDENTIFIER,
        ORA_TABLE_OR_VIEW_DOES_NOT_EXIST,
        ORA_INVALID_DATATYPE,
        ORA_PLSQL_COMPILATION_ERROR,
    }
)
# Oracle parameter direction `IN OUT` → PostgreSQL `INOUT` (do this before the
# type rewrites, which share the DDL type list).
_PARAM_IN_OUT = re.compile(r'\bIN\s+OUT\b', re.IGNORECASE)


# PL/SQL's own integer types, which a routine's parameters and locals and a
# block's locals use and no table column can (#1604).
_PLSQL_INTEGER_TYPE = re.compile(r'\b(?:PLS|BINARY)_INTEGER\b', re.IGNORECASE)
# A table's row type, `tab%ROWTYPE`: PostgreSQL's composite of the table (#1607).
_ROWTYPE = re.compile(r'\b([A-Za-z_][\w$#.]*)%ROWTYPE\b', re.IGNORECASE)
# A parameter's NOCOPY, a hint to Oracle to pass by reference (#1604).
_NOCOPY = re.compile(r'\bNOCOPY\s+', re.IGNORECASE)
# What may follow a routine body's closing END: its name, which PL/pgSQL would
# read as a block label (#1604).
_END_LABEL = re.compile(r'\s*(?:[A-Za-z_][\w$#]*)?\s*')


# An explicit cursor's declaration, `CURSOR c [(params)] [RETURN type] IS`
# (#1665), which PL/pgSQL spells `c CURSOR [(params)] FOR`.
_CURSOR_DECLARATION = re.compile(
    r'(?is)\bCURSOR\s+(\w+)\s*(\((?:[^()]|\([^()]*\))*\))?\s*'
    r'(?:RETURN\s+[\w$#.%]+\s+)?IS\b'
)
# A cursor parameter's IN, which PL/pgSQL's take no mode for.
_CURSOR_PARAM_IN = re.compile(r'(?i)(\w+\s+)IN\s+(?=\w)')


def _cursor_declaration(match: re.Match[str]) -> str:
    params = match.group(2)
    if params:
        params = ' ' + _CURSOR_PARAM_IN.sub(r'\1', params)
    return f'{match.group(1)} CURSOR{params or ""} FOR'


def _translate_routine_types(text: str) -> str:
    text = _CURSOR_DECLARATION.sub(_cursor_declaration, text)
    # A routine's DATE -- a parameter, a local, a package type's -- is the
    # ora_date domain, as a table's column is, so its arithmetic is Oracle's
    # (#1611).
    text = _DDL_DATE_COLUMN.sub(_DATE_TYPE, text)
    for pattern, replacement in _DDL_TYPE_REWRITES:
        text = pattern.sub(replacement, text)
    return _ROWTYPE.sub(r'\1', _PLSQL_INTEGER_TYPE.sub('integer', text))


def _routine_body(body: str) -> str:
    """A routine's PL/SQL body -- its declarations, then BEGIN ... END [name]
    -- as PL/pgSQL takes it (#1604): the declarations under DECLARE, their types
    mapped, and the END without the routine's name. A body this cannot read --
    a local routine among the declarations, an END not the last word -- is left
    as it is, to fail as before."""
    begin = next((start for word, start, _end in _words(body) if word == 'BEGIN'), None)
    if begin is None:
        return body
    if any(word in ('FUNCTION', 'PROCEDURE') for word, _s, _e in _words(body[:begin])):
        return body
    closed = _block_end(body, begin)
    if closed is None or _END_LABEL.fullmatch(body, closed[1]) is None:
        return body
    declarations = body[:begin].strip()
    block = body[begin : closed[1]]
    if not declarations:
        return block
    return f'DECLARE {_translate_routine_types(declarations)} {block}'


# The FROM of a SELECT the backend evaluates a PL/SQL expression with: it marks
# the statement as PL/SQL's for sys.ora_ndf_to_null() (#1612).
_PLSQL_CALL_MARK: Final = 'sys.ora_plsql_call() AS ora_plsql_call'

# What lets a routine's body raise NO_DATA_FOUND by itself (#1612): a SELECT
# ... INTO, an index-by table read (`t$get`, #1607) or a RAISE of it. A call
# to another user routine can pass one up; those are matched by name.
_RAISES_NO_DATA_FOUND = re.compile(
    r'(?is)\bSELECT\b[^;]*?\bINTO\b|\$get\s*\(|\bRAISE\s+NO_DATA_FOUND\b'
)


def _guards_no_data_found(name: str, body: str, user_routines: frozenset[str]) -> bool:
    # Whether a function's body can raise NO_DATA_FOUND, so that it needs the
    # handler that makes it NULL in SQL (#1612): by itself, or through a call
    # to a user routine -- or to itself -- which the handler cannot see past.
    (masked, _contents) = _mask_quoted(body)
    if _RAISES_NO_DATA_FOUND.search(masked):
        return True
    called = {w.lower() for w in _IDENTIFIER.findall(masked)}
    own = name.lower().rpartition('.')[2]
    return own in called or not called.isdisjoint(user_routines)


def _guarded_body(block: str) -> str:
    # A function body with the NO_DATA_FOUND handler around it (#1612). The
    # handler is a subtransaction per call, which is why only a body that can
    # raise one gets it.
    return (
        f'BEGIN {block}; EXCEPTION WHEN no_data_found THEN '
        'IF sys.ora_ndf_to_null() THEN RETURN NULL; END IF; RAISE; END'
    )


def _routine_mark(name: str, status: str) -> str:
    # The statement that marks every routine of `name` -- one, as the routine
    # DDL drops it by name first -- as a user's, VALID or INVALID (#1606).
    (schema, _dot, routine) = name.lower().rpartition('.')
    namespace = (
        f"'{schema}'::regnamespace" if schema else 'current_schema()::regnamespace'
    )
    return (
        'DO $m$ DECLARE r regprocedure; BEGIN FOR r IN SELECT p.oid::regprocedure '
        f"FROM pg_proc p WHERE p.proname = '{routine}' AND p.pronamespace = {namespace} "
        "LOOP EXECUTE format('COMMENT ON ROUTINE %s IS %L', r, "
        f"'{_ROUTINE_MARK}{status}'); END LOOP; END $m$"
    )


def _translate_routine_ddl(
    sql: str, user_routines: frozenset[str] | None = None, mark: bool = True
) -> str:
    """Rewrite an Oracle ``CREATE PROCEDURE`` / ``CREATE FUNCTION`` to a PL/pgSQL
    routine (#503): translate the parameter types + ``IN OUT`` → ``INOUT``, map
    ``RETURN t`` → ``RETURNS t``, and wrap the ``BEGIN … END`` body as a
    ``LANGUAGE plpgsql`` dollar-quoted body. Non-routine SQL is unchanged."""
    head = _ROUTINE_HEAD.match(sql)
    if head is None:
        return sql
    kind, name = head.groups()
    params, rest = None, head.end()
    if sql.startswith('(', rest):
        close = _matching_paren(sql, rest)
        params, rest = sql[rest + 1 : close], close + 1
    tail = _ROUTINE_TAIL.match(sql, rest)
    if tail is None:
        return sql
    return_type, body = tail.groups()
    # Oracle allows a routine with no parameters to omit the list entirely
    # (FUNCTION f RETURN NUMBER AS …); PostgreSQL always needs the parentheses, so
    # an absent list (params is None) becomes an empty one (#530).
    params = _translate_routine_types(
        _NOCOPY.sub('', _PARAM_IN_OUT.sub('INOUT', params or ''))
    )
    header = f'CREATE OR REPLACE {kind.upper()} {name}({params})'
    if kind.upper() == 'FUNCTION' and return_type:
        header += f' RETURNS {_translate_routine_types(return_type.strip())}'
    # Oracle's CREATE OR REPLACE freely redefines a routine, but PostgreSQL's
    # refuses to change an existing routine's OUT-parameter row type or return type
    # ("cannot change return type of existing function"). The suite reuses one
    # routine name across tests with different signatures, so drop any prior
    # definition first — by name (the suite never overloads, so it is unambiguous),
    # IF EXISTS so the first CREATE is fine (#521).
    drop = f'DROP {kind.upper()} IF EXISTS {name};'
    block = _routine_body(body)
    if (
        kind.upper() == 'FUNCTION'
        and user_routines is not None
        and (block.split(None, 1) or [''])[0].upper() in ('BEGIN', 'DECLARE')
        and _guards_no_data_found(name, body, user_routines)
    ):
        block = _guarded_body(block)
    created = f'{drop} {header} LANGUAGE plpgsql AS $$ {block} $$'
    # A package's member is the package's, listed with it (#1605).
    return f'{created}; {_routine_mark(name, "VALID")}' if mark else created


# --- PL/SQL packages (#1605) ---------------------------------------------------

# CREATE [OR REPLACE] [EDITIONABLE] PACKAGE [BODY] [owner.]name AS|IS, and DROP
# PACKAGE [BODY] [owner.]name. A package is a schema of its name: a caller names
# a member as package.member, which PostgreSQL reads as schema.routine.
_PACKAGE_HEAD = re.compile(
    r'(?is)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:NON)?EDITIONABLE\s+)?PACKAGE\s+'
    r'(BODY\s+)?(?:([A-Za-z_][\w$#]*)\.)?([A-Za-z_][\w$#]*)\s+(?:AS|IS)\b'
)
_DROP_PACKAGE = re.compile(
    r'(?is)^\s*DROP\s+PACKAGE\s+(BODY\s+)?(?:([A-Za-z_][\w$#]*)\.)?'
    r'([A-Za-z_][\w$#]*)\s*;?\s*$'
)
_PLSQL_WORD = re.compile(r'[A-Za-z_][\w$#]*')
_MEMBER_RETURN = re.compile(r'(?is)\s*RETURN\s+(.+?)\s*(?=;|\b(?:IS|AS)\b)')
_MEMBER_TAIL = re.compile(r'(?is)\s*(;|(?:IS|AS)\b)')
_MEMBER_END = re.compile(r'\s*(?:[A-Za-z_][\w$#]*)?\s*;')
_PACKAGE_END = re.compile(r'\s*(?:[A-Za-z_][\w$#]*)?\s*;?\s*$')


class _PackageMember(NamedTuple):
    kind: str  # FUNCTION / PROCEDURE, or TYPE / SUBTYPE
    name: str
    params: str | None
    returns: str | None
    body: str | None  # a routine's body, None for its declaration; a type's definition


# A package's TYPE or SUBTYPE declaration: its name, and what follows IS (#1607).
_TYPE_DECLARATION = re.compile(
    r'(?is)\s*(TYPE|SUBTYPE)\s+([A-Za-z_][\w$#]*)\s+IS\s+(.*?)\s*'
)


def _blank_plsql(text: str) -> str:
    # `text` with each comment and string literal's contents blanked, the same
    # length, so a ; or a parenthesis inside one is not read as structure.
    out: list[str] = []
    for token in sql_tokens(text):
        piece = text[token.start : token.end]
        if token.kind == 'comment':
            piece = ' ' * len(piece)
        elif token.kind == 'string':
            # The prefix and the quotes stay, so it still reads as a literal.
            opening = piece.index("'") + 1
            closing = len(piece) if len(piece) > opening and piece.endswith("'") else 0
            end = closing - 1 if closing else len(piece)
            piece = piece[:opening] + ' ' * (end - opening) + piece[end:]
        out.append(piece)
    return ''.join(out)


def _statement_end(blanked: str, pos: int) -> int | None:
    # The ; that ends the declaration at `pos`, outside parentheses.
    depth = 0
    for i in range(pos, len(blanked)):
        char = blanked[i]
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == ';' and depth == 0:
            return i
    return None


def _package_members(text: str, pos: int) -> list[_PackageMember] | None:
    """The routines a package spec declares or a body defines, from `pos` (past
    AS|IS) to the package's END (#1605), and the types it declares (#1607). The
    rest of a package -- its variables, cursors, pragmas -- is passed over: a
    member that uses one fails to compile, and the package with it. None for what
    this cannot read: an initialization section, a member with local routines of
    its own."""
    blanked = _blank_plsql(text)
    members: list[_PackageMember] = []
    while True:
        pos = len(blanked) - len(blanked[pos:].lstrip())
        word = _PLSQL_WORD.match(blanked, pos)
        if word is None:
            return None
        keyword = word.group().upper()
        if keyword == 'END':
            return members if _PACKAGE_END.fullmatch(blanked, word.end()) else None
        if keyword == 'BEGIN':
            return None  # an initialization section
        if keyword not in ('FUNCTION', 'PROCEDURE'):
            end = _statement_end(blanked, pos)
            if end is None:
                return None
            declared = _TYPE_DECLARATION.fullmatch(blanked, pos, end)
            if declared is not None:
                definition = text[declared.start(3) : declared.end(3)]
                members.append(
                    _PackageMember(
                        declared.group(1).upper(),
                        declared.group(2),
                        None,
                        None,
                        definition,
                    )
                )
            pos = end + 1
            continue
        name = _PLSQL_WORD.match(
            blanked, len(blanked) - len(blanked[word.end() :].lstrip())
        )
        if name is None:
            return None
        at = len(blanked) - len(blanked[name.end() :].lstrip())
        params = None
        if blanked.startswith('(', at):
            close = _matching_paren(blanked, at)
            params, at = text[at + 1 : close], close + 1
        returns = _MEMBER_RETURN.match(blanked, at)
        if returns is not None:
            at = returns.end()
        tail = _MEMBER_TAIL.match(blanked, at)
        if tail is None:
            return None
        returned = text[returns.start(1) : returns.end(1)] if returns else None
        if tail.group(1) == ';':
            members.append(
                _PackageMember(keyword, name.group(), params, returned, None)
            )
            pos = tail.end()
            continue
        begin = next(
            (start for w, start, _e in _words(blanked, tail.end()) if w == 'BEGIN'),
            None,
        )
        if begin is None or any(
            w in ('FUNCTION', 'PROCEDURE')
            for w, _s, _e in _words(blanked[:begin], tail.end())
        ):
            return None
        closed = _block_end(blanked, begin)
        after = _MEMBER_END.match(blanked, closed[1]) if closed else None
        if closed is None or after is None:
            return None
        members.append(
            _PackageMember(
                keyword, name.group(), params, returned, text[tail.end() : closed[1]]
            )
        )
        pos = after.end()


_RECORD_TYPE = re.compile(r'(?is)RECORD\s*\((.*)\)')
_INDEX_BY_TYPE = re.compile(r'(?is)TABLE\s+OF\s+(.+?)\s+INDEX\s+BY\s+(.+)')
_NESTED_TABLE_TYPE = re.compile(r'(?is)TABLE\s+OF\s+(.+?)(?:\s+NOT\s+NULL)?')
_VARRAY_TYPE = re.compile(
    r'(?is)(?:VARRAY|VARYING\s+ARRAY)\s*\(\s*(\d+)\s*\)\s+OF\s+(.+?)'
    r'(?:\s+NOT\s+NULL)?'
)
_REF_CURSOR_TYPE = re.compile(r'(?is)REF\s+CURSOR\b.*')
# An index-by table keyed by a string, not an integer.
_STRING_KEY = re.compile(r'(?is)(?:N?VARCHAR2?|STRING|LONG)\b')
# What may follow a declared type: a constraint, a default, a range.
_DECLARED_EXTRAS = re.compile(r'(?is)\s+(?:NOT\s+NULL|RANGE|:=|DEFAULT)\b.*$')


def _package_type(package: str, member: _PackageMember) -> str | None:
    """A package's TYPE or SUBTYPE as a PostgreSQL type of its schema (#1607):

    - a RECORD a composite of its fields;
    - an index-by table a composite of two arrays, ``keys`` and ``vals``, the
      keys integers or, for a table indexed by a string, text -- sparse keys,
      ordered, the element's own type kept;
    - a nested table or VARRAY a domain over an array, as a schema's is, with
      its constructors;
    - a REF CURSOR a domain over refcursor, a SUBTYPE a domain over its type.

    Their parts name types as a routine's parameters do, ``tab%ROWTYPE`` the
    table's composite; another of the package's types resolves in its schema.
    None for a definition this does not read."""
    name = f'{package}.{member.name}'
    definition = member.body or ''
    if member.kind == 'SUBTYPE':
        base = _DECLARED_EXTRAS.sub('', definition)
        return f'CREATE DOMAIN {name} AS {_translate_routine_types(base)}'
    record = _RECORD_TYPE.fullmatch(definition)
    if record is not None:
        fields = []
        for start, end in _top_level_items(record.group(1), 0, len(record.group(1))):
            field = _DECLARED_EXTRAS.sub('', record.group(1)[start:end].strip())
            field_name, _space, field_type = field.partition(' ')
            if not field_type.strip():
                return None
            fields.append(
                f'{field_name} {_translate_routine_types(field_type.strip())}'
            )
        return f'CREATE TYPE {name} AS ({", ".join(fields)})'
    indexed = _INDEX_BY_TYPE.fullmatch(definition)
    if indexed is not None:
        element = _translate_routine_types(_DECLARED_EXTRAS.sub('', indexed.group(1)))
        key = 'text' if _STRING_KEY.match(indexed.group(2).strip()) else 'integer'
        return f'CREATE TYPE {name} AS (keys {key}[], vals {element}[]); ' + (
            _index_table_methods(name, key, element)
        )
    varray = _VARRAY_TYPE.fullmatch(definition)
    if varray is not None:
        element = _translate_routine_types(varray.group(2))
        return (
            f'CREATE DOMAIN {name} AS {element}[] CHECK '
            f'(VALUE IS NULL OR array_length(VALUE, 1) <= {varray.group(1)})'
            + _collection_constructors(name, element)
        )
    nested = _NESTED_TABLE_TYPE.fullmatch(definition)
    if nested is not None:
        element = _translate_routine_types(nested.group(1))
        return f'CREATE DOMAIN {name} AS {element}[]' + _collection_constructors(
            name, element
        )
    if _REF_CURSOR_TYPE.fullmatch(definition):
        return f'CREATE DOMAIN {name} AS refcursor'
    return None


# A built-in type as a package type's field or element names it: its name, an
# optional (length | precision[, scale] [CHAR|BYTE]), and what follows.
_PLSQL_BUILTIN = re.compile(
    r'(?is)\s*(TIMESTAMP\s*(?:\(\s*\d+\s*\))?\s+WITH\s+LOCAL\s+TIME\s+ZONE'
    r'|TIMESTAMP\s*(?:\(\s*\d+\s*\))?\s+WITH\s+TIME\s+ZONE|NUMBER|INTEGER|INT'
    r'|SMALLINT|FLOAT|REAL|BINARY_FLOAT|BINARY_DOUBLE|N?VARCHAR2|VARCHAR|N?CHAR'
    r'|DATE|TIMESTAMP|BOOLEAN|PLS_INTEGER|BINARY_INTEGER|N?CLOB|BLOB|RAW)\b'
    r'\s*(?:\(\s*(\d+|\*)\s*(?:,\s*(-?\d+))?\s*(CHAR|BYTE)?\s*\))?\s*'
)
# The name 23ai's dictionary gives a built-in that is not its own spelling.
_PLSQL_BUILTIN_NAMES = {
    'PLS_INTEGER': 'PL/SQL PLS INTEGER',
    'BINARY_INTEGER': 'PL/SQL BINARY INTEGER',
    'VARCHAR': 'VARCHAR2',
    'INT': 'INTEGER',
}


def _plsql_type_ref(text: str, package: str, siblings: dict[str, dict]) -> dict:
    """What a package type's field or element names (#1607): a built-in -- its
    dictionary name, length, precision, scale, character set -- or a named type,
    another of the package's, a schema's, another package's (``pkg.t``) or a
    table's row (``tab%ROWTYPE``). A SUBTYPE of the package stands for its base."""
    text = _DECLARED_EXTRAS.sub('', text.strip())
    builtin = _PLSQL_BUILTIN.fullmatch(text)
    if builtin is not None:
        word = re.sub(r'\s+', ' ', builtin.group(1).upper())
        size = builtin.group(2)
        number = int(size) if size and size != '*' else None
        ref: dict = {'named': False, 'package': None}
        if word.startswith('TIMESTAMP') and 'ZONE' in word:
            ref['name'] = (
                'TIMESTAMP WITH LOCAL TZ' if 'LOCAL' in word else 'TIMESTAMP WITH TZ'
            )
            precision = re.search(r'\((\d+)\)', word)
            ref['scale'] = int(precision.group(1)) if precision else 6
            return ref
        ref['name'] = _PLSQL_BUILTIN_NAMES.get(word, word)
        if word in ('VARCHAR2', 'VARCHAR', 'CHAR', 'NVARCHAR2', 'NCHAR', 'RAW'):
            ref['length'] = number if number is not None else 1
            if word != 'RAW':
                national = word.startswith('N')
                ref['charset'] = 'NCHAR_CS' if national else 'CHAR_CS'
                ref['char_used'] = 'C' if national or builtin.group(4) else 'B'
        elif word == 'TIMESTAMP':
            ref['scale'] = number if number is not None else 6
        elif word in ('NUMBER', 'FLOAT'):
            ref['precision'] = number
            if builtin.group(3) is not None:
                ref['scale'] = int(builtin.group(3))
        return ref
    rowtype = re.fullmatch(r'(?i)([A-Za-z_][\w$#.]*)%ROWTYPE', text)
    if rowtype is not None:
        table = rowtype.group(1).rpartition('.')[2]
        return {'named': True, 'package': None, 'name': f'{table.upper()}%ROWTYPE'}
    (qualifier, _dot, name) = text.rpartition('.')
    if not qualifier and name.upper() in siblings:
        sibling = siblings[name.upper()]
        if 'subtype' in sibling:
            return sibling['subtype']
        return {'named': True, 'package': package.upper(), 'name': name.upper()}
    return {
        'named': True,
        'package': qualifier.upper() or None,
        'name': name.upper(),
    }


def _plsql_ref_is_plsql(ref: dict) -> bool:
    # Whether a field or element is PL/SQL's own: a record, a row, a PL/SQL
    # integer -- what makes a type's CONTAINS_PLSQL YES, as 23ai reports it.
    if not ref.get('named'):
        return str(ref.get('name', '')).startswith('PL/SQL ')
    return ref.get('package') is not None or str(ref['name']).endswith('%ROWTYPE')


def _plsql_type_meta(
    package: str, member: _PackageMember, siblings: dict[str, dict]
) -> dict | None:
    """A package type's declaration, read for the dictionary and a client's
    type metadata (#1607): a record's fields, a collection's kind, bound, key
    and element; a SUBTYPE what it stands for. None for a declaration that is
    none of these."""
    definition = _DECLARED_EXTRAS.sub('', member.body or '')
    if member.kind == 'SUBTYPE':
        return {'subtype': _plsql_type_ref(definition, package, siblings)}
    record = _RECORD_TYPE.fullmatch(definition)
    if record is not None:
        fields: list[dict] = []
        refs: list[dict] = []
        for start, end in _top_level_items(record.group(1), 0, len(record.group(1))):
            (name, _space, declared) = record.group(1)[start:end].strip().partition(' ')
            ref = _plsql_type_ref(declared, package, siblings)
            refs.append(ref)
            fields.append({'name': name.upper(), 'type': ref})
        plsql = any(_plsql_ref_is_plsql(r) or r.get('name') == 'BOOLEAN' for r in refs)
        return {
            'typecode': 'PL/SQL RECORD',
            'attributes': len(fields),
            'contains_plsql': 'YES' if plsql else 'NO',
            'attrs': fields,
        }
    meta: dict = {'typecode': 'COLLECTION', 'attributes': 0, 'upper_bound': None}
    indexed = _INDEX_BY_TYPE.fullmatch(definition)
    varray = _VARRAY_TYPE.fullmatch(definition)
    nested = _NESTED_TABLE_TYPE.fullmatch(definition)
    if indexed is not None:
        (element, key) = (indexed.group(1), indexed.group(2).strip())
        key_word = _PLSQL_WORD.match(key)
        meta.update(
            coll_type='PL/SQL INDEX TABLE',
            index_by=key_word.group().upper() if key_word else None,
        )
    elif varray is not None:
        element = varray.group(2)
        meta.update(coll_type='VARYING ARRAY', upper_bound=int(varray.group(1)))
    elif nested is not None:
        element = nested.group(1)
        meta.update(coll_type='TABLE')
    else:
        return None
    meta['elem'] = _plsql_type_ref(element, package, siblings)
    meta['contains_plsql'] = 'YES' if _plsql_ref_is_plsql(meta['elem']) else 'NO'
    return meta


def _index_table_methods(name: str, key: str, element: str) -> str:
    """The methods of an index-by table type (#1607), functions over its keys
    and values, the keys kept sorted -- by number, or byte by byte for a string
    key, as Oracle orders them -- so FIRST / NEXT walk them in order:

    - ``$get(t, k)`` the element, ORA-01403 for a key not there;
    - ``$set(t, k, v)`` the table with ``k`` set, NULL taken as empty;
    - ``$count``, ``$first``, ``$last``, ``$next(t, k)``, ``$prior(t, k)``,
      ``$exists(t, k)`` and ``$delete(t [, k])``.
    """
    order = ' COLLATE "C"' if key == 'text' else ''
    # Each key with its element by subscript: a two-array unnest would spread a
    # record element over columns of its own.
    pairs = (
        '(SELECT ($1).keys[sub] AS k, ($1).vals[sub] AS v '
        'FROM generate_subscripts(($1).keys, 1) sub) u'
    )
    sql = 'LANGUAGE sql IMMUTABLE'
    return '; '.join(
        (
            # A key's slot: by arithmetic where the keys are the dense run 1..N
            # (or any run without a gap) a classic array or a loop builds,
            # else by array_position -- either O(1) per element in PL/SQL's
            # `for i in 1..a.count loop ... a(i)`, which a scan made quadratic.
            f'CREATE FUNCTION {name}$get({name}, {key}) RETURNS {element} '
            'LANGUAGE plpgsql IMMUTABLE AS $f$ DECLARE ks '
            + key
            + '[] := ($1).keys; n integer := coalesce(cardinality(ks), 0); '
            'i integer; BEGIN '
            + (
                'IF n > 0 AND ks[n] - ks[1] = n - 1 THEN i := $2 - ks[1] + 1; ELSE '
                if key == 'integer'
                else ''
            )
            + 'i := array_position(ks, $2); '
            + ('END IF; ' if key == 'integer' else '')
            + 'IF i IS NULL OR i < 1 OR i > n OR ks[i] IS DISTINCT FROM $2 THEN '
            "RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'no data found'; "
            'END IF; RETURN ($1).vals[i]; END $f$',
            # A set past the last key, or of a key there already, in place;
            # only a key between two others rebuilds the table in order.
            f'CREATE FUNCTION {name}$set({name}, {key}, {element}) RETURNS {name} '
            'LANGUAGE plpgsql IMMUTABLE AS $f$ DECLARE ks '
            + key
            + '[] := coalesce(($1).keys, '
            + "'{}'); vs "
            + element
            + "[] := coalesce(($1).vals, '{}'); n integer := cardinality(ks); "
            'i integer; r ' + name + '; BEGIN '
            f'IF n = 0 OR $2{order} > ks[n]{order} THEN '
            'r.keys := ks || $2; r.vals := vs || $3; RETURN r; END IF; '
            'i := array_position(ks, $2); '
            'IF i IS NOT NULL THEN vs[i] := $3; r.keys := ks; r.vals := vs; '
            'RETURN r; END IF; '
            f'SELECT array_agg(x.k ORDER BY x.k{order}), '
            f'array_agg(x.v ORDER BY x.k{order}) INTO r.keys, r.vals FROM '
            f'(SELECT u.k, u.v FROM {pairs} UNION ALL SELECT $2, $3) x; '
            'RETURN r; END $f$',
            f'CREATE FUNCTION {name}$count({name}) RETURNS integer {sql} AS $f$ '
            'SELECT coalesce(cardinality(($1).keys), 0) $f$',
            f'CREATE FUNCTION {name}$first({name}) RETURNS {key} {sql} AS $f$ '
            'SELECT ($1).keys[1] $f$',
            f'CREATE FUNCTION {name}$last({name}) RETURNS {key} {sql} AS $f$ '
            'SELECT ($1).keys[cardinality(($1).keys)] $f$',
            f'CREATE FUNCTION {name}$next({name}, {key}) RETURNS {key} {sql} AS $f$ '
            f'SELECT min(k{order}) FROM unnest(($1).keys) k WHERE k{order} > $2 $f$',
            f'CREATE FUNCTION {name}$prior({name}, {key}) RETURNS {key} {sql} AS $f$ '
            f'SELECT max(k{order}) FROM unnest(($1).keys) k WHERE k{order} < $2 $f$',
            f'CREATE FUNCTION {name}$exists({name}, {key}) RETURNS boolean {sql} AS $f$ '
            'SELECT coalesce($2 = ANY(($1).keys), false) $f$',
            f'CREATE FUNCTION {name}$delete({name}) RETURNS {name} {sql} AS $f$ '
            f"SELECT ROW('{{}}', '{{}}')::{name} $f$",
            f'CREATE FUNCTION {name}$delete({name}, {key}) RETURNS {name} {sql} AS $f$ '
            f'SELECT ROW(coalesce(array_agg(u.k ORDER BY u.k{order}), '
            f"'{{}}'), coalesce(array_agg(u.v ORDER BY u.k{order}), '{{}}'))::{name} "
            f'FROM {pairs} WHERE u.k <> $2 $f$',
        )
    )


# A collection method a body calls on an index-by table (#1607).
_COLLECTION_METHOD = re.compile(
    r'\s*\.\s*(COUNT|FIRST|LAST|NEXT|PRIOR|EXISTS|DELETE)\b', re.IGNORECASE
)
# What a statement starts after: where an assignment `v(k) := x` can stand.
_STATEMENT_START = re.compile(
    r'(?is)(?:^|[;]|\b(?:BEGIN|THEN|ELSE|LOOP|DECLARE|IS|AS)\b)\s*$'
)


def _rewrite_index_tables(body: str, tables: dict[str, str]) -> str:
    """A routine's body with its index-by tables' PL/SQL in PostgreSQL's
    terms (#1607): ``v(k) := x`` an assignment of ``T$set(v, k, x)``, a read
    ``v(k)`` ``T$get(v, k)``, ``v.COUNT`` ``T$count(v)``, ``v.DELETE`` an
    assignment of ``T$delete(v)``, and so on -- T the variable's type, as
    ``tables`` maps each name (lower case) to it."""
    if not tables:
        return body
    (masked, contents) = _mask_quoted(body)
    names = '|'.join(re.escape(n) for n in sorted(tables, key=len, reverse=True))
    # Not a field of something else, `x.v`; `1..v.COUNT` is a range, not one.
    use = re.compile(rf'(?<![\w$#])(?<![\w$#")]\.)({names})(?![\w$#])', re.IGNORECASE)

    def rewrite(text: str) -> str:
        out: list[str] = []
        pos = 0
        while (found := use.search(text, pos)) is not None:
            name = found.group(1)
            typ = tables[name.lower()]
            after = found.end()
            method = _COLLECTION_METHOD.match(text, after)
            if method is not None:
                kind = method.group(1).lower()
                at = method.end()
                args = ''
                stripped = len(text) - len(text[at:].lstrip())
                if text.startswith('(', stripped):
                    close = _matching_paren(text, stripped)
                    args = ', ' + rewrite(text[stripped + 1 : close])
                    at = close + 1
                call = f'{typ}${kind}({name}{args})'
                if kind == 'delete':
                    call = f'{name} := {call}'
                out.append(text[pos : found.start()] + call)
                pos = at
                continue
            stripped = len(text) - len(text[after:].lstrip())
            if not text.startswith('(', stripped):
                out.append(text[pos:after])
                pos = after
                continue
            close = _matching_paren(text, stripped)
            key = rewrite(text[stripped + 1 : close])
            assign = re.compile(r'\s*:=').match(text, close + 1)
            if assign is not None and _STATEMENT_START.search(text, 0, found.start()):
                end = _statement_end(text, assign.end())
                end = len(text) if end is None else end
                value = rewrite(text[assign.end() : end]).strip()
                out.append(
                    text[pos : found.start()]
                    + f'{name} := {typ}$set({name}, {key}, {value})'
                )
                pos = end
                continue
            out.append(text[pos : found.start()] + f'{typ}$get({name}, {key})')
            pos = close + 1
        out.append(text[pos:])
        return ''.join(out)

    return _unmask_quoted(rewrite(masked), contents)


def _routine_index_tables(
    package: str, member: _PackageMember, types: dict[str, str]
) -> dict[str, str]:
    # The parameters and locals of a member whose type is one of the package's
    # index-by tables (#1607): name (lower case) -> the PostgreSQL type.
    declared = (member.params or '').split(',')
    body = member.body or ''
    begin = next((s for w, s, _e in _words(body) if w == 'BEGIN'), None)
    if begin is not None:
        declared += body[:begin].split(';')
    tables = {}
    for declaration in declared:
        words = _PLSQL_WORD.findall(declaration)
        if len(words) < 2:
            continue
        typ = words[-1].lower()
        if typ in types:
            tables[words[0].lower()] = types[typ]
    return tables


def _package_types(package: str, members: list[_PackageMember], public: bool) -> str:
    # The DDL of a spec's or a body's types, each dropped first -- a body's
    # replace the last body's -- and each recorded with what it was declared as.
    # That is recorded hex-encoded: the rewrites that follow would make its
    # VARCHAR2 a varchar and its NVARCHAR2 one too, which the metadata tells apart.
    created: list[str] = []
    recorded: list[str] = []
    siblings: dict[str, dict] = {}
    for ord_, member in enumerate(m for m in members if m.kind in ('TYPE', 'SUBTYPE')):
        ddl = _package_type(package, member)
        if ddl is None:
            continue
        created.append(f'DROP TYPE IF EXISTS {package}.{member.name} CASCADE; {ddl}')
        declaration = f'{member.kind} {member.body}'.encode().hex()
        meta = _plsql_type_meta(package, member, siblings) or {}
        siblings[member.name.upper()] = meta
        encoded = json.dumps(meta).encode().hex()
        recorded.append(
            f"('{package}', '{member.name.upper()}', "
            f"convert_from(decode('{declaration}', 'hex'), 'UTF8'), "
            f'{"true" if public else "false"}, {ord_}, '
            f"convert_from(decode('{encoded}', 'hex'), 'UTF8')::jsonb)"
        )
    forget = f"DELETE FROM sys.ora_plsql_types WHERE package = '{package}'" + (
        '' if public else ' AND NOT public'
    )
    if not recorded:
        return forget
    return (
        '; '.join(created) + f'; {forget}; INSERT INTO sys.ora_plsql_types '
        '(package, name, declaration, public, ord, meta) VALUES ' + ', '.join(recorded)
    )


def _package_routine(
    package: str,
    member: _PackageMember,
    body: str,
    user_routines: frozenset[str] | None = None,
) -> str:
    # One member as a routine in the package's schema: the routine translation,
    # without its drop by name -- a package's members may share a name.
    params = f'({member.params})' if member.params is not None else ''
    returns = f' RETURN {member.returns}' if member.returns else ''
    name = f'{package}.{member.name}'
    translated = _translate_routine_ddl(
        f'CREATE OR REPLACE {member.kind} {name}{params}{returns} IS {body}',
        user_routines,
        mark=False,
    )
    return translated.removeprefix(f'DROP {member.kind} IF EXISTS {name}; ')


def _drop_package_routines(package: str) -> str:
    # Every routine in the package's schema, overloads and private ones too --
    # but its types' own: an index-by table's methods, `t$get`, and a
    # collection's constructors, named as the type is (#1607).
    return (
        'DO $p$ DECLARE r record; BEGIN FOR r IN SELECT p.oid::regprocedure AS '
        "sig, p.prokind FROM pg_proc p WHERE p.pronamespace = '"
        f"{package}'::regnamespace AND p.proname !~ '[$]' AND NOT EXISTS "
        '(SELECT 1 FROM pg_type t WHERE t.typnamespace = p.pronamespace '
        "AND t.typname = p.proname) LOOP EXECUTE format('DROP %s %s', CASE "
        "r.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END, r.sig); "
        'END LOOP; END $p$'
    )


def _restore_package_stubs(package: str, body: str) -> str:
    # The spec's stand-ins in place of the body's routines, the body as given.
    state = 'NULL' if body == 'NULL' else f"'{body}'"
    return (
        f'{_drop_package_routines(package)}; DO $p$ DECLARE s text; BEGIN '
        f"SELECT stubs INTO s FROM sys.ora_packages WHERE name = '{package}'; "
        "IF s <> '' THEN EXECUTE s; END IF; END $p$; "
        f"UPDATE sys.ora_packages SET body = {state} WHERE name = '{package}'"
    )


def _package_missing(package: str, code: int, message: str) -> str:
    # A check that raises Oracle's error when the package is not there.
    return (
        'DO $p$ BEGIN IF NOT EXISTS (SELECT 1 FROM sys.ora_packages WHERE name = '
        f"'{package}') THEN RAISE EXCEPTION USING ERRCODE = 'P0001', "
        f"MESSAGE = 'ORA-{code:05d}: {message}'; END IF; END $p$"
    )


def _translate_package_ddl(
    sql: str,
    spec_index_types: Callable[[str], dict[str, str]] | None = None,
    user_routines: Callable[[], frozenset[str]] | None = None,
) -> str:
    """A PL/SQL package's DDL as a schema of its name (#1605).

    The spec creates the schema -- replacing it drops the old one, body and all,
    as Oracle invalidates the body -- and a stand-in for each routine it
    declares, which raises ORA-04067 until a body replaces it. The body drops the
    schema's routines and creates its members in their place, each resolving
    names in the package first and then where its creator's session did, as a
    definer's package does. DROP PACKAGE drops the schema; DROP PACKAGE BODY puts
    the stand-ins back. A schema of that name that is no package is refused,
    ORA-00955. A private member is a routine of the schema like the others, so
    callable from outside where Oracle's is not. Other SQL is unchanged.

    ``spec_index_types`` gives, for a package, the index-by table types its
    spec declared -- name -> PostgreSQL type -- which a body indexes but does
    not declare itself (#1607).
    """
    dropped = _DROP_PACKAGE.match(sql)
    if dropped is not None:
        body, _owner, name = dropped.groups()
        package = name.lower()
        missing = _package_missing(
            package, 4043, f'object {name.upper()} does not exist'
        )
        if body:
            return f'{missing}; {_restore_package_stubs(package, "NULL")}'
        return (
            f'{missing}; DROP SCHEMA {package} CASCADE; '
            f"DELETE FROM sys.ora_packages WHERE name = '{package}'; "
            f"DELETE FROM sys.ora_plsql_types WHERE package = '{package}'"
        )
    head = _PACKAGE_HEAD.match(sql)
    if head is None:
        return sql
    body, owner, name = head.groups()
    package = name.lower()
    members = _package_members(sql, head.end())
    if members is None:
        raise BackendError(
            f'package {name.upper()} has PL/SQL the Mirror does not translate: '
            'an initialization section, or a member with routines of its own',
            ora_code=ORA_PLSQL_COMPILATION_ERROR,
        )
    routine_kinds = ('FUNCTION', 'PROCEDURE')
    # Each name the package's DDL gives resolves in the package first: set for
    # the statement and taken with each routine, then gone with the transaction.
    path = (
        "SELECT set_config('search_path', "
        f"'{package}, ' || current_setting('search_path'), true)"
    )
    if body:
        # The package's index-by table types, the spec's and the body's own:
        # name -> the PostgreSQL type (#1607).
        index_types = {
            m.name.lower(): f'{package}.{m.name.lower()}'
            for m in members
            if m.kind == 'TYPE' and _INDEX_BY_TYPE.fullmatch(m.body or '')
        }
        if spec_index_types is not None:
            index_types.update(spec_index_types(package))
        # The user routines a member may call (#1612), the package's own
        # members among them, for the members that need the NO_DATA_FOUND
        # handler.
        names = None
        if user_routines is not None:
            names = user_routines() | {
                m.name.lower() for m in members if m.kind in routine_kinds
            }
        routines = [
            _package_routine(
                package,
                m,
                _rewrite_index_tables(
                    m.body, _routine_index_tables(package, m, index_types)
                ),
                names,
            )
            for m in members
            if m.kind in routine_kinds and m.body is not None
        ]
        created = '; '.join(
            r.replace(
                ' LANGUAGE plpgsql AS $$',
                ' LANGUAGE plpgsql SET search_path FROM CURRENT AS $$',
                1,
            )
            for r in routines
        )
        missing = _package_missing(package, 6550, f'package {name.upper()} has no spec')
        return (
            f'{missing}; {_drop_package_routines(package)}; {path}; '
            f'{_package_types(package, members, public=False)}; '
            + (f'{created}; ' if created else '')
            + f"UPDATE sys.ora_packages SET body = 'VALID' WHERE name = '{package}'"
        )
    stubs = '; '.join(
        _package_routine(
            package, m, f"BEGIN PERFORM sys.ora_package_unusable('{package}'); END;"
        )
        for m in members
        if m.kind in routine_kinds and m.body is None
    )
    # The stand-ins are put back later, by a statement of their own: they carry
    # the package's search path with them.
    if stubs:
        stubs = f'{path}; {stubs}'
    owner_name = (owner or '').upper()
    owner_sql = f"'{owner_name}'" if owner_name else 'sys.ora_owner(current_schema())'
    taken = (
        'DO $p$ BEGIN IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '
        f"'{package}') AND NOT EXISTS (SELECT 1 FROM sys.ora_packages WHERE "
        f"name = '{package}') THEN RAISE EXCEPTION USING ERRCODE = 'P0001', "
        "MESSAGE = 'ORA-00955: name is already used by an existing object'; "
        'END IF; END $p$'
    )
    return (
        f'{taken}; DROP SCHEMA IF EXISTS {package} CASCADE; CREATE SCHEMA {package}; '
        # Recorded before the package's search path is set: its owner is the
        # schema the session is in.
        'INSERT INTO sys.ora_packages (name, owner, stubs, spec, body) VALUES '
        f"('{package}', {owner_sql}, $stubs${stubs}$stubs$, 'VALID', NULL) "
        'ON CONFLICT (name) DO UPDATE SET owner = EXCLUDED.owner, '
        "stubs = EXCLUDED.stubs, spec = 'VALID', body = NULL; "
        f'{path}; {_package_types(package, members, public=True)}'
        + (f'; {stubs}' if stubs else '')
    )


# An anonymous PL/SQL block a bind-less client sends — DECLARE … BEGIN … END, or a
# bare BEGIN … END. PostgreSQL can't run one directly, so wrap it as an anonymous
# code block: DO $$ … $$. The declared local types are mapped (VARCHAR2 → varchar,
# NUMBER → numeric, …) and the body is already valid PL/pgSQL for the assignment /
# DML cases the suite uses. A DO block takes no parameters, so this is the bind-less
# path — a block carrying binds goes through the callproc / OUT-bind flow (#517).
# The END must be present, so a bare `BEGIN` (transaction control) is left alone.
_ANON_BLOCK = re.compile(r'(?is)^\s*(DECLARE\b.*?\s)?BEGIN\b(.*)\bEND\s*;?\s*$')


def _bind_names(sql: str) -> list[str]:
    # The distinct bind names of `sql` in first-appearance order -- the order the
    # binds arrive in -- past literals, quoted identifiers and comments.
    names: list[str] = []
    for _kind, _text, name in _bind_spans(sql):
        if name is not None and name not in names:
            names.append(name)
    return names


def _replace_binds(sql: str, replacement: dict[str, str]) -> str:
    # `sql` with each bind named in `replacement` replaced by its text, past
    # literals, quoted identifiers and comments. A literal's or a quoted
    # identifier's `%` is doubled, as _translate_binds doubles it.
    out: list[str] = []
    for kind, text, name in _bind_spans(sql):
        if name is not None:
            out.append(replacement.get(name, text))
        elif kind in ('string', 'identifier'):
            out.append(text.replace('%', '%%'))
        else:
            out.append(text)
    return ''.join(out)


# The setting a block leaves each bind's value in, read back after it (#1456, #1459).
_BLOCK_BIND_SETTING = 'mirror.block_bind_{}'


def _bind_block(
    sql: str,
    locals_: dict[str, tuple[int, str, str]],
    routine_kind: Callable[[str], str | None] | None = None,
) -> str:
    """An anonymous block with binds, as a PostgreSQL DO block (#1456, #1459).

    A DO block takes no parameters and returns nothing, so each bind becomes a
    local of the block -- `locals_` maps its name to (slot, PostgreSQL type,
    initial literal) -- and before the block ends it leaves each local's value
    in a setting the backend reads back: '' for NULL, else 'v' and the text. A
    REF CURSOR's value is its portal name.
    """
    names = {name: f'mirror_bind_{slot}' for name, (slot, _t, _i) in locals_.items()}
    match = _ANON_BLOCK.match(_replace_binds(sql, names))
    if match is None:
        raise UnsupportedFeature('a bind outside a BEGIN ... END block')
    declare_part, body = match.groups()
    declare = (declare_part or 'DECLARE ') + ''.join(
        f' {names[name]} {pg_type} := {initial};'
        for name, (_slot, pg_type, initial) in locals_.items()
    )
    keep = ''.join(
        f" PERFORM set_config('{_BLOCK_BIND_SETTING.format(slot)}', "
        f"CASE WHEN {names[name]} IS NULL THEN '' ELSE 'v' || {names[name]}::text END, "
        'true);'
        for name, (slot, _t, _i) in locals_.items()
    )
    return _translate_plsql_block(
        f'{declare} BEGIN {body.rstrip()}{keep} END;', routine_kind
    )


# The PostgreSQL type a block's bind local takes, by the bind's declared type.
_BLOCK_BIND_TYPES = {
    TNS_TYPE_NUMBER: 'numeric',
    TNS_TYPE_INT: 'numeric',
    TNS_TYPE_VARCHAR: 'text',
    TNS_TYPE_CHAR: 'text',
    TNS_TYPE_LONG: 'text',
    TNS_TYPE_RAW: 'bytea',
    TNS_TYPE_LONGRAW: 'bytea',
    TNS_TYPE_DATE: 'timestamp(0)',
    TNS_TYPE_TIMESTAMP: 'timestamp',
    TNS_TYPE_BFLOAT: 'double precision',
    TNS_TYPE_BDOUBLE: 'double precision',
    TNS_TYPE_BOOLEAN: 'boolean',
    TNS_TYPE_REFCURSOR: 'refcursor',
}


def _block_bind_type(bind: object) -> str:
    # A bind's local type: its declared type's, else its value's.
    if isinstance(bind, BindVar):
        declared = _BLOCK_BIND_TYPES.get(bind.tns_type)
        if declared is not None:
            return declared
        bind = bind.value
    if isinstance(bind, bool):
        return 'boolean'
    if isinstance(bind, (int, decimal.Decimal)):
        return 'numeric'
    if isinstance(bind, float):
        return 'double precision'
    if isinstance(bind, (bytes, bytearray)):
        return 'bytea'
    if isinstance(bind, datetime.datetime):
        return 'timestamp'
    if isinstance(bind, datetime.date):
        return 'timestamp(0)'
    return 'text'


def _block_bind_value(pg_type: str, text: str) -> object:
    # A bind local's value from its text form (the setting carries text).
    if pg_type == 'numeric':
        return decimal.Decimal(text)
    if pg_type == 'double precision':
        return float(text)
    if pg_type == 'bytea':
        return bytes.fromhex(text[2:])
    if pg_type.startswith('timestamp'):
        return datetime.datetime.fromisoformat(text)
    if pg_type == 'boolean':
        return text == 'true'
    return text


# The words after which a new PL/SQL statement starts, and those that open a
# statement which is not a call however it continues (#1533).
_STATEMENT_OPENERS = frozenset({'BEGIN', 'THEN', 'ELSE', 'LOOP', 'DECLARE'})
_BLOCK_KEYWORDS = frozenset(
    {
        'IF',
        'ELSIF',
        'WHILE',
        'FOR',
        'CASE',
        'WHEN',
        'RETURN',
        'RAISE',
        'EXIT',
        'CONTINUE',
        'NULL',
        'GOTO',
        'COMMIT',
        'ROLLBACK',
        'SAVEPOINT',
        'EXECUTE',
        'OPEN',
        'CLOSE',
        'FETCH',
        'SELECT',
        'INSERT',
        'UPDATE',
        'DELETE',
        'MERGE',
        'WITH',
        'PERFORM',
        'CALL',
        'END',
        'BEGIN',
        'DECLARE',
        'PRAGMA',
        'LOCK',
    }
)


def _perform_bare_calls(body: str, routine_kind: Callable[[str], str | None]) -> str:
    """``body`` with each statement that is a bare call made one PL/pgSQL runs.

    A PL/SQL statement may be just a call, `put_line('x');` or `p;`. PL/pgSQL has
    no such statement: a function called for its effect is `PERFORM f(...)`, a
    procedure `CALL p(...)`, and the block failed to compile (#1533).
    ``routine_kind`` names what PostgreSQL has under a name -- 'f', 'p' or None --
    and a name it has nothing for is left as it was. Strings, comments and quoted
    identifiers are skipped whole, as the shared tokenizer reads them (#1693).
    """
    tokens = list(sql_tokens(body))
    out: list[str] = []
    (index, start) = (0, True)
    while index < len(tokens):
        token = tokens[index]
        text = body[token.start : token.end]
        index += 1
        if token.kind in ('space', 'comment'):
            out.append(text)  # neither starts nor ends a statement
            continue
        if token.kind != 'word':
            out.append(text)
            start = text == ';'
            continue
        # A name, dotted through as many parts as it has: pkg.proc.
        end = token.end
        while (
            index + 1 < len(tokens)
            and body[tokens[index].start : tokens[index].end] == '.'
            and tokens[index + 1].kind == 'word'
        ):
            end = tokens[index + 1].end
            index += 2
        word = body[token.start : end]
        upper = word.upper()
        rest = body[end:].lstrip()
        if (
            start
            and upper not in _BLOCK_KEYWORDS
            and upper not in _STATEMENT_OPENERS
            and rest[:1] in ('(', ';')
        ):
            kind = routine_kind(word)
            if kind == 'f':
                out.append('PERFORM ')
            elif kind == 'p':
                out.append('CALL ')
            if kind in ('f', 'p') and rest[:1] == ';':
                word += '()'
        out.append(word)
        start = upper in _STATEMENT_OPENERS
    return ''.join(out)


def _translate_plsql_block(
    sql: str, routine_kind: Callable[[str], str | None] | None = None
) -> str:
    """Wrap an anonymous DECLARE/BEGIN … END block as a PostgreSQL ``DO $$ … $$``
    block, mapping the declared local types (#533). Non-block SQL is unchanged.
    With ``routine_kind``, a statement that is a bare call becomes one PL/pgSQL
    runs (:func:`_perform_bare_calls`)."""
    hoisted = _hoist_local_functions(sql)
    if hoisted is not None:
        return hoisted
    match = _ANON_BLOCK.match(sql)
    if match is None:
        return sql
    declare_part, body = match.groups()
    declare = _translate_routine_types(declare_part) if declare_part else ''
    if routine_kind is not None:
        body = _perform_bare_calls(body, routine_kind)
    return f'DO $$ {declare}BEGIN {body.strip()} END $$'


# A local function in a block's declarations: FUNCTION name [(params)] RETURN
# type IS|AS, its own declarations up to BEGIN, then the body.
_LOCAL_FUNCTION_HEAD = re.compile(
    r'(?is)\s*FUNCTION\s+(\w+)\s*(\((?:[^()]|\([^()]*\))*\))?\s*'
    r'RETURN\s+(.+?)\s+(?:IS|AS)\b'
)


def _words(text: str, start: int = 0) -> Iterator[tuple[str, int, int]]:
    # (word upper-cased, start, end) for each word of `text` from `start`, past
    # string literals, comments and quoted identifiers -- the shared
    # tokenizer's words (#1693), so a keyword inside any of them counts for
    # nothing, and `$` / `#` are part of a name as Oracle has them.
    for token in sql_tokens(text[start:]):
        (begin, end) = (start + token.start, start + token.end)
        if token.kind == 'bind' and text[begin + 1 : begin + 2].isalpha():
            begin += 1  # an unquoted bind's name is a word too, as it always was
        elif token.kind != 'word':
            continue
        yield text[begin:end].upper(), begin, end


def _block_end(text: str, begin: int) -> tuple[int, int] | None:
    # The span of the END that closes the BEGIN at `begin`. BEGIN and CASE open a
    # level, END closes one -- but END IF / END LOOP close no BEGIN, so they are
    # passed over, and END CASE closes the CASE statement it ends. A keyword is
    # the END's own only right after it: `END; IF` is a CASE expression's END and
    # the next statement's IF (#1604).
    depth = 0
    words = list(_words(text, begin))
    after_end = False
    for i, (word, start, end) in enumerate(words):
        if after_end and word in ('IF', 'LOOP', 'CASE'):
            after_end = False  # the END's own keyword, which opens nothing
            continue
        after_end = False
        if word in ('BEGIN', 'CASE'):
            depth += 1
        elif word == 'END':
            following = words[i + 1] if i + 1 < len(words) else None
            after_end = following is not None and not text[end : following[1]].strip()
            if after_end and following is not None and following[0] in ('IF', 'LOOP'):
                continue
            depth -= 1
            if depth == 0:
                return start, end
    return None


# How a block's hoisted functions start: a translation beginning with it carries
# several commands, which only PostgreSQL's simple query protocol accepts.
_HOISTED_FUNCTION = 'DROP FUNCTION IF EXISTS pg_temp.'


def _hoist_local_functions(sql: str) -> str | None:
    # An anonymous block declaring local FUNCTIONs (#1322). PL/pgSQL has no nested
    # routines, so each becomes a session-temporary function created ahead of the
    # DO block, and every call to one is qualified with pg_temp -- which PostgreSQL
    # never searches for a function otherwise. None for any other block, which
    # keeps its plain DO translation; a local PROCEDURE is not handled.
    head = re.match(r'(?is)\s*DECLARE\b', sql)
    if head is None:
        return None
    functions: list[tuple[str, str, str, str, str]] = []
    declarations = []
    pos = head.end()
    while True:
        function = _LOCAL_FUNCTION_HEAD.match(sql, pos)
        if function is not None:
            begin = next(
                (s for w, s, _e in _words(sql, function.end()) if w == 'BEGIN'), None
            )
            if begin is None:
                return None
            span = _block_end(sql, begin)
            if span is None:
                return None
            name, params, return_type = function.groups()
            local = sql[function.end() : begin]
            body = sql[begin + len('BEGIN') : span[0]]
            functions.append((name, params or '()', return_type, local, body))
            close = re.compile(r'\s*(?:\w+\s*)?;').match(sql, span[1])
            pos = close.end() if close is not None else span[1]
            continue
        word = next(_words(sql, pos), None)
        if word is None:
            return None
        if word[0] == 'BEGIN':
            break
        if word[0] == 'PROCEDURE':
            return None
        # An ordinary declaration: through its terminating semicolon.
        stop = sql.find(';', word[1])
        if stop < 0:
            return None
        declarations.append(sql[pos : stop + 1])
        pos = stop + 1
    if not functions:
        return None
    rest = _ANON_BLOCK.match(sql[pos:])
    if rest is None:
        return None
    names = {name.lower() for name, *_ in functions}
    calls = re.compile(
        r'(?<![.\w])(' + '|'.join(re.escape(n) for n in names) + r')\s*\(',
        re.IGNORECASE,
    )

    def qualify(text: str) -> str:
        # Qualify the calls to the hoisted functions, outside string literals.
        parts = re.split(r"('(?:[^']|'')*')", text)
        return ''.join(
            part if i % 2 else calls.sub(r'pg_temp.\1(', part)
            for i, part in enumerate(parts)
        )

    statements = []
    for name, params, return_type, local, body in functions:
        params = _translate_routine_types(_PARAM_IN_OUT.sub('INOUT', params))
        returns = _translate_routine_types(return_type.strip())
        local = _translate_routine_types(local.strip())
        declare = f'DECLARE {qualify(local)} ' if local else ''
        statements.append(
            f'{_HOISTED_FUNCTION}{name}; '
            f'CREATE FUNCTION pg_temp.{name}{params} RETURNS {returns} '
            f'LANGUAGE plpgsql AS $f$ {declare}BEGIN {qualify(body.strip())} END $f$;'
        )
    declare = _translate_routine_types(''.join(declarations).strip())
    declare = f'DECLARE {qualify(declare)} ' if declare else ''
    body = qualify(rest.group(2).strip())
    return ' '.join(statements) + f' DO $$ {declare}BEGIN {body} END $$'


# The anonymous block a thin callproc / callfunc sends: BEGIN name(:a, :b); END;
# or BEGIN :r := name(:a, :b); END;
_CALL_BLOCK = re.compile(r'(?is)^\s*BEGIN\s+(.*?)\s*;?\s*END\s*;?\s*$')
# A block that only calls a procedure with no arguments, `BEGIN p; END;` or
# `BEGIN p(); END;` -- python-oracledb's callproc of one sends the former, with no
# binds (#1404).
_BARE_CALL = re.compile(
    r'(?is)^\s*BEGIN\s+([\w.$#]+)\s*(?:\(\s*\))?\s*;\s*END\s*;?\s*$'
)


def _single_call_block(sql: str) -> str | None:
    """The routine a block holding one call with arguments calls,
    `BEGIN p(...); END;` -- one statement, not an assignment -- or None (#1531)."""
    inner = _CALL_BLOCK.match(sql)
    if inner is None:
        return None
    statement = inner.group(1)
    bare = strip_non_bind_text(statement)
    if ';' in bare or ':=' in bare:
        return None
    call = _PROC_CALL.match(statement)
    if call is None or call.group(1).upper() in _PLSQL_WORD_STATEMENTS:
        return None
    return call.group(1)


# The PL/SQL statements that are one word, which a lone word in a block may be
# rather than a procedure's name: `begin null; end;` is a statement (#1404).
_PLSQL_WORD_STATEMENTS = frozenset(
    {'NULL', 'COMMIT', 'ROLLBACK', 'RETURN', 'EXIT', 'CONTINUE', 'RAISE'}
)
_FUNC_CALL = re.compile(r'(?is)^\s*:(\d+)\s*:=\s*([\w.]+)\s*\((.*)\)\s*$')
# A placeholder in a call, numbered `:3` or named `:lines` -- not the second colon
# of `::`. A client sends a block's values one per distinct placeholder, in the
# order they first appear, numbered or named alike (#1380, #1529). A quoted name
# is a placeholder too, and counts toward that order. (Not `_BIND_REF`, which is
# taken: rebinding it silently changed every reader of that one.)
_CALL_PLACEHOLDER = re.compile(r'(?<!:):(?:"([^"\n]+)"|(\d+|[A-Za-z_][\w$#]*))')
# A named argument in a call's list: `name => value`.
_NAMED_ARGUMENT = re.compile(r'(?is)^\s*(\w+)\s*=>')
# One argument of a call: a bind, by position or by name (`:3`, `a_x => :3`).
_CALL_ARGUMENT = re.compile(r'(?is)^\s*(?:(\w+)\s*=>\s*)?:(\d+|[A-Za-z_][\w$#]*)\s*$')


def _placeholder_ref(match: 're.Match') -> int | str:
    # The key of a _CALL_PLACEHOLDER match: a quoted name exactly as written,
    # with its quotes so it never meets an unquoted one.
    if match.group(1) is not None:
        return f'"{match.group(1)}"'
    return _placeholder_key(match.group(2))


def _placeholder_key(ref: str) -> int | str:
    # A placeholder's identity: a number as its value, a name case-folded, as
    # Oracle matches an unquoted bind name.
    return int(ref) if ref.isdigit() else ref.lower()


def _placeholder_slot(slots: dict[int | str, int], key: int | str) -> int:
    # The bind value a placeholder takes. A number the block never used falls
    # back to its own position, as it always did; a name is always in the block
    # its slots were read from.
    if isinstance(key, int) and key not in slots:
        return key - 1
    return slots[key]


@dataclass(frozen=True)
class _CallLiteral:
    """An argument a call passes as written -- a literal, an expression -- not
    through a bind (#1531)."""

    text: str


def _call_items(
    args: str,
) -> list[tuple[str | None, 'int | str | _CallLiteral']]:
    """A call's arguments as (parameter name or None, the placeholder's key or
    the argument's own text), in order: :func:`_call_arguments`, but with the
    literals and expressions a call may pass among its binds kept (#1531)."""
    items: list[tuple[str | None, int | str | _CallLiteral]] = []
    for start, end in _top_level_items(args, 0, len(args)):
        text = args[start:end].strip()
        if not text:
            continue
        match = _CALL_ARGUMENT.match(text)
        if match is not None:
            name = match.group(1)
            items.append(
                (name.lower() if name else None, _placeholder_key(match.group(2)))
            )
            continue
        named = _NAMED_ARGUMENT.match(text)
        value = text[named.end() :].strip() if named else text
        quoted = _CALL_PLACEHOLDER.fullmatch(value)
        items.append(
            (
                named.group(1).lower() if named else None,
                _placeholder_ref(quoted) if quoted else _CallLiteral(value),
            )
        )
    return items


def _call_arguments(args: str) -> list[tuple[str | None, int | str]] | None:
    """A call's arguments as (parameter name or None, bind number), in order.

    None when an argument is anything but a bind -- a literal, an expression --
    which the positional path goes on handling as it always has.
    """
    arguments = []
    for start, end in _top_level_items(args, 0, len(args)):
        if not args[start:end].strip():
            continue
        match = _CALL_ARGUMENT.match(args[start:end])
        if match is None:
            return None
        name = match.group(1)
        arguments.append(
            (name.lower() if name else None, _placeholder_key(match.group(2)))
        )
    return arguments


def _bind_slots(block: str) -> dict[int | str, int]:
    """Each numbered placeholder of a block -> the bind value it takes.

    A client sends a block's bind values one per distinct placeholder, in the
    order the placeholders first appear, whatever their numbers say:
    `p(:3, c => :1)` takes the first value for `:3` (measured on 8i to 23ai).
    Reading `:N` as the Nth value gave such a call the wrong values (#1380).
    Comments and literals are skipped, as for any placeholder scan.
    """
    slots: dict[int | str, int] = {}
    for match in _CALL_PLACEHOLDER.finditer(strip_non_bind_text(block)):
        slots.setdefault(_placeholder_ref(match), len(slots))
    return slots


def _call_placeholders(
    arguments: Sequence[tuple[str | None, 'int | str | _CallLiteral']],
) -> str:
    # PostgreSQL takes a call's named notation as Oracle writes it (#1377). A
    # literal argument goes in as written; a bind is a parameter (#1531).
    def one(ref: int | str | _CallLiteral) -> str:
        return ref.text.replace('%', '%%') if isinstance(ref, _CallLiteral) else '%s'

    return ', '.join(
        f'{name} => {one(ref)}' if name else one(ref) for name, ref in arguments
    )


def _call_argument_error(
    block: str, routine: str, args: str, parameters: Sequence[str] | None
) -> BackendError | None:
    """The ORA-06550 Oracle compiles a call's argument list into, or None.

    ``parameters`` are the routine's parameter names in order, when known. A
    parameter given twice is PLS-00703 -- by name twice, or by position and then
    by name, positional arguments filling the first parameters; more arguments
    than the routine has parameters is PLS-00306, checked after. Unchecked, both
    reached the backend's positional mapping, which indexed past the parameter
    list, and the IndexError that escaped became an ORA-00600 -- which a client
    reads as a dead connection, and python-oracledb then closes it (#1368).
    Oracle reports the position of the routine's name in the block.
    """
    items = [
        args[s:e] for s, e in _top_level_items(args, 0, len(args)) if args[s:e].strip()
    ]
    named = [m.group(1).lower() for m in map(_NAMED_ARGUMENT.match, items) if m]
    positional = len(items) - len(named)
    used = named + [name.lower() for name in (parameters or [])[:positional]]
    if len(used) != len(set(used)):
        pls = 'PLS-00703: multiple instances of named argument in list'
    elif parameters is not None and len(items) > len(parameters):
        pls = (
            'PLS-00306: wrong number or types of arguments in call to '
            f"'{routine.rsplit('.', 1)[-1].upper()}'"
        )
    else:
        return None
    at = max(block.lower().find(routine.lower()), 0)
    line = block.count('\n', 0, at) + 1
    column = at - block.rfind('\n', 0, at)
    return BackendError(
        f'line {line}, column {column}:\n{pls}', ora_code=ORA_PLSQL_COMPILATION_ERROR
    )


_PROC_CALL = re.compile(r'(?is)^\s*([\w.]+)\s*\((.*)\)\s*$')
# A scalar OUT-bind assignment inside a block: `:ref := <expr>` (#517).
_OUT_ASSIGN = re.compile(r'(?is)^\s*:(\w+)\s*:=\s*(.+?)\s*$')


# A block's lone SELECT ... INTO :b1[, :b2] FROM ... (#1396): the query, and the
# bind list it assigns the row to.
_SELECT_INTO = re.compile(
    r'(?is)^\s*(SELECT\s.+?)\s+INTO\s+(:\w+(?:\s*,\s*:\w+)*)\s+(FROM\b.*?)\s*;?\s*$'
)


def _parse_out_assignments(body: str) -> list[tuple[str, str]] | None:
    # An OUT-bind-assignment block is one or more `:ref := <expr>` statements
    # (BEGIN :y := 7*6; :2 := NULL; END). Returns (ref, expr) per assignment, or
    # None if any statement isn't such an assignment (so it isn't this shape).
    assignments = []
    for statement in filter(None, (s.strip() for s in body.split(';'))):
        match = _OUT_ASSIGN.match(statement)
        if match is None:
            return None
        assignments.append((match.group(1), match.group(2)))
    return assignments or None


# PostgreSQL type OIDs (pg_type.oid) → Oracle wire type.
_NUMBER_OIDS = frozenset(
    {
        16,
        20,
        21,
        23,
        26,
        1700,
    }  # bool int8 int2 int4 oid numeric
)
# IEEE-754 floats map to Oracle's native binary types, not base-100 NUMBER —
# preserving the exact bits and the "this is a float, not a decimal" nature.
_BINARY_FLOAT_OIDS = {
    700: (TNS_TYPE_BFLOAT, 4),  # float4 (real)
    701: (TNS_TYPE_BDOUBLE, 8),  # float8 (double precision)
}
_TEXT_OIDS = frozenset({18, 19, 25, 1042, 1043})  # char name text bpchar varchar
_BPCHAR_OID = 1042
_NUMERIC_OID = 1700
_RAW_OIDS = frozenset({17})  # bytea
# The base type oids the Oracle-typed domains report on the wire — ora_clob /
# ora_blob over text / bytea (#534), ora_intervalym over interval (#504), ora_date
# over timestamp (#1316). Only a column of one of these can be such a domain, so
# the catalog lookup that distinguishes them is skipped for anything else.
_DOMAIN_BASE_OIDS = frozenset({25, 17, _INTERVAL_OID, _TIMESTAMP_OID})
# Each PostgreSQL temporal OID maps to the Oracle type of matching precision:
# a bare date → DATE (7 bytes), timestamp → TIMESTAMP (11), and timestamptz →
# TIMESTAMP WITH LOCAL TIME ZONE (11), since WITH TIME ZONE is ora_tstz (#1208).
_TEMPORAL_OIDS = {
    1082: (TNS_TYPE_DATE, 7),  # date
    1114: (TNS_TYPE_TIMESTAMP, 11),  # timestamp (without time zone)
    1184: (TNS_TYPE_TIMESTAMPLTZ, 11),  # timestamptz
}
# PostgreSQL `interval` (oid 1186) → Oracle INTERVAL DAY TO SECOND by default;
# psycopg returns it as a timedelta (an OraInterval, months == 0), which the Mirror
# encodes for an INTERVALDS column. A YEAR TO MONTH interval is distinguished by its
# ora_intervalym domain (traced through the catalog) and handled separately (#504).
_INTERVAL_OIDS = frozenset({_INTERVAL_OID})


# Map a PostgreSQL error (by SQLSTATE) to the Oracle error number a client
# expects, so error-conditional flows behave (#500). The load-bearing one is
# `undefined_table` → ORA-00942: the suite's setUp/tearDown drops tables
# best-effort and only swallows ORA-00942 — reporting ORA-00900 instead re-raised
# and failed every test in setUp. Anything unmapped falls back to ORA-00900.
_SQLSTATE_TO_ORA = {
    '42P01': ORA_TABLE_OR_VIEW_DOES_NOT_EXIST,  # undefined_table
    '42704': ORA_TABLE_OR_VIEW_DOES_NOT_EXIST,  # undefined_object (type), for DROP TYPE cleanup
    '42P07': ORA_NAME_ALREADY_USED,  # duplicate_table
    '42703': ORA_INVALID_IDENTIFIER,  # undefined_column
    '42883': ORA_INVALID_IDENTIFIER,  # undefined_function
    '23505': ORA_UNIQUE_CONSTRAINT_VIOLATED,  # unique_violation
    '23502': ORA_CANNOT_INSERT_NULL,  # not_null_violation
    '23503': ORA_PARENT_KEY_NOT_FOUND,  # foreign_key_violation: the insert/update
    # direction; the delete direction is ORA-02292, which SQLSTATE cannot tell apart
    '23514': ORA_CHECK_CONSTRAINT_VIOLATED,  # check_violation
    '22P02': ORA_INVALID_NUMBER,  # invalid_text_representation (TO_NUMBER)
    '3B001': ORA_SAVEPOINT_NEVER_ESTABLISHED,  # invalid_savepoint_specification
    '55P03': ORA_RESOURCE_BUSY,  # lock_not_available (a DDL's lock wait, #1191)
    # A string too long for its VARCHAR2(n) column: Oracle's ORA-12899, "value
    # too large for column". Unlike 22003 below, this SQLSTATE means exactly one
    # Oracle error, so the table can carry it (#1127).
    '22001': ORA_VALUE_TOO_LARGE_FOR_COLUMN,  # string_data_right_truncation
    # #1323: the codes python-oracledb's suite checks by number.
    '22012': ORA_DIVISOR_IS_ZERO,  # division_by_zero
    'P0002': ORA_NO_DATA_FOUND,  # no_data_found (RAISE no_data_found)
    'P0003': ORA_TOO_MANY_ROWS,  # too_many_rows (a SELECT ... INTO STRICT, #1650)
}


# The canonical Oracle message text for a mapped ORA code, used in place of
# PostgreSQL's own wording so a client that matches on the Oracle phrasing behaves
# — ORA-00942 reads "table or view does not exist", not "relation … does not
# exist" (#529). A code with no entry keeps PostgreSQL's message (still prefixed
# with its ORA-NNNNN by the Mirror), which is right where the English text varies
# by Oracle version anyway (e.g. ORA-01722).
_ORA_MESSAGE = {
    ORA_RESOURCE_BUSY: 'resource busy and acquire with NOWAIT specified or timeout expired',
    ORA_TOO_MANY_VALUES: 'too many values',
    ORA_INVALID_DATATYPE: 'invalid datatype',
    ORA_TABLE_OR_VIEW_DOES_NOT_EXIST: 'table or view does not exist',
    ORA_NOT_ENOUGH_VALUES: 'not enough values',
    ORA_NO_DATA_FOUND: 'no data found',
    ORA_TOO_MANY_ROWS: 'exact fetch returns more than requested number of rows',
    ORA_DIVISOR_IS_ZERO: 'divisor is equal to zero',
    ORA_TYPE_HAS_DEPENDENTS: 'cannot drop or replace a type with type or table dependents',
}


# A user error raised by a translated RAISE_APPLICATION_ERROR: P0001 whose message
# leads with its ORA code (#1323). The Mirror's own PL/pgSQL raises Oracle's
# errors the same way, a package's ORA-04067 among them (#1605).
_APPLICATION_ERROR = re.compile(r'ORA-(\d{5}): (.*)', re.DOTALL)
# An INSERT whose value list and column list disagree in length. PostgreSQL files
# both under syntax_error (42601), which says nothing an Oracle client can use;
# Oracle names them ORA-00913 / ORA-00947 (#1323).
_INSERT_ARITY = (
    ('INSERT has more expressions than target columns', ORA_TOO_MANY_VALUES),
    ('INSERT has more target columns than expressions', ORA_NOT_ENOUGH_VALUES),
)


def _primary_message(exc) -> str:
    diag = getattr(exc, 'diag', None)
    return getattr(diag, 'message_primary', None) or str(exc)


def _application_error(exc) -> tuple[int, str] | None:
    # The (code, text) of a RAISE_APPLICATION_ERROR, or None for anything else.
    if getattr(exc, 'sqlstate', None) != 'P0001':
        return None
    m = _APPLICATION_ERROR.match(_primary_message(exc))
    return (int(m.group(1)), m.group(2)) if m else None


def _ora_code_for(exc) -> int:
    sqlstate = getattr(exc, 'sqlstate', None)
    if not isinstance(sqlstate, str):
        return ORA_INVALID_SQL_STATEMENT
    application = _application_error(exc)
    if application is not None:
        return application[0]
    if sqlstate == '42601':
        primary = _primary_message(exc)
        for text, code in _INSERT_ARITY:
            if text in primary:
                return code
    # `22003 numeric_value_out_of_range` covers two different Oracle errors, so the
    # SQLSTATE alone cannot pick the code (#1127). A value too wide for a NUMBER(p,s)
    # column is ORA-01438, "value larger than specified precision allowed for this
    # column"; an arithmetic result that overflows is ORA-01426, "numeric overflow".
    # PostgreSQL tells them apart only in the primary message -- measured:
    #     numeric(3,0) given 123456  -> 22003 "numeric field overflow"
    #     int 2147483647 + 1         -> 22003 "integer out of range"
    # so the column case is matched on its message and everything else under 22003
    # is the arithmetic one. A client that branches on the code -- batcherrors
    # reports it per row -- would otherwise be told ORA-00900, a syntax error, about
    # a statement whose syntax was fine.
    if sqlstate == '22003':
        diag = getattr(exc, 'diag', None)
        primary = getattr(diag, 'message_primary', None) or str(exc)
        if 'numeric field overflow' in primary:
            return ORA_VALUE_LARGER_THAN_PRECISION
        return ORA_NUMERIC_OVERFLOW
    return _SQLSTATE_TO_ORA.get(sqlstate, ORA_INVALID_SQL_STATEMENT)


# A type statement refused because something depends on the type: Oracle's
# ORA-02303 (#1197). PostgreSQL's dependent_objects_still_exist means other things
# for other objects, so it maps only for a type statement.
_TYPE_DDL = re.compile(r'\s*(?:CREATE\s+OR\s+REPLACE|DROP)\s+TYPE\b', re.IGNORECASE)
# A type PostgreSQL does not know. Dropping one is Oracle's missing-object case,
# which the ORA-00942 of _SQLSTATE_TO_ORA answers; anywhere else -- a CAST, a
# column -- it is Oracle's ORA-00902 invalid datatype (#1329).
_DROP_STATEMENT = re.compile(r'\s*DROP\b', re.IGNORECASE)


# What a PostgreSQL compile-time message names: an unknown variable, a function
# with no matching signature, or the token a syntax error is at.
_UNKNOWN_VARIABLE = re.compile(r'"([^"]+)" is not a known variable')
_UNKNOWN_FUNCTION = re.compile(r'(?:function|procedure) ([\w.$#"]+)\(')
_NEAR_TOKEN = re.compile(r'at or near "([^"]+)"')
# A call into a package that is not there: PostgreSQL finds no schema (#1605).
_UNKNOWN_SCHEMA = re.compile(r'schema "([^"]+)" does not exist')


def _plsql_compile_error(
    exc, block: str, arities: Callable[[str], set[int]] | None = None
) -> BackendError | None:
    # A block that fails to compile is ORA-06550 in Oracle, "line L, column C:"
    # and the PLS- error, whatever was wrong in it: an undeclared identifier, a
    # call no routine's signature matches, a syntax error (#1497). PostgreSQL
    # reports each under SQLSTATE class 42 -- 42601 / 42883 / 42703 / 42P01 --
    # which mapped to the SQL statement's codes, ORA-00900 / 00904 / 00942. The
    # position is the named thing's in the client's block, as Oracle gives it.
    # A call into a package that is not there is PostgreSQL's 3F000, no such
    # schema, and Oracle's PLS-00201 for the package.member it names (#1605).
    sqlstate = str(getattr(exc, 'sqlstate', None) or '')
    if not sqlstate.startswith('42') and sqlstate != '3F000':
        return None
    primary = _primary_message(exc)
    if sqlstate == '3F000':
        schema = _UNKNOWN_SCHEMA.search(primary)
        if schema is None:
            return None
        qualified = re.search(
            rf'\b{re.escape(schema.group(1))}\s*\.\s*[\w$#]+', block, re.IGNORECASE
        )
        name = re.sub(r'\s+', '', qualified.group()) if qualified else schema.group(1)
        pls = f"PLS-00201: identifier '{name.upper()}' must be declared"
    elif (variable := _UNKNOWN_VARIABLE.search(primary)) is not None:
        name = variable.group(1)
        pls = f"PLS-00201: identifier '{name.upper()}' must be declared"
    elif (function := _UNKNOWN_FUNCTION.search(primary)) is not None:
        name = function.group(1).strip('"').rsplit('.', 1)[-1]
        # A routine that takes this many arguments compiles in Oracle, and a
        # value of the wrong type fails when it is converted: ORA-06502, not a
        # compile error. PostgreSQL resolves the call by type and finds none.
        # Only a positional call that passes a string, which Oracle tries to
        # convert; a named argument given twice, or a type it does not convert,
        # does not compile there either.
        given = primary[function.end() : primary.find(')', function.end())]
        types = [a.strip() for a in given.split(',') if a.strip()]
        if (
            arities is not None
            and '=>' not in given
            and any(t in ('unknown', 'text', 'character varying') for t in types)
            and len(types) in arities(name)
        ):
            return BackendError(
                'PL/SQL: numeric or value error: character to number conversion error',
                ora_code=ORA_NUMERIC_OR_VALUE_ERROR,
            )
        pls = (
            f"PLS-00306: wrong number or types of arguments in call to '{name.upper()}'"
        )
    else:
        near = _NEAR_TOKEN.search(primary)
        name = near.group(1) if near is not None else ''
        pls = f'PLS-00103: {primary}'
    at = block.lower().find(name.lower()) if name else -1
    line = block.count('\n', 0, max(at, 0)) + 1
    column = max(at, 0) - block.rfind('\n', 0, max(at, 0))
    return BackendError(
        f'line {line}, column {column}:\n{pls}',
        ora_code=ORA_PLSQL_COMPILATION_ERROR,
        error_offset=at if at >= 0 else None,
    )


def _backend_error(
    exc,
    *,
    original: str | None = None,
    translated: str | None = None,
    arities: Callable[[str], set[int]] | None = None,
) -> BackendError:
    # A PostgreSQL failure as a clean ORA error: the mapped code, and the Oracle
    # canonical text for it when there is one, else PostgreSQL's own message (#529).
    if original is not None and is_plsql(original):
        compile_error = _plsql_compile_error(exc, original, arities)
        if compile_error is not None:
            return compile_error
    code = _ora_code_for(exc)
    if (
        getattr(exc, 'sqlstate', None) == '2BP01'
        and original is not None
        and _TYPE_DDL.match(original)
    ):
        code = ORA_TYPE_HAS_DEPENDENTS
    if (
        getattr(exc, 'sqlstate', None) == '42704'
        and original is not None
        and not _DROP_STATEMENT.match(original)
        and _primary_message(exc).startswith('type "')
    ):
        code = ORA_INVALID_DATATYPE
    if (
        getattr(exc, 'sqlstate', None) == '23514'
        and getattr(getattr(exc, 'diag', None), 'constraint_name', None)
        == _ROWID_FORMAT
    ):
        # A ROWID column's text that reads as no rowid (#1624).
        return BackendError('invalid ROWID', ora_code=ORA_INVALID_ROWID)
    application = _application_error(exc)
    if application is not None:
        # The user's own text, which the Mirror prefixes with the code.
        return BackendError(application[1], ora_code=code, error_offset=None)
    return BackendError(
        _ORA_MESSAGE.get(code, str(exc).strip()),
        ora_code=code,
        error_offset=_error_offset(exc, original, translated),
    )


def _error_offset(exc, original: str | None, translated: str | None) -> int | None:
    # PostgreSQL reports where a parse error sits as a 1-based character position
    # into the statement it received — the dialect-rewritten one. Oracle's offset
    # (DatabaseError.offset, the sqlplus caret) is 0-based into the statement the
    # client sent. The two agree only where the rewrite left everything before the
    # error untouched, so relay the position when the two texts share that prefix
    # and report nothing (None) otherwise, rather than a misplaced caret.
    position = getattr(getattr(exc, 'diag', None), 'statement_position', None)
    if position is None or original is None or translated is None:
        return None
    try:
        offset = int(position) - 1
    except (TypeError, ValueError):
        return None
    if offset < 0 or offset > len(translated):
        return None
    return offset if translated[:offset] == original[:offset] else None


# PostgreSQL's built-in `refcursor` type OID (stable across versions) — a CALL's
# OUT refcursor comes back as the portal name at this OID, which the backend then
# drains into a CursorResult for the REF CURSOR OUT bind (#518).
_REFCURSOR_OID = 1790


def _reconstruct_tstz(value):
    # A psycopg `ora_tstz(utc, off)` composite → an aware datetime re-tagged with
    # the entered offset, so TIMESTAMP WITH TIME ZONE round-trips its offset the way
    # Oracle does rather than coming back normalised to UTC (#519).
    if value is None:
        return None
    return value.utc.astimezone(
        datetime.timezone(datetime.timedelta(seconds=value.off))
    )


_TIMESTAMPTZ_OID = 1184


def _to_ltz(value):
    # A timestamptz cell → TIMESTAMP WITH LOCAL TIME ZONE's wire value: the
    # instant in the database time zone, naive, as Oracle sends it (#1208).
    if not isinstance(value, datetime.datetime) or value.tzinfo is None:
        return value
    return value.astimezone(_DB_TIME_ZONE).replace(tzinfo=None)


def _from_ltz(value):
    # A bound TIMESTAMP WITH LOCAL TIME ZONE value, naive, is an instant in the
    # database time zone, as an LtzValue bind is (#1222): made aware so
    # PostgreSQL does not read it in the session's zone instead (#1270).
    if not isinstance(value, datetime.datetime) or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=_DB_TIME_ZONE)


def _wire_cell(value, type_code: int, tstz_oid: int | None):
    # A fetched cell as the wire encoder wants it: an ora_tstz composite as an
    # aware datetime at its offset (#519), a timestamptz as an LTZ value (#1208).
    if tstz_oid is not None and type_code == tstz_oid:
        return _reconstruct_tstz(value)
    if type_code == _TIMESTAMPTZ_OID:
        return _to_ltz(value)
    return value


def _decode_row(cursor, row, tstz_oid: int | None) -> list | None:
    # Re-tag a fetched row's zoned cells (see _wire_cell), so a value returned from
    # a routine is sent as its type is.
    if row is None:
        return None
    return [
        _wire_cell(value, desc.type_code, tstz_oid)
        for value, desc in zip(row, cursor.description or ())
    ]


# How Oracle reports an identifier (#1204): an unquoted one folded to upper case,
# a quoted one as written. PostgreSQL folds the other way, so a legal unquoted
# lower-case name -- one that is not a reserved word, which only a quoted name
# could be -- came from an unquoted one and is upper-cased; anything else (mixed
# case, special characters) was quoted and is kept. A quoted all-lower-case name
# is indistinguishable here; sys.ora_quoted_names records those.
_UNQUOTED_NAME = re.compile(r'[a-z][a-z0-9_$#]*')


def _oracle_column_name(name: str) -> str:
    if _UNQUOTED_NAME.fullmatch(name) and name.upper() not in _ORACLE_RESERVED_WORDS:
        return name.upper()
    return name


def _lob_column_meta(name: str, tns_type: int) -> ColumnMeta:
    # A CLOB / BLOB result column (an ora_clob / ora_blob domain traced back through
    # the catalog). LOBs are unsized on the wire — data_length is nominal, max_size
    # 0 — and the Mirror streams the cell content as a locator (#534).
    return ColumnMeta(
        name=_oracle_column_name(name).encode('utf-8'),
        data_type=tns_type,
        data_length=4000,
        max_size=0,
    )


def _refcursor_column_meta(name: str) -> ColumnMeta:
    # A CURSOR(...) result column (#1461); its cells are drained CursorResults.
    return ColumnMeta(
        name=_oracle_column_name(name).encode('utf-8'),
        data_type=TNS_TYPE_REFCURSOR,
        data_length=0,
        max_size=0,
    )


def _date_column_meta(name: str) -> ColumnMeta:
    # A DATE result column (an ora_date domain traced back through the catalog,
    # #1316): Oracle's 7-byte DATE, whose cells are the timestamp(0)'s datetimes.
    data_type, width = _TEMPORAL_OIDS[_DATE_OID]
    return ColumnMeta(
        name=_oracle_column_name(name).encode('utf-8'),
        data_type=data_type,
        data_length=width,
        max_size=width,
    )


def _intervalym_column_meta(name: str) -> ColumnMeta:
    # An INTERVAL YEAR TO MONTH result column (an ora_intervalym domain traced back
    # through the catalog). The wire form is 5 bytes — 4-byte years + 1-byte months
    # (see the Mirror's encode_interval_ym) — and its cells are IntervalYM (#504).
    return ColumnMeta(
        name=_oracle_column_name(name).encode('utf-8'),
        data_type=TNS_TYPE_INTERVALYM,
        data_length=5,
        max_size=5,
    )


def _column_meta(desc, values: list, tstz_oid: int | None = None) -> ColumnMeta:
    # `desc` is a psycopg Column (name / type_code / precision / scale / ...).
    name, oid = desc.name, desc.type_code
    ident = _oracle_column_name(name).encode('utf-8')
    if tstz_oid is not None and oid == tstz_oid:
        # The ora_tstz composite backing TIMESTAMP WITH TIME ZONE — the cells are
        # reconstructed to aware datetimes by the caller (#519).
        return ColumnMeta(
            name=ident,
            data_type=TNS_TYPE_TIMESTAMPTZ,
            data_length=13,
            max_size=13,
            # Oracle's default until the caller finds a declared one (#1308).
            scale=_DEFAULT_FRACTIONAL_PRECISION,
        )
    if oid in _NUMBER_OIDS:
        # A numeric(p, s) column reports its precision/scale; int / float / bare
        # numeric report None, which becomes Oracle's unconstrained NUMBER (0/0).
        return ColumnMeta(
            name=ident,
            data_type=TNS_TYPE_NUMBER,
            data_length=22,
            max_size=22,
            precision=desc.precision or 0,
            scale=desc.scale or 0,
        )
    if oid in _BINARY_FLOAT_OIDS:
        data_type, width = _BINARY_FLOAT_OIDS[oid]
        return ColumnMeta(
            name=ident, data_type=data_type, data_length=width, max_size=width
        )
    if oid in _TEMPORAL_OIDS:
        data_type, width = _TEMPORAL_OIDS[oid]
        # A TIMESTAMP [WITH LOCAL TIME ZONE] carries its fractional-seconds
        # precision as the scale, precision 0, as Oracle describes it (#1308).
        # PostgreSQL keeps timestamp(n)'s n in the type modifier, which psycopg
        # reports as the column's precision; none declared is Oracle's 6.
        scale = 0
        if data_type != TNS_TYPE_DATE:
            scale = (
                desc.precision
                if desc.precision is not None
                else _DEFAULT_FRACTIONAL_PRECISION
            )
        return ColumnMeta(
            name=ident,
            data_type=data_type,
            data_length=width,
            max_size=width,
            scale=scale,
        )
    if oid in _INTERVAL_OIDS:
        return ColumnMeta(
            name=ident, data_type=TNS_TYPE_INTERVALDS, data_length=11, max_size=11
        )
    if oid in _RAW_OIDS:
        width = max(
            (len(v) for v in values if isinstance(v, (bytes, bytearray, memoryview))),
            default=1,
        )
        return ColumnMeta(
            name=ident, data_type=TNS_TYPE_RAW, data_length=width, max_size=width
        )
    if oid in _TEXT_OIDS:
        # A varchar(n) / char(n) reports its declared n, which psycopg gives as
        # the display size, as Oracle reports a VARCHAR2(n)'s; the longest value
        # returned stood in for it, so the size changed with the data (#1385).
        # Only an undeclared one -- text, an expression -- is sized from values.
        width = desc.display_size or max(
            (len(str(v)) for v in values if v is not None), default=1
        )
        # A char(n) is Oracle's CHAR(n), blank-padded, and describes as one (#1416).
        data_type = TNS_TYPE_CHAR if oid == _BPCHAR_OID else TNS_TYPE_VARCHAR
        return ColumnMeta(
            name=ident, data_type=data_type, data_length=width, max_size=width
        )
    if oid == _XML_OID:
        # An xml column is an XMLType one: an ADT of SYS.XMLTYPE, as a live
        # server describes it, whose value the Mirror serves as the document the
        # backend hands over as a str (#1536).
        return ColumnMeta(
            name=ident,
            data_type=TNS_TYPE_ADT,
            data_length=2000,
            max_size=0,
            charset=0,
            csfrm=0,
            type_oid=_XMLTYPE_OID,
            type_schema=b'SYS',
            type_name=b'XMLTYPE',
        )
    raise UnsupportedFeature(
        f'column {name!r}: PostgreSQL type oid {oid} is not supported yet'
    )


# Comments ahead of a statement's first word. Every rewrite here recognises a
# statement by that word -- CREATE TABLE's types, DDL's autocommit, PL/SQL's
# routing -- so `-- make it\nCREATE TABLE t (n NUMBER(9))` went to PostgreSQL
# untranslated and failed on `number`, a type it does not have. Oracle ignores
# the comment, so drop it before anything looks at the statement.
_LEADING_COMMENTS = re.compile(r'\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)+', re.DOTALL)


# Native PostgreSQL SQL over the Oracle connection (#1556), for an application
# moving to PostgreSQL a statement at a time: such a statement skips the
# Oracle-to-PostgreSQL translation and runs as written. Per statement, an
# Oracle hint naming PG -- `/*+ PG */` -- at its start or right after its first
# keyword, where Oracle reads hints and a real Oracle ignores one it does not
# know. Per session, ALTER SESSION SET seerdb_dialect = 'postgres' | 'oracle'.
_ALTER_SESSION_DIALECT = re.compile(
    r"\s*ALTER\s+SESSION\s+SET\s+SEERDB_DIALECT\s*=\s*'?(POSTGRES|ORACLE)'?\s*;?\s*$",
    re.IGNORECASE,
)
_ALTER_SESSION_REPORT = re.compile(
    r"\s*ALTER\s+SESSION\s+SET\s+SEERDB_TRANSLATION_REPORT\s*=\s*'?(TRUE|FALSE)'?\s*;?\s*$",
    re.IGNORECASE,
)
# A statement as the translation report keys it (#1557): its string and numeric
# literals folded to `?`, a quoted identifier kept, its spacing collapsed, so
# the runs of one statement with different values count together.
_REPORT_LITERAL = re.compile(
    r"(\"(?:[^\"]|\"\")*\")|'(?:[^']|'')*'"
    r'|(?<![\w$#.:"])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?'
)
# How many runs the report buffers before it writes them out.
_REPORT_FLUSH_EVERY: Final = 100
_REPORT_UPSERT: Final = (
    'INSERT INTO sys.ora_translation_log AS l VALUES '
    '(%s, %s, %s, %s, %s, %s, %s, now(), now()) '
    'ON CONFLICT (statement_key) DO UPDATE SET '
    'rules = ARRAY(SELECT DISTINCT r FROM unnest(l.rules || excluded.rules) r '
    'ORDER BY r), runs = l.runs + excluded.runs, '
    'failures = l.failures + excluded.failures, '
    'last_error = coalesce(excluded.last_error, l.last_error), '
    'native = l.native OR excluded.native, last_seen = now()'
)


def _normalise_statement(sql: str) -> str:
    folded = _REPORT_LITERAL.sub(lambda m: m.group(1) or '?', sql)
    return ' '.join(folded.split())


@dataclass
class _ReportEntry:
    """One normalised statement's runs since the report last wrote (#1557)."""

    rules: set[str] = field(default_factory=set)
    runs: int = 0
    failures: int = 0
    last_error: str | None = None
    native: bool = False


_Ran = TypeVar('_Ran')


_PG_HINT = re.compile(
    r'(?:\s|--[^\n]*(?:\n|$)|/\*(?!\+)(?:[^*]|\*(?!/))*\*/)*(?:[A-Za-z]+\s*)?'
    r'/\*\+[^*]*\bPG\b',
    re.IGNORECASE | re.DOTALL,
)


def _strip_leading_comments(sql: str) -> str:
    return _LEADING_COMMENTS.sub('', sql, count=1)


# Oracle's reserved words, which cannot name a bind: `:ROWID` is refused
# ORA-01745 at parse. Unquoted names only -- a quoted one may be anything.
_ORACLE_RESERVED_WORDS = frozenset(
    'ACCESS ADD ALL ALTER AND ANY AS ASC AUDIT BETWEEN BY CHAR CHECK CLUSTER '
    'COLUMN COMMENT COMPRESS CONNECT CREATE CURRENT DATE DECIMAL DEFAULT DELETE '
    'DESC DISTINCT DROP ELSE EXCLUSIVE EXISTS FILE FLOAT FOR FROM GRANT GROUP '
    'HAVING IDENTIFIED IMMEDIATE IN INCREMENT INDEX INITIAL INSERT INTEGER '
    'INTERSECT INTO IS LEVEL LIKE LOCK LONG MAXEXTENTS MINUS MLSLABEL MODE MODIFY '
    'NOAUDIT NOCOMPRESS NOT NOWAIT NULL NUMBER OF OFFLINE ON ONLINE OPTION OR '
    'ORDER PCTFREE PRIOR PUBLIC RAW RENAME RESOURCE REVOKE ROW ROWID ROWNUM ROWS '
    'SELECT SESSION SET SHARE SIZE SMALLINT START SUCCESSFUL SYNONYM SYSDATE '
    'TABLE THEN TO TRIGGER UID UNION UNIQUE UPDATE USER VALIDATE VALUES VARCHAR '
    'VARCHAR2 VIEW WHENEVER WHERE WITH'.split()
)


def _refuse_reserved_bind_names(sql: str) -> None:
    # Oracle refuses a bind named by a reserved word, `:ROWID` say, with
    # ORA-01745 -- at parse and at execute alike, a query's parse included, which
    # is described by running it (#1371). PostgreSQL has no such rule, and the
    # ROWID pseudo-column rewrite turned such a bind into a syntax error.
    for name, quoted in bind_placeholders(sql, dedupe=True):
        if not quoted and name.upper() in _ORACLE_RESERVED_WORDS:
            raise BackendError(
                'invalid host/bind variable name',
                ora_code=ORA_INVALID_BIND_VARIABLE_NAME,
            )


# The statements PostgreSQL's EXPLAIN does not take; a parse of one is answered
# as before, with a bare success.
_NOT_EXPLAINABLE = re.compile(
    r'\s*(CREATE|ALTER|DROP|TRUNCATE|RENAME|COMMENT|GRANT|REVOKE|LOCK|COMMIT|'
    r'ROLLBACK|SAVEPOINT|SET|CALL|ANALYZE|AUDIT|NOAUDIT|PURGE|FLASHBACK)\b',
    re.IGNORECASE,
)


# The longest password Oracle 12.2+ accepts; measured on 23ai (#1266).
_MAX_PASSWORD_BYTES = 1024


_BUILTIN_OIDS = frozenset(
    _NUMBER_OIDS
    | _BINARY_FLOAT_OIDS.keys()
    | _TEMPORAL_OIDS.keys()
    | _INTERVAL_OIDS
    | _RAW_OIDS
    | _TEXT_OIDS
)


def _varchar_column(name: bytes, size: int = 128) -> ColumnMeta:
    return ColumnMeta(
        name=name, data_type=TNS_TYPE_VARCHAR, data_length=size, max_size=size
    )


def _number_column(name: bytes) -> ColumnMeta:
    return ColumnMeta(name=name, data_type=TNS_TYPE_NUMBER, data_length=22, max_size=22)


# The attribute cursor GET_TYPE_SHAPE returns, one column per field (#1134).
_ATTRIBUTE_CURSOR_COLUMNS = (
    _number_column(b'VERSION'),
    _varchar_column(b'NAME'),
    _number_column(b'ATTRIBUTE#'),
    _varchar_column(b'TYPE_NAME'),
    _varchar_column(b'TYPE_OWNER'),
    _varchar_column(b'TYPE_PACKAGE'),
    ColumnMeta(name=b'ATTR_TOID', data_type=TNS_TYPE_RAW, data_length=16, max_size=16),
    _varchar_column(b'INSTANTIABLE', 3),
    _varchar_column(b'SUPERTYPE_OWNER'),
    _varchar_column(b'SUPERTYPE_NAME'),
)


def _object_type_oid(pg_oid: int) -> bytes:
    # The 16-byte OID all_types reports for a composite: its PostgreSQL oid,
    # zero-padded, the same bytes the view's `decode(lpad(to_hex(...)))` builds.
    return pg_oid.to_bytes(16, 'big')


# A REF locator this backend issues (#1127): a tag, the target type's and the
# object table's pg oids, and the row's hidden object id. It is opaque to the
# client, which only hands it back.
_REF_LOCATOR_TAG = b'PGREF1'


def _ref_locator(type_pg_oid: int, table_oid: int, row_id: object) -> bytes:
    raw_id = (
        row_id.bytes if isinstance(row_id, uuid.UUID) else uuid.UUID(str(row_id)).bytes
    )
    return _REF_LOCATOR_TAG + struct.pack('>II', type_pg_oid, table_oid) + raw_id


def _parse_ref_locator(locator: bytes) -> tuple[int, int, uuid.UUID] | None:
    tag = len(_REF_LOCATOR_TAG)
    if len(locator) != tag + 8 + 16 or not locator.startswith(_REF_LOCATOR_TAG):
        return None
    type_pg_oid, table_oid = struct.unpack('>II', locator[tag : tag + 8])
    return type_pg_oid, table_oid, uuid.UUID(bytes=locator[tag + 8 :])


def _served_lobs(value: DbObject, image: ObjectImage) -> DbObject:
    # A bound object's or collection's LOB attributes carry locators the Mirror
    # handed out; each becomes the content served under it, or NULL for one it
    # never served, as the passthrough binds it (#1256).
    def content(_attr: dict, locator: object) -> object:
        served = (
            image.served_lob(bytes(locator)) if isinstance(locator, bytes) else None
        )
        return served[0] if served is not None else None

    mapped = map_object_lobs(value, content)
    return mapped if isinstance(mapped, DbObject) else value


def _array_literal(values: list) -> str:
    # A PostgreSQL array literal of `values` (#1276): NULL for None, every other
    # element double-quoted with `"` and `\\` escaped, so a string, a timestamp
    # or an inner array literal all go in as text PostgreSQL parses by the
    # element type.
    def element(value: object) -> str:
        if value is None:
            return 'NULL'
        if isinstance(value, (bytes, bytearray)):
            text = '\\x' + bytes(value).hex()
        elif isinstance(value, datetime.datetime | datetime.date):
            text = (
                value.isoformat(sep=' ')
                if isinstance(value, datetime.datetime)
                else value.isoformat()
            )
        else:
            text = str(value)
        return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'

    return '{' + ','.join(element(v) for v in values) + '}'


def _pg_oid_of(oid: bytes) -> int | None:
    # The inverse of _object_type_oid; None for an OID that cannot be one of
    # ours (a real Oracle OID a client carried over, or a malformed one).
    if len(oid) != 16:
        return None
    value = int.from_bytes(oid, 'big')
    return value if 0 < value <= 0xFFFFFFFF else None


# PostgreSQL's xml type, and SYS.XMLTYPE's type id -- the same on 11g and 23ai
# (all_types.type_oid) -- which an XMLType column describes with (#1536).
_XML_OID = 142
_XMLTYPE_OID = bytes.fromhex('00000000000000000000000000020100')
# SYS.XMLTYPE as an attribute's or element's type: the image walkers frame a
# value of it as an XMLType image (#1537).
_XMLTYPE_DBTYPE = DbObjectType('SYS', 'XMLTYPE', _XMLTYPE_OID, 1, [])
# What DBMS_PICKLER.GET_TYPE_SHAPE answers for SYS.XMLTYPE, measured on a live
# 23ai: version 1, this TDS -- an opaque type's -- and no attributes. A client
# resolves an XMLType column's type through it before reading a value (#1536).
_XMLTYPE_TDS = bytes.fromhex(
    '00000029260100010001ff2900000000001e1b000000193a2afd0000000d01000000070000'
    '0000000000090007'
)


def _object_column_meta(name: str, typ: DbObjectType) -> ColumnMeta:
    # An object column describes as Oracle's does: ADT with the type's
    # identity, a 2000-byte data length, no max size and no character set --
    # the values a real server sends for one (#1127).
    return ColumnMeta(
        name=name.upper().encode('utf-8'),
        data_type=TNS_TYPE_ADT,
        data_length=2000,
        max_size=0,
        charset=0,
        csfrm=0,
        type_oid=typ.oid,
        type_schema=typ.schema.encode('ascii'),
        type_name=typ.name.encode('ascii'),
    )


# A session whose PostgreSQL connection is gone -- ALTER SYSTEM KILL SESSION ended
# its backend, or the server did -- answers every call as Oracle answers a killed
# session's: ORA-00028 (#1367). A client takes that code as the end of the
# connection (python-oracledb raises DPY-4011 and closes its side), where the
# ORA-00600 an escaping psycopg error became, or the ORA-00900 of an unmapped
# SQLSTATE, said neither. psycopg reports a terminated connection as closed.
def _session_terminated() -> BackendError:
    return BackendError('session has been terminated', ora_code=ORA_SESSION_TERMINATED)


def _while_connected(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        if self._conn.closed:
            raise _session_terminated()
        try:
            return method(self, *args, **kwargs)
        except (psycopg.Error, BackendError):
            if self._conn.closed:
                raise _session_terminated() from None
            raise

    return call


class PostgresBackend:
    """A :class:`~seerdb.server.Backend` over a psycopg connection.

    One instance per Mirror session. ``conninfo`` is a libpq connection string,
    e.g. ``host=127.0.0.1 port=5432 user=pyo password=... dbname=mirror``.

    The connection is transactional (``autocommit`` off), so the Mirror's
    commit / rollback are real: work persists only on commit and is discarded on
    rollback. Each statement runs inside an implicit ``SAVEPOINT`` so a failed
    statement rolls back only itself — the transaction (and any earlier
    uncommitted work) survives, matching Oracle's statement-level error model
    rather than PostgreSQL's abort-the-whole-transaction default.
    """

    capabilities = frozenset({Capability.TRANSACTIONS})
    # This demo speaks the 12.1 WIRE protocol (field version), which is also the
    # release it reports (server_identity). Those were deliberately apart while
    # the wire was 11.2: the dialect reads the RELEASE to pick native
    # OFFSET/FETCH pagination and identity columns -- both of which PostgreSQL
    # runs directly -- instead of Oracle's nested-ROWNUM pagination, which has no
    # faithful PostgreSQL rewrite (#33). They now agree.
    #
    # 12.1 rather than 12.2, and deliberately: a modern thin client refuses any
    # server below protocol version 315, which is 12.1's, so this is the LOWEST
    # version at which one will talk to the Mirror at all (#1127). Going to 12.2
    # was measured too and costs far more -- and a client can negotiate DOWN to
    # 12.1 whichever we advertise, so 12.1 has to work regardless.
    field_version = FIELD_VERSION_12_1
    server_identity = IDENTITY_12_1
    # This backend has Oracle's DUAL (orafce's, and sys.dual), so the compat
    # wrapper hands it `... FROM DUAL` as the client sent it: the translation
    # report then sees the application's own text (#1561).
    has_dual = True

    def __init__(
        self,
        conninfo: str = '',
        *,
        credentials: Credentials | None = None,
        translation_report: bool = False,
    ) -> None:
        self._conn = psycopg.connect(conninfo)
        # The translation report (#1557): whether it is on, the runs it has not
        # written yet, and its own autocommit connection, opened when it first
        # writes -- so writing it never joins, ends or aborts the client's
        # transaction.
        self._conninfo = conninfo
        self._reporting = translation_report
        self._report_runs: dict[str, _ReportEntry] = {}
        self._report_conn: psycopg.Connection | None = None
        # Disable psycopg's automatic server-side prepared statements. Every
        # statement runs inside a SAVEPOINT, and a ROLLBACK TO SAVEPOINT deallocates
        # any prepared statement created after that savepoint — which desyncs
        # psycopg's prepared-statement cache from the server ("prepared statement
        # _pgN_M does not exist"). A proxy backend running varied SQL gains little
        # from the cache anyway; the pipeline below is the real round-trip win.
        self._conn.prepare_threshold = None
        # Whether a routine the running call reached asked for a commit (#1630).
        self._commit_requested = False
        self._conn.add_notice_handler(self._hear_commit_request)
        # The portals the running call returned as implicit results (#1617).
        self._implicit_portals: list[str] = []
        self._conn.add_notice_handler(self._hear_implicit_result)
        # Shared, not copied, when a dict is given: the example hands one map to
        # every backend it creates, and ALTER USER ... IDENTIFIED BY rewrites an
        # entry in place so the new password reaches the next login. A read-only
        # mapping is copied into a dict the rewrite can touch.
        self._credentials: dict[str, str] = (
            credentials if isinstance(credentials, dict) else dict(credentials or {})
        )
        # Index-organized tables this session created, with their primary-key
        # columns, for the logical-rowid rendering.
        self._iot_pk: dict[str, list[str]] = {}
        # Whether the client has taken a SAVEPOINT in the open transaction, which
        # a transaction that has written nothing must keep for it (#1190).
        self._user_savepoint = False
        # The SQL dialect a statement with no PG hint is in (#1556): 'oracle',
        # translated, or 'postgres', run as written.
        self._sql_dialect = 'oracle'
        # The Oracle object type each PostgreSQL composite oid stands for, with
        # its registered psycopg CompositeInfo -- or None for an oid that is not
        # an object type, so a column of it is looked up once (#1127).
        self._object_types: dict[int, tuple[DbObjectType, CompositeInfo] | None] = {}
        # REFs (#1127): each `<type>$ref` composite's target type (None for any
        # other composite), and the registered companion per target type.
        self._ref_targets: dict[int, int | None] = {}
        self._ref_composites: dict[int, CompositeInfo] = {}
        # Collections (#1206): the collection type each array-domain oid stands
        # for (None for any other type); whether a result oid is an array at all;
        # and the declared type of each (relid, attnum) an array column traces
        # back to -- PostgreSQL describes a domain column by its base type.
        self._collection_types: dict[int, DbObjectType | None] = {}
        # The attribute types being described, so a type that reached itself
        # would be refused rather than recursed into (#1265).
        self._describing: set[int] = set()
        self._array_oids: dict[int, bool] = {}
        self._column_types: dict[tuple[int, int], int] = {}
        # Pipeline mode ships a statement's SAVEPOINT / statement / RELEASE in one
        # network round-trip instead of three (a 3x per-statement latency cut
        # against a remote database). It needs libpq >= 14; older builds fall back
        # to the sequential path.
        try:
            self._use_pipeline = psycopg.pq.version() >= 140000
        except Exception:
            self._use_pipeline = False
        self._conn.adapters.register_dumper(_PortalName, _PortalNameDumper)
        self._conn.adapters.register_dumper(_TypedNull, _TypedNullDumper)
        # The portals opened for cursors bound IN, numbered (#1609).
        self._portals_opened = 0
        # Lean on the `orafce` extension for Oracle-compatible SQL functions —
        # nvl, decode, to_char / to_date, add_months, instr, and much more —
        # rather than hand-rolling each rewrite. It installs those into the
        # `oracle` schema, so put it on the search_path; then only the idioms
        # orafce does NOT cover are translated in _translate_idioms. orafce is a
        # requirement of this backend (see the module docstring). Best-effort so a
        # PostgreSQL without it still starts — the uncovered idioms just fail as
        # before.
        try:
            self._conn.execute('CREATE EXTENSION IF NOT EXISTS orafce')
        except psycopg.Error:
            self._conn.rollback()
        # The Oracle data dictionary (#759) lives in a dedicated `sys` schema —
        # like Oracle's SYS — so its views are never reflected as user objects.
        try:
            self._conn.execute('CREATE SCHEMA IF NOT EXISTS sys')
            # SYSTEM is a user every Oracle database has, so a client may point
            # its session at it (ALTER SESSION SET CURRENT_SCHEMA = SYSTEM). That
            # becomes a search_path, and PostgreSQL's current_schema() passes
            # over a schema that does not exist -- the session stayed where it
            # was and SYS_CONTEXT went on naming it. An empty one is enough.
            self._conn.execute('CREATE SCHEMA IF NOT EXISTS system')
        except psycopg.Error:
            self._conn.rollback()
        # `sys` ahead of `oracle`: orafce ships its own `user_tables` and
        # `user_tab_columns` in the `oracle` schema, and they select from
        # information_schema with no owner filter at all -- every base table in
        # the database, PostgreSQL's own catalogs included, reported as though
        # the connected user owned them. Those are the only two names the two
        # schemas share, so this ordering changes nothing else: user objects
        # still resolve first through the schema ahead of both, and the rest of
        # orafce is still reached through `oracle` (#818).
        self._conn.execute(f'SET search_path TO {_SEARCH_PATH_TAIL}')
        self._conn.execute(_ORAFCE_SESSION_SETTINGS)
        # Create + register the composite that backs TIMESTAMP WITH TIME ZONE, so
        # its columns come back as a typed tuple the read path can re-tag with the
        # entered offset (#519). Best-effort: a backend that can't create the type
        # just leaves WITH TIME ZONE unsupported, like the orafce idioms above.
        self._tstz_oid: int | None = None
        self._tstz_info: CompositeInfo | None = None
        try:
            self._conn.execute(_TSTZ_TYPE_DDL)
            info = CompositeInfo.fetch(self._conn, _TSTZ_TYPE)
            if info is not None:
                register_composite(info, self._conn)
                self._tstz_oid = info.oid
                self._tstz_info = info
        except psycopg.Error:
            self._conn.rollback()
        # The BFILE composite (#1669), read back as a tuple of the two names.
        self._bfile_oid: int | None = None
        try:
            self._conn.execute(_BFILE_TYPE_DDL)
            bfile_info = CompositeInfo.fetch(self._conn, _BFILE_TYPE)
            if bfile_info is not None:
                register_composite(bfile_info, self._conn)
                self._bfile_oid = bfile_info.oid
        except psycopg.Error:
            self._conn.rollback()
        # Create the typed domains and map each domain's oid to the Oracle wire type
        # it stands for, so a result column tracing back to one is encoded as that
        # Oracle type — ora_clob / ora_blob as a LOB (#534), ora_intervalym as
        # INTERVAL YEAR TO MONTH (#504). Best-effort, like the composite above; a
        # (relid, attnum) → type cache avoids re-querying the catalog for a column
        # already seen. The ora_intervalym oid is kept on its own for the OUT-bind
        # path, which has no result column to trace and matches on the arg type.
        self._intervalym_oid: int | None = None
        self._domain_type_by_oid: dict[int, int] = {}
        self._domain_col_cache: dict[tuple[int, int], int | None] = {}
        try:
            self._conn.execute(_LOB_TYPE_DDL)
            self._conn.execute(_INTERVALYM_TYPE_DDL)
            self._conn.execute(_DATE_TYPE_DDL)
            self._conn.execute(_DATE_ARITHMETIC_DDL)
            self._conn.execute(_ROWID_TYPE_DDL)
            for name, tns_type in (
                (_CLOB_TYPE, TNS_TYPE_CLOB),
                (_BLOB_TYPE, TNS_TYPE_BLOB),
                (_INTERVALYM_TYPE, TNS_TYPE_INTERVALYM),
                (_DATE_TYPE, TNS_TYPE_DATE),
            ):
                row = self._conn.execute(
                    'SELECT oid FROM pg_type WHERE typname = %s', (name,)
                ).fetchone()
                if row is not None:
                    self._domain_type_by_oid[row[0]] = tns_type
                    if name == _INTERVALYM_TYPE:
                        self._intervalym_oid = row[0]
            # A LOB inside a composite or an array is typed by its domain, which
            # psycopg does not know: a BLOB attribute came back as its hex text.
            # Load both domains, and arrays of them, as their base types (#1256).
            from psycopg.types import TypeInfo
            from psycopg.types.array import register_array
            from psycopg.types.string import (
                ByteaBinaryLoader,
                ByteaLoader,
                TextBinaryLoader,
                TextLoader,
            )

            for name, loaders in (
                (_CLOB_TYPE, (TextLoader, TextBinaryLoader)),
                (_BLOB_TYPE, (ByteaLoader, ByteaBinaryLoader)),
            ):
                domain = TypeInfo.fetch(self._conn, name)
                if domain is not None:
                    for loader in loaders:
                        self._conn.adapters.register_loader(domain.oid, loader)
                    register_array(domain, self._conn)
            # Preserve an interval's months through psycopg (its default loader
            # flattens them to a timedelta), so a YEAR TO MONTH value survives (#504).
            self._conn.adapters.register_loader('interval', _IntervalMonthsTextLoader)
            # A date before year 1 loads as a BcDate the Mirror can serve (#1063).
            from psycopg.types.datetime import DateLoader, TimestampLoader

            self._conn.adapters.register_loader('date', _bc_date_loader(DateLoader))
            self._conn.adapters.register_loader(
                'timestamp', _bc_date_loader(TimestampLoader)
            )
            # A DATE inside a composite or an array is typed by the ora_date
            # domain, which psycopg does not know: it came back as text. Load it,
            # and arrays of it, as the timestamp it is over (#1548).
            date_domain = TypeInfo.fetch(self._conn, _DATE_TYPE)
            if date_domain is not None:
                self._conn.adapters.register_loader(
                    date_domain.oid, _bc_date_loader(TimestampLoader)
                )
                register_array(date_domain, self._conn)
            self._conn.adapters.register_loader('interval', _IntervalMonthsBinaryLoader)
        except psycopg.Error:
            self._conn.rollback()
        # Install the Oracle scalar helper functions (hextoraw, rawtohex,
        # empty_clob / empty_blob, from_tz), so those call sites need no rewrite
        # (#513). Best-effort like the type / domain setup above; empty_clob /
        # empty_blob return the LOB domains just created, so this runs after them.
        try:
            self._conn.execute(_HELPER_FUNCTIONS_DDL)
        except psycopg.Error:
            self._conn.rollback()
        # The helpers that need orafce, apart so they cannot take the others
        # down without it (#1299).
        try:
            self._conn.execute(_ORAFCE_HELPERS_DDL)
        except psycopg.Error:
            self._conn.rollback()
        # UTL_RAW as PostgreSQL functions (orafce ships no utl_raw) (#765).
        try:
            self._conn.execute(_UTL_RAW_DDL)
        except psycopg.Error:
            self._conn.rollback()
        # DBMS_UTILITY entry points orafce does not ship (#764).
        try:
            self._conn.execute(_DBMS_UTILITY_DDL)
        except psycopg.Error:
            self._conn.rollback()
        # DBMS_LOCK.SLEEP / DBMS_SESSION.SLEEP (#1511).
        try:
            self._conn.execute(_DBMS_SLEEP_DDL)
        except psycopg.Error:
            self._conn.rollback()
        # DBMS_SQL.RETURN_RESULT (#1617).
        try:
            self._conn.execute(_DBMS_SQL_RETURN_RESULT_DDL)
        except psycopg.Error:
            self._conn.rollback()
        # DBMS_OUTPUT.GET_LINES's collection type (#1411).
        try:
            self._conn.execute(_DBMS_OUTPUT_LINES_DDL)
        except psycopg.Error:
            self._conn.rollback()
        # Oracle data-dictionary emulation (#759): SYS_CONTEXT + catalog views.
        # Installed only when missing or changed, not on every connect: CREATE
        # OR REPLACE VIEW takes an exclusive lock, so a client reading one of the
        # views inside an open transaction made every new connection wait for
        # it to end -- a login hang one step removed from its cause (#1152).
        # When it does run -- a first install, or a seerdb whose views differ --
        # a held view makes it give up after a short wait rather than hang; the
        # session then works with the views already there, and a later
        # connection installs them. Committed first, so a give-up here cannot
        # take the helpers above down with it.
        self._conn.commit()
        try:
            self._install_dictionary()
        except psycopg.Error:
            self._conn.rollback()
        self._conn.commit()
        # The quoted-name catalog (#1204) comes with the dictionary; a session
        # whose dictionary could not be installed does without it.
        row = self._conn.execute(
            "SELECT to_regclass('sys.ora_quoted_names') IS NOT NULL"
        ).fetchone()
        self._has_quoted_names = bool(row and row[0])
        row = self._conn.execute(
            "SELECT to_regclass('sys.ora_tstz_precision') IS NOT NULL"
        ).fetchone()
        self._has_tstz_precision = bool(row and row[0])
        # A WITH TIME ZONE column's recorded precision, by (relid, attnum), None
        # for one not recorded (#1308); any DDL starts it over.
        self._tstz_precision_cache: dict[tuple[int, int], int | None] = {}
        self._quoted_col_cache: dict[tuple[int, int], bool] = {}
        # Whether a table column is NOT NULL, by (relid, attnum) (#1307); any DDL
        # starts it over, as a column's constraint may have changed.
        self._not_null_cache: dict[tuple[int, int], bool] = {}
        self._json_col_cache: dict[tuple[int, int], bool] = {}
        # INVISIBLE columns (#1195): whether the catalog exists, whether any
        # table has one (None: not yet read; reset by any DDL), and the visible
        # columns of the tables that do.
        row = self._conn.execute(
            "SELECT to_regclass('sys.ora_invisible_columns') IS NOT NULL"
        ).fetchone()
        self._has_invisible_catalog = bool(row and row[0])
        # Declared Oracle column types (#1386): whether the catalog exists, and its
        # rows by (relid, attnum) -- None for a column with none; any DDL starts
        # it over.
        row = self._conn.execute(
            "SELECT to_regclass('sys.ora_columns') IS NOT NULL"
        ).fetchone()
        self._has_column_catalog = bool(row and row[0])
        row = self._conn.execute(
            "SELECT to_regclass('sys.ora_collection_elements') IS NOT NULL"
        ).fetchone()
        self._has_collection_elements = bool(row and row[0])
        # A package's types (#1607): whether their catalog exists, and each
        # PostgreSQL type's -- None for one that is no package's; any DDL starts
        # it over.
        row = self._conn.execute(
            "SELECT to_regclass('sys.ora_plsql_types') IS NOT NULL"
        ).fetchone()
        self._has_package_catalog = bool(row and row[0])
        self._plsql_types: dict[int, tuple[str, str, str, dict] | None] = {}
        self._column_type_cache: dict[tuple[int, int], tuple | None] = {}
        self._collection_name_cache: frozenset[str] | None = None
        # Each package's functions a call may name without parentheses (#1651).
        self._package_function_cache: dict[str, frozenset[str]] | None = None
        # RAW targets (#1496): whether any column or attribute is a recorded
        # RAW, each relation's column order and RAW positions, and each object
        # type's RAW attribute positions, by name; any DDL starts them over.
        self._has_raw_targets_cache: bool | None = None
        self._raw_layout_cache: dict[str, tuple[list[str], frozenset[int]] | None] = {}
        self._raw_constructor_cache: dict[str, frozenset[int]] | None = None
        self._any_invisible: bool | None = None
        self._visible_cache: dict[str, list[str] | None] = {}
        self._conn.commit()

    def _install_dictionary(self) -> None:
        row = self._conn.execute(
            "SELECT obj_description(to_regnamespace('sys'), 'pg_namespace')"
        ).fetchone()
        if row is not None and row[0] == _DICTIONARY_STAMP:
            return
        self._conn.execute("SET LOCAL lock_timeout = '2s'")
        try:
            with self._conn.transaction():
                self._conn.execute(_ORACLE_DICTIONARY_DDL)
        except psycopg.errors.InvalidTableDefinition:
            # A dictionary from another seerdb -- a newer one, rolled back from,
            # or a branch's -- has a view CREATE OR REPLACE VIEW cannot turn into
            # this one: a column this one does not define, or a column's type
            # changed. It failed every connection for good (#1571). The views go
            # and come back, in the same transaction, so a failure here leaves
            # the old ones as they were. A view of the user's own built on one of
            # them goes with it: CASCADE is the only way past the dependency.
            self._conn.execute(
                f'DROP VIEW IF EXISTS {", ".join(_DICTIONARY_VIEWS)} CASCADE'
            )
            self._conn.execute(_ORACLE_DICTIONARY_DDL)
        self._conn.execute(f"COMMENT ON SCHEMA sys IS '{_DICTIONARY_STAMP}'")

    def set_client_identity(self, identity: dict[str, str]) -> None:
        # program / machine / terminal / osuser, as the client declared them in
        # its first login message; recorded for v$session (#1212).
        self._client_identity = dict(identity)

    def open_session(self, connect_attrs: dict[str, str]) -> None:
        # The driver name arrives only in the second login message (#1212).
        self._client_driver = connect_attrs.get('driver_name')
        self._open_as(connect_attrs.get('proxy_client_name'))
        edition = connect_attrs.get('edition')
        if edition:
            # The edition the client connected in (#1662); one that does not
            # exist refuses the login, ORA-38802, as Oracle's does.
            with self._rolled_back_on_error():
                self._conn.execute('SELECT sys.ora_set_edition(%s)', (edition.upper(),))
                self._conn.commit()
        # The service the client connected to, for USERENV SERVICE_NAME (#1409),
        # kept in a session setting as the tracing attributes are.
        service = connect_attrs.get('service_name')
        if service:
            try:
                self._conn.execute(
                    "SELECT set_config('seerdb.service_name', %s, false)", (service,)
                )
                # A setting made in a transaction that rolls back is undone with
                # it, and nothing has run yet for this session to keep open.
                self._conn.commit()
            except psycopg.Error:
                self._conn.rollback()

    def _open_as(self, target: str | None) -> None:
        # The session's user (#1620): the login's, or under a proxy login
        # (`user[target]`) the target's, whose schema it then starts in, with
        # the user who logged in as its PROXY_USER. Oracle admits only a target
        # granted CONNECT THROUGH the user; the Mirror keeps no such grant and
        # admits any. Committed at once, as the search_path at login is.
        login = getattr(self, '_login_user', None)
        if login is None:
            return
        user = target.upper() if target else login
        try:
            self._conn.execute(
                "SELECT set_config('seerdb.session_user', %s, false), "
                "set_config('seerdb.proxy_user', %s, false)",
                (user, login if target else ''),
            )
            if target:
                self._conn.execute(
                    sql.SQL('SET search_path TO {}, ' + _SEARCH_PATH_TAIL).format(
                        sql.Identifier(target.lower())
                    )
                )
                self._login_user = user
            self._conn.commit()
        except psycopg.Error:
            self._conn.rollback()

    def _record_session(self) -> None:
        # This session's row in sys.ora_sessions, and none for backends that have
        # ended. Best effort: a login must not fail over its v$session row.
        identity = getattr(self, '_client_identity', {})
        program = identity.get('program')
        try:
            if program:
                self._conn.execute(
                    "SELECT set_config('application_name', %s, false)", (program[:63],)
                )
            self._conn.execute(
                'DELETE FROM sys.ora_sessions WHERE pid NOT IN '
                '(SELECT pid FROM pg_stat_activity)'
            )
            self._conn.execute(
                'INSERT INTO sys.ora_sessions VALUES (pg_backend_pid(), %s, %s, %s, %s, %s, %s) '
                'ON CONFLICT (pid) DO UPDATE SET username = excluded.username, '
                'program = excluded.program, machine = excluded.machine, '
                'terminal = excluded.terminal, osuser = excluded.osuser, '
                'driver = excluded.driver',
                (
                    getattr(self, '_login_user', None),
                    program,
                    identity.get('machine'),
                    identity.get('terminal'),
                    identity.get('osuser'),
                    getattr(self, '_client_driver', None),
                ),
            )
            self._conn.commit()
        except psycopg.Error:
            self._conn.rollback()

    def session_info(self) -> SessionInfo:
        """The session's identity, for the Mirror's login reply (#1212).

        A client reads its session id and serial only from that reply, never by
        querying, so without this it reported a placeholder for the life of the
        connection. The SID is the backend's pid, as sys_context('userenv',
        'sid') reports it, so SQL and the reply agree; the names are the ones
        sys_context gives.
        """
        # The last login hook to run, so the session is recorded for v$session
        # here, with everything the client declared by now.
        self._record_session()
        row = self._conn.execute(
            'SELECT pg_backend_pid(), sys.ora_serial(pg_backend_pid()), '
            'upper(current_database())'
        ).fetchone()
        self._conn.commit()
        if row is None:
            return SessionInfo()
        (pid, serial, name) = row
        return SessionInfo(
            session_id=pid, serial_num=serial or 0, instance_name=name, db_name=name
        )

    def authenticate(self, username: str) -> str | None:
        # The login store the Mirror authenticates clients against — separate
        # from the libpq `conninfo` the backend itself connects to PostgreSQL
        # with. A production backend might instead consult a PG table here.
        secret = credential_lookup(self._credentials, username)
        self._login_user = username.upper()
        if secret is not None:
            # An Oracle session's current schema starts as the login user's, so
            # an unqualified name resolves there first (#1188) -- the path ALTER
            # SESSION SET CURRENT_SCHEMA builds. PostgreSQL skips a schema that
            # does not exist, so a user without one resolves as before. Committed
            # at once: a SET inside a transaction that rolls back is undone.
            self._conn.execute(
                sql.SQL('SET search_path TO {}, ' + _SEARCH_PATH_TAIL).format(
                    sql.Identifier(username.lower())
                )
            )
            self._conn.execute(_ORAFCE_SESSION_SETTINGS)
            # The session's user from the login on (#1621: what it may see), as
            # a proxy login then changes it (#1620).
            self._conn.execute(
                "SELECT set_config('seerdb.session_user', %s, false)",
                (username.upper(),),
            )
            self._conn.commit()
        return secret

    @property
    def closed(self) -> bool:
        # The PostgreSQL connection is gone: ALTER SYSTEM KILL SESSION ended it, or
        # the server did (#1367). Every call then answers ORA-00028. psycopg only
        # learns of it when it next uses the connection, and a statement answered
        # without the backend never does. An idle connection has nothing to read,
        # though, until the server terminates it -- it sends a FATAL, then closes
        # -- so while the socket is readable its input is fed to libpq: the first
        # read takes the FATAL, the next meets the end of the stream, and libpq
        # marks the connection bad. No round trip.
        try:
            for _read in range(4):
                if self._conn.closed:
                    return True
                readable, _w, _x = select.select([self._conn.fileno()], [], [], 0)
                if not readable:
                    break
                self._conn.pgconn.consume_input()
        except (OSError, ValueError, psycopg.Error):
            return True
        return bool(self._conn.closed)

    def ping(self) -> None:
        # A health check -- conn.ping(), or a pool checking a connection before
        # handing it out. A killed session answers ORA-00028 (#1367), so the pool
        # drops it; a live one needs no round trip, `closed` having looked.
        if self.closed:
            raise _session_terminated()

    @_while_connected
    @_plainly_quoted
    def open_ref_cursor(self, query: str, skip: int = 0) -> object:
        """A cursor of the client's, bound IN as a REF CURSOR (#1609): its query
        opened as a portal of this session, past the ``skip`` rows the client
        has had already, and named as a SYS_REFCURSOR parameter takes it -- so
        a routine's FETCH goes on where the client stopped, as in Oracle. The
        portal lives as long as the transaction, or until the routine closes it.
        """
        translated = _translate_idioms(
            _translate_plsql_block(
                _translate_routine_ddl(
                    _translate_ddl(_translate_admin(_quote_reserved_names(query)))
                )
            )
        )
        self._portals_opened += 1
        name = f'ora_cursor_{self._portals_opened}'
        with self._rolled_back_on_error(original=query):
            self._conn.execute(
                sql.SQL('DECLARE {} CURSOR FOR ')
                .format(sql.Identifier(name))
                .as_string(self._conn)
                + translated
            )
            if skip:
                self._conn.execute(
                    sql.SQL('MOVE FORWARD {} IN {}').format(
                        sql.Literal(skip), sql.Identifier(name)
                    )
                )
        return _PortalName(name)

    @_plainly_quoted
    def parse(self, sql: str) -> None:
        """Validate a statement without running it -- ``cursor.parse()`` of
        anything that is not a query.

        Without this the Mirror answered its own bare success and every
        parse-time error was lost. Oracle refuses a bind named by a reserved
        word (ORA-01745), a rule PostgreSQL does not have, so it is checked
        here; the rest is PostgreSQL's EXPLAIN of the translated statement,
        which plans without running, inside a savepoint so a refusal leaves the
        session as it was. DDL, PL/SQL and transaction control have no EXPLAIN
        and keep the bare success they had.
        """
        native = self._is_native(sql)
        sql = _strip_leading_comments(sql)
        _refuse_reserved_bind_names(sql)
        if is_plsql(sql) or _NOT_EXPLAINABLE.match(sql):
            return
        translated = (
            sql
            if native
            else _translate_idioms(
                _translate_plsql_block(
                    _translate_routine_ddl(
                        _translate_ddl(
                            _translate_admin(
                                _quote_reserved_names(strip_returning_into(sql))
                            )
                        )
                    )
                )
            )
        )
        placeholders = placeholder_count(sql)
        params: dict | None = None
        if placeholders:
            translated, params = _translate_binds(translated, [None] * placeholders)
        try:
            with self._under_savepoint('_mirror_parse'):
                self._conn.execute(f'EXPLAIN {translated}', params)
        except psycopg.Error as exc:
            # A bind whose type only its value would settle is not an error in
            # Oracle, whose parse has no value either.
            if getattr(exc, 'sqlstate', None) == '42P18':
                return
            raise _backend_error(exc, original=sql, translated=translated) from exc
        self._release_read_locks()

    @contextmanager
    def _under_savepoint(self, name: str = '_mirror_stmt') -> Iterator[None]:
        # The block runs under a savepoint: one that raises is rolled back to it,
        # leaving the rest of the transaction usable as Oracle's is, and the
        # savepoint goes either way. The caller maps the error, AFTER the
        # rollback here: naming it can read the catalog, which a failed
        # transaction refuses (#1632).
        self._conn.execute(f'SAVEPOINT {name}')
        try:
            yield
        except Exception:
            self._conn.execute(f'ROLLBACK TO SAVEPOINT {name}')
            self._conn.execute(f'RELEASE SAVEPOINT {name}')
            raise
        self._conn.execute(f'RELEASE SAVEPOINT {name}')

    @contextmanager
    def _rolled_back_on_error(self, **context: Any) -> Iterator[None]:
        # A PostgreSQL failure in the block rolls the transaction back and is
        # raised as the ORA error it maps to, `context` passed to the mapping.
        try:
            yield
        except psycopg.Error as exc:
            self._conn.rollback()
            raise _backend_error(exc, **context) from exc

    def _release_read_locks(self) -> None:
        """End the open transaction if it has written nothing (#1190).

        An Oracle query takes no table lock. A PostgreSQL read takes
        AccessShareLock and keeps it until its transaction ends, and a client
        has no reason to commit after a plain query, so the lock outlived the
        query for as long as the session did: another session's TRUNCATE, DROP
        or ALTER then waited on it for ever. A transaction with no transaction
        id has written nothing, not even a row lock (SELECT ... FOR UPDATE
        assigns one), so committing it only lets the read locks go. One the
        client took a SAVEPOINT in is kept, as the commit would destroy it.
        """
        if self._user_savepoint:
            return
        if self._conn.info.transaction_status != psycopg.pq.TransactionStatus.INTRANS:
            return
        row = self._conn.execute(
            'SELECT pg_current_xact_id_if_assigned() IS NULL'
        ).fetchone()
        if row is not None and row[0]:
            self._conn.commit()

    @_while_connected
    @_plainly_quoted
    def execute(self, sql: str, binds: Sequence = ()) -> Result:
        return self._committing(
            lambda: self._with_implicit_results(
                lambda: self._reported(sql, lambda: self._execute_statement(sql, binds))
            )
        )

    def _hear_implicit_result(self, diagnostic: psycopg.errors.Diagnostic) -> None:
        message = diagnostic.message_primary or ''
        if message.startswith(_IMPLICIT_RESULT):
            self._implicit_portals.append(message[len(_IMPLICIT_RESULT) :])

    def _with_implicit_results(self, call: Callable[[], Result]) -> Result:
        # A call whose block returned cursors through DBMS_SQL.RETURN_RESULT
        # (#1617) answers with their rows as implicit results, in the order it
        # returned them, each portal fetched and closed -- before a COMMIT the
        # block asked for, which would close them.
        self._implicit_portals = []
        result = call()
        portals, self._implicit_portals = self._implicit_portals, []
        if not portals:
            return result
        implicit = []
        for portal in portals:
            name = sql.Identifier(portal)
            fetch = sql.SQL('FETCH ALL FROM {}').format(name)
            with self._conn.cursor() as cursor:
                cursor.execute(fetch)
                fetched = self._build_result(cursor)
                cursor.execute(sql.SQL('CLOSE {}').format(name))
            implicit.append((fetched.columns, fetched.rows))
        return replace(result, implicit_results=implicit)

    def _hear_commit_request(self, diagnostic: psycopg.errors.Diagnostic) -> None:
        if diagnostic.message_primary == _COMMIT_REQUEST:
            self._commit_requested = True

    def _committing(self, call: Callable[[], _Ran]) -> _Ran:
        # A client's call, committed after it succeeds when a routine it reached
        # ran a COMMIT (#1630). One that fails commits nothing: its savepoint
        # undoes the work, Oracle's after the COMMIT and this one's before it too.
        self._commit_requested = False
        try:
            result = call()
        except BaseException:
            self._commit_requested = False
            raise
        if self._commit_requested:
            self._commit_requested = False
            self.commit()
        return result

    def _reported(self, sql: str, run: Callable[[], _Ran]) -> _Ran:
        # Run a statement, and while the translation report is on, note what
        # translating it took and how it ended (#1557).
        if not self._reporting:
            return run()
        rules: list[str] = []
        token = _TRANSLATION_RULES.set(rules)
        try:
            result = run()
        except Exception as error:
            self._record_run(sql, rules, error)
            raise
        finally:
            _TRANSLATION_RULES.reset(token)
        self._record_run(sql, rules, None)
        return result

    def _record_run(self, sql: str, rules: list[str], error: Exception | None) -> None:
        if _ALTER_SESSION_REPORT.match(sql) or _ALTER_SESSION_DIALECT.match(sql):
            return
        entry = self._report_runs.setdefault(_normalise_statement(sql), _ReportEntry())
        entry.rules.update(rules)
        entry.runs += 1
        entry.native = entry.native or self._is_native(sql)
        if error is not None:
            entry.failures += 1
            entry.last_error = str(error).splitlines()[0][:500] if str(error) else ''
        if sum(e.runs for e in self._report_runs.values()) >= _REPORT_FLUSH_EVERY:
            self._flush_report()

    def _flush_report(self) -> None:
        # Write the buffered runs out, on the report's own connection. A failure
        # loses them rather than the client's statement.
        if not self._report_runs:
            return
        rows = [
            (
                hashlib.sha256(statement.encode()).hexdigest(),
                statement,
                sorted(entry.rules),
                entry.runs,
                entry.failures,
                entry.last_error,
                entry.native,
            )
            for statement, entry in self._report_runs.items()
        ]
        self._report_runs = {}
        try:
            if self._report_conn is None:
                self._report_conn = psycopg.connect(self._conninfo, autocommit=True)
            with self._report_conn.cursor() as cursor:
                cursor.executemany(_REPORT_UPSERT, rows)
        except psycopg.Error:
            pass

    def _execute_statement(self, sql: str, binds: Sequence) -> Result:
        # A PL/SQL block from callproc / callfunc arrives with BindVar binds (the
        # Mirror's OUT-bind flow); run it via CALL / SELECT and return the OUT
        # values (#503). An ordinary statement's BindVar is a typed NULL, which
        # _translate_binds casts (#699).
        switch = _ALTER_SESSION_DIALECT.match(sql)
        if switch is not None:
            self._sql_dialect = switch.group(1).lower()
            return Result()
        report = _ALTER_SESSION_REPORT.match(sql)
        if report is not None:
            self._reporting = report.group(1).upper() == 'TRUE'
            if not self._reporting:
                self._flush_report()
            return Result()
        native = self._is_native(sql)
        sql = _strip_leading_comments(sql)
        _refuse_reserved_bind_names(sql)
        transaction_control = self._execute_transaction_control(sql)
        if transaction_control is not None:
            return transaction_control
        kill = _KILL_SESSION.match(sql)
        if kill is not None:
            return self._kill_session(kill.group(1))
        if native:
            return self._execute_native(sql, binds)
        refused = self._rowid_number_error(sql, binds)
        if refused is not None:
            raise refused
        sql = _ruled('package-function-call', self._call_package_functions(sql), sql)
        bare = _BARE_CALL.match(sql)
        if bare is not None and bare.group(1).upper() not in _PLSQL_WORD_STATEMENTS:
            _note_rule('bare-call')
            # No binds, so the block would otherwise go to PostgreSQL as it
            # stands; the call path runs it as any other call (#1404).
            return self._execute_plsql(f'BEGIN {bare.group(1)}(); END;', binds)
        single = None if binds else _single_call_block(sql)
        if single is not None and self._routine_exists(single):
            _note_rule('plsql-call')
            # A call with literal arguments and no binds -- sqlplus's
            # `DBMS_OUTPUT.ENABLE(NULL)`, a script's put_line('...'). As a DO block
            # PostgreSQL refuses a bare function call; the call path runs it,
            # its arguments as written (#1531). Only a routine PostgreSQL has:
            # RAISE_APPLICATION_ERROR and the like are the block path's to
            # translate.
            return self._execute_plsql(sql, binds)
        if binds and is_plsql(sql):
            _note_rule('plsql-call')
            return self._collection_outs(self._execute_plsql(sql, binds), binds)
        # A `SELECT REF(alias)` object-REF fetch: PostgreSQL has no REF, so stand in
        # the row's ctid as the locator and report the referenced object type from
        # the typed table's catalog entry, so the client decodes a REF whose
        # type_name matches (#139). The 12c+ REF *bind* the test does next is skipped
        # by its own version guard on the 11g Mirror.
        ref_select = _REF_SELECT.match(sql)
        if ref_select and ref_select.group(1).lower() == ref_select.group(3).lower():
            _note_rule('ref-select')
            return self._execute_ref_select(ref_select)
        # Reject the column types that are Oracle-only for the version the Mirror
        # advertises (JSON/VECTOR/BOOLEAN), so the suite's version guards skip
        # rather than the backend mis-representing them (#504).
        _reject_unsupported_ddl_types(sql)
        # Register / forget an index-organized table, and render ROWID on one from
        # its primary key before the generic rewrite turns ROWID into ctid.
        iot = _iot_primary_key(sql)
        if iot is not None:
            self._iot_pk[iot[0]] = iot[1]
        dropped = _DROP_TABLE_NAME.match(sql)
        if dropped is not None:
            self._iot_pk.pop(_bare_table(dropped.group(1)), None)
        sql = _ruled('iot-rowid', self._rewrite_iot_rowid(sql), sql)
        # INVISIBLE / VISIBLE columns (#1195): the attribute comes out of the DDL
        # and goes to the catalog; a MODIFY that only changes it has nothing left
        # to run. An INSERT with no column list and a `SELECT *` name only the
        # visible columns.
        (sql, visibility) = _column_visibility(sql)
        if visibility is not None:
            _note_rule('invisible-columns')
        if visibility is not None and visibility[3]:
            self._record_visibility(visibility)
            return Result()
        sql = _ruled('invisible-columns', self._expand_visible_columns(sql), sql)
        sql = _ruled('hex-to-raw', self._to_raw_targets(sql), sql)
        # Oracle auto-commits DDL — decide from the original statement, before the
        # dialect rewrite reshapes it (#532).
        is_ddl = _IS_DDL.match(sql) is not None
        original = sql
        # Translate Oracle SQL to PostgreSQL's dialect (#500/#502/#503) — DDL
        # column types, CREATE PROCEDURE/FUNCTION → PL/pgSQL, then the function /
        # literal idioms. This is where dialect knowledge belongs, not in the
        # generic compat shim.
        sql = _ruled('reserved-name', _quote_reserved_names(sql), sql)
        sql = _ruled(
            'package-ddl',
            _translate_package_ddl(
                sql, self._package_index_types, self._user_routine_names
            ),
            sql,
        )
        sql = _ruled('admin', _translate_admin(sql), sql)
        sql = _ruled('ddl', _translate_ddl(sql), sql)
        sql = _ruled(
            'routine-ddl',
            _translate_routine_ddl(
                sql,
                self._user_routine_names() if _ROUTINE_HEAD.match(sql) else None,
            ),
            sql,
        )
        sql = _ruled(
            'plsql-block', _translate_plsql_block(sql, self._routine_kind), sql
        )
        sql = _translate_idioms(sql)
        sql = _ruled(
            'one-element-constructor',
            _spell_one_element_constructors(sql, self._collection_names()),
            sql,
        )
        with_rowid = self._returning_rowid(original, sql)
        if with_rowid is not None:
            sql = with_rowid
        params: dict | None = None
        if binds:
            binds = self._resolve_object_binds(binds)
            sql, params = _translate_binds(sql, binds)
        # Each statement runs inside a SAVEPOINT so a failure rolls back just it
        # (clearing PostgreSQL's aborted-transaction state) and leaves the rest of
        # the transaction intact — Oracle's statement-level error model. The
        # pipelined path ships the SAVEPOINT, the statement and the RELEASE in one
        # network round-trip instead of three; the sequential path is the fallback
        # when libpq is too old for pipeline mode. DDL stays sequential: pipeline
        # mode forces the extended query protocol, which rejects the multi-command
        # `DROP …; CREATE …` a routine DDL rewrites to (#526) — the simple protocol
        # the sequential path uses accepts it. DDL is infrequent and auto-commits,
        # so the hot SELECT/DML path (single-command) is where the round-trips count.
        # A block with local functions is several commands too (#1322).
        hoisted = sql.startswith(_HOISTED_FUNCTION)
        if self._use_pipeline and not is_ddl and not hoisted:
            result = self._execute_pipelined(sql, params, original)
        elif is_ddl:
            try:
                result = self._execute_sequential(
                    sql, params, original, prelude=_DDL_LOCK_TIMEOUT
                )
            except BackendError as error:
                invalid = self._create_invalid(original, error)
                if invalid is None:
                    raise
                result = invalid
        else:
            result = self._execute_sequential(sql, params, original)
        # DDL auto-commits (Oracle semantics): persist it — and any pending DML —
        # so a later rollback discards only DML, not the table (#532).
        if is_ddl:
            self._conn.commit()
            self._user_savepoint = False
            self._record_quoted_names(original)
            self._record_tstz_precisions(original)
            self._record_visibility(visibility)
            self._record_column_types(original)
            self._collection_name_cache = None  # a type may have come or gone
            self._package_function_cache = None  # and a package
            self._plsql_types.clear()
            self._forget_raw_targets()
        if with_rowid is not None:
            # The rows are the rowids of the rows touched, not a result set: a
            # DML still answers with a count, and the last one is its rowid.
            touched = [row[0] for row in result.rows]
            return Result(
                rowcount=len(touched), last_rowid=touched[-1] if touched else None
            )
        if result.columns and not is_ddl:
            self._release_read_locks()
        return result

    def _is_native(self, sql: str) -> bool:
        # Whether a statement is PostgreSQL's own, to run untranslated (#1556).
        return self._sql_dialect == 'postgres' or _PG_HINT.match(sql) is not None

    def _execute_native(self, sql: str, binds: Sequence) -> Result:
        # A native PostgreSQL statement (#1556): none of the Oracle translation,
        # but the Oracle binds mapped, the statement-level savepoint and DDL's
        # autocommit, which the client on the other end still expects.
        is_ddl = _IS_DDL.match(sql) is not None
        params: dict | None = None
        if binds:
            binds = self._resolve_object_binds(binds)
            (sql, params) = _translate_binds(sql, binds)
        if self._use_pipeline and not is_ddl:
            result = self._execute_pipelined(sql, params, sql)
        else:
            result = self._execute_sequential(sql, params, sql)
        if is_ddl:
            self._conn.commit()
            self._user_savepoint = False
            self._collection_name_cache = None
            self._forget_raw_targets()
        elif result.columns:
            self._release_read_locks()
        return result

    def _create_invalid(self, sql: str, error: BackendError) -> Result | None:
        # A CREATE PROCEDURE / FUNCTION / TYPE that does not compile SUCCEEDS in
        # Oracle: the object exists, INVALID, and the reply says so with the
        # compilation warning the client reads as DPY-7000 (#1499). PostgreSQL
        # refuses it outright, so a stand-in of that name takes its place -- a
        # routine that fails when called, as an invalid one does (ORA-06550 in a
        # block, #1497), or a shell type -- and a later CREATE replaces it as it
        # would the invalid object. None for any other failure.
        if error.ora_code not in _COMPILE_ERROR_CODES:
            return None
        package = _PACKAGE_HEAD.match(sql)
        if package is not None:
            return self._create_invalid_package(package)
        routine = _ROUTINE_HEAD.match(sql)
        type_ = _INVALID_TYPE_DDL.match(sql) if routine is None else None
        if routine is not None:
            kind, name = routine.group(1).upper(), routine.group(2)
            returns = ' RETURNS integer' if kind == 'FUNCTION' else ''
            stub = (
                f'DROP {kind} IF EXISTS {name}; CREATE {kind} {name}(){returns} '
                'LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION USING ERRCODE = '
                f"'42883', MESSAGE = 'function {name}() does not exist'; END $$; "
                + _routine_mark(name, 'INVALID')
            )
        elif type_ is not None:
            name = type_.group(1)
            stub = f'DROP TYPE IF EXISTS {name}; CREATE TYPE {name}'
        else:
            return None
        try:
            self._conn.execute(stub)
        except psycopg.Error:
            self._conn.rollback()
            return None
        return Result(compilation_warning=True)

    def _create_invalid_package(self, head: re.Match) -> Result | None:
        # A package spec or body that does not compile (#1605). A body that does
        # not leaves the spec's stand-ins, which now raise ORA-04063; a spec
        # leaves its schema empty, the package INVALID, so a call finds nothing.
        # A body with no spec at all leaves nothing.
        body, owner, name = head.groups()
        package = name.lower()
        if body:
            known = self._conn.execute(
                'SELECT 1 FROM sys.ora_packages WHERE name = %s', (package,)
            ).fetchone()
            if known is None:
                return Result(compilation_warning=True)
            stub = _restore_package_stubs(package, 'INVALID')
        else:
            owner_sql = (
                f"'{owner.upper()}'" if owner else 'sys.ora_owner(current_schema())'
            )
            stub = (
                f'DROP SCHEMA IF EXISTS {package} CASCADE; CREATE SCHEMA {package}; '
                'INSERT INTO sys.ora_packages (name, owner, stubs, spec, body) '
                f"VALUES ('{package}', {owner_sql}, '', 'INVALID', NULL) "
                'ON CONFLICT (name) DO UPDATE SET owner = EXCLUDED.owner, '
                "stubs = '', spec = 'INVALID', body = NULL"
            )
        try:
            self._conn.execute(stub)
        except psycopg.Error:
            self._conn.rollback()
            return None
        return Result(compilation_warning=True)

    def _kill_session(self, session: str) -> Result:
        # ALTER SYSTEM KILL SESSION (#1212): end the backend whose pid is the SID,
        # but only while its serial is the one named, so a pid reused by a later
        # session is not killed in its place. The victim learns of it on its next
        # call, its connection gone, as a killed Oracle session's is. Like any
        # ALTER SYSTEM it leaves the caller's transaction alone.
        ids = _KILL_SESSION_ID.match(session)
        if ids is None:
            raise BackendError(
                'missing or invalid session ID', ora_code=ORA_INVALID_SESSION_ID
            )
        (sid, serial) = (int(ids.group(1)), int(ids.group(2)))
        row = self._conn.execute(
            'SELECT pg_backend_pid() FROM pg_stat_activity '
            'WHERE pid = %s AND sys.ora_serial(pid) = %s',
            (sid, serial),
        ).fetchone()
        if row is None:
            raise BackendError(
                'User session ID does not exist.',
                ora_code=ORA_SESSION_ID_DOES_NOT_EXIST,
            )
        if row[0] == sid:
            raise BackendError(
                'cannot kill current session', ora_code=ORA_CANNOT_KILL_CURRENT_SESSION
            )
        # Wait for the victim's backend to exit before answering, as Oracle's kill
        # does, or the victim's next call could still run on it (#1367).
        self._conn.execute(
            'SELECT pg_terminate_backend(%s, %s)', (sid, _KILL_SESSION_WAIT_MS)
        )
        return Result()

    def _execute_transaction_control(self, sql: str) -> Result | None:
        # COMMIT / ROLLBACK / SAVEPOINT / ROLLBACK TO as SQL text, outside the
        # per-statement savepoint (#1181); None for any other statement.
        end = _TRANSACTION_END.match(sql)
        if end is not None:
            if end.group(1).upper() == 'COMMIT':
                self.commit()
            else:
                self.rollback()
            return Result()
        savepoint = _SAVEPOINT.match(sql)
        if savepoint is not None:
            self._conn.execute(f'SAVEPOINT {savepoint.group(1)}')
            self._user_savepoint = True
            return Result()
        rollback_to = _ROLLBACK_TO.match(sql)
        if rollback_to is None:
            return None
        # Still under `_mirror_stmt`, so an unknown name fails as ORA-01086 and
        # leaves the transaction usable, as Oracle's does; PostgreSQL alone would
        # abort it. On success there is nothing to release: rolling back to the
        # older savepoint has destroyed `_mirror_stmt` already.
        self._conn.execute('SAVEPOINT _mirror_stmt')
        try:
            self._conn.execute(f'ROLLBACK TO SAVEPOINT {rollback_to.group(1)}')
        except psycopg.Error as exc:
            self._conn.execute('ROLLBACK TO SAVEPOINT _mirror_stmt')
            self._conn.execute('RELEASE SAVEPOINT _mirror_stmt')
            raise _backend_error(
                exc, original=sql, arities=self._routine_arities
            ) from exc
        return Result()

    @_while_connected
    @_plainly_quoted
    def execute_returning(self, sql: str, rows: Sequence[Sequence]) -> Result:
        # DML ... RETURNING col INTO :b (#689). PostgreSQL has the feature but
        # spells it without the INTO part, handing the columns back as rows
        # rather than assigning them to binds, so the clause is trimmed to the
        # form it knows and the rows are read.
        #
        # Each iteration goes through `execute` so the whole dialect rewrite,
        # bind translation and per-statement savepoint apply exactly as they do
        # to any other statement. The binds the clause fills carry no value and
        # are dropped: their placeholders are gone from the trimmed text.
        #
        # Each value is converted to the type its receiving bind declared, as
        # Oracle converts it, inside a savepoint of the iteration's own: a value
        # that cannot be converted fails the iteration and undoes it, and only
        # it (#1370).
        statement = strip_returning_into(sql)
        returned: list[list[tuple]] = []
        affected = 0
        # The iteration's savepoint has to outlive `execute`, which ends a
        # transaction that wrote nothing to let its read locks go -- an UPDATE
        # that matched no row, say -- and would take the savepoint with it. It
        # spares one the client took; this one is spared the same way, and the
        # locks are let go once the loop is done.
        client_savepoint = self._user_savepoint
        self._user_savepoint = True
        try:
            for row in rows:
                values = [v for v in row if not isinstance(v, BindVar)]
                declared = [v.tns_type for v in row if isinstance(v, BindVar)]
                self._conn.execute('SAVEPOINT _mirror_returning')
                try:
                    result = self.execute(statement, values)
                    iteration = [
                        tuple(
                            as_declared_type(value, declared[i])
                            if i < len(declared)
                            else value
                            for i, value in enumerate(r)
                        )
                        for r in result.rows
                    ]
                except BackendError as err:
                    self._conn.execute('ROLLBACK TO SAVEPOINT _mirror_returning')
                    self._conn.execute('RELEASE SAVEPOINT _mirror_returning')
                    err.rowcount = affected
                    raise
                self._conn.execute('RELEASE SAVEPOINT _mirror_returning')
                returned.append(iteration)
                # A RETURNING statement gives back one row per row it changed,
                # so the count is the rows read rather than a separate report.
                affected += len(iteration)
        finally:
            self._user_savepoint = client_savepoint
        self._release_read_locks()
        return Result(rowcount=affected, returned_rows=returned)

    def _returning_rowid(self, original: str, translated: str) -> str | None:
        # The translated DML with the rowid RETURNING added, or None where it
        # does not apply: not an INSERT / UPDATE / DELETE, one that returns
        # something already, or one on an index-organized table, whose ROWID is
        # its primary key rather than a heap address.
        if not _DML_HEAD.match(translated) or _HAS_RETURNING.search(translated):
            return None
        table = _STATEMENT_TABLE.search(original)
        if table is not None and _bare_table(table.group(1)) in self._iot_pk:
            return None
        return translated.rstrip().rstrip(';') + _ROWID_RETURNING

    def _rewrite_iot_rowid(self, sql: str) -> str:
        # ROWID on a registered index-organized table → its logical-rowid
        # expression. The statement's table is its first FROM / UPDATE / INTO
        # target; anything else is left for the generic ctid rewrite.
        if not self._iot_pk or _ROWID_WORD.search(sql) is None:
            return sql
        table = _STATEMENT_TABLE.search(sql)
        if table is None:
            return sql
        pk = self._iot_pk.get(_bare_table(table.group(1)))
        if pk is None:
            return sql
        return _ROWID_WORD.sub(_urowid_expression(pk), sql)

    def _forget_raw_targets(self) -> None:
        self._has_raw_targets_cache = None
        self._raw_layout_cache = {}
        self._raw_constructor_cache = None

    def _has_raw_targets(self) -> bool:
        if self._has_raw_targets_cache is None:
            row = self._conn.execute(
                "SELECT EXISTS (SELECT 1 FROM sys.ora_columns WHERE data_type = 'RAW')"
            ).fetchone()
            self._has_raw_targets_cache = bool(row and row[0])
        return self._has_raw_targets_cache

    def _raw_layout(self, name: str) -> tuple[list[str], frozenset[int]] | None:
        # A relation's columns, in order, and which of them are RAW (#1496).
        key = name.lower()
        if key not in self._raw_layout_cache:
            rows = self._conn.execute(
                "SELECT a.attname, coalesce(o.data_type = 'RAW', false) "
                'FROM pg_attribute a LEFT JOIN sys.ora_columns o '
                'ON o.relid = a.attrelid AND o.attnum = a.attnum '
                'WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 '
                f"AND NOT a.attisdropped AND a.attname <> '{_OBJECT_ID_COLUMN}' "
                'ORDER BY a.attnum',
                (name,),
            ).fetchall()
            self._raw_layout_cache[key] = (
                ([r[0] for r in rows], frozenset(i for i, r in enumerate(rows) if r[1]))
                if rows
                else None
            )
        return self._raw_layout_cache[key]

    def _raw_constructors(self) -> dict[str, frozenset[int]]:
        # The object types with a RAW attribute, by name and qualified name,
        # lower case, to the positions of their RAW attributes (#1496).
        if self._raw_constructor_cache is None:
            rows = self._conn.execute(
                'SELECT lower(c.relname), lower(n.nspname), array_agg(x.pos - 1) '
                'FROM (SELECT attrelid, attnum, row_number() OVER '
                '(PARTITION BY attrelid ORDER BY attnum) AS pos FROM pg_attribute '
                'WHERE attnum > 0 AND NOT attisdropped) x '
                'JOIN sys.ora_columns o ON o.relid = x.attrelid '
                "AND o.attnum = x.attnum AND o.data_type = 'RAW' "
                "JOIN pg_class c ON c.oid = x.attrelid AND c.relkind = 'c' "
                'JOIN pg_namespace n ON n.oid = c.relnamespace GROUP BY 1, 2'
            ).fetchall()
            self._raw_constructor_cache = {
                key: frozenset(positions)
                for typname, schema, positions in rows
                for key in (typname, f'{schema}.{typname}')
            }
        return self._raw_constructor_cache

    def _to_raw_targets(self, sql: str) -> str:
        """Wrap each string literal or bind headed for a RAW column or attribute
        in sys.ora_to_raw, which takes a character value as hex, as Oracle
        converts one implicitly (#1496): an INSERT's VALUES, an UPDATE's SET, an
        object constructor's arguments. PostgreSQL's bytea would read the text's
        own bytes instead. A bytes value passes through unchanged."""
        if not self._has_column_catalog or not self._has_raw_targets():
            return sql
        (masked, contents) = _mask_quoted(sql)
        spans: list[tuple[int, int]] = []
        insert = _INSERT_VALUES_HEAD.match(masked)
        if insert is not None:
            layout = self._raw_layout(_unmask_quoted(insert.group(1), contents))
            if layout is not None and layout[1]:
                (names, raw) = layout
                positions: set[int] | frozenset[int] = raw
                if insert.group(2) is not None:
                    listed = [
                        _pg_identifier(_unmask_quoted(c, contents))
                        for c in insert.group(2).split(',')
                    ]
                    positions = {
                        i
                        for i, column in enumerate(listed)
                        if column in names and names.index(column) in raw
                    }
                open_at = insert.end() - 1
                items = _top_level_items(
                    masked, open_at + 1, _matching_paren(masked, open_at)
                )
                spans += [items[i] for i in positions if i < len(items)]
        update = _UPDATE_SET_HEAD.match(masked)
        if update is not None:
            layout = self._raw_layout(_unmask_quoted(update.group(1), contents))
            if layout is not None and layout[1]:
                (names, raw) = layout
                words, _rownums = _top_level_words(masked)
                end = next(
                    (
                        pos
                        for pos, word in words
                        if pos > update.end() and word in ('WHERE', 'RETURNING')
                    ),
                    len(masked.rstrip().rstrip(';')),
                )
                for start, stop in _top_level_items(masked, update.end(), end):
                    equals = masked.find('=', start, stop)
                    if equals < 0:
                        continue
                    column = _pg_identifier(
                        _unmask_quoted(masked[start:equals], contents)
                    )
                    if column in names and names.index(column) in raw:
                        spans.append((equals + 1, stop))
        spans += self._raw_comparison_spans(masked, contents)
        constructors = self._raw_constructors()
        if constructors:
            for call in _CALL_HEAD.finditer(masked):
                positions = constructors.get(call.group(1).lower(), frozenset())
                if not positions:
                    continue
                open_at = call.end() - 1
                items = _top_level_items(
                    masked, open_at + 1, _matching_paren(masked, open_at)
                )
                spans += [items[i] for i in positions if i < len(items)]
        spans = [
            (a, b) for (a, b) in set(spans) if _RAW_TARGET_VALUE.fullmatch(masked[a:b])
        ]
        if not spans:
            return sql
        for a, b in sorted(spans, reverse=True):
            masked = f'{masked[:a]} sys.ora_to_raw({masked[a:b].strip()}){masked[b:]}'
        return _unmask_quoted(masked, contents)

    def _raw_comparison_spans(self, masked: str, contents: list[str]) -> list:
        # The values a single-table statement's WHERE compares with a RAW column
        # of its table, which Oracle reads as hex too (#1496): at the clause's
        # top level, either way round, and an IN list's items. A comparison in a
        # subquery or parentheses is left alone -- its column may be another
        # table's.
        words, _rownums = _top_level_words(masked)
        where = next((p for p, w in words if w == 'WHERE'), None)
        if where is None or not words:
            return []
        head = _UPDATE_SET_HEAD.match(masked) or _DELETE_FROM_HEAD.match(masked)
        if head is None and words[0][1] == 'SELECT':
            head = _SINGLE_TABLE_FROM.search(masked, 0, where + 5)
        if head is None:
            return []
        table = _unmask_quoted(head.group(1), contents)
        alias = head.group(2) if head.re.groups >= 2 else None
        layout = self._raw_layout(table)
        if layout is None or not layout[1]:
            return []
        (names, raw) = layout
        qualifiers = {table.split('.')[-1].strip('"').lower()}
        if alias:
            qualifiers.add(alias.lower())
        end = next(
            (
                p
                for p, w in words
                if p > where and w in ('ORDER', 'GROUP', 'HAVING', 'RETURNING', 'FOR')
            ),
            len(masked.rstrip().rstrip(';')),
        )
        depth = [0] * (len(masked) + 1)
        level = 0
        for i, char in enumerate(masked):
            depth[i] = level
            level += {'(': 1, ')': -1}.get(char, 0)

        def is_raw(qualifier: str | None, column: str) -> bool:
            name = _pg_identifier(column)
            return (
                (qualifier is None or qualifier.lower() in qualifiers)
                and name in names
                and names.index(name) in raw
            )

        spans = []
        for m in _RAW_COMPARISON.finditer(masked, where, end):
            if depth[m.start()] == depth[where] and is_raw(m.group(1), m.group(2)):
                spans.append(m.span(3))
        for m in _RAW_COMPARISON_REVERSED.finditer(masked, where, end):
            if depth[m.start()] == depth[where] and is_raw(m.group(2), m.group(3)):
                spans.append(m.span(1))
        for m in _RAW_IN_LIST.finditer(masked, where, end):
            if depth[m.start()] == depth[where] and is_raw(m.group(1), m.group(2)):
                open_at = m.end() - 1
                spans += _top_level_items(
                    masked, open_at + 1, _matching_paren(masked, open_at)
                )
        return spans

    def _collection_names(self) -> frozenset[str]:
        # The collection types (array domains) of the user schemas, by name and
        # by schema-qualified name, lower case; read once, then after any DDL.
        if self._collection_name_cache is None:
            rows = self._conn.execute(
                'SELECT lower(t.typname), lower(n.nspname) FROM pg_type t '
                'JOIN pg_namespace n ON n.oid = t.typnamespace '
                "JOIN pg_type b ON b.oid = t.typbasetype AND b.typcategory = 'A' "
                "WHERE t.typtype = 'd' "
                "AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'oracle')"
            ).fetchall()
            self._collection_name_cache = frozenset(
                name
                for typname, schema in rows
                for name in (typname, f'{schema}.{typname}')
            )
        return self._collection_name_cache

    def _record_column_types(self, statement: str) -> None:
        # After a committed DDL: keep sys.ora_columns in step with it (#1386).
        # Every DDL prunes the rows of columns that are gone -- a DROP TABLE or
        # DROP COLUMN leaves its own behind otherwise -- and a CREATE TABLE or
        # ALTER TABLE ... ADD / MODIFY records each column it declares a type
        # for, a MODIFY to a type not recorded taking the old record away. On
        # its own, like the other DDL records, so a failure here cannot take the
        # table with it.
        if not self._has_column_catalog:
            return
        self._column_type_cache.clear()
        declared = _declared_columns(statement)
        try:
            self._conn.execute(
                'DELETE FROM sys.ora_columns o WHERE NOT EXISTS '
                '(SELECT 1 FROM pg_attribute a WHERE a.attrelid = o.relid '
                'AND a.attnum = o.attnum AND NOT a.attisdropped)'
            )
            if declared is not None:
                table, columns = declared
                for name, detail in columns.items():
                    self._conn.execute(
                        'DELETE FROM sys.ora_columns o USING pg_attribute a '
                        'WHERE a.attrelid = o.relid AND a.attnum = o.attnum '
                        'AND a.attrelid = to_regclass(%s) AND a.attname = %s',
                        (table, name),
                    )
                    if detail is not None:
                        self._conn.execute(
                            'INSERT INTO sys.ora_columns SELECT attrelid, attnum, '
                            '%s, %s, %s, %s FROM pg_attribute WHERE attrelid = '
                            'to_regclass(%s) AND attname = %s AND attnum > 0',
                            (*detail, table, name),
                        )
            view = _CREATE_VIEW_NAME.match(statement)
            if view is not None:
                self._record_view_column_types(view.group(1))
            self._record_collection_element(statement)
            self._conn.commit()
        except psycopg.Error:
            self._conn.rollback()

    def _record_collection_element(self, statement: str) -> None:
        # A collection of NVARCHAR2 / NCHAR keeps its element's national type
        # (#1437), one of RAW(n) its n (#1545); every DDL prunes the rows of
        # types that are gone.
        if not self._has_collection_elements:
            return
        self._conn.execute(
            'DELETE FROM sys.ora_collection_elements x WHERE NOT EXISTS '
            '(SELECT 1 FROM pg_type t WHERE t.oid = x.typid)'
        )
        recorded = _CREATE_RECORDED_COLLECTION.match(statement)
        if recorded is not None:
            (name, data_type, length) = recorded.groups()
            self._conn.execute(
                'INSERT INTO sys.ora_collection_elements (typid, data_type, data_length) '
                'SELECT to_regtype(%s)::oid, %s, %s WHERE to_regtype(%s) IS NOT NULL '
                'ON CONFLICT (typid) DO UPDATE SET data_type = excluded.data_type, '
                'data_length = excluded.data_length',
                (name, data_type.upper(), int(length) if length else None, name),
            )

    def _recorded_element(self, pg_oid: int) -> tuple[str, int | None] | None:
        # The type a collection's elements were declared as, where it was
        # recorded -- (type, length) -- or None (#1437, #1545).
        if not self._has_collection_elements:
            return None
        row = self._conn.execute(
            'SELECT data_type, data_length FROM sys.ora_collection_elements '
            'WHERE typid = %s',
            (pg_oid,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def _record_view_column_types(self, name: str) -> None:
        # A view's columns take the records of the columns they come from
        # (#1425). A column read through a view traces to the view, not to its
        # table, so the view's own definition is run instead, empty, and each of
        # its columns traced through libpq ftable / ftablecol. A view over a
        # view copies from the inner view's rows.
        row = self._conn.execute(
            'SELECT c.oid, pg_get_viewdef(c.oid) FROM pg_class c '
            "WHERE c.oid = to_regclass(%s) AND c.relkind = 'v'",
            (name,),
        ).fetchone()
        if row is None:
            return
        relid, definition = row
        self._conn.execute('DELETE FROM sys.ora_columns WHERE relid = %s', (relid,))
        pgresult = self._conn.execute(
            f'SELECT * FROM ({definition.strip().rstrip(";")}) q LIMIT 0'
        ).pgresult
        if pgresult is None:
            return
        for index in range(pgresult.nfields):
            source = pgresult.ftable(index)
            if source:
                self._conn.execute(
                    'INSERT INTO sys.ora_columns SELECT %s, %s, data_type, '
                    'data_length, data_precision, data_scale FROM sys.ora_columns '
                    'WHERE relid = %s AND attnum = %s',
                    (relid, index + 1, source, pgresult.ftablecol(index)),
                )

    def _declared_column_types(self, pgresult, indexes: list[int]) -> dict[int, tuple]:
        # The declared type recorded for each result column at `indexes` that
        # comes straight from a table column (#1386), traced through libpq
        # ftable / ftablecol and cached per (relid, attnum).
        keys = {}
        for index in indexes:
            relid = pgresult.ftable(index)
            if relid:
                keys[index] = (relid, pgresult.ftablecol(index))
        unknown = {k for k in keys.values() if k not in self._column_type_cache}
        if unknown and self._has_column_catalog:
            found = self._conn.execute(
                'SELECT relid, attnum, data_type, data_length, data_precision, '
                'data_scale FROM sys.ora_columns WHERE relid = ANY(%s)',
                ([relid for relid, _attnum in unknown],),
            ).fetchall()
            recorded = {(row[0], row[1]): tuple(row[2:]) for row in found}
            for key in unknown:
                self._column_type_cache[key] = recorded.get(key)
        return {
            index: declared
            for index, key in keys.items()
            if (declared := self._column_type_cache.get(key)) is not None
        }

    def _record_visibility(self, visibility: tuple | None) -> None:
        # Keep what a DDL said about its columns' visibility (#1195): add the
        # invisible ones, drop the ones made visible, and prune the rows of
        # dropped tables. Any DDL may change which columns a table has, so the
        # cache of visible columns starts over either way.
        self._any_invisible = None
        self._visible_cache.clear()
        self._not_null_cache.clear()
        self._json_col_cache.clear()
        self._tstz_precision_cache.clear()
        if visibility is None or not self._has_invisible_catalog:
            return
        (table, hidden, shown, _only) = visibility
        try:
            self._conn.execute(
                'DELETE FROM sys.ora_invisible_columns i WHERE NOT EXISTS '
                '(SELECT 1 FROM pg_attribute a WHERE a.attrelid = i.relid '
                'AND a.attnum = i.attnum AND NOT a.attisdropped)'
            )
            if hidden:
                self._conn.execute(
                    'INSERT INTO sys.ora_invisible_columns SELECT attrelid, attnum '
                    'FROM pg_attribute WHERE attrelid = to_regclass(%s) '
                    'AND attname = ANY(%s) AND attnum > 0 ON CONFLICT DO NOTHING',
                    (table, hidden),
                )
            if shown:
                self._conn.execute(
                    'DELETE FROM sys.ora_invisible_columns i USING pg_attribute a '
                    'WHERE a.attrelid = i.relid AND a.attnum = i.attnum '
                    'AND a.attrelid = to_regclass(%s) AND a.attname = ANY(%s)',
                    (table, shown),
                )
            self._conn.commit()
        except psycopg.Error:
            self._conn.rollback()

    def _visible_columns(self, table: str) -> list[str] | None:
        # The visible columns of `table`, quoted, in order -- or None when it has
        # no INVISIBLE column, or is not a table (#1195).
        if not self._has_invisible_catalog:
            return None
        if self._any_invisible is None:
            row = self._conn.execute(
                'SELECT EXISTS (SELECT 1 FROM sys.ora_invisible_columns)'
            ).fetchone()
            self._any_invisible = bool(row and row[0])
        if not self._any_invisible:
            return None
        key = table.lower() if '"' not in table else table
        if key not in self._visible_cache:
            rows = self._conn.execute(
                'SELECT a.attname, h.relid IS NOT NULL FROM pg_attribute a '
                'LEFT JOIN sys.ora_invisible_columns h '
                'ON h.relid = a.attrelid AND h.attnum = a.attnum '
                'WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 '
                'AND NOT a.attisdropped ORDER BY a.attnum',
                (table,),
            ).fetchall()
            self._visible_cache[key] = (
                [
                    '"' + name.replace('"', '""') + '"'
                    for name, hidden in rows
                    if not hidden
                ]
                if any(hidden for _name, hidden in rows)
                else None
            )
        return self._visible_cache[key]

    def _expand_visible_columns(self, sql: str) -> str:
        # Name the visible columns where a statement means "all of them": an
        # INSERT with no column list, and a select list that is only `*` or
        # `alias.*` over one table. A join, a subquery or a view over a table
        # with an INVISIBLE column is not expanded (#1195).
        insert = _INSERT_NO_COLUMNS.match(sql)
        if insert is not None:
            columns = self._visible_columns(insert.group(2))
            if columns:
                at = insert.end()
                return f'{sql[:at].rstrip()} ({", ".join(columns)}) {sql[at:]}'
            return sql
        star = _SELECT_STAR.match(sql)
        if star is not None and '*' in star.group(2):
            select_list = star.group(2)
            if _NESTED_QUERY.search(select_list):
                return sql
            (table, alias) = (star.group(4), star.group(5))
            columns = self._visible_columns(table)
            if not columns:
                return sql
            names = {
                table.lower(),
                (alias or '').lower(),
                table.rsplit('.', 1)[-1].lower(),
            }
            items = []
            for start, end in _top_level_items(select_list, 0, len(select_list)):
                item = select_list[start:end].strip()
                if item == '*':
                    items.append(', '.join(columns))
                elif item.endswith('.*') and item[:-2].lower() in names:
                    items.append(', '.join(f'{item[:-2]}.{c}' for c in columns))
                else:
                    items.append(item)
            return (
                f'{star.group(1)}{", ".join(items)}{star.group(3)}'
                + sql[star.start(4) :]
            )
        return sql

    def _record_quoted_names(self, statement: str) -> None:
        # After a committed CREATE TABLE: note which of its columns were created
        # with a quoted all-lower-case name (#1204). Done after the commit and on
        # its own, so a failure here cannot take the table with it. A statement
        # without such a name costs nothing. A column added later by ALTER TABLE
        # ... ADD is not recorded, and reports its name by the folding rule.
        table = _CREATE_TABLE_NAME.match(statement)
        if table is None or not self._has_quoted_names:
            return
        names = [
            name
            for name in _QUOTED_LOWER_NAME.findall(statement)
            if name.upper() not in _ORACLE_RESERVED_WORDS
        ]
        if not names:
            return
        try:
            self._conn.execute(
                'DELETE FROM sys.ora_quoted_names q WHERE NOT EXISTS '
                '(SELECT 1 FROM pg_class c WHERE c.oid = q.relid)'
            )
            self._conn.execute(
                'INSERT INTO sys.ora_quoted_names SELECT attrelid, attnum '
                'FROM pg_attribute WHERE attrelid = to_regclass(%s) '
                'AND attname = ANY(%s) AND attnum > 0 ON CONFLICT DO NOTHING',
                (table.group(1), names),
            )
            self._conn.commit()
        except psycopg.Error:
            self._conn.rollback()

    def _record_tstz_precisions(self, statement: str) -> None:
        # After a committed CREATE TABLE: note the precision of each column it
        # declares TIMESTAMP(n) WITH TIME ZONE (#1308), and of each attribute a
        # CREATE TYPE ... AS OBJECT does, by the composite's relation (#1550).
        # On its own, like the quoted names, so a failure here cannot take the
        # table with it; a column added later by ALTER TABLE ... ADD keeps the
        # default.
        table = _CREATE_TABLE_NAME.match(statement) or _CREATE_OBJECT_TYPE_NAME.match(
            statement
        )
        if table is None or not self._has_tstz_precision:
            return
        declared = {
            (name[1:-1] if name.startswith('"') else name.lower()): int(prec)
            for name, prec in _TSTZ_PRECISION_COLUMN.findall(statement)
        }
        if not declared:
            return
        try:
            self._conn.execute(
                'DELETE FROM sys.ora_tstz_precision p WHERE NOT EXISTS '
                '(SELECT 1 FROM pg_class c WHERE c.oid = p.relid)'
            )
            for name, prec in declared.items():
                self._conn.execute(
                    'INSERT INTO sys.ora_tstz_precision SELECT attrelid, attnum, %s '
                    'FROM pg_attribute WHERE attrelid = to_regclass(%s) '
                    'AND attname = %s AND attnum > 0 '
                    'ON CONFLICT (relid, attnum) DO UPDATE SET prec = EXCLUDED.prec',
                    (prec, table.group(1), name),
                )
            self._conn.commit()
        except psycopg.Error:
            self._conn.rollback()

    def _tstz_precisions(self, pgresult, indexes: list[int]) -> dict[int, int]:
        # The recorded precision of each WITH TIME ZONE result column at
        # `indexes` that comes straight from a table column (#1308), traced
        # through libpq ftable / ftablecol and cached per (relid, attnum).
        keys = {}
        for index in indexes:
            relid = pgresult.ftable(index)
            if relid:
                keys[index] = (relid, pgresult.ftablecol(index))
        unknown = {k for k in keys.values() if k not in self._tstz_precision_cache}
        if unknown and self._has_tstz_precision:
            found = self._conn.execute(
                'SELECT relid, attnum, prec FROM sys.ora_tstz_precision '
                'WHERE relid = ANY(%s)',
                ([relid for relid, _attnum in unknown],),
            ).fetchall()
            recorded = {(relid, attnum): prec for relid, attnum, prec in found}
            for key in unknown:
                self._tstz_precision_cache[key] = recorded.get(key)
        return {
            index: prec
            for index, key in keys.items()
            if (prec := self._tstz_precision_cache.get(key)) is not None
        }

    def _quoted_lower_columns(self, pgresult, indexes: list[int]) -> set[int]:
        # Which of the result columns at `indexes` -- each one whose name the
        # folding rule would upper-case -- were created with a quoted
        # all-lower-case name, so Oracle reports them as written (#1204). Traced
        # through libpq ftable / ftablecol to the table column, cached per
        # (relid, attnum); a computed column has no table and keeps the rule.
        keys = {}
        for index in indexes:
            relid = pgresult.ftable(index)
            if relid:
                keys[index] = (relid, pgresult.ftablecol(index))
        unknown = {key for key in keys.values() if key not in self._quoted_col_cache}
        if unknown:
            found = self._conn.execute(
                'SELECT relid, attnum FROM sys.ora_quoted_names WHERE relid = ANY(%s)',
                ([relid for relid, _attnum in unknown],),
            ).fetchall()
            recorded = {(relid, attnum) for relid, attnum in found}
            for key in unknown:
                self._quoted_col_cache[key] = key in recorded
        return {index for index, key in keys.items() if self._quoted_col_cache[key]}

    def _not_null_columns(self, pgresult, count: int) -> set[int]:
        # Which result columns come straight from a NOT NULL table column, which
        # Oracle describes as not nullable (#1307). Traced through libpq
        # ftable / ftablecol, cached per (relid, attnum); a computed column has
        # no table and stays nullable, as in Oracle.
        keys = {}
        for index in range(count):
            relid = pgresult.ftable(index)
            if relid:
                keys[index] = (relid, pgresult.ftablecol(index))
        unknown = {key for key in keys.values() if key not in self._not_null_cache}
        if unknown:
            found = self._conn.execute(
                'SELECT attrelid, attnum FROM pg_attribute '
                'WHERE attrelid = ANY(%s) AND attnum > 0 AND attnotnull',
                ([relid for relid, _attnum in unknown],),
            ).fetchall()
            not_null = {(relid, attnum) for relid, attnum in found}
            for key in unknown:
                self._not_null_cache[key] = key in not_null
        return {index for index, key in keys.items() if self._not_null_cache[key]}

    def _json_columns(self, pgresult, count: int) -> set[int]:
        # Which result columns come straight from a table column with an IS
        # JSON check constraint (#1626), which Oracle describes as JSON, so a
        # client hands back the parsed value. The constraint is stored as
        # sys.ora_is_json(column) (#1614); one with NOT before it is no such
        # column. Traced and cached as _not_null_columns is.
        keys = {}
        for index in range(count):
            relid = pgresult.ftable(index)
            if relid:
                keys[index] = (relid, pgresult.ftablecol(index))
        unknown = {key for key in keys.values() if key not in self._json_col_cache}
        if unknown:
            found = self._conn.execute(
                'SELECT conrelid, conkey[1] FROM pg_constraint '
                "WHERE contype = 'c' AND conrelid = ANY(%s) "
                'AND cardinality(conkey) = 1 '
                "AND pg_get_constraintdef(oid) ~ '^CHECK \\(+(sys\\.)?ora_is_json\\('",
                ([relid for relid, _attnum in unknown],),
            ).fetchall()
            json = {(relid, attnum) for relid, attnum in found}
            for key in unknown:
                self._json_col_cache[key] = key in json
        return {index for index, key in keys.items() if self._json_col_cache[key]}

    def _build_result(self, cursor, sql: str = '', original: str = '') -> Result:
        # Turn an executed statement's cursor into a Result: a row count for a
        # no-row statement, else the fetched rows plus a ColumnMeta per column.
        # `sql` is the statement as run, read for the select-list items whose
        # type only the item says (a LOB call, an IntervalYM bind).
        if cursor.description is None:
            return Result(rowcount=max(cursor.rowcount, 0))
        computed = _computed_column_types(sql) if sql else {}
        constructors = _constructor_items(sql) if sql else {}
        rows = [list(r) for r in cursor.fetchall()]
        # Re-tag the zoned cells before they reach the wire encoder (_wire_cell).
        for i, desc in enumerate(cursor.description):
            if desc.type_code in (self._tstz_oid, _TIMESTAMPTZ_OID):
                for row in rows:
                    row[i] = _wire_cell(row[i], desc.type_code, self._tstz_oid)
        columns = []
        for i, desc in enumerate(cursor.description):
            # A column tracing back to a typed domain is that Oracle type — an
            # ora_clob / ora_blob LOB (so an empty value stays '' / b'' rather than
            # collapsing to NULL, #534), or an ora_intervalym INTERVAL YEAR TO MONTH
            # (so its months survive, #504), or an ora_date DATE (#1316). Only a
            # text / bytea / interval / timestamp column can be one, so cheaper
            # types skip the catalog lookup.
            domain = (
                self._domain_type(cursor.pgresult, i)
                if desc.type_code in _DOMAIN_BASE_OIDS
                else None
            )
            if domain is None and desc.type_code in (25, 17, _INTERVAL_OID):
                domain = computed.get(i)
            if domain in (TNS_TYPE_CLOB, TNS_TYPE_BLOB):
                columns.append(_lob_column_meta(desc.name, domain))
            elif domain == TNS_TYPE_INTERVALYM:
                for row in rows:
                    row[i] = _to_interval_ym(row[i])
                columns.append(_intervalym_column_meta(desc.name))
            elif domain == TNS_TYPE_DATE:
                columns.append(_date_column_meta(desc.name))
            elif (
                desc.type_code not in _BUILTIN_OIDS
                and (target := self._ref_target(desc.type_code)) is not None
            ):
                for row in rows:
                    row[i] = self._ref_cell(target, row[i])
                columns.append(self._ref_column_meta(desc.name, target))
            elif (
                coll := self._column_collection_type(
                    cursor.pgresult, i, desc.type_code, constructors.get(i)
                )
            ) is not None:
                for row in rows:
                    row[i] = self._db_collection(coll, row[i], desc.type_code)
                columns.append(_object_column_meta(desc.name, coll))
            elif self._bfile_oid is not None and desc.type_code == self._bfile_oid:
                # A BFILE (#1669): the two names, charset and form 0, as the
                # passthrough describes one; its cells the core's BFile.
                for row in rows:
                    if row[i] is not None:
                        row[i] = BFile(row[i][0], row[i][1])
                columns.append(
                    ColumnMeta(
                        name=_oracle_column_name(desc.name).encode('utf-8'),
                        data_type=TNS_TYPE_BFILE,
                        data_length=0,
                        max_size=0,
                        charset=0,
                        csfrm=0,
                    )
                )
            elif (entry := self._column_object_type(desc.type_code)) is not None:
                typ, info = entry
                for row in rows:
                    row[i] = self._db_object(typ, info, row[i])
                columns.append(_object_column_meta(desc.name, typ))
            elif desc.type_code == _REFCURSOR_OID:
                # A CURSOR(...) item (#1461): each row's portal, drained.
                for row in rows:
                    if row[i] is not None:
                        row[i] = self._drain_refcursor(row[i])
                columns.append(_refcursor_column_meta(desc.name))
            else:
                columns.append(_column_meta(desc, [r[i] for r in rows], self._tstz_oid))
        if self._has_quoted_names:
            folded = [
                i
                for i, desc in enumerate(cursor.description)
                if _oracle_column_name(desc.name) != desc.name
            ]
            for i in self._quoted_lower_columns(cursor.pgresult, folded):
                name = cursor.description[i].name.encode('utf-8')
                columns[i] = replace(columns[i], name=name)
        for i in self._not_null_columns(cursor.pgresult, len(columns)):
            columns[i] = replace(columns[i], null_ok=0)
        for i in self._json_columns(cursor.pgresult, len(columns)):
            columns[i] = replace(columns[i], is_json=True)
        # A RAW(n) column describes as its declared n, which bytea does not keep;
        # the values' widest stood in for it (#1386). A LONG / LONG RAW column,
        # stored as text / bytea, describes as itself, unsized (#1382). An
        # NVARCHAR2 column, a varchar, describes in the national form (#1383).
        plain = [
            i
            for i, col in enumerate(columns)
            if col.data_type in (TNS_TYPE_RAW, TNS_TYPE_VARCHAR, TNS_TYPE_CHAR)
        ]
        if plain:
            for i, (declared, length, _p, _s) in self._declared_column_types(
                cursor.pgresult, plain
            ).items():
                if declared in _LONG_TYPES:
                    columns[i] = replace(
                        columns[i],
                        data_type=_LONG_TYPES[declared],
                        data_length=0,
                        max_size=0,
                    )
                elif declared == 'RAW' and length:
                    columns[i] = replace(
                        columns[i], data_length=length, max_size=length
                    )
                elif declared == _NATIONAL_OF.get(columns[i].data_type) and length:
                    # National: n characters of AL16UTF16, 2n bytes (#1383,
                    # #1416).
                    columns[i] = replace(
                        columns[i],
                        csfrm=_CSFRM_NATIONAL,
                        charset=AL16UTF16_CHARSET,
                        data_length=length,
                        max_size=length // 2,
                    )
        # An unaliased computed item is named by its own Oracle text, as Oracle
        # names it, where PostgreSQL gave `length` or `?column?` (#1449).
        if original:
            named = _expression_names(original)
            if named and len(named) <= len(columns):
                for i, name in named.items():
                    if i < len(columns):
                        columns[i] = replace(columns[i], name=name.encode('utf-8'))
        # A table's unconstrained NUMBER column -- a numeric with no typmod --
        # describes with precision 0 and scale -127, as Oracle's does; a computed
        # number has neither, as on a live server, so only a column that traces
        # to a table counts (#1421). So does a constant item, which Oracle folds
        # -- 1, 1 + 1, abs(-1) -- where one computed from a column does not
        # (#1444). A FLOAT record below overrides it.
        constants = _constant_items(original) if original else set()
        for i, desc in enumerate(cursor.description):
            if columns[i].data_type == TNS_TYPE_NUMBER and (
                (
                    desc.type_code == _NUMERIC_OID
                    and cursor.pgresult.ftable(i)
                    and cursor.pgresult.fmod(i) == -1
                )
                or (i in constants and not columns[i].precision)
            ):
                columns[i] = replace(columns[i], precision=0, scale=-127)
        # An INTERVAL column describes with its declared precisions, which
        # PostgreSQL's interval does not keep (#1381). A FLOAT(b) column, a
        # numeric here, describes as a NUMBER of precision b and scale -127,
        # Oracle's marker for FLOAT (#1384).
        interval = [
            i
            for i, col in enumerate(columns)
            if col.data_type
            in (TNS_TYPE_INTERVALDS, TNS_TYPE_INTERVALYM, TNS_TYPE_NUMBER)
        ]
        if interval:
            for i, (declared, _length, precision, scale) in self._declared_column_types(
                cursor.pgresult, interval
            ).items():
                if declared == 'FLOAT':
                    columns[i] = replace(columns[i], precision=precision, scale=-127)
                elif precision is not None:
                    columns[i] = replace(
                        columns[i], precision=precision, scale=scale or 0
                    )
        # An NCLOB column, an ora_clob like a CLOB, describes in the national
        # character set form (#1369).
        clob = [i for i, col in enumerate(columns) if col.data_type == TNS_TYPE_CLOB]
        if clob:
            for i, (declared, _length, _p, _s) in self._declared_column_types(
                cursor.pgresult, clob
            ).items():
                if declared == 'NCLOB':
                    columns[i] = replace(
                        columns[i], csfrm=_CSFRM_NATIONAL, charset=AL16UTF16_CHARSET
                    )
        # A CAST to NVARCHAR2(n) / NCHAR(n) in the select list describes national,
        # n characters and 2n bytes, which its varchar(n) / char(n) does not say
        # (#1440).
        if original:
            for i, length in _computed_national_columns(original).items():
                if i < len(columns) and columns[i].data_type in _NATIONAL_OF:
                    columns[i] = replace(
                        columns[i],
                        csfrm=_CSFRM_NATIONAL,
                        charset=AL16UTF16_CHARSET,
                        data_length=2 * length,
                        max_size=length,
                    )
        tstz = [
            i for i, col in enumerate(columns) if col.data_type == TNS_TYPE_TIMESTAMPTZ
        ]
        if tstz:
            for i, prec in self._tstz_precisions(cursor.pgresult, tstz).items():
                columns[i] = replace(columns[i], scale=prec)
        return Result(columns=columns, rows=[tuple(r) for r in rows])

    def _column_object_type(
        self, pg_oid: int
    ) -> tuple[DbObjectType, CompositeInfo] | None:
        # Only an oid no built-in mapping claims can be an object type; the
        # ora_tstz composite is TIMESTAMP WITH TIME ZONE, not an object.
        if pg_oid in _BUILTIN_OIDS or pg_oid == self._tstz_oid:
            return None
        return self._object_type(pg_oid)

    def _column_collection_type(
        self, pgresult, index: int, pg_oid: int, constructor: str | None = None
    ) -> DbObjectType | None:
        # A VARRAY / nested-table column (#1206). PostgreSQL describes a domain
        # column by its base type, an array here, so an array column is traced
        # through libpq ftable / ftablecol to the type its table declares. A
        # computed array has no table column; when its select item is one call
        # to a collection type's constructor, that type is the one (#1473).
        # Any other computed array stays a plain array.
        if pg_oid not in self._array_oids:
            row = self._conn.execute(
                "SELECT typcategory = 'A' FROM pg_type WHERE oid = %s", (pg_oid,)
            ).fetchone()
            self._array_oids[pg_oid] = bool(row and row[0])
        if not self._array_oids[pg_oid]:
            return None
        relid = pgresult.ftable(index)
        if not relid:
            return (
                self._constructed_collection_type(constructor) if constructor else None
            )
        key = (relid, pgresult.ftablecol(index))
        if key not in self._column_types:
            row = self._conn.execute(
                'SELECT atttypid FROM pg_attribute WHERE attrelid = %s AND attnum = %s',
                key,
            ).fetchone()
            self._column_types[key] = row[0] if row else 0
        return self._collection_type(self._column_types[key])

    def _constructed_collection_type(self, name: str) -> DbObjectType | None:
        # The collection type a constructor call names, or None when the name
        # is not a type -- an ordinary function returning an array (#1473).
        # Looked up each time: a type dropped and created again under the same
        # name has a new oid.
        row = self._conn.execute('SELECT to_regtype(%s)::oid', (name,)).fetchone()
        return self._collection_type(row[0]) if row and row[0] else None

    def _collection_type(self, pg_oid: int) -> DbObjectType | None:
        """The Oracle collection type an array domain stands for (#1206).

        Built the way ``all_types`` / ``all_coll_types`` build what a client
        reads, so the two agree on the OID, the kind and the element. None when
        the oid is not a collection type.
        """
        if pg_oid in self._collection_types:
            return self._collection_types[pg_oid]
        kind = self._type_kind(pg_oid)
        typ = None
        if kind is not None and kind[0] == 'collection':
            (_kind, owner, name, element, _typmod) = kind
            bound = self._conn.execute(
                "SELECT substring(pg_get_constraintdef(oid) FROM '<=\\s*([0-9]+)')::int "
                'FROM pg_constraint WHERE contypid = %s',
                (pg_oid,),
            ).fetchone()
            upper = bound[0] if bound else None
            typ = DbObjectType(
                owner,
                name,
                _object_type_oid(pg_oid),
                1,
                [],
                is_collection=True,
                collection_type=(
                    COLLECTION_VARRAY if upper else COLLECTION_NESTED_TABLE
                ),
                element=self._collection_element(element),
                max_elements=upper or 0,
            )
            recorded = self._recorded_element(pg_oid)
            if recorded is not None and typ.element is not None:
                # A client sends and expects an NVARCHAR2 / NCHAR element in
                # AL16UTF16 inside the image, as an attribute (#1437); a RAW(n)
                # element as RAW bytes, not the BLOB a bytea reads as (#1545).
                declared = recorded[0]
                typ.element = {
                    **typ.element,
                    'type_name': declared,
                    'data_type': type_name_to_tns(declared),
                    'charset': AL16UTF16_CHARSET if declared != 'RAW' else None,
                }
        self._collection_types[pg_oid] = typ
        return typ

    def _collection_element(self, pg_oid: int) -> dict:
        # The element layout a collection's image is packed against: an object
        # element carries its own type, as a client's describe embeds it.
        if pg_oid == _XML_OID:
            # A collection of XMLType (#1537).
            return {
                'name': 'element',
                'type_name': 'XMLTYPE',
                'data_type': None,
                'charset': None,
                'object_type': _XMLTYPE_DBTYPE,
            }
        entry = self._object_type(pg_oid)
        if entry is not None:
            typ = entry[0]
            return {
                'name': 'element',
                'type_name': typ.name,
                'data_type': type_name_to_tns(typ.name),
                'charset': None,
                'object_type': typ,
            }
        inner = self._collection_type(pg_oid)
        if inner is not None:
            # A collection of collections (#1276): psycopg does not know the inner
            # domain, so a value came back as text; load the domain as its base
            # array, and arrays of it as lists of those.
            self._register_collection_loaders(pg_oid)
            return {
                'name': 'element',
                'type_name': inner.name,
                'data_type': type_name_to_tns(inner.name),
                'charset': None,
                'object_type': inner,
            }
        row = self._conn.execute(
            f"SELECT CASE WHEN typname = '{_CLOB_TYPE}' THEN 'CLOB' "
            f"WHEN typname = '{_BLOB_TYPE}' THEN 'BLOB' "
            'ELSE sys.ora_type_name(format_type(oid, NULL)) END '
            'FROM pg_type WHERE oid = %s',
            (pg_oid,),
        ).fetchone()
        type_name = row[0] if row else None
        return {
            'name': 'element',
            'type_name': type_name,
            'data_type': type_name_to_tns(type_name),
            'charset': None,
        }

    def _register_collection_loaders(self, pg_oid: int) -> None:
        # An array domain inside another array loads as its base array does, and
        # an array of it as a list of those (#1276).
        from psycopg.types import TypeInfo
        from psycopg.types.array import register_array

        row = self._conn.execute(
            'SELECT typbasetype, %s::oid::regtype::text FROM pg_type WHERE oid = %s',
            (pg_oid, pg_oid),
        ).fetchone()
        if row is None:
            return
        (base, regtype) = row
        for fmt in (psycopg.pq.Format.TEXT, psycopg.pq.Format.BINARY):
            loader = self._conn.adapters.get_loader(base, fmt)
            if loader is not None:
                self._conn.adapters.register_loader(pg_oid, loader)
        domain = TypeInfo.fetch(self._conn, regtype)
        if domain is not None:
            register_array(domain, self._conn)

    def _db_collection(
        self, typ: DbObjectType, value: object, array_oid: int
    ) -> object:
        # One array cell as the collection DbObject the Mirror encodes; an
        # object element is built as an object column's value is. A cursor that
        # executed before the element's composite was registered hands the text
        # form `{"(1,a)"}`; the array loader registered with it parses that.
        if value is None:
            return None
        if isinstance(value, str):
            loader = self._conn.adapters.get_loader(array_oid, psycopg.pq.Format.TEXT)
            if loader is None:
                raise UnsupportedFeature(f'collection type {typ.name}: no loader')
            value = loader(array_oid, self._conn).load(value.encode('utf-8'))
        if not isinstance(value, list):
            raise UnsupportedFeature(f'collection type {typ.name}: not an array value')
        nested = (typ.element or {}).get('object_type')
        if nested is not None and nested.is_collection:
            # A collection of collections (#1276): each element its own
            # collection, NULL staying NULL.
            inner_oid = _pg_oid_of(nested.oid) or 0
            base = self._conn.execute(
                'SELECT typbasetype FROM pg_type WHERE oid = %s', (inner_oid,)
            ).fetchone()
            value = [
                self._db_collection(nested, v, base[0] if base else 0) for v in value
            ]
        elif nested is not None and nested is not _XMLTYPE_DBTYPE:
            # (An XMLType element is its document's text, as it comes, #1537.)
            entry = self._object_type(_pg_oid_of(nested.oid) or 0)
            if entry is None:
                raise UnsupportedFeature(f'collection type {typ.name}: no element type')
            value = [self._db_object(entry[0], entry[1], v) for v in value]
        return typ.newobject(value)

    def describe_type(self, name: str) -> TypeDescription | None:
        """The object or collection type ``name`` names, for an OCI client's
        type describe (#1411): looked for in the session's own schemas, then in
        SYS, as a public synonym would find it there. None if there is none."""
        (schema, _dot, bare) = name.rpartition('.')
        row = self._conn.execute(
            'SELECT t.oid FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace '
            'WHERE sys.ora_name(t.typname) = %s '
            'AND (CASE WHEN %s <> %s THEN sys.ora_owner(n.nspname) = %s '
            "ELSE n.nspname = ANY(current_schemas(false)) OR n.nspname = 'sys' END) "
            # One the session may use (#1621).
            'AND sys.ora_type_visible(sys.ora_owner(n.nspname), sys.ora_name(t.typname)) '
            "ORDER BY n.nspname = 'sys' LIMIT 1",
            (bare, schema, '', schema),
        ).fetchone()
        if row is None:
            return None
        kind = self._type_kind(row[0])
        if kind is None:
            return None
        shape = self._type_shape(row[0])
        if isinstance(shape, _TdsCollection):
            description = 'varray' if shape.varray else 'nested_table'
            null_tds = _tds(_TdsObject((_TDS_NULL_LEAF,)))
        else:
            description = 'object'
            null_tds = _tds_null(shape)
        return TypeDescription(
            schema=kind[1],
            name=kind[2],
            oid=_object_type_oid(row[0]),
            kind=description,
            tds=_tds(shape),
            null_tds=null_tds,
        )

    def _type_kind(self, pg_oid: int) -> tuple[str, str, str, int, int] | None:
        # (kind, owner, name, element oid, element typmod) of a named type: kind
        # 'object' for a standalone or table row composite, 'collection' for an
        # array domain (its element and bound), else None.
        row = self._conn.execute(
            'SELECT t.typtype, sys.ora_owner(n.nspname), sys.ora_name(t.typname), '
            'b.typcategory, b.typelem, t.typtypmod, c.relkind '
            'FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace '
            'LEFT JOIN pg_type b ON b.oid = t.typbasetype '
            'LEFT JOIN pg_class c ON c.oid = t.typrelid WHERE t.oid = %s',
            (pg_oid,),
        ).fetchone()
        if row is None:
            return None
        (typtype, owner, name, base_category, element, typmod, relkind) = row
        if typtype == 'c' and relkind in ('c', 'r') and pg_oid != self._tstz_oid:
            return ('object', owner, name, 0, -1)
        if typtype == 'd' and base_category == 'A':
            return ('collection', owner, name, element, typmod)
        return None

    def _pg_type_name(self, pg_oid: int) -> str:
        row = self._conn.execute(
            'SELECT typname FROM pg_type WHERE oid = %s', (pg_oid,)
        ).fetchone()
        if row is None:
            raise UnsupportedFeature(f'type shape: no type has the oid {pg_oid}')
        return row[0]

    def _package_index_types(self, package: str) -> dict[str, str]:
        # The index-by table types a package's spec declared (#1607): name
        # (lower case) -> the PostgreSQL type, for its body's translation.
        if not self._has_package_catalog:
            return {}
        return {
            name.lower(): f'{package}.{name.lower()}'
            for (name,) in self._conn.execute(
                'SELECT name FROM sys.ora_plsql_types WHERE package = %s '
                "AND meta->>'coll_type' = 'PL/SQL INDEX TABLE'",
                (package,),
            ).fetchall()
        }

    def _plsql_type(self, pg_oid: int) -> tuple[str, str, str, dict] | None:
        """A package's type behind a PostgreSQL type (#1607): its owner,
        package, name and what its declaration says; None for any other type."""
        if pg_oid in self._plsql_types:
            return self._plsql_types[pg_oid]
        row = (
            self._conn.execute(
                'SELECT k.owner, upper(p.package), p.name, p.meta '
                'FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace '
                'JOIN sys.ora_plsql_types p ON p.package = n.nspname '
                'AND lower(p.name) = t.typname '
                'JOIN sys.ora_packages k ON k.name = p.package WHERE t.oid = %s',
                (pg_oid,),
            ).fetchone()
            if self._has_package_catalog
            else None
        )
        found = (row[0], row[1], row[2], row[3] or {}) if row is not None else None
        self._plsql_types[pg_oid] = found
        return found

    def _plsql_named_oid(self, owner: str, ref: dict) -> int | None:
        # The PostgreSQL type a package type's named field or element is: a
        # package's type, a schema's, a table's row (#1607).
        name = str(ref['name'])
        if name.endswith('%ROWTYPE'):
            row = self._conn.execute(
                'SELECT t.oid FROM pg_type t JOIN pg_class c ON c.oid = t.typrelid '
                "WHERE c.relkind = 'r' AND sys.ora_name(c.relname) = %s "
                'ORDER BY c.relnamespace = to_regnamespace(current_schema()) DESC '
                'LIMIT 1',
                (name[: -len('%ROWTYPE')],),
            ).fetchone()
        elif ref.get('package'):
            row = self._conn.execute(
                'SELECT to_regtype(%s)::oid',
                (f'{str(ref["package"]).lower()}.{name.lower()}',),
            ).fetchone()
        else:
            row = self._conn.execute(
                'SELECT t.oid FROM pg_type t JOIN pg_namespace n '
                'ON n.oid = t.typnamespace WHERE sys.ora_name(t.typname) = %s '
                'AND sys.ora_owner(n.nspname) = %s LIMIT 1',
                (name, owner),
            ).fetchone()
        return row[0] if row is not None and row[0] is not None else None

    def _plsql_part_shape(self, owner: str, ref: dict):
        # The TDS shape of a package type's field or element (#1607).
        if ref.get('named'):
            pg_oid = self._plsql_named_oid(owner, ref)
            if pg_oid is None:
                raise UnsupportedFeature(f'type shape: no type {ref["name"]}')
            return self._type_shape(pg_oid)
        name = str(ref['name'])
        if name in ('BOOLEAN', 'PL/SQL PLS INTEGER', 'PL/SQL BINARY INTEGER'):
            return _TdsLeaf(b'\x08')  # one code for all three, as 23ai sends them
        if name in ('VARCHAR2', 'CHAR', 'NVARCHAR2', 'NCHAR'):
            national = name.startswith('N')
            length = int(ref.get('length') or 1)
            code = 0x07 if 'VARCHAR' in name else 0x01
            return _tds_chars(code, 2 * length if national else length, national)
        if name == 'RAW':
            return _TdsLeaf(b'\x13' + int(ref.get('length') or 1).to_bytes(2, 'big'))
        if name in ('NUMBER', 'INTEGER', 'SMALLINT'):
            if name != 'NUMBER':
                return _tds_number(0, 0)
            precision = ref.get('precision')
            scale = ref.get('scale')
            return _tds_number(
                precision or 0, scale if scale is not None else 0 if precision else -127
            )
        if name in _FLOAT_ATTRIBUTE_TYPES:
            return _TdsLeaf(bytes([0x05, int(ref.get('precision') or 0)]))
        leaves = {
            'DATE': _TdsLeaf(b'\x02'),
            'CLOB': _TdsLeaf(b'\x1d'),
            'NCLOB': _TdsLeaf(b'\x1d'),
            'BLOB': _TdsLeaf(b'\x1e'),
            'BINARY_FLOAT': _TdsLeaf(b'\x25', newer=True),
            'BINARY_DOUBLE': _TdsLeaf(b'\x2d', newer=True),
        }
        if name in leaves:
            return leaves[name]
        fraction = ref.get('scale')
        digits = int(fraction) if fraction is not None else 6
        timestamp_code = {
            'TIMESTAMP': 0x15,
            'TIMESTAMP WITH TZ': 0x17,
            'TIMESTAMP WITH LOCAL TZ': 0x21,
        }.get(name)
        if timestamp_code is None:
            raise UnsupportedFeature(f'type shape: {name} has no TDS leaf')
        return _tds_timestamp(timestamp_code, digits)

    def _plsql_shape(self, owner: str, meta: dict):
        # A package type's TDS shape (#1607): a record an object of its fields,
        # a collection of its element, an index-by table kind 1.
        if meta.get('typecode') == 'PL/SQL RECORD':
            return _TdsObject(
                tuple(self._plsql_part_shape(owner, f['type']) for f in meta['attrs'])
            )
        coll_type = meta.get('coll_type')
        return _TdsCollection(
            varray=coll_type == 'VARYING ARRAY',
            bound=int(meta.get('upper_bound') or 0),
            element=self._plsql_part_shape(owner, meta['elem']),
            index_table=coll_type == 'PL/SQL INDEX TABLE',
        )

    def _plsql_attribute_rows(self, owner: str, meta: dict) -> list[tuple]:
        # The attribute cursor GET_TYPE_SHAPE returns for a package type, as
        # 23ai sends it (#1607): a record's fields, INSTANTIABLE YES; a
        # collection's element alone, unnamed, INSTANTIABLE NULL.
        def row(name, position, ref, instantiable) -> tuple:
            if ref.get('named'):
                pg_oid = self._plsql_named_oid(owner, ref)
                toid = _object_type_oid(pg_oid) if pg_oid is not None else None
                return (
                    1, name, position, ref['name'], owner, ref.get('package'),
                    toid, instantiable, None, None,
                )  # fmt: skip
            toid = bytes(15) + bytes([_BUILTIN_TYPE_OID_BYTE.get(ref['name'], 0)])
            return (
                1,
                name,
                position,
                ref['name'],
                None,
                None,
                toid,
                instantiable,
                None,
                None,
            )

        if meta.get('typecode') == 'PL/SQL RECORD':
            return [
                row(f['name'], position, f['type'], 'YES')
                for position, f in enumerate(meta['attrs'], 1)
            ]
        return [row(None, 1, meta['elem'], None)]

    def _plsql_type_named(self, full_name: str) -> int | None:
        # The PostgreSQL type of a package type a client names (#1607):
        # "OWNER"."PACKAGE"."TYPE", or "PACKAGE"."TYPE" in the session's schema.
        parts = [p.strip('"').upper() for p in full_name.split('.')]
        if len(parts) == 3:
            (owner, package, name) = parts
        elif len(parts) == 2:
            (package, name) = parts
            owner = None
        else:
            return None
        if not self._has_package_catalog:
            return None
        row = self._conn.execute(
            'SELECT to_regtype(p.package || %s || lower(p.name))::oid '
            'FROM sys.ora_plsql_types p JOIN sys.ora_packages k ON k.name = p.package '
            'WHERE p.package = lower(%s) AND p.name = %s AND p.public '
            "AND p.meta ? 'typecode' "
            'AND k.owner = coalesce(%s, sys.ora_owner(current_schema()))',
            ('.', package, name, owner),
        ).fetchone()
        return row[0] if row is not None else None

    def _type_shape(self, pg_oid: int, typmod: int = -1):
        """The TDS shape of a type (#1134): a _TdsObject of its attributes, a
        _TdsCollection of its element, or a scalar's leaf."""
        plsql = self._plsql_type(pg_oid)
        if plsql is not None:
            return self._plsql_shape(plsql[0], plsql[3])
        kind = self._type_kind(pg_oid)
        if kind is None:
            if pg_oid == _XML_OID:
                return _TDS_XMLTYPE  # an XMLType attribute or element (#1537)
            return _tds_scalar(self._pg_type_name(pg_oid), typmod)[0]
        if kind[0] == 'object':
            declared = self._declared_attribute_types(pg_oid)
            # A WITH TIME ZONE attribute is the ora_tstz composite, which keeps
            # no precision; its leaf takes the recorded one (#1308, #1552).
            zoned = self._attribute_tstz_precisions(pg_oid)
            return _TdsObject(
                tuple(
                    _declared_leaf(declared.get(n), m)
                    or (
                        _tds_timestamp(0x17, zoned[n])
                        if n in zoned
                        else self._type_shape(t, m)
                    )
                    for (n, t, m) in self._attributes(pg_oid)
                )
            )
        (_kind, _owner, _name, element, element_typmod) = kind
        bound = self._conn.execute(
            "SELECT substring(pg_get_constraintdef(oid) FROM '<=\\s*([0-9]+)')::int "
            'FROM pg_constraint WHERE contypid = %s',
            (pg_oid,),
        ).fetchone()
        # A recorded element type -- NVARCHAR2 / NCHAR, RAW(n) -- has its own
        # leaf (#1437, #1545).
        recorded = self._recorded_element(pg_oid)
        return _TdsCollection(
            varray=bound is not None and bound[0] is not None,
            bound=bound[0] if bound and bound[0] is not None else 0,
            element=_declared_leaf(
                (recorded[0], recorded[1], None) if recorded else None, element_typmod
            )
            or self._type_shape(element, element_typmod),
        )

    def _attribute_tstz_precisions(self, pg_oid: int) -> dict[str, int]:
        # The precision recorded for each TIMESTAMP(n) WITH TIME ZONE attribute
        # of a composite -- an object type's or a table's %ROWTYPE -- by name
        # (#1552); one not listed has Oracle's default, 6.
        if not self._has_tstz_precision:
            return {}
        return dict(
            self._conn.execute(
                'SELECT a.attname, p.prec FROM pg_type t '
                'JOIN pg_attribute a ON a.attrelid = t.typrelid '
                'JOIN sys.ora_tstz_precision p '
                'ON p.relid = a.attrelid AND p.attnum = a.attnum '
                'WHERE t.oid = %s',
                (pg_oid,),
            ).fetchall()
        )

    def _declared_attribute_types(
        self, pg_oid: int
    ) -> dict[str, tuple[str, int | None, int | None]]:
        # The type each attribute of a composite was declared as, where
        # sys.ora_columns records one (#1431): attribute name -> (Oracle type,
        # its recorded length, its recorded precision).
        if not self._has_column_catalog:
            return {}
        return {
            name: (data_type, length, precision)
            for name, data_type, length, precision in self._conn.execute(
                'SELECT a.attname, o.data_type, o.data_length, o.data_precision '
                'FROM pg_type t '
                'JOIN pg_attribute a ON a.attrelid = t.typrelid '
                'JOIN sys.ora_columns o ON o.relid = a.attrelid AND o.attnum = a.attnum '
                'WHERE t.oid = %s',
                (pg_oid,),
            ).fetchall()
        }

    def _attributes(self, pg_oid: int) -> list[tuple[str, int, int]]:
        # (name, type oid, typmod) of a composite's attributes, in order.
        return self._conn.execute(
            'SELECT a.attname, a.atttypid, a.atttypmod FROM pg_type t '
            'JOIN pg_attribute a ON a.attrelid = t.typrelid '
            'WHERE t.oid = %s AND a.attnum > 0 AND NOT a.attisdropped '
            "AND a.attname <> 'sys_nc_oid$' "
            + (
                # A table's INVISIBLE column is no attribute of its %ROWTYPE (#1195).
                'AND NOT EXISTS (SELECT 1 FROM sys.ora_invisible_columns h '
                'WHERE h.relid = a.attrelid AND h.attnum = a.attnum) '
                if self._has_invisible_catalog
                else ''
            )
            + 'ORDER BY a.attnum',
            (pg_oid,),
        ).fetchall()

    def _attribute_rows(self, pg_oid: int) -> list[tuple]:
        # The attribute cursor GET_TYPE_SHAPE returns for an object: one row per
        # attribute, as 23ai sends it -- a version, the name, the position, the
        # type name, its owner (a named type's only), its package, its OID, and
        # whether it is instantiable -- in the order the client reads them.
        rows = []
        declared = self._declared_attribute_types(pg_oid)
        for position, (attname, atttypid, atttypmod) in enumerate(
            self._attributes(pg_oid), 1
        ):
            kind = self._type_kind(atttypid)
            if atttypid == _XML_OID:
                # SYS.XMLTYPE, under its own id, as 23ai lists one (#1537).
                (type_name, owner, toid) = ('XMLTYPE', 'SYS', _XMLTYPE_OID)
            elif kind is None:
                type_name = _tds_scalar(self._pg_type_name(atttypid), atttypmod)[1]
                as_declared = declared.get(attname, (None, None, None))[0]
                # An NCLOB is an ora_clob like a CLOB; only its name tells them
                # apart, and the client types the attribute by it (#1431).
                if type_name == 'CLOB' and as_declared == 'NCLOB':
                    type_name = 'NCLOB'
                elif (type_name, as_declared) in (
                    ('VARCHAR2', 'NVARCHAR2'),
                    ('CHAR', 'NCHAR'),
                    ('BLOB', 'RAW'),  # a RAW(n) is a bytea (#1544)
                    # A FLOAT / REAL / DOUBLE PRECISION is a numeric (#1423).
                    *(('NUMBER', name) for name in _FLOAT_ATTRIBUTE_TYPES),
                ):
                    type_name = declared[attname][0]  # (#1383, #1416)
                (owner, toid) = (
                    None,
                    bytes(15) + bytes([_BUILTIN_TYPE_OID_BYTE[type_name]]),
                )
            else:
                (_kind, owner, type_name, _e, _m) = kind
                toid = _object_type_oid(atttypid)
            rows.append(
                (
                    1,
                    _oracle_column_name(attname),
                    position,
                    type_name,
                    owner,
                    None,
                    toid,
                    'YES',
                    None,
                    None,
                )
            )
        return rows

    def _execute_type_shape(self, sql: str, binds: Sequence) -> Result:
        """A block calling DBMS_PICKLER.GET_TYPE_SHAPE (#1134), answered whole:
        the type's return code, OID, version, TDS, attribute cursor, and -- in
        python-oracledb's own block -- its schema and name.

        A bind takes the answer for the argument it is in the call -- the
        full name first, the attribute cursor eighth -- or for the call's return
        value it is assigned; a bind outside the call by its name, as
        python-oracledb's :schema / :name / :package_name are (#1542).
        """
        names = [name.lower() for (name, _q) in bind_placeholders(sql, dedupe=True)]
        values: dict[str, object] = {n: b.value for (n, b) in zip(names, binds)}
        (roles, literal_name) = _type_shape_roles(sql)
        by_role = {roles.get(n, n): v for n, v in values.items()}
        full_name = str(by_role.get('full_name') or literal_name or '')
        attrs_rc = CursorResult(columns=list(_ATTRIBUTE_CURSOR_COLUMNS), rows=[])
        answer: dict[str, object] = {
            'ret_val': _TYPE_SHAPE_NOT_FOUND,
            'oid': None,
            # A type not found reads version 0, as 23ai answers (#1542).
            'version': 0,
            'tds': None,
            'attrs_rc': attrs_rc,
            'package_name': None,
            'schema': None,
            'name': None,
            'instantiable': 'YES',
            'supertype_owner': None,
            'supertype_name': None,
            'subtype_rc': CursorResult(
                columns=list(_ATTRIBUTE_CURSOR_COLUMNS), rows=[]
            ),
        }
        row_type = full_name.upper().endswith('%ROWTYPE')
        (schema, _dot, name) = full_name.rpartition('.')
        if row_type:
            name = name[: -len('%ROWTYPE')]
        if (schema.strip('"').upper(), name.strip('"').upper()) == ('SYS', 'XMLTYPE'):
            # A built-in, opaque type with no PostgreSQL counterpart to look up:
            # answered as Oracle answers it (#1536).
            answer.update(
                ret_val=0,
                oid=_XMLTYPE_OID,
                version=1,
                tds=_XMLTYPE_TDS,
                schema='SYS',
                name='XMLTYPE',
            )
            return Result(out_binds=[answer.get(n, values.get(n)) for n in names])
        found = self._conn.execute(
            'SELECT t.oid FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace '
            'LEFT JOIN pg_class c ON c.oid = t.typrelid '
            'WHERE sys.ora_owner(n.nspname) = coalesce(%s, sys.ora_owner(current_schema())) '
            'AND sys.ora_name(coalesce(c.relname, t.typname)) = %s '
            "AND coalesce(c.relkind = 'r', false) = %s "
            # A type the session may use (#1621): its own, or one granted it.
            # A table's row type is a table's privilege, which is not kept.
            'AND (%s OR sys.ora_type_visible(sys.ora_owner(n.nspname), '
            'sys.ora_name(coalesce(c.relname, t.typname)))) '
            # A package's schema is no owner: its types are the package's (#1607).
            + (
                'AND NOT EXISTS (SELECT 1 FROM sys.ora_packages k '
                'WHERE k.name = n.nspname) '
                if self._has_package_catalog
                else ''
            )
            + 'LIMIT 1',
            (schema.strip('"') or None, name.strip('"'), row_type, row_type),
        ).fetchone()
        # A package's type (#1607): "OWNER"."PACKAGE"."TYPE", or a two-part name
        # no schema has a type by.
        plsql_oid = (
            self._plsql_type_named(full_name)
            if not row_type and (found is None or full_name.count('.') == 2)
            else None
        )
        if plsql_oid is not None:
            plsql = self._plsql_type(plsql_oid)
            if plsql is not None:
                (owner, package, type_name, meta) = plsql
                answer.update(
                    ret_val=0,
                    oid=_object_type_oid(plsql_oid),
                    version=1,
                    tds=_tds(self._plsql_shape(owner, meta)),
                    schema=owner,
                    package_name=package,
                    name=type_name,
                )
                attrs_rc.rows.extend(self._plsql_attribute_rows(owner, meta))
                return Result(
                    out_binds=[
                        answer.get(roles.get(n, n), values.get(n)) for n in names
                    ]
                )
        pg_oid = found[0] if found is not None else None
        kind = self._type_kind(pg_oid) if pg_oid is not None else None
        if pg_oid is not None and kind is not None:
            (_kind, owner, type_name, _e, _m) = kind
            shape = self._type_shape(pg_oid)
            answer.update(
                ret_val=0,
                oid=_object_type_oid(pg_oid),
                version=1,
                tds=_tds(shape),
                schema=owner,
                name=f'{name.strip(chr(34))}%ROWTYPE' if row_type else type_name,
            )
            if row_type:
                # full_name is IN OUT, and the server hands it back qualified by
                # the table's owner -- 'T%ROWTYPE' returns as 'OWNER.T%ROWTYPE'.
                # python-oracledb takes the record's schema from it and reads
                # the attributes from all_tab_cols by that owner; echoed back
                # unqualified, the schema was empty and the query found no
                # columns at all (#1479).
                answer['full_name'] = f'{owner}.{name.strip(chr(34))}%ROWTYPE'
            if isinstance(shape, _TdsObject):
                attrs_rc.rows.extend(self._attribute_rows(pg_oid))
        return Result(
            out_binds=[answer.get(roles.get(n, n), values.get(n)) for n in names]
        )

    def _plsql_member(self, owner: str, name: str | None, ref: dict) -> dict:
        # A package type's field or element as the image walkers lay it out
        # (#1607): a built-in by its wire type, an NVARCHAR2 in AL16UTF16; a
        # named type with its own DbObjectType.
        type_name = str(ref['name'])
        member: dict = {
            'name': name or 'element',
            'type_name': type_name,
            'data_type': None if ref.get('named') else type_name_to_tns(type_name),
            'charset': AL16UTF16_CHARSET if ref.get('charset') == 'NCHAR_CS' else None,
        }
        if ref.get('named'):
            pg_oid = self._plsql_named_oid(owner, ref)
            nested = (
                self._nested_type(
                    pg_oid, f'{owner}.{type_name}', member['name'], type_name
                )
                if pg_oid is not None
                else None
            )
            if nested is None:
                raise UnsupportedFeature(f'package type: no type {type_name}')
            member['object_type'] = nested
        return member

    def _plsql_object_type(
        self, pg_oid: int
    ) -> tuple[DbObjectType, CompositeInfo] | None:
        """A package's record or index-by table as the object or collection type
        a client binds and reads (#1607), its fields and element as declared,
        and the composite it is stored as -- an index-by table's ``(keys,
        vals)`` -- registered with psycopg. None for any other type."""
        plsql = self._plsql_type(pg_oid)
        if plsql is None:
            return None
        (owner, package, name, meta) = plsql
        oid = _object_type_oid(pg_oid)
        if meta.get('typecode') == 'PL/SQL RECORD':
            typ = DbObjectType(
                owner,
                name,
                oid,
                1,
                [
                    self._plsql_member(owner, f['name'], f['type'])
                    for f in meta['attrs']
                ],
                package_name=package,
            )
        elif meta.get('coll_type') == 'PL/SQL INDEX TABLE':
            typ = DbObjectType(
                owner,
                name,
                oid,
                1,
                [],
                is_collection=True,
                collection_type=COLLECTION_PLSQL_INDEX_TABLE,
                element=self._plsql_member(owner, None, meta['elem']),
                max_elements=0,
                package_name=package,
            )
        else:
            return None
        regtype = self._conn.execute(
            'SELECT %s::oid::regtype::text', (pg_oid,)
        ).fetchone()
        info = CompositeInfo.fetch(self._conn, regtype[0]) if regtype else None
        if info is None:
            return None
        register_composite(info, self._conn)
        return (typ, info)

    def _object_type(self, pg_oid: int) -> tuple[DbObjectType, CompositeInfo] | None:
        """The Oracle object type a PostgreSQL composite oid stands for (#1127).

        Built the way ``all_types`` / ``all_type_attrs`` build what a client
        reads, so the backend and the client agree on the OID and the attribute
        order.
        The composite is registered with psycopg on first use, so a value of it
        binds and loads as a tuple rather than its text form. None when the oid
        is not an object type.
        """
        if pg_oid in self._object_types:
            return self._object_types[pg_oid]
        # A package's record or index-by table (#1607).
        plsql = self._plsql_object_type(pg_oid)
        if plsql is not None:
            self._object_types[pg_oid] = plsql
            return plsql
        oid = _object_type_oid(pg_oid)
        # The catalogs rather than the sys views, though the answer is the same:
        # reading a view inside the session's open transaction holds a lock that
        # the next session's CREATE OR REPLACE VIEW at connect waits on, so a
        # new connection hung until this one committed.
        row = self._conn.execute(
            'SELECT sys.ora_owner(n.nspname), sys.ora_name(t.typname), '
            'n.nspname, t.typname '
            'FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace '
            'JOIN pg_class c ON c.oid = t.typrelid '
            "WHERE t.oid = %s AND t.typtype = 'c' AND c.relkind = 'c' "
            "AND t.typname !~ '[$]ref$'",
            (pg_oid,),
        ).fetchone()
        entry = None
        if row is not None:
            owner, name, pg_schema, pg_name = row
            attrs = []
            # information_schema is PostgreSQL's own, never replaced at connect,
            # so reading it holds nothing a new session waits on. The mapping is
            # the one all_type_attrs applies.
            for attr_name, type_name, type_owner, attr_type_oid in self._conn.execute(
                'SELECT sys.ora_name(a.attribute_name), '
                f"CASE WHEN a.attribute_udt_name = '{_TSTZ_TYPE}' "
                "THEN 'TIMESTAMP WITH TZ' "
                # The national types, told apart by their record (#1383).
                f"WHEN a.attribute_udt_name = '{_CLOB_TYPE}' "
                "AND o.data_type = 'NCLOB' THEN 'NCLOB' "
                "WHEN o.data_type IN ('NVARCHAR2', 'NCHAR', 'RAW', 'REAL', "
                "'DOUBLE PRECISION', 'FLOAT') THEN o.data_type "
                f"WHEN a.attribute_udt_name = '{_CLOB_TYPE}' THEN 'CLOB' "
                f"WHEN a.attribute_udt_name = '{_BLOB_TYPE}' THEN 'BLOB' "
                f"WHEN a.attribute_udt_name = '{_DATE_TYPE}' THEN 'DATE' "
                "WHEN a.data_type = 'USER-DEFINED' "
                'THEN sys.ora_name(a.attribute_udt_name) '
                "WHEN a.data_type = 'xml' THEN 'XMLTYPE' "
                "WHEN a.data_type = 'timestamp with time zone' "
                "THEN 'TIMESTAMP WITH LOCAL TZ' "
                'ELSE sys.ora_type_name(a.data_type) END, '
                "CASE WHEN a.data_type = 'xml' THEN 'SYS' "
                "WHEN a.data_type = 'USER-DEFINED' "
                'AND a.attribute_udt_name NOT IN '
                f"('{_TSTZ_TYPE}', '{_CLOB_TYPE}', '{_BLOB_TYPE}', '{_DATE_TYPE}') "
                'THEN sys.ora_owner(a.attribute_udt_schema) END, '
                "format('%%I.%%I', a.attribute_udt_schema, a.attribute_udt_name)"
                '::regtype::oid '
                'FROM information_schema.attributes a '
                + (
                    'LEFT JOIN sys.ora_columns o ON o.relid = '
                    "format('%%I.%%I', a.udt_schema, a.udt_name)::regclass "
                    'AND o.attnum = a.ordinal_position '
                    if self._has_column_catalog
                    else 'CROSS JOIN (SELECT NULL::text AS data_type) o '
                )
                + 'WHERE a.udt_schema = %s AND a.udt_name = %s '
                'ORDER BY a.ordinal_position',
                (pg_schema, pg_name),
            ):
                attr = {
                    'name': attr_name,
                    'type_name': type_name,
                    'data_type': type_name_to_tns(type_name),
                    # A client sends and expects an NVARCHAR2 attribute in
                    # AL16UTF16 inside the object image (#1383).
                    'charset': AL16UTF16_CHARSET
                    if type_name in ('NVARCHAR2', 'NCHAR')
                    else None,
                }
                if type_name == 'XMLTYPE':
                    # An XMLType attribute rides as an XMLType image (#1537).
                    attr['object_type'] = _XMLTYPE_DBTYPE
                elif type_owner is not None:
                    # An object or collection attribute carries its own type, as
                    # a client's describe embeds it, for the image walkers (#1265).
                    attr['object_type'] = self._nested_type(
                        attr_type_oid, f'{owner}.{name}', attr_name, type_name
                    )
                attrs.append(attr)
            regtype = self._conn.execute(
                'SELECT %s::oid::regtype::text', (pg_oid,)
            ).fetchone()
            info = CompositeInfo.fetch(self._conn, regtype[0]) if regtype else None
            if info is not None:
                register_composite(info, self._conn)
                entry = (DbObjectType(owner, name, oid, 1, attrs), info)
        self._object_types[pg_oid] = entry
        return entry

    def _nested_type(
        self, pg_oid: int, outer: str, attr_name: str, type_name: str
    ) -> DbObjectType:
        # The type of an attribute that is itself an object or a collection
        # (#1265). PostgreSQL refuses a composite that contains itself; the
        # guard is there should a domain ever let one through.
        if pg_oid in self._describing:
            raise UnsupportedFeature(
                f'object type {outer}: attribute {attr_name} refers back to it'
            )
        self._describing.add(pg_oid)
        try:
            kind = self._type_kind(pg_oid)
            if kind is not None and kind[0] == 'object':
                entry = self._object_type(pg_oid)
                if entry is not None:
                    return entry[0]
            if kind is not None and kind[0] == 'collection':
                coll = self._collection_type(pg_oid)
                if coll is not None:
                    return coll
        finally:
            self._describing.discard(pg_oid)
        raise UnsupportedFeature(
            f'object type {outer}: attribute {attr_name} is of type {type_name}, '
            'which is neither an object nor a collection type'
        )

    def _nested_value(self, attr: dict, value: object) -> object:
        # A fetched object or collection attribute as its DbObject (#1265); any
        # other attribute passes through.
        nested = attr.get('object_type')
        if nested is None or value is None or nested is _XMLTYPE_DBTYPE:
            # An XMLType attribute's value is its document's text (#1537).
            return value
        pg_oid = _pg_oid_of(nested.oid) or 0
        if nested.is_collection:
            base = self._conn.execute(
                'SELECT typbasetype FROM pg_type WHERE oid = %s', (pg_oid,)
            ).fetchone()
            return self._db_collection(nested, value, base[0] if base else 0)
        entry = self._object_type(pg_oid)
        if entry is None:
            raise UnsupportedFeature(f'object type {nested.name}: not found')
        return self._db_object(entry[0], entry[1], value)

    def _db_object(
        self, typ: DbObjectType, info: CompositeInfo, value: object
    ) -> object:
        # One composite cell as the DbObject the Mirror encodes. A cursor that
        # executed before the composite was registered hands the text form
        # `(7,Alice)`; the registered loader parses that the way psycopg would.
        if value is None:
            return None
        if isinstance(value, str):
            loader = self._conn.adapters.get_loader(info.oid, psycopg.pq.Format.TEXT)
            if loader is None:
                raise UnsupportedFeature(f'object type {typ.name}: no loader')
            value = loader(info.oid, self._conn).load(value.encode('utf-8'))
        if not isinstance(value, tuple):
            raise UnsupportedFeature(f'object type {typ.name}: not a composite value')
        if typ.is_collection:
            # A package's index-by table (#1607): its keys and its values, the
            # values in key order, an element of a named type as its object.
            (keys, vals) = value
            element = typ.element or {}
            vals = [self._nested_value(element, v) for v in (vals or [])]
            return typ.newobject(vals, keys=list(keys or []))
        return typ.newobject(
            {
                a['name']: _reconstruct_tstz(v)
                if a['data_type'] == TNS_TYPE_TIMESTAMPTZ and hasattr(v, 'utc')
                # A LOCAL TIME ZONE attribute goes out as a column of it does:
                # the instant in the database time zone, naive (#1208, #1270).
                else _to_ltz(v)
                if a['data_type'] == TNS_TYPE_TIMESTAMPLTZ
                else self._nested_value(a, v)
                for a, v in zip(typ.attrs, value)
            }
        )

    def _ref_cell(self, type_pg_oid: int, value: object) -> DbRef | None:
        # A `<type>$ref` cell: a tuple once the composite is registered, its text
        # form `(16842,1c4e...)` before -- both carry the table oid and row id.
        if value is None:
            return None
        if isinstance(value, str):
            tab, _, rid = value.strip('()').partition(',')
            value = (int(tab) if tab else None, rid or None)
        if not isinstance(value, tuple):
            raise UnsupportedFeature('REF column: not a composite value')
        return self._db_ref(type_pg_oid, value[0], value[1])

    def _resolve_object_binds(self, binds: Sequence) -> list:
        # An object (ADT) bind arrives as the image the client packed, bare or
        # in a BindVar. Decode it against the type the OID names and bind the
        # composite; a NULL object arrived as None already (#1127).
        out: list = []
        for b in binds:
            value = b.value if isinstance(b, BindVar) else b
            if isinstance(value, ObjectImage):
                out.append(self._object_bind_value(value))
            elif isinstance(value, DbRef):
                out.append(self._ref_bind_value(value))
            elif (
                isinstance(b, BindVar)
                and value is None
                and b.tns_type == TNS_TYPE_ADT
                and (typed := self._null_object_type(b.toid)) is not None
            ):
                out.append(replace(b, value=_TypedNull(typed)))
            else:
                out.append(b)
        return out

    def _null_object_type(self, toid: bytes) -> int | None:
        # The PostgreSQL type of a NULL object bind, from the type OID its OAC
        # carries (#1622): one of our object or collection types, else None.
        oid = toid[4:20] if len(toid) >= 20 else toid
        pg_oid = _pg_oid_of(oid)
        if pg_oid is None:
            return None
        if self._collection_type(pg_oid) is None and self._object_type(pg_oid) is None:
            return None
        return pg_oid

    def _object_bind_value(self, image: ObjectImage) -> object:
        oid = image.type_oid or b''
        # A value's toid is the constructed 36-byte form (00 22 02 08 + OID +
        # extent); the OAC carries the bare 16-byte OID. Accept either.
        if len(oid) >= 20:
            oid = oid[4:20]
        pg_oid = _pg_oid_of(oid)
        coll = self._collection_type(pg_oid) if pg_oid is not None else None
        if coll is not None:
            array = self._collection_bind_value(coll, image)
            row = self._conn.execute(
                'SELECT %s::oid::regtype::text', (pg_oid,)
            ).fetchone()
            return _CollectionBind(array, row[0]) if row else array
        entry = self._object_type(pg_oid) if pg_oid is not None else None
        if entry is None:
            raise UnsupportedFeature(
                f'object bind: no object type has the OID {oid.hex()}'
            )
        typ = entry[0]
        if typ.is_collection:
            # A package's index-by table (#1607): its elements under their keys.
            (elements, keys) = decode_collection_keyed(
                image.image, typ.element or {}, AL32UTF8_CHARSET
            )
            values = _served_lobs(typ.newobject(elements, keys=keys), image).aslist()
            return self._index_table_value(
                entry, values, keys if keys is not None else []
            )
        value = _served_lobs(
            DbObject(typ.name, decode_object_image(image.image, typ.attrs), dbtype=typ),
            image,
        )
        return self._composite_value(entry, value.asdict())

    def _index_table_value(
        self, entry: tuple[DbObjectType, CompositeInfo], values: list, keys: list
    ) -> object:
        # A package's index-by table as the composite it is stored as (#1607):
        # its keys and its values, an element of a named type as its composite.
        (typ, info) = entry
        factory = info.python_type
        if factory is None:
            raise UnsupportedFeature(f'collection type {typ.name}: not registered')
        return factory(list(keys), self._collection_values(typ, values))

    def _collection_bind_value(
        self, typ: DbObjectType, image: ObjectImage
    ) -> list | str:
        # A VARRAY / nested-table bind (#1206): the image's elements, bound as the
        # array the domain is over; an object element as its composite.
        element = typ.element or {}
        values = _served_lobs(
            typ.newobject(decode_collection_image(image.image, element)), image
        ).aslist()
        return self._collection_values(typ, values)

    def _collection_values(self, typ: DbObjectType, values: list) -> list | str:
        # A collection's elements as the array its domain is over: an object
        # element as its composite, a NUMBER list all one Python type.
        element = typ.element or {}
        nested = element.get('object_type')
        if nested is _XMLTYPE_DBTYPE:
            # XMLType elements bind as their documents' text (#1537).
            return values
        if nested is not None and nested.is_collection:
            # A collection of collections (#1276): psycopg would bind a list of
            # lists as a rectangular multi-dimensional array, which the domain
            # refuses, so the value goes as an array literal PostgreSQL casts.
            return _array_literal(
                [None if v is None else _array_literal(v.aslist()) for v in values]
            )
        if nested is None:
            # A NUMBER decodes as an int when it is whole and a Decimal when not,
            # and psycopg binds no list that mixes the two.
            if any(isinstance(v, decimal.Decimal) for v in values):
                values = [
                    decimal.Decimal(v)
                    if isinstance(v, int) and not isinstance(v, bool)
                    else v
                    for v in values
                ]
            return values
        entry = self._object_type(_pg_oid_of(nested.oid) or 0)
        if entry is None:
            raise UnsupportedFeature(f'collection type {typ.name}: no element type')
        return [
            None if v is None else self._composite_value(entry, v.asdict())
            for v in values
        ]

    def _composite_value(
        self, entry: tuple[DbObjectType, CompositeInfo], attrs: dict
    ) -> object:
        # An object's attribute values as the registered composite psycopg binds.
        typ, info = entry
        factory = info.python_type  # set by register_composite
        if factory is None:
            raise UnsupportedFeature(f'object type {typ.name}: not registered')
        return factory(
            *(
                self._tstz_composite(attrs.get(a['name']))
                if a['data_type'] == TNS_TYPE_TIMESTAMPTZ
                else _from_ltz(attrs.get(a['name']))
                if a['data_type'] == TNS_TYPE_TIMESTAMPLTZ
                else self._nested_bind_value(a, attrs.get(a['name']))
                for a in typ.attrs
            )
        )

    def _nested_bind_value(self, attr: dict, value: object) -> object:
        # A bound object or collection attribute (#1265), already decoded into a
        # DbObject, as the composite or array PostgreSQL stores; any other
        # attribute passes through.
        nested = attr.get('object_type')
        if nested is None or not isinstance(value, DbObject):
            return value
        if nested.collection_type == COLLECTION_PLSQL_INDEX_TABLE:
            # A package's index-by table inside a record (#1607).
            entry = self._object_type(_pg_oid_of(nested.oid) or 0)
            if entry is not None:
                return self._index_table_value(
                    entry, value.aslist(), list(value._keys or [])
                )
        if nested.is_collection:
            return self._collection_values(nested, value.aslist())
        entry = self._object_type(_pg_oid_of(nested.oid) or 0)
        if entry is None:
            raise UnsupportedFeature(f'object type {nested.name}: not found')
        return self._composite_value(entry, value.asdict())

    def _tstz_composite(self, value: object) -> object:
        # An aware datetime as the ora_tstz composite a TIMESTAMP WITH TIME ZONE
        # attribute is stored as: the instant and the offset it was entered at
        # (#519). A naive one is taken as UTC.
        if not isinstance(value, datetime.datetime) or self._tstz_info is None:
            return value
        factory = self._tstz_info.python_type
        if factory is None:
            return value
        offset = value.utcoffset() or datetime.timedelta(0)
        instant = (
            value
            if value.tzinfo is not None
            else value.replace(tzinfo=datetime.timezone.utc)
        )
        return factory(
            instant.astimezone(datetime.timezone.utc), int(offset.total_seconds())
        )

    def _execute_sequential(
        self,
        sql: str,
        params: dict | None,
        original: str | None = None,
        *,
        prelude: str | None = None,
    ) -> Result:
        # SAVEPOINT + statement + RELEASE as three round-trips; the fallback path.
        # A `prelude` runs inside the savepoint first, so a SET LOCAL there is
        # undone with a statement that fails and ends with one that commits.
        # An our-side rejection after the statement ran (UnsupportedFeature on an
        # unmapped column type) is undone too, and left for the session to map.
        cursor = self._conn.cursor()
        try:
            with self._under_savepoint():
                if prelude is not None:
                    cursor.execute(prelude)
                cursor.execute(sql, params)
                result = self._build_result(cursor, sql, original or '')
        except psycopg.Error as exc:
            # A PostgreSQL failure surfaces as a clean ORA error — never a desync.
            # Map the SQLSTATE to the matching Oracle code so error-conditional
            # client flows (e.g. a best-effort DROP that swallows ORA-00942) work.
            raise _backend_error(exc, original=original, translated=sql) from exc
        return result

    def _execute_pipelined(
        self, sql: str, params: dict | None, original: str | None = None
    ) -> Result:
        # SAVEPOINT + statement + RELEASE shipped in ONE round-trip via a psycopg
        # pipeline. Each command uses its own cursor so the statement's cursor keeps
        # its own result (rowcount / description / rows / pgresult) after the sync —
        # a shared cursor would only retain the last command's (RELEASE) result.
        savepoint = self._conn.cursor()
        statement = self._conn.cursor()
        release = self._conn.cursor()
        try:
            with self._conn.pipeline():
                savepoint.execute('SAVEPOINT _mirror_stmt')
                statement.execute(sql, params)
                release.execute('RELEASE SAVEPOINT _mirror_stmt')
        except psycopg.Error as exc:
            # The statement failed inside the pipeline; the RELEASE that followed it
            # was discarded, so the savepoint still stands — roll the statement back
            # to it (preserving the rest of the transaction) and surface a clean ORA
            # error, never a desync.
            self._conn.execute('ROLLBACK TO SAVEPOINT _mirror_stmt')
            self._conn.execute('RELEASE SAVEPOINT _mirror_stmt')
            raise _backend_error(exc, original=original, translated=sql) from exc
        # The pipeline succeeded, so the savepoint is already released and the
        # statement had its effect. Building the result can only raise on an
        # unencodable SELECT column — no side effect to undo — so let it propagate
        # for the session to map to an ORA error; the connection stays usable.
        return self._build_result(statement, sql, original or '')

    @_while_connected
    @_plainly_quoted
    def execute_many(self, sql: str, rows: Sequence[Sequence]) -> int | Result:
        # One run of the statement in the translation report, however many rows.
        return self._committing(
            lambda: self._reported(sql, lambda: self._execute_batch(sql, rows))
        )

    def _execute_batch(self, sql: str, rows: Sequence[Sequence]) -> int | Result:
        # Array DML (executemany) in one round-trip: translate the statement once
        # and send every bind row through psycopg's executemany (which pipelines),
        # instead of a round-trip per row — the difference is ~7 s vs a few ms for
        # 500 rows against a remote database. Returns the total affected-row count.
        # The Mirror calls this only for the non-batcherrors path, where a per-row
        # failure aborts the whole batch — exactly Oracle's non-batcherrors DML.
        native = self._is_native(sql)
        if not native:
            sql = self._to_raw_targets(
                self._expand_visible_columns(_strip_leading_comments(sql))
            )
        rows = list(rows)
        if not rows:
            return 0
        translated = sql if native else _translated_batch(sql)
        with_rowid = None if native else self._returning_rowid(sql, translated)
        if with_rowid is not None:
            translated = with_rowid
        bound_sql, _ = _translate_binds(translated, rows[0])
        params = [_translate_binds(translated, row)[1] for row in rows]
        cursor = self._conn.cursor()
        touched: list = []
        try:
            with self._under_savepoint():
                if with_rowid is None:
                    cursor.executemany(bound_sql, params)
                    affected = cursor.rowcount
                else:
                    # One result per iteration, each the rowids that row touched;
                    # the batch's count is all of them, its rowid the very last.
                    cursor.executemany(bound_sql, params, returning=True)
                    while True:
                        touched.extend(r[0] for r in cursor.fetchall())
                        if not cursor.nextset():
                            break
                    affected = len(touched)
        except psycopg.Error as exc:
            # Oracle undoes only the row that failed: the rows before it stay
            # applied, and the error's rowcount says how many there were. The
            # pipelined batch cannot tell which row failed, and its savepoint
            # undid them all, so replay the rows one at a time up to the failure
            # (#1365).
            raise self._replay_until_failure(bound_sql, params) from exc
        if with_rowid is not None:
            return Result(
                rowcount=affected, last_rowid=touched[-1] if touched else None
            )
        return max(affected, 0)

    def _replay_until_failure(self, bound_sql: str, params: list) -> BackendError:
        # Run the rows of a batch that failed one at a time, each in a savepoint
        # of its own, until one fails; that one is undone and the rest are kept.
        # The failing row's error comes back carrying how many rows were
        # applied before it, which is what Oracle reports for a batch that stops
        # part-way (#998, #1365). Not the per-iteration counts: this path serves
        # a client that did not ask for them (that one is execute_many_rowcounts),
        # and a count block it did not ask for is a reply it cannot read.
        cursor = self._conn.cursor()
        cursor.execute('SAVEPOINT _mirror_replay')
        applied = 0
        for row in params:
            try:
                with self._under_savepoint('_mirror_row'):
                    cursor.execute(bound_sql, row)
            except psycopg.Error as exc:
                self._conn.execute('RELEASE SAVEPOINT _mirror_replay')
                err = _backend_error(exc)
                err.rowcount = applied
                return err
            applied += max(cursor.rowcount, 0)
        # The batch failed and no single row does: a failure only the batch as a
        # whole met. Nothing is left applied.
        self._conn.execute('ROLLBACK TO SAVEPOINT _mirror_replay')
        self._conn.execute('RELEASE SAVEPOINT _mirror_replay')
        return BackendError('the array statement failed', rowcount=0)

    @_while_connected
    @_plainly_quoted
    def execute_many_rowcounts(
        self, sql: str, rows: Sequence[Sequence]
    ) -> tuple[int, list[int]]:
        return self._reported(sql, lambda: self._execute_batch_rowcounts(sql, rows))

    def _execute_batch_rowcounts(
        self, sql: str, rows: Sequence[Sequence]
    ) -> tuple[int, list[int]]:
        """Array DML reporting the per-iteration affected-row counts (#18).

        The Mirror calls this only when the client asked for
        ``arraydmlrowcounts`` and did not ask for batcherrors, so the cost here
        is opt-in.

        Row by row, rather than through ``executemany`` -- and deliberately,
        because Oracle's semantics for a batch that ABORTS are what decide it.
        The rows before the failing one really applied and their counts are owed
        to the client in the error reply, so the batch cannot sit inside one
        savepoint: rolling that back would undo them. Each iteration gets its own
        savepoint instead, which is the same per-statement model the rest of this
        backend uses, and leaves the earlier rows exactly where Oracle leaves
        them.

        (``executemany(..., returning=True)`` does report per-statement counts
        through ``nextset()`` and keeps the pipeline, which would be faster. It
        cannot serve the abort case: once the batch raises, the counts for the
        rows that did apply are no longer reachable.)
        """
        rows = list(rows)
        if not rows:
            return 0, []
        translated = (
            sql
            if self._is_native(sql)
            else _translated_batch(self._to_raw_targets(sql))
        )
        bound_sql, _ = _translate_binds(translated, rows[0])
        cursor = self._conn.cursor()
        counts: list[int] = []
        total = 0
        for row in rows:
            _, params = _translate_binds(translated, row)
            try:
                with self._under_savepoint():
                    cursor.execute(bound_sql, params)
            except psycopg.Error as exc:
                error = _backend_error(exc, original=sql, translated=bound_sql)
                # What applied before the failure, and how much of it per
                # iteration: a client that asked for the counts is owed them in
                # the ERROR reply as much as in a successful one (#1031).
                error.rowcount = total
                error.row_counts = counts
                raise error from exc
            affected = max(cursor.rowcount, 0)
            counts.append(affected)
            total += affected
        return total, counts

    def _object_table_type(self, table: str) -> int | None:
        # The pg_type oid of an Oracle object table's type (CREATE TABLE t OF
        # type), from the registry the CREATE filled; None for any other table.
        row = self._conn.execute(
            'SELECT o.typ FROM sys.ora_object_tables o WHERE o.relid = to_regclass(%s)',
            (table,),
        ).fetchone()
        return None if row is None else int(row[0])

    def _ref_identity(self, type_pg_oid: int) -> tuple[str, str]:
        # (schema, name) of the object type a REF points at, as all_types has it.
        row = self._conn.execute(
            'SELECT sys.ora_owner(n.nspname), sys.ora_name(t.typname) '
            'FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace '
            'WHERE t.oid = %s',
            (type_pg_oid,),
        ).fetchone()
        if row is None:
            raise UnsupportedFeature(f'REF: no object type has pg oid {type_pg_oid}')
        return row[0], row[1]

    def _db_ref(
        self, type_pg_oid: int, table_oid: int | None, row_id: object
    ) -> DbRef | None:
        # A REF value for the client: the private locator plus the identity of
        # the type it points at, which the describe and the value both carry.
        if table_oid is None or row_id is None:
            return None
        schema, name = self._ref_identity(type_pg_oid)
        return DbRef(
            _ref_locator(type_pg_oid, table_oid, row_id),
            type_name=name,
            type_schema=schema,
            type_oid=_object_type_oid(type_pg_oid),
        )

    def _execute_ref_select(self, match: 're.Match[str]') -> Result:
        # Serve `SELECT REF(alias) FROM table alias [rest]` (#139/#1127). The
        # table must be an Oracle object table; each row's REF is its table oid
        # and hidden object id, which stay put across UPDATE and VACUUM FULL.
        ref_alias, table, table_alias, rest = match.groups()
        type_pg_oid = self._object_table_type(table)
        if type_pg_oid is None:
            raise UnsupportedFeature(
                f'REF({ref_alias}): {table} is not an object table'
            )
        query = (
            f'SELECT {table_alias}.tableoid, {table_alias}.{_OBJECT_ID_COLUMN} '
            f'FROM {table} {table_alias}{rest}'
        )
        try:
            with self._under_savepoint():
                found = self._conn.execute(query).fetchall()
        except psycopg.Error as exc:
            raise _backend_error(exc) from exc
        rows = [(self._db_ref(type_pg_oid, tab, rid),) for tab, rid in found]
        column = self._ref_column_meta(f'REF({ref_alias})', type_pg_oid)
        return Result(columns=[column], rows=rows)

    def _ref_column_meta(self, name: str, type_pg_oid: int) -> ColumnMeta:
        schema, type_name = self._ref_identity(type_pg_oid)
        return ColumnMeta(
            name=name.upper().encode('utf-8'),
            data_type=TNS_TYPE_REF,
            data_length=4000,
            max_size=0,
            type_name=type_name.encode('ascii'),
            type_schema=schema.encode('ascii'),
            type_oid=_object_type_oid(type_pg_oid),
        )

    def _ref_target(self, pg_oid: int) -> int | None:
        # For a `<type>$ref` companion composite, the pg_type oid of <type>;
        # None for any other type. Cached per session.
        if pg_oid in self._ref_targets:
            return self._ref_targets[pg_oid]
        row = self._conn.execute(
            'SELECT target.oid FROM pg_type ref '
            'JOIN pg_type target ON target.typnamespace = ref.typnamespace '
            "AND target.typname || '$ref' = ref.typname "
            "WHERE ref.oid = %s AND ref.typname ~ '[$]ref$'",
            (pg_oid,),
        ).fetchone()
        target = None if row is None else int(row[0])
        self._ref_targets[pg_oid] = target
        return target

    def _ref_bind_value(self, ref: DbRef) -> object:
        # A REF the client binds is the locator this backend handed it; it
        # becomes the `<type>$ref` composite the column / sys.deref() take.
        parsed = _parse_ref_locator(ref.bytes)
        if parsed is None:
            raise UnsupportedFeature('REF bind: not a locator this backend issued')
        type_pg_oid, table_oid, row_id = parsed
        info = self._ref_composites.get(type_pg_oid)
        if info is None:
            named = self._conn.execute(
                "SELECT format('%%I.%%I', n.nspname, t.typname || '$ref') "
                'FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace '
                'WHERE t.oid = %s',
                (type_pg_oid,),
            ).fetchone()
            if named is None:
                raise UnsupportedFeature(f'REF bind: no type with pg oid {type_pg_oid}')
            ref_name = named[0]
            info = CompositeInfo.fetch(self._conn, ref_name)
            if info is None:
                raise UnsupportedFeature(f'REF bind: no {ref_name} type')
            register_composite(info, self._conn)
            self._ref_composites[type_pg_oid] = info
        factory = info.python_type
        if factory is None:
            raise UnsupportedFeature('REF bind: companion type not registered')
        return factory(table_oid, row_id)

    def _domain_type(self, pgresult, index: int) -> int | None:
        # The Oracle wire type if result column `index` comes from one of the typed
        # domains — TNS_TYPE_CLOB / TNS_TYPE_BLOB (ora_clob / ora_blob, #534) or
        # TNS_TYPE_INTERVALYM (ora_intervalym, #504) — else None. A domain value
        # reports its base type on the wire (text / bytea / interval), so trace the
        # column back to its source table + attribute (libpq ftable / ftablecol) and
        # read the real declared type from pg_attribute — cached per (relid, attnum).
        # A computed column (ftable 0) isn't a domain column.
        if not self._domain_type_by_oid:
            return None
        relid = pgresult.ftable(index)
        if not relid:
            return None
        attnum = pgresult.ftablecol(index)
        key = (relid, attnum)
        if key not in self._domain_col_cache:
            row = self._conn.execute(
                'SELECT atttypid FROM pg_attribute WHERE attrelid = %s AND attnum = %s',
                (relid, attnum),
            ).fetchone()
            atttypid = row[0] if row else None
            self._domain_col_cache[key] = (
                self._domain_type_by_oid.get(atttypid) if atttypid is not None else None
            )
        return self._domain_col_cache[key]

    def _returns_a_value(self, name: str) -> bool:
        # Whether every function of this name returns a value of its own: not
        # void, and not the record a function with OUT parameters returns.
        rows = self._conn.execute(
            "SELECT prorettype = 'void'::regtype OR proargmodes IS NOT NULL "
            "FROM pg_proc WHERE proname = lower(%s) AND prokind = 'f'",
            (name.rsplit('.', 1)[-1],),
        ).fetchall()
        return bool(rows) and not any(procedure_like for (procedure_like,) in rows)

    def _routine_arities(self, name: str) -> set[int]:
        # How many arguments a routine of this name takes, each overload and
        # each count its defaults allow (#1497).
        rows = self._conn.execute(
            'SELECT pronargs, pronargdefaults FROM pg_proc WHERE proname = lower(%s)',
            (name,),
        ).fetchall()
        return {
            n for nargs, ndefaults in rows for n in range(nargs - ndefaults, nargs + 1)
        }

    def _execute_plsql(self, sql: str, binds: Sequence) -> Result:
        # A call that fails undoes only itself (#1632): the caller's open work
        # survives it, as it survives any other statement that fails, where a
        # rollback of the whole transaction took it too.
        self._conn.execute('SAVEPOINT _mirror_call')
        try:
            result = self._execute_plsql_call(sql, binds)
        except BaseException:
            # Unless the call ended the transaction itself, and the savepoint
            # with it.
            if self._conn.info.transaction_status in _OPEN_TRANSACTION:
                self._conn.execute('ROLLBACK TO SAVEPOINT _mirror_call')
                self._conn.execute('RELEASE SAVEPOINT _mirror_call')
            raise
        if self._conn.info.transaction_status in _OPEN_TRANSACTION:
            self._conn.execute('RELEASE SAVEPOINT _mirror_call')
        return result

    def _execute_plsql_call(self, sql: str, binds: Sequence) -> Result:
        # A callproc / callfunc block. `binds` is one BindVar per positional bind
        # (:1 → index 0), value None for a pure OUT. Run the underlying routine and
        # return every bind's value in order (input for IN, the routine's result
        # for OUT / IN OUT / the function return) — the Mirror marks them all OUT
        # and the client keeps only the positions it bound as a Var (#483/#503).
        if _TYPE_SHAPE_BLOCK.search(sql):
            return self._execute_type_shape(sql, binds)
        # An object or collection bind arrives as the image the client packed:
        # decoded against its type, as a statement's is (#1607). A collection
        # goes as its array: a CALL takes the parameter's type from the routine.
        given = [b.value if isinstance(b, BindVar) else b for b in binds]
        values = [
            v.value if isinstance(v, (BindVar, _CollectionBind)) else v
            for v in self._resolve_object_binds(binds)
        ]
        result = self._execute_plsql_values(sql, binds, values)
        # A bind that comes back as it went in goes back as the client sent it:
        # an object's image as the object it is, not the composite it was
        # decoded into -- every bind of a block goes back (#1607).
        out: list = []
        for at, value in enumerate(result.out_binds):
            sent = given[at] if at < len(values) and value is values[at] else None
            if isinstance(value, _PortalName):
                # The portal a client's cursor bound IN was opened as (#1634):
                # nothing to report, which the Mirror answers as the closed
                # cursor a routine leaves (#1048). As text it garbled the reply.
                out.append(None)
            elif isinstance(sent, ObjectImage):
                out.append(self._image_object(sent))
            elif at < len(values) and value is values[at]:
                out.append(given[at])
            else:
                out.append(value)
        return replace(result, out_binds=out)

    def _image_object(self, image: ObjectImage) -> object:
        # A bound object's or collection's image as its DbObject (#1607).
        oid = image.type_oid or b''
        if len(oid) >= 20:
            oid = oid[4:20]
        pg_oid = _pg_oid_of(oid)
        coll = self._collection_type(pg_oid) if pg_oid is not None else None
        if coll is not None:
            return coll.newobject(
                decode_collection_image(image.image, coll.element or {})
            )
        entry = self._object_type(pg_oid) if pg_oid is not None else None
        if entry is None:
            return None
        typ = entry[0]
        if typ.is_collection:
            (elements, keys) = decode_collection_keyed(
                image.image, typ.element or {}, AL32UTF8_CHARSET
            )
            return typ.newobject(elements, keys=keys)
        return DbObject(
            typ.name, decode_object_image(image.image, typ.attrs), dbtype=typ
        )

    def _execute_plsql_values(self, sql: str, binds: Sequence, values: list) -> Result:
        # _execute_plsql's work, with the binds' values decoded.
        inner = _CALL_BLOCK.match(sql)
        statement = inner.group(1) if inner else ''
        try:
            func = _FUNC_CALL.match(statement)
            if func is not None:
                return self._call_function(func, values, sql)
            proc = _PROC_CALL.match(statement)
            if proc is not None:
                return self._call_procedure(proc, values, sql, binds)
            # Not a call: a block that opens a REF CURSOR into a bind (#1456).
            if any(
                isinstance(b, BindVar) and b.tns_type == TNS_TYPE_REFCURSOR
                for b in binds
            ):
                return self._run_block(sql, binds)
            assignments = _parse_out_assignments(statement)
            if assignments is not None:
                return self._eval_out_assignments(statement, assignments, binds, values)
            select_into = _SELECT_INTO.match(statement)
            if select_into is not None:
                return self._select_into(select_into, values)
            if inner is not None:
                # A block wrapping DML (BEGIN INSERT/UPDATE/DELETE …(:x); END) —
                # unwrap and run the inner statement with the binds.
                return self._run_block_statement(statement, values)
            # Not a shape the call paths model: a block, its binds its locals
            # (#1459).
            if _ANON_BLOCK.match(sql):
                return self._run_block(sql, binds)
            # Anything else runs as it is (best effort) so a side-effecting
            # statement still executes.
            self._conn.cursor().execute(_translate_idioms(sql))
            return Result(out_binds=values)
        except psycopg.Error as exc:
            # Undone first (#1632): naming the error reads the catalog for the
            # routine's arities, which a failed transaction refuses.
            self._conn.execute('ROLLBACK TO SAVEPOINT _mirror_call')
            raise _backend_error(
                exc, original=sql, arities=self._routine_arities
            ) from exc

    def _call_function(self, match: 're.Match', values: list, block: str) -> Result:
        # BEGIN :r := name(:a, :b); END;  →  SELECT name(a, b); the result is the
        # function's return value, written back into the :r bind position.
        ret_ref, name, args = match.groups()
        refused = _call_argument_error(block, name, args, None)
        if refused is not None:
            raise refused
        arguments = _call_arguments(args)
        if arguments is None:
            arguments = [
                (None, _placeholder_ref(m)) for m in _CALL_PLACEHOLDER.finditer(args)
            ]
        slots = _bind_slots(block)
        arg_values = [values[_placeholder_slot(slots, ref)] for _name, ref in arguments]
        arg_values = self._index_table_arguments(name, arg_values)
        cursor = self._conn.cursor()
        cursor.execute(
            f'SELECT {name}({_call_placeholders(arguments)}) FROM {_PLSQL_CALL_MARK}',
            tuple(arg_values) or None,
        )
        row = _decode_row(cursor, cursor.fetchone(), self._tstz_oid)
        out = list(values)
        returned = row[0] if row else None
        if cursor.description and returned is not None:
            # A record or index-by table returned is the object a client reads
            # (#1607).
            returned = self._object_out(cursor.description[0].type_code, returned)
        out[_placeholder_slot(slots, _placeholder_key(ret_ref))] = returned
        return Result(out_binds=out)

    def _index_table_arguments(
        self, name: str, values: list, types: Sequence[int] | None = None
    ) -> list:
        # A classic PL/SQL array bound to an index-by table parameter -- a list,
        # as cursor.arrayvar binds one -- as the table's composite, keyed 1..N
        # as Oracle keys it (#1607). The parameter's type is the routine's: the
        # one routine of that name and arity, or one each overload agrees on.
        if not any(isinstance(v, list) for v in values):
            return values
        if types is None:
            schema, _dot, routine = name.lower().rpartition('.')
            candidates = self._conn.execute(
                'SELECT p.proargtypes::oid[] FROM pg_proc p JOIN pg_namespace n '
                'ON n.oid = p.pronamespace WHERE p.proname = %s '
                "AND (%s = '' OR n.nspname = %s) AND p.pronargs = %s",
                (routine, schema, schema, len(values)),
            ).fetchall()
        else:
            candidates = [(list(types),)]
        out = list(values)
        for at, value in enumerate(values):
            if not isinstance(value, list):
                continue
            kinds = {row[0][at] for row in candidates if row[0] and at < len(row[0])}
            if len(kinds) != 1:
                continue
            entry = self._object_type(next(iter(kinds)))
            if entry is not None and entry[0].collection_type == (
                COLLECTION_PLSQL_INDEX_TABLE
            ):
                out[at] = self._index_table_value(
                    entry, value, list(range(1, len(value) + 1))
                )
        return out

    def _object_out(
        self, type_oid: int, value: object, array: bool = False, toid: bytes = b''
    ) -> object:
        # An OUT or returned value of a record or index-by table type: the
        # object a client reads -- or, for a classic array bind, the table's
        # values in key order (#1607). Any other value as it is.
        if isinstance(value, (str, list)) and toid:
            # A collection the call returns (#1623) is built as the collection
            # the bind names: from its array's text when psycopg met the inner
            # collection's array type before it was registered, from the list
            # when it had been (a NULL bind of the type registers it, #1622).
            coll = self._collection_type(
                _pg_oid_of(toid[4:20] if len(toid) >= 20 else toid) or 0
            )
            if coll is not None:
                return self._db_collection(coll, value, type_oid)
        entry = (
            self._object_type(type_oid)
            if type_oid not in _BUILTIN_OIDS and type_oid != self._tstz_oid
            else None
        )
        if entry is None or not isinstance(value, (tuple, str)):
            return value
        if array and entry[0].is_collection and isinstance(value, tuple):
            return list(value[1] or [])
        return self._db_object(entry[0], entry[1], value)

    def _call_procedure(
        self, match: 're.Match', values: list, block: str, binds: Sequence = ()
    ) -> Result:
        # BEGIN name(:a, :b); END;  →  CALL name(a, b); the OUT / IN OUT arguments
        # come back as a result row, in parameter order, which we place onto their
        # bind positions.
        name, args = match.groups()
        arg_refs = [_placeholder_ref(m) for m in _CALL_PLACEHOLDER.finditer(args)]
        modes, argtypes, parameters, kind = self._proc_signature(name)
        refused = _call_argument_error(block, name, args, parameters or None)
        if refused is not None:
            raise refused
        if kind == 'f' and self._returns_a_value(name):
            # A function called as a procedure does not compile in Oracle:
            # PLS-00221 (#1497). A function that returns nothing -- orafce's
            # DBMS_OUTPUT, say -- is a procedure there, and runs as one below.
            at = max(block.lower().find(name.lower()), 0)
            raise BackendError(
                f'line {block.count(chr(10), 0, at) + 1}, column '
                f'{at - block.rfind(chr(10), 0, at)}:\nPLS-00221: '
                f"'{name.rsplit('.', 1)[-1].upper()}' is not a procedure or is "
                'undefined',
                ora_code=ORA_PLSQL_COMPILATION_ERROR,
                error_offset=at,
            )
        slots = _bind_slots(block)
        # Which parameter each argument binds: by position, or by name (#1377). A
        # named argument the signature cannot place, or an argument that is not a
        # plain bind, leaves the call positional, as it always was.
        # Literals or expressions among the arguments go in as written, where
        # they used to be dropped and the routine called without them (#1531).
        arguments: list[tuple[str | None, int | str | _CallLiteral]]
        arguments = _call_items(args)
        lowered = [p.lower() for p in parameters]
        if any(n is not None and n not in lowered for n, _ref in arguments):
            arguments = [(None, ref) for ref in arg_refs]
        bound = {
            (lowered.index(n) if n is not None else position): ref
            for position, (n, ref) in enumerate(arguments)
        }
        # A pure-OUT argument carries no input — pass an untyped NULL, not the
        # client's placeholder Var value: a REF CURSOR Var marshals to bytea, which
        # makes CALL's overload resolution miss the refcursor parameter (#518). IN
        # and IN OUT arguments pass their value.
        arg_values = [
            None
            if isinstance(ref, _CallLiteral)
            or (modes and param < len(modes) and modes[param] == 'o')
            else values[_placeholder_slot(slots, ref)]
            for param, ref in bound.items()
        ]
        arg_values = self._index_table_arguments(
            name,
            arg_values,
            [argtypes[param] if param < len(argtypes) else 0 for param in bound]
            if argtypes
            else None,
        )
        cursor = self._conn.cursor()
        if kind == 'f':
            # A routine PostgreSQL has as a function -- orafce's DBMS_OUTPUT, say
            # (#1410) -- which CALL refuses. SELECT it instead, with only the
            # arguments that carry an input: a function takes no OUT argument,
            # and returns its OUT values as the row a CALL would.
            given = [
                (argument, value)
                for (param, _ref), argument, value in zip(
                    bound.items(), arguments, arg_values
                )
                if not (modes and param < len(modes) and modes[param] == 'o')
            ]
            cursor.execute(
                f'SELECT * FROM {name}({_call_placeholders([a for a, _v in given])})',
                tuple(v for a, v in given if not isinstance(a[1], _CallLiteral))
                or None,
            )
        else:
            cursor.execute(
                f'CALL {name}({_call_placeholders(arguments)})',
                tuple(
                    v
                    for (_n, ref), v in zip(arguments, arg_values)
                    if not isinstance(ref, _CallLiteral)
                )
                or None,
            )
        # A procedure with no OUT or IN OUT parameter returns no row at all.
        returned = self._decode_out_row(
            cursor, cursor.fetchone() if cursor.description is not None else None
        )
        out = list(values)
        # The result row carries the OUT and IN OUT parameters in declaration
        # order, whatever order the call named them in; each goes to the bind its
        # argument used.
        outs = [param for param, mode in enumerate(modes or ()) if mode in ('o', 'b')]
        for value, param in zip(returned, outs):
            ref = bound.get(param)
            if ref is None or isinstance(ref, _CallLiteral):
                continue
            # An OUT INTERVAL YEAR TO MONTH arrives as an OraInterval (base
            # interval on the wire) — turn it into an IntervalYM by matching the
            # argument's declared ora_intervalym type, since a CALL result has no
            # table column to trace (#504).
            if (
                self._intervalym_oid is not None
                and param < len(argtypes)
                and argtypes[param] == self._intervalym_oid
            ):
                value = _to_interval_ym(value)
            elif param < len(argtypes) and value is not None:
                # A record or index-by table OUT (#1607): an object, or for a
                # classic array bind the table's values.
                slot = _placeholder_slot(slots, ref)
                bind = binds[slot] if slot < len(binds) else None
                value = self._object_out(
                    argtypes[param],
                    value,
                    array=isinstance(bind, BindVar) and bind.array_size > 0,
                )
            out[_placeholder_slot(slots, ref)] = value
        return Result(out_binds=out)

    def _decode_out_row(self, cursor, row) -> list:
        # Decode a routine's OUT-value row: an ora_tstz composite → an aware
        # datetime at its offset (#519); a refcursor portal → its rows drained into
        # a CursorResult for the REF CURSOR OUT bind (#518); anything else verbatim.
        if row is None:
            return []
        decoded: list = []
        for value, desc in zip(row, cursor.description or ()):
            if value is None:
                decoded.append(None)
            elif desc.type_code == _REFCURSOR_OID:
                decoded.append(self._drain_refcursor(value))
            else:
                decoded.append(_wire_cell(value, desc.type_code, self._tstz_oid))
        return decoded

    def _run_block(self, sql: str, binds: Sequence) -> Result:
        # An anonymous block of no shape the call paths model, with its binds: a
        # DECLARE block assigning one, a SELECT ... INTO or RETURNING ... INTO
        # one, a cursor opened into one (#1456, #1459). It runs as a DO block
        # whose locals stand for the binds, each initialised with the bind's
        # value; every bind comes back with the value its local ended with, a
        # REF CURSOR's portal drained into it as a routine's is (#518) -- None
        # for a cursor the block never opened.
        names = _bind_names(sql)
        locals_: dict[str, tuple[int, str, str]] = {}
        for slot, (name, bind) in enumerate(zip(names, binds)):
            pg_type = _block_bind_type(bind)
            value = bind.value if isinstance(bind, BindVar) else bind
            initial = (
                'NULL'
                if pg_type == 'refcursor' or value is None
                else psycopg.sql.Literal(value).as_string(self._conn)
            )
            if '$$' in initial:  # it would end the DO block's quoting
                raise UnsupportedFeature('a bind value holding $$ in a PL/SQL block')
            locals_[name] = (slot, pg_type, initial)
        self._conn.execute(
            _translate_idioms(_bind_block(sql, locals_, self._routine_kind))
        )
        values = [b.value if isinstance(b, BindVar) else b for b in binds]
        for slot, pg_type, _initial in locals_.values():
            row = self._conn.execute(
                'SELECT current_setting(%s, true)', (_BLOCK_BIND_SETTING.format(slot),)
            ).fetchone()
            text = row[0][1:] if row and row[0] else None
            if pg_type != 'refcursor':
                values[slot] = (
                    None if text is None else _block_bind_value(pg_type, text)
                )
            elif (
                text
                and self._conn.execute(
                    'SELECT 1 FROM pg_cursors WHERE name = %s', (text,)
                ).fetchone()
            ):
                values[slot] = self._drain_refcursor(text)
            else:
                values[slot] = None  # the block never opened it
        return Result(out_binds=values)

    def _drain_refcursor(self, portal: str) -> CursorResult:
        # A REF CURSOR OUT bind: the routine OPENed a portal, whose name the CALL
        # returned. Fetch all its rows (still inside this transaction) and hand them
        # back as a CursorResult the Mirror parks and serves as a nested cursor —
        # re-tagging any ora_tstz cells the same way the top-level read path does.
        fetch = self._conn.cursor()
        fetch.execute(sql.SQL('FETCH ALL FROM {}').format(sql.Identifier(portal)))
        rows = [list(r) for r in fetch.fetchall()]
        # Drained, so close it: an open portal holds its tables, and PostgreSQL
        # refuses DDL on them in this session ("being used by active queries").
        self._conn.execute(sql.SQL('CLOSE {}').format(sql.Identifier(portal)))
        for i, desc in enumerate(fetch.description or ()):
            if desc.type_code in (self._tstz_oid, _TIMESTAMPTZ_OID):
                for row in rows:
                    row[i] = _wire_cell(row[i], desc.type_code, self._tstz_oid)
            elif desc.type_code == _REFCURSOR_OID:
                # A nested CURSOR(...) item (#1461): its own portal, drained.
                for row in rows:
                    if row[i] is not None:
                        row[i] = self._drain_refcursor(row[i])
        columns = [
            _refcursor_column_meta(desc.name)
            if desc.type_code == _REFCURSOR_OID
            else _column_meta(desc, [r[i] for r in rows], self._tstz_oid)
            for i, desc in enumerate(fetch.description or ())
        ]
        # A column straight from a NOT NULL table column is not nullable, as a
        # query's is (#1618): a FETCH names the table column as a SELECT does.
        for i in self._not_null_columns(fetch.pgresult, len(columns)):
            columns[i] = replace(columns[i], null_ok=0)
        return CursorResult(columns=columns, rows=[tuple(r) for r in rows])

    def _eval_out_assignments(
        self, body: str, assignments: list, binds: Sequence, values: list
    ) -> Result:
        # BEGIN :a := <expr>; :b := <expr>; END — evaluate the right-hand sides
        # with one SELECT and place each result onto its bind position (#517).
        refs = _bind_names(body)
        # Bind the SELECT by NAME, against the block's own bind order. The SELECT
        # carries only the right-hand sides, so the OUT targets are gone from it
        # and its placeholders no longer line up with `values`, which is in the
        # BLOCK's order. Passing `values` straight through bound them by position
        # within the SELECT: for `:r := f(:p)` the block's order is [r, p], the
        # SELECT has only :p, and :p took position 0 -- r's value, which for a
        # pure OUT bind is None. So f received NULL whatever the caller passed,
        # and no error was raised (#1137).
        by_name = dict(zip(refs, values))
        # A classic PL/SQL array passed to a routine's index-by table parameter
        # -- python-oracledb's callfunc names its return :retval, so its call
        # comes this way -- goes as the table's composite (#1607).
        for _ref, expr in assignments:
            call = _PROC_CALL.match(expr)
            if call is None:
                continue
            items = [
                call.group(2)[a:b].strip()
                for a, b in _top_level_items(call.group(2), 0, len(call.group(2)))
            ]
            arg_refs = [i[1:].strip('"') for i in items if i.startswith(':')]
            if len(arg_refs) != len(items) or not any(
                isinstance(by_name.get(r), list) for r in arg_refs
            ):
                continue
            converted = self._index_table_arguments(
                call.group(1), [by_name.get(r) for r in arg_refs]
            )
            by_name.update(zip(arg_refs, converted))
        # A DATE or TIMESTAMP assigned to a TIMESTAMP WITH LOCAL TIME ZONE is read
        # in the session's zone, as Oracle converts it; the cast does that, and
        # leaves a value that is already an instant alone (#1240). The declared
        # type is on the bind, not on its value (#1245).
        declared = {
            ref: getattr(bind, 'tns_type', None) for ref, bind in zip(refs, binds)
        }
        exprs = ', '.join(
            f'CAST(({expr}) AS timestamptz)'
            if declared.get(ref) == TNS_TYPE_TIMESTAMPLTZ
            else expr
            for ref, expr in assignments
        )
        # A PL/SQL expression, so a NO_DATA_FOUND out of a function it calls
        # raises rather than turning NULL as in SQL (#1612).
        select = _translate_idioms(f'SELECT {exprs} FROM {_PLSQL_CALL_MARK}')
        sql, params = _translate_binds(
            select, [by_name[ref] for ref in _bind_names(select)]
        )
        cursor = self._conn.cursor()
        cursor.execute(sql, params)
        row = _decode_row(cursor, cursor.fetchone(), self._tstz_oid) or []
        out = list(values)
        for at, ((ref, _expr), result) in enumerate(zip(assignments, row)):
            if ref in refs:
                # An INTERVAL YEAR TO MONTH comes out of the SELECT as a plain
                # interval, which has no YEAR TO MONTH wire form; the bind's
                # declared type says which it is, as a procedure's parameter
                # type does (#504, #1400).
                if declared.get(ref) == TNS_TYPE_INTERVALYM and result is not None:
                    result = _to_interval_ym(result)
                elif result is not None and cursor.description:
                    # A record or index-by table returned is the object a client
                    # reads, or a classic array's values (#1607).
                    bind = (
                        binds[refs.index(ref)] if refs.index(ref) < len(binds) else None
                    )
                    result = self._object_out(
                        cursor.description[at].type_code,
                        result,
                        array=isinstance(bind, BindVar) and bind.array_size > 0,
                        toid=bind.toid if isinstance(bind, BindVar) else b'',
                    )
                out[refs.index(ref)] = result
        return Result(out_binds=out)

    def _select_into(self, match: 're.Match', values: list) -> Result:
        # BEGIN SELECT <cols> INTO :a, :b FROM ...; END (#1396): run the query with
        # its own binds and give its one row to the INTO binds, as PL/SQL does --
        # no row is ORA-01403 and more than one ORA-01422. The query goes through
        # execute(), so it is translated like any other (ROWID included).
        select, targets, rest = match.groups()
        refs = _bind_names(match.string)
        by_name = dict(zip(refs, values))
        query = f'{select} {rest}'
        result = self.execute(query, [by_name[ref] for ref in _bind_names(query)])
        if not result.rows:
            raise BackendError('no data found', ora_code=ORA_NO_DATA_FOUND)
        if len(result.rows) > 1:
            raise BackendError(
                'exact fetch returns more than requested number of rows',
                ora_code=ORA_TOO_MANY_ROWS,
            )
        out = list(values)
        for target, value in zip(_bind_names(targets), result.rows[0]):
            if target in refs:
                out[refs.index(target)] = value
        return Result(out_binds=out)

    def _run_block_statement(self, statement: str, values: list) -> Result:
        # A single DML statement unwrapped from a BEGIN … END block — run it with
        # the binds (#517). The input values keep the bind positions aligned.
        #
        # One with RETURNING ... INTO runs without the INTO, and the rows it
        # returns go to the INTO binds -- the trailing ones -- by PL/SQL's rule
        # for a single-row RETURNING: no row leaves them NULL, and more than one
        # is ORA-01422 (#1209).
        into = sorted(returning_bind_positions(statement, len(values)))
        if into:
            statement = strip_returning_into(statement)
        sql, params = _translate_binds(
            _translate_idioms(_translate_ddl(_quote_reserved_names(statement))),
            values,
        )
        cursor = self._conn.cursor()
        cursor.execute(sql, params)
        if not into:
            return Result(out_binds=values)
        rows = cursor.fetchall()
        if len(rows) > 1:
            raise BackendError(
                'exact fetch returns more than requested number of rows',
                ora_code=ORA_TOO_MANY_ROWS,
            )
        returned = _decode_row(cursor, rows[0], self._tstz_oid) if rows else None
        out = list(values)
        for i, position in enumerate(into):
            out[position] = returned[i] if returned is not None else None
        return Result(out_binds=out)

    def _collection_outs(self, result: Result, binds: Sequence) -> Result:
        """``result`` with each collection OUT value made an object of its type.

        A routine hands a collection back as a PostgreSQL array -- orafce's
        DBMS_OUTPUT.GET_LINES its lines as text[] -- and a client that bound a
        collection there, sqlplus binding DBMSOUTPUT_LINESARRAY (#1411), reads
        an object of that type: the bind carries the type's id.
        """
        out = list(result.out_binds)
        for at, bind in enumerate(binds):
            if (
                at < len(out)
                and isinstance(bind, BindVar)
                and bind.tns_type == TNS_TYPE_ADT
                and bind.toid
                and isinstance(out[at], list)
            ):
                pg_oid = _pg_oid_of(bind.toid)
                typ = self._collection_type(pg_oid) if pg_oid is not None else None
                if typ is not None:
                    out[at] = DbObject(typ.name, elements=out[at], dbtype=typ)
        return replace(result, out_binds=out)

    def _routine_exists(self, name: str) -> bool:
        """Whether PostgreSQL has a routine of this name, in the schema the name
        gives if it gives one."""
        schema, _dot, routine = name.lower().rpartition('.')
        row = self._conn.execute(
            'SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace '
            "WHERE p.proname = %s AND (%s = '' OR n.nspname = %s) LIMIT 1",
            (routine, schema, schema),
        ).fetchone()
        return row is not None

    def _bfile_path(self, directory: str, filename: str, operation: str) -> str:
        # The path of a BFILE's file on the PostgreSQL host (#1669): under its
        # DIRECTORY object's path, ORA-22285 when that object does not exist.
        row = self._conn.execute(
            'SELECT path FROM sys.ora_directories WHERE name = %s', (directory,)
        ).fetchone()
        if row is None:
            self._conn.rollback()
            raise BackendError(
                f'non-existent directory or file for {operation} operation',
                ora_code=ORA_NONEXISTENT_FILE,
            )
        return row[0].rstrip('/') + '/' + filename

    def _bfile_size(self, path: str) -> int | None:
        # The file's size, None for no such file. A missing file is a NULL
        # record; a present one's creation time is NULL on Linux, so a
        # whole-record test says neither.
        with self._rolled_back_on_error():
            row = self._conn.execute(
                'SELECT (pg_stat_file(%s, true)).size', (path,)
            ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def bfile_exists(self, directory: str, filename: str) -> bool:
        """Whether a BFILE's file exists (#1669): ORA-22285 when its DIRECTORY
        object does not, else whether the file is there under the directory's
        path, on the PostgreSQL host, as pg_stat_file finds it -- the server
        that reads it, as Oracle's does."""
        return (
            self._bfile_size(self._bfile_path(directory, filename, 'FILEEXISTS'))
            is not None
        )

    def bfile_length(self, directory: str, filename: str) -> int:
        """A BFILE's length in bytes (#1672), the file's on the PostgreSQL host."""
        size = self._bfile_size(self._bfile_path(directory, filename, 'GETLENGTH'))
        if size is None:
            raise BackendError(
                'non-existent directory or file for GETLENGTH operation',
                ora_code=ORA_NONEXISTENT_FILE,
            )
        return size

    def bfile_read(
        self, directory: str, filename: str, offset: int, amount: int
    ) -> bytes:
        """``amount`` bytes of a BFILE from ``offset`` (1-based), or the rest of
        the file for an amount of 0 or less (#1672), read on the PostgreSQL
        host through pg_read_binary_file."""
        path = self._bfile_path(directory, filename, 'READ')
        size = self._bfile_size(path)
        if size is None:
            raise BackendError(
                'non-existent directory or file for READ operation',
                ora_code=ORA_NONEXISTENT_FILE,
            )
        start = max(offset - 1, 0)
        count = size - start if amount <= 0 else min(amount, size - start)
        if count <= 0:
            return b''
        with self._rolled_back_on_error():
            row = self._conn.execute(
                'SELECT pg_read_binary_file(%s, %s, %s)', (path, start, count)
            ).fetchone()
        return bytes(row[0]) if row and row[0] is not None else b''

    def _rowid_columns(self, table: str) -> frozenset[str]:
        # The ROWID columns of a table a statement names (#1624), lower case.
        rows = self._conn.execute(
            'SELECT a.attname FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid '
            "WHERE a.attrelid = to_regclass(%s) AND t.typname = 'ora_rowid' "
            'AND NOT a.attisdropped',
            (table,),
        ).fetchall()
        return frozenset(name for (name,) in rows)

    def _rowid_number_error(self, sql: str, binds: Sequence) -> BackendError | None:
        """ORA-00932 for a number an INSERT ... VALUES or an UPDATE ... SET puts
        in a ROWID column (#1624), as Oracle refuses one. PostgreSQL converts
        it to its text, which the column's format check would only refuse as
        ORA-01410. A number bind or literal is caught here; one a query
        computes is left to that check."""
        if 'rowid' not in sql.lower() and not self._has_rowid_columns():
            return None
        (masked, _contents) = _mask_quoted(sql)
        insert = _ROWID_INSERT.match(masked)
        update = None if insert else _ROWID_UPDATE.match(masked)
        if insert is None and update is None:
            return None
        if insert is not None:
            table = insert.group(1)
            names = [
                masked[a:b].strip()
                for a, b in _top_level_items(masked, insert.start(2), insert.end(2))
            ]
            close = _matching_paren(masked, insert.end() - 1)
            values = [
                masked[a:b].strip()
                for a, b in _top_level_items(masked, insert.end(), close)
            ]
            pairs = list(zip(names, values))
        else:
            assert update is not None
            table = update.group(1)
            pairs = []
            for a, b in _top_level_items(masked, update.end(), len(masked)):
                (column, eq, value) = masked[a:b].partition('=')
                if eq:
                    pairs.append(
                        (column.strip(), re.split(r'(?i)\bWHERE\b', value)[0].strip())
                    )
        rowid_columns = self._rowid_columns(_pg_name(table))
        if not rowid_columns:
            return None
        bind_values = dict(zip(_bind_names(sql), binds))
        for column, value in pairs:
            last = re.split(r'\s*\.\s*', column)[-1]
            if (
                last[1:-1] if last.startswith('"') else last.lower()
            ) not in rowid_columns:
                continue
            ref = _BIND_REF.fullmatch(value)
            given = bind_values.get(_bind_name(ref)) if ref is not None else None
            number = (
                isinstance(given, (int, float, decimal.Decimal))
                and not isinstance(given, bool)
            ) or (ref is None and _NUMBER_LITERAL.fullmatch(value) is not None)
            if number:
                return BackendError(
                    'inconsistent datatypes: expected ROWID got NUMBER',
                    ora_code=ORA_INCONSISTENT_DATATYPES,
                )
        return None

    def _has_rowid_columns(self) -> bool:
        # Whether any table has a ROWID column at all (#1624), so a statement
        # into none costs no lookup. Read at once: ROWID columns are rare.
        row = self._conn.execute(
            'SELECT EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_type t '
            "ON t.oid = a.atttypid WHERE t.typname = 'ora_rowid' AND NOT a.attisdropped)"
        ).fetchone()
        return bool(row and row[0])

    def _package_functions(self) -> dict[str, frozenset[str]]:
        # Each package's functions that take no argument a call must give
        # (#1651): package name -> function names, all lower case.
        if self._package_function_cache is None:
            found: dict[str, set[str]] = {}
            if self._has_package_catalog:
                for package, name in self._conn.execute(
                    'SELECT k.name, p.proname FROM sys.ora_packages k '
                    'JOIN pg_namespace n ON n.nspname = k.name '
                    'JOIN pg_proc p ON p.pronamespace = n.oid '
                    "WHERE p.prokind = 'f' AND p.pronargs = p.pronargdefaults"
                ).fetchall():
                    found.setdefault(package, set()).add(name)
            self._package_function_cache = {
                package: frozenset(names) for package, names in found.items()
            }
        return self._package_function_cache

    def _call_package_functions(self, sql: str) -> str:
        """A package's function named without parentheses as the call it is
        (#1651): ``pkg.f`` as ``pkg.f()``, as Oracle calls a function with no
        arguments. PostgreSQL read it as column ``f`` of a table ``pkg``. Oracle
        looks for a table or an alias first, so a statement that names ``pkg``
        anywhere but before a dot -- ``FROM t pkg`` -- is left alone."""
        if '.' not in sql:
            return sql
        (masked, contents) = _mask_quoted(sql)
        lowered = masked.lower()
        packages = {
            p: names for p, names in self._package_functions().items() if p in lowered
        }
        if not packages:
            return sql
        out = masked
        for package, names in packages.items():
            alone = re.compile(
                rf'(?<![\w$#."]){re.escape(package)}(?![\w$#])(?!\s*\.)', re.I
            )
            if alone.search(out):
                continue  # a table or an alias of that name: Oracle's first look
            call = re.compile(
                rf'(?<![\w$#."])({re.escape(package)}\s*\.\s*([A-Za-z_][\w$#]*))'
                rf'(?![\w$#])(?!\s*[(.])',
                re.IGNORECASE,
            )
            out = call.sub(
                lambda m: (
                    f'{m.group(1)}()' if m.group(2).lower() in names else m.group(0)
                ),
                out,
            )
        if out == masked:
            return sql
        return _unmask_quoted(out, contents)

    def _user_routine_names(self) -> frozenset[str]:
        # The names of the routines a user created (#1612): every function or
        # procedure but PostgreSQL's, the extensions' (orafce) and the backend's
        # own sys schema -- what a function's call of one could pass a
        # NO_DATA_FOUND up through. Read when a routine is created.
        rows = self._conn.execute(
            'SELECT DISTINCT p.proname FROM pg_proc p JOIN pg_namespace n '
            'ON n.oid = p.pronamespace '
            "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'sys', "
            "'oracle', 'pg_toast') AND n.nspname NOT LIKE 'pg\\_temp%%' "
            "AND p.prokind IN ('f', 'p') AND NOT EXISTS (SELECT 1 FROM pg_depend d "
            "WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid "
            "AND d.deptype = 'e')"
        ).fetchall()
        return frozenset(name for (name,) in rows)

    def _routine_kind(self, name: str) -> str | None:
        """What PostgreSQL has under a routine name -- 'f' a function, 'p' a
        procedure -- in the schema the name gives if it gives one; None for
        nothing (#1533)."""
        schema, _dot, routine = name.lower().rpartition('.')
        row = self._conn.execute(
            'SELECT p.prokind FROM pg_proc p JOIN pg_namespace n '
            'ON n.oid = p.pronamespace '
            "WHERE p.proname = %s AND (%s = '' OR n.nspname = %s) "
            "AND p.prokind IN ('f', 'p') LIMIT 1",
            (routine, schema, schema),
        ).fetchone()
        return row[0] if row else None

    def _proc_signature(self, name: str) -> tuple[list | None, list, list[str], str]:
        # A routine's parameter modes ('i' IN, 'o' OUT, 'b' IN OUT), the aligned
        # argument type oids, the parameter names -- what a call's argument list
        # can name (#1368) -- and its kind ('p' procedure, 'f' function: orafce
        # has DBMS_OUTPUT's routines as functions, #1410), from one pg_proc row,
        # in the schema the call names if it names one. Modes place a CALL's result row
        # (which carries only the OUT / IN OUT values) back onto the right bind
        # positions; the types let the OUT-bind path spot an ora_intervalym argument
        # (which has no result column to trace). Modes are None for an all-IN routine
        # (PostgreSQL leaves proargmodes — and proallargtypes — NULL then).
        schema, _dot, routine = name.lower().rpartition('.')
        row = self._conn.execute(
            'SELECT p.proargmodes, p.proallargtypes, p.proargnames, p.prokind '
            'FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace '
            "WHERE p.proname = %s AND (%s = '' OR n.nspname = %s) "
            'ORDER BY p.oid DESC LIMIT 1',
            (routine, schema, schema),
        ).fetchone()
        if not row:
            return None, [], [], 'p'
        names = list(row[2] or ())
        if not row[0]:
            return None, [], names, row[3]
        return list(row[0]), list(row[1] or ()), names, row[3]

    def change_password(
        self, username: str, old_password: str, new_password: str
    ) -> None:
        # The Mirror's client auth (the credential map) is separate from the
        # backend's PostgreSQL connection (a fixed conninfo), so a password change
        # updates only the map — a fresh Mirror session then authenticates with
        # the new password and the old one is rejected — without touching a
        # PostgreSQL role (which would break the backend's own conninfo). Oracle
        # validates the old password (ALTER USER … REPLACE); do the same against
        # the stored secret (#515). The map is shared across sessions.
        current = credential_lookup(self._credentials, username)
        if current is not None and old_password != current:
            raise BackendError(
                'invalid username/password; logon denied',
                ora_code=ORA_INVALID_USERNAME_PASSWORD,
            )
        # Oracle takes a password of at most 1024 bytes (12.2+; 11g far less).
        # Past that, the 1500-character change a client may try draws ORA-01017
        # from 11g and 23ai alike, so the Mirror answers the same, rather than
        # storing a password no Oracle would (#1266).
        if len(new_password.encode('utf-8')) > _MAX_PASSWORD_BYTES:
            raise BackendError(
                'invalid username/password; logon denied',
                ora_code=ORA_INVALID_USERNAME_PASSWORD,
            )
        for name in list(self._credentials):
            if name.upper() == username.upper():
                self._credentials[name] = new_password
                return
        self._credentials[username.upper()] = new_password

    # The end-to-end tracing attributes a client sets over the 12c piggyback
    # (#183). Oracle keeps them on the session and reports them through
    # SYS_CONTEXT('USERENV', ...); PostgreSQL's equivalent of session-scoped
    # state is a customised option, so each lands in `seerdb.<name>` and
    # sys_context (above) reads it back.
    # dbop names the operation V$SQL_MONITOR reports (#1619).
    _END_TO_END_SETTINGS = (
        'client_identifier',
        'module',
        'action',
        'client_info',
        'dbop',
    )

    @_while_connected
    def set_app_context(self, entries: list[tuple[str, str, str]]) -> None:
        """Keep the application context the client declared at login
        (`connect(appcontext=[(namespace, attribute, value), ...])`), which
        SYS_CONTEXT(namespace, attribute) reads back (#1581).

        In a session setting, as the service name is, committed at once: a
        setting made in a transaction that rolls back is undone with it.
        """
        context = {
            f'{namespace.upper()}.{attribute.upper()}': value
            for namespace, attribute, value in entries
        }
        try:
            self._conn.execute(
                "SELECT set_config('seerdb.app_context', %s, false)",
                (json.dumps(context),),
            )
            self._conn.commit()
        except psycopg.Error:
            self._conn.rollback()

    def set_end_to_end(self, attrs: dict) -> None:
        """Record the session's tracing attributes (#183).

        Called by the Mirror when the client sends the tracing piggyback. Only
        the attributes the client actually sent are touched: the piggyback marks
        each one modified or not, and an unmodified attribute keeps its value
        rather than being cleared.

        A cleared attribute is stored as the empty string rather than removed --
        there is no "unset" for a customised option within a session, and
        sys_context maps '' back to NULL, which is what Oracle reports.
        """
        with self._conn.cursor() as cur:
            for name in self._END_TO_END_SETTINGS:
                if name not in attrs:
                    continue
                value = attrs[name]
                cur.execute(
                    'SELECT set_config(%s, %s, false)',
                    (f'seerdb.{name}', '' if value is None else value),
                )

    @_while_connected
    def alter_session(self, statement: str) -> None:
        """Run the ALTER SESSION a client sent with its login.

        A 12.1+ client pins the session time zone to its own UTC offset this
        way. It is committed at once: a PostgreSQL SET made inside a transaction
        is undone if that transaction rolls back, and Oracle's is not.
        """
        with self._conn.cursor() as cur:
            cur.execute(_translate_admin(statement))
        self._conn.commit()

    @_while_connected
    def commit(self) -> None:
        self._conn.commit()
        self._user_savepoint = False

    @_while_connected
    def rollback(self) -> None:
        self._conn.rollback()
        self._user_savepoint = False

    def close(self) -> None:
        # What the translation report has not written yet goes out first (#1557).
        self._flush_report()
        if self._report_conn is not None:
            self._report_conn.close()
        self._conn.close()
