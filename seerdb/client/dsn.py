# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The ``dsn`` connect argument without a wallet: an Easy Connect string (#1972).

``connect(dsn='db.example.com:1521/FREEPDB1')`` is how most python-oracledb code
connects. Without a wallet seerdb used to drop the argument and dial
``localhost:1521``. This module reads the string the way python-oracledb does:

    [user[/password]@][//]host[:port][/service_name]

The host may be a bracketed IPv6 address, the port defaults to 1521, and what
the string names wins over ``host`` / ``port`` / ``service_name`` passed beside
it, as it does in python-oracledb. A form seerdb does not read yet -- a connect
descriptor, a tnsnames alias (which needs ``config_dir``), a protocol prefix,
``?`` parameters or a ``:server_type`` -- raises NotSupportedError naming it,
rather than being ignored.
"""

from __future__ import annotations

from dataclasses import dataclass

from seerdb.common.exceptions import NotSupportedError, ProgrammingError

_DEFAULT_PORT = 1521


@dataclass(frozen=True)
class EasyConnect:
    """What an Easy Connect string names."""

    host: str
    port: int
    service_name: str
    user: str | None = None
    password: str | None = None


def parse_easy_connect(dsn: str) -> EasyConnect:
    """Parse ``[user[/password]@][//]host[:port][/service_name]``."""
    text = dsn.strip()
    user = password = None
    if '@' in text:
        # The address cannot hold an '@', a password can: split at the last one.
        credentials, _, text = text.rpartition('@')
        user, slash, secret = credentials.partition('/')
        password = secret if slash else None
        if not user:
            raise ProgrammingError(f'dsn {dsn!r} has an "@" but no user name before it')
    if text.startswith('('):
        raise NotSupportedError(
            f'dsn {dsn!r}: a connect descriptor is not supported yet; '
            'pass host, port and service_name'
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
    # A bare word is a tnsnames alias in python-oracledb, not a host name.
    if not (easy or rest or text.startswith('[')):
        raise NotSupportedError(
            f'dsn {dsn!r} looks like a tnsnames alias, which needs config_dir= '
            '(or wallet_location=); pass host:port/service_name otherwise'
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
    return EasyConnect(host, port, service_name, user, password)


def resolve_dsn_arguments(
    dsn: str,
    host: str,
    port: int,
    service_name: str,
    sid: str,
    user: str,
    password: str,
) -> tuple[str, int, str, str, str, str]:
    """The connect arguments once ``dsn`` is applied: the DSN wins over host,
    port and service_name passed beside it, and supplies the credentials only
    when no ``user`` was passed."""
    info = parse_easy_connect(dsn)
    if info.service_name:
        service_name, sid = info.service_name, ''
    if info.user is not None:
        if user:
            raise ProgrammingError(
                f'dsn {dsn!r} carries a user name, and user= was passed too'
            )
        user = info.user
        if info.password is not None:
            password = info.password
    return info.host, info.port, service_name, sid, user, password
