"""QEMU Guest Agent buffered executor tests."""

from __future__ import annotations

import base64
import io
import subprocess
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from hostctl.executor.qemu import GuestAgentProtocolError, QemuExecutor


def _encoded(value):
    return base64.b64encode(value).decode("ascii")


class _Transport:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def execute(self, request, timeout=None):
        self.calls.append((request, timeout))
        if request["execute"] == "guest-exec":
            return {"pid": 41}
        return self.statuses.pop(0)


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        (PurePosixPath("/usr/bin/printf"), "/usr/bin/printf"),
        (PureWindowsPath(r"C:\Tools\app.exe"), r"C:\Tools\app.exe"),
    ),
)
def test_qemu_executor_builds_guest_exec_argv_env_and_binary_input(command, expected):
    transport = _Transport(
        [
            {"exited": False},
            {
                "exited": True,
                "exitcode": 0,
                "out-data": _encoded(b"out\x00"),
                "err-data": _encoded(b"err"),
            },
        ]
    )
    executor = QemuExecutor(lambda: transport, sleep=lambda _: None)

    result = executor(
        command,
        "a b",
        b"raw",
        env={"COUNT": 2, b"RAW": b"value"},
        input=b"in\x00",
    )

    request = transport.calls[0][0]
    assert request == {
        "execute": "guest-exec",
        "arguments": {
            "path": expected,
            "arg": ["a b", "raw"],
            "capture-output": True,
            "env": ["COUNT=2", "RAW=value"],
            "input-data": _encoded(b"in\x00"),
        },
    }
    assert transport.calls[1][0]["arguments"] == {"pid": 41}
    assert result.args == [expected, "a b", "raw"]
    assert result.stdout == b"out\x00"
    assert result.stderr == b"err"
    assert result.pid == 41


def test_qemu_executor_text_capture_merge_and_output_targets():
    transport = _Transport(
        [
            {
                "exited": True,
                "exitcode": 0,
                "out-data": _encoded("hé".encode()),
                "err-data": _encoded(b"err"),
                "out-truncated": True,
            }
        ]
    )
    target = io.StringIO()
    executor = QemuExecutor(lambda: transport)

    result = executor(
        "program",
        stdout=target,
        stderr=subprocess.STDOUT,
        capture_output=False,
        text=True,
    )

    assert target.getvalue() == "héerr"
    assert result.stdout is None
    assert result.stderr is None
    assert result.stdout_truncated is True
    assert result.stderr_truncated is False


def test_qemu_executor_reads_stdin_without_closing_it():
    stream = io.BytesIO(b"payload")
    transport = _Transport([{"exited": True, "exitcode": 0}])

    QemuExecutor(lambda: transport)("program", stdin=stream)

    assert not stream.closed
    assert transport.calls[0][0]["arguments"]["input-data"] == _encoded(b"payload")


def test_qemu_executor_nonzero_check_and_missing_output():
    transport = _Transport([{"exited": True, "exitcode": 7}])

    with pytest.raises(subprocess.CalledProcessError) as raised:
        QemuExecutor(lambda: transport)("program")

    assert raised.value.returncode == 7
    assert raised.value.stdout == b""
    assert raised.value.stderr == b""


def test_qemu_executor_rejects_native_cwd_and_conflicting_input():
    executor = QemuExecutor(lambda: _Transport([]))
    with pytest.raises(NotImplementedError, match="working-directory"):
        executor("program", cwd="/tmp")
    with pytest.raises(ValueError, match="stdin"):
        executor("program", stdin=io.BytesIO(), input=b"value")
    with pytest.raises(ValueError, match="must not be empty"):
        executor("")
    with pytest.raises(ValueError, match="timeout"):
        executor("program", timeout=-1)


def test_qemu_executor_timeout_retains_orphan_pid_and_partial_deadline():
    now = [10.0]
    transport = _Transport(
        [
            {"exited": False},
            {
                "exited": False,
                "out-data": _encoded(b"partial"),
                "err-data": _encoded(b"warning"),
            },
        ]
    )

    def clock():
        return now[0]

    def sleep(delay):
        now[0] += delay

    executor = QemuExecutor(
        lambda: transport,
        poll_interval=0.06,
        max_poll_interval=0.06,
        clock=clock,
        sleep=sleep,
    )
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        executor("program", timeout=0.1)

    assert raised.value.timeout == 0.1
    assert raised.value.pid == 41
    assert raised.value.orphaned is True
    assert raised.value.output == b"partial"
    assert raised.value.stderr == b"warning"
    assert transport.calls[0][1] == pytest.approx(0.1)
    assert transport.calls[-1][1] == pytest.approx(0.04)


def test_qemu_executor_normalizes_transport_timeout_and_bad_replies():
    class _Timeout:
        def execute(self, request, timeout=None):
            raise TimeoutError("agent")

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        QemuExecutor(lambda: _Timeout())("program", timeout=1)
    assert raised.value.orphaned is True

    bad_pid = _Transport([])
    bad_pid.execute = lambda request, timeout=None: {"pid": "bad"}
    with pytest.raises(GuestAgentProtocolError, match="PID"):
        QemuExecutor(lambda: bad_pid)("program")

    invalid_output = _Transport([{"exited": True, "exitcode": 0, "out-data": "!"}])
    with pytest.raises(GuestAgentProtocolError, match="Base64"):
        QemuExecutor(lambda: invalid_output)("program")

    invalid_status = _Transport([{"exitcode": 0}])
    with pytest.raises(GuestAgentProtocolError, match="boolean exited"):
        QemuExecutor(lambda: invalid_status)("program")
