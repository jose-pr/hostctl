"""Raw serial transport ownership without console or shell assumptions."""

from __future__ import annotations

import dataclasses
import errno
import contextlib
import threading
import time
import types
import typing

if typing.TYPE_CHECKING:
    from ..process.serial import SerialProcess


class SerialLike(typing.Protocol):
    is_open: bool
    dtr: bool
    rts: bool
    timeout: typing.Optional[float]
    write_timeout: typing.Optional[float]

    def close(self) -> None: ...

    def flush(self) -> None: ...

    def read(self, size: int = 1) -> bytes: ...

    def send_break(self, duration: float = 0.25) -> None: ...

    def write(self, data: bytes) -> int: ...


SerialFactory = typing.Callable[..., SerialLike]


class SerialTransport:
    """Locked byte transport used by protocol profiles and tests."""

    def __init__(
        self,
        serial_port: SerialLike,
        *,
        lock: typing.Optional[threading.RLock] = None,
    ) -> None:
        self.serial = serial_port
        self.lock = lock or threading.RLock()

    @property
    def connected(self) -> bool:
        return bool(self.serial.is_open)

    @contextlib.contextmanager
    def _deadline_timeout(
        self, attribute: str, deadline: typing.Optional[float]
    ) -> typing.Iterator[None]:
        if deadline is None:
            yield
            return
        if not hasattr(self.serial, attribute):
            raise NotImplementedError(
                f"serial backend cannot enforce a finite {attribute.replace('_', ' ')}"
            )
        previous = getattr(self.serial, attribute)
        remaining = max(0.0, deadline - time.monotonic())
        setattr(self.serial, attribute, remaining)
        try:
            yield
        finally:
            setattr(self.serial, attribute, previous)

    def read(
        self, size: int = 4096, *, timeout: typing.Optional[float] = None
    ) -> bytes:
        if size < 0:
            raise ValueError("read size must not be negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must not be negative")
        with self.lock:
            while True:
                with self._deadline_timeout("timeout", deadline):
                    value = self.serial.read(size)
                if value or deadline is None or time.monotonic() >= deadline:
                    return value

    def write(self, data: bytes, *, timeout: typing.Optional[float] = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must not be negative")
        offset = 0
        with self.lock:
            while offset < len(data):
                with self._deadline_timeout("write_timeout", deadline):
                    written = self.serial.write(data[offset:])
                if written <= 0:
                    raise TimeoutError("serial write made no progress")
                offset += written
                if (
                    deadline is not None
                    and time.monotonic() >= deadline
                    and offset < len(data)
                ):
                    raise TimeoutError("serial write timed out")
            self.serial.flush()

    def reset_input_buffer(self) -> None:
        reset = getattr(self.serial, "reset_input_buffer", None)
        if callable(reset):
            with self.lock:
                reset()

    @property
    def dtr(self) -> bool:
        return bool(self.serial.dtr)

    @dtr.setter
    def dtr(self, value: bool) -> None:
        with self.lock:
            self.serial.dtr = bool(value)

    @property
    def rts(self) -> bool:
        return bool(self.serial.rts)

    @rts.setter
    def rts(self, value: bool) -> None:
        with self.lock:
            self.serial.rts = bool(value)

    def send_break(self, duration: float = 0.25) -> None:
        with self.lock:
            self.serial.send_break(duration)

    def close(self) -> None:
        with self.lock:
            self.serial.close()


@dataclasses.dataclass(frozen=True)
class SerialSettings:
    """Validated settings passed unchanged to ``serial_for_url``."""

    port: str
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False
    read_timeout: typing.Optional[float] = 0.1
    write_timeout: typing.Optional[float] = 10
    inter_byte_timeout: typing.Optional[float] = None
    exclusive: typing.Optional[bool] = None

    def __post_init__(self) -> None:
        if not self.port:
            raise ValueError("serial port must not be empty")
        if self.baudrate <= 0:
            raise ValueError("baudrate must be positive")
        if self.bytesize not in (5, 6, 7, 8):
            raise ValueError("bytesize must be 5, 6, 7, or 8")
        if self.parity.upper() not in ("N", "E", "O", "M", "S"):
            raise ValueError("parity must be N, E, O, M, or S")
        object.__setattr__(self, "parity", self.parity.upper())
        if self.stopbits not in (1, 1.5, 2):
            raise ValueError("stopbits must be 1, 1.5, or 2")
        for name in ("read_timeout", "write_timeout", "inter_byte_timeout"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative")

    def factory_options(self) -> typing.Dict[str, object]:
        options: typing.Dict[str, object] = {
            "baudrate": self.baudrate,
            "bytesize": self.bytesize,
            "parity": self.parity,
            "stopbits": self.stopbits,
            "xonxoff": self.xonxoff,
            "rtscts": self.rtscts,
            "dsrdtr": self.dsrdtr,
            "timeout": self.read_timeout,
            "write_timeout": self.write_timeout,
            "inter_byte_timeout": self.inter_byte_timeout,
        }
        if self.exclusive is not None:
            options["exclusive"] = self.exclusive
        return options


def _default_serial_factory(port: str, **options: object) -> SerialLike:
    try:
        import serial
    except ImportError as exc:
        raise ImportError(
            "serial support requires the 'serial' extra: " "pip install hostctl[serial]"
        ) from exc
    return typing.cast(SerialLike, serial.serial_for_url(port, **options))


def normalize_serial_error(exc: Exception) -> Exception:
    """Normalize pyserial/native failures without requiring pyserial at import."""
    if isinstance(exc, (FileNotFoundError, PermissionError, TimeoutError)):
        return exc
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOENT:
            return FileNotFoundError(exc.errno, exc.strerror, exc.filename)
        if exc.errno in (errno.EACCES, errno.EPERM):
            return PermissionError(exc.errno, exc.strerror, exc.filename)
    name = type(exc).__name__
    if name == "SerialTimeoutException":
        return TimeoutError(str(exc))
    if name in ("SerialException", "PortNotOpenError"):
        return ConnectionError(str(exc))
    return exc


class SerialExecutor:
    """Own one raw serial connection and grant one exclusive process lease."""

    def __init__(
        self,
        settings: SerialSettings,
        *,
        serial_factory: typing.Optional[SerialFactory] = None,
        serial_port: typing.Optional[SerialLike] = None,
        owns_serial_port: bool = False,
    ) -> None:
        if serial_factory is not None and serial_port is not None:
            raise ValueError("serial_factory and serial_port are mutually exclusive")
        self.settings = settings
        self._factory = serial_factory or _default_serial_factory
        self._serial = serial_port
        self._injected = serial_port is not None
        self._owns_serial = owns_serial_port if serial_port is not None else True
        self._lease = threading.Lock()
        self._io_lock = threading.RLock()
        self._connect_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def transport(self) -> SerialTransport:
        """Return the locked transport for protocol/profile integrations."""
        serial_port = self.connect()
        return SerialTransport(serial_port, lock=self._io_lock)

    def connect(self) -> SerialLike:
        with self._connect_lock:
            if self.connected:
                return typing.cast(SerialLike, self._serial)
            if self._injected:
                raise ConnectionError("injected serial port is closed")
            try:
                self._serial = self._factory(
                    self.settings.port, **self.settings.factory_options()
                )
            except Exception as exc:
                normalized = normalize_serial_error(exc)
                if normalized is exc:
                    raise
                raise normalized from exc
            return typing.cast(SerialLike, self._serial)

    def open(self) -> SerialProcess:
        """Acquire the connection for one raw byte-stream process."""
        if not self._lease.acquire(blocking=False):
            raise RuntimeError("serial connection already has an active process")
        try:
            serial_port = self.connect()
        except Exception:
            self._lease.release()
            raise
        from ..process.serial import SerialProcess

        return SerialProcess(
            serial_port,
            io_lock=self._io_lock,
            release=self._release,
        )

    def _release(self) -> None:
        self._lease.release()

    def close(self) -> None:
        serial_port = self._serial
        if serial_port is not None and self._owns_serial:
            self._serial = None
            try:
                serial_port.close()
            except Exception as exc:
                normalized = normalize_serial_error(exc)
                if normalized is exc:
                    raise
                raise normalized from exc

    def __enter__(self) -> SerialExecutor:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> bool:
        self.close()
        return False
