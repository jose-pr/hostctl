"""What a serial console host promises, and what it refuses to guess."""

from __future__ import annotations

import subprocess

import pytest

from hostctl import (
    Host,
    LoginStep,
    PromptConsoleProfile,
    RawConsoleProfile,
    SerialConfig,
    SerialHost,
)
from hostctl.serial import ConsoleProtocolError


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


def _framed_console():
    """A device that answers each line with output and a status marker."""

    def respond(console, data):
        if data.endswith(b"\r\n") and data != b"\r\n":
            console.reads.append(b"output\nSTATUS0\n> ")

    return _Console(reads=[b"> "], on_write=respond)


def _framed_host(console, **kwargs):
    profile = PromptConsoleProfile(
        rb"> ",
        status_marker=rb"STATUS0",
        reliable_status=True,
        status_parser=lambda match: 0,
        **kwargs,
    )
    return SerialHost(SerialConfig("loop://", serial_port=console, protocol=profile))


def test_a_structured_command_is_refused_rather_than_posix_quoted():
    """`shlex.join` is POSIX quoting on a host with no shell flavour.

    On a Cisco-style console `["show", "run | include hostname"]` went on
    the wire as `show 'run | include hostname'`, which the device takes
    literally and rejects.
    """
    host = _framed_host(_framed_console())

    with pytest.raises(NotImplementedError, match="argv quoting"):
        host.run(["show", "run | include hostname"])


def test_each_command_is_its_own_framed_exchange():
    """Joined with `;`, two commands went out as one line.

    Most device consoles parse that as a single malformed command -- and a
    profile that frames status per exchange could only report one status
    for both.
    """
    console = _framed_console()
    host = _framed_host(console)

    result = host.run("set name", "commit")

    commands = [value for value in console.writes if value.strip()]
    assert commands == [b"set name\r\n", b"commit\r\n"]
    assert result.returncode == 0


@pytest.mark.parametrize("keyword", ("stdin", "bufsize"))
def test_run_refuses_the_keywords_it_used_to_drop(keyword):
    """`input=` was refused explicitly while these two were ignored."""
    host = _framed_host(_framed_console())
    argument = {"stdin": subprocess.PIPE, "bufsize": 0}[keyword]

    with pytest.raises(NotImplementedError):
        host.run("show version", **{keyword: argument})


def test_spawn_with_an_encoding_reads_text():
    """`spawn(encoding=...)` gave `str` from SSH and QEMU, `bytes` here.

    Any code written against both concatenated the result with a `str` and
    got a `TypeError`.
    """
    console = _Console(reads=[b"hello"])
    host = SerialHost(SerialConfig("loop://", serial_port=console))

    with host.spawn(encoding="utf-8", errors="replace") as session:
        assert session.read(5) == "hello"


def test_spawn_without_a_text_mode_still_reads_bytes():
    console = _Console(reads=[b"hello"])
    host = SerialHost(SerialConfig("loop://", serial_port=console))

    with host.spawn() as session:
        assert session.read(5) == b"hello"


def test_an_echoed_password_is_redacted_from_a_timeout_transcript():
    """Consoles echo what they are sent, and the transcript rides the error.

    `LoginStep.__repr__` prints `<redacted>`; the transcript attached to
    `TimeoutError.output` -- which `run()` copies onto
    `TimeoutExpired.output`, a value callers routinely log -- carried the
    password itself.
    """
    console = _Console(reads=[b"login: ", b"Password: hunter2\r\nnope"])
    profile = PromptConsoleProfile(
        rb"> ",
        login=(
            LoginStep(rb"login: ", b"root"),
            LoginStep(rb"Password: ", b"hunter2", secret=True),
        ),
        status_marker=rb"STATUS0",
        reliable_status=True,
    )
    host = SerialHost(SerialConfig("loop://", serial_port=console, protocol=profile))

    with pytest.raises(ConsoleProtocolError) as caught:
        host.connect()

    transcript = b"".join(
        getattr(error, "output", b"") or b""
        for error in (caught.value, caught.value.__cause__)
    )
    assert b"hunter2" not in transcript
    assert b"<redacted>" in transcript


def test_a_negotiation_timeout_is_a_protocol_error():
    """It escaped as a bare `TimeoutError`, past every documented handler."""
    console = _Console(reads=[b"login: "])
    profile = PromptConsoleProfile(
        rb"> ",
        login=(LoginStep(rb"login: ", b"root"),),
        status_marker=rb"STATUS0",
        reliable_status=True,
    )
    host = SerialHost(SerialConfig("loop://", serial_port=console, protocol=profile))

    with pytest.raises(ConsoleProtocolError):
        host.connect()


def test_closing_the_executor_forces_a_fresh_login():
    """The negotiation flag outlived the transport it was established on.

    `host.executor.close()` -- a public method on a public property --
    dropped the port without touching the flag, so the next `run()` opened
    a fresh port, skipped every `LoginStep`, and typed the command at the
    device's `login:` prompt.
    """
    logins = []

    class _Recording(RawConsoleProfile):
        def negotiate(self, process):
            logins.append(True)

    consoles = []

    def factory(port, **options):
        consoles.append(_Console())
        return consoles[-1]

    host = SerialHost(
        SerialConfig("loop://", serial_factory=factory, protocol=_Recording())
    )

    host.connect()
    host.connect()
    assert len(logins) == 1 and len(consoles) == 1

    # A caller recovering from a transport error without discarding
    # host-level state -- the only reachable trigger, and enough.
    host.executor.close()
    host.connect()
    assert len(consoles) == 2, "a fresh port was not opened"
    assert len(logins) == 2, "the new port was never logged into"


def test_a_serial_uri_refuses_a_credential_nothing_reads():
    """Stored, whitelisted, and read by nothing.

    `Host("serial:///dev/ttyUSB0", password=...)` accepted a credential
    that had no effect -- console credentials live in the profile's
    `login=` steps and nowhere else.
    """
    with pytest.raises(ValueError, match="password"):
        Host("serial:///dev/ttyUSB0", password="s3cret")

    assert not hasattr(SerialConfig("loop://"), "password")


def test_an_unimplemented_process_member_fails_loudly():
    """Protocol members are not abstract, so a gap used to return `None`.

    Five adapters inherit `Process` explicitly. An inherited-but-missing
    member was a real method returning `None`: instantiation succeeded,
    `isinstance(x, Process)` passed, and `if process.wait():` read a
    missing implementation as "exited 0".
    """
    from hostctl.process import Process

    class _Half(Process):
        def read(self, size=-1):
            return b""

    half = _Half()
    assert isinstance(half, Process)
    assert half.read() == b""

    with pytest.raises(NotImplementedError, match="_Half.wait"):
        half.wait()
    with pytest.raises(NotImplementedError, match="_Half.close"):
        half.close()
    # `returncode` deliberately still answers `None`: on the 3.9 floor
    # `isinstance` evaluates a protocol property, so raising there breaks the
    # runtime check itself.
    assert half.returncode is None
