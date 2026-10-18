# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Oracle Net configuration text: the descriptor grammar of ``tnsnames.ora`` and
``sqlnet.ora``, and a connect descriptor passed as ``dsn`` (#1976).

Pure functions over ``str``, with no third-party imports, so a descriptor can be
read without the ``wallet`` extra; :mod:`seerdb.client.wallet` builds on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from seerdb.common.exceptions import NotSupportedError, ProgrammingError

# A parsed descriptor node is either a leaf string or a *branch*: a list of
# ``(key, node)`` pairs. Keys are lower-cased; the list preserves order and
# duplicates (an ADDRESS_LIST may repeat ADDRESS), which a plain dict could not.
Node: TypeAlias = 'str | list[tuple[str, Node]]'


class NetConfigError(ProgrammingError):
    """Oracle Net configuration text could not be read."""


# --------------------------------------------------------------------------- #
# tnsnames.ora / sqlnet.ora descriptor grammar
# --------------------------------------------------------------------------- #
#
# Both files are the same nested S-expression-ish syntax Oracle Net uses
# everywhere::
#
#     alias = (DESCRIPTION = (ADDRESS = (PROTOCOL = tcps)(HOST = h)(PORT = 1522))
#                            (CONNECT_DATA = (SERVICE_NAME = svc)))
#
# A parameter is ``(NAME = VALUE)`` where VALUE is either a run of child
# parameters or a single atom (bareword or double-quoted string — the server
# certificate DN arrives quoted because it is full of spaces and commas).


def _tokenize(Text: str) -> list[tuple[str, str]]:
    """Split descriptor text into ``(kind, value)`` tokens.

    ``kind`` is one of ``lparen`` / ``rparen`` / ``eq`` / ``comma`` / ``atom``.
    ``#`` starts a comment to end-of-line; whitespace is insignificant.
    """
    Tokens: list[tuple[str, str]] = []
    i = 0
    N = len(Text)
    Punct = {'(': 'lparen', ')': 'rparen', '=': 'eq', ',': 'comma'}
    while i < N:
        Ch = Text[i]
        if Ch.isspace():
            i += 1
            continue
        if Ch == '#':
            # Comment: skip to (and including) the newline.
            End = Text.find('\n', i)
            i = N if End < 0 else End + 1
            continue
        if Ch in Punct:
            Tokens.append((Punct[Ch], Ch))
            i += 1
            continue
        if Ch == '"':
            End = Text.find('"', i + 1)
            if End < 0:
                raise NetConfigError('unterminated quoted string in descriptor')
            Tokens.append(('atom', Text[i + 1 : End]))
            i = End + 1
            continue
        # Bareword: everything up to whitespace or a structural character.
        Start = i
        while i < N and not Text[i].isspace() and Text[i] not in '()=,#"':
            i += 1
        Tokens.append(('atom', Text[Start:i]))
    return Tokens


def _parse_value(Tokens: list[tuple[str, str]], i: int) -> tuple[Node, int]:
    """Parse the VALUE that follows an ``=`` at ``Tokens[i]``.

    A value is either a sequence of child parameters (when it opens with
    ``(``) or a single atom. Returns ``(node, next_index)``.
    """
    if i < len(Tokens) and Tokens[i][0] == 'lparen':
        Children: list[tuple[str, Node]] = []
        while i < len(Tokens) and Tokens[i][0] == 'lparen':
            (Param, i) = _parse_param(Tokens, i)
            Children.append(Param)
        return (Children, i)
    if i < len(Tokens) and Tokens[i][0] == 'atom':
        return (Tokens[i][1], i + 1)
    raise NetConfigError('expected a value in descriptor')


def _parse_param(Tokens: list[tuple[str, str]], i: int) -> tuple[tuple[str, Node], int]:
    """Parse one ``(NAME = VALUE)`` parameter. Returns ``((name, node), next)``."""
    if i >= len(Tokens) or Tokens[i][0] != 'lparen':
        raise NetConfigError("expected '(' in descriptor")
    i += 1
    if i >= len(Tokens) or Tokens[i][0] != 'atom':
        raise NetConfigError('expected a parameter name in descriptor')
    Name = Tokens[i][1].lower()
    i += 1
    if i >= len(Tokens) or Tokens[i][0] != 'eq':
        raise NetConfigError(f"expected '=' after {Name!r} in descriptor")
    i += 1
    (Value, i) = _parse_value(Tokens, i)
    if i >= len(Tokens) or Tokens[i][0] != 'rparen':
        raise NetConfigError(f"expected ')' closing {Name!r} in descriptor")
    return ((Name, Value), i + 1)


def parse_tnsnames(Text: str) -> dict[str, Node]:
    """Parse a ``tnsnames.ora`` into ``{alias: descriptor_node}``.

    Aliases are lower-cased. A comma-separated alias list (``a, b = (...)``)
    binds every name to the same descriptor.
    """
    Tokens = _tokenize(Text)
    Entries: dict[str, Node] = {}
    i = 0
    while i < len(Tokens):
        # Left-hand side: one or more comma-separated alias names.
        Names: list[str] = []
        if Tokens[i][0] != 'atom':
            raise NetConfigError('expected an alias name in tnsnames.ora')
        Names.append(Tokens[i][1].lower())
        i += 1
        while i < len(Tokens) and Tokens[i][0] == 'comma':
            i += 1
            if i >= len(Tokens) or Tokens[i][0] != 'atom':
                raise NetConfigError('expected an alias name after comma')
            Names.append(Tokens[i][1].lower())
            i += 1
        if i >= len(Tokens) or Tokens[i][0] != 'eq':
            raise NetConfigError(f"expected '=' after alias {Names[-1]!r}")
        i += 1
        (Value, i) = _parse_value(Tokens, i)
        for Name in Names:
            Entries[Name] = Value
    return Entries


def parse_sqlnet(Text: str) -> dict[str, Node]:
    """Parse a ``sqlnet.ora`` into ``{param: node}`` (params lower-cased)."""
    Tokens = _tokenize(Text)
    Params: dict[str, Node] = {}
    i = 0
    while i < len(Tokens):
        if Tokens[i][0] != 'atom':
            raise NetConfigError('expected a parameter name in sqlnet.ora')
        Name = Tokens[i][1].lower()
        i += 1
        if i >= len(Tokens) or Tokens[i][0] != 'eq':
            raise NetConfigError(f"expected '=' after {Name!r} in sqlnet.ora")
        i += 1
        (Value, i) = _parse_value(Tokens, i)
        Params[Name] = Value
    return Params


def _find_all(Node_: Node, Key: str) -> list[Node]:
    """Every value bound to ``Key`` anywhere in the subtree, in document order."""
    Found: list[Node] = []
    if isinstance(Node_, list):
        for K, V in Node_:
            if K == Key:
                Found.append(V)
            Found.extend(_find_all(V, Key))
    return Found


def _find_first(Node_: Node, Key: str) -> Node | None:
    """The first value bound to ``Key`` in the subtree, or ``None``."""
    Matches = _find_all(Node_, Key)
    return Matches[0] if Matches else None


# --------------------------------------------------------------------------- #
# A connect descriptor passed as dsn
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DescriptorTarget:
    """What a single-address connect descriptor names."""

    host: str
    port: int
    service_name: str = ''
    sid: str = ''


# The keys read, at each level. CID only describes the client, which seerdb
# reports itself, and FAILOVER means nothing with one address. Anything else is
# refused by name rather than ignored -- python-oracledb acts on most of them
# (RETRY_COUNT, SDU, SECURITY, ...), so dropping one would connect differently.
_DESCRIPTION_KEYS = frozenset({'address', 'address_list', 'connect_data', 'failover'})
_ADDRESS_KEYS = frozenset({'protocol', 'host', 'port'})
_CONNECT_DATA_KEYS = frozenset({'service_name', 'sid', 'cid'})
_DEFAULT_PORT = 1521


def _branch(value: Node, name: str) -> list[tuple[str, Node]]:
    if not isinstance(value, list):
        raise NetConfigError(f'{name.upper()} must hold parameters, not {value!r}')
    return value


def _refuse_unread(
    entries: list[tuple[str, Node]], keys: frozenset, where: str
) -> None:
    unread = sorted({key.upper() for key, _ in entries if key not in keys})
    if unread:
        raise NotSupportedError(
            f'{where} {", ".join(unread)} in a connect descriptor is not supported yet'
        )


def parse_descriptor(text: str) -> DescriptorTarget:
    """Read a connect descriptor with one TCP address:
    ``(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=h)(PORT=p))(CONNECT_DATA=...))``.
    """
    tokens = _tokenize(text)
    ((name, value), end) = _parse_param(tokens, 0)
    if end != len(tokens):
        raise NetConfigError('text follows the connect descriptor')
    if name == 'description_list':
        descriptions = [v for k, v in _branch(value, name) if k == 'description']
        if len(descriptions) != 1:
            raise NotSupportedError(
                'a DESCRIPTION_LIST of several descriptions is not supported yet (#1983)'
            )
        (name, value) = ('description', descriptions[0])
    if name != 'description':
        raise NetConfigError(
            f'a connect descriptor opens with DESCRIPTION, not {name.upper()}'
        )
    description = _branch(value, name)
    _refuse_unread(description, _DESCRIPTION_KEYS, 'DESCRIPTION parameter')
    addresses: list[Node] = []
    for key, child in description:
        if key == 'address':
            addresses.append(child)
        elif key == 'address_list':
            entries = _branch(child, key)
            _refuse_unread(
                entries, frozenset({'address', 'failover'}), 'ADDRESS_LIST parameter'
            )
            addresses.extend(v for k, v in entries if k == 'address')
    if not addresses:
        raise NetConfigError('the connect descriptor names no ADDRESS')
    if len(addresses) > 1:
        raise NotSupportedError(
            'a connect descriptor with several addresses is not supported yet (#1983)'
        )
    address = _branch(addresses[0], 'address')
    _refuse_unread(address, _ADDRESS_KEYS, 'ADDRESS parameter')
    protocol = _find_first(address, 'protocol')
    if isinstance(protocol, str) and protocol.lower() != 'tcp':
        raise NotSupportedError(
            f'PROTOCOL={protocol} in a connect descriptor is not supported yet; '
            'use ssl= for TCPS'
        )
    host = _find_first(address, 'host')
    if not isinstance(host, str) or not host:
        raise NetConfigError('the connect descriptor names no HOST')
    port = _find_first(address, 'port')
    if port is None:
        port_number = _DEFAULT_PORT
    elif isinstance(port, str) and port.isdigit():
        port_number = int(port)
    else:
        raise NetConfigError(f'PORT={port!r} in the connect descriptor is not a number')
    connect_data = [v for k, v in description if k == 'connect_data']
    service_name = sid = ''
    if connect_data:
        data = _branch(connect_data[0], 'connect_data')
        _refuse_unread(data, _CONNECT_DATA_KEYS, 'CONNECT_DATA parameter')
        found = _find_first(data, 'service_name')
        service_name = found if isinstance(found, str) else ''
        found = _find_first(data, 'sid')
        sid = found if isinstance(found, str) else ''
    return DescriptorTarget(host, port_number, service_name, sid)
