# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Shared constants for the classic sqlplus / thick-OCI (``deadbeef``) dialect.

Past the handshake, sqlplus and the OCI client marshal TTC differently from a
thin client (python-oracledb / seerdb): fixed little-endian ub4 lengths, an
8-byte pointer indicator in place of thin's ``0x01`` marker, and an OCI OER
return-status token on every reply (PROTOCOL.md §4.1.2, §36). The Mirror
(``seerdb/server/``) already speaks this dialect to answer a real sqlplus; these
are the low-level **wire constants both sides of that dialect share**, gathered
here so a future thick-OCI *client* and the Mirror use one definition rather than
each redefining them.

This module holds only the shared vocabulary — the fixed markers, field sizes,
and status/kind codes. The Mirror's captured 11.2 *identity* (banners, capability
vectors, OER templates) and its response-generation policy (SQL-verb → command
type maps) stay server-side: a client parses whatever the real server sends and
never emits those.
"""

from seerdb.common.tns_consts import TTI_80SES, TTI_FUN, TTI_MSG_TYPE_PIGGYBACK

# The 8-byte pointer indicator the OCI dialect uses where a thin client writes a
# single ``0x01`` marker byte: 0xFFFFFFFFFFFFFFFE, little-endian. It flags a
# present pointer field (e.g. the SQL text pointer in OALL8, the username pointer
# in OSESSKEY/AUTH); it is absent on a re-execute that reuses the cursor.
OCI_INDICATOR = b'\xfe\xff\xff\xff\xff\xff\xff\xff'


def pointer_present(field: bytes) -> bool:
    """Whether an 8-byte OCI pointer field says its value is present.

    From 11.2 sqlplus writes :data:`OCI_INDICATOR` there. sqlplus 10.2 writes the
    client's own 64-bit address (``80 a8 ba e2 fd 7f 00 00``) in the same field,
    so what a server can rely on is only that the field is not zero (#1764).
    """
    return len(field) == 8 and field != bytes(8)


# The TTI_80SES piggyback prefix: a thick-OCI call wrapped in a session-switch
# preamble. The first thing sqlplus sends after login, its version call, arrives
# this way (the server answers with its banner and sqlplus prints "Connected
# to: <banner>"), and so does a changepassword; is_version_call_oci tells them
# apart by the wrapped function.
OCI_PIGGYBACK_80SES = bytes([TTI_MSG_TYPE_PIGGYBACK, TTI_80SES])

# O5LOGON field sizes on the OCI dialect wire, as ASCII-hex lengths. The session
# key is a 48-byte value (96 hex); the AUTH_VFR_DATA salt is a 10-byte value
# (20 hex) — half of thin's 16-byte salt (PROTOCOL.md §4.1.2).
OCI_SESSKEY_HEXLEN = 96
OCI_SALT_HEXLEN = 20

# OCI OER return-status token fields (PROTOCOL.md §36). Every OCI reply ends with
# an OER envelope; these are the status code and the row-kind tag that vary within
# it. `status` says success vs error; `row_kind` distinguishes an ordinary row
# from one that carried a LOB locator or an inline LONG stream.
OCI_OER_STATUS_SUCCESS = 0x01
OCI_OER_STATUS_ERROR = 0x05
OCI_OER_ROW_KIND_NONE = 0x00
OCI_OER_ROW_KIND_LOB = 0x01
OCI_OER_ROW_KIND_LONG = 0x02

# V$SQL COMMAND_TYPE codes — the statement-kind vocabulary Oracle reports in an
# execute reply's OER (PROTOCOL.md §36); sqlplus renders its completion line
# ("Table created.", "5 rows updated.") purely from this field. A thick-OCI
# client reads the same codes back off a reply, so the vocabulary is shared; the
# Mirror's SQL-verb → code mapping (which of these a given statement produces) is
# response-generation policy and lives with the rest of the codec in common/tns.py. All confirmed
# live against sqlplus 11.2.
OCI_CMD_CREATE_TABLE = 1
OCI_CMD_INSERT = 2
OCI_CMD_SELECT = 3
OCI_CMD_UPDATE = 6
OCI_CMD_DELETE = 7
OCI_CMD_CREATE_INDEX = 9
OCI_CMD_DROP_INDEX = 10
OCI_CMD_ALTER_INDEX = 11
OCI_CMD_DROP_TABLE = 12
OCI_CMD_CREATE_SEQUENCE = 13
OCI_CMD_ALTER_SEQUENCE = 14
OCI_CMD_ALTER_TABLE = 15
OCI_CMD_DROP_SEQUENCE = 16
OCI_CMD_GRANT = 17
OCI_CMD_REVOKE = 18
OCI_CMD_CREATE_SYNONYM = 19
OCI_CMD_DROP_SYNONYM = 20
OCI_CMD_CREATE_VIEW = 21
OCI_CMD_DROP_VIEW = 22
OCI_CMD_LOCK_TABLE = 26
OCI_CMD_COMMIT = 44  # bare COMMIT typed as a statement (not OCITransCommit)
OCI_CMD_ROLLBACK = 45  # bare ROLLBACK typed as a statement
OCI_CMD_PLSQL = 47  # anonymous PL/SQL block (EXEC / OUT-bind reply)
OCI_CMD_TRUNCATE_TABLE = 85

# Every statement kind and its COMMAND_TYPE, as V$SQLCOMMAND lists them on a live
# 23ai (238 rows; the codes are stable across releases, an older one lists a
# prefix of them). A name is the statement's own leading words with its
# modifiers left out ("CREATE PACKAGE BODY", "ALTER SESSION", "COMMENT"), so a
# statement resolves by its longest leading phrase that is a name; GRANT, REVOKE,
# AUDIT and NOAUDIT are listed as "<verb> OBJECT" (#2012).
_SQL_COMMANDS = """
1 CREATE TABLE
2 INSERT
3 SELECT
4 CREATE CLUSTER
5 ALTER CLUSTER
6 UPDATE
7 DELETE
8 DROP CLUSTER
9 CREATE INDEX
10 DROP INDEX
11 ALTER INDEX
12 DROP TABLE
13 CREATE SEQUENCE
14 ALTER SEQUENCE
15 ALTER TABLE
16 DROP SEQUENCE
17 GRANT OBJECT
18 REVOKE OBJECT
19 CREATE SYNONYM
20 DROP SYNONYM
21 CREATE VIEW
22 DROP VIEW
23 VALIDATE INDEX
24 CREATE PROCEDURE
25 ALTER PROCEDURE
26 LOCK TABLE
27 NO-OP
28 RENAME
29 COMMENT
30 AUDIT OBJECT
31 NOAUDIT OBJECT
32 CREATE DATABASE LINK
33 DROP DATABASE LINK
34 CREATE DATABASE
35 ALTER DATABASE
36 CREATE ROLLBACK SEGMENT
37 ALTER ROLLBACK SEGMENT
38 DROP ROLLBACK SEGMENT
39 CREATE TABLESPACE
40 ALTER TABLESPACE
41 DROP TABLESPACE
42 ALTER SESSION
43 ALTER USER
44 COMMIT
45 ROLLBACK
46 SAVEPOINT
47 PL/SQL EXECUTE
48 SET TRANSACTION
49 ALTER SYSTEM
50 EXPLAIN
51 CREATE USER
52 CREATE ROLE
53 DROP USER
54 DROP ROLE
55 SET ROLE
56 CREATE SCHEMA
57 CREATE CONTROL FILE
58 ALTER TRACING
59 CREATE TRIGGER
60 ALTER TRIGGER
61 DROP TRIGGER
62 ANALYZE TABLE
63 ANALYZE INDEX
64 ANALYZE CLUSTER
65 CREATE PROFILE
66 DROP PROFILE
67 ALTER PROFILE
68 DROP PROCEDURE
70 ALTER RESOURCE COST
71 CREATE MATERIALIZED VIEW LOG
72 ALTER MATERIALIZED VIEW LOG
73 DROP MATERIALIZED VIEW LOG
74 CREATE MATERIALIZED VIEW
75 ALTER MATERIALIZED VIEW
76 DROP MATERIALIZED VIEW
77 CREATE TYPE
78 DROP TYPE
79 ALTER ROLE
80 ALTER TYPE
81 CREATE TYPE BODY
82 ALTER TYPE BODY
83 DROP TYPE BODY
84 DROP LIBRARY
85 TRUNCATE TABLE
86 TRUNCATE CLUSTER
87 CREATE BITMAPFILE
88 ALTER VIEW
89 DROP BITMAPFILE
90 SET CONSTRAINTS
91 CREATE FUNCTION
92 ALTER FUNCTION
93 DROP FUNCTION
94 CREATE PACKAGE
95 ALTER PACKAGE
96 DROP PACKAGE
97 CREATE PACKAGE BODY
98 ALTER PACKAGE BODY
99 DROP PACKAGE BODY
130 ALTER MINING MODEL
133 CREATE MINING MODEL
157 CREATE DIRECTORY
158 DROP DIRECTORY
159 CREATE LIBRARY
160 CREATE JAVA
161 ALTER JAVA
162 DROP JAVA
163 CREATE OPERATOR
164 CREATE INDEXTYPE
165 DROP INDEXTYPE
166 ALTER INDEXTYPE
167 DROP OPERATOR
168 ASSOCIATE STATISTICS
169 DISASSOCIATE STATISTICS
170 CALL METHOD
171 CREATE SUMMARY
172 ALTER SUMMARY
173 DROP SUMMARY
174 CREATE DIMENSION
175 ALTER DIMENSION
176 DROP DIMENSION
177 CREATE CONTEXT
178 DROP CONTEXT
179 ALTER OUTLINE
180 CREATE OUTLINE
181 DROP OUTLINE
182 UPDATE INDEXES
183 ALTER OPERATOR
187 CREATE SPFILE
188 CREATE PFILE
189 UPSERT
190 CHANGE PASSWORD
191 UPDATE JOIN INDEX
192 ALTER SYNONYM
193 ALTER DISK GROUP
194 CREATE DISK GROUP
195 DROP DISK GROUP
196 ALTER LIBRARY
197 PURGE RECYCLEBIN
198 PURGE DBA RECYCLEBIN
199 PURGE TABLESPACE
200 PURGE TABLE
201 PURGE INDEX
202 UNDROP OBJECT
203 DROP DATABASE
204 FLASHBACK DATABASE
205 FLASHBACK TABLE
206 CREATE RESTORE POINT
207 DROP RESTORE POINT
209 DECLARE REWRITE EQUIVALENCE
210 ALTER REWRITE EQUIVALENCE
211 DROP REWRITE EQUIVALENCE
212 CREATE EDITION
213 ALTER EDITION
214 DROP EDITION
215 DROP ASSEMBLY
216 CREATE ASSEMBLY
217 ALTER ASSEMBLY
218 CREATE FLASHBACK ARCHIVE
219 ALTER FLASHBACK ARCHIVE
220 DROP FLASHBACK ARCHIVE
222 CREATE SCHEMA SYNONYM
224 DROP SCHEMA SYNONYM
225 ALTER DATABASE LINK
226 CREATE PLUGGABLE DATABASE
227 ALTER PLUGGABLE DATABASE
228 DROP PLUGGABLE DATABASE
229 CREATE AUDIT POLICY
230 ALTER AUDIT POLICY
231 DROP AUDIT POLICY
234 CREATE LOCKDOWN PROFILE
235 DROP LOCKDOWN PROFILE
236 ALTER LOCKDOWN PROFILE
238 ADMINISTER KEY MANAGEMENT
239 CREATE MATERIALIZED ZONEMAP
240 ALTER MATERIALIZED ZONEMAP
241 DROP MATERIALIZED ZONEMAP
242 DROP MINING MODEL
243 CREATE ATTRIBUTE DIMENSION
244 ALTER ATTRIBUTE DIMENSION
245 DROP ATTRIBUTE DIMENSION
246 CREATE HIERARCHY
247 ALTER HIERARCHY
248 DROP HIERARCHY
249 CREATE ANALYTIC VIEW
250 ALTER ANALYTIC VIEW
251 DROP ANALYTIC VIEW
252 ALTER DATABASE DICTIONARY
253 CREATE INMEMORY JOIN GROUP
254 ALTER INMEMORY JOIN GROUP
255 DROP INMEMORY JOIN GROUP
260 CREATE BLOCKCHAIN
261 ALTER BLOCKCHAIN
262 DROP BLOCKCHAIN
263 CREATE LOGICAL PARTITION TRACKING
264 ALTER LOGICAL PARTITION TRACKING
265 DROP LOGICAL PARTITION TRACKING
266 CREATE MLE MODULE
267 DROP MLE MODULE
268 CREATE MLE ENV
269 DROP MLE ENV
270 CREATE LANGUAGE
271 DROP LANGUAGE
272 LOAD
273 ANALYZE STORAGEPOOL
275 CREATE PROPERTY GRAPH
276 ALTER PROPERTY GRAPH
277 DROP PROPERTY GRAPH
278 CREATE TRUE CACHE
279 ALTER MLE MODULE
280 ALTER MLE ENV
281 CREATE DOMAIN
282 ALTER DOMAIN
283 DROP DOMAIN
285 DESCRIBE
287 CREATE DIRECTIVE
288 ALTER DIRECTIVE
289 DROP DIRECTIVE
290 CREATE ASSERTION
291 ALTER ASSERTION
292 DROP ASSERTION
304 CREATE END USER
305 ALTER END USER
306 DROP END USER
307 CREATE DATA ROLE
308 CREATE END USER CONTEXT
309 DROP DATA ROLE
310 GRANT DATA ROLE
311 REVOKE DATA ROLE
312 DROP END USER CONTEXT
313 CREATE POLICY SET
314 DROP POLICY SET
315 CREATE DATA GRANT
316 DROP DATA GRANT
317 CREATE DATA PRIVILEGE
318 DROP DATA PRIVILEGE
319 CREATE APPLICATION IDENTITY
320 DROP APPLICATION IDENTITY
324 SET USE DATA GRANTS ONLY
"""
SQL_COMMAND_TYPES: dict[str, int] = {
    name: int(code)
    for (code, name) in (
        line.split(' ', 1) for line in _SQL_COMMANDS.strip().splitlines()
    )
}

# The end-to-end tracing piggyback (func TTI_SCID = 135) in the OCI dialect
# (#825). Modern sqlplus sends it once, immediately after login, carrying its
# module name -- and it is a PREFIX: a real call follows in the same message.
#
# Only ONE live sample exists (sqlplus 23.26 at field version 6, module
# "SQL*Plus"), and several field layouts fit it, so the block is NOT decoded
# here. What the sample does establish is its shape: a fixed head whose
# marshalled fields include client pointer values -- `fe ff ff ff ff ff ff ff`
# (OCI_INDICATOR) at offset 43 and a heap address at 59, which vary per run --
# followed by the attribute values as length-prefixed strings, and then the
# call. Offset 139 is where the values begin in that sample, and 0x18
# (MODULE | ACTION, exactly what sqlplus sets) sits at offset 19.
#
# So the walker below reads the values and then CHECKS ITS OWN ANSWER: it must
# land on a TTI_FUN byte, or it reports failure and the caller refuses the
# message. A wrong offset therefore costs a legible ORA-03115 and a session that
# stays usable -- never a desynchronised stream, which is the failure mode that
# guessing a layout invites.
OCI_E2E_VALUES_OFF = 139
# At the 12c band the head is 16 bytes longer, so the values begin 16 bytes
# later (sqlplus 23.26 against a live 18c; the same walk lands on the call).
OCI_E2E_VALUES_OFF_12C = 155
OCI_E2E_MAX_VALUES = 9  # client_identifier, module, action, ... (thin's slots)


def strip_oci_e2e_piggyback(
    body: bytes, values_off: int = OCI_E2E_VALUES_OFF
) -> bytes | None:
    """The real call behind an OCI end-to-end tracing piggyback, or ``None``.

    ``None`` means "this does not walk", and the caller must refuse the message
    rather than guess at it. See the note above for why the check matters more
    than the layout. ``values_off`` is where the values begin in the session's
    layout.
    """
    at = values_off
    for _ in range(OCI_E2E_MAX_VALUES + 1):
        if at >= len(body):
            return None
        if body[at] == TTI_FUN:
            # Landed on the call, and there is a function code behind it.
            return body[at:] if at + 1 < len(body) else None
        at += 1 + body[at]  # a length-prefixed attribute value
    return None
