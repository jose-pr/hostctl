from __future__ import annotations

import subprocess

import pytest

from hostctl import (
    Host,
    HostConfig,
    LoginStep,
    PromptConsoleProfile,
    RawConsoleProfile,
    SerialConfig,
    SerialHost,
)


class _Console:
    is_open = True
    dtr = False
    rts = False

    def __init__(self, reads=(), on_write=None):
        self.reads = list(reads)
        self.writes = []
        self.on_write = on_write

    def read(self, size=1):
        if not self.reads:
            return b""
        value = self.reads.pop(0)
        return value[:size]

    def write(self, data):
        self.writes.append(data)
        if self.on_write:
            self.on_write(self, data)
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.is_open = False

    def send_break(self, duration=0.25):
        return None


def test_serial_uri_round_trip_and_secret_safe_dispatch():
    config = SerialConfig("/dev/tty USB0", protocol=RawConsoleProfile())
    assert "tty%20USB0" in str(config)
    restored = HostConfig(str(config))
    assert isinstance(restored, SerialConfig)
    assert str(restored) == str(config)
    host = Host(str(config), serial_port=_Console())
    assert isinstance(host, SerialHost)
    assert host.capabilities == frozenset(("session",))


def test_raw_serial_shell_session_is_exclusive_and_merged():
    console = _Console()
    host = SerialHost(SerialConfig("loop://", serial_port=console))
    with host.shell.session("show version") as session:
        session.send("show interfaces")
    assert console.writes == [b"show version\r\n", b"show interfaces\r\n"]
    with pytest.raises(NotImplementedError):
        host.path("/")


def test_prompt_profile_frames_reliable_run_and_check():
    def respond(console, data):
        if data.endswith(b"\r\n") and data != b"\r\n":
            console.reads.extend([b"show\r\noutput\nSTATUS0\n> "])

    console = _Console(reads=[b"> "], on_write=respond)
    profile = PromptConsoleProfile(
        rb"> ",
        status_marker=rb"STATUS0",
        reliable_status=True,
    )
    host = SerialHost(SerialConfig("loop://", protocol=profile, serial_port=console))
    result = host.run("show")
    assert result.returncode == 0
    assert result.stdout == b"output\n"
    assert host.capabilities == frozenset(("session", "run"))


def test_prompt_login_steps_are_ordered():
    console = _Console(reads=[b"login: ", b"> "])
    profile = PromptConsoleProfile(
        rb"> ", login=(LoginStep(rb"login: ", b"admin", secret=True),)
    )
    host = SerialHost(SerialConfig("loop://", protocol=profile, serial_port=console))
    host.connect()
    assert console.writes[-1] == b"admin\r\n"


def test_raw_run_is_explicitly_unsupported():
    host = SerialHost(SerialConfig("loop://", serial_port=_Console()))
    with pytest.raises(NotImplementedError, match="reliable run"):
        host.run("show")
