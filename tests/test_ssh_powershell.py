"""PowerShell over SSH: how the script travels, and how its stderr comes back."""

from __future__ import annotations

import shlex
import shutil
import subprocess

import pytest
from pathapi import WindowsPathname

from hostctl import SshConfig
from hostctl.executor.ssh import _clixml_text
from hostctl.shell import CMD, POWERSHELL

ONE_LINE = (("Write-Output", "hi"),)
WITH_COMMENT = ("Write-Output hi # trailing",)
MULTI_LINE = ("if ($true) {\n  Write-Output hi\n}",)


def test_a_cmd_login_gets_command_for_a_one_line_body():
    line = POWERSHELL.command(ONE_LINE, login_shell="cmd").command
    assert "-Command" in line and "-EncodedCommand" not in line


@pytest.mark.parametrize("login", (None, "powershell"))
def test_other_logins_keep_the_encoded_fallback(login):
    line = POWERSHELL.command(ONE_LINE, login_shell=login).command
    assert "-EncodedCommand" in line


@pytest.mark.parametrize("body", (WITH_COMMENT, MULTI_LINE))
def test_a_comment_or_newline_cannot_cross_cmd_in_a_command_argument(body):
    line = POWERSHELL.command(body, login_shell="cmd").command
    assert "-EncodedCommand" in line


def test_a_free_stdin_carries_a_script_with_a_comment():
    rendered = POWERSHELL.command(WITH_COMMENT, login_shell="cmd", stdin_free=True)
    assert rendered.command.endswith("-Command -")
    assert "-EncodedCommand" not in rendered.command
    assert rendered.stdin.startswith("$ProgressPreference='SilentlyContinue';")
    assert "# trailing\n$host.SetShouldExit(" in rendered.stdin


def test_a_multi_line_body_never_uses_stdin():
    """PowerShell reads stdin line by line and drops an unterminated block."""
    rendered = POWERSHELL.command(MULTI_LINE, login_shell="cmd", stdin_free=True)
    assert rendered.stdin is None
    assert "-EncodedCommand" in rendered.command


def test_a_posix_login_gets_the_script_as_the_last_word():
    line = POWERSHELL.command(WITH_COMMENT, login_shell="posix").command
    words = shlex.split(line)
    assert words[-2] == "-Command"
    assert words[-1].endswith(
        "$host.SetShouldExit($(if ($?) { 0 } elseif ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }))"
    )


class _Result:
    returncode = 0
    stdout = b""
    stderr = b""


class _Process:
    def __init__(self):
        self.waits = []

    async def wait(self, check=False, timeout=None):
        return _Result()


class _Connection:
    def __init__(self):
        self.calls = []

    def is_closed(self):
        return False

    async def create_process(self, command, **kwargs):
        stdin = kwargs.get("stdin")
        self.calls.append((command, stdin.read() if stdin is not None else None))
        return _Process()


def _host():
    from hostctl import _async
    from hostctl.host import WindowsHost
    from hostctl.host._ssh import ssh_providers

    config = SshConfig("h", dialect=POWERSHELL, path_flavor=WindowsPathname)
    executor, paths = ssh_providers(config)
    connection = _Connection()
    executor.transport._ssh = connection
    executor.transport._loop_generation = _async.loop_generation()
    host = WindowsHost(
        config,
        executor_providers=(executor,),
        path_providers=(paths,),
        shell=POWERSHELL,
    )
    return host, connection


def test_a_run_without_input_sends_the_script_on_stdin():
    host, connection = _host()
    host.run("Write-Output hi # trailing", check=False)
    command, stdin = connection.calls[0]
    assert command.endswith("-Command -")
    assert b"# trailing" in stdin and b"SetShouldExit" in stdin


def test_a_run_with_its_own_input_keeps_stdin_for_the_caller():
    host, connection = _host()
    host.run("Write-Output hi # trailing", input="data", check=False)
    command, stdin = connection.calls[0]
    assert "-EncodedCommand" in command
    assert stdin == b"data"


def test_clixml_error_text_is_decoded():
    sample = (
        "#< CLIXML\r\n"
        '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
        '<Obj S="progress" RefId="0"><TN RefId="0"><T>System.Object</T></TN>'
        '<MS><I64 N="SourceId">1</I64></MS></Obj>'
        '<S S="Error">boom_x000D__x000A_</S></Objs>'
    )
    assert _clixml_text(sample) == "boom\r\n"
    assert _clixml_text(sample.encode()) == b"boom\r\n"
    assert _clixml_text("plain") == "plain"
    assert _clixml_text("#< CLIXML\r\n<broken") == "#< CLIXML\r\n<broken"
    assert _clixml_text(None) is None


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None or shutil.which("cmd.exe") is None,
    reason="needs Windows PowerShell and cmd.exe",
)
@pytest.mark.parametrize(
    "value",
    ("a b", "%OS%", "a&b", 'say "hi"', "c^d", "x|y", "it's", "$env:OS", "x\\", "!x"),
)
def test_a_command_argument_survives_a_cmd_login_shell(value):
    rendered = POWERSHELL.command(
        ("[Console]::Out.Write(" + POWERSHELL.quote(value) + ")",), login_shell="cmd"
    )
    completed = subprocess.run(
        f"cmd.exe /d /s /c {rendered.command}", capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr[:200]
    assert completed.stdout.decode() == value


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None or shutil.which("cmd.exe") is None,
    reason="needs Windows PowerShell and cmd.exe",
)
@pytest.mark.parametrize(
    "script, status",
    (("Write-Output ok", 0), ("Write-Error boom", 1), ("cmd.exe /c exit 7", 7)),
)
def test_the_status_and_plain_stderr_survive_the_stdin_route(script, status):
    rendered = POWERSHELL.command((script,), login_shell="powershell", stdin_free=True)
    completed = subprocess.run(
        f"cmd.exe /d /s /c {rendered.command}",
        input=rendered.stdin.encode(),
        capture_output=True,
        check=False,
    )
    assert completed.returncode == status
    assert not completed.stderr.startswith(b"#< CLIXML")
