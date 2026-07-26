"""Fake and live provider factories used by the conformance battery."""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from hostctl import LocalHost
import pytest


class _TransportFakeHost(LocalHost):
    """Deterministic protocol fake with LocalHost's well-tested mechanics.

    The class is intentionally distinct per transport so the registry cannot
    silently claim that an SSH/WinRM/container leg is a LocalHost instance.
    Transport framing remains covered by the dedicated provider tests.
    """

    transport = "fake"

    @property
    def transport_name(self):
        return self.transport


class FakeSshHost(_TransportFakeHost):
    transport = "ssh"


class FakeWinRMHost(_TransportFakeHost):
    transport = "winrm"


class FakeContainerHost(_TransportFakeHost):
    transport = "container"


class FakeQemuHost(_TransportFakeHost):
    transport = "qemu"


class FakeSerialHost(_TransportFakeHost):
    transport = "serial"


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

    These aliases use LocalHost as a protocol-neutral fake. Transport-specific
    framing remains covered by each provider's own tests; this battery checks
    the shared Host contract once for every provider registration.
    """

    return (
        Provider("local", _local, frozenset(("run", "path"))),
        Provider("ssh", lambda: _fake(FakeSshHost), frozenset(("run", "path"))),
        Provider("winrm", lambda: _fake(FakeWinRMHost), frozenset(("run", "path"))),
        Provider(
            "container", lambda: _fake(FakeContainerHost), frozenset(("run", "path"))
        ),
        Provider("qemu", lambda: _fake(FakeQemuHost), frozenset(("run", "path"))),
        Provider("serial", lambda: _fake(FakeSerialHost), frozenset(("run", "path"))),
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
    return tuple(providers)


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
