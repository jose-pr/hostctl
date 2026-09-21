"""Persistent Docker exec process behavior without a Docker installation."""

import collections

import pytest

from hostctl.process import ContainerProcess


def _frame(stream, value):
    return bytes((stream, 0, 0, 0)) + len(value).to_bytes(4, "big") + value


class _Socket:
    def __init__(self, *values):
        self.values = collections.deque(values)
        self.sent = []
        self.shutdowns = []
        self.closed = False

    def recv(self, size):
        if not self.values:
            return b""
        value = self.values.popleft()
        self.values.appendleft(value[size:])
        if not self.values[0]:
            self.values.popleft()
        return value[:size]

    def sendall(self, value):
        self.sent.append(value)

    def shutdown(self, how):
        self.shutdowns.append(how)

    def close(self):
        self.closed = True


class _NonBlockingSocket(_Socket):
    def __init__(self, *values):
        super().__init__(*values)
        self.timeout = None

    def recv(self, size):
        if not self.values:
            raise BlockingIOError
        value = self.values.popleft()
        if isinstance(value, BaseException):
            raise value
        self.values.appendleft(value[size:])
        if not self.values[0]:
            self.values.popleft()
        return value[:size]

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout


class _Api:
    def __init__(self, states):
        self.states = collections.deque(states)
        self.resizes = []

    def exec_inspect(self, exec_id):
        return self.states[0] if len(self.states) == 1 else self.states.popleft()

    def exec_resize(self, exec_id, *, height, width):
        self.resizes.append((exec_id, height, width))


def test_container_process_demultiplexes_output_and_encodes_input():
    stream = _Socket(_frame(2, b"bad"), _frame(1, b"good"))
    process = ContainerProcess(
        _Api([{"Running": False, "ExitCode": 0}]),
        "exec",
        stream,
        tty=False,
        command=["sh"],
        encoding="utf-8",
    )

    process.write("hello")
    assert process.read() == "good"
    assert process.read_stderr() == "bad"
    assert stream.sent == [b"hello"]
    assert process.wait() == 0


def test_container_tty_merges_stderr_and_resizes():
    api = _Api([{"Running": False, "ExitCode": 0}])
    process = ContainerProcess(
        api, "exec", _Socket(b"terminal"), tty=True, command=["sh"]
    )

    assert process.read() == b"terminal"
    with pytest.raises(NotImplementedError, match="combine"):
        process.read_stderr()
    process.resize(120, 40)
    assert api.resizes == [("exec", 40, 120)]


def test_container_process_unsupported_signals_are_explicit():
    process = ContainerProcess(
        _Api([{"Running": False, "ExitCode": 0}]),
        "exec",
        _Socket(),
        tty=False,
        command=["sh"],
    )
    with pytest.raises(NotImplementedError, match="signal"):
        process.terminate()
    with pytest.raises(NotImplementedError, match="signal"):
        process.kill()


def test_container_process_read_returns_available_data_and_rejects_truncated_frames():
    process = ContainerProcess(
        _Api([{"Running": False, "ExitCode": 0}]),
        "exec",
        _Socket(_frame(1, b"abcdef")),
        tty=False,
        command=["cat"],
    )
    assert process.read(2) == b"ab"
    assert process.read(2) == b"cd"

    broken = ContainerProcess(
        _Api([{"Running": False, "ExitCode": 0}]),
        "exec",
        _Socket(b"\x01\x00"),
        tty=False,
        command=["cat"],
    )
    with pytest.raises(ConnectionError, match="mid-frame"):
        broken.read()


def test_container_wait_preserves_partial_nonblocking_frames():
    frame = _frame(1, b"complete")
    stream = _NonBlockingSocket(
        frame[:3],
        BlockingIOError(),
        frame[3:10],
        BlockingIOError(),
        frame[10:],
        b"",
    )
    api = _Api(
        [
            {"Running": True, "ExitCode": None},
            {"Running": True, "ExitCode": None},
            {"Running": False, "ExitCode": 0},
        ]
    )
    process = ContainerProcess(
        api,
        "exec",
        stream,
        tty=False,
        command=["cat"],
    )

    assert process.wait(timeout=1) == 0
    assert process.read() == b"complete"


def test_a_finished_exec_with_no_exit_code_does_not_spin_forever():
    """`returncode` returned None both for "running" and "finished, status
    unknown", so wait() looped at ~95 exec_inspect calls a second and
    `timeout=None` never escaped."""
    process = ContainerProcess(
        _Api([{"Running": False, "ExitCode": None}]),
        "exec",
        _Socket(),
        tty=True,
        command=["sh"],
    )

    assert process.wait(timeout=5) == -1


def test_a_running_exec_still_reports_none():
    process = ContainerProcess(
        _Api([{"Running": True, "ExitCode": None}]),
        "exec",
        _Socket(),
        tty=True,
        command=["sh"],
    )

    assert process.returncode is None


def test_a_text_read_never_answers_empty_while_the_stream_is_live():
    """`read(n)` slices RAW bytes before decoding, so a read landing inside
    a multi-byte character returned '' -- the conventional EOF signal --
    while the process was still producing, and
    `while chunk := p.read(1024)` silently lost the rest."""
    payload = "\N{LATIN SMALL LETTER E WITH ACUTE}".encode("utf-8")
    stream = _Socket(_frame(1, payload[:1]), _frame(1, payload[1:]))
    process = ContainerProcess(
        _Api([{"Running": False, "ExitCode": 0}]),
        "exec",
        stream,
        tty=False,
        command=["cat"],
        encoding="utf-8",
    )

    assert process.read(1) == "\N{LATIN SMALL LETTER E WITH ACUTE}"


def test_a_truncated_trailing_sequence_is_not_dropped_at_eof():
    """`final=True` was never passed, so bytes left in the decoder when the
    stream ended -- a command killed mid-character -- were discarded where
    `errors="strict"` should raise."""
    payload = "\N{LATIN SMALL LETTER E WITH ACUTE}".encode("utf-8")
    stream = _Socket(_frame(1, payload[:1]))
    process = ContainerProcess(
        _Api([{"Running": False, "ExitCode": 0}]),
        "exec",
        stream,
        tty=False,
        command=["cat"],
        encoding="utf-8",
    )

    with pytest.raises(UnicodeDecodeError):
        process.read()


def test_send_eof_reports_a_transport_failure_as_one():
    """A real `OSError` was relabelled `NotImplementedError`, telling the
    caller to stop trying when the connection had actually dropped."""

    class _Broken(_Socket):
        def shutdown(self, how):
            raise OSError("connection reset")

    process = ContainerProcess(
        _Api([{"Running": False, "ExitCode": 0}]),
        "exec",
        _Broken(),
        tty=False,
        command=["cat"],
    )

    with pytest.raises(ConnectionError, match="connection reset"):
        process.send_eof()


class _ClosingSocket(_Socket):
    """A socket that answers like a real one after `close()`: WinError 10038."""

    def __init__(self, *values):
        super().__init__(*values)
        self.timeout = None

    def _check(self):
        if self.closed:
            raise OSError(
                10038,
                "an operation was attempted on something that is not a socket",
            )

    def recv(self, size):
        self._check()
        return super().recv(size)

    def settimeout(self, value):
        self._check()
        self.timeout = value

    def gettimeout(self):
        self._check()
        return self.timeout


def test_close_leaves_read_and_wait_answering_instead_of_raising_winerror():
    """A closed process reads EOF and still reports its exit code.

    `close()` closed the socket and nothing else, so the next `read()` or
    `wait()` reached `recv()`/`settimeout()` on a closed handle and raised
    the operating system's raw error -- `WinError 10038` on Windows,
    `EBADF` elsewhere -- out of a method whose contract is a stream.
    """
    socket_ = _ClosingSocket(_frame(1, b"before"))
    process = ContainerProcess(
        _Api([{"Running": False, "ExitCode": 3}]),
        "exec",
        socket_,
        tty=False,
        command=["sh"],
    )
    assert process.read() == b"before"

    process.close()
    assert process.read() == b""
    assert process.read_stderr() == b""
    assert process.wait() == 3
