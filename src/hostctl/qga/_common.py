"""QEMU Guest Agent transport contracts and normalized errors."""

from __future__ import annotations

import typing
import json
import secrets
import threading
import time


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
