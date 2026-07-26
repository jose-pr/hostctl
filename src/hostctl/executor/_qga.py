"""QEMU Guest Agent transport contracts and normalized errors."""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import secrets
import socket
import subprocess
import threading
import time
import typing


class QgaError(Exception):
    """Base class for QEMU Guest Agent failures."""


class QgaProtocolError(QgaError, ConnectionError):
    """The guest agent returned malformed or inconsistent protocol data."""


class QgaDisconnectedError(QgaError, ConnectionError):
    """The guest-agent transport disconnected before producing a reply."""


class QgaTimeoutError(QgaError, TimeoutError):
    """A guest-agent request did not complete before its deadline."""


class QgaCommandError(QgaError):
    """A structured QGA command error returned by the guest."""

    def __init__(
        self,
        error_class: str,
        description: str,
        *,
        data: typing.Optional[typing.Mapping[str, object]] = None,
    ) -> None:
        super().__init__(f"{error_class}: {description}")
        self.error_class = error_class
        self.description = description
        self.data = dict(data or {})


class _QgaFramedSession:
    """Shared QGA JSON-line framing and request lifecycle.

    Concrete transports only provide raw byte-stream operations.  Keeping the
    parser here ensures partial reads, synchronization, correlation, and error
    unwrapping behave identically for Unix sockets and SSH tunnels.
    """

    def __init__(self, *, timeout: float, max_reply_size: int) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_reply_size <= 0:
            raise ValueError("max_reply_size must be greater than zero")
        self.timeout = float(timeout)
        self.max_reply_size = int(max_reply_size)
        self._buffer = bytearray()
        self._ids = iter(range(1, 2**63))
        self._session_lock = threading.Lock()
        self._connected = False

    def connect(self) -> None:
        with self._session_lock:
            if self._connected:
                return
            deadline = time.monotonic() + self.timeout
            try:
                self._connect_raw(deadline)
                self._buffer.clear()
                self._connected = True
                self._synchronize(deadline)
            except Exception:
                self._disconnect_locked()
                raise

    def close(self) -> None:
        with self._session_lock:
            self._disconnect_locked()

    def execute(
        self,
        request: typing.Mapping[str, object],
        timeout: typing.Optional[float] = None,
    ) -> object:
        command = request.get("execute")
        if not isinstance(command, str) or not command:
            raise ValueError("QGA request requires a non-empty 'execute' string")
        request_timeout = self.timeout if timeout is None else float(timeout)
        if request_timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        deadline = time.monotonic() + request_timeout
        with self._session_lock:
            try:
                if not self._connected:
                    self._connect_raw(deadline)
                    self._buffer.clear()
                    self._connected = True
                    self._synchronize(deadline)
                request_id = next(self._ids)
                payload = dict(request)
                payload["id"] = request_id
                self._send(payload, deadline)
                return self._unwrap(self._correlated_reply(request_id, deadline))
            except Exception:
                self._disconnect_locked()
                raise

    def _disconnect_locked(self) -> None:
        self._connected = False
        self._buffer.clear()
        try:
            self._close_raw()
        except Exception:
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
        returned = self._unwrap(self._correlated_reply(request_id, deadline))
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
        self._send_raw(prefix + encoded + b"\n", deadline)

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
            # Some QGA versions omit id on parse errors.  Treat that as the
            # response for the oldest outstanding request instead of spinning.
            if "id" not in reply and "error" in reply:
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
        chunk = self._recv_raw(min(65536, self.max_reply_size + 1), deadline)
        if not isinstance(chunk, bytes):
            raise QgaProtocolError("QGA transport returned non-byte data")
        if not chunk:
            raise QgaDisconnectedError("QGA disconnected before replying")
        self._buffer.extend(chunk)

    @staticmethod
    def _unwrap(reply: typing.Mapping[str, object]) -> object:
        if "error" in reply:
            error = reply["error"]
            if not isinstance(error, dict):
                raise QgaProtocolError("QGA error reply must contain an object")
            error_class = error.get("class", "GenericError")
            description = error.get("desc", "QGA command failed")
            if not isinstance(error_class, str) or not isinstance(description, str):
                raise QgaProtocolError("QGA error fields must be strings")
            raise QgaCommandError(
                error_class,
                description,
                data={k: v for k, v in error.items() if k not in {"class", "desc"}},
            )
        if "return" not in reply:
            raise QgaProtocolError("QGA reply has neither 'return' nor 'error'")
        return reply["return"]

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QgaTimeoutError("QGA request deadline expired")
        return remaining

    def _connect_raw(self, deadline: float) -> None:
        raise NotImplementedError

    def _send_raw(self, data: bytes, deadline: float) -> None:
        raise NotImplementedError

    def _recv_raw(self, size: int, deadline: float) -> bytes:
        raise NotImplementedError

    def _close_raw(self) -> None:
        raise NotImplementedError


@typing.runtime_checkable
class GuestAgentTransport(typing.Protocol):
    """Synchronous transport for one correlated QGA request."""

    def execute(
        self,
        request: typing.Mapping[str, object],
        timeout: typing.Optional[float] = None,
    ) -> object:
        """Return the unwrapped QGA ``return`` value."""

    def close(self) -> None:
        """Release transport resources."""


def normalize_libvirt_error(error: BaseException) -> BaseException:
    """Normalize common libvirt/QGA failures without importing libvirt eagerly."""
    if not isinstance(error, Exception):
        return error
    message = str(error)
    folded = message.casefold()
    if any(value in folded for value in ("permission denied", "access denied")):
        return PermissionError(message)
    if any(
        value in folded
        for value in ("domain not found", "no domain with matching name")
    ):
        return FileNotFoundError(message)
    if "timed out" in folded or "timeout" in folded:
        return QgaTimeoutError(message)
    return ConnectionError(message)


class LibvirtGuestAgentTransport:
    """Issue QGA requests through ``virDomainQemuAgentCommand``."""

    def __init__(
        self,
        domain: str,
        *,
        connection_uri: typing.Optional[str] = None,
        timeout: float = 10.0,
        connect_factory: typing.Optional[
            typing.Callable[[typing.Optional[str]], object]
        ] = None,
        command_factory: typing.Optional[
            typing.Callable[[object, str, int, int], str]
        ] = None,
    ) -> None:
        if not domain:
            raise ValueError("libvirt domain must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.domain_name = domain
        self.connection_uri = connection_uri
        self.timeout = float(timeout)
        self._connect_factory = connect_factory
        self._command_factory = command_factory
        self._connection: typing.Optional[object] = None
        self._domain: typing.Optional[object] = None
        self._ids = itertools.count(1)

    def __enter__(self) -> LibvirtGuestAgentTransport:
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
        if self._domain is not None:
            return
        connect_factory = self._connect_factory
        command_factory = self._command_factory
        if connect_factory is None or command_factory is None:
            try:
                import libvirt
                import libvirt_qemu
            except ImportError as exc:
                raise ImportError(
                    "libvirt QGA support requires the 'qemu-libvirt' extra"
                ) from exc
            connect_factory = connect_factory or libvirt.open
            command_factory = command_factory or libvirt_qemu.qemuAgentCommand
        connection = None
        try:
            connection = connect_factory(self.connection_uri)
            if connection is None:
                raise ConnectionError("libvirt returned no connection")
            domain = connection.lookupByName(self.domain_name)
            if not domain.isActive():
                close = getattr(connection, "close", None)
                if close is not None:
                    close()
                raise ConnectionError(
                    f"libvirt domain {self.domain_name!r} is not active"
                )
        except (ConnectionError, FileNotFoundError, PermissionError):
            if connection is not None:
                try:
                    close = getattr(connection, "close", None)
                    if close is not None:
                        close()
                except Exception:
                    pass
            raise
        except Exception as exc:
            try:
                if connection is not None:
                    close = getattr(connection, "close", None)
                    if close is not None:
                        close()
            except Exception:
                pass
            normalized = normalize_libvirt_error(exc)
            if normalized is exc:
                raise
            raise normalized from exc
        self._connect_factory = connect_factory
        self._command_factory = command_factory
        self._connection = connection
        self._domain = domain

    def close(self) -> None:
        connection, self._connection = self._connection, None
        self._domain = None
        if connection is not None:
            close = getattr(connection, "close", None)
            if close is not None:
                close()

    def execute(
        self,
        request: typing.Mapping[str, object],
        timeout: typing.Optional[float] = None,
    ) -> object:
        command = request.get("execute")
        if not isinstance(command, str) or not command:
            raise ValueError("QGA request requires a non-empty 'execute' string")
        request_timeout = self.timeout if timeout is None else float(timeout)
        if request_timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.connect()
        request_id = next(self._ids)
        payload = dict(request)
        payload["id"] = request_id
        assert self._domain is not None
        assert self._command_factory is not None
        try:
            raw = self._command_factory(
                self._domain,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                max(1, math.ceil(request_timeout)),
                0,
            )
        except Exception as exc:
            normalized = normalize_libvirt_error(exc)
            if normalized is exc:
                raise
            raise normalized from exc
        try:
            reply = json.loads(raw)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QgaProtocolError("libvirt returned malformed QGA JSON") from exc
        if not isinstance(reply, dict):
            raise QgaProtocolError("QGA reply must be a JSON object")
        if reply.get("id") not in (None, request_id):
            raise QgaProtocolError("libvirt returned a mismatched QGA reply")
        return _QgaFramedSession._unwrap(reply)


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


class _Reader(typing.Protocol):
    def read(self, size: int) -> typing.Awaitable[bytes]: ...


class _Writer(typing.Protocol):
    def write(self, value: bytes) -> None: ...

    def drain(self) -> typing.Awaitable[None]: ...

    def close(self) -> None: ...


class _SshConnection(typing.Protocol):
    def open_unix_connection(
        self, path: str, *, encoding: typing.Optional[str] = None
    ) -> typing.Awaitable[typing.Tuple[_Reader, _Writer]]: ...


async def _wait(awaitable: typing.Awaitable[object], timeout: float) -> object:
    return await asyncio.wait_for(awaitable, timeout)


async def _close_writer(writer: _Writer) -> None:
    writer.close()
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is not None:
        result = wait_closed()
        if hasattr(result, "__await__"):
            await result


class SshUnixGuestAgentTransport(_QgaFramedSession):
    """Persistent QGA stream opened on a remote Unix socket over SSH."""

    def __init__(
        self,
        path: str,
        connection: typing.Callable[[], _SshConnection],
        *,
        timeout: float = 10.0,
        max_reply_size: int = 8 * 1024 * 1024,
    ) -> None:
        if not path:
            raise ValueError("remote QGA socket path must not be empty")
        self.path = path
        self._connection = connection
        self._reader: typing.Optional[_Reader] = None
        self._writer: typing.Optional[_Writer] = None
        super().__init__(timeout=timeout, max_reply_size=max_reply_size)

    def __enter__(self) -> SshUnixGuestAgentTransport:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

    def _connect_raw(self, deadline: float) -> None:
        from .. import _async

        try:
            opened = _async.async_to_sync(
                _wait(
                    self._connection().open_unix_connection(self.path, encoding=None),
                    self._remaining(deadline),
                )
            )
            self._reader, self._writer = typing.cast(
                typing.Tuple[_Reader, _Writer], opened
            )
        except Exception as exc:
            normalized = self._normalize_ssh_error(exc, "open QGA stream")
            if normalized is exc:
                raise
            raise normalized from exc

    def _send_raw(self, data: bytes, deadline: float) -> None:
        from .. import _async

        writer = self._require_writer()

        async def send() -> None:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), self._remaining(deadline))

        try:
            _async.async_to_sync(send())
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise QgaTimeoutError("timed out sending a QGA request") from exc
        except Exception as exc:
            normalized = self._normalize_ssh_error(exc, "send QGA request")
            if normalized is exc:
                raise
            raise normalized from exc

    def _recv_raw(self, size: int, deadline: float) -> bytes:
        from .. import _async

        try:
            chunk = _async.async_to_sync(
                _wait(self._require_reader().read(size), self._remaining(deadline))
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise QgaTimeoutError("timed out waiting for a QGA reply") from exc
        except Exception as exc:
            normalized = self._normalize_ssh_error(exc, "receive QGA reply")
            if normalized is exc:
                raise
            raise normalized from exc
        if not isinstance(chunk, bytes):
            raise TypeError("SSH QGA stream returned non-byte data")
        return chunk

    def _close_raw(self) -> None:
        from .. import _async

        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            try:
                _async.async_to_sync(_close_writer(writer))
            except Exception:
                pass

    def _require_reader(self) -> _Reader:
        if self._reader is None:
            raise QgaDisconnectedError("QGA transport is not connected")
        return self._reader

    def _require_writer(self) -> _Writer:
        if self._writer is None:
            raise QgaDisconnectedError("QGA transport is not connected")
        return self._writer

    @staticmethod
    def _normalize_ssh_error(exc: Exception, command: str) -> Exception:
        from .. import _async

        normalized = _async.normalize_asyncssh_error(exc, command=command)
        if isinstance(normalized, subprocess.TimeoutExpired):
            return QgaTimeoutError(str(normalized))
        if isinstance(normalized, ConnectionError):
            return QgaDisconnectedError(str(normalized))
        return normalized
