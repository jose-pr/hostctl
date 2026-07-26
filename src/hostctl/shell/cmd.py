"""Windows CMD/BAT shell flavour."""

from __future__ import annotations

import os
import subprocess
import typing

from ..executor import Environment, PathLike
from ._common import ShellCommand, ShellFlavour, ShellOperator, ShellToken


def _argument(value: object) -> str:
    """Quote one argv value for cmd.exe and a C-runtime child program."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    elif isinstance(value, bytes):
        value = value.decode("utf-8", "surrogateescape")
    value = str(value)
    if value and not any(char.isspace() or char in '&|<>()@^"%!' for char in value):
        return value
    value = value.replace("^", "^^").replace("%", "^%").replace("!", "^!")
    escaped = []
    backslashes = 0
    for char in value:
        if char == "\\":
            backslashes += 1
        elif char == '"':
            # A backslash is data to cmd.exe, not its quote escape.  Caret
            # quoting keeps the quote from terminating the command string.
            escaped.append("\\" * (backslashes * 2) + '^"')
            backslashes = 0
        else:
            escaped.append("\\" * backslashes + char)
            backslashes = 0
    escaped.append("\\" * (backslashes * 2))
    return '"' + "".join(escaped) + '"'


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
        return _argument(self._text(value))

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
        value = self._text(value).replace("^", "^^")
        value = value.replace("%", "^%").replace("!", "^!").replace('"', '^"')
        return f'set "{key}={value}"'

    def change_directory(self, cwd: PathLike) -> str:
        return f"cd /d {self.quote(cwd)}"

    def command(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> ShellCommand:
        script = self.script(cmds, cwd=cwd, env=env)
        executable_text = _argument(executable or self.default_executable)
        return ShellCommand(
            f'{executable_text} /d /v:off /s /c "{script}"',
            None,
        )

    def invocation(
        self, script: str, *, executable: typing.Optional[str] = None
    ) -> typing.Sequence[str]:
        return (
            executable or self.default_executable,
            "/d",
            "/v:off",
            "/s",
            "/c",
            script,
        )
