"""Protocol-independent host contracts and shared implementation helpers."""

from __future__ import annotations

import abc as _abc
import dataclasses as _dc
import importlib.metadata as _metadata
import subprocess as _subprocess
import threading as _threading
import typing as _ty
import types as _types
from pathlib import PurePath as _PurePath
from urllib.parse import (
    SplitResult as _SplitResult,
    parse_qsl as _parse_qsl,
    quote as _quote,
    unquote as _unquote,
    urlsplit as _urlsplit,
)

from pathlib_next import Path as HostPath, Pathname as _Pathname

from ..executor import (
    CaptureOutput,
    Environment,
    FileHandle,
    Input,
    PathLike,
    capture_streams,
    reject_stdin_conflict,
)

if _ty.TYPE_CHECKING:
    from ..executor import Executor, ExecutorCapability
    from ..process import Process, TerminalRequest
    from ..shell import Shell, ShellFlavour

Command = _ty.Union[str, PathLike, _ty.Sequence[object]]


def starts_direct_command(
    cmds: _ty.Sequence[Command],
) -> _ty.Optional[_ty.Tuple[_ty.Union[_PurePath, _Pathname], _ty.Tuple[object, ...]]]:
    """Split a path-led call into one executable and its argv arguments.

    A leading path object is the explicit direct-execution marker.  Every
    trailing value is an argv scalar; nested command sequences are rejected so
    callers cannot accidentally mix shell-command and argv semantics.
    """
    if not cmds or not isinstance(cmds[0], (_PurePath, _Pathname)):
        return None
    command = cmds[0]
    argv = tuple(cmds[1:])
    for value in argv:
        if isinstance(value, (tuple, list)):
            raise TypeError("direct command arguments must be scalar values")
        if not isinstance(value, (str, bytes, _PurePath, _Pathname)):
            raise TypeError(
                "direct command arguments must be str, bytes, or path values"
            )
    return command, argv


@_dc.dataclass(frozen=True)
class HostInfo:
    """Normalized system information; unavailable values remain ``None``."""

    hostname: _ty.Optional[str] = None
    os_family: _ty.Optional[str] = None
    os_name: _ty.Optional[str] = None
    os_version: _ty.Optional[str] = None
    architecture: _ty.Optional[str] = None


def parse_host_info(output: _ty.Union[bytes, str, None]) -> HostInfo:
    """Parse newline-delimited ``HostInfo`` fields from a transport response."""
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    values = {}
    for line in (output or "").splitlines():
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if separator and key in HostInfo.__dataclass_fields__ and value:
            values[key] = value
    if "os_family" in values:
        values["os_family"] = normalize_os_family(values["os_family"])
    return HostInfo(**values)


def normalize_os_family(value: _ty.Optional[str]) -> _ty.Optional[str]:
    """Normalize a directly reported OS family without inferring one."""
    if not value:
        return None
    normalized = value.casefold()
    aliases = {
        "win32nt": "windows",
        "windows": "windows",
        "linux": "linux",
        "darwin": "macos",
        "macos": "macos",
    }
    return aliases.get(normalized, normalized)


def uri_host(host: str) -> str:
    """Bracket an IPv6 literal for use in a URI authority."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


#: Stands in for a password wherever a URI is rendered for a human.
REDACTED = "***"


def _rebuild_authority(parsed: _SplitResult, password: _ty.Optional[str]) -> str:
    """Rebuild a URI authority with `password` in place of the original."""
    host = uri_host(parsed.hostname or "")
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    if not parsed.username:
        return host
    userinfo = _quote(parsed.username, safe="")
    if password is not None:
        userinfo = f"{userinfo}:{password}"
    return f"{userinfo}@{host}"


def _redacted_authority(parsed: _SplitResult, keep_password: bool) -> _SplitResult:
    """Return `parsed` with its password removed or replaced by `REDACTED`."""
    return parsed._replace(
        netloc=_rebuild_authority(parsed, REDACTED if keep_password else None)
    )


def redact_uri(uri: str) -> str:
    """Replace any password in `uri` with `REDACTED`, leaving it parseable.

    Use this wherever a connection string reaches a human -- an error message,
    a log record, a repr. `scheme://user:secret@host` renders as
    `scheme://user:***@host`; a URI with no password is returned unchanged.
    """
    parsed = _urlsplit(uri)
    if parsed.password is None:
        return uri
    return _redacted_authority(parsed, keep_password=True).geturl()


class _HostConfigMeta(_abc.ABCMeta):
    def __call__(cls, *args: object, **options: object) -> HostConfig:
        if cls is HostConfig:
            if len(args) != 1 or not isinstance(args[0], str):
                raise TypeError(
                    "HostConfig() requires one connection string positional argument"
                )
            return cls._from_uri(args[0], **options)
        return super().__call__(*args, **options)


class HostConfig(_abc.ABC, metaclass=_HostConfigMeta):
    """Secret-safe connection configuration and extensible URI dispatch."""

    _uri_schemes: _ty.ClassVar[_ty.Tuple[str, ...]] = ()
    _uri_registry_cache: _ty.ClassVar[
        _ty.Optional[_ty.Tuple[_ty.Type["HostConfig"], ...]]
    ] = None
    _uri_entry_points: _ty.ClassVar[_ty.Optional[tuple[object, ...]]] = None
    _uri_plugin_failures: _ty.ClassVar[dict[str, Exception]] = {}
    _uri_registry_generation: _ty.ClassVar[int] = 0
    _uri_registry_lock: _ty.ClassVar[_threading.RLock] = _threading.RLock()

    def __init__(self) -> None:
        self._opened_host: _ty.Optional[Host] = None
        self._lifecycle_lock = _threading.Lock()

    def __init_subclass__(
        cls,
        *,
        schemes: _ty.Iterable[str] = (),
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        cls._uri_schemes = tuple(scheme.casefold() for scheme in schemes)
        HostConfig._refresh_uri_registry()

    @property
    def scheme(self) -> str:
        return _urlsplit(self.connection_uri).scheme.casefold()

    def __str__(self) -> str:
        return self.connection_uri

    @property
    @_abc.abstractmethod
    def connection_uri(self) -> str:
        """Credential-safe canonical connection URI."""

    @_abc.abstractmethod
    def _create_host(self) -> Host:
        """Create the operational host represented by this configuration."""

    def open(self) -> Host:
        """Return a host context manager for this configuration."""
        return self._create_host()

    def __enter__(self) -> Host:
        with self._lifecycle_lock:
            if self._opened_host is not None:
                raise RuntimeError("host configuration is already open")
            host = self._create_host()
            self._opened_host = host
        try:
            return host.__enter__()
        except BaseException:
            try:
                host.close()
            except BaseException:
                pass
            finally:
                with self._lifecycle_lock:
                    if self._opened_host is host:
                        self._opened_host = None
            raise

    def __exit__(
        self,
        exc_type: _ty.Optional[_ty.Type[BaseException]],
        exc_value: _ty.Optional[BaseException],
        traceback: _ty.Optional[_types.TracebackType],
    ) -> _ty.Optional[bool]:
        with self._lifecycle_lock:
            host, self._opened_host = self._opened_host, None
        if host is not None:
            return host.__exit__(exc_type, exc_value, traceback)
        return False

    @classmethod
    def _from_uri(
        cls,
        uri: str,
        **credentials: object,
    ) -> HostConfig:
        """Dispatch a connection URI to a registered configuration.

        A `scheme://user:secret@host` URI is accepted: the password is
        extracted into the credential arguments and stripped from the parsed
        authority, so it is never stored where `connection_uri` or `repr()`
        would render it. Use :func:`redact_uri` before showing a URI that may
        still carry one.
        """
        parsed = _urlsplit(uri)
        if parsed.fragment:
            raise ValueError("connection URI fragments are not supported")
        if parsed.password is not None:
            # `scheme://user:secret@host` is a valid URI, so accept it: extract
            # the password into the credential arguments and strip it from the
            # parsed authority. The password therefore never reaches a config
            # field that `connection_uri`/`repr` render, which is what keeps
            # the canonical form credential-free.
            if credentials.get("password") is not None:
                raise ValueError(
                    "password given both in the connection URI and as an argument"
                )
            credentials["password"] = _unquote(parsed.password)
            parsed = _redacted_authority(parsed, keep_password=False)
        matches = [
            implementation
            for implementation in cls._uri_implementations(parsed.scheme)
            if implementation._matches_uri(parsed)
        ]
        if not matches:
            raise ValueError(
                f"unsupported host scheme: {parsed.scheme.casefold() or '<missing>'}"
            )
        if len(matches) > 1:
            names = ", ".join(item.__name__ for item in matches)
            raise ValueError(f"ambiguous host URI matched: {names}")
        return matches[0]._from_parsed_uri(parsed, **credentials)

    @classmethod
    def _matches_uri(cls, parsed: _SplitResult) -> bool:
        """Whether this implementation accepts a parsed URI."""
        return parsed.scheme.casefold() in cls._uri_schemes

    @classmethod
    def _from_parsed_uri(
        cls, parsed: _SplitResult, **credentials: object
    ) -> HostConfig:
        """Construct from a URI selected by :meth:`_matches_uri`."""
        raise NotImplementedError(f"{cls.__name__} does not implement URI construction")

    @classmethod
    def _refresh_uri_registry(cls) -> None:
        """Clear URI implementation discovery for tests and newly loaded plugins."""
        with HostConfig._uri_registry_lock:
            HostConfig._uri_registry_generation += 1
            HostConfig._uri_registry_cache = None
            HostConfig._uri_entry_points = None
            HostConfig._uri_plugin_failures = {}

    @classmethod
    def _uri_implementations(
        cls,
        requested_scheme: _ty.Optional[str] = None,
    ) -> _ty.Tuple[_ty.Type[HostConfig], ...]:
        scheme = requested_scheme.casefold() if requested_scheme else None
        with HostConfig._uri_registry_lock:
            if HostConfig._uri_registry_cache is None:
                from . import (
                    container as _container,
                    _local,
                    qemu as _qemu,
                    serial as _serial,
                    _ssh,
                    _winrm,
                )

                del _container, _local, _qemu, _serial, _ssh, _winrm
                while HostConfig._uri_registry_cache is None:
                    generation = HostConfig._uri_registry_generation
                    discovered = list(_recursive_subclasses(HostConfig))
                    if generation != HostConfig._uri_registry_generation:
                        continue
                    HostConfig._uri_registry_cache = tuple(
                        item for item in discovered if item._uri_schemes
                    )
            if HostConfig._uri_entry_points is None:
                points = _metadata.entry_points()
                if hasattr(points, "select"):
                    points = points.select(group="hostctl.configs")
                elif hasattr(points, "get"):
                    points = points.get("hostctl.configs", ())
                else:
                    points = tuple(
                        item
                        for item in points
                        if getattr(item, "group", None) == "hostctl.configs"
                    )
                HostConfig._uri_entry_points = tuple(points)
            candidates = list(HostConfig._uri_registry_cache)
            if scheme is not None:
                for entry_point in HostConfig._uri_entry_points:
                    name = str(getattr(entry_point, "name", "")).casefold()
                    if name != scheme:
                        continue
                    failure = HostConfig._uri_plugin_failures.get(name)
                    if failure is not None:
                        raise failure
                    try:
                        implementation = entry_point.load()
                        if not isinstance(implementation, type) or not issubclass(
                            implementation, HostConfig
                        ):
                            raise TypeError(
                                f"hostctl.configs entry point {name!r} "
                                "must load a HostConfig subclass"
                            )
                    except Exception as exc:
                        HostConfig._uri_plugin_failures[name] = exc
                        import warnings

                        warnings.warn(
                            f"unable to load hostctl.configs entry point {name!r}: {exc}",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                        raise
                    candidates.append(implementation)
                    break
            return tuple(
                item
                for item in dict.fromkeys(candidates)
                if issubclass(item, cls) and item._uri_schemes
            )


class _HostMeta(_abc.ABCMeta):
    def __call__(cls, *args: object, **options: object) -> Host:
        if cls is Host:
            if len(args) != 1 or not isinstance(args[0], str):
                raise TypeError(
                    "Host() requires one connection string positional argument"
                )
            config = HostConfig._from_uri(args[0], **options)
            return config._create_host()
        return super().__call__(*args, **options)


class _ShellAccessor:
    """Expose `host.shell` as both the shell itself and a configuring call.

    `host.shell` must keep working as the bound `Shell` -- `host.shell.run()`,
    `host.shell.session()`, `with host.shell as session:` all predate this.
    `host.shell(cwd=...)` must additionally return a shell carrying defaults.

    A plain property cannot do both, and overloading `Shell.__call__` is not an
    option: that is the `Executor` protocol's execute entry point, so making it
    mean "configure" when called without a command would make every executor
    call site ambiguous. This descriptor instead returns the built shell for
    attribute access and, because a `Shell` is itself callable, routes a
    keyword-only call through `Shell.configure`.
    """

    def __init__(self, build):
        self._build = build
        self.__doc__ = build.__doc__

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return _ConfigurableShell(self._build(instance))


class _ConfigurableShell:
    """A `Shell` proxy whose keyword-only call returns a configured shell."""

    __slots__ = ("_shell",)

    def __init__(self, shell):
        object.__setattr__(self, "_shell", shell)

    def __call__(self, *args, **options):
        if args:
            # A positional argument means the caller is using the `Executor`
            # protocol (`shell(command, ...)`); defer to the real shell.
            return self._shell(*args, **options)
        return self._shell.configure(**options)

    def __getattr__(self, name):
        return getattr(self._shell, name)

    def __setattr__(self, name, value):
        setattr(self._shell, name, value)

    def __enter__(self):
        return self._shell.__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        return self._shell.__exit__(exc_type, exc_value, traceback)

    def __repr__(self):
        return repr(self._shell)


class Host(_abc.ABC, metaclass=_HostMeta):
    """Protocol-independent operational interface to a machine."""

    config: HostConfig

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
        host = self

        class _HostExecutor:
            executor_capabilities = host.executor_capabilities

            def __call__(self, command, *args, **options):
                return host.run(command, *args, **options)

        return _HostExecutor()

    @property
    def executor_capabilities(self) -> _ty.FrozenSet[ExecutorCapability]:
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

    @_ShellAccessor
    def shell(self) -> Shell[_subprocess.CompletedProcess]:
        """A shell bound to this host's executor.

        Used directly -- `host.shell.run(...)`, `host.shell.session(...)`, or
        `with host.shell as session:` -- it carries no defaults.

        Called with keywords -- `host.shell(cwd="/srv/app", env={"TZ": "UTC"})`
        -- it returns a shell carrying those defaults, applied to every later
        `run`, `execute`, and `session` that does not pass its own value.
        `env` merges per key; `cwd`, `encoding`, and `errors` override.
        """
        from ..shell import Shell

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
        (``QemuHost``, ``SerialHost``), and empty before the first ``run()``.
        Values are redacted on the way in; see
        :meth:`~hostctl.provider.ProviderSelector.redact` for the limits of
        that guarantee.
        """
        selector = self._run_selector()
        if selector is None:
            return ()
        return getattr(selector, "trace", ())

    @property
    @_abc.abstractmethod
    def capabilities(self) -> _ty.FrozenSet[str]:
        """Operations supported by this host."""

    @_abc.abstractmethod
    def info(self) -> HostInfo:
        """Return normalized system information without inferred values."""

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
        command.  A leading :class:`pathlib.PurePath`/``pathlib_next`` path is
        a direct executable and all trailing values are its argv arguments.
        Otherwise multiple top-level commands are joined by the selected
        shell's command separator.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not provide the 'run' capability"
        )


def strict_uri_query(
    parsed: _SplitResult, allowed: _ty.Iterable[str]
) -> _ty.Dict[str, str]:
    """Parse one selected implementation's query without ambiguity."""
    query = {}
    for key, value in _parse_qsl(parsed.query, keep_blank_values=True):
        if key in query:
            raise ValueError(f"duplicate connection parameter: {key}")
        query[key] = value
    unknown = set(query) - set(allowed)
    if unknown:
        raise ValueError(f"unknown connection parameter: {sorted(unknown)[0]}")
    return query


def strict_uri_credentials(
    credentials: _ty.Mapping[str, object], allowed: _ty.Iterable[str]
) -> None:
    """Reject credentials which the selected implementation does not accept."""
    unknown = set(credentials) - set(allowed)
    if unknown:
        raise ValueError(f"unknown credential argument: {sorted(unknown)[0]}")


def _query_int(query: _ty.Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(query.get(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _recursive_subclasses(
    base: _ty.Type[HostConfig],
) -> _ty.Iterator[_ty.Type[HostConfig]]:
    for subclass in base.__subclasses__():
        yield subclass
        yield from _recursive_subclasses(subclass)
