"""Explicit raw QEMU console process behavior with fake libvirt streams."""

from __future__ import annotations

import errno
import subprocess

import pytest

from hostctl.process import Process
from hostctl.process.qemu_serial import (
    QemuSerialConsole,
    normalize_qemu_console_error,
)


class _Stream:
    def __init__(self, reads=()):
        self.reads = list(reads)
        self.sent = []
        self.finished = 0
        self.aborted = 0

    def send(self, value):
        size = min(2, len(value))
        self.sent.append(value[:size])
        return size

    def recv(self, size):
        if not self.reads:
            return b""
        return self.reads.pop(0)[:size]

    def finish(self):
        self.finished += 1

    def abort(self):
        self.aborted += 1


def test_qemu_console_factory_is_lazy_and_lease_is_exclusive():
    stream = _Stream()
    calls = []

    def factory():
        calls.append(True)
        return stream

    console = QemuSerialConsole(stream_factory=factory)
    process = console.open()
    assert isinstance(process, Process)
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match="active process"):
        console.open()
    process.close()
    assert stream.finished == 1
    second = console.open()
    second.close()
    assert len(calls) == 2


def test_qemu_console_partial_write_read_and_merged_stream():
    stream = _Stream((b"reply",))
    process = QemuSerialConsole(stream=stream).open()

    process.write(b"hello")
    assert b"".join(stream.sent) == b"hello"
    assert process.read(3) == b"rep"
    with pytest.raises(TypeError, match="bytes"):
        process.write("text")
    with pytest.raises(NotImplementedError, match="merged"):
        process.read_stderr()


def test_injected_stream_is_not_closed_implicitly_and_eof_is_unsupported():
    stream = _Stream()
    process = QemuSerialConsole(stream=stream).open()
    process.close()
    assert stream.finished == 0

    process = QemuSerialConsole(stream=stream).open()
    with pytest.raises(NotImplementedError, match="half-close"):
        process.send_eof()
    process.close()
    assert stream.finished == 0


def test_remote_eof_closes_session_and_releases_lease():
    stream = _Stream((b"",))
    console = QemuSerialConsole(stream=stream)
    process = console.open()
    assert process.read() == b""
    assert process.returncode == 0
    console.open().close()


def test_resize_is_capability_gated_and_validated():
    stream = _Stream()
    process = QemuSerialConsole(stream=stream).open()
    with pytest.raises(NotImplementedError, match="resize"):
        process.resize(80, 24)
    process.close()

    sizes = []
    process = QemuSerialConsole(
        stream=stream, resize=lambda *size: sizes.append(size)
    ).open()
    process.resize(100, 30, 800, 600)
    assert sizes == [(100, 30, 800, 600)]
    with pytest.raises(ValueError, match="positive"):
        process.resize(0, 30)


def test_qemu_console_wait_signals_and_close_are_explicit():
    process = QemuSerialConsole(stream=_Stream()).open()
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(0)
    with pytest.raises(NotImplementedError, match="signal"):
        process.terminate()
    with pytest.raises(NotImplementedError, match="signal"):
        process.kill()
    process.close()
    process.close()
    assert process.wait(0) == 0


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (OSError(errno.EACCES, "denied"), PermissionError),
        (OSError(errno.ECONNRESET, "gone"), ConnectionError),
        (type("libvirtError", (Exception,), {})("failed"), ConnectionError),
        (type("libvirtError", (Exception,), {})("timed out"), TimeoutError),
    ),
)
def test_qemu_console_error_normalization(error, expected):
    assert isinstance(normalize_qemu_console_error(error), expected)


def test_stream_error_retains_cause_and_failed_finish_aborts():
    failure = type("libvirtError", (Exception,), {})("disconnected")

    class _Failing(_Stream):
        def send(self, value):
            raise failure

        def finish(self):
            raise failure

    stream = _Failing()
    process = QemuSerialConsole(stream=stream, owns_stream=True).open()
    with pytest.raises(ConnectionError) as raised:
        process.write(b"x")
    assert raised.value.__cause__ is failure
    with pytest.raises(ConnectionError):
        process.close()
    assert stream.aborted == 1
