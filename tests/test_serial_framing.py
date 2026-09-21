"""The framing rule a prompt console runs by.

One framed exchange owns a window: it starts after the command this profile
echoed back and ends at the first prompt following the completion marker.
Nothing before that window can terminate, abort or forge the exchange, and
nothing inside it is ever dropped silently.

Every test here failed against the code before that rule existed -- each one
is a way a real device made `run()` return the wrong bytes, the wrong status,
or someone else's output, with returncode 0.
"""

from __future__ import annotations

import pytest

from hostctl import (
    ConsoleProtocolError,
    LoginStep,
    PromptConsoleProfile,
    SerialConfig,
    SerialHost,
)


class _Console:
    """A device that answers a written line with scripted bytes.

    Deliberately chunked: a real UART delivers a reply in pieces, and the
    whole-buffer searches this suite exists to pin only failed when it did.
    """

    is_open = True
    dtr = False
    rts = False

    def __init__(self, replies=None, *, chunk=7):
        self.replies = dict(replies or {})
        # An empty line is the wakeup `negotiate()` sends; answering it with
        # the prompt is what a real device does.
        self.replies.setdefault(b"", b"> ")
        self.writes = []
        self.pending = bytearray()
        self.chunk = chunk
        self.timeout = 0.1

    def read(self, size=1):
        if not self.pending:
            return b""
        take = min(size, self.chunk, len(self.pending))
        value = bytes(self.pending[:take])
        del self.pending[:take]
        return value

    def write(self, data):
        self.writes.append(data)
        line = data.rstrip(b"\r\n")
        if line in self.replies:
            self.pending.extend(self.replies[line])
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.is_open = False

    def reset_input_buffer(self):
        self.pending.clear()


def _host(console, **options):
    options.setdefault("status_marker", rb"STATUS(\d+)")
    options.setdefault("reliable_status", True)
    options.setdefault("status_parser", lambda match: int(match.group(1)))
    profile = PromptConsoleProfile(rb"> ", **options)
    return SerialHost(SerialConfig("loop://", protocol=profile, serial_port=console))


def test_a_prompt_in_the_output_does_not_end_the_read():
    """`peer-> down` inside `show ip bgp summary` ended the read, so the
    command failed with "completion marker missing" while the rest of the
    real reply stayed queued to corrupt the next command."""
    console = _Console(
        {
            b"show ip bgp summary": (
                b"show ip bgp summary\r\n"
                b"peer-> down\r\n"
                b"peer2 up\r\n"
                b"STATUS0\r\n> "
            )
        }
    )

    result = _host(console).run("show ip bgp summary", check=False, timeout=5)

    assert b"peer-> down" in result.stdout
    assert b"peer2 up" in result.stdout
    assert result.returncode == 0


def test_a_marker_in_the_output_does_not_forge_the_status():
    """A banner containing `STATUS7` reported returncode 7 and truncated
    stdout to the bytes before it -- and with the default check=True, raised
    for a command the device completed successfully."""
    console = _Console(
        {
            b"show banner": (
                b"show banner\r\n"
                b"welcome: build STATUS7 is current\r\n"
                b"STATUS0\r\n> "
            )
        }
    )

    result = _host(console).run("show banner", check=False, timeout=5)

    assert result.returncode == 0
    assert b"build STATUS7 is current" in result.stdout


def test_a_login_pattern_in_the_output_does_not_abort_the_command():
    console = _Console(
        {
            # The login the profile negotiates, then the command itself.
            b"": b"login: ",
            b"root": b"> ",
            b"show users": b"show users\r\nlast login: root\r\nSTATUS0\r\n> ",
        }
    )
    host = _host(console, login=(LoginStep(rb"login: ", b"root"),))

    result = host.run("show users", check=False, timeout=5)

    assert b"last login: root" in result.stdout
    assert result.returncode == 0


def test_the_previous_exchange_is_not_returned_as_this_command_s_output():
    """Raw serial has no request/response correlation, so bytes left by an
    earlier exchange -- a timed-out run, or the reply to `paging_disable`
    that negotiate never read -- were returned as the next command's output
    with returncode 0."""
    console = _Console({b"show version": b"show version\r\nVERSION 9\r\nSTATUS0\r\n> "})
    host = _host(console)
    host.connect()
    # The device kept talking after the last exchange gave up on it.
    console.pending.extend(b"stale output from the last command\r\nSTATUS3\r\n> ")

    result = host.run("show version", check=False, timeout=5)

    assert b"stale" not in result.stdout
    assert b"VERSION 9" in result.stdout
    assert result.returncode == 0


def test_paged_output_keeps_every_page():
    """Paging deleted each page's CONTENT along with the `--More--` marker,
    so a paged command returned only its LAST page -- with returncode 0 and
    no indication that anything was dropped. Paging exists because the
    output is long, so the feature lost almost all of what it retrieves."""
    # Each continuation byte yields the next page.
    pages = [b"line-2\r\n--More--", b"line-3\r\nSTATUS0\r\n> "]

    class _Paged(_Console):
        def write(self, data):
            if data == b" ":
                self.writes.append(data)
                if pages:
                    self.pending.extend(pages.pop(0))
                return len(data)
            return super().write(data)

    console = _Paged(
        {b"show running-config": b"show running-config\r\nline-1\r\n--More--"}
    )
    host = _host(console, paging_prompt=rb"--More--", paging_continue=b" ")

    result = host.run("show running-config", check=False, timeout=5)

    assert b"line-1" in result.stdout
    assert b"line-2" in result.stdout
    assert b"line-3" in result.stdout
    assert result.returncode == 0


def test_output_past_max_buffer_is_an_error_not_a_silent_truncation():
    """The transcript was cut from the front mid-line and returned as if
    complete, with `error_patterns` then searched only against the surviving
    tail -- so a failure printed early no longer set a status."""
    body = b"x" * 5000
    console = _Console({b"show running-config": b"show running-config\r\n" + body})
    host = _host(console, max_buffer=1024)

    with pytest.raises(ConsoleProtocolError, match="max_buffer"):
        host.run("show running-config", check=False, timeout=5)


def test_a_command_timeout_is_enforced_across_a_blocking_read():
    """`SerialConfig(read_timeout=None)` is pyserial's "block forever", and
    `run(timeout=)` was only checked between reads, so such a host blocked
    inside the backend instead of raising."""
    import subprocess
    import time

    class _Blocking(_Console):
        def __init__(self):
            super().__init__({})
            self.waits = []
            self.negotiated = False

        def read(self, size=1):
            if self.pending:
                return super().read(size)
            # pyserial blocks for `self.timeout`; `None` means forever, which
            # is what `SerialConfig(read_timeout=None)` configures and what a
            # test cannot wait for -- record what it was clamped to instead.
            self.waits.append(self.timeout)
            if self.timeout is None:
                raise AssertionError("a read was left unbounded")
            time.sleep(min(self.timeout, 0.05))
            return b""

    console = _Blocking()
    host = _host(console)
    host.connect()
    console.timeout = None  # the port is configured to block forever

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        host.run("show version", timeout=0.5)

    assert time.monotonic() - started < 5
    assert all(wait is not None for wait in console.waits)
