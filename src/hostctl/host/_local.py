"""Local host implementation."""

from __future__ import annotations

import os as _os
import platform as _platform
import subprocess as _subprocess
import typing as _ty

from ..executor import LocalExecutor
from ..provider import (
    LocalExecutorProvider,
    LocalPathProvider,
    OperationNotStarted,
    ProviderSelector,
)
from ._common import (
    CaptureOutput,
    Command,
    Environment,
    FileHandle,
    Host,
    HostConfig,
    HostInfo,
    HostPath,
    Input,
    PathLike,
    starts_direct_command,
    normalize_os_family,
    strict_uri_credentials,
)
from ..shell import POSIX_SHELL, POWERSHELL, ShellFlavour


class LocalConfig(HostConfig, schemes=("local",)):
    def __init__(self) -> None:
        super().__init__()

    @property
    def connection_uri(self) -> str:
        return "local:"

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> LocalConfig:
        strict_uri_credentials(credentials, ())
        if parsed.netloc or parsed.path or parsed.query:
            raise ValueError("local URI must be exactly 'local:'")
        return cls()

    def _create_host(self) -> LocalHost:
        return LocalHost(self)


class LocalHost(Host):
    """A host whose commands and paths are local to this process.

    The public surface is unchanged, but execution and filesystem access are
    assembled from ordered providers (:class:`LocalExecutorProvider` and
    :class:`LocalPathProvider`) rather than a hard-wired executor.  A subclass
    may supply its own providers to reuse the local semantics over a different
    access mechanism.
    """

    def __init__(
        self,
        config: _ty.Optional[LocalConfig] = None,
        *,
        executor_providers: _ty.Iterable[object] = (),
        path_providers: _ty.Iterable[object] = (),
    ) -> None:
        self.config = config or LocalConfig()
        self._executor_provider_selector = ProviderSelector(
            tuple(executor_providers) or (LocalExecutorProvider(),)
        )
        self._path_provider_selector = ProviderSelector(
            tuple(path_providers) or (LocalPathProvider(),)
        )

    @property
    def executor_providers(self) -> _ty.Tuple[object, ...]:
        """The ordered command providers backing :meth:`run`."""
        return self._executor_provider_selector.providers

    @property
    def path_providers(self) -> _ty.Tuple[object, ...]:
        """The ordered filesystem providers backing :meth:`path`."""
        return self._path_provider_selector.providers

    @property
    def capabilities(self) -> _ty.FrozenSet[str]:
        values = set()
        if any(
            self._executor_provider_selector.probe(provider).usable
            for provider in self._executor_provider_selector.providers
        ):
            values.add("run")
        if any(
            self._path_provider_selector.probe(provider).usable
            for provider in self._path_provider_selector.providers
        ):
            values.add("path")
        return frozenset(values)

    @property
    def shell_flavour(self) -> ShellFlavour:
        if _os.name == "nt":
            return POWERSHELL
        if _os.name == "posix":
            return POSIX_SHELL
        raise NotImplementedError(f"unsupported local OS: {_os.name}")

    @property
    def executor(self) -> LocalExecutor:
        return self._executor_provider_selector.select().provider.executor

    def info(self) -> HostInfo:
        return HostInfo(
            hostname=_platform.node() or None,
            os_family=normalize_os_family(_platform.system()),
            os_name=_platform.system() or None,
            os_version=_platform.version() or None,
            architecture=_platform.machine() or None,
        )

    def path(self, *segments: PathLike, backend: _ty.Optional[str] = None) -> HostPath:
        names = tuple(
            provider.name for provider in self._path_provider_selector.providers
        )
        if backend is not None and backend not in names:
            raise ValueError(
                "local path backend must be "
                + " or ".join(repr(name) for name in names or ("local",))
            )
        if backend is None:
            provider = self._path_provider_selector.select().provider
        else:
            provider = next(
                item
                for item in self._path_provider_selector.providers
                if item.name == backend
            )
            if not provider.probe().usable:
                raise OperationNotStarted(f"path provider {backend!r} is unavailable")
        return provider.path(*(segments or (_os.getcwd(),)))

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
        excluded: _ty.List[str] = []
        while True:
            provider = self._executor_provider_selector.select(
                exclude=excluded
            ).provider
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
                # Only a proven pre-dispatch refusal may reach another
                # provider; a dispatched command is never replayed.
                self._executor_provider_selector.decline(provider.name, str(exc))
                excluded.append(provider.name)

    def _run_with_provider(
        self,
        provider,
        cmds: _ty.Sequence[Command],
        *,
        bufsize: int,
        executable: _ty.Optional[str],
        stdin,
        stdout,
        stderr,
        cwd,
        env,
        capture_output,
        check,
        encoding,
        errors,
        input,
        timeout,
        text,
    ) -> _subprocess.CompletedProcess:
        direct = starts_direct_command(cmds)
        if direct is not None:
            command, args = direct
            if executable is not None:
                raise NotImplementedError(
                    "executable cannot be combined with a direct command"
                )
            return provider.execute(
                command,
                *args,
                bufsize=bufsize,
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
        script = self.shell_flavour.script(cmds, cwd=None, env=None)
        invocation = self.shell_flavour.invocation(
            script,
            executable=executable,
        )
        return provider.execute(
            invocation[0],
            *invocation[1:],
            bufsize=bufsize,
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
