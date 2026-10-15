"""Turn a flavour's script into what an argv or command-line transport sends."""

from __future__ import annotations

import typing

from ..executor import CommandLine, Environment, PathLike
from ._common import ShellFlavour, ShellToken


def for_login_shell(
    flavour: ShellFlavour, command: str, login_shell: typing.Optional[str]
) -> str:
    """Escape a rendered command line for the shell that will parse it.

    A command line is rendered for exactly ONE parse. A transport that
    delivers it to a login shell -- SSH does; `CreateProcess` does not --
    makes that shell parse it first, consuming the layer the flavour
    applied. Only the transport knows this, so it passes `login_shell`, the
    name of what parses the line on arrival.

    Measured against a real cmd.exe standing in for Windows OpenSSH's stock
    server: 4 of 8 adversarial values survived unescaped, and two of the
    failures were command injection (`a&b`, `x|y`).
    """
    if flavour.argv_invocation:
        # The flavour renders argv, so what a transport sends is a SCRIPT and
        # the login shell's single parse is the one it was rendered for. This
        # is every POSIX-family flavour: `$SHELL -c <script>` is exactly the
        # target, and adding a layer here would corrupt it.
        return command
    if login_shell is not None and login_shell == flavour.name:
        # A command LINE delivered to the same shell it was rendered for:
        # that shell parses it, and then the line's own `/c` parses it again.
        # A different login shell either does not parse it or could not run
        # it anyway, and PowerShell's `-EncodedCommand` is inert under all of
        # them.
        return flavour.escape_for_one_parse(command)
    return command


def shell_invocation(
    flavour: ShellFlavour,
    cmds: typing.Iterable[ShellToken],
    *,
    cwd: typing.Optional[PathLike],
    env: typing.Optional[Environment],
    epilogue: bool,
    executable: typing.Optional[str] = None,
    login_shell: typing.Optional[str] = None,
) -> typing.Tuple[typing.Union[str, CommandLine], ...]:
    """One shell layer, as argv or as a command line.

    Most flavours render as argv. `cmd` cannot -- an argv element's quotes
    are escaped by the platform's own command-line quoting before cmd ever
    sees them (`CmdShellFlavour.invocation`), which silently corrupted every
    value carrying a quote or a metacharacter. For those the rendered command
    line is submitted instead, marked so the executor does not re-quote it.
    `login_shell` is what `for_login_shell` asks.
    """
    if not flavour.argv_invocation:
        rendered = flavour.command(cmds, executable=executable, cwd=cwd, env=env)
        return (CommandLine(for_login_shell(flavour, rendered.command, login_shell)),)
    script = flavour.script(cmds, cwd=cwd, env=env, epilogue=epilogue)
    return tuple(flavour.invocation(script, executable=executable))
