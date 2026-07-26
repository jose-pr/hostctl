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
    path_flavor = PurePosixPath

    def quote(self, value: object) -> str:
        if isinstance(value, (PurePath, Path)):
            value = value.as_posix()
        return shlex.quote(self._text(value))

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
        return f"set -gx {key} {self.quote(value)}"

    def change_directory(self, cwd: PathLike) -> str:
        return f"cd -- {self.quote(PurePosixPath(cwd).as_posix())}"

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
