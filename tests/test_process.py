"""Persistent process contracts and the AsyncSSH adapter."""

from __future__ import annotations

import pytest

from hostctl.host.ssh import SshConfig, SshHost
from hostctl.process import Process, SshProcess, TerminalOptions


class _Reader:
    def __init__(self, value):
        self.value = value

    async def read(self, size=-1):
        if size < 0:
            value, self.value = self.value, self.value[:0]
        else:
            value, self.value = self.value[:size], self.value[size:]
        return value


class _Writer:
    def __init__(self):
        self.values = []
        self.eof = False

    def write(self, value):
        self.values.append(value)

    async def drain(self):
        return None

    def write_eof(self):
        self.eof = True


class _Completed:
    returncode = 7


class _Process:
    def __init__(self):
        self.returncode = None
        self.stdin = _Writer()
        self.stdout = _Reader(b"stdout")
        self.stderr = _Reader(b"stderr")
        self.sizes = []
        self.closed = False
        self.waited_closed = False
        self.terminated = False
        self.killed = False

    def change_terminal_size(self, *size):
        self.sizes.append(size)

    async def wait(self, check=False, timeout=None):
        assert check is False
        self.returncode = 7
        return _Completed()

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited_closed = True


class _SSH:
    def __init__(self):
        self.process = _Process()
        self.calls = []

    def is_closed(self):
        return False

    async def create_process(self, command=None, **options):
        self.calls.append((command, options))
        return self.process


def test_terminal_options_validate_and_publish_asyncssh_size():
    terminal = TerminalOptions("screen", 120, 40, 800, 600)
    assert terminal.size == (120, 40, 800, 600)
    with pytest.raises(ValueError, match="positive"):
        TerminalOptions(columns=0)
    with pytest.raises(ValueError, match="negative"):
        TerminalOptions(pixel_width=-1)


def test_ssh_spawn_renders_command_and_requests_terminal():
    host = SshHost(SshConfig("host"))
    ssh = _SSH()
    host._ssh = ssh

    process = host.spawn(
        ["printf", "%s", "a b"],
        cwd="/tmp/a b",
        env={"NAME": "value"},
        terminal=TerminalOptions(columns=100, rows=30),
        encoding="utf-8",
    )

    command, options = ssh.calls[0]
    assert "printf" in command
    assert "cd '/tmp/a b'" in command
    assert "export NAME=value" in command
    assert options["request_pty"] is True
    assert options["term_type"] == "xterm-256color"
    assert options["term_size"] == (100, 30, 0, 0)
    assert options["encoding"] == "utf-8"
    assert isinstance(process, Process)


def test_ssh_spawn_without_command_opens_default_shell():
    host = SshHost(SshConfig("host"))
    ssh = _SSH()
    host._ssh = ssh

    host.spawn(terminal=TerminalOptions())

    assert ssh.calls[0][0] is None


def test_ssh_process_controls_streams_lifecycle_and_terminal():
    remote = _Process()
    process = SshProcess(remote, "command")

    process.write(b"input")
    assert remote.stdin.values == [b"input"]
    assert process.read(3) == b"std"
    assert process.read_stderr() == b"stderr"
    process.send_eof()
    process.resize(90, 25)
    process.terminate()
    process.kill()
    assert process.wait() == 7
    process.close()
    process.close()

    assert remote.stdin.eof
    assert remote.sizes == [(90, 25, 0, 0)]
    assert remote.terminated
    assert remote.killed
    assert remote.closed
    assert remote.waited_closed
