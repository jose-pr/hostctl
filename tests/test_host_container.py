"""Container host behavior with a fake Docker Engine SDK."""

from __future__ import annotations

import io
import subprocess
from pathlib import PurePosixPath

import pytest

from hostctl.host._common import HostConfig
from hostctl.executor.container import normalize_container_error
from hostctl.host.container import ContainerConfig, ContainerHost
from hostctl.shell import POWERSHELL, POSIX_SHELL


class _ExecResult:
    def __init__(self, exit_code=0, output=(b"out", b"err")):
        self.exit_code = exit_code
        self.output = output


class _Container:
    def __init__(self, *, os_name="linux", running=True):
        self.attrs = {
            "Id": "stable-id",
            "Platform": os_name,
            "Architecture": "amd64",
            "State": {"Running": running},
        }
        self.calls = []
        self.reloaded = False

    def reload(self):
        self.reloaded = True

    def exec_run(self, command, **options):
        self.calls.append((command, options))
        return _ExecResult()


class _Containers:
    def __init__(self, container):
        self.container = container
        self.requested = []

    def get(self, name):
        self.requested.append(name)
        return self.container


class _Client:
    def __init__(self, container):
        self.containers = _Containers(container)
        self.closed = False

    def close(self):
        self.closed = True


def _host(*, os_name="linux", running=True, **config_options):
    container = _Container(os_name=os_name, running=running)
    client = _Client(container)
    config = ContainerConfig(
        "build-target",
        client_factory=lambda **options: client,
        **config_options,
    )
    return ContainerHost(config), client, container


def test_container_config_uri_round_trip():
    config = ContainerConfig(
        "build-target",
        engine_url="tcp://engine.example:2376",
        user="1000:1000",
        workdir="/workspace",
        dialect=POSIX_SHELL,
    )
    restored = HostConfig(config.connection_uri)
    assert isinstance(restored, ContainerConfig)
    assert restored.container == "build-target"
    assert restored.engine_url == config.engine_url
    assert restored.user == config.user
    assert restored.workdir == config.workdir
    assert restored.dialect is POSIX_SHELL
    assert str(restored) == str(config)


def test_container_connect_inspects_and_requires_running():
    host, client, container = _host()
    host.connect()
    assert container.reloaded
    assert client.containers.requested == ["build-target"]
    assert host.inspected_os == "linux"

    stopped, _, _ = _host(running=False)
    with pytest.raises(ConnectionError, match="not running"):
        stopped.connect()


@pytest.mark.parametrize(
    ("os_name", "flavour"), (("linux", POSIX_SHELL), ("windows", POWERSHELL))
)
def test_container_auto_shell_uses_inspected_os(os_name, flavour):
    host, _, _ = _host(os_name=os_name)
    assert host.shell_flavour is flavour


def test_container_direct_argv_preserves_arguments_and_context():
    host, _, container = _host(user="1000", workdir="/default")
    result = host.run(
        PurePosixPath("/opt/my tool"),
        "value with spaces",
        cwd="/override",
        env={"COUNT": 3},
        text=True,
    )
    command, options = container.calls[0]
    assert command == ["/opt/my tool", "value with spaces"]
    assert options["workdir"] == "/override"
    assert options["user"] == "1000"
    assert options["environment"] == {"COUNT": "3"}
    assert result.stdout == "out"
    assert result.stderr == "err"


def test_container_raw_command_invokes_selected_shell():
    host, _, container = _host()
    host.run("printf '%s' hello")
    command, _ = container.calls[0]
    assert command[:2] == ["/bin/sh", "-c"]
    assert command[2] == "printf '%s' hello"


def test_container_output_dispatch_and_check():
    host, _, container = _host()
    stdout = io.BytesIO()
    result = host.run("echo ok", stdout=stdout, capture_output=False)
    assert result.stdout is None
    assert stdout.getvalue() == b"out"

    container.exec_run = lambda *args, **kwargs: _ExecResult(7, (b"", b"bad"))
    with pytest.raises(subprocess.CalledProcessError) as raised:
        host.run("false")
    assert raised.value.returncode == 7


def test_container_context_closes_sdk_client():
    host, client, _ = _host()
    with host:
        pass
    assert client.closed


def test_container_executor_rejects_unimplemented_streaming_options():
    host, _, _ = _host()
    with pytest.raises(NotImplementedError, match="stdin"):
        host.run("cat", input=b"value")
    with pytest.raises(NotImplementedError, match="timeout"):
        host.run("sleep 1", timeout=0.1)


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("NotFound", FileNotFoundError),
        ("AuthenticationError", PermissionError),
        ("ReadTimeout", TimeoutError),
        ("DockerException", ConnectionError),
    ),
)
def test_container_sdk_errors_are_normalized(name, expected):
    error_type = type(name, (Exception,), {})
    assert isinstance(normalize_container_error(error_type("failure")), expected)
