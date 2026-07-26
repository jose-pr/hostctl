"""Windows PowerShell shell flavour."""

from __future__ import annotations

import os
import subprocess
import typing
from pathlib import PureWindowsPath

from ..executor import Environment, PathLike
from ._common import ShellCommand, ShellFlavour, ShellOperator, ShellToken


def _literal(value: object) -> str:
    return (
        "'"
        + str(value)
        .replace("'", "''")
        .replace("‘", "‘‘")
        .replace("’", "’’")
        .replace("‚", "‚‚")
        .replace("‛", "‛‛")
        + "'"
    )


class PowerShellFlavour(ShellFlavour):
    name = "powershell"
    default_executable = "powershell.exe"
    command_separator = ";"
    context_order = ("cwd", "env", "command")
    execution_epilogue = "; exit $LASTEXITCODE"
    structured_command_prefix = "& "
    path_flavor = PureWindowsPath
    info_script = (
        'Write-Output ("hostname=" + [Environment]::MachineName);'
        'Write-Output ("os_family=" + [Environment]::OSVersion.Platform);'
        'Write-Output ("os_name=" + '
        "[System.Runtime.InteropServices.RuntimeInformation]::OSDescription);"
        'Write-Output ("os_version=" + [Environment]::OSVersion.Version);'
        'Write-Output ("architecture=" + '
        "[System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture)"
    )

    def __init__(
        self,
        major_version: int = 5,
        executable: typing.Optional[str] = None,
    ) -> None:
        if major_version == 6 or major_version < 5:
            raise ValueError("supported PowerShell versions are 5 and 7+")
        self.major_version = major_version
        if executable is not None:
            self.default_executable = executable
        elif major_version >= 7:
            self.default_executable = "pwsh"
            self.name = "pwsh"

    def quote(self, value: object) -> str:
        return _literal(self._text(value))

    def operator(self, value: ShellOperator) -> str:
        if self.major_version >= 7 and value in (
            ShellOperator.AND,
            ShellOperator.OR,
        ):
            return {
                ShellOperator.AND: " && ",
                ShellOperator.OR: " || ",
            }[value]
        try:
            return {
                ShellOperator.PIPE: " | ",
                ShellOperator.REDIRECT: " > ",
                ShellOperator.APPEND: " >> ",
                ShellOperator.SEQUENCE: self.command_separator,
            }[value]
        except KeyError as exc:
            raise NotImplementedError(
                f"{value.name} is not portable to Windows PowerShell"
            ) from exc

    def environment_assignment(self, key: str, value: object) -> str:
        return f"$env:{key}={_literal(self._text(value))}"

    def change_directory(self, cwd: PathLike) -> str:
        return (
            f"Set-Location -LiteralPath {_literal(self._text(cwd))} -ErrorAction Stop"
        )

    def join_cwd(self, changed: str, command: str) -> str:
        # PowerShell 5 has no &&.  ErrorAction Stop makes a failed Set-Location
        # terminate the script before the payload is evaluated.
        return f"{changed};{command}"

    def command(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> ShellCommand:
        script = self.script(cmds, cwd=cwd, env=env)
        command = self.invocation(
            script,
            executable=executable,
        )
        return ShellCommand(subprocess.list2cmdline(command), None)

    def invocation(
        self, script: str, *, executable: typing.Optional[str] = None
    ) -> typing.Sequence[str]:
        return (
            executable or self.default_executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        )
