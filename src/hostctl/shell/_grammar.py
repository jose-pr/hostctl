"""The command grammar: what a caller may write in `run()`.

This lives beside the shell flavours rather than with the hosts because it
IS the command language -- `ShellFlavour.command_text` dispatches on it, and
`Exec` exists precisely to say "no shell layer". `host/_common.py` imported
it the other way round for historical reasons, which made the grammar depend
on host orchestration and `hostctl.shell` unimportable on its own.

`hostctl.Exec` and `hostctl.host.Exec` are unchanged: both re-export from
here.
"""

from __future__ import annotations

import dataclasses as _dc
import typing as _ty
from pathlib import PurePath as _PurePath

from pathlib_next import Pathname as _Pathname

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


Command = _ty.Union[str, PathLike, Exec, _ty.Sequence[object]]


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
