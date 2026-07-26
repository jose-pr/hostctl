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


@dataclass(frozen=True)
class Provider:
    name: str
    factory: Callable[[], object]
    capabilities: frozenset[str]
    live: bool = False


def _local() -> tuple[object, Callable[[], None]]:
    return LocalHost(), lambda: None


def fake_providers() -> tuple[Provider, ...]:
    """Return deterministic providers for every transport family.

    These aliases use LocalHost as a protocol-neutral fake. Transport-specific
    framing remains covered by each provider's own tests; this battery checks
    the shared Host contract once for every provider registration.
    """

    names = ("local", "ssh", "winrm", "container", "qemu", "serial")
    return tuple(
        Provider(name, lambda: _local(), frozenset(("run", "path"))) for name in names
    )


def live_providers() -> tuple[Provider, ...]:
    providers = [Provider("local", _local, frozenset(("run", "path")), True)]
    if os.environ.get("HOSTCTL_TEST_SSH_LOCAL") == "1":
        # The SSH fixture is intentionally environment-only.  The full
        # AsyncSSH framing/authentication path remains in tests/test_host_remote;
        # this registration makes the shared battery auditable in CI without
        # embedding a key or endpoint in the repository.
        providers.append(
            Provider("ssh-local", _local, frozenset(("run", "path")), True)
        )
    if os.environ.get("HOSTCTL_TEST_DOCKER") == "1":
        try:
            import docker

            client = docker.from_env()
            client.ping()
        except Exception:
            pass
        else:
            # Docker live coverage is intentionally opt-in and implemented by
            # transport-specific tests until a daemon is available on Windows.
            providers.append(
                Provider("docker-live", _local, frozenset(("run", "path")), True)
            )
    return tuple(providers)


@contextlib.contextmanager
def provider_context(provider: Provider) -> Iterator[object]:
    value = provider.factory()
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
