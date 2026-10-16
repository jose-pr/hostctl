"""Explicit raw QEMU/libvirt console stream sessions."""

from __future__ import annotations

import errno
import functools
import threading
import typing

from ..executor._common import LeaseToken, StreamLease
from ._common import (
    IncrementalTextDecoder,
    ProcessData,
    ReentrantLock,
    _LeasedStreamProcess,
    raise_normalized,
)


class QEMUConsoleStream(typing.Protocol):
    """Minimal libvirt-stream-like console channel."""

    def send(self, data: bytes) -> int: ...

    def recv(self, size: int) -> bytes: ...

    def finish(self) -> object: ...

    def abort(self) -> object: ...


ConsoleStreamFactory = typing.Callable[[], QEMUConsoleStream]
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


class QEMUSerialConsole:
    """Grant one exclusive raw process lease over a console stream."""

    def __init__(
        self,
        *,
        stream_factory: typing.Optional[ConsoleStreamFactory] = None,
        stream: typing.Optional[QEMUConsoleStream] = None,
        owns_stream: bool = False,
        resize: typing.Optional[ConsoleResize] = None,
    ) -> None:
        if (stream_factory is None) == (stream is None):
            raise ValueError("provide exactly one of stream_factory or stream")
        self._factory = stream_factory
        self._stream = stream
        self._owns_stream = owns_stream if stream is not None else True
        self._resize = resize
        self._lease = StreamLease(
            "QEMU console already has an active process", on_end=self._on_lease_end
        )
        self._io_lock = threading.RLock()

    def open(
        self,
        *,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> QEMUSerialProcess:
        """Lease the stream; `encoding`/`errors` make the process read `str`."""
        token = self._lease.acquire()
        try:
            stream = self._stream
            if stream is None:
                if self._factory is None:
                    # An injected stream is consumed by the process that
                    # owned it -- it was finished on teardown, so there is
                    # nothing left to reopen.
                    raise RuntimeError(
                        "this console's stream was consumed; supply a "
                        "stream_factory to reopen it"
                    )
                stream = self._factory()
                self._stream = stream
        except Exception as exc:
            self._lease.release(token)
            raise_normalized(exc, normalize_qemu_console_error)
        return QEMUSerialProcess(
            stream,
            io_lock=self._io_lock,
            token=token,
            release=functools.partial(self._lease.release, token),
            close_stream=self._owns_stream,
            resize=self._resize,
            encoding=encoding,
            errors=errors,
        )

    def _on_lease_end(self) -> None:
        if self._owns_stream:
            self._stream = None


class QEMUSerialProcess(_LeasedStreamProcess):
    """Raw merged byte I/O over one libvirt console/channel stream."""

    _closed_message = "QEMU console process is closed"
    _wait_label = "QEMU serial console"
    _stderr_message = "QEMU serial consoles have one merged byte stream"
    _eof_message = "QEMU console streams do not support a generic half-close"
    _terminate_message = "QEMU serial consoles have no generic terminate signal"
    _kill_message = "QEMU serial consoles have no generic kill signal"

    def __init__(
        self,
        stream: QEMUConsoleStream,
        *,
        io_lock: typing.Optional[ReentrantLock] = None,
        token: typing.Optional[LeaseToken] = None,
        release: typing.Callable[[], None],
        close_stream: bool = True,
        resize: typing.Optional[ConsoleResize] = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> None:
        super().__init__(io_lock=io_lock, token=token, release=release)
        self._stream = stream
        self._close_stream = close_stream
        self._resize = resize
        self._encoding = encoding
        self._errors = errors or "strict"
        self._decoder = (
            IncrementalTextDecoder(encoding, self._errors)
            if encoding is not None
            else None
        )

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

    def _eof_value(self) -> ProcessData:
        """The empty value this process reads in, in its own mode."""
        return "" if self._decoder is not None else b""

    def read(self, size: int = -1) -> ProcessData:
        if size < -1:
            raise ValueError("read size must be -1 or non-negative")
        if size == 0:
            # `b""` even with a decoder attached: a text-mode caller doing
            # `buffer += proc.read(0)` got a TypeError from a read that is
            # defined to return nothing.
            return self._eof_value()
        if self._closed.is_set():
            # EOF is sticky. An empty receive ends the lease, and raising
            # here made a second read of an ended stream a ConnectionError
            # where the Process contract says it reads `b""` -- so an
            # ordinary drain loop that reads once more after EOF crashed.
            return self._eof_value()
        request_size = 64 * 1024 if size == -1 else size
        try:
            # Deliberately NOT under `_io_lock`: holding it across a blocking
            # recv() meant write() and close() could not proceed while a read
            # was outstanding, so a console whose guest stopped emitting could
            # neither be driven nor closed. The stream owns its own framing.
            value = self._stream.recv(request_size)
        except Exception as exc:
            raise_normalized(exc, normalize_qemu_console_error)
        if not value:
            self.close()
        if self._decoder is not None:
            return self._decoder.decode(value, final=not value)
        return value

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

    def _teardown(self) -> None:
        if not self._close_stream:
            return
        try:
            with self._io_lock:
                self._stream.finish()
        except Exception as exc:
            try:
                self._stream.abort()
            except Exception:
                pass
            raise_normalized(exc, normalize_qemu_console_error)
