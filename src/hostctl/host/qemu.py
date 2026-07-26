"""QEMU virtual-machine host backed by QEMU Guest Agent."""

from __future__ import annotations

import dataclasses
import subprocess
import typing
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import quote, unquote, urlencode

from pathlib_next import Pathname, PosixPathname, WindowsPathname

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
    strict_uri_credentials,
    strict_uri_query,
    uri_host,
)
from ._ssh import SshConfig, _SshTransport

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
        self._ssh_transport: typing.Optional[_SshTransport] = None
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


import base64
import binascii
import io
import typing
import uuid

from pathlib import PurePath as _StdPurePath
from pathlib_next import Path, PosixPathname, WindowsPathname
from pathlib_next.utils.stat import FileStat


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

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat:
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


class _WriteBackBytesIO(io.BytesIO):
    def __init__(
        self, value: bytes, commit: typing.Optional[typing.Callable[[bytes], None]]
    ) -> None:
        super().__init__(value)
        self._commit = commit

    def close(self) -> None:
        if not self.closed and self._commit is not None:
            value = self.getvalue()
            commit, self._commit = self._commit, None
            try:
                commit(value)
            finally:
                super().close()
        else:
            super().close()


class _QgaReadStream(io.RawIOBase):
    """Lazy QGA guest-file-read stream with one bounded request per fill."""

    def __init__(self, backend: QgaPathBackend, path: str) -> None:
        self._backend = backend
        self._path = path
        self._handle = backend._open(path, "rb")
        self._eof = False

    def readable(self) -> bool:
        return True

    def readinto(self, target: bytearray) -> int:
        if self._eof or not target:
            return 0
        data, eof = self._backend.read_handle(
            self._handle,
            min(len(target), self._backend.chunk_size),
            path=self._path,
        )
        if data:
            target[: len(data)] = data
        self._eof = eof
        return len(data)

    def close(self) -> None:
        if not self.closed:
            try:
                self._backend._close(self._handle, self._path)
            finally:
                super().close()


class _QgaPathMixin:
    __slots__ = ()

    def copy(self, target, **kwargs):
        return Path.copy(self, target, **kwargs)

    def move(self, target, **kwargs):
        return Path.move(self, target, **kwargs)

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
        if (
            not mode
            or sum(mode.count(value) for value in "rwax") != 1
            or mode.count("+") > 1
            or len(mode) != 1 + mode.count("+")
        ):
            raise ValueError(f"invalid mode: {mode!r}")
        readable = "r" in mode or "+" in mode
        writable = any(value in mode for value in "wax+")
        if "r" in mode and not writable:
            return self.backend.open_read(str(self))
        if "r" in mode or "a" in mode:
            try:
                value = self.backend.read_bytes(str(self))
            except FileNotFoundError:
                if "a" in mode:
                    value = b""
                else:
                    raise
        else:
            value = b""
        stream = _WriteBackBytesIO(
            value,
            (
                (
                    lambda data: self.backend.write_bytes(
                        str(self), data, exclusive="x" in mode
                    )
                )
                if writable
                else None
            ),
        )
        if "a" in mode:
            stream.seek(0, io.SEEK_END)
        elif not readable:
            stream.seek(0)
        return stream

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
