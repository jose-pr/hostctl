"""Windows CMD/BAT shell flavour."""

from __future__ import annotations

import os
import typing

from ..executor import Environment, PathLike
from ._common import ShellCommand, ShellFlavour, ShellOperator, ShellToken


def _text(value: object) -> str:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    elif isinstance(value, bytes):
        value = value.decode()
    return str(value)


def _argument(value: object) -> str:
    """Quote one argv value passed through cmd.exe to a Windows program."""
    value = _text(value)
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("CMD arguments cannot contain NUL or newlines")
    if value and not any(char.isspace() or char in '&|<>()@^"' for char in value):
        return value
    # Quote values needing protection so CMD metacharacters remain argument
    # data. Backslash/quote handling follows the Windows C argv convention
    # used by most programs.
    escaped = value.replace('"', '\\"')
    trailing = len(escaped) - len(escaped.rstrip("\\"))
    if trailing:
        escaped += "\\" * trailing
    return f'"{escaped}"'


class CmdShellFlavour(ShellFlavour):
    """Windows ``cmd.exe`` and BAT-compatible command construction."""

    name = "cmd"
    default_executable = "cmd.exe"
    command_separator = "&"
    info_script = (
        "echo hostname=%COMPUTERNAME%&"
        "echo os_family=windows&"
        "echo os_name=%OS%&"
        "echo architecture=%PROCESSOR_ARCHITECTURE%"
    )

    def quote(self, value: object) -> str:
        return _argument(value)

    def operator(self, value: ShellOperator) -> str:
        return {
            ShellOperator.PIPE: "|",
            ShellOperator.AND: "&&",
            ShellOperator.OR: "||",
            ShellOperator.REDIRECT: ">",
            ShellOperator.APPEND: ">>",
            ShellOperator.SEQUENCE: self.command_separator,
        }[value]

    def environment_assignment(self, key: str, value: object) -> str:
        if isinstance(value, bytes):
            value = value.decode()
        value = str(value)
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError("CMD environment values cannot contain NUL or newlines")
        # The outer quotes are SET syntax and are not included in the value.
        return f'set "{key}={value.replace("%", "%%")}"'

    def script(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> str:
        script = self.join(cmds)
        parts = []
        if env:
            parts.append(self.environment_script(env))
        if cwd:
            parts.append(f"cd /d {_argument(cwd)}")
        if script:
            parts.append(script)
        return self.command_separator.join(parts)

    def command(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> ShellCommand:
        executable = _argument(executable or self.default_executable)
        script = self.script(cmds, cwd=cwd, env=env)
        return ShellCommand(f'{executable} /d /s /c "{script}"', None)

    def invocation(
        self, script: str, *, executable: typing.Optional[str] = None
    ) -> typing.Sequence[str]:
        return (executable or self.default_executable, "/d", "/s", "/c", script)
