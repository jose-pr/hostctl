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
from pathlib import Path, PurePath

from ..executor import (
    CommandArgument,
    Executor,
    ExecutorCapability,
    ExecutorCommand,
)
from ..executor import (
    CaptureOutput,
    Environment,
    FileHandle,
    Input,
    PathLike,
)
from ._grammar import Command, Exec
from ..process import Process, TerminalRequest

_Result = typing.TypeVar("_Result", covariant=True)
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

if typing.TYPE_CHECKING:
    from ..host._common import Host


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


@dataclasses.dataclass(frozen=True)
class ShellCommand:
    """A transport-ready command and any environment sent out of band."""

    command: str
    environment: typing.Optional[Environment]


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
        script = self.flavour.script(cmds, cwd=cwd, env=env, for_session=True)
        self.process.write(
            self.flavour.terminate_submission(script) + self.flavour.line_terminator
        )

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
    #: Trailing tokens that already end a command in this flavour, so
    #: `send()` does not append a second one. Extended by POSIX-family
    #: flavours, which also accept `&`, `|` and their doubled forms.
    submission_terminators: typing.Tuple[str, ...] = ()
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

    def terminate_submission(self, script: str) -> str:
        """Return `script` ending in exactly one command terminator.

        `send()` used to append the separator unconditionally, so ordinary
        shell text that already ended in one became a syntax error: `;;` in
        POSIX, and `while read x; do ...; done;;` is not a script. A
        background `&`, a pipe left open, and `&&`/`||` are terminators too
        -- appending after them produces `&;` and `&&;`.
        """
        stripped = script.rstrip()
        if not stripped:
            return script
        terminators = (self.command_separator,) + self.submission_terminators
        for terminator in sorted(terminators, key=len, reverse=True):
            if terminator and stripped.endswith(terminator):
                return stripped
        return stripped + self.command_separator

    def command_path(self, value: PathLike) -> PurePath:
        """Return a direct-command marker using the target shell's path syntax."""
        return self.path_flavor(value)

    #: Whether `invocation()` can express this shell as argv.
    #:
    #: False for `cmd`: Windows builds a child's command line with
    #: `CreateProcess` quoting, which escapes every `"` an argv element
    #: carries, and cmd reads those escapes as literal data. A caller holding
    #: a flavour that says False must render with `command()` and submit the
    #: result as a command line (`executor.CommandLine`).
    argv_invocation: typing.ClassVar[bool] = True

    @staticmethod
    def _text(value: object) -> str:
        """Normalize values and reject shell control characters."""
        # `__fspath__` first, and by attribute rather than `isinstance`, so a
        # duck-typed path that never registered with `os.PathLike` still
        # renders as its filesystem representation. See
        # `executor._common.command_text`, which follows the same rule.
        fspath = getattr(value, "__fspath__", None)
        if callable(fspath):
            try:
                resolved = fspath()
            except Exception:
                resolved = None
            if isinstance(resolved, (str, bytes)):
                value = resolved
        if isinstance(value, bytes):
            value = value.decode("utf-8", "surrogateescape")
        text = str(value)
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
        target = self.command_target(values)
        return self.structured_command_prefix + " ".join(
            self.argument(value, target=target) for value in values
        )

    @abc.abstractmethod
    def environment_assignment(self, key: str, value: object) -> str:
        """Render one validated environment assignment."""

    def environment_script(self, env: Environment) -> str:
        """Convert an environment mapping into a standalone shell script."""
        assignments = []
        for key, value in env.items():
            if isinstance(key, bytes):
                key = key.decode("utf-8", "surrogateescape")
            if not isinstance(key, str) or not _ENVIRONMENT_KEY.fullmatch(key):
                raise ValueError(f"invalid environment variable name: {key!r}")
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
        if isinstance(value, (bytes, PurePath, Path, os.PathLike)):
            return self.quote(value)
        if isinstance(value, str):
            return self._raw_source(value)
        if isinstance(value, collections.abc.Iterable):
            values = tuple(value)
            if not values:
                raise ValueError("structured command must not be empty")
            return self.structured_command(values)
        return self._text(value)

    def join(self, values: typing.Iterable[ShellToken]) -> str:
        """Join commands, preserving raw strings and explicit operators."""
        result = []
        pending = self.command_separator
        has_command = False
        expecting_command = False
        for value in values:
            if isinstance(value, ShellOperator):
                if not has_command or expecting_command:
                    raise ValueError("shell operator must appear between commands")
                pending = self.operator(value)
                expecting_command = True
                continue
            command = self.command_text(value)
            if not command:
                continue
            if has_command:
                result.append(pending)
            result.append(command)
            has_command = True
            expecting_command = False
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
        for_session: bool = False,
    ) -> str:
        """Build a script, applying environment and cwd consistently."""
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
        script = self.command_separator.join(parts)
        epilogue = self.execution_epilogue
        if script and epilogue and not for_session:
            script += epilogue
        return script

    @abc.abstractmethod
    def change_directory(self, cwd: PathLike) -> str:
        """Render a directory change which fails if the directory is absent."""

    def group(self, command: str) -> str:
        """Wrap `command` so an operator applies to all of it, not its head."""
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
    ) -> ShellCommand:
        """Build the command submitted to an SSH exec channel.

        The returned string is read by the **remote login shell**, so each
        flavour must say which parser it escaped for:

        * POSIX and fish quote with `shlex`, for a POSIX login shell.
        * PowerShell renders `-EncodedCommand <base64 utf-16le>`, which is
          inert under cmd.exe (Windows OpenSSH's default shell), PowerShell
          and a POSIX shell alike.
        * `cmd` escapes exactly one cmd layer, which makes it a command LINE
          for direct submission rather than text for a remote login shell --
          see the divergence ledger in `docs/guide/contracts.md`.
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


class Shell(Executor[_Result], typing.Generic[_Result]):
    """Bind one shell language to a one-string callable or host executor."""

    def __init__(
        self,
        flavour: ShellFlavour,
        executor: typing.Union[Executor[_Result], Host],
        *,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> None:
        self.flavour = flavour
        #: Defaults applied to every `run`, `execute`, and `session` call that
        #: does not pass its own value.  `cwd`, `encoding`, and `errors`
        #: override wholesale; `env` merges per key so a call can change one
        #: variable without restating the rest (see `_resolve_env`).
        self.cwd = cwd
        # `dict(env) if env is not None` kept an empty mapping, which the
        # local executor forwards to `subprocess` as `env={}` -- a child
        # started with NO environment at all, where the caller meant "no
        # overrides". `configure()` already normalised it this way.
        self.env = dict(env) if env else None
        self.encoding = encoding
        self.errors = errors
        run = getattr(executor, "run", None)
        spawn = getattr(executor, "spawn", None)
        self._spawn = spawn if callable(spawn) else None
        self._session: typing.Optional[ShellSession] = None
        if callable(executor):
            self._execute = executor
        elif callable(run):
            self._execute = run
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
        # A host `run(*cmds)` takes several *commands*; an executor
        # `__call__(command, *args)` takes one command and its argv. Only the
        # former needs the program and argv bundled into one `Exec`, since
        # otherwise each argument would become a separate command. The shapes
        # differ in their first parameter: VAR_POSITIONAL vs a named one.
        positional = [
            item
            for item in self._executor_parameters.values()
            if item.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            )
        ]
        self._execute_takes_commands = bool(positional) and (
            positional[0].kind is inspect.Parameter.VAR_POSITIONAL
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
        # Its members subclass `str`, so a set published as enum members by a
        # raw `Executor` and one published as plain strings by a `Host` (via
        # `ExecutorProvider`) compare and hash identically.  No conversion
        # happens here, and none is needed at any other boundary.
        self.executor_capabilities = frozenset(published)
        self._executor_accepts_cwd = (
            ExecutorCapability.CWD in self.executor_capabilities
        )
        self._executor_accepts_env = (
            ExecutorCapability.ENV in self.executor_capabilities
        )

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
        if self.env is None:
            return dict(env) if env else None
        merged = dict(self.env)
        merged.update(env)
        return merged

    def _resolve_cwd(self, cwd: typing.Optional[PathLike]) -> typing.Optional[PathLike]:
        return self.cwd if cwd is None else cwd

    def execute(
        self,
        command: ExecutorCommand,
        *args: CommandArgument,
        stdin: typing.Optional[FileHandle] = None,
        stdout: typing.Optional[FileHandle] = None,
        stderr: typing.Optional[FileHandle] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        capture_output: typing.Optional[CaptureOutput] = None,
        check: typing.Optional[bool] = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: Input = None,
        timeout: typing.Optional[float] = None,
        text: typing.Optional[bool] = None,
    ) -> _Result:
        """Execute one command and forward supported execution context."""
        executor_args = args
        if args and ExecutorCapability.ARGS not in self.executor_capabilities:
            command = self.flavour.structured_command((command, *args))
            executor_args = ()
        # Apply the shell's cwd/env defaults only where the executor can carry
        # them natively. Where it cannot, the caller renders them into the
        # script instead -- `run` does exactly that and then passes
        # cwd=None/env=None here, so resolving again would forward a value this
        # executor rejects. `execute` used directly against a capability-less
        # executor therefore ignores the defaults by design: it dispatches one
        # opaque command rather than building a script.
        if self._executor_accepts_cwd:
            cwd = self._resolve_cwd(cwd)
        if self._executor_accepts_env:
            env = self._resolve_env(env)
        if encoding is None:
            encoding = self.encoding
        if errors is None:
            errors = self.errors
        options = {
            name: value
            for name, value in (
                ("stdin", stdin),
                ("stdout", stdout),
                ("stderr", stderr),
                ("capture_output", capture_output),
                ("check", check),
                ("encoding", encoding),
                ("errors", errors),
                ("input", input),
                ("timeout", timeout),
                ("text", text),
            )
            if value is not None
        }
        if cwd is not None:
            if not self._executor_accepts_cwd:
                raise TypeError("executor does not accept cwd")
            options["cwd"] = cwd
        if env is not None:
            if not self._executor_accepts_env:
                raise TypeError("executor does not accept env")
            options["env"] = env
        unsupported = [
            name
            for name in options
            if not self._accepts_keyword(name) and not self._executor_accepts_options
        ]
        if unsupported:
            raise TypeError(f"executor does not accept {sorted(unsupported)[0]}")
        if executor_args and self._execute_takes_commands:
            # A host `run(*cmds)` treats several positionals as several
            # commands, so a program and its argv must arrive as one `Exec`
            # rather than relying on position to mark direct execution.
            # Only when there *are* argv values: `Shell.run` renders every
            # command into one script and dispatches it here with no args, and
            # that script is shell text to interpret, not a program to exec.
            return self._execute(Exec(command, *executor_args), **options)
        return self._execute(command, *executor_args, **options)

    __call__ = execute

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
        """Build one script from all commands and pass it to the executor.

        `env` merges over the shell's default per key; the default merges
        nothing and so inherits it. Pass `None` to run without the shell's
        configured environment, keeping whatever the host provides itself.
        """
        # Resolve defaults here rather than in `execute`: the script is
        # rendered before dispatch, so an embedded `cd`/env assignment has to
        # see the shell's defaults too.
        cwd = self._resolve_cwd(cwd)
        env = self._resolve_env(env)
        script = self.flavour.script(
            cmds,
            cwd=None if self._executor_accepts_cwd else cwd,
            env=None if self._executor_accepts_env else env,
        )
        return self.execute(
            script,
            cwd=cwd if self._executor_accepts_cwd else None,
            env=env if self._executor_accepts_env else None,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            capture_output=capture_output,
            check=check,
            encoding=encoding,
            errors=errors,
            input=input,
            timeout=timeout,
            text=text,
        )

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
            self.flavour,
            self._spawn(
                executable=executable,
                terminal=terminal,
                encoding=self.encoding if encoding is None else encoding,
                errors=self.errors if errors is None else errors,
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
        shell is left unchanged, which keeps `host.shell` -- a fresh object
        per access -- safe to configure without surprising another caller.
        """
        clone = copy.copy(self)
        clone._session = None
        clone.cwd = self.cwd if cwd is None else cwd
        clone.env = self._resolve_env(env)
        clone.encoding = self.encoding if encoding is None else encoding
        clone.errors = self.errors if errors is None else errors
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
