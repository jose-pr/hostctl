"""Operating-system hosts composed from ordered transport providers."""

from __future__ import annotations

import dataclasses
import logging
import subprocess
import threading
import typing
from contextvars import ContextVar
from urllib.parse import unquote, quote, urlencode

from pathapi import Path

from ..executor._common import CommandLine, command_text
from ..provider import OperationNotStarted, ProviderSelector
from ..shell import POWERSHELL, POSIX_SHELL, ShellFlavour, shell_flavour
from ._common import (
    CaptureOutput,
    Environment,
    FileHandle,
    Host,
    HostConfig,
    _HostExecutor,
    _refuse_uri,
    entry_points_in,
    reject_control_characters,
    HostInfo,
    Input,
    PathLike,
    Command,
    starts_direct_command,
    strict_uri_query,
)
from .composite_path import CompositePosixPath, CompositeWindowsPath

log = logging.getLogger("hostctl.host.system")

_SYSTEM_PROVIDER_RESOLVERS = {}


def register_system_provider(
    name: str,
    resolver: typing.Callable[["SystemConfig", str], typing.Any],
    *,
    replace: bool = False,
) -> None:
    """Register a provider descriptor resolver for :class:`SystemConfig`.

    `resolver(config, kind)` returns the provider for `kind` (`"executor"` or
    `"path"`), or `None` when the descriptor offers none. A name registered
    already raises `ValueError` unless `replace=True`.
    """
    key = str(name).casefold().strip()
    if not key or not callable(resolver):
        raise ValueError("provider name and callable resolver are required")
    if key in _SYSTEM_PROVIDER_RESOLVERS and not replace:
        raise ValueError(f"system provider is already registered: {key}")
    _SYSTEM_PROVIDER_RESOLVERS[key] = resolver


def system_provider_names() -> typing.Tuple[str, ...]:
    """Every provider descriptor `SystemConfig` accepts, sorted.

    The registered names plus the `hostctl.system_providers` entry points,
    which are listed and not imported.
    """
    names = set(_SYSTEM_PROVIDER_RESOLVERS)
    names.update(
        str(point.name).casefold() for point in entry_points_in(_ENTRY_POINT_GROUP)
    )
    return tuple(sorted(names))


_ENTRY_POINT_GROUP = "hostctl.system_providers"


def _load_system_provider(key: str):
    """Load, register and return the plugin resolver named `key`, if any."""
    for point in entry_points_in(_ENTRY_POINT_GROUP):
        if str(point.name).casefold() != key:
            continue
        try:
            resolver = point.load()
            register_system_provider(key, resolver, replace=True)
        except Exception as exc:
            raise ValueError(f"invalid system provider plugin {key!r}: {exc}") from exc
        return resolver
    return None


def _provider_option(config: "SystemConfig", name: str):
    values = config.options.get("provider_options", {})
    if isinstance(values, dict):
        return values.get(name)
    return None


def _local_provider(config, kind):
    del config
    from ..provider.transports import LocalExecutorProvider, LocalPathProvider

    return LocalExecutorProvider() if kind == "executor" else LocalPathProvider()


#: The transports one `_create_host()` call has built, by option key. Scoped to
#: the call (and so to its thread), because the executor and path providers of
#: ONE host must share a connection and two hosts must not.
_TRANSPORTS: "ContextVar[typing.Optional[dict]]" = ContextVar(
    "hostctl_transports", default=None
)


def _shared_transport(key, build):
    """The transport for `key` within this `_create_host()` call, built once."""
    cache = _TRANSPORTS.get()
    if cache is None:
        return build()
    if key not in cache:
        cache[key] = build()
    return cache[key]


def _transport_provider(config, kind, option, label, load):
    """Resolve one provider of a transport-backed pair from `provider_options`.

    `load()` imports lazily: (config type, transport type, executor provider
    type, path provider type).
    """
    config_type, transport_type, executor_type, path_type = load()
    value = _provider_option(config, option)
    if not isinstance(value, config_type):
        raise ValueError(
            f"system {label} descriptors require "
            f"provider_options={{'{option}': {config_type.__name__}(...)}}"
        )
    transport = _shared_transport(option, lambda: transport_type(value))
    return executor_type(transport) if kind == "executor" else path_type(transport)


def _load_ssh():
    from ._ssh import SftpPathProvider, SshConfig, SshExecutorProvider, _SshTransport

    return SshConfig, _SshTransport, SshExecutorProvider, SftpPathProvider


def _load_winrm():
    from ._winrm import (
        WinRMConfig,
        WinRMExecutorProvider,
        WinRMPathProvider,
        _WinRMTransport,
    )

    return WinRMConfig, _WinRMTransport, WinRMExecutorProvider, WinRMPathProvider


def _ssh_provider(config, kind):
    return _transport_provider(config, kind, "ssh", "SSH", _load_ssh)


def _winrm_provider(config, kind):
    return _transport_provider(config, kind, "winrm", "WinRM", _load_winrm)


register_system_provider("local", _local_provider)
register_system_provider("ssh", _ssh_provider)
register_system_provider("sftp", _ssh_provider)
register_system_provider("winrm", _winrm_provider)


class _ShellContext(typing.NamedTuple):
    """What the SHELL layer must supply, because the provider cannot.

    `cwd` and `env` are `None` when the provider carries them natively --
    passing them to the flavour as well would apply them twice. Computed
    once per dispatch instead of six times inline, which is where
    `core-01`, `core-03` and `core-32` all lived: the combinations are the
    bug surface, so there is one expression of them.
    """

    cwd: typing.Optional[PathLike]
    env: typing.Optional[typing.Any]
    for_session: bool


def _for_login_shell(flavour, command, provider) -> str:
    """Escape a rendered command line for the shell that will parse it.

    A command line is rendered for exactly ONE parse. A transport that
    delivers it to a login shell -- SSH does; `CreateProcess` does not --
    makes that shell parse it first, consuming the layer the flavour
    applied. Only the transport knows this, so the provider is asked.

    Measured against a real cmd.exe standing in for Windows OpenSSH's stock
    server: 4 of 8 adversarial values survived unescaped, and two of the
    failures were command injection (`a&b`, `x|y`).
    """
    if flavour.argv_invocation:
        # The flavour renders argv, so what a transport sends is a SCRIPT and
        # the login shell's single parse is the one it was rendered for. This
        # is every POSIX-family flavour: `$SHELL -c <script>` is exactly the
        # target, and adding a layer here would corrupt it.
        return command
    login_shell = getattr(provider, "login_shell", None)
    if login_shell is not None and login_shell == flavour.name:
        # A command LINE delivered to the same shell it was rendered for:
        # that shell parses it, and then the line's own `/c` parses it again.
        # A different login shell either does not parse it or could not run
        # it anyway, and PowerShell's `-EncodedCommand` is inert under all of
        # them.
        return flavour.escape_for_one_parse(command)
    return command


def _shell_invocation(
    flavour, cmds, *, cwd, env, for_session, executable, provider=None
):
    """One shell layer, as argv or as a command line.

    Most flavours render as argv. `cmd` cannot -- an argv element's quotes
    are escaped by the platform's own command-line quoting before cmd ever
    sees them (`CmdShellFlavour.invocation`), which silently corrupted every
    value carrying a quote or a metacharacter. For those the rendered command
    line is submitted instead, marked so the executor does not re-quote it.

    `login_shell` names what parses that line ON ARRIVAL, when a transport
    delivers it to a shell rather than to `CreateProcess`. Only the
    transport knows this, so it is passed in: a command line is rendered for
    exactly ONE parse, and a login shell that parses it again consumes that
    layer. Measured against a real cmd.exe standing in for Windows OpenSSH's
    stock server, 4 of 8 adversarial values survived unescaped and two of
    the failures were command injection.
    """
    if not flavour.argv_invocation:
        rendered = flavour.command(cmds, executable=executable, cwd=cwd, env=env)
        return (CommandLine(_for_login_shell(flavour, rendered.command, provider)),)
    script = flavour.script(cmds, cwd=cwd, env=env, for_session=for_session)
    return tuple(flavour.invocation(script, executable=executable))


class SystemConfig(HostConfig):
    """Base configuration for a logical system with one or more providers.

    Abstract: concrete system families are :class:`PosixConfig`,
    :class:`WindowsConfig`, and :class:`IosConfig`.  Each binds ``host_type``
    and ``uri_scheme``; this class binds neither and cannot be instantiated
    into a host on its own.
    """

    #: Class-level URI scheme used to *build* :attr:`connection_uri`.  This is
    #: deliberately not named ``scheme``: ``HostConfig.scheme`` is a property
    #: derived by parsing the built URI, and shadowing it with a plain string
    #: broke that contract for every subclass here.  The two now flow one way
    #: -- ``uri_scheme`` builds the URI, ``scheme`` reads it back -- so they
    #: cannot disagree.
    uri_scheme: typing.ClassVar[str] = ""

    #: Concrete `SystemHost` subclass this configuration creates.  Bound by
    #: each concrete config; ``None`` marks this base class as abstract.
    host_type: typing.ClassVar[typing.Optional[typing.Type["SystemHost"]]] = None

    def __init__(
        self,
        authority: str = "localhost",
        *,
        shell: object = None,
        executor: typing.Iterable[str] = (),
        path: typing.Iterable[str] = (),
        provider_options: typing.Optional[typing.Mapping[str, object]] = None,
        initializer: typing.Optional[typing.Callable[..., object]] = None,
    ):
        super().__init__()
        _refuse_uri(type(self).__name__, authority)
        for name, value in (("executor", executor), ("path", path)):
            if isinstance(value, (str, bytes)):
                raise TypeError(
                    f"{name}= takes a sequence of descriptor names, "
                    "e.g. ('local',), not a string"
                )
        self.authority = authority or "localhost"
        self.shell = shell
        self.executors = tuple(executor)
        self.paths = tuple(path)
        self.options = {
            key: value
            for key, value in (
                ("provider_options", provider_options),
                ("initializer", initializer),
            )
            if value is not None
        }

    def _build_providers(self, kind: str):
        descriptors = self.executors if kind == "executor" else self.paths
        result = []
        for descriptor in descriptors:
            key = str(descriptor).casefold()
            resolver = _SYSTEM_PROVIDER_RESOLVERS.get(key) or _load_system_provider(key)
            if resolver is None:
                raise ValueError(f"unknown {kind} provider descriptor: {descriptor!r}")
            provider = resolver(self, kind)
            if provider is None:
                raise ValueError(
                    f"provider descriptor {descriptor!r} does not support {kind}"
                )
            result.append(provider)
        return tuple(result)

    @property
    def connection_uri(self) -> str:
        if not self.uri_scheme:
            # The abstract base registers no URI scheme, so it has no
            # round-trippable URI to advertise.  It previously claimed
            # "system://", which `HostConfig(...)` then rejected as an
            # unsupported scheme -- advertising a URI that cannot be parsed
            # back is worse than declining to produce one.
            raise NotImplementedError(
                f"{type(self).__name__} is abstract and has no connection URI"
            )
        query: typing.List[typing.Tuple[str, str]] = [
            ("executor", value) for value in self.executors
        ]
        query += [("path", value) for value in self.paths]
        if self.shell is not None:
            query.append(("shell", getattr(self.shell, "name", str(self.shell))))
        return f"{self.uri_scheme}://{quote(self.authority, safe='')}" + (
            f"?{urlencode(query)}" if query else ""
        )

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials):
        constructor_only = {}
        for key in ("provider_options", "initializer"):
            if key in credentials:
                constructor_only[key] = credentials.pop(key)
        if credentials:
            names = ", ".join(sorted(str(key) for key in credentials))
            raise ValueError(
                "system URI reconstruction accepts only provider_options= and "
                f"initializer= constructor options; unsupported credentials: {names}"
            )
        # The SHARED parser, with the two keys that legitimately repeat
        # declared. This used to be a second hand-rolled query parse -- one
        # of three in this package -- and each of the three had a different
        # defect fixed in it.
        query = strict_uri_query(
            parsed, ("executor", "path", "shell"), repeatable=("executor", "path")
        )
        executors = typing.cast(typing.Tuple[str, ...], query.get("executor", ()))
        paths = typing.cast(typing.Tuple[str, ...], query.get("path", ()))
        # The whole netloc, userinfo included, is unquoted below, so a
        # percent-encoded control character there would arrive decoded.
        reject_control_characters(parsed.netloc)
        return cls(
            # Unquoted: `connection_uri` percent-encodes the authority, so
            # storing the still-encoded netloc added a layer on every
            # `HostConfig(str(config))` round trip -- `node:22` became
            # `node%3A22`, then `node%253A22` -- and the API header promises
            # `str(config)` can be handed straight back.
            unquote(parsed.netloc) or unquote(parsed.path) or "localhost",
            shell=query.get("shell"),
            executor=executors,
            path=paths,
            **constructor_only,
        )

    def _create_host(self):
        if self.host_type is None:
            names = ", ".join(
                item.__name__ for item in (PosixConfig, WindowsConfig, IosConfig)
            )
            raise TypeError(
                f"{type(self).__name__} is abstract and creates no host; "
                f"use a concrete system configuration ({names})"
            )
        # A fresh transport cache per call: one host's executor and path
        # providers share a connection, and two hosts never do -- either one's
        # `close()` would tear down the connection the other is using.
        token = _TRANSPORTS.set({})
        try:
            executor_providers = self._build_providers("executor")
            path_providers = self._build_providers("path")
        finally:
            _TRANSPORTS.reset(token)
        return self.host_type(
            self,
            executor_providers=executor_providers,
            path_providers=path_providers,
        )


class SystemHost(Host):
    """Host orchestration shared by POSIX, Windows, and IOS systems."""

    system_family = "generic"
    default_shell: typing.Optional[ShellFlavour] = None
    #: Configuration class used when a host is constructed without one.
    #: Bound by each concrete family below, once those classes exist.
    config_type: typing.ClassVar[typing.Type[SystemConfig]]

    def __init__(
        self,
        config: typing.Optional[SystemConfig] = None,
        *,
        executor_providers=(),
        path_providers=(),
        shell=None,
        info: typing.Optional[HostInfo] = None,
        initializer=None,
    ):
        # A config-less host builds the configuration matching its own system
        # family.  Previously this made a bare `SystemConfig` and then
        # assigned to `config.scheme` -- writing to what `HostConfig` defines
        # as a read-only computed property, which only worked because
        # `SystemConfig` shadowed that property with a plain string.  Picking
        # the right config type instead keeps `scheme` derived from the URI.
        if isinstance(config, str):
            raise TypeError(
                f"{type(self).__name__} takes a config, not a connection string; "
                "use Host(uri) or HostConfig(uri).open()"
            )
        if config is not None and not isinstance(config, HostConfig):
            raise TypeError(
                f"{type(self).__name__} takes a HostConfig, "
                f"not {type(config).__name__}"
            )
        self.config = config if config is not None else self.config_type()
        self._executor_selector = ProviderSelector(executor_providers)
        self._path_selector = ProviderSelector(path_providers)
        self._shell_resolver = (
            shell if callable(shell) and not isinstance(shell, type) else None
        )
        self._shell = (
            None
            if self._shell_resolver is not None
            else (
                shell_flavour(shell)
                if shell is not None
                else (
                    shell_flavour(getattr(self.config, "shell", None))
                    if getattr(self.config, "shell", None)
                    else self.default_shell
                )
            )
        )
        self._info = info
        config_options = getattr(self.config, "options", {})
        self._initializer = (
            initializer
            if initializer is not None
            else (
                config_options.get("initializer")
                if isinstance(config_options, dict)
                else None
            )
        )
        if self._initializer is not None and not callable(self._initializer):
            raise TypeError("initializer must be callable")
        self._initializer_generation = False
        self._initializing = False
        self._connected = False
        self._connected_providers = []
        self._closed_targets = set()
        # Serializes this host's connection *bookkeeping*: `_connected`,
        # `_connected_providers`, `_closed_targets`, and `_initializer_
        # generation`.  Without it, concurrent `run()` calls race the
        # check-then-append in `_ensure_provider_connected` and each append a
        # duplicate entry for the same provider, so the list grows without
        # bound and every racing caller repeats the connect round-trip.
        #
        # RLock, not Lock: these paths nest.  `connect()` runs the session
        # initializer, which is handed this same host and legitimately calls
        # `run()` -> `_ensure_provider_connected`; a plain Lock would deadlock
        # on that re-entry.
        #
        # Scope: the lock deliberately spans the provider `connect()` call.
        # Connecting is the operation being deduplicated, so releasing the
        # lock around it would reintroduce the very race it exists to close --
        # two callers would both observe "not connected" and both dial out.
        # Providers already own the slow part behind their own locks
        # (`_SshTransport._ssh_lock`), and a `SystemHost` is one logical
        # target whose providers are ordered fallbacks for that same target,
        # not independent endpoints to be dialed in parallel.  Command
        # dispatch itself -- `run()`, `path()`, `spawn()` -- stays outside the
        # lock, so this never serializes actual remote work.
        self._lifecycle_lock = threading.RLock()

    @property
    def executor_providers(self) -> typing.Tuple[object, ...]:
        """The ordered command providers backing :meth:`run`."""
        return self._executor_selector.providers

    @property
    def path_providers(self) -> typing.Tuple[object, ...]:
        """The ordered filesystem providers backing :meth:`path`."""
        return self._path_selector.providers

    def replace_providers(self, *, executor=None, path=None) -> None:
        """Swap either provider set; ``None`` keeps it.

        Starts a new connection generation: the session initializer runs again
        before the next operation. A replaced provider's transport is closed
        unless a provider that stays shares it, and a transport shared by
        several replaced providers is closed once. A path obtained earlier
        still routes to the old providers, so obtain it again. An invalid
        set (duplicate names) raises before anything changes.
        """
        with self._lifecycle_lock:
            new_executor = ProviderSelector(executor) if executor is not None else None
            new_path = ProviderSelector(path) if path is not None else None
            old = [
                *(self._executor_selector.providers if new_executor else ()),
                *(self._path_selector.providers if new_path else ()),
            ]
            kept = [
                *(new_executor or self._executor_selector).providers,
                *(new_path or self._path_selector).providers,
            ]
            replaced = [
                provider
                for provider in old
                if not any(provider is item for item in kept)
            ]
            kept_targets = {id(getattr(item, "transport", item)) for item in kept}
            targets = []
            seen = set()
            for provider in replaced:
                target = getattr(provider, "transport", provider)
                if (
                    id(target) not in seen
                    and id(target) not in kept_targets
                    and id(target) not in self._closed_targets
                ):
                    seen.add(id(target))
                    targets.append(target)
            self._connected_providers = [
                item
                for item in self._connected_providers
                if not any(item is provider for provider in replaced)
            ]
            if new_executor is not None:
                self._executor_selector = new_executor
            else:
                self._executor_selector.invalidate()
            if new_path is not None:
                self._path_selector = new_path
            else:
                self._path_selector.invalidate()
            self._connected = False
            self._initializer_generation = False
            first_error = None
            for target in reversed(targets):
                close = getattr(target, "close", None)
                if close:
                    try:
                        close()
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                self._closed_targets.add(id(target))
            if first_error is not None:
                raise first_error

    @property
    def shell_flavour(self):
        if self._shell is None and self._shell_resolver is not None:
            self._shell = shell_flavour(self._shell_resolver())
        if self._shell is None:
            raise NotImplementedError(
                f"{type(self).__name__} does not configure a shell"
            )
        return self._shell

    @property
    def executor_capabilities(self):
        values = set()
        for provider in self._executor_selector.providers:
            if self._provider_probe(self._executor_selector, provider).usable:
                values.update(self._executor_selector.effective_capabilities(provider))
        return frozenset(values)

    @staticmethod
    def _provider_probe(selector, provider):
        return selector.probe(provider)

    @property
    def provider_details(self):
        """Return deterministic, non-dispatching provider availability details."""
        details = []
        for kind, selector in (
            ("executor", self._executor_selector),
            ("path", self._path_selector),
        ):
            for provider in selector.providers:
                probe = self._provider_probe(selector, provider)
                capabilities = selector.effective_capabilities(provider)
                details.append(
                    {
                        "kind": kind,
                        "name": ProviderSelector.redact(provider.name),
                        "availability": probe.availability,
                        "reason": ProviderSelector.redact(probe.reason),
                        "capabilities": tuple(sorted(capabilities)),
                        "system_hint": probe.system_hint,
                        "policy": "ordered",
                    }
                )
        return tuple(details)

    @property
    def capabilities(self):
        values = set()
        executor_probes = [
            (provider, self._provider_probe(self._executor_selector, provider))
            for provider in self._executor_selector.providers
        ]
        path_probes = [
            (provider, self._provider_probe(self._path_selector, provider))
            for provider in self._path_selector.providers
        ]
        if any(probe.usable for _, probe in executor_probes):
            values.add("run")
        # `spawn` and `tty` were never reported, so an SSH-backed host
        # advertised {run, path} while `spawn()` and TTY sessions worked --
        # and contracts.md tells callers to gate on exactly this set.
        # `ContainerHost` reported them all along; the two now agree.
        for capability in ("runspace", "spawn", "tty"):
            if any(
                probe.usable
                and capability
                in self._executor_selector.effective_capabilities(provider)
                for provider, probe in executor_probes
            ):
                values.add(capability)
        if any(probe.usable for _, probe in path_probes):
            values.add("path")
        return frozenset(values)

    @property
    def executor(self):
        """The selected provider, presented as an `Executor`.

        A bare `ExecutorProvider` has no `executor_capabilities`, so a
        `Shell` built on one inferred CWD/ENV support from the callable's
        signature instead of reading the provider's declared set -- and
        inferred it wrongly. The wrapper carries the declared capabilities
        and dispatches through `run()`, so the host's own rendering and
        failover still apply.
        """
        selected = self._executor_selector.select()
        provider = selected.provider
        return _HostExecutor(self, frozenset(provider.capabilities), provider)

    def connect(self):
        with self._lifecycle_lock:
            self._connect_locked()

    def _connect_locked(self):
        if self._connected:
            return
        connected = []
        try:
            for selector in (self._executor_selector, self._path_selector):
                excluded = []
                while True:
                    try:
                        provider = selector.select(exclude=excluded).provider
                    except OperationNotStarted:
                        break
                    try:
                        connect = getattr(provider, "connect", None)
                        if connect:
                            connect()
                    except OperationNotStarted:
                        excluded.append(provider.name)
                        continue
                    target = getattr(provider, "transport", provider)
                    self._closed_targets.discard(id(target))
                    connected.append(provider)
                    break
        except BaseException:
            for provider in reversed(connected):
                close = getattr(provider, "close", None)
                if close:
                    close()
            raise
        self._connected_providers = connected
        self._run_initializer_locked(connected)
        self._connected = True

    def _run_initializer_locked(self, connected) -> None:
        """Run the session initializer once per connection generation.

        Called from the explicit `connect()` *and* from the lazy path, because
        `run()`, `path()` and `spawn()` all open a generation without going
        through `connect()` -- and providers.md promises the initializer runs
        once per generation, before the host is handed to the caller. It used
        to run only from `connect()`, so a host used lazily was bootstrapped
        silently not at all.

        The `_initializing` guard is what makes the lazy path safe: the
        initializer is handed this host and legitimately calls `run()`, which
        re-enters here through `_ensure_provider_connected` (the lock is an
        RLock for exactly that), and `_initializer_generation` is only set
        afterwards -- so without the guard the nested call would start the
        initializer again.
        """
        if (
            self._initializer is None
            or self._initializer_generation
            or self._initializing
        ):
            return
        self._initializing = True
        try:
            # `SessionInitializer` is itself callable and applies its own
            # default timeout, so it and a plain callable are invoked
            # identically -- there is no separate branch to take.
            self._initializer(self)
            self._initializer_generation = True
        except BaseException:
            for provider in reversed(connected):
                close = getattr(provider, "close", None)
                if close:
                    try:
                        close()
                    except BaseException:
                        pass
            self._connected_providers = []
            self._executor_selector.invalidate()
            self._path_selector.invalidate()
            raise
        finally:
            self._initializing = False

    def close(self):
        with self._lifecycle_lock:
            self._close_locked()

    def _close_locked(self):
        targets = []
        # Dedupe by identity, matching the `id(target)` check below. Value
        # equality would collapse two distinct transports that compare equal
        # (e.g. a user-supplied dataclass transport registered through
        # `register_system_provider`) into one entry and leak the other.
        seen_targets = set()
        for provider in (
            *self._connected_providers,
            *self._executor_selector.providers,
            *self._path_selector.providers,
        ):
            target = getattr(provider, "transport", provider)
            if id(target) not in seen_targets:
                seen_targets.add(id(target))
                targets.append(target)
        first_error = None
        for target in reversed(targets):
            if id(target) in self._closed_targets:
                continue
            close = getattr(target, "close", None)
            if close:
                try:
                    close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            self._closed_targets.add(id(target))
        self._connected = False
        self._connected_providers = []
        self._initializer_generation = False
        self._executor_selector.invalidate()
        self._path_selector.invalidate()
        if self._shell_resolver is not None:
            self._shell = None
        if first_error is not None:
            raise first_error

    def info(self) -> HostInfo:
        fields = {name: None for name in HostInfo.__dataclass_fields__}
        if self._info is not None:
            fields.update(
                {
                    name: value
                    for name, value in dataclasses.asdict(self._info).items()
                    if value is not None
                }
            )
        for provider in self._executor_selector.providers:
            if all(value is not None for value in fields.values()):
                # Everything is known. Dialling a backup transport merely to
                # read `HostInfo` could prompt for 2FA or wait out a TCP
                # timeout on a host whose primary already answered.
                break
            if not self._provider_probe(self._executor_selector, provider).usable:
                continue
            callback = getattr(provider, "info", None)
            if callback is None:
                continue
            try:
                self._ensure_provider_connected(provider)
                value = callback()
            # Transport failures only. A bare `except Exception` here also
            # swallowed programming errors, so a broken provider reported
            # "no information available" instead of its own traceback.
            except (
                OperationNotStarted,
                ConnectionError,
                TimeoutError,
                PermissionError,
                OSError,
            ) as exc:
                log.debug(
                    "provider %s could not report info: %s",
                    ProviderSelector.redact(provider.name),
                    type(exc).__name__,
                )
                continue
            if not isinstance(value, HostInfo):
                continue
            for name, item in dataclasses.asdict(value).items():
                if fields[name] is None and item is not None:
                    fields[name] = item
        authority = getattr(self.config, "authority", None) or getattr(
            self.config, "host", None
        )
        # The host, not the authority: `root@node:2222` is a target, and
        # reporting it as `HostInfo.hostname` put userinfo -- a username, and
        # with it a hint at the credential -- into every diagnostic that
        # printed the hostname.
        hostname = authority
        if isinstance(authority, str) and authority:
            from netimps import split_host

            hostname = authority.rpartition("@")[2]
            try:
                hostname = split_host(hostname)[0]
            except ValueError:
                pass
        if fields["hostname"] is None:
            fields["hostname"] = hostname
        if fields["os_family"] is None:
            fields["os_family"] = self.system_family
        return HostInfo(**fields)

    def _composite_path_type(self):
        """The path grammar this system family actually has.

        Everything that is not Windows used to fall through to POSIX, so an
        `IosHost` -- documented as session/command-only until an IOS path
        grammar is designed -- advertised `path` and applied POSIX rules to
        locations like `flash:/config.text`, where `:` is a device separator
        rather than part of a name.
        """
        if self.system_family == "windows":
            return CompositeWindowsPath
        if self.system_family == "posix":
            return CompositePosixPath
        raise NotImplementedError(
            f"{type(self).__name__} has no path grammar; "
            f"the {self.system_family!r} family is command-only"
        )

    def path(self, *segments: PathLike, backend: typing.Optional[str] = None) -> Path:
        if not self._path_selector.providers:
            raise NotImplementedError(
                f"{type(self).__name__} does not provide the 'path' capability"
            )
        if backend is None:
            provider = self._path_selector.select().provider
        else:
            provider = next(
                (
                    item
                    for item in self._path_selector.providers
                    if item.name == backend
                ),
                None,
            )
            if provider is None:
                raise ValueError(f"unknown path provider: {backend}")
            # Through the selector: it caches the probe, honours a decline, and
            # turns a probe that raises into "unavailable" instead of letting a
            # transport error escape from a routing choice.
            if not self._path_selector.probe(provider).usable:
                raise OperationNotStarted(f"path provider {backend!r} is unavailable")
        path_type = self._composite_path_type()
        excluded: typing.List[str] = []
        while True:
            try:
                # Note the provider as in use WITHOUT connecting: building a
                # path is I/O-free, and `host.path()` on a host that was never
                # connected must stay that way. The marker is what matters
                # here -- a transport these paths later reopen lazily was left
                # on `_closed_targets`, so every later close() skipped it and
                # the connection lived until process exit.
                self._note_provider_in_use(provider)
                value = provider.path(*segments)
            except OperationNotStarted as exc:
                if backend is not None:
                    raise
                # Every candidate, not just one. Falling back exactly once
                # meant three ordered providers with the first two offline
                # failed although the third was usable -- while `run()`,
                # which loops, succeeded on the same host.
                self._path_selector.decline(provider.name, str(exc))
                excluded.append(provider.name)
                provider = self._path_selector.select(exclude=excluded).provider
                continue
            return path_type.from_path(
                value,
                provider,
                provider.path,
                self._path_selector.providers,
                self._path_selector,
                logical_segments=segments,
            )

    @staticmethod
    def _out_of_band_env(provider, rendered, options):
        """Carry a flavour's out-of-band environment, or refuse to lose it.

        `ShellCommand.environment` means "the environment is NOT embedded in
        this command; send it separately" -- which is what `_SshTransport.run`
        does. `SystemHost` passed only `.command`, so a registered flavour
        for a shell that cannot export variables lost every one of them in
        silence. Latent for the four built-in flavours, which all return
        `None`; that is a reason to fix it cheaply, not to keep it.
        """
        environment = getattr(rendered, "environment", None)
        if not environment:
            # `None` means embedded, and an empty mapping says the same
            # thing with fewer words -- neither may overwrite an environment
            # the provider is already carrying natively.
            return options
        if "env" not in provider.capabilities:
            raise NotImplementedError(
                f"shell flavour sends its environment out of band, and executor "
                f"provider {provider.name!r} cannot carry one"
            )
        return {**options, "env": environment}

    def run(
        self,
        *cmds: Command,
        bufsize: int = -1,
        executable: typing.Optional[str] = None,
        stdin: typing.Optional[FileHandle] = None,
        stdout: typing.Optional[FileHandle] = None,
        stderr: typing.Optional[FileHandle] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        capture_output: CaptureOutput = True,
        check: bool = True,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: Input = None,
        timeout: typing.Optional[float] = None,
        text: typing.Optional[bool] = None,
    ) -> subprocess.CompletedProcess:
        if not self._executor_selector.providers:
            raise NotImplementedError(
                f"{type(self).__name__} does not provide the 'run' capability"
            )
        # No `retry_declined()` here. Clearing every refusal per call re-dialled
        # a dead primary on EVERY run() -- ~9 s each against a refused port --
        # although the fallback was serving. The selector re-admits an earlier
        # refusal only when nothing else is left, which is what keeps a
        # single-provider host from bricking on one transient failure.
        excluded: list[str] = []
        while True:
            provider = self._executor_selector.select(exclude=excluded).provider
            try:
                return self._run_with_provider(
                    provider,
                    cmds,
                    bufsize=bufsize,
                    executable=executable,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=cwd,
                    env=env,
                    capture_output=capture_output,
                    check=check,
                    encoding=encoding,
                    errors=errors,
                    input=input,
                    timeout=timeout,
                    text=text,
                )
            except OperationNotStarted as exc:
                # Providers may be retried only when they prove no operation
                # was dispatched; planning is repeated for the next provider.
                self._executor_selector.decline(provider.name, str(exc), cause=exc)
                excluded.append(provider.name)

    @staticmethod
    def _shell_context(provider, cwd, env) -> _ShellContext:
        """Which of cwd/env the shell must embed for this provider."""
        return _ShellContext(
            None if "cwd" in provider.capabilities else cwd,
            None if "env" in provider.capabilities else env,
            "manages_status" in provider.capabilities,
        )

    def _execute_command_line(
        self, provider, flavour, cmds, *, executable, context, options
    ):
        """Render one finalized command line and submit it.

        The one place a `ShellCommand` is turned into a dispatch: it carries
        an out-of-band environment for providers that cannot embed one, and
        it is the only path that needs the login-shell layer.
        """
        rendered = flavour.command(
            cmds,
            executable=executable,
            cwd=context.cwd,
            env=context.env,
        )
        return provider.execute(
            _for_login_shell(flavour, rendered.command, provider),
            **self._out_of_band_env(provider, rendered, options),
        )

    def _run_with_provider(self, provider, cmds, **kwargs):
        self._ensure_provider_connected(provider)
        cwd = kwargs.get("cwd")
        env = kwargs.get("env")
        options = dict(kwargs)
        options.pop("executable", None)
        options.pop("cwd", None)
        options.pop("env", None)
        if cwd is not None and "cwd" in provider.capabilities:
            options["cwd"] = cwd
        if env is not None and "env" in provider.capabilities:
            options["env"] = env
        direct = starts_direct_command(cmds)
        if direct is not None:
            command, args = direct
            if kwargs.get("executable") is not None:
                raise NotImplementedError(
                    "executable cannot be combined with a direct command"
                )
            # `cwd`/`env` were stripped from `options` above and put back only
            # for a provider that carries them natively. Whatever is left must
            # be embedded in a rendered script, or the call silently runs in
            # the wrong directory with the wrong environment -- which is what
            # `Exec` over the shipped SSH provider (no capabilities at all)
            # used to do.
            native_cwd = cwd is None or "cwd" in provider.capabilities
            native_env = env is None or "env" in provider.capabilities
            if "args" in provider.capabilities and native_cwd and native_env:
                return provider.execute(command, *args, **options)
            if self._shell is None and self._shell_resolver is None:
                if args:
                    raise NotImplementedError(
                        f"executor provider {provider.name!r} does not support argv arguments"
                    )
                if not (native_cwd and native_env):
                    raise NotImplementedError(
                        f"executor provider {provider.name!r} cannot apply cwd or env "
                        "and no shell is configured to embed them"
                    )
                return provider.execute(command_text(command), **options)
            flavour = self.shell_flavour
            shell_executable = getattr(provider, "shell_executable", None)
            context = self._shell_context(provider, cwd, env)
            if "script" in provider.capabilities:
                script = flavour.script(
                    ((command, *args),),
                    cwd=context.cwd,
                    env=context.env,
                    for_session=context.for_session,
                )
                return provider.execute(script, **options)
            if "args" in provider.capabilities:
                leading = _shell_invocation(
                    flavour,
                    ((command, *args),),
                    cwd=context.cwd,
                    env=context.env,
                    for_session=context.for_session,
                    executable=shell_executable,
                    provider=provider,
                )
                return provider.execute(leading[0], *leading[1:], **options)
            if not args and native_cwd and native_env:
                # Nothing to embed. Send the program itself when it needs no
                # quoting -- a provider that is not a shell (a bare callable,
                # a container exec) must not be handed `sh -c ...`. A program
                # whose text would not survive a shell verbatim still goes
                # through the flavour below, because the one provider shape
                # that reaches here -- an SSH exec request -- is read by the
                # remote login shell, which word-split a spaced path.
                text = command_text(command)
                if flavour.quote(text) == text:
                    return provider.execute(text, **options)
            return self._execute_command_line(
                provider,
                flavour,
                ((command, *args),),
                executable=shell_executable,
                context=context,
                options=options,
            )

        if self._shell is None and self._shell_resolver is None:
            raise NotImplementedError(
                "buffered run requires a shell or a direct executable"
            )
        flavour = self.shell_flavour
        executable = kwargs.get("executable")
        context = self._shell_context(provider, cwd, env)
        if "script" in provider.capabilities:
            if executable is not None:
                raise NotImplementedError(
                    f"script executor provider {provider.name!r} "
                    "does not accept an executable override"
                )
            script = flavour.script(
                cmds,
                cwd=context.cwd,
                env=context.env,
                for_session=context.for_session,
            )
            return provider.execute(script, **options)
        shell_executable = executable or getattr(provider, "shell_executable", None)
        if "args" not in provider.capabilities:
            return self._execute_command_line(
                provider,
                flavour,
                cmds,
                executable=shell_executable,
                context=context,
                options=options,
            )
        # One shell layer, exactly as LocalHost renders it. `flavour.command()`
        # already contains the shell invocation, so feeding *that* to
        # `invocation()` -- whose argument is a script -- ran the target shell
        # twice: on Windows the outer PowerShell re-parsed the inner
        # `-Command "...; exit $LASTEXITCODE"` as its own double-quoted string,
        # so the status never came back and `check=True` passed for a command
        # that failed.
        leading = _shell_invocation(
            flavour,
            cmds,
            cwd=context.cwd,
            env=context.env,
            for_session=context.for_session,
            executable=shell_executable,
            provider=provider,
        )
        return provider.execute(leading[0], *leading[1:], **options)

    def _note_provider_in_use(self, provider) -> None:
        """Mark a provider's transport as live again, without connecting it.

        `close()` skips a transport it has already closed; a transport that
        reconnects lazily behind an operation would otherwise never be closed
        again.
        """
        with self._lifecycle_lock:
            target = getattr(provider, "transport", provider)
            self._closed_targets.discard(id(target))

    def _ensure_provider_connected(self, provider):
        # The check and the append must be one atomic step; see the lock's
        # rationale in __init__.  Membership is tested by identity because a
        # provider is a live object, not a value.
        with self._lifecycle_lock:
            if any(item is provider for item in self._connected_providers):
                # A kept provider after `replace_providers()` is already
                # connected but belongs to a new generation.
                self._run_initializer_locked([provider])
                return
            connect = getattr(provider, "connect", None)
            if connect is not None:
                connect()
            target = getattr(provider, "transport", provider)
            self._closed_targets.discard(id(target))
            self._connected_providers.append(provider)
            self._run_initializer_locked([provider])

    def _select_capable(self, capability: str, operation: str):
        """Select an executor provider offering `capability`.

        `run()` and `path()` report an unsupported operation as
        `NotImplementedError`, which is the documented contract. `spawn()` and
        `runspace()` went straight to `select()`, whose "no provider is
        available" is an `OperationNotStarted` -- a RuntimeError -- so a caller
        catching NotImplementedError to fall back crashed instead.
        """
        try:
            return self._executor_selector.select(capability=capability).provider
        except OperationNotStarted as exc:
            raise NotImplementedError(
                f"{type(self).__name__} does not provide the {operation!r} capability"
            ) from exc

    def spawn(self, *cmds, **options):
        # Filtered on the capability, as runspace() already was: selecting
        # blind meant a first provider without sessions ended the search, so a
        # WindowsHost composed as (winrm, ssh) could not open an SSH session
        # at all.
        provider = self._select_capable("spawn", "spawn")
        self._ensure_provider_connected(provider)
        spawn = getattr(provider, "spawn", None)
        if spawn is None:
            raise NotImplementedError(
                f"executor provider {provider.name!r} does not support sessions"
            )
        return spawn(*cmds, **options)

    def runspace(self):
        """Return a provider-owned typed runspace when one is available."""
        provider = self._select_capable("runspace", "runspace")
        self._ensure_provider_connected(provider)
        method = getattr(provider, "runspace", None)
        if method is None:
            raise NotImplementedError(
                f"executor provider {provider.name!r} does not support runspaces"
            )
        return method()


class PosixHost(SystemHost):
    """A POSIX system: POSIX-shell command construction and POSIX paths.

    Compose it from providers directly, or open an :class:`SshConfig`.
    """

    system_family = "posix"
    default_shell = POSIX_SHELL


class WindowsHost(SystemHost):
    """A Windows system: PowerShell command construction and Windows paths.

    Compose it from providers directly, or open a :class:`WinRMConfig`.
    """

    system_family = "windows"
    default_shell = POWERSHELL


class IosHost(SystemHost):
    """A Cisco IOS-style device: session and command only, by design.

    It configures **no shell flavour and no path grammar**, which is a
    deliberate gap rather than an unfinished one: IOS command syntax is
    not a shell, and quoting it as one would corrupt ordinary commands.
    So commands must be :class:`Exec` (or raw text the device
    understands), and :meth:`path` raises ``NotImplementedError`` until
    an IOS path grammar exists.
    """

    system_family = "ios"
    default_shell = None


class PosixConfig(SystemConfig, schemes=("posix", "posix+ssh")):
    """Connection identity for a :class:`PosixHost` (`posix:`, `posix+ssh:`)."""

    host_type = PosixHost
    uri_scheme = "posix"


class WindowsConfig(SystemConfig, schemes=("windows", "windows+winrm")):
    """Connection identity for a :class:`WindowsHost` (`windows:`,
    `windows+winrm:`)."""

    host_type = WindowsHost
    uri_scheme = "windows"


class IosConfig(SystemConfig, schemes=("ios",)):
    """Connection identity for an :class:`IosHost` (`ios:`), which is
    session- and command-only."""

    host_type = IosHost
    uri_scheme = "ios"


# Each host family and its configuration are mutually referential, so the
# host -> config direction is bound here, once both sides are defined.
PosixHost.config_type = PosixConfig
WindowsHost.config_type = WindowsConfig
IosHost.config_type = IosConfig
