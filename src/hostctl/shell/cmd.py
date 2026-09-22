"""Windows CMD/BAT shell flavour."""

from __future__ import annotations

import os
import typing
from pathlib import PureWindowsPath

from ..executor import Environment, PathLike
from ._common import (
    ShellCommand,
    ShellFlavour,
    ShellOperator,
    ShellTarget,
    ShellToken,
)


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
        """The default layering: cmd, then the child's C runtime."""
        return _argument(self._text(value))

    def argument(
        self, value: object, *, target: ShellTarget = ShellTarget.NATIVE
    ) -> str:
        """Three genuinely different parsers, named.

        This is the flavour the distinction exists for: cmd splits a
        builtin's operands itself and never involves a C runtime, a child
        program re-parses the line cmd rebuilt, and the program slot is read
        by `CreateProcess` before cmd runs at all. One rule cannot serve
        three, and picking the wrong one has cost data -- `del /q "a b.txt"`
        deleting two other files, and a spaced `cmd.exe` path that would not
        launch at all.
        """
        text = self._text(value)
        if target is ShellTarget.PROGRAM:
            return _program(text)
        if target is ShellTarget.SHELL:
            return _builtin_argument(text)
        return _argument(text)

    def command_target(self, values: typing.Sequence[object]) -> ShellTarget:
        if values and self._text(values[0]).casefold() in self.builtins:
            return ShellTarget.SHELL
        return ShellTarget.NATIVE

    def structured_command(self, values: typing.Iterable[object]) -> str:
        values = tuple(values)
        if values and self._text(values[0]).casefold() == "echo":
            # `echo` is not a different TARGET -- it is a different escape
            # for the same one. Alone among the builtins it does not consume
            # quotes, it prints them, so quoting would change the output it
            # exists to produce, while its tail is still one operand.
            return " ".join(_echo_argument(self._text(value)) for value in values)
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
        # Bound out of the f-string on purpose: a multi-line expression
        # inside one is PEP 701, which the 3.9 floor does not parse.
        program = self.argument(
            executable or self.default_executable,
            target=ShellTarget.PROGRAM,
        )
        return ShellCommand(
            f'{program} /d /v:off /s /c "{script}"',
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
