"""Windows PowerShell shell flavour."""

from __future__ import annotations

import base64
import subprocess
import re
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


#: A PowerShell parameter name and nothing else: a leading dash, letters
#: and digits, and an optional trailing colon (`-Path:`, the explicit-bind
#: spelling). Deliberately narrow -- anything that needs quoting cannot
#: match.
_PARAMETER_NAME = re.compile(r"^-[A-Za-z][A-Za-z0-9]*:?$")


def _is_parameter_name(value: object) -> bool:
    return isinstance(value, str) and _PARAMETER_NAME.match(value) is not None


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

        A token that is exactly a PARAMETER NAME (`-LiteralPath`, `-Force`,
        `-Path:`) is left unquoted. PowerShell's binder does not read a
        quoted string as a parameter name -- it binds it positionally -- so
        `["Remove-Item", "-LiteralPath", path]` rendered as three string
        literals failed to bind and deleted nothing, with no spelling in
        this grammar that could reach a named parameter at all. The pattern
        admits nothing that needs quoting (no space, no quote, no dollar),
        and for a native program an unquoted `-Force` arrives exactly as a
        quoted one does, so this changes only the cmdlet case.

        On PowerShell 5 each remaining element additionally carries
        C-runtime escaping, because PS 5 rebuilds the native command line
        itself and does it wrongly for quotes, empty values and trailing
        backslashes. PowerShell 7 passes arguments to a native program
        without that rewrite, so the escaping would be visible in the child
        and is not applied.
        """
        rendered = []
        for index, value in enumerate(values):
            if index and _is_parameter_name(value):
                rendered.append(typing.cast(str, value))
            elif self.major_version >= 7:
                rendered.append(self.quote(value))
            else:
                rendered.append(_literal(_crt_escape(self._text(value))))
        return self.structured_command_prefix + " ".join(rendered)

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

    def command(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> ShellCommand:
        script = self.script(cmds, cwd=cwd, env=env)
        # `-EncodedCommand`, not `-Command`: this string is submitted to an
        # SSH exec channel, where the REMOTE login shell parses it first --
        # and Windows OpenSSH's default shell is cmd.exe. The previous
        # spelling escaped with `subprocess.list2cmdline`, which implements
        # the CreateProcess rule and escapes nothing for a shell: measured
        # against a real outer cmd.exe, all 7 adversarial values failed, one
        # of them by running injected text. Base64 of UTF-16LE is inert under
        # cmd.exe, PowerShell and a POSIX login shell alike, which is the
        # same reason `NativeWinRMSession._wrapper()` uses it.
        payload = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        command = (
            executable or self.default_executable,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            payload,
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
