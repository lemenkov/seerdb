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
OCI_E2E_MAX_VALUES = 9  # client_identifier, module, action, ... (thin's slots)


def strip_oci_e2e_piggyback(body: bytes) -> bytes | None:
    """The real call behind an OCI end-to-end tracing piggyback, or ``None``.

    ``None`` means "this does not walk", and the caller must refuse the message
    rather than guess at it. See the note above for why the check matters more
    than the layout.
    """
    at = OCI_E2E_VALUES_OFF
    for _ in range(OCI_E2E_MAX_VALUES + 1):
        if at >= len(body):
            return None
        if body[at] == TTI_FUN:
            # Landed on the call, and there is a function code behind it.
            return body[at:] if at + 1 < len(body) else None
        at += 1 + body[at]  # a length-prefixed attribute value
    return None
