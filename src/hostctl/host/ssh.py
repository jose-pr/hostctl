"""SSH host implementation."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import typing
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import quote, unquote, urlencode

from pathlib_next import Pathname, PosixPathname, WindowsPathname

from ..executor import SshConnection, SshExecutor
from ..process import Process, SshProcess, TerminalRequest
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
    parse_host_info,
    starts_direct_command,
    strict_uri_credentials,
    strict_uri_query,
    uri_host,
    reject_stdin_conflict,
)
from ..shell import (
    BASH,
    CMD,
    FISH,
    POWERSHELL,
    POSIX_SHELL,
    PWSH,
    ZSH,
    ShellFlavour,
    ShellFlavourSelection,
    shell_flavour,
)

SshKeySource = typing.Union[str, bytes, os.PathLike[str]]
SshClientKeys = typing.Optional[
    typing.Union[SshKeySource, typing.Sequence[SshKeySource]]
]
SshKnownHosts = typing.Optional[
    typing.Union[SshKeySource, typing.Sequence[SshKeySource]]
]
PathnameConstructor = typing.Type[typing.Union[PurePath, Pathname]]
SshShellSelection = typing.Union[ShellFlavourSelection, typing.Literal["auto"]]


def _path_flavor_from_connection_string(value: str) -> PathnameConstructor:
    try:
        return {
            "posix": PosixPathname,
            "windows": WindowsPathname,
        }[value]
    except KeyError as exc:
        raise ValueError(
            "path_flavor must be 'posix' or 'windows' in a connection string"
        ) from exc


@dataclasses.dataclass
class SshConfig(HostConfig, schemes=("ssh",)):
    """Explicit SSH transport, authentication, and target-shell settings."""

    host: str
    port: int = 22
    username: str = "root"
    password: typing.Optional[str] = dataclasses.field(default=None, repr=False)
    client_keys: SshClientKeys = dataclasses.field(default=None, repr=False)
    executable: typing.Optional[str] = None
    known_hosts: SshKnownHosts = ()
    dialect: SshShellSelection = POSIX_SHELL
    path_flavor: PathnameConstructor = PosixPathname

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.host:
            raise ValueError("host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.dialect != "auto":
            self.dialect = shell_flavour(self.dialect)
        elif self.executable is not None:
            raise ValueError("executable cannot be combined with dialect='auto'")
        if not isinstance(self.path_flavor, type) or not issubclass(
            self.path_flavor, (Pathname, PurePath)
        ):
            raise TypeError("path_flavor must be a pure-path class")
        if self.path_flavor is PurePath:
            raise TypeError(
                "bare PurePath uses the local OS; choose a concrete path flavor"
            )
        if not issubclass(self.path_flavor, (PurePosixPath, PureWindowsPath)):
            raise TypeError("path_flavor must use POSIX or Windows path semantics")

    def connect_opts(self) -> typing.Dict[str, object]:
        opts: typing.Dict[str, object] = {"username": self.username or "root"}
        if self.password is not None:
            opts["password"] = self.password
        if self.client_keys is not None:
            keys = self.client_keys
            if isinstance(keys, (str, bytes, os.PathLike)):
                keys = [keys]
            opts["client_keys"] = keys
        if self.known_hosts != ():
            opts["known_hosts"] = self.known_hosts
        return opts

    @property
    def connection_uri(self) -> str:
        user = f"{quote(self.username, safe='')}@" if self.username else ""
        path_flavor = (
            "windows" if issubclass(self.path_flavor, PureWindowsPath) else "posix"
        )
        query = {"dialect": self.dialect, "path_flavor": path_flavor}
        if self.executable:
            query["executable"] = self.executable
        return (
            f"ssh://{user}{uri_host(self.host)}:{self.port or 22}"
            f"?{urlencode(query)}"
        )

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> SshConfig:
        strict_uri_credentials(credentials, ("password", "client_keys", "known_hosts"))
        query = strict_uri_query(parsed, {"dialect", "path_flavor", "executable"})
        if not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("SSH URI requires a host and no path")
        return cls(
            host=parsed.hostname,
            port=parsed.port or 22,
            username=unquote(parsed.username or "") or "root",
            password=typing.cast(typing.Optional[str], credentials.get("password")),
            client_keys=typing.cast(SshClientKeys, credentials.get("client_keys")),
            executable=query.get("executable") or None,
            known_hosts=typing.cast(SshKnownHosts, credentials.get("known_hosts", ())),
            dialect=(
                "auto"
                if query.get("dialect") == "auto"
                else shell_flavour(query.get("dialect", POSIX_SHELL.name))
            ),
            path_flavor=_path_flavor_from_connection_string(
                query.get("path_flavor", "posix")
            ),
        )

    def _create_host(self) -> SshHost:
        return SshHost(self)


class SshHost(Host):
    """A host reached over SSH, with an explicitly configured command dialect."""

    def __init__(self, config: SshConfig) -> None:
        self.config = config
        self._ssh: typing.Optional[SshConnection] = None
        self._executor = SshExecutor(lambda: self.ssh)
        self._resolved_dialect: typing.Optional[
            typing.Tuple[typing.Tuple[object, ...], ShellFlavour]
        ] = None

    @property
    def capabilities(self) -> typing.FrozenSet[str]:
        return frozenset(("run", "path", "spawn", "tty"))

    @property
    def shell_flavour(self) -> ShellFlavour:
        selection = self.config.dialect
        if selection != "auto":
            return typing.cast(ShellFlavour, selection)
        key = (selection, self.config.path_flavor, self.config.executable)
        if self._resolved_dialect is not None and self._resolved_dialect[0] == key:
            return self._resolved_dialect[1]
        resolved = (
            self._detect_windows_shell()
            if issubclass(self.config.path_flavor, PureWindowsPath)
            else self._detect_posix_shell()
        )
        self._resolved_dialect = (key, resolved)
        return resolved

    def _detect_posix_shell(self) -> ShellFlavour:
        result = self.executor(
            "printf '%s\\n' \"$SHELL\"",
            check=False,
            text=True,
            timeout=5,
        )
        if result.returncode:
            raise RuntimeError("unable to detect the remote POSIX login shell")
        name = (result.stdout or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
        try:
            return {
                "sh": POSIX_SHELL,
                "dash": POSIX_SHELL,
                "bash": BASH,
                "zsh": ZSH,
                "fish": FISH,
            }[name.casefold()]
        except KeyError as exc:
            raise RuntimeError(
                f"unsupported or undetected remote POSIX shell: {name or '<empty>'}"
            ) from exc

    def _detect_windows_shell(self) -> ShellFlavour:
        probes = (
            (
                PWSH,
                "pwsh -NoProfile -NonInteractive -Command "
                '"Write-Output HOSTCTL_PWSH_7"',
                "HOSTCTL_PWSH_7",
            ),
            (
                POWERSHELL,
                "powershell.exe -NoProfile -NonInteractive -Command "
                '"Write-Output HOSTCTL_POWERSHELL_5"',
                "HOSTCTL_POWERSHELL_5",
            ),
            (CMD, 'cmd.exe /d /s /c "echo HOSTCTL_CMD"', "HOSTCTL_CMD"),
        )
        for flavour, command, marker in probes:
            try:
                result = self.executor(
                    command,
                    check=False,
                    text=True,
                    timeout=5,
                )
            except subprocess.TimeoutExpired:
                continue
            if result.returncode == 0 and (result.stdout or "").strip() == marker:
                return flavour
        raise RuntimeError("unable to detect a supported remote Windows shell")

    @property
    def executor(self) -> SshExecutor:
        return self._executor

    def info(self) -> HostInfo:
        flavour = self.shell_flavour
        result = self.run(
            flavour.info_script,
            check=False,
            encoding="utf-8",
        )
        return parse_host_info(result.stdout)

    @property
    def ssh(self) -> SshConnection:
        """The lazily opened and reused asyncssh connection."""
        if self._ssh is None or self._ssh.is_closed():
            from .. import _async

            try:
                self._ssh = _async.async_to_sync(
                    _async.asyncssh().connect(
                        self.config.host,
                        port=self.config.port or 22,
                        **self.config.connect_opts(),
                    )
                )
            except Exception as exc:
                normalized = _async.normalize_asyncssh_error(exc)
                if normalized is exc:
                    raise
                raise normalized from exc
        return self._ssh

    def connect(self) -> None:
        _ = self.ssh

    def close(self) -> None:
        if self._ssh is None:
            return
        connection, self._ssh = self._ssh, None
        connection.close()
        wait_closed = getattr(connection, "wait_closed", None)
        if wait_closed is not None:
            from .. import _async

            _async.async_to_sync(wait_closed())

    def path(
        self, *segments: PathLike, backend: typing.Optional[str] = None
    ) -> HostPath:
        from pathlib_next.uri.schemes.sftp import AsyncsshSftpBackend, SftpPath

        remote_path = self.config.path_flavor(*segments).as_posix()
        if not remote_path.startswith("/"):
            remote_path = "/" + remote_path
        sftp_backend = AsyncsshSftpBackend(connect_opts=self.config.connect_opts())
        return SftpPath(
            f"sftp://{self.config.host}:{self.config.port or 22}{remote_path}",
            backend=sftp_backend,
        )

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
        reject_stdin_conflict(input, stdin)
        direct = starts_direct_command(cmds)
        if direct is not None:
            command, args = direct
            cmds = ((command, *args),)
        if text and encoding is None:
            encoding = "utf-8"
        shell_command = self.shell_flavour.command(
            cmds,
            executable=executable or self.config.executable,
            cwd=cwd,
            env=env,
        )
        remote_command = shell_command.command
        remote_env = shell_command.environment

        return self.executor(
            remote_command,
            bufsize=bufsize,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=remote_env,
            capture_output=capture_output,
            check=check,
            encoding=encoding,
            errors=errors,
            input=input,
            timeout=timeout,
            text=text,
        )

    def spawn(
        self,
        *cmds: Command,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        terminal: TerminalRequest = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> Process:
        """Start a persistent SSH process, optionally allocating a PTY."""
        if cmds:
            shell_command = self.shell_flavour.command(
                cmds,
                executable=executable or self.config.executable,
                cwd=cwd,
                env=env,
            )
            command: typing.Optional[str] = shell_command.command
            remote_env = shell_command.environment
        else:
            if cwd is not None or env is not None:
                raise ValueError("cwd and env require a command when spawning")
            command = executable
            remote_env = None

        from ..process import terminal_options

        selected_terminal = terminal_options(terminal)
        options: typing.Dict[str, object] = {"env": remote_env}
        if encoding is not None or errors is not None:
            options["encoding"] = encoding or "utf-8"
        if errors is not None:
            options["errors"] = errors
        if selected_terminal is not None:
            options.update(
                request_pty=True,
                term_type=selected_terminal.term_type,
                term_size=selected_terminal.size,
            )

        from .. import _async

        try:
            process = _async.async_to_sync(self.ssh.create_process(command, **options))
        except Exception as exc:
            normalized = _async.normalize_asyncssh_error(exc, command=command)
            if normalized is exc:
                raise
            raise normalized from exc
        return SshProcess(typing.cast(typing.Any, process), command)
