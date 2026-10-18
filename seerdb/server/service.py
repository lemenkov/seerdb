# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The Mirror server — accept Oracle clients and serve them from a backend.

The public entry point (``seerdb.serve`` / ``seerdb.Server``) ties the
handshake, O5LOGON auth, and query loop (:func:`serve_session`) to a credential
map and a **per-session backend factory**: one fresh :class:`Backend` per client
connection (the model both SQLite — thread-affine — and PostgreSQL want). Each
connection is handled on its own thread.
"""

from __future__ import annotations

import logging
import socket
import ssl
import threading
from collections.abc import Callable

from seerdb.common.tns_consts import FIELD_VERSION_11_2
from seerdb.server.backend import Backend
from seerdb.server.framing import PacketStream, listen_socket
from seerdb.server.session import serve_session

logger = logging.getLogger('seerdb.server')

# Called once per client connection to open its backend session. The backend it
# returns owns auth too (its ``authenticate``), so the Server holds no
# credentials of its own.
BackendFactory = Callable[[], Backend]

_BACKLOG = 16

# How long a client has to finish the TLS handshake (#1834). The handshake runs
# on the session's thread, so a peer that connects and says nothing holds only
# that thread, and only this long.
_TLS_HANDSHAKE_SECONDS = 30.0

# Logged as a Mirror starts serving. A WARNING reaches stderr even when the
# application configured no logging.
_PROVISIONAL_NOTICE = (
    'the Mirror is a highly experimental technology preview, not for production: '
    'it may change or break in any seerdb release, '
    'outside the version promise the client keeps'
)


class Server:
    """A Mirror listening socket. Binds on construction (so ``port=0`` yields an
    ephemeral port readable as :attr:`port` before serving); serve on a thread
    or call :meth:`serve_forever` directly, and :meth:`close` to stop."""

    def __init__(
        self,
        host: str = '127.0.0.1',
        port: int = 1521,
        *,
        backend_factory: BackendFactory,
        field_version: int = FIELD_VERSION_11_2,
        tns_version: int | None = None,
        encryption: str = 'accepted',
        ssl_context: ssl.SSLContext | None = None,
        max_sessions: int | None = None,
    ) -> None:
        self._backend_factory = backend_factory
        # How many connections are served at once (GHSA-5827-wqfh-x2v3); one
        # more is closed as it arrives. None serves any number, as before.
        self._slots = threading.BoundedSemaphore(max_sessions) if max_sessions else None
        # The Mirror's ANO stance (§33): 'accepted', 'requested' or 'required'.
        self._encryption = encryption
        # TCPS (#1834): with a server context, every connection is TLS from its
        # first byte, as an Oracle TCPS listener's is; the TNS handshake runs
        # inside it.
        self._ssl_context = ssl_context
        # The field version the Mirror advertises to thin clients (default the
        # pinned 11.2). Higher values unlock the 12c+ / 23ai wire formats a client
        # gates on that version; the login path handles them, the query path is
        # being brought up format by format.
        self._field_version = field_version
        self._tns_version = tns_version
        self._running = True
        self._sock = listen_socket(host, port, _BACKLOG)
        # A short accept timeout lets close() stop the loop promptly (a plain
        # close() does not reliably wake a thread blocked in accept()).
        self._sock.settimeout(0.5)
        # An IPv6 address names itself in four fields, IPv4 in two (#2001).
        (self.host, self.port) = self._sock.getsockname()[:2]

    def serve_forever(self) -> None:
        """Accept and serve connections until :meth:`close` (or an interrupt)."""
        logger.warning(_PROVISIONAL_NOTICE)
        logger.info(
            'Mirror listening on %s:%d%s',
            self.host,
            self.port,
            ' (TCPS)' if self._ssl_context is not None else '',
        )
        while self._running:
            try:
                client, addr = self._sock.accept()
            except TimeoutError:
                continue  # poll the running flag
            except OSError:
                break  # the socket was closed
            if self._slots is not None and not self._slots.acquire(blocking=False):
                logger.warning(
                    'refused %s:%d: already serving the most sessions allowed',
                    addr[0],
                    addr[1],
                )
                client.close()
                continue
            threading.Thread(
                target=self._handle, args=(client, addr), daemon=True
            ).start()

    def _handle(self, client: socket.socket, addr: tuple) -> None:
        # The connection's slot is free again however it ends, a failed TLS
        # handshake included.
        try:
            self._serve(client, addr)
        finally:
            if self._slots is not None:
                self._slots.release()

    def _serve(self, client: socket.socket, addr: tuple) -> None:
        if self._ssl_context is not None:
            try:
                client.settimeout(_TLS_HANDSHAKE_SECONDS)
                client = self._ssl_context.wrap_socket(client, server_side=True)
                client.settimeout(None)
            except OSError as exc:  # ssl.SSLError, a timeout, a reset
                logger.warning(
                    'TLS handshake with %s:%d failed: %s', addr[0], addr[1], exc
                )
                client.close()
                return
        backend = None
        try:
            backend = self._backend_factory()
            user = serve_session(
                PacketStream(client),
                backend,
                encryption=self._encryption,
                field_version=self._field_version,
                tns_version=self._tns_version,
            )
            logger.info('%s:%d session ended (%s)', addr[0], addr[1], user)
        except Exception:
            logger.exception('session error from %s:%d', addr[0], addr[1])
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    logger.debug('backend close failed', exc_info=True)
            client.close()

    def close(self) -> None:
        """Stop accepting and release the listening socket."""
        self._running = False
        self._sock.close()


def serve(
    host: str = '127.0.0.1',
    port: int = 1521,
    *,
    backend_factory: BackendFactory,
    field_version: int = FIELD_VERSION_11_2,
    tns_version: int | None = None,
    encryption: str = 'accepted',
    ssl_context: ssl.SSLContext | None = None,
    max_sessions: int | None = None,
) -> None:
    """Run a Mirror server until interrupted — the one-call convenience."""
    server = Server(
        host,
        port,
        backend_factory=backend_factory,
        field_version=field_version,
        tns_version=tns_version,
        encryption=encryption,
        ssl_context=ssl_context,
        max_sessions=max_sessions,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
