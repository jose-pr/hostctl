"""Rendering rules that only show up against a real parser."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from hostctl.executor._common import ExecutorCapability, normalize_input
from hostctl.shell import CMD, POSIX_SHELL, ZSH, PowerShellFlavour, Shell


class _Recorder:
    """An executor that records what it was handed.

    It declares the native `env` capability, because a shell with no such
    executor embeds the assignments in the script instead of forwarding
    them -- which is the path where an empty mapping is harmless.
    """

    executor_capabilities = frozenset({ExecutorCapability.ENV})

    def __init__(self):
        self.calls = []

    def __call__(self, command, *args, env=None, **options):
        options["env"] = env
        self.calls.append((command, args, options))
        return subprocess.CompletedProcess([command, *args], 0, "", "")


class _Process:
    """A process that records what a session writes into it."""

    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    returncode = None


@pytest.mark.parametrize(
    ("script", "expected"),
    (
        ("echo hi", "echo hi;\n"),
        ("echo hi;", "echo hi;\n"),
        ("echo hi ;   ", "echo hi ;\n"),
        ("sleep 5 &", "sleep 5 &\n"),
        ("a | b", "a | b;\n"),
        ("a |", "a |\n"),
        ("a &&", "a &&\n"),
    ),
)
def test_send_does_not_append_a_separator_the_text_already_has(script, expected):
    """`send()` appended `;` unconditionally.

    Ordinary shell text that already ended in a terminator became a syntax
    error: `echo hi;;`, and `sleep 5 &;` is not a command in any POSIX
    shell.
    """
    from hostctl.shell._common import ShellSession

    process = _Process()
    session = ShellSession(POSIX_SHELL, process)
    session.send(script)

    assert process.written == [expected]


def test_zsh_quotes_a_leading_equals():
    """zsh expands `=word` to the path of `word`; sh does not.

    `shlex.quote` implements sh's rules, so `=report.txt` went through bare
    and zsh replaced it with a program path or failed outright.
    """
    assert ZSH.quote("=report.txt") == "'=report.txt'"
    assert POSIX_SHELL.quote("=report.txt") == "=report.txt"
    # Everything sh already quoted is untouched.
    assert ZSH.quote("a b") == POSIX_SHELL.quote("a b")


def test_powershell_7_is_named_pwsh_even_with_an_explicit_executable():
    """Identity comes from the version; the executable only moves the path."""
    flavour = PowerShellFlavour(7, executable="/opt/microsoft/powershell/7/pwsh")
    assert flavour.name == "pwsh"
    assert flavour.default_executable == "/opt/microsoft/powershell/7/pwsh"

    assert PowerShellFlavour(5).name == "powershell"
    assert PowerShellFlavour(7).default_executable == "pwsh"


def test_an_empty_env_is_not_an_empty_environment():
    """`env={}` means "no overrides", not "start the child with nothing".

    Forwarded to `subprocess` as `env={}`, it produced a child with no
    `PATH`, no `HOME` and no `SystemRoot`.
    """
    executor = _Recorder()
    Shell(POSIX_SHELL, executor, env={}).run("echo hi")

    assert executor.calls[0][2].get("env") is None


@pytest.mark.parametrize(
    "value",
    (bytearray(b"payload"), memoryview(b"payload")),
)
def test_normalize_input_covers_the_buffer_protocol(value):
    """The deadlock this function prevents, for the types it skipped.

    A `bytearray` reached a text-mode writer thread untouched and raised
    there -- the thread died without closing the pipe, the child never saw
    EOF, and `timeout=` could not fire because nothing was waiting.
    """
    assert normalize_input(value, text_mode=True) == "payload"
    assert normalize_input(value, text_mode=False) == b"payload"
    assert isinstance(normalize_input(value, text_mode=False), bytes)


@pytest.mark.skipif(sys.platform != "win32", reason="requires a real cmd.exe")
def test_a_cmd_executable_with_a_space_is_launchable(tmp_path):
    """The program slot is read by CreateProcess, not by cmd.

    Rendered with cmd's own caret quoting, `^"C:\\Program Files\\...^"` made
    Windows look for a program named `^` -- measured as
    `FileNotFoundError: [WinError 2]`.
    """
    directory = tmp_path / "with a space"
    directory.mkdir()
    spaced = directory / "my cmd.exe"
    shutil.copy(os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"), spaced)

    rendered = CMD.command(("echo ok",), executable=str(spaced)).command
    assert rendered.startswith(f'"{spaced}"')

    completed = subprocess.run(rendered, capture_output=True, text=True)
    assert completed.returncode == 0
    assert completed.stdout.strip() == "ok"


def test_a_cmd_executable_containing_a_quote_is_refused():
    with pytest.raises(ValueError, match="quote"):
        CMD.command(("echo ok",), executable='c:\\a"b\\cmd.exe')


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        (
            ("Remove-Item", "-LiteralPath", r"C:\tmp\x.txt"),
            r"& 'Remove-Item' -LiteralPath 'C:\tmp\x.txt'",
        ),
        (("prog", "-Path:", "v"), "& 'prog' -Path: 'v'"),
        # Not a parameter name: it needs quoting, so it gets it.
        (("prog", "-not a name"), "& 'prog' '-not a name'"),
        # The first element is the command, never a parameter name.
        (("-Weird",), "& '-Weird'"),
    ),
)
def test_powershell_binds_a_named_parameter(command, expected):
    """A quoted string is not a parameter name to PowerShell's binder.

    Measured against a real powershell.exe: `Remove-Item -LiteralPath`
    rendered as three string literals returned 1, reported "A positional
    parameter cannot be found", and left the file in place. Rendered with
    the name unquoted it returns 0 and the file is gone.
    """
    from hostctl.shell import PowerShellFlavour

    assert PowerShellFlavour(7).structured_command(command) == expected


def test_raw_shell_source_may_span_lines():
    """Raw text is shell source, and a newline there is ordinary syntax.

    Rejecting every control character in raw source made a heredoc, an
    `if`/`for` block and a literal tab unexpressible.
    """
    script = "if [ -d /srv/app ]; then\n  make deploy\nfi"
    assert POSIX_SHELL.script((script,)) == script
    assert POSIX_SHELL.script(("cut -d'\t' -f2 data.tsv",)) == "cut -d'\t' -f2 data.tsv"

    with pytest.raises(ValueError, match="control characters"):
        POSIX_SHELL.script(("echo \x00",))
    with pytest.raises(ValueError, match="control characters"):
        POSIX_SHELL.quote("a\nb")


@pytest.mark.skipif(sys.platform != "win32", reason="requires a real powershell.exe")
@pytest.mark.parametrize(
    "value",
    ("", 'q"x', 'a b" c d', "trail sp\\", "a & b", "$env:PATH", "'quoted'"),
)
@pytest.mark.parametrize("version", (5, 7))
def test_a_powershell_structured_argument_reaches_a_real_program(value, version):
    """Every PowerShell assertion in the suite was on rendered text.

    The ones that ran used `Write-Output` -- a cmdlet, whose binder never
    sees the native-command re-quoting PowerShell 5 applies. This sends the
    argument to a real native program and asks the child what it got.
    """
    from hostctl.shell import PowerShellFlavour

    flavour = PowerShellFlavour(version)
    if shutil.which(flavour.default_executable) is None:
        pytest.skip(f"{flavour.default_executable} is unavailable")
    code = "import sys; print(ascii(sys.argv[1]))"
    rendered = flavour.command(((sys.executable, "-c", code, value),))

    completed = subprocess.run(
        rendered.command, capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr[:400]
    assert completed.stdout.strip() == ascii(value)
