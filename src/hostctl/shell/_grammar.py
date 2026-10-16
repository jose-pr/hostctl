"""The command grammar: what a caller may write in `run()`.

This lives beside the shell flavours rather than with the hosts because it
IS the command language -- `ShellFlavour.command_text` dispatches on it, and
`Exec` exists precisely to say "no shell layer". Host orchestration imports
it from here, never the other way round, so `hostctl.shell` imports on its
own. `hostctl.Exec` re-exports it.
"""

from __future__ import annotations

import dataclasses as _dc
import re as _re
import typing as _ty
from pathlib import PurePath as _PurePath

from pathapi import Pathname as _Pathname

from ..executor import PathLike

#: One argv value. Everything reaching a transport is text, so a program or
#: argument may be spelled as a string, bytes, or a path object
#: interchangeably -- the spelling records how the caller held the value, not
#: what crosses the wire.
ExecValue = _ty.Union[str, bytes, _PurePath, _Pathname]


@_dc.dataclass(frozen=True)
class Exec:
    """One command executed directly, with no shell layer.

    ``Exec(program, *args)`` names a program and its argv. The program may be
    an absolute path or a bare name the target resolves through ``PATH``; a
    bare name has no other spelling, since a plain string is always shell text.

    This is the explicit marker for direct execution. A path used anywhere
    else is an ordinary value that stringifies, so several commands can be
    written without one of them silently becoming an executable::

        host.run(Exec("/bin/ls", "-l"))       # one direct command
        host.run(Exec("ls", "-l"))            # PATH-resolved, no shell
        host.run(Exec("/bin/a"), Exec("/bin/b"))   # two direct commands
        host.run(PurePosixPath("/bin/a"), PurePosixPath("/bin/b"))
                                              # two ordinary shell commands

    Arguments are argv values, never nested commands: a list or tuple raises
    ``TypeError`` rather than blurring argv and shell semantics.

    A deliberately non-iterable container. `ShellFlavour.command_text`
    dispatches structured commands on ``Iterable``, so an iterable marker
    would be quoted into an argv string instead of taking the direct branch.
    """

    program: ExecValue
    args: _ty.Tuple[ExecValue, ...]

    def __init__(self, program: ExecValue, *args: ExecValue) -> None:
        if not isinstance(program, (str, bytes, _PurePath, _Pathname)):
            raise TypeError("Exec program must be a str, bytes, or path value")
        for value in args:
            if isinstance(value, (tuple, list)):
                raise TypeError("direct command arguments must be scalar values")
            if not isinstance(value, (str, bytes, _PurePath, _Pathname)):
                raise TypeError(
                    "direct command arguments must be str, bytes, or path values"
                )
        object.__setattr__(self, "program", program)
        object.__setattr__(self, "args", tuple(args))


_PS_PARAMETER = _re.compile(r"^-[A-Za-z][A-Za-z0-9]*:?$")


@_dc.dataclass(frozen=True)
class PSParam:
    """A PowerShell parameter name, rendered bare inside a PowerShell command.

    PowerShell's binder does not read a quoted string as a parameter name, so
    a plain string in a structured command is always a value. Use
    ``PSParam("-LiteralPath")`` where a cmdlet needs the name itself::

        host.run(Cmdlet("Remove-Item", PSParam("-LiteralPath"), path))

    The name is a leading dash, letters and digits, and an optional trailing
    colon (``-Path:``). Not iterable, like `Exec`; other shells refuse it.
    """

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _PS_PARAMETER.match(self.name):
            raise ValueError(f"not a PowerShell parameter name: {self.name!r}")


@_dc.dataclass(frozen=True)
class Cmdlet:
    """One PowerShell command whose arguments the cmdlet binder reads.

    ``Cmdlet(name, *args)`` renders ``& 'name' args``: every plain string is a
    PowerShell string literal and a `PSParam` is a bare parameter name. No
    C-runtime quoting is applied, because no native program re-parses the
    line, so this is the spelling for cmdlets, functions and aliases. A
    command that may be a native program or a batch file is a plain tuple.

    Not iterable, like `Exec`; other shells refuse it.
    """

    name: str
    args: _ty.Tuple[_ty.Union[str, bytes, _PurePath, _Pathname, PSParam], ...]

    def __init__(self, name: str, *args: object) -> None:
        if not isinstance(name, str) or not name:
            raise TypeError("Cmdlet name must be a non-empty str")
        for value in args:
            if isinstance(value, (tuple, list)):
                raise TypeError("cmdlet arguments must be scalar values")
            if not isinstance(value, (str, bytes, _PurePath, _Pathname, PSParam)):
                raise TypeError(
                    "cmdlet arguments must be str, bytes, path or PSParam values"
                )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "args", tuple(args))


Command = _ty.Union[str, PathLike, Exec, Cmdlet, _ty.Sequence[object]]


def starts_direct_command(
    cmds: _ty.Sequence[Command],
) -> _ty.Optional[_ty.Tuple[ExecValue, _ty.Tuple[object, ...]]]:
    """Split an :class:`Exec` call into one executable and its argv arguments.

    Returns ``None`` unless the call is exactly one `Exec`. Direct execution
    replaces the whole command list rather than joining with anything, so an
    `Exec` alongside other commands is rejected: there is no shell to join
    them with, and silently running only one would lose the rest.
    """
    if not cmds:
        return None
    marked = [value for value in cmds if isinstance(value, Exec)]
    if not marked:
        return None
    if len(cmds) > 1:
        raise TypeError(
            "a direct Exec command cannot be combined with other commands; "
            "run it in its own call"
        )
    command = marked[0]
    return command.program, command.args
