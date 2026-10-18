"""The SSH transport service shared by a host's executor and path providers."""

from __future__ import annotations


import logging
import re
import subprocess
import threading
import typing
from pathlib import PureWindowsPath


from pathapi import PosixPathname, WindowsPathname, Path

from ._executor import SSHConnection, SSHExecutor
from ...exceptions import OperationNotStartedError
from ...provider._selector import ProviderSelector
from ...process import Process, TerminalRequest
from ._process import SSHProcess
from ...shell._grammar import Command
from ...executor import CommandEnvironment, StrPath
from ...host._info import HostInfo, parse_host_info
from ...shell import BASH, CMD, FISH, POWERSHELL, POSIX_SHELL, PWSH, ZSH, shell_flavour
from ...shell._flavour import ShellFlavour
from ...shell._invocation import render_command
from ...shell._vocabulary import ShellCommand
from ...host._selection import PathnameType
from ._config import SSHConfig
from ._config_source import _materialized

log = logging.getLogger(__name__)

#: The remedy a decline names when the server's host key is not trusted.
_UNTRUSTED_HOST_KEY = (
    "add the key to known_hosts (known_hosts= names the file to read) "
    "or pass verify=False to skip the host-key check"
)


class _SSHTransport:
    """A host reached over SSH, with an explicitly configured command shell."""

    def __init__(self, config: SSHConfig) -> None:
        self.config = config
        self._ssh: typing.Optional[SSHConnection] = None
        self._ssh_lock = threading.RLock()
        #: The process the cached connection belongs to; see `ssh`.
        self._loop_generation: typing.Optional[int] = None
        self._executor = SSHExecutor(lambda: self.ssh)
        self._resolved_dialect: typing.Optional[
            typing.Tuple[typing.Tuple[object, ...], ShellFlavour, typing.Optional[str]]
        ] = None
        #: The backend every path of this host holds; see `_backend()`.
        self._sftp_backend: typing.Optional[object] = None
        #: `(connection, AsyncSSHBackend.over_connection(connection, ...))`.
        self._sftp_generation: typing.Optional[typing.Tuple[object, typing.Any]] = None
        #: SFTP `realpath(".")`, asked once per connection generation.
        self._login_dir: typing.Optional[str] = None
        #: What `pathname="auto"` resolved to from the login directory.
        self._resolved_flavor: typing.Optional[PathnameType] = None

    @property
    def pathname(self) -> PathnameType:
        """The path constructor, with `"auto"` answered by the login directory."""
        declared = self.config.pathname
        if declared != "auto":
            return typing.cast(PathnameType, declared)
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
        return "cmd" if issubclass(self.pathname, PureWindowsPath) else "posix"

    @property
    def capabilities(self) -> typing.FrozenSet[str]:
        return frozenset(("run", "path", "spawn", "tty"))

    @property
    def shell_flavour(self) -> ShellFlavour:
        selection = self.config.shell
        if selection != "auto":
            return typing.cast(ShellFlavour, selection)
        key = (selection, self.pathname, self.config.executable)
        with self._ssh_lock:
            if self._resolved_dialect is not None and self._resolved_dialect[0] == key:
                return self._resolved_dialect[1]
            resolved, executable = (
                self._detect_windows_shell()
                if issubclass(self.pathname, PureWindowsPath)
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
    def executor(self) -> SSHExecutor:
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
        cwd: typing.Optional[StrPath] = None,
        env: typing.Optional[CommandEnvironment] = None,
    ) -> ShellCommand:
        """The only place this transport renders: `command()` for the login shell."""
        flavour = self.shell_flavour
        program = executable or self.config.executable
        if program is None and self.config.shell == "auto":
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
        from ... import _async

        generation = _async.loop_generation()
        if self._loop_generation is None:
            self._loop_generation = generation
            return False
        if self._loop_generation == generation:
            return False
        self._ssh = None
        self._sftp_generation = None
        self._login_dir = None
        self._resolved_flavor = None
        self._loop_generation = generation
        return True

    @property
    def ssh(self) -> SSHConnection:
        """The lazily opened and reused asyncssh connection."""
        with self._ssh_lock:
            from ... import _async

            self._drop_if_forked()
            if self._ssh is None or self._ssh.is_closed():
                log.debug(
                    "opening SSH connection to %s",
                    ProviderSelector.redact(self.config.connection_uri),
                )
                try:
                    options = self.config.connect_opts()
                    if self.config.port is not None:
                        options["port"] = self.config.port
                    with _materialized(options) as options:
                        self._ssh = _async.async_to_sync(
                            _async.asyncssh().connect(self.config.host, **options)
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

    def connect_or_decline(self) -> None:
        """`connect()`, with a failure to connect raised as a decline.

        `OSError`, not just `ConnectionError`: a DNS failure (`socket.gaierror`)
        and a routing failure (EHOSTUNREACH/ENETUNREACH) are `OSError` but not
        `ConnectionError`, and every one of them is pre-dispatch -- no channel
        opened, nothing sent -- so the provider declines and ordered fallback
        moves on.
        """
        try:
            self.connect()
        except OperationNotStartedError:
            raise
        except (OSError, TimeoutError) as exc:
            log.debug(
                "SSH provider declining before dispatch: %s: %s",
                type(exc).__name__,
                ProviderSelector.redact(exc),
            )
            raise OperationNotStartedError(
                self._decline_reason("SSH", exc), cause=exc
            ) from exc

    def _decline_reason(self, service: str, exc: BaseException) -> str:
        """Why `service` declined, naming the host and what refused it."""
        from .._decline import connect_failure

        return connect_failure(
            service,
            self.config.host,
            self.config.port,
            exc,
            trust=_UNTRUSTED_HOST_KEY,
        )

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
            from ... import _async

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
        generation, self._sftp_generation = self._sftp_generation, None
        if generation is None:
            return
        # Exits the SFTP channel only: an over_connection backend never
        # closes the connection it was given.
        generation[1].close()

    def _sftp_over_connection(self) -> typing.Any:
        """The SFTP backend over the current connection, rebuilt after a reconnect.

        An `over_connection()` backend is bound to one connection object and
        raises `ConnectionResetError` once that closes, so a new connection
        needs a new one.
        """
        from ... import _async

        with self._ssh_lock:
            connection = self.ssh
            generation = self._sftp_generation
            if generation is not None and generation[0] is connection:
                return generation[1]
            self._sftp_generation = None
            if generation is not None:
                generation[1].close()
            # `host=` always: a tunnelled connection may report no peer address.
            backend = _async.sftppath().AsyncSSHBackend.over_connection(
                connection, _async._ensure_loop(), host=self.config.host
            )
            self._sftp_generation = (connection, backend)
            return backend

    def _close_sftp_channel(self) -> None:
        with self._ssh_lock:
            if self._drop_if_forked():
                return
            generation = self._sftp_generation
            if generation is not None:
                generation[1].close()

    def _login_directory(self) -> str:
        """The SFTP login directory, asked once per connection generation.

        sftppath exposes no working directory, and a relative name must mean
        what `run()`'s working directory means, so it is read from an SFTP
        client started on the exec connection.
        """
        from ... import _async

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

    def path(self, *segments: StrPath) -> Path:
        from ... import _async

        SFTPPath = _async.sftppath().SFTPPath

        # The path style ("auto") and a relative path's base come from the
        # login directory, read over the exec connection; failing to open it
        # declines, so the host builds the path from its next provider.
        if self.config.pathname == "auto" and self._resolved_flavor is None:
            self.connect_or_decline()
        pure = self.pathname(*segments)
        remote_path = pure.as_posix()
        if pure.anchor:
            if not remote_path.startswith("/"):
                remote_path = "/" + remote_path
        else:
            self.connect_or_decline()
            base = self._login_directory().rstrip("/")
            remote_path = (
                (base + "/" + remote_path) if remote_path not in ("", ".") else base
            ) or "/"
        return SFTPPath(remote_path, backend=self._backend())

    def _backend(self) -> typing.Any:
        """The SFTP backend this host's paths share; building it connects nothing.

        It opens its SFTP channels on `ssh`, the connection `run()` uses, so a
        host authenticates once whichever leg connects first.
        """
        with self._ssh_lock:
            if self._sftp_backend is None:
                from ._sftp import _SharedSFTPBackend

                self._sftp_backend = _SharedSFTPBackend(self)
            return self._sftp_backend

    def spawn(
        self,
        *cmds: Command,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[StrPath] = None,
        env: typing.Optional[CommandEnvironment] = None,
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
            # Resolve the remote program the same way the command branch
            # does, so a bare `with host.shell as session:` starts the shell
            # that send() renders for, not the account's *login* shell.
            command = executable or self.config.executable
            if command is None:
                flavour = self.shell_flavour
                if self.config.shell == "auto":
                    resolved = self._resolved_dialect
                    if resolved is not None:
                        command = resolved[2]
                if command is None:
                    command = getattr(flavour, "default_executable", None)
            remote_env = None

        from ...process import terminal_options

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

        from ... import _async

        try:
            process = _async.async_to_sync(self.ssh.create_process(command, **options))
        except Exception as exc:
            normalized = _async.normalize_asyncssh_error(exc, command=command)
            if normalized is exc:
                raise
            raise normalized from exc
        return SSHProcess(
            typing.cast(typing.Any, process),
            command,
            encoding=typing.cast(typing.Optional[str], options.get("encoding")),
            errors=typing.cast(typing.Optional[str], options.get("errors")),
        )
