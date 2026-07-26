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

from .providers import live_providers, provider_context


@pytest.mark.parametrize("provider", live_providers(), ids=lambda p: p.name)
def test_live_provider_direct_command(provider):
    with provider_context(provider) as host:
        result = host.run(Path(sys.executable), "-c", "print('live')")
    assert result.stdout.strip() == b"live"


@pytest.mark.skipif(
    os.environ.get("HOSTCTL_TEST_SSH_LOCAL") != "1",
    reason="set HOSTCTL_TEST_SSH_LOCAL=1 to enable localhost sshd leg",
)
def test_ssh_local_gate_is_explicit():
    assert os.environ["HOSTCTL_TEST_SSH_LOCAL"] == "1"


@pytest.mark.skipif(
    os.environ.get("HOSTCTL_TEST_DOCKER") != "1",
    reason="set HOSTCTL_TEST_DOCKER=1 to enable Docker leg",
)
def test_docker_gate_is_explicit():
    assert os.environ["HOSTCTL_TEST_DOCKER"] == "1"
