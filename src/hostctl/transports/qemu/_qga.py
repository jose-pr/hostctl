"""QEMU Guest Agent framing, the transport contract and normalized errors."""

from __future__ import annotations


import errno
import json
import secrets
import threading
import time
import typing

from ...exceptions import (
    QGACommandError,
    QGADisconnectedError,
    QGAProtocolError,
    QGATimeoutError,
)


class _QGARequestNotSerializable(ValueError):
    """The request never reached the wire, so the stream is untouched.

    A `ValueError` to every caller; a distinct type only so `execute()` can
    tell "nothing was sent" from "something failed mid-stream".
    """


#: Guest-error classification, keyed on the `strerror` tail.
#:
#: qemu-ga formats a failure as `"<context>: <strerror>"`, and the context
#: embeds the guest path -- so classifying the whole string reported
#: `/var/log/notfound.log` missing whatever had actually gone wrong with it.
_GUEST_ERRORS: typing.Tuple[
    typing.Tuple[int, typing.Type[OSError], typing.Tuple[str, ...]], ...
] = (
    (
        errno.ENOENT,
        FileNotFoundError,
        (
            "notfound",
            "enoent",
            "filenotfound",
            "no such file or directory",
            "cannot find the file",
            "cannot find the path",
        ),
    ),
    (
        errno.EACCES,
        PermissionError,
        ("permission", "denied", "eacces", "access is denied"),
    ),
    (
        errno.EEXIST,
        FileExistsError,
        ("eexist", "already exists", "file exists"),
    ),
    (
        errno.EISDIR,
        IsADirectoryError,
        ("isdir", "eisdir", "is a directory"),
    ),
    (
        errno.ENOTDIR,
        NotADirectoryError,
        ("enotdir", "not a directory"),
    ),
    (
        errno.ENOTEMPTY,
        OSError,
        ("enotempty", "directory not empty", "not empty"),
    ),
)


def guest_oserror(exc: Exception, path: str) -> OSError:
    """Translate a guest-agent failure into the filesystem error it means.

    `path` names what the operation was about -- a guest path, or the
    program a `guest-exec` tried to start. The result carries `.errno` and
    `.filename`, so a caller can branch on the errno instead of the text.
    """
    if isinstance(exc, (QGATimeoutError, QGADisconnectedError)):
        return exc
    if isinstance(exc, OSError) and exc.errno is not None:
        # Already classified BY THE OS, which knows better than a text
        # match. Re-deriving it got this wrong: `str(exc)` for an errno
        # error is "[Errno 21] Is a directory: '<path>'", so the last
        # ": " segment -- the strerror tail this reads -- is the PATH, no
        # needle matched, and a perfectly good `IsADirectoryError` was
        # downgraded to a bare `OSError`. Windows hid it, because it
        # raises EACCES for opening a directory and "denied" happens to
        # match. The text ladder below is for `QGACommandError`, which is
        # the guest's own words and carries no errno at all.
        return exc
    name = str(
        getattr(exc, "error_class", "")
        or getattr(exc, "name", "")
        or getattr(exc, "code", "")
        or type(exc).__name__
    ).lower()
    message = (
        str(getattr(exc, "description", "") or getattr(exc, "message", "") or exc)
        or path
    )
    # Only the tail: everything before the last ": " is qemu-ga's own
    # context, which quotes the path the caller supplied.
    tail = message.rsplit(": ", 1)[-1]
    detail = f"{name} {tail.lower()}"
    for code, error_type, needles in _GUEST_ERRORS:
        if any(value in detail for value in needles):
            return error_type(code, message, path)
    return OSError(message)


class _QGAFramedSession:
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
            except (QGACommandError, _QGARequestNotSerializable):
                # The guest answered, or nothing was ever sent. Either way
                # the stream sits at a frame boundary and the connection is
                # healthy -- tearing it down made every "no such file" cost a
                # reconnect and a fresh `guest-sync-delimited`, turning an
                # ordinary `exists()` returning False into three round trips.
                raise
            except Exception:
                # A protocol, timeout or transport failure: the stream
                # position is unknown, so the connection cannot be reused.
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
        while True:
            self._discard_until_delimiter(deadline)
            returned = self._unwrap(self._correlated_reply(request_id, deadline))
            if returned == token:
                return
            # A delimiter and a reply left over from an abandoned session.
            # One scan was enough only when the stream was already clean;
            # with anything buffered ahead of our own reply, connect() gave
            # up where the protocol's own recovery is to keep discarding to
            # the next delimiter. The deadline, not an attempt count, ends
            # this.
            self._remaining(deadline)

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
            raise _QGARequestNotSerializable(
                "QGA request is not JSON serializable"
            ) from exc
        self._send_raw(prefix + encoded + b"\n", deadline)

    def _discard_until_delimiter(self, deadline: float) -> None:
        while True:
            position = self._buffer.find(b"\xff")
            if position >= 0:
                del self._buffer[: position + 1]
                return
            if len(self._buffer) > self.max_reply_size:
                raise QGAProtocolError("QGA synchronization data exceeded size limit")
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
                    raise QGAProtocolError("QGA reply exceeded size limit")
                self._receive(deadline)
                continue
            frame = bytes(self._buffer[:newline]).lstrip(b"\xff")
            del self._buffer[: newline + 1]
            if not frame:
                continue
            if len(frame) > self.max_reply_size:
                raise QGAProtocolError("QGA reply exceeded size limit")
            try:
                reply = json.loads(frame.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                # A deeply nested reply exhausts the parser's stack.
                raise QGAProtocolError("QGA returned malformed JSON") from exc
            if not isinstance(reply, dict):
                raise QGAProtocolError("QGA reply must be a JSON object")
            return typing.cast(typing.Mapping[str, object], reply)

    def _receive(self, deadline: float) -> None:
        chunk = self._recv_raw(min(65536, self.max_reply_size + 1), deadline)
        if not isinstance(chunk, bytes):
            raise QGAProtocolError("QGA transport returned non-byte data")
        if not chunk:
            raise QGADisconnectedError("QGA disconnected before replying")
        self._buffer.extend(chunk)

    @staticmethod
    def _unwrap(reply: typing.Mapping[str, object]) -> object:
        if "error" in reply:
            error = reply["error"]
            if not isinstance(error, dict):
                raise QGAProtocolError("QGA error reply must contain an object")
            error_class = error.get("class", "GenericError")
            description = error.get("desc", "QGA command failed")
            if not isinstance(error_class, str) or not isinstance(description, str):
                raise QGAProtocolError("QGA error fields must be strings")
            raise QGACommandError(
                error_class,
                description,
                data={k: v for k, v in error.items() if k not in {"class", "desc"}},
            )
        if "return" not in reply:
            raise QGAProtocolError("QGA reply has neither 'return' nor 'error'")
        return reply["return"]

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QGATimeoutError("QGA request deadline expired")
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
