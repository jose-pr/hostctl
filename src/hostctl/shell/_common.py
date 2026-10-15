"""Target-shell command construction independent of the host transport."""

from __future__ import annotations

import abc
import collections.abc
import copy
import dataclasses
import enum
import inspect
import os
import re
import typing
import types
from pathlib import Path, PurePath, PurePosixPath

from ..executor import ExecutorCapability, validate_environment_names
from ..executor._common import command_text, masked_environment, run_displayed
from ..executor import (
    CaptureOutput,
    Environment,
    FileHandle,
    Input,
    PathLike,
)
from ._grammar import Cmdlet, Command, Exec, PSParam
from ..process import Process, TerminalRequest

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


class ShellSession(Process):
    """A persistent process with shell-aware command submission."""

    def __init__(self, flavour: ShellFlavour, process: Process) -> None:
        self.flavour = flavour
        self.process = process

    @property
    def returncode(self) -> typing.Optional[int]:
        return self.process.returncode

    def send(
        self,
        *cmds: ShellToken,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> None:
        """Render and submit commands using this session's shell language."""
        if not cmds and cwd is None and not env:
            raise ValueError("send requires commands, cwd, or env")
        script = self.flavour.script(cmds, cwd=cwd, env=env, epilogue=False)
        # A line terminator ends a command in every supported shell; an
        # appended `;` broke a heredoc delimiter and an open pipeline.
        self.process.write(script + self.flavour.line_terminator)

    def write(self, data):
        self.process.write(data)

    def read(self, size: int = -1):
        return self.process.read(size)

    def read_stderr(self, size: int = -1):
        return self.process.read_stderr(size)

    def send_eof(self) -> None:
        self.process.send_eof()

    def resize(
        self,
        columns: int,
        rows: int,
        pixel_width: int = 0,
        pixel_height: int = 0,
    ) -> None:
        self.process.resize(columns, rows, pixel_width, pixel_height)

    def wait(self, timeout: typing.Optional[float] = None) -> int:
        return self.process.wait(timeout)

    def terminate(self) -> None:
        self.process.terminate()

    def kill(self) -> None:
        self.process.kill()

    def close(self) -> None:
        self.process.close()

    def __enter__(self) -> ShellSession:
        self.process.__enter__()
        return self

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> bool:
        return self.process.__exit__(exc_type, exc_value, traceback)


#: Control characters that are ordinary syntax in raw shell source.
_RAW_WHITESPACE = frozenset(("\t", "\n", "\r"))


class ShellFlavour(abc.ABC):
    """Construct scripts and commands for one explicitly selected target shell."""

    name: str
    default_executable: str
    info_script: str
    command_separator: str
    line_terminator: str = "\n"
    #: Whether raw source may span lines (a heredoc, a block). Where it does,
    #: the separator after a command that contains a newline is a newline:
    #: `EOF;next` leaves the here-document open and swallows what follows.
    multiline_source: typing.ClassVar[bool] = False
    #: Appended to a rendered script before it is executed. Declared here so
    #: `script()` reads an attribute instead of duck-probing with `getattr`.
    execution_epilogue: str = ""
    context_order = ("env", "cwd", "command")
    #: How a failed `cd` is stopped from running the payload anyway.
    #:
    #: `"operator"` fuses the directory change onto a GROUPED payload with
    #: this flavour's AND operator, so the guard covers all of it: fused
    #: onto the payload directly, the AND bound to the first command only,
    #: and `cd /srv && a; b` ran `b` in the login directory and exited 0.
    #:
    #: `"statement"` is for a flavour whose change-directory statement
    #: aborts the script by itself -- PowerShell's `-ErrorAction Stop` --
    #: where there is nothing to fuse, and PowerShell 5 has no `&&` to fuse
    #: with anyway.
    #:
    #: Declared, because it used to be INFERRED from whether `command`
    #: happened to sit next to `cwd` in `context_order`. PowerShell's order
    #: puts `env` between them, so its `join_cwd` override was unreachable
    #: for years and nothing failed to say so.
    cwd_guard: typing.ClassVar[str] = "operator"
    structured_command_prefix = ""
    path_flavor: type[PurePath] = PurePath

    def escape_for_one_parse(self, text: str) -> str:
        """Escape `text` so ONE parse by this shell hands it back verbatim.

        This is the OUTER layer, for a transport that delivers a finalized
        command line to a login shell which parses it before the command
        line's own shell ever runs. SSH is the case: AsyncSSH sends one
        command string, and Windows OpenSSH's stock server hands it to
        `cmd.exe /c` -- so a line from `CmdShellFlavour.command()`, which
        already carries one cmd layer, is parsed by cmd twice.

        The default refuses rather than guessing. A POSIX login shell
        receiving a POSIX-rendered line is the ordinary single-parse case
        and never reaches here; a flavour that genuinely has an escape for
        an extra parse implements it.
        """
        raise NotImplementedError(
            f"{self.name} has no escaping for an additional parse by itself; "
            "the command line is already rendered for exactly one"
        )

    #: Whether `invocation()` can express this shell as argv.
    #:
    #: False for `cmd`: Windows builds a child's command line with
    #: `CreateProcess` quoting, which escapes every `"` an argv element
    #: carries, and cmd reads those escapes as literal data. A caller holding
    #: a flavour that says False must render with `command()` and submit the
    #: result as a command line (`executor.CommandLine`).
    argv_invocation: typing.ClassVar[bool] = True
    #: Whether `command()` accepts `stdin_free=True` and may then return a
    #: `ShellCommand` whose script travels on standard input.
    accepts_script_stdin: typing.ClassVar[bool] = False

    @staticmethod
    def _text(value: object) -> str:
        """Normalize values and reject shell control characters."""
        text = command_text(value, errors="surrogateescape")
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise ValueError("shell values cannot contain control characters")
        return text

    @staticmethod
    def _raw_source(value: str) -> str:
        """Validate raw shell *source*, which is not a value.

        A quoted value must not carry control characters -- that is what
        `_text` enforces. Raw text is the opposite case: it is shell source
        the caller wrote deliberately, where a newline is ordinary syntax
        (and where a generated script, such as PowerShell's multi-line exit
        epilogue, may legitimately be handed back for a second render). Only
        NUL and the other non-whitespace control characters are refused --
        nothing in any supported shell needs them and they defeat terminal
        and log inspection.
        """
        if any(
            (ord(char) < 32 and char not in _RAW_WHITESPACE) or ord(char) == 127
            for char in value
        ):
            raise ValueError("shell text cannot contain control characters")
        return value

    @abc.abstractmethod
    def quote(self, value: object) -> str:
        """Quote one value for THIS SHELL's parser.

        The syntactic layer only: enough that the shell sees one word and
        expands nothing. What the token meets after that is
        :meth:`argument`'s question.
        """

    def argument(
        self, value: object, *, target: ShellTarget = ShellTarget.NATIVE
    ) -> str:
        """Quote one value for `target`, the parser that reads it next.

        The default is every flavour whose shell hands tokens straight to
        `execve`: there is no second parser, so `quote()` is the whole
        answer. `cmd` and PowerShell override it, because on Windows the
        child re-parses its own command line and the two layers disagree.
        """
        del target
        return self.quote(value)

    def command_target(self, values: typing.Sequence[object]) -> ShellTarget:
        """Which parser this command's arguments will meet.

        Inferred where it is decidable -- `cmd` knows its own builtin list --
        and `NATIVE` otherwise. It is deliberately NOT inferred for
        PowerShell: whether `Remove-Item` is a cmdlet or an executable on the
        guest's PATH cannot be known without a runspace, and guessing is the
        class of mistake this package has been removing. A caller who knows
        passes `target=` to :meth:`argument` directly.
        """
        del values
        return ShellTarget.NATIVE

    @abc.abstractmethod
    def operator(self, value: ShellOperator) -> str:
        """Render one supported command operator."""

    def structured_command(self, values: typing.Iterable[object]) -> str:
        values = tuple(values)
        if any(isinstance(value, PSParam) for value in values):
            raise TypeError(
                f"PSParam is a PowerShell token; {self.name} has no use for it"
            )
        if not values:
            return self.structured_command_prefix
        target = self.command_target(values)
        # The first element names the program; it is never argv data.
        head = self.argument(values[0], target=ShellTarget.SHELL)
        return self.structured_command_prefix + " ".join(
            [head] + [self.argument(value, target=target) for value in values[1:]]
        )

    def cmdlet_command(self, value: Cmdlet) -> str:
        """Render a `Cmdlet`; only PowerShell has cmdlets."""
        raise TypeError(f"Cmdlet is a PowerShell command and cannot run in {self.name}")

    @abc.abstractmethod
    def environment_assignment(self, key: str, value: object) -> str:
        """Render one validated environment assignment."""

    def environment_script(self, env: Environment) -> str:
        """Convert an environment mapping into a standalone shell script."""
        assignments = []
        for key, value in env.items():
            if isinstance(key, bytes):
                key = key.decode("utf-8", "surrogateescape")
            validate_environment_names({key: value})
            assignments.append(self.environment_assignment(key, value))
        return self.command_separator.join(assignments)

    def command_text(self, value: Command) -> str:
        if isinstance(value, Exec):
            # Direct execution bypasses the shell entirely, so an `Exec` that
            # reaches here means a transport did not take its direct branch.
            # Rendering it as a quoted argv would silently reintroduce the
            # shell layer the caller asked to skip.
            raise TypeError(
                "Exec is executed directly and cannot be rendered into a shell "
                "script; this host does not support direct execution"
            )
        if isinstance(value, Cmdlet):
            return self.cmdlet_command(value)
        if isinstance(value, PSParam):
            raise TypeError("PSParam is only valid inside a PowerShell command")
        if isinstance(value, (bytes, PurePath, Path, os.PathLike)):
            # A standalone path is a program to run: PowerShell needs the call
            # operator or it prints the string, and cmd reads a command word.
            return self.structured_command((value,))
        if isinstance(value, str):
            return self._raw_source(value)
        if isinstance(value, collections.abc.Iterable):
            values = tuple(value)
            if not values:
                raise ValueError("structured command must not be empty")
            return self.structured_command(values)
        return self._text(value)

    def redirect_operand(self, value: Command) -> str:
        """Render the file name after `>` or `>>`.

        Raw text stays source. A path, bytes or a one-element structured
        command is a name, quoted for the shell's own parser: the redirect
        target is read by the shell, never by a child's C runtime.
        """
        if isinstance(value, str):
            return self._raw_source(value)
        if isinstance(value, (bytes, PurePath, Path, os.PathLike)):
            return self.argument(value, target=ShellTarget.SHELL)
        if isinstance(value, collections.abc.Iterable):
            values = tuple(value)
            if len(values) != 1:
                raise ValueError("a redirect takes exactly one file name")
            return self.argument(values[0], target=ShellTarget.SHELL)
        raise TypeError("a redirect operand must be a path or a one-element command")

    def redirect(self, value: ShellOperator, target: Command, operand: str) -> str:
        """Render a REDIRECT or APPEND `value` together with its target.

        `operand` is `redirect_operand(target)`; `target` is passed as well so
        a flavour can tell raw source from a file name.
        """
        del target
        return self.operator(value) + operand

    def join(self, values: typing.Iterable[ShellToken]) -> str:
        """Join commands, preserving raw strings and explicit operators."""
        result = []
        pending = self.command_separator
        redirecting: typing.Optional[ShellOperator] = None
        has_command = False
        expecting_command = False
        for value in values:
            if isinstance(value, ShellOperator):
                if not has_command or expecting_command:
                    raise ValueError("shell operator must appear between commands")
                pending = self.operator(value)
                redirecting = (
                    value
                    if value in (ShellOperator.REDIRECT, ShellOperator.APPEND)
                    else None
                )
                expecting_command = True
                continue
            if redirecting is not None:
                # What follows `>` is a file name, not a command.
                operand = self.redirect_operand(value)
                if not operand:
                    continue
                result.append(self.redirect(redirecting, value, operand))
            else:
                command = self.command_text(value)
                if not command:
                    continue
                if has_command:
                    if pending == self.command_separator:
                        pending = self._separator_after(result[-1])
                    result.append(pending)
                result.append(command)
            has_command = True
            expecting_command = False
            redirecting = None
            pending = self.command_separator
        if expecting_command:
            raise ValueError("shell operator must be followed by a command")
        return "".join(result)

    def script(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        epilogue: bool = True,
    ) -> str:
        """Build a script, applying environment and cwd consistently.

        `epilogue=False` leaves off the flavour's status epilogue, for a
        script whose transport reports the status itself (`wants_epilogue`
        answers that for a set of capabilities) or that is one line of a
        persistent session.
        """
        command = self.join(cmds)
        changed = self.change_directory(cwd) if cwd is not None else ""
        rendered = {
            "env": self.environment_script(env) if env else "",
            "cwd": changed,
            "command": command,
        }
        if cwd is not None and command and self.cwd_guard == "operator":
            if "cwd" not in self.context_order or "command" not in self.context_order:
                raise ValueError(
                    f"{self.name}: cwd_guard='operator' needs both 'cwd' and "
                    f"'command' in context_order, found {self.context_order!r}"
                )
            rendered["cwd"] = self.join_cwd(changed, command)
            rendered["command"] = ""
        parts = [rendered[name] for name in self.context_order if rendered[name]]
        script = ""
        for part in parts:
            if script:
                script += self._separator_after(script)
            script += part
        if script and epilogue and self.execution_epilogue:
            script += self.execution_epilogue
        return script

    @abc.abstractmethod
    def change_directory(self, cwd: PathLike) -> str:
        """Render a directory change which fails if the directory is absent."""

    def _separator_after(self, command: str) -> str:
        """The separator that may follow `command` without breaking it."""
        if self.multiline_source and "\n" in command:
            return self.line_terminator
        return self.command_separator

    def group(self, command: str) -> str:
        """Wrap `command` so an operator applies to all of it, not its head."""
        if self.multiline_source and "\n" in command:
            # `; }` after a here-document delimiter would not close it.
            return "{ " + command + self.line_terminator + "}"
        return "{ " + command + "; }"

    def join_cwd(self, changed: str, command: str) -> str:
        """Join cwd setup and payload with the shell's AND operator.

        Reached only under `cwd_guard = "operator"`. The payload is grouped
        first: fused on directly, the AND bound to the *first* command only,
        so `cd /srv && a; b` ran `b` in the login directory and exited 0 --
        reachable through QGA (which always embeds cwd), fish or cmd over
        SSH, and any Shell over a capability-less executor.
        """
        return f"{changed}{self.operator(ShellOperator.AND)}{self.group(command)}"

    @abc.abstractmethod
    def command(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        login_shell: typing.Optional[str] = None,
    ) -> ShellCommand:
        """Build the command submitted to an SSH exec channel.

        The string is parsed first by `login_shell`: `None` means nothing
        parses it before the command line's own shell (as with
        `CreateProcess`), `"posix"` an sh-family shell or fish, `"cmd"` and
        `"powershell"` those shells. Each flavour's rule:

        * POSIX and fish join the invocation with `login_join`, which sh,
          bash, zsh, dash, ash and fish read back identically; `cwd` is part
          of the script. csh and tcsh are not supported.
        * PowerShell renders `-Command` for a `cmd` or POSIX login shell,
          the script on standard input (`ShellCommand.stdin`) when it
          cannot cross cmd.exe as an argument and the call leaves stdin
          free, and `-EncodedCommand <base64 utf-16le>` as the last resort.
        * `cmd` escapes exactly one cmd layer and, for `login_shell="cmd"`,
          one more for the login shell's own parse -- see the divergence
          ledger in `docs/guide/contracts.md`.
        """

    @abc.abstractmethod
    def invocation(
        self,
        script: str,
        *,
        executable: typing.Optional[str] = None,
    ) -> typing.Sequence[str]:
        """Build local-process argv which invokes this shell for one script.

        Raises `NotImplementedError` when this shell cannot be invoked that
        way -- see `argv_invocation`, and use `command()` instead.
        """

    def __str__(self) -> str:
        return self.name


class Shell(typing.Generic[_Result]):
    """Bind one shell language to a host or to an executor.

    Over a **host**, `run()` is that host's `run()`: the host renders exactly
    one shell layer with its provider's capabilities, so `host.shell.run(x)`
    hands the provider what `host.run(x)` does. Over an **executor** (a
    callable such as `LocalExecutor()`), the shell renders the script itself
    and sends it as a script string, as argv (`args`), or as a command line,
    embedding `cwd`/`env` unless the executor carries them natively.

    A `Shell` is not an `Executor`: calling it configures it, and a
    positional argument raises `TypeError`.
    """

    def __init__(
        self,
        flavour: ShellFlavour,
        executor: typing.Union[typing.Callable[..., _Result], _CommandHost],
        *,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> None:
        self._flavour = flavour
        #: Defaults applied to every `run` and `session` call that does not
        #: pass its own value. `cwd`, `encoding`, and `errors` override
        #: wholesale; `env` merges per key so a call can change one variable
        #: without restating the rest (see `_resolve_env`).
        self._cwd = cwd
        # An empty mapping would reach `subprocess` as `env={}` -- a child
        # started with NO environment -- where the caller meant "no overrides".
        self._env = dict(env) if env else None
        self._encoding = encoding
        self._errors = errors
        run = getattr(executor, "run", None)
        spawn = getattr(executor, "spawn", None)
        self._spawn = spawn if callable(spawn) else None
        self._session: typing.Optional[ShellSession] = None
        self._host = None
        if callable(executor):
            self._execute = executor
        elif callable(run):
            self._host = executor
            self._execute = run
            try:
                named = executor.shell_flavour.name
            except NotImplementedError:
                named = None
            if named != flavour.name:
                raise ValueError(
                    f"a {flavour.name} shell cannot run on a host that names "
                    f"{named or 'no'} shell flavour"
                )
        else:
            raise TypeError("executor must be callable or provide run(command)")
        try:
            self._executor_parameters = inspect.signature(self._execute).parameters
        except (TypeError, ValueError):
            self._executor_parameters = {}
        self._executor_accepts_options = any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in self._executor_parameters.values()
        )
        published = getattr(executor, "executor_capabilities", None)
        if published is None:
            inferred = set()
            if self._accepts_keyword("cwd"):
                inferred.add(ExecutorCapability.CWD)
            if self._accepts_keyword("env"):
                inferred.add(ExecutorCapability.ENV)
            if any(
                item.kind is inspect.Parameter.VAR_POSITIONAL
                for item in self._executor_parameters.values()
            ):
                inferred.add(ExecutorCapability.ARGS)
            published = frozenset(inferred)
        # One capability vocabulary, strings -- see `ExecutorCapability`.
        self.executor_capabilities = frozenset(published)
        self._executor_accepts_cwd = (
            ExecutorCapability.CWD in self.executor_capabilities
        )
        self._executor_accepts_env = (
            ExecutorCapability.ENV in self.executor_capabilities
        )

    flavour = property(lambda self: self._flavour)
    cwd = property(lambda self: self._cwd)
    env = property(lambda self: self._env)
    encoding = property(lambda self: self._encoding)
    errors = property(lambda self: self._errors)

    def _accepts_keyword(self, name: str) -> bool:
        parameter = self._executor_parameters.get(name)
        return parameter is not None and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )

    def _resolve_env(self, env: EnvironmentSelection) -> typing.Optional[Environment]:
        """Merge a per-call environment over this shell's default.

        The default `{}` merges nothing, so a call that says nothing about the
        environment inherits the shell's. A mapping merges over that default
        per key: the call wins for keys it names, and default-only keys
        survive, so one variable can change without restating the rest.

        `None` declines the shell's configured defaults. It does **not** mean
        an empty environment: the command still runs with whatever the host
        provides on its own -- a login profile, rc files, the service
        environment -- because nothing is sent to override it. Requesting a
        genuinely empty environment is a separate feature that does not exist
        yet.
        """
        if env is None:
            return None
        if self._env is None:
            return dict(env) if env else None
        merged = dict(self._env)
        merged.update(env)
        return merged

    def _resolve_cwd(self, cwd: typing.Optional[PathLike]) -> typing.Optional[PathLike]:
        return self._cwd if cwd is None else cwd

    def __call__(
        self,
        *args: object,
        cwd: typing.Optional[PathLike] = None,
        env: EnvironmentSelection = _INHERIT_ENV,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> "Shell[_Result]":
        """Configure: return a copy carrying these defaults (see `configure`)."""
        if args:
            raise TypeError(
                "a Shell is not an Executor: call host.executor(command, *args) "
                "or host.run(Exec(command, *args)) to run a program with argv, "
                "or shell.run(...) to run shell commands"
            )
        return self.configure(cwd=cwd, env=env, encoding=encoding, errors=errors)

    def run(
        self,
        *cmds: ShellToken,
        stdin: typing.Optional[FileHandle] = None,
        stdout: typing.Optional[FileHandle] = None,
        stderr: typing.Optional[FileHandle] = None,
        cwd: typing.Optional[PathLike] = None,
        env: EnvironmentSelection = _INHERIT_ENV,
        capture_output: typing.Optional[CaptureOutput] = None,
        check: typing.Optional[bool] = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: Input = None,
        timeout: typing.Optional[float] = None,
        text: typing.Optional[bool] = None,
    ) -> _Result:
        """Run shell commands, as one script, on the host or executor.

        `env` merges over the shell's default per key; the default merges
        nothing and so inherits it. Pass `None` to run without the shell's
        configured environment, keeping whatever the host provides itself.
        """
        cwd = self._resolve_cwd(cwd)
        env = self._resolve_env(env)
        options = {
            name: value
            for name, value in (
                ("stdin", stdin),
                ("stdout", stdout),
                ("stderr", stderr),
                ("capture_output", capture_output),
                ("check", check),
                ("encoding", self._encoding if encoding is None else encoding),
                ("errors", self._errors if errors is None else errors),
                ("input", input),
                ("timeout", timeout),
                ("text", text),
            )
            if value is not None
        }
        if self._host is not None:
            if cwd is not None:
                options["cwd"] = cwd
            if env is not None:
                options["env"] = env
            return self._execute(*cmds, **options)
        if cwd is not None and self._executor_accepts_cwd:
            options["cwd"] = cwd
        if env is not None and self._executor_accepts_env:
            options["env"] = env
        unsupported = [
            name
            for name in options
            if not self._accepts_keyword(name) and not self._executor_accepts_options
        ]
        if unsupported:
            raise TypeError(f"executor does not accept {sorted(unsupported)[0]}")
        embed_cwd = None if self._executor_accepts_cwd else cwd
        embed_env = None if self._executor_accepts_env else env
        epilogue = wants_epilogue(self.executor_capabilities)

        def render(environment):
            if ExecutorCapability.ARGS in self.executor_capabilities:
                from ._invocation import shell_invocation

                leading = shell_invocation(
                    self._flavour,
                    cmds,
                    cwd=embed_cwd,
                    env=environment,
                    epilogue=epilogue,
                )
                return (leading[0], *leading[1:])
            return (
                self._flavour.script(
                    cmds, cwd=embed_cwd, env=environment, epilogue=epilogue
                ),
            )

        sent = render(embed_env)
        if embed_env:
            # Embedded values would otherwise reach any exception or log line
            # that carries the command; show a rendering with them masked.
            shown = render(masked_environment(embed_env))
            if shown != sent:
                return run_displayed(shown, lambda: self._execute(*sent, **options))
        return self._execute(*sent, **options)

    def session(
        self,
        *cmds: ShellToken,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: EnvironmentSelection = _INHERIT_ENV,
        terminal: TerminalRequest = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> ShellSession:
        """Open a persistent process using this shell language.

        `env` merges over the shell's default per key; the default merges
        nothing and so inherits it. Pass `None` to open the session without
        the shell's configured environment, keeping whatever the host
        provides itself.
        """
        if self._spawn is None:
            raise NotImplementedError("executor does not provide persistent sessions")
        cwd = self._resolve_cwd(cwd)
        env = self._resolve_env(env)
        session = ShellSession(
            self._flavour,
            self._spawn(
                executable=executable,
                terminal=terminal,
                encoding=self._encoding if encoding is None else encoding,
                errors=self._errors if errors is None else errors,
            ),
        )
        # A shell default cwd/env applies to the session too: it is submitted
        # once here so it persists for every later `send` in that shell.
        if cmds or cwd is not None or env:
            session.send(
                *cmds,
                cwd=cwd,
                env=env,
            )
        return session

    def configure(
        self,
        *,
        cwd: typing.Optional[PathLike] = None,
        env: EnvironmentSelection = _INHERIT_ENV,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> "Shell[_Result]":
        """Return a copy of this shell with additional defaults applied.

        `env` merges over this shell's default the same way a per-call `env`
        does, so configuring twice layers rather than replaces, and `None`
        produces a copy carrying no environment default at all. The original
        shell is left unchanged.
        """
        clone = copy.copy(self)
        clone._session = None
        clone._cwd = self._cwd if cwd is None else cwd
        clone._env = self._resolve_env(env)
        clone._encoding = self._encoding if encoding is None else encoding
        clone._errors = self._errors if errors is None else errors
        return clone

    def __enter__(self) -> ShellSession:
        """Open a default session, so ``with host.shell as session:`` works.

        The session is closed on exit. `session(...)` remains the way to pass
        a command, cwd, env, terminal, or encoding; this is the no-argument
        shorthand. A `Shell` is not reusable as a context manager while a
        session it opened is still active -- each `with` opens its own.
        """
        if self._session is not None:
            raise RuntimeError("shell already has an active session")
        session = self.session()
        try:
            # Enter the session so the underlying process sees a balanced
            # __enter__/__exit__ pair; `session.__exit__` delegates to it.
            session.__enter__()
        except BaseException:
            session.close()
            raise
        self._session = session
        return session

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> bool:
        session, self._session = self._session, None
        if session is None:
            return False
        return session.__exit__(exc_type, exc_value, traceback)
