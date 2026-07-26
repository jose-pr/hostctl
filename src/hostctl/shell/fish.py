"""Fish shell flavour."""

from __future__ import annotations

import os
import shlex
import typing
from pathlib import Path, PurePath, PurePosixPath

from ..executor import Environment, PathLike
from ._common import ShellCommand, ShellFlavour, ShellOperator, ShellToken


class FishShellFlavour(ShellFlavour):
    name = "fish"
    default_executable = "/usr/bin/fish"
    command_separator = ";"
    info_script = (
        "echo hostname=(hostname);"
        "echo os_family=(uname -s);"
        "echo architecture=(uname -m)"
    )

    def quote(self, value: object) -> str:
        if isinstance(value, (PurePath, Path)):
            value = value.as_posix()
        elif isinstance(value, os.PathLike):
            value = os.fspath(value)
        elif isinstance(value, bytes):
            value = value.decode()
        return shlex.quote(str(value))

    def operator(self, value: ShellOperator) -> str:
        return {
            ShellOperator.PIPE: "|",
            ShellOperator.AND: "; and ",
            ShellOperator.OR: "; or ",
            ShellOperator.REDIRECT: ">",
            ShellOperator.APPEND: ">>",
            ShellOperator.SEQUENCE: self.command_separator,
        }[value]

    def environment_assignment(self, key: str, value: object) -> str:
        if isinstance(value, bytes):
            value = value.decode()
        return f"set -gx {key} {shlex.quote(str(value))}"

    def script(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> str:
        parts = []
        if env:
            parts.append(self.environment_script(env))
        if cwd:
            parts.append(f"cd {shlex.quote(PurePosixPath(cwd).as_posix())}")
        command = self.join(cmds)
        if command:
            parts.append(command)
        return self.command_separator.join(parts)

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
        return ShellCommand(shlex.join(command), None)

    def invocation(
        self, script: str, *, executable: typing.Optional[str] = None
    ) -> typing.Sequence[str]:
        return (executable or self.default_executable, "-c", script)
