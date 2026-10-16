"""Protocol-independent host contracts and shared implementation helpers."""

from __future__ import annotations

import abc as _abc

import dataclasses as _dc

import inspect as _inspect


import subprocess as _subprocess

import threading as _threading

import typing as _ty


import types as _types

from urllib.parse import (
    SplitResult as _SplitResult,
    unquote as _unquote,
    urlsplit as _urlsplit,
)

from pathapi import Path as HostPath


from ..executor import CaptureOutput, Environment, FileHandle, Input, PathLike

# Runtime imports: `typing.get_type_hints` resolves the public annotations
# against this module's globals. `executor`, `process` and `shell` never
# import `host`, so there is no cycle.
from ..executor import Executor

from ..process import Process, TerminalRequest

from ..shell._flavour import Shell, ShellFlavour

from ..shell._grammar import Command as Command, Exec as Exec

from ..uri._parse import (
    _encode_stripped_characters,
    _refuse_cut_short_userinfo,
    _reject_authority_control_characters,
    _without_password,
    parse_credentials,
    strict_uri_credentials,
)
from .._registry import _recursive_subclasses, entry_points_in


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


_T = _ty.TypeVar("_T")


if _ty.TYPE_CHECKING:
    # A checker that sees `HostConfig`/`Host` as abstract rejects the
    # dispatching `HostConfig(uri)` call; the abstract-method check is the
    # runtime's, which refuses to build an incomplete subclass.
    def _abstract(function: _T) -> _T:
        return function

else:
    _abstract = _abc.abstractmethod


#: What `HostConfig(...)` and `Host(...)` themselves accept.
_DISPATCH_SIGNATURE = _inspect.Signature(
    [
        _inspect.Parameter(
            "connection_string",
            _inspect.Parameter.POSITIONAL_ONLY,
            annotation="str",
        ),
        _inspect.Parameter("secrets", _inspect.Parameter.VAR_KEYWORD, annotation="str"),
    ]
)


def _constructor_signature(cls: type, base: type) -> _inspect.Signature:
    """The signature `inspect.signature(cls)` reports through a dispatching
    metaclass: the dispatch form for the base, `__init__` minus `self` for a
    subclass, and a `__signature__` the class sets itself, as it is."""
    own = cls.__dict__.get("__signature__")
    if isinstance(own, _inspect.Signature):
        return own
    if cls is base:
        return _DISPATCH_SIGNATURE
    init = _inspect.signature(cls.__init__)
    parameters = list(init.parameters.values())[1:]
    return init.replace(
        parameters=parameters, return_annotation=_inspect.Signature.empty
    )


class _HostConfigMeta(_abc.ABCMeta):
    # The return type is the class called, not `HostConfig`, so a checker
    # types a subclass call as that subclass and checks its keywords.
    def __call__(cls: _ty.Type[_T], *args: _ty.Any, **options: _ty.Any) -> _T:
        if cls is HostConfig:
            if len(args) != 1 or not isinstance(args[0], str):
                raise TypeError(
                    "HostConfig() requires one connection string positional argument"
                )
            return _ty.cast(_T, HostConfig._from_uri(args[0], **options))
        return _ty.cast(_T, type.__call__(cls, *args, **options))

    @property
    def __signature__(cls) -> _inspect.Signature:
        return _constructor_signature(cls, HostConfig)


class HostConfig(_abc.ABC, metaclass=_HostConfigMeta):
    """Secret-safe connection configuration and extensible URI dispatch."""

    #: Credential names `HostConfig(uri, **credentials)` may pass to this
    #: config. Declaring it makes dispatch reject anything else *before*
    #: construction, so a typo (`passwrd=`) fails loudly instead of silently
    #: producing a config with no password. `None` (the default) skips the
    #: check, for configs that validate credentials themselves.
    uri_credentials: _ty.ClassVar[_ty.Optional[_ty.Tuple[str, ...]]] = None

    _uri_schemes: _ty.ClassVar[_ty.Tuple[str, ...]] = ()
    _uri_registry_cache: _ty.ClassVar[
        _ty.Optional[_ty.Tuple[_ty.Type["HostConfig"], ...]]
    ] = None
    _uri_entry_points: _ty.ClassVar[_ty.Optional[tuple[object, ...]]] = None
    _uri_plugin_failures: _ty.ClassVar[dict[str, Exception]] = {}
    #: Entry-point names already imported, so a sweep for an unclaimed scheme
    #: loads each plugin once.
    _uri_plugins_loaded: _ty.ClassVar[set[str]] = set()
    _uri_registry_generation: _ty.ClassVar[int] = 0
    _uri_registry_lock: _ty.ClassVar[_threading.RLock] = _threading.RLock()

    #: Lifecycle state lives on the CLASS as well, because `HostConfig` is a
    #: documented extension point and a subclass with its own `__init__` that
    #: does not call `super().__init__()` -- the shape this project's own
    #: tests use -- otherwise dispatched and rendered fine and then failed
    #: `with config as host:` with a bare AttributeError about a private
    #: attribute.
    _opened_host: _ty.Optional["Host"] = None
    _lifecycle_lock: _ty.Optional["_threading.Lock"] = None

    # The second overload is the dispatch form `HostConfig(uri, **secrets)`,
    # which the metaclass serves; a subclass calls `super().__init__()`.
    @_ty.overload
    def __init__(self) -> None: ...

    @_ty.overload
    def __init__(self, connection_string: str, /, **secrets: str) -> None: ...

    def __init__(self, *args: object, **secrets: object) -> None:
        if args or secrets:
            raise TypeError("HostConfig.__init__() takes no arguments")
        self._opened_host: _ty.Optional[Host] = None
        self._lifecycle_lock = _threading.Lock()

    def _lifecycle(self) -> "_threading.Lock":
        """The instance's lifecycle lock, created on first use if needed."""
        lock = self.__dict__.get("_lifecycle_lock")
        if lock is None:
            with HostConfig._uri_registry_lock:
                lock = self.__dict__.get("_lifecycle_lock")
                if lock is None:
                    lock = _threading.Lock()
                    object.__setattr__(self, "_lifecycle_lock", lock)
        return lock

    def __init_subclass__(
        cls,
        *,
        schemes: _ty.Iterable[str] = (),
        replace: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        # Claimed only once nothing else holds them: a class that raises here
        # stays a live subclass until it is collected, and must claim nothing.
        cls._uri_schemes = ()
        wanted = tuple(scheme.casefold() for scheme in schemes)
        with HostConfig._uri_registry_lock:
            cls._claim_schemes(wanted, replace)
            cls._uri_schemes = wanted
            HostConfig._uri_registry_generation += 1
            HostConfig._uri_registry_cache = None

    @classmethod
    def _claim_schemes(cls, wanted: _ty.Tuple[str, ...], replace: bool) -> None:
        """Refuse a scheme another live config already claims.

        A redefinition (same module and name: a reload, or a test that builds
        the class per call) replaces its predecessor. `replace=True` takes the
        scheme over from the previous owner.
        """
        for other in _recursive_subclasses(HostConfig):
            if other is cls or not other._uri_schemes:
                continue
            taken = set(wanted) & set(other._uri_schemes)
            if not taken:
                continue
            same = (other.__module__, other.__qualname__) == (
                cls.__module__,
                cls.__qualname__,
            )
            if same:
                other._uri_schemes = ()
            elif replace:
                other._uri_schemes = tuple(
                    scheme for scheme in other._uri_schemes if scheme not in taken
                )
            else:
                name = sorted(taken)[0]
                raise ValueError(
                    f"host scheme {name!r} is already registered by "
                    f"{other.__name__}; pass replace=True to take it over"
                )

    @property
    def scheme(self) -> str:
        return _urlsplit(self.connection_uri).scheme.casefold()

    def __str__(self) -> str:
        return self.connection_uri

    @property
    @_abstract
    def connection_uri(self) -> str:
        """Credential-safe canonical connection URI."""
        raise NotImplementedError

    @_abstract
    def _create_host(self) -> Host:
        """Create the operational host represented by this configuration.

        Documented override point: implement it in a subclass, and reach it
        through :meth:`open` or ``with config as host``.
        """
        raise NotImplementedError

    def open(self) -> Host:
        """Return a host context manager for this configuration."""
        return self._create_host()

    def __enter__(self) -> Host:
        with self._lifecycle():
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
                with self._lifecycle():
                    if self._opened_host is host:
                        self._opened_host = None
            raise

    def __exit__(
        self,
        exc_type: _ty.Optional[_ty.Type[BaseException]],
        exc_value: _ty.Optional[BaseException],
        traceback: _ty.Optional[_types.TracebackType],
    ) -> _ty.Optional[bool]:
        with self._lifecycle():
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

        The password field is parsed by :func:`parse_credentials`, so a newline
        separates trailing `key:value` extras -- an OTP or other second factor
        travels through the same field. The newline may be written raw: it is
        percent-encoded before parsing (`urlsplit` would otherwise delete it),
        and decoded again when the password is read. A control character in the
        *host* is still refused, because no encoding makes that meaningful.
        """
        parsed = _urlsplit(_encode_stripped_characters(uri))
        _reject_authority_control_characters(parsed)
        _refuse_cut_short_userinfo(parsed)
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
            password, extras = parse_credentials(_unquote(parsed.password))
            credentials["password"] = password
            for key, value in extras.items():
                if key in credentials:
                    raise ValueError(
                        f"credential {key!r} given both in the connection URI "
                        "and as an argument"
                    )
                credentials[key] = value
            parsed = _without_password(parsed)
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
        implementation = matches[0]
        # Fail closed on an unknown credential *here*, so a config gets the
        # safe behaviour by declaring `uri_credentials` rather than by
        # remembering to call a helper. A typo like `passwrd=` would otherwise
        # build a config with no password and no complaint, surfacing much
        # later as an unexplained authentication failure.
        accepted = implementation._uri_credentials_for(parsed)
        if accepted is not None:
            strict_uri_credentials(credentials, accepted)
        return implementation._from_parsed_uri(parsed, **credentials)

    @classmethod
    def _uri_credentials_for(
        cls, parsed: _SplitResult
    ) -> _ty.Optional[_ty.Tuple[str, ...]]:
        """The credential names this implementation accepts for `parsed`.

        `uri_credentials` unless a scheme of the class narrows it.
        """
        return cls.uri_credentials

    @classmethod
    def supported_credentials(cls, uri: str) -> _ty.Optional[_ty.Tuple[str, ...]]:
        """Credential names the implementation selected by `uri` accepts.

        `None` means the implementation declares no whitelist and takes
        whatever it is given. Use this for an *ambient* credential -- one
        supplied for a session rather than for a single call, such as the
        CLI's `HOSTCTL_PASSWORD` -- which should be offered only where it can
        be used. An explicitly passed credential still fails closed, because
        there a name the config does not know is a typo worth reporting.
        """
        parsed = _urlsplit(_encode_stripped_characters(uri))
        _refuse_cut_short_userinfo(parsed)
        matches = [
            implementation
            for implementation in cls._uri_implementations(parsed.scheme)
            if implementation._matches_uri(parsed)
        ]
        if len(matches) != 1:
            return None
        return matches[0]._uri_credentials_for(parsed)

    @classmethod
    def _matches_uri(cls, parsed: _SplitResult) -> bool:
        """Whether this implementation accepts a parsed URI."""
        return parsed.scheme.casefold() in cls._uri_schemes

    @classmethod
    def _from_parsed_uri(
        cls, parsed: _SplitResult, **credentials: object
    ) -> HostConfig:
        """Construct from a URI selected by :meth:`_matches_uri`.

        Documented override point for a config subclass; callers use
        ``HostConfig(uri)``.
        """
        raise NotImplementedError(f"{cls.__name__} does not implement URI construction")

    @classmethod
    def _refresh_uri_registry(cls) -> None:
        """Clear URI implementation discovery for tests and newly loaded plugins."""
        with HostConfig._uri_registry_lock:
            HostConfig._uri_registry_generation += 1
            HostConfig._uri_registry_cache = None
            HostConfig._uri_entry_points = None
            HostConfig._uri_plugin_failures = {}
            HostConfig._uri_plugins_loaded = set()

    @classmethod
    def _uri_implementations(
        cls,
        requested_scheme: _ty.Optional[str] = None,
    ) -> _ty.Tuple[_ty.Type[HostConfig], ...]:
        scheme = requested_scheme.casefold() if requested_scheme else None

        # Every import below happens OUTSIDE `_uri_registry_lock`. Holding it
        # across an import inverted the lock order against
        # `__init_subclass__`, which takes the registry lock from inside a
        # module body: one thread held the registry lock and waited for a
        # module lock while another held that module lock and waited for the
        # registry lock. Python's import deadlock detection only covers module
        # locks, so both hung forever.
        with HostConfig._uri_registry_lock:
            needs_builtins = HostConfig._uri_registry_cache is None
        if needs_builtins:
            from ..transports import container as _container
            from ..transports.local import _host as _local
            from ..transports.qemu import _host as _qemu
            from ..transports.serial import _host as _serial
            from ..transports.ssh import _transport as _ssh
            from ..transports.winrm import _transport as _winrm

            del _container, _local, _qemu, _serial, _ssh, _winrm

        with HostConfig._uri_registry_lock:
            while HostConfig._uri_registry_cache is None:
                generation = HostConfig._uri_registry_generation
                discovered = list(_recursive_subclasses(HostConfig))
                if generation != HostConfig._uri_registry_generation:
                    continue
                HostConfig._uri_registry_cache = tuple(
                    item for item in discovered if item._uri_schemes
                )
            if HostConfig._uri_entry_points is None:
                # Reading metadata imports nothing, so it stays under the lock.
                HostConfig._uri_entry_points = entry_points_in("hostctl.configs")
            candidates = list(HostConfig._uri_registry_cache)
            entry_points = HostConfig._uri_entry_points
            failure = (
                HostConfig._uri_plugin_failures.get(scheme)
                if scheme is not None
                else None
            )

        if failure is not None:
            # A FRESH exception. Re-raising the cached instance appended the
            # current frames to its traceback every time, so a service that
            # retried a broken plugin's scheme per request grew the traceback
            # -- and every frame's locals, including credentials passed as
            # dispatch kwargs -- without bound, for the life of the process.
            raise RuntimeError(
                f"hostctl.configs entry point for scheme {scheme!r} failed to load"
            ) from failure
        if scheme is not None:
            known = {
                value
                for item in candidates
                for value in getattr(item, "_uri_schemes", ())
            }
            # The entry point named for the scheme goes first: a broken
            # unrelated plugin earlier in the list must not stand between a
            # scheme and its own plugin.
            ordered = sorted(
                entry_points,
                key=lambda point: str(getattr(point, "name", "")).casefold() != scheme,
            )
            for entry_point in ordered:
                name = str(getattr(entry_point, "name", "")).casefold()
                with HostConfig._uri_registry_lock:
                    if (
                        name in HostConfig._uri_plugins_loaded
                        or name in HostConfig._uri_plugin_failures
                    ):
                        continue
                # The entry point's NAME matching the scheme is the fast
                # path, not the rule: a plugin may declare several schemes
                # under one entry point (`schemes=("plug", "plug+tls")`). An
                # unknown scheme loads the remaining entry points too; each
                # load happens once, and a plugin that matches nothing leaves
                # the scheme unsupported.
                if name != scheme and scheme in known:
                    continue
                try:
                    # The plugin import that must not hold the registry lock.
                    implementation = entry_point.load()
                    if not isinstance(implementation, type) or not issubclass(
                        implementation, HostConfig
                    ):
                        raise TypeError(
                            f"hostctl.configs entry point {name!r} "
                            "must load a HostConfig subclass"
                        )
                except Exception as exc:
                    with HostConfig._uri_registry_lock:
                        HostConfig._uri_plugin_failures[name] = exc
                    import warnings

                    warnings.warn(
                        f"unable to load hostctl.configs entry point {name!r}: {exc}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    continue
                with HostConfig._uri_registry_lock:
                    HostConfig._uri_plugins_loaded.add(name)
                candidates.append(implementation)
                if scheme in getattr(implementation, "_uri_schemes", ()):
                    break
            with HostConfig._uri_registry_lock:
                failure = HostConfig._uri_plugin_failures.get(scheme)
            claimed = any(
                scheme in getattr(item, "_uri_schemes", ()) for item in candidates
            )
            if failure is not None and not claimed:
                raise RuntimeError(
                    f"hostctl.configs entry point for scheme {scheme!r} failed to load"
                ) from failure
        return tuple(
            item
            for item in dict.fromkeys(candidates)
            if issubclass(item, cls) and item._uri_schemes
        )


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
        from ..shell._flavour import Shell

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
