"""Target-shell command construction independent of the host transport."""

from __future__ import annotations

import abc
import collections.abc
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
from ..host._common import (
    CaptureOutput,
    Command,
    Environment,
    FileHandle,
    Input,
    PathLike,
)
from ..process import Process, TerminalRequest

_Result = typing.TypeVar("_Result", covariant=True)
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

if typing.TYPE_CHECKING:
    from ..host._common import Host


class ShellOperator(enum.Enum):
    PIPE = "pipe"
    AND = "and"
    OR = "or"
    REDIRECT = "redirect"
    APPEND = "append"
    SEQUENCE = "sequence"


ShellToken = typing.Union[Command, ShellOperator]


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
            script + self.flavour.command_separator + self.flavour.line_terminator
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


class ShellFlavour(abc.ABC):
    """Construct scripts and commands for one explicitly selected target shell."""

    name: str
    default_executable: str
    info_script: str
    command_separator: str
    line_terminator: str = "\n"
    context_order = ("env", "cwd", "command")
    structured_command_prefix = ""
    path_flavor: type[PurePath] = PurePath

    def command_path(self, value: PathLike) -> PurePath:
        """Return a direct-command marker using the target shell's path syntax."""
        return self.path_flavor(value)

    @staticmethod
    def _text(value: object) -> str:
        """Normalize values and reject shell control characters."""
        if isinstance(value, os.PathLike):
            value = os.fspath(value)
        elif isinstance(value, bytes):
            value = value.decode("utf-8", "surrogateescape")
        text = str(value)
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise ValueError("shell values cannot contain control characters")
        return text

    @abc.abstractmethod
    def quote(self, value: object) -> str:
        """Quote one structured argument for this shell."""

    @abc.abstractmethod
    def operator(self, value: ShellOperator) -> str:
        """Render one supported command operator."""

    def structured_command(self, values: typing.Iterable[object]) -> str:
        return self.structured_command_prefix + " ".join(
            self.quote(value) for value in values
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
        if isinstance(value, (bytes, PurePath, Path, os.PathLike)):
            return self.quote(value)
        if isinstance(value, str):
            return self._text(value)
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
        if (
            "cwd" in self.context_order
            and "command" in self.context_order
            and cwd is not None
            and command
        ):
            cwd_index = self.context_order.index("cwd")
            command_index = self.context_order.index("command")
            if command_index == cwd_index + 1:
                rendered["cwd"] = self.join_cwd(changed, command)
                rendered["command"] = ""
        parts = [rendered[name] for name in self.context_order if rendered[name]]
        script = self.command_separator.join(parts)
        epilogue = getattr(self, "execution_epilogue", "")
        if script and epilogue and not for_session:
            script += epilogue
        return script

    @abc.abstractmethod
    def change_directory(self, cwd: PathLike) -> str:
        """Render a directory change which fails if the directory is absent."""

    def join_cwd(self, changed: str, command: str) -> str:
        """Join cwd setup and payload with the shell's AND operator."""
        return f"{changed}{self.operator(ShellOperator.AND)}{command}"

    @abc.abstractmethod
    def command(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> ShellCommand:
        """Build the command submitted to an SSH exec channel."""

    @abc.abstractmethod
    def invocation(
        self,
        script: str,
        *,
        executable: typing.Optional[str] = None,
    ) -> typing.Sequence[str]:
        """Build local-process argv which invokes this shell for one script."""

    def __str__(self) -> str:
        return self.name


class Shell(Executor[_Result], typing.Generic[_Result]):
    """Bind one shell language to a one-string callable or host executor."""

    def __init__(
        self,
        flavour: ShellFlavour,
        executor: typing.Union[Executor[_Result], Host],
    ) -> None:
        self.flavour = flavour
        run = getattr(executor, "run", None)
        spawn = getattr(executor, "spawn", None)
        self._spawn = spawn if callable(spawn) else None
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
        return self._execute(command, *executor_args, **options)

    __call__ = execute

    def run(
        self,
        *cmds: ShellToken,
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
        """Build one script from all commands and pass it to the executor."""
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
        env: typing.Optional[Environment] = None,
        terminal: TerminalRequest = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> ShellSession:
        """Open a persistent process using this shell language."""
        if self._spawn is None:
            raise NotImplementedError("executor does not provide persistent sessions")
        session = ShellSession(
            self.flavour,
            self._spawn(
                executable=executable,
                terminal=terminal,
                encoding=encoding,
                errors=errors,
            ),
        )
        if cmds or cwd is not None or env:
            session.send(
                *cmds,
                cwd=cwd,
                env=env,
            )
        return session
