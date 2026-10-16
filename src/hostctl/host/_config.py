"""Connection configuration, `HostConfig(uri)` dispatch and the scheme registry."""

from __future__ import annotations

import abc as _abc
import inspect as _inspect
import threading as _threading
import typing as _ty
import types as _types
from urllib.parse import (
    SplitResult as _SplitResult,
    unquote as _unquote,
    urlsplit as _urlsplit,
)


from ..uri._parse import (
    _encode_stripped_characters,
    _refuse_cut_short_userinfo,
    _reject_authority_control_characters,
    _without_password,
    parse_credentials,
    strict_uri_credentials,
)
from .._registry import _recursive_subclasses, entry_points_in
from ._dispatch import _T, _abstract, _constructor_signature
from ._timeout import Timeout, _resolve as _resolve_timeout


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

    #: The `Timeout` fields this config honours, each with what `DEFAULT`
    #: resolves to; any other field set to a value raises `ValueError`.
    _timeout_defaults: _ty.ClassVar[_ty.Mapping[str, object]] = {}

    @property
    def timeouts(self) -> Timeout:
        """`timeout` with each `DEFAULT` replaced by what the transport applies.

        A field the transport does not support stays `DEFAULT`; so does a
        system config's `total`, which the selected provider decides. A
        config without a `timeout` attribute reports `Timeout()`.
        """
        timeout = getattr(self, "timeout", None)
        if not isinstance(timeout, Timeout):
            return Timeout()
        return _resolve_timeout(timeout, self._timeout_defaults)

    @property
    def scheme(self) -> str:
        return _urlsplit(self.connection_uri).scheme.casefold()

    @property
    def name(self) -> str:
        """A short label for the target, for a log line: never a credential.

        By default the authority of `connection_uri` without its userinfo
        (`nas.example`, `node:2222`), else its path (`vm1`), else
        `localhost`. Assigning it replaces the label.
        """
        given = self.__dict__.get("_name")
        return given if given is not None else self._default_name()

    @name.setter
    def name(self, value: str) -> None:
        self.__dict__["_name"] = str(value)

    def _default_name(self) -> str:
        try:
            parsed = _urlsplit(self.connection_uri)
        except NotImplementedError:
            return ""
        authority = _unquote(parsed.netloc).rpartition("@")[2]
        if authority:
            return authority
        path = _unquote(parsed.path)
        return (path[1:] if path.startswith("/") else path) or "localhost"

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


# Bound last: `_host` imports this module, and `get_type_hints` resolves
# the `Host` annotations above against these globals.
from ._host import Host  # noqa: E402
