"""QEMU virtual-machine host backed by QEMU Guest Agent."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import io
import subprocess
import typing
import warnings
import uuid
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from pathlib import PurePath as _StdPurePath
from urllib.parse import quote, unquote, urlencode

from pathlib_next import Path, Pathname, PosixPathname, WindowsPathname
from pathlib_next.utils.stat import FileStat

from ..executor import QemuExecutor
from ..executor._qga import (
    GuestAgentTransport,
    LibvirtGuestAgentTransport,
    QgaDisconnectedError,
    QgaTimeoutError,
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
    strict_uri_query,
    uri_host,
    uri_hostname,
)
from ._ssh import SshConfig, _SshTransport
from ._staged_io import copy_from, StagedOpenMixin, staged_open

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

    #: Declared rather than enforced inside `_from_parsed_uri`, so the
    #: whitelist can be read before a config is built: an ambient
    #: credential (the CLI's `HOSTCTL_PASSWORD`) is offered only where it
    #: is accepted. The base dispatcher enforces it.
    uri_credentials = (
        "password",
        "client_keys",
        "known_hosts",
        "transport_factory",
        "serial_console",
        "path_helper",
    )

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
    #: Guest-side helper supplying the metadata and namespace operations QGA
    #: has no RPC for. QGA's file RPCs move bytes and nothing else, so without
    #: one `stat`, `scandir` and every mutation are unavailable and the path
    #: provider reports `degraded`. hostctl ships no implementation yet (see
    #: `.agents/plans/qga_guest_path_helper.md`); supplying one is how a
    #: caller with a known guest gets a complete path surface today.
    path_helper: typing.Optional["GuestPathHelper"] = dataclasses.field(
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
        if isinstance(self.path_flavor, str):
            self.path_flavor = _path_selection(self.path_flavor)
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
        authority = f"{quote(ssh.username, safe='')}@{uri_host(ssh.host)}:{ssh.port}"
        return (
            f"qga+ssh://{authority}/{quote(self.domain, safe='')}?"
            f"{urlencode(query)}"
        )

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> QemuConfig:
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
            "path_helper": credentials.get("path_helper"),
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
            uri_hostname(parsed),
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
        self._ssh_transport: typing.Optional[_SshTransport] = None
        self._commands: typing.Optional[typing.FrozenSet[str]] = None
        self._os_info: typing.Mapping[str, object] = {}
        self._hostname: typing.Optional[str] = None
        self._executor = QemuExecutor(lambda: self.transport)
        self._path_backend: typing.Optional[QgaPathBackend] = None
        self._path_provider: typing.Optional[object] = None

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
                ssh_transport = typing.cast(
                    SshConfig, self.config.ssh
                )._create_transport()
                try:
                    ssh_transport.connect()
                except Exception:
                    try:
                        ssh_transport.close()
                    except Exception:
                        pass
                    raise
                self._ssh_transport = ssh_transport
                socket_path = self.config.socket_path or (
                    f"/run/qemu-server/{self.config.domain}.qga"
                )
                self._transport = SshUnixGuestAgentTransport(
                    socket_path,
                    lambda: ssh_transport.ssh,
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
        # `_commands` is the "already discovered" flag, so it is committed
        # only once the optional probes below have run. Set first, a single
        # transient probe failure froze a Windows guest as POSIX for the
        # object's lifetime, and a retried connect() silently succeeded
        # without re-probing.
        discovered = frozenset(commands)
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
        self._commands = discovered

    def close(self) -> None:
        self._commands = None
        self._os_info = {}
        self._hostname = None
        self._path_backend = None
        self._path_provider = None
        transport, self._transport = self._transport, None
        ssh, self._ssh_transport = self._ssh_transport, None
        try:
            if transport is not None:
                transport.close()
        finally:
            if ssh is not None:
                ssh.close()

    @property
    def _windows(self) -> bool:
        self.connect()
        values = " ".join(
            str(self._os_info.get(key, "")) for key in ("id", "name", "pretty-name")
        ).casefold()
        return "windows" in values

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
                helper=self.config.path_helper,
                timeout=self.config.agent_timeout,
            )
        path_backend = self._path_backend
        if (
            self._path_provider is None
            or self._path_provider.backend is not path_backend
        ):
            from ..provider.transports import QgaPathProvider

            # The provider declares exactly the operations the probed guest
            # agent supports, so an unavailable metadata/mutation RPC is
            # rejected rather than routed to a different backend.
            self._path_provider = QgaPathProvider(self._qga_path, path_backend)
        if not self._path_provider.probe().usable:
            raise NotImplementedError("guest agent does not provide usable file RPCs")
        return self._path_provider.path(*segments)

    @property
    def path_provider(self):
        """The QGA path provider, once :meth:`path` has assembled it."""
        return self._path_provider

    def _qga_path(self, *segments: PathLike) -> HostPath:
        """Build a QGA-backed guest path with the configured pathname flavour."""
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
            if env is not None:
                # guest-exec's `env` list is handed to the guest agent as
                # envp, which REPLACES the child environment -- no PATH, no
                # HOME, no SystemRoot. hostctl's contract is additive, and a
                # direct argv execution has no shell to embed assignments
                # into, so this is refused exactly as cwd is.
                raise NotImplementedError("QGA direct argv execution cannot apply env")
        else:
            script = self.shell_flavour.script(cmds, cwd=cwd, env=env)
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
            # Never natively: the assignments are already in the script, and
            # sending them again would replace the guest environment.
            env=None,
            capture_output=capture_output,
            check=check,
            encoding=encoding,
            errors=errors,
            input=input,
            timeout=timeout,
            text=text,
        )


class GuestPathHelper(typing.Protocol):
    """Positively probed OS helper operations not supplied by QGA file RPCs."""

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat: ...

    def scandir(self, path: str) -> typing.Iterable[typing.Tuple[str, FileStat]]: ...

    def mkdir(self, path: str, mode: int) -> None: ...

    def unlink(self, path: str, *, missing_ok: bool = False) -> None: ...

    def rmdir(self, path: str) -> None: ...

    def rename(self, path: str, target: str, *, replace: bool = False) -> None: ...

    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None: ...


def _qga_error(exc: Exception, path: str) -> OSError:
    """Translate transport-specific QGA errors without importing a provider."""
    if isinstance(exc, (QgaTimeoutError, QgaDisconnectedError)):
        return exc
    name = str(
        getattr(exc, "error_class", "")
        or getattr(exc, "name", "")
        or getattr(exc, "code", "")
        or type(exc).__name__
    ).lower()
    message = (
        str(getattr(exc, "description", "") or getattr(exc, "message", "") or exc)
        or path
    )
    detail = f"{name} {message.lower()}"
    if any(
        value in detail
        for value in (
            "notfound",
            "enoent",
            "filenotfound",
            "no such file or directory",
            "cannot find the file",
            "cannot find the path",
        )
    ):
        return FileNotFoundError(message)
    if any(
        value in detail
        for value in ("permission", "denied", "eacces", "access is denied")
    ):
        return PermissionError(message)
    if any(value in detail for value in ("eexist", "already exists", "file exists")):
        return FileExistsError(message)
    if any(value in detail for value in ("isdir", "eisdir", "is a directory")):
        return IsADirectoryError(message)
    return OSError(message)


class QgaPathBackend:
    """Bounded file transfer plus capability-gated guest path helpers."""

    chunk_size = 48 * 1024
    _READ_COMMANDS = frozenset(
        {"guest-file-open", "guest-file-read", "guest-file-close"}
    )
    _WRITE_COMMANDS = frozenset(
        {
            "guest-file-open",
            "guest-file-write",
            "guest-file-flush",
            "guest-file-close",
        }
    )

    def __init__(
        self,
        transport: GuestAgentTransport,
        *,
        supported_commands: typing.Iterable[str],
        helper: typing.Optional[GuestPathHelper] = None,
        timeout: typing.Optional[float] = None,
        chunk_size: int = chunk_size,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.transport = transport
        self.supported_commands = frozenset(supported_commands)
        self.helper = helper
        self.timeout = timeout
        self.chunk_size = chunk_size

    def _require(self, commands: typing.Iterable[str]) -> None:
        missing = set(commands) - self.supported_commands
        if missing:
            values = ", ".join(sorted(missing))
            raise NotImplementedError(f"QGA commands are unavailable: {values}")

    def _execute(
        self, command: str, arguments: typing.Optional[dict] = None, *, path: str
    ) -> object:
        self._require((command,))
        request: typing.Dict[str, object] = {"execute": command}
        if arguments is not None:
            request["arguments"] = arguments
        try:
            value = self.transport.execute(request, timeout=self.timeout)
        except Exception as exc:
            raise _qga_error(exc, path) from exc
        if isinstance(value, dict) and set(value) == {"return"}:
            return value["return"]
        return value

    def _open(self, path: str, mode: str) -> int:
        value = self._execute(
            "guest-file-open", {"path": path, "mode": mode}, path=path
        )
        if isinstance(value, dict):
            value = value.get("handle")
        if not isinstance(value, int):
            raise OSError("QGA guest-file-open returned an invalid handle")
        return value

    def _close(self, handle: int, path: str) -> None:
        self._execute("guest-file-close", {"handle": handle}, path=path)

    def seek(
        self,
        handle: int,
        offset: int,
        whence: typing.Union[str, int] = "set",
        *,
        path: str,
    ) -> int:
        """Seek an open QGA file handle and return its resulting position."""
        if isinstance(whence, str):
            if whence not in {"set", "cur", "end"}:
                raise ValueError(f"invalid seek origin: {whence!r}")
            whence_value: object = {"name": whence}
        elif isinstance(whence, int) and whence in (0, 1, 2):
            whence_value = whence
        else:
            raise ValueError(f"invalid seek origin: {whence!r}")
        value = self._execute(
            "guest-file-seek",
            {
                "handle": handle,
                "offset": offset,
                "whence": whence_value,
            },
            path=path,
        )
        if not isinstance(value, dict) or not isinstance(value.get("position"), int):
            raise OSError("QGA guest-file-seek returned an invalid position")
        return typing.cast(int, value["position"])

    def read_bytes(self, path: str) -> bytes:
        self._require(self._READ_COMMANDS)
        handle = self._open(path, "rb")
        chunks: typing.List[bytes] = []
        failed = False
        try:
            while True:
                value = self._execute(
                    "guest-file-read",
                    {"handle": handle, "count": self.chunk_size},
                    path=path,
                )
                if not isinstance(value, dict):
                    raise OSError("QGA guest-file-read returned invalid data")
                encoded = value.get("buf-b64", "")
                if not isinstance(encoded, str):
                    raise OSError("QGA guest-file-read returned invalid content")
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise OSError(
                        "QGA guest-file-read returned invalid Base64"
                    ) from exc
                count = value.get("count", len(data))
                if not isinstance(count, int) or count != len(data):
                    raise OSError("QGA guest-file-read returned an invalid count")
                eof = value.get("eof", False)
                if not isinstance(eof, bool):
                    raise OSError("QGA guest-file-read returned an invalid EOF flag")
                chunks.append(data)
                if eof or count == 0:
                    break
        except BaseException:
            failed = True
            raise
        finally:
            try:
                self._close(handle, path)
            except Exception:
                if not failed:
                    raise
        return b"".join(chunks)

    def read_handle(
        self, handle: int, count: int, *, path: str
    ) -> typing.Tuple[bytes, bool]:
        if count <= 0:
            return b"", True
        value = self._execute(
            "guest-file-read",
            {"handle": handle, "count": count},
            path=path,
        )
        if not isinstance(value, dict):
            raise OSError("QGA guest-file-read returned invalid data")
        encoded = value.get("buf-b64", "")
        if not isinstance(encoded, str):
            raise OSError("QGA guest-file-read returned invalid content")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise OSError("QGA guest-file-read returned invalid Base64") from exc
        actual = value.get("count", len(data))
        eof = value.get("eof", False)
        if not isinstance(actual, int) or actual != len(data):
            raise OSError("QGA guest-file-read returned an invalid count")
        if not isinstance(eof, bool):
            raise OSError("QGA guest-file-read returned an invalid EOF flag")
        return data, eof or not data

    def open_read(self, path: str) -> io.BufferedReader:
        self._require(self._READ_COMMANDS)
        return io.BufferedReader(_QgaReadStream(self, path))

    def _write_direct(self, path: str, value: bytes) -> None:
        self._require(self._WRITE_COMMANDS)
        handle = self._open(path, "wb")
        failed = False
        try:
            for offset in range(0, len(value), self.chunk_size):
                pending = value[offset : offset + self.chunk_size]
                while pending:
                    encoded = base64.b64encode(pending).decode("ascii")
                    result = self._execute(
                        "guest-file-write",
                        {"handle": handle, "buf-b64": encoded},
                        path=path,
                    )
                    if not isinstance(result, dict):
                        raise OSError("QGA guest-file-write returned invalid data")
                    count = result.get("count", len(pending))
                    if not isinstance(count, int) or count <= 0 or count > len(pending):
                        raise OSError("QGA guest-file-write returned an invalid count")
                    pending = pending[count:]
            self._execute("guest-file-flush", {"handle": handle}, path=path)
        except BaseException:
            failed = True
            raise
        finally:
            try:
                self._close(handle, path)
            except Exception:
                if not failed:
                    raise

    def write_bytes(self, path: str, value: bytes, *, exclusive: bool = False) -> None:
        if self.helper is None:
            if exclusive:
                raise NotImplementedError(
                    "exclusive QGA writes require a transactional guest helper"
                )
            self._write_direct(path, value)
            return
        temporary = f"{path}.hostctl-{uuid.uuid4().hex}"
        try:
            self._write_direct(temporary, value)
            self._helper_method("rename")(temporary, path, replace=not exclusive)
        except Exception:
            try:
                self._helper_method("unlink")(temporary, missing_ok=True)
            except Exception:
                pass
            raise

    def _helper_method(self, operation: str):
        if self.helper is None:
            raise NotImplementedError(
                f"QGA {operation} requires a positively probed guest helper"
            )
        method = getattr(self.helper, operation, None)
        if not callable(method):
            raise NotImplementedError(f"QGA helper does not support {operation}")
        return method

    def exists(self, path: str, *, follow_symlinks: bool = True) -> bool:
        """Answer existence with whatever the guest agent actually offers.

        `stat` needs a probed helper, but existence does not: opening a path
        for reading distinguishes "absent" from "there but unreadable" using
        only the file RPCs every QGA build has. Without this, `exists()` on
        every host hostctl can build raised `NotImplementedError` instead of
        returning a boolean, which `docs/guide/contracts.md` requires -- and
        that is what made `Path.copy()` into a guest impossible.
        """
        if self.helper is not None:
            try:
                self.stat(path, follow_symlinks=follow_symlinks)
            except (FileNotFoundError, NotADirectoryError):
                return False
            return True
        try:
            handle = self._open(path, "r")
        except FileNotFoundError:
            return False
        except NotImplementedError:
            raise
        except OSError:
            # Permission denied, EISDIR on a Windows guest: something is
            # there, it just cannot be opened this way.
            return True
        try:
            return True
        finally:
            try:
                self._close(handle, path)
            except Exception:
                pass

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat:
        if self.helper is None and not self.exists(path):
            # Absence the file RPCs can prove is reported as absence, not as
            # "this build cannot describe files". Only that distinction lets
            # a generic `Path.copy()` push a file into a helper-less guest:
            # it reads FileNotFoundError as "create it" and anything else as
            # a failure. An entry that *is* there still raises
            # NotImplementedError below -- hostctl knows it exists and
            # genuinely cannot describe it.
            raise FileNotFoundError(path)
        return self._helper_method("stat")(path, follow_symlinks=follow_symlinks)

    def scandir(self, path: str) -> typing.List[typing.Tuple[str, FileStat]]:
        return list(self._helper_method("scandir")(path))

    def mkdir(self, path: str, mode: int) -> None:
        self._helper_method("mkdir")(path, mode)

    def unlink(self, path: str, *, missing_ok: bool = False) -> None:
        self._helper_method("unlink")(path, missing_ok=missing_ok)

    def rmdir(self, path: str) -> None:
        self._helper_method("rmdir")(path)

    def rename(self, path: str, target: str) -> None:
        self._helper_method("rename")(path, target)

    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None:
        self._helper_method("chmod")(path, mode, follow_symlinks=follow_symlinks)

    def symlink(self, path: str, target: str) -> None:
        raise NotImplementedError(
            "QEMU Guest Agent has no symlink RPC; the guest-file-* commands "
            "only open, read, write, seek, flush, and close regular files"
        )

    def readlink(self, path: str) -> str:
        raise NotImplementedError(
            "QEMU Guest Agent has no readlink RPC; the guest-file-* commands "
            "only open, read, write, seek, flush, and close regular files"
        )


class _QgaReadStream(io.RawIOBase):
    """Lazy QGA guest-file-read stream with one bounded request per fill."""

    def __init__(self, backend: QgaPathBackend, path: str) -> None:
        self._backend = backend
        self._path = path
        self._handle: typing.Optional[int] = None
        self._handle = backend._open(path, "rb")
        self._eof = False

    def readable(self) -> bool:
        return True

    def readinto(self, target: bytearray) -> int:
        if self._eof or not target:
            return 0
        data, eof = self._backend.read_handle(
            typing.cast(int, self._handle),
            min(len(target), self._backend.chunk_size),
            path=self._path,
        )
        if data:
            target[: len(data)] = data
        self._eof = eof
        return len(data)

    def close(self) -> None:
        if not self.closed and self._handle is not None:
            handle, self._handle = self._handle, None
            try:
                self._backend._close(handle, self._path)
            finally:
                super().close()
        else:
            super().close()

    def __del__(self) -> None:
        # Never issue the guest RPC from the collector. `RawIOBase.__del__`
        # calls `close()`, and `close()` sends `guest-file-close`; the framed
        # session's lock is not reentrant, so a collection firing inside
        # `execute()` on the same thread deadlocked the transport for good.
        # Dropping the handle leaks one guest-side file handle, which is the
        # lesser harm and is what the warning is for.
        if not self.closed and self._handle is not None:
            warnings.warn(
                f"unclosed QGA read stream for {self._path}: the guest handle "
                "was not released",
                ResourceWarning,
                stacklevel=2,
            )
            self._handle = None
        try:
            super().close()
        except Exception:
            pass


class _QgaPathMixin(StagedOpenMixin):
    __slots__ = ()

    def exists(self, *, follow_symlinks: bool = True) -> bool:
        # pathlib_next answers this from `stat()`, swallowing OSError but not
        # NotImplementedError -- so on a helper-less guest the documented
        # boolean probe raised. The backend can still answer with file RPCs.
        return self.backend.exists(str(self), follow_symlinks=follow_symlinks)

    def copy(self, target, **kwargs):
        return Path.copy(self, target, **kwargs)

    def move(self, target, **kwargs):
        return Path.move(self, target, **kwargs)

    def _copy_from(self, source, **kwargs):
        """CPython 3.14's `Path.copy()` destination hook.

        One shared implementation (`_staged_io.copy_from`). This body used to
        be copied verbatim into four modules, and every copy diverged from
        what stdlib actually calls it with: it raised `FileExistsError` where
        stdlib overwrites, and opened a directory `"rb"`.
        """
        return copy_from(self, source, **kwargs)

    @property
    def backend(self) -> QgaPathBackend:
        return self._backend

    def with_segments(self, *segments: str):
        return type(self)(*segments, backend=self.backend)

    def __truediv__(self, key):
        return type(self)(self, key, backend=self.backend)

    def joinpath(self, *args):
        return type(self)(self, *args, backend=self.backend)

    @property
    def parent(self):
        return type(self)(str(super().parent), backend=self.backend)

    def stat(self, *, follow_symlinks: bool = True) -> FileStat:
        return self.backend.stat(str(self), follow_symlinks=follow_symlinks)

    def _scandir(self):
        yield from self.backend.scandir(str(self))

    def iterdir(self):
        for name, _ in self._scandir():
            yield self / name

    def _open(self, mode="r", buffering=-1):
        del buffering
        return staged_open(self.backend, str(self), mode, label="QGA")

    def _symlink_target(self, target):
        """Normalise a `str` target WITH this path's backend.

        `Path._symlink_target` builds `type(self)(target)`, and these
        classes refuse a construction without a backend -- so the public
        `symlink_to()` raised `TypeError` before reaching the primitive.
        """
        return self.with_segments(target) if isinstance(target, str) else target

    def _symlink_to(self, target, target_is_directory: bool = False):
        """Always raise -- QGA exposes no symlink RPC.

        The guest agent's file protocol is limited to opening, reading,
        writing, seeking, flushing, and closing handles.  Emulating a
        symlink through ``guest-exec`` would be a different transport with
        different failure and permission semantics, so the gap is reported
        explicitly instead of being faked.
        """
        self.backend.symlink(str(self), str(target))

    def readlink(self):
        return self.with_segments(self.backend.readlink(str(self)))

    def _mkdir(self, mode: int):
        self.backend.mkdir(str(self), mode)

    def chmod(self, mode: int, *, follow_symlinks: bool = True):
        self.backend.chmod(str(self), mode, follow_symlinks=follow_symlinks)

    def unlink(self, missing_ok=False):
        self.backend.unlink(str(self), missing_ok=missing_ok)

    def rmdir(self):
        self.backend.rmdir(str(self))

    def rename(self, target):
        if not isinstance(target, _QgaPathMixin):
            target = type(self)(target, backend=self.backend)
        if target.backend is not self.backend:
            raise ValueError("cannot rename across QGA path backends")
        self.backend.rename(str(self), str(target))
        return target


class PosixQemuPath(_QgaPathMixin, PosixPathname, Path):
    """A POSIX guest path backed by QEMU Guest Agent."""

    __slots__ = ("_backend",)

    def __init__(self, *segments, backend=None):
        # Python 3.14's pathlib.PurePath.__init__ no longer accepts kwargs.
        # Path state is initialized by __new__; backend is attached there.
        if not hasattr(self, "_raw_paths") and not hasattr(self, "_parts"):
            _StdPurePath.__init__(self, *segments)

    def __new__(
        cls,
        *segments: typing.Union[str, PosixPathname],
        backend: typing.Optional[QgaPathBackend] = None,
    ):
        inherited = next(
            (
                segment.backend
                for segment in segments
                if isinstance(segment, _QgaPathMixin)
            ),
            None,
        )
        self = super().__new__(cls, *segments)
        self._backend = backend or inherited
        if self._backend is None:
            raise TypeError("PosixQemuPath requires a backend")
        return self


class WindowsQemuPath(_QgaPathMixin, WindowsPathname, Path):
    """A Windows guest path backed by QEMU Guest Agent."""

    __slots__ = ("_backend",)

    def __init__(self, *segments, backend=None):
        # Python 3.14's pathlib.PurePath.__init__ no longer accepts kwargs.
        # Path state is initialized by __new__; backend is attached there.
        if not hasattr(self, "_raw_paths") and not hasattr(self, "_parts"):
            _StdPurePath.__init__(self, *segments)

    def __new__(
        cls,
        *segments: typing.Union[str, WindowsPathname],
        backend: typing.Optional[QgaPathBackend] = None,
    ):
        inherited = next(
            (
                segment.backend
                for segment in segments
                if isinstance(segment, _QgaPathMixin)
            ),
            None,
        )
        self = super().__new__(cls, *segments)
        self._backend = backend or inherited
        if self._backend is None:
            raise TypeError("WindowsQemuPath requires a backend")
        return self
