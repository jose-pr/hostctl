"""The operational host contract and `Host(uri)` dispatch."""

from __future__ import annotations

import abc as _abc


import inspect as _inspect

import subprocess as _subprocess


import typing as _ty

import types as _types


from pathapi import Path as HostPath

from ..executor import CaptureOutput, Environment, FileHandle, Input, PathLike

# Runtime imports: `typing.get_type_hints` resolves the public annotations
# against this module's globals. `executor`, `process` and `shell` never
# import `host`, so there is no cycle.
from ..executor import Executor

from ..process import Process, TerminalRequest

from ..shell._shell import Shell
from ..shell._flavour import ShellFlavour

from ..shell._grammar import Command as Command, Exec as Exec


from ._info import HostInfo
from ._dispatch import _T, _abstract, _constructor_signature


class _HostMeta(_abc.ABCMeta):
    def __call__(cls: _ty.Type[_T], *args: _ty.Any, **options: _ty.Any) -> _T:
        if cls is Host:
            if len(args) != 1 or not isinstance(args[0], str):
                raise TypeError(
                    "Host() requires one connection string positional argument"
                )
            config = HostConfig._from_uri(args[0], **options)
            return _ty.cast(_T, config._create_host())
        return _ty.cast(_T, type.__call__(cls, *args, **options))

    @property
    def __signature__(cls) -> _inspect.Signature:
        return _constructor_signature(cls, Host)


class _HostExecutor:
    """A host presented as an `Executor`: calls go through `host.run()`, so the
    host's own rendering and failover apply.

    A bare `ExecutorProvider` declares its capabilities but has no
    `executor_capabilities`, so a `Shell` built on one would infer CWD/ENV
    support from the callable's signature -- and infer it wrongly. This carries
    the declared set, and the provider's name and attributes when one is given.
    """

    def __init__(self, host, capabilities, provider=None):
        self._host = host
        self._provider = provider
        self.executor_capabilities = capabilities
        if provider is not None:
            self.name = provider.name

    def __call__(self, command, *args, **options):
        # `Executor.__call__(command, *args)` is one program plus argv.
        # Forwarding the argv as further positionals made `host.run()` read
        # them as SEPARATE top-level commands, so `executor("grep", "-r",
        # "needle")` ran `grep; -r; needle`.
        if args:
            return self._host.run(Exec(command, *args), **options)
        return self._host.run(command, **options)

    def __getattr__(self, attribute):
        provider = self.__dict__.get("_provider")
        if provider is None:
            raise AttributeError(attribute)
        return getattr(provider, attribute)


class Host(_abc.ABC, metaclass=_HostMeta):
    """Protocol-independent operational interface to a machine."""

    config: HostConfig

    # The second overload is the dispatch form `Host(uri, **secrets)`, which
    # the metaclass serves; a subclass calls `super().__init__()`.
    @_ty.overload
    def __init__(self) -> None: ...

    @_ty.overload
    def __init__(self, connection_string: str, /, **secrets: str) -> None: ...

    def __init__(self, *args: object, **secrets: object) -> None:
        super().__init__(*args, **secrets)

    @property
    def scheme(self) -> str:
        return self.config.scheme

    @property
    def connection_uri(self) -> str:
        return self.config.connection_uri

    @property
    def shell_flavour(self) -> ShellFlavour:
        """The explicitly known shell language used by this host."""
        raise NotImplementedError(
            f"{type(self).__name__} does not identify a shell flavour"
        )

    @property
    def executor(self) -> Executor[_subprocess.CompletedProcess]:
        """The command executor used when binding this host's shell."""
        return _HostExecutor(self, self.executor_capabilities)

    @property
    def executor_capabilities(self) -> _ty.FrozenSet[str]:
        """Native context/argument features of the underlying executor.

        Derived from the host's own executor so a host that does not compose
        providers still reports truthfully. `Host.executor`'s default wrapper
        reads this property, so it is skipped here to avoid recursing; a host
        that overrides `executor` with a real executor reports that executor's
        capabilities.
        """
        executor = type(self).executor
        if executor is Host.executor:
            return frozenset()
        return frozenset(getattr(self.executor, "executor_capabilities", ()))

    @property
    def shell(self) -> Shell[_subprocess.CompletedProcess]:
        """A shell bound to this host: `host.shell.run(...)` is `host.run(...)`.

        `host.shell.session(...)` and `with host.shell as session:` open a
        persistent session. Called with keywords --
        `host.shell(cwd="/srv/app", env={"TZ": "UTC"})` -- it returns a shell
        carrying those defaults, applied to every later `run` and `session`
        that does not pass its own value. `env` merges per key; `cwd`,
        `encoding`, and `errors` override. A shell's attributes are read-only.
        """
        from ..shell._shell import Shell

        return Shell(self.shell_flavour, self)

    def _run_selector(self) -> _ty.Optional[object]:
        """The :class:`~hostctl.provider.ProviderSelector` backing :meth:`run`.

        ``None`` for a host that does not select a command provider at all.
        The default finds the attribute used by the provider-composed system
        hosts; assemblies that name theirs differently override this.
        """
        return getattr(self, "_executor_selector", None)

    @property
    def last_selection(self) -> _ty.Tuple[_ty.Dict[str, object], ...]:
        """The redacted provider trace for the most recent :meth:`run`.

        The run-side counterpart of ``CompositePosixPath.selection_trace``.
        Entries appear in provider precedence order and carry ``provider``,
        ``availability``, ``reason``, ``capabilities``, ``chosen``,
        ``generation``, ``policy``, and ``pin``.

        The trace accumulates across the failover attempts of a single
        ``run()``, so a call that fell through from one provider to another
        reports every provider tried and every refusal reason -- including on
        the very call that suffered them.

        Empty for a host whose ``run()`` does not select between providers
        (``QEMUHost``, ``SerialHost``), and empty before the first ``run()``.
        Values are redacted on the way in; see
        :meth:`~hostctl.provider.ProviderSelector.redact` for the limits of
        that guarantee.
        """
        selector = self._run_selector()
        if selector is None:
            return ()
        return getattr(selector, "trace", ())

    @property
    @_abstract
    def capabilities(self) -> _ty.FrozenSet[str]:
        """Operations supported by this host."""
        raise NotImplementedError

    @_abstract
    def info(self) -> HostInfo:
        """Return normalized system information without inferred values."""
        raise NotImplementedError

    def connect(self) -> None:
        """Open transport resources; local/default implementations are no-op."""

    def close(self) -> None:
        """Close transport resources; must be safe to call repeatedly."""

    def __enter__(self) -> Host:
        try:
            self.connect()
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise
        return self

    def __exit__(
        self,
        exc_type: _ty.Optional[_ty.Type[BaseException]],
        exc_value: _ty.Optional[BaseException],
        traceback: _ty.Optional[_types.TracebackType],
    ) -> bool:
        self.close()
        return False

    def path(self, *segments: PathLike, backend: _ty.Optional[str] = None) -> HostPath:
        """Return a pathlib-compatible path for this host."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide the 'path' capability"
        )

    def spawn(
        self,
        *cmds: Command,
        executable: _ty.Optional[str] = None,
        cwd: _ty.Optional[PathLike] = None,
        env: _ty.Optional[Environment] = None,
        terminal: TerminalRequest = None,
        encoding: _ty.Optional[str] = None,
        errors: _ty.Optional[str] = None,
    ) -> Process:
        """Start a persistent process controlled through a synchronous facade."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide the 'spawn' capability"
        )

    def run(
        self,
        *cmds: Command,
        bufsize: int = -1,
        executable: _ty.Optional[str] = None,
        stdin: _ty.Optional[FileHandle] = None,
        stdout: _ty.Optional[FileHandle] = None,
        stderr: _ty.Optional[FileHandle] = None,
        cwd: _ty.Optional[PathLike] = None,
        env: _ty.Optional[Environment] = None,
        capture_output: CaptureOutput = True,
        check: bool = True,
        encoding: _ty.Optional[str] = None,
        errors: _ty.Optional[str] = None,
        input: Input = None,
        timeout: _ty.Optional[float] = None,
        text: _ty.Optional[bool] = None,
    ) -> _subprocess.CompletedProcess:
        """Run commands and return a subprocess-compatible result.

        A string is verbatim shell text.  A tuple/list is one quoted argv
        command.  :class:`Exec` is the only direct-execution spelling: it runs
        one program with an argv and no shell layer, and it cannot be combined
        with other commands.  A path anywhere else is an ordinary value that
        stringifies.  Multiple top-level commands are joined by the selected
        shell's command separator.

        Two defaults differ from :func:`subprocess.run` deliberately, and
        both are the opposite of it: ``check`` is ``True``, so a non-zero
        status raises :class:`subprocess.CalledProcessError` unless the
        caller passes ``check=False``; and ``capture_output`` is ``True``, so
        output is captured rather than inherited by a process that may have
        no console.  The result type and every other keyword do follow
        ``subprocess``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not provide the 'run' capability"
        )


# Bound last, so either module may be imported first: `_config` binds
# `Host` the same way.
from ._config import HostConfig  # noqa: E402
