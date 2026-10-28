# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The ``ws://`` / ``wss://`` transport, a seerdb extension (#1988).

TNS framing carried in WebSocket binary messages. The transport is a **byte
pipe**: the existing TNS packet framer in :mod:`seerdb.client.connection` and
:mod:`seerdb.client.aconnection` reassembles packets from it exactly as it does
over a TCP socket, so the TNS and TTC layers are untouched. No Oracle client or
server speaks this -- it reaches a WebSocket bridge or a seerdb Mirror that
does. See the ``Transports`` note in ``PROTOCOL.md``.

Two adapters keep the connection code unchanged:

* :class:`WsSocket` quacks like a :class:`socket.socket` for the sync
  connection -- ``send`` / ``sendall`` / ``recv`` / ``settimeout`` /
  ``shutdown`` / ``close``. A WebSocket has no out-of-band data, so the sync
  connection never advertises OOB (``_supports_oob`` stays False) and break
  rides the in-band ``TNS_MARKER_TYPE_INTERRUPT`` packet.
* :class:`WsStreamReader` / :class:`WsStreamWriter` present the slice of the
  asyncio ``StreamReader`` / ``StreamWriter`` surface the async connection uses.

This needs the optional ``websockets`` dependency (the ``ws`` extra).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from seerdb.common.exceptions import NotSupportedError
from seerdb.common.ws_transport import WsSocket

if TYPE_CHECKING:
    import ssl as _ssl

__all__ = [
    'WsSocket',
    'WsStreamReader',
    'WsStreamWriter',
    'connect_ws',
    'connect_ws_async',
    'ssl_context_for',
]

_INSTALL_HINT = (
    "the ws:// / wss:// transport needs the 'websockets' package; "
    'install seerdb with the "ws" extra (pip install seerdb[ws])'
)

# TNS packets can exceed websockets' 1 MiB default message cap (a large array
# bind), and the byte pipe carries whatever the framer wrote; lift the cap.
_NO_MESSAGE_CAP = None


def _import_websockets() -> Any:
    try:
        import websockets  # noqa: F401
    except ImportError as Exc:  # pragma: no cover - exercised via connect paths
        raise NotSupportedError(_INSTALL_HINT) from Exc
    return websockets


def _ws_uri(protocol: str, host: str, port: int, path: str) -> str:
    Scheme = 'wss' if protocol == 'wss' else 'ws'
    if not path.startswith('/'):
        path = '/' + path
    # A bracketed IPv6 literal keeps its brackets in the URI authority.
    Authority = f'[{host}]' if ':' in host and not host.startswith('[') else host
    return f'{Scheme}://{Authority}:{port}{path}'


def ssl_context_for(ssl_arg: Any) -> _ssl.SSLContext | None:
    """Resolve a connection's ``ssl`` argument into an :class:`ssl.SSLContext`
    for a ``wss://`` WebSocket, from the same shapes the TCPS path accepts.

    None lets ``websockets`` build a secure default context; a dict builds one
    (``ca_certs`` / ``certfile`` / ``keyfile`` / ``check_hostname`` /
    ``verify_mode``); an :class:`ssl.SSLContext` is used verbatim; True is a
    secure default context. Hostname verification is the WebSocket host's."""
    import ssl as _ssl_mod

    if ssl_arg is None:
        return None
    if isinstance(ssl_arg, _ssl_mod.SSLContext):
        return ssl_arg
    if isinstance(ssl_arg, dict):
        Opts = dict(ssl_arg)
        Opts.pop('server_hostname', None)  # SNI is the WebSocket host
        Ctx = _ssl_mod.create_default_context(cafile=Opts.pop('ca_certs', None))
        Ctx.minimum_version = _ssl_mod.TLSVersion.TLSv1_2
        if 'check_hostname' in Opts:
            Ctx.check_hostname = bool(Opts.pop('check_hostname'))
        if 'verify_mode' in Opts:
            Ctx.verify_mode = Opts.pop('verify_mode')
        CertFile = Opts.pop('certfile', None)
        KeyFile = Opts.pop('keyfile', None)
        if CertFile:
            Ctx.load_cert_chain(CertFile, KeyFile)
        if Opts:
            raise ValueError(f'unknown ssl options: {sorted(Opts)}')
        return Ctx
    return _ssl_mod.create_default_context()


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #


def connect_ws(
    protocol: str,
    host: str,
    port: int,
    path: str,
    ssl_context: _ssl.SSLContext | None,
    open_timeout: float | None,
) -> WsSocket:
    """Open a sync ``ws://`` / ``wss://`` connection and wrap it as a socket.

    ``ssl_context`` is used for ``wss://`` (None lets ``websockets`` build a
    secure default context); it is ignored for ``ws://``."""
    _import_websockets()
    from websockets.sync.client import connect as _connect

    Uri = _ws_uri(protocol, host, port, path)
    Kwargs: dict[str, Any] = {'max_size': _NO_MESSAGE_CAP, 'open_timeout': open_timeout}
    if protocol == 'wss':
        Kwargs['ssl'] = ssl_context
    Conn = _connect(Uri, **Kwargs)
    return WsSocket(Conn)


# --------------------------------------------------------------------------- #
# Async
# --------------------------------------------------------------------------- #


class WsStreamReader:
    """The ``StreamReader.read`` the async connection uses, over a byte pipe
    fed by a ``websockets`` async connection's binary messages."""

    def __init__(self, Conn: Any) -> None:
        self._conn = Conn
        self._buf = b''
        self._eof = False

    async def read(self, Size: int = -1) -> bytes:
        from websockets.exceptions import ConnectionClosed

        while not self._buf and not self._eof:
            try:
                Message = await self._conn.recv()
            except ConnectionClosed:
                self._eof = True
                return b''
            if isinstance(Message, str):
                Message = Message.encode('utf-8')
            self._buf = Message
        if Size is None or Size < 0:
            (Chunk, self._buf) = (self._buf, b'')
        else:
            (Chunk, self._buf) = (self._buf[:Size], self._buf[Size:])
        return Chunk


class _WsAsyncTransport:
    """The sliver of ``asyncio.Transport`` the async connection reaches for a
    hard abort and for ``get_extra_info``."""

    def __init__(self, Conn: Any) -> None:
        self._conn = Conn

    def abort(self) -> None:
        Inner = getattr(self._conn, 'transport', None)
        if Inner is not None:
            Inner.abort()

    def get_extra_info(self, Name: str, Default: Any = None) -> Any:
        Inner = getattr(self._conn, 'transport', None)
        if Inner is not None:
            return Inner.get_extra_info(Name, Default)
        return Default


class WsStreamWriter:
    """The ``StreamWriter`` surface the async connection uses: write / drain /
    close / wait_closed / get_extra_info, and ``can_write_eof`` -> False so the
    half-close branch is skipped (a WebSocket has none)."""

    def __init__(self, Conn: Any) -> None:
        self._conn = Conn
        self._pending: list[bytes] = []
        self.transport = _WsAsyncTransport(Conn)

    def write(self, Data: Any) -> None:
        self._pending.append(bytes(Data))

    async def drain(self) -> None:
        (Pending, self._pending) = (self._pending, [])
        for Payload in Pending:
            await self._conn.send(Payload)

    def can_write_eof(self) -> bool:
        return False

    def write_eof(self) -> None:  # pragma: no cover - guarded by can_write_eof
        pass

    def get_extra_info(self, Name: str, Default: Any = None) -> Any:
        return self.transport.get_extra_info(Name, Default)

    def close(self) -> None:
        import asyncio

        try:
            Loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # websockets' close is a coroutine; schedule it so this sync close()
        # matches StreamWriter.close(). wait_closed() awaits the real teardown.
        self._close_task = Loop.create_task(self._conn.close())

    async def wait_closed(self) -> None:
        await self._conn.close()


async def connect_ws_async(
    protocol: str,
    host: str,
    port: int,
    path: str,
    ssl_context: _ssl.SSLContext | None,
    open_timeout: float | None,
) -> tuple[WsStreamReader, WsStreamWriter]:
    """Open an async ``ws://`` / ``wss://`` connection as a reader/writer pair."""
    _import_websockets()
    from websockets.asyncio.client import connect as _connect

    Uri = _ws_uri(protocol, host, port, path)
    Kwargs: dict[str, Any] = {'max_size': _NO_MESSAGE_CAP, 'open_timeout': open_timeout}
    if protocol == 'wss':
        Kwargs['ssl'] = ssl_context
    Conn = await _connect(Uri, **Kwargs)
    return (WsStreamReader(Conn), WsStreamWriter(Conn))
