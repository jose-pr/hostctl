"""Fake and live provider factories used by the conformance battery."""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from hostctl import HostInfo, HostPath, LocalHost
from hostctl.host._common import starts_direct_command
from hostctl.executor import LocalExecutor
from hostctl.shell import POSIX_SHELL, POWERSHELL
import pytest


@dataclass(frozen=True)
class _FakeConfig:
    """Minimal credential-free config carried by a fake transport host."""

    scheme: str
    connection_uri: str


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


class FakeSshSession(_FakeTransport):
    def is_closed(self):
        return not self.connected

    async def wait_closed(self):
        return None

    async def run(self, command, **options):
        self.connect()
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            check=False,
            env=options.get("env"),
            timeout=options.get("timeout"),
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
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", script],
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


class FakeContainerClient(_FakeTransport):
    pass


class FakeQemuTransport(_FakeTransport):
    pass


class FakeSerialTransport(_FakeTransport):
    pass


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


class _FakeDockerClient:
    def __init__(self, executor):
        self.executor = executor
        self.container = _FakeDockerContainer()
        self.containers = self

    def get(self, name):
        return self.container

    def close(self):
        return None


class _FakeQemuGuest(_FakeTransport):
    def __init__(self, executor):
        super().__init__("qemu", executor)
        self._result = None

    def execute(self, request, timeout=None):
        command = request["execute"]
        if command in ("guest-ping", "guest-info"):
            if command == "guest-ping":
                return {}
            return {
                "supported_commands": [
                    {"name": n, "enabled": True}
                    for n in ("guest-exec", "guest-exec-status")
                ]
            }
        if command == "guest-get-osinfo":
            return {
                "id": "linux",
                "pretty-name": "Linux",
                "version": "fake",
                "machine": "x86_64",
            }
        if command == "guest-get-host-name":
            return {"host-name": "fake-qemu"}
        if command == "guest-exec":
            args = request.get("arguments", {})
            path = args.get("path")
            argv = args.get("arg", [])
            result = self.executor(path, *argv, capture_output=True, check=False)
            self._result = result
            return {"pid": 1}
        if command == "guest-exec-status":
            result = self._result
            return {
                "exited": True,
                "exitcode": result.returncode if result else 1,
                "out-data": __import__("base64")
                .b64encode((result.stdout or b"") if result else b"")
                .decode(),
            }
        raise AssertionError(command)


class _TransportFakeHost:
    """Marker shared by concrete transport host fakes below."""


class FakeSshHost:
    transport = "ssh"

    def __new__(cls):
        from hostctl import HostPath, PosixConfig, PosixHost, SshConfig
        from hostctl.host._ssh import SshExecutorProvider, _SshTransport
        from hostctl.provider import PathProvider

        dialect = POWERSHELL if os.name == "nt" else POSIX_SHELL
        config = SshConfig("fake", dialect=dialect, known_hosts=None)
        transport = _SshTransport(config)
        transport._ssh = FakeSshSession("ssh", LocalExecutor())
        return PosixHost(
            config,
            executor_providers=(SshExecutorProvider(transport),),
            path_providers=(PathProvider("fake-sftp", lambda *s: HostPath(*s)),),
            shell=dialect,
        )


class FakeWinRMHost:
    transport = "winrm"

    def __new__(cls):
        from hostctl import HostPath, WindowsConfig, WindowsHost, WinRMConfig
        from hostctl.host._winrm import WinRMExecutorProvider, _WinRMTransport
        from hostctl.provider import PathProvider

        config = WinRMConfig("fake", username="fake", password="fake")
        transport = _WinRMTransport(config)
        transport._session = FakeWinRMSession("winrm", LocalExecutor())
        return WindowsHost(
            config,
            executor_providers=(WinRMExecutorProvider(transport),),
            path_providers=(PathProvider("fake-winrm", lambda *s: HostPath(*s)),),
            shell=POWERSHELL,
        )


class FakeContainerHost:
    transport = "container"

    def __new__(cls):
        from hostctl import ContainerConfig, ContainerHost, HostPath

        client = _FakeDockerClient(LocalExecutor())
        config = ContainerConfig("fake", client_factory=lambda **_: client)
        host = ContainerHost(config)
        host.path = lambda *s, **_: HostPath(*s)  # deterministic archive stand-in
        return host


class FakeQemuHost:
    transport = "qemu"

    def __new__(cls):
        from hostctl import HostPath, QemuConfig, QemuHost

        transport = _FakeQemuGuest(LocalExecutor())
        host = QemuHost(QemuConfig("fake", transport_factory=lambda: transport))
        host.path = lambda *s, **_: HostPath(*s)
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


def _local() -> tuple[object, Callable[[], None]]:
    return LocalHost(), lambda: None


def _fake(host_type: type[_TransportFakeHost]) -> tuple[object, Callable[[], None]]:
    return host_type(), lambda: None


def fake_providers() -> tuple[Provider, ...]:
    """Return deterministic providers for every transport family.

    Each factory constructs a concrete :class:`Host` implementation with its
    own fake session/client boundary.  The fake boundary executes locally so
    the shared contract remains deterministic and network-free.
    """

    return (
        Provider("local", _local, frozenset(("run", "path", "args", "cwd", "env"))),
        Provider("ssh", lambda: _fake(FakeSshHost), frozenset(("run", "path"))),
        Provider("winrm", lambda: _fake(FakeWinRMHost), frozenset(("run", "path"))),
        Provider(
            "container", lambda: _fake(FakeContainerHost), frozenset(("run", "path", "args", "cwd", "env"))
        ),
        Provider("qemu", lambda: _fake(FakeQemuHost), frozenset(("run", "path"))),
        Provider("serial", lambda: _fake(FakeSerialHost), frozenset(("session",))),
    )


def live_providers() -> tuple[Provider, ...]:
    providers = [Provider("local", _local, frozenset(("run", "path")), True)]
    providers.append(
        Provider("loop-serial", _loop_serial, frozenset(("session",)), True)
    )
    if os.environ.get("HOSTCTL_TEST_SSH_LOCAL") == "1":
        # The SSH fixture is intentionally environment-only.  Connection
        # failures are reported as pytest skips by provider_context, never
        # replaced with LocalHost.
        providers.append(
            Provider("ssh-local", _ssh_local, frozenset(("run", "path")), True)
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
                Provider("docker-live", _docker_live, frozenset(("run", "path")), True)
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
                )
            )
    return tuple(providers)


def _uri_live(uri: str) -> tuple[object, Callable[[], None]]:
    from hostctl import HostConfig

    options = {}
    if os.environ.get("HOSTCTL_TEST_PASSWORD"):
        options["password"] = os.environ["HOSTCTL_TEST_PASSWORD"]
    if os.environ.get("HOSTCTL_TEST_SSH_KEY"):
        options["client_keys"] = os.environ["HOSTCTL_TEST_SSH_KEY"]
    options["known_hosts"] = os.environ.get("HOSTCTL_TEST_KNOWN_HOSTS", None)
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
