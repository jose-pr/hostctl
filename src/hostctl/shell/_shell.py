"""`Shell` and `ShellSession`: configured shells over an executor."""

from __future__ import annotations


import copy
import inspect
import typing
import types


from ..executor import (
    ExecutorCapability,
    masked_environment,
    run_displayed,
    CaptureOutput,
    Environment,
    FileHandle,
    Input,
    PathLike,
)
from ..process import Process, TerminalRequest
from ._vocabulary import (
    EnvironmentSelection,
    ShellToken,
    _CommandHost,
    _INHERIT_ENV,
    _Result,
    wants_epilogue,
)
from ._flavour import ShellFlavour


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
