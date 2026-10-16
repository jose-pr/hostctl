"""QEMU virtual-machine host backed by QEMU Guest Agent."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import io
import logging
import re
import subprocess
import threading
import typing
import warnings
import uuid
from pathlib import PurePath, PureWindowsPath
from urllib.parse import quote, unquote, urlencode

from pathapi import Pathname, WindowsPathname
from pathapi import FileStat
from pathapi.protocols import Chmod, Create, Listing, Move, Remove, Stat
from pathapi.providers import notimplemented

from ..exceptions import QGAProtocolError
from ..executor import QEMUExecutor
from ..executor._qga import (
    GuestAgentTransport,
    LibvirtGuestAgentTransport,
    guest_oserror,
    SSHUnixGuestAgentTransport,
    UnixSocketGuestAgentTransport,
)
from ..process import Process, QEMUSerialConsole
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
    path_flavor_name,
    path_flavor_selection,
    starts_direct_command,
    strict_uri_query,
    uri_hostname,
)
from ._backend_path import PosixBackendPath, WindowsBackendPath
from ._ssh import SSHConfig, _SSHTransport

QEMUTransport = typing.Literal["libvirt", "unix", "ssh"]
PathnameConstructor = typing.Type[typing.Union[PurePath, Pathname]]
QEMUShellSelection = typing.Union[ShellFlavourSelection, typing.Literal["auto"]]
QEMUPathSelection = typing.Union[PathnameConstructor, typing.Literal["auto"]]


@dataclasses.dataclass
class QEMUConfig(HostConfig, schemes=("qemu+libvirt", "qga+unix", "qga+ssh")):
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
    transport: QEMUTransport = "libvirt"
    connection: typing.Optional[str] = None
    socket_path: typing.Optional[str] = None
    ssh: typing.Optional[SSHConfig] = dataclasses.field(default=None, repr=False)
    agent_timeout: float = 10.0
    #: Largest QGA reply this host will accept, in bytes. qemu-ga caps its
    #: own captured output at 16 MiB per stream and sends it Base64-encoded
    #: inside JSON, so a command producing that much needed roughly 21 MiB
    #: of reply to survive -- against a hard-wired 8 MiB the transports
    #: could not be told to raise. A rejected reply is not recoverable: the
    #: output is already gone.
    max_reply_size: int = 48 * 1024 * 1024
    dialect: QEMUShellSelection = "auto"
    path_flavor: QEMUPathSelection = "auto"
    transport_factory: typing.Optional[typing.Callable[[], GuestAgentTransport]] = (
        dataclasses.field(default=None, repr=False, compare=False)
    )
    serial_console: typing.Optional[QEMUSerialConsole] = dataclasses.field(
        default=None, repr=False, compare=False
    )
    #: Guest-side helper supplying the metadata and namespace operations QGA
    #: has no RPC for. QGA's file RPCs move bytes and nothing else, so without
    #: one `stat`, `scandir` and every mutation are unavailable and the path
    #: provider reports `degraded`. hostctl ships no implementation yet;
    #: supplying one is how a caller with a known guest gets a complete path
    #: surface today.
    path_helper: typing.Optional["GuestPathHelper"] = dataclasses.field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.domain or "\x00" in self.domain:
            raise ValueError("domain must not be empty or contain NUL")
        if self.transport not in typing.get_args(QEMUTransport):
            raise ValueError(f"unsupported QEMU transport: {self.transport}")
        if self.agent_timeout <= 0:
            raise ValueError("agent_timeout must be positive")
        if self.max_reply_size <= 0:
            raise ValueError("max_reply_size must be positive")
        if self.transport == "unix" and not self.socket_path:
            raise ValueError("unix QGA transport requires socket_path")
        if self.transport == "ssh" and self.ssh is None:
            raise ValueError("SSH QGA transport requires an SSHConfig")
        if self.transport != "ssh" and self.ssh is not None:
            raise ValueError("ssh config requires transport='ssh'")
        if self.dialect != "auto":
            self.dialect = shell_flavour(self.dialect)
        self.path_flavor = path_flavor_selection(self.path_flavor, allow_auto=True)

    @property
    def connection_uri(self) -> str:
        dialect = "auto" if self.dialect == "auto" else str(self.dialect)
        path_flavor = path_flavor_name(self.path_flavor)
        query: typing.Dict[str, object] = {
            "agent_timeout": self.agent_timeout,
            "max_reply_size": self.max_reply_size,
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
        ssh = typing.cast(SSHConfig, self.ssh)
        query["socket_path"] = self.socket_path or (
            f"/run/qemu-server/{self.domain}.qga"
        )
        from netimps import join_host

        user = f"{quote(ssh.username, safe='')}@" if ssh.username else ""
        authority = f"{user}{join_host(ssh.host, ssh.port)}"
        return (
            f"qga+ssh://{authority}/{quote(self.domain, safe='')}?"
            f"{urlencode(query)}"
        )

    #: Credentials every QEMU scheme accepts. `password`, `client_keys` and
    #: `known_hosts` are meaningful only where an SSH tunnel carries the
    #: agent stream, so only `qga+ssh` takes them.
    _COMMON_CREDENTIALS = ("transport_factory", "serial_console", "path_helper")

    @classmethod
    def _uri_credentials_for(cls, parsed):
        # A `password=` on a libvirt or Unix socket URI would read as
        # authentication that had been applied.
        if parsed.scheme.casefold() != "qga+ssh":
            return cls._COMMON_CREDENTIALS
        return cls.uri_credentials

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> QEMUConfig:
        scheme = parsed.scheme.casefold()
        query = strict_uri_query(
            parsed,
            (
                "agent_timeout",
                "connection",
                "dialect",
                "domain",
                "max_reply_size",
                "path_flavor",
                "socket_path",
            ),
        )
        try:
            timeout = float(query.get("agent_timeout", "10"))
        except ValueError as exc:
            raise ValueError("agent_timeout must be numeric") from exc
        try:
            max_reply_size = int(query.get("max_reply_size", cls.max_reply_size))
        except ValueError as exc:
            raise ValueError("max_reply_size must be an integer") from exc
        common = {
            "agent_timeout": timeout,
            "max_reply_size": max_reply_size,
            "dialect": (
                "auto"
                if query.get("dialect", "auto") == "auto"
                else shell_flavour(query["dialect"])
            ),
            "path_flavor": path_flavor_selection(
                query.get("path_flavor", "auto"), allow_auto=True
            ),
            "transport_factory": credentials.get("transport_factory"),
            "serial_console": credentials.get("serial_console"),
            "path_helper": credentials.get("path_helper"),
        }
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
        from netimps import get_default_port

        ssh = SSHConfig(
            uri_hostname(parsed),
            port=parsed.port or get_default_port("ssh"),
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

    def _create_host(self) -> QEMUHost:
        return QEMUHost(self)


#: Sentinel: the guest helper has not been probed yet. `None` is a real
#: answer -- this guest has no helper -- and must not trigger a re-probe on
#: every `path()` call.
log = logging.getLogger(__name__)

_UNPROBED = object()


class QEMUHost(Host):
    """A guest OS reached through QEMU Guest Agent."""

    def __init__(self, config: QEMUConfig) -> None:
        self.config = config
        # Lazy initialisation is racy without this. Two threads reaching
        # `transport` first each built a transport, each opened its own SSH
        # connection, and the loser's was dropped on the floor still open --
        # a connection nobody owned and `close()` could not reach.
        self._lock = threading.RLock()
        self._transport: typing.Optional[GuestAgentTransport] = None
        self._ssh_transport: typing.Optional[_SSHTransport] = None
        self._commands: typing.Optional[typing.FrozenSet[str]] = None
        self._os_info: typing.Mapping[str, object] = {}
        self._hostname: typing.Optional[str] = None
        #: qemu-ga's `guest-info` version string, once discovered.
        self._agent_version: typing.Optional[str] = None
        self._executor = QEMUExecutor(
            lambda: self.transport,
            agent_timeout=config.agent_timeout,
            merged_capture=self._merges_output,
        )
        self._path_backend: typing.Optional[QGAPathBackend] = None
        #: The probed guest helper, or `None` once probing has failed.
        #: `_UNPROBED` distinguishes "not asked yet" from "asked, no helper".
        self._path_helper: object = _UNPROBED
        self._path_provider: typing.Optional[object] = None

    @property
    def transport(self) -> GuestAgentTransport:
        if self._transport is not None:
            return self._transport
        with self._lock:
            return self._build_transport()

    def _build_transport(self) -> GuestAgentTransport:
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
                    max_reply_size=self.config.max_reply_size,
                )
            else:
                ssh_transport = typing.cast(
                    SSHConfig, self.config.ssh
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
                self._transport = SSHUnixGuestAgentTransport(
                    socket_path,
                    lambda: ssh_transport.ssh,
                    timeout=self.config.agent_timeout,
                    max_reply_size=self.config.max_reply_size,
                )
        return self._transport

    @property
    def executor(self) -> QEMUExecutor:
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

    def open_serial(
        self,
        *,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> Process:
        """Open the explicitly configured VM console; `str` I/O with `encoding`."""
        if self.config.serial_console is None:
            raise NotImplementedError("QEMU host has no configured serial console")
        return self.config.serial_console.open(encoding=encoding, errors=errors)

    def connect(self) -> None:
        if self._commands is not None:
            return
        with self._lock:
            self._discover()

    def _discover(self) -> None:
        if self._commands is not None:
            return
        transport = self.transport
        transport.execute({"execute": "guest-ping"}, self.config.agent_timeout)
        info = transport.execute({"execute": "guest-info"}, self.config.agent_timeout)
        if not isinstance(info, typing.Mapping):
            raise ConnectionError("guest-info returned a non-object result")
        version = info.get("version")
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
        self._agent_version = version if isinstance(version, str) else None
        self._commands = discovered

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        self._commands = None
        self._agent_version = None
        self._os_info = {}
        self._hostname = None
        self._path_helper = _UNPROBED
        backend, self._path_backend = self._path_backend, None
        self._path_provider = None
        if backend is not None:
            # A path handed out earlier still holds this backend, and the
            # backend still holds the transport. Dropping the host's
            # reference did nothing to that path: its next operation
            # reconnected the transport -- a fresh SSH connection nobody
            # owned and no `close()` could reach.
            backend.invalidate()
        transport, self._transport = self._transport, None
        ssh, self._ssh_transport = self._ssh_transport, None
        try:
            if transport is not None:
                transport.close()
        finally:
            if ssh is not None:
                ssh.close()

    #: qemu-ga commands only one family implements. An administrator can
    #: blocklist `guest-get-osinfo` (that is what `enabled` in `guest-info`
    #: is for), and then the command set is the only positive evidence left.
    _WINDOWS_ONLY_COMMANDS = frozenset({"guest-get-devices"})
    _POSIX_ONLY_COMMANDS = frozenset({"guest-get-cpustats", "guest-get-diskstats"})

    def _probe_os_family(self) -> typing.Optional[str]:
        """The guest's OS family, or `None` when nothing positively said.

        Every branch here is positive evidence. Reading "not Windows" out of
        an `guest-get-osinfo` that never ran is a guess, and the project's
        rule for auto-detection is to raise rather than guess.
        """
        self.connect()
        values = " ".join(
            str(self._os_info.get(key, "")) for key in ("id", "name", "pretty-name")
        ).casefold()
        if "windows" in values:
            return "windows"
        kernel = str(self._os_info.get("kernel-name", "")).strip().casefold()
        if kernel:
            # `Linux`, `FreeBSD`, ... -- the family, where `id` is the
            # distribution (`ubuntu`, `rhel`) every other transport does
            # not report here.
            return kernel
        if self._os_info:
            # osinfo answered and named no kernel: an older agent, but the
            # answer itself is evidence the guest is not Windows.
            return "posix"
        commands = self._commands or frozenset()
        if commands & self._WINDOWS_ONLY_COMMANDS:
            return "windows"
        if commands & self._POSIX_ONLY_COMMANDS:
            return "posix"
        return None

    def _merges_output(self) -> bool:
        """Whether the agent can interleave stderr into stdout itself.

        `capture-output: "merged"` exists from qemu-ga 8.0 and is compiled
        out of the Windows agent, so both facts must be positively known.
        """
        self.connect()
        match = re.match(r"(\d+)\.(\d+)", self._agent_version or "")
        if not match or (int(match[1]), int(match[2])) < (8, 0):
            return False
        family = self._probe_os_family()
        return family is not None and "windows" not in family

    @property
    def _windows(self) -> bool:
        family = self._probe_os_family()
        if family is None:
            raise NotImplementedError(
                "this guest agent did not report an OS: 'guest-get-osinfo' is "
                "unavailable or blocklisted and the advertised command set "
                "names no family. Set dialect= and path_flavor= explicitly "
                "(for example dialect=POSIX_SHELL, path_flavor='posix')."
            )
        return family == "windows"

    @property
    def shell_flavour(self) -> ShellFlavour:
        if self.config.dialect != "auto":
            return typing.cast(ShellFlavour, self.config.dialect)
        return POWERSHELL if self._windows else POSIX_SHELL

    def info(self) -> HostInfo:
        self.connect()
        # `os_family` is a FAMILY (`linux`, as over SSH), not
        # `guest-get-osinfo`'s `id`, which is an os-release id such as
        # `ubuntu`; `id` and `pretty-name` feed `os_name`.
        return HostInfo(
            hostname=self._hostname,
            os_family=self._probe_os_family(),
            os_name=typing.cast(
                typing.Optional[str],
                self._os_info.get("pretty-name")
                or self._os_info.get("name")
                or self._os_info.get("id"),
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
            self._path_backend = QGAPathBackend(
                self.transport,
                supported_commands=self.supported_commands,
                helper=self.config.path_helper or self._guest_path_helper(),
                timeout=self.config.agent_timeout,
            )
        path_backend = self._path_backend
        if (
            self._path_provider is None
            or self._path_provider.backend is not path_backend
        ):
            from ..provider.transports import QGAPathProvider

            # The provider declares exactly the operations the probed guest
            # agent supports, so an unavailable metadata/mutation RPC is
            # rejected rather than routed to a different backend.
            self._path_provider = QGAPathProvider(self._qga_path, path_backend)
        if not self._path_provider.probe().usable:
            raise NotImplementedError("guest agent does not provide usable file RPCs")
        return self._path_provider.path(*segments)

    def _guest_path_helper(self):
        """Probe the guest for a metadata/namespace helper, or answer None.

        QGA's file RPCs move bytes and nothing else, so `stat`, `scandir`
        and every mutation need a guest-side helper. One is built here from
        `guest-exec` -- positively probed, never assumed: `stat -c` and
        `find -printf` are GNU spellings busybox and the BSDs do not share,
        and a helper that assumes GNU and silently mis-parses is worse than
        none. A failed probe returns `None`, which leaves the backend in
        exactly the degraded mode it had before: reads and writes keep
        working, and only the operations that genuinely need a helper are
        unavailable.

        `QEMUConfig(path_helper=...)` overrides this entirely.
        """
        if self._path_helper is not _UNPROBED:
            return self._path_helper
        self._path_helper = None
        if not {"guest-exec", "guest-exec-status"} <= self.supported_commands:
            return None
        from ._qga_helper import PosixGuestPathHelper, WindowsGuestPathHelper

        family = self._probe_os_family()
        if family is None:
            # The same refusal `shell_flavour` makes: a helper for the wrong
            # family would run commands the guest does not have.
            return None
        helper_type = (
            WindowsGuestPathHelper if family == "windows" else PosixGuestPathHelper
        )
        try:
            self._path_helper = helper_type.probe(self._executor)
        except Exception:
            log.debug("guest path helper probe failed", exc_info=True)
            self._path_helper = None
        return self._path_helper

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
            return WindowsQEMUPath(*(segments or ("C:\\",)), backend=path_backend)
        return PosixQEMUPath(*(segments or ("/",)), backend=path_backend)

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


class QGAPathBackend:
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
        self._closed = False
        self.supported_commands = frozenset(supported_commands)
        self.helper = helper
        self.timeout = timeout
        self.chunk_size = chunk_size

    def invalidate(self) -> None:
        """Refuse further work: the host that owned this backend is closed."""
        self._closed = True

    def _require(self, commands: typing.Iterable[str]) -> None:
        if self._closed:
            raise ValueError("the QEMU host that owns this path is closed")
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
            raise guest_oserror(exc, path) from exc
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

    def _read_chunk(
        self, handle: int, count: int, *, path: str
    ) -> typing.Tuple[bytes, bool]:
        """One `guest-file-read` round trip: the bytes and whether EOF came."""
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
        if len(data) > count:
            raise QGAProtocolError(
                "QGA guest-file-read returned more bytes than requested"
            )
        actual = value.get("count", len(data))
        if not isinstance(actual, int) or actual != len(data):
            raise OSError("QGA guest-file-read returned an invalid count")
        eof = value.get("eof", False)
        if not isinstance(eof, bool):
            raise OSError("QGA guest-file-read returned an invalid EOF flag")
        return data, eof or not data

    def read_bytes(self, path: str) -> bytes:
        self._require(self._READ_COMMANDS)
        handle = self._open(path, "rb")
        chunks: typing.List[bytes] = []
        failed = False
        try:
            while True:
                data, eof = self._read_chunk(handle, self.chunk_size, path=path)
                chunks.append(data)
                if eof:
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
        return self._read_chunk(handle, count, path=path)

    def open_read(self, path: str) -> io.BufferedReader:
        self._require(self._READ_COMMANDS)
        return io.BufferedReader(_QGAReadStream(self, path))

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
        if not exclusive:
            # In place, like a local `open(path, "w")`: the file keeps its
            # inode, mode, owner and any symlink, and a Windows guest (whose
            # rename cannot replace) writes at all.
            self._write_direct(path, value)
            return
        if self.helper is None:
            raise NotImplementedError(
                "exclusive QGA writes require a transactional guest helper"
            )
        temporary = f"{path}.hostctl-{uuid.uuid4().hex}"
        try:
            self._write_direct(temporary, value)
            self._helper_method("rename")(temporary, path, replace=False)
        except Exception:
            try:
                self._helper_method("unlink")(temporary, missing_ok=True)
            except Exception:
                pass
            raise

    def _helper_method(self, operation: str):
        if self._closed:
            raise ValueError("the QEMU host that owns this path is closed")
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

    def rename(self, path: str, target: str, *, replace: bool = False) -> None:
        self._helper_method("rename")(path, target, replace=replace)

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


class _QGAReadStream(io.RawIOBase):
    """Lazy QGA guest-file-read stream with one bounded request per fill."""

    def __init__(self, backend: QGAPathBackend, path: str) -> None:
        self._backend = backend
        self._path = path
        self._handle: typing.Optional[int] = None
        self._handle = backend._open(path, "rb")
        self._eof = False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        # The backend has implemented `guest-file-seek` all along and the
        # documentation advertised it, but nothing exposed it: `open("rb")`
        # answered `seekable() == False`, so `zipfile`, `tarfile` and every
        # other reader that looks at the end of a file raised
        # `io.UnsupportedOperation` on a guest path.
        return "guest-file-seek" in self._backend.supported_commands

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if self._handle is None:
            raise ValueError("seek of a closed QGA read stream")
        if not self.seekable():
            raise io.UnsupportedOperation(
                "this guest agent does not support guest-file-seek"
            )
        position = self._backend.seek(self._handle, offset, whence, path=self._path)
        # After a seek the position is not known to be at the end.
        self._eof = False
        return position

    def tell(self) -> int:
        if self._handle is None:
            raise ValueError("tell of a closed QGA read stream")
        return self.seek(0, io.SEEK_CUR)

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


#: The guest-helper operations each protocol's primitives need. QGA's file
#: RPCs move bytes and nothing else, so these hold only with a helper.
_HELPER_OPERATIONS = {
    Stat: ("stat",),
    Listing: ("scandir",),
    Create: ("mkdir",),
    Remove: ("unlink", "rmdir"),
    Chmod: ("chmod",),
    Move: ("rename",),
}


class _QGAPathMixin:
    __slots__ = ()

    def _supports(self, protocol) -> typing.Optional[bool]:
        """Answer from the helper probed at discovery; no request is made."""
        operations = _HELPER_OPERATIONS.get(protocol)
        if operations is None:
            return None
        helper = self.backend.helper
        return helper is not None and all(
            callable(getattr(helper, name, None)) for name in operations
        )

    def exists(self, *, follow_symlinks: bool = True) -> bool:
        # pathapi answers this from `stat()`, swallowing OSError but not
        # NotImplementedError -- so on a helper-less guest the documented
        # boolean probe raised. The backend can still answer with file RPCs.
        return self.backend.exists(str(self), follow_symlinks=follow_symlinks)

    def _check_open(self, mode: str) -> None:
        if "x" in mode and self.backend.helper is None:
            # `staged_open` would return a writable stream and only call
            # `write_bytes` from `close()`, so the refusal arrived after the
            # caller had streamed the whole payload into memory -- from the
            # context manager's exit, where nothing was wrapped in a
            # capability check. Every other unsupported QGA operation is
            # refused before any work happens.
            raise NotImplementedError(
                "exclusive QGA writes require a transactional guest helper"
            )

    @notimplemented(
        "QEMU Guest Agent has no symlink RPC; the guest-file-* commands "
        "only open, read, write, seek, flush, and close regular files"
    )
    def _symlink_to(self, target, target_is_directory: bool = False):
        """Always raise -- QGA exposes no symlink RPC.

        The guest agent's file protocol is limited to opening, reading,
        writing, seeking, flushing, and closing handles.  Emulating a
        symlink through ``guest-exec`` would be a different transport with
        different failure and permission semantics, so the gap is reported
        explicitly instead of being faked.
        """

    @notimplemented(
        "QEMU Guest Agent has no readlink RPC; the guest-file-* commands "
        "only open, read, write, seek, flush, and close regular files"
    )
    def readlink(self): ...

    def _mkdir(self, mode: int):
        self.backend.mkdir(str(self), mode)

    def _rename_on_backend(self, target) -> None:
        # `os.rename`'s own rule per family: a POSIX guest replaces, a Windows
        # guest refuses an existing target.
        self.backend.rename(
            str(self), str(target), replace=not isinstance(self, WindowsPathname)
        )


class PosixQEMUPath(_QGAPathMixin, PosixBackendPath):
    """A POSIX guest path backed by QEMU Guest Agent."""

    __slots__ = ()
    _backend_type = QGAPathBackend


class WindowsQEMUPath(_QGAPathMixin, WindowsBackendPath):
    """A Windows guest path backed by QEMU Guest Agent."""

    __slots__ = ()
    _backend_type = QGAPathBackend
