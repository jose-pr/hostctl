"""`ConsoleExecutor`: `run()` over a POSIX shell's console, with no thread.

It puts `PosixConsoleProfile`'s framed protocol behind `StreamingExecutor`,
so a console reached any way gets the call surface every hostctl executor
has. What it must keep: a decline only before the command line is typed
(the host then tries its next provider), output streamed as it arrives, one
command per terminal, and a timeout that types ^C and reports the command
as possibly still running.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import threading
import time

import pytest

from hostctl import (
    ConsoleExecutor,
    ExecutorProvider,
    OperationNotStartedError,
    PosixConfig,
    PosixConsoleProfile,
    PosixHost,
    PromptConsoleProfile,
    console_stream,
)
from test_console_posix import (
    _HAS_PTY,
    _HAS_SH,
    _TOOLS,
    _PipeProcess,
    _PtyStream,
    _Terminal,
    pty_shell,
)


class _Console:
    """`_Terminal` (an emulated shell's terminal) as a `ConsoleStream`."""

    def __init__(self, terminal):
        self.terminal = terminal
        self.reads = 0
        self.fail_reads = False

    def read(self, size=-1, *, timeout=None):
        if self.fail_reads:
            raise ConnectionError("console closed")
        self.reads += 1
        return self.terminal.read(size if size > 0 else 4096)

    def write(self, data):
        self.terminal.write(data)


def _sh(command, data, merged):
    """Answers `/bin/sh -c '...'` the way the host renders a command."""
    text = command.decode()
    if "printf" in text and "boom" in text:
        return (b"", b"boom\n", 4)
    if text == "/bin/sh -c cat":
        return (data, b"", 0)
    if "sleep" in text:
        return None
    return (b"out:" + command, b"err:" + command, 0)


def _host(executor, *fallback):
    return PosixHost(
        PosixConfig("nas1"),
        executor_providers=(ExecutorProvider("console", executor), *fallback),
        path_providers=(),
    )


def _completed(command, *args, **options):
    return subprocess.CompletedProcess(command, 0, b"from-fallback", b"")


def test_run_captures_stdout_stderr_and_status():
    console = _Console(_Terminal(_sh))
    host = _host(ConsoleExecutor(lambda: console))

    result = host.run("echo hi", timeout=10)

    assert result.stdout == b"out:/bin/sh -c 'echo hi'"
    assert result.stderr == b"err:/bin/sh -c 'echo hi'"
    assert result.returncode == 0


def test_the_call_surface_is_the_hosts():
    console = _Console(_Terminal(_sh))
    host = _host(ConsoleExecutor(lambda: console))

    text = host.run("echo hi", text=True, timeout=10)
    merged = host.run("echo hi", stderr=subprocess.STDOUT, timeout=10)
    piped = host.run("cat", input=b"\x00\x03 data", timeout=10)
    with pytest.raises(subprocess.CalledProcessError) as failed:
        host.run("printf boom >&2; exit 4", timeout=10)

    assert text.stdout == "out:/bin/sh -c 'echo hi'"
    assert merged.stderr is None
    assert piped.stdout == b"\x00\x03 data"
    assert (failed.value.returncode, failed.value.stderr) == (4, b"boom\n")


def test_a_profile_without_acknowledgements_is_used_as_given():
    terminal = _Terminal(_sh)
    executor = ConsoleExecutor(
        lambda: _Console(terminal), profile=PosixConsoleProfile(batch=None)
    )
    payload = os.urandom(20000)

    assert _host(executor).run("cat", input=payload, timeout=30).stdout == payload
    assert not any("__AK" in line for line in terminal.lines)


def test_only_a_framed_posix_profile_is_accepted():
    with pytest.raises(TypeError, match="PosixConsoleProfile"):
        ConsoleExecutor(lambda: None, profile=PromptConsoleProfile(prompt="$ "))
    assert isinstance(ConsoleExecutor(lambda: None).profile, PosixConsoleProfile)


# -- the decline rule --------------------------------------------------------------


def test_a_stream_that_cannot_be_opened_declines_to_the_next_provider(caplog):
    def refused():
        raise ConnectionRefusedError("connection refused by wss://u:hunter2@nas")

    host = _host(ConsoleExecutor(refused), ExecutorProvider("ssh", _completed))

    with caplog.at_level(logging.WARNING, logger="hostctl"):
        assert host.run("true", timeout=10).stdout == b"from-fallback"

    assert "run() fell back from console to ssh" in caplog.text
    assert "console stream unavailable: ConnectionRefusedError" in caplog.text
    assert "hunter2" not in caplog.text


def test_a_write_failure_before_the_command_line_declines_and_types_ctrl_c():
    terminal = _Terminal(_sh)
    terminal.fail_writes = True
    abandoned = []

    class Recording(ConsoleExecutor):
        def _abandon(self, request, *, stream=None):
            abandoned.append(request.command)
            return super()._abandon(request, stream=stream)

    host = _host(
        Recording(lambda: _Console(terminal)), ExecutorProvider("ssh", _completed)
    )

    assert host.run("rm -rf /data", timeout=10).stdout == b"from-fallback"
    assert abandoned == ["/bin/sh -c 'rm -rf /data'"]


def test_a_failure_on_the_command_line_itself_is_not_a_decline():
    terminal = _Terminal(_sh)
    write = terminal.write

    def fail_on_the_command_line(data):
        if b"stty -opost" in data:
            raise OSError("link down")
        return write(data)

    terminal.write = fail_on_the_command_line
    fallback = []
    host = _host(
        ConsoleExecutor(lambda: _Console(terminal)),
        ExecutorProvider("ssh", lambda *a, **k: fallback.append(a)),
    )

    with pytest.raises(OSError, match="link down"):
        host.run("rm -rf /data", timeout=10)
    assert fallback == []


def test_a_stream_lost_after_the_command_line_is_a_connection_error(monkeypatch):
    console = _Console(_Terminal(lambda command, data, merged: None))
    fallback = []
    host = _host(
        ConsoleExecutor(lambda: console),
        ExecutorProvider("ssh", lambda *a, **k: fallback.append(a)),
    )
    executor = host.executor_providers[0].executor
    prepare = PosixConsoleProfile._prepare

    def prepare_then_drop(self, stream, *args, **kwargs):
        result = prepare(self, stream, *args, **kwargs)
        console.fail_reads = True
        return result

    monkeypatch.setattr(PosixConsoleProfile, "_prepare", prepare_then_drop)
    with pytest.raises(ConnectionError) as lost:
        host.run("rm -rf /data", timeout=10)

    assert not isinstance(lost.value, OperationNotStartedError)
    assert fallback == []
    assert executor._running is None


# -- timeouts and the one terminal ---------------------------------------------------


def test_a_timeout_types_ctrl_c_and_reports_the_command_orphaned():
    terminal = _Terminal(_sh)
    host = _host(ConsoleExecutor(lambda: _Console(terminal)))

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        host.run("sleep 100", timeout=0.5)

    assert caught.value.orphaned is True
    assert terminal.writes[-1] == b"\x03"
    assert host.run("echo next", timeout=10).returncode == 0


def test_a_timeout_before_the_command_line_ran_nothing():
    """Acknowledged batches the shell never answers: the command line is
    never typed, so the timeout reports nothing left running."""
    terminal = _Terminal(_sh)
    typed = []
    terminal._line = typed.append
    host = _host(ConsoleExecutor(lambda: _Console(terminal)))

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        host.run("cat", input=os.urandom(20000), timeout=0.5)

    assert caught.value.orphaned is False
    assert typed and not any("stty -opost" in line for line in typed)


def test_an_override_that_drops_the_session_reports_the_command_ended():
    terminal = _Terminal(_sh)
    closed = []

    class Session(ConsoleExecutor):
        def _abandon(self, request, *, stream=None):
            super()._abandon(request, stream=stream)
            closed.append(stream)
            return False

    console = _Console(terminal)
    host = _host(Session(lambda: console))

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        host.run("sleep 100", timeout=0.5)

    assert caught.value.orphaned is False
    assert closed == [console]


class _Dying(_Console):
    """A console whose link drops once the command line is typed."""

    def __init__(self, terminal):
        super().__init__(terminal)
        self.dead = False

    def write(self, data):
        if self.dead:
            raise ConnectionResetError("link down")
        super().write(data)
        if b"stty -opost" in data:
            self.dead = True


def _dropping(dropped):
    class Dropping(ConsoleExecutor):
        def _abandon(self, request, *, stream=None):
            super()._abandon(request, stream=stream)
            dropped.append(stream)
            return False

    return Dropping


def test_the_override_drops_the_stream_after_a_failure_on_a_dead_stream():
    dying = _Dying(_Terminal(lambda command, data, merged: None))
    dropped = []
    executor = _dropping(dropped)(lambda: dying)
    host = _host(executor)

    def lose_the_link(size=-1, *, timeout=None):
        if dying.dead:
            raise ConnectionError("console closed")
        return _Console.read(dying, size, timeout=timeout)

    dying.read = lose_the_link
    with pytest.raises(ConnectionError, match="console closed"):
        host.run("sleep 100", timeout=10)

    assert dropped == [dying]
    assert executor._running is None


def test_a_timeout_on_a_dead_stream_is_still_a_timeout():
    dying = _Dying(_Terminal(lambda command, data, merged: None))
    dropped = []
    host = _host(_dropping(dropped)(lambda: dying))

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        host.run("sleep 100", timeout=0.5)

    assert caught.value.orphaned is False
    assert dropped == [dying]


def test_the_base_ctrl_c_on_a_dead_stream_reports_the_command_orphaned():
    dying = _Dying(_Terminal(lambda command, data, merged: None))
    executor = ConsoleExecutor(lambda: dying)
    host = _host(executor)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        host.run("sleep 100", timeout=0.5)

    assert caught.value.orphaned is True
    assert executor._terminal.acquire(blocking=False), "the terminal was kept"


def test_one_command_holds_the_terminal_and_a_waiter_keeps_its_timeout():
    release = threading.Event()

    def slow(command, data, merged):
        if b"slow" in command:
            release.wait(10)
        return (b"done", b"", 0)

    terminal = _Terminal(slow)
    executor = ConsoleExecutor(lambda: _Console(terminal))
    host = _host(executor)
    first = threading.Thread(target=lambda: host.run("slow", timeout=20))
    first.start()
    while not any(b"stty -opost" in write for write in terminal.writes):
        time.sleep(0.01)
    typed_before = len(terminal.writes)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        host.run("second", timeout=0.3)

    assert caught.value.orphaned is False
    assert len(terminal.writes) == typed_before, "the waiter typed into the console"
    release.set()
    first.join(10)
    assert host.run("third", timeout=10).stdout == b"done"


# -- live output, against real shells -------------------------------------------------


class _Timed(io.RawIOBase):
    """A stdout target recording when each piece arrived."""

    def __init__(self):
        self.pieces = []

    def writable(self):
        return True

    def write(self, data):
        self.pieces.append((time.monotonic(), bytes(data)))
        return len(data)


def _streams_live(stream_factory):
    host = _host(ConsoleExecutor(stream_factory))
    sink = _Timed()

    host.run("echo first; sleep 1; echo second", stdout=sink, timeout=30)
    ended = time.monotonic()

    assert b"".join(piece for _, piece in sink.pieces) == b"first\nsecond\n"
    first_at = next(at for at, piece in sink.pieces if b"first" in piece)
    assert ended - first_at > 0.5, "stdout arrived only when the command ended"


@pytest.mark.skipif(not _HAS_SH, reason=f"needs {', '.join(_TOOLS)} on PATH")
def test_a_real_shell_over_pipes_streams_stdout_as_it_arrives():
    process = _PipeProcess()
    try:
        stream = console_stream(process)
        PosixConsoleProfile(wakeup=b"").negotiate(stream)
        _streams_live(lambda: stream)
        host = _host(ConsoleExecutor(lambda: stream))
        assert host.run("cat", input=b"abc", timeout=30).stdout == b"abc"
        with pytest.raises(subprocess.CalledProcessError) as failed:
            host.run("echo e >&2; exit 3", timeout=30)
        assert (failed.value.returncode, failed.value.stderr) == (3, b"e\n")
    finally:
        process.close()


@pytest.mark.skipif(not _HAS_PTY, reason="needs a POSIX pty and base64/mktemp/stty")
def test_a_real_pty_streams_and_a_timeout_leaves_it_usable(pty_shell):
    stream = _PtyStream(pty_shell.fd)
    PosixConsoleProfile().negotiate(stream)
    _streams_live(lambda: stream)
    host = _host(ConsoleExecutor(lambda: stream))

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        host.run("echo partial; sleep 5", timeout=1)

    assert caught.value.orphaned is True
    assert caught.value.output == b"partial\n"
    assert host.run("echo again", timeout=20).stdout == b"again\n"
