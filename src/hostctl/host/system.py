"""Operating-system hosts composed from ordered transport providers."""

from __future__ import annotations

import dataclasses
import platform
import subprocess
import typing
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from pathlib import PurePath
from pathlib_next import Path

from ..provider import (
    ExecutorProvider,
    OperationNotStarted,
    PathProvider,
    ProviderSelector,
    ProviderSelection,
)
from ..shell import CMD, POWERSHELL, POSIX_SHELL, ShellFlavour, shell_flavour
from ._common import (
    Host,
    HostConfig,
    HostInfo,
    PathLike,
    Command,
    Environment,
    FileHandle,
    Input,
    CaptureOutput,
    starts_direct_command,
    normalize_os_family,
)
from .composite_path import CompositePosixPath, CompositeWindowsPath


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
        if credentials:
            raise ValueError("system credentials must be supplied to the constructor")
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
        )

    def _create_host(self):
        return self.host_type(self)


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
            values.update(provider.capabilities)
        return frozenset(values)

    @property
    def capabilities(self):
        values = set()
        if self._executor_selector.providers:
            values.add("run")
        if any(
            "runspace" in provider.capabilities
            for provider in self._executor_selector.providers
        ):
            values.add("runspace")
        if self._path_selector.providers:
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
                try:
                    provider = selector.select().provider
                except OperationNotStarted:
                    continue
                connect = getattr(provider, "connect", None)
                if connect:
                    connect()
                target = getattr(provider, "transport", provider)
                self._closed_targets.discard(id(target))
                connected.append(provider)
        except BaseException:
            for provider in reversed(connected):
                close = getattr(provider, "close", None)
                if close:
                    close()
            raise
        self._connected_providers = connected
        self._connected = True

    def close(self):
        targets = []
        for provider in (
            *self._connected_providers,
            *self._executor_selector.providers,
            *self._path_selector.providers,
        ):
            target = getattr(provider, "transport", provider)
            if target not in targets:
                targets.append(target)
        for target in reversed(targets):
            if id(target) in self._closed_targets:
                continue
            close = getattr(target, "close", None)
            if close:
                close()
            self._closed_targets.add(id(target))
        self._connected = False
        self._connected_providers = []
        self._executor_selector.invalidate()
        self._path_selector.invalidate()
        if self._shell_resolver is not None:
            self._shell = None

    def info(self) -> HostInfo:
        if self._info is not None:
            return self._info
        if self._connected:
            try:
                provider = self._executor_selector.select().provider
                self._ensure_provider_connected(provider)
                callback = getattr(provider, "info", None)
                if callback is not None:
                    return callback()
            except Exception:
                pass
        hostname = getattr(self.config, "authority", None) or getattr(
            self.config, "host", None
        )
        return HostInfo(hostname=hostname, os_family=self.system_family)

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
        except OperationNotStarted:
            if backend is not None:
                raise
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
            except OperationNotStarted:
                # Providers may be retried only when they prove no operation
                # was dispatched; planning is repeated for the next provider.
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
            if not args:
                return provider.execute(str(command), **options)
            if self._shell is None and self._shell_resolver is None:
                raise NotImplementedError(
                    f"executor provider {provider.name!r} does not support argv arguments"
                )
            flavour = self.shell_flavour
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
        return config._create_host()


class WindowsHost(SystemHost):
    system_family = "windows"
    default_shell = POWERSHELL

    @classmethod
    def from_winrm(cls, config):
        """Compose Windows semantics over an existing :class:`WinRMConfig`."""
        return config._create_host()


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
