"""Windows CMD/BAT shell flavour."""

from __future__ import annotations

import os
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


def _echo_argument(value: object) -> str:
    """Escape data for `echo`, which prints quotes rather than consuming them."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    elif isinstance(value, bytes):
        value = value.decode("utf-8", "surrogateescape")
    text = str(value).replace("^", "^^")
    for character in '&|<>()"%!':
        text = text.replace(character, f"^{character}")
    return text


#: Characters a builtin argument may carry unquoted. A whitelist, because
#: cmd splits a builtin's operands on far more than whitespace -- `,`, `;` and
#: `=` are separators too, and `del /q a=b.txt` deleted `a` and `b.txt` while
#: the named file survived.
_BUILTIN_SAFE = frozenset("._-:\\/")


def _builtin_argument(value: object) -> str:
    """Quote and escape data for a cmd.exe builtin, which has no C argv parser.

    Caret escaping alone is not enough: a builtin's operands are split on
    whitespace, `,`, `;` and `=`, and a caret does not stop that. The value is
    therefore wrapped in double quotes, which builtins strip and which do stop
    the split. Inside a quoted span cmd ignores carets but still expands
    `%VAR%` and would end the span on a `"`, so those two characters are
    emitted caret-escaped *outside* the quotes instead.
    """
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    elif isinstance(value, bytes):
        value = value.decode("utf-8", "surrogateescape")
    text = str(value)
    if text and all(char.isalnum() or char in _BUILTIN_SAFE for char in text):
        return text

    parts: typing.List[str] = []
    span: typing.List[str] = []
    for char in text:
        if char in '%"':
            if span:
                parts.append('"' + "".join(span) + '"')
                span = []
            parts.append(f"^{char}")
        else:
            span.append(char)
    if span or not parts:
        parts.append('"' + "".join(span) + '"')
    return "".join(parts)


def _program(path: str) -> str:
    r"""Quote the PROGRAM of a command line, which cmd never parses.

    `CreateProcess` reads this token, so it takes C-runtime quoting -- plain
    double quotes. Rendered with `_argument`'s caret form, a path with a
    space came out as `^"C:\Program Files\...^"` and Windows looked for a
    program literally named `^`.
    """
    if '"' in path:
        raise ValueError(f"cmd executable path must not contain a quote: {path!r}")
    if any(character.isspace() for character in path):
        return f'"{path}"'
    return path


class CmdShellFlavour(ShellFlavour):
    """Windows ``cmd.exe`` and BAT-compatible command construction."""

    name = "cmd"
    default_executable = "cmd.exe"
    argv_invocation = False
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
            # `echo` is the one builtin that does not consume quotes: it
            # prints them, so quoting would change the output it exists to
            # produce. It also takes its whole tail as one operand, which is
            # what the quoting protects everywhere else.
            escape = (
                _echo_argument
                if self._text(values[0]).casefold() == "echo"
                else _builtin_argument
            )
            return " ".join(escape(self._text(value)) for value in values)
        return super().structured_command(values)

    def group(self, command: str) -> str:
        """cmd groups with parentheses."""
        return "(" + command + ")"

    def operator(self, value: ShellOperator) -> str:
        return {
            ShellOperator.PIPE: "|",
            ShellOperator.AND: "&&",
            ShellOperator.OR: "||",
            ShellOperator.REDIRECT: " > ",
            ShellOperator.APPEND: " >> ",
            ShellOperator.SEQUENCE: self.command_separator,
        }[value]

    def environment_assignment(self, key: str, value: object) -> str:
        """Render `set KEY=VALUE`, caret-escaping the value.

        Deliberately *not* the quoted `set "KEY=VALUE"` form. cmd does not
        process carets inside a quoted span, so escaping there put the carets
        into the child's environment verbatim (`100%` arrived as `100^%`),
        while `%VAR%` still expanded and a `"` in the value ended the
        assignment early -- leaving the rest of it to run as a command.
        Unquoted, every metacharacter is caret-escapable, which round-trips
        all of `100%`, `%OS%`, `c^d`, `a!b`, `x&y`, `a|b`, `(x)` and
        `say "hi"` through a real cmd.exe.

        A value of `""` is beyond cmd: `set KEY=` deletes the variable, and
        cmd has no spelling for an empty one. The variable arrives unset.
        """
        text = self._text(value).replace("^", "^^")
        for character in '&|<>()"%!':
            text = text.replace(character, f"^{character}")
        return f"set {key}={text}"

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
        return ShellCommand(
            f"{_program(executable or self.default_executable)} "
            f'/d /v:off /s /c "{script}"',
            None,
        )

    def invocation(
        self, script: str, *, executable: typing.Optional[str] = None
    ) -> typing.Sequence[str]:
        """cmd has no argv spelling; render with `command()` instead.

        The argv this used to return carried a script quoted for *cmd's* own
        parser (`^"`, `^&`, `^%`). Every consumer delivers argv through an
        exec-style API, and on Windows that means `CreateProcess` quoting:
        the script element is wrapped in quotes and each inner `"` is escaped
        with a backslash, which cmd passes to the child as literal data.
        Measured against a real `cmd.exe`, 14 of 16 adversarial values came
        back corrupted -- and there is provably no argv element whose encoded
        form yields an unescaped quote, so this is not a fixable escaping
        rule but a missing spelling.
        """
        raise NotImplementedError(
            "cmd cannot be invoked as argv: CreateProcess quoting escapes the "
            "quotes a cmd script needs. Render with command() and submit the "
            "result as an executor.CommandLine."
        )
