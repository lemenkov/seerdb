# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The ``dsn`` connect argument without a wallet: an Easy Connect string
(#1972) or a connect descriptor (#1976).

``connect(dsn='db.example.com:1521/FREEPDB1')`` is how most python-oracledb code
connects. Without a wallet seerdb used to drop the argument and dial
``localhost:1521``. This module reads the string the way python-oracledb does:

    [user[/password]@][//]host[:port][/service_name]

or a connect descriptor with one address,
``[user[/password]@](DESCRIPTION=(ADDRESS=...)(CONNECT_DATA=...))``
(:func:`seerdb.client.netconfig.parse_descriptor`).

The host may be a bracketed IPv6 address, the port defaults to 1521, and what
the string names wins over ``host`` / ``port`` / ``service_name`` passed beside
it, as it does in python-oracledb. A bare word is a tnsnames alias, read from
``config_dir``, ``TNS_ADMIN`` or ``$ORACLE_HOME/network/admin``
(:func:`seerdb.client.netconfig.resolve_alias`). A form seerdb does not read
yet -- a protocol prefix, ``?`` parameters, a ``:server_type``, or a descriptor
parameter seerdb does not act on -- raises NotSupportedError naming it, rather
than being ignored.
"""

from __future__ import annotations

from dataclasses import dataclass

from seerdb.client.netconfig import parse_descriptor, resolve_alias
from seerdb.common.exceptions import NotSupportedError, ProgrammingError

_DEFAULT_PORT = 1521


@dataclass(frozen=True)
class DsnTarget:
    """What a dsn names."""

    host: str
    port: int
    service_name: str
    user: str | None = None
    password: str | None = None
    sid: str = ''


def parse_dsn(dsn: str, config_dir: str | None = None) -> DsnTarget:
    """Parse an Easy Connect string, a connect descriptor or a tnsnames alias
    (looked up from ``config_dir``, ``TNS_ADMIN`` or ``ORACLE_HOME``), each behind
    an optional ``user[/password]@``."""
    text = dsn.strip()
    user = password = None
    # The credentials end at the last '@' before any descriptor: a password can
    # hold an '@', the address cannot, and a descriptor's quoted values might.
    head = text[: text.find('(')] if '(' in text else text
    if '@' in head:
        credentials, _, _rest = head.rpartition('@')
        text = text[len(credentials) + 1 :]
        user, slash, secret = credentials.partition('/')
        password = secret if slash else None
        if not user:
            raise ProgrammingError(f'dsn {dsn!r} has an "@" but no user name before it')
    if text.startswith('('):
        found = parse_descriptor(text)
        return DsnTarget(
            found.host, found.port, found.service_name, user, password, found.sid
        )
    if '://' in text:
        raise NotSupportedError(
            f'dsn {dsn!r}: a protocol prefix is not supported yet; use ssl= for TCPS'
        )
    if '?' in text:
        raise NotSupportedError(f'dsn {dsn!r}: "?" parameters are not supported yet')
    easy = text.startswith('//')  # only Easy Connect opens with '//'
    text = text.removeprefix('//')
    if text.startswith('['):
        close = text.find(']')
        if close < 0:
            raise ProgrammingError(f'dsn {dsn!r}: an IPv6 address lacks its "]"')
        host, rest = text[1:close], text[close + 1 :]
    else:
        cut = min(
            (i for i in (text.find(':'), text.find('/')) if i >= 0), default=len(text)
        )
        host, rest = text[:cut], text[cut:]
    if not host:
        raise ProgrammingError(f'dsn {dsn!r} names no host')
    # A bare word is a tnsnames alias, as in python-oracledb, not a host name.
    if not (easy or rest or text.startswith('[')):
        found = resolve_alias(text, config_dir)
        return DsnTarget(
            found.host, found.port, found.service_name, user, password, found.sid
        )
    port = _DEFAULT_PORT
    if rest.startswith(':'):
        digits, slash, after = rest[1:].partition('/')
        if not digits.isdigit():
            raise ProgrammingError(f'dsn {dsn!r}: the port is not a number')
        port = int(digits)
        rest = slash + after
    service_name = ''
    if rest.startswith('/'):
        service_name = rest[1:]
        if ':' in service_name:
            raise NotSupportedError(
                f'dsn {dsn!r}: a ":server_type" is not supported yet'
            )
    elif rest:
        raise ProgrammingError(f'dsn {dsn!r} is not [//]host[:port][/service_name]')
    return DsnTarget(host, port, service_name, user, password)


def resolve_dsn_arguments(
    dsn: str,
    host: str,
    port: int,
    service_name: str,
    sid: str,
    user: str,
    password: str,
    config_dir: str | None = None,
) -> tuple[str, int, str, str, str, str]:
    """The connect arguments once ``dsn`` is applied: the DSN wins over host,
    port and service_name passed beside it, and supplies the credentials only
    when no ``user`` was passed."""
    info = parse_dsn(dsn, config_dir)
    if info.service_name:
        service_name, sid = info.service_name, ''
    elif info.sid:
        service_name, sid = '', info.sid
    if info.user is not None:
        if user:
            raise ProgrammingError(
                f'dsn {dsn!r} carries a user name, and user= was passed too'
            )
        user = info.user
        if info.password is not None:
            password = info.password
    return info.host, info.port, service_name, sid, user, password
