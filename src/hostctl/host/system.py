"""Operating-system hosts composed from ordered transport providers."""

from __future__ import annotations

import dataclasses
import subprocess
import typing
from urllib.parse import parse_qsl, quote, urlencode

from pathlib_next import Path

from ..provider import (
    ExecutorProvider,
    OperationNotStarted,
    PathProvider,
    ProviderSelector,
    ProviderSelection,
    SessionInitializer,
)
from ..shell import POWERSHELL, POSIX_SHELL, ShellFlavour, shell_flavour
from ._common import (
    Host,
    HostConfig,
    HostInfo,
    PathLike,
    Command,
    starts_direct_command,
)
from .composite_path import CompositePosixPath, CompositeWindowsPath

_SYSTEM_PROVIDER_RESOLVERS = {}


def register_system_provider(name: str, resolver) -> None:
    """Register a provider descriptor resolver for :class:`SystemConfig`."""
    key = str(name).casefold().strip()
    if not key or not callable(resolver):
        raise ValueError("provider name and callable resolver are required")
    _SYSTEM_PROVIDER_RESOLVERS[key] = resolver


def _provider_option(config: "SystemConfig", name: str):
    values = config.options.get("provider_options", {})
    if isinstance(values, dict):
        return values.get(name)
    return None


def _local_provider(config, kind):
    del config
    if kind == "executor":
        from ..executor import LocalExecutor

        return ExecutorProvider("local", LocalExecutor())
    from pathlib_next import Path as LocalPath

    return PathProvider("local", lambda *parts: LocalPath(*parts))


def _ssh_provider(config, kind):
    from ._ssh import SftpPathProvider, SshConfig, SshExecutorProvider, _SshTransport

    value = _provider_option(config, "ssh")
    if not isinstance(value, SshConfig):
        raise ValueError(
            "system SSH descriptors require provider_options={'ssh': SshConfig(...)}"
        )
    transports = getattr(config, "_provider_transports", {})
    transport = transports.get("ssh")
    if transport is None:
        transport = _SshTransport(value)
        transports["ssh"] = transport
        config._provider_transports = transports
    if kind == "executor":
        return SshExecutorProvider(transport)
    return SftpPathProvider(transport)


def _winrm_provider(config, kind):
    from ._winrm import (
        WinRMConfig,
        WinRMExecutorProvider,
        WinRMPathProvider,
        _WinRMTransport,
    )

    value = _provider_option(config, "winrm")
    if not isinstance(value, WinRMConfig):
        raise ValueError(
            "system WinRM descriptors require provider_options={'winrm': WinRMConfig(...)}"
        )
    transports = getattr(config, "_provider_transports", {})
    transport = transports.get("winrm")
    if transport is None:
        transport = _WinRMTransport(value)
        transports["winrm"] = transport
        config._provider_transports = transports
    if kind == "executor":
        return WinRMExecutorProvider(transport)
    return WinRMPathProvider(transport)


register_system_provider("local", _local_provider)
register_system_provider("ssh", _ssh_provider)
register_system_provider("sftp", _ssh_provider)
register_system_provider("winrm", _winrm_provider)


class SystemConfig(HostConfig):
    """Configuration for a logical system with one or more providers."""

    scheme = "system"

    def __init__(
        self,
        authority: str = "localhost",
        *,
        shell: object = None,
        executor: typing.Iterable[str] = (),
        path: typing.Iterable[str] = (),
        **options: object,
    ):
        super().__init__()
        self.authority = authority or "localhost"
        self.shell = shell
        self.executors = tuple(executor)
        self.paths = tuple(path)
        self.options = dict(options)
        self._provider_transports = {}

    def _build_providers(self, kind: str):
        descriptors = self.executors if kind == "executor" else self.paths
        result = []
        for descriptor in descriptors:
            key = str(descriptor).casefold()
            resolver = _SYSTEM_PROVIDER_RESOLVERS.get(key)
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
        query: list[tuple[str, str]] = [("executor", value) for value in self.executors]
        query += [("path", value) for value in self.paths]
        if self.shell is not None:
            query.append(("shell", getattr(self.shell, "name", str(self.shell))))
        return f"{self.scheme}://{quote(self.authority, safe='')}" + (
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
        values = dict(parse_qsl(parsed.query, keep_blank_values=True))
        executors = tuple(
            v
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k == "executor"
        )
        paths = tuple(
            v for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k == "path"
        )
        return cls(
            parsed.netloc or parsed.path or "localhost",
            shell=values.get("shell"),
            executor=executors,
            path=paths,
            **constructor_only,
        )

    def _create_host(self):
        return self.host_type(
            self,
            executor_providers=self._build_providers("executor"),
            path_providers=self._build_providers("path"),
        )


class SystemHost(Host):
    """Host orchestration shared by POSIX, Windows, and IOS systems."""

    system_family = "generic"
    default_shell: ShellFlavour | None = None

    def __init__(
        self,
        config: SystemConfig | None = None,
        *,
        executor_providers=(),
        path_providers=(),
        shell=None,
        info: HostInfo | None = None,
        initializer=None,
    ):
        self.config = config or SystemConfig()
        if config is None and self.system_family != "generic":
            self.config.scheme = self.system_family
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
        self._connected = False
        self._connected_providers = []
        self._closed_targets = set()

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
                values.update(provider.capabilities)
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
                capabilities = probe.capabilities or provider.capabilities
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
        if any(
            probe.usable and "runspace" in (probe.capabilities | provider.capabilities)
            for provider, probe in executor_probes
        ):
            values.add("runspace")
        if any(probe.usable for _, probe in path_probes):
            values.add("path")
        return frozenset(values)

    @property
    def executor(self):
        selected = self._executor_selector.select()
        return selected.provider

    def connect(self):
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
        if self._initializer is not None and not self._initializer_generation:
            try:
                initializer = self._initializer
                if isinstance(initializer, SessionInitializer):
                    initializer(self)
                else:
                    initializer(self)
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
        self._connected = True

    def close(self):
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
            if not self._provider_probe(self._executor_selector, provider).usable:
                continue
            callback = getattr(provider, "info", None)
            if callback is None:
                continue
            try:
                self._ensure_provider_connected(provider)
                value = callback()
            except (OperationNotStarted, ConnectionError, TimeoutError, OSError):
                continue
            except Exception:
                continue
            if not isinstance(value, HostInfo):
                continue
            for name, item in dataclasses.asdict(value).items():
                if fields[name] is None and item is not None:
                    fields[name] = item
        hostname = getattr(self.config, "authority", None) or getattr(
            self.config, "host", None
        )
        if fields["hostname"] is None:
            fields["hostname"] = hostname
        if fields["os_family"] is None:
            fields["os_family"] = self.system_family
        return HostInfo(**fields)

    def path(self, *segments: PathLike, backend: str | None = None) -> Path:
        if not self._path_selector.providers:
            raise NotImplementedError(
                f"{type(self).__name__} does not provide the 'path' capability"
            )
        if backend is None:
            selected = self._path_selector.select()
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
            probe = provider.probe()
            if not probe.usable:
                raise OperationNotStarted(f"path provider {backend!r} is unavailable")
            selected = ProviderSelection(
                provider,
                (
                    {
                        "provider": backend,
                        "availability": probe.availability,
                        "chosen": True,
                    },
                ),
            )
        try:
            value = selected.provider.path(*segments)
            path_type = (
                CompositeWindowsPath
                if self.system_family == "windows"
                else CompositePosixPath
            )
            return path_type.from_path(
                value,
                selected.provider,
                selected.provider.path,
                self._path_selector.providers,
                self._path_selector,
                logical_segments=segments,
            )
        except OperationNotStarted as exc:
            if backend is not None:
                raise
            self._path_selector.decline(selected.provider.name, str(exc))
            fallback = self._path_selector.select(
                exclude=(selected.provider.name,)
            ).provider
            value = fallback.path(*segments)
            path_type = (
                CompositeWindowsPath
                if self.system_family == "windows"
                else CompositePosixPath
            )
            return path_type.from_path(
                value,
                fallback,
                fallback.path,
                self._path_selector.providers,
                self._path_selector,
                logical_segments=segments,
            )

    def run(
        self,
        *cmds: Command,
        bufsize=-1,
        executable=None,
        stdin=None,
        stdout=None,
        stderr=None,
        cwd=None,
        env=None,
        capture_output=True,
        check=True,
        encoding=None,
        errors=None,
        input=None,
        timeout=None,
        text=None,
    ):
        if not self._executor_selector.providers:
            raise NotImplementedError(
                f"{type(self).__name__} does not provide the 'run' capability"
            )
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
                self._executor_selector.decline(provider.name, str(exc))
                excluded.append(provider.name)

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
            if "args" in provider.capabilities:
                return provider.execute(command, *args, **options)
            if args and self._shell is None and self._shell_resolver is None:
                raise NotImplementedError(
                    f"executor provider {provider.name!r} does not support argv arguments"
                )
            flavour = self.shell_flavour
            if "script" in provider.capabilities:
                script = flavour.script(
                    ((command, *args),),
                    cwd=None if "cwd" in provider.capabilities else cwd,
                    env=None if "env" in provider.capabilities else env,
                    for_session="manages_status" in provider.capabilities,
                )
                return provider.execute(script, **options)
            if not args:
                return provider.execute(str(command), **options)
            shell_executable = getattr(provider, "shell_executable", None)
            rendered = flavour.command(
                ((command, *args),),
                executable=shell_executable,
                cwd=None if "cwd" in provider.capabilities else cwd,
                env=None if "env" in provider.capabilities else env,
            )
            if "args" not in provider.capabilities:
                return provider.execute(rendered.command, **options)
            invocation = flavour.invocation(
                rendered.command, executable=shell_executable
            )
            return provider.execute(invocation[0], *invocation[1:], **options)

        if self._shell is None and self._shell_resolver is None:
            raise NotImplementedError(
                "buffered run requires a shell or a direct executable"
            )
        flavour = self.shell_flavour
        executable = kwargs.get("executable")
        if "script" in provider.capabilities:
            if executable is not None:
                raise NotImplementedError(
                    f"script executor provider {provider.name!r} "
                    "does not accept an executable override"
                )
            script = flavour.script(
                cmds,
                cwd=None if "cwd" in provider.capabilities else cwd,
                env=None if "env" in provider.capabilities else env,
                for_session="manages_status" in provider.capabilities,
            )
            return provider.execute(script, **options)
        shell_executable = executable or getattr(provider, "shell_executable", None)
        rendered = flavour.command(
            cmds,
            executable=shell_executable,
            cwd=None if "cwd" in provider.capabilities else cwd,
            env=None if "env" in provider.capabilities else env,
        )
        if "args" not in provider.capabilities:
            return provider.execute(rendered.command, **options)
        invocation = flavour.invocation(rendered.command, executable=shell_executable)
        return provider.execute(invocation[0], *invocation[1:], **options)

    def _ensure_provider_connected(self, provider):
        if provider in self._connected_providers:
            return
        connect = getattr(provider, "connect", None)
        if connect is not None:
            connect()
        target = getattr(provider, "transport", provider)
        self._closed_targets.discard(id(target))
        self._connected_providers.append(provider)

    def spawn(self, *cmds, **options):
        provider = self._executor_selector.select().provider
        spawn = getattr(provider, "spawn", None)
        if spawn is None:
            raise NotImplementedError(
                f"executor provider {provider.name!r} does not support sessions"
            )
        return spawn(*cmds, **options)

    def runspace(self):
        """Return a provider-owned typed runspace when one is available."""
        selected = self._executor_selector.select(capability="runspace")
        provider = selected.provider
        method = getattr(provider, "runspace", None)
        if method is None:
            raise NotImplementedError(
                f"executor provider {provider.name!r} does not support runspaces"
            )
        return method()


class PosixHost(SystemHost):
    system_family = "posix"
    default_shell = POSIX_SHELL

    @classmethod
    def from_ssh(cls, config):
        """Compose POSIX semantics over an existing :class:`SshConfig`."""
        host = config._create_host()
        if not isinstance(host, cls):
            raise ValueError(
                f"SSH configuration selects {type(host).__name__}, not {cls.__name__}"
            )
        return host


class WindowsHost(SystemHost):
    system_family = "windows"
    default_shell = POWERSHELL

    @classmethod
    def from_winrm(cls, config):
        """Compose Windows semantics over an existing :class:`WinRMConfig`."""
        host = config._create_host()
        if not isinstance(host, cls):
            raise ValueError(
                f"WinRM configuration selects {type(host).__name__}, not {cls.__name__}"
            )
        return host


class IosHost(SystemHost):
    system_family = "ios"
    default_shell = None


for _cls, _scheme in ((PosixHost, "posix"), (WindowsHost, "windows"), (IosHost, "ios")):
    _cls.__name__  # keep lint tools from treating the loop as accidental


class PosixConfig(SystemConfig, schemes=("posix", "posix+ssh")):
    host_type = PosixHost
    scheme = "posix"


class WindowsConfig(SystemConfig, schemes=("windows", "windows+winrm")):
    host_type = WindowsHost
    scheme = "windows"


class IosConfig(SystemConfig, schemes=("ios",)):
    host_type = IosHost
    scheme = "ios"
