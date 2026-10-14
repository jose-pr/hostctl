"""The CLI's console path: operands, help, and an idle line."""

from __future__ import annotations

import contextlib
import io
import threading

import duho
import pytest

from hostctl.cli import Hostctl, main


class _IdleSession:
    """A console that says nothing for a while and then speaks.

    A serial read answers `b""` whenever its read timeout expires, which is
    not EOF -- the line is simply idle.
    """

    returncode = None

    def __init__(self):
        self.reads = [b"", b"", b"banner\r\n", b""]
        self.sent = []
        #: Set once the banner has actually been written out, so the test
        #: measures the pump rather than racing it.
        self.spoke = threading.Event()

    def read(self, size=-1):
        if self.reads:
            value = self.reads.pop(0)
            if value:
                self.spoke.set()
            elif not self.reads:
                self.returncode = 0
            return value
        self.returncode = 0
        return b""

    def send(self, line):
        self.sent.append(line)

    def send_eof(self):
        self.spoke.wait(5)

    def close(self):
        self.returncode = 0


class _Shell:
    def __init__(self, session, *, terminal_supported):
        self.session_object = session
        self.terminal_supported = terminal_supported
        self.terminal_requests = []

    def session(self, **options):
        self.terminal_requests.append(options.get("terminal"))
        if options.get("terminal") and not self.terminal_supported:
            raise NotImplementedError("serial connections cannot allocate a PTY")
        return self.session_object


class _Host:
    def __init__(self, shell):
        self.shell = shell

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_shell(monkeypatch, capsysbinary, shell, stdin_text="hello\n"):
    monkeypatch.setattr(
        "hostctl.cli._common.Host", lambda uri, **credentials: _Host(shell)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    command = duho.parse(Hostctl, ["shell", "serial:///dev/ttyUSB0"])
    return command(), capsysbinary.readouterr().out


def test_shell_opens_a_console_that_cannot_allocate_a_terminal(
    monkeypatch, capsysbinary
):
    """A URI-built serial host is raw, so asking only for a terminal could
    never open a session -- the one verb a console most needs."""
    shell = _Shell(_IdleSession(), terminal_supported=False)

    status, output = _run_shell(monkeypatch, capsysbinary, shell)

    assert status == 0
    assert shell.terminal_requests == [True, None]
    assert b"banner" in output


def test_a_terminal_capable_host_still_gets_one(monkeypatch, capsysbinary):
    shell = _Shell(_IdleSession(), terminal_supported=True)

    status, _ = _run_shell(monkeypatch, capsysbinary, shell)

    assert status == 0
    assert shell.terminal_requests == [True]


def test_an_opaque_uri_is_a_legal_path_operand():
    """`local:/tmp/x` is the spelling every other subcommand documents."""
    command = duho.parse(Hostctl, ["cp", "local:/tmp/x", "y"])
    with contextlib.ExitStack() as stack:
        path = command.path_operand(stack, "local:/tmp/x")
        assert str(path).replace("\\", "/").endswith("/tmp/x")


def test_a_host_with_no_path_names_the_spelling():
    command = duho.parse(Hostctl, ["cp", "ssh://host:2222/srv", "y"])
    with contextlib.ExitStack() as stack:
        with pytest.raises(ValueError, match="URI:PATH"):
            command.path_operand(stack, "ssh://host:2222/srv")


def test_the_help_documents_the_grammar_and_the_exit_codes(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out

    assert "URI:PATH" in text
    assert "HOSTCTL_PASSWORD" in text
    assert "126" in text and "127" in text
    assert "serial:///dev/ttyUSB0" in text
