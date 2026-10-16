"""Turn a flavour's script into what an argv or command-line transport sends."""

from __future__ import annotations

import typing

from ..executor import CommandLine, CommandEnvironment, StrPath
from ._vocabulary import ShellCommand, ShellToken
from ._flavour import ShellFlavour


def render_command(
    flavour: ShellFlavour,
    cmds: typing.Iterable[ShellToken],
    *,
    executable: typing.Optional[str] = None,
    cwd: typing.Optional[StrPath] = None,
    env: typing.Optional[CommandEnvironment] = None,
    login_shell: typing.Optional[str] = None,
    stdin_free: bool = False,
) -> ShellCommand:
    """`flavour.command()` for a line a login shell parses before it runs.

    A transport that delivers a command string to a login shell (SSH does;
    `CreateProcess` does not) passes `login_shell`, the name of what parses
    it on arrival. The keyword is left off when there is none, so a flavour
    written before it existed keeps working for local use. `stdin_free` says
    the call sends no input of its own, so a flavour that can deliver its
    script on standard input may (see `ShellCommand.stdin`).
    """
    options: typing.Dict[str, object] = {}
    if login_shell is not None:
        options["login_shell"] = login_shell
    if stdin_free and flavour.accepts_script_stdin:
        options["stdin_free"] = True
    return flavour.command(cmds, executable=executable, cwd=cwd, env=env, **options)


def shell_invocation(
    flavour: ShellFlavour,
    cmds: typing.Iterable[ShellToken],
    *,
    cwd: typing.Optional[StrPath],
    env: typing.Optional[CommandEnvironment],
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
    `login_shell` is what `render_command` asks.
    """
    if not flavour.argv_invocation:
        rendered = render_command(
            flavour,
            cmds,
            executable=executable,
            cwd=cwd,
            env=env,
            login_shell=login_shell,
        )
        return (CommandLine(rendered.command),)
    script = flavour.script(cmds, cwd=cwd, env=env, epilogue=epilogue)
    return tuple(flavour.invocation(script, executable=executable))
