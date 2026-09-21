"""Fish shell flavour."""

from __future__ import annotations

import shlex
import typing
from pathlib import Path, PurePath, PurePosixPath

from ..executor import Environment, PathLike
from ._common import ShellCommand, ShellFlavour, ShellOperator, ShellToken

#: Characters that need no quoting, matching `shlex.quote`'s own set.
_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyz" "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "0123456789" "@%+=:,./-_"
)


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
        text = self._text(value)
        if not text:
            return "''"
        if all(char in _SAFE for char in text):
            return text
        # NOT shlex.quote: POSIX single quotes are literal throughout, but
        # fish honours a backslash escape inside them. A value ending in a
        # backslash therefore rendered as 'a\', whose closing quote fish
        # consumed as an escape -- merging it with the next argument and
        # leaving the remainder as fish source to execute.
        escaped = text.replace("\\", r"\\").replace("'", r"\'")
        return "'" + escaped + "'"

    def group(self, command: str) -> str:
        """fish has no `{ ...; }`; `begin; ...; end` is its block."""
        return "begin; " + command + "; end"

    def operator(self, value: ShellOperator) -> str:
        return {
            ShellOperator.PIPE: "|",
            ShellOperator.AND: "; and ",
            ShellOperator.OR: "; or ",
            ShellOperator.REDIRECT: " > ",
            ShellOperator.APPEND: " >> ",
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
