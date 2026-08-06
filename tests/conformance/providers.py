"""Fake and live provider factories used by the conformance battery."""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from hostctl import HostInfo, HostPath, LocalHost
from hostctl.host._common import starts_direct_command
from hostctl.executor import LocalExecutor
from hostctl.shell import POSIX_SHELL, POWERSHELL
import pytest

from .path_fakes import (
    LocalQgaPathHelper,
    LocalQgaTransport,
    LocalSftpBackend,
    QGA_FILE_COMMANDS,
    WinRMFilesystemRunner,
)

_POWERSHELL_LITERAL = re.compile(r"'(?:''|[^'])*'")


def _direct_powershell_argv(command: str):
    """Decode the simple structured invocation emitted by PowerShellFlavour."""

    marker = " -Command "
    if marker not in command:
        return None
    script = command.rsplit(marker, 1)[1].strip()
    if len(script) >= 2 and script[0] == script[-1] == '"':
        script = script[1:-1]
    if not script.startswith("& ") or not script.endswith("; exit $LASTEXITCODE"):
        return None
    values = [
        match.group(0)[1:-1].replace("''", "'")
        for match in _POWERSHELL_LITERAL.finditer(script)
    ]
    return values or None


class _FakeTransport:
    """Deterministic session/client stand-in used by every fake host.

    It deliberately owns lifecycle state and dispatches through a local
    executor, so contract tests exercise a transport boundary without
    requiring SSH, WinRM, Docker, QGA, or a serial device in CI.
    """

    def __init__(self, name: str, executor: LocalExecutor) -> None:
        self.name = name
        self.executor = executor
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def execute(self, command, *args, **options):
        self.connect()
        return self.executor(command, *args, **options)


class _PopenWriter:
    """asyncssh-shaped stdin over a local pipe."""

    def __init__(self, pipe):
        self._pipe = pipe

    def write(self, data):
        self._pipe.write(data.encode() if isinstance(data, str) else data)

    async def drain(self):
        self._pipe.flush()

    def write_eof(self):
        self._pipe.close()


class _PopenReader:
    """asyncssh-shaped stdout/stderr over a local pipe."""

    def __init__(self, pipe):
        self._pipe = pipe

    async def read(self, size=-1):
        if size is None or size < 0:
            return self._pipe.read()
        return self._pipe.read(size)


class _FakeSshChannel:
    """AsyncSSH ``create_process`` stand-in backed by a local subprocess.

    This is a real bidirectional byte channel, so ``SshProcess`` -- the
    production adapter, driven through hostctl's real ``_async`` bridge --
    is genuinely exercised.  The child is a local shell; nothing here opens a
    socket, resolves a name, or reaches the network.
    """

    def __init__(self, command):
        argv = (
            ["cmd.exe", "/c", command]
            if os.name == "nt"
            else ["/bin/sh", "-c", command]
        )
        self._popen = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.stdin = _PopenWriter(self._popen.stdin)
        self.stdout = _PopenReader(self._popen.stdout)
        self.stderr = _PopenReader(self._popen.stderr)

    @property
    def returncode(self):
        return self._popen.returncode

    def change_terminal_size(self, width, height, pixwidth, pixheight):
        # A pipe has no window size; accepting the resize matches what a
        # real channel does when no PTY was requested.
        return None

    def close(self):
        for pipe in (self._popen.stdin, self._popen.stdout, self._popen.stderr):
            try:
                pipe.close()
            except Exception:
                pass

    def kill(self):
        self._popen.kill()

    def terminate(self):
        self._popen.terminate()

    async def wait(self, check=False, timeout=None):
        returncode = self._popen.wait(timeout=timeout)
        return type("Completed", (), {"returncode": returncode})()

    async def wait_closed(self):
        return None


class FakeSshSession(_FakeTransport):
    def is_closed(self):
        return not self.connected

    async def wait_closed(self):
        return None

    async def create_process(self, command, **options):
        """Back ``_SshTransport.spawn`` with a real streaming channel."""
        self.connect()
        return _FakeSshChannel(command)

    async def run(self, command, **options):
        self.connect()
        if os.name == "nt":
            # AsyncSSH gives the target one finalized command line. Passing
            # that line directly to CreateProcess emulates the same single
            # PowerShell layer; wrapping it in another PowerShell process
            # would expand $env variables before the target script sees them.
            invocation = command
        else:
            invocation = command
        encoding = options.get("encoding")
        stdin = options.get("stdin")
        input_value = stdin.read() if hasattr(stdin, "read") else None
        if os.name == "nt" and (
            bool(input_value) or options.get("timeout") is not None
        ):
            direct = _direct_powershell_argv(command)
            if direct is not None:
                invocation = direct
        if encoding is not None and isinstance(input_value, bytes):
            input_value = input_value.decode(
                encoding,
                options.get("errors") or "strict",
            )
        result = subprocess.run(
            invocation,
            shell=os.name != "nt",
            capture_output=True,
            check=False,
            env=options.get("env"),
            timeout=options.get("timeout"),
            encoding=encoding,
            errors=options.get("errors"),
            text=encoding is not None,
            input=input_value,
        )
        return type(
            "Result",
            (),
            {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )()


class FakeWinRMSession(_FakeTransport):
    def run_ps(self, script):
        executable = "powershell.exe" if os.name == "nt" else shutil.which("pwsh")
        if not executable:
            raise NotImplementedError("PowerShell is unavailable for fake WinRM")
        invocation = (
            script
            if os.name == "nt"
            and script.casefold().startswith(("powershell.exe ", "pwsh "))
            else [executable, "-NoProfile", "-NonInteractive", "-Command", script]
        )
        result = subprocess.run(
            invocation,
            capture_output=True,
            check=False,
        )
        return type(
            "Response",
            (),
            {
                "status_code": result.returncode,
                "std_out": result.stdout,
                "std_err": result.stderr,
            },
        )()


class _ExecResult:
    def __init__(self, result):
        self.exit_code = result.returncode
        self.output = (result.stdout or b"", result.stderr or b"")


class _FakeDockerContainer:
    id = "fake-container"

    def __init__(self):
        self.attrs = {
            "Platform": "windows" if os.name == "nt" else "linux",
            "Architecture": "x86_64",
            "State": {"Running": True},
        }

    def reload(self):
        return None

    def exec_run(self, command, **options):
        env = options.get("environment")
        cwd = options.get("workdir")
        return _ExecResult(
            LocalExecutor()(
                command[0],
                *command[1:],
                env=env,
                cwd=cwd,
                capture_output=True,
                check=False,
            )
        )

    def get_archive(self, path):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            archive.add(path, arcname=os.path.basename(path) or ".")
        value = os.lstat(path)
        metadata = {
            "name": os.path.basename(path),
            "size": value.st_size,
            "mode": value.st_mode,
            "mtime": int(value.st_mtime),
            "linkTarget": os.readlink(path) if os.path.islink(path) else "",
        }
        return [stream.getvalue()], metadata

    def put_archive(self, path, data):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as archive:
            # Python 3.14 defaults extractall() to filter='data', which
            # rejects symlink members whose target is absolute.  Real Docker
            # accepts them -- absolute links are ordinary inside a container
            # image (/etc/localtime -> /usr/share/zoneinfo/...) -- so the fake
            # must not be stricter than the transport it stands in for.
            # 'tar' still blocks traversal outside the destination, which is
            # the property the backend's own _safe_name() also enforces.
            # The keyword does not exist on the 3.9 floor, where extractall()
            # already behaves like 'tar'.
            if sys.version_info >= (3, 12):
                archive.extractall(path, filter="tar")
            else:
                archive.extractall(path)
        return True


class _FakeExecSocket:
    """Docker exec socket stand-in over a finished local subprocess.

    Non-TTY Docker exec output is multiplexed with an 8-byte header per chunk
    (``stream``, three reserved bytes, then a big-endian length), which
    ``ContainerProcess`` demultiplexes.  The fake reproduces that framing so
    the production adapter's real parsing path runs.
    """

    def __init__(self, argv, tty):
        self._completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        if tty:
            payload = self._completed.stdout + self._completed.stderr
        else:
            payload = b"".join(
                struct.pack(">BxxxI", stream, len(data)) + data
                for stream, data in (
                    (1, self._completed.stdout),
                    (2, self._completed.stderr),
                )
                if data
            )
        self._buffer = io.BytesIO(payload)
        self.closed = False

    @property
    def returncode(self):
        return self._completed.returncode

    def recv(self, size):
        return self._buffer.read(size)

    def sendall(self, value):
        # The child already exited, so writes are accepted and discarded --
        # the same observable behaviour as writing to a finished exec.
        return None

    def settimeout(self, value):
        return None

    def shutdown(self, how):
        return None

    def close(self):
        self.closed = True


class _FakeDockerApi:
    """Minimal `docker.APIClient` surface used by ContainerHost.spawn."""

    def __init__(self):
        self._execs = {}
        self._next_id = 0

    def exec_create(self, container, **options):
        self._next_id += 1
        exec_id = f"fake-exec-{self._next_id}"
        self._execs[exec_id] = {"cmd": list(options.get("cmd", ())), "socket": None}
        return {"Id": exec_id}

    def exec_start(self, exec_id, socket=False, tty=False):
        entry = self._execs[exec_id]
        argv = entry["cmd"]
        stream = _FakeExecSocket(argv, tty)
        entry["socket"] = stream
        return stream

    def exec_inspect(self, exec_id):
        stream = self._execs[exec_id]["socket"]
        if stream is None:
            return {"Running": True, "ExitCode": None}
        return {"Running": False, "ExitCode": stream.returncode}

    def exec_resize(self, exec_id, *, height, width):
        return None


class _FakeDockerClient:
    def __init__(self, executor):
        self.executor = executor
        self.container = _FakeDockerContainer()
        self.containers = self
        # Real `docker.DockerClient` exposes the low-level API here; spawn
        # goes through it, so a fake without it made ContainerHost.spawn fail
        # with AttributeError while looking "covered".
        self.api = _FakeDockerApi()

    def get(self, name):
        return self.container

    def close(self):
        return None


class FakeSshHost:
    transport = "ssh"

    def __new__(cls):
        from hostctl import PosixHost, SshConfig
        from hostctl.host._ssh import (
            SftpPathProvider,
            SshExecutorProvider,
            _SshTransport,
        )

        dialect = POWERSHELL if os.name == "nt" else POSIX_SHELL
        config = SshConfig("fake", dialect=dialect, known_hosts=None)
        transport = _SshTransport(config)
        session = FakeSshSession("ssh", LocalExecutor())
        session.connect()
        transport._ssh = session
        backend = LocalSftpBackend()
        transport._sftp_backend = backend
        host = PosixHost(
            config,
            executor_providers=(SshExecutorProvider(transport),),
            path_providers=(SftpPathProvider(transport),),
            shell=dialect,
        )
        host._conformance_cleanup = backend.close
        return host


class FakeWinRMHost:
    transport = "winrm"

    def __new__(cls):
        from hostctl import WindowsHost, WinRMConfig
        from hostctl.host._winrm import (
            WinRMExecutorProvider,
            WinRMPathBackend,
            WinRMPathProvider,
            _WinRMTransport,
        )

        config = WinRMConfig(
            "fake",
            username="fake",
            password="fake",
            provider="pywinrm",
        )
        transport = _WinRMTransport(config)
        transport._session = FakeWinRMSession("winrm", LocalExecutor())
        runner = WinRMFilesystemRunner()
        transport._path_backend = WinRMPathBackend(runner)
        host = WindowsHost(
            config,
            executor_providers=(WinRMExecutorProvider(transport),),
            path_providers=(WinRMPathProvider(transport),),
            shell=POWERSHELL,
        )
        host._conformance_cleanup = runner.close
        return host


class FakeContainerHost:
    transport = "container"

    def __new__(cls):
        from hostctl import ContainerConfig, ContainerHost, HostPath

        client = _FakeDockerClient(LocalExecutor())
        config = ContainerConfig("fake", client_factory=lambda **_: client)
        host = ContainerHost(config)
        return host


class FakeQemuHost:
    transport = "qemu"

    def __new__(cls):
        from hostctl import QemuConfig, QemuHost
        from hostctl.host.qemu import QgaPathBackend

        transport = LocalQgaTransport()
        host = QemuHost(QemuConfig("fake", transport_factory=lambda: transport))
        host.connect()
        host._path_backend = QgaPathBackend(
            transport,
            supported_commands=QGA_FILE_COMMANDS,
            helper=LocalQgaPathHelper(transport),
        )
        return host


class FakeSerialHost:
    transport = "serial"

    def __new__(cls):
        import serial
        from hostctl import RawConsoleProfile, SerialConfig

        port = serial.serial_for_url("loop://", timeout=0.1, write_timeout=1)
        return SerialConfig(
            "loop://", serial_port=port, protocol=RawConsoleProfile()
        )._create_host()


@dataclass(frozen=True)
class Provider:
    name: str
    factory: Callable[[], object]
    capabilities: frozenset[str]
    live: bool = False
    #: Why this transport cannot create symlinks, when ``symlink`` is absent
    #: from :attr:`capabilities`.  Required so a symlink skip always names a
    #: real transport limitation instead of an unexplained attribute error.
    symlink_gap: str = ""

    def __post_init__(self) -> None:
        if "path" in self.capabilities and "symlink" not in self.capabilities:
            if not self.symlink_gap:
                raise ValueError(
                    f"{self.name} must explain why it cannot create symlinks"
                )


def _local() -> tuple[object, Callable[[], None]]:
    return LocalHost(), lambda: None


def _fake(host_type: Callable[[], object]) -> tuple[object, Callable[[], None]]:
    host = host_type()
    return host, getattr(host, "_conformance_cleanup", lambda: None)


def fake_providers() -> tuple[Provider, ...]:
    """Return deterministic providers for every transport family.

    Each factory constructs a concrete :class:`Host` implementation with its
    own fake session/client boundary.  The fake boundary executes locally so
    the shared contract remains deterministic and network-free.
    """

    return (
        Provider(
            "local",
            _local,
            frozenset(
                ("run", "path", "args", "cwd", "env", "input", "timeout", "symlink")
            ),
        ),
        Provider(
            "ssh",
            lambda: _fake(FakeSshHost),
            frozenset(
                (
                    "run",
                    "path",
                    "args",
                    "cwd",
                    "env",
                    "input",
                    "timeout",
                    "symlink",
                    # Backed by _FakeSshChannel: a local subprocess presenting
                    # the asyncssh channel shape, so the production SshProcess
                    # adapter really runs.  Never touches the network.
                    "spawn",
                )
            ),
        ),
        Provider(
            "winrm",
            lambda: _fake(FakeWinRMHost),
            frozenset(("run", "path", "args", "cwd", "env", "symlink")),
        ),
        Provider(
            "container",
            lambda: _fake(FakeContainerHost),
            # `spawn` is backed by _FakeDockerApi/_FakeExecSocket: a local
            # subprocess wrapped in Docker's exec stream framing, so
            # ContainerProcess's real demultiplexing runs.  No daemon, no
            # socket, no network.
            frozenset(("run", "path", "args", "cwd", "env", "symlink", "spawn")),
        ),
        Provider(
            "qemu",
            lambda: _fake(FakeQemuHost),
            frozenset(("run", "path", "args", "env", "input", "timeout")),
            symlink_gap=(
                "QEMU Guest Agent has no symlink RPC (guest-file-* covers only "
                "open/read/write/seek/flush/close)"
            ),
        ),
        Provider(
            "serial",
            lambda: _fake(FakeSerialHost),
            # A serial console is a persistent merged byte stream, so spawn is
            # its native mode; `loop://` is pyserial's in-memory loopback and
            # opens no device and no socket.
            frozenset(("session", "spawn")),
        ),
    )


def conformance_path(host, provider: Provider, tmp_path: Path, *parts: str):
    """Return an existing, transport-native scratch root joined with parts."""

    token = tmp_path.name
    if provider.name == "winrm":
        root = host.path(r"C:\hostctl-conformance", token)
    elif provider.name in ("ssh", "qemu"):
        root = host.path("/hostctl-conformance", token)
    else:
        root = host.path(tmp_path)
    if provider.name != "container":
        root.mkdir(parents=True, exist_ok=True)
    return root.joinpath(*parts)


# Live legs address a real remote whose symlink policy this process cannot
# know ahead of time (a Windows target without Developer Mode, a read-only
# guest, a container image with no writable parent).  They therefore stay out
# of the symlink capability set and say why.
_LIVE_SYMLINK_GAP = "live symlink support depends on the real remote's policy"


def live_providers() -> tuple[Provider, ...]:
    providers = [
        Provider(
            "local",
            _local,
            frozenset(("run", "path", "symlink")),
            True,
        )
    ]
    providers.append(
        Provider("loop-serial", _loop_serial, frozenset(("session",)), True)
    )
    if os.environ.get("HOSTCTL_TEST_SSH_LOCAL") == "1":
        # The SSH fixture is intentionally environment-only.  Connection
        # failures are reported as pytest skips by provider_context, never
        # replaced with LocalHost.
        providers.append(
            Provider(
                "ssh-local",
                _ssh_local,
                frozenset(("run", "path")),
                True,
                symlink_gap=_LIVE_SYMLINK_GAP,
            )
        )
    if os.environ.get("HOSTCTL_TEST_DOCKER") == "1":
        try:
            import docker

            client = docker.from_env()
            client.ping()
        except Exception:
            pass
        else:
            providers.append(
                Provider(
                    "docker-live",
                    _docker_live,
                    frozenset(("run", "path")),
                    True,
                    symlink_gap=_LIVE_SYMLINK_GAP,
                )
            )
    for name, variable, capabilities in (
        ("ssh-uri", "HOSTCTL_TEST_SSH_URI", frozenset(("run", "path"))),
        ("winrm-uri", "HOSTCTL_TEST_WINRM_URI", frozenset(("run", "path"))),
        ("qemu-uri", "HOSTCTL_TEST_QEMU_URI", frozenset(("run", "path"))),
    ):
        uri = os.environ.get(variable)
        if uri:
            providers.append(
                Provider(
                    name,
                    lambda uri=uri: _uri_live(uri),
                    capabilities,
                    True,
                    symlink_gap=_LIVE_SYMLINK_GAP,
                )
            )
    return tuple(providers)


def _uri_live(uri: str) -> tuple[object, Callable[[], None]]:
    from hostctl import HostConfig

    options = {}
    scheme = uri.split(":", 1)[0].casefold()
    if os.environ.get("HOSTCTL_TEST_PASSWORD"):
        options["password"] = os.environ["HOSTCTL_TEST_PASSWORD"]
    if scheme.startswith(("ssh", "qga+")):
        if os.environ.get("HOSTCTL_TEST_SSH_KEY"):
            options["client_keys"] = os.environ["HOSTCTL_TEST_SSH_KEY"]
        if os.environ.get("HOSTCTL_TEST_KNOWN_HOSTS"):
            options["known_hosts"] = os.environ["HOSTCTL_TEST_KNOWN_HOSTS"]
    config = HostConfig(uri, **options)
    host = config._create_host()
    host.connect()
    return host, host.close


def _ssh_local() -> tuple[object, Callable[[], None]]:
    from hostctl import SshConfig

    config = SshConfig(
        "127.0.0.1",
        port=int(os.environ.get("HOSTCTL_TEST_SSH_PORT", "22")),
        username=os.environ.get(
            "HOSTCTL_TEST_SSH_USER", os.environ.get("USER", "runner")
        ),
        client_keys=os.environ.get("HOSTCTL_TEST_SSH_KEY") or None,
        known_hosts=None,
    )
    host = config._create_host()
    try:
        host.connect()
    except Exception:
        host.close()
        raise
    return host, host.close


def _loop_serial() -> tuple[object, Callable[[], None]]:
    import serial
    from hostctl import RawConsoleProfile, SerialConfig

    port = serial.serial_for_url("loop://", timeout=0.1, write_timeout=1)
    config = SerialConfig(
        "loop://",
        serial_port=port,
        protocol=RawConsoleProfile(),
    )
    host = config._create_host()
    host.connect()
    return host, host.close


def _docker_live() -> tuple[object, Callable[[], None]]:
    from hostctl import ContainerConfig

    config = ContainerConfig(
        os.environ.get("HOSTCTL_TEST_DOCKER_CONTAINER", "hostctl-conformance"),
    )
    host = config._create_host()
    try:
        host.connect()
    except Exception:
        host.close()
        raise
    return host, host.close


def _sandbox_for(host, provider: Provider, path):
    """Return the `_Sandbox` backing a fake provider's paths, or None.

    The fakes present target-flavoured absolute paths (`/hostctl-conformance/
    ...`, `C:\\hostctl-conformance\\...`) that have no local existence -- each
    maps into a private temporary root.  Reaching that root is the only way to
    drive a real filesystem call against a fake remote path.

    Each fake reaches its sandbox by a different route (`_sftp_backend._client`
    for SFTP, `_path_backend.runner` for WinRM, the transport itself for QGA),
    so this walks object attributes breadth-first rather than encoding one
    chain per transport -- a hardcoded chain silently returns None when a fake
    is restructured, which reintroduces exactly the misreported skip this
    helper exists to prevent.
    """

    if provider.live or provider.name == "local":
        return None
    roots = list(getattr(path, "_providers", ()) or ())
    roots += [host]
    seen: set = set()
    queue = list(roots)
    while queue:
        current = queue.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        sandbox = getattr(current, "sandbox", None)
        if sandbox is not None and hasattr(sandbox, "local"):
            return sandbox
        for value in list(getattr(current, "__dict__", {}).values()):
            if hasattr(value, "__dict__"):
                queue.append(value)
    return None


def conformance_utime(host, provider: Provider, path, times) -> bool:
    """Set `path`'s mtime through whatever really stores it.

    Returns False only when the provider genuinely cannot set timestamps, so a
    caller can skip for that reason alone.  `os.utime(str(path))` is NOT a
    substitute: for every fake remote provider that call targets a local path
    that does not exist, raising `FileNotFoundError` -- an `OSError` that reads
    as "this transport has no timestamp support" while actually meaning the
    test pointed at the wrong filesystem.
    """

    target = str(path)
    sandbox = _sandbox_for(host, provider, path)
    if sandbox is not None:
        target = str(sandbox.local(target))
    try:
        os.utime(target, times)
    except (OSError, NotImplementedError):
        return False
    return True


@contextlib.contextmanager
def provider_context(provider: Provider) -> Iterator[object]:
    try:
        value = provider.factory()
    except Exception as exc:
        if provider.live:
            pytest.skip(
                f"{provider.name} live leg unavailable: {type(exc).__name__}: {exc}"
            )
        raise
    if isinstance(value, tuple) and len(value) == 2:
        host, cleanup = value
    else:
        host, cleanup = value, lambda: None
    try:
        yield host
    finally:
        try:
            close = getattr(host, "close", None)
            if close:
                close()
        finally:
            cleanup()


def test_provider_registry_is_capability_explicit() -> None:
    providers = fake_providers()
    assert {item.name for item in providers} >= {"local", "ssh", "winrm"}
    for provider in providers:
        assert provider.capabilities
        assert callable(provider.factory)
        # A path provider either advertises symlink support or names the
        # transport limitation; an unexplained gap is a registry bug.
        if "path" in provider.capabilities:
            assert ("symlink" in provider.capabilities) != bool(provider.symlink_gap)


def test_transport_fakes_are_not_local_host_aliases() -> None:
    """Registry entries must not silently collapse back to ``LocalHost``."""

    for provider in fake_providers():
        value = provider.factory()
        host = value[0] if isinstance(value, tuple) else value
        try:
            if provider.name != "local":
                assert not isinstance(host, LocalHost)
                assert type(host) is not LocalHost
        finally:
            close = getattr(host, "close", None)
            if close:
                close()
