"""Windows PowerShell shell flavour."""

from __future__ import annotations

import subprocess
import typing
from pathlib import PureWindowsPath

from ..executor import Environment, PathLike
from ._common import ShellCommand, ShellFlavour, ShellOperator, ShellToken


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


class PowerShellFlavour(ShellFlavour):
    name = "powershell"
    default_executable = "powershell.exe"
    command_separator = ";"
    context_order = ("cwd", "env", "command")
    # Both channels, on a line of its own. `$LASTEXITCODE` alone is set only
    # by NATIVE commands: a pure-cmdlet script that failed left it $null, and
    # `exit $null` is 0 -- so `Get-Item <missing>` returned success and
    # check=True never fired. It is also stale, so a native failure followed
    # by a successful cmdlet reported the native code. `$?` covers the cmdlet
    # half and `$LASTEXITCODE` keeps a native command's real code. The
    # leading newline is not cosmetic: appended after `;` on the same line, a
    # script ending in a `#` comment commented the whole epilogue out.
    execution_epilogue = (
        "\nexit $(if ($?) { 0 } elseif ($LASTEXITCODE) { $LASTEXITCODE } else { 1 })"
    )
    structured_command_prefix = "& "
    path_flavor = PureWindowsPath
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
        if executable is not None:
            self.default_executable = executable
        elif major_version >= 7:
            self.default_executable = "pwsh"
            self.name = "pwsh"

    def quote(self, value: object) -> str:
        return _literal(self._text(value))

    def structured_command(self, values: typing.Iterable[object]) -> str:
        """Render `& 'program' 'argument' ...` for the call operator.

        On PowerShell 5 each element additionally carries C-runtime escaping,
        because PS 5 rebuilds the native command line itself and does it
        wrongly for quotes, empty values and trailing backslashes. PowerShell 7
        passes arguments to a native program without that rewrite, so the
        escaping would be visible in the child and is not applied.
        """
        if self.major_version >= 7:
            return super().structured_command(values)
        return self.structured_command_prefix + " ".join(
            _literal(_crt_escape(self._text(value))) for value in values
        )

    def operator(self, value: ShellOperator) -> str:
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

    def environment_assignment(self, key: str, value: object) -> str:
        return f"$env:{key}={_literal(self._text(value))}"

    def change_directory(self, cwd: PathLike) -> str:
        return (
            f"Set-Location -LiteralPath {_literal(self._text(cwd))} -ErrorAction Stop"
        )

    def join_cwd(self, changed: str, command: str) -> str:
        # PowerShell 5 has no &&.  ErrorAction Stop makes a failed Set-Location
        # terminate the script before the payload is evaluated.
        return f"{changed};{command}"

    def command(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> ShellCommand:
        script = self.script(cmds, cwd=cwd, env=env)
        command = self.invocation(
            script,
            executable=executable,
        )
        return ShellCommand(subprocess.list2cmdline(command), None)

    def invocation(
        self, script: str, *, executable: typing.Optional[str] = None
    ) -> typing.Sequence[str]:
        return (
            executable or self.default_executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        )
