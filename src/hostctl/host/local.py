"""Local host implementation."""

from __future__ import annotations

import os as _os
import platform as _platform
import subprocess as _subprocess
import typing as _ty

from ..executor import LocalExecutor
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
    is_direct_command,
    normalize_os_family,
    strict_uri_credentials,
    strict_uri_query,
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
        strict_uri_query(parsed, ())
        return cls()

    def _create_host(self) -> LocalHost:
        return LocalHost(self)


class LocalHost(Host):
    """A host whose commands and paths are local to this process."""

    def __init__(self, config: _ty.Optional[LocalConfig] = None) -> None:
        self.config = config or LocalConfig()
        self._executor = LocalExecutor()

    @property
    def capabilities(self) -> _ty.FrozenSet[str]:
        return frozenset(("path", "run"))

    @property
    def shell_flavour(self) -> ShellFlavour:
        if _os.name == "nt":
            return POWERSHELL
        if _os.name == "posix":
            return POSIX_SHELL
        raise NotImplementedError(f"unsupported local OS: {_os.name}")

    @property
    def executor(self) -> LocalExecutor:
        return self._executor

    def info(self) -> HostInfo:
        return HostInfo(
            hostname=_platform.node() or None,
            os_family=normalize_os_family(_platform.system()),
            os_name=_platform.system() or None,
            os_version=_platform.version() or None,
            architecture=_platform.machine() or None,
        )

    def path(self, *segments: PathLike, backend: _ty.Optional[str] = None) -> HostPath:
        return HostPath(*segments) if segments else HostPath("/")

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
        if cmds and is_direct_command((cmds[0],)):
            return self.executor(
                cmds[0],
                *cmds[1:],
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
        return self.executor(
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
