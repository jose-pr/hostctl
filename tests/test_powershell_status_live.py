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

from hostctl.shell import POWERSHELL

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Windows PowerShell is the subject"
)

_MISSING = r"C:\hostctl-definitely-missing"


def _shells():
    for executable in ("powershell.exe", "pwsh.exe"):
        if shutil.which(executable):
            yield executable


def _status(executable, *cmds):
    script = POWERSHELL.script(cmds)
    invocation = POWERSHELL.invocation(script, executable=executable)
    completed = subprocess.run(
        list(invocation), capture_output=True, text=True, check=False
    )
    return completed.returncode


@pytest.mark.parametrize("executable", list(_shells()))
def test_a_failing_cmdlet_is_not_reported_as_success(executable):
    """The deploy case: a missing artefact read as present."""
    assert _status(executable, ("Get-Item", "-LiteralPath", _MISSING)) != 0


@pytest.mark.parametrize("executable", list(_shells()))
def test_a_stale_native_code_is_not_reported_after_a_later_success(executable):
    assert (
        _status(
            executable,
            "cmd /c exit 3",
            ("Write-Output", "ok"),
        )
        == 0
    )


@pytest.mark.parametrize("executable", list(_shells()))
def test_a_failing_native_command_still_reports_its_own_code(executable):
    assert _status(executable, "cmd /c exit 3") == 3


@pytest.mark.parametrize("executable", list(_shells()))
def test_success_is_still_zero(executable):
    assert _status(executable, ("Write-Output", "ok")) == 0


@pytest.mark.parametrize("executable", list(_shells()))
def test_a_trailing_comment_cannot_swallow_the_epilogue(executable):
    """The epilogue was appended after `;` on the same line, so a script
    ending in a comment commented the status reporting out."""
    assert _status(executable, "Get-Item -LiteralPath " + _MISSING + " # check") != 0
