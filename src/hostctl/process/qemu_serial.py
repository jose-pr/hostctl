"""Explicit raw QEMU/libvirt console stream sessions."""

from __future__ import annotations

import errno
import subprocess
import threading
import types
import typing

from ._common import IncrementalTextDecoder, Process, ProcessData, raise_normalized


class QemuConsoleStream(typing.Protocol):
    """Minimal libvirt-stream-like console channel."""

    def send(self, data: bytes) -> int: ...

    def recv(self, size: int) -> bytes: ...

    def finish(self) -> object: ...

    def abort(self) -> object: ...


ConsoleStreamFactory = typing.Callable[[], QemuConsoleStream]
ConsoleResize = typing.Callable[[int, int, int, int], object]


def normalize_qemu_console_error(exc: Exception) -> Exception:
    """Normalize libvirt-like stream failures without importing libvirt."""
    if isinstance(exc, (ConnectionError, PermissionError, TimeoutError)):
        return exc
    if isinstance(exc, OSError):
        if exc.errno in (errno.EACCES, errno.EPERM):
            return PermissionError(exc.errno, exc.strerror, exc.filename)
        if exc.errno in (errno.ETIMEDOUT,):
            return TimeoutError(str(exc))
        return ConnectionError(str(exc))
    message = str(exc)
    folded = message.casefold()
    if "permission denied" in folded or "access denied" in folded:
        return PermissionError(message)
    if "timed out" in folded or "timeout" in folded:
        return TimeoutError(message)
    if type(exc).__name__ in ("libvirtError", "StreamError"):
        return ConnectionError(message)
    return exc


class QemuSerialConsole:
    """Grant one exclusive raw process lease over a console stream."""

    def __init__(
        self,
        *,
        stream_factory: typing.Optional[ConsoleStreamFactory] = None,
        stream: typing.Optional[QemuConsoleStream] = None,
        owns_stream: bool = False,
        resize: typing.Optional[ConsoleResize] = None,
    ) -> None:
        if (stream_factory is None) == (stream is None):
            raise ValueError("provide exactly one of stream_factory or stream")
        self._factory = stream_factory
        self._stream = stream
        self._owns_stream = owns_stream if stream is not None else True
        self._resize = resize
        self._lease = threading.Lock()
        self._io_lock = threading.RLock()

    def open(self) -> QemuSerialProcess:
        if not self._lease.acquire(blocking=False):
            raise RuntimeError("QEMU console already has an active process")
        try:
            stream = self._stream
            if stream is None:
                assert self._factory is not None
                stream = self._factory()
                self._stream = stream
        except Exception as exc:
            self._lease.release()
            normalized = normalize_qemu_console_error(exc)
            if normalized is exc:
                raise
            raise normalized from exc
        return QemuSerialProcess(
            stream,
            io_lock=self._io_lock,
            release=self._release,
            close_stream=self._owns_stream,
            resize=self._resize,
        )

    def _release(self) -> None:
        if self._owns_stream:
            self._stream = None
        self._lease.release()


class QemuSerialProcess(Process):
    """Raw merged byte I/O over one libvirt console/channel stream."""

    def __init__(
        self,
        stream: QemuConsoleStream,
        *,
        io_lock: typing.Optional[threading.RLock] = None,
        release: typing.Callable[[], None],
        close_stream: bool = True,
        resize: typing.Optional[ConsoleResize] = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> None:
        self._stream = stream
        self._io_lock = io_lock or threading.RLock()
        self._release = release
        self._close_stream = close_stream
        self._resize = resize
        self._encoding = encoding
        self._errors = errors or "strict"
        self._closed = threading.Event()
        self._decoder = (
            IncrementalTextDecoder(encoding, self._errors)
            if encoding is not None
            else None
        )

    @property
    def returncode(self) -> typing.Optional[int]:
        return 0 if self._closed.is_set() else None

    def _require_open(self) -> None:
        if self._closed.is_set():
            raise ConnectionError("QEMU console process is closed")

    def write(self, data: ProcessData) -> None:
        if isinstance(data, str):
            if self._encoding is None:
                raise TypeError("raw QEMU console writes require bytes")
            data = data.encode(self._encoding, self._errors)
        self._require_open()
        offset = 0
        try:
            with self._io_lock:
                while offset < len(data):
                    sent = self._stream.send(data[offset:])
                    if sent <= 0:
                        raise ConnectionError("QEMU console write made no progress")
                    offset += sent
        except Exception as exc:
            raise_normalized(exc, normalize_qemu_console_error)

    def read(self, size: int = -1) -> ProcessData:
        self._require_open()
        if size < -1:
            raise ValueError("read size must be -1 or non-negative")
        if size == 0:
            return b""
        request_size = 64 * 1024 if size == -1 else size
        try:
            with self._io_lock:
                value = self._stream.recv(request_size)
        except Exception as exc:
            raise_normalized(exc, normalize_qemu_console_error)
        if not value:
            self._finish(closing_stream=self._close_stream)
        if self._decoder is not None:
            return self._decoder.decode(value, final=not value)
        return value

    def read_stderr(self, size: int = -1) -> bytes:
        raise NotImplementedError("QEMU serial consoles have one merged byte stream")

    def send_eof(self) -> None:
        raise NotImplementedError(
            "QEMU console streams do not support a generic half-close"
        )

    def resize(
        self,
        columns: int,
        rows: int,
        pixel_width: int = 0,
        pixel_height: int = 0,
    ) -> None:
        self._require_open()
        if self._resize is None:
            raise NotImplementedError(
                "QEMU console backend does not support terminal resize"
            )
        if columns <= 0 or rows <= 0:
            raise ValueError("terminal columns and rows must be positive")
        if pixel_width < 0 or pixel_height < 0:
            raise ValueError("terminal pixel dimensions must not be negative")
        try:
            self._resize(columns, rows, pixel_width, pixel_height)
        except Exception as exc:
            raise_normalized(exc, normalize_qemu_console_error)

    def wait(self, timeout: typing.Optional[float] = None) -> int:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must not be negative")
        if not self._closed.wait(timeout):
            raise subprocess.TimeoutExpired("QEMU serial console", timeout)
        return 0

    def terminate(self) -> None:
        raise NotImplementedError(
            "QEMU serial consoles have no generic terminate signal"
        )

    def kill(self) -> None:
        raise NotImplementedError("QEMU serial consoles have no generic kill signal")

    def close(self) -> None:
        self._finish(closing_stream=self._close_stream)

    def _finish(self, *, closing_stream: bool) -> None:
        if self._closed.is_set():
            return
        error: typing.Optional[Exception] = None
        if closing_stream:
            try:
                with self._io_lock:
                    self._stream.finish()
            except Exception as exc:
                error = exc
                try:
                    self._stream.abort()
                except Exception:
                    pass
        self._closed.set()
        self._release()
        if error is not None:
            raise_normalized(error, normalize_qemu_console_error)

    def __enter__(self) -> QemuSerialProcess:
        return self

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> bool:
        self.close()
        return False
