"""Windows CMD/BAT shell flavour."""

from __future__ import annotations

import os
import subprocess
import typing
from pathlib import PureWindowsPath

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
    for character in "&|<>()":
        value = value.replace(character, f"^{character}")
    escaped = []
    backslashes = 0
    for char in value:
        if char == "\\":
            backslashes += 1
        elif char == '"':
            # The caret preserves the quote through cmd.exe; the extra
            # backslash makes the child C argv parser retain it as data.
            escaped.append("\\" * (backslashes * 2 + 1) + '^"')
            backslashes = 0
        else:
            escaped.append("\\" * backslashes + char)
            backslashes = 0
    escaped.append("\\" * (backslashes * 2))
    # The delimiters must survive cmd.exe so the child C runtime, rather
    # than cmd itself, consumes them as argv quoting.
    return '^"' + "".join(escaped) + '^"'


def _builtin_argument(value: object) -> str:
    """Escape data for a cmd.exe builtin, which has no C argv parser."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    elif isinstance(value, bytes):
        value = value.decode("utf-8", "surrogateescape")
    text = str(value).replace("^", "^^")
    for character in '&|<>()@^"%!':
        if character == "^":
            continue
        text = text.replace(character, f"^{character}")
    return text


class CmdShellFlavour(ShellFlavour):
    """Windows ``cmd.exe`` and BAT-compatible command construction."""

    name = "cmd"
    default_executable = "cmd.exe"
    command_separator = "&"
    path_flavor = PureWindowsPath
    info_script = (
        "echo hostname=%COMPUTERNAME%&"
        "echo os_family=windows&"
        "echo os_name=%OS%&"
        "echo architecture=%PROCESSOR_ARCHITECTURE%"
    )
    builtins = frozenset(
        (
            "assoc",
            "break",
            "call",
            "cd",
            "chdir",
            "cls",
            "color",
            "copy",
            "date",
            "del",
            "dir",
            "echo",
            "endlocal",
            "erase",
            "exit",
            "md",
            "mkdir",
            "mklink",
            "move",
            "path",
            "pause",
            "popd",
            "prompt",
            "pushd",
            "rd",
            "ren",
            "rename",
            "rmdir",
            "set",
            "setlocal",
            "shift",
            "start",
            "time",
            "title",
            "type",
            "ver",
            "verify",
            "vol",
        )
    )

    def quote(self, value: object) -> str:
        return _argument(self._text(value))

    def structured_command(self, values: typing.Iterable[object]) -> str:
        values = tuple(values)
        if values and self._text(values[0]).casefold() in self.builtins:
            return " ".join(_builtin_argument(self._text(value)) for value in values)
        return super().structured_command(values)

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
        return f"cd /d {_builtin_argument(self._text(cwd))}"

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
