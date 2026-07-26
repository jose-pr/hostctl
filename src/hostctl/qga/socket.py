"""Direct Unix-domain socket transport for QEMU Guest Agent."""

from __future__ import annotations

import itertools
import json
import secrets
import socket
import threading
import time
import typing

from ._common import (
    QgaCommandError,
    QgaDisconnectedError,
    QgaProtocolError,
    QgaTimeoutError,
)


class UnixSocketGuestAgentTransport:
    """Persistent QGA connection with synchronization and request correlation."""

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
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_reply_size <= 0:
            raise ValueError("max_reply_size must be greater than zero")
        self.path = path
        self.timeout = float(timeout)
        self.max_reply_size = max_reply_size
        self._socket_factory = socket_factory
        self._uses_system_socket = socket_factory is socket.socket
        self._socket: typing.Optional[socket.socket] = None
        self._buffer = bytearray()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def __enter__(self) -> UnixSocketGuestAgentTransport:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: object,
    ) -> bool:
        self.close()
        return False

    def connect(self) -> None:
        """Connect and discard stale/partial data using ``guest-sync-delimited``."""
        with self._lock:
            if self._socket is None:
                self._connect(time.monotonic() + self.timeout)

    def close(self) -> None:
        with self._lock:
            self._disconnect()

    def execute(
        self,
        request: typing.Mapping[str, object],
        timeout: typing.Optional[float] = None,
    ) -> object:
        """Execute one request and return its unwrapped ``return`` member."""
        command = request.get("execute")
        if not isinstance(command, str) or not command:
            raise ValueError("QGA request requires a non-empty 'execute' string")
        request_timeout = self.timeout if timeout is None else float(timeout)
        if request_timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        deadline = time.monotonic() + request_timeout
        with self._lock:
            try:
                if self._socket is None:
                    self._connect(deadline)
                request_id = next(self._ids)
                payload = dict(request)
                payload["id"] = request_id
                self._send(payload, deadline)
                reply = self._correlated_reply(request_id, deadline)
                return self._unwrap(reply)
            except (QgaTimeoutError, QgaDisconnectedError, QgaProtocolError):
                self._disconnect()
                raise
            except socket.timeout as exc:
                self._disconnect()
                raise QgaTimeoutError(f"QGA command {command!r} timed out") from exc
            except OSError:
                self._disconnect()
                raise

    def _connect(self, deadline: float) -> None:
        if not hasattr(socket, "AF_UNIX") and self._uses_system_socket:
            raise NotImplementedError(
                "direct QGA sockets require Unix-domain socket support"
            )
        family = getattr(socket, "AF_UNIX", 1)
        connection = self._socket_factory(family, socket.SOCK_STREAM)
        self._socket = connection
        self._buffer.clear()
        try:
            connection.settimeout(self._remaining(deadline))
            connection.connect(self.path)
            self._synchronize(deadline)
        except BaseException:
            self._disconnect()
            raise

    def _disconnect(self) -> None:
        connection, self._socket = self._socket, None
        self._buffer.clear()
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _synchronize(self, deadline: float) -> None:
        token = secrets.randbits(52)
        request_id = f"sync-{next(self._ids)}"
        self._send(
            {
                "execute": "guest-sync-delimited",
                "arguments": {"id": token},
                "id": request_id,
            },
            deadline,
            prefix=b"\xff",
        )
        self._discard_until_delimiter(deadline)
        reply = self._correlated_reply(request_id, deadline)
        returned = self._unwrap(reply)
        if returned != token:
            raise QgaProtocolError("QGA synchronization token did not match")

    def _send(
        self,
        request: typing.Mapping[str, object],
        deadline: float,
        *,
        prefix: bytes = b"",
    ) -> None:
        try:
            encoded = json.dumps(
                request, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("QGA request is not JSON serializable") from exc
        connection = self._require_socket()
        try:
            connection.settimeout(self._remaining(deadline))
            connection.sendall(prefix + encoded + b"\n")
        except socket.timeout as exc:
            raise QgaTimeoutError("timed out sending a QGA request") from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise QgaDisconnectedError("QGA disconnected while sending") from exc

    def _discard_until_delimiter(self, deadline: float) -> None:
        while True:
            position = self._buffer.find(b"\xff")
            if position >= 0:
                del self._buffer[: position + 1]
                return
            if len(self._buffer) > self.max_reply_size:
                raise QgaProtocolError("QGA synchronization data exceeded size limit")
            self._receive(deadline)

    def _correlated_reply(
        self, request_id: object, deadline: float
    ) -> typing.Mapping[str, object]:
        while True:
            reply = self._read_reply(deadline)
            if reply.get("id") == request_id:
                return reply

    def _read_reply(self, deadline: float) -> typing.Mapping[str, object]:
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > self.max_reply_size:
                    raise QgaProtocolError("QGA reply exceeded size limit")
                self._receive(deadline)
                continue
            frame = bytes(self._buffer[:newline]).lstrip(b"\xff")
            del self._buffer[: newline + 1]
            if not frame:
                continue
            if len(frame) > self.max_reply_size:
                raise QgaProtocolError("QGA reply exceeded size limit")
            try:
                reply = json.loads(frame.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise QgaProtocolError("QGA returned malformed JSON") from exc
            if not isinstance(reply, dict):
                raise QgaProtocolError("QGA reply must be a JSON object")
            return typing.cast(typing.Mapping[str, object], reply)

    def _receive(self, deadline: float) -> None:
        connection = self._require_socket()
        try:
            connection.settimeout(self._remaining(deadline))
            chunk = connection.recv(min(65536, self.max_reply_size + 1))
        except socket.timeout as exc:
            raise QgaTimeoutError("timed out waiting for a QGA reply") from exc
        except (ConnectionResetError, ConnectionAbortedError) as exc:
            raise QgaDisconnectedError("QGA disconnected while receiving") from exc
        if not chunk:
            raise QgaDisconnectedError("QGA disconnected before replying")
        self._buffer.extend(chunk)

    def _unwrap(self, reply: typing.Mapping[str, object]) -> object:
        if "error" in reply:
            error = reply["error"]
            if not isinstance(error, dict):
                raise QgaProtocolError("QGA error reply must contain an object")
            error_class = error.get("class", "GenericError")
            description = error.get("desc", "QGA command failed")
            if not isinstance(error_class, str) or not isinstance(description, str):
                raise QgaProtocolError("QGA error fields must be strings")
            return_data = {
                key: value
                for key, value in error.items()
                if key not in {"class", "desc"}
            }
            raise QgaCommandError(error_class, description, data=return_data)
        if "return" not in reply:
            raise QgaProtocolError("QGA reply has neither 'return' nor 'error'")
        return reply["return"]

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QgaTimeoutError("QGA request deadline expired")
        return remaining

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise QgaDisconnectedError("QGA transport is not connected")
        return self._socket
