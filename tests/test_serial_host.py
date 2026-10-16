from __future__ import annotations

import io
import subprocess
import sys

import pytest

from hostctl.host._host import Host
from hostctl.host._config import HostConfig
from hostctl.transports.serial._console import (
    LoginStep,
    PromptConsoleProfile,
    RawConsoleProfile,
)
from hostctl.transports.serial._host import SerialConfig, SerialHost


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
    config = SerialConfig("/dev/tty USB0", profile=RawConsoleProfile())
    assert "tty%20USB0" in str(config)
    restored = HostConfig(str(config))
    assert isinstance(restored, SerialConfig)
    assert str(restored) == str(config)
    host = Host(str(config), serial_port=_Console())
    assert isinstance(host, SerialHost)
    assert host.capabilities == frozenset(("session",))


@pytest.mark.parametrize(
    ("uri", "device", "rendered"),
    [
        ("serial:///dev/ttyUSB0", "/dev/ttyUSB0", "serial:///dev/ttyUSB0"),
        ("serial:/dev/ttyUSB0", "/dev/ttyUSB0", "serial:///dev/ttyUSB0"),
        ("serial:///COM3", "COM3", "serial:///COM3"),
        ("serial:///com12", "com12", "serial:///com12"),
        ("serial:COM3", "COM3", "serial:///COM3"),
        ("serial:///%5C%5C.%5CCOM10", "\\\\.\\COM10", "serial:///%5C%5C.%5CCOM10"),
        ("serial:///loop%3A%2F%2F", "loop://", "serial:///loop://"),
        (
            "serial:///rfc2217://h:4001",
            "rfc2217://h:4001",
            "serial:///rfc2217://h:4001",
        ),
        # An encoded slash after the leading one is part of the device.
        ("serial:///%2Fdev%2FttyUSB0", "/dev/ttyUSB0", "serial:///dev/ttyUSB0"),
        ("serial:///%2FCOM3", "/COM3", "serial:///%2FCOM3"),
        ("serial:///%2F%2F.%2FCOM10", "//./COM10", "serial:///%2F/./COM10"),
        ("serial:///dev/tty%20USB0", "/dev/tty USB0", "serial:///dev/tty%20USB0"),
        (
            "serial:///dev/serial/by-path/pci-0000:00:14.0-port0",
            "/dev/serial/by-path/pci-0000:00:14.0-port0",
            "serial:///dev/serial/by-path/pci-0000:00:14.0-port0",
        ),
        ("serial:ttyUSB0", "ttyUSB0", "serial:ttyUSB0"),
    ],
)
def test_a_serial_uri_path_reads_like_a_file_uri(uri, device, rendered):
    config = HostConfig(uri)
    assert config.device == device
    assert config.connection_uri.split("?")[0] == rendered
    assert HostConfig(config.connection_uri) == config


@pytest.mark.parametrize(
    "device",
    [
        "/dev/ttyUSB0",
        "/COM3",
        "//./COM10",
        "\\\\.\\COM10",
        "COM3",
        "loop://",
        "socket://h:7000?logging=debug",
        "/tmp/a://b",
        "dev/ttyUSB0",
        "/dev/a@b#c",
    ],
)
def test_every_serial_device_round_trips_through_its_uri(device):
    config = SerialConfig(device, baudrate=9600)
    restored = HostConfig(config.connection_uri)
    assert restored == config
    assert restored.device == device


def test_a_serial_uri_with_four_slashes_names_the_right_spelling():
    with pytest.raises(ValueError, match=r"serial:///dev/ttyUSB0"):
        HostConfig("serial:////dev/ttyUSB0")


@pytest.mark.parametrize("uri", ["serial:", "serial:///"])
def test_a_serial_uri_requires_a_device(uri):
    with pytest.raises(ValueError, match="requires a device"):
        HostConfig(uri)


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
    host = SerialHost(SerialConfig("loop://", profile=profile, serial_port=console))
    result = host.run("show")
    assert result.returncode == 0
    assert result.stdout == b"output\n"
    assert host.capabilities == frozenset(("session", "run"))


def _prompt_host(console_reads=(b"> ",)):
    def respond(console, data):
        if data.endswith(b"\r\n") and data != b"\r\n":
            console.reads.extend([b"show\r\noutput\nSTATUS0\n> "])

    console = _Console(reads=list(console_reads), on_write=respond)
    profile = PromptConsoleProfile(
        rb"> ", status_marker=rb"STATUS0", reliable_status=True
    )
    return SerialHost(SerialConfig("loop://", profile=profile, serial_port=console))


def test_serial_uncaptured_output_is_written_not_discarded(monkeypatch):
    """`capture_output=False` routes the transcript, it does not drop it.

    Serial reimplemented the stream contract and treated a `None` stdout
    target as "discard"; every other transport, and `dispatch_output`
    itself, treats it as `sys.stdout`.
    """
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stdout", sink)
    result = _prompt_host().run("show", capture_output=False, text=True)
    assert result.stdout is None
    assert sink.getvalue() == "output\n"


def test_serial_explicit_stdout_target_still_wins():
    target = io.BytesIO()
    result = _prompt_host().run("show", stdout=target, capture_output=False)
    assert result.stdout is None
    assert target.getvalue() == b"output\n"


def test_serial_devnull_target_discards_deliberately(monkeypatch):
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stdout", sink)
    result = _prompt_host().run("show", stdout=subprocess.DEVNULL, capture_output=False)
    assert result.stdout is None
    assert sink.getvalue() == ""


def test_serial_errors_alone_selects_text_mode():
    """Matches every other executor and `subprocess.run`."""
    assert _prompt_host().run("show", errors="replace").stdout == "output\n"
    assert _prompt_host().run("show").stdout == b"output\n"


def test_prompt_login_steps_are_ordered():
    console = _Console(reads=[b"login: ", b"> "])
    profile = PromptConsoleProfile(
        rb"> ", login=(LoginStep(rb"login: ", b"admin", secret=True),)
    )
    host = SerialHost(SerialConfig("loop://", profile=profile, serial_port=console))
    host.connect()
    assert console.writes[-1] == b"admin\r\n"


def test_raw_run_is_explicitly_unsupported():
    host = SerialHost(SerialConfig("loop://", serial_port=_Console()))
    with pytest.raises(NotImplementedError, match="reliable run"):
        host.run("show")


def test_prompt_profile_paging_and_terminal_setup_hooks():
    setup = []
    console = _Console(reads=[b"> "])
    profile = PromptConsoleProfile(
        rb"> ",
        paging_prompt=rb"--More--",
        paging_continue=b" ",
        paging_disable=b"terminal length 0",
        terminal_setup=lambda process, columns, rows: setup.append((columns, rows)),
    )
    host = SerialHost(SerialConfig("loop://", profile=profile, serial_port=console))
    host.connect()
    raw = host._executor.open()
    try:
        profile.resize(raw, 120, 40)
    finally:
        raw.close()
    assert setup == [(120, 40)]
    assert b"terminal length 0\r\n" in console.writes


def test_prompt_profile_uses_status_parser():
    def respond(console, data):
        if data.endswith(b"\r\n") and data != b"\r\n":
            console.reads.extend([b"cmd\r\nresult\nS=7\n> "])

    console = _Console(reads=[b"> "], on_write=respond)
    profile = PromptConsoleProfile(
        rb"> ",
        status_marker=rb"S=(\d+)",
        reliable_status=True,
        status_parser=lambda match: int(match.group(1)),
    )
    host = SerialHost(SerialConfig("loop://", profile=profile, serial_port=console))
    with pytest.raises(subprocess.CalledProcessError) as raised:
        host.run("cmd")
    assert raised.value.returncode == 7


def test_closing_a_host_under_a_live_session_releases_the_lease():
    """`SerialHost.close()` closed the port without releasing the exclusive
    lease, so a session the caller did not close -- an exception inside
    `with host:` is enough -- held it forever, and every later `connect()`,
    `spawn()` and `run()` on that host raised RuntimeError. The host cannot
    even reconnect, because `connect()` itself opens a process."""
    console = _Console()
    host = SerialHost(SerialConfig("loop://", serial_port=console))

    session = host.shell.session()
    assert session is not None
    host.close()

    console.is_open = True  # the caller reopens the same injected port
    with host.shell.session("show version") as reopened:
        reopened.send("show interfaces")

    assert console.writes[-1] == b"show interfaces\r\n"


def test_the_documented_session_shorthand_works_on_a_serial_host():
    """`with host.shell as session:` is a serial console's primary mode and
    raised `TypeError: object does not support the context manager
    protocol`, because the binding is not a `Shell`."""
    console = _Console()
    host = SerialHost(SerialConfig("loop://", serial_port=console))

    with host.shell as session:
        session.send("show version")

    assert console.writes == [b"show version\r\n"]


def test_a_serial_shell_refuses_what_it_cannot_mean():
    console = _Console()
    host = SerialHost(SerialConfig("loop://", serial_port=console))

    with pytest.raises(NotImplementedError, match="no shell flavour"):
        host.shell(cwd="/srv")


def test_a_spawned_console_honours_the_errors_it_was_given():
    """`spawn(errors=...)` was accepted and dropped, so a console whose
    device cannot represent a character answered `errors="replace"` with a
    `UnicodeEncodeError`."""
    console = _Console()
    host = SerialHost(SerialConfig("loop://", serial_port=console))

    with host.spawn(encoding="ascii", errors="replace") as session:
        session.write("caf\N{LATIN SMALL LETTER E WITH ACUTE}\n")

    assert console.writes == [b"caf?\n"]


@pytest.mark.parametrize("errors", [None, "replace"])
def test_text_read_keeps_a_character_split_across_idle_reads(errors):
    """An idle line reads `b""`, which is not EOF: the decoder must hold the
    first byte of `é` until the second arrives."""
    console = _Console()
    console.reads = [b"\xc3", b"", b"\xa9"]
    host = SerialHost(SerialConfig("loop://", serial_port=console))
    with host.spawn(encoding="utf-8", errors=errors) as process:
        received = "".join(process.read() for _ in range(3))
    assert received == "é"
