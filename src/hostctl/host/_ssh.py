"""SSH host implementation."""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import threading
import typing
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import quote, unquote, urlencode

from pathlib_next import Pathname, PosixPathname, WindowsPathname

from ..executor import SshConnection, SshExecutor, wants_text
from ..provider import (
    ExecutorProvider,
    OperationNotStarted,
    PathProvider,
    ProviderProbe,
    ProviderSelector,
)

log = logging.getLogger("hostctl.host.ssh")

#: RFC 3986 `pchar`, plus `/`.  Everything legal in a URI path segment is
#: left as written -- including `:` so a Windows-flavoured remote path still
#: reads as `sftp://host:22/C:/Temp` -- while `%`, `?`, `#`, space, and
#: non-ASCII are percent-encoded, because those are what a URI parser would
#: otherwise take for syntax.
_URI_PATH_SAFE = "/:@-._~!$&'()*+,;="
from ..process import Process, SshProcess, TerminalRequest
from ._common import (
    CaptureOutput,
    Command,
    Environment,
    FileHandle,
    HostConfig,
    HostInfo,
    HostPath,
    Input,
    PathLike,
    parse_host_info,
    starts_direct_command,
    strict_uri_query,
    uri_host,
    uri_hostname,
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

    #: Declared rather than enforced inside `_from_parsed_uri`, so the
    #: whitelist can be read before a config is built: an ambient
    #: credential (the CLI's `HOSTCTL_PASSWORD`) is offered only where it
    #: is accepted. The base dispatcher enforces it.
    uri_credentials = ("password", "client_keys", "known_hosts")

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
        # Always passed, never omitted on the default. `()` is asyncssh's own
        # "resolve known_hosts the usual way" value, so the exec leg is
        # unchanged -- but pathlib_next's SFTP connect seeds `known_hosts=None`
        # (verification off) for options it is not given, so omitting the key
        # made the path leg accept any host key, including a substituted one,
        # and hand it the password. Opting out stays possible and explicit:
        # `known_hosts=None`.
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
        query = strict_uri_query(parsed, {"dialect", "path_flavor", "executable"})
        if not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("SSH URI requires a host and no path")
        return cls(
            host=uri_hostname(parsed),
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

    def _create_host(self):
        from .system import PosixHost, WindowsHost

        transport = self._create_transport()
        host_type = (
            WindowsHost if issubclass(self.path_flavor, PureWindowsPath) else PosixHost
        )
        return host_type(
            self,
            executor_providers=(SshExecutorProvider(transport),),
            path_providers=(SftpPathProvider(transport),),
            shell=(
                self.dialect
                if self.dialect != "auto"
                else (lambda: transport.shell_flavour)
            ),
        )

    def _create_transport(self) -> _SshTransport:
        """Create the private SSH service shared by executor/path providers."""
        return _SshTransport(self)


class _SshTransport:
    """A host reached over SSH, with an explicitly configured command dialect."""

    def __init__(self, config: SshConfig) -> None:
        self.config = config
        self._ssh: typing.Optional[SshConnection] = None
        self._ssh_lock = threading.RLock()
        #: The process the cached connection belongs to; see `ssh`.
        self._loop_generation: typing.Optional[int] = None
        self._executor = SshExecutor(lambda: self.ssh)
        self._resolved_dialect: typing.Optional[
            typing.Tuple[typing.Tuple[object, ...], ShellFlavour, typing.Optional[str]]
        ] = None
        self._sftp_backend: typing.Optional[object] = None

    @property
    def capabilities(self) -> typing.FrozenSet[str]:
        return frozenset(("run", "path", "spawn", "tty"))

    @property
    def shell_flavour(self) -> ShellFlavour:
        selection = self.config.dialect
        if selection != "auto":
            return typing.cast(ShellFlavour, selection)
        key = (selection, self.config.path_flavor, self.config.executable)
        with self._ssh_lock:
            if self._resolved_dialect is not None and self._resolved_dialect[0] == key:
                return self._resolved_dialect[1]
            resolved, executable = (
                self._detect_windows_shell()
                if issubclass(self.config.path_flavor, PureWindowsPath)
                else self._detect_posix_shell()
            )
            self._resolved_dialect = (key, resolved, executable)
            return resolved

    def _detect_posix_shell(self) -> typing.Tuple[ShellFlavour, str]:
        result = self.executor(
            "printf '%s\\n' \"$SHELL\"",
            check=False,
            text=True,
            timeout=5,
        )
        if result.returncode:
            raise RuntimeError("unable to detect the remote POSIX login shell")
        name = (result.stdout or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
        executable = (result.stdout or "").strip()
        try:
            return {
                "sh": POSIX_SHELL,
                "dash": POSIX_SHELL,
                "bash": BASH,
                "zsh": ZSH,
                "fish": FISH,
            }[name.casefold()], executable
        except KeyError as exc:
            raise RuntimeError(
                f"unsupported or undetected remote POSIX shell: {name or '<empty>'}"
            ) from exc

    def _detect_windows_shell(self) -> typing.Tuple[ShellFlavour, str]:
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
                executable = {
                    PWSH: "pwsh",
                    POWERSHELL: "powershell.exe",
                    CMD: "cmd.exe",
                }[flavour]
                return flavour, executable
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
        with self._ssh_lock:
            from .. import _async

            generation = _async.loop_generation()
            if self._loop_generation is None:
                self._loop_generation = generation
            elif self._loop_generation != generation:
                # `fork()`: this connection belongs to the parent's loop,
                # which does not exist here, and the child rebuilt its own.
                # Dropping WITHOUT closing is the only safe move -- closing
                # would run asyncssh's teardown against a socket the parent
                # still uses.
                self._ssh = None
                self._sftp_backend = None
                self._loop_generation = generation
            if self._ssh is None or self._ssh.is_closed():
                log.debug(
                    "opening SSH connection to %s",
                    ProviderSelector.redact(self.config.connection_uri),
                )
                try:
                    self._ssh = _async.async_to_sync(
                        _async.asyncssh().connect(
                            self.config.host,
                            port=self.config.port or 22,
                            **self.config.connect_opts(),
                        )
                    )
                except Exception as exc:
                    log.debug(
                        "SSH connection to %s failed: %s",
                        ProviderSelector.redact(self.config.connection_uri),
                        type(exc).__name__,
                    )
                    normalized = _async.normalize_asyncssh_error(exc)
                    if normalized is exc:
                        raise
                    raise normalized from exc
                log.debug(
                    "SSH connection to %s established",
                    ProviderSelector.redact(self.config.connection_uri),
                )
            return self._ssh

    def connect(self) -> None:
        _ = self.ssh

    def close(self) -> None:
        with self._ssh_lock:
            # The SFTP invalidation runs first but must not decide whether
            # the SSH connection gets closed: a backend whose own close
            # raised left the connection open AND unreachable -- `_ssh` was
            # still set, so a later `close()` tried the same failing
            # invalidation again and never reached the connection either.
            first_error: typing.Optional[BaseException] = None
            try:
                self._invalidate_sftp()
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                first_error = exc
            if self._ssh is None:
                if first_error is not None:
                    raise first_error
                return
            connection, self._ssh = self._ssh, None
            log.debug(
                "closing SSH connection to %s",
                ProviderSelector.redact(self.config.connection_uri),
            )
            from .. import _async

            async def close_connection() -> None:
                connection.close()
                await connection.wait_closed()

            try:
                _async.async_to_sync(close_connection())
            except Exception as exc:
                normalized = _async.normalize_asyncssh_error(exc)
                if normalized is exc:
                    raise
                raise normalized from exc
            finally:
                if first_error is not None:
                    raise first_error

    def _invalidate_sftp(self) -> None:
        backend, self._sftp_backend = self._sftp_backend, None
        if backend is None:
            return
        # `BaseSftpBackend.close()` closes every connection the backend
        # cached and leaves it usable, so the next path() reconnects.
        backend.close()

    def warm_sftp(self) -> None:
        """Establish the SFTP client from the *calling* thread.

        pathlib_next's native recursive operations (`rm`, `copy`) run as
        coroutines on the shared bridge loop and resolve the client from
        there. With nothing cached that waits on a future only that loop can
        complete, so the loop parks for the full 60s connect timeout, the
        removal never happens, and every later SFTP operation in the process
        -- on any path, on any host -- queues behind it. Warming it here costs
        the connection that is about to be made anyway.
        """
        path = self.path()
        backend = self._sftp_backend
        client = getattr(backend, "client", None)
        source = getattr(path, "source", None)
        if client is None or source is None:
            return
        client(source)

    def path(self, *segments: PathLike) -> HostPath:
        from pathlib_next.uri.schemes.sftp import AsyncsshSftpBackend, SftpPath

        remote_path = self.config.path_flavor(*segments).as_posix()
        if not remote_path.startswith("/"):
            remote_path = "/" + remote_path
        with self._ssh_lock:
            if self._sftp_backend is None:
                self._sftp_backend = AsyncsshSftpBackend(
                    connect_opts=self.config.connect_opts()
                )
            # The remote path becomes part of a URI, so it has to be encoded
            # as one. `pathlib_next` parses with `uritools.urisplit` and then
            # uridecodes the components: a raw `?` or `#` in a filename was
            # taken as the start of the query or fragment and truncated the
            # path, and a genuine `%xx` was decoded into a different name.
            path = SftpPath(
                f"sftp://{uri_host(self.config.host)}:{self.config.port or 22}"
                f"{quote(remote_path, safe=_URI_PATH_SAFE)}",
                backend=self._sftp_backend,
            )
            return path

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
        if wants_text(text, encoding, errors) and encoding is None:
            # `wants_text`, not a local `if text`: `errors="replace"` selects
            # text mode too, and deciding it here differently from the
            # exported rule is exactly how SSH, PSRP and serial came to
            # disagree with the other four transports.
            encoding = "utf-8"
        selected_flavour = self.shell_flavour
        selected_executable = executable or self.config.executable
        if self.config.dialect == "auto" and selected_executable is None:
            assert self._resolved_dialect is not None
            selected_executable = self._resolved_dialect[2]
        shell_command = selected_flavour.command(
            cmds,
            executable=selected_executable,
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
            selected_flavour = self.shell_flavour
            selected_executable = executable or self.config.executable
            if self.config.dialect == "auto" and selected_executable is None:
                assert self._resolved_dialect is not None
                selected_executable = self._resolved_dialect[2]
            shell_command = selected_flavour.command(
                cmds,
                executable=selected_executable,
                cwd=cwd,
                env=env,
            )
            command: typing.Optional[str] = shell_command.command
            remote_env = shell_command.environment
        else:
            if cwd is not None or env is not None:
                raise ValueError("cwd and env require a command when spawning")
            # Resolve the remote program the same way the command branch does.
            # It used to be just the per-call `executable=`, so a bare
            # `with host.shell as session:` started the account's *login*
            # shell and ignored SshConfig.executable, the flavour's default,
            # and the executable recovered by dialect="auto" -- while send()
            # went on rendering in the configured dialect.
            command = executable or self.config.executable
            if command is None:
                flavour = self.shell_flavour
                if self.config.dialect == "auto":
                    resolved = self._resolved_dialect
                    if resolved is not None:
                        command = resolved[2]
                if command is None:
                    command = getattr(flavour, "default_executable", None)
            remote_env = None

        from ..process import terminal_options

        selected_terminal = terminal_options(terminal)
        options: typing.Dict[str, object] = {"env": remote_env, "encoding": encoding}
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
        return SshProcess(
            typing.cast(typing.Any, process),
            command,
            encoding=typing.cast(typing.Optional[str], options.get("encoding")),
            errors=typing.cast(typing.Optional[str], options.get("errors")),
        )


class SshExecutorProvider(ExecutorProvider):
    """Lifecycle-owning SSH command provider used by :class:`PosixHost`."""

    def __init__(self, transport: _SshTransport):
        self.transport = transport
        # SSH receives one finalized command string; argv arguments are
        # rendered by the host shell before dispatch.
        #
        # `spawn`/`tty` are declared because this provider implements
        # `spawn()`: the host selects on the capability, and a provider that
        # can open a session but does not say so is skipped for it.
        capabilities = set(transport.executor.executor_capabilities)
        capabilities |= {"spawn", "tty"} & transport.capabilities
        super().__init__("ssh", transport.executor, capabilities=capabilities)

    def probe(self):
        return ProviderProbe("available", capabilities=self.capabilities)

    def connect(self):
        try:
            self.transport.connect()
        # OSError, not just ConnectionError: a DNS failure (socket.gaierror)
        # and a routing failure (EHOSTUNREACH/ENETUNREACH) are OSError but not
        # ConnectionError, and they are unambiguously pre-dispatch -- no
        # channel opened, nothing sent. They used to escape the provider, so
        # ordered fallback stopped dead and a raw socket error crossed the
        # public boundary.
        except (OSError, TimeoutError) as exc:
            log.debug(
                "SSH provider declining before dispatch: %s: %s",
                type(exc).__name__,
                ProviderSelector.redact(exc),
            )
            raise OperationNotStarted(
                "SSH connection failed before dispatch", cause=exc
            ) from exc

    def close(self):
        self.transport.close()

    def info(self):
        return self.transport.info()

    @property
    def shell_executable(self):
        resolved = self.transport._resolved_dialect
        return resolved[2] if resolved is not None else None

    def spawn(self, *args, **options):
        return self.transport.spawn(*args, **options)


class SftpPathProvider(PathProvider):
    """SFTP path provider sharing the SSH transport lifecycle."""

    def __init__(self, transport: _SshTransport):
        self.transport = transport
        super().__init__(
            "sftp", lambda *segments: transport.path(*segments), capabilities=("path",)
        )

    # Closing is owned by SshExecutorProvider for the shared transport;
    # connecting is not, because the SFTP client has to be established from
    # the caller's thread before any native recursive operation runs.
    def connect(self):
        try:
            self.transport.connect()
            self.transport.warm_sftp()
        except (OSError, TimeoutError) as exc:
            log.debug(
                "SFTP provider declining before dispatch: %s: %s",
                type(exc).__name__,
                ProviderSelector.redact(exc),
            )
            raise OperationNotStarted(
                "SFTP connection failed before dispatch", cause=exc
            ) from exc


def ssh_providers(
    config: SshConfig,
) -> typing.Tuple[SshExecutorProvider, SftpPathProvider]:
    """Build an executor and path provider sharing one SSH transport.

    This is the supported way to compose SSH into a host you assemble
    yourself, rather than taking the finished `PosixHost` that
    `SshConfig._create_host()` returns:

        executors, paths = [], []
        run_provider, path_provider = ssh_providers(config.ssh)
        executors.append(run_provider)
        paths.append(path_provider)

    Both providers **must** share one transport: that is what makes them share
    a connection and a lifecycle, and `SshExecutorProvider` is the one that
    owns close. Assembling them by hand from two transports type-checks and
    silently opens two connections, only one of which is ever closed -- which
    is precisely why this returns a pair instead of exposing the transport.
    """
    transport = _SshTransport(config)
    return SshExecutorProvider(transport), SftpPathProvider(transport)
