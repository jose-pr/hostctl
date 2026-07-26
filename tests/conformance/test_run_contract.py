"""Shared subprocess semantics for every registered provider."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .providers import fake_providers, live_providers, provider_context


def test_live_provider_registry_is_env_gated():
    providers = live_providers()
    assert providers and providers[0].name == "local"


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_direct_argv_and_capture(provider):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    with provider_context(provider) as host:
        result = host.run(
            Path(sys.executable),
            "-c",
            "import sys; print(sys.argv[1]); print('err', file=sys.stderr)",
            "a & b",
        )
    assert result.returncode == 0
    assert result.stdout == b"a & b\r\n" if os.name == "nt" else b"a & b\n"
    assert result.stderr.startswith(b"err")


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_text_env_cwd_and_nonzero_check(provider, tmp_path):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    code = "import os, pathlib; print(os.environ['HOSTCTL_CONFORMANCE']); print(pathlib.Path.cwd())"
    with provider_context(provider) as host:
        result = host.run(
            Path(sys.executable),
            "-c",
            code,
            cwd=tmp_path,
            env={"HOSTCTL_CONFORMANCE": 42},
            text=True,
        )
        assert result.stdout.splitlines()[0] == "42"
        assert Path(result.stdout.splitlines()[1]) == tmp_path
        failed = host.run(
            Path(sys.executable), "-c", "raise SystemExit(3)", check=False
        )
        assert failed.returncode == 3
        with pytest.raises(subprocess.CalledProcessError):
            host.run(Path(sys.executable), "-c", "raise SystemExit(4)")


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_silent_capture_is_empty_bytes(provider):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    with provider_context(provider) as host:
        result = host.run(Path(sys.executable), "-c", "pass")
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_shell_command_shapes_and_operators_remain_explicit(provider):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    with provider_context(provider) as host:
        invocation = host.shell_flavour.invocation("echo first")
        if shutil.which(invocation[0]) is None:
            pytest.skip(f"shell executable {invocation[0]!r} is unavailable")
        # Sequence syntax is portable to POSIX sh and Windows PowerShell 5;
        # raw strings remain shell source rather than argv data.
        raw = host.run("echo first; echo second")
        argv = host.run(("echo", "a & b"))
        joined = host.run(("echo", "one"), ("echo", "two"))
    assert b"first" in raw.stdout and b"second" in raw.stdout
    assert b"a & b" in argv.stdout
    assert b"one" in joined.stdout and b"two" in joined.stdout


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_timeout_and_input_are_subprocess_compatible(provider):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    if provider.name != "local":
        pytest.skip(f"{provider.name} does not advertise buffered input/timeout")
    with provider_context(provider) as host:
        result = host.run(
            Path(sys.executable),
            "-c",
            "import sys; print(sys.stdin.read())",
            input="payload",
            text=True,
        )
        assert result.stdout.strip() == "payload"
        with pytest.raises(subprocess.TimeoutExpired):
            host.run(
                Path(sys.executable), "-c", "import time; time.sleep(2)", timeout=0.01
            )
