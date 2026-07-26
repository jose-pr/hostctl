"""Raw serial transport and process behavior with an injected fake."""

from __future__ import annotations

import errno
import subprocess

import pytest

from hostctl.executor.serial import (
    SerialExecutor,
    SerialSettings,
    normalize_serial_error,
)
from hostctl.process import Process
from hostctl.process.serial import SerialProcess


class _Serial:
    def __init__(self, reads=()):
        self.is_open = True
        self.dtr = False
        self.rts = False
        self.reads = list(reads)
        self.writes = []
        self.flushes = 0
        self.closed = False
        self.breaks = []

    def close(self):
        self.closed = True
        self.is_open = False

    def flush(self):
        self.flushes += 1

    def read(self, size=1):
        if not self.reads:
            return b""
        value = self.reads.pop(0)
        return value[:size]

    def write(self, data):
        written = min(2, len(data))
        self.writes.append(data[:written])
        return written

    def send_break(self, duration=0.25):
        self.breaks.append(duration)


def test_serial_settings_validate_and_forward_every_setting():
    settings = SerialSettings(
        "loop://",
        baudrate=9600,
        bytesize=7,
        parity="e",
        stopbits=1.5,
        xonxoff=True,
        rtscts=True,
        dsrdtr=True,
        read_timeout=0.2,
        write_timeout=3,
        inter_byte_timeout=0.05,
        exclusive=True,
    )
    assert settings.parity == "E"
    assert settings.factory_options() == {
        "baudrate": 9600,
        "bytesize": 7,
        "parity": "E",
        "stopbits": 1.5,
        "xonxoff": True,
        "rtscts": True,
        "dsrdtr": True,
        "timeout": 0.2,
        "write_timeout": 3,
        "inter_byte_timeout": 0.05,
        "exclusive": True,
    }
    with pytest.raises(ValueError, match="baudrate"):
        SerialSettings("port", baudrate=0)
    with pytest.raises(ValueError, match="read_timeout"):
        SerialSettings("port", read_timeout=-1)


def test_executor_factory_is_lazy_and_process_lease_is_exclusive():
    serial_port = _Serial((b"reply",))
    calls = []

    def factory(port, **options):
        calls.append((port, options))
        return serial_port

    executor = SerialExecutor(SerialSettings("loop://"), serial_factory=factory)
    assert calls == []
    process = executor.open()
    assert isinstance(process, Process)
    assert calls[0][0] == "loop://"
    with pytest.raises(RuntimeError, match="active process"):
        executor.open()
    process.close()
    second = executor.open()
    second.close()
    assert len(calls) == 1
    executor.close()
    assert serial_port.closed


def test_injected_serial_remains_caller_owned():
    serial_port = _Serial()
    executor = SerialExecutor(
        SerialSettings("injected"),
        serial_port=serial_port,
    )
    with executor.open():
        pass
    executor.close()
    assert not serial_port.closed


def test_serial_process_is_raw_partial_write_and_merged_read():
    serial_port = _Serial((b"abc",))
    process = SerialExecutor(
        SerialSettings("injected"),
        serial_port=serial_port,
    ).open()

    process.write(b"hello")
    assert b"".join(serial_port.writes) == b"hello"
    assert serial_port.flushes == 1
    assert process.read(2) == b"ab"
    with pytest.raises(TypeError, match="bytes"):
        process.write("text")
    with pytest.raises(NotImplementedError, match="merged"):
        process.read_stderr()
    with pytest.raises(NotImplementedError, match="half-close"):
        process.send_eof()


def test_serial_process_controls_and_unsupported_process_operations():
    serial_port = _Serial()
    process = SerialExecutor(
        SerialSettings("injected"),
        serial_port=serial_port,
    ).open()
    process.send_break(0.5)
    process.dtr = True
    process.rts = True
    assert serial_port.breaks == [0.5]
    assert process.dtr is True
    assert process.rts is True
    with pytest.raises(NotImplementedError, match="resized"):
        process.resize(80, 24)
    with pytest.raises(NotImplementedError, match="terminate"):
        process.terminate()
    with pytest.raises(NotImplementedError, match="kill"):
        process.kill()
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(0)
    process.close()
    assert process.wait(0) == 0
    assert process.returncode == 0


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (OSError(errno.ENOENT, "missing"), FileNotFoundError),
        (OSError(errno.EACCES, "denied"), PermissionError),
        (type("SerialTimeoutException", (Exception,), {})("late"), TimeoutError),
        (type("SerialException", (Exception,), {})("gone"), ConnectionError),
    ),
)
def test_serial_error_normalization(error, expected):
    normalized = normalize_serial_error(error)
    assert isinstance(normalized, expected)


def test_factory_error_retains_cause():
    serial_error = type("SerialException", (Exception,), {})("unavailable")

    def factory(port, **options):
        raise serial_error

    executor = SerialExecutor(SerialSettings("port"), serial_factory=factory)
    with pytest.raises(ConnectionError) as raised:
        executor.connect()
    assert raised.value.__cause__ is serial_error
