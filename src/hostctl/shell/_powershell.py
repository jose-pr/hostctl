"""Windows PowerShell shell flavour."""

from __future__ import annotations

import base64
import subprocess
import typing
from pathlib import PureWindowsPath

from ..executor import CommandEnvironment, StrPath, POWERSHELL_STATUS
from ._vocabulary import (
    ShellCommand,
    ShellOperator,
    ShellTarget,
    ShellToken,
    login_join,
    program_target,
)
from ._flavour import ShellFlavour
from ._grammar import Cmdlet, Command, PSParam
from ._cmd import _batch_token, _echo_argument

#: Keeps progress records off stderr; a script that sets it itself still wins.
_PRELUDE = "$ProgressPreference='SilentlyContinue'"
#: pwsh colours tables and error records when TERM names a colour terminal,
#: even into a pipe. Guarded: the executable may be a PowerShell without it.
_PLAIN_OUTPUT = "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }"


def _literal(value: object) -> str:
    return (
        "'"
        + str(value)
        .replace("'", "''")
        .replace("‘", "‘‘")
        .replace("’", "’’")
        .replace("‚", "‚‚")
        .replace("‛", "‛‛")
        + "'"
    )


def _crt_escape(value: str) -> str:
    """Escape `value` by MSVC C-runtime rules, without adding outer quotes.

    Windows PowerShell 5.1 re-quotes an already-parsed argument when it builds
    the command line for a *native* program: it wraps the value in `"` only
    when it contains whitespace, and leaves any embedded `"` alone. The child's
    C runtime then reads those quotes as structure, so `my file" /MIR "z`
    arrives as three arguments and `/MIR` is one of them.

    Escaping here -- inside the PowerShell literal, so PowerShell still hands
    the shell-level value through unchanged -- makes the child's CRT read the
    quotes as data. Outer quotes are deliberately not added: PS 5 strips a pair
    it finds and the CRT then mis-splits what is left.
    """
    if not value:
        # PS 5 drops an empty argument entirely; this two-character token is
        # what survives its rewrite as an empty argv entry.
        return '""'
    result = []
    backslashes = 0
    for char in value:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            result.append("\\" * (backslashes * 2 + 1) + '"')
        else:
            result.append("\\" * backslashes + char)
        backslashes = 0
    if backslashes:
        # A trailing run would escape the closing quote PS 5 adds for a value
        # containing whitespace, swallowing the argument that follows.
        result.append(
            "\\" * (backslashes * (2 if any(c.isspace() for c in value) else 1))
        )
    return "".join(result)


def _binder_wraps(token: str, *, major_version: int) -> bool:
    """Whether PowerShell re-wraps `token` in quotes for a native command line.

    The legacy binder wraps an argument when whitespace sits at an even count
    of `"` before it, counting every quote it can see (the `\\"` this package
    emits included); version 7 does not count a quote that directly follows a
    backslash. A token it wraps again is delivered with the extra pair read
    as structure by the child, so the caller refuses it rather than send it.
    """
    quotes = 0
    previous = ""
    for char in token:
        if char == '"':
            if not (major_version >= 7 and previous == "\\"):
                quotes += 1
        elif char.isspace() and quotes % 2 == 0:
            return True
        previous = char
    return False


#: Characters a batch file's re-parse would act on, which PowerShell passes
#: through unquoted when the value has no whitespace.
_BATCH_ACTIVE = frozenset('"&|<>')

_WILDCARD = frozenset("[]*?")


def _wildcard_escape(path: str) -> str:
    """Backtick-escape `path` for a parameter that matches wildcards.

    Windows PowerShell 5 resolves a called program path as a wildcard when it
    contains one, so `C:\\x\\[ab]\\t.cmd` runs `C:\\x\\a\\t.cmd` if that exists.
    The backtick is escaped too, but only once a wildcard character makes
    PowerShell read the path as a pattern.
    """
    if not _WILDCARD.intersection(path):
        return path
    return "".join("`" + char if char in "`[]*?" else char for char in path)


class PowerShellFlavour(ShellFlavour):
    name = "powershell"
    default_executable = "powershell.exe"
    command_separator = ";"
    context_order = ("cwd", "env", "command")
    # `Set-Location -ErrorAction Stop` terminates the script itself, so
    # there is nothing to fuse -- and PowerShell 5 has no `&&` to fuse with.
    cwd_guard = "statement"
    multiline_source = True
    # Both channels, on a line of its own. `$LASTEXITCODE` alone is set only
    # by NATIVE commands: a pure-cmdlet script that failed left it $null, and
    # `exit $null` is 0 -- so `Get-Item <missing>` returned success and
    # check=True never fired. It is also stale, so a native failure followed
    # by a successful cmdlet reported the native code. `$?` covers the cmdlet
    # half and `$LASTEXITCODE` keeps a native command's real code. The
    # leading newline is not cosmetic: appended after `;` on the same line, a
    # script ending in a `#` comment commented the whole epilogue out.
    # `$host.SetShouldExit`, not `exit`: an `exit` statement discards the
    # objects still deferred in the formatting pipeline, so an object result
    # such as `[pscustomobject]@{a=1}` printed nothing with status 0.
    execution_epilogue = "\n$host.SetShouldExit(" + POWERSHELL_STATUS + ")"
    structured_command_prefix = "& "
    pathname = PureWindowsPath
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
        # Identity comes from the VERSION, then the executable overrides
        # only the path. Deriving the name inside the `elif` meant
        # `PowerShellFlavour(7, executable="/opt/pwsh")` stayed named
        # "powershell" -- so `shell_flavour("pwsh")` round-trips, provider
        # selection and every log line disagreed with the shell in use.
        if major_version >= 7:
            self.name = "pwsh"
            self.default_executable = "pwsh"
        if executable is not None:
            self.default_executable = executable

    def quote(self, value: object) -> str:
        return _literal(self._text(value))

    def structured_command(self, values: typing.Iterable[object]) -> str:
        """Render `& 'program' 'argument' ...` for the call operator.

        The first element is the program and takes a plain literal. The
        rest are classified by the program's name (`command_target`): a
        native program, a batch file, or a name that could be either. A
        `PSParam` is the one token rendered bare, for a program that reads
        its own parameter names; cmdlets are `Cmdlet`.
        """
        values = tuple(values)
        if not values:
            return self.structured_command_prefix
        if isinstance(values[0], PSParam):
            raise TypeError("a PSParam cannot be the command itself")
        target = self.command_target(values)
        rendered = [self._program(values[0])]
        for value in values[1:]:
            if isinstance(value, PSParam):
                rendered.append(value.name)
            else:
                rendered.append(self.argument(value, target=target))
        return self.structured_command_prefix + " ".join(rendered)

    def _program(self, value: object) -> str:
        """The program slot after `&`: a literal, wildcard-escaped where needed.

        Only an absolute path is escaped. Measured on 5.1, an escaped
        relative path is not found at all, and the location a relative path
        would resolve against is itself reported escaped when it contains a
        wildcard character, so no spelling reads it literally.
        """
        text = self._text(value)
        if self.major_version < 7 and PureWindowsPath(text).is_absolute():
            return _literal(_wildcard_escape(text))
        return self.argument(text, target=ShellTarget.SHELL)

    def cmdlet_command(self, value: Cmdlet) -> str:
        """Render `& 'Name' args` for a cmdlet, function or alias.

        Arguments are PowerShell literals: no native program re-parses the
        line, so no C-runtime escaping applies and nothing is refused.
        """
        rendered = [self.quote(value.name)]
        for item in value.args:
            if isinstance(item, PSParam):
                rendered.append(item.name)
            else:
                rendered.append(self.argument(item, target=ShellTarget.CMDLET))
        return self.structured_command_prefix + " ".join(rendered)

    def command_target(self, values: typing.Sequence[object]) -> ShellTarget:
        """Classify the program by its name; PowerShell cannot know a cmdlet.

        Whether `Remove-Item` is a cmdlet or an executable on the guest's
        PATH needs a runspace to answer, so a name with no suffix is
        `UNRESOLVED` and the caller who knows says `Cmdlet`.
        """
        if not values or isinstance(values[0], PSParam):
            return ShellTarget.NATIVE
        return program_target(self._text(values[0]))

    def argument(
        self, value: object, *, target: ShellTarget = ShellTarget.NATIVE
    ) -> str:
        """Render one value for the parser that reads it after PowerShell.

        PowerShell 5 rebuilds a NATIVE command line, so the C-runtime layer
        belongs to `NATIVE` on 5 and nowhere else: applied to a cmdlet
        argument it would be visible in the bound parameter, and 7 does not
        rebuild the line. A `BATCH` target gets the batch token. A value that
        a native line cannot carry, or that a possible batch file would
        execute, raises rather than arriving changed.
        """
        text = self._text(value)
        if text == "--%" and target not in (ShellTarget.CMDLET, ShellTarget.BATCH):
            # A native argument whose value is `--%` is read as the
            # stop-parsing token however it is quoted; no spelling sends it.
            raise ValueError("PowerShell reads a native argument '--%' as stop-parsing")
        if target is ShellTarget.BATCH:
            token = _batch_token(text)
            if _binder_wraps(token, major_version=self.major_version):
                raise ValueError(
                    f"{self.name} would quote this batch-file argument a second "
                    f"time and the batch file would receive it split: {text!r}"
                )
            return _literal(token)
        if target is ShellTarget.UNRESOLVED:
            if _BATCH_ACTIVE.intersection(text):
                raise ValueError(
                    f"{text!r} could be executed if the program turns out to be a "
                    "batch file; name the program with its .exe or .bat suffix, "
                    "or use Cmdlet for a cmdlet"
                )
            target = ShellTarget.NATIVE
        if target is ShellTarget.NATIVE and self.major_version < 7:
            token = _crt_escape(text)
            if any(char.isspace() for char in token) and not _binder_wraps(
                token, major_version=5
            ):
                raise ValueError(
                    "Windows PowerShell 5 would split this argument at a space "
                    f"that follows a quote: {text!r}; use pwsh (PWSH), or Exec"
                )
            return _literal(token)
        return self.quote(text)

    def operator(self, value: ShellOperator) -> str:
        """Render an operator.

        Windows PowerShell 5 re-encodes the bytes of a pipe between native
        programs through the console code page, and `>`/`>>` write UTF-16LE
        with a byte-order mark; PowerShell 7 and cmd pass bytes through.
        """
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

    def redirect(self, value: ShellOperator, target: Command, operand: str) -> str:
        """Write to a file NAME through `Out-File -LiteralPath` on PowerShell 5.

        `>` is `Out-File -FilePath`, which reads `[ab]` in the name as a
        wildcard. `-Encoding Unicode` is what `>` writes on 5 (UTF-16LE with a
        byte-order mark); measured identical bytes for native and cmdlet
        output, new and appended. Raw text after `>` stays PowerShell source.
        pwsh keeps `>`: there, `| Out-File` would decode a native program's
        bytes, which its `>` writes untouched.
        """
        if self.major_version >= 7 or isinstance(target, str):
            return super().redirect(value, target, operand)
        append = " -Append" if value is ShellOperator.APPEND else ""
        return f" | Out-File -LiteralPath {operand} -Encoding Unicode{append}"

    def environment_assignment(self, key: str, value: object) -> str:
        """Render `$env:KEY='VALUE'`.

        An empty value unsets the variable under Windows PowerShell 5 (as
        under cmd); pwsh sets it empty.
        """
        return f"$env:{key}={_literal(self._text(value))}"

    def change_directory(self, cwd: StrPath) -> str:
        # `-ErrorAction Stop` is what guards the payload here: PowerShell 5
        # has no `&&`, so a failed `Set-Location` must terminate the script
        # itself rather than be joined to the payload by an operator. That
        # is also why this flavour needs no `join_cwd` override -- its
        # `context_order` puts `env` between cwd and command, so the base
        # class's fusing branch never applies. (It had one anyway, for
        # years, unreachable.)
        return (
            f"Set-Location -LiteralPath {_literal(self._text(cwd))} -ErrorAction Stop"
        )

    accepts_script_stdin = True

    def command(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[StrPath] = None,
        env: typing.Optional[CommandEnvironment] = None,
        login_shell: typing.Optional[str] = None,
        stdin_free: bool = False,
    ) -> ShellCommand:
        program = executable or self.default_executable
        options = (program, "-NoProfile", "-NonInteractive")
        body = self.script(cmds, cwd=cwd, env=env, epilogue=False)
        # The epilogue starts with a newline so a trailing `#` comment cannot
        # swallow it; joined with `;` it is only safe on a body that has none.
        epilogue = self.execution_epilogue
        prelude = _PRELUDE
        if self.major_version >= 7:
            prelude += ";" + _PLAIN_OUTPUT
        script = prelude + (";" + body if body else "") + epilogue
        one_line = not (set(body) & {"\r", "\n", "#"})
        if login_shell == "posix":
            # The portable join keeps the newline: nothing here crosses cmd.
            return ShellCommand(login_join((*options, "-Command", script)), None)
        if login_shell == "cmd" and one_line:
            # `-Command` is plain-text stderr and, unlike a long
            # `-EncodedCommand`, is not refused by endpoint protection. A
            # newline cannot cross `cmd.exe /c`, hence the one-line rule.
            joined = prelude + (";" + body if body else "") + ";"
            joined += epilogue.lstrip("\n")
            line = subprocess.list2cmdline((*options, "-Command", joined))
            return ShellCommand(_echo_argument(line), None)
        no_newline_body = not (set(body) & {"\r", "\n"})
        if stdin_free and no_newline_body:
            # The script rides on standard input: nothing in the command line
            # for a login shell to parse or endpoint protection to flag. A
            # multi-line construct is not safe here -- measured, PowerShell
            # reads stdin line by line and drops a block that is not followed
            # by a blank line -- so only a one-line body takes this route.
            line = subprocess.list2cmdline((*options, "-Command", "-"))
            if login_shell == "cmd":
                line = _echo_argument(line)
            return ShellCommand(line, None, stdin=script + "\n")
        # Last resort: `-EncodedCommand` is inert under cmd.exe, PowerShell
        # and a POSIX login shell alike, but it writes CLIXML to stderr and
        # endpoint protection flags it.
        payload = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        command = (*options, "-EncodedCommand", payload)
        return ShellCommand(subprocess.list2cmdline(command), None)

    def invocation(
        self, script: str, *, executable: typing.Optional[str] = None
    ) -> typing.Sequence[str]:
        if self.major_version >= 7:
            script = _PLAIN_OUTPUT + (";" + script if script else "")
        return (
            executable or self.default_executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        )
