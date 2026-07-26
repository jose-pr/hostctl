"""Windows PowerShell shell flavour."""

from __future__ import annotations

import os
import subprocess
import typing

from ..executor import Environment, PathLike
from ._common import ShellCommand, ShellFlavour, ShellOperator, ShellToken


def _literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class PowerShellFlavour(ShellFlavour):
    name = "powershell"
    default_executable = "powershell.exe"
    command_separator = ";"
    structured_command_prefix = "& "
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
        if isinstance(value, os.PathLike):
            value = os.fspath(value)
        elif isinstance(value, bytes):
            value = value.decode()
        return _literal(value)

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
        if isinstance(value, bytes):
            value = value.decode()
        return f"$env:{key}={_literal(value)}"

    def script(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> str:
        script = self.join(cmds)
        if env:
            script = self.command_separator.join(
                part for part in (self.environment_script(env), script) if part
            )
        if cwd:
            script = f"Set-Location -LiteralPath {_literal(cwd)};{script}"
        return script

    def command(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> ShellCommand:
        command = self.invocation(
            self.script(cmds, cwd=cwd, env=env),
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
