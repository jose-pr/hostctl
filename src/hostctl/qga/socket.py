"""Direct Unix-domain socket transport for QEMU Guest Agent."""

from __future__ import annotations

import socket
import typing

from ._common import QgaDisconnectedError, QgaTimeoutError, _QgaFramedSession


class UnixSocketGuestAgentTransport(_QgaFramedSession):
    """Persistent QGA connection over a Unix-domain socket."""

    def __init__(
        self,
        path: str,
        *,
        timeout: float = 10.0,
        max_reply_size: int = 8 * 1024 * 1024,
        socket_factory: typing.Callable[..., socket.socket] = socket.socket,
    ) -> None:
        if not path:
            raise ValueError("QGA socket path must not be empty")
        self.path = path
        self._socket_factory = socket_factory
        self._uses_system_socket = socket_factory is socket.socket
        self._socket: typing.Optional[socket.socket] = None
        super().__init__(timeout=timeout, max_reply_size=max_reply_size)

    def __enter__(self) -> UnixSocketGuestAgentTransport:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

    def _connect_raw(self, deadline: float) -> None:
        if not hasattr(socket, "AF_UNIX") and self._uses_system_socket:
            raise NotImplementedError(
                "direct QGA sockets require Unix-domain socket support"
            )
        family = getattr(socket, "AF_UNIX", 1)
        connection = self._socket_factory(family, socket.SOCK_STREAM)
        self._socket = connection
        try:
            connection.settimeout(self._remaining(deadline))
            connection.connect(self.path)
        except Exception:
            try:
                connection.close()
            finally:
                self._socket = None
            raise

    def _send_raw(self, data: bytes, deadline: float) -> None:
        connection = self._require_socket()
        try:
            connection.settimeout(self._remaining(deadline))
            connection.sendall(data)
        except socket.timeout as exc:
            raise QgaTimeoutError("timed out sending a QGA request") from exc
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            raise QgaDisconnectedError("QGA disconnected while sending") from exc

    def _recv_raw(self, size: int, deadline: float) -> bytes:
        connection = self._require_socket()
        try:
            connection.settimeout(self._remaining(deadline))
            return connection.recv(size)
        except socket.timeout as exc:
            raise QgaTimeoutError("timed out waiting for a QGA reply") from exc
        except (ConnectionResetError, ConnectionAbortedError) as exc:
            raise QgaDisconnectedError("QGA disconnected while receiving") from exc

    def _close_raw(self) -> None:
        connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise QgaDisconnectedError("QGA transport is not connected")
        return self._socket
