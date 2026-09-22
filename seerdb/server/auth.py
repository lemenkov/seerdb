# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Server-side O5LOGON (11g, 192-bit salted path).

The encode side of the client crypto in :mod:`seerdb.common.crypto` (``o5logon`` /
``validate``). O5LOGON is *mutually* authenticated, so the server must hold the
account password — supplied by ``backend.authenticate(user)`` (auth lives with
the backend). The flow the server drives:

1. **Challenge** (:func:`make_challenge`): pick a salt and a server session key,
   derive ``key_sess = SHA1(password + salt) + 0x00000000``, and send
   ``AUTH_SESSKEY = AES-CBC(server_session, key_sess)`` with the salt
   (``AUTH_VFR_DATA``).
2. The client derives the same ``key_sess`` from the password it typed, recovers
   the server session key, mints its own session key, and returns it (its
   ``AUTH_SESSKEY``) plus ``AUTH_PASSWORD``.
3. **Derive** (:func:`derive_conn_key`): recover the client session key and
   combine both halves into the session ``ConnKey`` — identical to the one the
   client computed *only if the passwords match*.
4. **Verify** (:func:`verify_password`): decrypt the client's ``AUTH_PASSWORD``
   under the ConnKey and confirm it proves the account password — the server
   half of the mutual auth, so a wrong password is rejected (ORA-01017) rather
   than served.
5. **Prove** (:func:`server_proof`): return ``AES-CBC(SERVER_TO_CLIENT, ConnKey)``
   — the token the client's ``validate()`` decrypts and checks, closing the
   mutual authentication.

This module is the crypto core; the RPA wire encode/parse that carries these
values is layered on top separately.
"""

from __future__ import annotations

import contextvars
import struct
from binascii import unhexlify
from secrets import token_bytes

from Crypto.Cipher import AES

from seerdb.common import oci
from seerdb.common.crypto import (
    O5LOGON_IV,
    PBKDF2_SDER_COUNT,
    VFR_11G_SHA1,
    cat_key,
    conn_key,
    decrypt_password,
    key_sess_11g,
    pad2,
    server_proof_padded,
)
from seerdb.common.exceptions import InterfaceError
from seerdb.common.tns import (
    _CHALLENGE_TRAILER,
    _DECODE_FIELD_VERSION,
    _RESULT_TRAILER,
    AUTH_GLOBALLY_UNIQUE_DBID,
    Challenge,
    _hexval,
    decode_dalc,
    decode_kv,
    decode_ub4,
)
from seerdb.common.tns_consts import (
    FIELD_VERSION_11_2,
    FIELD_VERSION_12_1,
    FIELD_VERSION_12_2,
    FIELD_VERSION_23_1,
    TTI_AUTH,
    TTI_FUN,
    TTI_RPA,
    TTI_SESS,
)
from seerdb.server.identity import IDENTITY_11_2, ServerIdentity

# 11g accounts carry the SHA1 verifier → the 192-bit AES key schedule.
_BITS_11G = 192
# A server session key is 40 random bytes + an 8-byte pad2 tail, so the client
# recognises it and mints a matching 48-byte session key (see crypto.o5logon0).
_SERVER_SESSION_LEN = 40
# AUTH_PBKDF2_CSK_SALT is 16 bytes on a live 21c (32 hex on the wire).
_DERIVED_SALT_LEN = 16
# The modern session key is 32 bytes, and its LENGTH is what selects the
# derivation -- not the presence of the salt. python-oracledb branches on
# `len(session_key_part_a) == 48` and takes the 11g XOR+MD5 path when it matches,
# ignoring AUTH_PBKDF2_CSK_SALT entirely; a live 21c sends 32. Sending 48
# alongside a CSK salt therefore makes the client derive an 11g key while the
# server derives a PBKDF2 one, and the login fails with both sides believing
# they were right (#829).
_MODERN_SESSION_LEN = 32


def make_challenge(
    password: bytes,
    *,
    salt: bytes | None = None,
    server_session: bytes | None = None,
    derived_salt: bytes | None = None,
    field_version: int = FIELD_VERSION_11_2,
) -> Challenge:
    """Build the O5LOGON challenge for an account whose password is known.

    From field version 12.1 the challenge also carries ``AUTH_PBKDF2_CSK_SALT``
    and the two iteration counts, and both sides derive the session key through
    PBKDF2 instead of the 11g transform (#829). The *verifier* stays 11g SHA-1 —
    the Mirror computes it from the plaintext secret and has no SHA-2 verifier to
    offer — which is a combination a real server also produces, for an account
    that predates SHA-2. ``AUTH_VFR_DATA``'s flag tells the client so.

    ``salt`` / ``server_session`` / ``derived_salt`` are injectable for
    deterministic tests; all default to fresh random values.
    """
    if salt is None:
        salt = token_bytes(16)
    if derived_salt is None and field_version >= FIELD_VERSION_12_1:
        derived_salt = token_bytes(_DERIVED_SALT_LEN)
    if server_session is None:
        server_session = (
            token_bytes(_MODERN_SESSION_LEN)
            if derived_salt is not None
            else token_bytes(_SERVER_SESSION_LEN) + pad2(b'', 8)
        )
    key_sess = key_sess_11g(password, salt)
    auth_sesskey = AES.new(key_sess, AES.MODE_CBC, O5LOGON_IV).encrypt(server_session)
    return Challenge(salt, server_session, key_sess, auth_sesskey, derived_salt)


def derive_conn_key(challenge: Challenge, client_auth_sesskey: bytes) -> bytes:
    """Derive the session ConnKey from the client's AUTH_SESSKEY response.

    Recovers the client session key and combines it with the server's — the
    result equals the ConnKey the client derived. The challenge's
    ``derived_salt`` decides which derivation is used, so this stays the exact
    inverse of whatever :func:`make_challenge` advertised: absent, the 11g
    transform; present, the PBKDF2 one the 12.1+ client ran (#829).
    """
    client_session = AES.new(challenge.key_sess, AES.MODE_CBC, O5LOGON_IV).decrypt(
        client_auth_sesskey
    )
    combined = cat_key(
        challenge.server_session, client_session, challenge.derived_salt, _BITS_11G
    )
    return conn_key(combined, challenge.derived_salt, _BITS_11G, PBKDF2_SDER_COUNT)


# The OCI dialect's AUTH_SVR_RESPONSE is 48 bytes, not the thin 16: the real 11g
# listener encrypts a 16-byte nonce, the SERVER_TO_CLIENT marker, and a full
# PKCS7 pad block (verified byte-exact against a live capture). The client finds
# the marker substring after decrypting, so the nonce is not checked.
_PROOF_NONCE_LEN = 16
_PKCS7_FULL_BLOCK = bytes([16]) * 16


def server_proof_oci(session_key: bytes, *, nonce: bytes | None = None) -> bytes:
    """The 48-byte OCI ``AUTH_SVR_RESPONSE`` (deadbeef dialect, #265).

    ``AES-CBC(nonce16 + SERVER_TO_CLIENT + PKCS7pad, ConnKey)`` — the classic
    O5LOGON server response the real 11g listener sends. ``nonce`` is injectable
    for deterministic tests; it defaults to a fresh random value and the client
    does not check it.
    """
    try:
        return server_proof_padded(session_key, nonce=nonce)
    except ValueError as exc:
        raise InterfaceError(str(exc)) from exc


def verify_password(
    session_key: bytes, auth_password: bytes | None, password: bytes
) -> bool:
    """True if the client's ``AUTH_PASSWORD`` proves it holds ``password``.

    ``AUTH_PASSWORD = AES-CBC(pad1(password), ConnKey)``, where ``pad1`` prepends
    a fixed 16-byte block the server discards. Decrypting with the server's
    ConnKey and comparing the payload past that block to ``pad2(password)``
    confirms both sides derived the same ConnKey — i.e. the client used the right
    password. A wrong password yields a different ConnKey, so the payload is
    garbage and the check fails. This is the server half of the mutual auth: it
    lets the Mirror *reject* a bad login (ORA-01017) rather than relying on the
    client to notice the server proof it can't validate.
    """
    if not auth_password or len(auth_password) % 16 != 0:
        return False
    # decrypt_password reverses the exact AUTH_PASSWORD transform (AES-CBC decrypt
    # under the ConnKey, drop pad1's 16-byte prefix, strip the PKCS7 tail). A wrong
    # password gives a different ConnKey and so garbage that will not equal it.
    return decrypt_password(session_key, auth_password) == password


_RESULT_PARAMS_TAIL: tuple[tuple[bytes, bytes], ...] = (
    (b'AUTH_XACTION_TRAITS', b'3'),
    (b'AUTH_VERSION_STATUS', b'0'),
    (b'AUTH_CAPABILITY_TABLE', b''),
    (b'AUTH_DBNAME', b'XE'),
    (b'AUTH_DB_MOUNT_ID\x00', b'3121942702'),
    (b'AUTH_DB_ID\x00', b'3115068141'),
    (b'AUTH_USER_ID', b'48'),
    (b'AUTH_SESSION_ID', b'59'),
    (b'AUTH_SERIAL_NUM', b'2021'),
    (b'AUTH_INSTANCE_NO', b'1'),
    (b'AUTH_FAILOVER_ID', b'1'),
    (b'AUTH_SERVER_PID', b'3327'),
    (b'AUTH_SC_SERVER_HOST', b'75106c7f39db'),
    (b'AUTH_SC_DBUNIQUE_NAME', b'XE'),
    (b'AUTH_SC_INSTANCE_NAME', b'XE'),
    (b'AUTH_SC_SERVICE_NAME', b'XE'),
    (b'AUTH_SC_INSTANCE_ID', b'1'),
    (b'AUTH_SC_INSTANCE_START_TIME', b'2026-08-09 16:48:44.000000000 +00:00'),
    (b'AUTH_SC_DB_DOMAIN', b''),
    (b'AUTH_SC_SVC_FLAGS', b'8'),
    (b'AUTH_INSTANCENAME', b'XE'),
    (b'AUTH_NLS_LXLAN\x00', b'AMERICAN'),
    (b'AUTH_NLS_LXCTERRITORY\x00', b'AMERICA'),
    (b'AUTH_NLS_LXCCURRENCY\x00', b'$'),
    (b'AUTH_NLS_LXCISOCURR\x00', b'AMERICA'),
    (b'AUTH_NLS_LXCNUMERICS\x00', b'.,'),
    (b'AUTH_NLS_LXCDATEFM\x00', b'DD-MON-RR'),
    (b'AUTH_NLS_LXCDATELANG\x00', b'AMERICAN'),
    (b'AUTH_NLS_LXCSORT\x00', b'BINARY'),
    (b'AUTH_NLS_LXCCALENDAR\x00', b'GREGORIAN'),
    (b'AUTH_NLS_LXCUNIONCUR\x00', b'$'),
    (b'AUTH_NLS_LXCTIMEFM\x00', b'HH.MI.SSXFF AM'),
    (b'AUTH_NLS_LXCSTMPFM\x00', b'DD-MON-RR HH.MI.SSXFF AM'),
    (b'AUTH_NLS_LXCTTZNFM\x00', b'HH.MI.SSXFF AM TZR'),
    (b'AUTH_NLS_LXCSTZNFM\x00', b'DD-MON-RR HH.MI.SSXFF AM TZR'),
)


def _result_params(identity: ServerIdentity) -> tuple[tuple[bytes, bytes], ...]:
    # The release fields lead the table, in the order the captured 11g server
    # sends them, then the fixed remainder.
    return (
        (b'AUTH_VERSION_STRING', identity.version_string),
        (b'AUTH_VERSION_SQL', identity.version_sql),
        *_RESULT_PARAMS_TAIL[:1],
        (b'AUTH_VERSION_NO', str(identity.version_no).encode('ascii')),
        *_RESULT_PARAMS_TAIL[1:],
    )


def encode_kv_oci(key: bytes, val: bytes, flags: int = 0) -> bytes:
    """One OCI-dialect (deadbeef) key-value pair: a little-endian ub4 declared
    length + short DALC for the key and the value (an empty value is the ub4
    length 0 with no data byte), then a ub4 flags field (the verifier type for
    AUTH_VFR_DATA, else 0). The write inverse of :func:`_oci_auth_value`."""

    def field(data: bytes) -> bytes:
        if not data:
            return struct.pack('<I', 0)
        return struct.pack('<I', len(data)) + bytes([len(data)]) + data

    return field(key) + field(val) + struct.pack('<I', flags)


def _oci_auth_packet(pairs: list[tuple[bytes, bytes, int]], trailer: bytes) -> bytes:
    """Assemble a full deadbeef-dialect O5LOGON DATA packet from its key-value
    pairs and trailer: a TTI_RPA marker, the pair count, a zero lead byte, the
    pairs, and the trailer — behind the 10-byte TNS DATA header."""
    payload = bytes([TTI_RPA, len(pairs), 0])
    payload += b''.join(encode_kv_oci(k, v, f) for k, v, f in pairs)
    payload += trailer
    header = struct.pack('>H', len(payload) + 10) + bytes([0, 0, 6, 0, 0, 0, 0, 0])
    return header + payload


def encode_challenge_oci(challenge: Challenge) -> bytes:
    """Build the sqlplus / thick-OCI (deadbeef dialect) O5LOGON challenge (#265).

    Returns the **full TNS_DATA packet** (header included), ready for
    ``PacketStream.send_raw``. Requires an 11g-shaped challenge — a 48-byte
    encrypted server session (96 hex) and a 10-byte salt (20 hex): pass
    ``make_challenge(secret, salt=token_bytes(10))``. Validated against live
    sqlplus 11.2, which accepts it and proceeds to send AUTH.
    """
    sesskey = _hexval(challenge.auth_sesskey)
    salt = _hexval(challenge.salt)
    if len(sesskey) != oci.OCI_SESSKEY_HEXLEN or len(salt) != oci.OCI_SALT_HEXLEN:
        raise InterfaceError(
            'OCI challenge needs a 48-byte server session and a 10-byte salt, '
            f'got {len(challenge.auth_sesskey)}/{len(challenge.salt)} bytes'
        )
    pairs = [
        (b'AUTH_SESSKEY', sesskey, 0),
        (b'AUTH_VFR_DATA', salt, VFR_11G_SHA1),
        (b'AUTH_GLOBALLY_UNIQUE_DBID\x00', AUTH_GLOBALLY_UNIQUE_DBID, 0),
    ]
    return _oci_auth_packet(pairs, _CHALLENGE_TRAILER)


def encode_result_oci(
    session_key: bytes,
    *,
    nonce: bytes | None = None,
    identity: ServerIdentity = IDENTITY_11_2,
) -> bytes:
    """Build the sqlplus / thick-OCI (deadbeef dialect) O5LOGON result (#265).

    Returns the **full TNS_DATA packet** (header included), ready for
    ``PacketStream.send_raw``. ``AUTH_SVR_RESPONSE`` (the freshly computed 48-byte
    server proof) is the one per-login value; the release fields come from
    ``identity`` and the rest is the Mirror's fixed identity. ``nonce`` is
    forwarded to :func:`server_proof_oci` for deterministic tests.
    """
    proof = _hexval(server_proof_oci(session_key, nonce=nonce))
    pairs = [(k, v, 0) for k, v in _result_params(identity)]
    pairs.append((b'AUTH_SVR_RESPONSE', proof, 0))
    return _oci_auth_packet(pairs, _RESULT_TRAILER)


# The ordered key/value pairs of the auth message most recently parsed, for the
# few fields that REPEAT and so cannot survive the dict form (#826).
_LAST_AUTH_PAIRS: contextvars.ContextVar[list] = contextvars.ContextVar(
    'last_auth_pairs', default=[]
)


def _parse_fun_auth(
    payload: bytes, field_version: int = FIELD_VERSION_11_2
) -> tuple[int, bytes, dict[bytes, bytes | None]]:
    # Parse a client TTI_FUN auth message (OSESSKEY or AUTH):
    #   TTI_FUN, subtype, seq, 0x01, sb4(userlen), sb4(mode), 0x01,
    #   sb4(numpairs), 0x01, 0x01, user, <numpairs key-value pairs>
    # ``user`` is the raw bytes (read via userlen) on 11g / fv < 12.1; a 12.1+
    # client writes it length-prefixed (write_bytes_with_length), so the session's
    # negotiated field version decides whether a length byte precedes it — reading
    # the 12c form as 11g yields b'\x03PYO'-style garbage and a rejected login.
    if len(payload) < 4 or payload[0] != TTI_FUN:
        raise InterfaceError('not a TTI_FUN message')
    subtype = payload[1]
    rest = payload[3:]  # skip TTI_FUN, subtype, seq
    # At fv > 17 the OAUTH (phase-two AUTH) header carries a `0` has-user pointer
    # byte before the `0x01` marker (PROTOCOL.md §20.3), which the phase-one
    # OSESSKEY does not — so detect it rather than assume by subtype: when the
    # byte after the sequence is not the `0x01` marker, it is that extra pointer,
    # consume it. Below fv 18 there is never one, so this reduces to the
    # historical single-byte skip (payload[4:]).
    if field_version > FIELD_VERSION_23_1 and rest[:1] != b'\x01':
        _has_user, rest = decode_ub4(rest)
    rest = rest[1:]  # skip the 0x01 marker
    userlen, rest = decode_ub4(rest)
    _mode, rest = decode_ub4(rest)
    rest = rest[1:]  # skip the 0x01 has-more byte
    numpairs, rest = decode_ub4(rest)
    rest = rest[2:]  # skip the 0x01 0x01 pointer pair
    if field_version >= FIELD_VERSION_12_1:
        rest = rest[1:]  # the 12.1+ length byte in front of the username
    user = rest[:userlen]
    kvs, _ = decode_kv(rest[userlen:], numpairs, [])
    # The ORDERED pairs are kept alongside the dict: application context arrives
    # as repeated NSPACE / ATTR / VALUE triples under the same three keys, so
    # collapsing to a dict keeps only the LAST entry (#826).
    _LAST_AUTH_PAIRS.set(list(kvs))
    return subtype, user, dict(kvs)


def parse_osesskey(payload: bytes, field_version: int = FIELD_VERSION_11_2) -> bytes:
    """Return the username from the client's OSESSKEY (phase-one) request."""
    subtype, user, _ = _parse_fun_auth(payload, field_version)
    if subtype != TTI_SESS:
        raise InterfaceError(f'expected OSESSKEY, got subtype {subtype}')
    return user


# The OSESSKEY pairs that carry the client's session identity, mapped to the
# v$session column each one lands in. AUTH_SID is osuser, not a session id (#826).
_IDENTITY_KEYS = {
    b'AUTH_PROGRAM_NM': 'program',
    b'AUTH_MACHINE': 'machine',
    b'AUTH_TERMINAL': 'terminal',
    b'AUTH_SID': 'osuser',
}


def parse_client_identity(
    payload: bytes, field_version: int = FIELD_VERSION_11_2
) -> dict[str, str]:
    """The session identity the client declared in its OSESSKEY (#826).

    program / machine / terminal / osuser, by the name each is known by, and only
    the ones actually sent. They ride in the FIRST auth message, which is why a
    Mirror can relay them to an upstream session it opens during authentication --
    the driver name arrives later, in the AUTH, and is not here.

    Never raises: identity is informational, and a login must not fail over it.
    """
    try:
        _subtype, _user, kvs = _parse_fun_auth(payload, field_version)
    except Exception:  # noqa: BLE001 - a login must not fail over metadata
        return {}
    out = {}
    for key, name in _IDENTITY_KEYS.items():
        value = kvs.get(key)
        if value:
            out[name] = value.decode('utf-8', 'replace')
    return out


# The classic sqlplus / thick-OCI (deadbeef dialect) OSESSKEY marshals its fixed
# header fields very differently from the thin form: an 8-byte 0xFE indicator
# (0xFFFFFFFFFFFFFFFE little-endian) stands in for thin's 0x01 pointer bytes, and
# lengths are fixed 4-byte little-endian ub4s. The layout up to the username is
# constant (confirmed against live sqlplus 11.2 for usernames of different
# lengths), so the ub1-length-prefixed username sits at a fixed offset (#265):
#   03(TTI_FUN) subtype seq | IND | ub4 ub4 | IND | ub4 ub4 | IND | IND | ub1+user


def find_fast_auth_osesskey(body: bytes, field_version: int) -> int:
    """Offset of the OSESSKEY (phase-one auth) message inside a client FAST_AUTH
    bundle — the server counterpart of the client's ``find_fast_auth_rpa``. The
    bundle is a ``0x22`` header, the PRO message, five bytes, a version byte, the
    DTY message, then the OSESSKEY as the tail (§20). The OSESSKEY leads with
    ``TTI_FUN TTI_SESS`` (``03 76``), but the DTY type table carries those bytes
    too, so accept the first ``03 76`` whose :func:`parse_osesskey` yields a
    plausible (printable, non-empty) username rather than trusting the position.
    """
    marker = bytes([TTI_FUN, TTI_SESS])
    for off in range(len(body) - 1):
        if body[off : off + 2] != marker:
            continue
        try:
            user = parse_osesskey(body[off:], field_version)
        except Exception:
            continue
        if user and all(0x20 <= b < 0x7F for b in user):
            return off
    return -1


def _parse_oci_fun_username(payload: bytes, subtype: int, what: str) -> bytes:
    # OSESSKEY and AUTH share the same TTI_FUN prefix in the deadbeef dialect:
    # only the subtype byte and the ub4 field values differ, so the username sits
    # at the same fixed offset in both. Validate every indicator so a
    # differently-shaped message surfaces as an error, not a garbage username.
    if len(payload) < 4 or payload[0] != TTI_FUN:
        raise InterfaceError('not a TTI_FUN message')
    if payload[1] != subtype:
        raise InterfaceError(f'expected {what}, got subtype {payload[1]}')
    # Indicators sit at these offsets; between the 1st/2nd and 3rd/4th come the
    # two ub4 length-field pairs that make up the gaps.
    for expected_ind_off in (3, 19, 35, 43):
        if payload[expected_ind_off : expected_ind_off + 8] != oci.OCI_INDICATOR:
            raise InterfaceError(
                f'OCI {what}: no indicator at offset {expected_ind_off}'
            )
    user_off = 51  # 3 + 8 + (4+4) + 8 + (4+4) + 8 + 8
    userlen = payload[user_off]
    return payload[user_off + 1 : user_off + 1 + userlen]


def parse_osesskey_oci(payload: bytes) -> bytes:
    """Return the username from a sqlplus / thick-OCI OSESSKEY (deadbeef dialect).

    Verified against live sqlplus 11.2 captures (usernames ``pyo`` and
    ``abcdefgh``). Raises :class:`InterfaceError` if the fixed indicator layout
    is not where the OCI OSESSKEY puts it.
    """
    return _parse_oci_fun_username(payload, TTI_SESS, 'OSESSKEY')


def _oci_auth_value(payload: bytes, key: bytes) -> bytes:
    # In the OCI AUTH, each key-value pair is ``<key> <ub4 declared-len> <DALC
    # value>``: a fixed 4-byte little-endian length precedes the DALC-chunked
    # value (0xFE-marked chunks — the same encoding seerdb's client decoder
    # reads). The value is uppercase-hex ASCII, so unhexlify recovers the bytes.
    i = payload.find(key)
    if i < 0:
        raise InterfaceError(f'OCI AUTH: missing {key.decode()}')
    hexval, _ = decode_dalc(payload[i + len(key) + 4 :])
    # decode_dalc reports an empty/null value as []; a real AUTH_SESSKEY /
    # AUTH_PASSWORD is always non-empty hex bytes.
    if not isinstance(hexval, bytes):
        raise InterfaceError(f'OCI AUTH: empty {key.decode()}')
    return unhexlify(hexval)


def parse_auth_response_oci(payload: bytes) -> tuple[bytes, bytes, bytes]:
    """Return ``(username, client AUTH_SESSKEY, AUTH_PASSWORD)`` from the OCI AUTH.

    The sqlplus / thick-OCI (deadbeef dialect) counterpart of
    :func:`parse_auth_response`. The client's session key derives the shared
    ConnKey (:func:`derive_conn_key`); ``AUTH_PASSWORD`` is the password proof
    that :func:`verify_password` checks. Verified against a live sqlplus 11.2
    AUTH: a 48-byte session key and a 32-byte proof.
    """
    user = _parse_oci_fun_username(payload, TTI_AUTH, 'AUTH')
    sesskey = _oci_auth_value(payload, b'AUTH_SESSKEY')
    password = _oci_auth_value(payload, b'AUTH_PASSWORD')
    return user, sesskey, password


def _group_app_context(pairs: list) -> list[tuple[str, str, str]]:
    """Application-context entries from the auth message's key/value pairs.

    :func:`decode_kv` SORTS the pairs, so the wire order across the three keys
    is gone by the time they arrive -- but Python's sort is stable, so each key
    keeps its OWN sequence (ATTR1, ATTR2, ATTR3 / VALUE1, VALUE2, VALUE3).
    Group by key and zip: that recovers the entries without depending on an
    interleaving that no longer exists. The keys carry a trailing NUL.
    """
    columns: dict[str, list[str]] = {}
    for raw_key, raw_value in pairs:
        key = bytes(raw_key).rstrip(b'\x00')
        if not key.startswith(b'AUTH_APPCTX_'):
            continue
        field = key[len(b'AUTH_APPCTX_') :].decode('ascii', 'replace').lower()
        columns.setdefault(field, []).append(
            bytes(raw_value or b'').decode('utf-8', 'replace')
        )
    return list(
        zip(
            columns.get('nspace', []),
            columns.get('attr', []),
            columns.get('value', []),
            strict=False,
        )
    )


def parse_auth_app_context(
    payload: bytes, field_version: int = FIELD_VERSION_11_2
) -> list[tuple[str, str, str]]:
    """The application-context entries a login AUTH declares (#826).

    `connect(appcontext=[(namespace, attribute, value), ...])` sends each entry
    as three consecutive pairs -- ``AUTH_APPCTX_NSPACE`` / ``_ATTR`` / ``_VALUE``
    -- REPEATING the same three keys per entry. The dict form of the message
    therefore keeps only the last, which is why this reads the ordered pairs.
    The keys carry a trailing NUL on the wire.

    Never raises: a malformed value must not fail an otherwise good login.
    """
    try:
        _parse_fun_auth(payload, field_version)
        pairs = _LAST_AUTH_PAIRS.get()
    except Exception:  # noqa: BLE001 - a login must not fail over this
        return []
    return _group_app_context(pairs)


_CONNECT_ATTR_KEYS = {
    b'SESSION_CLIENT_DRIVER_NAME': 'driver_name',
    b'AUTH_ORA_EDITION': 'edition',
    # A proxy login: the user the authenticating user connects on behalf of
    # (`user[proxy]`). The session belongs to THIS user, so a backend that opens
    # its own upstream session has to open it as a proxy session too (#1093).
    b'PROXY_CLIENT_NAME': 'proxy_client_name',
}


def parse_auth_connect_attrs(
    payload: bytes, field_version: int = FIELD_VERSION_11_2
) -> dict[str, str]:
    """Connect-time attributes that ride in the login AUTH (#826).

    The driver banner and the edition arrive HERE, not in the OSESSKEY the
    identity comes from -- so a backend can only honour them if it has held its
    upstream connect until after this message. Only the ones actually sent are
    returned.

    Never raises: a malformed value must not fail an otherwise good login.
    """
    try:
        _subtype, _user, kvs = _parse_fun_auth(payload, field_version)
    except Exception:  # noqa: BLE001 - a login must not fail over this
        return {}
    out = {}
    for key, name in _CONNECT_ATTR_KEYS.items():
        value = kvs.get(key)
        if value:
            out[name] = bytes(value).rstrip(b'\x00').decode('utf-8', 'replace')
    return out


def parse_auth_new_password(
    payload: bytes, field_version: int = FIELD_VERSION_11_2
) -> bytes | None:
    """The ``AUTH_NEWPASSWORD`` a login AUTH carries, or ``None``.

    A client may change the password AS IT CONNECTS (`newpassword=` on connect),
    which rides in the ordinary login AUTH beside the proof rather than in a
    separate changepassword call. It is AES-encrypted under the ConnKey, the same
    as the standalone change (#826).

    Never raises: a malformed value must not fail a login that is otherwise good.
    """
    try:
        _subtype, _user, kvs = _parse_fun_auth(payload, field_version)
    except Exception:  # noqa: BLE001 - a login must not fail over this
        return None
    value = kvs.get(b'AUTH_NEWPASSWORD')
    if not value:
        return None
    try:
        return unhexlify(value)
    except Exception:  # noqa: BLE001
        return None


def parse_auth_response(
    payload: bytes, field_version: int = FIELD_VERSION_11_2
) -> tuple[bytes, bytes, bytes | None]:
    """Return ``(username, client AUTH_SESSKEY, AUTH_PASSWORD)`` from the AUTH.

    The client's session key derives the shared ConnKey; ``AUTH_PASSWORD`` (the
    client's password proof, ``None`` if absent) lets the server verify the
    password with :func:`verify_password`.
    """
    subtype, user, kvs = _parse_fun_auth(payload, field_version)
    if subtype != TTI_AUTH:
        raise InterfaceError(f'expected AUTH, got subtype {subtype}')
    sesskey = kvs.get(b'AUTH_SESSKEY')
    if sesskey is None:
        raise InterfaceError('AUTH response missing AUTH_SESSKEY')
    password = kvs.get(b'AUTH_PASSWORD')
    auth_password = unhexlify(password) if password else None
    return user, unhexlify(sesskey), auth_password


def parse_changepassword_oci(payload: bytes) -> tuple[bytes, bytes, bytes]:
    """Return ``(username, AUTH_PASSWORD, AUTH_NEWPASSWORD)`` from an OCI
    changepassword — sqlplus's ``PASSWORD`` command (OCIPasswordChange).

    The OCI counterpart of :func:`parse_changepassword`. sqlplus wraps it in a
    TTI_80SES piggyback (stripped before this) and marshals it in the OCI dialect,
    but the payload is the same two fields as the thin form: ``AUTH_PASSWORD`` (the
    current password) and ``AUTH_NEWPASSWORD`` (the new one), each the AES-CBC
    ciphertext the client encrypted under the login ConnKey — the session decrypts
    them with :func:`~seerdb.common.crypto.decrypt_password`. Unlike the OCI login
    AUTH there is no proof to verify; the live session is the authorisation."""
    user = _parse_oci_fun_username(payload, TTI_AUTH, 'AUTH')
    old_cipher = _oci_auth_value(payload, b'AUTH_PASSWORD')
    new_cipher = _oci_auth_value(payload, b'AUTH_NEWPASSWORD')
    return user, old_cipher, new_cipher


def parse_changepassword(
    payload: bytes, field_version: int = FIELD_VERSION_11_2
) -> tuple[bytes, bytes, bytes]:
    """Return ``(username, AUTH_PASSWORD, AUTH_NEWPASSWORD)`` from a changepassword
    TTI_AUTH (#21/#486). Both password fields are the AES-CBC ciphertext (already
    un-hexed) the client encrypted under the login ConnKey — the session decrypts
    them with :func:`~seerdb.common.crypto.decrypt_password`. Unlike login this
    carries no ``AUTH_SESSKEY`` (the session already exists)."""
    subtype, user, kvs = _parse_fun_auth(payload, field_version)
    if subtype != TTI_AUTH:
        raise InterfaceError(f'expected AUTH, got subtype {subtype}')
    old_cipher = kvs.get(b'AUTH_PASSWORD')
    new_cipher = kvs.get(b'AUTH_NEWPASSWORD')
    if old_cipher is None or new_cipher is None:
        raise InterfaceError('changepassword missing AUTH_PASSWORD / AUTH_NEWPASSWORD')
    return user, unhexlify(old_cipher), unhexlify(new_cipher)


# Token auth is a modern feature: its long values (the RSA signature, and real
# JWTs) are written in the fv >= 12.2 chunked form (ub4-prefixed chunks). Decode
# them with that field version, not the Mirror's pinned-11g default of 6.
_TOKEN_DECODE_FV = FIELD_VERSION_12_2


def is_token_auth(payload: bytes) -> bool:
    """Whether a post-DTY auth message is a token AUTH (#125) rather than the
    O5LOGON OSESSKEY (which is a ``TTI_SESS`` subtype). A token AUTH is a
    ``TTI_AUTH`` carrying an ``AUTH_TOKEN`` pair, sent in place of OSESSKEY."""
    if len(payload) < 2 or payload[0] != TTI_FUN or payload[1] != TTI_AUTH:
        return False
    _DECODE_FIELD_VERSION.set(_TOKEN_DECODE_FV)
    try:
        _subtype, _user, kvs = _parse_fun_auth(payload)
    except InterfaceError:
        return False
    return b'AUTH_TOKEN' in kvs


def parse_token_auth(payload: bytes) -> tuple[bytes, bytes | None, bytes | None]:
    """Return ``(token, header, signature)`` from a token AUTH (#125).

    ``header`` / ``signature`` are the OCI IAM signed-request pair (both ``None``
    for the OAuth2 bare-token variant).
    """
    _DECODE_FIELD_VERSION.set(_TOKEN_DECODE_FV)
    subtype, _user, kvs = _parse_fun_auth(payload)
    if subtype != TTI_AUTH:
        raise InterfaceError(f'expected token AUTH, got subtype {subtype}')
    token = kvs.get(b'AUTH_TOKEN')
    if token is None:
        raise InterfaceError('token AUTH missing AUTH_TOKEN')
    return token, kvs.get(b'AUTH_HEADER'), kvs.get(b'AUTH_SIGNATURE')
