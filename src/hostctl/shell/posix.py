"""POSIX shell flavour."""

from __future__ import annotations

import os
import shlex
import typing
from pathlib import Path, PurePath, PurePosixPath

from ..executor import Environment, PathLike
from ._common import ShellCommand, ShellFlavour, ShellOperator, ShellToken


class PosixShellFlavour(ShellFlavour):
    name = "posix"
    default_executable = "/bin/sh"
    command_separator = ";"
    info_script = (
        "printf 'hostname=%s\\n' \"$(hostname 2>/dev/null)\";"
        "printf 'os_family=%s\\n' \"$(uname -s 2>/dev/null)\";"
        "if [ -r /etc/os-release ]; then . /etc/os-release;"
        "printf 'os_name=%s\\n' \"$ID\";"
        "printf 'os_version=%s\\n' \"$VERSION_ID\"; fi;"
        "printf 'architecture=%s\\n' \"$(uname -m 2>/dev/null)\""
    )

    def quote(self, value: object) -> str:
        if isinstance(value, (PurePath, Path)):
            value = value.as_posix()
        return shlex.quote(self._text(value))

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
        return f"export {key}={self.quote(value)}"

    def change_directory(self, cwd: PathLike) -> str:
        return f"cd {self.quote(PurePosixPath(cwd).as_posix())}"

    def command(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> ShellCommand:
        command = self.invocation(
            self.script(cmds, env=env),
            executable=executable,
        )
        remote_command = shlex.join(command)
        if cwd:
            remote_command = f"{self.change_directory(cwd)}{self.operator(ShellOperator.AND)}{remote_command}"
        return ShellCommand(remote_command, None)

    def invocation(
        self, script: str, *, executable: typing.Optional[str] = None
    ) -> typing.Sequence[str]:
        return (executable or self.default_executable, "-c", script)


class BashShellFlavour(PosixShellFlavour):
    """Bash using the portable POSIX command-construction baseline."""

    name = "bash"
    default_executable = "/bin/bash"


class ZshShellFlavour(PosixShellFlavour):
    """Zsh using the portable POSIX command-construction baseline."""

    name = "zsh"
    default_executable = "/bin/zsh"
