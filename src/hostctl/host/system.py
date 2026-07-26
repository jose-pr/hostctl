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
from .composite_path import CompositePath


class SystemConfig(HostConfig):
    """Configuration for a logical system with one or more providers."""

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
        self._executor_selector = ProviderSelector(executor_providers)
        self._path_selector = ProviderSelector(path_providers)
        self._shell_resolver = (
            shell if callable(shell) and not isinstance(shell, type) else None
        )
        self._shell = (
            shell_flavour(shell)
            if shell is not None and self._shell_resolver is None
            else (
                shell_flavour(getattr(self.config, "shell", None))
                if getattr(self.config, "shell", None)
                else self.default_shell
            )
        )
        self._info = info
        self._connected = False

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
            for provider in (
                *self._executor_selector.providers,
                *self._path_selector.providers,
            ):
                connect = getattr(provider, "connect", None)
                if connect:
                    connect()
                connected.append(provider)
        except BaseException:
            for provider in reversed(connected):
                close = getattr(provider, "close", None)
                if close:
                    close()
            raise
        self._connected = True

    def close(self):
        if not self._connected:
            return
        for provider in reversed(
            (*self._executor_selector.providers, *self._path_selector.providers)
        ):
            close = getattr(provider, "close", None)
            if close:
                close()
        self._connected = False
        self._executor_selector.invalidate()
        self._path_selector.invalidate()

    def info(self) -> HostInfo:
        if self._info is not None:
            return self._info
        try:
            provider = self._executor_selector.select().provider
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
            return CompositePath.from_path(
                value,
                selected.provider,
                selected.provider.path,
                self._path_selector.providers,
            )
        except OperationNotStarted:
            if backend is not None:
                raise
            fallback = self._path_selector.select(
                exclude=(selected.provider.name,)
            ).provider
            value = fallback.path(*segments)
            return CompositePath.from_path(
                value, fallback, fallback.path, self._path_selector.providers
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
        selected = self._executor_selector.select()
        provider = selected.provider
        direct = starts_direct_command(cmds)
        options = dict(
            bufsize=bufsize,
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
        if cwd is not None and "cwd" in provider.capabilities:
            options["cwd"] = cwd
        if env is not None and "env" in provider.capabilities:
            options["env"] = env
        if direct is not None:
            command, args = direct
            if executable is not None:
                raise NotImplementedError(
                    "executable cannot be combined with a direct command"
                )
            if "args" not in provider.capabilities and args:
                if self._shell is None:
                    raise NotImplementedError(
                        f"executor provider {provider.name!r} does not support argv arguments"
                    )
                command = self.shell_flavour.command(
                    cmds,
                    executable=executable,
                    cwd=None if "cwd" in provider.capabilities else cwd,
                    env=None if "env" in provider.capabilities else env,
                )
                return provider.execute(command.command, **options)
            try:
                return provider.execute(command, *args, **options)
            except OperationNotStarted:
                # This is the sole safe replay point: the provider guarantees
                # that no remote operation was dispatched.
                fallback = self._executor_selector.select(
                    exclude=(provider.name,)
                ).provider
                return fallback.execute(command, *args, **options)
        if self._shell is None:
            raise NotImplementedError(
                "buffered run requires a shell or a direct executable"
            )
        native_cwd = cwd if "cwd" in provider.capabilities else None
        native_env = env if "env" in provider.capabilities else None
        flavour = self.shell_flavour
        command = flavour.command(
            cmds,
            executable=executable,
            cwd=None if native_cwd is not None else cwd,
            env=None if native_env is not None else env,
        )
        if native_cwd is not None:
            options["cwd"] = native_cwd
        if native_env is not None:
            options["env"] = native_env
        if "args" not in provider.capabilities:
            return provider.execute(command.command, **options)
        invocation = flavour.invocation(command.command, executable=executable)
        try:
            return provider.execute(invocation[0], *invocation[1:], **options)
        except OperationNotStarted:
            fallback = self._executor_selector.select(exclude=(provider.name,)).provider
            return fallback.execute(invocation[0], *invocation[1:], **options)

    def spawn(self, *cmds, **options):
        provider = self._executor_selector.select().provider
        spawn = getattr(provider, "spawn", None)
        if spawn is None:
            raise NotImplementedError(
                f"executor provider {provider.name!r} does not support sessions"
            )
        return spawn(*cmds, **options)


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
