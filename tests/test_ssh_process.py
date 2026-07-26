"""SSH executor/process regression coverage using transport fakes."""

from __future__ import annotations

import io
import subprocess

import pytest

from hostctl.executor.ssh import SshExecutor
from hostctl.process.ssh import SshProcess


class _Result:
    def __init__(self, returncode=0, stdout=b"out", stderr=b"err"):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Connection:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or _Result()
        self.process = None

    async def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.result


def test_executor_sends_eof_when_stdin_is_omitted():
    connection = _Connection()
    result = SshExecutor(lambda: connection)("wc -l")
    assert result.returncode == 0
    stream = connection.calls[0][1]["stdin"]
    assert isinstance(stream, io.BytesIO)
    assert stream.read() == b""


def test_executor_rejects_zero_buffering():
    with pytest.raises(ValueError, match="bufsize=0"):
        SshExecutor(lambda: _Connection())("echo", bufsize=0)


def test_executor_maps_missing_exit_status_to_failure():
    connection = _Connection(_Result(returncode=None))
    result = SshExecutor(lambda: connection)("echo", check=False)
    assert result.returncode == -1
    with pytest.raises(subprocess.CalledProcessError) as raised:
        SshExecutor(lambda: connection)("echo")
    assert raised.value.returncode == -1


class _TimeoutConnection(_Connection):
    async def run(self, command, **kwargs):
        raise TimeoutError


def test_timeout_is_explicitly_marked_orphaned_when_channel_unavailable():
    connection = _TimeoutConnection()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        SshExecutor(lambda: connection)("sleep 60", timeout=0.01)
    assert raised.value.orphaned is True


class _Reader:
    async def read(self, size=-1):
        raise ConnectionError("lost")


class _Writer:
    def write(self, data):
        raise ConnectionError("lost")

    async def drain(self):
        return None

    def write_eof(self):
        raise ConnectionError("lost")


class _Process:
    returncode = None
    stdin = _Writer()
    stdout = _Reader()
    stderr = _Reader()

    def close(self):
        raise ConnectionError("lost")

    async def wait_closed(self):
        return None

    async def wait(self, check=False, timeout=None):
        return _Result(returncode=None)


def test_process_maps_io_failures_and_missing_returncode():
    process = SshProcess(_Process(), "echo")
    with pytest.raises(ConnectionError):
        process.read()
    with pytest.raises(ConnectionError):
        process.write(b"x")
    assert process.wait() == -1


def test_process_close_failure_is_retryable():
    process = SshProcess(_Process(), "echo")
    with pytest.raises(ConnectionError):
        process.close()
    assert process._closed is False
