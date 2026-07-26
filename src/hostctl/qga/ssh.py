"""QGA Unix-socket transport tunneled through an AsyncSSH connection."""

from __future__ import annotations

import asyncio
import itertools
import json
import secrets
import subprocess
import threading
import time
import typing

from ._common import (
    QgaCommandError,
    QgaDisconnectedError,
    QgaProtocolError,
    QgaTimeoutError,
)


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


class SshUnixGuestAgentTransport:
    """Persistent QGA stream opened on a remote Unix socket over SSH.

    The SSH connection remains caller-owned. Closing this transport closes only
    its direct-stream channel.
    """

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
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_reply_size <= 0:
            raise ValueError("max_reply_size must be greater than zero")
        self.path = path
        self.timeout = float(timeout)
        self.max_reply_size = max_reply_size
        self._connection = connection
        self._reader: typing.Optional[_Reader] = None
        self._writer: typing.Optional[_Writer] = None
        self._buffer = bytearray()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def __enter__(self) -> SshUnixGuestAgentTransport:
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
        with self._lock:
            if self._reader is None:
                self._connect(time.monotonic() + self.timeout)

    def close(self) -> None:
        with self._lock:
            self._disconnect()

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
        with self._lock:
            try:
                if self._reader is None:
                    self._connect(deadline)
                request_id = next(self._ids)
                payload = dict(request)
                payload["id"] = request_id
                self._send(payload, deadline)
                return self._unwrap(self._correlated_reply(request_id, deadline))
            except (QgaTimeoutError, QgaDisconnectedError, QgaProtocolError):
                self._disconnect()
                raise
            except Exception as exc:
                self._disconnect()
                normalized = self._normalize_ssh_error(exc, command)
                if normalized is exc:
                    raise
                raise normalized from exc

    def _connect(self, deadline: float) -> None:
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
            self._buffer.clear()
            self._synchronize(deadline)
        except BaseException:
            self._disconnect()
            raise

    def _disconnect(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        self._buffer.clear()
        if writer is not None:
            writer.close()

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
        from .. import _async

        try:
            encoded = json.dumps(
                request, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("QGA request is not JSON serializable") from exc
        writer = self._require_writer()
        writer.write(prefix + encoded + b"\n")
        try:
            _async.async_to_sync(_wait(writer.drain(), self._remaining(deadline)))
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise QgaTimeoutError("timed out sending a QGA request") from exc

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
        from .. import _async

        try:
            chunk = _async.async_to_sync(
                _wait(
                    self._require_reader().read(min(65536, self.max_reply_size + 1)),
                    self._remaining(deadline),
                )
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise QgaTimeoutError("timed out waiting for a QGA reply") from exc
        if not isinstance(chunk, bytes):
            raise QgaProtocolError("SSH QGA stream returned non-byte data")
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
                data={
                    key: value
                    for key, value in error.items()
                    if key not in {"class", "desc"}
                },
            )
        if "return" not in reply:
            raise QgaProtocolError("QGA reply has neither 'return' nor 'error'")
        return reply["return"]

    @staticmethod
    def _normalize_ssh_error(exc: Exception, command: str) -> Exception:
        from .. import _async

        normalized = _async.normalize_asyncssh_error(exc, command=command)
        if isinstance(normalized, subprocess.TimeoutExpired):
            return QgaTimeoutError(str(normalized))
        if isinstance(normalized, ConnectionError):
            return QgaDisconnectedError(str(normalized))
        return normalized

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QgaTimeoutError("QGA request deadline expired")
        return remaining

    def _require_reader(self) -> _Reader:
        if self._reader is None:
            raise QgaDisconnectedError("QGA transport is not connected")
        return self._reader

    def _require_writer(self) -> _Writer:
        if self._writer is None:
            raise QgaDisconnectedError("QGA transport is not connected")
        return self._writer
