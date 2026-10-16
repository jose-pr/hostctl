"""The QEMU guest host: guest-agent commands, paths and the serial console."""

from __future__ import annotations


import logging

import re

import subprocess

import threading

import typing


from pathlib import PureWindowsPath


from ._executor import QEMUExecutor

from ._qga import GuestAgentTransport
from ._qga_transports import (
    LibvirtGuestAgentTransport,
    SSHUnixGuestAgentTransport,
    UnixSocketGuestAgentTransport,
)

from ...process import Process


from ...shell import POWERSHELL, POSIX_SHELL, shell_flavour

from ...shell._flavour import ShellFlavour

from ...executor import CaptureOutput, Environment, FileHandle, Input, PathLike

from ...shell._grammar import Command, starts_direct_command

from ...host._host import Host
from ...host._info import HostInfo

from pathapi import Path as HostPath


from ..ssh._config import SSHConfig
from ..ssh._transport import _SSHTransport

from ._config import PathnameConstructor, QEMUConfig
from ._path import PosixQEMUPath, WindowsQEMUPath
from ._backend import QGAPathBackend

#: Sentinel: the guest helper has not been probed yet. `None` is a real
#: answer -- this guest has no helper -- and must not trigger a re-probe on
#: every `path()` call.
log = logging.getLogger("hostctl.transports.qemu")


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
            from ._providers import QGAPathProvider

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
        from ._helper import PosixGuestPathHelper, WindowsGuestPathHelper

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
