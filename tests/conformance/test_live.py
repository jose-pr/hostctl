"""Opt-in live provider smoke legs.

The default local leg always runs. Docker and SSH registrations are enabled by
environment variables in CI and skipped transparently otherwise.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hostctl import Exec

from .providers import live_providers, provider_context


@pytest.mark.parametrize("provider", live_providers(), ids=lambda p: p.name)
def test_live_provider_direct_command(provider):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} does not advertise run")
    with provider_context(provider) as host:
        if provider.name == "local":
            result = host.run(Exec(sys.executable, "-c", "print('live')"))
        else:
            command = (
                "Write-Output live"
                if getattr(host.shell_flavour, "name", "") == "powershell"
                else "printf '%s\\n' live"
            )
            result = host.run(command)
    assert result.stdout.strip() == b"live"


def test_every_gate_registers_the_provider_its_tests_look_up(monkeypatch):
    """Guard the guards: a live test that selects a provider by name is
    invisible until CI turns its gate on, and the SFTP percent-encoding guard
    selected `"ssh"` -- a name `live_providers()` never registers -- so it
    raised StopIteration on every parametrization the moment the gate was
    enabled, and had never once run.
    """
    monkeypatch.setenv("HOSTCTL_TEST_SSH_LOCAL", "1")
    monkeypatch.setenv("HOSTCTL_TEST_SSH_URI", "ssh://user@example.invalid")

    names = {p.name for p in live_providers()}

    assert any(name.split("-")[0] == "ssh" for name in names), names


@pytest.mark.skipif(
    os.environ.get("HOSTCTL_TEST_SSH_LOCAL") != "1",
    reason="set HOSTCTL_TEST_SSH_LOCAL=1 to enable localhost sshd leg",
)
def test_ssh_local_gate_is_explicit():
    assert os.environ["HOSTCTL_TEST_SSH_LOCAL"] == "1"


@pytest.mark.skipif(
    os.environ.get("HOSTCTL_TEST_SSH_LOCAL") != "1",
    reason="set HOSTCTL_TEST_SSH_LOCAL=1 to enable localhost sshd leg",
)
@pytest.mark.parametrize(
    "name", ["report%20final.txt", "cache?v=2", "hash#tag.txt", "sp ace.txt"]
)
def test_live_sftp_addresses_uri_syntax_filenames(name):
    """Only a real SFTP server settles this one.

    The remote path is embedded in an `sftp://` URI, and the fakes reach
    their sandbox without going through that construction -- so the encoding
    is proven here and by `tests/test_host_remote.py`, not by the fake
    conformance leg.
    """
    # Any registered SSH leg, by prefix: the localhost sshd leg is named
    # "ssh-local" and the URI-driven one "ssh-uri". Looking up the bare name
    # "ssh" matched neither, so `next()` raised StopIteration before
    # `provider_context` was ever entered -- the designated live guard for the
    # SFTP percent-encoding fix had never once run.
    provider = next(
        (p for p in live_providers() if p.name.split("-")[0] == "ssh"), None
    )
    if provider is None:
        pytest.skip("no live SSH provider is registered")
    with provider_context(provider) as host:
        path = host.path("/tmp", f"hostctl-{name}")
        try:
            path.write_bytes(b"addressed")
            assert path.read_bytes() == b"addressed"
            assert path.name == f"hostctl-{name}"
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


@pytest.mark.skipif(
    os.environ.get("HOSTCTL_TEST_DOCKER") != "1",
    reason="set HOSTCTL_TEST_DOCKER=1 to enable Docker leg",
)
def test_docker_gate_is_explicit():
    assert os.environ["HOSTCTL_TEST_DOCKER"] == "1"
