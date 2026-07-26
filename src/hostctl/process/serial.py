"""Raw byte-stream process facade for a serial connection."""

from __future__ import annotations

import subprocess
import threading
import types
import typing

from ._common import Process, ProcessData, raise_normalized
from ..executor.serial import SerialLike, normalize_serial_error


class SerialProcess(Process):
    """Exclusive raw access to one serial connection."""

    def __init__(
        self,
        serial_port: SerialLike,
        *,
        io_lock: threading.RLock,
        release: typing.Callable[[], None],
    ) -> None:
        self._serial = serial_port
        self._io_lock = io_lock
        self._release = release
        self._closed = threading.Event()

    @property
    def returncode(self) -> typing.Optional[int]:
        return 0 if self._closed.is_set() else None

    def _require_open(self) -> None:
        if self._closed.is_set() or not self._serial.is_open:
            raise ConnectionError("serial process is closed")

    def write(self, data: ProcessData) -> None:
        if isinstance(data, str):
            raise TypeError("raw serial process writes require bytes")
        self._require_open()
        offset = 0
        try:
            with self._io_lock:
                while offset < len(data):
                    written = self._serial.write(data[offset:])
                    if written <= 0:
                        raise TimeoutError("serial write made no progress")
                    offset += written
                self._serial.flush()
        except Exception as exc:
            normalized = normalize_serial_error(exc)
            if normalized is exc:
                raise
            raise normalized from exc

    def read(self, size: int = -1) -> bytes:
        self._require_open()
        if size < -1:
            raise ValueError("read size must be -1 or non-negative")
        if size == -1:
            size = 64 * 1024
        try:
            with self._io_lock:
                return self._serial.read(size)
        except Exception as exc:
            raise_normalized(exc, normalize_serial_error)

    def read_stderr(self, size: int = -1) -> bytes:
        raise NotImplementedError("serial has one merged byte stream")

    def send_eof(self) -> None:
        raise NotImplementedError("serial connections do not support half-close")

    def resize(
        self,
        columns: int,
        rows: int,
        pixel_width: int = 0,
        pixel_height: int = 0,
    ) -> None:
        raise NotImplementedError("serial terminals cannot be resized generically")

    def wait(self, timeout: typing.Optional[float] = None) -> int:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must not be negative")
        if not self._closed.wait(timeout):
            raise subprocess.TimeoutExpired("serial session", timeout)
        return 0

    def terminate(self) -> None:
        raise NotImplementedError("serial has no generic terminate signal")

    def kill(self) -> None:
        raise NotImplementedError("serial has no generic kill signal")

    def send_break(self, duration: float = 0.25) -> None:
        if duration < 0:
            raise ValueError("break duration must not be negative")
        self._require_open()
        try:
            with self._io_lock:
                self._serial.send_break(duration)
        except Exception as exc:
            normalized = normalize_serial_error(exc)
            if normalized is exc:
                raise
            raise normalized from exc

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

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._release()

    def __enter__(self) -> SerialProcess:
        return self

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> bool:
        self.close()
        return False


class SerialConsoleProcess(Process):
    """Console-aware wrapper used by :class:`hostctl.host.SerialHost`.

    It keeps the raw process lease while allowing shell sessions to submit
    text.  Reads remain merged bytes; framing and prompt interpretation belong
    to the selected profile.
    """

    def __init__(
        self, process: SerialProcess, profile: object, encoding: str = "utf-8"
    ) -> None:
        self.process = process
        self.profile = profile
        self.encoding = encoding

    @property
    def returncode(self):
        return self.process.returncode

    def write(self, data: ProcessData) -> None:
        if isinstance(data, str):
            data = data.encode(self.encoding)
        self.process.write(data)

    def read(self, size: int = -1) -> bytes:
        value = self.process.read(size)
        return typing.cast(bytes, value)

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
