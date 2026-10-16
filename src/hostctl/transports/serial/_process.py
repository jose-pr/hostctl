"""Raw byte-stream process facade for a serial connection."""

from __future__ import annotations

import contextlib
import typing

from ...executor import LeaseToken, raise_normalized
from ...process import (
    IncrementalTextDecoder,
    Process,
    ProcessData,
    ReentrantLock,
    _LeasedStreamProcess,
)
from ._executor import SerialLike, normalize_serial_error

if typing.TYPE_CHECKING:  # pragma: no cover - import cycle at runtime
    from ._console import ConsoleProfile


class SerialProcess(_LeasedStreamProcess):
    """Exclusive raw access to one serial connection."""

    _closed_message = "serial process is closed"
    _wait_label = "serial session"
    _stderr_message = "serial has one merged byte stream"
    _eof_message = "serial connections do not support half-close"
    _terminate_message = "serial has no generic terminate signal"
    _kill_message = "serial has no generic kill signal"

    def __init__(
        self,
        serial_port: SerialLike,
        *,
        io_lock: ReentrantLock,
        token: LeaseToken,
        release: typing.Callable[[], None],
    ) -> None:
        super().__init__(io_lock=io_lock, token=token, release=release)
        self._serial = serial_port

    def _require_open(self) -> None:
        super()._require_open()
        if not self._serial.is_open:
            raise ConnectionError(self._closed_message)

    def write(
        self, data: ProcessData, *, timeout: typing.Optional[float] = None
    ) -> None:
        """Write `data`, bounded by `timeout` when one is given.

        Like `read`, the port's own `write_timeout` is what actually bounds
        a backend write, so a deadline is applied by clamping it for the
        duration and restoring it afterwards.
        """
        if isinstance(data, str):
            raise TypeError("raw serial process writes require bytes")
        if timeout is not None and timeout < 0:
            raise ValueError("write timeout must not be negative")
        self._require_open()
        offset = 0
        try:
            with self._io_lock:
                while offset < len(data):
                    with self._bounded(timeout, "write_timeout"):
                        written = self._serial.write(data[offset:])
                    if written <= 0:
                        raise TimeoutError("serial write made no progress")
                    offset += written
                self._serial.flush()
        except Exception as exc:
            raise_normalized(exc, normalize_serial_error)

    def read(self, size: int = -1, *, timeout: typing.Optional[float] = None) -> bytes:
        """Read up to `size` bytes, bounded by `timeout` when one is given.

        The port's own `timeout` is what actually bounds a backend read, and
        `SerialConfig(timeout=Timeout(read=None))` -- a supported setting that
        round-trips through the URI -- is pyserial's "block forever". A
        caller's `run(timeout=)` was only checked *between* reads, so such a
        host blocked inside the backend instead of raising `TimeoutExpired`.
        The port setting is clamped for the duration of this read and then
        restored; a backend that has no settable `timeout` is read as before.
        """
        self._require_open()
        if size < -1:
            raise ValueError("read size must be -1 or non-negative")
        if timeout is not None and timeout < 0:
            raise ValueError("read timeout must not be negative")
        if size == -1:
            available = getattr(self._serial, "in_waiting", None)
            if available is not None:
                size = min(max(int(available), 0), 64 * 1024)
                if size == 0:
                    return b""
            else:
                # A generic injected serial object may not expose an
                # availability count. Read at most one byte rather than
                # blocking for an arbitrary 64 KiB request.
                size = 1
        try:
            with self._io_lock:
                with self._bounded(timeout):
                    return self._serial.read(size)
        except Exception as exc:
            raise_normalized(exc, normalize_serial_error)

    @contextlib.contextmanager
    def _bounded(self, timeout: typing.Optional[float], attribute: str = "timeout"):
        if timeout is None or not hasattr(self._serial, attribute):
            yield
            return
        previous = getattr(self._serial, attribute)
        try:
            setattr(self._serial, attribute, timeout)
        except Exception:
            # A read-only or exotic backend attribute: fall back to whatever
            # the port was configured with rather than failing the call.
            yield
            return
        try:
            yield
        finally:
            try:
                setattr(self._serial, attribute, previous)
            except Exception:
                pass

    def resize(
        self,
        columns: int,
        rows: int,
        pixel_width: int = 0,
        pixel_height: int = 0,
    ) -> None:
        raise NotImplementedError("serial terminals cannot be resized generically")

    def send_break(self, duration: float = 0.25) -> None:
        if duration < 0:
            raise ValueError("break duration must not be negative")
        self._require_open()
        try:
            with self._io_lock:
                self._serial.send_break(duration)
        except Exception as exc:
            raise_normalized(exc, normalize_serial_error)

    def reset_input_buffer(self) -> None:
        """Discard stale bytes before a protocol negotiation or command."""
        self._require_open()
        reset = getattr(self._serial, "reset_input_buffer", None)
        if not callable(reset):
            raise NotImplementedError("serial backend cannot reset its input buffer")
        try:
            with self._io_lock:
                reset()
        except Exception as exc:
            raise_normalized(exc, normalize_serial_error)

    @property
    def dtr(self) -> bool:
        self._require_open()
        return self._serial.dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self._require_open()
        self._serial.dtr = bool(value)

    @property
    def rts(self) -> bool:
        self._require_open()
        return self._serial.rts

    @rts.setter
    def rts(self, value: bool) -> None:
        self._require_open()
        self._serial.rts = bool(value)


class SerialConsoleProcess(Process):
    """Console-aware wrapper used by :class:`hostctl.transports.serial.SerialHost`.

    It keeps the raw process lease while allowing shell sessions to submit
    text.  Reads remain merged bytes; framing and prompt interpretation belong
    to the selected profile.
    """

    def __init__(
        self,
        process: SerialProcess,
        profile: "ConsoleProfile",
        encoding: str = "utf-8",
        errors: typing.Optional[str] = None,
        *,
        text: bool = False,
    ) -> None:
        self.process = process
        self.profile = profile
        self.encoding = encoding
        # `SerialHost.spawn` accepted `errors=` and dropped it here, so a
        # console with a non-UTF-8 device answered `errors="replace"` with a
        # `UnicodeEncodeError`. `QEMUSerialProcess` already carried both.
        self.errors = errors or "strict"
        #: Set when the caller asked for text, so `read()` returns `str`
        #: here as it does from SSH and a QEMU console.
        self._decoder = (
            IncrementalTextDecoder(self.encoding, self.errors) if text else None
        )

    @property
    def returncode(self):
        return self.process.returncode

    def write(self, data: ProcessData) -> None:
        if isinstance(data, str):
            data = data.encode(self.encoding, self.errors)
        self.process.write(data)

    def read(self, size: int = -1) -> ProcessData:
        value = typing.cast(bytes, self.process.read(size))
        if self._decoder is None:
            return value
        # Never final: an idle line reads `b""` too, so a character split
        # across two reads would otherwise be cut short.
        return self._decoder.decode(value)

    def read_stderr(self, size: int = -1) -> bytes:
        return self.process.read_stderr(size)

    def send_eof(self) -> None:
        self.process.send_eof()

    def resize(
        self, columns: int, rows: int, pixel_width: int = 0, pixel_height: int = 0
    ) -> None:
        self.process.resize(columns, rows, pixel_width, pixel_height)

    def wait(self, timeout: typing.Optional[float] = None) -> int:
        return self.process.wait(timeout)

    def terminate(self) -> None:
        self.process.terminate()

    def kill(self) -> None:
        self.process.kill()

    def close(self) -> None:
        self.process.close()

    def send_break(self, duration: float = 0.25) -> None:
        self.process.send_break(duration)

    @property
    def dtr(self) -> bool:
        return self.process.dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self.process.dtr = value

    @property
    def rts(self) -> bool:
        return self.process.rts

    @rts.setter
    def rts(self, value: bool) -> None:
        self.process.rts = value

    def send_command(self, command: str | bytes) -> None:
        sender = getattr(self.profile, "send", None)
        if not callable(sender):
            raise NotImplementedError("console profile cannot send commands")
        sender(self.process, command)

    def send(self, *commands: str | bytes) -> None:
        """Submit one or more profile-framed console commands."""
        if not commands:
            raise ValueError("send requires at least one command")
        for command in commands:
            self.send_command(command)

    def __enter__(self) -> "SerialConsoleProcess":
        self.process.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return self.process.__exit__(exc_type, exc_value, traceback)
