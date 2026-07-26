"""QEMU virtual-machine host backed by QEMU Guest Agent."""

from __future__ import annotations

import dataclasses
import subprocess
import typing
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import quote, unquote, urlencode

from pathlib_next import Pathname, PosixPathname, WindowsPathname

from ..executor import QemuExecutor
from ..qga import (
    GuestAgentTransport,
    LibvirtGuestAgentTransport,
    SshUnixGuestAgentTransport,
    UnixSocketGuestAgentTransport,
)
from ..process import Process, QemuSerialConsole
from ..shell import (
    POWERSHELL,
    POSIX_SHELL,
    ShellFlavour,
    ShellFlavourSelection,
    shell_flavour,
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
    strict_uri_credentials,
    strict_uri_query,
)
from .qemu_path import PosixQemuPath, QgaPathBackend, WindowsQemuPath
from .ssh import SshConfig, SshHost

QemuTransport = typing.Literal["libvirt", "unix", "ssh"]
PathnameConstructor = typing.Type[typing.Union[PurePath, Pathname]]
QemuShellSelection = typing.Union[ShellFlavourSelection, typing.Literal["auto"]]
QemuPathSelection = typing.Union[PathnameConstructor, typing.Literal["auto"]]


def _path_selection(value: str) -> QemuPathSelection:
    try:
        return {
            "auto": "auto",
            "posix": PosixPathname,
            "windows": WindowsPathname,
        }[value]
    except KeyError as exc:
        raise ValueError("path_flavor must be 'auto', 'posix', or 'windows'") from exc


@dataclasses.dataclass
class QemuConfig(HostConfig, schemes=("qemu+libvirt", "qga+unix", "qga+ssh")):
    """Guest identity, QGA transport, and guest semantic selections."""

    domain: str
    transport: QemuTransport = "libvirt"
    connection: typing.Optional[str] = None
    socket_path: typing.Optional[str] = None
    ssh: typing.Optional[SshConfig] = dataclasses.field(default=None, repr=False)
    agent_timeout: float = 10.0
    dialect: QemuShellSelection = "auto"
    path_flavor: QemuPathSelection = "auto"
    transport_factory: typing.Optional[typing.Callable[[], GuestAgentTransport]] = (
        dataclasses.field(default=None, repr=False, compare=False)
    )
    serial_console: typing.Optional[QemuSerialConsole] = dataclasses.field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.domain or "\x00" in self.domain:
            raise ValueError("domain must not be empty or contain NUL")
        if self.transport not in typing.get_args(QemuTransport):
            raise ValueError(f"unsupported QEMU transport: {self.transport}")
        if self.agent_timeout <= 0:
            raise ValueError("agent_timeout must be positive")
        if self.transport == "unix" and not self.socket_path:
            raise ValueError("unix QGA transport requires socket_path")
        if self.transport == "ssh" and self.ssh is None:
            raise ValueError("SSH QGA transport requires an SshConfig")
        if self.transport != "ssh" and self.ssh is not None:
            raise ValueError("ssh config requires transport='ssh'")
        if self.dialect != "auto":
            self.dialect = shell_flavour(self.dialect)
        if self.path_flavor != "auto":
            value = self.path_flavor
            if not isinstance(value, type) or not issubclass(
                value, (Pathname, PurePath)
            ):
                raise TypeError("path_flavor must be a pure-path class or 'auto'")
            if value is PurePath or not issubclass(
                value, (PurePosixPath, PureWindowsPath)
            ):
                raise TypeError("path_flavor must use POSIX or Windows semantics")

    @property
    def connection_uri(self) -> str:
        dialect = "auto" if self.dialect == "auto" else str(self.dialect)
        path_flavor = (
            "auto"
            if self.path_flavor == "auto"
            else (
                "windows"
                if issubclass(
                    typing.cast(PathnameConstructor, self.path_flavor),
                    PureWindowsPath,
                )
                else "posix"
            )
        )
        query: typing.Dict[str, object] = {
            "agent_timeout": self.agent_timeout,
            "dialect": dialect,
            "path_flavor": path_flavor,
        }
        if self.transport == "libvirt":
            if self.connection is not None:
                query["connection"] = self.connection
            return (
                f"qemu+libvirt:///{quote(self.domain, safe='')}?" f"{urlencode(query)}"
            )
        if self.transport == "unix":
            query["domain"] = self.domain
            return (
                f"qga+unix:///{quote(self.socket_path or '', safe='')}?"
                f"{urlencode(query)}"
            )
        ssh = typing.cast(SshConfig, self.ssh)
        query["socket_path"] = self.socket_path or (
            f"/run/qemu-server/{self.domain}.qga"
        )
        authority = f"{quote(ssh.username, safe='')}@{ssh.host}:{ssh.port}"
        return (
            f"qga+ssh://{authority}/{quote(self.domain, safe='')}?"
            f"{urlencode(query)}"
        )

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> QemuConfig:
        strict_uri_credentials(
            credentials,
            (
                "password",
                "client_keys",
                "known_hosts",
                "transport_factory",
                "serial_console",
            ),
        )
        query = strict_uri_query(
            parsed,
            (
                "agent_timeout",
                "connection",
                "dialect",
                "domain",
                "path_flavor",
                "socket_path",
            ),
        )
        try:
            timeout = float(query.get("agent_timeout", "10"))
        except ValueError as exc:
            raise ValueError("agent_timeout must be numeric") from exc
        common = {
            "agent_timeout": timeout,
            "dialect": (
                "auto"
                if query.get("dialect", "auto") == "auto"
                else shell_flavour(query["dialect"])
            ),
            "path_flavor": _path_selection(query.get("path_flavor", "auto")),
            "transport_factory": credentials.get("transport_factory"),
            "serial_console": credentials.get("serial_console"),
        }
        scheme = parsed.scheme.casefold()
        if scheme == "qemu+libvirt":
            if parsed.netloc or not parsed.path.strip("/"):
                raise ValueError("libvirt QEMU URI requires a domain path")
            return cls(
                unquote(parsed.path.lstrip("/")),
                transport="libvirt",
                connection=query.get("connection") or None,
                **common,
            )
        if scheme == "qga+unix":
            path = unquote(parsed.path.lstrip("/"))
            domain = query.get("domain")
            if not path or not domain:
                raise ValueError("Unix QGA URI requires socket path and domain")
            if not path.startswith("/"):
                path = "/" + path
            return cls(
                domain,
                transport="unix",
                socket_path=path,
                **common,
            )
        if not parsed.hostname or not parsed.username or not parsed.path.strip("/"):
            raise ValueError("SSH QGA URI requires user, host, and domain")
        ssh = SshConfig(
            parsed.hostname,
            port=parsed.port or 22,
            username=unquote(parsed.username),
            password=typing.cast(typing.Optional[str], credentials.get("password")),
            client_keys=credentials.get("client_keys"),
            known_hosts=credentials.get("known_hosts", ()),
        )
        return cls(
            unquote(parsed.path.lstrip("/")),
            transport="ssh",
            socket_path=query.get("socket_path") or None,
            ssh=ssh,
            **common,
        )

    def _create_host(self) -> QemuHost:
        return QemuHost(self)


class QemuHost(Host):
    """A guest OS reached through QEMU Guest Agent."""

    def __init__(self, config: QemuConfig) -> None:
        self.config = config
        self._transport: typing.Optional[GuestAgentTransport] = None
        self._ssh_host: typing.Optional[SshHost] = None
        self._commands: typing.Optional[typing.FrozenSet[str]] = None
        self._os_info: typing.Mapping[str, object] = {}
        self._hostname: typing.Optional[str] = None
        self._executor = QemuExecutor(lambda: self.transport)
        self._path_backend: typing.Optional[QgaPathBackend] = None

    @property
    def transport(self) -> GuestAgentTransport:
        if self._transport is None:
            if self.config.transport_factory is not None:
                self._transport = self.config.transport_factory()
            elif self.config.transport == "libvirt":
                self._transport = LibvirtGuestAgentTransport(
                    self.config.domain,
                    connection_uri=self.config.connection,
                    timeout=self.config.agent_timeout,
                )
            elif self.config.transport == "unix":
                self._transport = UnixSocketGuestAgentTransport(
                    typing.cast(str, self.config.socket_path),
                    timeout=self.config.agent_timeout,
                )
            else:
                self._ssh_host = SshHost(typing.cast(SshConfig, self.config.ssh))
                self._ssh_host.connect()
                socket_path = self.config.socket_path or (
                    f"/run/qemu-server/{self.config.domain}.qga"
                )
                self._transport = SshUnixGuestAgentTransport(
                    socket_path,
                    lambda: self._ssh_host.ssh,
                    timeout=self.config.agent_timeout,
                )
        return self._transport

    @property
    def executor(self) -> QemuExecutor:
        return self._executor

    @property
    def supported_commands(self) -> typing.FrozenSet[str]:
        self.connect()
        return self._commands or frozenset()

    @property
    def capabilities(self) -> typing.FrozenSet[str]:
        commands = self.supported_commands
        values = set()
        if {"guest-exec", "guest-exec-status"} <= commands:
            values.add("run")
        if {"guest-file-open", "guest-file-close"} <= commands and (
            "guest-file-read" in commands or "guest-file-write" in commands
        ):
            values.add("path")
        if self.config.serial_console is not None:
            values.add("serial")
        return frozenset(values)

    def open_serial(self) -> Process:
        """Open the explicitly configured raw VM console."""
        if self.config.serial_console is None:
            raise NotImplementedError("QEMU host has no configured serial console")
        return self.config.serial_console.open()

    def connect(self) -> None:
        if self._commands is not None:
            return
        transport = self.transport
        transport.execute({"execute": "guest-ping"}, self.config.agent_timeout)
        info = transport.execute({"execute": "guest-info"}, self.config.agent_timeout)
        if not isinstance(info, typing.Mapping):
            raise ConnectionError("guest-info returned a non-object result")
        entries = info.get("supported_commands", ())
        commands = set()
        if isinstance(entries, typing.Iterable):
            for entry in entries:
                if (
                    isinstance(entry, typing.Mapping)
                    and entry.get("enabled", True)
                    and isinstance(entry.get("name"), str)
                ):
                    commands.add(typing.cast(str, entry["name"]))
        self._commands = frozenset(commands)
        if "guest-get-osinfo" in commands:
            value = transport.execute(
                {"execute": "guest-get-osinfo"}, self.config.agent_timeout
            )
            if isinstance(value, typing.Mapping):
                self._os_info = value
        if "guest-get-host-name" in commands:
            value = transport.execute(
                {"execute": "guest-get-host-name"},
                self.config.agent_timeout,
            )
            if isinstance(value, typing.Mapping):
                hostname = value.get("host-name")
                self._hostname = str(hostname) if hostname else None

    def close(self) -> None:
        self._commands = None
        self._os_info = {}
        self._hostname = None
        self._path_backend = None
        if self._transport is not None:
            transport, self._transport = self._transport, None
            transport.close()
        if self._ssh_host is not None:
            ssh, self._ssh_host = self._ssh_host, None
            ssh.close()

    @property
    def _windows(self) -> bool:
        self.connect()
        values = " ".join(
            str(self._os_info.get(key, "")) for key in ("id", "name", "pretty-name")
        ).casefold()
        return "windows" in values or "mswindows" in values

    @property
    def shell_flavour(self) -> ShellFlavour:
        if self.config.dialect != "auto":
            return typing.cast(ShellFlavour, self.config.dialect)
        return POWERSHELL if self._windows else POSIX_SHELL

    def info(self) -> HostInfo:
        self.connect()
        os_id = self._os_info.get("id")
        return HostInfo(
            hostname=self._hostname,
            os_family=("windows" if self._windows else str(os_id) if os_id else None),
            os_name=typing.cast(
                typing.Optional[str],
                self._os_info.get("pretty-name") or self._os_info.get("name"),
            ),
            os_version=typing.cast(
                typing.Optional[str],
                self._os_info.get("version") or self._os_info.get("version-id"),
            ),
            architecture=typing.cast(
                typing.Optional[str], self._os_info.get("machine")
            ),
        )

    def path(
        self, *segments: PathLike, backend: typing.Optional[str] = None
    ) -> HostPath:
        if "path" not in self.capabilities:
            raise NotImplementedError("guest agent does not provide file RPCs")
        if backend not in (None, "qga"):
            raise ValueError(f"unsupported QEMU path backend: {backend!r}")
        if self._path_backend is None:
            self._path_backend = QgaPathBackend(
                self.transport,
                supported_commands=self.supported_commands,
                timeout=self.config.agent_timeout,
            )
        path_backend = self._path_backend
        selection = self.config.path_flavor
        windows = (
            self._windows
            if selection == "auto"
            else issubclass(
                typing.cast(PathnameConstructor, selection), PureWindowsPath
            )
        )
        if windows:
            return WindowsQemuPath(*(segments or ("C:\\",)), backend=path_backend)
        return PosixQemuPath(*(segments or ("/",)), backend=path_backend)

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
        if "run" not in self.capabilities:
            raise NotImplementedError("guest agent does not provide guest-exec")
        direct = starts_direct_command(cmds)
        if direct is not None:
            command, args = direct
            if executable is not None:
                raise NotImplementedError(
                    "executable cannot be combined with a direct QGA command"
                )
            if cwd is not None:
                raise NotImplementedError("QGA direct argv execution cannot apply cwd")
        else:
            script = self.shell_flavour.script(cmds, cwd=cwd, env=None)
            selected_executable = executable
            if selected_executable is None and self._windows:
                selected_executable = {
                    "powershell.exe": (
                        r"C:\Windows\System32\WindowsPowerShell" r"\v1.0\powershell.exe"
                    ),
                    "cmd.exe": r"C:\Windows\System32\cmd.exe",
                }.get(self.shell_flavour.default_executable.casefold())
                if selected_executable is None:
                    raise NotImplementedError(
                        "QGA Windows shell execution requires an absolute "
                        "executable path for this shell flavour"
                    )
            invocation = self.shell_flavour.invocation(
                script, executable=selected_executable
            )
            command, args = invocation[0], invocation[1:]
        return self.executor(
            command,
            *args,
            bufsize=bufsize,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            capture_output=capture_output,
            check=check,
            encoding=encoding,
            errors=errors,
            input=input,
            timeout=timeout,
            text=text,
        )
