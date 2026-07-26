"""QEMU serial console process behavior with an injected stream."""

import subprocess

import pytest

from hostctl.process.qemu_serial import QemuSerialProcess


class _Stream:
    def __init__(self):
        self.reads = [b"reply"]
        self.writes = []
        self.finished = False

    def recv(self, size):
        return self.reads.pop(0)[:size] if self.reads else b""

    def send(self, data):
        count = min(2, len(data))
        self.writes.append(data[:count])
        return count

    def finish(self):
        self.finished = True


def test_qemu_serial_partial_io_and_ownership_release():
    stream = _Stream()
    released = []
    process = QemuSerialProcess(stream, release=lambda: released.append(True))

    process.write(b"hello")
    assert b"".join(stream.writes) == b"hello"
    assert process.read() == b"reply"
    with pytest.raises(NotImplementedError, match="merged"):
        process.read_stderr()
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(0)
    process.close()
    process.close()
    assert stream.finished
    assert released == [True]
    assert process.wait(0) == 0


def test_qemu_serial_optional_text_and_explicit_unsupported_operations():
    stream = _Stream()
    process = QemuSerialProcess(stream, release=lambda: None, encoding="utf-8")
    process.write("hello")
    assert process.read() == "reply"
    with pytest.raises(NotImplementedError, match="half-close"):
        process.send_eof()
    with pytest.raises(NotImplementedError, match="resize"):
        process.resize(80, 24)
    with pytest.raises(NotImplementedError, match="terminate"):
        process.terminate()
    with pytest.raises(NotImplementedError, match="kill"):
        process.kill()


def test_qemu_serial_incremental_decoder_preserves_split_utf8():
    stream = _Stream()
    stream.reads = [b"\xc3", b"\xa9"]
    process = QemuSerialProcess(stream, release=lambda: None, encoding="utf-8")
    assert process.read() == ""
    assert process.read() == "é"
    process.close()
