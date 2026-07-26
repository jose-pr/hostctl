"""Persistent Docker exec process behavior without a Docker installation."""

import collections

import pytest

from hostctl import ContainerProcess


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
