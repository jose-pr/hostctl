"""SSH host implementation."""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import subprocess
import threading
import typing
from pathlib import PurePath, PureWindowsPath
from urllib.parse import quote, unquote, urlencode

from pathapi import Pathname, PosixPathname, WindowsPathname

from ..executor import SshConnection, SshExecutor
from ..provider import (
    ExecutorProvider,
    OperationNotStarted,
    PathProvider,
    ProviderProbe,
    ProviderSelector,
)

log = logging.getLogger("hostctl.host.ssh")
from ..process import Process, SshProcess, TerminalRequest
from ._common import (
    Command,
    Environment,
    HostConfig,
    HostInfo,
    HostPath,
    PathLike,
    _refuse_uri,
    parse_host_info,
    path_flavor_name,
    path_flavor_selection,
    strict_uri_query,
    uri_hostname,
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
from ..shell._invocation import render_command
from ..shell._common import ShellCommand

SshKeySource = typing.Union[str, bytes, os.PathLike[str]]
SshClientKeys = typing.Optional[
    typing.Union[SshKeySource, typing.Sequence[SshKeySource]]
]
SshKnownHosts = typing.Optional[
    typing.Union[SshKeySource, typing.Sequence[SshKeySource]]
]
PathnameConstructor = typing.Type[typing.Union[PurePath, Pathname]]
SshShellSelection = typing.Union[ShellFlavourSelection, typing.Literal["auto"]]
SshPathSelection = typing.Union[PathnameConstructor, typing.Literal["auto"]]

#: `connect_options` keys hostctl owns, and the field to use instead.
_OWNED_CONNECT_OPTIONS = {
    "host": "host",
    "port": "port",
    "username": "username",
    "password": "password",
    "client_keys": "client_keys",
    "known_hosts": "known_hosts",
    "config": "ssh_config",
    "connect_timeout": "connect_timeout",
}


#: Login shells hostctl knows how to render for. `"auto"` is resolved from
#: the dialect; the rest name what the server actually runs.
_LOGIN_SHELLS = frozenset(("auto", "cmd", "powershell", "posix"))


#: Characters OpenSSH's `valid_hostname` refuses, beyond whitespace and a
#: leading `-`. A host is substituted into a ProxyCommand (`%h`), which
#: OpenSSH runs through a shell (CVE-2023-51385).
_HOST_REFUSED = frozenset("'`\"$\\;&<>|(){}")
#: The same for `valid_ruser`, which keeps `\\`, `$` and spaces legal.
_USER_REFUSED = frozenset("'`\";&<>|(){}")


def _refuse_unsafe_target(host: str, username: typing.Optional[str]) -> None:
    """Refuse a host or username OpenSSH itself would refuse.

    netimps decides whether the host is an address literal or a well-formed
    name; the rest is the OpenSSH-specific list, which netimps does not know.
    """
    import netimps

    if host.startswith("-"):
        raise ValueError(
            "SSH host must be one host name, as OpenSSH requires: it cannot start with '-'"
        )
    for char in host:
        if char.isspace() or ord(char) < 32 or ord(char) == 127:
            raise ValueError(
                "SSH host must be one host name, as OpenSSH requires: it cannot contain whitespace or control characters"
            )
        if char in _HOST_REFUSED:
            raise ValueError(
                f"SSH host must be one host name, as OpenSSH requires: it cannot contain {char!r}"
            )
    if netimps.try_parse(host) is None and not netimps.is_valid(host, netimps.FQDN):
        raise ValueError("SSH host must be an address or a valid host name")
    if not username:
        return
    if username.startswith("-"):
        raise ValueError("SSH username cannot start with '-'")
    if "${" in username:
        raise ValueError("SSH username cannot contain '${'")
    if username.endswith("\\"):
        raise ValueError("SSH username cannot end with a backslash")
    for char in username:
        if char in _USER_REFUSED:
            raise ValueError(f"SSH username cannot contain {char!r}")
    for index, char in enumerate(username[:-1]):
        if char.isspace() and username[index + 1] == "-":
            raise ValueError("SSH username cannot have whitespace before '-'")


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
    username: typing.Optional[str] = None
    password: typing.Optional[str] = dataclasses.field(default=None, repr=False)
    client_keys: SshClientKeys = dataclasses.field(default=None, repr=False)
    executable: typing.Optional[str] = None
    known_hosts: SshKnownHosts = ()
    dialect: SshShellSelection = POSIX_SHELL
    path_flavor: SshPathSelection = PosixPathname
    #: What parses the command string when it ARRIVES, before the command
    #: line's own shell runs.
    #:
    #: AsyncSSH sends one string and the server hands it to a login shell.
    #: On a POSIX server that is `$SHELL -c`; `"posix"` covers every sh-family
    #: shell and fish (csh and tcsh are unsupported), and the string is
    #: rendered so each reads it back verbatim. Windows OpenSSH's
    #: stock server hands it to `cmd.exe /c`, so a `cmd`-dialect command
    #: line (which already carries one cmd layer) is parsed by cmd TWICE:
    #: measured against a real cmd.exe, 4 of 8 adversarial values survived
    #: and two of the failures were command injection.
    #:
    #: `"auto"` reads the dialect: a Windows path flavour means the stock
    #: `cmd.exe`, anything else means a POSIX login shell. The server's
    #: shell is configurable (`HKLM\SOFTWARE\OpenSSH\DefaultShell`), so
    #: say so explicitly when it has been changed -- guessing is wrong in
    #: both directions.
    login_shell: str = "auto"
    #: Seconds asyncssh may spend establishing the connection (TCP, key
    #: exchange and authentication); `None` leaves asyncssh's own default.
    connect_timeout: typing.Optional[float] = 30.0
    #: An ssh_config to read on BOTH legs (`Host`, `HostName`, `IdentityFile`,
    #: `ProxyJump` ...). `None`, the default, reads none. One path: OpenSSH's
    #: `Include` covers the rest. Never part of the URI: a `ProxyCommand`
    #: starts a local process, which whoever wrote the URI must not choose.
    ssh_config: typing.Optional[typing.Union[str, "os.PathLike[str]"]] = None
    #: Extra keywords for `asyncssh.connect`, given to both legs. Never part
    #: of the URI or `repr()`, since it may hold a passphrase.
    connect_options: typing.Mapping[str, object] = dataclasses.field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.host:
            raise ValueError("host must not be empty")
        # The constructor takes a HOST; the dispatcher takes a URI.
        _refuse_uri("SshConfig", self.host)
        _refuse_unsafe_target(self.host, self.username)
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.dialect != "auto":
            self.dialect = shell_flavour(self.dialect)
        elif self.executable is not None:
            raise ValueError("executable cannot be combined with dialect='auto'")
        self.path_flavor = path_flavor_selection(self.path_flavor, allow_auto=True)
        self.connect_options = dict(self.connect_options)
        for key in self.connect_options:
            if key in _OWNED_CONNECT_OPTIONS:
                raise ValueError(
                    f"connect_options cannot set {key!r}; "
                    f"use SshConfig({_OWNED_CONNECT_OPTIONS[key]}=...)"
                )
        if self.connect_timeout is not None and not self.connect_timeout > 0:
            raise ValueError("connect_timeout must be greater than 0 seconds")
        if self.login_shell not in _LOGIN_SHELLS:
            values = ", ".join(sorted(_LOGIN_SHELLS))
            raise ValueError(f"login_shell must be one of: {values}")

    @property
    def resolved_login_shell(self) -> str:
        """`login_shell` with `"auto"` answered from the dialect.

        With `path_flavor="auto"` the answer needs the server: use the
        transport's `login_shell`.
        """
        if self.login_shell != "auto":
            return self.login_shell
        if self.path_flavor == "auto":
            raise ValueError("login_shell='auto' needs a known path_flavor")
        return "cmd" if issubclass(self.path_flavor, PureWindowsPath) else "posix"

    def connect_opts(self) -> typing.Dict[str, object]:
        opts: typing.Dict[str, object] = {}
        if self.username:
            # Left out otherwise, so asyncssh uses the local account name,
            # as `ssh` does.
            opts["username"] = self.username
        if self.password is not None:
            opts["password"] = self.password
        if self.client_keys is not None:
            keys = self.client_keys
            if isinstance(keys, (str, bytes, os.PathLike)):
                keys = [keys]
            opts["client_keys"] = keys
        # `config` is always passed: asyncssh would otherwise load
        # `~/.ssh/config`, and a library call must not depend on a personal
        # file. `ssh_config` is the explicit opt-in. Both legs get these
        # options, so they resolve and verify the host alike.
        opts["config"] = (
            None
            if self.ssh_config is None
            else [os.path.expanduser(os.fspath(self.ssh_config))]
        )
        # Always passed, never omitted on the default: `()` is asyncssh's own
        # "resolve known_hosts the usual way" value, and both legs receive
        # these options, so the path leg verifies the host key exactly as the
        # exec leg does. Opting out stays possible and explicit:
        # `known_hosts=None`.
        opts["known_hosts"] = self.known_hosts
        if self.connect_timeout is not None:
            opts["connect_timeout"] = self.connect_timeout
        opts.update(self.connect_options)
        return opts

    @property
    def connection_uri(self) -> str:
        user = f"{quote(self.username, safe='')}@" if self.username else ""
        query = {
            "dialect": self.dialect,
            "path_flavor": path_flavor_name(self.path_flavor),
        }
        if self.executable:
            query["executable"] = self.executable
        if self.login_shell != "auto":
            # Only when it was said: `auto` is the default and carrying it
            # would change every existing URI's text.
            query["login_shell"] = self.login_shell
        if self.connect_timeout != 30.0:
            query["connect_timeout"] = self.connect_timeout
        from netimps import join_host

        return f"ssh://{user}{join_host(self.host, self.port)}?{urlencode(query)}"

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> SshConfig:
        query = strict_uri_query(
            parsed,
            {
                "dialect",
                "path_flavor",
                "executable",
                "login_shell",
                "connect_timeout",
            },
        )
        if not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("SSH URI requires a host and no path")
        from netimps import get_default_port

        return cls(
            host=uri_hostname(parsed),
            port=get_default_port("ssh") if parsed.port is None else parsed.port,
            username=unquote(parsed.username or "") or None,
            password=typing.cast(typing.Optional[str], credentials.get("password")),
            client_keys=typing.cast(SshClientKeys, credentials.get("client_keys")),
            executable=query.get("executable") or None,
            known_hosts=typing.cast(SshKnownHosts, credentials.get("known_hosts", ())),
            dialect=(
                "auto"
                if query.get("dialect") == "auto"
                else shell_flavour(query.get("dialect", POSIX_SHELL.name))
            ),
            path_flavor=path_flavor_selection(
                query.get("path_flavor", "posix"), allow_auto=True
            ),
            login_shell=query.get("login_shell", "auto"),
            connect_timeout=(
                float(query["connect_timeout"]) if "connect_timeout" in query else 30.0
            ),
        )

    def _create_host(self):
        from .system import PosixHost, WindowsHost

        transport = self._create_transport()
        try:
            # "auto" connects while the host is created, because the host
            # class depends on what the server answers.
            flavor = transport.path_flavor
        except BaseException:
            transport.close()
            raise
        host_type = WindowsHost if issubclass(flavor, PureWindowsPath) else PosixHost
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
        #: SFTP `realpath(".")`, asked once per connection generation.
        self._login_dir: typing.Optional[str] = None
        #: What `path_flavor="auto"` resolved to from the login directory.
        self._resolved_flavor: typing.Optional[PathnameConstructor] = None

    @property
    def path_flavor(self) -> PathnameConstructor:
        """The path constructor, with `"auto"` answered by the login directory."""
        declared = self.config.path_flavor
        if declared != "auto":
            return typing.cast(PathnameConstructor, declared)
        with self._ssh_lock:
            if self._resolved_flavor is None:
                answer = self._login_directory()
                if re.match(r"^/[A-Za-z]:(/|$)", answer):
                    self._resolved_flavor = WindowsPathname
                elif answer.startswith("/"):
                    self._resolved_flavor = PosixPathname
                else:
                    raise ValueError(
                        f"cannot tell the server's path style from {answer!r}"
                    )
            return self._resolved_flavor

    @property
    def login_shell(self) -> str:
        """What parses a delivered command line first, `"auto"` resolved."""
        if self.config.login_shell != "auto":
            return self.config.login_shell
        return "cmd" if issubclass(self.path_flavor, PureWindowsPath) else "posix"

    @property
    def capabilities(self) -> typing.FrozenSet[str]:
        return frozenset(("run", "path", "spawn", "tty"))

    @property
    def shell_flavour(self) -> ShellFlavour:
        selection = self.config.dialect
        if selection != "auto":
            return typing.cast(ShellFlavour, selection)
        key = (selection, self.path_flavor, self.config.executable)
        with self._ssh_lock:
            if self._resolved_dialect is not None and self._resolved_dialect[0] == key:
                return self._resolved_dialect[1]
            resolved, executable = (
                self._detect_windows_shell()
                if issubclass(self.path_flavor, PureWindowsPath)
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
                "ash": POSIX_SHELL,
                "ksh": POSIX_SHELL,
                "ksh93": POSIX_SHELL,
                "mksh": POSIX_SHELL,
                "lksh": POSIX_SHELL,
                "oksh": POSIX_SHELL,
                "yash": POSIX_SHELL,
                "posh": POSIX_SHELL,
                "bash": BASH,
                "zsh": ZSH,
                "fish": FISH,
            }[name.casefold()], executable
        except KeyError as exc:
            raise RuntimeError(
                f"unsupported or undetected remote POSIX shell: {name or '<empty>'} "
                "(csh and tcsh are not supported)"
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
        rendered = self._render((self.shell_flavour.info_script,))
        result = self.executor(rendered.command, check=False, encoding="utf-8")
        return parse_host_info(result.stdout)

    def _render(
        self,
        cmds: typing.Iterable[Command],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
    ) -> ShellCommand:
        """The only place this transport renders: `command()` for the login shell."""
        flavour = self.shell_flavour
        program = executable or self.config.executable
        if program is None and self.config.dialect == "auto":
            assert self._resolved_dialect is not None
            program = self._resolved_dialect[2]
        return render_command(
            flavour,
            cmds,
            executable=program,
            cwd=cwd,
            env=env,
            login_shell=self.login_shell,
        )

    def _drop_if_forked(self) -> bool:
        """After `fork()` the cached objects belong to the parent: forget them.

        The connection is bound to the parent's loop, which does not exist in
        the child. Dropping WITHOUT closing is the only safe move -- closing
        would run asyncssh's teardown against a socket the parent still uses.
        Call with `_ssh_lock` held.
        """
        from .. import _async

        generation = _async.loop_generation()
        if self._loop_generation is None:
            self._loop_generation = generation
            return False
        if self._loop_generation == generation:
            return False
        self._ssh = None
        self._sftp_backend = None
        self._login_dir = None
        self._resolved_flavor = None
        self._loop_generation = generation
        return True

    @property
    def ssh(self) -> SshConnection:
        """The lazily opened and reused asyncssh connection."""
        with self._ssh_lock:
            from .. import _async

            self._drop_if_forked()
            if self._ssh is None or self._ssh.is_closed():
                log.debug(
                    "opening SSH connection to %s",
                    ProviderSelector.redact(self.config.connection_uri),
                )
                try:
                    self._ssh = _async.async_to_sync(
                        _async.asyncssh().connect(
                            self.config.host,
                            port=self.config.port,
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
            if self._drop_if_forked():
                return
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
        self._login_dir = None
        self._resolved_flavor = None
        backend, self._sftp_backend = self._sftp_backend, None
        if backend is None:
            return
        # `BaseSFTPBackend.close()` closes every connection the backend
        # cached and leaves it usable, so the next path() reconnects.
        backend.close()

    def _login_directory(self) -> str:
        """The SFTP login directory, asked once per connection generation.

        sftppath exposes no working directory, and a relative name must mean
        what `run()`'s working directory means, so it is read from an SFTP
        client started on the exec connection.
        """
        from .. import _async

        with self._ssh_lock:
            if self._login_dir is None:
                connection = self.ssh  # resolved on the calling thread

                async def realpath() -> str:
                    client = await connection.start_sftp_client()
                    try:
                        answer = await client.realpath(".")
                    finally:
                        client.exit()
                    return answer.decode() if isinstance(answer, bytes) else answer

                try:
                    self._login_dir = _async.async_to_sync(realpath())
                except Exception as exc:
                    normalized = _async.normalize_asyncssh_error(exc)
                    if normalized is exc:
                        raise
                    raise normalized from exc
            return self._login_dir

    def path(self, *segments: PathLike) -> HostPath:
        from .. import _async

        SFTPPath = _async.sftppath().SFTPPath

        pure = self.path_flavor(*segments)
        remote_path = pure.as_posix()
        if pure.anchor:
            if not remote_path.startswith("/"):
                remote_path = "/" + remote_path
        else:
            base = self._login_directory().rstrip("/")
            remote_path = (
                (base + "/" + remote_path) if remote_path not in ("", ".") else base
            ) or "/"
        return SFTPPath(remote_path, backend=self._backend())

    def _backend(self) -> typing.Any:
        """The SFTP backend this host shares, built once."""
        from .. import _async

        with self._ssh_lock:
            if self._sftp_backend is None:
                # The options carry `config` (see `connect_opts`), which
                # sftppath keeps, so this leg reads the exec leg's ssh_config.
                self._sftp_backend = _async.sftppath().AsyncSSHBackend(
                    self.config.host,
                    self.config.port,
                    user=self.config.username,
                    connect_opts=self.config.connect_opts(),
                )
            return self._sftp_backend

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
            shell_command = self._render(cmds, executable=executable, cwd=cwd, env=env)
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

    @property
    def login_shell(self):
        """What parses a delivered command line before its own shell does.

        The host reads this when it renders a `CommandLine`, because only
        the transport knows there is an extra parse at all.
        """
        return self.transport.login_shell

    def spawn(self, *args, **options):
        return self.transport.spawn(*args, **options)


class SftpPathProvider(PathProvider):
    """SFTP path provider sharing the SSH transport lifecycle."""

    def __init__(self, transport: _SshTransport):
        self.transport = transport
        super().__init__(
            "sftp", lambda *segments: transport.path(*segments), capabilities=("path",)
        )

    def probe(self):
        from .. import _async

        try:
            _async.sftppath()
        except ImportError:
            return ProviderProbe(
                "unavailable",
                reason=(
                    "the 'ssh' extra is incomplete: sftppath is not installed; "
                    "pip install hostctl[ssh]"
                ),
            )
        return ProviderProbe("available", capabilities=self.capabilities)

    # Closing is owned by SshExecutorProvider for the shared transport.
    #
    # Composite paths call this before EVERY dispatch, so it stays cheap: the
    # client is cached after the first call. Opening it here keeps a failed
    # connection a pre-dispatch decline rather than an error mid-operation.
    def connect(self):
        from .. import _async

        try:
            client = getattr(self.transport._backend(), "client", None)
            if client is not None:
                client()
        except Exception as exc:
            # sftppath translates asyncssh's errors but not its timeouts: on
            # 3.9 a connect timeout is `asyncio.TimeoutError`, not the builtin.
            mapped = _async.normalize_asyncssh_error(exc)
            if not isinstance(mapped, (OSError, TimeoutError)):
                raise
            log.debug(
                "SFTP provider declining before dispatch: %s: %s",
                type(mapped).__name__,
                ProviderSelector.redact(mapped),
            )
            raise OperationNotStarted(
                "SFTP connection failed before dispatch", cause=mapped
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
