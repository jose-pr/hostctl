"""`console_stream`: any process as the byte stream a console profile drives.

A profile needs a read that can be given a deadline. A serial port has one;
a QEMU console or a container TTY blocks in its read until data arrives, so
the adapter reads it on a thread and hands the profile bounded reads.
"""

from __future__ import annotations

import queue
import threading
import time

import pytest

from hostctl.console import (
    ConsoleStream,
    PromptConsoleProfile,
    console_stream,
)
from hostctl.transports.qemu._console import QEMUSerialConsole
from hostctl.transports.serial._executor import SerialExecutor, SerialSettings


class _BlockingProcess:
    """A process whose `read` blocks until a chunk is queued; `None` is EOF."""

    def __init__(self):
        self.chunks = queue.Queue()
        self.written = []
        self.returncode = None

    def read(self, size=-1):
        item = self.chunks.get()
        return b"" if item is None else item

    def write(self, data):
        self.written.append(data)


class _Port:
    is_open = True
    dtr = rts = False
    timeout = 0.1
    write_timeout = 10

    def read(self, size=1):
        return b""

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def send_break(self, duration=0.25):
        pass


def test_a_process_whose_read_takes_a_timeout_is_returned_as_it_is():
    executor = SerialExecutor(SerialSettings("loop://"), serial_port=_Port())
    process = executor.open()
    try:
        assert console_stream(process) is process
        assert isinstance(process, ConsoleStream)
    finally:
        process.close()


def test_a_blocking_read_is_given_a_deadline():
    process = _BlockingProcess()
    stream = console_stream(process)

    started = time.monotonic()
    assert stream.read(10, timeout=0.05) == b""
    assert time.monotonic() - started < 2

    process.chunks.put(b"hello world")
    assert stream.read(5, timeout=2) == b"hello"
    assert stream.read(-1, timeout=2) == b" world"
    assert stream.read(0) == b""


def test_the_end_of_the_process_ends_the_stream():
    process = _BlockingProcess()
    stream = console_stream(process)
    process.chunks.put(b"tail")
    process.chunks.put(None)

    assert stream.read(timeout=2) == b"tail"
    with pytest.raises(ConnectionError, match="ended"):
        stream.read(timeout=2)
    with pytest.raises(ConnectionError):
        stream.read(timeout=0)


def test_a_failed_read_ends_the_stream_with_its_cause():
    class Failing(_BlockingProcess):
        def read(self, size=-1):
            raise OSError("link down")

    stream = console_stream(Failing())

    with pytest.raises(ConnectionError) as caught:
        stream.read(timeout=2)
    assert isinstance(caught.value.__cause__, OSError)


def test_a_text_mode_process_is_refused_on_the_first_read():
    process = _BlockingProcess()
    process.read = lambda size=-1: "text"
    stream = console_stream(process)

    with pytest.raises(TypeError, match="reads bytes"):
        stream.read(timeout=2)


def test_writes_go_to_the_process_and_reset_discards_unread_bytes():
    process = _BlockingProcess()
    stream = console_stream(process)
    process.chunks.put(b"stale")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not process.chunks.empty():
        time.sleep(0.01)
    time.sleep(0.05)

    stream.reset_input_buffer()
    stream.write(b"ls\n")

    assert stream.read(timeout=0.05) == b""
    assert process.written == [b"ls\n"]


def test_a_negative_size_or_timeout_raises():
    stream = console_stream(_BlockingProcess())

    with pytest.raises(ValueError):
        stream.read(-2)
    with pytest.raises(ValueError):
        stream.read(1, timeout=-1)


class _GuestConsole:
    """A libvirt-like console stream whose `recv` blocks, as a real one does.

    It answers each line it is sent the way a device would, in small pieces.
    """

    def __init__(self, replies):
        self.replies = dict(replies)
        self.pending = queue.Queue()
        self.line = bytearray()
        self.lock = threading.Lock()

    def send(self, data):
        with self.lock:
            self.line += data
            while b"\r" in self.line:
                line, _, rest = bytes(self.line).partition(b"\r")
                self.line = bytearray(rest.lstrip(b"\n"))
                reply = self.replies.get(line, b"")
                for offset in range(0, len(reply), 5):
                    self.pending.put(reply[offset : offset + 5])
        return len(data)

    def recv(self, size):
        return self.pending.get()

    def finish(self):
        self.pending.put(b"")

    def abort(self):
        self.pending.put(b"")


def test_a_prompt_profile_runs_over_a_qemu_console():
    """A prompt profile drives a QEMU console through `console_stream`,
    with no serial transport involved."""
    guest = _GuestConsole(
        {
            b"": b"\r\nrouter> ",
            b"show clock": b"show clock\r\n12:00:00\r\nRC=0\r\nrouter> ",
        }
    )
    profile = PromptConsoleProfile(
        rb"router> ",
        status_marker=rb"RC=(\d+)",
        reliable_status=True,
        status_parser=lambda match: int(match.group(1)),
    )
    console = QEMUSerialConsole(stream=guest)

    with console.open() as process:
        stream = console_stream(process)
        profile.negotiate(stream)
        output, status = profile.run(stream, "show clock", timeout=5)

    assert (output, status) == (b"12:00:00\r\n", 0)
