"""Shell-independent command vocabulary: operators, targets and tokens."""

from __future__ import annotations


import dataclasses

import enum


import typing

import types

from pathlib import PurePosixPath

from ..executor import ExecutorCapability


from ..executor import Environment, PathLike

from ._grammar import Command

_Result = typing.TypeVar("_Result", covariant=True)


class _CommandHost(typing.Protocol):
    """What `Shell` needs of a host: something it can hand commands to.

    Structural, because `shell` must not import `host` at runtime.
    """

    def run(self, *cmds: typing.Any, **options: typing.Any) -> typing.Any: ...


class ShellTarget(str, enum.Enum):
    """Who parses a token *after* the shell has finished splitting it.

    A rendered command passes through two parsers, not one: the shell's, and
    then whatever the shell hands the token to. Where those coincide -- POSIX
    sh, where `execve` takes a vector and nothing re-parses -- one rule
    serves. Where they do not, each flavour had grown its own private answer
    and the rule a value got depended on which call path found it.

    The member is a `str`, like `ExecutorCapability`, so a flavour may accept
    a spelling this enum does not know.
    """

    #: The shell itself consumes it -- a `cmd` builtin, whose operands are
    #: split by cmd and never reach a C runtime.
    SHELL = "shell"
    #: A child program's own argv parser: the C runtime on Windows, nothing
    #: at all on POSIX.
    NATIVE = "native"
    #: PowerShell's parameter binder, which is neither of the above: it reads
    #: typed arguments and does not re-quote a native command line.
    CMDLET = "cmdlet"
    #: The program slot of a command line, read by `CreateProcess` before any
    #: shell sees it.
    PROGRAM = "program"
    #: A batch file's own `%*`/`%1` expansion: the argument is spliced into a
    #: line that cmd then parses again, whichever shell started the batch file.
    BATCH = "batch"
    #: A program the flavour cannot classify -- a bare name, a `.ps1`, any
    #: PATHEXT type -- and which may therefore turn out to be a batch file.
    UNRESOLVED = "unresolved"


def posix_cwd_operand(cwd: PathLike) -> str:
    """A POSIX path for `cd`, spelled so CDPATH cannot redirect it.

    `cd` consults CDPATH for any operand whose first component is not `.` or
    `..`, so a relative directory is written `./name`; `cd -- ./name` fails
    when the directory is absent instead of entering a CDPATH match.
    """
    path = PurePosixPath(cwd)
    text = path.as_posix()
    if not path.is_absolute() and path.parts and path.parts[0] not in (".", ".."):
        return "./" + text
    return text


def wants_epilogue(capabilities: typing.Iterable[str]) -> bool:
    """Whether a script for a provider with `capabilities` needs a status epilogue.

    A provider that declares `manages_status` reports the remote status on its
    own channel, and the flavour's epilogue would end the remote pipeline
    before that report could be emitted. Every caller that renders a script
    for a provider asks here, so the rule has one home.
    """
    return ExecutorCapability.MANAGES_STATUS.value not in set(capabilities)


_NATIVE_SUFFIXES = (".exe", ".com")

_BATCH_SUFFIXES = (".bat", ".cmd")


def program_target(program: str, builtins: typing.Collection[str] = ()) -> ShellTarget:
    """Classify a Windows program by its name alone.

    A builtin is the shell's own; `.exe`/`.com` is a native program; `.bat`/
    `.cmd` is a batch file, whose arguments cmd parses again. Everything else
    -- a bare name, `.ps1`, any other PATHEXT type -- is `UNRESOLVED`, since
    only the target's search path can say what it runs.
    """
    name = program.casefold()
    if name in builtins:
        return ShellTarget.SHELL
    if name.endswith(_NATIVE_SUFFIXES):
        return ShellTarget.NATIVE
    if name.endswith(_BATCH_SUFFIXES):
        return ShellTarget.BATCH
    return ShellTarget.UNRESOLVED


class ShellOperator(enum.Enum):
    """A command operator, spelled by each shell flavour in its own syntax.

    Place a member between two commands in a `run()`/`script()` call and
    the flavour renders it: `PIPE` is `|` everywhere, `AND`/`OR` are
    `&&`/`||` in POSIX and PowerShell 7 but raise on PowerShell 5, which
    has neither. A flavour that cannot express one refuses rather than
    approximating it.
    """

    PIPE = "pipe"
    AND = "and"
    OR = "or"
    REDIRECT = "redirect"
    APPEND = "append"
    SEQUENCE = "sequence"


ShellToken = typing.Union[Command, ShellOperator]


#: What a caller may pass for `env` on a `Shell` call. A mapping merges over
#: the shell's default per key; the default `{}` merges nothing and therefore
#: inherits it; `None` declines the shell's defaults, leaving whatever
#: environment the host itself provides (login profile, rc files, service
#: environment) untouched.
EnvironmentSelection = typing.Optional[Environment]


#: The `env` default: an empty mapping, meaning "merge nothing, inherit the
#: shell's environment". A module-level immutable value rather than a literal
#: `{}` in each signature, so the default can never be mutated by a caller.
_INHERIT_ENV: Environment = types.MappingProxyType({})


#: Characters a word may hold and still need no quoting under sh, bash, zsh,
#: dash, ash and fish alike. Narrower than `shlex.quote`'s set: `%` is fish's
#: PID expansion.
_LOGIN_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyz" "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "0123456789" "@+=:,./_-"
)


def login_join(argv: typing.Iterable[str]) -> str:
    """Join argv so sh, bash, zsh, dash, ash and fish all read it back verbatim.

    Inside single quotes fish honours a backslash escape and sh does not, so
    a backslash and a single quote are written outside the quotes, where both
    agree. A leading `=` is quoted because zsh expands it.
    """
    words = []
    for value in argv:
        if not value:
            words.append("''")
        elif value[0] != "=" and all(char in _LOGIN_SAFE for char in value):
            words.append(value)
        else:
            parts = []
            run = ""
            for char in value:
                if char in "\\'":
                    if run:
                        parts.append("'" + run + "'")
                        run = ""
                    parts.append("\\" + char)
                else:
                    run += char
            if run:
                parts.append("'" + run + "'")
            words.append("".join(parts))
    return " ".join(words)


@dataclasses.dataclass(frozen=True)
class ShellCommand:
    """A transport-ready command and any environment sent out of band."""

    command: str
    environment: typing.Optional[Environment]
    #: Text the transport must send on the command's standard input, for a
    #: flavour that delivers its script that way (`accepts_script_stdin`).
    stdin: typing.Optional[str] = None
