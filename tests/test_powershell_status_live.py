"""Run rendered PowerShell scripts through a real PowerShell and check status.

`; exit $LASTEXITCODE` reports a variable only *native* commands set, so a
pure-cmdlet script that failed exited 0 and `check=True` never raised, while a
stale native code was reported after a later cmdlet succeeded. Nothing but an
actual PowerShell can settle what the epilogue does -- every unit test here
asserts the text of the script, which was exactly the text that was wrong.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from hostctl.shell import POWERSHELL, PWSH

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Windows PowerShell is the subject"
)

_MISSING = r"C:\hostctl-definitely-missing"


def _shells():
    for executable, flavour in (("powershell.exe", POWERSHELL), ("pwsh.exe", PWSH)):
        if shutil.which(executable):
            yield pytest.param((executable, flavour), id=executable)


def _status(shell, *cmds):
    executable, flavour = shell
    script = flavour.script(cmds)
    invocation = flavour.invocation(script, executable=executable)
    completed = subprocess.run(
        list(invocation), capture_output=True, text=True, check=False
    )
    return completed.returncode


@pytest.mark.parametrize("shell", list(_shells()))
def test_a_failing_cmdlet_is_not_reported_as_success(shell):
    """The deploy case: a missing artefact read as present."""
    assert _status(shell, ("Get-Item", "-LiteralPath", _MISSING)) != 0


@pytest.mark.parametrize("shell", list(_shells()))
def test_a_stale_native_code_is_not_reported_after_a_later_success(shell):
    assert (
        _status(
            shell,
            "cmd /c exit 3",
            ("Write-Output", "ok"),
        )
        == 0
    )


@pytest.mark.parametrize("shell", list(_shells()))
def test_a_failing_native_command_still_reports_its_own_code(shell):
    assert _status(shell, "cmd /c exit 3") == 3


@pytest.mark.parametrize("shell", list(_shells()))
def test_success_is_still_zero(shell):
    assert _status(shell, ("Write-Output", "ok")) == 0


@pytest.mark.parametrize("shell", list(_shells()))
def test_object_output_survives_the_epilogue(shell):
    """`exit` discarded objects still waiting to be formatted, so
    `[pscustomobject]@{a=1}` printed nothing with status 0. pwsh colours the
    table header for a colour TERM even into a pipe unless told not to."""
    executable, flavour = shell
    script = flavour.script(["[pscustomobject]@{a=1}"])
    completed = subprocess.run(
        list(flavour.invocation(script, executable=executable)),
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, TERM="xterm-256color"),
    )

    assert completed.returncode == 0
    assert completed.stdout.split() == ["a", "-", "1"]


@pytest.mark.parametrize("shell", list(_shells()))
def test_an_error_record_is_plain_text(shell):
    executable, flavour = shell
    script = flavour.script([("Get-Item", "-LiteralPath", _MISSING)])
    completed = subprocess.run(
        list(flavour.invocation(script, executable=executable)),
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, TERM="xterm-256color"),
    )

    assert completed.returncode != 0
    assert _MISSING in completed.stderr
    assert "\x1b" not in completed.stderr


@pytest.mark.parametrize("shell", list(_shells()))
def test_a_user_exit_before_the_epilogue_keeps_its_code(shell):
    assert _status(shell, "exit 5") == 5


@pytest.mark.parametrize("shell", list(_shells()))
def test_a_trailing_comment_cannot_swallow_the_epilogue(shell):
    """The epilogue was appended after `;` on the same line, so a script
    ending in a comment commented the status reporting out."""
    assert _status(shell, "Get-Item -LiteralPath " + _MISSING + " # check") != 0
