"""SSH executor/process regression coverage using transport fakes."""

from __future__ import annotations

import io
import subprocess

import pytest

from hostctl.transports.ssh._executor import SSHExecutor
from hostctl.transports.ssh._process import SSHProcess


class _Result:
    def __init__(self, returncode=0, stdout=b"out", stderr=b"err"):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _WaitProcess:
    """What asyncssh's `create_process()` returns: a handle you can terminate."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.waits = []
        self.terminated = False
        self.closed = False

    async def wait(self, check=False, timeout=None):
        self.waits.append({"check": check, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.result

    def terminate(self):
        self.terminated = True

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class _Connection:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or _Result()
        self.process = None

    async def create_process(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.process = _WaitProcess(self.result)
        return self.process


def test_executor_sends_eof_when_stdin_is_omitted():
    connection = _Connection()
    result = SSHExecutor(lambda: connection)("wc -l")
    assert result.returncode == 0
    stream = connection.calls[0][1]["stdin"]
    assert isinstance(stream, io.BytesIO)
    assert stream.read() == b""


def test_executor_rejects_zero_buffering():
    with pytest.raises(ValueError, match="bufsize=0"):
        SSHExecutor(lambda: _Connection())("echo", bufsize=0)


def test_executor_maps_missing_exit_status_to_failure():
    connection = _Connection(_Result(returncode=None))
    result = SSHExecutor(lambda: connection)("echo", check=False)
    assert result.returncode == -1
    with pytest.raises(subprocess.CalledProcessError) as raised:
        SSHExecutor(lambda: connection)("echo")
    assert raised.value.returncode == -1


class _TimeoutConnection(_Connection):
    async def create_process(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.process = _WaitProcess(error=TimeoutError())
        return self.process


def test_a_timeout_terminates_the_remote_process():
    """`connection.run()` never handed the process back, so a timeout
    abandoned a live channel and a running remote command, and `orphaned` was
    True every time -- conveying nothing."""
    connection = _TimeoutConnection()

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        SSHExecutor(lambda: connection)("sleep 60", timeout=0.01)

    assert connection.process.terminated is True
    assert raised.value.orphaned is False


class _UnterminableConnection(_Connection):
    class _Stuck:
        async def wait(self, check=False, timeout=None):
            raise TimeoutError

        async def wait_closed(self):
            raise ConnectionError("channel state unknown")

    async def create_process(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.process = self._Stuck()
        return self.process


def test_orphaned_still_reports_a_process_that_cannot_be_terminated():
    connection = _UnterminableConnection()

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        SSHExecutor(lambda: connection)("sleep 60", timeout=0.01)

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
    process = SSHProcess(_Process(), "echo")
    with pytest.raises(ConnectionError):
        process.read()
    with pytest.raises(ConnectionError):
        process.write(b"x")
    assert process.wait() == -1


def test_process_close_failure_is_retryable():
    process = SSHProcess(_Process(), "echo")
    with pytest.raises(ConnectionError):
        process.close()
    assert process._closed is False


def _slow_writer(first=b"early\n", second=b"late\n"):
    import asyncio

    async def handler(process):
        process.stdout.write(first)
        await asyncio.sleep(2.5)
        process.stdout.write(second)
        process.exit(0)

    return handler


def _read_all(process):
    chunks = []
    while True:
        chunk = process.read()
        if not chunk:
            return b"".join(chunks) if isinstance(chunk, bytes) else "".join(chunks)
        chunks.append(chunk)


@pytest.mark.parametrize("encoding", (None, "utf-8"))
def test_a_timed_out_wait_keeps_the_output_it_drained(encoding):
    pytest.importorskip("asyncssh")
    from conformance.inprocess_ssh import InProcessSSHServer

    with InProcessSSHServer(_slow_writer()) as server:
        host = server.config()._create_host()
        try:
            process = host.spawn("x", encoding=encoding)
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            assert process.wait() == 0
            output = _read_all(process)
        finally:
            host.close()

    assert output == ("early\nlate\n" if encoding else b"early\nlate\n")
