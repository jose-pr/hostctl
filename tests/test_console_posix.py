"""`PosixConsoleProfile`: framed commands through a POSIX shell's terminal.

Parsing a terminal's echo is unsafe: the echo comes back as stdout, `input=`
typed into the terminal can hang, and a ^C inside it runs the rest as a
command. The framed protocol is pinned in four layers:

* what is TYPED: printable ASCII only, bounded lines, nothing a line editor
  interprets, and no marker ever contiguous in what the terminal echoes;
* how output is FRAMED: only markers the shell printed count;
* `SerialHost.run()` against an emulated terminal (echo with redraws, output
  in fragments, a 4 KiB input queue), on every platform;
* a real `sh` over pipes, and a real shell in a real pty where the platform
  has one -- where each safety property is proven against the real thing.
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import sys
import time

import pytest

from hostctl.console import LoginStep, PosixConsoleProfile, console_stream
from hostctl.console import _posix
from hostctl.console._posix import _Frame, _typed_batches
from hostctl.exceptions import ConsoleProtocolError, OperationNotStartedError
from hostctl.transports.serial import SerialConfig, SerialHost

TOKEN = "0123456789abcdef"
HOSTILE = "x\x03echo injected\n\ttab !! $(id) `id` 'q\" end\x04\x1a\n"

# -- what is typed -------------------------------------------------------------


def _lines(command, **options):
    return [
        line for batch in _typed_batches(command, TOKEN, **options) for line in batch
    ]


def _decoded(lines, name):
    chunks = [
        re.match(
            r"printf %s '([A-Za-z0-9+/=]*)' >> \"\$\{__hc\w+d:\?\}\"/" + name, line
        )
        for line in lines
    ]
    return base64.b64decode("".join(match.group(1) for match in chunks if match))


def test_typed_lines_carry_nothing_a_line_editor_would_interpret():
    payload = HOSTILE.encode() + bytes(range(256))
    lines = _lines(HOSTILE.encode(), input=payload)

    for line in lines:
        assert all(32 <= ord(ch) < 127 for ch in line), line
        assert "!" not in line
    assert _decoded(lines, "c") == HOSTILE.encode()
    assert _decoded(lines, "i") == payload


def test_every_typed_line_fits_a_minimal_line_editor():
    """busybox ash drops a typed line near 3 KiB (1 KiB in its default
    build); a long command and a long input are chunked like each other."""
    lines = _lines(b"echo " + b"y" * 20000, input=os.urandom(50000))

    assert max(len(line) for line in lines) < 1024
    assert _decoded(lines, "c") == b"echo " + b"y" * 20000


def test_no_typed_line_contains_a_marker_contiguously():
    for line in _lines(b"true", input=b"x" * 5000):
        assert f"__ST{TOKEN}__" not in line
        assert f"__OUT{TOKEN}__" not in line
        assert not re.search(rf"__EN{TOKEN}_\d*", line)
        assert not re.search(rf"__AK{TOKEN}_\d+__", line)


def test_batches_bound_the_bytes_in_flight_and_are_acknowledged():
    batches = _typed_batches(b"cat", TOKEN, input=os.urandom(20000))

    assert len(batches) > 3
    for number, batch in enumerate(batches, 1):
        assert sum(len(line) + 1 for line in batch) <= _posix._BATCH
        if number < len(batches):
            assert batch[-1].endswith(f'printf %s%s "__AK" "{TOKEN}_{number}__"')
    assert "stty -opost" in batches[-1][-1]
    assert len(_typed_batches(b"true", TOKEN)) == 1


def test_stdin_is_dev_null_without_input_and_stderr_mode_is_chosen():
    plain = _lines(b"true")[-1]
    assert "< /dev/null" in plain and "2>&1" not in plain
    assert "2>&1" in _lines(b"true", merge_stderr=True)[-1]
    assert "< /dev/null" not in _lines(b"cat", input=b"x")[-1]


# -- framing -------------------------------------------------------------------


def _stream(out=b"", err=b"", rc=0):
    """The bytes a terminal sends for one command: banner, echo, frames."""
    junk = b"Welcome\r\n$ "
    for line in _lines(b"cmd"):
        text = line.encode()
        junk += text[:40] + b"\r\x1b[K" + text[40:] + b"\r\n"
    body = (
        f"__ST{TOKEN}__".encode()
        + out
        + f"__OUT{TOKEN}__".encode()
        + err
        + f"__EN{TOKEN}_{rc}_EN__".encode()
    )
    return junk + body + b"\r\n$ "


def _feed_all(data, size):
    frame = _Frame(TOKEN)
    emitted_out = emitted_err = b""
    for offset in range(0, len(data), size):
        out, err = frame.feed(data[offset : offset + size])
        emitted_out += out
        emitted_err += err
        if frame.state == "done":
            break
    return frame, emitted_out, emitted_err


@pytest.mark.parametrize("size", [1, 2, 3, 7, 13, 64, 100000])
def test_frame_ignores_echo_and_banner_at_every_split(size):
    frame, out, err = _feed_all(_stream(out=b"hi\n", err=b"oops\n", rc=3), size)

    assert (bytes(frame.out), bytes(frame.err), frame.returncode) == (
        b"hi\n",
        b"oops\n",
        3,
    )
    assert (out, err) == (b"hi\n", b"oops\n")


def test_frame_keeps_exact_bytes_with_no_trailing_newline():
    binary = bytes(range(256)) + b"\r\n\r" + b"__ST" + b"__EN"
    frame, _, _ = _feed_all(_stream(out=binary), 9)

    assert bytes(frame.out) == binary


def test_frame_streams_a_short_line_at_once_and_holds_back_only_a_partial_marker():
    frame = _Frame(TOKEN)
    frame.feed(b"echo...\r\n" + f"__ST{TOKEN}__".encode())

    assert frame.feed(b"line1\n") == (b"line1\n", b"")
    assert frame.feed(b"data__OU") == (b"data", b"")
    frame.feed(f"T{TOKEN}__".encode())
    assert frame.state == "err"


def test_frame_is_linear_and_drops_what_precedes_it():
    frame = _Frame(TOKEN)
    for _ in range(200):
        frame.feed(b"console noise " * 512)
    assert len(frame.buffer) < 64

    started = time.monotonic()
    done, _, _ = _feed_all(_stream(out=b"a" * (4 * 1024 * 1024)), 1024)
    assert done.returncode == 0 and len(done.out) == 4 * 1024 * 1024
    assert time.monotonic() - started < 10


# -- an emulated terminal behind SerialHost --------------------------------------

_SUFFIX = re.compile(r'; printf %s%s "__AK" "(\w+)__"$')
_CHUNK_LINE = re.compile(r"printf %s '([A-Za-z0-9+/=]*)' >> \"\$\{__hc\w+d:\?\}\"/(\w)")


class _Terminal:
    """A POSIX shell's terminal at the byte level, behind a `SerialLike` port.

    Echoes every typed line (redrawn mid-line, as zsh does), answers in small
    fragments, and holds 4 KiB of typed input: bytes past that are lost, as
    on a real serial line, so a writer that does not wait for the shell
    corrupts its own command. It runs exactly the syntax the profile types:
    `responder(command, stdin, merged)` returns `(stdout, stderr, status)`,
    or `None` for a command that never finishes.
    """

    is_open = True
    dtr = rts = False

    def __init__(self, responder=None, *, login=(), fragment=5):
        self.responder = responder or (lambda command, data, merged: (b"", b"", 0))
        self.login = list(login)
        self.fragment = fragment
        self.timeout = 0.1
        self.write_timeout = 10
        self.pending = bytearray(b"\r\nlogin: " if self.login else b"Welcome\r\n$ ")
        self.typed = bytearray()
        self.files = {}
        self.writes = []
        self.lines = []
        self.lost = 0
        self.interrupts = 0
        self.fail_writes = False

    def read(self, size=1):
        if not self.pending:
            time.sleep(min(self.timeout or 0.01, 0.01))
            return b""
        take = min(size, self.fragment, len(self.pending))
        value = bytes(self.pending[:take])
        del self.pending[:take]
        return value

    def write(self, data):
        if self.fail_writes:
            raise OSError("link down")
        data = bytes(data)
        self.writes.append(data)
        if data == b"\x03":
            self.interrupts += 1
            self.typed.clear()
            self.pending += b"^C\r\n$ "
            return len(data)
        room = 4096 - len(self.typed)
        if len(data) > room:
            self.lost += len(data) - room
            data = data[:room]
        self.typed += data
        while b"\n" in self.typed:
            line, _, rest = bytes(self.typed).partition(b"\n")
            self.typed = bytearray(rest)
            self._line(line.decode("ascii", "replace"))
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def send_break(self, duration=0.25):
        pass

    def _line(self, line):
        self.lines.append(line)
        self.pending += line[:30].encode() + b"\r\x1b[K" + line[30:].encode() + b"\r\n"
        if self.login:
            if line == self.login[0][1]:
                self.login.pop(0)
                self.pending += self.login[0][0] if self.login else b"\r\n$ "
            else:
                self.pending += b"\r\nlogin: "
            return
        ack = _SUFFIX.search(line)
        if ack:
            line = line[: ack.start()]
        if line.endswith("=$(mktemp -d)"):
            self.files = {}
        elif _CHUNK_LINE.fullmatch(line):
            match = _CHUNK_LINE.fullmatch(line)
            self.files[match.group(2)] = self.files.get(
                match.group(2), ""
            ) + match.group(1)
        elif "stty -opost" in line:
            self._run(line)
        if ack:
            self.pending += f"__AK{ack.group(1)}__".encode() + b"\r\n$ "
        elif line and "stty -opost" not in line:
            self.pending += b"$ "

    def _run(self, line):
        token = re.search(r'"__ST" "(\w+)__"', line).group(1)
        command = base64.b64decode(self.files.get("c", ""))
        uses_input = re.search(r'< "\$\{__hc\w+d:\?\}"/f ', line) is not None
        data = base64.b64decode(self.files.get("i", "")) if uses_input else b""
        merged = "2>&1" in line
        answer = self.responder(command, data, merged)
        if answer is None:
            return
        out, err, status = answer
        self.pending += (
            f"__ST{token}__".encode()
            + out
            + f"__OUT{token}__".encode()
            + err
            + f"__EN{token}_{status}_EN__\r\n$ ".encode()
        )


def _host(terminal, **options):
    profile = PosixConsoleProfile(**options)
    return SerialHost(SerialConfig("pty", profile=profile, serial_port=terminal))


def _echo(command, data, merged):
    """Like `sh -c`: stdout `out:<command>[|in:<stdin>]`, stderr `err`."""
    out = b"out:" + command + (b"|in:" + data if data else b"")
    return (out + b"err", b"", 0) if merged else (out, b"err", 0)


def test_run_returns_exactly_what_the_command_printed():
    terminal = _Terminal(_echo)

    result = _host(terminal).run("echo hi", timeout=10)

    assert (result.stdout, result.stderr, result.returncode) == (
        b"out:echo hi",
        b"err",
        0,
    )
    assert terminal.lost == 0
    assert max(len(line) for line in terminal.lines) < 1024


def test_stderr_to_stdout_merges_and_text_mode_decodes():
    host = _host(_Terminal(_echo))

    merged = host.run("x", stderr=subprocess.STDOUT, timeout=10)
    text = host.run("x", text=True, timeout=10)

    assert (merged.stdout, merged.stderr) == (b"out:xerr", None)
    assert (text.stdout, text.stderr) == ("out:x", "err")


def test_a_failing_status_raises_with_stdout_and_stderr():
    host = _host(
        _Terminal(
            lambda command, data, merged: (
                (b"partial", b"boom", 3) if command == b"false" else (b"", b"", 0)
            )
        )
    )

    with pytest.raises(subprocess.CalledProcessError) as caught:
        host.run("false", timeout=10)

    assert (caught.value.returncode, caught.value.output, caught.value.stderr) == (
        3,
        b"partial",
        b"boom",
    )


@pytest.mark.parametrize(
    "value",
    [b"", b"plain", HOSTILE.encode(), bytes(range(256)) * 64],
    ids=["empty", "plain", "hostile", "binary-16k"],
)
def test_input_reaches_the_command_byte_for_byte(value):
    terminal = _Terminal(_echo)

    result = _host(terminal).run("cat", input=value, timeout=30)

    assert result.stdout == b"out:cat" + (b"|in:" + value if value else b"")
    assert terminal.lost == 0


def test_a_stdin_stream_is_read_and_delivered_as_input():
    result = _host(_Terminal(_echo)).run(
        "cat", stdin=io.BytesIO(b"from a stream"), timeout=10
    )

    assert result.stdout == b"out:cat|in:from a stream"


def test_input_with_stdin_or_with_several_commands_is_refused():
    host = _host(_Terminal(_echo))

    with pytest.raises(ValueError):
        host.run("cat", input=b"x", stdin=io.BytesIO(b"y"))
    with pytest.raises(NotImplementedError, match="one command"):
        host.run("cat", "cat", input=b"x")


def test_a_timeout_interrupts_and_raises_the_hostctl_shape():
    terminal = _Terminal(
        lambda command, data, merged: (b"", b"", 0) if command == b":" else None
    )
    host = _host(terminal)
    host.connect()

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        host.run("sleep 100", timeout=0.5)

    assert caught.value.orphaned is True
    assert caught.value.stderr == b""
    assert terminal.writes[-1] == b"\x03"


def test_a_timeout_while_typing_acknowledged_batches_never_types_the_command():
    """The command line is typed only after the shell acknowledged every
    earlier batch; a shell that stops answering never receives it."""
    terminal = _Terminal(_echo)
    host = _host(terminal)
    host.connect()
    terminal.pending.clear()
    typed = []
    terminal._line = typed.append

    with pytest.raises(subprocess.TimeoutExpired):
        host.run("cat", input=os.urandom(20000), timeout=0.5)

    assert typed and not any("stty -opost" in line for line in typed)


def test_a_write_failure_before_the_command_line_did_not_start_it():
    terminal = _Terminal(_echo)
    host = _host(terminal)
    host.connect()
    terminal.fail_writes = True

    with pytest.raises(OperationNotStartedError):
        host.run("rm -rf /data", timeout=5)


def test_login_steps_then_a_framed_probe_negotiate_the_console():
    terminal = _Terminal(
        _echo, login=[(b"login: ", "root"), (b"Password: ", "hunter2")]
    )
    host = _host(
        terminal,
        login=[("login: ", "root"), ("Password: ", "hunter2")],
    )

    assert host.run("id", timeout=10).stdout == b"out:id"
    assert any(line.endswith("=$(mktemp -d)") for line in terminal.lines)


def test_a_shell_that_never_answers_the_probe_fails_negotiation():
    host = _host(_Terminal(lambda command, data, merged: None), login_timeout=0.5)

    with pytest.raises(ConsoleProtocolError):
        host.connect()


def test_a_failed_probe_status_fails_negotiation():
    host = _host(_Terminal(lambda command, data, merged: (b"", b"", 2)))

    with pytest.raises(ConsoleProtocolError, match="status 2"):
        host.connect()


def test_a_login_timeout_redacts_the_secret_from_the_transcript():
    profile = PosixConsoleProfile(
        login=[LoginStep("never: ", "hunter2", secret=True)],
        login_timeout=0.3,
    )

    class Echoing(_Terminal):
        def _line(self, line):
            self.pending += line.encode() + b"\r\n"

    terminal = Echoing()
    stream = SerialHost(
        SerialConfig("pty", profile=profile, serial_port=terminal)
    ).executor.open()
    terminal.pending += b"hunter2\r\n"
    try:
        with pytest.raises(TimeoutError) as caught:
            profile.negotiate(stream)
    finally:
        stream.close()

    assert b"hunter2" not in caught.value.output


def test_the_profile_advertises_run_and_sends_lines_for_a_session():
    terminal = _Terminal()
    host = _host(terminal)

    assert "run" in host.capabilities
    with host.spawn() as session:
        session.send("ls")
    assert terminal.writes[-1] == b"ls\n"


# -- a real shell over pipes (Linux; Git Bash on Windows) -------------------------

_TOOLS = ("sh", "base64", "mktemp", "cat", "rm")
_HAS_SH = all(shutil.which(tool) for tool in _TOOLS)


class _PipeProcess:
    """`sh -s` over pipes: a real shell parsing the typed lines, no terminal."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [shutil.which("sh"), "-s"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.returncode = None

    def read(self, size=-1):
        data = os.read(self.proc.stdout.fileno(), 65536)
        if not data:
            self.returncode = 0
        return data

    def write(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def close(self):
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.proc.stdin.close()


@pytest.fixture
def piped_shell():
    if not _HAS_SH:
        pytest.skip(f"needs {', '.join(_TOOLS)} on PATH")
    process = _PipeProcess()
    stream = console_stream(process)
    profile = PosixConsoleProfile(wakeup=b"")
    profile.negotiate(stream)
    yield profile, stream
    process.close()


def test_real_shell_over_pipes_runs_the_protocol(piped_shell, tmp_path):
    profile, stream = piped_shell

    assert profile.execute(stream, "echo out; echo err >&2", timeout=20) == (
        b"out\n",
        b"err\n",
        0,
    )
    for status in (0, 3, 255):
        assert profile.execute(stream, f"(exit {status})", timeout=20)[2] == status
    assert profile.execute(stream, "no-such-command-hostctl", timeout=20)[2] == 127
    assert profile.execute(stream, "cat", timeout=20) == (b"", b"", 0)
    payload = bytes(range(256)) * 300
    assert profile.execute(stream, "cat", input=payload, timeout=60)[0] == payload

    marker = (tmp_path / "injected").as_posix()
    hostile = f"x\x03touch {marker}\n\ttab !! $(touch {marker}) end\n".encode()
    assert profile.execute(stream, "cat", input=hostile, timeout=20)[0] == hostile
    assert not os.path.exists(marker)
    assert profile.run(stream, "echo a; echo b >&2", timeout=20) == (b"a\nb\n", 0)


# -- a real shell in a real pty (POSIX) ------------------------------------------

_PTY_TOOLS = ("base64", "mktemp", "stty", "cat", "rm")
_HAS_PTY = sys.platform != "win32" and all(shutil.which(tool) for tool in _PTY_TOOLS)

_SHELLS = {
    "sh": ["sh", "-i"],
    "bash": ["bash", "--norc", "--noprofile", "-i"],
    "zsh": ["zsh", "-f", "-i"],
    "busybox": ["busybox", "sh", "-i"],
}


class _PtyShell:
    """An interactive shell on the slave side of a real pty."""

    def __init__(self, argv):
        import pty

        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # pragma: no cover - child
            os.environ["PS1"] = "root@device:~# "
            os.environ.pop("ENV", None)
            os.execvp(argv[0], argv)
        self.returncode = None

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, 9)
        except OSError:
            pass
        os.waitpid(self.pid, 0)


class _PtyStream:
    """A `ConsoleStream` over the pty master: select-bounded reads."""

    def __init__(self, fd):
        self.fd = fd

    def read(self, size=-1, *, timeout=None):
        import select

        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return b""
        try:
            data = os.read(self.fd, size if size > 0 else 65536)
        except OSError:
            data = b""
        if not data:
            raise ConnectionError("pty closed")
        return data

    def write(self, data):
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view) :]


class _PtyPort(_PtyStream):
    """A pyserial-shaped port over the pty master: `read` honours `.timeout`."""

    is_open = True
    dtr = rts = False
    write_timeout = 10

    def __init__(self, fd):
        super().__init__(fd)
        self.timeout = 0.1

    def read(self, size=1):  # type: ignore[override]
        try:
            return super().read(size, timeout=self.timeout)
        except ConnectionError:
            return b""

    def write(self, data):  # type: ignore[override]
        super().write(bytes(data))
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def send_break(self, duration=0.25):
        pass


class _BlockingPty:
    """A process whose `read` blocks, as a QEMU console or container TTY does."""

    def __init__(self, fd):
        self.fd = fd
        self.returncode = None

    def read(self, size=-1):
        try:
            return os.read(self.fd, size if size > 0 else 65536)
        except OSError:
            return b""

    def write(self, data):
        os.write(self.fd, data)


def _available_shells():
    names = [name for name, argv in _SHELLS.items() if shutil.which(argv[0])]
    return names or ["sh"]


@pytest.fixture(params=_available_shells())
def pty_shell(request):
    if not _HAS_PTY:
        pytest.skip(f"needs a POSIX pty and {', '.join(_PTY_TOOLS)}")
    shell = _PtyShell(_SHELLS[request.param])
    yield shell
    shell.close()


def _negotiated(stream):
    profile = PosixConsoleProfile()
    profile.negotiate(stream)
    return profile


@pytest.mark.skipif(not _HAS_PTY, reason="needs a POSIX pty and base64/mktemp/stty")
def test_real_pty_echo_is_never_output_and_status_is_exact(pty_shell):
    stream = _PtyStream(pty_shell.fd)
    profile = _negotiated(stream)

    assert profile.execute(stream, "echo hi", timeout=20) == (b"hi\n", b"", 0)
    assert profile.execute(stream, "printf abc", timeout=20)[0] == b"abc"
    assert profile.execute(stream, "printf '__ST%s\\r\\n' x", timeout=20)[0] == (
        b"__STx\r\n"
    )
    assert profile.execute(stream, "echo out; echo err >&2", timeout=20)[:2] == (
        b"out\n",
        b"err\n",
    )
    for status in (0, 3, 255):
        assert profile.execute(stream, f"(exit {status})", timeout=20)[2] == status
    assert profile.execute(stream, "no-such-command-hostctl", timeout=20)[2] == 127


@pytest.mark.skipif(not _HAS_PTY, reason="needs a POSIX pty and base64/mktemp/stty")
def test_real_pty_input_never_hangs_and_is_never_typed(pty_shell, tmp_path):
    stream = _PtyStream(pty_shell.fd)
    profile = _negotiated(stream)
    marker = tmp_path / "injected"

    started = time.monotonic()
    assert profile.execute(stream, "cat", timeout=20) == (b"", b"", 0)
    assert profile.execute(stream, 'read x; echo "[$x]"', timeout=20)[0] == b"[]\n"
    assert time.monotonic() - started < 10

    hostile = f"x\x03touch {marker}\n\ttab !! end\x04\n".encode()
    assert profile.execute(stream, "cat", input=hostile, timeout=20)[0] == hostile
    split = profile.execute(stream, "cat; echo e >&2", input=b"in", timeout=20)
    assert split == (b"in", b"e\n", 0)
    time.sleep(0.5)
    assert not marker.exists(), "input reached the shell as a command"


@pytest.mark.skipif(not _HAS_PTY, reason="needs a POSIX pty and base64/mktemp/stty")
def test_real_pty_timeout_leaves_nothing_to_execute(pty_shell, tmp_path):
    stream = _PtyStream(pty_shell.fd)
    profile = _negotiated(stream)
    marker = tmp_path / "late"

    with pytest.raises(TimeoutError):
        profile.execute(stream, f"sleep 2; touch {marker}", timeout=0.5)
    time.sleep(2.5)

    assert not marker.exists(), "the rest of the line ran after the interrupt"
    assert profile.execute(stream, "echo ok", timeout=20) == (b"ok\n", b"", 0)


@pytest.mark.skipif(not _HAS_PTY, reason="needs a POSIX pty and base64/mktemp/stty")
def test_real_pty_is_binary_safe_and_restores_the_terminal(pty_shell):
    import termios

    stream = _PtyStream(pty_shell.fd)
    profile = _negotiated(stream)
    payload = bytes(range(256)) * 64

    assert profile.execute(stream, "cat", input=payload, timeout=60)[0] == payload
    assert profile.execute(stream, "printf 'a\\r\\nb\\n'", timeout=20)[0] == (
        b"a\r\nb\n"
    )
    assert termios.tcgetattr(pty_shell.fd)[1] & termios.OPOST


@pytest.mark.skipif(not _HAS_PTY, reason="needs a POSIX pty and base64/mktemp/stty")
def test_real_pty_through_serial_host(pty_shell):
    """The whole stack: `SerialHost` over a pyserial-shaped port on the pty."""
    host = SerialHost(
        SerialConfig(
            "pty",
            profile=PosixConsoleProfile(),
            serial_port=_PtyPort(pty_shell.fd),
        )
    )

    result = host.run("echo out; echo err >&2", timeout=20)
    assert (result.stdout, result.stderr, result.returncode) == (
        b"out\n",
        b"err\n",
        0,
    )
    assert host.run("cat", input=b"abc", timeout=20).stdout == b"abc"
    with pytest.raises(subprocess.CalledProcessError) as caught:
        host.run("exit_with() { return 4; }; exit_with", timeout=20)
    assert caught.value.returncode == 4
    with pytest.raises(subprocess.TimeoutExpired):
        host.run("sleep 5", timeout=0.5)
    assert host.run("echo again", timeout=20).stdout == b"again\n"


@pytest.mark.skipif(not _HAS_PTY, reason="needs a POSIX pty and base64/mktemp/stty")
def test_real_pty_through_console_stream_over_a_blocking_read(pty_shell):
    """How a QEMU console or a container TTY is driven: its read blocks."""
    stream = console_stream(_BlockingPty(pty_shell.fd))
    profile = _negotiated(stream)

    assert profile.execute(stream, "echo hi", timeout=20) == (b"hi\n", b"", 0)
    with pytest.raises(TimeoutError):
        profile.execute(stream, "sleep 5", timeout=0.5)
    assert profile.execute(stream, "echo ok", timeout=20) == (b"ok\n", b"", 0)
